# 功能设计 · LLM 生成链路（M1 里程碑 · 详细设计）

> - **体系位置**：`docs/02_设计文档/`（二开方向 1 的独立详细设计，文档先行五阶段「阶段二」产出）
> - **上游**：`docs/01_需求文档/05_需求规格_第二阶段二开.md`（方向 1）、`docs/02_设计文档/16_功能设计_第二阶段二开.md` §2
> - **状态**：v1.3 · 2026-09-29（**方案待评审，未开始写代码**）
> - **覆盖**：LLM 网关（**集成 New API 市场网关**）+ 服务商/账号（New API 渠道+令牌）+ 文案生成 + 媒体生成 + 提示词模板 + 本地素材库 + 用量监控（New API 看板 + 项目侧快照）+ 闭环；
>   ~27 端点、~9 张新表 → 全部在本文档定稿
> - **写作约定**：只写「现状结论」，不堆演进链；被推翻结论就地标注「被 XX 取代」；每条结论可追到实现位置
> - **v1.1 修订**（2026-09-29 用户新需求「新增 LLM 网关 + 统计大模型数据 + 支持多服务商 + 服务商下多账号」）：
>   ① 新增**服务商层** `llm_providers`（表）+ `model_configs` 升级为**账号层**（`provider_id` 归属）；
>   ② 新增**用量统计** `llm_usage_records` + `llm_usage_stats` + 统计端点；
>   ⚠️ **v1.0 的「不建 llm_providers」结论被 v1.1 取代**。
> - **v1.2 修订**（2026-09-29 用户拍板：「直接复用 `D:\work\WB2API`，只需做数据落库」）：
>   网关不自研协议层，改由本地 WB2API 转发腾讯后端。
> - **v1.3 修订**（2026-09-29 用户拍板：「**取消 WB2API，调研市场成熟 API 通用网关，要能整合进项目且有数据监控**」，
>   选型结论 = **New API**（QuantumNous/new-api，45K★，Go 单二进制，Windows 免 Docker，SQLite 默认，自带数据看板 +
>   `/api/log/*` 统计 API，原生「渠道=服务商 · 令牌=账号」）：
>   ③ **取消自建服务商层** `llm_providers`（v1.1 新增）与 **`model_configs` 账号层改造** —— 服务商/账号由 **New API 渠道+令牌**承载，项目侧不再自建；
>   ④ **取消自建用量表** `llm_usage_records`/`llm_usage_stats`（v1.1 新增）—— 用量监控由 **New API 数据看板 + `/api/log/stat`** 承载，项目侧仅**快照缓存**（`llm_gateway_snapshots`）；
>   ⑤ 新增 **`llm_gateway_configs`**（存 New API 连接配置：base_url / 管理令牌 / 网关令牌 / 默认模型 / 健康状态）+
>   **`llm_gateway_snapshots`**（每日统计快照）；
>   ⑥ 端点数 ~30 → **~27**、新表数 10 → **9**（`llm_providers`/`llm_usage_records`/`llm_usage_stats` 取消，
>   增 `llm_gateway_configs`/`llm_gateway_snapshots`）；
>   ⚠️ **v1.1/v1.2 的服务商层 + 用量表 + WB2API 方案结论全部被 v1.3 取代**（原句保留见 §1/§3/§4）。

---

## 0. 一句话

把一期「千帆 AI 智能生成」扩成**第二套通用 LLM 生成链路**：**网关层集成市场成熟的 New API 网关**
（本机自托管，OpenAI/Claude/Gemini 统一格式，渠道=服务商、令牌=账号，自带数据看板与统计 API），
项目侧用「公共提示词模块 + 按任务类型模板」两层结构组装 Prompt，向 New API 网关发起生成，产出
**标题/正文/话题/封面/视频**，媒体来源支持 **AI 生成 / 本地素材库 / 复用商品原始图 / 参考原始图生成新图** 四种，
生成结果落库为 `local`+`pending` 本地笔记，用户预览编辑后显式发布（走一期千帆 `note/publish` 链路）。

---

## 1. 范围与边界

### 1.1 本期做（M1）

| 模块 | 做 |
|---|---|
| LLM 网关 | **集成 New API 网关**（`QuantumNous/new-api`，本机新起进程，默认 :3000，SQLite 落盘；OpenAI/Claude/Gemini 统一格式），不自研协议层 |
| 服务商/账号 | **由 New API 的渠道 + 令牌承载**（渠道=服务商，一个渠道下多令牌/多渠道=多账号）；项目侧只存**网关连接配置**（`llm_gateway_configs`：base_url / 管理令牌 / 网关令牌 / 默认模型 / 健康状态） |
| 用量监控 | **New API 数据看板 + `/api/log/stat` 统计 API 承载**；项目侧每日**快照缓存**（`llm_gateway_snapshots`）供 XHS 前端展示 |
| 文案生成 | 标题（多备选）＋ 正文（600–900 字）＋ 话题标签；经一期护栏 + 话题标记 |
| 媒体生成 | 文生图 / 图生图（参考图）/ 图生视频 / 图文拼图，落本地 + AIGC 溯源 |
| 提示词模板 | 两层结构：`prompt_modules`（公共片段）+ `prompt_templates`（任务模板引用模块） |
| 本地素材库 | 多文件夹扫描/索引/缩略图/检索；只读不改用户文件 |
| 闭环 | 商品 → 配置 → 入队 → worker 生成 → 落库可编辑 → 显式发布（复用一期） |

### 1.2 本期不做（明确排除）

- **不做热点生成**（方向 2，M3）、**不做仪表盘**（方向 3，M2）、**不做商品编辑扩展**（方向 4，M4）、
  不做工作流（方向 5，M5）、订单（方向 6，M6/M7）。
- **不自研 LLM 协议网关**（v1.3 取代 v1.2 的 WB2API 方案与 v1.1 的自研方案）—— 网关 **集成 New API**
  （默认 `127.0.0.1:3000`），多服务商/多账号（渠道/令牌）、OpenAI/Claude/Gemini 格式转换、日志统计全部由 New API 承担。
- **不建服务商层表** `llm_providers`、**不改 `model_configs` 为账号层**（v1.3 取代 v1.1）—— 服务商/账号在 New API 里管理。
- **不建用量明细/聚合表** `llm_usage_records`/`llm_usage_stats`（v1.3 取代 v1.1）—— 用量监控走 New API 看板 + 项目侧快照。
- **不做供应商的「能力探测 / 自动选型」**（本期 New API 渠道手工配模型，测试连通走 New API `/v1/models`）。
- **不做对话式改写**（一期 `ai_drafts` 已有 rewrite/polish，本期聚焦「商品 → 笔记」生成）。
- **不做媒体落 `media_assets` 新表** —— 一期 `notes.note_assets` 已是笔记媒体唯一落库；素材库落 `material_files`。

> ⚠️ **本设计对上游 16 文档的三处修订**（16 文档是需求级「链路/方案/原型」，本文档是字段级定稿）：
> ① ~~`llm_providers` / `llm_models` 两张表**取消** → 复用一期 `model_configs`~~ → 历经 **v1.1**（恢复 `llm_providers` 服务商层）→ **v1.3 定稿**：
>   **服务商/账号不再自建**，由 **New API 渠道+令牌**承载；项目侧仅 `llm_gateway_configs`（网关连接配置）—— 见 §3.8；
> ② `media_assets` 表**取消** → 笔记媒体复用 `notes.note_assets`、素材库用 `material_files`（同一语义只留一份，DEF-044 纪律）；
> ③ 新增表经 v1.1（9 张）→ **v1.3 净 9 张**：`llm_gateway_configs` / `llm_gateway_snapshots` / `prompt_modules` / `prompt_templates` /
>   `material_folders` / `material_files` / `generation_records` / `note_generation_links` / `aigc_media_assets`
>   （`llm_providers`/`llm_usage_records`/`llm_usage_stats` 取消，`media_assets`/`llm_models` 不建）。
> 上述在 16 文档 §10/§11 已就地标注「被 17_功能设计_LLM生成链路.md 取代」。

