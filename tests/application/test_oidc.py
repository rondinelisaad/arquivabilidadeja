from __future__ import annotations

import json
import secrets
import sys
import time
import unittest
import warnings
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, rsa  # noqa: E402

from archivability import (  # noqa: E402
    OidcConfigurationError,
    OidcJwtVerifier,
    OidcVerificationError,
    OidcVerifierConfig,
)


ISSUER = "https://identity.example.org/realms/archivability"
AUDIENCE = "arquivabilidade-api"
JWKS_URI = "https://identity.example.org/realms/archivability/jwks"


class StaticJwkClient:
    def __init__(self, signing_key: Any) -> None:
        self.signing_key = signing_key
        self.tokens: list[str] = []

    def get_signing_key_from_jwt(self, token: str) -> Any:
        self.tokens.append(token)
        return self.signing_key


class OidcVerifierConfigTests(unittest.TestCase):
    def test_accepts_only_explicit_https_asymmetric_configuration(self) -> None:
        config = OidcVerifierConfig(
            issuer=ISSUER,
            audience=AUDIENCE,
            jwks_uri=JWKS_URI,
            algorithms=("PS256", "RS256", "ES256", "EdDSA"),
        )

        self.assertEqual(("at+jwt",), config.token_types)

    def test_rejects_unsafe_urls_algorithms_and_limits(self) -> None:
        invalid_values = (
            {"issuer": "http://identity.example.org"},
            {"jwks_uri": "file:///etc/passwd"},
            {"jwks_uri": "https://user:password@identity.example.org/jwks"},
            {"algorithms": ("none",)},
            {"algorithms": ("HS256",)},
            {"algorithms": ("RS256", "RS256")},
            {"leeway_seconds": 121},
            {"max_token_lifetime_seconds": 86_401},
            {"jwks_timeout_seconds": 0},
            {"session_claim": "invalid claim"},
        )
        defaults = {"issuer": ISSUER, "audience": AUDIENCE, "jwks_uri": JWKS_URI}

        for override in invalid_values:
            with self.subTest(override=override), self.assertRaises(OidcConfigurationError):
                OidcVerifierConfig(**{**defaults, **override})


class OidcJwtVerifierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.private_key.public_key()))
        jwk.update({"alg": "ES256", "kid": "test-key-1", "use": "sig"})
        self.signing_key = jwt.PyJWK.from_dict(jwk)
        self.jwk_client = StaticJwkClient(self.signing_key)
        self.config = OidcVerifierConfig(
            issuer=ISSUER,
            audience=AUDIENCE,
            jwks_uri=JWKS_URI,
            algorithms=("ES256",),
            max_token_lifetime_seconds=3600,
        )
        self.verifier = OidcJwtVerifier(self.config, jwk_client=self.jwk_client)

    def token(self, **overrides: Any) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "person@example.org",
            "sid": "provider-session-123",
            "scope": "analysis:create analysis:read",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        }
        claims.update(overrides)
        return jwt.encode(
            claims,
            self.private_key,
            algorithm="ES256",
            headers={"kid": "test-key-1", "typ": "at+jwt"},
        )

    async def test_verifies_signature_claims_and_returns_only_opaque_identifiers(self) -> None:
        token = self.token()
        principal = await self.verifier.verify(token)

        self.assertRegex(principal.user_id, r"^oidc-user:[0-9a-f]{64}$")
        self.assertRegex(principal.session_id, r"^oidc-session:[0-9a-f]{64}$")
        self.assertNotIn("person", principal.user_id)
        self.assertNotIn("provider-session", principal.session_id)
        self.assertEqual(
            frozenset({"analysis:create", "analysis:read"}),
            principal.permissions,
        )
        self.assertEqual([token], self.jwk_client.tokens)

    async def test_rejects_wrong_issuer_audience_or_temporal_claims(self) -> None:
        now = int(time.time())
        invalid_tokens = (
            self.token(iss="https://attacker.example"),
            self.token(aud="different-api"),
            self.token(iat=now - 900, nbf=now - 900, exp=now - 300),
            self.token(iat=now + 300, nbf=now + 300, exp=now + 600),
            self.token(iat=now, nbf=now, exp=now + 7200),
        )

        for token in invalid_tokens:
            with self.subTest(token_index=invalid_tokens.index(token)):
                with self.assertRaisesRegex(OidcVerificationError, "verification failed"):
                    await self.verifier.verify(token)

    async def test_rejects_wrong_type_missing_session_and_non_jwt_input(self) -> None:
        token_with_wrong_type = jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": "subject-1",
                "sid": "session-1",
                "scope": "analysis:read",
                "iat": int(time.time()),
                "nbf": int(time.time()),
                "exp": int(time.time()) + 600,
            },
            self.private_key,
            algorithm="ES256",
            headers={"kid": "test-key-1", "typ": "JWT"},
        )
        invalid_tokens = (
            token_with_wrong_type,
            self.token(sid=None),
            self.token(scope=None),
            self.token(scope="analysis:read analysis:read"),
            self.token(scope='analysis:read invalid"permission'),
            "not-a-jwt",
        )

        for index, token in enumerate(invalid_tokens):
            with self.subTest(token_index=index):
                with self.assertRaises(OidcVerificationError):
                    await self.verifier.verify(token)

    async def test_rejects_symmetric_algorithm_and_short_rsa_key(self) -> None:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "subject-1",
            "sid": "session-1",
            "scope": "analysis:read",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        }
        symmetric_token = jwt.encode(
            claims,
            secrets.token_bytes(32),
            algorithm="HS256",
            headers={"typ": "at+jwt"},
        )
        with self.assertRaises(OidcVerificationError):
            await self.verifier.verify(symmetric_token)

        weak_private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=1024,
        )
        weak_jwk = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(weak_private_key.public_key())
        )
        weak_jwk.update({"alg": "RS256", "kid": "weak-key", "use": "sig"})
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", jwt.warnings.InsecureKeyLengthWarning)
            weak_token = jwt.encode(
                claims,
                weak_private_key,
                algorithm="RS256",
                headers={"kid": "weak-key", "typ": "at+jwt"},
            )
        weak_verifier = OidcJwtVerifier(
            OidcVerifierConfig(
                issuer=ISSUER,
                audience=AUDIENCE,
                jwks_uri=JWKS_URI,
                algorithms=("RS256",),
            ),
            jwk_client=StaticJwkClient(jwt.PyJWK.from_dict(weak_jwk)),
        )
        with self.assertRaises(OidcVerificationError):
            await weak_verifier.verify(weak_token)

    async def test_error_never_contains_token_or_provider_details(self) -> None:
        token = self.token(aud="private-provider-detail")

        try:
            await self.verifier.verify(token)
        except OidcVerificationError as exc:
            serialized = str(exc)
        else:
            self.fail("invalid token was accepted")

        self.assertEqual("token verification failed", serialized)
        self.assertNotIn(token, serialized)
        self.assertNotIn("private-provider-detail", serialized)


if __name__ == "__main__":
    unittest.main()
