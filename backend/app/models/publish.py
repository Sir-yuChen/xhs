from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

#: 素材行类型（**父子唯一判据**）—— 一个 `row_type` 决定 `parent_id` 指向哪张父表。
#: `note` → `notes.id` / `draft` → `xhs_ai_drafts.id` /
#: `publish` → `xhs_publish_jobs.id` / `aigc` → `xhs_generation_records.id`。
ASSET_NOTE = "note"
ASSET_DRAFT = "draft"
ASSET_PUBLISH = "publish"
ASSET_AIGC = "aigc"


class PublishJob(Base):
    __tablename__ = "xhs_publish_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_users.id"), index=True)
    platform_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("xhs_accounts.id"), nullable=True, index=True)
    source_draft_id: Mapped[Optional[int]] = mapped_column(ForeignKey("xhs_ai_drafts.id"), nullable=True)
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
    #: 发布幂等键（认领时生成，防重复发布的唯一可靠防线）。保持 nullable：
    #: 未进入发布的任务为 NULL，唯一索引允许多个 NULL ⇒ 不误伤「建了又取消」。
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(160), nullable=True, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)


class PublishAsset(Base):
    """发布素材（**草稿与发布共用一张表**，`row_type` 分两型，小红书私有 L1）。

    2026-10-28 用户口径「分析功能类似重复表合并……全部为小红书私有表」：
    原 `draft_assets`（草稿素材，有序）与 `publish_assets`（发布素材 + 上传态）
    列几乎一一对应，差别只是发布侧多 4 个上传态字段。`send-to-publish` 原本要
    把素材**整批复制**一遍，同一份素材在两张表各存一行 ⇒ 合并为本表。

    * `row_type = draft` ⇒ `parent_id` 指 `ai_drafts.id`，上传态列为空。
    * `row_type = publish` ⇒ `parent_id` 指 `xhs_publish_jobs.id`，上传态列有值。

    ⚠️ 判据用 `row_type` 而不是「parent_id 落在哪张表」—— 同表两种父，
    靠列值分型才不会张冠李戴。
    """

    __tablename__ = "xhs_publish_assets"
    __table_args__ = (
        Index("ix_xhs_publish_assets_parent", "row_type", "parent_id"),
        Index("ix_xhs_publish_assets_sort", "parent_id", "sort_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: `draft` / `publish`。
    row_type: Mapped[str] = mapped_column(String(8), default=ASSET_DRAFT, index=True)
    #: 父行：由 `row_type` 决定指向哪张父表（笔记 / 草稿 / 发布任务 / 生成记录）。
    parent_id: Mapped[int] = mapped_column(Integer, index=True)
    #: 租户归属（`aigc` 型必需：生成素材按用户隔离；其余型可空）。
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)

    #: `image` / `video`。
    asset_type: Mapped[str] = mapped_column(String(32), default="image")
    #: 远端地址（草稿期可能直接引用平台 URL）。
    url: Mapped[str] = mapped_column(Text, default="")
    #: 本地文件路径（原 `publish_assets.file_path` 并入此列）。
    local_path: Mapped[str] = mapped_column(Text, default="")
    #: 草稿内顺序（发布侧保持复制时的顺序）。
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    # ---- 上传态（仅 `publish` 型有值，其余型恒为默认值）----
    upload_status: Mapped[str] = mapped_column(String(32), default="pending")
    creator_media_id: Mapped[str] = mapped_column(String(128), default="")
    upload_error: Mapped[str] = mapped_column(Text, default="")
    creator_upload_info: Mapped[str] = mapped_column(Text, default="{}")

    # ---- 生成态（仅 `aigc` 型有值）----
    #: 生成用的 prompt 原文。
    prompt: Mapped[str] = mapped_column(Text, default="")
    model_name: Mapped[str] = mapped_column(String(128), default="")
    #: 生成参数快照（JSON 字符串，原 `aigc_media_assets.params`）。
    params: Mapped[str] = mapped_column(Text, default="{}")
    #: 关联商品 id（原 `aigc_media_assets.item_id`，生成图的业务归属）。
    item_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    platform_account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    aigc_task_id: Mapped[str] = mapped_column(String(128), default="", server_default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)

    def note_to_dict(self) -> dict[str, Any]:
        """笔记素材原始字段（`url` 的展示拼装留在 API 层 `_serialize_asset`）。"""
        return {
            "id": self.id,
            "note_id": self.parent_id,
            "asset_type": self.asset_type,
            "url": self.url or "",
            "local_path": self.local_path or "",
            "sort_order": self.sort_order,
        }

    def aigc_to_dict(self) -> dict[str, Any]:
        """AI 生成素材原始字段（`params` 的 JSON 解析留在 API 层）。"""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "generation_record_id": self.parent_id,
            "asset_type": self.asset_type,
            "local_path": self.local_path or "",
            "url": self.url or "",
            "aigc_task_id": self.aigc_task_id or "",
            "prompt": self.prompt or "",
            "model_name": self.model_name or "",
            "item_id": self.item_id or "",
            "platform_account_id": self.platform_account_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def draft_to_dict(self) -> dict[str, Any]:
        """草稿素材原始字段（`url` 的展示拼装留在 API 层 `_serialize_draft_asset`）。"""
        return {
            "id": self.id,
            "draft_id": self.parent_id,
            "asset_type": self.asset_type,
            "url": self.url or "",
            "local_path": self.local_path or "",
            "sort_order": self.sort_order,
        }

    def publish_to_dict(self) -> dict[str, Any]:
        """发布素材原始字段（`creator_upload_info` 的 JSON 解析留在 API 层）。"""
        return {
            "id": self.id,
            "publish_job_id": self.parent_id,
            "asset_type": self.asset_type,
            "file_path": self.local_path or "",
            "upload_status": self.upload_status,
            "creator_media_id": self.creator_media_id or "",
            "upload_error": self.upload_error or "",
            "creator_upload_info": self.creator_upload_info or "{}",
        }
