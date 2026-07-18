# Verigo 运行维护

## 线上地址

- 主站：`https://verigo.site`
- 别名：`https://www.verigo.site`
- VPS：`103.242.2.226`

## 服务器目录

- 应用：`/opt/verigo`
- 数据库：`/opt/verigo/data/verigo.db`
- CSV：`/opt/verigo/data/results`
- 环境变量：`/etc/verigo/verigo.env`
- Caddy：`/etc/caddy/Caddyfile`
- 维护模式 Caddy：`/etc/caddy/Caddyfile.maintenance`

## 常用命令

```bash
systemctl status verigo caddy fail2ban --no-pager
journalctl -u verigo -n 100 --no-pager
journalctl -u caddy -n 100 --no-pager
curl http://127.0.0.1:8000/api/health
fail2ban-client status sshd
```

## Cloud Studio QQ 节点

QQ 任务保留 `tencent_qq` 执行目标，站点重启不会将其改成本机任务。Cloud Studio
worker 通过 claim 和任务 heartbeat 请求报告在线状态。

按需启动使用腾讯云 Cloud Studio API 3.0 的 `RunWorkspace` 与
`StopWorkspace`。在 `/etc/verigo/verigo.env` 中配置：

```bash
VERIGO_CLOUDSTUDIO_LIFECYCLE_ENABLED=true
VERIGO_CLOUDSTUDIO_SECRET_ID=
VERIGO_CLOUDSTUDIO_SECRET_KEY=
VERIGO_CLOUDSTUDIO_REGION=ap-guangzhou
VERIGO_CLOUDSTUDIO_SPACE_KEY=
VERIGO_CLOUDSTUDIO_STARTUP_TIMEOUT_SECONDS=300
VERIGO_CLOUDSTUDIO_IDLE_STOP_SECONDS=600
```

使用仅允许 Cloud Studio `RunWorkspace`、`StopWorkspace` 的子账号密钥，不要使用
主账号密钥。密钥只保存在权限为 `600` 的服务器环境文件中。配置完成后执行：

```bash
systemctl restart verigo
journalctl -u verigo -f
```

首次创建或重建工作空间后，使用与服务相同的环境文件写入启动钩子：

```bash
cd /opt/verigo
set -a; . /etc/verigo/verigo.env; set +a
runuser -u verigo -- /opt/verigo/.venv/bin/python deploy/configure_cloudstudio_worker.py
```

该钩子在每次工作空间启动时先调用 Cloud Studio 探针，再拉起 QQ worker。命令体以
Base64 提交，因为 Cloud Studio API 的 WAF 会拦截包含明文后台 shell 命令的
`ModifyWorkspace` 请求。

当 worker 离线且出现新 QQ 任务时，页面会显示节点启动进度。API 调用连续失败或
启动超时后，任务会进入失败状态，不会无限排队。队列清空并持续空闲后，协调器会
请求停止工作空间。

## 维护模式

网站默认公开，应用内账号用于保存个人任务历史。进入维护时启用 Caddy 入口密码：

```bash
cp /etc/caddy/Caddyfile.maintenance /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

恢复公开访问：

```bash
cp /opt/verigo/deploy/Caddyfile /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

## 备份

停止应用后备份整个数据目录，再重新启动：

```bash
systemctl stop verigo
tar -C /opt/verigo -czf /root/verigo-data-backup.tar.gz data
systemctl start verigo
```

登录密码和邮箱授权码不应写入本文件或项目仓库。
