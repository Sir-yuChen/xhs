# 51 · 数据库架构 · PostgreSQL 为主，SQLite 为辅

> ⚠️⚠️ **本文已被取代（2026-10-24，用户口径「完全放弃 SQLite，全部迁移到 PG」）。**
> 双方言兼容方案整体退场：现在**只有 PostgreSQL 一种方言**，`core/database.py`
> 对非 PG URL 直接拒绝启动（无任何逃逸口）；`database_type` / `database_sqlite_path` /
> `database_mysql_*` 配置字段与 `config/default.yaml` 对应键一并删除。
> 落地过程与实测证据见 `59_v1.1.0实施台账.md` §十；下方原文按项目纪律保留，不再更新。

> 上游：`50_技术方案_权限骨架实现`
> 用户口径（2026-10-23）：「后期可能切换成 pg 数据库，所有你要兼容无缝衔接到 pg 数据库」
> **口径修订（同日）**：「应该是以 pg 数据库为主，sqlite 为辅」
> ~~（以上口径已被 2026-10-24「完全放弃 SQLite」取代，见顶部声明）~~

---

# 零、定位（先定这个，其余都是推论）

| | **PostgreSQL（主）** | **SQLite（辅）** |
|---|---|---|
| 定位 | **产品正式形态** | **本地 / 离线 / 单机试用** |
| 谁用 | 部署服务器、多用户、多实例 | 安装包首次启动、断网、便携模式 |
| 迁移链 | **主线**：模型改动 → 立刻出 PG 迁移 | 从属：**由 PG 状态降级生成**，不手写 |
| 新功能 | **必须**在 PG 上验证 | 保证「能在本机跑起来」即可 |
| 测试 | 全量回归的**主跑环境** | 冒烟 + 关键路径 |
| 真相源 | `backend/app/models/`（两边共用） | 同左 |

**由此直接推出的三条铁律**

1. **PG 能跑不算完，SQLite 也要能跑** —— 但反过来：**SQLite 能跑不算完，PG 必须能跑**。
2. **新代码一律只用方言中立写法**（§2.2 表格），因为两库都要跑。
3. **历史 95 个迁移不重写**（它们是 SQLite 形态的真实历史）。改为：
   **PG 用一次性基线建成，SQLite 从基线降级导出**（§2.1）。

---

# 一、现状实测（2026-10-23）

## 1.1 已经做对的部分（不用动）

| 项 | 证据 | 结论 |
|---|---|---|
| 连接层方言隔离 | `core/database.py:44,57,103,184` 全部 `if not settings.database_url.startswith("sqlite"): return` | ✅ PRAGMA / WAL 只在 SQLite 上跑 |
| WAL 与 FK 分两段注册 | `_enable_wal` 只在 `init_db()` 调；FK 注册在 **Engine 类**上并判 `dialect.name != "sqlite"` | ✅ PG 上不会误跑 PRAGMA |
| URL 构造器 | `core/config.py:283-306` 已支持 `database_type == "mysql"` 分支 | ⚠️ 有 mysql 无 postgres，需补 |
| 模型层 | 全用 SQLAlchemy 2.0 `Mapped` + `mapped_column`，无原生 SQL | ✅ 无方言绑定 |

## 1.2 真正的障碍（实测计数）

