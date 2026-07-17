from hashlib import sha256
from hmac import compare_digest

from fastapi import Request
from fastapi.responses import JSONResponse

from northgate.config import Settings


def authorize_operator(request: Request) -> JSONResponse | None:
    settings: Settings = request.app.state.settings
    expected = settings.operator_key_sha256
    if expected is None or not expected.get_secret_value():
        return JSONResponse(
            {
                "error": {
                    "code": "OPERATOR_AUTH_UNAVAILABLE",
                    "message": "Operator API unavailable",
                }
            },
            status_code=503,
        )

    scheme, separator, credential = request.headers.get("authorization", "").partition(" ")
    actual = (
        sha256(credential.encode()).hexdigest() if separator and scheme.lower() == "bearer" else ""
    )
    if not compare_digest(actual, expected.get_secret_value()):
        return JSONResponse(
            {"error": {"code": "INVALID_OPERATOR_KEY", "message": "Invalid operator key"}},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return None
