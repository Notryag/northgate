# Operator diagnostics interface

Status: shared service, Operator REST, and CLI implemented; MCP and console open
Last reviewed: 2026-07-22

This document defines a read-only diagnostics surface for operators and coding
agents. It builds on Northgate's existing analytics API and does not turn
Northgate into an agent runtime or an MCP data-plane proxy.

## Goal

Given an application correlation value such as a Dayboard `run_id`, an operator
or coding agent should be able to answer, without database access or prompt
logging:

- how many gateway requests and provider attempts occurred, and in what order;
- prompt, completion, total, and provider-cached prompt tokens per request;
- exact-cache status, latency, retry/fallback route, outcome, and stable error;
- whether request, attempt, settlement event, and policy settlement agree;
- which records are stale, incomplete, ambiguous, or missing usage;
- aggregate token, cache, latency, and cost totals for the correlated operation.

The immediate use case is AI-assisted incident diagnosis. The same contract must
remain useful to a human operator and CI without requiring an AI client.

## Existing foundation

Northgate already implements:

- `GET /api/v1/diagnostics/requests/{request_id}` with joined request, attempt,
  redacted settlement state, and stable findings;
- `GET /api/v1/diagnostics/correlated` with bounded metadata correlation,
  aggregate usage/cache/cost, finding counts, and ordered request diagnostics;
- `GET /api/v1/diagnostics/stale` with bounded stale request/attempt detection,
  recoverable-event classification, and active or expired concurrency leases;
- `GET /api/v1/usage/requests` filtered by metadata key and value;
- `GET /api/v1/usage/requests/{request_id}/attempts`;
- normalized `cached_prompt_tokens` extraction from OpenAI-compatible usage;
- durable request, attempt, metadata-trust, and settlement-event records;
- reconciliation preview and Prometheus stale-record metrics.

The diagnostics REST schema is version `1`. It never returns request metadata
values inside an individual request, settlement payloads, prompts, responses, or
tool data. The correlation endpoint echoes only its operator-supplied filter.

The React console does not yet expose correlated request diagnostics. There is
also no supported MCP server for this workflow. Direct PostgreSQL queries are an
emergency investigation technique, not a product interface.

## Proposed architecture

Implement diagnostics once as an application service over Northgate's durable
records, then expose thin adapters:

```text
Northgate diagnostics service
  -> Operator REST API
  -> northgate-inspect CLI
  -> read-only Northgate diagnostics MCP server
```

REST remains the stable service boundary. CLI and MCP must call the same service
or shared typed client and must not duplicate settlement classification rules.

The MCP server is an operator integration. It is distinct from accepting MCP as
a model-provider data-plane protocol and does not change the product boundary in
`current-state.md`.

## MCP tools

The initial MCP server should expose only bounded, read-only tools:

| Tool | Input | Result |
| --- | --- | --- |
| `inspect_correlated_run` | `metadata_key`, `metadata_value`, optional bounded time range | ordered requests, aggregate usage, cache ratio, and findings |
| `inspect_request` | `request_id` | request, attempts, settlement state, and findings |
| `get_provider_attempts` | `request_id` | ordered retry/fallback attempts |
| `find_stale_settlements` | bounded minimum age and limit | stale requests, attempts, leases, and protected/unprotected state |
| `diagnose_prompt_cache` | correlation filter | cached tokens, eligible prompt tokens, missing-detail calls, and exact-cache distinction |

Do not create Dayboard-specific MCP tool names or schemas. `run_id` is one
allowlisted correlation dimension, not a Northgate domain object.

Every result should include a stable schema version and machine-readable
findings. Initial finding codes should cover:

- `REQUEST_STILL_STARTED`;
- `ATTEMPT_STILL_STARTED`;
- `TERMINAL_HTTP_WITHOUT_SETTLEMENT`;
- `USAGE_MISSING`;
- `CACHED_USAGE_MISSING`;
- `PROMPT_CACHE_NOT_HIT`;
- `EXACT_CACHE_BYPASSED`;
- `METADATA_TRUST_MISSING`;
- `RETRY_OR_FALLBACK_USED`;
- `REQUEST_ATTEMPT_TOTAL_MISMATCH`;
- `RECOVERABLE_SETTLEMENT_PENDING`;
- `UNPROTECTED_STALE_SETTLEMENT`;
- `STALE_CONCURRENCY_LEASE`.

Findings report evidence; they must not invent usage or infer provider billing
when usage is absent.

## CLI contract

Provide a thin CLI for humans, CI, recovery environments, and reproducible bug
reports:

```sh
northgate-inspect run <correlation-value> [--metadata-key run_id] [--json]
northgate-inspect request <request-id> [--json]
northgate-inspect stale [--minimum-age 5m] [--limit 100] [--json]
```

