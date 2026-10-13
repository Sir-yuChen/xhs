# XHS_ALL_IN_ONE · 工程文档体系总纲

> **本文档是 `docs/` 目录的唯一入口。** 任何人在阅读、新增、修改项目文档之前，先读这一份。
>
> - **体系版本**：v2.0（文档先行体系）
> - **建立时间**：2026-09-18
> - **最近校验**：2026-09-21 —— §五「事实基线」按**实测回数**重修（`verify_doc_counts.py` 一次查出 **13 处**漂移：端点 147→156、path 127→135、请求函数 128→132、数据表 24→25、测试 269→473 等），并删去「引用一份 09-19 冻结审计文档当实时来源」的写法；同日晚回归基线 473→**480**（DEF-058 批次 +7 条守卫，含一条**前端行为验证** `tests/frontend/detail_message.cjs`；**同批次收尾 v1.29**：422 校验错误的数组 `detail` 也还原成「字段路径 + 原因」—— 初版丢空串 ⇒ 只剩英文 axios 文案兜底，行为断言 11→**13 PASS**、变异开关 2→**3**、变异验证 7/7→**8/8** 变红；**用例数 / 缺陷数 / 回归数本轮均不变**）。
>   **2026-09-23 再校（商品笔记智能生成与发布，设计 13 §31）**：端点 162→**166**、path 142→**146**（新增 `POST /xhs/product-notes/generate-save`、`POST /xhs/product-notes/batch-generate`、`POST /notes/{id}/publish`、`POST /notes/batch-publish` 4 端点）、请求函数 139→**143**（+`generateProductNote`/`batchGenerateProductNotes`/`publishSavedNote`/`batchPublishSavedNotes`）；回归 689→**697**（`test_product_note_publish.py` 9 条 + `tests/frontend/note_publish.cjs` 23 PASS/5 变异 + item-notes 用例改关系表源；注：旧声明 689 本就比实收少 1，本轮按 `--junitxml` 实测 697 回写）；数据表仍 **25**（新增 4 列走迁移 `u4v5w6x7y8z9`）。**真机全链路验证通过**（观隅好物 acct 8：千帆智能生成 1 篇 → 下载媒体 → 上传 → 平台预审核 → 发布，平台 ID `6ab2c7df00000000140026fd` 回写、临时目录已删）。要点：挂链笔记改读 **`note_goods` 关系表** + Modal/Tabs 弹窗（复用 `NoteDetailBody`）；发布走千帆 `note/publish`（**`x-cos-security-token` 头上传**、**预审核前置**、**SKU 粒度挂链 + AI 溯源 `aigcTaskId`**）；失败记平台原文 + 留现场可重发。`verify_doc_counts.py` 报 **15 处**声明待更新，已逐处回写；双脚本 PASS；**无新缺陷**。
>   **2026-09-23 四校（商品列表页上机验收修正，设计 13 §33）**：**端点 166 / path 146 / 请求函数 143 / 数据表 25 均不变**（只给 `/items` 加两个查询参数 `note_count_op` / `note_count`，不新增端点）；回归 703→**709**（`test_shop_items.py` +3：关系表口径筛选 / 未知比较符 400 / 平台值不参与；`test_page_optimizations.py` +1 页面守卫 + 1 变异参数）→ **710**（v1.35 追加排版守卫，见下）。① **挂链笔记的说明文本两处都移除**（弹窗 `Alert` + 列表列 Tooltip），取代 §32.4 的「Tooltip 里对照说明」（`platform_note_count` 仍在响应里，只是不再占界面）；② 搜索条件**恰好两行**、按钮组右对齐置尾（追加「挂链笔记数」后仍两行，靠库存/销量四区间收到 `span=3`）；③ 商品台账**只有「上架时间」列可排**，默认排序随之为**上架时间倒序** —— ⚠️ 推翻 §32.1 / §32.7 的「默认仍是创建时间倒序」（原句已就地标注被取代）；④ 千帆可用性作**前置校验**：`lib/shop-items-view.ts::ledgerPrerequisite` = 已绑店铺 ∧ `ark` 通道非「确定不可用」（复用 `sync-gates::channelUnavailable`，`unknown` 放行），且切账号**先清绑定态**消除「用新账号打旧绑定」的 400 竞态 ⇒ 未绑千帆时**零请求、只提示一句**「请在账号信息卡片中扫码登录千帆后再来查看」（旧的三步取 Cookie 长文案与「登录态已失效」独立告警移除）；⑤ 分类 `…` 收起 + Tooltip（ADR-018），**不自动换行**；⑥ **新增「挂链笔记数」筛选**（比较符默认「小于」+ 指定篇数，**留空 = 不筛**；口径 = `note_goods` **关系表**，与列表该列同一个数；未知比较符 → 400 `QIANFAN_NOTE_COUNT_OP_UNSUPPORTED`；实现用相关子查询，不动分页总数）。`tests/frontend/shop_items_view.cjs` **10 → 27 PASS**、变异 **6 → 7**（新增 `sortany` / `gateoff` / `noteop`）；**v1.35 追补排版三条**（用户口径「商品名称一行展示，商品图片数据量放图片左上角，商品图放大 15px」+「商品列表（千帆）下的空白处移除」，记在设计 13 §33.9）：商品缩略图 **56 → 71**（尺寸收敛成 `ITEM_THUMB_SIZE` 一个常量，真图与无图占位块共用；「商品」列宽随之 380 → 400）、「N 图」Tag 从标题后挪到**图片左上角**（绝对定位 + `pointerEvents: none` 不吃「点开大图」的点击）、商品名 `…` 收起 + Tooltip **单行**（⚠️ 用「Tooltip + 手写 CSS」，`Typography.Link.ellipsis` 在 antd 6.3.7 只收 `boolean`，传对象 TS2322）、首卡 body 去掉空壳 `Space`（antd `Card` 判 `childNodes.length`，空壳也算子节点 ⇒ 标题下空出 48px；改裸 `Text` + `null` 兜底后 body 整块不渲染）；新增页面守卫 `test_shop_items_page_follows_the_2026_09_23_thumbnail_review`（4 条口径，另跑变异：把空壳塞回去 ⇒ 精确变红）；设计 04 §2.12/§2.21 结构与细节行（含 v1.7 版本行）、设计 05 接口表第 5/6 行（顺带改正「打千帆 / `shop_note_goods`」两处陈旧）、设计 13 §33 已同步；双脚本 PASS；**无新缺陷**。
>   **2026-09-23 五校（商品列表 v1.36：筛选未生效根因 / 批量删除 / 弹窗 75%，设计 13 §33.10）**：端点 166→**163**、path 146→**147**（新增 `POST /api/xhs/qianfan/items/delete`）、请求函数 143→**144**（+`deleteXhsShopItems`）；数据表仍 **25**（无迁移：批量删除只是 `DELETE` 现有行）。回归 710→**716**（`test_shop_items.py` **+3**：批量删除只删本店且关系行不动 / 四道门（空 / 超限携 `limit` / 未绑店 / 越权）/ 请求模型未知字段 422 `extra_forbidden`；`test_page_optimizations.py` **+2**：`…_the_2026_09_23_batch_delete_review` 页面守卫 + **前后端上限同值**的真值比对；变异参数表 **+1**（`delmax`）；`shop_items_view.cjs` **27 → 33 PASS**、变异 **7 → 8**）。① **「挂链笔记数筛选未生效」的根因是运行时而非代码**：筛选实现从头就是对的（新代码在真实库上跑：`lt 1 → 276` / `gte 1 → 1698` / 留空 → 1974，且与纯 Python 按同一关系表口径逐项相等），错在**跑着的后端是改动前的旧进程** —— 它的 `ShopItemsRequest` 里没有这两个字段，而 **Pydantic 默认 `extra="ignore"`** 把参数**静默丢弃**（不报错、不记日志，观感与「代码写错」完全一样）。修复两条：**重启后端** + 请求模型加 **`extra="forbid"`**（未知字段 422，把「新旧不匹配」从静默丢弃改成第一跳炸出声）；② **批量删除本地台账行**（用户确认口径）：`POST /xhs/qianfan/items/delete`，只删本店 `shop_products` 行（**平台商品不动** —— 千帆 SDK 无商品删除接口；**`note_goods` 关系行也不动**，商品被同步写回时关系原样接上），键 = `item_id` 且由 `shop_id_fk` 限定（传别人的只删 0 行）；上限 **200/次**（前后端**同值**由守卫直接比对），空 / 超限**在发请求前**拦住、后端另有 400 硬闸且**不静默截断**；回参 `deleted` 是 `rowcount`，与 `requested` 分开显示；确认框正文由 `batchDeleteHint()` 生成，**必须**讲清「关系不动 / 平台仍在 ⇒ 下次同步会写回」两个连带后果；勾选跨页保留但**换查询条件 / 换账号清空**；③ 挂链笔记弹窗 **80vw × 84vh → 75vw × 75vh**（去 `maxWidth`（设了就不是 75%）、改 `centered`、body `overflow: hidden → auto`（窄视口两栏折行时 `hidden` 会把右栏裁掉））。设计 04 §2.21 结构图 / 细节行（「只读」一行**就地标注被 v1.36 取代**）+ v1.8 版本行、设计 05 接口表第 5/5.1 行、设计 13 §33 抬头与 §33.10 已同步；顺带把 `AGENTS.md` §5 那段回归演进链**无损重排**（压缩版 / 完整版两遍追加交错 ⇒ `539 → 559 → 557` 倒退，按同号相邻、恢复单调，**一个字符都没删**，备份 `AGENTS.md.bak-before-chain-reorder`）；双脚本 PASS；**无新缺陷**。
>   **2026-09-23 九校（删笔记 / 删商品连带删商品关联 + 已发布笔记也支持同步，设计 13 §26.6 / §25.7.5 / §33.10）**：**端点 167 / path 147 / 请求函数 144 / 数据表 25 均不变**（一条迁移 `w6x7y8z9a0b1` 是纯数据清理，不加表）。三条用户口径**一条一条做**：
> ① **删笔记连带删商品关联**：`purge_note` 原先只清标签 / 资产 / 评论 / 草稿引用，**漏了 `note_goods`** ⇒ 真实库已留 **4 条孤儿行**（note_id 1963/1964/1965/1967 → product_id 1）。补 `delete(NoteGoods)`；存量由迁移 `w6x7y8z9a0b1` 清（`DELETE … WHERE note_id NOT IN (SELECT id FROM notes)`，实测 1937 → **1933**，**有效关系行零误删**）。⚠️ 只改代码不做迁移 = 假修复（存量孤儿永远留着）。
> ② **已发布的笔记也支持同步**：新增 `models/note.py::note_supports_sync()` 作**后端唯一判据**（= `synced ∨ publish_status == "published"`），`POST /notes/{id}/sync` 改调它；错误码 `NOTE_NOT_SYNCED` → **`NOTE_NOT_SYNCABLE`**（名字里不再预设「只有 synced 才行」）。⚠️ 这是上一轮 `canSyncNote`（前端）留下的**待办**，本轮办结；**前后端两份判据必须同改** —— 各改一半就是「按钮显示却 400」或「按钮没有却还能调」。
> ③ **删商品也连带删关联**（**推翻** v1.36 / §33.10 的「关系行不动」）：`delete_items` 改成**先取将删行的本地 `id` → 按 `product_id` 删关系 → 删商品行**（**顺序不能反**，删了就对不上），且**只删指向被删商品的关系** —— 幸存商品的、别家店同名商品的关系都必须留下。确认框文案随之改（`batchDeleteHint`：「关系不动」→「关联关系一并删除」），`shop_items_view.cjs` 的 H6 断言同步改。⚠️ **同步侧 `_drop_stale_rows` 刻意没改**：平台侧商品消失 ≠ 用户显式删除，那条要另议（设计 13 §33.12 待办）。
> 回归 730→**734**：`test_note_goods.py` +2（删链接 / 无关系不报错）、`test_note_type.py` +2（已发布的本地生成 / 采集笔记都能同步）、`test_items_delete_removes_only_local_rows` **改写**（原断言「关系行不跟着删」保留在 docstring；新断言四条：只删被删商品的关系 / 别家店的留下 / 商品行只删本店 / 5 篇笔记全活）；`test_page_optimizations.py` 补第 ④ 条（前后端判据同在）与第 ③ 条（确认框必须讲「关联关系一并删除」）。TC-LIB-20~**24**（内容库 19→**25**、合计 291→**297**）。设计 13 新增 §26.6 / §33.12、§26.4 测试要点 +2 行；设计 05 `/notes/{id}/sync` 行同步。双脚本 PASS；**无新端点、无新迁移（除纯数据清理）、无新缺陷**。
>   **2026-09-23 八校（内容库第三轮：类型 Tag 归位封面右下角 + 只留卡片视图 + 撤批量标签入口，设计 13 §25.7.6）**：**端点 167 / path 147 / 请求函数 144 / 数据表 25 均不变**（纯前端 + 测试：无新端点、无迁移）。① **只留卡片一种视图**：`viewMode` / `setViewMode` / `tableColumns` / `<Table>` 及 antd 的 `Segmented` / `Table` / `ColumnsType` 导入**整条移除**（用户口径「内容库默认使用卡片，不再支持表格展示笔记」）—— 是**移除**不是「默认改成 card 留着切换器」：视图只剩一种，「当前视图」就不再是状态，留着就是一个永远不变的死状态；② **类型 Tag 归位封面右下角**（「三方同步/本地生成使用 tag 放图片右下角」）—— ⚠️ **不是**把 §25.7.3 推翻：那条取消的是「封面**左下角**那枚 Tag」（与「图文/视频」同排易混、且和左下角的发布状态/按钮争位），本条要的是「封面**右下角**」（那个位置本来是空的）。新增 `noteTypeTagColor` 作为**唯一配色**，详情弹窗里原先那段内联三元（synced→blue / local_generated→purple / 其它→default）也改成调它（DEF-061 同型：不留两份实现）；左下角那排 `maxWidth` 由 `calc(100% - 16px)` 收成 `calc(100% - 84px)` 给右下角**让位**（两块都是 absolute + 同 zIndex，不收窄就压住）；③ **卡片级 Tooltip 与两句长文案整条移除**（「三方同步笔记：账号同步链路落库，可拉取平台最新内容」/「本地生成笔记：由商品智能生成，待发布 / 发布失败可重发」）—— 类型已直接可见，悬停提示只剩复读；`noteTypeTooltip` 函数删除；④ **批量操作卡的标签下拉三件套一起撤**（「选择标签」下拉 + 「加标签」/「去标签」按钮）—— 没有下拉就选不到标签，两枚按钮也就永远 disabled，**只删下拉会留下死按钮**；`batchTagId` / `applyBatchTag` 一并移除，但 **`batchTagNotes`（api.ts）与 `POST /notes/batch-tag` 端点保留** —— 详情弹窗里给**单条**笔记增删标签走的正是它，撤的只是批量卡里那个入口，不是打标签能力。回归 729→**730**：`test_page_optimizations.py` 80→**81**（新守卫 `…_has_no_table_view_and_no_batch_tag_dropdown`）、`note_filters.cjs` 29→**34 PASS**（D 段由「悬停长文案」改写为「三档配色」断言 + D5 钉 `noteTypeTooltip` 已彻底移除）、变异 7→**8**（`fallback` 锚点原在被删函数里 ⇒ 改指 `noteTypeLabel` 兜底分支；新增 `tagcolor`）；**8 个变异全部被捕获**。⚠️ 两条既有守卫按**前提变化**改断言而不是放松：`count("tags.map") >= 2` → `>= 1`（表格没了，「卡片+表格两处」前提不复存在）、`'"author_name"' in code` → 属性访问 `author_name`（表格列 `dataIndex` 形态消失）—— 原断言都保留在注释里。设计 13 §25.7.6（含**类型承载四轮演变表**）+ §25.6 测试要点 +3 行；TC-LIB-17~**19**（内容库 16→19、合计 288→**291**）。双脚本 PASS；**无新端点、无新迁移、无新缺陷**。
>   **2026-09-23 七校（内容库筛选 + 卡片正文精简，设计 13 §25.7 / §25.7.4）**：**端点 167 / path 147 / 请求函数 144 / 数据表 25 均不变**（筛选只是 `GET /api/notes` 加两个**可选查询参数**，无新端点、无迁移）。① **来源账号筛选** `platform_account_id`（**不校验存在性、不返 404** —— `user_id` 那道 where 已是租户闸，别人的 id 给空集比 404 少泄漏一次「这个 id 存在」；软删账号的历史笔记仍可筛出）；② **笔记类型筛选** `note_type`（白名单 `NOTE_TYPE_FILTER_VALUES`，**从模型 `NOTE_TYPE_*` 常量组装**、不在 API 层重抄；未知值 400 `NOTE_TYPE_UNSUPPORTED` + `supported`；空串 = 不筛；`stats` 仍是全库口径、不受筛选影响）；③ 前端两个 `Select`（账号候选来自 `GET /accounts?platform=xhs`，取不到留空**不弹错**）+ 「重置」按**手写全空条件**重拉（不能用 `currentFilters()`：`setState` 异步 ⇒ 面板空白而列表仍筛着）；④ **文案唯一来源**收敛到新建的 `lib/note-filters.ts::noteTypeLabel()` / `noteTypeTooltip()`（下拉 label / 卡片悬停 / 详情 Tag 三处共用），**顺带修 DEF-061**（详情卡把 `local_generated` 显示成「本地」，与三档下拉口径不一致）；⑤ 卡片类型 Tag **从封面左下角移除**，改由**卡片级 Tooltip** 承载（悬停卡片任意位置可看，§25.7.3）；⑥ **卡片正文只留「标题 + 来源 + 时间」**（§25.7.4）：互动数据（赞/藏/评/转）**不上卡片**（详情弹窗仍完整可见，`getNoteEngagement` 函数**保留**），账号名与时间的间距由 `marginLeft`（寄生在作者名是否存在 ⇒ 作者缺值就粘连）改为 `display:flex; gap:6`（与元素是否渲染**解耦**）。回归 719→**726**：`test_note_type.py` 14→**19**（两账号×两类型夹具，含「别人的账号 id ⇒ 空集」「筛选取 stats 不变」）、`test_page_optimizations.py` 76→**77**（新守卫 `test_library_card_body_is_title_source_and_time_only`，三条断言**各自变异验证**：加回互动图标 / 去掉 `gap` / 函数改名 ⇒ 分别变红）、新前端行为脚本 `tests/frontend/note_filters.cjs`（**23 PASS / 4 变异**：空串混进合法枚举 / label 漂移 / 未知类型兜底 / 昵称空白；断言钉**外部字面期望表**而非「下拉 == 列表」自我印证）；另**改写两条既有结构守卫**（原断言「卡片必须有类型 Tag」与用户新口径冲突，改写而不是删除，原文写进 docstring；文案断言从页面文件改指 `lib/note-filters.ts`，否则等于要求把 DEF-061 重新引进来）。设计 13 §25.7.1~25.7.4 / §25.6 测试要点 +1 行、§29.9「互动行之下」半句**就地标注被 §25.7.4 取代**（标签行其余契约继续生效）、设计 05 `/api/notes` 行；TC-LIB-09~**13**（内容库 12→13、合计 284→**285**）；缺陷台账 +**DEF-061**（P2 `[FIXED]`，P2 12→13 / `[FIXED]` 17→18 / 合计 60→**61** / 关闭率 **32.8%**）。双脚本 PASS；**无新端点、无新迁移、无新缺陷（DEF-061 已修）**。
>   **2026-09-23 六校（商品列表 v1.37：排序「默认不排序 + 点表头才排」，设计 13 §33.11）**：**端点 167 / path 147 / 请求函数 144 / 数据表 25 均不变**（纯前端 + 测试：排序态与请求参数组装改在 `lib/shop-items-view.ts` 与页面，**无新端点、无迁移**）。① **默认不排序**：`DEFAULT_SORT` 由「上架时间倒序」→ **`null`**（进来表头没有箭头、请求**不带** `sort_field`，让后端用它自己的稳定默认序 = `create_time` 倒序 + `id` 次序键）；② **点表头才排、且一律后端排**：`resolveSort(dataIndex, order)` 返回 `SortState | null`（升 → 降 → **取消**），列上 `sorter: true` + 受控 `sortOrder`，**不许给 `sorter` 传比较函数** —— antd `useSorter.getSortFunction` 只在 `sorter` 是 function / 带 `.compare` 时返回真值（`antd/es/table/hooks/useSorter.js`），`sorter: true` ⇒ `getSortData` 里 `if (compareFn && sortOrder)` 跳过本地排序；传了函数就变成「只排当前页」的**假排序**（后端结果被它盖住）；③ 「重置」把排序一并退回 `null`（重置 = 回初始态），「搜索」保留用户当前排序；④ **「当前排序」行不变**：仍渲染后端回传的 `sort_field` / `order`（无排序态这行照样有值）。**两个「像前端在排」的怀疑都已排除**（实证）：后端排得动（真实库 `on_shelf_time_ms` 非空 **1972/1974**）、前端不本地排（antd 源码如上）⇒ 界面顺序只可能来自后端。回归 716→**719**（`test_shop_items.py` **+1**：不传排序 ⇒ `create_time` 倒序且**回传生效口径**、显式传 `create_time desc` 与不传逐项相同、未知字段值兜回默认；`test_page_optimizations.py` **+1** 页面守卫 `…_sorting_is_server_side_and_off_by_default` + **+1** 变异参数 `sortdefault`）；`shop_items_view.cjs` 仍 **33 PASS**（E 段改写：默认 `null` / 三级循环 / 非白名单不给排序）、变异 **8 → 9**（`sortdefault`：取消兜成默认序 ⇒ `E4` 精确变红；页面守卫另跑 5 个变异全部被捕获 —— 其中「把 `: {}` 换成 `: { sort_field: "" }`」这一变异**反过来暴露了守卫第一版断言太弱**，已改成切片断言）。另：**服务启停机制本轮查清并固化** —— `run-app.py`（launcher）只管 `uvicorn :8000` + `vite :5173`（崩了 5 秒自动拉起）⇒ **重启 8000 = 只杀它的 uvicorn 子进程、等 launcher 重拉**，**绝不起第二个 launcher**（会互相抢 8000 ⇒ 「每 10 秒 exited」崩溃循环）；8001 等端口用新增的 `.hermes/scripts/spawn-detached-api.py`（`pythonw`；⚠️ `DETACHED_PROCESS` 会秒死且日志无一行）。设计 04 §2.21 结构图 / 细节行 + **v1.9 版本行**、设计 13 §33.11（§33.3 与 §32.1 的原句**就地标注「被 §33.11 取代」**）已同步；双脚本 PASS；**无新缺陷**。
>   **2026-09-23 三校（上机验收四问题修正，设计 13 §32）**：**端点 166 / path 146 / 请求函数 143 均不变**（一改一补：`PATCH /api/notes/{id}/published` → `PATCH /api/notes/{id}/publish-status`，前端 `updateSavedNotePublished` → `updateSavedNotePublishStatus`）；回归 697→**703**（`test_note_published.py` 由 3 条**重写为 7 条**、`test_product_note_publish.py` +4、`test_shop_items.py` 本地契约改写、`test_page_optimizations` 守卫改指）；数据表仍 **25**（迁移 `v5w6x7y8z9a0` 是**纯回填**，不改结构）。① **发布状态收敛为一个字段**：`publish_status` 为唯一权威、`published` 降为**投影**、写入唯一入口 `models/note.py::set_note_publish_status()`；迁移已在本机实跑（先备份 `data/spider_xhs.db.bak-20260923-pre-s32`）：1962 条 `synced` → `published`、5 条 `local_generated` 真值保留；筛选四档（新增「发布失败」）/ 统计三档；② **生成媒体上限** 图 ≤6 / 视频 ≤1（视频优先）且**生成绝不发布**；③ **排序可见**（`_ts_text` 到秒 + `/items` 回传生效 `sort_field`/`order`）；④ **挂链笔记数**改 `note_goods` 关系表口径（平台原值另存 `platform_note_count`）。设计 03 §4.1 / 设计 04 §2.12 / 设计 05（API 表 + 口径段）与设计 13 §32 已同步；**另修回长期空转的 `tests/frontend/qr_session_identity.cjs`**（mock 桩缺导出 ⇒ 打包期即死；现 9 PASS / 变异 4 FAIL，补千帆分支覆盖）；双脚本 PASS；**无新缺陷**。
>   **2026-09-22 夜间再校（详情弹窗收敛 + 话题增删）**：端点 161→**162**（新增 `PUT /api/notes/{id}/topics`，设计 13 §29.3）、path 141→**142**、请求函数 138→**139**（+`updateSavedNoteTopics`）；回归 660→**668**（+8：话题端点 5 + 源码守卫 3）；数据表仍 **25**。内容库详情弹窗**移除评论与原始 JSON**（组件与端点保留给「我的笔记」页）；挂载商品版式对齐「我的笔记」（Divider 标题 + 72px 图 Card + 规格/价格同行）；话题改 **Tag 展示 + 「+」新增 / × 删除**，增删经 `PUT /notes/{id}/topics`（整列替换 + 归一化）**实时落库**。`verify_doc_counts.py` 报 **15 处**声明待更新（端点 ×6 / path ×2 / 请求函数 ×7 中的各处），已逐处回写；双脚本 PASS；**无新缺陷**。
>   **2026-09-22 晚间再校（笔记类型 / 关系表 / 对账融合 / 话题列）**：端点 162→**161**（新增 `POST /api/notes/{id}/sync`，移除独立对账 2 端点）、path 142→**141**、请求函数 137→**138**（+`syncSavedNote` −`reconcileAccountNotes`/`reconcileShopGoods`；注：改动前文件里实为 139，声明 137 本就落后 2，本次按实测回写）、数据表 24→**25**（新增 `note_goods`）；回归 583→**660**（+45 条：`test_note_type` 14 / `test_note_goods` 6 / `test_note_fields` 8 / `test_reconcile_fused` 10 / `test_shop_goods_sync` +2 / `test_page_optimizations` +5 源码守卫）。真实库已实跑迁移（备份 `.bak-20260922-pre-note-type`，1962/1962 行回填 `synced`）。本次 `verify_doc_counts.py` 报 **22 处**声明待更新（跨 12 份文档），本段即为其**逐处**回写记录；**无新缺陷**（同轮修掉一个未出厂的数据丢失缺口，未登记 DEF）。
>   **同日再校（新端点数）**：端点 156→**156**、path 135→**136**、请求函数 132→**133**（设计 13 §18 新增 `POST /api/accounts/{id}/login-state` + 前端 `fetchXhsAccountLoginState`）；回归 480→**511**；**顺带修 DEF-059**（缺陷 58→**59**）。本次 `verify_doc_counts.py` 报 **11 处**声明待更新，本段即为其**逐处**回写记录。
>   **2026-09-22 再校（内容库「是否发布过」标识）**：端点 159→**160**、path 139→**140**、请求函数 136→**137**（设计 12 §22 新增 `PATCH /api/notes/{id}/published` + 前端 `updateSavedNotePublished`）；回归 558→**572**（新增 `tests/backend/test_note_published.py` +14 条）。本次 `verify_doc_counts.py` 报 **14 处**声明待更新（跨 7 份文档），本段即为其**逐处**回写记录；**未新增缺陷**。**同日再校（内容库页界面收敛，用户口径）**：回归 572→**573**（+1 条源码守卫 `test_library_shows_the_real_author_or_nothing`，定向变异 **6/6**）；**端点 / path / 请求函数本轮均不变**（只改界面渲染与文案）；设计 04 §2.12 升 **v1.5**。
>   ⚠️ **同类漂移还在别处**：`docs/04_测试文档/03_测试用例库.md` 的**头部版本行**曾长期停在 v1.23（264/93）而 §14 表已到 265/94 —— **表被校验脚本看着、头部没被看着**。判据：凡「同一数字出现在表外」，就必须问它由谁看着。
> - **取代范围**：v1 全部散落文档（`docs/00`–`docs/18`、根目录 `PROJECT_MASTER.md` / `PROJECT_REVIEW.md` / `SIGNATURE_ALGORITHM_DEEP_DIVE.md` / `IDEA.md` / `ISSUES.md`），曾整体移入 `docs/_legacy_v1/`，已于 2026-09-29 M0 蒸馏删除，git 历史可回溯。
> - **适用阶段**：当前项目进入二次开发（下称「二开」），本体系为二开的唯一文档依据。

