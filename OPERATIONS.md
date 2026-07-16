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
