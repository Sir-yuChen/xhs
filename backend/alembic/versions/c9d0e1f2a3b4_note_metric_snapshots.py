"""笔记指标日快照表（设计 17 §13.5.1，M2 方向 3）

2026-10-03 用户口径（原文）：

> 「你一个一个需求做」→ 按二期里程碑顺序推进，M1 已收口，进入 **M2 数据仪表盘（方向 3）**。

## 为什么改

平台侧指标（点赞 / 收藏 / 评论 / 分享 / 阅读）此前**只存在于 `notes.raw_json`**：

- 真机实测（`data/spider_xhs.db`）确认 `raw_json` **确实带这些值** ——
  `likes` / `collects` / `comments` / `shares` / `view_count` 都在；
- 但它是**「最后一次同步时的值」，覆盖写、无历史** ⇒ 趋势类问题一律答不了
  （「发布后 7 天互动增长」「这周哪些笔记在涨」「阅读→互动转化率变化」）。

M2 的仪表盘以**趋势**为核心，没有历史快照就无从谈起 ⇒ 本迁移新建快照表。

## 表设计判据

| 列 | 语义 | 判据 |
|---|---|---|
| `user_id` | 归属用户 | **租户隔离**，趋势查询一律按它收口 |
| `note_pk` | `notes.id`（本地主键） | ⚠️ **不用平台 `note_id`** —— 平台 ID 可能变，本地主键稳定 |
| `stat_date` | 统计日 `YYYY-MM-DD` | 字符串形态：SQLite 按串比大小即日期序，无时区歧义 |
| `likes/collects/comments/shares/views` | 该日**快照值** | 解析不到 ⇒ `0`（与 `_note_metrics` 同口径） |
| `engagement` | 四项互动之和（冗余） | 趋势查询要按它排序/求和，避免每次写表达式 |

⚠️ **唯一键 `(note_pk, stat_date)`**：一天内多次采集**覆盖**同一条（取当天最终值），
不是追加 —— 追加会把趋势线画成锯齿。这是「同日重复采集」的正确语义。

⚠️ **存快照值而非增量**：
- 取某天数据 ⇒ 直接读；
- 算区间增量 ⇒ 区间末 − 区间初；
- **漏采一天仍可算**（只要两端有值），存增量则漏一天就永久算不出那天的存量。

## 回填策略

**不回填**。历史趋势**本来就无从重建**（raw_json 只有最后一版，拿它当「历史某天」
就是**编造数据**，违反硬约束⑤「解析不到就留空，不造假」）。
表从本次迁移起**空表开始**，由采集任务逐日沉淀。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None

_TABLE = "note_metric_snapshots"


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": table},
    ).fetchone()
    return row is not None


def upgrade() -> None:
    conn = op.get_bind()

    # 幂等：已存在则整段跳过（「stamp 回早期 revision 再 upgrade」是正常用法）。
    if _table_exists(conn, _TABLE):
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("note_pk", sa.Integer(), nullable=False),
        sa.Column("stat_date", sa.String(length=10), nullable=False),
        sa.Column("likes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("collects", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comments", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shares", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("views", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("engagement", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["note_pk"], ["notes.id"]),
        sa.PrimaryKeyConstraint("id"),
        # ⚠️ 同日同笔记唯一（覆盖语义，不是追加）—— 见模块 docstring。
        sa.UniqueConstraint("note_pk", "stat_date", name="uq_note_metric_note_date"),
    )
    # 查询形态：① 按用户 + 日期区间取趋势；② 按笔记取单篇时间线。
    op.create_index("ix_note_metric_snapshots_user_id", _TABLE, ["user_id"])
    op.create_index("ix_note_metric_snapshots_note_pk", _TABLE, ["note_pk"])
    op.create_index("ix_note_metric_snapshots_stat_date", _TABLE, ["stat_date"])


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    op.drop_index("ix_note_metric_snapshots_stat_date", table_name=_TABLE)
    op.drop_index("ix_note_metric_snapshots_note_pk", table_name=_TABLE)
    op.drop_index("ix_note_metric_snapshots_user_id", table_name=_TABLE)
    op.drop_table(_TABLE)
