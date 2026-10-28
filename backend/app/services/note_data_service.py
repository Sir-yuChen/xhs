"""「我的笔记」离线数据落库服务（2026-10-06 需求「我的笔记页面重构」）。

## 数据来源与判据

「商业笔记」与「普通笔记」的判据是**千帆数据中心的下载入口**（`note-data/goods`
vs `note-data/normal`），不是「笔记类型」字段（那个是图文/视频）。两条落库路：

* **自动同步**（定时任务，每 6h）：走 `butterfly/data` 列表接口（**无签名直连**，
  已验证 2026-10-06），按 `category` 分页拉全量，字段覆盖 xlsx 全部列且更全。
* **手动导入**（用户上传 xlsx）：解析 xlsx 文件，同样的字段映射落库。

两条路**共享同一份落库逻辑**（`upsert_note_data_rows`），只是「取数」不同 ——
一个来自接口、一个来自文件。字段映射也同一份（`_note_row_from_butterfly` /
`_note_row_from_xlsx` 各自解包，产出**同一个** `NoteDataRow` 形状）。

## 落库语义

* 2026-10-12 设计 35 起：`notes.note_category` 已退役，落库分两处 ——
  `notes`（基础信息）与 `note_datacenter_stats`（指标/分类）。
* 笔记本体走 `(user_id, note_id)` 覆盖更新（既有唯一键 `uq_notes_user_note`）。
* 挂载商品走 `note_goods` 关系表（`(user_id, note_id, item_id)` 幂等），
  只增不删（§26.7 口径），商品信息不冗余进笔记表。
* 指标（阅读/点赞/支付/退款/点击…）进 `raw_json`（字段集还会变，真值留原文）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select

from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models import Note, NoteDatacenterStat, PlatformAccount
from backend.app.models.note_datacenter_stat import WINDOW_RECENT_1, WINDOW_CUSTOM

#: 千帆数据中心「笔记数据」两类入口（`butterfly/data` 的 blockKey 由 SDK 映射）。
#: 2026-10-12 设计 35：`category` 仍作**取数入口**（决定拉 goods 还是 normal 块），
#: 但**不再落 `notes.note_category`**（该列已退役）。落库时 `category` 映射到
#: 统计表的 `note_flag`：`goods` → `"goods_note"`，`normal` → `""`（普通/未知）。
CATEGORY_GOODS = "goods"
CATEGORY_NORMAL = "normal"
CATEGORIES = (CATEGORY_GOODS, CATEGORY_NORMAL)

#: `category` → 统计表 `note_flag`（goods 是平台自报的商业笔记）。
_CATEGORY_TO_FLAG = {CATEGORY_GOODS: "goods_note", CATEGORY_NORMAL: ""}

#: 单页条数上限（`butterfly/data` 的 `size` 档位：页面可选 10/20/30/40/50）。
PAGE_SIZE = 50

#: 一轮自动同步最多翻多少页（**有界**，硬约束 C5：全量方法必须有分页插槽，
#: 刹车由页数上限 + 每页限流保证 —— 这里不是平台写操作，是只读列表，但同样
#: 不许一次性无界翻页）。
MAX_PAGES = 200


@dataclass
class NoteDataRow:
    """一条离线笔记数据的**归一化形状**（两个来源产出同一形状）。

    字段名对齐 `notes` 表 + `raw_json` 指标键。缺值 = `None`（不造假）。
    """

    note_id: str
    title: str = ""
    note_url: str = ""
    author_name: str = ""
    author_xhs_id: str = ""
    note_type: str = ""  # 图文 / 视频（平台原文，不是商业/普通）
    published_at: Optional[str] = None  # 笔记创建时间（平台原文字符串）
    category: str = CATEGORY_GOODS  # goods / normal（取数入口，落库映射到 note_flag）
    # 挂载商品（商业笔记有；普通笔记无）
    item_id: str = ""
    item_title: str = ""
    # 指标（统计表列，缺值 None）
    metrics: dict[str, Any] = field(default_factory=dict)


def _unwrap(value: Any) -> Any:
    """解包 butterfly 的 `{"value": x, "ratio": ...}` 包裹结构。

    直接取 `value`；若本身不是包裹结构（xlsx 来的裸值），原样返回。
    缺值口径：`None` / 空串 / `0` 都如实返回，由调用方决定是否造假。
    """
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def _to_str(value: Any) -> str:
    """安全转字符串：`None` → `""`，数字 → 去尾零的十进制。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # 整数浮点（如 38.0）→ "38"，非整 → 原样
        if value.is_integer():
            return str(int(value))
        return str(value)
    return str(value)


