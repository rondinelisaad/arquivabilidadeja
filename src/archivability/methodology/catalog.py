from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from archivability.methodology.loader import load_methodology
from archivability.methodology.models import MethodologyValidationError


@dataclass(frozen=True, slots=True)
class IndicatorDocumentation:
    id: str
    slug: str
    name: str
    description: str
    origin_kind: str
    origin_references: tuple[str, ...]
    dimension: str | None
    clear_plus_facets: tuple[str, ...]
    classification: str
    severity: str
    score_enabled: bool
    weight: float
    scoring_rule: str
    blocking_candidates: tuple[str, ...]
    measurement_method: str
    evidence_requirements: tuple[str, ...]
    limitations: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "origin": {
                "kind": self.origin_kind,
                "references": list(self.origin_references),
            },
            "dimension": self.dimension,
            "clear_plus_facets": list(self.clear_plus_facets),
            "classification": self.classification,
            "severity": self.severity,
            "scoring": {
                "enabled": self.score_enabled,
                "weight": self.weight,
                "rule": self.scoring_rule,
            },
            "blocking_candidates": list(self.blocking_candidates),
            "measurement": self.measurement_method,
            "evidence_requirements": list(self.evidence_requirements),
            "limitations": self.limitations,
            "path": f"/methodology/indicators/{self.slug}",
        }


@dataclass(frozen=True, slots=True)
class MethodologyCatalog:
    methodology_id: str
    version: str
    status: str
    title: str
    released_at: str | None
    language: str
    source_sha256: str
    dimensions: tuple[Mapping[str, Any], ...]
    indicators: tuple[IndicatorDocumentation, ...]
    gates: tuple[Mapping[str, Any], ...]
    minimum_coverage: float
    status_scores: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "dimensions",
            tuple(MappingProxyType(dict(item)) for item in self.dimensions),
        )
        object.__setattr__(
            self, "gates", tuple(MappingProxyType(dict(item)) for item in self.gates)
        )
        object.__setattr__(
            self, "status_scores", MappingProxyType(dict(self.status_scores))
        )

    def methodology_document(self) -> dict[str, Any]:
        return {
            "methodology_schema_version": "1.0",
            "methodology_id": self.methodology_id,
            "version": self.version,
            "status": self.status,
            "title": self.title,
            "released_at": self.released_at,
            "language": self.language,
            "source_sha256": self.source_sha256,
            "dimensions": [dict(item) for item in self.dimensions],
            "scoring": {
                "type": "dimension_profile_with_gates",
                "single_overall_score": False,
                "minimum_coverage": self.minimum_coverage,
                "status_scores": dict(self.status_scores),
                "weights_basis": "project_hypothesis",
            },
            "gates": [dict(item) for item in self.gates],
            "indicator_count": len(self.indicators),
            "indicators_path": "/v1/indicators",
            "limitations": [
                "This methodology is a candidate until independent scientific validation is complete.",
                "Weights and coverage thresholds are versioned project hypotheses.",
                "Candidate gates are inactive unless a later methodology release activates them.",
            ],
        }

    def list_indicators(
        self,
        *,
        dimension: str | None = None,
        classification: str | None = None,
    ) -> tuple[IndicatorDocumentation, ...]:
        return tuple(
            item
            for item in self.indicators
            if (dimension is None or item.dimension == dimension)
            and (classification is None or item.classification == classification)
        )

    def get_indicator(self, identifier: str) -> IndicatorDocumentation | None:
        return next(
            (item for item in self.indicators if identifier in {item.id, item.slug}),
            None,
        )


def load_methodology_catalog(directory: str | Path) -> MethodologyCatalog:
    root = Path(directory).resolve()
    config = load_methodology(root)
    manifest = _read_object(root / "manifest.yaml")
    raw_dimensions = _read_object(root / "dimensions.yaml")
    raw_indicators = _read_object(root / "indicators.yaml")
    raw_gates = _read_object(root / "gates.yaml")
    raw_scoring = _read_object(root / "scoring.yaml")
    documents = (manifest, raw_dimensions, raw_indicators, raw_gates, raw_scoring)
    canonical = json.dumps(
        documents, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")

    indicators = tuple(
        _indicator_document(item)
        for item in sorted(raw_indicators["indicators"], key=lambda value: value["id"])
    )
    if {item.id for item in indicators} != set(config.indicators):
        raise MethodologyValidationError("catalog indicators do not match methodology")

    dimensions = tuple(
        {
            "id": item["id"],
            "name": item["name"],
            "description": item["description"],
            "weight": float(item["weight"]),
            "score_enabled": bool(item["score_enabled"]),
            "weight_basis": item["weight_basis"],
        }
        for item in raw_dimensions["dimensions"]
    )
    gates = tuple(
        {
            "id": item["id"],
            "name": item["name"],
            "status": item["status"],
            "effect": item["effect"],
            "minimum_confidence": float(item["minimum_confidence"]),
            "caution": item["caution"],
        }
        for item in raw_gates["gates"]
    )
    return MethodologyCatalog(
        methodology_id=config.id,
        version=config.version,
        status=config.status,
        title=str(manifest["title"]),
        released_at=manifest.get("released_at"),
        language=str(manifest["language"]),
        source_sha256=hashlib.sha256(canonical).hexdigest(),
        dimensions=dimensions,
        indicators=indicators,
        gates=gates,
        minimum_coverage=config.minimum_coverage,
        status_scores={
            key.value: value for key, value in config.status_score_map.items()
        },
    )


def _indicator_document(item: Mapping[str, Any]) -> IndicatorDocumentation:
    try:
        origin = item["origin"]
        score = item["score"]
        return IndicatorDocumentation(
            id=str(item["id"]),
            slug=str(item["slug"]),
            name=str(item["name"]),
            description=str(item["description"]),
            origin_kind=str(origin["kind"]),
            origin_references=tuple(str(value) for value in origin["refs"]),
            dimension=(str(item["dimension"]) if item["dimension"] else None),
            clear_plus_facets=tuple(str(value) for value in item["clear_plus_facets"]),
            classification=str(item["classification"]),
            severity=str(item["default_severity"]),
            score_enabled=bool(score["enabled"]),
            weight=float(score["weight"]),
            scoring_rule=str(score["rule"]),
            blocking_candidates=tuple(
                str(value) for value in item["blocking_candidates"]
            ),
            measurement_method=str(item["measurement_method"]),
            evidence_requirements=tuple(
                str(value) for value in item["evidence_requirements"]
            ),
            limitations=str(item["limitations"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MethodologyValidationError("indicator documentation is invalid") from exc


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MethodologyValidationError("methodology catalog cannot be read") from exc
    if not isinstance(value, dict):
        raise MethodologyValidationError(
            "methodology catalog document must be an object"
        )
    return value
