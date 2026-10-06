from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from archivability.storage.audit import AuditContext


@dataclass(frozen=True, slots=True)
class QueueMetricsSnapshot:
    pending: int
    running: int
    succeeded: int
    failed: int
    available: int
    expired_leases: int
    oldest_pending_seconds: float

    def __post_init__(self) -> None:
        for name in (
            "pending",
            "running",
            "succeeded",
            "failed",
            "available",
            "expired_leases",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("queue metric counters must be non-negative integers")
        if (
            not isinstance(self.oldest_pending_seconds, (int, float))
            or isinstance(self.oldest_pending_seconds, bool)
            or self.oldest_pending_seconds < 0
            or not math.isfinite(float(self.oldest_pending_seconds))
        ):
            raise ValueError("oldest pending age must be non-negative")
        if self.available > self.pending or self.expired_leases > self.running:
            raise ValueError("queue metric subsets cannot exceed their states")


class QueueMetricsRepository(Protocol):
    def get_queue_metrics(
        self, *, audit: AuditContext = AuditContext()
    ) -> QueueMetricsSnapshot: ...


class QueueMetricsService:
    def __init__(self, repository: QueueMetricsRepository) -> None:
        self._repository = repository

    def snapshot(self, *, audit: AuditContext = AuditContext()) -> QueueMetricsSnapshot:
        return self._repository.get_queue_metrics(audit=audit)


def render_prometheus_metrics(snapshot: QueueMetricsSnapshot) -> bytes:
    lines = [
        "# HELP archivability_queue_jobs Jobs by durable queue state.",
        "# TYPE archivability_queue_jobs gauge",
    ]
    for state in ("pending", "running", "succeeded", "failed"):
        lines.append(
            f'archivability_queue_jobs{{state="{state}"}} {getattr(snapshot, state)}'
        )
    lines.extend(
        (
            "# HELP archivability_queue_available_jobs Pending jobs ready to claim.",
            "# TYPE archivability_queue_available_jobs gauge",
            f"archivability_queue_available_jobs {snapshot.available}",
            "# HELP archivability_queue_expired_leases Running jobs with expired leases.",
            "# TYPE archivability_queue_expired_leases gauge",
            f"archivability_queue_expired_leases {snapshot.expired_leases}",
            "# HELP archivability_queue_oldest_pending_seconds Age of the oldest pending job.",
            "# TYPE archivability_queue_oldest_pending_seconds gauge",
            (
                "archivability_queue_oldest_pending_seconds "
                f"{snapshot.oldest_pending_seconds:.3f}"
            ),
        )
    )
    return ("\n".join(lines) + "\n").encode("ascii")