def _to_int_or_none(value: Any) -> Optional[int]:
    """安全转 int：非数字 / None → None（缺值口径，不造 0）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        if isinstance(value, float):
            if not value.is_integer():
                return None
            return int(value)
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float_or_none(value: Any) -> Optional[float]:
    """安全转 float：非数字 / None → None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _note_row_from_butterfly(row: dict[str, Any], category: str) -> NoteDataRow:
    """butterfly 响应的一行 → `NoteDataRow`。

    字段名来自 2026-10-06 真机抓包（见 hindsight 记忆 / 本模块 docstring）。
    """
    note_id = _to_str(_unwrap(row.get("noteId")))
    title = _to_str(_unwrap(row.get("discoveryTitle")))
    if not title:
        # discoveryTitle 可能为空（真实抓包见过 ""），退 nitemName 或空
        title = _to_str(_unwrap(row.get("nitemName")))
    note_url = _to_str(_unwrap(row.get("discoveryLink")))
    # 作者：优先 userBasic[0]（nickName / redId），退顶层 nickName / redId
    author_name = _to_str(_unwrap(row.get("nickName")))
    author_xhs_id = _to_str(_unwrap(row.get("redId")))
    user_basic = _unwrap(row.get("userBasic"))
    if isinstance(user_basic, list) and user_basic:
        ub = user_basic[0]
        if isinstance(ub, dict):
            author_name = author_name or _to_str(_unwrap(ub.get("nickName")))
            author_xhs_id = author_xhs_id or _to_str(_unwrap(ub.get("redId")))
    note_type = _to_str(_unwrap(row.get("noteType")))
    published_at = _to_str(_unwrap(row.get("notePublishTime")))

    # 挂载商品
    item_id = _to_str(_unwrap(row.get("itemId"))) or _to_str(_unwrap(row.get("nitemId")))
    item_title = _to_str(_unwrap(row.get("nitemName")))

    # 指标 → raw_json（缺值 None）
    metrics = {
        "video_duration": _to_int_or_none(_unwrap(row.get("videoDuration"))),
        "read_count": _to_int_or_none(_unwrap(row.get("noteReadCnt"))),
        "like_count": _to_int_or_none(_unwrap(row.get("noteLikeCnt"))),
        "collect_count": _to_int_or_none(_unwrap(row.get("noteFavCnt"))),
        "comment_count": _to_int_or_none(_unwrap(row.get("noteCmtCnt"))),
        "share_count": _to_int_or_none(_unwrap(row.get("noteShareCnt"))),
        "follow_count": _to_int_or_none(_unwrap(row.get("noteFollowCnt"))),
        "danmaku_count": _to_int_or_none(_unwrap(row.get("danmakuCnt"))),
        "avg_view_time": _to_float_or_none(_unwrap(row.get("noteViewTime"))),
        "full_view_rate": _to_float_or_none(_unwrap(row.get("noteFullViewRate"))),
        # 商品指标（商业笔记才有；普通笔记这些键为 None）
        "pay_gmv": _to_float_or_none(_unwrap(row.get("notePayGmv"))),
        "pay_order_cnt": _to_int_or_none(_unwrap(row.get("notePayOrderCnt"))),
        "item_click_cnt": _to_int_or_none(_unwrap(row.get("noteItemClickCnt"))),
        "item_click_pv_rate": _to_float_or_none(_unwrap(row.get("noteItemClickPvRate"))),
        "item_click_uv": _to_int_or_none(_unwrap(row.get("noteItemClickUv"))),
        "pay_user_cnt": _to_int_or_none(_unwrap(row.get("notePayUserCnt"))),
        "pay_pv_rate": _to_float_or_none(_unwrap(row.get("notePayPvRate"))),
        "pay_uv_rate": _to_float_or_none(_unwrap(row.get("notePayUvRate"))),
        "refund_gmv": _to_float_or_none(_unwrap(row.get("noteRefundGmv"))),
        "refund_order_cnt": _to_int_or_none(_unwrap(row.get("noteRefundOrderCnt"))),
        "refund_user_cnt": _to_int_or_none(_unwrap(row.get("noteRefundUserCnt"))),
        "cart_item_cnt": _to_int_or_none(_unwrap(row.get("cartItemCnt"))),
        "lead_user_page_view_cnt": _to_int_or_none(_unwrap(row.get("leadUserPageViewCnt"))),
        "lead_user_page_pay_gmv": _to_float_or_none(_unwrap(row.get("leadUserPagePayGmv"))),
        "lead_live_view_cnt": _to_int_or_none(_unwrap(row.get("leadLiveViewCnt"))),
        "lead_live_pay_gmv": _to_float_or_none(_unwrap(row.get("leadLivePayGmv"))),
    }
    # 剔除 None（缺值不落库，读方按缺值口径渲染 0 并区分原因）
    metrics = {k: v for k, v in metrics.items() if v is not None}

    return NoteDataRow(
        note_id=note_id,
        title=title,
        note_url=note_url,
        author_name=author_name,
        author_xhs_id=author_xhs_id,
        note_type=note_type,
        published_at=published_at,
        category=category,
        item_id=item_id,
        item_title=item_title,
        metrics=metrics,
    )


