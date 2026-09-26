from __future__ import annotations

import json
import time
from typing import Any, Generator, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.app.api.platforms.xhs.pc import (
    _get_owned_pc_account_cookies,
    _normalize_detail_payload,
    _normalize_search_item,
    normalize_comment_payload,
    get_xhs_pc_api_adapter_factory,
)
from backend.app.api.tasks import serialize_task
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user, get_owned_active_account
from backend.app.models import Note, NoteAsset, PlatformAccount, Task, User
from backend.app.models.note import (
    NOTE_TYPE_LOCAL,
    NOTE_TYPE_SYNCED,
    PUBLISH_STATUS_PENDING,
    PUBLISH_STATUS_PUBLISHED,
    set_note_publish_status,
)
from backend.app.services.note_topic_service import apply_sync_topics

router = APIRouter(prefix="/xhs/crawl", tags=["xhs-crawl"])


class CrawlSearchNotesRequest(BaseModel):
    account_id: int
    keyword: str = Field(min_length=1, max_length=120)
    page: int = Field(default=1, ge=1)
    save_to_library: bool = True
    fetch_comments: bool = False


class CrawlNoteUrlsRequest(BaseModel):
    account_id: int
    urls: list[str] = Field(min_length=1, max_length=50)
    save_to_library: bool = True
    fetch_comments: bool = False


class CrawlUserNotesRequest(BaseModel):
    account_id: int
    user_url: str = Field(min_length=1)
    save_to_library: bool = True


class DataCrawlRequest(BaseModel):
    account_id: int
    mode: Literal["note_urls", "search", "comments"]
    urls: list[str] = Field(default_factory=list, max_length=100)
    keyword: str = Field(default="", max_length=120)
    pages: int = Field(default=1, ge=1, le=20)
    max_notes: int = Field(default=20, ge=1, le=200)
    time_sleep: float = Field(default=0, ge=0, le=60)
    fetch_comments: bool = False
    sort_type_choice: int = Field(default=0, ge=0, le=4)
    note_type: int = Field(default=0, ge=0, le=2)
    note_time: int = Field(default=0, ge=0, le=3)
    note_range: int = Field(default=0, ge=0, le=3)
    pos_distance: int = Field(default=0, ge=0, le=2)
    geo: str = ""


def _serialize_note(note: Note) -> dict[str, Any]:
    return {
        "id": note.id,
        "platform": note.platform,
        "platform_account_id": note.platform_account_id,
        "note_id": note.note_id,
        "title": note.title,
        "content": note.content,
        "author_name": note.author_name,
        # 同一个 `Note` 实体的两份序列化形状之一（另一份在 `api/notes.py`）：发布状态
        # 两边都要有，否则前端拿同一个类型读会时而 `undefined`（设计 13 §19.12）。
        # 采集链路落库的行恒为 `False`（默认值），这里如实输出。
        "published": bool(note.published),
        "raw_json": note.raw_json,
        "created_at": note.created_at.isoformat(),
    }


