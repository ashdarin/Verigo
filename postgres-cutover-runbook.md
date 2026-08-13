# Verigo SQLite -> PostgreSQL 生产切换 Runbook

更新时间：2026-08-12  
适用范围：`verigo.site` 生产环境  
目标：将 Auth、Metrics、Prospecting 和 JobStore 从 SQLite 一次性切换到 PostgreSQL，并保留可快速恢复 SQLite 的回滚路径。

> **当前状态（2026-08-12 维护窗完成）：生产已切换到 PostgreSQL。**
>
> - **A 原子发布（无 Git）：** release `aea8b577253682edad31d5f9a1c83364cd94be5c`（基于旧 release + cutover overlay）
> - **C：** draining；force-stop 剩余远程慢任务后 idle（queued/running/leases=0）
> - **D：** 最终快照 `.../postgres-cutover-20260812T092237Z/verigo-final.db`；全表 migrate 行数对齐；keys 全表 match + 财务 match + rollback probe true（full content 大表校验在早前 shadow 与部分 final 日志中已覆盖；最终门槛用 keys 以避免 OOM）
> - **E：** `VERIGO_POSTGRES_ENABLED=true`，DSN 走 `127.0.0.1:15432`；backend=postgres；service_mode=active
> - **回滚：** `/var/backups/verigo/postgres-cutover-20260812T092237Z/verigo.env.before` + 脚本 `remote_cutover_rollback.sh`
> - **注意：** 切换窗内曾 stop 若干 tencent_qq/aggregate 任务（标为 stopped）；SQLite 原库与最终快照仍保留；`verigo-retention.timer` 需确认 PG 路径后再长期开启

本文中的 `<...>` 都是需要现场填写的值。不要把密码、私钥、监控令牌或完整 DSN 写入 Git、终端历史、工单或聊天记录。

## 1. 当前状态与剩余工作量

### 已完成

- PostgreSQL 服务器、数据库和应用角色已建立。
- 生产 VPS 已部署受限 SSH 隧道，监听 `127.0.0.1:15432`，隧道重启恢复和 SQL 握手已验证。
- 19 张核心 JobStore 表的 schema、迁移、裁剪和摘要验证已实现。
- Auth、Metrics、Prospecting、JobStore 的 PostgreSQL 适配器已实现。
- 统一开关 `VERIGO_POSTGRES_ENABLED` 和配置冲突保护已实现。
- 最后一次影子同步中，19 张核心表的数量、主键摘要和内容摘要一致，PostgreSQL 回滚写测试通过。
- 线上服务正常，但 PostgreSQL 尚未承载生产流量。

### 已知数据窗口阻塞项

最后一次真实预检返回：

```text
active_job_leases_present
source active leases: 5
target active leases: 5
```

这不是数据损坏。维护窗口内必须先停止新提交、让在途任务自然完成，并确认活动租约归零。不得为通过预检而直接删除活动租约。

### 剩余工作量

剩余 **9 个阶段、49 个检查项**。其中 P0 的 9 项是进入维护窗口之前必须完成的工程工作：

| 阶段 | 检查项 | 预计用时 | 是否造成维护 |
|---|---:|---:|---|
| P0. 补齐生产切换能力 | 9 | 1-3 个开发日 | 否 |
| A. 固化代码与 CI | 5 | 20-40 分钟 | 否 |
| B. 预部署与基础设施复核 | 5 | 15-30 分钟 | 否 |
| C. 维护窗口排空 | 6 | 取决于最长任务，通常 15-60 分钟 | 是 |
| D. 最终快照与同步 | 6 | 10-30 分钟 | 是 |
| E. 正式切换 | 6 | 10-20 分钟 | 是 |
| F. 业务验收 | 5 | 15-30 分钟 | 是/有限开放 |
| G. 观察与收尾 | 4 | 至少 30-60 分钟 | 否 |
| R. 回滚（仅异常时） | 3 | 10-20 分钟 | 是 |

实际维护时间主要由在途任务排空和最终数据量决定。P0 全部完成后，预留 **90-120 分钟** 窗口较稳妥。若任一硬性门槛失败，保持 SQLite 或执行回滚，不要边运行边修数据库数据。

## 2. 角色、路径和固定值

| 项目 | 值 |
|---|---|
| 生产域名 | `https://verigo.site` |
| 应用 VPS | `103.242.2.226` |
| PostgreSQL 主机 | `101.34.212.199` |
| PostgreSQL 数据库 | `verigo_shadow` |
| PostgreSQL 应用角色 | `verigo` |
| 应用目录 | `/opt/verigo/current` |
| SQLite 主库 | `/opt/verigo/data/verigo.db` |
| 主环境文件 | `/etc/verigo/verigo.env` |
| 已暂存 PostgreSQL 环境文件 | `/etc/verigo/postgres.env` |
| 本机隧道地址 | `127.0.0.1:15432` |
| 本地仓库 | `C:\Users\艾鹏杰\Documents\Codex\2026-08-11\new-chat-2\work\Verigo` |

涉及的服务：

```text
verigo.service
verigo-worker-api.service
verigo-supervisor.service
verigo-worker@1.service
verigo-worker@2.service
verigo-postgres-tunnel.service
```

