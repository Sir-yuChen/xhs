"""话题字典 CRUD（设计 13 §34.4，2026-09-25；**原 `/tags` 标签路由**）。

## 为什么改名（用户 ② 口径「将当前系统的笔记标签表合并成话题表」）

`tags` / `note_tags` 两表已整表并入 `topics` / `note_topics`（迁移
`b2c3d4e5f6g7`）⇒ 路由随之从 `/tags` 改 `/topics`：底层表已改名，路由名
留着「tags」只会让后来人以为还有第二张表。**4 个 operation 数不变**
（list / create / update / delete）。

## 与笔记侧话题的分工

* 本文件管**话题字典行**（`topics`：名字 / 配色 / 平台话题 ID）；
* 笔记↔话题**关系**在 `note_topics`，写入走 `PUT /notes/{id}/topics`
  （手动整列替换）或同步链路（只增不删）—— 本文件不直接动关系行，
  **删词典行时**才连带清关系（§34.7：关系指向不存在的典例行 = 孤儿行）。

⚠️ 前端内容库详情弹窗的「标签展示 / 添加」UI 已合并进话题功能（§34.4）——
「+ 新增」走 `PUT /notes/{id}/topics`（后端 find-or-create），**不再**需要
先 `POST /topics` 再关联；本路由保留给字典管理消费者。

## `GET /topics` 的 `keyword`（§34.4 模糊搜索）

内容库筛选栏话题下拉走**远程模糊搜索**（`showSearch` + 300ms 防抖）——
话题字典随同步链路持续增长，全量一次拉完再前端过滤只是把成本挪到进页面时。
`keyword` 非空 ⇒ `name LIKE '%关键词%'`；空 / 全空白 = 不过滤（全量）。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models import NoteTopic, Topic, User
from backend.app.schemas.common import paginated

router = APIRouter(prefix="/topics", tags=["topics"])


class TopicCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    color: str = Field(default="#1677ff", max_length=24)


class TopicUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    color: Optional[str] = Field(default=None, max_length=24)


def serialize_topic(topic: Topic) -> dict:
    return {
        "id": topic.id,
        "name": topic.name,
        "color": topic.color,
    }


def _normalize_name(name: str) -> str:
    return name.strip()


def _get_owned_topic(db: Session, current_user: User, topic_id: int) -> Topic:
    topic = db.get(Topic, topic_id)
    if topic is None or topic.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Topic not found")
    return topic


def _ensure_unique_name(db: Session, current_user: User, name: str, exclude_topic_id: Optional[int] = None) -> None:
    statement = select(Topic).where(Topic.user_id == current_user.id, Topic.name == name)
    if exclude_topic_id is not None:
        statement = statement.where(Topic.id != exclude_topic_id)
    if db.scalars(statement).first() is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Topic name already exists")


@router.get("")
def list_topics(
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=100),
    keyword: Optional[str] = Query(None, description="话题名模糊搜索（内容库筛选栏下拉用，前端 300ms 防抖后传入）"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = select(Topic).where(Topic.user_id == current_user.id).order_by(Topic.id.asc())
    if keyword and keyword.strip():
        # 模糊搜索（§34.4）：SQL 层 LIKE 过滤再分页 —— 话题字典随同步链路增长，
        # 全量拉回前端过滤只是把成本挪到进页面时。空 / 全空白 = 不过滤（全量）。
        statement = statement.where(Topic.name.like(f"%{keyword.strip()}%"))
    topics = db.scalars(statement).all()
    return paginated([serialize_topic(topic) for topic in topics], page, page_size)


@router.post("")
def create_topic(
    payload: TopicCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    name = _normalize_name(payload.name)
    if not name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Topic name is required")
    _ensure_unique_name(db, current_user, name)
    topic = Topic(user_id=current_user.id, name=name, color=payload.color or "#1677ff")
    db.add(topic)
    db.commit()
    db.refresh(topic)
    return serialize_topic(topic)


@router.patch("/{topic_id}")
def update_topic(
    topic_id: int,
    payload: TopicUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    topic = _get_owned_topic(db, current_user, topic_id)
    if payload.name is not None:
        name = _normalize_name(payload.name)
        if not name:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Topic name is required")
        _ensure_unique_name(db, current_user, name, exclude_topic_id=topic.id)
        topic.name = name
    if payload.color is not None:
        topic.color = payload.color or "#1677ff"
    db.commit()
    db.refresh(topic)
    return serialize_topic(topic)


@router.delete("/{topic_id}")
def delete_topic(
    topic_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删话题词典行 —— **关系行一并删**（§34.7：指向已删典讳行的关系 = 孤儿行）。

    与删笔记（`purge_note`）同一条规则：本地实体真删 ⇒ 关系行跟着走。
    """
    topic = _get_owned_topic(db, current_user, topic_id)
    db.execute(delete(NoteTopic).where(NoteTopic.topic_id == topic.id))
    db.delete(topic)
    db.commit()
    return {"id": topic_id, "status": "deleted"}
