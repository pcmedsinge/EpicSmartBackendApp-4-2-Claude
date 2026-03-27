"""
Evidence chain configuration — maps drug class → ordered list of steps.

This is pure configuration: no logic, no imports from orchestrator.
The Orchestrator reads this dict to decide which steps to execute and in
what order, making the system's clinical decision flow auditable and
easy to extend.

C# analogy: a static readonly Dictionary<string, ChainConfig> — a
configuration object that drives a strategy pattern.

Adding a new drug class = add one entry here + implement the step
functions in orchestrator.py.  No other files change.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Chain definitions
# ---------------------------------------------------------------------------
# Each entry has:
#   name          Human-readable label (appears in chain_log + card)
#   steps         Ordered list of step names. The orchestrator dispatches
#                 each name to an execute_step() handler.
#   pgx_check     True = PGx safety engine must run; False = skip PGx.
#   note          Optional free-text design note (not used at runtime).
# ---------------------------------------------------------------------------

EVIDENCE_CHAINS: dict[str, dict] = {

    # ------------------------------------------------------------------
    # GLP-1 agonists (Ozempic, Wegovy, Mounjaro …)
    # Goal: produce a Prior Authorization bundle with denial-risk score.
    # ------------------------------------------------------------------
    "glp1": {
        "name": "GLP-1 Prior Authorization",
        "steps": [
            "fetch_labs",               # A1C, BMI from FHIR / synthetic
            "fetch_rx_history",         # Medication history (metformin days)
            "fetch_coverage",           # Insurance / payer details
            "check_step_therapy",       # Metformin >= 90 days?
            "check_clinical_criteria",  # A1C >= 7.0, BMI >= 30?
            "score_denial_risk",        # 5-factor weighted score
            "build_pa_bundle",          # Assemble PA documentation
            "generate_narrative",       # OpenAI clinical summary (or template)
        ],
        "pgx_check": False,
    },

    # ------------------------------------------------------------------
    # Oncology agents (Keytruda / pembrolizumab …)
    # Goal: validate NCCN pathway + produce oncology PA bundle.
    # ------------------------------------------------------------------
    "oncology": {
        "name": "Oncology Pathway Validation",
        "steps": [
            "fetch_condition",          # Tumor type from FHIR / synthetic
            "fetch_biomarkers",         # PD-L1 score, genomic markers
            "fetch_prior_regimens",     # Previous treatment history
            "validate_nccn_pathway",    # Tumor + biomarker + drug → approved?
            "build_pa_bundle",          # Complex oncology PA
            "generate_narrative",       # OpenAI clinical summary (or template)
        ],
        "pgx_check": False,
    },

    # ------------------------------------------------------------------
    # PGx-sensitive drugs (clopidogrel, warfarin, codeine …)
    # Goal: CPIC safety check — alert clinician if interaction found.
    #
    # IMPORTANT: generate_narrative is intentionally NOT in this chain.
    # PGx safety alerts are always composed from deterministic templates.
    # LLM-generated text must never be used for safety-critical guidance.
    # ------------------------------------------------------------------
    "pgx_sensitive": {
        "name": "Pharmacogenomic Safety Check",
        "steps": [
            "fetch_pgx_data",           # Genomic observations from FHIR / synthetic
            "check_cpic",               # CPIC rule lookup (deterministic)
            "suggest_alternative",      # Suggest prasugrel / ticagrelor if poor metabolizer
        ],
        "pgx_check": True,
        "note": (
            "PGx cards use deterministic templates only — no LLM for "
            "safety-critical content. narrative_source will always be 'template'."
        ),
    },

    # ------------------------------------------------------------------
    # Standard procedures and drugs without a dedicated pipeline
    # (MRI, X-ray, generic medications, …)
    # Goal: surface past denial patterns and documentation gaps.
    # ------------------------------------------------------------------
    "standard": {
        "name": "Denial Prevention",
        "steps": [
            "fetch_claims_history",     # Past ExplanationOfBenefit resources
            "pattern_match_denials",    # Similar past denials with same payer?
            "check_documentation",      # Required docs on file?
            "score_denial_risk",        # Weighted denial-risk score
            "generate_narrative",       # OpenAI risk summary (or template)
        ],
        "pgx_check": False,
    },
}


# ---------------------------------------------------------------------------
# Helper — used by orchestrator to look up a chain safely
# ---------------------------------------------------------------------------

def get_chain(drug_class: str) -> dict:
    """
    Return the evidence chain config for a drug class.

    Falls back to "standard" if the drug class is not in the registry.
    This ensures the orchestrator always has a valid chain to execute —
    no KeyError, no silent failure.

    C# analogy: dict.TryGetValue(key, out var chain) ?? _standardChain
    """
    return EVIDENCE_CHAINS.get(drug_class, EVIDENCE_CHAINS["standard"])
