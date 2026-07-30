#!/usr/bin/env bash
set -Eeuo pipefail

worker_dir=${VERIGO_CODEARTS_WORKER_DIR:-"$HOME/verigo-codearts-worker"}
repo_url=${VERIGO_CODEARTS_REPOSITORY:-"https://github.com/ashdarin/Verigo.git"}

read -r -p "Verigo server URL [https://verigo.site]: " server_url
server_url=${server_url:-https://verigo.site}
read -r -s -p "CodeArts worker token: " worker_token
printf '\n'
test -n "$worker_token"
read -r -p "Worker ID [codearts-singapore-1]: " worker_id
worker_id=${worker_id:-codearts-singapore-1}
read -r -p "Initial worker capacity [4]: " worker_capacity
worker_capacity=${worker_capacity:-4}
[[ "$worker_capacity" =~ ^[1-9][0-9]*$ ]] || { echo "Capacity must be a positive integer." >&2; exit 1; }

if [[ -d "$worker_dir/.git" ]]; then
    git -C "$worker_dir" fetch --depth=1 origin main
    git -C "$worker_dir" reset --hard origin/main
else
    git clone --depth=1 "$repo_url" "$worker_dir"
fi

python3 -m venv "$worker_dir/.venv"
"$worker_dir/.venv/bin/pip" install --disable-pip-version-check --no-cache-dir -r "$worker_dir/requirements.txt"
umask 077
cat > "$worker_dir/.codearts-worker.env" <<EOF
VERIGO_REMOTE_WORKER_TARGET=codearts
VERIGO_REMOTE_WORKER_SERVER=$server_url
VERIGO_REMOTE_WORKER_TOKEN=$worker_token
VERIGO_REMOTE_WORKER_ID=$worker_id
VERIGO_REMOTE_WORKER_CAPACITY=$worker_capacity
VERIGO_TENCENT_QQ_POLL_SECONDS=0.25
VERIGO_TENCENT_QQ_RETRY_SECONDS=5
EOF
cat > "$worker_dir/run-codearts-worker.sh" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
set -a
. ./.codearts-worker.env
set +a
exec .venv/bin/python -m app.tencent_qq_worker
EOF
chmod 700 "$worker_dir/run-codearts-worker.sh"

if [[ -f "$worker_dir/codearts-worker.pid" ]]; then
    old_pid=$(cat "$worker_dir/codearts-worker.pid")
    if kill -0 "$old_pid" 2>/dev/null; then
        kill "$old_pid"
        for _ in {1..20}; do kill -0 "$old_pid" 2>/dev/null || break; sleep 1; done
    fi
fi
nohup "$worker_dir/run-codearts-worker.sh" > "$worker_dir/codearts-worker.log" 2>&1 &
echo $! > "$worker_dir/codearts-worker.pid"
sleep 2
kill -0 "$(cat "$worker_dir/codearts-worker.pid")"
echo "CodeArts worker started. Log: $worker_dir/codearts-worker.log"
