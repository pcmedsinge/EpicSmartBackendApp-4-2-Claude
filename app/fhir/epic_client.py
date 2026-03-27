"""
EpicFHIRClient — high-level FHIR client for the Phase 6 orchestrator.

Fetches all 5 resource types for a patient in parallel, returns a FhirDataBundle.
Never raises — errors are captured in FhirDataBundle.fetch_errors and the
orchestrator falls back to synthetic overlay for any gaps.

Relationship to app/fhir/client.py:
  client.py  — targeted lookups by LOINC / RxNorm code (used in Phase 1 verify script)
  epic_client.py — broad "give me everything" fetch for the CDS orchestrator pipeline

C# analogy: this is the IEpicFhirClient service registered in DI, used by the
MediatR handler that processes each CDS hook.  client.py is like a lower-level
repository used by scripts and unit tests.
"""

import asyncio
import logging

import httpx

from app.config import get_settings
from app.fhir.auth import get_access_token
from app.models.fhir_bundle import FhirDataBundle

logger = logging.getLogger(__name__)

# How many FHIR resources to request per search — high enough to cover sandbox data,
# low enough to keep response times fast.
_DEFAULT_COUNT = "50"


class EpicFHIRClient:
    """
    Async FHIR client that fetches a complete patient bundle in one parallel round-trip.

    Usage:
        client = EpicFHIRClient()
        bundle = await client.fetch_patient_bundle("erXuFYUfucBZaryVksYEcMg3")
        if bundle.has_real_data:
            # use bundle.medications, bundle.lab_observations, etc.

    The client is stateless — create a new instance per orchestrator call.
    Token caching is handled inside app.fhir.auth (module-level singleton).
    C# analogy: a transient-scoped service — new instance per request, but
    the IMemoryCache (token cache) is singleton underneath.
    """

    async def fetch_patient_bundle(self, patient_id: str) -> FhirDataBundle:
        """
        Fetch all relevant FHIR resources for a patient.

        Runs 5 FHIR calls in parallel using asyncio.gather():
          Patient, MedicationRequest, Observation (labs), Condition, Coverage

        Never raises — all errors are captured in FhirDataBundle.fetch_errors.
        A failed resource type gets an empty list; synthetic overlay fills the gap.

        C# analogy: Task.WhenAll(patientTask, medsTask, ...) inside a try/catch.
        """
        settings = get_settings()

        # --- Step 1: Authenticate (uses cached token if still valid) ---
        try:
            token = await get_access_token()
        except Exception as exc:
            # Auth failure = can't reach Epic at all.
            # Return an empty bundle — orchestrator will use 100% synthetic overlay.
            logger.warning(
                "Epic auth failed for patient %s — falling back to synthetic: %s",
                patient_id,
                exc,
            )
            return FhirDataBundle(
                fetch_errors=[f"auth_failed: {exc}"],
                fetched_from="prefetch_only",
            )

        # --- Step 2: Build shared httpx client for all parallel requests ---
        # One AsyncClient = one connection pool reused across all 5 fetches.
        # C# analogy: one IHttpClientFactory-managed HttpClient for the request scope.
        auth_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/fhir+json",
        }

        async with httpx.AsyncClient(
            base_url=settings.epic_fhir_base_url,
            headers=auth_headers,
            timeout=30.0,
        ) as http:

            # --- Step 3: Fire all 5 fetches concurrently ---
            # asyncio.gather() starts all coroutines simultaneously and waits for
            # all to finish.  return_exceptions=True means each failed coroutine
            # returns its Exception object instead of propagating — we handle errors
            # per-resource below.
            # C# analogy: await Task.WhenAll(...) — exceptions don't cancel siblings.
            results = await asyncio.gather(
                self._fetch_patient(http, patient_id),
                self._fetch_medications(http, patient_id),
                self._fetch_lab_observations(http, patient_id),
                self._fetch_conditions(http, patient_id),
                self._fetch_coverage(http, patient_id),
                return_exceptions=True,
            )

        # --- Step 4: Unpack results, separating data from errors ---
        patient_result, meds_result, labs_result, conditions_result, coverage_result = results

        errors: list[str] = []

        patient = _unwrap(patient_result, "Patient", errors)
        medications = _unwrap(meds_result, "MedicationRequest", errors) or []
        lab_observations = _unwrap(labs_result, "Observation", errors) or []
        conditions = _unwrap(conditions_result, "Condition", errors) or []
        coverage = _unwrap(coverage_result, "Coverage", errors) or []

        # fetched_from reflects whether we got the core Patient record.
        # If Patient is None (fetch failed or 404), we treat it as prefetch_only
        # so the orchestrator knows real demographics are unavailable.
        fetched_from = "epic_fhir" if patient is not None else "prefetch_only"

        if errors:
            logger.warning(
                "FHIR partial fetch for patient %s (%d error(s)): %s",
                patient_id,
                len(errors),
                "; ".join(errors),
            )
        else:
            logger.info(
                "FHIR bundle fetched: patient=%s meds=%d labs=%d conditions=%d coverage=%d",
                patient_id,
                len(medications),
                len(lab_observations),
                len(conditions),
                len(coverage),
            )

        return FhirDataBundle(
            patient=patient,
            medications=medications,
            lab_observations=lab_observations,
            conditions=conditions,
            coverage=coverage,
            fetch_errors=errors,
            fetched_from=fetched_from,
        )

    # -------------------------------------------------------------------------
    # Private fetch helpers — one per FHIR resource type
    # Each returns the parsed data or raises on non-404 HTTP errors.
    # 404 is treated as "not in sandbox" and returns empty — not an error.
    # -------------------------------------------------------------------------

    async def _fetch_patient(self, http: httpx.AsyncClient, patient_id: str) -> dict | None:
        """
        GET /Patient/{id} — returns the raw Patient resource dict, or None if not found.
        """
        response = await http.get(f"/Patient/{patient_id}")
        if response.status_code == 404:
            logger.info("Patient %s not found in Epic sandbox (404)", patient_id)
            return None
        response.raise_for_status()
        return response.json()

    async def _fetch_medications(
        self,
        http: httpx.AsyncClient,
        patient_id: str,
    ) -> list[dict]:
        """
        GET /MedicationRequest?patient={id} — returns all medication orders.

        No RxNorm filter here — we want the full medication history so the
        orchestrator can check step therapy, prior drug classes, etc.
        """
        response = await http.get(
            "/MedicationRequest",
            params={
                "patient": patient_id,
                "_sort": "-authoredon",   # most recent first
                "_count": _DEFAULT_COUNT,
            },
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return _extract_resources(response.json(), "MedicationRequest")

    async def _fetch_lab_observations(
        self,
        http: httpx.AsyncClient,
        patient_id: str,
    ) -> list[dict]:
        """
        GET /Observation?patient={id}&category=laboratory — returns lab results.

        Fetches all lab observations, not just a specific LOINC code.
        The orchestrator extracts A1C, BMI, PD-L1 etc. by code from this list.
        """
        response = await http.get(
            "/Observation",
            params={
                "patient": patient_id,
                "category": "laboratory",
                "_sort": "-date",
                "_count": _DEFAULT_COUNT,
            },
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return _extract_resources(response.json(), "Observation")

    async def _fetch_conditions(
        self,
        http: httpx.AsyncClient,
        patient_id: str,
    ) -> list[dict]:
        """
        GET /Condition?patient={id} — returns diagnoses.

        Used to verify tumor type for oncology scenarios, chronic conditions
        for GLP-1 step therapy, etc.
        """
        response = await http.get(
            "/Condition",
            params={
                "patient": patient_id,
                "_count": _DEFAULT_COUNT,
            },
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return _extract_resources(response.json(), "Condition")

    async def _fetch_coverage(
        self,
        http: httpx.AsyncClient,
        patient_id: str,
    ) -> list[dict]:
        """
        GET /Coverage?patient={id}&status=active — returns active insurance.

        Epic public sandbox typically returns no Coverage resources — that's expected.
        Synthetic overlay provides payer info in that case.
        """
        response = await http.get(
            "/Coverage",
            params={
                "patient": patient_id,
                "status": "active",
                "_count": "10",
            },
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return _extract_resources(response.json(), "Coverage")


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _extract_resources(bundle: dict, resource_type: str) -> list[dict]:
    """
    Pull individual resource dicts out of a FHIR Bundle response.

    A FHIR Bundle search result looks like:
      { "resourceType": "Bundle", "entry": [{ "resource": {...} }, ...] }

    We extract only the inner resource dicts, filtered to the expected type.
    C# analogy: bundle.Entry.Select(e => e.Resource).OfType<TResource>().ToList()
    """
    # bundle.get("entry") can return None for an empty bundle — `or []` handles that.
    entries: list[dict] = bundle.get("entry") or []
    return [
        entry["resource"]
        for entry in entries
        if entry.get("resource", {}).get("resourceType") == resource_type
    ]


def _unwrap(result: object, label: str, errors: list[str]) -> object | None:
    """
    If result is an Exception, record it in errors and return None.
    Otherwise return the result unchanged.

    Used after asyncio.gather(return_exceptions=True) to separate
    successful results from failures without re-raising.
    """
    if isinstance(result, Exception):
        msg = f"{label}: {result}"
        errors.append(msg)
        logger.warning("FHIR fetch error — %s", msg)
        return None
    return result