#: xlsx 表头 → 字段（商品笔记 38 列 / 普通笔记 20 列，按列名索引）。
#: 普通笔记没有商品列，映射时按列名动态取，取不到就留空。
_XLSX_COL_MAP = {
    "笔记标题": "title",
    "笔记ID": "note_id",
    "笔记链接": "note_url",
    "作者昵称": "author_name",
    "作者xhs_ID": "author_xhs_id",
    "笔记创建时间": "published_at",
    "笔记类型": "note_type",
    "关联商品名称": "item_title",
    "关联商品ID": "item_id",
    "视频时长（秒）": "video_duration",
    "笔记支付金额": "pay_gmv",
    "笔记支付订单数": "pay_order_cnt",
    "笔记商品点击次数": "item_click_cnt",
    "笔记商品点击率（PV）": "item_click_pv_rate",
    "笔记商品点击人数": "item_click_uv",
    "笔记支付人数": "pay_user_cnt",
    "笔记支付转化率（PV）": "pay_pv_rate",
    "笔记支付转化率（UV）": "pay_uv_rate",
    "笔记退款金额（退款时间）": "refund_gmv",
    "笔记退款订单数（退款时间）": "refund_order_cnt",
    "笔记退款人数（退款时间）": "refund_user_cnt",
    "笔记加购件数": "cart_item_cnt",
    "引流店铺主页次数": "lead_user_page_view_cnt",
    "引流店铺主页支付金额": "lead_user_page_pay_gmv",
    "引流直播间次数": "lead_live_view_cnt",
    "引流直播间支付金额": "lead_live_pay_gmv",
    "笔记阅读数": "read_count",
    "点赞次数": "like_count",
    "收藏次数": "collect_count",
    "评论次数": "comment_count",
    "分享次数": "share_count",
    "笔记点击关注次数": "follow_count",
    "弹幕次数": "danmaku_count",
    "平均阅读时长（观播时长）": "avg_view_time",
    "完播率（PV）": "full_view_rate",
}