`run`, `request`, and `stale` are implemented. `--json` emits the REST response
without changing its versioned shape; human output is a compact summary. Exit
codes are `0` for no findings, `2` for findings present, `3` for authorization
failure, and `4` for configuration, transport, or service failure. The CLI uses
Operator APIs and never connects directly to PostgreSQL or Redis.

Configure it with `NORTHGATE_INSPECT_BASE_URL` and exactly one of
`NORTHGATE_INSPECT_OPERATOR_KEY` or `NORTHGATE_INSPECT_OPERATOR_KEY_FILE`. Key
files must be regular, no larger than 4 KiB, and inaccessible to group and other
users. The raw key is never accepted as a command argument.

## Security and operational constraints

- All diagnostic interfaces require a dedicated read-only operator capability.
  Do not give the MCP process control-plane mutation credentials.
- Read credentials from process configuration or a protected file. Never accept
  them as MCP tool arguments or return them in output.
- Never return prompts, responses, tool payloads, authorization headers,
  application keys, provider credentials, or raw request bodies.
- Bound time ranges, result counts, metadata key/value lengths, and concurrent
  queries. Paginate rather than returning an unbounded incident history.
- Preserve metadata trust classification. Correlation is not authorization.
- Log tool name, result count, duration, and finding codes, but not correlation
  values by default.
- MCP failures must not affect the Northgate data plane. Prefer an independently
  deployable process using the Operator API.

## Production evidence motivating the work

On 2026-07-22, Dayboard Run
`d7079ded-c414-4177-9e8a-b0feb60f103b` produced four correlated Northgate
requests. The first three settled successfully:

| Request role | Prompt | Completion | Total | Cached prompt | Outcome |
| --- | ---: | ---: | ---: | ---: | --- |
| history summarization | 449 | 29 | 478 | 0 | succeeded |
| scheduling model call | 3,337 | 50 | 3,387 | 0 | succeeded |
| post-tool summarization | 464 | 33 | 497 | 0 | succeeded |
| final confirmation | unknown in Northgate | unknown | unknown | unknown | remained `started` |

Dayboard independently observed 2,449 prompt tokens, 14 completion tokens, and
2,463 total tokens for the final confirmation. Northgate logged an HTTP 200 after
approximately 7.4 seconds, but its request and provider-attempt records remained
`started` and no settlement event was created. A matching failure mode has since
been reproduced: direct application-task cancellation could interrupt an
AnyIO-shielded finalizer before its late outbox handoff. The code now hands off
durably before close/cache/health side effects and delays direct cancellation
until finalization completes. Production attribution remains provisional until
the deployed fix is verified.

All four request records contained the expected tenant, user, and run correlation
metadata. The diagnostic API returned `metadata_trust: null`, which also requires
investigation or application-key migration before trust-aware reporting can be
considered complete.

This incident demonstrates both sides of the current state: Northgate already
makes multi-call token and cache behavior substantially easier to locate, but a
diagnostic client must detect gaps in Northgate's own accounting rather than
assuming every ledger record is complete.

## Implementation order

1. Deploy and verify the implemented direct-cancellation fix. Its
   production-shaped regression test covers real PostgreSQL/Redis, ASGI 2.3,
   terminal usage plus `[DONE]`, blocked upstream close, and direct task
   cancellation.
2. Completed on 2026-07-22: add a diagnostics service that joins request,
   attempts, settlement events, and relevant Redis concurrency leases and emits
   stable finding codes.
3. Completed on 2026-07-22: add Operator REST responses for individual requests
   and bounded correlated request sets.
4. Add the independently deployable read-only MCP adapter over the same API as
   the primary coding-agent interface.
5. Completed on 2026-07-22: `northgate-inspect run`, `request`, and `stale` are a
   thin REST client with JSON and human output for operators, CI, and recovery
   environments.
6. Add a correlated-request view to the console after the machine interface is
   stable.

Do not let MCP implementation delay the settlement fix. Do not make the MCP
server query Northgate's database directly as a shortcut.

## Acceptance criteria

- The production evidence above can be represented by a fixture and produces
  `TERMINAL_HTTP_WITHOUT_SETTLEMENT`, `REQUEST_STILL_STARTED`,
  `ATTEMPT_STILL_STARTED`, and `USAGE_MISSING` findings.
- A healthy streamed request reports matching request/attempt totals, terminal
  settlement, and no false findings.
- A retry/fallback request exposes every attempt without double-counting request
  totals.
- `cached_prompt_tokens=0`, missing cached-token details, and exact-cache bypass
  remain three distinct states.
- REST, CLI `--json`, and MCP return the same schema version and finding codes.
- Authorization, range bounds, redaction, pagination, and data-plane isolation
  have automated tests.
- No test or diagnostic output contains prompts, model output, tool payloads, or
  credentials.