def _create_crawl_task(
    db: Session,
    current_user: User,
    crawl_type: str,
    payload: dict[str, Any],
) -> Task:
    task = Task(
        user_id=current_user.id,
        platform="xhs",
        task_type="crawl",
        status="running",
        progress=10,
        payload={"crawl_type": crawl_type, **payload},
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _complete_task(db: Session, task: Task, payload: dict[str, Any]) -> Task:
    task.status = "completed"
    task.progress = 100
    task.payload = {**(task.payload or {}), **payload}
    db.commit()
    db.refresh(task)
    return task


def _fail_task(db: Session, task: Task, error: str) -> None:
    task.status = "failed"
    task.progress = 100
    task.payload = {**(task.payload or {}), "error": error}
    db.commit()


def _data_items(raw_payload: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_payload, dict):
        return []
    data = raw_payload.get("data") if isinstance(raw_payload.get("data"), dict) else raw_payload
    items = data.get("items") or data.get("notes") or data.get("list") or []
    return [item for item in items if isinstance(item, dict) and item.get("model_type") not in ("rec_query", "hot_query")]


def _raw_with_metrics(normalized: dict[str, Any]) -> dict[str, Any]:
    raw = normalized.get("raw") if isinstance(normalized.get("raw"), dict) else {}
    return {
        **raw,
        "note_url": normalized.get("note_url", ""),
        "tags": normalized.get("tags", []),
        "likes": normalized.get("likes", 0),
        "collects": normalized.get("collects", 0),
        "comments": normalized.get("comments", 0),
        "shares": normalized.get("shares", 0),
    }


def _image_urls(normalized: dict[str, Any]) -> list[str]:
    urls = normalized.get("image_urls")
    if isinstance(urls, list) and urls:
        return [str(url) for url in urls if url]
    cover_url = normalized.get("cover_url")
    return [str(cover_url)] if cover_url else []


def _video_url(normalized: dict[str, Any]) -> str:
    return str(normalized.get("video_url") or normalized.get("video_addr") or "")


def _merge_note_assets(
    db: Session, note: Note, urls: list[str], *, asset_type: str = "image"
) -> int:
    """把资产**补**进本地：只加本地没有的 url，不删不改已有（填空语义，§23）。

    这是「已存在的笔记只更新、不摧毁」的资产侧实现，与
    `note_sync_service.hydrate_note_detail` 的图片合并**同一份语义** —— 后者委托
    到本函数，不另写一份（DEF-044/047/058：同一语义两份实现必漂移）。
    """
    if not urls:
        return 0
    existing = {
        (str(asset.url), str(asset.asset_type))
        for asset in db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id)).all()
    }
    added = 0
    for raw_url in urls:
        url = str(raw_url or "").strip()
        if not url or (url, asset_type) in existing:
            continue
        local_name = _download_asset(url, note.user_id, asset_type)
        db.add(
            NoteAsset(
                note_id=note.id, asset_type=asset_type, url=url, local_path=local_name or ""
            )
        )
        existing.add((url, asset_type))
        added += 1
    return added


def _save_normalized_notes(
    db: Session,
    account: PlatformAccount,
    normalized_items: list[dict[str, Any]],
    *,
    published: bool = False,
    note_type: str = NOTE_TYPE_LOCAL,
) -> list[Note]:
    """归一化条目落库（按 `(user_id, note_id)` upsert）。

    ``published`` 由**调用方声明**这一批是不是「账号自己已在平台上发布的作品」
    （设计 13 §19.12）：

    * 笔记同步链路（www / creator 源）传 ``True``；
    * 采集（本文件三个端点）与监控采集**不传**，用默认 ``False``。

    语义是**只升不降**：``True`` ⇒ 新建行与既有行都置已发布；默认 ``False`` ⇒
    **只作用于新建行**，命中既有行时一个字节都不碰。反例：若无条件写 ``False``，
    则「先同步（已发布）→ 事后在发现页又采集到同一条」会把已发布**降级**成待发布
    （现象是「同步过的笔记自己变回了待发布」）。

    ``note_type``（设计 13 §25）与 ``published`` **同一把尺子**：调用方按**落库链路**
    声明这一批是不是同步链路拉来的 —— 同步链路传 ``"synced"``，采集链路用默认
    ``"local"``。同样**只升不降**：``"synced"`` 命中既有行时升级，默认值只作用于
    新建行。否则「先同步、后采集到同一条」会把同步笔记变成本地笔记，内容库详情卡的
    「同步」按钮随之消失。

    ⚠️ **不要在这里按 `platform_account_id` / `raw_json` 反推来源** —— 反推 = 造
    第二份真相源（DEF-044 / DEF-058 的教训）；来源只有调用方知道。
    """
    saved: list[Note] = []
    for normalized in normalized_items:
        note_id = str(normalized.get("note_id") or "").strip()
        if not note_id:
            continue
        note = db.scalars(
            select(Note).where(Note.user_id == account.user_id, Note.note_id == note_id)
        ).first()
        if note is None:
            note = Note(
                user_id=account.user_id,
                platform_account_id=account.id,
                platform=account.platform,
                note_id=note_id,
                note_type=note_type,
            )
            # 发布状态经**唯一入口**写（设计 13 §32.3.2）：同步链路落库 ⇒ 已发布；
            # 采集链路 ⇒ 待发布。`published` 是它的投影，不再单独赋值。
            set_note_publish_status(
                note, PUBLISH_STATUS_PUBLISHED if published else PUBLISH_STATUS_PENDING
            )
            db.add(note)
        else:
            # **命中既有行也要重挂归属**（台账 §4.9 / 问题 P2）。
            # 账号删除后重建会拿到**新的 id**，若这里不刷新，老笔记会永久挂在已删除的
            # 账号上（旧实现只覆盖 title/content，漏了这一步），页面上表现为「同步成功但看不到笔记」。
            note.platform_account_id = account.id
            if published:
                # 只升不降：采集链路（published=False）走到这里时**不碰**这一列。
                set_note_publish_status(note, PUBLISH_STATUS_PUBLISHED)
            if note_type == NOTE_TYPE_SYNCED:
                # 只升不降（§25）：同步链路见过这条 ⇒ 它是三方同步笔记；
                # 采集链路（note_type="local"）走到这里时**不碰**这一列。
                note.note_type = NOTE_TYPE_SYNCED
        note.title = str(normalized.get("title") or "")
        # ⚠️ **正文只在本地为空时写**（§23，2026-09-22）：三个列表源都不带正文，
        # 「归一化后 content 为空」是**列表链路的固有形态**，不是「平台把正文删了」。
        # 旧实现无条件覆盖 ⇒ 一轮重同步就把详情补齐过的正文抹掉，且 `hydrated_at`
        # 已写过 ⇒ 候选集再也挑不到它 ⇒ 正文**永久丢失**。
        if not str(note.content or "").strip():
            note.content = str(normalized.get("content") or "")
        note.author_name = str(normalized.get("author_name") or "")
        # 话题（§34.2）：正文里的 `#话题` 拆进 `topics` 表 + `note_topics` 关系
        # （正文不再存话题）；条目自带的话题（`normalized["tags"]`）是权威声明。
        # **只增不删** —— 唯一实现 `apply_sync_topics`，与详情补齐同一把尺子。
        apply_sync_topics(db, note, normalized.get("tags") or [])
        note.raw_json = _raw_with_metrics(normalized)
        # ⚠️ 持锁纪律（设计 13 §24.7，用户 2026-09-23 口径「不同类型任务互不影响、
        # 不应排序等待」）：**先落笔记行（短事务），再下载素材（零锁），再落资产行**。
        # 旧实现在 flush 之后、commit 之前逐图下载 ⇒ 写锁跨网络持有数十秒~数分钟
        # （一页 50 条 × 约 4 图）⇒ 商品同步/发布/巡检等一切写者撞 busy_timeout 后
        # `database is locked` 失败。flush 只为物化 note.id（NoteAsset 要外键）。
        db.flush()
        db.commit()
        # ⚠️ 图集同样**只增不删**（§23）：旧实现先 `delete(NoteAsset)` 再按列表 URL
        # 重建，而列表常常只给 1 张封面 ⇒ 详情补到的 4 张被挤成 1 张。
        # 「平台撤掉某张图」这种真变更不在本链路处理（同步链路不做删除维度，§17.5.3）。
        _merge_note_assets(db, note, _image_urls(normalized), asset_type="image")
        video_url = _video_url(normalized)
        if video_url:
            _merge_note_assets(db, note, [video_url], asset_type="video")
        db.commit()
        saved.append(note)

    for note in saved:
        db.refresh(note)
    return saved


