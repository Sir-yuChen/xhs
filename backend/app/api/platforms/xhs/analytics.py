from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.perm_deps import require_perm
from backend.app.core.deps import get_current_user
from backend.app.core.time import shanghai_now
from backend.app.services.note_metrics_service import note_metrics
from backend.app.models import Note, PlatformAccount, PublishJob, User

router = APIRouter(prefix="/xhs/analytics", tags=["xhs-analytics"])


def _owned_notes(db: Session, current_user: User) -> list[Note]:
    return db.scalars(
        select(Note)
        .where(Note.user_id == current_user.id, Note.platform == "xhs", Note.is_deleted.is_(False))
        .order_by(Note.created_at.desc())
    ).all()


@router.get("/overview")
def overview(
    _perm: None = Depends(require_perm("xhs:dashboard:view")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """总览四个指标（**首页唯一消费方**）。洞察两页与监控中心的端点已全部退场。"""
    notes = _owned_notes(db, current_user)
    accounts = db.scalars(
        select(PlatformAccount).where(
            PlatformAccount.user_id == current_user.id,
            PlatformAccount.platform == "xhs",
            PlatformAccount.deleted_at.is_(None),
        )
    ).all()
    pending_publishes = db.scalars(
        select(PublishJob)
        .join(PlatformAccount, PublishJob.platform_account_id == PlatformAccount.id)
        .where(
            PlatformAccount.user_id == current_user.id,
            PublishJob.platform == "xhs",
            PublishJob.status.in_(["pending", "uploading", "publishing", "scheduled"]),
        )
    ).all()
    today = shanghai_now().date()
    return {
        "platform": "xhs",
        "today_crawls": len([note for note in notes if note.created_at.date() == today]),
        "saved_notes": len(notes),
        "pending_publishes": len(pending_publishes),
        "healthy_accounts": len([account for account in accounts if account.status in ("active", "healthy")]),
        "at_risk_accounts": len([account for account in accounts if account.status not in ("active", "healthy")]),
        "total_engagement": sum(note_metrics(note)["engagement"] for note in notes),
    }
