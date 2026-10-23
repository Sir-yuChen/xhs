"""数据库方言判定的唯一实现（修设计 51 的方言散落问题）。

改造前 `core/database.py` 有 4 处各写一遍「URL 前缀是不是 sqlite」的字符串判断
（:44 `connect_args` / :57 PRAGMA / :103 WAL / :184 时区归一），
散落写法的问题是：**换成 PostgreSQL 时漏改一处，那一段就静默不生效**
（最坏的是 :184 —— 会把 PG 的 `timestamptz` 当 SQLite 文本再 +8 小时）。

判据：`backend/app` 里除本文件外，任何位置再写「URL 前缀含 sqlite 的字符串判断」即红
（守卫 `test_perms_core.py::test_no_raw_sqlite_sniffing_outside_the_dialect_module`）。
**方言判断只许调本模块**。

## 为什么按 URL 前缀判而不是 `engine.dialect.name`

`database.py` 在**模块导入期**就要决定 `connect_args`，那时 engine 还没建；
而 `create_engine()` 只接受部分方言的 connect_args。两处口径必须一致 ⇒
统一走 URL 前缀（URL 是唯一输入，engine 是它的产物）。
"""

from __future__ import annotations

from sqlalchemy import text

SQLITE = "sqlite"
POSTGRESQL = "postgresql"

#: 能识别的方言（PostgreSQL 的两种驱动前缀都归一成 `postgresql`）
_POSTGRES_PREFIXES: tuple[str, ...] = ("postgresql", "postgres")


def dialect_of(url: str | None) -> str:
    """从数据库 URL 判断方言，返回 `"sqlite"` / `"postgresql"` / 其他驱动名。

    未知驱动返回其 scheme 原文（不抛）：将来接 MySQL 时，
    `is_sqlite()` / `is_postgres()` 都为假，SQLite 专属代码自然跳过，
    不会因为不认识就崩在启动期。
    """
    scheme = str(url or "").split("://", 1)[0].strip().lower()
    if scheme.startswith(SQLITE):
        return SQLITE
    for prefix in _POSTGRES_PREFIXES:
        # 长度先判，避免 `postgres` 前缀误伤 `postgresql` 之外的东西（不会发生，但判据要明确）
        if scheme == prefix or scheme.startswith(prefix + "+"):
            return POSTGRESQL
    return scheme or "unknown"


def current_dialect() -> str:
    """当前配置的方言。**懒读 settings** —— 导入期调用 `get_settings()` 会与
    `database.py` 的导入顺序耦合（`config` ↔ `database` 循环）。
    """
    from backend.app.core.config import get_settings

    return dialect_of(get_settings().database_url)


def is_sqlite(url: str | None = None) -> bool:
    """真 ⇒ 需要 SQLite 专属处理（`check_same_thread` / PRAGMA / WAL / 文本时间）。"""
    return dialect_of(url) == SQLITE if url is not None else current_dialect() == SQLITE


def is_postgres(url: str | None = None) -> bool:
    """真 ⇒ 需要 PG 专属处理（连接池参数 / 原生 `BOOLEAN` 默认值 / `ILIKE`）。"""
    return dialect_of(url) == POSTGRESQL if url is not None else current_dialect() == POSTGRESQL


def bool_server_default(value: bool) -> object:
    """布尔列的 `server_default` —— **两库都能执行的写法**。

    SQLite 只认 `0/1` 整数，PG 只认 `true/false`；写 `"0"` 在 PG 上会
    `invalid input syntax for type boolean`，写 `"false"` 在 SQLite 上会被
    存成字符串 `"false"`（读出恒真 —— 最坏的情况：默认启用的开关默认关不掉）。

    ⇒ 统一发 `sa.text(...)`：SQLite 会把 `false` 当**字面量标识符**求值成 0，
    PG 求值成布尔 false。**两库语义一致**，且建表语句里看不到方言分支。
    """
    return text("true" if value else "false")
