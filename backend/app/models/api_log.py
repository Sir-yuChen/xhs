from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import String, DateTime, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


class ApiLog(Base):
    __tablename__ = "api_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    platform: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    endpoint: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), default="success")
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    #: 链路 id（W3C trace-id，32 位十六进制；设计 67 / 2026-10-25）。
    #: 一次用户操作可能触发多个请求与后台任务，靠它**串成一条链路**。
    #: 可空：迁移前的存量行没有（缺值口径：不造假）。
    trace_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    #: HTTP 状态码（v1.1.0 / 设计 48 R3，迁移 `b2c3d4e5f6a0`）。
    #: 从 `meta['status_code']` 提成真列 —— 日志页要按「200 / 403 / 500」筛选，
    #: 塞在 JSON 里既**无法建索引**也**无法 SQL 筛**。
    #: 可空：历史行里确实可能没有这个键（缺值口径：不造假）。
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
