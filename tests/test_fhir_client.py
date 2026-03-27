"""
Tests for Phase 6 FHIR client: JWT auth, token caching, FHIR fetch, error handling.

All Epic HTTP calls are mocked with pytest-mock / unittest.mock — no real network
calls, no Epic credentials needed.

Run with:
    pytest tests/test_fhir_client.py -v

C# analogy: unit test class using Moq to mock IHttpClientFactory,
verified with xUnit Facts.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — build minimal FHIR resources and httpx response mocks
# ---------------------------------------------------------------------------

def _mock_response(status_code: int, json_body: dict) -> MagicMock:
    """
    Build a mock object that looks like an httpx.Response.

    MagicMock auto-creates attributes on access, so we only need to set
    the ones our code actually reads.
    C# analogy: new Mock<HttpResponseMessage>() with .Setup(...).Returns(...)
    """
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_body
    mock.text = str(json_body)
    # raise_for_status() does nothing for 2xx; raises for >= 400
    if status_code >= 400:
        from httpx import HTTPStatusError, Request, Response
        # We only need the exception raised — the actual response object
        # doesn't need to be a real httpx.Response for our tests.
        mock.raise_for_status.side_effect = HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(spec=Request),
            response=MagicMock(spec=Response),
        )
    else:
        mock.raise_for_status.return_value = None
    return mock


def _patient_resource(patient_id: str = "erXuFYUfucBZaryVksYEcMg3") -> dict:
    return {
        "resourceType": "Patient",
        "id": patient_id,
        "name": [{"use": "official", "family": "Lin", "given": ["Derrick"]}],
        "birthDate": "1974-08-15",
        "gender": "male",
    }


def _bundle(resource_type: str, resources: list[dict]) -> dict:
    """Build a minimal FHIR Bundle search result."""
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"resource": r} for r in resources],
    }


def _token_response(expires_in: int = 3600) -> dict:
    return {
        "access_token": "test-bearer-token-abc123",
        "token_type": "bearer",
        "expires_in": expires_in,
    }


# ---------------------------------------------------------------------------
# Test: JWT assertion built correctly
# ---------------------------------------------------------------------------

def test_jwt_assertion_built_correctly(tmp_path):
    """
    _build_client_assertion() must produce a JWT with the required Epic claims
    and sign it with RS384.

    We generate a fresh RSA key pair for the test — no dependency on keys/ dir.
    C# analogy: Assert that the JWT payload contains expected claims after decode.
    """
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    import jwt as pyjwt

    # Generate a throwaway RSA key pair for this test
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_file = tmp_path / "test_key.pem"
    key_file.write_bytes(pem)

    with patch("app.fhir.auth.get_settings") as mock_settings:
        mock_settings.return_value.epic_client_id    = "test-client-id"
        mock_settings.return_value.epic_token_endpoint = "https://fhir.epic.com/token"
        mock_settings.return_value.epic_key_id       = "test-key-v1"
        mock_settings.return_value.epic_private_key_path = key_file

        from app.fhir.auth import _build_client_assertion
        token_str = _build_client_assertion()

    # Decode without verification to inspect claims
    # algorithms=["RS384"] tells PyJWT which algorithm to expect
    payload = pyjwt.decode(
        token_str,
        options={"verify_signature": False},
        algorithms=["RS384"],
    )

    assert payload["iss"] == "test-client-id",       "iss must be client_id"
    assert payload["sub"] == "test-client-id",       "sub must be client_id"
    assert payload["aud"] == "https://fhir.epic.com/token", "aud must be token endpoint"
    assert "jti" in payload,                         "jti (unique ID) must be present"
    assert "exp" in payload,                         "exp (expiry) must be present"
    assert payload["exp"] > payload["iat"],          "exp must be after iat"

    # Check the header for correct algorithm and kid
    header = pyjwt.get_unverified_header(token_str)
    assert header["alg"] == "RS384",    "algorithm must be RS384 (Epic requirement)"
    assert header["kid"] == "test-key-v1", "kid must match configured key ID"


# ---------------------------------------------------------------------------
# Test: Token cached across calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_cached_across_calls(tmp_path):
    """
    get_access_token() should return the cached token on the second call
    without hitting the token endpoint again.

    C# analogy: verify IMemoryCache.TryGetValue returns cached value
    and HttpClient.PostAsync is only called once.
    """
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(pem)

    with patch("app.fhir.auth.get_settings") as mock_settings, \
         patch("app.fhir.auth._fetch_token", new_callable=AsyncMock) as mock_fetch, \
         patch("app.fhir.auth._cache") as mock_cache_ref:

        mock_settings.return_value.epic_client_id    = "test-client"
        mock_settings.return_value.epic_token_endpoint = "https://fhir.epic.com/token"
        mock_settings.return_value.epic_key_id       = "k1"
        mock_settings.return_value.epic_private_key_path = key_file

        mock_fetch.return_value = ("cached-token-xyz", 3600)

        # Reset the module-level cache to a fresh empty state
        from app.fhir import auth as auth_module
        from app.fhir.auth import _TokenCache
        auth_module._cache = _TokenCache()   # start with no cached token

        # First call — should call _fetch_token
        token1 = await auth_module.get_access_token()
        assert token1 == "cached-token-xyz"
        assert mock_fetch.call_count == 1

        # Second call — cache is now valid, should NOT call _fetch_token again
        token2 = await auth_module.get_access_token()
        assert token2 == "cached-token-xyz"
        assert mock_fetch.call_count == 1, "Second call must use cached token"


# ---------------------------------------------------------------------------
# Test: Expired token triggers re-auth
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_refreshed_on_expiry():
    """
    When the cached token is within the refresh buffer window (expires soon),
    get_access_token() must fetch a new token.

    C# analogy: assert IMemoryCache entry is not returned after TTL expires.
    """
    from app.fhir import auth as auth_module
    from app.fhir.auth import _TokenCache

    # Set cache to a token that expired 1 minute ago
    expired_cache = _TokenCache(
        access_token="old-expired-token",
        expires_at=datetime.now(tz=timezone.utc) - timedelta(minutes=1),
    )
    auth_module._cache = expired_cache

    with patch("app.fhir.auth._fetch_token", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = ("fresh-token-999", 3600)
        token = await auth_module.get_access_token()

    assert token == "fresh-token-999", "Must return fresh token after expiry"
    assert mock_fetch.call_count == 1, "_fetch_token must be called once for refresh"


# ---------------------------------------------------------------------------
# Test: FHIR fetch returns a populated FhirDataBundle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fhir_fetch_returns_bundle():
    """
    fetch_patient_bundle() with all 5 resources returning 200 should produce
    a FhirDataBundle with has_real_data=True and all lists populated.

    C# analogy: mock HttpMessageHandler returning 200 for all FHIR endpoints,
    assert result DTO has expected values.
    """
    patient_id = "erXuFYUfucBZaryVksYEcMg3"

    patient_resp  = _mock_response(200, _patient_resource(patient_id))
    meds_resp     = _mock_response(200, _bundle("MedicationRequest", [
        {"resourceType": "MedicationRequest", "id": "med-1",
         "medicationCodeableConcept": {"text": "metformin 500mg tablet"}},
    ]))
    labs_resp     = _mock_response(200, _bundle("Observation", [
        {"resourceType": "Observation", "id": "obs-1",
         "code": {"coding": [{"code": "4548-4", "display": "HbA1c"}]},
         "valueQuantity": {"value": 7.5}},
    ]))
    conditions_resp = _mock_response(200, _bundle("Condition", []))
    coverage_resp   = _mock_response(200, _bundle("Coverage", []))

    # AsyncMock side_effect takes a list — each call consumes the next value.
    # The 5 parallel FHIR fetches all go through http.get(), so we simulate
    # them in the order asyncio.gather() fires them (Patient, Meds, Labs,
    # Conditions, Coverage).
    # C# analogy: HttpMessageHandler.SendAsync returning different responses
    # per call sequence.
    mock_get = AsyncMock(side_effect=[
        patient_resp,
        meds_resp,
        labs_resp,
        conditions_resp,
        coverage_resp,
    ])

    with patch("app.fhir.epic_client.get_access_token", new_callable=AsyncMock) as mock_auth, \
         patch("httpx.AsyncClient") as mock_client_cls:

        mock_auth.return_value = "test-bearer-token"

        # Set up the context manager returned by httpx.AsyncClient(...)
        mock_http = AsyncMock()
        mock_http.get = mock_get
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_client_cls.return_value.__aexit__  = AsyncMock(return_value=False)

        from app.fhir.epic_client import EpicFHIRClient
        client = EpicFHIRClient()
        bundle = await client.fetch_patient_bundle(patient_id)

    assert bundle.has_real_data,                    "has_real_data must be True when Patient fetched"
    assert bundle.fetched_from == "epic_fhir",      "fetched_from must be 'epic_fhir'"
    assert bundle.patient is not None,              "Patient resource must be populated"
    assert len(bundle.medications) == 1,            "MedicationRequest bundle should have 1 entry"
    assert len(bundle.lab_observations) == 1,       "Lab observation bundle should have 1 entry"
    assert bundle.fetch_errors == [],               "No errors expected for successful fetch"
    assert bundle.patient_name() == "Derrick Lin",  "patient_name() should parse HumanName"


# ---------------------------------------------------------------------------
# Test: 404 on Patient resource → empty bundle, no exception raised
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fhir_404_handled_gracefully():
    """
    A 404 on /Patient/{id} means the patient isn't in the Epic sandbox.
    fetch_patient_bundle() must return an empty bundle rather than raising.

    C# analogy: assert no exception thrown when HttpStatusCode is NotFound;
    assert result.IsSuccess == false.
    """
    patient_id = "unknown-patient-id"

    patient_404  = _mock_response(404, {"resourceType": "OperationOutcome"})
    empty_bundle = _mock_response(200, _bundle("MedicationRequest", []))

    mock_get = AsyncMock(side_effect=[
        patient_404,     # /Patient/{id} → 404
        empty_bundle,    # /MedicationRequest → 200 empty
        empty_bundle,    # /Observation → 200 empty
        empty_bundle,    # /Condition → 200 empty
        empty_bundle,    # /Coverage → 200 empty
    ])

    with patch("app.fhir.epic_client.get_access_token", new_callable=AsyncMock) as mock_auth, \
         patch("httpx.AsyncClient") as mock_client_cls:

        mock_auth.return_value = "test-token"
        mock_http = AsyncMock()
        mock_http.get = mock_get
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_client_cls.return_value.__aexit__  = AsyncMock(return_value=False)

        from app.fhir.epic_client import EpicFHIRClient
        client = EpicFHIRClient()
        bundle = await client.fetch_patient_bundle(patient_id)

    # Must not raise — 404 is expected for patients not in Epic sandbox
    assert not bundle.has_real_data,                "Patient not found → has_real_data=False"
    assert bundle.patient is None,                  "Patient resource must be None on 404"
    assert bundle.fetched_from == "prefetch_only",  "fetched_from must fall back to prefetch_only"
    assert bundle.fetch_errors == [],               "404 is not an error — it is expected"


# ---------------------------------------------------------------------------
# Test: Auth failure → empty bundle returned, no exception raised
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auth_failure_returns_empty_bundle():
    """
    When get_access_token() raises (network down, bad key, etc.),
    fetch_patient_bundle() must return an empty FhirDataBundle gracefully.

    This validates the 'graceful degradation' contract: FHIR failure must
    never cause a CDS hook to fail.  Synthetic overlay covers 100%.

    C# analogy: assert no exception propagates when ITokenService.GetTokenAsync throws.
    """
    with patch("app.fhir.epic_client.get_access_token", new_callable=AsyncMock) as mock_auth:
        mock_auth.side_effect = ConnectionError("Epic token endpoint unreachable")

        from app.fhir.epic_client import EpicFHIRClient
        client = EpicFHIRClient()
        bundle = await client.fetch_patient_bundle("any-patient-id")

    assert not bundle.has_real_data,               "Auth failure → has_real_data=False"
    assert bundle.fetched_from == "prefetch_only", "Auth failure → fetched_from=prefetch_only"
    assert len(bundle.fetch_errors) == 1,          "Auth failure must be recorded in fetch_errors"
    assert "auth_failed" in bundle.fetch_errors[0], "Error must be labelled as auth_failed"


# ---------------------------------------------------------------------------
# Test: JWK Set endpoint returns valid JSON
# ---------------------------------------------------------------------------

def test_jwks_endpoint_returns_valid_json(tmp_path):
    """
    GET /.well-known/jwks.json must return HTTP 200 with a keys array
    containing an RSA public key with the correct kid.

    C# analogy: TestServer.CreateClient().GetAsync("/.well-known/jwks.json")
    then Assert.Equal(HttpStatusCode.OK, response.StatusCode).
    """
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from fastapi.testclient import TestClient

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(pem)

    with patch("app.api.jwks.get_settings") as mock_settings:
        mock_settings.return_value.epic_private_key_path = key_file
        mock_settings.return_value.epic_key_id = "test-jwk-kid"

        from app.main import app
        with TestClient(app) as client:
            response = client.get("/.well-known/jwks.json")

    assert response.status_code == 200,             "JWKS endpoint must return 200"
    body = response.json()
    assert "keys" in body,                          "Response must have 'keys' array"
    assert len(body["keys"]) == 1,                  "Must return exactly one key"
    key = body["keys"][0]
    assert key["kty"] == "RSA",                     "Key type must be RSA"
    assert key["use"] == "sig",                     "Key use must be 'sig'"
    assert key["alg"] == "RS384",                   "Algorithm must be RS384"
    assert key["kid"] == "test-jwk-kid",            "kid must match configured EPIC_KEY_ID"
    assert "n" in key and "e" in key,               "RSA modulus (n) and exponent (e) must be present"
    assert key["e"] == "AQAB",                      "Public exponent 65537 encodes as AQAB"
