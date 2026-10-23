"""users + email / phone with partial unique indexes

Mirrors the SQLite chain's `e5f6a1b2c3d4` (design 57 S3).

`email` is a **login credential** (registration code / login code, matching
dataPanel's `t_user.email`). Design 47 claimed `uq_users_email` already
existed -- measured false: neither the SQLite main DB nor PG had an `email`
column at all. This migration adds it.

Uniqueness is partial (`WHERE col <> ''`) rather than a plain UNIQUE
constraint: 27 pre-existing users have an empty email, and `'' = ''` would
make the second empty-email row fail to insert. `<> ''` expresses "unique
only among rows that actually filled it in".

Both `sqlite_where` and `postgresql_where` are written on the model side
(design 51 §3.1); this PG revision only needs the PG predicate, but the
index name matches the SQLite side so a schema diff stays quiet.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "pg_0003_users_email_phone"
down_revision: Union[str, None] = "pg_0002_role_menus_user_nullable"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def _indexes(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {i["name"] for i in inspector.get_indexes(table)}


def upgrade() -> None:
    cols = _columns("users")
    if not cols:
        return
    with op.batch_alter_table("users") as batch:
        if "email" not in cols:
            batch.add_column(
                sa.Column("email", sa.String(255), nullable=False, server_default="")
            )
        if "phone" not in cols:
            batch.add_column(
                sa.Column("phone", sa.String(32), nullable=False, server_default="")
            )

    names = _indexes("users")
    if "uq_users_email_nonempty" not in names:
        op.create_index(
            "uq_users_email_nonempty",
            "users",
            ["email"],
            unique=True,
            postgresql_where=sa.text("email <> ''"),
        )
    if "uq_users_phone_nonempty" not in names:
        op.create_index(
            "uq_users_phone_nonempty",
            "users",
            ["phone"],
            unique=True,
            postgresql_where=sa.text("phone <> ''"),
        )


def downgrade() -> None:
    cols = _columns("users")
    if not cols:
        return
    names = _indexes("users")
    if "uq_users_email_nonempty" in names:
        op.drop_index("uq_users_email_nonempty", table_name="users")
    if "uq_users_phone_nonempty" in names:
        op.drop_index("uq_users_phone_nonempty", table_name="users")
    with op.batch_alter_table("users") as batch:
        if "email" in cols:
            batch.drop_column("email")
        if "phone" in cols:
            batch.drop_column("phone")
