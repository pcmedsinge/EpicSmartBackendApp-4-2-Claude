"""
FhirDataBundle — typed container for all FHIR resources fetched for one patient.

This dataclass is the output of EpicFHIRClient.fetch_patient_bundle().
The orchestrator reads it instead of prefetch to decide what's real vs synthetic.

Why raw dicts instead of Pydantic models?
  The synthetic overlay and evidence chains work field-by-field on raw FHIR JSON.
  Keeping them as dicts avoids double-parsing and lets the overlay inspect any field
  without a pre-defined schema.  The typed Pydantic models in fhir_types.py are still
  used in Phase 1 / targeted FHIR lookups where we know exactly which fields we need.
"""

from dataclasses import dataclass, field


@dataclass
class FhirDataBundle:
    """
    All FHIR data fetched for one patient in a single orchestrator request.

    Fields are empty (None / []) if the resource type was not found or the
    fetch failed.  Errors are collected in fetch_errors rather than raised —
    the orchestrator uses synthetic overlay to cover any gaps.

    C# analogy: a DTO / record with nullable fields, like:
        public record FhirDataBundle(
            JsonDocument? Patient,
            IReadOnlyList<JsonDocument> Medications,
            ...
        )
    """

    # Patient demographics — None if fetch failed or patient not in Epic sandbox
    patient: dict | None = field(default=None)

    # MedicationRequest resources — all active + historical orders
    medications: list[dict] = field(default_factory=list)

    # Observation resources with category=laboratory (A1C, BMI, CBC, etc.)
    lab_observations: list[dict] = field(default_factory=list)

    # Condition resources — diagnoses (ICD-10 codes, tumor types, chronic conditions)
    conditions: list[dict] = field(default_factory=list)

    # Coverage resources — active insurance / payer information
    coverage: list[dict] = field(default_factory=list)

    # Per-resource error messages — populated when a fetch returned an error.
    # Non-empty means some fields will be covered by synthetic overlay.
    # default_factory=list ensures each instance gets its own list, not a shared one —
    # a common Python gotcha when using mutable defaults.
    fetch_errors: list[str] = field(default_factory=list)

    # Where this data came from:
    #   "epic_fhir"     — at least the Patient record was fetched from Epic FHIR API
    #   "prefetch_only" — Epic auth failed or patient not found; data is all synthetic
    fetched_from: str = field(default="prefetch_only")

    # -------------------------------------------------------------------------
    # Convenience helpers — used by orchestrator + synthetic overlay
    # -------------------------------------------------------------------------

    @property
    def has_real_data(self) -> bool:
        """True if at least the Patient resource was fetched from Epic."""
        return self.fetched_from == "epic_fhir" and self.patient is not None

    @property
    def error_count(self) -> int:
        """Number of resource types that failed to fetch."""
        return len(self.fetch_errors)

    def patient_name(self) -> str | None:
        """
        Extract the patient's display name from the Patient resource.

        FHIR Patient.name is a list of HumanName objects.  We pick the first
        'official' use name, falling back to the first name in the list.
        Returns None if no patient data was fetched.
        """
        if not self.patient:
            return None

        names: list[dict] = self.patient.get("name", [])
        if not names:
            return None

        # Prefer name with use="official"; fall back to first entry
        official = next((n for n in names if n.get("use") == "official"), names[0])

        # HumanName.text is a pre-formatted string — use it if available
        if official.get("text"):
            return official["text"]

        # Otherwise construct from family + given parts
        family = official.get("family", "")
        given = " ".join(official.get("given", []))
        return f"{given} {family}".strip() or None
