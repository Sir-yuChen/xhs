"""drop queue batch rows; promote children to top-level records (2026-09-26 常驻化)

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-09-26

2026-09-26 用户口径（原文）：

> 不需要队列批次，队列是常驻，只需要队列详情记录即可

**批次父行整层移除**（代码侧同轮改完：`note_queue_service` 不再有
`ensure_open_batch` / 批次类型 / `parent_task_id` 挂载）：

* `xhs_note_sync_batch` / `xhs_note_publish_batch` / `xhs_note_gen_batch` 三类
  **批次行全部删除**；
* 它们的**子行（队列项）升为顶级记录**（`parent_task_id` 置 NULL）—— 子行才是
  「哪一篇 / 哪一个商品跑成什么样」的详情记录，历史不丢；`pending` / `running`
  的活项一并升级（队列常驻，父行消失不影响 worker 认领 —— 认领只认 task_type）。

**顺序不可换**：先摘子行的 `parent_task_id` 再删父行（`tasks.parent_task_id` 是
自引用外键，父行还被引用时删它会违反 FK）。

**可逆性**：**不可逆** —— 批次父行删后无法重建（它是「那一轮」的记账行，轮次信息
随常驻化一起作废）。`downgrade()` 为文档化 no-op：本仓调版本一律用
`command.upgrade(rev)`（见 REF §F），不用 downgrade。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 三类批次父行（与移除前的 `note_queue_service` 批次常量一致；迁移不 import 业务模块）。
_BATCH_TASK_TYPES = (
    "xhs_note_sync_batch",
    "xhs_note_publish_batch",
    "xhs_note_gen_batch",
)

_TYPES_SQL = ", ".join(f"'{t}'" for t in _BATCH_TASK_TYPES)


def _has_tasks_table(conn) -> bool:
    """表存在性守卫：迁移测试的夹具库是「链中 stamp + 手建部分表」，缺表不该让迁移炸。"""
    return (
        conn.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tasks'")
        ).first()
        is not None
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_tasks_table(conn):
        return
    # 1) 子行升为顶级记录（先摘引用，再删父行 —— 自引用 FK 的顺序）
    conn.execute(
        sa.text(
            f"UPDATE tasks SET parent_task_id = NULL "
            f"WHERE parent_task_id IN (SELECT id FROM tasks WHERE task_type IN ({_TYPES_SQL}))"
        )
    )
    # 2) 删批次父行
    conn.execute(sa.text(f"DELETE FROM tasks WHERE task_type IN ({_TYPES_SQL})"))


def downgrade() -> None:
    # 不可逆（见模块 docstring）：批次行删了无法重建，轮次记账随常驻化作废。
    # 刻意 no-op 而不是抛异常 —— 本仓调版本只用 upgrade（REF §F）。
    return None
