"""删除监控中心相关表

「监控中心」页面及其全部后端逻辑已退场（用户 2026-10-28 口径：全面彻底删除），
故 drop `monitoring_snapshots` 与 `monitoring_targets`。两表实测均 0 行。

Revision ID: y1z2a3b4c5d6
Revises: x0y1z2a3b4c5
Create Date: 2026-10-28
"""

from alembic import op
import sqlalchemy as sa

revision = "y1z2a3b4c5d6"
down_revision = "x0y1z2a3b4c5"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                "select 1 from information_schema.tables "
                "where table_schema = 'public' and table_name = :t"
            ),
            {"t": table},
        ).first()
    )


def upgrade() -> None:
    # 先删子表（有 FK 指向 targets）。
    if _table_exists("monitoring_snapshots"):
        op.drop_table("monitoring_snapshots")
    if _table_exists("monitoring_targets"):
        op.drop_table("monitoring_targets")


def downgrade() -> None:
    if not _table_exists("monitoring_targets"):
        op.create_table(
            "monitoring_targets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("platform", sa.String(length=32), nullable=False, server_default="xhs"),
            sa.Column("target_type", sa.String(length=32), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("value", sa.String(length=512), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
            sa.Column("config", sa.JSON(), nullable=True),
            sa.Column("last_refreshed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column(
                "platform_account_id",
                sa.Integer(),
                sa.ForeignKey("platform_accounts.id"),
                nullable=True,
            ),
            sa.Column("crawl_interval_minutes", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_crawl_error", sa.Text(), nullable=True),
        )
        op.create_index("ix_monitoring_targets_user_id", "monitoring_targets", ["user_id"])
        op.create_index("ix_monitoring_targets_platform", "monitoring_targets", ["platform"])
        op.create_index("ix_monitoring_targets_target_type", "monitoring_targets", ["target_type"])

    if not _table_exists("monitoring_snapshots"):
        op.create_table(
            "monitoring_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "target_id",
                sa.Integer(),
                sa.ForeignKey("monitoring_targets.id"),
                nullable=False,
            ),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_monitoring_snapshots_target_id", "monitoring_snapshots", ["target_id"])
