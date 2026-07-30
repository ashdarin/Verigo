#!/usr/bin/env bash
set -Eeuo pipefail

release_dir=${VERIGO_RELEASE_DIR:-/tmp/verigo-release}
state_dir=/opt/verigo
releases_dir="$state_dir/releases"
current_link="$state_dir/current"
release_version_file="$release_dir/.verigo-release"

test -f "$release_dir/app/main.py"
test -f "$release_dir/requirements.txt"
test -f "$release_dir/验证8.py"
test -f "$release_version_file"
release_version=$(tr -d '\r\n' < "$release_version_file")
if [[ ! "$release_version" =~ ^[0-9a-f]{7,40}$ ]]; then
    echo "Release version must be a Git commit hash" >&2
    exit 1
fi

activate_release() {
    local target=$1
    local pending_link="$state_dir/.current-${release_version:0:12}-$$"
    ln -s "$target" "$pending_link"
    mv -Tf "$pending_link" "$current_link"
}

ensure_legacy_release() {
    if [[ -L "$current_link" ]]; then
        return
    fi
    local legacy_version
    legacy_version=$(tr -d '\r\n' < "$state_dir/RELEASE_VERSION" 2>/dev/null || true)
    [[ "$legacy_version" =~ ^[0-9a-f]{7,40}$ ]] || legacy_version="legacy-$(date -u +%Y%m%dT%H%M%SZ)"
    local legacy_release="$releases_dir/$legacy_version"
    if [[ ! -d "$legacy_release" ]]; then
        local incoming
        incoming=$(mktemp -d "$releases_dir/.incoming-legacy.XXXXXX")
        rsync -a --delete --exclude='__pycache__' "$state_dir/app/" "$incoming/app/"
        rsync -a --delete "$state_dir/static/" "$incoming/static/"
        rsync -a --delete "$state_dir/deploy/" "$incoming/deploy/"
        cp "$state_dir/requirements.txt" "$incoming/requirements.txt"
        cp "$state_dir/验证8.py" "$incoming/验证8.py"
        printf '%s\n' "$legacy_version" > "$incoming/RELEASE_VERSION"
        chown -R verigo:verigo "$incoming"
        mv "$incoming" "$legacy_release"
    fi
    activate_release "$legacy_release"
}

rollback() {
    local status=$?
    trap - ERR
    if [[ -n "${previous_release:-}" ]]; then
        echo "Release failed; switching back to $previous_release" >&2
        activate_release "$previous_release"
        systemctl restart verigo || true
        systemctl restart verigo-supervisor || true
        systemctl restart verigo-worker@1.service || true
        systemctl restart verigo-worker@2.service || true
    fi
    set_service_mode active || true
    exit "$status"
}

