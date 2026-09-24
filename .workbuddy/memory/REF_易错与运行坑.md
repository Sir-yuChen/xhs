# 参考资料 · 易错速查 + 本机运行坑

> **本文件是 `.workbuddy/memory/MEMORY.md`（主记忆）拆出的参考资料**，主记忆只留索引指针。
> **不属于「核心规范」** —— 按需读，不整读、不进主记忆。
> 变更时同步更新主记忆 §0 索引表里的指针（别把内容搬回主记忆）。

## A. 易错速查

- **域名**：千帆 `ark.xiaohongshu.com`（**只指 ark**）· 蒲公英 `pugongying` · 乘风 `chengfeng` · 聚光 `ad` · creator `creator.xiaohongshu.com` · PC API `edith`。
- **千帆坑**：「全部」= **不传** `card_type`；价格单位**分**；创建时间参数**秒**、响应**毫秒**；按笔记查商品是**复数** `note_ids`；sourcemap 私有（403）。
- **不许读第三方响应里「不存在的字段」**（DEF-044）：千帆商品 `data` **只有** `['response','total','items','item_ids']`。⚠️ **`page_size` 效力两链路相反**：商品生效，**creator 笔记是死参数**。
- **`xsec_token` 不是平台登录令牌**：是网页版主页 URL 查询参数，登录 / 扫码 / 续期都不产生它；只有 `user_posted` 要它 ⇒ 卡片触发同步时拿不到（这也是 `www` 源被整体移除的原因，设计 13 §21.4）。
- **note_id 是 24 位 hex**：`_note_id_from_url` 只认全 hex 且 ≥16 位 ⇒ 假 id 让依赖它的逻辑**静默退化**。**凡「凭解析结果决定要不要做安全检查」的写法都是缺陷**。
- **会话 jar 三层判据**：`web_session` 游客也有 / **`id_token` 才说明登录过**（零请求）/ 能不能用只有发一次请求才知道（探针）。
- **SQLAlchemy `列 == None` → `IS NULL`**：以身份为闸门的查询会**静默变成「全平台空值行」** ⇒ **必须显式判空**。
- **测试账号统一「观隅好物」（red_id `293066917`）**：**只统一夹具**，代码不硬编码昵称。真实库 acct8（`sub_type=creator`，notes_count 1961），有 www+creator、**无自有 ark** ⇒ 千帆取票返回空串（**没有绑定表兜底这条路**）。
- **错误提示的判据是「句式」不是「平台名」**（v1.40 / 2026-09-24 用户口径「只需提示登录创作者平台或 pc 端平台」）：`WEB_SESSION_REQUIRED` 文案已从约 100 字收敛为一句登录指引，**文案里出现「创作者平台」是登录入口指引**（与 `_self_content_capabilities.notes_reason` 同口径），**不是**能力归因 —— 别当回归信号删掉（旧守卫 `assert "创作者平台" not in message` 已按此收敛）；要防的仍是 DEF-058 那条**被真机推翻的句式**（「不提供这三类数据」）。前端 `QUIET_REASONS` 里同一 `code` 的短文案**必须同口径**（一个 code 两处两种引导 ⇒ 用户对不上界面按钮名），且**「重新登录」四字不能丢** —— `test_quiet_reason_is_actionable_not_a_wait_and_see` 钉的就是它，它是这条文案**唯一可执行的动作**（2026-09-24 实测：改成「需登录…」当场变红）。

## B. 本机运行坑（只留最容易踩的）

