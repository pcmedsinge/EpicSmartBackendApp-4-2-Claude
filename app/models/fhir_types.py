"""
Thin Pydantic models for FHIR R4 resources used in Phase 1.

Design principle: we only model fields CFIP actually uses — not the full FHIR spec.
Extra fields in the Epic response are silently ignored (model_config extra="ignore").

These models sit between the raw Epic JSON and the rest of the app.
C# analogy: DTO classes with [JsonPropertyName] attributes, but validation is built in.
"""

from datetime import date
from typing import Any

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Shared base — all our FHIR models inherit from this.
# extra="ignore" means Pydantic won't raise an error when Epic sends fields
# we haven't declared. FHIR resources are large — we only want what we need.
# ---------------------------------------------------------------------------
class FhirBase(BaseModel):
    model_config = {"extra": "ignore"}


# ---------------------------------------------------------------------------
# Patient
# ---------------------------------------------------------------------------
class FhirHumanName(FhirBase):
    """One entry in a Patient.name list."""
    use: str | None = None          # "official", "usual", "nickname", etc.
    family: str | None = None       # last name
    # list[str] — a list of first/middle name strings
    given: list[str] = Field(default_factory=list)
    # default_factory=list means each instance gets its OWN empty list.
    # Never use given: list[str] = [] — that shares one list across all instances.
    # C# analogy: = new List<string>() in the constructor, not as a field initializer.

    @property
    def full_name(self) -> str:
        """Combine given names + family into a single display string."""
        parts = self.given + ([self.family] if self.family else [])
        # " ".join(parts) concatenates a list of strings with a space separator.
        # C# analogy: string.Join(" ", parts)
        return " ".join(parts).strip() or "Unknown"


class Patient(FhirBase):
    """
    FHIR R4 Patient resource — only fields CFIP needs.
    Full spec: https://hl7.org/fhir/R4/patient.html
    """
    id: str
    gender: str | None = None
    birth_date: date | None = Field(None, alias="birthDate")
    # alias="birthDate" tells Pydantic that the JSON key is "birthDate" (camelCase)
    # but the Python attribute is birth_date (snake_case).
    # C# analogy: [JsonPropertyName("birthDate")]

    # Raw name list from FHIR — we process this into display_name below
    _raw_names: list[FhirHumanName] = []

    # Computed display name — populated by the model_validator
    display_name: str = "Unknown"

    @model_validator(mode="before")
    @classmethod
    def extract_display_name(cls, data: Any) -> Any:
        """
        Pull the best available name from the FHIR name list before validation.

        @model_validator(mode="before") runs on the raw input dict BEFORE Pydantic
        tries to coerce types. We use it here to flatten the nested name list
        into a simple string.
        C# analogy: a custom JsonConverter.Read() that pre-processes the JSON token.

        @classmethod means the method receives the class (cls) as first argument
        instead of an instance (self) — needed because the object doesn't exist yet.
        C# analogy: a static method.
        """
        if not isinstance(data, dict):
            return data

        names = data.get("name", [])
        if not names:
            return data

        # Parse the raw dicts into FhirHumanName objects
        parsed = [FhirHumanName.model_validate(n) for n in names]

        # Prefer "official" name, fall back to first available
        # next() returns the first item matching the condition, or the default.
        # C# analogy: names.FirstOrDefault(n => n.use == "official") ?? names.First()
        best = next((n for n in parsed if n.use == "official"), parsed[0])
        data["display_name"] = best.full_name
        return data


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------
class FhirReference(FhirBase):
    """A FHIR Reference type — a pointer to another resource."""
    reference: str | None = None    # e.g. "Organization/abc123"
    display: str | None = None      # human-readable name, e.g. "UnitedHealthcare"


class FhirPeriod(FhirBase):
    """A FHIR Period — a start/end date range."""
    start: date | None = None
    end: date | None = None


class Coverage(FhirBase):
    """
    FHIR R4 Coverage resource — insurance plan information.
    Full spec: https://hl7.org/fhir/R4/coverage.html
    """
    id: str
    status: str | None = None       # "active", "cancelled", "draft", "entered-in-error"
    period: FhirPeriod | None = None

    # payor is a list in FHIR (a patient can have multiple payors on one Coverage)
    # We'll extract the first one's display name for Phase 1.
    payor: list[FhirReference] = Field(default_factory=list)

    # Flattened display fields — populated by model_validator
    payor_name: str = "Unknown"
    payor_reference: str | None = None

    @model_validator(mode="before")
    @classmethod
    def extract_payor_info(cls, data: Any) -> Any:
        """Flatten the first payor reference into display fields."""
        if not isinstance(data, dict):
            return data

        payors = data.get("payor", [])
        if payors:
            first = payors[0]
            data["payor_name"] = first.get("display", "Unknown")
            data["payor_reference"] = first.get("reference")
        return data


# ---------------------------------------------------------------------------
# FHIR Bundle wrapper
# A FHIR search result is always wrapped in a Bundle with an "entry" list.
# Each entry has a "resource" field containing the actual resource.
# ---------------------------------------------------------------------------
class BundleEntry(FhirBase):
    resource: dict = Field(default_factory=dict)


class Bundle(FhirBase):
    """
    Generic FHIR R4 Bundle — used for search results.
    We keep resource as raw dict here and let the caller cast it to the
    correct model type (Patient, Coverage, etc.).
    """
    resourceType: str = "Bundle"
    total: int = 0
    entry: list[BundleEntry] = Field(default_factory=list)

    def resources(self) -> list[dict]:
        """Extract the raw resource dicts from bundle entries."""
        # List comprehension — build a new list by transforming each item.
        # C# analogy: entries.Select(e => e.resource).ToList()
        return [e.resource for e in self.entry]
