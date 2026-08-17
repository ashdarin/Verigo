#!/usr/bin/env bash
set -Eeuo pipefail

release_dir=${VERIGO_RELEASE_DIR:-/tmp/verigo-release}
deploy_role=${VERIGO_DEPLOY_ROLE:-}
state_dir=/opt/verigo
releases_dir="$state_dir/releases"
current_link="$state_dir/current"
release_version_file="$release_dir/.verigo-release"

case "$deploy_role" in
    shanghai-app|hong-kong-edge-worker) ;;
    *)
        echo "VERIGO_DEPLOY_ROLE must be shanghai-app or hong-kong-edge-worker" >&2
        exit 2
        ;;
esac

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
    [[ "$legacy_version" =~ ^[0-9a-f]{7,40}$ ]] \
        || legacy_version="legacy-$(date -u +%Y%m%dT%H%M%SZ)"
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

disable_units() {
    local unit
    for unit in "$@"; do
        systemctl disable --now "$unit" >/dev/null 2>&1 || true
    done
}

edge_worker_units() {
    {
        printf '%s\n' verigo-worker@1.service verigo-worker@2.service
        systemctl list-unit-files --type=service --no-legend \
            'verigo-worker@*.service' 2>/dev/null \
            | awk '$1 != "verigo-worker@.service" && $2 == "enabled" {print $1}'
        systemctl list-units --type=service --state=active --no-legend \
            'verigo-worker@*.service' 2>/dev/null \
            | awk '$1 != "verigo-worker@.service" {print $1}'
    } | sort -uV
}

restart_edge_workers() {
    local workers=()
    mapfile -t workers < <(edge_worker_units)
    systemctl restart verigo-supervisor "${workers[@]}" verigo-qq-worker
}

assert_edge_worker_release() {
    local workers=() unit pid cwd
    mapfile -t workers < <(edge_worker_units)
    for unit in "${workers[@]}"; do
        systemctl is-active --quiet "$unit"
        pid=$(systemctl show --property MainPID --value "$unit")
        [[ "$pid" =~ ^[1-9][0-9]*$ ]]
        cwd=$(readlink -f "/proc/${pid}/cwd")
        [[ "$cwd" == "$release_path" ]] || {
            echo "$unit is still running $cwd instead of $release_path" >&2
            return 1
        }
    done
}

