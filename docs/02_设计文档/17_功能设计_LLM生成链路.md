# 功能设计 · LLM 生成链路（M1 里程碑 · 详细设计）

> - **体系位置**：`docs/02_设计文档/`（二开方向 1 的独立详细设计，文档先行五阶段「阶段二」产出）
> - **上游**：`docs/01_需求文档/05_需求规格_第二阶段二开.md`（方向 1）、`docs/02_设计文档/16_功能设计_第二阶段二开.md` §2
> - **v2.19**（2026-10-03 用户口径三条：「将商品名称、商品参数信息和选择的参考图以及提示词
>   和生成的图片要求**全部整理扔给大模型**…图片支持**下载到本地指定的素材库**」/「新增一个
>   条件：**生成多少张图片**」/「**必须选择原始参考图**才可以点击生成按钮」）：
>   ① **两段式调用** —— 先让文本/多态模型把商品信息（名/参数/要求/参考图）整理成结构化提示词，
>   再交图像模型出图（整理失败**回落**本地拼接，不阻断）；② `count` 1~4 **逐张**发请求，
>   半成功可交付（成功落库 + 失败如实报，全败 502）；③ 参考图**必选**（取代「不勾回落首图」，
>   前后端同一判据）；④ `material_folder_id` 落盘到**用户自己的素材库文件夹**。详见 **§13.3.7**。
>   **零新端点 / 零新表 / 零迁移**，新服务模块 `detail_image_prompt.py`。
> - **v2.15**（2026-10-03 用户口径「生成商品详情图功能，**支持使用提供原商品的详情图作为参考，
>   原始详情图可勾选**」）：**参考图可选 + 详情图从未被用上的根因修复** ——
>   ① `item_detail` 拆出 `product_images`（主图）/ `detail_images`（**原商品详情图**）两组
>   （真机实证：10 张方图 vs 5 张竖长图，此前被 `_image_links` 合并 ⇒ 详情图链路只拿得到主图）；
>   ② 端点加 `reference_image_urls`（**白名单校验**，不在台账里 ⇒ 400 `REFERENCE_IMAGE_NOT_FOUND`，
>   保留 SSRF 防线）；③ 弹窗加缩略图勾选（主图 / 详情图两组）。详见 **§13.3.6**。
>   **零新端点、零新表、零迁移、零新请求函数**。
> - **v2.13**（2026-10-02 用户口径「商品的 AI 生成详情图主图功能在商品列表**操作列**使用**单独弹窗**
>   实现，不要在编辑或详情弹窗中实现；商品同步更新需求为**同步全部状态的商品**和对应操作列的
>   功能和商品列表的批量功能」）：**详情图入口迁到操作列独立弹窗 + 商品同步状态范围显式化** ——
>   ① 「生成商品详情图」从**商品详情弹窗**迁到**列表操作列**的独立弹窗（详情弹窗内联区块整段移除）；
>   ② `SyncShopGoodsRequest.item_status`（默认 `all`）= 显式「同步全部状态商品」，坏值退 `all`；
>   ③ 列表工具栏「同步商品」+ 台账卡「批量同步商品（N）」两个入口。
>   调研依据见 §13.3.5。**零新端点、零新表、零迁移、零新请求函数**。
> - **v2.11**（2026-10-02 用户口径「模型配置列表的当前服务商 / 模型列，服务商和服务商模型**应使用二级联动
>   组件**展示，且**支持修改**，目前**全部无法修改**服务商和模型」）：**服务商 / 模型列改二级联动 + 修掉取数端点** ——
>   ① ⚠️ 根因是**取数端点用错**：页面调 `GET /llm/gateway-configs`（该端点 `models` **恒为 `[]`**，
>   见 §13.2.12 的「`models` 恒空」地雷）⇒ 候选服务商被「按 `models.length > 0` 过滤」全数丢弃 ⇒
>   两列**恒 `disabled`**（正是「全部无法修改」的现象）；改用 **`GET /llm/gateway-configs/models-tree`**
>   （真带模型行）。② 服务商 + 模型**合并为单个 antd `Cascader`**（一级 = 服务商，二级 = 该服务商在该业务点
>   下的可用模型；`changeOnSelect` 允许只选到一级，中间态**只落本地暂存不落库**，保留 v2.5 防死循环守卫）。
>   ③ 类型匹配从「前端裸 `=== kind`」改为**镜像后端 `_type_satisfies`**（`multimodal` 可服务 text/image）；
>   规则全部下沉到 `provider-options.ts` 纯函数（有单测钉住）。**零新端点、零新表**。详见 **§13.2.13**。
> - **v2.10**（2026-10-02 用户口径「**健康状态和启用状态开关放具体模型维度**」「具体的模型也生成一个
>   唯一ID，**使用自然序号**」「子表健康列**未检查的展示检测检查按钮**，触发可健康检查」
>   「**被使用中的模型不支持停用**」）：**模型级健康 + 自然序号 ID + 在用不可停用** ——
>   ① `llm_gateway_models` 加 `healthy` / `last_health_at` / `model_key` 三列（迁移 `b8c9d0e1f2a3`）；
>   ② 健康与启用**下沉到模型维度**（父表删两列）；③ 未探测给「检查」按钮（探测粒度 = 服务商，写回整组）；
>   ④ 被业务点引用的模型停用 ⇒ **400** `LLM_MODEL_IN_USE_BY_BUSINESS`（只拦停用、不拦启用）。
>   **零新端点、零新表**。详见 **§13.2.12**。
> - **v2.9**（2026-10-02 用户口径「**服务商模型不允许手动添加，只能拉取三方**」「添加服务商时**默认拉取一次**，
>   操作列新增**同步服务商模型**按钮」「模型类型**图文改回图片**」「模型类型列表中**支持修改，下拉框**」
>   「配置服务商时**直接父子树展示**该服务商下所有模型」「模型类型由 **AI 调用总结**（text/image/multimodal/video）」
>   「**多平台共用同一模型**」「列表的**地址太短了**」「**父子表使用各自的表头**」）：**模型全生命周期自动化** ——
>   ① 类型枚举扩到**四值**（文本/图片/多态/视频）+ 后端下发；② `platform` 单值 → **多值**（多平台共用）；
>   ③ 模型行**只由同步产生**（拉目录 + **AI 归纳类型**），弹窗手动编辑器**整段移除**；
>   ④ 创建即同步 + 操作列「同步服务商模型」（共用 `_sync_provider_models_core`）；⑤ 类型列**就地可编辑下拉**；
>   ⑥ 前端父子**各自表头**（父表 `expandedRowRender` 渲染独立子表）。详见 **§13.2.11**。
> - **v2.7**（2026-10-02 用户口径「服务商信息是其下的模型**公用的**（接口 key / 调用地址），
>   具体模型才需要分文本、多模态、图片、视频；**使用平台下沉到具体模型**」）：**服务商 / 模型数据分层** ——
>   ① 新增 **`llm_gateway_models`** 子表（模型层：`model_type` + `platform` + 启停）；
>   ② `llm_gateway_configs` **删除 `model_type` / `platform` 两列**（SQL 修复存量后删，非保留）；
>   ③ 类型匹配校验**改查模型层**；④ 前端「候选服务商按类型过滤」→「选完服务商后**模型下拉按 kind 过滤**」。
>   取代 §13.2.1a「一条服务商 = 一种能力 ⇒ 建两条」。详见 **§13.2.9**。
> - **v2.6**（2026-10-02 用户口径「小红书模型配置页面只能配置小红书相关业务使用的模型，
>   把右上角的下拉移除，不支持切换到其它平台」）：**模型配置页移除平台下拉**，页内固定 `xhs`；
>   后端 `PLATFORM_ENUM` 与跨平台隔离能力**原样保留**（前端只删假入口，不删后端能力）。详见 §13.2.8。
> - **状态**：v2.0 · 2026-10-01（**「平台 × 业务点 → 服务商」映射**，用户口径「服务商为单独配置，
>   业务中的使用点映射成固定 key，新增一个模型配置页，不同平台和不同的业务可以**实时切换服务商**」）：
>   ① **一期 `model_configs` 整表退场** —— 表 drop（迁移 `f8a9b0c1d2e3`）+ 6 端点转 **410 墓碑**
>   （`include_in_schema=False`）+ **`ModelConfig` ORM 类整类移除**（留着会让 `create_all()` 把已 drop
>   的表建回来）；② 新增 `llm_business_providers`（唯一键 `(user_id, platform, business_key)`）
>   + 固定业务点常量 `services/business_provider_keys.py`（**5 文本 / 4 图像，共 9 个**；
>   每个业务点带 `label` 中文名与 `triggers` **触发源**，两者只读、随端点下发，见 §13.2.2a）；③ 调用层统一入口
>   `resolve_business_provider(db, user, BUSINESS_KEY_*, platform)`，**映射缺失不回落**（回落 = 用错
>   服务商还不报错）；④ 新增映射 3 端点 + `model_type` 进出参；⑤ 模型配置页重写为「业务点 → 服务商」
>   映射表（业务点清单**后端下发**、候选按 kind 过滤、**选中即 PUT 无保存按钮**）。详见 §13.2。
>   前值 **v1.8 · 2026-09-30**（**`base_url` 语义定稿**：`base_url` = **模型调用地址本身**，
>   后端不重写 / 不归一化 / 不猜测，**直接使用用户输入**拼接子路径 —— 修掉「后端硬拼 `/v1` ⇒
>   路径带 `/v1` 的上游必 404」缺陷（实测 `discovery` 404 根因），见 §7.1 契约与修订 ㉑㉒。v1.7
>   **网关去外挂化**：取消 New API 进程与 iframe 控制台，回归「项目内建 OpenAI 兼容直连 + 本地台账」。
>   v1.6 已落地的 `llm_gateway_tokens`/`llm_gateway_alerts`/`llm_gateway_usage_logs` 三表与监控任务**全部保留沿用**；
>   本轮新增**调用记账器**（台账唯一写入点，`record_usage()`）并**重做前端为网关内 4 个独立页面**（子路由）。
>   ⚠️ 原「T0 部署 New API + iframe 控制台」章节（§15）作废，见 §4.8 自研精简说明。**T2–T10 待后续开工**）
> - **覆盖**：LLM 网关（**项目内建，OpenAI 兼容直连**）+ 服务商/账号（网关配置 + 令牌映射）+ 文案生成 + 媒体生成 + 提示词模板 + 本地素材库 + 用量监控（**本地台账多维聚合看板** + 阈值预警）+ 闭环；
>   ~26 端点、~9 张新表 → 全部在本文档定稿
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
> - **v1.4 修订**（2026-09-29 用户口径：「把大模型网关提升到工作平台维度，多平台公共的大模型网关，内嵌完整的网关自带 UI 页面」）：
>   ⑦ **网关脱离平台前缀**：网关配置端点 `/api/xhs/llm/gateway-configs` → **`/api/llm/gateway-configs`**（工作平台公共维度，
>   小红书/抖音/快手等一切平台共用同一份网关配置；数据层本就 user-scoped 平台无关，**零迁移**，纯路由前缀变更）；
>   ⚠️ **v1.3 的 `/api/xhs/llm/gateway-configs` 路径被 v1.4 取代**（`/api/xhs/llm/` 前缀仍归 T2–T10 的提示词/素材/生成端点）；
>   ⑧ **前端入口公共化**：页面由 `pages/platforms/xhs/llm-gateway-page.tsx` 移至 `pages/llm-gateway/`，路由
>   `/platforms/xhs/llm-gateway` → **`/llm-gateway`**（旧路由重定向），菜单进侧栏公共区（「模型配置」旁）——
>   网关不再属于 xhs 平台菜单；
>   ⑨ **内嵌完整网关自带 UI**：页面改 Tabs「网关配置 / 网关控制台」，控制台 Tab 全高 iframe 内嵌 New API
>   完整管理界面（渠道/令牌/日志/看板都在网关自带 UI 里操作）；统计拉取改走 `fetchGatewayStats()`（http 实例带鉴权，
>   修掉页面裸 `fetch` 无 Authorization ⇒ 401 ⇒ 统计恒空取不到的缺陷）。
> - **v1.5 修订**（2026-09-29 晚用户口径：「入口放 /platform-select 新增网关卡片；网关是独立公共模块，
>   不进小红书平台工作区，有自己的新工作区壳；壳内直接内嵌 New API 新版 UI 所有页面」）：
>   ⑩ **入口 = 平台选择页卡片**：侧栏「大模型网关」菜单项移除；`/platform-select` 平台卡片网格新增独立「大模型网关」卡片
>   （`PlatformSelector` 加 `extra` 插槽承载，紫色 ApiOutlined 图标 + 「公共」Tag），点击直达 `/llm-gateway`；
>   ⑪ **独立工作区壳**：路由 `/llm-gateway` 从 AppShell 路由组**上移为独立 ProtectedRoute**（与 `/platform-select` 同层），
>   页面自建壳层，**布局与小红书工作区（AppShell）保持一致**——Sider 220 可折叠 + logo 行（点击回平台选择）/
>   48px 顶栏（右对齐：网关下拉 + 新窗口打开 + 刷新）/ Content 24 内边距 + PageHeader / 底部「连接配置」菜单 + 用户行
>   ——**不套任何平台工作区壳**；旧路由（`/platforms/xhs/llm-gateway` 与
>   工作区内 `/llm-gateway`）重定向保深链不死；
>   ⑫ **壳内直嵌 New API 新版 UI 所有页面**：侧边栏主菜单 = 新版 UI（Base UI + Tailwind）各页面直达
>   （网关控制台 `/console`、数据看板、渠道管理、令牌管理、用户管理、日志与监控、系统设置、模型游乐场，rc.40 实测全 200），
>   内容区 iframe 直嵌 `{base_url}/console/<page>`（**网关自带原始页面**，非项目重绘）；`?page=` 承载选中页
>   （缺省 = `console` 总览）；「连接配置」（本项目侧 CRUD + 用量统计）为壳内唯一自有页面（`?page=configs`，
>   footer 菜单项）；**不展示** `/console/xxx` 路径类技术文案（用户 22:58 口径「不要」）；
>   ⚠️ **v1.4 ⑧的「菜单进侧栏公共区」与 v1.4 ⑨的「页面 Tabs 双页签」均被 v1.5 取代**。
> - **v1.6 修订**（2026-09-29 晚用户拍板「架构采用 A；令牌按平台发、平台下按功能发；预警做站内通知」，
>   且进一步明确「两个数据库合并 —— 数据层全归主库，New API 只做转发」）：
>   ⑬ **New API 降级为纯转发网关**：三方 Key 仍配在 New API 渠道里（运行时转发 `/v1/chat/completions`），
>   但项目侧**不直读** New API 的 logs / SQLite 库（`local_db_path` 列废弃）；
>   ⑭ **令牌两级（平台→功能）落主库**：新增 `llm_gateway_tokens`（业务功能→New API 令牌名映射，
>   平台 × 功能 两级，如 `xhs` 平台下 `generate`/`review`/`monitor` 三个功能各绑一个 `token_name`）；
>   ⑮ **本地调用台账 = 唯一用量真相源**：新增 `llm_gateway_usage_logs`（项目侧每次经网关发起的 LLM 调用记一行，
>   含 `token_name`/`platform`/`function`/`model`/token 用量/耗时/成败），用量统计与预警都聚合本表 ——
>   **与「仅本机 127.0.0.1 可读 New API 本地库」的旧口径彻底脱钩，任意 base_url（三方/自托管）都支持**；
>   ⑯ **监控预警**：新增 `llm_gateway_alerts`（预警规则：用量阈值 / 失败率阈值 / 网关健康），
>   新增内置任务 `system.llm_gateway_monitor` 轮询本地台账，超阈值 → 写 `notifications` 站内通知；
>   ⚠️ **v1.3 的「`llm_gateway_snapshots` 每日快照」与「统计直读 New API logs」被 v1.6 取代**（原句保留见 §3.9/§4.5/§7.5）；
>   `llm_gateway_snapshots` 表保留不删（历史数据不动），但新代码不再写入。
> - **v1.7 修订**（2026-09-30 用户拍板「**网关太复杂，只做两件事：接三方 API + 监控用量**」，
>   采纳方案 A「精简自研」；参考 GitHub 调研 `llm-gateway-lite`/`llm-proxy-tk`/LiteLLM 结论，
>   见 §4.8 附录）：
>   ⑰ **取消 New API 外挂进程**：不再内置/启动 New API（`data/newapi/` 136MB 二进制 + 独立 SQLite 移除），
>   网关**回归「项目内建、按 OpenAI 兼容协议直连上游」**——上游可以是**三方服务商直连**
>   （DeepSeek / 月之暗面 / SiliconFlow / 中转站，`{base}/v1/chat/completions`）**或**用户自托管的
>   OpenAI 兼容网关（New API/OneAPI/LiteLLM 均可，仅作 base_url）；
>   ⑱ **取消网关控制台 iframe**：`{base}/console/<page>` 内嵌 8 页签整段移除（**8 个空白页根因**），
>   页面改为项目自有 4 个菜单（连接配置 / 用量看板 / 令牌映射 / 预警规则），**不依赖任何外部 UI**；
>   ⚠️ **页面形态经三次修订**（2026-10-01）：
>   - **第一次**：删「用量看板」页签（6 个裸数字无图表），用量视图迁**独立首页** `/usage-dashboard`
>     （DEF-044 不留两份实现）⇒ 网关页 4→**3 页签**；
>   - **第二次**：**独立首页撤销**（用户口径「用量看板单独页面，不要放连接配置中」+「网关里看不到看板」），
>     换成**复用带图表的完整看板**收进网关页作**第一个页签**（排连接配置之前）⇒ 网关页 3→**4 页签**，
>     顺序 = **用量看板 / 连接配置 / 令牌映射 / 预警规则**；`/usage-dashboard` 路由**整条删除**、`/` 重定向回
>     `/platform-select`。**同一语义仍只有一份实现**：内容组件 `UsageDashboardPanel` 只渲染内容，
>     壳层由网关页统一提供。原句保留；
>   - **第三次（现行）**：**取消 Tabs**（用户口径「网关内，每个菜单均是一个单独的页面不要使用 tab」），
>     4 个菜单改为**子路由** `/llm-gateway/{usage,gateways,tokens,alerts}`，侧栏项成为**真正的页面导航菜单**
>     （点击切换页面 + **高亮当前项**），各自独立 URL（可收藏 / 刷新 / 深链）；旧 `?tab=` 链接由
>     `GatewayRoutes` 的 index 分支**重定向**到对应子路由（无 tab ⇒ 默认 `usage`）、未匹配路径回落默认页
>     （不 404）。**「同一语义只有一份实现」仍成立且更强**：壳层（Sider / 48px 顶栏 / `PageHeader`）只有
>     `LlmGatewayPage` + `GatewaySection` 一份，4 个页面只是内容组件（`UsagePage` / `GatewaysPage` /
>     `TokensPage` / `AlertsPage`），看板内容仍是 `UsageDashboardPanel` 唯一实现。原句保留；
>   ⑲ **用量台账成为唯一（也是唯一存在的）真相源**：`llm_gateway_usage_logs` 由「业务调用后记账」
>   写入（唯一写入点 = `services/llm_gateway_usage_service.record_usage()`），用量看板 3 端点
>   （overview/daily/breakdown）全量聚合本表；
>   ⑳ **`local_db_path` 列彻底弃用**（v1.6 标废弃，v1.7 从请求模型移除、新代码不读写）；
>   ⚠️ **v1.3/v1.6 的「New API 纯转发网关」结论被 v1.7 取代**（原句保留见 §1.1/§1.2/§4.1–§4.3/§15）；
>   **v1.2 的 WB2API 与 v1.1 的自研三层调用**仍为历史结论（被 v1.3 取代，不复活）。
> - **v1.8 修订**（2026-09-30 用户拍板，起因实测缺陷：`discovery` 配置调用模型 404）：
>   > 用户原话：「**B（改代码，治本）base_url 就是模型调用地址，不可改变，必须用户输入正确代码直接使用**」
>   ㉑ **`base_url` 语义定稿 = 「模型调用地址本身」**：用户填什么就是什么，后端
>   **不重写 / 不归一化 / 不猜测 / 不自动补或剥 `/v1`**，一切上游请求**直接以该地址为前缀拼接子路径**
>   （`{base_url}/models`、`{base_url}/chat/completions`）。⇒ 用户若填
>   `https://api.deepseek.com/v1`，实际请求即 `https://api.deepseek.com/v1/chat/completions`。
>   ⚠️ **v1.7 及更早的「后端硬拼 `/v1/...`、base_url 必须不带 `/v1`」口径被 v1.8 取代**
>   （原句保留见下方 §3.8 / §4.1 / §4.2 / §7.1 / §15.3 对照）。
>   ⚠️ **旧口径的实测缺陷（本次修复动因）**：库中 `discovery` 行 `base_url` 按前端提示填了
>   `https://discovery-api.intern-ai.org.cn/v1`（该上游 `/v1` 前缀**确为必需**），后端再硬拼一次 `/v1`
>   ⇒ 实际请求 `/v1/v1/models`、`/v1/v1/chat/completions` ⇒ **404 page not found**，
>   该配置**从未探测成功**（`healthy=0` / `models_json=[]` / `last_health_at=NULL`）。
>   ⇒ 旧口径**只能容纳「根地址型」上游**，对「路径本身带 `/v1` 型」上游（书生 discovery、
>   `https://api.moonshot.cn/v1` 等大量真实服务商）**必然失败**。
>   ㉒ **前端提示文案同步改口径**：不再写「填到 `/v1` 为止」，改为**「填入模型调用地址（含路径，
>   如 `https://api.deepseek.com/v1`）」**并给「须与实际调用路径一致」的示例。
>   ⚠️ v1.1–v1.7 的「填到 `/v1` 为止（如 `https://api.deepseek.com/v1`）」保留为历史原句。

---

## 0. 一句话

把一期「千帆 AI 智能生成」扩成**第二套通用 LLM 生成链路**：**网关层在项目内建、按 OpenAI 兼容协议直连上游**
（三方服务商 DeepSeek / 月之暗面 / SiliconFlow / 中转站，或用户自托管的 OpenAI 兼容网关；**不内置任何外挂进程**），
项目侧用「公共提示词模块 + 按任务类型模板」两层结构组装 Prompt，向所选上游发起生成，产出
**标题/正文/话题/封面/视频**，媒体来源支持 **AI 生成 / 本地素材库 / 复用商品原始图 / 参考原始图生成新图** 四种，
生成结果落库为 `local`+`pending` 本地笔记，用户预览编辑后显式发布（走一期千帆 `note/publish` 链路）。
**每次调用落本地台账**（`llm_gateway_usage_logs`，唯一真相源），用量看板与阈值预警全部基于它。

> ⚠️ **v1.7 取代 v1.3–v1.6 的「网关 = New API 外挂进程 + iframe 控制台」结论**：本链路**不依赖任何外部进程**，
> 网关能力由项目自身提供（配置管理 + OpenAI 兼容转发 + 台账 + 看板 + 预警）。原 §4.1–§4.3 与 §15 保留为历史结论。

---

## 1. 范围与边界

### 1.1 本期做（M1）

| 模块 | 做 |
|---|---|
| LLM 网关 | **项目内建**：按 **OpenAI 兼容协议**直连上游（三方服务商或自托管网关），**不内置外挂进程**（v1.7），**不重绘协议层**（上游统一为 OpenAI 形态） |
| 服务商/账号 | **网关连接配置** `llm_gateway_configs`（base_url + 访问令牌 + 模型目录 + 健康状态）+ **令牌映射** `llm_gateway_tokens`（platform×function→token_name，用于台账维度切片） |
| 用量监控 | **本地台账 `llm_gateway_usage_logs`（唯一真相源）** + 项目侧多维聚合看板（overview/daily/breakdown）；**预警** = 内置任务轮询台账 → 超阈值写 `notifications` 站内通知 |
| 文案生成 | 标题（多备选）＋ 正文（600–900 字）＋ 话题标签；经一期护栏 + 话题标记 |
| 媒体生成 | 文生图 / 图生图（参考图）/ 图生视频 / 图文拼图，落本地 + AIGC 溯源 |
| 提示词模板 | 两层结构：`prompt_modules`（公共片段）+ `prompt_templates`（任务模板引用模块） |
| 本地素材库 | 多文件夹扫描/索引/缩略图/检索；只读不改用户文件 |
| 闭环 | 商品 → 配置 → 入队 → worker 生成 → 落库可编辑 → 显式发布（复用一期） |

### 1.2 本期不做（明确排除）

- **不做热点生成**（方向 2，M3）、**不做仪表盘**（方向 3，M2）、**不做商品编辑扩展**（方向 4，M4）、
  不做工作流（方向 5，M5）、订单（方向 6，M6/M7）。
- **不内置外挂网关进程**（v1.7 取代 v1.3 的「集成 New API 独立进程」与 v1.2 的 WB2API）—— 网关能力由项目自身提供
  （配置管理 + OpenAI 兼容直连 + 本地台账 + 看板 + 预警）；上游若要用 New API/OneAPI/LiteLLM，**只当作一个 base_url** 填进连接配置，项目不负责其生命周期。
- **不重绘协议层**：一切上游统一按 OpenAI 兼容形态调用（`GET {base}/v1/models` / `POST {base}/v1/chat/completions`），
  不做 Claude/Gemini 原生格式适配（如需，由上游网关负责转换）。
- **不建服务商层表** `llm_providers`（v1.3 取代 v1.1）—— 服务商即一条网关连接配置。
- **不建用量明细/聚合表** `llm_usage_records`/`llm_usage_stats`（v1.1 概念）—— 用量台账由 **`llm_gateway_usage_logs`（v1.6/v1.7）** 承载。
- **不直读任何上游库 / logs**（v1.6「两个数据库合并」+ v1.7 去外挂）—— `local_db_path` 列**彻底弃用**（v1.7），统计从本地台账聚合。
- **不做网关自带控制台内嵌**（v1.7）—— ~~iframe 内嵌 `{base}/console/<page>`~~ 整段移除（8 页签空白根因）。
- **不做供应商的「能力探测 / 自动选型」**（测试连通走上游 `/v1/models`）。
- **不做对话式改写**（一期 `ai_drafts` 已有 rewrite/polish，本期聚焦「商品 → 笔记」生成）。
- **不做媒体落 `media_assets` 新表** —— 一期 `notes.note_assets` 已是笔记媒体唯一落库；素材库落 `material_files`。

> ⚠️ **本设计对上游 16 文档的三处修订**（16 文档是需求级「链路/方案/原型」，本文档是字段级定稿）：
> ① ~~`llm_providers` / `llm_models` 两张表**取消** → 复用一期 `model_configs`~~ → 历经 **v1.1**（恢复 `llm_providers` 服务商层）→ **v1.3 定稿**：
>   **服务商/账号不再自建**，由**上游侧**承载（v1.3 时为 New API 渠道+令牌；**v1.7 改为直接填一条连接配置**）；
>   项目侧仅 `llm_gateway_configs`（连接配置）—— 见 §3.8；
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
        ├─ (5a) 网关（POST {base}/v1/chat/completions，OpenAI 兼容）→ 标题/正文/话题（JSON，经护栏 + 话题标记解析）
        │       上游 base_url + 令牌 = llm_gateway_configs；模型 = configs.default_model / 生成配置所选
        │       调用后记 llm_gateway_usage_logs（记账器 record_usage，唯一写入点）
        ├─ (5b) 媒体：AI 文生图 / 参考图生成 / 图生视频（同一份连接配置，按上游能力）  OR  本地素材库  OR  复用商品原始图
        ▼
(6) 话题落库 topics + note_topics（复用 note_topic_service，只增不删）
(7) 保存本地笔记（note_type=local + publish_status=local+pending）+ note_assets + note_goods
    + generation_records（一次生成一条审计）+ aigc_media_assets（平台溯源）
