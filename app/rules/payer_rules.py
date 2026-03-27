"""
Payer rules engine — queries SQLite and returns typed PA requirements.

Phase 3: GLP-1 + UnitedHealthcare only.
Phase 4+: Add oncology and PGx-sensitive drug classes.

The denial scorer (denial_scorer.py) reads from these typed models directly —
no raw SQL rows pass beyond this module.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.data.db import get_db


# ---------------------------------------------------------------------------
# Pydantic models — typed representations of what comes back from SQLite
#
# BaseModel is Pydantic's base class. It gives us:
#   - Automatic type coercion (e.g. "7.0" string → float)
#   - .model_dump() to convert to dict
#   - Validation on construction
# C# analogy: a record class or DTO with DataAnnotations.
# ---------------------------------------------------------------------------

class GLP1Requirements(BaseModel):
    """
    All PA criteria UHC applies to GLP-1 receptor agonist requests.
    Fields map 1-to-1 to rule_key values in the payer_rules table.
    """

    # Step therapy
    min_metformin_days: int = Field(
        default=90,
        description="Minimum days of metformin trial required before GLP-1 PA is approved.",
    )
    metformin_contraindication_accepted: bool = Field(
        default=True,
        description="Whether documented metformin contraindication waives step therapy.",
    )

    # Clinical criteria
    min_a1c: float = Field(
        default=7.0,
        description="Minimum HbA1c (%) required. Must be measured within the last 6 months.",
    )
    min_bmi_standard: float = Field(
        default=30.0,
        description="Minimum BMI for standard obesity indication.",
    )
    min_bmi_with_comorbidity: float = Field(
        default=27.0,
        description="Minimum BMI if patient has a weight-related comorbidity (T2D, HTN, etc.).",
    )
    diagnosis_required: str = Field(
        default="T2D",
        description="Active diagnosis required (e.g. 'T2D' = Type 2 Diabetes, ICD-10 E11.x).",
    )

    # Documentation requirements (in months)
    required_doc_a1c_months: int = Field(
        default=6,
        description="HbA1c lab must be on file and dated within this many months.",
    )
    required_doc_bmi_months: int = Field(
        default=12,
        description="BMI measurement must be on file and dated within this many months.",
    )
    required_doc_rx_history: bool = Field(
        default=True,
        description="Medication history documenting metformin trial must be on file.",
    )

    # Baseline statistics
    denial_rate_baseline: float = Field(
        default=0.22,
        description="Historical denial rate for this payer + drug class (0.0–1.0).",
    )
    avg_processing_days: float = Field(
        default=3.5,
        description="Average PA processing time in business days.",
    )


class DenialPattern(BaseModel):
    """A known denial reason for a payer + drug class combination."""
    denial_reason: str
    frequency: float    # fraction of all denials — e.g. 0.45 means 45% of denials cite this
    recommendation: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_payer_requirements(drug_class: str, payer_name: str) -> GLP1Requirements | None:
    """
    Return typed PA requirements for the given drug class + payer.

    Queries the payer_rules table and maps rule_key → field name on GLP1Requirements.
    Returns None if no rules exist for this combination.

    Args:
        drug_class:  e.g. "glp1"
        payer_name:  e.g. "UnitedHealthcare"

    C# analogy: a repository method returning a typed DTO, or null if not found.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT rule_key, rule_value
            FROM   payer_rules
            WHERE  payer_name = ? AND drug_class = ?
            """,
            (payer_name, drug_class),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    # Build a plain dict from the SQL rows: {rule_key: rule_value}
    # dict comprehension — C# analogy: rows.ToDictionary(r => r.rule_key, r => r.rule_value)
    rule_map: dict[str, str] = {row["rule_key"]: row["rule_value"] for row in rows}

    # We only have GLP-1 requirements in Phase 3.
    # The GLP1Requirements model coerces string values from SQLite to their
    # correct Python types (int, float, bool) automatically via Pydantic.
    if drug_class != "glp1":
        # Phase 4+ will add oncology and pgx_sensitive branches here.
        return None

    return _build_glp1_requirements(rule_map)


def get_denial_patterns(drug_class: str, payer_name: str) -> list[DenialPattern]:
    """
    Return known denial reasons for this payer + drug class, sorted by frequency (highest first).

    Returns an empty list if no patterns are found — callers treat this as
    "no historical denial data available".
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT denial_reason, frequency, recommendation
            FROM   denial_patterns
            WHERE  payer_name = ? AND drug_class = ?
            ORDER  BY frequency DESC
            """,
            (payer_name, drug_class),
        ).fetchall()
    finally:
        conn.close()

    # List comprehension — builds a list by iterating rows.
    # C# analogy: rows.Select(r => new DenialPattern(...)).ToList()
    return [
        DenialPattern(
            denial_reason=row["denial_reason"],
            frequency=row["frequency"],
            recommendation=row["recommendation"],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _build_glp1_requirements(rule_map: dict[str, str]) -> GLP1Requirements:
    """
    Map rule_key → rule_value strings from SQLite into a typed GLP1Requirements model.

    Uses .get() with fallback to the model's default values — if a rule_key
    is missing from the DB, the model's Field(default=...) takes over.
    This makes the function resilient to partial data without crashing.

    C# analogy: mapping a Dictionary<string, string> into a typed record,
    with null-coalescing for missing keys.
    """
    return GLP1Requirements(
        # int() and float() cast the string values stored in SQLite.
        # Pydantic also does this automatically, but being explicit here
        # makes the mapping obvious to future readers.
        min_metformin_days=int(rule_map.get("min_metformin_days", "90")),
        metformin_contraindication_accepted=(
            rule_map.get("metformin_contraindication_accepted", "true").lower() == "true"
        ),
        min_a1c=float(rule_map.get("min_a1c", "7.0")),
        min_bmi_standard=float(rule_map.get("min_bmi_standard", "30.0")),
        min_bmi_with_comorbidity=float(rule_map.get("min_bmi_with_comorbidity", "27.0")),
        diagnosis_required=rule_map.get("diagnosis_required", "T2D"),
        required_doc_a1c_months=int(rule_map.get("required_doc_a1c_months", "6")),
        required_doc_bmi_months=int(rule_map.get("required_doc_bmi_months", "12")),
        required_doc_rx_history=(
            rule_map.get("required_doc_rx_history", "true").lower() == "true"
        ),
        denial_rate_baseline=float(rule_map.get("denial_rate_baseline", "0.22")),
        avg_processing_days=float(rule_map.get("avg_processing_days", "3.5")),
    )
