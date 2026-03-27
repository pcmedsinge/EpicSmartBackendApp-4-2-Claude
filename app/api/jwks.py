"""
JWK Set endpoint — publishes CFIP's RSA public key so Epic can verify our JWT signatures.

Epic's OAuth server calls GET /.well-known/jwks.json when validating a client_assertion JWT.
Without this endpoint, every auth attempt fails with a signature verification error.

Flow:
  Epic app registration points to: https://<zrok-url>/.well-known/jwks.json
  When CFIP sends a JWT, Epic fetches this endpoint and uses the public key to verify it.
  The key ID in the JWT header ("kid") must match the "kid" field in the returned JWK.
"""

import base64
import logging

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import APIRouter, HTTPException

from app.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Router — registered in main.py with no prefix so the path is exactly
# /.well-known/jwks.json (the conventional OIDC/SMART discovery path).
# C# analogy: [Route(".well-known/jwks.json")] on a controller.
# ---------------------------------------------------------------------------
router = APIRouter(tags=["Infrastructure"])


def _int_to_base64url(n: int) -> str:
    """
    Convert a large integer (RSA modulus or exponent) to base64url-encoded bytes.

    JWK format requires the modulus (n) and exponent (e) encoded as
    base64url with no padding characters.

    Steps:
      1. Convert int to bytes — big-endian (most-significant byte first)
      2. base64-encode the bytes
      3. Switch to URL-safe alphabet (+ → -, / → _)
      4. Strip trailing = padding

    C# analogy: Convert.ToBase64String(BigInteger.ToByteArray(...))
                followed by Base64UrlEncoder.Encode(...)
    """
    # bit_length() returns the number of bits; we round up to whole bytes
    # (n.bit_length() + 7) // 8 is the idiomatic Python "ceiling division"
    byte_length = (n.bit_length() + 7) // 8
    # to_bytes() converts the integer to a big-endian byte string of the given length
    n_bytes = n.to_bytes(byte_length, byteorder="big")
    # urlsafe_b64encode produces base64url with + and / replaced; rstrip removes padding
    return base64.urlsafe_b64encode(n_bytes).rstrip(b"=").decode("ascii")


@router.get("/.well-known/jwks.json", summary="JWK Set — RSA public key for JWT verification")
async def jwks() -> dict:
    """
    Return the JWK Set containing CFIP's RSA public key.

    Epic calls this URL (registered in the Epic app configuration) to fetch
    the public key needed to verify JWT client_assertion signatures.

    The response format follows RFC 7517 (JSON Web Key):
      {
        "keys": [{
          "kty": "RSA",
          "use": "sig",
          "alg": "RS384",
          "kid": "<key-id matching JWT header>",
          "n":   "<base64url-encoded RSA modulus>",
          "e":   "<base64url-encoded RSA public exponent>"
        }]
      }

    No authentication required — this endpoint is intentionally public.
    """
    settings = get_settings()

    try:
        # Read the PEM-encoded private key file from disk.
        # The private key contains both the private and public components —
        # we extract only the public part to serve here.
        # C# analogy: RSA.Create(); rsa.ImportFromPem(File.ReadAllText(path))
        pem_bytes = settings.epic_private_key_path.read_bytes()

        # load_pem_private_key parses PEM and returns an RSAPrivateKey object.
        # password=None because our key is not passphrase-protected.
        private_key = load_pem_private_key(pem_bytes, password=None)

        # Extract the public key, then get its raw mathematical components.
        # public_key().public_numbers() returns an RSAPublicNumbers object with
        # attributes .n (modulus) and .e (exponent) as Python integers.
        # C# analogy: rsa.ExportParameters(includePrivateParameters: false)
        pub_numbers = private_key.public_key().public_numbers()

    except FileNotFoundError:
        logger.error("Private key file not found: %s", settings.epic_private_key_path)
        raise HTTPException(status_code=500, detail="JWK key file not configured")
    except Exception as exc:
        logger.error("Failed to load private key for JWKS: %s", exc)
        raise HTTPException(status_code=500, detail="JWK generation failed")

    jwk = {
        "kty": "RSA",          # key type — always RSA for RS384
        "use": "sig",          # intended use — signature verification (not encryption)
        "alg": "RS384",        # algorithm — must match the JWT header alg claim
        "kid": settings.epic_key_id,  # key ID — must match the "kid" in our JWT headers
        "n": _int_to_base64url(pub_numbers.n),  # RSA modulus (large prime product)
        "e": _int_to_base64url(pub_numbers.e),  # RSA public exponent (typically 65537)
    }

    logger.debug("JWKS requested — returning key kid=%s", settings.epic_key_id)

    # The outer "keys" array allows multiple keys — useful during key rotation.
    # We publish only one key (the active signing key).
    return {"keys": [jwk]}
