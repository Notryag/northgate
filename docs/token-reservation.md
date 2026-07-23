# Token admission reservation

Status: implemented  
Last reviewed: 2026-07-23

This document is the implementation contract for token estimation, policy
admission, terminal reconciliation, and reservation diagnostics. It applies to
OpenAI-compatible chat-completions requests.

## Reservation model

Northgate estimates only model-visible request fields: `messages`, `tools`,
`functions`, `response_format`, `tool_choice`, and `parallel_tool_calls`. It does
not charge HTTP framing, Northgate metadata, or unrelated request fields to the
prompt estimate.

For a known model, prompt text is encoded with the model's `tiktoken` encoding and
includes a small per-message framing allowance. OpenAI `gpt-4o`, `gpt-5`, `o1`,
`o3`, and `o4` family names use `o200k_base` when a newly released suffix is not
yet present in the tokenizer's exact model registry. For any other unknown model
or provider, the safe fallback remains `ceil(model_visible_utf8_bytes / 3)`.
The fallback is explicitly reported; Northgate does not silently select an
unrelated tokenizer.

The output reservation uses the first available value in this order:

1. positive request `max_completion_tokens` or `max_tokens`;
2. the selected route's `default_max_output_tokens`;
3. `NORTHGATE_POLICY_MODEL_MAX_OUTPUT_TOKENS[model]`;
4. `NORTHGATE_POLICY_DEFAULT_MAX_OUTPUT_TOKENS`.

The reservation is:

```text
margin_per_attempt = max(16, ceil(estimated_prompt_tokens * margin_percent / 100))
reservation_margin_tokens = margin_per_attempt * attempt_multiplier
reserved_total_tokens =
  (estimated_prompt_tokens + reserved_output_tokens) * attempt_multiplier
  + reservation_margin_tokens
```

`attempt_multiplier` is the complete bounded retry/fallback plan. Incremental
reservation before each later attempt remains future work; the current model
therefore preserves the existing fail-closed budget guarantee while making every
component measurable.

## Durable fields

Each new `RequestRecord` stores:

- `estimated_prompt_tokens`;
- `reserved_output_tokens`;
- `attempt_multiplier`;
- `reservation_margin_tokens`;
- `reserved_total_tokens`;
- `token_estimator`;
- `output_limit_source`.

The legacy `estimated_tokens` column remains a compatibility alias whose exact
value for new records is `reserved_total_tokens`. Historical records have only
that legacy total; diagnostics expose it as the reserved total but leave unknown
components null rather than inventing them.

Terminal diagnostics derive `actual_total_tokens`, `released_tokens`, and
`estimate_actual_ratio`. They remain null when provider total usage is
unknown. A released amount is never inferred from an ambiguous or missing actual
total.

## Settlement and observability

The Redis token reservation is reconciled once through the existing policy lease
and settlement-event idempotency key. Success, provider failure, client
disconnect, direct cancellation, retry, and fallback all use the same terminal
request settlement boundary. Metrics observe reservation and terminal actuals
even when token policy is disabled; policy settlement itself runs only when a
lease exists.

Request diagnostics, analytics request rows, `northgate-inspect`, MCP results,
and the request-detail console expose the same fields without exposing request
bodies. Prometheus exports bounded component, actual-ratio, and released-token
observations.

Time-range diagnostics emit `EXCESSIVE_TOKEN_RESERVATION` only when the number of
records with known reserved and actual totals reaches
`NORTHGATE_POLICY_ESTIMATE_EXCESS_MIN_SAMPLE_SIZE` and their aggregate ratio
reaches `NORTHGATE_POLICY_ESTIMATE_EXCESS_RATIO_THRESHOLD`. A single request with
an explicit high output limit is therefore not classified by itself.

## Configuration

| Setting | Default | Purpose |
| --- | ---: | --- |
| `NORTHGATE_POLICY_DEFAULT_MAX_OUTPUT_TOKENS` | `4096` | global output fallback |
| `NORTHGATE_POLICY_MODEL_MAX_OUTPUT_TOKENS` | empty | JSON model-to-positive-integer defaults |
| `NORTHGATE_POLICY_PROMPT_MARGIN_PERCENT` | `15` | prompt safety margin before the per-attempt minimum |
| `NORTHGATE_POLICY_ESTIMATE_EXCESS_RATIO_THRESHOLD` | `3.0` | aggregate warning threshold |
| `NORTHGATE_POLICY_ESTIMATE_EXCESS_MIN_SAMPLE_SIZE` | `10` | minimum known sample count for the warning |

Changing these values changes admission behavior. Calibrate route or model output
defaults from provider-reported usage and application requirements; do not lower
unknown-model prompt safety merely to improve apparent utilization.