| # | 障碍 | 实测规模 | 严重度 |
|---|---|---|---|
| **P1** | `batch_alter_table`（SQLite 的「建新表→拷数据→换名」变通） | **52 / 95 迁移** | 🔴 高 —— PG 上不需要它，但写法本身 PG 也能跑（SQLAlchemy 会降级为原生 ALTER），**只是慢且会重建表** |
| **P2** | `PRAGMA table_info(...)` 判列存在 | **37 文件真的在 `conn.execute(sa.text(...))` 执行**（逐文件清单见 §2.1） | 🔴 高 —— **PG 第一次就报** `syntax error at or near "PRAGMA"`，链断在第一个 |
| **P3** | `op.execute("ALTER TABLE ... DROP COLUMN ...")` 裸 SQL | `f4a1c8e2d935:197`、`h1i2j3k4l5m6:75,106` | 🟡 中 —— PG 语法兼容，但缺 `IF EXISTS` 时行为不同 |
| **P4** | `sa.JSON()` 无 variant | 7 处（`meta` / `keywords` / `config` / `payload` / `raw_json` / `params`） | 🟡 中 —— SQLite 存 TEXT，PG 建 `json` 类型；**SQLAlchemy 会自动适配**，但 `json_extract` 那类查询不通用 |
| **P5** | `server_default="0"`（布尔）/ `CURRENT_TIMESTAMP` | 8 处 `""`、2 处 `"0"` | 🟡 中 —— PG 上 `Boolean` 列默认 `'0'` **会报错**（需 `false`） |
| **P6** | `datetime(col, '+8 hours')` 裸 SQL（时区归一） | `database.py:216,232` | 🔴 高 —— **PG 无此函数**（用 `col + interval '8 hours'`） |
| **P7** | 无 PG URL 分支 | `config.py` 只有 mysql / sqlite | 🟡 中 |
| **P8** | 迁移链顺序依赖（95 个迁移重放到空 PG 库） | 全链 | 🔴 高 —— 任何一个 P2 都会让**链断在中间** |

---

# 二、兼容策略（PG 为主之后重定）

## 2.1 为什么不能「把 95 个迁移改一遍」

实测：**95 个迁移里 37 个真的在跑 `PRAGMA table_info(...)`**（不是注释，是 `conn.execute(sa.text(...))`）：

```
a7b8c9d0e1f2 / b2c3d4e5f6a7(×2) / b2c3d4e5f6g7 / b3e4f5a6c7d8 / b8c9d0e1f2a3 / c1d2e3f4a5b6
c4d5e6f7a8b9 / d2e3f4a5b6c7 / d4e5f6a7b8c9 / d5e6f7a8b9c0 / e3f4a5b6c7d8 / e5f6a7b8c9d0
f2a3b4c5d6e7 / f4a5b6c7d8e9 / f5a6b7c8d9e0 / f6a7b8c9d0e1 / f7a8b9c0d1e2 / f8a9b0c1d2e3
h1i2j3k4l5m6 / h9i0j1k2l3m4 / k4m5n6o7p8q9 / m4n5o6p7q8r9 / n5o6p7q8r9s0 / n6o7p8q9r0s1
o7p8q9r0s1t2 / p8q9r0s1t2u3 / q9r0s1t2u3v4 / s1t2u3v4w5x6 / t3u4v5w6x7y8 / u4v5w6x7y8z9(+PRAGMA index_list)
```

**PG 上第一次执行就 `syntax error at or near "PRAGMA"` ⇒ 链断在第一个**。
逐条改 37 个文件 × 每个都在真实库跑过 = 高风险且不可验证（改错会让**已部署的 SQLite 库**升不上去）。

## 2.2 选定方案：**PG 主线 + SQLite 派生**

```
        backend/app/models/          ← 唯一真相源（两边共用）
                 │
        ┌────────┴─────────┐
        │                  │
   PG 迁移链（主线）    SQLite 迁移链（辅）
   backend/alembic_pg/  backend/alembic/
        │                  │
   pg_baseline_0001     历史 95 个（不动）
        │                  │
   + 后续每个新功能        + 新功能同步出一条
        │                  │
   ✅ 部署环境跑这个      ✅ 安装包/离线跑这个
```

**两条链的关系**
- **PG 是主线**：模型改了，**先出 PG 迁移**，用 `alembic_pg revision --autogenerate` 生成。
- **SQLite 是从属**：同一次改动，再补一条 SQLite 迁移。
- 两条链**不互相引用**，各自有 `alembic_version`。
- 一致性由守卫保证：**两条链的 head 必须指向相同的模型状态**
  （判据：对 PG 跑 `--autogenerate` 得空迁移 + 对 SQLite 跑 `--autogenerate` 得空迁移，两个都空才算一致）。

## 2.3 为什么反过来（SQLite 当基线）不行