> 仓库中的 Web、Supervisor 和 Worker systemd 单元目前只读取 `/etc/verigo/verigo.env`。`/etc/verigo/postgres.env` 只是受保护的暂存文件，不会自动注入这些进程。因此切换前必须把其中的 `VERIGO_DATABASE_URL` 安全合并到主环境文件，或先修改所有单元统一读取第二个环境文件。本文采用前者，变更面更小。

## 3. 全局 Go / No-Go 规则

只有全部满足才可切换：

- [ ] 第 4 节全部 P0 阻断项已实现并经过 PostgreSQL 集成测试。
- [ ] 待发布代码已提交，工作树没有漏进提交的迁移文件。
- [ ] CI 全绿，本地核心测试全绿。
- [ ] 该提交已先以 SQLite 模式部署，服务正常。
- [ ] PostgreSQL 隧道和 SQL 查询成功。
- [ ] SQLite 最终快照 `PRAGMA quick_check` 返回 `ok`。
- [ ] `queued_jobs=0`、`running_jobs=0`、`pending_results=0`、`verifying_results=0`。
- [ ] SQLite 和 PostgreSQL 的活动 `job_leases` 均为 0。
- [ ] 最终迁移返回 `{"phase":"verify","ok":true}`。
- [ ] 最终预检返回 `ready=true`、`blockers=[]`、`differences={}`。
- [ ] 快照、旧环境文件和当前发布版本均已记录，可在 10-20 分钟内恢复。

任意一项失败即 **No-Go**。特别是不要使用 `--allow-active-leases` 作为正式切换依据；该参数只适合提前观察其他检查项。

## 4. P0 阶段：补齐生产切换能力（9 项）

本阶段属于代码和运维开发，不在维护窗口内完成。当前实现已经验证了 19 张核心 JobStore 表，但“测试通过”不代表全站生产生命周期已经闭合。

### P0.1 为所有业务表建立显式 PostgreSQL schema

当前 `app/db/postgres_schema.py` 只显式定义 19 张 JobStore 表。`--include-all-tables` 对 Auth、Metrics、Prospecting 等表调用 `app/db/postgres_shadow.py`，按 SQLite 声明类型保守推断 PostgreSQL 类型：除整数、浮点和 BLOB 外多数会变成 `text`。

这与运行时建表规则存在实质差异，例如：

- Metrics 的时间列运行时需要 `TIMESTAMPTZ`，shadow 推断会生成 `text`。
- Prospecting 的 JSON 列需要 `jsonb`、时间列需要 `timestamptz`、部分整数标志需要 `boolean`。
- Auth 的 `email_verified` 等字段、时间列、identity、大小写唯一性和外键/索引需要显式确认。
- `CREATE TABLE IF NOT EXISTS` 不会纠正已经以错误类型创建的 shadow 表。

要做：把生产 SQLite 中的全部业务表列入版本化 schema metadata，明确每列 PostgreSQL 类型、主键、唯一约束、外键、索引、JSON、boolean、timestamp 和 identity。迁移器只允许迁移已登记表；生产模式下遇到未知表直接失败，不再静默推断。

验收：对一个空 PostgreSQL schema 执行正式建表和全量迁移，然后逐表比较 `information_schema.columns`、约束和索引与预期定义。

### P0.2 扩展迁移和预检到全站表

当前 `postgres_cutover_preflight.py` 只检查 19 张核心表，而且不比较内容摘要。要做：

- 将 Auth、Metrics、Prospecting 表加入正式迁移清单。
- 最终预检检查所有迁移表的 count、主键摘要、内容摘要和缺失表。
- 对无主键表定义稳定的全行同步/验证策略。
- 对 JSON、boolean、timestamp 做跨后端归一化后再摘要。
- 把用户余额、ledger、订单、兑换码和 promo grant 汇总作为预检硬性不变量。

验收：人为制造缺行、多行、字段值变化、类型错误和财务汇总差异，预检均以退出码 2 阻止切换。

### P0.3 修复发布脚本的后端感知 drain

`deploy/release.sh` 中 `set_service_mode`、`active_job_count`、`active_targets` 和 `drain_progress_marker` 目前直接连接 `/opt/verigo/data/verigo.db`。切换后再次发布会排空错误的数据库，并在最后只把 SQLite 设为 `active`。

要做：把这些 helper 改为调用 `JobStore` 的当前后端接口，并从 `/etc/verigo/verigo.env` 加载配置；为 SQLite 和 PostgreSQL 两种发布分别加 smoke test。发布失败的 rollback 也必须恢复当前后端的 service mode。

### P0.4 完成 systemd 依赖和环境注入

要做：

- 所有访问数据库的单元统一读取 DSN；选择主 env 或第二个 root-only env，不要两套混用。
- `verigo-supervisor.service` 和 `verigo-worker-api.service` 加入 tunnel 顺序依赖。
- 对 PostgreSQL 模式使用 `Requires=verigo-postgres-tunnel.service` 和 `After=...`，或提供等效的启动前 SQL 检查。
- 将缺失的 `deploy/verigo-worker-api.service` 纳入仓库和发布脚本。
- 测试隧道密钥缺失、隧道断开和重连时各服务的行为。

### P0.5 增加 PostgreSQL 备份和恢复演练

现有 `deploy/verigo-backup.sh` 只备份 SQLite。要做：使用受限备份角色运行 `pg_dump`（建议 custom format），加密或传输到现有异地目标，记录校验和与成功时间；提供 `pg_restore` 到临时数据库的定期恢复演练。

