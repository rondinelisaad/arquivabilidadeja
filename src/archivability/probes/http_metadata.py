from __future__ import annotations

import uuid
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from archivability.evidence.models import Observation
from archivability.probes.http import HttpFetchResult, PinnedHttpClient
from archivability.probes.models import (
    ProbeContext,
    ProbeValidationError,
)


class HttpFetcher(Protocol):
    def fetch(self, context: ProbeContext) -> HttpFetchResult: ...


Clock = Callable[[], datetime]
IdentifierFactory = Callable[[], str]
_MAX_DISCOVERED_LINKS = 1000
_MAX_XML_LOCATIONS = 1000


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
    tool_version = "0.2.0"

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
        homepage = self._client.fetch(context)
        request = context.request
        if homepage.redirect_count > request.max_redirects:
            raise ProbeValidationError("HTTP result exceeded the approved redirect limit")
        response = homepage.response
        headers: dict[str, list[str]] = {}
        for name, value in response.headers:
            headers.setdefault(name, []).append(value)
        content_type = self._content_type(headers)
        html = self._html_summary(homepage, content_type)
        robots = self._optional_fetch(context, "/robots.txt", kind="robots")
        sitemap = self._optional_fetch(context, "/sitemap.xml", kind="sitemap")
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
            "robots": robots,
            "sitemap": sitemap,
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
        return (observation,)

    def _optional_fetch(
        self, context: ProbeContext, path: str, *, kind: str
    ) -> dict[str, object]:
        parsed = urlsplit(context.target.normalized_uri)
        authority = parsed.hostname or ""
        if parsed.port is not None:
            authority = f"{authority}:{parsed.port}"
        uri = f"{parsed.scheme}://{authority}{path}"
        try:
            target = context.policy.approve(uri, context.resolver)
            child = ProbeContext(
                request=context.request,
                target=target,
                policy=context.policy,
                resolver=context.resolver,
            )
            result = self._client.fetch(child)
        except Exception:
            return {
                "outcome": "unavailable",
                "status_code": None,
                "parseable": None,
                "item_count": 0,
                "truncated": False,
                "elapsed_milliseconds": None,
            }
        body = result.response.body
        parseable = False
        count = 0
        if 200 <= result.response.status_code < 300 and not result.response.truncated:
            if kind == "robots":
                parseable, count = self._parse_robots(body)
            else:
                parseable, count = self._parse_sitemap(body)
        return {
            "outcome": "available" if 200 <= result.response.status_code < 300 else "http_error",
            "status_code": result.response.status_code,
            "parseable": parseable,
            "item_count": count,
            "truncated": result.response.truncated,
            "elapsed_milliseconds": round(result.elapsed_milliseconds, 3),
        }

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
    def _html_summary(result: HttpFetchResult, content_type: str | None) -> dict[str, object]:
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
            return summary
        parser = _HomepageParser(result.target.normalized_uri)
        try:
            parser.feed(result.response.body.decode("utf-8", errors="replace"))
            parser.close()
        except Exception:
            summary["parseable"] = False
            return summary
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
        return summary

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
