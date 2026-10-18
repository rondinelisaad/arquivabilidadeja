from __future__ import annotations

import hashlib
import re
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

_UNSAFE_URI_CHARACTER = re.compile(r"[\x00-\x20\x7f\\]")
_FAILURE_CLASSES = frozenset({"dns", "network", "policy", "timeout", "tls", "tool"})


class CrawlError(ValueError):
    """Raised when crawl inputs or state transitions are invalid."""


@dataclass(frozen=True, slots=True)
class CrawlLimits:
    max_depth: int = 2
    max_urls: int = 100
    max_response_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 20 * 1024 * 1024
    max_total_elapsed_milliseconds: float = 60_000
    max_links_per_document: int = 1_000

    def __post_init__(self) -> None:
        self._integer("max_depth", self.max_depth, 0, 5)
        self._integer("max_urls", self.max_urls, 1, 100)
        self._integer("max_response_bytes", self.max_response_bytes, 1, 10 * 1024 * 1024)
        self._integer("max_total_bytes", self.max_total_bytes, 1, 100 * 1024 * 1024)
        if self.max_response_bytes > self.max_total_bytes:
            raise CrawlError("per-response bytes cannot exceed the total byte budget")
        if (
            isinstance(self.max_total_elapsed_milliseconds, bool)
            or not isinstance(self.max_total_elapsed_milliseconds, (int, float))
            or not 100 <= self.max_total_elapsed_milliseconds <= 300_000
        ):
            raise CrawlError("total elapsed budget must be between 100 and 300000 ms")
        self._integer("max_links_per_document", self.max_links_per_document, 1, 10_000)

    @staticmethod
    def _integer(name: str, value: int, minimum: int, maximum: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise CrawlError(f"{name} must be between {minimum} and {maximum}")

    def to_dict(self) -> dict[str, int | float]:
        return {
            "max_depth": self.max_depth,
            "max_urls": self.max_urls,
            "max_response_bytes": self.max_response_bytes,
            "max_total_bytes": self.max_total_bytes,
            "max_total_elapsed_milliseconds": self.max_total_elapsed_milliseconds,
            "max_links_per_document": self.max_links_per_document,
        }


@dataclass(frozen=True, slots=True)
class CrawlTarget:
    uri: str
    depth: int
    max_response_bytes: int
    max_elapsed_milliseconds: float


@dataclass(frozen=True, slots=True)
class CrawlSummary:
    discovered_references: int
    discovered_unique_internal: int
    enqueued: int
    visited: int
    successful: int
    broken_internal: int
    edge_count: int
    maximum_depth_reached: int
    response_bytes: int
    elapsed_milliseconds: float
    status_classes: tuple[tuple[str, int], ...]
    failures: tuple[tuple[str, int], ...]
    discarded: tuple[tuple[str, int], ...]
    termination_reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "discovered_references": self.discovered_references,
            "discovered_unique_internal": self.discovered_unique_internal,
            "enqueued": self.enqueued,
            "visited": self.visited,
            "successful": self.successful,
            "broken_internal": self.broken_internal,
            "edge_count": self.edge_count,
            "maximum_depth_reached": self.maximum_depth_reached,
            "response_bytes": self.response_bytes,
            "elapsed_milliseconds": self.elapsed_milliseconds,
            "status_classes": dict(self.status_classes),
            "failures": dict(self.failures),
            "discarded": dict(self.discarded),
            "termination_reason": self.termination_reason,
        }