(8) 用户预览/编辑 ──► 显式「发布」──► 一期发布链路（预审核 + SKU 挂链 + aigcTaskId）
```

- 生成走队列：`POST /xhs/product-notes/generate-llm` 入队即返回，前端 5s 轮询 `GET /xhs/product-notes/generate-llm-queue`；
  快照键名 `generated`（沿用一期改名纪律，透传底座 `synced` = 进度卡恒 0）。
- **不自动发布**：发布必须用户显式动作（一期 §31.5 纪律延续）。
- 用量统计：**本地台账 `llm_gateway_usage_logs` 聚合**（v1.7 唯一口径）→ 前端「用量看板」三端点
  （overview/daily/breakdown）。~~New API 数据看板承载实时监控；项目侧按日快照 `llm_gateway_snapshots`~~（v1.6/v1.7 取代）。

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
| gateway_config_id | Integer | FK llm_gateway_configs.id, index | ✅ | 所用上游连接配置 |
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

### 3.8 `llm_gateway_configs`（上游连接配置，v1.3 新增；v1.7 通用化定稿 ✅；v1.8 base_url 语义定稿）

> 语义：项目侧对一个 **OpenAI 兼容上游**（三方服务商直连，或用户自托管的 New API/OneAPI/LiteLLM）的
> **单行连接配置**（base_url + 访问令牌 + 默认模型 + 模型目录 + 健康状态）。一切上游统一按 OpenAI 兼容协议接入
> （`GET {base_url}/models` / `POST {base_url}/chat/completions`），**不绑定任何网关特有接口**（v1.7）。
> ⚠️ **v1.8 订正**：上句原写 `GET {base}/v1/models` / `POST {base}/v1/chat/completions` ——
> **`/v1` 不再由后端硬拼**，`base_url` 就是**模型调用地址本身**（含其路径前缀），
> 用户填 `https://api.deepseek.com/v1` ⇒ 实际请求 `https://api.deepseek.com/v1/chat/completions`。见 §7.1。
> ⚠️ **取代 v1.1 的 `llm_providers` 服务商层与 `model_configs` 账号层改造**：服务商即一条连接配置，不另建层。

> ⚠️ **v1.9（2026-10-01，用户口径「连接配置应是新增服务商」）**：本表语义由「网关连接配置」订正为
> **「服务商（Provider）」** —— 一行 = **一个接入的服务商**。三处新增/订正见下表 ⬇️
> （`provider_key` 新增、`platform` 新增、`access_token`/`default_model` 由可选改**必填**、`enabled` 补停用语义）。
> ~~「连接配置」~~ 为 v1.3–v1.8 用语，**页面文案与端点标题一律改「服务商」**（路径不变，见 §7.1）。

| 列 | 类型 | 约束 | 落库 | 说明 |
|---|---|---|---|---|
| id | Integer | PK | ✅ | |
| user_id | Integer | FK users.id, index | ✅ | 归属 |
| **provider_key** | String(64) | 非空，唯一 | ✅ | **v1.9 新增**：**服务编码**（唯一）。业务代码按它引用服务商（§4.3）。**系统生成、创建后不可编辑**（D1，见 §7.1）。格式 `{slug(name)}-{4位随机}`，字母表去易混字符 `0/O/1/I/L`（同 §35.13 批次号纪律） |
| name | String(128) | 非空 | ✅ | 服务商别名（如「DeepSeek 官方」「本机网关」） |
| **platform** | String(32) | 非空（枚举） | ✅ | **v1.9 新增**：「**使用平台**」= 该服务商服务哪个业务平台。取值由后端枚举下发（D3，见 §7.1）。⚠️ **与台账 `platform` 不是同一个东西**，**不许合并**：本列 = 服务商适用范围（静态配置）；台账 `platform` = 该次调用的业务上下文（动态，取自 `draft.platform` 等） |
| base_url | String(512) | 非空 | ✅ | **模型调用地址本身**（v1.8）：如 `https://api.deepseek.com/v1`、`https://discovery-api.intern-ai.org.cn/v1`、自托管网关 `http://127.0.0.1:3000`。**存原值**（仅去首尾空白；~~不带末尾斜杠~~ 为 v1.7 旧口径，见 §7.1 对照）。~~OpenAI 兼容 base_url（如 `https://api.deepseek.com`；不带末尾斜杠）~~ |
| access_token | Text | **非空**（v1.9） | 🔒 | 上游访问令牌（服务商 sk- 或网关签发令牌；`encrypt_text` 加密，响应只给 `has_access_token`）。⚠️ **v1.9：由可选改必填** —— 新建时**必须提供**（空 ⇒ 400 `LLM_PROVIDER_TOKEN_REQUIRED`）；~~默认 ""~~ 仅对 v1.9 之前的存量行成立 |
| local_db_path | String(512) | 默认 "" | ⛔ | ~~New API 本地 SQLite 路径~~ **v1.7 彻底弃用**（列保留不删，新代码不读写） |
| default_model | String(128) | **非空**（v1.9） | ✅ | 默认模型名（**仅兜底**，见 §4.4 ⚠️）。⚠️ **v1.9：由可选改必填** —— 新建时**必须提供**（空 ⇒ 400 `LLM_PROVIDER_MODEL_REQUIRED`） |
| models_json | Text | 默认 "[]" | ✅ | 模型目录（`/test` 从 `{base_url}/models` 拉取后持久化，前端下拉由此驱动；v1.6） |
| enabled | Boolean | 默认 True | ✅ | **停用 = 业务取不到该服务商**（v1.9 补语义）：业务按 `provider_key` 取服务商时命中 `enabled=False` ⇒ **结构化报错 `LLM_PROVIDER_DISABLED`，不静默回落其它服务商**（D4） |
| healthy | Boolean | 默认 False | ✅ | 最近一次健康探测结果（改 base_url 自动置 False） |
| last_health_at | DateTime | nullable | ✅ | 最近健康探测时间 |
| created_at / updated_at | DateTime | default / onupdate | ✅ | |

- **唯一约束**：`UniqueConstraint("user_id", "name")`（用户内服务商名唯一）**+ v1.9 新增**
  `UniqueConstraint("user_id", "provider_key", name="uq_llm_gateway_configs_user_provider_key")`（D2：应用层查重 + DB 唯一索引两道防线）。
- **删除**：v1.9 **移除删除功能**（D-删，见 §7.1）—— 服务商一旦被业务引用（`provider_key` 落在业务数据里），
  删除会让引用悬空 ⇒ 只提供**停用/开启**。存量 `DELETE` 端点保留为 **410 墓碑**。
- **`model_configs` 一期表**：v1.9 起**加 `provider_key` 列**（D5，可空、`String(64)`），
  与 `llm_gateway_configs.provider_key` 相关联 —— 调用链据此取服务商（§4.3）。
  ⚠️ 一期语义（服务 `ai_drafts` 改写/润色）不变；**不**升级为账号层、不加 `provider_id`。

### 3.9 `llm_gateway_snapshots`（用量快照，v1.3 新增；⚠️ v1.6/v1.7 废弃写入）

> 语义：~~New API 数据看板 + `/api/log/stat` 统计 API 承载实时用量；项目侧**按日快照**缓存到本地~~。
> ⚠️ **v1.6 取代**：用量台账改为**本地 `llm_gateway_usage_logs`（§3.13）**，本表**不再写入**
> （历史行保留不删，仅向前兼容）。⚠️ **v1.7 进一步**：连数据来源的 New API 进程本身都已移除 ⇒ 本表**彻底无写入方**。

| 列 | 类型 | 约束 | 落库 | 说明 |
|---|---|---|---|---|
| id | Integer | PK | ✅ | |
| user_id | Integer | FK users.id, index | ✅ | 归属 |
| gateway_id | Integer | FK llm_gateway_configs.id, index | ✅ | 对应连接配置 |
| stat_date | Date | 非空 | ✅ | 统计日（shanghai 时区） |
| call_count / success_count / fail_count | Integer | 默认 0 | ✅ | 当日计数（原自 `/api/log/stat` 聚合） |
| total_quota | Integer | 默认 0 | ⛔ | ~~当日配额消耗（New API quota 单位）~~（v1.7 无来源） |
| total_tokens | Integer | 默认 0 | ✅ | 当日总 token |
| avg_latency_ms | Integer | nullable | ✅ | 当日平均响应耗时 |
| source | String(16) | 默认 "newapi" | ⛔ | ~~快照来源~~（v1.7 无写入方） |
| raw_json | Text | 默认 "{}" | ✅ | 原文快照（历史证据链） |
| created_at / updated_at | DateTime | default / onupdate | ✅ | |

- **唯一约束**：`UniqueConstraint("user_id", "gateway_id", "stat_date")`（每网关每日一行，upsert 幂等）。
- ~~写入方：定时任务每天 0:10 拉一次 New API 统计存快照；前端读本表~~ —— **v1.6/v1.7 无写入方**（表保留不删）。
- ~~明细查询（下钻单次调用）：前端跳到 New API 自带的 /console/log~~ —— **v1.7 移除**。

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

> **新增表合计 12 张**（v1.6 定稿：v1.3 的 9 张 + 令牌映射 / 用量台账 / 预警规则 3 张）：
> `llm_gateway_configs` / `llm_gateway_snapshots` / `prompt_modules` / `prompt_templates` /
> `material_folders` / `material_files` / `generation_records` / `note_generation_links` / `aigc_media_assets`
> / `llm_gateway_tokens` / `llm_gateway_usage_logs` / `llm_gateway_alerts`；
> `llm_providers` / `llm_usage_records` / `llm_usage_stats`（v1.1 增）与 `media_assets`/`llm_models`（16 文档设想）**均不建**。

### 3.11 ~~`llm_gateway_tokens`（业务功能→令牌映射，v1.6 新增）~~ ⚠️ **v1.9 整表退场**

> ⚠️ **v1.9（2026-10-01 用户口径「令牌映射页面移除」）**：**本表整表退场**（迁移 `drop_table`）。
> 理由与逐项清单见 **§7.1a**。原表定义保留在下（不静默删除，AGENTS 硬约束①）：

> ~~语义：**令牌两级（平台→功能）映射表**（v1.6 用户口径「令牌按平台发、平台下按功能发」）。
> v1.6 时为「映射 New API 面板签发的令牌名」；**v1.7 语义收敛**：本表**给台账提供业务维度标签**
> （`(platform, function) → token_name`），调用时按此快照进 `llm_gateway_usage_logs`，
> 用途 = **用量切片 + 预警定域**（不再用于换密钥 —— 令牌统一为 `llm_gateway_configs.access_token`）。
> ⚠️ 上游密钥只在 `llm_gateway_configs` 存一份（项目侧单点），本表只存**维度标签 + 归属**。~~

| 列 | 类型 | 约束 | 落库 | 说明 |
|---|---|---|---|---|
| ~~id~~ | Integer | PK | ~~✅~~ | |
| ~~user_id~~ | Integer | FK users.id, index | ~~✅~~ | 归属 |
| ~~gateway_id~~ | Integer | FK llm_gateway_configs.id, index | ~~✅~~ | 归属连接配置 |
| ~~platform~~ | String(32) | 非空 | ~~✅~~ | 平台（`xhs`/`douyin`/`kuaishou`…） |
| ~~function~~ | String(32) | 非空 | ~~✅~~ | 功能（`generate`/`review`/`monitor`…） |
| ~~token_name~~ | String(128) | 非空 | ~~✅~~ | 维度标签（台账聚合键；可自定义，如 `xhs-generate`） |
| ~~enabled~~ | Boolean | 默认 True | ~~✅~~ | 停用 = 该功能不参与维度切片 |
| ~~created_at / updated_at~~ | DateTime | default / onupdate | ~~✅~~ | |

- ~~**唯一约束**：`UniqueConstraint("user_id", "gateway_id", "platform", "function")`（同网关内 平台×功能 唯一）。~~
- ~~**默认令牌**：未命中映射时台账记 `token_name=""`（不静默失败、不阻断调用；`platform/function` 仍由调用方传入）。~~

⚠️ **退场后遗留**：`llm_gateway_usage_logs.token_name` **列保留不删**（历史行审计用），
v1.9 起新行**恒落 `""`**。

### 3.12 `llm_gateway_alerts`（预警规则，v1.6 新增 ✅）

> 语义：**网关用量/健康预警规则**（v1.6 用户口径「预警做站内通知」）。监控任务 `system.llm_gateway_monitor`
> 定时轮询本地台账，对照规则判断，超阈值 → 写 `notifications` 表。规则分三类：用量阈值 / 失败率阈值 / 网关健康。

| 列 | 类型 | 约束 | 落库 | 说明 |
|---|---|---|---|---|
| id | Integer | PK | ✅ | |
| user_id | Integer | FK users.id, index | ✅ | 归属 |
| gateway_id | Integer | FK llm_gateway_configs.id, index, **nullable** | ✅ | 监控的服务商；**`NULL` = 全网关作用域**（v2.3）|
| platform | String(32) | 默认 "" | ✅ | 平台维度（空 = 该网关全平台） |
| function | String(32) | 默认 "" | ✅ | 功能维度（空 = 该平台全功能） |
| model_name | String(128) | 默认 "" | ✅ | **v2.3 新增**：具体模型作用域（空 = 该服务商全模型）|
| metric | String(16) | 非空 | ✅ | `calls`/`fail_rate`/`health`（白名单 `ALERT_METRICS`，`quota` 已退场） |
| operator | String(8) | 非空 | ✅ | `gt`/`gte`/`lt`/`eq`（白名单 `ALERT_OPERATORS`；health 恒用 `eq`） |
| threshold | Float | 非空 | ✅ | 阈值（calls 为绝对值；fail_rate 为 0–1；health 用 0/1） |
| window_seconds | Integer | 默认 86400 | ✅ | 统计窗口（秒；默认 24h） |
| level | String(16) | 默认 "warning" | ✅ | 通知级别（`info`/`warning`/`error`） |
| enabled | Boolean | 默认 True | ✅ | 停用 = 不触发 |
| last_fired_at | DateTime | nullable | ✅ | 最近触发时间（同日去重防抖） |
| created_at / updated_at | DateTime | default / onupdate | ✅ | |

- **去重**：同规则同日只发一次（`last_fired_at` 当天已发 ⇒ 跳过），避免轮询刷屏。

> ⚠️ **v2.3（2026-10-02）：三级作用域**（用户口径「预警规则可以控制到具体模型维度，
> 也可以服务商维度或全网关」）。作用域由 `gateway_id` + `model_name` **两个字段共同表达**
> （不用哨兵值，语义直白；`gateway_id` 因此**改可空**）：
>
> | 作用域 | `gateway_id` | `model_name` | 读出的 `scope` | 判定目标 |
> |---|---|---|---|---|
> | 全网关 | `NULL` | `""`（必须） | `all` | 该用户**名下全部服务商**，逐个判（方案 A）|
> | 服务商 | 具体 id | `""` | `gateway` | 该服务商 |
> | 具体模型 | 具体 id | 非空 | `model` | 该服务商 + 台账按 `model_name` 再过滤 |
>
> - **唯一非法组合** = **全网关 + 指定模型** ⇒ 400 `LLM_ALERT_SCOPE_CONFLICT`（校验入口
>   `_validate_alert_scope()`）。全网关不绑单个服务商，而「某个模型」必须依附某个服务商
>   才有意义；两者并存只会产生一条语义不明、**永远不会命中**的规则。
> - **`scope` 是派生只读字段**（`_serialize_alert()` 算），前端据此回填三选一下拉。
> - **全网关逐个判（方案 A）**：`_iter_scope_gateway_ids()` 对 `gateway_id IS NULL` 的规则
>   返回**该用户全部服务商**；命中可**多项**，每项各发一条通知（正文指明**是哪个服务商**超限）
>   —— 而不是把多服务商台账合并成一个数字（那样用户不知道去看谁）。
> - ⚠️ **列表端点不带 `gateway_id` ⇒ 返回全部作用域（含 `NULL` 行）**；带 `gateway_id` 过滤
>   ⇒ 只回该服务商的行（`NULL` 行被排除）—— 这是过滤的定义行为，**不是缺陷**。
>   前端预警页刷新**必须不带过滤**，否则全网关规则会「消失」。
> - ⚠️ **更新端点不带作用域字段**（`GatewayAlertUpdateRequest` 只收 threshold/operator/
>   window_seconds/level/enabled）：改作用域 = 删了重建（避免「改了一半」的中间态）。

> ⚠️ **v2.2（2026-10-02）字段口径校准**：上表 `metric` / `operator` / `threshold` 三行的取值
> **以代码白名单为唯一真相源** —— `ALERT_METRICS = ("calls","fail_rate","health")`、
> `ALERT_OPERATORS = ("gt","gte","lt","eq")`（`backend/app/api/llm_gateway_monitor.py`），
> 校验入口 `_validate_alert()`，非法值 ⇒ 400 `LLM_ALERT_METRIC_UNSUPPORTED` /
> `LLM_ALERT_OPERATOR_UNSUPPORTED`。**历史文档里的 `quota` 指标从未落地实现**（v1.6 草案遗留），
> 已从本表删除；`tokens` 亦非本表指标（跑量入口是 `calls`）。

> ⚠️ **v2.2（2026-10-02）：`health` 指标「从未探测」不算不健康**（缺陷修复，见 §4.5 ⚠️）。
> 判据 = `last_health_at is not None AND healthy is False` ⇒ 才触发；**`last_health_at is None`
> （建了但从未跑过 `/test`）⇒ 返回 `None` 不触发**。原口径「`healthy=False` 即触发」会让**每一条
> 新建服务商**在首次探测前就发一条假的「健康探测失败（不可达）」通知 —— 实测复现 1 fired。

### 3.13 `llm_gateway_usage_logs`（本地调用台账，v1.6 新增 ✅）

> 语义：**项目侧每次经网关发起的 LLM 调用记一行**（v1.6 用户口径「两个数据库合并 —— 数据层全归主库」）。
> **唯一用量真相源**：用量展示 / 预警都聚合本表，与 New API logs 彻底解耦（任意 base_url 均支持）。
> ⚠️ 取代 v1.1 的 `llm_usage_records`（明细）与 v1.3 的「快照直读 New API logs」两条旧路线。

| 列 | 类型 | 约束 | 落库 | 说明 |
|---|---|---|---|---|
| id | Integer | PK | ✅ | |
| user_id | Integer | FK users.id, index | ✅ | 归属 |
| gateway_id | Integer | FK llm_gateway_configs.id, index | ✅ | 发起调用的网关 |
| platform | String(32) | 默认 "" | ✅ | 业务平台（映射命中时快照） |
| function | String(32) | 默认 "" | ✅ | 业务功能（映射命中时快照） |
| token_name | String(128) | 默认 "" | ✅ | 实际使用的令牌名（聚合键） |
| model_name | String(128) | 默认 "" | ✅ | 实际所用模型 |
| prompt_tokens | Integer | 默认 0 | ✅ | 提示 token |
| completion_tokens | Integer | 默认 0 | ✅ | 补全 token |
| latency_ms | Integer | 默认 0 | ✅ | 耗时 |
| status | String(16) | 默认 "success" | ✅ | `success`/`failed` |
| error_message | Text | 默认 "" | ✅ | 失败原因（护栏/上游/超时） |
| created_at | DateTime | default | ✅ | 调用时刻（index，窗口聚合用） |

- **索引**：`user_id`、`gateway_id`、`created_at`（窗口聚合按 `created_at >= now - window` 扫）。
- 写入方：**`services/llm_gateway_usage_service`（唯一写入点，v1.7）** —— 内部两个入口：
  `record_usage()`（唯一 INSERT，已知网关 id 时直调）与 `record_usage_for_model()`（`ai_service` 用：
  手里只有 `ModelConfig`，先按 `base_url` 反查本用户的网关 id）。由
  `ai_service` 的 `_complete()`（文本）与 `describe_image()`（图片）在调用后触发，**成功或失败都记一行**
  （失败记 `status="failed"` + `error_message`，不落半条笔记但台账要留痕）；**记账失败只 warning，不影响业务调用**。
  字段级契约（反查规则 / `platform×function` 取值表 / 缺值口径）见 **§4.3.1**。

---

## 4. LLM 网关设计（v1.7：项目内建、OpenAI 兼容直连 + 本地台账）

> ⚠️ **本节 v1.7 取代 v1.6 的「New API 纯转发外挂进程」结论**（2026-09-30 用户拍板「太复杂，只做两件事」）。
> ⚠️ 更早的结论保留为历史：v1.2「复用本地 WB2API」、v1.1「自研三层调用」、v1.3「集成 New API 网关」均被取代。

### 4.1 网关是什么（v1.7 定位）

**网关 = 项目内建的一层薄能力，不是一个外挂进程。** 它只做三件事：

1. **管上游连接**：`llm_gateway_configs` 存 `base_url` + `access_token`（加密）+ `default_model` + 模型目录（`models_json`）；
2. **转发调用**：业务按 OpenAI 兼容形态直连 `{base_url}/chat/completions`（复用 `ai_service` 既有直连逻辑）。
   ⚠️ **v1.8**：`{base_url}` 即**用户填的调用地址本身**，后端**不再硬拼 `/v1`** ——
   填 `https://api.deepseek.com/v1` ⇒ 实际 `…/v1/chat/completions`；
   填 `http://127.0.0.1:3000` ⇒ 实际 `…:3000/chat/completions`（**由用户在地址里自行决定有无 `/v1`**）。~~`{base_url}/v1/chat/completions`~~（v1.7 旧口径）
3. **记台账**：每次调用落 `llm_gateway_usage_logs`（`record_usage()` 唯一写入点），供看板与预警聚合。

**上游是什么**？任意 OpenAI 兼容端点：

- **三方服务商直连**（最常用）：DeepSeek `https://api.deepseek.com/v1`（~~`https://api.deepseek.com`~~ v1.8 订正：
  该服务商调用路径带 `/v1`）、月之暗面 `https://api.moonshot.cn/v1`、书生 `https://discovery-api.intern-ai.org.cn/v1`、
  SiliconFlow、OpenRouter、各类中转站 —— 只需一份 sk- 令牌，**地址按各家文档原样填**；
- **用户自托管的聚合网关**：New API / OneAPI / LiteLLM / llm-gateway-lite —— 项目**只把它的地址当 base_url 填**，
  **不负责它的进程生命周期、不内嵌它的控制台、不读它的库**。

> ⚠️ ~~**New API 是什么（选型依据）**：45K★，One API 增强分支……自带数据看板与 `/api/log/*` 统计 API~~
> —— **该选型结论被 v1.7 取代**（原句见 git 历史 v1.6）。理由：对「只接三方 API + 看用量」而言，
> New API 的多租户/额度/兑换码/渠道分销体系是**过度设计**，且引入 136MB 外挂进程 + 独立库 + iframe 登录态问题
> （8 个空白控制台页的真实根因）。

### 4.2 集成拓扑（v1.7：无外挂进程，单库单链路）

```
┌─────────────── XHS 项目（唯一进程 + 唯一数据库）───────────────┐
│ 前端：网关内 4 个独立页面（用量看板 / 连接配置 / 令牌映射 / 预警规则，子路由） │
│ 后端：                                                          │
│   llm_gateway_configs ──► ai_service 直连 {base_url}/chat/…   │──HTTP──► 三方服务商 / 自托管网关
│   （base_url + 令牌 + 模型目录）  ▲ v1.8：base_url 即调用地址，   │            （仅作上游，项目不管其生命周期）
│                                  后端不再硬拼 /v1              │
│   record_usage() ──► llm_gateway_usage_logs（唯一真相源）       │
│   usage 端点：overview / daily / breakdown ◄── 聚合台账          │
│   system.llm_gateway_monitor ──► 超阈值 → notifications         │
│  SQLite data/spider_xhs.db（一切数据，无第二个库）              │
└────────────────────────────────────────────────────────────────┘
```

- **进程生命周期**：**没有额外进程**。~~New API 以独立进程随 XHS 桌面端一并启停~~
- **数据归属**：用量台账 / 令牌映射 / 预警规则**全部落项目主库** `data/spider_xhs.db`，**全项目只有这一个库**。
- **健康探测**：`POST /api/llm/gateway-configs/{id}/test` 打 `{base_url}/models`（带令牌）取模型目录 + 可选极小 chat 验证。
  ⚠️ **v1.8**：`/v1` 不再由后端硬拼，`{base_url}` 即用户填的地址本身（见 §7.1）。

### 4.3 项目侧调用链（薄客户端）

> ⚠️ **v1.9（2026-10-01）本节重写**：用户口径「**其它业务代码中将直接使用该服务商 key 直接调用**」。
> 这是本次优化的**核心实质改动**（其余多为页面与字段）。
>
> **改造前的事实（v1.8 及更早，实测）**：调用链**根本没接网关表** ——
> `ai_service.OpenAICompatibleTextClient._complete()` 用的是一期 `model_configs` 表的
> `base_url` + `encrypted_api_key`；`llm_gateway_configs` 仅被 `resolve_gateway_id(user_id, base_url)`
> **按 base_url 字符串反查**，用途只有「给台账找个 `gateway_id`」，匹配不上就**静默不记账**。
> ⇒ 所谓「网关」当时只是一张**旁挂的记账索引表**，不控制调用。

#### 4.3.1 服务商解析（**v1.9 唯一入口**）

`resolve_provider_context(db, user_id, provider_key) -> ProviderContext`

| 返回 | 说明 |
|---|---|
| `base_url` | 服务商的模型调用地址（`llm_gateway_configs.base_url`，原值） |
| `access_token` | **解密后**的服务商令牌（`decrypt_text`） |
| `default_model` | 服务商默认模型（兜底） |
| `provider_id` / `platform` | 台账记账用（`gateway_id` = 服务商 id） |

**解析规则（D4/D5/D6 定稿）**

1. **按 `provider_key` 查**（**D6**）：`(user_id, provider_key)` 唯一命中一行。
   ⚠️ **不再按 `base_url` 反查** —— 旧反查是「配置字符串巧合匹配」，改一个字符就断；
   `provider_key` 是**显式契约**，业务引用它不会因用户改地址而失联。
2. **`enabled=False` ⇒ 抛 `LLMProviderDisabledError`**（码 `LLM_PROVIDER_DISABLED`，**D4**）。
   ⚠️ **禁止静默回落**到其它服务商：回落会让台账把 A 的调用记在 B 名下，且用户以为停用生效了。
3. **查不到 `provider_key` ⇒ 抛 `LLMProviderNotFoundError`**（码 `LLM_PROVIDER_NOT_FOUND`）。
   ⚠️ **不做 base_url 兜底回落**（**D6 定稿：不保留过渡反查**）—— 回落 = 两条真相源，
   改错了不会报错，只会静默用旧凭据（DEF-048 同型）。
4. **`provider_key` 从哪来**（**D5**）：`model_configs` 表**新增可空 `provider_key` 列** ——
   一期模型配置是「业务实际持有的那份凭据记录」，让它**指向**服务商即可，无需业务层另传参。
   - `model_configs.provider_key` **非空** ⇒ 走服务商解析（**v1.9 主路径**）；
   - **为空** ⇒ 回落旧行为（直接用 `model_configs` 自身的 `base_url` + `encrypted_api_key`），
     **且不记账**（与 v1.8 的「反查不中就不记账」等价，行为不变）。
     ⚠️ 这条回落**只服务存量数据**，不是推荐路径；新建模型配置应填 `provider_key`。

#### 4.3.2 模块职责

- `services/llm_gateway_usage_service.py`：
  - `record_usage(user_id, gateway_id, platform, function, model_name, prompt_tokens, completion_tokens, latency_ms, status, error_message)`
    —— **台账唯一写入点**。失败也记（`status="failed"` + `error_message` 截断），保证「失败率」预警有数据。
  - ⚠️ **v1.9**：`token_name` **恒落 `""`**（`resolve_token_name()` 与令牌映射随 §7.1a 退场）。
    `platform` / `function` 参数**保留**（仍是台账业务维度）。
- `services/llm_gateway_client.py`：
  - **v1.9 新增** `resolve_provider_context()`（上表）—— 服务商解析唯一实现（DEF-044）。
  - `resolve_text_model_context(db, current_user)` 保留（取默认文本模型），
    但**改由它内部先解析服务商**（若 `model_configs.provider_key` 非空则叠加服务商的
    `base_url`/`access_token`，见 §4.3.1 规则 4）。
- `services/ai_service.py`：`OpenAICompatibleTextClient._complete()`（**文本唯一汇聚点**）与
  `OpenAICompatibleImageClient.describe_image()`（图片链路）在 `requests.post` 前后计时、
  读取 `payload["usage"]`（缺则 0），**调用后统一记账**。
  ⚠️ 记账**不得改变调用语义**：记账失败只 `logger.warning`，**绝不让业务调用失败**（安全网不得条件性，DEF-048）。
  ⚠️ **v1.9**：本方法**签名与实现不变**（它只认 `model_config` + `api_key`）——
  服务商解析发生在**上游的 `resolve_*` 阶段**，不侵入汇聚点。
- **不做**：不做协议适配（上游统一 OpenAI 形态）、不做多账号轮转（需要则由上游网关做）。

#### 4.3.3 记账契约（字段级）

> `platform × function` 是看板切片与预警定域的**聚合键**，调用点各写各的字符串就是维度分叉（DEF-044）。

**两个入口（`services/llm_gateway_usage_service.py`，全部关键字传参）**

| 入口 | 入参 | 用途 |
|---|---|---|
| `record_usage()` | `user_id` / `gateway_id` / `platform` / `function` / `model_name` / `prompt_tokens` / `completion_tokens` / `latency_ms` / `status` / `error_message` | **台账唯一 INSERT 点**；已知网关 id 时直调。返回 `bool`（是否写入成功），**从不抛异常** |
| `record_usage_for_model()` | `model_config`（`ModelConfig` 行）+ 上述除 `user_id`/`gateway_id` 外的字段 | `ai_service` 专用：先解析服务商再委托 `record_usage()` |

