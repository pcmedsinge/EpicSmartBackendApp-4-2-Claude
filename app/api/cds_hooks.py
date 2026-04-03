"""
CDS Hooks API endpoints.

Endpoints:
  GET  /cds-services                              — discovery
  POST /cds-services/cfip-order-intelligence      — order-select hook handler
  GET  /cds-services/appeals/{appeal_id}          — retrieve a generated appeal draft

Phase 5: if/else drug-class router replaced by the agentic Orchestrator.
  - Orchestrator.process() selects the evidence chain, runs each step,
    generates an LLM narrative, and composes CDS cards for all 4 scenarios.
  - AppealGenerator produces a PA appeal draft for high-denial-risk orders.
  - Appeal letters are stored in memory (keyed by UUID) and linked from the card.
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from app.agents.orchestrator import Orchestrator
from app.intelligence.appeal_generator import AppealGenerator, should_generate_appeal
from app.models.cds_hooks import (
    Card,
    CdsDiscoveryResponse,
    CdsResponse,
    CdsServiceDefinition,
    CdsSource,
    HookRequest,
    Link,
)
from app.config import get_settings
from app.models.domain import AppealLetter

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
# In-memory appeal store — keyed by UUID string.
# Phase 5: ephemeral (reset on server restart).
# Phase 6: replaced by persistent storage / SMART app delivery.
# C# analogy: a ConcurrentDictionary<string, AppealLetter> scoped to the host.
# ---------------------------------------------------------------------------
_APPEAL_STORE: dict[str, AppealLetter] = {}


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
# Appeal retrieval endpoint — served from the in-memory store
# ---------------------------------------------------------------------------
@router.get("/appeals/{appeal_id}", response_class=PlainTextResponse)
async def get_appeal(appeal_id: str) -> str:
    """
    Return the text of a previously generated appeal letter draft.

    Called when the clinician clicks "View Appeal Draft" on the CDS card.
    Returns plain text so the browser can display or print the letter.

    Phase 6: this endpoint will be replaced by the SMART Companion App.
    """
    letter = _APPEAL_STORE.get(appeal_id)
    if letter is None:
        raise HTTPException(status_code=404, detail="Appeal letter not found")
    return letter.content


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


# ---------------------------------------------------------------------------
# Phase 5 orchestrator handler
# ---------------------------------------------------------------------------

async def _handle_order_intelligence(request: HookRequest) -> CdsResponse:
    """
    Phase 5 handler: delegates to the agentic Orchestrator.

    The Orchestrator replaces the Phase 4 if/elif drug-class router:
      - Selects the evidence chain for the drug class (glp1 / oncology /
        pgx_sensitive / standard)
      - Executes each step (scoring, PA bundle, PGx, NCCN validation…)
      - Generates an LLM narrative via OpenAI (template fallback)
      - Composes CDS Hooks cards via card_composer

    For high-denial-risk orders (score < 50, or moderate with prior denials),
    an appeal letter draft is generated and linked as a second card.

    Always returns a valid CdsResponse — never raises a 500 to Epic.
    C# analogy: thin controller action that dispatches to a MediatR handler.
    """
    orchestrator = Orchestrator()

    try:
        result = await orchestrator.process(request)
    except Exception as exc:
        logger.exception("Orchestrator error — returning empty response: %s", exc)
        return CdsResponse(cards=[])

    # Build final trace — appeal generation happens after the orchestrator, so we append it below
    trace = list(result.evidence_chain_log)

    # Generate appeal letter when denial risk warrants it
    if should_generate_appeal(result) and result.cards:
        try:
            generator = AppealGenerator()
            letter = await generator.generate(result)

            # Persist in the in-memory store and build a card with a "View" link
            appeal_id = _store_appeal(letter)
            appeal_card = _make_appeal_card(letter, appeal_id)

            # Append the appeal card after the primary analysis card
            updated_cards = list(result.cards) + [appeal_card]
            result = result.model_copy(update={"cards": updated_cards})

            model = "GPT-4o-mini" if letter.source == "openai" else "template fallback"
            trace.append(f"AI appeal_letter: Appeal draft written — {model} ({len(letter.content)} chars)")

            logger.info(
                "Appeal letter generated: id=%s drug=%s risk=%s source=%s",
                appeal_id, letter.drug, letter.generated_for_risk_level, letter.source,
            )
        except Exception as exc:
            trace.append(f"ERR appeal_letter: {exc}")
            logger.warning("Appeal generation failed — continuing without appeal: %s", exc)

    return CdsResponse(cards=result.cards, pipeline_trace=trace)


# ---------------------------------------------------------------------------
# Appeal helpers
# ---------------------------------------------------------------------------

def _store_appeal(letter: AppealLetter) -> str:
    """
    Persist the appeal letter in the in-memory store and return its UUID key.

    C# analogy: IAppealRepository.Save(letter) returning the new entity ID.
    """
    appeal_id = str(uuid.uuid4())
    _APPEAL_STORE[appeal_id] = letter
    return appeal_id


def _make_appeal_card(letter: AppealLetter, appeal_id: str) -> Card:
    """
    Build a CDS Hooks card that links to the generated appeal draft.

    The card is "warning" indicator to draw attention without alarming the
    clinician — it is an opportunity, not an error.

    C# analogy: a factory method producing a Card DTO from an AppealLetter.
    """
    payer_note = f" ({letter.payer})" if letter.payer else ""
    reason_readable = letter.denial_reason.replace("_", " ").title()
    source_badge = "_(AI-generated)_" if letter.source == "openai" else "_(template)_"

    return Card(
        summary=f"PA Appeal Draft Ready — {letter.drug}{payer_note}",
        indicator="warning",
        source=CFIP_SOURCE,
        detail=(
            f"### Prior Authorization Appeal Draft {source_badge}\n\n"
            f"**Drug:** {letter.drug}  \n"
            f"**Payer:** {letter.payer or 'Unknown'}  \n"
            f"**Denial reason addressed:** {reason_readable}  \n"
            f"**Risk level:** {letter.generated_for_risk_level}\n\n"
            f"A formal appeal letter has been drafted for your review. "
            f"Click **View Appeal Draft** to open the letter — edit as needed "
            f"before sending to the payer's medical director.\n\n"
            + (
                f"**Evidence cited in draft:** {'; '.join(letter.evidence_references[:3])}\n\n"
                if letter.evidence_references
                else ""
            )
            + "_This is a draft only. Review and sign before submission._"
        ),
        suggestions=[],
        links=[
            Link(
                label="View Appeal Draft",
                url=f"http://localhost:{get_settings().app_port}/cds-services/appeals/{appeal_id}",
                type="absolute",
            )
        ],
    )
