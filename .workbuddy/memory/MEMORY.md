# XHS_ALL_IN_ONE · 主记忆（核心规范 + 索引）

> 权威数字/命令在 `AGENTS.md`；本文件只放**核心规范 + 索引**，**不整读大文档**。收尾必整理（≤8KB）；新增**压进已有条目**，非核心写日志。

## 0. 去哪找（索引）

| 要什么 | 去哪 |
|---|---|
| 开工入口（命令 / 硬约束 / 数字） | `AGENTS.md`（先读 §3/§5） |
| 文档地图 · 真相源 | `docs/README.md` |
| 缺陷 `DEF-NNN` · 用例 `TC-XXX-NN` | `docs/04_测试文档/`；功能契约按 `§` 精读 `docs/02_设计文档/` |
| 历史事故 · 怎么写 | 日志 `2026-09-*.md`（grep）· `tests/` |
| 易错 / 运行坑 / 排版坑 | `REF_易错与运行坑.md`（A–F 分区） |

## 1. 核心架构事实（不能违反）

- **一个账号、三个平台令牌**（§18.14）：`www`/`creator`/`ark`，共用**单向**（ark 超集）⇒ 按 jar 形态取令牌，**一次登录只更新自己那个平台**。
- **千帆取票只认本账号 `ark`**（`qianfan_cookies_for_account()`，无令牌返空串）；绑定表兜底**整条删除**。
- **店铺 ↔ 账号 1:1 由 DB 强制**（唯一索引 `uq_platform_accounts_shop_id_fk`）；绑定表**整表移除**（关系→`shop_id_fk`、凭据→`ark`、状态→`shop_state`，只经 `get/set_shop_state()`）。
- **通道由 jar 形态判**（`channel_for_cookies()`），不由端点/调用方写死；`sub_type` 只表达协议链路。
- **凭据只认本账号**：拿不到该平台令牌 ⇒ 400 + 结构化原因，**不借会话、不发探活**（§17.10）。

## 2. 核心规范（AGENTS.md §3 之外的执行细节）

**同步链路**
- **同步笔记 = 创作者会话硬门禁**（§21.4）：没有 ⇒ **不发任何源请求**（`CREATOR_SESSION_REQUIRED`），**不得退化成「哪个源能用就用哪个」**。
- **同步是全量**（§21）：翻到没有下一页；限流**等待式** `wait_allow()`；`ABSOLUTE_PAGE_CAP=500` 仅防死循环。⚠️ **取消上限与改造限流是一件事的两半**。
- **发布状态唯一字段**（§32.3）：`publish_status`（四态）权威、`published` 降为投影、唯一写 `set_note_publish_status()`。
- **三方同步 = `note_type=synced` + `publish_status=published`**（只升不降，§19.12/§25）；采集 / 监控 / 批量保存 ⇒ `local` + `pending`。
- **同步细节**（§19.14/§24/§27）：范围解析只在 `resolve_time_window()`（日期坏退全量须说明）；已存在行正文只在空时写、图集只增不删；对账匹配键 = 唯一 ID、枚举不完整连标记也不打；详情页内联补齐（时长 ≈ 待补行数 × 12 秒）。
- **三表分工**（§26/§28）：`note_goods` 多对多（唯一键 `(note_id, product_id)`）；**商品信息不进 notes**；**删笔记 / 删商品 ⇒ 关系行一并删**（FK 不级联 ⇒ 漏一边 = 孤儿行）。
- **商品台账 / 商品笔记 / 内容库筛选**（§30~§33 / §31 / §25.7）：台账只读本地 `shop_products` + 门禁 `ledgerPrerequisite`（坑见 REF §E）；生成不发布；`canSyncNote` 与后端 `note_supports_sync()` 同改。
- **定时任务中心 / 初始化数据**（§4.5）：种子**唯一真相源 = `config/initial_data/scheduled_jobs.json`**（**随包** + **正向断言**）；迁移 `y8z9a0b1c2d3` 播种段**已删**；config 的 `*_enabled`/`*_interval_hours` 仅**缺 key 兜底**。播种 = lifespan（**与 `scheduler_enabled` 解耦**）+ **注册即播种** + `sync_jobs_from_db()`，**只补缺失、不覆盖已有**。
- **⛔ 账号维度隔离（DEF-066，§4.1.1）**：**所有 `scheduled_jobs` 行必归属用户**（内置行按用户各持一份，判重 `user_id + job_key`；`user_id IS NULL` 全局行已废）；`account_ids` **建（POST）改（PUT）都过归属校验**；`scheduled_job_logs.user_id` = 执行时快照、三端点按它收口；`JobSpec.user_scoped` 任务由 runner 注入 `job.user_id`（NULL ⇒ 失败，不跑全库），空 `account_ids` = **本人全部账号**；`*_for_all_users` 包装已删。迁移 **`a0b1c2d3e4f5`（唯一 head，revises `k1l2m3n4o5p6`）**。

