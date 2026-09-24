# 功能设计 · 后端 API 与服务层

> - **阶段**：二 · 设计
> - **访问端**：HTTP API 消费方（Web 前端、第三方脚本、上层 Agent 工具链）
> - **规模**：23 个 router / **180 个端点**（`create_app()` 后 OpenAPI 实测 `operations = 180`；path 条数 156；2026-09-24 设计 15：+12 `/api/schedule`、−1 `/tasks/scheduler/status`；2026-09-23 §18.15 再校：+1 端点 `POST /xhs/login-sessions/qianfan/email-login`（千帆邮箱密码登录）；**2026-09-23 五校（设计 13 §33.10 商品列表 v1.36：+1 端点 `POST /xhs/qianfan/items/delete`）**。此前 2026-09-23 再校（设计 13 §31 商品笔记生成与发布：+4 端点）。其中 2026-09-19 起新增千帆 8 端点 + 商品笔记 1 端点，2026-09-21 新增账号登录态 1 端点，2026-09-22 新增内容库发布状态 1 端点、单条同步 1 端点，**移除独立对账 2 端点**（对账融合进同步，设计 13 §27）、话题增删 1 端点（设计 13 §29））
> - **版本**：v1.17 · 2026-09-22（**数据对账**（设计 13 §24）：新增 `POST /api/accounts/{id}/notes/reconcile` 与 `POST /api/xhs/qianfan/reconcile-shop-goods`；对账范围与同步条件同源（`scope`/`start_date`/`end_date`），本地多出的行**打状态标记不删** ⇒ 端点 160 → **162**、path 140 → **142**）。v1.16 · 2026-09-22（**内容库正文与图集补齐 + 来源账号**（设计 13 §19.13 / 设计 04 §2.12）：`GET /notes/{id}` 新增查询参数 **`hydrate`**（`true` = 先对本条做一次详情补齐再返回，**不新增端点**）；`_serialize_note` 增字段 **`account_name`**（来源账号，列表/详情/导出三处一致，查不到留空串）。端点 **160** / path **140** / 请求函数 **137** 均不变）。v1.15 · 2026-09-22（**内容库「是否发布过」标识**（设计 12 §22 / 设计 04 §2.12 / 设计 13 §19.12）：`GET /notes` 加三态 `published` 过滤 + **全库口径** `stats{published,unpublished}`；新增 **`PATCH /api/notes/{id}/published`**（手动切换）⇒ 端点 159 → **160**、path 139 → **140**）。v1.14 · 2026-09-21（**新增 `POST /api/accounts/{account_id}/login-state`**（设计 13 §18）：端点 156 → **156**、path 135 → **136**；`GET /api/accounts` 每项新增 `login_channels` 字段（**不新增端点，随列表下发**））。v1.13 · 2026-09-21（**「规模」行按实测回数**：router 21 → **22**、端点 147 → **156**、path 127 → **135**；与 `AGENTS.md` §5 同口径，漂移会被 `verify_doc_counts.py` 拦下）。v1.12 · 2026-09-21（`POST /xhs/qianfan/shops` 的 `account_count` / `accounts[]` 明确**只数当前租户下账号未软删的绑定**，DEF-054/055）。v1.11 · 2026-09-19（v1.11 T-401/403/405 新增 `xhs/product-notes/generate` → 147 operations / 127 paths；v1.10 T-32 新增 `qianfan/shops`；v1.9 商品映射改**按需补齐**）

---

## 1. API 设计约定

| 项 | 约定 |
|---|---|
| 前缀 | **全部 `/api`**（`backend/app/main.py:49-68` 统一挂载） |
| 鉴权 | `OAuth2PasswordBearer(tokenUrl="/api/auth/login")` + `Depends(get_current_user)` |
| 令牌 | 自研 JWT（HMAC-SHA256）；**access 15 分钟 / refresh 7 天** |
| 响应格式 | 大部分为裸对象或 `{items, total}`；**无统一 envelope** |
| 分页 | 响应体里的 `total` + 查询参数；**无统一分页参数命名** |
| 错误 | `HTTPException(status_code, detail)`，中文/英文 detail 混用 |
| 流式 | 抓取接口用 **SSE**（`text/event-stream`） |
| 文档 | 自动生成于 `/docs`（FastAPI Swagger） |

### 1.1 鉴权与数据隔离

