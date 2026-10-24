"""backfill tasks.payload.source for queue records (design 13 §35.9 / §35.11, 2026-09-26 用户口径)

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-09-26

2026-09-26 用户口径（原文）：

> 来源列不能是未知，你排查所有来源后给每个来源固定值

**问题**：`note_queue_service.enqueue_item` / `ensure_open_batch` 入队时**不写**
`payload.source` ⇒ 队列记录页（设计 13 §35.11）的来源列对全部队列记录只能显示
「未知」，用户分不清一条记录是自己点出来的还是系统自己跑的（账号级同步行
`xhs_note_sync` / 商品同步行一直在写 `source`，队列六类型是缺口）。

**修复分两半**（本迁移是第二半，代码那一半见 `note_queue_service` 的
`SOURCE_*` 常量与 `enqueue_item(..., source=...)`）：

1. 代码：入队即写 `source`（默认 `manual`；重试标 `retry`；启动恢复标
   `recovery`；定时入口预留 `scheduled`）；
2. 数据（本迁移）：存量队列行补 `source = 'manual'`。

**为什么存量补 `manual` 不是猜**：队列六类型（同步 / 发布 / 商品生成各自的
队列项与批次父行）的**全部现存入口都是页面按钮**（单条 / 批量同步、发布、
生成）—— 代码里没有任何定时任务走队列入队（`run_scheduled_note_syncs` 只建
账号级 `xhs_note_sync` 行，那不是队列记录）。因此存量缺 source 的队列行
**只能**是用户手点出来的。

**幂等**：只补 `source` 缺失的行（`json_extract(...) IS NULL`），重复执行无副作用。

**降级**：摘掉这六类行上的 `source` 键（回到迁移前形态）。纯数据、不动结构。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

from backend.app.core.migration_introspect import table_exists

revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 队列六类型（与 `note_queue_service.QUEUE_REGISTRY` 派生一致；这里写死是因为
#: 迁移必须能在**任意代码版本**上跑，不 import 业务模块）。
_QUEUE_TASK_TYPES = (
    "xhs_note_sync_item",
    "xhs_note_publish_item",
    "xhs_note_gen_item",
    "xhs_note_sync_batch",
    "xhs_note_publish_batch",
    "xhs_note_gen_batch",
)

_TYPES_SQL = ", ".join(f"'{t}'" for t in _QUEUE_TASK_TYPES)


def upgrade() -> None:
    conn = op.get_bind()
    # ⚠️ 表存在性守卫：迁移测试的夹具库是「链中 stamp + 手建部分表」（缺 `tasks`
    # 也不稀奇），真实库则一定有。没有这张表 = 没有可回填的行，安静跳过即可 ——
    # 数据迁移不该因为「没数据可搬」而炸。
    #: v1.1.0 PG 化：原为内联 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    has_tasks = table_exists(conn, "tasks")
    if not has_tasks:
        return
    # 只补缺 source 的行；payload 为 NULL 的（不该有，防御性）落一个最小 JSON。
    # v1.1.0 PG 化（2026-10-24）：原为 SQLite JSON1 的 json_set / json_extract。
    # PG 等价物是 jsonb 函数族 —— `payload` 列是 sa.JSON（PG 渲染为 json 类型），
    # jsonb 函数要求 jsonb 入参 ⇒ 显式 `::jsonb` 进、`::json` 出。
    # 「键缺失」判据：json_extract(...) IS NULL ⇔ `payload::jsonb ->> 'source' IS NULL`。
    conn.execute(
        sa.text(
            f"UPDATE tasks SET payload = jsonb_set("
            f"COALESCE(payload::jsonb, '{{}}'::jsonb), '{{source}}', '\"manual\"'::jsonb)::json "
            f"WHERE task_type IN ({_TYPES_SQL}) "
            f"AND (payload IS NULL OR payload::jsonb ->> 'source' IS NULL)"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    #: v1.1.0 PG 化：原为内联 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    has_tasks = table_exists(conn, "tasks")
    if not has_tasks:
        return
    #: v1.1.0 PG 化：json_remove ⇔ jsonb 的 `-` 删除键操作符。
    conn.execute(
        sa.text(
            f"UPDATE tasks SET payload = (payload::jsonb - 'source')::json "
            f"WHERE task_type IN ({_TYPES_SQL})"
        )
    )