---

## 2. 完整链路（字段级基线，先定稿再写码）

```
用户选商品(1) ──► 选生成配置(2) ──► 入生成队列(3) ──► worker 线程池(4)
                                                            │
   ┌────────────────────────────────────────────────────────┘
   ▼
(5) 组装 Prompt：商品白名单字段 + 公共模块 + 任务模板 + 素材/参考图引用
        ├─ (5a) New API 网关（POST /v1/chat/completions，OpenAI 兼容）→ 标题/正文/话题（JSON，经护栏 + 话题标记解析）
        │       网关令牌 = llm_gateway_configs.gateway_token；模型 = configs.default_model / 生成配置所选
        ├─ (5b) 媒体：AI 文生图 / 参考图生成 / 图生视频（New API 转发，按渠道能力）  OR  本地素材库  OR  复用商品原始图
        ▼
(6) 话题落库 topics + note_topics（复用 note_topic_service，只增不删）
(7) 保存本地笔记（note_type=local + publish_status=local+pending）+ note_assets + note_goods
    + generation_records（一次生成一条审计）+ aigc_media_assets（平台溯源）
(8) 用户预览/编辑 ──► 显式「发布」──► 一期发布链路（预审核 + SKU 挂链 + aigcTaskId）
```

- 生成走队列：`POST /xhs/product-notes/generate-llm` 入队即返回，前端 5s 轮询 `GET /xhs/product-notes/generate-llm-queue`；
  快照键名 `generated`（沿用一期改名纪律，透传底座 `synced` = 进度卡恒 0）。
- **不自动发布**：发布必须用户显式动作（一期 §31.5 纪律延续）。
- 用量统计：New API 数据看板承载实时监控；项目侧**按日快照** `llm_gateway_snapshots`（定时任务拉 `/api/log/stat`）供 XHS 前端展示。

---

## 3. 字段级数据模型（~9 张新表 + 复用表）

> 落库标注：✅ 明文落库 ｜ 🔒 加密落库 ｜ ⚠️ 必要脱敏 ｜ ❌ 不落库（实时拉取或临时）
> 命名/类型/约束对齐一期 (`models/*.py` 风格)；时间一律 `shanghai_now()`。

### 3.1 `prompt_modules`（公共提示词模块，新增 ✅）

> 语义：可复用的 prompt 片段，按类别组织，可组合拼接到任意生成（16 文档 §2.4-A 的「公共模块」层）。

| 列 | 类型 | 约束 | 落库 | 说明 |
|---|---|---|---|---|
| id | Integer | PK | ✅ | |
| user_id | Integer | FK users.id, **index** | ✅ | 归属（DEF-066 纪律） |
| name | String(128) | 非空 | ✅ | 模块名（唯一展示名，按 user 内唯一） |
| category | String(64) | 非空, index | ✅ | 类别（如 `style`/`tone`/`audience`） |
| content | Text | 非空 | ✅ | Prompt 片段正文 |
| sort_order | Integer | 默认 0 | ✅ | 拼接顺序（同组合内先后） |
| enabled | Boolean | 默认 True | ✅ | 停用即不参与渲染 |
| created_at / updated_at | DateTime | default/onupdate shanghai_now | ✅ | |

- **唯一约束**：`UniqueConstraint("user_id", "name")`（user 内模块名唯一，迁移 `ab1c2d3e4f5g` 由
  `(user_id, name)` 唯一索引锁定，不造第二份真相源）。

### 3.2 `prompt_templates`（提示词模板，新增 ✅）

> 语义：按任务类型的完整 prompt 模板，引用多个公共模块；另一期的「单独模板」层。

| 列 | 类型 | 约束 | 落库 | 说明 |
|---|---|---|---|---|
| id | Integer | PK | ✅ | |
| user_id | Integer | FK users.id, index | ✅ | 归属 |
| name | String(128) | 非空 | ✅ | 模板名 |
| task_type | String(32) | 非空, index | ✅ | `text`/`image`/`video`/`cover`（白名单见 §4.4） |
| prompt_skeleton | Text | 非空 | ✅ | 模板骨架（含 `{商品字段}` / `{模块}` 插槽） |
| module_ids | JSON | 默认 `[]` | ✅ | 引用的 `prompt_modules.id` 列表（顺序即拼接序） |
| is_default | Boolean | 默认 False | ✅ | 按 (user_id, task_type) 唯一默认（写时清旧） |
| version | Integer | 默认 1 | ✅ | 每次改骨架 +1（历史可追溯） |
| created_at / updated_at | DateTime | | ✅ | |

- 渲染：`渲染最终 Prompt = 基础指令 + Σ(引用的公共模块按 sort_order) + 任务模板骨架 + 商品白名单字段/素材注入`（§5.3）。

### 3.3 `material_folders`（素材库文件夹，新增 ✅）

> 语义：用户配置的本地素材文件夹（**绝对路径**），扫描只读、不改用户文件。

| 列 | 类型 | 约束 | 落库 | 说明 |
|---|---|---|---|---|
| id | Integer | PK | ✅ | |
| user_id | Integer | FK users.id, index | ✅ | 归属 |
| name | String(128) | 非空 | ✅ | 文件夹别名（展示名） |
| path | Text | 非空 | ✅ | **绝对路径**（只存路径字符串，不读文件内容落库） |
| enabled | Boolean | 默认 True | ✅ | 停用 = 不扫描不展示 |
| last_scan_at | DateTime | nullable | ✅ | 最近一次成功扫描时间；NULL = 尚未扫描 |
| file_count | Integer | 默认 0 | ✅ | 最近一次扫描的文件计数（面板显示用） |
| created_at / updated_at | DateTime | | ✅ | |

- **唯一约束**：`UniqueConstraint("user_id", "path")`（同 user 的路径唯一，防重复扫描）。
- **安全边界**：`path` 必须存在且是目录才可启用；启用时校验可读权限；**禁扫描系统目录/根分区**白名单拦截
  （实现 `is_safe_material_path()`，见 §6.3）。

### 3.4 `material_files`（素材库文件索引，新增 ✅）

| 列 | 类型 | 约束 | 落库 | 说明 |
|---|---|---|---|---|
| id | Integer | PK | ✅ | |
| user_id | Integer | FK users.id, index | ✅ | 归属 |
| folder_id | Integer | FK material_folders.id, index | ✅ | 所属文件夹 |
| filename | String(255) | 非空 | ✅ | 文件名（含扩展名） |
| file_type | String(16) | 非空 | ✅ | `image` / `video` |
| rel_path | String(512) | 非空 | ✅ | 相对文件夹路径（防同名、面板展示用） |
| size_bytes | Integer | | ✅ | 文件大小 |
| mtime | DateTime | index | ✅ | 文件修改时间（增量扫描判据） |
| width / height | Integer | nullable | ✅ | 图片尺寸（Pillow 读；视频可为空） |
| thumb_url | Text | 默认 "" | ✅ | 缩略图相对 URL（`/api/files/materials/thumbs/xxx`） |
| created_at | DateTime | | ✅ | |

- **唯一约束**：`UniqueConstraint("folder_id", "rel_path")`（同一文件夹内路径唯一）。
- **不落库**：文件二进制本体（只在用户磁盘原位置；缩略图副本存 `storage/materials/thumbs/`）。

