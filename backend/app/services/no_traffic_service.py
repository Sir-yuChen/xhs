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

#: 三项指标的默认阈值（用户可覆盖，**不写死 0**）。
DEFAULT_MAX_BROWSE = 0
DEFAULT_MAX_PAY_ORDER = 0
DEFAULT_MAX_PAY_GMV = 0.0

#: 单批清理上限（平台写操作风控：一次最多删这么多篇）。
DEFAULT_BATCH_LIMIT = 20

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
    #: 笔记分类（2026-10-06 方案 A）：``""`` 不限 / ``goods`` 商业笔记 / ``normal`` 普通笔记。
    #: 数据源切到 `notes` 表后，按 `note_category` 过滤 —— 不同 tab 只清理对应类型。
    category: str = ""

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
        category = str(data.get("category") or "").strip()
        return cls(
            keyword=keyword,
            note_type=note_type,
            published_before=published_before,
            category=category,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "keyword": self.keyword,
            "note_type": self.note_type,
            "published_before": self.published_before,
            "category": self.category,
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
    """无流量判定阈值（每个指标都可单独配）。

    2026-10-06 需求「方案 A」：判定指标从**固定三项**（阅读/支付订单/支付金额）
    扩为**可选核心指标**（阅读/支付订单/支付金额/点赞/收藏/评论/分享）。语义：

    * 阈值 `None` = 该指标**不参与判定**（用户没勾选）；
    * 阈值非 `None` = 该指标必须 `<= 阈值`。
    * 「无流量」= **所有非 None 的指标都 <= 阈值**；至少一个指标非 None 才判定
      （全部 None = 没有判定依据，不判为无流量）。

    默认值保留旧三项（`0`），新增四项默认 `None`（向后兼容：旧调用只传三项，
    新四项不参与判定，行为与旧版一致）。
    """

    max_browse: Optional[int] = DEFAULT_MAX_BROWSE
    max_pay_order: Optional[int] = DEFAULT_MAX_PAY_ORDER
    max_pay_gmv: Optional[float] = DEFAULT_MAX_PAY_GMV
    # 2026-10-06 新增可选互动指标（默认 None = 不参与判定）。
    max_like: Optional[int] = None
    max_collect: Optional[int] = None
    max_comment: Optional[int] = None
    max_share: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "TrafficThresholds":
        if data is None:
            return cls()
        # 允许传 pydantic 模型（FastAPI 端点参数）或裸 dict。
        if hasattr(data, "model_dump"):
            data = data.model_dump()
        if not isinstance(data, dict):
            return cls()

        def _opt_int(key: str, default: Optional[int]) -> Optional[int]:
            raw = data.get(key, default)
            if raw is None:
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return default

        def _opt_float(key: str, default: Optional[float]) -> Optional[float]:
            raw = data.get(key, default)
            if raw is None:
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return default

        return cls(
            max_browse=_opt_int("max_browse", DEFAULT_MAX_BROWSE),
            max_pay_order=_opt_int("max_pay_order", DEFAULT_MAX_PAY_ORDER),
            max_pay_gmv=_opt_float("max_pay_gmv", DEFAULT_MAX_PAY_GMV),
            max_like=_opt_int("max_like", None),
            max_collect=_opt_int("max_collect", None),
            max_comment=_opt_int("max_comment", None),
            max_share=_opt_int("max_share", None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_browse": self.max_browse,
            "max_pay_order": self.max_pay_order,
            "max_pay_gmv": self.max_pay_gmv,
            "max_like": self.max_like,
            "max_collect": self.max_collect,
            "max_comment": self.max_comment,
            "max_share": self.max_share,
        }

    def active_metrics(self) -> dict[str, float]:
        """返回「参与判定的指标 → 阈值」映射（非 None 项）。"""
        mapping = {
            "read_count": self.max_browse,
            "pay_order_cnt": self.max_pay_order,
            "pay_gmv": self.max_pay_gmv,
            "like_count": self.max_like,
            "collect_count": self.max_collect,
            "comment_count": self.max_comment,
            "share_count": self.max_share,
        }
        out: dict[str, float] = {}
        for key, value in mapping.items():
            if value is not None:
                out[key] = float(value)
        return out


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
    #: 笔记分类（``goods`` 商业 / ``normal`` 普通 / ``""`` 未知）—— 删除执行器按它
    #: 分流删除通道（商业 → 千帆 batch_delete_note，普通 → 创作者 delete_note）。
    note_category: str = ""
    #: 本地 `notes.id`（主键）。删除执行器平台删成功后按它定位本地行做 `purge_note`
    #: （2026-10-06：无流量清理此前只删平台、漏删本地 notes 表）。
    local_id: int = 0
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


def is_no_traffic_by_metrics(
    metrics: dict[str, Any], thresholds: TrafficThresholds
) -> bool:
    """基于**离线数据中心指标**（`notes.raw_json.metrics`）的无流量判定（方案 A）。

    语义：`thresholds.active_metrics()` 里每个非 None 的指标，都必须在 `metrics`
    里存在且 `<= 阈值`。任一指标缺失 → 该笔记**不判为无流量**（缺值不是无流量，
    与旧口径同一条纪律）。至少一个指标非 None 才判定；全 None = 无判定依据，不判。

    ⚠️ 指标键对齐 `note_data_service` 的 metrics 键（`read_count` / `pay_gmv` /
    `pay_order_cnt` / `like_count` / `collect_count` / `comment_count` / `share_count`）。
    """
    if not isinstance(metrics, dict) or not metrics:
        return False
    active = thresholds.active_metrics()
    if not active:
        return False
    for key, threshold in active.items():
        value = _as_number(metrics.get(key))
        if value is None:
            return False  # 缺值不是无流量
        if value > threshold:
            return False
    return True


def list_no_traffic_candidates(
    db: Session,
    account_id: int,
    thresholds: Optional[TrafficThresholds] = None,
    limit: int = DEFAULT_BATCH_LIMIT,
    query: Optional[CleanupQuery] = None,
    page: int = 1,
) -> dict[str, Any]:
    """列出无流量候选。

    两条路径，由 ``query`` 决定：

    * **无搜索条件**（``query`` 为空或 keyword 为空）：**纯本地查询**，零平台请求，
      直接在已同步映射里按阈值筛。
    * **有搜索条件**：先调千帆 ``mix_search_word`` 做**服务端标题搜索**（千帆笔记管理页
      同款接口，2026-09-19 实测），把命中的笔记**落库**（补上指标），再在本地按阈值筛。
      这就是「按搜索条件 + 阈值」共同决定候选池。

    返回 ``{candidates, total, thresholds, query, coverage}``：
    * ``candidates`` — **第 ``page`` 页**、每页 ``limit`` 条（从 1 起）；
    * ``total`` — 符合条件的**总数**（跨页不变，前端据此算页数）；
    * ``page`` — 回显实际采用的页码（越界的页会回落成最后一页，见下）；
    * ``coverage`` — 候选池覆盖率说明，让前端讲清边界。
    """
    thresholds = thresholds or TrafficThresholds()
    query = query or CleanupQuery()
    # ⚠️ 门禁按**本链路的令牌**判，不是千帆 ark（ADR-021「隔离键 ≠ 能力判据」的另一半：
    #    能力判据要取该链路自己那份）。无流量清理打的是**创作者平台**的笔记管理接口
    #    （`_account_cookies` 取 `CHANNEL_CREATOR`、走 `XhsCreatorApiAdapter`），
    #    所以判据 = `creator` 通道有令牌。
    #    2026-09-30 收尾时这里一度误写成 `has_ark_session`（千帆令牌）——那会让
    #    「有创作者会话但没绑千帆」的账号本地候选池恒空（test_no_traffic_cleanup_runner
    #    实测：给了 creator jar 仍零候选、什么也不删）。
    from backend.app.services.account_service import (
        CHANNEL_CREATOR,
        latest_cookies_for_channel,
    )

    if not latest_cookies_for_channel(db, account_id, CHANNEL_CREATOR):
        return {
            "candidates": [],
            "total": 0,
            "page": 1,
            "page_count": 1,
            "page_size": max(1, int(limit or DEFAULT_BATCH_LIMIT)),
            "thresholds": thresholds.to_dict(),
            "query": query.to_dict(),
            "coverage": {
                "synced_notes": 0,
                "pool_total": 0,
                "reason": "本账号没有可用的创作者平台登录态",
            },
        }

    # 有搜索条件：**本地标题匹配**（零平台请求）。方案 A 下候选池是离线数据中心
    # 数据（`notes`），标题已在 `notes.title` 里，关键词搜索本地 `LIKE` 即可 ——
    # 不再打千帆 `mix_search_word`（那条路补的是 `shop_note_goods` 挂链映射，
    # 与新数据源脱节）。
    searched = 0
    search_error = ""

    # 数据源（方案 A）：`notes` 表（离线数据中心数据），按账号 + 笔记分类过滤。
    from backend.app.models import Note

    stmt = select(Note).where(
        Note.platform_account_id == account_id,
    )
    if query.category:
        # 指定分类 = 只清理该类（需求 7「不同 tab 只清理对应类型」）。
        stmt = stmt.where(Note.note_category == query.category)
    else:
        # 未指定 = 所有离线数据中心笔记（note_category 非空），向后兼容。
        stmt = stmt.where(Note.note_category != "")
    if query.keyword:
        keyword_like = f"%{query.keyword.strip()}%"
        stmt = stmt.where(Note.title.ilike(keyword_like))
    note_rows = db.scalars(stmt.order_by(Note.created_at.desc())).all()

    matched: list[CleanupCandidate] = []
    for note in note_rows:
        raw = note.raw_json if isinstance(note.raw_json, dict) else {}
        metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
        if not is_no_traffic_by_metrics(metrics, thresholds):
            continue
        # 发布时间：优先 raw_json.metrics 或 raw_json.published_at，退 None。
        published_at_str = raw.get("published_at") or ""
        publish_time = None
        if isinstance(published_at_str, str) and published_at_str:
            try:
                from datetime import datetime as _dt
                publish_time = int(_dt.strptime(published_at_str, "%Y-%m-%d %H:%M:%S").timestamp())
            except (ValueError, TypeError):
                publish_time = None
        candidate = CleanupCandidate(
            note_id=str(note.note_id or ""),
            title=str(note.title or ""),
            cover=str(raw.get("cover_url") or ""),
            browse_num=_as_number(metrics.get("read_count")),
            browse_num_30d=None,
            likes=_as_number(metrics.get("like_count")),
            collects=_as_number(metrics.get("collect_count")),
            comments=_as_number(metrics.get("comment_count")),
            shares=_as_number(metrics.get("share_count")),
            pay_gmv=_as_number(metrics.get("pay_gmv")),
            pay_order_cnt=_as_number(metrics.get("pay_order_cnt")),
            publish_time=publish_time,
            synced_at=note.created_at.strftime("%Y-%m-%d %H:%M:%S") if note.created_at else "",
            note_category=str(note.note_category or ""),
            local_id=note.id,
            raw=raw,
        )
        # 搜索条件过滤（note_type / published_before / category）。
        if not query.matches(candidate):
            continue
        matched.append(candidate)

    total = len(matched)
    # 分页（2026-10-05 用户口径「笔记列表实现分页」）：**先算 total 再切片** ——
    # 这也让「清理」侧的调用（`limit=batch_limit`、不传 page）语义**逐字不变**
    # （第 1 页 = 原来那批前 N 条）。
    size = max(1, int(limit or DEFAULT_BATCH_LIMIT))
    page_count = max(1, (total + size - 1) // size) if total else 1
    page_no = max(1, int(page or 1))
    # 越界的页回落成**最后一页**（不是空集）：删完几篇后 total 会变小，用户停在末页
    # 时不该看到一张空表 —— 空表会让人以为「筛选没生效」。
    if page_no > page_count:
        page_no = page_count
    start = (page_no - 1) * size
    page_rows = matched[start:start + size]
    cat_label = {"goods": "商业笔记", "normal": "普通笔记"}.get(query.category, "全部")
    reason = f"候选池 = {cat_label}（离线数据中心数据）；未同步离线的笔记不在池内"
    if query.keyword:
        reason = (
            f"已按关键词「{query.keyword}」在本地标题中匹配"
            + (f"；搜索失败：{search_error}" if search_error else "")
            + f"；候选池 = {cat_label}（离线数据中心数据）"
        )
    return {
        "candidates": page_rows,
        "total": total,
        "page": page_no,
        "page_count": page_count,
        "page_size": size,
        "thresholds": thresholds.to_dict(),
        "query": query.to_dict(),
        "coverage": {
            "synced_notes": len(note_rows),
            "pool_total": len(note_rows),
            "reason": reason,
        },
    }


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
