import json
import math

from northgate.token_reservation import estimate_token_reservation


def _agent_body(*, model: str, max_tokens: int | None = None) -> bytes:
    tools = [
        {
            "type": "function",
            "function": {
                "name": f"tool_{index}",
                "description": "查询并更新日程，返回结构化结果",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "用户查询"},
                        "timezone": {"type": "string"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }
        for index in range(8)
    ]
    payload: dict[str, object] = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个严谨的日程助手，请按工具结果回答。"},
            {"role": "user", "content": "检查下周三上海时间的安排，并找一个两小时空档。"},
        ],
        "tools": tools,
        "stream": True,
        "metadata": {"must_not_affect_prompt_estimate": "x" * 500},
    }
    if max_tokens is not None:
        payload["max_completion_tokens"] = max_tokens
    return json.dumps(payload, ensure_ascii=False).encode()


def test_known_model_estimates_chinese_messages_and_tool_schemas() -> None:
    body = _agent_body(model="gpt-4o-mini")

    reservation = estimate_token_reservation(
        body,
        model="gpt-4o-mini",
        route_default_output_tokens=None,
        model_output_defaults={"gpt-4o-mini": 512},
        global_default_output_tokens=4096,
        margin_percent=15,
        attempt_multiplier=2,
    )

    assert reservation.estimator.startswith("tiktoken:")
    assert reservation.estimated_prompt_tokens > 300
    assert reservation.estimated_prompt_tokens < math.ceil(len(body) / 3)
    assert reservation.reserved_output_tokens == 512
    assert reservation.output_limit_source == "model"
    assert (
        reservation.reservation_margin_tokens
        >= math.ceil(reservation.estimated_prompt_tokens * 0.15) * 2
    )
    assert (
        reservation.reserved_total_tokens
        == (reservation.estimated_prompt_tokens + reservation.reserved_output_tokens) * 2
        + reservation.reservation_margin_tokens
    )


def test_known_openai_model_family_uses_current_encoding_before_registry_catches_up() -> None:
    reservation = estimate_token_reservation(
        _agent_body(model="gpt-5.4-mini"),
        model="gpt-5.4-mini",
        route_default_output_tokens=512,
        model_output_defaults={},
        global_default_output_tokens=4096,
        margin_percent=15,
        attempt_multiplier=1,
    )

    assert reservation.estimator == "tiktoken:o200k_base"
    assert reservation.output_limit_source == "route"


def test_unknown_model_fallback_is_bounded_and_ignores_non_visible_fields() -> None:
    first = estimate_token_reservation(
        _agent_body(model="unknown-provider-model", max_tokens=100),
        model="unknown-provider-model",
        route_default_output_tokens=None,
        model_output_defaults={},
        global_default_output_tokens=4096,
        margin_percent=20,
        attempt_multiplier=1,
    )
    second = estimate_token_reservation(
        _agent_body(model="unknown-provider-model", max_tokens=200),
        model="unknown-provider-model",
        route_default_output_tokens=None,
        model_output_defaults={},
        global_default_output_tokens=4096,
        margin_percent=20,
        attempt_multiplier=1,
    )

    assert first.estimator == "utf8_bytes_div_3"
    assert first.estimated_prompt_tokens == second.estimated_prompt_tokens
    assert first.reserved_output_tokens == 100
    assert second.reserved_output_tokens == 200
    assert first.output_limit_source == "request"


def test_output_reservation_precedence_is_request_route_model_global() -> None:
    common = {
        "model": "gpt-4o-mini",
        "model_output_defaults": {"gpt-4o-mini": 512},
        "global_default_output_tokens": 4096,
        "margin_percent": 0,
        "attempt_multiplier": 1,
    }
    explicit = estimate_token_reservation(
        _agent_body(model="gpt-4o-mini", max_tokens=64),
        route_default_output_tokens=256,
        **common,
    )
    route = estimate_token_reservation(
        _agent_body(model="gpt-4o-mini"),
        route_default_output_tokens=256,
        **common,
    )
    model = estimate_token_reservation(
        _agent_body(model="gpt-4o-mini"),
        route_default_output_tokens=None,
        **common,
    )
    global_default = estimate_token_reservation(
        _agent_body(model="unknown"),
        model="unknown",
        route_default_output_tokens=None,
        model_output_defaults={},
        global_default_output_tokens=4096,
        margin_percent=0,
        attempt_multiplier=1,
    )

    assert (explicit.reserved_output_tokens, explicit.output_limit_source) == (64, "request")
    assert (route.reserved_output_tokens, route.output_limit_source) == (256, "route")
    assert (model.reserved_output_tokens, model.output_limit_source) == (512, "model")
    assert (global_default.reserved_output_tokens, global_default.output_limit_source) == (
        4096,
        "global",
    )
