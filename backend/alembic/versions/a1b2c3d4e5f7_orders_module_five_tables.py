"""orders module (design 16 §7.2, M6 direction 6): 5 read-only order tables

Revision ID: a1b2c3d4e5f7
Revises: z9a0b1c2d3e4, f2a3b4c5d6e7
Create Date: 2026-10-04

M6 方向 6 · 订单模块一期只读：建 5 张表（orders / order_items /
order_shipments / after_sales / order_remarks）。

⚠️ **没有数据迁移、没有换算、没有回填**：这是全新建表，存量库里一行订单
数据都没有（此前项目没有任何订单链路）。因此本迁移只建表，不做任何数据
搬运 —— 需要数据就跑「同步订单」，同步是 M6 自己的链路。

⚠️ **列必须与 `models/order.py` 逐字一致**（`test_schema_matches_models.py`
会拿 `create_all` 的测试库与真库对拍；对不上就是「测试绿、真机炸」）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from backend.app.core.migration_introspect import table_exists


revision: str = "a1b2c3d4e5f7"
# ⚠️ 双父（merge）：本迁移同时挂在两条已存在的链上 ——
#   * ``z9a0b1c2d3e4`` = 已提交的调度任务中心迁移（代码库主线，干净库上 upgrade 经过它）；
#   * ``f2a3b4c5d6e7`` = M3 热点迁移（**本机真库已 apply**；2026-10-07 由工作流迁移
#     改为它 —— 工作流整条特性退场后，那条迁移被删除，本行的另一父必须换成本机库
#     真实走过的链，否则 `alembic_version` 停在别处时 upgrade 会重放整条链
#     （而链上多条加列迁移**没有幂等守卫** ⇒ `duplicate column name`）。
# 双父保证两类环境都能 upgrade 到 head：干净库从 z9 走过来，本机库从 f2 走过来。
down_revision: Union[str, Sequence[str], None] = ("z9a0b1c2d3e4", "f2a3b4c5d6e7")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(conn, table: str) -> bool:
    """SQLite 判表存在（本迁移的**幂等守卫**）。

    ⚠️ **必须幂等**（与 `c9d0e1f2a3b4` / `e1f2a3b4c5d6` / `f2a3b4c5d6e7` 同规制）：
    迁移用例的常规打法是「stamp 回父 revision 再 `upgrade head`」，而那些夹具库
    由 `Base.metadata.create_all()` 建 ⇒ 表**已经在了**。无守卫 ⇒
    `sqlalchemy.exc.OperationalError: table orders already exists`
    （2026-10-04 实测：M6 的 `test_dedupe_seeded_scheduled_jobs_migration` 就红在这里，
    而报错被 alembic/fixture 包装吞掉，只看到「upgrade head 失败」）。
    """
    #: v1.1.0 PG 化：原为 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return table_exists(conn, table)


def _orders() -> None:
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("platform_account_id", sa.Integer(), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("order_no", sa.String(64), nullable=False, server_default=""),
        sa.Column("package_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("type_desc", sa.String(64), nullable=False, server_default=""),
        sa.Column("buyer_nick", sa.String(128), nullable=False, server_default=""),
        sa.Column("buyer_user_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("receiver_name_full", sa.String(128), nullable=False, server_default=""),
        sa.Column("receiver_phone_full", sa.String(64), nullable=False, server_default=""),
        sa.Column("receiver_province", sa.String(64), nullable=False, server_default=""),
        sa.Column("receiver_city", sa.String(64), nullable=False, server_default=""),
        sa.Column("receiver_address_full", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default=""),
        sa.Column("platform_status", sa.Integer(), nullable=True),
        sa.Column("platform_after_sale_status", sa.Integer(), nullable=True),
        sa.Column("total_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("pay_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("freight", sa.Numeric(12, 2), nullable=True),
        sa.Column("discount", sa.Numeric(12, 2), nullable=True),
        sa.Column("pay_method", sa.String(32), nullable=False, server_default=""),
        sa.Column("order_source", sa.String(32), nullable=False, server_default=""),
        sa.Column("logistics_company", sa.String(64), nullable=False, server_default=""),
        sa.Column("tracking_no", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("pay_time", sa.DateTime(), nullable=True),
        sa.Column("ship_time", sa.DateTime(), nullable=True),
        sa.Column("finish_time", sa.DateTime(), nullable=True),
        sa.Column("promise_ship_time", sa.DateTime(), nullable=True),
        sa.Column("platform_updated_at", sa.DateTime(), nullable=True),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.Column("seller_mark", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "platform_account_id", "order_no", name="uq_orders_account_order_no"
        ),
    )
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.create_index("ix_orders_id", ["id"], unique=False)
        batch_op.create_index("ix_orders_user_id", ["user_id"], unique=False)
        batch_op.create_index("ix_orders_platform_account_id", ["platform_account_id"], unique=False)
        batch_op.create_index("ix_orders_shop_id", ["shop_id"], unique=False)
        batch_op.create_index("ix_orders_order_no", ["order_no"], unique=False)
        batch_op.create_index("ix_orders_status", ["status"], unique=False)
        batch_op.create_index("ix_orders_tracking_no", ["tracking_no"], unique=False)
        batch_op.create_index("ix_orders_created_at", ["created_at"], unique=False)


def _order_items() -> None:
    op.create_table(
        "order_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("shop_product_id", sa.Integer(), nullable=True),
        sa.Column("item_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("item_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("item_image", sa.Text(), nullable=False, server_default=""),
        sa.Column("sku_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("sku", sa.String(255), nullable=False, server_default=""),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("refund_status", sa.String(64), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "sku_id", name="uq_order_items_order_sku"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["shop_product_id"], ["shop_products.id"]),
    )
    with op.batch_alter_table("order_items", schema=None) as batch_op:
        batch_op.create_index("ix_order_items_id", ["id"], unique=False)
        batch_op.create_index("ix_order_items_order_id", ["order_id"], unique=False)
        batch_op.create_index("ix_order_items_user_id", ["user_id"], unique=False)
        batch_op.create_index("ix_order_items_item_id", ["item_id"], unique=False)


def _order_shipments() -> None:
    op.create_table(
        "order_shipments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("package_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("logistics_company", sa.String(64), nullable=False, server_default=""),
        sa.Column("logistics_company_code", sa.String(64), nullable=False, server_default=""),
        sa.Column("tracking_no", sa.String(64), nullable=False, server_default=""),
        sa.Column("ship_time", sa.DateTime(), nullable=True),
        sa.Column("sign_time", sa.DateTime(), nullable=True),
        sa.Column("warehouse_desc", sa.String(128), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "package_id", name="uq_order_shipments_order_package"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    with op.batch_alter_table("order_shipments", schema=None) as batch_op:
        batch_op.create_index("ix_order_shipments_id", ["id"], unique=False)
        batch_op.create_index("ix_order_shipments_order_id", ["order_id"], unique=False)
        batch_op.create_index("ix_order_shipments_user_id", ["user_id"], unique=False)
        batch_op.create_index("ix_order_shipments_tracking_no", ["tracking_no"], unique=False)


def _after_sales() -> None:
    op.create_table(
        "after_sales",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("after_sale_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("type", sa.String(64), nullable=False, server_default=""),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default=""),
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("apply_time", sa.DateTime(), nullable=True),
        sa.Column("handle_time", sa.DateTime(), nullable=True),
        sa.Column("handle_result", sa.Text(), nullable=False, server_default=""),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "after_sale_id", name="uq_after_sales_order_aftersale"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    with op.batch_alter_table("after_sales", schema=None) as batch_op:
        batch_op.create_index("ix_after_sales_id", ["id"], unique=False)
        batch_op.create_index("ix_after_sales_order_id", ["order_id"], unique=False)
        batch_op.create_index("ix_after_sales_user_id", ["user_id"], unique=False)
        batch_op.create_index("ix_after_sales_status", ["status"], unique=False)


def _order_remarks() -> None:
    op.create_table(
        "order_remarks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="订单"),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    with op.batch_alter_table("order_remarks", schema=None) as batch_op:
        batch_op.create_index("ix_order_remarks_id", ["id"], unique=False)
        batch_op.create_index("ix_order_remarks_order_id", ["order_id"], unique=False)
        batch_op.create_index("ix_order_remarks_user_id", ["user_id"], unique=False)
        batch_op.create_index("ix_order_remarks_order_kind", ["order_id", "kind"], unique=False)


def upgrade() -> None:
    conn = op.get_bind()
    # ⚠️ 逐表判存在（幂等；见 `_table_exists` 的说明）。
    if not _table_exists(conn, "orders"):
        _orders()
    if not _table_exists(conn, "order_items"):
        _order_items()
    if not _table_exists(conn, "order_shipments"):
        _order_shipments()
    if not _table_exists(conn, "after_sales"):
        _after_sales()
    if not _table_exists(conn, "order_remarks"):
        _order_remarks()


def downgrade() -> None:
    conn = op.get_bind()
    # ⚠️ 同样判存在：降级时表可能本就不在（幂等；否则 drop 会抛
    # `no such table`，让「降级 → 再升级」这条常规路径红）。
    for table in ("order_remarks", "after_sales", "order_shipments", "order_items", "orders"):
        if _table_exists(conn, table):
            op.drop_table(table)
