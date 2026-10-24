"""数据库方言判定（v1.1.0 / 2026-10-24 起：**只可能是 PostgreSQL**）。

## 历史与现状

本模块原本是「方言判定的唯一实现」，目的是修 `core/database.py` 里 4 处各写一遍
「URL 前缀是不是 sqlite」的问题 —— 那时项目同时支持 SQLite（桌面）与 PG（服务器），
散落判断的代价是「换库时漏改一处，那一段就静默失效」（最坏的是时区归一：
把 PG 的 `timestamptz` 当 SQLite 文本再 +8 小时，静默改错数据）。

用户 2026-10-24 口径「**完全放弃 SQLite，全部迁移到 PG**」⇒ 现在只剩一种方言。
⚠️ 但**本模块不删除**：`is_postgres()` / `dialect_of()` 仍在 `database.py` 的
启动守卫里用（给错方言时给出**看得懂的报错**，而不是让 SQLAlchemy 抛
`NoSuchModuleError`）。`is_sqlite()` 保留是因为它是一句便宜的自省，
且守卫报错文案要用；**没有任何生产路径会走 SQLite 分支**。

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
    """真 ⇒ 配置成了 SQLite。

    ⚠️ v1.1.0 起 SQLite 已退役：**生产路径不会走到 True 分支**，
    唯一用途是启动守卫给出一句准确的报错（「当前方言 = sqlite」）。
    """

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
