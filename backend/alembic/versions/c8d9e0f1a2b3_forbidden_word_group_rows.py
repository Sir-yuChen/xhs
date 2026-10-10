"""违规词分组本体表（用户口径 2026-10-08：「组新增启用停用功能」）

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-10-08

## 为什么需要这张表

词行只能表达「这个组有词」，表达不了「**这个组被停用了**」：
停用后词还在（不该删，那是用户的词），但整组不参与 OCR 匹配。
所以分组需要自己的行来承载开关状态。

⚠️ **不回填存量分组**：没有组行 = 默认启用（`enabled_groups` 的判据），
所以存量数据行为不变 —— 建行只在用户第一次动那个开关时才发生。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c8d9e0f1a2b3"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forbidden_word_groups",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ux_forbidden_word_groups_user_name",
        "forbidden_word_groups",
        ["user_id", "name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_forbidden_word_groups_user_name", table_name="forbidden_word_groups")
    op.drop_table("forbidden_word_groups")
