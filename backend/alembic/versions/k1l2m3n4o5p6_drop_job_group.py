"""drop job_group from scheduled_jobs / scheduled_job_logs (design 15 §4.1 / §4.2, R20/R22)

Revision ID: k1l2m3n4o5p6
Revises: h8i9j0k1l2m3
Create Date: 2026-09-24

2026-09-24 用户口径（R20 / R22，本轮由用户拍板**物理删除**）：

> 移除，分组：默认分组；调用目标，来自代码侧注册表白名单（设计 15 §5），新增类型需要发版，移除定时任务的分组
> 任务不再分组，分组字段移除，相关逻辑移除
> R20/R22 只去 UI，job_group 列保留；若要物理删列须另起迁移并同步清日志快照字段 —— **数据库物理删除**

**做法**：
- `scheduled_jobs.job_group` 整列删除（SQLite batch）；
- `scheduled_job_logs.job_group`（执行时快照）整列删除 —— 快照字段与源列同生共死，
  留着就是「一个永远指向已删列的历史副本」（没有源就没有快照的意义）；
- `ix_scheduled_jobs_job_group`（由更早的 `y8z9a0b1c2d3` 建立）**必须显式先 drop**：
  batch 模式重建表时会**反射现有索引并原样重建**，列已 drop 就变成
  `CREATE INDEX ... (job_group)` ⇒ `no such column: job_group` 直接炸。
  （实跑踩过，见 docs/04 台账本轮记录。）

**降级**：两列都以 `server_default='DEFAULT'` 恢复（结构可逆，但**历史分组取值不可逆还原** ——
列删了就没了，与 `h8i9j0k1l2m3` 的 interval→cron 同口径：只恢复结构，不还原语义）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "k1l2m3n4o5p6"
down_revision: Union[str, None] = "h8i9j0k1l2m3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- 两张表的 job_group 一起退（源列 + 快照列）----
    # ⚠️ 必须先 drop_index：batch 重建表会反射已有索引并重建，列没了就炸（实跑踩过）。
    with op.batch_alter_table("scheduled_jobs", schema=None) as batch_op:
        batch_op.drop_index("ix_scheduled_jobs_job_group")
        batch_op.drop_column("job_group")

    with op.batch_alter_table("scheduled_job_logs", schema=None) as batch_op:
        batch_op.drop_column("job_group")


def downgrade() -> None:
    with op.batch_alter_table("scheduled_jobs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("job_group", sa.String(length=32), nullable=False, server_default="DEFAULT")
        )
        batch_op.create_index("ix_scheduled_jobs_job_group", ["job_group"])

    with op.batch_alter_table("scheduled_job_logs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("job_group", sa.String(length=32), nullable=False, server_default="DEFAULT")
        )
