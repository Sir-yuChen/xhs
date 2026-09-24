from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from backend.app.api.platforms.xhs.pc import (
    get_xhs_pc_api_adapter_factory,
    normalize_comment_payload,
)
from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory
from backend.app.core.config import get_settings
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user, get_owned_active_account
from backend.app.core.time import shanghai_now
from backend.app.models import AiDraft, Note, NoteAsset, NoteComment, NoteGoods, PlatformAccount, ShopProduct, Tag, User, note_tags
from backend.app.models.note import (
    NOTE_TYPE_LOCAL,
    NOTE_TYPE_LOCAL_GENERATED,
    NOTE_TYPE_SYNCED,
    PUBLISH_STATUS_FILTER_GROUPS,
    note_supports_sync,
    set_note_publish_status,
)
from backend.app.schemas.common import paginated

router = APIRouter(prefix="/notes", tags=["notes"])

#: 内容库**笔记类型筛选**的合法取值（设计 13 §25，2026-09-23 新增筛选）。
#:
#: 从模型常量组装（**唯一定义处**在 `models/note.py`）—— 这里不重抄枚举字面量，
#: 否则以后加类型时必然出现「落库链路认、筛选却不认」的两处漂移。
#: 顺序即前端下拉顺序：三方同步 → 本地生成 → 本地。
NOTE_TYPE_FILTER_VALUES: tuple[str, ...] = (
    NOTE_TYPE_SYNCED,
    NOTE_TYPE_LOCAL_GENERATED,
    NOTE_TYPE_LOCAL,
)


class BatchSaveNoteItem(BaseModel):
    note_id: str = Field(min_length=1, max_length=128)
    note_url: str = ""
    title: str = ""
    content: str = ""
    author_name: str = ""
    cover_url: str = ""
    video_url: str = ""
    video_addr: str = ""
    image_urls: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class BatchSaveNotesRequest(BaseModel):
    account_id: int
    fetch_comments: bool = False
    notes: list[BatchSaveNoteItem] = Field(min_length=1)


class BatchTagNotesRequest(BaseModel):
    note_ids: list[int] = Field(min_length=1)
    tag_ids: list[int] = Field(default_factory=list)
    mode: Literal["replace", "add", "remove"] = "replace"


class BatchCreateDraftsRequest(BaseModel):
    note_ids: list[int] = Field(min_length=1)
    intent: str = Field(default="rewrite", max_length=32)


class ExportNotesRequest(BaseModel):
    note_ids: list[int] = Field(min_length=1)
    format: Literal["json", "csv"] = "json"


class UpdateNotePublishStatusRequest(BaseModel):
    """手动切换一条笔记的发布状态（设计 13 §19.12；**口径被 §32.3.3 收敛**）。

    2026-09-23 起发布状态只有**一个字段**：`publish_status`（`published` 布尔列
    降级为它的投影）。所以这个请求体从 `{published: bool}` 改成
    `{publish_status: "pending"|"published"}` —— 手切白名单**只有这两档**
    （`publishing` 是链路专属瞬时态、`failed` 由链路写，用户手设都没有意义）；
    非法取值由 Pydantic 直接 422 挡回（`Literal` 白名单，不猜枚举）。

    刻意**不需要**「不传 = 保持不变」的二义处理：字段必填，语义就是「改成什么」。
    """

    publish_status: Literal["pending", "published"]


class BatchPublishNotesRequest(BaseModel):
    """批量发布待发布笔记（设计 13 §31.5 步 8）。

    `note_ids` 是 `notes.id`（本地主键，不是平台 ID）；`max_items` 是服务端
    硬上限的**请求侧复述**（默认 20）—— 超限由服务端截断并如实报 `total`，
    不静默丢也不报错（批量是显式动作，分多次即可）。
    """

    note_ids: list[int] = Field(min_length=1)
    max_items: int = Field(default=20, ge=1, le=20)


#: 话题归一化上限（设计 13 §29.3）：展示字段，超限**静默截断**不报错。
NOTE_TOPICS_MAX_ITEMS = 20
NOTE_TOPICS_MAX_LEN = 50


def _normalize_topics(raw: list[str]) -> list[str]:
    """话题归一化（**唯一实现**，设计 13 §29.3）：trim → 去空 → 去重 → 限量。

    去重键 = 去空格后的**小写**（`#露营` 与 `#露营 ` 是同一个话题）；保留**首次
    出现的大小写形态**（用户敲的原样）。空列表合法（= 清空话题）。
    """
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > NOTE_TOPICS_MAX_LEN:
            text = text[:NOTE_TOPICS_MAX_LEN]
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= NOTE_TOPICS_MAX_ITEMS:
            break
    return out


class UpdateNoteTopicsRequest(BaseModel):
    """话题整列替换（设计 13 §29.3）：内容库话题增删的唯一落库入口。

    **整列替换语义**：调用方传增删后的**完整列表**，后端不猜「加了哪个删了哪个」——
    猜就是造第二份真相源（DEF-044/058 的教训）。归一化见 `_normalize_topics`。
    """

    topics: list[str] = Field(default_factory=list, max_length=200)


