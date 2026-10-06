from __future__ import annotations

import json
import unittest
from pathlib import Path

from .reference import evaluate_case


ROOT = Path(__file__).resolve().parents[2]
METHODOLOGY = ROOT / "methodology" / "v0.1.0"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_json_yaml(name: str) -> dict:
    """The candidate YAML files deliberately use the JSON subset of YAML 1.2."""
    return json.loads((METHODOLOGY / name).read_text(encoding="utf-8"))


class MethodologyConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_json_yaml("manifest.yaml")
        cls.dimensions = load_json_yaml("dimensions.yaml")
        cls.indicators = load_json_yaml("indicators.yaml")
        cls.scoring = load_json_yaml("scoring.yaml")
        cls.gates = load_json_yaml("gates.yaml")

    def test_manifest_references_existing_files(self) -> None:
        for relative_path in self.manifest["files"].values():
            self.assertTrue((METHODOLOGY / relative_path).is_file(), relative_path)

    def test_all_versions_match(self) -> None:
        version = self.manifest["version"]
        for document in (self.dimensions, self.indicators, self.scoring, self.gates):
            self.assertEqual(version, document["methodology_version"])

    def test_six_dimensions_and_45_unique_indicators(self) -> None:
        dimension_ids = [item["id"] for item in self.dimensions["dimensions"]]
        indicator_ids = [item["id"] for item in self.indicators["indicators"]]
        indicator_slugs = [item["slug"] for item in self.indicators["indicators"]]
        self.assertEqual(6, len(dimension_ids))
        self.assertEqual(6, len(set(dimension_ids)))
        self.assertEqual(45, len(indicator_ids))
        self.assertEqual(45, len(set(indicator_ids)))
        self.assertEqual(45, len(set(indicator_slugs)))

    def test_indicator_references_are_valid(self) -> None:
        dimension_ids = {item["id"] for item in self.dimensions["dimensions"]}
        gate_ids = {item["id"] for item in self.gates["gates"]}
        for indicator in self.indicators["indicators"]:
            if indicator["dimension"] is not None:
                self.assertIn(indicator["dimension"], dimension_ids)
            self.assertTrue(set(indicator["blocking_candidates"]) <= gate_ids)
            self.assertTrue(indicator["measurement_method"])
            self.assertTrue(indicator["evidence_requirements"])
            self.assertTrue(indicator["limitations"])

    def test_gate_references_and_default_status(self) -> None:
        indicator_ids = {item["id"] for item in self.indicators["indicators"]}
        self.assertEqual(7, len(self.gates["gates"]))
        for gate in self.gates["gates"]:
            self.assertEqual("candidate", gate["status"])
            for condition in gate["all"]:
                self.assertIn(condition["indicator"], indicator_ids)

    def test_observation_context_never_scores(self) -> None:
        context_indicators = [
            item
            for item in self.indicators["indicators"]
            if item["classification"] == "observation_context"
        ]
        self.assertTrue(context_indicators)
        for indicator in context_indicators:
            self.assertFalse(indicator["score"]["enabled"])
            self.assertEqual(0, indicator["score"]["weight"])

    def test_json_schemas_are_well_formed_json(self) -> None:
        schema_dir = METHODOLOGY / "schemas"
        schemas = sorted(schema_dir.glob("*.schema.json"))
        self.assertEqual(5, len(schemas))
        for schema in schemas:
            parsed = json.loads(schema.read_text(encoding="utf-8"))
            self.assertEqual("https://json-schema.org/draft/2020-12/schema", parsed["$schema"])


class GoldenCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dimensions = load_json_yaml("dimensions.yaml")
        cls.indicators = load_json_yaml("indicators.yaml")
        cls.scoring = load_json_yaml("scoring.yaml")
        cls.gates = load_json_yaml("gates.yaml")
        cls.cases = json.loads(
            (FIXTURES / "golden_cases.json").read_text(encoding="utf-8")
        )["cases"]

    def test_all_golden_cases(self) -> None:
        for case in self.cases:
            with self.subTest(case=case["id"]):
                actual = evaluate_case(
                    self.dimensions,
                    self.indicators,
                    self.scoring,
                    self.gates,
                    case,
                )
                self.assertEqual(case["expected"]["overall_state"], actual["overall_state"])
                self.assertEqual(case["expected"]["triggered_gates"], actual["triggered_gates"])
                self.assertEqual(
                    set(case["expected"]["dimension_results"]),
                    set(actual["dimension_results"]),
                )
                for dimension_id, expected in case["expected"]["dimension_results"].items():
                    observed = actual["dimension_results"][dimension_id]
                    self.assertEqual(expected["status"], observed["status"])
                    self.assertAlmostEqual(expected["coverage"], observed["coverage"], places=6)
                    self.assertAlmostEqual(expected["score"], observed["score"], places=6)

                if "legacy_clear_plus" in case["expected"]:
                    for key, expected in case["expected"]["legacy_clear_plus"].items():
                        self.assertAlmostEqual(
                            expected,
                            actual["legacy_clear_plus"][key],
                            places=6,
                        )


if __name__ == "__main__":
    unittest.main()
