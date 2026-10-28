"""删除关键词组表

「关键词组」页面及其全部后端逻辑已退场（用户 2026-10-28 口径），故 drop
`keyword_groups`。话题体系（`topics` / `note_topics`）**保留不动**。

Revision ID: z2a3b4c5d6e7
Revises: y1z2a3b4c5d6
Create Date: 2026-10-28
"""

from alembic import op
import sqlalchemy as sa

revision = "z2a3b4c5d6e7"
down_revision = "y1z2a3b4c5d6"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                "select 1 from information_schema.tables "
                "where table_schema = 'public' and table_name = :t"
            ),
            {"t": table},
        ).first()
    )


def upgrade() -> None:
    if _table_exists("keyword_groups"):
        op.drop_table("keyword_groups")


def downgrade() -> None:
    if not _table_exists("keyword_groups"):
        op.create_table(
            "keyword_groups",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("platform", sa.String(length=32), nullable=False, server_default="xhs"),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("keywords", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_keyword_groups_user_id", "keyword_groups", ["user_id"])
