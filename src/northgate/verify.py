import json
import os
import sys
import time
from dataclasses import dataclass

import httpx


class VerificationError(Exception):
    pass


@dataclass(frozen=True)
class VerificationConfig:
    base_url: str
    application_key: str
    model: str
    verify_tool_calls: bool = True
    timeout_seconds: float = 60.0

    @classmethod
    def from_environment(cls) -> "VerificationConfig":
        required = {
            "base_url": os.getenv("NORTHGATE_VERIFY_BASE_URL", "").strip(),
            "application_key": os.getenv("NORTHGATE_VERIFY_APPLICATION_KEY", "").strip(),
            "model": os.getenv("NORTHGATE_VERIFY_MODEL", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            variables = ", ".join(f"NORTHGATE_VERIFY_{name.upper()}" for name in missing)
            raise VerificationError(f"Missing required environment variables: {variables}")
        tool_calls = os.getenv("NORTHGATE_VERIFY_TOOL_CALLS", "true").lower()
        if tool_calls not in {"true", "false"}:
            raise VerificationError("NORTHGATE_VERIFY_TOOL_CALLS must be true or false")
        try:
            timeout = float(os.getenv("NORTHGATE_VERIFY_TIMEOUT_SECONDS", "60"))
        except ValueError as exc:
            raise VerificationError("NORTHGATE_VERIFY_TIMEOUT_SECONDS must be a number") from exc
        if timeout <= 0 or timeout > 600:
            raise VerificationError("NORTHGATE_VERIFY_TIMEOUT_SECONDS must be between 0 and 600")
        return cls(
            **required,
            verify_tool_calls=tool_calls == "true",
            timeout_seconds=timeout,
        )


def _endpoint(config: VerificationConfig) -> str:
    return f"{config.base_url.rstrip('/')}/chat/completions"


def _headers(config: VerificationConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {config.application_key}"}


def _check_response(response: httpx.Response) -> dict[str, object]:
    if response.status_code >= 400:
        code = None
        try:
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                code = payload["error"].get("code")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        suffix = f" ({code})" if isinstance(code, str) else ""
        raise VerificationError(f"Gateway returned HTTP {response.status_code}{suffix}")
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise VerificationError("Gateway returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise VerificationError("Gateway returned a non-object JSON response")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise VerificationError("Response does not contain a non-empty choices array")
    if not response.headers.get("Northgate-Request-Id", "").startswith("req_"):
        raise VerificationError("Response is missing Northgate-Request-Id")
    return payload


def _verify_non_streaming(client: httpx.Client, config: VerificationConfig) -> str:
    response = client.post(
        _endpoint(config),
        headers=_headers(config),
        json={
            "model": config.model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "stream": False,
        },
    )
    payload = _check_response(response)
    usage = payload.get("usage")
    return "reported usage" if isinstance(usage, dict) else "usage not reported"


def _verify_streaming(client: httpx.Client, config: VerificationConfig) -> str:
    started_at = time.perf_counter()
    first_event_ms: int | None = None
    events = 0
    completed = False
    with client.stream(
        "POST",
        _endpoint(config),
        headers=_headers(config),
        json={
            "model": config.model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    ) as response:
        if response.status_code >= 400:
            response.read()
            _check_response(response)
        if not response.headers.get("content-type", "").startswith("text/event-stream"):
            raise VerificationError("Streaming response is not text/event-stream")
        if not response.headers.get("Northgate-Request-Id", "").startswith("req_"):
            raise VerificationError("Streaming response is missing Northgate-Request-Id")
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                completed = True
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError as exc:
                raise VerificationError(
                    "Streaming response contains invalid JSON SSE data"
                ) from exc
            if not isinstance(event, dict):
                raise VerificationError("Streaming SSE data is not a JSON object")
            events += 1
            if first_event_ms is None:
                first_event_ms = round((time.perf_counter() - started_at) * 1000)
    if events == 0:
        raise VerificationError("Streaming response did not contain a JSON event")
    if not completed:
        raise VerificationError("Streaming response did not contain [DONE]")
    return f"{events} events, first event {first_event_ms} ms"


def _verify_tool_call(client: httpx.Client, config: VerificationConfig) -> str:
    response = client.post(
        _endpoint(config),
        headers=_headers(config),
        json={
            "model": config.model,
            "messages": [{"role": "user", "content": "Call the verification tool."}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "northgate_verification",
                        "description": "Return a verification value.",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    },
                }
            ],
            "tool_choice": {
                "type": "function",
                "function": {"name": "northgate_verification"},
            },
            "stream": False,
        },
    )
    payload = _check_response(response)
    choices = payload["choices"]
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    if not isinstance(tool_calls, list) or not tool_calls:
        raise VerificationError("Response does not contain a tool call")
    function = tool_calls[0].get("function") if isinstance(tool_calls[0], dict) else None
    if not isinstance(function, dict) or function.get("name") != "northgate_verification":
        raise VerificationError("Response called an unexpected tool")
    arguments = function.get("arguments")
    if not isinstance(arguments, str):
        raise VerificationError("Tool call arguments are not a JSON string")
    try:
        json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise VerificationError("Tool call arguments contain invalid JSON") from exc
    return "tool name and arguments preserved"


def run_verification(
    config: VerificationConfig, *, client: httpx.Client | None = None
) -> list[tuple[str, str]]:
    owns_client = client is None
    active_client = client or httpx.Client(timeout=config.timeout_seconds, follow_redirects=False)
    try:
        results = [
            ("non-streaming", _verify_non_streaming(active_client, config)),
            ("streaming", _verify_streaming(active_client, config)),
        ]
        if config.verify_tool_calls:
            results.append(("tool calls", _verify_tool_call(active_client, config)))
        return results
    except httpx.TimeoutException as exc:
        raise VerificationError("Verification request timed out") from exc
    except httpx.TransportError as exc:
        raise VerificationError("Could not reach the Northgate endpoint") from exc
    finally:
        if owns_client:
            active_client.close()


def main() -> None:
    try:
        config = VerificationConfig.from_environment()
        results = run_verification(config)
    except VerificationError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    for name, detail in results:
        print(f"PASS  {name}: {detail}")


if __name__ == "__main__":
    main()
