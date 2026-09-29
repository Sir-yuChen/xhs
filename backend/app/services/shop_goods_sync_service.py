"""店铺商品同步：Creator 账号 → 千帆商品列表 → 本地 ``shop_products`` 表（需求 R2）。

## 与 `/xhs/qianfan/items` 的分工

那个端点**v1.32 起已改本地**（设计 13 §30.3.2）：不再打千帆，改查本表的台账
（筛选 / 排序 / 分页全在本地 SQL 做）。本模块是它**唯一的数据来源** —— 列表翻页
落库 + SKU 详情**内联补齐** + 字段补足回填。

## 两步合一（2026-09-23 重构）

旧设计是「列表翻页 → 再起一个多轮队列补详情」，全店 1980 个商品每轮只补 100 个、
需 20 轮才能覆盖完——用户看到规格详情为空是常态。**现在每页列表里落在本次同步
范围内的商品，落库前立即拉取其 SKU 详情**，列表翻完 = 详情也全了，不再有单独的
补齐队列。

⚠️ **详情补齐严格限定在本次同步范围内**（2026-09-23 用户口径：「增量同步只补条件
区间内的商品信息和详情」）：平台圈回来的行先经本地窗口 / 游标过滤（`new_items`），
**范围外的商品既不拉详情（省风控配额）也不落库**（不把范围外的行标成「本次同步过」）。

## 三条纪律

* **完整同步才删旧行**：只有「翻完最后一页」（`has_more=false`）或「已存够 `total`」
  才删除本次未出现的 `shop_products` 行。半途而废就删 = 把没扫到的商品当已下架丢掉。
* **解不出店铺就不同步**：账号没有 `shop_id_fk`（未绑店）→ 任务 `failed` 并写明原因，
  **不编一个店铺出来**（缺值口径）。
* **凭据失效不清数据**：`-13000` → 账号的千帆状态标 `invalid` + 平台原文，
  **已有商品一行不删**（登录态过期不等于商品下架）。

## 风控

与笔记同步同一套：翻页由 `CrawlRateLimiter.wait_allow()` 的**速率**保证（§21：翻到
平台没有下一页为止，超限**等待**而非停止）；`ABSOLUTE_PAGE_CAP` 仅防死循环。
详情拉取与列表翻页共用同一个限流键（`{account.id}:goods`），自然串行节流。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models.platform_account import PlatformAccount
from backend.app.models.shop_product import ShopProduct
from backend.app.models.task import Task
from backend.app.services import sync_task_service as sts
from backend.app.services import task_cancel
from backend.app.services.rate_limiter import get_rate_limiter

logger = logging.getLogger(__name__)

#: 页与页之间的停顿**基础值**（秒）。测试置 0。
#: 2026-09-25 与笔记同步同口径（用户「拉长间隔时间，随机化间隔时间」）：3.0 → 5.0，
#: 实际停顿再乘抖动系数（`sync_pacing`）。
PAGE_GAP_SECONDS = 5.0
MAX_PAGE_GAP_SECONDS = 60.0

#: 「字段补足」每轮回填的行数上限（设计 13 §30.3.4）。**本地操作、零平台请求**，
#: 上限只是为了避免单次事务过长；多轮自然收敛。
LIST_BACKFILL_CAP = 1000


class SyncFailure(Exception):
    """本次同步失败（原因可展示给用户，不含 Cookie 等敏感值）。"""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# --------------------------------------------------------------------------- #
# 归一化：千帆条目 → shop_products 行
# --------------------------------------------------------------------------- #
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


def _price_fen_text(item: dict[str, Any]) -> str:
    """价格 → 字符串（**单位是分**，列名即口径；换算成元是展示层的事）。

    同价给 `1999`，区间给 `1999~2999`，取不到给空串（不造 0）。
    """
    low = _int_or_none(item.get("min_price"))
    high = _int_or_none(item.get("max_price"))
    if low is None and high is None:
        return ""
    if low is None or high is None:
        return str(low if low is not None else high)
    return str(low) if low == high else f"{low}~{high}"


def _category_path(categories: Any) -> str:
    """`categories[]` → 「一级 / 二级 / 三级」（按 `level` 升序，去空段）。"""
    if not isinstance(categories, list):
        return ""
    rows = [row for row in categories if isinstance(row, dict)]
    rows.sort(key=lambda row: row.get("level") or 0)
    names = [_first_text(row.get("name")) for row in rows]
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


def _status_label(item: dict[str, Any]) -> str:
    """条目自己的状态标签（与读取层 `status_label_from_raw` **同一实现**）。

    `status="all"` 同步时**不能**用查询条件当每行的状态（那会把全部商品都标成
    「全部」）。平台响应实测不含 `card_type` ⇒ 按生命周期信号推导（规则见
    `shop_products_service.status_key_from_raw`）；平台哪天真给了 `card_type`
    以平台值为准。取不到就留空 —— 不猜（硬约束⑤）。原始值仍在 `raw_json` 里。
    """
    from backend.app.services.shop_products_service import status_label_from_raw

    return status_label_from_raw(item)


def _list_columns_from_raw(item: dict[str, Any]) -> dict[str, Any]:
    """千帆商品条目 → **筛选 / 排序专用九列**（设计 13 §30.3.3）。

    与 `normalize_product` 共用这一个派生函数（字段补足 pass 也走它）——
    同一语义只许一份实现（DEF-044）。取不到给 `None` / `""`，**不造 0**
    （硬约束⑤）：平台没返回的字段派生后仍空，由展示层渲染 `-`。

    注意 `sales_30d` 与 `sales`（累计）是**两个口径**，不互推；
    五个时间列存平台原文的**毫秒**，不换算（换算成展示文本是读取层的事）。

    `card_type`：平台响应**实测不含该键**（真机 1974 条 0 命中，v1.32 补记）⇒
    缺省时按生命周期信号推导（`shop_products_service.status_key_from_raw`，
    规则与展示层 `status_label` 同源）—— 列的含义是「状态筛选键」，不是
    「平台原值」；平台哪天真给了，以平台值为准。
    `last_audit_time_ms`：平台字段名是 **`item_audit_time`**（真机核对），
    兼容理论上可能出现的 `last_audit_time`。
    """
    from apis.xhs_qianfan_apis import QIANFAN_ITEM_STATUSES

    from backend.app.services.shop_products_service import status_key_from_raw

    sale_qty = item.get("sale_qty_info") if isinstance(item.get("sale_qty_info"), dict) else {}
    card_type = _int_or_none(item.get("card_type"))
    if card_type is None:
        card_type = QIANFAN_ITEM_STATUSES.get(status_key_from_raw(item))
    return {
        "article_no": _first_text(item.get("article_no")),
        "card_type": card_type,
        "price_min_fen": _int_or_none(item.get("min_price")),
        "price_max_fen": _int_or_none(item.get("max_price")),
        "create_time_ms": _int_or_none(item.get("create_time")),
        "on_shelf_time_ms": _int_or_none(item.get("on_shelf_time")),
        "off_shelf_time_ms": _int_or_none(item.get("off_shelf_time")),
        "last_audit_time_ms": _int_or_none(
            item.get("item_audit_time") or item.get("last_audit_time")
        ),
        "sales_30d": _int_or_none(sale_qty.get("sale_qty30")),
    }


def normalize_product(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    """千帆商品条目 → `shop_products` 的列（写死的白名单，不整体透传）。"""
    item_id = _first_text(item.get("item_id"))
    if not item_id:
        # 没有主键就没有行的身份 —— 跳过，不上报为「成功」。
        return None
    sale_qty = item.get("sale_qty_info") if isinstance(item.get("sale_qty_info"), dict) else {}
    return {
        "item_id": item_id,
        "item_title": _first_text(
            item.get("item_name"), item.get("item_name_with_brand_name")
        ),
        "price": _price_fen_text(item),
        "stock": _int_or_none(item.get("total_stock")),
        "sales": _int_or_none(sale_qty.get("acc_sale_qty")),
        "category": _category_path(item.get("categories")),
        "images_json": json.dumps(_image_links(item), ensure_ascii=False),
        "status": _status_label(item),
        "raw_json": json.dumps(item, ensure_ascii=False),
        # v1.32（设计 13 §30.3.3）：筛选 / 排序九列。展示字段不落列 ——
        # 读取时从 raw_json 现场派生（契约与现网 /items 逐字段一致）。
        **_list_columns_from_raw(item),
    }


def backfill_list_columns(
    db: Session, shop_id_fk: int, *, cap: int = LIST_BACKFILL_CAP
) -> int:
    """**字段补足**（设计 13 §30.3.4）：raw_json 有值但筛选列为空的存量行
    → 从 raw_json **本地派生**回填。**零平台请求**，返回实际发生变更的行数。

    候选判据（SQL 预筛，宁窄勿宽）：`create_time_ms IS NULL`（老行指纹 ——
    迁移前落库的行九列全空）或 `article_no` / `card_type` / `price_min_fen`
    为空（平台当年没给的字段，派生后仍空则维持原状）。**不把
    `off_shelf_time_ms` 等「在售商品天然没有」的列写进候选** —— 否则候选集
    永远不空，每轮都白扫全店。

    平台本身没返回的字段派生后仍为空 ⇒ **留空**（硬约束⑤：不造 0、不猜），
    行保持候选身份，下一轮（或下一次全量同步刷新 raw_json 后）再试。
    """
    rows = db.scalars(
        select(ShopProduct)
        .where(
            ShopProduct.shop_id_fk == shop_id_fk,
            ShopProduct.raw_json != "",
            ShopProduct.raw_json != "{}",
            (
                ShopProduct.create_time_ms.is_(None)
                | (ShopProduct.article_no == "")
                | ShopProduct.card_type.is_(None)
                | ShopProduct.price_min_fen.is_(None)
            ),
        )
        .order_by(ShopProduct.id.asc())
        .limit(cap)
    ).all()
    changed = 0
    for row in rows:
        try:
            parsed = json.loads(row.raw_json or "{}")
        except (TypeError, ValueError):
            continue  # 坏 JSON 不猜，等下一次全量同步重写
        if not isinstance(parsed, dict):
            continue
        derived = _list_columns_from_raw(parsed)
        updates = {
            field: value
            for field, value in derived.items()
            if value not in (None, "") and getattr(row, field) in (None, "")
        }
        if not updates:
            continue
        for field, value in updates.items():
            setattr(row, field, value)
        changed += 1
    if changed:
        db.commit()
    return changed


def store_products(
    db: Session,
    user_id: int,
    shop_id_fk: int,
    items: list[dict[str, Any]],
    *,
    skus_map: dict[str, list[dict]] | None = None,
    detail_time: Any = None,
) -> list[str]:
    """把一页商品 upsert 进 `shop_products`（含 SKU 详情），返回本页**成功落库**的 item_id。

    2026-09-23 重构：维度降到商品级 —— 列表数据 + SKU 详情凑齐后一次性 upsert，
    不再分两步（先存列表、再补详情）。`skus_map` 为该页商品的 SKU 明细（按 item_id
    分组），`detail_time` 为详情拉取时间；缺值的商品（拉取失败 / 平台没返回）
    → `detail_json` 留空、`detail_synced_at` 留 NULL（下轮重试）。
    """
    if not items:
        return []
    existing = {
        row.item_id: row
        for row in db.scalars(
            select(ShopProduct).where(ShopProduct.shop_id_fk == shop_id_fk)
        ).all()
    }
    stored: list[str] = []
    now = shanghai_now()
    for item in items:
        normalized = normalize_product(item)
        if normalized is None:
            continue
        item_id = normalized["item_id"]
        # SKU 详情内联：有则归一化后写入，没有则留空（不造 0、不造假）
        skus = None
        if skus_map and item_id in skus_map:
            skus_raw = skus_map[item_id]
            if isinstance(skus_raw, list):
                skus = normalize_skus(skus_raw)
        if skus is not None:
            normalized["detail_json"] = json.dumps(
                skus, ensure_ascii=False, separators=(",", ":")
            )
            normalized["sku_count"] = len(skus) if skus else None
            normalized["detail_synced_at"] = detail_time or now
        row = existing.get(item_id)
        if row is None:
            row = ShopProduct(user_id=user_id, shop_id_fk=shop_id_fk, item_id=item_id)
            db.add(row)
            existing[item_id] = row
        for field, value in normalized.items():
            setattr(row, field, value)
        row.synced_at = now
        stored.append(item_id)
    return stored


# --------------------------------------------------------------------------- #
# 执行体
# --------------------------------------------------------------------------- #
def _resolve_shop_id(db: Session, account: PlatformAccount) -> int:
    """账号 → 店铺实体主键。解不出就抛（**不编店铺**）。

    2026-09-22 起「账号有没有店铺」只由 `platform_accounts.shop_id_fk` 回答
    （原来的绑定表已移除），守卫（软删闸 + 租户闸）在 `account_shop_id` 里。
    """
    from backend.app.services.shop_binding_service import account_shop_id

    shop_pk = account_shop_id(db, account.id)
    if shop_pk is None:
        raise SyncFailure("该账号尚未绑定千帆店铺，无法同步商品。", code="QIANFAN_BINDING_REQUIRED")
    return int(shop_pk)


def _sync_with_task(
    db: Session,
    task: Task,
    account: PlatformAccount,
    shop_id_fk: int,
    adapter_factory: Any,
    *,
    max_pages: Optional[int],
    page_size: int,
    mode: str = sts.SYNC_MODE_AUTO,
    time_from_ms: int = 0,
    time_to_ms: int = 0,
) -> dict[str, Any]:
    from backend.app.services.shop_binding_service import (
        qianfan_cookies_for_account,
        set_shop_state,
    )

    limiter = get_rate_limiter()
    # 千帆凭据只认**本账号自己的 ark 通道**（`qianfan_cookies_for_account`）：
    # 没有令牌就是可读的失败原因，**不借任何别的凭据**（2026-09-22 用户拍板）。
    cookies = qianfan_cookies_for_account(db, account.id)
    if not cookies:
        raise SyncFailure(
            "该账号缺少千帆（ark）令牌。",
            code="QIANFAN_CREDENTIAL_MISSING",
        )
    try:
        adapter = adapter_factory(cookies)
    except Exception as exc:  # noqa: BLE001 - 构造失败也要留下可读原因
        raise SyncFailure(f"千帆凭据无法解密：{type(exc).__name__}", code="BINDING_UNREADABLE")

    # §22：``auto``（默认）落成 full / incremental，判据是 ``goods_cursor_ms``。
    mode = sts.resolve_goods_sync_mode(db, account.id, mode)
    incremental = mode == sts.SYNC_MODE_INCREMENTAL
    # 增量游标（毫秒）。0 = 未同步过 → 走全量。见 §17.9 / D13。
    last_cursor_ms = sts.load_goods_cursor(db, account.id) if incremental else 0
    # §27 对账范围 = 本次同步范围（在游标被推进**之前**取）。
    scope_from_ms = max(last_cursor_ms, time_from_ms) if incremental else time_from_ms
    scope_to_ms = time_to_ms
    # ⚠️ **增量模式的对账上界要在本轮跑完后收敛**（2026-09-23）：
    # 增量翻到「本页全是旧行」就早停了（那是**追平**，不是**翻完**），根本没有
    # 枚举完游标之后的全部行。此时若拿本轮 `reconcile_ids` 判「平台没有」，会把
    # 那些**还没轮到枚举**的行误标成「三方不存在」（假阳性）。
    # 所以在增量下把上界收敛为「本轮真实枚举到的最大 create_time」——只对本轮
    # 确实扫过的区间下结论（DEF-057 同款纪律：样本不完整则结论不成立）。
    observed_raw_max_ms = 0
    cursor_advanced = False

    stored_ids: set[str] = set()
    # §27：本轮枚举到、且落在同步范围内的平台 item_id（对账用）。
    reconcile_ids: set[str] = set()
    total: Optional[int] = None
    warnings: list[str] = []
    stopped_by_limit = False
    complete = False
    # §27：枚举完整性（与 `complete` 是两件事 —— `complete` 还管「删旧行」的闸）。
    # 增量追平（本页无新行）算**范围内翻完**；限流 / 页数兜底 / 显式 max_pages /
    # 取数失败都算没翻完 ⇒ 本轮不对账（DEF-057：样本不完整则结论不成立）。
    page_cap_hit = False
    max_pages_hit = False
    page_failed = False
    page_no = 1
    # 本轮**新落库**的行数（增量模式只算比游标新的；全量模式 = 全部）。
    # stored_ids 仍收集全部 id，供 complete 时 _drop_stale_rows 用。
    new_count = 0
    # 内联详情补齐的累计计数（2026-09-23 重构：不再有多轮队列，这里只统计本轮）。
    detail_done = 0
    detail_failed = 0
    # 2026-09-28：**进度条口径** —— `processed` = 本任务累计已处理的商品数
    # （SKU 批次尝试过的，含失败），`work_total` = 本次任务的预计工作量
    # （首个页边界算一次，之后只增不减）。见 `_update_progress` / `sts.progress_percent`。
    processed = 0
    work_total: Optional[int] = None
    # 预先定义，避免「限流 break」路径上未赋值（DEF-045 同批引入的回归）。
    new_items: list[dict[str, Any]] = []

    while True:
        # 协作式中断检查点（设计 13 §24.8）：每翻一页前看一次取消信号，
        # 命中即停 —— 不再打平台、不再落库（半途成果保留，不删旧行）。
        if task_cancel.is_cancelled(task.id):
            break
        # 绝对兜底（设计 13 §21.2）：平台异常导致「本页非 0 行」恒真时必须能停下，
        # 否则「取消页数上限」= 死循环。
        if page_no > sts.ABSOLUTE_PAGE_CAP:
            warnings.append(f"已达绝对页数兜底（{sts.ABSOLUTE_PAGE_CAP} 页）")
            page_cap_hit = True
            break
        # 只有调用方**显式**要了小批量才停（调试用）；不传时 `max_pages is None`。
        if max_pages is not None and page_no > max_pages:
            warnings.append(f"已达指定页数上限（{max_pages} 页）")
            max_pages_hit = True
            break
        # 限流键带 `:goods`（§19.11）：笔记同步与商品同步是两个任务类型、可并发，
        # 都打 ark。共用一个账号键会互饿（同多源笔记的教训）。
        #
        # ⚠️ **等待**而不是 break（§21.3）：额度按账号读运行时配置（默认 60 次/分钟），页间只 sleep 3 秒 ⇒
        # 20 秒烧完额度；这里 break 等于把页数上限从 3 悄悄改成 5，问题原样存在。
        if not limiter.wait_allow(f"{account.id}:goods"):
            stopped_by_limit = True
            warnings.append(f"第 {page_no} 页前限流等待超时")
            break

        # §17.4 / §17.5.2：时间窗参数单位是**秒**，响应字段是毫秒，相差 1000 倍。
        # ⚠️ 边界（DEF-045，2026-09-20 真机实测）：不能用 `cursor // 1000`。
        # 平台按「秒」做 `>=` 比较，cursor 的毫秒部分会被抹掉，导致**同一秒内
        # 毫秒数更小**的旧行也被圈进窗口（实测 1784157704000 → from=1784157704
        # 圈回 1784157703000 的旧行）。必须 `+1` 跳过整个游标秒，只留严格更晚的行；
        # 代价是**同秒内更晚毫秒**的行会留到下一轮（下一轮会重扫该秒，不漏）。
        # §23：用户自选时间区间。起点取「游标」与「自选起始日」里**更晚**的那个 ——
        # 增量是「从游标往后」，区间是「夹在起止日之间」，两者是 and 不是 or。
        # ⚠️ 平台参数单位是**秒**且按 `>=` 比较（DEF-045）：毫秒下界必须 `//1000 + 1`。
        effective_from_ms = max(last_cursor_ms, time_from_ms)
        window_from = (effective_from_ms // 1000 + 1) if effective_from_ms > 0 else None
        success, message, raw_payload = adapter.search_items(
            page_no=page_no,
            page_size=page_size,
            status="all",  # 同步全部状态，不只售卖中
            sort_field="create_time",
            order="desc",
            create_time_from=window_from,
        )
        if not success:
            _mark_credential_state(db, account, message, raw_payload)
            if page_no == 1:
                # 首页失败 = 一条都没落 → 交给上层标 failed（已有数据一行不删）。
                raise SyncFailure(message or "商品列表获取失败")
            warnings.append(f"第 {page_no} 页获取失败")
            page_failed = True
            break

        data = raw_payload.get("data") if isinstance(raw_payload, dict) else None
        items = [
            item
            for item in ((data or {}).get("items") or [])
            if isinstance(item, dict)
        ]
        page_total = _int_or_none((data or {}).get("total"))
        if page_total is not None:
            total = page_total

        # ⚠️ **先按同步范围过滤，再拉详情 / 落库**（2026-09-23 用户口径：
        # 「增量同步只补条件区间内的商品信息和详情」）。平台的时间参数是**秒**且按
        # `>=` 比较，圈回来的行未必都在范围内（DEF-045）：
        #   * 下界：`create_time` ≤ 游标 的同秒旧行（`//1000+1` 也堵不住的边界；
        #     其它筛选条件也可能圈回旧行）⇒ 本地按毫秒游标再筛一次；
        #   * 上界：比结束日期还新的商品（平台没有 `create_time_to`，只能本地筛）。
        # **范围外的行既不拉详情也不落库** —— 拉详情要打平台（白耗风控配额），
        # 落库会把范围外的商品更新成「本次同步过」（与用户选的区间不符）。
        # 早停判据用的是**过滤后**的 `new_items`（见下），所以这里过滤不影响翻页语义：
        # 一页全是范围外的行 ⇒ 列表按 create_time desc ⇒ 下一页只会更旧 ⇒ 早停。
        if time_to_ms:
            items = [it for it in items if _item_create_ms(it) <= time_to_ms]
        new_items = (
            [it for it in items if _item_create_ms(it) > last_cursor_ms]
            if incremental
            else items
        )

        # 增量模式下只统计**本轮范围内**的行；被时间窗圈回但比游标旧的行既不入
        # stored_ids 也不计入「翻完」的进度比较，否则 total=1994 的账号永远够不着。
        new_count += len(new_items)

        # 内联详情（2026-09-23 重构）：**先拉 SKU 明细、再落库** —— 维度降到商品级，
        # 每个商品的列表数据 + 规格详情凑齐后一次性 upsert，不再分两步。
        # **只对本次范围内的商品**（`new_items`）拉详情 / 落库，不碰范围外的行。
        # 与列表翻页共用限流键 `{account.id}:goods`，自然串行节流。
        page_item_ids = [
            item_id
            for item_id in (_first_text(it.get("item_id")) for it in new_items)
            if item_id
        ]
        # ⚠️ **进度分母 = 平台按同一条查询条件报出的 `total`**（设计 13 §43.1，2026-09-28
        # 用户报障后改）：千帆列表接口按 `create_time_from` **服务端过滤** ⇒ `total` 就是
        # 本条查询条件（窗口下界 / 增量游标）下的条数 —— **有界同步它照样是本次工作量**
        # （实测：账号 8 窗口 2026-07-05~08-11 平台报 713、本地窗口内 707）。
        # ⚠️ 平台没有 `create_time_to`（上界只能本地筛）⇒ 纯历史区间的 `total` 会偏大，
        # 进度条停在 <100% 再随终态跳到 100% 是**如实**的（不谎报完成）。
        #
        # ⛔ **不许拿 `new_count`（本轮范围内枚举数）当分母**（旧实现的有界分支）：它与
        # 分子 `processed` 数的是**同一批商品**（本页枚举出来、立刻被 SKU 批次尝试的那批）
        # ⇒ 每个页边界都恰好 `processed == work_total` ⇒ **第一页跑完进度条就 100%**，
        # 而窗口内还有几百件没处理（真库 `task=82` 实测 `processed=100 / work_total=100`
        # 而 `total=713`）。「分母与分子同源」是这一类 bug 的通用判据（DEF 级）。
        #
        # 拿不到 `total` ⇒ **不报分母**（缺值口径：不知道就不报，不拿一个同源的数冒充）
        # ⇒ 前端如实渲染「比例未知」的不定量条。只允许变大（变小 = 进度倒退，禁止）。
        if total:
            candidate = int(total)
            work_total = candidate if work_total is None else max(work_total, candidate)

        # SKU 明细**逐批**回调（每批 10 个商品 ≈ 一次限流等待 ≈ 7.5 秒）⇒ 进度条在页内
        # 也慢慢涨。回调体只改内存态；刷盘由下面的 `_update_progress` + commit 完成，
        # 所以再补一次「页内每批 commit」的代价不值得（页粒度 ≈ 40 秒已够平滑）。
        processed_base = processed

        def _on_batch(attempted_in_page: int) -> None:
            _update_progress(
                db,
                task,
                stored=new_count,
                total=total,
                page=page_no,
                processed=processed_base + int(attempted_in_page),
                work_total=work_total,
            )

        page_skus = _fetch_page_skus(
            account, adapter, item_ids=page_item_ids, limiter=limiter, on_progress=_on_batch,
        )
        # 「本页已处理」= SKU 批次**已尝试**的商品数（成功 + 失败都算尝试过；
        # 限流等待超时那批一个请求都没发 ⇒ 不计入 ⇒ 进度条到不了 100% 是**如实**的）。
        processed += int(page_skus["done"]) + int(page_skus["failed"])
        detail_done += page_skus["done"]
        detail_failed += page_skus["failed"]
        warnings.extend(page_skus["warnings"])

        page_stored = store_products(
            db, account.user_id, shop_id_fk, new_items,
            skus_map=page_skus["skus_map"], detail_time=page_skus["time"],
        )
        stored_ids.update(page_stored)

        if incremental and new_items:
            # D13：游标只取「已成功落库行的最大值」，单位毫秒。
            page_max = max((_item_create_ms(it) or 0) for it in new_items)
            if page_max > last_cursor_ms:
                last_cursor_ms = page_max
                cursor_advanced = True

        # §27：收集本轮枚举到、且落在同步范围内的平台 **item_id（商品唯一 ID）**。
        # 对账匹配键只有它 —— 标题 / 价格 / 状态会变，时间只用于范围判定，
        # 拿会变的字段当匹配键就会把「改了个标题的商品」误判成「平台没有它」。
        # 放在窗口 / 游标过滤**之后** —— 范围外的行不参与对账判定。
        # 同时记录**本轮真实枚举到的最大 create_time**（增量对账上界收敛用）。
        for _item in new_items:
            _item_id = _first_text(_item.get("item_id"))
            if _item_id:
                reconcile_ids.add(_item_id)
                _stamp = _item_create_ms(_item)
                if _stamp > observed_raw_max_ms:
                    observed_raw_max_ms = _stamp

        _update_progress(
            db,
            task,
            stored=new_count,
            total=total,
            page=page_no,
            processed=processed,
            work_total=work_total,
        )
        db.commit()

        # §17.3：商品列表没有 end_tag，「翻到底」的信号是「本页 0 行」。
        # 平台不返回 has_more（DEF-044 同类坑），不要用它判定。
        # ⚠️ 增量模式下用**过滤后的新行**判早停（见上），但早停 ≠ 翻完：
        # 「整页都是比游标旧的」只代表「增量同步追平了」，本轮 complete 仍为 False
        # （旧行还有没落库的可能）。只有「平台真的一行都不返回」才是翻完。
        if not items:
            complete = True
            break
        if not new_items:
            # 增量追平：不再翻下一页（下一页只会更旧），但不算本轮翻完。
            break
        if total is not None and new_count >= total:
            complete = True
            break
        page_no += 1
        _sleep_between_pages()

    removed = 0
    # §27.5：只有**全量、无时间窗**的同步才删「本次未出现」的行。增量 / 区间同步的
    # 「未出现」只意味着「不在本轮范围」—— 删了就是把没扫到的商品当已下架丢掉。
    # ⚠️ 增量追平后平台回空页也会走到 `complete=True`（那次「未出现」的正确解释是
    # 「没有新的商品」），若无条件删会把整个店铺的商品清空 —— 本守卫顺带堵掉这个缺口。
    full_scope = not incremental and not time_from_ms and not time_to_ms
    if complete and not stopped_by_limit and full_scope:
        removed = _drop_stale_rows(db, shop_id_fk, stored_ids)
    elif not complete or stopped_by_limit:
        warnings.append("未翻完，保留既有行")
    else:
        warnings.append("本轮按同步范围作业，范围外的既有行保留不删（三方不存在的行打标记）")

    # 字段补足（设计 13 §30.3.4）：把 raw_json 有值、筛选列为空的存量行**本地**
    # 回填（零平台请求）。放在翻页之后：本轮新落库的行 already 带全列，这里只
    # 兜迁移前的老行；也放在删旧行之后，避免给即将删掉的行白做工。
    backfilled = backfill_list_columns(db, shop_id_fk)
    if backfilled:
        warnings.append(f"字段补足 {backfilled} 行（从 raw_json 本地回填）")

    if detail_failed:
        warnings.append(f"{detail_failed} 个商品详情获取失败（列表数据已落库，下轮重试）")

    # §26：商品落库后**自愈解析**一次「本地笔记 ↔ 本地商品」关系。
    # 为什么必须在这一侧也做：用户可能先同步笔记（那时商品还没落库 ⇒ 关系解析
    # 不出来）、后同步商品。只在笔记侧解析 = 两个按钮都点过，关系表还是空的
    # （§21.3 同款「只做一半 = 假修复」）。纯本地、零平台请求。
    note_goods_linked = 0
    try:
        from backend.app.services.shop_goods_service import (
            cached_goods_mappings,
            link_note_goods,
        )

        mappings = cached_goods_mappings(db, account.id)
        if mappings:
            note_goods_linked = link_note_goods(db, account, mappings)["linked"]
    except Exception as exc:  # noqa: BLE001 - 自愈失败不影响商品同步本身
        logger.warning(
            "shop goods sync: note-goods self-heal failed (account=%s): %s", account.id, exc
        )

    # §27：对账融合 —— 用**本轮已枚举**的平台 item_id 标记「本地有、平台无」的
    # 商品行（三方不存在数据状态）。匹配键 = 商品唯一 ID `item_id`。零额外平台请求。
    # 枚举不完整（限流 / 兜底 / 显式 max_pages / 页失败）⇒ 本轮不对账（DEF-057）。
    #
    # ⚠️ **增量模式的上界收敛**（2026-09-23）：增量早停（追平）时本轮没枚举完
    # 游标之后的所有行 ⇒ 只对「本轮真实扫过的区间」下结论，否则会把还没枚举到的
    # 行误标成「平台没有」。全量模式不存在这个问题（真的翻到底了）。
    reconcile_to_ms = scope_to_ms
    if incremental and observed_raw_max_ms > 0:
        reconcile_to_ms = (
            min(scope_to_ms, observed_raw_max_ms) if scope_to_ms else observed_raw_max_ms
        )
    enumerate_complete = not (stopped_by_limit or page_cap_hit or max_pages_hit or page_failed)
    if enumerate_complete:
        try:
            from backend.app.services import reconcile_service

            outcome = reconcile_service.mark_goods_missing(
                db,
                shop_id_fk,
                reconcile_ids,
                time_from_ms=scope_from_ms,
                time_to_ms=reconcile_to_ms,
            )
            # 有话可说才写任务行（干净同步不产噪声）
            if outcome["marked"] or outcome["healed"] or outcome["unknown_time"]:
                line = f"对账：标记三方不存在 {outcome['marked']} 件"
                if outcome["healed"]:
                    line += f"，自愈 {outcome['healed']} 件"
                if outcome["unknown_time"]:
                    line += f"；{outcome['unknown_time']} 件取不到创建时间未纳入对账"
                warnings.append(line)
        except Exception as exc:  # noqa: BLE001 - 对账失败不影响商品同步本身
            logger.warning("shop goods sync: reconcile failed (account=%s): %s", account.id, exc)
            warnings.append("对账未执行：标记过程异常，详见日志")
    else:
        warnings.append("对账未执行：平台列表未完整枚举（限流/兜底/失败），不下「平台没有」的结论")

    note = "；".join(warnings)
    # §30.3.5：平台商品总数 + 本地同步时间写进 shop_state（页面「本地台账 N 件 ·
    # 同步于 X」的唯一来源）。total 取不到（一页都没成）时不覆盖旧值 ——
    # 把「不知道」写成 0 会让页面显示「平台 0 件」（缺值口径）。
    if total is not None:
        set_shop_state(account, products_total=total)
    set_shop_state(account, products_synced_at=shanghai_now().isoformat())
    # 内联详情补齐后，统计全店还有多少商品没详情（增量同步可能只覆盖到新商品，
    # 老商品的详情要等下次全量同步时才补上）。NULL = 不知道（前端显示 `-`）。
    detail_remaining = db.scalar(
        select(ShopProduct)
        .where(
            ShopProduct.shop_id_fk == shop_id_fk,
            ShopProduct.detail_synced_at.is_(None),
        )
        .order_by(ShopProduct.id.asc())
        .limit(1)
    )
    if detail_remaining is None:
        detail_total = 0
    else:
        from sqlalchemy import func

        detail_total = db.scalar(
            select(func.count()).select_from(ShopProduct).where(
                ShopProduct.shop_id_fk == shop_id_fk,
                ShopProduct.detail_synced_at.is_(None),
            )
        )

    _finish(
        task,
        status=sts.STATUS_DONE,
        stored=new_count,
        total=total,
        removed=removed,
        complete=complete,
        note=note,
        detail_done=detail_done,
        detail_total=detail_total,
        note_goods_linked=note_goods_linked,
    )
    if cursor_advanced:
        # D13：游标只在本次同步落库成功之后推进。
        # ⚠️ **全量也要写游标**（§22）：不写 = 游标恒 0 = 自动模式永远选全量。
        sts.save_goods_cursor(db, account.id, last_cursor_ms)
    db.commit()
    return {
        "stored": len(stored_ids),
        "total": total,
        "removed": removed,
        "complete": complete,
        "note": note,
        "detail_done": detail_done,
        "detail_total": detail_total,
        "note_goods_linked": note_goods_linked,
    }


# --------------------------------------------------------------------------- #
# SKU 明细归一化（设计 13 §19.4.2）
# --------------------------------------------------------------------------- #
def _sku_stock(sku: dict[str, Any]) -> Optional[int]:
    """SKU 库存。取 `baseStockInfos[0].stock`；取不到返回 None（**不造 0**）。

    实测（2026-09-21，观隅好物桌垫 341 个 SKU）：该店 SKU 响应里
    `baseStockInfos` 普遍为 null，库存计数只在 **item 级** `total_stock`
    （列表响应）上有值 ⇒ SKU 级库存缺值是常态，留 null 由展示层决定怎么显示。
    """
    rows = sku.get("baseStockInfos")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                value = _int_or_none(row.get("stock"))
                if value is not None:
                    return value
    return None


def normalize_skus(skus: Any) -> list[dict[str, Any]]:
    """`get_skus_by_item_ids` 的 SKU 原文 → 白名单字段（**不透传 raw**）。

    为什么必须归一化：实测 10 个商品的详情响应 ≈ 5.4MB（700 个 SKU），
    整条透传会把库吹爆；而展示/检索只需要下面这些。白名单外的字段
    （`boutiqueInfo`/`logisticsInfo`/`faqs`…）**不落** —— 需要时按 item_id
    重新拉取，比存一份会过期的全量raw 便宜。

    缺值口径：标量取不到给 `""` / `None`，**不造 0**（硬约束⑤）。
    """
    if not isinstance(skus, list):
        return []
    out: list[dict[str, Any]] = []
    for sku in skus:
        if not isinstance(sku, dict):
            continue
        price = sku.get("basePriceInfo") if isinstance(sku.get("basePriceInfo"), dict) else {}
        spec_image = sku.get("specImage") if isinstance(sku.get("specImage"), dict) else {}
        buyable = sku.get("buyable")
        freeze = sku.get("freeze")
        out.append(
            {
                "sku_id": _first_text(sku.get("skuId")),
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
        )
    return out


# --------------------------------------------------------------------------- #
# 内联详情拉取（2026-09-23 重构）：每页列表商品的 SKU 明细在落库前拉取
# --------------------------------------------------------------------------- #
def _fetch_page_skus(
    account: PlatformAccount,
    adapter: Any,
    *,
    item_ids: list[str],
    limiter: Any,
    on_progress: Any = None,
) -> dict[str, Any]:
    """拉取一页商品的 SKU 明细，返回 ``{skus_map, done, failed, warnings, time}``。

    按 :data:`apis.xhs_qianfan_apis.SKU_BATCH_SIZE`（10）分批，每批过一次限流器。
    **失败不阻塞**：某批失败记一条 warning 继续下一批，对应商品的详情留空
    （`detail_synced_at = NULL`），下轮重试。凭据失效（`-13000`）停本函数
    （后续页面也没必要翻了，但调用方决定是否继续列表翻页）。

    ``on_progress(attempted_in_page)``（2026-09-28 加）：**每批**回调一次，参数 =
    本页**已尝试**的商品数（含失败）。这是「进度条在页内也慢慢涨」的唯一来源 ——
    一批 10 个商品 ≈ 一次限流等待（额度 8 次/分钟 ⇒ 约 7.5 秒），不接回调的话
    一页 50 个商品要 40 秒才动一格（用户口径：进度条要**慢慢增加**）。
    ⚠️ 回调里的异常全吞：进度是观测，不得影响执行（同 `_ProgressPump` 纪律）。
    """
    from apis.xhs_qianfan_apis import SKU_BATCH_SIZE

    warnings: list[str] = []
    skus_map: dict[str, list[dict]] = {}
    done = 0
    failed = 0

    if not item_ids:
        return {"skus_map": skus_map, "done": 0, "failed": 0, "warnings": [], "time": None}

    now = shanghai_now()
    for start in range(0, len(item_ids), SKU_BATCH_SIZE):
        batch = item_ids[start : start + SKU_BATCH_SIZE]
        if not limiter.wait_allow(f"{account.id}:goods"):
            warnings.append(f"补详情第 {start // SKU_BATCH_SIZE + 1} 批前限流等待超时")
            # ⚠️ 限流等待超时**不算已尝试**（`failed` 照记，供调用方如实报数）：
            # 这一批一个请求都没发出去，算进「已处理」会让进度条虚高。
            failed += len(batch)
            break
        try:
            success, message, raw_payload = adapter.get_skus_by_item_ids(batch)
        except Exception as exc:  # noqa: BLE001 - 单批失败不中断其余
            logger.warning("shop goods sync: sku detail batch failed: %s", exc)
            failed += len(batch)
            warnings.append(f"{len(batch)} 个商品详情获取异常")
            _notify_batch_progress(on_progress, done + failed)
            continue
        if not success:
            failed += len(batch)
            warnings.append(f"{len(batch)} 个商品详情获取失败：{message or '未知原因'}")
            _notify_batch_progress(on_progress, done + failed)
            if "-13000" in str(message):
                break
            continue

        data = raw_payload.get("data") if isinstance(raw_payload, dict) else None
        page_skus_map = data.get("skusMap") if isinstance(data, dict) else None
        page_skus_map = page_skus_map if isinstance(page_skus_map, dict) else {}
        for item_id in batch:
            skus = page_skus_map.get(item_id)
            if isinstance(skus, list):
                skus_map[item_id] = skus
                done += 1
            else:
                # 平台没返回这个商品的 SKU（可能刚删除）⇒ 留空，不算成功
                failed += 1
        _notify_batch_progress(on_progress, done + failed)

    return {
        "skus_map": skus_map,
        "done": done,
        "failed": failed,
        "warnings": warnings,
        "time": now,
    }


def _notify_batch_progress(on_progress: Any, attempted_in_page: int) -> None:
    """一批 SKU 拉完 ⇒ 通知进度回调（异常全吞：观测不得影响执行）。"""
    if on_progress is None:
        return
    try:
        on_progress(int(attempted_in_page))
    except Exception:  # noqa: BLE001 - 观测失败不得影响执行
        logger.debug("shop goods sync: batch progress callback failed", exc_info=True)


def _item_create_ms(item: dict[str, Any]) -> int:
    """商品行的 ``create_time``（**毫秒**，响应字段就是毫秒，不用换算）。"""
    value = item.get("create_time")
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _mark_credential_state(
    db: Session, account: PlatformAccount, message: str, payload: Any
) -> None:
    """`-13000` → 把账号的千帆状态标 `invalid`（页面的「重新登录千帆」提示据此接管）。"""
    from backend.app.services.shop_binding_service import set_shop_state

    code = payload.get("code") if isinstance(payload, dict) else None
    if code == -13000 or "-13000" in str(message):
        set_shop_state(account, status="invalid", status_message=str(message)[:200])
        db.commit()


def _drop_stale_rows(db: Session, shop_id_fk: int, keep_ids: set[str]) -> int:
    """**仅在完整同步成功后**删除本次未出现的商品行。返回删除条数。"""
    query = delete(ShopProduct).where(ShopProduct.shop_id_fk == shop_id_fk)
    if keep_ids:
        query = query.where(ShopProduct.item_id.notin_(keep_ids))
    result = db.execute(query)
    db.commit()
    return int(result.rowcount or 0)


def _sleep_between_pages() -> None:
    """页间停顿 = 基础值 × 随机抖动（唯一实现 `sync_pacing.sleep_between_pages`）。

    与笔记同步（`note_sync_service`）**同一把尺子**：拉长 + 随机化，避免等距
    节奏被平台风控识别。读模块级 `PAGE_GAP_SECONDS`，测试置 0 仍免等待。
    """
    from backend.app.services.sync_pacing import sleep_between_pages

    sleep_between_pages(PAGE_GAP_SECONDS, cap=MAX_PAGE_GAP_SECONDS)


def _update_progress(
    db: Session,
    task: Task,
    *,
    stored: int,
    total: Optional[int],
    page: int,
    processed: Optional[int] = None,
    work_total: Optional[int] = None,
) -> None:
    """写任务行的进行中进度。

    **刷盘点**（设计 13 §42.3，2026-09-28 用户口径「每执行 50 条应更新一次任务执行详情
    数据」+「进度条慢慢增加」）：翻页循环里**每页一次**（本页详情拉完 + 落库之后，
    `stored` 才为真），页粒度 = `sts.PROGRESS_FLUSH_EVERY`（默认 50 条）；
    **页内**由 `_fetch_page_skus` 的逐批回调更新（每批 10 个商品）——
    与笔记同步**同一把尺子**。

    ``processed`` / ``work_total`` = **进度条真正看的那一对**（单调口径，见
    `sts.progress_percent` 的三条优先级）：分子只增、分母固定 ⇒ 比值单调，
    不再出现「一页内 0→100 反复跳」。
    """
    payload = sts.task_payload(task)
    progress: dict[str, Any] = {"stored": stored, "total": total, "page": page}
    if processed is not None:
        progress["processed"] = processed
    if work_total is not None:
        progress["work_total"] = work_total
    payload["progress"] = progress
    task.payload = payload
    task.status = sts.STATUS_RUNNING
    if task.started_at is None:
        task.started_at = shanghai_now()
    if total:
        task.progress = min(100, int(stored * 100 / total))


def _finish(
    task: Task,
    *,
    status: str,
    stored: int,
    total: Optional[int],
    removed: int = 0,
    complete: bool = False,
    note: str = "",
    error: str = "",
    detail_done: int = 0,
    detail_total: Optional[int] = 0,
    note_goods_linked: int = 0,
) -> None:
    # ⚠️ 取消竞态（设计 13 §24.8）：已取消 ⇒ 不覆写 cancelled 终态，只补
    # finished_at。否则跑完的线程把「已取消」改回 done，用户以为取消没用。
    if task_cancel.is_cancelled(task.id):
        status = sts.STATUS_CANCELLED
        note = f"{note}；已被用户取消" if note else "已被用户取消"
    task.status = status
    task.finished_at = shanghai_now()
    payload = sts.task_payload(task)
    payload["progress"] = {**(payload.get("progress") or {}), "stored": stored, "total": total}
    payload["removed"] = removed
    payload["complete"] = complete
    payload["note"] = note
    payload["error"] = error
    # 详情补齐进度（设计 13 §19.5）：`detail_total` = 补完后队列剩余量，
    # None = 不知道（前端显示 `-`，不渲染 0 —— 缺值口径）。
    payload["detail_done"] = detail_done
    payload["detail_total"] = detail_total
    # 本轮顺带解析的「笔记 ↔ 商品」关系行数（§26；自愈解析，失败不影响商品同步）。
    payload["note_goods_linked"] = note_goods_linked
    task.payload = payload
    task_cancel.clear(task.id)


def _mark_failed(db: Session, task: Optional[Task], reason: str) -> None:
    if task is None:
        return
    # 取消竞态同 `_finish`：已取消的任务不覆写为 failed（§24.8）。
    if task_cancel.is_cancelled(task.id):
        try:
            task.status = sts.STATUS_CANCELLED
            task.finished_at = shanghai_now()
            payload = sts.task_payload(task)
            payload["error"] = payload.get("error", "")
            payload["note"] = payload.get("note", "") or "已被用户取消"
            task.payload = payload
            db.commit()
        except Exception:  # noqa: BLE001
            logger.warning("shop goods sync: could not persist cancelled state")
        finally:
            task_cancel.clear(task.id)
        return
    try:
        task.status = sts.STATUS_FAILED
        task.finished_at = shanghai_now()
        payload = sts.task_payload(task)
        payload["error"] = reason
        task.payload = payload
        db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("shop goods sync: could not mark task failed")


def run_shop_goods_sync(
    account_id: int,
    *,
    task_id: Optional[int] = None,
    user_id: Optional[int] = None,
    max_pages: Optional[int] = sts.DEFAULT_MAX_PAGES,
    page_size: int = sts.DEFAULT_PAGE_SIZE,
    source: str = "manual",
    adapter_factory: Any = None,
    mode: str = sts.SYNC_MODE_AUTO,
    time_from_ms: int = 0,
    time_to_ms: int = 0,
) -> dict[str, Any]:
    """执行一次店铺商品同步（供后台线程 / 调度器调用）。**不向上抛异常。**

    2026-09-23 重构：每页列表落库前**先拉 SKU 详情**，列表 + 规格一次性落库，
    不再有单独的多轮补齐队列。
    """
    db = SessionLocal()
    task: Optional[Task] = None
    try:
        account = db.get(PlatformAccount, account_id)
        if account is None:
            return {"task_id": task_id, "stored": 0, "total": None, "removed": 0,
                    "complete": False, "note": "", "error": "账号不存在",
                    "detail_done": 0, "detail_total": 0, "note_goods_linked": 0,
                    "status": sts.STATUS_FAILED}

        if task_id is not None:
            task = db.get(Task, task_id)
        if task is None:
            task = sts.create_task(
                db,
                user_id=user_id or account.user_id,
                task_type=sts.SHOP_GOODS_SYNC_TASK_TYPE,
                account_id=account_id,
                source=source,
                max_pages=max_pages,
                page_size=page_size,
                status=sts.STATUS_RUNNING,
            )
            db.commit()
            db.refresh(task)

        # 取消竞态（设计 13 §24.8）：线程启动前就被取消 ⇒ 不翻转为 running。
        if task.status == sts.STATUS_CANCELLED:
            if task.finished_at is None:
                task.finished_at = shanghai_now()
                db.commit()
            return {"task_id": task.id, "stored": 0, "total": None, "removed": 0,
                    "complete": False, "note": "已被用户取消", "error": "",
                    "detail_done": 0, "detail_total": 0, "note_goods_linked": 0,
                    "status": sts.STATUS_CANCELLED}
        task_cancel.register(task.id)

        task.status = sts.STATUS_RUNNING
        if task.started_at is None:
            task.started_at = shanghai_now()
        db.commit()

        shop_id_fk = _resolve_shop_id(db, account)  # 解不出店铺 → 抛（不编店铺）

        if adapter_factory is None:
            from backend.app.adapters.xhs.qianfan_api_adapter import (
                get_qianfan_api_adapter_factory,
            )

            adapter_factory = get_qianfan_api_adapter_factory()

        result = _sync_with_task(
            db, task, account, shop_id_fk, adapter_factory,
            max_pages=max_pages, page_size=page_size, mode=mode,
            time_from_ms=time_from_ms, time_to_ms=time_to_ms,
        )
        return {"task_id": task.id, "status": task.status, "error": "", **result}
    except SyncFailure as exc:
        reason = f"{exc.code}: {exc.message}" if exc.code else exc.message
        _mark_failed(db, task, reason)
        return {"task_id": task.id if task else task_id, "stored": 0, "total": None,
                "removed": 0, "complete": False, "note": "", "error": reason,
                "detail_done": 0, "detail_total": 0, "note_goods_linked": 0,
                "status": sts.STATUS_FAILED}
    except Exception as exc:  # noqa: BLE001 - 调度器/线程调用，必须吞异常
        logger.error("run_shop_goods_sync failed (account=%s): %s", account_id, exc)
        _mark_failed(db, task, str(exc))
        return {"task_id": task.id if task else task_id, "stored": 0, "total": None,
                "removed": 0, "complete": False, "note": "", "error": str(exc),
                "detail_done": 0, "detail_total": 0, "note_goods_linked": 0,
                "status": sts.STATUS_FAILED}
    finally:
        db.close()


def start_shop_goods_sync(
    account_id: int,
    *,
    user_id: int,
    max_pages: Optional[int] = sts.DEFAULT_MAX_PAGES,
    page_size: int = sts.DEFAULT_PAGE_SIZE,
    source: str = "manual",
    mode: str = sts.SYNC_MODE_AUTO,
    time_from_ms: int = 0,
    time_to_ms: int = 0,
) -> dict[str, Any]:
    """建任务行 + 唤醒账号任务 worker，**立即返回**任务摄要。

    §35.16（2026-09-27 用户口径「账号卡点同步商品，任务中心任务日志没有新增」）：
    执行**不再起独立线程**（原 `shop-goods-sync-{account_id}`），改由
    `account_task_service` 的账号任务 worker 认领 —— 串行 + 进程退出可续跑 + 记录页可见。
    防重口径与响应形状**逐字不变**。

    防重（需求 N3）：该账号已有未完成的商品同步 → 返回**已有任务**（`reused=True`）。
    """
    db = SessionLocal()
    try:
        existing = sts.find_active_task(db, sts.SHOP_GOODS_SYNC_TASK_TYPE, account_id)
        if existing is not None:
            return {"reused": True, **sts.task_summary(existing)}
        task = sts.create_task(
            db,
            user_id=user_id,
            task_type=sts.SHOP_GOODS_SYNC_TASK_TYPE,
            account_id=account_id,
            source=source,
            max_pages=max_pages,
            page_size=page_size,
            # §35.16：执行改由账号任务 worker 从**任务行**重建参数 ⇒ 同步语义必须落库
            # （原线程版靠 args 传，行里没有）。漏写 = 用户选的「全量 / 时间区间」静默丢失。
            extra={"mode": mode, "time_from_ms": time_from_ms, "time_to_ms": time_to_ms},
        )
        db.commit()
        db.refresh(task)
        summary = sts.task_summary(task)
        task_id = task.id
        # 建行即登记取消事件（§24.8）：取消端点可能在线程启动前就到。
        task_cancel.register(task_id)
    finally:
        db.close()

    # §35.16：不再起独立线程 —— 交给账号任务 worker 认领执行（串行 + 可续跑 + 可见）。
    # 执行参数已随任务行落库（见上面的 `extra`），worker 从行重建。
    from backend.app.services import account_task_service

    account_task_service.ensure_account_task_worker()
    return {"reused": False, **summary}
