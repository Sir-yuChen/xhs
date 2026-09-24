from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.roles import DEFAULT_ROLE
from backend.app.core.time import shanghai_now


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    # 系统登录用户的角色（admin / user）。见 core/roles.py。
    # 注意与 platform_accounts.sub_type 正交：那个是小红书账号的协议链路。
    # server_default 让既有行在迁移后立即有值，避免读出 NULL 需要处处判空。
    role: Mapped[str] = mapped_column(String(32), default=DEFAULT_ROLE, server_default=DEFAULT_ROLE, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
