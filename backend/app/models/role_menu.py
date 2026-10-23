"""角色 ↔ 菜单授权（多对多）—— 设计 47 §2.3 / 设计 48 §3.4。

**这是 `menus` 与 `roles` 之间唯一的物理连接**。

## 两个外键的删除策略**刻意不同**

| 从 | 到 | `ondelete` | 为什么 |
|---|---|---|---|
| `role_menus.role_id` | `roles.id` | **CASCADE** | 删角色 ⇒ 它的授权行必然失去意义，一起删是对的（设计里唯一允许的级联） |
| `role_menus.menu_id` | `menus.id` | **RESTRICT** | 有授权引用的菜单不许删。**不能 CASCADE** —— 那样删一个菜单会静默收回所有人的这块权限 |
| `role_menus.user_id` | `users.id` | **CASCADE** | 见下方「为什么有 `user_id`」 |

## 为什么本表带 `user_id`（对设计 47 §5「5 张新表都不带 user_id」的例外）

设计 47 §5 的结论是「`menus` / `roles` / `role_menus` / `dict_*` **是权限的定义**，
不是某用户的数据，带 `user_id` 就会变成每用户一套菜单」。这条对本表**不成立**：

`roles` 表是**全局码表**（一轮种 2 行），但一个用户可能**自建角色**（设计 49
「要就给用户自建」）。此时 `role_menus` 的授权行属于「那个用户的角色」——
不带 `user_id` 的话，`A` 删自己的角色会级联删掉 `B` 的授权行，
而且**谁都能改别人的授权**。隔离键放在授权行上是 DEF-066「所有数据按用户隔离」
的直接推论，且不改 `roles` / `menus` 的全局性（它们仍然不带 `user_id`）。
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class RoleMenu(Base):
    """一条授权 = 「某用户的某角色可以看 / 操作某菜单行」。"""

    __tablename__ = "role_menus"
    __table_args__ = (
        UniqueConstraint("role_id", "menu_id", name="uq_role_menus_role_menu"),
        #: 反查「哪些角色引用了这个菜单」（删菜单前置检查 P8 `MENU_IN_USE`）
        Index("ix_role_menus_menu", "menu_id"),
        Index("ix_role_menus_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 授权行的归属用户（归属**角色**，不是归属菜单 —— 见模块 docstring）
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    menu_id: Mapped[int] = mapped_column(
        ForeignKey("menus.id", ondelete="RESTRICT"), nullable=False
    )