### 3.5 `generation_records`（LLM 生成记录，新增 ✅）

> 语义：**一次 LLM 生成 = 一条审计记录**（谁在何时用哪个供应商/模板生成什么），支持失败重试与溯源。
> 为什么建：一期生成失败原因只在队列快照给一次（§31.4「原因只在快照给一次」），LLM 生成依赖外部供应商，
> 失败/限流/超时频次高，必须有持久记录支撑「重试/排查/统计」。

| 列 | 类型 | 约束 | 落库 | 说明 |
|---|---|---|---|---|
| id | Integer | PK | ✅ | |
| user_id | Integer | FK users.id, index | ✅ | 归属 |
| task_id | Integer | FK tasks.id, index | ✅ | 对应队列项（一个生成任务一条） |
| shop_product_id | Integer | FK shop_products.id, index | ✅ | 源商品（本地台账行） |
| template_id | Integer | FK prompt_templates.id, nullable | ✅ | 所用模板（null = 未用模板） |
| gateway_config_id | Integer | FK llm_gateway_configs.id, index | ✅ | 所用 New API 网关配置 |
| model_name | String(128) | 默认 "" | ✅ | 实际使用的模型名（快照，改配置不丢审计） |
| media_source | String(32) | 非空 | ✅ | `ai_generate`/`material`/`product_image`/`reference_image` |
| status | String(32) | 非空, index | ✅ | `queued`/`running`/`success`/`failed` |
| title / body | Text | | ✅ | 生成的标题 / 正文（护栏通过后落） |
| topics_json | Text | 默认 "[]" | ✅ | 生成的话题名列表（归一化后） |
| error_code / error_message | String/Text | nullable | ✅ | 失败原因（**供应商原文不落**，摘要记） |
| ai_generated_at | DateTime | nullable | ✅ | 供应商响应时间 |
| created_at / updated_at | DateTime | | ✅ | |

- **落库标注**：标题/正文 ✅（生成产物，用户可编辑后发布）；**供应商密钥 ❌ 不落**；
  **完整 Prompt ❌ 不落**（体积大且含商品细节；需要时按 `template_id`+`module_ids` 重建，跟踪在 §7.3）。
- 状态机：`queued → running → success | failed`；失败可重试（新队列项，幂等 §5.2）。
- **同名失败原因只在快照 `finished` 给一次** —— 本表落 `error_message` 摘要，快照展示从本表取（不再只靠轮询窗口）。

### 3.6 `aigc_media_assets`（AIGC 媒体资产溯源，新增 ✅）

> 语义：AI 生成的图片/视频的**溯源登记**（平台合规要求 `aigcTaskId`，一期发布链路 SKU 挂链 + AI 溯源已在用）。

| 列 | 类型 | 约束 | 落库 | 说明 |
|---|---|---|---|---|
| id | Integer | PK | ✅ | |
| user_id | Integer | FK users.id, index | ✅ | 归属 |
| generation_record_id | Integer | FK generation_records.id, index | ✅ | 关联生成记录 |
| asset_type | String(16) | 非空 | ✅ | `image` / `video` |
| local_path | Text | 非空 | ✅ | 本地落盘路径（`storage/media/` 或素材库副本） |
| url | Text | nullable | ✅ | 供应商回传 URL（若有） |
| aigc_task_id | Text | 默认 "" | ✅ | 平台 AIGC 溯源号（发布时写入 `feature.aigcTaskId`） |
| prompt | Text | 默认 "" | ✅ | 本次生成所用的图片/视频 Prompt（**溯源用，必留**） |
| model_name | String(128) | 默认 "" | ✅ | 用哪个模型生成 |
| created_at | DateTime | 默认 shanghai_now | ✅ | |

- **为什么建独立表而不塞 `note_assets`**：`note_assets` 是「笔记媒体清单」（发布要读它），
  溯源信息（prompt/model/aigcTaskId）不该污染笔记媒体行；发布时按需 join `aigc_media_assets` 取 `aigc_task_id`。
  ⚠️ 不含**平台发布**相关字段（发布状态在 `publish_jobs`），本表只登记「AI 生成」事实。

### 3.7 `note_generation_links`（生成笔记 ↔ 商品关系，新增 ✅）

> 语义：**LLM 生成链路**的「笔记 ↔ 商品」多对多。一期 `note_goods` 是**平台 ID 级**（`note_id`/`item_id`），
> 而 LLM 生成落库时笔记是本地 `local_<uuid>`（无平台 ID）、商品可能还没同步进台账 —— 需要一张**本地主键级**关系表。

| 列 | 类型 | 约束 | 落库 | 说明 |
|---|---|---|---|---|
| id | Integer | PK | ✅ | |
| user_id | Integer | FK users.id, index | ✅ | 归属 |
| note_pk | Integer | FK notes.id, index | ✅ | 本地笔记主键 |
| shop_product_id | Integer | FK shop_products.id, index | ✅ | 本地台账商品主键 |
| created_at | DateTime | 默认 shanghai_now | ✅ | |

- **唯一约束**：`UniqueConstraint("note_pk", "shop_product_id")`。
- **与一期 `note_goods` 的关系**：**不是两份实现** —— 一期关系表是「**平台证据缓存**」（千帆说挂什么就落什么，平台 ID），
  本表是「**本地生成编排**」（从台账挑商品做生成，本地主键）。发布成功后，两者**都保留**：
  `note_goods` 由发布链路按平台返回的 sku 落（一期行为不变），本表是生成链路的审计维度。
  ⚠️ 前端展示挂链商品**仍读 `note_goods`**（契约不变）；本表只服务「生成链路追查」。

### 3.8 `llm_gateway_configs`（New API 网关连接配置，v1.3 新增 ✅）

> 语义：项目侧对 **New API 网关**的单行连接配置（本机自托管网关的连接 + 鉴权 + 默认模型 + 健康状态）。
> ⚠️ **取代 v1.1 的 `llm_providers` 服务商层与 `model_configs` 账号层改造**（v1.3 定稿）：服务商=New API 渠道、
> 账号=New API 令牌，项目侧不复制这两层，只存「怎么连网关」。

| 列 | 类型 | 约束 | 落库 | 说明 |
|---|---|---|---|---|
| id | Integer | PK | ✅ | |
| user_id | Integer | FK users.id, index | ✅ | 归属 |
| name | String(128) | 非空 | ✅ | 网关别名（如「本机 New API」） |
| base_url | String(512) | 非空 | ✅ | New API 地址（默认 `http://127.0.0.1:3000`） |
| admin_token | Text | 默认 "" | 🔒 | New API **管理令牌**（拉 `/api/log/stat` 等管理 API 用；`encrypt_text` 加密） |
| gateway_token | Text | 默认 "" | 🔒 | New API **网关令牌**（业务调用 `POST /v1/chat/completions` 用；加密） |
| default_model | String(128) | 默认 "" | ✅ | 生成默认模型名（New API 模型目录中的名字） |
| enabled | Boolean | 默认 True | ✅ | 停用 = 业务调用不可用 |
| healthy | Boolean | 默认 False | ✅ | 最近一次健康探测结果 |
| last_health_at | DateTime | nullable | ✅ | 最近健康探测时间 |
| created_at / updated_at | DateTime | default / onupdate | ✅ | |

- **唯一约束**：`UniqueConstraint("user_id", "name")`（用户内网关配置名唯一）。
- **`model_configs` 一期表不动**（继续服务一期 `ai_drafts` 的改写/润色）；**不**升级为账号层、不加 `provider_id`。

### 3.9 `llm_gateway_snapshots`（New API 用量快照，v1.3 新增 ✅）

