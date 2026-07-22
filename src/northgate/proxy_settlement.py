import time
from dataclasses import dataclass
from uuid import UUID

import structlog

from northgate.metrics import Metrics
from northgate.policy import PolicyEngine, PolicyLease
from northgate.routing import ResolvedRoute
from northgate.settlement import SettlementCoordinator
from northgate.tracing import add_span_event
from northgate.usage import UsageRecorder, UsageResult

logger = structlog.get_logger()


@dataclass
class AttemptTotals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_prompt_tokens: int = 0
    cached_prompt_tokens_complete: bool = True
    cost_microusd: int = 0
    has_usage: bool = False
    has_cost: bool = False
    ambiguous: bool = False

    def add(self, usage: UsageResult, cost_microusd: int | None) -> None:
        if usage.total_tokens is not None:
            self.prompt_tokens += usage.prompt_tokens or 0
            self.completion_tokens += usage.completion_tokens or 0
            self.total_tokens += usage.total_tokens
            if usage.cached_prompt_tokens is None:
                self.cached_prompt_tokens_complete = False
            else:
                self.cached_prompt_tokens += usage.cached_prompt_tokens
            self.has_usage = True
        if cost_microusd is not None:
            self.cost_microusd += cost_microusd
            self.has_cost = True

    def usage(self) -> UsageResult:
        if not self.has_usage:
            return UsageResult()
        return UsageResult(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.total_tokens,
            cached_prompt_tokens=(
                self.cached_prompt_tokens if self.cached_prompt_tokens_complete else None
            ),
        )


async def settle_request_safely(
    recorder: UsageRecorder,
    *,
    metrics: Metrics | None,
    request_id: str,
    outcome: str,
    status_code: int | None,
    provider_request_id: str | None,
    started_at: float,
    usage: UsageResult,
    cost_microusd: int | None,
    first_token_ms: int | None,
    final_route: ResolvedRoute,
    price_id: UUID | None,
) -> None:
    try:
        await recorder.settle(
            request_id=request_id,
            outcome=outcome,
            status_code=status_code,
            provider_request_id=provider_request_id,
            latency_ms=round((time.perf_counter() - started_at) * 1000),
            first_token_ms=first_token_ms,
            usage=usage,
            cost_microusd=cost_microusd,
            final_route=final_route,
            price_id=price_id,
        )
    except Exception:
        if metrics is not None:
            metrics.settlement_failures.labels(stage="request").inc()
        await logger.aexception("usage_settlement_failed", northgate_request_id=request_id)


async def enqueue_request_settlement(
    coordinator: SettlementCoordinator | None,
    *,
    policy_engine: PolicyEngine | None,
    policy_lease: PolicyLease | None,
    metrics: Metrics | None,
    request_id: str,
    outcome: str,
    status_code: int | None,
    provider_request_id: str | None,
    started_at: float,
    usage: UsageResult,
    cost_microusd: int | None,
    first_token_ms: int | None,
    final_route: ResolvedRoute,
    price_id: UUID | None,
    attempt_id: UUID | None = None,
    attempt_started_at: float | None = None,
    attempt_outcome: str | None = None,
    attempt_usage: UsageResult | None = None,
    attempt_cost_microusd: int | None = None,
) -> bool:
    if coordinator is None:
        return False
    payload: dict[str, object] = {
        "request": {
            "outcome": outcome,
            "status_code": status_code,
            "provider_request_id": provider_request_id,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cached_prompt_tokens": usage.cached_prompt_tokens,
            "cost_microusd": cost_microusd,
            "route_id": str(final_route.route_id) if final_route.route_id is not None else None,
            "provider": final_route.provider,
            "price_id": str(price_id) if price_id is not None else None,
            "latency_ms": round((time.perf_counter() - started_at) * 1000),
            "first_token_ms": first_token_ms,
        },
        "attempt": (
            {
                "id": str(attempt_id),
                "outcome": attempt_outcome or outcome,
                "status_code": status_code,
                "provider_request_id": provider_request_id,
                "prompt_tokens": (attempt_usage or usage).prompt_tokens,
                "completion_tokens": (attempt_usage or usage).completion_tokens,
                "total_tokens": (attempt_usage or usage).total_tokens,
                "cached_prompt_tokens": (attempt_usage or usage).cached_prompt_tokens,
                "cost_microusd": attempt_cost_microusd,
                "latency_ms": round(
                    (time.perf_counter() - (attempt_started_at or started_at)) * 1000
                ),
            }
            if attempt_id is not None
            else None
        ),
        "policy": (
            {
                "gateway_key": policy_lease.gateway_key,
                "token_day": policy_lease.token_day,
                "spend_day": policy_lease.spend_day,
                "spend_month": policy_lease.spend_month,
                "actual_tokens": usage.total_tokens,
                "actual_cost_microusd": cost_microusd,
            }
            if policy_lease is not None
            else None
        ),
    }
    try:
        event_id = await coordinator.enqueue(request_id=request_id, payload=payload)
    except Exception:
        if metrics is not None:
            metrics.settlement_failures.labels(stage="outbox_enqueue").inc()
        await logger.aexception(
            "settlement_outbox_enqueue_failed",
            northgate_request_id=request_id,
        )
        return False
    if policy_engine is not None and policy_lease is not None:
        await policy_engine.stop_renewal(policy_lease)
    return await coordinator.process(event_id)


