# XHS_ALL_IN_ONE 项目梳理

> 梳理对象：`D:\work\XHS_ALL_IN_ONE`
> 梳理方式：代码 + 配置 + 文档通读（README、SIGNATURE_ALGORITHM_DEEP_DIVE.md、源码）
> 说明：仓库内已有的 `PROJECT_REVIEW.md` 本次未能读取，以下结论均来自源码与配置本身。

---

## 1. 一句话定位

**小红书一站式智能运营平台**：把「数据采集 → 内容库 → AI 改写 → 图片润色 → 一键/定时发布 → 全自动运营」串成一条闭环。

对外的两个身份：

- **Web 运营平台**：给运营人员用的可视化后台（多账号矩阵 + 内容管线）
- **XHS 底层 SDK 仓库**：`apis/` + `xhs_utils/` 是逆向签名 + HTTP 接口的封装，可独立作为能力层被上层 Agent 工具链（Clawbot / Claude Code / Codex）通过 skills 复用

> ⚠️ README 明确标注：仅供学习交流，禁止商业化。

---

## 2. 技术栈

| 层 | 选型 |
|---|---|
| 后端框架 | FastAPI + Uvicorn |
| ORM / 迁移 | SQLAlchemy 2.0 + Alembic（14 个版本脚本） |
| 数据库 | SQLite（默认，`data/spider_xhs.db`）/ MySQL（生产） |
| 调度 | APScheduler `BackgroundScheduler` |
| 鉴权 | 自研 JWT（HMAC-SHA256）+ PBKDF2-SHA256 密码（26 万次迭代） |
| 加密 | Fernet 对称加密（Cookie、API Key 落库前加密） |
| HTTP 客户端 | **curl_cffi**（伪装 Chrome 146 TLS/HTTP2 指纹），非 requests |
| 签名运行时 | **Node.js 子进程**执行 JS 算法（唯一算法实现源，Python 侧不重复实现） |
| 前端 | React 19 + Vite 7 + TypeScript 5.9 + antd 6 + recharts 3 |
| 拖拽 | @dnd-kit（素材排序） |
| 部署 | 多阶段 Dockerfile（Node 构建前端 → Python 运行）+ docker-compose |

---

## 3. 目录职责

```
XHS_ALL_IN_ONE/
├── main.py                     统一启动入口（后端 uvicorn + 可选拉起前端 Vite）
├── config/                     default.yaml / production.yaml（分层配置）
├── apis/                       ★ XHS 底层 SDK：PC / Creator / 蒲公英 / 千帆 四套接口
├── xhs_utils/                  ★ 签名算法封装（core / xhs_pc / xhs_creator 三套 + JS）
├── backend/
│   ├── alembic/                数据库迁移（14 个 revision）
│   └── app/
│       ├── main.py             FastAPI 应用装配（21 个 router + SPA 静态回退）
│       ├── core/               config / database / security / deps / time / platforms
│       ├── models/             SQLAlchemy 模型（21 张表）
│       ├── api/                HTTP 路由层
│       ├── services/           业务逻辑（AI、调度、账号、下载、限流、通知）
│       ├── adapters/xhs/       ★ SDK 适配层（上层唯一允许调用 SDK 的入口）
│       └── storage/            media/ 素材 + exports/ 导出
├── frontend/src/
│   ├── app/                    providers + router
│   ├── pages/platforms/xhs/    14 个功能页面
│   ├── components/             layout（侧边栏/通知）、account（三种登录面板）
│   ├── lib/api.ts             HTTP 客户端（axios 封装）
│   └── types/index.ts         全量 TS 类型
├── tests/backend/              3 个测试文件
├── static/                     README 用的 7 张界面截图
└── author/                     作者收款码等
```

---

## 4. 分层架构与调用约定

```
前端 SPA
   ↓ axios (/api)
FastAPI 路由层  ── 21 个 router，约 130 个端点，全部 /api 前缀
   ↓
服务层 services/（AI 改写、调度、账号、通知、限流、素材下载）
   ↓
适配层 adapters/xhs/  ← 唯一允许碰 SDK 的地方
   ↓
SDK 层 apis/ + xhs_utils/（curl_cffi 发请求，Node 子进程算签名）
   ↓
小红书（PC 站 / 创作者平台 / 蒲公英 / 千帆）
```

**硬性约定（README 明示）**：`apis/` 属底层 SDK，请勿直接修改；上层必须经 `backend/app/adapters/` 中转。

