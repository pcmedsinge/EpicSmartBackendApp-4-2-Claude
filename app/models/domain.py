"""
Domain models — the shared data contracts that cross module boundaries.

These are not FHIR types (those live in fhir_types.py) and not API request/response
models (those live in cds_hooks.py). They are the internal language of CFIP's pipeline:
  bridge → DenialRiskResult → card_composer → CDS card

C# analogy: domain DTOs in an Application layer — separate from API contracts
and persistence models.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    # Imported only for type-checker visibility — no runtime circular import risk.
    # AgentResult references Card (cds_hooks) and PgxResult (cpic_engine), both of
    # which have no back-dependency on domain.py.
    from app.models.cds_hooks import Card
    from app.rules.cpic_engine import PgxResult


class DenialRiskResult(BaseModel):
    """
    The output of the Clinical-Financial Bridge pipeline.

    Produced by: app/agents/denial_prediction.py
    Consumed by: app/intelligence/card_composer.py

    Combines the scorer's output (approval_probability, risk_level, criteria lists)
    with context the bridge adds (cost estimate, drug class, payer, data source).
    """

    # Core scoring output
    approval_probability: int = Field(
        description="Approval likelihood as a 0-100 integer. Directly maps to scorer points.",
    )
    risk_level: str = Field(
        description="'low' | 'moderate' | 'high' — derived from approval_probability.",
    )
    indicator: str = Field(
        description="CDS Hooks card indicator: 'info' | 'warning' | 'critical'.",
    )

    # Evidence breakdown — used to build the card detail section
    met_criteria: list[str] = Field(
        default_factory=list,  # default_factory=list avoids the mutable-default-argument
        description="Human-readable lines for criteria that were satisfied.",
    )
    unmet_criteria: list[str] = Field(
        default_factory=list,
        description="Human-readable lines for criteria that were NOT satisfied.",
    )
    suggested_actions: list[str] = Field(
        default_factory=list,
        description="Recommended actions for each unmet criterion.",
    )

    # Context added by the bridge
    drug_class: str = Field(description="Drug class: 'glp1' | 'oncology' | 'pgx_sensitive' | 'standard'.")
    drug_name: str = Field(default="", description="Drug name as ordered (e.g. 'Ozempic').")
    payer: str = Field(default="", description="Payer name (e.g. 'UnitedHealthcare').")
    cost_estimate_monthly: float = Field(
        default=0.0,
        description="Indicative monthly patient cost (copay/coinsurance). Hardcoded placeholder — not derived from patient formulary or Coverage resource.",
    )

    # Audit / transparency
    data_source: str = Field(
        default="fhir",
        description="'fhir' | 'synthetic' | 'mixed' — origin of evidence data.",
    )
    patient_id: str = Field(default="", description="Epic patient ID (for audit trail).")


class EvidenceItem(BaseModel):
    """
    A single piece of clinical evidence in a PA bundle.
    Produced by: app/agents/specialty_pa.py
    """
    criterion: str = Field(description="What the payer requires (e.g. 'Step therapy: metformin ≥90 days').")
    met: bool = Field(description="True if this criterion is satisfied.")
    value: str = Field(description="The actual value found (e.g. 'metformin 180 days').")
    source: str = Field(default="fhir", description="'fhir' | 'synthetic' — where the value came from.")


class PABundle(BaseModel):
    """
    A structured prior authorization bundle for a specialty drug order.

    Produced by: app/agents/specialty_pa.py
    Consumed by: app/intelligence/card_composer.py (PA-ready card)

    Phase 4: structured documentation only — no FHIR Claim/$submit yet.
    Phase 5: this bundle will be submitted via FHIR Claim resource.
    """
    drug: str = Field(description="Drug name (e.g. 'Ozempic').")
    drug_class: str = Field(description="Drug class (e.g. 'glp1').")
    payer: str = Field(default="", description="Payer name (e.g. 'UnitedHealthcare').")

    # Patient context — populated from hook prefetch
    patient_id: str = Field(default="")
    patient_name: str = Field(default="")

    # Evidence breakdown — maps directly from DenialRiskResult criteria lists
    clinical_evidence: list[EvidenceItem] = Field(default_factory=list)
    payer_requirements: list[str] = Field(
        default_factory=list,
        description="What the payer requires for this drug class.",
    )
    requirements_met: list[str] = Field(default_factory=list)
    requirements_unmet: list[str] = Field(default_factory=list)

    # PA readiness
    ready_to_submit: bool = Field(
        description="True when all payer requirements are met and bundle is complete.",
    )
    supporting_documents: list[str] = Field(
        default_factory=list,
        description="Documents that should be attached to the PA submission.",
    )
    appeal_notes: str | None = Field(
        default=None,
        description="Pre-filled appeal notes if a prior denial exists for this drug/payer.",
    )

    # Approval probability carried from the denial pipeline for card display
    approval_probability: int = Field(default=0)
    data_source: str = Field(default="fhir")


class AgentResult(BaseModel):
    """
    Single output contract from the Phase 5 Orchestrator.

    Returned by Orchestrator.process() and consumed by:
      - The CDS hook handler  (takes result.cards)
      - The card composer     (compose_from_agent_result)
      - The SMART companion   (Phase 6 — full result available)
      - Tests                 (validate chain, narrative, evidence)

    C# analogy: a result record from a MediatR handler — everything
    the pipeline produced, typed and available for downstream use.
    """

    drug: str = Field(default="", description="Drug/procedure name from the hook context.")
    drug_class: str = Field(
        default="standard",
        description="'glp1' | 'oncology' | 'pgx_sensitive' | 'standard'",
    )
    chain_name: str = Field(default="", description="Human-readable evidence chain label.")

    # Typed results from Phase 3/4 pipelines — None when that pipeline didn't run
    denial_risk: DenialRiskResult | None = Field(
        default=None,
        description="Output of the Clinical-Financial Bridge (glp1 / standard chains).",
    )
    # PgxResult is from app.rules.cpic_engine — use Any to avoid circular import at runtime.
    # The TYPE_CHECKING import above gives IDE type-checking support.
    # C# analogy: object typed as IPgxResult at runtime, PgxResult at compile time.
    pgx_result: Any = Field(
        default=None,
        description="Output of the CPIC engine (pgx_sensitive chain). Type: PgxResult | None.",
    )
    pa_bundle: PABundle | None = Field(
        default=None,
        description="Output of the Specialty PA Builder (glp1 / oncology chains).",
    )

    # Narrative — LLM-generated (D5) or template fallback
    narrative: str = Field(
        default="",
        description="Clinical summary narrative for the card detail section.",
    )
    narrative_source: str = Field(
        default="template",
        description="'openai' when LLM generated the text, 'template' when fallback was used.",
    )

    # Audit trail — every step that ran (or was skipped) is recorded here
    evidence_chain_log: list[str] = Field(
        default_factory=list,
        description="Ordered list of step outcomes for regulators / debugging.",
    )

    # Phase 6 — FHIR integration tracking
    fhir_fetched: bool = Field(
        default=False,
        description="True when Epic FHIR API was called and returned at least a Patient resource.",
    )
    data_sources: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-step data origin: step_name → 'fhir' | 'synthetic' | 'prefetch'. "
            "Used by the card composer to render data source footnotes."
        ),
    )

    # Final output — goes directly into CdsResponse.cards.
    # Uses Any to avoid importing Card (cds_hooks.py) at runtime.
    # Type: list[Card]
    cards: list[Any] = Field(
        default_factory=list,
        description="CDS Hooks cards to return to the EHR.",
    )


class AppealLetter(BaseModel):
    """
    A prior authorization appeal letter draft.

    Produced by: app/intelligence/appeal_generator.py
    Consumed by: card composer (added as "View Appeal Draft" card link)

    Phase 5: content is LLM-generated (OpenAI) with template fallback.
    Phase 6: delivered via the SMART Companion App to the provider.

    C# analogy: a result record from IAppealGenerator.GenerateAsync().
    """

    drug: str = Field(description="Drug or procedure name this appeal covers.")
    payer: str = Field(default="", description="Payer name (addressed to their medical director).")
    denial_reason: str = Field(
        default="",
        description="The denial reason code or text this letter addresses.",
    )

    content: str = Field(description="Full letter text — formal appeal to the medical director.")
    source: str = Field(
        default="template",
        description="'openai' when LLM generated the letter, 'template' when fallback was used.",
    )
    addressed_to: str = Field(
        default="Medical Director",
        description="Salutation target — always 'Medical Director' for now.",
    )

    evidence_references: list[str] = Field(
        default_factory=list,
        description="Met criteria strings cited as evidence in the appeal (from DenialRiskResult).",
    )
    generated_for_risk_level: str = Field(
        default="high",
        description="The risk level that triggered the appeal: 'high' | 'moderate'.",
    )


class PipelineError(BaseModel):
    """
    Returned by the bridge when something goes wrong during evidence gathering or scoring.

    The hook handler catches this and converts it to a safe error card rather
    than letting a 500 propagate to Epic.

    C# analogy: a Result<T, Error> pattern — the bridge returns either a
    DenialRiskResult (success) or a PipelineError (failure).
    """
    code: str = Field(description="Short error code, e.g. 'fhir_unavailable' | 'no_payer_rules'.")
    message: str = Field(description="Human-readable description of what went wrong.")
    recoverable: bool = Field(
        default=True,
        description="True if the clinician can still proceed (just without CFIP's analysis).",
    )
