from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from archivability.probes.models import ApprovedTarget, ProbeValidationError
from archivability.probes.ports import AddressResolver


_UNSAFE_CHARACTER_PATTERN = re.compile(r"[\x00-\x20\x7f\\]")
_ASCII_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True, slots=True)
class SsrfPolicy:
    """Approves public HTTP(S) targets without performing network I/O itself."""

    allowed_ports: frozenset[int] = frozenset({80, 443})
    allowed_schemes: frozenset[str] = frozenset({"http", "https"})
    maximum_addresses: int = 16

    def __post_init__(self) -> None:
        if not self.allowed_ports or any(
            isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
            for port in self.allowed_ports
        ):
            raise ProbeValidationError("allowed_ports must contain valid TCP ports")
        if not self.allowed_schemes or not self.allowed_schemes <= {"http", "https"}:
            raise ProbeValidationError("allowed_schemes can contain only HTTP and HTTPS")
        if isinstance(self.maximum_addresses, bool) or not isinstance(
            self.maximum_addresses, int
        ) or not 1 <= self.maximum_addresses <= 64:
            raise ProbeValidationError("maximum_addresses must be between 1 and 64")

    def approve(self, uri: str, resolver: AddressResolver) -> ApprovedTarget:
        if not isinstance(uri, str) or not uri or len(uri) > 2048:
            raise ProbeValidationError("URI must contain between 1 and 2048 characters")
        if _UNSAFE_CHARACTER_PATTERN.search(uri):
            raise ProbeValidationError("URI contains whitespace, controls or backslashes")
        try:
            parsed = urlsplit(uri)
            port = parsed.port
        except ValueError as exc:
            raise ProbeValidationError("URI authority or port is invalid") from exc
        scheme = parsed.scheme.lower()
        if scheme not in self.allowed_schemes:
            raise ProbeValidationError("URI scheme is not allowed")
        if not parsed.netloc or parsed.hostname is None:
            raise ProbeValidationError("URI must have an authority and hostname")
        if parsed.username is not None or parsed.password is not None:
            raise ProbeValidationError("URI cannot contain credentials")
        if parsed.fragment:
            raise ProbeValidationError("URI fragments are not accepted")

        hostname = self._canonical_hostname(parsed.hostname)
        effective_port = port or (443 if scheme == "https" else 80)
        if effective_port not in self.allowed_ports:
            raise ProbeValidationError("URI port is not allowed")
        addresses = self._resolve_public_addresses(hostname, effective_port, resolver)

        host_for_uri = f"[{hostname}]" if ":" in hostname else hostname
        default_port = 443 if scheme == "https" else 80
        authority = host_for_uri if effective_port == default_port else f"{host_for_uri}:{effective_port}"
        path = parsed.path or "/"
        normalized_uri = urlunsplit((scheme, authority, path, parsed.query, ""))
        return ApprovedTarget(
            normalized_uri=normalized_uri,
            scheme=scheme,
            hostname=hostname,
            port=effective_port,
            addresses=addresses,
        )

    def approve_redirect(
        self,
        current: ApprovedTarget,
        location: str,
        resolver: AddressResolver,
    ) -> ApprovedTarget:
        if not isinstance(location, str) or not location:
            raise ProbeValidationError("redirect location must be a non-empty string")
        return self.approve(urljoin(current.normalized_uri, location), resolver)

    @staticmethod
    def _canonical_hostname(hostname: str) -> str:
        candidate = hostname.rstrip(".").lower()
        if not candidate or len(candidate) > 253 or "%" in candidate:
            raise ProbeValidationError("hostname is invalid")
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            try:
                ascii_hostname = candidate.encode("idna").decode("ascii")
            except UnicodeError as exc:
                raise ProbeValidationError("hostname IDNA encoding failed") from exc
            labels = ascii_hostname.split(".")
            if any(not _ASCII_LABEL_PATTERN.fullmatch(label) for label in labels):
                raise ProbeValidationError("hostname has an invalid DNS label")
            if ascii_hostname == "localhost" or ascii_hostname.endswith(".localhost"):
                raise ProbeValidationError("localhost names are forbidden")
            return ascii_hostname
        return str(address)

    def _resolve_public_addresses(
        self,
        hostname: str,
        port: int,
        resolver: AddressResolver,
    ) -> tuple[str, ...]:
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            raw_addresses = resolver.resolve(hostname, port)
        else:
            raw_addresses = (str(literal),)
        if not raw_addresses:
            raise ProbeValidationError("hostname did not resolve to an address")
        if len(raw_addresses) > self.maximum_addresses:
            raise ProbeValidationError("hostname resolved to too many addresses")

        normalized: list[str] = []
        for raw_address in raw_addresses:
            if not isinstance(raw_address, str) or "%" in raw_address:
                raise ProbeValidationError("resolver returned an invalid address")
            try:
                address = ipaddress.ip_address(raw_address)
            except ValueError as exc:
                raise ProbeValidationError("resolver returned a non-IP address") from exc
            if not address.is_global:
                raise ProbeValidationError("target resolves to a non-global address")
            canonical = str(address)
            if canonical not in normalized:
                normalized.append(canonical)
        return tuple(normalized)