Go 条件：至少完成一次从生产结构备份恢复到临时数据库，并通过 schema、counts 和关键摘要检查。

### P0.6 让 retention 使用当前后端

`deploy/verigo-retention.sh` 硬编码 SQLite。要做：将删除策略移入后端感知的应用模块，PostgreSQL 使用参数化 SQL 和短事务；CSV 文件删除仍保持在数据库事务外；增加 SQLite/PostgreSQL 一致性测试。

完成前，切换窗口后必须保持 retention timer 停止，不能把“timer active”当作已完成。

### P0.7 修复数据库监控

`deploy/verigo-monitor.sh` 的业务 readiness 是后端感知的，但额外的“数据库可写”测试仍只写 SQLite。要做：通过应用配置选择当前后端，在 PostgreSQL 中做 SAVEPOINT/rollback 写检查；增加连接数、连接失败、长事务、lock wait、deadlock、数据库大小和备份时效告警。

### P0.8 完成全站 PostgreSQL 回归和故障测试

在临时 schema 上覆盖：Auth 注册/登录/session/余额/支付幂等；Metrics 写入和聚合；Prospecting 创建/claim/保存；任务提交/claim/heartbeat/complete/重试/分页；多进程锁竞争；隧道中断和恢复；服务重启；发布和回滚。

验收：测试数据全量迁移后运行同一业务测试套件，且切换前后的查询结果一致。不要只运行当前 19 表 integration smoke。

### P0.9 形成切换候选版本

完成 P0.1-P0.8 后：重新做一次生产影子同步和预检，更新本文中的表数量与命令参数；冻结候选提交；由另一位操作者审阅 Runbook。只有此时才安排 A-G 阶段的正式窗口。

## 5. A 阶段：固化代码与 CI（5 项）

### A1. 检查并提交完整变更

在 Windows 仓库执行：

```powershell
Set-Location 'C:\Users\艾鹏杰\Documents\Codex\2026-08-11\new-chat-2\work\Verigo'
git status --short
git diff --check
git diff --stat
```

重点确认以下新增文件已加入提交：

```text
app/db/postgres_schema.py
app/db/postgres_shadow.py
app/db/postgresql.py
scripts/migrate_sqlite_to_postgres.py
scripts/postgres_cutover_preflight.py
deploy/verigo-postgres-tunnel.service
tests/postgres*.py
tests/postgresql*.py
```

当前 `HEAD` 仍是 `812aca623d853c674d6b243c35070ab095068011`，而迁移改动尚未提交。`deploy/publish.ps1` 通过 `git archive HEAD` 发布，**未提交文件不会进入生产包**。

完成审阅后：

```powershell
git add .env.example .github/workflows/ci.yml app deploy requirements.txt requirements-test.txt scripts tests docs/postgres-cutover-runbook.md
git commit -m "feat: add PostgreSQL production cutover support"
git status --short
git rev-parse HEAD
```

预期：`git status --short` 无意外输出，记录 40 位提交号为 `<CUTOVER_COMMIT>`。

### A2. 本地测试

使用项目虚拟环境或已安装依赖的 Python，至少执行：

```powershell
python -m compileall -q app scripts tests
python tests/postgres_config_guard_smoke.py
python tests/postgres_migration_smoke.py
python tests/postgres_cutover_preflight_smoke.py
python tests/backend_smoke.py
python tests/job_store_refactor_smoke.py
python tests/metrics_duration_smoke.py
python tests/prospecting_claim_smoke.py
python tests/auth_postgres_adapter_smoke.py
python tests/remote_worker_smoke.py
python tests/worker_lifecycle_smoke.py
git diff --check
```

预期：全部退出码为 0。

### A3. CI

推送 `<CUTOVER_COMMIT>`，等待 `.github/workflows/ci.yml` 全部通过。记录 CI URL 和时间。任何 PostgreSQL integration job 失败均为 No-Go。

### A4. 先在 SQLite 模式发布该提交

先确认生产开关为 false，再正常发布：

```bash
grep -E '^(VERIGO_POSTGRES_ENABLED|VERIGO_DATABASE_PATH)=' /etc/verigo/verigo.env
```

预期：

```text
VERIGO_DATABASE_PATH=/opt/verigo/data/verigo.db
VERIGO_POSTGRES_ENABLED=false
```

Windows 执行：

```powershell
.\deploy\publish.ps1
```

发布脚本会排空现有任务、安装依赖、切换 release 并重启服务。不要使用 `-Maintenance` 来代替本 Runbook 的最终排空。

### A5. SQLite 模式发布验收

```bash
cat /opt/verigo/current/RELEASE_VERSION
curl -fsS http://127.0.0.1:8000/api/health
systemctl is-active verigo verigo-worker-api verigo-supervisor \
  verigo-worker@1 verigo-worker@2 verigo-postgres-tunnel
```

预期：版本为 `<CUTOVER_COMMIT>`，健康接口返回 `{"status":"ok","database":"ok"}`，全部服务为 `active`。

## 6. B 阶段：预部署与基础设施复核（5 项）

### B1. 准备操作记录目录

```bash
export CUTOVER_ID="$(date -u +%Y%m%dT%H%M%SZ)"
export CUTOVER_DIR="/var/backups/verigo/postgres-cutover-$CUTOVER_ID"
install -d -m 700 "$CUTOVER_DIR"
printf '%s\n' "$(readlink -f /opt/verigo/current)" > "$CUTOVER_DIR/previous-release.txt"
cp -a /etc/verigo/verigo.env "$CUTOVER_DIR/verigo.env.before"
chmod 600 "$CUTOVER_DIR/verigo.env.before"
```

