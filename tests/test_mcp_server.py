from collections.abc import AsyncGenerator

import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from northgate import mcp_server


class _DiagnosticsClient:
    def inspect_request(self, request_id: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "request": {"request_id": request_id, "outcome": "succeeded"},
            "attempts": [{"attempt_index": 1, "provider": "openai"}],
            "settlement": {"events": [{"status": "completed"}]},
            "findings": [],
        }

    def inspect_correlated(self, **_kwargs: object) -> dict[str, object]:
        return {
            "schema_version": 1,
            "correlation": {"metadata_key": "run_id", "metadata_value": "run-test"},
            "has_more": False,
            "aggregate": {
                "requests": 1,
                "prompt_tokens": 100,
                "cached_prompt_tokens": 80,
                "usage_missing_requests": 0,
                "prompt_cache_percent": 80.0,
            },
            "requests": [
                {
                    "request": {
                        "request_id": "req_12345678",
                        "prompt_tokens": 100,
                        "cached_prompt_tokens": 80,
                        "cache_status": "bypass",
                    }
                }
            ],
            "findings": [
                {
                    "code": "EXACT_CACHE_BYPASSED",
                    "severity": "info",
                    "request_id": "req_12345678",
                },
                {
                    "code": "RETRY_OR_FALLBACK_USED",
                    "severity": "info",
                    "request_id": "req_12345678",
                },
            ],
        }

    def inspect_stale(self, **_kwargs: object) -> dict[str, object]:
        return {
            "schema_version": 1,
            "minimum_age_seconds": 300,
            "requests": [],
            "findings": [],
        }

    def inspect_usage(self, **kwargs: object) -> dict[str, object]:
        return {
            "schema_version": 1,
            "filter": {
                "metadata_key": kwargs["metadata_key"],
                "metadata_value": kwargs["metadata_value"],
            },
            "group_by": kwargs["group_by"],
            "has_more": False,
            "aggregate": {"requests": 1, "total_tokens": 100},
            "groups": [{"metadata_value": "run-test"}],
            "requests": [],
            "findings": [],
        }

    def resolve_application(self, name_or_id: str) -> str:
        assert name_or_id == "dayboard"
        return "application-id"


@pytest.fixture
async def mcp_client(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[ClientSession]:
    diagnostics = _DiagnosticsClient()
    monkeypatch.setattr(mcp_server, "_execute", lambda action: action(diagnostics))
    async with create_connected_server_and_client_session(
        mcp_server.server,
        raise_exceptions=True,
    ) as session:
        yield session


@pytest.mark.anyio
async def test_mcp_exposes_only_bounded_read_only_diagnostic_tools(
    mcp_client: ClientSession,
) -> None:
    result = await mcp_client.list_tools()
    tools = {tool.name: tool for tool in result.tools}

    assert set(tools) == {
        "inspect_correlated_run",
        "inspect_request",
        "get_provider_attempts",
        "find_stale_settlements",
        "diagnose_prompt_cache",
        "inspect_usage_range",
        "list_recent_correlations",
    }
    for tool in tools.values():
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert "operator_key" not in str(tool.inputSchema)
    assert tools["inspect_correlated_run"].inputSchema["properties"]["limit"]["maximum"] == 100
    assert (
        tools["find_stale_settlements"].inputSchema["properties"]["minimum_age_seconds"]["minimum"]
        == 30
    )
    assert tools["inspect_usage_range"].inputSchema["properties"]["limit"]["maximum"] == 100
    assert (
        tools["list_recent_correlations"].inputSchema["properties"]["since_seconds"]["maximum"]
        == 7776000
    )


@pytest.mark.anyio
async def test_mcp_request_and_attempt_tools_return_versioned_structured_data(
    mcp_client: ClientSession,
) -> None:
    request_result = await mcp_client.call_tool(
        "inspect_request",
        {"request_id": "req_12345678"},
    )
    attempts_result = await mcp_client.call_tool(
        "get_provider_attempts",
        {"request_id": "req_12345678"},
    )

    assert request_result.isError is False
    assert request_result.structuredContent["schema_version"] == 1
    assert request_result.structuredContent["request"]["request_id"] == "req_12345678"
    assert attempts_result.structuredContent == {
        "schema_version": 1,
        "request_id": "req_12345678",
        "attempts": [{"attempt_index": 1, "provider": "openai"}],
        "findings": [],
    }


@pytest.mark.anyio
async def test_prompt_cache_tool_filters_unrelated_findings(mcp_client: ClientSession) -> None:
    result = await mcp_client.call_tool(
        "diagnose_prompt_cache",
        {"metadata_key": "run_id", "metadata_value": "run-test"},
    )

    assert result.isError is False
    assert result.structuredContent["aggregate"]["prompt_cache_percent"] == 80.0
    assert [finding["code"] for finding in result.structuredContent["findings"]] == [
        "EXACT_CACHE_BYPASSED"
    ]


@pytest.mark.anyio
async def test_usage_and_recent_tools_return_shared_usage_schema(
    mcp_client: ClientSession,
) -> None:
    usage_result = await mcp_client.call_tool(
        "inspect_usage_range",
        {
            "metadata_key": "user_id",
            "metadata_value": "user-test",
            "group_by": "run_id",
            "start": "2026-07-23T09:00:00+08:00",
            "end": "2026-07-23T10:00:00+08:00",
        },
    )
    recent_result = await mcp_client.call_tool(
        "list_recent_correlations",
        {"application": "dayboard", "group_by": "run_id", "since_seconds": 7200},
    )

    assert usage_result.isError is False
    assert usage_result.structuredContent["schema_version"] == 1
    assert usage_result.structuredContent["filter"] == {
        "metadata_key": "user_id",
        "metadata_value": "user-test",
    }
    assert recent_result.isError is False
    assert recent_result.structuredContent["filter"] == {
        "metadata_key": "northgate.application_id",
        "metadata_value": "application-id",
    }


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
