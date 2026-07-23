import json
from dataclasses import dataclass

from fastapi import Request

from northgate.routing import ResolvedRoute

_MAX_METADATA_BYTES = 8 * 1024
_MAX_METADATA_KEYS = 32
_MAX_METADATA_KEY_LENGTH = 64
_MAX_METADATA_VALUE_LENGTH = 256
_FORWARDED_REQUEST_HEADERS = {"accept", "content-type"}


class RequestBodyTooLargeError(Exception):
    pass


@dataclass(frozen=True)
class ProxyRequestInput:
    body: bytes
    model: str | None
    metadata: dict[str, str]
    forwarded_headers: dict[str, str]


def request_metadata(request: Request, route: ResolvedRoute) -> dict[str, str] | None:
    encoded = request.headers.get("northgate-metadata")
    if encoded is None:
        return {}
    if len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
        return None
    try:
        metadata = json.loads(encoded)
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, dict) or len(metadata) > _MAX_METADATA_KEYS:
        return None
    for key, value in metadata.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or len(key) > _MAX_METADATA_KEY_LENGTH
            or len(value) > _MAX_METADATA_VALUE_LENGTH
            or key.startswith("northgate.")
            or key not in route.allowed_metadata_keys
        ):
            return None
    return metadata


async def read_proxy_request_input(
    request: Request,
    *,
    metadata: dict[str, str],
    max_body_bytes: int,
) -> ProxyRequestInput:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_body_bytes:
                raise RequestBodyTooLargeError
        except ValueError:
            pass

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_body_bytes:
            raise RequestBodyTooLargeError
    body_bytes = bytes(body)
    return ProxyRequestInput(
        body=body_bytes,
        model=request_model(body_bytes),
        metadata=metadata,
        forwarded_headers={
            name: value
            for name, value in request.headers.items()
            if name.lower() in _FORWARDED_REQUEST_HEADERS
        },
    )


def request_model(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    model = payload.get("model")
    return model if isinstance(model, str) else None
