from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from archivability.evidence.models import Observation
from archivability.lifecycle.models import Attempt, AttemptState, LifecycleError
from archivability.lifecycle.state_machine import finish_attempt
from archivability.probes.models import ProbeRequest
from archivability.probes.ports import Probe, ProbeExecutionRepository
from archivability.probes.runner import ProbeRunner
from archivability.storage.audit import AuditContext
from archivability.storage.errors import PersistenceError


Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class ProbeExecutionOutcome:
    attempt: Attempt
    observations: tuple[Observation, ...]
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.observations, tuple):
            raise LifecycleError("execution observations must be an immutable tuple")
        if self.attempt.state is AttemptState.SUCCEEDED:
            if not self.observations or self.failure_code is not None:
                raise LifecycleError(
                    "successful execution requires observations and no failure code"
                )
        elif self.attempt.state is AttemptState.FAILED:
            if self.observations or self.failure_code != self.attempt.failure_code:
                raise LifecycleError(
                    "failed execution cannot expose unpersisted observations"
                )
        else:
            raise LifecycleError("probe execution outcome requires a terminal attempt")

    @property
    def succeeded(self) -> bool:
        return self.attempt.state is AttemptState.SUCCEEDED


class ProbeExecutionService:
    """Runs one probe and coordinates durable observations with attempt state."""

    def __init__(
        self,
        *,
        runner: ProbeRunner,
        repository: ProbeExecutionRepository,
        clock: Clock = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._runner = runner
        self._repository = repository
        self._clock = clock

    def execute(
        self,
        request: ProbeRequest,
        probe: Probe,
        *,
        audit: AuditContext = AuditContext(),
    ) -> ProbeExecutionOutcome:
        try:
            result = self._runner.run(request, probe, audit=audit)
        except Exception:
            return self._fail_attempt(request.attempt_id, "PROBE_FAILED", audit)

        previous = self._require_running_attempt(request.attempt_id)
        succeeded = finish_attempt(
            previous,
            target=AttemptState.SUCCEEDED,
            occurred_at=self._now(),
        )
        try:
            self._repository.complete_probe_attempt(
                previous,
                succeeded,
                result.observations,
                audit=audit,
            )
        except PersistenceError:
            return self._fail_attempt(request.attempt_id, "PERSISTENCE_FAILED", audit)
        return ProbeExecutionOutcome(
            attempt=succeeded,
            observations=result.observations,
        )

    def _fail_attempt(
        self,
        attempt_id: str,
        failure_code: str,
        audit: AuditContext,
    ) -> ProbeExecutionOutcome:
        previous = self._require_running_attempt(attempt_id)
        failed = finish_attempt(
            previous,
            target=AttemptState.FAILED,
            occurred_at=self._now(),
            failure_code=failure_code,
        )
        self._repository.update_attempt(previous, failed, audit=audit)
        return ProbeExecutionOutcome(
            attempt=failed,
            observations=(),
            failure_code=failure_code,
        )

    def _require_running_attempt(self, attempt_id: str) -> Attempt:
        attempt = self._repository.get_attempt(attempt_id)
        if attempt is None:
            raise LifecycleError("probe attempt was not found")
        if attempt.state is not AttemptState.RUNNING:
            raise LifecycleError("probe execution requires a running attempt")
        return attempt

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise LifecycleError("probe execution clock must include a timezone")
        return value