- **真实运行库 `data/spider_xhs.db`**（`backend/app/storage/` 下是过期副本）；迁移前先 `cp` 备份。
- **回归只看 `--junitxml`、不看退出码**（收尾清 temp 被沙箱拦 → 假性变 1）；禁止 `--basetemp` 指向工作区；**默认只跑 1 轮**。
- **⚠️ 沙箱里 pytest 的 stdout 会偶发整个丢失**（2026-09-24 实踩）：同一条命令连跑两次，第一次**完全无输出**、第二次正常 —— 看着像「没跑」或「命令没生效」，极易误判。**对策：拿不到输出就原样重跑一次**，别据此判断测试状态。
- **改过任何后端源文件（哪怕只改一句文案）都必须重启并复核**：按「进程启动时间 vs 源文件 mtime」判新代码；杀 uvicorn 按端口（`netstat -ano | grep :8000` → PID → `Stop-Process`，监督进程 5 秒拉起）；改前端必须 `vite build`（8000 吃产物）。
- **判「服务吃到新代码没有」的最小动作 = 问一次 `GET /openapi.json`**（字段 / 路径在不在），**比 mtime 可靠**。⚠️ 2026-09-23 实踩：挂链笔记数筛选「未生效」，代码从头就是对的（新代码在真库上 `lt 1 → 276` 等逐项正确），错在**跑着的后端是旧进程** —— 它的模型里没有 `note_count`，而 **Pydantic 默认 `extra="ignore"` 把未知字段静默丢弃**（不报错、不写日志），界面表现与「代码写错」**完全一样**。通用闸门：请求模型加 **`extra="forbid"`**（未知字段 422）——「新旧不匹配」必须第一跳炸出声，不许看起来成功。
- **真机抓包走 CDP、必须开真实窗口**（用户明确要求，headless 实测失败）：Chrome + 隔离 profile + 9222，**启动→抓包→关闭写在同一个工具调用里**；**必须有空 profile 对照组**；**CDP 客户端必须有后台读循环**（只 send 不 recv 会永久死等）。**平台登录别用脚本自动化**（交给用户在人浏览器里扫码）。
- **中文文档一律 Write/Edit（UTF-8），不用 shell 追加**（混入非法字节后整文件被按 GBK 解读）；**bash heredoc 追加大段中文不可靠** ⇒ Write 临时文件 + `cat a b > final` 字节级拼接。
- **`create_all` 套用 `server_default` ⇒ 新契约在测试里等于没测**（DEF-049/051 夹具共谋）；**收紧/新增过滤后先跑单文件再跑全量**（DEF-051）；**同一文件多处编辑必须串行**（并行 Edit 会静默丢改动）。
- **SQLAlchemy JSON 列原地改 dict 不标记脏**：赋回**同一个对象** ⇒ **UPDATE 不发**，改动静默丢失 ⇒ 必须赋**新 dict**。
- **断言会「静默失效」**：① 负向 `not in` 找的词若被别处改动静默删掉就恒真 ⇒ 指向**两侧关系**；② 源码断言证明不了行为 ⇒ 用「**打包真实源码 + 驱动真实调用点**」（先例 `tests/frontend/*.cjs`，esbuild 异步 build、依赖桩写进产物 `node_modules/` + `external`）；③ **子串断言会被「超串」蒙过** ⇒ 用**带引号整 token** / `\b` 正则 / **导入路径**三重断言；**变异集必须同时含「超串型」与「真移除型」**（2026-09-22 实测 5 条里 2 条假绿）；**反向断言必须先剥注释**（`_code_only()`）。
- **「设定值 vs 反推值」不可省**（DEF-046 两侧列和都等于 46，`MATCH` 一直 True）；**校验脚本自己也会骗人**；**零覆盖 ≠ 没问题**。
- **⚠️ 沙箱里 curl 127.0.0.1 会被网络层缓存**（2026-09-23 实踩）：连抓 `/openapi.json` 两次，一次新一次旧——旧响应里竟有早已删掉的 `/api/xhs/qianfan/sync-goods`，差点误判「重启没生效」。**校验 live 服务用 `python urllib` 直取**（实测稳定拿到新代码）；`curl --noproxy '*'` 在沙箱里直接失败（无输出）。另：openapi.json 是**单行压缩**的，`grep -c` 永远得 1，别用它数出现次数。


## E. 商品台账（`/items`）实现坑（2026-09-23 §30~§33.10 汇总）

> 主记忆只留口径（「五件事」），这些是**怎么实现才不出错**的细节。

- **筛选走列、展示走 `raw_json`**：`WHERE` 只认 `shop_products` 的列（不要拿 JSON 现算），
  展示字段由 `normalize_item_from_raw()` + 列表页拼装唯一构造（§33.7）；缺字段由同步任务的
  **补足 pass** 从 `raw_json` 回填（迁移 `t3u4v5w6x7y8` 加了九列）。
- **平台响应不含 `card_type`** ⇒ 四个审核态**不从行推导**（选了就是空集 + 一行说明），**不猜**。
- **`note_count` 与 `platform_note_count` 两个数**：前者 = `note_goods` 关系表（本地实体关系，
  列表与筛选都用它），后者 = 平台原文 `product_note_num`（只作对照）；**关系表只覆盖「已同步 +
  本地生成」** ⇒ 本地数 ≤ 平台数是常态，**不要合成一个数**。
- **挂链笔记数筛选用相关子查询**，别改成 `LEFT JOIN + GROUP BY`：分页 `total` 那层已经包了
  `.subquery()`（`select(count()).select_from(stmt.order_by(None).subquery())`），换 JOIN 会连带改总数。
