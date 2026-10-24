"""迁移脚本共用的表结构内省助手（**PostgreSQL 专用**）。

## 为什么存在（v1.1.0 / 2026-10-24，SQLite 退役）

迁移链长期用 SQLite 专属的 `PRAGMA table_info` / `sqlite_master` 判
「表 / 列 / 索引在不在」。桌面端退役后只剩一种方言，而**全新安装**
（docker-compose 空库首启）要在 PG 上从头跑完整迁移链 —— 这些写法必须统一
换成 `information_schema` / `pg_indexes`。

集中一处（DEF-044：同一语义不留两份实现）：迁移文件一律
`from backend.app.core.migration_introspect import ...`，**不许**再内联
`PRAGMA` / `sqlite_master`（守卫见 `test_migration_introspect.py`）。

## 为什么不做方言分支

SQLite 已整体退役（`core/database.py` 非 PG 直接拒绝启动），不存在
「两种方言都要跑」的场景。留一个 `if is_sqlite(...)` 分支等于两套语义各写一份，
而本项目已有过「方言分支漏改一处就静默失效」的教训（见 `db_dialect` 模块
docstring）。历史迁移在 SQLite 上的执行结果已经是既成事实（真实库已 stamp 到
head），这里只需要服务 PG 的现在与将来。

## conn 参数

接受任何带 `.execute(sa.text(...))` 的对象（`Connection` / `Engine` 均可）——
迁移里两种都会传。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text


def table_exists(conn: Any, table: str) -> bool:
    """当前 schema 里表是否存在（等价旧 `sqlite_master type='table'` 查询）。"""
    row = conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": table},
    ).first()
    return row is not None


def index_exists(conn: Any, index: str) -> bool:
    """当前 schema 里索引是否存在（等价旧 `sqlite_master type='index'` 查询）。"""
    row = conn.execute(
        text(
            "SELECT 1 FROM pg_indexes "
            "WHERE schemaname = current_schema() AND indexname = :i"
        ),
        {"i": index},
    ).first()
    return row is not None


def column_names(conn: Any, table: str) -> set[str]:
    """表的列名集合（等价旧 `PRAGMA table_info` 的 `row[1]` 列）。

    ⚠️ 表不存在时返回**空集**而不是报错 —— 与 PRAGMA 行为一致，
    迁移的「列在不在」判据依赖这个语义（不在 ⇒ 走加列分支）。
    """
    return {
        row[0]
        for row in conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": table},
        )
    }


def index_names(conn: Any, table: str) -> set[str]:
    """表上的索引名集合（等价旧 `PRAGMA index_list` 的 name 列）。"""
    return {
        row[0]
        for row in conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = current_schema() AND tablename = :t"
            ),
            {"t": table},
        )
    }
