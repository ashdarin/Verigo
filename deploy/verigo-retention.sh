#!/usr/bin/env bash
set -Eeuo pipefail

cd /opt/verigo/current
config=/etc/verigo/retention.env
if [[ -r "$config" ]]; then
    # shellcheck disable=SC1090
    source "$config"
fi
if [[ -r /etc/verigo/verigo.env ]]; then
    set -a
    # shellcheck disable=SC1091
    source /etc/verigo/verigo.env
    set +a
fi

results_days=${VERIGO_RESULTS_RETENTION_DAYS:-30}
job_days=${VERIGO_JOB_RETENTION_DAYS:-90}
analytics_unique_days=${VERIGO_ANALYTICS_UNIQUE_RETENTION_DAYS:-45}
if ! [[ "$results_days" =~ ^[1-9][0-9]*$ && "$job_days" =~ ^[1-9][0-9]*$ \
    && "$analytics_unique_days" =~ ^[1-9][0-9]*$ ]]; then
    echo "Retention periods must be positive integers" >&2
    exit 1
fi

# Backend-aware retention lives in app.db.retention so SQLite and PostgreSQL
# share the same deletion policy after cutover.
RESULTS_DAYS="$results_days" JOB_DAYS="$job_days" ANALYTICS_UNIQUE_DAYS="$analytics_unique_days" \
  PYTHONPATH=/opt/verigo/current /opt/verigo/.venv/bin/python - <<'PY'
import json
import os
from app.db.retention import run_retention

summary = run_retention(
    results_days=int(os.environ["RESULTS_DAYS"]),
    job_days=int(os.environ["JOB_DAYS"]),
    analytics_unique_days=int(os.environ["ANALYTICS_UNIQUE_DAYS"]),
)
print(json.dumps(summary, ensure_ascii=False))
PY
