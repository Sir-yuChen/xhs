"""实时平台查询服务（设计 75 §2.1）：商品 / 订单 / 笔记↔商品三域的**实时查询唯一实现**。

## 为什么要有这一层

本仓此前把三域数据**全量同步到本地台账**（`shop_products` / `orders` / `note_goods`），
代价是「本地与平台迟早不一致」：同步有间隔、失败会静默、平台侧改了本地不知道。
本模块把读路径整体切到**平台实时查询**，本地只保留平台不存在的数据
（生成记录 / 任务队列）。端点与后台任务只调这里 —— 各写一份必然漂移。

**判据（新增读功能时逐条过）**：这份数据换一个平台（抖音/闲鱼）还要不要存在？
要 ⇒ 落本地；不要 ⇒ 走本模块实时查，**禁止落库**。

## 四条边界（改这里必须先读）

1. **只读**：全部走 GET 或平台定义的只读 POST，**不碰任何写接口**。
2. **不打印凭据**：`cookies` 只在构造 adapter 时使用，日志一律不含 cookie / token。
3. **失败要响**：平台失败抛 :class:`RealtimeQueryError`（带稳定 `code` + 人话原因），
   由端点转 502 / 400。**不回退本地、不静默降级** —— 回退本地正是本方案要消灭的
   「两份真相」；静默空列表会让用户以为「平台上没有这件商品」。
4. **缺值留空**：平台给什么归一化什么，取不到就是 `""` / `None`，**不猜不补**（硬约束⑤）。

## 统一返回

列表方法一律返回 ``{"items": [...], "total": N, "source": "platform"}``：

* `items` —— 归一化后的行（形状与既有端点契约一致，前端渲染零改动）；
* `total` —— 平台报的总数；**平台没给就是 `None`**（不拿本页条数冒充分母）；
* `source` —— 固定 `"platform"`，便于排查「这行数据是哪来的」。

## ⚠️ 三条真机硬约束

1. **订单时间窗口必须有界** —— `get_order_page` 传 `0/0` 平台**不报错而是挂到 15s 超时**
   （2026-10-04 真机，对照表见 SDK docstring）。本模块用「今天往前 365 天 ~ 今天」兜底，
   并且**先算窗口再发请求**：窗口非法 ⇒ 抛错、**一个平台请求都不发**。
2. **`get_skus_by_item_ids` 批量上限 10** —— 超了平台回 `code 500`。本模块按
   :data:`SKU_BATCH_SIZE` 分片，并把每片的失败如实累计（**不吞**）。
3. **`item_mix_search_word` 认商品 ID**，传笔记 ID 返回空。按商品查笔记只有这一个口子。

## ⚠️ 响应形状的长相（最容易踩的一处）

SDK 的 `(ok, msg, payload)` 里 `payload` 是**完整响应体** `{code, success, msg, data: {...}}`，
业务数据在 `data` 下面。适配器**不做剥离**（见 `qianfan_api_adapter` 头部：只做环境包装 +
委托）。所以取行必须走 :func:`_data_of`，直接 `payload.get("items")` 恒为空 —— 而且
**不报错**，是最难发现的一类静默缺陷。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

#: 统一来源标记（设计 75 §3.1）。
SOURCE_PLATFORM = "platform"

#: 订单查询默认回看天数（无界会挂死 15 秒，见模块 docstring 约束 1）。
ORDER_DEFAULT_LOOKBACK_DAYS = 365

#: `get_skus_by_item_ids` 的批量上限（真机实测 10 → 200，20 → code 500）。
SKU_BATCH_SIZE = 10

#: 单页上限（平台侧最大 100；更大不会更快，只会更容易超时）。
MAX_PAGE_SIZE = 100

#: 东八区（平台时间按北京时间解释，与本仓 `core.time.shanghai_now` 同区）。
_TZ_SHANGHAI = timezone(timedelta(hours=8))

_MS_PER_DAY = 86_400_000


class RealtimeQueryError(Exception):
    """实时查询失败（结构化）。

    `code` 是**稳定**的机器判据（端点 / 前端按它分流），`message` 是给人看的原因。

    | code | 何时 | 发平台请求了吗 |
    |---|---|---|
    | `INVALID_ARGUMENT` | 必填参数缺失 / 条件全空 | ❌ 否 |
    | `ORDER_WINDOW_REQUIRED` | 订单窗口非法（起点晚于终点 / 负数） | ❌ 否 |
    | `QIANFAN_CREDENTIAL_EXPIRED` | 千帆登录态失效 | ✅ 是 |
    | `QIANFAN_QUERY_FAILED` | 其余平台失败（网络 / 结构异常 / 服务端错） | ✅ 是 |
    | `ITEM_NOT_FOUND` | 平台 `skusMap` 里没有该商品 | ✅ 是 |
    | `ORDER_NOT_FOUND` | 平台详情里没有该包裹 | ✅ 是 |
    """

    def __init__(self, code: str, message: str, *, detail: Any = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        """端点直接下发用（塞进 `HTTPException.detail`）。"""
        return {"code": self.code, "message": self.message}


class OrderWindowRequired(RealtimeQueryError):
    """订单查询未给合法窗口（**未发平台请求**）。

    保留独立类名是为了调用方 `except OrderWindowRequired` 可读；
    判据与其它错误统一（`code = "ORDER_WINDOW_REQUIRED"`）。
    """

    def __init__(
        self, message: str = "订单查询必须给出有界时间窗口（默认近 365 天）", *, detail: Any = None
    ) -> None:
        super().__init__("ORDER_WINDOW_REQUIRED", message, detail=detail)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _first_text(*values: Any) -> str:
    """第一个非空标量的文本（容器跳过 —— `str(dict)` 会把半个响应写进表格）。"""
    for value in values:
        if value is None or isinstance(value, (dict, list, tuple, set)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _data_of(raw: Any) -> dict[str, Any]:
    """平台完整响应 → `data` 字典（见模块 docstring 最后一段）。

    不是 dict / 没有 `data` ⇒ 空字典，调用方按「零行」处理（缺值口径，不造行）。
    """
    if not isinstance(raw, dict):
        return {}
    data = raw.get("data")
    return data if isinstance(data, dict) else {}


def _page_args(page: Any, page_size: Any) -> tuple[int, int]:
    """页码 / 页大小归一化（坏值给 1 / 20，**不 500**）。"""
    page_no = _int_or_none(page) or 1
    size = _int_or_none(page_size) or 20
    return max(1, page_no), max(1, min(size, MAX_PAGE_SIZE))


def _ts_text(value: Any) -> str:
    """Unix **毫秒**时间戳 → `YYYY-MM-DD HH:MM:SS`（东八区）；取不到给空串。

    商品 `create_time` / `on_shelf_time` / `update_time` 都是毫秒；
    0 / 负数 / 非法值一律空串（**不渲染 1970**）。
    """
    stamp = _int_or_none(value)
    if not stamp or stamp <= 0:
        return ""
    return datetime.fromtimestamp(stamp / 1000, tz=_TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def _image_links(item: dict[str, Any], key: str = "images") -> list[str]:
    rows = item.get(key)
    if not isinstance(rows, list):
        return []
    links: list[str] = []
    for row in rows:
        link = _first_text(row.get("link"), row.get("url")) if isinstance(row, dict) else ""
        if link:
            links.append(link)
    return links


def _num_text(value: Any) -> Optional[str]:
    """金额原样保留两位文本；`None` 保留 `None`（不造 0）。"""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return None


def _raise_if_failed(ok: bool, message: str, raw: Any, *, what: str) -> None:
    """SDK 三元组 → 结构化异常（成功时直接返回）。

    ⚠️ 凭据失效走 SDK 的**唯一判据** `is_credential_error`（DEF-084 的教训：
    拿中文 msg 做子串匹配的分支从来没命中过，令牌过期时白跑完全部批次）。
    """
    if ok:
        return
    from apis.xhs_qianfan_apis import XHS_Qianfan_Apis

    payload = raw if isinstance(raw, dict) else {}
    if XHS_Qianfan_Apis.is_credential_error(payload.get("code")):
        raise RealtimeQueryError(
            "QIANFAN_CREDENTIAL_EXPIRED",
            message or "千帆登录凭证已过期，请重新登录千帆",
            detail=payload,
        )
    raise RealtimeQueryError(
        "QIANFAN_QUERY_FAILED", message or f"{what}失败（平台未给出原因）", detail=payload
    )


# --------------------------------------------------------------------------- #
# 时间窗口（设计 75 §1.3 的唯一实现）
# --------------------------------------------------------------------------- #
def _parse_bound(value: Any) -> Optional[datetime]:
    """日期入参 → 东八区 **naive** datetime；解析不了按「没给」处理。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(_TZ_SHANGHAI).replace(tzinfo=None) if value.tzinfo else value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_TZ_SHANGHAI)
    return parsed.replace(tzinfo=None)


