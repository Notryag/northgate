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
```

`run` correlates requests by trusted `run_id` metadata by default. A different
allowlisted correlation dimension and a bounded time range can be selected:

```sh
uv run northgate-inspect run <correlation-value> \
  --metadata-key tenant_id \
  --start 2026-07-23T00:00:00Z \
  --end 2026-07-24T00:00:00Z \
  --limit 50
```

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

## Verification and troubleshooting

First verify the Operator API and credential with the CLI:

```sh
uv run northgate-inspect stale --minimum-age 5m --limit 1 --json
```

Then verify that the MCP client lists `northgate-diagnostics` and discovers the
five tools above. Common failures are:

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
