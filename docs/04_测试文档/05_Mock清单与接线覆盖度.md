# Mock 清单与接线覆盖度

> - **阶段**：四 · 测试
> - **来源**：图片要求第 4 项第 4 条 —— 「查一下系统中所有 mock 标记，前后端的接线覆盖度（100%）」
> - **检查方式**：静态代码核查（全仓库搜索 + 逐条确认）
>
> ---
>
> ## ⚠️ 快照声明（2026-09-21 加）
>
> **本文是一份 `2026-09-19` 的冻结审计快照，不是实时数据源。** 它的价值在于「当时逐条确认的过程与理由」
> （哪些是 mock、哪些是死代码、孤儿端点各自为什么合理），**不在于其中的合计数字**。
>
> | 项 | 本文写的 | 2026-09-21 实测 | 说明 |
> |---|---|---|---|
> | Mock 端点 | 3 | **3** ✅ | **仍准确**（`search/users`、`homefeed/channels`、`homefeed/recommend`） |
> | 后端端点总数 | 138 | **155** | 已过时（此后新增「我的笔记」/千帆/商品等批次） |
> | 前端请求函数 | 120 | **132** | 已过时（且与 `README.md` 当时的 128 **口径不同**） |
> | 后端被接线率 | 87.0% | **未重算** | 见下 |
> | 孤儿端点 | 18 | **未重算** | 见下 |
>
> **为什么接线覆盖率「未重算」而不是给个新值**：该项要「前端实际发出的每一次请求」↔「后端每一条路由」
> 逐条对齐，而前端存在**动态拼接路径**、**SSE `fetch`** 等非 `await http.` 形态 —— 用正则反推实测只能
> 解析出 103 条（人工口径 120 条，**低约 15%**）。**用一个偏低的自动值去刷新它，等于拿新错数换旧错数。**
> ⇒ 该项**不进自动校验**，引用时必须连**快照日期**一起引用。
>
> 📌 **本文自身还有一处旧账**：§2.1 写的端点总数 **138**、同表「占比」却按 **136** 算（`3 / 136`），
> §8 摘要又写 **86.8%**（而 §6.1 是 87.0%）。**这些矛盾一并保留原样、不逐一改写** ——
> 因为整张表的基数都已过时，逐格修正只是把「一份没人该引用的旧快照」修饰得更像可信数据。
> **要新数字，去 `AGENTS.md` §5（唯一权威）或直接跑 `verify_doc_counts.py`。**
> - **检查时间**：2026-09-19
> - **版本**：v1.7 · 2026-09-19（新增 `xhs/qianfan/health-check` → 端点 **144 → 145**、path **124 → 125**、请求函数 **126 → 127**、接线率 **87.5% → 87.6%**；v1.6：商品→挂链笔记反查；v1.5：商品列表页）

---

## 1. 检查方法与口径

### 1.1 搜索关键词

```
mock | mock_data | sample_ | fake | placeholder | TODO | FIXME |
NotImplemented | return [] | coming_soon | 即将上线 | 开发中
```

### 1.2 统计口径

| 项 | 口径 |
|---|---|
| 后端端点数 | `backend/app/api/**` 中所有 `@router.*` 装饰器 + `main.py:45` 的 `/api/health` |
| 前端请求函数 | `frontend/src/lib/api.ts` 中的 `export async function` |
| 「mock 端点」定义 | 返回硬编码假数据的**已注册路由** |
| 「死代码」定义 | 无任何调用方 |
| 「半成品」定义 | 有真实实现但路由没接（返回 mock 或未注册） |

---

## 2. 后端 Mock 端点清单

### 2.1 汇总

| 指标 | 数值 |
|---|---|
| 后端端点总数 | **138**（132 − M1 下线的假 PATCH 1 个 + 「我的笔记」新增 5 个 + 页面体验优化批次 2 个：`/accounts/{id}/metrics`、`/xhs/pc/users/search`） |
| **Mock 端点** | **3**（原 4；M2 已于 2026-09-18 转真实实现） |
| **占比** | **3 / 136 = 2.2%**（原 4 / 132 = 3.0%） |
| 假实现端点（非 mock 数据但语义为假） | **0**（`PATCH /accounts/{id}` 已于 M1 下线，DEF-007 `[VERIFIED]`） |
| 含 mock 的文件 | 1（`backend/app/api/platforms/xhs/pc.py`） |

