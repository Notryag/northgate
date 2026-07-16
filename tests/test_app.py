import pytest
from httpx import ASGITransport, AsyncClient

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


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
