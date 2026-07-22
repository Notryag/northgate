import httpx
import pytest
from httpx import AsyncClient

from northgate.attempt_execution import execute_provider_attempt, resolve_retryable_response
from northgate.routing import PolicyLimits, ResolvedRoute


def _route() -> ResolvedRoute:
    return ResolvedRoute(
        project_id=None,
        gateway_id=None,
        route_id=None,
        provider="test",
        base_url="https://provider.test/v1",
        api_key="provider-secret",
        allowed_metadata_keys=frozenset(),
        adapter="openai_compatible",
        adapter_config=(),
        policy=PolicyLimits(),
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("exception", "expected_failure"),
    [
        (httpx.ReadTimeout("timed out"), "provider_timeout"),
        (httpx.ConnectError("connection failed"), "connection_error"),
        (httpx.ReadError("response interrupted"), "transport_ambiguous"),
    ],
)
async def test_execute_provider_attempt_classifies_transport_failure(
    exception: httpx.TransportError,
    expected_failure: str,
) -> None:
    async def upstream(_request: httpx.Request) -> httpx.Response:
        raise exception

    client = AsyncClient(transport=httpx.MockTransport(upstream))
    result = None
    try:
        result = await execute_provider_attempt(
            client,
            _route(),
            forwarded_headers={"content-type": "application/json"},
            body=b"{}",
            model="gpt-test",
        )
    finally:
        await client.aclose()

    assert result.response is None
    assert result.failure == expected_failure


@pytest.mark.anyio
async def test_execute_provider_attempt_returns_streaming_response() -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer provider-secret"
        assert await request.aread() == b'{"model":"gpt-test"}'
        return httpx.Response(200, content=b'{"id":"response"}')

    client = AsyncClient(transport=httpx.MockTransport(upstream))
    try:
        result = await execute_provider_attempt(
            client,
            _route(),
            forwarded_headers={"content-type": "application/json"},
            body=b'{"model":"gpt-test"}',
            model="gpt-test",
        )
        assert result.failure is None
        assert result.response is not None
        assert await result.response.aread() == b'{"id":"response"}'
    finally:
        if result is not None and result.response is not None:
            await result.response.aclose()
        await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status_code", "has_next", "expected_outcome", "passes_through"),
    [
        (502, False, "retryable_status", False),
        (429, True, "retryable_status", False),
        (429, False, None, True),
    ],
)
async def test_resolve_retryable_response_preserves_terminal_contract(
    status_code: int,
    has_next: bool,
    expected_outcome: str | None,
    passes_through: bool,
) -> None:
    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={
                "error": {"type": "provider_error"},
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    client = AsyncClient(transport=httpx.MockTransport(upstream))
    response = None
    try:
        transport_result = await execute_provider_attempt(
            client,
            _route(),
            forwarded_headers={"content-type": "application/json"},
            body=b"{}",
            model="gpt-test",
        )
        response = transport_result.response
        assert response is not None
        result = await resolve_retryable_response(
            response,
            _route(),
            has_next=has_next,
            started_at=0.0,
            price=None,
        )

        assert result.outcome == expected_outcome
        assert (result.response is response) is passes_through
        if not passes_through:
            assert response.is_closed
            assert result.usage.total_tokens == 5
            assert result.exhausted is (status_code >= 500 and not has_next)
    finally:
        if response is not None:
            await response.aclose()
        await client.aclose()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