---

## 一、为什么要重建文档体系

项目一期的文档是**事后补写**的：代码写完 → 分析代码 → 输出报告。结果是文档数量多、彼此覆盖、结论互相取代，读的人要先分辨「哪份才算数」。

二开需要在**动手之前**把「要做什么、怎么做、做完怎么验」写清楚。所以本次不是整理旧文档，而是换一套**驱动开发顺序**的文档体系。

---

## 二、文档先行五阶段（本项目的执行标准）

这套流程把开发切成五个阶段，每个阶段有明确的**责任人（模型规格）**、**产出物**与**放行条件**。核心思想：**文档不是开发的副产品，而是开发的前置输入。**

### 阶段一 · 需求文档（使用小模型）

| 项 | 要求 |
|---|---|
| 输入 | 一句话业务想法 |
| 产出 | `01_需求文档/` |
| 规模约束 | **用不超过 10 句话**说清：开发者在项目中的角色、项目名称、项目主要功能、项目主要用户都有哪些 |
| 硬性要求 | 必须先建项目目录（骨架先落地），需求描述必须让上下文短、术语少的小模型也能零歧义读懂 |
| 模型规格 | **小模型**——需求文档追求短、准、无歧义，不追求信息量 |
| 放行条件 | 需求可被复述，且范围外事项被显式列出 |

