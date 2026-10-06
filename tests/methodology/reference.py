"""Small dependency-free reference evaluator for methodology golden cases.

This is a test oracle, not the production scoring engine.
"""

from __future__ import annotations

from typing import Any


def score_dimensions(
    dimensions: dict[str, Any],
    indicators: dict[str, Any],
    scoring: dict[str, Any],
    applicable_ids: list[str],
    results: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    definitions = {item["id"]: item for item in indicators["indicators"]}
    score_map = scoring["status_score_map"]
    minimum_coverage = scoring["dimension_scoring"][
        "minimum_coverage_for_nominal_assessment"
    ]
    output: dict[str, dict[str, Any]] = {}

    for dimension in dimensions["dimensions"]:
        dimension_id = dimension["id"]
        expected = [
            indicator_id
            for indicator_id in applicable_ids
            if definitions[indicator_id]["dimension"] == dimension_id
            and definitions[indicator_id]["score"]["enabled"]
        ]
        if not expected:
            continue

        expected_weight = sum(definitions[item]["score"]["weight"] for item in expected)
        known = [item for item in expected if results.get(item, {}).get("state") in score_map]
        known_weight = sum(definitions[item]["score"]["weight"] for item in known)
        coverage = known_weight / expected_weight if expected_weight else 0.0

        score = None
        if known_weight:
            score = sum(
                definitions[item]["score"]["weight"]
                * score_map[results[item]["state"]]
                for item in known
            ) / known_weight

        output[dimension_id] = {
            "score": score,
            "coverage": coverage,
            "status": (
                "sufficient"
                if coverage >= minimum_coverage
                else "insufficient_evidence"
            ),
        }
    return output


def evaluate_gates(
    gates: dict[str, Any],
    scoring: dict[str, Any],
    results: dict[str, dict[str, Any]],
    overrides: dict[str, str] | None = None,
) -> list[str]:
    overrides = overrides or {}
    score_map = scoring["status_score_map"]
    triggered: list[str] = []

    for gate in gates["gates"]:
        status = overrides.get(gate["id"], gate["status"])
        if status != "active":
            continue

        matched = True
        confidences: list[float] = []
        for condition in gate["all"]:
            result = results.get(condition["indicator"])
            if result is None:
                matched = False
                break
            confidences.append(float(result.get("confidence", 0.0)))
            if "state_in" in condition:
                matched = matched and result["state"] in condition["state_in"]
            if "score_lte" in condition:
                value = result.get("score", score_map.get(result["state"]))
                matched = matched and value is not None and value <= condition["score_lte"]

        if matched and confidences and min(confidences) >= gate["minimum_confidence"]:
            triggered.append(gate["id"])
    return triggered


def legacy_scores(scoring: dict[str, Any], legacy: dict[str, Any]) -> dict[str, float]:
    facets = legacy["facets"]
    maximum = scoring["legacy_clear_plus"]["facet_max_weights"]
    applicable = legacy["applicable_facet_weights"]
    return {
        "flat": sum(facets.values()) / len(facets),
        "fixed_max": sum(maximum[key] * facets[key] for key in maximum)
        / sum(maximum.values()),
        "paper_applicable": sum(applicable[key] * facets[key] for key in applicable)
        / sum(applicable.values()),
    }


def evaluate_case(
    dimensions: dict[str, Any],
    indicators: dict[str, Any],
    scoring: dict[str, Any],
    gates: dict[str, Any],
    case: dict[str, Any],
) -> dict[str, Any]:
    dimension_results = score_dimensions(
        dimensions,
        indicators,
        scoring,
        case["applicable_indicator_ids"],
        case["indicator_results"],
    )
    triggered = evaluate_gates(
        gates,
        scoring,
        case["indicator_results"],
        case.get("gate_status_overrides"),
    )

    if triggered:
        overall_state = "blocked"
    elif not dimension_results:
        overall_state = "indeterminate"
    elif any(
        result["status"] == "insufficient_evidence"
        for result in dimension_results.values()
    ):
        overall_state = "partially_assessable"
    else:
        overall_state = "assessable"

    output: dict[str, Any] = {
        "overall_state": overall_state,
        "triggered_gates": triggered,
        "dimension_results": dimension_results,
    }
    if "legacy_clear_plus" in case:
        output["legacy_clear_plus"] = legacy_scores(scoring, case["legacy_clear_plus"])
    return output