**适配层的一个细节**：每个 SDK 调用都包在 `direct_xhs_request_env()` 上下文里，临时摘掉 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY` 等环境变量（加线程锁），避免本机代理污染 SDK 请求。

---

## 5. 数据模型（21 张表）

| 域 | 表 |
|---|---|
| 用户 | `users` |
| 账号矩阵 | `platform_accounts`、`account_cookie_versions`（Cookie 多版本加密存档）、`login_sessions` |
| 内容库 | `notes`、`note_assets`、`note_comments`、`tags`、`note_tags`（多对多） |
| 草稿/AI | `ai_drafts`、`draft_assets`、`ai_generated_assets`、`model_configs` |
| 发布 | `publish_jobs`、`publish_assets` |
| 自动运营 | `auto_tasks` |
| 监控 | `monitoring_targets`、`monitoring_snapshots` |
| 系统 | `tasks`（任务审计）、`notifications`、`api_logs`、`keyword_groups`、`app_migrations` |

**账号设计要点**：`platform_accounts.sub_type` 区分 `pc` / `creator`，两种账号职责不同——PC 账号负责「搜索采集」，Creator 账号负责「上传发布」。自动运营任务必须同时绑定两者（`pc_account_id` + `creator_account_id`）。

**Cookie 存储**：不覆盖写，而是往 `account_cookie_versions` 追加新版本，取用时按 `created_at desc, id desc` 取最新；内容用 Fernet 加密。

---

## 6. API 面（约 130 个端点）

| 模块 | 前缀 | 端点 | 职责 |
|---|---|---|---|
| auth | `/api/auth` | 5 | 注册/登录/刷新/登出/当前用户 |
| accounts | `/api/accounts` | 5 | 账号列表、Cookie 导入、健康检查、删除 |
| login_sessions | `/api/xhs/login-sessions` | 7 | PC/Creator 扫码 + 手机验证码登录 |
| platforms | `/api/platforms` | 1 | 平台注册表（6 平台，仅 XHS 启用） |
| notes | `/api/notes` | 13 | 内容库 CRUD、批量保存/打标/导出、素材排序 |
| crawl | `/api/xhs/crawl` | 4 | 搜索抓取、URL 抓取、用户笔记、通用抓取 |
| pc | `/api/xhs/pc` | 7 | PC 端搜索/详情/评论/用户/首页推荐 |
| creator | `/api/xhs/creator` | 7 | 话题/地点搜索、素材上传、图集/视频发布、已发布列表 |
| drafts | `/api/drafts` | 10 | 草稿 CRUD、送发布中心、素材增删改排序 |
| ai | `/api/ai` | 10 | 改写/生成/标题/标签/润色 + 图片生成/描述/资产管理 |
| publish | `/api/publish` | 11 | 发布任务 CRUD、素材上传、立即发布、重试/取消 |
| auto_tasks | `/api/auto-tasks` | 5 | 自动运营任务 CRUD + 手动触发 |
| monitoring | `/api/xhs/monitoring` | 7 | 监控目标 CRUD、刷新、快照历史 |
| analytics | `/api/xhs/analytics` | 9 | 概览、Top 内容、热门话题、互动趋势、评论洞察、竞品 |
| keyword_groups | `/api/keyword-groups` | 5 | 关键词分组 |
| tags | `/api/tags` | 4 | 标签 CRUD |
| tasks | `/api/tasks` | 6 | 任务中心、调度器状态、重试/取消 |
| files | `/api/files` | 7 | 图片列表/删除/合成/缩放、媒体与导出文件读取、上传 |
| notifications | `/api/notifications` | 3 | 通知列表、单条已读、全部已读 |
| model_configs | `/api/model-configs` | 6 | 模型配置 CRUD、连通性测试、设默认 |

---

## 7. 前端页面（14 个 XHS 功能页）

| 路由 | 页面 |
|---|---|
| `/platform-select` | 平台选择（6 平台，5 个 Coming Soon） |
| `/platforms/xhs/dashboard` | 仪表盘 |
| `/platforms/xhs/accounts` | 账号矩阵 |
| `/platforms/xhs/discovery` | 笔记发现 |
| `/platforms/xhs/crawler` | 数据抓取 |
| `/platforms/xhs/keywords` | 关键词组 |
| `/platforms/xhs/library` | 内容库 |
| `/platforms/xhs/drafts` | 草稿工坊（三栏：队列 + 编辑器 + AI 助手） |
| `/platforms/xhs/benchmarks` | 竞品监控 |
| `/platforms/xhs/image-studio` | 图片工坊 |
| `/platforms/xhs/video-studio` | 视频工坊 |
| `/platforms/xhs/publish` | 发布中心 |
| `/platforms/xhs/auto-ops` | 自动运营 |
| `/platforms/xhs/analytics` | 数据洞察 |
| `/tasks` `/models` `/settings` | 任务中心 / 模型配置 / 设置 |

---

## 8. 五条核心链路

### 8.1 账号接入
扫码 / 手机验证码 / Cookie 导入 → `login_sessions` 轮询会话状态 → 成功后写 `platform_accounts` + 追加 `account_cookie_versions`（加密）。

### 8.2 采集入库
选账号 → PC 适配器 `search_note` / `get_note_info` → 归一化 → `notes` + `note_assets`（素材下载到 `storage/media/`）→ 内容库可打标签、导出 JSON/CSV。

### 8.3 AI 改写
内容库深拷贝 → `ai_drafts` → 调 OpenAI 兼容接口（用户自配端点）改写正文 / 生成标题 / 生成标签 / 润色 → `draft_assets` 存图，可拖拽排序。

### 8.4 发布
草稿送入 `publish_jobs` → 素材上传 Creator（`publish_assets.creator_upload_info` 存 fileIds）→ 立即发布走 `post_note`，定时发布落 `scheduled_at` 由调度器补跑。

**发布实现的一个关键点**：图集发布不是用 SDK 的通用 `post_note`，而是 `creator_api_adapter._post_uploaded_image_note()` 重新组装请求——本地生成 `post_loc`（空地点时**必须删掉** `post_loc` 键，否则服务端报参数错误）、逐条解析话题并拼接 `#话题[话题]#`、再手动补 `x-rap-param` 头。这些都是在浏览器实抓基础上对齐的。

