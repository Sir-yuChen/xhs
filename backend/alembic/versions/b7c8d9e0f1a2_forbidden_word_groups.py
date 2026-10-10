"""违规词分组（用户口径 2026-10-08：「违规词新增分组逻辑」）

Revision ID: a1b2c3d4e5f6
Revises: z9a8b7c6d5e4
Create Date: 2026-10-08

## 改了两件事

1. **加列** `forbidden_words.group_name`（默认空串 = 未分组）。
2. **换唯一键**：`(user_id, word)` → `(user_id, group_name, word)`。

⚠️ **为什么必须换键**：旧键下「『最佳』同时属于『极限词』和『广告词』」会被拒。
分组本来就是为了让同一个词在不同语境下分开管，键不改等于分组没做成。

⚠️ 换键用的是 `batch_alter_table`：SQLite 不支持 `DROP CONSTRAINT`，只能
重建表；SQLAlchemy 的 batch 模式会把数据一起搬过去。**存量行不丢** ——
它们全部落在 `group_name = ''`（未分组）里，与用户的原有认知一致
（以前没有分组概念，所以都算未分组）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7c8d9e0f1a2"
down_revision = "z9a8b7c6d5e4"
branch_labels = None
depends_on = None

_OLD_INDEX = "ux_forbidden_words_user_word"
_NEW_INDEX = "ux_forbidden_words_user_group_word"
_GROUP_INDEX = "ix_forbidden_words_user_group"


def upgrade() -> None:
    #: ① 先加列（NULL 会挡住唯一索引，所以带 server_default 落成空串）
    op.add_column(
        "forbidden_words",
        sa.Column("group_name", sa.String(length=60), nullable=False, server_default=""),
    )

    #: ② 换唯一键（batch 重建表，SQLite 下的唯一做法）
    with op.batch_alter_table("forbidden_words") as batch:
        batch.drop_index(_OLD_INDEX)
        batch.create_index(_NEW_INDEX, ["user_id", "group_name", "word"], unique=True)
        batch.create_index(_GROUP_INDEX, ["user_id", "group_name"])


def downgrade() -> None:
    with op.batch_alter_table("forbidden_words") as batch:
        batch.drop_index(_GROUP_INDEX)
        batch.drop_index(_NEW_INDEX)
        batch.create_index(_OLD_INDEX, ["user_id", "word"], unique=True)

    op.drop_column("forbidden_words", "group_name")
