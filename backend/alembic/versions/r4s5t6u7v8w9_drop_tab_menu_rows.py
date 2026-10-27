"""删除「页签伪装成的菜单项」（笔记工坊 6 个子项）

Revision ID: r4s5t6u7v8w9
Revises: q3r4s5t6u7v8
Create Date: 2026-10-27

页签只是同一页面的不同视图，不该各占一个菜单项与一份授权行
（原来一个页面在侧栏里长出 6 个子项，授权行 1 条变 7 条）。

本迁移做两件事，**顺序不能反**（否则外键拦）：
1. 删 `role_menus` 里指向这些行的授权行；
2. 删 `menus` 里这 6 行本身。

⚠️ 不动宿主行 `/platforms/xhs/note-workshop`（权限码 `xhs:workshop:view` 挂在它上面）。
⚠️ 判据用 `path LIKE '%?tab=%'` 而非 `name` —— 名字是用户可改字段。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "r4s5t6u7v8w9"
down_revision = "q3r4s5t6u7v8"
branch_labels = None
depends_on = None

#: 页签行的判据（path 带 `?tab=`）—— **不用名字**，名字可被用户改。
TAB_PATH_PREFIX = "/platforms/xhs/note-workshop"
TAB_PATH_MARK = "?tab="


def _tab_menu_ids(bind: sa.engine.Connection) -> list[int]:
    rows = bind.execute(
        sa.text(
            "SELECT id FROM menus "
            "WHERE path LIKE :prefix AND path LIKE :mark"
        ),
        {"prefix": f"{TAB_PATH_PREFIX}%", "mark": f"%{TAB_PATH_MARK}%"},
    ).scalars().all()
    return list(rows)


def upgrade() -> None:
    bind = op.get_bind()
    ids = _tab_menu_ids(bind)
    if not ids:
        return
    #: ① 先删授权行（外键从表）。
    bind.execute(
        sa.text("DELETE FROM role_menus WHERE menu_id IN :ids").bindparams(
            sa.bindparam("ids", expanding=True)
        ),
        {"ids": ids},
    )
    #: ② 再删菜单行本体。
    bind.execute(
        sa.text("DELETE FROM menus WHERE id IN :ids").bindparams(
            sa.bindparam("ids", expanding=True)
        ),
        {"ids": ids},
    )


def downgrade() -> None:
    """**不回填**（刻意）。

    回填要做三件事：重插 6 行、找回它们的父 id、给每个角色补 6 条授权 —— 而「该给哪些
    角色授权」的回溯信息已经在下行里删掉了，猜一个默认值（全员授权）等于**扩权**。
    需要恢复时走正向：改 `generate_menu_seed.py` 的 `_TAB_CHILDREN` 并重新播种，
    那条路径是有意为之、可复核的。
    """
    return
