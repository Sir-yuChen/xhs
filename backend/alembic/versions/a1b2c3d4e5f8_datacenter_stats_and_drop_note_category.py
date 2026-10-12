"""数据中心统计表 + 笔记分类列退役（设计 35，2026-10-12）

Revision ID: a1b2c3d4e5f8
Revises: c8d9e0f1a2b3
Create Date: 2026-10-12

## 做什么

1. 新建 `note_datacenter_stats`（千帆数据中心统计，独立历史，无外键）。
2. `DROP COLUMN notes.note_category`（退役，分类权威改统计表 `note_flag`）。

## ⚠️ 历史数据重建不在本迁移里做

本迁移只落**表结构**（建表 + 删列）。历史数据的重建（把 `notes.raw_json.ark` /
`raw_json.metrics` 里已有的 2005 条并集迁进统计表）是**一次性数据脚本**
`.hermes/scripts/migrate_datacenter_stats.py`，在迁移跑完后单独执行 ——
把「结构变更」与「数据回填」分开，迁移可逆、数据回填幂等。

## 为什么删列而不是留空

用户口径（2026-10-12）：「note_category 退役且不保留，相关逻辑删除、相关数据
迁移修复调整」。留空列 = 同一语义留一个死字段 + 诱导后人误读。删列干净。

⚠️ SQLite 3.35+ 才支持 `DROP COLUMN`；本机 SQLite 版本满足（见 `core/database.py`
的 `event` 监听，项目已在多处用 `drop_column`）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f8"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "note_datacenter_stats",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("platform_account_id", sa.Integer(), nullable=False),
        sa.Column("note_id", sa.String(length=128), nullable=False),
        sa.Column("stat_date", sa.String(length=10), nullable=False),
        sa.Column("window", sa.String(length=16), nullable=False, server_default="recent_1"),
        sa.Column("note_flag", sa.String(length=24), nullable=False, server_default=""),
        # 快照列
        sa.Column("title", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("cover_url", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("item_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("item_title", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("variant_desc", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("publish_time", sa.Integer(), nullable=True),
        # 互动（可空 = 缺值，无流量判定时 null 不参与）
        sa.Column("read_count", sa.Integer(), nullable=True),
        sa.Column("like_count", sa.Integer(), nullable=True),
        sa.Column("collect_count", sa.Integer(), nullable=True),
        sa.Column("comment_count", sa.Integer(), nullable=True),
        sa.Column("share_count", sa.Integer(), nullable=True),
        sa.Column("follow_count", sa.Integer(), nullable=True),
        sa.Column("danmaku_count", sa.Integer(), nullable=True),
        # 观看
        sa.Column("video_duration", sa.Float(), nullable=True),
        sa.Column("avg_view_time", sa.Float(), nullable=True),
        sa.Column("full_view_rate", sa.Float(), nullable=True),
        # 商品
        sa.Column("pay_gmv", sa.Float(), nullable=True),
        sa.Column("pay_order_cnt", sa.Float(), nullable=True),
        sa.Column("pay_user_cnt", sa.Float(), nullable=True),
        sa.Column("item_click_cnt", sa.Float(), nullable=True),
        sa.Column("item_click_uv", sa.Float(), nullable=True),
        sa.Column("item_click_pv_rate", sa.Float(), nullable=True),
        sa.Column("pay_pv_rate", sa.Float(), nullable=True),
        sa.Column("pay_uv_rate", sa.Float(), nullable=True),
        # 退款 / 引流
        sa.Column("refund_gmv", sa.Float(), nullable=True),
        sa.Column("refund_order_cnt", sa.Float(), nullable=True),
        sa.Column("refund_user_cnt", sa.Float(), nullable=True),
        sa.Column("cart_item_cnt", sa.Float(), nullable=True),
        sa.Column("lead_user_page_view_cnt", sa.Float(), nullable=True),
        sa.Column("lead_user_page_pay_gmv", sa.Float(), nullable=True),
        sa.Column("lead_live_view_cnt", sa.Float(), nullable=True),
        sa.Column("lead_live_pay_gmv", sa.Float(), nullable=True),
        # 平台补充
        sa.Column("browse_num_30d", sa.Integer(), nullable=True),
        # 删除标记 + 元数据
        sa.Column("platform_deleted_at", sa.DateTime(), nullable=True),
        sa.Column("synced_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "user_id",
            "platform_account_id",
            "note_id",
            "stat_date",
            "window",
            name="uq_datacenter_note_date_window",
        ),
    )
    op.create_index(
        "ix_datacenter_account",
        "note_datacenter_stats",
        ["user_id", "platform_account_id"],
    )
    op.create_index("ix_datacenter_note_id", "note_datacenter_stats", ["note_id"])
    op.create_index("ix_datacenter_stat_date", "note_datacenter_stats", ["stat_date"])

    # note_category 退役：删列（分类权威改统计表 note_flag）。
    op.drop_column("notes", "note_category")


def downgrade() -> None:
    # 删列不可逆恢复数据（值已在上游数据脚本里迁进统计表），只恢复结构（空列）。
    op.add_column(
        "notes",
        sa.Column("note_category", sa.String(length=16), nullable=False, server_default=""),
    )
    op.drop_index("ix_datacenter_stat_date", table_name="note_datacenter_stats")
    op.drop_index("ix_datacenter_note_id", table_name="note_datacenter_stats")
    op.drop_index("ix_datacenter_account", table_name="note_datacenter_stats")
    op.drop_table("note_datacenter_stats")
