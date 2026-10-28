"""角色（码表，只 2 行种子）—— 设计 47 §2.2 / 设计 48 §3.1。

## 为什么必须新建（而不是只用 `users.role`）

`users.role` 只存一个 code 字符串，**放不下** `is_system`（内置禁删）/
`name`（中文名）/ `description` / `status` 四样。而这四样正是「角色管理页」
的全部内容。

⇒ `users.role` **保留不动**（值域外键 → `roles.code`，无 DDL FK），
`roles` 只做码表。**不新建 `user_roles`**：一人一角色（设计 48 M5，
实测 26 个用户全是单角色）。

## 内置两条

`admin` / `user` 都 `is_system=True` ⇒ 禁删 / 禁停 / 禁改 `code` / 禁改授权。
`admin` 的权限**不走 `role_menus`**（`core/perms.role_perms("admin")` 直接返回
`ALL_PERMS`），理由见 `core/perms.py` 模块 docstring 第 1 条。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

ROLE_STATUS_NORMAL = "normal"
ROLE_STATUS_DISABLED = "disabled"
ROLE_STATUSES: tuple[str, ...] = (ROLE_STATUS_NORMAL, ROLE_STATUS_DISABLED)


class Role(Base):
    """角色行。`code` 是 `users.role` 的值域（应用层校验 + 守卫测试）。"""

    __tablename__ = "sys_roles"
    __table_args__ = (
        CheckConstraint("status IN ('normal', 'disabled')", name="ck_roles_status"),
        #: 内置角色**必须启用** —— 停用一个内置角色等于把一整类用户锁在门外，
        #: 而且唯一能改回来的入口（角色管理）本身需要权限。
        #: 用 `NOT is_system OR ...` 表达蕴含关系（`is_system=false ⇒ 不约束`）。
        #: ⚠️ **2026-10-23 实测踩坑**：初版写 `is_system = 0 OR status = 'normal'`，
        #: SQLite 能跑（布尔就是 0/1），**PG 上直接失败**：
        #: `psycopg.errors.UndefinedFunction: 操作符不存在: boolean = integer`
        #: ⇒ 建表语句整体失败 ⇒ `roles` 表根本建不出来。
        #: 布尔列在 CHECK 里必须用**布尔字面量语义**（`NOT is_system`），不能跟 0/1 比。
        CheckConstraint(
            "NOT is_system OR status = 'normal'", name="ck_roles_system_stays_enabled"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: **唯一**。小写字母数字下划线；内置 `admin` / `user`
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    #: 内置角色：禁删 / 禁停 / 禁改 code / 禁改授权
    is_system: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    status: Mapped[str] = mapped_column(
        String(16), default=ROLE_STATUS_NORMAL, nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=shanghai_now, onupdate=shanghai_now, nullable=False
    )
