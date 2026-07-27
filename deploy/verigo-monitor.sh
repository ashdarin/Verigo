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
health_payload=
if ! health_payload=$(curl -fsS --max-time 12 https://verigo.site/api/health); then
    issues+=("public health endpoint is unavailable")
else
    read -r health_status service_mode queued pending verifying stale unhealthy < <(
        HEALTH_PAYLOAD="$health_payload" /opt/verigo/.venv/bin/python - <<'PY'
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

if ! PYTHONPATH=/opt/verigo/current /opt/verigo/.venv/bin/python - <<'PY'
from pathlib import Path

from app.db.sqlite import begin_immediate, connect

with connect(Path('/opt/verigo/data/verigo.db')) as connection:
    begin_immediate(connection)
    connection.rollback()
PY
then
    issues+=("database is not writable")
fi

disk_used=$(df -P / | awk 'NR==2 {gsub("%", "", $5); print $5}')
if (( disk_used >= disk_limit )); then
    issues+=("disk usage is ${disk_used}%")
fi

backup_success=/var/lib/verigo-backup/last-success
if [[ ! -f "$backup_success" ]] || (( $(date +%s) - $(stat -c %Y "$backup_success") > backup_max_age_hours * 3600 )); then
    issues+=("latest completed backup is older than ${backup_max_age_hours} hours")
fi

if [[ -n "$health_payload" ]] && (( queued >= queue_limit )); then
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
if [[ -r "$state_file" ]]; then
    # shellcheck disable=SC1090
    source "$state_file"
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
    printf 'previous_status=%q\nprevious_sent=%q\n' "$status" "$now" > "$state_file"
fi
