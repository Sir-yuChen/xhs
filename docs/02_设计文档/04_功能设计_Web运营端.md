# 功能设计 · Web 运营端

> - **阶段**：二 · 设计
> - **访问端**：浏览器（React SPA）
> - **基准**：按页面（用户可见的功能单元）组织，每个页面给出布局、交互、数据来源
> - **版本**：v1.10 · 2026-09-26（**§2.12 内容库统计卡：两张进度卡走「当日口径」（设计 13 §37）**：发布 / 同步进度卡的卡面三数不再由页面自己累计（刷新即归零、也看不见定时 / 恢复入口入队的项），改读快照新键 `today`（后端从 `tasks` 表按当日现算 成功 / 失败 / 待处理）；两张卡右下角加小字「仅当日数据」；六张卡统一内容高度（`flex="1 1 0%"` 只管等宽，高度得显式钉）。统计卡行由「三卡」口径勘正为**六卡**）。v1.9 · 2026-09-23（**§2.21 商品列表 v1.37 排序两改（设计 13 §33.11）**：① **默认不排序** —— `DEFAULT_SORT` 改为 `null`，进来表头没有箭头、请求也不带 `sort_field`（后端用它自己的稳定默认序）；② 点表头才触发，且**一律由后端排** —— `sorter: true` + 受控 `sortOrder`，**不许给 `sorter` 传比较函数**（antd `getSortFunction` 只在 function / `.compare` 时返回真值，`sorter: true` 时 `getSortData` 跳过本地排序；传了函数就变成「只排当前页」的假排序）；三态与 antd 原生一致（升 → 降 → 取消），「重置」清排序、「搜索」保留排序）。v1.8 · 2026-09-23（**§2.21 商品列表 v1.36 三改（设计 13 §33.10）**：① 修「挂链笔记数筛选未生效」—— 根因是**跑着的后端是旧进程**（模型里没这两个字段，Pydantic 默认 `extra="ignore"` ⇒ 参数被静默丢弃），修复 = 重启 + 请求模型加 **`extra="forbid"`**（未知字段 422，不许静默丢弃）；② 新增**批量删除本地台账行**（勾选跨页保留、换条件/换账号清空；上限 200/次；确认框讲清「关系不动 / 同步会写回」；平台商品不动）；③ 挂链笔记弹窗 **80vw × 84vh → 75vw × 75vh**（去 `maxWidth`、改 `centered`、body `overflow: auto`））。v1.7 · 2026-09-23（**§2.21 商品列表页上机验收修正（设计 13 §33，v1.33~v1.35）**：千帆可用性前置校验且未通过时**零请求 + 只提示一句**；搜索条件**恰好两行**、按钮组置尾；**新增挂链笔记数筛选**（比较符默认「小于」，留空不筛，关系表口径）；台账**只有「上架时间」列可排**且默认上架时间倒序；分类 / 商品名**不换行**（`…` 收起 + Tooltip）；缩略图 **71×71**、「N 图」角标挪到图片左上角；首卡「商品列表（千帆）」无内容时 body 整块不渲染（去空白））。v1.6 · 2026-09-22（**§2.12 内容库正文/图集补齐 + 来源账号**（设计 13 §19.13）：① 详情弹窗在正文为空、或点右上角「刷新」时带 `?hydrate=true` **即时补齐该条**（补齐后再拉素材），补齐后空正文用一行原因区分「取回过但确实没有」与「这次没取到（原因）」；② 表格新增「来源账号」列、卡片在来源与作者**不同名**时显示「来源：X」、详情弹窗加「来源账号」项 —— **空则整项不渲染**（缺值口径））。v1.5 · 2026-09-22（**§2.12 两处界面收敛（用户口径）**：① 撤掉详情弹窗里「同步回来的笔记默认『已发布』；下一轮同步会把改回『待发布』的再升回去」那行机制说明 —— 口径本身仍记在 §2.12「手动切换」行与 `03_数据模型设计.md` §4.1，只是**不搬到界面上**（把实现机制写在页面上属噪声，与「商品来源行不解释补齐行为」同源）；② 作者昵称缺值**整项不渲染**，不再写「未知」充数（硬约束⑤：没数据 ≠ 有数据））。v1.4 · 2026-09-22（**内容库「发布状态」工作流 + 详情改弹窗**：§2.12 内容库页 ① 顶部统计卡改为「已发布 / 待发布」（**全库口径**，不随筛选变化）；② 筛选加「发布状态」；③ 卡片与表格展示发布状态标识并支持**手动切换**；④ 详情由 `Drawer` 改为**弹窗**，与「我的笔记」**共用** `components/platforms/note-detail-modal.tsx`；§2.20 详情 Modal 同步标注「已抽为共用组件」）。v1.3 · 2026-09-18（v1.3：§2.4 账号卡片五字段统一 + 指标真实来源与同步入口；「我的笔记」页翻页改滚动触发、新增关键词搜索、弹窗评论区固定尺寸）

---

## 1. 导航结构

侧边栏分为「主导航」与「底部导航」两组（`frontend/src/components/layout/app-shell.tsx:54-74`）。

```
┌─ 主导航（12 项）────────────────────┐
│ 总览          /platforms/xhs/dashboard
│ 账号矩阵      /platforms/xhs/accounts
│ 笔记发现      /platforms/xhs/discovery
│ 我的笔记      /platforms/xhs/my-notes   ← 2026-09-18 新增
│ 数据抓取      /platforms/xhs/crawler
│ 关键词组      /platforms/xhs/keywords
│ 数据洞察      /platforms/xhs/analytics
│ 竞品监控      /platforms/xhs/benchmarks
│ 内容库        /platforms/xhs/library
│ 笔记工坊      /platforms/xhs/note-workshop   ← 2026-09-25 合并图片 / 视频 / 草稿三坊（§2.22）
│ 发布中心      /platforms/xhs/publish
│ 自动运营      /platforms/xhs/auto-ops
├─ 底部导航（3 项）───────────────────┤
│ 任务中心      /tasks
│ 模型配置      /models
│ 设置          /settings
└────────────────────────────────────┘
```

> ⚠️ **2026-09-25 §36 合并**：原「图片工坊 / 视频工坊 / 草稿工坊」三个菜单项与三条路由
> 合并为**「笔记工坊」一项一页**（页内 Tabs 隔离三个工坊）。旧路由
> `/image-studio`、`/video-studio`、`/drafts` **重定向**到
> `/note-workshop?tab=images|videos|drafts`（深链与书签不死）。下文 §2.10 / §2.11 /
> §2.13 保留原设计并标注「被 §2.22 取代」，新口径以 §2.22 为准。

**布局骨架**：`AppShell` = 左侧可折叠 Sider（宽 64 / 220）+ 顶部 Header（通知铃铛 + 主题切换 + 用户信息）+ 内容区。

**全局件**：
| 组件 | 位置 | 行为 |
|---|---|---|
| 通知铃铛 | Header 右侧（`app-shell.tsx:206-210`） | Badge 显示未读数，**每 30s 轮询**（`:99-103`）；下拉列表支持单条已读与全部已读 |
| 主题切换 | Header（`:199-205`） | 明暗切换 |
| `PageHeader` | 复用组件（`:222-233`） | 统一页面标题区（标题 + 描述 + 右侧操作区） |
| 路由缓存 | `KeepAliveRouteOutlet`（`:215`） | **仅对 discovery / crawler 启用**（保留搜索状态与结果） |

---

## 2. 页面清单与功能设计

### 2.1 `/login` 登录页

| 项 | 内容 |
|---|---|
| 布局 | 双栏：左营销区（Logo + 标题 + 3 个 Statistic 数字），右登录卡 |
| 交互 | Segmented 切换「登录 / 注册」；注册需确认密码；失败显示 Alert |
| 数据 | `useAuth().login` / `.register` → `POST /auth/login`、`POST /auth/register` |
| 守卫 | `PublicOnlyRoute`（已登录访问会跳走） |

### 2.2 `/platform-select` 平台选择

| 项 | 内容 |
|---|---|
| 布局 | 头部（eyebrow + 标题 + 描述 + 退出登录）+ 平台卡片网格 |
| 数据 | `fetchPlatforms` → `GET /platforms` |
| 兜底 | ⚠️ 请求失败时用前端硬编码副本 `lib/platforms.ts:3-10` |
| 行为 | 点非 xhs 平台 → `ComingSoonPage` |

### 2.3 `/platforms/xhs/dashboard` 运营总览

| 项 | 内容 |
|---|---|
| 布局 | PageHeader（含「开始发现」按钮）+ 4 张指标卡 + 下方两列（高潜话题 List / 自动化结果 List） |
| 数据 | `fetchXhsOverview` → `GET /xhs/analytics/overview` |
| 容错 | catch 时使用全 0 / 空数组的**安全零值兜底**（`xhs-dashboard.tsx:19-28`） |

### 2.4 `/platforms/xhs/accounts` 账号矩阵

| 项 | 内容 |
|---|---|
| 布局 | 账号卡片列表（Avatar / 三个平台的登录态标签 / 小红书号 / 粉丝·关注·获赞 / 同步与删除按钮）；**无页面头部**，「绑定账号」（primary）与「刷新」同在卡片右上角 `extra` |
| 数据 | `fetchAccounts` / `checkAccount` / `deleteAccount` |
| 添加账号 | `AddAccountDrawer` 抽屉，三种方式：扫码登录、手机验证码、Cookie 导入 |
| 状态展示 | `status` 徽标（`active` / `expired` / `unknown`） |
| ⚠️ 缺口 | 无「编辑账号」入口（`PATCH /accounts/{id}` 是 stub 且前端未调用） |

