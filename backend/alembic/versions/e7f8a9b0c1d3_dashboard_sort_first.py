"""总览排序提前到工作区第一项 —— 存量库对齐（设计 62 菜单树梳理）

2026-10-25 用户口径「菜单树全面梳理」：总览是工作区的**第一项**
（排在全部目录组之前）。种子生成器已产出 `sort_order=-10`，但菜单播种是
「**只补缺不覆盖**」（已存在的行不碰）⇒ 存量库的这一行还是 100，
侧栏里总览仍排在目录组之后 —— 必须由迁移显式对齐。

⚠️ 只动这一行（`path` 唯一索引保证精确命中）。新装库由新种子直接建对，
本迁移对它是幂等 UPDATE（重复执行值不变）。

幂等：是。可逆：改回 100（与历史值一致 —— 迁移 c3d4e5f6a1b2 播种时的值）。

Revision ID: e7f8a9b0c1d3
Revises: d6e7f8a9b0c2
"""

from __future__ import annotations

from alembic import op

revision = "e7f8a9b0c1d3"
down_revision = "d6e7f8a9b0c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE menus SET sort_order = -10 "
        "WHERE path = '/platforms/xhs/dashboard' AND menu_type = 'C'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE menus SET sort_order = 100 "
        "WHERE path = '/platforms/xhs/dashboard' AND menu_type = 'C'"
    )
