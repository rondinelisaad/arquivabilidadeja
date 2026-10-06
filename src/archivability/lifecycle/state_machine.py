from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Collection

from archivability.lifecycle.models import (
    ATTEMPT_TERMINAL_STATES,
    Analysis,
    AnalysisState,
    Attempt,
    AttemptState,
    LifecycleError,
)


def _transition_time(current: datetime, occurred_at: datetime) -> None:
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise LifecycleError("transition timestamp must include a timezone")
    if occurred_at < current:
        raise LifecycleError("transition timestamp cannot move backwards")


def start_attempt(
    analysis: Analysis, *, attempt_id: str, occurred_at: datetime
) -> tuple[Analysis, Attempt]:
    if analysis.state not in {AnalysisState.REQUESTED, AnalysisState.RUNNING}:
        raise LifecycleError(f"cannot start an attempt from {analysis.state.value}")
    if len(analysis.attempt_ids) >= analysis.max_attempts:
        raise LifecycleError("analysis attempt limit has been reached")
    if attempt_id in analysis.attempt_ids:
        raise LifecycleError("attempt_id is already registered")
    _transition_time(analysis.updated_at, occurred_at)

    attempt = Attempt(
        attempt_id=attempt_id,
        analysis_id=analysis.analysis_id,
        sequence=len(analysis.attempt_ids) + 1,
        state=AttemptState.RUNNING,
        created_at=occurred_at,
        updated_at=occurred_at,
        started_at=occurred_at,
    )
    updated_analysis = replace(
        analysis,
        state=AnalysisState.RUNNING,
        attempt_ids=(*analysis.attempt_ids, attempt_id),
        updated_at=occurred_at,
        started_at=analysis.started_at or occurred_at,
        revision=analysis.revision + 1,
    )
    return updated_analysis, attempt


def finish_attempt(
    attempt: Attempt,
    *,
    target: AttemptState,
    occurred_at: datetime,
    failure_code: str | None = None,
) -> Attempt:
    if attempt.state is not AttemptState.RUNNING:
        raise LifecycleError(f"cannot finish an attempt from {attempt.state.value}")
    if not isinstance(target, AttemptState) or target not in ATTEMPT_TERMINAL_STATES:
        if not isinstance(target, AttemptState):
            raise LifecycleError("target must be an AttemptState")
        raise LifecycleError(f"invalid terminal attempt state: {target.value}")
    _transition_time(attempt.updated_at, occurred_at)
    return replace(
        attempt,
        state=target,
        updated_at=occurred_at,
        finished_at=occurred_at,
        failure_code=failure_code,
        revision=attempt.revision + 1,
    )


def finalize_analysis(
    analysis: Analysis,
    attempts: Collection[Attempt],
    *,
    target: AnalysisState,
    occurred_at: datetime,
) -> Analysis:
    if analysis.state is not AnalysisState.RUNNING:
        raise LifecycleError(f"cannot finalize analysis from {analysis.state.value}")
    if not isinstance(target, AnalysisState):
        raise LifecycleError("target must be an AnalysisState")
    if target not in {
        AnalysisState.COMPLETED,
        AnalysisState.PARTIALLY_COMPLETED,
        AnalysisState.FAILED,
        AnalysisState.CANCELLED,
    }:
        raise LifecycleError(f"invalid terminal analysis state: {target.value}")
    _transition_time(analysis.updated_at, occurred_at)
    by_id = {attempt.attempt_id: attempt for attempt in attempts}
    if (
        set(by_id) != set(analysis.attempt_ids)
        or len(by_id) != len(analysis.attempt_ids)
        or len(by_id) != len(attempts)
    ):
        raise LifecycleError("attempt collection does not match the analysis")
    if any(item.analysis_id != analysis.analysis_id for item in by_id.values()):
        raise LifecycleError("attempt belongs to another analysis")
    if any(item.state not in ATTEMPT_TERMINAL_STATES for item in by_id.values()):
        raise LifecycleError("all attempts must be terminal before finalization")

    states = {item.state for item in by_id.values()}
    if target is AnalysisState.COMPLETED and states != {AttemptState.SUCCEEDED}:
        raise LifecycleError("completed analysis requires only successful attempts")
    if target is AnalysisState.PARTIALLY_COMPLETED and not (
        AttemptState.SUCCEEDED in states and AttemptState.FAILED in states
    ):
        raise LifecycleError("partial completion requires successes and failures")
    if target is AnalysisState.FAILED and states != {AttemptState.FAILED}:
        raise LifecycleError("failed analysis requires only failed attempts")
    if target is AnalysisState.CANCELLED and AttemptState.CANCELLED not in states:
        raise LifecycleError("cancelled analysis requires a cancelled attempt")

    return replace(
        analysis,
        state=target,
        updated_at=occurred_at,
        finished_at=occurred_at,
        revision=analysis.revision + 1,
    )
