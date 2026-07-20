import asyncio
import os
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr
from redis.asyncio import Redis
from redis.exceptions import RedisError

from northgate.app import create_app
from northgate.config import Settings
from northgate.policy import PolicyRejectedError
from northgate.route_health import RouteHealthDecision

APPLICATION_KEY = "ng_test_application"
PROVIDER_KEY = "provider-secret"
PROXY_PATH = "/v1/gateways/default/openai/chat/completions"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "application_key_sha256": SecretStr(sha256(APPLICATION_KEY.encode()).hexdigest()),
        "provider_base_url": "https://provider.test/v1",
        "provider_api_key": SecretStr(PROVIDER_KEY),
    }
    values.update(overrides)
    return Settings(**values)


def _authorization() -> dict[str, str]:
    return {"Authorization": f"Bearer {APPLICATION_KEY}"}


@pytest.mark.anyio
async def test_policy_rejection_records_estimate_and_error_code() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.rejection: dict | None = None

        async def record_rejection(self, **kwargs: object) -> None:
            self.rejection = kwargs

    class RejectingPolicy:
        async def admit(self, **_: object):
            raise PolicyRejectedError(
                "TOKEN_LIMIT_EXCEEDED",
                "Token limit exceeded",
                {"Northgate-TokenLimit-Remaining": "1"},
            )

    called = False

    async def upstream(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    recorder = Recorder()
    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        _settings(token_limit_per_day=10),
        upstream_client=upstream_client,
    )
    app.state.usage_recorder = recorder
    app.state.policy_engine = RejectingPolicy()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            PROXY_PATH,
            json={"model": "gpt-test"},
            headers=_authorization(),
        )
    await upstream_client.aclose()

    assert response.status_code == 429
    assert called is False
    assert recorder.rejection is not None
    assert recorder.rejection["error_code"] == "TOKEN_LIMIT_EXCEEDED"
    assert recorder.rejection["estimated_tokens"] > 4096


@pytest.mark.anyio
async def test_non_streaming_response_is_forwarded_without_client_credential() -> None:
    request_body = b'{"model":"gpt-test","stream":false}'

    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://provider.test/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {PROVIDER_KEY}"
        assert APPLICATION_KEY not in str(request.headers)
        assert "northgate-metadata" not in request.headers
        assert await request.aread() == request_body
        return httpx.Response(
            200,
            content=b'{"id":"chatcmpl_test"}',
            headers={"Content-Type": "application/json", "X-Request-Id": "upstream-1"},
        )

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(_settings(), upstream_client=upstream_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            PROXY_PATH,
            content=request_body,
            headers={
                **_authorization(),
                "Content-Type": "application/json",
                "Northgate-Metadata": '{"tenant_id":"tenant-test"}',
            },
        )
    await upstream_client.aclose()

    assert response.status_code == 200
    assert response.content == b'{"id":"chatcmpl_test"}'
    assert response.headers["Northgate-Provider"] == "openai"
    assert response.headers["Northgate-Route"] == "configured-openai"
    assert response.headers["X-Request-Id"] == "upstream-1"


@pytest.mark.anyio
async def test_proxy_exports_provider_usage_metrics() -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        await request.aread()
        return httpx.Response(
            200,
            json={
                "id": "metrics-response",
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 3,
                    "total_tokens": 10,
                },
            },
        )

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(_settings(metrics_enabled=True), upstream_client=upstream_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            PROXY_PATH,
            json={"model": "gpt-test"},
            headers=_authorization(),
        )
        metrics = await client.get("/metrics")
    await upstream_client.aclose()

    assert response.status_code == 200
    assert (
        'northgate_provider_attempts_total{adapter="openai_compatible",'
        'outcome="succeeded",provider="openai"} 1.0' in metrics.text
    )
    assert (
        'northgate_provider_tokens_total{adapter="openai_compatible",'
        'provider="openai",type="prompt"} 7.0' in metrics.text
    )
    assert 'northgate_cache_requests_total{result="bypass"} 1.0' in metrics.text


