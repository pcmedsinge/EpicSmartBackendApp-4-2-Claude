"""
Demo scenario definitions for the CDS Hooks test harness.

Each scenario captures everything needed to fire a realistic hook request
at CFIP without requiring Epic Hyperspace — patient context, medication
being ordered, and pre-built prefetch data.

Phase 2: Scenario A only (Ozempic / GLP-1).
Phase 4: Scenario B (clopidogrel / PGx) added.
Phase 5: Scenarios C (Keytruda oncology) and D (MRI denial prevention) fully wired.
"""

from dataclasses import dataclass, field


@dataclass
class Scenario:
    """
    A complete demo scenario for the CDS Hooks test harness.

    @dataclass auto-generates __init__, __repr__, __eq__ from annotated fields.
    C# analogy: a record with init-only properties.
    """
    id: str                         # short key, e.g. "A"
    name: str                       # display name
    description: str
    patient_id: str                 # Epic sandbox FHIR patient ID
    hook: str                       # which CDS hook fires
    context: dict                   # hook-specific context payload
    prefetch: dict = field(default_factory=dict)  # pre-built FHIR prefetch data


# ---------------------------------------------------------------------------
# Scenario A — GLP-1 Prior Authorization (Ozempic)
# Patient: Derrick Lin (erXuFYUfucBZaryVksYEcMg3)
# Clinical context: T2 diabetes, BMI 33, A1C 7.5, tried metformin 6 months
# Payer: UnitedHealthcare
# What CFIP will show (Phase 5): full PA chain, 87% approval, $150/mo
# ---------------------------------------------------------------------------
SCENARIO_A = Scenario(
    id="A",
    name="GLP-1 Prior Authorization — Ozempic",
    description=(
        "T2 diabetic patient ordering semaglutide (Ozempic). "
        "Requires prior authorization with step therapy verification. "
        "Expected: PA required, high approval probability, cost estimate."
    ),
    patient_id="erXuFYUfucBZaryVksYEcMg3",
    hook="order-select",
    context={
        "patientId": "erXuFYUfucBZaryVksYEcMg3",
        "userId": "Practitioner/demo-practitioner",
        "encounterId": "demo-encounter-001",
        # selections: the medication(s) the clinician just picked in order entry
        "selections": ["MedicationRequest/demo-ozempic-order"],
        "draftOrders": {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "MedicationRequest",
                        "id": "demo-ozempic-order",
                        "status": "draft",
                        "intent": "proposal",
                        "subject": {"reference": "Patient/erXuFYUfucBZaryVksYEcMg3"},
                        "medicationCodeableConcept": {
                            # RxNorm code for semaglutide 0.5mg/dose injection
                            "coding": [
                                {
                                    "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                    "code": "2200750",
                                    "display": "semaglutide 0.5 MG/DOSE subcutaneous injection",
                                }
                            ],
                            "text": "Ozempic (semaglutide) 0.5mg injection",
                        },
                        "dosageInstruction": [
                            {"text": "0.5mg subcutaneous once weekly"}
                        ],
                    }
                }
            ],
        },
    },
    prefetch={
        # Patient resource — Epic would normally fetch this for us
        "patient": {
            "resourceType": "Patient",
            "id": "erXuFYUfucBZaryVksYEcMg3",
            "name": [
                {
                    "use": "official",
                    "family": "Lin",
                    "given": ["Derrick"],
                }
            ],
            "gender": "male",
            "birthDate": "1973-11-05",
        },
        # MedicationRequest bundle — active medications for this patient
        "medications": {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": 1,
            "entry": [
                {
                    "resource": {
                        "resourceType": "MedicationRequest",
                        "id": "demo-ozempic-order",
                        "status": "draft",
                        "medicationCodeableConcept": {
                            "coding": [
                                {
                                    "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                    "code": "2200750",
                                    "display": "semaglutide 0.5 MG/DOSE subcutaneous injection",
                                }
                            ],
                            "text": "Ozempic (semaglutide) 0.5mg injection",
                        },
                    }
                }
            ],
        },
    },
)

