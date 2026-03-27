"""
Drug classifier — maps medication names and RxNorm codes to drug classes.

Phase 3: Only 'glp1' classification is active in the pipeline.
Phase 4+ will activate 'oncology' and 'pgx_sensitive'.

Drug classes:
  glp1          — GLP-1 receptor agonists (Ozempic, Wegovy, Mounjaro, etc.)
  oncology      — Checkpoint inhibitors and targeted therapies (Keytruda, Opdivo)
  pgx_sensitive — Drugs with pharmacogenomic interactions (Clopidogrel, Warfarin)
  standard      — Everything else (default denial prevention path)
"""

from __future__ import annotations  # allows lowercase type hints like list[str] in Python 3.9

import re

# ---------------------------------------------------------------------------
# Name-based lookup
# Keys are lowercase — normalize input before lookup.
# Multiple brand names and generic names map to the same class.
# ---------------------------------------------------------------------------
_NAME_TO_CLASS: dict[str, str] = {
    # GLP-1 receptor agonists
    "ozempic": "glp1",
    "wegovy": "glp1",
    "mounjaro": "glp1",
    "trulicity": "glp1",
    "victoza": "glp1",
    "saxenda": "glp1",
    "rybelsus": "glp1",
    "semaglutide": "glp1",       # generic name for Ozempic/Wegovy/Rybelsus
    "tirzepatide": "glp1",       # generic name for Mounjaro
    "dulaglutide": "glp1",       # generic name for Trulicity
    "liraglutide": "glp1",       # generic name for Victoza/Saxenda
    "exenatide": "glp1",         # Byetta, Bydureon
    "byetta": "glp1",
    "bydureon": "glp1",

    # Oncology — checkpoint inhibitors (Phase 4+)
    "keytruda": "oncology",
    "pembrolizumab": "oncology",  # generic name for Keytruda
    "opdivo": "oncology",
    "nivolumab": "oncology",      # generic name for Opdivo
    "tecentriq": "oncology",
    "atezolizumab": "oncology",   # generic name for Tecentriq
    "imfinzi": "oncology",
    "durvalumab": "oncology",

    # PGx-sensitive drugs (Phase 4+)
    "plavix": "pgx_sensitive",
    "clopidogrel": "pgx_sensitive",
    "coumadin": "pgx_sensitive",
    "warfarin": "pgx_sensitive",
    "codeine": "pgx_sensitive",
    "tramadol": "pgx_sensitive",
    "tamoxifen": "pgx_sensitive",
    "simvastatin": "pgx_sensitive",
}

# ---------------------------------------------------------------------------
# RxNorm code-based lookup
# RxNorm is the standard drug coding system used in FHIR MedicationRequest.
# These are the "ingredient" concept codes (not brand/dose specific).
# ---------------------------------------------------------------------------
_RXNORM_TO_CLASS: dict[str, str] = {
    # GLP-1 receptor agonists — ingredient codes + common clinical drug codes
    "2200786": "glp1",   # semaglutide (ingredient)
    "2200750": "glp1",   # semaglutide 0.5 MG/DOSE subcutaneous injection (Ozempic)
    "2200751": "glp1",   # semaglutide 1 MG/DOSE subcutaneous injection (Ozempic)
    "2200781": "glp1",   # semaglutide 2.4 MG/DOSE subcutaneous injection (Wegovy)
    "475968":  "glp1",   # exenatide (ingredient)
    "475969":  "glp1",   # liraglutide (ingredient)
    "2200644": "glp1",   # tirzepatide (ingredient)
    "1991302": "glp1",   # dulaglutide (ingredient)

    # Oncology
    "1547545": "oncology",  # pembrolizumab
    "1657973": "oncology",  # nivolumab

    # PGx-sensitive — ingredient codes + common clinical drug codes
    "32968":  "pgx_sensitive",   # clopidogrel (ingredient)
    "309362": "pgx_sensitive",   # clopidogrel 75 MG oral tablet (Plavix)
    "11289":  "pgx_sensitive",   # warfarin (ingredient)
    "855295": "pgx_sensitive",   # warfarin 5 MG oral tablet (Coumadin)
    "41493":  "pgx_sensitive",   # codeine
    "41267":  "pgx_sensitive",   # tramadol
    "30125":  "pgx_sensitive",   # tamoxifen
    "36567":  "pgx_sensitive",   # simvastatin
}

# The set of drug classes recognized by this system.
# C# analogy: an enum — but Python strings are simpler here since we don't
# need type-level enforcement in the scoring pipeline.
DRUG_CLASSES = frozenset({"glp1", "oncology", "pgx_sensitive", "standard"})


def classify_drug(
    drug_name: str | None = None,
    rxnorm_code: str | None = None,
) -> str:
    """
    Return the drug class for a given name or RxNorm code.

    Lookup priority:
      1. RxNorm code (most precise — preferred when available)
      2. Drug name (case-insensitive, strips whitespace)
      3. 'standard' fallback (safe default — routes to denial prevention path)

    Args:
        drug_name:    Brand or generic drug name (e.g. "Ozempic", "semaglutide").
        rxnorm_code:  RxNorm concept code string (e.g. "2200786").

    Returns:
        A drug class string: 'glp1' | 'oncology' | 'pgx_sensitive' | 'standard'

    C# analogy: a switch expression with a default: case returning an enum value.
    """
    # Try RxNorm code first — it's unambiguous
    if rxnorm_code:
        result = _RXNORM_TO_CLASS.get(rxnorm_code.strip())
        if result:
            return result

    # Try drug name — two passes:
    #   Pass 1: exact match (fastest, handles clean short names like "Ozempic")
    #   Pass 2: word-boundary substring match for full FHIR display strings like
    #           "Ozempic (semaglutide) 0.5mg injection" or "clopidogrel 75 MG tablet"
    # C# analogy: first TryGetValue(), then .Any(k => normalized.Contains(k))
    if drug_name:
        normalized = drug_name.strip().lower()
        result = _NAME_TO_CLASS.get(normalized)
        if result:
            return result
        # Substring pass — check if any known key appears as a word in the string
        for known_name, drug_class in _NAME_TO_CLASS.items():
            # Word-boundary check: known name must not be immediately preceded or
            # followed by another letter. Prevents "codeine" matching "hydrocodeine".
            # C# analogy: Regex.IsMatch(normalized, @"(?<![a-z])clopidogrel(?![a-z])")
            if re.search(r"(?<![a-z])" + re.escape(known_name) + r"(?![a-z])", normalized):
                return drug_class

    # Nothing matched — default to standard denial prevention pipeline
    return "standard"


def is_glp1(drug_name: str | None = None, rxnorm_code: str | None = None) -> bool:
    """Convenience check — True if the drug is a GLP-1 agonist."""
    return classify_drug(drug_name=drug_name, rxnorm_code=rxnorm_code) == "glp1"