async def settle_attempt_safely(
    recorder: UsageRecorder | None,
    *,
    metrics: Metrics | None,
    route: ResolvedRoute,
    attempt_id: UUID | None,
    outcome: str,
    status_code: int | None,
    provider_request_id: str | None,
    started_at: float,
    usage: UsageResult,
    cost_microusd: int | None,
) -> None:
    duration_seconds = time.perf_counter() - started_at
    if metrics is not None:
        metrics.observe_provider_attempt(
            provider=route.provider,
            adapter=route.adapter,
            outcome=outcome,
            duration_seconds=duration_seconds,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cached_prompt_tokens=usage.cached_prompt_tokens,
            cost_microusd=cost_microusd,
        )
    add_span_event(
        "northgate.provider_attempt",
        {
            "northgate.provider": route.provider,
            "northgate.adapter": route.adapter,
            "northgate.attempt.outcome": outcome,
            "northgate.attempt.duration_ms": round(duration_seconds * 1000, 2),
            "northgate.usage.prompt_tokens": usage.prompt_tokens,
            "northgate.usage.completion_tokens": usage.completion_tokens,
            "northgate.usage.cached_prompt_tokens": usage.cached_prompt_tokens,
            "northgate.cost_microusd": cost_microusd,
            "http.response.status_code": status_code,
        },
    )
    if recorder is None or attempt_id is None:
        return
    try:
        await recorder.settle_attempt(
            attempt_id=attempt_id,
            outcome=outcome,
            status_code=status_code,
            provider_request_id=provider_request_id,
            latency_ms=round((time.perf_counter() - started_at) * 1000),
            usage=usage,
            cost_microusd=cost_microusd,
        )
    except Exception:
        if metrics is not None:
            metrics.settlement_failures.labels(stage="attempt").inc()
        await logger.aexception("provider_attempt_settlement_failed", attempt_id=str(attempt_id))


async def settle_attempt_with_outbox(
    coordinator: SettlementCoordinator | None,
    recorder: UsageRecorder | None,
    *,
    metrics: Metrics | None,
    route: ResolvedRoute,
    request_id: str,
    attempt_id: UUID | None,
    outcome: str,
    status_code: int | None,
    provider_request_id: str | None,
    started_at: float,
    usage: UsageResult,
    cost_microusd: int | None,
) -> None:
    if coordinator is not None and recorder is not None and attempt_id is not None:
        payload: dict[str, object] = {
            "request": None,
            "attempt": {
                "id": str(attempt_id),
                "outcome": outcome,
                "status_code": status_code,
                "provider_request_id": provider_request_id,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "cached_prompt_tokens": usage.cached_prompt_tokens,
                "cost_microusd": cost_microusd,
                "latency_ms": round((time.perf_counter() - started_at) * 1000),
            },
            "policy": None,
        }
        try:
            event_id = await coordinator.enqueue(
                request_id=request_id,
                event_key=f"attempt:{attempt_id}",
                payload=payload,
            )
        except Exception:
            if metrics is not None:
                metrics.settlement_failures.labels(stage="outbox_enqueue").inc()
            await logger.aexception(
                "attempt_settlement_outbox_enqueue_failed",
                northgate_request_id=request_id,
                attempt_id=str(attempt_id),
            )
        else:
            await settle_attempt_safely(
                None,
                metrics=metrics,
                route=route,
                attempt_id=attempt_id,
                outcome=outcome,
                status_code=status_code,
                provider_request_id=provider_request_id,
                started_at=started_at,
                usage=usage,
                cost_microusd=cost_microusd,
            )
            if await coordinator.process(event_id):
                return
    await settle_attempt_safely(
        recorder,
        metrics=metrics,
        route=route,
        attempt_id=attempt_id,
        outcome=outcome,
        status_code=status_code,
        provider_request_id=provider_request_id,
        started_at=started_at,
        usage=usage,
        cost_microusd=cost_microusd,
    )
