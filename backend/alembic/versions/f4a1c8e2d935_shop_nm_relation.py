"""T-32：账号 ↔ 店铺放回 N:M —— 新建 `shops`，绑定表加 `shop_id_fk` 并换唯一约束

Revision ID: f4a1c8e2d935
Revises: e2b8f1a6c4d7
Create Date: 2026-09-19

## 这次迁移为什么必须重建表

`shop_account_bindings` 原来的 1:1 语义由**列级 `UNIQUE (platform_account_id)`** 表达；
SQLite 不支持 `ALTER TABLE ... DROP CONSTRAINT`，所以「去掉这个唯一约束」只能靠
batch 模式重建表（与 `10d6e0bf7083` 同一手法，`env.py` 已开 `render_as_batch=True`）。

## 顺序（先有店铺实体，再把绑定指过去，最后才换约束）

1. 建 `shops` 表；
2. 给绑定表加 `shop_id_fk`（**可空**，迁移期）；
3. **按 `seller_id` 去重**回填：每个不同的店铺插一行 `shops`，并把已有绑定的 `shop_id_fk` 指过去
   —— 这一步让「历史绑定不丢」，同时把 N:M 的店铺实体准备好；
4. batch 重建绑定表：去掉 `UNIQUE (platform_account_id)`、加 `UNIQUE (shop_id_fk, platform_account_id)`
   与外键。

## 回填口径

* 同一用户下 `shop_user_id` 相同的绑定**合并到同一个店铺行**（这正是 N:M 要的）；
* `shop_user_id` 与 `shop_name` 都为空的历史行（理论不存在，bind 会拒绝）→ 不建店铺行，
  `shop_id_fk` 留空：宁可留空也不编一个店铺出来。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f4a1c8e2d935"
down_revision: Union[str, None] = "e2b8f1a6c4d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shops",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("seller_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("shop_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("shop_name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "seller_id", name="uq_shops_user_seller"),
    )
    op.create_index("ix_shops_id", "shops", ["id"])
    op.create_index("ix_shops_user_id", "shops", ["user_id"])

    # ① 先加可空列（SQLite 支持 ADD COLUMN，不需要重建）
    op.add_column("shop_account_bindings", sa.Column("shop_id_fk", sa.Integer(), nullable=True))

    # ② 回填：按 (user_id, seller_id) 去重建店铺行，绑定指过去
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, user_id, shop_user_id, shop_name FROM shop_account_bindings "
            "ORDER BY id"
        )
    ).fetchall()

    shops: dict[tuple[int, str], int] = {}
    for row in rows:
        seller_id = (row[2] or "").strip()
        shop_name = (row[3] or "").strip()
        if not seller_id and not shop_name:
            # 没有店铺信息的历史行：留空 FK，不编造店铺
            continue
        key = (row[1], seller_id)
        shop_pk = shops.get(key)
        if shop_pk is None:
            result = bind.execute(
                sa.text(
                    "INSERT INTO shops (user_id, seller_id, shop_id, shop_name, status, "
                    "created_at, updated_at) VALUES (:user_id, :seller_id, '', :shop_name, "
                    "'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"user_id": row[1], "seller_id": seller_id, "shop_name": shop_name},
            )
            shop_pk = result.lastrowid
            shops[key] = shop_pk
        bind.execute(
            sa.text("UPDATE shop_account_bindings SET shop_id_fk = :shop_id WHERE id = :id"),
            {"shop_id": shop_pk, "id": row[0]},
        )

    # ③ 换唯一约束：1:1 → N:M
    #
    # ⚠️ 这里**不能**用 `batch_op.alter_column(..., unique=False)`：实测（真实库副本）
    # 那条语句**不会**去掉 SQLite 上的列级 `UNIQUE (platform_account_id)` ——
    # 迁移"成功"、旧的 1:1 限制却还在（再绑第二个店铺仍报 UNIQUE constraint failed），
    # 属于最难发现的一类错：DDL 无报错、语义没变。所以按 SQLite 官方 12 步**显式重建**。
    #
    # 本库 `PRAGMA foreign_keys` 实测为 0（`core/database.py` 只设 journal_mode，
    # 全仓无 foreign_keys=ON）→ 重建期间不必处理级联；`shop_note_goods.binding_id`
    # 引用的是**保留不动的 id**，重建后仍然有效。
    if bind.dialect.name == "sqlite":
        bind.exec_driver_sql(
            """
            CREATE TABLE shop_account_bindings_nm (
                id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                platform_account_id INTEGER NOT NULL,
                shop_id_fk INTEGER,
                shop_user_id VARCHAR(64) NOT NULL,
                shop_name VARCHAR(128) NOT NULL,
                encrypted_cookies TEXT NOT NULL,
                status VARCHAR(32) NOT NULL,
                status_message TEXT NOT NULL,
                goods_note_total INTEGER NOT NULL,
                goods_synced_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                health_checked_at DATETIME,
                PRIMARY KEY (id),
                FOREIGN KEY(platform_account_id) REFERENCES platform_accounts (id),
                FOREIGN KEY(user_id) REFERENCES users (id),
                FOREIGN KEY(shop_id_fk) REFERENCES shops (id),
                UNIQUE (shop_id_fk, platform_account_id)
            )
            """
        )
        bind.exec_driver_sql(
            """
            INSERT INTO shop_account_bindings_nm (
                id, user_id, platform_account_id, shop_id_fk, shop_user_id, shop_name,
                encrypted_cookies, status, status_message, goods_note_total, goods_synced_at,
                created_at, updated_at, health_checked_at
            )
            SELECT id, user_id, platform_account_id, shop_id_fk, shop_user_id, shop_name,
                   encrypted_cookies, status, status_message, goods_note_total, goods_synced_at,
                   created_at, updated_at, health_checked_at
            FROM shop_account_bindings
            """
        )
        bind.exec_driver_sql("DROP TABLE shop_account_bindings")
        bind.exec_driver_sql(
            "ALTER TABLE shop_account_bindings_nm RENAME TO shop_account_bindings"
        )
        for index_name, columns in (
            ("ix_shop_account_bindings_id", "id"),
            ("ix_shop_account_bindings_user_id", "user_id"),
            ("ix_shop_account_bindings_platform_account_id", "platform_account_id"),
            ("ix_shop_account_bindings_shop_id_fk", "shop_id_fk"),
        ):
            bind.exec_driver_sql(
                f"CREATE INDEX {index_name} ON shop_account_bindings ({columns})"
            )
        return

    # 非 SQLite：走标准 batch（PostgreSQL 等能直接 drop 约束）
    with op.batch_alter_table("shop_account_bindings", schema=None) as batch_op:
        batch_op.alter_column(
            "platform_account_id",
            existing_type=sa.Integer(),
            nullable=False,
            unique=False,
        )
        batch_op.create_unique_constraint(
            "uq_shop_bindings_shop_account", ["shop_id_fk", "platform_account_id"]
        )
        batch_op.create_foreign_key(
            "fk_shop_bindings_shop_id_fk", "shops", ["shop_id_fk"], ["id"]
        )


def downgrade() -> None:
    """回到 1:1。

    有「同一账号绑了多个店铺」的数据时**故意报错**：静默删行 = 悄悄丢数据。
    """
    bind = op.get_bind()
    duplicated = bind.execute(
        sa.text(
            "SELECT platform_account_id, COUNT(*) FROM shop_account_bindings "
            "GROUP BY platform_account_id HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if duplicated:
        raise RuntimeError(
            "存在「同一账号绑定多个店铺」的数据，无法回到 1:1；"
            f"请先清理这些绑定：{duplicated}"
        )

    with op.batch_alter_table("shop_account_bindings", schema=None) as batch_op:
        batch_op.drop_constraint("fk_shop_bindings_shop_id_fk", type_="foreignkey")
        batch_op.drop_constraint("uq_shop_bindings_shop_account", type_="unique")
    op.execute("ALTER TABLE shop_account_bindings DROP COLUMN shop_id_fk")

    op.drop_index("ix_shops_user_id", table_name="shops")
    op.drop_index("ix_shops_id", table_name="shops")
    op.drop_table("shops")