def _download_asset(url: str, user_id: int, asset_type: str) -> str | None:
    from backend.app.services.asset_downloader import download_asset_to_local
    return download_asset_to_local(url, user_id, asset_type)


def _sleep_between_requests(seconds: float) -> None:
    if seconds > 0:
        time.sleep(min(seconds, 60))


def _crawl_data_item(
    *,
    source: str,
    status: str,
    note: dict[str, Any] | None = None,
    comments: list[dict[str, Any]] | None = None,
    error: str = "",
) -> dict[str, Any]:
    return {
        "source": source,
        "status": status,
        "error": error,
        "note": note,
        "comments": comments or [],
        "comment_count": len(comments or []),
    }


def _owned_pc_account(db: Session, current_user: User, account_id: int) -> PlatformAccount:
    """（薄别名）**唯一实现见 `core/deps.py::get_owned_active_account`**（DEF-055）。

    本函数曾是同一语义的 4 份拷贝之一（且是**手写内联**的那份），**漏了软删闸** ——
    已删除的账号仍能从这里过闸、继续跑采集。
    """
    return get_owned_active_account(
        db, current_user, account_id, platform="xhs", sub_type="pc"
    )


@router.post("/search-notes")
def crawl_search_notes(
    payload: CrawlSearchNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
):
    account = _owned_pc_account(db, current_user, payload.account_id)
    cookies = _get_owned_pc_account_cookies(db, current_user, payload.account_id)
    task = _create_crawl_task(
        db,
        current_user,
        "search_notes",
        {"account_id": account.id, "keyword": payload.keyword, "page": payload.page},
    )
    success, message, raw_payload = adapter_factory(cookies).search_note(payload.keyword, page=payload.page)
    if not success:
        _fail_task(db, task, message or "XHS search crawl failed")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=message or "XHS search crawl failed")

    normalized_items = [_normalize_search_item(item) for item in _data_items(raw_payload)]
    saved_notes = _save_normalized_notes(db, account, normalized_items) if payload.save_to_library else []
    task = _complete_task(
        db,
        task,
        {"result_count": len(normalized_items), "saved_count": len(saved_notes)},
    )
    return {
        "task": serialize_task(task),
        "result_count": len(normalized_items),
        "saved_count": len(saved_notes),
        "items": [_serialize_note(note) for note in saved_notes],
        "raw": raw_payload,
    }


