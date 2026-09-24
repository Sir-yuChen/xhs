"""商品笔记智能生成（设计 13 §31，2026-09-23 用户口径）。

## 这个模块做什么（其余不归它管）

| 做 | 不归它 |
|---|---|
| ① 调千帆「智能生成笔记」三步链路（attributes → sellerpoint/aigc → 轮询） | 发布（`note_publish_service`） |
| ② 把生成结果落库：`notes`（`local_generated`/`pending`）+ `note_assets`（商品图）+ `note_goods`（关系） | 干运行预览（`product_notes.py::generate` 端点原样保留） |
| ③ 护栏**记录不拦截**（内容出自平台官方 AI，与本地模型不同链） | 话题增删入口（设计 13 §29 已有） |

## 三条硬约定

1. **商品取自本地台账，不打平台**：媒体链接、价格、挂链关系都必须指向**同一行**
   `shop_products`（v1.32 起商品列表即本地，§30）—— 生成时看平台、发布时挂台账，
   两头对不上就是「笔记挂了 A 商品的图、链了 B 商品」。
2. **一个事务落四张表**：笔记 / 素材 / 关系 / 话题同生同死，不留半条。
3. **生成上下文尽力而为**：需求详情（卖点/灵感/人群）三级链路任一环失败就退化为
   attributes 自带的卖点词与默认风格 —— 少一级依赖不该让功能不可用。

## 轮询节奏（真机实测）

页面自身 1.5s 间隔、~6–21s 出结果。本服务同样 1.5s 间隔、90s 上限；
超时按失败处理（502 + 平台 `errMsg` 或超时说明），**不落库**。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from apis.xhs_qianfan_apis import (
    AIGC_TASK_STATUS_COMPLETED,
    AIGC_TASK_STATUS_RUNNING,
)
from backend.app.models.note import (
    NOTE_TYPE_LOCAL_GENERATED,
    PUBLISH_STATUS_PENDING,
    Note,
    set_note_publish_status,
)
from backend.app.models.shop import NoteGoods
from backend.app.models.shop_product import ShopProduct
from backend.app.services.product_note_service import run_guardrails
from backend.app.services.shop_binding_service import account_shop_id, qianfan_cookies_for_account

logger = logging.getLogger(__name__)

#: 轮询间隔与上限（秒）。真机 ~6–21s 出结果；90s 是「平台卡死」的判定线。
_POLL_INTERVAL = 1.5
_POLL_TIMEOUT = 90.0

#: 本地生成笔记的 ID 前缀（需求③：项目自动生成唯一 ID；平台 ID 发布后才回写）。
_LOCAL_NOTE_PREFIX = "local_"

#: 一篇笔记的媒体上限（设计 13 §32.2.1，用户 2026-09-23 口径
#: 「控制图片或视频数量，图片最多 6 张，视频最多一个」）。
#: **视频优先**：命中视频 ⇒ 只落 1 个 video 资产、不落图片；否则落前 6 张图。
MAX_NOTE_IMAGES = 6
MAX_NOTE_VIDEOS = 1

#: 商品原文里可能承载视频的键（口径见 :func:`_product_video`）。
_VIDEO_KEYS = ("video", "video_url", "video_link", "item_video", "video_info")


class ProductNoteGenerationError(Exception):
    """生成失败（门禁 / 平台 / 解析）。`code` 给前端做结构化呈现。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _local_note_id() -> str:
    return f"{_LOCAL_NOTE_PREFIX}{uuid.uuid4().hex[:12]}"


def _first_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _first_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _product_row(db: Session, account, item_id: str) -> ShopProduct:
    """本地台账取商品行（同一份真相源：生成 / 挂链 / 媒体都指它）。"""
    shop_pk = account_shop_id(db, account.id)
    if shop_pk is None:
        raise ProductNoteGenerationError(
            "QIANFAN_BINDING_REQUIRED", "尚未绑定千帆商家后台，无法生成商品笔记。请先绑定后再生成。"
        )
    row = db.scalars(
        select(ShopProduct).where(
            ShopProduct.shop_id_fk == shop_pk, ShopProduct.item_id == item_id
        )
    ).first()
    if row is None:
        raise ProductNoteGenerationError(
            "QIANFAN_ITEM_NOT_FOUND",
            f"本地台账里找不到商品 {item_id}（可能还没同步商品，或已被删除）。请先执行「同步商品」。",
        )
    return row