> 语义：**New API 数据看板 + `/api/log/stat` 统计 API** 承载实时用量；项目侧**按日快照**缓存到本地，
> 供 XHS 前端「大模型用量」页展示（不每次打网关管理 API）。
> ⚠️ **取代 v1.1 的 `llm_usage_records`（明细）+ `llm_usage_stats`（聚合）两表**（v1.3 定稿）：
> 明细不去项目侧落，New API 的 Logs 表本地已有（数据监控的核心在 New API 侧）。

| 列 | 类型 | 约束 | 落库 | 说明 |
|---|---|---|---|---|
| id | Integer | PK | ✅ | |
| user_id | Integer | FK users.id, index | ✅ | 归属 |
| gateway_id | Integer | FK llm_gateway_configs.id, index | ✅ | 对应网关配置 |
| stat_date | Date | 非空 | ✅ | 统计日（shanghai 时区） |
| call_count / success_count / fail_count | Integer | 默认 0 | ✅ | 当日计数（自 `/api/log/stat` 聚合） |
| total_quota | Integer | 默认 0 | ✅ | 当日配额消耗（New API quota 单位） |
| total_tokens | Integer | 默认 0 | ✅ | 当日总 token（自 Logs 聚合） |
| avg_latency_ms | Integer | nullable | ✅ | 当日平均响应耗时 |
| source | String(16) | 默认 "newapi" | ✅ | 快照来源（预留多网关） |
| raw_json | Text | 默认 "{}" | ✅ | 网关统计 API 原文快照（证据链） |
| created_at / updated_at | DateTime | default / onupdate | ✅ | |

- **唯一约束**：`UniqueConstraint("user_id", "gateway_id", "stat_date")`（每网关每日一行，upsert 幂等）。
- 写入方：**定时任务**（复用二期 `schedule_triggers` 机制）每天 0:10 拉一次 New API 统计存快照；前端读本表。
- 明细查询（下钻单次调用）：前端跳到 **New API 自带的 /console/log**（项目内嵌 iframe 或外链），不在 XHS 重做单据页。

### 3.10 复用/扩展的一期表

| 表 | 用途 | 变更 |
|---|---|---|
| `model_configs` | 一期 AI 改写/润色的模型配置 | **不动**（v1.3 取消 v1.1 的账号层改造；`model_type` 加 video 可顺带支持、非本模块依赖） |
| `notes` | 生成结果落库 | 无结构变更（`note_type=local`、`publish_status=local+pending`、`note_id=local_<uuid>`） |
| `note_assets` | 笔记媒体 | 无结构变更（生成媒体落这里，`url` 用本地下载 URL） |
| `note_goods` | 挂链商品（平台级） | 无结构变更（发布链路复用） |
| `topics` / `note_topics` | 话题 | 无结构变更（生成话题经 `apply_sync_topics` 只增不删） |
| `tasks` / `QUEUE_REGISTRY` | 队列底座 | **结构无变更**；队列注册表加 **`note_generate_llm`** 队列（`TASK_TYPE_GEN_LLM_ITEM`/`TASK_TYPE_GEN_LLM_BATCH`） |
| `system_settings` | 运行时配置 | 无结构变更（如有读点键则补 LIVE_KEYS） |
| `ai_drafts` | 一期文案改写 | 不动（本期无对话改写需求） |

> **新增表合计 9 张**（v1.3 定稿）：
> `llm_gateway_configs` / `llm_gateway_snapshots` / `prompt_modules` / `prompt_templates` /
> `material_folders` / `material_files` / `generation_records` / `note_generation_links` / `aigc_media_assets`；
> `llm_providers` / `llm_usage_records` / `llm_usage_stats`（v1.1 增）与 `media_assets`/`llm_models`（16 文档设想）**均不建**。

---

## 4. LLM 网关设计（v1.3：集成 New API 市场网关）

> ⚠️ **本节取代 v1.2 的「复用本地 WB2API」结论与 v1.1 的「自研 `llm_gateway.py` 三层调用」结论**（2026-09-29 用户拍板）：
> 网关 = **New API（QuantumNous/new-api）**，本机自托管、独立进程，项目侧只做薄连接 + 快照。

### 4.1 New API 是什么（选型依据）

- **成熟市场网关**：45K★，One API 增强分支，Go 单二进制，**Windows 免 Docker**（官方 Release `new-api-v1.x.x.exe`，前端内嵌），
  **SQLite 默认**（数据落 `data/` 单文件），自带**数据看板**（消耗趋势折线 + 按用户/模型/渠道多维统计）与 **`/api/log/*` 统计 API**。
- **「多服务商 + 服务商下多账号」由 New API 原生承载**：**渠道** = 服务商/上游（可加多个 DeepSeek / OpenAI / 通义… 渠道，加权随机 + 失败重试）；
  **令牌** = 对外账号（可为每个 XHS 场景建多个令牌，分组 + 额度 + 模型限制）。
- **统一协议**：OpenAI Compatible / Claude Messages / Google Gemini / Responses / Realtime，自动格式转换 —— 业务侧只用 OpenAI 兼容形态。
- **许可**：AGPLv3（**自托管自用不受影响**；不对外分发/修改后闭源发布）。**合规**：仅使用有合法授权的上游，且绝不对公众开放（本机 127.0.0.1）。

### 4.2 集成拓扑

```
┌─────────────── XHS 项目 ───────────────┐        ┌──────── New API（独立进程）────────┐
│ 前端：生成页/素材/模板/用量展示          │        │  :3000（默认，可配）                  │
│ 后端：prompt 渲染 → 护栏 → worker        │  HTTP   │  /v1/chat/completions（业务调用）     │
│   └ worker 组装 prompt ────────────────┼───────► │  /api/log/stat（管理统计）            │
│   └ 定时任务拉统计 → llm_gateway_snapshots │        │  /console（自带数据看板）             │
└──────────────────────────────────────┘        │  SQLite data/（渠道/令牌/日志全落盘）│
                                                 └──────────────────────────────────────┘
```

- **进程生命周期**：New API 以独立进程随 XHS 桌面端**一并启停**（launcher `run-app.py` 增加一个子进程拉起，或作为独立快捷方式）；
  其数据落 `D:\work\XHS\data\newapi\`（SQLite + logs），纳入既有备份/打包口径。
- **健康探测**：项目侧定时 `GET http://127.0.0.1:3000/api/status`（或 `/v1/models`）写 `llm_gateway_configs.healthy`。

### 4.3 项目侧薄客户端

- `services/llm_gateway_client.py`（v1.2 的薄客户端保留，目标改为 New API）：
  ① 读 `llm_gateway_configs` 的 base_url / gateway_token / default_model；② `POST {base}/v1/chat/completions` OpenAI 兼容形态；
  ③ 解析回包（`choices[0].message.content`）；④ **不自己落 token/耗时明细**（New API 的 Logs 落盘兜底）—— 业务侧只把
  关键信息写进 `generation_records`（本次生成用的模型/网关）。
- **不做**：不做协议适配（New API 已统一）、不做多账号轮转（New API 渠道池管）、不做格式转换。

### 4.4 能力线 / 模型

- New API 渠道里配置的模型 = 可用模型集合（`GET /v1/models` 可查）。XHS 生成侧只需在 `llm_gateway_configs.default_model`
  与生成配置里选模型名；**模型能力（text/image/video）由 New API 渠道模型映射决定**（一个渠道模型名即一种能力）。
- 文案生成走 `text` 模型；封面/配图走 `image` 模型；视频走 `video` 模型 —— 均指 New API 模型目录里的名字。
- 测试连通：`POST /api/gateway/{id}/test` → 打 New API `/v1/models`（校验模型存在）+ 一次极小 chat 请求（可选）。

