"""PG 镜像：scheduled_jobs.workspace（设计 64 §3.3，SQLite 链 d0e1f2a3b4c5）

Revision ID: pg_0006_job_workspace
Revises: pg_0005_settings_three_scopes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "pg_0006_job_workspace"
down_revision: str | None = "pg_0005_settings_three_scopes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scheduled_jobs", sa.Column("workspace", sa.String(32), nullable=True))
    op.create_index("ix_scheduled_jobs_workspace", "scheduled_jobs", ["workspace"])
    op.execute(sa.text("UPDATE scheduled_jobs SET workspace = 'system' WHERE job_key = 'system.queue_record_cleanup'"))
    op.execute(
        sa.text("UPDATE scheduled_jobs SET workspace = 'xhs' WHERE workspace IS NULL AND job_key IS NOT NULL AND job_key <> ''")
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_jobs_workspace", table_name="scheduled_jobs")
    op.drop_column("scheduled_jobs", "workspace")