@pytest.mark.anyio
async def test_proxy_exports_trace_events_and_propagates_context() -> None:
    exporter = InMemorySpanExporter()
    upstream_traceparent = ""

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_traceparent
        upstream_traceparent = request.headers["traceparent"]
        assert "baggage" not in request.headers
        await request.aread()
        return httpx.Response(
            200,
            json={
                "id": "traced-response",
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                },
            },
        )

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        _settings(tracing_enabled=True),
        upstream_client=upstream_client,
        span_exporter=exporter,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            PROXY_PATH,
            json={"model": "gpt-test"},
            headers={**_authorization(), "baggage": "tenant_id=must-not-propagate"},
        )
    await upstream_client.aclose()

    assert response.status_code == 200
    assert upstream_traceparent.startswith("00-")
    assert app.state.tracing.force_flush()
    span = exporter.get_finished_spans()[0]
    assert span.name == "POST /v1/gateways/{gateway_slug}/openai/chat/completions"
    assert [event.name for event in span.events] == [
        "northgate.cache",
        "northgate.provider_attempt",
    ]
    assert "model" not in span.attributes
    app.state.tracing.shutdown()


@pytest.mark.anyio
async def test_azure_adapter_builds_deployment_url_and_api_key_auth() -> None:
    request_body = b'{"model":"deployment/test","stream":false}'

    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path.split(b"?", 1)[0] == (
            b"/openai/deployments/deployment%2Ftest/chat/completions"
        )
        assert request.url.params["api-version"] == "test-version"
        assert request.headers["api-key"] == PROVIDER_KEY
        assert "authorization" not in request.headers
        assert await request.aread() == request_body
        return httpx.Response(200, json={"id": "azure-response"})

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        _settings(
            provider_base_url="https://resource.openai.azure.com",
            provider_adapter="azure_openai",
            provider_api_version="test-version",
        ),
        upstream_client=upstream_client,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            PROXY_PATH,
            content=request_body,
            headers={**_authorization(), "Content-Type": "application/json"},
        )
    await upstream_client.aclose()

    assert response.status_code == 200
    assert response.json() == {"id": "azure-response"}


@pytest.mark.anyio
async def test_azure_adapter_requires_model_before_provider_attempt() -> None:
    called = False

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        _settings(
            provider_base_url="https://resource.openai.azure.com",
            provider_adapter="azure_openai",
            provider_api_version="test-version",
        ),
        upstream_client=upstream_client,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(PROXY_PATH, json={}, headers=_authorization())
    await upstream_client.aclose()

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_PROVIDER_REQUEST"
    assert called is False


