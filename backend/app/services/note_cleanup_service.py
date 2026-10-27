"""无流量笔记候选查询（**实时查千帆**，设计 75 S5 起）。

## 判定口径（硬约束 ⑤「缺值口径」）

「无流量」= `browse_num` / `note_pay_order_cnt` / `note_pay_gmv` 三项**在
`entry_json` 里都明确存在且都等于 0**。

* 字段**缺失** → `None`（留空），**不当作 0**：千帆不返回负记录，缺字段说明
  「这条笔记的指标读不到」，与「读到了，确实是 0」是两件事。只有三项**都明确是 0**
  才算无流量。
* `browse_num` 是「阅读」；支付两项是「交易」。三者都 0 = 没人看也没人买。
* `note_browse_num30d`（30 日阅读）**不参与判定**，它是可选的附加筛选条件
  （`include_30d`）：开了就额外要求 30 日阅读也 ≤ 阈值，关了就完全不看它。

## 候选池来自哪里（2026-10-27 改）

原来是本地映射表 `shop_note_goods`（「同步过的笔记」才有），边界是「店铺里没同步的
笔记不在池子里」。该表已随迁移 `n8o9p0q1r2s3` 整表 DROP ⇒ 现在**直接实时查千帆**
（`rt.list_goods_notes` 翻页，`page_size=100`）。**边界消失了** —— 候选池就是店铺里
真正挂过商品的笔记，不再是"本地同步过的那部分"。

所以 `coverage.pool` = 本次实际翻到的笔记数，`coverage.goods_note_total` 仍取平台
报的店铺商品笔记总数（分母）。两者都来自平台 ⇒ 「候选 N / 总数 M」现在可以真的对上。

⚠️ 代价：本接口**会打平台**（最多 `MAX_SCAN_PAGES` 页只读请求）。判定逻辑（纯函数
`is_zero_traffic` / `CandidateQuery.matches`）保持不变 —— 它只看一份 `entry` 字典，
数据从哪来与它无关。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from backend.app.models import PlatformAccount
from backend.app.services.shop_binding_service import has_ark_session, get_shop_state

#: 扫描上限（页 × 100 条）：候选查询是给人看的清单，不是全量导出。
#: 超过这个规模说明该账号笔记太多，应改用发布时间区间收窄。
MAX_SCAN_PAGES = 20
SCAN_PAGE_SIZE = 100

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


def _entry_of(note: dict[str, Any]) -> dict[str, Any]:
    """实时归一化条目 → 判定用的 `entry` 字典（键名与平台原文**同口径**）。

    ⚠️ 设计 75 S5：原来读的是 `shop_note_goods.entry_json`（平台整条原文）。
    现在数据直接来自 `rt.list_goods_notes` 的归一化结果（`normalize_goods_note`），
    键名是 `read_count` / `pay_order_count` / `pay_gmv` / `publish_time` —— 这里
    翻译回判定函数认的那套键，**判定实现一行不改**（缺值仍是 `None`，不当 0）。
    """
    return {
        "browse_num": note.get("read_count"),
        "note_pay_order_cnt": note.get("pay_order_count"),
        "note_pay_gmv": note.get("pay_gmv"),
        "note_browse_num30d": note.get("read_count_30d"),
        "publish_time": note.get("publish_time"),
        "note_title": note.get("title"),
        "note_image": note.get("cover"),
        "note_id": note.get("note_id"),
    }


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


def _candidate_row(note: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """实时条目 → 前端候选清单的一格（白名单构造）。"""
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
        "note_id": text("note_id"),
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
        #: 挂载商品（平台原文 `goods_infos[]` 的归一化结果，可能为空列表）。
        "item_id": (note.get("goods") or [{}])[0].get("item_id", "") if note.get("goods") else "",
        "item_title": (note.get("goods") or [{}])[0].get("title", "") if note.get("goods") else "",
        #: 实时链路没有「同步时间」—— 留空（不谎报一个本地写入时刻）。
        "synced_at": "",
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
    """候选清单（**实时翻页查千帆**，设计 75 S5 / 2026-10-27）。

    ⚠️ 原文「**纯本地**，不打千帆 / `pool` = 本地映射条数」那套口径**已被设计 75 取代**：
    `shop_note_goods` 关系表已随迁移 `n8o9p0q1r2s3` DROP，指标（阅读 / 支付订单 /
    支付金额）不再有本地副本 ⇒ 只能是 `rt.list_goods_notes` 逐页拉平台数据。

    返回 `{candidates, page, page_size, total, coverage, bound}`：
    `coverage.pool` = **本次实时扫到的笔记条数**（候选池大小，不是本地行数）；
    `coverage.goods_note_total` = 平台报的店铺商品笔记总数（分母）。

    没有可用的千帆登录态 / 没有 `ark` 令牌 ⇒ 返回空池 + `bound=False`（404 由调用方给）。
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

    #: ⚠️ 实时翻页（设计 75 S5）：原来读本地映射表。每页 `SCAN_PAGE_SIZE`，
    #: 最多 `MAX_SCAN_PAGES` 页；任一页失败**向上抛**（带平台原话）——
    #: 少扫几页就等于「候选少了」，那是最危险的静默错误（用户会照这份清单去删笔记）。
    from backend.app.services import realtime_platform_query_service as rt
    from backend.app.services.shop_binding_service import qianfan_cookies_for_account
    from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory

    cookies = qianfan_cookies_for_account(db, account_id)
    if not cookies:
        return {
            "candidates": [],
            "page": max(page, 1),
            "page_size": page_size,
            "total": 0,
            "coverage": {"pool": 0, "goods_note_total": 0},
            "bound": False,
        }
    adapter = get_qianfan_api_adapter_factory()(cookies)
    notes: list[dict[str, Any]] = []
    platform_total = 0
    for page_no in range(1, MAX_SCAN_PAGES + 1):
        result = rt.list_goods_notes(adapter, mix_search_word=" ", page=page_no, page_size=SCAN_PAGE_SIZE)
        batch = result.get("items") or []
        if platform_total == 0 and result.get("total"):
            platform_total = int(result["total"])
        notes.extend(batch)
        if not batch or not result.get("has_more"):
            break
    rows = notes

    matched: list[dict[str, Any]] = []
    for row in rows:
        entry = _entry_of(row)
        if not entry.get("note_id"):
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
            "goods_note_total": platform_total or get_shop_state(account)["goods_note_total"],
        },
        "bound": True,
    }
