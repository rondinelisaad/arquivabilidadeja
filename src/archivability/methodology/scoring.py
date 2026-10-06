from __future__ import annotations

from types import MappingProxyType
from typing import Collection, Iterable, Mapping

from archivability.methodology.models import (
    AssessmentResult,
    DimensionResult,
    GateDefinition,
    GateEvaluation,
    GateState,
    IndicatorDefinition,
    IndicatorResult,
    LegacyClearPlusInput,
    LegacyClearPlusResult,
    MethodologyConfig,
    OverallState,
    ResultState,
    ScoringInputError,
)


class ScoringEngine:
    """Deterministic, side-effect-free evaluator for a loaded methodology."""

    def __init__(self, methodology: MethodologyConfig) -> None:
        self.methodology = methodology

    def evaluate(
        self,
        indicator_results: Iterable[IndicatorResult],
        applicable_indicator_ids: Collection[str],
        *,
        activated_gate_ids: Collection[str] = (),
        legacy_clear_plus: LegacyClearPlusInput | None = None,
    ) -> AssessmentResult:
        results = self._validate_results(indicator_results)
        applicable = self._validate_applicable_ids(applicable_indicator_ids)
        self._validate_result_applicability(results, applicable)
        activated = self._validate_activated_gates(activated_gate_ids)

        dimensions = self._score_dimensions(results, applicable)
        gates = self._evaluate_gates(results, activated)
        overall = self._overall_state(dimensions, gates)
        legacy = self._legacy_scores(legacy_clear_plus) if legacy_clear_plus else None

        return AssessmentResult(
            methodology_version=self.methodology.version,
            overall_state=overall,
            dimensions=MappingProxyType(dimensions),
            gates=MappingProxyType(gates),
            legacy_clear_plus=legacy,
        )

    def _validate_results(
        self, values: Iterable[IndicatorResult]
    ) -> dict[str, IndicatorResult]:
        output: dict[str, IndicatorResult] = {}
        for result in values:
            if result.indicator_id not in self.methodology.indicators:
                raise ScoringInputError(f"unknown indicator result: {result.indicator_id}")
            if result.indicator_id in output:
                raise ScoringInputError(f"duplicate indicator result: {result.indicator_id}")
            definition = self.methodology.indicators[result.indicator_id]
            if not definition.score_enabled and result.state is not ResultState.CONTEXT:
                raise ScoringInputError(
                    f"unscored indicator {result.indicator_id} must use context state"
                )
            output[result.indicator_id] = result
        return output

    def _validate_applicable_ids(self, values: Collection[str]) -> frozenset[str]:
        applicable = frozenset(values)
        if len(applicable) != len(values):
            raise ScoringInputError("applicable indicator ids must be unique")
        unknown = applicable - set(self.methodology.indicators)
        if unknown:
            raise ScoringInputError(f"unknown applicable indicators: {sorted(unknown)}")
        return applicable

    def _validate_activated_gates(self, values: Collection[str]) -> frozenset[str]:
        activated = frozenset(values)
        if len(activated) != len(values):
            raise ScoringInputError("activated gate ids must be unique")
        unknown = activated - set(self.methodology.gates)
        if unknown:
            raise ScoringInputError(f"unknown gates requested for activation: {sorted(unknown)}")
        retired = {
            gate_id
            for gate_id in activated
            if self.methodology.gates[gate_id].status == "retired"
        }
        if retired:
            raise ScoringInputError(f"retired gates cannot be activated: {sorted(retired)}")
        return activated

    @staticmethod
    def _validate_result_applicability(
        results: Mapping[str, IndicatorResult], applicable: frozenset[str]
    ) -> None:
        inconsistent = sorted(
            indicator_id
            for indicator_id, result in results.items()
            if result.state is not ResultState.NOT_APPLICABLE
            and indicator_id not in applicable
        )
        if inconsistent:
            raise ScoringInputError(
                "results must be declared applicable unless their state is "
                f"not_applicable: {inconsistent}"
            )

    def _score_dimensions(
        self,
        results: Mapping[str, IndicatorResult],
        applicable: frozenset[str],
    ) -> dict[str, DimensionResult]:
        output: dict[str, DimensionResult] = {}
        for dimension_id, dimension in self.methodology.dimensions.items():
            if not dimension.score_enabled:
                continue
            expected = sorted(
                indicator_id
                for indicator_id in applicable
                if (definition := self.methodology.indicators[indicator_id]).dimension
                == dimension_id
                and definition.score_enabled
            )
            if not expected:
                continue

            expected_weight = sum(
                self.methodology.indicators[indicator_id].weight
                for indicator_id in expected
            )
            known = [
                indicator_id
                for indicator_id in expected
                if indicator_id in results
                and results[indicator_id].state in self.methodology.status_score_map
            ]
            unknown = tuple(item for item in expected if item not in known)
            known_weight = sum(
                self.methodology.indicators[indicator_id].weight for indicator_id in known
            )
            coverage = known_weight / expected_weight if expected_weight else 0.0
            score = None
            if known_weight:
                score = sum(
                    self.methodology.indicators[indicator_id].weight
                    * self._numeric_score(
                        self.methodology.indicators[indicator_id], results[indicator_id]
                    )
                    for indicator_id in known
                ) / known_weight

            output[dimension_id] = DimensionResult(
                dimension_id=dimension_id,
                score=score,
                coverage=coverage,
                status=(
                    "sufficient"
                    if coverage >= self.methodology.minimum_coverage
                    else "insufficient_evidence"
                ),
                known_indicator_ids=tuple(known),
                unknown_indicator_ids=unknown,
            )
        return output

    def _numeric_score(
        self, definition: IndicatorDefinition, result: IndicatorResult
    ) -> float:
        if not definition.score_enabled:
            raise ScoringInputError(f"indicator {definition.id} is not score-enabled")
        if result.state not in self.methodology.status_score_map:
            raise ScoringInputError(
                f"indicator {definition.id} state {result.state.value} is not numeric"
            )
        if result.score is not None:
            return result.score
        return self.methodology.status_score_map[result.state]

    def _evaluate_gates(
        self,
        results: Mapping[str, IndicatorResult],
        activated: frozenset[str],
    ) -> dict[str, GateEvaluation]:
        output: dict[str, GateEvaluation] = {}
        for gate_id, gate in self.methodology.gates.items():
            active = gate.status == "active" or gate_id in activated
            if not active:
                output[gate_id] = GateEvaluation(
                    gate_id, GateState.INACTIVE, "gate is not active in this evaluation"
                )
                continue
            output[gate_id] = self._evaluate_active_gate(gate, results)
        return output

    def _evaluate_active_gate(
        self,
        gate: GateDefinition,
        results: Mapping[str, IndicatorResult],
    ) -> GateEvaluation:
        required = [results.get(condition.indicator_id) for condition in gate.conditions]
        if any(result is None for result in required):
            return GateEvaluation(
                gate.id, GateState.UNKNOWN, "one or more required indicators are missing"
            )
        concrete = [result for result in required if result is not None]
        if any(result.confidence < gate.minimum_confidence for result in concrete):
            return GateEvaluation(
                gate.id, GateState.UNKNOWN, "minimum confidence was not reached"
            )
        for condition, result in zip(gate.conditions, concrete, strict=True):
            if condition.states is not None and result.state not in condition.states:
                return GateEvaluation(
                    gate.id, GateState.NOT_TRIGGERED, "state condition did not match"
                )
            if condition.score_lte is not None:
                definition = self.methodology.indicators[result.indicator_id]
                if result.state not in self.methodology.status_score_map:
                    return GateEvaluation(
                        gate.id, GateState.UNKNOWN, "numeric condition has no known score"
                    )
                if self._numeric_score(definition, result) > condition.score_lte:
                    return GateEvaluation(
                        gate.id, GateState.NOT_TRIGGERED, "score condition did not match"
                    )
        return GateEvaluation(gate.id, GateState.TRIGGERED, "all gate conditions matched")

    @staticmethod
    def _overall_state(
        dimensions: Mapping[str, DimensionResult],
        gates: Mapping[str, GateEvaluation],
    ) -> OverallState:
        if any(value.state is GateState.TRIGGERED for value in gates.values()):
            return OverallState.BLOCKED
        if not dimensions:
            return OverallState.INDETERMINATE
        if any(value.state is GateState.UNKNOWN for value in gates.values()):
            return OverallState.PARTIALLY_ASSESSABLE
        if any(value.status == "insufficient_evidence" for value in dimensions.values()):
            return OverallState.PARTIALLY_ASSESSABLE
        return OverallState.ASSESSABLE

    def _legacy_scores(
        self, value: LegacyClearPlusInput
    ) -> LegacyClearPlusResult:
        required = {"FA", "FS", "FC", "FM"}
        try:
            facets = {key: float(score) for key, score in value.facets.items()}
            applicable = {
                key: float(weight)
                for key, weight in value.applicable_facet_weights.items()
            }
        except (TypeError, ValueError) as exc:
            raise ScoringInputError("legacy values and weights must be numeric") from exc
        if set(facets) != required:
            raise ScoringInputError("legacy facets must define FA, FS, FC and FM")
        if set(applicable) != required:
            raise ScoringInputError(
                "applicable legacy weights must define FA, FS, FC and FM"
            )
        if any(not 0 <= score <= 100 for score in facets.values()):
            raise ScoringInputError("legacy facet scores must be between 0 and 100")
        if any(weight < 0 for weight in applicable.values()):
            raise ScoringInputError("applicable legacy weights cannot be negative")
        if sum(applicable.values()) <= 0:
            raise ScoringInputError("applicable legacy weights must have a positive sum")

        maximum = self.methodology.legacy_facet_max_weights
        return LegacyClearPlusResult(
            flat=sum(facets[key] for key in required) / len(required),
            fixed_max=sum(maximum[key] * facets[key] for key in required)
            / sum(maximum.values()),
            paper_applicable=sum(
                applicable[key] * facets[key] for key in required
            )
            / sum(applicable[key] for key in required),
        )
