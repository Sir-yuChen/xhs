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
# ⚠️ 全量务必带 `-n auto`（pytest-xdist）：实测 499s → 150s（3.3×）
"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe" -m pytest tests/ -q -n auto

# 前端类型检查 + 构建（改前端后必须 build，8000 吃的是产物）
cd frontend && node node_modules/typescript/bin/tsc --noEmit && node node_modules/vite/bin/vite.js build

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
| ① 内循环 | **每改一次代码** | `pytest tests/backend/test_xxx.py -q` |
| ② 中循环 | 一个功能点做完 / 动了公共层 | `pytest <受影响文件集> -q -n auto` |
| ③ 外循环 | 收尾 / 用户明确要求 | `pytest tests/ -q -n auto --junitxml=...` |

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
| `.workbuddy/memory/MEMORY.md` | **7.2KB（≤8KB 硬上限）** | **整篇可读**；只含核心规范 + 索引，细节它不重复、会指路 |
| `.workbuddy/memory/REF_*.md` | 按需 | 参考资料（易错速查 / 运行坑等），**非核心，按需读**，由 MEMORY.md §0 索引 |
| `.workbuddy/memory/2026-09-*.md` | 25–60KB | 只按日期 `grep` 关键词，**勿整读** |

**顺序**：本文件 → `.workbuddy/memory/MEMORY.md`（**只含核心规范 + 「去哪找」索引**，整篇可读）
→ `docs/README.md`（文档地图）
→ 需要具体契约时**按 `§`/`TC`/`DEF` 编号精读** → 代码用 `search_files` 定位后按行读。

> 📌 **会话收尾纪律（2026-09-22 立为项目规范，强制执行）**：**每次会话结束前必须整理主记忆
> `.workbuddy/memory/MEMORY.md`** —— 目标是「**只保留核心规范和索引**」，**≤8KB**。新增内容**优先压缩进
> 已有条目，不追加新条目**；能通过索引检索到的一律不写进来。**非核心内容（易错速查 / 运行坑 / 过程 /
> 一次性结论）不落主记忆** —— 写当日日志 `YYYY-MM-DD.md`（append-only）、`REF_*.md`、或设计文档，
> 主记忆只留指针。历史事故过程按日期 grep 日志，**勿整读**。

## 5. 当前权威数字（2026-09-22 实测，改前先复核）

| 项 | 值 |
|---|---|
| 后端端点 | **180** operations / **156** paths（`GET /openapi.json` 实测；2026-09-24 定时任务中心重构（设计 15）：`/api/schedule` 新增 12 端点（占 8 条 path），删 `GET /api/tasks/scheduler/status` ⇒ 168→180 / 148→155；2026-09-23 v1.36 新增 `POST /api/xhs/qianfan/items/delete`；2026-09-23 v1.39 新增 `POST /api/xhs/login-sessions/qianfan/email-login`，设计 13 §18.15） |
| 数据表 | **27** 张（新增 `scheduled_jobs` + `scheduled_job_logs`（定时任务中心，设计 15 §4，迁移 `y8z9a0b1c2d3`）；`auto_tasks` 删 4 调度列 + `next_run_at`、加 `schedule_job_id`；2026-09-24；前值 25 张（新增 `note_goods`（笔记↔商品多对对关系表，设计 13 §26），2026-09-22 本批次；前值 24（新增 `shop_products`，T-802；**2026-09-22 移除 `shop_account_bindings`** —— 店铺↔账号回到 **1:1** 并由 DB 部分唯一索引强制，故 25 → 24。被取代的 N:M 原句保留在 `docs/02_设计文档/03_数据模型设计.md` §9.2） |
| 前端请求函数 | **156**（口径：`lib/api.ts` 里 `await http.` 的出现次数；2026-09-24 设计 15 +11 schedule 端点族、−1 `fetchSchedulerStatus`；2026-09-23 v1.36 +`deleteXhsShopItems`；2026-09-23 v1.39 +`loginXhsQianfanByEmail`） |
| 回归 | **868 passed / 0 failed / 0 errors / 0 skipped**（2026-09-24 全量实测，`--junitxml` 判定，**127s**；**DEF-066 定时任务中心账号维度隔离整改**（设计 15 §4.1.1）：内置任务行按用户各持一份（注册即播种）、建/改同一把尺子（`account_ids` 归属校验两条路）、`scheduled_job_logs` 加 `user_id` 快照列且三端点按它收口、7 个系统任务执行按行主收口（`JobSpec.user_scoped` + runner 注入，行主 NULL 拒绝执行）、删两个全用户包装、迁移 `a0b1c2d3e4f5`（新唯一 head，revises `k1l2m3n4o5p6`）；新增 9 条（隔离 6 + 新迁移用例 1 + 注册播种 1 + 变异参数化 1）；真实库已实跑迁移（6 用户 × 7 任务、全局行清零、日志全部有归属），备份 `data/spider_xhs.db.bak-20260924-pre-def066`。**前值 859 / 150s**：R25 测试提速与规范固化（`pytest-xdist` 全量 `-n auto` 499s→150s；前端行为脚本盲等改 `waitFor` 省约 96s；公共夹具 + 17 条可执行守卫；⚠️ 写文档禁用 `cat >>`）。演进：431 → 443 → 446 → 448 → 458 → 463 → 468 → 473 → 480 → 511 → 532 → 539 → 557 → 719 → … → 823 → 837 → 859 → **868**）：`"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe" -m pytest tests/ -q -n auto --junitxml=...`（判定只看 xml 四数，不看退出码） |
| 用例库 | **310** 条 / P0 **105**（2026-09-24 设计 15：+13 条 TC-SCH-12~24，自动化调度域 11→24） |
| 测试完成度 | **47.7%**（148/310；分子为历史快照，口径未随用例库扩张重算 —— 待按「有自动化覆盖的用例数」重新定义，勿当精确值引用） |
| 缺陷 | **66** 条，关闭率 **30.3%**（只算 `[VERIFIED]` = 20/66）。**2026-09-24 DEF-066（🟠P1 · `[FIXED]`，同轮修完）**：定时任务中心**账号维度隔离三缺口**（用户口径「数据库中的所有数据均需要按照平台账号维度进行数据隔离，避免多用户相互操作同一份数据」）—— ① 内置任务是 `user_id IS NULL` 的全局共享单行（任何登录用户可改/暂停/删除/执行，A 配的 account_ids 写在共享行上，B 执行同一行跑 A 的账号）⇒ 内置行**按用户各持一份**（注册即播种）；② `POST /jobs` 不过 `account_ids` 归属校验（PUT 过）⇒ 建/改同一把尺子；③ 调度日志零隔离（表无 `user_id`，三端点不过滤）⇒ 日志加 `user_id` 快照列并按它收口。另 7 个系统任务执行按行主收口（`JobSpec.user_scoped` + runner 注入，行主 NULL 拒绝执行）、删两个全用户包装、清理 Task 行 `user_id or 1` 兜底、迁移 `a0b1c2d3e4f5`（新唯一 head）。设计 15 新增 **§4.1.1** 专节；`test_scheduled_jobs.py` 33 → 50 passed。历史条目与逐条根因见 `docs/04_测试文档/04_测试跟踪与缺陷标记.md` §2（按 DEF 编号检索，勿整读）。 |
| 前端页面 | **23** 个 |
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
