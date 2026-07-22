# Existing-system integration guide

Status: implemented workflow  
Last reviewed: 2026-07-17

This workflow moves an existing OpenAI-compatible application to Northgate while
keeping rollback independent from Northgate itself.

## Prepare

1. Create the project, gateway, application key, provider credentials, routes,
   and gateway policy through the operator control API.
2. Add an effective-dated model price for every exact provider/model pair before
   enabling spend limits or treating cost analytics as complete.
3. Keep the application's previous provider base URL and credential available in
   its secret manager for the reconciliation period. Do not copy that credential
   into source code or traffic metadata.
4. Confirm Northgate readiness and record the application key ID, gateway ID,
   route IDs, previous base URL, and rollback owner.
5. Use a non-production model or account when possible. Compatibility checks make
   real provider requests and may incur cost.

For applications on the shared platform Docker network, use the platform Compose
override while retaining Northgate's private PostgreSQL and Redis network:

```sh
export COMPOSE_FILE=docker-compose.yml:docker-compose.platform.yml
docker compose config --quiet
docker compose build northgate
docker compose run --rm --no-deps northgate alembic upgrade head
docker compose up -d --wait
```

The Northgate application joins both networks and is reachable as
`http://northgate:8080` from platform containers. Its PostgreSQL and Redis services
remain only on the private `northgate-dev` network. The override binds host access
to `127.0.0.1:8081` by default to avoid the platform's existing port 8080 service.
Set `PLATFORM_INFRA_NETWORK` only when the external network uses a different name.

Keep `COMPOSE_FILE` set when running Northgate backup, restore, or upgrade scripts;
Docker Compose will apply the same deployment topology.

## Verify the protocol

Set the gateway's OpenAI prefix, not the final chat-completions path:

```sh
export NORTHGATE_VERIFY_BASE_URL=http://northgate:8080/v1/gateways/example/openai
export NORTHGATE_VERIFY_APPLICATION_KEY=<Northgate application key>
export NORTHGATE_VERIFY_MODEL=<model or Azure deployment>
uv run northgate-verify
```

The command verifies non-streaming JSON, SSE termination and first-event timing,
and a forced tool call. It reads secrets only from the environment and prints no
request content, response content, or credentials. Set
`NORTHGATE_VERIFY_TOOL_CALLS=false` only when the selected model does not support
tools. `NORTHGATE_VERIFY_TIMEOUT_SECONDS` defaults to 60 seconds.

## Canary traffic

Application-level canary controls how much business traffic enters Northgate.
Run a bounded application instance, worker group, tenant allowlist, or deployment
percentage with:

```text
OPENAI_BASE_URL=http://northgate:8080/v1/gateways/example/openai
OPENAI_API_KEY=<Northgate application key>
```

Northgate route weights control how traffic already inside Northgate is divided
between eligible upstream routes. They do not move traffic from the application's
old direct provider path. Weight ratios converge over enough requests and are not
an exact percentage guarantee for a small sample.

Start with non-production traffic, then a bounded production group. Increase the
share only after streaming, tool execution, provider errors, latency, usage, and
cost reconcile for the previous stage. Keep request content logging disabled.

## Observe and reconcile

- Use `/api/v1/usage/summary` for final request outcomes.
- Use `/api/v1/usage/tenants` for authenticated tenant attribution and reconciliation.
- Use `/api/v1/usage/routes` for actual upstream load, retry, and fallback share.
- Use `/api/v1/usage/requests/{request_id}/attempts` for ambiguous or costly requests.
- Compare provider invoices or usage exports with Northgate attempt tokens and cost.
- A successful final request may contain failed or billable earlier attempts.

Preview stale `started` records and expired concurrency leases without changing
state:

```sh
uv run northgate-reconcile --older-than-seconds 900
```

After confirming no legitimate request has been running longer than the selected
threshold, apply recovery explicitly:

```sh
uv run northgate-reconcile --older-than-seconds 900 --apply
```

The command removes expired leases and leases owned by stale requests, then marks
stale request and attempt records as `settlement_incomplete`. It does not fabricate
token or cost values. Re-running it is safe. Use a threshold longer than the
maximum expected provider request and settlement duration.

Migration `0012` creates the durable settlement outbox, `0013` adds multi-event
keys, and `0016` versions payloads and indexes the recoverable queue. Apply the
single current Alembic head before starting the current worker. The worker can
drain currently available events once and exit:

```sh
uv run northgate-worker --once
```

or run continuously with a bounded poll interval:

```sh
uv run northgate-worker --poll-seconds 0.25
```

Run completed-event retention from a scheduler appropriate to the deployment:

```sh
uv run northgate-worker --cleanup-completed --retention-days 30 --cleanup-batch-size 1000
```

Repeat the bounded cleanup command until it reports `deleted_events=0` when a
large historical backlog must be removed. It never deletes retryable or failed
events.

Enable the guarded provider-response handoff only after migration `0016`, the
worker, metrics scraping, and alert rules are active:

```sh
export NORTHGATE_USAGE_PERSISTENCE_ENABLED=true
export NORTHGATE_SETTLEMENT_OUTBOX_ENABLED=true
docker compose --profile settlement-worker up -d
```

The feature remains default-off. It covers terminal settlement after a provider
response enters the relay path, cache hits, and final provider-unavailable/timeout
responses. Revision `0013` provides multiple event keys per request, so timeout,
transport-error, and retryable-status attempts are durable independently of the
terminal request event. `northgate-reconcile` remains the recovery backstop for
records created before an outbox event can be written.

When enabled, `/health/ready` reports `ready` with `degraded: true` if no worker
heartbeat is present but the recoverable backlog is empty or still within
`NORTHGATE_SETTLEMENT_READINESS_MAX_PENDING_AGE_SECONDS`. It returns `503` once
the oldest recoverable event exceeds that threshold. Worker heartbeat and
backlog alerts remain required even during the grace period.

## Roll back

For one unhealthy upstream, disable its route through `PATCH /api/v1/routes/{id}`;
healthy fallback routes continue to serve Northgate traffic. Do not set a route
weight to zero because zero is not a valid routing weight.

For a Northgate-wide problem, restore the application's previous provider base URL
and provider credential, then restart or reload only the canary application group.
This rollback must not depend on the Northgate control API being available. Stop
increasing traffic until delayed attempt settlement and provider reconciliation are
complete.

Revoke the Northgate application key and remove the retained direct-provider secret
only after the reconciliation window closes and Northgate is the accepted path.