### 2.2 逐条清单

| # | 端点 | 位置 | 现在返回 | **真实实现是否已存在** | 缺口 |
|---|---|---|---|---|---|
| **M1** | `POST /api/xhs/pc/search/users` | `pc.py:594` | `{"total":1,"items":[{"id":"demo-user","nickname":"运营研究员"}]}` | ⚠️ **仅 SDK 层**：`apis/xhs_pc_apis.py:635 search_user()` | 缺 adapter 方法 + 路由改接 + **补鉴权** |
| ~~**M2**~~ | ~~`POST /api/xhs/pc/users/notes`~~ | `pc.py:753` | ✅ **已于 2026-09-18 转为真实实现** | 本需求新增单页方法组：`pc_api_adapter.py::get_user_posted_page()` → `apis/xhs_pc_apis.py:292` | **已消除** |
| **M3** | `GET /api/xhs/pc/homefeed/channels` | `pc.py:851` | `{"items":[{"id":"homefeed_recommend","name":"推荐"}]}` | ⚠️ **仅 SDK 层**：`apis/xhs_pc_apis.py:154 get_homefeed_all_channel()` | 缺 adapter + 路由改接 + 鉴权 |
| **M4** | `POST /api/xhs/pc/homefeed/recommend` | `pc.py:856` | `{"items":sample_notes()}` | ⚠️ **仅 SDK 层**：`apis/xhs_pc_apis.py:171 get_homefeed_recommend()` | 同上 |

> ⚠️ **剩余这 3 个端点全部无鉴权依赖参数**（无 `Depends(get_current_user)`）→ 修成真实实现时**必须同时补鉴权**，否则造成越权访问（见 DEF-021）。
>
> ✅ **M2 的处置值得记录**：它没有采用「并行新增一条 `/self/notes`、把旧 mock 留着」的做法，而是**就地替换**。理由是① 前端从未引用该 mock（实测 `frontend/src/` 无匹配），改动影响面为零；② 留旧 mock 会让 mock 总数不减，与阶段四的清零目标背道而驰。

### 2.3 不是 mock 但容易被误判的

| 位置 | 实际情况 |
|---|---|
| `pc.py:617` `/notes/comments` | ✅ **真实实现**：调 `adapter.get_note_comments()` → `apis/xhs_pc_apis.py:803` |
| `pc.py:600` `/notes/detail` | ✅ **真实实现** |
| `analytics.py:345 overview()` | ✅ **真实 DB 查询**（读 `notes`/`accounts`/`publish_jobs`） |
| `pc.py:634 note_comments_placeholder()` | ⚠️ 返回假数据，但**无路由装饰器 → 是死函数**，不构成线上 mock |

> ⚠️ **行号提示**：`pc.py` 于 2026-09-18 新增 5 个自有内容端点，行号整体下移。上表已更新为当前行号；旧文档中的 `:364` / `:347` / `:382` 均已失效。

---

## 3. 死代码清单

> **判定**：全项目无任何调用方。