**`gateway_id` 解析规则**（⚠️ **v1.9 取代 v1.8 的 base_url 反查**）

- **v1.9 主路径**：`model_config.provider_key` 非空 ⇒ 按 `(user_id, provider_key)` 查 `llm_gateway_configs.id`。
- **v1.9 回落**：`provider_key` 为空 ⇒ **跳过记账**（不落半条台账、不阻断业务，记 `logger.warning`）。
- ~~v1.8：按 `normalize(model_config.base_url)` 与该用户全部网关逐行比，命中即取其 id~~ —— **被 v1.9 取代**（原句保留）。

| 位置 | v1.8 及更早（旧） | v1.9（现行） |
|---|---|---|
| 命中的键 | `base_url`（字符串归一化比对） | **`provider_key`**（显式契约） |
| 未命中行为 | 静默跳过记账 | 静默跳过记账（不变） |
| 停用服务商 | 无「停用」概念 | **抛 `LLM_PROVIDER_DISABLED`，不回落** |

- ⚠️ **解析必须带 `user_id` 约束**（跨用户不串号：两个用户可以填同一个 `base_url` / 同名服务商）。
- ⚠️ **解析失败 ⇒ 不落半条台账、不阻断业务**（宁缺勿造 = §5 硬约束 5）。
  两类失败形态：① 该用户没建服务商配置（`model_configs` 是历史直连配置，不属服务商托管）；
  ② `model_config` 缺 `user_id` / `provider_key`。

**`platform` / `function` 由调用方显式下传**（`_complete()` / `describe_image()` 及其公开方法
都加了这两个**可选**关键字参数，默认 `""`）：

| 调用点 | platform | function |
|---|---|---|
| `POST /api/ai/rewrite-note` | `draft.platform` | `rewrite` |
| `POST /api/ai/generate-note` | 请求体 `platform` | `generate` |
| `POST /api/ai/generate-title` | `xhs` | `title` |
| `POST /api/ai/generate-tags` | `xhs` | `tag` |
| `POST /api/ai/polish-text` | `xhs` | `polish` |
| `POST /api/ai/images/describe` | `xhs` | `describe` |
| `POST /api/xhs/product-notes/generate`（商品笔记干跑，经 `product_note_service.generate_product_note`） | `xhs` | `generate` |
| `services/auto_ops_service.py` 无人值守链路（关键词→搜索→AI 改写→发布） | `xhs` | `rewrite`（正文改写）/ `title`（标题改写） |

- 取值仍是**代码常量**（`FUNCTION_REWRITE` / `FUNCTION_GENERATE` / `FUNCTION_TITLE` / `FUNCTION_TAG` /
  `FUNCTION_POLISH` / `FUNCTION_DESCRIBE`），调用点**不许就地写字面量** —— 理由从 v1.8 的
  「必须与 `llm_gateway_tokens.function` 逐字一致」**改为**「看板切片与预警定域按它聚合，
  写错只是维度对不上、不报错」（**v1.9：该纪律的触发源变了，纪律本身保留**）。
- ~~未命中映射 ⇒ 台账 `token_name=""`（§3.11「默认令牌」）~~ —— **v1.9：`token_name` 恒 `""`**（§7.1a）。
- 图片生成的另两条链路（`generate_cover` / `generate_image`）**v1.7 暂不记账**（本期范围只圈
  `_complete` 与 `describe_image` 两个汇聚点）；纳入时按本表补一行并同步 `FUNCTION_*`。

**`status` / token 用量 / 耗时口径**

- `status` = `success` / `failed`；**HTTP 通了但解析失败**（`choices[0].message.content` 缺失、
  正文为空、`_load_json_response` 失败）记 `failed` + `error_message`（上游违约，业务同样拿不到结果）。
- `prompt_tokens` / `completion_tokens` 读响应 `usage`；**缺 `usage` 或字段非法一律记 0**
  （不造假、不推算精度）。
- `latency_ms` = `time.perf_counter()` 包住 `requests.post` + 解析的整数毫秒。
- ⚠️ **参数校验失败（缺 `base_url` / `model_name` / `api_key`）不记账** —— 那一步**没有发起上游
  调用**，记了就是假账。

### 4.4 能力线 / 模型

- New API 渠道里配置的模型 = 可用模型集合（`GET /v1/models` 可查）。XHS 生成侧只需在 `llm_gateway_configs.default_model`
  与生成配置里选模型名；**模型能力（text/image/video）由上游模型名决定**（一个模型名即一种能力）。
- 文案生成走 `text` 模型；封面/配图走 `image` 模型；视频走 `video` 模型 —— 均指上游 `/v1/models` 目录里的名字。
- ⚠️ **业务不同 ⇒ 模型不同是硬需求**：`llm_gateway_configs.default_model` 只作**兜底默认**；每个生成任务必须按 `task_type`（text/image/video/cover）从**模型目录**（`/test` 端点返回的 models 白名单）显式指定 `model_name`，`generation_records.model_name` 快照实际所用（改配置不丢审计）。禁止把网关配置设计成「单一模型一条道走到黑」。
- 测试连通：`POST /api/llm/gateway-configs/{id}/test` → 打上游 `/v1/models`（校验令牌 + 取模型目录）+ 一次极小 chat 请求（可选）。

### 4.5 用量监控与预警（v1.6：本地台账为真相源，站内通知预警）

> ⚠️ **本节 v1.6 取代 v1.3 的「New API 看板 + `/api/log/stat` + 项目侧快照」结论**（用户口径
> 「两个数据库合并」「预警做站内通知」）：用量台账落主库，项目侧聚合自己的 `llm_gateway_usage_logs`，
> **不再直读 New API logs**。

| 层 | 谁做 | 说明 |
|---|---|---|
| 本地台账 | **本项目 `llm_gateway_usage_logs`** | 项目侧每次发起 LLM 调用记一行（token_name/platform/function/model/token 用量/耗时/成败），**唯一用量真相源**，由 `record_usage()` 写入 |
| 用量展示 | **本项目** | 从 `llm_gateway_usage_logs` 聚合（按平台/功能/模型/日 多维）→ 前端「用量看板」页（overview/daily/breakdown 三端点） |
| 预警 | **本项目内置任务 `system.llm_gateway_monitor`** | 定时轮询本地台账，对照 `llm_gateway_alerts` 规则（用量阈值 / 失败率阈值 / 网关健康），超限 → 写 `notifications` 站内通知 |

> ⚠️ ~~实时看板 **New API /console**（参考）：渠道/令牌/日志在网关自带 UI 操作~~ —— **v1.7 移除**（不再有外挂控制台）。

- **令牌两级维度**：台账按 `platform × function` 两级聚合（如 `xhs/generate`、`xhs/review`、`douyin/generate`），
  预警与展示都按这两个维度切片，与 `llm_gateway_tokens` 映射对齐。
- **预警去重**：同一规则同一天只发一次通知（`llm_gateway_alerts.last_fired_at` 防抖），避免轮询周期内刷屏。
- **通知**：复用一期 `notification_service` + `notifications` 表（站内通知中心，`level=warning/error`），**不接外部推送**。

> ⚠️ **`health` 指标的可触发前提（v2.2 修复，2026-10-02）**：`last_health_at IS NOT NULL` ——
> **「从未探测」≠「探测失败」**。`healthy` 列默认 `False`，若直接按 `healthy=False` 触发，
> 则**任何新建服务商在第一次点「测试连接」之前**都会命中「健康探测失败（不可达）」，
> 发出一条与事实相反的站内通知（用户看到「不可达」但其实从没探测过）。
> - 判据：`last_health_at is None` ⇒ 返回 `None`（不触发，与 `fail_rate` 无数据同型）；
> - 仅当**探测过且结果为不健康**（`last_health_at` 有值 + `healthy=False`）才触发；
> - 文案区分：探测过失败 ⇒ 「健康探测失败（不可达）」；从未探测 ⇒ 不通知（用户未测试，无话可说）。
> 实测复现：未探测网关 + `health eq 0` 规则 ⇒ 旧代码 `fired=1`（假通知），修复后 `fired=0`。

### 4.5.1 预警规则 CRUD 与「不可删除」纪律（v2.2 补充）

- 预警规则**可删除**（`DELETE /api/llm/gateway-alerts/{id}` 真删，返回 `{"status":"deleted"}`）——
  ⚠️ **与「服务商不可删除」（§7.1 D-删，410 墓碑）刻意不同**：服务商的 `provider_key` 会**落到业务数据里**
  （`llm_business_providers` 引用）⇒ 删了会让业务悬空；而预警规则**只被监控任务读、不被任何业务数据引用**
  ⇒ 真删无悬空风险。**判据 = 是否被业务数据引用，不是「表重不重要」**（避免把 D-删 误推广成全表禁删）。
- 规则字段校验走 `_validate_alert()`：`metric` ∈ {`calls`,`fail_rate`,`health`}、
  `operator` ∈ {`gt`,`gte`,`lt`,`eq`}；非法 ⇒ 400 结构化原因。
- **作用域校验走 `_validate_alert_scope()`**（**v2.3**）：唯一非法组合 = 全网关 + 指定模型
  ⇒ 400 `LLM_ALERT_SCOPE_CONFLICT`；服务商作用域还须校验 `gateway_id` **归属**（别人的 ⇒ 404
  `LLM_GATEWAY_NOT_FOUND`）。三级作用域定义见 §3.12 ⚠️。
- ⚠️ **更新端点不改作用域**：`GatewayAlertUpdateRequest` 只收 threshold/operator/window_seconds/
  level/enabled —— 改作用域 = 删了重建（避免「改了一半」的中间态；前端编辑弹窗把作用域
  三个下拉置灰）。

> ⚠️ **v2.3（2026-10-02）：预警三级作用域**（用户口径「预警规则可以控制到具体模型维度，
> 也可以服务商维度或全网关」）。字段口径与判定表见 §3.12；三处实现要点：
> 1. **全网关逐个判（方案 A）**：`_iter_scope_gateway_ids()` 对 `gateway_id IS NULL` 的规则
>    返回**该用户全部服务商**，逐个取值判定；命中**可多项**，每项各发一条通知，正文用
>    `_describe(alert, value, gateway_label)` 带上**服务商名**（否则用户不知道去看谁）。
> 2. **具体模型作用域**：台账 SQL 再 `WHERE model_name == alert.model_name` 过滤
>    （数据源列 `llm_gateway_usage_logs.model_name` 早已存在，本轮只是终于用上）。
> 3. **端点侧**：`GET /gateway-alerts` **不带过滤 ⇒ 返回全部作用域（含 `NULL` 行）**；
>    `POST` 按两字段推导并校验；`_serialize_alert()` 下发派生只读 `scope`
>    （`all`/`gateway`/`model`）+ `model_name`，前端据此渲染与回填。

> ⚠️ 与 v1.1/v1.3 的差异：**Token/额度明细不重复落 New API 侧权威**（本地台账已是权威，DEF-044 单一真相源）。

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

### 4.8 附录：开源方案调研结论（v1.7 决策依据）

> 2026-09-30 用户口径：「网关太复杂，调研 GitHub 简化项目，或参考开源自研精简版」。
> 调研范围：按「**只接三方 API + 监控用量**」两条硬标准筛。

| 项目 | 技术栈 | 契合度 | 结论 |
|---|---|---|---|
| **llm-gateway-lite**（answerlink） | OpenResty/Lua | ★★★★★ 默认 `GATEWAY_AUTH_ENABLED=false`（无用户体系）、YAML 热重载、内置 `/admin/stats` 看板 | **仍是一个外挂进程**（要装 OpenResty）⇒ 不解决「多一个东西要运维」 |
| **llm-proxy-tk**（boonya-hrgk） | FastAPI | ★★★★ 多上游路由、SSE 透传、sk- 密钥管理、用量统计 | 技术栈一致，但**仍是外挂进程** |
| **llm-gateway**（AaronRZH） | Go + Gin | ★★★ 轻量，PG/Redis 可降级 | 外挂进程 + 额外依赖 |
| **LiteLLM**（BerriAI） | Python 58K★ | ★★ 100+ 厂商 | Proxy 模式强依赖 PostgreSQL，**比 New API 更重** |
| **New API / One-API** | Go 单二进制 | ★★ | 自带用户/额度/兑换码体系，**就是被嫌弃复杂的那个** |

**决策**：**不引入任何外挂进程**。项目**已具备**「接三方 API」全部能力（`ai_service` 直连 + `llm_gateway_configs` 配置），
唯一缺口是「记用量」——只需一个记账器 + 三个聚合端点 + 一个看板页。故取**方案 A（精简自研）**：
删外挂、删 iframe、补台账，**净减一个 136MB 进程 + 一个独立数据库 + 一套账号体系**。

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
> ⚠️ 端点前缀沿用一期 `GET /openapi.json` 实测的**三层平台路由** —— 生成链路端点挂在
> `/api/xhs/product-notes/` 与 `/api/xhs/llm/` 下；**唯一例外 = 网关配置**（v1.4 提升工作平台维度，
> 多平台公共，挂 **`/api/llm/`**，见 §7.1）。

### 7.1 网关配置（上游连接 CRUD，v1.3：~5 端点；v1.4 路径工作平台维度；v1.7 通用化；v1.8 base_url 语义定稿）

> ⚠️ **v1.7**：上游是**任意 OpenAI 兼容端点**（三方直连或自托管网关），端点语义不变、**去掉 New API 特化探测**。
> ⚠️ **v1.1 的「服务商层 + 账号层升级」与 v1.2 的「WB2API 连接」均被 v1.3 取代**（原句见 §0 修订说明）；
> v1.3 的「New API 面板管理渠道+令牌」被 v1.7 取代（改为连接配置直管）。
> ⚠️ **v1.4（2026-09-29 用户口径）**：网关是**多平台公共**的工作平台级设施，端点路径为 **`/api/llm/gateway-configs`**。

#### 7.1.0 `base_url` 语义定稿（v1.8 唯一口径，全链路强制）

> **`base_url` = 模型调用地址本身。** 用户在连接配置里填的就是**发起 HTTP 请求时要打的那个地址前缀**，
> 后端**直接使用**它拼子路径，**不重写、不归一化、不猜测、不自动补或剥 `/v1`**。
> 用户原话（2026-09-30）：「**base_url 就是模型调用地址，不可改变，必须用户输入正确代码直接使用**」。

**拼接规则（唯一口径，三处拼接点必须一致）**

| 动作 | 实际请求 URL | 用户应填 |
|---|---|---|
| 健康探测 / 模型目录 | `{base_url}/models` | `https://discovery-api.intern-ai.org.cn/v1` ⇒ `…/v1/models` |
| 业务调用（文本） | `{base_url}/chat/completions` | `https://api.deepseek.com/v1` ⇒ `…/v1/chat/completions` |
| 图片生成 | `{base_url}/images/generations` | 同上（含 `/v1`） |
| 自托管网关（New API 等） | `{base_url}/chat/completions` | `http://127.0.0.1:3000` ⇒ `…:3000/chat/completions` |

- **规范化边界**：落库前**只做「去首尾空白」**（`str.strip()`）。**不**去末尾斜杠（~~v1.7 的 `.rstrip("/")`~~ 已取消），
  **不**剥 `/v1`、**不**补 `/v1`。⚠️ 为兼容既有历史行（可能带末尾 `/`，如 `http://127.0.0.1:3000/`），
  **拼接子路径前仍做一次 `rstrip("/")` 以防出现双子斜杠** —— 这是**URL 拼接的语法需要，不是对用户语义的改写**；
  ⚠️ 记账反查口径（§4.3.1）也必须同步为「strip + rstrip(`/`)」以保持命中。
- ⚠️ **旧口径对照表（被 v1.8 取代，原句保留）**：

| 位置 | v1.7 及更早（旧） | v1.8（现行） |
|---|---|---|
| `api/llm_gateway.py` 探测 | `f"{base_url}/v1/models"` | `f"{base_url}/models"` |
| `api/llm_gateway.py` chat | `f"{base_url}/v1/chat/completions"` | `f"{base_url}/chat/completions"` |
| `services/ai_service.py` 文本汇聚点 | `f"{base_url.rstrip('/')}/chat/completions"`（**已正确，不动**） | 同左（本就是「直接用地址」语义） |
| `api/model_configs.py` 测试 | `f"{base_url}/chat/completions"`（**已正确，不动**） | 同左 |
| `DEFAULT_BASE_URL` | `https://api.openai.com/v1`（与硬拼 `/v1` 冲突） | `https://api.openai.com/v1`（**保留**，因不再硬拼 ✓） |
| 表 3.8 base_url 说明 | 「不带末尾斜杠」 | 「模型调用地址本身」 |
| 前端提示 | 「填到 `/v1` 为止（如 `…/v1`）」 | 「填入模型调用地址（含路径），须与实际调用路径一致」 |

> ⚠️ **该缺陷的教训（写入 §7.1 以免复发）**：旧口径下「后端硬拼 `/v1`」**只能容纳根地址型上游**，
> 对路径本身带 `/v1` 的上游（书生 discovery、`api.moonshot.cn/v1` 等）**必然 404**
> —— 且症状是 `healthy=0` + 模型目录空，**看起来像令牌错或网络不通，实为路径双拼**。
> ⇒ 判定「上游不通」时**必须先打印实际请求 URL**（本设计口径要求：探测失败的 `message` 应含 URL 片段）。

**`GET /api/llm/gateway-configs`**（服务商列表）
- 返回 `paginated`，字段 `id/name/provider_key/platform/base_url/default_model/models/enabled/healthy/last_health_at`
  （**令牌字段永不返回**，只给 `has_access_token` 布尔）。
- ⚠️ **v1.9**：新增 `provider_key` 与 `platform` 两个出参。

**`GET /api/llm/providers/platforms`**（**v1.9 新增**：使用平台枚举下发；**独立前缀** `/api/llm/providers`，不挂在 `gateway-configs` 下 —— 枚举是「服务商」维度的公共字典，与某条具体配置无关）
- 无参；返回 `{items: [{value, label}...]}`，`value` 为枚举键、`label` 为中文名。
- 取值集（D3，与 `api/ai.py` 既有的 `platform` 字面量集**同源**）：
  `xhs`(小红书) / `douyin`(抖音) / `kuaishou`(快手) / `weibo`(微博) / `xianyu`(闲鱼) / `taobao`(淘宝)。
- ⚠️ **枚举由后端下发**（用户口径「后端新增枚举下拉选择」）—— 前端**不许就地写死选项**，
  否则新增平台要改两处（DEF-044）。
- ⚠️ 与台账 `platform`（§4.3.1 的 `platform × function` 聚合键）**不是同一个概念**（§3.8 ⚠️），**不许复用同一份常量表**。

**`POST /api/llm/gateway-configs`**（新增服务商）
- 请求：~~`{name, platform, access_token, default_model, base_url?}`~~
  ⚠️ **被 §13.2.9（v2.7）+ §13.2.10（v2.8）取代** —— 现为
  `{name, access_token, base_url?, models: [{model_name, model_type, platform, enabled, remark}]}`：
  `platform` / `model_type` 下沉到**模型行**，`default_model` 整列删除。
  `access_token` 用 `encrypt_text` 加密落库；`name` 用户内唯一（409 `LLM_GATEWAY_NAME_EXISTS`）；
  返回 201 + 对象（令牌不返）。
- ⚠️ **v1.9 必填三件**（用户口径「访问令牌和默认模型必填」「使用平台必选」）：
  ~~`access_token` 空 ⇒ 400 `LLM_PROVIDER_TOKEN_REQUIRED`；`default_model` 空 ⇒ 400 `LLM_PROVIDER_MODEL_REQUIRED`；
  `platform` 缺失或非枚举值 ⇒ 400 `LLM_PROVIDER_PLATFORM_REQUIRED`。~~
  **v2.7/v2.8 现状**（原句保留）：`access_token` 空 ⇒ 仍 400 `LLM_PROVIDER_TOKEN_REQUIRED`；
  ~~`default_model`~~ / ~~服务商级 `platform`~~ **两件已随列删除**，改为**每个模型行**各自校验
  `model_type ∈ MODEL_TYPES`、`platform ∈ PLATFORM_VALUES`（§13.2.9.9）。
  ⚠️ **必填只约束新增**（`PATCH` 的 `access_token` 语义仍是「留空 = 保持不变」，见下）。
- ⚠️ **v1.9 `provider_key` 由系统生成**（D1），**请求体不收该字段**（`extra="forbid"` ⇒ 传了 422）。
  生成算法：`{slug(name)}-{4位随机}`（字母表去 `0/O/1/I/L`），生成后查重 ×8 重试，
  再落 DB 唯一索引兜底（D2 两道防线）。
- ⚠️ **v1.8**：`base_url` **原值落库**（仅 strip），**不做任何 `/v1` 补剥**；缺省值仍为 `DEFAULT_BASE_URL`。

**`PATCH /api/llm/gateway-configs/{id}`**（编辑服务商）
- 改 `access_token` 时**留空 = 保持不变**（防误清）；改 `base_url` 后 `healthy` 自动置 False（需重新探测）。
- ⚠️ **v1.9 `provider_key` 不可编辑**（用户口径「新增后不可编辑」）：`GatewayConfigUpdateRequest`
  **不声明该字段** + `extra="forbid"` ⇒ 请求里传 `provider_key` 一律 **422**。
  ⚠️ 这是**服务端**约束，前端隐藏输入框只是表层（前端可绕过）。
- ⚠️ **v1.9 新增 `platform` 可改**（服务商换适用范围是合理操作），取值校验同上。
- ⚠️ **v1.8**：`base_url` 变更同样**原值落库**（仅 strip）；变更比较用 strip 后的值。

**`DELETE /api/llm/gateway-configs/{id}`**（⚠️ **v1.9 退场**，用户口径「移除删除功能」）
- **保留路由为墓碑**：`include_in_schema=False` + 显式 `410 Gone`
  （`{"code": "LLM_PROVIDER_DELETE_GONE", "message": "服务商已不支持删除，请改用停用"}`）。
  ⚠️ **不回 404** —— 404 无法区分「这个 id 不存在」与「该功能已退场」，排障时会把二者混为一谈
  （同 ADR-021 / 设计 18 的退场惯例）。
- **数据一行不删**（`db.delete` 整段移除）。理由：`provider_key` 已被业务数据引用，删除会让引用悬空。
- ⚠️ **旧口径对照表（被 v1.9 取代，原句保留）**：

| 位置 | v1.8 及更早（旧） | v1.9（现行） |
|---|---|---|
| `DELETE /{id}` 行为 | 真删 + ~~同步清理 `llm_gateway_snapshots` 历史快照~~ | **410 墓碑，不删数据** |
| 停用/开启 | 只有 `enabled` 字段，**页面无入口** | 页面提供**开关**（§11.2） |

**`POST /api/llm/gateway-configs/{id}/test`**（健康探测 + 模型列表）
- 顺序：① `GET {base_url}/models`（带令牌，校验可达 + 取模型目录）→（可选）② 一次极小 chat 请求验证 default_model；
- 结果写 `healthy`/`last_health_at`/`models_json`；不可达 400 `LLM_GATEWAY_UNREACHABLE`；令牌错 401 `LLM_GATEWAY_AUTH_FAILED`。
- 返回 `{id, healthy, message, models: [name...]}`（前端用模型列表填充生成配置下拉）。
- ⚠️ **v1.8**：端点由 `{base}/v1/models` 改为 **`{base_url}/models`**（`/v1` 由用户地址自带）；
  失败 `message` 应含**实际请求 URL**（便于一眼看出是否路径双拼）。

**`POST /api/llm/gateway-configs/models-preview`**（**v1.9 新增**：**未保存**时拉模型目录）
- 用户口径（2026-10-01）：「配置服务商地址和 key 之后是否可以拉取到该服务商下所有模型，
  然后在模型输入框改为下拉框选择」。
- 请求：`{base_url, access_token}`（**不落库、不建行**）；返回 `{models: [name...], message}`。
- 语义 = 「`/{id}/test` 的第 ① 步，去掉持久化」：同样打 `GET {base_url}/models`（同一口径，
  **`/v1` 不硬拼**），同样 401 `LLM_GATEWAY_AUTH_FAILED` / 400 `LLM_GATEWAY_UNREACHABLE`。
- ⚠️ **为什么必须独立于 `/{id}/test`**：新增弹窗里服务商**还没有 id**，拿不到 `{id}`；
  若让用户「先保存再测试再回来选模型」，等于把一个字段拆成三轮操作
  （且中间态会落一条 `default_model` 为占位的脏行）。
- ⚠️ **为什么不落库**：这是**预览**，用户可能取消。落库只在 `POST`（新建）或 `/{id}/test`（已知 id）发生。
- ⚠️ **编辑态也要能用**：编辑时若用户改了 `base_url` 或重填了令牌，须按**表单当前值**重新拉，
  而不是用库里旧值 —— 故本端点的入参是**地址 + 令牌**，而非 `id`。
  编辑态令牌留空（= 不修改）时，前端应传 `id` 让后端**用库里已存的令牌**兜底
  ⇒ 请求体额外支持可选 `id`：`{base_url, access_token?, id?}`，
  `access_token` 为空且给了 `id` ⇒ 用该行的令牌（**同样只读、不落库**）。

**`GET /api/llm/gateway-configs/{id}`**（单条服务商，**v1.9 新增**）
- ⚠️ **路由顺序**：必须注册在 `GET ""`（列表）之后、且与 `/{config_id}/…` 子路径不冲突
  —— 本前缀下 `GET /platforms` 已存在，故 `/{config_id}` 的 `config_id` 必须声明为 `int`，
  否则 `platforms` 会被当作 id 解析（FastAPI 按声明类型不匹配即跳过该路由，但**声明成 str 就会踩**）。
- 用途：编辑弹窗打开时取该行的 `provider_key` / `platform` / 令牌是否存在等**单条详情**
  （列表接口分页 100，深链进编辑不必依赖列表已加载）。
- ⚠️ ~~① `GET {base}/api/status`（New API 可达性）~~ **v1.7 移除**（New API 特化接口，非 OpenAI 标准）。


> 端点合计：服务商配置 **6**（v1.8 的 5 + v1.9 新增平台枚举 1）——
> `GET`/`POST …/gateway-configs`（1 path）、`PATCH`/`DELETE …/{id}`（1 path）、
> `POST …/{id}/test`（1 path）、`GET …/{id}/stats`（1 path）、**`GET /api/llm/providers/platforms`（1 path）**。
> ⚠️ **v1.9**：`DELETE` 退场（`include_in_schema=False`）⇒ **不计入 OpenAPI 的 operations / paths**，
> 故 **+1 端点 / +1 path**（platforms）。
> ~~New API 本身的渠道/令牌/日志由用户在内嵌网关自带 UI（控制台 Tab）操作~~
> —— **v1.7 移除 iframe 控制台**，全部管理能力收敛到项目自有页面（网关内 3 个独立页面，§11）。

### 7.1a 令牌映射（v1.6 新增，⚠️ **v1.9 整节退场**：4 端点 + 表）

> ⚠️ **v1.9（2026-10-01 用户口径「令牌映射页面移除」）**：**整节退场**。原内容保留在下方对照表，
> 不静默删除（AGENTS 硬约束①）。
> **退场理由**：令牌映射的实际作用只有一件 —— 给台账 `token_name` 打一个业务标签
> （`resolve_token_name()` 反查 `llm_gateway_tokens`）。它**不参与任何发给上游的请求**
> （上游认证用的是服务商自己的 `access_token`，见 §3.8），因此它既不控制调用、也不影响计费，
> 只产生一份需要用户手工维护的映射表 —— 维护成本 > 标签价值。
> **v1.9 起**：服务商自身已带服务编码 + `platform`（§3.8），业务维度由「哪个服务商」直接表达，
> 台账 `token_name` 维度失去存在意义。

#### 退场清单（逐项落地）

| 对象 | 处置 | 说明 |
|---|---|---|
| `GET/POST /api/llm/gateway-tokens` | **删除**（`include_in_schema=False` + **410 墓碑**） | 同 DELETE 的退场惯例；`GET` 与 `POST` 各占 1 path |
| `PATCH/DELETE /api/llm/gateway-tokens/{id}` | **删除**（410 墓碑） | 占 1 path |
| `llm_gateway_tokens` 表 | **整表退场**（迁移 drop） | 见 §3.11 |
| `llm_gateway_usage_logs.token_name` 列 | ⚠️ **保留不删** | 历史行不动（审计）；**新行一律落 `""`** |
| `resolve_token_name()`（`llm_gateway_usage_service.py`） | **函数删除** | `record_usage()` 不再查映射，直接落 `token_name=""` |
| `usage/breakdown` 的 `dimension=token` | **从白名单移除** ⇒ 400 | 保留列但不再作为可选项；历史行仍可按其它维度聚合 |
| 前端「令牌映射」页 + 菜单项 | **删除** | 见 §11 |
| `lib/api.ts` 的 5 个 token 请求函数 + 2 个类型 | **删除** | DEF-044：不留死代码 |

