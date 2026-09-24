from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


class PublishJob(Base):
    __tablename__ = "publish_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    platform_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("platform_accounts.id"), nullable=True, index=True)
    source_draft_id: Mapped[Optional[int]] = mapped_column(ForeignKey("ai_drafts.id"), nullable=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    publish_mode: Mapped[str] = mapped_column(String(32), default="immediate")
    publish_options: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    external_note_id: Mapped[str] = mapped_column(String(128), default="")
    publish_error: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 发布幂等键（T-103 / DEF-002）。生成时机 = **真正发布前**（认领时），
    # 不是建任务时；键 = "{platform}:{账号}:{标题|正文|发布参数 的 sha256 前 32 位}"。
    # 见 services/publish_service.py 的完整论证与已知副作用。
    #
    # 唯一索引是「防重复发布」四道防线里唯一可靠的一道（其余三道都在应用层）。
    # 保持 nullable：未进入发布的任务为 NULL，而 SQLite / MySQL 的唯一索引
    # 允许多个 NULL，因此不会误伤「建了又取消」的合法流程。
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(160), nullable=True, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)


class PublishAsset(Base):
    __tablename__ = "publish_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publish_job_id: Mapped[int] = mapped_column(ForeignKey("publish_jobs.id"), index=True)
    asset_type: Mapped[str] = mapped_column(String(32))
    file_path: Mapped[str] = mapped_column(Text)
    upload_status: Mapped[str] = mapped_column(String(32), default="pending")
    creator_media_id: Mapped[str] = mapped_column(String(128), default="")
    upload_error: Mapped[str] = mapped_column(Text, default="")
    creator_upload_info: Mapped[str] = mapped_column(Text, default="{}")
