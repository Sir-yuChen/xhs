"""违规词两张表合并为一张自引用父子表

`forbidden_word_groups`（0 行）+ `forbidden_words`（717 行）⇒ 单表
`xhs_forbidden_words`：`parent_id IS NULL` = 分组，非空 = 组内的一条词。

合并的实质收益：旧形态下「组」有两个真相源（词上的 `group_name` 字符串 +
分组表的行），实测词表里有「满组」「小红书」两个组名而分组表 **0 行** ——
两边可以不一致。合并后分组就是那行记录。

唯一性：`(user_id, parent_id, word)` —— 同组内不重复，**不同组可同名**。

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-10-28
"""

from alembic import op
import sqlalchemy as sa

revision = "c5d6e7f8a9b0"
down_revision = "b4c5d6e7f8a9"
branch_labels = None
depends_on = None

NEW = "xhs_forbidden_words"
OLD_GROUPS = "forbidden_word_groups"
OLD_WORDS = "forbidden_words"


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
    if _table_exists(NEW):
        return
    has_groups = _table_exists(OLD_GROUPS)
    has_words = _table_exists(OLD_WORDS)

    op.create_table(
        NEW,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey(NEW + ".id", ondelete="CASCADE"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("word", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("match_mode", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("remark", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_xhs_fw_parent_id", NEW, ["parent_id"])
    op.create_index("ix_xhs_fw_user_id", NEW, ["user_id"])
    op.create_index("ix_xhs_fw_name", NEW, ["name"])
    op.create_index("ix_xhs_fw_user_enabled", NEW, ["user_id", "enabled"])
    op.create_index(
        "uq_xhs_fw_group_name",
        NEW,
        ["user_id", "name"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NULL"),
    )
    op.create_index(
        "uq_xhs_fw_group_word",
        NEW,
        ["parent_id", "word"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NOT NULL"),
    )

    bind = op.get_bind()

    # 1) 有分组行 ⇒ 原样搬成父行
    if has_groups:
        bind.execute(
            sa.text(
                f"insert into {NEW} (user_id, parent_id, name, word, match_mode, enabled, remark, created_at, updated_at) "
                f"select user_id, cast(NULL as integer), name, '', 1, enabled, '', created_at, updated_at from {OLD_GROUPS}"
            )
        )

    # 2) 词行里出现的组名，若无对应父行则补出来（实测原分组表 0 行，全靠这步）
    if has_words:
        bind.execute(
            sa.text(
                f"insert into {NEW} (user_id, parent_id, name, word, match_mode, enabled, remark, created_at, updated_at) "
                f"select distinct w.user_id, cast(NULL as integer), coalesce(w.group_name, ''), '', 1, true, '', now(), now() "
                f"from {OLD_WORDS} w "
                f"where not exists ("
                f"  select 1 from {NEW} p where p.user_id = w.user_id and p.name = coalesce(w.group_name, '') "
                f"  and p.parent_id is null)"
            )
        )

        # 3) 词行挂到对应父行下（同组内重复词只保留最早一条，避免撞唯一索引）
        bind.execute(
            sa.text(
                f"insert into {NEW} (parent_id, user_id, name, word, match_mode, enabled, remark, created_at, updated_at) "
                f"select p.id, w.user_id, coalesce(w.group_name, ''), w.word, w.match_mode, w.enabled, "
                f"coalesce(w.remark, ''), w.created_at, w.updated_at "
                f"from {OLD_WORDS} w "
                f"join {NEW} p on p.user_id = w.user_id and p.name = coalesce(w.group_name, '') "
                f"  and p.parent_id is null "
                f"where w.id in ("
                f"  select min(id) from {OLD_WORDS} group by user_id, coalesce(group_name, ''), word)"
            )
        )

    op.execute(sa.text(f"drop table if exists {OLD_WORDS} cascade"))
    op.execute(sa.text(f"drop table if exists {OLD_GROUPS} cascade"))


def downgrade() -> None:
    if not _table_exists(NEW):
        return

    bind = op.get_bind()
    op.create_table(
        OLD_WORDS,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("word", sa.String(length=120), nullable=False),
        sa.Column("match_mode", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("remark", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("group_name", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        OLD_GROUPS,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )

    # ⚠️ 先摘外键再删父行：`ON DELETE CASCADE` 会把词行一起带走。
    op.drop_index("ix_xhs_fw_parent_id", table_name=NEW)

    bind.execute(
        sa.text(
            f"insert into {OLD_WORDS} (user_id, word, match_mode, enabled, remark, group_name, created_at, updated_at) "
            f"select user_id, word, match_mode, enabled, remark, name, created_at, updated_at "
            f"from {NEW} where parent_id is not null"
        )
    )
    bind.execute(
        sa.text(
            f"insert into {OLD_GROUPS} (user_id, name, enabled, created_at, updated_at) "
            f"select user_id, name, enabled, created_at, updated_at from {NEW} where parent_id is null"
        )
    )
    op.drop_table(NEW)
