import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import structlog
from redis.asyncio import Redis
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from northgate.config import get_settings
from northgate.db.database import Database
from northgate.db.models import ProviderAttemptRecord, RequestRecord, SettlementEvent
from northgate.policy import PolicyEngine

logger = structlog.get_logger()
WORKER_HEARTBEAT_PATTERN = "northgate:settlement:worker:heartbeat:*"


class SettlementPayloadError(Exception):
    pass


class SettlementCoordinator:
    def __init__(
        self,
        database: Database,
        policy_engine: PolicyEngine | None,
        *,
        max_attempts: int = 20,
        lock_timeout_seconds: int = 60,
    ) -> None:
        self.database = database
        self.policy_engine = policy_engine
        self.max_attempts = max_attempts
        self.lock_timeout_seconds = lock_timeout_seconds

    async def enqueue(
        self,
        *,
        request_id: str,
        payload: dict[str, object],
        event_key: str = "terminal",
    ) -> UUID:
        policy_settled_at = datetime.now(UTC) if payload.get("policy") is None else None
        async with self.database.sessions() as session:
            existing = await session.scalar(
                select(SettlementEvent).where(
                    SettlementEvent.request_id == request_id,
                    SettlementEvent.event_key == event_key,
                )
            )
            if existing is not None:
                return existing.id
            event = SettlementEvent(
                request_id=request_id,
                event_key=event_key,
                payload=payload,
                status="pending",
                attempts=0,
                policy_settled_at=policy_settled_at,
            )
            session.add(event)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(SettlementEvent).where(
                        SettlementEvent.request_id == request_id,
                        SettlementEvent.event_key == event_key,
                    )
                )
                if existing is None:
                    raise
                return existing.id
            return event.id

    async def process_one(self) -> bool:
        return await self.process()

    async def process(self, event_id: UUID | None = None) -> bool:
        """Process one event and report whether every settlement stage completed."""

        event = await self._claim(event_id)
        if event is None:
            return False
        try:
            if event.database_settled_at is None:
                await self._apply_database(event)
            if event.policy_settled_at is None:
                await self._apply_policy(event)
            await self._complete(event.id)
        except Exception as exc:
            await self._retry(event.id, event.attempts, exc)
            await logger.aexception(
                "settlement_event_failed",
                settlement_event_id=str(event.id),
                request_id=event.request_id,
            )
            return False
        return True

    async def _claim(self, event_id: UUID | None = None) -> SettlementEvent | None:
        now = datetime.now(UTC)
        stale_lock = now - timedelta(seconds=self.lock_timeout_seconds)
        async with self.database.sessions() as session:
            async with session.begin():
                statement = (
                    select(SettlementEvent)
                    .where(
                        or_(
                            SettlementEvent.status.in_(("pending", "retry")),
                            (
                                (SettlementEvent.status == "processing")
                                & (SettlementEvent.locked_at < stale_lock)
                            ),
                        ),
                        SettlementEvent.available_at <= now,
                    )
                    .order_by(SettlementEvent.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if event_id is not None:
                    statement = statement.where(SettlementEvent.id == event_id)
                event = await session.scalar(statement)
                if event is None:
                    return None
                event.status = "processing"
                event.locked_at = now
                event.attempts += 1
            return event

    async def _apply_database(self, event: SettlementEvent) -> None:
        request = event.payload.get("request")
        attempt = event.payload.get("attempt")
        now = datetime.now(UTC)
        async with self.database.sessions() as session:
            async with session.begin():
                if attempt is not None:
                    if not isinstance(attempt, dict):
                        raise SettlementPayloadError("attempt must be an object or null")
                    attempt_id = UUID(_string(attempt, "id"))
                    await session.execute(
                        update(ProviderAttemptRecord)
                        .where(
                            ProviderAttemptRecord.id == attempt_id,
                            ProviderAttemptRecord.outcome == "started",
                        )
                        .values(
                            outcome=_string(attempt, "outcome"),
                            http_status=_optional_int(attempt, "status_code"),
                            provider_request_id=_optional_string(attempt, "provider_request_id"),
                            prompt_tokens=_optional_int(attempt, "prompt_tokens"),
                            completion_tokens=_optional_int(attempt, "completion_tokens"),
                            total_tokens=_optional_int(attempt, "total_tokens"),
                            cached_prompt_tokens=_optional_int(attempt, "cached_prompt_tokens"),
                            cost_microusd=_optional_int(attempt, "cost_microusd"),
                            latency_ms=_optional_int(attempt, "latency_ms"),
                            completed_at=now,
                        )
                    )
                if request is not None:
                    if not isinstance(request, dict):
                        raise SettlementPayloadError("request must be an object or null")
                    await session.execute(
                        update(RequestRecord)
                        .where(
                            RequestRecord.request_id == event.request_id,
                            RequestRecord.outcome == "started",
                        )
                        .values(
                            outcome=_string(request, "outcome"),
                            http_status=_optional_int(request, "status_code"),
                            provider_request_id=_optional_string(request, "provider_request_id"),
                            prompt_tokens=_optional_int(request, "prompt_tokens"),
                            completion_tokens=_optional_int(request, "completion_tokens"),
                            total_tokens=_optional_int(request, "total_tokens"),
                            cached_prompt_tokens=_optional_int(request, "cached_prompt_tokens"),
                            cost_microusd=_optional_int(request, "cost_microusd"),
                            route_id=_optional_uuid(request, "route_id"),
                            provider=_string(request, "provider"),
                            price_id=_optional_uuid(request, "price_id"),
                            latency_ms=_optional_int(request, "latency_ms"),
                            first_token_ms=_optional_int(request, "first_token_ms"),
                            completed_at=now,
                        )
                    )
                await session.execute(
                    update(SettlementEvent)
                    .where(SettlementEvent.id == event.id)
                    .values(database_settled_at=now)
                )
        event.database_settled_at = now

    async def _apply_policy(self, event: SettlementEvent) -> None:
        policy = _mapping(event.payload, "policy")
        if self.policy_engine is None:
            raise RuntimeError("policy engine is unavailable")
        await self.policy_engine.settle_reservation(
            gateway_key=_string(policy, "gateway_key"),
            request_id=event.request_id,
            token_day=_string(policy, "token_day"),
            spend_day=_string(policy, "spend_day"),
            spend_month=_string(policy, "spend_month"),
            actual_tokens=_optional_int(policy, "actual_tokens"),
            actual_cost_microusd=_optional_int(policy, "actual_cost_microusd"),
        )
        now = datetime.now(UTC)
        async with self.database.sessions() as session:
            await session.execute(
                update(SettlementEvent)
                .where(SettlementEvent.id == event.id)
                .values(policy_settled_at=now)
            )
            await session.commit()
        event.policy_settled_at = now

    async def _complete(self, event_id: UUID) -> None:
        now = datetime.now(UTC)
        async with self.database.sessions() as session:
            await session.execute(
                update(SettlementEvent)
                .where(SettlementEvent.id == event_id)
                .values(
                    status="completed",
                    last_error=None,
                    locked_at=None,
                    completed_at=now,
                )
            )
            await session.commit()

    async def _retry(self, event_id: UUID, attempts: int, exc: Exception) -> None:
        failed = attempts >= self.max_attempts
        delay_seconds = min(60, 2 ** min(attempts, 6))
        message = f"{type(exc).__name__}: {exc}"[:500]
        async with self.database.sessions() as session:
            await session.execute(
                update(SettlementEvent)
                .where(SettlementEvent.id == event_id)
                .values(
                    status="failed" if failed else "retry",
                    last_error=message,
                    locked_at=None,
                    available_at=datetime.now(UTC) + timedelta(seconds=delay_seconds),
                )
            )
            await session.commit()


async def run_worker(coordinator: SettlementCoordinator, poll_seconds: float) -> None:
    while True:
        processed = await coordinator.process_one()
        if not processed:
            await asyncio.sleep(poll_seconds)


async def settlement_worker_available(redis: Redis) -> bool:
    async for _key in redis.scan_iter(match=WORKER_HEARTBEAT_PATTERN, count=10):
        return True
    return False


async def _maintain_worker_heartbeat(redis: Redis, ttl_seconds: int) -> None:
    key = f"northgate:settlement:worker:heartbeat:{uuid4()}"
    interval = max(1.0, ttl_seconds / 3)
    try:
        while True:
            await redis.set(key, int(datetime.now(UTC).timestamp() * 1000), ex=ttl_seconds)
            await asyncio.sleep(interval)
    finally:
        try:
            await redis.delete(key)
        except Exception:
            pass


def _mapping(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise SettlementPayloadError(f"{key} must be an object")
    return value


def _string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise SettlementPayloadError(f"{key} must be a string")
    return value


def _optional_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise SettlementPayloadError(f"{key} must be a string or null")
    return value


def _optional_int(payload: dict[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise SettlementPayloadError(f"{key} must be an integer or null")
    return value


def _optional_uuid(payload: dict[str, object], key: str) -> UUID | None:
    value = _optional_string(payload, key)
    return UUID(value) if value is not None else None


async def _run_cli(*, once: bool, poll_seconds: float) -> None:
    settings = get_settings()
    database = Database(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    policy_engine = PolicyEngine(redis, lease_seconds=settings.concurrency_lease_seconds)
    coordinator = SettlementCoordinator(database, policy_engine)
    try:
        if once:
            while await coordinator.process_one():
                pass
        else:
            heartbeat = asyncio.create_task(
                _maintain_worker_heartbeat(
                    redis,
                    settings.settlement_worker_heartbeat_ttl_seconds,
                )
            )
            try:
                await run_worker(coordinator, poll_seconds)
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
    finally:
        await database.close()
        await redis.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Process durable Northgate settlement events.")
    parser.add_argument("--once", action="store_true", help="Drain available events and exit.")
    parser.add_argument("--poll-seconds", type=float, default=0.25)
    args = parser.parse_args()
    if not 0.05 <= args.poll_seconds <= 60:
        raise SystemExit("--poll-seconds must be between 0.05 and 60")
    asyncio.run(_run_cli(once=args.once, poll_seconds=args.poll_seconds))


if __name__ == "__main__":
    main()
