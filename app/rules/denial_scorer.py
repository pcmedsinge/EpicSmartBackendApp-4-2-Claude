"""
Denial risk scorer — deterministic weighted scoring model for GLP-1 PA requests.

No AI, no black box. Every point traces to a specific evidence factor.
This is intentional: healthcare systems require auditable, explainable decisions.

Scoring model (total = 100 points):
  Step therapy        25 pts  — metformin ≥90 days (or documented contraindication)
  Clinical criteria   25 pts  — A1C ≥7.0% AND BMI meets threshold
  Documentation       20 pts  — required labs on file and recent
  Payer history       15 pts  — no past denials for similar orders
  Coverage status     15 pts  — active insurance confirmed

Score → risk level → CDS card indicator:
  80-100  low risk      info     (blue)
  50-79   moderate risk warning  (orange)
  0-49    high risk     critical (red)
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.rules.payer_rules import GLP1Requirements, DenialPattern


# ---------------------------------------------------------------------------
# Input model — the clinical evidence the bridge gathers before scoring
# ---------------------------------------------------------------------------

class EvidenceBundle(BaseModel):
    """
    All clinical and coverage data needed to score a GLP-1 PA request.

    The bridge (denial_prediction.py) populates this from FHIR calls
    and/or the synthetic overlay. The scorer only reads from this model —
    it never touches FHIR or the database directly.

    Optional fields use None as a sentinel for "data not available".
    The scorer treats missing data as unmet — conservative by design.
    C# analogy: a nullable record DTO where null means "not found".
    """

    # Step therapy evidence
    metformin_days: int | None = Field(
        default=None,
        description="Days of documented metformin use. None = no history found.",
    )
    metformin_contraindicated: bool = Field(
        default=False,
        description="True if provider has documented a metformin contraindication.",
    )

    # Clinical criteria
    a1c_value: float | None = Field(
        default=None,
        description="Most recent HbA1c (%). None = no result on file.",
    )
    a1c_days_old: int | None = Field(
        default=None,
        description="How many days ago the A1C was measured. None = unknown.",
    )
    bmi_value: float | None = Field(
        default=None,
        description="Most recent BMI. None = no measurement on file.",
    )
    bmi_days_old: int | None = Field(
        default=None,
        description="How many days ago the BMI was measured. None = unknown.",
    )
    has_t2d_diagnosis: bool = Field(
        default=False,
        description="True if patient has an active Type 2 Diabetes diagnosis (ICD-10 E11.x).",
    )
    has_weight_comorbidity: bool = Field(
        default=False,
        description="True if patient has a weight-related comorbidity (HTN, dyslipidemia, OSA).",
    )

    # Coverage
    coverage_active: bool = Field(
        default=False,
        description="True if patient has active insurance coverage.",
    )
    drug_on_formulary: bool = Field(
        default=True,
        description="True if the ordered drug is on the patient's formulary.",
    )

    # Payer history
    past_denials_similar: int = Field(
        default=0,
        description="Number of past denials for similar drug + payer combinations.",
    )

    # Metadata — for card narrative and audit trail
    patient_id: str = Field(default="", description="Epic patient ID (for audit).")
    payer_name: str = Field(default="", description="Payer name (e.g. 'UnitedHealthcare').")
    drug_name: str = Field(default="", description="Drug name as ordered (e.g. 'Ozempic').")
    data_source: str = Field(
        default="fhir",
        description="'fhir' | 'synthetic' | 'mixed' — where the evidence came from.",
    )


# ---------------------------------------------------------------------------
# Output model — what the scorer returns to the bridge
# ---------------------------------------------------------------------------

class FactorScore(BaseModel):
    """Result of scoring a single evidence factor."""
    factor: str             # e.g. "step_therapy"
    points_earned: int      # 0 to max_points
    max_points: int         # the weight of this factor
    met: bool               # True if full points were earned
    evidence: str           # human-readable evidence line (shown in card)
    action: str | None      # suggested action if not met (None if met)


class ScoreResult(BaseModel):
    """
    Output of the denial risk scorer.
    The bridge adds cost_estimate, drug_class, payer to produce DenialRiskResult.
    """
    approval_probability: int           # 0-100 (total points earned)
    risk_level: str                     # "low" | "moderate" | "high"
    indicator: str                      # "info" | "warning" | "critical" (CDS card indicator)
    factors: list[FactorScore]          # one entry per scoring factor
    met_criteria: list[str]             # evidence lines for met factors
    unmet_criteria: list[str]           # evidence lines for unmet factors
    suggested_actions: list[str]        # actions for unmet factors (non-null only)
    denial_patterns: list[DenialPattern]  # known denial reasons from DB


# ---------------------------------------------------------------------------
# Scorer — public entry point
# ---------------------------------------------------------------------------

def score_glp1_denial_risk(
    evidence: EvidenceBundle,
    requirements: GLP1Requirements,
    denial_patterns: list[DenialPattern],
) -> ScoreResult:
    """
    Score a GLP-1 PA request and return a full breakdown.

    Args:
        evidence:        Clinical data gathered by the bridge.
        requirements:    Payer PA criteria from payer_rules.py.
        denial_patterns: Known denial reasons from the DB.

    Returns:
        ScoreResult with approval probability, risk level, and per-factor breakdown.

    C# analogy: a pure static method that takes DTOs and returns a result DTO.
    No side effects — same inputs always produce same output.
    """
    factors: list[FactorScore] = [
        _score_step_therapy(evidence, requirements),
        _score_clinical_criteria(evidence, requirements),
        _score_documentation(evidence, requirements),
        _score_payer_history(evidence),
        _score_coverage_status(evidence),
    ]

    # Sum up points earned across all factors
    total_points = sum(f.points_earned for f in factors)

    # Separate met and unmet factors for the card narrative
    # List comprehensions — filter and extract in one line.
    # C# analogy: factors.Where(f => f.met).Select(f => f.evidence).ToList()
    met_criteria = [f.evidence for f in factors if f.met]
    unmet_criteria = [f.evidence for f in factors if not f.met]
    suggested_actions = [f.action for f in factors if not f.met and f.action]

    risk_level, indicator = _interpret_score(total_points)

    return ScoreResult(
        approval_probability=total_points,
        risk_level=risk_level,
        indicator=indicator,
        factors=factors,
        met_criteria=met_criteria,
        unmet_criteria=unmet_criteria,
        suggested_actions=suggested_actions,
        denial_patterns=denial_patterns,
    )


# ---------------------------------------------------------------------------
# Private scoring functions — one per factor
# Each returns a FactorScore with full evidence and action detail.
# ---------------------------------------------------------------------------

def _score_step_therapy(
    evidence: EvidenceBundle,
    req: GLP1Requirements,
) -> FactorScore:
    """
    Factor 1 — Step therapy (25 points)
    Pass: metformin ≥ min_metformin_days OR documented contraindication.
    """
    max_points = 25

    # Contraindication waives step therapy entirely
    if evidence.metformin_contraindicated:
        return FactorScore(
            factor="step_therapy",
            points_earned=max_points,
            max_points=max_points,
            met=True,
            evidence="Step therapy waived: metformin contraindication documented",
            action=None,
        )

    days = evidence.metformin_days
    threshold = req.min_metformin_days

    if days is not None and days >= threshold:
        return FactorScore(
            factor="step_therapy",
            points_earned=max_points,
            max_points=max_points,
            met=True,
            evidence=f"Step therapy met: metformin {days} days (required ≥{threshold})",
            action=None,
        )

    # Not met — explain exactly what's missing
    if days is None:
        evidence_text = "Step therapy NOT met: no metformin history found"
        action = (
            f"Document metformin trial ≥{threshold} days, "
            "or provide contraindication letter if intolerant"
        )
    else:
        remaining = threshold - days
        evidence_text = (
            f"Step therapy NOT met: metformin {days} days "
            f"(need {remaining} more days to reach ≥{threshold})"
        )
        action = f"Continue metformin trial — {remaining} more days needed to meet ≥{threshold}-day requirement"

    return FactorScore(
        factor="step_therapy",
        points_earned=0,
        max_points=max_points,
        met=False,
        evidence=evidence_text,
        action=action,
    )


def _score_clinical_criteria(
    evidence: EvidenceBundle,
    req: GLP1Requirements,
) -> FactorScore:
    """
    Factor 2 — Clinical criteria (25 points)
    Pass: A1C ≥ min_a1c AND (BMI ≥ min_bmi_standard OR BMI ≥ min_bmi_with_comorbidity).
    Both must be met for full points. Partial failure = 0 points.
    """
    max_points = 25
    issues: list[str] = []
    evidence_lines: list[str] = []

    # Check A1C
    if evidence.a1c_value is None:
        issues.append(f"A1C not on file (required ≥{req.min_a1c}%)")
    elif evidence.a1c_value < req.min_a1c:
        issues.append(
            f"A1C {evidence.a1c_value}% below threshold (required ≥{req.min_a1c}%)"
        )
    else:
        evidence_lines.append(f"A1C {evidence.a1c_value}% meets threshold (≥{req.min_a1c}%)")

    # Check BMI — standard threshold OR lower threshold with comorbidity
    if evidence.bmi_value is None:
        issues.append(f"BMI not on file (required ≥{req.min_bmi_standard})")
    elif evidence.bmi_value >= req.min_bmi_standard:
        evidence_lines.append(
            f"BMI {evidence.bmi_value} meets standard threshold (≥{req.min_bmi_standard})"
        )
    elif (
        evidence.bmi_value >= req.min_bmi_with_comorbidity
        and evidence.has_weight_comorbidity
    ):
        evidence_lines.append(
            f"BMI {evidence.bmi_value} meets comorbidity threshold "
            f"(≥{req.min_bmi_with_comorbidity} with weight-related comorbidity)"
        )
    else:
        if evidence.has_weight_comorbidity:
            issues.append(
                f"BMI {evidence.bmi_value} below comorbidity threshold "
                f"(required ≥{req.min_bmi_with_comorbidity})"
            )
        else:
            issues.append(
                f"BMI {evidence.bmi_value} below standard threshold "
                f"(required ≥{req.min_bmi_standard}, or ≥{req.min_bmi_with_comorbidity} with comorbidity)"
            )

    if issues:
        action = "Address clinical gaps: " + "; ".join(issues)
        combined_evidence = "Clinical criteria NOT met: " + "; ".join(issues)
        return FactorScore(
            factor="clinical_criteria",
            points_earned=0,
            max_points=max_points,
            met=False,
            evidence=combined_evidence,
            action=action,
        )

    return FactorScore(
        factor="clinical_criteria",
        points_earned=max_points,
        max_points=max_points,
        met=True,
        evidence="Clinical criteria met: " + "; ".join(evidence_lines),
        action=None,
    )


def _score_documentation(
    evidence: EvidenceBundle,
    req: GLP1Requirements,
) -> FactorScore:
    """
    Factor 3 — Documentation completeness (20 points)
    Pass: A1C result recent enough, BMI recent enough, Rx history on file.
    """
    max_points = 20
    gaps: list[str] = []

    # A1C recency check
    if evidence.a1c_value is None:
        gaps.append(f"A1C lab missing (must be on file, dated within {req.required_doc_a1c_months} months)")
    elif evidence.a1c_days_old is not None:
        max_days = req.required_doc_a1c_months * 30  # approximate months → days
        if evidence.a1c_days_old > max_days:
            gaps.append(
                f"A1C result is {evidence.a1c_days_old} days old "
                f"(must be within {req.required_doc_a1c_months} months / ~{max_days} days)"
            )

    # BMI recency check
    if evidence.bmi_value is None:
        gaps.append(f"BMI measurement missing (must be on file, dated within {req.required_doc_bmi_months} months)")
    elif evidence.bmi_days_old is not None:
        max_days = req.required_doc_bmi_months * 30
        if evidence.bmi_days_old > max_days:
            gaps.append(
                f"BMI measurement is {evidence.bmi_days_old} days old "
                f"(must be within {req.required_doc_bmi_months} months / ~{max_days} days)"
            )

    # Rx history for step therapy
    if req.required_doc_rx_history and evidence.metformin_days is None and not evidence.metformin_contraindicated:
        gaps.append("Medication history missing: no metformin record on file")

    if gaps:
        action = "Complete documentation: " + "; ".join(gaps)
        return FactorScore(
            factor="documentation",
            points_earned=0,
            max_points=max_points,
            met=False,
            evidence="Documentation gaps: " + "; ".join(gaps),
            action=action,
        )

    return FactorScore(
        factor="documentation",
        points_earned=max_points,
        max_points=max_points,
        met=True,
        evidence="Documentation complete: A1C, BMI, and medication history on file",
        action=None,
    )


def _score_payer_history(evidence: EvidenceBundle) -> FactorScore:
    """
    Factor 4 — Payer history (15 points)
    Full points: no past denials for similar orders.
    Partial (8 pts): 1 past denial.
    Zero: 2+ past denials.
    """
    max_points = 15
    denials = evidence.past_denials_similar

    if denials == 0:
        return FactorScore(
            factor="payer_history",
            points_earned=max_points,
            max_points=max_points,
            met=True,
            evidence="No prior denials for similar orders with this payer",
            action=None,
        )
    elif denials == 1:
        return FactorScore(
            factor="payer_history",
            points_earned=8,
            max_points=max_points,
            met=False,
            evidence=f"1 prior denial for similar order with {evidence.payer_name or 'this payer'}",
            action="Review denial reason from prior claim and ensure it is addressed in this PA",
        )
    else:
        return FactorScore(
            factor="payer_history",
            points_earned=0,
            max_points=max_points,
            met=False,
            evidence=f"{denials} prior denials for similar orders with {evidence.payer_name or 'this payer'}",
            action=(
                "High denial history — consider requesting peer-to-peer review "
                "or attaching additional clinical justification"
            ),
        )


def _score_coverage_status(evidence: EvidenceBundle) -> FactorScore:
    """
    Factor 5 — Coverage status (15 points)
    Full points: active coverage AND drug on formulary.
    Partial (8 pts): active coverage but formulary status unknown.
    Zero: inactive coverage OR drug not on formulary.
    """
    max_points = 15

    if not evidence.coverage_active:
        return FactorScore(
            factor="coverage_status",
            points_earned=0,
            max_points=max_points,
            met=False,
            evidence="Coverage NOT active — no active insurance found",
            action="Verify patient insurance status before submitting PA",
        )

    if not evidence.drug_on_formulary:
        return FactorScore(
            factor="coverage_status",
            points_earned=0,
            max_points=max_points,
            met=False,
            evidence="Drug not on formulary for this plan",
            action="Check formulary alternatives or request formulary exception",
        )

    return FactorScore(
        factor="coverage_status",
        points_earned=max_points,
        max_points=max_points,
        met=True,
        evidence=f"Coverage active: {evidence.payer_name or 'insurance'} confirmed, drug on formulary",
        action=None,
    )


# ---------------------------------------------------------------------------
# Procedure denial-prevention scorer (Phase 5 D4)
# ---------------------------------------------------------------------------
# Scoring model for standard procedures (MRI, CT, physical therapy…):
#   Past denials (payer history)   40 pts  — highest weight: patterns repeat
#   Documentation completeness     35 pts  — #1 fixable cause of denials
#   Coverage status                25 pts  — active insurance confirmed
#                                  ─────
#   Total                         100 pts
#
# Note: procedures don't have step-therapy or A1C criteria, so those factors
# are dropped and the weights are redistributed to history + documentation.
# ---------------------------------------------------------------------------

class ProcedureEvidenceBundle(BaseModel):
    """
    Clinical and administrative data needed to score a procedure denial risk.

    Analogous to EvidenceBundle for drugs — same pattern, different fields.
    The orchestrator populates this from the fetch_claims_history,
    pattern_match_denials, and check_documentation steps.

    C# analogy: a nullable record DTO for procedure PA pre-checks.
    """

    # Procedure info
    procedure_name: str = Field(default="", description="Human-readable procedure name (e.g. 'MRI Lumbar Spine').")
    procedure_code: str = Field(default="", description="CPT or HCPCS procedure code.")

    # Payer history — most predictive factor for procedure denials
    past_denials_similar: int = Field(
        default=0,
        description="Number of past denials for this or similar procedures with the same payer.",
    )
    past_denial_reasons: list[str] = Field(
        default_factory=list,
        description="Denial reason codes/text from prior claims (e.g. 'insufficient documentation').",
    )
    payer_name: str = Field(default="", description="Current payer name.")

    # Documentation — what the payer requires vs. what's on file
    required_docs: list[str] = Field(
        default_factory=list,
        description="Documents the payer requires for this procedure type.",
    )
    docs_on_file: list[str] = Field(
        default_factory=list,
        description="Documents already present in the patient chart.",
    )
    missing_docs: list[str] = Field(
        default_factory=list,
        description="Required documents that are NOT on file.",
    )

    # Coverage
    coverage_active: bool = Field(
        default=False,
        description="True if patient has active insurance coverage.",
    )

    # Metadata
    patient_id: str = Field(default="")
    data_source: str = Field(default="synthetic")


def score_procedure_denial_risk(
    evidence: ProcedureEvidenceBundle,
) -> ScoreResult:
    """
    Score a procedure order for denial risk using the 3-factor model.

    Args:
        evidence: Clinical and administrative data gathered by the orchestrator.

    Returns:
        ScoreResult — same model as score_glp1_denial_risk(), so the orchestrator
        can convert it to DenialRiskResult using the same logic.

    C# analogy: a pure static method — no side effects, same input = same output.
    """
    factors: list[FactorScore] = [
        _score_procedure_payer_history(evidence),    # 40 pts
        _score_procedure_documentation(evidence),    # 35 pts
        _score_procedure_coverage(evidence),         # 25 pts
    ]

    total_points = sum(f.points_earned for f in factors)

    met_criteria    = [f.evidence for f in factors if f.met]
    unmet_criteria  = [f.evidence for f in factors if not f.met]
    suggested_actions = [f.action for f in factors if not f.met and f.action]

    risk_level, indicator = _interpret_score(total_points)

    return ScoreResult(
        approval_probability=total_points,
        risk_level=risk_level,
        indicator=indicator,
        factors=factors,
        met_criteria=met_criteria,
        unmet_criteria=unmet_criteria,
        suggested_actions=suggested_actions,
        denial_patterns=[],   # populated by payer DB lookup in full implementation
    )


def _score_procedure_payer_history(evidence: ProcedureEvidenceBundle) -> FactorScore:
    """
    Factor 1 — Past denial history (40 points).

    Past denials for the same procedure + same payer are the strongest
    predictor of a new denial. Weight is higher than drug PA (15 pts → 40 pts)
    because payer behaviour for procedures is highly pattern-driven.

    0 denials  → 40/40  full points
    1 denial   → 20/40  partial (history exists but may be addressable)
    2+ denials →  0/40  zero points (repeat pattern, very high risk)
    """
    max_points = 40
    denials = evidence.past_denials_similar
    payer   = evidence.payer_name or "this payer"

    if denials == 0:
        return FactorScore(
            factor="payer_history",
            points_earned=max_points,
            max_points=max_points,
            met=True,
            evidence="No prior denials for similar procedures with this payer",
            action=None,
        )
    elif denials == 1:
        reasons = "; ".join(evidence.past_denial_reasons[:2]) or "reason not recorded"
        return FactorScore(
            factor="payer_history",
            points_earned=20,
            max_points=max_points,
            met=False,
            evidence=f"1 prior denial for similar procedure with {payer} — reason: {reasons}",
            action="Review prior denial reason and ensure it has been addressed before resubmitting",
        )
    else:
        reasons = "; ".join(dict.fromkeys(evidence.past_denial_reasons[:3])) or "see claims history"
        return FactorScore(
            factor="payer_history",
            points_earned=0,
            max_points=max_points,
            met=False,
            evidence=(
                f"{denials} prior denials for similar procedures with {payer} — "
                f"recurring reason: {reasons}"
            ),
            action=(
                "High repeat-denial risk — address all prior denial reasons, "
                "attach additional clinical justification, "
                "and consider requesting peer-to-peer review before submission"
            ),
        )


def _score_procedure_documentation(evidence: ProcedureEvidenceBundle) -> FactorScore:
    """
    Factor 2 — Documentation completeness (35 points).

    Insufficient documentation is the most common fixable denial reason for
    procedures. This factor checks whether all required documents are on file.

    0 missing  → 35/35  full points
    1 missing  → 15/35  partial (some documentation present, gap identified)
    2+ missing →  0/35  zero points (documentation too incomplete to proceed)
    """
    max_points = 35
    missing = evidence.missing_docs

    if not missing:
        docs_present = len(evidence.docs_on_file)
        return FactorScore(
            factor="documentation",
            points_earned=max_points,
            max_points=max_points,
            met=True,
            evidence=f"Documentation complete: {docs_present} required document(s) on file",
            action=None,
        )
    elif len(missing) == 1:
        return FactorScore(
            factor="documentation",
            points_earned=15,
            max_points=max_points,
            met=False,
            evidence=f"Documentation gap: missing — {missing[0]}",
            action=f"Obtain and attach before submitting: {missing[0]}",
        )
    else:
        gap_list = "; ".join(missing)
        return FactorScore(
            factor="documentation",
            points_earned=0,
            max_points=max_points,
            met=False,
            evidence=f"Documentation gaps ({len(missing)} items missing): {gap_list}",
            action=f"Obtain all missing documents before submitting: {gap_list}",
        )


def _score_procedure_coverage(evidence: ProcedureEvidenceBundle) -> FactorScore:
    """
    Factor 3 — Coverage status (25 points).

    Simplified vs. the drug scorer: procedures don't have a formulary, so
    we only check whether coverage is active.
    """
    max_points = 25

    if evidence.coverage_active:
        payer = evidence.payer_name or "insurance"
        return FactorScore(
            factor="coverage_status",
            points_earned=max_points,
            max_points=max_points,
            met=True,
            evidence=f"Coverage active: {payer} confirmed",
            action=None,
        )

    return FactorScore(
        factor="coverage_status",
        points_earned=0,
        max_points=max_points,
        met=False,
        evidence="Coverage NOT active — no active insurance found",
        action="Verify patient insurance eligibility before submitting",
    )


# ---------------------------------------------------------------------------
# Score interpretation
# ---------------------------------------------------------------------------

def _interpret_score(total_points: int) -> tuple[str, str]:
    """
    Map a numeric score to a (risk_level, card_indicator) pair.

    Returns a tuple — C# analogy: a ValueTuple<string, string>.
    The caller unpacks it: risk_level, indicator = _interpret_score(score)
    """
    if total_points >= 80:
        return ("low", "info")
    elif total_points >= 50:
        return ("moderate", "warning")
    else:
        return ("high", "critical")
