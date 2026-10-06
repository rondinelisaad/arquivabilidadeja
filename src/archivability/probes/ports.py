from __future__ import annotations

from typing import Protocol

from archivability.evidence.models import Observation
from archivability.lifecycle.models import Attempt
from archivability.probes.models import ProbeContext
from archivability.storage.audit import AuditContext


class AddressResolver(Protocol):
    """Resolves A/AAAA records; implementations must not cache unsafe results."""

    def resolve(self, hostname: str, port: int) -> tuple[str, ...]: ...


class Probe(Protocol):
    """A probe must use only the addresses pinned in ProbeContext.target."""

    probe_id: str
    tool_name: str
    tool_version: str

    def execute(self, context: ProbeContext) -> tuple[Observation, ...]: ...


class ProbeEventRecorder(Protocol):
    def record_probe_event(
        self,
        *,
        analysis_id: str,
        attempt_id: str,
        probe_id: str,
        result: str,
        error_code: str | None,
        audit: AuditContext,
    ) -> None: ...


class ProbeRequestAuthorizer(Protocol):
    def authorize_probe_request(
        self, *, analysis_id: str, attempt_id: str, subject_uri: str
    ) -> None: ...


class ProbeExecutionRepository(Protocol):
    def get_attempt(self, attempt_id: str) -> Attempt | None: ...

    def update_attempt(
        self,
        previous: Attempt,
        current: Attempt,
        *,
        audit: AuditContext,
    ) -> None: ...

    def complete_probe_attempt(
        self,
        previous: Attempt,
        current: Attempt,
        observations: tuple[Observation, ...],
        *,
        audit: AuditContext,
    ) -> None: ...