- ⚠️ **`record_usage()` 签名不变**（仍收 `platform`/`function` —— 它们是台账的业务维度，与令牌无关），
  只是**不再用它反查令牌名**。调用点（`ai_service` / `llm_gateway_client`）**无需改动**。
- ⚠️ `llm_gateway_client.py` 的模块 docstring 里「platform/function 必须用常量，否则查不到令牌」这条纪律
  **随本表退场而失效**，必须**改写**（留着 = 一条已不成立的操作说明）。

#### 旧口径对照表（被 v1.9 取代，原句保留）

| 位置 | v1.8 及更早（旧） | v1.9（现行） |
|---|---|---|
| 令牌映射表 | `llm_gateway_tokens`（`platform × function → token_name`，唯一 `(user_id, gateway_id, platform, function)`） | **整表退场** |
| 令牌映射端点 | 4 端点 `/api/llm/gateway-tokens` | **4 端点退场（410 墓碑）** |
| 台账 `token_name` | 由 `resolve_token_name()` 反查映射写入 | **恒 `""`**（列保留） |
| 用量查看板的「令牌」拆分 | `dimension=token` 可选 | **移除** |
| 前端 | 独立「令牌映射」页 | **删除** |

### 7.1b 预警规则（v1.6 新增，~4 端点）

**`GET /api/llm/gateway-alerts`**（规则列表）
- 参数：`gateway_id?` / `enabled?` / `page` / `page_size`；返回 `paginated`。

**`POST /api/llm/gateway-alerts`**（新增规则）
- 请求：`{gateway_id, platform?, function?, metric, operator, threshold, window_seconds?, level?, enabled?}`；
  `metric` 白名单 `quota|calls|fail_rate|health`、`operator` 白名单 `gt|gte|lt|eq`（未知 400 `LLM_ALERT_METRIC_UNSUPPORTED`）；返回 201。

**`PATCH /api/llm/gateway-alerts/{id}`**（编辑）
- 请求：`{threshold?, operator?, window_seconds?, level?, enabled?}`；越权 404。

**`DELETE /api/llm/gateway-alerts/{id}`**（删除）

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

### 7.3 素材库（新增，8 端点）

**`GET /api/xhs/materials/folders`**（文件夹列表）
- 返回 `[{id, name, path, enabled, last_scan_at, file_count}]`。

**`GET /api/xhs/materials/folders/{id}`**（单个文件夹）
- 返回单对象（同上字段）；**手动刷新后的轮询判据**（见下 `POST …/scan`）。
- ⚠️ **修订（2026-10-02）**：本端点原写在本节正文里（旧 L1090）却**漏进 §7.6 计数**
  （旧表记「folders 4」，实含本端点应为 5）。按「文档先行」补齐计数：
  素材库 7 → **8**（folders 5 + files 3）；端点合计 35 → **36**。

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

> 端点合计：素材库 **8**（folders 5 + files 3）。

> ✅ **T3 落地记录（2026-10-02）· 数据层 + 服务层（API 端点进行中）**：§3.3 / §3.4 两表 + §6.1
> 扫描/索引/缩略图已实现，落地文件：
>
> - **数据层**：`backend/app/models/material.py`（`MaterialFolder` / `MaterialFile`，
>   唯一约束 `(user_id, path)` / `(folder_id, rel_path)` 逐字对齐 §3.3 / §3.4）+
>   迁移 `backend/alembic/versions/9b8c7d6e5f4a_material_library.py`
>   （`down_revision="f8a9b0c1d2e3"`，幂等 upgrade / 干净 downgrade 已脚本验证）。
> - **服务层**：`backend/app/services/material_scan_service.py`。要点：
>   - `is_safe_material_path()` 六条拦截（空 / 盘符根 / 文件系统根 / 系统目录黑名单前缀 /
>     不存在 / 非目录 / 无读权限）；⚠️ **系统目录判定先于存在性判定** —— `C:\Windows` 这类
>     必然存在的目录必须靠黑名单拦，不能等到 `exists()` 之后；
>   - 缩略图**根目录** `storage/materials/thumbs/{folder_id}/f{folder_id}-{file_id}.jpg`
>     （`thumbs_dir()` 取 `get_settings().storage_dir`，⚠️ **不新增 core 模块**）；
>   - 缩略图 URL 用 **`folder_id` + 文件名**而非原始路径 ⇒ 不把用户本机目录结构暴露进 URL；
>   - **视频降级**：项目无 opencv ⇒ `thumb_url=""` + 尺寸留空（前端显类型图标，不是破图）；
>   - **坏图片降级**：单文件 `except` 吞掉 ⇒ 仍索引、仅无缩略图，**不让一个坏文件废掉整次扫描**；
>   - **增量**：`mtime` 差 < 1.0s 判未变 ⇒ `skipped`；源文件消失 ⇒ 删索引行（用户源文件不动）；
>   - ⚠️ **`file_count` 统计前必须 `db.flush()` + `db.expire_all()`** —— `db.delete()` 只标记，
>     不 flush 会让 count 把已删行数进去（实测差 1，`file_count=1` 而实际 0）；
>   - 后台扫描 `scan_folder_in_background()`：daemon 线程 + **自建 SessionLocal**（请求 Session
>     已关）+ 异常全吞记日志（daemon 线程异常无人接管）。
> - **测试**：`tests/backend/test_material_scan_service.py`（18 用例 / 全 PASS），覆盖路径安全、
>   增量、类型与缩略图、**只读边界 C2（扫描前后源文件字节与 mtime 不变）**、归属隔离。
> - ⏳ **未完成**：§7.3 七端点（folders 4 + files 3）待实现。

### 7.4 生成（新增，~4 端点）

**`POST /api/xhs/product-notes/generate-llm`**（单篇入队）
- 请求：`{platform_account_id, item_id, template_id?, gateway_config_id?, model_name?, media_source, media_options?}`
  - `platform_account_id`：目标账号（门禁：有 creator 会话？**否** —— 生成阶段不需要平台令牌，
    只在校验「该账号属于当前用户」；发布阶段才需要令牌）；
  - `item_id`：商品 `shop_products.item_id`。
    ⚠️ **2026-09-30 口径订正（被 ADR-021 取代）**：原文写「必须在本账号某店铺台账存在」——
    `shops` 表与 `shop_id_fk` 已随 ADR-021 退场，商品行的归属列改为
    `shop_products.platform_account_id`。**现行判据**：按
    `(user_id, platform_account_id, item_id)` 唯一命中台账行（`UniqueConstraint` 同形），
    命中即过；不在该账号台账内 ⇒ 400 `LLM_PRODUCT_NOT_FOUND`。
    端点层把 `item_id` 解析成台账主键（`shop_products.id`）后传服务层 ——
    `generation_records.shop_product_id` **存的是本地主键**（§3.5），不是平台 `item_id`；
  - `gateway_config_id`：上游连接配置（**v1.7 无 New API**）。⚠️ **现行实现**：T6 走
    `llm_gateway_client.resolve_text_model_context()` 取**默认文本模型**（`ai.ModelConfig`
    的 `model_name` + `base_url` + `encrypted_api_key`），`gateway_config_id` 仅作审计留痕
    （写 `generation_records.gateway_config_id`）；台账的 gateway 反查由
    `ai_service.OpenAICompatibleTextClient._complete()` 内部按 `base_url` 匹配完成
    （`llm_gateway_usage_service.resolve_gateway_id`）。**不引入第二套模型上下文**；
  - `model_name?` 可选覆盖默认文本模型的 `model_name`；
  - `media_source`：`ai_generate` / `material` / `product_image` / `reference_image`；
  - `media_options`：素材选中 ids / 参考图 ids / 是否生成封面 / 文生图·图生视频·图文拼图等；
- 门禁：模板存在（给了 `template_id` 但查不到 ⇒ 404 `LLM_TEMPLATE_NOT_FOUND`）/ **有可用的默认文本模型**
  （缺 → 400 `LLM_GATEWAY_NOT_CONFIGURED`）/ 商品台账行存在（缺 → 404 `LLM_PRODUCT_NOT_FOUND`）
  ⇒ 全过 → 入队 `TASK_TYPE_GEN_LLM_ITEM` → 返回 `{ok, status:"queued", shop_product_id, record_id}`；
  ⚠️ **v1.7 取消 `LLM_GATEWAY_UNREACHABLE` 同步探测**：不再在入队前打上游（无外挂进程、无 `healthy` 字段），
  上游不通在 worker 内体现为 `generation_records.status=failed` + `error_code=LLM_GENERATE_FAILED`；
- 幂等：同用户未终态队列已有同 `item_id`（= 台账主键）⇒ `already_queued: true`（不重复入队，
  **也不新建审计行**；回执里的 `record_id` 回填首次入队那条记录）。

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

> ✅ **T6 落地记录（2026-10-01）**：§7.4 四端点已实现，全部委托到
> `backend/app/services/note_gen_llm_worker.py`。要点：
>
> - **模块分工**：本队列（`note_generate_llm`）与一期 `product_note_gen_service`
>   （`note_generate` 队列）**并存不替代** —— 内容来源（prompt 模板两层 vs 内置提示词）、
>   落库表（`generation_records` 三表 vs 无）、产物契约（`local_generated` 草稿 vs 直接发布）
>   三项维度都不同，不是「同一语义两份实现」（DEF-044 不适用）。
> - **入队三入口**：`start_llm_generate_note` / `start_llm_batch_generate` / `start_llm_retry`，
>   复用 `note_queue_service.enqueue_item`（幂等 + 批次懒创建的唯一实现），
>   仅在 `created=True` 时补写额外行体（⚠️ 必须赋**新 dict** —— JSON 列原地改不标脏）。
> - **worker 五件套**（DEF-069 同型）：篇内 `except BaseException` 网 + 循环外层第二层网
>   + 看门狗 + 停止出口；存活判据 = **线程在不在**（`llm_gen_worker_thread_alive()`）。
> - **落库一个事务**：`notes`（`local_generated` + `pending`）+ `note_topics`（`apply_sync_topics`）
>   + `note_assets` + `note_generation_links` + 回写 `generation_records`；
>   ⚠️ `notes.platform_account_id` 是 **NOT NULL** ⇒ 以**台账行** `row.platform_account_id`
>   为权威归属（ADR-021），行体 `account_id` 只作兜底（实测踩过一次 NOT NULL 失败）。
> - **护栏不过 ⇒ 不落半条**（`LLM_GUARDRAIL_BLOCKED`）；失败原子性靠**先 `rollback` 再记失败**。
> - **`generated`/`failed` 计数**按 `finished` 逐项数（**不是** `total - failed` —— `total`
>   含未终态项，减法会把还在跑的算成「已生成」）；快照关联审计行的键是 **`item_id`**
>   （底座 `_entry` 对商品队列不回 `task_id`，照 `task_id` 关联会静默全 miss）。

### 7.5 用量展示（v1.6/v1.7：本地台账聚合，~3 端点）

> ⚠️ **v1.6 取代 v1.3 的「快照 + 手动刷新」方案**：用量从本地 `llm_gateway_usage_logs`（§3.13）实时聚合，
> 不再依赖 `llm_gateway_snapshots` 也不打任何上游管理 API（v1.7 去外挂后更彻底）。

**`GET /api/llm/usage/overview`**（总览：今日/近 7 日/近 30 日，按维度切片）
- 参数：`gateway_id?` / `platform?` / `function?` / `model_name?`；返回 `{today: {calls, success, fail, fail_rate, total_tokens, avg_latency_ms},
  week: {…}, month: {…}, gateway_healthy}`（从台账聚合；无数据日 = 空态，不造假）。
- ⚠️ `model_name`（2026-10-02 新增）：筛选粒度下探到「服务商下具体模型」（台账 `model_name` 列直接过滤）。

**`GET /api/llm/usage/daily`**（按日序列，供折线图）
- 参数：`gateway_id?` / `platform?` / `function?` / `model_name?` / `start_date` / `end_date`；返回 `[{date, calls, success, fail, total_tokens, avg_latency_ms}]`（仅台账有的日期）。

**`GET /api/llm/usage/breakdown`**（维度拆分：按平台/功能/模型分组）
- 参数：`gateway_id?` / `model_name?` / `start_date` / `end_date` / `dimension`（`platform|function|model`；⚠️ `token` 已随令牌映射退场从白名单移除，见 §7.1a）；返回分组聚合，供「平台→功能」两级切片展示。

> 端点合计：用量展示 **3**。~~**明细下钻** = 前端「查看详情」跳 New API `/console/log`（iframe/外链）~~
> —— **v1.7 移除**；明细下钻改为**看板内按维度筛选**（breakdown 已够用，不做逐条明细页）。

### 7.6 端点统计与一期对齐

| 组 | 新端点 | 说明 |
|---|---|---|
| 连接配置 | 5 | gateway-configs CRUD + test（v1.3 新增，v1.7 通用化） |
| 令牌映射 | 4 | gateway-tokens CRUD（v1.6 新增） |
| 预警规则 | 4 | gateway-alerts CRUD（v1.6 新增） |
| 提示词模板 | 8 | modules 4 + templates 4 |
| 素材库 | 8 | folders 5 + files 3（v2.2：补 `GET /folders/{id}`，原漏计） |
| 生成 | 4 | 单/批/队列/重试 |
| 用量展示 | 3 | overview/daily/breakdown（v1.6/v1.7 台账聚合） |
| **合计** | **36** | 连接 5 + 令牌 4 + 预警 4 + 模板 8 + 素材 8 + 生成 4 + 用量 3 = 36 |

- **不引入任何外挂网关**（v1.7）：上游渠道/模型管理由用户在上游侧自理（三方服务商后台，或自托管网关的自有控制台），
  项目侧**不建对应端点、不做内嵌**；
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
| 调用 token/耗时/成败 明细 | ✅ 本地台账落库 | **v1.7 取代**（去外挂化）：权威改为本地 `llm_gateway_usage_logs`（唯一 INSERT 点 `llm_gateway_usage_service.record_usage`），项目侧**自持**；~~「❌ 项目侧不落；New API SQLite（Logs）权威；项目侧只落 `llm_gateway_snapshots` 按日快照」~~ 为 v1.3–v1.6 外挂架构原文，**已被 v1.7 取代**（设计 17 §7.1，2026-09-30） |
| 网关令牌 / 上游渠道密钥 | 🔒 加密落库 | **v1.7 取代**（去外挂化）：两级令牌（平台级 + 功能级）加密存配置表；~~「❌ 项目侧不落；上游渠道密钥只在 New API 库（项目侧不接触）」~~ 为 v1.3–v1.6 原文，**已被 v1.7 取代**（无外挂库，上游凭据由项目侧加密持有） |
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
| **大模型网关页** | `pages/llm-gateway/llm-gateway-page.tsx`（`/platform-select` 网关卡片进入） | **v1.9 起自有 3 个独立页面**（用量看板 / **服务商** / 预警规则），**无 iframe、无 Tabs**<br>⚠️ **v1.9（2026-10-01）**：「连接配置」改称「**服务商**」、「令牌映射」页**整页删除**（4→3）<br>（2026-10-01 页面形态三次修订：先删「用量看板」页签迁独立首页 ⇒ 4→3；随后**独立首页撤销**、完整看板收进本页**第一个页签** ⇒ 3→**4**；最后**取消 Tabs**、4 项改**子路由独立页面**，侧栏成为导航菜单，旧 `?tab=` 重定向） |
| ↳ **用量看板页面**（**第一项**，`/llm-gateway/usage`） | 同上 | **v1.7 完整看板**：总览三卡（今日/近7日/近30日）+ 30 日双轴折线 + **三维**拆分环形（平台/功能/模型）+ 明细表 + 筛选器；内容组件 `pages/llm-gateway/usage-dashboard-panel.tsx`（**只渲染内容，壳层由网关页提供**），见 §11.3<br>⚠️ **v1.9**：四维→**三维**（令牌映射退场，`dimension=token` 移除） |
| ↳ **服务商页面**（`/llm-gateway/gateways`） | 同上 | **v1.9 改名**（原「连接配置」）：`llm_gateway_configs` 增删改（**无删除**）+ 健康探测 + 模型目录<br>**v2.3（2026-10-02）列表改父子树**（用户口径「服务商列表使用父子树，父是服务商，子是服务商下的具体模型」）：父行 = 服务商、子行 = 该服务商 `models` 目录里的每个模型（`Table` 可展开子行，默认全展开）。<br>父行列：服务商名 / **KEY**（只读，可复制）/ **使用平台** / 类型 / 地址 / 默认模型 / 调用量 / 令牌 / 健康 / **启停开关** / 操作（测试、编辑）；子行**只展示模型名**，其余列以 `-` 占位（模型不是独立实体，无编码/地址/令牌等属性，**不复制父行值**）。<br>弹窗（**新增服务商** / **编辑服务商**）：名称* / **使用平台\*（下拉）** / **模型类型\*** / 地址* / **访问令牌\*** / **默认模型\***；KEY **不在弹窗内可编辑**（仅新增后展示） |
| ↳ ~~令牌映射页面~~（`/llm-gateway/tokens`） | ~~同上~~ | ~~platform×function → token_name 映射（v1.6）~~ **被 v1.9 整页删除**（用户口径「令牌映射页面移除」；路由 + 菜单项 + 组件 + `lib/api.ts` 5 个请求函数一并移除），原句保留 |
| ↳ 预警规则页面 | 同上（`/llm-gateway/alerts`） | 阈值规则 CRUD：用量 / 失败率 / 健康（v1.6） |
| ↳ ~~页签形态（`?tab=gateways` 等）~~ | 同上 | ~~单页 + antd Tabs 承载 4 页签，选中态进 URL `?tab=`~~ **被 2026-10-01 §11.3 第三次修订取代**（取消 Tabs ⇒ 子路由独立页面；旧 `?tab=` 仍经 `TAB_TO_PATH` 重定向，深链不死），原句保留 |
| ↳ ~~用量看板页签（早期形态）~~ | 同上（`?page=usage`） | ~~overview 三卡（今日/近7日/近30日）+ daily 折线 + breakdown 分布（平台/功能/模型）~~ **被 2026-10-01 §11.3 三次修订取代**（先迁独立首页 `/usage-dashboard`、随后撤销回归本页第一页签并升级为完整图表看板、最后改子路由独立页面），原句保留 |
| ~~**用量看板（首页）**~~ | ~~`pages/usage-dashboard/usage-dashboard-page.tsx`（`/usage-dashboard`，进站默认落地页）~~ | ~~v1.7 新增独立全屏公共页~~ **被 2026-10-01 §11.3 第二次修订取代**：独立首页与路由**整条删除**（`/` 重定向回 `/platform-select`），看板回归网关页第一页签；`frontend/src/pages/usage-dashboard/` 目录已移除。原句保留 |

> ⚠️ **v1.7 取代 v1.4/v1.5 的「Tabs 双页签 + 全高 iframe 内嵌 New API 控制台 8 页面」**（原句见 §0 v1.4⑨/v1.5⑫）：
> **iframe 整段移除**（那是 8 个控制台页全空白的根因——iframe 内未登录 New API）；页面**不依赖任何外部 UI**。
> ⚠️ **v1.7 取代上表「↳ 用量看板页签（`?page=usage`）」的定位**（2026-10-01 实测决定）：原设计把用量看板做进网关页的
> 一个页签，落地时**提升为独立首页**——用量是运营者**每天都要看**的信息，网关配置是**配一次就不动**的；
> 把高频信息塞进低频页面的页签 = 每次都要点两层。
> ⚠️ **原文（2026-10-01 早间）曾写「网关页内保留一个精简『用量概览』页签（今日四数 + 跳首页链接）」——该方案已于同日
> 当日晚间被用户裁决推翻**：页签**整段删除**（连「精简概览」也不留），网关页改为在 `PageHeader` 右侧放一个
> **「用量看板」跳转按钮**（`navigate("/usage-dashboard")`）。**权威看板 = 首页，且唯一实现 = 首页**
> （避免同一语义两份实现，DEF-044 同型风险 ⇒ 连精简版也不留）。落地实测：网关页 `PAGE_TABS` 4→3。
> ⚠️ **上段「权威看板 = 独立首页」的定位又被同日更晚的第三次裁决取代**（用户口径：「用量看板单独页面，
> 不要放连接配置中」＋实测「网关页里看不到看板」）：结论 = **看板回到网关页，且必须是「第一个页签」**
> （排在连接配置之前），并**带上完整图表**（不再是最初那个「6 个裸数字」的简化版）；**独立首页 `/usage-dashboard`
> 与路由整条删除**、`/` 重定向回 `/platform-select`。用户对「单独页面」的澄清 = **不要塞进连接配置页签里，
> 而是独立成一个顶级页签**，不是新增一个 URL。
> 落地实测：网关页 `PAGE_TABS` 3→**4**（用量看板 / 连接配置 / 令牌映射 / 预警规则）。
> **「唯一实现」如何仍然成立**：看板内容抽为独立组件 `usage-dashboard-panel.tsx`（只渲染内容），
> 壳层（Sider / Header / PageHeader）由网关页统一提供 —— 不存在「独立页壳层 + 网关页壳层」两份实现。
> ⚠️ **第四次裁决（2026-10-01 深夜，现行）**：用户口径 **「网关内，每个菜单均是一个单独的页面不要使用 tab」**
> ⇒ **取消 antd `Tabs`**，4 个菜单改为**子路由独立页面** `/llm-gateway/{usage,gateways,tokens,alerts}`，
> 侧栏 4 项成为**真正的页面导航菜单**（点击切换 + **高亮当前项**），各页独立 URL（可收藏 / 刷新 / 深链）。
> 上段「第一个页签」的表述随之改为「**第一项菜单**」，但**语义（看板排最前、进网关默认落看板）不变** ——
> 进 `/llm-gateway` 经 index 分支重定向到 `/llm-gateway/usage`。**「同一语义只有一份实现」更强地成立**：
> 壳层只有 `LlmGatewayPage`（Layout/Sider/顶栏）+ `GatewaySection`（Header/Content/PageHeader 骨架）一份，
> 4 个页面只是内容组件，看板内容仍是 `UsageDashboardPanel` 唯一实现。原句保留。

### 11.3 用量看板（**网关内第一项菜单**，2026-10-01 三次修订）

**定位**：用量是运营者**每天都要看**的高频信息，网关配置是**配一次就不动**的低频信息 ⇒ 看板放在网关内
**最前面**（第一项菜单），进网关**默认就落在看板**（`/llm-gateway` → 重定向 `/llm-gateway/usage`），无需再点第二层。

**页面形态三次修订**（2026-10-01，按时间序，原句均保留）：
1. **早间**：看板是网关页一个页签，后被**提升为独立首页** `/usage-dashboard`（意图：高频信息不该塞进低频页的页签）；
2. **晚间**：独立首页被**撤销** —— 用户口径「用量看板单独页面，不要放连接配置中」+「网关里看不到看板」⇒
   完整图表看板收进网关页作**第一个页签**，`/usage-dashboard` 路由**整条删除**、`/` 改回 `/platform-select`；
3. **深夜（现行）**：**取消 Tabs** —— 用户口径「网关内，每个菜单均是一个单独的页面不要使用 tab，公用一个页面」
   ⇒ 4 个菜单改**子路由独立页面**，侧栏 = 导航菜单（高亮当前项）。**用户对「单独页面」的最终澄清 = 每个菜单
   一个独立 URL 的页面**（此前误读为「不要塞进连接配置页签里」），故第一/第二次的「页签」形态一并被本次取代。
   落地：`PAGE_ITEMS`（替代 `PAGE_TABS`）4 项 + `GatewayRoutes` 4 条 `<Route>` + `TAB_TO_PATH` 旧链接重定向 +
   `path="*"` 回落默认页（不 404）；`GatewaySection` 提供 Header/Content/PageHeader 共用骨架。

| 区块 | 内容 | 数据源 |
|---|---|---|
| ① 总览卡 | **三段时间并排**（今日 / 近 7 日 / 近 30 日）× 六指标（调用 / 成功 / 失败 / 失败率 / 总 Token / 平均延迟） | `GET /api/llm/usage/overview`（一次请求回三段） |
| ② 趋势折线 | 近 30 日**双轴折线**：调用量（左轴）+ 总 Token（右轴），X 轴日期 | `GET /api/llm/usage/daily` |
| ③ 维度拆分 | **四维可切换**（平台 / 功能 / 模型 / 令牌）：环形图占比 + 图例带数值 | `GET /api/llm/usage/breakdown?dimension=` |
| ④ 明细表 | 切换「按天 / 按模型」两张表：日期或模型 + 调用 / 成功 / 失败 / 失败率 / Token / 平均延迟 | daily 或 `breakdown?dimension=model` |
| ⑤ 筛选器 | 时间区间（RangePicker，默认近 30 日）+ 网关下拉（可空 = 全部网关）+ **模型下拉（级联，选网关后列出其下具体模型；可空 = 全部模型）** | 三端点共用 `start_date`/`end_date`/`gateway_id`/`model_name` |

**图表库 = recharts**（`frontend/package.json` 的 `dependencies` 里 **v3.6.0 已装**，此前无任何页面使用）。
理由：① **零新增依赖**（已在依赖表里，只是没接线）；② React 组件式 API（`<LineChart>`/`<PieChart>`）
与项目「React 19 + antd 6」栈同构；③ 按需 import 具体图表组件，体积可控。

**呈现纪律（沿用项目通用规则）**：
- **失败三分法**：有数据 ⇒ 渲染；无数据 ⇒ **空态（`Empty`，不是错误）**；预期内取不到 ⇒ 一行安静说明；真异常 ⇒ 红块 + 真原因。
- **消息提醒**统一 `App.useApp().message.*`；**禁**静态 import / inline Alert 消息条（inline 只留结构呈现）。
- **只读页**：不提供任何写操作（停用令牌 / 改阈值等仍在网关页）。
- **既有端点，微调筛选参数**：三端点（overview/daily/breakdown）在 T1b 已落地；2026-10-02 为把筛选粒度下探到「服务商下具体模型」，三端点统一新增 `model_name` 筛选参数（台账 `model_name` 列直接过滤），前端补「模型」级联下拉。

---

### 11.2 交互要点

- 生成进度卡复用一期「队列记录页」5s 轮询、无 Tip；停刷判据 `total > 0 且待处理 0`（§35 纪律）；
- 素材面板秒开：列表本地先用 `file_count` + 缩略图 URL 占位，缩略图懒加载；
- 生成结果在内容库可见（`local`+`pending`），可预览编辑、可一键发布（复用一期 `POST /notes/{id}/publish`）；
- **用量下钻**：看板内维度筛选（平台/功能/模型/令牌 + 日期区间），~~跳 New API 自带看板~~（v1.7 移除）；
- **落地入口**：登录后 `/` → `/platform-select`；从平台选择页的网关卡片进入 `/llm-gateway`，经 index 分支**重定向到第一项菜单「用量看板」**（`/llm-gateway/usage`）；看板顶部工具条有「网关配置」按钮一键跳到 `/llm-gateway/gateways`。4 个菜单各有独立 URL，可收藏 / 刷新 / 深链；旧 `?tab=` 链接自动重定向到对应子路由。

---

## 12. 测试要点（阶段二验收前置，补充到 `docs/04_测试文档/07_第二阶段验收测试方案.md`）

