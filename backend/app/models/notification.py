from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

#: 通知级别（设计 47 §6 的 `sys_notify_level` 镜像这两个常量）。
#: ⚠️ 字典是**镜像不是权威** —— 这三个值原本只散落在 `notification_service` 的
#: `level="warning"` 字面量里（4 处），没有常量可镜像。收敛成常量是为了让
#: 「字典 ↔ 代码」的守卫有东西可比（守卫 `test_dict_mirrors_python_constants`）。
NOTIFICATION_LEVEL_INFO = "info"
NOTIFICATION_LEVEL_WARNING = "warning"
NOTIFICATION_LEVEL_ERROR = "error"
NOTIFICATION_LEVELS: tuple[str, ...] = (
    NOTIFICATION_LEVEL_INFO,
    NOTIFICATION_LEVEL_WARNING,
    NOTIFICATION_LEVEL_ERROR,
)

#: 通知来源（`sys_notify_source`）。取值 = `source_type` 实测在用的 6 种：
#: `notification_service` 的四个工厂（task / task / target / account）+
#: `llm_gateway_alert`（预警）+ `note_data_sync` / `note_data_import`（api/notes.py）。
NOTIFICATION_SOURCE_TASK = "task"
NOTIFICATION_SOURCE_ACCOUNT = "account"
NOTIFICATION_SOURCE_TARGET = "target"
NOTIFICATION_SOURCE_LLM_GATEWAY_ALERT = "llm_gateway_alert"
NOTIFICATION_SOURCE_NOTE_DATA_SYNC = "note_data_sync"
NOTIFICATION_SOURCE_NOTE_DATA_IMPORT = "note_data_import"
NOTIFICATION_SOURCE_TYPES: tuple[str, ...] = (
    NOTIFICATION_SOURCE_TASK,
    NOTIFICATION_SOURCE_ACCOUNT,
    NOTIFICATION_SOURCE_TARGET,
    NOTIFICATION_SOURCE_LLM_GATEWAY_ALERT,
    NOTIFICATION_SOURCE_NOTE_DATA_SYNC,
    NOTIFICATION_SOURCE_NOTE_DATA_IMPORT,
)


class Notification(Base):
    __tablename__ = "sys_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_users.id"), index=True)
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text, default="")
    level: Mapped[str] = mapped_column(String(16), default="info")
    source_task_id: Mapped[Optional[int]] = mapped_column(ForeignKey("queue_jobs.id"), nullable=True)
    source_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: 点击跳转路径（v1.1.0 / 设计 48 R4，迁移 `b2c3d4e5f6a0`）。
    #: 历史行**留空**（不造假：4 条存量里 3 条 `source_id` 为 NULL，本来就没有
    #: 可靠跳转目标）；前端 `lib/notification-routes.ts` 的映射表保留作兜底。
    link: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