### 阶段二 · 设计文档（使用大模型）

| 项 | 要求 |
|---|---|
| 输入 | 阶段一的需求文档 |
| 产出 | `02_设计文档/` |
| 规模约束 | **越细越好**。单文件过大时，把「架构设计」与「功能设计」拆开；功能设计**以访问端为基准**拆分（Web 运营端 / 后端 API / 调度侧 / SDK 侧各一份） |
| 必须覆盖 | ① 依据需求出设计 ② 技术栈（开发语言、系统架构、数据库选型、是否有移动端、全部第三方技术）③ 用户量与并发设计 ④ 开发环境与生产环境如何隔离 ⑤ 用 AI 发散思维，指出功能是否可完善（加分项）⑥ 系统中涉及的开关、配置，要在后台设计系统管理功能 ⑦ 系统安全性设计 |
| 模型规格 | **大模型**——设计文档追求穷尽与严谨 |
| 放行条件 | 逐项对照上述 7 项，无遗漏；每个功能点都能追到实现位置 |

### 阶段三 · 开发（使用小模型）

| 项 | 要求 |
|---|---|
| 输入 | 阶段二的详细设计文档 |
| 产出 | 代码 + `03_开发文档/` 中的任务台账更新 |
| 硬性要求 | **开发的功能以详细设计文档为准则**；设计文档没写的东西不写；追求「写完即对」，**尽量不要改 bug** |
| 模型规格 | **小模型**——按图施工，不需要临场发挥 |
| 放行条件 | 实现与设计逐条对齐，偏差全部登记 |