| 层 | 用例 |
|---|---|
| 模型 | 9 表建表/约束/唯一索引；归属校验；`material_files` 唯一 `(folder_id, rel_path)`；`llm_gateway_usage_logs` 归属与索引 |
| 迁移 | `upgrade head` 幂等、`downgrade` 可逆；真库前备份（REF §F 纪律） |
| 连接配置 | `/gateway-configs` CRUD + test（健康探测 / 模型目录 / 令牌永不回传 / 改 base_url 置 healthy=false） |
| 模板渲染 | 插槽白名单 / 未知插槽报错 / 默认模板唯一（变异：清旧默认不生效 → 红） |
| 素材库 | 安全路径拦截（C:\Windows / 不存在 / 非目录）/ 增量扫描 mtime / 删除不物理删用户文件 |
| 生成 | 门禁（商品不存在/模板不存在/网关未配置或不健康）/ 幂等 already_queued / 护栏命中不落半条 / 失败记 generation_records / retry 幂等 |
| 队列 | `QUEUE_REGISTRY` assert 自检（新增队列两边同加）/ 快照键名 `generated` / 停刷判据 |
| 用量台账 | **记账器 `record_usage()` 成功/失败都记**（失败含 error_message）/ `usage` 缺失时 token 记 0 不报错 / **记账异常不影响业务调用**（安全网不得条件性）/ overview & daily & breakdown 聚合正确（无数据 = 空态，不造假） |
| 前端 | 网关内 **3 个独立页面（子路由，无 Tabs、无 iframe）**：`/llm-gateway/usage`（第一项，进网关默认落地）/ `gateways` / `alerts`；侧栏 3 项 = 导航菜单（高亮当前项）；旧 `?tab=` 重定向 + `path="*"` 回落默认页；连接配置 + 模型下拉 / 预警规则 / 生成进度卡 5s 轮询无 Tip；**用量看板内容组件 `usage-dashboard-panel.tsx`**（三卡 + 双轴折线 + 四维拆分 + 明细表；只渲染内容，壳层由网关页统一提供）<br>⚠️ **v1.9 修订（2026-10-01）**：**令牌映射页已退场**（端点 410 墓碑 + `include_in_schema=False`，前端删除页面与 `TAB_TO_PATH` 中的 `tokens` 项）⇒ 「网关内 4 个独立页面」**修订为 3 个**（用量看板 / 服务商 / 预警规则）；原「4 个」句已被本行取代，见 §11.4 退场记录 |
| 回归 | 一期 `/model-configs` 与 `/notes` 相关行为零破坏（`test_model_configs.py` / `test_product_note_*.py` 全绿） |

---

## 13. 任务拆分（按本文档落地顺序）

> ⚠️ **v1.7 修订（2026-09-30）**：原 **T0「引入 New API 网关」整条作废**（原句保留于 §15 标题与 §15.4 验收记录）。
> 网关能力**由项目自身提供**，不引入任何外挂进程，故 **T1 不再依赖 T0**（无「下载 exe / 起进程 / 探活」前置）；
> 「test 端点」语义随之收敛为**纯 OpenAI 兼容探测**（`GET {base}/v1/models`），见 §7.1。
> 表中 T4/T7 原写「走 **New API 渠道**」一并订正为「走**上游连接配置**（任意 OpenAI 兼容 base_url）」。

| 阶段 | 任务 | 依赖 | 交付 |
|---|---|---|---|
| ~~T0~~ | ~~**引入 New API 网关**（下载官方 Release exe → 本机 `:3000` 起进程 → 渠道/令牌配置 → 探活）~~ **v1.7 作废**：外挂进程整层取消（§4.8） | ~~无~~ | ~~网关可用，`/v1/chat/completions` 通~~ |
| T1 | `llm_gateway_configs` 表 + 迁移 + 网关配置 CRUD + test 端点（**v1.7：无 T0 前置**，上游 = 一条 OpenAI 兼容连接配置） | 无（v1.7） | 项目侧能连上游、取模型列表 |
| T1b | `llm_gateway_tokens`/`llm_gateway_usage_logs`/`llm_gateway_alerts` 表 + 迁移 + 令牌映射/预警规则 CRUD + 本地台账 | T1 | 令牌两级映射、台账落库、预警规则可配 |
| T2 | `prompt_modules`/`prompt_templates` 表 + 迁移 + CRUD 端点 | T1 | 模板两层结构可管理 |
| T3 | `material_folders`/`material_files` 表 + 迁移 + 扫描/索引/检索 + 安全拦截 | T1 | 素材库多文件夹秒开 |
| T4 | 上游薄客户端（`llm_gateway_client` chat 调用，**OpenAI 兼容直连**）+ 生成 JSON + 护栏 + 话题落库 + 台账记行 | T1/T1b/T2 | 文案生成（干运行）+ 台账 |
| T5 | `generation_records`/`aigc_media_assets`/`note_generation_links` 表 + 迁移 | T2/T4 | 生成审计可追溯 |
| T6 | 生成编排 + 队列注册 + worker + 单/批/队列/重试端点 | T4/T5 | 商品→可发布笔记闭环 |
| T7 | 媒体生成（文生图/图生视频/拼图，走**上游连接配置**）+ 落库 + 溯源 | T6 | 媒体来源四选一 |
| T8 | 监控任务 `system.llm_gateway_monitor`（聚合台账 → 对照规则 → 站内通知）+ 用量页（overview/daily/breakdown） | T1b | 数据监控 + 预警闭环 |
| T9 | 前端：网关设置卡 + 令牌映射卡 + 预警规则卡 + 提示词模板卡 + 素材面板 + 生成配置弹窗 + 进度卡 + **§11.3 用量看板（网关内第一项菜单 `/llm-gateway/usage`，内容组件 `usage-dashboard-panel.tsx`；网关 4 菜单 = 子路由独立页面）** | T2/T3/T6/T7/T8 | UI 闭环 |
| T10 | 测试补全（§12）+ 双脚本 + 同步 AGENTS.md §5 + README | 全部 | 验收可过 |

> ⚠️ **T1b 中 `llm_gateway_snapshots` 已从建表清单移除**（v1.6 起无写入方，v1.7 连数据来源进程都取消）；
> 表作为历史存量保留不删，见 §3.9。

> 每个任务是「文档先行」的一个完整闭环：改契约先改本文档对应 §，再写码。

---

## 13.1 v1.9 服务商化改造（2026-10-01 用户口径）· **决策定稿 + 迁移结论**

> 本节是**实施卡（T2–T6）的唯一执行依据**。D1–D6 六条**已定稿**，实施不得自行改口径；
> 若要改，先改本节并留对照表。

### 需求 → 落地映射（用户 11 条口径）

| # | 用户原话 | 落地 | 卡 |
|---|---|---|---|
| 1 | 连接配置页面新增唯一 KEY | `llm_gateway_configs.provider_key`（**服务编码**，§3.8） | T2 |
| 2 | 新增后不可编辑 | `PATCH` 不声明该字段 + `extra="forbid"` ⇒ 422（**服务端**约束，§7.1） | T3 |
| 3 | 其它业务代码中直接使用该服务商 key 直接调用 | `resolve_provider_context()` + `model_configs.provider_key`（**服务编码**，**§4.3 重写**） | T4 |
| 4 | 连接配置中应是新增服务商 | 页面文案 + 端点标题（路径不变，§7.1/§11.1） | T5 |
| 5 | 移除删除功能 | `DELETE` 退场为 **410 墓碑**，数据一行不删（§7.1） | T3 |
| 6 | 新增停用开启功能 | 页面**启停开关**调 `PATCH {enabled}`（§11.1） | T5 |
| 7 | 编辑网关弹窗→编辑服务商 / 新增网关→新增服务商 | 弹窗标题与按钮文案（§11.1） | T5 |
| 8 | 访问令牌和默认模型必填 | CREATE 校验（400 `LLM_PROVIDER_TOKEN_REQUIRED` / `LLM_PROVIDER_MODEL_REQUIRED`）；**只约束新增**（§7.1） | T3+T5 |
| 9 | 新增使用平台，后端新增枚举下拉选择必选 | `platform` 列 + `GET /api/llm/providers/platforms` 枚举下发 + 前端下拉必选（§3.8/§7.1） | T2+T3+T5 |
| 10 | 令牌映射页面移除 | **4 端点 + 表 + 前端页整体退场**（§7.1a/§3.11） | T2+T3+T5 |
| 11 | 分析需求并建立任务卡跟踪执行 | 看板 `xhs` 的 T1–T8 | — |

### D1–D6 决策定稿

| 决策 | **定稿** | 理由 |
|---|---|---|
| **D1** 服务编码（`provider_key`）生成方式 | **系统生成**，格式 `{slug(name)}-{4位随机}`（字母表去 `0/O/1/I/L`），请求体**不收该字段** | 用户要「不可编辑」+「业务代码直接用」；生成式不会写错/冲突。用户手填则要处理「填了重复/填了非法」两条额外错误路径，收益为零 |
| **D2** 唯一性 | **两道防线**：应用层生成后查重 ×8 重试 + DB 唯一索引 `(user_id, provider_key)` | 与 §35.13 批次号同一纪律；只靠应用层在并发下会漏 |
| **D3** 「使用平台」枚举 | `xhs/douyin/kuaishou/weibo/xianyu/taobao`（与 `api/ai.py` 同源），**后端下发**，**非**台账 `platform` | 用户要「后端新增枚举下拉选择必选」；两个 platform 不同层（§3.8 ⚠️），合并会让「服务商适用范围」与「本次调用的业务平台」互相污染 |
| **D4** 停用行为 | 抛 `LLM_PROVIDER_DISABLED`，**禁止静默回落**其它服务商 | 回落会让台账把 A 的调用记在 B 名下，且用户以为停用生效了 —— 「看起来正常但数据错」是最难查的形态 |
| **D5** `model_configs` 加 `provider_key` 列 | **加**（可空 `String(64)`）；非空走服务商，为空回落旧行为**且不记账** | 一期 `model_configs` 就是「业务持有的那份凭据记录」，让它**指向**服务商即可；否则要在每个生成调用点加参数（多处实现，DEF-044） |
| **D6** 是否保留 base_url 反查过渡 | **不保留**（彻底改按 `provider_key`） | 回落 = 两条真相源，改错了不报错只静默用旧凭据（DEF-048 同型）。存量数据由 `provider_key` 为空时的「跳过记账」自然覆盖，无兼容负担 |

### 迁移结论

- **需要迁移**（不是零迁移）：两处列新增 + 一处列新增 + 一张表退场。
- 迁移内容（**一个新 revision**）：
  1. `llm_gateway_configs` **加列** `provider_key String(64)`（`nullable=False`）+ `platform String(32)`（`nullable=False`）
     - ⚠️ 存量行回填：`provider_key` 按 D1 算法为每行生成（**不留 NULL**）；`platform` 回填 `"xhs"`（存量配置全部服务小红书）
     - ⚠️ 加 **唯一索引** `uq_llm_gateway_configs_user_provider_key (user_id, provider_key)`
     - ⚠️ **`default_model` 存量空值回填**（v1.9 「默认模型必填」的存量面）：v1.9 之前 `default_model`
       允许空串，存量行可能是空 —— 直接留着就与「必填」自相矛盾，且下游 `_complete()` 会因
       缺 model_name 抛 `ValueError`。回填策略 = **按 `models_json` 目录取第一个可用模型**；
       目录也空 ⇒ 落 `PLACEHOLDER`（`"未指定-请在页面补填"`），**不静默留空**
       （留空 = 一条永远调不通的服务商，而用户看不出原因）。用户可在页面「编辑服务商」补正确值。
       ⚠️ 为什么不用「猜一个真实模型名」：那是替用户编造凭据（§5 硬约束 5），
       占位文案能让用户一眼看出「这条需要我补」。
     - ⚠️ `access_token` 存量空值**不回填**（令牌是密钥，猜不得）；改为**可编辑补填** ——
       页面「编辑服务商」即可保存新令牌，无需迁移。
  2. `model_configs` **加列** `provider_key String(64)`（**nullable**，D5）
  3. **drop** `llm_gateway_tokens` 表（§3.11）
     - ⚠️ **不动** `llm_gateway_usage_logs.token_name` 列（历史行审计）
- revision 名：**`d5e6f7a8b9c0`**（`down_revision = "c4d5e6f7a8b9"`），**必须含 downgrade**。
  ⚠️ **实测订正（2026-10-01 实施时）**：设计初稿写 `o6p7q8r9s0t1` / `down_revision = n5o6p7q8r9s0`，
  但盘中实测**当前唯一 head 是 `c4d5e6f7a8b9`**（`c4d5e6f7a8b9_prompt_and_generation_tables.py`，
  链尾 `… → n5o6p7q8r9s0 → b3c4d5e6f7a8 → c4d5e6f7a8b9`）—— 设计稿的 down_revision **被实测取代**。

### 端点 / 表 / 请求函数 增减量（供 T7 逐域回数）

| 域 | 变化 | 说明 |
|---|---|---|
| **端点 / paths** | 令牌映射 **−4 / −3**；`GET /api/llm/providers/platforms` **+1 / +1**；`POST /api/llm/gateway-configs/models-preview` **+1 / +1**（拉模型目录，预览不落库）；`GET /api/llm/gateway-configs/{config_id}` **+1 / +1**（单条详情）⇒ 净 **−1 / 0** | `DELETE /{id}` 与 4 个 `gateway-tokens` 端点均 `include_in_schema=False`（410 墓碑）⇒ **不计入 OpenAPI** |
| **数据表** | **−1**（`llm_gateway_tokens` 退场） | 38 → 37 |
| **前端请求函数** | **−5**（`fetchGatewayTokens`/`createGatewayToken`/`updateGatewayToken`/`deleteGatewayToken`）… 实测 4 个 token 函数 + `deleteGatewayConfig`（删除功能退场后无调用方）⇒ **−5**；**+1**（`fetchProviderPlatforms`）⇒ 净 **−4** | T7 按实测回数 |
| **前端页面** | **不变**（令牌映射是页内子路由，非独立页面文件） | 页面文件数不含子路由 |

⚠️ 以上为**设计预期**，T7 一律以 `curl /openapi.json` + 实测计数为**准**，若与预期不符**先查原因再回写**（不许拿预期值当实测值）。

---

## 13.2 v2.0 业务点 → 服务商映射（2026-10-01 用户口径）· **决策定稿**

> 用户口径（原文）：「小红书中**模型配置移除**，相关逻辑直接切换到项目中大模型的网关中配置的
> 服务商 api，**根据 key 直接调用**，注意 api 状态。」
> 追加口径（同日，**取代**前一条的部分解读）：「服务商为单独配置，**业务中的使用点映射成固定 key**，
> **新增一个模型配置页**，不同平台和不同的业务可以**实时切换服务商**。」
>
> ⇒ **不是删掉模型配置页，而是把它改造为「业务点 → 服务商」的映射页**；
> `model_configs`（一期表，存 base_url + key + text/image）**退场**。

### 13.2.1 三层职责（定稿）

| 层 | 职责 | 落地 |
|---|---|---|
| **服务商层** | 单独配置服务商（地址 / 令牌 / **模型类型** / 默认模型 / 模型目录 / 启停） | `llm_gateway_configs`（v1.9 已就位 + **本版加 `model_type`**） |
| **映射层** | 「平台 × 业务点」→ 服务商，**页面可实时切换** | **新表 `llm_business_providers`** + 模型配置页（改造） |
| **调用层** | 业务代码用**固定 key** 查映射 → 取服务商凭据调用 | `resolve_business_provider(business_key, platform)` |

⚠️ **为什么不用「单一默认服务商」**（v2.0 初稿的 `is_default` 方案，**已被本版取代**）：
用户要的是「**不同平台和不同的业务**可以实时切换」—— 单一默认只能表达「所有业务用同一条」，
无法表达「xhs 的生成用 A、抖音的生成用 B」。映射表是这个需求的**最小**表达。
~~`llm_gateway_configs.is_default` / `image_model` 两列~~ **已放弃**（迁移 `e6f7a8b9c0d1` 作废）。

### 13.2.1a 服务商的**模型类型**（用户 2026-10-01 口径：「同一服务商可能存在不同的模型类型」）

> ⚠️ **被 v2.7 取代（2026-10-02）**：本节把「能力类型」挂在**服务商层**，要求「同一上游要供两种能力
> ⇒ 建两条服务商」。**新口径改为类型下沉到模型层**（`llm_gateway_models` 子表）——
> 服务商唯一、模型行各带 `model_type`。**下文的可扩展性论证仍然成立**（「新能力 = 新增枚举值，
> 零表结构改动」），但它服务的层级从「服务商」变成「模型」。**原句保留如下，见 §13.2.9。**

**一条服务商 = 一种能力**：`llm_gateway_configs` 新增 `model_type String(16)`，取值 `text` / `image`。

- **同一上游要供两种能力** ⇒ **建两条**（同 `base_url`、可同令牌；`name` 与 `provider_key` 各不相同，
  如 `discovery-文本` / `discovery-图像`）。页面按类型分组展示，用户看得清。
- ⚠️ **为什么不用「一条服务商带多个模型名」**（如 `default_model` + `image_model` 两列）：
  那要求每一种新能力都加一列（文本/图像/视频/向量…），**加能力 = 改表结构**；
  且「哪些能力可用」无法在一条记录内表达清楚（一列有值一列空，组合数量爆炸）。
  按类型建条是**可扩展**的：新能力 = 新增一个枚举值，零表结构改动。
- ⚠️ **与 `models_json` 的关系**：`model_type` 是**用户声明**（这条服务商拿来干什么），
  `models_json` 是**上游回报**（`GET /models` 拉到什么）。两者都可能不全 ——
  声明为准（用户知道自己的中转站开了什么），`models_json` 只服务前端下拉候选。

**类型匹配校验（必须做）**：映射时 `business_key` 的 kind（§13.2.2）**必须等于**服务商的 `model_type`
⇒ 不等则 **400 `LLM_BUSINESS_PROVIDER_TYPE_MISMATCH`**（「该业务点是图像类，不能指向文本服务商」）。
⚠️ 不做这个校验的后果：把 `generate_cover` 指到文本服务商 ⇒ 调用时拿文本模型打
`/images/generations`，报一个看不懂的上游错；**提前在校验层拦住，错误才有指向性**。

### 13.2.2 业务点固定 key（**代码常量，用户不可改**）

文本类（打 `POST {base_url}/chat/completions`）：

| key | 中文名 | 对应端点 | 触发源（只读展示） |
|---|---|---|---|
| `generate_note` | 生成笔记 | `POST /api/ai/generate-note`、商品笔记生成链路 | 笔记工坊-草稿-生成草稿；商品列表-商品详情-生成笔记 |
| `rewrite_note` | 改写笔记 | `POST /api/ai/rewrite-note` | 笔记工坊-草稿-改写；自动运营 |
| `generate_title` | 生成标题 | `POST /api/ai/generate-title` | 笔记工坊-草稿-生成标题 |
| `generate_tags` | 生成标签 | `POST /api/ai/generate-tags` | —（无界面入口，仅 API 调用） |
| `polish_text` | 文本润色 | `POST /api/ai/polish-text` | —（无界面入口，仅 API 调用） |

图像类（打 `POST {base_url}/images/generations`）：

| key | 中文名 | 对应端点 | 触发源（只读展示） |
|---|---|---|---|
| `generate_cover` | 生成封面 | `POST /api/ai/images/generate-cover` | —（无界面入口，仅 API 调用） |
| `generate_image` | 生成配图 | `POST /api/ai/images/generate` | 笔记工坊-图片-生成配图；笔记工坊-草稿-生成配图 |
| `describe_image` | 图片理解 | `POST /api/ai/images/describe` | 笔记工坊-图片-图片理解 |
| `generate_product_detail_image` | 生成商品详情图 | **待接**（本轮只登记业务点与触发源） | 商品列表-商品详情-生成商品详情图 |

- ⚠️ **key 是常量**（`BUSINESS_KEY_*`），页面只让用户**选服务商**，不让改 key
  —— 用户改 key 等于让代码里的调用点找不到映射（同 §4.3.3 的 `FUNCTION_*` 纪律）。
- ⚠️ **「文本 / 图像」的区别体现在 key 上**，不体现在配置结构上：
  这正是用户说的「无需区分文本模型或图像模型」—— **不必配两条记录**，
  表格里一行 `generate_cover → 服务商 X` 就够了，用哪个模型由服务商的目录决定。

#### 13.2.2a 触发源（`triggers`，2026-10-01 用户口径）

> 用户口径（2026-10-01）：「**在小红书模型配置页业务入口处加触发源**：商品列表-商品详情-生成商品详情图」
> +「**源列不支持修改**」。

- **语义**：一个业务点**本身就是一个业务触发点**；`triggers` 说明它**会从哪个界面动作被调到**
  （页面路径链，从粗到细，如 `商品列表-商品详情-生成商品详情图`）。
- **只读**：`triggers` 与 `label` 都是**常量元数据**（`BusinessKey.triggers` / `.label`），
  随端点**下发**给页面渲染，**页面不提供编辑入口**——它们镜像的是代码里的调用点，
  用户改不了（与 key 同级：都是调用契约的镜像）。
- **空列表 = 无界面入口**（该业务点当前只由 API 直接调用）：页面**如实渲染**
  「无界面入口（仅 API 调用）」，**不编一个入口出来**（同「缺值不造假」纪律）。
- **一处真相源**：`label` / `triggers` 只写在 `services/business_provider_keys.py` 常量里，
  端点（清单 `business-provider-keys` + 映射行 `business-providers`）与前端都**不另抄一份**；
  此前端点里那份手写 `labels = {...}` 映射表**已删除**（DEF-044：同一语义不留两份实现，
  加了新业务点那份表会静默过期）。
- **新增业务点只改一处**：加一个 `BusinessKey(...)` 常量并加入 `BUSINESS_KEYS` 元组 ⇒
  下发端点、类型校验、页面清单与两列**自动跟进**（映射行按 `BUSINESS_KEYS` 逐条生成，
  未配服务商的业务点也会出现，只是 `provider_key` 为空）。

**本轮新增**：`generate_product_detail_image`（生成商品详情图，kind=`image`）——
与「配图」(`generate_image`) **刻意分开**：配图服务笔记正文，详情图服务**商品**
（电商详情页长图），提示词与尺寸口径都不同 ⇒ 独立业务点可分别挂服务商。
⚠️ 本轮**只登记业务点与触发源**（映射行、页面列、`resolve_business_provider` 可用），
**尚未接调用层**——商品详情里那个「生成商品详情图」按钮是下一张卡的事。

### 13.2.3 新表 `llm_business_providers`（映射，唯一键三段）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | Integer | PK | |
| user_id | Integer | FK users.id, index | 归属 |
| platform | String(32) | 非空 | 业务平台（与 §3.8 的 `PLATFORM_ENUM` 同源） |
| business_key | String(48) | 非空 | 业务点固定 key（§13.2.2 常量之一） |
| provider_key | String(64) | 非空 | 指向 `llm_gateway_configs.provider_key` |
| model_name | String(128) | 默认 "" | **v2.3 新增**：该业务点选用的**具体模型**（必须落在服务商模型目录里）|
| enabled | Boolean | 默认 True | 停用 = 该业务点**回落**到「同平台同 key 的其它行」？**否** —— 见下 |
| created_at / updated_at | DateTime | | |

- **唯一约束**：`UniqueConstraint("user_id", "platform", "business_key")`
  —— 同用户同平台同业务点**只能指向一个服务商**（「实时切换」= 改这一行，不是并存多条）。
- ⚠️ **`provider_key` 不做 DB 外键**：与 §3.8 同理（本项目迁移风格为裸列 + 应用层归属/引用校验），
  且服务商**不可删除**（v1.9 已改 410 墓碑）⇒ 引用不会悬空。
- ⚠️ **未命中映射的回落**（定稿）：**不回落** ⇒ 抛 `LLM_BUSINESS_PROVIDER_NOT_CONFIGURED`
  （「该业务点还没配服务商，请到模型配置页选择」）。理由：回落 = 用错服务商而不报错，
  是最难查的形态（同 D4「停用不静默回落」）。
- ⚠️ **映射命中但服务商停用/不存在** ⇒ `LLM_PROVIDER_DISABLED` / `LLM_PROVIDER_NOT_FOUND`
  —— 承接 v1.9 D4，**仍然不回落**。
- ⚠️ **v2.3：`model_name` 必填（用户口径「两者都应是必须选择，不指定具体模型无法调用成功」）**：
  服务商与具体模型**都必选**。`model_name` 为空 ⇒ 解析层抛 `LLM_BUSINESS_MODEL_REQUIRED`，
  **绝不回落** `default_model`（那是 v2.3 前的静默兜底，已**整体废除**）—— 用户配错要立刻看到，
  而不是调用时才发现用的是另一个模型。`configured` 判据 = `bool(provider_key and model_name)`
  （只选服务商**不算**配好）。
- ⚠️ **`model_name` 目录校验**：PUT 时非空 `model_name` 必须落在该服务商的 `models_json`
  模型目录里 ⇒ 否则 400 `LLM_BUSINESS_MODEL_NOT_IN_CATALOG`。**目录为空时放行**（目录是
  「上游 `/models` 拉到什么」，有些上游不含全部可用模型，硬拦会误伤）。
  清空 `provider_key` ⇒ `model_name` 一并清空（无服务商则无模型可言）。
- ⚠️ **v2.4（2026-10-02，用户实测报错「选择服务商后无法选择具体模型」）**：
  上一条「目录为空时放行」是**后端**校验口径（不硬拦，避免误伤）；但**前端候选下拉**
  若也把空目录服务商当可选，用户在 ② 步**必然选不出模型** ⇒ 卡在 `LLM_BUSINESS_MODEL_REQUIRED`
  且**无法自救**（后端同时要求 `model_name` 非空，用户却无模型可选）。两处口径必须分开看：
  - **后端 PUT**：保持「目录为空时放行」——防上游目录不全被硬拦（不变）。
  - **前端候选**：空目录服务商**置灰不可选**（`disabled` + 后缀 `（无模型目录）`，**与
    `（已停用）` 分开标注真实原因**），**仍在列表可见不隐藏**（用户看得见「它存在但还差什么」，
    才知道去服务商页拉目录 —— 同「缺值不造假」）。
  - 某 `model_type` 下**全部服务商都空目录** ⇒ ① 步下拉**整体置灰** + 一行说明
    「请先去服务商配置拉取模型目录」（引导，**不静默**）。
  - 实现抽为纯模块 `frontend/src/pages/models/provider-options.ts`（`hasNoCatalog` /
    `buildProviderOptions` / `isAllCatalogEmpty`）+ 单测 `provider-options.test.ts`（13 例，
    3 处变异全部 CAUGHT）—— 理由同 §11.1 的 `gateway-tree.ts`：**只有纯函数才能被单测钉住**。
- ⚠️ **v2.5（2026-10-02，用户二次反馈「还是选不了模型」—— 根因在两段式写入时机）**：
  v2.4 只是把「空目录服务商」置灰，**没有解决用户实际踩到的死循环**。实测复现（令牌直打
  `PUT /api/llm/business-providers`）确认根因是**① 步落库时机错误**：
  - ① 步（选服务商）`onChange` 原本**立刻 PUT**（`provider_key=X, model_name=""`）。
  - 但后端契约是**两者都非空**才接受 ⇒ 这次 PUT **必被 400 拒**
    （`LLM_BUSINESS_MODEL_REQUIRED`，错误文案正是「业务点「改写笔记」必须选择具体模型
    （服务商「discovery」）」）。
  - 写入被拒 ⇒ 库内该行 `provider_key` **仍是空** ⇒ ② 步 `disabled = !provider_key` **恒真**
    ⇒ 模型下拉**永远打不开**。
  - 用户视角：*选了服务商 → 弹「必须选择具体模型」→ 模型却选不了* ⇒ **死循环、且不可自救**。
  修正（前后端契约不变，只改前端提交时机）：
  - ① 步选中服务商 ⇒ **只写本地暂存**（`pendingProvider`），据暂存渲染 ② 步的模型目录候选；
    **不落库**（`commitDecision(key, "") === "hold"`）。
  - ② 步选中模型 ⇒ **一次性 PUT（服务商 + 模型齐全）** = 唯一写点，单请求即合法
    （`commitDecision(key, model) === "commit"`）。
  - ① 步**清空** ⇒ 直接 PUT（`provider_key=""` 是合法请求，后端连带清空 `model_name`）。
  - 另修：`allCatalogEmpty` 整体置灰**只在本行未绑服务商时**生效（`isStepOneLocked`）——
    否则已绑空目录服务商的脏行连「清空」都点不了，同样锁死。
  - 已绑定项即使 `disabled` 也**对本行放行**（`isSelectableForRow`），保证用户总能清空/改选。
  - 新增守卫：`commitDecision` / `isStepOneLocked` / `isSelectableForRow`（单测 25 例，
    本轮 4 处变异全部 CAUGHT）。

### 13.2.4 调用链（取代 v1.9 §4.3.1 的 `model_configs` 路径）

```
业务端点（ai.py 8 个 + product_notes.py + note_gen_llm_worker）
  │  用常量调用（如 resolve_business_provider(db, user, BUSINESS_KEY_GENERATE_NOTE, "xhs")）
  ▼
llm_business_providers ──provider_key──► llm_gateway_configs
  │                                        （enabled 校验 + 解密令牌）
  ▼
BusinessProviderContext(base_url, api_key, model_name, provider_id, platform)
  │
  ▼
OpenAICompatibleTextClient._complete()  /  OpenAICompatibleImageClient.generate_image()
```

- **`model_configs` 表**：**退场**（表 + `api/model_configs.py` 6 端点 + `api/ai.py` 的两个
  `_*_model_context` + `services/llm_gateway_client.py::resolve_text_model_context` 的回落分支）。
