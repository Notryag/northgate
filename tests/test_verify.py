import json

import httpx
import pytest

from northgate.verify import VerificationConfig, VerificationError, run_verification


def _config() -> VerificationConfig:
    return VerificationConfig(
        base_url="http://northgate/v1/gateways/test/openai",
        application_key="application-secret",
        model="gpt-test",
    )


def test_verifier_checks_json_streaming_and_tool_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer application-secret"
        payload = json.loads(request.content)
        headers = {"Northgate-Request-Id": "req_verify"}
        if payload.get("stream"):
            return httpx.Response(
                200,
                headers={**headers, "Content-Type": "text/event-stream"},
                content=(b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n'),
            )
        if payload.get("tools"):
            message = {
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {
                            "name": "northgate_verification",
                            "arguments": '{"value":"ok"}',
                        },
                    }
                ]
            }
        else:
            message = {"content": "OK"}
        return httpx.Response(
            200,
            headers=headers,
            json={
                "choices": [{"message": message}],
                "usage": {"total_tokens": 2},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = run_verification(_config(), client=client)

    assert [name for name, _ in results] == ["non-streaming", "streaming", "tool calls"]
    assert results[0][1] == "reported usage"
    assert "1 events" in results[1][1]
    assert results[2][1] == "tool name and arguments preserved"


def test_verifier_error_does_not_include_gateway_response_content() -> None:
    response_secret = "provider-response-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            headers={"Northgate-Request-Id": "req_verify"},
            json={"error": {"code": "PROVIDER_UNAVAILABLE", "message": response_secret}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(VerificationError) as raised:
            run_verification(_config(), client=client)

    assert "HTTP 502 (PROVIDER_UNAVAILABLE)" in str(raised.value)
    assert response_secret not in str(raised.value)
    assert _config().application_key not in str(raised.value)
