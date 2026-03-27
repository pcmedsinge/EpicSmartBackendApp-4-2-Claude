"""
Seed CPIC (Clinical Pharmacogenomics Implementation Consortium) drug-gene data.

Run once from the project root:
    python app/data/seed_cpic.py

Idempotent — safe to run multiple times without duplicating data.

Data source: real CPIC guidelines (https://cpicpgx.org)
  - Clopidogrel + CYP2C19: https://cpicpgx.org/guidelines/guideline-for-clopidogrel-and-cyp2c19/
  - Warfarin + CYP2C9/VKORC1: https://cpicpgx.org/guidelines/guideline-for-warfarin-and-cyp2c9-and-vkorc1/

These are published, peer-reviewed clinical rules — not synthetic.
CPIC evidence levels: 1A (strongest) → 1B → 2A → 2B (weakest).

Phase 4: clopidogrel + CYP2C19 is active.
         warfarin + CYP2C9 is seeded for Phase 5+.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.data.db import get_connection, init_db

# ---------------------------------------------------------------------------
# Clopidogrel + CYP2C19
#
# Clopidogrel is a prodrug — CYP2C19 must convert it to its active form.
# Without functional CYP2C19, the drug never activates → no antiplatelet effect.
# This is one of the most clinically important PGx interactions.
#
# CPIC recommendation: for poor and intermediate metabolizers, use an
# alternative antiplatelet agent (prasugrel or ticagrelor).
# ---------------------------------------------------------------------------
CLOPIDOGREL_CYP2C19 = [
    {
        "drug_name": "clopidogrel",
        "gene": "CYP2C19",
        "diplotype_pattern": "*2/*2",
        "metabolizer_status": "poor_metabolizer",
        "recommendation": (
            "CYP2C19 poor metabolizer. Clopidogrel is a prodrug that requires CYP2C19 "
            "for activation. This patient cannot convert clopidogrel to its active form — "
            "the drug will be ineffective. Use an alternative antiplatelet agent."
        ),
        "alternative_drug": "prasugrel, ticagrelor",
        "severity": "high",
        "evidence_level": "1A",
    },
    {
        "drug_name": "clopidogrel",
        "gene": "CYP2C19",
        "diplotype_pattern": "*2/*3",
        "metabolizer_status": "poor_metabolizer",
        "recommendation": (
            "CYP2C19 poor metabolizer (*2/*3). Reduced conversion to active metabolite. "
            "Clopidogrel will be largely ineffective. Use an alternative antiplatelet agent."
        ),
        "alternative_drug": "prasugrel, ticagrelor",
        "severity": "high",
        "evidence_level": "1A",
    },
    {
        "drug_name": "clopidogrel",
        "gene": "CYP2C19",
        "diplotype_pattern": "*3/*3",
        "metabolizer_status": "poor_metabolizer",
        "recommendation": (
            "CYP2C19 poor metabolizer (*3/*3). No functional CYP2C19 activity. "
            "Clopidogrel will be ineffective. Use an alternative antiplatelet agent."
        ),
        "alternative_drug": "prasugrel, ticagrelor",
        "severity": "high",
        "evidence_level": "1A",
    },
    {
        "drug_name": "clopidogrel",
        "gene": "CYP2C19",
        "diplotype_pattern": "*1/*2",
        "metabolizer_status": "intermediate_metabolizer",
        "recommendation": (
            "CYP2C19 intermediate metabolizer. Reduced but not absent CYP2C19 activity. "
            "Consider alternative antiplatelet therapy or monitor closely for reduced efficacy. "
            "Clinical context (ACS vs. stable CAD) should guide the decision."
        ),
        "alternative_drug": "prasugrel",
        "severity": "moderate",
        "evidence_level": "1A",
    },
    {
        "drug_name": "clopidogrel",
        "gene": "CYP2C19",
        "diplotype_pattern": "*1/*3",
        "metabolizer_status": "intermediate_metabolizer",
        "recommendation": (
            "CYP2C19 intermediate metabolizer (*1/*3). Reduced CYP2C19 activity. "
            "Consider alternative antiplatelet therapy or monitor closely."
        ),
        "alternative_drug": "prasugrel",
        "severity": "moderate",
        "evidence_level": "1A",
    },
    {
        "drug_name": "clopidogrel",
        "gene": "CYP2C19",
        "diplotype_pattern": "*1/*1",
        "metabolizer_status": "normal_metabolizer",
        "recommendation": (
            "CYP2C19 normal metabolizer. Standard clopidogrel activation expected. "
            "Use clopidogrel as prescribed per clinical guidelines."
        ),
        "alternative_drug": None,
        "severity": "low",
        "evidence_level": "1A",
    },
    {
        "drug_name": "clopidogrel",
        "gene": "CYP2C19",
        "diplotype_pattern": "*1/*17",
        "metabolizer_status": "rapid_metabolizer",
        "recommendation": (
            "CYP2C19 rapid metabolizer. Enhanced conversion to active metabolite. "
            "Use clopidogrel as prescribed. Monitor for increased bleeding risk."
        ),
        "alternative_drug": None,
        "severity": "low",
        "evidence_level": "1B",
    },
    {
        "drug_name": "clopidogrel",
        "gene": "CYP2C19",
        "diplotype_pattern": "*17/*17",
        "metabolizer_status": "ultrarapid_metabolizer",
        "recommendation": (
            "CYP2C19 ultrarapid metabolizer. Significantly enhanced activation. "
            "Use clopidogrel as prescribed but monitor for increased bleeding risk."
        ),
        "alternative_drug": None,
        "severity": "low",
        "evidence_level": "1B",
    },
]

# ---------------------------------------------------------------------------
# Warfarin + CYP2C9
#
# CYP2C9 metabolises (breaks down) warfarin. Poor metabolizers clear warfarin
# slowly → drug accumulates → bleeding risk if standard dose is used.
# CPIC recommends dose reduction for intermediate and poor metabolizers.
#
# Note: Full warfarin dosing also requires VKORC1 genotype (not seeded here).
# Seeded for Phase 5+ — Phase 4 only uses clopidogrel/CYP2C19 actively.
# ---------------------------------------------------------------------------
WARFARIN_CYP2C9 = [
    {
        "drug_name": "warfarin",
        "gene": "CYP2C9",
        "diplotype_pattern": "*1/*1",
        "metabolizer_status": "normal_metabolizer",
        "recommendation": (
            "CYP2C9 normal metabolizer. Standard warfarin dosing applies. "
            "Use clinical guidelines and INR monitoring as usual."
        ),
        "alternative_drug": None,
        "severity": "low",
        "evidence_level": "1A",
    },
    {
        "drug_name": "warfarin",
        "gene": "CYP2C9",
        "diplotype_pattern": "*1/*2",
        "metabolizer_status": "intermediate_metabolizer",
        "recommendation": (
            "CYP2C9 intermediate metabolizer (*1/*2). Reduced warfarin clearance. "
            "Consider initiating at 75-80% of the standard dose. Monitor INR closely."
        ),
        "alternative_drug": None,
        "severity": "moderate",
        "evidence_level": "1A",
    },
    {
        "drug_name": "warfarin",
        "gene": "CYP2C9",
        "diplotype_pattern": "*1/*3",
        "metabolizer_status": "intermediate_metabolizer",
        "recommendation": (
            "CYP2C9 intermediate metabolizer (*1/*3). Significantly reduced warfarin clearance. "
            "Consider initiating at 65-70% of the standard dose. Monitor INR closely."
        ),
        "alternative_drug": None,
        "severity": "moderate",
        "evidence_level": "1A",
    },
    {
        "drug_name": "warfarin",
        "gene": "CYP2C9",
        "diplotype_pattern": "*2/*2",
        "metabolizer_status": "poor_metabolizer",
        "recommendation": (
            "CYP2C9 poor metabolizer (*2/*2). Markedly reduced warfarin clearance — "
            "high bleeding risk at standard doses. Initiate at 50% of standard dose. "
            "Monitor INR very closely during initiation. Consider frequent dose adjustments."
        ),
        "alternative_drug": None,
        "severity": "high",
        "evidence_level": "1A",
    },
    {
        "drug_name": "warfarin",
        "gene": "CYP2C9",
        "diplotype_pattern": "*2/*3",
        "metabolizer_status": "poor_metabolizer",
        "recommendation": (
            "CYP2C9 poor metabolizer (*2/*3). Markedly reduced warfarin clearance. "
            "Initiate at 40-50% of standard dose. Monitor INR very closely."
        ),
        "alternative_drug": None,
        "severity": "high",
        "evidence_level": "1A",
    },
    {
        "drug_name": "warfarin",
        "gene": "CYP2C9",
        "diplotype_pattern": "*3/*3",
        "metabolizer_status": "poor_metabolizer",
        "recommendation": (
            "CYP2C9 poor metabolizer (*3/*3). Severely reduced warfarin clearance — "
            "extreme bleeding risk at standard doses. Initiate at 30-40% of standard dose. "
            "Consider alternative anticoagulant. Requires very close INR monitoring."
        ),
        "alternative_drug": None,
        "severity": "high",
        "evidence_level": "1A",
    },
]


def seed() -> None:
    """Seed CPIC rules if not already present."""
    init_db()  # ensures cpic_rules table exists

    with get_connection() as conn:
        # Idempotency check — skip if clopidogrel rules already seeded
        existing_clopi = conn.execute(
            "SELECT id FROM cpic_rules WHERE drug_name = ? LIMIT 1",
            ("clopidogrel",),
        ).fetchone()

        if existing_clopi:
            print("Clopidogrel CPIC rules already seeded — skipping.")
        else:
            conn.executemany(
                """
                INSERT INTO cpic_rules
                    (drug_name, gene, diplotype_pattern, metabolizer_status,
                     recommendation, alternative_drug, severity, evidence_level)
                VALUES
                    (:drug_name, :gene, :diplotype_pattern, :metabolizer_status,
                     :recommendation, :alternative_drug, :severity, :evidence_level)
                """,
                CLOPIDOGREL_CYP2C19,
            )
            print(f"Seeded {len(CLOPIDOGREL_CYP2C19)} clopidogrel/CYP2C19 CPIC rules.")

        existing_warf = conn.execute(
            "SELECT id FROM cpic_rules WHERE drug_name = ? LIMIT 1",
            ("warfarin",),
        ).fetchone()

        if existing_warf:
            print("Warfarin CPIC rules already seeded — skipping.")
        else:
            conn.executemany(
                """
                INSERT INTO cpic_rules
                    (drug_name, gene, diplotype_pattern, metabolizer_status,
                     recommendation, alternative_drug, severity, evidence_level)
                VALUES
                    (:drug_name, :gene, :diplotype_pattern, :metabolizer_status,
                     :recommendation, :alternative_drug, :severity, :evidence_level)
                """,
                WARFARIN_CYP2C9,
            )
            print(f"Seeded {len(WARFARIN_CYP2C9)} warfarin/CYP2C9 CPIC rules.")

    print("CPIC seed complete.")


if __name__ == "__main__":
    seed()
