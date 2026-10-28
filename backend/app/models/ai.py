from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


DEFAULT_TEXT_MODEL_NAME = "gpt-5.4"


class AiDraft(Base):
    __tablename__ = "xhs_ai_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_users.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    source_note_id: Mapped[Optional[int]] = mapped_column(ForeignKey("xhs_notes.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)


class AiGeneratedAsset(Base):
    __tablename__ = "xhs_ai_generated_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_users.id"), index=True)
    draft_id: Mapped[Optional[int]] = mapped_column(ForeignKey("xhs_ai_drafts.id"), nullable=True)
    prompt: Mapped[str] = mapped_column(Text, default="")
    model_name: Mapped[str] = mapped_column(String(128), default="")
    params: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    file_path: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)



