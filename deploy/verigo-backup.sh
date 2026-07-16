#!/usr/bin/env bash
set -Eeuo pipefail

app_dir=/opt/verigo
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

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="$backup_root/$timestamp"
umask 077
install -d -m 700 "$backup_dir"

BACKUP_DIR="$backup_dir" "$app_dir/.venv/bin/python" - <<'PY'
import os
import sqlite3
from pathlib import Path

target = Path(os.environ["BACKUP_DIR"])
for source_name in ("verigo.db", "smtp_limiter.db"):
    source = Path("/opt/verigo/data") / source_name
    if not source.exists():
        continue
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db:
        with sqlite3.connect(target / source_name) as backup_db:
            source_db.backup(backup_db)
            assert backup_db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
PY

legacy_file=$(find "$app_dir" -maxdepth 1 -type f -name '*8.py' -printf '%f\n' -quit)
if [[ -z "$legacy_file" ]]; then
    echo "Legacy verifier source was not found" >&2
    exit 1
fi

tar -C "$app_dir" --exclude='__pycache__' -czf "$backup_dir/application.tar.gz" \
    app static deploy requirements.txt "$legacy_file"
if [[ -d "$app_dir/data/results" ]]; then
    tar -C "$app_dir/data" -czf "$backup_dir/results.tar.gz" results
fi
if [[ -f "$app_dir/domain_type_cache.json" ]]; then
    cp "$app_dir/domain_type_cache.json" "$backup_dir/"
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

find "$backup_root" -mindepth 1 -maxdepth 1 -type d -name '20*' -mtime "+$keep_days" \
    -exec rm -rf -- {} +
