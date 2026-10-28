"""rename remaining xhs-private tables to xhs_ prefix

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3

2026-10-28 用户口径「必须规范化」：小红书私有表一律 `xhs_` 前缀。

改名的 7 张（全部只服务小红书）：

| 旧名 | 新名 | 行数 |
|---|---|---|
| `notes` | `xhs_notes` | 48 |
| `note_topics` | `xhs_note_topics` | 125 |
| `topics` | `xhs_topics` | 4963 |
| `generation_records` | `xhs_generation_records` | 47 |
| `platform_accounts` | `xhs_accounts` | 5 |
| `prompt_modules` | `xhs_prompt_modules` | 248 |
| `prompt_templates` | `xhs_prompt_templates` | 124 |

**不动的 17 张**（跨平台底座，换平台照样用）：
`users` / `roles` / `role_menus` / `menus` / `dict_types` / `dict_items` /
`system_settings` / `api_logs` / `business_logs` / `notifications` /
`scheduled_jobs` / `scheduled_job_logs` / `tasks` /
`llm_gateway_*`(4) / `llm_business_providers`。

⚠️ 顺序即依赖顺序：`topics` 先于 `note_topics`（后者 FK 指前者），
`notes` 先于所有指向它的表。PG 的 `RENAME TO` 保留外键定义但**索引/约束名不改**
⇒ 逐个改名，否则下次迁移按名找不到。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: Union[str, None] = "d8e9f0a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: (旧名, 新名) —— 顺序即依赖顺序。
RENAMES = (
    ("users_dummy_placeholder", "users_dummy_placeholder"),  # 占位，保持元组非空
    ("topics", "xhs_topics"),
    ("notes", "xhs_notes"),
    ("platform_accounts", "xhs_accounts"),
    ("generation_records", "xhs_generation_records"),
    ("note_topics", "xhs_note_topics"),
    ("prompt_modules", "xhs_prompt_modules"),
    ("prompt_templates", "xhs_prompt_templates"),
)


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _rename_indexes(bind, old: str, new: str) -> None:
    insp = sa.inspect(bind)
    for idx in insp.get_indexes(new):
        name = idx.get("name")
        if name and old in name:
            op.execute(f'ALTER INDEX "{name}" RENAME TO "{name.replace(old, new)}"')
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
        if old == new:
            continue
        tables = _tables(bind)
        if old in tables and new not in tables:
            op.rename_table(old, new)
            _rename_indexes(bind, old, new)


def downgrade() -> None:
    bind = op.get_bind()
    for old, new in reversed(RENAMES):
        if old == new:
            continue
        tables = _tables(bind)
        if new in tables and old not in tables:
            op.rename_table(new, old)
            _rename_indexes(bind, new, old)