### 阶段四 · 测试（使用大模型）

| 项 | 要求 |
|---|---|
| 输入 | 已实现的系统 + 设计文档 |
| 产出 | `04_测试文档/` 八份（含**功能点全链路验收清单** = 唯一验收入口） |
| 必须执行 | ① 生成 codegraph 型代码图谱 ② 系统总体测试进度文档 + 各功能点测试用例 + 各功能点测试跟踪文档 ③ 每次测出 bug 与修完 bug 都要在文档上打标记，以便后续审计 ④ 排查系统中所有 mock 标记，核对前后端接线覆盖度（目标 100%）⑤ 核查管理端开关在 UI 上是否生效、是否落库（数据库 or git 库）**⑥ 开发完毕后生成/更新业务链路验收文档**（`04_测试文档/10`，业务角度以链路对功能点验收 —— 见 `00_管理规范/01` 阶段四 6 项 / `04_测试文档/01` §14） |
| 模型规格 | **大模型**——测试需要全面性与推理能力 |
| 放行条件 | **六项**全部产出；mock 与接线覆盖度有明确的数字与清单；**业务链路验收文档已更新且 `verify_acceptance_links.py` PASS** |

### 阶段五 · 回流沉淀（本项目补充）

阶段四暴露的问题，两类分别回流：**设计缺陷 → 回阶段二修订设计文档；实现缺陷 → 回阶段三修复并登记**。禁止直接改代码而不回写文档。

---

## 三、目录结构与职责地图

```
docs/
├── README.md                        总纲（本文件）：理念、索引、使用规则
│
├── 00_管理规范/
│   └── 01_文档先行工作流.md          五阶段的执行细则、门禁、变更管理、模板
│
├── 01_需求文档/                      阶段一产出（小模型可读）
│   ├── 01_需求规格说明书.md           现行系统的需求基线
│   ├── 02_二开需求规格_商品笔记自动化.md  本次二开的需求规格（草案）
│   ├── 03_需求规格_我的笔记列表.md     叠加需求：自有内容与互动数据（已实现）
│   ├── 04_需求规格_账号矩阵同步.md     叠加需求：笔记/商品同步落库（起步中）
│   └── 05_需求规格_第二阶段二开.md     第二阶段二开总体需求（2026-09-29 规划，待开工）
│
├── 02_设计文档/                      阶段二产出（大模型可写）
│   ├── 00_设计总览.md                设计文档的索引与关键决策摘要
│   ├── 01_技术栈与选型.md            语言 / 架构 / 数据库 / 移动端 / 第三方
│   ├── 02_系统架构设计.md            分层、模块、部署、环境隔离、并发与容量
│   ├── 03_数据模型设计.md            54 张表逐字段
│   ├── 04_功能设计_Web运营端.md       以 Web 访问端为基准
│   ├── 05_功能设计_后端API与服务层.md  以 HTTP API 访问端为基准
│   ├── 06_功能设计_采集链路.md
│   ├── 07_功能设计_发布链路.md
│   ├── 08_功能设计_自动化调度与矩阵运营.md
│   ├── 09_功能设计_逆向SDK与签名层.md
│   ├── 10_系统管理功能设计.md         开关、配置的管理端设计与落库策略
│   ├── 11_安全设计.md
│   ├── 12_功能设计_我的笔记与互动数据.md  自有内容只读读链路（已实现）
│   ├── 13_功能设计_账号矩阵同步.md     现状索引（演进链已归档 `_history/`）
│   ├── 14_功能设计_桌面打包与安装.md   PyInstaller + Inno Setup 桌面安装包（v1.0）
│   ├── 15_功能设计_定时任务中心.md     调度模块重构（贴近若依：任务行 + 调度日志 + 注册表白名单；2026-09-24 方案，取代 08 §1.1/§1.2/§2/§2.1）
│   ├── 16_功能设计_第二阶段二开.md     第二阶段二开详细需求（链路/字段级模型/业务逻辑/落库规划，v0.4）
│   ├── 17_功能设计_LLM生成链路.md     M1 LLM 生成链路详细设计（~7 新表 + ~19 端点字段级契约 + 迁移方案；2026-09-29 阶段二）
│   ├── 18_设计变更_数据隔离统一到平台账号维度.md  数据隔离收敛到平台账号（删 shops / shop_id_fk；2026-09-30 提案，待确认）
│   ├── 19_功能设计_自动化工作流.md     工作流（已被 2026-10-08 移除整条特性，改用定时任务）
│   ├── 20_功能设计_商品编辑优化扩展.md  商品列表操作列重排 / 同步按钮合并 / 蒙版+耗时 / 文案清理（设计 20 §11/§12）
│   ├── 21_功能设计_前端页面整合.md     竞品监控并入监控中心 / 笔记发现+抓取合并 / 死代码清理
│   ├── 22_功能设计_图片空间与菜单整合.md  千帆云图片资产只读浏览（设计 22 §1）
│   ├── 23_功能设计_商品新增到草稿箱.md  从商品数据包新增商品 → 草稿箱（只存草稿不上架）
│   └── 24_功能设计_商品发布到线上.md   提交商品真机发布（红线：成功第一时间下架）+ 草稿箱弹窗（商品页内、账号跟随、数据不落库）
│
├── 03_开发文档/                      阶段三产出
│   ├── 01_开发规范与架构硬约定.md      写代码前必读
│   ├── 02_代码地图与阅读路径.md        从入口到模块的导航
│   └── 03_二开任务分解与里程碑.md      任务台账（唯一进度真相源）
│
├── 04_测试文档/                      阶段四产出
│   ├── 01_测试体系与流程规范.md        测试怎么做、怎么标记
│   ├── 02_代码图谱_codegraph.md        模块/调用/数据依赖图谱
│   ├── 03_测试用例库.md               用例正文 + 分域统计（版本史已归档 `_history/`）
│   ├── 04_测试跟踪与缺陷标记.md        现状快照（审计轨迹已归档 `_history/`）
│   ├── 05_Mock清单与接线覆盖度.md      mock 全清单 + 前后端接线覆盖率
│   ├── 06_系统开关与配置核查表.md      开关台账：UI 生效性 + 落库位置
│   ├── 07_第二阶段验收测试方案.md      ⚠️ **已作废并归档** `_history/07_第二阶段验收测试方案_已归档.md`（草案 v0.1；验收职能已被 09 取代；§6 随「自动化工作流」退场）
│   ├── 08_真机验收攒批清单.md          攒批的「需用户肉眼确认」事项（§13.4 执行载体）
│   └── 09_第二阶段功能点全链路验收清单.md  **用户验收唯一入口**：16 条业务链路（串联前端入口）× 125 步 / 140 个功能点（入口/操作/期望/判定）；**有效 15 条 × 116 步 / 131 点**（链 7 随「自动化工作流」退场作废）
│   └── 10_前端页面整合验收清单.md       设计 21 前端整合验收：4 条链路 × 14 核对点（竞品监控并入监控中心 / 笔记发现+抓取合并 / 未受影响页面回归 / 文档数字）
│
├── 05_项目沉淀/
│   ├── 01_架构决策记录_ADR.md         已做过的重大决策与理由
│   └── 02_已知缺陷与技术债.md         待修问题清单
│
└── _history/                          M0 蒸馏归档（append-only：演进链 / 审计轨迹 / 版本演进史）
```

---

## 四、文档使用规则

### 4.1 谁是唯一真相源

| 问题 | 看哪份 | 其他文档不得重复定义 |
|---|---|---|
| 这个项目做什么、给谁用 | `01_需求文档/01_需求规格说明书.md` | ✅ |
| 这次二开要做什么 | `01_需求文档/02_二开需求规格_商品笔记自动化.md` | ✅ |
| 「我的笔记」这个功能要什么 | `01_需求文档/03_需求规格_我的笔记列表.md` | ✅ |
| 「账号矩阵同步」要什么 | `01_需求文档/04_需求规格_账号矩阵同步.md` | ✅ |
| 账号自有内容（我发布/赞过/收藏/互动消息）怎么读 | `02_设计文档/12_功能设计_我的笔记与互动数据.md` | ✅ |
| 笔记/商品怎么同步落库 | `02_设计文档/13_功能设计_账号矩阵同步.md` | ✅ |
| 技术选型是什么 | `02_设计文档/01_技术栈与选型.md` | ✅ |
| 表结构长什么样 | `02_设计文档/03_数据模型设计.md` | ✅ |
| 某个功能怎么实现的 | `02_设计文档/04`–`09`（按访问端） | ✅ |
| 定时任务怎么配、怎么跑、怎么查日志 | `02_设计文档/15_功能设计_定时任务中心.md`（调度侧设计；`08` §1.1/§1.2/§2/§2.1 的被取代原句以注释保留） | ✅ |
| 某个开关存哪里、UI 能不能改 | `02_设计文档/10_系统管理功能设计.md` + `04_测试文档/06` | ✅ |
| 任务做到哪一步了 | `03_开发文档/03_二开任务分解与里程碑.md` | ✅ |
| 第二阶段二开要做什么（总体） | `01_需求文档/05_需求规格_第二阶段二开.md` | ✅ |
| 第二阶段二开怎么做（详细链路/字段级模型/业务逻辑/落库规划） | `02_设计文档/16_功能设计_第二阶段二开.md` | ✅ |
| M1 LLM 生成链路怎么做（字段级契约） | `02_设计文档/17_功能设计_LLM生成链路.md`（供应商/模板/素材库/生成 19 端点 + 7 新表；M1 开发唯一依据，取代 16 §10/§11 方向 1 口径） | ✅ |
| 第二阶段二开怎么验收 | **`04_测试文档/09_第二阶段功能点全链路验收清单.md`（用户验收唯一入口：按链路走、按功能点核对）**；各方向 DoD 真相源 = 设计 16 / 17 / 20 / 15（原 `04_测试文档/07_…验收测试方案.md` 已作废归档，其 **§9 合规 C1–C8 / §1 验收策略 / §10–§11 门禁** 仍有效，原文见 `_history/`） | ✅ |
| 有哪些 bug 没修 | `05_项目沉淀/02_已知缺陷与技术债.md` | ✅ |

### 4.2 写作约定

1. **每条结论必须带证据**。格式：`` `文件路径:行号` ``。没有代码证据的推断，标注 `[待验证]`。
2. **不重复**。一个事实只在唯一真相源里定义，其他文档用链接指过去。
3. **区分「现状」与「目标」**。描述现有系统用「现状」，描述二开方案用「目标」，两者不混写。
4. **结论被推翻时**，在原文档标注「被 XX 取代」并附对照表，**不要静默删除**。
5. 需求文档面向小模型，**宁短勿长**；设计文档面向大模型，**宁细勿略**。