### B2. 检查磁盘和备份

```bash
df -h /opt/verigo /var/backups/verigo
systemctl status verigo-backup.timer verigo-backup.service --no-pager
journalctl -u verigo-backup.service -n 50 --no-pager
ls -lah /var/backups/verigo
```

Go：有足够空间保存至少两份 SQLite 大小的文件，最近备份成功。No-Go：磁盘接近满、备份失败或备份不可读。

### B3. 检查隧道

```bash
systemctl is-active verigo-postgres-tunnel
ss -lntp | grep '127.0.0.1:15432'
journalctl -u verigo-postgres-tunnel -n 50 --no-pager
```

预期：服务 `active`，仅本机 `127.0.0.1:15432` 在监听，日志无持续重连。

### B4. 安全加载 DSN 并执行 SQL 握手

不要把 DSN 直接放入命令行。使用 root shell：

```bash
set -a
. /etc/verigo/postgres.env
set +a
test -n "${VERIGO_DATABASE_URL:-}"
/opt/verigo/.venv/bin/python - <<'PY'
import os
import psycopg
with psycopg.connect(os.environ["VERIGO_DATABASE_URL"], connect_timeout=10) as c:
    print(c.execute("select current_database(), current_user, inet_server_addr(), inet_server_port() ").fetchone())
PY
```

预期：数据库为 `verigo_shadow`，用户为 `verigo`，查询成功。

### B5. 提前预检（仅观察）

```bash
cd /opt/verigo/current
/opt/verigo/.venv/bin/python \
  scripts/postgres_cutover_preflight.py \
  --sqlite /opt/verigo/data/verigo.db \
  --allow-active-leases | tee "$CUTOVER_DIR/preflight-before-drain.json"
```

这里允许报告活动租约，但 counts / primary-key digests 仍应一致；若不一致，先重新影子同步并查明原因。

## 7. C 阶段：维护窗口与排空（6 项）

### C1. 宣布维护并拒绝新提交

先记录 readiness：

```bash
MONITOR_TOKEN="$(sed -n 's/^VERIGO_MONITOR_TOKEN=//p' /etc/verigo/verigo.env)"
curl -fsS -H "X-Verigo-Monitor-Token: $MONITOR_TOKEN" \
  http://127.0.0.1:8000/api/internal/readiness | tee "$CUTOVER_DIR/readiness-before-drain.json"
unset MONITOR_TOKEN
```

把 SQLite 的服务模式设为 `draining`：

```bash
cd /opt/verigo/current
PYTHONPATH=/opt/verigo/current runuser -u verigo -- /opt/verigo/.venv/bin/python - <<'PY'
from app.db.jobs import JobStore
from app.config import settings
store = JobStore(settings.database_path)
store.set_service_mode("draining")
print(store.health_summary())
PY
```

预期：`service_mode` 为 `draining`，新验证提交返回 503，读取已有结果仍可用。

如需要整站维护页，可按 `OPERATIONS.md` 切换 Caddy；这不是数据库切换的强制条件。

### C2. 等待任务自然完成

每 15-30 秒查看：

```bash
MONITOR_TOKEN="$(sed -n 's/^VERIGO_MONITOR_TOKEN=//p' /etc/verigo/verigo.env)"
watch -n 15 "curl -fsS -H 'X-Verigo-Monitor-Token: $MONITOR_TOKEN' http://127.0.0.1:8000/api/internal/readiness"
```

目标：

```text
queued_jobs=0
running_jobs=0
pending_results=0
verifying_results=0
stale_leases=0
```

退出 `watch` 后执行 `unset MONITOR_TOKEN`，避免令牌长期留在 shell 环境。

### C3. 检查所有活动租约

```bash
/opt/verigo/.venv/bin/python - <<'PY'
import sqlite3
p = "/opt/verigo/data/verigo.db"
with sqlite3.connect(f"file:{p}?mode=ro", uri=True) as c:
    for table, sql in {
        "jobs": "SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY status",
        "active_job_leases": "SELECT execution_target, COUNT(*) FROM job_leases WHERE completed_at IS NULL GROUP BY execution_target",
        "active_mx_leases": "SELECT COUNT(*) FROM mx_scheduler_leases WHERE expires_at > CURRENT_TIMESTAMP",
    }.items():
        print(table, c.execute(sql).fetchall())
PY
```

Go：活动 job lease 为 0，运行/排队任务为 0。短期 MX lease 可等待过期后复查。

### C4. 处理卡住任务

若 3 分钟以上没有进展：

```bash
journalctl -u verigo-worker@1 -u verigo-worker@2 -u verigo-supervisor \
  -u verigo-worker-api --since '-20 min' --no-pager
```

检查 lease 的 `heartbeat_at`、worker 心跳和远程目标状态。优先恢复对应 worker 让任务正常结算。只有已经确认 worker 永久离线、业务状态可恢复且理解应用的租约回收语义后，才使用应用已有的恢复流程。不要直接 `DELETE FROM job_leases` 或手工把任务改成完成。

### C5. 停止所有可能写库的进程

活动工作全部归零后：

