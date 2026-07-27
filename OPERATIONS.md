# Verigo 运行维护

## 线上资源

- 主站：`https://verigo.site`
- 别名：`https://www.verigo.site`
- VPS：`103.242.2.226`
- 当前发布目录：`/opt/verigo/current`
- 持久化数据：`/opt/verigo/data`
- 应用环境变量：`/etc/verigo/verigo.env`（权限 `600`）
- Caddy 配置：`/etc/caddy/Caddyfile`

`/opt/verigo/current` 是一个指向已发布版本的符号链接。不要直接修改该目录；修复应提交到 Git 后通过发布流程部署。数据库、CSV 结果和节点凭据均在 `/opt/verigo/data` 或 `/etc/verigo`，发布不会删除它们。

## 发布

在 Windows 工作目录执行：

```powershell
.\deploy\publish.ps1
```

该脚本使用 `verigo-deploy` 的 SSH 私钥和已验证的 known-hosts 文件，上传当前 Git 提交的归档，再由服务器上的 `deploy/release.sh` 原子切换发布版本。发布前会进入 drain 状态，等待运行中的任务安全结束；不要在发布过程中手动重启 `verigo` 或修改任务数据。

发布完成后确认：

```bash
sudo -n cat /opt/verigo/current/RELEASE_VERSION
curl -fsS http://127.0.0.1:8000/api/health
systemctl is-active verigo verigo-supervisor verigo-backup.timer verigo-monitor.timer verigo-retention.timer
```

## 健康与监控

公开接口 `GET /api/health` 只用于可用性与数据库存活检查，正常响应为：

```json
{"status":"ok","database":"ok"}
```

队列长度、运行任务、节点健康和过期租约属于运行细节，只能通过本机接口 `GET /api/internal/readiness` 获取。该接口要求 `/etc/verigo/verigo.env` 中的 `VERIGO_MONITOR_TOKEN`，并使用请求头 `X-Verigo-Monitor-Token`；不要把令牌写入仓库、日志或网页。

`verigo-monitor.timer` 每五分钟执行一次：检查公网健康接口、本机受保护的 readiness、SQLite 可写性、磁盘空间、备份时效和队列积压。查看状态与最近告警：

```bash
systemctl status verigo-monitor.timer verigo-monitor.service --no-pager
journalctl -t verigo-monitor -n 100 --no-pager
```

## 日常排障

```bash
systemctl status verigo verigo-supervisor caddy fail2ban --no-pager
journalctl -u verigo -n 100 --no-pager
journalctl -u verigo-supervisor -n 100 --no-pager
journalctl -u caddy -n 100 --no-pager
curl -fsS http://127.0.0.1:8000/api/health
fail2ban-client status sshd
```

只有在确认没有发布或正在运行的关键任务时才重启服务：

```bash
systemctl restart verigo
systemctl restart verigo-supervisor
```

不要通过删除 SQLite、CSV、租约或队列文件来排障。这些文件包含用户任务与结果；先检查日志、健康状态和部署版本。

## 自动备份与保留

`verigo-backup.timer` 每天执行一次，并在发布后异步触发一次备份。备份脚本使用 SQLite 在线备份 API，保存数据库、结果 CSV、应用归档、运行配置和校验和；不会为备份而停机。保留天数与异地上传配置在 `/etc/verigo/backup.env`。

```bash
systemctl status verigo-backup.timer verigo-backup.service --no-pager
journalctl -u verigo-backup.service -n 100 --no-pager
ls -lah /var/backups/verigo
cat /var/lib/verigo-backup/last-success
```

备份包含 `verigo.env`，其中可能有访问令牌和第三方密钥。备份目录权限必须保持为仅 root 可读；异地存储必须使用独立的受限凭据和访问控制。当前备份脚本尚未进行客户端加密，启用之前需要先提供并配置专用的公开加密接收方。

`verigo-retention.timer` 每天清理过期 CSV 和数据库记录，采用短 SQLite 事务以避免长时间阻塞验证写入：

```bash
systemctl status verigo-retention.timer verigo-retention.service --no-pager
journalctl -u verigo-retention.service -n 100 --no-pager
```

## 维护模式

需要临时阻止公网访问时：

```bash
cp /etc/caddy/Caddyfile.maintenance /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

恢复公开访问：

```bash
cp /opt/verigo/current/deploy/Caddyfile /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

## Cloud Studio 节点

QQ 任务保留 `tencent_qq` 执行目标；站点重启不会把它们改派为本机任务。Cloud Studio worker 使用 claim 和 heartbeat 上报在线状态，生命周期协调器只会在队列有需要时启动工作空间，并在空闲后停止它。

Cloud Studio 凭据、节点令牌和 SSH 私钥只保存在 `/etc/verigo/verigo.env` 或 `/opt/verigo/data`，且文件权限应为 `600`。修改节点配置后，重启应用并检查日志：

```bash
systemctl restart verigo
journalctl -u verigo -f
```

按需启动工作空间需要在 `/etc/verigo/verigo.env` 中配置：

```bash
VERIGO_CLOUDSTUDIO_LIFECYCLE_ENABLED=true
VERIGO_CLOUDSTUDIO_SECRET_ID=
VERIGO_CLOUDSTUDIO_SECRET_KEY=
VERIGO_CLOUDSTUDIO_REGION=ap-guangzhou
VERIGO_CLOUDSTUDIO_SPACE_KEY=
VERIGO_CLOUDSTUDIO_STARTUP_TIMEOUT_SECONDS=300
VERIGO_CLOUDSTUDIO_IDLE_STOP_SECONDS=600
```

应使用仅具备 Cloud Studio `RunWorkspace`、`StopWorkspace` 权限的子账号密钥。首次创建或重建工作空间后，使用服务环境写入启动钩子：

```bash
cd /opt/verigo/current
set -a; . /etc/verigo/verigo.env; set +a
runuser -u verigo -- /opt/verigo/.venv/bin/python deploy/configure_cloudstudio_worker.py
```