### 4.3 什么时候必须更新文档

| 触发事件 | 必须更新 |
|---|---|
| 需求变更 | `01_需求文档/` → 若影响设计，级联更新 `02_设计文档/` |
| 新增/修改数据表 | `02_设计文档/03_数据模型设计.md` |
| 新增 API 端点 | `02_设计文档/05_功能设计_后端API与服务层.md` + `13`（账号矩阵同步的端点契约） |
| 新增或改动开关、配置项 | `02_设计文档/10_系统管理功能设计.md` |
| 完成一个开发任务 | `03_开发文档/03_二开任务分解与里程碑.md` |
| 发现或修复一个 bug | `05_项目沉淀/02_已知缺陷与技术债.md` + `04_测试文档/04_测试跟踪与缺陷标记.md` |
| 做出架构级决策 | `05_项目沉淀/01_架构决策记录_ADR.md` |

---

## 五、本项目的事实基线（写文档时的公共上下文）

| 维度 | 值 |
|---|---|
| 后端 | Python 3.10+ / FastAPI + Uvicorn；SQLAlchemy 2.0 + Alembic；APScheduler |
| 数据库 | SQLite（默认 `data/spider_xhs.db`）/ MySQL（生产） |
| 前端 | React 19 + TypeScript 5.9 + Vite 7 + antd 6 + recharts 3 |
| 签名运行时 | Node.js 子进程（算法唯一实现源在 `xhs_utils/**/js/`） |
| API 规模 | **314 个端点**（**31** 个 router + `/api/health`，OpenAPI `operations` 实测；**path 条数 272**；（2026-10-09 预检执行进度：**+1 端点 / +1 path** —— `GET /api/xhs/package-precheck/{token}/progress`（预检跑 OCR 期间前端轮询「已处理/总数」，用户口径「执行进度」）⇒ 307→308 / 265→266；**+1 请求函数** `fetchPrecheckProgress` ⇒ 258→259；OCR 并发由 4 提到 **5**（用户口径「5 个并发处理 ocr」），视频抽帧一并改并发。前值 **307 个端点**（2026-10-05 无流量清理改手动任务 + 独立日志表：**+2 端点 / +2 path** —— `GET /api/xhs/pc/users/no-traffic/cleanup/active`（在途清理状态，按钮态数据源）、`GET /api/xhs/pc/users/no-traffic/cleanup-logs`（清理操作日志）⇒ 273→275 / 229→231；**+1 表** `no_traffic_cleanup_logs`（迁移 `d2e3f4a5b6c7`）⇒ 55→56；**+2 请求函数** `fetchNoTrafficCleanupActive` / `fetchNoTrafficCleanupLogs` ⇒ 223→225；2026-10-04 设计 16 §7.4 **M7 订单二期写操作（发货 / 售后审核）+4 端点 / +4 path** ⇒ 268→272 / 224→228；2026-10-04 设计 16 §7 / M6 订单只读 +3/path+3；前值 **265 个端点**（前值路径 **221** 条；2026-10-04 设计 17 **§13.9/§13.11 + M4/M5/M6**：+21 端点 / +16 path ⇒ 240→261 / 201→217；前值 **240** / **201**（2026-10-04 §13.9 M2 切片 2：+2 端点 / +2 path（`shop-metrics/collect`、`shop-metrics/trend`）+1 表 ⇒ 238→240 / 199→201。前值 **238** / **199**（2026-10-03 设计 17 §13.5 M2 切片 1：+2 端点 ⇒ 236→238；2026-10-02 设计 17 **v2.10 模型级健康 + 自然序号 ID**：模型行新增 `healthy` / `last_health_at` / `model_key` 三列（迁移 `b8c9d0e1f2a3`），健康与启用下沉模型维度、停用在用模型 400；**±0 端点 / ±0 path**（复用既有端点）。前一轮 **v2.9 模型全生命周期自动化**：新增同步端点 `POST /api/llm/gateway-configs/{id}/sync-models` **+1 端点 / +1 path** ⇒ 229→232 / 190→193，请求函数 164→**165**；同轮类型枚举扩四值 + `platform` 多值（零表结构改动）。更前 **228**：2026-10-02 设计 17 **v2.7 服务商模型分层**：新增模型子资源 **+4 端点 / +2 path**（`GET`/`POST /api/llm/gateway-configs/{config_id}/models` + `PATCH`/`DELETE …/models/{model_id}`）⇒ 224→228 / 187→189；同轮 v2.8 删除 `default_model` 字段（零端点变化）。前值 **224**（**30** 个 router + `/api/health`，OpenAPI `operations` 实测；path 曾为 187；2026-10-02 **设计 17 T3 素材库**：新增 `materials` router **+8 端点 / +4 path**（folders 5 + files 3）⇒ 216→224 / 182→187，router 29→30；2026-10-01 **设计 17 v1.9 服务商化**：令牌映射 4 端点退场（`include_in_schema=False` + 410 墓碑）⇒ −4 端点 / −3 path，新增 `GET /api/llm/providers/platforms`（使用平台枚举下发，独立前缀）+1 / +1，`DELETE /api/llm/gateway-configs/{id}` 退场为 410 墓碑（`include_in_schema=False`）⇒ 220→216 / 183→182；2026-10-01 **设计 17 T2/T5/T6 落地**（LLM 生成链路）：T2 提示词模块/模板 CRUD +8 端点 / +5 path ⇒ 207→215 / 174→179；T6 生成队列 +4 端点 / +3 path ⇒ 215→219 / 179→182；另 v1.7 去外挂化 `GET /api/llm/usage/overview` +1 端点 / +1 path ⇒ 219→220 / 182→183；2026-09-30 **ADR-021 数据隔离统一到平台账号维度**（设计 18）：三个端点退场 `POST /bind`、`POST /unbind`、`POST /shops`（关系与店铺实体一起退场 ⇒ 同批做**显式 `410` 墓碑** + `include_in_schema=False`，不进 OpenAPI）⇒ 210→207 / 177→174，**零新增端点**（能力判据从「有没有绑店」改「本账号 ark 会话是否可用」，`post_items` 等沿用同一路径）；2026-09-30 设计 13 **§44 同步笔记拆分**：+2 端点 `POST /api/accounts/{account_id}/goods-notes/sync`（商品笔记专用，ark 硬门禁）与 `GET /api/accounts/{account_id}/goods-notes/sync/status`（占 2 条 path）⇒ 208→210 / 175→177，同步链路 `_note_sources(only=…)` 改**单源路由** + 新增内置任务 `system.goods_note_sync`；2026-09-29 **LLM 网关 v1.6 架构 A +11 端点**（令牌映射 4 + 预警规则 4 + 用量展示 3，占 7 path）⇒ 197→208 / 168→175，同日 stats 改读本地台账、+`system.llm_gateway_monitor` 内置任务；2026-09-29 **LLM 网关配置（M1/T1）**：+5 端点 `GET`/`POST /api/llm/gateway-configs` + `PATCH`/`DELETE /api/llm/gateway-configs/{id}` + `POST /api/llm/gateway-configs/{id}/test`（占 3 条 path）⇒ 193→196 / 164→167；+1 端点 `GET /api/llm/gateway-configs/{id}/stats`（读 New API 本地 logs 表聚合）⇒ 196→197 / 167→168；同日 **v1.4 提升工作平台维度**：路径由 `/api/xhs/llm/` 改挂 `/api/llm/`（脱离平台前缀、多平台公共，端点数不变）；2026-09-28 §19.13.9 删 1 端点 `GET /api/xhs/creator/published`（与 `POST /api/xhs/pc/users/notes` 数据 100% 重复 + 唯一「翻到底」读端点）⇒ 192→193 / 165→164；2026-09-27 **运行时配置中心**（设计 10 §7）：+5 端点 `GET`/`POST /api/settings` + `GET`/`PUT`/`DELETE /api/settings/{key}`（占 2 条 path）⇒ 187→192 / 163→165；2026-09-27 §35.18 **补齐详情并入同步笔记**：删 2 `POST /accounts/{account_id}/notes/hydrate-pending` / `GET /notes/hydrate-queue`（`note_hydrate` 队列整条移除）⇒ 189→187 / 165→163；2026-09-27 §35.15 详情补齐队列：+2 `POST /accounts/{account_id}/notes/hydrate-pending` / `GET /notes/hydrate-queue` ⇒ 187→189 / 163→165；2026-09-26 §35.12⑤ 手动清理：+2 `POST /tasks/clean` / `POST /tasks/clean-all` ⇒ 185→187 / 161→163（当时漏记，本轮 §35.13 逐域回数补上）；2026-09-26 §35.13 批次层 + 批次号查询走 `GET /tasks?tree=1&batch_no=`（同一 path，无新端点）；2026-09-25 §31.10 商品生成走队列：+1 `GET /xhs/product-notes/generate-queue`（生成进度卡，键名 `generated`）⇒ 184→185 / 160→161；2026-09-25 §35.11 队列记录页：+1 `GET /tasks/queues`（队列清单 + 实时计数）⇒ 183→184 / 159→160；2026-09-25 §35 批量同步：+1 `POST /notes/batch-sync` +1 `GET /notes/sync-queue`（同步进度卡）⇒ 181→183 / 157→159；2026-09-25 §31.5 发布异步化：+1 `GET /notes/publish-queue`（发布进度卡）⇒ 180→181 / 156→157；2026-09-24 设计 15：+12 `/api/schedule`、−1 `/tasks/scheduler/status`），全部挂 `/api` 前缀（2026-09-23 五校 +1：`POST /api/xhs/qianfan/items/delete`；2026-09-23 绑定弹窗改造 +1：`POST /api/xhs/login-sessions/qianfan/email-login`，设计 13 §18.15） |

