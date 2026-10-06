from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

from archivability.methodology.models import (
    DimensionDefinition,
    GateCondition,
    GateDefinition,
    IndicatorDefinition,
    MethodologyConfig,
    MethodologyValidationError,
    ResultState,
)


def _read_json_yaml(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MethodologyValidationError(f"missing methodology file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MethodologyValidationError(f"invalid JSON-compatible YAML: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MethodologyValidationError(f"methodology document must be an object: {path}")
    return value


def _resolve_member(root: Path, relative_path: Any) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise MethodologyValidationError("methodology file path must be a string")
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise MethodologyValidationError("methodology file path must be relative")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise MethodologyValidationError(
            f"methodology file escapes package directory: {relative_path}"
        )
    return resolved


def _unique_by_id(items: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise MethodologyValidationError(f"{label} has an invalid id")
        if item_id in output:
            raise MethodologyValidationError(f"duplicate {label} id: {item_id}")
        output[item_id] = item
    return output


def load_methodology(directory: str | Path) -> MethodologyConfig:
    root = Path(directory).resolve()
    manifest = _read_json_yaml(root / "manifest.yaml")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise MethodologyValidationError("manifest.files must be an object")

    required_files = {"dimensions", "indicators", "scoring", "gates"}
    if set(files) != required_files:
        raise MethodologyValidationError(
            f"manifest.files must contain exactly {sorted(required_files)}"
        )

    documents = {
        key: _read_json_yaml(_resolve_member(root, files[key]))
        for key in sorted(required_files)
    }
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise MethodologyValidationError("manifest.version must be a non-empty string")
    for name, document in documents.items():
        if document.get("methodology_version") != version:
            raise MethodologyValidationError(f"version mismatch in {name}")

    raw_dimensions = _unique_by_id(documents["dimensions"].get("dimensions", []), "dimension")
    dimensions = {
        item_id: DimensionDefinition(
            id=item_id,
            name=str(item["name"]),
            weight=float(item["weight"]),
            score_enabled=bool(item["score_enabled"]),
        )
        for item_id, item in raw_dimensions.items()
    }

    raw_indicators = _unique_by_id(documents["indicators"].get("indicators", []), "indicator")
    indicators: dict[str, IndicatorDefinition] = {}
    slugs: set[str] = set()
    for item_id, item in raw_indicators.items():
        slug = str(item.get("slug", ""))
        if not slug or slug in slugs:
            raise MethodologyValidationError(
                f"indicator {item_id} has an empty or duplicate slug"
            )
        slugs.add(slug)
        dimension = item.get("dimension")
        if dimension is not None and dimension not in dimensions:
            raise MethodologyValidationError(
                f"indicator {item_id} references unknown dimension {dimension}"
            )
        score = item.get("score")
        if not isinstance(score, dict):
            raise MethodologyValidationError(f"indicator {item_id} has invalid score config")
        score_enabled = bool(score.get("enabled"))
        weight = float(score.get("weight", -1))
        if score_enabled and weight <= 0:
            raise MethodologyValidationError(
                f"scored indicator {item_id} must have positive weight"
            )
        if not score_enabled and weight != 0:
            raise MethodologyValidationError(
                f"unscored indicator {item_id} must have zero weight"
            )
        if item.get("classification") == "observation_context" and score_enabled:
            raise MethodologyValidationError(
                f"observation-context indicator {item_id} cannot score"
            )
        indicators[item_id] = IndicatorDefinition(
            id=item_id,
            slug=slug,
            dimension=dimension,
            classification=str(item["classification"]),
            score_enabled=score_enabled,
            weight=weight,
            score_rule=str(score["rule"]),
            blocking_candidates=tuple(item.get("blocking_candidates", [])),
        )

    raw_gates = _unique_by_id(documents["gates"].get("gates", []), "gate")
    gates: dict[str, GateDefinition] = {}
    for gate_id, item in raw_gates.items():
        if item.get("status") not in {"candidate", "active", "retired"}:
            raise MethodologyValidationError(f"gate {gate_id} has invalid status")
        if item.get("effect") != "blocked":
            raise MethodologyValidationError(f"gate {gate_id} has invalid effect")
        minimum_confidence = float(item.get("minimum_confidence", -1))
        if not 0 <= minimum_confidence <= 1:
            raise MethodologyValidationError(
                f"gate {gate_id} minimum confidence must be between 0 and 1"
            )
        conditions: list[GateCondition] = []
        for condition in item.get("all", []):
            indicator_id = condition.get("indicator")
            if indicator_id not in indicators:
                raise MethodologyValidationError(
                    f"gate {gate_id} references unknown indicator {indicator_id}"
                )
            states = condition.get("state_in")
            parsed_states = (
                tuple(ResultState(state) for state in states) if states is not None else None
            )
            score_lte = condition.get("score_lte")
            if (parsed_states is None) == (score_lte is None):
                raise MethodologyValidationError(
                    f"gate {gate_id} condition must define exactly one operator"
                )
            conditions.append(
                GateCondition(
                    indicator_id=indicator_id,
                    states=parsed_states,
                    score_lte=float(score_lte) if score_lte is not None else None,
                )
            )
        if not conditions:
            raise MethodologyValidationError(f"gate {gate_id} must have conditions")
        gates[gate_id] = GateDefinition(
            id=gate_id,
            name=str(item["name"]),
            status=str(item["status"]),
            effect=str(item["effect"]),
            minimum_confidence=minimum_confidence,
            conditions=tuple(conditions),
        )

    for indicator in indicators.values():
        unknown = set(indicator.blocking_candidates) - set(gates)
        if unknown:
            raise MethodologyValidationError(
                f"indicator {indicator.id} references unknown gates {sorted(unknown)}"
            )

    scoring = documents["scoring"]
    try:
        status_score_map = {
            ResultState(key): float(value)
            for key, value in scoring["status_score_map"].items()
        }
        excluded_states = frozenset(
            ResultState(value) for value in scoring["excluded_from_score"]
        )
        minimum_coverage = float(
            scoring["dimension_scoring"]["minimum_coverage_for_nominal_assessment"]
        )
        legacy_weights = {
            key: float(value)
            for key, value in scoring["legacy_clear_plus"]["facet_max_weights"].items()
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise MethodologyValidationError("invalid scoring configuration") from exc

    if set(status_score_map) != {
        ResultState.PASS,
        ResultState.WARNING,
        ResultState.FAIL,
    }:
        raise MethodologyValidationError("status_score_map must define pass, warning and fail")
    if any(not 0 <= value <= 100 for value in status_score_map.values()):
        raise MethodologyValidationError("status scores must be between 0 and 100")
    if excluded_states != {
        ResultState.UNKNOWN,
        ResultState.NOT_APPLICABLE,
        ResultState.CONTEXT,
    }:
        raise MethodologyValidationError(
            "excluded states must be unknown, not_applicable and context"
        )
    if not 0 <= minimum_coverage <= 1:
        raise MethodologyValidationError("minimum coverage must be between 0 and 1")
    if set(legacy_weights) != {"FA", "FS", "FC", "FM"}:
        raise MethodologyValidationError("legacy facet weights must define FA, FS, FC and FM")

    return MethodologyConfig(
        id=str(manifest["methodology_id"]),
        version=version,
        status=str(manifest["status"]),
        dimensions=MappingProxyType(dimensions),
        indicators=MappingProxyType(indicators),
        gates=MappingProxyType(gates),
        status_score_map=MappingProxyType(status_score_map),
        excluded_states=excluded_states,
        minimum_coverage=minimum_coverage,
        legacy_facet_max_weights=MappingProxyType(legacy_weights),
    )