def _serialize_tag(tag: Tag) -> dict:
    return {
        "id": tag.id,
        "name": tag.name,
        "color": tag.color,
    }


def _get_note_tags(db: Session, note_id: int) -> list[dict]:
    tags = db.scalars(
        select(Tag)
        .join(note_tags, Tag.id == note_tags.c.tag_id)
        .where(note_tags.c.note_id == note_id)
        .order_by(Tag.id.asc())
    ).all()
    return [_serialize_tag(tag) for tag in tags]


def _get_note_assets(db: Session, note: Note) -> list[NoteAsset]:
    return db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id).order_by(NoteAsset.sort_order.asc(), NoteAsset.id.asc())).all()


def _asset_display_url(asset: NoteAsset) -> str:
    if asset.local_path:
        return f"/api/files/media/{asset.local_path}"
    return asset.url


def _json_list(text: str) -> list:
    """Text 列里的 JSON 数组 ⇒ list；坏 JSON / 非数组 ⇒ `[]`（不造假）。"""
    try:
        value = json.loads(text or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _note_goods(db: Session, note: Note) -> list[dict]:
    """挂载商品（设计 13 §28.3）：走 `note_goods` 关系表 join `shop_products`。

    **商品信息只在 `shop_products` 存一份**（用户 2026-09-22 口径：「笔记表不要再存
    商品信息，直接通过笔记和商品关联表进行查询」）⇒ 商品改价 / 下架后，这里查出去
    的是**当下的真值**，不是笔记行里一份会漂的快照。关系为空 = 没挂商品（不是错误）。
    """
    rows = db.scalars(
        select(ShopProduct)
        .join(NoteGoods, NoteGoods.product_id == ShopProduct.id)
        .where(NoteGoods.note_id == note.id)
        .order_by(ShopProduct.id.asc())
    ).all()
    return [
        {
            "item_id": product.item_id,
            "item_title": product.item_title,
            # 价格是**分**的字符串（列名即口径，展示层换算成元）。
            "price": product.price,
            "images": _json_list(product.images_json),
            "stock": product.stock,
            "sales": product.sales,
        }
        for product in rows
    ]


def _serialize_note(db: Session, note: Note) -> dict:
    assets = _get_note_assets(db, note)
    image_assets = [asset for asset in assets if asset.asset_type == "image"]
    video_assets = [asset for asset in assets if asset.asset_type == "video"]
    asset_urls = [_asset_display_url(asset) for asset in assets if asset.url or asset.local_path]
    raw = note.raw_json if isinstance(note.raw_json, dict) else {}
    raw_cover = raw.get("cover_url") if isinstance(raw.get("cover_url"), str) else ""
    # 来源账号（2026-09-22）：同步落库的笔记要能看出**它属于哪个账号** ——
    # 「已发布」这个标识本身就是账号维度的事实，界面不给账号就无从核对。
    # 软删账号的昵称**照常回传**：那是这条笔记的历史归属（事实），不是「可选账号」；
    # 昵称取不到时留空，前端按缺值口径不展示（硬约束⑤）。
    account = db.get(PlatformAccount, note.platform_account_id) if note.platform_account_id else None
    return {
        "id": note.id,
        "platform": note.platform,
        "platform_account_id": note.platform_account_id,
        "account_name": (account.nickname or "") if account is not None else "",
        "note_id": note.note_id,
        "title": note.title,
        "content": note.content,
        "author_name": note.author_name,
        # 发布状态：`_serialize_note` 是列表 / 详情 / 导出 JSON 三处的**同一份**输出，
        # 因此这一列天然三处一致（不另开序列化路径）。
        # ⚠️ 2026-09-23 起它是 **`publish_status` 的投影**（设计 13 §32.3.2），
        # 仅为 CSV / 既有消费者保留；界面与筛选一律看 `publish_status`。
        "published": bool(note.published),
        # 笔记类型（设计 13 §25）：`synced` = 三方同步笔记 / `local` = 本地笔记。
        # 内容库详情卡的「同步」按钮**只对 synced 渲染**（本地笔记是采集来的别人
        # 的笔记，拉平台最新内容没有意义）。列 not null，不存在第三态。
        "note_type": note.note_type,
        # 话题（设计 13 §28）：专有列，`raw_json` 的投影；空 = 没解析到（不造假）。
        "topics": _json_list(note.topics_json),
        # 挂载商品（设计 13 §28.3）：走 `note_goods` join `shop_products` 查，
        # **不在笔记表存商品快照**；空 = 没挂商品。
        "goods": _note_goods(db, note),
        # 对账标记（设计 13 §24）：平台列表里已经没有这条笔记的时间。
        # `null` = 正常；非空 = 最近一次对账发现「本地有、平台无」。
        "platform_missing_at": note.platform_missing_at.isoformat() if note.platform_missing_at else None,
        # 发布状态（设计 13 §32.3）：**所有笔记的唯一发布状态字段** ——
        # `pending` 待发布 / `publishing` 发布中 / `published` 已发布 / `failed` 发布失败。
        # 卡片与详情只渲染这一个状态（失败时 Tooltip 摆 `publish_error` 原文）。
        "publish_status": note.publish_status or "",
        "publish_error": note.publish_error or "",
        "publish_attempts": note.publish_attempts or 0,
        "published_at": note.published_at.isoformat() if note.published_at else None,
        "raw_json": note.raw_json,
        "asset_urls": asset_urls,
        "cover_url": _asset_display_url(image_assets[0]) if image_assets else raw_cover,
        "video_url": _asset_display_url(video_assets[0]) if video_assets else "",
        "video_addr": _asset_display_url(video_assets[0]) if video_assets else "",
        "created_at": note.created_at.isoformat(),
    }


def _serialize_note_with_tags(db: Session, note: Note) -> dict:
    serialized = _serialize_note(db, note)
    serialized["tags"] = _get_note_tags(db, note.id)
    return serialized


def _build_notes_csv(db: Session, notes: list[Note]) -> str:
    output = io.StringIO()
    fieldnames = ["note_id", "title", "author_name", "content", "tags", "published", "created_at"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for note in notes:
        tags = ",".join(tag["name"] for tag in _get_note_tags(db, note.id))
        writer.writerow(
            {
                "note_id": note.note_id,
                "title": note.title,
                "author_name": note.author_name,
                "content": note.content,
                "tags": tags,
                # 与 API 字段同一表示（`true` / `false`），不留第二套写法。
                "published": "true" if note.published else "false",
                "created_at": note.created_at.isoformat(),
            }
        )
    return output.getvalue()


def _serialize_asset(asset: NoteAsset) -> dict:
    return {
        "id": asset.id,
        "note_id": asset.note_id,
        "asset_type": asset.asset_type,
        "url": _asset_display_url(asset),
        "local_path": asset.local_path,
        "download_url": f"/api/files/media/{asset.local_path}" if asset.local_path else "",
        "sort_order": asset.sort_order,
    }


def _serialize_comment(comment: NoteComment) -> dict:
    return {
        "id": comment.id,
        "note_id": comment.note_id,
        "comment_id": comment.comment_id,
        "user_name": comment.user_name,
        "user_id": comment.user_id,
        "content": comment.content,
        "like_count": comment.like_count,
        "parent_comment_id": comment.parent_comment_id,
        "created_at_remote": comment.created_at_remote,
        "raw_json": comment.raw_json,
    }


def _serialize_draft(draft: AiDraft) -> dict:
    return {
        "id": draft.id,
        "platform": draft.platform,
        "title": draft.title,
        "body": draft.body,
        "source_note_id": draft.source_note_id,
        "created_at": draft.created_at.isoformat(),
    }


def _get_owned_account(db: Session, current_user: User, account_id: int) -> PlatformAccount:
    """（薄别名）**唯一实现见 `core/deps.py::get_owned_active_account`**（DEF-055）。

    本函数曾是同一语义的 4 份拷贝之一，**漏了软删闸** —— 已删除的账号仍能从这里过闸，
    用它名下的笔记继续走需要账号的操作。
    """
    return get_owned_active_account(db, current_user, account_id, platform="xhs")


def _get_latest_account_cookies(db: Session, account: PlatformAccount) -> str:
    """抓笔记评论的令牌：**只取 www 平台**（2026-09-21 拆分，§11.5）。

    评论接口（`edith`）只有网页版提供，调用方也已要求 `sub_type == "pc"`；
    按平台取 www，不再按「最新一版」猜（拆分后最新一行信封可能只含本次登录
    碰过的那个平台）。取不到返回空串，由调用方决定怎么报。
    """
    from backend.app.services.account_service import CHANNEL_WWW, latest_cookies_for_channel

    return latest_cookies_for_channel(db, account.id, CHANNEL_WWW)


def _get_owned_note(db: Session, current_user: User, note_id: int) -> Note:
    note = db.get(Note, note_id)
    if note is None or note.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    return note


def _get_unique_owned_notes(db: Session, current_user: User, note_ids: list[int]) -> list[Note]:
    return [_get_owned_note(db, current_user, note_id) for note_id in dict.fromkeys(note_ids)]


def _publish_status_stats(db: Session, user_id: int, platform: Optional[str]) -> dict:
    """内容库「待发布 / 已发布 / 发布失败」计数（设计 05 §2.5；三档口径见设计 13 §32.3.3）。

    **全库口径**：只按 `user_id`（+ `platform`）统计，**不叠加** `q` / `tag_id` /
    `has_assets` / `has_comments` / `publish_status` 任一筛选 —— 它服务的是「内容池结构」
    这一眼。跟着筛选走会在「只看发布失败」时把已发布显示成 0，用户读不出池子结构。

    同理，**前端不得用 `items.length` 反推**（那是「本页条数」）。

    三数之和 = 该用户（该平台）的笔记总数（迁移 `v5w6x7y8z9a0` 之后不存在第四态；
    未回填的空值 `""` 归入「待发布」档，与筛选同一份映射 `PUBLISH_STATUS_FILTER_GROUPS`）。
    """
    base = select(Note.publish_status, func.count()).select_from(Note).where(Note.user_id == user_id)
    if platform:
        base = base.where(Note.platform == platform)
    counts = {str(status or ""): int(total or 0) for status, total in db.execute(base.group_by(Note.publish_status)).all()}
    return {
        group: sum(counts.get(value, 0) for value in values)
        for group, values in PUBLISH_STATUS_FILTER_GROUPS.items()
    }


@router.get("/ids")
def get_note_ids(
    platform: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = (
        select(Note.note_id)
        .where(Note.user_id == current_user.id)
    )
    if platform:
        statement = statement.where(Note.platform == platform)
    note_ids = db.scalars(statement).all()
    return {"items": list(note_ids)}


@router.get("")
def get_notes(
    platform: Optional[str] = None,
    q: Optional[str] = None,
    tag_id: Optional[int] = None,
    has_assets: Optional[bool] = None,
    has_comments: Optional[bool] = None,
    publish_status: str = "all",
    platform_account_id: Optional[int] = None,
    note_type: Optional[str] = None,
    item_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """内容库列表（设计 13 §32.3.3 发布状态筛选 + §25 账号 / 笔记类型筛选）。

    `publish_status` ∈ `all` / `pending` / `published` / `failed`（未知值 → 400
    `NOTE_STATUS_UNSUPPORTED`，**不猜枚举**，与商品列表的状态筛选同口径）。
    旧参数 `published`（布尔，两态）随 §32.3 一并删除 —— 「一份语义两份实现」
    的筛选参数正是这次要消掉的东西。

    2026-09-23 新增两个内容库筛选（用户口径「内容库新增根据账号筛选的条件」+「笔记类型」）：

    * `platform_account_id` —— **来源账号**。按 `Note.platform_account_id` 硬过滤，
      与界面「来源账号」列同一事实。**存在性不校验**：软删账号的笔记照常按 id 查得到
      （那是历史归属，不是「可选账号」）；用户 A 拿用户 B 的账号 id 来问，只会得到空列表
      （因为 `user_id` 那道 where 始终在），**不返回 404** —— 不泄漏「这个 id 存不存在」。
    * `note_type` ∈ `Note.note_type` 的合法取值（`synced` / `local` / `local_generated`）；
      未知值 → 400 `NOTE_TYPE_UNSUPPORTED`（与 `publish_status` 同口径：白名单 + 支持列表）。
      不传 = 不筛。
    * `item_id` —— **挂链商品**（平台商品 ID）。走 `note_goods` join `shop_products`
      子查询（与 `has_assets` 同款形态，不引入 JOIN+GROUP-BY）。**存在性不校验**：
      `user_id` 那道 where 始终在，别人的 / 不存在的 item_id 只会得到空集，不 404
      （与 `platform_account_id` 同一理由，设计 13 §25.8）。不传 = 不筛。
    """
    statement = (
        select(Note)
        .where(Note.user_id == current_user.id)
    )
    if platform:
        statement = statement.where(Note.platform == platform)
    if q:
        keyword = f"%{q.strip()}%"
        statement = statement.where(
            or_(
                Note.title.ilike(keyword),
                Note.content.ilike(keyword),
                Note.author_name.ilike(keyword),
                Note.note_id.ilike(keyword),
            )
        )
    if tag_id is not None:
        tag = db.get(Tag, tag_id)
        if tag is None or tag.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
        statement = statement.where(Note.id.in_(select(note_tags.c.note_id).where(note_tags.c.tag_id == tag_id)))
    if has_assets is True:
        statement = statement.where(Note.id.in_(select(NoteAsset.note_id)))
    elif has_assets is False:
        statement = statement.where(Note.id.not_in(select(NoteAsset.note_id)))
    if has_comments is True:
        statement = statement.where(Note.id.in_(select(NoteComment.note_id)))
    elif has_comments is False:
        statement = statement.where(Note.id.not_in(select(NoteComment.note_id)))
    # 发布状态筛选（四档，设计 13 §32.3.3）：**白名单校验**，未知值 400 不静默忽略；
    # 档 → 存储值的映射只有 `PUBLISH_STATUS_FILTER_GROUPS` 一份（统计用同一份）。
    status_key = (publish_status or "all").strip()
    if status_key != "all":
        if status_key not in PUBLISH_STATUS_FILTER_GROUPS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "NOTE_STATUS_UNSUPPORTED",
                    "message": f"不支持的发布状态筛选：{status_key}",
                    "supported": ["all", *PUBLISH_STATUS_FILTER_GROUPS.keys()],
                },
            )
        statement = statement.where(
            Note.publish_status.in_(PUBLISH_STATUS_FILTER_GROUPS[status_key])
        )
    # 来源账号筛选（2026-09-23）：直接打在 `platform_account_id` 上。
    # **不查账号表**（存在性 / 归属都不校验）—— 上面 `user_id` 那道 where 已是租户闸，
    # 「别人的账号 id ⇒ 空列表」比「404 告诉你这个 id 存在」更安全，也少一次查询。
    if platform_account_id is not None:
        statement = statement.where(Note.platform_account_id == platform_account_id)
    # 笔记类型筛选（2026-09-23，设计 13 §25）：白名单校验，未知值 400 不静默忽略；
    # 合法取值从模型常量取（**唯一定义处**），不在本文件重抄一份枚举。
    type_key = (note_type or "").strip()
    if type_key:
        if type_key not in NOTE_TYPE_FILTER_VALUES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "NOTE_TYPE_UNSUPPORTED",
                    "message": f"不支持的笔记类型筛选：{type_key}",
                    "supported": list(NOTE_TYPE_FILTER_VALUES),
                },
            )
        statement = statement.where(Note.note_type == type_key)
    # 挂链商品筛选（2026-09-23，设计 13 §25.8）：按 note_goods 关系表反查
    # 「挂了这件商品的笔记」。子查询形态与 has_assets 一致 —— 分页 total 那层
    # 已经包了一层计数，JOIN+GROUP-BY 会让 total 把关系行数当成笔记数（REF §E）。
    # item_id 是平台商品 ID（shop_products.item_id），租户闸由外层 user_id where
    # 保证；查不到 = 空集，不校验存在性、不 404。
    item_key = (item_id or "").strip()
    if item_key:
        statement = statement.where(
            Note.id.in_(
                select(NoteGoods.note_id)
                .join(ShopProduct, ShopProduct.id == NoteGoods.product_id)
                .where(ShopProduct.item_id == item_key)
            )
        )
    notes = db.scalars(statement.order_by(Note.created_at.desc())).all()
    result = paginated([_serialize_note_with_tags(db, note) for note in notes], page, page_size)
    # 统计与筛选是两件事：`stats` 是全库口径，不随上面任一 `where` 变化。
    result["stats"] = _publish_status_stats(db, current_user.id, platform)
    return result


