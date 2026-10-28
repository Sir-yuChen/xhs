"""note_datacenter_stats 加 note_url

数据中心与 `notes` 表完全拆分后，平台笔记的详情只能靠统计表自带的原链接
（含 `xsec_token`）实时拉取，故把 `note_url` 落到本表。

Revision ID: w9x0y1z2a3b4
Revises: v8w9x0y1z2a3
Create Date: 2026-10-28
"""

from alembic import op
import sqlalchemy as sa

revision = "w9x0y1z2a3b4"
down_revision = "v8w9x0y1z2a3"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    return {
        row[0]
        for row in bind.execute(
            sa.text(
                "select column_name from information_schema.columns "
                "where table_schema = 'public' and table_name = :t"
            ),
            {"t": table},
        )
    }


def upgrade() -> None:
    if "note_url" not in _columns("note_datacenter_stats"):
        op.add_column(
            "note_datacenter_stats",
            sa.Column("note_url", sa.String(length=1024), nullable=False, server_default=""),
        )


def downgrade() -> None:
    if "note_url" in _columns("note_datacenter_stats"):
        op.drop_column("note_datacenter_stats", "note_url")
