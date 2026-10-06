from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability.methodology.loader import load_methodology  # noqa: E402
from archivability.methodology.models import (  # noqa: E402
    IndicatorResult,
    LegacyClearPlusInput,
    ResultState,
    ScoringInputError,
    MethodologyValidationError,
)
from archivability.methodology.scoring import ScoringEngine  # noqa: E402


METHODOLOGY = ROOT / "methodology" / "v0.1.0"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def indicator_results(values: dict[str, dict]) -> list[IndicatorResult]:
    return [
        IndicatorResult(
            indicator_id=indicator_id,
            state=ResultState(value["state"]),
            confidence=float(value["confidence"]),
            score=float(value["score"]) if "score" in value else None,
        )
        for indicator_id, value in values.items()
    ]


class ProductionScoringEngineGoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.methodology = load_methodology(METHODOLOGY)
        cls.engine = ScoringEngine(cls.methodology)
        cls.cases = json.loads(
            (FIXTURES / "golden_cases.json").read_text(encoding="utf-8")
        )["cases"]

    def test_production_engine_matches_all_golden_cases(self) -> None:
        for case in self.cases:
            with self.subTest(case=case["id"]):
                legacy = None
                if "legacy_clear_plus" in case:
                    legacy = LegacyClearPlusInput(**case["legacy_clear_plus"])
                result = self.engine.evaluate(
                    indicator_results(case["indicator_results"]),
                    case["applicable_indicator_ids"],
                    activated_gate_ids=[
                        gate_id
                        for gate_id, status in case.get(
                            "gate_status_overrides", {}
                        ).items()
                        if status == "active"
                    ],
                    legacy_clear_plus=legacy,
                )
                expected = case["expected"]
                self.assertEqual(expected["overall_state"], result.overall_state.value)
                self.assertEqual(expected["triggered_gates"], list(result.triggered_gate_ids))
                self.assertEqual(set(expected["dimension_results"]), set(result.dimensions))
                for dimension_id, expected_dimension in expected[
                    "dimension_results"
                ].items():
                    observed = result.dimensions[dimension_id]
                    self.assertEqual(expected_dimension["status"], observed.status)
                    self.assertAlmostEqual(
                        expected_dimension["coverage"], observed.coverage, places=6
                    )
                    self.assertAlmostEqual(
                        expected_dimension["score"], observed.score, places=6
                    )

                if "legacy_clear_plus" in expected:
                    self.assertIsNotNone(result.legacy_clear_plus)
                    observed_legacy = result.legacy_clear_plus
                    assert observed_legacy is not None
                    self.assertAlmostEqual(
                        expected["legacy_clear_plus"]["flat"],
                        observed_legacy.flat,
                        places=6,
                    )
                    self.assertAlmostEqual(
                        expected["legacy_clear_plus"]["fixed_max"],
                        observed_legacy.fixed_max,
                        places=6,
                    )
                    self.assertAlmostEqual(
                        expected["legacy_clear_plus"]["paper_applicable"],
                        observed_legacy.paper_applicable,
                        places=6,
                    )

    def test_explicit_numeric_score_overrides_status_default(self) -> None:
        result = self.engine.evaluate(
            [IndicatorResult("R01", ResultState.WARNING, 0.9, score=73.5)],
            ["R01"],
        )
        self.assertEqual(73.5, result.dimensions["retrievability"].score)

    def test_unknown_state_cannot_carry_numeric_score(self) -> None:
        with self.assertRaises(ScoringInputError):
            IndicatorResult("D01", ResultState.UNKNOWN, 0.0, score=0)

    def test_plain_string_state_is_rejected(self) -> None:
        with self.assertRaises(ScoringInputError):
            IndicatorResult("D01", "pass", 1.0)  # type: ignore[arg-type]

    def test_unknown_indicator_is_rejected(self) -> None:
        with self.assertRaises(ScoringInputError):
            self.engine.evaluate(
                [IndicatorResult("X99", ResultState.PASS, 1.0)], ["X99"]
            )

    def test_duplicate_result_is_rejected(self) -> None:
        result = IndicatorResult("D01", ResultState.PASS, 1.0)
        with self.assertRaises(ScoringInputError):
            self.engine.evaluate([result, result], ["D01"])

    def test_known_result_must_be_declared_applicable(self) -> None:
        with self.assertRaises(ScoringInputError):
            self.engine.evaluate(
                [IndicatorResult("D01", ResultState.PASS, 1.0)], []
            )

    def test_context_indicator_cannot_be_scored(self) -> None:
        with self.assertRaises(ScoringInputError):
            self.engine.evaluate(
                [IndicatorResult("N01", ResultState.PASS, 1.0)], ["N01"]
            )

    def test_context_only_assessment_is_indeterminate(self) -> None:
        result = self.engine.evaluate(
            [IndicatorResult("N01", ResultState.CONTEXT, 1.0)], ["N01"]
        )
        self.assertEqual("indeterminate", result.overall_state.value)
        self.assertEqual({}, dict(result.dimensions))

    def test_unknown_gate_activation_is_rejected(self) -> None:
        with self.assertRaises(ScoringInputError):
            self.engine.evaluate([], [], activated_gate_ids=["HG99"])

    def test_unknown_active_gate_prevents_assessable_conclusion(self) -> None:
        result = self.engine.evaluate(
            [
                IndicatorResult("D01", ResultState.FAIL, 0.5),
                IndicatorResult("O01", ResultState.FAIL, 0.5),
            ],
            ["D01", "O01"],
            activated_gate_ids=["HG01"],
        )
        self.assertEqual("unknown", result.gates["HG01"].state.value)
        self.assertEqual("partially_assessable", result.overall_state.value)

    def test_serialized_result_exposes_coverage_and_gate_states(self) -> None:
        result = self.engine.evaluate(
            [IndicatorResult("D01", ResultState.PASS, 1.0)], ["D01", "D07"]
        ).to_dict()
        self.assertEqual(0.5, result["dimensions"]["discoverability"]["coverage"])
        self.assertEqual("inactive", result["gates"]["HG01"]["state"])

    def test_manifest_cannot_escape_methodology_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "methodology"
            shutil.copytree(METHODOLOGY, copied)
            manifest_path = copied / "manifest.yaml"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["dimensions"] = "../outside.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(MethodologyValidationError):
                load_methodology(copied)


if __name__ == "__main__":
    unittest.main()
