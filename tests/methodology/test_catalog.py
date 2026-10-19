from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability.methodology import load_methodology_catalog  # noqa: E402


class MethodologyCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_methodology_catalog(ROOT / "methodology" / "v0.1.0")

    def test_catalog_projects_all_versioned_indicators(self) -> None:
        self.assertEqual(45, len(self.catalog.indicators))
        self.assertEqual(64, len(self.catalog.source_sha256))
        self.assertEqual("candidate", self.catalog.status)
        document = self.catalog.methodology_document()
        self.assertFalse(document["scoring"]["single_overall_score"])
        self.assertTrue(
            all(gate["status"] == "candidate" for gate in document["gates"])
        )

    def test_indicator_page_contains_required_methodological_fields(self) -> None:
        indicator = self.catalog.get_indicator("latency_breakdown")
        self.assertIsNotNone(indicator)
        assert indicator is not None
        document = indicator.to_dict()
        for field in (
            "description",
            "origin",
            "measurement",
            "scoring",
            "limitations",
            "evidence_requirements",
        ):
            self.assertIn(field, document)
        self.assertFalse(document["scoring"]["enabled"])

    def test_filters_do_not_mix_dimensions_or_context(self) -> None:
        discoverability = self.catalog.list_indicators(dimension="discoverability")
        context = self.catalog.list_indicators(classification="observation_context")
        self.assertTrue(discoverability)
        self.assertTrue(context)
        self.assertTrue(
            all(item.dimension == "discoverability" for item in discoverability)
        )
        self.assertTrue(
            all(item.classification == "observation_context" for item in context)
        )


if __name__ == "__main__":
    unittest.main()
