"""no_traffic_cleanup_items: 无流量清理的**笔记维度**明细 + 批次行语义更正

Revision ID: e3f4a5b6c7d8
Revises: h1i2j3k4l5m6
Create Date: 2026-10-06

## 为什么（用户 2026-10-06 两条口径）

> 「清理无流量笔记失败，分析原因：候选池仅包含已同步千帆挂链映射的笔记；
>  未同步/未绑定千帆的笔记不在池内」
> 「清理无流量笔记 · 操作日志 展示也有问题，清理无流量的主要对象是笔记，
>  所有主体应是笔记维度，记录操作日志时也应该是笔记维度，一起修复」

① **日志主体改笔记维度**：批次行答「这次整体怎么样」，但用户真正要问的是
「**这一篇**被删了没有、没删是为什么」—— 那个问题此前只能从 `details` JSON 列里
自己解析，既查不了（无索引）也没在界面上渲染过。新表 `no_traffic_cleanup_items`
把逐篇处置事实升级为**一行一篇笔记**，可按笔记 ID / 账号 / 结果 / 时间检索。

② **`reason` 语义更正**（DEF-095）：批次行的 `reason` 此前被写成**候选池覆盖度
文案**（`_finish(note=coverage["reason"])`）⇒ 平台风控失败时，界面「失败原因」列显示
「候选池仅包含已同步千帆挂链映射的笔记…」，把用户指向完全错误的方向。新增
`coverage_note` 承载那句文案，`reason` 归还给「本次失败原因」。

③ **`aborted` / `abort_reason`**（DEF-097）：平台风控（`code=906`）时本批应**中止**，
不再朝一个已被风控告警的账号连打剩余 19 次写前置请求。中止事实要如实落库。

## 兼容性

* 纯新增表 + `no_traffic_cleanup_logs` 加三列（`coverage_note` / `aborted` /
  `abort_reason`），**不动任何存量列**。
* **存量行不回填 `coverage_note`**：历史行的 `reason` 里那句文案就是当时的覆盖度
  说明，回填等于把「历史失败原因」整列改写成另一件事 —— 用户读历史记录时会看到
  「昨天说账号风控，今天变成了候选池边界」。历史就让它保持原样（那时它确实是那么
  写的），新行按新语义写。
* `downgrade` 直接 drop 新表 + drop 三列。

时间列与全库一致用朴素 datetime（`core/time.py::shanghai_now` 口径）。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from backend.app.core.migration_introspect import column_names, table_exists

revision: str = "e3f4a5b6c7d8"
down_revision: Union[str, Sequence[str], None] = "h1i2j3k4l5m6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BATCH_TABLE = "no_traffic_cleanup_logs"
ITEM_TABLE = "no_traffic_cleanup_items"


def _columns(conn, table: str) -> set[str]:
    """列清单（最小夹具库上列可能不全 —— 同 `d2e3f4a5b6c7` 的教训）。"""
    #: v1.1.0 PG 化：原为 `PRAGMA table_info`（SQLite 专属），统一走内省助手。
    return column_names(conn, table)


def _has_table(conn, name: str) -> bool:
    #: v1.1.0 PG 化：原为 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return table_exists(conn, name)


def upgrade() -> None:
    conn = op.get_bind()

    # ---------- ① 批次行补三列（幂等：重复跑 / 手工加过就跳过） ----------
    if _has_table(conn, BATCH_TABLE):
        cols = _columns(conn, BATCH_TABLE)
        if "coverage_note" not in cols:
            op.add_column(
                BATCH_TABLE,
                sa.Column("coverage_note", sa.Text(), nullable=True, server_default=""),
            )
        if "aborted" not in cols:
            op.add_column(
                BATCH_TABLE,
                # v1.1.0 PG 化：布尔列默认值写 sa.false()（原 `"0"` 是 SQLite 写法）。
                sa.Column("aborted", sa.Boolean(), nullable=True, server_default=sa.false()),
            )
        if "abort_reason" not in cols:
            op.add_column(
                BATCH_TABLE,
                sa.Column("abort_reason", sa.Text(), nullable=True, server_default=""),
            )

    # ---------- ② 笔记维度明细表 ----------
    if not _has_table(conn, ITEM_TABLE):
        op.create_table(
            ITEM_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("account_id", sa.Integer(), nullable=True),
            sa.Column("account_name", sa.String(length=128), nullable=True, server_default=""),
            #: 关联**批次**日志行：删批次行置空，明细不跟着消失（本表存在的理由之一）。
            sa.Column(
                "log_id",
                sa.Integer(),
                sa.ForeignKey(f"{BATCH_TABLE}.id", ondelete="SET NULL"),
                nullable=True,
            ),
            #: 关联 tasks 行（仅追溯用）。
            sa.Column(
                "task_id",
                sa.Integer(),
                sa.ForeignKey("tasks.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("note_id", sa.String(length=128), nullable=True, server_default=""),
            sa.Column("note_title", sa.String(length=512), nullable=True, server_default=""),
            sa.Column("note_cover", sa.Text(), nullable=True, server_default=""),
            #: `deleted` / `skipped` / `failed` / `not_in_pool` / `not_attempted`。
            sa.Column("result", sa.String(length=16), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True, server_default=""),
            sa.Column("published_at", sa.Integer(), nullable=True),
            sa.Column("operated_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index(f"ix_{ITEM_TABLE}_user_id", ITEM_TABLE, ["user_id"])
        op.create_index(f"ix_{ITEM_TABLE}_account_id", ITEM_TABLE, ["account_id"])
        op.create_index(f"ix_{ITEM_TABLE}_log_id", ITEM_TABLE, ["log_id"])
        op.create_index(f"ix_{ITEM_TABLE}_note_id", ITEM_TABLE, ["note_id"])
        op.create_index(f"ix_{ITEM_TABLE}_result", ITEM_TABLE, ["result"])
        op.create_index(f"ix_{ITEM_TABLE}_operated_at", ITEM_TABLE, ["operated_at"])


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, ITEM_TABLE):
        op.drop_table(ITEM_TABLE)
    if _has_table(conn, BATCH_TABLE):
        cols = _columns(conn, BATCH_TABLE)
        for name in ("abort_reason", "aborted", "coverage_note"):
            if name in cols:
                op.drop_column(BATCH_TABLE, name)