@router.post("/batch-create-drafts")
def batch_create_drafts(
    payload: BatchCreateDraftsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    notes = _get_unique_owned_notes(db, current_user, payload.note_ids)
    drafts: list[AiDraft] = []
    for note in notes:
        draft = AiDraft(
            user_id=current_user.id,
            platform=note.platform,
            title=note.title,
            body=note.content,
            source_note_id=note.id,
        )
        db.add(draft)
        drafts.append(draft)

    db.commit()
    for draft in drafts:
        db.refresh(draft)

    return {
        "created_count": len(drafts),
        "items": [_serialize_draft(draft) for draft in drafts],
    }


@router.get("/{note_id}")
def get_note(
    note_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """笔记详情。**只读本地库，零平台请求**（2026-09-22 用户口径）。

    ⚠️ ~~`hydrate=true` 按需打平台补齐~~ **已被移除**（原说明保留）：
    三个源的列表接口都不带正文、全文只有详情接口有，所以曾在这里开过一条
    「打开详情就地补一次」的路径。**它直接导致了 500**：`db.commit()` 会与
    正在跑的同步任务抢 SQLite 写锁（`database is locked`，实测 0.00s 失败、
    未捕获 ⇒ 冒泡成 500），而用户看到的是「内容库打不开」。

    内容库的定位是**本地台账**（用户 2026-09-22：「内容库应查询的均是本地数据」）——
    一个只读页面不该有平台副作用，更不该因为平台侧的锁竞争整个打不开。
    正文与图集的补齐现在**只由同步链路**做（`_hydrate_pending_notes`，有界 +
    跨轮累积，设计 13 §19.13）；本地暂时没有正文的行，前端按缺值口径安静说明。
    """
    note = _get_owned_note(db, current_user, note_id)
    return _serialize_note_with_tags(db, note)


def purge_note(db: Session, note: Note) -> None:
    """删一条笔记**及其全部从属行**（标签 / 资产 / 评论 / 商品关联 / 草稿引用）。

    ⚠️ **唯一的删除链实现**（§24 起对账也要删行）：手工删除与对账删除必须是
    同一份代码，否则某一边漏清关联行就会留下孤儿数据。调用方负责 commit。

    ⚠️ 2026-09-23 用户口径「删除笔记时需要删除笔记与商品的关联关系」⇒ 补上
    **`note_goods` 关系行**（§26：`note_goods` 是笔记↔商品的多对多关系表）。
    漏清它的实际后果（真实库已复现）：删掉 4 条本地生成笔记后留下 4 条孤儿关系行
    （note_id 1963/1964/1965/1967 → product_id 1）。它们**不参与任何列表口径**，
    于是「挂链笔记数」之类的关系表计数**不会**被它们污染 —— 但每次 join 都要多扫
    几行、且数据本身自相矛盾（关系指向一条不存在的笔记）。
    ⚠️ 与**商品台账删除**的口径刻意不同（§33.10）：删商品时关系行**不级联删**
    （商品被同步写回时关系要原样接上）；删笔记时关系行**必须删**（笔记是真删，
    没有「写回时接上」这回事）。同一个关系表、两个方向、两种正确处理。
    """
    db.execute(delete(note_tags).where(note_tags.c.note_id == note.id))
    db.execute(delete(NoteAsset).where(NoteAsset.note_id == note.id))
    db.execute(delete(NoteComment).where(NoteComment.note_id == note.id))
    # 商品关联关系（§26）：笔记没了，指向它的多对对关系行一并删。
    db.execute(delete(NoteGoods).where(NoteGoods.note_id == note.id))
    # 草稿只解除引用、不连坐：AI 草稿是用户资产，笔记没了它还在（§12）。
    db.query(AiDraft).filter(AiDraft.source_note_id == note.id).update({"source_note_id": None})
    db.delete(note)


@router.delete("/{note_id}")
def delete_note(
    note_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    note = _get_owned_note(db, current_user, note_id)
    purge_note(db, note)
    db.commit()
    return {"id": note_id, "status": "deleted"}


@router.patch("/{note_id}/publish-status")
def update_note_publish_status(
    note_id: int,
    payload: UpdateNotePublishStatusRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """手动把一条笔记标为「已发布过」或「待发布」（设计 13 §19.12；§32.3.3 收敛）。

    **用户直接改发布状态的唯一入口**（系统侧的入口是同步链路 / 发布链路，
    两条都经 `set_note_publish_status()` 写同一列，不各存一份状态）。

    ⚠️ **本地生成笔记不接受手改**（400 `NOTE_STATUS_MANAGED`）：它的状态由发布
    链路决定 —— 手改等于给「发没发出去」编一个与平台不一致的答案。

    幂等：重复提交同一个值不报错（前端是开关语义，重试很常见）。返回更新后的整行
    （含 `tags`），让前端**就地替换列表行**，不必整表刷新 —— 但 `stats` 会随之变化，
    前端需要自己重取一次列表拿新统计（本接口不返回 `stats`：它跟筛选条件无关，
    在这里返回反而会诱导前端把它当「当前视图的统计」用）。
    """
    note = _get_owned_note(db, current_user, note_id)
    if note.note_type == NOTE_TYPE_LOCAL_GENERATED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "NOTE_STATUS_MANAGED",
                "message": "本地生成笔记的发布状态由发布链路管理（待发布 → 已发布 / 发布失败），不能手动切换。",
            },
        )
    set_note_publish_status(note, payload.publish_status)
    db.commit()
    db.refresh(note)
    return _serialize_note_with_tags(db, note)


@router.post("/{note_id}/sync")
def sync_note(
    note_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """内容库「同步」按钮：用笔记 id 拉平台最新内容，更新落库（设计 13 §25.4）。

    **判据是 `note_supports_sync()`**（models/note.py，与前端 `canSyncNote` 同口径）：
    `synced` **或** `publish_status == "published"`。

    ⚠️ 2026-09-23 用户口径「**已发布的笔记也支持同步笔记最新的三方数据**」⇒ 把原来
    只认 `synced` 的闸门放宽到「发布过也算」。漏放的实际表现（真实库 id 1966：
    `local_generated` + `published`）：本地生成笔记发布成功后**类型不变**，旧判据
    于是即使用户在卡片上点到了同步按钮，也会拿到 400 —— 按钮在、功能不在。

    仍被拒的是**既不是 synced、也没发布过**的笔记（采集来的别人的笔记）：
    「拉平台最新内容」对它没有意义，按钮同样不渲染。

    与定时任务同步笔记逻辑**同源**：单条刷新走 `note_sync_service
    .sync_note_from_platform`（复用 `hydrate_note_detail` 唯一实现 + 同一把限流尺子）。

    **失败也回 200**：没网页会话 / 没有详情地址 / 平台异常都是「试一次的结果」，
    放在 `sync.status` + `sync.reason` 里由前端按三分法渲染（预期内安静一行、
    真异常红块摆原话），不伪装成接口错误。响应带**更新后的整行**，前端就地替换。
    """
    note = _get_owned_note(db, current_user, note_id)
    if not note_supports_sync(note):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "NOTE_NOT_SYNCABLE",
                "message": "只有三方同步笔记或已发布过的笔记支持同步",
            },
        )
    from backend.app.services import note_sync_service

    result = note_sync_service.sync_note_from_platform(db, note)
    db.refresh(note)
    return {"note": _serialize_note_with_tags(db, note), "sync": result}


@router.post("/{note_id}/publish")
def publish_note_endpoint(
    note_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """内容库「发布」按钮：把待发布的本地生成笔记发布到平台（设计 13 §31.5）。

    **仅本地生成笔记可用**（`note_type == "local_generated"` 且
    `publish_status ∈ {pending, failed}`）—— 采集 / 同步笔记不走本链路
    （它们本来就是平台已存在或别人的内容）。

    链路：下载媒体到临时文件夹 → 上传平台 → `note/publish` → 回写平台
    真实 note_id + 删临时目录。**失败也回 200**：门禁之外的一切失败
    （下载 / 上传 / 平台拒发）都是「试一次的结果」，落在
    `publish.ok=false` + `publish.error`（平台原文），前端把笔记状态
    改成「发布失败」并展示原因、支持重发 —— 不伪装成接口错误。

    二次确认在前端做（用户口径），本端点不重复拦。
    """
    from backend.app.services.note_publish_service import NotePublishError, publish_note

    try:
        result = publish_note(db, current_user.id, note_id, adapter_factory=adapter_factory)
    except NotePublishError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": error.code, "message": error.message},
        )
    note = db.get(Note, note_id)
    return {"note": _serialize_note_with_tags(db, note), "publish": result}


@router.post("/batch-publish")
def batch_publish_notes_endpoint(
    payload: BatchPublishNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """批量发布待发布笔记（设计 13 §31.5 步 8；顺序处理，单项失败不阻断）。

    请求体：`{"note_ids": [<notes.id>, …]}`（≤20，去重保序）。
    响应：`{total, succeeded, failed, results:[{note_id, ok, platform_note_id?, error?}]}`
    —— 每篇独立成败，前端按结果逐条更新卡片状态。
    """
    from backend.app.services.note_publish_service import batch_publish_notes

    result = batch_publish_notes(
        db, current_user.id, payload.note_ids, adapter_factory=adapter_factory, max_items=payload.max_items
    )
    return result


@router.put("/{note_id}/topics")
def update_note_topics(
    note_id: int,
    payload: UpdateNoteTopicsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """话题增删的**落库入口**（设计 13 §29.3）：整列替换 `topics_json`。

    内容库详情卡里「+ 新增话题 / Tag 上 × 删话题」都调这里 —— 每次增删一次写库
    （「实时更新数据库」），前端传增删后的完整列表，后端归一化后覆写。

    与平台写入方（同步链路 `hydrate_note_detail` / `_save_normalized_notes`，
    §28.2）写**同一列**，与 `published` 的「用户改 / 系统改同一列」同构；下次同步
    详情带回非空话题时会**以平台为准覆盖**用户编辑（真值源是平台）。
    """
    note = _get_owned_note(db, current_user, note_id)
    note.topics_json = json.dumps(_normalize_topics(payload.topics), ensure_ascii=False)
    db.commit()
    db.refresh(note)
    return _serialize_note_with_tags(db, note)


@router.get("/{note_id}/assets")
def get_note_assets(
    note_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    note = _get_owned_note(db, current_user, note_id)
    assets = db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id).order_by(NoteAsset.sort_order.asc(), NoteAsset.id.asc())).all()
    return paginated([_serialize_asset(asset) for asset in assets], page, page_size)


class AddNoteAssetRequest(BaseModel):
    asset_type: str = Field(pattern="^(image|video)$")
    url: str = Field(default="", max_length=2048)
    local_path: str = Field(default="", max_length=512)


@router.post("/{note_id}/assets")
def add_note_asset(
    note_id: int,
    payload: AddNoteAssetRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    note = _get_owned_note(db, current_user, note_id)
    if not payload.url and not payload.local_path:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="url or local_path is required")
    asset = NoteAsset(
        note_id=note.id,
        asset_type=payload.asset_type,
        url=payload.url,
        local_path=payload.local_path,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return _serialize_asset(asset)


@router.delete("/{note_id}/assets/{asset_id}")
def delete_note_asset(
    note_id: int,
    asset_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    note = _get_owned_note(db, current_user, note_id)
    asset = db.scalars(
        select(NoteAsset).where(NoteAsset.id == asset_id, NoteAsset.note_id == note.id)
    ).first()
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    db.delete(asset)
    db.commit()
    return {"id": asset_id, "status": "deleted"}


class ReorderAssetsRequest(BaseModel):
    asset_ids: list[int] = Field(min_length=1)


@router.put("/{note_id}/assets/reorder")
def reorder_note_assets(
    note_id: int,
    payload: ReorderAssetsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    note = _get_owned_note(db, current_user, note_id)
    assets = db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id)).all()
    asset_map = {a.id: a for a in assets}
    for idx, aid in enumerate(payload.asset_ids):
        if aid in asset_map:
            asset_map[aid].sort_order = idx
    db.commit()
    return {"ok": True}


@router.get("/{note_id}/comments")
def get_note_comments(
    note_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    note = _get_owned_note(db, current_user, note_id)
    comments = db.scalars(
        select(NoteComment).where(NoteComment.note_id == note.id).order_by(NoteComment.id.asc())
    ).all()
    return paginated([_serialize_comment(comment) for comment in comments], page, page_size)


def _download_asset(url: str, user_id: int, asset_type: str) -> str | None:
    from backend.app.services.asset_downloader import download_asset_to_local
    return download_asset_to_local(url, user_id, asset_type)


@router.post("/batch-save")
def batch_save_notes(
    payload: BatchSaveNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_xhs_pc_api_adapter_factory),
):
    account = _get_owned_account(db, current_user, payload.account_id)
    comment_adapter = None
    if payload.fetch_comments:
        if account.sub_type != "pc":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PC account is required to fetch comments")
        comment_adapter = adapter_factory(_get_latest_account_cookies(db, account))
    saved_notes: list[Note] = []

    for note_payload in payload.notes:
        existing = db.scalars(
            select(Note).where(
                Note.user_id == current_user.id,
                Note.note_id == note_payload.note_id,
            )
        ).first()
        if existing is None:
            existing = Note(
                user_id=current_user.id,
                platform_account_id=account.id,
                platform=account.platform,
                note_id=note_payload.note_id,
            )
            db.add(existing)
        else:
            # 同 `_save_normalized_notes`：覆盖更新时把归属重挂到当前账号（台账 §4.9 / P2），
            # 否则账号换行后这条笔记会一直指向已删除的账号。
            existing.platform_account_id = account.id

        existing.title = note_payload.title
        existing.content = note_payload.content
        existing.author_name = note_payload.author_name
        merged_raw = dict(note_payload.raw) if note_payload.raw else {}
        if note_payload.note_url:
            merged_raw["note_url"] = note_payload.note_url
        existing.raw_json = merged_raw
        db.flush()
        db.execute(delete(NoteAsset).where(NoteAsset.note_id == existing.id))
        image_candidates = [*note_payload.image_urls] or ([note_payload.cover_url] if note_payload.cover_url else [])
        unique_image_urls = [url for index, url in enumerate(image_candidates) if url and url not in image_candidates[:index]]
        for image_url in unique_image_urls:
            local_name = _download_asset(image_url, current_user.id, "image")
            db.add(NoteAsset(note_id=existing.id, asset_type="image", url=image_url, local_path=local_name or ""))
        video_url = note_payload.video_url or note_payload.video_addr
        if video_url:
            local_name = _download_asset(video_url, current_user.id, "video")
            db.add(NoteAsset(note_id=existing.id, asset_type="video", url=video_url, local_path=local_name or ""))
        if payload.fetch_comments and note_payload.note_url and comment_adapter is not None:
            success, message, raw_payload = comment_adapter.get_note_comments(note_payload.note_url)
            if not success:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=message or "XHS note comments failed",
                )
            db.execute(delete(NoteComment).where(NoteComment.note_id == existing.id))
            for comment in normalize_comment_payload(raw_payload):
                db.add(
                    NoteComment(
                        note_id=existing.id,
                        comment_id=comment["comment_id"],
                        user_name=comment["user_name"],
                        user_id=comment["user_id"],
                        content=comment["content"],
                        like_count=comment["like_count"],
                        parent_comment_id=comment["parent_comment_id"],
                        created_at_remote=comment["created_at_remote"],
                        raw_json=comment["raw_json"],
                    )
                )
        saved_notes.append(existing)

    db.commit()
    for note in saved_notes:
        db.refresh(note)

    return {
        "saved_count": len(saved_notes),
        "items": [_serialize_note_with_tags(db, note) for note in saved_notes],
    }


@router.post("/batch-tag")
def batch_tag_notes(
    payload: BatchTagNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    notes: list[Note] = []
    for note_id in dict.fromkeys(payload.note_ids):
        notes.append(_get_owned_note(db, current_user, note_id))

    tag_ids = list(dict.fromkeys(payload.tag_ids))
    for tag_id in tag_ids:
        tag = db.get(Tag, tag_id)
        if tag is None or tag.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")

    for note in notes:
        if payload.mode == "replace":
            db.execute(delete(note_tags).where(note_tags.c.note_id == note.id))
            for tag_id in tag_ids:
                db.execute(note_tags.insert().values(note_id=note.id, tag_id=tag_id))
            continue

        if payload.mode == "add":
            existing_tag_ids = set(
                db.scalars(select(note_tags.c.tag_id).where(note_tags.c.note_id == note.id)).all()
            )
            for tag_id in tag_ids:
                if tag_id not in existing_tag_ids:
                    db.execute(note_tags.insert().values(note_id=note.id, tag_id=tag_id))
            continue

        if tag_ids:
            db.execute(
                delete(note_tags).where(
                    note_tags.c.note_id == note.id,
                    note_tags.c.tag_id.in_(tag_ids),
                )
            )

    db.commit()
    return {
        "updated_count": len(notes),
        "items": [_serialize_note_with_tags(db, note) for note in notes],
    }


@router.post("/export")
def export_notes(
    payload: ExportNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    notes = _get_unique_owned_notes(db, current_user, payload.note_ids)
    export_dir = Path(get_settings().storage_dir) / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    exported_at = shanghai_now()
    file_name = f"xhs-notes-u{current_user.id}-{exported_at.strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}.{payload.format}"
    file_path = export_dir / file_name
    if payload.format == "csv":
        file_path.write_text("\ufeff" + _build_notes_csv(db, notes), encoding="utf-8")
    else:
        export_payload = {
            "platform": "xhs",
            "format": payload.format,
            "exported_at": exported_at.isoformat(),
            "total": len(notes),
            "items": [_serialize_note_with_tags(db, note) for note in notes],
        }
        file_path.write_text(json.dumps(export_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "exported_count": len(notes),
        "file_name": file_name,
        "file_path": str(file_path.resolve()),
        "download_url": f"/api/files/exports/{file_name}",
    }
