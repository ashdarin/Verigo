# Stage A — Code freeze / publish readiness (PostgreSQL cutover)

**Goal:** freeze and publish cutover code while production remains on **SQLite** (`VERIGO_POSTGRES_ENABLED=false`). Do **not** flip the Postgres switch in this stage.

**Workspace:** `C:\Users\hgl18721713285\Desktop\Verigo`  
**Related runbook:** [`postgres-cutover-runbook.md`](../postgres-cutover-runbook.md) (full A–G procedure)

---

## 0. Preconditions (this tree)

| Check | Status / action |
|--------|------------------|
| `requirements.txt` includes `psycopg[binary]>=3.1,<4` | **Present** (line 12) |
| Tunnel unit `deploy/verigo-postgres-tunnel.service` | **Present** (SSH local forward `127.0.0.1:15432` → PG host) |
| Soft deps on tunnel | `deploy/verigo.service`, `deploy/verigo-worker@.service`, `deploy/verigo-supervisor.service` use `Wants=` / `After=` tunnel |
| `deploy/release.sh` installs tunnel unit | **Does not** currently `install` `verigo-postgres-tunnel.service` — ensure unit is already on the host or install manually before relying on `systemctl is-active verigo-postgres-tunnel` |
| Git in this folder | **Not available** as of Stage A prep (`git` not on PATH; no `.git` directory). `deploy/publish.ps1` requires `git rev-parse HEAD` + `git archive HEAD`. Restore a Git repo + Git for Windows before commit/publish. |
| Secrets / local DB | Do **not** ship: `postgres.env`, `verigo.env.live`, `verigo.env.server`, `*.db`, `.server-staging/` (see `.gitignore`) |

---

## 1. Inventory — files that should ship with the cutover commit

Ship these paths (and any other app/deploy deltas needed for dual-backend safety). Paths are relative to repo root.

### App DB / cutover core

- `app/db/postgresql.py`
- `app/db/postgres_schema.py`
- `app/db/postgres_shadow.py`
- `app/db/postgres_migrate.py`
- `app/db/pg_compat.py`
- `app/db/backend_ops.py`
- `app/db/retention.py`
- `app/config.py` (Postgres flags + `_validate_postgres_settings`)
- Related store modules if changed for dual backend: `app/db/jobs.py`, `app/db/auth.py`, `app/db/metrics.py`, `app/db/prospecting.py`, `app/db/sqlite.py`

### Scripts

- `scripts/migrate_sqlite_to_postgres.py`
- `scripts/postgres_cutover_preflight.py`
- `scripts/postgres_schema_inventory.py`
- `scripts/cutover_snapshot_and_migrate.py`

### Deploy

- `deploy/verigo-postgres-tunnel.service`
- `deploy/verigo.service` (tunnel `Wants`/`After`)
- `deploy/verigo-worker@.service`
- `deploy/verigo-supervisor.service`
- `deploy/verigo.env.example` (`VERIGO_POSTGRES_ENABLED=false`, DSN comments)
- `deploy/verigo-retention.sh` / `.service` / `.timer` / `.env.example`
- `deploy/verigo-backup.sh` (optional PG dump via tunnel)
- `deploy/verigo-monitor.sh` (backend-aware probe via `backend_ops`)
- `deploy/release.sh` (backend_ops drain helpers; unit installs)
- `deploy/publish.ps1`

### Tests (Postgres-focused)

- `tests/postgres_adapter_smoke.py`
- `tests/postgres_claim_rewrite_smoke.py`
- `tests/postgres_config_guard_smoke.py`
- `tests/postgres_cutover_preflight_smoke.py`
- `tests/postgres_digest_roundtrip_smoke.py`
- `tests/postgres_jobstore_sqlite_regression_smoke.py`
- `tests/postgres_migration_smoke.py`
- `tests/postgres_schema_smoke.py`
- Plus dual-backend regression: `tests/backend_smoke.py`, `tests/job_store_refactor_smoke.py`, `tests/metrics_duration_smoke.py`, `tests/prospecting_claim_smoke.py`, `tests/remote_worker_smoke.py`, `tests/worker_lifecycle_smoke.py`

### Dependencies / ignore / docs

