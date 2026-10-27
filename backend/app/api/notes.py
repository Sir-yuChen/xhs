from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, Sequence
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from backend.app.api.platforms.xhs.pc import (
    get_xhs_pc_api_adapter_factory,
    normalize_comment_payload,
)
from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory
from backend.app.core.config import get_settings
from backend.app.core.database import SessionLocal, get_db
from backend.app.core.deps import get_current_user, get_owned_active_account
from backend.app.core.time import shanghai_now
#: ⚠️ 设计 75 S5（2026-10-27）：`NoteGoods` / `ShopNoteGoods` 两个 ORM 类随「笔记↔商品挂链改实时」
#: 退场（两张表已 DROP），不再 import。
from backend.app.models import AiDraft, Note, NoteAsset, NoteComment, NoteDatacenterStat, NoteTopic, Notification, PlatformAccount, Topic, User
from backend.app.services.note_purge_service import purge_note
from backend.app.models.scheduled_job import (
    LOG_FAILED,
    LOG_RUNNING,
    LOG_SUCCESS,
    ScheduledJob,
    ScheduledJobLog,
)
from backend.app.models.note import (
    NOTE_TYPE_LOCAL,
    NOTE_TYPE_LOCAL_GENERATED,
    NOTE_TYPE_SYNCED,
    LIBRARY_EXCLUDED_PUBLISH_STATUSES,
    LIBRARY_NOTE_TYPES,
    PUBLISH_STATUS_FAILED,
    PUBLISH_STATUS_FILTER_GROUPS,
    PUBLISH_STATUS_PENDING,
    PUBLISH_STATUS_PUBLISHED,
    PUBLISH_STATUS_PUBLISHING,
    set_note_publish_status,
)
from backend.app.schemas.common import paginated
from backend.app.services.note_topic_service import replace_note_topics

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
    #: 话题字典行 id（`topics.id`）—— 2026-09-25 起标签表已并入话题表（§34.4），
    #: 字段名沿用 `tag_ids` 保持 API 兼容，语义 = topic ids。
    tag_ids: list[int] = Field(default_factory=list)
    mode: Literal["replace", "add", "remove"] = "replace"


class BatchCreateDraftsRequest(BaseModel):
    note_ids: list[int] = Field(min_length=1)
    intent: str = Field(default="rewrite", max_length=32)




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




#: 话题归一化上限（NOTE_TOPICS_MAX_ITEMS / NOTE_TOPICS_MAX_LEN）：**唯一定义处**在
#: `services/note_topic_service.py`（本文件顶部 import 复用，不重抄字面量 ——
#: 否则「后端两份归一化」必然漂移，DEF-044/047/058 同款）。


class UpdateNoteTopicsRequest(BaseModel):
    """话题整列替换（设计 13 §29.3 / §34.4）：内容库话题增删的唯一落库入口。

    **整列替换语义**：调用方传增删后的**完整列表**，后端不猜「加了哪个删了哪个」——
    猜就是造第二份真相源（DEF-044/058 的教训）。归一化见
    `note_topic_service.normalize_topic_names`（唯一实现）。
    """

    topics: list[str] = Field(default_factory=list, max_length=200)


def _serialize_topic(topic: Topic) -> dict:
    """话题行 ⇒ 序列化（`{id, name, color}`；原 `_serialize_tag` 的替代）。"""
    return {
        "id": topic.id,
        "name": topic.name,
        "color": topic.color,
    }


@dataclass
class _NoteRelations:
    """一批笔记的**关联数据**（设计 13 §41.4）：assets / topics / goods / account。

    `_load_note_relations()` 的返回类型。前三个字典以 `notes.id` 为键，
    `accounts` 以 `platform_account_id` 为键。**缺键 = 该项为空**（不是错误，
    与逐篇版的「空 = 没挂 / 没补」同口径）。
    """

    #: `notes.id` → 该篇的素材（按 `sort_order, id`）
    assets: dict[int, list[NoteAsset]] = field(default_factory=dict)
    #: `notes.id` → 该篇的话题（`[{id, name, color}]`，按 `NoteTopic.id`）
    topics: dict[int, list[dict]] = field(default_factory=dict)
    #: `notes.id` → 该篇的挂载商品（按 `NoteGoods.id`）
    goods: dict[int, list[dict]] = field(default_factory=dict)
    #: `platform_accounts.id` → 账号行（取昵称用）
    accounts: dict[int, PlatformAccount] = field(default_factory=dict)


def _load_note_relations(db: Session, notes: Sequence[Note]) -> _NoteRelations:
    """**关联数据的唯一加载实现**（设计 13 §41.4）：一次 `IN (…)` 取回整批。

    ⚠️ **单篇不是另一份实现**：详情 / 导出 / batch 回执都调
    `_load_note_relations(db, [note])` —— `len == 1` 时 `IN (…)` 与 `==` 等价，
    走的是同一个函数体（DEF-044：一个语义一份实现）。

    为什么必须批量：改前 `_serialize_note` 对**每一篇**各发 4 次查询
    （assets / topics / goods / account）⇒ 一页 20 篇 = 80+ 条 SQL；真库 3620 篇时
    列表端点实测 **16390 条 / 5.7 秒**（只返回 20 条）。这里改成每张表一次 `IN`，
    实测 **7 条 / 15.5 ms**。

    ⚠️ **顺序是契约的一部分**（前端按落库顺序渲染图集与话题 chips）——
    下面的 ORDER BY 与改前逐篇版**逐字相同**，不要「顺手」改成别的排序。
    """
    relations = _NoteRelations()
    ids = [note.id for note in notes]
    if not ids:
        return relations

    # 素材（`note_assets.note_id` 是**本地主键**）
    for asset in db.scalars(
        select(NoteAsset)
        .where(NoteAsset.note_id.in_(ids))
        .order_by(NoteAsset.sort_order.asc(), NoteAsset.id.asc())
    ).all():
        relations.assets.setdefault(asset.note_id, []).append(asset)

    # 话题（§34.1：`note_topics` 关系表 join `topics` 字典表；关系行 id 序 = 落库序）
    for note_id, topic in db.execute(
        select(NoteTopic.note_id, Topic)
        .join(Topic, Topic.id == NoteTopic.topic_id)
        .where(NoteTopic.note_id.in_(ids))
        .order_by(NoteTopic.id.asc())
    ).all():
        relations.topics.setdefault(note_id, []).append(_serialize_topic(topic))

    # 挂载商品（§26.7：关系表两侧都是**平台 ID** ⇒ 用 `note.note_id` 查）
    relations.goods.update(_load_batch_goods(db, notes))

    # 来源账号（取昵称；软删账号照常回传 —— 那是这条笔记的历史归属，不是「可选账号」）
    account_ids = {note.platform_account_id for note in notes if note.platform_account_id}
    if account_ids:
        for account in db.scalars(
            select(PlatformAccount).where(PlatformAccount.id.in_(account_ids))
        ).all():
            relations.accounts[account.id] = account

    return relations


def _load_batch_goods(db: Session, notes: Sequence[Note]) -> dict[int, list[dict]]:
    """这批笔记的挂载商品 —— ⚠️ 设计 75 S5 起恒为空（不查库、不查平台）。

    ## 为什么不再在这里查

    | 原实现 | 现状 |
    |---|---|
    | 读 `note_goods` 关系表 | 表已 DROP（迁移 `n8o9p0q1r2s3`） |
    | 读 `shop_note_goods` 补规格 | 同上 |
    | 读 `shop_products` 取标题 / 价格 | 同上（商品改实时查平台） |

    ## 为什么不改成「一页 20 篇逐篇去平台查」

    平台只有「按**商品**反查笔记」（`item_mix_search_word`）和「按**笔记 ID** 批量查
    挂链」（`note_ids`）。后者其实可用 —— 但它与本函数服务的内容库列表的候选集合并
    不完全一致（本地笔记包含**还没发布**的本地生成笔记，平台上根本不存在），为了让
    「列表每篇都带商品」而去打一次平台，换来的是一页最多 1 次请求的固定成本 + 一处
    失败面，而列表页的主要信息是笔记本身。

    ⇒ 挂载商品在**需要它的地方**实时查：
    * 「我的笔记」列表 / 详情 → `_attach_shop_goods`（按本页 `note_ids` 一次实时查回）；
    * 单篇商品详情 → `GET /notes/goods/{item_id}`（实时）。

    调用方拿到空字典即渲染成「无挂载商品」—— 与改造前「关系表里没有这行」**同一形状**，
    前端零改动。
    """
    return {}


