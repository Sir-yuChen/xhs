from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.time import shanghai_now
from backend.app.services.note_metrics_service import (
    collect_snapshots,
    note_metrics,
    stat_date_of,
    trend_series,
)
#: 商品指标侧（M2 切片 2）—— **别名导入**：两侧的 `collect_snapshots` /
#: `trend_series` / `stat_date_of` 同名（刻意同形，见 `shop_metrics_service` 的
#: 模块 docstring），直接 import 会覆盖笔记侧的名字。
from backend.app.services.shop_metrics_service import (
    collect_snapshots as collect_shop_snapshots,
    trend_series as shop_trend_series,
)
from backend.app.services.sqlite_retry import LockRetryExhausted
#: 热点驱动生成（M3 方向 2，设计 17 §13.11）—— 别名导入：stat_date_of 与指标侧同名。
from backend.app.services.hot_topic_service import (
    collect_topics_for_item,
    list_matches as list_topic_matches,
    list_topics as list_hot_topics,
    match_topics_to_goods,
    stat_date_of as hot_topic_stat_date_of,
)
from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory
from backend.app.services.shop_binding_service import qianfan_cookies_for_account
#: 话题的平台正文形态（#名字[话题]#）—— 唯一实现，本文件不另写（DEF-044）。
from backend.app.services.note_topic_service import topic_token
from backend.app.services.note_gen_llm_service import NoteGenerationError
#: 入队入口在 worker 模块（不在 service）—— 别照名字猜模块（实踩一次）。
from backend.app.services.note_gen_llm_worker import start_llm_generate_note
from backend.app.api.platforms.xhs.product_notes import (
    _ledger_product,
    _llm_generation_error,
    _owned_account,
)
from backend.app.models import AiDraft, HotTopic, KeywordGroup, MonitoringTarget, Note, NoteComment, NoteTopic, PlatformAccount, PublishJob, ShopProduct, Topic, User

router = APIRouter(prefix="/xhs/analytics", tags=["xhs-analytics"])


class PlatformHotTopicRefresh(BaseModel):
    """采集平台热点话题的入参（设计 17 §13.11）。

    必须给 item_ids：平台接口按商品调用，
    没有「不选商品拉全站热搜榜」的能力。
    """

    item_ids: list[str] = Field(default_factory=list)


class PlatformHotTopicGenerate(BaseModel):
    """按热点词驱动生成（设计 16 §3.2）。"""

    item_id: str
    topic: str
    platform_account_id: int
    template_id: int | None = None
    media_source: str | None = "goods"


class AnalyticsReportRequest(BaseModel):
    note_ids: list[int] = Field(default_factory=list)
    format: Literal["json"] = "json"




def _as_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip().lower().replace(",", "")
        multiplier = 1
        if cleaned.endswith("w"):
            multiplier = 10000
            cleaned = cleaned[:-1]
        try:
            return int(float(cleaned) * multiplier)
        except ValueError:
            return 0
    return 0


def _note_metrics(note: Note) -> dict[str, int]:
    """单篇笔记指标（**委托公共口径**，v2.24）。

    ⚠️ 口径定义已下沉到 `services/note_metrics_service.py` —— 采集层（写快照）与
    展示层（本模块）**必须同一份**，否则「页面上看到的互动数」与「趋势里的数」
    会对不上，且没人能归因（DEF-044）。
    """
    return note_metrics(note)


def _owned_notes_statement(current_user: User):
    return (
        select(Note)
        .where(Note.user_id == current_user.id, Note.platform == "xhs")
    )


def _owned_notes(db: Session, current_user: User) -> list[Note]:
    return db.scalars(_owned_notes_statement(current_user).order_by(Note.created_at.desc())).all()


def _note_haystack(note: Note) -> str:
    return "\n".join(
        [
            note.note_id or "",
            note.title or "",
            note.content or "",
            note.author_name or "",
            str(note.raw_json or {}),
        ]
    ).lower()


def _note_matches_value(note: Note, value: str) -> bool:
    needle = value.strip().lower()
    if not needle:
        return False
    return needle in _note_haystack(note)


