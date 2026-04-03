"""
Clinical-Financial Bridge — linear pipeline for GLP-1 denial prediction.

Phase 3: Linear pipeline (not agentic yet). One drug class, one payer.
Phase 5: This becomes the first agent in the agentic orchestrator.

Pipeline steps:
  1. Extract patient ID and medication from hook context / prefetch
  2. Classify drug → drug class
  3. Get active Coverage → payer name
  4. Load payer requirements from SQLite
  5. Gather clinical evidence from FHIR (A1C, BMI, Rx history, Coverage)
  6. Fill gaps from synthetic overlay (if USE_SYNTHETIC_OVERLAY=true)
  7. Run denial scorer
  8. Return DenialRiskResult

Entry point: run_bridge(hook_request) → DenialRiskResult | PipelineError
"""

from __future__ import annotations

import logging
from datetime import date

from app.config import get_settings
from app.data.seed_synthetic import get_synthetic_data
from app.fhir.client import FhirClient, days_since, parse_fhir_date
from app.models.cds_hooks import HookRequest
from app.models.domain import DenialRiskResult, PipelineError
from app.rules.denial_scorer import EvidenceBundle, score_glp1_denial_risk
from app.rules.drug_classifier import classify_drug
from app.rules.payer_rules import get_denial_patterns, get_payer_requirements

logger = logging.getLogger(__name__)

# RxNorm ingredient code for metformin — used when searching MedicationRequests
_METFORMIN_RXNORM_CODES = ["6809"]

# LOINC codes for the observations we need
_LOINC_A1C = "4548-4"
_LOINC_BMI = "39156-5"

