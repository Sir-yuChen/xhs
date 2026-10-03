# 代码图谱（Codegraph）

> - **阶段**：四 · 测试
> - **来源**：图片要求第 4 项第 1 条 —— 「执行 codegraph 型代码图谱」
> - **用途**：让测试与开发能快速判断「改一处会影响哪些地方」
> - **版本**：v1.6 · 2026-10-01（**L5 分层图数字同步**：前端页面 26 → **27**（设计 17 §11.3 用量看板独立首页 +1 = `pages/usage-dashboard/usage-dashboard-page.tsx`，路由 `/usage-dashboard`）；端点 216 / path 182 / 请求函数 163 / router 27 不变）。v1.5 · 2026-09-22（**L4/L5 数字同步**：数据对账新增 2 端点（设计 13 §24）⇒ 端点 160 → **162**、path 140 → **142**）。v1.4 · 2026-09-22（**L4/L5 分层图数字同步**：端点 159 → **160**、请求函数 136 → **137**（设计 12 §22））。v1.3 · 2026-09-21（**L4/L5 分层图数字同步**：端点 156 → **156**、请求函数 132 → **133**（设计 13 §18））。v1.2 · 2026-09-21（**L4/L5 分层图数字同步**：router 20 → **22**、端点 136 → **156**、页面 21 → **23**、请求函数 112 → **132**）。v1.1 · 2026-09-18（L4 路由层端点 132 → **136**）
> - **生成方式**：基于源码静态阅读（含 `文件:行号` 证据）

---

## 1. 分层依赖图谱

```
┌─────────────────────────────────────────────────────────────────┐
│ L5  前端 SPA                                                     │
│     React 19 + antd 6 · 32 页面 · 194 请求函数                    │
│     app/router.tsx → pages/** → components/** → lib/api.ts       │
└───────────────────────────┬─────────────────────────────────────┘
                            │ HTTP /api（Bearer）
┌───────────────────────────▼─────────────────────────────────────┐
│ L4  API 路由层（32 个 router / 261 端点）                          │
│     auth · platforms · accounts · login-sessions · notes ·        │
│     files · drafts · ai · tasks · model-configs · tags ·          │
│     notifications · keyword-groups · publish ·                    │
│     xhs/{analytics, pc, creator, crawl, monitoring} · auto-tasks  │
│     llm/gateway-configs（设计 17 §7.1，M1/T1；v1.4 工作平台公共）   │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│ L3  服务层 services/                                             │
│     ai_service · scheduler_service · account_service ·            │
│     monitoring_crawl_service · notification_service ·             │
│     asset_downloader · image_util · rate_limiter                  │
│     ★ publish_service（2026-09-18 新增，发布幂等）                │
│     ✅ task_service 已删 · mock_data 部分删（见 §6.2）            │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│ L2  适配层 adapters/xhs/  ★ 唯一允许碰 SDK 的地方                 │
│     pc_api_adapter · creator_api_adapter ·                        │
│     pc_login_adapter · creator_login_adapter · request_env        │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│ L1  SDK 层                                                        │
│     apis/（7 文件） ──▶ xhs_utils/                                │
│                          ├─ xhs_core/（auth cookies params        │
│                          │   http dsl runtime + js/）            │
│                          ├─ xhs_pc/（auth state params http       │
│                          │   dsl runtime + js/）                 │
│                          └─ xhs_creator/（同上 + js/）            │
└───────────────────────────┬─────────────────────────────────────┘
                            │ curl_cffi（chrome146）
                            ▼
        edith / so / www / as / creator / customer /
        ros-upload / pgy / ci  （小红书各域）
```

**横向依赖**：`core/`（基础设施）被 L1–L4 引用；`models/`（数据定义）被 L3–L4 引用。

---

## 2. 模块依赖矩阵（router → 依赖的服务/适配器）