- ⚠️ **`ai_service` 签名解耦**（v1.9 已定，本版沿用）：`_complete()` / `generate_image()`
  **不再收 `ModelConfig`**，改收 `base_url` + `api_key` + `model_name` 三个标量
  —— ORM 行不该穿透到服务层。
- ⚠️ **`model_name` 从哪来**（**v2.3 更新**）：~~服务商的 `default_model`（§3.8，v1.9 已标必填）；
  业务点若要指定别的模型，由该业务点在代码里传 `model_override`（可选，默认空 = 用服务商默认）。
  本轮**不新增字段**：先跑通「业务点 → 服务商」，模型粒度留到有真实需求再加。~~
  ⇒ **被 v2.3 取代**：`model_name` 是**映射行自己的列**（§13.2.3），用户在模型配置页
  **显式选**（服务商 + 具体模型两级联动下拉），解析层**只读映射行**、**不回落**
  `default_model`。理由（原句保留）：模型粒度**不是「以后再说」而是硬需求** ——
  同一服务商下不同模型质量/价格差异大，业务点必须能各选各的；静默用默认模型 =
  用户以为选了 A、实际跑 B，是最难查的形态。`default_model` 退为「服务商连通性探测」
  用的默认值（`/test` 端点），**不再参与业务调用**。

### 13.2.5 迁移结论

- **需要迁移**（新 revision **`f8a9b0c1d2e3`**，`down_revision = d5e6f7a8b9c0`）：
  ⚠️ 本节原稿写的是 `f7a8b9c0d1e2` —— **该 id 已被 `note_goods_by_platform_ids` 占用**，
  实施时实测发现并改号。教训：**revision id 必须实施前用 `alembic heads` 查占用**，不能凭稿定。
  1. **建表** `llm_business_providers`（§13.2.3）
  2. `llm_gateway_configs` **加列** `model_type String(16)`（§13.2.1a）
     - ⚠️ 存量行回填：按现有语义推断 —— `models_json` 非空且全部像文本模型 ⇒ `text`；
       **判不准时回填 `text` + 让用户到页面确认**（存量两条服务商确实都是文本用途）。
       ⚠️ **不猜 `image`**：猜错会让用户在「模型配置页」看到一条假的图像服务商。
  3. **drop** `model_configs` 表
     - ⚠️ **drop 前先做存量兼容**：每条 `model_configs.is_default=1` 的记录，
       其 `base_url`（归一化）能匹配到该用户某条服务商 ⇒ 为该用户的对应业务点
       建映射（**文本类 key → 该服务商**；图像类 key **不建** ——
       存量服务商按其 `model_type` 是文本，图像映射建了也过不了类型校验）。
       匹配不到 ⇒ **不建映射**（用户到页面自己选；宁可未配置，也不认错服务商）。
  4. ⚠️ **`llm_gateway_usage_logs.token_name` 列保留**（历史行审计，v1.9 既定）。
- ⚠️ **`model_configs` 的 drop 是不可逆的数据删除** ⇒ `downgrade()` 只重建表结构（空表），
  **不还原数据**（同 `llm_gateway_tokens` 的先例）。迁移 docstring 必须写明这一点。

### 13.2.6 端点 / 表 增减量（设计预期，供回数）

| 域 | 变化 |
|---|---|
| **端点** | `api/model_configs.py` **6 端点退场**（`include_in_schema=False` + 410 墓碑）；新增映射 CRUD + 业务点清单下发 **+4 端点 / +3 path** ⇒ **净 −2 / −1** |
| **数据表** | `llm_business_providers` **+1**、`model_configs` **−1** ⇒ **净 0**（37 不变） |
| **前端** | 模型配置页**改造**（不删）：页面文件数不变；请求函数 +3（映射 CRUD）−（`model_configs` 的若干）|

**M2 实测回数（2026-10-01，`create_app().openapi()`）**：

| 项 | T8 基线（v1.9） | M2 后（v2.0） | 差 |
|---|---|---|---|
| paths | 182 | **181** | −1 |
| operations | 216 | **215** | −1 |

- 6 个 `/api/model-configs/*` 端点全部从 schema 消失（`include_in_schema=False`），
  实打仍回 **410 `MODEL_CONFIGS_GONE`** + 结构化 `{code, message}`；
- 新增 `/api/llm/business-providers`（GET / PUT）与 `/api/llm/business-provider-keys`（GET）
  共 **+2 path / +3 operation**，挂在 `business_router`（§13.2.6）。
- ⚠️ 净差为 **−1 path / −1 operation**（设计预期「净 −2 / −1」的端点项按 operation 计对齐：
  6 退场 − 3 新增 = −3 operation，但 schema 里退场的 6 个本就只占 3 个 path
  （`""` / `/{config_id}` / `/{config_id}/test` / `/{config_id}/set-default` 中
   `""` 与 `/{config_id}` 各含 2~3 个 operation）—— 以 `openapi()` 实测值为准）。

⚠️ 以上为设计预期，回数以 `curl /openapi.json` + 实测为准。

### 13.2.7 待实施确认的唯一遗留

**图像服务商从哪来？** 实测现有两条服务商的模型目录**全是文本类**（`discovery` 10 个模型无出图模型），
⇒ 图像类 3 个 key 映射后**仍会在调用时报服务商错**。本版**不阻塞**（映射先建，能配就先配上），
但**图像真正可用**需要用户提供一条支持 `/images/generations` 的服务商。

### 13.2.8 模型配置页**去掉平台下拉**（2026-10-02 用户口径）· v2.6

> 用户口径（原文）：「小红书模型配置页面**只能配置小红书相关业务使用的模型**，
> 把右上角的下拉移除，**不支持切换到其它平台**。」

**改的是前端，不动后端**（**本轮范围，用户已确认**）：

| 项 | 处理 |
|---|---|
| 页面右上角平台 `Select` | **移除**（含 `ReloadOutlined` 之外的这一项；刷新图标保留） |
| 卡内**说明段落**（「服务商在『服务商配置』页单独维护…」） | **一并移除**（用户追加口径：「这里面这段说明也不要了」）—— 它与下方 `Alert` 语义重叠，移除后只留 `Alert` 承载「两者都必选」的关键约束 |
| 页内平台态 | 常量 `XHS_PLATFORM = "xhs"` 固定，**不再有 `setPlatform` / 切换分支** |
| `fetchGatewayPlatforms()` 调用 | 从本页**移出**（页面不再需要平台枚举 ⇒ 少一次请求）。⚠️ 函数与 `/api/llm/providers/platforms` 端点**保留** —— 「服务商配置」页（`llm-gateway-page.tsx`）仍用它填「使用平台」下拉 |
| 后端 `PLATFORM_ENUM` / `?platform=` 查询参数 | **原样保留**（服务商层仍按平台标注适用范围；映射表唯一键仍是 `(user_id, platform, business_key)`） |
| `test_mapping_is_isolated_across_platforms` | **原样保留**（后端跨平台隔离能力未变，测试仍应过） |

**理由**：本项目**当前只有一个业务平台（小红书）**，业务代码的调用点**全部**传 `'xhs'`
（`resolve_business_provider(db, user, BUSINESS_KEY_*, "xhs")`）。页面上那个 6 平台下拉
（`xhs/douyin/kuaishou/weibo/xianyu/taobao`）**是一个入口指向 5 个没有任何业务代码会读的映射** ——
用户切到「抖音」配置一通，业务侧**永远不会读到**（所有调用写死 `xhs`），
这是典型的**「能改但不生效」静默坑**（同 §13.2.3 的「不回落」纪律：宁可没有入口，也不要一个假入口）。

⚠️ **不是删能力，是删假入口**：后端多平台表达力（`PLATFORM_ENUM` + 隔离键）**完整保留** ——
将来真接抖音时，后端**零改动**，只需在前端把 `XHS_PLATFORM` 换回真实平台态 + 恢复下拉。

⚠️ **页面标题沿用「模型配置」**，不改名为「小红书模型配置」：侧边栏菜单项
（`app-shell.tsx` 的「模型配置」）与本页 `PageHeader.title` 是同一语义的两处展示，
改名要同步两处且会影响「服务商配置」页里「请到模型配置页选择」等**跨页文案引用**；
本轮只做用户明确要求的「移除下拉」。**待用户后续要求时再统一改名**（记于此，不静默漏掉）。

---

### 13.2.9 **服务商 / 模型数据分层**：类型与平台**下沉到模型级**（2026-10-02 用户口径）· v2.7

> 用户口径（原文）：「模型网关中的服务商页面，**将服务商和服务商模型的数据才分开**，因为服务商信息是
> 其下的模型**公用的**比如服务商接口 key、调用地址是公共的，**具体模型才需要分文本，多模态，图片，
> 视频类似等等**，你可以调研一下目前市场上的服务商和其下的模块类型，对应的逻辑进行调整，
> **使用平台下沉到具体模型**，当前具体平台配置后同步修改这个参数。」

> 澄清结论（同日问答，用户逐项确认）：
> ① 模型级数据**建子表**落库；
> ② 服务商表的 `model_type` / `platform` **不保留**，用 SQL **修复存量数据**（搬走再删列）；
> ③ 「同步修改这个参数」= **在具体平台下配置该服务商的具体模型时，把当前平台的 platform
>    写进该模型的 `platform`**。

#### 13.2.9.1 被取代的结论（原句保留）

~~§13.2.1a「**一条服务商 = 一种能力**：`llm_gateway_configs` 新增 `model_type String(16)`，
取值 `text` / `image`。**同一上游要供两种能力 ⇒ 建两条**（同 `base_url`、可同令牌；`name` 与
`provider_key` 各不相同，如 `discovery-文本` / `discovery-图像`）。页面按类型分组展示，用户看得清。」~~

**被 v2.7 取代。** 该结论的**可扩展性论证本身仍然成立**（「新能力 = 新增枚举值，零表结构改动」），
但它把**能力**挂错了层级 —— 详见 §13.2.9.3 的对照表。新口径：**服务商唯一，模型行各带能力类型**。

~~§13.2.1 三层职责表「服务商层 | … 地址 / 令牌 / **模型类型** / 默认模型 …」~~
**被 v2.7 取代**：服务商层不再含「模型类型」，该维度移入**模型层**（新增第四层）。

#### 13.2.9.2 为什么改：现状的两个具体症状（实测存量数据）

存量 `llm_gateway_configs` 共 **7 行**（2026-10-02 实测），其中 user 7 的两行是**教科书案例**：

| id | provider_key | name | base_url | model_type |
|---|---|---|---|---|
| 3 | `TEST-TEXT` | test-text | `https://api.example.test/v1` | `text` |
| 4 | `TEST-IMAG` | test-image | **同一个 `https://api.example.test/v1`** | `image` |

同一上游、同一 base_url、同一令牌，**为了表达两种能力被拆成两条服务商**。症状：

1. **凭据重复**：`base_url` 与 `access_token`（本质是同一个 key）填两遍，改一处漏一处 ⇒ 两张行静默不一致。
2. **服务商列表失真**：「服务商」这个概念在页面上退化成「能力条目」，用户看到的是 7 条「服务商」，
   其中真正不同的上游只有 5 个（`127.0.0.1:3000`、`discovery-api…`、`api.example.test`、`127.0.0.1:9`）。
3. **`/models` 目录重复拉取**：同一上游拉两遍模型目录（`models_json` 各存一份）。

#### 13.2.9.3 市场调研结论（用户要求「调研一下目前市场上的服务商和其下的模块类型」）

**行业共识：能力类型是「模型」的属性，不是「服务商/渠道」的属性。**

| 来源 | 组织方式 | 能力类型维度 |
|---|---|---|
| **OpenAI 原生** | provider 一份 key + base_url；能力由**端点**区分 | chat/completions、responses、embeddings、images（生成+编辑）、audio（speech/transcription/realtime）、video（Sora） |
| **OpenRouter** | 一个 provider key 覆盖全部；模型目录**每条模型**带 `output_modalities` | `text` / `image` / `video` / `audio` / `speech` / `transcription` / `embeddings` / `rerank` |
| **New API**（本项目 v1.3 曾选型） | 渠道（channel）持 key + 地址；**渠道下挂多个模型** | Chat、Embeddings、Rerank、Realtime、Image、Audio、Music、Video |
| **API7 / 各兼容层** | provider → models 两级 | 各服务商能力集差异大（OpenAI 最全；Anthropic 仅 chat；Cohere 支持 rerank） |

OpenRouter 目录实测计数（2026-09-12）：Text 445 / Image 52 / Video 29 / Transcription+speech 39 /
Embeddings+rerank 44 / Audio 4 —— **同一个 key 下面挂着多种输出模态的模型**，这是主流形态。

⇒ 用户口径与市场一致：**服务商 = 公共凭据（key + 地址）持有者；能力类型 = 模型属性。**

#### 13.2.9.4 新表 `llm_gateway_models`（模型层，v2.7 新增）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | Integer PK | |
| `user_id` | FK `users.id`，索引 | 归属（多用户隔离，同全项目纪律） |
| `provider_key` | String(64) | **指向 `llm_gateway_configs.provider_key`**（裸列，**不做 DB 外键** —— 同 `llm_business_providers` 的迁移风格，应用层校验归属） |
| `model_name` | String(128) | 模型名（上游 `/models` 里的名字，如 `deepseek-v4-flash-0731`） |
| `model_type` | String(16) | **能力类型**（本项目当前 `text` / `image`；枚举源 `services/business_provider_keys.py` 的 `MODEL_TYPES`，可扩展 video 等） |
| `platform` | String(32) | **使用平台**（下沉自服务商层；枚举源同 `PLATFORM_ENUM`） |
| `enabled` | Boolean | 该模型是否启用（不启用 = 业务点不能指向它） |
| `remark` | String(255) | 备注（可选，用户自填） |
| `created_at` / `updated_at` | DateTime | `shanghai_now()` |

**唯一约束 `(user_id, provider_key, model_name)`** —— 同一服务商下同一模型名只能一行。

⚠️ **模型目录（`models_json`）与模型行（本表）的关系**：

| | 来源 | 内容 | 用途 |
|---|---|---|---|
| `llm_gateway_configs.models_json` | **上游回报**（`GET {base_url}/models`） | 字符串数组，全部**候选**模型名 | 前端下拉候选池 |
| `llm_gateway_models`（本表） | **用户声明** | 用户**显式配置**过的模型行 | 类型/平台判据、业务点匹配、预警模型维度 |

⇒ 目录里**未被配置**的模型**不建行**（用户没表态 ⇒ 不给它猜类型，同 §13.2.1a 的
「不造假 / 不猜 image」纪律）。**用户配一个、建一行**（方案 C 语义：目录留 JSON，被配的才有行）。

⚠️ **`model_type` / `platform` 仍是「用户声明，不是上游回报」**（原 §13.2.1a 的这条纪律**继续有效**）：
上游 `/models` 不返回能力类型（OpenRouter 例外，但非本项目接入口径）⇒ 类型必须由用户标。

#### 13.2.9.5 服务商层与模型层的字段归属（新四层）

| 层 | 职责 | 落地 | 变化 |
|---|---|---|---|
| **服务商层** | 公共凭据与连通性：地址 / 令牌 / 名称 / 健康 / **模型目录** / 启停 | `llm_gateway_configs` | ⚠️ **去掉 `model_type` / `platform` 两列**（v2.7） |
| **模型层**（新） | 单个模型的**能力类型 + 使用平台** + 启停 | **新表 `llm_gateway_models`** | 🆕 v2.7 |
| **映射层** | 「平台 × 业务点」→ 服务商 **+ 具体模型** | `llm_business_providers` | 不变（v2.3 已就位） |
| **调用层** | 固定 key 查映射 → 取凭据 → 按模型调用 | `resolve_business_provider(...)` | ⚠️ **类型匹配改查模型层**（§13.2.9.6） |

**~~`default_model` 保留在服务商层~~** —— ⚠️ **被 §13.2.10（v2.8）取代**：该列**已整列删除**。
~~它是「该服务商没指定模型时的兜底名」，属服务商配置。~~
（原句保留。删除理由见 §13.2.10.2：v2.3 起已无任何回落调用点 ⇒ 是死列。）
⚠️ §13.2.3 v2.3 纪律**不变且被本次强化**：业务点映射**必须显式选模型**，**无任何兜底可回落**。

#### 13.2.9.6 类型匹配校验的落点迁移

原 §13.2.1a 的校验：`business_key.kind` **必须等于**服务商 `model_type`。

**v2.7 改为**：`business_key.kind` **必须等于映射所选模型的 `model_type`**
（查 `llm_gateway_models` 的 `(provider_key, model_name)` 行）⇒ 不等则
**400 `LLM_BUSINESS_PROVIDER_TYPE_MISMATCH`**，错误消息带上模型名（「模型 X 的能力是 text，
而业务点『生成封面』要求 image」）。

**前置条件**：所选模型**必须已配置成行**（否则 400 `LLM_BUSINESS_MODEL_NOT_CONFIGURED`
——「请先到服务商配置页为该模型标注能力类型」）。这是新增的一道校验：v2.3 只要求模型名落在
`models_json` 目录里，v2.7 还要求它在**模型层有类型声明**（否则无法判类型）。

**两处实现必须同步改**（同 `_validate_model_type` 纪律，禁留两份实现）：

| 位置 | 现状 | v2.7 |
|---|---|---|
| `services/llm_gateway_client.py:298-307` | 读 `config.model_type` 比对 `spec.kind` | 查模型行 `model_type` 比对 |
| `api/llm_gateway.py:843/:881/:888` | 用 `config.model_type` 校验映射 PUT | 查模型行 `model_type` 校验 |
| `api/llm_gateway.py:799/:821` | `type_of = {provider_key: config.model_type}` | 回填**模型级**类型 |
| `frontend/src/pages/models/model-config-page.tsx:105` | `providers.filter(p => p.model_type === kind)` | 候选**服务商**不再按类型过滤（服务商无类型）；改为**选完服务商后，模型下拉只列该服务商下 `model_type === kind` 的模型** |

⚠️ 最后一条是**交互口径的实质变化**，需要用户过目：
原来「挑服务商时就按类型筛掉不匹配的」，现在是「服务商不再有类型 ⇒ 都能选；
选完服务商后，**模型下拉按业务点 kind 过滤**」。这更合理 —— 同一服务商可能既有文本模型又有图像模型，
按类型筛服务商反而会把它整条筛掉（现状 `TEST-IMAG` / `TEST-TEXT` 就是被迫拆条的根源）。

#### 13.2.9.7 「平台下沉 + 配置时同步」（用户第 ③ 条口径的精确定义）

> 用户原话：「**再具体平台配置该服务商下的具体模型时，将当前平台的设置为当前服务商下的模型的使用平台**」

落到实现：

- **模型层 `platform` 的写入时机** = 用户在**某个平台上下文**里配置该模型时，
  取**当前平台**写入该模型行。
- 本项目的平台上下文 = 模型配置页的 `XHS_PLATFORM`（§13.2.8 已固定为 `xhs`）
  ／服务商配置页的「使用平台」下拉 ⇒ **配置动作发生在哪个平台，模型就归属哪个平台**。
- **不做隐式跨平台联动**：不因「父服务商 `platform` 变了」去批量改模型的 `platform`
  —— 服务商层的 `platform` 列**已删**（§13.2.9.5），不存在「父值」可联动。
  这是**有意的**：平台是模型的属性，模型各自表态，不继承。
- ⚠️ **`LlmBusinessProvider.platform` 不变**（映射层唯一键仍是 `(user_id, platform, business_key)`）：
  映射的 `platform` = **业务上下文**（业务代码传 `"xhs"`），模型行的 `platform` = **该模型的适用范围**。
  调用时两者**不做相等校验**（映射指向模型即可；模型 platform 是展示与筛选维度）——
  若未来接入第二平台，再决定是否加该校验。⚠️ **本轮不加**，避免把「多平台表达力」做成「多平台硬校验」。

#### 13.2.9.8 迁移结论（`down_revision = b2c3d4e5f6a7`，脚本核验，非凭记忆）

1. **建表** `llm_gateway_models`（+ 唯一约束 `(user_id, provider_key, model_name)` + `user_id` 索引）。
2. **存量回填**（用 SQL 从既有数据反推，**每一条都有来源，不猜**）：

   | 来源 | 回填规则 |
   |---|---|
   | `llm_business_providers` 的 13 行映射 | 每行的 `(provider_key, model_name)` ⇒ 一条模型行；`model_type` = 该映射指向的 `business_key` 的 kind（**可靠**：业务点的 kind 是代码常量，`generate_cover` 必是 image）；`platform` = 映射行的 `platform` |
   | `llm_gateway_configs.default_model` | 若该 `(provider_key, default_model)` 尚未由上面产生，补一条；`model_type` = **该服务商的 `model_type`**（旧列值）；`platform` = 该服务商的 `platform` |
   | 其余 | **不补**（目录里未被任何映射/默认引用的模型，用户没表态 ⇒ 不建行，同 §13.2.9.4） |

   ⚠️ 冲突处理：同一 `(provider_key, model_name)` 若被**不同 kind** 的映射引用（理论上不该发生，
   因旧校验已保证 kind = 服务商 model_type），以**服务商旧 `model_type`** 为准并**留迁移告警日志**
   （不静默丢；本项目历史上这类「不一致存量」正是靠日志发现的）。
3. **删列**：`llm_gateway_configs.model_type`、`llm_gateway_configs.platform`
   （SQLite：`batch_alter_table` 重建表，项目已有先例）。
4. **降级（downgrade）**：建回两列 + 从模型层**取每个服务商的第一条模型行**回填
   （取不到则 `text` / `xhs`，`text` 是 §13.2.1a 定的安全默认）。

⚠️ **迁移是破坏性的**（删列），上生产前**必须**用库副本三向实测（升级 → 校验 → 降级 → 再升级），
同项目迁移纪律（`REF §F`）。

#### 13.2.9.9 端点增减量（供 T7 逐域回数）

| 端点 | 变化 |
|---|---|
| `POST/PATCH /api/llm/gateway-configs` | 入参**去** `model_type` / `platform`；**加** `models: [{model_name, model_type, platform, enabled, remark}]`（创建时一并落模型行；PATCH 时**只增改不删**，删除走模型端点） |
| `GET /api/llm/gateway-configs` | 出参 `models` 由「字符串数组」升级为**结构数组**（含 type/platform/enabled）；`model_type` / `platform` **移除** |
| 🆕 `GET/POST /api/llm/gateway-configs/{id}/models` | 列 / 增该服务商下的模型行 |
| 🆕 `PATCH/DELETE /api/llm/gateway-configs/{id}/models/{model_id}` | 改（类型/平台/启停）/ 删模型行 |
| `GET /api/llm/business-providers/keys` | `provider_model_type` 语义变化；新增**模型级候选**下发 |
| `POST /api/llm/gateway-configs/models-preview` | 不变（拉上游目录）；⚠️ 可对齐标注「哪些已在模型层配置过」 |

⇒ **净增 4 端点**（2 组 models 子资源）；`llm_gateway_configs` 净增 1 张表。

#### 13.2.9.10 本轮不做

- **不加 video / audio / embeddings 等类型值**：`MODEL_TYPES` 的**可扩展性**已验证（新能力 = 新增枚举值），
  但本轮**只迁移既有 `text` / `image`**，不趁机扩类型（避免范围蔓延；用户提到的「视频」在 §13.4.4 另有安排）。
- **不做模型行的批量导入**：目录 179 个模型（`discovery` 实测）逐个手配不现实，但**批量一键按名猜类型**
  会违反「不猜」纪律 ⇒ 本轮**手动配**；将来若痛点明确，再设计「批量标注」交互（用户显式确认每类）。

---

### 13.2.10 服务商 `default_model` 列删除 · v2.8（2026-10-02 用户口径）

#### 13.2.10.1 用户口径（原句保留）

> 「**不指定默认模型，该字段移除**」

三个次生决策（`AskUserQuestion` 逐项确认）：

| 议题 | 用户选择 | 落地 |
|---|---|---|
| 测试连通性端点用哪个模型 | **改用模型行的第一个模型** | `POST /{id}/test` 取该服务商**首条启用模型行**，无则退回极小 chat 探测 |
| 存量 `default_model` 数据 | **删列 + 用迁移补齐模型行（推荐）** | 迁移 `f6a7b8c9d0e1`：先按 `(provider_key, default_model)` 补模型行，**再**删列 |
| 前端表单/列 | **直接移除输入框与列** | 弹窗改「模型行列表」编辑器；表格删「默认模型」列 |

#### 13.2.10.2 为什么删：它自 v2.3 起就是死列

| 判据 | 结论 |
|---|---|
| 调用链是否读它 | **不读** —— `resolve_business_provider`（v2.3 起）**只认映射层的 `model_name`**，明确**不回落** `default_model` |
| 写它是否有用 | **无用** —— 写了没有任何读点；改了不改变任何调用行为 |
| 唯一残留用途 | v1.9 的「测试连通性」顺手用它做探测模型名 —— 属**探测参数**，不是业务语义 |

⇒ 结论：一个**无读点的可写列** = 死字段。保留它只会误导（使用者以为配了它就有默认行为），
与 §13.2.9 的「信息下沉到模型层」方向也矛盾（模型能力现已全在 `llm_gateway_models`）。

#### 13.2.10.3 端点与出参变更

| 端点 | 变化 |
|---|---|
| `POST/PATCH /api/llm/gateway-configs` | 入参**去** `default_model`（`extra="forbid"` ⇒ 传了 422） |
| `GET /api/llm/gateway-configs` | 出参**去** `default_model`；`catalog`（原 `models` 字符串数组）= 上游候选池 |
| `POST /api/llm/gateway-configs/{id}/test` | 探测模型改取**首条启用模型行**；该服务商无模型行 ⇒ 退回极小 chat 探测（不报 500） |

⚠️ **`models` / `catalog` 分工**（§13.2.9.4 已定，本次未改）：`models` = 已配置模型行（结构数组），
`catalog` = 上游候选池（字符串数组）。前端模型下拉读 `models[*].model_name`，**不读 `catalog`**
（候选池里的名字在 v2.7 起无法调用 ⇒ 400 `LLM_BUSINESS_MODEL_NOT_CONFIGURED`）。

#### 13.2.10.4 迁移（`down_revision = e5f6a7b8c9d0`，脚本核验）

| 步 | 动作 |
|---|---|
| ① | 遍历 `llm_gateway_configs`，对 `default_model` 非空者，按 `(user_id, provider_key, default_model)` upsert 一条模型行 |
| ② | 模型行的 `model_type` / `platform` 取该服务商的旧列值（`model_type` 缺 ⇒ `text`；`platform` 缺 ⇒ `xhs`，§13.2.1a 安全默认） |
| ③ | 全部补完后 `DROP COLUMN default_model` |

⚠️ **顺序不可颠倒**：必须先补模型行再删列 —— 反了就没法从旧列取类型/平台。
⚠️ **破坏性迁移**（删列）：上生产前用库副本三向实测（升级 → 校验 → 降级 → 再升级），同 `REF §F`。
✅ 已落地真实库 `data/spider_xhs.db`，head = `f6a7b8c9d0e1`；备份 `data/spider_xhs.db.bak-20261002-pre-v28-dropdefaultmodel`。

#### 13.2.10.5 本轮不做

- **不给模型行加「是否默认」标志**：用户口径是「**不指定默认模型**」——
  引入 `is_default` 等于换个名字把同一个概念加回来，与口径相反。
- **不做多模型批量删除端点**：模型行删除走 `DELETE /{id}/models/{model_id}`（§13.2.9.9），
  前端「模型行列表」的删除 = 逐条调用，不满批。

---

### 13.2.11 模型全生命周期自动化 · v2.9（2026-10-02 用户口径）

> **一句话**：模型**不再手输**——配置服务商时**自动拉取三方目录**、用 **AI 归纳每个模型的类型**、
> **父子树展示**；类型/平台**下沉为多值**、AI 只给**初始值**、用户可就地改；前端**父子各自表头**。

#### 13.2.11.1 用户口径（原句保留，逐条）

