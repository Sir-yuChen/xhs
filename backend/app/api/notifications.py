from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models import Notification, User
from backend.app.schemas.common import paginated_with_total
from backend.app.schemas.notifications import (
    MarkAllReadResponse,
    NotificationItem,
    NotificationListResponse,
    UnreadCountResponse,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


def serialize_notification(n: Notification) -> dict:
    return {
        "id": n.id,
        "title": n.title,
        "body": n.body,
        "level": n.level,
        "source_task_id": n.source_task_id,
        "source_type": n.source_type,
        "source_id": n.source_id,
        "read": n.read,
        "created_at": n.created_at.isoformat(),
    }


@router.get("", response_model=NotificationListResponse)
def list_notifications(
    unread: Optional[bool] = None,
    source_type: Optional[str] = None,
    level: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """通知列表（**真分页**：DB 层 limit/offset，不是全表查出后切片）。

    ⚠️ 2026-10-13（用户口径「分析站内消息模块是否完善成熟，优化不足的地方」）：
    旧实现 `paginated(db.scalars(stmt...).all(), page, page_size)` **先把全表读进内存
    再切片** —— 通知只增不减（无清理），数据量上来后每次列表都全表加载。
    改为 SQL `LIMIT/OFFSET` + 独立 `total` 计数（`paginated` 只负责算页数元信息）。
    """
    where = [Notification.user_id == current_user.id]
    if unread is True:
        where.append(Notification.read == False)  # noqa: E712
    if source_type:
        where.append(Notification.source_type == source_type)
    if level:
        where.append(Notification.level == level)

    total = int(db.scalar(select(func.count(Notification.id)).where(*where)) or 0)
    items = db.scalars(
        select(Notification)
        .where(*where)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return paginated_with_total(
        [serialize_notification(n) for n in items], page, page_size, total
    )


@router.get("/unread-count", response_model=UnreadCountResponse)
def unread_count(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """未读通知条数（**独立端点**）。

    ⚠️ 2026-10-13：旧前端用「当页 20 条里未读的条数」当未读数 ⇒ 未读超过 20 条时
    **恒显示 20**（少算）。未读数必须由后端按全量计数下发，前端不再自行推断。
    """
    count = int(
        db.scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == current_user.id,
                Notification.read == False,  # noqa: E712
            )
        )
        or 0
    )
    return {"unread": count}


@router.post("/{notification_id}/read", response_model=NotificationItem)
def mark_read(
    notification_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    n = db.get(Notification, notification_id)
    if n is None or n.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    n.read = True
    db.commit()
    db.refresh(n)
    return serialize_notification(n)


@router.post("/read-all", response_model=MarkAllReadResponse)
def mark_all_read(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    unread = db.scalars(
        select(Notification).where(Notification.user_id == current_user.id, Notification.read == False)
    ).all()
    for n in unread:
        n.read = True
    db.commit()
    return {"marked": len(unread)}