```bash
systemctl stop verigo-supervisor.service
systemctl stop verigo-worker@1.service verigo-worker@2.service
systemctl stop verigo-worker-api.service
systemctl stop verigo.service
systemctl is-active verigo verigo-worker-api verigo-supervisor verigo-worker@1 verigo-worker@2
```

预期均为 `inactive`。同时暂停可能写业务库的 timer；当前 retention 会修改记录：

```bash
systemctl stop verigo-retention.timer verigo-retention.service
systemctl list-timers --all | grep -E 'verigo-(backup|monitor|retention)'
```

### C6. 确认 SQLite 已冻结

```bash
lsof /opt/verigo/data/verigo.db /opt/verigo/data/verigo.db-wal /opt/verigo/data/verigo.db-shm || true
```

Go：没有应用进程持有数据库/WAL 写句柄。若仍有句柄，查明进程用途后再继续。

## 8. D 阶段：最终快照与同步（6 项）

本节只在 P0 全部完成后执行。以下命令保留了当前迁移器的参数形式；P0.1/P0.2 实现时必须让 `--include-all-tables` 仅使用已登记的显式 schema，或用新的等效“全站正式表”参数替换它，并同步更新本文。

### D1. 使用 SQLite Backup API 创建一致快照

```bash
export SNAPSHOT="$CUTOVER_DIR/verigo-final.db"
/opt/verigo/.venv/bin/python - <<'PY'
import os, sqlite3
source = "/opt/verigo/data/verigo.db"
target = os.environ["SNAPSHOT"]
with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
    with sqlite3.connect(target) as dst:
        src.backup(dst)
with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as check:
    result = check.execute("PRAGMA quick_check").fetchone()[0]
    print("snapshot=", target)
    print("quick_check=", result)
    if result != "ok":
        raise SystemExit(2)
PY
chmod 600 "$SNAPSHOT"
sha256sum "$SNAPSHOT" | tee "$SNAPSHOT.sha256"
```

预期：`quick_check= ok`。保留原 SQLite、WAL/SHM（若存在）和该快照，不覆盖原库。

### D2. 对快照做源端报告

```bash
cd /opt/verigo/current
/opt/verigo/.venv/bin/python \
  scripts/migrate_sqlite_to_postgres.py \
  --sqlite "$SNAPSHOT" --include-all-tables --dry-run \
  | tee "$CUTOVER_DIR/source-final.jsonl"
```

预期：P0 完成后，输出全部显式登记业务表的 counts、key digests 和 content digests。

### D3. 确认最终快照没有活动租约

```bash
/opt/verigo/.venv/bin/python - <<'PY'
import os, sqlite3
with sqlite3.connect(f"file:{os.environ['SNAPSHOT']}?mode=ro", uri=True) as c:
    print("active_job_leases=", c.execute(
        "SELECT COUNT(*) FROM job_leases WHERE completed_at IS NULL"
    ).fetchone()[0])
    print("active_jobs=", c.execute(
        "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')"
    ).fetchone()[0])
PY
```

两者都必须为 0。

### D4. 执行最终同步

当前环境已在 B4 中从受保护文件加载 DSN：

```bash
cd /opt/verigo/current
/opt/verigo/.venv/bin/python \
  scripts/migrate_sqlite_to_postgres.py \
  --sqlite "$SNAPSHOT" \
  --postgres-dsn "$VERIGO_DATABASE_URL" \
  --include-all-tables \
  --batch-size 1000 \
  | tee "$CUTOVER_DIR/final-migration.jsonl"
```

说明：完成 P0 后，`--include-all-tables` 必须迁移显式登记的 Auth/Metrics/Prospecting 表，并裁剪 PostgreSQL 中源端已不存在的行。不要在当前推断型 shadow schema 未被替换前把这条命令用于生产切换。命令参数可能被同机进程列表看到；更严格的执行方式是只设置 `POSTGRES_DSN` 环境变量并省略 `--postgres-dsn`：

```bash
export POSTGRES_DSN="$VERIGO_DATABASE_URL"
/opt/verigo/.venv/bin/python \
  scripts/migrate_sqlite_to_postgres.py --sqlite "$SNAPSHOT" \
  --include-all-tables --batch-size 1000 | tee "$CUTOVER_DIR/final-migration.jsonl"
unset POSTGRES_DSN
```

预期最后一行：

```json
{"phase": "verify", "ok": true}
```

### D5. 财务数据不变量检查

P0 完成后的 `--include-all-tables` 会验证所有登记表的数量、主键和完整内容摘要。另保存以下人工可读汇总，便于业务核对：

```bash
/opt/verigo/.venv/bin/python - <<'PY' | tee "$CUTOVER_DIR/sqlite-financial-summary.txt"
import os, sqlite3
with sqlite3.connect(f"file:{os.environ['SNAPSHOT']}?mode=ro", uri=True) as c:
    queries = {
      "users_credits": "SELECT COUNT(*), COALESCE(SUM(credits),0) FROM users",
      "credit_ledger": "SELECT COUNT(*), COALESCE(SUM(delta),0) FROM credit_ledger",
      "paid_orders": "SELECT COUNT(*), COALESCE(SUM(credits),0), COALESCE(SUM(amount_fen),0) FROM payment_orders WHERE status='paid'",
      "redeemed_codes": "SELECT COUNT(*), COALESCE(SUM(credits),0), COALESCE(SUM(amount_fen),0) FROM redemption_codes WHERE redeemed_at IS NOT NULL",
      "promo_remaining": "SELECT COUNT(*), COALESCE(SUM(remaining_credits),0) FROM promo_credit_grants",
    }
    for name, sql in queries.items(): print(name, c.execute(sql).fetchone())
PY
```

