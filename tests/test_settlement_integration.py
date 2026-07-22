import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, text, update
from sqlalchemy.exc import SQLAlchemyError

from northgate.db.database import Database
from northgate.db.models import ProviderAttemptRecord, RequestRecord, SettlementEvent
from northgate.metrics import Metrics
from northgate.policy import PolicyEngine, PolicyUnavailableError
from northgate.routing import PolicyLimits
from northgate.settlement import SettlementCoordinator


@pytest.mark.anyio
async def test_settlement_outbox_recovers_partial_policy_failure() -> None:
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
    request_id = f"req_outbox_{uuid4().hex}"
    attempt_id = uuid4()
    retry_attempt_id = uuid4()
    gateway_key = f"outbox-{uuid4().hex[:12]}"
    event_id = None
    record_created = False
    try:
        if not await database.ping():
            pytest.skip("PostgreSQL is not available")
        await redis.ping()
        async with database.sessions() as session:
            await session.execute(text("SELECT 1 FROM settlement_events LIMIT 1"))
    except (RedisError, SQLAlchemyError, OSError):
        await database.close()
        await redis.aclose()
        pytest.skip("Northgate integration stores are not available or are not migrated")

    policy_engine = PolicyEngine(redis, lease_seconds=30)
    lease = await policy_engine.admit(
        gateway_key=gateway_key,
        request_id=request_id,
        limits=PolicyLimits(concurrent_requests=1, tokens_per_day=100),
        estimated_tokens=10,
    )
    await policy_engine.stop_renewal(lease)
    try:
        async with database.sessions() as session:
            request_record = RequestRecord(
                request_id=request_id,
                provider="primary",
                model="gpt-outbox",
                estimated_tokens=10,
                cache_status="bypass",
                outcome="started",
            )
            session.add(request_record)
            await session.flush()
            session.add(
                ProviderAttemptRecord(
                    id=attempt_id,
                    request_id=request_id,
                    attempt_index=1,
                    provider="primary",
                    outcome="started",
                )
            )
            session.add(
                ProviderAttemptRecord(
                    id=retry_attempt_id,
                    request_id=request_id,
                    attempt_index=2,
                    provider="fallback",
                    outcome="started",
                )
            )
            await session.commit()
            record_created = True

        payload: dict[str, object] = {
            "request": {
                "outcome": "succeeded",
                "status_code": 200,
                "provider_request_id": "provider-outbox",
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
                "cached_prompt_tokens": 1,
                "cost_microusd": None,
                "route_id": None,
                "provider": "primary",
                "price_id": None,
                "latency_ms": 20,
                "first_token_ms": 5,
            },
            "attempt": {
                "id": str(attempt_id),
                "outcome": "succeeded",
                "status_code": 200,
                "provider_request_id": "provider-outbox",
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
                "cached_prompt_tokens": 1,
                "cost_microusd": None,
                "latency_ms": 18,
            },
            "policy": {
                "gateway_key": gateway_key,
                "token_day": lease.token_day,
                "spend_day": lease.spend_day,
                "spend_month": lease.spend_month,
                "actual_tokens": 5,
                "actual_cost_microusd": None,
            },
        }

        class FailingPolicyEngine(PolicyEngine):
            async def settle_reservation(self, **_: object) -> None:
                raise PolicyUnavailableError

        coordinator = SettlementCoordinator(
            database,
            FailingPolicyEngine(redis, lease_seconds=30),
        )
        retry_payload: dict[str, object] = {
            "request": None,
            "attempt": {
                "id": str(retry_attempt_id),
                "outcome": "connection_error",
                "status_code": None,
                "provider_request_id": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "cached_prompt_tokens": None,
                "cost_microusd": None,
                "latency_ms": 8,
            },
            "policy": None,
        }
        retry_event_id = await coordinator.enqueue(
            request_id=request_id,
            event_key=f"attempt:{retry_attempt_id}",
            payload=retry_payload,
        )
        event_id = await coordinator.enqueue(request_id=request_id, payload=payload)
        assert retry_event_id != event_id
        assert await coordinator.process(retry_event_id) is True
        assert await coordinator.enqueue(request_id=request_id, payload=payload) == event_id
        assert await coordinator.process(event_id) is False

        async with database.sessions() as session:
            event = await session.get(SettlementEvent, event_id)
            request_record = await session.get(RequestRecord, request_id)
            retry_attempt = await session.get(ProviderAttemptRecord, retry_attempt_id)
        assert event is not None
        assert event.status == "retry"
        assert event.database_settled_at is not None
        assert event.policy_settled_at is None
        assert request_record is not None and request_record.outcome == "succeeded"
        assert retry_attempt is not None and retry_attempt.outcome == "connection_error"
        assert await redis.zcard(f"northgate:policy:{{{gateway_key}}}:concurrency") == 1
        metrics = Metrics("integration")
        await metrics.refresh_operational_state(database, redis)
        samples = {
            sample.name: sample.value
            for metric in metrics.registry.collect()
            for sample in metric.samples
        }
        assert samples["northgate_settlement_outbox_pending_events"] >= 1
        assert samples["northgate_settlement_outbox_failed_events"] == 0

        async with database.sessions() as session:
            await session.execute(
                update(SettlementEvent)
                .where(SettlementEvent.id == event_id)
                .values(available_at=datetime.now(UTC))
            )
            await session.commit()
        coordinator.policy_engine = policy_engine
        assert await coordinator.process(event_id) is True

        async with database.sessions() as session:
            event = await session.get(SettlementEvent, event_id)
            attempt = await session.get(ProviderAttemptRecord, attempt_id)
        assert event is not None and event.status == "completed"
        assert event.database_settled_at is not None
        assert event.policy_settled_at is not None
        assert attempt is not None and attempt.outcome == "succeeded"
        assert await redis.zcard(f"northgate:policy:{{{gateway_key}}}:concurrency") == 0
        used = await redis.hget(
            f"northgate:policy:{{{gateway_key}}}:tokens:{lease.token_day}", "used"
        )
        assert used == b"5"
        assert await coordinator.process_one() is False
        await metrics.refresh_operational_state(database, redis)
        samples = {
            sample.name: sample.value
            for metric in metrics.registry.collect()
            for sample in metric.samples
        }
        assert samples["northgate_settlement_outbox_pending_events"] == 0
    finally:
        if event_id is not None:
            async with database.sessions() as session:
                await session.execute(
                    delete(SettlementEvent).where(SettlementEvent.request_id == request_id)
                )
                if record_created:
                    await session.execute(
                        delete(ProviderAttemptRecord).where(
                            ProviderAttemptRecord.request_id == request_id
                        )
                    )
                    await session.execute(
                        delete(RequestRecord).where(RequestRecord.request_id == request_id)
                    )
                await session.commit()
        async for key in redis.scan_iter(match=f"northgate:policy:{{{gateway_key}}}:*"):
            await redis.delete(key)
        await database.close()
        await redis.aclose()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
