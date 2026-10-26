"""千帆商品条目的**纯契约函数**（零 DB、零平台请求）。

## 设计 75 S5（2026-10-27）之后这个模块还剩什么

商品数据源整体切到平台实时查询（`services/realtime_platform_query_service.py`），
本地台账表 `shop_products` 已随迁移 `n8o9p0q1r2s3` DROP。原本住在这里的
**全部 DB / 平台函数**（`query_items` / `item_detail` / `delete_items` / `export_rows` /
`edit_product` / `load_product_dict_from_ledger` / `recent_item_ids_for_account` …）
一并退场 —— 它们唯一的输入（台账行）已经不存在了。

留下的是**平台原文 → 展示契约**的纯函数：实时链路、生成链路、详情图弹窗都要用
同一份口径，它们不属于任何一张表。

| 函数 | 用途 |
|---|---|
| `normalize_item_from_raw` | 平台条目 → 前端契约（**白名单构造**，唯一实现） |
| `status_key_from_raw` / `status_label_from_raw` | 上下架门禁（`shop_item_operate_gates`）的判据原料 |
| `product_image_links` / `detail_image_links` | 详情图弹窗参考图分组、`media_generation` |
| `_image_links` / `_category_path` / `_ts_text` | 上面几个的内部件 |

⚠️ **不要往这里加 DB 查询**：商品数据一律走 `rt.list_products` / `rt.get_product_detail`。
"""
from __future__ import annotations

