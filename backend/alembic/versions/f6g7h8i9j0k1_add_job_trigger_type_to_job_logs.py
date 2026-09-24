"""add job_trigger_type snapshot to scheduled_job_logs (design 15 §4.2)

Revision ID: f6g7h8i9j0k1
Revises: z9a0b1c2d3e4
Create Date: 2026-09-24

2026-09-24 用户口径：任务日志按**任务类型**筛选（下拉框默认「全部任务」）。

筛选依据是日志行自带的 `job_trigger_type` 快照列（与 job_name/job_key 同一套
快照模式），**不 join scheduled_jobs** —— 任务删除后它的历史日志也必须还能
按类型筛出来。本列新增之前的存量日志类型为空串：不进任何类型筛选结果
（不猜、不补，缺值口径），「全部任务」下仍可见。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f6g7h8i9j0k1"
down_revision: Union[str, None] = "z9a0b1c2d3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("scheduled_job_logs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("job_trigger_type", sa.String(length=16), nullable=False, server_default="")
        )
    op.create_index("ix_scheduled_job_logs_job_trigger_type", "scheduled_job_logs", ["job_trigger_type"])


def downgrade() -> None:
    op.drop_index("ix_scheduled_job_logs_job_trigger_type", "scheduled_job_logs")
    with op.batch_alter_table("scheduled_job_logs", schema=None) as batch_op:
        batch_op.drop_column("job_trigger_type")
