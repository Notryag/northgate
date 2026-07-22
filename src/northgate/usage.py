import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from northgate.db.database import Database
from northgate.db.models import ProviderAttemptRecord, RequestRecord
from northgate.routing import ResolvedRoute

logger = structlog.get_logger()


class DuplicateRequestError(Exception):
    pass


@dataclass(frozen=True)
class UsageResult:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_prompt_tokens: int | None = None


class UsageAccumulator:
    _MAX_CAPTURE_BYTES = 1024 * 1024

    def __init__(self, content_type: str, started_at: float) -> None:
        self.is_sse = content_type.startswith("text/event-stream")
        self.started_at = started_at
        self.first_token_ms: int | None = None
        self.terminal_event_seen = False
        self._buffer = bytearray()
        self._usage = UsageResult()

    def observe(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self.first_token_ms is None:
            self.first_token_ms = round((time.perf_counter() - self.started_at) * 1000)
        if len(self._buffer) + len(chunk) > self._MAX_CAPTURE_BYTES:
            return
        self._buffer.extend(chunk)
        if self.is_sse:
            self._consume_sse_events()

    def result(self) -> UsageResult:
        if not self.is_sse and self._buffer:
            self._read_payload(bytes(self._buffer))
        return self._usage

    def _consume_sse_events(self) -> None:
        while True:
            separators = [
                (index, separator)
                for separator in (b"\r\n\r\n", b"\n\n")
                if (index := self._buffer.find(separator)) >= 0
            ]
            if not separators:
                return
            index, separator = min(separators, key=lambda item: item[0])
            event = bytes(self._buffer[:index])
            remainder = self._buffer[index + len(separator) :]
            self._buffer = bytearray(remainder)
            data = b"\n".join(
                line[5:].lstrip() for line in event.splitlines() if line.startswith(b"data:")
            )
            if data == b"[DONE]":
                self.terminal_event_seen = True
            elif data:
                self._read_payload(data)

    def _read_payload(self, payload: bytes) -> None:
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return
        prompt_details = usage.get("prompt_tokens_details", usage.get("input_tokens_details"))
        cached_prompt_tokens = None
        if isinstance(prompt_details, dict):
            cached_prompt_tokens = _integer(
                prompt_details.get("cached_tokens", prompt_details.get("cache_read"))
            )
        self._usage = UsageResult(
            prompt_tokens=_integer(usage.get("prompt_tokens", usage.get("input_tokens"))),
            completion_tokens=_integer(usage.get("completion_tokens", usage.get("output_tokens"))),
            total_tokens=_integer(usage.get("total_tokens")),
            cached_prompt_tokens=cached_prompt_tokens,
        )


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class UsageRecorder:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def start(
        self,
        *,
        request_id: str,
        route: ResolvedRoute,
        model: str | None,
        request_metadata: dict[str, str],
        request_metadata_trust: dict[str, str],
        price_id: UUID | None,
        estimated_tokens: int,
        cache_status: str,
    ) -> None:
        async with self.database.sessions() as session:
            session.add(
                RequestRecord(
                    request_id=request_id,
                    project_id=route.project_id,
                    gateway_id=route.gateway_id,
                    route_id=route.route_id,
                    provider=route.provider,
                    model=model,
                    request_metadata=request_metadata or None,
                    request_metadata_trust=request_metadata_trust or None,
                    price_id=price_id,
                    estimated_tokens=estimated_tokens,
                    cache_status=cache_status,
                    outcome="started",
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise DuplicateRequestError from exc

    async def record_rejection(
        self,
        *,
        request_id: str,
        route: ResolvedRoute,
        model: str | None,
        request_metadata: dict[str, str],
        request_metadata_trust: dict[str, str],
        price_id: UUID | None,
        estimated_tokens: int,
        cache_status: str,
        error_code: str,
        status_code: int,
    ) -> None:
        async with self.database.sessions() as session:
            session.add(
                RequestRecord(
                    request_id=request_id,
                    project_id=route.project_id,
                    gateway_id=route.gateway_id,
                    route_id=route.route_id,
                    provider=route.provider,
                    model=model,
                    request_metadata=request_metadata or None,
                    request_metadata_trust=request_metadata_trust or None,
                    price_id=price_id,
                    estimated_tokens=estimated_tokens,
                    cache_status=cache_status,
                    error_code=error_code,
                    outcome="policy_rejected",
                    http_status=status_code,
                    latency_ms=0,
                    completed_at=datetime.now(UTC),
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise DuplicateRequestError from exc

    async def settle(
        self,
        *,
        request_id: str,
        outcome: str,
        status_code: int | None,
        provider_request_id: str | None,
        latency_ms: int,
        first_token_ms: int | None,
        usage: UsageResult,
        cost_microusd: int | None,
        final_route: ResolvedRoute,
        price_id: UUID | None,
    ) -> None:
        async with self.database.sessions() as session:
            await session.execute(
                update(RequestRecord)
                .where(
                    RequestRecord.request_id == request_id,
                    RequestRecord.outcome == "started",
                )
                .values(
                    outcome=outcome,
                    http_status=status_code,
                    provider_request_id=provider_request_id,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                    cached_prompt_tokens=usage.cached_prompt_tokens,
                    cost_microusd=cost_microusd,
                    route_id=final_route.route_id,
                    provider=final_route.provider,
                    price_id=price_id,
                    latency_ms=latency_ms,
                    first_token_ms=first_token_ms,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()

    async def start_attempt(
        self,
        *,
        request_id: str,
        attempt_index: int,
        route: ResolvedRoute,
        price_id: UUID | None,
    ) -> UUID:
        async with self.database.sessions() as session:
            attempt = ProviderAttemptRecord(
                request_id=request_id,
                attempt_index=attempt_index,
                route_id=route.route_id,
                provider=route.provider,
                price_id=price_id,
                outcome="started",
            )
            session.add(attempt)
            await session.commit()
            return attempt.id

    async def settle_attempt(
        self,
        *,
        attempt_id: UUID,
        outcome: str,
        status_code: int | None,
        provider_request_id: str | None,
        latency_ms: int,
        usage: UsageResult,
        cost_microusd: int | None,
    ) -> None:
        async with self.database.sessions() as session:
            await session.execute(
                update(ProviderAttemptRecord)
                .where(
                    ProviderAttemptRecord.id == attempt_id,
                    ProviderAttemptRecord.outcome == "started",
                )
                .values(
                    outcome=outcome,
                    http_status=status_code,
                    provider_request_id=provider_request_id,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                    cached_prompt_tokens=usage.cached_prompt_tokens,
                    cost_microusd=cost_microusd,
                    latency_ms=latency_ms,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()
