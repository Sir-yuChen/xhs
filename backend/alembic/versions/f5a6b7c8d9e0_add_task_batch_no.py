"""add tasks.batch_no (queue batch parent rows carry a unique batch number)

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-09-26

2026-09-26 用户口径（原文）：

> 将队列执行详情表使用批次+详情父子树风格展示，批次逻辑不再移除，生成唯一批次号

**批次层回来了，但形态与淘汰的那版不同**（`e4f5a6b7c8d9` 曾把批次父行整层删除）：

* 那次删的是「**轮次**记账」（`ensure_open_batch` 复用同一个未闭合批次行，导致
  「点一次单条也冒出一个批次」）；这次要的是「**一次入队动作 = 一个批次**」的
  记账行，用户口径是「父子树展示 + 唯一批次号」；
* 批次行仍是 `tasks` 表的一行（父行，`parent_task_id IS NULL`），队列项是它的
  子行（`parent_task_id = 批次行.id`）—— 复用既有自引用外键（`8c7dceca1a6a` 加的）；
* **批次号落成一个真列**（`tasks.batch_no`）而不是塞进 `payload`：批次号要
  「**唯一**」，就必须能在 SQL 层做唯一约束 —— 塞进 JSON 只能靠应用层扫表自证，
  并发下挡不住重号（安全网不能条件性）。

⚠️ **存量行不补批次**：被 `e4f5a6b7c8d9` 摘成顶级记录的队列项（`parent_task_id`
为 NULL）不重建父行 —— 批次号是「当时那一次动作」的事实，事后编一个批次号给它
就是造假（硬约束⑤）。记录页把这类行按**顶级单条**渲染。

⚠️ **可逆性**：`downgrade()` 丢弃列 + 唯一索引（行本身与父子关系不动）。本仓调
版本一律 `command.upgrade(rev)`（REF §F），downgrade 只为迁移链完整。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

from backend.app.core.migration_introspect import column_names, table_exists

revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, None] = "e4f5a6b7c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 唯一索引名（应用层按名字查重、测试按名字断言；改名前先想清楚谁在读它）。
_INDEX_NAME = "ux_tasks_batch_no"


def _has_tasks_table(conn) -> bool:
    """表存在性守卫：迁移测试的夹具库是「链中 stamp + 手建部分表」，缺表不该让迁移炸。"""
    #: v1.1.0 PG 化：原为 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return table_exists(conn, "tasks")


def _has_batch_no_column(conn) -> bool:
    """列存在性守卫：新库由 `create_all` 直接建全列，迁移不该再 add 一次（会报重复列）。"""
    #: v1.1.0 PG 化：原为 `PRAGMA table_info`（SQLite 专属），统一走内省助手。
    return "batch_no" in column_names(conn, "tasks")


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_tasks_table(conn):
        return
    if not _has_batch_no_column(conn):
        op.add_column("tasks", sa.Column("batch_no", sa.String(length=32), nullable=True))
    # 唯一索引：`batch_no` 为 NULL 的普通行（绝大多数）不受约束（SQLite / PG 同语义：
    # NULL 互不相等），只有真批次行必须两两不同。
    op.create_index(_INDEX_NAME, "tasks", ["batch_no"], unique=True)


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_tasks_table(conn):
        return
    op.drop_index(_INDEX_NAME, table_name="tasks")
    if _has_batch_no_column(conn):
        op.drop_column("tasks", "batch_no")
