"""通知域响应模型（设计 61 §A6 第二期）。

字段逐字段抄自 `api/notifications.py::serialize_notification()`（:17-27）
与各端点的 return。分页信封形状对齐 `schemas/common.paginated_with_total`：
`{total, page, page_size, items}`。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class NotificationItem(BaseModel):
    """一条站内通知（`serialize_notification()` 的产物）。"""

    id: int
    title: str = ""
    body: str = ""
    #: `info` / `warning` / `error`（对齐 `dict` 的 `sys_notify_level`）
    level: str = "info"
    #: 触发的任务 id（手工产生的通知为 None）
    source_task_id: int | None = None
    #: 来源域（`sync` / `publish` / `llm_gateway` …）
    #: ⚠️ **必须可空**（2026-10-26 修）：模型侧是 `Mapped[Optional[str]]`，
    #: 手工产生的通知（`Notification(user_id, title, level)`）不写这个字段 ⇒ 库里是 NULL。
    #: 原写法 `str = ""` 只给**缺省值**、不接 None ⇒ 只要有 NULL 行，
    #: `GET /api/notifications` 直接 `ResponseValidationError` 返回 500（实测）。
    source_type: str | None = None
    source_id: int | None = None
    read: bool = False
    created_at: str = ""


class NotificationListResponse(BaseModel):
    """`GET /api/notifications` —— 真分页（DB 层 LIMIT/OFFSET）。"""

    total: int = 0
    page: int = 1
    page_size: int = 20
    items: list[NotificationItem] = Field(default_factory=list)


class UnreadCountResponse(BaseModel):
    """`GET /api/notifications/unread-count` —— 未读数（角标数据源）。"""

    unread: int = 0


class MarkAllReadResponse(BaseModel):
    """`POST /api/notifications/read-all`。"""

    marked: int = Field(default=0, description="本次标记为已读的条数")