| # | 位置 | 规模 | 判定依据 |
|---|---|---|---|
| **D1** | `backend/app/services/task_service.py` | 整模块（`list_tasks`，`:8`） | 返回 `sample_tasks()`，**无任何调用方**。任务中心实际读 `tasks` 表 |
| **D2** | `backend/app/services/mock_data.py` | 5 个函数 | 其中 `dashboard_overview`(:4) / `sample_accounts`(:25) / `sample_model_configs`(:67) **完全无引用**；`sample_notes`(:32) 仅被 M4 使用（原 M2 已于 2026-09-18 消除）、`sample_tasks`(:59) 仅被 D1 使用 |
| **D3** | `api/platforms/xhs/pc.py:634 note_comments_placeholder()` | 1 函数 | **无路由装饰器** |
| **D4** | `backend/app/core/task_runner.py` 的 `InProcessTaskRunner` | 1 类 | 只被实例化，**从未被调用** |
| **D5** | `frontend/src/pages/platforms/xhs/drafts-page.tsx` | **1250 行** | 与 `rewrite-page.tsx` 都导出 `XhsDraftsPage`；`router.tsx:18` 只引用后者 |
| **D6** | `frontend/src/pages/platforms/xhs/monitoring-page.tsx` | 401 行 | 被 `router.tsx:21` import，但**无任何 `<Route>` 使用** |
| **D7** | `xhs_utils/xhs_util.py` | 兼容转发层 | 仅转发 `xhs_pc.params` |

**死代码合计**：后端约 2 个模块 + 1 函数 + 1 类；前端 **1651 行**。

---

## 4. 「半成品」清单（有真实实现但没接）

> 这类问题比「完全没实现」更危险——因为看代码会以为能用。

| # | 端点/能力 | 真实实现位置 | 缺口 |
|---|---|---|---|
| ~~1~~ | ~~`POST /xhs/pc/users/notes`~~ | ✅ **2026-09-18 已接线**（「我的笔记」需求）：`pc_api_adapter.py::get_user_posted_page` + `pc.py::user_notes` | **已消除** |
| 2 | `POST /xhs/pc/search/users` | `apis/xhs_pc_apis.py:635` | 缺 adapter + 路由改接 |
| 3 | `GET /xhs/pc/homefeed/channels` | `apis/xhs_pc_apis.py:154` | 缺 adapter + 路由改接 |
| 4 | `POST /xhs/pc/homefeed/recommend` | `apis/xhs_pc_apis.py:171` | 缺 adapter + 路由改接 |
| 5 | PC 消息类能力 | `apis/xhs_pc_apis.py:844-962` | ✅ **已接线**（2026-09-18）：@我的 / 新增关注 / 赞和收藏 → `/xhs/pc/users/interactions`。⚠️ **未读数 `:827` 仍未接**（本需求范围外） |
| 6 | `POST /xhs/crawl/user-notes` | `crawl.py:302`（**已实现**） | **前端未调用**（能力闲置） |
| 7 | Creator 已发布作品 | `apis/xhs_creator_apis.py:796` | 前端未接 |
| 8 | 蒲公英全链路 | `apis/xhs_pugongying_apis.py` | **backend 零引用**，且链路有必 TypeError 缺陷（DEF-010，⚪ 已决定**不修**，见 T-24） |
| 9 | 蒲公英分销商广场全链路 | `apis/xhs_pugongying_distributor_apis.py`（原名 `xhs_qianfan_apis.py`，2026-09-18 重命名） | **backend 零引用**。⚠️ 命名问题已解决（DEF-015 `[VERIFIED]`），但「能不能用」未解决 |

---

## 5. 前端未完成标记清单

| # | 位置 | 内容 | 性质 |
|---|---|---|---|
| F1 | `lib/platforms.ts:3-10` | `fallbackPlatforms`（6 个平台，`enabled:false`） | 与后端 `core/platforms.py:34-39` **重复的硬编码副本**，且 `fetchPlatforms` 失败时用它兜底（**掩盖后端故障**） |
| F2 | `analytics-page.tsx:176` | Alert「数据洞察模块正在开发优化中，当前为基础版本」 | 半成品自述（后端接口本身是真实的） |
| F3 | `video-studio-page.tsx:27-29` | 「视频工坊即将上线」空壳页（37 行） | 未实现 |
| F4 | `xhs-section-page.tsx:39-49` | `Result` 占位页 | 通配路由兜底 |
| F5 | `drafts-page.tsx:989` / `rewrite-page.tsx:1143` | 「视频润色（即将上线）」禁用入口 | 未实现 |
| F6 | `settings-page.tsx:60,70`（**2026-09-24 已关闭**） | 硬编码 `/api/files/media/wx_pay.png`、`zfb_pay.jpg` | 已关闭：随「为爱发电」模块移除，这两个路径的唯一使用者已删除 |