> **被取代的原句（保留备查，不静默删除）**：
> 「布局 | 头部 + 账号卡片列表（Avatar / 标签 / **类型 · 小红书号** / 粉丝·关注·获赞 / 健康检查按钮 / 删除）」。
> 其中「头部」「类型」「健康检查按钮」三项**均已不存在**：类型于 v1.30 移除（用户 2026-09-21 口径：
> 协议链路对使用者没有意义）、「检查」按钮于 v1.31 移除（探活改为进页面 / 刷新自动做，见设计 13 §19）、
> **页面头部于 2026-09-28 整块移除**（「绑定账号」收进卡片右上角与「刷新」同行，见设计 13 **§40**）。

**卡片信息区（2026-09-18 二次统一：五字段 · 两类账号展示完全一致）**：`类型` / `小红书号` 一行（各 50%），
`粉丝` / `关注` / `获赞数` 一行（各 33%），PC 与 Creator **共用同一段 JSX**（不允许两份分支）。
此前 Creator 的互动指标行被整行隐藏（`accounts-page.tsx` 的 `isCreator ? null : …`），于是两类卡片结构不同；
本次删除该分支——**结构一致是硬要求**，取不到值也不许隐藏（**小红书号用 `-`，三项计数用 `0`**，见下表）。

| 字段 | 来源 | 缺失时 |
|---|---|---|
| 类型 | `sub_type`（`creator` → Creator，其余 → PC） | — |
| 小红书号 | `profile.red_id`（PC ← `/v2/user/me`；Creator ← `/api/galaxy/user/info`） | `-` |
| 粉丝 / 关注 / 获赞数 | `profile.followers / following / likes` | **`0`** + 一行 **25–30 字**可读原因 |

> **缺值口径（2026-09-18 晚定稿，R10 / D16）**：三项计数缺值渲染 `0` 而不是 `-`。
> `0` 是**展示层默认值**，「真 0」与「没取到」由下方那行原因区分，库里 `profile_json` 仍写「未知」（**不得把 `None` 改写成 0**）。
> 原因一律 25–30 字、来自后端 `metrics_reason`（前端不硬编码业务判断），超长由后端 `_clamp_reason()` 截断到 30 字。

- **指标真实来源（2026-09-18 实测，推翻 v1.2 的「PC 来自 `/v2/user/me`」）**：`/v2/user/me` 的 `data`
  只有 8 个字段（`desc/gender/guest/imageb/images/nickname/red_id/user_id`），**没有 `interactions`**，
  所以原实现读它 → 三项**恒为 `None`**（卡片永远 `-`，属「静默错值」族缺陷 DEF-039）。
  真实来源是 `/v1/user/otherinfo`：实测 `interactions = [{follows:258},{fans:49},{interaction:39}]`。
- **Creator 链路结构上不可得**：`/api/galaxy/user/info` 实测无任何计数；数据中心候选端点（`/creator/data/fans/overview` 等 4 条）实测 404。
  → 卡片渲染 `-` + 一行灰字原因，**不伪造、也不隐藏**。补齐路径见看板卡⑧（需真实抓包）。
- **同步入口**：每张卡片一个「同步数据」小按钮 → `POST /api/accounts/{id}/metrics`（`_silent: true`，失败只在卡内提示）。
  ⚠️ **禁止在页面加载时自动拉取平台数据** —— 由 `tests/backend/test_api.py::test_accounts_page_does_not_auto_check_accounts_on_load` 钉住。
- 缺失时渲染 **`-` 而不是隐藏**。理由沿用项目既有原则「前端置灰而非隐藏」：
  隐藏会被当成 bug（用户无从判断是「平台没返回」还是「页面没做」），`-` 明确表达「没有这个值」。

**登录交互设计（三种方式）**：

```
扫码登录    → 建会话 → 展示二维码 → 每 2s 轮询会话状态 → scanned/confirmed/expired 三态
手机验证码  → 发码（手机号掩码展示）→ 输码 → 确认
Cookie 导入 → 粘贴 Cookie 字符串 → 校验格式 → 落库（追加版本）
```

#### 2.4.1 「登录 PC 后同步 Creator 账号」的正确做法（2026-09-18 修订）

PC 链路登录成功后可顺带绑定同名 Creator 账号。这个选项的实现有三条**互相独立**的约束，
踩错任何一条都会表现为「勾了没反应」：

| # | 约束 | 反例（修订前的实现） |
|---|---|---|
| 1 | **勾选不得重建登录会话** | 会话创建 effect 依赖了 `syncCreator`，勾选即调 `/pc/qrcode` 建**新会话**：二维码被静默换掉，若用户已扫旧码，前端却只轮询新会话 → **页面永远无反应** |
| 2 | **偏好必须在「确认」时被读到** | 偏好只在会话创建时落库。正确做法是轮询接口接受 `sync_creator` 查询参数作为**实时覆盖**（与手机验证码路径的 `PhoneConfirmRequest.sync_creator` 同构） |
| 3 | **同步失败必须可见** | `except Exception: return None` 让「同步成功」与「静默什么都没做」对外**完全等价**；响应里既没有结果也没有原因，前端从不读 `creator_account` |

**修订后的契约**：

- `GET /api/xhs/login-sessions/{id}?sync_creator=<bool>`：可选覆盖，缺省用会话里存的值。
  幂等——同步只在**首次观测到 `confirmed`** 的那次轮询执行。
- 响应新增 `creator_sync_error`：PC 已绑定时同步失败的**原因**（成功为 `null`）。
  `creator_account` 语义不变（失败为 `null`）。
- `POST /api/accounts/import-cookie` 同构返回 `creator_sync_error`（Cookie 导入路径是同一事实的另一个入口）。
- 前端：勾选状态用 ref 读取（不触发会话重建与轮询重建）；三种登录面板都把同步结果显示为
  **成功提示**（含 Creator 昵称）或**可读的失败原因**；确认后复选框置灰并说明原因。

> **规律**：`except Exception: return None` 在「可选步骤」上是**最坏的写法**——它把
> 「这一步没必要做」与「这一步做失败了」压成同一个值。可选步骤可以**不阻塞主流程**，
> 但必须**留下结果**（本项目先例：`_extract_goods` 返回 `None` 时前端不渲染，
> 但页面级/文档明确交代原因，而不是假装成功）。

#### 2.4.2 二维码登录的「会话身份不变式」（2026-09-18 修订 · DEF-038）

扫码登录是**跨设备**流程：浏览器出一张码，手机扫它并确认，浏览器轮询同一个会话。
因此面板必须满足一条不变式：

> **屏幕上那张二维码所属的会话，必须就是正在被轮询的会话。**

违反它的表现极具迷惑性：手机明明点了「确认登录」，浏览器**既不报错也不前进**，
后端也查不到任何账号（`login_sessions.status` 永远停在 `pending`）。

**这条不变式曾经被两处独立破坏**（DEF-036 / DEF-038，同族复发）：

| # | 破坏方式 | 表现 | 约束 |
|---|---|---|---|
| 1 | 会话创建 effect 依赖了「同步 Creator」勾选值 → 勾选即建新会话 | 码被静默换掉，轮询转向无人扫过的新会话（DEF-036） | **偏好不得有副作用** |
| 2 | 轮询响应**无条件**写回 state（`{...polled, 保留当前图片}`）→ 旧会话的响应把 `session_id` 拉回旧值，图片仍是新码 | 屏幕是 QR-B，轮询器盯着 A（DEF-038） | **无身份的响应合并必须被禁止** |

**实现约束（三层，删掉任一层都能复现）**：

| 层 | 机制 | 作用 |
|---|---|---|
| 一次挂载只建一个会话 | `initialisedForRef` 记住已初始化的 `accountType`（按**值**而非布尔） | 抵消 `React.StrictMode` 在 dev 下把 effect 调两次；PC↔Creator 真切换仍会新建 |
| 被取代的创建不得上屏 | `createGenerationRef` 单调递增，响应回来时 `generation` 过期即丢弃 | 「刷新二维码」/切换类型时，旧请求的迟到响应不覆盖新码 |
| 跨会话的响应必须丢弃 | 轮询响应先判 `polled.session_id === activeSessionIdRef.current`；写 state 时在 updater 内再判 `current.session_id === sessionId`，不满足**原样返回 `current`** | 把「身份」与「内容」锁在同一份数据上，从根上消除漂移窗口 |

> **为什么这不是「调时序」就能修好的**：竞态只在时序抖动时出现，所以它会表现为
> 「以前偶尔能绑上，现在几乎绑不上」。**按不变式修**——凡是不属于当前会话的数据一律丢弃——
> 才能与调度无关地成立。同理，**异步合并必须带身份**：`{...response, 保留当前某字段}`
> 这种写法本身就是缺陷形态。

> **验证方式（本项目首次采用「变异验证」）**：`tests/frontend/qr_session_identity.cjs` 用 jsdom +
> **真实组件** + React StrictMode + 打桩 api 跑三条场景；把三个守卫在 esbuild 加载期就地改成恒假后，
> 同一套断言必须变红（实测 `createPc` 1→2、上屏变成迟到的旧响应、轮询目标 `[301]→[301,999]`）。
> **断言全绿不足以证明守卫必要；「删掉守卫就变红」才算。**