#: 指标键（数值型，走 `_to_int_or_none` / `_to_float_or_none`）。
_METRIC_INT_KEYS = {
    "video_duration", "read_count", "like_count", "collect_count", "comment_count",
    "share_count", "follow_count", "danmaku_count", "pay_order_cnt", "item_click_cnt",
    "item_click_uv", "pay_user_cnt", "refund_order_cnt", "refund_user_cnt",
    "cart_item_cnt", "lead_user_page_view_cnt", "lead_live_view_cnt",
}
_METRIC_FLOAT_KEYS = {
    "avg_view_time", "full_view_rate", "pay_gmv", "item_click_pv_rate",
    "pay_pv_rate", "pay_uv_rate", "refund_gmv", "lead_user_page_pay_gmv",
    "lead_live_pay_gmv",
}

#: 非指标的基础字段（不进 metrics）。
_BASE_KEYS = {"title", "note_id", "note_url", "author_name", "author_xhs_id",
              "published_at", "note_type", "item_title", "item_id"}


def _extract_url_from_hyperlink(value: Any) -> str:
    """xlsx 的「笔记链接」列是 `=HYPERLINK("url","url")` 公式（data_only 下读到 0.0）。

    这里从公式字符串里提取第一个 `https://...` URL。不是公式时原样返回。
    """
    if isinstance(value, str) and value.startswith("=HYPERLINK("):
        import re
        m = re.search(r'https://[^"\)]+', value)
        if m:
            return m.group(0)
        return ""
    return _to_str(value)


def _note_row_from_xlsx(row_dict: dict[str, Any], category: str) -> NoteDataRow:
    """xlsx 的一行（表头→值）→ `NoteDataRow`。

    列名用 `_XLSX_COL_MAP` 映射到统一字段。表头缺失 / 值为空 → 留空（缺值口径）。
    """
    fields: dict[str, Any] = {}
    for header, value in row_dict.items():
        key = _XLSX_COL_MAP.get(str(header).strip())
        if key is None:
            continue
        if key == "note_url":
            fields[key] = _extract_url_from_hyperlink(value)
        else:
            fields[key] = value

    note_id = _to_str(fields.get("note_id"))
    metrics = {}
    for key, value in fields.items():
        if key in _METRIC_INT_KEYS:
            v = _to_int_or_none(value)
            if v is not None:
                metrics[key] = v
        elif key in _METRIC_FLOAT_KEYS:
            v = _to_float_or_none(value)
            if v is not None:
                metrics[key] = v

    return NoteDataRow(
        note_id=note_id,
        title=_to_str(fields.get("title")),
        note_url=fields.get("note_url", ""),
        author_name=_to_str(fields.get("author_name")),
        author_xhs_id=_to_str(fields.get("author_xhs_id")),
        note_type=_to_str(fields.get("note_type")),
        published_at=_to_str(fields.get("published_at")),
        category=category,
        item_id=_to_str(fields.get("item_id")),
        item_title=_to_str(fields.get("item_title")),
        metrics=metrics,
    )


