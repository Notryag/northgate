from dataclasses import dataclass

import httpx
import pytest

from northgate.stream_relay import relay_response_body
from northgate.usage import UsageAccumulator


class TerminalThenErrorStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[],"usage":{"total_tokens":5}}\n\n'
        yield b"data: [DONE]\n\n"
        raise httpx.ReadError("relay read past terminal event")


class InterruptedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[]}\n\n'
        raise httpx.ReadError("provider interrupted stream")


@dataclass
class RecordedFinalization:
    accumulator: UsageAccumulator | None = None
    outcome: str | None = None
    transport_failed: bool | None = None
    completed: bool | None = None
    cache_body: bytearray | None = None

    async def finish(
        self,
        *,
        accumulator: UsageAccumulator,
        outcome: str,
        transport_failed: bool,
        completed: bool,
        cache_body: bytearray | None,
    ) -> None:
        self.accumulator = accumulator
        self.outcome = outcome
        self.transport_failed = transport_failed
        self.completed = completed
        self.cache_body = cache_body


@pytest.mark.anyio
async def test_relay_stops_at_terminal_event_before_upstream_error() -> None:
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=TerminalThenErrorStream(),
        request=httpx.Request("POST", "https://provider.test"),
    )
    finalization = RecordedFinalization()

    chunks = [
        chunk
        async for chunk in relay_response_body(
            response,
            started_at=0.0,
            cache_enabled=True,
            cache_max_entry_bytes=1024,
            finalizer=finalization,
        )
    ]

    assert chunks == [
        b'data: {"choices":[],"usage":{"total_tokens":5}}\n\n',
        b"data: [DONE]\n\n",
    ]
    assert finalization.outcome == "succeeded"
    assert finalization.transport_failed is False
    assert finalization.completed is True
    assert finalization.cache_body == bytearray(b"".join(chunks))
    assert finalization.accumulator is not None
    assert finalization.accumulator.terminal_event_seen is True


@pytest.mark.anyio
async def test_relay_reports_transport_failure_to_finalizer() -> None:
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=InterruptedStream(),
        request=httpx.Request("POST", "https://provider.test"),
    )
    finalization = RecordedFinalization()

    with pytest.raises(httpx.ReadError):
        async for _chunk in relay_response_body(
            response,
            started_at=0.0,
            cache_enabled=False,
            cache_max_entry_bytes=1024,
            finalizer=finalization,
        ):
            pass

    assert finalization.outcome == "provider_error"
    assert finalization.transport_failed is True
    assert finalization.completed is False
    assert finalization.cache_body is None


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