### 2.5 `/platforms/xhs/discovery` 笔记发现

| 项 | 内容 |
|---|---|
| 布局 | 搜索表单（PC 账号 + 关键词 + 多维筛选 + URL 直查）+ 结果卡网格 |
| 筛选维度 | 排序、类型、时间、范围、距离、地理位置 |
| 详情 | Drawer：无水印原图、互动数据、评论区预览 |
| 入库 | 单个/批量保存到内容库，已保存项打标 |
| 数据 | `searchXhsNotes` / `fetchXhsNoteDetail` / `fetchXhsNoteComments` / `saveXhsNotesToLibrary` / `fetchSavedNoteIds` |
| 缓存 | ✅ 路由 keepalive（保留结果） |

### 2.6 `/platforms/xhs/crawler` 数据抓取

| 项 | 内容 |
|---|---|
| 布局 | 抓取表单（账号 + 模式 + URL/关键词 + 分页/数量/间隔 + 选项）+ 结果 Table |
| 三个模式 | 批量 URL / 关键词搜索 / 评论抓取 |
| 流式进度 | **SSE**：`fetch('/api/xhs/crawl/data')`，逐条推送进度（`lib/api.ts:401`） |
| 选项 | `save_to_library`（是否入库，**请求级参数，不持久化**） |
| 导出 | Excel 导出 + 素材本地下载 |
| 缓存 | ✅ 路由 keepalive |

### 2.7 `/platforms/xhs/keywords` 关键词组

左表单卡（新建/编辑）+ 右分组列表（名称、关键词、命中数、互动量）。数据走 `keyword-groups` 五个端点。

### 2.8 `/platforms/xhs/analytics` 数据洞察

| 项 | 内容 |
|---|---|
| 布局 | ⚠️ 顶部「模块开发优化中」Alert + PageHeader（生成报告/刷新）+ 4 张指标卡 + 两处列表（高潜内容 Table / 话题与评论洞察） |
| 数据 | `fetchXhsOverview` / `fetchXhsTopContent` / `fetchXhsHotTopics` / `fetchXhsCommentInsights` / `createXhsAnalyticsReport` / `downloadExportFile` |
| 状态 | 后端接口是**真实 DB 实现**，前端自述为基础版 |
| ⚠️ 未接线 | 后端还有 `engagement`、`keyword-trends` 两个端点，前端无 UI |

### 2.9 `/platforms/xhs/benchmarks` 竞品监控

| 项 | 内容 |
|---|---|
| 布局 | PageHeader + 添加目标卡（笔记链接 + 刷新间隔）+ 目标卡片网格（2 列） |
| 数据 | 复用 monitoring 端点：`createMonitoringTarget` / `deleteMonitoringTarget` / `fetchMonitoringTargets` / `refreshMonitoringTarget` / `fetchMonitoringSnapshots` |
| ⚠️ 缺口 | 无「暂停/恢复」与「编辑」入口 |

> 说明：`monitoring-page.tsx`（另一个监控页，401 行）被 `router.tsx:20` import 但**没有任何 `<Route>` 使用** → **孤儿页面**，竞品监控能力实际由本页承载。

### 2.10 `/platforms/xhs/image-studio` 图片工坊（**已被 §2.22 取代**，原设计保留）

| 项 | 内容 |
|---|---|
| 布局 | PageHeader + 生成/描述区（Row）+ 底部 Tabs（AI 图片资产 / 用户图片）+ 选图 Modal |
| 数据 | `fetchGeneratedImageAssets` / `deleteGeneratedImageAsset` / `fetchUserImages` / `deleteUserImage` / `generateImageWithAi` / `describeImageWithAi` / `uploadAssetFile` |
| 能力 | AI 生成（支持参考图）、图片理解（转文字）、资产管理与删除 |

> ⚠️ 2026-09-25：本页**重构为面板组件** `ImageStudioPanel`（`image-studio-panel.tsx`），
> 作为「笔记工坊 → 图片工坊」页签的内容；独立路由与 PageHeader 已移除
> （现口径见 §2.22，设计 13 §36）。

### 2.11 `/platforms/xhs/video-studio` 视频工坊（**已被 §2.22 取代**，原设计保留）

**空壳页**（37 行）：PageHeader + Empty 卡「视频工坊即将上线」。无数据调用。

> ⚠️ 2026-09-25：重构为 `VideoStudioPanel`（`video-studio-panel.tsx`）—— 只保留
> 「即将上线」Empty 卡，作为「笔记工坊 → 视频工坊」页签（现口径见 §2.22）。

### 2.12 `/platforms/xhs/library` 内容库

| 项 | 内容 |
|---|---|
| 布局 | 标题行 + 统计卡 + 筛选卡（关键词/标签/**发布状态**/有素材/有评论/视图切换/重置）+ 批量操作卡 + 主视图（Table 或卡片网格）+ 详情**弹窗** |
| 双视图 | 卡片 / 列表切换 |
| 批量操作 | 打标签、批量建草稿、批量导出 |
| 数据 | 13 个端点（`fetchSavedNotes`、`fetchSavedNote`、`fetchSavedNoteAssets`、`fetchSavedNoteComments`、`fetchTags`、`createTag`、`batchTagNotes`、`batchCreateDraftsFromNotes`、`createDraftFromNote`、**`updateSavedNotePublishStatus`**（§32.3.3 取代 `updateSavedNotePublished`，函数数不变）、`exportSavedNotes`、`downloadExportFile`、`deleteSavedNote`） |

**发布状态工作流（v1.4 新增 / v1.5 收敛界面 / 2026-09-23 §32.3 收敛为单一状态）**

内容库同时装两类笔记，**`notes.publish_status` 是唯一的发布状态字段**（`published` 降级为它的投影；字段语义见 `03_数据模型设计.md` §4.1）：

| 项 | 设计 |
|---|---|
| 标识来源 | **落库链路决定，不在前端猜**：笔记同步链路（creator / ark 两源）落库 ⇒ `published`；发现页采集保存与监控采集 ⇒ `pending`；商品智能生成 ⇒ `pending`（详见 §32.3.2） |
| 统计卡 | **六卡一行（`flex="1 1 0%"` 等宽 + 统一内容高度）**：「已发布」/「待发布」/「发布失败」三张取列表响应的 `stats`（**全库口径**：只按 `user_id + platform` 统计，不叠加任何筛选，三数之和 = 该用户笔记总数）+「已保存笔记（/ 已选 N）」+ **发布进度** + **同步进度**（两张进度卡是**当日三数**：成功 / 失败 / 待处理，右下角小字「仅当日数据」，数据来自快照 `today` —— 当日 `tasks` 行现算，见设计 13 §31.5 / §35.4；2026-09-26 用户口径）。⚠️ 不得用 `items.length` 反推 —— 那会把「本页条数」当成「全库条数」 |
| 筛选 | 筛选卡「发布状态」`Select` 四档（全部 / 待发布 / 已发布 / **发布失败**），值传 `GET /notes?publish_status=`；未知值后端 400（不猜）；「重置」一并清空 |
| 展示 | **卡片与详情弹窗都只渲染一个状态标签**（`PublishStatusTag`，纯逻辑在 `lib/note-publish-gates.ts`）：`failed` ⇒ **红色 + Tooltip 摆 `publish_error` 原文**（长文案 `…` 收起 + Tooltip、不换行，ADR-018）；表格视图：独立「发布状态」列。（旧版「封面右上角绿/橙 Tag + 另一个『发布失败』tag」的**并排双状态已被取代** —— 两个标签可能互相矛盾） |
| 手动切换 | 卡片/详情内的**同一个** `PublishStatusTag` 可点切换（·待发布 ⇄ 已发布），调 `PATCH /notes/{id}/publish-status`，**就地更新列表行与统计卡**（不整表刷新）；**本地生成笔记只读**（后端 400 `NOTE_STATUS_MANAGED`，界面不给入口）；失败沿用失败三分法，不弹通用 toast。⚠️ 同步回来的笔记改成「待发布」后**下一轮同步会升回已发布**（这是同步落库的定义，不是 bug）—— **该口径只留在文档，界面上不写这行提示**（2026-09-22 用户口径：机制说明属噪声） |
| 来源账号 | 表格新增「来源账号」列（`account_name`，取自笔记行的 `platform_account_id` 对应账号昵称）；卡片仅在 `account_name` 与 `author_name` **不同**时显示「来源：X」；详情弹窗加「来源账号」项（`Tag`）。**空则整项不渲染**（缺值口径，同下行）。口径见设计 13 §19.13.5 |
| 正文 / 图集补齐 | 列表接口**不带正文**，故已发布笔记进内容库时正文可能为空、图集只有封面。打开详情弹窗时若 `content` 为空（或点右上角「刷新」）⇒ 调 `fetchSavedNote(id, silent, **hydrate=true**)` **即时补齐该条**，再拉素材；补齐后正文空态用一行原因区分「取回过但确实没有正文」与「这次没取到（原因）」，非泛化文案。机制见设计 13 §19.14（2026-09-23 起同步链路页内联补齐，账号级队列已移除） |
| 缺值口径 | 作者昵称取不到时**整项不渲染**，不写「未知」充数（硬约束⑤：没数据 ≠ 有数据；与 DEF-058 失败三分法同源）。`note_id` 这类**非计数**标识仍按既有口径渲染 `—` |
| 详情弹窗 | 由 `Drawer` 改为**弹窗**，与「我的笔记」共用 `components/platforms/note-detail-modal.tsx`（同一版式一份实现）。**合并两边的功能，不丢任何一个**：左侧媒体区取**本地已下载素材**（`asset_urls`，空时回退 `cover_url` / CDN 直链）；右侧保留内容库原有能力（标签增删改、复制标题正文、加入草稿工坊、AI 改写、删除、原始 JSON 折叠）+ 我的笔记侧的元信息版式（类型 / 笔记 ID 可复制 / 作者 / 发布时间 / 互动四项 / 作品链接）+ 评论区（数据取本地 `fetchSavedNoteComments`，**渲染共用同一份评论组件**） |
| 失败呈现 | 与「我的笔记」同一套**三分法**（设计 12 §17.9）：有数据→渲染 / 没数据→空态 / 预期内取不到→一行安静说明 / 真·接口异常→红块 + 真实原因。原内容库的 `Alert type="warning"` 泛化文案**被取代** |