| Router | 依赖服务 | 依赖适配器 | 依赖模型 |
|---|---|---|---|
| `auth` | — | — | `User` |
| `platforms` | — | — | — |
| `accounts` | `account_service` | `pc_login_adapter` / `creator_login_adapter` / `creator_api_adapter`(检查用) | `PlatformAccount`, `AccountCookieVersion` |
| `login-sessions` | `account_service` | `pc_login_adapter`, `creator_login_adapter` | `LoginSession`, `PlatformAccount` |
| `notes` | `asset_downloader` | `pc_api_adapter`(评论) | `Note`, `NoteAsset`, `NoteComment`, `Tag` |
| `files` | `image_util` | — | — |
| `drafts` | — | — | `AiDraft`, `DraftAsset` |
| `ai` | `ai_service` | — | `ModelConfig`, `AiGeneratedAsset` |
| `tasks` | `scheduler_service` | — | `Task` |
| `model-configs` | `ai_service`(测试连通) | — | `ModelConfig` |
| `tags` | — | — | `Tag` |
| `notifications` | — | — | `Notification` |
| `keyword-groups` | — | — | `KeywordGroup` |
| `publish` | `scheduler_service` | `creator_api_adapter` | `PublishJob`, `PublishAsset` |
| `xhs/analytics` | — | — | `Note`, `PlatformAccount`, `PublishJob` |
| `xhs/pc` | `mock_data`(✅ 2026-09-28 已删 4 个死函数，仅剩 `sample_notes`) | `pc_api_adapter` | — |
| `xhs/creator` | — | `creator_api_adapter` | — |
| `xhs/crawl` | `asset_downloader` | `pc_api_adapter` | `Note`, `NoteAsset`, `NoteComment` |
| `xhs/monitoring` | `monitoring_crawl_service` | `pc_api_adapter` | `MonitoringTarget`, `MonitoringSnapshot` |
| `auto-tasks` | `scheduler_service` | — | `AutoTask`, `PlatformAccount` |

**服务 → 适配器**：

| 服务 | 用到的适配器 |
|---|---|
| `scheduler_service` | `creator_api_adapter`（发布）、`pc_api_adapter`（搜索）、`creator_login_adapter`/`pc_login_adapter`（Cookie 巡检） |
| `monitoring_crawl_service` | `pc_api_adapter` |
| `account_service` | `pc_login_adapter` / `creator_login_adapter` |
| `ai_service` | 无（走 HTTP 调用户自配的 OpenAI 兼容端点） |

---

## 3. 核心调用链图谱

### 3.1 采集链（搜索笔记）

```
[前端] discovery-page.tsx
   └─ lib/api.ts:389 searchXhsNotes()
        └─ POST /api/xhs/pc/search/notes
             └─ api/platforms/xhs/pc.py  (路由)
                  └─ adapters/xhs/pc_api_adapter.py:19 search_note()
                       │  ├─ with direct_xhs_request_env()        :30  ← 剥离代理
                       │  └─ self._api()                          :12-17
                       │       ├─ XHSPcAuth.from_cookie(cookies)  (xhs_pc/auth.py:412)
                       │       └─ XHS_Apis(auth)                  (apis/xhs_pc_apis.py:52)
                       └─ XHS_Apis.search_note()                  (apis/xhs_pc_apis.py:526)
                            ├─ _request_params()                  (:102)
                            │    ├─ auth.validate()               (xhs_pc/auth.py:263)
                            │    ├─ auth.next_sign_context()      (xhs_pc/auth.py:303)
                            │    │    └─ PcDeviceProfile.next_sign_context()  (xhs_pc/state.py:530)
                            │    ├─ auth.current_b1()             (xhs_pc/auth.py:300)
                            │    │    └─ runtime.generate_b1()    (xhs_pc/runtime.py:37)
                            │    │         └─ node b1.js          (runtime.py:46-54)
                            │    ├─ generate_request_params()     (xhs_pc/params.py:444)
                            │    │    ├─ generate_headers()       (:411)
                            │    │    │    └─ generate_xs_xs_common()  (:163)
                            │    │    │         └─ runtime.run_signer()  (runtime.py:68)
                            │    │    │              └─ node sign.js     (runtime.py:109-117)
                            │    │    └─ generate_xy_direction()   (:269, murmur3)
                            │    └─ generate_x_rap_param()        (:213, 仅 RAP 白名单)
                            │         └─ node rap_cli.js           (:227-241)
                            └─ self.http.post()                   (:584)
                                 └─ BrowserHttpClient.request()   (xhs_core/http.py:108)
                                      ├─ ordered_wire_headers()   (:34)  ← header 顺序契约
                                      └─ curl_cffi request()      (:156)
```

**链路上的关键约束点**：
| 点 | 文件:行 | 违反后果 |
|---|---|---|
| 代理剥离 | `pc_api_adapter.py:30` | 请求走代理 → 风控 |
| 签名门禁 | `xhs_pc/runtime.py:28-32, 134-143` | 带错签发请求 |
| header 契约 | `xhs_core/http.py:68-77` | 顺序漂移被识别 |
| RAP 白名单 | `apis/xhs_pc_apis.py:37-44` | 漏签被拒 |

### 3.2 发布链（图集）

