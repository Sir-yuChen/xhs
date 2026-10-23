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

⚠️ **但要注意「自建角色」不等于「同名角色」**：`roles.code` 是**全局唯一**的
（`Role.code` 上 `unique=True`），所以 A 和 B 不可能各有一个 `code='ops'` 的角色。
⇒ 「带 `user_id`」的**真实收益**是：① 删角色时级联删的是**归属者**的授权行；
② 授权变更的审计能追到「谁改的」；③ 不会出现「B 的授权被 A 的删除动作带走」。
唯一键因此必须是 `(user_id, role_id, menu_id)` —— 只约束 `(role_id, menu_id)`
会让同一角色在同一菜单上出现「同 role 同 menu 不同 user」的语义重复行。
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class RoleMenu(Base):
    """一条授权 = 「某用户的某角色可以看 / 操作某菜单行」。"""

    __tablename__ = "role_menus"
    __table_args__ = (
        #: ⚠️ **唯一键必须含 `user_id`**（2026-10-23 修正）：本表带 `user_id` 就说明
        #: 授权行是**按用户隔离**的（见下方「为什么本表带 user_id」）。只约束
        #: `(role_id, menu_id)` 会造成两种错误：
        #: ① 若角色真能按用户重名 ⇒ A 给角色授菜单 X，B 也授 ⇒ **唯一键冲突，B 保存失败**；
        #: ② 若角色全局唯一（当前 `roles.code` 是 unique 的）⇒ 两行同菜单的授权必然
        #:    「同 role + 同 menu + 不同 user」，那是**语义重复的脏行**，不应当能插进去。
        #: 两种情形都指向同一结论：唯一键 = `(user_id, role_id, menu_id)`。
        UniqueConstraint("user_id", "role_id", "menu_id", name="uq_role_menus_user_role_menu"),
        #: 反查「哪些角色引用了这个菜单」（删菜单前置检查 P8 `MENU_IN_USE`）
        Index("ix_role_menus_menu", "menu_id"),
        Index("ix_role_menus_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 授权行的归属用户。**可空**（2026-10-24 修订）：
    #:
    #: | 值 | 含义 |
    #: |---|---|
    #: | `NULL` | **模板行** —— 「持有该角色的人默认看得见什么」。新用户注册即继承 |
    #: | 具体 id | **用户增量** —— 该用户被单独加授/取消的差异 |
    #:
    #: ⚠️ 初版是 `nullable=False`（只有用户行），实测后果：模板行插不进去，
    #: `perms_for_role("user")` 恒 0 码 ⇒ **新注册用户登录后侧栏空白**。
    #: 「默认可见」必须能表达成一条不属于任何人的数据，否则每来一个新用户
    #: 都要补一次播种，而那一步永远会被忘掉。
    #:
    #: 归属**角色**而不是菜单（见模块 docstring）。
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    menu_id: Mapped[int] = mapped_column(
        ForeignKey("menus.id", ondelete="RESTRICT"), nullable=False
    )
