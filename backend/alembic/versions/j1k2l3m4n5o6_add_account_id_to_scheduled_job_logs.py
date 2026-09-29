"""add account_id to scheduled_job_logs (design 15 §4.2 / design 13 §35.17.5)

Revision ID: j1k2l3m4n5o6
Revises: i0j1k2l3m4n5
Create Date: 2026-09-27

2026-09-27 用户口径：「账号信息卡的判断逻辑应只需要关注**最新一条**的同步笔记任务
日志的状态即可」—— 卡片判据从「`tasks` 行状态」改为「`scheduled_job_logs` 状态」
（用户原话还有「根据任务日志详情的状态判断终态即可」「只有任务日志状态在『正在执行』，
才展示同步中」）。

**前置缺口**：日志行**不记账号**（本表原先没有 `account_id` 列）⇒ 按账号取「最新一条」
无从下手。此前唯一的关联路径是 `task_id`，但账号卡路径上那个字段同样是空的
（`run_targeted_job` 建日志时不写它）⇒ 两条路都断。

本迁移只加**可空**列 + 索引（取「最新一条」走 `user_id` + `account_id` 过滤后按 id 倒序）：

* **不加外键**：日志是**历史快照**（与 `user_id` 列的注释同风格 —— 「任务删了历史日志
  仍能按用户收口」）。加 FK 会让「删除账号」被历史日志顶住，而日志要的恰恰是
  「账号当时是谁」这个**不可变事实**。
* **存量行为 NULL** ⇒ 按缺值口径处理：**不参与**「最新一条」的候选（卡片回落到既有
  `tasks` 行判据），**不造一个假账号**（硬约束⑤）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "j1k2l3m4n5o6"
down_revision: Union[str, None] = "i0j1k2l3m4n5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """加 `account_id` 列 + 索引（**幂等**）。

    ⚠️ 幂等是必需的而不是锦上添花：迁移用例的「④ 幂等」一段会把版本 `stamp` 回父
    revision 再 `upgrade head`，重复执行本迁移。`batch_alter_table` 的加列**不幂等**
    （SQLite 上重建表 + 再次 ADD COLUMN 会直接报 duplicate column）⇒ 必须自己先探。
    表不存在的分支同理：迁移用例用**最小夹具**（只建它要碰的表），真实库里这张表当然
    一定在，但「夹具缺表」不该红在**迁移**上（那是夹具的问题，不是被测行为坏了）。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "scheduled_job_logs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("scheduled_job_logs")}
    if "account_id" in columns:
        return
    with op.batch_alter_table("scheduled_job_logs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("account_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            "ix_scheduled_job_logs_account_id", ["account_id"]
        )


def downgrade() -> None:
    """去掉列 + 索引（**幂等**，理由同 `upgrade`）。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "scheduled_job_logs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("scheduled_job_logs")}
    if "account_id" not in columns:
        return
    with op.batch_alter_table("scheduled_job_logs", schema=None) as batch_op:
        batch_op.drop_index("ix_scheduled_job_logs_account_id")
        batch_op.drop_column("account_id")
