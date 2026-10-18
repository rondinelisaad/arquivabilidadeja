from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit

from archivability.evidence.browser_derivation import (
    BrowserDerivationError,
    validate_browser_render_observation,
)
from archivability.evidence.http_derivation import (
    HttpMetadataDerivationError,
    validate_bounded_crawl_observation,
)
from archivability.evidence.models import Evidence, EvidenceSource, Observation
from archivability.jobs.browser import BrowserJob
from archivability.jobs.collection import CollectionJob
from archivability.jobs.models import AssessmentJob, AssessmentJobState
from archivability.lifecycle.models import (
    Analysis,
    AnalysisState,
    Attempt,
    AttemptState,
)
from archivability.methodology.models import (
    AssessmentResult,
    IndicatorResult,
    MethodologyConfig,
)
from archivability.methodology.scoring import ScoringEngine
from archivability.storage.audit import AuditContext

_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


class ReportError(ValueError):
    """Raised when an analysis report cannot be safely produced."""


class ReportNotFoundError(ReportError):
    """Raised when the requested analysis does not exist."""


@dataclass(frozen=True, slots=True)
class AnalysisReportSnapshot:
    analysis: Analysis
    attempts: tuple[Attempt, ...]
    jobs: tuple[AssessmentJob | BrowserJob | CollectionJob, ...]
    indicator_results: tuple[IndicatorResult, ...]
    evidence: tuple[Evidence, ...]
    crawl_observations: tuple[Observation, ...] = ()
    browser_observations: tuple[Observation, ...] = ()

    def __post_init__(self) -> None:
        analysis_id = self.analysis.analysis_id
        if tuple(item.attempt_id for item in self.attempts) != self.analysis.attempt_ids:
            raise ReportError("report attempts do not match the analysis")
        if any(item.analysis_id != analysis_id for item in self.attempts):
            raise ReportError("report attempt belongs to another analysis")
        if any(item.analysis_id != analysis_id for item in self.jobs):
            raise ReportError("report job belongs to another analysis")
        if any(item.analysis_id != analysis_id for item in self.evidence):
            raise ReportError("report evidence belongs to another analysis")
        if any(item.analysis_id != analysis_id for item in self.indicator_results):
            raise ReportError("report result belongs to another analysis")
        attempt_ids = {item.attempt_id for item in self.attempts}
        if any(
            item.analysis_id != analysis_id
            or item.attempt_id not in attempt_ids
            or item.kind != "bounded_crawl"
            for item in self.crawl_observations
        ):
            raise ReportError("report crawl observation is inconsistent")
        crawl_attempt_ids = tuple(item.attempt_id for item in self.crawl_observations)
        if len(set(crawl_attempt_ids)) != len(crawl_attempt_ids):
            raise ReportError("report has multiple crawl observations for an attempt")
        if any(
            item.analysis_id != analysis_id
            or item.attempt_id not in attempt_ids
            or item.kind != "browser_render"
            for item in self.browser_observations
        ):
            raise ReportError("report browser observation is inconsistent")
        browser_attempt_ids = tuple(
            item.attempt_id for item in self.browser_observations
        )
        if len(set(browser_attempt_ids)) != len(browser_attempt_ids):
            raise ReportError("report has multiple browser observations for an attempt")
        evidence_by_id = {item.evidence_id: item for item in self.evidence}
        if len(evidence_by_id) != len(self.evidence):
            raise ReportError("report evidence identities must be unique")
        for result in self.indicator_results:
            for evidence_id, evidence_hash in zip(
                result.evidence_ids, result.evidence_hashes, strict=True
            ):
                evidence = evidence_by_id.get(evidence_id)
                if evidence is None or evidence.content_hash != evidence_hash:
                    raise ReportError("report result provenance is incomplete")


