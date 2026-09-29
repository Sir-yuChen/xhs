from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter
from backend.app.api.tasks import serialize_task
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user, get_owned_active_account
from backend.app.models import PlatformAccount, Task, User

router = APIRouter(prefix="/xhs/creator", tags=["xhs-creator"])


class CreatorKeywordRequest(BaseModel):
    account_id: int
    keyword: str = Field(min_length=1, max_length=120)


class CreatorUploadRequest(BaseModel):
    account_id: int
    file_path: str = Field(min_length=1)
    media_type: Literal["image", "video"] = "image"


class CreatorImagePublishRequest(BaseModel):
    account_id: int
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(default="")
    image_file_infos: list[dict[str, Any]] = Field(min_length=1)
    publish_mode: Literal["immediate", "scheduled"] = "immediate"
    scheduled_at: Optional[datetime] = None
    topics: Optional[list[str]] = None
    location: Optional[str] = None
    privacy_type: Optional[int] = Field(default=None, ge=0, le=1)
    is_private: Optional[bool] = None


class CreatorVideoPublishRequest(BaseModel):
    account_id: int
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(default="")
    video_info: dict[str, Any] = Field(default_factory=dict)
    publish_mode: Literal["immediate", "scheduled"] = "immediate"
    scheduled_at: Optional[datetime] = None
    topics: Optional[list[str]] = None
    location: Optional[str] = None
    privacy_type: Optional[int] = Field(default=None, ge=0, le=1)
    is_private: Optional[bool] = None


def get_creator_api_adapter_factory():
    return XhsCreatorApiAdapter


def _cookies_to_string(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return stripped
    if stripped.startswith("{"):
        cookies = json.loads(stripped)
        return "; ".join(f"{key}={cookie_value}" for key, cookie_value in cookies.items())
    return stripped


def _get_owned_creator_account(db: Session, current_user: User, account_id: int) -> PlatformAccount:
    """（薄别名）**唯一实现见 `core/deps.py::get_owned_active_account`**（DEF-055）。

    本函数曾是同一语义的拷贝之一，**漏了软删闸** —— 已删除的创作者账号仍能过闸。
    """
    return get_owned_active_account(
        db,
        current_user,
        account_id,
        platform="xhs",
        sub_type="creator",
        detail="Creator account not found",
    )


def _get_latest_creator_cookies(db: Session, account: PlatformAccount) -> str:
    """创作者平台接口的令牌：**只取 creator 平台**（2026-09-21 拆分，§11.5）。

    发布 / 笔记管理都打在 `creator.xiaohongshu.com`，用 www 或千帆的 jar 都打不开。
    取不到返回空串，由调用方给结构化原因（不在这里抛 400 —— 调用点各有各的文案）。
    """
    from backend.app.services.account_service import CHANNEL_CREATOR, latest_cookies_for_channel

    return latest_cookies_for_channel(db, account.id, CHANNEL_CREATOR)


def _adapter_for_account(
    db: Session,
    current_user: User,
    account_id: int,
    adapter_factory,
) -> tuple[PlatformAccount, Any]:
    account = _get_owned_creator_account(db, current_user, account_id)
    cookies = _get_latest_creator_cookies(db, account)
    if not cookies:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该账号没有创作者平台（creator）平台的令牌，请在创作者平台登录后导入或续期。",
        )
    return account, adapter_factory(cookies)


