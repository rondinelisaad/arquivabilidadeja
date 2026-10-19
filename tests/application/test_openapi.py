from __future__ import annotations

import json

from archivability.application.openapi import build_openapi_document


def test_openapi_contract_is_json_serializable_and_covers_router() -> None:
    document = build_openapi_document(methodology_version="0.1.0")

    assert document["openapi"] == "3.1.0"
    assert document["security"] == [{"bearerAuth": []}]
    assert set(document["paths"]) == {
        "/v1/analyses",
        "/v1/batches",
        "/v1/analyses/{analysis_id}",
        "/v1/analyses/{analysis_id}/export.json",
        "/v1/analyses/{analysis_id}/comparison",
        "/v1/sites/{domain}/history",
        "/v1/methodology",
        "/v1/indicators",
        "/v1/indicators/{identifier}",
    }
    operation_ids = {
        operation["operationId"]
        for path in document["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post"}
    }
    assert len(operation_ids) == 10
    assert "createAnalysis" in operation_ids
    assert "getSiteHistory" in operation_ids
    assert "getIndicator" in operation_ids
    json.dumps(document, ensure_ascii=False, allow_nan=False)


def test_openapi_write_schemas_are_closed_and_bounded() -> None:
    schemas = build_openapi_document(methodology_version="0.1.0")["components"][
        "schemas"
    ]

    assert schemas["AnalysisRequest"]["additionalProperties"] is False
    assert schemas["AnalysisRequest"]["properties"]["subject_uri"]["maxLength"] == 2048
    assert schemas["BatchRequest"]["properties"]["subjects"]["maxItems"] == 25