- **排序**：`_SORT_COLUMNS` 白名单后端不动（界面只放开「上架时间」是**呈现层**的收紧）；
  生效口径由后端回传 `sort_field`/`order`（前端不许自持一份「我以为的排序」）；`_ts_text()`
  格式**到秒**（只到分钟时，同批入库的行时间串一模一样 ⇒ 用户看不出列表在按什么排）。
- **切账号必须先把绑定态清空**（`setBinding(null)/setBindingLoaded(false)/setData(null)`）：
  否则新账号会拿着旧账号的 `binding.bound` 去打 `/items` ⇒ 未绑店 400，页面同时冒 toast +
  错误块 + 未绑定说明（用户原话「乱七八糟的异常」）。
- **批量删除只删本地台账行**：⚠️ 2026-09-23 起 `note_goods` 关系行**一并删**
  （用户口径「删除商品时也需要删除笔记与商品的关联关系」，**推翻** v1.36 的
  「关系行不级联删、商品写回时原样接上」—— 关系指向已删商品就是孤儿行，
  与 §26.6「删笔记留孤儿关系」同一个病）。实现顺序**不能反**：先取将删行的
  本地 `id` → 按 `product_id` 删关系 → 删商品行。**只删指向被删商品的关系**
  （幸存商品 / 别家店同名商品的关系必须留下）；`deleted` 回 `rowcount`
  （**不假定「请求几条就删几条」**）；上限前后端各一份且**必须同值**
  （守卫直接读两侧真值比对 —— 各自断言「等于 200」时两边一起漂移仍然绿）。
  ⚠️ **同步侧 `_drop_stale_rows` 刻意没改**（平台侧商品消失 ≠ 用户显式删除，
  见设计 13 §33.12 待办）。
- **弹窗「占屏幕 N%」** 见 §D（别配 `maxWidth`、`overflow: auto` 兜窄视口折行）。

## C. 数据库层与同步调度（2026-09-22）

- **SQLite 并发配置**（`core/database.py`）：`journal_mode=WAL`（旧值 `MEMORY` 有崩溃损坏风险，
  且读者持 SHARED 锁挡写者 —— 这正是「内容库详情 500（database is locked）」的根因）+
  `busy_timeout=5000` + `synchronous=NORMAL` + `cache_size=-32000` + `temp_store=MEMORY`。
  WAL 在 `init_db()`（启动期单线程）设，逐连接 PRAGMA 在 `connect` 事件里设。
  `alembic/env.py` 里那句 `journal_mode=MEMORY` 必须一并改，否则迁移后 WAL 被撤掉。
- **连接池保持 SQLAlchemy 对文件型 SQLite 的默认 `NullPool`**，**不引第三方连接池**：
  文件连接开销极低，且避免同一连接跨线程复用；本项目瓶颈是「持锁久」不是「建连接慢」。
- **WAL 备份**：不能再只 `cp spider_xhs.db`，必须连 `-wal` / `-shm` 一起复制，或用 `VACUUM INTO`。
- **同步定时任务已停用**（用户口径「先停止本地项目同步定时任务」）：`xhs_note_sync_enabled` /
  `xhs_shop_goods_sync_enabled` / `scheduler_enabled` 均默认 `False` ⇒ 同步 job 不注册、
  调度器不启动，同步完全由用户手动触发（弹窗选全量 / 时间区间）。测试
  `test_sync_jobs_are_not_registered_by_default` 断言的是**真实默认值**，不 monkeypatch 成 False。
- **⚠️ 跑 alembic 必须显式给 `DATABASE_URL`（2026-09-24 R23 实踩）**：
  `backend/alembic.ini` 里的默认 `sqlalchemy.url = sqlite:///./backend/app/storage/spider_xhs.db`
  是**废弃路径**，而活库是 `./data/spider_xhs.db`（`core/config.py::database_sqlite_path` 默认）。
  ⇒ 直接 `alembic -c backend/alembic.ini upgrade head` **会迁移错的库**（无声、rc=0、日志正常）。
  正解：`DATABASE_URL="sqlite:///./data/spider_xhs.db" python -m alembic -c backend/alembic.ini upgrade head`。
  ⚠️ **应用启动时跑的是它自己的 URL**（`database.py` 从 `settings.database_url` 传进 alembic cfg），
  所以 8000 重启会迁移活库 —— **CLI 与运行时是两套 URL，别混**。
