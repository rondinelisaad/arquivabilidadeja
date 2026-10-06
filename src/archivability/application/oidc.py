from __future__ import annotations

import asyncio
import hashlib
import importlib
import re
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from archivability.application.api import ApiPrincipal


_APPROVED_ALGORITHMS = frozenset({"PS256", "RS256", "ES256", "EdDSA"})
_CLAIM_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,63}$")


class OidcConfigurationError(ValueError):
    """Raised when trusted OIDC verifier configuration is unsafe or malformed."""


class OidcVerificationError(ValueError):
    """Raised without provider or token details when a JWT cannot be trusted."""


@dataclass(frozen=True, slots=True)
class OidcVerifierConfig:
    issuer: str
    audience: str
    jwks_uri: str
    algorithms: tuple[str, ...] = ("RS256",)
    token_types: tuple[str, ...] = ("at+jwt",)
    session_claim: str = "sid"
    leeway_seconds: int = 30
    max_token_lifetime_seconds: int = 3600
    jwks_cache_seconds: int = 300
    jwks_timeout_seconds: int = 5
    jwks_cooldown_seconds: int = 30

    def __post_init__(self) -> None:
        self._validate_https_url("issuer", self.issuer)
        self._validate_https_url("jwks_uri", self.jwks_uri)
        if (
            not isinstance(self.audience, str)
            or not 1 <= len(self.audience) <= 512
            or any(ord(character) < 33 for character in self.audience)
        ):
            raise OidcConfigurationError("audience is invalid")
        if (
            not isinstance(self.algorithms, tuple)
            or not self.algorithms
            or any(not isinstance(value, str) for value in self.algorithms)
            or len(set(self.algorithms)) != len(self.algorithms)
            or not set(self.algorithms) <= _APPROVED_ALGORITHMS
        ):
            raise OidcConfigurationError(
                "algorithms must be a unique subset of approved asymmetric algorithms"
            )
        if (
            not isinstance(self.token_types, tuple)
            or not self.token_types
            or any(
                not isinstance(value, str)
                or not 1 <= len(value) <= 64
                or any(
                    ord(character) < 33 or ord(character) > 126
                    for character in value
                )
                for value in self.token_types
            )
            or len(set(self.token_types)) != len(self.token_types)
        ):
            raise OidcConfigurationError("token_types is invalid")
        if not isinstance(self.session_claim, str) or not _CLAIM_NAME.fullmatch(
            self.session_claim
        ):
            raise OidcConfigurationError("session_claim is invalid")
        self._bounded_int("leeway_seconds", self.leeway_seconds, minimum=0, maximum=120)
        self._bounded_int(
            "max_token_lifetime_seconds",
            self.max_token_lifetime_seconds,
            minimum=60,
            maximum=86_400,
        )
        self._bounded_int(
            "jwks_cache_seconds", self.jwks_cache_seconds, minimum=60, maximum=3600
        )
        self._bounded_int(
            "jwks_timeout_seconds", self.jwks_timeout_seconds, minimum=1, maximum=10
        )
        self._bounded_int(
            "jwks_cooldown_seconds",
            self.jwks_cooldown_seconds,
            minimum=0,
            maximum=300,
        )

    @staticmethod
    def _validate_https_url(field: str, value: str) -> None:
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 2048
            or any(ord(character) < 33 or ord(character) > 126 for character in value)
        ):
            raise OidcConfigurationError(f"{field} is invalid")
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise OidcConfigurationError(f"{field} is invalid") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port == 0
        ):
            raise OidcConfigurationError(
                f"{field} must be an HTTPS URL without credentials"
            )

    @staticmethod
    def _bounded_int(field: str, value: int, *, minimum: int, maximum: int) -> None:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not minimum <= value <= maximum
        ):
            raise OidcConfigurationError(
                f"{field} must be between {minimum} and {maximum}"
            )


