"""
Demo scenario definitions for the CDS Hooks test harness.

Each scenario captures everything needed to fire a realistic hook request
at CFIP without requiring Epic Hyperspace — patient context, medication
being ordered, and pre-built prefetch data.

Phase 2: Scenario A only (Ozempic / GLP-1).
Phase 3+: Scenarios B (PGx clopidogrel), C (Keytruda), D (denial prevention).
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
# Scenarios B, C, D — stubs for Phase 4+
# Defined here so harness.py can reference them without crashing
# ---------------------------------------------------------------------------
SCENARIO_B = Scenario(
    id="B",
    name="PGx Safety Alert — Clopidogrel",
    description="CYP2C19 poor metabolizer prescribed clopidogrel. Phase 4.",
    patient_id="erXuFYUfucBZaryVksYEcMg3",
    hook="order-select",
    context={"patientId": "erXuFYUfucBZaryVksYEcMg3"},
)

SCENARIO_C = Scenario(
    id="C",
    name="Oncology PA — Keytruda",
    description="PD-L1+ lung cancer patient requesting pembrolizumab. Phase 4.",
    patient_id="erXuFYUfucBZaryVksYEcMg3",
    hook="order-select",
    context={"patientId": "erXuFYUfucBZaryVksYEcMg3"},
)

SCENARIO_D = Scenario(
    id="D",
    name="Denial Prevention — MRI",
    description="Routine MRI with history of Aetna denials. Phase 3.",
    patient_id="erXuFYUfucBZaryVksYEcMg3",
    hook="order-select",
    context={"patientId": "erXuFYUfucBZaryVksYEcMg3"},
)

# Registry — used by harness.py to look up scenarios by ID
# Dict comprehension: builds {id: scenario} from a list of scenarios.
# C# analogy: scenarios.ToDictionary(s => s.Id)
ALL_SCENARIOS: dict[str, Scenario] = {
    s.id: s for s in [SCENARIO_A, SCENARIO_B, SCENARIO_C, SCENARIO_D]
}
