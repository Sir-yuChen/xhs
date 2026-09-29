"""add task_id to scheduled_job_logs (design 15 §15)

Revision ID: k2l3m4n5o6p7
Revises: j1k2l3m4n5o6
Create Date: 2026-09-27

2026-09-27 用户口径「再次分析账号笔记同步任务是否真实还在执行」的排查结论：**日志与业务
任务行无法精确关联**。`run_targeted_job` 明明**收得到** `task_id`（账号卡端点传进来的），
但表里**没有这一列** ⇒ 只能靠「同秒启动」猜（实测 `log=14821` 与它要观测的 `task=51`
在重启重跑后 `started_at` 差了近 2 小时，猜法直接失效）。

后果是实测到的这一种：**进程重启把观测线程带走** ⇒ 日志永远停在 `running`，
而任务行早已 `done` ⇒ 账号卡显示「同步中」、任务其实结束了（用户报障原话）。

本迁移只加**可空**列 + 索引（收口孤儿日志按它精确回查任务行）：

* **不加外键**：日志是历史快照（与 `user_id` / `account_id` 同理由 —— 任务行被记录清理
  删掉后，日志仍要能回答「当时是哪次执行」）。
* **存量行为 NULL** ⇒ 缺值口径：孤儿收口时回落到「按账号取最近一次终态任务」，不造假。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "k2l3m4n5o6p7"
down_revision: Union[str, None] = "j1k2l3m4n5o6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """加 `task_id` 列 + 索引（**幂等**，理由同 `j1k2l3m4n5o6`：迁移用例会 stamp 回父版本再 upgrade）。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "scheduled_job_logs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("scheduled_job_logs")}
    if "task_id" in columns:
        return
    with op.batch_alter_table("scheduled_job_logs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("task_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_scheduled_job_logs_task_id", ["task_id"])


def downgrade() -> None:
    """去掉列 + 索引（**幂等**，理由同 `upgrade`）。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "scheduled_job_logs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("scheduled_job_logs")}
    if "task_id" not in columns:
        return
    with op.batch_alter_table("scheduled_job_logs", schema=None) as batch_op:
        batch_op.drop_index("ix_scheduled_job_logs_task_id")
        batch_op.drop_column("task_id")