@pytest.mark.anyio
async def test_invalid_application_key_fails_before_upstream() -> None:
    called = False

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(_settings(), upstream_client=upstream_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(PROXY_PATH, json={}, headers={"Authorization": "Bearer wrong"})
    await upstream_client.aclose()

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_APPLICATION_KEY"
    assert response.json()["error"]["request_id"].startswith("req_")
    assert called is False


@pytest.mark.anyio
async def test_unpermitted_metadata_fails_before_upstream() -> None:
    called = False

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(_settings(), upstream_client=upstream_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            PROXY_PATH,
            json={},
            headers={
                **_authorization(),
                "Northgate-Metadata": '{"unauthorized_dimension":"value"}',
            },
        )
    await upstream_client.aclose()

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_METADATA"
    assert called is False


@pytest.mark.anyio
async def test_forbidden_gateway_fails_before_upstream() -> None:
    upstream_client = AsyncClient(transport=httpx.MockTransport(lambda request: None))
    app = create_app(_settings(), upstream_client=upstream_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/gateways/other/openai/chat/completions",
            json={},
            headers=_authorization(),
        )
    await upstream_client.aclose()

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN_GATEWAY"


@pytest.mark.anyio
async def test_provider_timeout_has_stable_error() -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider stalled", request=request)

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(_settings(), upstream_client=upstream_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(PROXY_PATH, json={}, headers=_authorization())
    await upstream_client.aclose()

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "PROVIDER_TIMEOUT"
    assert response.json()["error"]["retryable"] is True


@pytest.mark.anyio
async def test_retryable_status_falls_back_to_next_provider() -> None:
    calls: list[tuple[str, str]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, request.headers["authorization"]))
        await request.aread()
        if request.url.host == "provider.test":
            return httpx.Response(503, json={"error": "primary unavailable"})
        return httpx.Response(200, json={"id": "fallback-success"})

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        _settings(
            provider_retry_backoff_ms=0,
            fallback_provider_name="backup",
            fallback_provider_base_url="https://fallback.test/v1",
            fallback_provider_api_key=SecretStr("fallback-secret"),
        ),
        upstream_client=upstream_client,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            PROXY_PATH, json={"model": "gpt-test"}, headers=_authorization()
        )
    await upstream_client.aclose()

    assert response.status_code == 200
    assert response.json() == {"id": "fallback-success"}
    assert response.headers["Northgate-Provider"] == "backup"
    assert response.headers["Northgate-Attempts"] == "2"
    assert calls == [
        ("provider.test", f"Bearer {PROVIDER_KEY}"),
        ("fallback.test", "Bearer fallback-secret"),
    ]


@pytest.mark.anyio
async def test_route_retry_is_bounded_before_fallback() -> None:
    call_count = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        await request.aread()
        if call_count == 1:
            return httpx.Response(503, json={"error": "retry"})
        return httpx.Response(200, json={"id": "retry-success"})

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        _settings(provider_max_retries=1, provider_retry_backoff_ms=0),
        upstream_client=upstream_client,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            PROXY_PATH, json={"model": "gpt-test"}, headers=_authorization()
        )
    await upstream_client.aclose()

    assert response.status_code == 200
    assert response.headers["Northgate-Attempts"] == "2"
    assert call_count == 2


@pytest.mark.anyio
async def test_open_route_is_skipped_without_incrementing_provider_attempts() -> None:
    class RouteHealthStub:
        def __init__(self) -> None:
            self.failed_routes: set[str] = set()

        async def allow(self, *, route_key: str, **_: object) -> RouteHealthDecision:
            return RouteHealthDecision(allowed=route_key not in self.failed_routes)

        async def record_failure(self, *, route_key: str, **_: object) -> int:
            self.failed_routes.add(route_key)
            return 1

        async def record_success(self, **_: object) -> None:
            return None

    calls: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host or "")
        await request.aread()
        if request.url.host == "provider.test":
            raise httpx.ConnectError("primary unavailable", request=request)
        return httpx.Response(200, json={"id": "fallback-success"})

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        _settings(
            provider_retry_backoff_ms=0,
            fallback_provider_name="backup",
            fallback_provider_base_url="https://fallback.test/v1",
            fallback_provider_api_key=SecretStr("fallback-secret"),
            route_health_enabled=True,
            route_health_failure_threshold=1,
        ),
        upstream_client=upstream_client,
    )
    app.state.route_health_engine = RouteHealthStub()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(PROXY_PATH, json={}, headers=_authorization())
        second = await client.post(PROXY_PATH, json={}, headers=_authorization())
    await upstream_client.aclose()

    assert first.status_code == second.status_code == 200
    assert first.headers["Northgate-Attempts"] == "2"
    assert second.headers["Northgate-Attempts"] == "1"
    assert calls == ["provider.test", "fallback.test", "fallback.test"]


