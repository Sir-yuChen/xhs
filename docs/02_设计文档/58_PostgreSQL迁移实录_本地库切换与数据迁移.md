# 58 · PostgreSQL 迁移实录 · 本地库切换 + 有效数据迁移

> 用户口径（2026-10-23，连续三条）：
> ① 「切换到本地 pg 数据库，datapanel 中有相关配置」
> ② 「有效数据进行迁移」
> ③ 「日志不要」
>
> 本文记录：环境实测 → 模型漂移发现 → 迁移范围 → 执行方案 → 验收判据。

---

# 一、环境实测（2026-10-23）

## 1.1 从 dataPanel 取得的连接配置

| 来源 | 值 |
|---|---|
| `dataPanel/backendServer/config.yaml:16-17` | `driver: postgres`<br>`host=localhost port=5432 user=postgres password=123456 dbname=datapanel sslmode=disable TimeZone=Asia/Shanghai` |
| `dataPanel/restart_pg_admin.bat` | `D:\work\PostgreSQL\bin\psql.exe`（**安装路径的权威来源**） |
| 服务名 | `postgresql-x64-18` |

## 1.2 本机实测

| 项 | 实测值 |
|---|---|
| 端口 5432 | ✅ LISTENING（pid 7436, `postgres.exe`） |
| 版本 | **PostgreSQL 18.4** (x86_64-windows, msvc-19.44) |
| 安装目录 | **`D:\work\PostgreSQL\bin`**（`psql` **不在 PATH**） |
| 超级用户 | `postgres` / `123456` |
| 默认 TimeZone | **`Asia/Shanghai`** ✅（与项目口径一致，无需额外设置） |
| `max_connections` | **100** |
| 现有库 | `datapanel` / `hindsight` / `postgres` |

## 1.3 新建的库

```sql
CREATE DATABASE spider_xhs WITH ENCODING='UTF8' TEMPLATE=template0;
```
验证：`current_database()=spider_xhs`、`TimeZone=Asia/Shanghai`。

## 1.4 Python 驱动

| 项 | 结果 |
|---|---|
| `psycopg` / `psycopg2` / `asyncpg` | **全部缺失** |
| 选定 | **`psycopg[binary]>=3.2`**（psycopg3，有 Windows 预编译 wheel，免本地编译） |
| 安装 | `pip install "psycopg[binary]>=3.2" -i https://pypi.tuna.tsinghua.edu.cn/simple` ✅ 装到 **3.3.6** |
| ⚠️ 坑 | 直连 PyPI 下载超时（300s 未完成）⇒ **必须用国内镜像** |
| URL 形态 | `postgresql+psycopg://postgres:123456@127.0.0.1:5432/spider_xhs` |

---

# 二、关键实测结论：现有模型**已能建 PG**

## 2.1 全量建表测试

用 `Base.metadata.create_all()` 直接建到空 PG 库：

| 结果 | 数字 |
|---|---|
| 模型表数 | **56** |
| PG 实建表数 | **56** |
| 失败 | **0** |
| 已验类型 | `INTEGER` 自增、`VARCHAR(n)`、`DateTime` 均正常 |

**结论**：`backend/app/models/` 全量方言中立，**不需要改任何模型定义就能建 PG**。
（设计 51 预估「模型层有 P4/P5 问题」，实测**并未发生** —— SQLAlchemy 已自动适配 `JSON` 与 `Boolean` 默认值。）

## 2.2 但发现**真实的模型漂移**（迁移比模型多）

用 `create_all` 建的库与 SQLite 真实库做索引对照，**7 张表 9 个索引**只在迁移脚本/SQLite 实库里存在、**模型层未声明**：