### 4.5 用量监控（数据监控的核心，New API 承担）

| 层 | 谁做 | 说明 |
|---|---|---|
| 实时看板 | **New API /console** | 消耗趋势折线、按用户/模型/渠道消耗分布、日志表格（含 token/耗时/quota） |
| 统计 API | **New API `/api/log/stat`** | 返回 `{quota, rpm, tpm}`（最近 60s）+ `GET /api/log/` 明细页过滤 |
| 项目侧快照 | **本项目** | 定时任务每天拉 `/api/log/stat` + 当日 Logs 聚合成 `llm_gateway_snapshots`，XHS「大模型用量」页展示（不重做单据页） |
| 下钻 | **本项目 iframe/外链** | 前端「查看详情」跳到 New API /console/log（带网关 base_url） |

> ⚠️ 与 v1.1 的差异：**Token/额度明细不重复落项目侧库**（New API SQLite 已有权威数据），
> 项目只做**快照缓存**供集成展示 —— 避免「同一语义两份真相源」（DEF-044）。

### 4.6 重试 / 超时 / 限流

| 项 | 约定 |
|---|---|
| 超时 | 文本 60s / 媒体（image/video）180s（对齐一期 `ai_service` 180s） |
| 重试 | 客户端对 `ConnectError`/`Timeout` **最多重试 1 次**（间隔 2s）；HTTP 4xx/5xx **不重试**（网关/上游侧问题，记失败） |
| 限流 | 复用一期 `rate_limiter`（`wait_allow()`）**在 worker 内**调用（项目自身对网关的调用频控，防止把人流量压到网关） |
| 密钥 | 项目→网关鉴权用 `llm_gateway_configs.gateway_token`（New API 令牌）加密落库；**不落日志** |

### 4.7 文本生成格式约束（JSON 输出 + 护栏）

`generate_full_note()` 输出**严格 JSON**：

```json
{ "title": "≤20字标题", "body": "600-900字正文", "topics": ["话题1", "话题2"] }
```

- 解析用一期 `_load_json_response` + `parse_generated_payload`（product_note_service）同款健壮解析；
- **护栏**：复用 `product_note_service` 的 `run_guardrails`（商品字段白名单 + 绝对化用语/违禁词/功效宣称 +
  价格区间一致性）；命中即 `失败`，`generation_records.error_message` 记护栏原因摘要，**不落库半条笔记**（§31.4 纪律）；
- **话题**：经 `note_topic_service.parse_topic_tokens` + `strip_topic_marker` 归一化，落 `note_topics`（只增不删，§34 纪律）。

---

## 5. Prompt 渲染（两层模板）

### 5.1 渲染规则（唯一实现 `services/prompt_renderer.py`）

```
final_prompt =
    基础指令（内置常量，如「你是小红书种草内容专家」）
  + Σ(引用的 prompt_modules 按 sort_order 拼接)
  + prompt_templates.prompt_skeleton（含 {商品字段} / {素材引用} 插槽的模板骨架）
  + 商品白名单字段注入（PRODUCT_FIELDS，复用 product_note_service）
  + 素材/参考图引用（选中的本地素材或商品原始图 → data URL / 本地路径）
```

- **模板插槽白名单**：只认 `{title} / {body} / {topics} / {商品字段…} / {images} / {video}` 等一期定义的槽位；
  未知插槽渲染时**原样报错**（防模板注入静默吞内容，DEF-048 纪律）。
- 渲染结果是**纯文本**（text 走 chat completions；image/video 走相应端点 prompt 参数）。

### 5.2 模板预览

- 前端模板设置页提供「预览渲染效果」：选模板 + 选商品 → 调 `POST /xhs/llm-templates/render-preview`
  返回最终 Prompt 文本（不调供应商，纯本地渲染）。

---

## 6. 本地素材库设计

### 6.1 扫描

- 扫描走**线程池**（防阻塞主流程），`os.walk` 遍历文件夹；
- **图片**（jpg/jpeg/png/webp）与**视频**（mp4）两类（对齐一期 `ALLOWED_UPLOAD_EXTENSIONS`）；
- 缩略图：图片用 Pillow 生成（**80 × 80 cover** 存 `storage/materials/thumbs/`，命名 `f{folder_id}-{file_id}.jpg`）；
  视频缩略图用 **opencv 首帧**（若可用）否则空占位（`thumb_url=""`，面板显示视频图标）；
- **增量扫描**：`mtime` 变更才重索引，删除的文件置为不可用（软删标记，不物理删记录）；
- 大文件夹（>1000 文件）分页 + 增量（16 文档 §2.3 已约定）。
- **安全边界**：`is_safe_material_path()` 拦截：路径不存在 / 非目录 / 无读权限 / 命中系统目录黑名单
  （`C:\Windows`、`/System`、`/usr`、`/proc` 等）→ 返回 400 结构化原因；**只读**，绝不写用户文件。

### 6.2 检索

- `GET /xhs/materials/files` 参数：`folder_id`（必填，或全部）/ `keyword`（filename 模糊）/ `file_type`（image|video）/
  `sort`（mtime desc 默认）/ 分页。
- 面板按「文件夹分组」展示缩略图网格，支持搜索 + 类型/时间筛选（16 文档 §2.3 功能点 3）。

### 6.3 引用与落素材库

- 生成配置里「媒体来源 = 本地素材库」→ 勾选 N 张 → 生成时作为配图/封面；落 `notes.note_assets`（URL 用本地下载 URL）；
- 「生成图落点可指定」：生成的新图/视频可①保存到指定素材库（拷到 `material_folders.path`？**否** —— 只读边界！）
  ⚠️ **修订 16 文档 §2.4-B**：生成图「保存到指定素材库」**不写用户文件夹**（只读边界，硬约束 C2），
  统一存 `storage/media/`（一期下载目录）+ 登记 `material_files`（`folder_id=NULL` 的特殊「系统素材」）——
  前端素材面板加「AI 生成」分组展示它们 ⇒ 达到「可复用」而**不改用户文件**。

---

## 7. API 契约（逐端点字段级）

> 前缀一律 `/api`；请求模型 `extra="forbid"`（一期纪律）；分页用一期 `paginated` 约定；
> 错误码用一期 `HTTPException(detail={"code","message"})` 结构化格式。
> ⚠️ 端点前缀沿用一期 `GET /openapi.json` 实测的**三层平台路由** —— LLM 模块挂在
> `/api/xhs/product-notes/` 与新增 `/api/xhs/llm/` 下。

### 7.1 网关配置（New API 集成，v1.3：~5 端点）

> ⚠️ **v1.1 的「服务商层 + 账号层升级」与 v1.2 的「WB2API 连接」均被 v1.3 取代**（原句见 §0 修订说明）：
> 服务商/账号在 **New API 面板**（渠道 + 令牌）管理，项目侧只配 **网关连接**（`llm_gateway_configs`）+ 看板跳转。

**`GET /api/xhs/llm/gateway-configs`**（网关配置列表）
- 返回 `paginated`，字段 `id/name/base_url/default_model/enabled/healthy/last_health_at/gateway_version?`（**令牌字段永不返回**，只给 `has_admin_token`/`has_gateway_token` 布尔）。

**`POST /api/xhs/llm/gateway-configs`**（新增网关配置）
- 请求：`{name, base_url?, admin_token?, gateway_token?, default_model?}`；
  `base_url` 默认 `http://127.0.0.1:3000`；两个 token 用 `encrypt_text` 加密落库；
  `name` 用户内唯一（409 `LLM_GATEWAY_NAME_EXISTS`）；返回 201 + 对象（token 不返）。

