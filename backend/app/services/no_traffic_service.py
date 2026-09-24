"""无流量笔记候选查询（**纯本地**，零平台请求）。

无流量口径：本地 ``shop_note_goods.entry_json`` 里千帆同步时落库的三项指标——
阅读 ``browse_num``、支付订单数 ``note_pay_order_cnt``、支付金额 ``note_pay_gmv``——
全部**小于等于阈值**即判为无流量。

三项阈值**全部可配置**，不写死为 0（用户 2026-09-19 明确要求）。缺值（未同步 /
未绑定千帆的笔记）**不进候选池**，因为「没有数据」不等于「没有流量」。

候选池边界（诚实说明，不造假）：
* 只有 ``shop_note_goods`` 里**已有映射**的笔记才可能进入候选池。
* 未同步或未绑定千帆的笔记不在池内 —— 本地统计口径本身就不全面，这是用户
  明确接受过的已知边界。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.time import shanghai_now
from backend.app.models import PlatformAccount
from backend.app.models.shop import ShopNoteGoods
from backend.app.services.shop_binding_service import account_shop_id

#: 三项指标的默认阈值（用户可覆盖，**不写死 0**）。
DEFAULT_MAX_BROWSE = 0
DEFAULT_MAX_PAY_ORDER = 0
DEFAULT_MAX_PAY_GMV = 0.0

#: 单批清理上限（平台写操作风控：一次最多删这么多篇）。
DEFAULT_BATCH_LIMIT = 20

#: 千帆标题搜索一页取多少条（搜索是过滤前置，命中即落库补指标）。
SEARCH_PAGE_SIZE = 50


def _as_number(value: Any) -> Optional[float]:
    """千帆数值字段可能是 int / float / 字符串 / None；统一成 float 或 None。"""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass
class CleanupQuery:
    """清理/预览的搜索条件（与阈值共同决定候选池）。

    全部可选、可组合；空值 = 该条件不生效。这是「按搜索条件清理」的过滤前置。
    """

    #: 标题关键词（子串匹配，不区分大小写）。
    keyword: str = ""
    #: 笔记类型：``""`` 不限 / ``normal`` 图文 / ``video`` 视频。
    note_type: str = ""
    #: 只清理发布时间早于该 Unix 时间戳（秒）的笔记；``None`` 不限。
    published_before: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "CleanupQuery":
        if data is None:
            return cls()
        if hasattr(data, "model_dump"):
            data = data.model_dump()
        if not isinstance(data, dict):
            return cls()
        keyword = str(data.get("keyword") or "").strip()
        note_type = str(data.get("note_type") or "").strip()
        raw_before = data.get("published_before")
        published_before = int(raw_before) if _as_number(raw_before) is not None else None
        return cls(keyword=keyword, note_type=note_type, published_before=published_before)

    def to_dict(self) -> dict[str, Any]:
        return {
            "keyword": self.keyword,
            "note_type": self.note_type,
            "published_before": self.published_before,
        }

    def matches(self, candidate: "CleanupCandidate") -> bool:
        """搜索条件过滤（**不含阈值**，阈值另见 :func:`is_no_traffic`）。"""
        if self.keyword:
            haystack = f"{candidate.title} {candidate.raw.get('display_title', '')}"
            if self.keyword.lower() not in haystack.lower():
                return False
        if self.note_type:
            entry_type = str(candidate.raw.get("type") or candidate.raw.get("note_type") or "")
            if entry_type.lower() != self.note_type.lower():
                return False
        if self.published_before is not None:
            if candidate.publish_time is None or candidate.publish_time >= self.published_before:
                return False
        return True


@dataclass
class TrafficThresholds:
    """无流量判定阈值，每个指标都可单独配。"""

    max_browse: int = DEFAULT_MAX_BROWSE
    max_pay_order: int = DEFAULT_MAX_PAY_ORDER
    max_pay_gmv: float = DEFAULT_MAX_PAY_GMV

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "TrafficThresholds":
        if data is None:
            return cls()
        # 允许传 pydantic 模型（FastAPI 端点参数）或裸 dict。
        if hasattr(data, "model_dump"):
            data = data.model_dump()
        if not isinstance(data, dict):
            return cls()
        return cls(
            max_browse=_int_field(data, "max_browse", DEFAULT_MAX_BROWSE),
            max_pay_order=_int_field(data, "max_pay_order", DEFAULT_MAX_PAY_ORDER),
            max_pay_gmv=_float_field(data, "max_pay_gmv", DEFAULT_MAX_PAY_GMV),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_browse": self.max_browse,
            "max_pay_order": self.max_pay_order,
            "max_pay_gmv": self.max_pay_gmv,
        }


@dataclass
class CleanupCandidate:
    note_id: str
    title: str
    cover: str
    browse_num: Optional[int]
    browse_num_30d: Optional[int]
    likes: Optional[int]
    collects: Optional[int]
    comments: Optional[int]
    shares: Optional[int]
    pay_gmv: Optional[float]
    pay_order_cnt: Optional[int]
    publish_time: Optional[int]
    synced_at: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _int_field(data: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(data.get(key, default))
    except (TypeError, ValueError):
        return default


def _float_field(data: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(data.get(key, default))
    except (TypeError, ValueError):
        return default


def _entry_metrics(entry_json: str) -> dict[str, Any]:
    """解析 ``shop_note_goods.entry_json`` 为展示字段（失败给空 dict，不抛）。"""
    if not entry_json:
        return {}
    try:
        parsed = json.loads(entry_json)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_no_traffic(entry: dict[str, Any], thresholds: TrafficThresholds) -> bool:
    """三项指标**全部** 小于等于阈值 → 无流量。

    任一指标缺失（本地没有该笔记的同步数据）→ 返回 ``False``：缺值不是无流量。
    """
    browse = _as_number(entry.get("browse_num"))
    pay_cnt = _as_number(entry.get("note_pay_order_cnt"))
    pay_gmv = _as_number(entry.get("note_pay_gmv"))
    if browse is None or pay_cnt is None or pay_gmv is None:
        return False
    return (
        browse <= thresholds.max_browse
        and pay_cnt <= thresholds.max_pay_order
        and pay_gmv <= thresholds.max_pay_gmv
    )


def list_no_traffic_candidates(
    db: Session,
    account_id: int,
    thresholds: Optional[TrafficThresholds] = None,
    limit: int = DEFAULT_BATCH_LIMIT,
    query: Optional[CleanupQuery] = None,
) -> dict[str, Any]:
    """列出无流量候选。

    两条路径，由 ``query`` 决定：

    * **无搜索条件**（``query`` 为空或 keyword 为空）：**纯本地查询**，零平台请求，
      直接在已同步映射里按阈值筛。
    * **有搜索条件**：先调千帆 ``mix_search_word`` 做**服务端标题搜索**（千帆笔记管理页
      同款接口，2026-09-19 实测），把命中的笔记**落库**（补上指标），再在本地按阈值筛。
      这就是「按搜索条件 + 阈值」共同决定候选池。

    返回 ``{candidates, total, thresholds, query, coverage}``：
    * ``candidates`` — 最多 ``limit`` 条（清理批次有上限，见 ``DEFAULT_BATCH_LIMIT``）；
    * ``total`` — 符合条件的总数（分页只影响返回条数，不影响计数）；
    * ``coverage`` — 候选池覆盖率说明，让前端讲清边界。
    """
    thresholds = thresholds or TrafficThresholds()
    query = query or CleanupQuery()
    if account_shop_id(db, account_id) is None:
        return {
            "candidates": [],
            "total": 0,
            "thresholds": thresholds.to_dict(),
            "query": query.to_dict(),
            "coverage": {"synced_notes": 0, "pool_total": 0, "reason": "未绑定千帆店铺"},
        }

    # 有搜索条件：先服务端搜 + 落库，再本地筛（搜索是过滤前置）。
    searched = 0
    search_error = ""
    if query.keyword:
        searched, search_error = _search_and_store(db, account_id, query.keyword)

    rows = db.scalars(
        select(ShopNoteGoods)
        .where(ShopNoteGoods.platform_account_id == account_id)
        .order_by(ShopNoteGoods.synced_at.desc())
    ).all()

    matched: list[CleanupCandidate] = []
    for row in rows:
        entry = _entry_metrics(row.entry_json)
        if not entry or not is_no_traffic(entry, thresholds):
            continue
        candidate = CleanupCandidate(
            note_id=str(row.note_id or ""),
            title=str(entry.get("note_title") or row.item_title or ""),
            cover=str(entry.get("note_image") or row.item_image or ""),
            browse_num=_as_number(entry.get("browse_num")),
            browse_num_30d=_as_number(entry.get("note_browse_num30d")),
            likes=_as_number(entry.get("like_num")),
            collects=_as_number(entry.get("collect_num")),
            comments=_as_number(entry.get("comment_num")),
            shares=_as_number(entry.get("share_num")),
            pay_gmv=_as_number(entry.get("note_pay_gmv")),
            pay_order_cnt=_as_number(entry.get("note_pay_order_cnt")),
            publish_time=(
                int(entry["publish_time"])
                if _as_number(entry.get("publish_time")) is not None
                else None
            ),
            synced_at=row.synced_at.strftime("%Y-%m-%d %H:%M:%S") if row.synced_at else "",
            raw=entry,
        )
        # 搜索条件过滤（note_type / published_before；keyword 已由服务端搜过）。
        if not query.matches(candidate):
            continue
        matched.append(candidate)

    total = len(matched)
    page = matched[: max(1, int(limit or DEFAULT_BATCH_LIMIT))]
    reason = (
        "候选池仅包含已同步千帆挂链映射的笔记；未同步/未绑定千帆的笔记不在池内"
    )
    if query.keyword:
        reason = (
            f"已按关键词「{query.keyword}」搜索千帆笔记（命中 {searched} 条并落库）"
            + (f"；搜索失败：{search_error}" if search_error else "")
            + "；其余边界同上"
        )
    return {
        "candidates": page,
        "total": total,
        "thresholds": thresholds.to_dict(),
        "query": query.to_dict(),
        "coverage": {
            "synced_notes": len(rows),
            "pool_total": len(rows),
            "reason": reason,
        },
    }


def _search_and_store(db: Session, account_id: int, keyword: str) -> tuple[int, str]:
    """千帆服务端标题搜索（``mix_search_word``），命中落库。返回 (命中数, 错误)。"""
    from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory
    from backend.app.services.shop_binding_service import qianfan_cookies_for_account
    from backend.app.services.shop_goods_service import existing_rows, store_entry

    account = db.get(PlatformAccount, account_id)
    if account is None:
        return 0, "账号不存在或已被删除"
    try:
        # 千帆凭据只认**本账号自己的 ark 通道**（2026-09-22 起不再有绑定表兜底）。
        cookies = qianfan_cookies_for_account(db, account_id)
        if not cookies:
            return 0, "该账号尚未导入千帆（ark）令牌"
        adapter = get_qianfan_api_adapter_factory()(cookies)
        success, message, payload = adapter.get_goods_note_page(
            page_num=1, page_size=SEARCH_PAGE_SIZE, mix_search_word=keyword
        )
    except Exception as exc:  # noqa: BLE001 - 搜索失败不阻塞，降级为本地池
        return 0, f"{type(exc).__name__}: {exc}"

    if not success:
        return 0, str(message)

    data = payload.get("data") if isinstance(payload, dict) else None
    entries = [item for item in ((data or {}).get("note_infos") or []) if isinstance(item, dict)]
    rows = existing_rows(db, account_id)
    stored = 0
    for entry in entries:
        if store_entry(db, account.user_id, account_id, rows, entry):
            stored += 1
    db.commit()
    return stored, ""


def serialize_candidate(candidate: CleanupCandidate) -> dict[str, Any]:
    return {
        "note_id": candidate.note_id,
        "title": candidate.title,
        "cover": candidate.cover,
        "browse_num": candidate.browse_num,
        "browse_num_30d": candidate.browse_num_30d,
        "likes": candidate.likes,
        "collects": candidate.collects,
        "comments": candidate.comments,
        "shares": candidate.shares,
        "pay_gmv": candidate.pay_gmv,
        "pay_order_cnt": candidate.pay_order_cnt,
        "publish_time": candidate.publish_time,
        "synced_at": candidate.synced_at,
    }