```
[前端] publish-page.tsx
   └─ lib/api.ts:730 publishJobToCreator()
        └─ POST /api/publish/jobs/{id}/publish
             └─ api/publish.py
                  ├─ 校验：publish_mode / sub_type=="creator" / 素材存在 / 非视频
                  └─ adapters/xhs/creator_api_adapter.py:87 post_note()
                       └─ _post_uploaded_image_note()            :97
                            ├─ api.get_location_info()   ──▶ 组 post_loc
                            ├─ get_post_note_image_data()        (xhs_creator_util.py:119)
                            │     └─ common.goods_info = {}      (:177) ★二开改这里
                            ├─ if not post_loc: pop("post_loc")  :137-138  ← 必须删键
                            ├─ 逐话题 get_topic() → hash_tag + desc  :139-156
                            ├─ api._request_params()             :157
                            ├─ generate_x_rap_param()            :165  ← Creator 专用模板
                            └─ POST {edith_url}/web_api/sns/v2/note  :170
                                 └─ 写回 publish_jobs.status / external_note_id
```

**素材上传子链**（发布前）：
```
publish_assets.{id}/upload
  └─ creator_api_adapter.upload_media()                      :41
       ├─ _resolve_file_data()                               :49
       │    ├─ 远端 URL → requests.get
       │    ├─ /api/files/media/ → 本地读
       │    └─ webp/RIFF → Pillow 转 JPEG                    :74-83
       └─ XHS_Creator_Apis.upload_media()                    (apis/xhs_creator_apis.py:318)
            ├─ get_fileIds()（permit）                        (:284)
            └─ PUT ros-upload.../spectrum/{file_ids}
                 └─ HmacSHA1 签名（JS: xhs_creator_signature.js）
       └─ 结果缓存到 publish_assets.creator_upload_info
```

### 3.3 登录链（扫码）

```
[前端] qr-login-panel.tsx
   ├─ POST /api/xhs/login-sessions/{pc|creator}/qrcode
   │    └─ api/login_sessions.py
   │         └─ adapters/xhs/pc_login_adapter.py 或 creator_login_adapter.py
   │              └─ apis/xhs_pc_login_apis.py:582 generate_qrcode()
   │                   └─ 建 LoginSession(status=pending) + 加密存临时 Cookie
   │
   └─ 每 2s GET /api/xhs/login-sessions/{id}      ← 轮询
        └─ check_qrcode_status()                  (xhs_pc_login_apis.py:614)
             └─ codeStatus 2 → _login_by_qrcode_status()   (:667)  ← 换取 web_session
                  └─ account_service.upsert_platform_account_from_login()
                       ├─ upsert platform_accounts（按 external_user_id）
                       └─ 追加 account_cookie_versions（Fernet 加密）
```

### 3.4 自动运营链

```
APScheduler(auto_tasks_runner, 60s)
  └─ scheduler_service.py:604 run_due_auto_tasks()
       ├─ 查 auto_tasks（status=active 且非 manual 且 next_run_at<=now）
       ├─ 随机关键词
       ├─ ① pc_api_adapter.search_note()         ← 采集链
       ├─ ② 按互动量选笔记
       ├─ ③ 建 AiDraft
       ├─ ④ ai_service.generate_note()           ← 非致命，失败继续
       ├─ ⑤ creator_api_adapter.upload_media()   ← 发布链（上传段）
       ├─ ⑥ 建 PublishJob + 发布                  ← 发布链（发布段）
       └─ ⑦ 重算 next_run_at（⚠️ 在执行之后）
```

### 3.5 调度器 job 图谱

```
backend/app/main.py:18-29 lifespan
  └─ 若 settings.scheduler_enabled
       └─ scheduler_service.py:740 start_due_publish_scheduler(interval)
            └─ :699 build_due_publish_scheduler()  →  4 个 job
                 ├─ due_publish_runner         → run_due_publish_jobs_once()   :262
                 ├─ monitoring_refresh_runner  → run_monitoring_refresh_once() :404
                 ├─ auto_tasks_runner(60s)     → run_due_auto_tasks()          :604
                 └─ cookie_health_checker(2h)  → check_all_account_cookies_once :？
```

---

## 4. 数据依赖图谱