class AnalysisReportRepository(Protocol):
    def load_analysis_report_snapshot(
        self, analysis_id: str
    ) -> AnalysisReportSnapshot | None: ...

    def record_analysis_report_access(
        self,
        *,
        analysis_id: str,
        result: str,
        error_code: str | None,
        state: str | None,
        attempt_count: int,
        job_count: int,
        result_count: int,
        crawl_count: int,
        browser_count: int,
        audit: AuditContext,
    ) -> None: ...

    def list_owned_analysis_ids(
        self,
        owner_user_id: str,
        *,
        limit: int,
    ) -> tuple[str, ...]: ...

    def record_analysis_history_access(
        self,
        *,
        operation: str,
        analysis_id: str | None,
        result: str,
        error_code: str | None,
        item_count: int,
        change_count: int,
        audit: AuditContext,
    ) -> None: ...


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True, slots=True)
class AttemptReport:
    attempt_id: str
    sequence: int
    state: str
    failure_code: str | None
    started_at: datetime
    finished_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "sequence": self.sequence,
            "state": self.state,
            "failure_code": self.failure_code,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }


@dataclass(frozen=True, slots=True)
class JobReport:
    job_id: str
    stage: str
    state: str
    attempt_count: int
    max_attempts: int
    available_at: datetime
    completed_at: datetime | None
    error_code: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "stage": self.stage,
            "state": self.state,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "available_at": _iso(self.available_at),
            "completed_at": _iso(self.completed_at),
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class IndicatorReport:
    indicator_id: str
    state: str
    confidence: float
    score: float | None
    evidence_ids: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    measured_at: datetime | None
    measurement_method_version: str | None
    probe_ids: tuple[str, ...]
    tool_versions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "indicator_id": self.indicator_id,
            "state": self.state,
            "confidence": self.confidence,
            "score": self.score,
            "evidence_ids": list(self.evidence_ids),
            "evidence_hashes": list(self.evidence_hashes),
            "measured_at": _iso(self.measured_at),
            "measurement_method_version": self.measurement_method_version,
            "probe_ids": list(self.probe_ids),
            "tool_versions": list(self.tool_versions),
        }


@dataclass(frozen=True, slots=True)
class EvidenceSourceReport:
    observation_id: str
    content_hash: str
    probe_id: str
    tool_name: str
    tool_version: str
    observed_at: datetime

    @classmethod
    def from_source(cls, value: EvidenceSource) -> EvidenceSourceReport:
        return cls(
            observation_id=value.observation_id,
            content_hash=value.content_hash,
            probe_id=value.probe_id,
            tool_name=value.tool_name,
            tool_version=value.tool_version,
            observed_at=value.observed_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "content_hash": self.content_hash,
            "probe_id": self.probe_id,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "observed_at": _iso(self.observed_at),
        }


@dataclass(frozen=True, slots=True)
class EvidenceReport:
    evidence_id: str
    indicator_id: str
    kind: str
    method_id: str
    method_version: str
    confidence: float
    content_hash: str
    created_at: datetime
    sources: tuple[EvidenceSourceReport, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "indicator_id": self.indicator_id,
            "kind": self.kind,
            "method_id": self.method_id,
            "method_version": self.method_version,
            "confidence": self.confidence,
            "content_hash": self.content_hash,
            "created_at": _iso(self.created_at),
            "sources": [item.to_dict() for item in self.sources],
        }


@dataclass(frozen=True, slots=True)
class AnalysisProgress:
    phase: str
    attempts_used: int
    attempts_allowed: int
    jobs_total: int
    results_total: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "attempts_used": self.attempts_used,
            "attempts_allowed": self.attempts_allowed,
            "jobs_total": self.jobs_total,
            "results_total": self.results_total,
        }


