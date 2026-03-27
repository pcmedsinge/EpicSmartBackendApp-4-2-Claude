"""
NCCN Pathway Validator — simplified lookup table for oncology drug approval.

This is NOT a full NCCN database implementation. We seed the 2-3 most
clinically relevant pathways for the demo scenarios (pembrolizumab + NSCLC).
Adding a new pathway = adding one entry to _NCCN_PATHWAYS below.

NCCN = National Comprehensive Cancer Network. Their guidelines define the
standard-of-care drug + indication combinations that payers recognise as
medically necessary. "Category 1" = highest evidence level (uniform consensus
+ high-quality evidence).

Public API:
    result = validate_nccn_pathway(
        drug="pembrolizumab",
        tumor_type="NSCLC",
        pd_l1_score=80,
        prior_regimens=["carboplatin", "pemetrexed"],
    )

Three outcomes:
  pathway_approved=True  → payer should approve; PA bundle ready to submit
  pathway_approved=False → pathway criteria not met; card surfaces the gaps
  drug not in registry   → NccnResult with approved=False, no_data=True

C# analogy: a static service with a private readonly lookup table and a
single public method returning a discriminated union result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Internal pathway definition — used to configure _NCCN_PATHWAYS below.
# @dataclass keeps this as a plain Python struct (not Pydantic) — it is
# internal-only and never serialised to JSON.
# C# analogy: a private readonly record struct.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _NccnPathway:
    """A single NCCN-validated drug + indication combination."""

    drug: str                            # generic name, lowercase
    tumor_types: frozenset[str]          # normalised tumor labels to match against
    indication: str                      # human-readable indication line
    pd_l1_min_pct: int                   # minimum PD-L1 positivity (%)
    prior_regimen_required: bool         # True = patient must have prior chemo
    prior_regimen_keywords: frozenset[str]  # at least one must appear in prior_regimens list
    evidence_level: str                  # "Category 1" | "Category 2A"
    recommendation: str                  # full clinical recommendation text


# ---------------------------------------------------------------------------
# NCCN pathway registry
# Each entry encodes one validated drug + tumor type + biomarker combination.
# Sources: NCCN NSCLC Guidelines v1.2024 (public summary).
# ---------------------------------------------------------------------------

_NCCN_PATHWAYS: list[_NccnPathway] = [

    # Pembrolizumab — NSCLC first-line monotherapy
    # Requirement: PD-L1 ≥ 50%, no prior systemic therapy for metastatic disease.
    # Evidence: KEYNOTE-024 RCT → Category 1.
    _NccnPathway(
        drug="pembrolizumab",
        tumor_types=frozenset({
            "nsclc", "non-small cell lung cancer",
            "c34.1", "c34.10", "c34.11", "c34.12",
        }),
        indication="First-line monotherapy",
        pd_l1_min_pct=50,
        prior_regimen_required=False,
        prior_regimen_keywords=frozenset(),
        evidence_level="Category 1",
        recommendation=(
            "NCCN Category 1: Pembrolizumab monotherapy is recommended as first-line "
            "treatment for metastatic NSCLC with PD-L1 TPS ≥50% and no EGFR/ALK alterations. "
            "(KEYNOTE-024)"
        ),
    ),

    # Pembrolizumab — NSCLC second-line after platinum
    # Requirement: PD-L1 ≥ 1%, prior platinum-based chemotherapy.
    # Evidence: KEYNOTE-010 RCT → Category 1.
    _NccnPathway(
        drug="pembrolizumab",
        tumor_types=frozenset({
            "nsclc", "non-small cell lung cancer",
            "c34.1", "c34.10", "c34.11", "c34.12",
        }),
        indication="Second-line after platinum chemotherapy",
        pd_l1_min_pct=1,
        prior_regimen_required=True,
        prior_regimen_keywords=frozenset({"carboplatin", "cisplatin", "oxaliplatin"}),
        evidence_level="Category 1",
        recommendation=(
            "NCCN Category 1: Pembrolizumab is recommended as second-line therapy for "
            "metastatic NSCLC with PD-L1 TPS ≥1% after prior platinum-containing regimen. "
            "(KEYNOTE-010)"
        ),
    ),

    # Nivolumab — NSCLC second-line (any PD-L1)
    # Evidence: CheckMate 017/057 → Category 1.
    _NccnPathway(
        drug="nivolumab",
        tumor_types=frozenset({
            "nsclc", "non-small cell lung cancer",
            "c34.1", "c34.10", "c34.11", "c34.12",
        }),
        indication="Second-line after platinum chemotherapy",
        pd_l1_min_pct=0,
        prior_regimen_required=True,
        prior_regimen_keywords=frozenset({"carboplatin", "cisplatin", "oxaliplatin"}),
        evidence_level="Category 1",
        recommendation=(
            "NCCN Category 1: Nivolumab is recommended for metastatic NSCLC regardless "
            "of PD-L1 expression after prior platinum-based therapy. (CheckMate 017/057)"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

class NccnResult(BaseModel):
    """
    Result of an NCCN pathway validation check.

    pathway_approved=True  → evidence meets NCCN criteria; PA ready to submit.
    pathway_approved=False → criteria not met; gaps listed in `gaps` field.
    drug_not_in_registry   → True when we have no pathways for this drug at all.

    C# analogy: a discriminated union result type —
      ApprovedResult | NotMetResult | UnknownDrugResult
    """

    drug: str = Field(description="Drug name checked (lowercase generic).")
    tumor_type: str = Field(description="Tumor type as provided.")
    indication: str = Field(default="", description="Matched NCCN indication line.")

    pathway_approved: bool = Field(
        description="True when all NCCN criteria are satisfied.",
    )
    drug_not_in_registry: bool = Field(
        default=False,
        description="True when no NCCN pathways exist for this drug.",
    )

    pd_l1_required: int = Field(
        default=0,
        description="Minimum PD-L1 score required by matched pathway (%).",
    )
    pd_l1_found: int | None = Field(
        default=None,
        description="PD-L1 score found for this patient (%).",
    )
    pd_l1_met: bool = Field(
        default=False,
        description="True when pd_l1_found >= pd_l1_required.",
    )

    prior_regimen_required: bool = Field(
        default=False,
        description="True when pathway requires documented prior chemotherapy.",
    )
    prior_regimen_met: bool = Field(
        default=False,
        description="True when a qualifying prior regimen was found.",
    )

    evidence_level: str = Field(
        default="",
        description="NCCN evidence category for the matched pathway.",
    )
    recommendation: str = Field(
        default="",
        description="Full NCCN recommendation text from the matched pathway.",
    )

    gaps: list[str] = Field(
        default_factory=list,
        description="Unmet criteria descriptions (empty when pathway_approved=True).",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate_nccn_pathway(
    drug: str,
    tumor_type: str,
    pd_l1_score: int | None,
    prior_regimens: list[str],
) -> NccnResult:
    """
    Validate whether a drug + indication combination meets NCCN criteria.

    Args:
        drug:            Generic or brand drug name (case-insensitive).
        tumor_type:      Tumor type string from the patient record
                         (e.g. "NSCLC", "C34.10", "Non-small cell lung cancer").
        pd_l1_score:     PD-L1 TPS score as an integer percentage (0-100),
                         or None if not tested.
        prior_regimens:  List of prior chemotherapy agent names (lowercase OK).
                         Empty list = no prior systemic therapy.

    Returns:
        NccnResult — see docstring for the three outcome types.

    C# analogy: a pure static method returning a discriminated union result.
    """
    drug_lower   = drug.strip().lower()
    # Resolve brand names to generic — substring match handles full display strings
    # e.g. "Keytruda (pembrolizumab) 200mg IV every 3 weeks" contains "keytruda"
    drug_generic = drug_lower
    for brand, generic in _BRAND_TO_GENERIC.items():
        if brand in drug_lower:
            drug_generic = generic
            break
    tumor_lower  = tumor_type.strip().lower()
    regimens_lower = [r.strip().lower() for r in prior_regimens]

    # Find all pathways for this drug
    drug_pathways = [
        p for p in _NCCN_PATHWAYS
        if p.drug == drug_generic and tumor_lower in p.tumor_types
    ]

    if not drug_pathways:
        return NccnResult(
            drug=drug_generic,
            tumor_type=tumor_type,
            pathway_approved=False,
            drug_not_in_registry=True,
            recommendation=(
                f"No NCCN pathways registered for {drug} + {tumor_type}. "
                f"Manual review required."
            ),
        )

    # Try each pathway in order of descending PD-L1 requirement
    # (most specific pathway first — first-line before second-line).
    # C# analogy: pathways.OrderByDescending(p => p.PdL1MinPct)
    for pathway in sorted(drug_pathways, key=lambda p: p.pd_l1_min_pct, reverse=True):
        result = _evaluate_pathway(pathway, pd_l1_score, regimens_lower)
        if result.pathway_approved:
            return result   # first approved pathway wins

    # No pathway was fully approved — return the last (least restrictive) result
    # with all gaps captured, so the clinician knows what's missing.
    return _evaluate_pathway(
        sorted(drug_pathways, key=lambda p: p.pd_l1_min_pct)[0],  # least restrictive
        pd_l1_score,
        regimens_lower,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _evaluate_pathway(
    pathway: _NccnPathway,
    pd_l1_score: int | None,
    regimens_lower: list[str],
) -> NccnResult:
    """Evaluate a single pathway against the patient's data."""
    gaps: list[str] = []

    # PD-L1 check
    pd_l1_met = (pd_l1_score is not None) and (pd_l1_score >= pathway.pd_l1_min_pct)
    if not pd_l1_met:
        if pd_l1_score is None:
            gaps.append(
                f"PD-L1 test result not on file "
                f"(pathway requires ≥{pathway.pd_l1_min_pct}%)"
            )
        else:
            gaps.append(
                f"PD-L1 score {pd_l1_score}% is below the required "
                f"≥{pathway.pd_l1_min_pct}% for {pathway.indication}"
            )

    # Prior regimen check
    prior_regimen_met = True
    if pathway.prior_regimen_required:
        # At least one of the required platinum agents must appear in the list.
        # any() returns True if at least one element passes the test.
        # C# analogy: regimensLower.Any(r => pathway.PriorRegimenKeywords.Contains(r))
        prior_regimen_met = any(
            keyword in regimens_lower
            for keyword in pathway.prior_regimen_keywords
        )
        if not prior_regimen_met:
            needed = " / ".join(sorted(pathway.prior_regimen_keywords))
            gaps.append(
                f"Pathway '{pathway.indication}' requires documented prior "
                f"platinum-based chemotherapy ({needed})"
            )

    pathway_approved = len(gaps) == 0

    return NccnResult(
        drug=pathway.drug,
        tumor_type=pathway.tumor_types and next(iter(pathway.tumor_types), ""),
        indication=pathway.indication,
        pathway_approved=pathway_approved,
        pd_l1_required=pathway.pd_l1_min_pct,
        pd_l1_found=pd_l1_score,
        pd_l1_met=pd_l1_met,
        prior_regimen_required=pathway.prior_regimen_required,
        prior_regimen_met=prior_regimen_met,
        evidence_level=pathway.evidence_level,
        recommendation=pathway.recommendation,
        gaps=gaps,
    )


# Brand → generic name map (same pattern as cpic_engine.py)
_BRAND_TO_GENERIC: dict[str, str] = {
    "keytruda": "pembrolizumab",
    "opdivo":   "nivolumab",
    "tecentriq": "atezolizumab",
    "imfinzi":  "durvalumab",
}
