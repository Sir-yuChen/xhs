from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.roles import DEFAULT_ROLE
from backend.app.core.time import shanghai_now

#: 用户状态（设计 47 §2.6）：`pending` = 注册待审（设计 46 P11 / A1），
#: `disabled` = 停用（登录时返回 **403**，不是 401 —— 401 会被前端当成「密码错」）。
USER_STATUS_NORMAL = "normal"
USER_STATUS_PENDING = "pending"
USER_STATUS_DISABLED = "disabled"
USER_STATUSES: tuple[str, ...] = (USER_STATUS_NORMAL, USER_STATUS_PENDING, USER_STATUS_DISABLED)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        #: ⚠️ 用 `CheckConstraint` 而**不是** PG 原生 `ENUM`（设计 51 §3.1）：
        #: 原生 ENUM 要额外 `CREATE TYPE`，迁移回退麻烦，SQLite 根本没有。
        CheckConstraint(
            "status IN ('normal', 'pending', 'disabled')", name="ck_users_status"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    # 系统登录用户的角色（admin / user）。见 core/roles.py。
    # 注意与 platform_accounts.sub_type 正交：那个是小红书账号的协议链路。
    # server_default 让既有行在迁移后立即有值，避免读出 NULL 需要处处判空。
    role: Mapped[str] = mapped_column(String(32), default=DEFAULT_ROLE, server_default=DEFAULT_ROLE, index=True)
    # 令牌版本（DEF-022 / T-19，2026-10-04）：改密 / 登出 / 强制下线时 +1，
    # 让**所有已签发的 refresh token 立即失效**（无状态令牌唯一的撤销手段）。
    # 迁移 `b3c4d5e6f7a8`，server_default='0' 让既有行立即可用、读出不必判空。
    token_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)

    # ------------------------------------------------------------------ #
    # 系统管理域 +6 列（v1.1.0 / 设计 47 §2.6，迁移 `a1b2c3d4e5f9`）
    #
    # 全部给 `server_default`：既有行升级后立刻可读，不必处处判 NULL。
    # ⚠️ `last_login_at` **刻意不给 server_default**：从未登录 = NULL（缺值口径
    #    硬约束⑤「不造假」），写 0 或 now 都会让用户列表显示一个假时间。
    # ------------------------------------------------------------------ #
    #: `normal` / `pending`（注册待审）/ `disabled`（停用）
    status: Mapped[str] = mapped_column(
        String(16), default=USER_STATUS_NORMAL, server_default=USER_STATUS_NORMAL,
        nullable=False, index=True,
    )
    #: 姓名（用户管理页展示）
    real_name: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)
    #: 最后登录 IP —— IPv6 最长 45 字符，留 64
    last_login_ip: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)
    #: 最后登录时间。**从未登录 = NULL**（不造假）
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: 首次登录强制改密。种子 `admin/admin123` 为 True，改密后置 False。
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: 软删（设计 49 B9 定稿，2026-10-23 用户「按照推荐」）。与
    #: `platform_accounts.deleted_at` 同口径：49 张表按 `user_id` 关联，
    #: 物理删会留下孤儿行或撞 FK，且答不了「这人是被删了还是改过名」。
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