class BoundedCrawlFrontier:
    """Sequential same-origin frontier that exposes only aggregate summaries."""

    def __init__(self, entry_uri: str, *, limits: CrawlLimits = CrawlLimits()) -> None:
        if not isinstance(limits, CrawlLimits):
            raise CrawlError("limits must be CrawlLimits")
        self._limits = limits
        self._entry_uri = self._canonical_entry(entry_uri)
        parsed = urlsplit(self._entry_uri)
        self._origin = (
            parsed.scheme,
            parsed.hostname,
            parsed.port or self._default_port(parsed.scheme),
        )
        self._queue: deque[tuple[str, int]] = deque([(self._entry_uri, 0)])
        self._seen_internal_digests = {self._digest(self._entry_uri)}
        self._active: CrawlTarget | None = None
        self._discovered_references = 0
        self._discovered_unique_internal = 1
        self._enqueued = 1
        self._visited = 0
        self._successful = 0
        self._broken_internal = 0
        self._edge_count = 0
        self._maximum_depth_reached = 0
        self._response_bytes = 0
        self._elapsed_milliseconds = 0.0
        self._status_classes: Counter[str] = Counter()
        self._failures: Counter[str] = Counter()
        self._discarded: Counter[str] = Counter()
        self._termination_reason: str | None = None
        self._soft_limit_reason: str | None = None

    @property
    def limits(self) -> CrawlLimits:
        return self._limits

    def claim_next(self) -> CrawlTarget | None:
        if self._active is not None:
            raise CrawlError("the sequential frontier already has an active target")
        self._apply_hard_budget_stop()
        if self._termination_reason is not None:
            return None
        if not self._queue:
            self._termination_reason = self._soft_limit_reason or "complete"
            return None
        uri, depth = self._queue.popleft()
        self._active = CrawlTarget(
            uri=uri,
            depth=depth,
            max_response_bytes=min(
                self._limits.max_response_bytes,
                self._limits.max_total_bytes - self._response_bytes,
            ),
            max_elapsed_milliseconds=(
                self._limits.max_total_elapsed_milliseconds - self._elapsed_milliseconds
            ),
        )
        return self._active

    def record_response(
        self,
        target: CrawlTarget,
        *,
        status_code: int,
        response_bytes: int,
        elapsed_milliseconds: float,
        discovered_links: Iterable[str] = (),
    ) -> None:
        self._require_active(target)
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 100 <= status_code <= 599
        ):
            raise CrawlError("HTTP status must be between 100 and 599")
        self._validate_consumption(target, response_bytes, elapsed_milliseconds)
        links = self._bounded_links(discovered_links)
        self._record_consumption(target, response_bytes, elapsed_milliseconds)
        self._status_classes[f"{status_code // 100}xx"] += 1
        if 200 <= status_code < 400:
            self._successful += 1
        elif target.depth > 0 and status_code >= 400:
            self._broken_internal += 1
        self._active = None
        if 200 <= status_code < 300:
            self._discover(target, links)
        self._apply_hard_budget_stop()

    def record_failure(
        self,
        target: CrawlTarget,
        *,
        failure_class: str,
        elapsed_milliseconds: float,
    ) -> None:
        self._require_active(target)
        if failure_class not in _FAILURE_CLASSES:
            raise CrawlError("crawl failure class is invalid")
        self._validate_consumption(target, 0, elapsed_milliseconds)
        self._record_consumption(target, 0, elapsed_milliseconds)
        self._failures[failure_class] += 1
        if target.depth > 0:
            self._broken_internal += 1
        self._active = None
        self._apply_hard_budget_stop()

    def summary(self) -> CrawlSummary:
        reason = self._termination_reason
        if reason is None and self._active is None and not self._queue:
            reason = self._soft_limit_reason or "complete"
        return CrawlSummary(
            discovered_references=self._discovered_references,
            discovered_unique_internal=self._discovered_unique_internal,
            enqueued=self._enqueued,
            visited=self._visited,
            successful=self._successful,
            broken_internal=self._broken_internal,
            edge_count=self._edge_count,
            maximum_depth_reached=self._maximum_depth_reached,
            response_bytes=self._response_bytes,
            elapsed_milliseconds=round(self._elapsed_milliseconds, 3),
            status_classes=tuple(sorted(self._status_classes.items())),
            failures=tuple(sorted(self._failures.items())),
            discarded=tuple(sorted(self._discarded.items())),
            termination_reason=reason or "in_progress",
        )

    def _discover(self, parent: CrawlTarget, candidates: tuple[str, ...]) -> None:
        for candidate in candidates:
            self._discovered_references += 1
            try:
                normalized = self._canonical_candidate(parent.uri, candidate)
            except CrawlError:
                self._discarded["invalid"] += 1
                continue
            if normalized is None:
                self._discarded["external_origin"] += 1
                continue
            self._edge_count += 1
            digest = self._digest(normalized)
            if digest in self._seen_internal_digests:
                self._discarded["duplicate"] += 1
                continue
            self._seen_internal_digests.add(digest)
            self._discovered_unique_internal += 1
            depth = parent.depth + 1
            if depth > self._limits.max_depth:
                self._discarded["depth_limit"] += 1
                continue
            if self._enqueued >= self._limits.max_urls:
                self._discarded["url_budget"] += 1
                self._soft_limit_reason = "url_budget"
                continue
            self._queue.append((normalized, depth))
            self._enqueued += 1

    def _apply_hard_budget_stop(self) -> None:
        if self._termination_reason is not None:
            return
        reason = None
        if self._response_bytes >= self._limits.max_total_bytes:
            reason = "byte_budget"
        elif self._elapsed_milliseconds >= self._limits.max_total_elapsed_milliseconds:
            reason = "time_budget"
        if reason is None:
            return
        self._termination_reason = reason
        self._discarded[reason] += len(self._queue)
        self._queue.clear()

    def _record_consumption(
        self,
        target: CrawlTarget,
        response_bytes: int,
        elapsed_milliseconds: float,
    ) -> None:
        self._visited += 1
        self._maximum_depth_reached = max(self._maximum_depth_reached, target.depth)
        self._response_bytes += response_bytes
        self._elapsed_milliseconds += elapsed_milliseconds

    def _require_active(self, target: CrawlTarget) -> None:
        if not isinstance(target, CrawlTarget) or self._active != target:
            raise CrawlError("crawl result does not match the active target")

    @staticmethod
    def _validate_consumption(
        target: CrawlTarget,
        response_bytes: int,
        elapsed_milliseconds: float,
    ) -> None:
        if (
            isinstance(response_bytes, bool)
            or not isinstance(response_bytes, int)
            or not 0 <= response_bytes <= target.max_response_bytes
        ):
            raise CrawlError("response bytes exceed the active target budget")
        if (
            isinstance(elapsed_milliseconds, bool)
            or not isinstance(elapsed_milliseconds, (int, float))
            or not 0 <= elapsed_milliseconds <= target.max_elapsed_milliseconds
        ):
            raise CrawlError("elapsed time exceeds the active target budget")

    def _bounded_links(self, links: Iterable[str]) -> tuple[str, ...]:
        if isinstance(links, (str, bytes)) or not isinstance(links, Iterable):
            raise CrawlError("discovered links must be an iterable of strings")
        values: list[str] = []
        for value in links:
            if len(values) >= self._limits.max_links_per_document:
                self._discarded["document_link_limit"] += 1
                break
            if not isinstance(value, str):
                raise CrawlError("discovered links must contain only strings")
            values.append(value)
        return tuple(values)

    @classmethod
    def _canonical_entry(cls, uri: str) -> str:
        normalized = cls._canonical_absolute(uri)
        if normalized is None:
            raise CrawlError("crawl entry URI must be absolute HTTP(S)")
        return normalized

    def _canonical_candidate(self, parent_uri: str, candidate: str) -> str | None:
        if not candidate or len(candidate) > 2048 or _UNSAFE_URI_CHARACTER.search(candidate):
            raise CrawlError("discovered URI is invalid")
        try:
            absolute = urljoin(parent_uri, candidate)
        except ValueError as exc:
            raise CrawlError("discovered URI cannot be resolved") from exc
        normalized = self._canonical_absolute(absolute)
        if normalized is None:
            raise CrawlError("discovered URI is not HTTP(S)")
        parsed = urlsplit(normalized)
        origin = (
            parsed.scheme,
            parsed.hostname,
            parsed.port or self._default_port(parsed.scheme),
        )
        return normalized if origin == self._origin else None

    @classmethod
    def _canonical_absolute(cls, uri: str) -> str | None:
        if not isinstance(uri, str) or not uri or len(uri) > 2048:
            raise CrawlError("URI must contain between 1 and 2048 characters")
        if _UNSAFE_URI_CHARACTER.search(uri):
            raise CrawlError("URI contains unsafe characters")
        try:
            parsed = urlsplit(uri)
            port = parsed.port
        except ValueError as exc:
            raise CrawlError("URI authority is invalid") from exc
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or parsed.hostname is None:
            return None
        if parsed.username is not None or parsed.password is not None:
            raise CrawlError("URI credentials are forbidden")
        try:
            hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise CrawlError("URI hostname is invalid") from exc
        effective_port = port or cls._default_port(scheme)
        host = f"[{hostname}]" if ":" in hostname else hostname
        authority = (
            host if effective_port == cls._default_port(scheme) else f"{host}:{effective_port}"
        )
        return urlunsplit((scheme, authority, parsed.path or "/", parsed.query, ""))

    @staticmethod
    def _default_port(scheme: str) -> int:
        return 443 if scheme == "https" else 80

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