from typing import Any, Optional


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ts_text(value: Any) -> str:
    """毫秒时间戳 → `YYYY-MM-DD HH:MM:SS`（东八区）；取不到给空串。

    ⚠️ **精度必须到秒**（设计 13 §32.1，2026-09-23 用户口径）：原来是 `%H:%M`，
    同一批同步入库的商品创建时间因此**逐行显示成同一个字符串**，用户看不出列表
    按什么排（观感 = 「默认不排序」）。排序依据（时间列）必须能分辨，否则
    「排了但看不见」与「没排」在界面上没区别。
    """
    try:
        stamp = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    from datetime import datetime, timedelta, timezone

    return (datetime.fromtimestamp(stamp / 1000, tz=timezone(timedelta(hours=8)))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _category_path(categories: Any) -> str:
    """`categories[]` → 「一级 / 二级 / 三级」（按 level 升序拼接，去掉空段）。"""
    if not isinstance(categories, list):
        return ""
    rows = [row for row in categories if isinstance(row, dict)]
    rows.sort(key=lambda row: row.get("level") or 0)
    names = [str(row.get("name") or "").strip() for row in rows]
    return " / ".join(name for name in names if name)


def _image_links(item: dict[str, Any]) -> list[str]:
    links: list[str] = []
    for row in item.get("images") or []:
        if isinstance(row, dict):
            link = _first_text(row.get("link"), row.get("url"))
            if link:
                links.append(link)
    for row in item.get("image_descriptions") or []:
        if isinstance(row, dict):
            link = _first_text(row.get("link"))
            if link and link not in links:
                links.append(link)
    return links


def product_image_links(item: dict[str, Any]) -> list[str]:
    """**商品主图**（`raw_json.images`）—— 与详情图**分开**（v2.15，设计 17 §13.3.6）。

    ⚠️ 与 `_image_links` 的区别是**刻意的**：`_image_links` 是「商品图集」的展示口径
    （主图 + 详情图合并去重，列表/详情弹窗用它）；本函数只取 `images`，
    是「生成详情图时可选参考图」的主图分组。
    """
    links: list[str] = []
    for row in item.get("images") or []:
        if isinstance(row, dict):
            link = _first_text(row.get("link"), row.get("url"))
            if link and link not in links:
                links.append(link)
    return links


def detail_image_links(item: dict[str, Any]) -> list[str]:
    """**原商品详情图**（`raw_json.image_descriptions`）—— 竖长图，与主图分开（v2.15）。

    真机实测（2026-10-02）：主图 10 张（方图，~800×800）、详情图 5 张（竖长图，
    如 2480×2824 / 1500×2322）。此前两者被混进 `_image_links`，
    详情图生成链路只用得上 `images[:1]`（主图）⇒ **原详情图从未被用作参考**。
    """
    links: list[str] = []
    for row in item.get("image_descriptions") or []:
        if isinstance(row, dict):
            link = _first_text(row.get("link"))
            if link and link not in links:
                links.append(link)
    return links


def status_key_from_raw(item: dict[str, Any]) -> str:
    """商品状态键（v1.32 补记，设计 13 §30.3.2）。

    ⚠️ **平台列表响应不含 `card_type`** —— 2026-09-22 对真机台账 1974 条
    raw_json 全量核对：0 条带回该键。而页面六个 tab 的全量筛选在实时链路里
    是靠查询条件表达的不是行字段 ⇒ 本地化后**按行的状态只能从生命周期硬信号
    推导**：

    * `item_freeze=true` → `frozen`（已冻结）；
    * 有 `off_shelf_time` **或**没有 `on_shelf_time` → `off_shelf`（已下架；
      真机 2 条下架行全部符合：`buyable=false` + 无上架时间 + 有下架时间）；
    * 其余 → `on_shelf`（售卖中）。

    **审核态四相**（`auditing` / `audit_rejected` / `audit_passed` / `rectify`）
    **无法从列表字段可靠区分**（`item_audit_status` 等在真机数据上恒为定值）
    ⇒ 返回 `""`，**不猜**（硬约束⑤）：这些商品只出现在「全部」里，状态展示 `-`，
    按审核态筛选得到空集（诚实边界，设计 13 §30.6）。

    平台哪天真在响应里给了 `card_type`，以平台值为准（见
    `shop_goods_sync_service._list_columns_from_raw`）—— 本函数只是兜底推导。
    """
    if not isinstance(item, dict):
        return ""
    if item.get("item_freeze"):
        return "frozen"
    if item.get("deleted"):
        # 已删除的商品不在六个 tab 的语义内，不归族（对账链路会打平台缺失标记）。
        return ""
    if item.get("off_shelf_time") or not item.get("on_shelf_time"):
        return "off_shelf"
    return "on_shelf"


def status_label_from_raw(item: dict[str, Any]) -> str:
    """按行的状态标签：优先平台 `card_type` 反查，否则按生命周期推导（§30.3.2）。

    与同步落库的 `status` 列**同源**（都走 `status_key_from_raw`）。
    「全部」查询下每行各自标真实状态，不再把整页都标成「全部」（那是查询
    条件不是行状态）。取不到 ⇒ 空串，**不猜**（硬约束⑤）。
    """
    from apis.xhs_qianfan_apis import QIANFAN_ITEM_STATUS_LABELS, QIANFAN_ITEM_STATUSES

    card_type = _int_or_none(item.get("card_type"))
    if card_type is not None:
        for key, value in QIANFAN_ITEM_STATUSES.items():
            if value == card_type:
                return QIANFAN_ITEM_STATUS_LABELS.get(key, "")
        return ""
    return QIANFAN_ITEM_STATUS_LABELS.get(status_key_from_raw(item), "")


def normalize_item_from_raw(item: dict[str, Any]) -> dict[str, Any]:
    """`raw_json`（千帆商品条目原文）→ 前端契约（**白名单构造**，不整体透传）。

    与改造前实时链路的 `_normalize_item` **逐字段一致**（v1.32 的唯一变化：
    `status_label` 按行反查，见 `_status_label_from_raw`）。
    """
    if not isinstance(item, dict):
        item = {}
    images = _image_links(item)
    sale_qty = item.get("sale_qty_info") if isinstance(item.get("sale_qty_info"), dict) else {}
    risk = item.get("inventory_risk_info") if isinstance(item.get("inventory_risk_info"), dict) else {}
    note_ids = [str(x) for x in (item.get("product_note_ids") or []) if x]
    return {
        "item_id": _first_text(item.get("item_id")),
        "title": _first_text(item.get("item_name"), item.get("item_name_with_brand_name")),
        "article_no": _first_text(item.get("article_no")),
        "image": images[0] if images else "",
        "images": images[:9],
        "image_count": len(images),
        # 单位统一为**分**（平台原样），展示层再除以 100
        "price_min_fen": _int_or_none(item.get("min_price")),
        "price_max_fen": _int_or_none(item.get("max_price")),
        "stock": _int_or_none(item.get("total_stock")),
        "sku_count": _int_or_none(item.get("sku_count")),
        "on_sale_sku_count": _int_or_none(item.get("on_sale_sku_count")),
        "sales_total": _int_or_none(sale_qty.get("acc_sale_qty")),
        "sales_30d": _int_or_none(sale_qty.get("sale_qty30")),
        #: 平台口径的挂链笔记数（原文 `product_note_num`）—— **只作对照**，
        #: 不是列表展示的 `note_count`（那个走关系表，见 `_relation_note_counts`，
        #: 设计 13 §32.4）。本地生成的笔记在平台侧还不存在，平台这个数不会动。
        "platform_note_count": _int_or_none(item.get("product_note_num")) or 0,
        "note_ids": note_ids,
        "category": _category_path(item.get("categories")),
        "status_label": status_label_from_raw(item),
        #: 状态筛选键（**权威实现只有一处**：`status_key_from_raw`）。上下架门禁按它判
        #: 「这个商品现在能不能上/下」。⚠️ 不在这里重算一遍 —— 两份口径必然漂移。
        "status_key": status_key_from_raw(item),
        "buyable": bool(item.get("buyable")),
        "frozen": bool(item.get("item_freeze")),
        "deleted": bool(item.get("deleted")),
        "audit_status": _int_or_none(item.get("item_audit_status")),
        "check_status": _int_or_none(item.get("check_status")),
        "is_channel": bool(item.get("is_channel")),
        "stock_warning_sku_count": _int_or_none(risk.get("warning_sku_count")) or 0,
        "create_time": _ts_text(item.get("create_time")),
        "on_shelf_time": _ts_text(item.get("on_shelf_time")),
        "update_time": _ts_text(item.get("update_time")),
        "xsec_token": _first_text(item.get("xsec_token")),
        # ------------------------------------------------------------------ #
        # 上下架 / 删除的**门禁输入**（2026-10-05，需求 7/8）。
        # ⚠️ 这几个字段是 `shop_item_operate_gates` 的判据来源 —— 判据只在那里实现，
        #    这里只负责把原料按白名单搬出来（不整体透传 raw_json）。
        # ------------------------------------------------------------------ #
        #: 平台在售活动的标志（定金预售等）—— 拦截下架 / 删除。
        "is_item_attend_promotion": bool(item.get("is_item_attend_promotion")),
        #: 平台给出的「这件商品哪些操作被锁」列表（`lockField`/`lockDimension`/
        #: `lockDesc`）。只为三个动作服务，故裁到需要的键（不搬整条）。
        "item_lock_infos": [
            {
                "lockField": _first_text(row.get("lockField")),
                "lockDimension": _first_text(row.get("lockDimension")),
                "lockDesc": _first_text(row.get("lockDesc")),
            }
            for row in (item.get("item_lock_infos") or [])
            if isinstance(row, dict)
        ],
        #: 平台处罚结果（真机 1 = 正常）。上架时判违规。
        "item_punish_result": _int_or_none(item.get("item_punish_result")),
        #: ⚠️ `item_audit_status`（审核状态）**没有**作为独立字段下发 ——
        #: 已在上面 `audit_status` 里（同名字段的既有键）。缺值口径：取不到就是
        #: `None`，上架门禁按「不判违规」处理（不把「不知道」当「违规」）。
    }

# --------------------------------------------------------------------------- #
# ⚠️ 已退场的函数（设计 75 S5，2026-10-27）—— 列出来是为了让「下一次想找它们的人」
#    知道东西去哪了，而不是在 git 历史里翻：
#
#   query_items / item_detail        → rt.list_products / rt.get_product_detail
#   export_rows / _row_to_csv        → goods.py::export_shop_items（实时翻页）
#   delete_items                     → 端点 410（本地无台账可删）；平台删除走 /items/operate
#   edit_product / ProductEditError  → 端点 410（ai_title / ai_selling_point 列已随表退场）
#   load_product_dict_from_ledger    → 直接传实时归一化 dict（media_generation / worker）
#   recent_item_ids_for_account      → publish.py 内联的 rt.list_products(page_size=3)
#   _apply_filters / _apply_order    → rt.list_products 的参数（筛选下推到平台）
#   _relation_note_counts            → rt.list_goods_notes(item_id)
# --------------------------------------------------------------------------- #
