"""drop order_remarks —— 订单备注退场（设计 75，2026-10-27）

用户口径（2026-10-26）：「订单备注不保留直接移除」。

## 为什么删

订单备注是本方案里**唯一一处**「平台不存在但我方要留」的订单数据。
用户决定不留 ⇒ 订单域变成「平台有什么就是什么」，本地零副本，
彻底消除「本地与平台不一致」这一类问题。

连带退场：
* 端点 `POST /qianfan/orders/remark` → 410 墓碑（`ORDER_REMARK_REMOVED`）
* 服务层 `order_sync_service.upsert_remark` 整函数
* ORM `OrderRemark` 整类（**必须删** —— 留着 `create_all()` 会把表建回来，
  v2.0 删 `model_configs` 时踩过同一个坑）
* 订单详情响应里的 `remarks` 字段（**删字段**而不是留空数组：
  留空会让前端以为「有功能但这次没数据」）

## 依赖关系

`order_remarks.order_id` 有外键指向 `orders.id`。DROP 的是**子表**（备注），
父表 `orders` 本次不动 ⇒ 反向依赖（谁引用 `order_remarks`）**没有**，
不需要先清引用方。`user_id` 同理指向 `users`（父表不动）。

Revision ID: m7n8o9p0q1r2
Revises: j0k1l2m3n4o5
Create Date: 2026-10-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "m7n8o9p0q1r2"
down_revision = "j0k1l2m3n4o5"
branch_labels = None
depends_on = None

TABLE = "order_remarks"


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def upgrade() -> None:
    #: 幂等：表可能已被手动删过（开发库上反复试过），`DROP TABLE` 会报错。
    if _has_table(TABLE):
        op.drop_table(TABLE)


def downgrade() -> None:
    """重建空表（**不回填数据** —— 备注内容随功能一起退场，没有来源可恢复）。"""
    if _has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_order_remarks_id", TABLE, ["id"])
    op.create_index("ix_order_remarks_order_id", TABLE, ["order_id"])
    op.create_index("ix_order_remarks_user_id", TABLE, ["user_id"])
    op.create_index("ix_order_remarks_order_kind", TABLE, ["order_id", "kind"])
