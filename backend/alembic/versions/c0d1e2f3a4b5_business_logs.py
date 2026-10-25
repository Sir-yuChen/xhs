"""业务日志表 business_logs（设计 64 §2.2）

2026-10-25 用户口径「补充完善日志体系以及相关业务核心业务重点，出入口，操作删除，
修改新增等节点」。`api_logs`（API 审计）只记端点面；本表记**业务语义面**
（认证出入口 / 系统管理 CRUD / 配置变更 / 队列终态），由
`services/business_log_service.record()` 双写（DB 行 + logfmt 统一格式文本行）。

保留策略：`system.queue_record_cleanup` 的 CLEANERS 注册表按 `log_retention_days`
（系统级配置，默认 90 天）清理 —— 与 api_logs / scheduled_job_logs 同批机制
（设计 64 §2.4）。

零数据迁移（新表）。幂等：是（IF NOT EXISTS）。可逆：DROP TABLE（审计层无回填诉求）。

Revision ID: c0d1e2f3a4b5
Revises: b0c1d2e3f4a5
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c0d1e2f3a4b5"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("level", sa.String(16), nullable=False, server_default="info"),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False, server_default=""),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("actor_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("workspace", sa.String(32), nullable=True),
        sa.Column("target_type", sa.String(32), nullable=False, server_default=""),
        sa.Column("target_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("summary", sa.String(255), nullable=False, server_default=""),
        sa.Column("detail_json", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_business_logs_event", "business_logs", ["event"])
    op.create_index("ix_business_logs_actor", "business_logs", ["actor_id"])
    op.create_index("ix_business_logs_created", "business_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_business_logs_created", table_name="business_logs")
    op.drop_index("ix_business_logs_actor", table_name="business_logs")
    op.drop_index("ix_business_logs_event", table_name="business_logs")
    op.drop_table("business_logs")