| 若 SQLite 当基线 | 后果 |
|---|---|
| PG 基线从 SQLite 导出 | 导出时要处理 `PRAGMA`/`json_extract` 这些 SQLite 专有物 ⇒ **又回到逐条改** |
| 新功能先写 SQLite 迁移 | 主线是辅库，**部署环境的表结构永远是二等公民** —— 与「PG 为主」直接违背 |
| PG 上验证 | 只能在事后补，发现问题时 SQLite 迁移已经合入 |

## 2.4 新代码的写法约束（S0 起强制）

| ❌ 禁用 | ✅ 改用 | 为什么 |
|---|---|---|
| `conn.execute(text("PRAGMA table_info(t)"))` | `sa.inspect(conn).get_columns("t")` | **两方言通用**，这是现有 37 处问题的根因 |
| `op.execute("ALTER TABLE t DROP COLUMN c")` | `op.drop_column("t", "c")` | SQLite 自动走 batch，PG 走原生 ALTER |
| `json_extract(col,'$.k')` | Python 侧解析（读出来在内存里取） | SQLite 无 `jsonb` 操作符体系 |
| `datetime(col,'+8 hours')` | 时区在 Python 侧处理（已有 `shanghai_now()`） | PG 无此函数 |
| `syntax sqlite_where=...` 只写一边 | `Index(..., sqlite_where=..., postgresql_where=...)` **两个都写** | 部分唯一索引两边都要 |
| `server_default="0"` 给 Boolean | `server_default=sa.text("false")` | PG 上 `Boolean DEFAULT '0'` 报错 |
| ~~`Column.ilike()`~~ | **保留 —— 实测两库都可用** | SQLAlchemy 自动按方言降级：SQLite ⇒ `lower(x) LIKE lower(?)`，PG ⇒ 原生 `ILIKE`。**不需要改**（见下方实测） |
| `INSERT OR IGNORE` | `insert().on_conflict_do_nothing()` | 两个方言都支持 |

> ✅ **实测（2026-10-23）**：现有 5 处 `ilike`（`api/notes.py:703-706` + `services/no_traffic_service.py:425`）**不需要改**。
> SQLAlchemy 的 `.ilike()` 是**方言感知**的，同一个语句在两个方言编译结果不同：
> ```
> SQLite:  WHERE lower(t.title) LIKE lower(?)     ← 自动降级
> PG:      WHERE t.title ILIKE %(title_1)s        ← 原生
> ```
> 且在 SQLite 上实跑通过。**教训**：判断跨方言兼容性要看 SQLAlchemy 编译器实际产出，不能凭 SQL 关键字印象。

# 三、对本次 v1.1.0 的直接影响（逐段）

## 3.1 数据模型（S1）—— 必须一次做对

```python
# menus 表的两条唯一索引：两个方言都要写
__table_args__ = (
    Index("ux_menus_perms", "perms", unique=True,
          sqlite_where=text("perms IS NOT NULL AND perms <> ''"),
          postgresql_where=text("perms IS NOT NULL AND perms <> ''")),   # ← 新增
    Index("ux_menus_path", "path", unique=True,
          sqlite_where=text("path IS NOT NULL AND path <> ''"),
          postgresql_where=text("path IS NOT NULL AND path <> ''")),     # ← 新增
)
```

```python
# users.status 等字符串枚举：PG 上用 CHECK 约束，SQLite 上 CheckConstraint 也支持
__table_args__ = (
    CheckConstraint("status IN ('normal','pending','disabled')", name="ck_users_status"),
)
```
> ⚠️ 不用数据库原生 `ENUM` 类型 —— PG 建 ENUM 要 `CREATE TYPE`，迁移回退麻烦；SQLite 根本没有。

## 3.2 迁移（S1）—— 3 个新迁移写成方言中立

| 迁移 | 里的危险写法 | 改法 |
|---|---|---|
| `a1b2c3d4e5f9`（建 3 表） | 无（纯 `create_table`） | ✅ 天然兼容 |
| `b2c3d4e5f6a0`（建 2 表 + `api_logs.status_code` 回填） | 回填用 `json_extract(meta,'$.status_code')` | **改为 Python 侧循环**：`for row in conn.execute(select(ApiLog).where(status_code is null)): row.status_code = (row.meta or {}).get("status_code")` |
| `c3d4e5f6a1b2`（灌种子） | 无 | ✅ |

