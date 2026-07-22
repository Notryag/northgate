import httpx
import pytest
from httpx import AsyncClient

from northgate.attempt_execution import execute_provider_attempt
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


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