# ---------------------------------------------------------------------------
# Scenario B — PGx Safety Alert (Clopidogrel / CYP2C19)
# Patient: Derrick Lin (same patient ID — reuses synthetic overlay)
# Clinical context: CYP2C19 *2/*2 poor metabolizer → clopidogrel ineffective
# Expected: CPIC critical alert, recommend prasugrel or ticagrelor
# ---------------------------------------------------------------------------
SCENARIO_B = Scenario(
    id="B",
    name="PGx Safety Alert — Clopidogrel",
    description=(
        "CYP2C19 poor metabolizer (*2/*2) prescribed clopidogrel (Plavix). "
        "CPIC guidelines flag this as ineffective — clopidogrel requires CYP2C19 "
        "activation and poor metabolizers have <10% of normal platelet inhibition. "
        "Expected: critical safety alert, switch to prasugrel or ticagrelor."
    ),
    patient_id="erXuFYUfucBZaryVksYEcMg3",
    hook="order-select",
    context={
        "patientId": "erXuFYUfucBZaryVksYEcMg3",
        "userId": "Practitioner/demo-practitioner",
        "encounterId": "demo-encounter-002",
        # selections: the medication the clinician just picked in order entry
        "selections": ["MedicationRequest/demo-clopidogrel-order"],
        "draftOrders": {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "MedicationRequest",
                        "id": "demo-clopidogrel-order",
                        "status": "draft",
                        "intent": "proposal",
                        "subject": {"reference": "Patient/erXuFYUfucBZaryVksYEcMg3"},
                        "medicationCodeableConcept": {
                            # RxNorm code 309362 = clopidogrel 75 MG oral tablet (Plavix)
                            "coding": [
                                {
                                    "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                    "code": "309362",
                                    "display": "clopidogrel 75 MG oral tablet",
                                }
                            ],
                            "text": "clopidogrel (Plavix) 75mg tablet",
                        },
                        "dosageInstruction": [
                            {"text": "75mg by mouth once daily"}
                        ],
                    }
                }
            ],
        },
    },
    prefetch={
        # Patient resource — same Derrick Lin used in Scenario A
        "patient": {
            "resourceType": "Patient",
            "id": "erXuFYUfucBZaryVksYEcMg3",
            "name": [
                {
                    "use": "official",
                    "family": "Lin",
                    "given": ["Derrick"],
                }
            ],
            "gender": "male",
            "birthDate": "1973-11-05",
        },
        # MedicationRequest bundle — active medications for this patient
        "medications": {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": 1,
            "entry": [
                {
                    "resource": {
                        "resourceType": "MedicationRequest",
                        "id": "demo-clopidogrel-order",
                        "status": "draft",
                        "medicationCodeableConcept": {
                            "coding": [
                                {
                                    "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                    "code": "309362",
                                    "display": "clopidogrel 75 MG oral tablet",
                                }
                            ],
                            "text": "clopidogrel (Plavix) 75mg tablet",
                        },
                    }
                }
            ],
        },
    },
)

