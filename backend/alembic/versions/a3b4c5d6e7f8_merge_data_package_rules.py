"""商品数据包规则两张表合并为一张自引用父子表

`data_package_rule_sets`（68 行 / 规则包）+ `data_package_rules`（117 行 / 包内规则）
⇒ 单表 `data_package_rules`，`parent_id IS NULL` = 规则包，非空 = 包内规则。

两张表是同一条业务实体的两层（父 1:N 子），合表后可少一次 join、少一张表。
`user_id` 子行冗余一份（取父行的），查询不必 join 父表。

契约不变：`set_id` / `rule_name` 两个旧字段名由 ORM 属性别名映射到
`parent_id` / `name`，API 与前端零改动。

Revision ID: a3b4c5d6e7f8
Revises: z2a3b4c5d6e7
Create Date: 2026-10-28
"""

from alembic import op
import sqlalchemy as sa

revision = "a3b4c5d6e7f8"
down_revision = "z2a3b4c5d6e7"
branch_labels = None
depends_on = None

NEW = "data_package_rules"
OLD_SETS = "data_package_rule_sets"


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
    if not _table_exists(OLD_SETS):
        return

    cols = _columns(NEW)

    # 1) 新列：parent_id + remark + user_id（子行冗余）
    if "parent_id" not in cols:
        op.add_column(NEW, sa.Column("parent_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_dp_rules_parent", NEW, NEW, ["parent_id"], ["id"], ondelete="CASCADE"
        )
        op.create_index("ix_data_package_rules_parent_id", NEW, ["parent_id"])
    if "remark" not in cols:
        op.add_column(NEW, sa.Column("remark", sa.String(length=500), nullable=True))
    if "user_id" not in cols:
        op.add_column(NEW, sa.Column("user_id", sa.Integer(), nullable=True))
        op.create_index("ix_data_package_rules_user_id", NEW, ["user_id"])

    #: ⚠️ **先把子表的 `rule_name` 改成 `name`**，否则下面「搬父行」的 insert
    #: 会引用一个不存在的列（PDB 报 `UndefinedColumn: name`）。
    if "rule_name" in _columns(NEW) and "name" not in _columns(NEW):
        op.alter_column(NEW, "rule_name", new_column_name="name")

    # 2) 规则包行（父）搬进来：新 id = 原 sets.id + OFFSET（避免与既有子行 id 撞）
    #: ⚠️ `set_id` 原为 NOT NULL（子行必有父）⇒ 父行插入前必须先放开，
    #: 否则 `null value in column "set_id" violates not-null constraint`。
    op.alter_column(NEW, "set_id", nullable=True)
    bind = op.get_bind()
    offset = bind.execute(sa.text(f"select coalesce(max(id), 0) from {NEW}")).scalar_one()
    bind.execute(
        sa.text(
            f"insert into {NEW} "
            "(id, parent_id, user_id, name, remark, op, params_json, enabled, sort_order, created_at, updated_at) "
            f"select :off + id, NULL, user_id, name, remark, '', '{{}}', enabled, sort_order, created_at, updated_at "
            f"from {OLD_SETS}"
        ),
        {"off": offset},
    )

    # 3) 子行回填 parent_id（原 set_id + offset）与 user_id（取父行 owner）
    bind.execute(
        sa.text(
            f"update {NEW} r set parent_id = r.set_id + :off, "
            f"user_id = s.user_id from {OLD_SETS} s where s.id = r.set_id"
        ),
        {"off": offset},
    )

    # 4) 删旧列 set_id（改用 parent_id）
    #: ⚠️ 索引名随历史版本变动（降级重建时可能没有它）⇒ 存在才删，
    #: 否则 `DROP INDEX ix_data_package_rules_set_id` 直接炸。
    if bind.execute(
        sa.text(
            "select 1 from pg_indexes where tablename = :t and indexname = 'ix_data_package_rules_set_id'"
        ),
        {"t": NEW},
    ).first():
        op.drop_index("ix_data_package_rules_set_id", table_name=NEW)
    if bind.execute(
        sa.text(
            "select 1 from pg_constraint where conname = 'data_package_rules_set_id_fkey'"
        )
    ).first():
        op.drop_constraint("data_package_rules_set_id_fkey", NEW, type_="foreignkey")
    if "set_id" in _columns(NEW):
        op.drop_column(NEW, "set_id")

    # 6) user_id 收紧非空（父行与子行都已回填）
    op.alter_column(NEW, "user_id", nullable=False)

    # 7) 唯一约束：旧的两条已随列走，新建部分索引（父 / 子各一条）
    if bind.execute(
        sa.text("select 1 from pg_constraint where conname = 'uq_dp_rules_set_name'")
    ).first():
        op.drop_constraint("uq_dp_rules_set_name", NEW, type_="unique")
    op.create_index(
        "uq_dp_rules_owner_name",
        NEW,
        ["user_id", "name"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NULL"),
    )
    op.create_index(
        "uq_dp_rules_child_name",
        NEW,
        ["parent_id", "name"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NOT NULL"),
    )

    # 8) 删旧父表
    op.execute(sa.text(f"drop table if exists {OLD_SETS} cascade"))


def downgrade() -> None:
    #: ⚠️ **不能用「旧父表存在与否」当判据** —— 升级时该表已被 drop，
    #: 降级入口必然看到「不存在」⇒ 直接 return，等于降级什么都不做
    #: （实测：降级后 rules=0 / sets=68，数据全丢）。
    #: 正确的判据是「新表已有 `parent_id` 列」= 当前处于合并后形态。
    if "parent_id" not in _columns(NEW):
        return

    bind = op.get_bind()
    op.create_table(
        OLD_SETS,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("remark", sa.String(length=500), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "name", name="uq_dp_rule_sets_user_name"),
    )
    op.create_index("ix_data_package_rule_sets_user_id", OLD_SETS, ["user_id"])

    # 父行搬回 sets（新 id 复原）
    parent_ids = [r[0] for r in bind.execute(
        sa.text(f"select id from {NEW} where parent_id is null order by id")
    ).all()]
    if parent_ids:
        offset = min(parent_ids) - 1
        bind.execute(
            sa.text(
                f"insert into {OLD_SETS} (id, user_id, name, remark, enabled, sort_order, created_at, updated_at) "
                f"select id - :off, user_id, name, coalesce(remark,''), enabled, sort_order, created_at, updated_at "
                f"from {NEW} where parent_id is null"
            ),
            {"off": offset},
        )
        # 子行恢复 set_id（先把 parent_id 映射回原 id）
        op.add_column(NEW, sa.Column("set_id", sa.Integer(), nullable=True))
        bind.execute(
            sa.text(f"update {NEW} set set_id = parent_id - :off where parent_id is not null"),
            {"off": offset},
        )
        #: ⚠️ **删父行之前必须先摘掉自引用外键**：`fk_dp_rules_parent` 是
        #: `ON DELETE CASCADE`，直接删父行会级联把所有子行一起删掉
        #: （实测：降级后 rules=0 —— 117 条规则全丢）。
        op.drop_constraint("fk_dp_rules_parent", NEW, type_="foreignkey")
        bind.execute(sa.text(f"delete from {NEW} where parent_id is null"))
        op.alter_column(NEW, "set_id", nullable=False)

    if "name" in _columns(NEW):
        op.alter_column(NEW, "name", new_column_name="rule_name")
    if "remark" in _columns(NEW):
        op.drop_column(NEW, "remark")
    if "user_id" in _columns(NEW):
        op.drop_index("ix_data_package_rules_user_id", table_name=NEW)
        op.drop_column(NEW, "user_id")
    if "parent_id" in _columns(NEW):
        op.drop_index("ix_data_package_rules_parent_id", table_name=NEW)
        op.drop_column(NEW, "parent_id")