> **2026-10-07 · 工作流退场（设计 19 整条特性移除）**：用户口径「把工作流相关逻辑表等完整移除，
> 直接使用定时任务完成即可」⇒ 删除 `workflows` / `workflow_nodes` / `workflow_runs` /
> `workflow_run_logs` / `workflow_scheduled_publishes` **5 张表**（迁移 `e6f7a8b9c0d1`）、
> 引擎 + 7 类节点 + 11 个端点 + 前端页面与 `工作流` Tab。
> 规模 **292→282** operations / **250→243** paths / **32→31** router / 表 **56→51** /
> 请求函数 **244→233** / 页面 **36→35**（页面路由 22 不变 —— 旧的 workflows 路由是重定向条目）。
> ⚠️ 一并修正一条**真实缺陷 DEF-098**（工作流的调度**从未装配**：注册表只声明前缀键而装配查带后缀的
> 真实 job_key ⇒ `get_job_spec` 抛 `KeyError` ⇒ 静默不注册）—— 该缺陷随特性退场消失，载体已不存在。
| 前端请求函数 | **265 个**（2026-10-09 预检执行进度：**+1** —— `fetchPrecheckProgress`（`GET /xhs/package-precheck/{token}/progress`，预检跑 OCR 期间轮询「已处理/总数」）⇒ 258→259；前值 **258 个**（2026-10-05 无流量清理改手动任务 + 独立日志表：**+2 端点 / +2 path** —— `GET /api/xhs/pc/users/no-traffic/cleanup/active`（在途清理状态，按钮态数据源）、`GET /api/xhs/pc/users/no-traffic/cleanup-logs`（清理操作日志）⇒ 273→275 / 229→231；**+1 表** `no_traffic_cleanup_logs`（迁移 `d2e3f4a5b6c7`）⇒ 55→56；**+2 请求函数** `fetchNoTrafficCleanupActive` / `fetchNoTrafficCleanupLogs` ⇒ 223→225；2026-10-04 **M9 收口 +1** = `setMonitoringTargetStatus`（监控暂停/恢复，T-112）；前值 **222**（2026-10-04 设计 16 §7.4 **M7 订单二期写操作 +4**（`previewOrderWrite` / `shipOrder` / `shipOrdersBatch` / `handleAfterSale`）；前值 **218**（2026-10-04 设计 20 §5 / M4 商品编辑 **+7**（`fetchProductGroups` / `createProductGroup` / `renameProductGroup` / `deleteProductGroup` / `editShopItem` / `polishShopItem` / `exportShopItems`）⇒ 211→218；前值 **211 个**（口径：`lib/api.ts` 里 `await http.` 的出现次数；2026-10-04 **设计 17 §13.9 M2 切片 2**：+4（笔记/商品各 2 个指标快照函数）⇒ 190→194；前值 **190 个**（2026-10-02 **设计 17 v2.10 模型级健康 + 自然序号 ID ±0**：子表「检查」按钮复用 `testGatewayConfig`，不新增函数；本值由 **v2.9 +2** 落实（`syncGatewayModels` + `updateGatewayModel`）⇒ 162→164，另 +1 为此前未回写的漂移，本轮按盘上实测订正为 **165**。2026-10-01 **设计 17 v2.1 触发源 + 商品详情图链路 +1**：`generateProductDetailImage`（`POST /xhs/qianfan/generate-detail-image`）⇒ 161→162；2026-10-01 **设计 17 v1.9 服务商化 −4**：删 `fetchGatewayTokens` / `createGatewayToken` / `updateGatewayToken` / `deleteGatewayToken` / `deleteGatewayConfig`（5 个）+ 新增 `fetchGatewayPlatforms`（1 个）⇒ 167→163；2026-09-30 §44 +1：`syncGoodsNotesStatus`（`await http.get`）+ `syncGoodsNotes`（复用 `postNoteSync`，净 0）⇒ 166→167；2026-09-29 **LLM 网关（M1/T1）+5**：`fetchGatewayConfigs` / `createGatewayConfig` / `updateGatewayConfig` / `deleteGatewayConfig` / `testGatewayConfig` ⇒ 149→154，同日 **v1.4 +1**：`fetchGatewayStats`（`GET /llm/gateway-configs/{id}/stats` 改走 http 实例带鉴权，修掉页面裸 `fetch` 401 ⇒ 统计恒取不到）⇒ 154→155，5 个网关函数路径 `/xhs/llm/` → `/llm/`（净 0）；2026-09-28 **死代码清理 −19**：删掉 20 个全仓零调用方的请求包装（`fetchXhsBenchmarks` / `createBenchmarkDrafts` / `addNoteAsset` / `deleteNoteAsset` / `reorderNoteAssets` / `createTopic` / `downloadMediaFile` / `createMediaObjectUrl` / `fetchXhsQianfanShops` / `polishTextWithAi` / `generateCoverWithAi` / `composeImageUtility` / `resizeImageUtility` / `updateMonitoringTarget` / `retryPublishJob` / `cancelPublishJob` / `addPublishAsset` / `deletePublishAsset` / `uploadPublishAsset` / `fetchSystemSetting`；`downloadMediaFile` 不含 `await http.` ⇒ −19），并删掉只被它们引用的 11 个类型定义 ⇒ 168→149；2026-09-27 **运行时配置中心**（设计 10 §7）+5：`fetchSystemSettings` / `createSystemSetting` / `fetchSystemSetting` / `updateSystemSetting` / `deleteSystemSetting` ⇒ 163→168；2026-09-27 §35.18 补齐详情并入同步笔记 + 任务中心移除账号级任务页签 −3：`hydratePendingNotes` / `fetchHydrateQueue` / `fetchAccountTasks` ⇒ 166→163；2026-09-27 §35.16 账号级任务视图 +1：`fetchAccountTasks`（`GET /tasks?task_types=`）⇒ 165→166；2026-09-27 §35.15 详情补齐队列 +2：`hydratePendingNotes` / `fetchHydrateQueue` ⇒ 163→165；2026-09-26 §35.12⑤ 手动清理 +2：`cleanTasks` / `cleanAllTasks` ⇒ 161→163；2026-09-26 §35.13 批次层 `fetchTasks` → `fetchQueueRecords`（净 0）；2026-09-25 §31.10 商品生成走队列 +1：`fetchGenerateQueue`；2026-09-25 §35.11 队列记录页 +2：`fetchTaskDetail` / `fetchQueues`；2026-09-25 §35 +2：`batchSyncSavedNotes` / `fetchSyncQueue`；2026-09-24 设计 15 +11/−1），全部能对上后端（404 风险 0）（2026-09-23 五校 +1：`deleteXhsShopItems`；2026-09-23 §18.15 +1：`loginXhsQianfanByEmail`） |
| 接线覆盖率 | **2026-09-19 快照**：后端被接线率 87.0%（120/138，18 个孤儿端点）—— 见 `04_测试文档/05_Mock清单与接线覆盖度.md` §6。⚠️ **该指标未纳入自动校验，勿当实时值引用** |
| 数据表 | **54 张**（2026-10-06 **无流量清理日志改「笔记维度」**（设计 15 §17，用户口径「所有主体应是笔记维度」）：新增 `no_traffic_cleanup_items`（**一篇笔记一行**的处置明细，迁移 `e3f4a5b6c7d8`；`log_id` 用 `SET NULL` ⇒ 批次行过期后明细仍可查）**+1** ⇒ 55→56；同轮 `no_traffic_cleanup_logs` **加三列**（`coverage_note` / `aborted` / `abort_reason`，零新表）。2026-10-05 无流量清理改手动任务 + 独立日志表：**+2 端点 / +2 path** —— `GET /api/xhs/pc/users/no-traffic/cleanup/active`（在途清理状态，按钮态数据源）、`GET /api/xhs/pc/users/no-traffic/cleanup-logs`（清理操作日志）⇒ 273→275 / 229→231；**+1 表** `no_traffic_cleanup_logs`（迁移 `d2e3f4a5b6c7`）⇒ 55→56；**+2 请求函数** `fetchNoTrafficCleanupActive` / `fetchNoTrafficCleanupLogs` ⇒ 223→225；**2026-10-04 设计 17 §13.11 + M4/M5/M6**：M3 热点 2 表（`f2a3b4c5d6e7`）+ M4 商品分组 1 表（`g0a1b2c3d4e5`）+ M5 工作流 5 表（`q8r9s0t1u2v3`）+ M6 订单 5 表（`a1b2c3d4e5f7`）**+13** ⇒ 42→55；前值 **42 张**（**2026-10-04 设计 17 §13.9 M2 切片 2**：新增 `shop_metric_snapshots`（商品指标日快照，迁移 `e1f2a3b4c5d6`）⇒ 41→42；前值 **41 张**（**2026-10-03 设计 17 §13.5 M2 切片 1**：新增 `note_metric_snapshots`（笔记指标**日快照**，唯一键 `(note_pk, stat_date)`，迁移 `c9d0e1f2a3b4`，revises `b8c9d0e1f2a3`）**+1** ⇒ 41→42（2026-10-04 §13.9 又 +1 `shop_metric_snapshots`）；前值 **41**（2026-10-03 §13.5：+1 `note_metric_snapshots` ⇒ 40→41）；**2026-10-02 设计 17 v2.7 服务商模型分层**：新增 `llm_gateway_models` +1（服务商下的模型行子表）⇒ 39→40；**2026-10-02 设计 17 T3 素材库数据层落地**：新增 `material_folders` / `material_files` +2（迁移 `9b8c7d6e5f4a`，revises `f8a9b0c1d2e3`）⇒ 37→39；**2026-10-01 设计 17 T2/T5 落地**（LLM 生成链路）：T2 `prompt_modules` / `prompt_templates` +2 ⇒ 33→35；T5 `generation_records` / `aigc_media_assets` / `note_generation_links` +3（合并迁移 `c4d5e6f7a8b9`）⇒ 35→38；**2026-09-30 ADR-021 数据隔离统一到平台账号维度**：`shops` 表**整表退场**（店铺实体并入平台账号，`shop_account_bindings` 绑定关系随之消失）、`shop_products` 归属列改挂 `platform_account_id` ⇒ 34→33；**2026-09-29 v1.6 新增 `llm_gateway_tokens` / `llm_gateway_alerts` / `llm_gateway_usage_logs`**（架构 A 令牌两级映射 + 预警规则 + 本地台账，设计 17 §3.11/§3.12/§3.13，迁移 `n5o6p7q8r9s0`）⇒ 31→34；**2026-09-29 新增 `llm_gateway_configs`**（New API 连接配置，设计 17 §3.8；唯一键 `(user_id, name)`，迁移 `m4n5o6p7q8r9`）+ **`llm_gateway_snapshots`**（用量日快照，§3.9；唯一键 `(user_id, gateway_id, stat_date)`）⇒ 29→31；**2026-09-27 新增 `system_settings`**（运行时配置中心，设计 10 §7；唯一键 `(user_id, key)`，迁移 `h9i0j1k2l3m4`）⇒ 28→29；**2026-09-25 实测复核 28**（`__tablename__`；标签表并入话题表：删 `tags` / `note_tags`、增 `topics` / `note_topics`，迁移 `b2c3d4e5f6g7` —— 原「仍 27」的声明基数在本次前已漂移 1，按实测回写）；**2026-09-24 起含 `scheduled_jobs` + `scheduled_job_logs`**（定时任务中心，设计 15）；v1.6 起含 `shop_note_goods`；T-32 起含 `shops`；**T-802 起含 `shop_products`**；**2026-09-22 本批次起含 `note_goods`**（笔记↔商品多对对关系表）；**2026-09-22 起移除 `shop_account_bindings`** —— 店铺↔账号回到 1:1 由 DB 部分唯一索引强制。被取代的旧口径原句：~~「25 张（v1.6 起含 `shop_account_bindings` / `shop_note_goods`）」~~） |
| mock 端点 | **0 个**（原 4 个；`/users/notes` 2026-09-18 去 mock，**其余 3 个 2026-10-04 T-107 全部改真实现** + 补鉴权；`services/mock_data.py` 整模块删除） |
| 前端页面 | **43** 个页面文件（2026-10-05 需求 6 **同步指定商品**：+1 = `sync-specified-items-modal.tsx`（无限列表选商品 + 逐条同步结果）⇒ 33→34；前值 **33** 个页面文件（2026-10-04 设计 21 **前端页面整合**：删 `benchmarks-page.tsx`（并入监控中心）+ 删 `crawler-page.tsx`（并入「笔记发现 → 批量抓取」，内容迁 `bulk-crawl-panel.tsx`）⇒ 34→**33**；前值 **34**（2026-10-04 **M9 收口 T-106**：删死代码 `drafts-page.tsx` ⇒ 35→34；前值 **35** 个页面文件（2026-10-04 设计 20 §5 / M4 商品编辑：+1 `product-edit-modal.tsx`（商品编辑弹窗）⇒ 34→35；前值 **34** 个页面文件（2026-10-04 设计 19 / M5：+1 `workflow-page.tsx`（自动化工作流）⇒ 32→33；前值 **32**（2026-10-04 设计 17 **§13.10 M2 切片 3**：+1 `metrics-dashboard-panel.tsx` ⇒ 31→32；前值 **31** 个页面文件（2026-10-01 设计 17 §11.3 **用量看板独立首页** +1 = `usage-dashboard-page.tsx`（路由 `/usage-dashboard`，进站默认落地页）；2026-09-29 设计 17 §7.1 **大模型网关独立页** +1 = `llm-gateway-page.tsx`；同日 v1.4 工作平台维度：由 `pages/platforms/xhs/` 移至 `pages/llm-gateway/`、路由改 `/llm-gateway` 公共区；2026-09-25 §35.11 队列记录页 +1 = `task-records-page.tsx`；v1.7 起含 `shop-items-page.tsx`；2026-09-25 §36 笔记工坊合并图片 / 视频 / 草稿三坊为 `note-workshop-page.tsx`，其中 **1** 个是死代码（2026-10-04 T-106 删掉最后一个 `drafts-page.tsx`；此前 1 个））（2026-10-04 T-106：`drafts-page.tsx` 死代码**已删除**，页面文件 35→**34**；同步改 `verify_doc_counts.py` 口径） |
| 自动化测试 | **1897 passed / 0 failed / 0 errors / 0 skipped**（`pytest tests/ -q -n auto`，2026-10-05 全量实测，`--junitxml` 判定；较前值 **1817** **+80**：`test_shop_item_operate.py` **+33**（新增文件，设计 20 §11 商品操作门禁 / 任务形态 / 删除接口分流 / 指定同步的精确再筛与状态口径）+ `test_page_optimizations.py` **+8**（页面重构六条 + 无限列表两条 + vitest 用例接线）+ `test_product_edit.py` 分组退场后回数 + vitest **+10**（`shop-items-operate.test.ts`）；**变异 14/14 CAUGHT**（`.hermes/scripts/mutate_verify_product_edit.py`，实测跑完，含 4 个新增承重面：门禁失效 / 删除复用上下架接口 / 单行按钮复用批量处理器 / 无限列表退化）+ 验收清单变异 **11/11**；**−4 端点 / +3 端点**（分组 CRUD 退场 ⇒ 410 墓碑、新增 `items/sync` 与 `items/operate`）⇒ 274 operations / 232 paths；**−1 表**（`product_groups` drop）；请求函数 225→**224**；页面 33→**34**。前值 **1794 passed / 0 failed / 0 errors / 0 skipped**（`pytest tests/ -q -n auto`，2026-10-04 全量实测，`--junitxml` 判定；较前值 **1494** **+300**（M3–M8 各里程碑累计：M3 热点 +13 / M4 商品编辑 +19 / M5 工作流 +21 / M6 订单只读 +41 / **M7 订单二期写操作 +25** 等））。<br>⚠️ 前值 **1218 passed / 0 failed / 0 errors / 0 skipped**（`pytest tests/ -q -n auto`，2026-09-27 全量实测，`--junitxml` 判定，**244.6s**；较前值 **1209** **+9**，其中可逐位定位 **+8** = `test_initial_data.py` **14→22**（§1b 运行时配置种子守卫），余 **+1** 是前值未回写的漂移，本轮按盘上实测订正。本轮交付 = **配置表当前数据进随包种子（额度基线 60→8，设计 13 §38.8）+ 打包逐文件正向断言 + 第 5 次 v1.0.1 出包**；**零迁移、零端点、零表**（192 / 165 / 29 不变）；用例库 **373→374**（+TC-SYS-15）/ P0 **115** 不变）。<br>⚠️ 前值（同日 **运行时配置中心 + 限流额度去硬编码 + 跨账号并行轮**，**1209**）：**1209 passed / 0 failed / 0 errors / 0 skipped**（`pytest tests/ -q -n auto`，2026-09-27 全量实测，`--junitxml` 判定；较前值 **1164** **+45**，其中可逐位定位 **+36**：`test_app_settings.py` **23**（新增，设计 10 §7 配置中心 + 弹窗灌值时序守卫）+ `test_note_sync_parallel.py` **11**（新增，设计 13 §38.3 跨账号并行 / 账号级互斥）+ `test_sync_worker_watchdog.py` **+1** + `test_testing_spec_guards.py` **+1**；余 **+9** 是前值未回写的漂移，本轮按盘上实测订正。本轮交付 = **运行时配置中心 + 限流额度去硬编码（DEF-076）+ 同步 worker 池化**；迁移 head `f7a8b9c0d1e2` → **`h9i0j1k2l3m4`**；端点 **187→192** / path **163→165** / 请求函数 **163→168** / 数据表 **28→29** / 用例库 **368→372**、P0 **113→115**）。<br>⚠️ 前值（同日 §35.18 轮，**1164**）：**1164 passed / 0 failed / 0 errors / 0 skipped**（2026-09-27 全量实测，`--junitxml` 判定；较前值 **1171** **−7**，逐位吻合：删 `test_note_hydrate_queue.py` **−8**（补齐详情队列随 §35.18 整条移除）+ `test_page_optimizations.py` 页面守卫变异 **+1**（5→6）。本轮交付 = **设计 13 §35.18**（补齐详情并入「同步笔记」任务收尾 —— 账号级候选集扫**全部历史欠账**、`attempted_pks` 去重同一任务不重复打同一篇、`detail_done/detail_total` 进度键让「任务日志」进度条会动；账号卡去掉「补齐详情」按钮与「同步任务」进度区块，只留按钮态「同步中」；任务中心移除「账号级任务」页签；后端删 2 端点 + `note_hydrate` 队列整条移除 ⇒ **187 / 163**，请求函数 **166→163**）；⛔ 本节**取代**同日的 **1171**（账号卡同步 = 手动触发定时任务轮）与 **1150**（详情补齐队列 + 账号级任务统一底座轮），其下「1123」是更早一轮的写回值（原句保留）。前值 **1123 passed / 0 failed / 0 errors / 0 skipped**（`pytest tests/ -q -n auto`，2026-09-26 全量实测，**165.7s**；**前值 989 订正**：989 是 2026-09-25 的写回值，此后 §35.13 各轮与守卫补齐累计 +134 未回写，本轮按盘上实测订正；盘上收集数 1123 与 xml 判定四数一致），`--junitxml` 判定，119.1s；**调度器守活（DEF-067）**（设计 15 **§6.8**）—— 真机实测 APScheduler 主循环在 `jobstore.update_job` 上抛 `database is locked` ⇒ **整个主循环线程带异常退出**、作业静默停摆 **14 分钟**（20/36 行 `next_run_time` 冻结），而 `scheduler.running` 与 `/api/schedule/status` 仍报「运行中」（**端点/UI/日志三处零信号**）；根因 = `SQLAlchemyJobStore` **自建引擎**不带 `core/database.py` 的 `busy_timeout` 事件监听（只剩 pysqlite 默认 5 秒）+ APScheduler **不自愈**。修法两半：**治因** job store 引擎 `timeout=30`；**治果** 线程探针 `scheduler_thread_alive()` + `revive_scheduler()`（**换新实例**）+ 看门狗 `start_scheduler_watchdog()`（`lifespan` 起停、30 秒巡检、失败指数退避），状态端点 `running` 改走线程探针；新增 `tests/backend/test_scheduler_watchdog.py` **9 条**（+9 = 980→**989**），变异 `.hermes/scripts/mutate_verify_scheduler_watchdog.py` **11/11 全红**，真机冻结 **20/36 → 0/36**（`.hermes/scripts/probe_scheduler_alive.py`）；**零迁移、零端点**。⚠️ **前值订正**：前值 **979** 是话题清洗轮的写回值，此后又给 `test_legacy_topic_repair.py` 补了第 8 条（跨租户判重）**未回写** ⇒ 盘上实际 **980**。前值 **979 passed / 0 failed / 0 errors / 0 skipped**（`pytest tests/ -q -n auto`，2026-09-25 全量实测，`--junitxml` 判定，170.0s；**平台话题存量清洗（以三方数据为准）**（设计 13 §34.10）—— `merge_legacy_topic_markers.py --apply` 三步：并脏名（按 `(user_id, name)` 判重）→ **补关系**（走 `apply_sync_topics` 同一实现）→ 摘正文，外加幂等复核；⚠️ **原脚本只摘正文不补关系 ＝ 把这些笔记的话题删光**（91 篇带标记笔记里 **64 篇本就零关系**）—— 而正文里的 `#名字[话题]#` 本身就是平台的话题声明。实测：脏行 **124→0**、正文带标记 **91→0**、有话题关系的笔记 **29→93**、`topics` 335→**691**、`note_topics` 356→**800**、孤儿 0；**与备份逐篇对账「非话题残文逐字一致」**；新增 `tests/backend/test_legacy_topic_repair.py` ~~**7 条**（+7 = 972→979）~~ ⇒ **8 条 / 盘上 980**（第 8 条 `test_merge_never_crosses_tenant_boundary` 是后来补的、当时未回写数字）。前值 **972 passed / 0 failed / 0 errors / 0 skipped**（`pytest tests/ -q -n auto`，2026-09-25 全量实测，`--junitxml` 判定，171.1s；**平台话题形态 `#名字[话题]#` 修复**（设计 13 §34.3 §34.6 §34.8 §31.5 + 新增 §34.10，用户报障「发布成功的笔记均没有话题，只有正文图片」）：发布 `desc` 改为回填**全部**话题的**平台形态** `#名字[话题]#`（`hash_tag` 并行给平台 ID —— 缺任一条笔记就没话题），解析侧新增唯一实现 `strip_topic_marker` / `topic_token`（剥标记 / 写标记），边界与 `_TRIM_CHARS` 认平台排版噪声 `\ufeff` / `\xa0`，带标记时长度 1 也认；新增**生成 → 发布全链路**端到端用例（假平台捕获 body 断言）+ `test_note_topics.py` 7 条，变异 10/10 全红；存量 124 行脏话题名 + 91 篇正文残留给出清洗脚本（默认 dry-run）。前值 **964 passed / 0 failed / 0 errors / 0 skipped**（`pytest tests/ -q -n auto`，2026-09-25 全量实测，`--junitxml` 判定，112.6s；**两张进度卡改「本轮三数」+ 5 秒轮询 + 待处理为 0 停刷 + 去 tip**（设计 13 §31.5 / §35.4，用户口径「同步进度未更新」「去掉 tip」「成功/失败/待同步数」「数据 5 秒更新一次」「待同步数为 0 时不再刷新统计数据」）：发布队列补本轮记账（`published`/`failed`/`total`）、停止条件由 tracked 集合改为**待处理数为 0**（修「进度未更新」）；**同步节流加固**（§35.7）：页间停顿 3→5s 且加 `uniform(0.8,1.6)` 抖动（实现收敛到 `services/sync_pacing.py`）、`ds` 签名材料有限重试、详情失败文案人话化、`external_user_id` 注入 PC Auth 免一次 `/v2/user/me`；新增 `test_sync_pacing_and_dsl.py` 16 条 + 发布快照三数 1 条，变异 8/8 全红。前值 **947 passed / 0 failed / 0 errors / 0 skipped**（`pytest tests/ -q -n auto`，2026-09-25 全量实测，`--junitxml` 判定，140.8s；**内容库页提示全部走 antd message + 同步进度卡去进度条 / 去空闲文案**（设计 04 §3.6「2026-09-25 迭代」、设计 13 §35.4）：`error` / `batchFailure` / `topicFailure` / `detailSyncFailure` / 加载更多失败五个页面级 inline 承载撤掉，新增守卫 `test_library_page_notices_use_message_and_no_longer_occupy_layout`（6 变异全红）；批量同步 405 = 8000 旧进程缺 §35 路由，重启后复核 openapi 通过。前置：**全站消息提醒统一 antd message**（设计 04 §3.6，用户口径「所有消息提醒参考 antd 成熟组件，不要在页面暂用空间」）：14 个页面/组件的 inline Alert 消息条与任务中心 notice 弹窗收敛为 `App.useApp()` 的 `message.*`，另有三处静态 message 导入一并收敛；`test_page_optimizations.py` 新增 3 条守卫（旧形态回退 / 静态导入白名单 / 三分法边界）且过变异验证，`schedule_center.cjs` 的 R22 弹窗断言改写为 message 断言；演进 811（2026-09-24，下表历史值）→ 868 → 918/5 flaky → 928 → 941 → 947 → 964 → 972 → **979**。权威数字以 `AGENTS.md` §5 为准）：**v1.38 内容库筛选 + 卡片精简**（设计 13 §25.7 / §25.7.4）：`test_note_type.py` 14→19 条、`test_page_optimizations.py` 76→77 条（新守卫 `…_card_body_is_title_source_and_time_only`，三条断言各自变异验证）、新前端行为脚本 `tests/frontend/note_filters.cjs`（23 PASS / 4 变异）；719→**726**；更前 719（商品列表 v1.37）、前值 697、更前 689 —— **商品笔记智能生成与发布**（设计 13 §31）：新增 `test_product_note_publish.py` **9 条**（生成落库 / 门禁零请求 / 平台失败不落库 / 批量隔离 / 发布全链 / 失败留现场可重发 / 门禁三态 / 预审核拒绝 / item-notes 关系表源）+ 前端行为脚本 `tests/frontend/note_publish.cjs`（23 PASS、5 变异开关）+ `test_shop_items.py` 的 item-notes 用例改写；端点 162→166 / path 142→146 / 请求函数 139→143 → **703**（**§32 上机验收四问题修正**（v1.34，设计 13 §32）：① **发布状态收敛为一个字段** `publish_status`（`published` 降为**投影** + 唯一写入入口 `models/note.py::set_note_publish_status()`；迁移 `v5w6x7y8z9a0` 纯回填，本机 1962 条 `synced` → `published`；筛选四档 / 统计三档 / `PATCH /notes/{id}/published` → `/publish-status`）；② **生成媒体上限** 图 ≤6 / 视频 ≤1（视频优先）且**生成绝不发布**；③ **排序可见**（`_ts_text` 到秒 + `/items` 回传生效 `sort_field`/`order`）；④ **挂链笔记数**改 `note_goods` 关系表口径（平台原值另存 `platform_note_count`）。测试：`test_note_published.py` 3 → 7 条、`test_product_note_publish.py` +4、`test_shop_items.py` 本地契约改写、守卫 `PublishedTag` → `PublishStatusTag` 改指；另修回**长期空转**的 `tests/frontend/qr_session_identity.cjs`（9 PASS / 变异 4 FAIL）；**§33 商品列表页上机验收修正七条 + v1.35 排版追补**（设计 13 §33）：测试 `test_shop_items.py` **+3**（关系表口径筛选 / 未知比较符 400 / 平台值不参与）、`test_page_optimizations.py` **+2** 页面守卫（`…_the_2026_09_23_review` / `…_the_2026_09_23_thumbnail_review`）、`tests/frontend/shop_items_view.cjs` 10 → **27 PASS**、变异 6 → **7**；界面层全部收紧（挂链笔记两处说明文本移除 / 千帆前置校验未通过时零请求 / 只有上架时间列可排 / 搜索条件两行 + 挂链笔记数筛选 / 分类与商品名不换行 / 缩略图 71 且「N 图」角标贴图 / 首卡空白移除）。规模 166 / 146 / 143 **均不变**，表数仍 **25**）；**§33.10 商品列表 v1.36**（2026-09-23 五校）：`test_shop_items.py` **+3**（批量删除只删本店 + 关系行不动 / 四道门（空 / 超限携 `limit` / 未绑店 400 / 越权 404）/ 未知字段 422 `extra_forbidden`）、`test_page_optimizations.py` **+2**（`…_the_2026_09_23_batch_delete_review` 页面守卫 + **前后端删除上限同值**的真值比对）、变异参数表 **+1**（`delmax`）、`tests/frontend/shop_items_view.cjs` 27 → **33 PASS**、变异 7 → **8**；规模 166 / 146 / 143 → **167 / 147 / 144**（+`POST /api/xhs/qianfan/items/delete` / +`deleteXhsShopItems`），表数仍 **25**）；**设计 14 桌面打包 v1.0**（2026-09-24）：`database.py` alembic `script_location` 绝对化 +1 条守卫（变异验证），新增 `packaging/` 打包资产，产物 `spider-xhs-setup-1.0.0.exe` 落项目根目录、冒烟 7 段全过；738→**781**（含 §18.15 系列只跑受影响文件未回写全量的欠账 +43、本轮 +1），规模数不变 |