```
users ───┬──▶ platform_accounts ───┬──▶ account_cookie_versions
         │                          ├──▶ notes ──┬──▶ note_assets
         │                          │            ├──▶ note_comments
         │                          │            └──▶ note_tags ──▶ tags
         │                          ├──▶ publish_jobs ──▶ publish_assets
         │                          └──▶ auto_tasks（pc_account_id + creator_account_id 双引用）
         ├──▶ ai_drafts ──┬──▶ draft_assets
         │                └──▶ ai_generated_assets
         ├──▶ model_configs
         ├──▶ keyword_groups
         ├──▶ monitoring_targets ──▶ monitoring_snapshots
         ├──▶ tasks ──▶ notifications
         └──▶ api_logs（⚠️ 无写入点）

二开新增：
shops ──┬──▶ platform_accounts            （1:1：platform_accounts.shop_id_fk + 部分唯一索引；2026-09-22 起，原为 `shop_account_bindings` N:M 中间表）
        └──▶ shop_products ──▶ product_note_jobs ──▶ ai_drafts / publish_jobs
```

### 4.1 外键影响面（改表时的爆炸半径）

| 若改这张表 | 会影响 |
|---|---|
| `users` | **全部**（所有业务表都挂 `user_id`） |
| `platform_accounts` | `account_cookie_versions`, `login_sessions`, `notes`, `publish_jobs`, `auto_tasks`(2 引用), `monitoring_targets` |
| `ai_drafts` | `draft_assets`, `ai_generated_assets`, `publish_jobs.source_draft_id` |
| `publish_jobs` | `publish_assets`, 二开 `product_note_jobs` |
| `notes` | `note_assets`, `note_comments`, `note_tags`, `ai_drafts.source_note_id` |
| `tasks` | `notifications.source_task_id` |

---

## 5. 端点 → 域名映射（改动影响面判定）

| 端点前缀 | 目标域名 | 签名栈 | 改动的连锁影响 |
|---|---|---|---|
| `/api/xhs/pc/*` | `edith` / `so` | PC（Node, `xhs-pc-web`） | `xhs_pc/**` |
| `/api/xhs/crawl/*` | `edith` / `so` | PC | `xhs_pc/**` |
| `/api/xhs/creator/*` | `creator` / `edith` | Creator（Node, `ugc`） | `xhs_creator/**` |
| `/api/xhs/login-sessions/pc/*` | `edith`/`as`/`www`/`pages` | PC | `xhs_pc/**` |
| `/api/xhs/login-sessions/creator/*` | `customer`/`creator`/`as`/`edith` | Creator | `xhs_creator/**` |
| `/api/publish/*` | `creator`/`edith`/`ros-upload` | Creator + PyExecJS（上传） | `xhs_creator/**` + `xhs_creator_util.py` |
| `/api/xhs/monitoring/*` | `edith`/`so` | PC | `xhs_pc/**` |
| `/api/ai/*` | **用户自配的 OpenAI 兼容端点** | 无（普通 HTTP） | `ai_service` |
| `/api/files/*` | 本地 | 无 | — |

**结论**：改动 `xhs_core/**` 会**同时影响全部链路**（PC + Creator）；改动 `xhs_pc/**` 只影响采集与 PC 登录；改动 `xhs_creator/**` 只影响发布与 Creator 登录。

---

## 6. 静态核查结果

### 6.0 ★ 本轮新增模块（2026-09-18 · M1 第一轮）

新增 4 个文件，均为「**收敛散落判断**」性质——把原先分散在多处的隐式规则集中到单一权威定义：

| 新增文件 | 层 | 承担的唯一职责 | 被谁使用 |
|---|---|---|---|
| `backend/app/core/account_types.py` | L0 core | `sub_type` 的**唯一权威定义**：`SUB_TYPE_PC` / `SUB_TYPE_CREATOR` / `SUB_TYPES` / `normalize_sub_type()` / `UnknownSubTypeError` | `api/accounts.py`、`services/scheduler_service.py` |
| `backend/app/core/roles.py` | L0 core | `role` 的**唯一权威定义**：`ROLE_ADMIN` / `ROLE_USER` / `ROLES` / `normalize_role()` / `is_admin()` | `core/deps.py`、`api/auth.py` |
| `backend/app/services/publish_service.py` | L3 services | **发布幂等键的唯一实现**：`build_publish_idempotency_key` / `find_duplicate_publish` / `claim_publish_idempotency` / `release_publish_idempotency` / `DuplicatePublishError` | `api/publish.py`、`services/scheduler_service.py` |
| `backend/alembic/versions/b1e4d7c9a2f3_add_roles_and_publish_idempotency.py` | 迁移 | `users.role` + `publish_jobs.idempotency_key` | HEAD（revises `60cd5c95fde1`） |

修改的关键文件：`core/config.py`（配置源优先级）、`api/accounts.py`（枚举化 + 删 PATCH）、
`models/user.py`、`models/publish.py`、`core/deps.py`、`api/auth.py`、`api/publish.py`、`services/account_service.py`、`services/scheduler_service.py`。