> **筛选条件与账号选择落本地缓存**（2026-09-26 用户口径）：8 项筛选（关键词 / 话题 /
> 发布状态 / 来源账号 / 笔记类型 / 商品 ID / 有素材 / 有评论）走 `usePersistedFilter`
> （`lib/filter-cache.ts`），下次进页面自动回填；来源账号缓存**在候选账号到手后自愈**
> （账号被删 ⇒ 退回默认）。契约见 §3.7。

### 2.13 `/platforms/xhs/drafts` 草稿工坊（**已被 §2.22 取代**，原设计保留；最复杂页面）

| 项 | 内容 |
|---|---|
| 规模 | 1377 行（`rewrite-page.tsx`；2026-09-25 §36 起为 `drafts-workshop-panel.tsx`） |
| 双模式 | Segmented 切换「改写模式」/「生成模式」 |

**改写模式：三栏布局**

```
┌── 草稿队列 ──┬────── 主编辑区 ──────┬─── AI 助手 ───┐
│ 卡片列表      │ 草稿内容（只读原文）  │ 改写指令输入   │
│ 状态/时间     │ 编辑器（标题+正文）   │ 参数折叠面板   │
│              │ 改写结果对比          │ 触发按钮      │
│              │ 素材优化（拖拽排序）  │              │
└──────────────┴──────────────────────┴──────────────┘
```

**生成模式**：居中卡片「AI 笔记生成」（输入主题/参考/指令 → 直接产出笔记）。

| 能力 | 实现 |
|---|---|
| AI 改写正文 | `rewriteDraftWithAi` |
| AI 生成笔记 | `generateNoteWithAi` |
| 生成标题候选 | `generateTitleOptions` |
| 生成标签 | `generateTagOptions` |
| 素材排序 | **@dnd-kit** 拖拽，`reorderDraftAssets` |
| 素材来源 | 从内容库引入 / 上传本地 / 从图片资产选 / AI 润色生成 |
| 送发布 | `sendDraftToPublish` → 创建 `publish_job` |
| ⚠️ 缺口 | 「视频润色」入口为禁用态（`:1143`） |

> ⚠️ **重复实现警告**：`drafts-page.tsx`（1250 行）与 `drafts-workshop-panel.tsx`（原 `rewrite-page.tsx`）都导出过 `XhsDraftsPage`；2026-09-25 §36 起后者更名 `DraftsWorkshopPanel`、由「笔记工坊」页引用，而 `drafts-page.tsx` **无任何文件引用** ⇒ **死代码**。改草稿功能务必确认改的是 `drafts-workshop-panel.tsx`。

> ⚠️ 2026-09-25 §36：本页**重构为面板组件** `DraftsWorkshopPanel`
> （`drafts-workshop-panel.tsx`，原 `rewrite-page.tsx`），作为「笔记工坊 → 草稿工坊」
> 页签；独立路由与 PageHeader 已移除（现口径见 §2.22）。**死代码 `drafts-page.tsx`
> 本轮不动**（FE-1 保持）。

### 2.14 `/platforms/xhs/publish` 发布中心

| 项 | 内容 |
|---|---|
| 布局 | PageHeader + 双栏（左：发布任务 List 带状态筛选；右：预览 + 参数卡 + 工具栏） |
| 预览 | 标题、正文、素材缩略图 |
| 参数 | 发布账号（仅 creator）、可见性（`is_private` / `privacy_type`）、发布模式（立即/定时 + 时间） |
| 操作 | 保存参数、删除、发布 |
| 数据 | `fetchPublishJobs` / `fetchPublishJob` / `fetchPublishAssets` / `updatePublishJob` / `publishJobToCreator` / `deletePublishJob` |
| ⚠️ 未接线 | 后端有 `retry` / `cancel` / 素材增删上传端点，前端仅定义了部分 |

### 2.15 `/platforms/xhs/auto-ops` 自动运营

| 项 | 内容 |
|---|---|
| 布局 | PageHeader + 任务卡网格（3 列：关键词 Tags / 已发布数 / 下次运行时间）+ 新建编辑 Modal |
| 配置项 | 名称、关键词组、采集账号（pc）、发布账号（creator）、AI 指令、调度类型（manual/daily/weekly/interval）、时间、间隔小时 |
| 操作 | 新建 / 编辑 / 删除 / 立即执行 / 暂停恢复 |
| 数据 | `fetchAutoTasks` / `createAutoTask` / `updateAutoTask` / `deleteAutoTask` / `runAutoTask` |

### 2.16 `/platforms/xhs/:section` 兜底页

通配路由（`router.tsx:76`），渲染 `Result` 占位（50 行）。常见 section 有静态文案映射。

### 2.17 `/tasks` 任务中心

> **2026-09-24 重构（设计 15 §9.1）**：从「一张执行记录表 + 只读状态卡」扩为按
> **任务类型体系**（设计 15 §3.1）组织的四页签。

| 项 | 内容 |
|---|---|
| 布局 | PageHeader（刷新）+ 调度状态卡（运行状态 / **时区** / 注册任务 / 任务行计数）+ Tabs × 4 |
| **Tab1 一次性任务** | 手动触发、跑一次即止的执行记录（`tasks` 表：同步笔记 / 同步商品 / 抓取 / AI / 发布 / 导出…）。保留取消 / 重试 / 执行到期任务 |
| **Tab2 定时任务** | `scheduled_jobs` 且 `trigger_type="at"` 的行（指定时刻执行一次，成功自动办结）。操作：暂停·恢复·执行一次·编辑·删除 |
| **Tab3 周期任务** | `trigger_type ∈ {cron, interval}` 的行（内置系统任务 + 用户自建）。列：名称/分组/调用目标/触发器/错过策略/并发/下次/上次/成败数/状态。builtin 行禁删除、禁改 key/params |
| **Tab4 调度日志** | 每次执行一行（running/success/failed/skipped）。筛选（状态/任务/时间范围）+ 批量删除 + 清空 |
| 新建/编辑弹窗 | `components/schedule/job-form-modal.tsx`：名称 + 调用目标（注册表下拉）+ 调度字段（`schedule-fields.tsx`：定时一次/cron/interval + 错过策略 + 并发 + 启用开关）+ 参数 + 备注 |
| cron 构建器 | `components/schedule/cron-builder-modal.tsx`：预设一键填充 + 六字段点选 + 手输；合法性走后端实解析（`POST /api/schedule/cron/validate`，未来 3 次触发时间预览） |
| 文案单一来源 | `lib/schedule.ts`（分组/触发器/错过策略/并发/任务状态/日志状态/触发方式 + `triggerSummary`） |
| 数据 | `fetchScheduleJobs` / `createScheduleJob` / `updateScheduleJob` / `deleteScheduleJob` / `changeScheduleJobStatus` / `runScheduleJobOnce` / `fetchScheduleLogs` / `deleteScheduleLogs` / `cleanScheduleLogs` / `validateCron` / `fetchScheduleStatus` / `fetchTasks` / `cancelTask` / `retryTask` / `runDueTasks` |
| 旧形态（被取代） | ~~调度状态只读展示，无法在 UI 上启停~~ → 调度行可暂停/恢复/改触发器；总开关 `scheduler.enabled` 仍只能改配置（状态卡如实报告 enabled/running） |

### 2.18 `/models` 模型配置

| 项 | 内容 |
|---|---|
| 布局 | PageHeader + 推荐服务商 Alert + 双栏（左新增/编辑卡，Segmented 切文本/图片；右模型列表卡） |
| 配置项 | 名称、服务商、模型名、base_url、API Key |
| 操作 | 新增 / 编辑 / 删除 / 设为默认 / 连通性测试 |
| 数据 | 6 个 model-configs 端点 |

### 2.19 `/settings` 设置

静态页：**只剩「安全边界说明」一张卡**。无数据调用。
**2026-09-26 移除「项目声明」卡**（用户口径「设置页面移除项目声明」）：开源警告 `Alert`
+ 四条禁止条款整卡删除，`WarningOutlined` / `Alert` 导入随之下线；`Row` 里只剩一个
`Col xs={24}`（原两张卡各 `lg={12}`，删一张后不补会留半行空白）。守卫
`test_settings_page_no_longer_hosts_the_donation_module` 的正向锚从「两张幸存卡」
改成「只剩安全边界」（仍要锚，否则删空整页也能变绿），并对声明文案做反向断言。
**2026-09-24 移除「为爱发电」模块**（用户口径「全部移除」）：原页面还有一张赞赏码卡片（微信 / 支付宝收款码，走 `/api/files/media/*` 硬编码路径，`settings-page.tsx:60,70`），是该路径的唯一使用者；随模块一并删除，`HeartOutlined` / `Image` 导入与 `author/` 下两张收款码（按 `.bak-20260924-donation-removed` 惯例隔离）同批清理。

