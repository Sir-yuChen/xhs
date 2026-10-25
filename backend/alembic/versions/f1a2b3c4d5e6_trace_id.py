"""add trace_id to log tables (design 67)

链路式日志（2026-10-25 用户口径「日志应以链路式记录」「后台支持使用链路id查该链路
请求的所有日志」）。

三张日志表加 `trace_id`（W3C trace-id，32 位十六进制）+ 索引：
* `api_logs.trace_id`          —— 端点面留痕
* `business_logs.trace_id`     —— 业务语义面
* `scheduled_job_logs.trace_id`—— 定时任务执行（用户触发时继承那次点击的链路）

可空：迁移前的存量行没有链路 id。**不补假值** —— 补了会让「按链路查」查出无关的行。

Revision ID: f1a2b3c4d5e6
Revises: d0e1f2a3b4c5
Create Date: 2026-10-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None

TABLES = ("api_logs", "business_logs", "scheduled_job_logs")


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    for table in TABLES:
        if not _has_column(table, "trace_id"):
            op.add_column(table, sa.Column("trace_id", sa.String(32), nullable=True))
        #: 索引名带表名前缀，避免三张表撞同一个名字（PG 索引名全库唯一）。
        op.execute(
            f'CREATE INDEX IF NOT EXISTS ix_{table}_trace_id ON {table} (trace_id)'
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_trace_id")
        if _has_column(table, "trace_id"):
            with op.batch_alter_table(table) as batch:
                batch.drop_column("trace_id")