**`b2c3…` 的 70,325 行回填要不要担心慢？**
- SQLite 单条 UPDATE 快，但 `json_extract` 不通 PG
- Python 循环 70k 行 ≈ 8–15s（一次性的，可接受）
- **正确做法**：按块 5000 行 `bulk_update_mappings` ⇒ 两个方言都走同一条代码路径

## 3.3 服务层（S2）—— PG 的事务语义差别

| 差异 | SQLite | PostgreSQL | 影响 |
|---|---|---|---|
| 并发写 | WAL 下全库一把写锁 | 行级锁，`serializable` 可选 | 现有「账号级互斥」逻辑在 PG 上**仍然正确但多余**；不要删，保持两库同语义 |
| DDL 事务性 | **DDL 不在事务里**（部分回滚） | DDL 可回滚 | 迁移失败时的行为不同 ⇒ 迁移必须写成幂等（`IF NOT EXISTS` 语义用 `inspect()` 判断） |
| 唯一约束冲突 | `IntegrityError` | `IntegrityError`（错误码不同） | 现有代码只 catch `IntegrityError` ⇒ ✅ 兼容；**不要**读 `e.orig.pgcode` |
| 空字符串 vs NULL | `''` 与 `NULL` 是两回事（都允许） | 同 | 现有代码已按此处理（`perms <> ''` 判断）⇒ ✅ |

**关键判据**：服务层**只允许**用 `IntegrityError` 判定唯一冲突，不许解析数据库错误码。

## 3.4 权限缓存（S2）—— 已经是进程内，天然兼容

`permission_cache.py` 用进程内 dict，不碰数据库 ⇒ ✅ 零成本。
> 副作用：多实例部署时缓存不共享。**现在不做**，因为本项目是单机桌面版；
> 若将来上多实例 + PG，缓存要迁到 PG 表或 Redis —— 这是**独立的一块**，不在本次范围。

## 3.5 API 层（S3）—— 分页与排序

| 写法 | 兼容性 |
|---|---|
| `offset(page-1)*size).limit(size)` | ✅ 两方言通用 |
| `ORDER BY id DESC` | ✅ |
| `ILIKE`（大小写不敏感模糊） | ❌ **PG 有 `ILIKE`，SQLite 没有**（SQLite 的 `LIKE` 默认就大小写不敏感） |

**统一做法**：不要在 SQL 里写 `ILIKE`，用 SQLAlchemy 的跨方言写法：
```python
from sqlalchemy import func
# 两个方言都能跑：lower(column) LIKE lower(pattern)
cond = func.lower(User.username).like(f"%{kw.lower()}%")
```
守卫：`test_no_ilike_in_queries.py` —— 出现 `ilike(` 且未包 `func.lower` ⇒ 红。

---

# 四、两条切换路径（都不是"那一天"，是常态）

## 4.1 路径 A：本地 SQLite → PostgreSQL（把试用的数据搬到正式库）

用户在某台机器上先用 SQLite 跑了一段时间，要迁到 PG 正式库。

| # | 步骤 | 判据 |
|---|---|---|
| 1 | 建 PG 库（`UTF8` + `TimeZone=Asia/Shanghai`） | `SELECT current_setting('TimeZone')` = `Asia/Shanghai` |
| 2 | 改配置 `database_type: postgres` | `settings.database_url` 以 `postgresql+psycopg://` 开头 |
| 3 | `alembic -c backend/alembic_pg.ini upgrade head` | 表数 = 67 |
| 4 | `.hermes/scripts/sqlite_to_pg.py` | 逐表计数一致；**主键序列 `setval` 到位** |
| 5 | 冒烟 8 项 | 登录 / 用户 / 角色授权 / 菜单树 / 字典 / 配置 / 日志 / 业务页 |