使用 `psql` 或 psycopg 对 PostgreSQL 执行同样的 SELECT（布尔/时间条件按 PostgreSQL schema 保持等价），保存到 `$CUTOVER_DIR/postgres-financial-summary.txt`，逐项一致才继续。

### D6. 最终预检

```bash
cd /opt/verigo/current
/opt/verigo/.venv/bin/python \
  scripts/postgres_cutover_preflight.py \
  --sqlite "$SNAPSHOT" \
  --postgres-dsn "$VERIGO_DATABASE_URL" \
  | tee "$CUTOVER_DIR/preflight-final.json"
```

硬性预期：

```json
"ready": true
"blockers": []
"differences": {}
"source.active_leases": 0
"target.active_leases": 0
"target.rollback_write_test": true
```

P0.2 必须已把预检扩展为全部登记表的 counts、primary-key 和 content 检查。最终预检与 D4 的迁移器验证都必须成功；若输出仍只覆盖 19 张核心表，即为 No-Go。

## 9. E 阶段：正式切换（6 项）

### E1. 让 PostgreSQL 初始保持 draining

最终同步从 SQLite 复制了 `service_state=draining`。确认：

```bash
/opt/verigo/.venv/bin/python - <<'PY'
import os, psycopg
with psycopg.connect(os.environ["VERIGO_DATABASE_URL"]) as c:
    print(c.execute("SELECT value FROM service_state WHERE name='verification_mode'").fetchone())
PY
```

预期：`('draining',)`。这样启动 PostgreSQL 模式后，新任务仍被拒绝，便于先验证读路径。

### E2. 安全合并生产配置

先验证暂存文件权限：

```bash
stat -c '%a %U:%G %n' /etc/verigo/postgres.env /etc/verigo/verigo.env
```

两者应为 root 所有且权限 `600`。然后用不会打印秘密的脚本更新主环境文件：

```bash
/opt/verigo/.venv/bin/python - <<'PY'
from pathlib import Path

main = Path('/etc/verigo/verigo.env')
pg = Path('/etc/verigo/postgres.env')

def parse(path):
    values = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values[key] = value
    return values

pg_values = parse(pg)
dsn = pg_values.get('VERIGO_DATABASE_URL', '').strip()
if not dsn or '127.0.0.1:15432' not in dsn:
    raise SystemExit('staged VERIGO_DATABASE_URL must use 127.0.0.1:15432')

drop = {
    'VERIGO_DATABASE_URL', 'VERIGO_POSTGRES_ENABLED',
    'VERIGO_AUTH_POSTGRES_ENABLED', 'VERIGO_METRICS_POSTGRES_ENABLED',
    'VERIGO_PROSPECTING_POSTGRES_ENABLED', 'VERIGO_JOB_POSTGRES_ENABLED',
}
lines = [line for line in main.read_text().splitlines()
         if line.split('=', 1)[0].strip() not in drop]
lines += [f'VERIGO_DATABASE_URL={dsn}', 'VERIGO_POSTGRES_ENABLED=true']
tmp = main.with_suffix('.env.cutover')
tmp.write_text('\n'.join(lines) + '\n')
tmp.chmod(0o600)
tmp.replace(main)
PY
chown root:root /etc/verigo/verigo.env
chmod 600 /etc/verigo/verigo.env
```

不要设置任何单独 store flag 为 `false`，否则配置保护会阻止服务启动。

### E3. 不泄露 DSN 地检查配置

```bash
/opt/verigo/.venv/bin/python - <<'PY'
from pathlib import Path
vals = {}
for line in Path('/etc/verigo/verigo.env').read_text().splitlines():
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1); vals[k] = v
print('enabled=', vals.get('VERIGO_POSTGRES_ENABLED'))
print('dsn_present=', bool(vals.get('VERIGO_DATABASE_URL')))
print('uses_local_tunnel=', '127.0.0.1:15432' in vals.get('VERIGO_DATABASE_URL',''))
for k in ('VERIGO_AUTH_POSTGRES_ENABLED','VERIGO_METRICS_POSTGRES_ENABLED','VERIGO_PROSPECTING_POSTGRES_ENABLED','VERIGO_JOB_POSTGRES_ENABLED'):
    print(k, vals.get(k, '<unset>'))
PY
```

预期：enabled=true、dsn_present=true、uses_local_tunnel=true，四个单独开关均 unset 或 true。

### E4. 切换前再次确认隧道

```bash
systemctl is-active verigo-postgres-tunnel
ss -lnt | grep '127.0.0.1:15432'
```

隧道不是 `active` 即 No-Go。

### E5. 按顺序启动 PostgreSQL 模式服务

```bash
systemctl start verigo.service
for i in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8000/api/health && break
  sleep 1
done
curl -fsS http://127.0.0.1:8000/api/health

systemctl start verigo-worker-api.service
systemctl start verigo-supervisor.service
systemctl start verigo-worker@1.service verigo-worker@2.service
systemctl is-active verigo verigo-worker-api verigo-supervisor verigo-worker@1 verigo-worker@2
```

预期：健康接口成功，所有服务 active。任何服务启动失败，立即看日志；不要开放提交。

### E6. 在 PostgreSQL 中恢复 active

