"""rename cleanup job to queue_record_cleanup (design 13 §35.9 / §31.10, 2026-09-25 用户口径)

Revision ID: c2d3e4f5a6b7
Revises: b2c3d4e5f6g7
Create Date: 2026-09-25

2026-09-25 用户口径（原文）：

> 笔记任务记录清理应扩展为队列详情记录清理，主逻辑不变，只保留最近15天的记录数据，
> 任务名称修改为队列记录清理

**为什么连历史日志一起改**：`scheduled_job_logs.job_key` / `job_name` 是**执行时快照**，
任务中心按它筛选 / 分组。只改任务行不改日志 ⇒ 同一个任务的历史被劈成两半
（旧键的日志在新名称下永远查不到）。快照字段跟着源字段同生共死（与
`k1l2m3n4o5p6` 删 `job_group` 时连日志快照列一起删同一纪律）。

**主逻辑不变**（本迁移只改名，不碰任何语义）：
- 清理范围（`QUEUE_RECORD_TYPES`：三种队列的队列项 + 批次父行 + 账号级同步行）、
  只删终态、保留期 15 天、先子行后父行、按用户收口 —— 全部照旧；
- `scheduled_jobs` 判重键是 `(user_id, job_key)`（DEF-066）⇒ 改了键之后，
  随包种子（`config/initial_data/scheduled_jobs.json`，唯一真相源）会按**新键**
  补行；旧键行由本迁移改名，不会一任务两行。

**apscheduler 不受影响**：store 里的 job id 是 `job_<scheduled_jobs.id>`（行主键，
见 `scheduler_service._apscheduler_job_id`），与 `job_key` 无关 ⇒ 改名不产生孤儿注册。

**降级**：键与中文名改回旧值（纯数据改写，结构无变化）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, None] = "b2c3d4e5f6g7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_KEY = "system.note_task_record_cleanup"
_NEW_KEY = "system.queue_record_cleanup"
_OLD_NAME = "笔记任务记录清理"
_NEW_NAME = "队列记录清理"


def upgrade() -> None:
    # 任务行：键 + 中文名一起改（`name` 是任务中心显示的那一列）
    op.execute(
        f"UPDATE scheduled_jobs SET job_key = '{_NEW_KEY}', name = '{_NEW_NAME}' "
        f"WHERE job_key = '{_OLD_KEY}'"
    )
    # 历史日志快照：同一个任务的历史不该被劈成两半（任务中心按 job_key 筛选）
    op.execute(
        f"UPDATE scheduled_job_logs SET job_key = '{_NEW_KEY}', job_name = '{_NEW_NAME}' "
        f"WHERE job_key = '{_OLD_KEY}'"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE scheduled_jobs SET job_key = '{_OLD_KEY}', name = '{_OLD_NAME}' "
        f"WHERE job_key = '{_NEW_KEY}'"
    )
    op.execute(
        f"UPDATE scheduled_job_logs SET job_key = '{_OLD_KEY}', job_name = '{_OLD_NAME}' "
        f"WHERE job_key = '{_NEW_KEY}'"
    )
