# XHS_ALL_IN_ONE 项目梳理报告

> **项目路径**：`D:\work\XHS_ALL_IN_ONE`  
> **梳理时间**：2025 年  
> **代码范围**：后端、前端、底层 SDK、测试、部署配置全量梳理  
> **结论**：这是一个以小红书 PC/Creator 双端逆向 SDK 为核心，面向内容运营场景的“采集 → 内容库 → AI 改写 → 自动发布”全链路闭环平台。

---

## 1. 项目概览

| 维度 | 说明 |
|---|---|
| **项目名称** | XHS_ALL_IN_ONE（内部代号 Spider_XHS） |
| **定位** | 小红书一站式智能运营平台 |
| **开源协议** | MIT |
| **技术栈** | Python 3.10+ / FastAPI / SQLAlchemy / React 19 / Vite 7 / AntD 6 |
| **入口文件** | `main.py`（后端 Uvicorn + 可选前端 Vite 子进程） |
| **启动方式** | `python main.py` 或 `python main.py --with-frontend` |
| **部署方式** | Docker Compose / 多阶段 Dockerfile |
| **项目承诺** | 仅供学习交流使用，禁止任何商业化行为 |

---

## 2. 顶层目录结构

```
D:\work\XHS_ALL_IN_ONE\
├── main.py                         # 统一启动入口
├── config/
│   ├── default.yaml                # 开发环境配置
│   └── production.yaml             # 生产环境配置
├── backend/
│   └── app/
│       ├── main.py                 # FastAPI 工厂 + lifespan
│       ├── core/                   # 配置、数据库、安全、依赖注入、平台枚举
│       ├── models/                 # SQLAlchemy ORM 模型（20+ 张表）
│       ├── api/                    # REST API 路由（handler 层）
│       ├── services/               # 业务逻辑层
│       ├── adapters/xhs/           # XHS SDK 适配层（pc/creator/login）
│       ├── schemas/                # 通用 schema（分页等）
│       └── storage/                # 媒体文件 + 导出文件存储
├── apis/                           # 小红书底层 SDK（逆向签名 + 接口封装）
│   ├── xhs_pc_apis.py              # PC 端 API
│   ├── xhs_creator_apis.py         # 创作者平台 API
│   ├── xhs_pc_login_apis.py        # PC 登录 API
│   ├── xhs_creator_login_apis.py   # Creator 登录 API
│   ├── xhs_pugongying_apis.py      # 蒲公英平台 API
│   └── xhs_qianfan_apis.py         # 千帆平台 API
├── xhs_utils/                      # 签名算法、参数组装、HTTP 工具
│   ├── xhs_core/                   # PC/通用 签名核心（含 JS 算法文件）
│   ├── xhs_pc/                     # PC 端 auth/params/http/state
│   ├── xhs_creator/                # Creator 端 auth/params/http/state
│   ├── common_util.py              # 通用工具（a1/web_id 生成等）
│   └── cookie_util.py              # Cookie 转换工具
├── frontend/                       # React + Vite 前端
│   └── src/
│       ├── app/                    # 路由、Provider
│       ├── pages/                  # 页面组件
│       ├── components/             # 通用组件
│       ├── lib/                    # API 客户端、平台配置
│       └── types/                  # TypeScript 类型定义
├── spider/                         # 独立爬虫脚本入口（spider.py）
├── tests/                          # pytest 测试（126 passed）
├── static/                         # 签名核心 JS 文件备份
├── data/                           # SQLite 数据库默认路径
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── package.json
└── README.md
```

---

## 3. 后端架构详解

### 3.1 启动与生命周期

- **统一入口**：`main.py` 解析 CLI 参数，调用 `uvicorn.run("backend.app.main:app")`，可选启动前端 Vite 子进程。
- ** lifespan**：
  1. `init_db()`：初始化数据库（优先 Alembic 迁移，失败回退 `create_all`）。
  2. 若 `scheduler_enabled=true`，启动 APScheduler 后台调度器。
- **路由注册**：在 `backend/app/main.py` 中集中 `include_router`，共 20+ 个路由模块。

### 3.2 核心模块分层

| 目录 | 职责 |
|---|---|
| `core/` | 配置（YAML + env）、数据库引擎、JWT/Fernet 安全、依赖注入 `get_current_user`、平台枚举 |
| `models/` | SQLAlchemy ORM 模型，所有表带 `t_` 前缀约定（本项目表名直接命名） |
| `api/` | FastAPI APIRouter，绑定请求/响应模型，调用 service 或 adapter |
| `services/` | 业务编排：AI 调用、平台账号管理、资产下载、定时调度、通知 |
| `adapters/xhs/` | XHS SDK 适配：将底层 `apis/` 和 `xhs_utils/` 封装为业务可直接调用的类 |
| `schemas/` | 通用 DTO，如 `paginated` 分页响应 |

