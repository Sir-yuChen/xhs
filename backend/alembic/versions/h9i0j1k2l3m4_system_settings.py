"""create system_settings (运行时配置中心)

2026-09-27 用户口径：

> 「配置表再设置页面实现前端的列表和增删改查，实现页面**实时配置**」。

## 为什么现在才建这张表

设计 10 §3 早就写好了目标设计（`system_settings` 表 + 系统管理页），但**一直没落地**，
前置 P-1（`users.role`）/ P-2（配置优先级缺陷）已在 2026-09-18 完成，**P-3（缓存失效）
一直挂着** —— 本次与表一起做（设计 10 §7.4）。

触发它的直接原因是一个**管理缺陷**（正是设计 10 §1 要消灭的那类）：`crawl_rate_limit_per_minute`
在 `config/default.yaml` 里声明 60，但 `rate_limiter.get_rate_limiter()` 把 5 **写死**在构造参数里
（优先级「构造参数 > settings > 5」⇒ 构造参数赢）⇒ 配置**声明了却读不到**，
1867 篇欠账因此要跑 6.2 小时（设计 13 §38）。

## 与设计 10 §3.1 的差异（原句保留，理由见设计 10 §7.1）

| 项 | §3.1 原设计 | 本迁移 |
|---|---|---|
| `scope` 列（`global` / `user`） | 有 | **去掉** —— 全项目口径已统一为「所有配置行必归属用户」（DEF-066），`scope` 是同一语义的第二份表达（DEF-044） |
| `user_id` | nullable | **NOT NULL** —— 归属是身份键的一部分，不是可选项 |
| 唯一键 | `key` 单列 unique | **`(user_id, key)`** —— 单列唯一会让「用户 1 建了 `a`，用户 2 就建不了 `a`」，而多用户各有各的运行期配置 |

## 幂等（本项目硬纪律，见设计 03 §9.5.1）

应用启动时自动 `upgrade head`，而 SQLite 的 DDL **不在事务里** ⇒ 中途失败会留下
「建了一半 + `alembic_version` 没推进」，下次启动从同一条 revision 重跑。
因此 `upgrade()` 先查 `PRAGMA table_info`：**表已存在就直接返回**。

## 可逆

`downgrade()` 直接 `DROP TABLE` —— 本表是**运行期配置**，没有需要回填到别处的历史数据；
配置值本身另有 git 侧来源（`config/default.yaml` + `config/initial_data/system_settings.json`），
删表不会让系统失去启动能力（读点回落 settings / 内置默认）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

from backend.app.core.migration_introspect import column_names

revision = "h9i0j1k2l3m4"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None

_TABLE = "system_settings"


def _columns(conn, table: str) -> set[str]:
    """表的列名集合（表不存在 ⇒ 空集）。"""
    #: v1.1.0 PG 化：原为 `PRAGMA table_info`（SQLite 专属），统一走内省助手。
    return column_names(conn, table)


def upgrade() -> None:
    conn = op.get_bind()
    if _columns(conn, _TABLE):
        return  # 表已存在（重跑 / 半途失败后继续）⇒ 幂等返回

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        # `value` 是 **JSON 文本**（不是 JSON 列）：配置行只做整串替换，
        # 用 Text 少一个「JSON 列原地改不标脏」的坑（本项目踩过）。
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("value_type", sa.String(length=16), nullable=False, server_default="str"),
        sa.Column("category", sa.String(length=32), nullable=False, server_default="feature"),
        sa.Column("label", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("requires_restart", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_dangerous", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # ⚠️ 唯一键是 (user_id, key) 而不是 key 单列：多用户各有各的运行期配置
        # （DEF-066 口径：所有行必归属用户）。
        sa.UniqueConstraint("user_id", "key", name="uq_system_settings_user_key"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
    )
    op.create_index(f"ix_{_TABLE}_id", _TABLE, ["id"])
    op.create_index(f"ix_{_TABLE}_user_id", _TABLE, ["user_id"])
    op.create_index(f"ix_{_TABLE}_key", _TABLE, ["key"])


def downgrade() -> None:
    conn = op.get_bind()
    if not _columns(conn, _TABLE):
        return
    for index in ("id", "user_id", "key"):
        conn.exec_driver_sql(f"DROP INDEX IF EXISTS ix_{_TABLE}_{index}")
    conn.exec_driver_sql(f"DROP TABLE {_TABLE}")
