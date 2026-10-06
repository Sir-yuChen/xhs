from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Literal, NamedTuple, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.adapters.xhs.pc_api_adapter import XhsPcApiAdapter
from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user, get_owned_active_account
from backend.app.models import (
    NoTrafficCleanupLog,
    PlatformAccount,
    Task,
    User,
)
from backend.app.models.shop import ShopNoteGoods
from backend.app.services.account_service import (
    CHANNEL_ARK,
    CHANNEL_CREATOR,
    CHANNEL_WWW,
    coerce_count,
    has_web_session,
    latest_cookies_for_channel,
)
from backend.app.services.shop_binding_service import (
    get_shop_state,
    has_ark_session,
    parse_state_time,
    qianfan_cookies_for_account,
    synced_note_count,
)
from backend.app.services.shop_goods_service import (
    existing_rows,
    missing_note_ids,
    sync_note_ids,
)
from backend.app.services.no_traffic_cleanup_service import (
    DEFAULT_DELETE_BATCH,
    running_cleanup_for_account,
    run_no_traffic_cleanup_background,
)
from backend.app.services.no_traffic_service import (
    CleanupQuery,
    TrafficThresholds,
    list_no_traffic_candidates,
    serialize_candidate,
)

router = APIRouter(prefix="/xhs/pc", tags=["xhs-pc"])


class SearchNotesRequest(BaseModel):
    account_id: int
    keyword: str = Field(min_length=1, max_length=120)
    page: int = Field(default=1, ge=1)
    sort_type_choice: int = Field(default=0, ge=0, le=4)
    note_type: int = Field(default=0, ge=0, le=2)
    note_time: int = Field(default=0, ge=0, le=3)
    note_range: int = Field(default=0, ge=0, le=3)
    pos_distance: int = Field(default=0, ge=0, le=2)
    geo: str = ""


class NoteDetailRequest(BaseModel):
    account_id: int
    url: str = Field(min_length=1)


class NoteCommentsRequest(BaseModel):
    account_id: int
    note_url: str = Field(min_length=1)


def get_xhs_pc_api_adapter_factory():
    return XhsPcApiAdapter


