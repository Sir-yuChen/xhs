"""本地商品台账查询（设计 13 §30）：`/xhs/qianfan/items` 与 `/item-detail` 的**唯一实现**。

## 这个模块为什么存在

v1.32 起商品列表**走本地数据**（设计 13 §30.3）：`POST /xhs/qianfan/items`
从「实时打千帆取一页」改为「查 `shop_products` 台账」。改造前 `/items` 的
归一化函数（`_normalize_item`）住在 `qianfan.py`，与平台响应打交道；现在
它的输入是**本地行的 `raw_json`**（同步时整条留存），职责变了，家也搬到
这个服务里 —— 展示契约逐字段不变，数据来源从「平台」换成「台账」。

## 三条纪律

* **零平台请求**：本模块**不许**碰适配器 / 发 HTTP。筛选、排序、分页、详情
  全部本地 SQL + JSON 解析。风控面归零是本次改造的目的之一（设计 12 §25.6
  「反查不做实时平台查询」同纪律）。
* **展示字段从 `raw_json` 现场派生**：`normalize_item_from_raw` 是唯一的
  契约构造函数（与改造前同构）。筛选 / 排序才用列（`shop_products` 九列，
  迁移 `t3u4v5w6x7y8`）—— WHERE 认列不认 JSON，两套判据就是两份实现
  （DEF-044 同型）。
* **缺值不是 0**：平台没返回的字段派生后仍空 ⇒ 上层渲染 `-`（硬约束⑤）；
  本模块不造 0、不猜。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from backend.app.models.platform_account import PlatformAccount
from backend.app.models.shop import NoteGoods
from backend.app.models.shop_product import ShopProduct

#: 排序字段 → 列（设计 13 §30.3.2）。未知字段由调用方的门禁挡掉（400），
#: 这里只做「已知 → 列」的映射，默认创建时间倒序。
#: v1.32 追加（用户口径「排序字段和方向在表头实现」）：价格 / 库存也可排 ——
#: 本地台账不受平台 sort_field 白名单约束，列即排序依据。
_SORT_COLUMNS = {
    "create_time": ShopProduct.create_time_ms,
    "on_shelf_time": ShopProduct.on_shelf_time_ms,
    "off_shelf_time": ShopProduct.off_shelf_time_ms,
    "last_audit_time": ShopProduct.last_audit_time_ms,
    "item_sale_qty_30": ShopProduct.sales_30d,
    "price_min_fen": ShopProduct.price_min_fen,
    "stock": ShopProduct.stock,
}


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
    }


def _relation_note_counts(db: Session, item_ids: list[str]) -> dict[str, int]:
    """**平台商品 ID** → 挂链笔记篇数（**关系表口径**，设计 13 §32.4）。

    为什么不用平台原文 `product_note_num`：本地生成的笔记在平台侧**还不存在**，
    平台那个数不会因为「刚生成了一篇」而变化 —— 用户点完生成却看到列上数字不动。
    关系表（`note_goods`，§26.7）是关系的唯一真相源，挂链篇数由它回答。

    关系表只覆盖「已同步 + 本地生成」的笔记 ⇒ 本地数 ≤ 平台数是常态，
    两个口径都回传（`note_count` / `platform_note_count`），不混成一个数字。
    当前页一次批量查询（`IN` + `GROUP BY`），零平台请求。

    ⚠️ 2026-09-27（§26.7）：关系表键从**本地商品主键**改成**平台 `item_id`**
    ⇒ 入参与返回都用 `item_id`（字符串），**台账缺行也有数**（关系不校验商品存在）。
    """
    ids = [str(item_id).strip() for item_id in item_ids if str(item_id or "").strip()]
    ids = list(dict.fromkeys(ids))
    if not ids:
        return {}
    rows = db.execute(
        select(NoteGoods.item_id, func.count())
        .where(NoteGoods.item_id.in_(ids))
        .group_by(NoteGoods.item_id)
    ).all()
    return {str(item_id): int(count) for item_id, count in rows}


def delete_items(db: Session, *, platform_account_id: int, item_ids: list[str]) -> int:
    """批量删除**本地台账行**（v1.36，用户口径：「商品列表新增批量删除操作」）。

    ## 删的是什么（口径，必须写清）

    只删 `shop_products` 里**这个账号**的选中行 —— **不打千帆**（零平台请求，本模块
    总纪律）。平台侧的商品**不动**：千帆 SDK 里根本没有商品删除接口，删平台商品
    要另立一条抓包写链路，那是另一件事（设计 13 §33.10 不做边界已记）。

    ⚠️ ADR-021：归属键是 `platform_account_id`（不再有店铺实体）。

    ## 两个连带后果（不藏）

    1. **平台还有的商品会回来**：下次「同步商品」把这些 item_id 重新 upsert 进台账
       ⇒ 删除是「清理本地台账」，不是「永久忽略」。要做到删了不回来，需要一张忽略
       名单 + 同步侧尊重它（本轮明确不做）。
    2. **`note_goods` 关系行一并删**（⚠️ 2026-09-23 用户口径「删除商品时也需要删除
       笔记与商品的关联关系」，**取代** v1.36 的「关系行不动」）：先取到将删行的
       本地 `id`，再按 `product_id` 删关系行，最后删商品行。
       ⚠️ **2026-09-27（§26.7）简化**：关系表改存平台 ID ⇒ 直接按 `item_id` 删，
       「先取本地 `id` / 顺序不能反」这条纪律随 `product_id` 一起消失。

    ⚠️ **为什么推翻 v1.36 的决定**：当时的理由是「商品被同步写回时关系原样接上」。
    用户明确要求改，理由也站得住 —— 关系指向一条已删商品就是**孤儿行**，与
    §26.6 修掉的「删笔记留孤儿关系」是**同一个病的另一个方向**。两处现在口径一致：
    **本地实体真删 ⇒ 关系行跟着走**。
    ⚠️ 顺序不能反：必须先拿 `id` 再删商品，否则关系行的 `product_id` 就再也对不上了。

    ⚠️ 与 `shop_goods_sync_service._drop_stale_rows` **刻意不同**：那个由
    「完整同步成功后平台已不存在的商品」触发，删的是**平台侧已消失**的行；
    本函数由**用户显式点击**触发。用户口径只针对显式删除，同步侧的连带删除
    **本轮不动**（要改需另议，见设计 13 §33.12 的待办）。

    返回**实际删除行数** —— 调用方拿它和请求条数对照（传了别人的 / 不存在的
    item_id 就会比请求数少），不假定「请求几条就删了几条」。
    """
    ids = [str(item_id).strip() for item_id in item_ids if str(item_id or "").strip()]
    if not ids:
        return 0

    # 1) 这个账号的台账里实际存在的选中行（传了别人的 / 不存在的 item_id ⇒ 少删几行）
    doomed = list(
        db.scalars(
            select(ShopProduct.id).where(
                ShopProduct.platform_account_id == platform_account_id,
                ShopProduct.item_id.in_(ids),
            )
        )
    )
    if not doomed:
        return 0

    # 2) 删指向这些商品的关系行（§26.7：本地实体真删 ⇒ 关系跟着走；
    #    关系表按平台 `item_id` 挂 ⇒ 直接按它删，不再需要先取本地 `id`）。
    #    `user_id` 一并收口：`item_id` 是平台 ID，跨用户同名理论上可能（DEF-066 同款纪律）。
    #    ADR-021：租户归属直接问账号（店铺实体已退场，不再经 `shops.user_id`）。
    owner_id = db.scalar(
        select(PlatformAccount.user_id).where(PlatformAccount.id == platform_account_id)
    )
    relation_delete = delete(NoteGoods).where(NoteGoods.item_id.in_(ids))
    if owner_id is not None:
        relation_delete = relation_delete.where(NoteGoods.user_id == int(owner_id))
    db.execute(relation_delete)

    # 3) 删商品行本体
    result = db.execute(
        delete(ShopProduct).where(
            ShopProduct.platform_account_id == platform_account_id,
            ShopProduct.item_id.in_(ids),
        )
    )
    db.commit()
    return int(result.rowcount or 0)


def _parse_raw(row: ShopProduct) -> dict[str, Any]:
    """行的 `raw_json` → dict；坏 JSON / 空 → `{}`（上层按缺值渲染）。"""
    try:
        parsed = json.loads(row.raw_json or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_product_dict_from_ledger(row: ShopProduct) -> dict[str, Any]:
    """台账行 → 商品字典（**只读本地，零平台请求**）。

    为什么需要它：`normalize_item_from_raw` 的入参是**千帆条目原文**，而单篇 /
    批量生成链路拿到的是 `ShopProduct` 行。没有这个函数时，调用方就各自手写
    `{"item_id": row.item_id, "sales_total": row.sales, ...}` 的字段映射 ——
    键名一旦与 `PRODUCT_FIELDS` 漂移（如把 `sales` 写成 `sales`/`sales_total`
    两套），`product_payload()` 会**静默丢字段**（缺值即不提取），
    生成结果悄悄少信息且不报错。⇒ 统一在这里构造，全项目只有这一份（DEF-044）。

    **只用于本地渲染 / 预览**：生成链路要求平台**当下**数据（07 §10），
    那条路径走 `normalize_item_from_raw`（平台响应），不走本函数。

    优先取 `raw_json`（与列表页展示契约逐字段一致）；`raw_json` 缺失或坏掉时
    退回归一化列，保证预览不会因为一条坏行而整页报错（DEF-048：安全网不条件开放）。
    """
    raw = _parse_raw(row)
    if raw:
        return normalize_item_from_raw(raw)
    return {
        "item_id": row.item_id or "",
        "title": row.item_title or "",
        "article_no": row.article_no or "",
        "price_min_fen": row.price_min_fen,
        "price_max_fen": row.price_max_fen,
        "stock": row.stock,
        "sales_total": row.sales,
        "category": row.category or "",
        # 归一化列里没有这两个（`on_sale_sku_count` 只在 `raw_json` 里）；
        # 缺值留 None，**不造 0**（硬约束⑤）。
        "sku_count": row.sku_count,
        "on_sale_sku_count": None,
    }


def _split_keywords(keyword: str) -> list[str]:
    """关键词入参 → 词表（逗号分隔，中英文逗号都认；平台 `whole_keywords` 同语义）。"""
    text = (keyword or "").replace("，", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def _apply_filters(
    stmt,
    payload: Any,
    *,
    from_ms: Optional[int],
    to_ms: Optional[int],
    price_from_fen: Optional[int],
    price_to_fen: Optional[int],
):
    """把筛选条件应用到语句上（设计 13 §30.3.2 的本地口径）。

    ⚠️ **空值行不命中**：价格 / 库存 / 销量 / 创建时间取不到的行**无法确认**
    是否在区间内 ⇒ 不代入（硬约束⑤：把「读不到」当「不在区间」是造假，
    当「在区间」也是造假，唯一诚实的做法是筛选时不代入、展示时渲染 `-`）。

    价格入参是**分**（元→分的换算只发生在路由层 `_price_fen()`，与改造前
    同一处 —— 两头各乘一次 100 是这类集成最典型的静默错值）。

    ⚠️ 唯一不属于「空值行不命中」的是**挂链笔记数**（v1.34）：它数的是本地关系表
    （`note_goods`），「没有关系行」是**确凿的 0 篇**（不是「读不到」）⇒ 照常代入比较，
    并且与列表「挂链笔记」列显示的**是同一个数**。
    """
    terms = _split_keywords(getattr(payload, "keyword", "") or "")
    if terms:
        clauses = []
        for term in terms:
            like = f"%{term}%"
            clauses.extend(
                [
                    ShopProduct.item_title.like(like),
                    ShopProduct.item_id.like(like),
                    ShopProduct.article_no.like(like),
                ]
            )
        stmt = stmt.where(or_(*clauses))

    status = (getattr(payload, "status", "") or "all").strip()
    if status and status != "all":
        from apis.xhs_qianfan_apis import QIANFAN_ITEM_STATUSES

        card_type = QIANFAN_ITEM_STATUSES.get(status)
        if card_type is not None:
            stmt = stmt.where(ShopProduct.card_type == card_type)

    if price_from_fen is not None:
        stmt = stmt.where(
            ShopProduct.price_max_fen.is_not(None), ShopProduct.price_max_fen >= int(price_from_fen)
        )
    if price_to_fen is not None:
        stmt = stmt.where(
            ShopProduct.price_min_fen.is_not(None), ShopProduct.price_min_fen <= int(price_to_fen)
        )

    stock_from = getattr(payload, "stock_from", None)
    stock_to = getattr(payload, "stock_to", None)
    if stock_from is not None:
        stmt = stmt.where(ShopProduct.stock.is_not(None), ShopProduct.stock >= int(stock_from))
    if stock_to is not None:
        stmt = stmt.where(ShopProduct.stock.is_not(None), ShopProduct.stock <= int(stock_to))

    sales_from = getattr(payload, "sales_from", None)
    sales_to = getattr(payload, "sales_to", None)
    if sales_from is not None:
        stmt = stmt.where(ShopProduct.sales.is_not(None), ShopProduct.sales >= int(sales_from))
    if sales_to is not None:
        stmt = stmt.where(ShopProduct.sales.is_not(None), ShopProduct.sales <= int(sales_to))

    if from_ms is not None:
        stmt = stmt.where(
            ShopProduct.create_time_ms.is_not(None), ShopProduct.create_time_ms >= int(from_ms)
        )
    if to_ms is not None:
        stmt = stmt.where(
            ShopProduct.create_time_ms.is_not(None), ShopProduct.create_time_ms <= int(to_ms)
        )

    # 挂链笔记数（v1.34，用户口径「商品列表搜索条件添加挂链笔记数，默认小于指定篇数」）：
    # 口径 = `note_goods` **关系表** —— 必须与列表「挂链笔记」列显示的是**同一个数**
    # （§33.8 / §32.4）；若按平台原文 `product_note_num` 筛，会出现「列上写 1 篇、
    # 却按 5 篇筛」的自相矛盾界面。`note_count` 为空 ⇒ 不筛（不是「小于 0」）。
    #
    # 用**相关子查询**而不是 `LEFT JOIN + GROUP BY`：后者要重写整个 stmt，而 total 还包了
    # 一层 `.subquery()`，改 JOIN 会连带影响分页总数；这里只是多一个 WHERE。
    note_count = getattr(payload, "note_count", None)
    if note_count is not None:
        # ⚠️ 2026-09-27（§26.7）：关系表按**平台 ID** 挂 ⇒ 按 `item_id` 关联
        # （不再是 `product_id = ShopProduct.id`）。
        relation_count = (
            select(func.count())
            .select_from(NoteGoods)
            .where(NoteGoods.item_id == ShopProduct.item_id)
            .scalar_subquery()
        )
        bound = int(note_count)
        # 未知比较符由路由层白名单挡成 400（不在这里猜一个默认值出来）。
        op = (getattr(payload, "note_count_op", "") or "lt").strip().lower()
        if op == "lt":
            stmt = stmt.where(relation_count < bound)
        elif op == "lte":
            stmt = stmt.where(relation_count <= bound)
        elif op == "eq":
            stmt = stmt.where(relation_count == bound)
        elif op == "gte":
            stmt = stmt.where(relation_count >= bound)
        elif op == "gt":
            stmt = stmt.where(relation_count > bound)
    return stmt


def _resolve_order(sort_field: str, order: str) -> tuple[str, str, Any]:
    """排序入参 → `(生效字段, 生效方向, 列)`。**回退规则只此一处**。

    未知字段回退 `create_time`、非 `asc` 一律 `desc` —— 与 `_apply_order` 同源，
    响应回传给前端的也就是这里算出来的值（设计 13 §32.1：排序口径由后端独家
    回传，前端不再自持一份「我以为排的是什么」）。
    """
    field = sort_field if sort_field in _SORT_COLUMNS else "create_time"
    direction = "asc" if order == "asc" else "desc"
    return field, direction, _SORT_COLUMNS[field]


def _apply_order(stmt, *, sort_field: str, order: str):
    """排序（设计 13 §30.3.2）：七个字段映射到列；**NULL 排后**（两个方向都是）。

    `(col IS NULL) ASC` 作第一键：非空（False=0）在前、空（True=1）在后，
    方向只作用于值本身。第二键 `id ASC` 保证同值行顺序确定（分页不跳行）。
    """
    _, direction, column = _resolve_order(sort_field, order)
    ordered = column.asc() if direction == "asc" else column.desc()
    return stmt.order_by(column.is_(None).asc(), ordered, ShopProduct.id.asc())


def query_items(
    db: Session,
    account: PlatformAccount,
    payload: Any,
    *,
    platform_account_id: int,
    from_ms: Optional[int],
    to_ms: Optional[int],
    price_from_fen: Optional[int] = None,
    price_to_fen: Optional[int] = None,
) -> dict[str, Any]:
    """本地商品列表一页（`POST /xhs/qianfan/items` 的执行体，**零平台请求**）。

    ⚠️ **零平台请求 ⇒ 零门禁**（用户 2026-09-30 口径）：本页读的是**本地台账**
    `shop_products`，**不需要千帆登录态就能看**。登录态只决定「能不能从平台同步
    新数据」，不决定「本地已有数据能不能读」—— 无登录态就拦下整页 = 把已落库的
    数据锁死，是错的。

    入参 `payload` 是路由层已通过 Pydantic 校验的 `ShopItemsRequest`；
    `from_ms` / `to_ms` 是路由层解析好的创建时间窗口（毫秒，结束日含当天）；
    `price_*_fen` 是路由层 `_price_fen()` 换算好的价格边界（元→分只在那一处）。
    """
    stmt = select(ShopProduct).where(ShopProduct.platform_account_id == platform_account_id)
    stmt = _apply_filters(
        stmt,
        payload,
        from_ms=from_ms,
        to_ms=to_ms,
        price_from_fen=price_from_fen,
        price_to_fen=price_to_fen,
    )
    stmt = _apply_order(
        stmt,
        sort_field=(getattr(payload, "sort_field", "") or "create_time"),
        order=(getattr(payload, "order", "") or "desc"),
    )
    #: 实际生效的排序口径（未知字段的回退也在这里体现）—— 随响应回传，前端只显示它。
    effective_sort_field, effective_order, _ = _resolve_order(
        getattr(payload, "sort_field", "") or "create_time",
        getattr(payload, "order", "") or "desc",
    )

    page_no = max(1, int(getattr(payload, "page_no", 1) or 1))
    page_size = max(1, min(int(getattr(payload, "page_size", 20) or 20), 100))
    total = int(
        db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    )
    rows = db.scalars(stmt.limit(page_size).offset((page_no - 1) * page_size)).all()

    items = [normalize_item_from_raw(_parse_raw(row)) for row in rows]
    # 挂链笔记数：**关系表口径**覆盖掉平台原值（设计 13 §32.4）；
    # 平台值留在 `platform_note_count` 里做对照。
    counts = _relation_note_counts(db, [str(row.item_id or "") for row in rows])
    for item, row in zip(items, rows):
        item["note_count"] = counts.get(str(row.item_id or ""), 0)
    #: 台账同步时间 = 本账号 max(synced_at)（**没有行 → 空串**，上层渲染 `-`）。
    latest_sync = db.scalar(
        select(func.max(ShopProduct.synced_at)).where(
            ShopProduct.platform_account_id == platform_account_id
        )
    )
    return {
        "total": total,
        "count": len(items),
        "page_no": page_no,
        "page_size": page_size,
        "has_more": page_no * page_size < total,
        "sort_field": effective_sort_field,
        "order": effective_order,
        "items": items,
        "queried_at": latest_sync.strftime("%Y-%m-%d %H:%M:%S") if latest_sync else "",
    }


def item_detail(
    db: Session, *, platform_account_id: int, item_id: str
) -> Optional[dict[str, Any]]:
    """单个商品的本地详情（`POST /xhs/qianfan/item-detail` 的执行体）。

    ⚠️ 同 `query_items`：**读本地台账，零平台请求 ⇒ 无门禁**（用户 2026-09-30 口径）。

    返回 `None` = 本账号的台账没有这个商品（路由层转 404）。SKU 明细读
    `detail_json`（同步任务的详情补齐落库，设计 13 §19.4.2）：**空串 =
    没补过** ⇒ `skus` 回 `[]` 且 `detail_synced_at` 为空串，由上层渲染
    空态 + 说明，**不渲染成 0 行**（缺值口径）。
    """
    row = db.scalar(
        select(ShopProduct).where(
            ShopProduct.platform_account_id == platform_account_id,
            ShopProduct.item_id == item_id,
        )
    )
    if row is None:
        return None

    skus: list[dict[str, Any]] = []
    detail_raw = (row.detail_json or "").strip()
    if detail_raw:
        try:
            parsed = json.loads(detail_raw)
            if isinstance(parsed, list):
                skus = [entry for entry in parsed if isinstance(entry, dict)]
        except (TypeError, ValueError):
            skus = []

    images: list[str] = []
    try:
        parsed_images = json.loads(row.images_json or "[]")
        if isinstance(parsed_images, list):
            images = [str(x) for x in parsed_images if x]
    except (TypeError, ValueError):
        images = []

    item = normalize_item_from_raw(_parse_raw(row))
    # 挂链笔记数按关系表口径（与列表同一份实现，设计 13 §32.4）。
    item["note_count"] = _relation_note_counts(db, [str(row.item_id or "")]).get(
        str(row.item_id or ""), 0
    )

    # v2.15（§13.3.6）：主图 / 详情图**分开下发**，供「生成详情图」弹窗勾选参考图。
    raw_item = _parse_raw(row)

    return {
        "item": item,
        "images": images,
        #: 商品主图（`raw_json.images`）—— 生成详情图时可选参考图分组 ①。
        "product_images": product_image_links(raw_item),
        #: 原商品详情图（`raw_json.image_descriptions`）—— 参考图分组 ②（v2.15 新增）。
        "detail_images": detail_image_links(raw_item),
        "skus": skus,
        "sku_count": row.sku_count,
        #: 空串 = 没补过详情（与「补过但 0 SKU」是两件事）。
        "detail_synced_at": (
            row.detail_synced_at.strftime("%Y-%m-%d %H:%M") if row.detail_synced_at else ""
        ),
        "synced_at": row.synced_at.strftime("%Y-%m-%d %H:%M") if row.synced_at else "",
        #: 对账标记（设计 13 §27）：非空 = 最近一次同步发现「本地有、平台无」。
        "platform_missing_at": (
            row.platform_missing_at.strftime("%Y-%m-%d %H:%M") if row.platform_missing_at else ""
        ),
    }