@pytest.mark.anyio
async def test_exact_cache_hit_avoids_a_second_provider_attempt() -> None:
    redis = Redis.from_url(
        os.environ.get(
            "NORTHGATE_TEST_CACHE_REDIS_URL",
            "redis://127.0.0.1:6379/14",
        )
    )
    try:
        await redis.ping()
    except RedisError:
        await redis.aclose()
        pytest.skip("Redis is not available")

    await redis.flushdb()
    call_count = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        await request.aread()
        return httpx.Response(200, json={"id": "cached-response"})

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        _settings(exact_cache_ttl_seconds=60),
        upstream_client=upstream_client,
        redis=redis,
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post(
                PROXY_PATH, json={"model": "gpt-test"}, headers=_authorization()
            )
            second = await client.post(
                PROXY_PATH, json={"model": "gpt-test"}, headers=_authorization()
            )
            different_accept = await client.post(
                PROXY_PATH,
                json={"model": "gpt-test"},
                headers={**_authorization(), "Accept": "text/event-stream"},
            )

        assert first.status_code == second.status_code == 200
        assert first.headers["Northgate-Cache"] == "MISS"
        assert second.headers["Northgate-Cache"] == "HIT"
        assert second.headers["Northgate-Attempts"] == "0"
        assert second.json() == {"id": "cached-response"}
        assert different_accept.headers["Northgate-Cache"] == "MISS"
        assert call_count == 2
    finally:
        await upstream_client.aclose()
        await redis.flushdb()
        await redis.aclose()


@pytest.mark.anyio
async def test_oversized_response_is_not_cached() -> None:
    redis = Redis.from_url(
        os.environ.get(
            "NORTHGATE_TEST_CACHE_REDIS_URL",
            "redis://127.0.0.1:6379/14",
        )
    )
    try:
        await redis.ping()
    except RedisError:
        await redis.aclose()
        pytest.skip("Redis is not available")

    await redis.flushdb()
    call_count = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        await request.aread()
        return httpx.Response(200, content=b"x" * 1025)

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        _settings(exact_cache_ttl_seconds=60, cache_max_entry_bytes=1024),
        upstream_client=upstream_client,
        redis=redis,
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post(PROXY_PATH, json={}, headers=_authorization())
            second = await client.post(PROXY_PATH, json={}, headers=_authorization())

        assert first.headers["Northgate-Cache"] == "MISS"
        assert second.headers["Northgate-Cache"] == "MISS"
        assert call_count == 2
    finally:
        await upstream_client.aclose()
        await redis.flushdb()
        await redis.aclose()


class GatedStream(httpx.AsyncByteStream):
    def __init__(self, release_second_chunk: asyncio.Event) -> None:
        self.release_second_chunk = release_second_chunk

    async def __aiter__(self):
        yield b"data: first\n\n"
        await self.release_second_chunk.wait()
        yield b"data: [DONE]\n\n"


class DisconnectStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def __aiter__(self):
        yield b"data: first\n\n"
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed.set()


class TerminalThenHangStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield (
            b'data: {"usage":{"prompt_tokens":9,"completion_tokens":1,'
            b'"total_tokens":10,"prompt_tokens_details":{"cached_tokens":8}}}'
            b"\r\n\r\n"
        )
        yield b"data: [DONE]\r\n\r\n"
        await asyncio.Event().wait()


@pytest.mark.anyio
async def test_streaming_sends_first_chunk_before_upstream_finishes() -> None:
    release_second_chunk = asyncio.Event()
    first_downstream_chunk = asyncio.Event()

    async def upstream(request: httpx.Request) -> httpx.Response:
        await request.aread()
        return httpx.Response(
            200,
            stream=GatedStream(release_second_chunk),
            headers={"Content-Type": "text/event-stream"},
        )

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(_settings(), upstream_client=upstream_client)
    messages: list[dict] = []
    request_received = False
    never_disconnect = asyncio.Event()

    async def receive() -> dict:
        nonlocal request_received
        if not request_received:
            request_received = True
            return {"type": "http.request", "body": b'{"stream":true}', "more_body": False}
        await never_disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)
        if message["type"] == "http.response.body" and message.get("body") == b"data: first\n\n":
            first_downstream_chunk.set()
            release_second_chunk.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": PROXY_PATH,
        "raw_path": PROXY_PATH.encode(),
        "query_string": b"",
        "headers": [
            (b"host", b"test"),
            (b"authorization", f"Bearer {APPLICATION_KEY}".encode()),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }

    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(first_downstream_chunk.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=1)
    await upstream_client.aclose()

    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    assert body == b"data: first\n\ndata: [DONE]\n\n"