def _serialize_top_note(note: Note) -> dict[str, Any]:
    metrics = _note_metrics(note)
    return {
        "id": note.id,
        "note_id": note.note_id,
        "title": note.title,
        "author_name": note.author_name,
        "created_at": note.created_at.isoformat(),
        **metrics,
    }


def _serialize_draft(draft: AiDraft) -> dict[str, Any]:
    return {
        "id": draft.id,
        "platform": draft.platform,
        "title": draft.title,
        "body": draft.body,
        "source_note_id": draft.source_note_id,
        "created_at": draft.created_at.isoformat(),
    }


def _get_owned_benchmark_target(db: Session, current_user: User, target_id: int) -> MonitoringTarget:
    target = db.get(MonitoringTarget, target_id)
    if target is None or target.user_id != current_user.id or target.platform != "xhs":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Benchmark target not found")
    if target.target_type not in {"account", "brand"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Benchmark target must be account or brand")
    return target


def _benchmark_matches(notes: list[Note], target: MonitoringTarget) -> list[Note]:
    matched = [note for note in notes if _note_matches_value(note, target.value)]
    return sorted(matched, key=lambda note: _note_metrics(note)["engagement"], reverse=True)


def _raw_topics(raw: dict[str, Any]) -> list[str]:
    topics: list[str] = []
    for key in ("tags", "topics", "keywords"):
        value = raw.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    topics.append(item.strip().lstrip("#"))
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("tag") or item.get("keyword")
                    if isinstance(name, str) and name.strip():
                        topics.append(name.strip().lstrip("#"))
    return topics


def _topic_items(db: Session, current_user: User, notes: list[Note]) -> list[dict[str, Any]]:
    note_by_id = {note.id: note for note in notes}
    topic_note_ids: dict[str, set[int]] = defaultdict(set)

    # 话题来源一：`note_topics` 关系表 join `topics` 字典表（§34.4：2026-09-25 起
    # 标签表已并入话题表 —— 原 `note_tags` join `tags` 的同款查询）。
    topic_rows = db.execute(
        select(NoteTopic.note_id, Topic.name)
        .join(Topic, Topic.id == NoteTopic.topic_id)
        .where(Topic.user_id == current_user.id, NoteTopic.note_id.in_(note_by_id.keys() or [-1]))
    ).all()
    for note_id, topic_name in topic_rows:
        if topic_name:
            topic_note_ids[topic_name].add(note_id)

    for note in notes:
        for topic in _raw_topics(note.raw_json or {}):
            topic_note_ids[topic].add(note.id)

    items: list[dict[str, Any]] = []
    for keyword, note_ids in topic_note_ids.items():
        engagement = sum(_note_metrics(note_by_id[note_id])["engagement"] for note_id in note_ids if note_id in note_by_id)
        items.append({"keyword": keyword, "notes": len(note_ids), "engagement": engagement})
    return sorted(items, key=lambda item: (item["notes"], item["engagement"], item["keyword"]), reverse=True)


def _owned_comments(db: Session, current_user: User) -> list[NoteComment]:
    return db.scalars(
        select(NoteComment)
        .join(Note, NoteComment.note_id == Note.id)
        .where(Note.user_id == current_user.id, Note.platform == "xhs")
        .order_by(NoteComment.like_count.desc(), NoteComment.id.asc())
    ).all()


def _comments_for_notes(db: Session, current_user: User, note_ids: set[int]) -> list[NoteComment]:
    if not note_ids:
        return []
    return db.scalars(
        select(NoteComment)
        .join(Note, NoteComment.note_id == Note.id)
        .where(Note.user_id == current_user.id, Note.platform == "xhs", NoteComment.note_id.in_(note_ids))
        .order_by(NoteComment.like_count.desc(), NoteComment.id.asc())
    ).all()


def _comment_insight_payload(comments: list[NoteComment]) -> dict[str, Any]:
    repeated_terms = Counter()
    for comment in comments:
        for term in ("price", "link", "suitable", "how", "where", "recommend", "commute"):
            if term in comment.content.lower():
                repeated_terms[term] += 1
    return {
        "total_comments": len(comments),
        "question_count": len([comment for comment in comments if "?" in comment.content or "？" in comment.content]),
        "top_terms": [{"term": term, "count": count} for term, count in repeated_terms.most_common(10)],
        "top_comments": [
            {
                "id": comment.id,
                "note_id": comment.note_id,
                "user_name": comment.user_name,
                "content": comment.content,
                "like_count": comment.like_count,
            }
            for comment in comments[:10]
        ],
    }


def _owned_report_notes(db: Session, current_user: User, note_ids: list[int]) -> list[Note]:
    if not note_ids:
        return _owned_notes(db, current_user)

    unique_ids = list(dict.fromkeys(note_ids))
    notes = db.scalars(_owned_notes_statement(current_user).where(Note.id.in_(unique_ids))).all()
    notes_by_id = {note.id: note for note in notes}
    if set(notes_by_id) != set(unique_ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report note not found")
    return [notes_by_id[note_id] for note_id in unique_ids]


def _benchmark_report_items(db: Session, current_user: User, notes: list[Note]) -> list[dict[str, Any]]:
    targets = db.scalars(
        select(MonitoringTarget)
        .where(
            MonitoringTarget.user_id == current_user.id,
            MonitoringTarget.platform == "xhs",
            MonitoringTarget.target_type.in_(("account", "brand")),
        )
        .order_by(MonitoringTarget.created_at.desc(), MonitoringTarget.id.desc())
    ).all()

    items: list[dict[str, Any]] = []
    for target in targets:
        matched = _benchmark_matches(notes, target)
        total_engagement = sum(_note_metrics(note)["engagement"] for note in matched)
        items.append(
            {
                "target_id": target.id,
                "target_type": target.target_type,
                "name": target.name or target.value,
                "value": target.value,
                "matched_notes": len(matched),
                "total_engagement": total_engagement,
                "average_engagement": round(total_engagement / len(matched), 2) if matched else 0,
                "top_notes": [_serialize_top_note(note) for note in matched[:5]],
            }
        )
    return sorted(items, key=lambda item: (item["total_engagement"], item["matched_notes"], item["name"]), reverse=True)


def _keyword_trend_items(db: Session, current_user: User, notes: list[Note]) -> list[dict[str, Any]]:
    groups = db.scalars(
        select(KeywordGroup)
        .where(KeywordGroup.user_id == current_user.id, KeywordGroup.platform == "xhs")
        .order_by(KeywordGroup.created_at.asc(), KeywordGroup.id.asc())
    ).all()

    items: list[dict[str, Any]] = []
    if groups:
        for group in groups:
            for keyword in group.keywords or []:
                keyword_text = str(keyword).strip()
                if not keyword_text:
                    continue
                matched = [note for note in notes if _note_matches_value(note, keyword_text)]
                engagement = sum(_note_metrics(note)["engagement"] for note in matched)
                items.append(
                    {
                        "keyword": keyword_text,
                        "group_id": group.id,
                        "group_name": group.name,
                        "notes": len(matched),
                        "engagement": engagement,
                        "top_notes": [_serialize_top_note(note) for note in sorted(matched, key=lambda note: _note_metrics(note)["engagement"], reverse=True)[:5]],
                    }
                )
        return items

    return [
        {
            "keyword": item["keyword"],
            "group_id": None,
            "group_name": "",
            "notes": item["notes"],
            "engagement": item["engagement"],
            "top_notes": [],
        }
        for item in _topic_items(db, current_user, notes)
    ]


def _build_report_payload(db: Session, current_user: User, notes: list[Note], generated_at: datetime) -> dict[str, Any]:
    comments = _comments_for_notes(db, current_user, {note.id for note in notes})
    top_notes = sorted(notes, key=lambda note: _note_metrics(note)["engagement"], reverse=True)
    topics = _topic_items(db, current_user, notes)
    total_engagement = sum(_note_metrics(note)["engagement"] for note in notes)
    benchmark_items = _benchmark_report_items(db, current_user, notes)
    summary = {
        "note_count": len(notes),
        "total_engagement": total_engagement,
        "comment_count": len(comments),
        "top_topics": topics[:10],
        "top_notes": [_serialize_top_note(note) for note in top_notes[:10]],
        "benchmark_count": len(benchmark_items),
    }
    return {
        "metadata": {
            "report_type": "operations",
            "platform": "xhs",
            "user_id": current_user.id,
            "generated_at": generated_at.isoformat(),
        },
        "summary": summary,
        "top_notes": [_serialize_top_note(note) for note in top_notes[:20]],
        "hot_topics": topics,
        "comment_insights": _comment_insight_payload(comments),
        "benchmarks": benchmark_items,
    }


def _write_report_file(current_user: User, payload: dict[str, Any]) -> tuple[str, Path]:
    export_dir = Path(get_settings().storage_dir) / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"xhs-report-u{current_user.id}-{uuid4().hex}.json"
    file_path = export_dir / file_name
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return file_name, file_path


@router.post("/metrics/collect")
def collect_note_metrics(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**采集当日笔记指标快照**（设计 17 §13.5.2，M2 方向 3）。

    把该用户全部已同步笔记的平台侧指标（点赞/收藏/评论/分享/**阅读**）按**当天**
    落一条快照。同日重复调用**覆盖**（唯一键 + upsert），不是追加。

    ⚠️ **纯本地操作、零平台请求**：数据源是 `notes.raw_json`（同步时就已落库）
    —— 所以这个端点可以随时调，不消耗平台额度、不触发风控。

    ⚠️ **不回填历史**：raw_json 只有最后一版，拿它当「历史某天」就是编造数据。
    趋势从本次采集起逐日积累。
    """
    # ⚠️ 撞写锁用尽 ⇒ **503（可重试）**，不是 500（服务端内部错误）。
    # 多用户同分钟并发采集时写锁竞争是**可预期**的；报 500 会让用户以为功能坏了，
    # 也会把「瞬时竞争」算进服务端错误率（口径污染）。前端/用户看到 503 应稍后重试。
    try:
        written = collect_snapshots(db, user_id=current_user.id)
    except LockRetryExhausted as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "SNAPSHOT_DB_BUSY",
                "message": "数据库繁忙（多个采集任务并发），请稍后重试",
            },
        ) from exc
    return {
        "stat_date": stat_date_of(shanghai_now()),
        "collected": written,
    }


@router.get("/metrics/trend")
def note_metrics_trend(
    days: int = Query(30, ge=0, le=365),
    note_pk: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**笔记指标趋势**（按日聚合；设计 17 §13.5.3，M2 方向 3）。

    - `days`：取最近 N 天（默认 30；`0` = 不限）。
    - `note_pk`：只看某一篇（不传 = 该用户全部笔记的合计）。

    ⚠️ **只返回有快照的天**，不补零 —— 缺失的天用 0 填充会把「那天没采」
    画成「那天互动掉到 0」（伪造数据，违反缺值口径）。
    """
    series = trend_series(db, user_id=current_user.id, days=days, note_pk=note_pk)
    return {
        "platform": "xhs",
        "days": days,
        "note_pk": note_pk,
        "series": series,
        "points": len(series),
    }


@router.post("/shop-metrics/collect")
def collect_shop_metrics(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**采集当日商品指标快照**（设计 17 §13.9.2，M2 方向 3 切片 2）。

    把该用户全部商品的交易侧指标（库存/销量/近 30 天销量/价格区间）按**当天**
    落一条快照。同日重复调用**覆盖**（唯一键 + upsert），不是追加。

    ⚠️ **纯本地操作、零平台请求**：数据源是 `shop_products`（同步时就已落库）
    —— 随时可调，不消耗平台额度、不触发风控。

    ⚠️ **不回填历史**：`shop_products` 只有「最后一次同步的值」，拿它当
    「历史某天」就是编造数据（硬约束⑤）。趋势从本次采集起逐日积累。
    """
    # ⚠️ 同笔记侧：撞锁用尽 ⇒ 503 可重试，不是 500。
    try:
        written = collect_shop_snapshots(db, user_id=current_user.id)
    except LockRetryExhausted as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "SNAPSHOT_DB_BUSY",
                "message": "数据库繁忙（多个采集任务并发），请稍后重试",
            },
        ) from exc
    return {
        "stat_date": stat_date_of(shanghai_now()),
        "collected": written,
    }


@router.get("/shop-metrics/trend")
def shop_metrics_trend(
    days: int = Query(30, ge=0, le=365),
    product_pk: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**商品指标趋势**（按日聚合；设计 17 §13.9.2，M2 方向 3 切片 2）。

    - `days`：取最近 N 天（默认 30；`0` = 不限）。
    - `product_pk`：只看某个商品（不传 = 该用户全部商品的合计）。

    ⚠️ **价格取区间最值**（`min(price_min_fen)` ~ `max(price_max_fen)`），
    不求和 —— 价格求和没有业务含义（同 `shop_metrics_service.trend_series`）。

    ⚠️ **只返回有快照的天**，不补零（与笔记侧同口径）—— 缺失的天用 0 填充会把
    「那天没采」画成「那天销量掉到 0」（伪造数据，违反缺值口径）。
    """
    series = shop_trend_series(
        db, user_id=current_user.id, days=days, product_pk=product_pk
    )
    return {
        "platform": "xhs",
        "days": days,
        "product_pk": product_pk,
        "series": series,
        "points": len(series),
    }


@router.get("/overview")
def overview(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    notes = _owned_notes(db, current_user)
    comments = _owned_comments(db, current_user)
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
    total_engagement = sum(_note_metrics(note)["engagement"] for note in notes)
    hot_topics = _topic_items(db, current_user, notes)[:5]
    recent_activity = [
        {"type": "note", "title": note.title or note.note_id, "status": "saved"} for note in notes[:5]
    ]
    return {
        "platform": "xhs",
        "today_crawls": len([note for note in notes if note.created_at.date() == today]),
        "saved_notes": len(notes),
        "pending_publishes": len(pending_publishes),
        "healthy_accounts": len([account for account in accounts if account.status in ("active", "healthy")]),
        "at_risk_accounts": len([account for account in accounts if account.status not in ("active", "healthy")]),
        "comment_count": len(comments),
        "total_engagement": total_engagement,
        "hot_topics": hot_topics,
        "recent_activity": recent_activity,
    }


@router.get("/top-content")
def top_content(
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    notes = sorted(_owned_notes(db, current_user), key=lambda note: _note_metrics(note)["engagement"], reverse=True)
    return {"items": [_serialize_top_note(note) for note in notes[:limit]]}


@router.get("/hot-topics")
def hot_topics(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return {"items": _topic_items(db, current_user, _owned_notes(db, current_user))}


@router.get("/engagement")
def engagement(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    daily: dict[str, Counter] = defaultdict(Counter)
    for note in _owned_notes(db, current_user):
        metrics = _note_metrics(note)
        key = note.created_at.date().isoformat()
        daily[key].update(metrics)
    return {
        "items": [
            {
                "date": date,
                "likes": counter["likes"],
                "collects": counter["collects"],
                "comments": counter["comments"],
                "shares": counter["shares"],
                "engagement": counter["engagement"],
            }
            for date, counter in sorted(daily.items())
        ]
    }


@router.get("/keyword-trends")
def keyword_trends(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return {"items": _keyword_trend_items(db, current_user, _owned_notes(db, current_user))}


@router.get("/comment-insights")
def comment_insights(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    comments = _owned_comments(db, current_user)
    repeated_terms = Counter()
    for comment in comments:
        for term in ("价格", "多少钱", "链接", "适合", "怎么", "哪里", "推荐", "通勤"):
            if term in comment.content:
                repeated_terms[term] += 1
    return {
        "total_comments": len(comments),
        "question_count": len([comment for comment in comments if "?" in comment.content or "？" in comment.content]),
        "top_terms": [{"term": term, "count": count} for term, count in repeated_terms.most_common(10)],
        "top_comments": [
            {
                "id": comment.id,
                "note_id": comment.note_id,
                "user_name": comment.user_name,
                "content": comment.content,
                "like_count": comment.like_count,
            }
            for comment in comments[:10]
        ],
    }


@router.post("/reports")
def create_report(
    payload: AnalyticsReportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    notes = _owned_report_notes(db, current_user, payload.note_ids)
    generated_at = shanghai_now()
    report_payload = _build_report_payload(db, current_user, notes, generated_at)
    file_name, file_path = _write_report_file(current_user, report_payload)
    return {
        "report_type": "operations",
        "generated_at": generated_at.isoformat(),
        "note_count": len(notes),
        "file_name": file_name,
        "file_path": str(file_path),
        "download_url": f"/api/files/exports/{file_name}",
        "summary": report_payload["summary"],
    }


@router.post("/platform-hot-topics/refresh")
def refresh_platform_hot_topics(
    payload: PlatformHotTopicRefresh,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """采集平台按商品推荐的热度词（设计 17 §13.11，M3 方向 2）。

    口径：平台接口 aigc-publish/attributes 按 item_id 调用
    => 回来的是「与该商品相关」的平台热度词，不是全站热搜榜。
    只读：POST 只是平台的查询形式（body 传商品 ID）。
    无令牌 / 接口失败 => 结构化错误，不造话题。
    """
    item_ids = [str(i).strip() for i in payload.item_ids if str(i).strip()]
    if not item_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "ITEM_IDS_REQUIRED", "message": "请至少选择一个商品"},
        )

    owned = {
        row.item_id: row
        for row in db.scalars(
            select(ShopProduct).where(
                ShopProduct.user_id == current_user.id, ShopProduct.item_id.in_(item_ids)
            )
        ).all()
    }
    unknown = [i for i in item_ids if i not in owned]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "PRODUCT_NOT_FOUND",
                "message": "商品不在本账号台账：%s" % ", ".join(unknown[:3]),
            },
        )

    account_cookies = {}
    topics_written = 0
    matches_written = 0
    failed = []

    for item_id in item_ids:
        product = owned[item_id]
        account_id = product.platform_account_id
        if account_id not in account_cookies:
            account_cookies[account_id] = qianfan_cookies_for_account(db, account_id)
        cookies = account_cookies[account_id]
        if not cookies:
            failed.append({"item_id": item_id, "reason": "该商品所属账号尚未导入千帆（ark）令牌"})
            continue

        try:
            adapter = adapter_factory(cookies)
            success, message, payload = adapter.aigc_publish_attributes(item_id)
        except Exception as exc:  # noqa: BLE001 —— 单个商品失败不拖垮整批
            failed.append({"item_id": item_id, "reason": "%s: %s" % (type(exc).__name__, exc)})
            continue

        if not success or not isinstance(payload, dict):
            failed.append({"item_id": item_id, "reason": str(message)[:200] or "接口失败"})
            continue

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        topics_written += collect_topics_for_item(
            db,
            user_id=current_user.id,
            item_id=item_id,
            attributes=data,
            platform_account_id=account_id,
        )
        matches_written += match_topics_to_goods(
            db, user_id=current_user.id, attributes=data, item_id=item_id
        )

    return {
        "items": len(item_ids),
        "topics": topics_written,
        "matches": matches_written,
        "failed": failed,
        "stat_date": hot_topic_stat_date_of(shanghai_now()),
        "source_note": "平台按所选商品推荐的热度词（非全站热搜榜）",
    }


@router.post("/platform-hot-topics/generate")
def generate_from_platform_hot_topic(
    payload: PlatformHotTopicGenerate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """按选中的平台热度词入队一次 LLM 生成（设计 16 §3.2「复用方向1 生成」）。

    复用而不重造（DEF-044）：生成走 start_llm_generate_note
    （与 /api/xhs/product-notes/generate-llm 同一个入口）。
    终态与方向 1 同一个出口 —— 轮询 GET /api/xhs/product-notes/generate-llm-queue。
    """
    topic = payload.topic.strip()
    if not topic:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "TOPIC_REQUIRED", "message": "请选择要驱动生成的话题"},
        )

    owned = db.scalars(
        select(HotTopic).where(HotTopic.user_id == current_user.id, HotTopic.topic == topic)
    ).first()
    if owned is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "TOPIC_NOT_COLLECTED",
                "message": "该话题不在已采集的平台热度词里，请先采集",
            },
        )

    account = _owned_account(db, current_user, payload.platform_account_id)
    row = _ledger_product(db, current_user, account, payload.item_id)

    tag = topic_token(topic)
    try:
        receipt = start_llm_generate_note(
            db,
            current_user.id,
            shop_product_id=int(row.id),
            account_id=int(account.id),
            template_id=payload.template_id,
            media_source=payload.media_source or "goods",
            hot_topic=topic,
        )
    except NoteGenerationError as error:
        raise _llm_generation_error(error)

    return {
        "ok": True,
        "topic": topic,
        "topic_tag": tag,
        "platform_account_id": int(account.id),
        "item_id": payload.item_id,
        **receipt,
    }


@router.get("/platform-hot-topics")
def list_platform_hot_topics(
    item_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出已采集的平台热度词（按平台热度降序）。"""
    return {
        "items": list_hot_topics(db, user_id=current_user.id, item_id=item_id, limit=limit),
        "source_note": "平台按所选商品推荐的热度词（非全站热搜榜）",
    }


@router.get("/platform-hot-topics/matches")
def list_platform_hot_topic_matches(
    item_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """话题↔商品匹配结果（按相关度降序，附可解释成分 match_reason）。"""
    return {"items": list_topic_matches(db, user_id=current_user.id, item_id=item_id, limit=limit)}


@router.get("/benchmarks")
def benchmarks(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    notes = _owned_notes(db, current_user)
    targets = db.scalars(
        select(MonitoringTarget)
        .where(
            MonitoringTarget.user_id == current_user.id,
            MonitoringTarget.platform == "xhs",
            MonitoringTarget.target_type.in_(("account", "brand")),
        )
        .order_by(MonitoringTarget.created_at.desc(), MonitoringTarget.id.desc())
    ).all()

    items: list[dict[str, Any]] = []
    for target in targets:
        matched = _benchmark_matches(notes, target)
        total_engagement = sum(_note_metrics(note)["engagement"] for note in matched)
        items.append(
            {
                "target_id": target.id,
                "target_type": target.target_type,
                "name": target.name or target.value,
                "value": target.value,
                "status": target.status,
                "last_refreshed_at": target.last_refreshed_at.isoformat() if target.last_refreshed_at else None,
                "matched_notes": len(matched),
                "total_engagement": total_engagement,
                "average_engagement": round(total_engagement / len(matched), 2) if matched else 0,
                "top_notes": [_serialize_top_note(note) for note in matched[:5]],
            }
        )

    items = sorted(items, key=lambda item: (item["total_engagement"], item["matched_notes"], item["name"]), reverse=True)
    total_matched = sum(item["matched_notes"] for item in items)
    total_engagement = sum(item["total_engagement"] for item in items)
    return {
        "total_targets": len(items),
        "matched_notes": total_matched,
        "total_engagement": total_engagement,
        "average_engagement": round(total_engagement / total_matched, 2) if total_matched else 0,
        "items": items,
    }


@router.post("/benchmarks/{target_id}/create-drafts")
def create_benchmark_drafts(
    target_id: int,
    limit: int = Query(5, ge=1, le=20),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    target = _get_owned_benchmark_target(db, current_user, target_id)
    matched = _benchmark_matches(_owned_notes(db, current_user), target)[:limit]
    drafts = [
        AiDraft(
            user_id=current_user.id,
            platform="xhs",
            title=note.title,
            body=note.content,
            source_note_id=note.id,
        )
        for note in matched
    ]
    db.add_all(drafts)
    db.commit()
    for draft in drafts:
        db.refresh(draft)
    return {
        "created_count": len(drafts),
        "items": [_serialize_draft(draft) for draft in drafts],
    }
