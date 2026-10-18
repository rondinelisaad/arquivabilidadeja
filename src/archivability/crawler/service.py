from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol

from archivability.crawler.frontier import (
    BoundedCrawlFrontier,
    CrawlError,
    CrawlLimits,
    CrawlSummary,
    CrawlTarget,
)
from archivability.storage.audit import AuditContext

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class CrawlEventRecorder(Protocol):
    def record_bounded_crawl_event(
        self,
        *,
        analysis_id: str,
        attempt_id: str,
        status: str,
        limits: CrawlLimits | None,
        summary: CrawlSummary | None,
        error_code: str | None,
        audit: AuditContext,
    ) -> None: ...


class AuditedBoundedCrawl:
    """Wrap a frontier with one sanitized lifecycle event per terminal outcome."""

    def __init__(
        self,
        *,
        analysis_id: str,
        attempt_id: str,
        entry_uri: str,
        recorder: CrawlEventRecorder,
        limits: CrawlLimits = CrawlLimits(),
        audit: AuditContext = AuditContext(),
    ) -> None:
        for name, value in (("analysis_id", analysis_id), ("attempt_id", attempt_id)):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise CrawlError(f"{name} is invalid")
        self._analysis_id = analysis_id
        self._attempt_id = attempt_id
        self._recorder = recorder
        self._audit = audit
        self._frontier = BoundedCrawlFrontier(entry_uri, limits=limits)
        self._terminal_recorded = False
        self._recorder.record_bounded_crawl_event(
            analysis_id=analysis_id,
            attempt_id=attempt_id,
            status="started",
            limits=limits,
            summary=None,
            error_code=None,
            audit=audit,
        )

    def claim_next(self) -> CrawlTarget | None:
        self._ensure_open()
        target = self._frontier.claim_next()
        if target is None:
            self._record_success()
        return target

    def record_response(
        self,
        target: CrawlTarget,
        *,
        status_code: int,
        response_bytes: int,
        elapsed_milliseconds: float,
        discovered_links: Iterable[str] = (),
    ) -> None:
        self._ensure_open()
        self._frontier.record_response(
            target,
            status_code=status_code,
            response_bytes=response_bytes,
            elapsed_milliseconds=elapsed_milliseconds,
            discovered_links=discovered_links,
        )

    def record_failure(
        self,
        target: CrawlTarget,
        *,
        failure_class: str,
        elapsed_milliseconds: float,
    ) -> None:
        self._ensure_open()
        self._frontier.record_failure(
            target,
            failure_class=failure_class,
            elapsed_milliseconds=elapsed_milliseconds,
        )

    def discard(self, target: CrawlTarget, *, reason: str) -> None:
        self._ensure_open()
        self._frontier.discard(target, reason=reason)

    def abort(self, error_code: str) -> None:
        if self._terminal_recorded:
            raise CrawlError("crawl terminal event was already recorded")
        if not isinstance(error_code, str) or not _ERROR_CODE.fullmatch(error_code):
            raise CrawlError("crawl error code is invalid")
        self._recorder.record_bounded_crawl_event(
            analysis_id=self._analysis_id,
            attempt_id=self._attempt_id,
            status="failed",
            limits=None,
            summary=None,
            error_code=error_code,
            audit=self._audit,
        )
        self._terminal_recorded = True

    def summary(self) -> CrawlSummary:
        return self._frontier.summary()

    def _record_success(self) -> None:
        if self._terminal_recorded:
            return
        summary = self._frontier.summary()
        if summary.termination_reason == "in_progress":
            raise CrawlError("crawl cannot complete while work remains")
        self._recorder.record_bounded_crawl_event(
            analysis_id=self._analysis_id,
            attempt_id=self._attempt_id,
            status="succeeded",
            limits=None,
            summary=summary,
            error_code=None,
            audit=self._audit,
        )
        self._terminal_recorded = True

    def _ensure_open(self) -> None:
        if self._terminal_recorded:
            raise CrawlError("crawl session is already terminal")