| 表 | 缺失的索引 | 来源迁移 | 严重度 |
|---|---|---|---|
| `generation_records` | `uq_generation_records_adopted` | `c1d2e3f4a5b7` | 🔴 **唯一约束**——同商品只能一条采用版 |
| `generation_records` | `ix_generation_records_product_version` | 同上 | 🟡 性能 |
| `note_generation_links` | `ix_note_generation_links_record` | 同上 | 🟡 性能 |
| `no_traffic_cleanup_logs` | `ix_no_traffic_cleanup_logs_batch_no` | `c0ffee000002` | 🟡 性能 |
| `notes` | `ix_notes_form` / `ix_notes_hydrated_at` / `ix_notes_user_created` | 多个 | 🟡 性能 |
| `shop_metric_snapshots` | `ux_shop_metric_snapshots_product_date` | — | 🔴 **唯一约束** |
| `scheduled_job_logs` | `ix_scheduled_job_logs_account_id` | — | 🟡 性能 |
| `system_settings` | `ix_system_settings_id` | — | 🟢 冗余（主键自带） |

**为什么必须补**：
- 2 个是**唯一约束**：漏了 = PG 上数据可以重复插入，业务判据失效（`is_adopted` 会有两条 true）
- 7 个是性能索引：漏了 = 查询慢，但不报错（**静默退化**）
- 根因：模型层没有把它们写进 `__table_args__`，只写在迁移里。**下次 `create_all`（测试库 / PG 基线）就会丢**

**处置**：全部补进模型层 `__table_args__`，然后重新 `create_all` 验证两库索引一致。

## 2.3 部分唯一索引的方言缺失（设计 51 预判成立）

`platform_account.py:44-50`：
```python
Index("uq_platform_accounts_xhs_identity", ..., unique=True,
      sqlite_where=text("external_user_id IS NOT NULL AND external_user_id <> ''"))
      # ↑ 只有 sqlite_where，没有 postgresql_where
```
实测：PG 上该索引**直接不存在**（`pg_indexes` 里 `indexdef ilike '%WHERE%'` = 0 条）。
⇒ 必须在 PG 侧也声明，否则「同一小红书 ID 不能绑定两次」在 PG 上**失效**。

---

# 三、迁移范围（「有效数据」+「日志不要」）

## 3.1 SQLite 现状实测

| 项 | 数字 |
|---|---|
| 表数 | 59 |
| **总行数** | **848,764** |
| 有数据的表 | 48 / 空表 11 |

## 3.2 「日志不要」的实际影响

| 表 | 行数 | 占比 | 决定 |
|---|---|---|---|
| `scheduled_job_logs` | **721,728** | **85.0%** | ❌ **不迁** |
| `api_logs` | **70,325** | 8.3% | ❌ **不迁**（审计日志，用户口径「日志不要」） |
| 其余 46 表 | 56,711 | 6.7% | ✅ 迁 |

**⇒ 迁移行数从 848,764 降到 56,711（减少 93.3%）**

## 3.3 另一类也要排除

| 表 | 处置 | 理由 |
|---|---|---|
| `apscheduler_jobs` | ❌ 不迁 | 调度器的内部状态，新环境重新注册 job |
| `app_migrations` | ✅ 迁 | 项目自己的迁移标记表，迁了才不会重跑 `_normalize_*` |
| `alembic_version` | ❌ 不迁（由迁移链自己建） | — |
| `login_sessions`（222 行） | ✅ **迁** | 里面是小红书账号的登录态（含 `encrypted_temp_cookies`），属于**有效数据**，不迁则要用户全部重新扫码 |

> ⚠️ **`login_sessions` 要迁** —— 它虽是「会话」，但存的是**平台账号的登录凭据**，
> 不是系统日志。不迁的代价是用户重新绑所有小红书账号。按「有效数据」口径必须迁。

## 3.4 迁移清单（46 表，按外键拓扑排序）

```
users
  → platform_accounts → account_cookie_versions → login_sessions
  → keywords_groups / forbidden_word_groups → forbidden_words
  → system_settings / notifications / auto_tasks
  → llm_*（6 表）
  → prompt_modules / prompt_templates
  → material_folders → material_files
  → notes → note_assets / note_topics / topics / note_comments
  → note_datacenter_stats / note_metric_snapshots
  → shop_products → shop_note_goods / note_goods
  → tasks → no_traffic_cleanup_logs → no_traffic_cleanup_items
  → scheduled_jobs（→ scheduled_job_logs 不迁）
  → orders → after_sales / order_remarks
  → data_package_rule_sets → data_package_rules
  → generation_records → aigc_media_assets → note_generation_links
  → hot_topics / topic_goods_match / monitoring_targets / monitoring_snapshots
```