def _goods_entry(product: Any, item_id: Any, variant: str = "") -> dict:
    """一条挂载商品的序列化（**唯一实现**）。

    ⚠️ 设计 75 S5（2026-10-27）：`product` 现在是**实时查询结果**（平台归一化 dict），
    不再是本地台账行；`None` = 平台上取不到这件商品（可能已删除）。
    两种情况的字段形状**完全一致**：取不到时标题 / 价格 / 图 / 库存留空
    （缺值口径，前端渲染「（无商品标题）」「价格待同步」），**不造假**。
    """
    item_key = str(item_id or "")
    if not product:
        return {
            "item_id": item_key,
            "item_title": "",
            # 价格是**分**的字符串（列名即口径，展示层换算成元）。
            "price": "",
            "images": [],
            "stock": None,
            "sales": None,
            "variant": variant,
        }
    price_min = product.get("price_min_fen")
    price_max = product.get("price_max_fen")
    if price_min is None and price_max is None:
        price = ""
    elif price_max is None or price_max == price_min:
        price = str(price_min)
    else:
        price = f"{price_min}~{price_max}"
    return {
        "item_id": str(product.get("item_id") or item_key),
        "item_title": str(product.get("title") or ""),
        "price": price,
        "images": [str(url) for url in (product.get("images") or []) if url],
        "stock": product.get("stock"),
        "sales": product.get("sales_total"),
        "variant": variant,
    }


def _note_goods(db: Session, note: Note) -> list[dict]:
    """（薄包装）**唯一实现见 `_load_note_relations`** —— 单篇 = 长度为 1 的批量。

    保留这个函数名只为既有调用点（`tests/backend/test_note_goods.py` 直接 import 它）
    不断裂；**函数体只有这一行转发**，不许在别处再抄一遍「`note_goods` join
    `shop_products`」的查法（DEF-044：一个语义两份实现）。
    """
    return _load_note_relations(db, [note]).goods.get(note.id, [])


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


def _serialize_note(db: Session, note: Note, relations: Optional[_NoteRelations] = None) -> dict:
    """笔记 ⇒ 序列化（列表 / 详情 / 导出 JSON 三处的**同一份**输出）。

    `relations`（设计 13 §41.4）：调用方**已预取**的关联数据。列表端点整页传一份
    （`_load_note_relations(db, rows)`）⇒ 序列化过程零额外查询；不传则内部对
    `[note]` 调**同一个** `_load_note_relations` —— **不是两份实现**，
    单篇只是「长度为 1 的批量」。
    """
    rel = relations if relations is not None else _load_note_relations(db, [note])
    assets = rel.assets.get(note.id, [])
    image_assets = [asset for asset in assets if asset.asset_type == "image"]
    video_assets = [asset for asset in assets if asset.asset_type == "video"]
    asset_urls = [_asset_display_url(asset) for asset in assets if asset.url or asset.local_path]
    raw = note.raw_json if isinstance(note.raw_json, dict) else {}
    raw_cover = raw.get("cover_url") if isinstance(raw.get("cover_url"), str) else ""
    # 来源账号（2026-09-22）：同步落库的笔记要能看出**它属于哪个账号** ——
    # 「已发布」这个标识本身就是账号维度的事实，界面不给账号就无从核对。
    # 软删账号的昵称**照常回传**：那是这条笔记的历史归属（事实），不是「可选账号」；
    # 昵称取不到时留空，前端按缺值口径不展示（硬约束⑤）。
    account = rel.accounts.get(note.platform_account_id) if note.platform_account_id else None
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
        # 话题（设计 13 §34）：走 `note_topics` 关系表 join `topics` 字典表
        # （`[{id, name, color}]`；`topics_json` 列已删，关系表是唯一读取路径）。
        # 空 = 这条笔记没有话题（不是错误）。取自 `relations`（§41.4 批量预取）。
        "topics": rel.topics.get(note.id, []),
        # 挂载商品（设计 13 §28.3）：走 `note_goods` join `shop_products` 查，
        # **不在笔记表存商品快照**；空 = 没挂商品。取自 `relations`（§41.4）。
        "goods": rel.goods.get(note.id, []),
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


