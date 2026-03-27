"""
Seed payer rules and denial patterns for Phase 3.

Run once from the project root:
    python app/data/seed_payer_rules.py

Idempotent — checks for existing rows before inserting.
Safe to run multiple times without duplicating data.

Data is synthetic but modeled on real UHC GLP-1 prior authorization requirements.
Sources: UHC Medical Policy for Semaglutide/GLP-1 receptor agonists (public).
"""

import sys
from pathlib import Path

# Allow running as a script from the project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.data.db import get_connection, init_db

# ---------------------------------------------------------------------------
# UHC GLP-1 payer rules
# These represent the criteria UHC uses to evaluate GLP-1 PA requests.
# Each rule_key maps directly to a scoring factor in denial_scorer.py.
# ---------------------------------------------------------------------------
UHC_GLP1_RULES = [
    # Step therapy — metformin trial requirement
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "glp1",
        "rule_type": "step_therapy",
        "rule_key": "min_metformin_days",
        "rule_value": "90",
        "description": "Patient must have documented metformin trial of at least 90 days "
                       "at therapeutic dose, unless contraindicated or intolerant.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "glp1",
        "rule_type": "step_therapy",
        "rule_key": "metformin_contraindication_accepted",
        "rule_value": "true",
        "description": "Step therapy waived if metformin is contraindicated (eGFR <30, "
                       "allergy, GI intolerance documented by provider).",
    },

    # Clinical criteria
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "glp1",
        "rule_type": "clinical_criteria",
        "rule_key": "min_a1c",
        "rule_value": "7.0",
        "description": "HbA1c ≥7.0% required. Must be documented within the last 6 months.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "glp1",
        "rule_type": "clinical_criteria",
        "rule_key": "min_bmi_standard",
        "rule_value": "30.0",
        "description": "BMI ≥30 required for standard obesity indication.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "glp1",
        "rule_type": "clinical_criteria",
        "rule_key": "min_bmi_with_comorbidity",
        "rule_value": "27.0",
        "description": "BMI ≥27 accepted if patient has at least one weight-related comorbidity "
                       "(T2D, hypertension, dyslipidemia, OSA).",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "glp1",
        "rule_type": "clinical_criteria",
        "rule_key": "diagnosis_required",
        "rule_value": "T2D",
        "description": "Active Type 2 Diabetes diagnosis required (ICD-10 E11.x).",
    },

    # Documentation requirements
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "glp1",
        "rule_type": "documentation",
        "rule_key": "required_doc_a1c_months",
        "rule_value": "6",
        "description": "HbA1c lab result must be on file, dated within the last 6 months.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "glp1",
        "rule_type": "documentation",
        "rule_key": "required_doc_bmi_months",
        "rule_value": "12",
        "description": "BMI measurement must be on file, dated within the last 12 months.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "glp1",
        "rule_type": "documentation",
        "rule_key": "required_doc_rx_history",
        "rule_value": "true",
        "description": "Medication history documenting metformin trial must be on file.",
    },

    # Baseline denial rate for this drug class + payer combination
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "glp1",
        "rule_type": "baseline",
        "rule_key": "denial_rate_baseline",
        "rule_value": "0.22",
        "description": "Historical UHC denial rate for GLP-1 PA requests: ~22% "
                       "(industry data, 2023-2024).",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "glp1",
        "rule_type": "baseline",
        "rule_key": "avg_processing_days",
        "rule_value": "3.5",
        "description": "Average PA processing time for UHC GLP-1 requests: 3-4 business days.",
    },
]