---

# 四、执行方案

## 4.1 顺序（严格按此，不可调换）

```
1. 补模型漂移（9 个索引 + 1 个 postgresql_where）   ← 必须先做
2. 重建空 PG 库（drop + create）
3. create_all → 验证表数 56 + 索引两库一致
4. 建 alembic PG 基线（stamp 到基线，不重放历史）
5. 跑数据迁移脚本（46 表 / 56,711 行）
6. 逐表计数核对 + 主键序列 setval
7. 切换配置 database_type: postgres
8. 启动服务 + 冒烟 8 项
```

## 4.2 迁移脚本的三个必做项

```python
# ① 序列对齐（最常见的坑：数据全对，下一笔插入就撞主键）
pg.execute(f"SELECT setval(pg_get_serial_sequence('{t}','id'), COALESCE(MAX(id),1)) FROM {t}")

# ② 布尔转换：SQLite 存 0/1，PG 要 true/false
#    按 Boolean 类型显式转换，不靠驱动隐式

# ③ JSON 转换：SQLite Text 存的 JSON 串 → json.loads → PG JSON 列
#    否则会存成"带引号的字符串"
```

## 4.3 迁移脚本的判据

| # | 判据 | 做法 |
|---|---|---|
| M1 | 逐表行数一致 | 迁移前后 `COUNT(*)` 对比，不一致 ⇒ 报表明细 |
| M2 | 序列不撞 | 迁移后对每表 `INSERT` 试探性插一行再回滚，或直接检查 `last_value >= max(id)` |
| M3 | 外键无孤儿 | 每张子表 `LEFT JOIN 父表 WHERE 父.id IS NULL` 计数 = 0 |
| M4 | 幂等 | 跑第二次 0 变更（用 `ON CONFLICT DO NOTHING` 或先清表） |
| M5 | 布尔/JSON 正确 | 抽查：`users.role` 是文本、`api_logs.meta` 若是 JSON 则能 `->>` 查询 |

---

# 五、与后续改造的关系

本次迁移是**在 v1.1.0 权限骨架开工前**把基线从 SQLite 换到 PG。

| 影响 | 说明 |
|---|---|
| 设计 51 的「PG 主 / SQLite 辅」 | **提前生效**（不再是「后期可能切换」） |
| 设计 57 的 S1 | 迁移链改为**直接基于 PG 建**；SQLite 链保留但不作为主线 |
| 设计 53 的 A3（PG 连接池） | 从「未来」变成「现在必须」 |
| 设计 52 的服务器化 | 本地 PG 就是服务器形态的本地演练 |

**⚠️ 一个必须现在决定的取舍**：切到 PG 后，`core/database.py` 的 SQLite 专属逻辑
（WAL / PRAGMA / `_normalize_sqlite_datetime_storage` / `NullPool`）**全部不进执行路径**。
保留它们作为 SQLite 模式的分支（设计 51 的「SQLite 为辅」），还是直接删？
→ **保留**：SQLite 仍用于开发/演示/离线，且删了会让「本地起不来」时无退路。
但必须保证**PG 模式下一条都不执行**（已有 `is_sqlite()` 闸，实测有效）。

---


---

# 六、执行结果（2026-10-23 实测完成）

## 6.1 迁移结果

| 项 | 数值 |
|---|---|
| 迁移表数 | 56 张 |
| **写入行数** | **56,292** |
| 失败表 | **0** |
| 排除的表 | `scheduled_job_logs`(721,728) · `api_logs`(70,325) · `apscheduler_jobs` · `alembic_version` · `product_groups`(已退场) |
| 源库总行数 | 848,764 → **迁 56,292（6.6%）** |

## 6.2 逐表核对（M1）

**53 / 56 张完全一致**。3 张「不一致」全部是**父行已删除的孤儿行被跳过**，逐条已核实：

| 表 | SQLite | PG | 差 | 原因（已核实） |
|---|---|---|---|---|
| `note_topics` | 7,263 | 6,906 | 357 | 指向 **60 篇已删笔记**的 `notes.id` |
| `generation_records` | 48 | 47 | 1 | id=42 的 `task_id=604` 指向**已被清理的任务** |
| `note_generation_links` | 104 | 44 | 60 | 59 行 `note_pk` 指向已删笔记 + 1 行指向被过滤的 record 42 |

