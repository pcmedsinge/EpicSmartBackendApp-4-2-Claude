"""
Tests for the CDS Hooks endpoints.

Uses FastAPI's TestClient which runs the app in-process — no live server needed.
C# analogy: WebApplicationFactory<Program> with HttpClient in an xUnit test.

Run with:
    pytest tests/test_cds_hooks.py -v
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app

# ---------------------------------------------------------------------------
# TestClient setup
#
# TestClient wraps the FastAPI app in a requests-compatible HTTP client.
# It starts the app (runs lifespan startup) when used as a context manager.
#
# @pytest.fixture creates a reusable test dependency — pytest injects it into
# any test function that declares it as a parameter.
# scope="module" means the client is created once per test file, not per test.
# C# analogy: IClassFixture<WebApplicationFactory<Program>> shared across tests.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    # The `with` block runs lifespan startup/shutdown around all tests.
    # raise_server_exceptions=True surfaces any unhandled app exceptions as
    # test failures rather than silently returning a 500.
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    # yield is like a "pause" — everything before yield is setup, everything
    # after is teardown. C# analogy: IAsyncLifetime.InitializeAsync / DisposeAsync.


# ---------------------------------------------------------------------------
# Shared fixture — a valid order-select hook request body
# ---------------------------------------------------------------------------
@pytest.fixture
def valid_hook_request():
    """A spec-compliant order-select HookRequest for Scenario A (Ozempic)."""
    return {
        "hook": "order-select",
        "hookInstance": str(uuid.uuid4()),
        "context": {
            "patientId": "erXuFYUfucBZaryVksYEcMg3",
            "userId": "Practitioner/demo",
            "selections": ["MedicationRequest/demo-ozempic"],
        },
        "prefetch": {
            "patient": {
                "resourceType": "Patient",
                "id": "erXuFYUfucBZaryVksYEcMg3",
                "name": [{"use": "official", "family": "Lin", "given": ["Derrick"]}],
                "gender": "male",
                "birthDate": "1973-11-05",
            },
            "medications": {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": 1,
                "entry": [
                    {
                        "resource": {
                            "resourceType": "MedicationRequest",
                            "id": "demo-ozempic",
                            "status": "draft",
                            "medicationCodeableConcept": {
                                "coding": [
                                    {
                                        "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                        "code": "2200750",
                                        "display": "semaglutide 0.5 MG/DOSE injection",
                                    }
                                ],
                                "text": "Ozempic (semaglutide) 0.5mg injection",
                            },
                        }
                    }
                ],
            },
        },
    }


# ---------------------------------------------------------------------------
# D2: Discovery endpoint tests
# ---------------------------------------------------------------------------
class TestDiscovery:
    def test_returns_200(self, client):
        response = client.get("/cds-services")
        assert response.status_code == 200

    def test_content_type_is_json(self, client):
        response = client.get("/cds-services")
        assert "application/json" in response.headers["content-type"]

    def test_has_services_list(self, client):
        body = client.get("/cds-services").json()
        # "services" key must exist and be a list
        assert "services" in body
        assert isinstance(body["services"], list)
        assert len(body["services"]) >= 1

    def test_cfip_service_registered(self, client):
        services = client.get("/cds-services").json()["services"]
        # next() with a default of None — returns the first match or None
        # C# analogy: services.FirstOrDefault(s => s.Id == "cfip-order-intelligence")
        cfip = next((s for s in services if s["id"] == "cfip-order-intelligence"), None)
        assert cfip is not None, "cfip-order-intelligence service not found in discovery"

    def test_service_has_required_fields(self, client):
        services = client.get("/cds-services").json()["services"]
        cfip = next(s for s in services if s["id"] == "cfip-order-intelligence")
        # CDS Hooks spec requires: hook, id, title, description
        for field in ("hook", "id", "title", "description"):
            assert field in cfip, f"Service missing required field: {field}"

    def test_service_declares_prefetch(self, client):
        services = client.get("/cds-services").json()["services"]
        cfip = next(s for s in services if s["id"] == "cfip-order-intelligence")
        assert "prefetch" in cfip
        assert "patient" in cfip["prefetch"]
        assert "medications" in cfip["prefetch"]

    def test_hook_is_order_select(self, client):
        services = client.get("/cds-services").json()["services"]
        cfip = next(s for s in services if s["id"] == "cfip-order-intelligence")
        assert cfip["hook"] == "order-select"


# ---------------------------------------------------------------------------
# D3: Hook handler tests
# ---------------------------------------------------------------------------
class TestHookHandler:
    def test_valid_request_returns_200(self, client, valid_hook_request):
        response = client.post(
            "/cds-services/cfip-order-intelligence",
            json=valid_hook_request,
        )
        assert response.status_code == 200

    def test_response_has_cards_list(self, client, valid_hook_request):
        body = client.post(
            "/cds-services/cfip-order-intelligence",
            json=valid_hook_request,
        ).json()
        assert "cards" in body
        assert isinstance(body["cards"], list)

    def test_returns_at_least_one_card(self, client, valid_hook_request):
        body = client.post(
            "/cds-services/cfip-order-intelligence",
            json=valid_hook_request,
        ).json()
        assert len(body["cards"]) >= 1

    def test_card_has_required_spec_fields(self, client, valid_hook_request):
        """CDS Hooks spec requires: summary, indicator, source on every card."""
        cards = client.post(
            "/cds-services/cfip-order-intelligence",
            json=valid_hook_request,
        ).json()["cards"]

        for card in cards:
            assert "summary" in card, "Card missing required field: summary"
            assert "indicator" in card, "Card missing required field: indicator"
            assert "source" in card, "Card missing required field: source"

    def test_card_indicator_is_valid(self, client, valid_hook_request):
        """indicator must be one of: info, warning, critical."""
        cards = client.post(
            "/cds-services/cfip-order-intelligence",
            json=valid_hook_request,
        ).json()["cards"]

        valid_indicators = {"info", "warning", "critical"}
        for card in cards:
            assert card["indicator"] in valid_indicators, (
                f"Invalid indicator: {card['indicator']}"
            )

    def test_card_summary_within_140_chars(self, client, valid_hook_request):
        """CDS Hooks spec: summary SHOULD be ≤140 characters."""
        cards = client.post(
            "/cds-services/cfip-order-intelligence",
            json=valid_hook_request,
        ).json()["cards"]

        for card in cards:
            assert len(card["summary"]) <= 140, (
                f"Card summary exceeds 140 chars: {len(card['summary'])}"
            )

    def test_card_source_has_label(self, client, valid_hook_request):
        cards = client.post(
            "/cds-services/cfip-order-intelligence",
            json=valid_hook_request,
        ).json()["cards"]

        for card in cards:
            assert "label" in card["source"], "Card source missing label"

    def test_missing_patient_id_returns_400(self, client):
        """A hook request without patientId in context must return HTTP 400."""
        bad_request = {
            "hook": "order-select",
            "hookInstance": str(uuid.uuid4()),
            # context is missing patientId
            "context": {"userId": "Practitioner/demo"},
        }
        response = client.post(
            "/cds-services/cfip-order-intelligence",
            json=bad_request,
        )
        assert response.status_code == 400

    def test_malformed_body_returns_422(self, client):
        """A body that fails Pydantic validation returns 422 Unprocessable Entity."""
        # FastAPI automatically returns 422 if required fields are missing.
        # hook and hookInstance are required by our HookRequest model.
        response = client.post(
            "/cds-services/cfip-order-intelligence",
            json={"not": "a valid hook request"},
        )
        assert response.status_code == 422

    def test_request_without_prefetch_still_returns_cards(self, client):
        """
        Prefetch is optional — handler must fall back gracefully.
        When prefetch is absent, we return a card with 'Unknown medication'.
        """
        no_prefetch_request = {
            "hook": "order-select",
            "hookInstance": str(uuid.uuid4()),
            "context": {"patientId": "erXuFYUfucBZaryVksYEcMg3"},
            # no prefetch key
        }
        response = client.post(
            "/cds-services/cfip-order-intelligence",
            json=no_prefetch_request,
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["cards"]) >= 1

    def test_unknown_service_id_returns_empty_cards(self, client, valid_hook_request):
        """An unknown service ID should return 200 with empty cards (spec behaviour)."""
        response = client.post(
            "/cds-services/unknown-service",
            json=valid_hook_request,
        )
        assert response.status_code == 200
        assert response.json()["cards"] == []