**数据迁移脚本的三个坑（现在就要写进脚本，不是事后补）**
```python
TABLES_BY_FK_ORDER = [...]        # 按外键拓扑排序，不手排
for t in TABLES_BY_FK_ORDER:
    rows = sqlite_conn.execute(f"SELECT * FROM {t}").fetchall()
    pg_conn.execute(insert(pg_table).values(rows))
    # ① 自增序列必须显式对齐，否则下一笔 insert 立刻 duplicate key
    pg_conn.execute(f"SELECT setval(pg_get_serial_sequence('{t}','id'), COALESCE(MAX(id),1)) FROM {t}")
```
1. **不 setval ⇒ 数据全对但下一次插入撞主键**（最常见的坑）
2. **布尔列**：SQLite 存 `0/1`，PG 要 `true/false` ⇒ 按 `Boolean` 类型显式转换，不靠驱动的隐式转换
3. **JSON 列**：SQLite 的 `Text` 存的 JSON 串要 `json.loads` 后再写入 PG 的 `JSON` 列（否则存成带引号的字符串）

## 4.2 路径 B：PG → SQLite（导出便携副本 / 离线包）

正式库在 PG 上跑着，某台机器要本地离线用（比如装机演示、单机试用）。

| # | 步骤 | 说明 |
|---|---|---|
| 1 | `alembic -c backend/alembic.ini upgrade head`（空库） | SQLite 链仍然是完整的 |
| 2 | `pg_to_sqlite.py` 按同样顺序导 | 反向脚本，**与 A 共用同一份拓扑排序** |
| 3 | 便携包随带这个 sqlite 文件 | 安装包首次启动直接用 |

> **为什么两条路径都要有**：定位既然是「PG 主，SQLite 辅」，**双向都得通**。
> 单向只留在纸面上 —— 真要离线时才发现导不回去。

# 五、本次要新增的文件

| 文件 | 作用 | 何时做 |
|---|---|---|
| `backend/alembic_pg.ini` | PG 链的 alembic 配置 | S1（与首个 PG 基线同批） |
| `backend/alembic_pg/env.py` | PG 专用 env（`compare_type=True`） | S1 |
| `backend/alembic_pg/versions/pg_baseline_0001.py` | PG 基线（autogenerate + 手工补索引） | S1 |
| `backend/app/core/db_dialect.py` | `is_sqlite()` / `is_postgres()` / `for_dialect(sq=..., pg=...)` 辅助 | S0 |
| `tests/backend/test_migrations_have_no_dialect_sql.py` | 新迁移禁方言 SQL 守卫 | S1 |
| `.hermes/scripts/sqlite_to_pg.py` | SQLite → PG 数据迁移（含 setval / 布尔 / JSON 三坑） | S1 同批 |
| `.hermes/scripts/pg_to_sqlite.py` | PG → SQLite 反向导出（共用同一份 FK 拓扑排序） | S1 同批 |
| `tests/backend/test_two_chains_in_sync.py` | 两条迁移链 head 指向同一模型状态 | S1 |
| `tests/backend/test_dialect_compat.py` | 同一批模型断言在两个方言都能建表 | S1 |

## 5.1 `core/db_dialect.py`

```python
"""方言判定唯一实现。禁止任何模块自己写 `"sqlite" in settings.database_url`。"""

def current_dialect() -> str:      # "sqlite" | "postgresql"
def is_sqlite() -> bool
def is_postgres() -> bool

def bool_server_default() -> str:
    """Boolean 列默认值：SQLite 认 '0'、PG 必须 'false' ⇒ 统一返回 sa.text(...)"""
```

> 现状是 **4 处** 自己写了 `startswith("sqlite")`（`database.py:44,57,103,184`）——
> 本次收敛到 `db_dialect.py` 单一实现，`database.py` 改调它。**不是必须**，但能防「PG 上了之后某处还在判 sqlite」。

---

# 六、验收判据（PG 为主之后重排）

