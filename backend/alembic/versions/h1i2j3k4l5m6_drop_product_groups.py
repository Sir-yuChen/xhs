"""彻底移除商品分组（2026-10-05 用户口径「彻底移除商品分组相关逻辑」）

| 操作 | 对象 |
|---|---|
| **删表** | `product_groups` —— M4 引入的本地分组（迁移 `g0a1b2c3d4e5` 建） |
| **删列** | `shop_products.group_id`（同一条迁移加的） |

## 为什么两件事一条迁移

它们是**同一个功能的两个半边**：`group_id` 只有在 `product_groups` 存在时才
有意义（一个指向已不存在表的 id 是纯噪声）。分开跑会出现「表没了但列还在」的
中间态，而中间态下任何 `SELECT ... group_id` 都会把错值当正常值读出来。

## 为什么用裸 `ALTER TABLE ... DROP COLUMN` 而不是 `batch_alter_table`

`batch_alter_table` 走的是「建新表 → 拷数据 → 删旧表 → 换名」的重建路径，而
`shop_products` 被 **3 张表**用 FK 引用（`shop_metric_snapshots` /
`topic_goods_match` / `order_items`）⇒ 重建时那句 `DROP TABLE shop_products`
直接撞 `FOREIGN KEY constraint failed`（真机实测，副本库上跑出来的）。

SQLite ≥ 3.35 支持**原生** `DROP COLUMN`（本项目实测 3.53.1），它**不重建表**
⇒ 绕开 FK 问题。前提是这一列上**没有索引** —— 查过：`shop_products` 只有
`id` / `user_id` / `platform_account_id` 三个索引，`group_id` 上**没有**索引
（迁移 `g0a1b2c3d4e5` 用 `ALTER TABLE ADD COLUMN` 加列，SQLite 不支持在同一条
语句里建索引，所以 ORM 上那句 `index=True` 从来没落成真索引）。

## 幂等

两件事都**先判存在**（与 `e1f2a3b4c5d6` / `c9d0e1f2a3b4` 同一判据）——
本项目「stamp 回早期 revision 再 upgrade」是**正常用法**（迁移用例这么跑），
夹具库用 `create_all()` 建表 ⇒ 表 / 列可能已经不在，不判存在就炸。

⚠️ **不删任何商品行**：分组是弱关联，删的是「组织方式」。
`shop_products` 的其余列与全部行**一件不动**。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.app.core.migration_introspect import column_names, table_exists

revision = "h1i2j3k4l5m6"
down_revision = "d2e3f4a5b6c7"


def _table_exists(conn: object, name: str) -> bool:
    #: v1.1.0 PG 化：原为 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return table_exists(conn, name)


def _existing_columns(conn: object, table: str) -> set[str]:
    """表不存在 ⇒ 空集（调用方据此跳过整段，**不是**判成「列都没有」）。

    ⚠️ 这个区分是必需的：`_existing_columns` 对不存在的表返回空集，调用方若
    拿它当「列都没有」就会去 `ADD COLUMN` 到一张不存在的表上（DEF 同款坑）。
    """
    #: v1.1.0 PG 化：原为 `PRAGMA table_info`（SQLite 专属），统一走内省助手。
    return column_names(conn, table)


def upgrade() -> None:
    conn = op.get_bind()

    # 1) 删 `shop_products.group_id`（原生 DROP COLUMN，不重建表 ⇒ 不撞 FK）。
    if "group_id" in _existing_columns(conn, "shop_products"):
        op.execute("ALTER TABLE shop_products DROP COLUMN group_id")

    # 2) 删 `product_groups` 表（连同唯一键约束一起消失）。
    # ⚠️ 此时 `shop_products.group_id` 已经不存在 ⇒ 没有任何 FK 指向它。
    if _table_exists(conn, "product_groups"):
        op.drop_table("product_groups")


def downgrade() -> None:
    """回滚：**重建表与列，但不回填任何分组数据**（数据已随升级丢弃）。

    ⚠️ 如实说明：`downgrade` 只恢复**结构**——升级时删掉的分组行与
    `group_id` 取值**无法恢复**（没有留备份表）。这与本项目其他「退场」类
    迁移（如 `m5n6o7p8q9r0_drop_shop_account_bindings`）同口径。
    """
    conn = op.get_bind()

    if not _table_exists(conn, "product_groups"):
        op.create_table(
            "product_groups",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("user_id", "name", name="uq_product_groups_user_name"),
        )
        op.create_index("ix_product_groups_id", "product_groups", ["id"])
        op.create_index("ix_product_groups_user_id", "product_groups", ["user_id"])

    if "group_id" not in _existing_columns(conn, "shop_products"):
        op.execute("ALTER TABLE shop_products ADD COLUMN group_id INTEGER")