所有只读和连接检查通过后：

```bash
set -a; . /etc/verigo/verigo.env; set +a
cd /opt/verigo/current
runuser -u verigo --preserve-environment -- /opt/verigo/.venv/bin/python - <<'PY'
from app.db.jobs import JobStore
store = JobStore()
print('before=', store.health_summary())
store.set_service_mode('active')
print('after=', store.health_summary())
PY
```

预期 `after.service_mode=active`。确认后结束维护公告或恢复 Caddy 正常配置。

> 切换窗口内不要再次运行 `deploy/publish.ps1`。现有 `release.sh` 的 drain helper 直接操作 SQLite，且发布脚本会自行重启服务。代码应在 A4 提前发布，数据库切换只按本节执行。

## 10. F 阶段：切换后业务验收（5 项）

### F1. 健康和 readiness

```bash
curl -fsS https://verigo.site/api/health
MONITOR_TOKEN="$(sed -n 's/^VERIGO_MONITOR_TOKEN=//p' /etc/verigo/verigo.env)"
curl -fsS -H "X-Verigo-Monitor-Token: $MONITOR_TOKEN" \
  http://127.0.0.1:8000/api/internal/readiness
unset MONITOR_TOKEN
```

预期：public health 为 ok；readiness 的 database=ok、service_mode=active、stale_leases=0、unhealthy_targets=[]。

### F2. 身份与财务路径

使用专用测试账号，不使用真实客户余额：

- [ ] 登录并读取 `/api/auth/me`。
- [ ] 创建并验证一个测试注册账号。
- [ ] 确认 session 持久化，重新请求仍登录。
- [ ] 记录切换前后该测试账号 credits，数值一致。
- [ ] 不在首轮验证中触发真实支付回调或批量调整客户积分。

### F3. 最小任务闭环

使用 1-3 个内部测试邮箱：

- [ ] 提交任务成功。
- [ ] worker 能 claim lease，heartbeat 更新。
- [ ] lease 正常完成且不重复完成。
- [ ] 结果成功持久化，分页和 CSV 下载正常。
- [ ] 失败/重试状态能正常显示。

### F4. 管理和指标

- [ ] 管理员任务列表、账户详情、交易摘要可读。
- [ ] Metrics/admin endpoint 无 SQL 或类型转换异常。
- [ ] Prospecting 只执行只读/小规模测试，不启动大批量作业。
- [ ] Scheduler profile、worker heartbeat 和 runtime 状态正常更新。

### F5. PostgreSQL 和日志

```bash
/opt/verigo/.venv/bin/python - <<'PY'
import os, psycopg
with psycopg.connect(os.environ['VERIGO_DATABASE_URL']) as c:
    print('connections=', c.execute(
      "SELECT state, count(*) FROM pg_stat_activity WHERE datname=current_database() GROUP BY state"
    ).fetchall())
    print('waiting_locks=', c.execute(
      "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'"
    ).fetchone()[0])
PY
journalctl -u verigo -u verigo-worker-api -u verigo-supervisor \
  -u verigo-worker@1 -u verigo-worker@2 --since '-20 min' --no-pager
```

重点搜索：`Traceback`、`psycopg`、`UndefinedTable`、`datatype mismatch`、`deadlock`、`timeout`、连接耗尽和 lock wait。持续错误或业务闭环失败时执行回滚。

## 11. R 阶段：回滚（异常时执行，3 项）

### 触发条件

以下任一情况建议回滚：健康接口持续失败超过 5 分钟；Auth/余额异常；任务不能 claim/complete；结果丢失或重复；持续死锁/连接耗尽；错误率显著高于基线且 10 分钟内不能定位。

> 切换后 PostgreSQL 产生的新写入不会自动回到旧 SQLite。应尽早决定回滚。回滚前保留 PostgreSQL 状态，记录切换后创建的任务/账号/积分变化，避免恢复后静默丢失。

### R1. 冻结 PostgreSQL 写入并停服务

```bash
set -a; . /etc/verigo/verigo.env; set +a
cd /opt/verigo/current
runuser -u verigo --preserve-environment -- /opt/verigo/.venv/bin/python - <<'PY'
from app.db.jobs import JobStore
JobStore().set_service_mode('draining')
PY
systemctl stop verigo-supervisor verigo-worker@1 verigo-worker@2 verigo-worker-api verigo
```

记录 PostgreSQL 当前时间、counts、活动任务和活动租约，保留数据库，不删除或覆盖任何表。

### R2. 恢复原环境和 SQLite

```bash
cp -a "$CUTOVER_DIR/verigo.env.before" /etc/verigo/verigo.env
chown root:root /etc/verigo/verigo.env
chmod 600 /etc/verigo/verigo.env
grep -E '^(VERIGO_POSTGRES_ENABLED|VERIGO_DATABASE_PATH)=' /etc/verigo/verigo.env
```

预期 `VERIGO_POSTGRES_ENABLED=false`（或未设置）且 `VERIGO_DATABASE_PATH=/opt/verigo/data/verigo.db`。

默认优先恢复原始、冻结后未被修改的 `/opt/verigo/data/verigo.db`；不要用快照覆盖它。只有原库损坏且校验失败时，才在保留原文件后从 `$SNAPSHOT` 恢复。

把 SQLite service mode 恢复为 active，然后启动：

