"""
Phase 5 Agentic Orchestrator — plan-execute-verify-compose loop.

Replaces the Phase 4 if/elif drug-class router with a single entry point.
The clinical logic (denial scorer, CPIC engine, PA builder) does NOT change —
the orchestrator is only the routing and composition layer on top of it.

Architecture: Plan → Execute → Verify → Compose
  PLAN    : classify drug, look up evidence chain config
  EXECUTE : run each step, collect evidence, build audit log
  VERIFY  : identify gaps in evidence, note them in the log
  COMPOSE : build CDS cards from accumulated evidence

Public entry point:
  orchestrator = Orchestrator()
  result = await orchestrator.process(hook_request)
  return CdsResponse(cards=result.cards)

C# analogy: an async Mediator pipeline — each step is a handler, the
orchestrator is the pipeline that calls them in sequence and aggregates
their outputs into a single AgentResult.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

# Evidence chain configuration (D2)
from app.agents.evidence_chains import get_chain

# Phase 6 — FHIR client + bundle model
from app.fhir.epic_client import EpicFHIRClient
from app.models.fhir_bundle import FhirDataBundle

# Oncology support (D3)
from app.data.seed_synthetic import get_synthetic_oncology_data
from app.rules.nccn_validator import NccnResult, validate_nccn_pathway

# Denial prevention support (D4)
from app.data.seed_synthetic import get_synthetic_denial_data
from app.rules.denial_scorer import ProcedureEvidenceBundle, score_procedure_denial_risk

# Phase 3/4 pipeline functions — orchestrator calls these as steps
from app.agents.denial_prediction import (
    _extract_drug_name,   # noqa: PLC2701  (private import is intentional)
    _extract_rxnorm_code, # noqa: PLC2701
    run_bridge,
)
from app.agents.pgx_safety import run_pgx_pipeline
from app.agents.specialty_pa import build_pa_bundle as _build_pa_bundle

# Card composers
from app.intelligence.card_composer import (
    compose_denial_card,
    compose_error_card,
    compose_from_agent_result,
    compose_pgx_card,
)

# Models
from app.models.cds_hooks import Card, CdsSource, HookRequest, Link, Suggestion
from app.models.domain import AgentResult, DenialRiskResult, PABundle, PipelineError
from app.rules.cpic_engine import PgxResult
from app.rules.drug_classifier import classify_drug

logger = logging.getLogger(__name__)

# Source shown on stub cards for unimplemented scenarios
_SOURCE = CdsSource(label="CFIP Clinical-Financial Intelligence")


# ---------------------------------------------------------------------------
# Internal step result — used only inside the orchestrator during execution.
# Not Pydantic: we store arbitrary Python objects in `data` (DenialRiskResult,
# PgxResult …) which don't need to be serialised at this stage.
# C# analogy: a private record StepResult { string Summary; object Data; }
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    """Outcome of a single evidence-chain step."""

    summary: str                           # human-readable line for chain_log
    data: dict[str, Any] = field(default_factory=dict)  # arbitrary step output
    skipped: bool = False                  # True when step was intentionally bypassed
    error: str | None = None               # non-None if the step threw an exception
    ai_powered: bool = False               # True when step calls an LLM (shown as AI in trace)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """
    Agentic orchestrator — single entry point for all CDS hook processing.

    Replaces the Phase 4 if/elif router in cds_hooks.py.
    All clinical pipelines are called from within this class.

    C# analogy: a sealed class with one public async method and several
    private async helper methods. No shared state between requests.
    """

    async def process(self, hook_request: HookRequest) -> AgentResult:
        """
        Process a CDS hook request end-to-end.

        Phases:
          1. PLAN    — extract drug info, classify, look up chain
          2. EXECUTE — run each step, collect StepResults
          3. VERIFY  — check completeness, log any gaps
          4. COMPOSE — build CDS cards from accumulated evidence

        Never raises: all exceptions are caught and surfaced as error cards.

        C# analogy: async Task<AgentResult> ProcessAsync(HookRequest request)
        """

        # ── 1. PLAN ─────────────────────────────────────────────────────────
        drug_name    = _extract_drug_name(hook_request) or ""
        rxnorm_code  = _extract_rxnorm_code(hook_request)
        drug_class   = classify_drug(drug_name=drug_name, rxnorm_code=rxnorm_code)
        chain        = get_chain(drug_class)
        patient_id   = hook_request.context.get("patientId", "")

        chain_log: list[str] = [
            f"PLAN: patient={patient_id} drug='{drug_name}' "
            f"rxnorm={rxnorm_code} class={drug_class} chain='{chain['name']}'"
        ]

        logger.info(
            "Orchestrator PLAN: patient=%s drug=%s class=%s chain=%s",
            patient_id, drug_name, drug_class, chain["name"],
        )

        # ── 1.5: FHIR FETCH ──────────────────────────────────────────────────
        # Phase 6: authenticate with Epic and fetch real patient data before
        # running any evidence steps.  The bundle is passed to every step so
        # each can use real data where available and synthetic only for gaps.
        # If auth or fetch fails the bundle is empty — all steps degrade
        # gracefully to synthetic overlay with no card failure.
        fhir_bundle = await self._fetch_fhir_bundle(patient_id, chain_log)

        # ── 2. EXECUTE ───────────────────────────────────────────────────────
        # Iterate the ordered step list; pass accumulated evidence forward so
        # later steps can read what earlier steps found.
        # C# analogy: a foreach over a List<string> stepNames, with a
        # shared Dictionary<string, StepResult> evidence passed to each handler.
        evidence: dict[str, StepResult] = {}

        for step in chain["steps"]:
            try:
                step_result = await self._execute_step(
                    step_name=step,
                    hook_request=hook_request,
                    drug_name=drug_name,
                    drug_class=drug_class,
                    evidence=evidence,
                    fhir_bundle=fhir_bundle,
                )
            except Exception as exc:
                # Catch-all: a crashing step must not break the whole request.
                # The card for this scenario may be degraded but will still exist.
                logger.error("Step %s raised: %s", step, exc, exc_info=True)
                step_result = StepResult(
                    summary=f"ERROR — {exc}",
                    error=str(exc),
                )

            evidence[step] = step_result
            if step_result.skipped:
                prefix = "SKIP"
            elif step_result.error:
                prefix = "ERR"
            elif step_result.ai_powered:
                prefix = "AI"
            else:
                prefix = "STEP"
            chain_log.append(f"{prefix} {step}: {step_result.summary}")

        # ── 3. VERIFY ────────────────────────────────────────────────────────
        gaps = self._check_evidence_completeness(evidence, chain)
        for gap in gaps:
            chain_log.append(f"GAP: {gap}")
            logger.warning("Evidence gap detected: %s", gap)

        # ── 4. COMPOSE ───────────────────────────────────────────────────────
        # Extract typed results from the evidence dict.
        # .get() returns None when a step didn't run — safe to pass downstream.
        denial_risk = _get_step_data(evidence, "score_denial_risk", "result")
        pgx_result  = _get_step_data(evidence, "check_cpic", "result")
        pa_bundle   = _get_step_data(evidence, "build_pa_bundle", "result")

        narrative_step   = evidence.get("generate_narrative")
        narrative        = narrative_step.data.get("text", "") if narrative_step else ""
        narrative_source = narrative_step.data.get("source", "template") if narrative_step else "template"

        # Build AgentResult first so compose_from_agent_result() has the narrative.
        # compose_from_agent_result() injects the LLM narrative into glp1/oncology/standard
        # cards and enforces the PGx safety rule (no narrative on safety alerts).
        # Build per-step data source map for card footnotes (Phase 6)
        data_sources = {
            step: result.data["data_source"]
            for step, result in evidence.items()
            if not result.error and not result.skipped and "data_source" in result.data
        }

        agent_result = AgentResult(
            drug=drug_name,
            drug_class=drug_class,
            chain_name=chain["name"],
            denial_risk=denial_risk,
            pgx_result=pgx_result,
            pa_bundle=pa_bundle,
            narrative=narrative,
            narrative_source=narrative_source,
            evidence_chain_log=chain_log,
            fhir_fetched=fhir_bundle.has_real_data,
            data_sources=data_sources,
            cards=[],
        )

        cards = compose_from_agent_result(agent_result)

        chain_log.append(
            f"COMPOSE: {len(cards)} card(s), narrative_source={narrative_source}"
        )

        logger.info(
            "Orchestrator COMPOSE: class=%s cards=%d narrative=%s",
            drug_class, len(cards), narrative_source,
        )

        return agent_result.model_copy(update={"cards": cards, "evidence_chain_log": chain_log})

    # ── Step dispatcher ─────────────────────────────────────────────────────

    async def _execute_step(
        self,
        step_name: str,
        hook_request: HookRequest,
        drug_name: str,
        drug_class: str,
        evidence: dict[str, StepResult],
        fhir_bundle: FhirDataBundle,
    ) -> StepResult:
        """
        Dispatch a step name to its implementation.

        C# analogy: a switch expression mapping string → async delegate.
        New steps added in D3/D4 = new cases here, nothing else changes.
        """
        match step_name:

            # ── GLP-1 chain steps ──────────────────────────────────────────

            case "fetch_labs":
                # A1C and BMI are fetched inside run_bridge() as part of
                # _gather_evidence(). We acknowledge the step here; the actual
                # FHIR call happens when score_denial_risk runs.
                return StepResult(summary="Acknowledged — labs will be fetched during scoring")

            case "fetch_rx_history":
                # Metformin history is also part of run_bridge()/_gather_evidence().
                return StepResult(summary="Acknowledged — Rx history will be fetched during scoring")

            case "fetch_coverage":
                # Coverage/payer lookup is part of run_bridge()/_gather_evidence().
                return StepResult(summary="Acknowledged — coverage will be fetched during scoring")

            case "check_step_therapy":
                # Step therapy check is part of the denial scorer inside run_bridge().
                # Result is visible in DenialRiskResult.met_criteria after scoring.
                return StepResult(summary="Acknowledged — step therapy evaluated during scoring")

            case "check_clinical_criteria":
                # A1C ≥7.0, BMI ≥30 — evaluated inside denial scorer.
                return StepResult(summary="Acknowledged — clinical criteria evaluated during scoring")

            case "score_denial_risk":
                if drug_class == "standard":
                    # Procedure path: build ProcedureEvidenceBundle from earlier steps,
                    # then call the 3-factor procedure scorer.
                    patient_id = hook_request.context.get("patientId", "")
                    proc_evidence = ProcedureEvidenceBundle(
                        procedure_name=drug_name,
                        past_denials_similar=_get_step_data(
                            evidence, "fetch_claims_history", "past_denials_similar"
                        ) or 0,
                        past_denial_reasons=_get_step_data(
                            evidence, "fetch_claims_history", "past_denial_reasons"
                        ) or [],
                        payer_name=_get_step_data(
                            evidence, "fetch_claims_history", "payer_name"
                        ) or "",
                        coverage_active=_get_step_data(
                            evidence, "fetch_claims_history", "coverage_active"
                        ) or False,
                        required_docs=_get_step_data(
                            evidence, "check_documentation", "required_docs"
                        ) or [],
                        docs_on_file=_get_step_data(
                            evidence, "check_documentation", "docs_on_file"
                        ) or [],
                        missing_docs=_get_step_data(
                            evidence, "check_documentation", "missing_docs"
                        ) or [],
                        patient_id=patient_id,
                        data_source="synthetic",
                    )
                    score = score_procedure_denial_risk(proc_evidence)

                    # Convert ScoreResult → DenialRiskResult so _compose_cards()
                    # can reuse compose_denial_card() without branching.
                    # DenialRiskResult is the common output contract for all scoring paths.
                    denial_result = DenialRiskResult(
                        approval_probability=score.approval_probability,
                        risk_level=score.risk_level,
                        indicator=score.indicator,
                        met_criteria=score.met_criteria,
                        unmet_criteria=score.unmet_criteria,
                        suggested_actions=score.suggested_actions,
                        drug_class="standard",
                        drug_name=drug_name,
                        payer=proc_evidence.payer_name,
                        cost_estimate_monthly=0.0,
                        data_source="synthetic",
                        patient_id=patient_id,
                    )
                    return StepResult(
                        summary=(
                            f"Procedure scoring: {score.approval_probability}% approval | "
                            f"risk={score.risk_level} | payer={proc_evidence.payer_name}"
                        ),
                        data={"result": denial_result},
                    )

                else:
                    # GLP-1 path: calls run_bridge() which does all FHIR fetching,
                    # synthetic overlay, payer rule lookup, and weighted scoring.
                    bridge_result = await run_bridge(hook_request)

                    if isinstance(bridge_result, PipelineError):
                        return StepResult(
                            summary=f"Bridge returned error: {bridge_result.message}",
                            data={"error": bridge_result},
                            error=bridge_result.message,
                        )

                    return StepResult(
                        summary=(
                            f"Approval {bridge_result.approval_probability}% | "
                            f"risk={bridge_result.risk_level} | "
                            f"payer={bridge_result.payer}"
                        ),
                        data={"result": bridge_result},
                    )

            case "build_pa_bundle":
                # For GLP-1: builds from DenialRiskResult (existing Phase 3/4 builder).
                # For oncology: builds directly from NCCN + biomarker evidence (D3).
                if drug_class == "oncology":
                    bundle = self._build_oncology_pa_bundle(
                        drug_name=drug_name,
                        evidence=evidence,
                        hook_request=hook_request,
                    )
                else:
                    # GLP-1 path: requires score_denial_risk to have run
                    denial_result: DenialRiskResult | None = _get_step_data(
                        evidence, "score_denial_risk", "result"
                    )
                    if denial_result is None:
                        return StepResult(
                            summary="Skipped — no denial result available",
                            skipped=True,
                        )
                    bundle = _build_pa_bundle(
                        denial_result=denial_result,
                        hook_request=hook_request,
                    )

                return StepResult(
                    summary=(
                        f"PA bundle built — ready={bundle.ready_to_submit} | "
                        f"met={len(bundle.requirements_met)} "
                        f"unmet={len(bundle.requirements_unmet)}"
                    ),
                    data={"result": bundle},
                )

            # ── PGx chain steps ────────────────────────────────────────────

            case "fetch_pgx_data":
                # Genomic observations are fetched inside run_pgx_pipeline().
                # We acknowledge here; check_cpic does the real fetch.
                return StepResult(summary="Acknowledged — PGx data will be fetched during CPIC check")

            case "check_cpic":
                # THE core PGx step: calls run_pgx_pipeline() which fetches
                # genomic FHIR data (or synthetic overlay) and runs the CPIC engine.
                pgx = await run_pgx_pipeline(
                    hook_request=hook_request,
                    drug_name=drug_name,
                )
                severity_note = (
                    "INTERACTION FOUND" if pgx.has_interaction
                    else ("no data — recommend testing" if not pgx.pgx_data_available
                          else "no interaction")
                )
                return StepResult(
                    summary=(
                        f"CPIC check complete — {severity_note} | "
                        f"severity={pgx.severity} | gene={pgx.gene}"
                    ),
                    data={"result": pgx},
                )

            case "suggest_alternative":
                # Read the PGx result from the check_cpic step.
                pgx_r: PgxResult | None = _get_step_data(evidence, "check_cpic", "result")
                if pgx_r is None or not pgx_r.has_interaction:
                    return StepResult(
                        summary="No alternative needed",
                        skipped=not bool(pgx_r),
                    )
                alts = pgx_r.alternative_drug or "consult clinical pharmacist"
                return StepResult(
                    summary=f"Alternative(s) suggested: {alts}",
                    data={"alternatives": alts},
                )

            # ── Oncology chain steps (D3) ──────────────────────────────────

            case "fetch_condition":
                # Phase 6: try real FHIR Condition resources first, fall back to synthetic.
                patient_id = hook_request.context.get("patientId", "")

                fhir_tumor = _extract_tumor_from_fhir(fhir_bundle)
                if fhir_tumor:
                    tumor_type  = fhir_tumor["tumor_type"]
                    icd10       = fhir_tumor["icd10_code"]
                    stage       = fhir_tumor.get("tumor_stage", "")
                    data_source = "fhir"
                    logger.info("fetch_condition: tumor type from FHIR — %s %s", tumor_type, icd10)
                else:
                    scenario = get_synthetic_oncology_data(patient_id, drug_name)
                    if not scenario:
                        return StepResult(
                            summary="No oncology data found for this patient/drug",
                            data={},
                            error="missing_oncology_data",
                        )
                    tumor_type  = scenario.get("tumor_type", "unknown")
                    icd10       = scenario.get("icd10_code", "")
                    stage       = scenario.get("tumor_stage", "")
                    data_source = "synthetic"
                    logger.info("fetch_condition: tumor type from synthetic overlay")

                return StepResult(
                    summary=f"Condition: {tumor_type} {icd10} {stage} [source: {data_source}]",
                    data={
                        "tumor_type": tumor_type,
                        "icd10_code": icd10,
                        "tumor_stage": stage,
                        "data_source": data_source,
                    },
                )

            case "fetch_biomarkers":
                # Phase 6: PD-L1 is not a standard lab in Epic sandbox — it uses
                # proprietary assay codes not returned by the laboratory category search.
                # We check FHIR lab_observations anyway (future-proof), then fall back
                # to synthetic overlay which always has the demo-ready PD-L1 score.
                patient_id = hook_request.context.get("patientId", "")
                scenario   = get_synthetic_oncology_data(patient_id, drug_name)

                fhir_pdl1 = _extract_pdl1_from_fhir(fhir_bundle)
                if fhir_pdl1 is not None:
                    pd_l1       = fhir_pdl1
                    assay       = "Epic FHIR"
                    egfr        = False
                    alk         = False
                    data_source = "fhir"
                    logger.info("fetch_biomarkers: PD-L1 from FHIR — %s%%", pd_l1)
                else:
                    if not scenario:
                        return StepResult(
                            summary="No biomarker data found",
                            data={"pd_l1_score": None, "data_source": "synthetic"},
                        )
                    pd_l1       = scenario.get("pd_l1_score")
                    assay       = scenario.get("pd_l1_assay", "unknown assay")
                    egfr        = scenario.get("egfr_mutation", False)
                    alk         = scenario.get("alk_rearrangement", False)
                    data_source = "synthetic"
                    logger.info("fetch_biomarkers: PD-L1 from synthetic overlay (not in Epic sandbox)")

                return StepResult(
                    summary=(
                        f"Biomarkers: PD-L1={pd_l1}% ({assay}) | "
                        f"EGFR={'positive' if egfr else 'negative'} | "
                        f"ALK={'positive' if alk else 'negative'} [source: {data_source}]"
                    ),
                    data={
                        "pd_l1_score": pd_l1,
                        "pd_l1_assay": assay,
                        "egfr_mutation": egfr,
                        "alk_rearrangement": alk,
                        "data_source": data_source,
                    },
                )

            case "fetch_prior_regimens":
                # Phase 6: scan real FHIR MedicationRequest resources for known
                # chemo agents.  Epic sandbox may have some medication history —
                # if carboplatin or pemetrexed appear, we use real data.
                patient_id = hook_request.context.get("patientId", "")

                fhir_regimens = _extract_chemo_from_fhir(fhir_bundle)
                if fhir_regimens:
                    regimens    = fhir_regimens
                    cycles      = 0   # cycle count not in MedicationRequest
                    status      = "on file (Epic FHIR)"
                    data_source = "fhir"
                    logger.info("fetch_prior_regimens: found in FHIR — %s", regimens)
                else:
                    scenario = get_synthetic_oncology_data(patient_id, drug_name)
                    if not scenario:
                        return StepResult(
                            summary="No prior regimen data found",
                            data={"prior_regimens": [], "data_source": "synthetic"},
                        )
                    regimens    = scenario.get("prior_regimens", [])
                    cycles      = scenario.get("prior_regimen_cycles", 0)
                    status      = scenario.get("prior_regimen_status", "unknown")
                    data_source = "synthetic"
                    logger.info("fetch_prior_regimens: using synthetic overlay")

                return StepResult(
                    summary=(
                        f"Prior regimens: {', '.join(regimens) or 'none'} | "
                        f"{cycles} cycles | {status} [source: {data_source}]"
                    ),
                    data={
                        "prior_regimens": regimens,
                        "prior_regimen_cycles": cycles,
                        "prior_regimen_status": status,
                        "data_source": data_source,
                    },
                )

            case "validate_nccn_pathway":
                # Read biomarker + condition + regimen data from earlier steps,
                # then call the NCCN validator (deterministic lookup table).
                tumor_type = _get_step_data(evidence, "fetch_condition", "tumor_type") or ""
                pd_l1      = _get_step_data(evidence, "fetch_biomarkers", "pd_l1_score")
                regimens   = _get_step_data(evidence, "fetch_prior_regimens", "prior_regimens") or []

                # Resolve brand name → generic for the validator
                from app.rules.nccn_validator import _BRAND_TO_GENERIC as _NCCN_BRAND_MAP
                drug_generic = _NCCN_BRAND_MAP.get(drug_name.lower(), drug_name)

                nccn_result = validate_nccn_pathway(
                    drug=drug_generic,
                    tumor_type=tumor_type,
                    pd_l1_score=pd_l1,
                    prior_regimens=regimens,
                )
                status = "APPROVED" if nccn_result.pathway_approved else "NOT APPROVED"
                return StepResult(
                    summary=(
                        f"NCCN {status}: {nccn_result.indication or 'no matched pathway'} | "
                        f"evidence={nccn_result.evidence_level}"
                    ),
                    data={"result": nccn_result},
                )

            # ── Denial prevention chain steps (D4) ────────────────────────

            case "fetch_claims_history":
                # Phase 6: pull real payer name from FHIR Coverage if present.
                # Past denials (EOB) are not in the Epic public sandbox, so denial
                # history always comes from synthetic overlay.
                patient_id = hook_request.context.get("patientId", "")
                scenario = get_synthetic_denial_data(patient_id, drug_name)

                # Try to get real payer name from FHIR Coverage
                fhir_payer = _extract_payer_from_fhir(fhir_bundle)

                if not scenario:
                    payer_name = fhir_payer or ""
                    return StepResult(
                        summary=f"No claims history found for this patient/procedure (payer={payer_name or 'unknown'})",
                        data={
                            "past_denials_similar": 0,
                            "past_denial_reasons": [],
                            "payer_name": payer_name,
                            "coverage_active": bool(fhir_payer),
                            "data_source": "fhir" if fhir_payer else "synthetic",
                        },
                    )

                denials  = scenario.get("past_denials_similar", 0)
                reasons  = scenario.get("past_denial_reasons", [])
                # Prefer real payer from FHIR Coverage; fall back to synthetic scenario payer
                payer    = fhir_payer or scenario.get("payer_name", "")
                coverage = scenario.get("coverage_active", False)
                payer_source = "fhir" if fhir_payer else "synthetic"

                return StepResult(
                    summary=(
                        f"Claims history: {denials} prior denial(s) with {payer} | "
                        f"coverage={'active' if coverage else 'inactive'} "
                        f"[payer source: {payer_source}]"
                    ),
                    data={
                        "past_denials_similar": denials,
                        "past_denial_reasons": reasons,
                        "payer_name": payer,
                        "coverage_active": coverage,
                        "detail": scenario.get("past_denials_detail", []),
                        "data_source": "mixed" if fhir_payer else "synthetic",
                    },
                )

            case "pattern_match_denials":
                # Identify recurring denial patterns to surface before resubmission.
                denials  = _get_step_data(evidence, "fetch_claims_history", "past_denials_similar") or 0
                reasons  = _get_step_data(evidence, "fetch_claims_history", "past_denial_reasons") or []
                if denials == 0:
                    return StepResult(
                        summary="No prior denial pattern — first submission",
                        data={"pattern": None, "risk": "low"},
                    )
                # Identify the most common reason (mode of the reasons list)
                # Counter.most_common(1) — C# analogy: .GroupBy(r => r).OrderByDescending(g => g.Count()).First()
                from collections import Counter
                most_common = Counter(reasons).most_common(1)
                pattern = most_common[0][0] if most_common else "unknown"
                return StepResult(
                    summary=(
                        f"Denial pattern identified: '{pattern}' "
                        f"({denials} occurrence(s))"
                    ),
                    data={"pattern": pattern, "risk": "high" if denials >= 2 else "moderate"},
                )

            case "check_documentation":
                # Check which required documents are on file vs. missing.
                # Phase 5: synthetic overlay; real FHIR DocumentReference search in Phase 6.
                patient_id = hook_request.context.get("patientId", "")
                scenario = get_synthetic_denial_data(patient_id, drug_name)
                if not scenario:
                    return StepResult(
                        summary="No documentation data found",
                        data={"missing_docs": [], "docs_on_file": [], "required_docs": []},
                    )
                required = scenario.get("required_docs", [])
                on_file  = scenario.get("docs_on_file", [])
                missing  = scenario.get("missing_docs", [])
                status = "complete" if not missing else f"{len(missing)} gap(s) identified"
                return StepResult(
                    summary=(
                        f"Documentation {status}: "
                        f"{len(on_file)}/{len(required)} required docs on file"
                    ),
                    data={
                        "required_docs": required,
                        "docs_on_file": on_file,
                        "missing_docs": missing,
                    },
                )

            # ── Shared last step ───────────────────────────────────────────

            case "generate_narrative":
                # D5: call OpenAI client with 5s timeout + template fallback.
                # PGx chain intentionally never reaches this step (see evidence_chains.py).
                from app.intelligence.openai_client import OpenAIClient
                client  = OpenAIClient()
                context = self._extract_narrative_context(evidence, drug_class, drug_name)
                text, source = await client.generate_narrative(context, drug_class)
                model = "GPT-4o-mini" if source == "openai" else "template fallback"
                return StepResult(
                    summary=f"Clinical narrative generated — {model}",
                    data={"text": text, "source": source},
                    ai_powered=True,
                )

            case _:
                logger.warning("Unknown step '%s' — skipping", step_name)
                return StepResult(
                    summary=f"Unknown step '{step_name}' — skipped",
                    skipped=True,
                )

    # ── FHIR fetch ──────────────────────────────────────────────────────────

    async def _fetch_fhir_bundle(
        self,
        patient_id: str,
        chain_log: list[str],
    ) -> FhirDataBundle:
        """Delegate to the module-level helper (keeps process() readable)."""
        return await _fetch_fhir_bundle_standalone(patient_id, chain_log)

    # ── Verify ──────────────────────────────────────────────────────────────

    def _check_evidence_completeness(
        self,
        evidence: dict[str, StepResult],
        chain: dict,
    ) -> list[str]:
        """
        Identify evidence gaps: steps that errored or were unexpectedly skipped.

        Returns a list of gap descriptions for the chain_log.
        Intentionally stubbed steps (D3/D4) are not counted as gaps.
        """
        gaps: list[str] = []

        for step in chain["steps"]:
            result = evidence.get(step)
            if result is None:
                gaps.append(f"Step '{step}' did not produce a result")
                continue
            if result.error:
                gaps.append(f"Step '{step}' failed: {result.error}")
                continue
            # Steps that are intentionally skipped (stubs or "no alternative needed")
            # don't count as gaps — they're expected in this phase.

        return gaps

    # ── Compose ─────────────────────────────────────────────────────────────

    def _compose_cards(
        self,
        drug_class: str,
        drug_name: str,
        denial_risk: DenialRiskResult | None,
        pgx_result: PgxResult | None,
        pa_bundle: PABundle | None,
        hook_request: HookRequest,
    ) -> list[Card]:
        """
        Build the list of CDS Hooks cards from accumulated evidence.

        Card selection logic:
          glp1         → denial card (+ PA status)
          pgx_sensitive → PGx alert or recommend-testing card
          oncology      → stub info card (D3 will replace)
          standard      → stub info card (D4 will replace)

        Always returns at least one card.
        C# analogy: a factory method returning List<Card>.
        """
        cards: list[Card] = []

        if drug_class == "glp1":
            if denial_risk and not isinstance(denial_risk, PipelineError):
                cards.append(compose_denial_card(denial_risk, pa_bundle))
            else:
                err = denial_risk if isinstance(denial_risk, PipelineError) else None
                cards.append(compose_error_card(
                    err or PipelineError(
                        code="no_result",
                        message="GLP-1 pipeline did not return a result.",
                        recoverable=True,
                    )
                ))

        elif drug_class == "pgx_sensitive":
            if pgx_result:
                card = compose_pgx_card(pgx_result)
                if card:
                    cards.append(card)
                # compose_pgx_card returns None for "all clear" — no card is correct
            if not cards:
                # No PGx data + no card → return an info card
                cards.append(Card(
                    summary=f"{drug_name or 'Drug'}: PGx check complete — no action required",
                    indicator="info",
                    source=_SOURCE,
                    detail=(
                        "### PGx Check — All Clear\n\n"
                        "No clinically significant drug-gene interaction was identified "
                        "for this order based on available genomic data."
                    ),
                    suggestions=[],
                    links=[],
                ))

        elif drug_class == "oncology":
            # D3: Real oncology card built from NCCN result + PA bundle
            nccn_result: NccnResult | None = None
            if pa_bundle is None:
                # No PA bundle — build a minimal card from whatever evidence we have
                cards.append(Card(
                    summary=f"{drug_name or 'Oncology Drug'}: Pathway analysis incomplete",
                    indicator="warning",
                    source=_SOURCE,
                    detail=(
                        "### Oncology Pathway Validation\n\n"
                        "Pathway validation could not be completed. "
                        "Please review the clinical evidence and resubmit."
                    ),
                    suggestions=[],
                    links=[],
                ))
            else:
                status_icon = "✅" if pa_bundle.ready_to_submit else "⚠️"
                pa_status   = "PA Bundle Ready" if pa_bundle.ready_to_submit else "PA Incomplete"
                summary = (
                    f"{status_icon} {drug_name or 'Drug'}: NCCN Pathway Validated | {pa_status}"
                    if pa_bundle.ready_to_submit
                    else f"{status_icon} {drug_name or 'Drug'}: Pathway Review Required | {pa_status}"
                )[:140]

                # Detail section
                lines = ["### Oncology Pathway Validation", ""]
                if pa_bundle.requirements_met:
                    lines.append("**Criteria met:**")
                    for req in pa_bundle.requirements_met:
                        lines.append(f"- ✅ {req}")
                    lines.append("")
                if pa_bundle.requirements_unmet:
                    lines.append("**Outstanding:**")
                    for req in pa_bundle.requirements_unmet:
                        lines.append(f"- ❌ {req}")
                    lines.append("")
                lines.append("**Required documents:**")
                for doc in pa_bundle.supporting_documents[:3]:
                    lines.append(f"- {doc}")
                lines.append("")
                lines.append(f"_Payer: UnitedHealthcare | Drug class: Oncology_")

                suggestions = []
                if pa_bundle.ready_to_submit:
                    suggestions = [
                        Suggestion(label="Submit PA Now", isRecommended=True),
                        Suggestion(label="Review PA Bundle"),
                    ]
                else:
                    suggestions = [Suggestion(label="Address Documentation Gaps")]

                cards.append(Card(
                    summary=summary,
                    indicator="info" if pa_bundle.ready_to_submit else "warning",
                    source=_SOURCE,
                    detail="\n".join(lines),
                    suggestions=suggestions,
                    selectionBehavior="at-most-one" if suggestions else None,
                    links=[Link(
                        label="View Oncology PA Bundle",
                        url="https://placeholder.cfip.app/oncology",
                        type="absolute",
                    )],
                ))

        else:
            # standard — D4: real denial prevention card built from procedure scorer
            if denial_risk and not isinstance(denial_risk, PipelineError):
                cards.append(compose_denial_card(denial_risk, pa_bundle=None))
            else:
                cards.append(Card(
                    summary=f"{drug_name or 'Order'}: Denial prevention analysis unavailable",
                    indicator="info",
                    source=_SOURCE,
                    detail=(
                        "### Denial Prevention Analysis\n\n"
                        "Could not complete the denial prevention analysis for this order. "
                        "Proceed with standard workflow."
                    ),
                    suggestions=[],
                    links=[],
                ))

        return cards

    # ── Oncology PA builder (D3) ─────────────────────────────────────────────

    def _build_oncology_pa_bundle(
        self,
        drug_name: str,
        evidence: dict[str, StepResult],
        hook_request: HookRequest,
    ) -> PABundle:
        """
        Build a PABundle for an oncology PA request from NCCN + biomarker evidence.

        Unlike the GLP-1 PA builder (which takes a DenialRiskResult), this method
        reads the structured evidence collected by the oncology chain steps directly.

        C# analogy: a factory method producing a PABundle from a different input type —
        same return type, different construction path.
        """
        nccn_result: NccnResult | None = _get_step_data(evidence, "validate_nccn_pathway", "result")
        pd_l1       = _get_step_data(evidence, "fetch_biomarkers", "pd_l1_score")
        assay       = _get_step_data(evidence, "fetch_biomarkers", "pd_l1_assay") or "PD-L1 IHC"
        regimens    = _get_step_data(evidence, "fetch_prior_regimens", "prior_regimens") or []
        tumor_type  = _get_step_data(evidence, "fetch_condition", "tumor_type") or "Unknown"
        patient_id  = hook_request.context.get("patientId", "")

        # Patient name from prefetch
        from app.agents.specialty_pa import _extract_patient_name
        patient_name = _extract_patient_name(hook_request)

        # Build typed EvidenceItems from biomarker + NCCN data
        from app.models.domain import EvidenceItem
        clinical_evidence: list[EvidenceItem] = []

        if pd_l1 is not None:
            clinical_evidence.append(EvidenceItem(
                criterion="PD-L1 expression",
                met=True,
                value=f"{pd_l1}% TPS ({assay})",
                source="synthetic",
            ))

        if regimens:
            clinical_evidence.append(EvidenceItem(
                criterion="Prior platinum-based chemotherapy",
                met=True,
                value=", ".join(regimens),
                source="synthetic",
            ))

        if nccn_result:
            clinical_evidence.append(EvidenceItem(
                criterion=f"NCCN pathway ({nccn_result.indication})",
                met=nccn_result.pathway_approved,
                value=nccn_result.evidence_level or "Category 1",
                source="synthetic",
            ))

        # Requirements and readiness
        payer_requirements = [
            "PD-L1 IHC assay result (Dako 22C3) required",
            "NCCN-validated indication documented",
            "EGFR/ALK testing completed (negative for actionable mutations)",
            "Prior regimen documentation (if second-line use)",
        ]

        # Split met vs unmet based on what was found
        requirements_met:   list[str] = []
        requirements_unmet: list[str] = []

        if pd_l1 is not None:
            requirements_met.append(f"PD-L1 testing: {pd_l1}% TPS — documented ✓")
        else:
            requirements_unmet.append("PD-L1 IHC test result — not on file")

        egfr = _get_step_data(evidence, "fetch_biomarkers", "egfr_mutation")
        alk  = _get_step_data(evidence, "fetch_biomarkers", "alk_rearrangement")
        if egfr is False and alk is False:
            requirements_met.append("EGFR negative, ALK negative — no targeted therapy exclusion ✓")
        elif egfr or alk:
            requirements_unmet.append("EGFR/ALK positive — targeted therapy required before checkpoint inhibitor")

        if nccn_result and nccn_result.pathway_approved:
            requirements_met.append(
                f"NCCN {nccn_result.evidence_level}: {nccn_result.indication} ✓"
            )
        elif nccn_result:
            for gap in nccn_result.gaps:
                requirements_unmet.append(gap)

        ready_to_submit = len(requirements_unmet) == 0

        supporting_documents = [
            "PD-L1 pathology report (Dako 22C3 assay)",
            "EGFR mutation test report",
            "ALK rearrangement test report",
            "Oncology progress notes with tumor staging",
            "Prior chemotherapy treatment summary",
            "Letter of medical necessity citing NCCN guideline",
        ]

        approval_probability = 90 if ready_to_submit else 35

        return PABundle(
            drug=drug_name,
            drug_class="oncology",
            payer=_get_step_data(evidence, "fetch_condition", "tumor_type") and "UnitedHealthcare" or "",
            patient_id=patient_id,
            patient_name=patient_name,
            clinical_evidence=clinical_evidence,
            payer_requirements=payer_requirements,
            requirements_met=requirements_met,
            requirements_unmet=requirements_unmet,
            ready_to_submit=ready_to_submit,
            supporting_documents=supporting_documents,
            appeal_notes=None,
            approval_probability=approval_probability,
            data_source="synthetic",
        )

    # ── Narrative template ───────────────────────────────────────────────────

    def _build_template_narrative(
        self,
        drug_class: str,
        drug_name: str,
        evidence: dict[str, StepResult],
    ) -> str:
        """
        Build a template narrative string when OpenAI is not yet wired (D1-D4).

        D5 replaces the body of this method with an OpenAI call; the signature
        and calling convention remain identical.

        C# analogy: a virtual method that D5 overrides with an AI implementation.
        """
        drug = drug_name or drug_class.upper()

        if drug_class == "glp1":
            denial_result: DenialRiskResult | None = _get_step_data(
                evidence, "score_denial_risk", "result"
            )
            if denial_result:
                met_text = "; ".join(denial_result.met_criteria[:2]) or "pending review"
                return (
                    f"{drug} prior authorization pre-check complete. "
                    f"Approval probability: {denial_result.approval_probability}%. "
                    f"Key evidence: {met_text}. "
                    f"Payer: {denial_result.payer}."
                )
            return f"{drug} GLP-1 prior authorization analysis complete."

        if drug_class == "oncology":
            nccn: NccnResult | None = _get_step_data(evidence, "validate_nccn_pathway", "result")
            pd_l1 = _get_step_data(evidence, "fetch_biomarkers", "pd_l1_score")
            tumor = _get_step_data(evidence, "fetch_condition", "tumor_type") or "NSCLC"
            if nccn and nccn.pathway_approved:
                return (
                    f"{drug} oncology pathway validation complete. "
                    f"Indication: {tumor}, PD-L1 {pd_l1}%. "
                    f"NCCN {nccn.evidence_level} pathway confirmed: {nccn.indication}. "
                    f"PA bundle is ready to submit."
                )
            return (
                f"{drug} oncology pathway review required. "
                f"PD-L1: {pd_l1}%. "
                f"Gaps: {'; '.join(nccn.gaps) if nccn else 'pathway validation incomplete'}."
            )

        if drug_class == "standard":
            denial: DenialRiskResult | None = _get_step_data(
                evidence, "score_denial_risk", "result"
            )
            pattern = _get_step_data(evidence, "pattern_match_denials", "pattern")
            missing = _get_step_data(evidence, "check_documentation", "missing_docs") or []
            if denial:
                pattern_note = f" Recurring denial pattern: '{pattern}'." if pattern else ""
                doc_note = (
                    f" Missing: {'; '.join(missing[:2])}." if missing else " Documentation complete."
                )
                return (
                    f"{drug} denial prevention analysis complete. "
                    f"Approval probability: {denial.approval_probability}% "
                    f"({denial.risk_level} risk | {denial.payer}).{pattern_note}{doc_note} "
                    f"Address identified gaps before submission."
                )
            return f"{drug} denial prevention analysis complete."

        # pgx_sensitive does not call generate_narrative — this is a safety net
        return f"{drug} clinical analysis complete."

    def _extract_narrative_context(
        self,
        evidence: dict[str, StepResult],
        drug_class: str,
        drug_name: str,
    ) -> dict[str, str]:
        """
        Extract a flat dict of clinical facts for the OpenAI narrative prompt.

        Converts StepResult.data values to plain strings so the OpenAI client
        can build its prompt without knowing about StepResult internals.

        C# analogy: a mapping method from domain objects to a PromptContext DTO.
        """
        ctx: dict[str, str] = {"drug": drug_name, "drug_class": drug_class}

        if drug_class == "glp1":
            denial: DenialRiskResult | None = _get_step_data(evidence, "score_denial_risk", "result")
            if denial:
                ctx["payer"]               = denial.payer or ""
                ctx["approval_probability"] = str(denial.approval_probability)
                ctx["risk_level"]           = denial.risk_level
                ctx["met_criteria"]         = "; ".join(denial.met_criteria[:3])
                ctx["unmet_criteria"]       = "; ".join(denial.unmet_criteria[:3])

        elif drug_class == "oncology":
            ctx["tumor_type"]   = _get_step_data(evidence, "fetch_condition", "tumor_type") or "NSCLC"
            pd_l1 = _get_step_data(evidence, "fetch_biomarkers", "pd_l1_score")
            ctx["pd_l1_score"]  = str(pd_l1) if pd_l1 is not None else ""
            nccn: NccnResult | None = _get_step_data(evidence, "validate_nccn_pathway", "result")
            if nccn and nccn.pathway_approved:
                # Prefix "APPROVED" so the AI clearly knows the pathway is approved
                ctx["nccn_pathway"] = f"APPROVED — {nccn.indication}" if nccn.indication else "APPROVED"
                ctx["pa_status"]    = "PA bundle ready to submit"
            else:
                gaps_note = f" (gaps: {'; '.join(nccn.gaps[:2])})" if nccn and nccn.gaps else ""
                ctx["nccn_pathway"] = f"not validated{gaps_note}"
                ctx["pa_status"]    = "PA requires review"

        elif drug_class == "standard":
            denial_std: DenialRiskResult | None = _get_step_data(evidence, "score_denial_risk", "result")
            if denial_std:
                ctx["payer"]      = denial_std.payer or ""
                ctx["risk_level"] = denial_std.risk_level
            pattern = _get_step_data(evidence, "pattern_match_denials", "pattern")
            ctx["denial_pattern"] = pattern or ""
            missing = _get_step_data(evidence, "check_documentation", "missing_docs") or []
            ctx["missing_docs"] = "; ".join(missing[:2])

        return ctx


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------

def _get_step_data(
    evidence: dict[str, StepResult],
    step_name: str,
    key: str,
) -> Any:
    """
    Safely extract a value from a step result's data dict.

    Returns None if the step didn't run, errored, or the key is absent.
    C# analogy: evidence.TryGetValue(stepName, out var step)
                  ? step.Data.TryGetValue(key, out var val) ? val : null
                  : null
    """
    step = evidence.get(step_name)
    if step is None or step.error:
        return None
    return step.data.get(key)


# ---------------------------------------------------------------------------
# Phase 6 — Orchestrator FHIR helpers
# ---------------------------------------------------------------------------

async def _fetch_fhir_bundle_standalone(patient_id: str, chain_log: list[str]) -> FhirDataBundle:
    """
    Fetch the patient's FHIR bundle and record the outcome in chain_log.

    Wrapped in a standalone function (not a method) so it can be called
    before the orchestrator builds its full evidence dict.
    Never raises — returns an empty bundle on any failure.
    C# analogy: private async Task<FhirDataBundle> FetchFhirBundleAsync(...)
    """
    try:
        client = EpicFHIRClient()
        bundle = await client.fetch_patient_bundle(patient_id)
        if bundle.has_real_data:
            chain_log.append(
                f"FHIR: authenticated ✓ | fetched patient={patient_id} "
                f"(meds={len(bundle.medications)} labs={len(bundle.lab_observations)} "
                f"conditions={len(bundle.conditions)} coverage={len(bundle.coverage)})"
            )
            logger.info("FHIR bundle fetched for patient %s", patient_id)
        else:
            errors_note = "; ".join(bundle.fetch_errors[:2]) if bundle.fetch_errors else "patient not in sandbox"
            chain_log.append(
                f"FHIR: degraded — {errors_note} — synthetic overlay active"
            )
            logger.warning(
                "FHIR fetch degraded for patient %s — using synthetic overlay", patient_id
            )
        return bundle
    except Exception as exc:
        chain_log.append(f"FHIR: error — {exc} — synthetic overlay active")
        logger.error("FHIR fetch error for patient %s: %s", patient_id, exc)
        return FhirDataBundle(fetch_errors=[str(exc)], fetched_from="prefetch_only")


def _extract_tumor_from_fhir(bundle: FhirDataBundle) -> dict | None:
    """
    Scan FHIR Condition resources for NSCLC / lung cancer diagnoses.

    Returns a dict with tumor_type and icd10_code if found, else None.
    ICD-10 C34.x = malignant neoplasm of bronchus and lung.
    C# analogy: conditions.FirstOrDefault(c => c.Code.StartsWith("C34"))
    """
    LUNG_CANCER_PREFIXES = ("C34", "C33")   # C33 = trachea, C34 = bronchus/lung
    for condition in bundle.conditions:
        for coding in condition.get("code", {}).get("coding", []):
            code: str = coding.get("code", "")
            if code.startswith(LUNG_CANCER_PREFIXES):
                display: str = coding.get("display", "") or condition.get("code", {}).get("text", "")
                return {
                    "tumor_type": "NSCLC" if "C34" in code else "Lung cancer",
                    "icd10_code": code,
                    "tumor_stage": "",   # stage is in separate staging resources, not basic Condition
                    "display": display,
                }
    return None


def _extract_pdl1_from_fhir(bundle: FhirDataBundle) -> float | None:
    """
    Try to find a PD-L1 lab observation in the FHIR bundle.

    Epic sandbox does not include PD-L1 in its public test data (it uses
    proprietary assay codes), so this will almost always return None.
    Included for completeness — returns None gracefully so synthetic overlay
    fills the gap.
    C# analogy: observations.FirstOrDefault(o => o.IsPdL1()) -- always null in sandbox
    """
    PDL1_CODES = {"85319-2", "101206-5"}   # LOINC codes sometimes used for PD-L1
    PDL1_KEYWORDS = {"pd-l1", "pdl1", "pd l1"}

    for obs in bundle.lab_observations:
        for coding in obs.get("code", {}).get("coding", []):
            loinc = coding.get("code", "")
            display = coding.get("display", "").lower()
            if loinc in PDL1_CODES or any(kw in display for kw in PDL1_KEYWORDS):
                # Extract the numeric value
                value_qty = obs.get("valueQuantity", {})
                value = value_qty.get("value")
                if value is not None:
                    return float(value)
    return None


def _extract_chemo_from_fhir(bundle: FhirDataBundle) -> list[str]:
    """
    Scan FHIR MedicationRequest resources for known chemotherapy agents.

    Epic sandbox may include some historical medications.  We match on display
    name (case-insensitive substring) rather than RxNorm codes because
    chemo brand/generic naming varies widely.

    Returns a deduplicated list of found drug names.
    C# analogy: meds.Select(m => m.Medication?.Display?.ToLower())
                    .Where(n => knownChemo.Any(c => n.Contains(c)))
                    .Distinct().ToList()
    """
    CHEMO_AGENTS = {
        "carboplatin", "pemetrexed", "cisplatin",
        "paclitaxel", "docetaxel", "gemcitabine",
        "vincristine", "cyclophosphamide",
    }
    found: list[str] = []

    for med in bundle.medications:
        # MedicationRequest can reference by code or by resource reference
        concept = med.get("medicationCodeableConcept", {})
        display_text = (
            concept.get("text", "")
            or next(
                (c.get("display", "") for c in concept.get("coding", [])),
                "",
            )
        ).lower()

        for agent in CHEMO_AGENTS:
            if agent in display_text and agent not in found:
                found.append(agent)

    return found


def _extract_payer_from_fhir(bundle: FhirDataBundle) -> str | None:
    """
    Extract the first payer display name from FHIR Coverage resources.

    Epic public sandbox typically returns no Coverage resources — so this
    will usually return None and the synthetic payer name is used.
    C# analogy: coverages.FirstOrDefault()?.Payor?.FirstOrDefault()?.Display
    """
    for coverage in bundle.coverage:
        for payor in coverage.get("payor", []):
            name: str = payor.get("display", "")
            if name:
                logger.info("Payer from FHIR Coverage: %s", name)
                return name
    return None