### 2.20 `/platforms/xhs/my-notes` 我的笔记 **（2026-09-18 新增 · 已实现）**

| 项 | 内容 |
|---|---|
| 文件 | `frontend/src/pages/platforms/xhs/my-notes-page.tsx` |
| 需求/设计 | `01_需求文档/03_需求规格_我的笔记列表.md`、`02_设计文档/12_功能设计_我的笔记与互动数据.md` |
| 定位 | 对标平台 App 的「我」页面：读**当前账号自己**的内容与互动 |

**结构与交互**（v1.1 起账号不再限 `pc`，改为**能力驱动**；v1.3 正文见 §12 设计文档）：

```
账号选择器（列出全部 xhs 账号，标签区分 PC / 创作者）
  │
  ├─ 账号级说明（当前链路能力：可用 Tab / 不可用原因）
  ├─ 身份卡（昵称 / 小红书号 / IP 属地 / 头像）
  │
  └─ Tabs × 4（自然隔离，按需懒加载，切换才请求）
       ① 我发布的   → POST /users/notes        （PC 或 创作者链路，按能力选源）
       ② 我赞过的   → POST /users/liked        （仅网页会话账号）
       ③ 我收藏的   → POST /users/collected    （仅网页会话账号）
       ④ 互动消息   → POST /users/interactions （仅网页会话账号）
            └─ 内层 Segmented 切换 kind：赞和收藏 / 提及 / 新关注
```

| 细节 | 设计 |
|---|---|
| 能力探测 | 账号切换后先调 `POST /users/capabilities`（**纯本地、零平台请求**）；失败即视作「能力未知」→ 全部 Tab 保持可用，由后端在真正请求时就地报错（不在前端复制路由规则） |
| Tab 置灰 | 不可用时**置灰 + Tooltip 说明原因**（不隐藏、不静默） |
| 卡片 | 封面（视频叠播放角标）+ 类型标签 + **仅点赞**（列表接口唯一提供的指标）+ 原链接。**不渲染正文**、**不渲染收藏/评论/分享**（列表拿不到，渲染 0 等于假数据） |
| 详情 **Modal** | `80vw` / `~80vh` 自适应，主体**上下可滚动**；**左右两栏**：左栏媒体（画廊多图翻页 + 缩略图 / 视频 `<video controls>` / 下载），右栏内容（正文、**蓝色话题**、元信息、互动四项、原始链接、商品卡、评论懒加载）。**v1.14 起本弹窗抽出为共用组件** `frontend/src/components/platforms/note-detail-modal.tsx`，与内容库共用同一份版式（见 §2.12）——**同一版式不再留两份实现** |
| 素材下载 | `saveRemoteAsset()`：`fetch → blob → 下载`，失败回退 `target="_blank"`（不做服务端转发，不给后端加带宽） |
| 「加载更多」（**v1.4 已被取代**） | 原为显式按钮。v1.4 起**移除按钮**，改为滚到底部自动加载下一页（`IntersectionObserver` + `rootMargin: 240px`）：一页一请求、in-flight 去重、**失败即停 + 显式「重试」**、无自动重试。契约见 `12_功能设计_我的笔记与互动数据.md` §20 |
| 关键词搜索（**v1.4 新增**） | Tab 内容区顶部 `Input.Search`：跨「我发布的 / 我赞过的 / 我收藏的」检索，结果卡带**来源标签**，并显示每类已扫描页数与命中数；creator 链路置灰 + 原因。契约见 `12_功能设计_我的笔记与互动数据.md` §22 |
| 令牌降级 UI | 后端返回 `code=XSEC_TOKEN_REQUIRED` 时，页内出现「粘贴主页 URL」输入框（L1 降级入口） |
| 空态 | 明确区分「没有内容」与「拉取失败」，不吞错误 |
| 商品区（**v1.6 重做**） | **未绑定千帆**：一条 `Alert`（来源说明 + 原因）+ 「绑定千帆（读取挂载商品）」按钮 → 弹窗粘贴千帆 Cookie；**已绑定**：`商品来源：千帆` 标签 + **`已按需补齐 N/M 条商品笔记`（v1.12；M 未知时只显示 N）+ 同步时间**（**v1.13 移除**该行尾部的「（打开笔记时自动补齐；也可点「同步商品」批量补）」——按需补齐是**默认行为**，不需要在页面上解释）+ `巡检 MM-DD HH:MM`（v1.11）+「立即巡检」+「同步商品」+「解除绑定」；绑定 `invalid` 时多一条告警 + 「重新绑定千帆」。**v1.14 / DEF-040**：这一行的**数字与时间必须取自「每次加载笔记」带回的 `goods_source`**（`goodsSource ?? capabilities.goods`），否则浏览时按需补齐的商品不计入数字，只有点「同步商品」才跳到位。**详情内商品链接以文本展示、点击即复制**（不跳转，D17）；**价格 v1.10 起展示**（如 `¥ 54.40~144.00`），拿不到价时该行仍不渲染 |
| 页面级长说明（**v1.7 移除**） | 创作者账号原来那条「该账号由创作服务平台登录…」不再出现；能力边界由「无网页会话」标签 + 置灰 Tab 的悬浮原因就近承担（D11 修订） |

**关键取舍（用户已确认）**：

| 决策 | 选择 | 理由 |
|---|---|---|
| 数据落地 | 笔记与互动**纯实时拉取，不落库**；**千帆商品映射例外** | 自有内容变动频繁且已有平台侧真相源；商品映射是「另一平台的按页列表 + 无 note_id 查询口子」（§24.1），不落库就得每次浏览都翻千帆的页 |
| 素材策略 | **引用平台 CDN 直链 + 按需下载** | 不为展示而占用本机存储与带宽 |
| 链接 | **原笔记链接 + 商品链接都要** | 运营需要「回到平台看」与「看挂了什么货」两个视角 |
| 商品数据源 | **千帆商家后台（`ark`）**，✅ 已接线 | PC / 创作者两条链路都不返回该字段（§19.0 三层实测）；登录态不可复用，需单独导入 |
| 创作者账号 | **支持「我发布的」** | 全部小红书账号都能发布笔记；赞藏互动属网页会话功能，置灰并说明（D5 / D9） |
| 千帆指标的呈现 | 弹窗内「千帆数据」一行（阅读 / 近 30 日阅读 / 支付金额 / 支付订单数） | 「创作者账户数据全部接入千帆」：千帆给到的字段全部透出，**缺值不渲染**（不是 0） |

> 完整决策清单见 `01_需求文档/03_需求规格_我的笔记列表.md` D1–D12；
> 字段保真度与商品调研结论见 `02_设计文档/12_功能设计_我的笔记与互动数据.md` §17–§19。

> **账号选择 / 页签 / 互动 kind 等落本地缓存**（2026-09-26 用户口径）：5 项
> （账号 / 页签 / 互动 kind / 无流量阈值 / 无流量查询）走 `usePersistedFilter`
> （`lib/filter-cache.ts`），下次进页面自动回到上次的账号与页签；账号缓存**在账号清单
> 到手后自愈**（账号被删 ⇒ 退回首个可用账号）。契约见 §3.7。

---

### 2.21 `/platforms/xhs/shop-items` 商品列表 **（2026-09-19 新增 · 已实现）**

| 项 | 内容 |
|---|---|
| 文件 | `frontend/src/pages/platforms/xhs/shop-items-page.tsx`（含 `XhsShopItemsPage`） |
| 需求/设计 | 需求 `01_需求文档/02_二开需求规格_商品笔记自动化.md` **R6**、设计 `02_设计文档/12` **§25** |
| 定位 | 店铺商品**本体**的只读视图（与「我的笔记」的分工：那边是「笔记挂了什么商品」，这边是「店里有哪些货」） |
| 数据源 | 千帆 `POST /api/edith/product/search_item_v2` → 本系统 `POST /api/xhs/qianfan/items` |

**结构与交互**：

