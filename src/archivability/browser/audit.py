from __future__ import annotations

from typing import Any

from archivability.browser.models import (
    BrowserContractError,
    BrowserRenderRequest,
    BrowserRenderResult,
)

_FAILURE_CODES = frozenset(
    {
        "BROWSER_CONTRACT_FAILED",
        "BROWSER_RENDER_FAILED",
        "BROWSER_PERSISTENCE_FAILED",
    }
)


def browser_render_event_extra(
    request: BrowserRenderRequest,
    status: str,
    observation_id: str | None,
    result: BrowserRenderResult | None,
    error_code: str | None,
) -> dict[str, Any]:
    """Build a validated audit payload that never includes the target URI or DOM."""
    valid = (
        status == "started"
        and observation_id is None
        and result is None
        and error_code is None
    ) or (
        status == "succeeded"
        and observation_id is not None
        and result is not None
        and error_code is None
    ) or (
        status == "reused"
        and observation_id is not None
        and result is None
        and error_code is None
    ) or (
        status == "failed"
        and observation_id is None
        and result is None
        and error_code in _FAILURE_CODES
    )
    if not valid:
        raise BrowserContractError("browser render audit event is inconsistent")
    if result is not None:
        result.validate_against(request)
    extra: dict[str, Any] = {
        "analysis_id": request.analysis_id,
        "status": status,
        "profile_id": request.profile.profile_id,
    }
    if status == "started":
        extra["budgets"] = request.budgets.to_dict()
    elif status == "succeeded":
        assert result is not None
        extra.update(
            {
                "observation_id": observation_id,
                "isolation_policy_id": result.isolation.policy_id,
                "termination_reason": result.aggregate.termination_reason,
                "request_count": result.aggregate.request_count,
                "api_request_count": result.aggregate.api_request_count,
                "failed_request_count": result.aggregate.failed_request_count,
                "blocked_request_count": result.aggregate.blocked_request_count,
                "transfer_bytes": result.aggregate.transfer_bytes,
                "rendered_dom_bytes": result.aggregate.rendered_dom_bytes,
                "elapsed_milliseconds": result.aggregate.elapsed_milliseconds,
            }
        )
    elif status == "reused":
        extra["observation_id"] = observation_id
    else:
        extra["error_code"] = error_code
    return extra
