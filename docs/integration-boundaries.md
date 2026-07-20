# Integration boundaries

Status: partially implemented
Last reviewed: 2026-07-17

## Northgate and Dayboard

Dayboard calls Northgate as an AI application. Dayboard owns users, tenants,
sessions, command authorization, scheduling behavior, and product-specific
abuse controls. Northgate owns provider credentials, provider traffic policy,
cross-application usage, cost accounting, and model routing.

Dayboard should send attribution metadata using a project-scoped application
key. Northgate must not query Dayboard tables or treat Dayboard identifiers as
authorization claims.

The model client base URL is the gateway's OpenAI prefix:

```text
OPENAI_BASE_URL=http://northgate:8080/v1/gateways/dayboard/openai
OPENAI_API_KEY=<Northgate application key>
```

Dayboard should attach bounded `Northgate-Metadata` containing `tenant_id`,
`user_id`, and `run_id`. Northgate accepts only dimensions allowed by the
resolved application key. These values provide attribution; they never grant
authorization.

Dayboard commit `3429776` implements this header behind the default-off
`DAYBOARD_NORTHGATE_METADATA_ENABLED` setting. Dayboard commit `b6c0f58` adds a
separate Northgate connection and trusted tenant allowlist, so unmatched tenants
retain the previous provider path. It uses trusted server context and the durable
run ID; browser input, model content, and queue payloads cannot supply or override
the values. North commits `10d2280` and `63ff252` provide the host-controlled
header and per-agent connection boundaries used by Dayboard.

Dayboard's existing `provider_usage_records` may remain temporarily for business
audit and reconciliation. Northgate becomes the authoritative cross-application
provider usage ledger after the integration is verified.

## Northgate and north

`north` is an agent runtime. It owns model invocation abstractions, tool
execution, run events, and runtime usage observation. Northgate is network
infrastructure. It owns traffic admission, provider routing, credentials, and
settled gateway usage.

Neither project depends on the other's internal types. They integrate through
documented provider protocols and request metadata.

## Northgate and platform infrastructure

Northgate may join the shared Docker network and use shared PostgreSQL and Redis
servers, but it must use:

- A dedicated PostgreSQL database and migration history.
- Dedicated Redis database numbers or key prefixes.
- Independent credentials and backup/restore procedures.
- Independent health checks and deployment lifecycle.

Sharing infrastructure does not permit sharing application tables or secrets.

## Initial Dayboard migration

1. Deploy Northgate without changing Dayboard traffic.
2. Configure an upstream provider credential and one Dayboard gateway.
3. Issue a Dayboard application key.
4. Verify streaming and token accounting with non-production requests.
5. Point Dayboard's OpenAI-compatible base URL to Northgate.
6. Compare Dayboard and Northgate usage records during a reconciliation period.
7. Make Northgate analytics authoritative after discrepancies are understood.

Rollback consists of restoring Dayboard's previous provider base URL and key.
Both current production Dayboard tenants select the Northgate connection. The
previous direct provider connection remains configured only as an explicit
rollback path.

Northgate operator diagnostics may correlate Dayboard traffic by authenticated
`run_id` metadata. The generic request diagnostics expose gateway reservation,
actual usage, provider cached prompt usage, exact-cache status, and terminal
outcome. Dayboard-specific interpretation stays in Dayboard documentation and
does not enter Northgate's data model.