**数据与文档**
- **两个校验脚本都要跑**（`verify_doc_stats.py` + `verify_doc_counts.py`）；**禁只改合计**。
- **现状数字 ≠ 历史数字**：被推翻的断言 → 标「被 XX 取代」+ **原句保留**。**不许用快照预判「能不能取到」**（DEF-057）。
- **缺值口径**：计数缺值渲染 `0` + 原因行；**不得把 `None` 改成 0 落库**；非计数用 `-`。
- **同一语义不留两份实现**（DEF-044/047/058）；`notes.raw_json` 是 JSON 列（dict 非 str）⇒ 统一 `_parse_raw`。
- **「UI 看不到了」≠「列/数据已删」**：回写「整列删除」**必须由迁移文件本身证明**；**判据永远落在库上**（`job_group` 走过两段）。
- **账号下拉只展示账号名称**（五处全清）：类型、状态、店铺名、`(#id)` 都不进 label。守护 `test_account_dropdowns_show_only_the_account_name`（**新页面必须加进 `ACCOUNT_DROPDOWN_PAGES`**）。

**迁移纪律** → REF §F（6 条）。⚠️ 必背：**`Select` 没有 `.all()`**（DEF-065）⇒ `db.scalars(stmt).all()`。

**代码与呈现**
- **安全网不能条件性**（DEF-048/056）：可严、可整体失败，不能时开时关；最隐蔽形态是**前置条件式短路**。
- **失败三分法**（DEF-058）：有数据→渲染 / 没数据→**空态（非错误）** / 取不到（预期内 code）→一行安静说明 / 真异常→红块+真原因（`detailMessage()` 还原）。
- **轮询「摘除在跑项」禁止写进 `finally`（H9）**：`return` 也触发 ⇒ 写在 `try/catch` **之后**。**可容忍失败须显式 `{ _silent: true }`；长文案 `…` 收起 + Tooltip**（ADR-018）。
- **前端行为脚本 `.cjs` 必须接入 pytest 才算数**（`detail_message` / `shop_items_view` / `schedule_center` 已接线）—— 新脚本先接线，**变异也要接**。
- **动过 `backend/app/**` ⇒ 收尾重启 + 复核**（判据 = 新字段真出现在 `/openapi.json`）。**8000 归 launcher `run-app.py`**（只杀 uvicorn 子进程等自动重拉；**禁起第二个 launcher**）；8001 用 `.workbuddy/scripts/spawn-detached-api.py`。旧进程 + `extra="ignore"` ⇒ 新参数静默丢弃 ⇒ 请求模型加 `extra="forbid"`。
- **时区**一律 `shanghai_now()`；**`apis/` 禁直接改**（只经 `adapters/xhs/` 中转）。

**测试规范（强制执行）**
- **全量回归必带 `-n auto`**（`pytest-xdist` 已装）：`pytest tests/ -q -n auto --junitxml=...`；判定只看 xml 四数。
- **三层循环**：① 内循环 = 改哪个源文件跑对应那**一个**测试文件；② 中循环 = 动了公共层 ⇒ 受影响文件集 `-n auto`；③ 外循环 = 收尾才全量。**全量不许当内循环用**。
- **⛔ 前端行为脚本禁固定 `sleep(N>=100)`**，一律 `waitFor(条件, {timeout})`（盲等会被 pytest 调用次数放大）；判据用**内容**不用「元素在不在」。
- **新测试文件必须用 `tests/backend/conftest.py` 公共夹具**，**禁再抄** `create_engine`+`create_all`+`dependency_overrides` 三件套；既有 21 个文件在白名单 `LEGACY_ENGINE_FILES`。⚠️ `api_client` 退出**必摘自己加的 override**（模块级全局，泄漏不报错）。
- **规范要可执行**：`test_testing_spec_guards.py` 17 条守卫，**且守卫必须过变异验证**。⚠️ 负向断言禁裸 `in`（子串命中 ⇒ 恒真）：必须 `\b(?:function|const|let|var)\s+waitFor\b` 词边界。
- **⛔ 改文件禁用 `cat >> file <<'EOF'`**（**三次毁文件**：`test_scheduled_jobs.py` 头部 187 行被覆盖后按上下文重建、`01_测试体系与流程规范.md` §1–§5 被冲掉）。一律 Read+Edit / Write；被 `verify_doc_counts.py` 覆盖的文件重建时**数字逐字保留**。⚠️ 非 git 仓库 ⇒ 临时备份**放 `.workbuddy/tmp/` 并及时还原**；别在 `alembic/versions/` 留 `.bak`/改名文件。
