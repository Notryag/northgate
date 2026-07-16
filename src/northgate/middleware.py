import re
import time
from uuid import uuid4

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

logger = structlog.get_logger()

_REQUEST_ID_HEADER = b"northgate-request-id"
_VALID_REQUEST_ID = re.compile(r"^req_[A-Za-z0-9_-]{8,120}$")


def _request_id(headers: list[tuple[bytes, bytes]]) -> str:
    supplied = next(
        (
            value.decode("ascii", errors="ignore")
            for key, value in headers
            if key == _REQUEST_ID_HEADER
        ),
        "",
    )
    if _VALID_REQUEST_ID.fullmatch(supplied):
        return supplied
    return f"req_{uuid4().hex}"


class RequestContextMiddleware:
    """Attach request context without wrapping or consuming response bodies."""

    def __init__(self, app: object) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        clear_contextvars()
        request_id = _request_id(scope["headers"])
        scope.setdefault("state", {})["request_id"] = request_id
        bind_contextvars(request_id=request_id)
        started_at = time.perf_counter()
        status_code = 500

        async def send_with_context(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message["headers"].append((_REQUEST_ID_HEADER, request_id.encode("ascii")))
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        except Exception:
            await logger.aexception(
                "request_failed",
                method=scope["method"],
                path=scope["path"],
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
            )
            raise
        else:
            await logger.ainfo(
                "request_completed",
                method=scope["method"],
                path=scope["path"],
                status_code=status_code,
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
            )
        finally:
            clear_contextvars()
