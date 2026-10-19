from __future__ import annotations

import re
import ssl
import time
import uuid
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from archivability.crawler import (
    AuditedBoundedCrawl,
    CrawlEventRecorder,
    CrawlLimits,
    CrawlTarget,
    RobotsError,
    RobotsRules,
)
from archivability.evidence.models import Observation
from archivability.probes.http import (
    HttpFetchResult,
    HttpTransportError,
    PinnedHttpClient,
)
from archivability.probes.models import (
    ProbeContext,
    ProbeRequest,
    ProbeValidationError,
)
from archivability.storage.audit import AuditContext


class HttpFetcher(Protocol):
    def fetch(self, context: ProbeContext) -> HttpFetchResult: ...


class CrawlHttpFetcher(HttpFetcher, Protocol):
    def fetch_same_origin(self, context: ProbeContext) -> HttpFetchResult: ...


Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]
IdentifierFactory = Callable[[], str]
_MAX_DISCOVERED_LINKS = 1000
_MAX_XML_LOCATIONS = 1000
_PROBE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


@dataclass(frozen=True, slots=True)
class _AuxiliaryFetch:
    summary: dict[str, object]
    result: HttpFetchResult | None
    failed: bool = False


class _HomepageParser(HTMLParser):
    def __init__(self, base_uri: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_uri = base_uri
        self.internal_links = 0
        self.external_links = 0
        self.feed_links = 0
        self.canonical_present = False
        self.total_links = 0
        self.truncated = False
        self.discovered_links: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = {name.lower(): value for name, value in attrs if value is not None}
        if tag.lower() == "link":
            relations = {item.lower() for item in attributes.get("rel", "").split()}
            mime = attributes.get("type", "").lower()
            if "canonical" in relations:
                self.canonical_present = True
            if "alternate" in relations and mime in {
                "application/rss+xml",
                "application/atom+xml",
            }:
                self.feed_links += 1
        candidate = attributes.get("href") if tag.lower() in {"a", "link"} else None
        if candidate is None and tag.lower() in {"img", "script", "iframe", "source"}:
            candidate = attributes.get("src")
        if candidate is None:
            return
        if self.total_links >= _MAX_DISCOVERED_LINKS:
            self.truncated = True
            return
        absolute = urljoin(self._base_uri, candidate)
        parsed = urlsplit(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return
        base = urlsplit(self._base_uri)
        self.discovered_links.append(absolute)
        self.total_links += 1
        if (parsed.scheme, parsed.hostname, parsed.port) == (
            base.scheme,
            base.hostname,
            base.port,
        ):
            self.internal_links += 1
        else:
            self.external_links += 1


class HttpMetadataProbe:
    """Collect bounded homepage, robots and sitemap aggregates without raw bodies."""

    probe_id = "http-metadata"
    tool_name = "arquivabilidade-http"
    tool_version = "0.3.0"

    def __init__(
        self,
        client: HttpFetcher | None = None,
        *,
        clock: Clock = lambda: datetime.now(timezone.utc),
        monotonic_clock: MonotonicClock = time.monotonic,
        observation_id_factory: IdentifierFactory = lambda: str(uuid.uuid4()),
        crawl_limits: CrawlLimits = CrawlLimits(),
        crawl_event_recorder: CrawlEventRecorder | None = None,
        crawl_audit: AuditContext = AuditContext(),
        probe_id: str = "http-metadata",
    ) -> None:
        if not isinstance(probe_id, str) or not _PROBE_ID_PATTERN.fullmatch(probe_id):
            raise ProbeValidationError("probe_id is invalid")
        self.probe_id = probe_id
        self._client = client if client is not None else PinnedHttpClient()
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._observation_id_factory = observation_id_factory
        self._crawl_limits = crawl_limits
        self._crawl_event_recorder = crawl_event_recorder
        self._crawl_audit = crawl_audit
        if crawl_event_recorder is not None and not callable(
            getattr(self._client, "fetch_same_origin", None)
        ):
            raise ProbeValidationError(
                "bounded crawl requires a same-origin HTTP fetcher"
            )

    def execute(self, context: ProbeContext) -> tuple[Observation, ...]:
        if self._crawl_event_recorder is not None:
            context = self._bounded_crawl_context(context)
        homepage = self._client.fetch(context)
        request = context.request
        if homepage.redirect_count > request.max_redirects:
            raise ProbeValidationError("HTTP result exceeded the approved redirect limit")
        response = homepage.response
        headers: dict[str, list[str]] = {}
        for name, value in response.headers:
            headers.setdefault(name, []).append(value)
        content_type = self._content_type(headers)
        html, homepage_links = self._html_analysis(homepage, content_type)
        robots_fetch = self._optional_fetch(context, "/robots.txt", kind="robots")
        sitemap_fetch = self._optional_fetch(context, "/sitemap.xml", kind="sitemap")
        payload = {
            "status_code": response.status_code,
            "headers": headers,
            "response_bytes_observed": len(response.body),
            "response_byte_limit": request.max_response_bytes,
            "response_truncated": response.truncated,
            "redirect_count": homepage.redirect_count,
            "final_transport_secure": homepage.target.scheme == "https",
            "elapsed_milliseconds": round(homepage.elapsed_milliseconds, 3),
            "content_type": content_type,
            "format_family": self._format_family(content_type),
            "html": html,
            "robots": robots_fetch.summary,
            "sitemap": sitemap_fetch.summary,
        }
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
            payload_schema_version="1.2",
            payload=payload,
            truncated=response.truncated or html["truncated"],
        )
        if self._crawl_event_recorder is None:
            return (observation,)
        crawl_observation = self._crawl(
            context,
            homepage,
            homepage_links,
            robots_fetch,
        )
        return (observation, crawl_observation)

    def _optional_fetch(
        self, context: ProbeContext, path: str, *, kind: str
    ) -> _AuxiliaryFetch:
        parsed = urlsplit(context.target.normalized_uri)
        authority = parsed.hostname or ""
        if ":" in authority:
            authority = f"[{authority}]"
        if parsed.port is not None:
            authority = f"{authority}:{parsed.port}"
        uri = f"{parsed.scheme}://{authority}{path}"
        try:
            target = context.policy.approve(uri, context.resolver)
            child_request = context.request
            if kind == "robots":
                child_request = replace(
                    child_request,
                    max_response_bytes=min(
                        child_request.max_response_bytes,
                        512 * 1024,
                    ),
                )
            child = ProbeContext(
                request=child_request,
                target=target,
                policy=context.policy,
                resolver=context.resolver,
            )
            result = self._client.fetch(child)
        except Exception:
            return _AuxiliaryFetch(
                summary={
                    "outcome": "unavailable",
                    "status_code": None,
                    "parseable": None,
                    "item_count": 0,
                    "truncated": False,
                    "elapsed_milliseconds": None,
                },
                result=None,
                failed=True,
            )
        body = result.response.body
        parseable = False
        count = 0
        if 200 <= result.response.status_code < 300 and not result.response.truncated:
            if kind == "robots":
                parseable, count = self._parse_robots(body)
            else:
                parseable, count = self._parse_sitemap(body)
        return _AuxiliaryFetch(
            summary={
                "outcome": (
                    "available"
                    if 200 <= result.response.status_code < 300
                    else "http_error"
                ),
                "status_code": result.response.status_code,
                "parseable": parseable,
                "item_count": count,
                "truncated": result.response.truncated,
                "elapsed_milliseconds": round(result.elapsed_milliseconds, 3),
            },
            result=result,
        )

    @staticmethod
    def _parse_robots(body: bytes) -> tuple[bool, int]:
        try:
            text = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return False, 0
        directives = 0
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            name, separator, _value = line.partition(":")
            if not separator or not name.strip():
                return False, directives
            directives += 1
        return True, directives

    @staticmethod
    def _parse_sitemap(body: bytes) -> tuple[bool, int]:
        upper = body[:4096].upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            return False, 0
        try:
            root = ElementTree.fromstring(body)
        except (ElementTree.ParseError, ValueError):
            return False, 0
        count = sum(
            1
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1].lower() == "loc"
        )
        return True, min(count, _MAX_XML_LOCATIONS)

    @staticmethod
    def _html_analysis(
        result: HttpFetchResult,
        content_type: str | None,
    ) -> tuple[dict[str, object], tuple[str, ...]]:
        summary: dict[str, object] = {
            "parseable": None,
            "link_count": 0,
            "internal_link_count": 0,
            "external_link_count": 0,
            "feed_link_count": 0,
            "canonical_present": False,
            "truncated": False,
        }
        if content_type != "text/html":
            return summary, ()
        parser = _HomepageParser(result.target.normalized_uri)
        try:
            parser.feed(result.response.body.decode("utf-8", errors="replace"))
            parser.close()
        except Exception:
            summary["parseable"] = False
            return summary, ()
        summary.update(
            {
                "parseable": True,
                "link_count": parser.total_links,
                "internal_link_count": parser.internal_links,
                "external_link_count": parser.external_links,
                "feed_link_count": parser.feed_links,
                "canonical_present": parser.canonical_present,
                "truncated": parser.truncated,
            }
        )
        return summary, tuple(parser.discovered_links)

    def _bounded_crawl_context(self, context: ProbeContext) -> ProbeContext:
        request = replace(
            context.request,
            timeout_seconds=min(
                context.request.timeout_seconds,
                self._crawl_limits.max_total_elapsed_milliseconds / 1000,
            ),
            max_response_bytes=min(
                context.request.max_response_bytes,
                self._crawl_limits.max_response_bytes,
            ),
        )
        return ProbeContext(
            request=request,
            target=context.target,
            policy=context.policy,
            resolver=context.resolver,
            redirect_count=context.redirect_count,
        )

    def _crawl(
        self,
        context: ProbeContext,
        homepage: HttpFetchResult,
        homepage_links: tuple[str, ...],
        robots_fetch: _AuxiliaryFetch,
    ) -> Observation:
        assert self._crawl_event_recorder is not None
        crawl = AuditedBoundedCrawl(
            analysis_id=context.request.analysis_id,
            attempt_id=context.request.attempt_id,
            entry_uri=context.request.subject_uri,
            recorder=self._crawl_event_recorder,
            limits=self._crawl_limits,
            audit=self._crawl_audit,
        )
        try:
            robots = self._robots_rules(robots_fetch)
            seed_decision = robots.decide(context.request.subject_uri)
            seed = crawl.claim_next()
            if seed is None:
                raise ProbeValidationError("bounded crawl did not expose its seed")
            crawl.record_response(
                seed,
                status_code=homepage.response.status_code,
                response_bytes=len(homepage.response.body),
                elapsed_milliseconds=homepage.elapsed_milliseconds,
                discovered_links=homepage_links,
            )
            while (target := crawl.claim_next()) is not None:
                self._crawl_target(crawl, context, robots, target)
            summary = crawl.summary()
        except Exception:
            crawl.abort("CRAWL_EXECUTION_FAILED")
            raise
        return Observation.create(
            observation_id=self._observation_id_factory(),
            analysis_id=context.request.analysis_id,
            attempt_id=context.request.attempt_id,
            kind="bounded_crawl",
            subject_uri=context.request.subject_uri,
            observed_at=self._clock(),
            probe_id=self.probe_id,
            tool_name=self.tool_name,
            tool_version=self.tool_version,
            payload_schema_version="1.0",
            payload={
                "limits": self._crawl_limits.to_dict(),
                "robots": seed_decision.to_dict(),
                "summary": summary.to_dict(),
            },
            truncated=summary.termination_reason != "complete",
        )

    def _crawl_target(
        self,
        crawl: AuditedBoundedCrawl,
        root_context: ProbeContext,
        robots: RobotsRules,
        target: CrawlTarget,
    ) -> None:
        if not robots.decide(target.uri).allowed:
            crawl.discard(target, reason="robots_policy")
            return
        if target.max_elapsed_milliseconds < 100:
            crawl.record_failure(
                target,
                failure_class="timeout",
                elapsed_milliseconds=target.max_elapsed_milliseconds,
            )
            return
        started_at = self._monotonic_clock()
        try:
            approved = root_context.policy.approve(target.uri, root_context.resolver)
            child = ProbeContext(
                request=self._crawl_request(root_context.request, target),
                target=approved,
                policy=root_context.policy,
                resolver=root_context.resolver,
            )
            fetch_same_origin = getattr(self._client, "fetch_same_origin")
            result = fetch_same_origin(child)
        except (HttpTransportError, ProbeValidationError, TimeoutError) as exc:
            elapsed = min(
                target.max_elapsed_milliseconds,
                max(0.0, (self._monotonic_clock() - started_at) * 1000),
            )
            crawl.record_failure(
                target,
                failure_class=self._failure_class(exc),
                elapsed_milliseconds=elapsed,
            )
            return
        elapsed = max(
            result.elapsed_milliseconds,
            max(0.0, (self._monotonic_clock() - started_at) * 1000),
        )
        if elapsed > target.max_elapsed_milliseconds:
            crawl.record_failure(
                target,
                failure_class="timeout",
                elapsed_milliseconds=target.max_elapsed_milliseconds,
            )
            return
        content_type = self._content_type(self._headers(result))
        _, links = self._html_analysis(result, content_type)
        crawl.record_response(
            target,
            status_code=result.response.status_code,
            response_bytes=len(result.response.body),
            elapsed_milliseconds=elapsed,
            discovered_links=links,
        )

    def _robots_rules(self, value: _AuxiliaryFetch) -> RobotsRules:
        if value.failed or value.result is None or value.result.response.truncated:
            return RobotsRules.unreachable()
        try:
            return RobotsRules.from_http(
                value.result.response.status_code,
                value.result.response.body,
            )
        except RobotsError:
            return RobotsRules.unreachable()

    @staticmethod
    def _crawl_request(request: ProbeRequest, target: CrawlTarget) -> ProbeRequest:
        return replace(
            request,
            timeout_seconds=min(
                request.timeout_seconds,
                target.max_elapsed_milliseconds / 1000,
            ),
            max_response_bytes=min(
                request.max_response_bytes,
                target.max_response_bytes,
            ),
        )

    @staticmethod
    def _failure_class(exc: Exception) -> str:
        if isinstance(exc, ProbeValidationError):
            return "policy"
        if isinstance(exc, TimeoutError):
            return "timeout"
        if isinstance(exc, HttpTransportError):
            cause = exc.__cause__
            if isinstance(cause, TimeoutError):
                return "timeout"
            if isinstance(cause, ssl.SSLError):
                return "tls"
            return "network"
        return "tool"

    @staticmethod
    def _headers(result: HttpFetchResult) -> dict[str, list[str]]:
        headers: dict[str, list[str]] = {}
        for name, value in result.response.headers:
            headers.setdefault(name, []).append(value)
        return headers

    @staticmethod
    def _content_type(headers: dict[str, list[str]]) -> str | None:
        values = headers.get("content-type", [])
        if not values:
            return None
        value = values[0].split(";", 1)[0].strip().lower()
        return value if value else None

    @staticmethod
    def _format_family(content_type: str | None) -> str:
        if content_type is None:
            return "unknown"
        if content_type == "text/html":
            return "html"
        if content_type in {"application/xml", "text/xml"} or content_type.endswith("+xml"):
            return "xml"
        if content_type == "text/css":
            return "css"
        if "javascript" in content_type:
            return "javascript"
        if content_type.startswith("image/"):
            return "image"
        if content_type == "application/pdf":
            return "pdf"
        if content_type.startswith("audio/"):
            return "audio"
        if content_type.startswith("video/"):
            return "video"
        if content_type.startswith("font/"):
            return "font"
        return "other"