def _upsert_datacenter_stat(
    db, row: NoteDataRow, *, user_id: int, account_id: int, window: str = WINDOW_RECENT_1
) -> None:
    """一条 `NoteDataRow` 的指标落 `note_datacenter_stats`（UPSERT，未 commit）。

    2026-10-12 设计 35：指标与分类的**唯一落库点**。`row.category` 映射到
    `note_flag`（`goods` → `"goods_note"` / `normal` → `""`），指标直接写真实列
    （缺值 None 不写，读方按缺值口径渲染 0 并区分原因）。
    """
    stat_date = shanghai_now().strftime("%Y-%m-%d")
    note_flag = _CATEGORY_TO_FLAG.get(row.category, "")
    m = row.metrics

    def _f(key, default=None):
        v = m.get(key)
        return v if v is not None else default

    existing = db.scalars(
        select(NoteDatacenterStat).where(
            NoteDatacenterStat.user_id == user_id,
            NoteDatacenterStat.platform_account_id == account_id,
            NoteDatacenterStat.note_id == row.note_id,
            NoteDatacenterStat.stat_date == stat_date,
            NoteDatacenterStat.window == window,
        )
    ).first()
    if existing is None:
        existing = NoteDatacenterStat(
            user_id=user_id,
            platform_account_id=account_id,
            note_id=row.note_id,
            stat_date=stat_date,
            window=window,
            note_flag=note_flag,
        )
        db.add(existing)
    # 覆盖更新（同 note + 同日 + 同窗口取最新值）。
    existing.note_flag = note_flag
    existing.title = row.title or existing.title
    existing.item_id = row.item_id
    existing.item_title = row.item_title
    # 发布时间：butterfly 的 published_at 是字符串（如 "2026-08-13 09:22"），
    # 解析成 Unix 时间戳（秒）落 `publish_time`；解析失败留 None（缺值不造）。
    if row.published_at:
        try:
            from datetime import datetime as _dt
            existing.publish_time = int(_dt.strptime(row.published_at, "%Y-%m-%d %H:%M:%S").timestamp())
        except (ValueError, TypeError):
            existing.publish_time = None
    # 互动（int）
    for col in ("read_count", "like_count", "collect_count", "comment_count",
                "share_count", "follow_count", "danmaku_count"):
        v = _f(col)
        if v is not None:
            setattr(existing, col, int(v))
    # 观看 / 商品 / 退款 / 引流（float）
    for col in ("video_duration", "avg_view_time", "full_view_rate",
                "pay_gmv", "pay_order_cnt", "pay_user_cnt", "item_click_cnt",
                "item_click_uv", "item_click_pv_rate", "pay_pv_rate", "pay_uv_rate",
                "refund_gmv", "refund_order_cnt", "refund_user_cnt", "cart_item_cnt",
                "lead_user_page_view_cnt", "lead_user_page_pay_gmv",
                "lead_live_view_cnt", "lead_live_pay_gmv"):
        v = _f(col)
        if v is not None:
            setattr(existing, col, v)


def upsert_note_data_rows(
    db,
    rows: list[NoteDataRow],
    *,
    user_id: int,
    account_id: int,
    window: str = WINDOW_RECENT_1,
) -> dict[str, int]:
    """一批 `NoteDataRow` 落库 —— **只写 `note_datacenter_stats`，不碰 `notes`**。

    ⚠️ 2026-10-28 用户口径「数据中心数据不落内容库表」：数据中心是**平台事实的快照**，
    内容库是**用户自己的笔记**，两者只在平台 `note_id` 上语义相关，读路径也互不 join。
    此前这里会顺带 `_upsert_note` 建 `notes` 行（`note_type` 吃默认值 `local`），
    结果是内容库凭空多出 20 条数据中心同步的副产物。

    调用方负责 commit（批量写一次提交，不逐条 commit —— 写锁）。
    `window`（butterfly 的 `dateSelectType`）：默认 `recent_1`；自定义日期范围走 `custom`。
    """
    created = updated = 0
    for row in rows:
        if not row.note_id:
            continue
        #: 一条统计行 = 一条笔记在统计表的落点；`existed` 按统计表判定。
        existed = db.scalars(
            select(NoteDatacenterStat.id).where(
                NoteDatacenterStat.user_id == user_id,
                NoteDatacenterStat.platform_account_id == account_id,
                NoteDatacenterStat.note_id == row.note_id,
            )
        ).first() is not None
        _upsert_datacenter_stat(db, row, user_id=user_id, account_id=account_id, window=window)
        if existed:
            updated += 1
        else:
            created += 1
    return {
        "created": created,
        "updated": updated,
        "total": created + updated,
        "skipped_deleted": 0,
    }


