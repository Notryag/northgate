from hashlib import sha256

import pytest
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from northgate.app import create_app
from northgate.config import Settings


@pytest.mark.anyio
async def test_health_endpoints_include_request_id() -> None:
    app = create_app(Settings(environment="test"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert live.headers["Northgate-Request-Id"].startswith("req_")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


@pytest.mark.anyio
async def test_metrics_are_disabled_by_default() -> None:
    app = create_app(Settings(environment="test"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/metrics")

    assert response.status_code == 404


@pytest.mark.anyio
async def test_metrics_require_configured_key_and_use_route_templates() -> None:
    metrics_key = "metrics-test-secret"
    app = create_app(
        Settings(
            environment="test",
            metrics_enabled=True,
            metrics_key_sha256=SecretStr(sha256(metrics_key.encode()).hexdigest()),
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.get("/health/live")
        await client.get("/arbitrary/high-cardinality-value")
        unauthorized = await client.get("/metrics")
        authorized = await client.get(
            "/metrics", headers={"Authorization": f"Bearer {metrics_key}"}
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.headers["content-type"].startswith("text/plain")
    assert 'northgate_build_info{version="0.1.0"} 1.0' in authorized.text
    assert (
        'northgate_http_requests_total{method="GET",route="/health/live",status_code="200"}'
        in authorized.text
    )
    assert (
        'northgate_http_requests_total{method="GET",route="unmatched",status_code="404"}'
        in authorized.text
    )
    assert "arbitrary/high-cardinality-value" not in authorized.text


@pytest.mark.anyio
async def test_tracing_preserves_parent_context_and_uses_route_template() -> None:
    exporter = InMemorySpanExporter()
    app = create_app(
        Settings(environment="test", tracing_enabled=True),
        span_exporter=exporter,
    )
    traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live", headers={"traceparent": traceparent})

    assert response.status_code == 200
    assert app.state.tracing.force_flush()
    span = exporter.get_finished_spans()[0]
    assert span.name == "GET /health/live"
    assert span.parent is not None
    assert span.parent.span_id == int("b7ad6b7169203331", 16)
    assert span.attributes["http.route"] == "/health/live"
    assert span.attributes["http.response.status_code"] == 200
    assert span.resource.attributes["service.name"] == "northgate"
    app.state.tracing.shutdown()


def test_tracing_requires_otlp_endpoint_without_injected_exporter() -> None:
    with pytest.raises(ValueError, match="NORTHGATE_OTLP_TRACES_ENDPOINT"):
        create_app(Settings(environment="test", tracing_enabled=True))


@pytest.mark.anyio
async def test_valid_request_id_is_preserved() -> None:
    request_id = "req_test-request-123"
    app = create_app(Settings(environment="test"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live", headers={"Northgate-Request-Id": request_id})

    assert response.headers["Northgate-Request-Id"] == request_id


@pytest.mark.anyio
async def test_invalid_request_id_is_replaced() -> None:
    app = create_app(Settings(environment="test"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live", headers={"Northgate-Request-Id": "invalid"})

    assert response.headers["Northgate-Request-Id"].startswith("req_")
    assert response.headers["Northgate-Request-Id"] != "invalid"


@pytest.mark.anyio
async def test_application_key_cannot_access_operator_analytics() -> None:
    operator_key = "operator-test"
    app = create_app(
        Settings(
            environment="test",
            operator_key_sha256=SecretStr(sha256(operator_key.encode()).hexdigest()),
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get(
            "/api/v1/usage/summary", headers={"Authorization": "Bearer application-key"}
        )
        operator = await client.get(
            "/api/v1/usage/summary", headers={"Authorization": f"Bearer {operator_key}"}
        )

    assert unauthorized.status_code == 401
    assert operator.status_code == 503


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
