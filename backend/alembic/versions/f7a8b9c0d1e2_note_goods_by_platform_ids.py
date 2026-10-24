"""rebuild note_goods by platform ids (平台笔记 ID + 平台商品 ID)

2026-09-27 用户口径：

> 「同步笔记时，如果笔记挂有商品必须将笔记与商品的关系落库到笔记与商品关系表，
> **无需校验商品是否存在**」＋「**关系表改存平台商品 ID 和平台笔记 ID**」。

## 问题（旧键为什么挡住关系落库）

旧表键是**本地主键**：`note_id → notes.id`、`product_id → shop_products.id`
（迁移 `r0s1t2u3v4w5` 建，唯一键 `uq_note_goods_note_product`）。
解析器 `shop_goods_service.link_note_goods` 因此必须**两侧本地行都在**才建关系，
任一侧缺失 ⇒ 跳过（§26.2 原第 3 条「不造假，等下一次同步自然补上」）。

后果（真实库实测）：`shop_note_goods` 缓存里 **1933 个挂载条目**，其中 **16 个** 的
`item_id` 在 `shop_products` 里查不到（商品已下架 / 台账还没同步过）⇒ 这些关系
**永远落不了库**，内容库那条笔记的「挂载商品」区空着 —— 而商品标题就躺在缓存行里。

## 改法（本迁移）

| 列 | 旧 | 新 |
|---|---|---|
| 归属 | （无） | `user_id` FK `users.id`（索引）—— 对齐 `notes` 的身份键 `(user_id, note_id)`，守住账号维度隔离 |
| 笔记 | `note_id` INTEGER FK `notes.id` | `note_id` VARCHAR(128) = **平台笔记 ID**（`notes.note_id`） |
| 商品 | `product_id` INTEGER FK `shop_products.id` | `item_id` VARCHAR(64) = **平台商品 ID**（`shop_products.item_id`） |
| 唯一键 | `(note_id, product_id)` | `(user_id, note_id, item_id)`（`uq_note_goods_user_note_item`） |
| 外键 | 两个 | **全去** ⇒ 「关系指向已删本地行」在结构上不可能再出现 |

## 回填口径

按 `notes.id → notes.note_id` + `shop_products.id → shop_products.item_id` 反解出平台 ID；
**join 不上的旧行直接丢弃** —— 它们正是 §26.6 意义上的孤儿行（指向已不存在的本地行），
在新模型里没有任何合法表示。

按 `(user_id, 平台 note_id, 平台 item_id)` 去重（旧键是本地主键，理论上不会撞，
但 join 之后可能撞 —— 唯一键必须能建起来）。

## 幂等（本项目的硬纪律，见设计 03 §9.5.1）

应用**启动时自动 `upgrade head`**，而 SQLite 的 DDL **不在事务里** ⇒ 迁移中途失败会
留下「改了一半 + `alembic_version` 没推进」的状态，下次启动从同一条 revision 重跑。
因此本迁移先查 `PRAGMA table_info`：已经是新结构（有 `item_id`、没有 `product_id`）
就**直接返回**；旧结构才重建。

## 可逆

`downgrade()` 按 `(user_id, 平台 note_id)` / `(user_id, 平台 item_id)` 反查本地行重建
旧表；**反查不到的行丢弃**（回滚方向上它们本来就是「指不到本地行」的关系）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

from backend.app.core.migration_introspect import column_names

revision = "f7a8b9c0d1e2"
down_revision = "g6h7i8j9k0l1"
branch_labels = None
depends_on = None

_TABLE = "note_goods"
_TMP = "note_goods_new"


def _columns(conn, table: str) -> set[str]:
    """表的列名集合（表不存在 ⇒ 空集）。"""
    #: v1.1.0 PG 化：原为 `PRAGMA table_info`（SQLite 专属），统一走内省助手。
    return column_names(conn, table)


def _create_indexes(conn, *, user_id: bool, item_id: bool) -> None:
    conn.execute(sa.text(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_id ON {_TABLE} (id)"))
    if user_id:
        conn.execute(
            sa.text(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_user_id ON {_TABLE} (user_id)")
        )
    conn.execute(
        sa.text(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_note_id ON {_TABLE} (note_id)")
    )
    if item_id:
        conn.execute(
            sa.text(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_item_id ON {_TABLE} (item_id)")
        )


def upgrade() -> None:
    conn = op.get_bind()
    columns = _columns(conn, _TABLE)
    if not columns:
        return  # 表不存在（全新库由模型建）⇒ 无存量可迁
    if "item_id" in columns and "product_id" not in columns:
        return  # 已经是新结构（重跑 / 半途失败后继续）⇒ 幂等返回

    conn.exec_driver_sql(f"DROP TABLE IF EXISTS {_TMP}")
    conn.exec_driver_sql(
        f"""
        CREATE TABLE {_TMP} (
            id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            note_id VARCHAR(128) NOT NULL,
            item_id VARCHAR(64) NOT NULL,
            created_at TIMESTAMP NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_note_goods_user_note_item UNIQUE (user_id, note_id, item_id),
            FOREIGN KEY(user_id) REFERENCES users (id)
        )
        """
    )
    conn.exec_driver_sql(
        f"""
        INSERT INTO {_TMP} (id, user_id, note_id, item_id, created_at)
        SELECT MIN(ng.id), n.user_id, n.note_id, p.item_id, MIN(ng.created_at)
          FROM {_TABLE} AS ng
          JOIN notes AS n ON n.id = ng.note_id
          JOIN shop_products AS p ON p.id = ng.product_id
         GROUP BY n.user_id, n.note_id, p.item_id
        """
    )
    conn.exec_driver_sql(f"DROP TABLE {_TABLE}")
    conn.exec_driver_sql(f"ALTER TABLE {_TMP} RENAME TO {_TABLE}")
    _create_indexes(conn, user_id=True, item_id=True)


def downgrade() -> None:
    conn = op.get_bind()
    columns = _columns(conn, _TABLE)
    if not columns:
        return
    if "product_id" in columns and "item_id" not in columns:
        return  # 已是旧结构

    conn.exec_driver_sql("DROP TABLE IF EXISTS note_goods_old")
    conn.exec_driver_sql(
        """
        CREATE TABLE note_goods_old (
            id INTEGER NOT NULL,
            note_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_note_goods_note_product UNIQUE (note_id, product_id),
            FOREIGN KEY(note_id) REFERENCES notes (id),
            FOREIGN KEY(product_id) REFERENCES shop_products (id)
        )
        """
    )
    conn.exec_driver_sql(
        f"""
        INSERT INTO note_goods_old (id, note_id, product_id, created_at)
        SELECT MIN(ng.id), n.id, p.id, MIN(ng.created_at)
          FROM {_TABLE} AS ng
          JOIN notes AS n
            ON n.user_id = ng.user_id AND n.note_id = ng.note_id
          JOIN shop_products AS p
            ON p.user_id = ng.user_id AND p.item_id = ng.item_id
         GROUP BY n.id, p.id
        """
    )
    conn.exec_driver_sql(f"DROP TABLE {_TABLE}")
    conn.exec_driver_sql("ALTER TABLE note_goods_old RENAME TO note_goods")
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_note_goods_id ON note_goods (id)")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_note_goods_note_id ON note_goods (note_id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_note_goods_product_id ON note_goods (product_id)"
    )