| # | 判据 | 命令 | 频次 |
|---|---|---|---|
| **D1** | **PG 能从零建库** | `alembic -c backend/alembic_pg.ini upgrade head` ⇒ 表数 67 | 每次改模型 |
| **D2** | PG 与模型零漂移 | `alembic_pg revision --autogenerate` ⇒ **空迁移** | 每次改模型 |
| **D3** | SQLite 也能从零建库 | `alembic -c backend/alembic.ini upgrade head` ⇒ 表数 67 | 每次改模型 |
| **D4** | 两条链 head 模型状态一致 | `test_two_chains_in_sync.py` | CI |
| **D5** | **全量回归主跑在 PG** | `DATABASE_TYPE=postgres pytest tests/backend -q` | 收尾 |
| **D6** | SQLite 冒烟 | `DATABASE_TYPE=sqlite pytest tests/backend/test_api.py tests/backend/test_rbac_*.py -q` | 收尾 |
| **D7** | 新迁移禁方言 SQL | `test_migrations_have_no_dialect_sql.py` | 每新增迁移 |
| **D8** | 双向数据迁移幂等 | `sqlite_to_pg.py` 与 `pg_to_sqlite.py` 各跑两次，第二次 0 变更 | 切换时 |

> **D1–D4 需要本机有 PG**。本机若无 PG：`docker run --rm -e POSTGRES_PASSWORD=x -p 55432:5432 postgres:16-alpine`。
> **这是日常开发的一部分**（因为 PG 是主），不再是「一次性验证」。

# 七、明确不做的（避免过度设计）

| 项 | 为什么不做 |
|---|---|
| 现在就废弃 SQLite | 它是**辅助形态**（安装包首次启动 / 离线 / 便携），不是过渡品 —— 保留并保证可用 |
| 把 95 个历史迁移改写成方言中立 | 37 处真实 `PRAGMA` 全改 = 高风险且会让**已部署 SQLite 库**升级路径变化；PG 主线用基线已解决 |
| 引入 ORM 抽象层 / 存储库模式（Repository Pattern） | 现有代码直接用 Session，**再包一层是纯增负担**，且不解决任何方言问题 |
| 支持 MySQL | 已不在规划中（`config.py` 里有残留分支，保持不动即可） |
| 多实例共享权限缓存 | 独立议题，与方言无关 |

---

# 八、对 S0–S9 的增补（并入 50 号文档）

| 段 | 原内容 | **增补（PG 为主）** |
|---|---|---|
| S0 | `core/perms.py` + registry | **+ `core/db_dialect.py`**（方言判定收敛到唯一实现，现有 4 处散落调用改调它） |
| S1 | 5 模型 + 3 迁移 | **+ PG 基线 `alembic_pg`**（**先出 PG 迁移，再出 SQLite 迁移**）；索引双写 `sqlite_where` + `postgresql_where`；Boolean 默认 `sa.text("false")`；`users.status` 用 `CheckConstraint` 不用原生 ENUM；**+ 两个数据迁移脚本骨架** |
| S2 | rbac / 缓存 / route_guard | 唯一冲突只 catch `IntegrityError`，**不解析数据库错误码**（PG 的 `pgcode` 与 SQLite 的 `sqlite_errorname` 不同） |
| S3 | 6 个 API | 模糊查询可继续用 `.ilike()`（实测跨方言安全）；**唯一要求**：不要写原生 SQL 的 `ILIKE` 字符串 |
| S9 | 数字口径 | **+ D1–D8 八条判据**；**全量回归改在 PG 上跑**，SQLite 跑冒烟子集 |

**工期影响**：+2 人日（PG 基线 + 双链漂移守卫 + 双向迁移脚本 + 5 处 ilike 改造 + PG 环境搭建）。

---

# 九、给用户的一句话结论

**现在不用切换数据库**（SQLite 继续跑），但**从 S0 起所有新代码按「两库都能跑」的写法落地**，
并在本次 v1.1.0 一并交付：PG 主线迁移链 + SQLite 从属链 + 双向数据迁移脚本 + 9 条方言判据。
**这样「切 PG」就变成改一个配置项 + 跑一次迁移脚本，不是一次改造工程。**

---

<!-- gate2: 方言兼容 · PG 主线 / SQLite 从属 / 双向迁移 / 八条验收判据 -->
