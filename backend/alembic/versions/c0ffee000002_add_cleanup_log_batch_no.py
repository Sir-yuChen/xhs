"""add no_traffic_cleanup_logs.batch_no (2026-10-13)

Revision ID: c0ffee000002
Revises: c0ffee000001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c0ffee000002"
down_revision = "c0ffee000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "no_traffic_cleanup_logs",
        sa.Column("batch_no", sa.String(64), nullable=False, server_default=""),
    )
    op.create_index("ix_no_traffic_cleanup_logs_batch_no", "no_traffic_cleanup_logs", ["batch_no"])


def downgrade() -> None:
    op.drop_index("ix_no_traffic_cleanup_logs_batch_no", table_name="no_traffic_cleanup_logs")
    op.drop_column("no_traffic_cleanup_logs", "batch_no")
