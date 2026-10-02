"""无流量笔记候选查询（纯本地，零平台请求）。

## 判定口径（硬约束 ⑤「缺值口径」）

「无流量」= `browse_num` / `note_pay_order_cnt` / `note_pay_gmv` 三项**在
`entry_json` 里都明确存在且都等于 0**。

* 字段**缺失** → `None`（留空），**不当作 0**：千帆不返回负记录，缺字段说明
  「这条笔记的指标读不到」，与「读到了，确实是 0」是两件事。只有三项**都明确是 0**
  才算无流量。
* `browse_num` 是「阅读」；支付两项是「交易」。三者都 0 = 没人看也没人买。
* `note_browse_num30d`（30 日阅读）**不参与判定**，它是可选的附加筛选条件
  （`include_30d`）：开了就额外要求 30 日阅读也 ≤ 阈值，关了就完全不看它。

## 候选池的已知边界（必须在响应里讲清楚）

`shop_note_goods` 只装**绑了千帆且已同步映射**的笔记（按需补齐 / 批量同步落库的）。
店铺里**还没同步**的笔记不在池子里 —— 这是「本地数据不全面」的已知边界，不是 Bug：
所以响应必带 `coverage = {pool, goods_note_total}`，让调用方把「候选 N 条 / 店铺共 M 条」
讲清楚，否则「无流量 N 条」会被误读成「店铺一共就这么多笔记」。

## 为什么不做平台请求

判定所需的三项指标在本地 `entry_json` 里是**现成的**（同步时存的整条原文），
再打一次千帆只是平添风控面，且让「候选查询」变成一个有副作用的接口。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import PlatformAccount
from backend.app.models.shop import ShopNoteGoods
from backend.app.services.shop_binding_service import has_ark_session, get_shop_state

#: 三项指标在 `entry_json` 里的键名（千帆「商品笔记」列表原文）。
METRIC_KEYS = ("browse_num", "note_pay_order_cnt", "note_pay_gmv")
#: 30 日阅读的键名（可选附加条件，不参与「无流量」判定）。
KEY_BROWSE_30D = "note_browse_num30d"
#: 发布时间的键名（毫秒时间戳，用于时间区间筛选与排序）。
KEY_PUBLISH_TIME = "publish_time"
#: JSON 中取指标的最小可信度：三项都必须明确是数字 0。
_MISSING = object()


def _json_value(entry: dict[str, Any], key: str) -> Any:
    """`entry_json` 解析后的取值；键不存在返回哨兵 `_MISSING`（区别于 `None`）。"""
    return entry.get(key, _MISSING)


def _as_number(value: Any) -> Optional[float]:
    """值 → 数字。非数字 / None → None（**不当作 0**）。"""
    if value is None or value is _MISSING:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_entry(row: ShopNoteGoods) -> dict[str, Any]:
    """`entry_json` → dict；解析失败或不是对象 → 空 dict（指标全部按缺值处理）。"""
    import json

    try:
        parsed = json.loads(row.entry_json or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _entry_metrics(entry: dict[str, Any]) -> dict[str, Optional[float]]:
    """三项指标 + 30 日阅读 + 发布时间的数值形态（缺值 = `None`）。"""
    values: dict[str, Optional[float]] = {}
    for key in (*METRIC_KEYS, KEY_BROWSE_30D, KEY_PUBLISH_TIME):
        values[key] = _as_number(_json_value(entry, key))
    return values


def _threshold(value: float | None) -> float:
    """阈值入参：`None` → 0（默认全 0），负数 → 0（阈值没有负的含义）。"""
    if value is None:
        return 0.0
    return max(float(value), 0.0)


def is_zero_traffic(entry: dict[str, Any], *, thresholds: dict[str, float]) -> bool:
    """纯函数判定：三项指标是否**都明确存在且都 ≤ 阈值**。

    返回 `False` 的三种情形（调用方不该把它们混为一谈）：

    * 任一指标**缺失**（`entry_json` 里没有这个键）→ 读不到，不算无流量；
    * 任一指标**不是数字**（字符串脏数据等）→ 同上，留空不算 0；
    * 任一指标**大于阈值** → 有流量。
    """
    metrics = _entry_metrics(entry)
    for key in METRIC_KEYS:
        value = metrics.get(key)
        if value is None:
            return False
        if value > _threshold(thresholds.get(key)):
            return False
    return True


class CandidateQuery:
    """可配置的候选查询（条件全部可选 → 不是写死全库）。"""

    def __init__(
        self,
        *,
        publish_from: Optional[datetime] = None,
        publish_to: Optional[datetime] = None,
        include_30d: bool = False,
        browse_30d_max: Optional[float] = None,
        thresholds: Optional[dict[str, float]] = None,
    ) -> None:
        self.publish_from = publish_from
        self.publish_to = publish_to
        self.include_30d = include_30d
        self.browse_30d_max = browse_30d_max
        #: 三项阈值，默认全 0（`None` 也按 0 处理，见 `_threshold`）。
        self.thresholds = thresholds or {}

    def matches(self, entry: dict[str, Any]) -> bool:
        """单条 `entry_json`（已解析）是否命中全部条件。"""
        if not is_zero_traffic(entry, thresholds=self.thresholds):
            return False
        metrics = _entry_metrics(entry)
        if self.include_30d:
            value = metrics.get(KEY_BROWSE_30D)
            #: 30 日阅读缺值时**不命中**：附加条件开了，就要求它确实读得到且达标。
            if value is None:
                return False
            if value > _threshold(self.browse_30d_max):
                return False
        publish = metrics.get(KEY_PUBLISH_TIME)
        if self.publish_from is not None or self.publish_to is not None:
            #: 发布时间缺值 → 时间区间成了无法验证的条件，不命中（不造假、不默认）。
            if publish is None:
                return False
            stamp = publish / 1000.0
            if self.publish_from is not None and stamp < self.publish_from.timestamp():
                return False
            if self.publish_to is not None and stamp > self.publish_to.timestamp():
                return False
        return True


def _candidate_row(row: ShopNoteGoods, entry: dict[str, Any]) -> dict[str, Any]:
    """映射行 → 前端候选清单的一格（字段全部来自本地，白名单构造）。"""
    metrics = _entry_metrics(entry)

    def text(*keys: str) -> str:
        for key in keys:
            value = entry.get(key)
            if value is None:
                continue
            rendered = str(value).strip()
            if rendered:
                return rendered
        return ""

    publish = metrics.get(KEY_PUBLISH_TIME)
    return {
        "note_id": row.note_id,
        "title": text("note_title"),
        "cover": text("note_image"),
        "publish_time": _millis_text(publish),
        #: 三项指标原样透出（缺值 = `None`，由渲染层决定不显示）。
        "browse_num": metrics.get("browse_num"),
        "pay_order_cnt": metrics.get("note_pay_order_cnt"),
        "pay_gmv": metrics.get("note_pay_gmv"),
        #: 30 日阅读只在查询明确要求（`include_30d`）时才有值，否则恒 `None`
        #:（前端没有理由展示一个用户没勾的指标）。
        "browse_num_30d": None,
        "item_id": row.item_id,
        "item_title": row.item_title,
        "synced_at": row.synced_at.strftime("%Y-%m-%d %H:%M") if row.synced_at else "",
    }


def _millis_text(value: Optional[float]) -> str:
    """毫秒时间戳 → `YYYY-MM-DD HH:MM`；空 / 非法 → 空串。"""
    if value is None or value <= 0:
        return ""
    return (
        datetime.fromtimestamp(value / 1000.0, tz=timezone(timedelta(hours=8)))
        .strftime("%Y-%m-%d %H:%M")
    )


def list_candidates(
    db: Session,
    account_id: int,
    query: CandidateQuery,
    *,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """候选清单（**纯本地**，不打千帆）。

    返回 `{candidates, page, page_size, total, coverage}`。`coverage` 是已知边界
    的量化：`pool` = 本地映射条数（候选池大小），`goods_note_total` = 店铺商品笔记
    总数（分母）。**没有可用的千帆登录态 → 404 由调用方给**，这里返回空池 + 分母 0。
    """
    account = db.get(PlatformAccount, account_id)
    if account is None or account.deleted_at is not None or not has_ark_session(db, account_id):
        return {
            "candidates": [],
            "page": max(page, 1),
            "page_size": page_size,
            "total": 0,
            "coverage": {"pool": 0, "goods_note_total": 0},
            "bound": False,
        }

    rows = db.scalars(
        select(ShopNoteGoods)
        .where(ShopNoteGoods.platform_account_id == account_id)
        .order_by(ShopNoteGoods.synced_at.desc())
    ).all()

    matched: list[dict[str, Any]] = []
    for row in rows:
        entry = _parse_entry(row)
        if not entry:
            continue
        if query.matches(entry):
            candidate = _candidate_row(row, entry)
            if query.include_30d:
                candidate["browse_num_30d"] = _entry_metrics(entry).get(KEY_BROWSE_30D)
            matched.append(candidate)

    #: 发布时间倒序（缺时间戳的排最后，不丢数据）。
    matched.sort(
        key=lambda item: (item.get("publish_time") or "", item.get("synced_at") or ""),
        reverse=True,
    )

    total = len(matched)
    offset = max(page - 1, 0) * page_size
    window = matched[offset : offset + page_size] if page_size > 0 else matched

    return {
        "candidates": window,
        "page": max(page, 1),
        "page_size": page_size,
        "total": total,
        "coverage": {
            "pool": len(rows),
            "goods_note_total": get_shop_state(account)["goods_note_total"],
        },
        "bound": True,
    }
