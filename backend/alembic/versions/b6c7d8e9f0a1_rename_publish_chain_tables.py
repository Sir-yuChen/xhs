"""rename the remaining publish-chain tables to xhs_ prefix

Revision ID: b6c7d8e9f0a1
Revises: f1a2b3c4d5e7

2026-10-28 用户口径「私有表起名按照规范」：小红书私有的表一律加 `xhs_` 前缀。
发布链路还剩三张未带前缀（`ai_drafts` 17 行 / `publish_jobs` 1 行 /
`ai_generated_assets` 0 行），本迁移统一改名。

⚠️ 外键与序列名一并处理：
* `ai_generated_assets.draft_id` → `xhs_ai_drafts.id`
* `publish_jobs.source_draft_id` → `xhs_ai_drafts.id`
* `xhs_publish_assets.parent_id` 是**裸整数**（跨两种父），无外键，不用动。

`ALTER TABLE ... RENAME TO` 在 PG 里会保留外键定义与索引，但**索引名不会跟着改**
⇒ 逐个改名，否则下次迁移按名找不到。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6c7d8e9f0a1"
down_revision: Union[str, None] = "f1a2b3c4d5e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: (旧名, 新名)，**顺序即依赖顺序** —— 父表先改，指向它的外键才跟得上。
RENAMES = (
    ("ai_drafts", "xhs_ai_drafts"),
    ("publish_jobs", "xhs_publish_jobs"),
    ("ai_generated_assets", "xhs_ai_generated_assets"),
)


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _rename_indexes(bind, old: str, new: str) -> None:
    """把带旧表名的索引/约束名换成新表名（PG 不自动改）。"""
    insp = sa.inspect(bind)
    for idx in insp.get_indexes(new):
        name = idx.get("name")
        if name and old in name:
            op.execute(f'ALTER INDEX "{name}" RENAME TO "{name.replace(old, new)}"')
    # 主键/唯一约束名
    for uc in insp.get_unique_constraints(new):
        name = uc.get("name")
        if name and old in name:
            op.execute(
                f'ALTER TABLE "{new}" RENAME CONSTRAINT "{name}" TO '
                f'"{name.replace(old, new)}"'
            )


def upgrade() -> None:
    bind = op.get_bind()
    for old, new in RENAMES:
        tables = _tables(bind)
        if old in tables and new not in tables:
            op.rename_table(old, new)
            _rename_indexes(bind, old, new)


def downgrade() -> None:
    bind = op.get_bind()
    for old, new in reversed(RENAMES):
        tables = _tables(bind)
        if new in tables and old not in tables:
            op.rename_table(new, old)
            _rename_indexes(bind, new, old)
