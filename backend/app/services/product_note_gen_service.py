"""商品笔记智能生成（设计 13 §31，2026-09-23 用户口径；§31.10 起走队列）。

## 这个模块做什么（其余不归它管）

| 做 | 不归它 |
|---|---|
| ① 调千帆「智能生成笔记」三步链路（attributes → sellerpoint/aigc → 轮询） | 发布（`note_publish_service`） |
| ② 把生成结果落库：`notes`（`local_generated`/`pending`）+ `note_assets`（商品图）+ `note_goods`（关系） | 干运行预览（`product_notes.py::generate` 端点原样保留） |
| ③ 护栏**记录不拦截**（内容出自平台官方 AI，与本地模型不同链） | 话题增删入口（设计 13 §29 已有） |
| ④ **单 / 批量生成都走队列**（§31.10）：端点只做门禁 + 入队即返回，worker 逐篇跑 | 前端轮询（`GET /xhs/product-notes/generate-queue`） |

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

## 队列化（§31.10，2026-09-25 用户口径「单独生成商品笔记和批量生成商品笔记均需走队列」）

单篇生成要跑「三步 + 轮询」~6–21s，批量 20 篇就是数分钟 —— 同步段跑完会把 HTTP
请求挂死（前端 120s 超时），且两个入口并存时各打各的平台请求。因此与发布 / 同步
队列**同构**：`start_generate_note` / `start_batch_generate` 只做门禁（绑店 → ark
令牌 → 台账商品，都是确定性答案 ⇒ 失败仍 400/404 结构化）+ 入队即返回；一个
`note-generate-worker` 守护线程逐篇跑 `generate_note_for_item`（顺序执行 = 节流
第二道保障）。队列底座复用 `note_queue_service`（`tasks` 表），队列项**不绑定笔记**
（生成前笔记不存在）⇒ 行上存 `item_id` / `item_name`，终态后补 `note_id` /
`note_title`。看门狗 / 停止出口 / 重启续跑与另两个队列同型（DEF-069 同款缺口）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from apis.xhs_qianfan_apis import (
    AIGC_TASK_STATUS_COMPLETED,
    AIGC_TASK_STATUS_RUNNING,
)
from backend.app.core.database import SessionLocal
from backend.app.models import Task
from backend.app.models.note import (
    NOTE_TYPE_LOCAL_GENERATED,
    PUBLISH_STATUS_PENDING,
    Note,
    set_note_publish_status,
)
from backend.app.models.platform_account import PlatformAccount
from backend.app.models.shop import NoteGoods
from backend.app.models.shop_product import ShopProduct
from backend.app.services import note_queue_service
from backend.app.services.product_note_service import run_guardrails
from backend.app.services.note_topic_service import apply_sync_topics
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

# --------------------------------------------------------------------------- #
# 生成队列（设计 13 §31.10，2026-09-25 用户口径「单独 / 批量生成均需走队列」）
# --------------------------------------------------------------------------- #
#: 批量生成单次上限（个）。批量是显式动作，超限分多次即可（与发布 / 同步同值）。
GENERATE_BATCH_MAX_ITEMS = 20

#: 生成队列空闲轮询间隔（秒）。唤醒信号只是**提示**，这个超时是兜底：
#: 任何一次 `set` 丢失，最坏也只晚这么久发现新任务，不会永久停摆（DEF-048）。
GEN_IDLE_POLL_SECONDS = 5.0

#: worker 线程名（探针与重建都认它；DEF-069 同型：存活判据只能是**线程在不在**）。
GENERATE_WORKER_THREAD_NAME = "note-generate-worker"

#: 看门狗巡检间隔 / 重建失败指数退避上限（与同步 / 发布 worker、调度器守活同值）。
GEN_WATCHDOG_INTERVAL_SECONDS = 30.0
GEN_WATCHDOG_MAX_BACKOFF_SECONDS = 600.0

#: 队列持久化（设计 13 §35.9 同型）：真相源是 `tasks` 表里的 `xhs_note_gen_item` 行，
#: **内存里不再有队列 / 结果 / 序号** —— 只留一个唤醒信号 `_gen_wakeup`。
_gen_wakeup = threading.Event()
_gen_queue_lock = threading.Lock()
_gen_worker: Optional[threading.Thread] = None
#: worker 收尾 / 测试专用出口（置位 + set wakeup ⇒ 不再认领新任务即退；生产不置位）。
_gen_worker_stop = threading.Event()
_gen_watchdog_stop = threading.Event()

#: 入队时的 adapter 工厂缓存（`tasks.id -> factory`），与发布队列同型：
#: 端点的工厂是 FastAPI 依赖注入（测试 override 出来的假适配器），worker 线程拿不到
#: 注入层，只能由入队路径带进来。⚠️ **是缓存不是真相源**：进程重启后为空，
#: `_default_generate_adapter_factory()` 回落到默认千帆工厂 —— 那正是续跑该走的来源
#: （凭据按库里当前有效的 ark 令牌现取，§18.14）。跑完即 pop，不无限增长。
_gen_adapter_factories: dict[int, Any] = {}

#: 后台线程自建 Session 的工厂（测试注入临时库的 sessionmaker）。
_session_factory = SessionLocal


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


def _gate_for_generation(db: Session, account, item_id: str) -> tuple[ShopProduct, str]:
    """生成门禁（§31.4 步 1-2）：绑店 → ark 令牌 → 台账商品行。

    **单 / 批量入队与 worker 重跑共用这一把尺子**（DEF-044）：三项都是**确定性
    答案**（本地库可判），不过 ⇒ 抛 :class:`ProductNoteGenerationError`，调用方
    按 400/404 结构化呈现。worker 重跑时再走一遍 —— 凭据 / 台账可能在排队期间
    变化（用户补令牌 / 同步商品），现取现判。

    返回 `(台账商品行, ark 令牌)`：调用方直接用这份令牌造 adapter，不在门禁里
    构造 adapter（门禁失败时一次平台对象都不该造）。
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
    return _product_row(db, account, item_id), cookies


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

    门禁顺序（设计 13 §31.4 步 1）：**绑店 → ark 令牌 → 台账商品**
    （唯一实现 :func:`_gate_for_generation`，队列入口与 worker 共用）。
    """
    row, cookies = _gate_for_generation(db, account, item_id)
    adapter = adapter_factory(cookies)
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

    # §34：AI 正文若自带 `#话题` ⇒ 拆进 topics 表 + note_topics 关系（正文不再存
    # 话题）；平台 AI 话题（`aigc.topics[{id,name}]`）是权威声明，`platform_id`
    # 存平台话题 ID —— 发布链路 `hash_tag` 直接取，不再回头打平台。
    apply_sync_topics(
        db,
        note,
        [t["name"] for t in generated["topics"]],
        platform_ids={
            str(t.get("name") or ""): str(t.get("id") or "")
            for t in generated["topics"]
            if isinstance(t, dict) and t.get("name") and t.get("id")
        },
    )

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
    # ⚠️ 2026-09-27（§26.7）：关系行按**平台 ID** 落（`note.note_id` 是本地生成的
    # `local_…` 合成 ID，发布成功后由 `_move_note_goods_relations` 迁到平台真 ID）。
    db.add(
        NoteGoods(
            user_id=note.user_id,
            note_id=note.note_id,
            item_id=str(row.item_id or ""),
        )
    )
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


def start_generate_note(
    db: Session,
    user_id: int,
    account,
    item_id: str,
    *,
    adapter_factory=None,
) -> dict:
    """单商品生成**入队即返回**（设计 13 §31.10；端点调）。

    同步段只做门禁（:func:`_gate_for_generation`，确定性答案 ⇒ 不过仍 400/404）
    + 入队；三步生成 + 轮询 + 落库在后台 worker 跑。立即返回入队回执
    `{ok, status:"queued", item_id, already_queued?}`；**终态与真实原因不在这里**
    —— 走 `GET /xhs/product-notes/generate-queue` 快照 `finished`（与批量同一个出口）。

    幂等：同用户未终态队列里已有同 `item_id` ⇒ `already_queued: true`（不重复生成）。
    """
    row, _cookies = _gate_for_generation(db, account, item_id)
    # 一次动作 = 一个批次（2026-09-26 用户口径「批次逻辑不再移除，生成唯一批次号」）：
    # 单商品点击也开新批次号（一条明细），不复用已有批次。
    generate_batch = note_queue_service.BatchRequest(
        user_id=user_id,
        item_task_type=note_queue_service.TASK_TYPE_GEN_ITEM,
        source=note_queue_service.SOURCE_PRODUCT_NOTE_GENERATE_SINGLE,
    )
    task_row, created = note_queue_service.enqueue_item(
        db,
        user_id=user_id,
        item_task_type=note_queue_service.TASK_TYPE_GEN_ITEM,
        account_id=account.id,
        item_id=item_id,
        item_name=str(getattr(row, "item_title", "") or ""),
        source=note_queue_service.SOURCE_PRODUCT_NOTE_GENERATE_SINGLE,
        batch=generate_batch,
    )
    if created and adapter_factory is not None:
        with _gen_queue_lock:
            _gen_adapter_factories[task_row.id] = adapter_factory
    db.commit()
    # ⚠️ 唤醒放在**提交之后**：worker 自建 Session，看不到未提交的行（§35.9 同型）。
    ensure_generate_worker()
    logger.info("product note generate enqueued (item_id=%s, created=%s)", item_id, created)
    return {
        "ok": True,
        "status": "queued",
        "item_id": item_id,
        **({} if created else {"already_queued": True}),
    }


def start_batch_generate(
    db: Session,
    user_id: int,
    account,
    item_ids: list[str],
    *,
    adapter_factory=None,
    max_items: int = GENERATE_BATCH_MAX_ITEMS,
) -> dict:
    """批量生成入队（设计 13 §31.10；端点调）：逐项过门禁，通过项按序入队。

    **一个** worker 顺序跑完队列（顺序执行 = 平台侧节流的第二道保障）。立即返回
    `{total, started, failed, results}`：通过项 `{ok:true, status:"queued",
    already_queued?}`，门禁失败项 `{ok:false, error, code}`（不阻断其余，与千帆页面
    `Promise.allSettled` 同语义）。终态走前端轮询（与单篇同一个出口）。

    来源统一标 `product_note_generate_batch`（按钮级，2026-09-26 用户口径
    「来源要具体到按钮」）。
    """
    cleaned = [str(i).strip() for i in item_ids if str(i).strip()]
    distinct = list(dict.fromkeys(cleaned))[:max_items]
    # 一次批量 = 一个批次（懒创建：全部门禁不过 ⇒ 不留空批次行）
    generate_batch = note_queue_service.BatchRequest(
        user_id=user_id,
        item_task_type=note_queue_service.TASK_TYPE_GEN_ITEM,
        source=note_queue_service.SOURCE_PRODUCT_NOTE_GENERATE_BATCH,
    )
    results: list[dict] = []
    for item_id in distinct:
        try:
            row, _cookies = _gate_for_generation(db, account, item_id)
        except ProductNoteGenerationError as error:
            results.append({"item_id": item_id, "ok": False, "error": error.message, "code": error.code})
            continue
        task_row, created = note_queue_service.enqueue_item(
            db,
            user_id=user_id,
            item_task_type=note_queue_service.TASK_TYPE_GEN_ITEM,
            account_id=account.id,
            item_id=item_id,
            item_name=str(getattr(row, "item_title", "") or ""),
            source=note_queue_service.SOURCE_PRODUCT_NOTE_GENERATE_BATCH,
            batch=generate_batch,
        )
        if created and adapter_factory is not None:
            with _gen_queue_lock:
                _gen_adapter_factories[task_row.id] = adapter_factory
        results.append(
            {
                "item_id": item_id,
                "ok": True,
                "status": "queued",
                **({} if created else {"already_queued": True}),
            }
        )
    db.commit()
    ensure_generate_worker()  # 同上：提交后才唤醒
    started = sum(1 for r in results if r.get("ok"))
    logger.info("product note generate batch enqueued (total=%s, started=%s)", len(distinct), started)
    return {"total": len(distinct), "started": started, "failed": len(distinct) - started, "results": results}


# --------------------------------------------------------------------------- #
# 生成 worker（与发布 / 同步 worker 同型：单线程 FIFO + 看门狗 + 停止出口）
# --------------------------------------------------------------------------- #
def ensure_generate_worker() -> None:
    """确保有一条活着的 worker（惰性起；已活则只唤醒它）。

    公开（不是 `_` 前缀）：**启动恢复**也要用它 —— `main.py::lifespan` 复位完
    中断行后立刻唤醒，把上次进程留下的 `pending` 项接着跑（否则要等看门狗 30 秒
    后巡检才发现「没 worker」）。
    """
    with _gen_queue_lock:
        if _gen_worker is None or not _gen_worker.is_alive():
            _spawn_generate_worker()
    _gen_wakeup.set()


def _gen_worker_loop() -> None:
    """DB 队列 worker：醒来 → 认领一条 → 跑一条 → 再认领 …… 认不到就回去等。

    ⚠️ 唤醒信号只是**提示**：`wait(timeout=GEN_IDLE_POLL_SECONDS)` 带兜底超时
    ⇒ 任何一次 `set` 丢失，最坏也只晚 5 秒发现新任务，不会永久停摆（DEF-048）。

    ⚠️ DEF-069 同型缺口（本模块从一开始就补齐四件）：篇内 `except BaseException`
    网、循环外层第二层网、看门狗（`start_generate_worker_watchdog`）、停止出口
    （`stop_generate_worker`）。`_gen_worker_stop` 是**收尾 / 测试专用出口**：
    置位（并 `set` 唤醒）后**不再认领新任务**即退。生产永不置位。⚠️「不丢正在
    排队的项」由**库**保证（未跑的项是 `pending` 行，下次启动接着跑）。
    """
    while not _gen_worker_stop.is_set():
        _gen_wakeup.wait(timeout=GEN_IDLE_POLL_SECONDS)
        _gen_wakeup.clear()
        while not _gen_worker_stop.is_set():
            item_id = _claim_one_generate_item()
            if item_id is None:
                break
            try:
                _run_one_generate(item_id)
            except BaseException as error:  # noqa: BLE001 - 线程不死（DEF-069）
                logger.error(
                    "note generate worker: run escaped the inner net (task=%s): %s",
                    item_id,
                    error,
                    exc_info=True,
                )


def _claim_one_generate_item() -> Optional[int]:
    """认领一条待生成项，返回其 `tasks.id`（无活可干 / 认领出错 ⇒ None）。

    认领用**独立 Session**（`claim_next_item` 内部提交换状态，用完即关）；认领期
    的 DB 异常在这里收住 —— 认领失败只是这一轮空转（外层 `wait(timeout=…)` 天然
    退避），不许把 worker 带走。
    """
    db = _session_factory()
    try:
        item = note_queue_service.claim_next_item(
            db, item_task_type=note_queue_service.TASK_TYPE_GEN_ITEM
        )
        return int(item.id) if item is not None else None
    except BaseException:  # noqa: BLE001 - 认领失败退避，不死 worker
        logger.exception("note generate worker: claim failed")
        return None
    finally:
        db.close()


def _default_generate_adapter_factory() -> Any:
    """worker 重建 adapter 的**默认**工厂（进程重启续跑走的来源）。

    惰性导入：`get_qianfan_api_adapter_factory()` 定义在适配器模块（避免与路由层
    成环），这里按需取，不在 import 期固化。
    """
    from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory

    return get_qianfan_api_adapter_factory()


def _run_one_generate(item_id: int) -> None:
    """跑一条（按 `tasks.id`）：自建 Session；任何漏网异常都不许把队列卡死。

    安全网不条件性（DEF-048）：**任何**异常路径都必须把这条写成终态，否则它
    永远挂在 `running` 上（进度卡「生成中」永远转圈）。凭据**按库里当前有效**
    的 ark 令牌现取（`generate_note_for_item` 内部走同一把门禁尺子，§18.14：
    拿不到就是失败，**不借会话、不发探活**）。
    """
    db = _session_factory()
    try:
        item = db.get(Task, item_id)
        if item is None:
            return  # 行已被清理任务删掉 / 不存在：无事可做
        payload = item.payload if isinstance(item.payload, dict) else {}
        gen_item_id = str(payload.get("item_id") or "")
        account_pk = int(payload.get("account_id") or 0)
        account = db.get(PlatformAccount, account_pk) if account_pk else None
        if not gen_item_id or account is None:
            note_queue_service.finish_item(
                db, item, ok=False, reason="队列项缺少商品或账号信息", code="GEN_ITEM_INVALID"
            )
            return
        with _gen_queue_lock:
            factory = _gen_adapter_factories.get(item_id)
        try:
            outcome = generate_note_for_item(
                db, account, gen_item_id, adapter_factory=factory or _default_generate_adapter_factory()
            )
        except ProductNoteGenerationError as error:
            note_queue_service.finish_item(db, item, ok=False, reason=error.message, code=error.code)
            return
        note_queue_service.finish_item(
            db,
            item,
            ok=True,
            # 成功后把「生成了哪篇笔记」补进行体：快照 / 详情抽屉直接显示，
            # 不回头反查（缺值口径：拿不到就不写，不造假）。
            extra={"note_id": outcome["note_id"], "note_title": outcome["title"]},
        )
    except BaseException as error:  # noqa: BLE001 - 兜底安全网：SystemExit 也不许把队列卡死
        logger.warning("note generate worker crashed (task=%s): %s", item_id, error)
        try:
            db.rollback()  # 失败的 commit 留待回滚状态；不回滚则恢复动作也会炸
        except Exception:  # noqa: BLE001 - 恢复也失败只能留痕，不能再抛
            logger.exception("note generate rollback failed (task=%s)", item_id)
        try:
            item = db.get(Task, item_id)
            if item is not None:
                note_queue_service.finish_item(
                    db,
                    item,
                    ok=False,
                    reason=f"{type(error).__name__}: {error}",
                    code="GEN_FAILED",
                )
        except Exception:  # noqa: BLE001 - 连记账都失败：留痕，行留 running 由启动恢复捡回
            logger.exception("note generate failed to record failure (task=%s)", item_id)
    finally:
        db.close()
        with _gen_queue_lock:
            _gen_adapter_factories.pop(item_id, None)


def count_open_generate_items(db: Session) -> int:
    """还有多少条待生成 / 在生成（测试与启动判断用）。"""
    return note_queue_service.count_open_items(
        db, item_task_type=note_queue_service.TASK_TYPE_GEN_ITEM
    )


def _spawn_generate_worker() -> None:
    """起一条新的 worker 线程（调用方持 `_gen_queue_lock`）。

    起新世代时清掉历史停止位 —— 重建出来的 worker 不该被上一世代的收尾位挡住。
    """
    global _gen_worker
    _gen_worker_stop.clear()
    _gen_worker = threading.Thread(
        target=_gen_worker_loop,
        name=GENERATE_WORKER_THREAD_NAME,
        daemon=True,
    )
    _gen_worker.start()


def generate_worker_thread_alive() -> bool:
    """进程里有没有**活着的** `note-generate-worker` 线程（唯一可信存活判据，DEF-069）。

    为什么不看 `_gen_worker` 模块变量：线程带异常 / `SystemExit` 死后它仍指着那个
    `Thread` 对象，而 `ensure_generate_worker` 只在**入队时**检查一次 —— 队列跑起来
    之后线程死了，没有任何现存路径会发现（同 scheduler DEF-067、同步 worker DEF-069
    的教训：状态位会骗人，线程本身不会）。
    """
    return any(
        thread.name == GENERATE_WORKER_THREAD_NAME and thread.is_alive()
        for thread in threading.enumerate()
    )


def stop_generate_worker(*, timeout: float = 5.0) -> None:
    """让当前 worker **立即停手退出**（**测试 / 进程收尾专用**，生产不调用）。

    只置停止位不够：worker 常态卡在 `_gen_wakeup.wait(timeout=…)` 上，必须再
    `set` 一下 wakeup 才能唤醒它回来检查停止位。唤醒后它**不再认领新任务**、手上
    那条跑完即退。⚠️「不丢正在排队的项」由**库保证**（未跑的项还是 `pending` 行），
    不靠 worker 排空。
    """
    _gen_worker_stop.set()
    _gen_wakeup.set()
    worker = _gen_worker
    if worker is not None and worker.is_alive():
        worker.join(timeout=timeout)


def _revive_generate_worker() -> None:
    """worker 线程死了 ⇒ 复位它卡住的那条 + 重建 worker（不丢项）。

    线程死后，它认领的那条 `tasks` 行**永远停在 `running`** ⇒ 不复位的话它既不会
    被重跑、也永远算在「生成中」里。复位就是把它改回 `pending`
    （`recover_interrupted` 只动 `running` 行）。

    ⚠️ **顺序：先复位、再起新 worker**。反过来新 worker 可能刚认领一条就被复位
    抢回 `pending` ⇒ 同一条被跑两遍。
    """
    recovered = {"requeued": 0, "failed": 0}
    try:
        db = _session_factory()
        try:
            recovered = note_queue_service.recover_interrupted(
                db, item_task_types=(note_queue_service.TASK_TYPE_GEN_ITEM,)
            )
        finally:
            db.close()
    except BaseException:  # noqa: BLE001 - 复位失败也要把 worker 建起来
        logger.exception("生成 worker 重建：中断行复位失败（继续重建 worker）")
    with _gen_queue_lock:
        if _gen_worker is None or not _gen_worker.is_alive():
            _spawn_generate_worker()
    _gen_wakeup.set()  # 新 worker 可能已在 wait 上：不等下一批也要立刻跑积压
    logger.error(
        "note-generate-worker 已死 —— 已重建（中断行 requeued=%s failed=%s）",
        recovered.get("requeued"),
        recovered.get("failed"),
    )


def start_generate_worker_watchdog(
    *, interval: float = GEN_WATCHDOG_INTERVAL_SECONDS
) -> threading.Thread:
    """守活线程：worker 死了就重建（DEF-069 同型缺口，2026-09-25）。

    与同步 / 发布 worker 同型：单线程组件没有自愈，任何够不着的死法（`SystemExit`
    之类，`except Exception` 抓不到、threading 对它静默）都是永久停摆。看门狗
    自己绝不被一次失败带走（`except BaseException` + 失败指数退避，上限
    `GEN_WATCHDOG_MAX_BACKOFF_SECONDS`）。进程收尾调
    :func:`stop_generate_worker_watchdog`。
    """
    _gen_watchdog_stop.clear()

    def _loop() -> None:
        backoff = interval
        while not _gen_watchdog_stop.is_set():
            if _gen_watchdog_stop.wait(backoff):  # 可被 stop 打断的等待
                return
            try:
                if generate_worker_thread_alive():
                    backoff = interval
                    continue
                _revive_generate_worker()
                backoff = interval
            except BaseException as error:  # noqa: BLE001 - 看门狗自己也不能死
                logger.exception("生成 worker 看门狗重建失败，%.0f 秒后重试", backoff)
                backoff = min(backoff * 2, GEN_WATCHDOG_MAX_BACKOFF_SECONDS)

    thread = threading.Thread(target=_loop, name="generate-worker-watchdog", daemon=True)
    thread.start()
    return thread


def stop_generate_worker_watchdog() -> None:
    """让看门狗退出巡检（幂等；下一次 `start_generate_worker_watchdog` 自动复位）。"""
    _gen_watchdog_stop.set()


# --------------------------------------------------------------------------- #
# 快照（进度卡）与测试辅助
# --------------------------------------------------------------------------- #
def generate_queue_snapshot(
    db: Session, user_id: int, *, platform: Optional[str] = None
) -> dict:
    """生成进度快照（商品列表页「生成进度卡」；`GET /xhs/product-notes/generate-queue`）。

    2026-09-26 常驻化（用户口径「不需要队列批次，队列是常驻」）：形状与另两个
    队列对齐（前端三卡同一套轮询骨架）—— `running` / `queued`（FIFO 序）+
    `total`（= 两者 + 近期终态数）+ `finished`（近期终态项的逐项 `ok` 与
    `reason` —— 生成失败不落库到笔记行，原因只在这里给一次）。

    **不再有 `generated` / `failed` 计数字段**：「本轮成败」由前端按自己入队的
    商品累计（后端不知道用户这一轮点了什么）。
    """
    raw = note_queue_service.snapshot(
        db, user_id=user_id, item_task_type=note_queue_service.TASK_TYPE_GEN_ITEM, platform=platform
    )
    return {
        "running": raw["running"],
        "queued": raw["queued"],
        "total": raw["total"],
        "finished": raw["finished"],
    }


def wait_generate_done(timeout: float = 30.0) -> bool:
    """等生成队列跑空（测试用；判定落在库上 —— 不信内存位）。

    与 `wait_publish_done(note_pk)` 的差别：生成项**没有 note_pk**（生成前笔记
    不存在）⇒ 按「没有未终态生成项」判定。worker 是单线程 FIFO，队列空 = 本轮
    全部出终态。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db = _session_factory()
        try:
            if count_open_generate_items(db) == 0:
                return True
        finally:
            db.close()
        time.sleep(0.1)
    return False