### 8.5 自动运营（无人值守管线）
```
APScheduler 每 60s 扫一次 auto_tasks
  → 取 next_run_at <= now 的 active 任务
  → 随机挑一个关键词 → PC 账号搜索
  → 按互动总量选最优笔记
  → 建草稿 → AI 改写标题+正文（非致命，失败也继续）
  → Creator 账号上传图片素材
  → 建 publish_job 并直接发布
  → 无论成败都重算 next_run_at
```
支持 `manual / daily / weekly / interval` 四种调度；`manual` 只能手动触发。

### 8.6 定时调度（四条后台任务）

| Job | 频率 | 作用 |
|---|---|---|
| `due_publish_runner` | `interval_seconds`（默认 60s） | 补跑到期的定时发布任务 |
| `monitoring_refresh_runner` | 同上 | 刷新监控目标快照（按关键词匹配内容库 + 互动量排序） |
| `auto_tasks_runner` | 60s | 自动运营管线 |
| `cookie_health_checker` | 2h | 全量账号 Cookie 巡检，新过期发通知 |

> 默认 `scheduler.enabled=false`，不开启则所有定时能力不生效。

---

## 9. 签名体系（本项目最有价值的部分）

详见仓库内 `SIGNATURE_ALGORITHM_DEEP_DIVE.md`，四层结构：

1. **传输层伪装** — curl_cffi `impersonate='chrome146'`、`http_version='v2tls'`、`discard_cookies=True`，且每个场景有**严格固定的 Header 顺序**契约（PC / Creator 各 8~10 套顺序常量）。
2. **认证态** — Cookie 解析（a1、web_session）/ DS 锚点 / websectiga（执行服务端下发的 JSVMP 程序）。
3. **请求签名** — `b1`（RC4 + 自定义 Base64）、MNS 签名 `x-s`/`x-t`（三档：`mns0101_` 冷启动 Base58、`mns0201_` 安全域 XXTEA、`mns0301_` 稳态 XOR）、`x-s-common`（CRC32）、追踪头。
4. **指纹/业务扩展** — `profileData`（PC 自定义 DES-ECB / Creator CryptoJS DES-ECB）、`x-rap-param`。

**工程约束**：算法只存在于 `xhs_utils/**/js/`，Python 通过 `subprocess` 调 Node，并对输出做**长度/前缀/正则校验**后才接受（如 b1 长度 ≥500 且是 4 的倍数、x3 必须匹配档位前缀与长度、profileData 必须是 `base64-des-ecb-zero-padding-hex` 且 10000~20000 位 hex）。这是防止算法静默降级的护栏。

---

## 10. 配置与启动

**配置优先级**：`config/default.yaml` < `CONFIG_FILE` < `.env` < 环境变量。

关键开关：

```yaml
database.type: "sqlite"        # sqlite | mysql
security.secret_key: "dev-only-change-me"   # 生产必改
security.fernet_key: ""        # 空则用 secret_key 派生
scheduler.enabled: false       # 定时能力总开关
frontend.serve_static: false   # Docker/生产置 true
```

**启动**：

```bash
python main.py --with-frontend     # 后端 :8000 + 前端 :5173
docker compose up -d               # 单容器，前端静态内嵌
```

**首次启动自动初始化**：`init_db()` 会跑 Alembic 迁移；若库里已有表但没有 `alembic_version`，先 `stamp head` 再 `upgrade head`。另外内置两段幂等数据修复（`app_migrations` 表记录是否执行过）：把历史的 `gpt5.4` 模型名规范为 `gpt-5.4`，以及把 SQLite 里存的 UTC 时间整体 `+8h` 迁到东八区。

