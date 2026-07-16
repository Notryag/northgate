import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from northgate.db.database import Database
from northgate.db.models import RequestRecord
from northgate.routing import ResolvedRoute

logger = structlog.get_logger()


class DuplicateRequestError(Exception):
    pass


@dataclass(frozen=True)
class UsageResult:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class UsageAccumulator:
    _MAX_CAPTURE_BYTES = 1024 * 1024

    def __init__(self, content_type: str, started_at: float) -> None:
        self.is_sse = content_type.startswith("text/event-stream")
        self.started_at = started_at
        self.first_token_ms: int | None = None
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
        while b"\n\n" in self._buffer:
            event, remainder = self._buffer.split(b"\n\n", 1)
            self._buffer = bytearray(remainder)
            data = b"\n".join(
                line[5:].lstrip() for line in event.splitlines() if line.startswith(b"data:")
            )
            if data and data != b"[DONE]":
                self._read_payload(data)

    def _read_payload(self, payload: bytes) -> None:
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return
        self._usage = UsageResult(
            prompt_tokens=_integer(usage.get("prompt_tokens", usage.get("input_tokens"))),
            completion_tokens=_integer(usage.get("completion_tokens", usage.get("output_tokens"))),
            total_tokens=_integer(usage.get("total_tokens")),
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
        price_id: UUID | None,
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
                    price_id=price_id,
                    outcome="started",
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
                    cost_microusd=cost_microusd,
                    latency_ms=latency_ms,
                    first_token_ms=first_token_ms,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()
