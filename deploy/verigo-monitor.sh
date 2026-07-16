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
if ! curl -fsS --max-time 12 https://verigo.site/api/health >/dev/null; then
    issues+=("public health endpoint is unavailable")
fi

disk_used=$(df -P / | awk 'NR==2 {gsub("%", "", $5); print $5}')
if (( disk_used >= disk_limit )); then
    issues+=("disk usage is ${disk_used}%")
fi

latest_backup=$(find /var/backups/verigo -mindepth 1 -maxdepth 1 -type d -name '20*' -printf '%T@\n' 2>/dev/null | sort -nr | head -n 1)
if [[ -z "$latest_backup" ]] || (( $(date +%s) - ${latest_backup%.*} > backup_max_age_hours * 3600 )); then
    issues+=("latest backup is older than ${backup_max_age_hours} hours")
fi

queued=$(/opt/verigo/.venv/bin/python - <<'PY'
import sqlite3
connection = sqlite3.connect('/opt/verigo/data/verigo.db')
print(connection.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0])
PY
)
if (( queued >= queue_limit )); then
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