| # | 原句 | 落地 |
|---|---|---|
| ① | 「**服务商模型不允许手动添加，只能拉取三方**」 | 弹窗内 `Form.List name="models"` 手动编辑器**整段移除**；模型行只由**同步**产生 |
| ② | 「**添加服务商时默认拉取一次**，操作列新增**同步服务商模型**按钮」 | 创建端点内联同步一次（响应带 `sync` 摘要）；操作列新增「同步服务商模型」（`POST /{id}/sync-models`）；两者共用 `_sync_provider_models_core` |
| ③ | 「模型类型**图文**，改回**图片**」 | `MODEL_TYPE_LABELS` 的 `image` 中文 = **「图片」**（非「图文」） |
| ④ | 「模型类型列表中**支持修改，下拉框**」 | 子表「模型类型」列 = 就地 `Select`（选项读后端下发），改动即 `PATCH` |
| ⑤ | 「配置服务商时**直接父子树展示**该服务商下所有模型」 | 列表按服务商展开，展开行 = 该服务商的模型清单 |
| ⑥ | 「模型类型由 **AI 调用总结**（text/image/multimodal/video）」 | `model_type_classifier.classify_model_types` 批量归纳（**不猜名**，走 AI 语义判断） |
| ⑦ | 「**多平台共用同一模型**」 | `llm_gateway_models.platform` 由单值 → **多值数组** |
| ⑧ | 「列表的**地址太短了**」 | 父表「模型调用地址」列加 `ellipsis`、去写死窄宽（自适应） |
| ⑨ | 「**父子表使用各自的表头**」 | 由「单表 + `children` 共用列」改为「父表 + `expandedRowRender` 独立子表」，**两张表各带表头** |

#### 13.2.11.2 类型枚举扩到四值（`text` / `image` / `multimodal` / `video`）

- 后端 `MODEL_TYPES = ("text", "image", "multimodal", "video")`，中文标签 `MODEL_TYPE_LABELS`
  （**文本 / 图片 / 多态 / 视频**）；经 `GET /llm/business-provider-keys` 的 `model_types` **下发**。
- 前端 **零硬编码**：`fetchBusinessProviderKeys()` → `modelTypeOptions`，选项文案随后端变。
- ⚠️ **枚举未加载 / 值不在枚举内 ⇒ 原样显示枚举键**（缺值口径：**不造名、不猜**）；
  旧 `text`/`image` 两值数据**原样可读**（新值只是**新增**，不是替换）。

#### 13.2.11.3 `platform` 由单值 → 多值（多平台共用）

- `llm_gateway_models.platform` 语义：**一个模型可被多个平台共用** ⇒ 出参为 `string[]`。
- 父表「覆盖平台」= 其模型行 `platform` 的 **flatMap 摊平 + 去重**（聚合只读）；
  子表「使用平台」= 逐值打标签（**列级多值**，这是逐模型看平台的唯一入口）。
- ⚠️ 与 §13.2.6「模型配置页固定 `xhs`」不冲突：页内仍固定 `xhs`，多值是**数据能力**（一期就把结构放开）。

#### 13.2.11.4 同步链路（`_sync_provider_models_core` 唯一实现 —— 守 DEF-044）

```
拉上游目录 (discovery) → AI 批量归纳类型 (classify_model_types)
   → 按 (user_id, provider_key, model_name) upsert 模型行 → 返回 sync 摘要
```

| 触发点 | 落点 | 说明 |
|---|---|---|
| **创建服务商** | `POST /llm/gateway-configs` 内联跑一次 | 响应体带 `sync`（`ok/reason/catalog_count/created/updated/classified/total`） |
| **列表操作列按钮** | 🆕 `POST /api/llm/gateway-configs/{id}/sync-models` | 恒 **200**，成功与否看 `sync.ok`（网络/令牌问题如实说，不当异常抛） |

- ⚠️ **同步「没成功」不是异常**：接口恒 200 ⇒ 前端据 `sync.ok` **分流**成功/警告提示。
- ⚠️ **两条路径共用同一个函数**（`_sync_provider_models_core`）—— 禁各写一份（DEF-044）。
- ⚠️ **编辑服务商不触发同步**：`PATCH` 不传 `models`（避免整表替换**冲掉用户手改的类型**）。
  ⇒ 「拉目录」只由**服务商本体**的两个明确动作发起，编辑动作不隐式覆盖。

#### 13.2.11.5 前端列表：父子**各自表头**（v2.9 末次口径）

| 表 | 列（表头） |
|---|---|
| **父表**（服务商，`providerColumns`） | 服务商名称 / 服务编码 / 覆盖平台(聚合) / 模型类型(聚合只读) / 模型调用地址(`ellipsis`) / 调用量 / 令牌 / 健康 / 启用 / 操作(测试·**同步服务商模型**·编辑) |
| **子表**（模型，`modelColumns`） | 模型名称 / **模型类型(就地 `Select`)** / 使用平台 / 启用 / 备注 |

- **结构**：父表 `Table` 的 `expandable.expandedRowRender` **渲染另一张子表 `Table`**（`showHeader` = 各自表头）。
  分叉点**收敛在一处**；`rowExpandable: isExpandableProvider` ⇒ 空目录**不出现**「点了没反应」的箭头。
- ⚠️ **被取代的形态（原句保留）**：v2.7–v2.8 为「**单张** `Table` + `children` 展开子行，**父子共用一套列**」。
  该形态下子行必须沿用父表列，**服务商专有列处处显示 `-`**、表头也是服务商语境的 ⇒ 已按用户口径改为**双表**。
- ⚠️ **父表删掉「启停」列**（下沉到子表）：启停是**模型级**属性，挂在服务商行上会含混「这一格描述谁」。
- ⚠️ **类型列可编辑只在子表**：父表类型 = 聚合**只读**；就地改类型在子表（`changeModelType` → `PATCH`）。
  - 无 id 的行（理论上不出现）**只读展示** —— 宁可不给入口，也不给**点了没反应**的入口。
- ⚠️ **子表无「停用中」视觉陷阱**：`enabled` 用 Tag 明确「启用/停用」二态。

#### 13.2.11.6 端点增减量（供逐域回数）

| 端点 | 变化 |
|---|---|
| `POST /api/llm/gateway-configs` | 响应**新增** `sync` 摘要（创建即同步一次） |
| 🆕 `POST /api/llm/gateway-configs/{id}/sync-models` | 拉目录 + AI 归纳 + upsert，恒 200（成功看 `sync.ok`） |
| `PATCH /api/llm/gateway-configs/{id}/models/{model_id}` | 复用于**就地改类型**（校验 `model_type ∈ MODEL_TYPES`、`platform` 多值）；**不新开端点**（守 DEF-044） |
| `GET /api/llm/business-provider-keys` | `model_types` 下发**四值**枚举 + 中文标签 |

⇒ **净增 1 端点**（`sync-models`）；**无表结构改动**（`platform` 多值仅语义与序列化变更）。

#### 13.2.11.7 本轮不做

- **不做「模型行批量删除 / 批量标注」**：删除仍逐条走 `DELETE /{id}/models/{model_id}`；
  批量猜类型违反「不猜」纪律（同 §13.2.9.10）。
- **不在前端做类型推断**：AI 归纳**只在后端**（`classify_model_types`），前端只消费结果 + 允许手改。

---

### 13.2.12 模型级健康 + 自然序号 ID + 在用不可停用 · v2.10（2026-10-02 用户口径）

> **一句话**：**健康与启用下沉到模型维度**（服务商行不再持有），模型用**服务商内自然序号**做 ID，
> **被业务点引用的模型不许停用**；未探测的模型行给一个「**检查**」按钮，点了就探测并回写整组。

#### 13.2.12.1 用户口径（原句保留，逐条）

| # | 原句 | 落地 |
|---|---|---|
| ① | 「**健康状态和启用状态开关放具体模型维度**」 | 父表**删**「健康」「启用」两列；子表**加**「健康」列 + 「启用」`Switch` |
| ② | 「**具体的模型也生成一个唯一ID，使用自然序号**」 | `llm_gateway_models.model_key`：**服务商内** `1/2/3…`（唯一键 `(config_id, model_key)`） |
| ③ | 「**子表健康列未检查的展示检测检查按钮，触发可健康检查**」 | 未探测（`last_health_at is None`）时渲染「检查」`Button`（非死标签），点击触发探测 |
| ④ | 「**被使用中的模型不支持停用**」 | `PATCH` 停用在用模型 ⇒ **400** `LLM_MODEL_IN_USE_BY_BUSINESS` |

#### 13.2.12.2 数据层（迁移 `b8c9d0e1f2a3`，revises `a7b8c9d0e1f2`）

`llm_gateway_models` 新增三列（**零新表、零新端点**）：

| 列 | 类型 | 语义 |
|---|---|---|
| `healthy` | `BOOLEAN NOT NULL DEFAULT 0` | 最近一次探测是否健康（**默认 False** —— 「从未探测」与「探测失败」都落在 False，靠 `last_health_at` 区分） |
| `last_health_at` | `DATETIME NULL` | 最近一次探测时间；**`NULL` = 从未探测**（前端据此显示「检查」按钮） |
| `model_key` | `INTEGER` | **服务商内自然序号**（1/2/3…），回填为「按 `id` 升序在 `config_id` 内计数」；唯一键 `(config_id, model_key)` |

- ⚠️ **`healthy` 默认 False + `last_health_at` 判据**：网关预警的 `health` 指标判据同步为
  `last_health_at is not None AND not healthy`（**不判「从未探测」就会误报每个新建服务商**）。
- ⚠️ **回填必须服务商内唯一无重号**：迁移后逐服务商核验（本机 24 行全部回填、无空 `model_key`）。
- ⚠️ **`model_key` 是展示/引用用的自然序号**，**不是主键**：主键仍是自增 `id`；
  `model_key` 在**同一服务商内**稳定（删除中间模型后**不重排**，避免引用错位）。

#### 13.2.12.3 「检查」按钮与探测粒度（服务商级探测写回整组）

- **探测粒度 = 服务商**：连通性由**凭据 + 调用地址**决定（与具体模型无关）⇒
  `POST /api/llm/gateway-configs/{id}/test` **一次探测成功后，把该服务商下所有模型行**一并写
  `healthy=True / last_health_at=now`（整组状态一致，不逐模型空转）。
- **子表「健康」列**：`last_health_at is None` ⇒ 渲染「检查」`Button`（`type="link"`，`loading` 绑当前行）；
  已探测 ⇒ 显示「正常 / 异常」`Tag`（按 `healthy`）。
- ⚠️ **不给死标签**：「未探测」若只显文案，用户没有任何下手的地方 ⇒ 必须给**可点入口**（同类：
  §13.2.11.5「宁可不给入口，也不给点了没反应的入口」）。
- ⚠️ **探测失败也写 `last_health_at`**（只是 `healthy=False`）：否则永远停在「未探测」⇒
  用户会以为「没点成功」而反复点（与「不造假」不冲突：时间是真的、结果是真实的失败）。

#### 13.2.12.4 在用的模型不可停用（400 `LLM_MODEL_IN_USE_BY_BUSINESS`）

- **判据**：`llm_business_providers` 中存在 `(user_id, provider_key, model_name)` 三字段都命中的行
  ⇒ 该模型正被业务点引用 ⇒ **禁止停用**。
- **只拦「停用」**（`payload.enabled is False and row.enabled`）；**启用不拦** ——
  启用是在**修复**「业务点指向已停用模型」的死配置，拦它只会把用户锁死。
- **响应**（结构化 400，前端据此给 Tooltip）：

```json
{ "code": "LLM_MODEL_IN_USE_BY_BUSINESS",
  "message": "模型「xxx」正在被 2 个业务点使用（生成笔记、改写笔记），请先在模型配置页改到别的模型再停用",
  "usages": [{ "business_key": "...", "label": "...", "kind": "...", "platform": "..." }] }
```

- ⚠️ **守卫在服务端、不在前端**：前端 Switch 置灰只是**提前告知**（`usages.length > 0` ⇒ `disabled` +
  Tooltip），真正拦截必须是后端 400（前端可被绕过）。
- ⚠️ **`usages` 随列表下发**：`_load_models()` 一次查完该用户全部 `LlmBusinessProvider`，按
  `(provider_key, model_name)` 建字典 ⇒ 每个模型行附 `usages`（缺则 `[]`）；
  **不在前端各页面重复统计**（DEF-044）。

#### 13.2.12.5 端点 / 表 / 请求函数增减量（供逐域回数）

| 项 | 变化 |
|---|---|
| 端点 / path | **±0**（复用 `GET /models-tree` 与 `PATCH /{id}/models/{model_id}`，无新端点） |
| 表 | **±0**（`llm_gateway_models` 加三列，迁移 `b8c9d0e1f2a3`） |
| 前端请求函数 | **±0**（子表「检查」复用 `testGatewayConfig`，不新增函数） |

⚠️ 文档声明的 `230 / 191 / 165` 由 **v2.9** 落地（v2.10 无增减）；此前声明停在 `229 / 190 / 164`
（v2.9 漏回写）⇒ 本轮一并订正。

### 13.2.13 服务商 / 模型列改二级联动 + 修掉取数端点 · v2.11（2026-10-02 用户口径）

#### 13.2.13.1 用户口径（原句保留）

> 「模型配置列表的当前服务商 / 模型列，服务商和服务商模型**应使用二级联动组件**展示，
> 且**支持修改**，目前**全部无法修改**服务商和模型」

拆成三条要求：① 该列要**一个**服务商→模型的**二级联动**控件（不是两个独立的 `Select`）；
② 该控件**可改**；③ 修掉「**全部无法修改**」这个现象。

#### 13.2.13.2 根因：取数端点用错（`models` 恒空的地雷）

⚠️ **同一个 `_serialize_config()`，两个端点语义不同**：

| 端点 | 序列化调用 | `models` 实得 |
|---|---|---|
| `GET /llm/gateway-configs` | `_serialize_config(config)`（**不带** `models` 实参） | **恒为 `[]`**（默认 `None ⇒ []`） |
| `GET /llm/gateway-configs/models-tree` | `_serialize_config(c, models_of.get(c.provider_key, []))` | **真带该服务商的模型行** |

页面原来调前者 ⇒ 前端 `candidatesFor()` 里「按 `p.models.length > 0` 过滤候选」把**每一个服务商都丢掉**
⇒ 候选集恒空 ⇒ 两个 `Select` 的 `disabled` **恒真** ⇒ 服务商和模型**都改不了** —— 与用户口径逐字吻合。

**结论**：改用 `fetchGatewayModelsTree()`（`GET /llm/gateway-configs/models-tree`）。
⚠️ 该地雷已在 `usage-dashboard-panel.tsx` 注释里记录过，本页是**最后一处**仍在用错端点的页面
（与「同一语义不留两份实现」同型：两个端点看着像孪生，实际一个不填 `models`）。

#### 13.2.13.3 二级联动：单个 antd `Cascader`

- **一级 = 服务商**（`buildProviderOptions`；该类型下无可用模型的置灰并**标注原因**，**仍可见不隐藏** ——
  隐藏会让用户以为「服务商没了」，与「缺值不造假」不符）。
- **二级 = 该服务商在该业务点下的可用模型**（`buildModelOptions`，等价于后端 `_type_satisfies` 的判据）。
- **`changeOnSelect`**：允许只选到一级。此刻**只落本地暂存**（`pendingProvider`），**绝不落库** ——
  后端要求「服务商 + 模型都非空」才收，中间态 PUT 必 400 ⇒ 落库不进去 ⇒ 二级永远打不开 ⇒
  死循环（v2.5 根因）。守卫仍是 `commitDecision(providerKey, modelName)`：两者齐 ⇒ `commit`；
  只服务商 ⇒ `hold`；都空 ⇒ `commit`（清空）。
- **`allowClear`**：清空 ⇒ 一次 PUT 把该行 `provider_key` / `model_name` 置空（合法路径）。
- **`showSearch`**：一级 / 二级 label 均可模糊匹配；`displayRender` 用 ` — ` 连接两级。
- ⚠️ **「库里存的模型已不可用」（被停用 / 改类型 / 删了）⇒ 不静默清空**：`isModelMissing()` 如实提示，
  当前值**退回只显一级**（不给假二级值）。用户没动它却悄悄抹掉，是最难查的那种（同「缺值不造假」）。

#### 13.2.13.4 类型匹配：前端镜像后端 `_type_satisfies`

前端原用裸 `(m.model_type || "text") === kind`（**严格相等**），会把 `multimodal` 模型从 text/image
业务点**静默滤掉**，而后端是**收**的（`multimodal` 可服务 `text` 与 `image`）。
⇒ 新增 `typeSatisfies(kind, modelType)`，**逐字镜像**后端
`services/llm_gateway_client._type_satisfies`：`text` 认 `{text, multimodal}`、`image` 认 `{image, multimodal}`、
`video` 只认 `{video}`（**方向性严格**：text 模型不能服务 image）。
规则全部**下沉到 `provider-options.ts` 纯函数**（`typeSatisfies` / `usableModels` / `buildModelOptions` /
`isModelMissing` / `hasNoCatalog` / `isAllCatalogEmpty` / `buildProviderOptions`），页面只做渲染与提交。

#### 13.2.13.5 端点 / 表 / 请求函数增减量（供逐域回数）

| 项 | 变化 |
|---|---|
| 端点 / path | **±0**（改用既有 `GET /models-tree`，无新端点） |
| 表 | **±0**（无迁移） |
| 前端请求函数 | **±0**（改用既有 `fetchGatewayModelsTree`，不新增函数） |

⚠️ 文档声明的 `230 / 191 / 165`（v2.9 落地）**本轮不变**。

---

## 13.3 v2.1 业务点「触发源」+ 商品详情图链路（2026-10-01 用户口径）· **决策定稿**

### 13.3.1 用户口径与两轮纠正（原句保留）

1. **第一轮**（08:xx）：「在小红书模型配置页 7 个业务入口处加触发源：商品列表-商品详情-生成商品详情图」
2. **第二轮纠正**（关键）：「入口源是**只业务触发的点**」——
   ⇒ `triggers` **不是**「界面上哪个按钮调它」的说明文字，而是**这个业务点本身是一个业务触发点**；
   且「源列**不支持修改**」（只读展示）。
3. **第三轮纠正**：「**仅 API 调用** 向上排查前端触发得触发该逻辑得入口」——
   ⇒ 标「无界面入口」之前**必须向上追到前端**（包装函数 → 页面调用方），
   **不许**凭「后端有端点」就下「无入口」结论。

⚠️ 第一轮我按「按钮位置说明」实现了 `triggers`（`笔记工坊-草稿-生成草稿` 这类），
第二轮被纠正；第三轮又查出**3 个业务点被错标成「无界面入口」**（漏追第二跳）。原句与两轮
纠正均保留在此，供后续追溯 —— 这两轮纠正都进了 §13.3.2 的判据。

### 13.3.2 触发源：唯一真相源与上游排查结论

**唯一真相源** = `services/business_provider_keys.py` 的 `BusinessKey.triggers`
（元组，页面路径链）。下发端点与前端**都不维护第二份**（DEF-044）。

⚠️ **`label` 也上提到常量**（原先散在下发端点的 `labels` dict 里）：两个展示性元数据同一处维护，
新增业务点只改一个文件（下端点漏改会静默丢字段，DEF-044 的同型风险）。

**上游排查（第三轮口径，逐条给证据；「无入口」必须查到根）**：

| 业务点 | 触发源（页面路径链） | 证据 |
|---|---|---|
| `generate_note` | 笔记工坊-草稿-生成草稿 | `drafts-workshop-panel.tsx:456` |
| `rewrite_note` | 笔记工坊-草稿-改写、自动运营 | `drafts-workshop-panel.tsx:480` / `auto_ops_service.py:143` |
| `generate_title` | 笔记工坊-草稿-生成标题 | `drafts-workshop-panel.tsx:511` |
| `generate_tags` | 笔记工坊-草稿-生成标签 | `drafts-workshop-panel.tsx:529` |
| `polish_text` | **（空）** | 前端 `grep polish-text` = 0 命中；后端 `POST /ai/polish-text` 在 |
| `generate_cover` | **（空）** | 前端 `grep images/generate-cover` = 0 命中；后端 `POST /ai/images/generate-cover` 在 |
| `generate_image` | 笔记工坊-图片-生成配图、笔记工坊-草稿-生成配图 | `image-studio-panel.tsx:108` / `drafts-workshop-panel.tsx:1097` |
| `describe_image` | 笔记工坊-图片-图片理解 | `image-studio-panel.tsx:133` |
| `generate_product_detail_image` | 商品列表-商品详情-生成商品详情图 | 见 §13.3.3（本版新建） |

⚠️ **`drafts-page.tsx:924` 的那处 `generateImageWithAi` 不计入触发源**：该文件是全仓唯一死代码页
（零引用，`drafts-workshop-panel.tsx:197` 有明确标记）—— 把死页面算成入口 = 编一个不存在的入口。

⚠️ **空元组的语义**：该业务点**目前确实没有界面入口**（只有后端端点，或连端点都还没建）。
页面渲染成「无界面入口（仅 API 调用）」，**如实说明**，不编入口、不隐藏该行
（同「缺值不造假」纪律：漏配的业务点必须仍可见，否则用户无从发现）。

### 13.3.3 商品详情图链路（本版新建，设计 13 §30.3.6 的商品详情弹窗内）

**为什么必须后端代理（不能前端直连服务商）**：

1. 服务商 `api_key` 在服务端（`llm_gateway_configs`），**绝不下发前端**；
2. 商品图是**千帆 CDN 直链**，服务商侧取不到 ⇒ 必须由后端下载/转存后作为参考图送入；
3. 出图要落 `aigc_media_assets` 溯源（平台发布要求 `aigcTaskId`）。

**端点**：`POST /api/xhs/qianfan/generate-detail-image`

| 字段 | 方向 | 说明 |
|---|---|---|
| `platform_account_id` | in | 归属校验（`_owned_account`） |
| `item_id` | in | 商品 ID |
| `prompt` | in（可选） | 用户补充要求；为空则由后端按商品信息拼默认提示词 |
| `url` / `asset` | out | 出图 URL + `aigc_media_assets` 行（`asset_id` / `local_path` / `prompt` / `model_name`） |

⚠️ **入参刻意不给 `image_url`**：商品图由后端**按 `item_id` 从本地台账取**
（`shop_products_service.item_detail` 的 `images`）——让前端传 URL = 前端可传任意 URL 让后端去下载，
是 SSRF 面；且台账里就有真值，让前端传一份会多一个「传的和台账不一致」的静默分叉。

**错误口径**（与 `ai.py` 的 `_business_model_context` 一致，结构化 + 不回落）：

| 情况 | 状态码 | code |
|---|---|---|
| 业务点未配服务商 | 400 | `LLM_BUSINESS_PROVIDER_MISSING`（由 `resolve_business_provider` 给） |
| 映射指向的服务商类型不是 `image` | 400 | `LLM_BUSINESS_PROVIDER_TYPE_MISMATCH` |
| 商品不在本地台账 | 404 | —— |
| 商品无图 | 400 | `PRODUCT_IMAGE_MISSING`（**如实报缺，不造一张图出来**） |
| 服务商出图失败 | 502 | —— |

**前端**（**v2.12 修订，2026-10-02 用户口径**「商品的 AI 生成详情图主图功能在商品**列表操作列**
使用**单独弹窗**实现，**不要在编辑或详情弹窗中实现**」）：

- 入口 = 商品列表「操作」列的 **`生成详情图` 按钮**（与「生成笔记」并列），点击打开
  **独立弹窗** `detailImageOpen`（宽 560，自带 `item` 上下文）。
- 弹窗内容：商品名 / 商品ID 只读 + 补充要求输入框 + 「生成」按钮 + 出图结果缩略图。
- ⚠️ **详情弹窗（`detail` Modal）里那段内联「生成商品详情图」区块已整段移除** ——
  用户口径明确「不要在编辑或详情弹窗中实现」。被取代的原实现（内联区块 + 结果缩略图）保留在
  git 历史与本节的修订记录里，**不在此处留半份**。
- ⚠️ **弹窗打开即重置三态**（prompt / 出图 / 报错）：弹窗是「一次生成动作」的载体，
  带着上一个商品的结果重开会把 A 的图挂在 B 名下（同 `batchDeleteHint` 的跨上下文纪律）。
- ⚠️ **无图不拦按钮**：商品图集可能还没同步到台账，但服务端拿到 `item_id` 后按台账取图，
  取不到给 `PRODUCT_IMAGE_MISSING` —— **如实报缺，不造图**（不在前端预判拦下）。

### 13.3.7 提示词整理 + 张数 + 参考图必选 + 落素材库（v2.19，2026-10-03 用户口径）

> 用户原话（三条连续口径）：
> ①「调用大模型时，将**商品名称、商品参数信息和选择的参考图以及提示词和生成的图片要求
> 全部整理扔给大模型**，最后返回大模型生成的结果展示在页面，**图片支持下载到本地指定的
> 素材库中，也就是本地指定文件夹下**」；
> ②「生成商品详情图功能新增一个条件：**生成多少张图片**」；
> ③「**必须选择原始参考图**才可以点击生成按钮」。

#### 13.3.7.1 提示词整理（两段式调用）

**问题**：v2.18 前端点把「标题 + 规格名 + 价格」拼一句话，**直接丢给出图模型**。
出图模型只吃一句 prompt，**看不见商品参数**（库存/销量/类目/尺码）—— 那些信息全被丢掉，
等于让模型「只凭一个商品名猜画面」。

**改法**：

```
商品 info + SKU 参数 + 用户补充要求 + 出图要求 + 参考图(URL)
        ↓  文本/多态模型（POST {base_url}/chat/completions）
   结构化详情图提示词
        ↓  图像模型（POST {base_url}/images/generations）+ 参考图
        出图
```

| 面 | 实现 |
|---|---|
| 整理函数 | `services/detail_image_prompt.py::refine_prompt()` —— **唯一实现**，`build_item_brief()` 拼输入 |
| 请求体 | `refine_prompt: bool = True`（可关）；关掉 = 只用本地拼接 |
| **失败回落** | 未配文本模型 / 调用失败 / 返回空 ⇒ **回落**本地拼接（`_default_detail_image_prompt`），**不阻断出图**；原因记 `prompt_source="local"` + `refine_note` 并回传前端 |
| 为何不硬门禁 | 整理是**增强层**：因「润色模型没配」而让整个功能不可用，是把可选层做成硬门禁 |
| 参考图给文本模型 | 给**图片 URL**（最多 3 张）。部分服务商抓不到外链 ⇒ 失败即回落，不因此判死 |

⚠️ **`_text_model_context()` 与 `_business_model_context()` 语义不同**：后者按业务点 `kind`
校验且**缺配置即 400**；前者要的是「拿得到就用、拿不到就算」⇒ 返回 `(None, None, "")`。
但两者的**解析入口仍是同一份** `resolve_business_provider`（不另写一份查找）。

#### 13.3.7.2 生成张数（`count`）

- 请求体 `count: int = 1`（`ge=1, le=4`；`MAX_DETAIL_IMAGE_COUNT = 4`）。
- ⚠️ **逐张发请求**（端点内 `for range(count)`），**不用上游 `n` 参数** —— 各家支持不一，
  依赖它 = 行为不可预期（有的静默只回 1 张，用户以为配错了）。
- **半成功可交付**：一张失败不丢已生成的 —— 成功的先落库并回 `assets`，
  失败的进 `errors`（前端 `message.warning` 如实报「已生成 N 张，M 张失败」）；
  **全部失败 ⇒ 502**（不假装成功）。
- 前端超时按张数放大（`180000 * count`）：逐张串行，固定 180s 会让多图在前端先超时，
  用户看到假失败而后端还在跑。

#### 13.3.7.3 参考图**必选**（取代「不勾则回落首图」）

| | 旧（v2.15~v2.18） | 新（v2.19） |
|---|---|---|
| 不勾参考图 | 回落商品首图，照常生成 | **400 `REFERENCE_IMAGE_REQUIRED`** |
| 台账无图可勾 | 400 `PRODUCT_IMAGE_MISSING` | 不变（但**判在参考图之后**：「没选」优先于「没得选」） |

⚠️ **前后端同一判据**：前端按钮 `disabled={detailImageRefs.length === 0}` +
Tooltip 说明原因；后端同样拒绝。只改一头 = 「能点但被拒」/「灰着但后端其实接受」，
两种都是最难查的不一致。

#### 13.3.7.4 落盘到用户指定的素材库文件夹

- 请求体 `material_folder_id: int | None`（`material_folders.id`）。
- 落盘到 **`material_folders.path`（用户自己的绝对路径目录）**，
  ⚠️ **不是** `storage/media/`（那是项目托管目录，`_download_media` 的地盘）。
- **只落文件，不建 `material_files` 索引行**：索引由用户点「扫描」时按真实文件生成
  （§3.4「只索引不搬移」）。在这里插行 = 与扫描抢同一份真相源。
- 路径过 `is_safe_material_path` 安全校验（系统目录黑名单，§6.1）；
  `Content-Type` 必须是 `image/*`（上游回 HTML 错误页时不能当图片存）。
- 文件夹不属于当前用户 ⇒ 404（租户隔离，与其余端点同一把尺子）。

**增减量**：端点 **±0**（字段扩展）/ 数据表 **±0** / 迁移 **±0** / 新服务模块 **+1**
（`detail_image_prompt.py`）/ 请求函数 **±0**（`generateProductDetailImage` 加两个可选参）。

