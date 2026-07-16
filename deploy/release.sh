#!/usr/bin/env bash
set -Eeuo pipefail

release_dir=${VERIGO_RELEASE_DIR:-/tmp/verigo-release}
app_dir=/opt/verigo
backup_dir=/opt/verigo-backup-current

rollback() {
    echo "Release failed; restoring previous application files" >&2
    rsync -a --delete "$backup_dir/app/" "$app_dir/app/"
    rsync -a --delete "$backup_dir/static/" "$app_dir/static/"
    rsync -a --delete "$backup_dir/deploy/" "$app_dir/deploy/"
    cp "$backup_dir/requirements.txt" "$app_dir/requirements.txt"
    cp "$backup_dir/验证8.py" "$app_dir/验证8.py"
    chown -R verigo:verigo "$app_dir"
    systemctl start verigo
}
test -f "$release_dir/app/main.py"
test -f "$release_dir/验证8.py"

mkdir -p "$backup_dir"
rsync -a --delete "$app_dir/app/" "$backup_dir/app/"
rsync -a --delete "$app_dir/static/" "$backup_dir/static/"
rsync -a --delete "$app_dir/deploy/" "$backup_dir/deploy/"
cp "$app_dir/requirements.txt" "$backup_dir/requirements.txt"
cp "$app_dir/验证8.py" "$backup_dir/验证8.py"

trap rollback ERR

systemctl stop verigo
rsync -a --delete --exclude='__pycache__' "$release_dir/app/" "$app_dir/app/"
rsync -a --delete "$release_dir/static/" "$app_dir/static/"
rsync -a --delete "$release_dir/deploy/" "$app_dir/deploy/"
cp "$release_dir/requirements.txt" "$app_dir/requirements.txt"
cp "$release_dir/验证8.py" "$app_dir/验证8.py"

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

chown -R verigo:verigo "$app_dir"
chmod 600 /etc/verigo/verigo.env
systemctl start verigo

for _ in {1..20}; do
    if curl -fsS http://127.0.0.1:8000/api/health >/dev/null; then
        trap - ERR
        echo "Verigo release health check passed"
        exit 0
    fi
    sleep 1
done

journalctl -u verigo -n 80 --no-pager >&2
exit 1