# ---------------------------------------------------------------------------
# UHC GLP-1 denial patterns
# frequency = fraction of all GLP-1 denials attributed to this reason
# Must sum to ≤1.0 (some denials are multi-reason)
# ---------------------------------------------------------------------------
UHC_GLP1_DENIAL_PATTERNS = [
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "glp1",
        "denial_reason": "step_therapy_not_met",
        "frequency": 0.45,
        "recommendation": "Document metformin trial ≥90 days or provide contraindication "
                          "letter. This is the #1 denial reason — address first.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "glp1",
        "denial_reason": "missing_or_outdated_a1c",
        "frequency": 0.28,
        "recommendation": "Order HbA1c lab if >6 months old. Include lab report in PA bundle. "
                          "Results must show A1C ≥7.0%.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "glp1",
        "denial_reason": "bmi_below_threshold",
        "frequency": 0.15,
        "recommendation": "Ensure BMI ≥30 is documented. If BMI 27-29.9, document weight-related "
                          "comorbidity (T2D, HTN, dyslipidemia) in the PA request.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "glp1",
        "denial_reason": "incomplete_documentation",
        "frequency": 0.12,
        "recommendation": "Review documentation checklist: A1C lab, BMI measurement, "
                          "medication history, and diagnosis codes must all be present.",
    },
]


# ---------------------------------------------------------------------------
# UHC Oncology / pembrolizumab payer rules
# Based on UHC Medical Policy: Pembrolizumab (Keytruda) — NSCLC indications.
# ---------------------------------------------------------------------------
UHC_ONCOLOGY_RULES = [
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "oncology",
        "rule_type": "biomarker",
        "rule_key": "pd_l1_assay_required",
        "rule_value": "true",
        "description": "FDA-approved PD-L1 immunohistochemistry assay (Dako 22C3) required "
                       "before pembrolizumab can be approved for NSCLC.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "oncology",
        "rule_type": "biomarker",
        "rule_key": "min_pd_l1_first_line_pct",
        "rule_value": "50",
        "description": "PD-L1 TPS ≥50% required for first-line monotherapy (no prior chemo).",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "oncology",
        "rule_type": "biomarker",
        "rule_key": "min_pd_l1_second_line_pct",
        "rule_value": "1",
        "description": "PD-L1 TPS ≥1% required for second-line use after prior platinum-based chemotherapy.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "oncology",
        "rule_type": "clinical_criteria",
        "rule_key": "nccn_pathway_required",
        "rule_value": "true",
        "description": "Drug must be used on an NCCN-validated pathway for the patient's tumor type.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "oncology",
        "rule_type": "clinical_criteria",
        "rule_key": "egfr_alk_exclusion",
        "rule_value": "true",
        "description": "Patients with actionable EGFR mutations or ALK rearrangements should receive "
                       "targeted therapy first. Pembrolizumab is not indicated as first-line for these patients.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "oncology",
        "rule_type": "documentation",
        "rule_key": "required_doc_pd_l1_report",
        "rule_value": "true",
        "description": "PD-L1 pathology report with TPS score must be submitted with the PA request.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "oncology",
        "rule_type": "documentation",
        "rule_key": "required_doc_prior_regimen",
        "rule_value": "true",
        "description": "Documentation of prior platinum-based chemotherapy regimen required "
                       "for second-line use.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "oncology",
        "rule_type": "baseline",
        "rule_key": "denial_rate_baseline",
        "rule_value": "0.15",
        "description": "Historical UHC denial rate for oncology checkpoint inhibitor PA requests: ~15%.",
    },
]

# ---------------------------------------------------------------------------
# UHC Oncology denial patterns
# ---------------------------------------------------------------------------
UHC_ONCOLOGY_DENIAL_PATTERNS = [
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "oncology",
        "denial_reason": "pd_l1_below_threshold",
        "frequency": 0.40,
        "recommendation": "Verify PD-L1 TPS score meets pathway minimum. For first-line "
                          "monotherapy, TPS must be ≥50%. For second-line, TPS must be ≥1%.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "oncology",
        "denial_reason": "missing_biomarker_report",
        "frequency": 0.30,
        "recommendation": "Submit Dako 22C3 PD-L1 pathology report. Without the assay result, "
                          "UHC will not process the PA.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "oncology",
        "denial_reason": "nccn_pathway_not_documented",
        "frequency": 0.20,
        "recommendation": "Include letter of medical necessity citing the specific NCCN "
                          "guideline and category supporting this indication.",
    },
    {
        "payer_name": "UnitedHealthcare",
        "drug_class": "oncology",
        "denial_reason": "egfr_alk_not_tested",
        "frequency": 0.10,
        "recommendation": "Submit EGFR mutation and ALK rearrangement test results confirming "
                          "no actionable driver mutations before initiating checkpoint inhibitor.",
    },
]


