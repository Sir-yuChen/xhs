"""账号管理提到小红书一级 + 移除「平台」目录

Revision ID: n1e2x3t4m5u6
Revises: r8t9y0u1i2o3, a5b6c7d8e9f1

侧栏信息架构调整：`sys_menus` 里「平台」目录（M 行）退场，其下唯一的存活页面
（`/platforms/xhs/accounts`）改名「账号管理」并挂到小红书工作区根下，
排在「总览」（`sort_order=-10`）之后。

判据一律用 **path**：id 在不同库里不一致（本地导出 / 重装 / 播种重建都会变）。

幂等：重复执行只改 0 行。可逆：downgrade 重建「平台」M 行并把账号页挂回去。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "n1e2x3t4m5u6"
down_revision: Union[str, Sequence[str], None] = ("r8t9y0u1i2o3", "a5b6c7d8e9f1")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 小红书工作区根 M 行（按 workspace 定位，不按 id）
XHS_WORKSPACE = "xhs"
#: 要拆掉的目录名（同工作区根下按名字定位）
PLATFORM_DIR_NAME = "平台"
#: 账号页：path 唯一，改名 + 重挂
ACCOUNTS_PATH = "/platforms/xhs/accounts"
ACCOUNTS_NAME = "账号管理"
ACCOUNTS_SORT = 0


def _has_menus(bind) -> bool:
    return "sys_menus" in set(sa.inspect(bind).get_table_names())


def _now_sql() -> str:
    """时间戳的方言无关写法（PG / SQLite 都认）。"""
    return "CURRENT_TIMESTAMP"


def _xhs_root_id(bind) -> int | None:
    return bind.execute(
        sa.text(
            "SELECT id FROM sys_menus WHERE menu_type = 'M' AND parent_id = 0 "
            "AND workspace = :ws ORDER BY id LIMIT 1"
        ),
        {"ws": XHS_WORKSPACE},
    ).scalar()


def _platform_dir_id(bind, root_id: int) -> int | None:
    return bind.execute(
        sa.text(
            "SELECT id FROM sys_menus WHERE menu_type = 'M' AND parent_id = :root "
            "AND name = :name ORDER BY id LIMIT 1"
        ),
        {"root": root_id, "name": PLATFORM_DIR_NAME},
    ).scalar()


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_menus(bind):
        return

    root_id = _xhs_root_id(bind)
    if root_id is None:
        return

    # 1. 账号页改名 + 提到工作区根（顺序不能反：先改名再重挂，两个动作同一条 UPDATE，
    #    重挂只针对这一行，不靠「把目录下所有子行挂到根」兜底）。
    bind.execute(
        sa.text(
            "UPDATE sys_menus SET name = :name, parent_id = :root, sort_order = :sort "
            "WHERE path = :path"
        ),
        {
            "name": ACCOUNTS_NAME,
            "root": root_id,
            "sort": ACCOUNTS_SORT,
            "path": ACCOUNTS_PATH,
        },
    )

    # 2. 删「平台」目录 M 行 —— 先清它的授权行（`sys_role_menus.menu_id` 是 RESTRICT
    #    外键，直接删会撞约束）。目录此时应当已没有任何子行（唯一子行是账号页，第 1 步
    #    已搬到根）；**还有子行就报错**而不是顺手搬 —— 顺手搬会让第 1 步的重挂变成
    #    可有可无，而这正是「账号页到底挂哪儿」的判据。
    dir_id = _platform_dir_id(bind, root_id)
    if dir_id is not None:
        leftover = bind.execute(
            sa.text("SELECT count(*) FROM sys_menus WHERE parent_id = :mid"), {"mid": dir_id}
        ).scalar()
        if leftover:
            raise RuntimeError(
                f"「{PLATFORM_DIR_NAME}」目录下还有 {leftover} 条子行未迁移；"
                f"迁移只负责账号页（{ACCOUNTS_PATH}），其余请先手工归位"
            )
        bind.execute(sa.text("DELETE FROM sys_role_menus WHERE menu_id = :mid"), {"mid": dir_id})
        bind.execute(sa.text("DELETE FROM sys_menus WHERE id = :mid"), {"mid": dir_id})

    # 3. 清孤儿按钮行：父菜单已退场的 F 行（监控 / 自动运营 / 关键词组 / 话题）
    #    没有页面可挂，留在库里只会被菜单树当成顶级项渲染出去。
    bind.execute(
        sa.text(
            "DELETE FROM sys_role_menus WHERE menu_id IN ("
            "SELECT id FROM sys_menus WHERE menu_type = 'F' AND parent_id <> 0 "
            "AND parent_id NOT IN (SELECT id FROM sys_menus))"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM sys_menus WHERE menu_type = 'F' AND parent_id <> 0 "
            "AND parent_id NOT IN (SELECT id FROM sys_menus)"
        )
    )


def downgrade() -> None:
    """把账号页挂回「平台」目录（目录不存在就重建），名字改回「账号矩阵」。"""
    bind = op.get_bind()
    if not _has_menus(bind):
        return

    root_id = _xhs_root_id(bind)
    if root_id is None:
        return

    #: ⚠️ 必须给全列默认值：`id` / `created_at` / `updated_at` 是 NOT NULL 且**无 DB 默认**
    #: （id 由序列给）—— 漏给任何一个，down 就会撞 NotNullViolation。
    dir_id = _platform_dir_id(bind, root_id)
    if dir_id is None:
        dir_id = bind.execute(
            sa.text(
                "INSERT INTO sys_menus (parent_id, name, menu_type, path, component, perms, "
                "icon, workspace, sort_order, is_visible, status, created_at, updated_at) "
                "VALUES (:root, :name, 'M', '', '', '', 'safety-certificate', :ws, 0, true, "
                "'normal', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) RETURNING id"
            ),
            {"root": root_id, "name": PLATFORM_DIR_NAME, "ws": XHS_WORKSPACE},
        ).scalar()

    bind.execute(
        sa.text(
            "UPDATE sys_menus SET name = :name, parent_id = :dir, sort_order = 0 WHERE path = :path"
        ),
        {"name": ACCOUNTS_NAME, "dir": dir_id, "path": ACCOUNTS_PATH},
    )