def _cookies_to_string(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return stripped
    if stripped.startswith("{"):
        cookies = json.loads(stripped)
        return "; ".join(f"{key}={cookie_value}" for key, cookie_value in cookies.items())
    return stripped


def _metric(value: Any) -> int:
    """平台计数 → int，**缺值返回 0**（笔记卡片/详情的历史口径）。

    ⚠️ 账号级计数**不要**用它：那里「缺值」必须能被上层区分出来（显示 `-` 而不是 0），
    用 `services.account_service.coerce_count()`（返回 `None`）。
    转换实现只有一份 —— 本函数委托给 `coerce_count`，避免同一字段被两条链路解析成不同结果。
    """
    return coerce_count(value) or 0


def _first_url(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        return _first_url(value[0])
    if isinstance(value, dict):
        info_list = value.get("info_list")
        if isinstance(info_list, list) and len(info_list) > 1 and isinstance(info_list[1], dict):
            preferred_url = info_list[1].get("url")
            if preferred_url:
                return str(preferred_url)
        for key in ("url_default", "url_pre", "url", "src"):
            if value.get(key):
                return str(value[key])
        for nested in ("info_list", "image_list", "images"):
            if value.get(nested):
                return _first_url(value[nested])
    return ""


def _all_urls(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        urls: list[str] = []
        for item in value:
            urls.extend(_all_urls(item))
        return [url for index, url in enumerate(urls) if url and url not in urls[:index]]
    if isinstance(value, dict):
        direct = _first_url(value)
        if direct:
            return [direct]
        urls: list[str] = []
        for nested in ("info_list", "image_list", "images"):
            if value.get(nested):
                urls.extend(_all_urls(value[nested]))
        return [url for index, url in enumerate(urls) if url and url not in urls[:index]]
    return []


def _tag_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    for item in value:
        if isinstance(item, str):
            tag = item
        elif isinstance(item, dict):
            tag = item.get("name") or item.get("tag_name") or item.get("title") or ""
        else:
            tag = ""
        if tag and tag not in tags:
            tags.append(str(tag))
    return tags


def _note_url(
    note_id: str,
    item: dict[str, Any],
    card: dict[str, Any],
    default_xsec_source: str = "pc_feed",
) -> str:
    """Build the explore URL for a note.

    ``default_xsec_source`` matters more than it looks: the platform binds an
    ``xsec_token`` to the scene that issued it. A token handed out by
    ``user_posted`` is rejected by ``/v1/feed`` when replayed with
    ``xsec_source=pc_search`` ("笔记不存在"), while ``pc_user`` / ``pc_feed``
    succeed (verified 2026-09-18). Callers that know their scene must pass it
    rather than relying on the default. See design doc §18.4.
    """
    xsec_token = card.get("xsec_token") or item.get("xsec_token") or ""
    if note_id and xsec_token:
        xsec_source = card.get("xsec_source") or item.get("xsec_source") or default_xsec_source
        return f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={xsec_token}&xsec_source={xsec_source}"
    for value in (
        card.get("note_url"),
        card.get("url"),
        card.get("share_url"),
        item.get("note_url"),
        item.get("url"),
        item.get("share_url"),
    ):
        if isinstance(value, str) and value:
            return value
    if not note_id:
        return ""
    return f"https://www.xiaohongshu.com/explore/{note_id}"


def _comment_id(comment: dict[str, Any]) -> str:
    return str(comment.get("comment_id") or comment.get("id") or comment.get("commentId") or "")


def _comment_user(comment: dict[str, Any]) -> dict[str, Any]:
    return comment.get("user_info") or comment.get("user") or comment.get("author") or {}


def _comment_children(comment: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("sub_comments", "sub_comment", "comments", "replies", "children"):
        value = comment.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    sub_comment_payload = comment.get("sub_comment_info")
    if isinstance(sub_comment_payload, dict):
        value = sub_comment_payload.get("comments")
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _normalize_comment(comment: dict[str, Any], parent_comment_id: str | None = None) -> dict[str, Any]:
    user = _comment_user(comment)
    return {
        "comment_id": _comment_id(comment),
        "user_name": str(user.get("nickname") or user.get("name") or comment.get("user_name") or ""),
        "user_id": str(user.get("user_id") or user.get("id") or comment.get("user_id") or "") or None,
        "content": str(comment.get("content") or comment.get("text") or comment.get("desc") or ""),
        "like_count": _metric(comment.get("like_count") or comment.get("liked_count") or comment.get("likes")),
        "parent_comment_id": parent_comment_id,
        "created_at_remote": comment.get("create_time") or comment.get("created_at") or comment.get("time"),
        "raw_json": comment,
    }


def _flatten_comments(comments: list[dict[str, Any]], parent_comment_id: str | None = None) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for comment in comments:
        normalized = _normalize_comment(comment, parent_comment_id=parent_comment_id)
        if normalized["comment_id"]:
            flattened.append(normalized)
            child_parent_id = normalized["comment_id"]
        else:
            child_parent_id = parent_comment_id
        flattened.extend(_flatten_comments(_comment_children(comment), parent_comment_id=child_parent_id))
    return flattened


def _extract_comment_list(raw_payload: Any) -> list[dict[str, Any]]:
    if isinstance(raw_payload, list):
        return [item for item in raw_payload if isinstance(item, dict)]
    if not isinstance(raw_payload, dict):
        return []
    data = raw_payload.get("data") if isinstance(raw_payload.get("data"), dict) else raw_payload
    for key in ("comments", "items", "list"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def normalize_comment_payload(raw_payload: Any) -> list[dict[str, Any]]:
    return _flatten_comments(_extract_comment_list(raw_payload))


def _normalize_search_item(item: dict[str, Any], default_xsec_source: str = "pc_feed") -> dict[str, Any]:
    """Normalise one note into the shared front-end contract.

    Two shapes are in the wild and both must work: search results nest the note
    under ``note_card``, while the self-content endpoints (``user_posted`` /
    ``like`` / ``collect``) return the bare object with **no** ``note_card``
    key. The ``or item`` fallback below is what bridges them.

    Beware what the self-content shape does *not* carry: no ``desc``, no
    ``image_list``, no four-way ``interact_info``. Nothing here can invent them,
    so the UI renders only what exists (design doc §18).
    """
    card = item.get("note_card") or item.get("note") or item
    author = card.get("user") or card.get("author") or {}
    interact = card.get("interact_info") or card.get("interaction") or {}
    note_id = card.get("note_id") or card.get("id") or item.get("id") or ""
    timestamp = card.get("time") or card.get("create_time") or item.get("time") or item.get("create_time") or 0
    return {
        "note_id": note_id,
        "note_url": _note_url(str(note_id), item, card, default_xsec_source),
        "title": card.get("display_title") or card.get("title") or "",
        "content": card.get("desc") or card.get("content") or "",
        "author_id": author.get("user_id") or author.get("id") or "",
        "author_name": author.get("nickname") or author.get("name") or "",
        "author_avatar": author.get("avatar") or author.get("avatar_url") or "",
        "cover_url": _first_url(card.get("cover") or card.get("image_list") or card.get("images")),
        "image_urls": _all_urls(card.get("image_list") or card.get("images") or card.get("cover")),
        "likes": _metric(interact.get("liked_count") or interact.get("likes")),
        "collects": _metric(interact.get("collected_count") or interact.get("collects")),
        "comments": _metric(interact.get("comment_count") or interact.get("comments")),
        "shares": _metric(interact.get("share_count") or interact.get("shares")),
        "type": card.get("type") or item.get("model_type") or "",
        "timestamp": timestamp,
        # Best-effort goods extraction (may be None). See _extract_goods: this
        # never fabricates a value, the UI labels "not parsed" instead.
        "goods": _extract_goods(card),
        "raw": item,
    }


def _video_url(card: dict[str, Any]) -> str:
    for key in ("video_addr", "video_url"):
        if card.get(key):
            return str(card[key])
    video_info = card.get("video") if isinstance(card.get("video"), dict) else {}
    stream_info = video_info.get("media", {}).get("stream", {}) if isinstance(video_info, dict) else {}
    for codec in ("h264", "h265", "av1"):
        streams = stream_info.get(codec, []) if isinstance(stream_info, dict) else []
        if isinstance(streams, list) and streams:
            first_stream = streams[0] if isinstance(streams[0], dict) else {}
            if first_stream.get("master_url") or first_stream.get("url"):
                return str(first_stream.get("master_url") or first_stream.get("url"))
    consumer = video_info.get("consumer") if isinstance(video_info.get("consumer"), dict) else {}
    origin_key = consumer.get("origin_video_key")
    return f"https://sns-video-bd.xhscdn.com/{origin_key}" if origin_key else ""


def _normalize_detail_payload(raw_payload: dict[str, Any], source_url: str = "") -> dict[str, Any]:
    data = (raw_payload or {}).get("data") or {}
    items = data.get("items") or []
    item = items[0] if items and isinstance(items[0], dict) else (data if isinstance(data, dict) else {})
    normalized = _normalize_search_item(item)
    card = item.get("note_card") or item.get("note") or item
    images = _all_urls(card.get("image_list") or card.get("images") or card.get("cover"))
    if source_url:
        normalized["note_url"] = source_url
    normalized["image_urls"] = images
    video_url = _video_url(card)
    normalized["video_url"] = video_url
    normalized["video_addr"] = video_url
    normalized["tags"] = _tag_names(card.get("tag_list") or card.get("tags") or card.get("topics"))
    normalized["raw"] = raw_payload
    return normalized


# --------------------------------------------------------------------------
# Self content (my notes / liked / collected / interaction messages)
#
# Design source: docs/02_设计文档/12_功能设计_我的笔记与互动数据.md
# --------------------------------------------------------------------------

# The three platform endpoints expose their list under *different* keys:
#   user_posted  -> data.notes        (apis/xhs_pc_apis.py:339)
#   feed         -> data.items        (api/platforms/xhs/pc.py:265)
#   you/*        -> data.message_list (apis/xhs_pc_apis.py:927/:879/:975)
# so every reader must probe rather than assume a single shape.
_NOTE_LIST_KEYS: tuple[str, ...] = ("notes", "items")
_MESSAGE_LIST_KEYS: tuple[str, ...] = ("message_list", "items", "notifications")

# Candidate containers for the goods a note links to. The PC link's ``feed`` carries
# none of them (verified 2026-09-19); the only shape that has ever been observed to
# hold real data is the 千帆 goods-note entry (``goods_infos`` — see §19). This stays
# probe-and-degrade for the two links whose payload shape we do not control.
_GOODS_CONTAINER_KEYS: tuple[str, ...] = (
    "goods_info",
    "goods_infos",
    "goods",
    "note_goods",
    "goods_list",
    "shop_info",
    "product_info",
)

# ``xsec_source`` used when *calling* each list endpoint, mirrored from the
# SDK's own variants so single-page calls behave identically.
_LIST_XSEC_SOURCE = {
    "notes": "pc_search",
    "liked": "pc_user",
    "collected": "pc_search",
}

#: 「我的笔记」补价格时一次批量查最多带多少个商品 ID。
#: 去重后按此截断（一页笔记 10–50 条，够用）；千帆 `whole_keywords` 支持逗号分隔多 ID。
_GOODS_PRICE_BATCH_LIMIT = 50

# ``xsec_source`` used when building a note URL for the *detail* call. Kept
# separate from the list value above because the token is scene-bound: a
# ``user_posted`` token replayed with ``pc_search`` fails with "笔记不存在",
# while ``pc_user`` / ``pc_feed`` succeed (verified 2026-09-18, design doc §18.4).
_NOTE_XSEC_SOURCE = {
    "notes": "pc_user",
    "liked": "pc_user",
    "collected": "pc_user",
}

# Keys probed on a creator note-manager entry
# (``/api/galaxy/v2/creator/note/user/posted``).
#
# VERIFIED 2026-09-18 against a live account holding 1969 notes (843 of them
# video). The platform's real entry is flat:
#   {"id": "6a7d1e2f...", "display_title": "...", "time": "2026-08-13 09:30",
#    "likes": 0, "collected_count": 0, "comments_count": 0, "shared_count": 0,
#    "view_count": 2, "images_list": [{"url": "..."}], "type": "normal",
#    "goods_info": {"goods_name": []}, "xsec_token": "...", "sticky": false}
# The leading name of each tuple is therefore the real key; the trailing
# candidates are kept as cheap insurance against a shape drift. See design
# doc §17.3 / §18.
#
# NOTE: the creator list carries **no** body/desc field at all — ``content``
# stays "" by construction. Body text is only obtainable from the note detail
# call, which is a separate request per note (design doc §18.2).
_CREATOR_NOTE_ID_KEYS: tuple[str, ...] = ("id", "note_id", "noteId")
_CREATOR_NOTE_TITLE_KEYS: tuple[str, ...] = ("display_title", "title", "note_title", "name")
_CREATOR_NOTE_DESC_KEYS: tuple[str, ...] = ("desc", "description", "content", "note_desc")
_CREATOR_NOTE_IMAGE_LIST_KEYS: tuple[str, ...] = ("images_list", "image_list", "images", "cover")
_CREATOR_NOTE_TYPE_KEYS: tuple[str, ...] = ("type", "note_type", "noteType", "media_type")
_CREATOR_NOTE_TIME_KEYS: tuple[str, ...] = (
    "time",
    "post_time",
    "postTime",
    "create_time",
    "publish_time",
    "publishTime",
)


def _coalesce(source: dict[str, Any], *keys: str) -> Any:
    """First non-``None`` value among ``keys`` — ``0`` and ``""`` are values.

    ``dict.get(key) or fallback`` would silently drop a real zero, which is
    exactly the failure mode that made every creator metric render as 0 with no
    way to tell "no likes" from "wrong key" (design doc §18.2).
    """
    for key in keys:
        value = source.get(key)
        if value is not None:
            return value
    return None


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _first_scalar(*values: Any) -> str:
    """``_first_text`` that unwraps list containers first.

    The creator platform returns goods attributes as arrays
    (``{"goods_name": ["..."]}``). ``_first_text`` would stringify an *empty*
    array into the literal ``"[]"`` — truthy, and therefore indistinguishable
    from real data downstream (design doc §19.1).
    """
    flattened: list[Any] = []
    for value in values:
        if isinstance(value, list):
            flattened.extend(value)
        else:
            flattened.append(value)
    return _first_text(*flattened)


def _extract_list(data: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """Return the first list found under any of ``keys``.

    An empty list is returned when nothing matches — deliberately not an
    exception. An empty platform result and a changed response shape are told
    apart during triage by keeping the raw payload alongside the item count.
    """
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _items_from_payload(raw_payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    data = raw_payload.get("data") if isinstance(raw_payload, dict) else None
    if isinstance(data, (dict, list)):
        return _extract_list(data, keys)
    return _extract_list(raw_payload, keys)


def _pagination_from_payload(raw_payload: Any) -> tuple[str, bool]:
    """Read ``cursor`` / ``has_more`` out of a platform payload.

    ``cursor`` is opaque: it is never parsed, never regenerated locally, and
    always passed back verbatim by the client.
    """
    data = raw_payload.get("data") if isinstance(raw_payload, dict) else None
    if not isinstance(data, dict):
        return "", False
    cursor = data.get("cursor")
    return (_first_text(cursor), bool(data.get("has_more", False)))


def _extract_goods(card: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Best-effort extraction of the goods a note links to.

    Returns ``None`` when nothing recognisable is present. That is intentional:
    a fabricated placeholder here would be indistinguishable from real data
    downstream, and the UI distinguishes "no goods on this note" from "goods
    exist but we cannot read them" at the *page* level instead (design doc §19.3).

    Two shapes are handled: the flat one the creator note-manager returns
    (``{"goods_info": {"goods_name": ["..."], "goods_id": [...]}}`` — array
    valued, hence ``_first_scalar``) and the nested PC ``feed`` shape that is
    still uncaptured (DEF-006). This function remains the single place to touch
    when that payload arrives.
    """
    container: Any = None
    for key in _GOODS_CONTAINER_KEYS:
        value = card.get(key)
        if isinstance(value, dict) and value:
            container = value
            break
        if isinstance(value, list) and value and isinstance(value[0], dict):
            container = value[0]
            break
    if not isinstance(container, dict):
        return None

    goods_id = _first_scalar(
        container.get("goods_id"), container.get("item_id"), container.get("id")
    )
    title = _first_scalar(
        container.get("title"),
        container.get("name"),
        container.get("goods_name"),
        container.get("goods_title"),
        # 千帆商品笔记条目的真实字段名（§19 实测）。
        container.get("item_title"),
    )
    # Require at least one identifying field; a container of unrelated keys is
    # not a goods object. An empty ``goods_name: []`` therefore yields ``None``,
    # not a phantom goods entry titled "[]".
    if not goods_id and not title:
        return None

    return {
        "goods_id": goods_id,
        "title": title,
        "price": _first_scalar(
            container.get("price"),
            container.get("price_text"),
            container.get("min_price"),
            container.get("display_price"),
        ),
        "image": _first_url(
            container.get("image")
            or container.get("cover")
            or container.get("pic")
            or container.get("images")
            or container.get("item_image")
        ),
        "url": _first_scalar(
            container.get("url"),
            container.get("link"),
            container.get("scheme"),
            container.get("jump_url"),
            container.get("item_url"),
        ),
        "shop_name": _first_scalar(
            container.get("shop_name"),
            container.get("seller_name"),
            container.get("merchant_name"),
        ),
        "variant": _first_scalar(container.get("variant_desc"), container.get("variant")),
    }


def _normalize_identity(data: Any) -> dict[str, Any]:
    """Normalise ``/v2/user/me`` into the fields the UI shows.

    Shape not yet captured, so user_id / nickname / avatar each probe a few
    candidate paths. Only for display: the authoritative user_id may also come
    from a pasted profile URL (see _resolve_identity).
    """
    payload = data if isinstance(data, dict) else {}
    basic = payload.get("basic_info") if isinstance(payload.get("basic_info"), dict) else {}
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    return {
        "user_id": _first_text(
            payload.get("user_id"), payload.get("userId"), basic.get("user_id"), user.get("user_id"), user.get("id")
        ),
        "nickname": _first_text(
            payload.get("nickname"), payload.get("nick_name"), basic.get("nickname"), user.get("nickname"), user.get("name")
        ),
        "avatar": _first_url(
            payload.get("avatar") or payload.get("imageb") or basic.get("image") or user.get("avatar")
        ),
        "red_id": _first_text(payload.get("red_id"), basic.get("red_id"), user.get("red_id")),
        "ip_location": _first_text(payload.get("ip_location"), basic.get("ip_location"), user.get("ip_location")),
    }


def _normalize_interaction_message(item: dict[str, Any], kind: str) -> dict[str, Any]:
    """Normalise one entry of ``data.message_list``.

    The three kinds (like_collect / mention / connection) share a list key but
    differ in detail, so every field probes candidates and the untouched entry
    stays available under ``raw``.
    """
    user = _comment_user(item)
    if not user and isinstance(item.get("user_info"), dict):
        user = item["user_info"]
    card = item.get("note_card") if isinstance(item.get("note_card"), dict) else {}
    if not card and isinstance(item.get("note"), dict):
        card = item["note"]
    return {
        "kind": kind,
        "message_id": _first_text(item.get("id"), item.get("message_id"), item.get("notice_id")),
        "title": _first_text(item.get("title"), card.get("display_title"), card.get("title")),
        "content": _first_text(item.get("content"), item.get("desc"), item.get("text")),
        "user_name": _first_text(user.get("nickname"), user.get("name"), item.get("user_name"), item.get("nickname")),
        "user_avatar": _first_url(user.get("avatar") or user.get("image") or user.get("imageb")),
        "note_id": _first_text(item.get("note_id"), card.get("note_id"), card.get("id")),
        "note_url": _first_text(item.get("note_url"), card.get("note_url")),
        "cover_url": _first_url(card.get("cover") or card.get("image_list") or item.get("image")),
        "time": item.get("time") or item.get("create_time") or item.get("timestamp") or 0,
        "raw": item,
    }


def _parse_profile_url(value: str) -> tuple[str, str, str]:
    """Split a pasted profile URL (or bare user id) into (user_id, token, source).

    Accepts ``https://www.xiaohongshu.com/user/profile/<id>?xsec_token=..&xsec_source=..``
    as well as a bare ``<id>``. This URL is the *only* source that carries
    user_id and xsec_token together, which is why it is the primary path.
    """
    text = (value or "").strip()
    if not text:
        return "", "", ""
    if not text.startswith("http"):
        return text, "", ""
    parsed = urllib.parse.urlparse(text)
    query = urllib.parse.parse_qs(parsed.query)
    user_id = parsed.path.rstrip("/").split("/")[-1] if parsed.path else ""
    token = (query.get("xsec_token") or [""])[0]
    source = (query.get("xsec_source") or [""])[0]
    return user_id, token, source


class _IdentityResolution(NamedTuple):
    user_id: str
    xsec_token: str
    xsec_source: str
    token_source: str  # "url" | "auto"
    identity: Optional[dict[str, Any]]
    self_message: str


def _resolve_identity(
    adapter: Any,
    profile_url: str = "",
    xsec_token: str = "",
    xsec_source: str = "",
    default_source: str = "",
) -> _IdentityResolution:
    """Resolve the acting account's identity, preferring a pasted profile URL.

    Three levels (design doc §7):
      L1  profile_url present -> authoritative user_id + token, zero extra calls
      L2  otherwise           -> /v2/user/me for user_id, then try with no token
      L3  L2 unusable         -> caller raises a structured 400 telling the user
                                 to paste their profile URL
    """
    url_user_id, url_token, url_source = _parse_profile_url(profile_url)
    token = xsec_token or url_token
    source = xsec_source or url_source or default_source
    if url_user_id:
        return _IdentityResolution(url_user_id, token, source, "url", None, "")

    success, message, payload = adapter.get_self_profile()
    if not success:
        return _IdentityResolution("", token, source, "auto", None, message or "")
    data = payload.get("data") if isinstance(payload, dict) else None
    identity = _normalize_identity(data)
    return _IdentityResolution(identity["user_id"], token, source, "auto", identity, "")


def _has_web_session(cookies: str) -> bool:
    """True when this cookie jar can authenticate PC (www) endpoints.

    **能力判定只有一份实现**：`services.account_service.has_web_session`。
    本函数只是本模块的历史别名，保留它是为了让既有调用点与守卫测试不必改动。
    """
    return has_web_session(cookies)


def _web_session_required_detail() -> dict[str, str]:
    """拿不到网页会话时的**结构化**原因（前端按 `code` 走安静降级，不弹 toast）。

    只有一种情况：**这个账号自己没有可用的网页会话**。所以文案也只有一句 ——
    用户维度与笔记维度说的是同一件事，不留第二份真相源。

    ⚠️ v1.40（2026-09-24 用户口径：「错误内容太长了，只需提示登录创作者平台或
    pc 端平台」）：原文案约 100 字，且带一对**永远不会被渲染**的 `**` 标记
    （antd 的 Tooltip / Alert 不解析 markdown，用户看到的是字面星号）。现收敛为
    一句登录指引。**被取代的原文**（保留备查，不静默删除）：
    「该账号的 Cookie 中不含可用的网页会话（web_session）—— 网页版的笔记正文、
    评论、我赞过的 / 我收藏的 / 互动消息都只能由它读取。请用**该账号本人**在
    网页版（xiaohongshu.com）登录后重新导入 Cookie。」

    ⚠️ 新文案里的「创作者平台」是**登录入口指引**（与 `_self_content_capabilities`
    的 `notes_reason`「请在网页版或创作者平台登录同一个账号后重试」同一口径），
    **不是**能力归因 —— 别看到这个词就当回归信号删掉。要防的回归仍是 v1.28 /
    DEF-058 那条：把「凭据缺失」说成「平台没有这个能力」。真机抓包（2026-09-21，
    三轮）证明创作者账号走 www 的 `sns/web/v1/note/like/page` /
    `v2/note/collect/page` / `v1/you/*` **全部可达**，唯一障碍是**凭据** ——
    错的归因会让用户永远不去重登。故判据禁**句式**（「不提供」/「不返回」），
    不禁平台名；被推翻的原文句式另有全仓 AST 扫描守着
    （`test_login_state.py::test_disproven_phrase_is_nowhere_in_the_backend`）。
    """
    return {
        "code": "WEB_SESSION_REQUIRED",
        "message": (
            "该账号的 Cookie 中不含可用的网页会话，"
            "请登录创作者平台或 PC 端平台后重新导入 Cookie。"
        ),
    }


def _account_cookies(
    db: Session,
    current_user: User,
    account_id: int,
    *,
    channel: str = CHANNEL_WWW,
) -> tuple[PlatformAccount, str]:
    """Ownership check, then the newest cookie version **of the given platform**.

    Says nothing about ``sub_type`` on purpose: that field describes the
    protocol link (search vs. publish), not which read links the jar can
    actually authenticate. Capability is decided separately by
    :func:`_has_web_session`, so a creator account that later gains a www
    session starts working without a code change.

    ``channel``（§11.5 / 2026-09-21 拆分）决定取哪个**平台**的令牌：**PC / www
    接口取 ``www``，创作者平台取 ``creator``，千帆取 ``ark``**。同一账号下三个
    平台的令牌并存、互不覆盖，所以必须显式选平台，不能再按「最新一版」猜 ——
    猜错的后果是拿创作者 jar 去打千帆接口，平台只会回一句「缺失登录凭证」。
    """
    #: 归属校验走 **唯一实现**（DEF-055）：这里原先是自己抄的一份（4 份拷贝之一），
    #: **漏了软删闸** —— 账号被删掉之后，旧页面留下的 id 仍能取到它的凭据去打平台。
    account = get_owned_active_account(db, current_user, account_id, platform="xhs")

    cookies = latest_cookies_for_channel(db, account.id, channel)
    if not cookies:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Account has no cookies")
    return account, cookies


def _account_cookies_auto(
    db: Session,
    current_user: User,
    account_id: int,
) -> tuple[PlatformAccount, str]:
    """自内容入口按「www 优先、creator 兜底、ark 兜底」取这个账号的一条 jar（§11.5）。

    这些端点**先按能力分流**（有 www 会话走网页版，否则走创作者平台），所以这里
    不该预设平台，而是把账号手里的 jar 按优先级拿出来，让下面的
    `_has_web_session(cookies)` 去决定走哪条链路。反之，PC 专用端点
    （detail / comments / search）仍**只取 www 平台** —— 那些接口只有网页版提供。

    ⚠️ 2026-09-21 拆分后顺序是 `www → creator → ark`：千帆那份 jar 是**超集**
    （同时带 creator 域令牌，真机实测可打创作者平台），所以它排在 creator 之后
    仍能兜住「只导入过千帆完整 Cookie」的账号。
    """
    last_error: HTTPException | None = None
    for channel in (CHANNEL_WWW, CHANNEL_CREATOR, CHANNEL_ARK):
        try:
            return _account_cookies(db, current_user, account_id, channel=channel)
        except HTTPException as exc:  # noqa: PERF203 - 三个平台都空时才抛
            last_error = exc
    raise last_error  # type: ignore[misc]


def _get_owned_pc_account_cookies(
    db: Session,
    current_user: User,
    account_id: int,
    *,
    channel: Optional[str] = None,
) -> str:
    """该账号**自己的**网页会话（www 通道）—— 取不到就 400，没有第二条路。

    **每个账号只使用自己账号对应的平台有效登录令牌**：自有 jar 拿不到就结构化
    失败，原因直接告诉用户登录哪个账号。绝不拿别的账号的会话去读别人的数据面。

    ``channel="auto"`` 供内部入口用（www 优先、ark 兜底）；PC 专用端点不传，
    只认 www 通道 —— detail / comments / search 只有网页版提供。
    """
    # §11.5：`channel="auto"` 供内部入口用（www 优先、ark 兜底）；
    # PC 专用端点不传，只认 www 通道。
    #
    # ⚠️ **自有 jar 是唯一来源**：拿不到就 400。
    own_jar = ""
    try:
        if channel == "auto":
            account, own_jar = _account_cookies_auto(db, current_user, account_id)
        else:
            account, own_jar = _account_cookies(
                db, current_user, account_id, channel=channel or CHANNEL_WWW
            )
    except HTTPException as exc:
        # 只吞「这个账号没存这条通道的令牌」；**归属类错误必须照旧抛出去**
        # （404 异租户 / 已软删，DEF-055）—— 兜底不能变成越权。
        if exc.status_code != status.HTTP_400_BAD_REQUEST:
            raise
        own_jar = ""
    if own_jar and _has_web_session(own_jar):
        return own_jar

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=_web_session_required_detail(),
    )


def _note_shop_detail(
    db: Session,
    account: PlatformAccount,
    note_id: str,
    qianfan_adapter_factory: Any = None,
) -> dict[str, Any]:
    """单篇笔记的千帆数据（v1.18）：一次只读请求 + 本地映射互补。

    返回 ``{bound, available, goods[], metrics{}, source}``；**任何失败都
    不抛**——千帆缺，详情正文仍在。metrics 一律留空不造（缺值口径）。
    """
    info: dict[str, Any] = {
        "bound": False,
        "available": False,
        "goods": [],
        "metrics": {},
        "source": "",
        "error": "",
    }
    # ADR-021：店铺实体已退场 ⇒「能力」= 本账号有可用的 ark 会话；展示名取账号昵称。
    if not has_ark_session(db, account.id):
        return info
    info["bound"] = True
    info["source"] = (account.nickname or "").strip()

    # 先读本地映射（零请求）；没有再按需补一次（一次只读请求）。
    rows = list(
        db.scalars(
            select(ShopNoteGoods).where(
                ShopNoteGoods.platform_account_id == account.id,
                ShopNoteGoods.note_id == note_id,
            )
        ).all()
    )
    if not rows and qianfan_adapter_factory is not None:
        outcome = sync_note_ids(db, account, [note_id], qianfan_adapter_factory)
        if outcome.get("credential_invalid"):
            info["error"] = outcome.get("error") or "千帆登录态失效"
        rows = list(
            db.scalars(
                select(ShopNoteGoods).where(
                    ShopNoteGoods.platform_account_id == account.id,
                    ShopNoteGoods.note_id == note_id,
                )
            ).all()
        )
    if not rows:
        return info

    info["available"] = True
    for row in rows:
        goods = json.loads(row.goods_json) if row.goods_json else []
        if not isinstance(goods, list):
            goods = [goods] if goods else []
        for g in goods:
            if isinstance(g, dict):
                info["goods"].append(
                    {
                        "item_id": g.get("item_id") or row.item_id or "",
                        "item_title": g.get("item_title") or row.item_title or "",
                        "item_image": g.get("item_image") or row.item_image or "",
                        "variant_desc": g.get("variant_desc") or row.variant_desc or "",
                        "price": _price_text(g.get("min_price"), g.get("max_price")),
                    }
                )
    # 千帆 entry_json 里的运营数据（30 天浏览 / GMV / 订单），平铺到 shop 顶层
    # —— 前端 `noteForDetail.shop.<field>` 直接读（XhsNoteShopMetrics 形状）。
    # 缺值留 None，不造 0（§18.2 缺值口径）。
    entry = json.loads(rows[0].entry_json) if rows[0].entry_json else {}
    if isinstance(entry, dict):
        info.update(
            {
                "note_id": note_id,
                "title": entry.get("note_title") or "",
                "cover": entry.get("note_image") or "",
                "note_flag": entry.get("note_flag") or "",
                "browse_num": entry.get("browse_num"),
                "browse_num_30d": entry.get("note_browse_num30d"),
                "likes": entry.get("like_num"),
                "collects": entry.get("collect_num"),
                "comments": entry.get("comment_num"),
                "shares": entry.get("share_num"),
                "pay_gmv": entry.get("note_pay_gmv"),
                "pay_order_cnt": entry.get("note_pay_order_cnt"),
                "status": entry.get("note_status"),
                "note_type": entry.get("note_type"),
                "publish_time": entry.get("publish_time"),
                "xsec_token": entry.get("xsec_token") or "",
            }
        )
    return info


def _note_id_from_url(url: str) -> str:
    """The ``note_id`` segment of an explore/discovery URL ("" if absent)."""
    if not url:
        return ""
    for segment in url.split("/"):
        candidate = segment.split("?")[0].strip()
        if candidate and all(c in "0123456789abcdefABCDEF" for c in candidate) and len(candidate) >= 16:
            return candidate
    return ""


@router.post("/search/notes")
def search_notes(
    payload: SearchNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
):
    # 关键词检索只认**这个账号自己的**网页会话（2026-09-22 用户拍板：每个账号
    # 只使用自己账号对应的平台有效登录令牌）：它同时跨「我发布的 / 我赞过的 /
    # 我收藏的」三份列表，用任何非本人的会话都是把别人的收藏当自己的检索范围。
    # 与 `user_search`（`/users/search`）保持一致，也与前端门控一致
    # （`searchAvailable = has_web_session && tab != interactions`）—— 没有自有会话时
    # 前端本来就不给搜索入口，所以这里返回 400 不会造成可见的功能缺失。
    cookies = _get_owned_pc_account_cookies(
        db, current_user, payload.account_id
    )
    success, message, raw_payload = adapter_factory(cookies).search_note(
        payload.keyword,
        page=payload.page,
        sort_type_choice=payload.sort_type_choice,
        note_type=payload.note_type,
        note_time=payload.note_time,
        note_range=payload.note_range,
        pos_distance=payload.pos_distance,
        geo=payload.geo,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=message or "XHS note search failed",
        )

    data = (raw_payload or {}).get("data") or {}
    items = data.get("items") or []
    normalized_items = [
        _normalize_search_item(item)
        for item in items
        if isinstance(item, dict) and item.get("model_type") not in ("rec_query", "hot_query")
    ]
    return {
        "total": len(normalized_items),
        "page": payload.page,
        "page_size": data.get("page_size") or 20,
        "has_more": bool(data.get("has_more", False)),
        "items": normalized_items,
        "raw": raw_payload,
    }


class SearchUsersRequest(BaseModel):
    """搜索用户请求（T-107）。**必须带账号 id** —— 检索范围只认该账号自己的会话。"""

    model_config = ConfigDict(extra="forbid")

    account_id: int
    keyword: str = Field(min_length=1, max_length=120)
    page: int = Field(default=1, ge=1)


@router.post("/search/users")
def search_users(
    payload: SearchUsersRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
):
    """搜索用户（**真实现**，T-107 取代原 mock）。

    ⚠️ 检索范围只认**这个账号自己的**网页会话（2026-09-22 用户拍板：每个账号
    只使用自己账号对应的平台有效登录令牌）—— 与 `/search/notes` 同一口径。
    """
    cookies = _get_owned_pc_account_cookies(db, current_user, payload.account_id)
    success, message, raw_payload = adapter_factory(cookies).search_user(
        payload.keyword, page=payload.page
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=message or "XHS user search failed",
        )
    data = (raw_payload or {}).get("data") or {}
    items = data.get("users") or data.get("items") or []
    normalized = [_normalize_user_item(item) for item in items if isinstance(item, dict)]
    return {
        "total": data.get("total") or len(normalized),
        "page": payload.page,
        "has_more": bool(data.get("has_more", False)),
        "items": normalized,
        "raw": raw_payload,
    }


def _normalize_user_item(item: dict[str, Any]) -> dict[str, Any]:
    """搜索结果的**用户条目**归一化（缺值留空，不造；硬约束⑤）。"""
    user = item.get("user") if isinstance(item.get("user"), dict) else item
    return {
        "user_id": _first_text(user.get("user_id"), user.get("id")),
        "nickname": _first_text(user.get("nickname"), user.get("name")),
        "red_id": _first_text(user.get("red_id")),
        "avatar_url": _first_text(user.get("image"), user.get("images"), user.get("avatar")),
        "desc": _first_text(user.get("desc")),
        "fans": user.get("fans") if isinstance(user.get("fans"), int) else None,
        "note_count": user.get("note_num") if isinstance(user.get("note_num"), int) else None,
        "xsec_token": _first_text(item.get("xsec_token")),
    }


@router.post("/notes/detail")
def note_detail(
    payload: NoteDetailRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
    qianfan_adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """单篇笔记的**全部数据**（v1.18：三源合并）。

    | 来源 | 给什么 |
    |---|---|
    | www feed（**该账号自己的** www 会话） | 正文 / 标题 / 话题 / 作者声明 / 高清图 / 视频 |
    | 千帆商品笔记（绑定了店铺时，按 note_id 一次只读查询） | 挂载商品 / 30 天浏览 / 支付 GMV / 订单数 |

    千帆失败不阻断详情（商品缺，正文仍在），只把失败原因带回去。
    """
    # 只取这个账号**自己的**网页会话（2026-09-22 用户拍板：每个账号只使用
    # 自己账号对应的平台有效登录令牌）；拿不到就 400 + 结构化原因。
    cookies = _get_owned_pc_account_cookies(
        db,
        current_user,
        payload.account_id,
    )
    success, message, raw_payload = adapter_factory(cookies).get_note_info(payload.url)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=message or "XHS note detail failed",
        )
    normalized = _normalize_detail_payload(raw_payload or {}, source_url=payload.url)
    note_id = _note_id_from_url(payload.url) or str(normalized.get("note_id") or "")
    if note_id:
        account = db.get(PlatformAccount, payload.account_id)
        if account is not None:
            shop = _note_shop_detail(db, account, note_id, qianfan_adapter_factory)
            normalized["shop"] = shop
            # 详情与列表必须给**同一形状**的挂载商品（DEF-047）：列表项由
            # `_attach_shop_goods` 带上 `NoteGoods`，而详情走的是 www feed，
            # 那个 payload **结构上没有商品容器**（§19.1）→ `goods` 恒为 `None`。
            # 前端一变详情就把列表那份覆盖成 null，用户看到商品信息整块消失。
            # 这里按首条回填（多商品由前端的 `mountGoods` 用 `shop.goods` 补齐）。
            if not normalized.get("goods") and shop.get("goods"):
                first = shop["goods"][0]
                normalized["goods"] = _note_goods_from_parts(
                    str(first.get("item_id") or ""),
                    str(first.get("item_title") or ""),
                    str(first.get("item_image") or ""),
                    str(first.get("variant_desc") or ""),
                    str(first.get("price") or ""),
                    str(shop.get("source") or ""),
                )
    return normalized


@router.post("/notes/comments")
def note_comments(
    payload: NoteCommentsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
):
    # 只取这个账号**自己的**网页会话（2026-09-22 用户拍板：每个账号只使用
    # 自己账号对应的平台有效登录令牌）；拿不到就 400 + 结构化原因。
    cookies = _get_owned_pc_account_cookies(
        db,
        current_user,
        payload.account_id,
    )
    success, message, raw_payload = adapter_factory(cookies).get_note_comments(payload.note_url)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=message or "XHS note comments failed",
        )
    items = normalize_comment_payload(raw_payload)
    return {"total": len(items), "items": items}


class SelfProfileRequest(BaseModel):
    account_id: int


class SelfContentRequest(BaseModel):
    account_id: int
    cursor: str = Field(default="", max_length=512)
    # Pasted profile URL is the primary token source (see _resolve_identity).
    profile_url: str = Field(default="", max_length=2048)
    xsec_token: str = Field(default="", max_length=512)
    xsec_source: str = Field(default="", max_length=64)


class InteractionMessagesRequest(BaseModel):
    account_id: int
    kind: Literal["like_collect", "mention", "connection"]
    cursor: str = Field(default="", max_length=512)


class TrafficThresholdsRequest(BaseModel):
    """无流量判定阈值（**全部可配，不写死 0**）：三项全部 <= 阈值才算无流量。"""

    max_browse: int = Field(default=0, ge=0, le=100000, description="阅读量上限")
    max_pay_order: int = Field(default=0, ge=0, le=100000, description="支付订单数上限")
    max_pay_gmv: float = Field(default=0.0, ge=0.0, le=10000000.0, description="支付金额上限")


class CleanupQueryRequest(BaseModel):
    """清理的搜索条件（可选）：与阈值共同决定候选池。"""

    #: 标题关键词；空 = 不限。走千帆服务端搜索（千帆笔记管理页同款接口）。
    keyword: str = Field(default="", max_length=60)
    #: 笔记类型："" 不限 / "normal" 图文 / "video" 视频。
    note_type: str = Field(default="", max_length=16)
    #: 只清理发布时间早于该时间戳（秒）的笔记；None = 不限。
    published_before: Optional[int] = Field(default=None, ge=0)


class NoTrafficCandidatesRequest(BaseModel):
    """无流量候选预览。无 keyword 时纯本地查询（零平台请求）。"""

    account_id: int
    thresholds: TrafficThresholdsRequest = Field(default_factory=TrafficThresholdsRequest)
    query: CleanupQueryRequest = Field(default_factory=CleanupQueryRequest)
    #: **每页条数**（预览用，默认 50）。分页见 `page`。
    limit: int = Field(default=50, ge=1, le=500)
    #: 第几页（从 1 起；越界回落成最后一页，见服务层 docstring）。
    page: int = Field(default=1, ge=1)


class NoTrafficCleanupRequest(BaseModel):
    """开始无流量清理（后台异步）。"""

    account_id: int
    thresholds: TrafficThresholdsRequest = Field(default_factory=TrafficThresholdsRequest)
    query: CleanupQueryRequest = Field(default_factory=CleanupQueryRequest)
    #: 单批删除上限（后端还会再夹一次硬上限 DEFAULT_DELETE_BATCH）。
    batch_limit: int = Field(default=20, ge=1, le=50)
    #: **指定笔记清理**（列表勾选）：非空 = 只删这些（且必须仍在候选池内）；
    #: 空 = 按阈值 + 查询条件全量清（上限见 `DEFAULT_DELETE_BATCH`）。
    note_ids: list[str] = Field(default_factory=list, max_length=500)


class SearchSelfContentRequest(BaseModel):
    """在自有内容（我发布的 / 我赞过的 / 我收藏的）内做关键词检索。"""

    account_id: int
    keyword: str = Field(min_length=1, max_length=60)
    #: 每个分类最多扫描多少页。**硬上限 5**：请求量必须是有界的（设计文档 §22）。
    pages_per_scope: int = Field(default=3, ge=1, le=5)
    #: 「继续搜索更早内容」时回传上一轮的 cursors（按 scope 键）。
    cursors: dict[str, str] = Field(default_factory=dict)
    # Pasted profile URL is the primary token source (see _resolve_identity).
    profile_url: str = Field(default="", max_length=2048)
    xsec_token: str = Field(default="", max_length=512)
    xsec_source: str = Field(default="", max_length=64)


def get_xhs_creator_api_adapter_factory():
    """Indirection local to this module so importing the creator router is not needed."""
    from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter

    return XhsCreatorApiAdapter


def _normalize_creator_note(item: dict[str, Any]) -> dict[str, Any]:
    """Normalise one creator note-manager entry into the shared note contract.

    Mirrors ``_normalize_search_item`` closely enough that the frontend needs no
    second render path, but the source shape is the creator one (flat metrics,
    ``images_list``) — see the constants above for the captured sample.

    Two honest gaps, both deliberate:

    * ``content`` stays "" — the creator list has no body field. It is filled in
      only when the detail call runs, on demand (design doc §18.2).
    * ``goods`` comes from ``goods_info``, which is ``{"goods_name": []}`` on
      every sampled note. Non-empty values are surfaced when present, but the
      empty shape is not treated as a parse failure (design doc §19).

    ``raw`` is kept so a wrong guess costs only a re-read, never a re-fetch.
    """
    interact = item.get("interact_info") if isinstance(item.get("interact_info"), dict) else {}
    note_id = _first_text(*(item.get(key) for key in _CREATOR_NOTE_ID_KEYS))

    image_source = [item.get(key) for key in _CREATOR_NOTE_IMAGE_LIST_KEYS]
    image_urls = _all_urls(image_source)

    likes = _coalesce(item, "likes", "like_count", "liked_count")
    if likes is None:
        likes = interact.get("liked_count")
    collects = _coalesce(item, "collected_count", "collect_count", "collected")
    if collects is None:
        collects = interact.get("collected_count")
    comments = _coalesce(item, "comments_count", "comment_count")
    if comments is None:
        comments = interact.get("comment_count")
    shares = _coalesce(item, "shared_count", "share_count")
    if shares is None:
        shares = interact.get("share_count")

    xsec_token = _first_text(item.get("xsec_token"))
    xsec_source = _first_text(item.get("xsec_source"))
    return {
        "note_id": note_id,
        # The explore URL carries the list's own ``xsec_token``. Without it the
        # link opens the note without a valid scene token and the page rejects
        # it; with it the URL is the same one the note manager itself builds.
        # ``xsec_source`` is optional — the platform omits it on some entries,
        # and an explore URL without it still resolves.
        "note_url": _first_text(item.get("note_url"), item.get("url"), item.get("share_url"))
        or (
            f"https://www.xiaohongshu.com/explore/{note_id}"
            + (f"?xsec_token={xsec_token}" if note_id and xsec_token else "")
            + (f"&xsec_source={xsec_source}" if note_id and xsec_token and xsec_source else "")
            if note_id
            else ""
        ),
        "title": _first_text(*(item.get(key) for key in _CREATOR_NOTE_TITLE_KEYS)),
        "content": _first_text(*(item.get(key) for key in _CREATOR_NOTE_DESC_KEYS)),
        # The creator list is self-scoped, so there is no separate author block.
        "author_id": "",
        "author_name": "",
        "author_avatar": "",
        "cover_url": image_urls[0] if image_urls else "",
        "image_urls": image_urls,
        "likes": _metric(likes),
        "collects": _metric(collects),
        "comments": _metric(comments),
        "shares": _metric(shares),
        "views": _metric(item.get("view_count")),
        # Type is left verbatim: the sampled value is the string "normal" for
        # image notes, and the UI already falls back to 图文 when it cannot see
        # "video" (video notes are 843 of 1969 on the sampled account).
        "type": _first_text(*(item.get(key) for key in _CREATOR_NOTE_TYPE_KEYS)),
        "timestamp": _first_text(*(item.get(key) for key in _CREATOR_NOTE_TIME_KEYS)),
        # Carried through because the detail call needs it; never rendered.
        "xsec_token": _first_text(item.get("xsec_token")),
        "goods": _extract_goods(item),
        "raw": item,
    }


def _creator_pagination(raw_payload: Any) -> tuple[str, bool]:
    """Creator note-manager paging.

    ``data.page`` is the *next* cursor and ``-1`` terminates. It is reused
    verbatim as our opaque cursor, matching the "never regenerate a cursor
    locally" rule in §8.1.
    """
    data = raw_payload.get("data") if isinstance(raw_payload, dict) else None
    if not isinstance(data, dict):
        return "", False
    try:
        next_page = int(data.get("page"))
    except (TypeError, ValueError):
        return "", False
    return ("", False) if next_page < 0 else (str(next_page), True)


def _creator_total(raw_payload: Any, fallback: int) -> int:
    """Total note count for the active creator tab.

    The payload carries a ``tags`` array (``[{"name": "所有笔记",
    "notes_count": 1969, "checked": true}, {"name": "视频", "notes_count": 843}]``),
    which is the only place the true total appears — counting a single page
    would report "10 of 10" on a 1969-note account. Falls back to the page size
    when the field is absent, so a shape drift degrades to imprecise, not wrong.
    """
    data = raw_payload.get("data") if isinstance(raw_payload, dict) else None
    tags = data.get("tags") if isinstance(data, dict) else None
    if not isinstance(tags, list):
        return fallback
    for tag in tags:
        if isinstance(tag, dict) and tag.get("checked"):
            count = _metric(tag.get("notes_count"))
            if count:
                return count
    return fallback


def _load_creator_posted_page(
    cookies: str, cursor: str, creator_adapter_factory: Any
) -> dict[str, Any]:
    """One page of the account's own published notes, via the creator platform.

    Uses ``get_posted_notes_page`` — the natively paged SDK method — and never
    ``get_all_posted_notes``, which loops until the server cursor runs out
    (same rate-limit shape as DEF-033, design doc §17.3).
    """
    try:
        page = int(cursor) if str(cursor).strip() else 0
    except (TypeError, ValueError):
        page = 0

    adapter = creator_adapter_factory(cookies)
    success, message, raw_payload = adapter.get_posted_notes_page(page=page, tab=0)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=message or "创作者平台笔记列表获取失败",
        )

    items = [
        _normalize_creator_note(item)
        for item in _items_from_payload(raw_payload, _NOTE_LIST_KEYS)
    ]
    cursor_out, has_more = _creator_pagination(raw_payload)
    return {
        "items": items,
        "cursor": cursor_out,
        "has_more": has_more,
        "total": _creator_total(raw_payload, len(items)),
        "scope": "notes",
        # No www user_id exists for this link; the creator endpoint is
        # self-scoped by cookie, so identity resolution is not a precondition.
        "user_id": "",
        "token_source": "creator",
        "identity": None,
    }


def _creator_identity(cookies: str, creator_adapter_factory: Any) -> dict[str, Any]:
    """Creator-platform identity, rebuilt from a fixed whitelist.

    ``/api/galaxy/user/info`` returns ``phone`` alongside the display fields
    (verified 2026-09-18), so the raw platform payload is **never** spread into
    the response. Only the listed keys survive. See design doc §10 / §17.5.
    """
    success, message, raw_payload = creator_adapter_factory(cookies).get_creator_profile()
    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=message or "创作者身份获取失败",
        )
    data = raw_payload.get("data") if isinstance(raw_payload, dict) else None
    data = data if isinstance(data, dict) else {}

    identity = {
        "user_id": _first_text(data.get("userId"), data.get("user_id")),
        "nickname": _first_text(data.get("userName"), data.get("user_name")),
        "avatar": _first_url(data.get("userAvatar") or data.get("user_avatar")),
        "red_id": _first_text(data.get("redId"), data.get("red_id")),
        # The creator profile exposes no location field; "" is the honest value.
        "ip_location": _first_text(data.get("ipLocation"), data.get("ip_location")),
    }
    if not identity["user_id"]:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="平台响应中未包含 userId，无法识别创作者账号身份",
        )
    return {
        **identity,
        "link": "creator",
        "raw": {
            key: data.get(key)
            for key in ("userId", "userName", "userAvatar", "redId", "userDesc", "role")
        },
    }


def _state_time_text(value: Any) -> str:
    """`shop_state` 里的 ISO 时间 → 展示用 `YYYY-MM-DD HH:MM`；空 / 坏值 → 空串。"""
    parsed = parse_state_time(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed is not None else ""


def _shop_goods_source(db: Session, account: PlatformAccount) -> dict[str, Any]:
    """千帆商品映射的本地状态（**零平台请求**）。

    读的是账号的店铺关联 + `shop_state` 同步计数，供两处消费：

    * `/users/capabilities` 的 `goods` 能力声明（机读）；
    * `/users/notes` 的 `goods_source` 元信息 —— 把「同步了多少 / 店铺共多少」讲清楚，
      否则「没同步到」会被误读成「这条笔记没挂商品」。

    ⚠️ `shop_account_count` 已于 2026-09-22 移除：用户拍板店铺与账号 **1:1**
    （`platform_accounts.shop_id_fk` UNIQUE），「同一店铺被几个账号绑着」不再是
    一个事实 —— 恒为 0 / 1 的计数字段留着只会让人以为还有多账号场景。
    2026-09-30 ADR-021：店铺实体整体退场，`bound` 改由**本账号 ark 会话**回答
    （不再是「有没有绑店」），`shop_name` 取账号昵称。
    """
    if not has_ark_session(db, account.id):
        return {
            "source": "ark",
            "bound": False,
            "shop_name": "",
            "status": "",
            "status_message": "",
            "shop_total": 0,
            "synced_notes": 0,
            "synced_at": "",
            "health_checked_at": "",
        }
    state = get_shop_state(account)
    return {
        "source": "ark",
        "bound": True,
        "shop_name": (account.nickname or "").strip(),
        "status": str(state["status"]),
        "status_message": str(state["status_message"]),
        "shop_total": int(state["goods_note_total"] or 0),
        "synced_notes": synced_note_count(db, account.id),
        "synced_at": _state_time_text(state["goods_synced_at"]),
        #: 最近一次千帆登录态巡检时间（T-31）；空 = 从未巡检。
        "health_checked_at": _state_time_text(state["health_checked_at"]),
    }


def _note_goods_from_parts(
    item_id: str,
    title: str,
    image: str,
    variant_desc: str,
    price: str,
    shop_name: str,
) -> dict[str, Any]:
    """挂载商品的六个字段 → 前端 `NoteGoods` 契约（**唯一**的合成点，DEF-047）。

    列表页（`_attach_shop_goods`）与详情页（`note_detail`）都必须给**同一形状**的商品：
    同一份数据两处各合成一次，就是「同一语义留两份实现」的翻版 —— 一处改了另一处不会跟着改，
    前端还得为两种形状各写一套渲染（那正是 DEF-047 的成因）。
    """
    return {
        "goods_id": item_id,
        "title": title,
        "price": price,
        "image": image,
        "url": (f"https://www.xiaohongshu.com/goods-detail/{item_id}" if item_id else ""),
        "shop_name": shop_name,
        "variant": variant_desc,
    }


def _note_goods_from_shop_row(row: ShopNoteGoods, shop_name: str) -> Optional[dict[str, Any]]:
    """千帆映射行 → 前端 `NoteGoods` 契约。

    `price` **恒为空**：千帆商品笔记列表不返回价格，而「解析不到就留空、不造假」
    是本模块的硬口径（需求 §七 数据真实性）。价格要另取商品详情接口，属 M2 商品同步范围。
    """
    if not row.item_id and not row.item_title:
        return None
    return _note_goods_from_parts(
        row.item_id, row.item_title, row.item_image, row.variant_desc, "", shop_name
    )


def _note_shop_metrics_from_row(row: ShopNoteGoods) -> Optional[dict[str, Any]]:
    """千帆条目 → 该笔记的**店铺侧指标**（`shop` 字段）。

    「创作者账户数据全部接入千帆」的落地：千帆为每条商品笔记给出的字段
    （标题 / 封面 / 四项互动 / 阅读数 / 30 日阅读 / 支付金额与订单数 / 发布状态 /
    发布时间 / xsec_token）全部透出，不再只取商品那一小块。

    缺失的键**不填 0**：读不到就留 `None`，由渲染层决定不显示（§18.2 的同一口径）。
    """
    try:
        entry = json.loads(row.entry_json or "{}")
    except (TypeError, ValueError):
        entry = {}
    if not isinstance(entry, dict) or not entry:
        return None

    def number(*keys: str) -> Optional[float]:
        for key in keys:
            value = entry.get(key)
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float)):
                return float(value)
            try:
                return float(str(value).strip())
            except (TypeError, ValueError):
                continue
        return None

    def text(*keys: str) -> str:
        for key in keys:
            value = entry.get(key)
            if value is None:
                continue
            rendered = str(value).strip()
            if rendered:
                return rendered
        return ""

    return {
        "note_id": text("note_id"),
        "title": text("note_title"),
        "cover": text("note_image"),
        "note_flag": text("note_flag"),
        "browse_num": number("browse_num"),
        "browse_num_30d": number("note_browse_num30d"),
        "likes": number("like_num"),
        "collects": number("collect_num"),
        "comments": number("comment_num"),
        "shares": number("share_num"),
        "pay_gmv": number("note_pay_gmv"),
        "pay_order_cnt": number("note_pay_order_cnt"),
        "status": number("note_status"),
        "note_type": number("note_type"),
        "publish_time": number("publish_time"),
        "xsec_token": text("xsec_token"),
    }


def _price_text(min_fen: Any, max_fen: Any) -> str:
    """千帆价格（**分**）→ 展示用元字符串：同价给 `54.40`，区间给 `54.40~144.00`。"""
    try:
        low = int(min_fen) if min_fen is not None else None
        high = int(max_fen) if max_fen is not None else None
    except (TypeError, ValueError):
        return ""
    if low is None and high is None:
        return ""
    if low is None or high is None:
        only = low if low is not None else high
        return f"{only / 100:.2f}"
    return f"{low / 100:.2f}" if low == high else f"{low / 100:.2f}~{high / 100:.2f}"


def _batch_item_prices(
    cookies: str, adapter_factory: Any, item_ids: list[str]
) -> dict[str, str]:
    """**一次**批量查价：千帆 `whole_keywords` 支持逗号分隔多个商品 ID。

    | 约定 | 做法 |
    |---|---|
    | 有界 | 去重后最多 :data:`_GOODS_PRICE_BATCH_LIMIT` 个 ID（一页笔记 ≤ 50 条，够用） |
    | 一次请求 | 实测 3 个 ID 一次请求全部命中 —— 所以一页笔记只多 **1** 次平台调用 |
    | 失败静默 | 查价是**增强项**：失败就返回 `{}`（价格留空），绝不让「我的笔记」因此报错 |
    | 状态不限 | `status="all"`（不传 `card_type`）—— 已下架的商品也有价，也能显示 |

    入参是**已取好的 Cookie 串**（调用方走唯一取票入口 `qianfan_cookies_for_account`）：
    本函数曾自己从绑定行解密，等于在「账号自有 ark 令牌」这条路上开了个后门。

    价格**不入库**：挂链映射（`goods-note/list`）本身没有价格，而商品价随时会变，
    缓存只会制造第二套真相源。
    """
    ids = [item_id for item_id in dict.fromkeys(item_ids) if item_id][:_GOODS_PRICE_BATCH_LIMIT]
    if not ids or not cookies or adapter_factory is None:
        return {}
    try:
        adapter = adapter_factory(cookies)
        success, _message, payload = adapter.search_items(
            keyword=",".join(ids),
            status="all",
            page_size=min(len(ids), 100),
        )
    except Exception:  # noqa: BLE001 - 增强项，任何异常都不向上抛
        return {}
    if not success:
        return {}
    rows = ((payload or {}).get("data") or {}).get("items") or []
    prices: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("item_id") or "")
        text = _price_text(row.get("min_price"), row.get("max_price"))
        if item_id and text:
            prices[item_id] = text
    return prices


def _attach_shop_goods(
    db: Session,
    account: PlatformAccount,
    items: list[dict[str, Any]],
    qianfan_adapter_factory: Any = None,
) -> dict[str, Any]:
    """给一页笔记就地补上千帆挂载商品（**按需补齐** + 价格）；返回 `goods_source` 元信息。

    | 数据 | 来源 | 平台请求 |
    |---|---|---|
    | 商品（标题/主图/规格/链接） | 本页缺映射时**按 note_ids 一次补齐**（§24.2.1），已有映射则纯本地读 | **0 或 1** |
    | **价格** | 千帆商品列表接口，按本页商品 ID 批量查一次 | **0 或 1**（仅当确有带商品但缺价的笔记） |

    「按需补齐」取代了早前的「后台全量同步」：店铺可能有近 2000 条商品笔记，而一页只有
    10 条 —— 只补用户真正打开的这页，成本从「上百页翻取」降到「**每页 ≤1 次**只读请求」，
    并且第一次看过的笔记会落库，第二次打开就是**零平台请求**。
    """
    info = _shop_goods_source(db, account)
    if not info["bound"] or not items:
        return info

    note_ids = [str(item.get("note_id") or "") for item in items if item.get("note_id")]
    if not note_ids:
        return info

    #: 取票走**唯一入口**（账号自己的 `ark` 通道）；没有就只读本地映射、不打平台。
    cookies = qianfan_cookies_for_account(db, account.id)

    # ① 按需补齐：只查本页**还没有映射**的笔记（已有映射 = 零请求）。
    if qianfan_adapter_factory is not None and cookies:
        rows_now = existing_rows(db, account.id)
        missing = missing_note_ids(rows_now, note_ids)
        if missing:
            outcome = sync_note_ids(db, account, missing, qianfan_adapter_factory)
            info["on_demand"] = {
                "requested": outcome["requested"],
                "stored": outcome["stored"],
                "error": outcome["error"],
            }
            if outcome["credential_invalid"]:
                state = get_shop_state(account)
                info["status"] = str(state["status"])
                info["status_message"] = str(state["status_message"])
            if outcome["stored"]:
                # 覆盖度是**补齐前**算的，这里按同一函数重算，避免把过期计数回给页面。
                refresh = _shop_goods_source(db, account)
                info["synced_notes"] = refresh["synced_notes"]
                info["synced_at"] = refresh["synced_at"]

    rows = db.scalars(
        select(ShopNoteGoods).where(
            ShopNoteGoods.platform_account_id == account.id,
            ShopNoteGoods.note_id.in_(note_ids),
        )
    ).all()
    mapping = {row.note_id: row for row in rows}
    # ADR-021：店铺实体已退场 ⇒ 商品展示里的店铺名取账号昵称。
    shop_name = (account.nickname or "").strip()
    pending: list[str] = []
    for item in items:
        row = mapping.get(str(item.get("note_id") or ""))
        if row is None:
            continue
        goods = _note_goods_from_shop_row(row, shop_name)
        if goods is not None:
            item["goods"] = goods
            if goods.get("goods_id") and not goods.get("price"):
                pending.append(str(goods["goods_id"]))
        metrics = _note_shop_metrics_from_row(row)
        if metrics is not None:
            item["shop"] = metrics

    if pending and qianfan_adapter_factory is not None and cookies:
        prices = _batch_item_prices(cookies, qianfan_adapter_factory, pending)
        if prices:
            for item in items:
                goods = item.get("goods")
                if isinstance(goods, dict) and not goods.get("price"):
                    goods["price"] = prices.get(str(goods.get("goods_id") or ""), "")
            info["price_source"] = "ark:items"
    return info


def _account_channels(db: Session, account_id: int) -> set[str]:
    """这个账号**实际存了哪些平台的令牌**（§11.5 / 2026-09-21 拆分）。

    纯本地查询（一次 SQL + 解密），零平台请求 —— 能力端点要在身份解析失败时也能回答。
    平台集合从信封里读（`{"channels": {...}}`），不再有独立的 channel 列。
    """
    from backend.app.services.account_service import channel_jars

    return set(channel_jars(db, account_id).keys())


def _self_content_capabilities(
    db: Session,
    account: PlatformAccount,
    cookies: str,
    current_user: Optional[User] = None,
    adapter_factory: Any = None,
) -> dict[str, Any]:
    """Which self-content links this account can actually use.

    Mostly pure computation over already-stored rows (account + cookie + 千帆 binding)
    so the frontend can rely on it even when identity resolution fails (design doc
    §17.4). **One exception, added in v1.20**: 我赞过的 / 我收藏的 / 互动消息 三类
   只由**网页版**（`www` 的 `sns/web`）提供 —— 注意是「**由网页版提供**」，
    不是「创作者平台没有这个能力」：真机抓包已证明创作者账号**只要持有 www 会话**
    三类端点全部可达（§17.7.3），所以这里的判据是**凭据**。

    **2026-09-22 用户拍板（「每个账号只使用自己账号对应的平台有效登录令牌」）**：
    凭据只认**账号自己的** www 会话；没有就是 `available=False` + 可读原因，让用户去给
    这个账号补登录。原先「向同一个人 / 同平台借会话」的分支**整条移除** —— 它读到的
    始终是别人的数据面，而且为了证明借来的会话还活着，还得额外发一次只读
    ``/v2/user/me``。移除后本端点**恒为零平台请求**（只读账号行 + 解密一版 Cookie），
    「需要探活」这个分支在结构上不再存在。

    **v1.24（§11.5）**：笔记链路从「按 `sub_type` 猜」改为**按平台判能力** ——
    账号里**存了 creator 或 ark 平台的令牌**（customer 单点登录）就能走创作者链路，
    不必再看它当初是按 pc 还是 creator 建的号。这正是「一个账号、多平台令牌」
    要解决的问题：库里不再需要一个身份两行账号。

    **v1.30（2026-09-21 拆分）**：千帆与创作者平台的令牌**分开存**（`ark` /
    `creator` 两个平台）。创作者链路的判据因此是「有 creator **或** ark 平台的
    令牌」—— 千帆那份完整 jar 是超集，实测同样能打创作者平台。
    """
    has_web_session = _has_web_session(cookies)
    channels = _account_channels(db, account.id)
    has_creator = CHANNEL_CREATOR in channels or CHANNEL_ARK in channels
    has_www_channel = CHANNEL_WWW in channels or has_web_session

    if has_web_session:
        notes_link = "pc"
    elif has_creator:
        notes_link = "creator"
    else:
        notes_link = ""

    notes_reason = (
        "该账号既没有网页版令牌（web_session），也没有创作者平台令牌（creator / ark 平台），"
        "无法读取笔记列表。请在网页版或创作者平台登录同一个账号后重试。"
    )
    # ⚠️ 用户维度这句话与 `_web_session_required_detail()`
    # **是同一件事**（都是「这个账号没有可用网页会话」）⇒ **直接复用那一份**，不再另写一句。
    # 这里曾经留着**第二份文案**，而且那一份没跟上 DEF-058 的更正文案 —— 于是「同一个
    # 问题，一处说的是对的、另一处说的是**已被真机抓包推翻**的归因」（创作者账号持
    # www 会话时三类端点全部可达，§17.7.3 / §17.8.4）。造成这种漂移的正是
    # **同一语义两份实现**；复用之后它在结构上不再可能发生。
    user_scope_reason = _web_session_required_detail()["message"]

    # 会话来源只认**自有**：available 为 False 时用户去给这个账号补网页登录。
    session_source = "own" if has_web_session else ""
    user_scope = {
        "available": bool(session_source),
        "reason": "" if session_source else user_scope_reason,
        "session_source": session_source,
    }

    goods_state = _shop_goods_source(db, account)
    goods_available = bool(goods_state["bound"])
    # 商品只有千帆一个真实来源：创作者列表的 goods_info 是空壳、网页版 feed 根本没有
    # 该字段（设计文档 §19）。这里声明的是**来源 + 是否已具备读取条件**，
    # 不再写「PC 链路不返回……待接入」这类占位说明。
    goods_reason = (
        ""
        if goods_available
        else "笔记挂载的商品来自千帆商家后台（ark），当前账号尚未绑定店铺；绑定并同步商品笔记后即可在本页看到商品。"
    )

    return {
        "account_id": account.id,
        "sub_type": account.sub_type,
        # §11.5：机器可读的「这个账号到底有哪些平台的令牌」。前端据此显示
        # 「网页版 / 创作者平台」两个入口的可用性，而不是靠 sub_type 猜。
        # 2026-09-21 拆分：`ark`（千帆）与 `creator`（创作者平台）各算一个；
        # `creator` 键对千帆那份超集 jar 也为真（实测可打创作者平台）。
        "channels": {
            "www": has_www_channel,
            "creator": has_creator,
            "ark": CHANNEL_ARK in channels,
        },
        "has_web_session": has_web_session,
        "session_source": session_source,
        "notes_link": notes_link,
        "capabilities": {
            "notes": {
                "available": bool(notes_link),
                "reason": "" if notes_link else notes_reason,
            },
            "liked": dict(user_scope),
            "collected": dict(user_scope),
            "interactions": dict(user_scope),
            "goods": {
                "source": "ark",
                "available": goods_available,
                "reason": goods_reason,
                "shop_name": goods_state["shop_name"],
                "shop_total": goods_state["shop_total"],
                "synced_notes": goods_state["synced_notes"],
                "synced_at": goods_state["synced_at"],
            },
        },
    }


def _load_self_note_page(
    db: Session,
    current_user: User,
    account_id: int,
    cursor: str,
    profile_url: str,
    xsec_token: str,
    xsec_source: str,
    scope: Literal["notes", "liked", "collected"],
    adapter_factory: Any,
    creator_adapter_factory: Any,
    qianfan_adapter_factory: Any = None,
) -> dict[str, Any]:
    """Shared body for the three self note-list scopes.

    Kept in one place so the three entry points cannot drift apart in their
    identity resolution, error mapping or pagination handling.
    """
    # 归属校验先单独做一次（DEF-055）：下面要按「有没有该账号**自己的** www 会话」判
    # 「走网页版链路还是走创作者平台链路」，而凭据本身可能压根没存 —— 那种情况
    # **不是错误**（v1.27 / DEF-056），
    # 所以账号本体与凭据要分开拿，不能让「取凭据失败」把账号信息也一起带走。
    account = get_owned_active_account(db, current_user, account_id, platform="xhs")
    cookies = ""
    try:
        _, cookies = _account_cookies_auto(db, current_user, account_id)
    except HTTPException as exc:
        # 同 `_get_owned_pc_account_cookies`：只吞「没存令牌」，归属类错误照旧抛。
        if exc.status_code != status.HTTP_400_BAD_REQUEST:
            raise

    # Capability first, type second (design doc §17.3). A jar carrying a www
    # session may use the PC link whatever ``sub_type`` says; a creator jar
    # cannot authenticate it at all, so "我发布的" is served by the creator
    # platform's own note manager instead of being refused.
    if not _has_web_session(cookies):
        if scope == "notes" and account.sub_type == "creator":
            page = _load_creator_posted_page(cookies, cursor, creator_adapter_factory)
            # 创作者列表带 `goods_info` 空壳（§19 实测），商品只能由千帆映射补齐；
            # 价格另由千帆商品接口按本页 ID 批量补一次（下面 factory 为空则跳过）。
            page["goods_source"] = _attach_shop_goods(
                db, account, page["items"], qianfan_adapter_factory
            )
            return page
        # 我赞过的 / 我收藏的：只认这个账号**自己的**网页会话
        # （2026-09-22 用户拍板：每个账号只使用自己账号对应的平台有效登录令牌）。
        # 拿不到即 400 + 可读原因 —— 绝不用别人的数据顶上。
        cookies = _get_owned_pc_account_cookies(
            db,
            current_user,
            account_id,
            channel="auto",
        )

    adapter = adapter_factory(cookies)
    resolution = _resolve_identity(
        adapter, profile_url, xsec_token, xsec_source, _LIST_XSEC_SOURCE[scope]
    )
    if not resolution.user_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=resolution.self_message or "无法识别当前账号身份，请确认 Cookie 是否有效",
        )

    fetchers = {
        "notes": adapter.get_user_posted_page,
        "liked": adapter.get_user_liked_page,
        "collected": adapter.get_user_collected_page,
    }
    success, message, raw_payload = fetchers[scope](
        resolution.user_id, cursor, resolution.xsec_token, resolution.xsec_source
    )
    if not success:
        # user_posted wants a URL-level xsec_token. When none was supplied, point
        # the user at the pasted-profile-URL path rather than surfacing a bare
        # platform error they cannot act on. The platform message is preserved
        # so a wrong guess is still diagnosable.
        if not resolution.xsec_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "XSEC_TOKEN_REQUIRED",
                    "message": (
                        "平台拒绝了笔记列表请求，很可能需要主页访问令牌（xsec_token）。"
                        "请从浏览器地址栏复制你的小红书主页链接并粘贴。"
                    ),
                    "platform_message": message or "",
                },
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=message or "XHS self note list failed",
        )

    # The scope's detail-scene source is passed explicitly: the note's own
    # xsec_token is bound to the scene that issued it (design doc §18.4).
    items = [
        _normalize_search_item(item, _NOTE_XSEC_SOURCE[scope])
        for item in _items_from_payload(raw_payload, _NOTE_LIST_KEYS)
    ]
    cursor_out, has_more = _pagination_from_payload(raw_payload)
    response = {
        "items": items,
        "cursor": cursor_out,
        "has_more": has_more,
        "total": len(items),
        "scope": scope,
        "user_id": resolution.user_id,
        "token_source": resolution.token_source,
        "identity": resolution.identity,
    }
    if scope == "notes":
        # 同一套千帆映射对网页会话账号同样适用（店铺的商品笔记不区分读它的链路）。
        response["goods_source"] = _attach_shop_goods(
            db, account, items, qianfan_adapter_factory
        )
    return response


@router.post("/users/me")
def user_me(
    payload: SelfProfileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
    creator_adapter_factory=Depends(get_xhs_creator_api_adapter_factory),
    qianfan_adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """Identify the signed-in account; the prerequisite for the note tabs.

    Two identity sources, chosen by capability: a www session is read through
    ``/v2/user/me``, a creator session through ``/api/galaxy/user/info``.
    """
    account, cookies = _account_cookies_auto(db, current_user, payload.account_id)
    if not _has_web_session(cookies):
        if account.sub_type != "creator":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=_web_session_required_detail()
            )
        return _creator_identity(cookies, creator_adapter_factory)

    success, message, raw_payload = adapter_factory(cookies).get_self_profile()
    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=message or "XHS self profile failed",
        )
    data = raw_payload.get("data") if isinstance(raw_payload, dict) else None
    identity = _normalize_identity(data)
    if not identity["user_id"]:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="平台响应中未包含 user_id，无法识别账号身份",
        )
    return {
        **identity,
        "link": "pc",
        # Whitelist rather than spread: the platform payload is not a contract
        # we control, and an unlisted key must never leak by default (design
        # doc §10).
        "raw": {
            key: (data or {}).get(key)
            for key in ("user_id", "nickname", "red_id", "images", "imageb", "desc", "gender")
        },
    }


@router.post("/users/capabilities")
def user_capabilities(
    payload: SelfProfileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
):
    """Which self-content links this account can actually use.

    Deliberately a separate endpoint: it only reads the account row and decrypts
    one cookie version, and issues **no platform request** at all. 2026-09-22
    （用户拍板：每个账号只使用自己账号对应的平台有效登录令牌）之后不再有
    「借同身份会话 + 发一次 ``/v2/user/me`` 探活」那条分支 —— 凭据只认账号
    自己的 www 会话。The frontend needs this even when ``/users/me`` fails
    (expired cookie), and it can therefore be called on every account switch.
    See design doc §17.4.
    """
    account, cookies = _account_cookies_auto(db, current_user, payload.account_id)
    return _self_content_capabilities(db, account, cookies, current_user, adapter_factory)


@router.post("/users/no-traffic/candidates")
def no_traffic_candidates(
    payload: NoTrafficCandidatesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """无流量候选预览（**纯本地**，零平台请求）。

    阈值由前端传入，后端不写死：阅读 / 支付订单数 / 支付金额各自可配，
    三项**全部**小于等于阈值才算无流量。缺值（未同步千帆）的笔记不进池。
    """
    account, _ = _account_cookies_auto(db, current_user, payload.account_id)
    thresholds = TrafficThresholds.from_dict(payload.thresholds)
    query = CleanupQuery.from_dict(payload.query)
    result = list_no_traffic_candidates(
        db, account.id, thresholds, limit=payload.limit, query=query, page=payload.page
    )
    return {
        **result,
        "candidates": [serialize_candidate(c) for c in result["candidates"]],
    }


@router.post("/users/no-traffic/cleanup")
def no_traffic_cleanup(
    payload: NoTrafficCleanupRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """开始无流量清理（**后台异步**，立即返回任务 id）。

    手动发起与定时调度共用 ``run_no_traffic_cleanup_background``：
    同一账号已有未完成的清理任务时直接返回那一条，不重复触发。
    """
    account, _ = _account_cookies_auto(db, current_user, payload.account_id)
    thresholds = TrafficThresholds.from_dict(payload.thresholds)
    query = CleanupQuery.from_dict(payload.query)
    task_id = run_no_traffic_cleanup_background(
        account.id,
        thresholds=thresholds,
        query=query,
        batch_limit=min(payload.batch_limit, DEFAULT_DELETE_BATCH),
        note_ids=[str(n) for n in payload.note_ids] or None,
        user_id=current_user.id,
    )
    # 日志行是**后台线程**开的（那时才算得出「清理前多少条」）⇒ 回执里不保证有 log_id；
    # 前端要展示日志就查列表端点（它按账号 + 时间倒序，最新一条就是本次）。
    return {"task_id": task_id, "status": "pending"}


@router.get("/users/no-traffic/cleanup/active")
def no_traffic_cleanup_active(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """该账号此刻**是否有清理在跑**（「正在清理无流量笔记」按钮态的数据源）。

    ⚠️ **必须注册在 `/users/no-traffic/cleanup/{task_id}` 之前**：否则 `active`
    会被那条路径吃掉当成 `task_id` 去 `int()` 转换 ⇒ 422。
    （同类坑见 `GET /notes/sync-queue` / `hydrate-queue` 的路由顺序注释。）

    返回 `{"running": bool, "task_id": int|None, "log_id": int|None, "status": str}`
    —— `task_id` 让前端把弹窗关闭后剩下的轮询接回同一条任务。
    """
    account, _ = _account_cookies_auto(db, current_user, account_id)
    task = running_cleanup_for_account(db, account.id, current_user.id)
    if task is None:
        return {"running": False, "task_id": None, "log_id": None, "status": ""}
    payload = task.payload if isinstance(task.payload, dict) else {}
    return {
        "running": True,
        "task_id": task.id,
        "log_id": payload.get("log_id"),
        "status": task.status,
    }


@router.get("/users/no-traffic/cleanup/{task_id}")
def no_traffic_cleanup_status(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查清理任务状态（前端轮询用）。"""
    task = db.scalars(
        select(Task).where(Task.id == task_id, Task.user_id == current_user.id)
    ).first()
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    payload = task.payload if isinstance(task.payload, dict) else {}
    return {
        "task_id": task.id,
        "status": task.status,
        "progress": task.progress,
        "deleted": payload.get("deleted", 0),
        "skipped": payload.get("skipped", 0),
        "failed": payload.get("failed", 0),
        "total": payload.get("total", 0),
        "note": payload.get("note", ""),
        "details": payload.get("details", []),
        # 「清理前 / 清理后 多少条笔记」（用户口径）：清理后是**重算**的候选池总数，
        # 不是 before - deleted 的估算（估算会把跳过/失败算成已清）。
        "before_total": payload.get("before_total"),
        "after_total": payload.get("after_total"),
        #: 勾选了但**已不在候选池**的笔记（改过阈值 / 已被删）—— 不删、如实报。
        "not_in_pool": payload.get("not_in_pool", []),
    }


@router.get("/users/no-traffic/cleanup-logs")
def no_traffic_cleanup_logs(
    account_id: Optional[int] = None,
    page: int = 1,
    limit: int = 20,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """清理无流量笔记的**操作日志**（用户口径「新增一个查询清理无流量笔记日志详情按钮，
    弹窗列表展示该日志记录」）。

    按**当前用户**收口（跨租户不可见）；`account_id` 给了就再收窄到一个账号。
    时间倒序。字段见 `NoTrafficCleanupLog.to_dict()`：账号 / 阈值 / 清理前多少条 /
    删了几篇 / 跳过 / 失败 / 失败原因 / 操作时间 / 结果时间。
    """
    page = max(1, int(page or 1))
    limit = min(max(1, int(limit or 20)), 200)
    where = [NoTrafficCleanupLog.user_id == current_user.id]
    if account_id:
        where.append(NoTrafficCleanupLog.account_id == int(account_id))
    total = int(
        db.scalar(select(func.count()).select_from(NoTrafficCleanupLog).where(*where)) or 0
    )
    rows = (
        db.scalars(
            select(NoTrafficCleanupLog)
            .where(*where)
            .order_by(NoTrafficCleanupLog.id.desc())
            .offset((page - 1) * limit)
            .limit(limit)
        )
        .all()
    )
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "page_count": max(1, (total + limit - 1) // limit),
        "items": [row.to_dict() for row in rows],
    }


@router.post("/users/notes")
def user_notes(
    payload: SelfContentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
    creator_adapter_factory=Depends(get_xhs_creator_api_adapter_factory),
    qianfan_adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """Notes authored by the signed-in account.

    Replaces the former ``sample_notes()`` mock. Scope is fixed to the current
    account: ``/users/*`` here always means "self", never "any user".

    Served by the PC link when the cookie jar holds a www session, and by the
    creator platform's note manager for creator accounts — every Xiaohongshu
    account can publish, so this tab must work for both (design doc §17).
    """
    return _load_self_note_page(
        db,
        current_user,
        payload.account_id,
        payload.cursor,
        payload.profile_url,
        payload.xsec_token,
        payload.xsec_source,
        "notes",
        adapter_factory,
        creator_adapter_factory,
        qianfan_adapter_factory,
    )


@router.post("/users/liked")
def user_liked(
    payload: SelfContentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
    creator_adapter_factory=Depends(get_xhs_creator_api_adapter_factory),
    qianfan_adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """Notes the signed-in account has liked.

    A www-session feature: an account holding only an ``ark`` channel gets 400
    ``WEB_SESSION_REQUIRED`` with a structured, self-serviceable reason.

    ⚠️ 界面**不再置灰**这个页签（v1.28 / DEF-057 / DEF-058）：置灰时给出的
    理由（「创作者平台没有这个能力」）已被真机抓包推翻 —— 三个 www 端点对
    创作者账号全部可达，差别只在凭据。现在页签一律可点、一律发请求，
    **取到就展示、没数据就空态、取不到才安静说一句**（设计 12 §17.8）。
    """
    return _load_self_note_page(
        db,
        current_user,
        payload.account_id,
        payload.cursor,
        payload.profile_url,
        payload.xsec_token,
        payload.xsec_source,
        "liked",
        adapter_factory,
        creator_adapter_factory,
    )


@router.post("/users/collected")
def user_collected(
    payload: SelfContentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
    creator_adapter_factory=Depends(get_xhs_creator_api_adapter_factory),
    qianfan_adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """Notes the signed-in account has collected. www session required (see user_liked)."""
    return _load_self_note_page(
        db,
        current_user,
        payload.account_id,
        payload.cursor,
        payload.profile_url,
        payload.xsec_token,
        payload.xsec_source,
        "collected",
        adapter_factory,
        creator_adapter_factory,
    )


@router.post("/users/interactions")
def user_interactions(
    payload: InteractionMessagesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
):
    """Interaction notifications: likes/collects, mentions, new followers."""
    # 只认这个账号**自己的**网页会话（2026-09-22 用户拍板：每个账号只使用
    # 自己账号对应的平台有效登录令牌）。借不到就 400，
    # 绝不用别人的互动消息顶上。
    cookies = _get_owned_pc_account_cookies(
        db,
        current_user,
        payload.account_id,
        channel="auto",
    )
    success, message, raw_payload = adapter_factory(cookies).get_interaction_messages(
        payload.kind, payload.cursor
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=message or "XHS interaction messages failed",
        )
    items = [
        _normalize_interaction_message(item, payload.kind)
        for item in _items_from_payload(raw_payload, _MESSAGE_LIST_KEYS)
    ]
    cursor_out, has_more = _pagination_from_payload(raw_payload)
    return {
        "items": items,
        "cursor": cursor_out,
        "has_more": has_more,
        "total": len(items),
        "kind": payload.kind,
    }


# ---------------------------------------------------------------------------
# 自有内容内的关键词搜索（R8 / 设计文档 §22）
#
# 语义：在「我发布的 / 我赞过的 / 我收藏的」三份列表里按关键词找笔记。
# 实现约束：**有界扫描**——每类最多 pages_per_scope 页，逐页调用单页方法；
# 不调 get_user_all_* 全量循环（C5：几百条笔记会变成几十次连续请求）。
#
# ⚠️ 匹配域的真相：自有内容的列表项**只有标题没有正文**（§18.1 实测），
# 所以这里的搜索**实际上是标题搜索**。正文/标签只在字段存在时一并匹配
# （详情接口才有正文，而逐条拉详情 = 另一种形式的全量循环，禁止）。
# 这一点必须在 UI 上说明，否则用户会以为「关键词没命中 = 我没有这条笔记」。
# ---------------------------------------------------------------------------

_SELF_SEARCH_SCOPES: tuple[str, ...] = ("notes", "liked", "collected")

_SELF_SEARCH_SCOPE_LABELS: dict[str, str] = {
    "notes": "我的笔记",
    "liked": "我赞过的",
    "collected": "我收藏的",
}


def _keyword_tokens(keyword: str) -> list[str]:
    """关键词 → token 列表：按空白切分并小写化；多 token 之间是 **AND** 语义。"""
    return [token for token in re.split(r"\s+", (keyword or "").strip().lower()) if token]


def _matches_keyword(note: dict[str, Any], tokens: list[str]) -> bool:
    """命中判定：标题 + 正文（若有）+ 话题标签（若有），大小写不敏感。

    刻意**不含作者名**：这里问的是「我的哪条内容里有这个关键词」，
    把作者算进来会让「搜索自己的昵称」命中全部条目，看起来像功能坏了。
    """
    if not tokens:
        return False
    tags = note.get("tags")
    haystack = " ".join(
        [
            str(note.get("title") or ""),
            str(note.get("content") or ""),
            " ".join(str(tag) for tag in tags) if isinstance(tags, list) else "",
        ]
    ).lower()
    return all(token in haystack for token in tokens)


@router.post("/users/search")
def user_search(
    payload: SearchSelfContentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
):
    """在自有内容范围内按关键词检索，跨「我发布的 / 我赞过的 / 我收藏的」。

    设计契约见 `02_设计文档/12_功能设计_我的笔记与互动数据.md` §22：

    | 约定 | 做法 |
    |---|---|
    | 有界扫描 | 每个分类最多 `pages_per_scope` 页（默认 3、上限 5，请求模型硬夹紧），逐页调用单页方法 |
    | 早停 | 某分类返回 `has_more=false` 即停止该分类 |
    | 部分失败 | 失败只记进 `scopes[x].error`，其余分类照常返回；**不整单 500** |
    | 能力路由 | 无 `web_session` → 400 `WEB_SESSION_REQUIRED`（创作者后台没有关键词检索接口，不假装支持） |
    | 令牌 | 复用三级降级；三类全失败且未提供令牌 → 结构化 400 `XSEC_TOKEN_REQUIRED` |
    """
    account, cookies = _account_cookies_auto(db, current_user, payload.account_id)
    if not _has_web_session(cookies):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=_web_session_required_detail()
        )

    adapter = adapter_factory(cookies)
    resolution = _resolve_identity(
        adapter, payload.profile_url, "", payload.xsec_source, _LIST_XSEC_SOURCE["notes"]
    )
    if not resolution.user_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=resolution.self_message or "无法识别当前账号身份，请确认 Cookie 是否有效",
        )

    tokens = _keyword_tokens(payload.keyword)
    fetchers = {
        "notes": adapter.get_user_posted_page,
        "liked": adapter.get_user_liked_page,
        "collected": adapter.get_user_collected_page,
    }

    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    scope_meta: dict[str, dict[str, Any]] = {}
    cursors_out: dict[str, str] = {}
    succeeded_scopes = 0
    first_error = ""

    for scope in _SELF_SEARCH_SCOPES:
        cursor = str((payload.cursors or {}).get(scope) or "")
        scanned_pages = 0
        scanned_items = 0
        matched = 0
        exhausted = False
        error = ""

        while scanned_pages < payload.pages_per_scope:
            success, message, raw_payload = fetchers[scope](
                resolution.user_id, cursor, resolution.xsec_token, _LIST_XSEC_SOURCE[scope]
            )
            if not success:
                error = message or "平台拒绝了该请求"
                first_error = first_error or error
                break

            page_items = [
                _normalize_search_item(item, _NOTE_XSEC_SOURCE[scope])
                for item in _items_from_payload(raw_payload, _NOTE_LIST_KEYS)
            ]
            scanned_pages += 1
            scanned_items += len(page_items)
            for note in page_items:
                if not _matches_keyword(note, tokens):
                    continue
                matched += 1
                key = (scope, str(note.get("note_id") or ""))
                if key in seen:
                    continue
                seen.add(key)
                items.append({**note, "scope": scope})

            cursor, page_has_more = _pagination_from_payload(raw_payload)
            if not page_has_more:
                exhausted = True
                break

        if scanned_pages:
            succeeded_scopes += 1
        scope_meta[scope] = {
            "label": _SELF_SEARCH_SCOPE_LABELS[scope],
            "scanned_pages": scanned_pages,
            "scanned_items": scanned_items,
            "matched": matched,
            "exhausted": exhausted,
            "error": error,
        }
        cursors_out[scope] = cursor

    # 三类全失败且用户没提供令牌：这正是 xsec_token 缺失的典型症状，
    # 给结构化错误让前端弹出「粘贴主页 URL」的降级入口（与列表端点同构）。
    if succeeded_scopes == 0 and not resolution.xsec_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "XSEC_TOKEN_REQUIRED",
                "message": (
                    "平台拒绝了搜索请求，很可能需要主页访问令牌（xsec_token）。"
                    "请从浏览器地址栏复制你的小红书主页链接并粘贴。"
                ),
                "platform_message": first_error,
            },
        )

    return {
        "keyword": payload.keyword,
        "items": items,
        "total_matched": len(items),
        "has_more": any(not meta["exhausted"] for meta in scope_meta.values()),
        "cursors": cursors_out,
        "scopes": scope_meta,
        "user_id": resolution.user_id,
        "token_source": resolution.token_source,
        "identity": resolution.identity,
    }


class HomefeedRecommendRequest(BaseModel):
    """主页推荐请求（T-107）。翻页由调用方带 `cursor_score` 再请求 —— 单次请求。"""

    model_config = ConfigDict(extra="forbid")

    account_id: int
    category: str = "homefeed_recommend"
    cursor_score: str = ""
    refresh_type: int = 1
    note_index: int = Field(default=0, ge=0)
    num: int = Field(default=20, ge=1, le=50)


@router.get("/homefeed/channels")
def homefeed_channels(
    account_id: int = Query(..., ge=1),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
):
    """主页频道列表（**真实现**，T-107 取代原 mock）。"""
    cookies = _get_owned_pc_account_cookies(db, current_user, account_id)
    success, message, raw_payload = adapter_factory(cookies).get_homefeed_channels()
    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=message or "XHS homefeed channels failed",
        )
    data = (raw_payload or {}).get("data") or {}
    raw_channels = data.get("channel") or data.get("channels") or []
    channels = [
        {
            "id": _first_text(item.get("id"), item.get("channel_id")),
            "name": _first_text(item.get("name"), item.get("title")),
        }
        for item in raw_channels
        if isinstance(item, dict)
    ]
    return {"items": channels, "raw": raw_payload}


@router.post("/homefeed/recommend")
def homefeed_recommend(
    payload: HomefeedRecommendRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
):
    """主页推荐（**真实现**，T-107 取代原 mock）。**单次请求**，翻页由调用方带游标。"""
    cookies = _get_owned_pc_account_cookies(db, current_user, payload.account_id)
    success, message, raw_payload = adapter_factory(cookies).get_homefeed_recommend(
        category=payload.category,
        cursor_score=payload.cursor_score,
        refresh_type=payload.refresh_type,
        note_index=payload.note_index,
        num=payload.num,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=message or "XHS homefeed recommend failed",
        )
    data = (raw_payload or {}).get("data") or {}
    raw_items = data.get("items") or []
    normalized = [
        _normalize_search_item(item)
        for item in raw_items
        if isinstance(item, dict)
    ]
    return {
        "items": normalized,
        "cursor_score": _first_text(data.get("cursor_score")),
        "has_more": bool(data.get("has_more", False)),
        "raw": raw_payload,
    }