def resolve_order_window(
    start_time: Any = None,
    end_time: Any = None,
    *,
    now: Optional[datetime] = None,
    lookback_days: int = ORDER_DEFAULT_LOOKBACK_DAYS,
) -> tuple[datetime, datetime]:
    """把订单查询窗口归一化成**有界**的 ``(start, end)``（东八区 naive datetime）。

    容忍 `datetime` / `date` / `"YYYY-MM-DD"` / `"YYYY-MM-DD HH:MM[:SS]"` / ISO 串；
    空值按「没给」。

    * 两端都缺 ⇒ `[今天-lookback_days 00:00, 现在]`；
    * 只缺一端 ⇒ 用默认窗口的对应端点补齐（**绝不把缺的一端留成 0**，那正是挂死的写法）；
    * 起点晚于终点 ⇒ 抛 :class:`OrderWindowRequired`（**在发请求之前**）。
    """
    anchor = (now or datetime.now(_TZ_SHANGHAI)).astimezone(_TZ_SHANGHAI).replace(tzinfo=None)
    default_start = (anchor - timedelta(days=max(1, int(lookback_days)))).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    start = _parse_bound(start_time)
    end = _parse_bound(end_time)
    start = start if start is not None else default_start
    end = end if end is not None else anchor
    if start > end:
        raise OrderWindowRequired(
            f"订单查询的开始时间（{start:%Y-%m-%d %H:%M:%S}）晚于结束时间"
            f"（{end:%Y-%m-%d %H:%M:%S}），请重新选择时间范围"
        )
    return start, end


def _to_epoch_ms(value: datetime) -> int:
    return int(value.replace(tzinfo=_TZ_SHANGHAI).timestamp() * 1000)


