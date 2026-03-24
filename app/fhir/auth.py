"""
SMART Backend Services authentication for Epic FHIR R4.

Flow (per architecture.md §4):
  1. Build a signed JWT (RS384) asserting our identity
  2. POST it to Epic's token endpoint as a client_assertion
  3. Receive a bearer access_token
  4. Cache the token until it expires
  5. Attach it as Authorization: Bearer <token> on every FHIR request
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import jwt  # PyJWT — signs and encodes JWTs

from app.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
JWT_EXPIRY_SECONDS = 300          # 5 minutes — Epic's maximum allowed
TOKEN_REFRESH_BUFFER_SECONDS = 30  # refresh this many seconds before expiry


# ---------------------------------------------------------------------------
# Token cache — a simple dataclass holding the token + its expiry time.
#
# @dataclass is a decorator that auto-generates __init__, __repr__, __eq__
# from the annotated class attributes.
# C# analogy: a record or a plain DTO class — saves writing boilerplate.
#
# field(default=None) provides a per-instance default without using a
# mutable default argument (a common Python gotcha — never use [] or {} as
# default argument values in function signatures).
# ---------------------------------------------------------------------------
@dataclass
class _TokenCache:
    access_token: str | None = field(default=None)
    # datetime | None is a "union type" — C# analogy: DateTime? (nullable)
    expires_at: datetime | None = field(default=None)

    def is_valid(self) -> bool:
        """Return True if we have a token that won't expire in the next 30 seconds."""
        if self.access_token is None or self.expires_at is None:
            return False
        now = datetime.now(tz=timezone.utc)
        # timedelta arithmetic works directly on datetime objects in Python
        from datetime import timedelta
        return now < self.expires_at - timedelta(seconds=TOKEN_REFRESH_BUFFER_SECONDS)


# Module-level singleton cache — lives for the lifetime of the process.
# C# analogy: a private static field on the auth service class.
_cache = _TokenCache()


# ---------------------------------------------------------------------------
# JWT builder
# ---------------------------------------------------------------------------
def _build_client_assertion() -> str:
    """
    Build and sign a JWT asserting our identity to Epic's OAuth server.

    Epic spec requirements:
      - Algorithm: RS384
      - iss: client_id
      - sub: client_id
      - aud: token endpoint URL (exact string match)
      - jti: unique UUID — Epic rejects duplicate JTIs within the token lifetime
      - exp: now + 5 minutes (maximum)
    """
    settings = get_settings()

    # Read the PEM-encoded private key from disk.
    # Path.read_text() is the idiomatic way to read a file in Python.
    # C# analogy: File.ReadAllText(path)
    private_key_pem = settings.epic_private_key_path.read_text(encoding="utf-8")

    now = datetime.now(tz=timezone.utc)

    payload = {
        "iss": settings.epic_client_id,
        "sub": settings.epic_client_id,
        "aud": settings.epic_token_endpoint,
        # str(uuid.uuid4()) generates a random UUID string — e.g. "550e8400-e29b-..."
        # C# analogy: Guid.NewGuid().ToString()
        "jti": str(uuid.uuid4()),
        # int() truncates to whole seconds — JWT standard uses Unix timestamps
        "exp": int(now.timestamp()) + JWT_EXPIRY_SECONDS,
        "nbf": int(now.timestamp()),
        "iat": int(now.timestamp()),
    }

    # jwt.encode() signs the payload and returns a compact JWT string.
    # headers={"kid": ...} tells Epic which public key to verify against —
    # must match the key ID registered in the Epic app configuration.
    token: str = jwt.encode(
        payload,
        private_key_pem,
        algorithm="RS384",
        headers={"kid": settings.epic_key_id},
    )

    logger.debug("Built client assertion JWT (jti=%s)", payload["jti"])
    return token


# ---------------------------------------------------------------------------
# Token fetch
# ---------------------------------------------------------------------------
async def _fetch_token() -> str:
    """
    Exchange our signed JWT for an Epic bearer access token.

    Uses httpx for async HTTP — C# analogy: await HttpClient.PostAsync(...)
    """
    settings = get_settings()
    assertion = _build_client_assertion()

    # httpx.AsyncClient is an async context manager — it opens a connection
    # pool on entry and cleanly closes all connections on exit.
    # C# analogy: using (var client = new HttpClient()) { ... }
    async with httpx.AsyncClient() as client:
        response = await client.post(
            settings.epic_token_endpoint,
            # Epic expects application/x-www-form-urlencoded (not JSON)
            data={
                "grant_type": "client_credentials",
                "client_assertion_type": (
                    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                ),
                "client_assertion": assertion,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15.0,
        )

    # response.raise_for_status() throws httpx.HTTPStatusError if status >= 400.
    # C# analogy: response.EnsureSuccessStatusCode()
    if response.status_code != 200:
        logger.error(
            "Epic token request failed: HTTP %s — %s",
            response.status_code,
            response.text,
        )
        response.raise_for_status()

    # response.json() parses the JSON body into a Python dict.
    # C# analogy: await response.Content.ReadFromJsonAsync<TokenResponse>()
    body: dict = response.json()
    access_token: str = body["access_token"]
    expires_in: int = body.get("expires_in", JWT_EXPIRY_SECONDS)

    logger.info("Epic access token acquired (expires_in=%ss)", expires_in)
    return access_token, expires_in


# ---------------------------------------------------------------------------
# Public interface — the only function the rest of the app should call
# ---------------------------------------------------------------------------
async def get_access_token() -> str:
    """
    Return a valid Epic bearer access token, fetching a new one if needed.

    This is the single entry point for auth — all FHIR calls go through here.
    The internal cache means we hit the token endpoint at most once per ~5 minutes.

    Usage:
        from app.fhir.auth import get_access_token
        token = await get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
    """
    global _cache  # tells Python we're modifying the module-level variable
                   # C# analogy: accessing a static field — no special syntax needed in C#

    if _cache.is_valid():
        logger.debug("Using cached Epic access token")
        return _cache.access_token

    logger.info("Fetching new Epic access token")
    access_token, expires_in = await _fetch_token()

    # Store in cache with computed expiry timestamp
    from datetime import timedelta
    _cache.access_token = access_token
    _cache.expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in)

    return _cache.access_token
