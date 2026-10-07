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
    """加 `job_trigger_type` 列 + 索引。

    ⚠️ **必须幂等**（2026-10-07 补；与 `j1k2l3m4n5o6` / `k2l3m4n5o6p7` / `c9d0e1f2a3b4`
    ／`f2a3b4c5d6e7` 等同规制）：迁移用例与**真库**都有「表已由 `create_all()` 建好、
    列已在、只是 revision 还没走到」的形态（本机真库就是：`scheduled_job_logs.job_trigger_type`
    已存在，而 `alembic_version` 停在 `f4a5b6c7d8e9`）。无守卫 ⇒
    `duplicate column name: job_trigger_type`，整个 `upgrade head` 当场失败
    —— 而这条迁移在**两条链的交汇处**，失败会连带挡住后面所有迁移。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "scheduled_job_logs" not in inspector.get_table_names():
        return  # 表都没有 ⇒ 无需加列
    columns = {c["name"] for c in inspector.get_columns("scheduled_job_logs")}
    if "job_trigger_type" not in columns:
        with op.batch_alter_table("scheduled_job_logs", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("job_trigger_type", sa.String(length=16), nullable=False, server_default="")
            )
    indexes = {i["name"] for i in inspector.get_indexes("scheduled_job_logs")}
    if "ix_scheduled_job_logs_job_trigger_type" not in indexes:
        op.create_index(
            "ix_scheduled_job_logs_job_trigger_type", "scheduled_job_logs", ["job_trigger_type"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "scheduled_job_logs" not in inspector.get_table_names():
        return
    indexes = {i["name"] for i in inspector.get_indexes("scheduled_job_logs")}
    if "ix_scheduled_job_logs_job_trigger_type" in indexes:
        op.drop_index("ix_scheduled_job_logs_job_trigger_type", "scheduled_job_logs")
    columns = {c["name"] for c in inspector.get_columns("scheduled_job_logs")}
    if "job_trigger_type" in columns:
        with op.batch_alter_table("scheduled_job_logs", schema=None) as batch_op:
            batch_op.drop_column("job_trigger_type")
