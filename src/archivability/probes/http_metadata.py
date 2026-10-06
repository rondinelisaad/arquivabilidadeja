from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from archivability.evidence.models import Observation
from archivability.probes.http import HttpFetchResult, PinnedHttpClient
from archivability.probes.models import ProbeContext, ProbeValidationError


class HttpFetcher(Protocol):
    def fetch(self, context: ProbeContext) -> HttpFetchResult: ...


Clock = Callable[[], datetime]
IdentifierFactory = Callable[[], str]


class HttpMetadataProbe:
    """Captures bounded HTTP response metadata without persisting response bodies."""

    probe_id = "http-metadata"
    tool_name = "arquivabilidade-http"
    tool_version = "0.1.0"

    def __init__(
        self,
        client: HttpFetcher | None = None,
        *,
        clock: Clock = lambda: datetime.now(timezone.utc),
        observation_id_factory: IdentifierFactory = lambda: str(uuid.uuid4()),
    ) -> None:
        self._client = client if client is not None else PinnedHttpClient()
        self._clock = clock
        self._observation_id_factory = observation_id_factory

    def execute(self, context: ProbeContext) -> tuple[Observation, ...]:
        result = self._client.fetch(context)
        request = context.request
        if result.redirect_count > request.max_redirects:
            raise ProbeValidationError("HTTP result exceeded the approved redirect limit")
        response = result.response
        headers: dict[str, list[str]] = {}
        for name, value in response.headers:
            headers.setdefault(name, []).append(value)
        observation = Observation.create(
            observation_id=self._observation_id_factory(),
            analysis_id=request.analysis_id,
            attempt_id=request.attempt_id,
            kind="http_metadata",
            subject_uri=request.subject_uri,
            observed_at=self._clock(),
            probe_id=self.probe_id,
            tool_name=self.tool_name,
            tool_version=self.tool_version,
            payload_schema_version="1.1",
            payload={
                "status_code": response.status_code,
                "headers": headers,
                "response_bytes_observed": len(response.body),
                "response_byte_limit": request.max_response_bytes,
                "response_truncated": response.truncated,
                "redirect_count": result.redirect_count,
                "final_transport_secure": result.target.scheme == "https",
            },
            truncated=response.truncated,
        )
        return (observation,)
