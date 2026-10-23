"""菜单与权限码同表（M 目录 / C 菜单 / F 按钮）—— 设计 47 §2.1。

## 一张表装三件事（这是刻意的）

| 列 | 回答什么 |
|---|---|
| `perms` | 「这个按钮 / 这页**能不能**操作」（后端 `require_perm(...)` 的取值来源） |
| `path` | 「这条菜单**跳到哪**」（必须与 `frontend/src/app/router.tsx` 登记一致） |
| `workspace` | 「它属于**哪个工作区**」（`xhs` / `douyin` / `xianyu` / `ai` / `system`） |
| `status` | 「**平台接入开关**」—— 停用一行 ⇒ 其下全部不下发（见设计 49 模块三） |

设计 48 §3.3 已裁定**不可合并**进 `dict_*` / `system_settings`：两者语义相反
（权限 ≠ 展示映射；权限 ≠ 每用户一份的 KV）。

## `parent_id` **不建外键**（这是个要解释的决定）

- `RESTRICT`：删父节点要么先手动清子行，要么每次都被 DB 报一个不带解释的错；
- `CASCADE`：删一个目录会静默删掉整棵子树，而 `role_menus` 里指向这些行的授权
  会一起消失 —— 等于**静默收回别人的权限**。

⇒ 用「有子节点即拒（409 `MENU_HAS_CHILDREN`）」+ `role_menus` 反查
（`MENU_IN_USE`）在**应用层**守，另加一条孤儿守卫测试
（`parent_id = 0 OR EXISTS(SELECT 1 FROM menus WHERE id = parent_id)`）。

## 索引口径（设计 51 §3.1）

`perms` / `path` 是**部分唯一**：M 行两列都为空 → 不能互相冲突。
⚠️ 必须 `sqlite_where` + `postgresql_where` **两边都写** —— 只写一边的后果是
「那条唯一约束在另一个方言上**根本不存在**」，且不报错（设计 58 §2.3 实测过
`platform_accounts` 的这个坑）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

#: 目录（不可选中，只做分组）
MENU_TYPE_DIR = "M"
#: 菜单 / 页面（可选中、有 `path`、**必带 `perms`**）
MENU_TYPE_MENU = "C"
#: 按钮（挂在自己所属的 C 行下，只带 `perms`）
MENU_TYPE_BUTTON = "F"
MENU_TYPES: tuple[str, ...] = (MENU_TYPE_DIR, MENU_TYPE_MENU, MENU_TYPE_BUTTON)

#: 根节点的 `parent_id` 取值（**不用 NULL** —— 树遍历时 NULL 会让
#: `parent_id = X` 漏掉根行，且 SQLite / PG 对 NULL 比较的默认行为容易写错）
ROOT_PARENT_ID = 0

MENU_STATUS_NORMAL = "normal"
MENU_STATUS_DISABLED = "disabled"
MENU_STATUSES: tuple[str, ...] = (MENU_STATUS_NORMAL, MENU_STATUS_DISABLED)


class Menu(Base):
    """菜单行（M / C / F 三型同表）。"""

    __tablename__ = "menus"
    __table_args__ = (
        #: 部分唯一：权限码全局唯一（M 行 `perms` 为空 ⇒ 条件不成立 ⇒ 互不冲突）。
        Index(
            "uq_menus_perms",
            "perms",
            unique=True,
            sqlite_where=text("perms IS NOT NULL AND perms <> ''"),
            postgresql_where=text("perms IS NOT NULL AND perms <> ''"),
        ),
        #: 部分唯一：同一路由不能挂两个菜单（M / F 行 `path` 为空）。
        Index(
            "uq_menus_path",
            "path",
            unique=True,
            sqlite_where=text("path IS NOT NULL AND path <> ''"),
            postgresql_where=text("path IS NOT NULL AND path <> ''"),
        ),
        Index("ix_menus_parent", "parent_id"),
        Index("ix_menus_workspace", "workspace"),
        CheckConstraint(
            "menu_type IN ('M', 'C', 'F')", name="ck_menus_menu_type"
        ),
        CheckConstraint(
            "status IN ('normal', 'disabled')", name="ck_menus_status"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: `0` = 根（见 `ROOT_PARENT_ID`）。自引用树，**不建外键**（见模块 docstring）。
    parent_id: Mapped[int] = mapped_column(Integer, default=ROOT_PARENT_ID, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    menu_type: Mapped[str] = mapped_column(String(1), default=MENU_TYPE_MENU, nullable=False)
    #: 路由路径 —— **仅 C 行必填**，且必须与 `router.tsx` 登记一致。
    path: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    #: 预留：XHS 不做动态 import（设计 46 D5），列留空但保留形状。
    component: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    #: 权限码 `<域>:<资源>:<动作>`。**非空即全局唯一**（见索引）。
    perms: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    #: antd 图标名（前端 `ICONS` 查表，查不到用默认）
    icon: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: **仅根 C 行有值**（`xhs` / `douyin` / `xianyu` / `ai` / `system`）
    workspace: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 隐藏 = 不进侧栏，但权限码仍生效（可从页面内跳转）
    is_visible: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=text("true")
    )
    #: **平台接入开关**：`disabled` ⇒ 该行及其下全部不下发菜单
    status: Mapped[str] = mapped_column(
        String(16), default=MENU_STATUS_NORMAL, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=shanghai_now, onupdate=shanghai_now, nullable=False
    )
