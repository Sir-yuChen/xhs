from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from backend.app.models.notification import Notification
from backend.app.models.task import Task


def notify_task_failed(db: Session, task: Task) -> Notification:
    return _create(
        db,
        user_id=task.user_id,
        title=f"任务失败: {task.task_type}",
        body=(task.payload or {}).get("error", ""),
        level="warning",
        source_task_id=task.id,
        source_type="task",
        source_id=task.id,
    )


def notify_task_exhausted(db: Session, task: Task) -> Notification:
    return _create(
        db,
        user_id=task.user_id,
        title=f"任务重试耗尽: {task.task_type}",
        body=f"已重试 {task.retry_count} 次",
        level="error",
        source_task_id=task.id,
        source_type="task",
        source_id=task.id,
    )


def notify_target_paused(db: Session, user_id: int, target_name: str, target_id: int) -> Notification:
    return _create(
        db,
        user_id=user_id,
        title=f"监控已暂停: {target_name}",
        body="连续 3 次刷新失败，已自动暂停",
        level="error",
        source_type="target",
        source_id=target_id,
    )


def _create(
    db: Session,
    *,
    user_id: int,
    title: str,
    body: str = "",
    level: str = "info",
    source_task_id: Optional[int] = None,
    source_type: Optional[str] = None,
    source_id: Optional[int] = None,
) -> Notification:
    n = Notification(
        user_id=user_id,
        title=title,
        body=body,
        level=level,
        source_task_id=source_task_id,
        source_type=source_type,
        source_id=source_id,
    )
    db.add(n)
    db.flush()
    return n