### 3.3 关键 Service

| 文件 | 职责 |
|---|---|
| `ai_service.py` | 定义 `TextAiClient` / `ImageAiClient` Protocol，实现 `OpenAICompatibleTextClient` / `OpenAICompatibleImageClient`，调用任意 OpenAI 兼容 API（火山引擎/阿里云百炼/中转等） |
| `platform_service.py` | 返回平台注册表列表（目前仅 XHS enabled） |
| `scheduler_service.py` | 后台调度核心：定时发布、监控刷新、自动运营任务、Cookie 健康巡检（每 2 小时） |
| `account_service.py` | 账号导入/序列化/enriched profile 处理 |
| `asset_downloader.py` | 素材下载到本地 storage |

---

## 4. 数据模型（核心表）

| 模型 | 表名 | 说明 |
|---|---|---|
| `User` | `users` | 系统用户，密码 pbkdf2_sha256 哈希 |
| `PlatformAccount` | `platform_accounts` | 平台账号（xhs pc/creator），含 status、profile_json |
| `AccountCookieVersion` | `account_cookie_versions` | Cookie 版本历史（加密存储） |
| `LoginSession` | `login_sessions` | 登录会话（二维码/手机验证码临时状态） |
| `Note` | `notes` | 采集/保存的笔记 |
| `NoteAsset` | `note_assets` | 笔记素材（图片/视频） |
| `NoteComment` | `note_comments` | 笔记评论 |
| `Tag` / `note_tags` | `tags` / `note_tags` | 标签 + 多对多关联 |
| `ModelConfig` | `model_configs` | AI 模型配置（text/image，支持多供应商） |
| `AiDraft` | `ai_drafts` | AI 草稿 |
| `DraftAsset` | `draft_assets` | 草稿素材 |
| `AiGeneratedAsset` | `ai_generated_assets` | AI 生成图片资产 |
| `PublishJob` | `publish_jobs` | 发布任务（immediate/scheduled） |
| `PublishAsset` | `publish_assets` | 发布素材 + Creator 上传状态 |
| `Task` | `tasks` | 通用任务记录（爬取/发布/自动运营/监控） |
| `AutoTask` | `auto_tasks` | 自动运营任务配置 |
| `MonitoringTarget` | `monitoring_targets` | 竞品监控目标 |
| `MonitoringSnapshot` | `monitoring_snapshots` | 监控快照 |
| `KeywordGroup` | `keyword_groups` | 关键词组 |
| `Notification` | `notifications` | 系统通知 |
| `ApiLog` | `api_logs` | API 日志 |

---

## 5. XHS 底层 SDK 与适配层

### 5.1 底层 SDK（`apis/` + `xhs_utils/`）

这是项目的**核心资产**，README 明确说明**请勿直接修改**，上层通过 `backend/app/adapters/` 中转调用。

| 子包 | 职责 |
|---|---|
| `apis/xhs_pc_apis.py` | PC 端完整 API 封装：搜索笔记/用户、笔记详情、评论、消息、点赞收藏等 |
| `apis/xhs_creator_apis.py` | 创作者平台 API：上传素材、发布笔记、查看已发布作品 |
| `apis/xhs_pc_login_apis.py` | PC 登录流程：二维码、手机验证码 |
| `apis/xhs_creator_login_apis.py` | Creator 登录流程 |
| `apis/xhs_pugongying_apis.py` | 蒲公英平台 API |
| `apis/xhs_qianfan_apis.py` | 千帆平台 API |
| `xhs_utils/xhs_core/` | 通用签名核心：auth、http、params、dsl、runtime，内置 JS 算法文件（`js/` 目录） |
| `xhs_utils/xhs_pc/` | PC 端签名状态机：b1 生成、rap fingerprint、DSL anchor |
| `xhs_utils/xhs_creator/` | Creator 端签名状态机 |
| `xhs_utils/common_util.py` | a1 / web_id 生成 |

**关键设计**：Python 层通过 `PyExecJS` / Node.js 子进程执行 JS 签名算法，`runtime.py` 负责 Node 适配。

### 5.2 适配层（`backend/app/adapters/xhs/`）

| 适配器 | 职责 |
|---|---|
| `pc_api_adapter.py` | 封装 PC 搜索/详情/评论/用户笔记调用，注入 `direct_xhs_request_env` 隔离代理环境变量 |
| `creator_api_adapter.py` | 封装 Creator 上传/发布/话题/地点/已发布作品 |
| `pc_login_adapter.py` | PC 二维码/手机登录 |
| `creator_login_adapter.py` | Creator 二维码/手机登录，支持 `exchange_from_user_cookies` 从 PC Cookie 换取 Creator Cookie |
| `request_env.py` | 临时清空 `HTTP_PROXY/HTTPS_PROXY` 等环境变量，防止代理污染 XHS 请求 |