def _product_images(row: ShopProduct) -> list[dict]:
    """商品图（URL + 宽高），**最多 `MAX_NOTE_IMAGES` 张**（§32.2.1）。

    宽高取 `raw_json.images[]`（发布 `images_list` 要用）；缺失留空不造假。
    """
    try:
        urls = json.loads(row.images_json or "[]")
    except ValueError:
        urls = []
    urls = [str(u) for u in urls if isinstance(u, str) and u.strip()][:MAX_NOTE_IMAGES]
    dims: dict[str, dict] = {}
    try:
        raw_images = json.loads(row.raw_json or "{}").get("images")
    except ValueError:
        raw_images = None
    if isinstance(raw_images, list):
        for entry in raw_images:
            if isinstance(entry, dict) and entry.get("link"):
                dims[str(entry["link"])] = {
                    "width": entry.get("width") or 0,
                    "height": entry.get("height") or 0,
                }
    out: list[dict] = []
    for url in urls:
        dim = dims.get(url, {})
        out.append({"url": url, "width": int(dim.get("width") or 0), "height": int(dim.get("height") or 0)})
    return out


def _product_video(row: ShopProduct) -> dict:
    """商品视频（§31.1.3 + §32.2.1 的「视频优先」取舍点）。

    ⚠️ **当前数据源不可得**：`search_item_v2` 响应不含商品视频（真机 1974 条遍历
    `raw_json` 0 命中 video 键）；商品视频只在要 `X-s` 签名的编辑页
    `publish_render`（C4 不接入）。因此本函数是**取舍规则的落点**而不是现状描述：
    命中即返回 `{url,width,height}`，否则空 dict —— 不猜 URL、不造占位。
    生成结果里 `video_count` 如实报告，别把「没有视频」渲染成「有 0 个」。
    """
    try:
        raw = json.loads(row.raw_json or "{}")
    except ValueError:
        raw = {}
    if not isinstance(raw, dict):
        return {}
    for key in _VIDEO_KEYS:
        value = raw.get(key)
        url = ""
        width = height = 0
        if isinstance(value, str):
            url = value.strip()
        elif isinstance(value, dict):
            url = str(value.get("link") or value.get("url") or "").strip()
            try:
                width = int(value.get("width") or 0)
                height = int(value.get("height") or 0)
            except (TypeError, ValueError):
                width = height = 0
        if url:
            return {"url": url, "width": width, "height": height}
    return {}


def _demand_context(adapter, item_id: str) -> dict:
    """需求详情（卖点 / 灵感 / 人群）—— **尽力而为**，任一环失败返回空 dict。

    链路（页面自身串的）：类目 → 类目需求清单 → 需求详情。三级都是只读。
    """
    try:
        ok, _, cat_raw = adapter.batch_get_category([item_id])
        if not ok or not isinstance(cat_raw, dict):
            return {}
        leaf = ""
        for entry in _first_list(_first_dict(cat_raw.get("data")).get("item_list")):
            for cat in _first_list(_first_dict(entry).get("category_list")):
                if _first_dict(cat).get("leaf"):
                    leaf = str(_first_dict(cat).get("category_id") or "")
        if not leaf:
            return {}
        ok, _, demand_raw = adapter.get_category_demand_list(leaf)
        if not ok or not isinstance(demand_raw, dict):
            return {}
        demand_id = ""
        for entry in _first_list(_first_dict(demand_raw.get("data")).get("demand_list")):
            if _first_dict(entry).get("demand_id"):
                demand_id = str(_first_dict(entry)["demand_id"])
                break
        if not demand_id:
            return {}
        ok, _, detail_raw = adapter.get_demand_detail(demand_id)
        if not ok or not isinstance(detail_raw, dict):
            return {}
        detail = _first_dict(_first_dict(detail_raw.get("data")).get("demand_detail"))
        points = [
            str(_first_dict(p).get("selling_point_name") or "")
            for p in _first_list(detail.get("related_selling_point_list"))
            if _first_dict(p).get("selling_point_name") and not _first_dict(p).get("is_risk_word")
        ]
        directions = [
            str(_first_dict(d).get("name") or "")
            for d in _first_list(detail.get("creative_inspiration_list"))
            if _first_dict(d).get("name") and not _first_dict(d).get("is_risk_word")
        ]
        crowds = [
            str(_first_dict(c).get("crowd_name") or "")
            for c in _first_list(detail.get("crowd_profile_list"))
            if _first_dict(c).get("crowd_name")
        ]
        return {
            "selling_point_list": [p for p in points if p][:8],
            "direction_list": [d for d in directions if d][:20],
            "crowd": crowds[0] if crowds else "",
            "requirement": points[0] if points else "",
        }
    except Exception as error:  # noqa: BLE001 - 需求详情是增强项，任何失败都只降级不阻断
        logger.info("demand context unavailable for %s: %s", item_id, error)
        return {}


