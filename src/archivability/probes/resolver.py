from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from typing import Any

from archivability.probes.models import ProbeValidationError


Lookup = Callable[..., Sequence[tuple[Any, ...]]]


class SystemAddressResolver:
    """Resolves A/AAAA records without caching or returning hostnames."""

    def __init__(self, *, lookup: Lookup = socket.getaddrinfo) -> None:
        self._lookup = lookup

    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        if not isinstance(hostname, str) or not hostname or len(hostname) > 253:
            raise ProbeValidationError("resolver hostname is invalid")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ProbeValidationError("resolver port is invalid")
        try:
            records = self._lookup(
                hostname,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except (OSError, UnicodeError) as exc:
            raise ProbeValidationError("hostname resolution failed") from exc

        addresses: list[str] = []
        for record in records:
            if not isinstance(record, tuple) or len(record) < 5:
                raise ProbeValidationError("resolver returned a malformed record")
            family, socktype, protocol, _, sockaddr = record[:5]
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            if socktype != socket.SOCK_STREAM or protocol not in {0, socket.IPPROTO_TCP}:
                continue
            if not isinstance(sockaddr, tuple) or not sockaddr:
                raise ProbeValidationError("resolver returned a malformed address")
            try:
                address = ipaddress.ip_address(sockaddr[0])
            except (TypeError, ValueError) as exc:
                raise ProbeValidationError("resolver returned a non-IP address") from exc
            if (family == socket.AF_INET and address.version != 4) or (
                family == socket.AF_INET6 and address.version != 6
            ):
                raise ProbeValidationError("resolver address family does not match its value")
            canonical = str(address)
            if canonical not in addresses:
                addresses.append(canonical)
        return tuple(addresses)
