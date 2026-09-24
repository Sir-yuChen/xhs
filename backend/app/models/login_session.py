from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


class LoginSession(Base):
    __tablename__ = "login_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    sub_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    login_method: Mapped[str] = mapped_column(String(32), default="qr")
    #: ~~`phone_mask`~~ —— **列已移除**（迁移 `x7y8z9a0b1c2`，2026-09-23）。
    #: 原因：用户口径「登录时手机号和邮箱密码等均不落库，数据库只存登录成功后的
    #: 账号信息和会话临牌」。连掩码都不留：该字段只写不读（前后端零消费方），
    #: 而「掩码」仍是手机号的派生值。短信验证码同样从不落库（只随 confirm
    #: 请求透传给平台）。
    qr_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    code: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    qr_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    encrypted_temp_cookies: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
