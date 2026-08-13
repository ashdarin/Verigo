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
mkdir -p "$state_dir"

issues=()
public_health_payload=
readiness_payload=
# Local PostgreSQL tunnel bind only. Never print DSN / DATABASE_URL / env.
tunnel_down=0
if ! ss -lnt 2>/dev/null | awk '$1 ~ /LISTEN/ && $4 == "127.0.0.1:15432" { found=1 } END { exit !found }'; then
    logger -t verigo-monitor -- "postgres ssh tunnel is not listening on 127.0.0.1:15432"
    issues+=("postgres ssh tunnel is not listening on 127.0.0.1:15432")
    tunnel_down=1
fi

if ! public_health_payload=$(curl -fsS --max-time 12 https://verigo.site/api/health); then
    issues+=("public health endpoint is unavailable")
else
    monitor_token=$(sed -n 's/^VERIGO_MONITOR_TOKEN=//p' /etc/verigo/verigo.env | tail -n 1)
    if [[ -z "$monitor_token" ]]; then
        issues+=("monitor token is not configured")
    elif ! readiness_payload=$(curl -fsS --max-time 12 \
        -H "X-Verigo-Monitor-Token: ${monitor_token}" \
        http://127.0.0.1:8000/api/internal/readiness); then
        issues+=("internal readiness endpoint is unavailable")
    fi
fi

if [[ -n "$readiness_payload" ]]; then
    read -r health_status service_mode queued pending verifying stale unhealthy < <(
        HEALTH_PAYLOAD="$readiness_payload" /opt/verigo/.venv/bin/python - <<'PY'
import json
import os

payload = json.loads(os.environ['HEALTH_PAYLOAD'])
print(
    payload.get('status', 'unknown'),
    payload.get('service_mode', 'unknown'),
    payload.get('queued_jobs', 0),
    payload.get('pending_results', 0),
    payload.get('verifying_results', 0),
    payload.get('stale_leases', 0),
    ','.join(payload.get('unhealthy_targets', [])) or '-',
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
fi

if ! set -a; source /etc/verigo/verigo.env; set +a; PYTHONPATH=/opt/verigo/current /opt/verigo/.venv/bin/python - <<'PY'
from app.db.backend_ops import database_write_probe, postgres_enabled

ok = database_write_probe()
print("backend=postgres" if postgres_enabled() else "backend=sqlite")
raise SystemExit(0 if ok else 1)
PY
then
    issues+=("database is not writable")
fi

if ! systemctl is-active --quiet verigo-postgres-tunnel; then
    issues+=("verigo-postgres-tunnel unit is not active")
fi

disk_used=$(df -P / | awk 'NR==2 {gsub("%", "", $5); print $5}')
if (( disk_used >= disk_limit )); then
    issues+=("disk usage is ${disk_used}%")
fi

backup_success=/var/lib/verigo-backup/last-success
if [[ ! -f "$backup_success" ]] || (( $(date +%s) - $(stat -c %Y "$backup_success") > backup_max_age_hours * 3600 )); then
    issues+=("latest completed backup is older than ${backup_max_age_hours} hours")
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
