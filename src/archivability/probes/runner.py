from __future__ import annotations

import re

from archivability.evidence.models import Observation
from archivability.probes.models import (
    ProbeContext,
    ProbeRequest,
    ProbeRunResult,
    ProbeValidationError,
)
from archivability.probes.ports import (
    AddressResolver,
    Probe,
    ProbeEventRecorder,
    ProbeRequestAuthorizer,
)
from archivability.probes.security import SsrfPolicy
from archivability.storage.audit import AuditContext


_PROBE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


class ProbeRunner:
    """Validates and invokes an injected probe; no network adapter is bundled."""

    def __init__(
        self,
        *,
        policy: SsrfPolicy,
        resolver: AddressResolver,
        event_recorder: ProbeEventRecorder,
        authorizer: ProbeRequestAuthorizer,
        maximum_observations: int = 100,
    ) -> None:
        self._policy = policy
        self._resolver = resolver
        self._event_recorder = event_recorder
        self._authorizer = authorizer
        if (
            isinstance(maximum_observations, bool)
            or not isinstance(maximum_observations, int)
            or not 1 <= maximum_observations <= 1000
        ):
            raise ProbeValidationError("maximum_observations must be between 1 and 1000")
        self._maximum_observations = maximum_observations

    def run(
        self,
        request: ProbeRequest,
        probe: Probe,
        *,
        audit: AuditContext = AuditContext(),
    ) -> ProbeRunResult:
        raw_probe_id = getattr(probe, "probe_id", None)
        audit_probe_id = (
            raw_probe_id
            if isinstance(raw_probe_id, str) and _PROBE_ID_PATTERN.fullmatch(raw_probe_id)
            else "invalid-probe"
        )
        try:
            self._validate_probe_metadata(probe)
            self._authorizer.authorize_probe_request(
                analysis_id=request.analysis_id,
                attempt_id=request.attempt_id,
                subject_uri=request.subject_uri,
            )
            target = self._policy.approve(request.subject_uri, self._resolver)
            context = ProbeContext(
                request=request,
                target=target,
                policy=self._policy,
                resolver=self._resolver,
            )
            observations = probe.execute(context)
            if not isinstance(observations, tuple):
                raise ProbeValidationError("probe must return a tuple of observations")
            if len(observations) > self._maximum_observations:
                raise ProbeValidationError("probe returned too many observations")
            self._validate_observations(request, probe, observations)
            result = ProbeRunResult(target=target, observations=observations)
        except Exception as exc:
            self._event_recorder.record_probe_event(
                analysis_id=request.analysis_id,
                attempt_id=request.attempt_id,
                probe_id=audit_probe_id,
                result="failure",
                error_code=type(exc).__name__.upper(),
                audit=audit,
            )
            raise
        self._event_recorder.record_probe_event(
            analysis_id=request.analysis_id,
            attempt_id=request.attempt_id,
            probe_id=probe.probe_id,
            result="success",
            error_code=None,
            audit=audit,
        )
        return result

    @staticmethod
    def _validate_probe_metadata(probe: Probe) -> None:
        if not isinstance(probe.probe_id, str) or not _PROBE_ID_PATTERN.fullmatch(
            probe.probe_id
        ):
            raise ProbeValidationError("probe_id has an invalid format")
        for field in ("tool_name", "tool_version"):
            value = getattr(probe, field)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ProbeValidationError(f"{field} must contain between 1 and 128 characters")

    @staticmethod
    def _validate_observations(
        request: ProbeRequest,
        probe: Probe,
        observations: tuple[Observation, ...],
    ) -> None:
        if not observations:
            raise ProbeValidationError("probe must produce at least one observation")
        for value in observations:
            if (
                value.analysis_id != request.analysis_id
                or value.attempt_id != request.attempt_id
                or value.subject_uri != request.subject_uri
            ):
                raise ProbeValidationError("probe observation does not match its request")
            if (
                value.probe_id != probe.probe_id
                or value.tool_name != probe.tool_name
                or value.tool_version != probe.tool_version
            ):
                raise ProbeValidationError("probe observation has invalid tool provenance")
