"""增量同步游标字段（设计 13 §17.9 / D13）

Revision ID: c3d4e5f6a7b8
Revises: a1b2c3d4e5f6
Create Date: 2026-09-19

三个游标列都存**毫秒时间戳**（BigInteger），全部 ``NOT NULL DEFAULT 0``：
``0 = 未同步过``，下一轮自动走全量。游标只在「本次同步落库成功」之后推进
（D13），所以列的值永远是「已经安全落库的位置」。
"""
from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b8"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # platform_accounts: 笔记增量游标
    op.add_column(
        "platform_accounts",
        sa.Column(
            "note_sync_cursor_ms",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "platform_accounts",
        sa.Column(
            "note_sync_cursor_note_id",
            sa.String(64),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.create_index(
        "ix_platform_accounts_note_sync_cursor_note_id",
        "platform_accounts",
        ["note_sync_cursor_note_id"],
    )

    # shop_account_bindings: 商品增量游标
    op.add_column(
        "shop_account_bindings",
        sa.Column(
            "goods_cursor_ms",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("shop_account_bindings", "goods_cursor_ms")
    op.drop_index(
        "ix_platform_accounts_note_sync_cursor_note_id",
        table_name="platform_accounts",
    )
    op.drop_column("platform_accounts", "note_sync_cursor_note_id")
    op.drop_column("platform_accounts", "note_sync_cursor_ms")