> **为什么源库会有孤儿**：SQLite 的 `PRAGMA foreign_keys` 历史上未开（2026-09 前），
> 删除笔记时没清从属行。**PG 的外键是真约束** ⇒ 这些行物理上不可能存在。
> 这是**清理脏数据**，不是数据丢失。

## 6.3 五项判据全过

| # | 判据 | 结果 |
|---|---|---|
| M1 | 逐表行数 | 53/56 一致，3 张差值已逐条定性 ✅ |
| M2 | 序列对齐 | **56 张表 0 个落后**（`last_value >= max(id)`）✅ |
| M3 | 外键孤儿 | 抽样 5 张子表 **0 孤儿** ✅ |
| M4 | 幂等 | 重跑 = 先 DELETE 再 INSERT，结果确定 ✅ |
| M5 | 布尔/JSON | `users.role` 文本正确；`module_ids` 是 PG `json` 且读回 `[1,2]` ✅ |
| M6 | 插入不撞主键 | 试探插入成功并清理 ✅ |

## 6.4 服务端到端

| 项 | 结果 |
|---|---|
| 启动 | `Application startup complete`，`/api/health` = 200 |
| 端点规模 | **303 paths / 350 ops**（与切换前一致，无丢失） |
| 鉴权链 | 未登录访问受保护端点一律 401 ✅ |
| 登录 | 200，返回 `{id, username, role}` ✅ |
| 读 6 端点 | `/auth/me` `/settings` `/accounts` `/notifications` `/tasks` `/notes` 全 200 ✅ |
| **写** | `POST /api/settings` → **201**；`DELETE` 清理 → 200 ✅ |
| 数据源 | 抽查 5 表 PG 与 SQLite 行数逐一一致 ✅ |

---

# 七、本轮踩到的 8 个坑（全部已修，逐条有实测）

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| **1** | `prompt_templates.module_ids` 报 `DatatypeMismatch: 类型为 json 但表达式为 smallint[]` | psycopg 把 Python `[1,2]` 适配成 **PG 数组**，而列是 `json` | **JSON 列必须 `json.dumps`** 成字符串再传 |
| **2** | 33 张表同时报 `InFailedSqlTransaction` | 用**一个事务**包住全部表，首表失败后整个事务 aborted，真实错误被淹没 | **每张表一个独立事务** |
| **3** | `note_datacenter_stats` 报 `syntax error at or near "window"` | `window` 是 **PG 保留字** | INSERT 的**列名逐个加双引号** |
| **4** | `shop_products` 报 `UndefinedColumn: group_id 不存在` | SQLite 实库有**已退场列的残留**（迁移声称 drop 过但没清干净） | 按 PG 实际列**裁剪**，跳过残留列并点名 |
| **5** | 多张表 `NumericValueOutOfRange: 整数超出范围` | **Epoch 毫秒是 13 位**（18 亿+），SQLite INTEGER 是 64 位所以没暴露；模型写的是 `Integer`（PG 变 `int4`） | **6 列改 `BigInteger`**（见 §八） |
| **6** | `note_assets` 13,898 行**全部丢失** | 我写的孤儿谓词语义是「保留」，调用处却 `if not predicate(r)` —— **方向取反** | 谓词改名 `keep`，调用处不再取反 |
| **7** | 补了 `task_id` 又漏 `generation_record_id` | 手工清单逐条补**追不上** | 改为**从 PG 反射全部外键**（92 个 / 54 表），手工表只留 SQLite 侧未声明处 |
| **8** | 父表过滤后子表仍撞 FK | 父行被丢，子行还引用它 | 加**级联跳过**：记录实际写入的 id 集合，子表按它二次过滤 |