def _to_epoch_s(value: Any) -> Optional[int]:
    """时间入参 → Unix **秒**（商品 `create_time_from/to` 的单位；毫秒会被下游 ES 拒成 500）。

    容忍三种形态（调用方来源不同，不该逼它们各自换算）：

    * `datetime` / `date` / `"YYYY-MM-DD[ HH:MM[:SS]]"` —— 按**东八区**解释；
    * **数值** —— 10 位（秒）原样，13 位（毫秒）÷ 1000；`0` / 负数 / 其它位数按「没给」。

    ⚠️ 数值分支是本层必须容错的实证：端点侧算出来的是**毫秒**（`_epoch_millis`），
    只认日期串的话毫秒会被当成「没给」**静默丢掉** —— 页面筛了时间却返回全量，
    而且零报错（本仓 DEF 级形态）。
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        stamp = int(value)
        if stamp <= 0:
            return None
        # 13 位 = 毫秒（2001 年之后的毫秒值都 ≥ 13 位）；10 位 = 秒
        return stamp // 1000 if stamp >= 100_000_000_000 else stamp
    parsed = _parse_bound(value)
    return None if parsed is None else int(parsed.replace(tzinfo=_TZ_SHANGHAI).timestamp())


# --------------------------------------------------------------------------- #
# 商品域
# --------------------------------------------------------------------------- #
def status_key_from_raw(item: Any) -> str:
    """商品行的状态键（生命周期硬信号推导，**与台账 `status_key` 同一套判据**）。

    ⚠️ 平台列表响应**不含 `card_type`**（2026-09-22 全量核对 1974 条 0 命中），
    行级状态只能推：

    * `item_freeze` ⇒ `frozen`；
    * `deleted` ⇒ `""`（不归族，不猜）；
    * 有 `off_shelf_time` **或**没有 `on_shelf_time` ⇒ `off_shelf`；
    * 其余 ⇒ `on_shelf`。

    审核态四相（`auditing`/`audit_rejected`/`audit_passed`/`rectify`）无法从列表字段可靠
    区分 ⇒ 返回 `""`（诚实边界，设计 13 §30.3.2）。
    """
    if not isinstance(item, dict):
        return ""
    if item.get("item_freeze"):
        return "frozen"
    if item.get("deleted"):
        return ""
    if item.get("off_shelf_time") or not item.get("on_shelf_time"):
        return "off_shelf"
    return "on_shelf"


def _status_label(status_key: str) -> str:
    from apis.xhs_qianfan_apis import QIANFAN_ITEM_STATUS_LABELS

    return QIANFAN_ITEM_STATUS_LABELS.get(status_key, "") if status_key else ""


def normalize_product(item: Any) -> dict[str, Any]:
    """千帆商品条目（`search_item_v2` 的 `data.items[]`）→ 前端契约（**白名单**）。

    字段名与 `shop_products_service.normalize_item_from_raw` **一致**（那是列表 / 详情 /
    导出的唯一契约）；差别只在数据来源 —— 这里是平台原文，那里是本地 `raw_json`（同构）。
    价格单位是**分**（平台原样），展示层再换算。
    """
    row = item if isinstance(item, dict) else {}
    images = _image_links(row)
    sale_qty = row.get("sale_qty_info") if isinstance(row.get("sale_qty_info"), dict) else {}
    risk = row.get("inventory_risk_info") if isinstance(row.get("inventory_risk_info"), dict) else {}
    status_key = status_key_from_raw(row)

    return {
        "item_id": _first_text(row.get("item_id")),
        "title": _first_text(row.get("item_name"), row.get("item_name_with_brand_name")),
        "article_no": _first_text(row.get("article_no")),
        "image": images[0] if images else "",
        "images": images[:9],
        "image_count": len(images),
        "price_min_fen": _int_or_none(row.get("min_price")),
        "price_max_fen": _int_or_none(row.get("max_price")),
        "stock": _int_or_none(row.get("total_stock")),
        "sku_count": _int_or_none(row.get("sku_count")),
        "on_sale_sku_count": _int_or_none(row.get("on_sale_sku_count")),
        "sales_total": _int_or_none(sale_qty.get("acc_sale_qty")),
        "sales_30d": _int_or_none(sale_qty.get("sale_qty30")),
        #: 平台口径的挂链笔记数（原文 `product_note_num`）—— **只作对照**，不是列表展示的
        #: `note_count`（后者走关系表 / 实时 `list_goods_notes`，设计 13 §32.4）。
        "platform_note_count": _int_or_none(row.get("product_note_num")) or 0,
        "note_ids": [str(x) for x in (row.get("product_note_ids") or []) if x],
        "status_key": status_key,
        "status_label": _status_label(status_key),
        "buyable": bool(row.get("buyable")),
        "frozen": bool(row.get("item_freeze")),
        "deleted": bool(row.get("deleted")),
        "audit_status": _int_or_none(row.get("item_audit_status")),
        "check_status": _int_or_none(row.get("check_status")),
        "is_channel": bool(row.get("is_channel")),
        "stock_warning_sku_count": _int_or_none(risk.get("warning_sku_count")) or 0,
        "create_time": _ts_text(row.get("create_time")),
        "on_shelf_time": _ts_text(row.get("on_shelf_time")),
        "update_time": _ts_text(row.get("update_time")),
        "xsec_token": _first_text(row.get("xsec_token")),
        # ------------------------------------------------------------------ #
        # 下面四组是「商品列表 / 详情」这一层的展示与**门禁**输入（设计 75 §4.1
        # G6）；台账时代的 `normalize_item_from_raw` 已下发同名字段，实时链路必须
        # 逐字段对齐，否则前端渲染与上下架按钮的判据会静默退化。
        # ------------------------------------------------------------------ #
        #: 分类路径（`categories[]` 按 `level` 升序拼「一级 / 二级 / 三级」）。
        "category": _category_path(row.get("categories")),
        #: 平台在售活动标志（定金预售等）—— 拦截下架 / 删除。
        "is_item_attend_promotion": bool(row.get("is_item_attend_promotion")),
        #: 平台处罚结果（真机 1 = 正常）。上架时判违规。
        "item_punish_result": _int_or_none(row.get("item_punish_result")),
        #: 「这件商品哪些操作被锁」列表，只为上下架 / 删除三个动作服务 ⇒ 裁到需要的键。
        "item_lock_infos": [
            {
                "lockField": _first_text(lock.get("lockField")),
                "lockDimension": _first_text(lock.get("lockDimension")),
                "lockDesc": _first_text(lock.get("lockDesc")),
            }
            for lock in (row.get("item_lock_infos") or [])
            if isinstance(lock, dict)
        ],
    }


def _category_path(categories: Any) -> str:
    """`categories[]` → 「一级 / 二级 / 三级」（按 `level` 升序拼接，去掉空段）。

    ⚠️ 与 `shop_products_service._category_path` **逐字同一判据**（台账时代列表的
    `category` 列由它构造）。两份实现在这里不可避免（一个在服务层给台账、一个给
    实时链路），所以守卫 `test_shop_items_realtime.py` 用同输入对拍两者相等 ——
    改任一处而另一处不改会立刻红。
    """
    if not isinstance(categories, list):
        return ""
    rows = [row for row in categories if isinstance(row, dict)]
    rows.sort(key=lambda row: row.get("level") or 0)
    names = [str(row.get("name") or "").strip() for row in rows]
    return " / ".join(name for name in names if name)


def list_products(
    adapter: Any,
    *,
    keyword: Any = None,
    status: Any = None,
    page: Any = 1,
    page_size: Any = 20,
    price_min_fen: Any = None,
    price_max_fen: Any = None,
    stock_min: Any = None,
    stock_max: Any = None,
    sales_min: Any = None,
    sales_max: Any = None,
    create_from: Any = None,
    create_to: Any = None,
    sort_field: Any = None,
    order: Any = None,
) -> dict[str, Any]:
    """商品列表一页（`POST /api/edith/product/search_item_v2`，只读）。

    ## 三个参数口径（照抄 SDK，不在这里另立）

    * `status` —— 取 `QIANFAN_ITEM_STATUSES` 的键（`on_shelf` / `off_shelf` / …）；
      `"all"` / 空 / 坏值 ⇒ **不传 `card_type`**（页面点「全部」时自己也不发这个字段；
      退成某个具体状态会静默少掉其余状态，`sync_task_service.normalize_item_status`
      同一条纪律）；
    * 价格 / 库存 / 销量 —— 单位是**分**与原样计数，本层不换算；
    * 创建时间 —— `create_from` / `create_to` 收日期串 / `datetime`，本层转平台要的
      **Unix 秒**。
    """
    from apis.xhs_qianfan_apis import QIANFAN_ITEM_STATUSES

    page_no, size = _page_args(page, page_size)
    status_key = _first_text(status).lower()
    # 未知键 ⇒ "all" ⇒ SDK 不加 card_type（SDK 的 `QIANFAN_ITEM_STATUSES.get()` 已兜底，
    # 这里显式写成 "all" 是为了让本层的意图可读、可测）。
    sdk_status = status_key if status_key in QIANFAN_ITEM_STATUSES else "all"

    ok, message, raw = adapter.search_items(
        page_no=page_no,
        page_size=size,
        keyword=_first_text(keyword) or None,
        status=sdk_status,
        price_from_fen=_int_or_none(price_min_fen),
        price_to_fen=_int_or_none(price_max_fen),
        stock_from=_int_or_none(stock_min),
        stock_to=_int_or_none(stock_max),
        sales_from=_int_or_none(sales_min),
        sales_to=_int_or_none(sales_max),
        create_time_from=_to_epoch_s(create_from),
        create_time_to=_to_epoch_s(create_to),
        sort_field=_first_text(sort_field) or "create_time",
        order=_first_text(order) or "desc",
    )
    _raise_if_failed(ok, message, raw, what="商品列表获取")

    data = _data_of(raw)
    rows = [row for row in (data.get("items") or []) if isinstance(row, dict)]
    return {
        "items": [normalize_product(row) for row in rows],
        "total": _int_or_none(data.get("total")),
        "source": SOURCE_PLATFORM,
        "page": page_no,
        "page_size": size,
        "status_key": status_key or "all",
    }


def normalize_sku(sku: Any) -> Optional[dict[str, Any]]:
    """`get_skus_by_item_ids` 的 SKU 原文 → 白名单（缺 `skuId` ⇒ `None`，跳过不计）。

    形状与台账 `detail_json` 里的 SKU 一致（`shop_goods_sync_service.normalize_skus`），
    这样 `item-detail` 切到实时后前端渲染零改动。
    """
    if not isinstance(sku, dict):
        return None
    sku_id = _first_text(sku.get("skuId"))
    if not sku_id:
        return None
    price = sku.get("basePriceInfo") if isinstance(sku.get("basePriceInfo"), dict) else {}
    spec_image = sku.get("specImage") if isinstance(sku.get("specImage"), dict) else {}
    buyable = sku.get("buyable")
    freeze = sku.get("freeze")
    return {
        "sku_id": sku_id,
        "sku_name": _first_text(sku.get("skuName")),
        "variant_name": _first_text(sku.get("variantName")),
        "barcode": _first_text(sku.get("barcode")),
        "erp_code": _first_text(sku.get("erpCode")),
        "price_fen": _int_or_none(price.get("price")),
        "original_price_fen": _int_or_none(price.get("originalPrice")),
        "stock": _sku_stock(sku),
        "spec_image": _first_text(spec_image.get("link")),
        "weight": _int_or_none(sku.get("weight")),
        "buyable": buyable if isinstance(buyable, bool) else None,
        "freeze": freeze if isinstance(freeze, bool) else None,
    }


def _sku_stock(sku: dict[str, Any]) -> Optional[int]:
    value = _int_or_none(sku.get("stock"))
    if value is not None:
        return value
    rows = sku.get("baseStockInfos")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                cell = _int_or_none(row.get("stock"))
                if cell is not None:
                    return cell
    return None


def get_product_detail(adapter: Any, *, item_ids: Any) -> dict[str, Any]:
    """按商品 ID 批量取 SKU 明细（`POST /api/edith/product/get_skus_by_item_ids`，只读）。

    返回 ``{"items": [{"item_id", "skus", "sku_count"}], "total", "source", "missing"}``：

    * `items` —— 平台 `skusMap` 里**返回了**的商品（按传入顺序）；
    * `missing` —— 传了但 `skusMap` 没有的 ID（**平台查不到 ≠ 请求失败**，调用方按
      404 / 跳过处理）；
    * `total` —— `len(items)`，本条查询的**确切**命中数（平台不给总数，这不是估算）。

    ⚠️ 平台**批量上限 10**：本层按 :data:`SKU_BATCH_SIZE` **自动分片**，任一分片失败
    都抛错（**不吞**）—— 吞掉一片会让「10 个商品里 3 个详情为空」看起来像「平台没数据」。
    """
    ids = [str(x).strip() for x in (item_ids or []) if str(x or "").strip()]
    if not ids:
        raise RealtimeQueryError("INVALID_ARGUMENT", "item_ids 为空，无法查询商品详情")
    ids = list(dict.fromkeys(ids))  # 去重保序（同一 ID 传两次不该拿两条明细）

    skus_map: dict[str, Any] = {}
    for offset in range(0, len(ids), SKU_BATCH_SIZE):
        chunk = ids[offset : offset + SKU_BATCH_SIZE]
        ok, message, raw = adapter.get_skus_by_item_ids(chunk)
        _raise_if_failed(ok, message, raw, what=f"商品详情获取（{len(chunk)} 个商品）")
        page_map = _data_of(raw).get("skusMap")
        if isinstance(page_map, dict):
            skus_map.update(page_map)

    items: list[dict[str, Any]] = []
    missing: list[str] = []
    for item_id in ids:
        raw_skus = skus_map.get(item_id)
        if not isinstance(raw_skus, list):
            missing.append(item_id)
            continue
        skus = [
            normalized
            for normalized in (normalize_sku(sku) for sku in raw_skus)
            if normalized is not None
        ]
        items.append({"item_id": item_id, "skus": skus, "sku_count": len(skus)})

    return {
        "items": items,
        "total": len(items),
        "source": SOURCE_PLATFORM,
        "missing": missing,
    }


# --------------------------------------------------------------------------- #
# 笔记 ↔ 商品（挂链）
# --------------------------------------------------------------------------- #
def _field_int(entry: dict[str, Any], key: str) -> Optional[int]:
    """千帆部分指标是 `{"value": 40, "ratio": …}` 单元格包裹（数据中心口径）。"""
    value = entry.get(key)
    if isinstance(value, dict):
        return _int_or_none(value.get("value"))
    return _int_or_none(value)


def normalize_goods_note(entry: Any) -> Optional[dict[str, Any]]:
    """千帆 `goods-note/list` 的 `data.note_infos[]` → 前端契约（缺 `note_id` ⇒ `None`）。

    计数类指标缺值给 **0**（前端「计数缺值渲染 0」的唯一例外，硬约束⑤）；
    字节类（标题 / 封面 / 金额）缺值给 `""` / `None`，不造。
    """
    if not isinstance(entry, dict):
        return None
    note_id = _first_text(entry.get("note_id"))
    if not note_id:
        return None
    return {
        "note_id": note_id,
        "title": _first_text(entry.get("note_title")),
        "cover": _first_text(entry.get("note_image")),
        "note_type": _field_int(entry, "note_type"),
        "read_count": _field_int(entry, "browse_num") or 0,
        "like_count": _field_int(entry, "like_num") or 0,
        "collect_count": _field_int(entry, "collect_num") or 0,
        "comment_count": _field_int(entry, "comment_num") or 0,
        "share_count": _field_int(entry, "share_num") or 0,
        "pay_gmv": _first_text(entry.get("pay_gmv"), entry.get("note_pay_gmv")),
        "pay_order_count": _field_int(entry, "pay_order_cnt") or 0,
        "publish_time": _first_text(entry.get("publish_time")),
        "status": _first_text(entry.get("publish_status"), entry.get("status_desc")),
        #: `goods_note` = 平台自报的商业笔记（平台原生判据，本层不反推）。
        "note_flag": _first_text(entry.get("note_flag")),
        "xsec_token": _first_text(entry.get("xsec_token")),
        #: 挂载商品（平台原文 `goods_infos[]` → 只留 id / 名称 / 图）
        "goods": [
            {
                "item_id": _first_text(row.get("item_id"), row.get("itemId")),
                "title": _first_text(row.get("item_title"), row.get("item_name")),
                "image": _first_text(row.get("item_image")),
            }
            for row in (entry.get("goods_infos") or [])
            if isinstance(row, dict)
        ],
    }


def list_goods_notes(
    adapter: Any,
    *,
    item_id: Any = None,
    note_ids: Any = None,
    mix_search_word: Any = None,
    page: Any = 1,
    page_size: Any = 20,
) -> dict[str, Any]:
    """「笔记 ↔ 商品」实时查询（`GET /api/edith/goods-note/list`，只读）。

    三种入口**互斥、优先级固定**（平台这三个参数彼此独立，混传只会拿到空集）：

    1. `item_id` ⇒ `item_mix_search_word=<item_id>` —— **正向**：这个商品挂了哪些笔记
       （千帆笔记管理页搜索框，2026-10-26 真机验证）；
    2. `note_ids` ⇒ `note_ids=<逗号分隔>` —— **反向**：这些笔记挂了哪些商品（复数，
       精确过滤；单数 `note_id` 会被平台忽略）；
    3. `mix_search_word` ⇒ 标题关键词搜索（服务端过滤）。

    条件全空 ⇒ `INVALID_ARGUMENT`（无条件翻页 = 把整店铺笔记当结果，绝不是调用方要的）。

    返回额外带 `has_more`（平台 `end_tag` 取反）与 `item_id`（正向查询回显）。
    """
    page_no, size = _page_args(page, page_size)
    item_text = _first_text(item_id)
    if isinstance(note_ids, (list, tuple, set)):
        ids = [str(x).strip() for x in note_ids if str(x or "").strip()]
    else:
        ids = [str(note_ids).strip()] if _first_text(note_ids) else []
    ids = [x for x in dict.fromkeys(ids) if x]
    word = _first_text(mix_search_word)

    kwargs: dict[str, Any] = {"page_num": page_no, "page_size": size}
    if item_text:
        kwargs["item_mix_search_word"] = item_text
    elif ids:
        kwargs["note_ids"] = ids
    elif word:
        kwargs["mix_search_word"] = word
    else:
        raise RealtimeQueryError(
            "INVALID_ARGUMENT", "至少需要 item_id / note_ids / mix_search_word 之一"
        )

    ok, message, raw = adapter.get_goods_note_page(**kwargs)
    _raise_if_failed(ok, message, raw, what="挂链笔记获取")

    data = _data_of(raw)
    rows = [row for row in (data.get("note_infos") or []) if isinstance(row, dict)]
    notes = [
        normalized
        for normalized in (normalize_goods_note(row) for row in rows)
        if normalized is not None
    ]
    return {
        "items": notes,
        "total": _int_or_none(data.get("total")),
        "source": SOURCE_PLATFORM,
        "page": page_no,
        "page_size": size,
        #: `end_tag=True` ⇒ 没有下一页。
        "has_more": not bool(data.get("end_tag")),
        "item_id": item_text,
    }


def list_notes_by_item_ids(adapter: Any, *, item_ids: Iterable[str]) -> dict[str, Any]:
    """多个商品各自的挂链笔记数（``{"counts": {item_id: n}, "source": "platform"}``）。

    **逐个商品一次请求**（平台的过滤参数只认单个商品 ID），只有 `page_size=1` 用来取
    `total`。⚠️ 成本与商品数线性相关（10 个商品 = 10 次请求）⇒ 调用方必须控制规模，
    也可以直接用 :func:`list_goods_notes` 逐条拿详情。

    任一商品查询失败都抛错（**不吞**：少一个商品的计数，列表页那个数字就是错的）。
    """
    counts: dict[str, int] = {}
    for item_id in dict.fromkeys(str(i).strip() for i in (item_ids or [])):
        if not item_id:
            continue
        result = list_goods_notes(adapter, item_id=item_id, page=1, page_size=1)
        counts[item_id] = _int_or_none(result.get("total")) or 0
    return {"counts": counts, "source": SOURCE_PLATFORM}


# --------------------------------------------------------------------------- #
# 订单域
# --------------------------------------------------------------------------- #
def normalize_order(package: Any) -> Optional[dict[str, Any]]:
    """千帆订单条目（`data.packages[]`）→ 前端契约（缺 `orderId` ⇒ `None`）。

    ⚠️ 隐私字段是平台**脱敏**后的原样值（`及*` / `184***3034`）—— 不猜不补。
    """
    if not isinstance(package, dict):
        return None
    order_no = _first_text(package.get("orderId"))
    if not order_no:
        return None
    user_info = package.get("userInfo") if isinstance(package.get("userInfo"), dict) else {}
    mark = package.get("sellerMarkInfo") if isinstance(package.get("sellerMarkInfo"), dict) else {}
    return {
        "order_no": order_no,
        "package_id": _first_text(package.get("packageId")),
        "status": _int_or_none(package.get("status")),
        "status_desc": _first_text(package.get("statusDesc")),
        "after_sale_status": _int_or_none(package.get("afterSaleStatus")),
        "after_sale_status_desc": _first_text(package.get("afterSaleStatusDesc")),
        "type_desc": _first_text(package.get("typeDesc")),
        "buyer": _first_text(user_info.get("nickName"), user_info.get("name")),
        "amount": _num_text(package.get("totalOrderAmount")),
        "paid_amount": _num_text(package.get("actualPaid")),
        "freight": _num_text(package.get("shippingFee")),
        "created_at": _first_text(package.get("orderedAt")),
        "paid_at": _first_text(package.get("paidAt")),
        "shipped_at": _first_text(package.get("shippedAt")),
        "finished_at": _first_text(package.get("finishedAt")),
        "updated_at": _first_text(package.get("updatedAt")),
        "logistics_company": _first_text(package.get("expressCompanyName")),
        "tracking_no": _first_text(package.get("expressNo")),
        "seller_mark": _first_text(mark.get("sellerMarkNote")),
        "skus": [
            {
                "sku_id": _first_text(row.get("skuId")),
                "item_id": _first_text(row.get("itemId")),
                "name": _first_text(row.get("skuName")),
                "spec": _first_text(row.get("skuSpecification"), row.get("specification")),
                "quantity": _int_or_none(row.get("skuQuantity")),
                "unit_price": _num_text(row.get("skuSoldPrice")),
                "amount": _num_text(row.get("skuTotalPaidAmount")),
                "image": _first_text(row.get("image")),
            }
            for row in (package.get("skus") or [])
            if isinstance(row, dict)
        ],
        "receiver": {
            "name": _first_text(user_info.get("name")),
            "phone": _first_text(user_info.get("phone")),
            "province": _first_text(user_info.get("province")),
            "city": _first_text(user_info.get("city")),
            "district": _first_text(user_info.get("district")),
            "address": _first_text(user_info.get("address"), user_info.get("street")),
        },
    }


def list_orders(
    adapter: Any,
    *,
    start_time: Any = None,
    end_time: Any = None,
    status: Any = None,
    keyword: Any = None,
    page: Any = 1,
    page_size: Any = 20,
    lookback_days: int = ORDER_DEFAULT_LOOKBACK_DAYS,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """订单列表一页（`POST /api/edith/fulfillment/order/page`，只读）。

    ⚠️ **时间窗口必须有界**（设计 75 §1.3）：缺窗口用「今天往前 365 天 ~ 今天」兜底，
    窗口非法抛 :class:`OrderWindowRequired`，**一个平台请求都不发**（无界实测挂死 15 秒，
    比立刻报错糟得多）。

    `status` / `keyword` **目前不下发平台**：`order/page` 的状态过滤走数字码
    `status: []` + `order_tag_list` 等结构化字段，尚未真机取证 —— 只读边界内不臆造过滤。
    签名保留在契约里（端点透传口径已定），实现等取证后再接；调用方按行再筛也成立。

    返回额外带 `start_time` / `end_time`（**实际生效**的窗口，兜底后的值）与 `has_more`。
    """
    window_start, window_end = resolve_order_window(
        start_time, end_time, now=now, lookback_days=lookback_days
    )
    page_no, size = _page_args(page, page_size)

    ok, message, raw = adapter.get_order_page(
        page_no=page_no,
        page_size=size,
        start_time_ms=_to_epoch_ms(window_start),
        end_time_ms=_to_epoch_ms(window_end),
    )
    _raise_if_failed(ok, message, raw, what="订单列表获取")

    data = _data_of(raw)
    packages = [row for row in (data.get("packages") or []) if isinstance(row, dict)]
    orders = [
        normalized
        for normalized in (normalize_order(pkg) for pkg in packages)
        if normalized is not None
    ]
    total = _int_or_none(data.get("total"))
    return {
        "items": orders,
        "total": total,
        "source": SOURCE_PLATFORM,
        "page": page_no,
        "page_size": size,
        "start_time": window_start.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": window_end.strftime("%Y-%m-%d %H:%M:%S"),
        #: 平台不返回 `end_tag`：按 `total` 与已翻页数推断（`total` 缺失时给 `None`）。
        "has_more": (total > page_no * size) if total is not None else None,
    }


def order_metrics(rows: Iterable[Any]) -> dict[str, Any]:
    """订单行（`:func:`normalize_order` 的输出）→ 监控指标（设计 16 §7.7 口径）。

    迁移前口径在本地台账里算（`order_sync_service.order_metrics`），订单域改实时后
    唯一的数据源是平台行 ⇒ 计算逻辑搬到这里，**口径逐条不变**：

    * **GMV** = `paid_amount`（实付）之和，不是 `amount`（商品总额）；
    * **客单价** = GMV / 已支付订单数（「已支付」= 实付字段有值，分母不含待付款）；
    * **退款率** = 有售后单的订单数 / 已完成订单数（`after_sales` 非空即算，不看金额 ——
      迁移前的判据同样只看「有没有售后行」）；
    * **平均发货时长** = `shipped_at - paid_at`（小时，一位小数），两端都有值才算；
    * `by_status` 每一档都建键（缺值 0），前端不用补样板。

    ⚠️ 缺值口径：分母为 0 时比率给 `None` 而不是 0（0 会被读成「退款率 0%」）。
    """
    from backend.app.models.order import (
        ORDER_STATUS_FINISHED,
        ORDER_STATUSES,
        resolve_order_status,
    )

    orders = [row for row in rows if isinstance(row, dict)]
    by_status: dict[str, int] = {status: 0 for status in ORDER_STATUSES}
    paid_amounts: list[float] = []
    finished = 0
    refunded = 0
    ship_hours: list[float] = []
    for row in orders:
        status_text = resolve_order_status(row.get("status"), row.get("after_sale_status"))
        if status_text in by_status:
            by_status[status_text] += 1
        if status_text == ORDER_STATUS_FINISHED:
            finished += 1
        if row.get("after_sales"):
            refunded += 1
        paid_text = row.get("paid_amount")
        try:
            paid_value = float(paid_text) if paid_text not in (None, "") else None
        except (TypeError, ValueError):
            paid_value = None
        if paid_value is not None:
            paid_amounts.append(paid_value)
        shipped = _parse_bound(row.get("shipped_at"))
        paid_at = _parse_bound(row.get("paid_at"))
        if shipped is not None and paid_at is not None and shipped >= paid_at:
            ship_hours.append((shipped - paid_at).total_seconds() / 3600.0)
    gmv = sum(paid_amounts)
    return {
        "order_count": len(orders),
        "gmv": round(gmv, 2),
        "avg_order_value": round(gmv / len(paid_amounts), 2) if paid_amounts else None,
        "paid_count": len(paid_amounts),
        "finished_count": finished,
        "refund_count": refunded,
        "refund_rate": round(refunded / finished, 4) if finished else None,
        "avg_ship_hours": round(sum(ship_hours) / len(ship_hours), 1) if ship_hours else None,
        "by_status": by_status,
    }


def get_order_detail(
    adapter: Any, *, package_id: Any, order_no: str = ""
) -> dict[str, Any]:
    """订单详情（`POST /api/edith/fulfillment/order/detail`，只读）。

    ⚠️ 平台入参是 **`package_id`**（不是订单号 / `orderId`）：传订单号平台回
    `code=-1, "required param check: package_id"`（真机实测）。本函数参数名就叫
    `package_id`，**不**接受订单号 —— 免得调用方以为传 `orderId` 也行。
    （`order_no → package_id` 的映射由订单列表提供，本层不做隐式转换。）

    返回 ``{"item": {...} | None, "items": [...], "total", "source"}``：`item` 是详情行
    （列表条目的**超集**，多出物流轨迹 / 售后单 / 运费险），`items` 保持统一列表形状
    （0 或 1 条），两者同源。
    """
    pid = _first_text(package_id)
    if not pid:
        raise RealtimeQueryError(
            "INVALID_ARGUMENT", "缺少 package_id，无法查询订单详情（平台不收订单号）"
        )

    ok, message, raw = adapter.get_order_detail(pid)
    _raise_if_failed(ok, message, raw, what="订单详情获取")

    package = _data_of(raw).get("fulfillment_package")
    #: ⚠️ **详情与列表的字段命名不同**（2026-10-27 真机实测）：
    #: 列表 `packages[]` 是驼峰（`orderId` / `packageId`），详情 `fulfillment_package`
    #: 是**下划线**（`package_id`，且**没有** `orderId`）。`normalize_order` 按驼峰判
    #: 缺值 ⇒ 直接喂会恒得 `None`（详情页永远空白，且**不报错**）。
    #: 这里先补一份驼峰别名，再走同一个归一化 —— 不另写第二份白名单。
    if isinstance(package, dict):
        package = dict(package)
        package.setdefault("packageId", package.get("package_id") or "")
        #: 详情响应**根本没有订单号**（只有 `package_id`）⇒ 由调用方从列表带进来。
        #: 不传就会因缺 `orderId` 被 `normalize_order` 判为空（页面空白且不报错）。
        package.setdefault("orderId", order_no or "")
        package.setdefault("orderedAt", package.get("ordered_at") or "")
    detail = normalize_order(package) if isinstance(package, dict) else None
    if detail is not None:
        detail["delivery_packages"] = [
            row for row in (package.get("delivery_packages") or []) if isinstance(row, dict)
        ]
        detail["after_sales"] = [
            row for row in (package.get("after_sale_list") or []) if isinstance(row, dict)
        ]
        detail["carriage_insurance"] = (
            package.get("carriage_insurance")
            if isinstance(package.get("carriage_insurance"), dict)
            else None
        )
        detail["confirmed_at"] = _first_text(package.get("confirmed_at"))
    return {
        "item": detail,
        "items": [detail] if detail is not None else [],
        "total": 1 if detail is not None else 0,
        "source": SOURCE_PLATFORM,
    }