set_env_value() {
    local file=$1 key=$2 value=$3
    if grep -q "^${key}=" "$file"; then
        sed -i "s/^${key}=.*/${key}=${value}/" "$file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

managed_cross_route_settings() {
    cat <<'EOF'
VERIGO_SMTP_CROSS_ROUTE_ENABLED=true
VERIGO_SMTP_CROSS_ROUTE_SHADOW_MODE=false
VERIGO_SMTP_CROSS_ROUTE_TARGET=local
VERIGO_SMTP_CROSS_ROUTE_MAX_PER_EMAIL=1
VERIGO_SMTP_CROSS_ROUTE_CONCURRENCY=1
VERIGO_SMTP_CROSS_ROUTE_PER_MX_CONCURRENCY=1
VERIGO_SMTP_CROSS_ROUTE_PRESSURE_MIN_SAMPLES=5
VERIGO_SMTP_CROSS_ROUTE_PRESSURE_4XX_RATE=0.60
VERIGO_SMTP_CROSS_ROUTE_DISPATCH_DELAY_SECONDS=0
EOF
}

sync_managed_cross_route_config() {
    local file=$1 setting key value
    while IFS= read -r setting; do
        key=${setting%%=*}
        value=${setting#*=}
        set_env_value "$file" "$key" "$value"
    done < <(managed_cross_route_settings)
}

assert_managed_cross_route_config() {
    local file=$1 setting
    while IFS= read -r setting; do
        grep -Fqx "$setting" "$file" || {
            echo "Managed SMTP cross-route setting drifted in $file: ${setting%%=*}" >&2
            return 1
        }
    done < <(managed_cross_route_settings)
}

verify_runtime_cross_route_config() {
    local env_file=$1
    (
        set -a
        source "$env_file"
        set +a
        PYTHONPATH="$release_path" "$state_dir/.venv/bin/python" - <<'PY'
from app.config import settings

actual = {
    "enabled": settings.smtp_cross_route_enabled,
    "shadow": settings.smtp_cross_route_shadow_mode,
    "target": settings.smtp_cross_route_target,
    "max_per_email": settings.smtp_cross_route_max_per_email,
    "concurrency": settings.smtp_cross_route_concurrency,
    "per_mx": settings.smtp_cross_route_per_mx_concurrency,
    "pressure_min": settings.smtp_cross_route_pressure_min_samples,
    "pressure_rate": settings.smtp_cross_route_pressure_4xx_rate,
    "dispatch_delay": settings.smtp_cross_route_dispatch_delay_seconds,
}
expected = {
    "enabled": True,
    "shadow": False,
    "target": "local",
    "max_per_email": 1,
    "concurrency": 1,
    "per_mx": 1,
    "pressure_min": 5,
    "pressure_rate": 0.60,
    "dispatch_delay": 0,
}
if actual != expected:
    raise SystemExit(f"SMTP cross-route runtime drift: {actual}")
PY
    )
}

rollback() {
    local status=$?
    trap - ERR
    if [[ -n "${previous_release:-}" ]]; then
        echo "Release failed; switching back to $previous_release" >&2
        activate_release "$previous_release"
        if [[ "$deploy_role" == "shanghai-app" ]]; then
            systemctl restart verigo verigo-worker-api || true
        elif [[ "${VERIGO_DEPLOY_MAINTENANCE:-false}" == "true" ]]; then
            restart_edge_workers || true
        fi
    fi
    exit "$status"
}

install_env_files() {
    install -d -m 700 /etc/verigo
    if [[ ! -f /etc/verigo/verigo.env ]]; then
        install -m 600 "$release_path/deploy/verigo.env.example" /etc/verigo/verigo.env
    fi
    local example target
    for example in verigo-backup verigo-monitor verigo-retention; do
        target=${example#verigo-}
        if [[ ! -f "/etc/verigo/${target}.env" ]]; then
            install -m 600 "$release_path/deploy/${example}.env.example" \
                "/etc/verigo/${target}.env"
        fi
    done

    local setting key
    for setting in \
        'VERIGO_DATABASE_PATH=/opt/verigo/data/verigo.db' \
        'VERIGO_RESULTS_DIR=/opt/verigo/data/results' \
        'VERIGO_NAME_CATALOG_PATH=/opt/verigo/data/name_catalog.db' \
        'VERIGO_SMTP_LIMITER_PATH=/opt/verigo/data/smtp_limiter.db' \
        'VERIGO_MAX_EMAILS=0' \
        'VERIGO_MAX_WORKERS=8' \
        'VERIGO_REMOTE_WORKER_MAX_EMAILS=5000' \
        'VERIGO_CLOUDSTUDIO_MAX_WORKERS=4' \
        'VERIGO_QQ_WORKER_MAX_WORKERS=6' \
        'VERIGO_CLOUDSHELL_MAX_WORKERS=3' \
        'VERIGO_CODEARTS_MAX_WORKERS=16' \
        'VERIGO_CLOUDSHELL_WORKER_PROCESSES=4' \
        'VERIGO_CLOUDSHELL_SECONDARY_WORKER_PROCESSES=4' \
        'VERIGO_SCHEDULER_GMAIL_CONCURRENCY=25' \
        'VERIGO_SCHEDULER_MICROSOFT_CONCURRENCY=64' \
        'VERIGO_SCHEDULER_DEFAULT_DOMAIN_CONCURRENCY=4' \
        'VERIGO_SCHEDULER_DOMAIN_MAX_CONCURRENCY=16' \
        'VERIGO_SCHEDULER_REMOTE_SHARD_SIZE=25' \
        'VERIGO_QQ_SCHEDULER_SHARD_SIZE=6' \
        'VERIGO_QQ_SMTP_PER_MX=6' \
        'VERIGO_PROSPECTING_SCHEDULER_SHARD_SIZE=4' \
        'VERIGO_SCHEDULER_CLAIM_SCAN_LIMIT=64' \
        'VERIGO_SCHEDULER_SUCCESSES_PER_STEP=20' \
        'VERIGO_PROSPECTING_SCHEDULER_INITIAL_DOMAIN_CONCURRENCY=16' \
        'VERIGO_PROSPECTING_SCHEDULER_SUCCESSES_PER_STEP=8' \
        'VERIGO_PROSPECTING_SCHEDULER_STEP_SIZE=2' \
        'VERIGO_SCHEDULER_COOLDOWN_SECONDS=120' \
        'VERIGO_SMTP_CROSS_ROUTE_ENABLED=true' \
        'VERIGO_SMTP_CROSS_ROUTE_SHADOW_MODE=false' \
        'VERIGO_SMTP_CROSS_ROUTE_TARGET=local' \
        'VERIGO_SMTP_CROSS_ROUTE_MAX_PER_EMAIL=1' \
        'VERIGO_SMTP_CROSS_ROUTE_CONCURRENCY=1' \
        'VERIGO_SMTP_CROSS_ROUTE_PER_MX_CONCURRENCY=1' \
        'VERIGO_SMTP_CROSS_ROUTE_PRESSURE_MIN_SAMPLES=5' \
        'VERIGO_SMTP_CROSS_ROUTE_PRESSURE_4XX_RATE=0.60' \
        'VERIGO_SMTP_CROSS_ROUTE_DISPATCH_DELAY_SECONDS=0' \
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
        grep -q "^${key}=" /etc/verigo/verigo.env \
            || printf '%s\n' "$setting" >> /etc/verigo/verigo.env
    done
    sync_managed_cross_route_config /etc/verigo/verigo.env
    grep -q '^VERIGO_METRICS_SALT=' /etc/verigo/verigo.env \
        || printf 'VERIGO_METRICS_SALT=%s\n' "$(openssl rand -hex 32)" \
            >> /etc/verigo/verigo.env
    grep -q '^VERIGO_MONITOR_TOKEN=[^[:space:]]' /etc/verigo/verigo.env \
        || printf 'VERIGO_MONITOR_TOKEN=%s\n' "$(openssl rand -hex 32)" \
            >> /etc/verigo/verigo.env
    if [[ "$deploy_role" == "shanghai-app" ]]; then
        if grep -q '^VERIGO_CLOUDSHELL_LIFECYCLE_DISPATCH_ENABLED=' /etc/verigo/verigo.env; then
            sed -i 's/^VERIGO_CLOUDSHELL_LIFECYCLE_DISPATCH_ENABLED=.*/VERIGO_CLOUDSHELL_LIFECYCLE_DISPATCH_ENABLED=false/' \
                /etc/verigo/verigo.env
        else
            printf '%s\n' 'VERIGO_CLOUDSHELL_LIFECYCLE_DISPATCH_ENABLED=false' \
                >> /etc/verigo/verigo.env
        fi
    else
        if grep -q '^VERIGO_CLOUDSHELL_LIFECYCLE_DISPATCH_ENABLED=' /etc/verigo/verigo.env; then
            sed -i 's/^VERIGO_CLOUDSHELL_LIFECYCLE_DISPATCH_ENABLED=.*/VERIGO_CLOUDSHELL_LIFECYCLE_DISPATCH_ENABLED=true/' \
                /etc/verigo/verigo.env
        else
            printf '%s\n' 'VERIGO_CLOUDSHELL_LIFECYCLE_DISPATCH_ENABLED=true' \
                >> /etc/verigo/verigo.env
        fi
    fi
    chmod 600 /etc/verigo/verigo.env

    if grep -q '^VERIGO_PROSPECTING_BETA_MAX_CANDIDATES=120$' /etc/verigo/verigo.env; then
        sed -i 's/^VERIGO_PROSPECTING_BETA_MAX_CANDIDATES=120$/VERIGO_PROSPECTING_BETA_MAX_CANDIDATES=1000/' \
            /etc/verigo/verigo.env
    fi
    sed -i '/^VERIGO_PROSPECTING_BETA_DAILY_RUN_LIMIT=/d' /etc/verigo/verigo.env
}

write_worker_env() {
    local worker_env_tmp
    worker_env_tmp=$(mktemp /etc/verigo/.verigo-worker.env.XXXXXX)
    if [[ "$deploy_role" == "hong-kong-edge-worker" ]]; then
        sed -e 's/127\.0\.0\.1:15432/127.0.0.1:15433/g' \
            -e 's/localhost:15432/127.0.0.1:15433/g' \
            /etc/verigo/verigo.env > "$worker_env_tmp"
    else
        cp /etc/verigo/verigo.env "$worker_env_tmp"
    fi
    if grep -q '^VERIGO_WORKER_LEASE_SECONDS=' "$worker_env_tmp"; then
        sed -i 's/^VERIGO_WORKER_LEASE_SECONDS=.*/VERIGO_WORKER_LEASE_SECONDS=180/' \
            "$worker_env_tmp"
    else
        printf '%s\n' 'VERIGO_WORKER_LEASE_SECONDS=180' >> "$worker_env_tmp"
    fi
    chmod 600 "$worker_env_tmp"
    assert_managed_cross_route_config "$worker_env_tmp"
    mv -f "$worker_env_tmp" /etc/verigo/verigo-worker.env
}

write_qq_env() {
    grep -q '^VERIGO_TENCENT_QQ_WORKER_TOKEN=.' /etc/verigo/verigo-worker.env
    local qq_env_tmp
    qq_env_tmp=$(mktemp /etc/verigo/.tencent-qq-worker.env.XXXXXX)
    grep -Ev '^(VERIGO_REMOTE_WORKER_(SERVER|TOKEN|ID|TARGET|CAPACITY|CLAIM_WAIT_SECONDS|MAX_WORKERS)|VERIGO_TENCENT_QQ_(SERVER|WORKER_ID)|VERIGO_QQ_WORKER_MAX_WORKERS|VERIGO_QQ_SMTP_PER_MX|VERIGO_QQ_SCHEDULER_SHARD_SIZE|VERIGO_EMAIL_HARD_TIMEOUT_SECONDS)=' \
        /etc/verigo/verigo-worker.env > "$qq_env_tmp" || true
    cat >> "$qq_env_tmp" <<'EOF'
VERIGO_REMOTE_WORKER_SERVER=https://verigo.site
VERIGO_REMOTE_WORKER_ID=vps-local-qq-fixed
VERIGO_REMOTE_WORKER_TARGET=tencent-qq
VERIGO_REMOTE_WORKER_CAPACITY=1
VERIGO_REMOTE_WORKER_CLAIM_WAIT_SECONDS=20
VERIGO_REMOTE_WORKER_MAX_WORKERS=6
VERIGO_QQ_WORKER_MAX_WORKERS=6
VERIGO_QQ_SMTP_PER_MX=6
VERIGO_QQ_SCHEDULER_SHARD_SIZE=6
VERIGO_EMAIL_HARD_TIMEOUT_SECONDS=90
EOF
    chmod 600 "$qq_env_tmp"
    assert_managed_cross_route_config "$qq_env_tmp"
    mv -f "$qq_env_tmp" /etc/verigo/tencent-qq-worker.env
}

prune_releases() {
    ls -t "$releases_dir" | grep -v '^\.' | tail -n +11 | while read -r old_release; do
        local old_path="$releases_dir/$old_release"
        if [[ "$old_path" != "$(readlink -f "$current_link")" ]]; then
            rm -rf "$old_path"
        fi
    done
}

mkdir -p "$releases_dir" "$state_dir/data"
ensure_legacy_release
previous_release=$(readlink -f "$current_link")
trap rollback ERR

release_path="$releases_dir/$release_version"
if [[ ! -d "$release_path" ]]; then
    incoming=$(mktemp -d "$releases_dir/.incoming-${release_version:0:12}.XXXXXX")
    rsync -a --delete --exclude='__pycache__' --exclude='.verigo-release' \
        --exclude='release.tar.gz' "$release_dir/" "$incoming/"
    printf '%s\n' "$release_version" > "$incoming/RELEASE_VERSION"
    chown -R verigo:verigo "$incoming"
    mv "$incoming" "$release_path"
fi

test -f "$release_path/app/main.py"
test -f "$release_path/RELEASE_VERSION"
install_env_files
write_worker_env
assert_managed_cross_route_config /etc/verigo/verigo.env
assert_managed_cross_route_config /etc/verigo/verigo-worker.env
verify_runtime_cross_route_config /etc/verigo/verigo-worker.env

if ! cmp -s "$previous_release/requirements.txt" "$release_path/requirements.txt"; then
    "$state_dir/.venv/bin/pip" install --disable-pip-version-check \
        -r "$release_path/requirements.txt"
fi

if [[ "$deploy_role" == "shanghai-app" ]]; then
    install -m 644 "$release_path/deploy/verigo.service" \
        /etc/systemd/system/verigo.service
    install -m 644 "$release_path/deploy/verigo-worker-api.service" \
        /etc/systemd/system/verigo-worker-api.service
    install -m 644 "$release_path/deploy/verigo-company-finder-tunnel.service" \
        /etc/systemd/system/verigo-company-finder-tunnel.service
    install -m 700 "$release_path/deploy/verigo-backup.sh" /usr/local/sbin/verigo-backup
    install -m 644 "$release_path/deploy/verigo-backup.service" \
        /etc/systemd/system/verigo-backup.service
    install -m 644 "$release_path/deploy/verigo-backup.timer" \
        /etc/systemd/system/verigo-backup.timer
    install -m 700 "$release_path/deploy/verigo-retention.sh" /usr/local/sbin/verigo-retention
    install -m 644 "$release_path/deploy/verigo-retention.service" \
        /etc/systemd/system/verigo-retention.service
    install -m 644 "$release_path/deploy/verigo-retention.timer" \
        /etc/systemd/system/verigo-retention.timer

    # Runtime stores intentionally do not issue DDL on application startup.
    # Apply this additive operational index before the new metrics query is
    # exposed; the helper uses PostgreSQL's concurrent build to keep workers
    # writing result rows during the release.
    set -a
    source /etc/verigo/verigo.env
    set +a
    PYTHONPATH="$release_path" "$state_dir/.venv/bin/python" \
        "$release_path/scripts/ensure_quality_dashboard_schema.py"
    PYTHONPATH="$release_path" "$state_dir/.venv/bin/python" \
        "$release_path/scripts/ensure_company_finder_metrics_schema.py"
    PYTHONPATH="$release_path" "$state_dir/.venv/bin/python" \
        "$release_path/scripts/ensure_verification_cache_schema.py"
    PYTHONPATH="$release_path" "$state_dir/.venv/bin/python" \
        "$release_path/scripts/ensure_smtp_cross_route_schema.py"

    worker_bundle_tmp=$(mktemp "$state_dir/data/.cloudstudio-worker.XXXXXX.tar.gz")
    tar -czf "$worker_bundle_tmp" -C "$release_path" app requirements.txt RELEASE_VERSION
    chown verigo:verigo "$worker_bundle_tmp"
    chmod 640 "$worker_bundle_tmp"
    mv -f "$worker_bundle_tmp" "$state_dir/data/cloudstudio-worker.tar.gz"

    systemctl daemon-reload
    mapfile -t edge_workers < <(edge_worker_units)
    disable_units caddy verigo-monitor.timer verigo-monitor.service \
        verigo-supervisor "${edge_workers[@]}" \
        verigo-postgres-tunnel verigo-postgres-worker-tunnel \
        verigo-data-app-tunnel verigo-cloudstudio-keepalive verigo-qq-worker
    systemctl enable --now verigo-company-finder-tunnel \
        verigo-backup.timer verigo-retention.timer
    activate_release "$release_path"
    systemctl enable verigo verigo-worker-api >/dev/null
    systemctl restart verigo
    for _ in {1..60}; do
        if curl -fsS http://127.0.0.1:8000/api/health >/dev/null; then
            systemctl restart verigo-worker-api
            for _ in {1..60}; do
                code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
                    http://127.0.0.1:8001/api/workers/tencent-qq/claim || true)
                [[ "$code" == "401" ]] && break
                sleep 1
            done
            [[ "$code" == "401" ]]
            assert_managed_cross_route_config /etc/verigo/verigo.env
            assert_managed_cross_route_config /etc/verigo/verigo-worker.env
            verify_runtime_cross_route_config /etc/verigo/verigo-worker.env
            prune_releases
            trap - ERR
            printf 'Verigo %s release %s health check passed\n' "$deploy_role" "$release_version"
            exit 0
        fi
        sleep 1
    done
    journalctl -u verigo -n 80 --no-pager >&2
    exit 1
fi

write_qq_env
assert_managed_cross_route_config /etc/verigo/tencent-qq-worker.env
verify_runtime_cross_route_config /etc/verigo/tencent-qq-worker.env
install -m 644 "$release_path/deploy/verigo-data-app-tunnel.service" \
    /etc/systemd/system/verigo-data-app-tunnel.service
install -m 644 "$release_path/deploy/verigo-postgres-tunnel.service" \
    /etc/systemd/system/verigo-postgres-tunnel.service
install -m 644 "$release_path/deploy/verigo-postgres-worker-tunnel.service" \
    /etc/systemd/system/verigo-postgres-worker-tunnel.service
install -m 644 "$release_path/deploy/verigo-supervisor.service" \
    /etc/systemd/system/verigo-supervisor.service
install -m 644 "$release_path/deploy/verigo-worker@.service" \
    /etc/systemd/system/verigo-worker@.service
install -m 644 "$release_path/deploy/verigo-qq-worker.service" \
    /etc/systemd/system/verigo-qq-worker.service
install -m 700 "$release_path/deploy/verigo-monitor.sh" /usr/local/sbin/verigo-monitor
install -m 644 "$release_path/deploy/verigo-monitor.service" \
    /etc/systemd/system/verigo-monitor.service
install -m 644 "$release_path/deploy/verigo-monitor.timer" \
    /etc/systemd/system/verigo-monitor.timer

if ! command -v caddy >/dev/null; then
    echo "Caddy is required by the hong-kong-edge-worker role" >&2
    exit 1
fi
caddy validate --config "$release_path/deploy/Caddyfile.edge" --adapter caddyfile
install -m 644 "$release_path/deploy/Caddyfile.edge" /etc/caddy/Caddyfile

disable_units verigo-cloudstudio-keepalive
rm -f /etc/systemd/system/verigo-cloudstudio-keepalive.service
systemctl daemon-reload
disable_units verigo verigo-worker-api verigo-company-finder-tunnel \
    verigo-backup.timer verigo-backup.service \
    verigo-retention.timer verigo-retention.service
activate_release "$release_path"
mapfile -t edge_workers < <(edge_worker_units)
systemctl enable --now verigo-data-app-tunnel verigo-postgres-tunnel \
    verigo-postgres-worker-tunnel caddy verigo-monitor.timer \
    verigo-supervisor "${edge_workers[@]}" \
    verigo-qq-worker
systemctl reload caddy

if [[ "${VERIGO_DEPLOY_MAINTENANCE:-false}" == "true" ]]; then
    restart_edge_workers
    assert_edge_worker_release
else
    echo "Worker processes were left running; use maintenance mode for an immediate worker restart"
fi

for _ in {1..60}; do
    if curl -fsS https://verigo.site/api/health >/dev/null; then
        assert_managed_cross_route_config /etc/verigo/verigo.env
        assert_managed_cross_route_config /etc/verigo/verigo-worker.env
        assert_managed_cross_route_config /etc/verigo/tencent-qq-worker.env
        verify_runtime_cross_route_config /etc/verigo/verigo-worker.env
        verify_runtime_cross_route_config /etc/verigo/tencent-qq-worker.env
        prune_releases
        trap - ERR
        printf 'Verigo %s release %s health check passed\n' "$deploy_role" "$release_version"
        exit 0
    fi
    sleep 1
done
journalctl -u caddy -u verigo-data-app-tunnel -n 80 --no-pager >&2
exit 1
