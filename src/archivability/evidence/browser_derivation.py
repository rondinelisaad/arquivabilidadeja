from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from archivability.browser.models import (
    BrowserBudgets,
    BrowserContractError,
    BrowserIsolationAttestation,
    BrowserProfile,
    BrowserRenderAggregate,
)
from archivability.evidence.derivation import derive_indicator_result
from archivability.evidence.models import Evidence, EvidenceValidationError, Observation
from archivability.methodology.models import (
    IndicatorResult,
    MethodologyConfig,
    ResultState,
)

_INDICATORS = ("D07", "C01", "C05", "O05")


class BrowserDerivationError(EvidenceValidationError):
    """Raised when browser evidence is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class BrowserDerivation:
    evidence: tuple[Evidence, ...]
    indicator_results: tuple[IndicatorResult, ...]

    def __post_init__(self) -> None:
        if tuple(item.indicator_id for item in self.evidence) != _INDICATORS:
            raise BrowserDerivationError("browser evidence set is incomplete")
        if tuple(item.indicator_id for item in self.indicator_results) != _INDICATORS:
            raise BrowserDerivationError("browser result set is incomplete")


def derive_browser_indicators(
    methodology: MethodologyConfig,
    http_observation: Observation,
    browser_observation: Observation | None,
) -> BrowserDerivation:
    """Derive conservative browser indicators from one bounded render."""
    if browser_observation is None:
        return _unknown_derivation(methodology, http_observation)
    if (
        browser_observation.analysis_id != http_observation.analysis_id
        or browser_observation.attempt_id != http_observation.attempt_id
        or browser_observation.subject_uri != http_observation.subject_uri
    ):
        raise BrowserDerivationError("browser observation does not match HTTP provenance")
    budgets, profile, aggregate = _validate_payload(browser_observation)
    complete = aggregate.termination_reason == "complete" and not browser_observation.truncated
    rendered_denominator = max(aggregate.rendered_url_count, 1)
    rendered_gain_share = aggregate.additional_url_count / rendered_denominator
    request_denominator = max(aggregate.request_count, 1)
    external_request_share = aggregate.external_request_count / request_denominator
    api_denominator = max(aggregate.api_request_count, 1)
    external_api_share = aggregate.external_api_request_count / api_denominator
    dom_delta_share = abs(
        aggregate.rendered_dom_bytes - aggregate.raw_dom_bytes
    ) / max(aggregate.raw_dom_bytes, aggregate.rendered_dom_bytes, 1)
    dom_changed = aggregate.raw_dom_hash != aggregate.rendered_dom_hash

    if not complete:
        states = {indicator: ResultState.UNKNOWN for indicator in _INDICATORS}
    else:
        states = {
            "D07": _rendered_gain_state(aggregate, rendered_gain_share),
            "C01": (
                ResultState.WARNING
                if external_request_share > 0.25
                else ResultState.PASS
            ),
            "C05": _external_api_state(
                aggregate,
                external_api_share=external_api_share,
                dom_changed=dom_changed,
            ),
            "O05": _javascript_dependency_state(
                aggregate,
                rendered_gain_share=rendered_gain_share,
                dom_delta_share=dom_delta_share,
                dom_changed=dom_changed,
            ),
        }
    measurements: dict[str, Mapping[str, Any]] = {
        "D07": {
            "raw_url_count": aggregate.raw_url_count,
            "rendered_url_count": aggregate.rendered_url_count,
            "additional_url_count": aggregate.additional_url_count,
            "rendered_gain_share": round(rendered_gain_share, 6),
            "raw_url_set_hash": aggregate.raw_url_set_hash,
            "rendered_url_set_hash": aggregate.rendered_url_set_hash,
        },
        "C01": {
            "request_count": aggregate.request_count,
            "internal_request_count": aggregate.internal_request_count,
            "external_request_count": aggregate.external_request_count,
            "external_request_share": round(external_request_share, 6),
        },
        "C05": {
            "api_request_count": aggregate.api_request_count,
            "external_api_request_count": aggregate.external_api_request_count,
            "external_api_share": round(external_api_share, 6),
            "rendered_content_changed": dom_changed,
            "additional_url_count": aggregate.additional_url_count,
        },
        "O05": {
            "raw_dom_hash": aggregate.raw_dom_hash,
            "rendered_dom_hash": aggregate.rendered_dom_hash,
            "raw_dom_bytes": aggregate.raw_dom_bytes,
            "rendered_dom_bytes": aggregate.rendered_dom_bytes,
            "dom_delta_share": round(dom_delta_share, 6),
            "api_request_count": aggregate.api_request_count,
            "additional_url_count": aggregate.additional_url_count,
        },
    }
    evidence = tuple(
        _evidence(
            browser_observation,
            indicator_id=indicator,
            data={
                **measurements[indicator],
                "termination_reason": aggregate.termination_reason,
                "profile_id": profile.profile_id,
                "budgets": budgets.to_dict(),
                "threshold_basis": "project_hypothesis_v1",
            },
            confidence=0.8 if complete else 0.0,
        )
        for indicator in _INDICATORS
    )
    results = tuple(
        derive_indicator_result(
            methodology,
            indicator_id=indicator,
            state=states[indicator],
            evidence=(item,),
        )
        for indicator, item in zip(_INDICATORS, evidence, strict=True)
    )
    return BrowserDerivation(evidence, results)


def _validate_payload(
    observation: Observation,
) -> tuple[BrowserBudgets, BrowserProfile, BrowserRenderAggregate]:
    if (
        observation.kind != "browser_render"
        or observation.payload_schema_version != "1.0"
        or observation.error_code is not None
        or set(observation.payload) != {"budgets", "profile", "isolation", "aggregate"}
    ):
        raise BrowserDerivationError("browser observation contract is invalid")
    try:
        budgets = BrowserBudgets(**observation.payload["budgets"])
        profile = BrowserProfile(**observation.payload["profile"])
        BrowserIsolationAttestation(**observation.payload["isolation"])
        aggregate = BrowserRenderAggregate(**observation.payload["aggregate"])
        aggregate.validate_against(budgets)
    except (BrowserContractError, TypeError) as exc:
        raise BrowserDerivationError("browser observation payload failed validation") from exc
    return budgets, profile, aggregate


def _rendered_gain_state(
    aggregate: BrowserRenderAggregate, share: float
) -> ResultState:
    if aggregate.additional_url_count == 0:
        return ResultState.PASS
    if aggregate.raw_url_count == 0 or share > 0.5:
        return ResultState.FAIL
    return ResultState.WARNING


def _external_api_state(
    aggregate: BrowserRenderAggregate,
    *,
    external_api_share: float,
    dom_changed: bool,
) -> ResultState:
    if aggregate.api_request_count == 0:
        return ResultState.NOT_APPLICABLE
    if aggregate.external_api_request_count == 0:
        return ResultState.PASS
    if not dom_changed and aggregate.additional_url_count == 0:
        return ResultState.UNKNOWN
    return ResultState.FAIL if external_api_share >= 0.5 else ResultState.WARNING


def _javascript_dependency_state(
    aggregate: BrowserRenderAggregate,
    *,
    rendered_gain_share: float,
    dom_delta_share: float,
    dom_changed: bool,
) -> ResultState:
    if not dom_changed and aggregate.additional_url_count == 0 and aggregate.api_request_count == 0:
        return ResultState.PASS
    if (
        aggregate.raw_dom_bytes == 0 < aggregate.rendered_dom_bytes
        or rendered_gain_share > 0.5
        or (dom_delta_share > 0.5 and aggregate.api_request_count > 0)
    ):
        return ResultState.FAIL
    return ResultState.WARNING


def _unknown_derivation(
    methodology: MethodologyConfig, observation: Observation
) -> BrowserDerivation:
    evidence = tuple(
        _evidence(
            observation,
            indicator_id=indicator,
            data={"reason": "browser_observation_unavailable"},
            confidence=0.0,
        )
        for indicator in _INDICATORS
    )
    results = tuple(
        derive_indicator_result(
            methodology,
            indicator_id=indicator,
            state=ResultState.UNKNOWN,
            evidence=(item,),
        )
        for indicator, item in zip(_INDICATORS, evidence, strict=True)
    )
    return BrowserDerivation(evidence, results)


def _evidence(
    observation: Observation,
    *,
    indicator_id: str,
    data: Mapping[str, Any],
    confidence: float,
) -> Evidence:
    return Evidence.create(
        evidence_id=f"browser-{indicator_id.lower()}-v1-0-0-{observation.content_hash}",
        analysis_id=observation.analysis_id,
        indicator_id=indicator_id,
        kind="browser_indicator_measurement",
        subject_uri=observation.subject_uri,
        method_id=f"browser-{indicator_id.lower()}-derivation",
        method_version="1.0.0",
        created_at=observation.observed_at,
        confidence=confidence,
        observations=(observation,),
        data=data,
        summary="Bounded browser aggregate interpreted by versioned project thresholds.",
    )