> **本表的权威源是 `AGENTS.md` §5**（数字口径只在那里定义）；本表与 §5 不一致时**以 §5 为准**。
> 两处数字由 `.hermes/skills/xhs-doc-stats-verify/verify_doc_counts.py` **自动比对**：
> 端点 / 数据表 / 请求函数 / 页面数 任一漂移都会让它 `OVERALL: FAIL`。
>
> ⚠️ **接线覆盖率为何标「快照」**：该项要「前端实际发出的每一次请求」↔「后端每一条路由」逐条对齐，
> 而前端存在**动态拼接路径**、**SSE `fetch`** 等非 `await http.` 形态 —— 实测正则反推只能解析出 103 条，
> 而人工审计口径是 120 条（**低约 15%**）。**用一个偏低的自动值去刷新它，等于拿新错数换旧错数**，
> 故显式标注快照日期与「勿当实时值引用」，而不是给它一个会骗人的「最新值」。

---

## 六、与 v1 旧文档的关系

- v1 全部旧文档（`docs/00`–`docs/18`、根目录 `PROJECT_MASTER.md` 等）原归档在 `docs/_legacy_v1/`，
  **已于 2026-09-29 M0 文档蒸馏时整体删除**（README 原已授权「确认不再需要回溯时可删除」）。