```
请求 → OAuth2PasswordBearer 提取 Bearer token
     → decode_token()  校验 HMAC + exp + token_type=="access"
     → db.get(User, payload["user_id"])
     → 注入 current_user

数据隔离：业务查询带 user_id 过滤
  ✅ 生效：notes / drafts / publish_jobs / auto_tasks / monitoring_targets /
           model_configs / keyword_groups / tags / tasks / notifications
  ⚠️ 间接：account_cookie_versions（经 platform_account_id → user_id）
  ✅ 新增：xhs/pc 自有内容 5 端点（`/users/*`）全部要求 `get_current_user` + 账号归属校验
  ❌ 仍缺失：xhs/pc 剩余 3 个 mock 端点（search/users、homefeed×2）无鉴权依赖参数
```

**实现位置**：`backend/app/core/deps.py:14-30`、`backend/app/core/security.py:74-105`

### 1.2 令牌刷新流程

```
登录  → { access_token(15min), refresh_token(7d) }
业务  → 401（access 过期）
前端  → 响应拦截器带 refresh_token 调 /auth/refresh
      → 成功：重放原请求；失败：清凭证跳登录
```

---

## 2. 端点清单（按模块）

### 2.1 `auth`（5）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register` | 注册（username + password） |
| POST | `/api/auth/login` | 登录，返回双令牌 |
| POST | `/api/auth/refresh` | 用 refresh token 换新 access |
| GET | `/api/auth/me` | 当前用户信息 |
| POST | `/api/auth/logout` | 登出（前端清凭证） |

### 2.2 `platforms`（1）

| GET | `/api/platforms` | 返回平台注册表（6 个平台，仅 xhs `enabled=true`） |

### 2.3 `accounts`（7）

| 方法 | 路径 | 说明 | 备注 |
|---|---|---|---|
| GET | `/api/accounts` | 账号列表（含派生信息） | 每项含 `login_channels`（L1/L2 登录态，**零平台请求**）—— 2026-09-21 新增，见设计 13 §18.11 |
| POST | `/api/accounts/import-cookie` | Cookie 导入建号 | 追加 Cookie 版本 |
| POST | `/api/accounts/{id}/check` | 健康检查 | ⚠️ creator 侧历史上必误判（已修，见 `05_项目沉淀/02`） |
| POST | `/api/accounts/{id}/login-state` | **账号维度登录态**（三层判据）。body `{probe: bool = false}` | ✅ **2026-09-21 新增**（设计 13 §18.11）。`probe=false` = 纯本地零请求；`probe=true` 对**已确认是登录态**的通道各发 1 次**只读**探针（www → `/v2/user/me`；ark → `/api/edith/seller/info/v2`）。响应 = `{channels: {www, ark}, checked_at}`；每个通道给 `{state, reason, probed, label}`，`state ∈ missing/guest/valid/expired/unknown`。**⚠️ `unknown` 绝不渲染成 `expired`**（探针「没问到」≠「平台拒绝」，DEF-057 同型红线） |
| POST | `/api/accounts/{id}/metrics` | **账号级互动数据同步**（粉丝 / 关注 / 获赞与收藏） | ✅ **2026-09-18 新增**；响应 = `serialize_account()` + `metrics_reason`，与 `/check` 同构 |
| DELETE | `/api/accounts/{id}` | 删除账号 | — |

> **`/metrics` 取数口径（2026-09-18 真机实测确立，禁止在别处另写一套）**：
> ① 按 **Cookie 能力**路由（`web_session` 是否存在），**不看 `sub_type`**；
> ② PC 链路 = `/v1/user/otherinfo?target_user_id=<self>` 的 `data.interactions[]`
> （`fans→followers` / `follows→following` / `interaction→likes`）；
> ③ creator-only 链路**结构上不可得**（`/api/galaxy/user/info` 实测无任何计数）→ **零平台请求**直接返回 `metrics_reason`；
> ④ 取不到值时**保留旧值**（失败刷新不得抹掉好数据）；
> ⑤ `/check`、`/import-cookie`、`/metrics` 三个入口共用同一取数 / 落库函数（同一事实只能有一个真相源）；
> ⑥ `metrics_reason` 文案受**长度窗口约束**（25–30 字，R10 / D16）：固定原因写成常量，动态部分（平台 `msg`、异常文本）经 `clamp_reason()` 截断到 30 字。
> 详见 `12_功能设计_我的笔记与互动数据.md` §21 与 `04_功能设计_Web运营端.md` §2.4。
| ~~PATCH~~ | ~~`/api/accounts/{id}`~~ | ✅ **已下线**（2026-09-18，T-105）：原 stub 恒返回 `{"status":"updated"}`，不查库不落库无鉴权 | 已核实 `PlatformAccount` 上不存在用户可编辑字段 → 选择下线而非实现。详见 ADR-015 |

### 2.4 `xhs/login-sessions`（9）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/xhs/login-sessions/pc/qrcode` | 建 PC 扫码会话 |
| POST | `/api/xhs/login-sessions/creator/qrcode` | 建 Creator 扫码会话 |
| POST | `/api/xhs/login-sessions/qianfan/qrcode` | 建千帆扫码会话（2026-09-21；`target_account_id` **可选** —— 2026-09-23 起支持扫码建号，设计 13 §18.15.3） |
| GET | `/api/xhs/login-sessions/{id}` | 轮询会话状态 |
| POST | `/api/xhs/login-sessions/{sub_type}/phone/send-code` | 发短信码 |
| POST | `/api/xhs/login-sessions/{sub_type}/phone/confirm` | 确认手机登录 |
| POST | `/api/xhs/login-sessions/qianfan/email-login` | **千帆邮箱密码登录**（2026-09-23，设计 13 §18.15.3）：同步一次请求完成全链（emailPwd → ticket/route → 导航换票 → ssologin → seller/login），落库仅 Cookie 信封；**邮箱与密码不保留**（不落库、不进 `LoginSession`、不回显）。body `{email, pwd, target_account_id?}`：无 target = 建号（`sub_type=qianfan`），有 target = 续期（店铺对账） |

> 设计要点：会话临时态（`qr_id` / `code` / 临时 Cookie）**加密落库**，轮询驱动状态机 `pending → scanned → confirmed`。成功后 upsert `platform_accounts` 并按 `external_user_id` 匹配。千帆邮箱密码登录**不建会话行**（无中间态），凭据只在当次请求内存里。

### 2.5 `notes`（13）

内容库 CRUD + 批量 + 导出 + 评论。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/notes/ids` | 已保存笔记 ID 集合（用于发现页打标） |
| GET | `/api/notes` | 列表（筛选/分页）。**2026-09-22 新增**查询参数 `published`（不传/`true`/`false` 三态）+ 响应体增补 `stats`；**2026-09-23 改**（设计 13 §32.3.3）：`published` 参数**删除**，改 `publish_status`（`all`/`pending`/`published`/`failed` **四档**，未知值 → 400 `NOTE_STATUS_UNSUPPORTED`），`stats` 改 **`{pending, published, failed}`** 三档。**同日晚些再改**（设计 13 §25.7）：新增 `platform_account_id`（来源账号，**不校验存在性**，别人的 id ⇒ 空集不 404）+ `note_type`（`synced`/`local_generated`/`local` 三档，未知值 → 400 `NOTE_TYPE_UNSUPPORTED` + `supported` 列表），两者均为**可选、不传 = 不筛**，与既有 `publish_status` 白名单同口径；`stats` 仍是**全库口径**、不随任何筛选变化 |
| GET | `/api/notes/{id}` | 详情。**2026-09-22 新增**查询参数 `hydrate`（不传/`false` = 只读本地；`true` = 先对本条做一次详情补齐再返回）。响应体增字段 `account_name`（来源账号昵称，取不到为 `""`），`hydrate=true` 时另带 `hydrate`（`{status, reason?, content_filled?, images_added?}`） |
| PATCH | `/api/notes/{id}/published` | ~~**切换「是否已发布过」**（2026-09-22 新增）。body `{published: bool}`~~ —— **已被 2026-09-23 的 `PATCH /api/notes/{id}/publish-status` 取代**（设计 13 §32.3.3：发布状态收敛为一个字段，旧布尔端点删除） |
| PATCH | `/api/notes/{id}/publish-status` | **切换发布状态**（2026-09-23 新增，设计 13 §32.3.3）。body `{publish_status: "pending" \| "published"}`（手切白名单只有这两档，`Literal` 校验），返回更新后的笔记（含 `tags`）。**本地生成笔记 → 400 `NOTE_STATUS_MANAGED`**（状态由发布链路决定，界面不给手切入口）；越权 404。写入经唯一入口 `models/note.py::set_note_publish_status()`，`published` 列作为**投影**同步更新 |
| POST | `/api/notes/{id}/sync` | **单条同步**（2026-09-22 新增，设计 13 §25）：用笔记 id 拉平台最新内容更新落库。⚠️ 2026-09-23 用户口径「已发布的笔记也支持同步笔记最新的三方数据」⇒ 判据放宽为 **`synced` 或 `publish_status=published`**（`models/note.py::note_supports_sync()`，与前端 `canSyncNote` 同口径）；两者都不满足 ⇒ 400 **`NOTE_NOT_SYNCABLE`**（原 `NOTE_NOT_SYNCED` 已随口径改名）。与定时任务同步逻辑同源（复用 `hydrate_note_detail` 唯一实现）。失败也回 200，结果在 `sync.{status,reason,code}`（`no_session` ⇒ `WEB_SESSION_REQUIRED` 安静降级） |
| PUT | `/api/notes/{id}/topics` | **话题整列替换**（2026-09-22 新增，设计 13 §29.3）：内容库话题增删的唯一落库入口。body `{topics: string[]}`，后端归一化（trim / 去空 / 去重 / 上限 20 个、单项 ≤50 字，超限静默截断）后整列覆写 `topics_json`，返回更新后的整行（含 `topics`）。越权 404。平台写入方仍是同步链路（§28.2） |
| DELETE | `/api/notes/{id}` | 删除 |
| POST | `/api/notes/batch-save` | 批量入库（采集结果） |
| POST | `/api/notes/batch-tag` | 批量打标签 |
| POST | `/api/notes/batch-create-drafts` | 批量建草稿 |
| POST | `/api/notes/export` | 导出 JSON/CSV |
| GET/DELETE/PATCH | `/api/notes/{id}/assets[/{aid}]` | 素材增删改 |
| PUT | `/api/notes/{id}/assets/reorder` | 素材排序 |
| GET | `/api/notes/{id}/comments` | 评论列表 |

> ⚠️ 路由顺序敏感：`/notes/ids`（`:211`）必须声明在 `/notes/{note_id}`（`:301`）之前。

**`publish_status` 与 `stats` 的口径（2026-09-23 起；旧 `published` 两态口径见下方对照）**

* `publish_status` **四档**：`all`（不传 = 不过滤）/ `pending` / `published` / `failed`。
  ⚠️ **未知值一律 400 `NOTE_STATUS_UNSUPPORTED`，不猜、不静默当 `all`**（与商品列表
  `status` 筛选同口径）。
* `pending` 档**含**瞬时的 `publishing` 与迁移前的空值 `""` —— 筛选与统计共用
  `models/note.py::PUBLISH_STATUS_FILTER_GROUPS` **同一份映射**，避免「统计把 `publishing`
  算进待发布、筛选却不含它」这类两处漂移。
* `stats` 是**全库口径**（`{pending: int, published: int, failed: int}`，只按 `user_id` +
  `platform` 统计，**不叠加** `q` / `tag_id` / `has_assets` / `has_comments` /
  `publish_status` 任一筛选）。理由：它服务的是「内容池结构」这一眼，跟着筛选走会在
  只看发布失败时把已发布显示成 0，用户读不出池子结构。
  ⇒ **筛选**与**统计**是两件事，`stats` 不随筛选变化，前端也不得用 `items` 长度反推。
  **三数之和 = 该用户（该平台）的笔记总数**（迁移 `v5w6x7y8z9a0` 之后不存在第四态）。
* 列表与详情**同一份** `_serialize_note` 输出（硬约束：同一语义不留两份实现），
  因此 `publish_status` 在列表、详情、导出 JSON 三处天然一致。

> **被取代的旧口径（原句保留，供对照）**：「`published` **必须显式判三态**
> （`Optional[bool]`，不传 = 不过滤）。⚠️ 不要写成 `if published:` —— 那会让「只看待发布」
> `published=false` **静默失效**」；「`stats` 是**全库口径**（`{published: int,
> unpublished: int}`…）」。
> **被 2026-09-23 的 §32.3 取代**（用户口径「后端数据库表**应是一个字段**，发布状态字段
> （待发布，已发布，发布失败）」）：两列各自表达发布与否，界面上就成了两个状态标签并排，
> 既重复又可能互相矛盾，筛选还缺「发布失败」这一维。教训（三态参数别写成 `if x:`）
> 仍成立，只是**判据换成了 `publish_status` 的四档校验**。

**`hydrate` 与 `account_name` 的口径（2026-09-22）**

* `hydrate` **默认为假义**（不传 = 不发任何平台请求），只有显式 `true` 才补一次
  （这一条有守卫钉死：不带 `hydrate` 时平台请求数必须为 0，否则测试变红）；
* 补齐走**本账号自己**对应通道的令牌（§17.10）；撞限流 / 失败**不报 5xx** ——
  仍返回 200 + 本地现状 + `hydrate` 里如实的原因（失败三分法：取不到 ≠ 接口异常）；
* `account_name` 在**列表、详情、导出**三处同样一致（同源 `_serialize_note`）；
  **查不到账号留空串**，前端按缺值口径整项不渲染（不写「未知」）；
* 补齐的落库规则（正文只在空时填、图集只增不删、`hydrated_at` 三态）见设计 13 §19.14（2026-09-23 起同步链路改**页内联补齐**：本页落库后就地打详情，一次同步拿全再落库；账号级补齐队列与「每轮 cap=30 多轮收敛」已移除，取代对照表见 §19.14.1）。

### 2.6 `files`（7）

| 上传 | `POST /api/files/upload` | 通用文件上传（素材、图片） |
| 媒体 | `GET /api/files/media/{name}` | 读取本地媒体文件 |
| 导出 | `GET /api/files/exports/{name}` | 下载导出文件 |
| 图片 | `GET /api/files/images` | 用户图片列表 |
| 图片 | `DELETE /api/files/images/{name}` | 删除 |
| 图片 | `POST /api/files/images/compose` | 图片合成 |
| 图片 | `POST /api/files/images/resize` | 图片缩放 |

### 2.7 `drafts`（10）

草稿 CRUD + 素材 + 送发布。

```
GET/POST          /api/drafts
PATCH/DELETE      /api/drafts/{id}
GET/POST          /api/drafts/{id}/assets
DELETE/PATCH      /api/drafts/{id}/assets/{aid}
PUT               /api/drafts/{id}/assets/reorder
POST              /api/drafts/{id}/send-to-publish   ← 创建 publish_job
```

### 2.8 `ai`（10）

| 文本 | `POST /ai/rewrite-note` | 改写 |
| 文本 | `POST /ai/generate-note` | 生成标题+正文 |
| 文本 | `POST /ai/generate-title` | 多标题候选 |
| 文本 | `POST /ai/generate-tags` | 标签 |
| 文本 | `POST /ai/polish-text` | 润色 |
| 图片 | `GET /ai/images/assets` | 生成的图片资产列表 |
| 图片 | `DELETE /ai/images/assets/{id}` | 删除 |
| 图片 | `POST /ai/images/generate-cover` | 生成封面 |
| 图片 | `POST /ai/images/generate` | 生成配图（可带参考图） |
| 图片 | `POST /ai/images/describe` | 图片理解 → 文字 |

**模型解析规则**：按 `model_type`（`text` / `image`）+ `is_default=True` 取配置（`api/ai.py:89-105`）。无默认文本模型 → 400。

### 2.9 `tasks`（5）

| GET | `/api/tasks` | 任务列表（一次性任务的执行记录） |
| GET | `/api/tasks/{id}` | 任务详情（前端未调用） |
| POST | `/api/tasks/{id}/cancel` | 取消 |
| POST | `/api/tasks/{id}/retry` | 重试 |
| POST | `/api/tasks/run-due` | 手动执行到期任务 |

> ⚠️ 2026-09-24（设计 15 §8）：`GET /api/tasks/scheduler/status` **已删除**，由
> `GET /api/schedule/status` 取代（调度器活值 + 全部任务行 + 状态计数）。

### 2.10 `model-configs`（6）

```
GET/POST   /api/model-configs
PATCH/DELETE /api/model-configs/{id}
POST       /api/model-configs/{id}/set-default
POST       /api/model-configs/{id}/test          ← 连通性测试
```

### 2.11 `tags`（4）/ `notifications`（3）/ `keyword-groups`（5）

```
GET/POST   /api/tags          PATCH/DELETE /api/tags/{id}      ← 后两者前端未接线
GET/POST   /api/notifications  .../{id}/read  .../read-all
GET/POST   /api/keyword-groups  GET/PATCH/DELETE /api/keyword-groups/{id}
```

### 2.12 `publish`（11）

```
GET          /api/publish/jobs
GET/PATCH/DELETE /api/publish/jobs/{id}
POST         /api/publish/jobs/{id}/publish     ← 触发发布
POST         /api/publish/jobs/{id}/retry
POST         /api/publish/jobs/{id}/cancel
GET/POST     /api/publish/jobs/{id}/assets
DELETE       /api/publish/assets/{id}
POST         /api/publish/assets/{id}/upload    ← 上传素材到 Creator
```

**发布前的校验链**（`api/publish.py`）：
1. 发布模式校验（`scheduled` 必须有 `scheduled_at`）
2. 账号 `sub_type == "creator"`（否则 400）
3. 素材存在性
4. 视频素材显式拒绝（`:499` 抛「视频发布功能即将上线」）
5. 应用 `publish_options`（可见性 → 笔记 `type`）

### 2.13 `xhs/analytics`（9）

`overview` / `top-content` / `hot-topics` / `comment-insights` / `benchmarks` / `benchmarks/{id}/create-drafts` / `reports` / **`engagement`** / **`keyword-trends`**（后两个无 UI）。

> 全部为**真实 DB 查询实现**（读 `notes` / `platform_accounts` / `publish_jobs`）。

### 2.14 `xhs/pc`（12）

| 方法 | 路径 | 状态 |
|---|---|---|
| POST | `/api/xhs/pc/search/notes` | ✅ 真实 |
| POST | `/api/xhs/pc/notes/detail` | ✅ 真实 |
| POST | `/api/xhs/pc/notes/comments` | ✅ 真实 |
| POST | `/api/xhs/pc/users/me` | ✅ 真实 **（2026-09-18 新增）** |
| POST | `/api/xhs/pc/users/notes` | ✅ 真实 **（2026-09-18 去 mock，见 `12_功能设计_我的笔记与互动数据`）** |
| POST | `/api/xhs/pc/users/liked` | ✅ 真实 **（新增）** |
| POST | `/api/xhs/pc/users/collected` | ✅ 真实 **（新增）** |
| POST | `/api/xhs/pc/users/interactions` | ✅ 真实 **（新增，`kind` ∈ like_collect/mention/new_connection）** |
| POST | `/api/xhs/pc/users/search` | ✅ 真实 **（2026-09-18 新增：跨「我发布的/赞过/收藏」的有界关键词检索，见 `12_功能设计` §22）** |
| POST | `/api/xhs/pc/search/users` | ❌ **mock** |
| GET | `/api/xhs/pc/homefeed/channels` | ❌ **mock** |
| POST | `/api/xhs/pc/homefeed/recommend` | ❌ **mock** |

> 6 个 `/users/*` 端点详见 `12_功能设计_我的笔记与互动数据.md`：只读、纯实时、不落库，单页拉取（禁全量循环）。
> ⚠️ `/users/search` 的「有界扫描」不是全量循环：每类最多 `pages_per_scope` 页（默认 3，上限 5），仍逐个调用单页方法。

### 2.15 `xhs/creator`（6）—— 前端全部未调用

`topics/search`、`locations/search`、`assets/upload`、`publish/image`、`publish/video`、`published`。

> 前端走的是 `/publish/*` 业务层（`publish.py`），平台级 creator 接口属**备用入口**。

### 2.16 `xhs/crawl`（4）—— 前端部分未调用

| POST | `/api/xhs/crawl/data` | ✅ SSE 流式抓取（主入口） |
| POST | `/api/xhs/crawl/search-notes` | ⚠️ 未接线（前端改走 `/pc/search/notes`） |
| POST | `/api/xhs/crawl/note-urls` | ⚠️ 未接线 |
| POST | `/api/xhs/crawl/user-notes` | ⚠️ 未接线（**有真实能力但闲置**） |

### 2.17 `xhs/monitoring`（7）

```
GET/POST   /api/xhs/monitoring/targets
PATCH/DELETE /api/xhs/monitoring/targets/{id}     ← PATCH 前端已定义但无页面调用
POST       /api/xhs/monitoring/targets/{id}/refresh
GET        /api/xhs/monitoring/targets/{id}/snapshots
GET        /api/xhs/monitoring/targets/{id}/notes
```

### 2.18 `auto-tasks`（5）

```
GET/POST   /api/auto-tasks
PATCH/DELETE /api/auto-tasks/{id}    ← PATCH 用于业务字段 + schedule 对象（调度设置）
POST       /api/auto-tasks/{id}/run  ← 立即执行（走统一 runner，返回 {auto_task, log, detail}）
```

> ⚠️ 2026-09-24（设计 15 §7.1）：请求/响应里的 `schedule_type` / `schedule_time` /
> `schedule_days` / `schedule_interval_hours` 四字段**移除**，改为可选 `schedule`
> 对象（`trigger_type` + `cron_expression` / `interval_seconds` / `run_at` +
> `misfire_policy` + `concurrent` + `enabled`），落到 1:1 的 `scheduled_jobs` 行；
> 响应新增 `schedule` 摘要（job_id / 触发器 / 状态 / 下次运行 / 执行计数）。
> 「立即执行」不再创建 `tasks` 行，执行记录以调度日志为准。

### 2.18b `schedule`（12）—— 2026-09-24 设计 15 §8 新增

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/schedule/jobs` | 任务列表（筛 `job_group`/`status`/`trigger_type`/关键字，带调度器活值 next_run_time） |
| POST | `/api/schedule/jobs` | 新建（job_key 白名单；cron 实解析；at 校验 run_at 在未来） |
| GET | `/api/schedule/jobs/{id}` | 详情 + 最近 10 条日志 |
| PUT | `/api/schedule/jobs/{id}` | 修改（builtin 禁改 key/params：400 `JOB_BUILTIN_PROTECTED`） |
| DELETE | `/api/schedule/jobs/{id}` | 删除（builtin 禁删；日志保留、job_id 置空） |
| PATCH | `/api/schedule/jobs/{id}/status` | 暂停 / 恢复 |
| POST | `/api/schedule/jobs/{id}/run` | 执行一次（后台线程，返回日志行 id） |
| GET | `/api/schedule/logs` | 调度日志（筛 job_id/job_key/status/时间范围） |
| DELETE | `/api/schedule/logs` | 按 id 批量删除 |
| DELETE | `/api/schedule/logs/clean` | 清空（可按 job_id 收窄） |
| POST | `/api/schedule/cron/validate` | cron 实解析 + 未来 3 次触发时间 |
| GET | `/api/schedule/status` | 调度器 enabled/running/时区 + jobs 活值 + 状态计数 |

错误码：`JOB_KEY_UNSUPPORTED` / `JOB_BUILTIN_PROTECTED` / `CRON_INVALID` /
`RUN_AT_REQUIRED` / `RUN_AT_INVALID` / `JOB_PARAMS_INVALID` /
`JOB_CONCURRENT_NOT_ALLOWED` / `LOG_STATUS_UNSUPPORTED` / `JOB_NOT_FOUND`。

### 2.19 健康检查

| GET | `/api/health` | `{"status":"ok","service":"spider-xhs"}` |

---

## 3. 服务层设计

| 服务 | 职责 | 被谁调用 |
|---|---|---|
| `ai_service` | 全部 AI 能力：改写 / 生成 / 润色 / 标签 / 图片生成 / 图片理解。**统一的模型解析与 OpenAI 兼容调用** | `api/ai.py`、`auto_ops_service` |
| `scheduler_service` | **全系统自动化中枢**：DB 驱动装配（ensure/sync/prune + API 同步原语）+ 到期发布补跑 + Cookie 巡检 + 同步轮 | `main.py` lifespan、`api/tasks.py`、`api/schedule.py` |
| `job_registry` | **任务注册表（白名单）**：8 项 JobSpec（7 系统 + 自动运营管线）；运行时不可注入 | `scheduler_service`、`scheduled_job_runner`、`api/schedule.py` |
| `scheduled_job_runner` | **调度执行唯一入口**：退避门 → 注册表函数 → 日志（running→终态）+ 计数 + at 办结 | APScheduler、`api/schedule.py`、`api/auto_tasks.py` |
| `auto_ops_service` | 自动运营管线**唯一实现**（关键词→搜索→选优→草稿→AI 改写→建发布任务） | `job_registry`（经 runner） |
| `schedule_triggers` | cron 实解析 / misfire→APScheduler 映射 / 并发硬顶（设计 15 §6） | `scheduler_service`、`api/schedule.py`、`schemas/schedule.py` |
| `account_service` | 账号 upsert（按 `external_user_id`）、Cookie 版本追加、健康检查 | `api/accounts.py`、`api/login_sessions.py` |
| `monitoring_crawl_service` | 监控目标抓取、快照写入、**连续失败自动暂停** | `api/xhs/monitoring.py`、`scheduler_service` |
| `asset_downloader` | 素材下载到 `storage/media/`，处理无水印 URL | `api/xhs/crawl.py` |
| `image_util` | 图片合成 / 缩放 / 格式处理 | `api/files.py` |
| `notification_service` | 站内通知写入（Cookie 过期、任务失败） | 各调度 job |
| `rate_limiter` | 请求限流 | 采集链路 |
| `task_service` | ⚠️ **死模块**：`list_tasks` 返回 `sample_tasks()` 假数据，**无任何调用方** | 无 |
| `mock_data` | ⚠️ **死模块**：5 个假数据函数，其中 3 个完全无引用 | `task_service`、`api/xhs/pc.py`（**1** 个 mock 端点，原 2 个；`/users/notes` 已于 2026-09-18 去 mock） |

### 3.1 `scheduler_service` 详细设计（自动化核心）

> ⚠️ **2026-09-24 被设计 15 取代**：本节原描述「硬编码四个 job + 60s 轮询自动运营」的
> 旧形态（`build_due_publish_scheduler()` / `run_due_auto_tasks()`，均已删除）。
> 现行设计 = **DB 驱动**：`scheduled_jobs` 行是唯一权威，启动 ensure/sync/prune，
> API 写入后同步注册/摘除，时区 Asia/Shanghai，统一执行入口
> `scheduled_job_runner.execute_job()`。详见 `15_功能设计_定时任务中心.md` §4~§8。
> 原句保留备查：

**（已取代）四个 job**（原 `scheduler_service.py:699-737`）：

| Job ID | 频率 | 作用 | 单实例约束 |
|---|---|---|---|
| `due_publish_runner` | `scheduler.interval_seconds`（默认 60s） | 补跑到期的定时发布任务 | `max_instances=1, coalesce=True` |
| `monitoring_refresh_runner` | 同 interval | 刷新所有 active 监控目标快照 | 同上 |
| `auto_tasks_runner` | **60s**（固定） | 自动运营管线 | 同上 |
| `cookie_health_checker` | **2h**（固定） | 全量账号 Cookie 巡检，新过期发通知 | 同上 |

**（已取代）调度器时区**：~~`BackgroundScheduler(timezone="UTC")`~~ → 现为 `Asia/Shanghai`（设计 15 §6.6，修 S-4）。

**（已取代）自动运营管线**（原 `run_due_auto_tasks`，60s 轮询）：每轮扫描 auto_tasks
（active ∧ 非 manual ∧ next_run_at ≤ now）→ 挑关键词 → 搜索 → 选优 → 建草稿 →
AI 改写 → 上传素材 → 发布 → 重算 next_run_at（执行后计算，耗时计入下轮间隔）。
现行实现 = `auto_ops_service.run_keyword_pipeline`（唯一实现），调度触发建
`scheduled`（now）发布任务由 `due_publish_runner` 走完整护栏发布；手动触发建
`immediate` 待发布任务。

**关键设计（仍有效）**：排期用 `scheduled_at` 落库 + 补跑，**不用执行侧 sleep** → 进程崩溃不丢任务，且补跑天然幂等。

---

## 4. SSE 流式抓取设计

| 项 | 设计 |
|---|---|
| 端点 | `POST /api/xhs/crawl/data` |
| 传输 | `text/event-stream` |
| 前端 | 原生 `fetch`（非 axios，`lib/api.ts:401`） |
| 关键配置 | Vite 代理对 SSE 响应补 `cache-control: no-cache` + `x-accel-buffering: no`（`vite.config.ts:13-20`）——**否则会被代理缓冲，进度不实时** |
| 消息粒度 | 逐条笔记推送（含进度与结果） |
| 落库控制 | 请求参数 `save_to_library`（请求级，不持久化） |

---

## 5. 二开新增 API 设计

| # | 方法 + 路径 | 说明 | 对应需求 |
|---|---|---|---|
| 1 | `POST /api/shops/import-cookie` | 导入千帆 Cookie，建店铺同步来源 | R1 |
| 2 | `GET /api/shops` | 店铺列表（含 `capabilities` 与验证时间） | R2 |
| 3 | `GET /api/shops/{id}` | 店铺详情 | R2 |
| 4 | `POST /api/shops/{id}/sync` | 手动触发店铺 + 商品同步 | R2 |
| 5 | `POST /api/shops/{id}/probe-capabilities` | 手动触发能力探测 | R5 |
| 6 | `GET /api/shop-products` | 商品列表（筛选：店铺/状态/类目） | R2 |
| 7 | `GET /api/shop-products/{id}` | 商品详情 | R2 |
| 8 | `POST /api/product-notes/generate` | 生成商品笔记草稿（商品专用 Prompt + 护栏） | R3 |
| 9 | `POST /api/product-notes/{id}/approve` | 人工审核通过（闸门） | 合规 |
| 10 | `POST /api/product-notes/{id}/publish` | 发布挂链笔记（补 `goods_info`） | R4 |
| 11 | `GET /api/product-notes` | 任务列表（状态追踪） | R4 |
| 12 | `GET /api/system-settings` | 读取运行时开关 | `10_系统管理功能设计` |
| 13 | `PUT /api/system-settings` | 修改运行时开关（落库） | `10` |

**鉴权**：全部要求 `Depends(get_current_user)` 且按 `user_id` 隔离。

### 5.1 我的笔记与互动数据（2026-09-18 新增，已实现）

| # | 方法 + 路径 | 说明 | 需求 |
|---|---|---|---|
| 1 | `POST /api/xhs/pc/users/me` | 读当前账号自身身份（`/v2/user/me`） | R1 |
| 2 | `POST /api/xhs/pc/users/notes` | 我发布的笔记（单页） | R1 |
| 3 | `POST /api/xhs/pc/users/liked` | 我赞过的笔记 | R2 |
| 4 | `POST /api/xhs/pc/users/collected` | 我收藏的笔记 | R2 |
| 5 | `POST /api/xhs/pc/users/interactions` | 互动消息，`kind` ∈ `like_collect`/`mention`/`new_connection` | R3 |

**契约要点**（完整设计见 `12_功能设计_我的笔记与互动数据.md`）：

| 约束 | 说明 |
|---|---|
| 纯只读 | 五个端点均为 `POST` 但语义只读，**不落库、不写文件**（除前端按需下载） |
| 单页硬约束 | 只调 SDK 单页方法，**禁止** `get_user_all_*`/`get_all_*` 全量循环（防触发风控 / 超时） |
| 令牌三级降级 | L1 粘贴主页 URL → L2 自动 `/v2/user/me` → L3 结构化 400（`code: XSEC_TOKEN_REQUIRED`） |
| 三形态列表探测 | `user_posted`→`notes`、`feed`→`items`、`you/*`→`message_list`，探测而非单一假设 |
| 商品不造假 | 解析不到商品返回 `null`；**商品真实来源见 §5.2（千帆）**，`price` 读不到就留空 |
| 归属校验 | 账号必须属于当前用户；`pc.py:660 _account_cookies` **只判归属**，能力/链路另行判定（§12 设计文档 §10） |

**关键实现约束**：

| 约束 | 说明 |
|---|---|
| 发布前必须校验商品归属 | 商品必须属于该发布账号绑定的店铺，否则服务端必拒 |
| 必须写 `idempotency_key` | `{seller_id}:{item_id}:{account_id}:{素材hash}` |
| 能力未验证时**只读** | `capabilities_verified_at IS NULL` → 拒绝发布端点 |
| 发布前复核商品状态与价格 | 商品可能已下架/改价 → 生成的笔记价格与实际不符 = 违规 |

### 5.2 千帆（ark）只读接线（2026-09-19 新增，已实现）

> 唯一真相源：`12_功能设计_我的笔记与互动数据.md` §24。本节只登记端点事实。

| # | 方法 + 路径 | 说明 | 打平台？ |
|---|---|---|---|
| 1 | `POST /api/xhs/qianfan/bind` | 校验千帆 Cookie 并绑定店铺（Fernet 加密落库） | 是（`/api/edith/seller/info/v2`） |
| 2 | `POST /api/xhs/qianfan/binding` | 绑定状态 + 已同步条数 | **否（纯本地）** |
| 3 | `POST /api/xhs/qianfan/sync-goods` | **批量补齐**商品笔记映射（可选；日常靠浏览时按需补齐） | 是（≤ `max_pages` 次） |
| 4 | `POST /api/xhs/qianfan/unbind` | 解除绑定并清理本地映射 | **否（纯本地）** |
| 5 | `POST /api/xhs/qianfan/items` | **商品列表**（v1.32 起查**本地台账** `shop_products`，零平台请求；搜索条件：关键词 / 状态 / 价格 / **创建时间区间** / 库存 / 销量 / **挂链笔记数**（v1.34，`note_count_op` 默认 `lt` + `note_count`，**关系表口径**，留空不筛）/ 排序（`sort_field`+`order`，服务端排；界面只放开「上架时间」，v1.34）/ 分页；**v1.36 起请求模型 `extra="forbid"`** —— 未知字段 422，不再静默丢弃，见设计 13 §33.10.1） | **否（纯本地）** |
| 5.1 | `POST /api/xhs/qianfan/items/delete` | **批量删除本地台账行**（v1.36，设计 13 §33.10.2）：body `{platform_account_id, item_ids[]}`，**只删本店** `shop_products` 行（`note_goods` 关系行不动、平台商品不动）；空 → 400 `QIANFAN_ITEM_IDS_REQUIRED`、超 `MAX_BATCH_DELETE`（200）→ 400 `QIANFAN_ITEM_IDS_TOO_MANY`（携 `limit`）、未绑店 → 400 `QIANFAN_BINDING_REQUIRED`、越权 → 404；回 `{deleted(=rowcount), requested, item_ids}` | **否（纯本地）** |
| 6 | `POST /api/xhs/qianfan/item-notes` | **商品 → 挂链笔记反查**（本地 `note_goods` join `notes`，带覆盖率；v1.33 起关系表源） | **否（纯本地）** |
| 7 | `POST /api/xhs/qianfan/health-check` | **立即巡检**千帆登录态（只读探活，见设计 12 §24.3.1） | 是（1 次 `seller/info/v2`） |
| 8 | `POST /api/xhs/qianfan/shops` | **店铺列表 + 已绑账号**（N:M 读侧，T-32；设计 12 §24.3.3）。`account_count` / `accounts[]` **只数当前用户（租户）名下的、账号未软删的绑定** —— 跨租户与软删行一律不计（v1.12 · DEF-054/055） | **否（纯本地）** |
| 9 | `POST /api/xhs/product-notes/generate` | **商品笔记生成（干运行）**：取商品 → 商品专用 Prompt → 护栏 + 格式校验；**不落库不发布**（设计 07 §12） | 是（1 次商品查询 + 1 次模型调用） |

> **`/api/xhs/pc/users/notes` 的千帆请求数（v1.12）**：商品本体**按需补齐** ——
> 本页缺映射时用 `note_ids` 一次拿回（**≤1 次**，见设计 12 §24.3.2），命中即落库，
> 第二次浏览该页 **0 次**；若本页还有带商品但缺价的笔记，再 **≤1 次**批量查价（§24.4.1）。
> 所以「浏览我的笔记」的千帆请求数是 **0–2 次**（v1.9 时代的「恒为 0」已被取代）。

**契约要点**：

| 约束 | 说明 |
|---|---|
| 登录态独立 | 千帆是 B 端统一登录；账号 Cookie 会被判 `code=-13000 缺失登录凭证`，必须单独导入 |
| 无需签名 | 实测不带 `x-s`/`x-t`/`x-s-common` 也返回 200 ⇒ `apis/xhs_qianfan_apis.py` 只有 cookie + 普通头（满足 C4 不新增签名维护面） |
| 请求有界 | `max_pages` ≤ 20、`page_size` ≤ 100 ⇒ 单次同步最多 2000 条 |
| 只读 | 只有两个 GET；无任何写接口（C6） |
| 敏感字段白名单 | 绑定响应只回店铺名 / 店铺 ID / 同步计数，`user_email`、`config_values` 等一律丢弃 |
| 归属校验 | 账号必须属于当前用户，否则 404 |

**与「我的笔记」的接口耦合点**：`/api/xhs/pc/users/notes` 在返回体里附带
`items[].goods`（千帆映射）与 `items[].shop`（千帆指标）以及页级 `goods_source`；
`/api/xhs/pc/users/capabilities` 的 `goods` 由「占位说明」改为 `source/available/shop_*/synced_*`。

### 5.3 账号矩阵同步（M1.9，2026-09-19 新增，已实现）

> 唯一真相源：`13_功能设计_账号矩阵同步.md`（§4 端点契约 / §17 增量同步）。本节只登记端点事实。

| # | 方法 + 路径 | 说明 | 打平台？ |
|---|---|---|---|
| 1 | `POST /api/accounts/{account_id}/notes/sync` | **触发笔记同步**（异步，立即返回 task_id）。body 全可选：`max_pages` / `page_size` / `profile_url` / `xsec_token`（PC 账号必需）/ `mode`（`full` 默认 / `incremental`） | 是（≤ `max_pages` 次，默认 3 页） |
| 2 | `GET /api/accounts/{account_id}/notes/sync/status` | 同步任务状态（轮询用，5 秒间隔） | **否（纯本地）** |
| 3 | `POST /api/xhs/qianfan/sync-shop-goods` | **触发店铺商品同步**（Creator 账号 → `shop_products`，异步）。body：`platform_account_id`（必填）/ `max_pages` / `page_size` / `mode` | 是（≤ `max_pages` 次） |
| 4 | `GET /api/xhs/qianfan/sync-shop-goods/status` | 同步任务状态（轮询用） | **否（纯本地）** |

**契约要点**：

| 约束 | 说明 |
|---|---|
| 异步 + 防重两道 | 接口立即返回 task_id；手动触发查未完成任务 → 返回已有 task_id 不重发；调度器每轮同样查（§7.1） |
| 有界（C5） | `max_pages` 默认 3 / 上限 20，`page_size` 默认 50 / 上限 100 ⇒ 单次最多 2000 条；翻够 `total` / 末页信号 / 游标到顶 提前结束 |
| 风控 | 复用 `CrawlRateLimiter`（按账号/分钟）+ 页间 sleep；超限停本次等下一轮 |
| **`mode`（D11）** | `full` = 全量重扫（**手动触发默认**）；`incremental` = 只取比上次游标新的（**调度器默认**）。详见 §17.8 |
| 只读 | 三个平台操作全是读取（creator 列表 GET、千帆 `goods-note/list` GET、千帆 `search_item_v2` 只读查询 POST）；零写接口 |
| 末页信号不对称 | **笔记**只信 `end_tag`（满页 20 行 + `end_tag=true` 也是末页）；**商品**只信「本页 0 行」（无 `end_tag`，`has_more` 平台不返回 —— DEF-044）。见 §17.7 |

---

## 6. API 层的已知缺口

| # | 缺口 | 影响 | 处理 |
|---|---|---|---|
| A-1 | **3 个** mock 端点（原 4 个） | 前端若接入会拿到假数据 | 见 `04_测试文档/05`；~~其中 1 个 stub（`PATCH /accounts/{id}`）~~ 已于 2026-09-18 下线（T-105）；~~`/users/notes` mock~~ 已于 2026-09-18 去 mock |
| A-2 | 18 个孤儿端点（前端未调用；原 20，已消除 2） | 部分含真实能力被闲置 | 逐条评估接线或下线 |
| A-3 | 无统一响应 envelope | 前端解析逻辑各异 | 低优先级 |
| A-4 | 无统一分页参数规范 | 前端需逐接口适配 | 低优先级 |
| A-5 | `api_logs` 表无写入点 | 无 API 审计 | 建议补中间件 |
| A-6 | ~~发布不校验账号 `status`~~ | — | ✅ **已修复（2026-09-18，T-109，DEF-005）**；回归见 TC-M1-09 |
| A-7 | 无请求体大小限制 | 大文件上传依赖前端约束 | 建议加中间件 |
