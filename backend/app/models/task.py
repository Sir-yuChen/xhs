from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


class Task(Base):
    __tablename__ = "tasks"
    #: 批次号唯一索引：与迁移 `f5a6b7c8d9e0` **同名同形**（`create_all` 建的测试库与
    #: alembic 建的真库必须长得一样，否则「测试绿、真机炸」）。NULL 互不相等 ⇒
    #: 非批次行（绝大多数）不受约束。
    __table_args__ = (Index("ux_tasks_batch_no", "batch_no", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    task_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    parent_task_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    #: 队列**批次号**（2026-09-26 用户口径「生成唯一批次号」）：只有批次父行有值，
    #: 普通行 / 队列项为 NULL（唯一索引对 NULL 不生效 ⇒ 不误伤存量行）。
    #: 形如 `NB20260926153324-7K2M`，唯一性由 `ux_tasks_batch_no` 在 SQL 层保证。
    batch_no: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