```
账号选择器（**列出所有小红书账号，不按类型过滤**；v1.32 / §30.1）
  │     选项标签：`昵称 · 店铺名` / `昵称 · 未绑店铺`（账号 ↔ 店铺 **1:1**，v1.32）
  │
  ├─ 前置校验（v1.34 / §33.4）：**已绑店铺 ∧ 千帆令牌不是「确定不可用」**才继续
  │     ├─ 不通过 → **一个 Alert**：「请在账号信息卡片中扫码登录千帆后再来查看。」
  │     │     + 「去『账号矩阵』扫码登录千帆」/「绑定千帆（粘贴 Cookie）」
  │     │     ⇒ **一个请求都不发**，搜索区与台账都不渲染（旧的三步 Cookie 长说明已移除）
  │     └─ 通过   → 状态提示行（选中「本地无法区分的审核态」时一行安静说明）
  ├─ 搜索条件（**恰好两行**，按钮组在第二行末尾右对齐；v1.34 / §33.2）
  │     第一行：关键词（名称 / 商品 ID / 货号，逗号分隔，回车即搜）· 状态 · 价格下限 · 价格上限 · 创建时间区间
  │     第二行：库存下限 · 库存上限 · 销量下限 · 销量上限 · **挂链笔记数（比较符默认「小于」+ 篇数，留空不筛）** ·
  │             **搜索 / 重置 / 刷新**
  ├─ 商品台账（本地 `shop_products`，**零平台请求**；v1.32 / §30.3）
  │     卡头右侧：**批量生成智能笔记**（v1.33）· **批量删除**（v1.36，danger，**只删本地台账行**）
  │     勾选**跨页保留**（`preserveSelectedRowKeys`），但换查询条件 / 换账号**清空**（v1.36 / §33.10.2）
  └─ 结果表（服务端分页 20/50/100；**只有「上架时间」列可排**、且**默认不排序**（进来表头无箭头，点一下才排：升 → 降 → 取消，**一律由后端排**）；v1.37 / §33.11）
        主图（可预览）· 标题 · 商品 ID（点击可复制）· 货号 · 分类路径（`…` 收起 + Tooltip，v1.34）·
        价格区间 · 库存（+ 在售 SKU + 库存告警）· 近 30 天与累计销量 ·
        挂链笔记数（**关系表口径**，可点 → **弹窗**：Modal + Tabs 展示笔记详情；v1.33 / §31.6；
        尺寸 **75vw × 75vh**（v1.36 / §33.10.3））·
        上架时间 / 状态 · 底部 Descriptions（商品来源 / 店铺 / 状态口径 / 命中总数 / 当前排序 / 台账同步时间 / 平台商品总数）
```

| 细节 | 设计 |
|---|---|
| 金额口径 | 接口出入参：**入=元、出=分**；展示层 `/100` 保留两位。**换算只在一处**（后端 `_price_fen()`），避免两头各乘 100 的静默错值 |
| 状态筛选 | 8 个取值与千帆 tab 一一对应，**未知值后端 400 并返回支持列表**（不悄悄退化成「全部」） |
| 空态区分 | 「当前条件下没有商品」/「点『搜索』拉取商品列表」/「绑定千帆后可查询」/「拉取失败（带原因）」四态分明，不吞错误 |
| 排序 | **只有「上架时间」列带箭头**（v1.34 / §33.3），且**默认不排序**（v1.37 / §33.11）：进来无箭头、不带 `sort_field`（后端用它自己的稳定默认序），点一下才排（升 → 降 → **取消**，三态与 antd 原生一致）；`sorter: true` + 受控 `sortOrder` ⇒ **antd 不做本地排序**（`sorter` 传比较函数才会，那会变成「只排当前页」的假排序）。生效口径仍由后端回传（`sort_field` / `order`），底部「当前排序」按回传值显示（v1.33 / §32.1） |
| 不落库 | 列表**只读本地台账**（「同步商品」任务落库）；时效由底部「台账同步时间」明示（v1.32 / §30.3） |
| 写操作边界 | **没有平台写入口**：新增 / 编辑 / 上下架一律不做（C6）。⚠️ 原句为「页面**没有**任何商品写操作入口」（v1.32），**被 v1.36 取代**：现在有**一个本地写入口** —— 「批量删除」只删 `shop_products` 行（§33.10.2）；平台商品**不动**。⚠️ 「`note_goods` 关系**都不动**」这句是 v1.36 原文，**已被 2026-09-23 用户口径推翻**（「删除商品时也需要删除笔记与商品的关联关系」）⇒ 现在**关系行一并删**（§33.10.2 / §26.7.5） |
| **批量删除（v1.36）** | 卡头 danger 按钮，勾选后弹确认框（正文由 `lib/shop-items-view.ts::batchDeleteHint()` 生成，**必须**讲清两个连带后果：**关系行一并删** / 平台仍有 ⇒ 下次「同步商品」会写回）。上限 **200/次**（空 / 超限在**发请求前**就拦住，后端另有 400 硬闸且**不静默截断**）；回参 `deleted` 是 `rowcount`，与 `requested` 分开显示（§33.10.2） |
| 商品单元格排版 | 三类长内容一律**不换行**（换行会把整行商品卡撑高、右侧几列基线错开）：标题（v1.35）、分类路径（v1.34）、图数角标改贴图片（v1.35）。缩略图尺寸只住一个常量 `ITEM_THUMB_SIZE`，真图与无图占位块共用 |
| **挂链笔记反查（v1.9 / v1.33 改版）** | 点「N 篇」打开**弹窗**（Modal + 多篇用 Tabs 自然隔离）：**零平台请求**（走 `note_goods` 关系表 join `notes`；⚠️ 2026-09-27 起关系按**平台 ID** 挂、**不再要求台账里有这件商品**，见设计 13 §26.7），内容区与内容库详情弹窗**共用同一份两栏版式**（`NoteDetailBody`）。⚠️ 弹窗顶部的「本地已映射 M/N」说明（v1.34）与列表该列的 Tooltip（v1.34，§33.1）**均已移除**：界面只剩「一个可点的篇数」，两个口径（关系表 / 平台原文 `platform_note_count`）的对照**只在响应里**，不再占界面 |
| 与「我的笔记」联动 | 关系在**同步笔记（ark 源）**与**「生成笔记」**时自动建立（`note_goods`），不再有独立的「同步挂链映射」按钮（v1.32 已删） |

> **账号选择 / 搜索条件 / 每页条数 / 排序落本地缓存**（2026-09-26 用户口径）：4 项走
> `usePersistedFilter`（`lib/filter-cache.ts`）—— 搜索条件存**整份表单草稿**
> （`ShopItemsFilterDraft`，纯 JSON 形状、日期存 `YYYY-MM-DD`），「重置」回
> `EMPTY_SHOP_ITEMS_DRAFT`。换账号仍清空跨页勾选；账号缓存**在可选账号到手后自愈**。
> 契约见 §3.7。

### 2.22 `/platforms/xhs/note-workshop` 笔记工坊 **（2026-09-25 新增 · 已实现；设计 13 §36）**

| 项 | 内容 |
|---|---|
| 布局 | PageHeader（笔记工坊）+ `Tabs` 三页签：**图片工坊 / 视频工坊 / 草稿工坊**（tab 状态进 `?tab=images\|videos\|drafts`，可分享、刷新保持；默认 `images`） |
| 面板 | `ImageStudioPanel`（§2.10 重构）/ `VideoStudioPanel`（§2.11 重构）/ `DraftsWorkshopPanel`（§2.13 重构）—— 三个面板文件各自持状态，**视图隔离** |
| 挂载语义 | antd Tabs 默认保活：页签首次激活后跨切换不丢状态；未激活页签不渲染（不提前打接口） |
| 旧路由 | `/image-studio`、`/video-studio`、`/drafts` → `Navigate replace` 重定向到本页对应 tab（深链 / 书签不死） |
| 菜单 | 侧边菜单原三项合并为一项「笔记工坊」（`ToolOutlined`，内容库之后、发布中心之前） |
| 关联 | 内容库「加入草稿工坊」跳转改指 `/note-workshop?tab=drafts`；`xhs-section-page.tsx` 的 `image-studio` 死条目移除 |

**为什么合并**（用户口径「合并为一个页面视图 tab 自然隔离」）：三个工坊是同一批工作流
（找素材 → 做图 → 写草稿 → 发布）的连续环节，拆三个菜单项时用户要在侧边栏来回跳；
合成一页后 tab 即隔离，URL 仍可深链到具体工坊。发布中心等处的「草稿工坊」文字引用
**不动**（页签名未变）。

---

## 3. 前端基础设施设计

### 3.1 API 客户端（`frontend/src/lib/api.ts`）

| 项 | 设计 |
|---|---|
| 实例 | `axios.create({ baseURL: "/api", timeout: 120000 })`（`:85-86`） |
| 认证 | 请求拦截器注入 `Authorization: Bearer <token>`（`:127-132`） |
| 401 处理 | 响应拦截器自动尝试刷新 token，失败则跳登录（`:134-158`） |
| 导出规模 | **118 个请求函数** |
| 例外 | 仅 1 处绕过 axios：SSE 抓取用原生 `fetch`（原「设置页图片硬编码 URL」一处随 2026-09-24 「为爱发电」模块移除而消失） |

#### 3.1.1 全局错误提示与「可容忍失败」的关系（2026-09-18 修订）

响应拦截器对**任何**非 401 失败都会弹一次 `message.error(detail)`，`detail` 取自后端响应体：

```ts
if (!originalRequest?._silent) {
  const detail = error.response?.data?.detail;
  const msg = typeof detail === "string" ? detail : "请求失败，请稍后重试";
  message.error(msg);
}
```

**问题**：当一个调用点**故意容忍失败**时，它自己的 `try/catch` 只能拦住 promise，
**拦不住已经被拦击器弹出的 toast**。典型表现是页面上出现一个**裸 HTTP 状态短语**：

> 「刷新页面会异常提示 `Not Found`」

根因链：`/xhs/pc/users/capabilities` 返回 404（后端进程是旧构建，路由不存在）
→ 拦截器弹 `message.error("Not Found")` → 页面自己的 `catch` 把 `capabilities` 置 `null`
并继续正常工作（四个 Tab 保持可用）——**页面行为是对的，toast 是错的**。

**规则（本次确立）**：

| 场景 | 做法 |
|---|---|
| 调用点**把失败当作可接受结果**（能力探测、可选统计、渐进增强） | **必须传 `{ _silent: true }`**，由调用点决定是否展示 |
| 调用点是用户显式操作（提交、删除、发布） | 保持默认：交给全局提示，业务层不重复弹 |
| 401 | 永远由认证流程接管，不受 `_silent` 影响 |