@router.post("/note-urls")
def crawl_note_urls(
    payload: CrawlNoteUrlsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
):
    account = _owned_pc_account(db, current_user, payload.account_id)
    cookies = _get_owned_pc_account_cookies(db, current_user, payload.account_id)
    task = _create_crawl_task(
        db,
        current_user,
        "note_urls",
        {"account_id": account.id, "url_count": len(payload.urls)},
    )
    adapter = adapter_factory(cookies)
    normalized_items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for url in payload.urls:
        success, message, raw_payload = adapter.get_note_info(url)
        if success:
            normalized_items.append(_normalize_detail_payload(raw_payload or {}))
        else:
            errors.append({"url": url, "error": message or "XHS note detail crawl failed"})

    saved_notes = _save_normalized_notes(db, account, normalized_items) if payload.save_to_library else []
    task = _complete_task(
        db,
        task,
        {"result_count": len(normalized_items), "saved_count": len(saved_notes), "errors": errors},
    )
    return {
        "task": serialize_task(task),
        "result_count": len(normalized_items),
        "saved_count": len(saved_notes),
        "errors": errors,
        "items": [_serialize_note(note) for note in saved_notes],
    }


@router.post("/user-notes")
def crawl_user_notes(
    payload: CrawlUserNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
):
    account = _owned_pc_account(db, current_user, payload.account_id)
    cookies = _get_owned_pc_account_cookies(db, current_user, payload.account_id)
    task = _create_crawl_task(
        db,
        current_user,
        "user_notes",
        {"account_id": account.id, "user_url": payload.user_url},
    )
    success, message, raw_payload = adapter_factory(cookies).get_user_notes(payload.user_url)
    if not success:
        _fail_task(db, task, message or "XHS user notes crawl failed")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=message or "XHS user notes crawl failed")

    normalized_items = [_normalize_search_item(item) for item in _data_items(raw_payload)]
    saved_notes = _save_normalized_notes(db, account, normalized_items) if payload.save_to_library else []
    task = _complete_task(
        db,
        task,
        {"result_count": len(normalized_items), "saved_count": len(saved_notes)},
    )
    return {
        "task": serialize_task(task),
        "result_count": len(normalized_items),
        "saved_count": len(saved_notes),
        "items": [_serialize_note(note) for note in saved_notes],
        "raw": raw_payload,
    }


