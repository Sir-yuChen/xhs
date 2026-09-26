# AGENTS.md · XHS_ALL_IN_ONE 项目上下文速查

> **这是给 AI 助手读的唯一入口文件（≤ 200 行）。** 目标：**不读大文档也能开工**。
> 细节一律按 §4 的「按需取」方式定位，**不要**整读 `docs/` 里几万字节的文档。

## 1. 一句话

小红书运营自动化平台（一期：账号矩阵 + 采集 + 内容库 + 草稿 AI + 发布 + 监控；
二开：商品笔记自动化 + 「我的笔记与互动数据」）。桌面端跑 `backend`(FastAPI) + `frontend`(React/Vite)，
数据在 SQLite `data/spider_xhs.db`。

| 目录 | 作用 |
|---|---|
| `backend/app/api/` `backend/app/services/` | HTTP 端点与服务层 |
| `backend/app/adapters/xhs/` | 平台适配器（PC / 创作者 / 千帆） |
| `apis/` | 逆向 SDK（签名运行时在这层） |
| `frontend/src/` | React 前端（`pages/platforms/xhs/*` 是「我的笔记」等页） |
| `docs/` | **五阶段文档，唯一真相源**（1.3MB，55 文件 —— 别整读） |
| `.workbuddy/` | 记忆 / 技能 / 脚本 / 临时件 |

## 2. 运行与验证（本机实测）