> **`_silent` 不是「静音一切」的开关，而是「这个失败的展示权归调用点」的声明。**
> 与之配套的项目原则是 **错误就地展示**：谁最清楚该不该打扰用户，谁来决定。
> 反例见 `fetchXhsSelfCapabilities`——它已在设计上「失败即保持全 Tab 可用」，
> 却因为漏了 `_silent`，把一个被容忍的 404 变成了页面上的红色 `Not Found`。

> ⚠️ **排障提示**：裸英文 `Not Found` / `Unauthorized` 这类提示，几乎总是
> **后端路由缺失**（前端调了后端不存在的端点）而不是前端 bug。此时先核对
> 运行中进程的路由表（`GET /openapi.json` 的 `paths` 数量），再怀疑代码——
> 开发期最常见的成因是**后端进程没重启**。

### 3.2 路由守卫

| 守卫 | 行为 |
|---|---|
| `ProtectedRoute` | 未登录 → 跳 `/login` |
| `PublicOnlyRoute` | 已登录 → 跳 `/platform-select`（防重复登录页） |

### 3.3 状态管理

**未引入全局状态库**（无 Redux / Zustand / Jotai）。状态靠：
- 页面级 `useState` / `useEffect`
- `useAuth()` 自定义 Hook 管认证
- 需要跨页面保留的用 keepalive

**设计评价**：对当前规模是合理选择；若二开新增大量跨页共享状态（如店铺/商品上下文），需要重新评估。

### 3.4 类型定义

集中在 `frontend/src/types/index.ts`。所有后端实体类型手写维护，**未从 OpenAPI 自动生成** → 后端改字段时前端不会报错。

### 3.5 二开新增前端页面（设计）

| 页面 | 路由 | 布局 | 数据来源 |
|---|---|---|---|
| **我的笔记** ✅ **已实现** | `/platforms/xhs/my-notes` | 账号选择 + 身份卡 + 4 Tab + 卡片网格 + 详情 Drawer | `POST /xhs/pc/users/{me,notes,liked,collected,interactions}`（见 §2.20） |
| **店铺管理** | `/platforms/xhs/shops` | 店铺卡片网格：名称、类型、状态、能力徽标（可挂商品 ✓/✗、待缴保证金） | `GET /shops` |
| **商品库** | `/platforms/xhs/products` | 列表 + 筛选（店铺/状态/类目）+ 同步按钮 | `GET /shop-products` |
| **商品笔记工作台** | `/platforms/xhs/product-notes` | 三栏：商品列表 / 笔记草稿 + 护栏校验结果 / 发布配置 | `product-note-jobs` 系列端点 |
| **系统管理** | `/settings/system` | **开关面板**（见 `10_系统管理功能设计.md`） | `GET/PUT /system-settings` |

**前端原则（重要）**：能力不足时**置灰并给出可读原因**，不要隐藏。隐藏会被当成 bug 报过来。

```
店铺卡片示例：
  个人店    [可挂商品 ✗ 未验证]
  个体户店  [可挂商品 ✓]
  企业店    [可挂商品 ✓] [待缴保证金]
```

### 3.6 全站消息提醒统一规范（2026-09-25 用户口径）

用户口径：「项目中所有消息提醒均使用消息提醒，参考 antd 成熟的组件，**不要在页面暂用空间**」。

**规则（全站唯一口径）**：

| 场景 | 呈现 | 实现 |
|---|---|---|
| **操作结果反馈** —— 用户做一个动作（保存 / 删除 / 发布 / 切换 / 复制 / 生成 / 发送验证码…）后出现的一次性提示，**无论成功、失败还是警告** | antd `message` 轻提示（顶部悬浮、自动消失、**不占版面**） | `const { message } = App.useApp()`（`<App>` 已在 `app/providers.tsx` 挂载）；**禁止**再写 `setMessage`/`setNotice` 状态 + 页面内联 `<Alert>` |
| 静态说明 / 声明 / 免责（模型配置推荐服务商、分析页「开发优化中」；⚠️ 设置页「项目声明」**已于 2026-09-26 按用户口径整卡移除**，不再是本行的例证） | inline（**本就不是提醒**，是常驻内容） | `Alert` / `Card` 照旧 |
| 空态 / 占位（暂无数据、未解析到素材） | inline `Empty` / `quietHint` | 照旧 |
| 表单校验（弹窗内紧贴控件的验证说明） | inline | 照旧 |
| **失败三分法**（设计 12 §17.9：红块 + 真实原因 / 安静一行 + Tooltip） | inline | 照旧 —— 那是「这块数据为什么这样」的结构呈现，不是操作提醒。⚠️ **2026-09-25 细化**：同为「失败」，**操作结果的失败**（点了同步 / 删除 / 切状态，动作没成）是**操作提醒** ⇒ 走 `message`；**结构呈现的失败**（这块数据为什么是空的、这条笔记为什么发布失败）才留 inline。内容库页是本条细化后的样板页，见下方「2026-09-25 迭代」 |

**为什么是 `message` 而不是 `notification`**（两者都不占版面，按 antd 官方「何时使用」分工）：

| 组件 | 官方定位（文档原文） | 本项目用例 |
|---|---|---|
| **`message`** | 「顶部居中显示并自动消失，是一种**不打断用户操作的轻量级提示**。适合全局展示操作反馈信息，且不需要用户立即点击交互的场景」 | ✅ 全部现有场景：一句式操作反馈（「配置已保存。」「删除失败。」） |
| `notification` | 「在系统**四个角**显示通知提醒信息。经常用于以下情况：**较为复杂的通知内容**；**带有交互的通知**，给出用户下一步的行动点；**系统主动推送**」 | ❌ 当前无一例符合 —— 若将来出现「失败原因 + 一键重试按钮」或「后台任务完成推送」，走同一个 `App.useApp()` 的 `notification` 实例，**不新增第二套机制** |

**为什么必须 `App.useApp()` 而不是静态 `import { message } from "antd"`**：antd 6 的静态 `message` 走独立 React root（`es/message/index.js` 的 `GlobalHolder`），**不吃 `ConfigProvider` 的主题与 locale** —— 深色主题（本默认）下会冒出一条浅色 toast；官方文档亦明确「静态方法无法消费 Context，不能动态响应 ConfigProvider 提供的各项配置」，推荐 hooks 或 `App` 组件实例。`App.useApp()` 返回的实例继承 `<App>` 上的主题 / locale / `message` 配置（`duration` 默认 3s、`top` 默认 8，均用默认值，不另配）。

**唯一例外**：`lib/api.ts` 的全局响应拦截器是**非 React 模块**，拿不到 hook，继续用静态 `message.error(detail)`；§3.1.1 的 `_silent` 规则（展示权归调用点）不变。页面 / 组件内**不再新增**静态 message 用法。

**取代关系**：2026-09-24 任务中心「一切提示走 notice 弹窗」（设计 15 §9.1.1 R22）**被本条取代** —— 弹窗比 inline 消息条更占空间（整屏遮罩），`message` 是两条路线的共同终点。原句与演变表行保留在设计 15 并标注。

**本轮转换清单（14 个文件，inline 消息条 → `message`）**：

| 文件 | 原形态 | 现形态 |
|---|---|---|
| `pages/platforms/xhs/publish-page.tsx` | `message`/`error` 双状态 + 2 条 closable `Alert` | `message.success/error` |
| `pages/platforms/xhs/drafts-workshop-panel.tsx`（原 `rewrite-page.tsx`，§36 面板化） | 同上 + 一处静态 `antMessage` | `App.useApp()` 一处收口 |
| `pages/platforms/xhs/auto-ops-page.tsx` | 同上 | 同上 |
| `pages/platforms/xhs/drafts-page.tsx` | 同上 | 同上（死代码，一并转换保持全站一致） |
| `pages/platforms/xhs/benchmarks-page.tsx` | 同上 | 同上 |
| `pages/platforms/xhs/image-studio-panel.tsx`（原 `image-studio-page.tsx`，§36 面板化） | 同上 | 同上 |
| `pages/platforms/xhs/monitoring-page.tsx` | `message`(info)/`error` 双状态 + 2 条 `Alert` | 同上 |
| `pages/platforms/xhs/keywords-page.tsx` | `message` 单状态 + 1 条 `Alert`（成功 / 失败 / 校验复用） | `message.success/error/warning` |
| `pages/models/model-config-page.tsx` | `message`/`error` 双状态 + 2 条 `Alert` | 同上（两条静态信息 `Alert` 保留） |
| `pages/platforms/xhs/library-page.tsx` | `batchActionMessage` / `detailActionMessage` 双状态 + 2 条 `Alert` | `message.success/error/warning`。⚠️ 括注里的「`error` 红块与 `*Failure` 三分法保留」**已被 2026-09-25 迭代取代**（原句保留）⇒ 见下方「2026-09-25 迭代（内容库页）」 |
| `pages/tasks/task-center-page.tsx` | `notice` 状态 + 底部 `<Modal>` 结果弹窗 | `message.*`（`notify()` 助手删除） |
| `components/account/qr-login-panel.tsx` | `notice`/`error` + 2 条 `Alert` | `message.*`（`statusText` 状态行保留） |
| `components/account/phone-login-panel.tsx` | 同左 | 同左 |
| `components/account/qianfan-email-login-panel.tsx` | `error` + 1 条 `Alert` | `message.error` |