- 旧文档结论**已全部被本体系吸收或取代**；如确需回溯，从 **git 历史**（提交 `5dcb8ba` 及更早）恢复
  `docs/_legacy_v1/` 任意文件即可。

---

## 七、快速上手

| 你的角色 | 建议阅读顺序 |
|---|---|
| **AI 助手 / 自动化代理** | **根目录 `AGENTS.md`（唯一入口，≤200 行：运行命令 / 五条硬约束 / 按需读文档的方法 / 当前权威数字）** → **Hindsight 长期记忆**（`hindsight_recall` 按需检索；⚠️ 2026-10-05 起**不再是 `.hermes/memory/` 文件**） → 本文件 §四事实表 → 需要契约时按 `§ / TC / DEF` 编号**精读**。⚠️ 参考资料（易错速查 / 运行坑）在 **Hindsight 向量记忆**（`hindsight_recall` 检索），**按需读、不进硬约束** |
| 新接手的人 | 本文件 → `01_需求文档/01` → `03_开发文档/02_代码地图与阅读路径.md` |
| 要写二开代码 | `01_需求文档/02` → `02_设计文档/00` 起的全部 → `03_开发文档/01` |
| 要做测试 | `04_测试文档/01` → `04/02`–`04/06` |
| 要查某个开关 | `02_设计文档/10` + `04_测试文档/06` |
| 要查历史决策 | `05_项目沉淀/01_架构决策记录_ADR.md` |
