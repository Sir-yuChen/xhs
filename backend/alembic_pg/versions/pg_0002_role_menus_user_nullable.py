"""role_menus.user_id nullable -- template rows for role grants

Mirrors the SQLite chain's `d4e5f6a1b2c3` (design 57 S2).

Why this file exists as a *separate* PG revision instead of reusing the SQLite
revision id: the two chains are deliberately independent (design 51 §2.2 --
the 95 historical SQLite migrations issue `PRAGMA`, which PG rejects). The
PG chain is `pg_baseline_0001` + one revision per feature, and the SQLite
chain carries the full history. Same schema, two ordered sets of steps.

Keeping the PG side explicit (rather than "the baseline already has it") means
an existing PG database from a previous release can be upgraded in place --
the baseline is only applied to *empty* databases.

NULL semantics: `role_menus.user_id IS NULL` = a **template row**, i.e. what
holders of that role see by default. Concrete ids are per-user deltas.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "pg_0002_role_menus_user_nullable"
down_revision: Union[str, None] = "pg_baseline_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _nullable(table: str, column: str) -> bool | None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return None
    for col in inspector.get_columns(table):
        if col["name"] == column:
            return bool(col.get("nullable"))
    return None


def upgrade() -> None:
    if _nullable("role_menus", "user_id") is False:
        op.alter_column(
            "role_menus", "user_id", existing_type=sa.Integer(), nullable=True
        )


def downgrade() -> None:
    if _nullable("role_menus", "user_id") is True:
        # Template rows cannot survive a NOT NULL column; drop them first.
        op.execute("DELETE FROM role_menus WHERE user_id IS NULL")
        op.alter_column(
            "role_menus", "user_id", existing_type=sa.Integer(), nullable=False
        )