**`PATCH /api/xhs/llm/gateway-configs/{id}`**（编辑网关配置）
- 改 token 时**留空 = 保持不变**（防止误清）；改 base_url 后 `healthy` 自动置 False（需重新探测）。

**`DELETE /api/xhs/llm/gateway-configs/{id}`**（删除网关配置）
- 删除时同步清理 `llm_gateway_snapshots` 历史快照（软删？**否** —— 直接级联删，配置没了快照无意义）。

**`POST /api/xhs/llm/gateway-configs/{id}/test`**（健康探测 + 模型列表）
- 顺序：① `GET {base}/api/status`（New API 可达性）→ ② `GET {base}/v1/models`（带 gateway_token，取模型目录）→
  （可选）③ 一次极小 chat 请求验证 default_model；
- 结果写 `healthy`/`last_health_at`；不可达 400 `LLM_GATEWAY_UNREACHABLE`；令牌错 401 `LLM_GATEWAY_AUTH_FAILED`。
- 返回 `{id, healthy, message, models: [name...]}`（前端用模型列表填充生成配置下拉）。

> 端点合计：网关配置 **5**。New API 本身的渠道/令牌/日志由用户在其面板操作，项目侧不做端点承接。

### 7.2 提示词模板（新增，~7 端点）

**`GET /api/xhs/llm/prompt-modules`**（列表）
- 参数：`category?` / `page` / `page_size`；返回 `paginated`（字段 `id/name/category/content/sort_order/enabled`）。

**`POST /api/xhs/llm/prompt-modules`**（新增）
- 请求：`{name, category, content, sort_order?, enabled?}`；`name` 在 user 内唯一（冲突 409 `PROMPT_MODULE_NAME_EXISTS`）；
- 返回 201 + 完整对象。

**`PATCH /api/xhs/llm/prompt-modules/{id}`**（编辑）
- 请求：`{name?, category?, content?, sort_order?, enabled?}`；越权 404。

**`DELETE /api/xhs/llm/prompt-modules/{id}`**（删除）
- 删除被 `prompt_templates.module_ids` 引用 ⇒ 400 `PROMPT_MODULE_IN_USE`（列出引用模板名）。

**`GET /api/xhs/llm/prompt-templates`**（列表）
- 参数：`task_type?` / `page` / `page_size`；返回含 `module_ids` 展开的 `modules`（name/content）。

**`POST /api/xhs/llm/prompt-templates`**（新增）
- 请求：`{name, task_type, prompt_skeleton, module_ids?, is_default?}`；
  `task_type` 白名单 `text|image|video|cover`（未知 400 `LLM_TASK_TYPE_UNSUPPORTED`）；
  `is_default=true` 时清同 (user, task_type) 旧默认（一期 `_clear_default_for_type` 同款）；
  返回 201。

**`PATCH /api/xhs/llm/prompt-templates/{id}`**（编辑）
- 变更骨架/模块引用 ⇒ `version+1`（只升不降）；`is_default=true` 清旧默认。

**`DELETE /api/xhs/llm/prompt-templates/{id}`**（删除）
- 被 `generation_records.template_id` 引用 ⇒ 400 `PROMPT_TEMPLATE_IN_USE`。

**`POST /api/xhs/llm/prompt-templates/render-preview`**（预览，新）
- 请求：`{template_id, shop_product_id}`；返回 `{name, prompt}`（纯本地渲染，不调供应商）。

> 端点合计：提示词 **8**（modules 4 + templates 4）。

### 7.3 素材库（新增，~7 端点）

**`GET /api/xhs/materials/folders`**（文件夹列表）
- 返回 `[{id, name, path, enabled, last_scan_at, file_count}]`。

**`POST /api/xhs/materials/folders`**（新增文件夹）
- 请求：`{name, path}`；`path` 过 `is_safe_material_path()`，不合法 400 `MATERIAL_PATH_UNSAFE`；
  同 user 同 path 409 `MATERIAL_FOLDER_EXISTS`；成功后**异步触发首次扫描**（立即返回 202 + 对象）。

**`PATCH /api/xhs/materials/folders/{id}`**（编辑：改名 / 启用停用）
- `enabled=false` 时**不物理删索引**（保留 `material_files` 行，重启用即恢复）。

**`DELETE /api/xhs/materials/folders/{id}`**（删除）
- 级联删除该文件夹的 `material_files` 索引与缩略图副本（用户源文件**不删**）；被生成记录引用不拦
  （历史记录仍可查 file 名，路径字段冗余保留）。

**`POST /api/xhs/materials/folders/{id}/scan`**（手动刷新）
- 同步段返回 202（入后台线程）+ 轮询 `GET /api/xhs/materials/folders/{id}` 看 `last_scan_at`。

**`GET /api/xhs/materials/files`**（文件列表 + 搜索）
- 参数：`folder_id?` / `keyword` / `file_type` / `sort` / `page` / `page_size`；
  返回 `[{id, folder_id, filename, file_type, rel_path, size_bytes, mtime, width, height, thumb_url}]`。

**`GET /api/xhs/materials/files/{id}/file`**（取文件内容，供面板预览/发布用）
- 返回本地文件字节流（`FileResponse`；不存在 404 `MATERIAL_FILE_MISSING`）。

> 端点合计：素材库 **7**。

### 7.4 生成（新增，~4 端点）

**`POST /api/xhs/product-notes/generate-llm`**（单篇入队）
- 请求：`{platform_account_id, item_id, template_id?, gateway_config_id?, model_name?, media_source, media_options?}`
  - `platform_account_id`：目标账号（门禁：有 creator 会话？**否** —— 生成阶段不需要平台令牌，
    只在校验「该账号属于当前用户」；发布阶段才需要令牌）；
  - `item_id`：商品 `shop_products.item_id`（必须在本账号某店铺台账存在，400 `LLM_PRODUCT_NOT_FOUND`）；
  - `gateway_config_id`：New API 网关配置（默认取用户唯一配置）；`model_name?` 可选覆盖 `default_model`；
  - `media_source`：`ai_generate` / `material` / `product_image` / `reference_image`；
  - `media_options`：素材选中 ids / 参考图 ids / 是否生成封面 / 文生图·图生视频·图文拼图等；
- 门禁：模板存在 / 网关配置存在且 `enabled`（缺 → 400 `LLM_GATEWAY_NOT_CONFIGURED`）/ `healthy` 或允许重试探测
  （`healthy=false` 时不拦但触发一次探测，失败 400 `LLM_GATEWAY_UNREACHABLE`）⇒ 全过 → 入队
  `TASK_TYPE_GEN_LLM_ITEM` → 返回 `{ok, status:"queued", item_id, generation_record_id}`；
- 幂等：同用户未终态队列已有同 (item_id, template_id, gateway_config_id) => `already_queued: true`（不重复入队）。

**`POST /api/xhs/product-notes/batch-generate-llm`**（批量入队）
- 请求：`{platform_account_id, item_ids[], template_id?, gateway_config_id?, model_name?, media_source, media_options?}`；
  逐项过门禁（同单篇），单项失败不阻断；`max_items` 上限 20（一期 `GENERATE_BATCH_MAX_ITEMS` 对齐）；
  返回逐项回执 `[{item_id, ok, error?}]`。

**`GET /api/xhs/product-notes/generate-llm-queue`**（进度快照）
- 与一期 `generate-queue` 同构：`running`/`queued`（带 `item_id`/`title`）+ `generated`/`failed`/`total` +
  `finished`（逐项 `ok` + `reason`，原因从 `generation_records.error_message` 取）；
  ⚠️ 键名 `generated`（底座 `synced` 改名纪律，§4.0）。