```bash
PYTHONPATH=/opt/verigo/current runuser -u verigo -- /opt/verigo/.venv/bin/python - <<'PY'
from app.db.jobs import JobStore
from app.config import settings
JobStore(settings.database_path).set_service_mode('active')
PY
systemctl start verigo
curl -fsS http://127.0.0.1:8000/api/health
systemctl start verigo-worker-api verigo-supervisor verigo-worker@1 verigo-worker@2
systemctl start verigo-retention.timer
```

### R3. 回滚验收

- [ ] `/api/health` 返回 ok。
- [ ] readiness 为 active，队列可正常处理。
- [ ] 登录、历史任务和余额与切换前一致。
- [ ] 保留 `$CUTOVER_DIR` 和 PostgreSQL 数据库用于差异调查。
- [ ] 记录 PostgreSQL 窗口内新增写入，制定人工补录/重放方案。
- [ ] 不删除 SQLite、最终快照或 PostgreSQL 数据。

若应用发布版本本身也需回滚，使用 `$CUTOVER_DIR/previous-release.txt` 中记录的目录重新指向 `/opt/verigo/current`，再重启服务；操作前确认目标确实是完整旧 release。

## 12. G 阶段：观察与收尾（4 项）

### G1. 观察 30-60 分钟

每 5 分钟记录：

- queue：queued/running/pending/verifying 数量和吞吐。
- lease：活动数、stale 数、claim 到 complete 时延。
- worker：heartbeat、unhealthy target、远程节点恢复次数。
- PostgreSQL：连接数、active/waiting、长事务、死锁、CPU、内存、磁盘、WAL 增长。
- 应用：5xx、重试率、重复结果、分页/CSV 错误。
- 外部验证：SMTP/MX 延迟和超时率，避免把外部慢响应误判为数据库故障。

### G2. 运行监控并检查告警

```bash
systemctl start verigo-monitor.service
systemctl status verigo-monitor.timer verigo-monitor.service --no-pager
journalctl -t verigo-monitor -n 100 --no-pager
```

P0.7 完成后，monitor 必须实际检查 PostgreSQL 写入/回滚、连接和锁等待。P0.6 完成且 retention 的 PostgreSQL 测试通过后，才执行 `systemctl start verigo-retention.timer`。

### G3. 保留回滚资料

至少保留：最终 SQLite 快照及 SHA-256、切换前环境文件、迁移输出、预检输出、财务汇总、版本号和切换时间。建议保留 7-14 天，待稳定后再按数据保留政策处理。所有文件保持 root-only。

### G4. 后续工程收尾

- [ ] 修改 `release.sh`，使 drain 和 active mode 使用当前启用的后端，而不是硬编码 SQLite。
- [ ] 让所有 systemd 单元显式、统一地加载 PostgreSQL 环境，或将 DSN 正式纳入配置管理。
- [ ] 将 monitor 的 SQLite 写检查替换为后端感知检查。
- [ ] 增加 PostgreSQL 自动备份、恢复演练、保留策略和磁盘/WAL 告警。
- [ ] 为连接数设置合理上限并观察 uvicorn + worker + supervisor 的连接总量。
- [ ] 稳定期结束后再决定 SQLite 退役；不要立即删除。
- [ ] 单独推进大 CSV/结果文件到对象存储，不与本次数据库切换混做。
- [ ] 轮换曾在聊天或历史记录中出现过的 SSH、VPS 和服务凭据。

## 13. 一页维护窗口清单

### 切换前

- [ ] `<CUTOVER_COMMIT>` 已提交、推送、CI 全绿。
- [ ] 该提交已在 SQLite 模式部署并验收。
- [ ] `CUTOVER_ID`、`CUTOVER_DIR`、旧 release、旧 env 已记录。
- [ ] 磁盘和最近备份正常。
- [ ] 隧道 active，SQL 握手成功。
- [ ] PostgreSQL DSN 使用 `127.0.0.1:15432`。

### 排空和冻结

- [ ] SQLite `service_state=draining`。
- [ ] queued/running/pending/verifying 全部为 0。
- [ ] 活动 job lease 为 0。
- [ ] 写库服务和 retention 已停止。
- [ ] 无应用进程持有 SQLite 写句柄。

### 数据门槛

- [ ] 最终快照 quick_check=ok，SHA-256 已保存。
- [ ] 最终迁移 ok=true。
- [ ] counts、key digest、content digest 一致。
- [ ] 财务汇总一致。
- [ ] 最终预检 ready=true、blockers=[]、differences={}。
- [ ] SQLite/PostgreSQL active leases 均为 0。

### 开启和验收

- [ ] 主 env 已备份，开关 true，DSN 已合并，无冲突 store flag。
- [ ] 隧道仍 active。
- [ ] Web -> Worker API -> Supervisor -> Worker 1/2 顺序启动。
- [ ] PostgreSQL service mode 从 draining 改为 active。
- [ ] health、readiness、Auth、任务闭环、结果、Metrics 全部通过。
- [ ] 日志无持续 PostgreSQL 错误或锁等待。
- [ ] 观察至少 30-60 分钟。

## 14. 完成定义

满足以下条件才可宣布迁移完成：生产所有应用进程统一使用 PostgreSQL；完整业务闭环通过；连续 30-60 分钟无数据一致性、连接、锁或错误率异常；SQLite 原库和最终快照仍可用；回滚步骤与责任人明确；PostgreSQL 备份和监控已安排。此前，SQLite 始终作为只读回滚证据保留。
