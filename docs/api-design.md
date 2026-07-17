# API design

Status: partially implemented  
Last reviewed: 2026-07-17

The OpenAI chat-completions path and initial database-backed control plane are
implemented. Other provider-native paths remain design proposals.

## Protocol strategy

Northgate should support provider-native protocols and an OpenAI-compatible
entry point. Compatibility means preserving documented request, response, and
stream behavior; it does not mean forcing every provider feature into one
lowest-common-denominator schema.

The first implementation target is OpenAI-compatible chat completions and
responses because Dayboard currently uses an OpenAI-compatible model client.

## Proposed data-plane paths

```text
POST /v1/gateways/{gateway_slug}/openai/chat/completions
POST /v1/gateways/{gateway_slug}/openai/responses
POST /v1/gateways/{gateway_slug}/anthropic/messages
POST /v1/gateways/{gateway_slug}/gemini/{provider_path...}
```

The OpenAI chat-completions path currently forwards streaming and non-streaming
responses through one configured OpenAI-compatible upstream. Other paths are
reserved design direction, not MVP commitments.

## Authentication

Applications send a Northgate application key:

```http
Authorization: Bearer ng_live_example
```

The application key resolves the organization and project. A gateway must
belong to that project. Upstream provider credentials are selected by the
accepted route and are never accepted from an untrusted passthrough header by
default.

Implemented behavior: configuration routing compares a configured SHA-256 key
digest. Database routing resolves the digest to a project, verifies gateway
ownership, filters enabled routes by accepted metadata, and orders them by
priority, metadata specificity, and deterministic weight selection. Provider
credentials are decrypted in memory. Client authorization headers are never
forwarded upstream.

## Request metadata

Applications may attach business attribution such as user, tenant, environment,
or run IDs. Metadata affects analytics and policy only when the application key
is permitted to set the relevant dimensions.

The initial transport should use one bounded JSON header:

```http
Northgate-Metadata: {"tenant_id":"...","user_id":"...","run_id":"..."}
```

Limits:

- Maximum encoded size: 8 KiB.
- Keys and string values have explicit length limits.
- Reserved keys use the `northgate.` prefix.
- Metadata is never forwarded upstream unless a route explicitly maps it.
- Route metadata rules use exact string matches and never grant gateway access.

## Streaming

- Preserve provider event order and payloads.
- Do not buffer the complete response before sending it to the client.
- Emit Northgate request and rate-limit headers before the stream begins.
- Treat disconnect, provider error, and malformed stream as distinct outcomes.
- Settle usage reported in a terminal event even when the client disconnects.

The current implementation forwards chunks without whole-response buffering,
closes the upstream response on client disconnect, and extracts reported usage
from JSON responses and terminal SSE events. Durable settlement is enabled with
`NORTHGATE_USAGE_PERSISTENCE_ENABLED`.

## Response headers

Proposed headers:

```text
Northgate-Request-Id
Northgate-Route
Northgate-Provider
Northgate-RateLimit-Limit
Northgate-RateLimit-Remaining
Northgate-RateLimit-Reset
Northgate-ConcurrencyLimit-Remaining
Northgate-TokenLimit-Remaining
Northgate-DailySpendLimit-Remaining-MicroUSD
Northgate-MonthlySpendLimit-Remaining-MicroUSD
```

Headers must not expose provider credential IDs or internal error details.

## Gateway errors

Northgate-owned failures use a stable envelope and an HTTP status appropriate
to the failure. Provider-native errors should remain recognizable while adding
the Northgate request ID.

```json
{
  "error": {
    "code": "RATE_LIMIT_EXCEEDED",
    "message": "Request limit exceeded",
    "request_id": "req_...",
    "retryable": true
  }
}
```

Initial error codes should distinguish invalid key, forbidden gateway, invalid
metadata, request limit, token limit, spend limit, concurrency limit, route not
found, provider timeout, provider unavailable, and internal failure.

Implemented policy errors use `REQUEST_LIMIT_EXCEEDED`,
`CONCURRENCY_LIMIT_EXCEEDED`, `TOKEN_LIMIT_EXCEEDED`, and
`POLICY_UNAVAILABLE`. Redis admission checks all configured limits atomically;
no reservation is written unless every check succeeds.

Operator usage APIs are implemented at `/api/v1/usage/summary` and
`/api/v1/usage/timeseries`. They require a dedicated operator key and support a
maximum 90-day range with optional project and gateway filters. Application
keys are not control-plane credentials.

`/api/v1/usage/requests/{request_id}/attempts` exposes the ordered provider
attempt ledger to operators. Retry and fallback happen only before downstream
response headers are sent; mid-stream failures terminate the stream.

## Implemented control-plane resources

```text
/api/v1/organizations
/api/v1/projects
/api/v1/application-keys
/api/v1/provider-credentials
/api/v1/gateways
/api/v1/routes
/api/v1/usage
```

All endpoints require the dedicated operator bearer key. List endpoints accept
an optional parent resource ID as a query filter. Create endpoints accept JSON:

```text
POST /api/v1/organizations             {name}
POST /api/v1/projects                  {organization_id, name}
POST /api/v1/gateways                  {project_id, slug}
POST /api/v1/application-keys          {project_id, name, allowed_metadata_keys}
POST /api/v1/provider-credentials      {project_id, name, provider, base_url,
                                        adapter, adapter_config, api_key}
POST /api/v1/routes                    {gateway_id, provider_credential_id, name,
                                        priority, weight, match_metadata, ...}
```

Application key creation returns the plaintext `key` exactly once; list responses
never expose its digest. Provider credential create and secret-rotation responses
never return plaintext or ciphertext. `POST /application-keys/{id}/revoke`,
`PUT /provider-credentials/{id}/secret`, and `PATCH /routes/{id}` provide the
minimum lifecycle operations needed for integration, credential rotation, traffic
weight changes, and rollback. A route cannot join a gateway and provider credential
from different projects.

Policy management and audit-event APIs remain proposed. The initial operator key
has organization-wide authority; fine-grained users and roles remain a later
control-plane decision.
