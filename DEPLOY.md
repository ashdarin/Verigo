# Verigo 部署到 Ubuntu 22.04

目标拓扑：浏览器 -> Caddy HTTPS -> `127.0.0.1:8000` -> Verigo。

## 1. 腾讯云 DNS

在 `verigo.site` 的 DNS 解析页面添加：

| 主机记录 | 类型 | 记录值 | TTL |
| --- | --- | --- | --- |
| `@` | A | `103.242.2.226` | 600 |
| `www` | CNAME | `verigo.site` | 600 |

## 2. 海沫云防火墙

入站只放行：

- TCP 22：SSH，最好只允许你的固定公网 IP。
- TCP 80：Caddy 申请证书及 HTTP 跳转。
- TCP 443：HTTPS。

不要开放 8000，应用只监听 VPS 本机地址。另请服务商确认出站 TCP 25 是否允许。

## 3. 安装运行环境

以 root 或有 sudo 权限的账号登录 VPS：

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip rsync curl debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy
sudo useradd --system --home /opt/verigo --shell /usr/sbin/nologin verigo || true
sudo mkdir -p /opt/verigo /opt/verigo/data/results /etc/verigo
sudo touch /opt/verigo/domain_type_cache.json
sudo chown -R verigo:verigo /opt/verigo
```

## 4. 上传项目

从本机项目目录执行，SSH 用户名按 VPS 实际账号替换：

```powershell
scp -r app static deploy requirements.txt "验证8.py" root@103.242.2.226:/opt/verigo/
```

在 VPS 执行：

```bash
sudo chown -R verigo:verigo /opt/verigo
sudo -u verigo python3 -m venv /opt/verigo/.venv
sudo -u verigo /opt/verigo/.venv/bin/pip install --upgrade pip
sudo -u verigo /opt/verigo/.venv/bin/pip install -r /opt/verigo/requirements.txt
sudo cp /opt/verigo/deploy/verigo.env.example /etc/verigo/verigo.env
sudo chmod 600 /etc/verigo/verigo.env
sudo cp /opt/verigo/deploy/verigo.service /etc/systemd/system/verigo.service
```

只在 `/etc/verigo/verigo.env` 中填写新生成的邮箱授权码，不要上传本机 `.env`。

## 5. 配置 HTTPS

```bash
sudo cp /opt/verigo/deploy/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
```

## 6. 启动

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now verigo
sudo systemctl restart caddy
sudo systemctl status verigo --no-pager
sudo systemctl status caddy --no-pager
curl http://127.0.0.1:8000/api/health
```

浏览器访问 `https://verigo.site`。访客可直接验证；注册应用账户后可保存个人任务历史。

## 7. 排错

```bash
sudo journalctl -u verigo -n 100 --no-pager
sudo journalctl -u caddy -n 100 --no-pager
sudo ss -lntp
```