# ---------------------------------------------------------------------------
# Scenario C — Oncology PA (Keytruda / pembrolizumab)
# Patient: Alex Garcia (eAB3mDIBBcyUKviyzrxsnAw3) — Epic sandbox synthetic patient
# Clinical context: NSCLC (C34.10), PD-L1 80%, prior carboplatin+pemetrexed
# Payer: UnitedHealthcare
# What CFIP will show (Phase 5): NCCN pathway validated, PA bundle ready, appeal if needed
# ---------------------------------------------------------------------------
SCENARIO_C = Scenario(
    id="C",
    name="Oncology PA — Keytruda (Pembrolizumab)",
    description=(
        "NSCLC patient (PD-L1 80%) ordering pembrolizumab (Keytruda). "
        "Prior regimen: carboplatin + pemetrexed. Payer: UnitedHealthcare. "
        "Expected: NCCN Category 1 pathway approved, PA bundle ready to submit."
    ),
    patient_id="eAB3mDIBBcyUKviyzrxsnAw3",
    hook="order-select",
    context={
        "patientId": "eAB3mDIBBcyUKviyzrxsnAw3",
        "userId": "Practitioner/demo-practitioner",
        "encounterId": "demo-encounter-003",
        "selections": ["MedicationRequest/demo-keytruda-order"],
        "draftOrders": {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "MedicationRequest",
                        "id": "demo-keytruda-order",
                        "status": "draft",
                        "intent": "proposal",
                        "subject": {"reference": "Patient/eAB3mDIBBcyUKviyzrxsnAw3"},
                        "medicationCodeableConcept": {
                            # RxNorm: pembrolizumab 100mg injection (Keytruda)
                            "coding": [
                                {
                                    "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                    "code": "1547545",
                                    "display": "pembrolizumab 100 MG/4ML injection",
                                }
                            ],
                            "text": "Keytruda (pembrolizumab) 200mg IV every 3 weeks",
                        },
                        "dosageInstruction": [
                            {"text": "200mg IV over 30 min every 3 weeks"}
                        ],
                    }
                }
            ],
        },
    },
    prefetch={
        "patient": {
            "resourceType": "Patient",
            "id": "eAB3mDIBBcyUKviyzrxsnAw3",
            "name": [{"use": "official", "family": "Garcia", "given": ["Alex"]}],
            "gender": "male",
            "birthDate": "1958-04-22",
        },
        "medications": {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": 1,
            "entry": [
                {
                    "resource": {
                        "resourceType": "MedicationRequest",
                        "id": "demo-keytruda-order",
                        "status": "draft",
                        "medicationCodeableConcept": {
                            "coding": [
                                {
                                    "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                    "code": "1547545",
                                    "display": "pembrolizumab 100 MG/4ML injection",
                                }
                            ],
                            "text": "Keytruda (pembrolizumab) 200mg IV every 3 weeks",
                        },
                    }
                }
            ],
        },
    },
)

# ---------------------------------------------------------------------------
# Scenario D — Denial Prevention (MRI Lumbar Spine)
# Patient: Alex Garcia (eAB3mDIBBcyUKviyzrxsnAw3) — same as Scenario C
# Clinical context: 2 past Aetna denials "insufficient documentation", 2 doc gaps
# Payer: Aetna
# What CFIP will show (Phase 5): critical denial risk (25%), appeal letter generated
# ---------------------------------------------------------------------------
SCENARIO_D = Scenario(
    id="D",
    name="Denial Prevention — MRI Lumbar Spine",
    description=(
        "MRI Lumbar Spine order for patient with 2 prior Aetna denials "
        "for 'insufficient documentation'. Missing PT records + X-ray. "
        "Expected: critical denial risk card (25% approval), appeal draft generated."
    ),
    patient_id="eAB3mDIBBcyUKviyzrxsnAw3",
    hook="order-select",
    context={
        "patientId": "eAB3mDIBBcyUKviyzrxsnAw3",
        "userId": "Practitioner/demo-practitioner",
        "encounterId": "demo-encounter-004",
        "selections": ["ServiceRequest/demo-mri-order"],
        "draftOrders": {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "ServiceRequest",
                        "id": "demo-mri-order",
                        "status": "draft",
                        "intent": "proposal",
                        "subject": {"reference": "Patient/eAB3mDIBBcyUKviyzrxsnAw3"},
                        # FHIR uses code for procedure orders (not medicationCodeableConcept)
                        "code": {
                            "coding": [
                                {
                                    "system": "http://www.ama-assn.org/go/cpt",
                                    "code": "72148",
                                    "display": "MRI lumbar spine without contrast",
                                }
                            ],
                            "text": "MRI Lumbar Spine without contrast",
                        },
                    }
                }
            ],
        },
    },
    prefetch={
        "patient": {
            "resourceType": "Patient",
            "id": "eAB3mDIBBcyUKviyzrxsnAw3",
            "name": [{"use": "official", "family": "Garcia", "given": ["Alex"]}],
            "gender": "male",
            "birthDate": "1958-04-22",
        },
        # No active medications relevant to this procedure order
        "medications": {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": 0,
            "entry": [],
        },
    },
)

# Registry — used by harness.py to look up scenarios by ID
# Dict comprehension: builds {id: scenario} from a list of scenarios.
# C# analogy: scenarios.ToDictionary(s => s.Id)
ALL_SCENARIOS: dict[str, Scenario] = {
    s.id: s for s in [SCENARIO_A, SCENARIO_B, SCENARIO_C, SCENARIO_D]
}
