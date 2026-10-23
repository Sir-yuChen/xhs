"""role_menus.user_id 改为可空 —— 支持「模板行」（授权默认值）

v1.1.0 / 设计 57 S2 修订（实测反馈）。

## 为什么改

`role_menus` 的两层语义：

| `user_id` | 含义 |
|---|---|
| `NULL` | **模板行** —— 「持有该角色的人默认看得见什么」，新用户注册即继承 |
| 具体 id | **用户增量** —— 该用户被单独加授 / 取消的差异 |

S1 建表时定的是 `nullable=False`（只有用户行）。实测后果：新注册用户的
`perms_for_role("user")` 返回 0 码 ⇒ **登录后侧栏空白、直输 URL 403**
（`test_seed_no_overwrite` 里那条「整目录被清空 ≠ 复活」的用例没覆盖到
「新用户」这个路径，因为它检查的是「不复活」，不是「新用户能进」）。

## 方言差异

* **PostgreSQL**：`ALTER COLUMN ... DROP NOT NULL` 一条搞定；
* **SQLite**：不支持 `ALTER COLUMN`，**必须重建表**（batch mode）。用
  `op.batch_alter_table` 让 alembic 自动处理（它在 SQLite 上会建临时表 +
  拷数据 + 换名）。⚠️ batch 模式**必须重述所有索引与唯一约束**，否则重建后
  那些索引会静默消失 —— 这是 SQLite 迁移最经典的坑（改了列，索引没了，
  查得慢但功能正常，几个月后才发现）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a1b2c3"
down_revision = "e5f6a1b2c3d4"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    try:
        return {c["name"] for c in inspector.get_columns(table)}
    except Exception:  # noqa: BLE001 - 表不存在（最小夹具）按空列集处理
        return set()


def upgrade() -> None:
    if "role_menus" not in _tables():
        return
    if "user_id" not in _columns("role_menus"):
        return

    inspector = sa.inspect(op.get_bind())
    column = next(c for c in inspector.get_columns("role_menus") if c["name"] == "user_id")
    if column.get("nullable"):
        return  # 已经是可空（PG 上手工改过 / 重跑）

    with op.batch_alter_table("role_menus", recreate="always") as batch:
        batch.alter_column("user_id", existing_type=sa.Integer(), nullable=True)

    #: batch 重建后重述索引与唯一约束 —— 不重述就被 SQLite 丢掉了。
    #: 用 `if_not_exists` 语义（先查再建），让 PG 上已经存在时也不报错。
    indexes = {i["name"] for i in insp_get_indexes("role_menus")}
    if "ix_role_menus_user" not in indexes:
        op.create_index("ix_role_menus_user", "role_menus", ["user_id"])
    if "ix_role_menus_menu" not in indexes:
        op.create_index("ix_role_menus_menu", "role_menus", ["menu_id"])

    uniques = {u["name"] for u in insp_get_uniques("role_menus")}
    if "uq_role_menus_user_role_menu" not in uniques:
        op.create_unique_constraint(
            "uq_role_menus_user_role_menu", "role_menus", ["user_id", "role_id", "menu_id"]
        )


def downgrade() -> None:
    if "role_menus" not in _tables():
        return
    #: 降级前必须先删掉模板行，否则 NOT NULL 加不回去（NULL 值违反新约束）。
    #: 这一步**丢数据**，但它正是降级语义：回到「只支持用户行」的旧结构。
    op.execute("DELETE FROM role_menus WHERE user_id IS NULL")
    with op.batch_alter_table("role_menus", recreate="always") as batch:
        batch.alter_column("user_id", existing_type=sa.Integer(), nullable=False)

    indexes = {i["name"] for i in insp_get_indexes("role_menus")}
    if "ix_role_menus_user" not in indexes:
        op.create_index("ix_role_menus_user", "role_menus", ["user_id"])
    if "ix_role_menus_menu" not in indexes:
        op.create_index("ix_role_menus_menu", "role_menus", ["menu_id"])


def insp_get_indexes(table: str) -> list[dict]:
    return sa.inspect(op.get_bind()).get_indexes(table)


def insp_get_uniques(table: str) -> list[dict]:
    return sa.inspect(op.get_bind()).get_unique_constraints(table)
