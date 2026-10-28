"""note_generation_links 合并进 notes

`note_generation_links` 的三个列里只有 `generation_record_id` 有值（1:1，一个生成记录
只产出一篇笔记），`item_id` 与 `note_pk` 冗余（前者在 `generation_records.item_id` 与
`notes.raw_json.item_id`，后者就是 `notes.id`）。故把该列合并进 `notes`，删除整表。

Revision ID: x0y1z2a3b4c5
Revises: w9x0y1z2a3b4
Create Date: 2026-10-28
"""

from alembic import op
import sqlalchemy as sa

revision = "x0y1z2a3b4c5"
down_revision = "w9x0y1z2a3b4"
branch_labels = None
depends_on = None

_LINK = "note_generation_links"


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
    if "generation_record_id" not in _columns("notes"):
        op.add_column(
            "notes", sa.Column("generation_record_id", sa.Integer(), nullable=True)
        )
        op.create_foreign_key(
            "fk_notes_generation_record",
            "notes",
            "generation_records",
            ["generation_record_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(
            "ix_notes_generation_record_id", "notes", ["generation_record_id"]
        )

    if _table_exists(_LINK):
        op.execute(
            sa.text(
                "update notes set generation_record_id = l.generation_record_id "
                "from note_generation_links l "
                "where l.note_pk = notes.id and l.generation_record_id is not null"
            )
        )
        # 索引随 drop_table 一并删除（显式 drop 会因索引名随迁移历史变动而失败）。
        op.drop_table(_LINK)


def downgrade() -> None:
    if not _table_exists(_LINK):
        op.create_table(
            _LINK,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("note_pk", sa.Integer(), sa.ForeignKey("notes.id"), nullable=False),
            sa.Column("item_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column(
                "generation_record_id",
                sa.Integer(),
                sa.ForeignKey("generation_records.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_note_generation_links_user_id", _LINK, ["user_id"])
        op.create_index("ix_note_generation_links_note_pk", _LINK, ["note_pk"])
        op.create_index(
            "ix_note_generation_links_generation_record_id", _LINK, ["generation_record_id"]
        )
        op.execute(
            sa.text(
                "insert into note_generation_links "
                "(user_id, note_pk, item_id, generation_record_id, created_at) "
                "select user_id, id, '', generation_record_id, now() from notes "
                "where generation_record_id is not null"
            )
        )

    if "generation_record_id" in _columns("notes"):
        op.drop_index("ix_notes_generation_record_id", table_name="notes")
        op.drop_constraint("fk_notes_generation_record", "notes", type_="foreignkey")
        op.drop_column("notes", "generation_record_id")
