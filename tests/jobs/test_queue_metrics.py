from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability.jobs import (  # noqa: E402
    QueueMetricsSnapshot,
    render_prometheus_metrics,
)


class QueueMetricsTests(unittest.TestCase):
    def test_prometheus_output_contains_only_fixed_labels_and_numbers(self) -> None:
        snapshot = QueueMetricsSnapshot(
            pending=3,
            running=2,
            succeeded=5,
            failed=1,
            available=2,
            expired_leases=1,
            oldest_pending_seconds=12.3456,
        )

        rendered = render_prometheus_metrics(snapshot).decode("ascii")

        self.assertIn('archivability_queue_jobs{state="pending"} 3', rendered)
        self.assertIn("archivability_queue_available_jobs 2", rendered)
        self.assertIn("archivability_queue_oldest_pending_seconds 12.346", rendered)
        self.assertNotIn("user", rendered)
        self.assertTrue(rendered.endswith("\n"))

    def test_negative_metric_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-negative"):
            QueueMetricsSnapshot(
                pending=-1,
                running=0,
                succeeded=0,
                failed=0,
                available=0,
                expired_leases=0,
                oldest_pending_seconds=0,
            )

        with self.assertRaisesRegex(ValueError, "subsets"):
            QueueMetricsSnapshot(
                pending=0,
                running=0,
                succeeded=0,
                failed=0,
                available=1,
                expired_leases=0,
                oldest_pending_seconds=0,
            )


if __name__ == "__main__":
    unittest.main()
