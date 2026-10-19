from __future__ import annotations

from typing import Any


def build_openapi_document(*, methodology_version: str) -> dict[str, Any]:
    """Build the versioned public contract for the implemented HTTP boundary."""
    paths: dict[str, Any] = {
        "/v1/analyses": {
            "get": _operation("listAnalyses", "List the authenticated owner's analyses", 200),
            "post": _operation("createAnalysis", "Create and enqueue one analysis", 202, request_body="#/components/schemas/AnalysisRequest"),
        },
        "/v1/batches": {
            "post": _operation("createBatch", "Create and enqueue a bounded batch", 202, request_body="#/components/schemas/BatchRequest"),
        },
        "/v1/analyses/{analysis_id}": {
            "parameters": [_path_parameter("analysis_id", "Opaque analysis identifier")],
            "get": _operation("getAnalysis", "Read an analysis report snapshot", 200),
        },
        "/v1/analyses/{analysis_id}/export.json": {
            "parameters": [_path_parameter("analysis_id", "Opaque analysis identifier")],
            "get": _operation("exportAnalysis", "Export the canonical report snapshot", 200),
        },
        "/v1/analyses/{analysis_id}/comparison": {
            "parameters": [_path_parameter("analysis_id", "Opaque analysis identifier")],
            "get": _operation("compareAnalysis", "Compare with the latest compatible analysis", 200),
        },
        "/v1/sites/{domain}/history": {
            "parameters": [_path_parameter("domain", "Exact normalized hostname")],
            "get": _operation("getSiteHistory", "List the owner's series for an exact hostname", 200),
        },
        "/v1/methodology": {
            "get": _operation("getMethodology", "Read the active methodology", 200),
        },
        "/v1/indicators": {
            "get": {
                **_operation("listIndicators", "List indicator definitions", 200),
                "parameters": [
                    _query_parameter("version"),
                    _query_parameter("dimension"),
                    _query_parameter("classification"),
                ],
            },
        },
        "/v1/indicators/{identifier}": {
            "parameters": [_path_parameter("identifier", "Indicator ID or slug")],
            "get": _operation("getIndicator", "Read one indicator definition", 200),
        },
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Arquivabilidade Já API",
            "version": "0.1.0",
            "description": (
                "Authenticated API for reproducible website archivability assessments. "
                f"Active methodology: {methodology_version}."
            ),
        },
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "security": [{"bearerAuth": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                }
            },
            "schemas": {
                "AnalysisRequest": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["subject_uri"],
                    "properties": {
                        "subject_uri": {
                            "type": "string",
                            "format": "uri",
                            "maxLength": 2048,
                        }
                    },
                },
                "BatchRequest": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["subjects"],
                    "properties": {
                        "subjects": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 25,
                            "items": {"$ref": "#/components/schemas/AnalysisRequest"},
                        }
                    },
                },
                "Error": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["error", "request_id"],
                    "properties": {
                        "error": {
                            "type": "object",
                            "required": ["code", "message"],
                            "properties": {
                                "code": {"type": "string"},
                                "message": {"type": "string"},
                            },
                        },
                        "request_id": {"type": "string"},
                    },
                },
            },
        },
    }


def _operation(
    operation_id: str,
    summary: str,
    success_status: int,
    *,
    request_body: str | None = None,
) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "operationId": operation_id,
        "summary": summary,
        "responses": {
            str(success_status): {"description": "Successful response"},
            "400": _error_response("Invalid request"),
            "401": _error_response("Authentication required"),
            "403": _error_response("Access denied"),
            "404": _error_response("Resource not found"),
            "429": _error_response("Rate limited"),
            "500": _error_response("Internal error"),
        },
    }
    if request_body is not None:
        operation["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": {"$ref": request_body}}},
        }
    return operation


def _error_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Error"}}
        },
    }


def _path_parameter(name: str, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "in": "path",
        "required": True,
        "description": description,
        "schema": {"type": "string", "minLength": 1, "maxLength": 256},
    }


def _query_parameter(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "in": "query",
        "required": False,
        "schema": {"type": "string", "minLength": 1},
    }
