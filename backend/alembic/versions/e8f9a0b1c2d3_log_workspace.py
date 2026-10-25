"""scheduled_job_logs 加 workspace 列（设计 69 两级任务中心）

Revision ID: e8f9a0b1c2d3
Revises: a9b8c7d6e5f4
Create Date: 2026-10-25

## 为什么需要这一列

任务中心已分两级（系统级在系统管理 / 工作区级在各自工作区）。
工作区任务页必须**只显示本工作区的执行日志**，而日志列表是分页的 ——
前端取全量再过滤会让页码错乱（「本页 20 条里只有 3 条属于本工作区」）。
因此筛选必须下推到 SQL。

不能 join `scheduled_jobs` 取 workspace：日志是**历史快照**，
任务定义被删后（`scheduled_job_logs.job_id` 可空、无强制外键），
历史日志仍要能回答「当时属于哪个工作区」。既有 `job_name` / `job_key` /
`job_trigger_type` 全是快照列，本列沿用同一口径。

## 回填策略（缺值口径：不猜）

按 `job_key` 关联**当前**定义取其 `workspace`；查不到的留 NULL。
下次执行时写入真实值（写入侧在 `scheduled_job_runner`）。

⚠️ 不用 `DEFAULT 'system'` 之类的兜底值：那会把小红书任务的历史日志
错误归到系统级，用户在工作区页就看不到自己的历史记录 —— 静默错分类
比留空更糟（留空至少不会出现在别人的页面上）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e8f9a0b1c2d3"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scheduled_job_logs",
        sa.Column("workspace", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_scheduled_job_logs_workspace",
        "scheduled_job_logs",
        ["workspace"],
        unique=False,
    )
    #: 回填：按 job_key 关联当前定义。查不到的定义留 NULL（不猜）。
    op.execute(
        """
        UPDATE scheduled_job_logs AS l
           SET workspace = j.workspace
          FROM scheduled_jobs AS j
         WHERE l.job_key = j.job_key
           AND l.workspace IS NULL
           AND j.workspace IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_job_logs_workspace", table_name="scheduled_job_logs")
    op.drop_column("scheduled_job_logs", "workspace")