def _serialize_note_with_topics(
    db: Session, note: Note, relations: Optional[_NoteRelations] = None
) -> dict:
    """序列化 + 话题（话题已在 `_serialize_note` 内 join，本函数保留为**统一入口名**）。

    ⚠️ 2026-09-25 前叫 `_serialize_note_with_topics`（序列化后再挂 `tags` 字段）——
    标签表并入话题表（§34.4）后 `tags` 字段已移除，函数随之改名；
    `topics` 改由 `_serialize_note` 一次带出（少一次 join 的重复）。

    `relations`（§41.4）：列表端点整页传一份预取结果；不传 = 单篇自取（同一实现）。
    """
    return _serialize_note(db, note, relations)




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

    **口径 = 内容库可见范围**（2026-10-27 修订）：与列表同一把尺子 ——
    `is_deleted=False` + `note_type ∈ LIBRARY_NOTE_TYPES`。**不叠加** `q` /
    `topic_id` / `has_assets` / `has_comments` / `publish_status` 任一**用户筛选**
    （它服务的是「内容池结构」这一眼，跟着筛选走会读不出池子结构）。

    ⚠️ 为什么必须带这两道（改前只按 `user_id`+`platform`，是**全库**口径）：
    列表已经只放「本地 + 未发布」，软删除的行也不显示；统计卡若仍算全库，就会
    把已发布 / 三方同步 / 软删的行都数进去 —— 三数之和 ≠ 列表能看到的总数，
    用户会以为「卡片说有 300 篇，列表只有 48 篇」。

    ⚠️ `已发布` 在这个口径下**通常为 0**，这是**正确**的：内容库按定义不放已发布
    笔记（它们发布成功即软删除），已发布的去「我的笔记」看实时数据。

    三数之和 = 内容库可见笔记总数（迁移 `v5w6x7y8z9a0` 之后不存在第四态；
    未回填的空值 `""` 归入「待发布」档，与筛选同一份映射 `PUBLISH_STATUS_FILTER_GROUPS`）。
    """
    base = (
        select(Note.publish_status, func.count())
        .select_from(Note)
        .where(
            Note.user_id == user_id,
            Note.is_deleted.is_(False),
            Note.note_type.in_(LIBRARY_NOTE_TYPES),
        )
    )
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
    """已保存笔记的平台 `note_id` 清单（笔记发现页标「已保存」用）。

    ⚠️ **口径必须与内容库一致**（2026-10-27）：排除软删 + 只取内容库收录的类型。
    否则会标「已保存」而内容库里看不到 —— 发布成功后被软删的、三方同步来的
    都会这样，用户再点一次保存才在列表里出现，等于告诉他刚才那个标记是假的。
    """
    statement = (
        select(Note.note_id)
        .where(
            Note.user_id == current_user.id,
            Note.is_deleted.is_(False),
            Note.note_type.in_(LIBRARY_NOTE_TYPES),
        )
    )
    if platform:
        statement = statement.where(Note.platform == platform)
    note_ids = db.scalars(statement).all()
    return {"items": list(note_ids)}


@router.get("/publish-queue")
def get_publish_queue(
    platform: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """发布进度快照（内容库「发布进度卡」；2026-09-25 异步 + 队列）。

    两件事一次给全：① **进程内发布队列**（`running` 正在发布的那篇 / `queued`
    排队中的，FIFO 序，按归属过滤）；② **四态计数**（pending / publishing /
    published / failed，按存储值原样计数 —— 与筛选档统计 `_publish_status_stats`
    是两件事：那个把 `publishing` 归并进「待发布」档，这个不归并）。

    前端在「有发布中 / 排队」时每几秒轮询一次；队列空则停（ counts 随列表刷新。
    ⚠️ 路由必须注册在 `/{note_id}` **之前** —— 否则 "publish-queue" 会被
    `/{note_id}` 的 int 转换先吃掉，422。
    """
    from backend.app.services.note_publish_service import publish_queue_snapshot

    snapshot = publish_queue_snapshot(db, current_user.id, platform=platform)
    snapshot["counts"] = _publish_status_counts(db, current_user.id, platform)
    return snapshot


# --------------------------------------------------------------------------- #
# 笔记同步：**整段退场**（设计 75 §4.5，2026-10-27，S4）
#
# 内容库改**读平台实时**后，「把平台笔记翻页落进本地 `notes` 表」不再有任何数据
# 消费者 ⇒ 同步入口 + 进度查询 + 队列快照 4 条端点同批改 **410 墓碑**
# （`include_in_schema=False`：不进 OpenAPI，端点计数只算真实能力 394 → 387）。
#
# ⚠️ **标记常量只此一处**：`NOTE_SYNC_REMOVED` 是四条墓碑共用的错误码，
# 前端据此把「已下线」与真故障区分开（不猜 404）。
# ⚠️ 墓碑路由**不挂权限码、也不进 `ROUTE_PERMS`** —— 登记了就是
# 「指向不存在路由的码」，启动守卫 `[码指向不存在的路由]` 会拒绝启动。
# --------------------------------------------------------------------------- #
NOTE_SYNC_REMOVED = "NOTE_SYNC_REMOVED"

_NOTE_SYNC_REMOVED_MESSAGES = {
    "queue": "「同步队列」已随内容库实时查询下线：内容库直接读平台最新数据，"
             "不再有同步任务在排队。",
    "batch": "「批量同步」已随内容库实时查询下线：内容库直接读平台最新数据，"
             "不需要再同步到本地。",
    "single": "「同步笔记」已随内容库实时查询下线：内容库直接读平台最新数据，"
              "不需要再同步到本地。",
    "data": "「同步数据」已随内容库实时查询下线：内容库直接读平台最新数据，"
            "不需要再同步到本地。",
}


def _note_sync_gone(what: str) -> None:
    """四条墓碑共用的抛出体（同一句口径，避免四处文案分叉）。"""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={"code": NOTE_SYNC_REMOVED, "message": _NOTE_SYNC_REMOVED_MESSAGES[what]},
    )


@router.get("/sync-queue", include_in_schema=False)
def get_sync_queue_removed():
    """**已退场**（设计 75 §4.5，2026-10-27）—— 原批量同步的进度快照。

    同步链路本身已退场，队列快照自然没有意义（前端也不再轮询它）。
    """
    _note_sync_gone("queue")


# ⚠️ **`GET /notes/hydrate-queue` 已移除**（2026-09-27 用户口径：账号卡移除「补齐详情」，
# 补齐能力并入「同步笔记」任务本身，设计 13 §35.18）。详情补齐队列（`note_hydrate`）
# 与它的入队端点 `POST /accounts/{id}/notes/hydrate-pending` 一并删除 —— 没有入口的
# 队列快照端点留着只会让人以为这条链路还在跑。


def _publish_status_counts(db: Session, user_id: int, platform: Optional[str]) -> dict:
    """四态**原始**计数（发布进度卡用）。

    与筛选档统计 `_publish_status_stats` 刻意分开：那个按
    `PUBLISH_STATUS_FILTER_GROUPS` 归并（`publishing` 归「待发布」档、空值归
    「待发布」），服务的是筛选下拉；这个按存储值原样计数，服务的是「发布进度」
    这一眼 —— 进度卡上「发布中」必须单独一档，不能并进待发布。
    """
    base = select(Note.publish_status, func.count()).select_from(Note).where(Note.user_id == user_id)
    if platform:
        base = base.where(Note.platform == platform)
    counts = {str(status or ""): int(total or 0) for status, total in db.execute(base.group_by(Note.publish_status)).all()}
    return {
        "pending": counts.get(PUBLISH_STATUS_PENDING, 0),
        "publishing": counts.get(PUBLISH_STATUS_PUBLISHING, 0),
        "published": counts.get(PUBLISH_STATUS_PUBLISHED, 0),
        "failed": counts.get(PUBLISH_STATUS_FAILED, 0),
    }


@router.get("")
def get_notes(
    platform: Optional[str] = None,
    q: Optional[str] = None,
    topic_id: Optional[int] = None,
    has_assets: Optional[bool] = None,
    has_comments: Optional[bool] = None,
    publish_status: str = "all",
    platform_account_id: Optional[int] = None,
    note_type: Optional[str] = None,
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
    * ~~`item_id`（挂链商品）~~ —— **2026-10-27 移除**（设计 75 S5）：
      挂链关系改实时查千帆，本地 `note_goods` 表已 DROP，参数整条下线。
      查「某商品挂了哪些笔记」走商品列表 → 该商品 → 挂链笔记。

    2026-09-25：`tag_id` ⇒ `topic_id`（§34.4，标签表并入话题表）—— 走的
    `note_topics` 关系表；归属校验照旧（404 不暴露「存在但无权」）。
    """
    statement = (
        select(Note)
        .where(Note.user_id == current_user.id, Note.is_deleted.is_(False))
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
    if topic_id is not None:
        # 话题筛选（§34.4）：原「标签筛选」—— 标签表已并入话题表，筛的是
        # `note_topics` 关系。归属校验照旧（404 不暴露「存在但无权」）。
        topic = db.get(Topic, topic_id)
        if topic is None or topic.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Topic not found")
        statement = statement.where(Note.id.in_(select(NoteTopic.note_id).where(NoteTopic.topic_id == topic_id)))
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
    #: 内容库 = **只放还没发布出去的本地笔记**（2026-10-27 用户口径
    #: 「只显示用户本地生成未发布的笔记，已发布的笔记不展示在内容库中，
    #: 应到我的笔记页查看实时数据」）。
    #:
    #: 为什么在端点里再兜一道、而不是只靠发布成功后删行：删行是**事后的**，
    #: 存量与旁路（手工改状态、老数据）仍可能留下已发布的本地行；库里放着
    #: 一条已发布笔记却出现在「待发布」的地方，用户会以为它还没发。
    #: 三方同步（`synced`）也不进内容库 —— 那个源已整条退场。
    statement = statement.where(
        Note.note_type.in_(LIBRARY_NOTE_TYPES),
        Note.publish_status.not_in_(LIBRARY_EXCLUDED_PUBLISH_STATUSES),
    )
    # ⚠️ `item_id`（按商品筛笔记）**参数已整条移除**（2026-10-27，设计 75 S5）。
    # 挂链关系改实时查千帆（`item_mix_search_word`），本地 `note_goods` /
    # `shop_note_goods` 两表已随迁移 `n8o9p0q1r2s3` DROP ⇒ 本地没有商品维度可筛。
    # 查「某商品挂了哪些笔记」走**商品列表 → 该商品 → 挂链笔记**。
    # 为什么删参数而不是返回 400：参数已不存在，FastAPI 会自动忽略未知 query，
    # 留一个恒 400 的分支只是死代码（调用点已同批删除）。
    # ------------------------------------------------------------------ #
    # 分页**下推到 SQL**（设计 13 §41.3）：改前是「取全部命中行 → 内存切片」，
    # 真库 3620 篇实测 5774 ms / 16390 条 SQL（只返回 20 条）。
    # 现在：① `count(*)` 单独一次（实测 0.1 ms，`ix_notes_user_id` 是 covering index）；
    #       ② 只取本页 `page_size` 行。
    # ⚠️ **`total` 必须仍是「命中总数」**（前端 `hasMore` 靠它推），不是本页条数。
    # ⚠️ **`Note.id.desc()` 是稳定排序键**（§41.5）：`created_at` 可能同值
    #    （批量落库 / 同一秒同步的多篇），单键排序下行序不确定 ⇒ 翻页漏行 / 重行，
    #    而前端按 id 去重（`loadMoreNotes`）会把漏掉的那些**静默吞掉**。
    # ------------------------------------------------------------------ #
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.scalars(
        statement.order_by(Note.created_at.desc(), Note.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    # 关联数据一次预取（§41.4）：整页的 assets / topics / goods / account 各一条 `IN`。
    # 不预取的话，`_serialize_note` 会对每一篇各查 4 次（N+1，改前的形态）。
    relations = _load_note_relations(db, rows)
    return {
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "items": [_serialize_note_with_topics(db, note, relations) for note in rows],
        # 统计与筛选是两件事：`stats` 是全库口径，不随上面任一 `where` 变化。
        "stats": _publish_status_stats(db, current_user.id, platform),
    }


@router.get("/goods/{item_id}")
def get_note_goods_detail(
    item_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**按商品 ID 查商品详情**（数据中心「商品名称」点击弹窗用，2026-10-12）。

    ⚠️ 设计 75 S5（2026-10-27）：数据源由本地 `shop_products` 台账改为
    **千帆实时查询**（`rt.list_products(keyword=<商品ID>)` + 精确匹配）。
    本地台账已 DROP，`variant`（规格）也不再随行下发 —— 规格在商品详情的
    SKU 面里（`POST /xhs/qianfan/item-detail`），本端点只回答「这是什么商品」。

    **归属**：实时查询必须带账号（平台按账号鉴权）⇒ 取该商品的归属账号
    = 调用方**自己的**账号里 `qianfan_item_id == item_id` 那条；没有就退回
    「该用户第一个有 ark 会话的账号」。都没有 ⇒ 仍然返回缺值条目（只有
    item_id），**不造假**（与改造前「本地没有这件商品」同一口径）。

    ⚠️ 路由必须在 `/{note_id}` 之前 —— item_id 是平台商品 ID（可能是纯数字），
    排在 `/{note_id}`（int）之后会被抢先匹配。
    """
    product = None
    account = _item_owner_account(db, current_user.id, item_id)
    if account is not None:
        from backend.app.services import realtime_platform_query_service as rt

        try:
            adapter = _realtime_adapter_for_account(db, account)
            #: ⚠️ 平台 `search_item_v2` 的 `status` 缺省是 `on_shelf` ⇒ 已下架商品会搜不到；
            #: 这里显式 `"all"`（本端点是「按 ID 认商品」，不该被上架状态过滤掉）。
            result = rt.list_products(
                adapter, keyword=item_id, status="all", page=1, page_size=20
            )
            for row in result.get("items") or []:
                if str(row.get("item_id") or "") == str(item_id or ""):
                    product = row
                    break
        except Exception:  # noqa: BLE001 - 取不到就如实返回缺值条目
            product = None
    return _goods_entry(product, item_id, "")


def _item_owner_account(db: Session, user_id: int, item_id: str) -> Optional[PlatformAccount]:
    """这件商品最可能属于哪个账号（**本地已知信息，零平台请求**）。

    两个判据按优先级：① 本地笔记里记过这件商品的账号（`notes.raw_json.item_id`
    不可查，故改用「数据中心统计表的 item_id」—— 商业笔记统计行带平台商品 ID）；
    ② 兜底：该用户第一个有 ark 会话的账号（千帆商品只可能是它的）。
    """
    from backend.app.services.shop_binding_service import has_ark_session

    key = str(item_id or "").strip()
    if not key:
        return None
    candidates = db.scalars(
        select(PlatformAccount)
        .where(PlatformAccount.user_id == int(user_id), PlatformAccount.deleted_at.is_(None))
        .order_by(PlatformAccount.id.asc())
    ).all()
    for account in candidates:
        try:
            if has_ark_session(db, account.id):
                return account
        except Exception:  # noqa: BLE001 - 判据读不到就换下一个
            continue
    return None


def _realtime_adapter_for_account(db: Session, account: PlatformAccount):
    """按账号取千帆适配器（**实时查询唯一取法**，与 `qianfan` 端点同一门禁）。

    ⚠️ 与 `qianfan/goods.py::_realtime_adapter` 是同一判据的两处落地：那边是
    「端点 + 用户 + 账号」，这边只有「账号」（本端点已在外层校验归属）。
    取不到凭据 ⇒ 抛 400 `ARK_SESSION_REQUIRED`（与那边同码，前端分流逻辑共用）。
    """
    from backend.app.services.shop_binding_service import qianfan_cookies_for_account

    cookies = qianfan_cookies_for_account(db, account.id)
    if not cookies:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "ARK_SESSION_REQUIRED",
                "message": "该账号没有可用的千帆（ark）登录态，无法实时查询商品。",
            },
        )
    return get_qianfan_api_adapter_factory()(cookies)


@router.get("/offline")
def get_offline_notes(
    category: Optional[str] = None,
    platform_account_id: Optional[int] = None,
    keyword: Optional[str] = None,
    note_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = Query(None, pattern="^(asc|desc)$"),
    published_from: Optional[int] = None,
    published_to: Optional[int] = None,
    deleted: Optional[str] = Query(None, pattern="^(all|deleted|active)$"),
    read_from: Optional[float] = None,
    read_to: Optional[float] = None,
    like_from: Optional[float] = None,
    like_to: Optional[float] = None,
    collect_from: Optional[float] = None,
    collect_to: Optional[float] = None,
    comment_from: Optional[float] = None,
    comment_to: Optional[float] = None,
    share_from: Optional[float] = None,
    share_to: Optional[float] = None,
    pay_gmv_from: Optional[float] = None,
    pay_gmv_to: Optional[float] = None,
    pay_order_from: Optional[float] = None,
    pay_order_to: Optional[float] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """「数据中心」笔记列表（查本地统计表，零平台请求）。

    数据源是 `note_datacenter_stats` 统计表（千帆数据中心离线落库）。
    `category` 可选（`goods` / `normal`），映射到 `note_flag`
    （`goods` → `goods_note` / `normal` → 非 goods_note）；未知值 400
    `NOTE_CATEGORY_UNSUPPORTED`。不传 `category` 时返回全部统计行。

    `keyword`（2026-10-06）：标题**模糊查询**（`LIKE %keyword%`）。
    `item_id`（2026-10-06）：商业笔记按**商品 ID** 反查（统计表 `item_id` 列）。

    `sort_by` / `sort_order`（2026-10-12）：指标列排序。`sort_by` 是**白名单**里的
    统计表列名（`read_count` / `like_count` / `collect_count` / `comment_count` /
    `share_count` / `pay_gmv` / `pay_order_cnt` / `publish_time` / `stat_date`），
    非法列 400 `NOTE_SORT_UNSUPPORTED`（防注入）。缺省 `sort_order` = `desc`；
    不传 `sort_by` = 默认排序（stat_date desc, id desc）。

    `published_from` / `published_to`（2026-10-12）：发布时间区间（unix 秒，含端点）。
    `deleted`（2026-10-12）：`deleted` = 只看已删（`platform_deleted_at` 非空），
    `active` = 只看未删，缺省 `all` = 全部。

    指标区间（2026-10-12，数据中心页「指标搜索条件」）：`read_from/read_to`、
    `like_from/like_to`、`collect_from/collect_to`、`comment_from/comment_to`、
    `share_from/share_to`、`pay_gmv_from/pay_gmv_to`、`pay_order_from/pay_order_to`。
    均为可选，NULL 端点 = 该侧不筛；区间为**闭区间**（`col >= from` / `col <= to`）。

    ⚠️ 路由必须注册在 `/{note_id}` 之前 —— 否则 "offline" 会被 int 转换吃掉（422）。
    """
    #: 可排序列白名单（防注入：只允许这些真实列名进 ORDER BY）。
    _SORTABLE = {
        "read_count", "like_count", "collect_count", "comment_count",
        "share_count", "pay_gmv", "pay_order_cnt", "publish_time", "stat_date",
    }
    statement = select(NoteDatacenterStat).where(NoteDatacenterStat.user_id == current_user.id)
    #: ⚠️ **不再用 notes 的 `is_deleted` 过滤**（2026-10-27 用户口径「数据中心的逻辑
    #: 与内容库完全拆分」）：那是一个跨表 EXISTS，把数据中心的可见性绑在内容库的
    #: 软删状态上 —— 内容库删一条，数据中心就不显示，而它的指标快照本来是独立的
    #: 历史事实。数据中心自己的「已删除」判据是 `platform_deleted_at`（平台侧真删，
    #: 见下面的 `deleted` 参数），那才是它该认的。
    cat_key = (category or "").strip()
    if cat_key:
        if cat_key not in ("goods", "normal"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "NOTE_CATEGORY_UNSUPPORTED",
                    "message": f"不支持的笔记分类：{cat_key}",
                    "supported": ["goods", "normal"],
                },
            )
        if cat_key == "goods":
            statement = statement.where(NoteDatacenterStat.note_flag == "goods_note")
        else:
            statement = statement.where(NoteDatacenterStat.note_flag != "goods_note")
    if platform_account_id is not None:
        statement = statement.where(NoteDatacenterStat.platform_account_id == platform_account_id)
    # 标题模糊查询（2026-10-06）：空串 / 空白不筛。
    if keyword and keyword.strip():
        statement = statement.where(NoteDatacenterStat.title.like(f"%{keyword.strip()}%"))
    # 笔记 ID 精确查询（2026-10-12）：统计表 note_id 列直接筛。
    if note_id and note_id.strip():
        statement = statement.where(NoteDatacenterStat.note_id == note_id.strip())
    # 发布时间区间（2026-10-12，unix 秒闭区间）。
    if published_from is not None:
        statement = statement.where(NoteDatacenterStat.publish_time >= published_from)
    if published_to is not None:
        statement = statement.where(NoteDatacenterStat.publish_time <= published_to)
    # 已删除开关（2026-10-12）。
    if deleted == "deleted":
        statement = statement.where(NoteDatacenterStat.platform_deleted_at.is_not(None))
    elif deleted == "active":
        statement = statement.where(NoteDatacenterStat.platform_deleted_at.is_(None))
    # 指标区间（2026-10-12，闭区间；NULL 端点 = 不筛该侧）。
    metric_ranges = {
        "read_count": (read_from, read_to),
        "like_count": (like_from, like_to),
        "collect_count": (collect_from, collect_to),
        "comment_count": (comment_from, comment_to),
        "share_count": (share_from, share_to),
        "pay_gmv": (pay_gmv_from, pay_gmv_to),
        "pay_order_cnt": (pay_order_from, pay_order_to),
    }
    for col_name, (lo, hi) in metric_ranges.items():
        col = getattr(NoteDatacenterStat, col_name)
        if lo is not None:
            statement = statement.where(col >= lo)
        if hi is not None:
            statement = statement.where(col <= hi)

    # 排序：`sort_by` 命中白名单才生效，否则按默认（stat_date desc, id desc）。
    if sort_by:
        if sort_by not in _SORTABLE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "NOTE_SORT_UNSUPPORTED",
                    "message": f"不支持的排序字段：{sort_by}",
                    "supported": sorted(_SORTABLE),
                },
            )
        col = getattr(NoteDatacenterStat, sort_by)
        order_expr = col.desc() if (sort_order or "desc") == "desc" else col.asc()
        #: NULL 排最后（缺值不是最小也不是最大，别把「没数据」当成「数据为零」排到前面）。
        order_expr = col.is_(None), order_expr
        statement = statement.order_by(*order_expr)
    else:
        statement = statement.order_by(NoteDatacenterStat.stat_date.desc(), NoteDatacenterStat.id.desc())

    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.scalars(
        statement.limit(page_size).offset((page - 1) * page_size)
    ).all()
    #: ⚠️ **不再回查 `notes` 取标题/封面**（2026-10-27 用户口径「数据的中心逻辑与
    #: 内容库完全拆分」）：统计表**自己就有** `title` / `cover_url` / `item_title`
    #: 等全部展示字段，回查只会把两个功能绑在一起 —— 内容库删行（软删 / 真删 /
    #: 存量清理）后数据中心就取不到值，甚至静默显示空标题。
    #:
    #: 拆分的判据：数据中心 = `note_datacenter_stats` 一张表，零 `notes` 依赖；
    #: 内容库 = `notes` 一张表。两张表只在「同一个平台 note_id」上语义相关，
    #: 读路径互不 join。
    items = []
    for r in rows:
        items.append({
            "note_id": r.note_id,
            "title": r.title or "",
            "note_flag": r.note_flag,
            "item_id": r.item_id,
            "item_title": r.item_title,
            "publish_time": r.publish_time,
            "read_count": r.read_count,
            "like_count": r.like_count,
            "collect_count": r.collect_count,
            "comment_count": r.comment_count,
            "share_count": r.share_count,
            "pay_gmv": r.pay_gmv,
            "pay_order_cnt": r.pay_order_cnt,
            "stat_date": r.stat_date,
            "window": r.window,
            "platform_deleted_at": r.platform_deleted_at.isoformat() if r.platform_deleted_at else None,
        })
    return {
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "items": items,
    }


class BatchDeleteNotesRequest(BaseModel):
    #: 平台 note_id（字符串，不是本地主键 id）
    note_ids: list[str] = Field(min_length=1)
    #: True = 删本地 + 平台；False = 只删本地（默认）。
    delete_platform: bool = False
    #: 账号（可选）。**本地没有这些笔记时必须给** —— 否则无法确定打哪个平台的令牌。
    #: 给了就一定按它走（即使本地笔记的归属账号与它不同，也不静默改）。
    platform_account_id: int | None = None


@router.post("/batch-delete")
def batch_delete_notes_endpoint(
    payload: BatchDeleteNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """商业/普通 Tab 复选框**批量删除**（2026-10-06 需求）。

    `note_ids` 是**平台 note_id**（不是本地主键）。两种方式（前端单选框）：

    * `delete_platform=false`（默认）：**只删本地**（`purge_note`，连关系行一起删），不碰平台。
    * `delete_platform=true`：**先删平台、成功后删本地** —— 商业笔记（goods_note）走千帆
      `batch_delete_notes`（批量），普通笔记（非 goods_note）走创作者 `validate_delete_permission`
      + `delete_note`（逐篇）。平台删除失败的笔记**本地保留**，如实返回失败原因。

    返回 `{deleted: int, failed: [...], results: [...]}`。账号无对应平台 Cookie 时，
    平台删除全部失败（本地也不删），理由如实带出。

    2026-10-12 设计 35：商业/普通分流判据由 `note_category` 改 `note_flag`
    （`note_datacenter_stats.note_flag == "goods_note"` ⇒ 商业）。
    """
    from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter
    from backend.app.adapters.xhs.qianfan_api_adapter import XhsQianfanApiAdapter
    from backend.app.services.account_service import (
        CHANNEL_ARK,
        CHANNEL_CREATOR,
        latest_cookies_for_channel,
    )

    wanted = [str(n).strip() for n in payload.note_ids if str(n).strip()]
    if not wanted:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="note_ids 为空")

    notes = db.scalars(
        select(Note).where(Note.user_id == current_user.id, Note.note_id.in_(wanted))
    ).all()
    by_note_id = {n.note_id: n for n in notes}

    results: list[dict[str, Any]] = []
    deleted = 0
    failed: list[dict[str, Any]] = []

    if not payload.delete_platform:
        # 只删本地：直接 purge。
        for nid in wanted:
            note = by_note_id.get(nid)
            if note is None:
                results.append({"note_id": nid, "ok": False, "reason": "本地无此笔记"})
                failed.append({"note_id": nid, "reason": "本地无此笔记"})
                continue
            purge_note(db, note)
            deleted += 1
            results.append({"note_id": nid, "ok": True})
        db.commit()
        return {"deleted": deleted, "failed": failed, "results": results}

    # 删本地 + 平台：**先删平台，成功后再删本地**（设计 44 用户口径）。
    # 账号：优先用请求里给的（本地没这些笔记时只能靠它），否则取第一笔本地笔记的归属账号。
    account_id = payload.platform_account_id
    if account_id is None:
        for n in notes:
            if n.platform_account_id:
                account_id = n.platform_account_id
                break
    if account_id is None:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="这批笔记本地都没有记录，需显式指定 platform_account_id 才能删除平台数据",
        )

    # 用自己的账号做归属校验（不给别人的账号当令牌用）
    from backend.app.api.platforms.xhs.qianfan._shared import _owned_account

    _owned_account(db, current_user, account_id)

    ark_cookies = latest_cookies_for_channel(db, account_id, CHANNEL_ARK) or ""
    creator_cookies = latest_cookies_for_channel(db, account_id, CHANNEL_CREATOR) or ""
    qianfan_adapter = XhsQianfanApiAdapter(ark_cookies) if ark_cookies else None
    creator_adapter = XhsCreatorApiAdapter(creator_cookies) if creator_cookies else None

    # 分组：goods_note → 千帆批量，非 goods_note → creator 逐篇。
    # 2026-10-12 设计 35：分流判据读统计表 `note_flag`（取代已退役的 `note_category`）。
    flag_by_note_id = {
        r.note_id: r.note_flag
        for r in db.scalars(
            select(NoteDatacenterStat.note_id, NoteDatacenterStat.note_flag).where(
                NoteDatacenterStat.note_id.in_(wanted)
            )
        ).all()
    }
    goods_ids: list[str] = []
    creator_ids: list[str] = []
    for nid in wanted:
        note = by_note_id.get(nid)
        # ⚠️ 2026-10-18 口径修正（设计 44）：本地没有**不再直接判失败** ——
        #    旧写法在这里 `failed` 完就 continue，**根本不打平台**，于是「平台有、
        #    本地没同步过」的笔记永远删不掉。新口径：先删平台，本地没有就跳过。
        #    是否分区（商业/普通）仍看统计表；本地无统计行 ⇒ 按普通笔记处理。
        if note is not None and flag_by_note_id.get(nid) == "goods_note":
            goods_ids.append(nid)
        else:
            creator_ids.append(nid)

    # 商业笔记：千帆批量删除（一次请求删整组）。成功后逐篇 purge 本地。
    if goods_ids:
        # 商业笔记：**千帆先试 → 创作者兜底 → 任一成功即止**（用户 2026-10-18：
        # 「均调用一次，先走普通再走千帆，其中一个成功就不再走其它的」）。
        # ⚠️ 顺序不同不矛盾：商业笔记的**正路**本来就是千帆，普通笔记的正路是创作者；
        #    两者都是「正路失败再兜底」，绝不是各删一次。
        goods_ok_ids: list[str] = []
        goods_fail: dict[str, str] = {}

        if qianfan_adapter is not None:
            try:
                ok, msg, _ = qianfan_adapter.batch_delete_notes(goods_ids)
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, f"千帆删除异常：{type(exc).__name__}: {exc}"
            if ok:
                goods_ok_ids = list(goods_ids)
            else:
                for nid in goods_ids:
                    goods_fail[nid] = f"千帆通道：{msg}"
        else:
            for nid in goods_ids:
                goods_fail[nid] = "千帆通道：缺少千帆(ark)登录态"

        # 千帆没删掉的，创作者再试一次（有成就不再调）
        for nid in goods_ids:
            if nid in goods_ok_ids:
                continue
            if creator_adapter is None:
                goods_fail[nid] = f"{goods_fail.get(nid, '')}；普通笔记通道：缺少创作者登录态"
                continue
            try:
                ok, msg, _ = creator_adapter.validate_delete_permission(nid)
                if ok:
                    ok, msg, _ = creator_adapter.delete_note(nid)
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, f"创作者异常：{type(exc).__name__}: {exc}"
            if ok:
                goods_ok_ids.append(nid)
                goods_fail.pop(nid, None)
            else:
                goods_fail[nid] = f"{goods_fail.get(nid, '')}；普通笔记通道：{msg}"

        for nid in goods_ids:
            if nid in goods_ok_ids:
                note = by_note_id.get(nid)
                if note is not None:
                    purge_note(db, note)
                    deleted += 1
                    results.append({"note_id": nid, "ok": True, "status": "deleted"})
                else:
                    # 平台删了、本地没有 ⇒ 跳过（不是失败）
                    results.append({
                        "note_id": nid, "ok": True, "status": "platform_only",
                        "reason": "平台已删除；本地没有这条笔记，已跳过",
                    })
            else:
                reason = goods_fail.get(nid) or "平台删除失败"
                results.append({"note_id": nid, "ok": False, "reason": reason})
                failed.append({"note_id": nid, "reason": reason})

    # 普通笔记：**创作者通道先试 → 千帆兜底 → 任一成功即止**（用户 2026-10-18 口径：
    # 「商业笔记和普通笔记均调用一次，先走普通再走千帆，如果其中一个成功则不再走其它的」）。
    # 旧写法 creator 不可用就整条失败，而账号 8 实测创作者会话过期、千帆却可用 ⇒
    # 能删的笔记被判失败。
    for nid in creator_ids:
        note = by_note_id.get(nid)
        reason_parts: list[str] = []
        platform_ok = False

        if creator_adapter is not None:
            try:
                ok, msg, _ = creator_adapter.validate_delete_permission(nid)
                if ok:
                    ok, msg, _ = creator_adapter.delete_note(nid)
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, f"创作者异常：{type(exc).__name__}: {exc}"
            if ok:
                platform_ok = True
            else:
                reason_parts.append(f"普通笔记通道：{msg}")
        else:
            reason_parts.append("普通笔记通道：缺少创作者登录态")

        # ① 没成功 ⇒ 千帆再试一次；② 成功了就不再调另一条
        if not platform_ok:
            if qianfan_adapter is not None:
                try:
                    ok, msg, _ = qianfan_adapter.batch_delete_notes([nid])
                except Exception as exc:  # noqa: BLE001
                    ok, msg = False, f"千帆异常：{type(exc).__name__}: {exc}"
                if ok:
                    platform_ok = True
                else:
                    reason_parts.append(f"千帆通道：{msg}")
            else:
                reason_parts.append("千帆通道：缺少千帆(ark)登录态")

        if platform_ok:
            if note is not None:
                purge_note(db, note)
                deleted += 1
                results.append({"note_id": nid, "ok": True, "status": "deleted"})
            else:
                # 平台删了、本地没有 ⇒ 跳过（不是失败）
                results.append({
                    "note_id": nid, "ok": True, "status": "platform_only",
                    "reason": "平台已删除；本地没有这条笔记，已跳过",
                })
        else:
            reason = "；".join(reason_parts) or "平台删除失败"
            results.append({"note_id": nid, "ok": False, "reason": reason})
            failed.append({"note_id": nid, "reason": reason})

    db.commit()
    return {"deleted": deleted, "failed": failed, "results": results}


@router.post("/{note_id}/hydrate")
def hydrate_offline_note_detail(
    note_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**点击笔记卡片时实时拉详情并落库**（2026-10-06 需求，取代定时任务/导入自动拉）。

    对**一条**离线笔记（有数据中心统计，即 `note_datacenter_stats` 有行）实时拉
    平台详情（正文 / 媒体图视频 / 话题），**落库**：正文 → `notes.content`、媒体 →
    `note_assets`、话题 → `topics` + `note_topics`（拆表）。数据源是笔记原链接
    自带的 `xsec_token`（匿名 explore SSR，无需登录）。

    ⚠️ 仅离线笔记支持（统计表有行）；内容库/「全部笔记」tab 的线上笔记
    走 `POST /xhs/pc/notes/detail`（不落库）。

    返回序列化后的完整笔记（含落库后的 `content` / `asset_urls` / `topics` /
    `raw_json.metrics` 指标）。幂等：已有正文+媒体则跳过（`hydrate_offline_note`）。
    """
    from backend.app.services.note_data_service import hydrate_offline_note
    from backend.app.core.rate_limiter import get_rate_limiter
    from backend.app.adapters.xhs.note_detail_ssr import fetch_note_detail_payload

    # 离线笔记判定：`note_datacenter_stats` 统计表有行（2026-10-12 设计 35，
    # 取代 `note_category != DEFAULT`）。
    has_stats = db.scalar(
        select(NoteDatacenterStat.id).where(
            NoteDatacenterStat.note_id == note_id,
            NoteDatacenterStat.user_id == current_user.id,
        )
    )
    note = db.scalars(
        select(Note).where(
            Note.note_id == note_id,
            Note.user_id == current_user.id,
            Note.is_deleted.is_(False),
        )
    ).first()
    if note is None or has_stats is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "OFFLINE_NOTE_NOT_FOUND", "message": "该离线笔记不存在或不属于当前账号"},
        )

    # 2026-10-06：详情拉取用 `raw_json.note_url` 里已有的 xsec_token。笔记在平台侧
    # 已被删除/不可见时拉不到详情是**数据事实**（用户已确认，无需回查补救）——
    # 这里只拉一次，失败就安静降级，不按 note_id 回查（回查也救不了已删除的笔记）。
    outcome = hydrate_offline_note(
        db,
        note,
        fetch_payload=fetch_note_detail_payload,
        limiter=get_rate_limiter(),
    )
    # 2026-10-06：详情拉取失败（笔记平台已删除/不可见）**不 502 报错**，而是安静降级
    # —— 返回笔记现有的离线数据（标题/指标/商品），正文/媒体留空。前端据
    # `hydrate_status` 显示「详情暂不可用」的安静提示，而不是红块错误。
    db.commit()
    serialized = _serialize_note_with_topics(db, note)
    serialized["hydrate_status"] = outcome["status"]
    return serialized


@router.post("/import-data")
async def import_note_data(
    file: UploadFile = File(...),
    category: str = Form(...),
    account_id: int = Form(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """导入离线笔记数据文件（商业 / 普通 Tab 的「导入」按钮，2026-10-06 需求）。

    用户上传 xlsx（千帆数据中心导出的笔记数据文件），后端**异步**解析落库
    （弹窗无需等待解析），解析完成后发**站内通知**推送统计结果。临时文件解析
    完毕后删除。

    `category` ∈ `goods` / `normal`（决定笔记分类）；`account_id` 是归属账号
    （越权 404）。文件格式必须是 `.xlsx`。

    返回立即回执 `{ok, status:"importing"}`；统计经 `notifications` 站内消息推送。
    """
    from backend.app.services.note_data_service import import_note_data_file

    if category not in ("goods", "normal"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "NOTE_CATEGORY_UNSUPPORTED",
                "message": f"不支持的笔记分类：{category}",
                "supported": ["goods", "normal"],
            },
        )
    # 账号归属校验（越权 404，不暴露「存在但无权」）
    get_owned_active_account(
        db, current_user, account_id, platform="xhs"
    )

    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="只支持 .xlsx 文件")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件为空")

    # 落临时文件（解析线程读它，读完删除）
    tmp_dir = Path(get_settings().storage_dir) / "imports"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"note-import-u{current_user.id}-{uuid4().hex}.xlsx"
    tmp_path.write_bytes(content)

    # 2026-10-06：导入改走**任务日志**入口（`scheduled_job_logs`），复用
    # `system.note_data_sync` 任务做归属，但**不新增任务列表条目**（用户口径
    # 「改任务不在任务列表不展示」）。这样任务中心「任务日志」能查到执行进度，
    # 前端导入按钮据此显示「文件解析执行中」。
    from backend.app.services import scheduler_service

    db_log = SessionLocal()
    try:
        scheduler_service.seed_builtin_jobs_for_user(db_log, current_user.id)
        job = db_log.scalars(
            select(ScheduledJob).where(
                ScheduledJob.user_id == current_user.id,
                ScheduledJob.job_key == "system.note_data_sync",
            )
        ).first()
        if job is None:
            # 理论上 seed 后必存在；兜底仍失败则回退到无日志的后台线程解析。
            job = None
        log = ScheduledJobLog(
            job_id=job.id if job is not None else None,
            user_id=current_user.id,
            platform_account_id=account_id,
            job_name="离线笔记数据导入",
            job_key="system.note_data_sync",
            job_trigger_type=job.trigger_type if job is not None else "manual",
            trigger_type="manual",
            status=LOG_RUNNING,
            progress=0,
            message=f"正在解析{'商业' if category == 'goods' else '普通'}笔记数据文件…",
            config_snapshot={"category": category, "account_id": account_id},
        )
        db_log.add(log)
        db_log.commit()
        db_log.refresh(log)
        log_id = log.id
    except Exception:  # noqa: BLE001 - 建日志失败不阻断导入
        log_id = None
    finally:
        db_log.close()

    # 后台线程解析 + 落库 + 发通知 + 删临时文件 + 写日志进度/终态（弹窗无需等待）
    import threading

    def _run() -> None:
        try:
            result = import_note_data_file(
                account_id=account_id,
                category=category,
                file_path=str(tmp_path),
                user_id=current_user.id,
            )
            if result.get("error"):
                title = f"笔记数据导入失败（{category}）"
                body = result["error"]
                level = "error"
                log_status = LOG_FAILED
                log_message = f"导入失败：{result['error']}"
                log_progress = None  # 保留泵最后一次写入的进度（缺值口径，不猜）
            else:
                hydrate = result.get("hydrate") or {}
                title = "笔记数据导入完成"
                parts = [
                    f"{'商业笔记' if category == 'goods' else '普通笔记'}："
                    f"解析 {result.get('fetched', 0)} 条，新增 {result.get('created', 0)} 条，"
                    f"更新 {result.get('updated', 0)} 条",
                ]
                if hydrate.get("attempted"):
                    parts.append(
                        f"补齐详情 {hydrate.get('filled', 0)} 篇"
                        f"（失败 {hydrate.get('failed', 0)}，缺链接 {hydrate.get('no_url', 0)}）"
                    )
                body = "；".join(parts)
                level = "info"
                log_status = LOG_SUCCESS
                log_message = body
                log_progress = 100
            db_notify = SessionLocal()
            try:
                db_notify.add(
                    Notification(
                        user_id=current_user.id,
                        title=title,
                        body=body,
                        level=level,
                        source_type="note_data_import",
                    )
                )
                db_notify.commit()
            finally:
                db_notify.close()
            # 写日志终态（复用 SessionLocal 自建 Session）
            if log_id is not None:
                db_log2 = SessionLocal()
                try:
                    log_row = db_log2.get(ScheduledJobLog, log_id)
                    if log_row is not None:
                        log_row.status = log_status
                        log_row.finished_at = shanghai_now()
                        log_row.duration_ms = max(
                            0, int((log_row.finished_at - log_row.started_at).total_seconds() * 1000)
                        )
                        if log_progress is not None:
                            log_row.progress = log_progress
                        log_row.message = log_message[:1000]
                        db_log2.commit()
                finally:
                    db_log2.close()
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    threading.Thread(target=_run, name=f"note-data-import-{current_user.id}", daemon=True).start()
    return {"ok": True, "status": "importing", "category": category, "log_id": log_id}


class SyncNoteDataRequest(BaseModel):
    #: 归属账号（越权 404）。
    account_id: int
    #: `goods` / `normal`。
    category: str
    #: 自定义日期范围起止（`YYYY-MM-DD`，可空 = 默认近 90 日）。
    start_date: Optional[str] = None
    end_date: Optional[str] = None


@router.post("/sync-data")
def sync_note_data_endpoint(
    payload: SyncNoteDataRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**手动同步**离线笔记数据（商业 / 普通 Tab 的「同步」按钮，2026-10-06 需求）。

    与「导入 xlsx」并列的第二个离线数据入口：直接从千帆数据中心 `butterfly/data`
    拉（无需上传文件），按**用户选择的日期范围**同步，**最长 90 天**（超出 400）。

    后台线程执行（弹窗无需等待），走任务日志（`system.note_data_sync` 归属，不新增
    任务列表条目，同「导入」口径），完成后站内通知统计。

    `start_date`/`end_date` 非空 ⇒ 走自定义日期范围（`dateSelectType="custom"`）；
    空 ⇒ 默认 `recent_90`（近 90 日）。
    """
    from datetime import datetime as _dt

    if payload.category not in ("goods", "normal"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "NOTE_CATEGORY_UNSUPPORTED",
                "message": f"不支持的笔记分类：{payload.category}",
                "supported": ["goods", "normal"],
            },
        )
    get_owned_active_account(
        db, current_user, payload.account_id, platform="xhs"
    )

    start_date = (payload.start_date or "").strip()
    end_date = (payload.end_date or "").strip()
    # 日期范围 ≤ 90 天（2026-10-06 用户口径「同步最长 90 天内的数据」）。
    if start_date and end_date:
        try:
            s = _dt.strptime(start_date, "%Y-%m-%d")
            e = _dt.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="日期格式应为 YYYY-MM-DD",
            )
        if s > e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="开始日期不能晚于截止日期")
        if (e - s).days > 90:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "NOTE_DATA_DATE_RANGE_TOO_LONG",
                    "message": "同步日期范围最长 90 天",
                },
            )

    # 走任务日志（与「导入」同一条口径：`system.note_data_sync` 归属，不新增任务条目）。
    from backend.app.services import scheduler_service

    db_log = SessionLocal()
    try:
        scheduler_service.seed_builtin_jobs_for_user(db_log, current_user.id)
        job = db_log.scalars(
            select(ScheduledJob).where(
                ScheduledJob.user_id == current_user.id,
                ScheduledJob.job_key == "system.note_data_sync",
            )
        ).first()
        log = ScheduledJobLog(
            job_id=job.id if job is not None else None,
            user_id=current_user.id,
            platform_account_id=payload.account_id,
            job_name="离线笔记数据同步",
            job_key="system.note_data_sync",
            job_trigger_type=job.trigger_type if job is not None else "manual",
            trigger_type="manual",
            status=LOG_RUNNING,
            progress=0,
            message=f"正在同步{'商业' if payload.category == 'goods' else '普通'}笔记数据…",
            config_snapshot={
                "category": payload.category,
                "account_id": payload.account_id,
                "start_date": start_date or None,
                "end_date": end_date or None,
            },
        )
        db_log.add(log)
        db_log.commit()
        db_log.refresh(log)
        log_id = log.id
    except Exception:  # noqa: BLE001 - 建日志失败不阻断同步
        log_id = None
    finally:
        db_log.close()

    import threading

    from backend.app.services.note_data_service import sync_account_note_data

    def _run() -> None:
        result = sync_account_note_data(
            account_id=payload.account_id,
            category=payload.category,
            user_id=current_user.id,
            start_date=start_date,
            end_date=end_date,
            date_select_type="recent_90" if not start_date else "custom",
        )
        if result.get("error"):
            title = f"笔记数据同步失败（{payload.category}）"
            body = result["error"]
            level = "error"
            log_status = LOG_FAILED
            log_message = f"同步失败：{result['error']}"
            log_progress = None
        else:
            title = "笔记数据同步完成"
            body = (
                f"{'商业笔记' if payload.category == 'goods' else '普通笔记'}："
                f"拉取 {result.get('fetched', 0)} 条，新增 {result.get('created', 0)} 条，"
                f"更新 {result.get('updated', 0)} 条"
            )
            level = "info"
            log_status = LOG_SUCCESS
            log_message = body
            log_progress = 100
        db_notify = SessionLocal()
        try:
            db_notify.add(
                Notification(
                    user_id=current_user.id,
                    title=title,
                    body=body,
                    level=level,
                    source_type="note_data_sync",
                )
            )
            db_notify.commit()
        finally:
            db_notify.close()
        if log_id is not None:
            db_log2 = SessionLocal()
            try:
                log_row = db_log2.get(ScheduledJobLog, log_id)
                if log_row is not None:
                    log_row.status = log_status
                    log_row.finished_at = shanghai_now()
                    log_row.duration_ms = max(
                        0, int((log_row.finished_at - log_row.started_at).total_seconds() * 1000)
                    )
                    if log_progress is not None:
                        log_row.progress = log_progress
                    log_row.message = log_message[:1000]
                    db_log2.commit()
            finally:
                db_log2.close()

    threading.Thread(target=_run, name=f"note-data-sync-{current_user.id}", daemon=True).start()
    return {"ok": True, "status": "syncing", "category": payload.category, "log_id": log_id}


@router.post("/batch-create-drafts")
def batch_create_drafts(
    payload: BatchCreateDraftsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """批量把已入库笔记转成改写草稿（设计 05 §「`/api/notes/batch-create-drafts`」）。

    ⚠️ **本函数此前一直是死代码**（2026-10-07 修）：函数体、请求模型
    `BatchCreateDraftsRequest`、序列化器 `_serialize_draft` 三样都在，**唯独丢了
    `@router.post` 装饰器** ⇒ 路由不存在。表现极具误导性：

      - `GET /openapi.json` 里没有它（端点数照旧），所以**端点数口径看不出异常**；
      - 前端内容库「批量加入草稿工坊」按钮（`createBatchRewriteDrafts`）点下去
        必 `405 Method Not Allowed`；
      - `test_notes_batch_create_drafts_...` 长期红，容易被当成「测试写错了」。

    归属校验走 `_get_unique_owned_notes`（非本人笔记 ⇒ 404），与单篇
    `POST /notes/{id}/...` 同一把尺子。`intent` 只影响前端文案，落库形态与
    单篇 `POST /api/drafts` 一致（拷贝标题 / 正文 / 来源笔记 id）。
    """
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
    return _serialize_note_with_topics(db, note)


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
    return _serialize_note_with_topics(db, note)


@router.post("/{note_id}/sync", include_in_schema=False)
def sync_note_removed(note_id: int):
    """**已退场**（设计 75 §4.5，2026-10-27）—— 原「同步单篇笔记」。

    内容库改**读平台实时**后没有「本地副本需要刷新」这回事，
    与批量同步 / 同步队列同批退场（同一个 `NOTE_SYNC_REMOVED` 错误码）。
    """
    _note_sync_gone("single")


@router.post("/{note_id}/publish")
def publish_note_endpoint(
    note_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """内容库「发布」按钮：**异步**启动发布（设计 13 §31.5，2026-09-25 异步化）。

    同步段只做门禁（归属 / 类型 / 状态 / 媒体 / ark 令牌；**发布中 ⇒ 400，
    不会双发**）；下载 / 上传 / 预审核 / 发布在后台守护线程跑，本端点**立即返回**
    `publish:{ok:true, status:"publishing"}` 与 publishing 态的笔记行 —— 前端点击
    发布无需等待。终态（`published` / `failed` + `publish_error` 平台原文）由前端
    轮询 `GET /notes/{id}` 获取（`publish_status` 是唯一状态字段，§32.3）。

    门禁失败（不含 / 已发布 / 发布中 / 无媒体 / 无 ark 令牌）走 400 结构化错误。
    二次确认在前端做（用户口径），本端点不重复拦。
    """
    from backend.app.services.note_publish_service import NotePublishError, start_publish

    try:
        start_publish(db, current_user.id, note_id, adapter_factory=adapter_factory)
    except NotePublishError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": error.code, "message": error.message},
        )
    note = db.get(Note, note_id)
    return {
        "note": _serialize_note_with_topics(db, note),
        "publish": {"ok": True, "status": "publishing", "note_pk": note.id, "note_id": note.note_id},
    }


@router.post("/batch-publish")
def batch_publish_notes_endpoint(
    payload: BatchPublishNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """批量发布（异步队列；设计 13 §31.5 步 8，2026-09-25 异步化）。

    逐篇过门禁（同步；失败篇立即进 `results`，不阻断），通过篇入队由**一个**
    后台线程顺序跑完。**立即返回** `{total, started, failed, results}`：通过篇
    `{note_pk, ok:true, status:"publishing", note_id}`，门禁失败篇
    `{note_pk, ok:false, error, code}`。终态由前端对 publishing 篇轮询获取。
    """
    from backend.app.services.note_publish_service import start_batch_publish

    return start_batch_publish(
        db, current_user.id, payload.note_ids, adapter_factory=adapter_factory, max_items=payload.max_items
    )


@router.post("/batch-sync", include_in_schema=False)
def batch_sync_notes_removed():
    """**已退场**（设计 75 §4.5，2026-10-27）—— 原「批量同步选中笔记」。"""
    _note_sync_gone("batch")


@router.put("/{note_id}/topics")
def update_note_topics(
    note_id: int,
    payload: UpdateNoteTopicsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """话题增删的**落库入口**（设计 13 §29.3 / §34.4）：整列替换话题关系。

    内容库详情卡里「+ 新增话题 / Tag 上 × 删话题」都调这里 —— 每次增删一次写库
    （「实时更新数据库」），前端传增删后的完整列表，后端归一化后**对齐关系行**
    （`replace_note_topics`：话题行 find-or-create，关系行删到与列表严格一致后
    按输入顺序重建）。

    ⚠️ 与同步链路的写入语义**刻意不同**（§34.5）：同步 / 采集 / 详情补齐 /
    商品生成走 `apply_sync_topics`（**只增不删** —— 平台撤了话题也不删关系行）；
    本端点是**用户手动管理**，整列替换（§29.3 原契约：调用方传完整列表，后端不猜）。
    ⚠️ 2026-09-25 起不再写 `topics_json`（该列已删，§34.1）—— 落点就是
    `note_topics` 关系表，与序列化读取路径同一张表。
    """
    note = _get_owned_note(db, current_user, note_id)
    replace_note_topics(db, note, payload.topics)
    db.commit()
    db.refresh(note)
    return _serialize_note_with_topics(db, note)


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
        # 2026-10-12：已软删的笔记**跳过不复活**（无流量清理软删后，采集/监控
        # 若再落库同一条，会把它「复活」回列表）。
        if existing is not None and existing.is_deleted:
            continue
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
        "items": [_serialize_note_with_topics(db, note) for note in saved_notes],
    }


@router.post("/batch-tag")
def batch_tag_notes(
    payload: BatchTagNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """按话题 id 批量增删笔记的话题关系（§34.4：标签表已并入话题表，`tag_ids` 即 topic ids）。

    `mode`：`replace` = 整列替换 / `add` = 只增 / `remove` = 只删。前端详情弹窗
    的话题增删在 2026-09-25 前走这里；现已统一到 `PUT /notes/{id}/topics`
    （整列替换 + find-or-create，一个入口）⇒ 本端点保留给 API 消费者，
    行为与手工删话题的删除链一致（关系行跟着走，话题字典行不动）。
    """
    notes: list[Note] = []
    for note_id in dict.fromkeys(payload.note_ids):
        notes.append(_get_owned_note(db, current_user, note_id))

    topic_ids = list(dict.fromkeys(payload.tag_ids))
    for topic_id in topic_ids:
        topic = db.get(Topic, topic_id)
        if topic is None or topic.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Topic not found")

    for note in notes:
        if payload.mode == "replace":
            db.execute(delete(NoteTopic).where(NoteTopic.note_id == note.id))
            for topic_id in topic_ids:
                db.add(NoteTopic(note_id=note.id, topic_id=topic_id))
            continue

        if payload.mode == "add":
            existing_topic_ids = set(
                db.scalars(select(NoteTopic.topic_id).where(NoteTopic.note_id == note.id)).all()
            )
            for topic_id in topic_ids:
                if topic_id not in existing_topic_ids:
                    db.add(NoteTopic(note_id=note.id, topic_id=topic_id))
            continue

        if topic_ids:
            db.execute(
                delete(NoteTopic).where(
                    NoteTopic.note_id == note.id,
                    NoteTopic.topic_id.in_(topic_ids),
                )
            )

    db.commit()
    return {
        "updated_count": len(notes),
        "items": [_serialize_note_with_topics(db, note) for note in notes],
    }
