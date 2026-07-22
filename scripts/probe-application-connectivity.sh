#!/usr/bin/env bash
set -euo pipefail

application_container="${NORTHGATE_APPLICATION_PROBE_CONTAINER:?Set NORTHGATE_APPLICATION_PROBE_CONTAINER}"
probe_url="${NORTHGATE_APPLICATION_PROBE_URL:-http://northgate:8080/health/ready}"

docker exec "${application_container}" python -c '
import json
import sys
import urllib.request

url = sys.argv[1]
with urllib.request.urlopen(url, timeout=10) as response:
    payload = json.load(response)
    if response.status != 200 or payload != {"status": "ready"}:
        raise SystemExit(f"unexpected Northgate readiness response: {response.status} {payload!r}")
' "${probe_url}"

echo "Application connectivity valid: ${application_container} reached ${probe_url}."