- **`batch_alter_table` 重建表会**反射并原样重建**现有索引（2026-09-24 R23 实踩）**：
  删列时必须**先 `drop_index` 再 `drop_column`**，否则索引指向已删列 ⇒
  `sqlalchemy.exc.OperationalError: no such column: <col>`
  （报错 SQL 是 `CREATE INDEX ix_... ON <table> (<col>)`，容易误读成「迁移命名写错」）。
- **对齐迁移版本只能用 `command.upgrade(rev)`，绝不用 `downgrade`**：本项目的
  `y8z9a0b1c2d3.downgrade()` 会 `drop_table("scheduled_jobs")` —— 用 downgrade 把库对齐到
  某个 revision，会把表连同数据一起砸掉（探针脚本实踩）。
- **SQLAlchemy 2.0 的 `Select` 没有 `.all()`**（2026-09-24 DEF-065）：
  `select(X).where(...).order_by(...)` 返回的是**编译期 `Select`**，不是 1.x 的 `Query`。
  一律 `db.scalars(stmt).all()`（项目惯例，见 `api/accounts.py:205`）。
  写错**不在 import 期报错**，只在运行时 `AttributeError`。

## D. 前端排版坑（antd 6.3.7 实测，2026-09-23 §33.9）

- **`Card` 只在 `childNodes.length > 0` 时渲染 `.ant-card-body`**：`antd/es/card/Card.js` 是
  `loading || childNodes.length ? <div className={bodyClasses}>…</div> : null`，而 `toArray`
  （`@rc-component/util`）**会把 `null` / `false` 过滤掉**（实测 `toArray([null,false]).length === 0`）。
  ⇒ 想让卡片「没内容就整块不留白」，**children 必须是 `null`**；**包一层 `<Space>` 就白留
  上下各 24px**（空壳 `Space` 也是真元素，算一个子节点）。
- **`Typography.Link` 的 `ellipsis` 只收 `boolean`**（`Text` 才收 `{ tooltip }` 配置对象）
  ⇒ `ellipsis={{ tooltip }}` 直接 **TS2322**。要「链接 + 单行 + 悬停看全文」就用
  `<Tooltip title={…}><a style={{display:"block", maxWidth, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap"}}>`。
- **`Image` 外面那层 `.ant-image` 是行内元素**：想在图片上叠角标得给容器加 `position: relative`
  **并 `lineHeight: 0`**（否则行高在图片下留基线缝隙，角标 `top: 0` 跟着偏）；
  角标记得 `pointerEvents: "none"`，否则会吃掉「点开大图」的点击。
- **「长文案不换行」的守卫必须切片断言**：`Space direction="vertical" size={10} style={{ width: "100%" }}`
  这类容器在页面别处还有正当用法 ⇒ 全文 `not in` 会误伤，用 `_slice_between()`。
- **`@media (max-width: 900px)` 是 viewport 查询，不是容器查询**（2026-09-23 §33.10 实踩）：
  `global.css` 那个断点是按**整窗宽度**触发的，而 `.note-detail-body` 的 `flex-wrap` 按
  **弹窗宽度**折行 —— 弹窗一改小（`80vw → 75vw`），两栏 `flex-basis` 之和（300+320+20 = 640）
  就在「窗口 > 900px 但弹窗 < 640px」那段缝里折行，此时两栏各 100% 高、总高超过 body 的
  `height`，**`overflow: hidden` 会直接把右栏裁掉**。修法不是改共享断点（那是 80vw 弹窗与
  内容库弹窗的共用口径 ⇒ 边界扩散），而是把这一个 modal 的 body 改成 `overflow: auto`
  （装得下时子列自己滚、body 永不溢出 ⇒ 与 `hidden` 表现一致；装不下时正好接住）。
- **弹窗「占屏幕 N%」就别再设 `maxWidth`**：设了它宽屏上就不是 N%（会被截回固定值），
  口径与实际不符；「按视口占比」和「固定上限」是两种口径，混用等于谁都读不准。


- **⚠️ 沙箱 `HTTP_PROXY` 会让平台请求全超时（2026-09-23 实踩）**：沙箱 bash 注入了
  `HTTP_PROXY=http://127.0.0.1:57149`，而应用客户端是 `http_version="v2tls"`（HTTP/2）——
  本机代理不支持 HTTP/2 ⇒ `curl_cffi` 8 秒超时、表现是「ds 接口首次 fetch 失败」，
  极易误判成「会话过期」。常驻服务（launcher 在沙箱外启动）**直连**不受影响；
  `curl` 裸命令反而能通（它可能不走代理）。**在沙箱里跑一次性服务层探针（补验 / 探测）前，
  先 `HTTP_PROXY= HTTPS_PROXY= http_proxy= https_proxy= ALL_PROXY= all_proxy= <python> …`**。

