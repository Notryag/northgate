# Operator CLI and MCP usage

Status: implemented
Last reviewed: 2026-07-23

This is the practical usage guide for Northgate's read-only diagnostics CLI and
MCP server. The versioned response contract and security invariants remain in
[Operator diagnostics interface](diagnostics-interface.md).

## Prerequisites

Install the locked project environment from the repository root:

```sh
uv sync --locked
```

Both clients call the Northgate Operator API. They do not connect directly to
PostgreSQL or Redis. Configure the API URL and exactly one credential source:

```sh
export NORTHGATE_INSPECT_BASE_URL=http://127.0.0.1:8080
export NORTHGATE_INSPECT_OPERATOR_KEY_FILE=/run/secrets/northgate_operator_key
```

The URL is the address visible from the client process. A Compose host port may
differ from the container's port `8080`. The credential file must be a regular
file, no larger than 4 KiB, and inaccessible to group and other users:

```sh
chmod 600 /run/secrets/northgate_operator_key
```

`NORTHGATE_INSPECT_OPERATOR_KEY` is supported for environments that inject
secrets directly, but a protected file is preferred. Never put the raw key in a
command argument, MCP tool input, repository file, or shell transcript.

`NORTHGATE_INSPECT_TIMEOUT_SECONDS` optionally changes the HTTP timeout from its
30-second default and accepts a value greater than zero and no more than 300.

## CLI

Run the CLI through the repository environment:

```sh
uv run northgate-inspect run <run-id>
uv run northgate-inspect request <request-id>
uv run northgate-inspect stale --minimum-age 5m --limit 100
uv run northgate-inspect doctor
```

`run` correlates requests by allowlisted `run_id` metadata by default. A different
allowlisted correlation dimension and a bounded time range can be selected:

```sh
uv run northgate-inspect run <correlation-value> \
  --metadata-key tenant_id \
  --start 2026-07-23T00:00:00Z \
  --end 2026-07-24T00:00:00Z \
  --limit 50
```

Use `usage` to aggregate one metadata filter and optionally group it by another
metadata dimension. The output reports the selected range, request and group
counts, token and cache totals, outcome, model, latency, attempts, and findings:

```sh
uv run northgate-inspect usage \
  --metadata-key user_id \
  --metadata-value <trusted-user-id> \
  --start '2026-07-23T09:00:00+08:00' \
  --end now \
  --group-by run_id
```

Relative ranges use an explicit timezone and never silently consume the host
timezone:

```sh
uv run northgate-inspect usage \
  --metadata-key user_id \
  --metadata-value <trusted-user-id> \
  --since today@09:00 \
  --timezone Asia/Shanghai \
  --group-by run_id
```

`recent` resolves a Northgate application-key name or ID and lists recent
correlation groups. It does not resolve product usernames or application-domain
objects:

```sh
uv run northgate-inspect recent --application dayboard --since 2h --group-by run_id
```

All aggregates cover at most 100 returned requests. When `has_more=true`, both
human and JSON output state that totals and groups cover only the returned page.
A cache percentage is labelled as a lower bound when provider cache detail is
missing from any included request.

Add `--json` to any command for the versioned machine-readable REST shape. CLI
exit codes are suitable for scripts:

| Code | Meaning |
| ---: | --- |
| `0` | Command completed and no diagnostic finding was present |
| `2` | One or more findings were present |
| `3` | Operator authentication failed |
| `4` | Configuration, transport, or Operator API failure |

Exit code `2` is diagnostic evidence, not a CLI crash. Scripts should preserve
the JSON output before deciding whether a finding is expected or actionable.

## MCP server

`northgate-mcp` is a stdio server launched by an MCP client. Do not start it as
an HTTP service. A manual launch normally waits silently for MCP messages on
stdin:

```sh
uv run northgate-mcp
```

It exposes these bounded, read-only tools:

| Tool | Use |
| --- | --- |
| `inspect_correlated_run` | Inspect ordered requests and aggregate findings for a correlation value |
| `inspect_request` | Inspect one request, its attempts, settlement state, and findings |
| `get_provider_attempts` | Return the ordered retry and fallback attempts for one request |
| `find_stale_settlements` | Find stale ledger records and concurrency leases |
| `diagnose_prompt_cache` | Compare provider prompt-cache evidence for correlated requests |
| `inspect_usage_range` | Aggregate a bounded metadata-filtered range with optional grouping |
| `list_recent_correlations` | Resolve a Northgate application and list recent grouped correlations |