```bash
# 服务（由常驻任务监督，杀掉 uvicorn 后 5 秒自动用新代码拉起）
#   http://127.0.0.1:8000  → 后端直出 frontend/dist 构建产物 + API
#   http://127.0.0.1:5173  → Vite dev（热更新，/api 反代 8000）

# 后端测试（基线见 §5；判定一律以 --junitxml 为准，别信退出码）
# ⚠️ **默认只跑改动涉及的文件**，见下方「测试范围」铁律
# ⚠️ pytest.ini 已固化 `-n auto` / `--junitxml` / `--durations`：裸跑 pytest 即并行、自动出判据
#    （想串行调试用 `-n0` 覆盖）
"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe" -m pytest tests/ -q -n auto

# 前端类型检查（tsconfig.json 已开 incremental + tsBuildInfoFile：二次编译 ~10s）
cd frontend && node node_modules/typescript/bin/tsc --noEmit
# vite build **仅在「8000 吃产物 / 发布」前跑**；日常开发走 5173 热更新，不必 build
# cd frontend && node node_modules/vite/bin/vite.js build

# 文档数字校验 —— 两个脚本查**两类不同的错**，改完数字两个都要跑到 PASS：
#   ① 表内自洽（明细 vs 合计）
"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe" .workbuddy/skills/xhs-doc-stats-verify/verify_doc_stats.py
#   ② 文档 vs 实测（声明的端点/表/请求函数/页面数 vs 代码实际值；**离线**，~1.5s，不需服务在跑）
"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe" .workbuddy/skills/xhs-doc-stats-verify/verify_doc_counts.py

### 测试范围铁律（2026-09-22 用户口径，2026-09-24 按实测修订，强制执行）

**默认只跑本次改动涉及的测试文件，不要每次跑全量回归。** 全量回归（`pytest tests/ -q -n auto`，
本机 **150 秒**）**只在用户明确要求时跑**，或改到了跨模块公共层（`core/database.py`、`models/`、
`lib/api.ts`、认证 / 中间件）且要确认没有连带回归时跑。

**三层循环**（细则见 `docs/04_测试文档/01_测试体系与流程规范.md` §9.1 / §12）：

| 循环 | 什么时候跑 | 命令 |
|---|---|---|
| ① 内循环 | **每改一次代码** | `pytest tests/backend/test_xxx.py -q -x --lf`（pytest.ini 已带 `-n auto`，不用手打；`-x` 首个失败即停，`--lf` 优先跑上次失败的；慢用例多时加 `-m "not slow"`） |
| ② 中循环 | 一个功能点做完 / 动了公共层 | `pytest <受影响文件集> -q`（同上，自动并行） |
| ③ 外循环 | 收尾 / 用户明确要求 | `pytest tests/ -q`（自动并行 + 自动写 `.workbuddy/reports/reg.xml`） |

**「涉及哪些文件」用脚本选，不靠人脑反查**（P1-1，2026-09-25）：

```bash
python .workbuddy/scripts/pick_tests.py            # 按 git 未提交改动自动选测试文件
pytest $(python .workbuddy/scripts/pick_tests.py) -q   # 直接跑选出的文件集
```

原理：扫测试文件的 import 建「源模块 → 测试文件」反向索引（缓存 `.workbuddy/tmp/test-dep-index.json`），
命中完整模块名 + 一级父包；公共层（`models/`、`core/database`、`lib/api.ts`…）改动会 WARN 并建议全量。

判「涉及哪些文件」：按**改动的源文件**反查 —— 改 `services/xxx.py` ⇒ 跑 `tests/backend/test_xxx.py`
及 import 它的测试；改 API ⇒ 跑对应契约测试；改前端 `pages/**/x.tsx` ⇒ 跑 `tests/frontend/*.cjs`
里对应该页的脚本 + `tsc --noEmit`。拿不准是否连带时，**先跑直接相关的那一个文件**，红了再扩大
范围，不要一上来就全量。

理由：全量回归的时间成本会让人下意识少跑验证，反而更容易带病交付；按文件跑既能秒级反馈，
又能保证每次改动都被测到。

### 写测试的三条硬规矩（2026-09-24 新增，**由 `test_testing_spec_guards.py` 强制执行**）

1. **等异步一律 `waitFor(条件, {timeout})`，不许固定 `sleep(N>=100)`** —— 盲等慢且 flaky，
   还会被 pytest 的调用次数成倍放大（本次实测：两个脚本 44 次盲等 = 全量 20% 的时间）。
2. **新测试文件必须用 `tests/backend/conftest.py` 的公共夹具**（`api_client` / `db_factory` /
   `frontend_runner`…），不许再抄 `create_engine` + `create_all` + `dependency_overrides` 三件套。
   范例照抄 `tests/backend/test_conftest_fixtures.py`。
3. **规范要可执行**：只写进文档不算数，必须落到会红的守卫上，且**守卫要过变异验证**
   （本次三条变异：塞 sleep / 新文件抄样板 / 改名 waitFor，均已实测能被抓住）。
```

## 3. 五条硬约束（违反=返工）

1. **文档先行**：代码变更必须由文档变更发起；改了代码但设计文档没跟上 = 未完成；结论被推翻要标「被 XX 取代」并附对照表，**不静默删除**（归档用**移动**）。
2. **只读边界**：对小红书/千帆的探测一律**只读**（GET 或只读查询 POST）；**不碰写接口、不做登录逆向**；不打印 Cookie / token（打 `[REDACTED]`）。
3. **禁无分页插槽的全量方法**（C5，2026-09-22 修订）：`get_user_all_*` 这类**没有分页 / 限流插槽**的一次性全量方法**不得**在业务链路调用（调用即失控，中间没有刹车点）。
   ⚠️ **不再是「禁全量翻页」**：同步链路**要求翻到平台没有下一页为止**（设计 13 §21，用户 2026-09-22 口径），
   刹车由 `CrawlRateLimiter.wait_allow()` 的**速率**保证 —— 每页都过一次限流器，超限**等待**而非停止。
   替代关系：老的「`max_pages` 有界（默认 3 / 上限 20）」**已被 §21 取代**，不要再按 20 页上限写新代码。
4. **统计自洽**：改任何统计数字必须**逐域回数**，禁止只改合计；跑 §2 的自洽脚本。
5. **缺值口径**：解析不到就留空 / `null`，**不造假**；前端计数缺值渲染 `0` 并由 25–30 字原因行区分（唯一例外，见设计 12 §21.2）。

## 4. 读文档的策略（省 token 的关键，务必遵守）

**不要** `read_file` 整读下面这些（等于一次烧掉 1–2 万 token）：

| 文件 | 大小 | 正确读法 |
|---|---|---|
| `docs/02_设计文档/12_功能设计_我的笔记与互动数据.md` | 82KB | 先 `search_files` 找 `§24` / 关键词，再 `read_file(offset, limit≤150)` |
| `docs/04_测试文档/04_测试跟踪与缺陷标记.md` | 84KB | 同上（缺陷只在 §2.x 章节，按 DEF 编号定位） |
| `docs/04_测试文档/03_测试用例库.md` | 61KB | 同上（按 TC 编号定位） |
| `.workbuddy/memory/MEMORY.md` | **≤12KB 硬上限**（≈4.7K token） | **整篇可读**；只含不变量 + 索引，细节它不重复、会指路。⚠️ 字节受代码标识符密度影响会虚高（本文件 ≈2.6 B/token）——核 token 用 `tiktoken cl100k_base`；机械判据见 `.workbuddy/scripts/verify_memory_budget.py` |
| `.workbuddy/memory/REF_*.md` | 按需 | 参考资料（易错速查 / 运行坑等），**非核心，按需读**，由 MEMORY.md §0 索引 |
| `.workbuddy/memory/2026-09-*.md` | 25–60KB | 只按日期 `grep` 关键词，**勿整读** |

**顺序**：本文件 → `.workbuddy/memory/MEMORY.md`（**只含核心规范 + 「去哪找」索引**，整篇可读）
→ `docs/README.md`（文档地图）
→ 需要具体契约时**按 `§`/`TC`/`DEF` 编号精读** → 代码用 `search_files` 定位后按行读。

> 📌 **会话收尾纪律（2026-09-22 立为项目规范，强制执行）**：**每次会话结束前必须整理主记忆
> `.workbuddy/memory/MEMORY.md`** —— 目标是「**只保留不变量和索引**」，**≤12KB**（2026-09-25 由 8KB
> 校准而来：8KB 定于 2026-09-22，此后规则面随 36 个设计章节翻倍；**实测地板 ≈10KB** —— 叙述性内容
> 全部下移到 `REF §G` 后仍要 10KB，再压只能删规则，而删规则的历史成本（DEF-044/048/056/058 全是
> 规则缺位造成的）远高于这几百 token。12KB ≈4.7K token = 200K 上下文的 2.3%，**别再往上调**）。
> 新增内容**优先压缩进已有条目，不追加新条目**；能通过索引检索到的一律不写进来。**非核心内容（易错
> 速查 / 运行坑 / 过程 / 一次性结论）不落主记忆** —— 写当日日志 `YYYY-MM-DD.md`（append-only）、
> `REF_*.md`、或设计文档，主记忆只留指针。历史事故过程按日期 grep 日志，**勿整读**。
> ⚠️ 三条预算 + 一条可解码判据，由 `.workbuddy/scripts/verify_memory_budget.py` 兜底（已接入 pytest，
> 含 4 个变异开关）：主记忆 **≤12KB**、`REF_*.md` 单文件 **≤25KB**、**单日日志 ≤25KB**（立规前的
> 历史超限文件在白名单里，只报不判）、以上全部**必须是合法 UTF-8**。

## 5. 当前权威数字（2026-09-22 实测，改前先复核）

| 项 | 值 |
|---|---|
| 后端端点 | **187** operations / **163** paths（`GET /openapi.json` 实测；2026-09-26 §35.12⑤ 手动清理新增 2 端点 `POST /tasks/clean` / `POST /tasks/clean-all` ⇒ 185→187 / 161→163（当时漏记，本轮 §35.13 逐域回数补上）；2026-09-26 §35.13 批次层回归 + 批次号查询走 `GET /tasks?tree=1&batch_no=`（**同一 path，无新端点**）；2026-09-25 §31.10 商品生成走队列：+1 `GET /xhs/product-notes/generate-queue`（生成进度卡数据源，键名 `generated` 不是底座通用的 `synced`）⇒ 184→185 / 160→161；2026-09-25 §35.11 队列记录页：+1 `GET /tasks/queues`（队列清单 + 实时计数，后端下发 ⇒ 新队列上线前端零改动）⇒ 183→184 / 159→160；2026-09-25 §35 批量同步：+1 `POST /notes/batch-sync` +1 `GET /notes/sync-queue`（同步进度卡数据源；路由必须注册在 `/{note_id}` 之前）⇒ 181→183 / 157→159；2026-09-25 §31.5 发布异步化：+1 `GET /notes/publish-queue`（发布进度卡：队列 running/queued + 四态计数）⇒ 180→181 / 156→157；2026-09-24 定时任务中心重构（设计 15）：`/api/schedule` 新增 12 端点（占 8 条 path），删 `GET /api/tasks/scheduler/status` ⇒ 168→180 / 148→155；2026-09-23 v1.36 新增 `POST /api/xhs/qianfan/items/delete`；2026-09-23 v1.39 新增 `POST /api/xhs/login-sessions/qianfan/email-login`，设计 13 §18.15） |
| 数据表 | **28** 张（2026-09-25 实测复核 **28**：标签表并入话题表（删 `tags` / `note_tags`、增 `topics` / `note_topics`，迁移 `b2c3d4e5f6g7`）—— 原「仍 27」的声明基数在本次前已漂移 1，按 `__tablename__` 实测回写；新增 `scheduled_jobs` + `scheduled_job_logs`（定时任务中心，设计 15 §4，迁移 `y8z9a0b1c2d3`）；`auto_tasks` 删 4 调度列 + `next_run_at`、加 `schedule_job_id`；2026-09-24；前值 25 张（新增 `note_goods`（笔记↔商品多对对关系表，设计 13 §26），2026-09-22 本批次；前值 24（新增 `shop_products`，T-802；**2026-09-22 移除 `shop_account_bindings`** —— 店铺↔账号回到 **1:1** 并由 DB 部分唯一索引强制，故 25 → 24。被取代的 N:M 原句保留在 `docs/02_设计文档/03_数据模型设计.md` §9.2） |
| 前端请求函数 | **163**（口径：`lib/api.ts` 里 `await http.` 的出现次数；2026-09-26 §35.12⑤ 手动清理 +2（`cleanTasks` / `cleanAllTasks`）⇒ 161→163（当时漏记，本轮 §35.13 逐域回数补上）；2026-09-26 §35.13 批次层把 `fetchTasks` 换成 `fetchQueueRecords`（`GET /tasks?tree=1`，**净 0**）；2026-09-25 §31.10 商品生成走队列 +1（`fetchGenerateQueue`）；2026-09-25 §35.11 队列记录页 +2（`fetchTaskDetail` / `fetchQueues`）；2026-09-25 §35 批量同步 +2（`batchSyncSavedNotes` / `fetchSyncQueue`）；2026-09-24 设计 15 +11 schedule 端点族、−1 `fetchSchedulerStatus`；2026-09-23 v1.36 +`deleteXhsShopItems`；2026-09-23 v1.39 +`loginXhsQianfanByEmail`） |
| 回归 | **全量 1134 passed / 0 failed / 0 errors / 0 skipped**（2026-09-26 **用户 v1.0.1 实测九问题修复轮**实测，`--junitxml` 判定，**163.3s**；较前值 **1123** **+11**，逐位吻合：`test_page_optimizations.py` **+7**（`library_item_filter.cjs` 接线 —— 1 条契约 + 6 个变异参数化）+ `test_login_state.py` **+2**（`qr_session_identity.cjs` 契约 + 变异承重）+ `test_testing_spec_guards.py` **+1**（接线守卫自检 `test_wiring_guard_is_not_vacuous`）+ `test_self_content.py` **+1**（同一小红书身份不许两行）。本轮交付 = **DEF-070 / 071 / 072 / 073 / 074 五条修复**（千帆扫码走 ark 换票 / 账号身份唯一键改按小红书 ID / 登录即建店 / 弹窗成功自动关 + 蒙版可见 / 托盘退出关窗口）+ **两处「假防线」整改**（接线守卫**恒真** ⇒ `library_item_filter.cjs` 与 `qr_session_identity.cjs` 此前**从未被任何用例执行过**；`library_item_filter.cjs` 的 `urlinit` 变异**锚点失配的哑弹**）。迁移 head `f5a6b7c8d9e0` → **`g6h7i8j9k0l1`**（`uq_platform_accounts_xhs_identity` 改键，真实完整性库 `upgrade head` 实测跑通）；**零端点**（187 / 163 不变）；`tsc --noEmit` 0 error；两个文档校验脚本 PASS。⚠️ **三项真机验证留给用户**（本机不可代验）：扫码绑千帆是否拿到有效令牌 / 页面「已绑店」观感 / 托盘退出是否真的关掉窗口。<br>⚠️ 前值（同日 v1.0.1 第一阶段改造收口轮，**1123**）：**全量 1123 passed / 0 failed / 0 errors / 0 skipped**（2026-09-26 **第一阶段改造收口轮**实测，`--junitxml` 判定，**165.7s**；较上一个写回值 **1103** **+20**（期间多轮改动未回写数字，本轮按盘上实测订正）。本轮交付：**README 结合现状重写（含 v1.0.1 版本记录）+ 桌面打包 1.0.1 + 仓库清理**（历史 DB 备份 27 个 → 保留最近 2 份，`data/` 1.1G → 293M；`static/` 下 7 张 v1 期界面截图移除并同步改 README；打包自检新增 7 项禁止项：static / author / .workbuddy / .git / data / docs / frontend/src，已在 `packaging/README.md` 立「什么不该进包」清单；`packaging/build.ps1` 改后经 `[Parser]::ParseFile` 校验 0 error；产物 `spider-xhs-setup-1.0.1.exe` **86.5MB**（bundle 292MB，自检「全部通过」，含新增 7 项禁止项）。⚠️ 三条环境坑：① 该脚本**带 UTF-8 BOM 且必须 CRLF**，用 Python 重写时 `newline` 要传 `''`（否则 `\r\n` 二次翻译成 `\r\r\n` ⇒ PS 5.1 把行尾反引号续行判成语法错误）；② **打包前先清 `packaging/dist` 与 `packaging/build`** —— PyInstaller 起手删旧 `dist\SpiderXHS`（近千文件）会被本机删除保护拦下导致构建挂掉，v1.0.1 首轮就是这样红的；③ `cv2/data` 裁剪改成**失败只告警**（2026-09-26 起）—— 10MB 是体积优化不是正确性判据，删不掉就让包大一点，不许让整包失败）。；`tsc --noEmit` 0 error；两个文档校验脚本 PASS。⚠️ 前值（同日 §35.13 第四轮，1103）： **队列记录页批次 + 详情父子树 + 唯一批次号 + 批次号查询 + 清理两维度（不做存量兼容）+ 账号列 + 卡片只显队列类型 + 状态筛选在表头**（设计 13 §35.13 第二 ~ 四轮，用户同日六条口径「不做兼容逻辑，数据已全部清空，批次号 / 记录列只展示批次行，批次创建时间，注意使用父子树列表」+「支持按照批次清理记录，批次中存在正在执行或待执行得记录，则不支持该批次全部清理，也就是讲清理功能分为两个维度，批次维度和批次内的操作，批次操作维度」+「批次记录执行的账号」+「分析队列项 xhs_note_gen_item · 批次 xhs_note_gen_batch 队列管理中得批次为何固定得，每次应事一个逻辑自动生成唯一批次号，不应是固定值批次 xhs_note_gen_batch，其它队列也存在这个问题」+「直接使用队列类型不就可以了，唯一 key 肯定是唯一得，不需要多一个批次」+「讲队列执行记录表中得状态筛选放到批次内，父子树表不使用同一个表头，在表头状态列加筛选条件」）：① **不做存量兼容**：顶级行**只列批次行**（`list_queue_record_roots` 去掉「批次行 ∪ 无父队列项行」的 union，连 `parent_task_id IS NULL` 判据一起去掉）；「批次号」/「批次创建时间」两列 render **只对 `isBatchRow` 渲染**（明细行返 `null`）—— 旧版「无父队列项行也进顶级」的兼容路径**整段删除**，「不做存量兼容」由本轮「顶级只批次行 + 两列专属」钉死。② **清理分两个维度**（§35.13⑧）：**批次维度** = 原子清整批 —— `batch_clean_blockers(children)` 数 `pending`/`running`（active）与 `failed`/`exhausted`（retryable），**任一 >0 ⇒ 整批一行不删**，返 `blocked:[{batch_no, reason, active, retryable}]`；**批次内维度** = 单条清（`CLEANABLE_STATUSES` = `done`/`cancelled`，一条一条点）；`clean_records` 返回拆成 `{batches:{cleaned,blocked}, items:{cleaned,skipped}, blocked:[…]}`。前端操作列拆 `onCleanBatch`（整批）/ `onCleanItem`（单条）；被阻批次按钮 `disabled` + Tooltip 原因（`batchCleanBlockReason`，含条数 + 「可展开明细逐条清理」出路）；`cleanOutcomeText` 分层报账（成功 / 部分受阻 / 无操作）。③ **测试侧回数**：行为脚本 120→**134** 条（N 段 7 条批次维度阻塞判据 + O 段 7 条分层报账）、变异 14→**16**（`batchblock` / `cleanreport`）；页面结构变异 16/16→**21/21** 全红（新增 `batchcol` / `batchtime` / `batchclean` / `noreason` / `nocleanreport`）；`test_task_records.py` 16→**18**（1 逻辑 + 变异参数化 14→**16**：新增 `batchblock` / `cleanreport` + 1 结构守卫补 §35.13⑧ 断言）；`test_api.py` +1（`test_batch_clean_is_atomic_and_blocked_by_unfinished_items` 5 段）。零迁移（沿用 `f5a6b7c8d9e0`）、零端点；`tsc --noEmit` 0 error + `vite build` 重建 dist；8000 已重启复核；两个文档校验脚本 PASS。⚠️ 结构守卫首轮**假绿**（页面 `cleanableCount` 也调 `batchCleanBlockReason`、modal `okText:"清理整批"` 让宽锚点被别处满足）⇒ 锚点收紧为**特指操作列**那两处代码后 21/21 全红。④ **账号列（第三轮）**：「**账号**」列**同一列两判据** —— **明细行**取自己 `payload.account_id`（入队快照；缺 ⇒ `-`、不在清单 ⇒ `账号 #id`），**批次行**取后端**按子行派生**的 `accounts`（`batch_account_ids`：去重 + FIFO 保序；单账号显名 / 跨账号「N 个账号」+ Tooltip 列清 / 空 ⇒ `-`）—— 与「批次状态 / 计数 / 耗时一律由子行派生」同一条原则：**批次行不存 `account_id`**（一次批量可跨账号勾选，存一个只会在跨账号时说错）。⚠️ **顺手补掉一个真缺口**：`note_sync_service._enqueue_sync` 原先**没写** `account_id`（发布队列取 `note.platform_account_id`、商品生成取 `account.id`，**三个队列只有同步漏了**）⇒ 修前同步记录整列 `-`；入队路径只有 `_enqueue_notes`（单条 + 批量共用）一个 ⇒ 补一处即全覆盖，**零迁移**（用户已清空数据、无存量回填）。前端账号名映射 = `fetchAccounts()` 拉一次 + lib 的 `accountNameMap()`（**只放账号名**，与账号下拉同口径；**不随 5 秒轮询重复拉**；拉不到安静退 `账号 #id`）；⚠️ 页面 `useState<Record<string, string>>` 撞上既有守卫「页面不许出现 `Record<string, string>`」⇒ 改由 lib 导出**具名类型** `AccountNameMap`。**测试回数（在 ③ 之上再加）**：行为脚本 134→**146**（+P 段 12 条）、变异 16→**18**（`account` / `accountname`）、页面结构变异 21/21→**24/24**（`noaccountcol` / `inlinaccount` / `noaccountfetch`）；`test_task_records.py` 18→**20**；`test_note_queue_service.py` 34→**35**（`batch_account_ids` 去重保序 / 缺值非法值跳过）；`test_note_batch_sync.py` 补**账号快照**断言；`test_api.py` 树用例补 `accounts == [7, 9]`（跨账号）+「明细行不带 `accounts`」。⑤ **卡片只显队列类型 + 状态筛选搬表头（第四轮）**：① **队列管理卡片底部只显队列类型**（`item_task_type`）—— 原先写成「队列项 `xhs_note_gen_item` · 批次 `xhs_note_gen_batch`」，后者是**注册表常量**（`QueueSpec.batch_task_type`，每个队列恒定）却被读成「批次号」（用户口径「为何固定得，每次应事一个逻辑自动生成唯一批次号」）⇒ 常量**不上屏**；真实批次号是任务行 `batch_no`（`NB…`，每次入队自动生成），只在记录页「批次号」列。⚠️ **字段仍下发**（`queueKeyForTask` 靠它把批次行归队做筛选 —— 不上屏 ≠ 能删）。② **状态筛选从工具栏搬到「状态」列表头**（用户口径「状态筛选放到批次内，父子树表不使用同一个表头，在表头状态列加筛选条件」）：`filters: STATUS_FILTERS` + **受控** `filteredValue` + `filterMultiple: false`（单选），工具栏只留队列筛选 —— 同一件事不留两个入口。③ 真过滤下沉 lib `filterBatchRows`（**只筛批次（父）行**，命中批次整批明细原样 —— 「父子树不使用同一个表头」的落地），页面列上**不写 `onFilter`**（写了 = 同一判据两份实现 + 连带筛明细，DEF-044）。**测试回数（在 ④ 之上再加）**：行为脚本 146→**152**（+Q 段 6 条）、变异 18→**19**（`statusdeep` 咬「明细也被状态条件切」）、页面结构变异 24/24→**27/27**（`batchlabel` / `statusheader` / `statustoolbar`）、`test_task_records.py` 20→**21**（+1 变异参数化）；⚠️ `noqueuekey` 首轮**假绿**（守卫只查 lib 里 `queueKeyForTask`「函数存在」，而变异只改**调用点**）⇒ 锚点收紧为**调用形态** `queueKeyForTask(row.task_type, queues) === queueKey`，变异脚本同时补 `LIB` 目标。<br>⚠️ **前值（同日 §35.13 第一版，1096）**：**队列记录页批次 + 详情父子树 + 唯一批次号 + 批次号查询**（设计 13 §35.13，用户口径「将队列执行详情表使用批次+详情父子树风格展示，批次逻辑不再移除，生成唯一批次号」+「新增列表支持使用唯一批次号查询」；**覆盖同日 §35.12① 的「批次层整层移除」**）：① 一次入队动作 = 一个**唯一批次号** `NB{yyyymmddHHMMSS}-{4}`（字母表去 `0/O/1/I/L`）；唯一性两道 = 应用层查重 ×8 + SQL 唯一索引 `ux_tasks_batch_no`（迁移 **`f5a6b7c8d9e0`**，**不回填存量行**）。② `GET /tasks?tree=1&batch_no=…` 返回顶级行 + `children`(FIFO) + 派生 `summary`（**默认平铺形状逐字段不变**）；批次状态 / 计数 / 耗时**由子行派生**（`batch_rollup`，**失败先于取消**）。③ 批次行**不可取消 / 重试**（400 `BATCH_ROW_NOT_OPERABLE`）；清理**只在子行全可清时**清整批（**该句已被本轮「清理两维度」取代** —— 批次维度改原子判定并返 `blocked`，批次内维度单条清，见本行头部 ②）；保留期只删**已无子行**的陈旧批次。④ 前端：`lib/task-records.ts` 白名单回到六种 + `isBatchRow`（**数据判据**）/ `queueKeyForTask` / `batchExecutionDetailLines`；页面重写为树形表格 + 批次号 `Input.Search` **受控提交**。⑤ **逐域回数**：端点 185→**187** / path 161→**163**（补记 §35.12⑤ 手动清理两端点 `POST /tasks/clean` / `clean-all`）、请求函数 161→**163**。⑥ 测试：行为脚本 104→**120** 条 + 变异 10→**14**（`batchtypes` / `batchrow` / `queuekey` / `batchdetail`）；页面结构变异 **16/16** 全红（**修复 3 条因页面重写失锚的旧锚点** + 新增 `noqueuekey` / `nobatchsearch`）；`test_task_records.py` 12→**16**；`test_note_queue_service.py` 30→**34**；`test_api.py` +1。真实库已实跑迁移（备份 `data/spider_xhs.db.bak-20260926-pre-batchno`）；8000 已重启复核 `/api/tasks` 含 `tree` + `batch_no`（paths 163 / operations 187）；`tsc --noEmit` 0 error + `vite build` 重建 dist（服务已下发新包）；两个文档校验脚本 PASS。⚠️ 前值：**全量 1079 passed / 0 failed / 0 errors / 0 skipped**（2026-09-25 **商品生成笔记走队列（单 + 批量）+ 队列记录页新增商品生成队列与队列内详情记录逻辑 + 清理任务改名「队列记录清理」**（用户当晚三条连续口径：「单独生成商品笔记和批量生成商品笔记均需走队列，再队列记录页新增相关逻辑和队列内详情记录逻辑」；「笔记任务记录清理应扩展为队列详情记录清理，主逻辑不变，只保留最近15天的记录数据，任务名称修改为队列记录清理」）（设计 13 **§31.10 新增节** = 队列化第三块：发布 §31.5 / 同步 §35 早已入队，生成是最后一条挂在请求线程里的链路 —— 一篇 ~6–21s、批量数分钟，同步段扛不住）：① 两个生成端点同步段只做门禁（绑店→ark→台账，确定性答案 ⇒ 不过仍 **400/404 结构化**）+ 入队即返回；三步生成 + 轮询 + 落库在 `note-generate-worker` 逐项跑；新增 `GET /xhs/product-notes/generate-queue`（快照键名 **`generated`** —— 底座通用形状 `synced` 必须显式改名，透传 = 进度卡恒 0 而**后端不报错**，这类静默退化由守卫钉死）。② 底座 `note_queue_service` 扩成**三个队列共用一份**：`xhs_note_gen_item`/`xhs_note_gen_batch` + `QUEUE_REGISTRY` 第三行（`note_generate`，**`note_bound=False`**）⇒ 队列记录页**零前端改动**自动多一张卡 / 一个筛选项（§35.11「后端下发」口径的第一次实证）；`enqueue_item` 幂等主键扩展为 `note_pk` **或** `item_id`（生成项不绑定笔记 ⇒ 只认 note_pk 会把所有生成项互为重复）；`finish_item(extra=)` 成功后补 `note_id`/`note_title`；`snapshot` 条目两种来源；`cleanup_records` 清理范围**从注册表派生**（写死两个类型 = 新队列记录永远不清）。③ worker 四件从第一版补齐（DEF-069 同款：篇内 `BaseException` 网 / 循环外层网 / 看门狗 30s+指数退避+线程探针 / 停止出口）+ `lifespan` 起第三个 worker（`test_scheduler_sync.py` 的打桩与 `woken == ["sync","publish","generate"]` 顺序断言同步补）。④ 队列记录页「队列内详情记录逻辑」：详情抽屉子行主体列按队列项类型分叉 —— 同步 / 发布显笔记标题（按需拉），**商品生成显商品名**（`isProductGenItem`/`genItemProductText`：`item_name`→`item_id`→`-`），终态后附带「已生成《note_title》」（worker 写进行体）；行为脚本 **56→72 条**（+`genitem` 变异：抹掉商品名优先级必须红）；结构守卫 +`nosubject` 变异（页面变异 **13→14/14** 全红）。⑤ 清理任务改名：`system.note_task_record_cleanup`「笔记任务记录清理」→ `system.queue_record_cleanup`「**队列记录清理**」（`NOTE_TASK_RECORD_TYPES`→`QUEUE_RECORD_TYPES`、`cleanup_note_task_records`→`cleanup_queue_records`；**主逻辑零变化**）；迁移 **`c2d3e4f5a6b7`**（新唯一 head，revises `b2c3d4e5f6g7`）把存量 `scheduled_jobs` 行 + `scheduled_job_logs` **执行时快照**（`job_key`/`job_name`）一起改 —— 只改任务行不改日志 ⇒ 同一任务的历史被劈成两半（任务中心按 job_key 筛选）；apscheduler job id 是行主键 ⇒ 改名不产生孤儿注册。⑥ 测试：新增 `tests/backend/test_product_note_gen_queue.py` **12 条**；`test_product_note_publish.py` 4 条生成端点用例换锚（响应体断言 → worker 跑完后的库 / 队列项）；`test_note_queue_service.py` **21→25 条**；`test_scheduled_jobs.py` +1 迁移用例（并重建其未提交的注册表 8→9 与中文名守卫）；`task_records.cjs` 56→72；全量较 1061 **+18 逐位吻合**。⑦ 端点 184→**185**/path 160→**161**（`GET /generate-queue`）、请求函数 160→**161**（`fetchGenerateQueue`）；**逐域回数同步 12 处**，双校验 PASS；`tsc --noEmit` 0 error；变异：行为脚本 **8/8** + 结构守卫 **14/14** + 目标中文名 **3/3** 全红。⚠️ **本轮最大事故（REF §B heredoc 纪律的更狠变体，已写入当日日志）**：给 `test_scheduled_jobs.py` 追加迁移用例时又用 `cat >>` ⇒ **文件头部 107 行被顶掉**；且该文件**已在 git 但 HEAD 是旧版**（§35.9 的未提交改动）⇒ `git checkout` 把未提交成果一并丢。恢复 = 抽新内容 → checkout 还原 → 按「HEAD 49 vs 工作版 50 条」差集重建（注册表 8→9 + 新键 + 中文名守卫全文，它被 `mutate_verify_schedule_target_names.py` 的 GUARD 引用）→ `upgrade head` 改定点 → 变异锚点 3/3 红验证重建无误。**教训：heredoc 追加对「有未提交改动的已跟踪文件」是双杀（毁头部 + 诱导 checkout 丢成果）；追加内容一律先 Read 尾部再 Edit。**）。⚠️ 前值：**队列记录页重构：两个 Tab（队列管理 / 队列执行记录）+ 只收队列 + 队列清单后端下发**（设计 13 §35.11，用户当天三条口径连续纠正）（设计 13 §35.11 = §35.9 分段落地的 ⑥，持久化闭环最后一段）**：前面 ①~⑤⑦ 把「跑」修好了（落库 / 续跑 / 清理 /单条入队），这一段把「**看得见**」补上 —— 否则持久化只是让记录躺在 `tasks` 表里，用户既不知道跑到哪了、也没有取消 / 重试入口。① 入口 = **独立页面**（不是抽屉）：任务记录是**全局**的（账号级同步 / 商品同步 / AI / 抓取 / 监控都在 `tasks` 表），挂内容库页里别处记录看不见。② 与「任务中心」**两页并存**：`/tasks`（`scheduled_jobs`）管「什么时候跑」，`/task-records`（`tasks` 表）管「跑完了什么」—— 名字像但不是一回事，路由 / 菜单 / 文案三处分清。③ **零新端点、零新表**：复用 `GET /tasks` / `GET /tasks/{id}`（含 `children`）/ `POST /tasks/{id}/cancel` / `/tasks/{id}/retry` 四件（持久化底座刻意复用 `tasks` 表的红利，DEF-044）。④ 纯逻辑单独成文件 `lib/task-records.ts`（类型中文名 / 状态映射 / 可取消可重试判据 / 耗时格式 / 结果摘要）⇒ 页面只渲染，行为脚本 esbuild 打包跑 **47 条**真实断言（不渲染也能验证）。⑤ 详情抽屉必须显示子行 `payload.reason` —— 同步 / 发布失败的真实原因**只在子行给一次**（不落库到笔记行），不显示就是把用户的自救路径拿走（DEF-058）。⑥ 缺值口径：耗时缺值 `-`（不是 0）；类型 / 状态**表外原样显示**（不造名，DEF-057）。⑦ 有未终态才 5s 轮询、全终态即停。新增 **TC-SCH-30/31**（均 P1）⇒ 自动化调度 29→31、合计 356→**358**、P0 不变 113；页面文件 24→**25**、页面路由 20→**21**、请求函数 158→**159**（**逐域回数**已同步三份文档 + AGENTS/README/技术栈，双校验 PASS）；全量 1058（较 1050 **+8** = 新增 `test_task_records.py` 8 条，逐位吻合）；`tsc --noEmit` 0 error；变异：行为脚本 **6/6** + 结构守卫 **7/7** 全红。⚠️ 结构守卫判据必须落在 `_code_only` 剥注释之后的代码上 —— 页面 docstring 会提到被守护的符号名 ⇒ 不剥就被自己的注释命中（把 import 改成别的文件这种破坏仍然绿，实测三轮才收敛到 7/7）。⚠️ 前值：**单条「同步」按钮入队（与批量同一个队列/worker，设计 13 §25.4 / §35.10 = §35.9 分段落地的 ⑦）+ 进度卡文案「发布成功 / 同步成功」**：① `POST /notes/{id}/sync` 从**同步阻塞执行**改为**入队即返回**（与 `POST /notes/{id}/publish` 同形）—— 单条阻塞 + 批量队列并存时**两条链路各打各的平台请求**（www 额度 5 次/分钟下互相插队、限流等待叠加），且单条把 HTTP 请求挂十几秒；`sync` 字段从逐项结果（`status` / `content_updated` / …）变成**入队回执** `{ok, status:"queued", note_pk, note_id, already_queued?}`，终态与真实原因搬进 `GET /notes/sync-queue` 的快照 `finished` —— **与批量同一个出口**，前端不另走一套。② **单一实现**：`start_batch_sync` 的入队体抽成 `_enqueue_notes(..., *, gate, gate_message, max_items)`，单条 `start_sync_note` 与批量**只差门禁函数 / 门禁文案 / 上限三处**（DEF-044）。③ ⚠️ **门禁仍是单条那把尺子**（`note_supports_sync`：`synced` **或**已发布）—— **不许**顺手收紧成批量尺子（仅 published），否则把 2026-09-23 修掉的「按钮在、点了 400」找回来；门禁不过**仍 400 结构化**（确定性答案，不是异步回执）。④ 前端报「已加入同步队列 / 已在同步队列中」+ `trackSyncNotes([id])` 交给**同一个轮询器**（与批量共用 `pollSyncQueue` / `stopSyncPolling`），按钮 loading 由快照 `running + queued`（`syncQueueNoteIds`）接管；`SavedNoteSyncResult` 类型改为入队回执 `SavedNoteSyncEnqueueResult`。⑤ 进度卡文案：「成功」→「**发布成功**」/「**同步成功**」。⑥ 测试侧两处关键：`api_env` 夹具补 `monkeypatch.setattr(nss, "_session_factory", SessionLocal)`（worker 自建 Session 必须落**临时库**，否则既跑不到本篇又是数据隔离事故）+ teardown `stop_sync_worker()` 等库里没有 `running` 行（worker 是进程级全局线程，不收会跨用例抢队列）；`test_single_sync_is_idempotent_when_already_queued` 必须用**门控替身**造「已认领未跑完」确定态（真实替身秒级返回 ⇒ 第二次点击第一项可能已终态，那是**竞态假象**不是幂等失效 —— 实测 `-n auto` 下偶发红过一次）。新增 **TC-LIB-52/53**（均 P1）⇒ 内容库 52→54、合计 354→356、P0 不变 113；全量 1050（较 1048 +2，逐位吻合）、定向八文件 258；`tsc --noEmit` 0 error；**零迁移、零端点**；两个文档校验脚本 PASS。⚠️ 前值：**发布队列切持久化 + 发布 worker 守活 + `lifespan` 重启接线（设计 13 §35.9 分段落地的 ④⑤）**：① 发布队列的队列项真相源同样从**纯内存 `deque`** 迁到 **`tasks` 表**（`xhs_note_publish_item` / `xhs_note_publish_batch`）—— `_publish_queue` / `_publish_results` / `_publish_running` / `_publish_seq` / `_publish_done_events` **整段删除、不留影子**（**绝不内存 + DB 双写**）。⚠️ **adapter 不进队列**：它不可序列化、重启后必然失效 ⇒ 队列只存 `note_pk`，worker 用 `_publish_adapter_for()` **现场重建** —— 按笔记的 `platform_account_id` 取**库里当前有效**的 ark 令牌（§18.14：一个账号一个平台一个令牌），取不到 ⇒ 记 `failed` + `QIANFAN_CREDENTIAL_MISSING`（**不借会话、不发探活**）。端点工厂来自 FastAPI 依赖注入、**worker 线程够不着注入层** ⇒ `_publish_adapter_factories`（`tasks.id -> factory`）入队时记一次、认领后跑完即 pop —— ⚠️ 它是**缓存不是真相源**：进程重启后为空 ⇒ `_default_publish_adapter_factory()` 回落千帆工厂，那正是续跑该走的来源。② **发布 worker 守活**（与 §35.8 同型缺口，改造前**四件全缺**：只有 `except Exception` 内网、无循环外层网、无看门狗、无停止出口）⇒ 补齐篇内 `except BaseException`、循环**外层网**、看门狗 `start_publish_worker_watchdog`（30s 巡检 + 指数退避 + 探针只信**活着的同名线程**）、停止出口 `stop_publish_worker`（置位 = 不再认领新任务）。③ **快照键名契约**：底座 `snapshot()` 回的是通用形状（`synced` / `finished`），发布域必须是 `published` 且**不含** `finished` ⇒ 必须**显式改名**；直接 `return` 底座结果 ⇒ 前端拿到 `synced`、发布进度卡三数恒 0，**而后端不报错**（这类静默退化由守卫钉死）。④ **`lifespan` 重启接线**：`recover_interrupted()`（两类队列项一起复位）→ `recover_publishing_notes()`（把卡在 `publishing` 的笔记行复位）→ `ensure_sync_worker()` + `ensure_publish_worker()`（原 `_ensure_*` **公开化**，复位完**立刻唤醒**，不等看门狗 30s 巡检）→ 两个看门狗 → `yield` → finally 停两个看门狗。⚠️ **先复位再唤醒是判据**：先唤醒的话 worker 会去认领那些本该被复位成 `pending` 的 `running` 行（认领只认 `pending` ⇒ 白跑一趟）。⚠️ `tests/backend/test_scheduler_sync.py::test_scheduler_is_not_started_when_master_switch_is_off` **真跑一遍 lifespan** ⇒ 新增的恢复 / 唤醒 / 两个看门狗**全部打桩**（否则拿**真实运行库**做队列恢复、并起常驻 worker 认领真实数据 = 数据隔离事故，不是测试噪声）。⑤ 测试：**新增 `tests/backend/test_publish_worker_watchdog.py` 15 条**（探针 / 篇内网含「笔记不许卡在 `publishing`」/ 凭据拿不到记失败 / 循环外层网 / **重启续跑：库里 `pending` 行是全部交接物且 adapter 按库现取** / 工厂缓存命中与清理 / 看门狗重建复位中断行 / 重试用尽 / 退避与静默 / 停止出口 / **快照键名契约** / 跨租户 / lifespan 接线与顺序），`test_product_note_publish.py` 29 条**只改一处夹具**（`_wait_publish_idle` 从「等内存位 `_publish_running` 收回 None」改「等库里没有未终态发布项」）；**+15 逐位吻合**（1033 → 1048）。<br>⚠️ **前值**：**全量 1033 passed / 0 failed / 0 errors / 0 skipped**（2026-09-25 **同步队列切持久化（设计 13 §35.9 分段落地的 ③）+ 调用目标中文名（含日志详情弹窗）**：① 队列项真相源从**纯内存 `deque`** 迁到 **`tasks` 表**（复用既有表，不造第二张，DEF-044）—— `_sync_queue` / `_sync_results` / `_sync_running` / `_sync_seq` / `_sync_done_events` **整段删除、不留影子**（**绝不内存 + DB 双写**：那是两份真相源，一致性迟早崩），内存只留一个 `_sync_wakeup` 唤醒信号（**是提示不是队列**）；worker 改 `claim_next_item()`（条件 UPDATE 按 `rowcount` 防抢同一行）+ `_sync_wakeup.wait(timeout=SYNC_IDLE_POLL_SECONDS=5)` 兜底巡检（**唤醒信号丢了也只是晚 5 秒**，DEF-048 安全网不条件性）；`start_batch_sync` 惰性开批 + 整批一个事务 + **提交之后才唤醒**（worker 自建 Session 看不到未提交的行）；`_revive_sync_worker` **先** `recover_interrupted` 复位 `running` 行**再**起新 worker（反序 ⇒ 新 worker 刚认领的一条被复位抢回、同一条跑两遍）；`stop_sync_worker` 语义改为「**不再认领新任务**」（「不丢正在排队的篇」改由**库**保证：未跑的项还是 `pending` 行）；`sync_queue_snapshot` 直接委托底座（**响应形状逐字段不变 ⇒ 前端零改动**）。② 用户口径「笔记任务记录清理，调用目标需要配置中文名称」—— 现象出在新增内置任务 `system.note_task_record_cleanup` 上，但**根因是口径没被守卫钉住**（中文名唯一来源 = `JobSpec.name`）：任务列表列早在 R18 就是两行，**日志详情弹窗**却一直只显 `snapshot.job_key` 裸代码键 ⇒ 弹窗拉齐为「中文名 + 代码键」两行，中文名由 `targetNames` prop 传入（同一份 `/schedule/targets`，弹窗内不另写映射表）。⚠️ **真机易混点**：8000 进程 19:01 启动、`job_registry.py` 19:14 落盘 ⇒ 旧注册表只有 7 项、界面显示裸键 —— **动过 `backend/app/**` 必须重启复核**（本次重启后即正常，`/schedule/targets` 9 项 + 库内每用户 8 条内置行含中文名），别急着改代码。本轮全量实测，`--junitxml` 判定，**127.0s**；**本轮新增 +3 逐位吻合**：`test_sync_worker_watchdog.py` **9→11**（+2 = 重启续跑 / 重试用尽）+ `test_scheduled_jobs.py` 中文名守卫 **1**；定向四文件 **87 / 0 / 0 / 0**（17.3s）；变异 `.workbuddy/scripts/mutate_verify_schedule_target_names.py` **3/3 全红** + `SCHEDULE_MUTATE=detailtarget` 前端实测红；`tsc --noEmit` 0 error；**零迁移、零端点**（端点 183 / path 159 / 请求函数 158 / 数据表 28 / 页面 24 全不变）；两个文档校验脚本 PASS。⚠️ **前值订正**：AGENTS.md 此前停在 **989**（中间几轮到 v1.55 的 **1030** 未回写 AGENTS.md），本轮按实测订正为 **1033**（= 1030 + 本轮 3 条）。前值 **全量 989 passed / 0 failed / 0 errors / 0 skipped**（2026-09-25 **调度器守活（DEF-067，真机日志排查）**：APScheduler 主循环在 `jobstore.update_job` 上抛 `database is locked` ⇒ **整个主循环线程带异常退出**（`running` 仍报 True、端点/UI/日志三处零信号），实测停摆 14 分钟、**20/36 行 `next_run_time` 冻结**；根因 = `SQLAlchemyJobStore` **自建引擎**不带 `core/database.py` 的 `busy_timeout` 事件监听（只剩 pysqlite 默认 5 秒）+ APScheduler **不自愈**。修法两半：**治因** `engine_options` 给足 `timeout=30`；**治果** `scheduler_thread_alive()` 线程探针 + `revive_scheduler()` **换新实例** + `start_scheduler_watchdog()`（`main.py::lifespan` 起停、30 秒巡检、失败指数退避）；并把 `/api/schedule/status` 的 `running` 改走线程探针。本轮全量实测，`--junitxml` 判定，**119.1s**；较 **980** **+9** = 新增 `tests/backend/test_scheduler_watchdog.py` **9 条**（逐位吻合）；变异 `.workbuddy/scripts/mutate_verify_scheduler_watchdog.py` **11/11 全红**；**零迁移、零端点**（183/159/158/28 全不变）；真机重启后 `.workbuddy/scripts/probe_scheduler_alive.py` 实测冻结 **20/36 → 0/36**、`system.due_publish` 每分钟落日志；两个文档校验脚本 PASS。⚠️ **前值订正**：下面写的 979 是**平台话题清洗轮**的写回值，此后又给 `test_legacy_topic_repair.py` 补了第 8 条（跨租户判重）而**未回写数字** ⇒ 盘上实际 **980**（本轮按实测订正）。）。前值 **全量 979 passed / 0 failed / 0 errors / 0 skipped**（2026-09-25 **平台话题存量清洗**（同日追加，用户口径「本地数据可删除，可修复，**以三方数据为准**」）：`merge_legacy_topic_markers.py --apply` 三步（并脏名，按 `(user_id, name)` 判重 → **补关系**走 `apply_sync_topics` 同一实现 → 摘正文）+ 幂等复核；本轮全量实测，`--junitxml` 判定，**170.0s**；较 972 **+7** = 新增 `test_legacy_topic_repair.py` 7 条。⚠️ **原脚本只摘正文不补关系 = 把话题删光**（91 篇带标记笔记里 64 篇本就零关系）⇒ 改三步后实测：脏行 124→0、正文带标记 91→0、有话题关系的笔记 29→93、`topics` 335→**691**、`note_topics` 356→**800**、词典孤儿 0；**与备份逐篇对账「非话题残文逐字一致」**（只摘话题、正文零丢失）+ 91/91 篇正文话题全部落到关系行；备份 `data/spider_xhs.db.bak-20260925-160556-pre-topic-marker`；两个文档校验脚本 PASS）。前值 **全量 972 passed / 0 failed / 0 errors / 0 skipped**（2026-09-25 平台话题形态 `#名字[话题]#` 修复，本轮全量实测，`--junitxml` 判定，**171.1s**；较 964 **+8**（`test_note_topics.py` 7 + 生成→发布全链路 1）；变异 `.workbuddy/scripts/mutate_verify_topic_platform_form.py` **10/10 全红**；`tsc --noEmit` 0 error；两个文档校验脚本 PASS。⚠️ 根因：发布 `desc` 回填 ` #名字`（平台认 `#名字[话题]#`）**且**把进了 hash_tag 的话题从 desc 排除 ⇒ desc 无 `#`；解析又不剥标记 ⇒ 124 行脏话题名 + 91 篇正文残留）。前值 **全量 964 passed / 0 failed / 0 errors / 0 skipped**（2026-09-25 两张进度卡改「本轮三数」+ 同步节流加固，本轮全量实测，`--junitxml` 判定，**112.6s**；较 947 **+17**（`test_sync_pacing_and_dsl.py` 16 + 发布快照三数 1）；变异 `.workbuddy/scripts/mutate_verify_progress_and_pacing.py` **8/8 全红**；`tsc --noEmit` 0 error；两个文档校验脚本 PASS；8000 已重启复核 **183 / 159** + 前端产物重建）。前值 **全量 947 passed / 0 failed / 0 errors / 0 skipped**（2026-09-25 内容库页提示消息化 + 同步进度卡口径迭代，`--junitxml` 判定，**140.8s**；本轮只**新增 1 条守卫** `test_library_page_notices_use_message_and_no_longer_occupy_layout`（6 变异全红）+ 改写 `test_library_batch_sync_with_progress_card` ⑦，另 +5 是 941 声明之后落地的用例 ⇒ 实测 947（不是 942）；`tsc --noEmit` 0 error，两个文档校验脚本 PASS。⚠️ 同轮批量同步「405 Method Not Allowed」= 8000 上跑的是**缺 §35 路由的旧进程**，重启（杀 uvicorn ⇒ launcher 5s 内用新代码拉起）后 `/openapi.json` 含 `/api/notes/batch-sync` + `/api/notes/sync-queue`、POST 由 405 → 401 ⇒ 口径：**路由有新增就必须重启并复核 openapi，别只看源码**）。前值 **全量 941 passed / 0 failed / 0 errors / 0 skipped**（2026-09-25 §35+§36 本轮全量实测，`--junitxml` 判定，**132s**；较 928 基线 **+13**：`test_note_batch_sync.py` 6 + 同步进度卡/工坊合并结构守卫 2 + `note_filters.cjs` 真正接入 pytest 1+4 变异；中循环受影响文件集 33 个 **785 passed / 0 failed**，新增文件隔离 **6/6** 绿，`tsc --noEmit` 0 error，两个文档校验脚本 PASS）。前值 **本轮改动文件 26/26 绿**（`test_product_note_publish.py` 25 + 进度卡守卫 1，`--junitxml` 判定）；全量 **928 passed / 0 failed / 0 errors / 0 skipped**（2026-09-25 全量实测，`--junitxml` 判定；**全站消息提醒统一 antd message**（设计 04 §3.6，用户口径「所有消息提醒参考 antd 成熟组件，不要在页面暂用空间」）：14 个页面/组件的 inline Alert 消息条 + 任务中心 notice 弹窗 → `App.useApp()` 的 `message.*`；`my-notes`/`shop-items`/`add-account-drawer` 三处静态 message 一并收敛（静态方法不吃 ConfigProvider 主题，antd 官方不推荐）；新增 3 条守卫（旧形态回退 / 静态导入白名单 / 三分法边界）且过变异验证；`schedule_center.cjs` R22 弹窗断言改写为 message 断言（原句留 docstring）；`add_account_modal.cjs` harness 补 `<App component={false}> 包裹（App.useApp() 在 App 之外返回空对象，真机不炸是因为 providers.tsx 有 <App>）；tsc --noEmit 零错误；⚠️ 同文件多Edit并行有竞态（部分编辑被回退），修复走 Python 单写脚本 + tsc 驱动核对）；前值全量 **918 / 5 failed**（2026-09-25 实测；5 条全是 `test_product_note_publish.py` 的**异步发布用例**——§31.5 共享 worker 队列 + `wait_publish_done` 超时 + Windows 临时目录锁的**负载相关 flaky**：该文件隔离跑 25/25 绿、带插件的全量跑 **918 / 0 / 0 / 0** 绿，双 AI 会话并发跑全量时高负载下复现；话题功能本身（`test_note_topics.py` 31 条 + 改写的 `test_note_fields.py` 13 条）全绿）；前值全量 **868 passed / 0 failed / 0 errors / 0 skipped**（2026-09-24 全量实测，`--junitxml` 判定，**127s**；**DEF-066 定时任务中心账号维度隔离整改**（设计 15 §4.1.1）：内置任务行按用户各持一份（注册即播种）、建/改同一把尺子（`account_ids` 归属校验两条路）、`scheduled_job_logs` 加 `user_id` 快照列且三端点按它收口、7 个系统任务执行按行主收口（`JobSpec.user_scoped` + runner 注入，行主 NULL 拒绝执行）、删两个全用户包装、迁移 `a0b1c2d3e4f5`（新唯一 head，revises `k1l2m3n4o5p6`）；新增 9 条（隔离 6 + 新迁移用例 1 + 注册播种 1 + 变异参数化 1）；真实库已实跑迁移（6 用户 × 7 任务、全局行清零、日志全部有归属），备份 `data/spider_xhs.db.bak-20260924-pre-def066`。**前值 859 / 150s**：R25 测试提速与规范固化（`pytest-xdist` 全量 `-n auto` 499s→150s；前端行为脚本盲等改 `waitFor` 省约 96s；公共夹具 + 17 条可执行守卫；⚠️ 写文档禁用 `cat >>`）。演进：431 → 443 → 446 → 448 → 458 → 463 → 468 → 473 → 480 → 511 → 532 → 539 → 557 → 719 → … → 823 → 837 → 859 → 868 → 928 → 941 → 947 → 964 → 972 → 979 → ~~989~~ → 1033 → 1048 → 1050 → 1058 → 1061 → 1079 → 1096 → 1099 → 1102 → 1103 → 1123 → **1134**）：`"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe" -m pytest tests/ -q -n auto --junitxml=...`（判定只看 xml 四数，不看退出码） |
| 用例库 | **335** 条 / P0 **111**（2026-09-25 设计 13 §35+§36：+7 TC-LIB-35~41（P0×3）批量同步与同步进度卡、+4 TC-NWS-01~04 笔记工坊新域；内容库 35→42、笔记工坊 0→4。本行同步订正 v1.42–v1.46 期间滞后的 14 条，现与 `03_测试用例库.md` §14 逐域回数一致） |
| 测试完成度 | **47.7%**（148/310；分子为历史快照，口径未随用例库扩张重算 —— 待按「有自动化覆盖的用例数」重新定义，勿当精确值引用） |
| 缺陷 | **74** 条，关闭率 **27.0%**（只算 `[VERIFIED]` = 20/74）。**2026-09-26 DEF-070 / 071 / 072 / 073 / 074（五条 · `[FIXED]`，同轮修完）**：用户装 **v1.0.1** 实测九个问题（归并五组，原文与对照表见设计 13 **§18.17**）—— ① **DEF-070（🟠P1）** 千帆扫码**只轮询不换票** ⇒ 拿到的 jar 打千帆一律 `-13000`（用户「密码登录正常、扫码异常」在代码里是必然）；② **DEF-071（🟠P1）** 唯一索引键**含 `sub_type`** ⇒ 同一小红书 ID 落两行（迁移 `g6h7i8j9k0l1` 改键 + `strongest_sub_type` 取强链路 ⇒ 合并不丢发布能力）；③ **DEF-072（🟠P1）** 登录成功**不建店** ⇒ 我的笔记 / 商品列表照旧「尚未绑定店铺」；④ **DEF-073（🟡P2）** 绑定 / 补登成功后**弹窗不关** + 默认蒙版在**深色主题**下不可见；⑤ **DEF-074（🟠P1）** 托盘退出**窗口不关**（`_app_window_proc` 单变量 + Chromium **单例转交** ⇒ 按 pid 杀落在空壳上）。**2026-09-24 DEF-066（🟠P1 · `[FIXED]`，同轮修完）**：定时任务中心**账号维度隔离三缺口**（用户口径「数据库中的所有数据均需要按照平台账号维度进行数据隔离，避免多用户相互操作同一份数据」）—— ① 内置任务是 `user_id IS NULL` 的全局共享单行（任何登录用户可改/暂停/删除/执行，A 配的 account_ids 写在共享行上，B 执行同一行跑 A 的账号）⇒ 内置行**按用户各持一份**（注册即播种）；② `POST /jobs` 不过 `account_ids` 归属校验（PUT 过）⇒ 建/改同一把尺子；③ 调度日志零隔离（表无 `user_id`，三端点不过滤）⇒ 日志加 `user_id` 快照列并按它收口。另 7 个系统任务执行按行主收口（`JobSpec.user_scoped` + runner 注入，行主 NULL 拒绝执行）、删两个全用户包装、清理 Task 行 `user_id or 1` 兜底、迁移 `a0b1c2d3e4f5`（新唯一 head）。设计 15 新增 **§4.1.1** 专节；`test_scheduled_jobs.py` 33 → 50 passed。历史条目与逐条根因见 `docs/04_测试文档/04_测试跟踪与缺陷标记.md` §2（按 DEF 编号检索，勿整读）。 |
| 前端页面 | **25** 个（2026-09-25 §35.11 任务记录页 +1 = `task-records-page.tsx`（接线路由 / 菜单，零新端点）；2026-09-25 §36 笔记工坊：图片 / 视频 / 草稿三坊合并为 `note-workshop-page.tsx` 一个页面（Tabs 隔离），页面文件 23→24、页面路由 22→20；死代码页仍 **1** = `drafts-page.tsx`） |
| 千帆接口 | 店铺信息 / 商品笔记列表（**支持 `note_ids` 按笔记查询**）/ **商品列表（含搜索条件 + 创建时间）** / **挂链笔记反查（本地）** / **登录态巡检（只读探活）** —— 全部只读，写接口零触碰 |

> 复核命令：`curl -s http://127.0.0.1:8000/openapi.json` + §2 的命令。
>
> 🔒 **本表是「数字口径」的唯一权威**：`docs/README.md` §五、`docs/02_设计文档/00`、`03`、`05`、
> `docs/03_开发文档/02`、`docs/04_测试文档/01` 里出现的同类数字**都被 `verify_doc_counts.py`
> 逐处比对**，任何一处漂移都会让它 `OVERALL: FAIL`（2026-09-21 建脚本时**一次查出 13 处**——
> README 停在 v1.7 期的 147 端点 / 24 表 / 269 passed，而它自己的校验一直是绿的）。
> ⚠️ 反向教训：**别用粗算的自动值去「更新」文档** —— 接线覆盖率因前端有动态拼路径 / SSE `fetch`，
> 正则反推只有人工口径的 ~85%，故该项**显式标注「快照、勿当实时值引用」，不进校验**。

