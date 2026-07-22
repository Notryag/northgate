import argparse
import json

import httpx


def _post(client: httpx.Client, payload: dict[str, object]) -> dict[str, object]:
    response = client.post("/chat/completions", json=payload)
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError("provider response is not an object")
    return result


def _stream(client: httpx.Client, *, disconnect: bool) -> None:
    completed = False
    events = 0
    with client.stream(
        "POST",
        "/chat/completions",
        json={"model": "gpt-soak", "messages": [{"role": "user", "content": "ok"}], "stream": True},
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            events += 1
            if disconnect:
                break
            if line[5:].strip() == "[DONE]":
                completed = True
                break
    if events == 0 or (not disconnect and not completed):
        raise RuntimeError("stream did not produce the expected terminal events")


def run(iterations: int, mode: str) -> None:
    headers = {"Authorization": "Bearer ng_soak_application"}
    with httpx.Client(
        base_url="http://127.0.0.1:18082/v1/gateways/soak/openai",
        headers=headers,
        timeout=10,
    ) as client:
        if mode == "stream":
            _stream(client, disconnect=False)
            return
        for _ in range(iterations):
            _post(
                client,
                {"model": "gpt-soak", "messages": [{"role": "user", "content": "ok"}]},
            )
            _stream(client, disconnect=False)
            tool_response = _post(
                client,
                {
                    "model": "gpt-soak",
                    "messages": [{"role": "user", "content": "use tool"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "soak_tool",
                                "parameters": {"type": "object"},
                            },
                        }
                    ],
                },
            )
            choices = tool_response.get("choices")
            if not isinstance(choices, list) or not choices:
                raise RuntimeError("tool response is missing choices")
            message = choices[0].get("message")
            if not isinstance(message, dict) or not message.get("tool_calls"):
                raise RuntimeError("tool response is missing a tool call")
            _post(
                client,
                {
                    "model": "gpt-soak",
                    "messages": [
                        message,
                        {"role": "tool", "tool_call_id": "call_northgate_1", "content": "ok"},
                    ],
                },
            )
            _stream(client, disconnect=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--mode", choices=("full", "stream"), default="full")
    args = parser.parse_args()
    if not 1 <= args.iterations <= 1000:
        raise SystemExit("--iterations must be between 1 and 1000")
    run(args.iterations, args.mode)
    print(json.dumps({"iterations": args.iterations, "mode": args.mode, "status": "passed"}))


if __name__ == "__main__":
    main()