def _build_aigc_context(attributes: dict, demand: dict) -> dict:
    """组装 `aigc_context_d_t_o`：需求详情优先，attributes 兜底。"""
    data = _first_dict(attributes.get("data"))
    hot_topics = [
        str(_first_dict(t).get("topic") or "")
        for t in _first_list(_first_dict(data.get("topic_list")).get("topic_list_info"))
        if _first_dict(t).get("type") == 1 and _first_dict(t).get("topic")
    ]
    seller_words = [
        str(w)
        for w in _first_list(_first_dict(data.get("seller_point")).get("seller_point_info"))
        if w
    ]
    context = {
        "requirement": demand.get("requirement") or "",
        "crowd": demand.get("crowd") or "",
        "topic": demand.get("topic") or (hot_topics[0] if hot_topics else ""),
        "selling_point_list": demand.get("selling_point_list") or seller_words[:8],
        "direction_list": demand.get("direction_list") or [],
    }
    return context


def _run_aigc(adapter, item_id: str) -> dict:
    """三步生成链路 → `{title, content, topics:[{id,name}], task_id}`。

    任一步平台侧失败都抛 :class:`ProductNoteGenerationError`（502 语义），
    **不落库** —— 调用方把 `errMsg` 原文带给用户。
    """
    ok, message, attributes = adapter.aigc_publish_attributes(item_id)
    if not ok or not isinstance(attributes, dict):
        raise ProductNoteGenerationError("AI_GENERATION_FAILED", message or "获取商品生成属性失败")

    data = _first_dict(attributes.get("data"))
    sku_list = _first_list(data.get("sku_list"))
    sku = _first_dict(sku_list[0]) if sku_list else {}
    sku_id = str(sku.get("sku_id") or "")
    if not sku_id:
        raise ProductNoteGenerationError(
            "AI_GENERATION_FAILED", "平台没有返回该商品的 SKU 信息，无法生成笔记。"
        )
    sku_name = str(sku.get("sku_name") or sku.get("item_name") or "")

    context = _build_aigc_context(attributes, _demand_context(adapter, item_id))
    ok, message, started = adapter.sellerpoint_aigc(biz_id=sku_id, aigc_context=context)
    if not ok or not isinstance(started, dict):
        raise ProductNoteGenerationError("AI_GENERATION_FAILED", message or "发起笔记生成失败")
    task_id = str(_first_dict(started.get("data")).get("task_id") or "")
    if not task_id:
        raise ProductNoteGenerationError("AI_GENERATION_FAILED", "平台没有返回生成任务 ID")

    deadline = time.monotonic() + _POLL_TIMEOUT
    last_detail = ""
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL)
        ok, message, result = adapter.aigc_task_result(task_id)
        if not ok or not isinstance(result, dict):
            raise ProductNoteGenerationError("AI_GENERATION_FAILED", message or "查询生成结果失败")
        payload = _first_dict(result.get("data"))
        status = str(payload.get("task_status") or "")
        raw_detail = str(payload.get("task_detail") or "")
        if status == AIGC_TASK_STATUS_COMPLETED or (raw_detail and status != AIGC_TASK_STATUS_RUNNING):
            try:
                detail = json.loads(raw_detail) if raw_detail else {}
            except ValueError:
                raise ProductNoteGenerationError(
                    "AI_GENERATION_FAILED", f"平台返回的生成结果不是可解析的 JSON：{raw_detail[:200]}"
                )
            detail = _first_dict(detail)
            if not detail.get("success"):
                raise ProductNoteGenerationError(
                    "AI_GENERATION_FAILED",
                    str(detail.get("errMsg") or "平台生成失败（未给原因）"),
                )
            topics = [
                {"id": str(_first_dict(t).get("id") or ""), "name": str(_first_dict(t).get("name") or "")}
                for t in _first_list(detail.get("topics"))
                if _first_dict(t).get("name")
            ]
            return {
                "title": str(detail.get("title") or ""),
                "content": str(detail.get("content") or ""),
                "topics": topics,
                "task_id": task_id,
                "sku_id": sku_id,
                "sku_name": sku_name,
            }
        last_detail = raw_detail or last_detail
    raise ProductNoteGenerationError(
        "AI_GENERATION_FAILED",
        f"平台生成超时（{int(_POLL_TIMEOUT)}s 未出结果，最后状态：{last_detail[:120] or 'RUNNING'}）",
    )


