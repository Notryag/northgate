import json
import time

from northgate.usage import UsageAccumulator


def test_sse_usage_supports_crlf_and_cached_prompt_tokens() -> None:
    accumulator = UsageAccumulator("text/event-stream; charset=utf-8", time.perf_counter())
    usage_payload = json.dumps(
        {
            "usage": {
                "prompt_tokens": 9,
                "completion_tokens": 1,
                "total_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 7},
            }
        }
    ).encode()

    accumulator.observe(b"data: " + usage_payload + b"\r\n\r\n")
    accumulator.observe(b"data: [DONE]\r\n\r\n")

    assert accumulator.result().prompt_tokens == 9
    assert accumulator.result().cached_prompt_tokens == 7
    assert accumulator.terminal_event_seen is True


def test_sse_usage_supports_separator_split_across_chunks() -> None:
    accumulator = UsageAccumulator("text/event-stream", time.perf_counter())
    accumulator.observe(b'data: {"usage":{"input_tokens":4,"output_tokens":2,')
    accumulator.observe(b'"total_tokens":6,"input_tokens_details":{"cache_read":3}}}\r\n')
    accumulator.observe(b"\r\ndata: [DONE]\n\n")

    assert accumulator.result().total_tokens == 6
    assert accumulator.result().cached_prompt_tokens == 3
    assert accumulator.terminal_event_seen is True