> ⚠️ **三方令牌关系 = 「两套体系」（2026-09-20 真机 Chrome 矩阵实测，回答用户提问）**：
> **① 网页版**独立 —— `web_session` + `id_token`；**② customer 单点登录** —— `customer-sso-sid` +
> `customerClientId`，**创作者平台与千帆共用**，凭 `POST /api/cas/customer/web/service-ticket` 互相换取。
> 实测：**只注入创作者凭据即可直接进千帆**（`ark.xiaohongshu.com/app-system/home`，显示「观隅好物的店」）；
> **PC 凭据 → 千帆 ❌**（无 `customer-sso-sid`，被打回登录页）；**创作者/千帆 → www ❌**（无反向交换入口）。
> ⇒ 千帆绑定**理论上可由创作者凭据派生**，但**「免单独绑定」在服务端做不到** ——
> 浏览器最后一步是 `POST https://ark.xiaohongshu.com/api/edith/open/ssologin`（body `{system, ticket}`），
> **必须带 `X-s` / `X-t` 签名**，复现它 = 登录逆向 + 长期维护签名，**违反只读边界与硬约束 C4**。
> 已按此结论登记为**「不可交付」**（`docs/03` §4.10.9，含 A/B/C 三个备选方案）。**不要再试第三条路。**
> ⚠️ **`web_session` 存在 ≠ 已登录**（游客也会被种）→ **DEF-051 已修**：
> `has_web_session()` 现要求「有 `web_session` **且** 有 `id_token`」——`id_token` 是**零请求**可判的判据。
> ⚠️ **凭据只认本账号（2026-09-22 用户拍板，借用整条路已移除）**：**每个账号只使用自己账号
> 对应的平台有效登录令牌** —— 拿不到就 400 `WEB_SESSION_REQUIRED`（结构化原因 + 让用户补登录），
> **不向任何其他账号借会话、也不为此发探活请求**。`_borrowed_web_session` / `_jar_is_same_identity`
> / `_identity_key` 已**删除**（不是标注废弃）；`session_source` 只剩 `own`/`""`；能力端点恒零平台请求。
> 契约见 `docs/02_设计文档/12_…§17.10`；判定「同 platform 不限昵称」的 **DEF-052 随之失去载体**
> （台账已加「状态变更」行，原文与计数保留）。
> 详见 `docs/02_设计文档/12_…§17.6.2`（v1.22，v1.21 的「三个互不通用」被取代并附对照表）。

