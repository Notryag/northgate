# Existing-system integration guide

Status: implemented workflow  
Last reviewed: 2026-07-17

This workflow moves an existing OpenAI-compatible application to Northgate while
keeping rollback independent from Northgate itself.

## Prepare

1. Create the project, gateway, application key, provider credentials, routes,
   and gateway policy through the operator control API.
2. Keep the application's previous provider base URL and credential available in
   its secret manager for the reconciliation period. Do not copy that credential
   into source code or traffic metadata.
3. Confirm Northgate readiness and record the application key ID, gateway ID,
   route IDs, previous base URL, and rollback owner.
4. Use a non-production model or account when possible. Compatibility checks make
   real provider requests and may incur cost.

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
- Use `/api/v1/usage/routes` for actual upstream load, retry, and fallback share.
- Use `/api/v1/usage/requests/{request_id}/attempts` for ambiguous or costly requests.
- Compare provider invoices or usage exports with Northgate attempt tokens and cost.
- A successful final request may contain failed or billable earlier attempts.

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
