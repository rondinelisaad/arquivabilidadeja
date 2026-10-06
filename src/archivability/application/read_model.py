from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from archivability.evidence.models import Evidence, EvidenceSource
from archivability.jobs.models import AssessmentJob, AssessmentJobState
from archivability.lifecycle.models import Analysis, AnalysisState, Attempt, AttemptState
from archivability.methodology.models import IndicatorResult
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
    jobs: tuple[AssessmentJob, ...]
    indicator_results: tuple[IndicatorResult, ...]
    evidence: tuple[Evidence, ...]

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
    state: str
    attempt_count: int
    max_attempts: int
    available_at: datetime
    completed_at: datetime | None
    error_code: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
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
class AnalysisReport:
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
    indicator_results: tuple[IndicatorReport, ...]
    evidence: tuple[EvidenceReport, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
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
            "indicator_results": [item.to_dict() for item in self.indicator_results],
            "evidence": [item.to_dict() for item in self.evidence],
        }


class AnalysisReportService:
    """Builds an audited report without exposing collected payloads or target paths."""

    def __init__(self, repository: AnalysisReportRepository) -> None:
        self._repository = repository

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
            audit=audit,
        )
        return report

    @staticmethod
    def _build(snapshot: AnalysisReportSnapshot) -> AnalysisReport:
        analysis = snapshot.analysis
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
        return AnalysisReport(
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
            indicator_results=results,
            evidence=evidence,
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


def _progress_phase(
    analysis: Analysis,
    attempts: tuple[Attempt, ...],
    jobs: tuple[AssessmentJob, ...],
) -> str:
    if analysis.state is not AnalysisState.RUNNING:
        return analysis.state.value
    if any(item.state is AssessmentJobState.RUNNING for item in jobs):
        return "assessing"
    if any(item.state is AssessmentJobState.PENDING for item in jobs):
        return "assessment_queued"
    if any(item.state is AssessmentJobState.FAILED for item in jobs):
        return "assessment_failed"
    if any(item.state is AssessmentJobState.SUCCEEDED for item in jobs):
        return "awaiting_analysis_finalization"
    if any(item.state is AttemptState.RUNNING for item in attempts):
        return "probing"
    if attempts and attempts[-1].state is AttemptState.FAILED:
        return "retry_available"
    if attempts and attempts[-1].state is AttemptState.SUCCEEDED:
        return "awaiting_assessment_enqueue"
    return "requested"