## 6. 常见任务 → 该看哪里（唯一真相源）

| 任务 | 真相源 |
|---|---|
| 「我的笔记」/商品/千帆 | `docs/02_设计文档/12_…§19、§24`（笔记↔商品映射）、**§25（商品列表页 + 搜索条件）**；需求 `docs/01_需求文档/03_…`（R1–R11 / D1–D17）与 `01/02_二开需求规格_商品笔记自动化.md`（R6） |
| **定时任务 / 调度器 / 调度日志** | **`docs/02_设计文档/15_功能设计_定时任务中心.md`**（2026-09-24 重构方案；`08` §1.1/§1.2/§2/§2.1 的被取代原句以注释保留） |
| 后端端点契约 | `docs/02_设计文档/05_功能设计_后端API与服务层.md` |
| 数据表 / 迁移 | `docs/02_设计文档/03_数据模型设计.md` |
| 页面与交互 | `docs/02_设计文档/04_功能设计_Web运营端.md` |
| 缺陷 / 测试跟踪 | `docs/04_测试文档/04_测试跟踪与缺陷标记.md` |
| 用例 | `docs/04_测试文档/03_测试用例库.md` |
| 决策史 | `docs/05_项目沉淀/01_架构决策记录_ADR.md`（最新 ADR-019：商品来源=千帆） |
| 二开任务与里程碑 | `docs/03_开发文档/03_二开任务分解与里程碑.md` |
| 文档地图 | `docs/README.md` |