@pytest.mark.anyio
async def test_client_disconnect_closes_upstream_stream() -> None:
    stream = DisconnectStream()
    response_started = asyncio.Event()
    disconnect = asyncio.Event()

    async def upstream(request: httpx.Request) -> httpx.Response:
        await request.aread()
        return httpx.Response(
            200,
            stream=stream,
            headers={"Content-Type": "text/event-stream"},
        )

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(_settings(), upstream_client=upstream_client)
    request_received = False

    async def receive() -> dict:
        nonlocal request_received
        if not request_received:
            request_received = True
            return {"type": "http.request", "body": b'{"stream":true}', "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            response_started.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": PROXY_PATH,
        "raw_path": PROXY_PATH.encode(),
        "query_string": b"",
        "headers": [
            (b"host", b"test"),
            (b"authorization", f"Bearer {APPLICATION_KEY}".encode()),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }

    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(response_started.wait(), timeout=1)
    disconnect.set()
    await asyncio.wait_for(task, timeout=1)
    await asyncio.wait_for(stream.closed.wait(), timeout=1)
    await upstream_client.aclose()


@pytest.mark.anyio
async def test_disconnect_after_terminal_event_settles_actual_usage() -> None:
    disconnect = asyncio.Event()

    class Recorder:
        def __init__(self) -> None:
            self.request: dict | None = None
            self.attempt: dict | None = None

        async def start(self, **_: object) -> None:
            return None

        async def start_attempt(self, **_: object):
            return uuid4()

        async def settle_attempt(self, **kwargs: object) -> None:
            self.attempt = kwargs

        async def settle(self, **kwargs: object) -> None:
            self.request = kwargs

    class Policy:
        def __init__(self) -> None:
            self.actual_tokens: int | None = None

        async def admit(self, **_: object):
            return SimpleNamespace(headers={})

        async def settle(self, _lease, actual_tokens, _actual_cost) -> None:
            self.actual_tokens = actual_tokens

    async def upstream(request: httpx.Request) -> httpx.Response:
        await request.aread()
        return httpx.Response(
            200,
            stream=TerminalThenHangStream(),
            headers={"Content-Type": "text/event-stream"},
        )

    recorder = Recorder()
    policy = Policy()
    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        _settings(token_limit_per_day=60_000),
        upstream_client=upstream_client,
    )
    app.state.usage_recorder = recorder
    app.state.policy_engine = policy
    request_received = False

    async def receive() -> dict:
        nonlocal request_received
        if not request_received:
            request_received = True
            return {
                "type": "http.request",
                "body": b'{"model":"gpt-test","stream":true}',
                "more_body": False,
            }
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body" and b"[DONE]" in message.get("body", b""):
            disconnect.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": PROXY_PATH,
        "raw_path": PROXY_PATH.encode(),
        "query_string": b"",
        "headers": [
            (b"host", b"test"),
            (b"authorization", f"Bearer {APPLICATION_KEY}".encode()),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }

    await asyncio.wait_for(app(scope, receive, send), timeout=1)
    await upstream_client.aclose()

    assert recorder.attempt is not None
    assert recorder.attempt["outcome"] == "succeeded"
    assert recorder.attempt["usage"].cached_prompt_tokens == 8
    assert recorder.request is not None
    assert recorder.request["outcome"] == "succeeded"
    assert policy.actual_tokens == 10


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
