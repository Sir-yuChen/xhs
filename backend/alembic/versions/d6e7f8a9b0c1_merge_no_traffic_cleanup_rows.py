"""merge no-traffic cleanup tables into xhs_no_traffic_cleanup_rows + rename note_datacenter_stats

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0

2026-10-28 用户口径：
1. 「no_traffic_cleanup_logs 和 no_traffic_cleanup_items 是否能合并成一张表，父子结构，
   懒加载，且小红书私有」⇒ 两表合并为 `xhs_no_traffic_cleanup_rows`（`row_type` 判父子）。
2. 「note_datacenter_stats 也是小红书私有」⇒ 改名为 `xhs_note_datacenter_stats`。

⚠️ 子行保留语义：原 `no_traffic_cleanup_items.log_id` 是 `ON DELETE SET NULL`
（批次行过期被删，明细行仍在）⇒ 新表 `parent_id` 保持 `SET NULL`，**不级联删**。

⚠️ 父子判据用 `row_type` 而非 `parent_id IS NULL`：`SET NULL` 之后孤儿明细的
`parent_id` 也是 NULL，用 NULL 判父行会把它们当批次读出来。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_LOGS = "no_traffic_cleanup_logs"
OLD_ITEMS = "no_traffic_cleanup_items"
NEW = "xhs_no_traffic_cleanup_rows"
OLD_STATS = "note_datacenter_stats"
NEW_STATS = "xhs_note_datacenter_stats"


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    insp = sa.inspect(bind)
    if table not in set(insp.get_table_names()):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    # ---- 1. 统计表改名（L1 私有）----
    if OLD_STATS in tables and NEW_STATS not in tables:
        op.rename_table(OLD_STATS, NEW_STATS)
        # 索引/约束名带旧表名，一并改（否则下次迁移按名找不到）。
        for idx in sa.inspect(bind).get_indexes(NEW_STATS):
            old_name = idx["name"]
            if old_name and OLD_STATS in old_name:
                op.execute(
                    f'ALTER INDEX "{old_name}" RENAME TO '
                    f'"{old_name.replace(OLD_STATS, NEW_STATS)}"'
                )

    # ---- 2. 建合并表 ----
    if NEW not in _tables(bind):
        op.create_table(
            NEW,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("row_type", sa.String(8), nullable=False, server_default="batch"),
            sa.Column(
                "parent_id",
                sa.Integer(),
                sa.ForeignKey(f"{NEW}.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("account_id", sa.Integer(), nullable=True),
            sa.Column("account_name", sa.String(128), nullable=False, server_default=""),
            sa.Column(
                "task_id",
                sa.Integer(),
                sa.ForeignKey("tasks.id", ondelete="SET NULL"),
                nullable=True,
            ),
            # 批次行字段
            sa.Column("trigger_type", sa.String(16), nullable=False, server_default="manual"),
            sa.Column("batch_no", sa.String(64), nullable=False, server_default=""),
            sa.Column("thresholds", sa.JSON(), nullable=True),
            sa.Column("query", sa.JSON(), nullable=True),
            sa.Column("note_ids", sa.JSON(), nullable=True),
            sa.Column("batch_limit", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("before_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("after_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("not_in_pool", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(16), nullable=False, server_default="running"),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("coverage_note", sa.Text(), nullable=False, server_default=""),
            sa.Column("details", sa.JSON(), nullable=True),
            sa.Column("aborted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("abort_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            # 明细行字段
            sa.Column("note_id", sa.String(128), nullable=False, server_default=""),
            sa.Column("note_title", sa.String(512), nullable=False, server_default=""),
            sa.Column("note_cover", sa.Text(), nullable=False, server_default=""),
            sa.Column("result", sa.String(16), nullable=False, server_default=""),
            sa.Column("published_at", sa.BigInteger(), nullable=True),
            sa.Column("operated_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_xhs_cleanup_rows_batch_no", NEW, ["batch_no"])
        op.create_index("ix_xhs_cleanup_rows_parent", NEW, ["parent_id"])
        op.create_index("ix_xhs_cleanup_rows_type_created", NEW, ["row_type", "created_at"])
        op.create_index("ix_xhs_cleanup_rows_row_type", NEW, ["row_type"])
        op.create_index("ix_xhs_cleanup_rows_user_id", NEW, ["user_id"])
        op.create_index("ix_xhs_cleanup_rows_account_id", NEW, ["account_id"])
        op.create_index("ix_xhs_cleanup_rows_created_at", NEW, ["created_at"])

    # ---- 3. 搬数据：批次行原 id 不变，明细行新 id、parent_id 指回批次 ----
    tables = _tables(bind)
    if OLD_LOGS in tables:
        # 幂等：已搬过的批次行不重复插（按原 id 判）。
        op.execute(
            f"""
            INSERT INTO {NEW} (
                id, row_type, parent_id, user_id, account_id, account_name, task_id,
                trigger_type, batch_no, thresholds, query, note_ids, batch_limit,
                before_total, after_total, total, deleted, skipped, failed, not_in_pool,
                status, reason, coverage_note, details, aborted, abort_reason,
                started_at, finished_at, duration_ms, created_at
            )
            SELECT
                l.id, 'batch', NULL, l.user_id, l.account_id, l.account_name, l.task_id,
                l.trigger_type, l.batch_no, l.thresholds, l.query, l.note_ids, l.batch_limit,
                l.before_total, l.after_total, l.total, l.deleted, l.skipped, l.failed,
                l.not_in_pool, l.status, l.reason, l.coverage_note, l.details, l.aborted,
                l.abort_reason, l.started_at, l.finished_at, l.duration_ms, l.created_at
            FROM {OLD_LOGS} l
            WHERE NOT EXISTS (SELECT 1 FROM {NEW} n WHERE n.id = l.id AND n.row_type = 'batch')
            """
        )
        # 批次行的自增序列要跟进，否则后续 insert 撞主键。
        op.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{NEW}', 'id'),
                GREATEST(COALESCE((SELECT MAX(id) FROM {NEW}), 1), 1)
            )
            """
        )

    if OLD_ITEMS in tables:
        op.execute(
            f"""
            INSERT INTO {NEW} (
                row_type, parent_id, user_id, account_id, account_name, task_id,
                note_id, note_title, note_cover, result, reason, published_at,
                operated_at, created_at
            )
            SELECT
                'item', i.log_id, i.user_id, i.account_id, i.account_name, i.task_id,
                i.note_id, i.note_title, i.note_cover, i.result, i.reason, i.published_at,
                i.operated_at, i.created_at
            FROM {OLD_ITEMS} i
            WHERE NOT EXISTS (
                SELECT 1 FROM {NEW} n
                WHERE n.row_type = 'item'
                  AND n.note_id = i.note_id
                  AND n.operated_at IS NOT DISTINCT FROM i.operated_at
                  AND n.parent_id IS NOT DISTINCT FROM i.log_id
            )
            """
        )

    # ---- 4. 删旧表 ----
    if OLD_ITEMS in _tables(bind):
        op.drop_table(OLD_ITEMS)
    if OLD_LOGS in _tables(bind):
        op.drop_table(OLD_LOGS)


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    # ---- 1. 重建两张旧表 ----
    if OLD_LOGS not in tables:
        op.create_table(
            OLD_LOGS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("account_id", sa.Integer(), nullable=True),
            sa.Column("account_name", sa.String(128), nullable=False, server_default=""),
            sa.Column(
                "task_id",
                sa.Integer(),
                sa.ForeignKey("tasks.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("trigger_type", sa.String(16), nullable=False, server_default="manual"),
            sa.Column("batch_no", sa.String(64), nullable=False, server_default=""),
            sa.Column("thresholds", sa.JSON(), nullable=True),
            sa.Column("query", sa.JSON(), nullable=True),
            sa.Column("note_ids", sa.JSON(), nullable=True),
            sa.Column("batch_limit", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("before_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("after_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("not_in_pool", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(16), nullable=False, server_default="running"),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("coverage_note", sa.Text(), nullable=False, server_default=""),
            sa.Column("details", sa.JSON(), nullable=True),
            sa.Column("aborted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("abort_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_index(
            "ix_no_traffic_cleanup_logs_batch_no", OLD_LOGS, ["batch_no"]
        )
        op.create_index("ix_no_traffic_cleanup_logs_user_id", OLD_LOGS, ["user_id"])
        op.create_index("ix_no_traffic_cleanup_logs_account_id", OLD_LOGS, ["account_id"])
        op.create_index("ix_no_traffic_cleanup_logs_status", OLD_LOGS, ["status"])
        op.create_index("ix_no_traffic_cleanup_logs_created_at", OLD_LOGS, ["created_at"])

    if OLD_ITEMS not in _tables(bind):
        op.create_table(
            OLD_ITEMS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("account_id", sa.Integer(), nullable=True),
            sa.Column("account_name", sa.String(128), nullable=False, server_default=""),
            sa.Column(
                "log_id",
                sa.Integer(),
                sa.ForeignKey(f"{OLD_LOGS}.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "task_id",
                sa.Integer(),
                sa.ForeignKey("tasks.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("note_id", sa.String(128), nullable=False, server_default=""),
            sa.Column("note_title", sa.String(512), nullable=False, server_default=""),
            sa.Column("note_cover", sa.Text(), nullable=False, server_default=""),
            sa.Column("result", sa.String(16), nullable=False, server_default="failed"),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("published_at", sa.BigInteger(), nullable=True),
            sa.Column("operated_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_no_traffic_cleanup_items_user_id", OLD_ITEMS, ["user_id"])
        op.create_index("ix_no_traffic_cleanup_items_account_id", OLD_ITEMS, ["account_id"])
        op.create_index("ix_no_traffic_cleanup_items_log_id", OLD_ITEMS, ["log_id"])
        op.create_index("ix_no_traffic_cleanup_items_note_id", OLD_ITEMS, ["note_id"])
        op.create_index("ix_no_traffic_cleanup_items_result", OLD_ITEMS, ["result"])
        op.create_index("ix_no_traffic_cleanup_items_operated_at", OLD_ITEMS, ["operated_at"])

    # ---- 2. 搬回去 ----
    if NEW in _tables(bind):
        op.execute(
            f"""
            INSERT INTO {OLD_LOGS} (
                id, user_id, account_id, account_name, task_id, trigger_type, batch_no,
                thresholds, query, note_ids, batch_limit, before_total, after_total,
                total, deleted, skipped, failed, not_in_pool, status, reason,
                coverage_note, details, aborted, abort_reason, created_at, started_at,
                finished_at, duration_ms
            )
            SELECT
                id, user_id, account_id, account_name, task_id, trigger_type, batch_no,
                thresholds, query, note_ids, batch_limit, before_total, after_total,
                total, deleted, skipped, failed, not_in_pool, status, reason,
                coverage_note, details, aborted, abort_reason, created_at, started_at,
                finished_at, duration_ms
            FROM {NEW}
            WHERE row_type = 'batch'
              AND NOT EXISTS (SELECT 1 FROM {OLD_LOGS} o WHERE o.id = {NEW}.id)
            """
        )
        op.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{OLD_LOGS}', 'id'),
                GREATEST(COALESCE((SELECT MAX(id) FROM {OLD_LOGS}), 1), 1)
            )
            """
        )
        op.execute(
            f"""
            INSERT INTO {OLD_ITEMS} (
                user_id, account_id, account_name, log_id, task_id, note_id,
                note_title, note_cover, result, reason, published_at, operated_at, created_at
            )
            SELECT
                user_id, account_id, account_name, parent_id, task_id, note_id,
                note_title, note_cover, result, reason, published_at,
                COALESCE(operated_at, created_at), created_at
            FROM {NEW}
            WHERE row_type = 'item'
              AND NOT EXISTS (
                  SELECT 1 FROM {OLD_ITEMS} o
                  WHERE o.note_id = {NEW}.note_id
                    AND o.operated_at IS NOT DISTINCT FROM COALESCE({NEW}.operated_at, {NEW}.created_at)
              )
            """
        )
        op.drop_table(NEW)

    # ---- 3. 统计表改回原名 ----
    if NEW_STATS in _tables(bind) and OLD_STATS not in _tables(bind):
        for idx in sa.inspect(bind).get_indexes(NEW_STATS):
            old_name = idx["name"]
            if old_name and NEW_STATS in old_name:
                op.execute(
                    f'ALTER INDEX "{old_name}" RENAME TO '
                    f'"{old_name.replace(NEW_STATS, OLD_STATS)}"'
                )
        op.rename_table(NEW_STATS, OLD_STATS)
