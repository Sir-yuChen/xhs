"""merge dict_types + dict_items into dict_rows

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4

2026-10-28 用户口径「dict_types 和 dict_items 合并一张表，父子，懒加载，项目骨架基础表」。

`dict_types`（46 行）+ `dict_items`（219 行）⇒ `dict_rows`，`row_type` 分型：

* `row_type='type'` ⇒ 类型行，`parent_id` 恒 NULL
* `row_type='item'` ⇒ 项行，`parent_id` 指类型行（原 `type_id`）

列合并：`type_id` → `parent_id`；`type_code` 保留为冗余列（免 join 高频读）。
`dict_items` 原本没有 `created_at`/`updated_at`，补上（写侧已用 `shanghai_now()`）。

⚠️ **唯一约束必须改成部分唯一索引**：普通 UNIQUE(code) 会让所有项行的空串
`code` 互撞；UNIQUE(parent_id, value) 对类型行（parent_id 为 NULL）本就不生效，
但为了让语义显式，两条都用带 `WHERE` 的部分索引。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, None] = "e9f0a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_TYPES = "dict_types"
OLD_ITEMS = "dict_items"
NEW = "dict_rows"

LIST_CLASS_SQL = (
    "list_class IN ('default', 'success', 'warning', 'danger', 'info', "
    "'processing', 'error', 'red', 'volcano', 'orange', 'gold', 'yellow', "
    "'lime', 'green', 'cyan', 'blue', 'geekblue', 'purple', 'magenta', 'pink')"
)


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()

    if NEW not in _tables(bind):
        op.create_table(
            NEW,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("row_type", sa.String(8), nullable=False),
            sa.Column(
                "parent_id",
                sa.Integer(),
                sa.ForeignKey(f"{NEW}.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("name", sa.String(64), nullable=False, server_default=""),
            sa.Column("code", sa.String(64), nullable=False, server_default=""),
            sa.Column("type_code", sa.String(64), nullable=False, server_default=""),
            sa.Column("label", sa.String(64), nullable=False, server_default=""),
            sa.Column("value", sa.String(64), nullable=False, server_default=""),
            sa.Column(
                "list_class", sa.String(16), nullable=False, server_default="default"
            ),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(16), nullable=False, server_default="normal"),
            sa.Column("remark", sa.String(255), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('normal', 'disabled')", name="ck_dict_rows_status"
            ),
            sa.CheckConstraint(
                "row_type IN ('type', 'item')", name="ck_dict_rows_row_type"
            ),
            sa.CheckConstraint(LIST_CLASS_SQL, name="ck_dict_rows_list_class"),
        )
        op.create_index("ix_dict_rows_row_type", NEW, ["row_type"])
        op.create_index("ix_dict_rows_code", NEW, ["code"])
        op.create_index("ix_dict_rows_type_code", NEW, ["type_code"])
        op.create_index("ix_dict_rows_parent", NEW, ["parent_id"])
        #: 类型行 code 唯一（部分索引：项行不参与）。
        op.execute(
            f'CREATE UNIQUE INDEX uq_dict_rows_type_code ON {NEW} (code) '
            f"WHERE row_type = 'type'"
        )
        #: 项行 (父, value) 唯一（部分索引：类型行 parent_id 为 NULL 本就不参与，
        #: 显式写 WHERE 让意图可读）。
        op.execute(
            f'CREATE UNIQUE INDEX uq_dict_rows_parent_value ON {NEW} (parent_id, value) '
            f"WHERE row_type = 'item'"
        )

    # ---- 搬类型行（保留原 id）----
    tables = _tables(bind)
    if OLD_TYPES in tables:
        op.execute(
            f"""
            INSERT INTO {NEW} (
                id, row_type, parent_id, name, code, type_code, label, value,
                list_class, sort_order, status, remark, created_at, updated_at
            )
            SELECT
                t.id, 'type', NULL, t.name, t.code, '', '', '',
                'default', 0, t.status, t.remark,
                t.created_at, t.updated_at
            FROM {OLD_TYPES} t
            WHERE NOT EXISTS (SELECT 1 FROM {NEW} n WHERE n.id = t.id)
            """
        )
        op.execute(
            f"""SELECT setval(pg_get_serial_sequence('{NEW}','id'),
                 GREATEST(COALESCE((SELECT MAX(id) FROM {NEW}),1),1))"""
        )

    # ---- 搬项行（新分配 id，parent_id 指类型行）----
    if OLD_ITEMS in _tables(bind):
        op.execute(
            f"""
            INSERT INTO {NEW} (
                row_type, parent_id, name, code, type_code, label, value,
                list_class, sort_order, status, remark, created_at, updated_at
            )
            SELECT
                'item', i.type_id, '', '', i.type_code, i.label, i.value,
                i.list_class, i.sort_order, i.status, i.remark,
                NOW(), NOW()
            FROM {OLD_ITEMS} i
            WHERE NOT EXISTS (
                SELECT 1 FROM {NEW} n
                WHERE n.row_type = 'item'
                  AND n.parent_id = i.type_id
                  AND n.value = i.value
            )
            """
        )

    # ---- 删旧表 ----
    for old in (OLD_ITEMS, OLD_TYPES):
        if old in _tables(bind):
            op.drop_table(old)


def downgrade() -> None:
    bind = op.get_bind()

    if OLD_TYPES not in _tables(bind):
        op.create_table(
            OLD_TYPES,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(64), nullable=False, server_default=""),
            sa.Column("code", sa.String(64), nullable=False),
            sa.Column("remark", sa.String(255), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="normal"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_dict_types_code", OLD_TYPES, ["code"], unique=True)

    if OLD_ITEMS not in _tables(bind):
        op.create_table(
            OLD_ITEMS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "type_id",
                sa.Integer(),
                sa.ForeignKey(f"{OLD_TYPES}.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("type_code", sa.String(64), nullable=False, server_default=""),
            sa.Column("label", sa.String(64), nullable=False, server_default=""),
            sa.Column("value", sa.String(64), nullable=False, server_default=""),
            sa.Column(
                "list_class", sa.String(16), nullable=False, server_default="default"
            ),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(16), nullable=False, server_default="normal"),
            sa.Column("remark", sa.String(255), nullable=False, server_default=""),
            sa.UniqueConstraint("type_id", "value", name="uq_dict_items_type_value"),
        )
        op.create_index("ix_dict_items_code", OLD_ITEMS, ["type_code"])

    if NEW in _tables(bind):
        op.execute(
            f"""
            INSERT INTO {OLD_TYPES} (id, name, code, remark, status, created_at, updated_at)
            SELECT id, name, code, remark, status, created_at, updated_at
            FROM {NEW} WHERE row_type = 'type'
              AND NOT EXISTS (SELECT 1 FROM {OLD_TYPES} o WHERE o.id = {NEW}.id)
            """
        )
        op.execute(
            f"""
            INSERT INTO {OLD_ITEMS} (type_id, type_code, label, value, list_class,
                                     sort_order, status, remark)
            SELECT parent_id, type_code, label, value, list_class, sort_order, status, remark
            FROM {NEW} WHERE row_type = 'item' AND parent_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM {OLD_ITEMS} o
                  WHERE o.type_id = {NEW}.parent_id AND o.value = {NEW}.value
              )
            """
        )
        op.drop_table(NEW)
