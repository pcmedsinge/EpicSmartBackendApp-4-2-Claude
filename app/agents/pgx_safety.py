"""
PGx Safety Agent — linear pipeline for pharmacogenomic drug-gene checking.

Phase 4: linear pipeline (not agentic yet).
Phase 5: becomes a tool in the agentic orchestrator.

Pipeline:
  1. Extract drug info from hook context
  2. Fetch genomic Observations from FHIR (GET /Observation?category=genomics)
  3. Parse FHIR observations → {gene: diplotype} dict
  4. Fall back to synthetic overlay if FHIR returns nothing (and overlay is on)
  5. Run CPIC engine → PgxResult
  6. Return PgxResult

Entry point: run_pgx_pipeline(hook_request) → PgxResult
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.data.seed_synthetic import get_synthetic_pgx_data
from app.fhir.client import FhirClient
from app.models.cds_hooks import HookRequest
from app.rules.cpic_engine import PgxResult, check_pgx

logger = logging.getLogger(__name__)

# LOINC codes used in FHIR to encode gene names within genomic Observations.
# When an Observation has category=genomics, the specific gene being reported
# is usually in a component coded with one of these LOINC codes.
_GENE_LOINC_CODE = "48018-6"       # "Gene studied [ID]" — component code
_DIPLOTYPE_LOINC_CODE = "84413-4"  # "Genotype display name" — component code

# Mapping of gene names to their common FHIR Observation LOINC codes
# Used as a fallback when parsing the observation's primary code
_GENE_OBSERVATION_LOINCS: dict[str, str] = {
    "CYP2C19": "49583-7",
    "CYP2C9":  "49584-5",
    "CYP2D6":  "49590-2",
    "VKORC1":  "49586-0",
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_pgx_pipeline(
    hook_request: HookRequest,
    drug_name: str,
) -> PgxResult:
    """
    Run the full PGx safety pipeline for a given drug order.

    Args:
        hook_request:  The CDS Hooks request (used to extract patient ID).
        drug_name:     The drug being ordered (e.g. "clopidogrel").

    Returns:
        PgxResult — always returns a result, never raises.
        If something goes wrong, returns a "no data available" result
        rather than propagating the exception.

    C# analogy: async Task<PgxResult> RunPgxPipelineAsync(HookRequest, string)
    """
    settings = get_settings()
    patient_id = hook_request.context.get("patientId", "")

    logger.info("PGx pipeline started: patient=%s drug=%s", patient_id, drug_name)

    # Step 1: Attempt FHIR genomic data fetch
    genomic_data = await _fetch_genomic_data(patient_id)
    data_source = "fhir" if genomic_data else None

    # Step 2: Synthetic overlay fallback
    if not genomic_data and settings.use_synthetic_overlay and patient_id:
        genomic_data = _get_synthetic_genomic_data(patient_id, drug_name)
        if genomic_data:
            data_source = "synthetic"
            logger.info(
                "PGx synthetic overlay applied: patient=%s drug=%s genes=%s",
                patient_id, drug_name, list(genomic_data.keys()),
            )

    # Step 3: Run CPIC engine
    result = check_pgx(drug=drug_name, genomic_data=genomic_data)

    logger.info(
        "PGx pipeline complete: drug=%s severity=%s has_interaction=%s data_source=%s",
        drug_name, result.severity, result.has_interaction, data_source,
    )

    return result


# ---------------------------------------------------------------------------
# FHIR genomic data fetching and parsing
# ---------------------------------------------------------------------------

async def _fetch_genomic_data(patient_id: str) -> dict[str, str] | None:
    """
    Fetch and parse genomic Observations from Epic FHIR.

    Returns a dict mapping gene → diplotype (e.g. {"CYP2C19": "*2/*2"}),
    or None if no genomic observations were found.

    Epic sandbox typically has no genomic data — None is the expected result
    in most test runs. The synthetic overlay handles the demo scenario.
    """
    if not patient_id:
        return None

    try:
        async with FhirClient() as fhir:
            observations = await fhir.get_genomic_observations(patient_id)

        if not observations:
            return None

        # Parse each observation into a gene → diplotype entry
        # Dict comprehension with filtering — builds only non-None entries.
        # C# analogy: observations.Select(ParseObs).Where(x => x != null)
        #                          .ToDictionary(x => x.Gene, x => x.Diplotype)
        parsed: dict[str, str] = {}
        for obs in observations:
            gene, diplotype = _parse_genomic_observation(obs)
            if gene and diplotype:
                parsed[gene] = diplotype

        return parsed if parsed else None

    except Exception as exc:
        logger.warning("FHIR genomic fetch failed: %s", exc)
        return None


def _parse_genomic_observation(obs: dict) -> tuple[str | None, str | None]:
    """
    Extract (gene, diplotype) from a FHIR R4 genomic Observation.

    FHIR genomic Observations encode PGx results in a few different ways
    depending on the EHR vendor. We try the most common Epic patterns:

    Pattern 1 — components:
      obs.component[code=48018-6].valueString = "CYP2C19"   (gene name)
      obs.component[code=84413-4].valueString = "*2/*2"      (diplotype)

    Pattern 2 — direct valueString:
      obs.code.coding[0].display = "CYP2C19 gene diplotype"
      obs.valueString = "*2/*2"

    Returns (None, None) if neither pattern matches.
    C# analogy: a TryParse method returning (bool success, string gene, string diplotype).
    """
    gene: str | None = None
    diplotype: str | None = None

    # Pattern 1: look in components
    components = obs.get("component", [])
    for component in components:
        # Get the LOINC code for this component
        codings = component.get("code", {}).get("coding", [])
        loinc = next((c.get("code", "") for c in codings), "")

        if loinc == _GENE_LOINC_CODE:
            # This component holds the gene name
            gene = (
                component.get("valueString")
                or component.get("valueCodeableConcept", {}).get("text")
            )
        elif loinc == _DIPLOTYPE_LOINC_CODE:
            # This component holds the diplotype
            diplotype = (
                component.get("valueString")
                or component.get("valueCodeableConcept", {}).get("text")
            )

    if gene and diplotype:
        return (gene.upper(), diplotype)

    # Pattern 2: gene from code display, diplotype from valueString
    obs_codings = obs.get("code", {}).get("coding", [])
    for coding in obs_codings:
        display = coding.get("display", "").upper()
        # Check if display mentions a gene we care about
        for gene_name in _GENE_OBSERVATION_LOINCS:
            if gene_name in display:
                gene = gene_name
                diplotype = obs.get("valueString")
                if gene and diplotype:
                    return (gene, diplotype)

    return (None, None)


# ---------------------------------------------------------------------------
# Synthetic overlay helper
# ---------------------------------------------------------------------------

def _get_synthetic_genomic_data(patient_id: str, drug_name: str) -> dict[str, str] | None:
    """
    Extract the genomic dict from the synthetic scenario for this patient + drug.

    The synthetic scenario stores pgx_data as:
      {"CYP2C19": {"diplotype": "*2/*2", "metabolizer_status": "poor_metabolizer", ...}}

    We flatten this to:
      {"CYP2C19": "*2/*2"}

    That's the format check_pgx() expects.
    """
    scenario = get_synthetic_pgx_data(patient_id, drug_name)
    if not scenario:
        return None

    raw_pgx = scenario.get("pgx_data")
    if not raw_pgx:
        return None   # scenario exists but pgx_data=None → simulate "no data" path

    # Flatten: {gene: {diplotype: "...", ...}} → {gene: diplotype_string}
    # Dict comprehension iterating over gene → gene_data pairs.
    # C# analogy: rawPgx.ToDictionary(kv => kv.Key, kv => (string)kv.Value["diplotype"])
    return {
        gene: gene_data["diplotype"]
        for gene, gene_data in raw_pgx.items()
        if isinstance(gene_data, dict) and "diplotype" in gene_data
    }
