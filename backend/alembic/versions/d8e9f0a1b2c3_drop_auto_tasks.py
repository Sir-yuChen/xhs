"""drop dead table auto_tasks

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2

2026-10-28 用户口径「删」。

`auto_tasks` **0 行**：端点 `/api/auto-tasks`（3 条）与前端 `auto-ops-panel.tsx`
都在，但从未产出过一条数据。它记的「自动运营任务」（keywords / pc_account_id /
creator_account_id / ai_instruction）与 `scheduled_jobs`（调度定义）不是同一件事，
但功能未落地 ⇒ 整条链路退场，不留空表。

同时删：端点 3 条、页面 `auto-ops-panel.tsx`、模型与权限码。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, None] = "c7d8e9f0a1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "auto_tasks"


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE in set(sa.inspect(bind).get_table_names()):
        op.drop_table(TABLE)


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("name", sa.String(128), nullable=False, server_default=""),
            sa.Column("keywords", sa.Text(), nullable=False, server_default=""),
            sa.Column("pc_account_id", sa.Integer(), nullable=True),
            sa.Column("creator_account_id", sa.Integer(), nullable=True),
            sa.Column("ai_instruction", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="paused"),
            sa.Column("schedule_job_id", sa.Integer(), nullable=True),
            sa.Column("last_run_at", sa.DateTime(), nullable=True),
            sa.Column("total_published", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