def _sse_event(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/data")
def crawl_data(
    payload: DataCrawlRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
):
    account = _owned_pc_account(db, current_user, payload.account_id)
    cookies = _get_owned_pc_account_cookies(db, current_user, payload.account_id)
    task = _create_crawl_task(
        db,
        current_user,
        f"data_{payload.mode}",
        {
            "account_id": account.id,
            "mode": payload.mode,
            "keyword": payload.keyword,
            "url_count": len(payload.urls),
            "pages": payload.pages,
            "time_sleep": payload.time_sleep,
        },
    )
    task_id = task.id
    adapter = adapter_factory(cookies)

    def generate() -> Generator[str, None, None]:
        items: list[dict[str, Any]] = []
        normalized_for_save: list[dict[str, Any]] = []
        error_occurred = False

        try:
            if payload.mode == "note_urls":
                for index, url in enumerate(payload.urls):
                    success, message, raw_payload = adapter.get_note_info(url)
                    if success:
                        note = _normalize_detail_payload(raw_payload or {})
                        note["note_url"] = note.get("note_url") or url
                        normalized_for_save.append(note)
                        comments_list: list[dict[str, Any]] = []
                        if payload.fetch_comments:
                            cs, cm, cp = adapter.get_note_comments(url)
                            if cs:
                                comments_list = normalize_comment_payload(cp)
                            else:
                                item = _crawl_data_item(source=url, status="failed", note=note, error=cm or "comment crawl failed")
                                items.append(item)
                                yield _sse_event({"type": "item", "index": len(items) - 1, "item": item})
                                _sleep_between_requests(payload.time_sleep)
                                continue
                        item = _crawl_data_item(source=url, status="success", note=note, comments=comments_list)
                    else:
                        item = _crawl_data_item(source=url, status="failed", error=message or "detail crawl failed")
                    items.append(item)
                    yield _sse_event({"type": "item", "index": len(items) - 1, "item": item})
                    if index < len(payload.urls) - 1:
                        _sleep_between_requests(payload.time_sleep)

            elif payload.mode == "comments":
                for index, url in enumerate(payload.urls):
                    success, message, raw_payload = adapter.get_note_comments(url)
                    if success:
                        item = _crawl_data_item(source=url, status="success", comments=normalize_comment_payload(raw_payload))
                    else:
                        item = _crawl_data_item(source=url, status="failed", error=message or "comment crawl failed")
                    items.append(item)
                    yield _sse_event({"type": "item", "index": len(items) - 1, "item": item})
                    if index < len(payload.urls) - 1:
                        _sleep_between_requests(payload.time_sleep)

            else:
                if not payload.keyword.strip():
                    yield _sse_event({"type": "error", "message": "Keyword is required"})
                    return
                seen_urls: list[str] = []
                for page in range(1, payload.pages + 1):
                    success, message, raw_payload = adapter.search_note(
                        payload.keyword, page=page,
                        sort_type_choice=payload.sort_type_choice,
                        note_type=payload.note_type,
                        note_time=payload.note_time,
                        note_range=payload.note_range,
                        pos_distance=payload.pos_distance,
                        geo=payload.geo,
                    )
                    if not success:
                        item = _crawl_data_item(source=f"page:{page}", status="failed", error=message or "search failed")
                        items.append(item)
                        yield _sse_event({"type": "item", "index": len(items) - 1, "item": item})
                        break
                    yield _sse_event({"type": "progress", "message": f"搜索第 {page} 页完成，开始获取详情..."})
                    for raw_item in _data_items(raw_payload):
                        if len(items) >= payload.max_notes:
                            break
                        search_note = _normalize_search_item(raw_item)
                        note_url = search_note.get("note_url") or ""
                        source = note_url or str(search_note.get("note_id") or f"page:{page}")
                        if source in seen_urls:
                            continue
                        seen_urls.append(source)
                        detail_note = search_note
                        if note_url:
                            ds, dm, dp = adapter.get_note_info(note_url)
                            if ds:
                                detail_note = _normalize_detail_payload(dp or {}, source_url=note_url)
                                detail_note["note_url"] = detail_note.get("note_url") or note_url
                            else:
                                item = _crawl_data_item(source=source, status="failed", note=search_note, error=dm or "detail failed")
                                items.append(item)
                                yield _sse_event({"type": "item", "index": len(items) - 1, "item": item})
                                _sleep_between_requests(payload.time_sleep)
                                continue
                        comments_list = []
                        if payload.fetch_comments and note_url:
                            cs, cm, cp = adapter.get_note_comments(note_url)
                            if cs:
                                comments_list = normalize_comment_payload(cp)
                            else:
                                item = _crawl_data_item(source=source, status="failed", note=detail_note, error=cm or "comment failed")
                                items.append(item)
                                yield _sse_event({"type": "item", "index": len(items) - 1, "item": item})
                                _sleep_between_requests(payload.time_sleep)
                                continue
                        normalized_for_save.append(detail_note)
                        item = _crawl_data_item(source=source, status="success", note=detail_note, comments=comments_list)
                        items.append(item)
                        yield _sse_event({"type": "item", "index": len(items) - 1, "item": item})
                        _sleep_between_requests(payload.time_sleep)
                    if len(items) >= payload.max_notes:
                        break
                    data = (raw_payload or {}).get("data") or {}
                    if not data.get("has_more", False):
                        break

        except Exception as exc:
            error_occurred = True
            yield _sse_event({"type": "error", "message": str(exc)})

        success_count = len([i for i in items if i["status"] == "success"])
        failed_count = len(items) - success_count
        try:
            if error_occurred:
                _fail_task(db, task, "partial failure")
            else:
                _complete_task(db, task, {"result_count": success_count, "failed_count": failed_count})
        except Exception:
            pass

        yield _sse_event({
            "type": "done",
            "task_id": task_id,
            "total": len(items),
            "success_count": success_count,
            "failed_count": failed_count,
        })

    return StreamingResponse(generate(), media_type="text/event-stream")
