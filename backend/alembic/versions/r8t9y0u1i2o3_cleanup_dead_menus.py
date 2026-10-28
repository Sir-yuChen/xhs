"""clean up dead menu rows pointing at removed pages

Revision ID: r8t9y0u1i2o3
Revises: q7w8e9r0t1y2

2026-10-28 用户报「商品洞察页未删除，点击空白页」。

根因：页面与前端路由已退场，但 `sys_menus` 里的菜单行还留着 —— 点进去没有
匹配的路由 ⇒ 空白页。这不是单条问题，全量对账（菜单 `path` vs `router.tsx`
实际路由）查出 6 条死链：

| 菜单 | 旧路径 | 处理 |
|---|---|---|
| 商品洞察 | `/platforms/xhs/insights/shop` | 删（页面已删） |
| 内容洞察 | `/platforms/xhs/insights/content` | 删（页面已删） |
| 监控中心 | `/platforms/xhs/monitoring` | 删（页面已删） |
| 自动运营 | `/platforms/xhs/auto-ops` | 删（随 `auto_tasks` 退场） |
| 关键词组 | `/platforms/xhs/keywords` | 删（页面已删） |
| 用量看板 | `/usage-dashboard` | **改** `/llm-gateway/usage`（实际是网关子路由） |
| 服务商 | `/llm-gateway/providers` | **改** `/llm-gateway/gateways` |
| 个人资料 | `/profile` | 删（走头像下拉，不走路由） |

同时清 `sys_role_menus` 里指向这些菜单的授权行。

⚠️ 用**路径**定位而不是硬编码 id：id 在不同库里不一致（本地导出 / 重装 / 播种
重建都能变），路径才是稳定判据。找不到就不动（幂等，重复执行无副作用）。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r8t9y0u1i2o3"
down_revision: Union[str, None] = "q7w8e9r0t1y2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 要删的菜单路径（页面已退场）。
DEAD_PATHS = (
    "/platforms/xhs/insights/shop",
    "/platforms/xhs/insights/content",
    "/platforms/xhs/monitoring",
    "/platforms/xhs/auto-ops",
    "/platforms/xhs/keywords",
    "/profile",
)

#: 要改的菜单（路径写错，指向不存在的路由）。
FIX_PATHS = (
    ("/usage-dashboard", "/llm-gateway/usage"),
    ("/llm-gateway/providers", "/llm-gateway/gateways"),
)


def _has_menus(bind) -> bool:
    return "sys_menus" in set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_menus(bind):
        return

    # ---- 1. 删死链菜单（先清授权行，菜单行无外键级联）----
    for path in DEAD_PATHS:
        menu_ids = [
            r[0]
            for r in bind.execute(
                sa.text("SELECT id FROM sys_menus WHERE path = :p"), {"p": path}
            ).all()
        ]
        if not menu_ids:
            continue
        bind.execute(
            sa.text("DELETE FROM sys_role_menus WHERE menu_id = ANY(:ids)"),
            {"ids": menu_ids},
        )
        bind.execute(
            sa.text("DELETE FROM sys_menus WHERE id = ANY(:ids)"), {"ids": menu_ids}
        )

    # ---- 2. 修正指错路的菜单 ----
    for old, new in FIX_PATHS:
        bind.execute(
            sa.text(
                "UPDATE sys_menus SET path = :new, updated_at = now() WHERE path = :old"
            ),
            {"old": old, "new": new},
        )


def downgrade() -> None:
    """不回滚：删掉的菜单指向的是**已被删除的页面**，恢复只会让空白页重现。

    修正过的两条路径也不回滚（旧路径本身就不存在。
    """
