"""
Pydantic models for the CDS Hooks 2.0 specification.

Spec reference: https://cds-hooks.hl7.org/2.0/

These models are the contract between:
  - Epic (sends HookRequest, expects CdsResponse)
  - Our CDS engine (receives HookRequest, builds CdsResponse)
  - Our test harness (constructs HookRequest, validates CdsResponse)

Nothing here contains business logic — pure data shapes.
"""

import uuid as uuid_lib
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------
class CdsBase(BaseModel):
    model_config = {"extra": "ignore"}  # tolerate unknown fields from Epic


# ---------------------------------------------------------------------------
# Inbound — what Epic sends us
# ---------------------------------------------------------------------------
class FhirAuthorization(CdsBase):
    """
    OAuth token Epic includes so we can make FHIR calls on its behalf.
    Present only when fhirServer is also provided.
    """
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str
    subject: str


class HookRequest(CdsBase):
    """
    The full body of a CDS Hooks POST request from Epic.

    Fields:
      hook          — which hook fired, e.g. "order-select"
      hookInstance  — unique UUID for this specific firing (for logging/dedup)
      context       — hook-specific data (patientId, selections, etc.)
                      typed as dict[str, Any] because structure varies per hook
      prefetch      — FHIR resources Epic pre-fetched per our discovery template
                      keyed by the names we declared (e.g. "patient", "medications")
      fhirServer    — base URL of Epic's FHIR server (optional)
      fhirAuthorization — token to call fhirServer (optional)

    C# analogy: a [FromBody] request DTO with nullable fields.
    """
    hook: str
    hook_instance: str = Field(alias="hookInstance")
    # alias="hookInstance" maps the camelCase JSON key to our snake_case attribute.
    # We set populate_by_name=True below so tests can use either name.
    context: dict[str, Any] = Field(default_factory=dict)
    prefetch: dict[str, Any] | None = None
    fhir_server: str | None = Field(None, alias="fhirServer")
    fhir_authorization: FhirAuthorization | None = Field(None, alias="fhirAuthorization")

    model_config = {
        "extra": "ignore",
        # populate_by_name=True lets us construct the model using either the
        # alias ("hookInstance") or the Python name ("hook_instance").
        # Useful in tests where we write Python dicts, not JSON.
        "populate_by_name": True,
    }


# ---------------------------------------------------------------------------
# Outbound — what we send back to Epic
# ---------------------------------------------------------------------------
class CdsSource(CdsBase):
    """
    Source attribution shown on the card — tells the clinician where this
    information comes from.
    """
    label: str
    url: str | None = None
    icon: str | None = None     # URL to a small icon image (optional)


class Action(CdsBase):
    """
    A single action within a Suggestion — what happens when the clinician
    clicks the suggestion button.

    type:
      "create"  — add a new FHIR resource
      "update"  — modify an existing FHIR resource
      "delete"  — remove a FHIR resource
    """
    type: Literal["create", "update", "delete"]
    description: str
    resource: dict[str, Any] | None = None  # FHIR resource payload


class Suggestion(CdsBase):
    """
    An actionable button on the card — e.g. "Submit PA Now", "Switch to prasugrel".
    The clinician can accept or ignore suggestions.
    """
    label: str
    # uuid is auto-generated if not provided.
    # default_factory=... calls the function each time a new instance is created.
    # C# analogy: = Guid.NewGuid() in the constructor.
    uuid: str = Field(default_factory=lambda: str(uuid_lib.uuid4()))
    isRecommended: bool = False     # spec uses camelCase for this field
    actions: list[Action] = Field(default_factory=list)


class Link(CdsBase):
    """
    A URL attached to the card.

    type:
      "absolute" — opens a regular URL in a new tab
      "smart"    — launches a SMART on FHIR app with EHR context
                   (used for our SMART Companion App in Phase 6)
    """
    label: str
    url: str
    type: Literal["absolute", "smart"]
    appContext: str | None = None   # passed to SMART app as launch context


class Card(CdsBase):
    """
    A CDS card rendered by Epic in the clinician's workflow.

    indicator controls the visual treatment:
      "info"     — blue  — informational, no urgency
      "warning"  — orange — something to consider
      "critical" — red   — requires attention before proceeding

    summary is the headline (≤140 chars per spec).
    detail is optional markdown rendered in an expandable section.

    C# analogy: a strongly-typed response DTO with validation attributes.
    """
    summary: str = Field(..., max_length=140)
    indicator: Literal["info", "warning", "critical"] = "info"
    source: CdsSource
    detail: str | None = None           # markdown supported
    suggestions: list[Suggestion] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    # selectionBehavior controls whether suggestions are mutually exclusive.
    # "at-most-one" means clinician can accept zero or one suggestion.
    # Only required when suggestions are present — None means no constraint.
    selectionBehavior: Literal["at-most-one"] | None = None


class CdsResponse(CdsBase):
    """
    The full response body we return to Epic.
    A response can contain multiple cards — each addressing a different concern.
    An empty cards list is valid and means "no alerts for this order".
    """
    cards: list[Card] = Field(default_factory=list)

    # systemActions are top-level actions (not attached to a card) that the
    # EHR should perform automatically. Unused in Phase 2 but modelled for completeness.
    systemActions: list[Action] = Field(default_factory=list)

    # CDS Hooks spec allows extension fields — Epic ignores unknown keys.
    # The harness uses this to display the agent reasoning trace in
    # "View Full Analysis" without a separate endpoint.
    agent_trace: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Discovery — what GET /cds-services returns
# ---------------------------------------------------------------------------
class CdsServiceDefinition(CdsBase):
    """
    One entry in the discovery response — describes a single CDS service we offer.

    prefetch declares FHIR queries using template variables.
    Epic evaluates these and includes the results in hook requests.
    {{context.patientId}} is replaced by Epic with the actual patient ID.
    """
    hook: str
    id: str
    title: str
    description: str
    prefetch: dict[str, str] = Field(default_factory=dict)


class CdsDiscoveryResponse(CdsBase):
    """Response body for GET /cds-services."""
    services: list[CdsServiceDefinition] = Field(default_factory=list)
