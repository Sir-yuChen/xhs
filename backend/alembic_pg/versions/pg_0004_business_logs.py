"""PG 镜像：business_logs（设计 64 §2.2，SQLite 链 c0d1e2f3a4b5）

Revision ID: pg_0004_business_logs
Revises: pg_0003_users_email_phone
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "pg_0004_business_logs"
down_revision: str | None = "pg_0003_users_email_phone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("level", sa.String(16), nullable=False, server_default="info"),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False, server_default=""),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("actor_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("workspace", sa.String(32), nullable=True),
        sa.Column("target_type", sa.String(32), nullable=False, server_default=""),
        sa.Column("target_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("summary", sa.String(255), nullable=False, server_default=""),
        sa.Column("detail_json", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_business_logs_event", "business_logs", ["event"])
    op.create_index("ix_business_logs_actor", "business_logs", ["actor_id"])
    op.create_index("ix_business_logs_created", "business_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_business_logs_created", table_name="business_logs")
    op.drop_index("ix_business_logs_actor", table_name="business_logs")
    op.drop_index("ix_business_logs_event", table_name="business_logs")
    op.drop_table("business_logs")