## F. 迁移与 alembic 纪律（2026-09-24 实踩汇总，主记忆只留指针）

- **必须显式给 URL**：`DATABASE_URL="sqlite:///./data/spider_xhs.db"`
  `python -m alembic -c backend/alembic.ini upgrade head`
  —— `backend/alembic.ini` 里默认指向**废弃**的 `backend/app/storage/spider_xhs.db`，
  直跑会**无声迁错库**（rc=0、毫无提示）。运行时（8000 启动）走它自己那份 URL，不受影响。
- **`batch_alter_table` 会反射并重建已有索引** ⇒ 删列顺序必须是
  **先 `drop_index` 再 `drop_column`**（SQLite batch 模式本来就要重建整表）。
- **对齐/回退版本只用 `command.upgrade(rev)`**（`alembic upgrade <rev>`）；
  本项目某些 `downgrade()` 会 `drop_table` —— **别用 downgrade 调版本**（砸库）。
- **删列迁移要连带全仓静态守卫**：R23 的 `test_job_group_has_no_reference_in_backend_or_frontend`
  扫 `backend/app` + `frontend/src` 下 `.py/.ts/.tsx`（注释行豁免 + 单一白名单），
  否则某处又写回旧列名时只在运行期 INSERT 才炸（DEF-064 同族）。
- **改历史迁移内容的语义**（R24 删播种段的先例）：只影响**尚未执行到该 revision 的库**
  （= 新装库，正是目标）；已记进 `alembic_version` 的库**零影响**。改动必须在
  docstring/注释里写明**被谁取代**，**被推翻的常量原样保留**供审计。
- **迁移不读环境变量**（可重放、可审计）—— 一旦要在迁移里播种，这条就会逼出
  「写死值 vs 运行期读 config」的双路径漂移；**正解是把种子移出迁移**
  （随包分发的初始化数据，设计 15 §4.5）。
- **判「用例为什么慢」必须先量化单条，别先怀疑框架**（R25 实测，白花 10+ 次工具调用）：
  正确顺序 = ① `node <脚本>` 单独计时 → ② `pytest <单条用例>` 计时 → ③ 两者相差 30 倍以上
  说明**问题在脚本/被测代码内**，不在 pytest。
  本仓实测对照：干净 pytest 里跑 node 子进程恒 **0.27s**、连续 10 次**不劣化**；
  `TestClient(app)` 构造 / 首次请求 / 模块级 import 后端全部依赖 / `tests/backend` 路径 /
  捕获缓冲区 / 测试条数 —— **逐项探针全部否定**。
  真凶是 **`tests/frontend/add_account_modal.cjs` 里 38 次 `await sleep()`（12.2s）**；
  esbuild 打包只占 **0.7s**（2.5MB 产物）。
  ⚠️ **前端行为脚本里的 `await sleep(N)` 是纯等待、会被 pytest 调用次数成倍放大**
  （该脚本被调用 6 次 = 96s）。等 React 渲染请用**轮询直到条件成立**（`waitFor(cond, timeout)`），
  不要固定 sleep。
- **⛔ 改文件一律用编辑工具，禁用 `cat >> file <<'EOF'` 追加**（本项目**已两次出事**）：
  ① 曾把 `tests/backend/test_scheduled_jobs.py` 毁掉（末行 CRLF ⇒ heredoc 负载错位插进原文中间）；
  ② 本次把 `docs/04_测试文档/01_测试体系与流程规范.md` 的 **§1–§5 整段冲掉 + 编码打烂**
  （混合 CRLF/LF + GBK/UTF-8 字节），`verify_doc_counts.py` 直接崩在 `read_text`。
  症状很好认：**章节号乱序**（`## 12` 出现在 `## 6` 前面）、`grep '^## [1-5]'` 查不到、
  `python -c "path.read_text(encoding='utf-8')"` 抛 UnicodeDecodeError。
  修法：从上下文/历史读取重建整份文件（**用 Write 工具**，不用 shell），
  重建后必须复核三件事 —— 章节号连续、utf-8 可解码、CRLF 数符合该文件原风格。
  ⚠️ 若该文件被 `verify_doc_counts.py` 覆盖，重建时**数字必须与原值逐字一致**
  （本文件的 P14 表数 27 就是被逐处比对的），否则校验会红。
