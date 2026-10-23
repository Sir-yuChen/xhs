# 服务器部署（v1.1.0）

> **一句话**：`cp .env.example .env` → 填两个密码 → `docker compose up -d`。
> 不用 Docker 的走 [§5 裸机部署](#5-裸机部署systemd)。

---

## 1. 前置：`.env`

```bash
cp deploy/.env.example .env
```

**必须填的两项**（不填直接起不来，这是故意的）：

| 变量 | 生成方式 |
|---|---|
| `SECRET_KEY` | `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `POSTGRES_PASSWORD` | 任意强口令 |

⚠️ `SECRET_KEY` 留空或保持 `dev-only-change-me` ⇒ 应用**拒绝启动**
（`core/config.py` 的启动安全自检）。这是设计 53 C1 的守卫，不是故障：
用默认密钥签发的 JWT 等于任何人都能伪造登录态。

## 2. 构建前端

前端产物**不在镜像里构建**（镜像里不装 node，省 80MB+ 与构建时间）：

```bash
cd frontend
npm ci
npm run build          # ⇒ frontend/dist
cd ..
```

## 3. 起服务

```bash
docker compose up -d
docker compose ps      # 三个都该是 healthy / running
```

访问 `http://<服务器IP>/`，用 `admin` / `admin123` 登录。

⚠️ **首次登录会强制改密**（`must_change_password`）：不改密进不了任何页面。
出厂口令写在文档里，不强制改密等于公开它。

## 4. 部署冒烟（8 项，逐条都能手工验）

| # | 检查 | 命令 / 判据 |
|---|---|---|
| 1 | 容器起来了 | `docker compose ps` 三个服务 running |
| 2 | `secret_key` 守卫生效 | 清空 `SECRET_KEY` 再 `up` ⇒ 日志里 `RuntimeError` 且容器退出 |
| 3 | 连上 PG | `docker compose exec app python -c "from backend.app.core.database import engine;print(engine.url.database)"` |
| 4 | 迁移跑过 | `docker compose exec app python -m alembic -c backend/alembic.ini current` ⇒ 显示 head |
| 5 | admin 首登改密 | 登录 ⇒ 只看到改密页；改完跳登录 |
| 6 | 权限闭环 | 建一个 `user` 用户 ⇒ 侧栏无「系统管理」⇒ 直敲 `/system/users` 显示 403 |
| 7 | 数据包上传 | 上传一个 xlsx + 图片的包 ⇒ 预检通过（不被 413 拦住） |
| 8 | 调度单实例 | `docker compose logs app \| grep 租约` ⇒ 只有一个实例拿到锁 |

## 5. 裸机部署（systemd）

见 `deploy/spider-xhs.service` 文件头的注释（含必须改的三个占位）。

要点：
- 专用服务账户 `spider`（**不要 root**，否则上传的文件属主是 root）；
- 密钥放 `/etc/spider-xhs/env`（`chmod 600`），不写进单元文件；
- `ReadWritePaths` 只给 `data/`，其余文件系统只读。

## 6. 三条运维红线

1. **不要扩 `app` 副本** —— 调度靠文件租约，多副本会重复执行定时任务。
   要扩副本须先换掉 `core/singleton.py` 的实现（设计 52 §4）。
2. **不要删 `pg_data` 卷** —— 那是全部业务数据（SQLite 只是历史源）。
   备份：`docker compose exec db pg_dump -U spider_xhs spider_xhs > backup.sql`。
3. **发版后先看 `index.html` 的缓存头** —— 若代理/CDN 缓存了旧的
   `index.html`，它会引用已删除的 js 文件 ⇒ 用户白屏。`deploy/nginx.conf`
   已给 `index.html` 设 `no-store`，另加 CDN 时要同步这条。

## 7. 与本机开发态的差别

| 项 | 开发（本机） | 服务器 |
|---|---|---|
| 前端 | 5173 Vite dev（热更新） | nginx 直出 `dist` |
| 数据库 | PG `spider_xhs`（本机 5432） | `db` 容器 |
| 启动 | `uvicorn` 手动 / launcher | compose / systemd |
| 前端构建 | 不必（热更新） | **必须** `npm run build` |

⚠️ 无论如何**都不要**用 `python main.py --with-frontend` 当常驻服务：
它是一次性阻塞、无监督循环的形态，launcher 一退 uvicorn 就变孤儿
（父进程消失但端口仍持着）。