- `requirements.txt` (`psycopg[binary]`)
- `.gitignore` (`postgres.env`, `postgres-cutover-*/`, `*.db`, secrets)
- `postgres-cutover-runbook.md`
- `docs/cutover-a-stage-checklist.md` (this file)

### Do **not** commit

- `postgres.env`, `/etc/verigo/postgres.env` contents, live DSNs
- `verigo.env.live`, `verigo.env.server`, `*.pem`, `*.db*`
- `.server-staging/*`, `_upstream_ref/*`, cutover backup trees `postgres-cutover-*/`

---

## 2. Commit message (when git works)

```powershell
Set-Location 'C:\Users\hgl18721713285\Desktop\Verigo'

# Confirm repo + clean review
git status --short
git diff --check
git diff --stat

# Stage ship-set (adjust if more dual-backend files changed)
git add `
  app `
  deploy `
  scripts `
  tests `
  requirements.txt `
  .gitignore `
  postgres-cutover-runbook.md `
  docs/cutover-a-stage-checklist.md

git commit -m "feat: add PostgreSQL production cutover support (Stage A SQLite-safe)"

git status --short
git rev-parse HEAD
```

Record the 40-char SHA as `<CUTOVER_COMMIT>`.

**Suggested `git status --short` scope (paths only, no commit):**

```powershell
git status --short -- `
  app/db/postgresql.py app/db/postgres_schema.py app/db/postgres_shadow.py `
  app/db/postgres_migrate.py app/db/pg_compat.py app/db/backend_ops.py `
  app/db/retention.py app/config.py `
  scripts/migrate_sqlite_to_postgres.py scripts/postgres_cutover_preflight.py `
  scripts/postgres_schema_inventory.py scripts/cutover_snapshot_and_migrate.py `
  deploy/verigo-postgres-tunnel.service deploy/verigo.service `
  deploy/verigo-worker@.service deploy/verigo-supervisor.service `
  deploy/verigo.env.example deploy/verigo-retention.sh deploy/verigo-backup.sh `
  deploy/verigo-monitor.sh deploy/release.sh deploy/publish.ps1 `
  tests/postgres_*.py requirements.txt .gitignore `
  postgres-cutover-runbook.md docs/cutover-a-stage-checklist.md
```

---

## 3. Local tests to run (before publish)

From repo root, with project venv / deps installed (`pip install -r requirements.txt`):

```powershell
Set-Location 'C:\Users\hgl18721713285\Desktop\Verigo'

python -m compileall -q app scripts tests

# Postgres-focused smokes (real paths in this repo)
python tests/postgres_config_guard_smoke.py
python tests/postgres_migration_smoke.py
python tests/postgres_cutover_preflight_smoke.py
python tests/postgres_schema_smoke.py
python tests/postgres_adapter_smoke.py
python tests/postgres_claim_rewrite_smoke.py
python tests/postgres_digest_roundtrip_smoke.py
python tests/postgres_jobstore_sqlite_regression_smoke.py

