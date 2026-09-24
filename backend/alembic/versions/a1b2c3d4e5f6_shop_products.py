"""T-802：账号矩阵同步 —— 新建 `shop_products`（店铺商品本地台账）

Revision ID: a1b2c3d4e5f6
Revises: f4a1c8e2d935
Create Date: 2026-09-19

## 为什么需要这张表

「同步店铺商品」（设计 13 §3.1）要的是**本地台账**：离线可查、可检索、供商品
笔记生成取用。`GET /xhs/qianfan/items` 是「看当下状态」的实时读侧，两者并存、
不互相替代 —— 所以需要落库表（此前商品列表明确不建表）。

## 表结构口径（设计 13 §3.1 为权威）

* `price` 用 **String(32)** 而非 Integer：千帆价格字段是元+分混排的字符串，
  存字符串避免浮点与进制误差，展示与校验按原样比对；
* `stock` / `sales` **nullable**：千帆缺值时留空，不造 0（硬约束⑤ 缺值口径）；
* `status` 存**状态标签原文**，不写死枚举：千帆状态枚举会变，白名单映射集中在
  SDK 层（`apis/xhs_qianfan_apis.py::QIANFAN_ITEM_STATUS_LABELS`）；
* `raw_json` 存千帆条目**整条原文**：字段集还会变，真值留本地，新增展示项不
  需要改表也不需要再打一次平台；
* `shop_id_fk` 指向 `shops.id`（T-32 的 N:M 店铺实体）且 **NOT NULL**：商品必须
  属于一个店铺实体，解不出店铺行 → 同步失败留任务行说明，**不编店铺**。

## 索引

唯一约束 `UNIQUE (shop_id_fk, item_id)` 兼作查询索引（按店铺列商品）。
`user_id` 单列索引供跨店铺的用户级列举。其余列不建索引（无消费者）。

## 回滚

本表为本需求新建，无历史数据需要保留 —— `downgrade` 直接删表。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f4a1c8e2d935"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shop_products",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        # 指向 T-32 落地的 `shops` 实体；NOT NULL —— 商品必须属于一个店铺。
        sa.Column(
            "shop_id_fk",
            sa.Integer(),
            sa.ForeignKey("shops.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("item_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("item_title", sa.String(length=512), nullable=False, server_default=""),
        # 字符串存价格：千帆价格是元+分混排字符串，存原文避免进制/浮点误差。
        sa.Column("price", sa.String(length=32), nullable=False, server_default=""),
        # nullable：千帆缺值时留空，不造 0。
        sa.Column("stock", sa.Integer(), nullable=True),
        sa.Column("sales", sa.Integer(), nullable=True),
        sa.Column("category", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("images_json", sa.Text(), nullable=False, server_default="[]"),
        # 状态标签原文：不写死枚举（映射在 SDK 层）。
        sa.Column("status", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("shop_id_fk", "item_id", name="uq_shop_products_shop_item"),
    )


def downgrade() -> None:
    op.drop_table("shop_products")
