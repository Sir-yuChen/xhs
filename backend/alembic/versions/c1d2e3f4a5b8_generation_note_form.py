"""generation records note_form

Adds `generation_records.note_form` (`image` / `video`).

User requirement (2026-10-19): generation records must carry an explicit
note-form type instead of guessing from media assets -- when media
generation fails there is nothing to infer from, and the UI echo then
lands on the wrong tab.

Revision ID: c1d2e3f4a5b8
Revises: b5c6d7e8f9a0
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c1d2e3f4a5b8"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    if "note_form" not in _columns("generation_records"):
        op.add_column(
            "generation_records",
            sa.Column("note_form", sa.String(length=16), nullable=False, server_default="image"),
        )
    # Index for "show me only image / only video versions of this product".
    existing = {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes("generation_records")}
    if "ix_generation_records_note_form" not in existing:
        op.create_index(
            "ix_generation_records_note_form", "generation_records", ["note_form"], unique=False
        )

    # Backfill from media_json: any video asset wins, otherwise image.
    op.execute(
        """
        UPDATE generation_records
           SET note_form = 'video'
         WHERE media_json LIKE '%"asset_type": "video"%'
            OR media_json LIKE '%"asset_type":"video"%'
        """
    )


def downgrade() -> None:
    existing = {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes("generation_records")}
    if "ix_generation_records_note_form" in existing:
        op.drop_index("ix_generation_records_note_form", table_name="generation_records")
    if "note_form" in _columns("generation_records"):
        op.drop_column("generation_records", "note_form")
