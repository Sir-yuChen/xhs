"""no_traffic_cleanup_logs: 无流量笔记清理日志表

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-10-05

## 为什么

2026-10-05 用户口径：「新增清理无流量笔记日志表……记录清理无流量笔记的信息和
清理结果时间等等，失败原因，操作时间，账号」「日志表独立」。

此前「清理无流量」只落一行 `tasks` 记录（`task_type=xhs_no_traffic_cleanup`），
而 `tasks` 行是**并发控制载体**（`_running_cleanup` 靠它防重入）—— 它会被公共
「清理记录表」任务按保留期删掉 ⇒ 删完就再也答不出「上个月清了多少篇、失败几篇、
为什么失败」。用户在**我的笔记**页要一个「查询清理日志详情」入口，就需要一张
**独立保留**的表。

## 为什么不是复用 scheduled_job_logs

本清理是**手动触发**（用户口径「入口只有我的笔记页的清理无流量按钮」），
手动触发不落 `scheduled_job_logs`（那张表是调度执行日志，靠 `job_id` 关联任务行）。

## 兼容性

纯新增表，不动任何存量表；`downgrade` 直接 drop。
时间列与全库一致用朴素 datetime（`core/time.py::shanghai_now` 口径）。
"""
from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from backend.app.core.migration_introspect import column_names, table_exists

revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "no_traffic_cleanup_logs"


def _columns(conn, table: str) -> set[str]:
    """列清单（最小夹具库上 `params` 可能不存在 —— 见下）。"""
    #: v1.1.0 PG 化：原为 `PRAGMA table_info`（SQLite 专属），统一走内省助手。
    return column_names(conn, table)


def _has_table(conn, name: str) -> bool:
    #: v1.1.0 PG 化：原为 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return table_exists(conn, name)


def upgrade() -> None:
    conn = op.get_bind()
    # ---------- ① 新表（幂等：重跑 / 手工建过就跳过） ----------
    if not _has_table(conn, TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("account_id", sa.Integer(), nullable=True),
            sa.Column("account_name", sa.String(length=128), nullable=True, server_default=""),
            sa.Column(
                "task_id",
                sa.Integer(),
                sa.ForeignKey("tasks.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("trigger_type", sa.String(length=16), nullable=True, server_default="manual"),
            sa.Column("thresholds", sa.JSON(), nullable=True),
            sa.Column("query", sa.JSON(), nullable=True),
            sa.Column("note_ids", sa.JSON(), nullable=True),
            sa.Column("batch_limit", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("before_total", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("after_total", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("total", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("deleted", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("skipped", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("failed", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("not_in_pool", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("status", sa.String(length=16), nullable=True, server_default="running"),
            sa.Column("reason", sa.Text(), nullable=True, server_default=""),
            sa.Column("details", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True, server_default="0"),
        )
        for col in ("user_id", "account_id", "task_id", "status", "created_at"):
            op.create_index(f"ix_{TABLE}_{col}", TABLE, [col])

    # ---------- ② 数据侧：任务改名 + 加默认保留期参数 ----------
    # 「队列记录清理」升级为**公共「清理记录表」任务**（2026-10-05 用户口径）：
    # job_key **不变**（存量行与历史日志快照 `job_key`/`job_name` 不必迁移就能继续对上），
    # 只改中文名；`params` 补默认 `retention_days=15`（缺它时执行侧退同一默认值，
    # 但显式写入用户才能在任务中心的编辑弹窗里看到并修改当前值）。
    if _has_table(conn, "scheduled_jobs"):
        # ⚠️ 列判据不是洁癖：测试的**最小遗留库**（`stamp <父 revision>` 那条路）里
        #    `scheduled_jobs` 可能只建了迁移链当时用得上的列 —— 不判就整条迁移
        #    `no such column: params` 炸掉，而报错在 alembic 子进程里、看起来像无关的
        #    迁移债（同类坑见 `d5e6f7a8b9c0` 的教训）。
        cols = _columns(conn, "scheduled_jobs")
        if "name" in cols:
            conn.execute(
                sa.text(
                    "update scheduled_jobs set name='清理记录表' "
                    "where job_key='system.queue_record_cleanup'"
                )
            )
        if "params" not in cols:
            return
        rows = conn.execute(
            sa.text(
                "select id, params from scheduled_jobs "
                "where job_key='system.queue_record_cleanup'"
            )
        ).fetchall()
        for row in rows:
            job_id, params = row[0], row[1]
            if params:
                # 已有参数（JSON 文本）：只补缺失的键，**不覆盖**用户已设的值。
                try:
                    parsed = json.loads(params) if isinstance(params, str) else dict(params)
                except (TypeError, ValueError):
                    parsed = {}
                if not isinstance(parsed, dict):
                    parsed = {}
                if "retention_days" in parsed:
                    continue
                parsed["retention_days"] = 15
                conn.execute(
                    sa.text("update scheduled_jobs set params=:p where id=:i"),
                    {"p": json.dumps(parsed, ensure_ascii=False), "i": job_id},
                )
            else:
                conn.execute(
                    sa.text("update scheduled_jobs set params=:p where id=:i"),
                    {"p": json.dumps({"retention_days": 15}), "i": job_id},
                )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, TABLE):
        op.drop_table(TABLE)
    # 数据侧同义回滚：名字改回旧值、参数清掉（列不存在时不报错 —— 幂等）。
    if _has_table(conn, "scheduled_jobs"):
        conn.execute(
            sa.text(
                "update scheduled_jobs set name='队列记录清理' "
                "where job_key='system.queue_record_cleanup'"
            )
        )
