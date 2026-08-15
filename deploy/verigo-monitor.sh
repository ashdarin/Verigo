#!/usr/bin/env bash
set -Eeuo pipefail

config=/etc/verigo/monitor.env
state_dir=/var/lib/verigo-monitor
state_file="$state_dir/state"
if [[ -r "$config" ]]; then
    # shellcheck disable=SC1090
    source "$config"
fi

repeat_minutes=${VERIGO_ALERT_REPEAT_MINUTES:-360}
disk_limit=${VERIGO_MONITOR_DISK_PERCENT:-85}
backup_max_age_hours=${VERIGO_MONITOR_BACKUP_MAX_AGE_HOURS:-27}
queue_limit=${VERIGO_MONITOR_QUEUE_LIMIT:-10}
provider_pressure_limit=${VERIGO_MONITOR_PROVIDER_PRESSURE_LAST_60_SECONDS:-1}
public_base_url=${VERIGO_MONITOR_PUBLIC_BASE_URL:-https://verigo.site}
readiness_url=${VERIGO_MONITOR_READINESS_URL:-http://127.0.0.1:18000/api/internal/readiness}
postgres_tunnel_port=${VERIGO_MONITOR_POSTGRES_TUNNEL_PORT:-15433}
postgres_tunnel_unit=${VERIGO_MONITOR_POSTGRES_TUNNEL_UNIT:-verigo-postgres-worker-tunnel}
check_local_backup=${VERIGO_MONITOR_CHECK_LOCAL_BACKUP:-0}
asset_marker=${VERIGO_MONITOR_ASSET_MARKER:-risk-signals-v3}
database_env_file=${VERIGO_MONITOR_DATABASE_ENV_FILE:-/etc/verigo/verigo-worker.env}
mkdir -p "$state_dir"

issues=()
public_health_payload=
readiness_payload=
# Local PostgreSQL tunnel bind only. Never print DSN / DATABASE_URL / env.
tunnel_down=0
if ! ss -lnt 2>/dev/null | awk -v port="$postgres_tunnel_port" '$1 ~ /LISTEN/ && $4 == "127.0.0.1:" port { found=1 } END { exit !found }'; then
    logger -t verigo-monitor -- "postgres ssh tunnel is not listening on 127.0.0.1:${postgres_tunnel_port}"
    issues+=("postgres ssh tunnel is not listening on 127.0.0.1:${postgres_tunnel_port}")
    tunnel_down=1
fi

if ! public_health_payload=$(curl -fsS --max-time 12 "${public_base_url}/api/health"); then
    issues+=("public health endpoint is unavailable")
else
    monitor_token=$(sed -n 's/^VERIGO_MONITOR_TOKEN=//p' /etc/verigo/verigo.env | tail -n 1)
    if [[ -z "$monitor_token" ]]; then
        issues+=("monitor token is not configured")
    elif ! readiness_payload=$(curl -fsS --max-time 12 \
        -H "X-Verigo-Monitor-Token: ${monitor_token}" \
        "$readiness_url"); then
        issues+=("internal readiness endpoint is unavailable")
    fi
fi

# These probes only validate public routing and asset publication. They never
# create an account, submit a job, or change user-visible analytics.
if ! verify_page=$(curl -fsS --max-time 12 "${public_base_url}/verify"); then
    issues+=("verification workspace is unavailable")
elif [[ -n "$asset_marker" ]] && ! grep -Fq "$asset_marker" <<<"$verify_page"; then
    issues+=("verification workspace is serving an unexpected asset version")
fi

auth_status=$(curl -sS --max-time 12 -o /dev/null -w '%{http_code}' "${public_base_url}/api/auth/me" || true)
if [[ "$auth_status" != "200" ]]; then
    issues+=("anonymous session endpoint returned HTTP ${auth_status:-000}")
fi
result_status=$(curl -sS --max-time 12 -o /dev/null -w '%{http_code}' \
    "${public_base_url}/api/jobs/verigo-monitor-probe/results" || true)
if [[ "$result_status" != "404" ]]; then
    issues+=("result route probe returned HTTP ${result_status:-000}")
fi

if [[ -n "$readiness_payload" ]]; then
    read -r health_status service_mode queued pending verifying stale unhealthy provider_pressure < <(
        HEALTH_PAYLOAD="$readiness_payload" PROVIDER_PRESSURE_LIMIT="$provider_pressure_limit" /opt/verigo/.venv/bin/python - <<'PY'
import json
import os

payload = json.loads(os.environ['HEALTH_PAYLOAD'])
pressure_limit = max(1, int(os.environ['PROVIDER_PRESSURE_LIMIT']))
provider_pressure = []
for provider, runtime in payload.get("scheduler_runtime", {}).items():
    if not isinstance(runtime, dict):
        continue
    pressure = int(runtime.get("pressure_last_60_seconds", 0) or 0)
    current = int(runtime.get("limit", 0) or 0)
    configured = int(runtime.get("configured_limit", 0) or 0)
    if pressure >= pressure_limit and current < configured:
        provider_pressure.append(f"{provider}:{pressure}/60s@{current}/{configured}")
print(
    payload.get('status', 'unknown'),
    payload.get('service_mode', 'unknown'),
    payload.get('queued_jobs', 0),
    payload.get('pending_results', 0),
    payload.get('verifying_results', 0),
    payload.get('stale_leases', 0),
    ','.join(payload.get('unhealthy_targets', [])) or '-',
    ','.join(provider_pressure) or '-',
)
PY
    )
    if [[ "$health_status" != "ok" ]]; then
        issues+=("application health: ${health_status}")
    fi
    if [[ "$service_mode" != "active" ]]; then
        issues+=("verification service mode: ${service_mode}")
    fi
    if (( stale > 0 )); then
        issues+=("stale worker leases: ${stale}")
    fi
    if [[ "$unhealthy" != "-" ]]; then
        issues+=("remote targets without healthy nodes: ${unhealthy}")
    fi
    if [[ "$provider_pressure" != "-" ]]; then
        issues+=("provider pressure and reduced concurrency: ${provider_pressure}")
    fi
fi

if [[ ! -r "$database_env_file" ]]; then
    issues+=("database environment file is unavailable")
elif ! (
    set -a
    source "$database_env_file"
    set +a
    PYTHONPATH=/opt/verigo/current /opt/verigo/.venv/bin/python - <<'PY'
from app.db.backend_ops import database_write_probe, postgres_enabled

ok = database_write_probe()
print("backend=postgres" if postgres_enabled() else "backend=sqlite")
raise SystemExit(0 if ok else 1)
PY
); then
    issues+=("database is not writable")
fi

if ! systemctl is-active --quiet "$postgres_tunnel_unit"; then
    issues+=("${postgres_tunnel_unit} unit is not active")
fi

disk_used=$(df -P / | awk 'NR==2 {gsub("%", "", $5); print $5}')
if (( disk_used >= disk_limit )); then
    issues+=("disk usage is ${disk_used}%")
fi

if [[ "$check_local_backup" == "1" ]]; then
    backup_success=/var/lib/verigo-backup/last-success
    if [[ ! -f "$backup_success" ]] || (( $(date +%s) - $(stat -c %Y "$backup_success") > backup_max_age_hours * 3600 )); then
        issues+=("latest completed backup is older than ${backup_max_age_hours} hours")
    fi
fi

if [[ -n "$readiness_payload" ]] && (( queued >= queue_limit )); then
    issues+=("queued jobs: ${queued}")
fi

status=ok
message="Verigo monitor: all checks passed"
if ((${#issues[@]})); then
    status=alert
    message="Verigo monitor alert: ${issues[*]}"
fi

previous_status=
previous_sent=0
tunnel_down_count=0
if [[ -r "$state_file" ]]; then
    # shellcheck disable=SC1090
    source "$state_file"
fi
if (( tunnel_down )); then
    tunnel_down_count=$((tunnel_down_count + 1))
else
    tunnel_down_count=0
fi
now=$(date +%s)
should_send=false
if [[ "$status" != "$previous_status" ]] || (( now - previous_sent >= repeat_minutes * 60 )); then
    should_send=true
fi

if [[ "$should_send" == true ]]; then
    logger -t verigo-monitor -- "$message"
    if [[ -n "${VERIGO_ALERT_WEBHOOK_URL:-}" ]]; then
        payload=$(MESSAGE="$message" /opt/verigo/.venv/bin/python - <<'PY'
import json
import os
print(json.dumps({"text": os.environ["MESSAGE"]}))
PY
)
        curl -fsS --max-time 12 -H 'Content-Type: application/json' \
            --data "$payload" "$VERIGO_ALERT_WEBHOOK_URL" >/dev/null || true
    fi
    previous_status=$status
    previous_sent=$now
fi
printf 'previous_status=%q\nprevious_sent=%q\ntunnel_down_count=%q\n' \
    "$previous_status" "$previous_sent" "$tunnel_down_count" > "$state_file"

if (( tunnel_down )); then
    exit 1
fi