set_service_mode() {
    local mode=$1
    MODE="$mode" PYTHONPATH="$release_dir" "$state_dir/.venv/bin/python" - <<'PY'
import os
from datetime import datetime, timezone
from pathlib import Path

from app.db.sqlite import begin_immediate, connect

with connect(Path('/opt/verigo/data/verigo.db')) as connection:
    begin_immediate(connection)
    connection.execute('''CREATE TABLE IF NOT EXISTS service_state (
        name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    connection.execute('''INSERT INTO service_state(name, value, updated_at)
        VALUES ('verification_mode', ?, ?)
        ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at''',
        (os.environ['MODE'], datetime.now(timezone.utc).isoformat()))
PY
}

active_job_count() {
    PYTHONPATH="$release_dir" "$state_dir/.venv/bin/python" - <<'PY'
from pathlib import Path

from app.db.sqlite import connect

connection = connect(Path('/opt/verigo/data/verigo.db'))
print(connection.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')").fetchone()[0])
PY
}

active_targets() {
    PYTHONPATH="$release_dir" "$state_dir/.venv/bin/python" - <<'PY'
from pathlib import Path

from app.db.sqlite import connect

connection = connect(Path('/opt/verigo/data/verigo.db'))
for (target,) in connection.execute("""
    SELECT DISTINCT execution_target FROM jobs
    WHERE status IN ('queued', 'running') AND execution_target != 'aggregate'
    ORDER BY execution_target
"""):
    print(target)
PY
}

drain_progress_marker() {
    PYTHONPATH="$release_dir" "$state_dir/.venv/bin/python" - <<'PY'
from pathlib import Path

from app.db.sqlite import connect

connection = connect(Path('/opt/verigo/data/verigo.db'))
rows = connection.execute("""
    SELECT j.status, j.execution_target, COUNT(*) AS jobs,
           COALESCE(MAX(j.heartbeat_at), ''),
           COALESCE(MAX(r.updated_at), '')
    FROM jobs j
    LEFT JOIN job_results r ON r.job_id = j.id
    WHERE j.status IN ('queued', 'running')
    GROUP BY j.status, j.execution_target
    ORDER BY j.status, j.execution_target
""").fetchall()
print('|'.join(':'.join(str(value) for value in row) for row in rows))
PY
}

ensure_drain_workers() {
    local targets=()
    mapfile -t targets < <(active_targets)
    if (( ${#targets[@]} == 0 )); then
        return 0
    fi

    local target
    for target in "${targets[@]}"; do
        if [[ "$target" == "local" ]]; then
            if ! systemctl is-active --quiet verigo-worker@1.service \
                && ! systemctl is-active --quiet verigo-worker@2.service; then
                echo "Cannot drain local verification jobs: no local worker is active" >&2
                return 1
            fi
        elif ! systemctl is-active --quiet verigo-supervisor.service; then
            echo "Cannot drain $target verification jobs: remote node supervisor is inactive" >&2
            return 1
        fi
    done
}

mkdir -p "$releases_dir"
ensure_legacy_release
previous_release=$(readlink -f "$current_link")
trap rollback ERR

# Reject new submissions first, then let existing workers settle their queue.
# A timeout restores active mode and leaves the current release untouched.
if [[ "${VERIGO_DEPLOY_MAINTENANCE:-false}" != "true" && -f "$state_dir/data/verigo.db" ]]; then
    drain_timeout=${VERIGO_DEPLOY_DRAIN_TIMEOUT_SECONDS:-900}
    drain_stall_timeout=${VERIGO_DEPLOY_DRAIN_STALL_SECONDS:-180}
    if ! [[ "$drain_timeout" =~ ^[1-9][0-9]*$ ]]; then
        echo "VERIGO_DEPLOY_DRAIN_TIMEOUT_SECONDS must be a positive integer" >&2
        exit 1
    fi
    if ! [[ "$drain_stall_timeout" =~ ^[1-9][0-9]*$ ]]; then
        echo "VERIGO_DEPLOY_DRAIN_STALL_SECONDS must be a positive integer" >&2
        exit 1
    fi
    set_service_mode draining
    if ! ensure_drain_workers; then
        set_service_mode active
        exit 2
    fi
    deadline=$((SECONDS + drain_timeout))
    last_progress_at=$SECONDS
    progress_marker=$(drain_progress_marker)
    while :; do
        active_jobs=$(active_job_count)
        if [[ "$active_jobs" == "0" ]]; then
            break
        fi
        current_marker=$(drain_progress_marker)
        if [[ "$current_marker" != "$progress_marker" ]]; then
            progress_marker=$current_marker
            last_progress_at=$SECONDS
        elif (( SECONDS - last_progress_at >= drain_stall_timeout )); then
            echo "Release drain stalled for ${drain_stall_timeout}s with $active_jobs active verification jobs: $current_marker" >&2
            set_service_mode active
            exit 2
        fi
        if (( SECONDS >= deadline )); then
            echo "Release drain timed out with $active_jobs active verification jobs" >&2
            set_service_mode active
            exit 2
        fi
        sleep 5
    done
fi

release_path="$releases_dir/$release_version"
if [[ ! -d "$release_path" ]]; then
    incoming=$(mktemp -d "$releases_dir/.incoming-${release_version:0:12}.XXXXXX")
    rsync -a --delete --exclude='__pycache__' --exclude='.verigo-release' --exclude='release.tar.gz' "$release_dir/" "$incoming/"
    printf '%s\n' "$release_version" > "$incoming/RELEASE_VERSION"
    chown -R verigo:verigo "$incoming"
    mv "$incoming" "$release_path"
fi

test -f "$release_path/app/main.py"
test -f "$release_path/RELEASE_VERSION"

if ! command -v aws >/dev/null && grep -q '^VERIGO_BACKUP_S3_BUCKET=.' /etc/verigo/backup.env 2>/dev/null; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y awscli
fi

install -m 700 "$release_path/deploy/verigo-backup.sh" /usr/local/sbin/verigo-backup
install -m 644 "$release_path/deploy/verigo-backup.service" /etc/systemd/system/verigo-backup.service
install -m 644 "$release_path/deploy/verigo-backup.timer" /etc/systemd/system/verigo-backup.timer
install -m 700 "$release_path/deploy/verigo-monitor.sh" /usr/local/sbin/verigo-monitor
install -m 644 "$release_path/deploy/verigo-monitor.service" /etc/systemd/system/verigo-monitor.service
install -m 644 "$release_path/deploy/verigo-monitor.timer" /etc/systemd/system/verigo-monitor.timer
install -m 700 "$release_path/deploy/verigo-retention.sh" /usr/local/sbin/verigo-retention
install -m 644 "$release_path/deploy/verigo-retention.service" /etc/systemd/system/verigo-retention.service
install -m 644 "$release_path/deploy/verigo-retention.timer" /etc/systemd/system/verigo-retention.timer
install -m 644 "$release_path/deploy/verigo.service" /etc/systemd/system/verigo.service
install -m 644 "$release_path/deploy/verigo-supervisor.service" /etc/systemd/system/verigo-supervisor.service
install -m 644 "$release_path/deploy/verigo-worker@.service" /etc/systemd/system/verigo-worker@.service
install -d -m 700 /etc/verigo
if [[ ! -f /etc/verigo/verigo.env ]]; then
    install -m 600 "$release_path/deploy/verigo.env.example" /etc/verigo/verigo.env
fi
if [[ ! -f /etc/verigo/backup.env ]]; then
    install -m 600 "$release_path/deploy/verigo-backup.env.example" /etc/verigo/backup.env
fi
if [[ ! -f /etc/verigo/monitor.env ]]; then
    install -m 600 "$release_path/deploy/verigo-monitor.env.example" /etc/verigo/monitor.env
fi
if [[ ! -f /etc/verigo/retention.env ]]; then
    install -m 600 "$release_path/deploy/verigo-retention.env.example" /etc/verigo/retention.env
fi

for setting in \
    'VERIGO_DATABASE_PATH=/opt/verigo/data/verigo.db' \
    'VERIGO_RESULTS_DIR=/opt/verigo/data/results' \
    'VERIGO_NAME_CATALOG_PATH=/opt/verigo/data/name_catalog.db' \
    'VERIGO_SMTP_LIMITER_PATH=/opt/verigo/data/smtp_limiter.db' \
    'VERIGO_DOMAIN_CACHE_PATH=/opt/verigo/data/domain_type_cache.json' \
    'VERIGO_MAX_EMAILS=0' \
    'VERIGO_MAX_WORKERS=8' \
    'VERIGO_REMOTE_WORKER_MAX_EMAILS=5000' \
    'VERIGO_CLOUDSTUDIO_MAX_WORKERS=4' \
    'VERIGO_CLOUDSHELL_MAX_WORKERS=25' \
    'VERIGO_CODEARTS_MAX_WORKERS=16' \
    'VERIGO_CLOUDSHELL_WORKER_PROCESSES=2' \
    'VERIGO_CLOUDSHELL_SECONDARY_WORKER_PROCESSES=1' \
    'VERIGO_SCHEDULER_GMAIL_CONCURRENCY=25' \
    'VERIGO_SCHEDULER_MICROSOFT_CONCURRENCY=64' \
    'VERIGO_SCHEDULER_DEFAULT_DOMAIN_CONCURRENCY=4' \
    'VERIGO_SCHEDULER_DOMAIN_MAX_CONCURRENCY=16' \
    'VERIGO_SCHEDULER_REMOTE_SHARD_SIZE=25' \
    'VERIGO_PROSPECTING_SCHEDULER_SHARD_SIZE=4' \
    'VERIGO_SCHEDULER_CLAIM_SCAN_LIMIT=64' \
    'VERIGO_SCHEDULER_SUCCESSES_PER_STEP=20' \
    'VERIGO_PROSPECTING_SCHEDULER_INITIAL_DOMAIN_CONCURRENCY=16' \
    'VERIGO_PROSPECTING_SCHEDULER_SUCCESSES_PER_STEP=8' \
    'VERIGO_PROSPECTING_SCHEDULER_STEP_SIZE=2' \
    'VERIGO_SCHEDULER_COOLDOWN_SECONDS=120' \
    'VERIGO_TENCENT_QQ_WORKER_ALLOWED_EMAILS=*' \
    'VERIGO_GMAIL_WORKER_ALLOWED_EMAILS=*' \
    'VERIGO_CODEARTS_WORKER_ALLOWED_EMAILS=*' \
    'VERIGO_MAX_GUEST_EMAILS=100' \
    'VERIGO_PROSPECTING_BETA_MAX_CANDIDATES=1000' \
    'VERIGO_FREE_SINGLE_DAILY_LIMIT=20' \
    'VERIGO_EMAIL_VERIFICATION_TRIAL_CREDITS=10' \
    'VERIGO_TRIAL_CREDIT_DAYS=7' \
    'VERIGO_MAX_IMPORT_BYTES=5242880' \
    'VERIGO_SESSION_TTL_DAYS=30' \
    'VERIGO_SECURE_COOKIES=true'
do
    key=${setting%%=*}
    if ! grep -q "^${key}=" /etc/verigo/verigo.env; then
        printf '%s\n' "$setting" >> /etc/verigo/verigo.env
    fi
done
if ! grep -q '^VERIGO_METRICS_SALT=' /etc/verigo/verigo.env; then
    printf 'VERIGO_METRICS_SALT=%s\n' "$(openssl rand -hex 32)" >> /etc/verigo/verigo.env
fi
if ! grep -q '^VERIGO_MONITOR_TOKEN=[^[:space:]]' /etc/verigo/verigo.env; then
    printf 'VERIGO_MONITOR_TOKEN=%s\n' "$(openssl rand -hex 32)" >> /etc/verigo/verigo.env
fi
if [[ ! -f "$state_dir/data/domain_type_cache.json" && -f "$state_dir/domain_type_cache.json" ]]; then
    install -m 600 -o verigo -g verigo "$state_dir/domain_type_cache.json" "$state_dir/data/domain_type_cache.json"
fi
chmod 600 /etc/verigo/verigo.env

# Version 2 of private prospecting needs a materially larger statistical
# sample. Migrate only the old shipped default, preserving operator overrides.
if grep -q '^VERIGO_PROSPECTING_BETA_MAX_CANDIDATES=120$' /etc/verigo/verigo.env; then
    sed -i 's/^VERIGO_PROSPECTING_BETA_MAX_CANDIDATES=120$/VERIGO_PROSPECTING_BETA_MAX_CANDIDATES=1000/' /etc/verigo/verigo.env
fi
# The private beta no longer has a daily-run quota. Its per-run size remains
# bounded while each account receives a fresh, non-repeating candidate set.
sed -i '/^VERIGO_PROSPECTING_BETA_DAILY_RUN_LIMIT=/d' /etc/verigo/verigo.env

if ! cmp -s "$previous_release/requirements.txt" "$release_path/requirements.txt"; then
    "$state_dir/.venv/bin/pip" install --disable-pip-version-check -r "$release_path/requirements.txt"
fi

systemctl daemon-reload
systemctl enable --now verigo-backup.timer verigo-monitor.timer verigo-retention.timer
# Keep release backup independent from a remote provider's latency.
systemctl start --no-block verigo-backup.service

set -a
. /etc/verigo/verigo.env
set +a
PYTHONPATH="$release_path" runuser -u verigo --preserve-environment -- \
    "$state_dir/.venv/bin/python" -m app.maintenance migrate-results

activate_release "$release_path"
systemctl restart verigo
for _ in {1..20}; do
    if curl -fsS http://127.0.0.1:8000/api/health >/dev/null; then
        systemctl restart verigo-supervisor.service
        # Jobs were drained before activation, so workers can safely reload
        # the release instead of retaining modules from the old symlink target.
        systemctl restart verigo-worker@1.service
        systemctl restart verigo-worker@2.service
        set_service_mode active
        trap - ERR
        # Keep only the 10 most recent releases to prevent disk accumulation.
        ls -t "$releases_dir" | grep -v '^\.' | tail -n +11 | while read -r old_release; do
            old_path="$releases_dir/$old_release"
            if [ "$old_path" != "$(readlink -f "$current_link")" ]; then
                rm -rf "$old_path"
            fi
        done
        printf 'Verigo release %s health check passed\n' "$release_version"
        exit 0
    fi
    sleep 1
done

journalctl -u verigo -n 80 --no-pager >&2
exit 1