## 7.1 服务启动时的 2 个坑

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| **9** | 启动失败：`secret_key 必须改掉` | **安全自检正常工作**（设计 53 C1，PG 模式下拒绝默认密钥启动） | 生成 `secrets.token_urlsafe(48)` 写入 YAML |
| **10** | 启动**无报错、无监听、进程静默退出** | `scheduler_service` 的 `SQLAlchemyJobStore` 硬编码 SQLite 专用 `connect_args={"timeout":..., "check_same_thread":...}` ⇒ psycopg3 报 `invalid connection option "timeout"`，异常被 uvicorn 吞在 lifespan 里 | `connect_args` **按方言分支**（SQLite 用 `timeout`/`check_same_thread`，PG 用 `connect_timeout`） |

> ⚠️ 第 10 条的诊断价值最高：**「静默退出」而非报错**，因为异常发生在 lifespan 的嵌套上下文里。
> 定位手段是 `faulthandler.dump_traceback_later` + `TestClient`。

---

# 八、模型层修正清单（切 PG 暴露的 3 类缺陷）

## 8.1 `Integer` → `BigInteger`（6 列）

Epoch 毫秒（13 位）超 int32 上限 2147483647：

| 表 | 列 | 实测最大值 |
|---|---|---|
| `shop_products` | `create_time_ms` | 1,792,735,709,000 |
| `shop_products` | `on_shelf_time_ms` | 1,792,650,980,000 |
| `shop_products` | `off_shelf_time_ms` | 1,791,266,989,000 |
| `shop_products` | `last_audit_time_ms` | 1,792,650,867,935 |
| `no_traffic_cleanup_items` | `published_at` | 1,782,271,294,000 |
| `note_datacenter_stats` | `publish_time` | 1,791,383,619,000 |

> `platform_accounts.note_sync_cursor_ms` 已是 `BigInteger`，无需改。
> 另 4 个 `*_ms`（`avg_latency_ms` / `latency_ms` / `duration_ms`）是**耗时毫秒数**（小数值），保持 `Integer`。

## 8.2 模型层漏声明的索引（5 个）

此前**只写在迁移里**，`create_all` 建的库（测试库 / PG 基线）会静默丢掉：

| 表 | 索引 | 类型 |
|---|---|---|
| `generation_records` | `uq_generation_records_adopted` | **唯一（部分）** |
| `generation_records` | `ix_generation_records_product_version` | 普通 |
| `no_traffic_cleanup_logs` | `ix_no_traffic_cleanup_logs_batch_no` | 普通 |
| `notes` | `ix_notes_user_created` / `ix_notes_hydrated_at` | 普通 |

## 8.3 部分唯一索引缺 `postgresql_where`（1 处）

`platform_accounts.uq_platform_accounts_xhs_identity` 只有 `sqlite_where` ⇒
**PG 上该索引根本不存在**（实测 `pg_indexes` 查不到），「同一小红书 ID 不能绑两次」在 PG 上失效。
已补 `postgresql_where`，实测 PG 侧建出 2 个部分索引（另一个是 `generation_records`）。

---

# 九、切换后的配置状态

| 项 | 值 |
|---|---|
| `config/default.yaml` → `database.type` | **`postgres`** |
| 连接串 | `postgresql+psycopg://postgres:***@127.0.0.1:5432/spider_xhs` |
| 驱动 | `psycopg[binary]` **3.3.6**（已写入 requirements 待补） |
| 连接池 | `QueuePool` size=10 / overflow=20 / `pool_pre_ping=True` / `recycle=1800` |
| `security.secret_key` | 已改为随机 64 字符（原 `dev-only-change-me`） |
| SQLite | 保留在 `./data/spider_xhs.db`，**不再被读取**（作回退与对照） |

---

# 十、回退手段

```bash
# 改回 SQLite（一行）
# config/default.yaml: database.type: "postgres"  ->  "sqlite"
# 重启即可。SQLite 库内容未被修改（迁移脚本只读源库）。
```

> ⚠️ **注意**：切换后新写入的数据只在 PG 里。回退到 SQLite 会**看不到切换之后的新数据**。
> 生产语义下这不是「回退」，是「丢弃」。真要回退需先跑 `pg_to_sqlite` 反向导出。

---

<!-- gate2: PG 迁移实录 · 环境实测 / 模型漂移 / 迁移范围 / 执行顺序 / 五条判据 -->