def _payload_items(raw_payload: Any) -> list[Any]:
    if isinstance(raw_payload, list):
        return raw_payload
    if not isinstance(raw_payload, dict):
        return []
    data = raw_payload.get("data") if isinstance(raw_payload.get("data"), dict) else raw_payload
    for key in ("items", "list", "notes", "topics", "pois"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _create_operation_task(
    db: Session,
    current_user: User,
    task_type: str,
    payload: dict[str, Any],
) -> Task:
    task = Task(
        user_id=current_user.id,
        platform="xhs",
        task_type=task_type,
        status="running",
        progress=20,
        payload=payload,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _complete_operation_task(db: Session, task: Task, payload: dict[str, Any]) -> Task:
    task.status = "completed"
    task.progress = 100
    task.payload = {**(task.payload or {}), **payload}
    db.commit()
    db.refresh(task)
    return task


def _fail_operation_task(db: Session, task: Task, error: str) -> None:
    task.status = "failed"
    task.progress = 100
    task.payload = {**(task.payload or {}), "error": error}
    db.commit()


def _scheduled_post_time(publish_mode: str, scheduled_at: Optional[datetime]) -> Optional[int]:
    if publish_mode != "scheduled":
        return None
    if scheduled_at is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Scheduled publish time is required")
    return int(scheduled_at.timestamp() * 1000)


def _clean_topics(topics: Optional[list[str]]) -> list[str]:
    if topics is None:
        return []
    return [topic.strip() for topic in topics if topic and topic.strip()]


def _apply_publish_options(note_info: dict[str, Any], payload: CreatorImagePublishRequest | CreatorVideoPublishRequest) -> None:
    topics = _clean_topics(payload.topics)
    if topics:
        note_info["topics"] = topics
    if payload.location and payload.location.strip():
        note_info["location"] = payload.location.strip()
    if payload.is_private is not None:
        note_info["type"] = 1 if payload.is_private else 0
    elif payload.privacy_type is not None:
        note_info["type"] = payload.privacy_type


@router.post("/topics/search")
def search_topics(
    payload: CreatorKeywordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_creator_api_adapter_factory),
):
    _, adapter = _adapter_for_account(db, current_user, payload.account_id, adapter_factory)
    success, message, raw_payload = adapter.get_topic(payload.keyword)
    if not success:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=message or "Creator topic search failed")
    return {"items": _payload_items(raw_payload), "raw": raw_payload}


@router.post("/locations/search")
def search_locations(
    payload: CreatorKeywordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_creator_api_adapter_factory),
):
    _, adapter = _adapter_for_account(db, current_user, payload.account_id, adapter_factory)
    success, message, raw_payload = adapter.get_location_info(payload.keyword)
    if not success:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=message or "Creator location search failed")
    return {"items": _payload_items(raw_payload), "raw": raw_payload}


@router.post("/assets/upload")
def upload_asset(
    payload: CreatorUploadRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_creator_api_adapter_factory),
):
    account, adapter = _adapter_for_account(db, current_user, payload.account_id, adapter_factory)
    task = _create_operation_task(
        db,
        current_user,
        "creator_direct_upload",
        {"account_id": account.id, "file_path": payload.file_path, "media_type": payload.media_type},
    )
    try:
        upload_payload = adapter.upload_media(payload.file_path, payload.media_type)
        task = _complete_operation_task(db, task, {"payload": upload_payload})
        return {"task": serialize_task(task), "payload": upload_payload}
    except Exception as exc:
        _fail_operation_task(db, task, str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/publish/image")
def publish_image(
    payload: CreatorImagePublishRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_creator_api_adapter_factory),
):
    account, adapter = _adapter_for_account(db, current_user, payload.account_id, adapter_factory)
    note_info = {
        "title": payload.title,
        "desc": payload.body,
        "media_type": "image",
        "image_file_infos": payload.image_file_infos,
        "type": 1,
        "postTime": _scheduled_post_time(payload.publish_mode, payload.scheduled_at),
    }
    _apply_publish_options(note_info, payload)
    task = _create_operation_task(
        db,
        current_user,
        "creator_direct_publish",
        {"account_id": account.id, "media_type": "image", "publish_mode": payload.publish_mode},
    )
    try:
        publish_payload = adapter.post_note(note_info)
        task = _complete_operation_task(db, task, {"payload": publish_payload})
        return {"task": serialize_task(task), "payload": publish_payload}
    except Exception as exc:
        _fail_operation_task(db, task, str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/publish/video")
def publish_video(
    payload: CreatorVideoPublishRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_creator_api_adapter_factory),
):
    account, adapter = _adapter_for_account(db, current_user, payload.account_id, adapter_factory)
    note_info = {
        "title": payload.title,
        "desc": payload.body,
        "media_type": "video",
        "video_info": payload.video_info,
        "type": 1,
        "postTime": _scheduled_post_time(payload.publish_mode, payload.scheduled_at),
    }
    _apply_publish_options(note_info, payload)
    task = _create_operation_task(
        db,
        current_user,
        "creator_direct_publish",
        {"account_id": account.id, "media_type": "video", "publish_mode": payload.publish_mode},
    )
    try:
        publish_payload = adapter.post_note(note_info)
        task = _complete_operation_task(db, task, {"payload": publish_payload})
        return {"task": serialize_task(task), "payload": publish_payload}
    except Exception as exc:
        _fail_operation_task(db, task, str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
