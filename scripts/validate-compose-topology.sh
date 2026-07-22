#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
platform_network="${PLATFORM_INFRA_NETWORK:-platform-infra}"
compose=(docker compose -f "${repo_root}/docker-compose.yml" -f "${repo_root}/docker-compose.platform.yml")
case "${NORTHGATE_SETTLEMENT_OUTBOX_ENABLED:-false}" in
    1 | [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss] | [Oo][Nn]) outbox_enabled=true ;;
    *) outbox_enabled=false ;;
esac
if [[ "${outbox_enabled}" == "true" ]]; then
    compose+=(--profile settlement-worker)
fi

config_json="$("${compose[@]}" config --format json)"
python3 -c '
import json
import sys

config = json.load(sys.stdin)
networks = config.get("services", {}).get("northgate", {}).get("networks", {})
if isinstance(networks, list):
    names = set(networks)
elif isinstance(networks, dict):
    names = set(networks)
else:
    names = set()
required = {"default", "platform-infra"}
missing = sorted(required - names)
if missing:
    raise SystemExit("northgate is missing required Compose networks: " + ", ".join(missing))
' <<<"${config_json}"

if [[ "${outbox_enabled}" == "true" ]]; then
    python3 -c '
import json
import sys

config = json.load(sys.stdin)
services = config.get("services", {})
worker = services.get("settlement-worker")
if not isinstance(worker, dict):
    raise SystemExit(
        "NORTHGATE_SETTLEMENT_OUTBOX_ENABLED requires the settlement-worker Compose service"
    )
healthcheck = worker.get("healthcheck")
if not isinstance(healthcheck, dict) or not healthcheck.get("test"):
    raise SystemExit(
        "NORTHGATE_SETTLEMENT_OUTBOX_ENABLED requires a settlement-worker healthcheck"
    )
' <<<"${config_json}"
fi

if ! docker network inspect "${platform_network}" >/dev/null 2>&1; then
    echo "Required external Docker network does not exist: ${platform_network}" >&2
    exit 1
fi

echo "Compose topology valid: northgate joins private and ${platform_network} networks."
