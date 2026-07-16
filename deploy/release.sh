#!/usr/bin/env bash
set -Eeuo pipefail

release_dir=${VERIGO_RELEASE_DIR:-/tmp/verigo-release}
app_dir=/opt/verigo
backup_dir=/opt/verigo-release-rollback
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

rollback() {
    echo "Release failed; restoring previous application files" >&2
    rsync -a --delete "$backup_dir/app/" "$app_dir/app/"
    rsync -a --delete "$backup_dir/static/" "$app_dir/static/"
    rsync -a --delete "$backup_dir/deploy/" "$app_dir/deploy/"
    cp "$backup_dir/requirements.txt" "$app_dir/requirements.txt"
    cp "$backup_dir/验证8.py" "$app_dir/验证8.py"
    chown -R verigo:verigo "$app_dir"
    systemctl restart verigo || true
}

systemctl start verigo-backup.service
mkdir -p "$backup_dir"
rsync -a --delete "$app_dir/app/" "$backup_dir/app/"
rsync -a --delete "$app_dir/static/" "$backup_dir/static/"
rsync -a --delete "$app_dir/deploy/" "$backup_dir/deploy/"
cp "$app_dir/requirements.txt" "$backup_dir/requirements.txt"
cp "$app_dir/验证8.py" "$backup_dir/验证8.py"

trap rollback ERR

rsync -a --delete --exclude='__pycache__' "$release_dir/app/" "$app_dir/app/"
rsync -a --delete "$release_dir/static/" "$app_dir/static/"
rsync -a --delete "$release_dir/deploy/" "$app_dir/deploy/"
cp "$release_dir/requirements.txt" "$app_dir/requirements.txt"
cp "$release_dir/验证8.py" "$app_dir/验证8.py"
printf '%s\n' "$release_version" > "$app_dir/RELEASE_VERSION"

install -m 700 "$app_dir/deploy/verigo-backup.sh" /usr/local/sbin/verigo-backup
install -m 644 "$app_dir/deploy/verigo-backup.service" /etc/systemd/system/verigo-backup.service
install -m 644 "$app_dir/deploy/verigo-backup.timer" /etc/systemd/system/verigo-backup.timer
install -m 700 "$app_dir/deploy/verigo-monitor.sh" /usr/local/sbin/verigo-monitor
install -m 644 "$app_dir/deploy/verigo-monitor.service" /etc/systemd/system/verigo-monitor.service
install -m 644 "$app_dir/deploy/verigo-monitor.timer" /etc/systemd/system/verigo-monitor.timer
install -m 700 "$app_dir/deploy/verigo-retention.sh" /usr/local/sbin/verigo-retention
install -m 644 "$app_dir/deploy/verigo-retention.service" /etc/systemd/system/verigo-retention.service
install -m 644 "$app_dir/deploy/verigo-retention.timer" /etc/systemd/system/verigo-retention.timer
if [[ ! -f /etc/verigo/backup.env ]]; then
    install -m 600 "$app_dir/deploy/verigo-backup.env.example" /etc/verigo/backup.env
fi
if [[ ! -f /etc/verigo/monitor.env ]]; then
    install -m 600 "$app_dir/deploy/verigo-monitor.env.example" /etc/verigo/monitor.env
fi
if [[ ! -f /etc/verigo/retention.env ]]; then
    install -m 600 "$app_dir/deploy/verigo-retention.env.example" /etc/verigo/retention.env
fi
systemctl daemon-reload
systemctl enable --now verigo-backup.timer verigo-monitor.timer verigo-retention.timer

if ! cmp -s "$backup_dir/requirements.txt" "$app_dir/requirements.txt"; then
    "$app_dir/.venv/bin/pip" install --disable-pip-version-check -r "$app_dir/requirements.txt"
fi

for setting in \
    'VERIGO_MAX_GUEST_EMAILS=100' \
    'VERIGO_FREE_SINGLE_DAILY_LIMIT=20' \
    'VERIGO_EMAIL_VERIFICATION_TRIAL_CREDITS=10' \
    'VERIGO_TRIAL_CREDIT_DAYS=7' \
    'VERIGO_MAX_IMPORT_BYTES=5242880' \
    'VERIGO_SESSION_TTL_DAYS=30' \
    'VERIGO_SECURE_COOKIES=true'
do
    key=${setting%%=*}
    if grep -q "^${key}=" /etc/verigo/verigo.env; then
        sed -i "s|^${key}=.*|${setting}|" /etc/verigo/verigo.env
    else
        printf '%s\n' "$setting" >> /etc/verigo/verigo.env
    fi
done

if ! grep -q '^VERIGO_METRICS_SALT=' /etc/verigo/verigo.env; then
    printf 'VERIGO_METRICS_SALT=%s\n' "$(openssl rand -hex 32)" >> /etc/verigo/verigo.env
fi

chown -R verigo:verigo "$app_dir"
chmod 600 /etc/verigo/verigo.env
systemctl restart verigo

for _ in {1..20}; do
    if curl -fsS http://127.0.0.1:8000/api/health >/dev/null; then
        trap - ERR
        printf 'Verigo release %s health check passed\n' "$release_version"
        exit 0
    fi
    sleep 1
done

journalctl -u verigo -n 80 --no-pager >&2
exit 1