# Dual-backend / job path regression
python tests/backend_smoke.py
python tests/job_store_refactor_smoke.py
python tests/metrics_duration_smoke.py
python tests/prospecting_claim_smoke.py
python tests/remote_worker_smoke.py
python tests/worker_lifecycle_smoke.py
```

Expect exit code `0` for every command.  
Note: older runbook listed `tests/auth_postgres_adapter_smoke.py` — that file is **not** in this tree; use `tests/postgres_adapter_smoke.py` instead.

Optional when git works: `git diff --check`.

---

## 4. `publish.ps1` SQLite-mode deploy notes

`deploy/publish.ps1` **only packages committed `HEAD`** via `git archive HEAD`. Uncommitted cutover files will **not** reach production.

### Pre-publish (production host)

```bash
grep -E '^(VERIGO_POSTGRES_ENABLED|VERIGO_DATABASE_PATH|VERIGO_DATABASE_URL)=' /etc/verigo/verigo.env || true
```

**Required for Stage A:**

```text
VERIGO_DATABASE_PATH=/opt/verigo/data/verigo.db
VERIGO_POSTGRES_ENABLED=false
```

- Do **not** set `VERIGO_POSTGRES_ENABLED=true`.
- Do **not** merge `/etc/verigo/postgres.env` into `verigo.env` yet (that is Stage E).
- Do **not** use `.\deploy\publish.ps1 -Maintenance` as a substitute for cutover drain (Stage C).

### Publish (Windows, after `<CUTOVER_COMMIT>` is `HEAD`)

```powershell
Set-Location 'C:\Users\hgl18721713285\Desktop\Verigo'
.\deploy\publish.ps1
```

Requirements of the script:

- Git for Windows (`git`, plus `ssh.exe`/`scp.exe` under `Program Files\Git\usr\bin`)
- Deploy key default: `$env:USERPROFILE\.ssh\verigo_deploy_ed25519`
- Known hosts: `$env:USERPROFILE\.ssh\verigo_known_hosts`
- Remote: `verigo-deploy@103.242.2.226` → uploads archive → `deploy/release.sh`

`release.sh` will install/update units (including tunnel **Wants** on web/worker/supervisor), pip-install `requirements.txt` (pulls `psycopg`), and restart services. Tunnel unit file must already be installed/enabled on the host if you assert tunnel is `active` (see §0).

---

## 5. Post-deploy verification greps / checks

On the production host after SQLite-mode publish:

```bash
# Release identity matches cutover commit
cat /opt/verigo/current/RELEASE_VERSION
# expect: <CUTOVER_COMMIT>

# Still SQLite mode — critical greps
grep -E '^(VERIGO_POSTGRES_ENABLED|VERIGO_DATABASE_PATH|VERIGO_DATABASE_URL)=' /etc/verigo/verigo.env
# expect: VERIGO_POSTGRES_ENABLED=false (or unset/false)
# expect: VERIGO_DATABASE_PATH=/opt/verigo/data/verigo.db
# expect: no live production traffic on VERIGO_DATABASE_URL yet

# Cutover code present in release tree
test -f /opt/verigo/current/app/db/postgresql.py
test -f /opt/verigo/current/app/db/pg_compat.py
test -f /opt/verigo/current/app/db/backend_ops.py
test -f /opt/verigo/current/app/db/retention.py
test -f /opt/verigo/current/scripts/migrate_sqlite_to_postgres.py
test -f /opt/verigo/current/scripts/postgres_cutover_preflight.py
test -f /opt/verigo/current/deploy/verigo-postgres-tunnel.service
grep -n 'psycopg' /opt/verigo/current/requirements.txt
grep -n 'verigo-postgres-tunnel' /opt/verigo/current/deploy/verigo.service

# Health + units
curl -fsS http://127.0.0.1:8000/api/health
systemctl is-active verigo verigo-supervisor verigo-worker@1 verigo-worker@2
systemctl is-active verigo-postgres-tunnel || echo "tunnel inactive (OK if SQLite-only and unit not required yet)"

# Confirm runtime still SQLite-backed (env must remain false)
/opt/verigo/.venv/bin/python - <<'PY'
from app.config import settings
print("postgres_enabled=", settings.postgres_enabled)
print("database_path=", settings.database_path)
assert settings.postgres_enabled is False, "Stage A must stay on SQLite"
print("ok")
PY
```

**Pass criteria for Stage A:**

1. `RELEASE_VERSION` == `<CUTOVER_COMMIT>`
2. Health `{"status":"ok","database":"ok"}` (or equivalent ok)
3. `VERIGO_POSTGRES_ENABLED=false` still true on host
4. Core cutover modules + `psycopg` line present under `/opt/verigo/current`
5. Web/workers/supervisor active on SQLite

Then proceed to runbook **Stage B** (infra/tunnel/preflight), not Stage E switch.

---

## 6. Stage A done definition

- [ ] Inventory files above are committed on a real Git `HEAD`
- [ ] Local smokes in §3 all exit 0
- [ ] `publish.ps1` ran against that commit only
- [ ] Post-deploy greps show SQLite mode + cutover code present
- [ ] No production traffic on PostgreSQL

**Do not** run final migrate / env merge / `VERIGO_POSTGRES_ENABLED=true` until Stages C–E of `postgres-cutover-runbook.md`.
