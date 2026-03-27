"""
CPIC PGx safety engine — deterministic drug-gene interaction checker.

All logic is based on published CPIC guidelines (cpicpgx.org).
No AI, no LLM. Same input always produces the same output — required for
patient safety decisions.

Public API:
    result = check_pgx(drug="clopidogrel", genomic_data={"CYP2C19": "*2/*2"})

Three outcomes:
  1. Drug is not PGx-sensitive        → severity "none", no alert
  2. PGx data available, match found  → interaction result with recommendation
  3. PGx data not available           → "recommend testing" result

C# analogy: a static service class with a single public method and
            private helper methods — no state, pure computation.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.data.db import get_db


# ---------------------------------------------------------------------------
# Which genes to check for each drug
# Only drugs in this map get PGx evaluation — everything else returns "none".
# ---------------------------------------------------------------------------
_DRUG_GENE_MAP: dict[str, list[str]] = {
    "clopidogrel": ["CYP2C19"],
    "plavix":      ["CYP2C19"],    # brand name for clopidogrel
    "warfarin":    ["CYP2C9"],     # full check also needs VKORC1 (Phase 5+)
    "coumadin":    ["CYP2C9"],     # brand name for warfarin
    "codeine":     ["CYP2D6"],
    "tramadol":    ["CYP2D6"],
    "tamoxifen":   ["CYP2D6"],
    "simvastatin": ["SLCO1B1"],
}

# Drugs that need PGx testing recommended when no data is on file.
# Maps drug → gene for the "recommend testing" card message.
_TESTING_RECOMMENDATION: dict[str, str] = {
    "clopidogrel": "CYP2C19",
    "plavix":      "CYP2C19",
    "warfarin":    "CYP2C9",
    "coumadin":    "CYP2C9",
}


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

class PgxResult(BaseModel):
    """
    Result of a PGx drug-gene interaction check.

    has_interaction=True  → alert the clinician (severity high/moderate)
    has_interaction=False + pgx_data_available=False → recommend testing
    has_interaction=False + pgx_data_available=True  → all clear, no card needed
    has_interaction=False + severity="none"          → drug not PGx-sensitive
    """

    has_interaction: bool = Field(
        description="True if a clinically significant gene-drug interaction was found.",
    )
    gene: str | None = Field(
        default=None,
        description="The gene involved in the interaction (e.g. 'CYP2C19').",
    )
    metabolizer_status: str | None = Field(
        default=None,
        description="Patient's metabolizer phenotype: 'poor_metabolizer' | 'intermediate_metabolizer' | 'normal_metabolizer' | 'rapid_metabolizer' | 'ultrarapid_metabolizer'.",
    )
    diplotype: str | None = Field(
        default=None,
        description="The diplotype found in the patient's genomic data (e.g. '*2/*2').",
    )
    recommendation: str = Field(
        description="Clinical recommendation text from CPIC guidelines.",
    )
    alternative_drug: str | None = Field(
        default=None,
        description="Suggested alternative drug(s) if current drug should be avoided.",
    )
    severity: str = Field(
        description="'high' | 'moderate' | 'low' | 'none'",
    )
    evidence_level: str | None = Field(
        default=None,
        description="CPIC evidence grade: '1A' (strongest) | '1B' | '2A' | '2B'.",
    )
    pgx_data_available: bool = Field(
        default=True,
        description="False when no genomic data exists for this patient. Triggers 'recommend testing' card.",
    )
    drug_name: str = Field(
        default="",
        description="The drug that was checked.",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def check_pgx(
    drug: str,
    genomic_data: dict[str, str] | None,
) -> PgxResult:
    """
    Check for a PGx drug-gene interaction.

    Args:
        drug:          Drug name (case-insensitive). E.g. "clopidogrel", "Ozempic".
        genomic_data:  Dict mapping gene → diplotype. E.g. {"CYP2C19": "*2/*2"}.
                       None means no genomic data is on file for this patient.

    Returns:
        PgxResult — see docstring for the three outcome types.

    C# analogy: a pure static method returning a discriminated union result.
    """
    # Normalize drug name: lowercase, strip whitespace
    # Then resolve to a canonical key — FHIR display strings like
    # "clopidogrel (Plavix) 75mg tablet" must map to "clopidogrel".
    drug_lower = drug.strip().lower()
    drug_lower = _resolve_drug_name(drug_lower)

    # --- Path 1: Drug is not PGx-sensitive ---
    relevant_genes = _DRUG_GENE_MAP.get(drug_lower)
    if relevant_genes is None:
        return PgxResult(
            has_interaction=False,
            recommendation=f"No PGx checking required for {drug}.",
            severity="none",
            pgx_data_available=False,
            drug_name=drug,
        )

    # Normalize genomic data keys to uppercase (genes are always uppercase: CYP2C19)
    # Dict comprehension with .upper() — C# analogy: dict.ToDictionary(k => k.Key.ToUpper(), v => v.Value)
    normalized_genomic: dict[str, str] | None = (
        {gene.upper(): diplotype for gene, diplotype in genomic_data.items()}
        if genomic_data
        else None
    )

    # --- Path 2: No genomic data on file ---
    if not normalized_genomic:
        return _no_pgx_data_result(drug, drug_lower, relevant_genes[0])

    # --- Path 3: Genomic data available — check each relevant gene ---
    # For most drugs there's one relevant gene; warfarin eventually needs two.
    # We return the highest-severity result found.
    results: list[PgxResult] = []

    for gene in relevant_genes:
        diplotype = normalized_genomic.get(gene)
        if diplotype is None:
            # Genomic data exists but this specific gene wasn't tested
            results.append(_no_pgx_data_result(drug, drug_lower, gene))
            continue

        match = _query_cpic_rule(drug_lower, gene, diplotype)
        if match:
            results.append(_build_result_from_row(match, drug, diplotype))
        else:
            # Gene tested, diplotype not in our KB — treat as "no interaction found"
            results.append(PgxResult(
                has_interaction=False,
                gene=gene,
                diplotype=diplotype,
                recommendation=(
                    f"CYP2C19 diplotype {diplotype} not found in CPIC knowledge base. "
                    f"Use clinical judgment. Consider specialist consultation."
                ),
                severity="low",
                pgx_data_available=True,
                drug_name=drug,
            ))

    # Return the highest-severity result
    # _severity_rank assigns numeric priority; max() finds the highest
    return max(results, key=lambda r: _severity_rank(r.severity))


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _query_cpic_rule(drug: str, gene: str, diplotype: str) -> dict | None:
    """
    Look up a CPIC rule by drug + gene + diplotype.

    Tries both orderings of the diplotype (e.g. *1/*2 and *2/*1) because
    genomic reports don't always follow a consistent allele ordering convention.

    Returns the raw sqlite3.Row as a dict, or None if not found.
    """
    # Generate both orderings of the diplotype
    # alleles[0], alleles[1] = "*2", "*2" or "*1", "*2"
    alleles = diplotype.split("/")

    # Candidate patterns to try: original order and reversed order
    # set() deduplicates — for *2/*2, both orderings are identical
    candidates: list[str] = list({diplotype})
    if len(alleles) == 2 and alleles[0] != alleles[1]:
        candidates.append(f"{alleles[1]}/{alleles[0]}")

    conn = get_db()
    try:
        for pattern in candidates:
            row = conn.execute(
                """
                SELECT drug_name, gene, diplotype_pattern, metabolizer_status,
                       recommendation, alternative_drug, severity, evidence_level
                FROM   cpic_rules
                WHERE  drug_name = ? AND gene = ? AND diplotype_pattern = ?
                """,
                (drug, gene, pattern),
            ).fetchone()
            if row:
                # Convert sqlite3.Row to a plain dict so we can return it cleanly
                # dict(row) works because sqlite3.Row supports iteration over (key, value) pairs
                return dict(row)
    finally:
        conn.close()

    return None


def _build_result_from_row(row: dict, drug: str, diplotype: str) -> PgxResult:
    """Build a PgxResult from a cpic_rules database row."""
    # Determine has_interaction: any severity above "low" is a meaningful interaction
    severity = row["severity"]
    has_interaction = severity in ("high", "moderate")

    return PgxResult(
        has_interaction=has_interaction,
        gene=row["gene"],
        metabolizer_status=row["metabolizer_status"],
        diplotype=diplotype,
        recommendation=row["recommendation"],
        alternative_drug=row["alternative_drug"],
        severity=severity,
        evidence_level=row["evidence_level"],
        pgx_data_available=True,
        drug_name=drug,
    )


def _no_pgx_data_result(drug: str, drug_lower: str, gene: str) -> PgxResult:
    """
    Build a PgxResult for when no genomic data is on file.

    This is the most common real-world case — most patients haven't had
    PGx testing. The result triggers a "recommend testing" card in the composer.
    """
    gene_context = _gene_context_message(drug_lower, gene)

    return PgxResult(
        has_interaction=False,
        gene=gene,
        metabolizer_status=None,
        diplotype=None,
        recommendation=(
            f"No {gene} genomic data on file for this patient. "
            f"{gene_context} "
            f"Consider ordering a {gene} PGx panel before initiating {drug} therapy."
        ),
        alternative_drug=None,
        severity="low",
        evidence_level=None,
        pgx_data_available=False,
        drug_name=drug,
    )


def _gene_context_message(drug: str, gene: str) -> str:
    """
    Return a one-sentence clinical context explaining why this gene matters
    for this drug. Used in the "recommend testing" recommendation text.
    """
    contexts: dict[tuple[str, str], str] = {
        ("clopidogrel", "CYP2C19"): (
            "CYP2C19 status directly determines whether clopidogrel can be activated — "
            "poor metabolizers (~30% of patients) receive no antiplatelet benefit."
        ),
        ("plavix", "CYP2C19"): (
            "CYP2C19 status directly determines whether clopidogrel can be activated — "
            "poor metabolizers (~30% of patients) receive no antiplatelet benefit."
        ),
        ("warfarin", "CYP2C9"): (
            "CYP2C9 variants reduce warfarin metabolism — poor metabolizers "
            "are at significantly increased bleeding risk at standard doses."
        ),
        ("coumadin", "CYP2C9"): (
            "CYP2C9 variants reduce warfarin metabolism — poor metabolizers "
            "are at significantly increased bleeding risk at standard doses."
        ),
    }
    # .get() with a default fallback — C# analogy: dict.TryGetValue() with ?? fallback
    return contexts.get(
        (drug, gene),
        f"{gene} variants can significantly affect {drug} metabolism and efficacy.",
    )


# Brand name → generic name map for DB lookups.
# The cpic_rules table stores rules under the generic name only.
# Brand names are valid keys in _DRUG_GENE_MAP (for gene routing) but must be
# mapped to their generic before querying the DB.
_BRAND_TO_GENERIC: dict[str, str] = {
    "plavix":   "clopidogrel",
    "coumadin": "warfarin",
}


def _resolve_drug_name(drug_lower: str) -> str:
    """
    Resolve a full FHIR display string or brand name to the canonical generic
    drug name used as the primary key in the cpic_rules table.

    FHIR MedicationRequest.medicationCodeableConcept.text often contains the
    full clinical drug name, e.g. "clopidogrel (Plavix) 75mg tablet".
    We need just "clopidogrel" for DB lookups.

    Resolution order:
      1. Brand → generic map ("plavix" → "clopidogrel")
      2. Exact match against _DRUG_GENE_MAP keys
      3. Substring scan (handles full FHIR display strings)

    C# analogy: a normalizer method — returns the input unchanged if already canonical.
    """
    # 1. Brand name → generic name (DB rows use generic names only)
    if drug_lower in _BRAND_TO_GENERIC:
        return _BRAND_TO_GENERIC[drug_lower]

    # 2. Already a canonical generic key — nothing to do
    if drug_lower in _DRUG_GENE_MAP:
        return drug_lower

    # 3. Substring scan: find the first known key that appears as a word in the string.
    # Sorted longest-first so "clopidogrel" matches before "plavix" in mixed strings.
    for known_drug in sorted(_DRUG_GENE_MAP.keys(), key=len, reverse=True):
        # Resolve brand names found via substring to their generic form
        generic = _BRAND_TO_GENERIC.get(known_drug, known_drug)
        if known_drug in drug_lower:
            return generic

    return drug_lower   # no match — return as-is, Path 1 will return "none"


def _severity_rank(severity: str) -> int:
    """
    Map severity string → integer for comparison.
    Higher number = higher priority. Used to select the worst result when
    multiple genes are checked.

    C# analogy: a switch expression returning an int.
    """
    return {"high": 3, "moderate": 2, "low": 1, "none": 0}.get(severity, 0)