### 13.3.6 参考图可选「原商品详情图」（v2.15，2026-10-02 用户口径）

> 用户原话：「生成商品详情图功能，**支持使用提供原商品的详情图作为参考，原始详情图可勾选**」。

**关键实证**（2026-10-02 真机台账核对）：商品行有**两套不同的图**，此前被 `_image_links`
**混成一个列表**（`images` 在前、`image_descriptions` 追加在后）⇒ 详情图链路实际只用了
`images[:1]`（**商品主图**），**原商品详情图从来没被用上**。

| 字段（`raw_json`） | 语义 | 真机实测 |
|---|---|---|
| `images[]` | **商品主图**（10 张，方图为主，如 800×800） | 10 张 / 条 |
| `image_descriptions[]` | **商品详情图**（5 张，**竖长图**，如 2480×2824 / 1500×2322） | 5 张 / 条 |

两者都带同一组键（`link` / `width` / `height` / `material_id` / `extension`）。

**契约变更**：

| 面 | 变更 |
|---|---|
| 请求体 | `GenerateDetailImageRequest` 加 **`reference_image_urls: str[]`**（可选，**上限 5 张**）；空 ⇒ 沿用旧行为（商品首图） |
| 台账读接口 | `item_detail` 新增 **`detail_images`** 字段（`image_descriptions` 的 link 列表，**与 `images` 分开**），供前端渲染勾选 |
| 服务端取图 | 参考图从**白名单**里校验：传入的 URL 必须能在该商品的 `images` 或 `image_descriptions` 里找到 ⇒ 否则 400 `REFERENCE_IMAGE_NOT_FOUND`（**不让前端传任意 URL** = 保留 SSRF 防线；同 §13.3.3「不给 image_url」的初衷） |
| 前端 | 独立弹窗新增**参考图勾选区**（主图 / 详情图两组，多选，默认勾主图首张）；未勾任何 ⇒ 按旧行为取首图 |

**为什么仍不让前端自由传 URL**：允许任意 URL ⇒ 后端变成「下载任意地址」的跳板（SSRF）。
**白名单校验**既满足「可勾选」，又不打开这个口子 —— 勾选项全部来自服务端下发的台账图。

**增减量**：端点 **±0** / 数据表 **±0** / 迁移 **±0** / 请求函数 **±0**（字段扩展）。

**v2.16 布局重排（2026-10-03，用户口径「生成商品详情图 重新布局一下」+「全屏的的 60%」）**：
原版 **560 宽 + 从上到下一路堆叠**（商品 → 参考图两组 → 提示词 → 结果）⇒
参考图缩略图被挤成窄条、**出图结果压在弹窗最底部要滚动才看得到**（生成动作的结果
反而是最不显眼的元素）。重排为 **`60vw`（全屏 60%）+ 左右两栏 1:1**：

| 栏 | 内容 | 宽度 |
|---|---|---|
| 左 | 商品信息 / **参考图勾选**（主图 + 原商品详情图两组） / 补充要求 | 12/24 |
| 右 | **生成结果**（出图即时可见，虚线占位框不留白） | 12/24 |

⚠️ **`60vw` 的连带处理**：宽屏下弹窗会**很高**（内容纵向撑开超出视口）⇒
内容区 `maxHeight: 68vh` + 滚动，弹窗本体不超出视口；右栏结果框改
`minHeight: 320` + `height: calc(100% - 26px)` 跟随栏高（固定 260 在 60vw 下会显小）。
缩略图 64 → 76；「已选 N 张」并入标题行（省一行高度）。用 `Row/Col` 而非 `flex`：
与全站栅格口径一致，窄视口自动折行单列。
守卫 `test_detail_image_modal_uses_two_column_layout`（5 断言，钉 `width="60vw"`；
变异「改回 560 + 右栏改名」实测 CAUGHT）。**零端点 / 零表 / 零迁移 / 零请求函数**。

**落地记录（2026-10-03）**：后端 `product_image_links` / `detail_image_links` 两个纯函数
（与合并口径 `_image_links` 并存，各服务不同用途）+ `item_detail` 下发 `product_images` /
`detail_images`；端点加白名单校验（`REFERENCE_IMAGE_NOT_FOUND`）；前端弹窗加
`ReferenceImagePicker`（缩略图点击勾选，两组分开）。**真机实证**：acct 8 商品
合并 15 = 主图 10 + 详情图 5 ✅。

⚠️ **本轮顺带修掉一个「看着绿但没测到东西」的夹具缺陷**：`test_api.py` 的
`_seed_shop_product` 把 `raw_json` 写成自造的 `image_links` 键（**平台根本没有这个键**）
⇒ 一切「从 raw 取图」的断言都在对着空列表做。已改为平台真实字段名
`images` / `image_descriptions`（真机形状 `[{link, width, height, …}]`）。

### 13.3.4 增减量（供回数）

| 域 | 变化 |
|---|---|
| 端点 | **+1**（`POST /api/xhs/qianfan/generate-detail-image`）⇒ 181 → **182** paths |
| 数据表 | **0**（复用 `aigc_media_assets` / `tasks`，不建表） |
| 迁移 | **0** |
| 前端请求函数 | **+1**（`generateProductDetailImage`）⇒ 161 → **162** |
| 业务点 | 8 → **9**（`generate_product_detail_image`，kind `image`） |

### 13.3.5 商品同步 = 同步全部状态商品（v2.13，2026-10-02 用户口径）

> 用户原话：「商品同步更新需求为**同步全部状态的商品**和对应对应操作列的功能和商品列表的批量功能」。

**真机只读调研**（2026-10-02，CDP + 项目自己的 ark 会话，账号 8 观隅好物；见
`.workbuddy/tmp/research_20261002_qianfan_ops.md`）：

- 平台商品列表有 **8 个状态 tab**：全部 / 售卖中 / 已下架 / 已冻结 / 整改 / 审核中 / 审核驳回 /
  审核通过 —— 与 `apis.xhs_qianfan_apis.QIANFAN_ITEM_STATUSES`（7 值 + 全部）**逐字吻合**。
- **「全部」= 不传 `card_type`**；平台主力端点 `POST /api/edith/product/search_item_v2`
  与项目 SDK `search_items` **同源**。
- 平台「操作」列：`编辑` / `下架` / `更多`（更多 = 上架/删除/改价/改库存/改标题/改属性/尺码表/物流/笔记管理/推广）。
- 平台「批量操作」：发布单品 / 发布组合品 / 下架 / 删除 / 修改（标题/售价/库存/属性/尺码表）/ 物流 / 批量操作记录。

**落地（本项目只读边界内可做的）**：

| 面 | 实现 |
|---|---|
| 后端状态范围 | `SyncShopGoodsRequest.item_status`（**默认 `all`**）；`sync_task_service.SYNC_ITEM_STATUS_ALL` + `normalize_item_status()`（**坏值 ⇒ `all`**，不退成更窄的状态）；`shop_goods_sync_service._sync_with_task(item_status=…)` 透传到 SDK `search_items(status=…)`；任务行 `extra.item_status` 落库 ⇒ worker 从行重建（与 `mode`/时间窗同一纪律） |
| 前端条目 | 商品列表**搜索工具栏**「同步商品」按钮（全量全部状态）+ **商品台账卡「批量同步商品（N）」**（勾选后触发，提示如实说明平台按页同步、不支持只同步几个） |
| 操作列 | 新增 **`生成详情图`** 按钮（独立弹窗，见 §13.3.3 的 v2.12）；与「生成笔记」并列 |
| **不实现**（硬约束②只读） | 平台写操作（上架/下架/删除/改价/改库存/发布/推广）**一律不碰** —— 那是平台侧写接口，未授权 |

**增减量**：端点 **0**（`item_status` 是既有端点的请求体字段扩展）/ 数据表 **0** / 迁移 **0** /
前端请求函数 **0**（`syncShopGoods` 加可选字段，不新增函数）。

> ⚠️ 平台「批量操作」的绝大多数项（改价 / 改库存 / 上下架 / 删除）**是写接口**，本项目按
> 硬约束②「对小红书/千帆的探测一律只读」**不实现**。本项目的「批量功能」= 读 + 本地 + LLM 链路：
> **批量生成智能笔记 / 批量同步商品 / 批量删除（本地台账行）**。

---

## 13.4 T7 媒体生成（图片先行）· 字段级定稿（2026-10-02）

> **本轮范围**：LLM 生成链路的**图片**媒体落地（`ai_generate` / `material` /
> `reference_image` 三种来源）+ `aigc_media_assets` 溯源落库 + LLM 生成前端入口
> （生成配置弹窗 + 媒体来源四选一）。
> ⚠️ **视频生成（图生视频）明确排到下一轮**：视频是**异步任务链路**（提交 → 轮询 →
> 下载，分钟级），与图片的同步调用（`/images/generations` 一次返回）差异大，且需新增
> `model_type=video` + `/videos/generations` 客户端 + 异步轮询，独立成需求（§13.4.4 预留）。

### 13.4.1 `media_options` 精确结构（补全 §7.4 原「文生图·图生视频·图文拼图等」）

`media_options` 是 `media_source` 的**参数载体**，随 `media_source` 取值不同而语义不同。
JSON 结构（请求模型里是 `Optional[dict]`，**不落库**，只进队列项 `payload`）：

| `media_source` | `media_options` 字段 | 说明 |
|---|---|---|
| `product_image` | 无需（`{}`） | 复用商品原图直链，已有实现 |
| `material` | `material_file_ids: int[]` | 素材库 `material_files.id` 列表（勾选 N 张） |
| `reference_image` | `reference_image_urls: str[]`（可选） | 参考图 URL；**为空则后端按台账首图自动取**（同 §13.3.3 详情图口径：不让前端传 URL 是 SSRF 面） |
| `ai_generate` | `prompt?: str` / `reference_image_urls?: str[]` / `count?: int` | 文生图提示词（可选，空则后端按商品拼默认）；带 `reference_image_urls` 则走图生图；`count` 生成张数（默认 1，上限 4） |

- **错误口径**：`media_options` 与 `media_source` 不匹配（如 `material` 但无 `material_file_ids`）
  ⇒ 400 `LLM_MEDIA_OPTIONS_INVALID`（同步段门禁拦截，不入队）。
- **缺值口径**：`material_file_ids` 指向不存在 / 非本人的文件 ⇒ 400 `MATERIAL_FILE_NOT_FOUND`
  （DEF-066 归属校验）；`reference_image` 但商品无图 ⇒ 400 `PRODUCT_IMAGE_MISSING`（如实报缺，不造图）。

### 13.4.2 三种图片来源的精确行为（worker 内 `_media_assets_for` 扩写）

| 来源 | 行为 | 落 `note_assets` | 落 `aigc_media_assets` |
|---|---|---|---|
| `product_image` | 商品图直链（现有，不动） | url=商品图 URL | ❌（非 AI 生成） |
| `material` | 按 `material_file_ids` 取 `material_files` 行 → 本地文件 URL | url=本地下载 URL（`/api/xhs/materials/files/{id}/file`） | ❌（用户素材，非 AI 生成） |
| `reference_image` | 取参考图（`reference_image_urls` 或台账首图）→ `OpenAICompatibleImageClient.generate_image(reference_images=[…])` → 下载落 `storage/media/` → `note_assets` 用本地 URL | url=本地 URL | ✅（`prompt` / `model_name` / `aigc_task_id` 留空） |
| `ai_generate` | 文生图（`prompt`）或图生图（带 `reference_image_urls`）→ 同上 | url=本地 URL | ✅ |

- **媒体生成的模型上下文**：走 `generate_image` 业务点（`BUSINESS_KEY_GENERATE_IMAGE`，kind=`image`），
  **不是** `generate_note`（文本业务点）。worker 里媒体生成与文案生成**分开取模型上下文**：
  文案走 `generate_note`（text），媒体走 `generate_image`（image）—— 两者可能指向不同服务商。
- **媒体生成失败 ≠ 笔记生成失败**（本轮决策）：媒体是「配图/封面」，正文才是产物。
  `ai_generate` / `reference_image` 出图失败 ⇒ 记 `aigc_media_assets` 为空、`note_assets` 无媒体，
  **笔记照常落库**（正文 + 话题仍在），worker 日志告警 —— 与 `_media_assets_for` 现有
  「照实返回空列表、不阻塞笔记落库」纪律一致（一个坏媒体不该废掉整篇正文）。
- **落盘只写 `storage/media/`**（§10 安全设计，不写用户素材文件夹）；下载用 `requests`，
  校验 `Content-Type` 是图片（`image/*`），失败丢弃不落半条。

### 13.4.3 `aigc_media_assets` 溯源落库（接线现有表）

表已存在（T5 建，`models/generation.py::AigcMediaAsset`，迁移 `c4d5e6f7a8b9`），
本轮**不建表、不迁移**，只把落库接进媒体生成路径：

- `generation_record_id` → 关联 `generation_records.id`（本队列已有审计行）；
- `asset_type` = `image`；`local_path` = 落盘路径；`url` = 供应商回传 URL（若有）；
- `prompt` = 本次图片 Prompt（**溯源必留**）；`model_name` = 图像模型名；
- `aigc_task_id` **本轮留空串**（图片生成上游不回平台溯源号；发布时再按需回填）。
- ⚠️ **落库时机**：与笔记落库**同一事务**（§8.2「一条笔记的落库是一个事务」），
  媒体生成成功后 `db.add(AigcMediaAsset)` + `db.flush()`，随 `_persist_generated_note` 一起 commit。

### 13.4.4 视频生成（v2.14 定稿，2026-10-02）

> **原「下一轮预留」已落地为定稿**（原句保留在下方「前置结论」里，不静默删除）。

**上游协议**（OpenAI 兼容风格的视频接口，与图片的「一次返回」不同 —— 视频是**异步任务**）：

| 步骤 | 请求 | 说明 |
|---|---|---|
| ① 提交 | `POST {base_url}/videos/generations` | body：`{model, prompt, image?, duration?, size?}`；返回 `{id}` |
| ② 轮询 | `GET {base_url}/videos/generations/{id}` | 返回 `{status, url?}`；`status ∈ queued/running/succeeded/failed` |
| ③ 下载 | `GET {url}` | 落 `storage/media/`，校验 `Content-Type` 是 `video/*` |

**业务点**：新增 `generate_video`（kind=`video`，label「生成视频」），
与 `generate_image` **分开挂服务商**（图像与视频模型可能不同供应商）。

**`media_source` 复用**：`ai_generate`（文生视频）/ `reference_image`（图生视频，参考图 = 台账首图）；
`product_image` / `material` 对视频**不适用** ⇒ 传了给 400 `LLM_MEDIA_SOURCE_INVALID_FOR_VIDEO`
（**不静默退化成图片**：那会让用户以为选了视频却拿到一张图）。

**`media_options`**：

| 字段 | 说明 |
|---|---|
| `prompt?: str` | 视频提示词（空则由后端按商品拼默认） |
| `duration?: int` | 时长秒数（默认 5，上限 10；上游不支持则忽略） |
| `size?: str` | 分辨率（默认 `720x1280` 竖版；空则用默认） |

**异步轮询嵌进 LLM 生成 worker**：复用 `CrawlRateLimiter.wait_allow()` 限流 +
状态机 `queued → running → succeeded/failed`；**总超时 10 分钟**（视频慢），
超时记 `failed` + 原因，**不无限等**（无界的等待就是没有刹车点）。

**落库**：`aigc_media_assets.asset_type = "video"`；`prompt` / `model_name` / `local_path` 照图片口径。
本轮**不建表、不迁移**（复用既有表与列）。

**失败口径同图片**：视频生成失败 ⇒ 笔记照常落库（正文是产物），`note_assets` 无视频，
worker 日志告警 —— 一个坏视频不废掉整篇正文。

**前端**：生成配置弹窗的媒体来源按**模型类型可用性**动态显示 ——
选了 kind=`video` 的服务商时只出「AI 生成视频 / 参考图生成视频」两项。

#### 前置结论（原「下一轮预留」，保留供审计）

- 需新增 `model_type=video`（已就位：`business_provider_keys.MODEL_TYPE_VIDEO` +
  `MODEL_TYPES` 四值枚举 + 服务商页下拉，v2.9 落地）+ 视频业务点（**本轮新建 `generate_video`**）
  + 视频客户端（**本轮新建**）。

### 13.4.5 前端：LLM 生成入口 + 生成配置弹窗 + 媒体来源四选一

- `lib/api.ts` 新增请求函数：`generateLlmNote` / `batchGenerateLlmNotes` /
  `fetchLlmGenerateQueue` / `retryLlmGenerate`（对应 §7.4 四端点）。
- 商品列表页（`shop-items-page.tsx`）新增「LLM 生成」入口（与一期「生成笔记」并存，
  两队列语义不同，见 `note_gen_llm_worker.py` 模块 docstring）。
- 生成配置弹窗：选模板（`prompt_templates`）/ 媒体来源四选一（`ai_generate` /
  `material` / `product_image` / `reference_image`）/ 素材选择（`material` 时勾选
  `material_files`）/ 参考图（`reference_image` 时可选）。
- 生成进度卡复用一期骨架（5s 轮询 `generate-llm-queue`，键名 `generated`）。

### 13.4.6 增减量（供回数）

| 域 | 变化 |
|---|---|
| 端点 | **0**（复用 §7.4 四端点，`media_options` 只是请求体字段扩展） |
| 数据表 | **0**（复用 `aigc_media_assets`，不建表） |
| 迁移 | **0** |
| 前端请求函数 | **+4**（`generateLlmNote`/`batchGenerateLlmNotes`/`fetchLlmGenerateQueue`/`retryLlmGenerate`）⇒ 162 → **166** |
| 业务点 | **0**（复用 `generate_image`，kind `image`） |

### 13.4.7 落地记录（2026-10-02，backend 部分）
**结论：T7 后端已落地，前端（§13.4.5 的 4 个请求函数 + 商品页入口 + 四选一 UI）尚未实现，转为 T9 一并做。**

| 面 | 状态 | 落点 |
|---|---|---|
| 媒体生成服务 | ✅ 已落地 | `services/media_generation.py`（文生图/图生图 + 三种来源 + 失败不阻断正文） |
| worker 接线 | ✅ 已落地 | `services/note_gen_llm_worker.py` 调用媒体生成、消费 `aigc_origins` |
| AIGC 溯源落库 | ✅ 已落地 | `media_generation.py` 内 `AigcMediaAsset(...)`（不建表、不迁移，接线既有表） |
| API 字段门禁 | ✅ 已落地 | `media_options` 请求体字段校验（模型类型匹配 `_type_satisfies`） |
| 前端 4 函数 + UI | ⏳ **未实现** | 归 T9（`shop-items-page.tsx` 现仍只有一期「生成笔记」入口，零 `media_source` 引用） |

**增减量实测（照 §13.4.6 回数，均为 0）：** 端点 **0** / 数据表 **0** / 迁移 **0** / 业务点 **0**。
⚠️ 前端请求函数 **仍未 +4**（T9 才落地，届时 162 → 166）—— 本节落地**不改变**该口径。

### 13.4.8 T9 前端落地记录（2026-10-02）

**T9 已落地**（原「T2–T10 待后续开工」中的前端 UI 面）：

| 面 | 状态 | 落点 |
|---|---|---|
| 请求函数层 | ✅ | `lib/api.ts` **+17 个**（提示词 5 + 素材 6 + 生成 4 + `materialFileUrl` + `previewPromptTemplate`）⇒ 165 → **184**（含此前漂移） |
| 类型定义 | ✅ | `types/index.ts` **+8 个**（`MaterialFolder` / `MaterialFile` / `PromptModule` / `PromptTemplate` / `PromptTemplateModuleRef` / `LlmMediaSource` / `LlmMediaOptions` / `LlmGenerateQueueSnapshot`） |
| 素材库面板 | ✅ | `pages/platforms/xhs/material-library-panel.tsx`；挂工坊 `?tab=materials` |
| 提示词模板面板 | ✅ | `pages/platforms/xhs/prompt-template-panel.tsx`（模块 + 模板两层 + 渲染预览）；挂 `?tab=prompts` |
| 生成配置弹窗 | ✅ | `pages/platforms/xhs/llm-generate-modal.tsx`（选模板 + 媒体来源四选一 + 素材勾选） |
| 生成进度卡 | ✅ | `pages/platforms/xhs/llm-generate-progress-card.tsx`（5s 轮询 / 键名 `generated` / 禁 finally） |
| 商品页入口 | ✅ | 操作列 **`LLM 生成`** 按钮（与「生成笔记」「生成详情图」并列） |

**增减量实测**：端点 **0** / 数据表 **0** / 迁移 **0**（纯前端接线）；请求函数 **165 → 184**；
页面文件 **27 → 27**（面板是子组件，不是独立页面文件）；工坊页签 **3 → 5**。

**守卫**：`test_page_optimizations.py::test_t9_panels_are_wired_into_workshop`（4 件：接线 /
只读边界文案 / 进度卡禁 finally / 媒体来源四选一）；**变异 2/2 CAUGHT**（进度卡塞 finally /
摘掉素材页签 均精确变红）。

⚠️ **仍未做**：视频生成（§13.4.4，图生视频异步链路）—— 独立成需求，下一轮做。

### 13.4.7.1 连带修复：迁移幂等（`default_model` 列守卫）

T7 连带跑全量回归时暴露 **3 条未提交迁移的幂等缺陷**（`e5f6a7b8c9d0` v2.7 / `b2c3d4e5f6a7` v2.3 / `d5e6f7a8b9c0` v1.9）：它们都**无守卫地引用 `llm_gateway_configs.default_model`**，而 `f6a7b8c9d0e1`（v2.8）会 **drop** 该列；「stamp 回早期 revision 再 upgrade」是 alembic 正常用法（迁移用例 ④ 幂等步骤正是如此）⇒ 重跑即 `no such column: default_model`。

修法：三处均加 `_column_exists(conn, _TABLE_CONFIGS, "default_model")` 守卫（与各文件既有的判列/判表纪律同型）。**这是迁移正确性修复，非测试妥协。**

⚠️ 同时补齐夹具：`tests/backend/conftest.py` 的 `LATER_MIGRATION_TABLES` 新增 `users` 最小形状表 —— `a7b8c9d0e1f2`（v2.9）对 `llm_gateway_models` 做 `batch_alter_table` 时 SQLAlchemy 反射整表，要解析建表时（`e5f6a7b8c9d0`）带的 `sa.ForeignKey("users.id")` ⇒ 夹具缺 `users` 即 `NoSuchTableError`。

---

## 14. 里程碑验收（对齐 16 文档 §2.6）

- 选商品 → 选模板 → 点「生成并保存」，**≤60s** 产出：标题（多备选）+ 正文（600–900 字）+
  话题标签 + 封面图/视频（AI 生成 / 素材库选取 / 复用商品原始图 / 参考原始图生成新图）；
- 生成结果在内容库可见（`local`+`pending`），可预览编辑、可一键发布（走一期预审核 + 挂链）；
- 素材库：配 2–3 个本地文件夹后，面板秒开缩略图，可搜索/拖拽选用；增删文件后「刷新」即同步；
- 提示词模板：公共模块可组合复用、单独模板按任务类型调用；生成图可指定落素材库 + 直接用于当前笔记；
- **数据监控**：**网关内第一项菜单**「用量看板」（`/llm-gateway/usage`，进 `/llm-gateway` 默认重定向到此）从 **`llm_gateway_usage_logs` 本地台账** 展示
  今日/近 7 日/近 30 日 调用量、成功率、token、耗时 的卡片 + 折线 + 多维分布；
  （2026-10-01 §11.3 三次修订：原「网关页内简化页签」先迁独立首页 `/usage-dashboard`、随后**撤销独立首页**、
  完整看板回归网关页作**第一个页签**、最后**取消 Tabs 改子路由独立页面**（本页排最前）；网关页**不再有任何跳转按钮**
  —— 看板本身就在页内可见）
  ~~New API 面板可见消耗趋势/模型分布/日志；跳转 New API 明细~~（v1.7 移除）。

---

## 15. ~~New API 网关部署与初始化（v1.3 工程前置，T0）~~

> ⚠️ **本节整节作废（v1.7）**：项目**不再内置 New API 外挂进程**，无需部署、无需 .env、无需防火墙端口例外、
> 无需管理员引导、无需渠道/令牌面板配置。上游连接改为在网关内「连接配置」页面（`/llm-gateway/gateways`）**填一条 base_url + 令牌**即可
> （三方服务商直连是零部署路径）。以下 v1.3–v1.6 的部署记录**仅作历史存档保留**（工程事实，不删除）。

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

### 15.4 T0/T1 验收记录（2026-09-29 实测）

| 项 | 结果 |
|---|---|
| exe 下载 | `new-api-v1.0.0-rc.40.exe`（140,763,648 B，SHA256 与 `checksums-windows.txt` 逐字节一致，经 ghfast.top 反代） |
| .env | `data/newapi/.env`：随机 `SESSION_SECRET`/`CRYPTO_SECRET`、`SQLITE_PATH=D:/work/XHS/data/newapi/new-api.db?_busy_timeout=30000`、`TZ=Asia/Shanghai`、`BATCH_UPDATE_ENABLED=true` |
| 启动 | `--port 3000 --log-dir ./logs`，`/api/status` 200 `success:true`；⚠️ exe 固定监听 `0.0.0.0`（Windows 无 `BIND_ADDRESS` 参数，Docker 才靠 `-p 127.0.0.1`）⇒ **已加防火墙入站拦截 3000 端口**，局域网不可达，满足「仅 127.0.0.1 对外」合规 |
| 管理员 | `POST /api/setup`（`confirmPassword` 驼峰字段，`SelfUseModeEnabled=true`），root + 随机强密码 |
| 渠道 | **书生平台**（`discovery-api.intern-ai.org.cn/v1`，OpenAI 兼容，type=1）：复用本机 `models.json` 已配的合法上游 key（项目一期 `model_configs` 里的 deepseek key 已失效 401）⇒ 10 模型：`Agents-A1`/`intern-s2`/`deepseek-v4-flash-0731`/… |
| 令牌 | 业务令牌 `xhs-biz`（sk- 开头，`/v1/models` 200）；⚠️ rc.40 **无长效系统访问令牌**（setup 建的 root `access_token=NULL`，改新版短效 JWT 15 分钟）⇒ `admin_token` 语义 = 登录 JWT（`/api/log/stat` 用它 200 `{quota/rpm/tpm}`），T8 快照实现时按「存用户名+密码定期登录换 JWT」设计 |
| 端到端 | `POST /v1/chat/completions`（业务令牌）→ New API → 书生上游 → 返回 `"成功"` 200（**T0 验收线通过**） |
| 渠道 base_url 坑 | ~~OpenAI 类渠道 base_url **必须填根地址不带 `/v1`**（New API 自己拼 `/v1`；填了 `/v1` 会拼出 `/v1/v1/*` ⇒ 上游 404 `bad_response_status_code`）~~ —— **v1.8 该结论只对「New API 渠道配置」成立**（New API 内部自己拼 `/v1`）；**本项目网关相反**：`base_url` 就是**调用地址本身**，**带不带 `/v1` 完全由用户地址决定**，后端不拼（见 §7.1.0）。⚠️ 本条曾导致「填了 `/v1` 就 404」被误当成用户填错，实为**后端双拼**，v1.8 已修 |
| T1 建表 | 迁移 `m4n5o6p7q8r9`（revises `l3m4n5o6p7q8`）：`llm_gateway_configs`（唯一 `(user_id,name)`）+ `llm_gateway_snapshots`（唯一 `(user_id,gateway_id,stat_date)`）；升级幂等、降级可逆（已实测 downgrade/upgrade 往返）；真库备份 `data/spider_xhs.db.bak-20260929-pre-llm-gateway` |
| T1 端点 | 5 端点 CRUD+test 已上线（**v1.4 起挂 `/api/llm/`**，多平台公共）：`GET/POST /api/llm/gateway-configs`、`PATCH/DELETE /{id}`、`POST /{id}/test`；`/test` 实测返回 `{healthy:true, models:[10], chat 连通}`、令牌只给 `has_*` 布尔不回传明文字段；旧路径 `/api/xhs/llm/gateway-configs` 已下线（守卫钉 404） |
| 测试 | `tests/backend/test_llm_gateway.py` 11 用例全绿（CRUD/越权 404/重名 409/base_url 改后 healthy=false/token 留空保持不变/删除级联快照/不可达 400/**v1.4 旧路径 404 守卫** `test_legacy_xhs_path_is_gone`）；slow 迁移用例（upgrade head 过新迁移区间）另行全绿 |
| 文档 | 双校验脚本 PASS（端点 191→196 / path 164→167 / router 24→25 / 表 29→31 全量逐域回数） |