---

## 6. 前端架构详解

### 6.1 技术栈

- **框架**：React 19 + TypeScript 5.9
- **构建**：Vite 7
- **UI 库**：Ant Design 6
- **状态管理**：无显式 Redux，使用组件本地 state + 直接 API 调用
- **路由**：React Router DOM 7
- **图表**：Recharts 6
- **工具库**：Axios、Zod、Lucide React、DnD Kit（拖拽排序）
- **HTTP 客户端**：`frontend/src/lib/api.ts`，统一拦截器 + Refresh Token 自动续期

### 6.2 路由结构

| 路径 | 页面 | 说明 |
|---|---|---|
| `/login` | LoginPage | 登录页 |
| `/platform-select` | PlatformSelectPage | 平台选择页 |
| `/platforms/xhs/dashboard` | XhsDashboard | 仪表盘 |
| `/platforms/xhs/accounts` | XhsAccountsPage | 账号矩阵 |
| `/platforms/xhs/discovery` | XhsDiscoveryPage | 笔记发现（搜索） |
| `/platforms/xhs/crawler` | XhsCrawlerPage | 数据抓取（批量 URL/搜索/评论） |
| `/platforms/xhs/library` | XhsLibraryPage | 内容库 |
| `/platforms/xhs/drafts` | XhsDraftsPage | 草稿工坊（AI 改写/生成） |
| `/platforms/xhs/image-studio` | XhsImageStudioPage | 图片工坊 |
| `/platforms/xhs/video-studio` | XhsVideoStudioPage | 视频工坊 |
| `/platforms/xhs/publish` | XhsPublishPage | 发布中心 |
| `/platforms/xhs/auto-ops` | AutoOpsPage | 自动运营 |
| `/platforms/xhs/keywords` | XhsKeywordsPage | 关键词管理 |
| `/platforms/xhs/analytics` | XhsAnalyticsPage | 数据洞察 |
| `/platforms/xhs/monitoring` | XhsMonitoringPage | 竞品监控 |
| `/tasks` | TaskCenterPage | 任务中心 |
| `/models` | ModelConfigPage | 模型配置 |
| `/settings` | SettingsPage | 设置 |

---

## 7. 关键业务流程

### 7.1 账号矩阵
- **三种登录方式**：二维码登录、手机验证码登录、Cookie 导入
- **双账号类型**：PC 端（`sub_type=pc`）用于数据采集；Creator 端（`sub_type=creator`）用于发布
- **Cookie 加密存储**：Fernet 对称加密，每次登录生成新 `AccountCookieVersion` 版本
- **健康巡检**：后台每 2 小时调用账号对应 adapter 的 `get_user_info` 校验有效性，过期自动创建 `Notification`

### 7.2 笔记发现与内容库
- **搜索**：前端调用 `/api/xhs/pc/search/notes`，后端通过 `XhsPcApiAdapter` 调 PC SDK
- **详情**：`/api/xhs/pc/notes/detail`，解析笔记正文/图片/视频
- **评论**：`/api/xhs/pc/notes/comments`，递归展平二级评论
- **保存到库**：`/api/notes/batch-save`，去重 upsert + 自动下载图片视频到本地 `storage/media`

### 7.3 草稿工坊与 AI
- **深拷贝**：从 `Note` 深拷贝生成 `AiDraft`
- **AI 改写/生成/润色**：调用 OpenAI 兼容 Chat Completions API
- **AI 图片生成**：调用 OpenAI 兼容 Images API（支持参考图 base64）
- **图片描述**：多模态 Chat Completions（image_url）

### 7.4 发布中心
- **草稿发送到发布**：创建 `PublishJob` + `PublishAsset`
- **素材上传**：通过 `XhsCreatorApiAdapter.upload_media` 上传到 Creator 平台
- **发布笔记**：调用 `post_note`，支持 `immediate` / `scheduled`（定时发布时间戳 ms）
- **发布选项**：topics、location、privacy_type 均序列化到 `publish_options` JSON

### 7.5 自动运营（Auto Tasks）
- **配置**：关键词列表、PC/Creator 账号、AI 改写指令、调度类型（manual/daily/weekly/interval）
- **执行管线**：
  1. 随机选关键词搜索热门笔记
  2. 按互动量（点赞+收藏+评论+分享）选最高笔记
  3. AI 改写标题+正文
  4. Creator 上传图片素材
  5. 自动创建 `PublishJob` 并发布
- **调度器**：APScheduler `BackgroundScheduler`，支持 `--scheduler-enabled` 启动

### 7.6 竞品监控
- **监控目标**：关键词/账号/品牌/URL
- **刷新逻辑**：在已有内容库 `notes` 中按目标值模糊匹配，计算互动量生成快照 `MonitoringSnapshot`

