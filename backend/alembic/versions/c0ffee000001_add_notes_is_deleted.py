"""add notes.is_deleted soft-delete flag (2026-10-12, no-traffic cleanup)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f8
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision = "c0ffee000001"
down_revision = "a1b2c3d4e5f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    #: SQLite 加列带非空 + 默认值时直接 ALTER 即可（默认值用于回填存量行）。
    # v1.1.0 PG 化：布尔列的默认值必须写 sa.false()（原 `"0"` 是 SQLite 写法，
    # PG 渲染 DEFAULT 0 对 boolean 列报 DatatypeMismatch）。
    op.add_column(
        "notes",
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    #: 与模型 `index=True` 对齐，软删过滤走索引。
    op.create_index("ix_notes_is_deleted", "notes", ["is_deleted"])


def downgrade() -> None:
    op.drop_index("ix_notes_is_deleted", table_name="notes")
    op.drop_column("notes", "is_deleted")
