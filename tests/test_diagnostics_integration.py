import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, text
from sqlalchemy.exc import SQLAlchemyError

from northgate.app import create_app
from northgate.config import Settings
from northgate.db.database import Database
from northgate.db.models import ProviderAttemptRecord, RequestRecord, SettlementEvent

pytestmark = pytest.mark.integration


def _integration_store_unavailable(reason: str) -> None:
    if os.environ.get("NORTHGATE_REQUIRE_INTEGRATION_STORES") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.mark.anyio
async def test_operator_can_inspect_correlated_healthy_and_incomplete_requests() -> None:
    database_url = os.environ.get(
        "NORTHGATE_TEST_DATABASE_URL",
        "postgresql+asyncpg://northgate:northgate@127.0.0.1:5433/northgate",
    )
    redis_url = os.environ.get(
        "NORTHGATE_TEST_REDIS_URL",
        "redis://127.0.0.1:6380/15",
    )
    database = Database(database_url)
    redis = Redis.from_url(redis_url)
    try:
        if not await database.ping():
            _integration_store_unavailable("PostgreSQL is not available")
        await redis.ping()
        async with database.sessions() as session:
            await session.execute(text("SELECT 1 FROM settlement_events LIMIT 1"))
    except (RedisError, SQLAlchemyError, OSError):
        await database.close()
        await redis.aclose()
        _integration_store_unavailable(
            "Northgate integration stores are not available or are not migrated"
        )

    operator_key = f"operator-{uuid4().hex}"
    run_id = f"run-{uuid4().hex}"
    healthy_id = f"req_diagnostic_{uuid4().hex}"
    incomplete_id = f"req_diagnostic_{uuid4().hex}"
    protected_id = f"req_diagnostic_{uuid4().hex}"
    attempt_only_id = f"req_diagnostic_{uuid4().hex}"
    request_ids = [healthy_id, incomplete_id, protected_id, attempt_only_id]
    now = datetime.now(UTC)
    policy_key = f"northgate:policy:{{diagnostic-{uuid4().hex}}}:concurrency"
    upstream_client = AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    app = create_app(
        Settings(
            environment="test",
            operator_key_sha256=SecretStr(sha256(operator_key.encode()).hexdigest()),
            routing_source="configuration",
            usage_persistence_enabled=True,
            settlement_outbox_enabled=True,
            route_health_enabled=False,
            database_url=SecretStr(database_url),
            redis_url=SecretStr(redis_url),
        ),
        upstream_client=upstream_client,
        database=database,
        redis=redis,
    )

    try:
        async with database.sessions() as session:
            session.add_all(
                [
                    RequestRecord(
                        request_id=healthy_id,
                        provider="openai",
                        model="gpt-test",
                        request_metadata={"run_id": run_id},
                        request_metadata_trust={"run_id": "untrusted"},
                        cost_microusd=17,
                        outcome="succeeded",
                        http_status=200,
                        prompt_tokens=2449,
                        completion_tokens=14,
                        total_tokens=2463,
                        cached_prompt_tokens=0,
                        estimated_tokens=3000,
                        cache_status="bypass",
                        latency_ms=7400,
                        first_token_ms=200,
                        started_at=now - timedelta(seconds=2),
                        completed_at=now - timedelta(seconds=1),
                    ),
                    RequestRecord(
                        request_id=incomplete_id,
                        provider="openai",
                        model="gpt-test",
                        request_metadata={"run_id": run_id},
                        request_metadata_trust=None,
                        outcome="started",
                        http_status=200,
                        estimated_tokens=3000,
                        cache_status="bypass",
                        started_at=now - timedelta(hours=1),
                    ),
                    RequestRecord(
                        request_id=protected_id,
                        provider="openai",
                        model="gpt-test",
                        request_metadata={"run_id": run_id},
                        request_metadata_trust={"run_id": "untrusted"},
                        outcome="started",
                        http_status=200,
                        estimated_tokens=3000,
                        cache_status="bypass",
                        started_at=now - timedelta(minutes=59),
                    ),
                    RequestRecord(
                        request_id=attempt_only_id,
                        provider="openai",
                        model="gpt-test",
                        request_metadata={"run_id": run_id},
                        request_metadata_trust={"run_id": "untrusted"},
                        outcome="succeeded",
                        http_status=200,
                        prompt_tokens=8,
                        completion_tokens=2,
                        total_tokens=10,
                        cached_prompt_tokens=0,
                        estimated_tokens=20,
                        cache_status="bypass",
                        started_at=now - timedelta(minutes=58),
                        completed_at=now - timedelta(minutes=57),
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    ProviderAttemptRecord(
                        request_id=healthy_id,
                        attempt_index=1,
                        provider="openai",
                        outcome="succeeded",
                        http_status=200,
                        prompt_tokens=2449,
                        completion_tokens=14,
                        total_tokens=2463,
                        cached_prompt_tokens=0,
                        cost_microusd=17,
                        latency_ms=7400,
                        started_at=now - timedelta(seconds=2),
                        completed_at=now - timedelta(seconds=1),
                    ),
                    ProviderAttemptRecord(
                        request_id=incomplete_id,
                        attempt_index=1,
                        provider="openai",
                        outcome="started",
                        http_status=200,
                        started_at=now - timedelta(hours=1),
                    ),
                    ProviderAttemptRecord(
                        request_id=protected_id,
                        attempt_index=1,
                        provider="openai",
                        outcome="started",
                        http_status=200,
                        started_at=now - timedelta(minutes=59),
                    ),
                    ProviderAttemptRecord(
                        request_id=attempt_only_id,
                        attempt_index=1,
                        provider="openai",
                        outcome="started",
                        http_status=200,
                        started_at=now - timedelta(minutes=58),
                    ),
                    SettlementEvent(
                        request_id=healthy_id,
                        event_key="terminal",
                        payload={"schema_version": 1, "request": None},
                        status="completed",
                        attempts=1,
                        database_settled_at=now,
                        policy_settled_at=now,
                        created_at=now - timedelta(seconds=1),
                        completed_at=now,
                    ),
                    SettlementEvent(
                        request_id=protected_id,
                        event_key="terminal",
                        payload={"schema_version": 1, "request": None},
                        status="retry",
                        attempts=1,
                        created_at=now - timedelta(minutes=58),
                    ),
                    SettlementEvent(
                        request_id=attempt_only_id,
                        event_key="terminal",
                        payload={"schema_version": 1, "request": None},
                        status="completed",
                        attempts=1,
                        database_settled_at=now - timedelta(minutes=57),
                        policy_settled_at=now - timedelta(minutes=57),
                        created_at=now - timedelta(minutes=57),
                        completed_at=now - timedelta(minutes=57),
                    ),
                ]
            )
            await session.commit()

        now_ms = int(now.timestamp() * 1000)
        await redis.zadd(
            policy_key,
            {
                incomplete_id: now_ms + 300_000,
                protected_id: now_ms - 1_000,
            },
        )
        await redis.hset(
            f"{policy_key}:started",
            mapping={
                incomplete_id: now_ms - 3_600_000,
                protected_id: now_ms - 3_540_000,
            },
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            unauthorized = await client.get(
                "/api/v1/diagnostics/correlated",
                params={"metadata_key": "run_id", "metadata_value": run_id},
            )
            response = await client.get(
                "/api/v1/diagnostics/correlated",
                params={"metadata_key": "run_id", "metadata_value": run_id},
                headers={"Authorization": f"Bearer {operator_key}"},
            )
            limited_response = await client.get(
                "/api/v1/diagnostics/correlated",
                params={"metadata_key": "run_id", "metadata_value": run_id, "limit": 1},
                headers={"Authorization": f"Bearer {operator_key}"},
            )
            invalid_filter_response = await client.get(
                "/api/v1/diagnostics/correlated",
                params={"metadata_key": "invalid key", "metadata_value": run_id},
                headers={"Authorization": f"Bearer {operator_key}"},
            )
            request_response = await client.get(
                f"/api/v1/diagnostics/requests/{healthy_id}",
                headers={"Authorization": f"Bearer {operator_key}"},
            )
            missing_response = await client.get(
                "/api/v1/diagnostics/requests/req_00000000",
                headers={"Authorization": f"Bearer {operator_key}"},
            )
            invalid_response = await client.get(
                "/api/v1/diagnostics/requests/not-valid",
                headers={"Authorization": f"Bearer {operator_key}"},
            )
            stale_response = await client.get(
                "/api/v1/diagnostics/stale",
                params={"minimum_age_seconds": 30, "limit": 10},
                headers={"Authorization": f"Bearer {operator_key}"},
            )

        assert unauthorized.status_code == 401
        assert response.status_code == 200
        payload = response.json()
        assert payload["schema_version"] == 1
        assert payload["aggregate"]["requests"] == 4
        assert payload["aggregate"]["total_tokens"] == 2473
        assert payload["aggregate"]["usage_missing_requests"] == 2
        assert payload["finding_counts"]["REQUEST_STILL_STARTED"] == 2
        assert payload["finding_counts"]["ATTEMPT_STILL_STARTED"] == 3
        assert payload["finding_counts"]["TERMINAL_HTTP_WITHOUT_SETTLEMENT"] == 1
        assert payload["finding_counts"]["USAGE_MISSING"] == 2
        assert limited_response.status_code == 200
        assert limited_response.json()["aggregate"]["requests"] == 1
        assert limited_response.json()["has_more"] is True
        assert invalid_filter_response.status_code == 400
        assert request_response.status_code == 200
        assert request_response.json()["settlement"]["events"][0]["status"] == "completed"
        assert "request" not in request_response.json()["settlement"]["events"][0]
        assert missing_response.status_code == 404
        assert invalid_response.status_code == 400
        assert stale_response.status_code == 200
        stale_payload = stale_response.json()
        assert len(stale_payload["requests"]) == 3
        assert stale_payload["policy_state_available"] is True
        assert stale_payload["finding_counts"]["UNPROTECTED_STALE_SETTLEMENT"] == 2
        assert stale_payload["finding_counts"]["RECOVERABLE_SETTLEMENT_PENDING"] == 1
        assert stale_payload["finding_counts"]["STALE_CONCURRENCY_LEASE"] == 2
        stale_by_id = {
            item["request"]["request_id"]: item["stale"] for item in stale_payload["requests"]
        }
        assert stale_by_id[incomplete_id]["recoverable_settlement"] is False
        assert stale_by_id[incomplete_id]["concurrency_leases"][0]["expired"] is False
        assert stale_by_id[protected_id]["recoverable_settlement"] is True
        assert stale_by_id[protected_id]["concurrency_leases"][0]["expired"] is True
        assert stale_by_id[attempt_only_id]["request_started"] is False
        assert stale_by_id[attempt_only_id]["stale_attempt_indexes"] == [1]
    finally:
        async with database.sessions() as session:
            await session.execute(
                delete(SettlementEvent).where(SettlementEvent.request_id.in_(request_ids))
            )
            await session.execute(
                delete(ProviderAttemptRecord).where(
                    ProviderAttemptRecord.request_id.in_(request_ids)
                )
            )
            await session.execute(
                delete(RequestRecord).where(RequestRecord.request_id.in_(request_ids))
            )
            await session.commit()
        await redis.delete(policy_key, f"{policy_key}:started")
        await upstream_client.aclose()
        await database.close()
        await redis.aclose()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