def generate_note_for_item(
    db: Session,
    account,
    item_id: str,
    *,
    adapter_factory,
) -> dict:
    """为单个商品生成一篇待发布笔记并落库（设计 13 §31.4）。

    返回 `{ok, note_id, title, topics, guardrails, item_id}`；
    门禁 / 平台失败抛 :class:`ProductNoteGenerationError`（不落库）。

    门禁顺序（设计 13 §31.4 步 1）：**绑店 → ark 令牌 → 台账商品**。
    """
    if account_shop_id(db, account.id) is None:
        raise ProductNoteGenerationError(
            "QIANFAN_BINDING_REQUIRED", "尚未绑定千帆商家后台，无法生成商品笔记。请先绑定后再生成。"
        )
    cookies = qianfan_cookies_for_account(db, account.id)
    if not cookies:
        raise ProductNoteGenerationError(
            "QIANFAN_CREDENTIAL_MISSING", "该账号还没有千帆（ark）令牌，请先在浏览器登录千帆后导入。"
        )
    adapter = adapter_factory(cookies)
    row = _product_row(db, account, item_id)
    generated = _run_aigc(adapter, item_id)
    if not generated["title"] and not generated["content"]:
        raise ProductNoteGenerationError("AI_GENERATION_FAILED", "平台返回了空的标题与正文。")

    # 护栏：记录不拦截（设计 13 §31.4 步 4）—— 内容出自平台官方 AI。
    product_for_check = {
        "price_min_fen": row.price_min_fen,
        "price_max_fen": row.price_max_fen,
    }
    guardrails = run_guardrails(generated["title"], generated["content"], product_for_check)

    images = _product_images(row)
    video = _product_video(row)
    # 媒体取舍（§32.2.1）：**视频优先** —— 命中视频只落 1 个视频、不落图片；
    # 否则落前 6 张商品图。两种形态互斥，不存在「视频 + 图片混排」。
    media: list[dict] = (
        [{"asset_type": "video", "url": video["url"], "sort_order": 0}]
        if video
        else [
            {"asset_type": "image", "url": image["url"], "sort_order": index}
            for index, image in enumerate(images)
        ]
    )
    local_note_id = _local_note_id()
    note = Note(
        user_id=account.user_id,
        platform_account_id=account.id,
        platform="xhs",
        note_id=local_note_id,
        title=generated["title"],
        content=generated["content"],
        author_name="",
        note_type=NOTE_TYPE_LOCAL_GENERATED,
        topics_json=json.dumps([t["name"] for t in generated["topics"]], ensure_ascii=False),
        raw_json={
            # sku_id / sku_name 是发布链路的 `goodsInfo` / `bizRelations` 必需项
            # （平台用 sku 粒度挂链，不用 item 粒度；bundle 实抓）—— 生成时存下，
            # 发布时直接取，不再回头打平台。
            "aigc": {
                "task_id": generated["task_id"],
                "topics": generated["topics"],
                "sku_id": generated["sku_id"],
                "sku_name": generated["sku_name"],
            },
            "guardrails": guardrails.as_dict(),
            "item_id": item_id,
        },
    )
    # 状态与 `published` 投影**一次写入**（设计 13 §32.3.2 的唯一入口）：
    # 生成 ⇒ 待发布。生成链路到此为止 —— **不调用发布链路的任何一步**（§32.2.2）。
    set_note_publish_status(note, PUBLISH_STATUS_PENDING)
    db.add(note)
    db.flush()  # 先拿 note.id（资产 / 关系的外键）

    from backend.app.models.note import NoteAsset  # 局部导入避免环

    for entry in media:
        db.add(
            NoteAsset(
                note_id=note.id,
                asset_type=entry["asset_type"],
                url=entry["url"],
                local_path="",
                sort_order=entry["sort_order"],
            )
        )
    db.add(NoteGoods(note_id=note.id, product_id=row.id))
    db.commit()

    return {
        "ok": True,
        "note_id": local_note_id,
        "note_pk": note.id,
        "title": generated["title"],
        "topics": [t["name"] for t in generated["topics"]],
        "guardrails": guardrails.as_dict(),
        "item_id": item_id,
        "image_count": sum(1 for entry in media if entry["asset_type"] == "image"),
        "video_count": sum(1 for entry in media if entry["asset_type"] == "video"),
    }


def batch_generate_notes(
    db: Session,
    account,
    item_ids: list[str],
    *,
    adapter_factory,
    max_items: int = 20,
) -> dict:
    """批量生成（顺序处理，单项失败不阻断；设计 13 §31.4 步 6）。

    返回 `{total, succeeded, failed, results:[{item_id, ok, note_id?, title?, error?}]}`。
    """
    cleaned = [str(i).strip() for i in item_ids if str(i).strip()]
    distinct = list(dict.fromkeys(cleaned))[:max_items]
    results: list[dict] = []
    for item_id in distinct:
        try:
            outcome = generate_note_for_item(db, account, item_id, adapter_factory=adapter_factory)
            results.append(
                {
                    "item_id": item_id,
                    "ok": True,
                    "note_id": outcome["note_id"],
                    "title": outcome["title"],
                }
            )
        except ProductNoteGenerationError as error:
            results.append({"item_id": item_id, "ok": False, "error": error.message, "code": error.code})
    succeeded = sum(1 for r in results if r["ok"])
    return {"total": len(distinct), "succeeded": succeeded, "failed": len(distinct) - succeeded, "results": results}