### 5.1 不是假数据的硬编码（排除误报）

| 位置 | 性质 |
|---|---|
| `xhs-dashboard.tsx:19-28`、`analytics-page.tsx:51-69` | **安全零值兜底**（catch 时用全 0 / 空数组） |
| `xhs-section-page.tsx:4-37` `sectionInfo` | 静态文案映射 |
| `crawler-page.tsx:22-48`、`discovery-page.tsx:25-29` 等 | 下拉选项配置 |
| `app-shell.tsx:54-71` | 导航菜单配置 |
| `model-config-page.tsx:53` `is_default: true` | 表单创建默认值 |
| 各页面的 `columns` 定义 | 表格列配置 |

> 前端 `src/` 内**未发现** `mock` / `fake` / `TODO` / `FIXME` 关键字（`placeholder` 命中均为 antd 输入框占位符）。

---

## 6. 前后端接线覆盖度

### 6.1 双向比对结果

| 方向 | 指标 | 数值 | 变化 |
|---|---|---|---|
| 前端 → 后端 | 前端请求函数数 | **120** | +8（「我的笔记」需求 5 + 能力探测 1 + 页面体验优化批次 2） |
| 前端 → 后端 | 能对上后端的 | **120** | +8 |
| 前端 → 后端 | **前端有效应答率** | **120 / 120 = 100%** ✅ | 保持 |
| 后端 → 前端 | 后端端点数 | **138** | +2（`/accounts/{id}/metrics`、`/xhs/pc/users/search`） |
| 后端 → 前端 | 被前端调用的 | **120** | +2 |
| 后端 → 前端 | **后端被接线率** | **120 / 138 = 87.0%** | ↑（原 86.8%） |
| — | **孤儿端点** | **17** | 2026-09-28 −1（`GET /api/xhs/creator/published` 已删除，设计 13 §19.13.9） |

> ✅ **前端 404 风险 = 0**：所有前端调用的路径在后端都存在。
>
> **数字自洽校验**：138 − 120 = 18，与孤儿端点计数一致。
> ⚠️ **此算式算的是 2026-09-19 快照口径**（138 / 120 均为快照值），不是当前值 ——
> 当前孤儿端点数为 **17**（见本表末行），引用时别把两个口径混算。
> （请求函数计数口径 = `lib/api.ts` 里发起后端请求的导出函数：117 个 axios 调用 + 1 个 SSE `fetch` 流 + 本次新增 2 个 = **120**；不含 `getAccessToken` / `hasRefreshToken` / `clearAuthTokens` / `bootstrapAuth` / `downloadMediaFile` 等工具函数。）
>
> ⚠️ **2026-09-18 实测提醒**：本表的「前端 404 风险 = 0」描述的是**代码层面**。
> 实际运行时若**后端进程未重启**（仍是旧构建，如当时运行中的进程只有 115 条路由），
> 前端调用新端点仍会 404。这是 DEF-037 的诱因——**静态接线结论不能替代运行态核对**，
> 部署/重启后须再跑一次真实端点探测。

### 6.2 表 A：前端调用但后端不存在

> **无。0 条。**

这是本次核查最好的一个结论：**前端不存在「调用了不存在的接口」的情况**。

### 6.3 表 B：后端存在但前端未调用（孤儿端点，**17** 个；原 20 个，3 个已消除）

