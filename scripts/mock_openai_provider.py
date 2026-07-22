"""Small OpenAI-compatible provider used for local tool-calling verification."""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    request_count = 0
    request_count_lock = threading.Lock()

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length))
        messages = request.get("messages", [])
        tools = request.get("tools", [])

        with self.request_count_lock:
            type(self).request_count += 1
            request_count = type(self).request_count
        fail_every = int(os.getenv("MOCK_PROVIDER_FAIL_EVERY", "0"))
        if fail_every > 0 and request_count % fail_every == 0:
            body = b'{"error":{"type":"injected_failure"}}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            first = {
                "id": "chatcmpl_northgate_stream",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": "first"}}],
            }
            self.wfile.write(f"data: {json.dumps(first)}\n\n".encode())
            self.wfile.flush()
            time.sleep(float(os.getenv("MOCK_PROVIDER_STREAM_DELAY_SECONDS", "5")))
            usage = {
                "id": "chatcmpl_northgate_stream",
                "object": "chat.completion.chunk",
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            }
            self.wfile.write(f"data: {json.dumps(usage)}\n\ndata: [DONE]\n\n".encode())
            self.wfile.flush()
            return

        if any(message.get("role") == "tool" for message in messages):
            message = {"role": "assistant", "content": "tool result accepted"}
            finish_reason = "stop"
        elif tools:
            tool_name = tools[0]["function"]["name"]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_northgate_1",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps({"value": "ping"}),
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": "ok"}
            finish_reason = "stop"

        body = json.dumps(
            {
                "id": "chatcmpl_northgate_mock",
                "object": "chat.completion",
                "created": 0,
                "model": request.get("model", "gpt-test"),
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", "provider-tool-call")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    bind = os.getenv("MOCK_PROVIDER_BIND", "127.0.0.1")
    port = int(os.getenv("MOCK_PROVIDER_PORT", "9090"))
    ThreadingHTTPServer((bind, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