def seed() -> None:
    """Seed payer rules and denial patterns if not already present."""
    init_db()  # ensure tables exist before seeding

    with get_connection() as conn:
        # Check if UHC GLP-1 rules already exist — if so, skip to avoid duplicates.
        # fetchone() returns the first matching row, or None if no match.
        # C# analogy: .FirstOrDefault() returning null if not found.
        existing = conn.execute(
            "SELECT id FROM payer_rules WHERE payer_name = ? AND drug_class = ? LIMIT 1",
            ("UnitedHealthcare", "glp1"),
        ).fetchone()

        if existing:
            print("Payer rules already seeded — skipping.")
        else:
            # executemany() inserts a list of dicts in a single call.
            # C# analogy: DbContext.AddRange(rules); DbContext.SaveChanges()
            conn.executemany(
                """
                INSERT INTO payer_rules
                    (payer_name, drug_class, rule_type, rule_key, rule_value, description)
                VALUES
                    (:payer_name, :drug_class, :rule_type, :rule_key, :rule_value, :description)
                """,
                UHC_GLP1_RULES,
            )
            print(f"Seeded {len(UHC_GLP1_RULES)} payer rules for UHC / GLP-1.")

        # Same idempotency check for denial patterns
        existing_patterns = conn.execute(
            "SELECT id FROM denial_patterns WHERE payer_name = ? AND drug_class = ? LIMIT 1",
            ("UnitedHealthcare", "glp1"),
        ).fetchone()

        if existing_patterns:
            print("Denial patterns already seeded — skipping.")
        else:
            conn.executemany(
                """
                INSERT INTO denial_patterns
                    (payer_name, drug_class, denial_reason, frequency, recommendation)
                VALUES
                    (:payer_name, :drug_class, :denial_reason, :frequency, :recommendation)
                """,
                UHC_GLP1_DENIAL_PATTERNS,
            )
            print(f"Seeded {len(UHC_GLP1_DENIAL_PATTERNS)} denial patterns for UHC / GLP-1.")

        # ── UHC Oncology rules ──────────────────────────────────────────────
        existing_oncology = conn.execute(
            "SELECT id FROM payer_rules WHERE payer_name = ? AND drug_class = ? LIMIT 1",
            ("UnitedHealthcare", "oncology"),
        ).fetchone()

        if existing_oncology:
            print("Oncology payer rules already seeded — skipping.")
        else:
            conn.executemany(
                """
                INSERT INTO payer_rules
                    (payer_name, drug_class, rule_type, rule_key, rule_value, description)
                VALUES
                    (:payer_name, :drug_class, :rule_type, :rule_key, :rule_value, :description)
                """,
                UHC_ONCOLOGY_RULES,
            )
            print(f"Seeded {len(UHC_ONCOLOGY_RULES)} payer rules for UHC / Oncology.")

        existing_oncology_patterns = conn.execute(
            "SELECT id FROM denial_patterns WHERE payer_name = ? AND drug_class = ? LIMIT 1",
            ("UnitedHealthcare", "oncology"),
        ).fetchone()

        if existing_oncology_patterns:
            print("Oncology denial patterns already seeded — skipping.")
        else:
            conn.executemany(
                """
                INSERT INTO denial_patterns
                    (payer_name, drug_class, denial_reason, frequency, recommendation)
                VALUES
                    (:payer_name, :drug_class, :denial_reason, :frequency, :recommendation)
                """,
                UHC_ONCOLOGY_DENIAL_PATTERNS,
            )
            print(f"Seeded {len(UHC_ONCOLOGY_DENIAL_PATTERNS)} denial patterns for UHC / Oncology.")

    print("Seed complete. Database:", str(__import__('app.data.db', fromlist=['DB_PATH']).DB_PATH))


if __name__ == "__main__":
    seed()