The tools never accept credentials and never return prompts, responses, tool
payloads, request bodies, or provider credentials.

The current operator key is an organization-wide administrative credential; it
is not yet role- or scope-limited. The MCP implementation invokes only the
read-only diagnostics endpoints, but possession of that key still carries the
credential's full authority outside MCP. Run the server locally or in a tightly
controlled operator environment, restrict the key file to the MCP process, and
rotate the key after suspected exposure. A separately scoped diagnostics
credential remains future security work.

## Register with Codex CLI

Use an absolute repository path because the MCP client may start in a different
working directory:

```sh
codex mcp add northgate-diagnostics \
  --env NORTHGATE_INSPECT_BASE_URL=http://127.0.0.1:8080 \
  --env NORTHGATE_INSPECT_OPERATOR_KEY_FILE=/run/secrets/northgate_operator_key \
  -- uv --directory /absolute/path/to/northgate run northgate-mcp
```

Inspect or remove the registration with:

```sh
codex mcp list
codex mcp get northgate-diagnostics --json
codex mcp remove northgate-diagnostics
```

Start a new Codex session after adding the server so tool discovery runs with the
new configuration. The coding agent can then be asked, for example, to inspect a
Northgate request ID, diagnose cache behavior for a run ID, or check for stale
settlements. MCP remains read-only; recovery mutations still require the
explicit operational CLI and procedures documented elsewhere.

## Other stdio MCP clients

Clients that use the common `mcpServers` JSON shape can launch the same process:

```json
{
  "mcpServers": {
    "northgate-diagnostics": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/northgate",
        "run",
        "northgate-mcp"
      ],
      "env": {
        "NORTHGATE_INSPECT_BASE_URL": "http://127.0.0.1:8080",
        "NORTHGATE_INSPECT_OPERATOR_KEY_FILE": "/run/secrets/northgate_operator_key"
      }
    }
  }
}
```

The location and exact outer configuration shape are client-specific. Keep the
command, arguments, and environment contract shown above when translating it.
Streamable HTTP is intentionally unsupported until Northgate has an explicit
OAuth or token-verification deployment contract.

## Provision a production client

The server stores only `NORTHGATE_OPERATOR_KEY_SHA256`; a digest cannot be used
to reconstruct a CLI credential. Retain the one-time raw operator key in the
deployment secret store. Provision a host client from a protected key file with:

```sh
scripts/provision-inspect-client.sh \
  --base-url http://127.0.0.1:8082 \
  --source-key-file /run/secrets/northgate_operator_key_source \
  --config-dir /var/lib/northgate-inspect \
  --expected-sha256 "$NORTHGATE_OPERATOR_KEY_SHA256"

set -a
source /var/lib/northgate-inspect/inspect.env
set +a
uv run northgate-inspect doctor
```

The script accepts no raw key argument, validates the source file, optionally
verifies its digest, writes the copied key and environment file with mode `0600`,
and refuses to overwrite an existing client unless `--force` is explicitly used
for rotation. If the raw credential was not retained, rotate the operator key.

## Verification and troubleshooting

First verify the Operator API and credential with the CLI:

```sh
uv run northgate-inspect stale --minimum-age 5m --limit 1 --json
```

Then verify that the MCP client lists `northgate-diagnostics` and discovers the
seven tools above. Common failures are:

| Symptom | Check |
| --- | --- |
| Missing `NORTHGATE_INSPECT_BASE_URL` | Set the URL in the MCP server environment, not only in an unrelated shell |
| Operator authentication failed | Confirm the key is an operator key and the file contains no extra value |
| Credential file rejected | Use a regular file of at most 4 KiB and mode `0600` |
| Operator API unreachable | Use the address visible from the MCP process; container and host addresses differ |
| Server appears to hang when launched manually | This is normal for stdio; the MCP client must own the process |
| Tools absent after registration | Restart the MCP client session and inspect its registered server list |

Do not enable MCP in Northgate readiness or the request data path. A diagnostics
client failure must not affect gateway traffic.