---

## 8. 安全与认证

| 机制 | 实现 |
|---|---|
| **用户认证** | JWT（access_token 15min / refresh_token 7day），自定义 HS256 签名 |
| **密码存储** | PBKDF2-HMAC-SHA256，260000 迭代，兼容 legacy SHA256 |
| **敏感数据加密** | Fernet 对称加密（Cookie、API Key），密钥可从 `secret_key` 派生 |
| **数据权限** | 所有列表/操作均追加 `user_id = current_user.id`，无 `super_admin` 豁免逻辑 |
| **CORS** | 支持配置多来源，开发环境默认 `localhost:5173/3000` |
| **代理隔离** | `direct_xhs_request_env` 临时清空系统代理变量，避免企业代理干扰 XHS 请求 |

---

## 9. 配置与数据库

### 9.1 配置分层
1. `config/default.yaml`（默认）
2. `CONFIG_FILE` 环境变量指定的 YAML
3. `.env` 文件
4. 环境变量（最高优先级）

### 9.2 数据库
- **开发**：SQLite（`data/spider_xhs.db`）
- **生产**：MySQL（`mysql+pymysql`）
- **迁移**：Alembic，启动时自动 `upgrade head`
- **兼容处理**：旧默认路径自动迁移、model_name 标准化（`gpt5.4` → `gpt-5.4`）、SQLite 时区自动修正（+8h）

---

## 10. 测试概况

测试位于 `tests/backend/`，使用 `pytest` + FastAPI `TestClient`：

| 测试文件 | 覆盖内容 |
|---|---|
| `test_api.py` | 健康检查、平台注册表、认证全流程（注册/登录/refresh/me）、登录会话（QR/手机）、Cookie 导入/校验/过期、账号删除权限、数据库迁移、SQLite 时区修正、前端页面代码回归（source 级断言） |
| `test_platforms.py` | 平台枚举（XHS enabled，其他 coming_soon） |
| `test_root_main.py` | `main.py` CLI 参数解析、前端命令构建 |

**共 126 个 passed**，测试策略混合了：
- 后端 API 集成测试（SQLite in-memory / tmp_path）
- 前端页面源码级回归测试（读取 `.tsx` 断言关键字存在）

---

## 11. 部署要点

| 方式 | 命令/说明 |
|---|---|
| **本地开发** | `pip install -r requirements.txt && npm install && python main.py --with-frontend` |
| **前端开发** | `cd frontend && npm install && npm run dev`（Vite 代理 `/api` → `127.0.0.1:8000`） |
| **Docker** | `docker compose up -d`（单容器，前端构建产物 COPY 进 Python 镜像） |
| **端口** | 后端默认 8000，前端默认 5173 |
| **数据卷** | `./data:/app/data`、`./config:/app/config`、`backend/app/storage/media`、`backend/app/storage/exports` |

---

## 12. 待扩展平台

`backend/app/core/platforms.py` 已预注册 6 个平台，仅 **XHS** 已实现：

| 平台 ID | 名称 | 状态 |
|---|---|---|
| `xhs` | 小红书 | ✅ enabled |
| `douyin` | 抖音 | coming_soon |
| `kuaishou` | 快手 | coming_soon |
| `weibo` | 微博 | coming_soon |
| `xianyu` | 闲鱼 | coming_soon |
| `taobao` | 淘宝 | coming_soon |

前端也有 `ComingSoonPage` 组件兜底未实现平台。

---

## 13. 关键风险/注意点

1. **逆向 SDK 脆弱性**：`apis/` + `xhs_utils/` 依赖小红书前端 JS 签名算法，平台端更新可能导致签名失效，属于项目最高风险点
2. **Cookie 时效**：PC/Creator Cookie 均有过期风险，项目已内置 2 小时巡检 + 通知，但无法完全避免
3. **代理环境**：企业代理环境必须配置 `direct_xhs_request_env` 隔离，否则签名请求易被污染
4. **Node.js 依赖**：Docker 镜像必须安装 Node.js 20，用于 JS 签名运行时
5. **仅图片发布**：当前 Creator 发布明确拒绝视频（`raise "视频发布功能即将上线"`）
6. **测试混合策略**：部分测试直接读取前端源码做字符串断言，重构前端时需同步更新测试

---

## 14. 后续可深挖方向

如需进一步下钻，可继续按以下维度细化：
- 签名算法与 JS 运行时（`xhs_utils/xhs_core/js/`）
- 自动运营调度器精确行为（`scheduler_service.py`）
- 前端页面组件树与状态流转（`frontend/src/pages/platforms/xhs/`）
- 数据库迁移历史（`backend/alembic/versions/`）
- AI 提示词工程与多模态调用细节（`ai_service.py`）