重命名：`apis/xhs_qianfan_apis.py` → `apis/xhs_pugongying_distributor_apis.py`（+ `xhs_utils/` 同名对称）。

### 6.1 分层违规检测

| 检查项 | 结果 |
|---|---|
| `apis/` 或 `xhs_utils/` 里 import `backend.app.*` | ✅ **未发现** |
| `models/` 里 import `services` 或 `api` | ✅ **未发现** |
| `api/` 或 `services/` 直接 import `apis.*` | ✅ **未发现**（全部经 adapter） |
| 裸 `datetime.now()` | ⚠️ 需复核（`core/security.py` 用 `datetime.now(timezone.utc)` 处理 JWT exp，属合理例外） |
| `models/` 中非 `shanghai_now` 的时间默认值 | ✅ **未发现** |

### 6.2 死代码检测

| # | 位置 | 规模 | 判定依据 |
|---|---|---|---|
| 1 | ~~`backend/app/services/task_service.py`~~ | 整模块 | ✅ **已删除**（2026-09-28，归档至 `.workbuddy/tmp/dead-code-archive/`） |
| 2 | ~~`backend/app/services/mock_data.py` 的 `dashboard_overview` / `sample_accounts` / `sample_model_configs`~~（另含 `sample_tasks`） | 4 个函数 | ✅ **已删除**（2026-09-28）。⚠️ **模块本身保留**：`sample_notes` 被 `api/platforms/xhs/pc.py` 的 mock 端点引用 |
| 3 | ~~`backend/app/api/platforms/xhs/pc.py note_comments_placeholder()`~~ | 1 函数 | ✅ **已删除**（2026-09-28） |
| 4 | ~~`backend/app/core/task_runner.py` 的 `InProcessTaskRunner`~~ | 整模块 | ✅ **已删除**（2026-09-28，归档同上） |
| 5 | `frontend/src/pages/platforms/xhs/drafts-page.tsx` | **1250 行** | 未被 router 引用 |
| 6 | `frontend/src/pages/platforms/xhs/monitoring-page.tsx` | 401 行 | 被 import 但无 `<Route>` |
| 7 | `xhs_utils/xhs_util.py` | 兼容转发层 | 仅转发 `xhs_pc.params` |
| 8 | ~~`backend/app/api/auth.py` 的 `UserResponse`~~ | 1 类 | ✅ **已删除**（2026-09-28）：无 `response_model=` 引用，实际响应由 `_serialize_user()` 出（T-104 注释已随迁） |
| 9 | ~~`backend/app/services/shop_goods_sync_service.py` 的 `_detail_queue()`~~ | 1 函数 | ✅ **已删除**（2026-09-28）：详情补齐已**内联**进同步流程（同文件 `detail_remaining` / `detail_total` 查询），旧独立队列函数无人调用 |

### 6.3 循环依赖检测

| 检查 | 结果 |
|---|---|
| Python 模块循环 import | ✅ **未发现** |
| 前端组件循环引用 | ✅ **未发现** |

### 6.4 孤儿端点（后端有、前端不调）

共 **17 个**（2026-09-28：原 20 个，3 个已消除；最近一次是 `GET /api/xhs/creator/published`），分类见 `05_Mock清单与接线覆盖度.md` §3。

### 6.5 共享资源竞争点

| 资源 | 竞争场景 | 现有防护 |
|---|---|---|
| 代理环境变量 | 多线程同时摘/恢复 | ✅ `request_env.py` 加线程锁 |
| Node 子进程 | 每次签名起一个进程 | 无池化，无竞争（成本换简单） |
| `publish_jobs` 状态 | 调度器 + 手动触发并发 | ⚠️ `max_instances=1` 仅单进程内 |
| `model_configs.is_default` | 并发设默认 | ⚠️ 应用层 `_clear_default_for_type`，无 DB 约束 |
| SQLite 写 | 并发写 | ⚠️ `journal_mode=MEMORY`，非 WAL |

---

## 7. 图谱使用指引

| 场景 | 用本文件的哪节 |
|---|---|
| 「我改这个函数会影响什么」 | §2 依赖矩阵 + §4.1 外键影响面 + §5 域名映射 |
| 「这个功能怎么实现的」 | §3 调用链 |
| 「哪些地方是死代码」 | §6.2 |
| 「有没有架构违规」 | §6.1 |
| 「哪里可能有并发问题」 | §6.5 |
| 「我要加一个端点要动几层」 | §1 分层图 + §3.1 的链路结构 |