**`POST /api/xhs/product-notes/generate-llm/{record_id}/retry`**（失败重试，新）
- 门禁：`generation_records` 属当前用户且 `status=failed`（否则 400 `LLM_RECORD_NOT_RETRYABLE`）；
  重新入队（新队列项）；返回新回执。

> 端点合计：生成 **4**。

### 7.5 用量展示（v1.3：New API 看板 + 项目侧快照，~3 端点）

**`GET /api/xhs/llm/usage/overview`**（总览：今日/近 7 日/近 30 日）
- 从 `llm_gateway_snapshots` 聚合：`{today: {call_count, success_count, fail_count, success_rate, total_tokens, avg_latency_ms},
  week: {…}, month: {…}, gateway_healthy}`（快照没有的日 = 当日无数据，前端按空态处理，不造假）。

**`GET /api/xhs/llm/usage/daily`**（按日序列，供折线图）
- 参数：`gateway_id?` / `start_date` / `end_date`；返回 `[{stat_date, call_count, success_count, fail_count, total_tokens, avg_latency_ms}]`（仅快照有的日期）。

**`POST /api/xhs/llm/usage/refresh`**（手动刷新快照，可选）
- 立即拉一次 New API `/api/log/stat` + 当日 Logs 聚合写 `llm_gateway_snapshots`（upsert）；返回 202 后台跑。

> 端点合计：用量展示 **3**。**明细下钻** = 前端「查看详情」跳 New API `/console/log`（iframe/外链），项目侧不做明细端点。

### 7.6 端点统计与一期对齐

| 组 | 新端点 | 说明 |
|---|---|---|
| 网关配置 | 5 | gateway-configs CRUD + test（v1.3 新增） |
| 提示词模板 | 8 | modules 4 + templates 4 |
| 素材库 | 7 | folders 4 + files 3 |
| 生成 | 4 | 单/批/队列/重试 |
| 用量展示 | 3 | overview/daily/refresh（v1.3 新增） |
| **合计** | **27** | v1.3：网关 5 + 模板 8 + 素材 7 + 生成 4 + 用量 3 = 27（v1.2 的 28 + 服务商 5 - 用量 4 - 账号层 0 调整 ≈ 净变化） |

- 服务商/账号（New API 渠道+令牌）、模型管理（New API `/console`）**不在项目侧建端点**；
- `/model-configs`（一期）**不动**（继续服务 `ai_drafts`）；
- 实施时逐域回数并同步 `AGENTS.md` §5 + `verify_doc_counts.py`（16 文档 §11 已声明）。

---

## 8. 队列与 worker

### 8.1 队列注册

- `QUEUE_REGISTRY` 加一个队列 `note_generate_llm`：
  - `item_task_type = TASK_TYPE_GEN_LLM_ITEM`（`xhs_note_gen_llm_item`）
  - `batch_task_type = TASK_TYPE_GEN_LLM_BATCH`（`xhs_note_gen_llm_batch`）
  - `note_bound = False`（生成前笔记不存在；详情抽屉显商品名，沿用一期 `note_generate` 队列）
- ⚠️ 新队列 = 后端 `QUEUE_REGISTRY` 加一行 + `QUEUE_ITEM_TASK_TYPES`/`QUEUE_BATCH_TASK_TYPES` 常量同步加
  （模块自校验 assert 会拦，§35 纪律）。

### 8.2 worker

- 复用一期 `ensure_generate_worker()` 的线程池模式（跨账号并行 = 独立的 LLM worker 线程）；
- 认领队列项 → 读 payload（item_id/template_id/gateway_config_id/model_name/media_source/media_options）→
  `generation_records` 状态 `queued→running` → 组装 Prompt → **调 New API**（`POST /v1/chat/completions`）→
  护栏校验 → 落库（笔记/媒体/话题/挂链）→ `running→success`；任一步异常 `running→failed` + 记 `error_code/message`；
- 异常分支**先 `db.rollback()` 再记失败**（§35 纪律，防卡 running）。

---

## 9. 落库/不落库总表（M1 范围）

| 数据 | 落库 | 说明 |
|---|---|---|
| 网关 base_url / default_model / name | ✅ 明文 | `llm_gateway_configs` |
| New API 管理令牌 / 网关令牌 | 🔒 加密 | `llm_gateway_configs.admin_token`/`gateway_token`（`encrypt_text`；响应只给 `has_*` 布尔） |
| 提示词模块 / 模板 | ✅ 明文 | `prompt_modules` / `prompt_templates` |
| 素材文件夹路径 | ✅ 明文 | 本地绝对路径；**不读内容落库** |
| 素材文件索引 / 缩略图 | ✅ 索引落库 / 缩略图副本落盘 | 二进制不落库（源文件在用户磁盘原位） |
| 生成标题 / 正文 / 话题 | ✅ 明文（可编辑草稿） | `notes` + `note_topics` |
| 生成完整 Prompt | ❌ 不落 | 体积大；按 `template_id`+`module_ids` 可重建（§3.8） |
| 调用 token/耗时/成败 明细 | ❌ 项目侧不落 | **New API SQLite（Logs）权威**；项目侧只落 `llm_gateway_snapshots` 按日快照 |
| 网关令牌 / 上游渠道密钥 | ❌ 项目侧不落 | 网关令牌加密存配置；上游渠道密钥只在 New API 库（项目侧**不接触**） |
| AIGC 溯源 | ✅ 明文 | `aigc_media_assets.aigc_task_id`（合规要求） |
| 平台发布状态 / 挂链 | ✅ 明文 | 复用一期 `publish_jobs` / `note_goods` |

---

## 10. 安全设计

| 项 | 设计 |
|---|---|
| API Key | 一期 `encrypt_text/decrypt_text` 加解密；日志/响应 `[REDACTED]`；测试连通不吐明文 |
| 素材路径 | `is_safe_material_path()` 白名单黑名单双重拦截（见 §6.1） |
| 越权 | 所有表 `user_id` 归属校验（DEF-066 纪律：资源行必属当前用户） |
| 提示词注入 | `prompt_skeleton` 插槽白名单；未知插槽渲染报错（§5.1） |
| Prompt 泄漏 | 生成产物含商品细节，属用户自有数据；演示/截图不打全量 Prompt |
| 媒体落盘 | 只写 `storage/media/`（应用目录），**不写用户素材文件夹**（只读边界 C2） |
| 网关令牌 | `llm_gateway_configs.admin_token`/`gateway_token` 用 `encrypt_text` 加密落库；响应只给 `has_*` 布尔，**永不回传明文** |
| 网关访问 | New API **仅监听 127.0.0.1**（本机自用）；上游渠道密钥只存 New API 库，项目侧不接触 |

---

## 11. 前端设计

### 11.1 页面与路由

| 页面 | 位置 | 说明 |
|---|---|---|
| 生成配置弹窗 | `pages/platforms/xhs/`（商品列表页入口） | 选模板/网关模型/媒体来源/素材；复用 16 §2.5 原型 |
| 素材库面板 | `components/MaterialPicker` | 弹窗 + 文件夹分组 + 缩略图网格 + 搜索/筛选 + 多选 + 拖拽复用 antd Upload.Dragger |
| 提示词模板设置卡 | 设置页「提示词模板」卡 | 两层管理（公共模块库 + 模板库）+ 预览渲染效果 |
| LLM 网关设置卡 | 设置页「大模型网关」卡 | 配 `llm_gateway_configs`（base_url/令牌/默认模型）+ 健康探测 + 模型列表下拉 |
| 大模型用量页 | `pages/platforms/xhs/`（仪表盘Tab 或独立页） | 从 `llm_gateway_snapshots` 展示 今日/近 7 日/近 30 日 卡片 + 按日折线；「查看详情」跳 New API `/console/log` |

