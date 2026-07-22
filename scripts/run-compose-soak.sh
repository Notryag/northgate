#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
project_name="northgate-soak"
compose=(docker compose -p "${project_name}" -f "${repo_root}/docker-compose.soak.yml")
fault_log="$(mktemp)"
stream_pid=""

cleanup() {
    if [[ -n "${stream_pid}" ]] && kill -0 "${stream_pid}" >/dev/null 2>&1; then
        kill "${stream_pid}" >/dev/null 2>&1 || true
    fi
    rm -f "${fault_log}"
    "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

wait_for_worker() {
    for _ in $(seq 1 60); do
        heartbeat_keys="$("${compose[@]}" exec -T redis redis-cli --scan \
            --pattern 'northgate:settlement:worker:heartbeat:*' | tr -d '\r')"
        [[ -n "${heartbeat_keys}" ]] && return 0
        sleep 0.25
    done
    echo "Settlement worker heartbeat did not appear" >&2
    return 1
}

active_lease_count() {
    local total=0
    local key
    while IFS= read -r key; do
        [[ -z "${key}" ]] && continue
        count="$("${compose[@]}" exec -T redis redis-cli ZCARD "${key}" | tr -d '\r')"
        total=$((total + count))
    done < <("${compose[@]}" exec -T redis redis-cli --scan \
        --pattern 'northgate:policy:*:concurrency' | tr -d '\r')
    echo "${total}"
}

"${compose[@]}" build northgate settlement-worker mock-primary mock-fallback
"${compose[@]}" up -d --wait postgres redis mock-primary mock-fallback
"${compose[@]}" run --rm --no-deps northgate alembic upgrade head
"${compose[@]}" up -d settlement-worker
wait_for_worker
"${compose[@]}" up -d --wait northgate

uv run --project "${repo_root}" python "${script_dir}/soak_requests.py" --iterations 5
"${compose[@]}" up -d --force-recreate --wait northgate
uv run --project "${repo_root}" python "${script_dir}/soak_requests.py" --iterations 5

# A live worker is part of readiness whenever durable settlement is enabled.
"${compose[@]}" stop settlement-worker
readiness=""
for _ in $(seq 1 40); do
    readiness="$({ uv run --project "${repo_root}" python -c \
        'import httpx; r=httpx.get("http://127.0.0.1:18082/health/ready"); print(r.status_code, r.json().get("reason", ""))'; } 2>/dev/null)"
    [[ "${readiness}" == "503 settlement_worker_unavailable" ]] && break
    sleep 0.25
done
if [[ "${readiness}" != "503 settlement_worker_unavailable" ]]; then
    echo "Readiness did not reject a missing settlement worker: ${readiness}" >&2
    exit 1
fi

# Admission succeeds while Redis is healthy. Stop Redis only after the active
# lease appears, while the mock provider is holding the stream open.
uv run --project "${repo_root}" python "${script_dir}/soak_requests.py" \
    --iterations 1 --mode stream >"${fault_log}" 2>&1 &
stream_pid=$!
lease_seen=false
for _ in $(seq 1 40); do
    if [[ "$(active_lease_count)" -gt 0 ]]; then
        lease_seen=true
        break
    fi
    sleep 0.1
done
if [[ "${lease_seen}" != "true" ]]; then
    echo "Fault injection stream never acquired a concurrency lease" >&2
    cat "${fault_log}" >&2
    exit 1
fi
"${compose[@]}" stop redis
if ! wait "${stream_pid}"; then
    stream_pid=""
    cat "${fault_log}" >&2
    exit 1
fi
stream_pid=""

retry_count="$(${compose[@]} exec -T postgres psql -U northgate -d northgate -Atc \
    "SELECT count(*) FROM settlement_events WHERE status = 'retry'")"
if [[ "${retry_count}" == "0" ]]; then
    echo "Redis outage did not leave a retryable durable settlement event" >&2
    exit 1
fi

"${compose[@]}" up -d --wait redis
"${compose[@]}" up -d --force-recreate northgate
"${compose[@]}" up -d settlement-worker
wait_for_worker
"${compose[@]}" up -d --wait northgate

for _ in $(seq 1 80); do
    pending_count="$(${compose[@]} exec -T postgres psql -U northgate -d northgate -Atc \
        "SELECT count(*) FROM settlement_events WHERE status IN ('pending', 'processing', 'retry')")"
    [[ "${pending_count}" == "0" ]] && break
    sleep 0.25
done

sleep 1
started_count="$("${compose[@]}" exec -T postgres psql -U northgate -d northgate -Atc \
    "SELECT count(*) FROM request_records WHERE outcome = 'started'")"
fallback_count="$("${compose[@]}" exec -T postgres psql -U northgate -d northgate -Atc \
    "SELECT count(*) FROM provider_attempt_records WHERE provider = 'backup'")"
pending_count="$("${compose[@]}" exec -T postgres psql -U northgate -d northgate -Atc \
    "SELECT count(*) FROM settlement_events WHERE status IN ('pending', 'processing', 'retry')")"
failed_count="$("${compose[@]}" exec -T postgres psql -U northgate -d northgate -Atc \
    "SELECT count(*) FROM settlement_events WHERE status = 'failed'")"

lease_count=0
while IFS= read -r key; do
    [[ -z "${key}" ]] && continue
    count="$("${compose[@]}" exec -T redis redis-cli ZCARD "${key}" | tr -d '\r')"
    lease_count=$((lease_count + count))
done < <("${compose[@]}" exec -T redis redis-cli --scan --pattern 'northgate:policy:*:concurrency' | tr -d '\r')

if [[ "${started_count}" != "0" || "${lease_count}" != "0" || \
      "${fallback_count}" == "0" || "${pending_count}" != "0" || "${failed_count}" != "0" ]]; then
    echo "Soak reconciliation failed: started=${started_count} leases=${lease_count} pending=${pending_count} failed=${failed_count} fallback_attempts=${fallback_count}" >&2
    exit 1
fi

echo "Soak passed: started=0 leases=0 pending=0 failed=0 fallback_attempts=${fallback_count}"