@dataclass(frozen=True, slots=True)
class CrawlRunReport:
    observation_id: str
    attempt_id: str
    observed_at: datetime
    content_hash: str
    probe_id: str
    tool_name: str
    tool_version: str
    payload_schema_version: str
    truncated: bool
    limits: Mapping[str, int | float]
    robots: Mapping[str, str | int | None]
    summary: Mapping[str, object]

    @classmethod
    def from_observation(cls, value: Observation) -> CrawlRunReport:
        try:
            payload = validate_bounded_crawl_observation(value)
        except HttpMetadataDerivationError as exc:
            raise ReportError("crawl observation failed report validation") from exc
        return cls(
            observation_id=value.observation_id,
            attempt_id=value.attempt_id,
            observed_at=value.observed_at,
            content_hash=value.content_hash,
            probe_id=value.probe_id,
            tool_name=value.tool_name,
            tool_version=value.tool_version,
            payload_schema_version=value.payload_schema_version,
            truncated=value.truncated,
            limits=_freeze_report_json(payload["limits"]),
            robots=_freeze_report_json(payload["robots"]),
            summary=_freeze_report_json(payload["summary"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "attempt_id": self.attempt_id,
            "observed_at": _iso(self.observed_at),
            "content_hash": self.content_hash,
            "probe_id": self.probe_id,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "payload_schema_version": self.payload_schema_version,
            "truncated": self.truncated,
            "limits": dict(self.limits),
            "robots": dict(self.robots),
            "summary": {
                **self.summary,
                "status_classes": dict(self.summary["status_classes"]),
                "failures": dict(self.summary["failures"]),
                "discarded": dict(self.summary["discarded"]),
            },
        }


@dataclass(frozen=True, slots=True)
class BrowserRunReport:
    observation_id: str
    attempt_id: str
    observed_at: datetime
    content_hash: str
    probe_id: str
    tool_name: str
    tool_version: str
    payload_schema_version: str
    truncated: bool
    profile_id: str
    limits: Mapping[str, int]
    summary: Mapping[str, int | str]

    @classmethod
    def from_observation(cls, value: Observation) -> BrowserRunReport:
        try:
            budgets, profile, aggregate = validate_browser_render_observation(value)
        except BrowserDerivationError as exc:
            raise ReportError("browser observation failed report validation") from exc
        aggregate_document = aggregate.to_dict()
        for field in (
            "raw_dom_hash",
            "rendered_dom_hash",
            "raw_url_set_hash",
            "rendered_url_set_hash",
        ):
            aggregate_document.pop(field)
        return cls(
            observation_id=value.observation_id,
            attempt_id=value.attempt_id,
            observed_at=value.observed_at,
            content_hash=value.content_hash,
            probe_id=value.probe_id,
            tool_name=value.tool_name,
            tool_version=value.tool_version,
            payload_schema_version=value.payload_schema_version,
            truncated=value.truncated,
            profile_id=profile.profile_id,
            limits=_freeze_report_json(budgets.to_dict()),
            summary=_freeze_report_json(aggregate_document),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "attempt_id": self.attempt_id,
            "observed_at": _iso(self.observed_at),
            "content_hash": self.content_hash,
            "probe_id": self.probe_id,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "payload_schema_version": self.payload_schema_version,
            "truncated": self.truncated,
            "profile_id": self.profile_id,
            "limits": dict(self.limits),
            "summary": dict(self.summary),
        }


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    report_schema_version: str
    analysis_id: str
    target_origin: str
    methodology_id: str
    methodology_version: str
    state: str
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    progress: AnalysisProgress
    attempts: tuple[AttemptReport, ...]
    jobs: tuple[JobReport, ...]
    crawl_runs: tuple[CrawlRunReport, ...]
    browser_runs: tuple[BrowserRunReport, ...]
    assessment: AssessmentResult | None
    indicator_results: tuple[IndicatorReport, ...]
    evidence: tuple[EvidenceReport, ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_schema_version": self.report_schema_version,
            "analysis_id": self.analysis_id,
            "target_origin": self.target_origin,
            "methodology_id": self.methodology_id,
            "methodology_version": self.methodology_version,
            "state": self.state,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "finished_at": _iso(self.finished_at),
            "progress": self.progress.to_dict(),
            "attempts": [item.to_dict() for item in self.attempts],
            "jobs": [item.to_dict() for item in self.jobs],
            "crawl_runs": [item.to_dict() for item in self.crawl_runs],
            "browser_runs": [item.to_dict() for item in self.browser_runs],
            "assessment": (
                self.assessment.to_dict() if self.assessment is not None else None
            ),
            "indicator_results": [item.to_dict() for item in self.indicator_results],
            "evidence": [item.to_dict() for item in self.evidence],
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class AnalysisHistoryEntry:
    analysis_id: str
    target_origin: str
    methodology_id: str
    methodology_version: str
    state: str
    created_at: datetime
    finished_at: datetime | None
    overall_state: str | None
    dimensions: Mapping[str, Mapping[str, Any]]

    @classmethod
    def from_report(cls, report: AnalysisReport) -> AnalysisHistoryEntry:
        assessment = report.assessment
        dimensions = (
            {
                key: _freeze_report_json(value.to_dict())
                for key, value in assessment.dimensions.items()
            }
            if assessment is not None
            else {}
        )
        return cls(
            analysis_id=report.analysis_id,
            target_origin=report.target_origin,
            methodology_id=report.methodology_id,
            methodology_version=report.methodology_version,
            state=report.state,
            created_at=report.created_at,
            finished_at=report.finished_at,
            overall_state=(assessment.overall_state.value if assessment else None),
            dimensions=_freeze_report_json(dimensions),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "target_origin": self.target_origin,
            "methodology_id": self.methodology_id,
            "methodology_version": self.methodology_version,
            "state": self.state,
            "created_at": _iso(self.created_at),
            "finished_at": _iso(self.finished_at),
            "overall_state": self.overall_state,
            "dimensions": {
                key: dict(value) for key, value in self.dimensions.items()
            },
        }


@dataclass(frozen=True, slots=True)
class IndicatorChange:
    indicator_id: str
    baseline_state: str
    current_state: str
    baseline_score: float | None
    current_score: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "indicator_id": self.indicator_id,
            "baseline_state": self.baseline_state,
            "current_state": self.current_state,
            "baseline_score": self.baseline_score,
            "current_score": self.current_score,
        }


@dataclass(frozen=True, slots=True)
class AnalysisComparison:
    comparison_schema_version: str
    baseline: AnalysisHistoryEntry
    current: AnalysisHistoryEntry
    classification: str
    indicator_changes: tuple[IndicatorChange, ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "comparison_schema_version": self.comparison_schema_version,
            "baseline": self.baseline.to_dict(),
            "current": self.current.to_dict(),
            "classification": self.classification,
            "indicator_changes": [item.to_dict() for item in self.indicator_changes],
            "limitations": list(self.limitations),
        }


class AnalysisReportService:
    """Builds an audited report without exposing collected payloads or target paths."""

    def __init__(
        self,
        repository: AnalysisReportRepository,
        methodology: MethodologyConfig,
    ) -> None:
        self._repository = repository
        self._methodology = methodology
        self._scoring = ScoringEngine(methodology)

    def get_report(
        self,
        analysis_id: str,
        *,
        audit: AuditContext = AuditContext(),
    ) -> AnalysisReport:
        self._validate_analysis_id(analysis_id)
        snapshot = self._repository.load_analysis_report_snapshot(analysis_id)
        if snapshot is None:
            self._repository.record_analysis_report_access(
                analysis_id=analysis_id,
                result="failure",
                error_code="ANALYSIS_NOT_FOUND",
                state=None,
                attempt_count=0,
                job_count=0,
                result_count=0,
                crawl_count=0,
                browser_count=0,
                audit=audit,
            )
            raise ReportNotFoundError("analysis report was not found")
        try:
            report = self._build(snapshot)
        except Exception:
            self._repository.record_analysis_report_access(
                analysis_id=analysis_id,
                result="failure",
                error_code="REPORT_BUILD_FAILED",
                state=snapshot.analysis.state.value,
                attempt_count=len(snapshot.attempts),
                job_count=len(snapshot.jobs),
                result_count=len(snapshot.indicator_results),
                crawl_count=len(snapshot.crawl_observations),
                browser_count=len(snapshot.browser_observations),
                audit=audit,
            )
            raise
        self._repository.record_analysis_report_access(
            analysis_id=analysis_id,
            result="success",
            error_code=None,
            state=report.state,
            attempt_count=len(report.attempts),
            job_count=len(report.jobs),
            result_count=len(report.indicator_results),
            crawl_count=len(report.crawl_runs),
            browser_count=len(report.browser_runs),
            audit=audit,
        )
        return report

    def list_history(
        self,
        owner_user_id: str,
        *,
        limit: int = 20,
        audit: AuditContext = AuditContext(),
    ) -> tuple[AnalysisHistoryEntry, ...]:
        self._validate_owner_and_limit(owner_user_id, limit)
        try:
            entries: list[AnalysisHistoryEntry] = []
            for analysis_id in self._repository.list_owned_analysis_ids(
                owner_user_id, limit=limit
            ):
                snapshot = self._repository.load_analysis_report_snapshot(analysis_id)
                if snapshot is None:
                    raise ReportError("history snapshot disappeared")
                entries.append(AnalysisHistoryEntry.from_report(self._build(snapshot)))
            history = tuple(entries)
        except Exception:
            self._repository.record_analysis_history_access(
                operation="history",
                analysis_id=None,
                result="failure",
                error_code="HISTORY_BUILD_FAILED",
                item_count=0,
                change_count=0,
                audit=audit,
            )
            raise
        self._repository.record_analysis_history_access(
            operation="history",
            analysis_id=None,
            result="success",
            error_code=None,
            item_count=len(history),
            change_count=0,
            audit=audit,
        )
        return history

    def compare_with_previous(
        self,
        analysis_id: str,
        owner_user_id: str,
        *,
        audit: AuditContext = AuditContext(),
    ) -> AnalysisComparison:
        self._validate_analysis_id(analysis_id)
        self._validate_owner_and_limit(owner_user_id, 1)
        try:
            owned_ids = self._repository.list_owned_analysis_ids(
                owner_user_id, limit=1000
            )
            if analysis_id not in owned_ids:
                raise ReportNotFoundError("analysis comparison was not found")
            current_snapshot = self._repository.load_analysis_report_snapshot(analysis_id)
            if current_snapshot is None:
                raise ReportNotFoundError("analysis comparison was not found")
            if current_snapshot.analysis.state not in {
                AnalysisState.COMPLETED,
                AnalysisState.PARTIALLY_COMPLETED,
            }:
                raise ReportError("comparison requires a terminal assessment")
            baseline_snapshot = None
            for candidate_id in owned_ids:
                if candidate_id == analysis_id:
                    continue
                candidate = self._repository.load_analysis_report_snapshot(candidate_id)
                if candidate is None:
                    raise ReportError("comparison snapshot disappeared")
                if (
                    candidate.analysis.subject_uri
                    == current_snapshot.analysis.subject_uri
                    and candidate.analysis.methodology_id
                    == current_snapshot.analysis.methodology_id
                    and candidate.analysis.methodology_version
                    == current_snapshot.analysis.methodology_version
                    and candidate.analysis.state
                    in {
                        AnalysisState.COMPLETED,
                        AnalysisState.PARTIALLY_COMPLETED,
                    }
                    and candidate.analysis.created_at
                    < current_snapshot.analysis.created_at
                ):
                    baseline_snapshot = candidate
                    break
            if baseline_snapshot is None:
                raise ReportNotFoundError("compatible baseline was not found")
            comparison = self._compare(
                self._build(baseline_snapshot), self._build(current_snapshot)
            )
        except ReportNotFoundError:
            self._repository.record_analysis_history_access(
                operation="comparison",
                analysis_id=analysis_id,
                result="failure",
                error_code="COMPARISON_NOT_FOUND",
                item_count=0,
                change_count=0,
                audit=audit,
            )
            raise
        except Exception:
            self._repository.record_analysis_history_access(
                operation="comparison",
                analysis_id=analysis_id,
                result="failure",
                error_code="COMPARISON_BUILD_FAILED",
                item_count=0,
                change_count=0,
                audit=audit,
            )
            raise
        self._repository.record_analysis_history_access(
            operation="comparison",
            analysis_id=analysis_id,
            result="success",
            error_code=None,
            item_count=2,
            change_count=len(comparison.indicator_changes),
            audit=audit,
        )
        return comparison

    def _build(self, snapshot: AnalysisReportSnapshot) -> AnalysisReport:
        analysis = snapshot.analysis
        if (
            analysis.methodology_id != self._methodology.id
            or analysis.methodology_version != self._methodology.version
        ):
            raise ReportError("analysis methodology does not match report methodology")
        attempts = tuple(
            AttemptReport(
                attempt_id=item.attempt_id,
                sequence=item.sequence,
                state=item.state.value,
                failure_code=item.failure_code,
                started_at=item.started_at,
                finished_at=item.finished_at,
            )
            for item in snapshot.attempts
        )
        jobs = tuple(
            JobReport(
                job_id=item.job_id,
                stage=(
                    "collection"
                    if isinstance(item, CollectionJob)
                    else "browser"
                    if isinstance(item, BrowserJob)
                    else "assessment"
                ),
                state=item.state.value,
                attempt_count=item.attempt_count,
                max_attempts=item.max_attempts,
                available_at=item.available_at,
                completed_at=item.completed_at,
                error_code=item.error_code,
            )
            for item in snapshot.jobs
        )
        results = tuple(
            IndicatorReport(
                indicator_id=item.indicator_id,
                state=item.state.value,
                confidence=item.confidence,
                score=item.score,
                evidence_ids=item.evidence_ids,
                evidence_hashes=item.evidence_hashes,
                measured_at=item.measured_at,
                measurement_method_version=item.measurement_method_version,
                probe_ids=item.probe_ids,
                tool_versions=item.tool_versions,
            )
            for item in snapshot.indicator_results
        )
        evidence = tuple(
            EvidenceReport(
                evidence_id=item.evidence_id,
                indicator_id=item.indicator_id,
                kind=item.kind,
                method_id=item.method_id,
                method_version=item.method_version,
                confidence=item.confidence,
                content_hash=item.content_hash,
                created_at=item.created_at,
                sources=tuple(
                    EvidenceSourceReport.from_source(source) for source in item.sources
                ),
            )
            for item in snapshot.evidence
        )
        crawl_runs = tuple(
            CrawlRunReport.from_observation(item)
            for item in snapshot.crawl_observations
        )
        browser_runs = tuple(
            BrowserRunReport.from_observation(item)
            for item in snapshot.browser_observations
        )
        assessment = None
        if analysis.state in {
            AnalysisState.COMPLETED,
            AnalysisState.PARTIALLY_COMPLETED,
        }:
            assessment = self._scoring.evaluate(
                snapshot.indicator_results,
                applicable_indicator_ids=tuple(self._methodology.indicators),
            )
        return AnalysisReport(
            report_schema_version="1.1",
            analysis_id=analysis.analysis_id,
            target_origin=_safe_origin(analysis.subject_uri),
            methodology_id=analysis.methodology_id,
            methodology_version=analysis.methodology_version,
            state=analysis.state.value,
            created_at=analysis.created_at,
            updated_at=analysis.updated_at,
            finished_at=analysis.finished_at,
            progress=AnalysisProgress(
                phase=_progress_phase(analysis, snapshot.attempts, snapshot.jobs),
                attempts_used=len(snapshot.attempts),
                attempts_allowed=analysis.max_attempts,
                jobs_total=len(snapshot.jobs),
                results_total=len(snapshot.indicator_results),
            ),
            attempts=attempts,
            jobs=jobs,
            crawl_runs=crawl_runs,
            browser_runs=browser_runs,
            assessment=assessment,
            indicator_results=results,
            evidence=evidence,
            limitations=(
                "Scores and thresholds are a versioned project hypothesis, not a "
                "scientifically validated equivalence to CLEAR+.",
                "Candidate gates remain inactive until explicitly approved in a "
                "methodology release.",
                "The legacy CLEAR+ comparison is unavailable until a validated "
                "mapping from current evidence to the original facets is approved.",
            ),
        )

    @staticmethod
    def _compare(
        baseline: AnalysisReport,
        current: AnalysisReport,
    ) -> AnalysisComparison:
        baseline_results = {
            item.indicator_id: item for item in baseline.indicator_results
        }
        current_results = {item.indicator_id: item for item in current.indicator_results}
        changes = tuple(
            IndicatorChange(
                indicator_id=indicator_id,
                baseline_state=baseline_results[indicator_id].state,
                current_state=current_results[indicator_id].state,
                baseline_score=baseline_results[indicator_id].score,
                current_score=current_results[indicator_id].score,
            )
            for indicator_id in sorted(baseline_results.keys() & current_results.keys())
            if (
                baseline_results[indicator_id].state
                != current_results[indicator_id].state
                or baseline_results[indicator_id].score
                != current_results[indicator_id].score
            )
        )
        return AnalysisComparison(
            comparison_schema_version="1.0",
            baseline=AnalysisHistoryEntry.from_report(baseline),
            current=AnalysisHistoryEntry.from_report(current),
            classification="observed_change" if changes else "no_observed_change",
            indicator_changes=changes,
            limitations=(
                "The comparison is descriptive and does not establish causality.",
                "Only analyses with identical target, methodology and version are compared.",
                "A single changed run is not classified as a structural website change.",
            ),
        )

    @staticmethod
    def _validate_analysis_id(value: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 256
            or _CONTROL_CHARACTER_PATTERN.search(value)
        ):
            raise ReportError("analysis_id is invalid")

    @staticmethod
    def _validate_owner_and_limit(owner_user_id: str, limit: int) -> None:
        if (
            not isinstance(owner_user_id, str)
            or not owner_user_id
            or len(owner_user_id) > 256
            or _CONTROL_CHARACTER_PATTERN.search(owner_user_id)
        ):
            raise ReportError("history owner identifier is invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ReportError("history limit is invalid")


def _safe_origin(subject_uri: str) -> str:
    parsed = urlsplit(subject_uri)
    hostname = parsed.hostname
    if hostname is None:
        raise ReportError("analysis subject has no hostname")
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    try:
        port = parsed.port
    except ValueError as exc:
        raise ReportError("analysis subject port is invalid") from exc
    default_port = 443 if parsed.scheme == "https" else 80
    authority = display_host if port in {None, default_port} else f"{display_host}:{port}"
    return f"{parsed.scheme}://{authority}/"


def _freeze_report_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_report_json(item) for key, item in value.items()}
        )
    if isinstance(value, tuple):
        return tuple(_freeze_report_json(item) for item in value)
    return value


def _progress_phase(
    analysis: Analysis,
    attempts: tuple[Attempt, ...],
    jobs: tuple[AssessmentJob | BrowserJob | CollectionJob, ...],
) -> str:
    if analysis.state in {
        AnalysisState.COMPLETED,
        AnalysisState.PARTIALLY_COMPLETED,
        AnalysisState.FAILED,
        AnalysisState.CANCELLED,
    }:
        return analysis.state.value
    collection = tuple(item for item in jobs if isinstance(item, CollectionJob))
    browser = tuple(item for item in jobs if isinstance(item, BrowserJob))
    assessment = tuple(item for item in jobs if isinstance(item, AssessmentJob))
    if any(item.state is AssessmentJobState.RUNNING for item in collection):
        return "collecting"
    if any(item.state is AssessmentJobState.PENDING for item in collection):
        return "collection_queued"
    if any(item.state is AssessmentJobState.FAILED for item in collection):
        return "collection_failed"
    if any(item.state is AssessmentJobState.RUNNING for item in browser):
        return "rendering"
    if any(item.state is AssessmentJobState.PENDING for item in browser):
        return "browser_queued"
    if any(item.state is AssessmentJobState.RUNNING for item in assessment):
        return "assessing"
    if any(item.state is AssessmentJobState.PENDING for item in assessment):
        return (
            "assessment_queued_degraded"
            if any(item.state is AssessmentJobState.FAILED for item in browser)
            else "assessment_queued"
        )
    if any(item.state is AssessmentJobState.FAILED for item in browser):
        return "browser_failed"
    if any(item.state is AssessmentJobState.FAILED for item in assessment):
        return "assessment_failed"
    if any(item.state is AssessmentJobState.SUCCEEDED for item in assessment):
        return "awaiting_analysis_finalization"
    if any(item.state is AttemptState.RUNNING for item in attempts):
        return "probing"
    if attempts and attempts[-1].state is AttemptState.FAILED:
        return "retry_available"
    if attempts and attempts[-1].state is AttemptState.SUCCEEDED:
        return "awaiting_assessment_enqueue"
    return "requested"