def parse_butterfly_response(payload: dict, category: str) -> list[NoteDataRow]:
    """butterfly 响应 `data[0].data[]` → `NoteDataRow` 列表。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        return []
    blk = data[0]
    rows = blk.get("data") if isinstance(blk, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[NoteDataRow] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = _note_row_from_butterfly(row, category)
        if parsed.note_id:
            out.append(parsed)
    return out


def parse_xlsx_file(path: str, category: str) -> list[NoteDataRow]:
    """解析 xlsx 文件 → `NoteDataRow` 列表（手动导入用）。

    用 openpyxl 读第一个 sheet，首行是表头，后续行是数据。
    「笔记链接」列是 `=HYPERLINK(...)` 公式，读 data_only 拿不到 URL，
    需读非 data_only 的公式文本（`_extract_url_from_hyperlink` 处理）。
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=False)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    header = next(rows_iter, None)
    if header is None:
        return []
    headers = [str(h).strip() if h is not None else "" for h in header]
    out: list[NoteDataRow] = []
    for values in rows_iter:
        if values is None or all(v is None for v in values):
            continue
        row_dict = {headers[i]: values[i] for i in range(min(len(headers), len(values)))}
        parsed = _note_row_from_xlsx(row_dict, category)
        if parsed.note_id:
            out.append(parsed)
    return out


