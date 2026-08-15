#!/usr/bin/env bash
set -Eeuo pipefail

state_dir=/opt/verigo
app_dir="$state_dir/current"
data_dir="$state_dir/data"
venv_python="$state_dir/.venv/bin/python"
backup_root=/var/backups/verigo
backup_config=/etc/verigo/backup.env

if [[ -r "$backup_config" ]]; then
    # shellcheck disable=SC1090
    source "$backup_config"
fi

keep_days=${VERIGO_BACKUP_KEEP_DAYS:-14}
if ! [[ "$keep_days" =~ ^[1-9][0-9]*$ ]]; then
    echo "VERIGO_BACKUP_KEEP_DAYS must be a positive integer" >&2
    exit 1
fi

# Deploys can be frequent. Keep a small number of the newest complete local
# snapshots as a hard disk-space ceiling, independent of age-based retention.
keep_count=${VERIGO_BACKUP_KEEP_COUNT:-3}
if ! [[ "$keep_count" =~ ^[1-9][0-9]*$ ]]; then
    echo "VERIGO_BACKUP_KEEP_COUNT must be a positive integer" >&2
    exit 1
fi

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="$backup_root/$timestamp"
umask 077
install -d -m 700 "$backup_dir"

BACKUP_DIR="$backup_dir" "$venv_python" - <<'PY'
import os
import sqlite3
from pathlib import Path

target = Path(os.environ["BACKUP_DIR"])
for source_name in ("verigo.db", "smtp_limiter.db", "name_catalog.db"):
    source = Path("/opt/verigo/data") / source_name
    if not source.exists():
        continue
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db:
        with sqlite3.connect(target / source_name) as backup_db:
            source_db.backup(backup_db)
            assert backup_db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
PY

# Optional PostgreSQL dump through the local SSH tunnel. Safe while still on
# SQLite: dumps the shadow/cutover database for recovery rehearsal.
if [[ -r /etc/verigo/postgres.env ]]; then
    set -a
    # shellcheck disable=SC1091
    source /etc/verigo/postgres.env
    set +a
fi
if [[ -n "${VERIGO_DATABASE_URL:-}" ]]; then
    if command -v pg_dump >/dev/null 2>&1; then
        # custom format; credentials come from the URL, never echoed.
        BACKUP_DIR="$backup_dir" DATABASE_URL="$VERIGO_DATABASE_URL" \
          "$venv_python" - <<'PY'
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse, unquote

url = os.environ["DATABASE_URL"]
parsed = urlparse(url)
env = os.environ.copy()
if parsed.password:
    env["PGPASSWORD"] = unquote(parsed.password)
host = parsed.hostname or "127.0.0.1"
port = str(parsed.port or 5432)
user = unquote(parsed.username or "verigo")
dbname = (parsed.path or "/verigo").lstrip("/") or "verigo"
out = Path(os.environ["BACKUP_DIR"]) / "verigo_postgres.dump"
cmd = [
    "pg_dump",
    "--format=custom",
    "--no-owner",
    "--no-acl",
    "-h", host,
    "-p", port,
    "-U", user,
    "-d", dbname,
    "-f", str(out),
]
subprocess.check_call(cmd, env=env)
subprocess.check_call(["pg_restore", "--list", str(out)], stdout=subprocess.DEVNULL, env=env)
print(f"postgres_dump={out} bytes={out.stat().st_size}")
PY
    else
        echo "pg_dump not installed; skipping PostgreSQL dump" >&2
    fi
fi

legacy_file=$(find -L "$app_dir" -maxdepth 1 -type f -name '*8.py' -printf '%f\n' -quit)
if [[ -z "$legacy_file" ]]; then
    echo "Legacy verifier source was not found" >&2
    exit 1
fi

tar -C "$app_dir" --exclude='__pycache__' -czf "$backup_dir/application.tar.gz" \
    app static deploy requirements.txt "$legacy_file"
if [[ -d "$data_dir/results" ]]; then
    tar -C "$data_dir" -czf "$backup_dir/results.tar.gz" results
fi

cp /etc/verigo/verigo.env "$backup_dir/verigo.env"
cp /etc/caddy/Caddyfile "$backup_dir/Caddyfile"
cp /etc/systemd/system/verigo.service "$backup_dir/verigo.service"
if [[ -f /etc/systemd/system/verigo-worker@.service ]]; then
    cp /etc/systemd/system/verigo-worker@.service "$backup_dir/"
fi
sha256sum "$backup_dir"/* > "$backup_dir/SHA256SUMS"
chmod 600 "$backup_dir"/*

if [[ -n "${VERIGO_BACKUP_RSYNC_TARGET:-}" ]]; then
    rsync -a --chmod=Du=rwx,Dgo=,Fu=rw,Fgo= "$backup_dir/" \
        "${VERIGO_BACKUP_RSYNC_TARGET%/}/$timestamp/"
fi

if [[ -n "${VERIGO_BACKUP_S3_BUCKET:-}" ]]; then
    : "${VERIGO_BACKUP_S3_ENDPOINT:?VERIGO_BACKUP_S3_ENDPOINT is required for S3 backups}"
    : "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID is required for S3 backups}"
    : "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY is required for S3 backups}"
    command -v aws >/dev/null || { echo "aws CLI is required for S3 backups" >&2; exit 1; }
    export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
    AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-auto} aws s3 cp "$backup_dir/" \
        "s3://${VERIGO_BACKUP_S3_BUCKET%/}/verigo/$timestamp/" --recursive --only-show-errors \
        --endpoint-url "$VERIGO_BACKUP_S3_ENDPOINT"
fi

find "$backup_root" -mindepth 1 -maxdepth 1 -type d -name '20*' -mtime "+$keep_days" \
    -exec rm -rf -- {} +

mapfile -t backup_dirs < <(find "$backup_root" -mindepth 1 -maxdepth 1 -type d -name '20*' -printf '%f\n' | sort -r)
if (( ${#backup_dirs[@]} > keep_count )); then
    for expired_dir in "${backup_dirs[@]:keep_count}"; do
        rm -rf -- "$backup_root/$expired_dir"
    done
fi

install -d -m 700 /var/lib/verigo-backup
date -u +%Y-%m-%dT%H:%M:%SZ > /var/lib/verigo-backup/last-success