### 11.2 交互要点

- 生成进度卡复用一期「队列记录页」5s 轮询、无 Tip；停刷判据 `total > 0 且待处理 0`（§35 纪律）；
- 素材面板秒开：列表本地先用 `file_count` + 缩略图 URL 占位，缩略图懒加载；
- 生成结果在内容库可见（`local`+`pending`），可预览编辑、可一键发布（复用一期 `POST /notes/{id}/publish`）；
- **用量下钻**：项目侧不做明细页，跳到 New API 自带看板（iframe 或新标签，`gateway_configs.base_url` + `/console/log`）。

---

## 12. 测试要点（阶段二验收前置，补充到 `docs/04_测试文档/07_第二阶段验收测试方案.md`）

| 层 | 用例 |
|---|---|
| 模型 | 9 表建表/约束/唯一索引；归属校验；`material_files` 唯一 `(folder_id, rel_path)`；`llm_gateway_snapshots` 唯一 `(user_id, gateway_id, stat_date)` |
| 迁移 | `upgrade head` 幂等、`downgrade` 可逆；真库前备份（REF §F 纪律） |
| 网关配置 | `/gateway-configs` CRUD + test（健康探测 / 模型列表 / 令牌永不回传） |
| 模板渲染 | 插槽白名单 / 未知插槽报错 / 默认模板唯一（变异：清旧默认不生效 → 红） |
| 素材库 | 安全路径拦截（C:\Windows / 不存在 / 非目录）/ 增量扫描 mtime / 删除不物理删用户文件 |
| 生成 | 门禁（商品不存在/模板不存在/网关未配置或不健康）/ 幂等 already_queued / 护栏命中不落半条 / 失败记 generation_records / retry 幂等 |
| 队列 | `QUEUE_REGISTRY` assert 自检（新增队列两边同加）/ 快照键名 `generated` / 停刷判据 |
| 用量快照 | 定时拉取 upsert 幂等 / overview & daily 聚合（无数据日按空态不造假）/ New API 不可达时不写假数据 |
| 前端 | 网关设置卡 + 模型下拉 / 素材面板搜索筛选 / 提示词模板卡两层管理 / 生成进度卡 5s 轮询无 Tip / 用量页折线 |
| 回归 | 一期 `/model-configs` 与 `/notes` 相关行为零破坏（`test_model_configs.py` / `test_product_note_*.py` 全绿） |

---

## 13. 任务拆分（按本文档落地顺序）

| 阶段 | 任务 | 依赖 | 交付 |
|---|---|---|---|
| T0 | **引入 New API 网关**（下载官方 Release exe → 本机 `:3000` 起进程 → 渠道/令牌配置 → 探活） | 无 | 网关可用，`/v1/chat/completions` 通 |
| T1 | `llm_gateway_configs`/`llm_gateway_snapshots` 表 + 迁移 + 网关配置 CRUD + test 端点 | T0 | 项目侧能连网关、取模型列表 |
| T2 | `prompt_modules`/`prompt_templates` 表 + 迁移 + CRUD 端点 | T1 | 模板两层结构可管理 |
| T3 | `material_folders`/`material_files` 表 + 迁移 + 扫描/索引/检索 + 安全拦截 | T1 | 素材库多文件夹秒开 |
| T4 | New API 薄客户端（`llm_gateway_client` chat 调用）+ 生成 JSON + 护栏 + 话题落库 | T1/T2 | 文案生成（干运行） |
| T5 | `generation_records`/`aigc_media_assets`/`note_generation_links` 表 + 迁移 | T2/T4 | 生成审计可追溯 |
| T6 | 生成编排 + 队列注册 + worker + 单/批/队列/重试端点 | T4/T5 | 商品→可发布笔记闭环 |
| T7 | 媒体生成（文生图/图生视频/拼图，走 New API 渠道）+ 落库 + 溯源 | T6 | 媒体来源四选一 |
| T8 | 定时任务拉 New API 统计 → `llm_gateway_snapshots` + 用量页（overview/daily）+ 跳转看板 | T1 | 数据监控集成展示 |
| T9 | 前端：网关设置卡 + 提示词模板卡 + 素材面板 + 生成配置弹窗 + 进度卡 | T2/T3/T6/T7/T8 | UI 闭环 |
| T10 | 测试补全（§12）+ 双脚本 + 同步 AGENTS.md §5 + README | 全部 | 验收可过 |

> 每个任务是「文档先行」的一个完整闭环：改契约先改本文档对应 §，再写码。

---

## 14. 里程碑验收（对齐 16 文档 §2.6）

- 选商品 → 选模板 → 点「生成并保存」，**≤60s** 产出：标题（多备选）+ 正文（600–900 字）+
  话题标签 + 封面图/视频（AI 生成 / 素材库选取 / 复用商品原始图 / 参考原始图生成新图）；
- 生成结果在内容库可见（`local`+`pending`），可预览编辑、可一键发布（走一期预审核 + 挂链）；
- 素材库：配 2–3 个本地文件夹后，面板秒开缩略图，可搜索/拖拽选用；增删文件后「刷新」即同步；
- 提示词模板：公共模块可组合复用、单独模板按任务类型调用；生成图可指定落素材库 + 直接用于当前笔记；
- **数据监控**：New API 面板可见消耗趋势/模型分布/日志；XHS「大模型用量」页从 `llm_gateway_snapshots` 展示
  今日/近 7 日/近 30 日 调用与成功率的卡片 + 折线，可跳转 New API 明细。

---

## 15. New API 网关部署与初始化（v1.3 工程前置，T0）

### 15.1 获取与运行（Windows 免 Docker）

- 从 `github.com/QuantumNous/new-api/releases` 下载 `new-api-v1.x.x.exe`（前端内嵌，~115MB；国内可用 `ghfast.top` 反代）。
- 同目录建 `.env`：`SESSION_SECRET`/`CRYPTO_SECRET`（`Get-Random` 生成 hex）、`SQL_DSN=`（留空用 SQLite）、
  `SQLITE_PATH=data/new-api.db?_busy_timeout=30000`、`TZ=Asia/Shanghai`、`BATCH_UPDATE_ENABLED=true`。
- 前台跑：`.\new-api.exe --port 3000 --log-dir .\logs` → 访问 `http://127.0.0.1:3000` 引导设管理员。
- **常驻**：`Start-Process` 分离进程 或 挂任务计划程序（登录自启）；数据落 `D:\work\XHS\data\newapi\`。

### 15.2 项目侧初始化（`llm_gateway_configs` 一行）

| 字段 | 值 |
|---|---|
| base_url | `http://127.0.0.1:3000` |
| admin_token | New API「个人设置」里的系统访问令牌（拉统计用） |
| gateway_token | New API「令牌管理」新建一个 `sk-` 令牌（业务调用用，分组与渠道匹配） |
| default_model | New API 模型目录里的一个名字（如 `deepseek-v4-flash`） |

### 15.3 注意事项（合规 + 运维）

- **只监听 `127.0.0.1`**（New API 默认即 127.0.0.1，不要改 0.0.0.0）；不对外提供生成服务（《生成式 AI 服务管理暂行办法》合规）。
- 上游渠道须有**合法授权**（自购 API/订阅），遵守上游条款。
- `CRYPTO_SECRET` 丢失 ⇒ 已存渠道密钥解不开，须与 `data/` 一起备份。
- 端口冲突：若 `:3000` 被占，改用 `--port 3001` 并在 `llm_gateway_configs.base_url` 同步。
- 该项目是**独立进程**，与 XHS 的 FastAPI（:8000）/ Vite（:5173）互不干扰；随桌面端一并启停。