| # | 端点 | 位置 | **归类** | 是否需要处理 |
|---|---|---|---|---|
| 1 | `GET /api/health` | `main.py:45` | 🟢 **合理未接线**（容器健康检查 `Dockerfile:73`、`docker-compose.yml:27`） | ❌ 不需要 |
| ~~2~~ | ~~`PATCH /api/accounts/{id}`~~ | — | ✅ **已消除**（M1 / T-105 下线，DEF-007 `[VERIFIED]`） | 已消除 |
| 3 | `GET /api/tasks/{id}` | `tasks.py:124` | 🟢 单条查询备用 | ❌ 不需要 |
| 4 | `PATCH /api/tags/{id}` | `tags.py:83` | 🟡 **标签改名无 UI** | ⚠️ 评估 |
| 5 | `DELETE /api/tags/{id}` | `tags.py:104` | 🟡 **标签删除无 UI** | ⚠️ 评估 |
| 6 | `GET /api/xhs/analytics/engagement` | `analytics.py:402` | 🟡 **有真实实现无 UI** | ⚠️ 评估 |
| 7 | `GET /api/xhs/analytics/keyword-trends` | `analytics.py:427` | 🟡 有真实实现无 UI | ⚠️ 评估 |
| 8 | `POST /api/xhs/pc/search/users` | `pc.py:594` | 🔴 **mock**（M1） | ✅ 需处理 |
| ~~9~~ | ~~`POST /api/xhs/pc/users/notes`~~ | `pc.py:753` | ✅ **已消除**（本需求接线为真实实现，DEF-008 的 1/4） | 已消除 |
| 10 | `GET /api/xhs/pc/homefeed/channels` | `pc.py:851` | 🔴 **mock**（M3） | ✅ 需处理 |
| 11 | `POST /api/xhs/pc/homefeed/recommend` | `pc.py:856` | 🔴 **mock**（M4） | ✅ 需处理 |
| 12 | `POST /api/xhs/creator/topics/search` | `creator.py:182` | 🟢 **平台级备用入口**（前端走 `/publish/*` 业务层） | ❌ 标注用途即可 |
| 13 | `POST /api/xhs/creator/locations/search` | `creator.py:196` | 🟢 同上 | ❌ |
| 14 | `POST /api/xhs/creator/assets/upload` | `creator.py:210` | 🟢 同上 | ❌ |
| 15 | `POST /api/xhs/creator/publish/image` | `creator.py:233` | 🟢 同上 | ❌ |
| 16 | `POST /api/xhs/creator/publish/video` | `creator.py:265` | 🟢 同上（且视频能力未实现） | ❌ |
| ~~17~~ | ~~`GET /api/xhs/creator/published`~~ | ~~`creator.py:297`~~ | ✅ **已删除**（2026-09-28，设计 13 §19.13.9：与 `POST /api/xhs/pc/users/notes` 数据 100% 重复，且是唯一「翻到底」的读端点） | 已删除 |
| 18 | `POST /api/xhs/crawl/search-notes` | `crawl.py:226` | 🟢 前端改走 `/xhs/pc/search/notes` | ❌ |
| 19 | `POST /api/xhs/crawl/note-urls` | `crawl.py:262` | 🟢 同上 | ❌ |
| 20 | `POST /api/xhs/crawl/user-notes` | `crawl.py:302` | 🟡 **有真实实现但闲置**；⚠️ 且底层走全量循环（DEF-033） | ⚠️ 评估 |

**归类统计**（仅计未消除的 **17** 个）：

| 归类 | 数量 | 处理 |
|---|---|---|
| 🟢 合理未接线（健康检查 / 备用入口） | 9 | 仅在文档中标注用途 |
| 🟡 应评估是否接线（有真实能力无 UI） | 5 | 逐条评估 |
| 🔴 应处理（mock） | 3 | **必须处理**（T-107） |

> ⚠️ **修正一处原表算术错误**：上一版该统计写作「🟢 9 + 🟡 7 + 🔴 5 = 21」，与表内 20 行不符（实际 🟡 为 6 行）。该版已按表逐行重数并对齐为 **9 + 6 + 3 = 18**。
>
> 📌 **2026-09-28 逐域回数**：第 17 行（`GET /api/xhs/creator/published`）**已删除**（设计 13 §19.13.9）⇒ 🟡 **6 → 5**、合计 **18 → 17**，即 **9 + 5 + 3 = 17**；该行按「不静默删除」原则划线保留。
>
> **结论：17 个孤儿端点中，真正有问题的只有 3 个（全部是 mock）。** 其余 14 个是合理的备用入口或平台级接口。因此「后端被接线率 86.8%」**不应直接当作扣分项**——关键是每个未接线端点都有明确的归类与理由。

