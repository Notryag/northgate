import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from northgate.inspect import InspectConfig, OperatorDiagnosticsClient

server = FastMCP(
    "Northgate Diagnostics",
    instructions=(
        "Inspect Northgate accounting and settlement state through the read-only Operator API. "
        "Correlation metadata is evidence for grouping, not authorization."
    ),
    json_response=True,
)

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
_config: InspectConfig | None = None


def _configure_dependency_logging() -> None:
    # httpx INFO messages include full query strings and correlation values.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _execute(action: Callable[[OperatorDiagnosticsClient], dict[str, object]]) -> dict[str, object]:
    config = _config or InspectConfig.from_environment()
    client = OperatorDiagnosticsClient(config)
    try:
        return action(client)
    finally:
        client.close()


@server.tool(annotations=_READ_ONLY, structured_output=True)
def inspect_correlated_run(
    metadata_value: Annotated[str, Field(min_length=1, max_length=256)],
    metadata_key: Annotated[str, Field(min_length=1, max_length=64)] = "run_id",
    start: str | None = None,
    end: str | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
) -> dict[str, object]:
    """Inspect bounded requests sharing an operator-selected metadata value."""
    return _execute(
        lambda client: client.inspect_correlated(
            metadata_key=metadata_key,
            metadata_value=metadata_value,
            start=start,
            end=end,
            limit=limit,
        )
    )


@server.tool(annotations=_READ_ONLY, structured_output=True)
def inspect_request(
    request_id: Annotated[str, Field(pattern=r"^req_[A-Za-z0-9_-]{8,120}$")],
) -> dict[str, object]:
    """Inspect one request with its attempts, settlement progress, and findings."""
    return _execute(lambda client: client.inspect_request(request_id))


@server.tool(annotations=_READ_ONLY, structured_output=True)
def get_provider_attempts(
    request_id: Annotated[str, Field(pattern=r"^req_[A-Za-z0-9_-]{8,120}$")],
) -> dict[str, object]:
    """Return ordered provider attempts and findings for one request."""
    diagnostic = _execute(lambda client: client.inspect_request(request_id))
    return {
        "schema_version": diagnostic["schema_version"],
        "request_id": request_id,
        "attempts": diagnostic.get("attempts", []),
        "findings": diagnostic.get("findings", []),
    }


@server.tool(annotations=_READ_ONLY, structured_output=True)
def find_stale_settlements(
    minimum_age_seconds: Annotated[int, Field(ge=30, le=86400)] = 300,
    limit: Annotated[int, Field(ge=1, le=100)] = 100,
) -> dict[str, object]:
    """Find stale ledger records and matching concurrency leases."""
    return _execute(
        lambda client: client.inspect_stale(
            minimum_age_seconds=minimum_age_seconds,
            limit=limit,
        )
    )


@server.tool(annotations=_READ_ONLY, structured_output=True)
def inspect_usage_range(
    metadata_key: Annotated[str, Field(min_length=1, max_length=64)],
    metadata_value: Annotated[str, Field(min_length=1, max_length=256)],
    group_by: Annotated[str | None, Field(min_length=1, max_length=64)] = None,
    start: str | None = None,
    end: str | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 100,
) -> dict[str, object]:
    """Aggregate bounded usage and findings for one metadata filter."""
    now = datetime.now(UTC)
    resolved_end = end or now.isoformat()
    resolved_start = start or (now - timedelta(hours=24)).isoformat()
    return _execute(
        lambda client: client.inspect_usage(
            metadata_key=metadata_key,
            metadata_value=metadata_value,
            group_by=group_by,
            start=resolved_start,
            end=resolved_end,
            limit=limit,
        )
    )


@server.tool(annotations=_READ_ONLY, structured_output=True)
def list_recent_correlations(
    application: Annotated[str, Field(min_length=1, max_length=200)],
    group_by: Annotated[str, Field(min_length=1, max_length=64)] = "run_id",
    since_seconds: Annotated[int, Field(ge=60, le=7776000)] = 7200,
    limit: Annotated[int, Field(ge=1, le=100)] = 100,
) -> dict[str, object]:
    """List recent grouped correlations for a Northgate application name or ID."""
    now = datetime.now(UTC)

    def inspect(client: OperatorDiagnosticsClient) -> dict[str, object]:
        application_id = client.resolve_application(application)
        return client.inspect_usage(
            metadata_key="northgate.application_id",
            metadata_value=application_id,
            group_by=group_by,
            start=(now - timedelta(seconds=since_seconds)).isoformat(),
            end=now.isoformat(),
            limit=limit,
        )

    return _execute(inspect)


@server.tool(annotations=_READ_ONLY, structured_output=True)
def diagnose_prompt_cache(
    metadata_value: Annotated[str, Field(min_length=1, max_length=256)],
    metadata_key: Annotated[str, Field(min_length=1, max_length=64)] = "run_id",
    start: str | None = None,
    end: str | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
) -> dict[str, object]:
    """Summarize provider prompt-cache and exact-cache evidence for correlated requests."""
    diagnostic = _execute(
        lambda client: client.inspect_correlated(
            metadata_key=metadata_key,
            metadata_value=metadata_value,
            start=start,
            end=end,
            limit=limit,
        )
    )
    cache_codes = {"CACHED_USAGE_MISSING", "PROMPT_CACHE_NOT_HIT", "EXACT_CACHE_BYPASSED"}
    findings = diagnostic.get("findings", [])
    cache_findings = [
        finding
        for finding in findings
        if isinstance(finding, dict) and finding.get("code") in cache_codes
    ]
    request_cache = []
    requests = diagnostic.get("requests", [])
    for item in requests if isinstance(requests, list) else []:
        request = item.get("request") if isinstance(item, dict) else None
        if not isinstance(request, dict):
            continue
        request_cache.append(
            {
                "request_id": request.get("request_id"),
                "prompt_tokens": request.get("prompt_tokens"),
                "cached_prompt_tokens": request.get("cached_prompt_tokens"),
                "cache_status": request.get("cache_status"),
            }
        )
    aggregate = diagnostic.get("aggregate")
    return {
        "schema_version": diagnostic["schema_version"],
        "correlation": diagnostic.get("correlation"),
        "has_more": diagnostic.get("has_more"),
        "aggregate": {
            key: aggregate.get(key)
            for key in (
                "requests",
                "prompt_tokens",
                "cached_prompt_tokens",
                "usage_missing_requests",
                "prompt_cache_percent",
            )
        }
        if isinstance(aggregate, dict)
        else {},
        "requests": request_cache,
        "findings": cache_findings,
    }


def main() -> None:
    global _config
    _config = InspectConfig.from_environment()
    _configure_dependency_logging()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