**时区约定**：全项目用 `backend.app.core.time.shanghai_now()`，不接受裸 `datetime.now()`。

---

## 11. 上手建议（推荐阅读顺序）

1. `README.md` → 搞清产品全貌
2. `backend/app/main.py` → 21 个 router 的装配顺序即功能地图
3. `backend/app/models/__init__.py` + `models/*.py` → 21 张表的关系是理解一切业务的钥匙
4. `backend/app/adapters/xhs/*_api_adapter.py` → 看清「上层怎么调 SDK」
5. `backend/app/services/scheduler_service.py` → 唯一集中了定时/自动运营/Cookie 巡检的长文件（750 行），是理解自动化的核心
6. `xhs_utils/xhs_core/{auth,params,http}.py` + `SIGNATURE_ALGORITHM_DEEP_DIVE.md` → 签名体系（如果要做采集侧改动）
7. `frontend/src/app/router.tsx` → 前端页面地图；再看 `pages/platforms/xhs/drafts-page.tsx` 学到三栏编辑器的组织方式

---

## 12. 观察到的缺口与技术债

### 12.1 半成品接口（返回 mock 数据）

`backend/app/api/platforms/xhs/pc.py` 中有若干**占位实现**，直接返回 `mock_data.sample_notes()` 或硬编码：

- `POST /xhs/pc/notes/comments` → 返回 `{"total": 1, "items": [{"content": "这个选题很有用"}]}`
- `POST /xhs/pc/users/notes` → 返回 mock 笔记
- `GET /xhs/pc/homefeed/channels`、`POST /xhs/pc/homefeed/recommend` → mock
- `backend/app/services/task_service.py` 也用 `sample_tasks`

**注意**：真正的评论抓取走的是 `crawl.py` 的 `/xhs/crawl/data`（`get_note_all_comment`），所以这些 pc 端点属于历史遗留/未接线路由。

### 12.2 SDK 已实现但未接入上层

`apis/xhs_pugongying_apis.py`（蒲公英 KOL）、`apis/xhs_qianfan_apis.py`（千帆分销）以及对应的 `xhs_utils/xhs_pugongying_util.py`、`xhs_utils/xhs_qianfan_util.py` **在 backend 中零引用**——README 功能清单里标了 ✅，但 Web 层没有任何 adapter / API / 页面。这是一块现成但没接线的能力。

### 12.3 死代码

- `backend/app/core/task_runner.py`：`InProcessTaskRunner` 只被实例化，从未被调用。真正的任务状态在 `tasks` 表里。
- `xhs_utils/xhs_util.py`：只是 `from xhs_utils.xhs_pc.params import *` 的转发兼容层。

### 12.4 调度器的结构性风险

`apscheduler` 用的是 `BackgroundScheduler`，与 FastAPI 同进程。这意味着：

- 多 worker / 多副本部署时**每个进程都会起一套定时器**，发布任务会重复执行（`max_instances=1` 只约束单进程内）。
- 目前靠 `scheduler.enabled` 单开关控制，没有分布式锁。
- 自动运营的 `run_due_auto_tasks` 里 `next_run_at` 是在执行**之后**才算的，任务执行耗时会被计入下一轮间隔。

### 12.5 其他

- 测试覆盖偏薄：`tests/backend/` 只有 3 个文件（`test_api.py`、`test_platforms.py`、`test_root_main.py`），而 README 写「126 passed」，实际断言量需要核对。
- `security.secret_key` 默认值为 `dev-only-change-me`，`fernet_key` 为空时由 `secret_key` 派生——**两者不分离，改 secret_key 会导致历史 Cookie 全部解不开**。生产必须显式固定 `FERNET_KEY`。
- SQLite 连接用了 `PRAGMA journal_mode=MEMORY`（不是 WAL），并发写入下更容易锁表。
- 平台注册表里 5 个平台都是 `coming_soon`，前端 `/platforms/:platformId` 统一落到 `ComingSoonPage`，扩展新平台时需要补 adapter + API + 页面三件套。

---

## 13. 一句话总结

这是一个**把逆向 SDK 与完整运营后台缝合在一起**的项目：`apis/` + `xhs_utils/` 是硬核资产（签名体系四层、Node 单源算法、输出校验护栏），`backend/` 是标准 FastAPI 三层架构加上一套 APScheduler 驱动的自动化，`frontend/` 是 antd 6 + React 19 的运营后台。工程质量整体在线（加密存储、时区统一、幂等迁移、适配层隔离），主要短板在**多副本调度安全**、**mock 遗留路由**、**蒲公英/千帆能力未接线**和**测试覆盖**。
