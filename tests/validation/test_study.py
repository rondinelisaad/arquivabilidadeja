from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability.validation.cli import main
from archivability.validation.study import (
    ValidationInputError,
    analyze_validation_dataset,
)


def dataset() -> dict:
    return {
        "schema_version": "1.0",
        "dataset_id": "synthetic-study-001",
        "methodology_id": "arquivabilidade-ja",
        "methodology_version": "0.1.0",
        "primary_threshold_profile": "candidate",
        "threshold_profiles": [
            {"id": "candidate", "warning_min": 50, "pass_min": 80},
            {"id": "strict", "warning_min": 60, "pass_min": 90},
        ],
        "cases": [
            {
                "case_id": "cal-001",
                "split": "calibration",
                "strata": ["static", "small"],
                "dimension_scores": {"discoverability": 85},
                "expert_ratings": {
                    "expert-a": {"discoverability": "pass"},
                    "expert-b": {"discoverability": "pass"},
                },
            },
            {
                "case_id": "cal-002",
                "split": "calibration",
                "strata": ["dynamic"],
                "dimension_scores": {"discoverability": 55},
                "expert_ratings": {
                    "expert-a": {"discoverability": "warning"},
                    "expert-b": {"discoverability": "warning"},
                },
            },
            {
                "case_id": "hold-001",
                "split": "holdout",
                "strata": ["static"],
                "dimension_scores": {"discoverability": 20},
                "expert_ratings": {
                    "expert-a": {"discoverability": "fail"},
                    "expert-b": {"discoverability": "fail"},
                },
            },
            {
                "case_id": "hold-002",
                "split": "holdout",
                "strata": ["dynamic"],
                "dimension_scores": {"discoverability": 88},
                "expert_ratings": {
                    "expert-a": {"discoverability": "pass"},
                    "expert-b": {"discoverability": "warning"},
                    "expert-c": {"discoverability": "pass"},
                },
            },
        ],
    }


class ValidationStudyTests(unittest.TestCase):
    def test_report_separates_splits_and_measures_sensitivity(self) -> None:
        report = analyze_validation_dataset(dataset())

        self.assertEqual("descriptive_only", report["interpretation"]["status"])
        self.assertEqual(2, report["splits"]["calibration"]["case_count"])
        self.assertEqual(2, report["splits"]["holdout"]["case_count"])
        self.assertEqual(
            2,
            report["splits"]["calibration"]["threshold_sensitivity"]["strict"][
                "changed_from_primary"
            ],
        )
        agreement = report["splits"]["calibration"]["dimensions"]["discoverability"]
        self.assertEqual(1.0, agreement["expert_fleiss_kappa"])
        self.assertEqual(1.0, agreement["model_consensus"]["accuracy"])

    def test_output_is_deterministic_and_hides_websites(self) -> None:
        first = analyze_validation_dataset(dataset())
        second = analyze_validation_dataset(dataset())
        self.assertEqual(first, second)
        rendered = json.dumps(first).casefold()
        self.assertNotIn("http://", rendered)
        self.assertNotIn("https://", rendered)

    def test_duplicate_case_across_splits_is_rejected(self) -> None:
        value = dataset()
        value["cases"][-1]["case_id"] = "cal-001"
        with self.assertRaisesRegex(ValidationInputError, "unique"):
            analyze_validation_dataset(value)

    def test_experts_must_rate_every_dimension(self) -> None:
        value = dataset()
        value["cases"][0]["dimension_scores"]["retrievability"] = 80
        with self.assertRaisesRegex(ValidationInputError, "every scored"):
            analyze_validation_dataset(value)

    def test_cli_writes_canonical_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            input_path = Path(temporary) / "dataset.json"
            output_path = Path(temporary) / "report.json"
            input_path.write_text(json.dumps(dataset()), encoding="utf-8")
            self.assertEqual(0, main([str(input_path), "--output", str(output_path)]))
            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual("1.0", report["report_schema_version"])


if __name__ == "__main__":
    unittest.main()
