"""profile 菜单行 workspace 改 common —— 个人资料移出平台侧栏（设计 62）

2026-10-25 用户口径：
* 「个人资料应在头像下拉中展示」（不进任何平台侧栏）；
* 「各个平台是隔离的，不要混乱在一起」。

`menus/mine?workspace=<ws>` 按行的 `workspace` 精确过滤（含祖先保链）——
把 `/profile` 行的 workspace 从 `xhs` 改为 `common` 后，它天然从**所有**
工作区侧栏消失，入口改由壳层头像下拉直达（页面与权限码 `xhs:profile:view`
不动，权限码↔菜单行对账保持）。

⚠️ 为什么是迁移而不是只改种子：菜单播种是「**只补缺不覆盖**」（按 perms/path
判重，已存在的行一律不碰）—— 存量库里的这行不会被新种子改写，必须由迁移
显式 UPDATE。新装库由新种子直接建对。

幂等：UPDATE 是幂等的（重复执行 0 行变化）。可逆：改回 'xhs'。

Revision ID: d6e7f8a9b0c2
Revises: d5e6f7a8b9c1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d6e7f8a9b0c2"
down_revision = "d5e6f7a8b9c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE menus SET workspace = 'common' "
        "WHERE path = '/profile' AND menu_type = 'C'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE menus SET workspace = 'xhs' "
        "WHERE path = '/profile' AND menu_type = 'C'"
    )
