from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from archivability.browser.models import (
    BrowserContractError,
    BrowserIsolationAttestation,
    BrowserRenderAggregate,
    BrowserRenderRequest,
    BrowserRenderResult,
)

_MAX_RESPONSE_BYTES = 128 * 1024


class HttpResponse(Protocol):
    status: int
    headers: Any

    def read(self, amount: int = -1) -> bytes: ...
    def close(self) -> None: ...


Transport = Callable[[Request, float], HttpResponse]


class LoopbackBrowserRenderer:
    """Call the isolated Playwright sidecar through a bounded loopback API."""

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:9223/render",
        *,
        transport: Transport | None = None,
    ) -> None:
        try:
            parsed = urlsplit(endpoint)
            port = parsed.port
        except ValueError as exc:
            raise BrowserContractError("browser renderer endpoint is invalid") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or port is None
            or parsed.path != "/render"
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise BrowserContractError(
                "browser renderer endpoint must be an explicit loopback /render URL"
            )
        self._endpoint = endpoint
        self._transport = transport or self._open

    def render(self, request: BrowserRenderRequest) -> BrowserRenderResult:
        body = json.dumps(
            {
                "analysis_id": request.analysis_id,
                "attempt_id": request.attempt_id,
                "subject_uri": request.subject_uri,
                "requested_at": request.requested_at.isoformat(),
                "budgets": request.budgets.to_dict(),
                "profile": request.profile.to_dict(),
            },
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        http_request = Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "User-Agent": "arquivabilidade-ja-worker/0.1",
            },
        )
        timeout = request.budgets.total_timeout_milliseconds / 1000 + 2
        try:
            response = self._transport(http_request, timeout)
            try:
                if response.status != 200:
                    raise BrowserContractError("browser renderer rejected the request")
                if response.headers.get_content_type() != "application/json":
                    raise BrowserContractError("browser renderer response type is invalid")
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
            finally:
                response.close()
        except BrowserContractError:
            raise
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise BrowserContractError("browser renderer is unavailable") from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise BrowserContractError("browser renderer response exceeds its byte limit")
        return self._result(self._decode(raw))

    @staticmethod
    def _open(request: Request, timeout: float) -> HttpResponse:
        return urlopen(request, timeout=timeout)  # noqa: S310

    @staticmethod
    def _decode(raw: bytes) -> dict[str, Any]:
        def object_pairs(pairs):
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise BrowserContractError(
                        "browser renderer response has duplicate keys"
                    )
                value[key] = item
            return value

        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=object_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
        except BrowserContractError:
            raise
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise BrowserContractError("browser renderer response is invalid JSON") from exc
        if not isinstance(value, dict):
            raise BrowserContractError("browser renderer response must be an object")
        return value

    @staticmethod
    def _result(document: dict[str, Any]) -> BrowserRenderResult:
        if set(document) != {
            "analysis_id",
            "attempt_id",
            "engine_name",
            "engine_version",
            "started_at",
            "finished_at",
            "isolation",
            "aggregate",
        }:
            raise BrowserContractError("browser renderer response fields are invalid")
        isolation = document["isolation"]
        aggregate = document["aggregate"]
        if not isinstance(isolation, dict) or not isinstance(aggregate, dict):
            raise BrowserContractError("browser renderer response contracts are invalid")
        try:
            return BrowserRenderResult(
                analysis_id=document["analysis_id"],
                attempt_id=document["attempt_id"],
                engine_name=document["engine_name"],
                engine_version=document["engine_version"],
                started_at=datetime.fromisoformat(document["started_at"]),
                finished_at=datetime.fromisoformat(document["finished_at"]),
                isolation=BrowserIsolationAttestation(**isolation),
                aggregate=BrowserRenderAggregate(**aggregate),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, BrowserContractError):
                raise
            raise BrowserContractError("browser renderer response failed validation") from exc
