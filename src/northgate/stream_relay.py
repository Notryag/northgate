import asyncio
from collections.abc import AsyncIterator
from typing import Protocol

import httpx
from anyio import CancelScope

from northgate.usage import UsageAccumulator


class StreamFinalizer(Protocol):
    async def finish(
        self,
        *,
        accumulator: UsageAccumulator,
        outcome: str,
        transport_failed: bool,
        completed: bool,
        cache_body: bytearray | None,
    ) -> None: ...


async def _finish_during_cancellation(awaitable) -> None:
    with CancelScope(shield=True):
        await awaitable


async def relay_response_body(
    response: httpx.Response,
    *,
    started_at: float,
    cache_enabled: bool,
    cache_max_entry_bytes: int,
    finalizer: StreamFinalizer,
) -> AsyncIterator[bytes]:
    accumulator = UsageAccumulator(response.headers.get("content-type", ""), started_at)
    outcome = "succeeded" if response.status_code < 400 else "provider_error"
    transport_failed = False
    completed = False
    cache_body: bytearray | None = bytearray() if cache_enabled else None
    try:
        if response.is_stream_consumed:
            accumulator.observe(response.content)
            if cache_body is not None:
                if len(response.content) <= cache_max_entry_bytes:
                    cache_body.extend(response.content)
                else:
                    cache_body = None
            yield response.content
            completed = True
            return
        async for chunk in response.aiter_raw():
            accumulator.observe(chunk)
            if cache_body is not None:
                if len(cache_body) + len(chunk) <= cache_max_entry_bytes:
                    cache_body.extend(chunk)
                else:
                    cache_body = None
            yield chunk
            if accumulator.terminal_event_seen:
                break
        completed = True
    except asyncio.CancelledError:
        if accumulator.terminal_event_seen:
            outcome = "succeeded" if response.status_code < 400 else "provider_error"
            completed = True
        else:
            outcome = "client_disconnected"
        raise
    except httpx.TransportError:
        outcome = "provider_error"
        transport_failed = True
        raise
    finally:
        await _finish_during_cancellation(
            finalizer.finish(
                accumulator=accumulator,
                outcome=outcome,
                transport_failed=transport_failed,
                completed=completed,
                cache_body=cache_body,
            )
        )
