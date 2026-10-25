"""字典 list_class 白名单放宽到 antd 全量预设色（设计 63 §2.3）

2026-10-25 用户口径「所有枚举全部使用字典配置、动态实时可变」：前端既有枚举
颜色用 green/orange/gold/cyan/purple 这类 **antd 预设色**，原
`ck_dict_items_list_class` 只放行 5 档状态色（default/success/warning/danger/info）
⇒ 字典表达不了真实颜色。放宽到 5 状态色 + processing/error + 13 预设色。

⚠️ 存量行全部在旧值域内（种子只种过 5 档）⇒ 零回填、零数据改动，
只换约束本身。新装库由 `models/dict.py` 的 CheckConstraint 直接建新约束，
本迁移对它幂等。

⚠️ 双方言（设计 51 / 迁移 c1d2e3f4a5b7 同款先例）：PG 走原生
`ALTER TABLE ... DROP/ADD CONSTRAINT`；**SQLite 不支持 DROP CONSTRAINT**
（测试夹具会在 SQLite 上跑全链）⇒ batch_alter_table 重建表换约束。
幂等：是。可逆：换回 5 档约束（存量行都在旧值域内，能换回）。

Revision ID: c9b8d7e6f5a4
Revises: e7f8a9b0c1d3
"""

from __future__ import annotations

from alembic import op

revision = "c9b8d7e6f5a4"
down_revision = "e7f8a9b0c1d3"
branch_labels = None
depends_on = None

_OLD_VALUES = "'default', 'success', 'warning', 'danger', 'info'"
_NEW_VALUES = (
    "'default', 'success', 'warning', 'danger', 'info', 'processing', 'error', "
    "'red', 'volcano', 'orange', 'gold', 'yellow', 'lime', 'green', 'cyan', "
    "'blue', 'geekblue', 'purple', 'magenta', 'pink'"
)


def _swap_constraint(new_values_sql: str) -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("dict_items") as batch:
            batch.drop_constraint("ck_dict_items_list_class", type_="check")
            batch.create_check_constraint(
                "ck_dict_items_list_class", f"list_class IN ({new_values_sql})"
            )
        return
    op.execute("ALTER TABLE dict_items DROP CONSTRAINT IF EXISTS ck_dict_items_list_class")
    op.execute(
        f"ALTER TABLE dict_items ADD CONSTRAINT ck_dict_items_list_class "
        f"CHECK (list_class IN ({new_values_sql}))"
    )


def upgrade() -> None:
    _swap_constraint(_NEW_VALUES)


def downgrade() -> None:
    _swap_constraint(_OLD_VALUES)