## 7. 本仓库特有的坑

- **不是 git 仓库**（无 `.git`）：`git status/log` 一律报 `fatal: not a git repository`；建看板卡**不能**带 `project=`（会退化成 worktree 派发失败）。
- shell 是 **git-bash/MSYS**：`taskkill //PID` 会被路径改写，杀进程用 `powershell -NoProfile -Command "Stop-Process -Id <pid> -Force"`；原生程序路径要写 `C:/...` 正斜杠。
- **8000 吃构建产物**：改前端必须 `vite build`；改后端文案杀掉 uvicorn 即可（监督进程 5 秒内拉起）。判定「跑的是不是新代码」：**端点有增减**看 `/openapi.json` 的 paths 数；**只改内部逻辑**（paths 不变）则必须比 **uvicorn 子进程启动时间 vs 改动文件 mtime** —— 启动时间早于 mtime = 跑的是旧代码。
  - 查子进程：`Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ? CommandLine -like '*uvicorn*8000*'`；`Get-CimInstance`/`Stop-Process` 在 PowerShell 工具里**输出常被吞掉**，把结果 `Out-File` 到 `.workbuddy/tmp/*.txt` 再读。
  - ⚠️ **`wmic` 被沙箱黑名单拦截**（不可绕过），别用。
- **日志里「每 10 秒一次 `exited (code 3)`」= 多了一个 `run-app.py` 监督进程在抢 8000**（不是代码 bug）：两个 launcher 各起一个 uvicorn，抢到端口的那个健康存活，抢不到的那个每 5 秒失败重启一次。排查：列 `pythonw.exe` 的 cmdline，**保留持有 8000 的那个（看谁是活 uvicorn 的父进程），杀掉其余**；只杀 launcher 不够——已被它拉起的 uvicorn 要一并 `Stop-Process`。2026-09-21 实测存在（09-19 19:41/19:42 各起了一个，循环刷了 1 万+ 行日志）。
- **千帆登录态会过期**（`ark.xiaohongshu.com`，独立于小红书账号 Cookie）：失效时页面提示「重新绑定」，映射数据**不清空**。
