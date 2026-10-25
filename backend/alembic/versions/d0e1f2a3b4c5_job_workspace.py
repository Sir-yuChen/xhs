"""定时任务工作区归属列（设计 64 §3.3）

2026-10-25 用户口径「分析定时任务模块是否拆分还是每个工作区单独」⇒
**决策：公共单调度器保留（不拆分）**，行级 `user_id` 管权限面，新增 `workspace`
列管归属展示 —— 系统管理「定时任务」总览按工作区聚合（admin）。

回填口径（与 `job_registry.workspace_for_job` 同一映射的内联快照）：
`system.queue_record_cleanup` ⇒ `system`；其余已知 job_key ⇒ `xhs`。
未知键（理论不会有 —— job_key 是注册表白名单）留空，不猜（硬约束⑤）。

幂等：是。可逆：置空该列（归属是展示元数据，删了不丢功能）。

Revision ID: d0e1f2a3b4c5
Revises: c0d1e2f3a4b5
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d0e1f2a3b4c5"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None

_SYSTEM_KEYS = ("system.queue_record_cleanup",)


def upgrade() -> None:
    op.add_column("scheduled_jobs", sa.Column("workspace", sa.String(32), nullable=True))
    op.create_index("ix_scheduled_jobs_workspace", "scheduled_jobs", ["workspace"])
    for key in _SYSTEM_KEYS:
        op.execute(
            sa.text("UPDATE scheduled_jobs SET workspace = 'system' WHERE job_key = :k").bindparams(k=key)
        )
    op.execute(
        sa.text(
            "UPDATE scheduled_jobs SET workspace = 'xhs' "
            "WHERE workspace IS NULL AND job_key IS NOT NULL AND job_key <> ''"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_jobs_workspace", table_name="scheduled_jobs")
    op.drop_column("scheduled_jobs", "workspace")
