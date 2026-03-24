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
