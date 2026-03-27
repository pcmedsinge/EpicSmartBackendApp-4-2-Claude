"""
Synthetic data overlay for demo scenarios.

These constants supplement real Epic FHIR sandbox data when it is missing or
incomplete. The bridge (denial_prediction.py) checks FHIR first — this file
only fills the gaps.

Toggle via USE_SYNTHETIC_OVERLAY in .env:
  true  → bridge uses FHIR data where available, synthetic for the rest
  false → bridge uses FHIR data only (gaps become unmet criteria)

Why synthetic data? Epic's sandbox doesn't have clinical data perfectly
aligned with our demo scenarios. This overlay lets us demo the full
clinical-financial intelligence pipeline with realistic data.

All data is fictional. Patient IDs match Epic sandbox synthetic patients.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Imported only for type-checking — avoids a circular runtime import
    # between the data layer and the FHIR client layer.
    from app.models.fhir_bundle import FhirDataBundle

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scenario A — GLP-1 / Ozempic
# Patient: Derrick Lin (Epic sandbox synthetic patient)
# What it demonstrates: full GLP-1 PA with all criteria met → high approval
# ---------------------------------------------------------------------------
SCENARIO_A: dict = {
    "patient_id": "erXuFYUfucBZaryVksYEcMg3",
    "drug_name": "Ozempic",
    "drug_class": "glp1",

    # Step therapy
    "metformin_days": 180,              # 6 months — well above the 90-day requirement
    "metformin_contraindicated": False,

    # Clinical criteria
    "a1c_value": 7.5,                   # above 7.0% threshold
    "a1c_days_old": 45,                 # 45 days old — within the 6-month (180-day) window
    "bmi_value": 33.0,                  # above 30.0 standard threshold
    "bmi_days_old": 60,                 # 60 days old — within the 12-month (365-day) window
    "has_t2d_diagnosis": True,
    "has_weight_comorbidity": True,     # T2D counts as a weight-related comorbidity

    # Coverage
    "payer_name": "UnitedHealthcare",
    "plan_name": "UHC Choice Plus",
    "coverage_active": True,
    "drug_on_formulary": True,

    # Payer history
    "past_denials_similar": 0,

    # Cost
    "cost_estimate_monthly": 150.00,    # estimated copay after UHC tiered formulary
}

# ---------------------------------------------------------------------------
# Scenario A — variant: step therapy NOT met
# Same patient, same payer — but metformin history is absent.
# Used to test the warning/critical path in the scorer.
# ---------------------------------------------------------------------------
SCENARIO_A_NO_STEP_THERAPY: dict = {
    **SCENARIO_A,                       # ** unpacks SCENARIO_A into this dict, then we override
    "metformin_days": None,             # no metformin history on file
    "metformin_contraindicated": False,
}

# ---------------------------------------------------------------------------
# Scenario A — variant: multiple criteria missing
# Demonstrates critical (red) card.
# ---------------------------------------------------------------------------
SCENARIO_A_CRITICAL: dict = {
    **SCENARIO_A,
    "metformin_days": None,     # no step therapy
    "a1c_value": None,          # no A1C on file
    "a1c_days_old": None,
}

# ---------------------------------------------------------------------------
# Scenario B — PGx Safety Alert
# Patient: same synthetic patient (Derrick Lin), different drug order
# What it demonstrates: CYP2C19 poor metabolizer → clopidogrel ineffective
# ---------------------------------------------------------------------------
SCENARIO_B: dict = {
    "patient_id": "erXuFYUfucBZaryVksYEcMg3",
    "scenario_name": "PGx Safety Alert",
    "drug_name": "clopidogrel",
    "drug_class": "pgx_sensitive",

    # Genomic data — CYP2C19 *2/*2 = poor metabolizer
    # This is the most clinically significant PGx scenario for clopidogrel:
    # both alleles are non-functional, so no CYP2C19 activity at all.
    "pgx_data": {
        "CYP2C19": {
            "diplotype": "*2/*2",
            "metabolizer_status": "poor_metabolizer",
            "source": "synthetic_overlay",
        }
    },

    # Coverage — different payer from Scenario A (demonstrates multi-payer support)
    "payer_name": "Aetna",
    "plan_name": "Aetna PPO",
    "coverage_active": True,

    # Cost — clopidogrel is a generic drug, inexpensive
    "cost_estimate_monthly": 15.00,
}

# ---------------------------------------------------------------------------
# Scenario B variant — no PGx data on file
# Used to test the "recommend testing" card path.
# ---------------------------------------------------------------------------
SCENARIO_B_NO_PGX: dict = {
    **SCENARIO_B,
    "pgx_data": None,  # no genomic data available for this patient
}

# ---------------------------------------------------------------------------
# Scenario C — Oncology / Keytruda (pembrolizumab)
# Patient: separate Epic sandbox synthetic patient (Alex Garcia)
# What it demonstrates: NCCN pathway validated + PA bundle ready
# ---------------------------------------------------------------------------
SCENARIO_C: dict = {
    "patient_id": "eAB3mDIBBcyUKviyzrxsnAw3",
    "scenario_name": "Oncology Pathway Validation",
    "drug_name": "Keytruda",
    "drug_class": "oncology",

    # Oncology diagnosis
    "tumor_type": "NSCLC",                    # Non-small cell lung cancer
    "icd10_code": "C34.10",                   # NSCLC, main bronchus, unspecified side
    "tumor_stage": "Stage IV (metastatic)",

    # Biomarkers — PD-L1 strongly positive (qualifies for both first and second-line)
    "pd_l1_score": 80,                        # 80% TPS — well above the 50% first-line threshold
    "pd_l1_assay": "Dako 22C3",               # assay used for testing
    "egfr_mutation": False,                   # EGFR wild-type — no targeted therapy contraindication
    "alk_rearrangement": False,               # ALK-negative

    # Prior treatment history — platinum-based chemo completed
    # Presence of these regimens makes the patient eligible for second-line pathway.
    # PD-L1 80% also satisfies the first-line pathway — validator finds the best match.
    "prior_regimens": ["carboplatin", "pemetrexed"],
    "prior_regimen_cycles": 4,
    "prior_regimen_status": "completed",

    # Coverage
    "payer_name": "UnitedHealthcare",
    "plan_name": "UHC Oncology Preferred",
    "coverage_active": True,

    # Cost — biologic, high cost
    "cost_estimate_monthly": 15000.00,
}

# ---------------------------------------------------------------------------
# Scenario D — Standard Procedure / MRI Denial Prevention
# Patient: same Epic sandbox patient as Scenario C (Alex Garcia)
# What it demonstrates: 2 past denials + documentation gaps → high risk card
# ---------------------------------------------------------------------------
SCENARIO_D: dict = {
    "patient_id": "eAB3mDIBBcyUKviyzrxsnAw3",
    "scenario_name": "Denial Prevention — MRI Lumbar Spine",
    "procedure_name": "MRI Lumbar Spine",
    "procedure_code": "72148",              # CPT: MRI lumbar spine without contrast
    "drug_class": "standard",

    # Payer — Aetna has strict MRI documentation requirements
    "payer_name": "Aetna",
    "plan_name": "Aetna PPO Select",
    "coverage_active": True,

    # Past denial history — same procedure, same payer, 2 denials
    # This is the key demonstration: CFIP surfaces the pattern BEFORE submission.
    "past_denials_similar": 2,
    "past_denial_reasons": [
        "insufficient documentation",
        "insufficient documentation",
    ],
    "past_denials_detail": [
        {
            "date": "2024-08-15",
            "claim_id": "AET-20240815-001",
            "reason_code": "52",
            "reason_text": "Insufficient documentation — physical therapy records not provided",
        },
        {
            "date": "2024-11-20",
            "claim_id": "AET-20241120-002",
            "reason_code": "52",
            "reason_text": "Insufficient documentation — conservative treatment history required",
        },
    ],

    # Documentation status — what Aetna requires vs. what's on file
    # Aetna's MRI LCD requires 6 weeks of conservative therapy documentation.
    "required_docs": [
        "Physical therapy records (minimum 6 weeks / 3 months preferred)",
        "Plain film X-ray results (lumbar spine, within last 12 months)",
        "Physician progress notes documenting conservative treatment failure",
        "ICD-10 diagnosis code documentation (M54.5 — low back pain)",
    ],
    "docs_on_file": [
        "Physician referral for MRI",
        "ICD-10 diagnosis code documentation (M54.5 — low back pain)",
    ],
    "missing_docs": [
        "Physical therapy records (minimum 6 weeks / 3 months preferred)",
        "Plain film X-ray results (lumbar spine, within last 12 months)",
    ],
}

# ---------------------------------------------------------------------------
# Lookup by patient_id — the bridge uses this to find the right scenario.
# Both scenarios use the same patient_id (Derrick Lin) — the PGx agent
# selects the right pgx_data based on the drug being ordered.
# ---------------------------------------------------------------------------
SYNTHETIC_SCENARIOS: dict[str, dict] = {
    SCENARIO_A["patient_id"]: SCENARIO_A,
}

# PGx scenarios keyed by (patient_id, drug_name) — allows same patient,
# different drug orders to return different synthetic data.
# Tuple keys: Python tuples can be dict keys (they are hashable/immutable).
# C# analogy: Dictionary<(string patientId, string drug), SyntheticData>
SYNTHETIC_PGX_SCENARIOS: dict[tuple[str, str], dict] = {
    (SCENARIO_B["patient_id"], "clopidogrel"): SCENARIO_B,
    (SCENARIO_B["patient_id"], "plavix"):      SCENARIO_B,
}

# Oncology scenarios keyed by (patient_id, drug_name)
SYNTHETIC_ONCOLOGY_SCENARIOS: dict[tuple[str, str], dict] = {
    (SCENARIO_C["patient_id"], "pembrolizumab"): SCENARIO_C,
    (SCENARIO_C["patient_id"], "keytruda"):      SCENARIO_C,
}

# Standard procedure denial scenarios keyed by (patient_id, procedure_name_lowercase)
SYNTHETIC_DENIAL_SCENARIOS: dict[tuple[str, str], dict] = {
    (SCENARIO_D["patient_id"], "mri lumbar spine"): SCENARIO_D,
    (SCENARIO_D["patient_id"], "mri"):              SCENARIO_D,
}


def get_synthetic_data(patient_id: str) -> dict | None:
    """
    Return the GLP-1 / denial pipeline synthetic scenario data for a patient.
    Returns None if no synthetic data exists for this patient.
    Used by the denial bridge (denial_prediction.py).
    """
    return SYNTHETIC_SCENARIOS.get(patient_id)


def get_synthetic_denial_data(patient_id: str, procedure_name: str) -> dict | None:
    """
    Return the denial prevention synthetic scenario for a (patient, procedure) pair.
    Returns None if no data exists.
    Used by the orchestrator's standard chain steps (fetch_claims_history, etc.).

    Substring matching handles names like "MRI Lumbar Spine without contrast".
    """
    normalized = procedure_name.strip().lower()

    result = SYNTHETIC_DENIAL_SCENARIOS.get((patient_id, normalized))
    if result:
        return result

    known_procs = {proc for _, proc in SYNTHETIC_DENIAL_SCENARIOS}
    for known in sorted(known_procs, key=len, reverse=True):
        if known in normalized:
            return SYNTHETIC_DENIAL_SCENARIOS.get((patient_id, known))

    return None


def get_synthetic_oncology_data(patient_id: str, drug_name: str) -> dict | None:
    """
    Return the oncology synthetic scenario data for a (patient, drug) pair.
    Returns None if no synthetic oncology data exists for this combination.
    Used by the orchestrator's oncology steps (fetch_condition, fetch_biomarkers, …).

    Lookup is case-insensitive. Substring matching handles full FHIR display
    strings like "Keytruda (pembrolizumab) 100mg injection".
    """
    normalized = drug_name.strip().lower()

    # Exact match
    result = SYNTHETIC_ONCOLOGY_SCENARIOS.get((patient_id, normalized))
    if result:
        return result

    # Substring match — handles FHIR display strings
    known_drugs = {drug for _, drug in SYNTHETIC_ONCOLOGY_SCENARIOS}
    for known in sorted(known_drugs, key=len, reverse=True):
        if known in normalized:
            return SYNTHETIC_ONCOLOGY_SCENARIOS.get((patient_id, known))

    return None


def get_synthetic_pgx_data(patient_id: str, drug_name: str) -> dict | None:
    """
    Return the PGx synthetic scenario data for a (patient, drug) pair.
    Returns None if no synthetic PGx data exists for this combination.
    Used by the PGx agent (pgx_safety.py).

    Lookup is case-insensitive. Also handles full FHIR display strings like
    "clopidogrel (Plavix) 75mg tablet" by substring-matching against known keys.
    C# analogy: a dictionary lookup with a normalized key resolver.
    """
    normalized = drug_name.strip().lower()

    # Exact match first
    result = SYNTHETIC_PGX_SCENARIOS.get((patient_id, normalized))
    if result:
        return result

    # Substring match — handles full FHIR display strings
    # e.g. "clopidogrel (plavix) 75mg tablet" should hit the "clopidogrel" key
    known_drugs = {drug for _, drug in SYNTHETIC_PGX_SCENARIOS}
    for known in sorted(known_drugs, key=len, reverse=True):
        if known in normalized:
            return SYNTHETIC_PGX_SCENARIOS.get((patient_id, known))

    return None


# ---------------------------------------------------------------------------
# Phase 6 — Gap-fill overlay
# ---------------------------------------------------------------------------

# LOINC codes for labs we can extract from Epic FHIR sandbox
_LOINC_A1C = "4548-4"      # Hemoglobin A1c/Hemoglobin.total in Blood
_LOINC_BMI = "39156-5"     # Body mass index (BMI) [Ratio]

# Chemotherapy agents recognised in MedicationRequest display strings
_CHEMO_AGENTS = {
    "carboplatin", "pemetrexed", "cisplatin",
    "paclitaxel", "docetaxel", "gemcitabine",
}

# Metformin RxNorm / display matching
_METFORMIN_KEYWORDS = {"metformin", "glucophage", "fortamet", "glumetza"}


@dataclass
class GapFilledScenario:
    """
    A synthetic scenario merged with any available FHIR data.

    Each field in `data` came from either the real Epic FHIR API or the
    synthetic overlay — tracked in `data_sources` for card footnotes.

    C# analogy: a result record carrying both the merged data and an audit map.
    """
    data: dict = field(default_factory=dict)

    # field_name → "fhir" | "synthetic"
    # Used by card_composer to render the data source footnote on each card.
    data_sources: dict[str, str] = field(default_factory=dict)

    fhir_field_count: int = 0
    synthetic_field_count: int = 0

    @property
    def has_any_fhir_data(self) -> bool:
        return self.fhir_field_count > 0

    def summary_line(self) -> str:
        """Human-readable summary for chain_log."""
        return (
            f"overlay: {self.fhir_field_count} field(s) from FHIR, "
            f"{self.synthetic_field_count} from synthetic"
        )


def fill_gaps(
    patient_id: str,
    drug_name: str,
    fhir_bundle: "FhirDataBundle",
) -> GapFilledScenario | None:
    """
    Return a scenario merged from real FHIR data (where available) + synthetic overlay.

    Algorithm:
      1. Look up the synthetic scenario for this patient/drug.
      2. For each clinically significant field, check if real FHIR data exists.
      3. If FHIR has the value → use it, tag as "fhir".
      4. If FHIR is missing it → use synthetic, tag as "synthetic".
      5. Respects USE_SYNTHETIC_OVERLAY=false (returns FHIR-only data with gaps as None).

    Returns None if no synthetic scenario exists for this patient/drug combination
    (e.g. a drug we have no scenario data for).

    C# analogy: a Merge(FhirData fhir, SyntheticData synthetic) factory that prefers
    real data and falls back gracefully.
    """
    from app.config import get_settings
    settings = get_settings()

    # Determine which scenario type this patient/drug maps to
    drug_class = _infer_drug_class(patient_id, drug_name)

    if drug_class == "glp1":
        return _fill_glp1_gaps(patient_id, fhir_bundle, settings.use_synthetic_overlay)

    if drug_class == "oncology":
        return _fill_oncology_gaps(patient_id, drug_name, fhir_bundle, settings.use_synthetic_overlay)

    # PGx and standard scenarios — no direct FHIR extraction here
    # (PGx genomic data is handled in pgx_safety.py; standard uses claims history)
    scenario = (
        get_synthetic_pgx_data(patient_id, drug_name)
        or get_synthetic_denial_data(patient_id, drug_name)
    )
    if not scenario:
        return None

    if not settings.use_synthetic_overlay:
        return GapFilledScenario()  # pure FHIR mode → return empty

    return GapFilledScenario(
        data=dict(scenario),
        data_sources={k: "synthetic" for k in scenario if not k.startswith("patient_id")},
        synthetic_field_count=len(scenario),
    )


def _infer_drug_class(patient_id: str, drug_name: str) -> str:
    """
    Quickly determine drug class from patient/drug lookup tables.
    Used by fill_gaps() to select the right field extraction logic.
    """
    normalized = drug_name.strip().lower()

    if get_synthetic_oncology_data(patient_id, drug_name):
        return "oncology"

    if get_synthetic_pgx_data(patient_id, drug_name):
        return "pgx_sensitive"

    # GLP-1 scenarios are keyed only by patient_id
    if patient_id in SYNTHETIC_SCENARIOS:
        glp1_drugs = {"ozempic", "wegovy", "semaglutide", "mounjaro", "tirzepatide",
                      "saxenda", "liraglutide", "victoza", "trulicity", "dulaglutide"}
        if any(d in normalized for d in glp1_drugs) or not normalized:
            return "glp1"

    return "standard"


def _fill_glp1_gaps(
    patient_id: str,
    fhir_bundle: "FhirDataBundle",
    use_synthetic: bool,
) -> GapFilledScenario | None:
    """
    Build a GLP-1 scenario merging real FHIR labs + meds + coverage with synthetic overlay.

    Real FHIR data expected from Epic sandbox for GLP-1 patients:
      - A1C (LOINC 4548-4) — often present for diabetes patients
      - BMI (LOINC 39156-5) — often present
      - Metformin history — may appear in MedicationRequest

    Always synthetic (not in public sandbox):
      - Coverage / payer details
      - CYP2C19 genotype (handled in PGx path, not here)
    """
    base = SYNTHETIC_SCENARIOS.get(patient_id)
    if base is None:
        return None

    merged: dict = dict(base)
    sources: dict[str, str] = {k: "synthetic" for k in base}
    fhir_count = 0
    synthetic_count = len(base)

    if not use_synthetic:
        # Pure FHIR mode: start from empty, only add what FHIR provides
        merged = {"patient_id": patient_id}
        sources = {}
        synthetic_count = 0

    # ── A1C ──────────────────────────────────────────────────────────────────
    a1c = _extract_lab_value(fhir_bundle, _LOINC_A1C)
    if a1c is not None:
        a1c_days = _lab_days_old(fhir_bundle, _LOINC_A1C)
        merged["a1c_value"] = a1c
        merged["a1c_days_old"] = a1c_days
        sources["a1c_value"] = "fhir"
        sources["a1c_days_old"] = "fhir"
        fhir_count += 2
        synthetic_count -= 2 if use_synthetic else 0
        logger.info("fill_gaps GLP-1: A1C from FHIR = %.1f%% (%s days old)", a1c, a1c_days)
    else:
        logger.debug("fill_gaps GLP-1: A1C not in FHIR — using synthetic")

    # ── BMI ──────────────────────────────────────────────────────────────────
    bmi = _extract_lab_value(fhir_bundle, _LOINC_BMI)
    if bmi is not None:
        bmi_days = _lab_days_old(fhir_bundle, _LOINC_BMI)
        merged["bmi_value"] = bmi
        merged["bmi_days_old"] = bmi_days
        sources["bmi_value"] = "fhir"
        sources["bmi_days_old"] = "fhir"
        fhir_count += 2
        synthetic_count -= 2 if use_synthetic else 0
        logger.info("fill_gaps GLP-1: BMI from FHIR = %.1f (%s days old)", bmi, bmi_days)
    else:
        logger.debug("fill_gaps GLP-1: BMI not in FHIR — using synthetic")

    # ── Metformin ─────────────────────────────────────────────────────────────
    metformin_days = _extract_metformin_days(fhir_bundle)
    if metformin_days is not None:
        merged["metformin_days"] = metformin_days
        sources["metformin_days"] = "fhir"
        fhir_count += 1
        synthetic_count -= 1 if use_synthetic else 0
        logger.info("fill_gaps GLP-1: metformin from FHIR = %d days", metformin_days)
    else:
        logger.debug("fill_gaps GLP-1: metformin not in FHIR — using synthetic")

    # ── Payer / coverage ─────────────────────────────────────────────────────
    payer = _extract_payer_name(fhir_bundle)
    if payer:
        merged["payer_name"] = payer
        merged["coverage_active"] = True
        sources["payer_name"] = "fhir"
        sources["coverage_active"] = "fhir"
        fhir_count += 2
        synthetic_count -= 2 if use_synthetic else 0
        logger.info("fill_gaps GLP-1: payer from FHIR = %s", payer)
    else:
        logger.debug("fill_gaps GLP-1: payer not in FHIR — using synthetic")

    return GapFilledScenario(
        data=merged,
        data_sources=sources,
        fhir_field_count=fhir_count,
        synthetic_field_count=max(0, synthetic_count),
    )


def _fill_oncology_gaps(
    patient_id: str,
    drug_name: str,
    fhir_bundle: "FhirDataBundle",
    use_synthetic: bool,
) -> GapFilledScenario | None:
    """
    Build an oncology scenario merging real FHIR conditions + meds with synthetic overlay.

    Real FHIR data that may exist:
      - Lung cancer ICD-10 diagnosis (C34.x) in Condition resources
      - Prior carboplatin / pemetrexed in MedicationRequest

    Always synthetic (not in public sandbox):
      - PD-L1 score (proprietary assay — no standard LOINC in sandbox)
      - Biomarker mutation status (EGFR, ALK)
      - Tumor stage (usually in staging resources not returned by basic Condition search)
    """
    base = get_synthetic_oncology_data(patient_id, drug_name)
    if base is None:
        return None

    merged: dict = dict(base)
    sources: dict[str, str] = {k: "synthetic" for k in base}
    fhir_count = 0
    synthetic_count = len(base)

    if not use_synthetic:
        merged = {"patient_id": patient_id}
        sources = {}
        synthetic_count = 0

    # ── Tumor type from FHIR Condition ────────────────────────────────────────
    icd10 = _extract_lung_cancer_icd10(fhir_bundle)
    if icd10:
        merged["icd10_code"] = icd10
        merged["tumor_type"] = "NSCLC"
        sources["icd10_code"] = "fhir"
        sources["tumor_type"] = "fhir"
        fhir_count += 2
        synthetic_count -= 2 if use_synthetic else 0
        logger.info("fill_gaps oncology: ICD-10 from FHIR = %s", icd10)

    # ── Prior chemotherapy from FHIR MedicationRequest ────────────────────────
    fhir_regimens = _extract_chemo_regimens(fhir_bundle)
    if fhir_regimens:
        merged["prior_regimens"] = fhir_regimens
        sources["prior_regimens"] = "fhir"
        fhir_count += 1
        synthetic_count -= 1 if use_synthetic else 0
        logger.info("fill_gaps oncology: prior regimens from FHIR = %s", fhir_regimens)

    return GapFilledScenario(
        data=merged,
        data_sources=sources,
        fhir_field_count=fhir_count,
        synthetic_field_count=max(0, synthetic_count),
    )


# ---------------------------------------------------------------------------
# Low-level FHIR extraction helpers
# (same logic as orchestrator helpers but operating on FhirDataBundle fields)
# ---------------------------------------------------------------------------

def _extract_lab_value(bundle: "FhirDataBundle", loinc_code: str) -> float | None:
    """
    Find the most recent numeric lab value for a given LOINC code.

    FHIR Observation valueQuantity.value holds the numeric result.
    Returns None if no matching observation is found.
    C# analogy: observations.Where(o => o.Code == loinc).OrderByDescending(o => o.Effective)
                            .FirstOrDefault()?.Value?.Value
    """
    best_date: date | None = None
    best_value: float | None = None

    for obs in bundle.lab_observations:
        for coding in obs.get("code", {}).get("coding", []):
            if coding.get("code") == loinc_code:
                raw_value = obs.get("valueQuantity", {}).get("value")
                if raw_value is None:
                    continue

                # Parse the effective date to find the most recent result
                effective = obs.get("effectiveDateTime", obs.get("effectivePeriod", {}).get("start", ""))
                try:
                    obs_date = date.fromisoformat(str(effective)[:10])
                except (ValueError, TypeError):
                    obs_date = None

                if best_date is None or (obs_date and obs_date > best_date):
                    best_date = obs_date
                    best_value = float(raw_value)

    return best_value


def _lab_days_old(bundle: "FhirDataBundle", loinc_code: str) -> int | None:
    """Return how many days ago the most recent matching lab observation was taken."""
    for obs in bundle.lab_observations:
        for coding in obs.get("code", {}).get("coding", []):
            if coding.get("code") == loinc_code:
                effective = obs.get("effectiveDateTime", "")
                try:
                    obs_date = date.fromisoformat(str(effective)[:10])
                    return (date.today() - obs_date).days
                except (ValueError, TypeError):
                    return None
    return None


def _extract_metformin_days(bundle: "FhirDataBundle") -> int | None:
    """
    Estimate how long the patient has been on metformin by finding the earliest
    active MedicationRequest and calculating days from authoredOn to today.

    Returns None if no metformin order is found.
    C# analogy: meds.Where(m => m.IsMetformin()).Min(m => m.AuthoredOn)
    """
    earliest: date | None = None

    for med in bundle.medications:
        display = (
            med.get("medicationCodeableConcept", {}).get("text", "")
            or next(
                (c.get("display", "") for c in
                 med.get("medicationCodeableConcept", {}).get("coding", [])),
                "",
            )
        ).lower()

        if any(kw in display for kw in _METFORMIN_KEYWORDS):
            authored = med.get("authoredOn", "")
            try:
                authored_date = date.fromisoformat(str(authored)[:10])
                if earliest is None or authored_date < earliest:
                    earliest = authored_date
            except (ValueError, TypeError):
                pass

    if earliest is None:
        return None
    return (date.today() - earliest).days


def _extract_payer_name(bundle: "FhirDataBundle") -> str | None:
    """Extract the first active payer display name from Coverage resources."""
    for coverage in bundle.coverage:
        for payor in coverage.get("payor", []):
            name: str = payor.get("display", "")
            if name:
                return name
    return None


def _extract_lung_cancer_icd10(bundle: "FhirDataBundle") -> str | None:
    """Return the ICD-10 code if any Condition resource is a lung cancer diagnosis."""
    for condition in bundle.conditions:
        for coding in condition.get("code", {}).get("coding", []):
            code: str = coding.get("code", "")
            if code.startswith(("C34", "C33")):
                return code
    return None


def _extract_chemo_regimens(bundle: "FhirDataBundle") -> list[str]:
    """Return known chemo agents found in MedicationRequest resources."""
    found: list[str] = []
    for med in bundle.medications:
        concept = med.get("medicationCodeableConcept", {})
        display = (
            concept.get("text", "")
            or next((c.get("display", "") for c in concept.get("coding", [])), "")
        ).lower()
        for agent in _CHEMO_AGENTS:
            if agent in display and agent not in found:
                found.append(agent)
    return found
