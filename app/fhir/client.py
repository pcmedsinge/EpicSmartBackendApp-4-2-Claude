"""
Epic FHIR R4 client — authenticated httpx-based client for all FHIR calls.

Design:
  - FhirClient wraps an httpx.AsyncClient (connection pool reuse)
  - Used as an async context manager: async with FhirClient() as client: ...
  - Calls auth.get_access_token() before every request (cached internally)
  - Returns typed Pydantic models, never raw dicts

C# analogy: a typed HttpClient wrapper (like Refit or a custom IFhirClient service)
registered as scoped/singleton in DI.
"""

import logging
from datetime import date, datetime, timezone

import httpx

from app.config import get_settings
from app.fhir.auth import get_access_token
from app.models.fhir_types import Bundle, Coverage, Patient

logger = logging.getLogger(__name__)


class FhirClient:
    """
    Async FHIR R4 client for Epic sandbox.

    Usage:
        async with FhirClient() as client:
            patient = await client.get_patient("abc123")
            coverages = await client.get_coverage("abc123")

    The `async with` block ensures the underlying httpx connection pool
    is properly opened on entry and closed on exit — even if an exception occurs.
    C# analogy: using (var client = new HttpClient()) { ... }
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        # _http is None until __aenter__ is called — the connection pool
        # doesn't open until we enter the `async with` block.
        self._http: httpx.AsyncClient | None = None

    # -------------------------------------------------------------------------
    # Async context manager protocol
    #
    # Python's context manager protocol requires two methods:
    #   __aenter__: called when entering `async with` — sets up resources
    #   __aexit__:  called when leaving `async with` — tears down resources
    #
    # The double-underscore prefix ("dunder") means these are special Python
    # protocol methods. C# analogy: IAsyncDisposable with await using (...).
    # -------------------------------------------------------------------------
    async def __aenter__(self) -> "FhirClient":
        self._http = httpx.AsyncClient(
            base_url=self._settings.epic_fhir_base_url,
            # Default headers sent on every request
            headers={
                "Accept": "application/fhir+json",
                "Content-Type": "application/fhir+json",
            },
            timeout=30.0,
        )
        return self  # returning self allows: `async with FhirClient() as client:`

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        # aclose() drains and closes all connections in the pool.
        # The three parameters carry exception info if the block raised —
        # we don't suppress exceptions here, just clean up.
        if self._http:
            await self._http.aclose()
        # Returning None (implicit) means we don't suppress any exception.

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------
    async def _auth_headers(self) -> dict[str, str]:
        """Build Authorization header with a fresh (or cached) bearer token."""
        token = await get_access_token()
        # f-string: f"Bearer {token}" — C# analogy: $"Bearer {token}"
        return {"Authorization": f"Bearer {token}"}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        """
        Perform an authenticated GET request and return the parsed JSON body.

        :param path: URL path relative to FHIR base, e.g. "/Patient/abc123"
        :param params: optional query parameters dict, e.g. {"status": "active"}
        """
        if self._http is None:
            raise RuntimeError("FhirClient must be used as an async context manager")

        headers = await self._auth_headers()
        logger.debug("FHIR GET %s params=%s", path, params)

        response = await self._http.get(path, headers=headers, params=params)

        if response.status_code != 200:
            logger.error(
                "FHIR GET %s failed: HTTP %s — %s",
                path,
                response.status_code,
                response.text[:500],  # truncate long error bodies
            )
            response.raise_for_status()

        return response.json()

    # -------------------------------------------------------------------------
    # Public FHIR operations
    # -------------------------------------------------------------------------
    async def get_patient(self, patient_id: str) -> Patient:
        """
        Read a single Patient resource by ID.
        Epic endpoint: GET /Patient/{id}
        """
        raw = await self._get(f"/Patient/{patient_id}")
        # model_validate() parses a dict into the Pydantic model, running all
        # validators. C# analogy: JsonSerializer.Deserialize<Patient>(json)
        patient = Patient.model_validate(raw)
        logger.info(
            "Fetched Patient: id=%s name=%s dob=%s gender=%s",
            patient.id,
            patient.display_name,
            patient.birth_date,
            patient.gender,
        )
        return patient

    async def get_coverage(self, patient_id: str) -> list[Coverage]:
        """
        Search for active Coverage resources for a patient.
        Epic endpoint: GET /Coverage?patient={id}&status=active

        Returns a list because a patient can have multiple active coverages
        (e.g. primary + secondary insurance).
        """
        raw = await self._get(
            "/Coverage",
            params={"patient": patient_id, "status": "active"},
        )

        bundle = Bundle.model_validate(raw)

        # Parse each raw resource dict in the bundle into a Coverage model.
        # List comprehension with type filtering — we skip any entry that isn't
        # a Coverage resource (defensive, shouldn't happen in practice).
        coverages = [
            Coverage.model_validate(r)
            for r in bundle.resources()
            if r.get("resourceType") == "Coverage"
        ]

        logger.info(
            "Fetched %d Coverage(s) for patient %s: %s",
            len(coverages),
            patient_id,
            [c.payor_name for c in coverages],
        )
        return coverages

    async def get_observations(
        self,
        patient_id: str,
        loinc_code: str,
    ) -> list[dict]:
        """
        Search for Observation resources by LOINC code, most recent first.
        Returns a list of raw resource dicts — the bridge extracts what it needs.

        Epic endpoint: GET /Observation?patient={id}&code={loinc}&_sort=-date&_count=5

        Common LOINC codes used in CFIP:
          4548-4   — HbA1c (%)
          39156-5  — BMI (kg/m²)
        """
        raw = await self._get(
            "/Observation",
            params={
                "patient": patient_id,
                "code": loinc_code,
                "_sort": "-date",   # most recent first
                "_count": "5",      # we only need the latest, but fetch a few for resilience
            },
        )
        bundle = Bundle.model_validate(raw)
        resources = [r for r in bundle.resources() if r.get("resourceType") == "Observation"]
        logger.info(
            "Fetched %d Observation(s) for patient %s, LOINC %s",
            len(resources),
            patient_id,
            loinc_code,
        )
        return resources

    async def get_medication_requests(
        self,
        patient_id: str,
        rxnorm_codes: list[str],
    ) -> list[dict]:
        """
        Search for MedicationRequest resources matching any of the given RxNorm codes.
        Returns a list of raw resource dicts — the bridge calculates days from authoredOn.

        Epic endpoint: GET /MedicationRequest?patient={id}&code={codes}

        Common RxNorm codes used in CFIP:
          6809  — metformin (ingredient-level code)
        """
        # Epic accepts multiple codes as a comma-separated string
        # C# analogy: string.Join(",", rxnormCodes)
        code_param = ",".join(rxnorm_codes)

        raw = await self._get(
            "/MedicationRequest",
            params={
                "patient": patient_id,
                "code": code_param,
                "_sort": "-authoredon",  # most recent first
                "_count": "10",
            },
        )
        bundle = Bundle.model_validate(raw)
        resources = [
            r for r in bundle.resources()
            if r.get("resourceType") == "MedicationRequest"
        ]
        logger.info(
            "Fetched %d MedicationRequest(s) for patient %s, codes %s",
            len(resources),
            patient_id,
            code_param,
        )
        return resources

    async def get_genomic_observations(self, patient_id: str) -> list[dict]:
        """
        Fetch genomic Observations for a patient (PGx gene results).

        Epic endpoint: GET /Observation?patient={id}&category=genomics

        Genomic observations in FHIR R4 encode PGx results as Observations
        with category=genomics. The gene name is in the code or component,
        and the diplotype is in valueString or valueCodeableConcept.

        Returns raw resource dicts — the PGx agent parses gene/diplotype from them.
        Returns an empty list if no genomic data exists (common — most patients
        haven't had PGx testing).
        """
        try:
            raw = await self._get(
                "/Observation",
                params={
                    "patient": patient_id,
                    "category": "genomics",
                    "_count": "50",
                },
            )
            bundle = Bundle.model_validate(raw)
            resources = [
                r for r in bundle.resources()
                if r.get("resourceType") == "Observation"
            ]
            logger.info(
                "Fetched %d genomic Observation(s) for patient %s",
                len(resources),
                patient_id,
            )
            return resources
        except Exception as exc:
            # Genomic observations are often absent — treat fetch failure as empty,
            # not as a pipeline error. The PGx agent will fall back to synthetic data.
            logger.warning("Genomic observation fetch failed for patient %s: %s", patient_id, exc)
            return []


# ---------------------------------------------------------------------------
# FHIR date parsing helpers — used by the bridge to calculate "days old"
# ---------------------------------------------------------------------------

def parse_fhir_date(date_str: str | None) -> date | None:
    """
    Parse a FHIR date or dateTime string into a Python date.

    FHIR supports several formats:
      "2024-03-15"                  — date only
      "2024-03-15T10:30:00+00:00"  — full dateTime with timezone
      "2024-03-15T10:30:00Z"       — UTC shorthand

    Returns None if the string is missing or unparseable.
    C# analogy: DateTime.TryParse() returning a nullable DateTime.
    """
    if not date_str:
        return None
    try:
        # Try date-only first (most common for lab results)
        return date.fromisoformat(date_str[:10])
    except ValueError:
        return None


def days_since(observation_date: date | None) -> int | None:
    """
    Return the number of days between an observation date and today.
    Returns None if observation_date is None.
    """
    if observation_date is None:
        return None
    # date.today() returns the current local date (no time component)
    return (date.today() - observation_date).days
