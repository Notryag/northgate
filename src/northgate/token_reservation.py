import json
import math
from dataclasses import dataclass
from typing import Literal

import tiktoken

OutputLimitSource = Literal["request", "route", "model", "global", "cache"]
_OPENAI_O200K_PREFIXES = ("gpt-4o", "gpt-5", "o1", "o3", "o4")


@dataclass(frozen=True)
class TokenReservation:
    estimated_prompt_tokens: int
    reserved_output_tokens: int
    attempt_multiplier: int
    reservation_margin_tokens: int
    reserved_total_tokens: int
    estimator: str
    output_limit_source: OutputLimitSource

    @classmethod
    def cache_hit(cls) -> "TokenReservation":
        return cls(
            estimated_prompt_tokens=0,
            reserved_output_tokens=0,
            attempt_multiplier=0,
            reservation_margin_tokens=0,
            reserved_total_tokens=0,
            estimator="exact_cache",
            output_limit_source="cache",
        )


def estimate_token_reservation(
    body: bytes,
    *,
    model: str | None,
    route_default_output_tokens: int | None,
    model_output_defaults: dict[str, int],
    global_default_output_tokens: int,
    margin_percent: int,
    attempt_multiplier: int,
) -> TokenReservation:
    if attempt_multiplier < 1:
        raise ValueError("attempt_multiplier must be positive")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None

    visible_body = _model_visible_body(payload, body)
    prompt_tokens, estimator = _estimate_prompt(visible_body, payload, model)
    output_tokens, output_source = _output_reservation(
        payload,
        model=model,
        route_default=route_default_output_tokens,
        model_defaults=model_output_defaults,
        global_default=global_default_output_tokens,
    )
    margin_per_attempt = max(16, math.ceil(prompt_tokens * margin_percent / 100))
    margin_tokens = margin_per_attempt * attempt_multiplier
    reserved_total = (prompt_tokens + output_tokens) * attempt_multiplier + margin_tokens
    return TokenReservation(
        estimated_prompt_tokens=prompt_tokens,
        reserved_output_tokens=output_tokens,
        attempt_multiplier=attempt_multiplier,
        reservation_margin_tokens=margin_tokens,
        reserved_total_tokens=reserved_total,
        estimator=estimator,
        output_limit_source=output_source,
    )


def _model_visible_body(payload: object, raw_body: bytes) -> bytes:
    if not isinstance(payload, dict):
        return raw_body
    visible = {
        key: payload[key]
        for key in (
            "messages",
            "tools",
            "functions",
            "response_format",
            "tool_choice",
            "parallel_tool_calls",
        )
        if key in payload
    }
    return json.dumps(
        visible,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _estimate_prompt(
    visible_body: bytes,
    payload: object,
    model: str | None,
) -> tuple[int, str]:
    if model:
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = (
                tiktoken.get_encoding("o200k_base")
                if model.lower().startswith(_OPENAI_O200K_PREFIXES)
                else None
            )
        if encoding is not None:
            text = visible_body.decode("utf-8", errors="replace")
            encoded = len(encoding.encode(text, disallowed_special=()))
            message_count = len(payload.get("messages", [])) if isinstance(payload, dict) else 0
            return max(1, encoded + message_count * 4 + 2), f"tiktoken:{encoding.name}"
    return max(1, math.ceil(len(visible_body) / 3)), "utf8_bytes_div_3"


def _output_reservation(
    payload: object,
    *,
    model: str | None,
    route_default: int | None,
    model_defaults: dict[str, int],
    global_default: int,
) -> tuple[int, OutputLimitSource]:
    if isinstance(payload, dict):
        configured = payload.get("max_completion_tokens", payload.get("max_tokens"))
        if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
            return configured, "request"
    if route_default is not None:
        return route_default, "route"
    if model is not None and model in model_defaults:
        return model_defaults[model], "model"
    return global_default, "global"
