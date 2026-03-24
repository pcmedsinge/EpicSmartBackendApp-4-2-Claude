"""
CDS Hooks API endpoints.

Endpoints:
  GET  /cds-services                              — discovery
  POST /cds-services/cfip-order-intelligence      — order-select hook handler

Phase 2 note: the hook handler returns a STUB card with hardcoded content.
Real denial scoring, PGx, and cost intelligence are added in Phases 3-5.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from app.fhir.client import FhirClient
from app.models.cds_hooks import (
    Card,
    CdsDiscoveryResponse,
    CdsResponse,
    CdsServiceDefinition,
    CdsSource,
    HookRequest,
    Link,
    Suggestion,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# APIRouter — groups all CDS Hooks endpoints under a common prefix.
# Registered in main.py with: app.include_router(router, prefix="/cds-services")
# C# analogy: a controller class with [Route("cds-services")] attribute.
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/cds-services", tags=["CDS Hooks"])

# ---------------------------------------------------------------------------
# Service definition — single source of truth for what we advertise.
# Defined here so discovery and handler stay in sync.
# ---------------------------------------------------------------------------
CFIP_SERVICE = CdsServiceDefinition(
    hook="order-select",
    id="cfip-order-intelligence",
    title="CFIP Order Intelligence",
    description=(
        "Clinical-financial intelligence for medication orders: "
        "denial risk, cost transparency, pharmacogenomics safety, "
        "and prior authorization — at the point of prescribing."
    ),
    prefetch={
        # These FHIR query templates are evaluated by Epic before calling us.
        # {{context.patientId}} is replaced with the actual patient ID.
        # The keys ("patient", "medications") become keys in HookRequest.prefetch.
        "patient": "Patient/{{context.patientId}}",
        "medications": "MedicationRequest?patient={{context.patientId}}&_count=50",
    },
)

# The source attribution shown on every card we return
CFIP_SOURCE = CdsSource(
    label="CFIP — Clinical-Financial Intelligence",
    url="http://localhost:8000",
)


# ---------------------------------------------------------------------------
# D2: Discovery endpoint
# ---------------------------------------------------------------------------
@router.get("", response_model=CdsDiscoveryResponse)
async def discover() -> CdsDiscoveryResponse:
    """
    CDS Hooks discovery endpoint.

    Epic calls this on startup to learn what hooks we handle and what
    prefetch data we need. Must be publicly accessible (no auth required
    per CDS Hooks spec).
    """
    return CdsDiscoveryResponse(services=[CFIP_SERVICE])


# ---------------------------------------------------------------------------
# D3: order-select hook handler
# ---------------------------------------------------------------------------
@router.post("/{service_id}", response_model=CdsResponse)
async def handle_hook(service_id: str, hook_request: HookRequest) -> CdsResponse:
    """
    CDS Hooks handler for all registered services.

    FastAPI automatically:
      - Parses and validates the JSON body into HookRequest
      - Returns 422 Unprocessable Entity if validation fails
      - Serialises our CdsResponse return value to JSON

    C# analogy: [HttpPost("{serviceId}")] action with [FromBody] HookRequest
    and automatic model validation via ModelState.
    """
    logger.info(
        "Hook received: service=%s hook=%s hookInstance=%s",
        service_id,
        hook_request.hook,
        hook_request.hook_instance,
    )

    # Route to the correct handler based on service ID
    if service_id == "cfip-order-intelligence":
        return await _handle_order_intelligence(hook_request)

    # Unknown service — return empty cards (spec-compliant; don't raise 404)
    logger.warning("Unknown service_id: %s", service_id)
    return CdsResponse(cards=[])


async def _handle_order_intelligence(request: HookRequest) -> CdsResponse:
    """
    Handler for the cfip-order-intelligence order-select hook.

    Phase 2: returns a stub card with hardcoded but realistic content.
    Phase 3+: stub values replaced with real denial scoring, cost, PGx.
    """
    # ------------------------------------------------------------------
    # Extract patient ID from context
    # dict.get() returns None if the key is absent — no KeyError.
    # C# analogy: dict.TryGetValue("patientId", out var patientId)
    # ------------------------------------------------------------------
    patient_id: str | None = request.context.get("patientId")
    if not patient_id:
        raise HTTPException(
            status_code=400,
            detail="Hook context missing required field: patientId",
        )

    # ------------------------------------------------------------------
    # Extract medication name — from prefetch if Epic provided it,
    # otherwise fall back to a FHIR call using our Phase 1 client.
    # ------------------------------------------------------------------
    medication_name = _extract_medication_from_prefetch(request)

    if medication_name is None:
        logger.info("Prefetch missing medication — fetching from Epic FHIR")
        medication_name = await _fetch_medication_name(patient_id)

    logger.info("Processing order: patient=%s medication=%s", patient_id, medication_name)

    # ------------------------------------------------------------------
    # Build stub card
    # Phase 2: all values are hardcoded to prove the shape is correct.
    # Phase 3 will replace these with real computed values.
    # ------------------------------------------------------------------
    card = _build_stub_card(medication_name, patient_id)
    return CdsResponse(cards=[card])


def _extract_medication_from_prefetch(request: HookRequest) -> str | None:
    """
    Pull the medication display name out of the prefetch bundle.

    Epic prefetch structure for our "medications" template:
      prefetch.medications = FHIR Bundle with MedicationRequest entries
      Each entry.resource.medicationCodeableConcept.text = drug name

    Returns None if prefetch is absent or doesn't contain a medication name.
    """
    if not request.prefetch:
        return None

    # Try to get the medications bundle
    med_bundle = request.prefetch.get("medications")
    if not med_bundle:
        return None

    # Navigate the nested FHIR structure safely using .get() at each level.
    # This avoids KeyError if any intermediate key is missing.
    entries: list = med_bundle.get("entry", [])
    if not entries:
        return None

    first_resource: dict = entries[0].get("resource", {})
    # MedicationRequest can encode the drug as either:
    #   medicationCodeableConcept.text  (most common in Epic)
    #   medicationCodeableConcept.coding[0].display
    med_concept: dict = first_resource.get("medicationCodeableConcept", {})
    name = med_concept.get("text") or (
        med_concept.get("coding", [{}])[0].get("display")
    )
    return name or None


async def _fetch_medication_name(patient_id: str) -> str:
    """
    Fallback: fetch the most recent active MedicationRequest from Epic
    when prefetch wasn't provided.
    """
    try:
        async with FhirClient() as client:
            # FhirClient doesn't have a get_medications method yet —
            # that's added in Phase 3. For now, return a placeholder.
            # TODO Phase 3: implement client.get_medication_requests()
            logger.info("Medication fetch fallback — placeholder until Phase 3")
            return "Unknown medication"
    except Exception as e:
        logger.warning("Medication fetch failed: %s", e)
        return "Unknown medication"


def _build_stub_card(medication_name: str, patient_id: str) -> Card:
    """
    Build a spec-compliant CDS card with stub (hardcoded) intelligence values.

    Phase 2 goal: prove the card shape is correct and renders in the harness.
    Phase 3: replace hardcoded values with real denial scoring + cost data.
    Phase 4: add real PGx results.
    """
    return Card(
        # summary must be ≤140 chars — Pydantic enforces this via max_length
        summary=f"{medication_name}: 87% approval | $150/mo | No PGx issues",
        indicator="info",
        source=CFIP_SOURCE,
        # detail supports markdown — Epic renders it in an expandable section
        detail=(
            f"**Prior Authorization Assessment** (stub — Phase 2)\n\n"
            f"- **Denial risk:** 13% (87% approval probability)\n"
            f"- **Step therapy:** Metformin trial documented (6 months) ✓\n"
            f"- **Estimated cost:** $150/mo patient copay (UHC Tier 3)\n"
            f"- **Pharmacogenomics:** No CYP interactions identified ✓\n\n"
            f"*Real intelligence from Phase 3+ — this is a stub card.*"
        ),
        suggestions=[
            Suggestion(
                label="Submit Prior Authorization",
                isRecommended=True,
                # Actions will be wired to real PA submission in Phase 5
            ),
        ],
        links=[
            Link(
                label="View Full Analysis",
                # In Phase 6 this becomes a real SMART launch URL with patient context
                url=f"http://localhost:8000/companion?patient={patient_id}",
                type="absolute",
            ),
        ],
        selectionBehavior="at-most-one",
    )