# Cost estimate per scenario — hardcoded placeholder values.
# A real formulary/coverage lookup (via FHIR Coverage resource) is needed to replace these.
_COST_ESTIMATES: dict[str, float] = {
    "glp1": 150.00,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_bridge(
    hook_request: HookRequest,
) -> DenialRiskResult | PipelineError:
    """
    Run the full Clinical-Financial Bridge pipeline for a hook request.

    This is an async function — it must be awaited by the hook handler.
    C# analogy: Task<DenialRiskResult> RunBridgeAsync(HookRequest request)

    Returns DenialRiskResult on success, PipelineError on failure.
    The hook handler checks the return type and acts accordingly.

    Union return type (A | B): the function can return either type.
    The caller checks: isinstance(result, PipelineError) to branch.
    C# analogy: Result<DenialRiskResult, PipelineError> (discriminated union).
    """
    settings = get_settings()

    # --- Step 1: Extract context from hook request ---
    patient_id = hook_request.context.get("patientId", "")
    if not patient_id:
        return PipelineError(
            code="missing_patient_id",
            message="No patientId found in hook context.",
            recoverable=True,
        )

    drug_name = _extract_drug_name(hook_request)
    rxnorm_code = _extract_rxnorm_code(hook_request)

    logger.info(
        "Bridge started: patient=%s drug=%s rxnorm=%s",
        patient_id, drug_name, rxnorm_code,
    )

    # --- Step 2: Classify drug ---
    drug_class = classify_drug(drug_name=drug_name, rxnorm_code=rxnorm_code)
    logger.info("Drug classified as: %s", drug_class)

    # Phase 3 only handles GLP-1. For other classes return a graceful error
    # that the card composer will turn into an informational stub card.
    if drug_class != "glp1":
        return PipelineError(
            code="unsupported_drug_class",
            message=f"Drug class '{drug_class}' not yet supported. Phase 3 covers GLP-1 only.",
            recoverable=True,
        )

    # --- Steps 3-6: Gather evidence ---
    try:
        evidence = await _gather_evidence(
            patient_id=patient_id,
            drug_name=drug_name or "",
            hook_request=hook_request,
            use_synthetic=settings.use_synthetic_overlay,
        )
    except Exception as exc:
        logger.error("Evidence gathering failed: %s", exc, exc_info=True)
        return PipelineError(
            code="fhir_unavailable",
            message=f"Could not gather clinical evidence: {exc}",
            recoverable=True,
        )

    # --- Step 4 (inside gather): resolve payer name ---
    payer_name = evidence.payer_name or "UnitedHealthcare"

    # --- Step 4: Load payer requirements ---
    requirements = get_payer_requirements(drug_class, payer_name)
    if requirements is None:
        return PipelineError(
            code="no_payer_rules",
            message=f"No PA rules found for {drug_class} / {payer_name}.",
            recoverable=True,
        )

    denial_patterns = get_denial_patterns(drug_class, payer_name)

    # --- Step 7: Score ---
    score_result = score_glp1_denial_risk(
        evidence=evidence,
        requirements=requirements,
        denial_patterns=denial_patterns,
    )

    logger.info(
        "Bridge complete: approval=%d%% risk=%s indicator=%s",
        score_result.approval_probability,
        score_result.risk_level,
        score_result.indicator,
    )

    # --- Step 8: Return DenialRiskResult ---
    return DenialRiskResult(
        approval_probability=score_result.approval_probability,
        risk_level=score_result.risk_level,
        indicator=score_result.indicator,
        met_criteria=score_result.met_criteria,
        unmet_criteria=score_result.unmet_criteria,
        suggested_actions=score_result.suggested_actions,
        drug_class=drug_class,
        drug_name=drug_name or "",
        payer=payer_name,
        cost_estimate_monthly=_COST_ESTIMATES.get(drug_class, 0.0),
        data_source=evidence.data_source,
        patient_id=patient_id,
    )


# ---------------------------------------------------------------------------
# Evidence gathering — FHIR calls + synthetic overlay
# ---------------------------------------------------------------------------

async def _gather_evidence(
    patient_id: str,
    drug_name: str,
    hook_request: HookRequest,
    use_synthetic: bool,
) -> EvidenceBundle:
    """
    Fetch all clinical evidence needed for GLP-1 scoring.

    Strategy:
      - Try FHIR for every field (real data preferred)
      - If FHIR returns nothing AND use_synthetic is True, fill from synthetic overlay
      - Track data_source: "fhir" | "synthetic" | "mixed"
    """
    # Load synthetic data for this patient (may be None)
    synthetic = get_synthetic_data(patient_id) if use_synthetic else None

    # Start with defaults — everything "unknown"
    evidence = EvidenceBundle(
        patient_id=patient_id,
        drug_name=drug_name,
    )

    # `async with` opens the FHIR client connection pool for the duration of
    # this block and closes it automatically when done.
    # C# analogy: await using (var client = new FhirClient()) { ... }
    async with FhirClient() as fhir:

        # --- Coverage → payer name ---
        payer_name = await _fetch_payer_name(fhir, patient_id, hook_request)
        if payer_name is None and synthetic:
            payer_name = synthetic.get("payer_name")
        evidence.payer_name = payer_name or ""
        evidence.coverage_active = payer_name is not None
        evidence.drug_on_formulary = True  # assumed True in Phase 3; real check in Phase 4

        # --- A1C ---
        a1c_value, a1c_days_old = await _fetch_latest_observation(fhir, patient_id, _LOINC_A1C)
        if a1c_value is None and synthetic:
            a1c_value = synthetic.get("a1c_value")
            a1c_days_old = synthetic.get("a1c_days_old")

        evidence.a1c_value = a1c_value
        evidence.a1c_days_old = a1c_days_old

        # --- BMI ---
        bmi_value, bmi_days_old = await _fetch_latest_observation(fhir, patient_id, _LOINC_BMI)
        if bmi_value is None and synthetic:
            bmi_value = synthetic.get("bmi_value")
            bmi_days_old = synthetic.get("bmi_days_old")

        evidence.bmi_value = bmi_value
        evidence.bmi_days_old = bmi_days_old

        # --- Metformin history ---
        metformin_days = await _fetch_metformin_days(fhir, patient_id)
        if metformin_days is None and synthetic:
            metformin_days = synthetic.get("metformin_days")

        evidence.metformin_days = metformin_days

    # Fill remaining fields from synthetic if still missing
    if synthetic:
        if not evidence.has_t2d_diagnosis:
            evidence.has_t2d_diagnosis = synthetic.get("has_t2d_diagnosis", False)
        if not evidence.has_weight_comorbidity:
            evidence.has_weight_comorbidity = synthetic.get("has_weight_comorbidity", False)
        if evidence.past_denials_similar == 0:
            evidence.past_denials_similar = synthetic.get("past_denials_similar", 0)

    # Determine data_source label for audit trail
    evidence.data_source = _determine_data_source(evidence, synthetic, use_synthetic)

    return evidence


async def _fetch_payer_name(
    fhir: FhirClient,
    patient_id: str,
    hook_request: HookRequest,
) -> str | None:
    """
    Try to get the payer name from:
    1. Prefetch (Coverage already fetched by Epic and included in the hook request)
    2. Direct FHIR call if not in prefetch
    Returns None if no active coverage found.
    """
    # Check prefetch first — avoid a redundant FHIR call if Epic already sent Coverage
    prefetch = hook_request.prefetch or {}
    coverage_prefetch = prefetch.get("coverage")

    if coverage_prefetch and isinstance(coverage_prefetch, dict):
        # Prefetch is a raw FHIR resource dict — extract payor display name
        payors = coverage_prefetch.get("payor", [])
        if payors:
            return payors[0].get("display")

    # Fall back to direct FHIR call
    try:
        coverages = await fhir.get_coverage(patient_id)
        if coverages:
            return coverages[0].payor_name
    except Exception as exc:
        logger.warning("Coverage FHIR call failed: %s", exc)

    return None


async def _fetch_latest_observation(
    fhir: FhirClient,
    patient_id: str,
    loinc_code: str,
) -> tuple[float | None, int | None]:
    """
    Fetch the most recent Observation for a LOINC code.

    Returns a tuple: (value, days_old)
      value     — the numeric result (e.g. 7.5 for A1C, 33.0 for BMI)
      days_old  — how many days ago the observation was recorded

    Returns (None, None) if no observation found or parsing fails.

    Tuple return: Python functions can return multiple values as a tuple.
    The caller unpacks: a1c_value, a1c_days_old = await _fetch_latest_observation(...)
    C# analogy: (float? value, int? daysOld) return type.
    """
    try:
        observations = await fhir.get_observations(patient_id, loinc_code)
        if not observations:
            return (None, None)

        # Take the first result (sorted by -date, so it's the most recent)
        obs = observations[0]

        # FHIR Observation value is in valueQuantity.value
        value_quantity = obs.get("valueQuantity", {})
        value = value_quantity.get("value")
        if value is None:
            return (None, None)

        # Parse the observation date — FHIR uses effectiveDateTime or effectivePeriod
        date_str = obs.get("effectiveDateTime") or obs.get("effectivePeriod", {}).get("start")
        obs_date = parse_fhir_date(date_str)
        age_days = days_since(obs_date)

        return (float(value), age_days)

    except Exception as exc:
        logger.warning("Observation fetch failed (LOINC %s): %s", loinc_code, exc)
        return (None, None)


async def _fetch_metformin_days(
    fhir: FhirClient,
    patient_id: str,
) -> int | None:
    """
    Estimate how many days the patient has been on metformin.

    Strategy: find the oldest authoredOn date among all metformin MedicationRequests
    and calculate days from that date to today.

    Returns None if no metformin history found.
    """
    try:
        med_requests = await fhir.get_medication_requests(patient_id, _METFORMIN_RXNORM_CODES)
        if not med_requests:
            return None

        # Collect all authoredOn dates across metformin prescriptions
        # and find the earliest one to estimate total therapy duration.
        authored_dates: list[date] = []
        for req in med_requests:
            date_str = req.get("authoredOn")
            obs_date = parse_fhir_date(date_str)
            if obs_date:
                authored_dates.append(obs_date)

        if not authored_dates:
            return None

        # min() returns the smallest value in a list.
        # C# analogy: authoredDates.Min()
        earliest = min(authored_dates)
        return days_since(earliest)

    except Exception as exc:
        logger.warning("MedicationRequest fetch failed (metformin): %s", exc)
        return None


def _determine_data_source(
    evidence: EvidenceBundle,
    synthetic: dict | None,
    use_synthetic: bool,
) -> str:
    """
    Return 'fhir', 'synthetic', or 'mixed' based on where evidence came from.
    Used in the card footer and audit trail.
    """
    if not use_synthetic or synthetic is None:
        return "fhir"

    # If we have real FHIR values for the key fields, it's 'fhir'.
    # If all key fields came from synthetic, it's 'synthetic'.
    # Otherwise, it's 'mixed'.
    has_real_a1c = evidence.a1c_value is not None
    has_real_bmi = evidence.bmi_value is not None
    has_real_metformin = evidence.metformin_days is not None

    real_count = sum([has_real_a1c, has_real_bmi, has_real_metformin])

    if real_count == 3:
        return "fhir"
    elif real_count == 0:
        return "synthetic"
    else:
        return "mixed"


# ---------------------------------------------------------------------------
# Context extraction helpers
# ---------------------------------------------------------------------------

def _extract_drug_name(hook_request: HookRequest) -> str | None:
    """
    Extract the drug name from the hook context or prefetch.

    CDS Hooks order-select spec: draftOrders Bundle lives in context, not prefetch.
    We check context first (spec-correct), then fall back to prefetch (legacy/harness).
    """
    def _name_from_bundle(bundle: dict) -> str | None:
        """Pull the first medication or procedure name from a FHIR Bundle dict."""
        for entry in bundle.get("entry", []):
            resource = entry.get("resource", {})
            resource_type = resource.get("resourceType")

            if resource_type == "MedicationRequest":
                med = resource.get("medicationCodeableConcept", {})
                name = med.get("text")
                if name:
                    return name
                codings = med.get("coding", [])
                if codings:
                    return codings[0].get("display")

            elif resource_type == "ServiceRequest":
                # Procedure orders (MRI, X-ray, labs) use ServiceRequest.code
                code = resource.get("code", {})
                name = code.get("text")
                if name:
                    return name
                codings = code.get("coding", [])
                if codings:
                    return codings[0].get("display")

        return None

    # 1. draftOrders in context — correct per CDS Hooks order-select spec
    draft_orders = hook_request.context.get("draftOrders", {})
    if isinstance(draft_orders, dict):
        name = _name_from_bundle(draft_orders)
        if name:
            return name

    prefetch = hook_request.prefetch or {}

    # 2. draftOrders in prefetch — accepted as fallback
    draft_orders_pf = prefetch.get("draftOrders", {})
    if isinstance(draft_orders_pf, dict):
        name = _name_from_bundle(draft_orders_pf)
        if name:
            return name

    # 3. medications prefetch bundle (simpler, no resourceType filter needed)
    medications = prefetch.get("medications", {})
    if isinstance(medications, dict):
        for entry in medications.get("entry", []):
            resource = entry.get("resource", {})
            med = resource.get("medicationCodeableConcept", {})
            name = med.get("text")
            if name:
                return name

    return None


def _extract_rxnorm_code(hook_request: HookRequest) -> str | None:
    """
    Extract the RxNorm code from the MedicationRequest coding list.

    Checks context.draftOrders first (CDS Hooks spec), then prefetch.draftOrders.
    """
    def _rxnorm_from_bundle(bundle: dict) -> str | None:
        """Pull the first RxNorm code from a FHIR Bundle dict."""
        for entry in bundle.get("entry", []):
            resource = entry.get("resource", {})
            if resource.get("resourceType") == "MedicationRequest":
                codings = resource.get("medicationCodeableConcept", {}).get("coding", [])
                for coding in codings:
                    if "rxnorm" in coding.get("system", "").lower():
                        return coding.get("code")
        return None

    # 1. context.draftOrders — correct per CDS Hooks spec
    draft_orders = hook_request.context.get("draftOrders", {})
    if isinstance(draft_orders, dict):
        code = _rxnorm_from_bundle(draft_orders)
        if code:
            return code

    # 2. prefetch.draftOrders — fallback
    prefetch = hook_request.prefetch or {}
    draft_orders_pf = prefetch.get("draftOrders", {})
    if isinstance(draft_orders_pf, dict):
        code = _rxnorm_from_bundle(draft_orders_pf)
        if code:
            return code

    return None