### 6.4 表 C：前端页面直接写死的假数据

| 文件:行 | 内容 | 判定 |
|---|---|---|
| `lib/platforms.ts:3-10` | `fallbackPlatforms`（6 个平台） | ⚠️ **是重复数据源**，且失败时兜底掩盖故障（DEF-023） |
| `settings-page.tsx:60,70`（2026-09-24 已移除） | 硬编码图片路径 | 🟢 低影响 · 随「为爱发电」模块移除而消失 |

> 严格意义上的「假业务数据」只有 `fallbackPlatforms` 1 处。

### 6.5 接线覆盖度目标值

| 指标 | 当前 | 目标 | 说明 |
|---|---|---|---|
| 前端有效应答率 | 100%（120/120） | **100%** | ✅ 已达标 |
| 后端被接线率 | **87.0%**（120/138） | **≥ 90%** | 处理剩余 3 个 🔴 mock 后 ≈ 88.4%（122/138）；再接线 2 个 🟡 即超 90% |
| 孤儿端点「有明确归类」 | 100% | **100%** | ✅ 已达标本表 §6.3 |

> **口径澄清**：图片要求「前后端的接线覆盖度（100%）」。本项目按 **「前端调用的后端 100% 存在」** 理解并已达成（118/118）。双向 100% 意味着「后端每个端点都有前端 UI」，这对 `health` 这类运维端点并不合理。

---

## 7. 处理计划

| # | 项 | 承接任务 | 里程碑 | 状态 |
|---|---|---|---|---|
| 1 | 修 `PATCH /accounts/{id}` 假实现 | T-105 | M1 | ✅ **已完成**（下线，DEF-007 `[VERIFIED]`） |
| 2 | 接线 mock 端点（补 adapter + 改路由 + **补鉴权**） | T-107 | M1 | 🟢 **1/4 已完成**（`/users/notes`）；剩 3 个 |
| 3 | 删除 `drafts-page.tsx` 死代码 | T-106 | M1 | ⬜ |
| 4 | 处理 `monitoring-page.tsx` 孤儿页（挂载或删除） | T-106 | M1 | ⬜ |
| 5 | 删除 `lib/platforms.ts` 前端副本 | 待建任务 | M1 | ⬜ |
| 6 | 清理 `task_service.py` / `mock_data.py` 死模块 | 待建任务 | M1 | ⬜ |
| 7 | 评估 6 个 🟡 孤儿端点是否接线 | 待建任务 | M1 | ⬜ |
| 8 | 给剩余 3 个 mock 端点补鉴权 | 随 T-107 一起 | M1 | ⬜ |
| 9 | 给全量循环 SDK 方法加页数上限 / 超时保护 | **待建任务**（DEF-033） | M1 或之后 | ⬜ **本需求新发现** |
| 10 | 接线 `/xhs/pc/users/*` 五端点（本需求） | T-201 | M1 | ✅ **已完成** |

---

## 8. 核查结论摘要

| 问题 | 结论 |
|---|---|
| 系统有多少 mock？ | 后端 **3 个 mock 端点（2.2%）**（原 4，`/users/notes` 已去 mock），全部集中在 `xhs/pc` 的 homefeed / search-users |
| 有假实现吗？ | 有 1 个：`PATCH /accounts/{id}` 恒返回成功但不落库 |
| 有死代码吗？ | 有：后端 2 个死模块 + 1 死函数 + 1 死类；前端 **1651 行** 死代码/孤儿页 |
| 前后端接线如何？ | **前端有效应答率 100%**（无 404 风险）；后端被接线率 86.8%，17 个孤儿端点中多数有合理理由 |
| 最危险的是什么？ | ① 剩余 3 个 mock 端点**无鉴权**，修真实实现时必须补；② `drafts-page.tsx` 与 `rewrite-page.tsx` 同名导出，**极易改错文件** |
| 与 README 的差异 | README 标注蒲公英与「千帆」为 ✅，实际 **backend 零引用**；且「千帆」实为蒲公英 |
