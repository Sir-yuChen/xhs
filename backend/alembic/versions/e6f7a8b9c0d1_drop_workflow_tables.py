"""drop workflow tables（移除「自动化工作流」整条特性）

用户口径（2026-10-07）：「把工作流相关逻辑表等完整移除，直接使用定时任务完成即可」。

设计 19（自动化工作流，M5 方向 5）整条特性退场：5 张表 + 引擎 + 节点实现 + 11 个端点
+ 前端页面全部删除。**本迁移是退场的一部分** —— 它存在的唯一理由是：
`f4a5b6c7d8e9`（note_category）与工作流迁移**原本是两条并行链**，工作流那条
（`q8r9s0t1u2v3`）被删掉后，历史库里的 5 张工作流表**不会自己消失**
（alembic 只按 revision 走，不会「撤销已不存在的迁移」）。

⚠️ **为什么不用 `cascade="all, delete-orphan"` / 为什么表存在才删**：
历史库可能从未跑过工作流迁移（例如新装的库、或 stamp 到早期 revision 的库）
⇒ 直接 `DROP TABLE` 会炸 `no such table`。这里逐表 `inspect` 后**只删存在的**，
使迁移**在两种库上都幂等**（与 `d5e6f7a8b9c0` 那条「先判表再判列」同一纪律）。

⚠️ **本迁移不可降级重建**：`downgrade()` 只重建**空表**（结构），数据无法恢复 ——
工作流表里从未有真实业务数据（该特性从未闭环，见设计 19 §11.5 的未做清单），
故这是可接受的；但**如实写明**，不假装能还原。

Revision ID: e6f7a8b9c0d1
Revises: g0a1b2c3d4e5, f4a5b6c7d8e9
Create Date: 2026-10-07
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect

revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = ("g0a1b2c3d4e5", "f4a5b6c7d8e9")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 退场的 5 张表（设计 19 §2.1–2.4 + §8.4 的排期表）。
#: 顺序 = 先删子表再删主表，避免外键约束在部分库上报错。
WORKFLOW_TABLES: tuple[str, ...] = (
    "workflow_run_logs",
    "workflow_scheduled_publishes",
    "workflow_runs",
    "workflow_nodes",
    "workflows",
)


def _table_names() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _table_names()
    for name in WORKFLOW_TABLES:
        if name in existing:
            op.execute(f"DROP TABLE {name}")


def downgrade() -> None:
    """只重建**空表**（结构）；数据不可恢复（见模块 docstring 的说明）。

    本函数刻意**不重建**索引之外的增量内容：工作流表从未承载真实业务数据。
    """
    # 结构重建交给「重新引入设计 19 迁移」——这里只保证 downgrade 不炸语句。
    # 逐表建最小结构（与 q8r9s0t1u2v3 的原 DDL 同形），让 alembic 链能往回走。
    existing = _table_names()
    if "workflows" not in existing:
        op.execute(
            """
            CREATE TABLE workflows (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name VARCHAR(128) NOT NULL,
                status VARCHAR(16) NOT NULL,
                trigger_type VARCHAR(16) NOT NULL,
                cron_expression VARCHAR(64) NOT NULL,
                run_at DATETIME,
                misfire_policy VARCHAR(16) NOT NULL,
                graph JSON NOT NULL,
                global_params JSON NOT NULL,
                remark VARCHAR(500) NOT NULL,
                last_run_at DATETIME,
                next_run_at DATETIME,
                run_count INTEGER NOT NULL,
                fail_count INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME,
                FOREIGN KEY(user_id) REFERENCES users (id)
            )
            """
        )