class OidcJwtVerifier:
    """Verify asymmetric OIDC access-token JWTs against a configured JWKS endpoint."""

    def __init__(
        self,
        config: OidcVerifierConfig,
        *,
        jwk_client: Any | None = None,
        jwt_module: Any | None = None,
    ) -> None:
        if not isinstance(config, OidcVerifierConfig):
            raise OidcConfigurationError("config must be an OidcVerifierConfig")
        if jwt_module is None:
            try:
                jwt_module = importlib.import_module("jwt")
            except ImportError as exc:
                raise OidcConfigurationError(
                    "PyJWT with the crypto extra is required for OIDC verification"
                ) from exc
        if jwk_client is None:
            tls_context = ssl.create_default_context()
            tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
            try:
                jwk_client = jwt_module.PyJWKClient(
                    config.jwks_uri,
                    cache_keys=False,
                    cache_jwk_set=True,
                    lifespan=config.jwks_cache_seconds,
                    timeout=config.jwks_timeout_seconds,
                    ssl_context=tls_context,
                    cooldown_duration=config.jwks_cooldown_seconds,
                )
            except Exception as exc:
                raise OidcConfigurationError("unable to initialize the JWKS client") from exc
        self._config = config
        self._jwt = jwt_module
        self._jwk_client = jwk_client
        self._lock = asyncio.Lock()

    async def verify(self, token: str) -> ApiPrincipal:
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 16_384
            or token.count(".") != 2
            or any(character.isspace() for character in token)
        ):
            raise OidcVerificationError("token verification failed")
        try:
            async with self._lock:
                return await asyncio.to_thread(self._verify_sync, token)
        except Exception:
            raise OidcVerificationError("token verification failed") from None

    def _verify_sync(self, token: str) -> ApiPrincipal:
        signing_key = self._jwk_client.get_signing_key_from_jwt(token)
        required_claims = [
            "iss",
            "aud",
            "exp",
            "iat",
            "nbf",
            "sub",
            self._config.session_claim,
        ]
        decoded = self._jwt.decode_complete(
            token,
            key=signing_key,
            algorithms=list(self._config.algorithms),
            audience=self._config.audience,
            issuer=self._config.issuer,
            leeway=self._config.leeway_seconds,
            options={
                "require": required_claims,
                "verify_signature": True,
                "verify_aud": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_iss": True,
                "verify_nbf": True,
                "verify_sub": True,
                "strict_aud": True,
                "enforce_minimum_key_length": True,
            },
        )
        if not isinstance(decoded, Mapping):
            raise OidcVerificationError("token verification failed")
        header = decoded.get("header")
        claims = decoded.get("payload")
        if not isinstance(header, Mapping) or not isinstance(claims, Mapping):
            raise OidcVerificationError("token verification failed")
        if header.get("typ") not in self._config.token_types:
            raise OidcVerificationError("token verification failed")
        subject = claims.get("sub")
        session = claims.get(self._config.session_claim)
        if (
            not isinstance(subject, str)
            or not 1 <= len(subject) <= 255
            or not isinstance(session, str)
            or not 1 <= len(session) <= 256
        ):
            raise OidcVerificationError("token verification failed")
        issued_at = self._integer_claim(claims, "iat")
        not_before = self._integer_claim(claims, "nbf")
        expires_at = self._integer_claim(claims, "exp")
        if (
            not_before < issued_at - self._config.leeway_seconds
            or expires_at <= issued_at
            or expires_at - issued_at > self._config.max_token_lifetime_seconds
        ):
            raise OidcVerificationError("token verification failed")
        return ApiPrincipal(
            user_id=self._opaque_identifier("user", self._config.issuer, subject),
            session_id=self._opaque_identifier(
                "session", self._config.issuer, subject, session
            ),
        )

    @staticmethod
    def _integer_claim(claims: Mapping[str, Any], name: str) -> int:
        value = claims.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise OidcVerificationError("token verification failed")
        return value

    @staticmethod
    def _opaque_identifier(kind: str, *parts: str) -> str:
        hasher = hashlib.sha256()
        for part in (kind, *parts):
            encoded = part.encode("utf-8")
            hasher.update(len(encoded).to_bytes(4, "big"))
            hasher.update(encoded)
        return f"oidc-{kind}:{hasher.hexdigest()}"
