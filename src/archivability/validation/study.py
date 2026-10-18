from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LABELS = ("fail", "warning", "pass")
_SPLITS = ("calibration", "holdout")


class ValidationInputError(ValueError):
    """Raised when a validation dataset is unsafe or internally inconsistent."""


def load_validation_dataset(path: Path) -> dict[str, Any]:
    if not isinstance(path, Path) or not path.is_file():
        raise ValidationInputError("validation dataset must be an existing file")
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValidationInputError("validation dataset exceeds 10 MiB")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationInputError(
            "validation dataset is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ValidationInputError("validation dataset root must be an object")
    return value


def analyze_validation_dataset(dataset: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _validate_dataset(dataset)
    canonical_input = json.dumps(
        normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    profiles = {item["id"]: item for item in normalized["threshold_profiles"]}
    primary = profiles[normalized["primary_threshold_profile"]]

    results: dict[str, Any] = {}
    for split in _SPLITS:
        cases = [item for item in normalized["cases"] if item["split"] == split]
        results[split] = _analyze_split(cases, profiles, primary)

    return {
        "report_schema_version": "1.0",
        "dataset_id": normalized["dataset_id"],
        "methodology_id": normalized["methodology_id"],
        "methodology_version": normalized["methodology_version"],
        "input_sha256": hashlib.sha256(canonical_input).hexdigest(),
        "primary_threshold_profile": primary["id"],
        "category_order": list(_LABELS),
        "splits": results,
        "interpretation": {
            "status": "descriptive_only",
            "limitations": [
                "Metrics do not establish construct validity or causality.",
                "Calibration and holdout results must never be pooled for threshold selection.",
                "Expert identities are opaque and website URLs are excluded from this artifact.",
                "A scientific release still requires a preregistered protocol, representative sampling, and independent review.",
            ],
        },
    }


def _validate_dataset(dataset: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema_version",
        "dataset_id",
        "methodology_id",
        "methodology_version",
        "primary_threshold_profile",
        "threshold_profiles",
        "cases",
    }
    if set(dataset) != allowed:
        raise ValidationInputError("validation dataset fields do not match schema 1.0")
    if dataset["schema_version"] != "1.0":
        raise ValidationInputError("unsupported validation dataset schema")
    for field in ("dataset_id", "methodology_id", "methodology_version"):
        _require_identifier(dataset[field], field)

    raw_profiles = dataset["threshold_profiles"]
    if not isinstance(raw_profiles, list) or not 1 <= len(raw_profiles) <= 32:
        raise ValidationInputError("threshold_profiles must contain 1 to 32 profiles")
    profiles = [_validate_profile(item) for item in raw_profiles]
    profile_ids = [item["id"] for item in profiles]
    if len(set(profile_ids)) != len(profile_ids):
        raise ValidationInputError("threshold profile ids must be unique")
    primary = dataset["primary_threshold_profile"]
    if primary not in profile_ids:
        raise ValidationInputError("primary threshold profile does not exist")

    raw_cases = dataset["cases"]
    if not isinstance(raw_cases, list) or not 2 <= len(raw_cases) <= 100_000:
        raise ValidationInputError("cases must contain 2 to 100000 cases")
    cases = [_validate_case(item) for item in raw_cases]
    case_ids = [item["case_id"] for item in cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValidationInputError("case ids must be unique across both splits")
    for split in _SPLITS:
        if not any(item["split"] == split for item in cases):
            raise ValidationInputError(f"dataset must contain a {split} split")

    return {
        "schema_version": "1.0",
        "dataset_id": dataset["dataset_id"],
        "methodology_id": dataset["methodology_id"],
        "methodology_version": dataset["methodology_version"],
        "primary_threshold_profile": primary,
        "threshold_profiles": profiles,
        "cases": cases,
    }


def _validate_profile(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"id", "warning_min", "pass_min"}:
        raise ValidationInputError("threshold profile fields are invalid")
    _require_identifier(value["id"], "threshold profile id")
    warning_min = _require_score(value["warning_min"], "warning_min")
    pass_min = _require_score(value["pass_min"], "pass_min")
    if not warning_min < pass_min:
        raise ValidationInputError("warning_min must be lower than pass_min")
    return {"id": value["id"], "warning_min": warning_min, "pass_min": pass_min}


def _validate_case(value: Any) -> dict[str, Any]:
    fields = {"case_id", "split", "strata", "dimension_scores", "expert_ratings"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationInputError("validation case fields are invalid")
    _require_identifier(value["case_id"], "case_id")
    if value["split"] not in _SPLITS:
        raise ValidationInputError("case split must be calibration or holdout")
    strata = value["strata"]
    if not isinstance(strata, list) or not 1 <= len(strata) <= 32:
        raise ValidationInputError("case strata must contain 1 to 32 identifiers")
    for item in strata:
        _require_identifier(item, "stratum")
    if len(set(strata)) != len(strata):
        raise ValidationInputError("case strata must be unique")

    raw_scores = value["dimension_scores"]
    if not isinstance(raw_scores, dict) or not 1 <= len(raw_scores) <= 32:
        raise ValidationInputError("dimension_scores must be a non-empty object")
    scores: dict[str, float] = {}
    for dimension, score in raw_scores.items():
        _require_identifier(dimension, "dimension id")
        scores[dimension] = _require_score(score, "dimension score")

    ratings = value["expert_ratings"]
    if not isinstance(ratings, dict) or not 2 <= len(ratings) <= 100:
        raise ValidationInputError("each case requires 2 to 100 expert ratings")
    normalized_ratings: dict[str, dict[str, str]] = {}
    expected_dimensions = set(scores)
    for expert_id, raw_rating in ratings.items():
        _require_identifier(expert_id, "expert id")
        if not isinstance(raw_rating, dict) or set(raw_rating) != expected_dimensions:
            raise ValidationInputError("experts must rate every scored dimension")
        if any(label not in _LABELS for label in raw_rating.values()):
            raise ValidationInputError("expert rating label is invalid")
        normalized_ratings[expert_id] = dict(sorted(raw_rating.items()))

    return {
        "case_id": value["case_id"],
        "split": value["split"],
        "strata": sorted(strata),
        "dimension_scores": dict(sorted(scores.items())),
        "expert_ratings": dict(sorted(normalized_ratings.items())),
    }


def _analyze_split(
    cases: list[dict[str, Any]],
    profiles: Mapping[str, dict[str, Any]],
    primary: dict[str, Any],
) -> dict[str, Any]:
    dimensions = sorted({key for case in cases for key in case["dimension_scores"]})
    expert_ids = sorted({key for case in cases for key in case["expert_ratings"]})
    strata = Counter(item for case in cases for item in case["strata"])
    dimension_results: dict[str, Any] = {}
    for dimension in dimensions:
        relevant = [case for case in cases if dimension in case["dimension_scores"]]
        rating_rows = [
            [rating[dimension] for rating in case["expert_ratings"].values()]
            for case in relevant
        ]
        consensus = [_majority(row) for row in rating_rows]
        model = [
            _classify(case["dimension_scores"][dimension], primary) for case in relevant
        ]
        comparable = [
            (predicted, expected)
            for predicted, expected in zip(model, consensus, strict=True)
            if expected is not None
        ]
        dimension_results[dimension] = {
            "case_count": len(relevant),
            "expert_fleiss_kappa": _fleiss_kappa(rating_rows),
            "expert_consensus_ties": sum(item is None for item in consensus),
            "model_consensus": _agreement(comparable),
        }

    comparisons: dict[str, Any] = {}
    primary_labels = _all_labels(cases, primary)
    for profile_id, profile in sorted(profiles.items()):
        labels = _all_labels(cases, profile)
        changed = sum(
            left != right for left, right in zip(primary_labels, labels, strict=True)
        )
        comparisons[profile_id] = {
            "classified_values": len(labels),
            "changed_from_primary": changed,
            "changed_rate": _ratio(changed, len(labels)),
        }

    return {
        "case_count": len(cases),
        "expert_count": len(expert_ids),
        "strata_counts": dict(sorted(strata.items())),
        "dimensions": dimension_results,
        "threshold_sensitivity": comparisons,
    }


def _all_labels(cases: list[dict[str, Any]], profile: dict[str, Any]) -> list[str]:
    return [
        _classify(case["dimension_scores"][dimension], profile)
        for case in cases
        for dimension in sorted(case["dimension_scores"])
    ]


def _classify(score: float, profile: Mapping[str, Any]) -> str:
    if score >= profile["pass_min"]:
        return "pass"
    if score >= profile["warning_min"]:
        return "warning"
    return "fail"


def _majority(labels: list[str]) -> str | None:
    counts = Counter(labels)
    top = max(counts.values())
    winners = sorted(label for label, count in counts.items() if count == top)
    return winners[0] if len(winners) == 1 else None


def _agreement(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    if not pairs:
        return {"comparable_cases": 0, "accuracy": None, "cohen_kappa": None}
    correct = sum(predicted == expected for predicted, expected in pairs)
    predicted_counts = Counter(predicted for predicted, _ in pairs)
    expected_counts = Counter(expected for _, expected in pairs)
    size = len(pairs)
    observed = correct / size
    expected = sum(
        predicted_counts[label] * expected_counts[label] for label in _LABELS
    ) / (size * size)
    kappa = (
        None if math.isclose(expected, 1.0) else (observed - expected) / (1 - expected)
    )
    return {
        "comparable_cases": size,
        "accuracy": round(observed, 6),
        "cohen_kappa": None if kappa is None else round(kappa, 6),
    }


def _fleiss_kappa(rows: list[list[str]]) -> float | None:
    if len(rows) < 2 or not rows:
        return None
    raters = len(rows[0])
    if raters < 2 or any(len(row) != raters for row in rows):
        return None
    counts = [Counter(row) for row in rows]
    observed = sum(
        (sum(count * count for count in item.values()) - raters)
        / (raters * (raters - 1))
        for item in counts
    ) / len(rows)
    totals = Counter(label for row in rows for label in row)
    total_ratings = len(rows) * raters
    expected = sum((totals[label] / total_ratings) ** 2 for label in _LABELS)
    if math.isclose(expected, 1.0):
        return None
    return round((observed - expected) / (1 - expected), 6)


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _require_identifier(value: Any, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValidationInputError(f"{field} must be an opaque identifier")


def _require_score(value: Any, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 100
    ):
        raise ValidationInputError(f"{field} must be a finite score from 0 to 100")
    return float(value)