**2026-09-25 迭代（内容库页，用户口径）**：用户口径「内容库页……**页面所有提示包括
异常提示，正常提示，成功提示等均实现消息提示组件不要占用页面布局**」。落地分
**两类来源**（由 §3.1.1 的 `_silent` 规则决定「谁报」）：

| 来源 | 谁报 | 依据 |
|---|---|---|
| **非 `_silent`**（`fetchSavedNotes` / `updateSavedNotePublishStatus` / `updateSavedNoteTopics` / `batchSyncSavedNotes` / `batchPublishSavedNotes` / `batchCreateDraftsFromNotes` / `exportSavedNotes` / `deleteSavedNote`） | **全局响应拦截器** | 拦截器本就会对任何非 401 失败弹一次 `message.error(detail)`；调用点再报就是**双弹** |
| **`_silent`**（`syncSavedNote`）+「**200 + 失败态**」（`sync.status !== "ok"`，如 `no_session` / `no_url` / `failed`） | **调用点** `message.error` | 这两条不走拦截器的异常分支；不报就彻底没声 ⇒ 必须有守卫钉住 |

撤掉的四个页面级承载 + 一个列表承载：`error`（列表加载失败 `Alert`）、
`batchFailure`（「批量操作失败」红块 —— 用户看到的那条「批量操作失败 Method Not
Allowed」正是长在这里）、`topicFailure`（「话题保存失败」）、`detailSyncFailure`
（详情里的「同步失败」）、加载更多的失败红块（**只留「重试」按钮本身** —— 它是
控件，不是提示条）。

**边界（明确不搬）**：① `publish_error` 的「发布失败原因」是**落库的持久状态说明**，
重开弹窗还要看（本表第 2 行「静态说明」类）；② 详情弹窗内部的 `failure`
（`renderFailure` 三分法）是 my-notes / 内容库**共用**的唯一实现，回答「这块数据
为什么是空的」。两者都不随 message 化搬走，且各有守卫钉住。

**顺带收敛的 3 处静态 message**（本就用的是 antd message，但静态方法不吃主题；改为同一个 hook，全站只此一种形态）：`my-notes-page.tsx`（约 15 处）、`shop-items-page.tsx`（约 18 处）、`add-account-drawer.tsx`（1 处）。两页的模块级 `copyToClipboard(value, okText)` 拿不到 hook ⇒ 由调用方把 `message` 实例作参数传入（`renderItemNoteInfo` 是模块级函数组件，自己调 hook）。

**守卫**：
- `tests/backend/test_page_optimizations.py::test_transient_feedback_uses_antd_message_not_inline_alerts`（上表 17 个文件逐一断言旧状态 / 旧渲染已除、`App.useApp()` 已接入、`message.*` 真有调用）；
- `::test_static_antd_message_import_only_survives_in_api_ts`（静态 `import { message }` 只允许活在 `lib/api.ts` —— 非 React 模块的 documented 例外）；
- `::test_failure_three_way_blocks_stay_inline_after_the_message_switch`（三分法红块 / 安静一行 / 静态声明**不随 message 化搬走**）；
- `::test_library_page_notices_use_message_and_no_longer_occupy_layout`（2026-09-25 迭代：内容库页四个页面级 inline 承载确实不在 + `_silent` 的同步失败必须由调用点报真原因 + 持久状态说明没被一起扫掉；**6 个变异全部被抓**）；
- `::test_library_batch_sync_with_progress_card`（⑦ 改为新口径：`<Progress` 不得出现、百分比只留 Tooltip、空闲文案不得出现）；
- `tests/frontend/schedule_center.cjs` 的 R22 弹窗断言改写为 message 断言（原句留 docstring）；`add_account_modal.cjs` 的 harness 补 `<App component={false}>` 包裹 —— `App.useApp()` 在 `<App>` 之外返回的是**空对象**（antd 源码 context 默认值），真机不炸只因 `providers.tsx` 有 `<App>`。

---

### 3.7 页面筛选条件 / 账号选择的本地缓存（2026-09-26 新增）

用户口径：「梳理所有商品列表，我的笔记，内容库页，**筛选条件和账号选择后放本地缓存**，
避免每次均需要重新选择」。

**唯一实现 = `frontend/src/lib/filter-cache.ts`**（DEF-044）：三个页面共用这一份 ——
页面只声明「哪个 key + 默认值 + 校验器」，读写 / 容错 / 自愈全在 lib。各页自己拼
`localStorage.getItem` 必然写出三种容错口径（一处 try/catch、一处崩、一处把脏值当合法值用）。

| 项 | 设计 |
|---|---|
| 覆盖面 | **内容库**（8 项：关键词 / 话题 / 发布状态 / 来源账号 / 笔记类型 / 商品 ID / 有素材 / 有评论）、**我的笔记**（5 项：账号 / 页签 / 互动 kind / 无流量阈值 / 无流量查询）、**商品列表**（4 项：账号 / 搜索条件整份草稿 / 每页条数 / 排序） |
| Hook | `usePersistedFilter(key, fallback, sanitize?)` —— **与 `useState` 同形**（`[value, setValue]`），页面只把 `useState(...)` 换成它，其余调用点一字不改；`fallback` 支持**惰性初值**（商品 ID 是「URL 里有就用 URL 的，否则取缓存」） |
| 键形态 | `xhs.filters.<page>.<field>`，**一个字段一个 key**（不按页面存大对象 —— 存大对象就得每次写时把整份拼一遍，漏一个字段 = 那个筛选静默不生效；一字段一 key 时读写的形状与 `useState` 一一对应） |
| 取值域校验 | 缓存值必须过**当前代码认的取值集合**：枚举走 `asEnum` / `asNumberEnum`，数字走 `asFiniteNumber`，对象走 `asFields`（**按默认值形状逐字段校验**，多出的键丢掉、类型对不上回默认）。否则后端改枚举 / 前端改默认值后，旧缓存会变成「界面选了一个后端不认的值」（400 或筛成空集，用户看不出原因） |
| 容错 | **缓存是体验，不是真相**：`window.localStorage` 拿不到（隐私模式 / 被沙箱 iframe 嵌住）⇒ 返回 `null` 不抛；读到坏 JSON ⇒ **静默回默认并顺手删掉那条 key**（免得每次进页面都解析一次坏 JSON）；写失败（配额满）⇒ 静默。**一个坏缓存绝不能把页面打红** |
| 账号自愈 | 账号可能已被删除 ⇒ 缓存里的 id 成了下拉中不存在的裸数字 + 恒空列表。**在账号列表成功到手后校验一次**（失配退回默认并覆盖缓存）—— ⚠️ 校验**必须等到「成功」这个分支**：拿失败时的空数组去校验，会把一个合法缓存账号误清（内容库用 `filterAccountsLoaded` 旗标，另两页用 effect 成功分支） |
| 刻意不缓存 | **分页游标 / 页码**（每次进页面从第一页开始）、详情弹窗状态、列表累加的 `page`、搜索结果本身（关键词只是**条件**，搜不搜由页面决定） |

**商品列表的「搜索条件」是整份表单草稿**（antd Form 不是 `useState`，单独一份读写）：
`ShopItemsFilterDraft` 是**纯 JSON 形状**（日期存 `YYYY-MM-DD` 字符串，不存 dayjs 对象 ——
`JSON.stringify(dayjs)` 出来是 `{"$d":…}` 这类脏值），字段名与千帆请求参数一致
（`price_from` / `note_count_op`…）；`EMPTY_SHOP_ITEMS_DRAFT` 是「重置」的落点，
`sanitizeShopItemsDraft` 做形状 + 取值域（`note_count_op` 走 `NOTE_COUNT_OPS`，
与后端同一份）。`status` 这里**只校验「非空字符串」**：取值域由后端兜底（未知值 400
并回支持列表，不静默降级），前端再抄一份状态白名单就是第二份真相源。

**测试要点**：`tests/frontend/persisted_filters.cjs`（esbuild 打包**真**
`lib/filter-cache.ts`，喂假 `localStorage`，**30** 条断言 + **7** 个变异）+ 
`tests/backend/test_filter_cache.py`（行为脚本接线 1 / 变异 7 逐一实测变红 / 三页每个
筛选都接线 / 页面不许自己碰 `localStorage` / 不许退回裸 `useState` / 账号自愈三页齐备，
共 **15** 条）；脚本已接入 `test_testing_spec_guards.py` 的接线参数化清单。

---

## 4. 前端技术债清单

| # | 问题 | 位置 | 影响 | 建议 |
|---|---|---|---|---|
| FE-1 | 死代码：`drafts-page.tsx`（1250 行） | 未被 router 引用 | 改错文件的概率极高 | 删除 |
| FE-2 | 孤儿页面：`monitoring-page.tsx`（401 行） | 被 import 无 Route | 竞品监控有两套实现，实际只跑一套 | 挂载或删除 |
| FE-3 | 平台注册表前端副本 | `lib/platforms.ts:3-10` | 与后端需手工同步 | 删副本，失败时降级为错误态 |
| FE-4 | 类型手写维护 | `types/index.ts` | 后端改字段前端无感知 | 从 OpenAPI 生成 |
| FE-5 | 通知 30s 轮询 | `app-shell.tsx:99-103` | 单机无压力，但架构上不优雅 | 可接受 |
| FE-6 | 设置页硬编码图片路径（**2026-09-24 已关闭**） | 原 `settings-page.tsx:60,70` | 绕过 API 客户端 | 随「为爱发电」模块移除而关闭：该路径唯一使用者已删除 |
