"""add config_snapshot to scheduled_job_logs (design 15 §4.2)

Revision ID: z9a0b1c2d3e4
Revises: y8z9a0b1c2d3
Create Date: 2026-09-24

2026-09-24 用户口径：任务日志的详情弹窗要能看到**任务执行那一刻的配置快照**
（任务行后来被改 / 删都不影响历史日志可读）。快照由执行包装器在插日志行时
就地写入（`scheduled_job_runner._new_log_row`）。

本迁移只加可空列；新增列之前的存量执行为 NULL —— 前端按缺值口径展示
（不造假，不补一个"看起来像当时"的配置）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "z9a0b1c2d3e4"
down_revision: Union[str, None] = "y8z9a0b1c2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("scheduled_job_logs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("config_snapshot", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scheduled_job_logs", schema=None) as batch_op:
        batch_op.drop_column("config_snapshot")
