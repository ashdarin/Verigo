# Verigo 模块划分

## 当前可运行结构

| 模块 | 目录 | 职责 |
| --- | --- | --- |
| Web 入口 | `app/main.py` | FastAPI 生命周期、静态文件、路由挂载 |
| API | `app/api/` | 创建任务、查询进度、读取结果、下载 CSV |
| 账户 | `app/api/auth.py`、`app/db/auth.py` | 注册登录、密码哈希、HttpOnly 会话 |
| 任务层 | `app/tasks/` | 后台执行长任务、汇总结果、生成 CSV |
| 状态层 | `app/db/` | SQLite 持久化用户、会话、任务归属、状态和结果 |
| 核心适配层 | `app/core/legacy.py` | 将原 CLI 验证器接入 Web，不执行交互菜单 |
| 前端 | `static/` | 输入/导入邮箱、进度、摘要和结果表格 |
| 原验证引擎 | `验证8.py` | 当前保持算法行为，后续按领域逐步迁移 |

## 下一阶段核心拆分

`验证8.py` 不应长期作为单文件保留。稳定运行第一版后，按以下顺序迁移：

1. `app/core/validation.py`：格式清洗和结果模型。
2. `app/core/dns.py`：域名、MX 查询和缓存。
3. `app/core/providers/outlook.py`：微软账号接口。
4. `app/core/providers/qq.py`：QQ/Foxmail SMTP 策略。
5. `app/core/smtp.py`：通用 SMTP 投递性验证。
6. `app/core/catch_all.py`：Catch-all 检测、实发探针和退信监听。
7. `app/core/notifications.py`：结果邮件与迟到退信通知。

## 上线前必须补齐

- 在 Caddy/Nginx 层启用 HTTPS、登录认证和请求频率限制。
- 高并发或多机部署时，将 SQLite 和线程任务迁移到 PostgreSQL + 独立 worker。
- 限制单用户并发和每日验证额度，记录审计日志。
- 确认 VPS 允许出站 TCP 25；很多云服务默认封禁该端口。
- 撤销源文件中曾出现的 QQ 授权码，并通过环境变量注入新授权码。