def sync_account_note_data(
    *,
    account_id: int,
    category: str,
    user_id: Optional[int] = None,
    date_select_type: str = "recent_1",
    max_pages: int = MAX_PAGES,
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any]:
    """给一个账号同步一类离线笔记数据（`butterfly/data` 分页拉全量 → 落库）。

    返回 `{category, fetched, created, updated, pages, error?}`。任何异常都不向上抛
    （调度器调用方不期待单账号失败拖垮整轮），失败把原因带在 `error` 里。

    ⚠️ 取账号的 **ark 通道** Cookie（千帆数据中心要 ark 登录态）；取不到 → 返回
    `error="ARK_SESSION_REQUIRED"`，不落任何数据。

    日期参数（2026-10-06）：`start_date`/`end_date` 非空时走「自定义日期范围」
    （`date_select_type="custom"`），否则用预设 `date_select_type`（默认 `recent_1`）。
    """
    from backend.app.adapters.xhs.qianfan_api_adapter import XhsQianfanApiAdapter
    from backend.app.services.account_service import CHANNEL_ARK, latest_cookies_for_channel

    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        if account is None:
            return {"category": category, "error": "ACCOUNT_NOT_FOUND"}
        owner_id = int(user_id or account.user_id or 0)
        cookies = latest_cookies_for_channel(db, account_id, CHANNEL_ARK) or ""
        if not cookies:
            return {"category": category, "error": "ARK_SESSION_REQUIRED"}

        adapter = XhsQianfanApiAdapter(cookies)
        # 自定义日期范围 ⇒ dateSelectType="custom" + startDate/endDate。
        effective_type = date_select_type
        effective_date_type = 1
        if start_date or end_date:
            effective_type = "custom"
            effective_date_type = -1  # custom 的 dateType 由平台按 startDate/endDate 解析
        all_rows: list[NoteDataRow] = []
        pages = 0
        for page in range(1, max_pages + 1):
            ok, msg, payload = adapter.get_note_data_list(
                category=category,
                page=page,
                size=PAGE_SIZE,
                date_select_type=effective_type,
                date_type=effective_date_type,
                start_date=start_date,
                end_date=end_date,
            )
            if not ok:
                return {"category": category, "error": msg, "pages": pages}
            rows = parse_butterfly_response(payload, category)
            pages += 1
            if not rows:
                break
            all_rows.extend(rows)
            if len(rows) < PAGE_SIZE:
                break

        result = upsert_note_data_rows(
            db, all_rows, user_id=owner_id, account_id=account_id,
            window=effective_type,
        )
        db.commit()
        # 2026-10-06 起：详情补齐**不再**在定时任务里自动拉（改由用户点击卡片时实时拉）。
        return {
            "category": category,
            "fetched": len(all_rows),
            "pages": pages,
            **result,
        }
    except Exception as exc:  # noqa: BLE001 - 单账号失败不拖垮整轮
        return {"category": category, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        db.close()


def import_note_data_file(
    *,
    account_id: int,
    category: str,
    file_path: str,
    user_id: Optional[int] = None,
) -> dict[str, Any]:
    """手动导入 xlsx 文件 → 落库（商业/普通 Tab 的「导入」按钮）。

    解析 xlsx → `upsert_note_data_rows` 落库。返回 `{category, fetched, created, updated}`。
    调用方负责在解析后删除临时文件（本函数不删，让调用方控制生命周期）。
    """
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        if account is None:
            return {"category": category, "error": "ACCOUNT_NOT_FOUND"}
        owner_id = int(user_id or account.user_id or 0)
        rows = parse_xlsx_file(file_path, category)
        result = upsert_note_data_rows(
            db, rows, user_id=owner_id, account_id=account_id, window=WINDOW_CUSTOM
        )
        db.commit()
        # 2026-10-06 起：导入**不再**自动拉详情（改由用户点击卡片时实时拉）。
        return {"category": category, "fetched": len(rows), **result}
    except Exception as exc:  # noqa: BLE001
        return {"category": category, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        db.close()


def hydrate_offline_note(
    db: Any,
    note: Any,
    *,
    fetch_payload: Any,
    limiter: Any,
) -> dict[str, Any]:
    """对**一条**离线笔记实时拉详情并落库（正文 / 媒体 / 话题拆分）。

    供两条链路共用（单一实现，DEF-044）：
    * :func:`hydrate_offline_notes`（批量，历史遗留—— 2026-10-06 起**不再被定时
      任务/导入调用**，仅保留给显式批量场景）；
    * 「点击卡片实时拉详情」端点（`POST /notes/{note_id}/hydrate`，**当前主路径**）。

    落库语义与同步链路的 `hydrate_note_detail` 同口径：正文**填空**（详情正文为空
    不抹本地）、媒体图/视频**只增不删**、话题 `apply_sync_topics` **拆表只增不删**。

    返回 `{status, filled, no_url, skipped_by_limit, failed}`。``status`` ∈
    ``ok`` / ``no_url``（缺 token）/ ``skipped_by_limit`` / ``failed``。
    """
    from backend.app.services.note_sync_service import (
        _merge_note_images,
        _needs_hydration,
        _note_detail_url,
    )
    from backend.app.services.note_topic_service import apply_sync_topics
    from backend.app.api.platforms.xhs.pc import _normalize_detail_payload
    from backend.app.api.platforms.xhs.crawl import _merge_note_assets

    result: dict[str, Any] = {
        "status": "failed",
        "filled": 0,
        "no_url": 0,
        "skipped_by_limit": 0,
        "failed": 0,
    }
    # 幂等：已有正文 + 媒体（或非视频且已补过）跳过，不重复拉。
    if not _needs_hydration(db, note):
        result["status"] = "already_hydrated"
        return result
    url = _note_detail_url(note)
    if not url or "xsec_token=" not in url:
        result["status"] = "no_url"
        result["no_url"] = 1
        return result
    if not limiter.wait_allow(str(note.id)):
        result["status"] = "skipped_by_limit"
        result["skipped_by_limit"] = 1
        return result

    success, _msg, raw_payload = fetch_payload(url)
    if not success:
        result["status"] = "failed"
        result["failed"] = 1
        return result
    detail_items = (
        (raw_payload or {}).get("data", {}).get("items")
        if isinstance(raw_payload, dict)
        else None
    )
    if not detail_items:
        result["status"] = "failed"
        result["failed"] = 1
        return result

    normalized = _normalize_detail_payload(raw_payload, url)
    content = str(normalized.get("content") or "")
    if content and not str(note.content or "").strip():
        note.content = content
    detail_topics = [str(t).strip() for t in (normalized.get("tags") or []) if str(t).strip()]
    apply_sync_topics(db, note, detail_topics)
    _merge_note_images(db, note, normalized.get("image_urls") or [])
    video_url = str(normalized.get("video_url") or normalized.get("video_addr") or "")
    if video_url:
        _merge_note_assets(db, note, [video_url], asset_type="video")
    note.hydrated_at = shanghai_now()
    result["status"] = "ok"
    result["filled"] = 1
    return result


def hydrate_offline_notes(
    *,
    account_id: int,
    user_id: Optional[int] = None,
    category: Optional[str] = None,
    session_factory: Any = None,
    fetch_payload: Any = None,
) -> dict[str, Any]:
    """离线笔记的**详情补齐**（2026-10-06 需求 4「根据笔记原链接获取详情媒体/正文/话题」）。

    与同步链路的 :func:`note_sync_service.hydrate_note_detail` 的**关键区别**：

    * **无需登录**：离线数据的 `note_url`（来自千帆 `discoveryLink`）**自带**笔记级
      `xsec_token`，走 explore SSR（:func:`note_detail_ssr.fetch_note_detail_payload`）
      **匿名**即可读详情 —— 不判 web_session 门禁（`hydrate_note_detail` 那条门禁是
      给「creator 列表落库的笔记」用的，它们没有自带 token，才需要账号会话）。
    * **幂等**：复用 `_needs_hydration`（`hydrated_at IS NULL`，或视频笔记但本地没有
      视频资产）⇒ 已有正文 + 媒体的行**不重复拉**（www 详情额度有限，重复 = 白烧）。
    * **限流**：每篇都过 `wait_allow`，超限等待不停（硬约束 C5 的刹车）。
    * **话题拆表**（用户 2026-10-06 口径「笔记话题才分到对应表」）：正文里的 `#话题`
      走 `apply_sync_topics` 拆进 `topics` 表 + `note_topics` 关系；媒体图/视频进
      `note_assets`；正文进 `notes.content`。**只有话题这一项拆表**，媒体与正文
      仍落在 `notes` 自己的字段 / 资产表。

    返回 `{attempted, filled, failed, no_url, skipped_by_limit}`。失败不向上抛
    （单篇失败不影响其余），也不写 `hydrated_at`（下次同步重试）。
    """
    from backend.app.core.rate_limiter import get_rate_limiter

    if fetch_payload is None:
        from backend.app.adapters.xhs.note_detail_ssr import fetch_note_detail_payload

        fetch_payload = fetch_note_detail_payload
    factory = session_factory or SessionLocal
    db = factory()
    stats: dict[str, Any] = {
        "attempted": 0,
        "filled": 0,
        "failed": 0,
        "no_url": 0,
        "skipped_by_limit": 0,
    }
    try:
        account = db.get(PlatformAccount, account_id)
        if account is None:
            return {**stats, "error": "ACCOUNT_NOT_FOUND"}
        owner_id = int(user_id or account.user_id or 0)

        # 候选：该账号下有数据中心统计的笔记（= 离线数据；`note_category` 已退役，
        # 2026-10-12 设计 35 改用统计表 `note_datacenter_stats` 判定存在性）。
        stat_note_ids = db.scalars(
            select(NoteDatacenterStat.note_id).where(
                NoteDatacenterStat.user_id == owner_id,
                NoteDatacenterStat.platform_account_id == account_id,
            )
        ).all()
        stmt = select(Note).where(
            Note.user_id == owner_id,
            Note.platform_account_id == account_id,
            Note.note_id.in_(stat_note_ids or {""}),
        )
        if category in CATEGORIES:
            flag = _CATEGORY_TO_FLAG.get(category, "")
            stmt = stmt.where(
                Note.note_id.in_(
                    select(NoteDatacenterStat.note_id).where(
                        NoteDatacenterStat.note_flag == flag,
                        NoteDatacenterStat.user_id == owner_id,
                        NoteDatacenterStat.platform_account_id == account_id,
                    )
                )
            )
        pending = db.scalars(stmt.order_by(Note.id)).all()

        limiter = get_rate_limiter()
        for note in pending:
            outcome = hydrate_offline_note(db, note, fetch_payload=fetch_payload, limiter=limiter)
            stats["no_url"] += outcome["no_url"]
            stats["skipped_by_limit"] += outcome["skipped_by_limit"]
            stats["failed"] += outcome["failed"]
            if outcome["status"] == "ok":
                stats["attempted"] += 1
                stats["filled"] += outcome["filled"]
                db.commit()
        return stats
    except Exception as exc:  # noqa: BLE001 - 详情补齐失败不拖垮同步/导入主流程
        db.rollback()
        return {**stats, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        db.close()
