"""订单同步：千帆订单列表 → 本地 ``orders`` + ``order_items`` + ``order_shipments``
+ ``after_sales``（设计 16 §7.3，M6 方向 6 · **一期只读**）。

## 与商品同步（``shop_goods_sync_service``）的分工

两者都打千帆、都翻页、都按平台 ID 幂等 upsert，但**同步的是两种数据**：

| | 商品同步 | 订单同步（本模块） |
|---|---|---|
| 接口 | ``product/search_item_v2`` | ``fulfillment/order/page`` + ``order/detail`` |
| 落表 | ``shop_products``（商品台账） | ``orders`` 等 5 张（交易单据） |
| 幂等键 | ``item_id`` | ``order_no`` |
| 增量游标 | 商品 ``create_time`` | 订单 ``orderedAt`` |

⚠️ **订单表不要和商品表混**（设计 16 §7 的边界提醒）：一个记「我在卖什么」，
一个记「谁买过什么」。

## 三条纪律（与商品同步同构）

* **增量同步**：按 ``synced_at`` 之后的 ``orderedAt`` 拉新单；已存在的订单
  **更新**状态 / 金额 / 物流 / 售后（订单是**活数据**：状态会变，不像商品
  增量只取新增）。分页翻到无下一页（限流**等待式**，``ABSOLUTE_PAGE_CAP`` 沿用）。
* **没有千帆登录态就不同步**：账号没有可用的 ``ark`` 令牌 → 任务 ``failed``
  并写明原因，**不编订单 / 不编店铺**（缺值口径，不造假）。
* **异常先 ``rollback()`` 再记失败**：不落半截数据（设计 16 §7.3 明文要求）。

## 履约数据口径（2026-09-29 已定）

收货地址 / 收件人 / 手机号**完整值落库**（本地单机 SQLite、数据不出商家机器；
千帆默认就给脱敏串 `及*` / `184***3034`，平台给什么存什么）。防护在展示层：
界面默认脱敏，完整值仅发货 / 打单 / 导出履约场景使用（M6 只做展示层脱敏 +
导出履约值）。⚠️ **敏感字段不进日志**（硬约束：日志里打 ``[REDACTED]``）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select, func, false
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.time import SHANGHAI_TZ, shanghai_now
from backend.app.models.order import (
    AFTERSALE_STATUS_PENDING,
    ORDER_REMARK_KIND_ORDER,
    ORDER_STATUS_MAP,
    ORDER_STATUSES,
    AfterSale,
    Order,
    OrderItem,
    OrderRemark,
    OrderShipment,
    resolve_order_status,
)
from backend.app.models.platform_account import PlatformAccount
from backend.app.models.shop_product import ShopProduct
from backend.app.models.task import Task
from backend.app.services import sync_task_service as sts
from backend.app.services import task_cancel
from backend.app.services.rate_limiter import get_rate_limiter

logger = logging.getLogger(__name__)

#: 任务类型（``Task.task_type``；建任务处与端点反查共用，写死两处就是漂移源）。
ORDER_SYNC_TASK_TYPE = "xhs_order_sync"

#: 全量同步默认覆盖的回看窗口（天）。订单是交易数据，平台只保有限期的历史；
#: 真机实测账号 8 最早一单是 2026-07-05（当时往前约 90 天），365 天足够覆盖。
FULL_SYNC_LOOKBACK_DAYS = 365

#: 页与页之间的停顿基础值（秒）。与商品同步同口径（用户要求「拉长间隔、
#: 随机化」），实际停顿再乘抖动系数（``sync_pacing``）。测试置 0。
PAGE_GAP_SECONDS = 5.0

#: 日志里敏感字段的固定占位（硬约束：手机号等不进日志）。
_REDACTED = "[REDACTED]"


class OrderSyncFailure(Exception):
    """本次订单同步失败（原因可展示给用户，不含 Cookie 等敏感值）。"""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# --------------------------------------------------------------------------- #
# 时间解析：千帆给的是 `YYYY-MM-DD HH:MM:SS`（上海时间，无时区）
# --------------------------------------------------------------------------- #
def _parse_datetime(value: Any) -> Optional[datetime]:
    """千帆时间串 → 本地 datetime；空 / 坏值 → ``None``（不猜）。"""
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _to_epoch_ms(value: Any) -> Optional[int]:
    dt = _parse_datetime(value)
    if dt is None:
        return None
    return int(dt.replace(tzinfo=SHANGHAI_TZ).timestamp() * 1000)


# --------------------------------------------------------------------------- #
# 归一化：千帆条目 → 本地行
# --------------------------------------------------------------------------- #
def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(*candidates: Any) -> Optional[float]:
    """取第一个能转成浮点的候选（前面的优先；都坏 ⇒ ``None``）。"""
    for value in candidates:
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        if num != num:  # NaN → None
            return None
        return num
    return None


def _mask_for_log(text: str) -> str:
    """日志脱敏：非空敏感串一律打 ``[REDACTED]``（不看长度，不偷看内容）。"""
    return _REDACTED if text else ""


def _item_image(sku: dict[str, Any]) -> str:
    link = _first_text(sku.get("image"))
    if link.startswith("//"):
        return "https:" + link
    return link


def normalize_sku(sku: dict[str, Any]) -> Optional[dict[str, Any]]:
    """千帆 SKU 条目 → ``order_items`` 的列。缺主键（skuId）→ ``None``。"""
    sku_id = _first_text(sku.get("skuId"))
    if not sku_id:
        return None
    return {
        "sku_id": sku_id,
        "item_id": _first_text(sku.get("itemId")),
        "item_name": _first_text(sku.get("skuName"), sku.get("displayName")),
        "item_image": _item_image(sku),
        "sku": _first_text(sku.get("skuSpecification"), sku.get("specification")),
        "quantity": _to_int(sku.get("skuQuantity")) or 1,
        "unit_price": _to_float(sku.get("skuSoldPrice"), sku.get("soldPrice")),
        "amount": _to_float(sku.get("skuTotalPaidAmount")),
        "refund_status": _first_text(sku.get("afterSaleStatusDesc")),
    }


def normalize_package(
    package: dict[str, Any], *, account_id: int, user_id: int, seller_id: str
) -> Optional[dict[str, Any]]:
    """千帆订单条目（``packages[]``）→ ``orders`` 的列。

    缺 ``orderId`` → ``None``（没有订单号就没有行的身份，跳过且不计入成功）。
    """
    order_no = _first_text(package.get("orderId"))
    if not order_no:
        return None
    user_info = package.get("userInfo") if isinstance(package.get("userInfo"), dict) else {}
    mark = package.get("sellerMarkInfo") if isinstance(package.get("sellerMarkInfo"), dict) else {}
    discount = (_to_float(package.get("redDiscountAmount")) or 0) + (
        _to_float(package.get("sellerDiscountAmount")) or 0
    )
    return {
        "platform_account_id": account_id,
        "user_id": user_id,
        "shop_id": seller_id,
        "order_no": order_no,
        "package_id": _first_text(package.get("packageId")),
        "type_desc": _first_text(package.get("typeDesc")),
        "buyer_nick": _first_text(user_info.get("nickName")),
        "buyer_user_id": _first_text(package.get("userId")),
        "receiver_name_full": _first_text(user_info.get("name")),
        "receiver_phone_full": _first_text(user_info.get("phone")),
        "receiver_province": _first_text(user_info.get("province")),
        "receiver_city": _first_text(user_info.get("city")),
        "receiver_address_full": _first_text(
            user_info.get("address"), user_info.get("street"), user_info.get("town")
        ),
        "status": resolve_order_status(package.get("status"), package.get("afterSaleStatus")),
        "platform_status": _to_int(package.get("status")),
        "platform_after_sale_status": _to_int(package.get("afterSaleStatus")),
        "total_amount": _to_float(package.get("totalOrderAmount")),
        "pay_amount": _to_float(package.get("actualPaid")),
        "freight": _to_float(package.get("shippingFee")),
        "discount": discount or None,
        "pay_method": "",
        "order_source": _first_text(package.get("sendFrom")),
        "logistics_company": _first_text(package.get("expressCompanyName")),
        "tracking_no": _first_text(package.get("expressNo")),
        "created_at": _parse_datetime(package.get("orderedAt")) or shanghai_now(),
        "pay_time": _parse_datetime(package.get("paidAt")),
        "ship_time": _parse_datetime(package.get("shippedAt")),
        "finish_time": _parse_datetime(package.get("finishedAt")),
        "promise_ship_time": _parse_datetime(package.get("promiseLastShipAt")),
        "platform_updated_at": _parse_datetime(package.get("updatedAt")),
        "synced_at": shanghai_now(),
        "seller_mark": _first_text(mark.get("sellerMarkNote")),
        "skus": [
            normalized
            for normalized in (normalize_sku(sku) for sku in (package.get("skus") or []) if isinstance(sku, dict))
            if normalized is not None
        ],
    }


def normalize_shipment(
    package: dict[str, Any], *, order_id: int, user_id: int
) -> dict[str, Any]:
    """千帆订单条目 → ``order_shipments`` 的列（主包裹一行）。"""
    return {
        "order_id": order_id,
        "user_id": user_id,
        "package_id": _first_text(package.get("packageId")),
        "logistics_company": _first_text(package.get("expressCompanyName")),
        "logistics_company_code": _first_text(package.get("expressCompanyCode")),
        "tracking_no": _first_text(package.get("expressNo")),
        "ship_time": _parse_datetime(package.get("shippedAt")),
        "sign_time": _parse_datetime(package.get("finishedAt")),
        "warehouse_desc": _first_text(package.get("warehouseDesc")),
    }


def normalize_after_sale(row: dict[str, Any], *, order_id: int, user_id: int) -> Optional[dict[str, Any]]:
    """千帆售后条目（订单详情的 ``after_sale_list[]``）→ ``after_sales`` 的列。"""
    after_sale_id = _first_text(row.get("after_sale_id"), row.get("afterSaleId"), row.get("id"))
    if not after_sale_id:
        return None
    return {
        "order_id": order_id,
        "user_id": user_id,
        "after_sale_id": after_sale_id,
        "type": _first_text(row.get("type_desc"), row.get("typeDesc"), row.get("type")),
        "reason": _first_text(row.get("reason")),
        "status": _first_text(row.get("status_desc"), row.get("statusDesc"))
        or AFTERSALE_STATUS_PENDING,
        "amount": _to_float(row.get("refund_amount"), row.get("amount")),
        "apply_time": _parse_datetime(row.get("apply_time"), row.get("applyTime"), row.get("created_at")),
        "handle_time": _parse_datetime(row.get("handle_time"), row.get("handleTime")),
        "handle_result": _first_text(row.get("handle_result"), row.get("handleResult")),
        "synced_at": shanghai_now(),
    }


# --------------------------------------------------------------------------- #
# 落库（幂等 upsert）
# --------------------------------------------------------------------------- #
def _link_shop_product_ids(db: Session, items: list[dict[str, Any]]) -> None:
    """把 SKU 的 ``item_id`` 批量连到本地 ``shop_products`` 的主键（删商品留 NULL）。"""
    item_ids = {_first_text(item.get("item_id")) for item in items}
    item_ids.discard("")
    if not item_ids:
        return
    id_map: dict[str, int] = {}
    for product in db.scalars(
        select(ShopProduct).where(ShopProduct.item_id.in_(list(item_ids)))
    ).all():
        id_map.setdefault(product.item_id, product.id)
    for item in items:
        product_id = id_map.get(_first_text(item.get("item_id")))
        if product_id is not None:
            item["shop_product_id"] = product_id


def upsert_order(
    db: Session, normalized: dict[str, Any], *, account_id: int
) -> Optional[Order]:
    """按 ``order_no`` 幂等 upsert 一条订单（含明细与主包裹物流）。

    **幂等口径（设计 16 §7.3）**：存在 ⇒ 更新状态 / 金额 / 物流 / 售后；
    不存在 ⇒ 插入。同 ``order_no`` 重复同步**不产生重复行**（唯一约束兜底）。

    返回落库的 ``Order``（缺 ``order_no`` 的条目返回 ``None``）。
    """
    order_no = normalized["order_no"]
    # ⚠️ **空单号必须挡在这里**（2026-10-04 实测补）：docstring 许诺「缺 order_no
    # 返回 None」，但实现只判 `normalized["order_no"]`（有键即可）——
    # 于是空串会落一行 `order_no=''`，而该列有唯一约束 ⇒ **第二笔空单号订单
    # 同步直接撞 UNIQUE 崩掉整个同步任务**。缺值口径：缺标识就不落（不造）。
    if not str(order_no or "").strip():
        normalized.pop("skus", None)
        return None
    skus = normalized.pop("skus")
    order = db.scalar(
        select(Order).where(
            Order.platform_account_id == account_id, Order.order_no == order_no
        )
    )
    if order is None:
        order = Order(**normalized)
        db.add(order)
        db.flush()
        created = True
    else:
        # 只更新可变字段（订单状态 / 金额 / 物流 / 售后 / 时间会变；
        # 订单号 / 账号 / 买家身份不会变，不重写以免覆盖唯一可信来源）。
        for key in (
            "status", "platform_status", "platform_after_sale_status",
            "total_amount", "pay_amount", "freight", "discount",
            "logistics_company", "tracking_no", "pay_method", "order_source",
            "pay_time", "ship_time", "finish_time", "promise_ship_time",
            "platform_updated_at", "synced_at", "seller_mark",
            "type_desc", "package_id", "shop_id",
        ):
            if key in normalized:
                setattr(order, key, normalized[key])
        db.flush()
        created = False

    # 明细：按 (order_id, sku_id) 全量替换（SKU 级退款状态会变，且订单不会
    # 无中生有多出 SKU —— 全量替换比分条 diff 简单且不会漏）。
    _link_shop_product_ids(db, skus)
    existing_sku_ids = {
        item.sku_id for item in db.scalars(select(OrderItem).where(OrderItem.order_id == order.id)).all()
    }
    for sku in skus:
        if sku["sku_id"] in existing_sku_ids:
            db.query(OrderItem).filter(
                OrderItem.order_id == order.id, OrderItem.sku_id == sku["sku_id"]
            ).update(
                {
                    "item_name": sku["item_name"],
                    "item_image": sku["item_image"],
                    "sku": sku["sku"],
                    "quantity": sku["quantity"],
                    "unit_price": sku["unit_price"],
                    "amount": sku["amount"],
                    "refund_status": sku["refund_status"],
                    "shop_product_id": sku.get("shop_product_id"),
                },
                synchronize_session=False,
            )
        else:
            db.add(OrderItem(order_id=order.id, user_id=order.user_id, **sku))
    db.flush()

    # 主包裹物流：全量替换（一行）。
    shipment = normalize_shipment(
        {"packageId": normalized.get("package_id") or "", "expressCompanyName": order.logistics_company,
         "expressCompanyCode": "", "expressNo": order.tracking_no,
         "shippedAt": None, "finishedAt": None, "warehouseDesc": ""},
        order_id=order.id, user_id=order.user_id,
    )
    if shipment["package_id"] or shipment["tracking_no"]:
        existing = db.scalar(
            select(OrderShipment).where(OrderShipment.order_id == order.id)
        )
        if existing is None:
            db.add(OrderShipment(**shipment))
        else:
            for key, value in shipment.items():
                if value:
                    setattr(existing, key, value)
    db.flush()
    logger.debug(
        "order upsert: order_no=%s created=%s items=%d",
        order_no, created, len(skus),
    )
    return order


def upsert_after_sales_from_detail(
    db: Session, order: Order, after_sale_list: Any
) -> int:
    """把订单详情的 ``after_sale_list`` 落进 ``after_sales``。返回落库条数。"""
    if not isinstance(after_sale_list, list):
        return 0
    written = 0
    for row in after_sale_list:
        if not isinstance(row, dict):
            continue
        normalized = normalize_after_sale(row, order_id=order.id, user_id=order.user_id)
        if normalized is None:
            continue
        existing = db.scalar(
            select(AfterSale).where(
                AfterSale.order_id == order.id,
                AfterSale.after_sale_id == normalized["after_sale_id"],
            )
        )
        if existing is None:
            db.add(AfterSale(**normalized))
        else:
            for key, value in normalized.items():
                if value:
                    setattr(existing, key, value)
        written += 1
    if written:
        db.flush()
    return written


# --------------------------------------------------------------------------- #
# 同步主循环
# --------------------------------------------------------------------------- #
def _order_cursor_key(account_id: int) -> str:
    """限流键（与商品同步的 ``:goods`` 分开，两个任务可并发）。"""
    return f"{account_id}:orders"


def _sleep_between_pages() -> None:
    from backend.app.services.sync_pacing import paced_sleep

    paced_sleep(PAGE_GAP_SECONDS)


def _fetch_order_detail(adapter: Any, package_id: str) -> tuple[bool, str, Any]:
    return adapter.get_order_detail(package_id)


def start_order_sync(
    account_id: int,
    *,
    user_id: int,
    source: str = "manual",
    max_pages: Optional[int] = None,
    page_size: int = sts.DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """API 层入口（与 `shop_goods_sync_service.start_shop_goods_sync` 同构）。

    * **重入保护**：该账号已有订单同步任务在跑 ⇒ 直接返回那条任务的摄要，
      不新建第二条（`reused=True` 让 API 知道「这不是新任务，别再建日志行」）。
    * 否则建一条任务行（`status=running`），**返回后由调用方在后台跑完** ——
      本函数只负责「建行 / 判重」，执行体是 `run_order_sync(task_id=...)`。
    """
    from backend.app.services import sync_task_service as sts

    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        if account is None or account.deleted_at is not None:
            return {"task_id": None, "status": "failed", "error": "账号不存在或已删除"}
        existing = sts.find_active_task(db, ORDER_SYNC_TASK_TYPE, account.id)
        if existing is not None:
            return {
                "task_id": existing.id,
                "status": "running",
                "reused": True,
                "error": "",
                "note": "该账号已有订单同步任务在跑",
            }
        task = sts.create_task(
            db,
            user_id=user_id,
            task_type=ORDER_SYNC_TASK_TYPE,
            account_id=account.id,
            source=source,
            max_pages=max_pages,
            page_size=page_size,
            status=sts.STATUS_RUNNING,
        )
        db.commit()
        # ⚠️ commit 之后 `task` 已是 detached 对象（会话关闭后访问属性会触发
        # DetachedInstanceError）。这里只需它的 id，commit 前就已生成，直接读。
        task_id = int(task.id)
    finally:
        db.close()

    import threading

    threading.Thread(
        target=_run_sync_thread,
        args=(account_id, task_id, user_id, source, max_pages, page_size),
        daemon=True,
        name=f"order-sync-{account_id}",
    ).start()
    return {"task_id": task_id, "status": "running", "error": "", "note": ""}


def _run_sync_thread(
    account_id: int,
    task_id: int,
    user_id: int,
    source: str,
    max_pages: Optional[int],
    page_size: int,
) -> None:
    """后台执行体（与商品同步的线程化同构）。失败只进日志 + 任务行，不抛。"""
    db = SessionLocal()
    try:
        run_order_sync(
            account_id,
            task_id=task_id,
            user_id=user_id,
            source=source,
            max_pages=max_pages,
            page_size=page_size,
        )
    except Exception:  # noqa: BLE001 - 后台线程，必须吞
        logger.exception("order sync thread for account %s failed", account_id)
    finally:
        db.close()


def run_order_sync(
    account_id: int,
    *,
    task_id: Optional[int] = None,
    user_id: Optional[int] = None,
    page_size: int = sts.DEFAULT_PAGE_SIZE,
    source: str = "manual",
    adapter_factory: Any = None,
    lookback_days: int = FULL_SYNC_LOOKBACK_DAYS,
    max_pages: Optional[int] = sts.DEFAULT_MAX_PAGES,
) -> dict[str, Any]:
    """执行一次订单同步（供后台线程 / 调度器调用）。**不向上抛异常。**

    增量语义：有同步历史 ⇒ 从上次同步到的最早 ``orderedAt`` 往后；
    没有历史 ⇒ 全量（近 ``lookback_days`` 天）。
    """
    failure: dict[str, Any] = {
        "task_id": task_id, "synced": 0, "total": None,
        "complete": False, "note": "", "error": "",
        "after_sales": 0, "status": sts.STATUS_FAILED,
    }
    db = SessionLocal()
    task: Optional[Task] = None
    try:
        account = db.get(PlatformAccount, account_id)
        if account is None or account.deleted_at is not None:
            return {**failure, "error": "账号不存在或已删除"}
        if task_id is not None:
            task = db.get(Task, task_id)
        if task is None:
            task = sts.create_task(
                db,
                user_id=user_id or account.user_id,
                task_type=ORDER_SYNC_TASK_TYPE,
                account_id=account_id,
                source=source,
                max_pages=max_pages,
                page_size=page_size,
                status=sts.STATUS_RUNNING,
            )
            db.commit()
            db.refresh(task)
        task.status = sts.STATUS_RUNNING
        task.started_at = shanghai_now()
        db.commit()
        task_cancel.register(task.id)

        # 千帆门禁（与商品同步同构）：只认本账号自己的 ark 通道。
        from backend.app.services.shop_binding_service import (
            account_for_ark_gate,
            has_ark_session,
            qianfan_cookies_for_account,
        )

        if account_for_ark_gate(db, account.id) is None:
            raise OrderSyncFailure("该账号不存在或已删除，无法同步订单。", code="ACCOUNT_NOT_FOUND")
        if not has_ark_session(db, account.id):
            raise OrderSyncFailure(
                "该账号没有可用的千帆登录态，无法同步订单。", code="ARK_SESSION_REQUIRED"
            )
        cookies = qianfan_cookies_for_account(db, account.id)
        if not cookies:
            raise OrderSyncFailure("该账号缺少千帆（ark）令牌。", code="QIANFAN_CREDENTIAL_MISSING")
        factory = adapter_factory
        if factory is None:
            from backend.app.adapters.xhs.qianfan_api_adapter import (
                get_qianfan_api_adapter_factory,
            )

            factory = get_qianfan_api_adapter_factory()
        try:
            adapter = factory(cookies)
        except Exception as exc:  # noqa: BLE001
            raise OrderSyncFailure(f"千帆凭据无法解密：{type(exc).__name__}", code="BINDING_UNREADABLE")

        # 卖家 ID（落库的 shop_id，排障用）。
        seller_id = ""
        ok, msg, payload = adapter.get_seller_info()
        if ok and isinstance(payload, dict):
            data = payload.get("data") or {}
            seller_id = _first_text(
                data.get("seller_id"), (data.get("ark_seller_basic_info") or {}).get("seller_id")
            )

        # 增量游标：上次**最早**同步到的订单时间。订单按 orderedAt desc 翻页，
        # 「翻完」= 本页最早一行早于游标（与商品同步的追平早停同构）。
        cursor_ms = _load_order_cursor(db, account.id)
        if cursor_ms:
            window_from_ms = cursor_ms
            scope_note = "增量同步"
        else:
            from datetime import timedelta

            window_from_ms = int((shanghai_now() - timedelta(days=lookback_days)).timestamp() * 1000)
            scope_note = f"全量同步（近 {lookback_days} 天）"
        window_to_ms = int(shanghai_now().timestamp() * 1000)
        # ⚠️ 时间窗口不能是 0/0（真机实测：平台挂到 15s 读超时，见 SDK docstring）。
        if not window_from_ms:
            window_from_ms = 1

        limiter = get_rate_limiter()
        page_no = 1
        total: Optional[int] = None
        synced = 0
        after_sale_rows = 0
        warnings: list[str] = []
        complete = False
        page_cap_hit = False
        max_pages_hit = False
        page_failed = False
        earliest_ms: Optional[int] = None
        seen_order_nos: set[str] = set()

        while True:
            if task_cancel.is_cancelled(task.id):
                warnings.append("用户已取消")
                break
            if page_no > sts.ABSOLUTE_PAGE_CAP:
                warnings.append(f"已达绝对页数兜底（{sts.ABSOLUTE_PAGE_CAP} 页）")
                page_cap_hit = True
                break
            if max_pages is not None and page_no > max_pages:
                warnings.append(f"已达指定页数上限（{max_pages} 页）")
                max_pages_hit = True
                break
            if not limiter.wait_allow(_order_cursor_key(account_id)):
                warnings.append(f"第 {page_no} 页前限流等待超时")
                break

            ok, msg, payload = adapter.get_order_page(
                page_no=page_no, page_size=page_size,
                start_time_ms=window_from_ms, end_time_ms=window_to_ms,
            )
            if not ok:
                _mark_credential_state(db, account, msg, payload)
                if page_no == 1:
                    raise OrderSyncFailure(msg or "订单列表获取失败")
                warnings.append(f"第 {page_no} 页获取失败")
                page_failed = True
                break

            data = payload.get("data") if isinstance(payload, dict) else None
            packages = [
                pkg for pkg in ((data or {}).get("packages") or []) if isinstance(pkg, dict)
            ]
            page_total = _to_int((data or {}).get("total"))
            if page_total is not None:
                total = page_total

            if not packages:
                complete = True
                break

            page_synced = 0
            for package in packages:
                normalized = normalize_package(
                    package, account_id=account.id, user_id=account.user_id, seller_id=seller_id
                )
                if normalized is None:
                    continue
                order_no = normalized["order_no"]
                if order_no in seen_order_nos:
                    continue
                seen_order_nos.add(order_no)
                ordered_ms = _to_epoch_ms(package.get("orderedAt"))
                if ordered_ms is not None:
                    earliest_ms = ordered_ms if earliest_ms is None else min(earliest_ms, ordered_ms)
                try:
                    order = upsert_order(db, normalized, account_id=account.id)
                except Exception:  # noqa: BLE001 - 单条失败不拖垮整页
                    db.rollback()
                    logger.warning("order upsert failed for %s; rolled back", _mask_for_log(order_no))
                    continue
                if order is None:
                    continue
                page_synced += 1
                # 详情补售后单（失败不影响订单落库）。
                if normalized["package_id"]:
                    ok2, _msg2, detail_payload = _fetch_order_detail(adapter, normalized["package_id"])
                    if ok2 and isinstance(detail_payload, dict):
                        fp = (detail_payload.get("data") or {}).get("fulfillment_package") or {}
                        after_sale_rows += upsert_after_sales_from_detail(
                            db, order, fp.get("after_sale_list")
                        )
            db.commit()
            synced += page_synced
            if task is not None:
                _update_progress(db, task, synced=synced, total=total, page=page_no, note=scope_note)

            logger.info(
                "order sync account=%s page=%d synced=%d total=%s (this page=%d)",
                account_id, page_no, synced, total, page_synced,
            )
            # 翻完：本页是最后一页（条数不足一页）或已存够总数。
            if total is not None and synced >= total:
                complete = True
                break
            if len(packages) < page_size:
                complete = True
                break
            page_no += 1
            _sleep_between_pages()

        # 游标推进：只在「翻完」时推进，且推进到「本轮真实枚举到的最早一行」
        # 的下一毫秒（同商品同步的 DEF-045 纪律：游标必须严格越过已见值，
        # 否则下一轮会重扫；千帆订单接口的时间参数是毫秒，直接按毫秒比较）。
        if complete and earliest_ms is not None:
            _save_order_cursor(db, account.id, earliest_ms)

        note = scope_note
        if warnings:
            note += "；" + "；".join(warnings)
        if total is not None and synced and synced < total and not complete:
            note += f"（已同步 {synced}/{total}，未翻完）"
        final_status = sts.STATUS_DONE if (complete or synced) and not page_failed else sts.STATUS_FAILED
        if page_failed or page_cap_hit or max_pages_hit:
            final_status = sts.STATUS_DONE if synced else sts.STATUS_FAILED
        result: dict[str, Any] = {
            "task_id": task.id,
            "synced": synced,
            "total": total,
            "complete": complete,
            "note": note,
            "error": "",
            "after_sales": after_sale_rows,
            "status": final_status,
        }
        _finish(db, task, result, status=final_status)
        return result

    except OrderSyncFailure as exc:
        db.rollback()
        _mark_failed(db, task, exc.message)
        return {**failure, "error": exc.message, "note": exc.code}
    except Exception as exc:  # noqa: BLE001 - 调度器调用，必须吞异常
        db.rollback()
        logger.exception("order sync for account %s failed", account_id)
        message = f"订单同步异常：{type(exc).__name__}: {exc}"
        _mark_failed(db, task, message)
        return {**failure, "error": message}
    finally:
        db.close()


def _update_progress(
    db: Session, task: Task, *, synced: int, total: Optional[int], page: int, note: str = ""
) -> None:
    """进度回写（与商品同步同口径：只覆盖本函数拥有的键，保留其余）。"""
    payload = sts.task_payload(task)
    progress: dict[str, Any] = dict(payload.get("progress") or {})
    progress["synced"] = synced
    if total is not None:
        progress["total"] = total
    else:
        progress.pop("total", None)
    payload["progress"] = progress
    payload["note"] = note
    task.payload = payload
    try:
        db.commit()
    except Exception:  # noqa: BLE001 - 进度回写失败不阻断同步
        db.rollback()
        logger.warning("order sync: progress flush failed at page %s", page)


def _finish(db: Session, task: Task, result: dict[str, Any], *, status: str) -> None:
    """标终态。⚠️ 取消竞态（设计 13 §24.8）：已取消 ⇒ 不覆写 cancelled。"""
    if task is None:
        return
    if task_cancel.is_cancelled(task.id):
        status = sts.STATUS_CANCELLED
        result["note"] = (result.get("note") or "") + "；已被用户取消"
        result["status"] = sts.STATUS_CANCELLED
    task.status = status
    task.finished_at = shanghai_now()
    payload = sts.task_payload(task)
    payload.update(result)
    payload.pop("task_id", None)
    task.payload = payload
    task_cancel.clear(task.id)
    try:
        db.commit()
    except Exception:  # noqa: BLE001 - 标终态本身不能再抛
        db.rollback()
        logger.warning("order sync: could not persist terminal state")


def _mark_failed(db: Session, task: Optional[Task], reason: str) -> None:
    if task is None:
        return
    if task_cancel.is_cancelled(task.id):
        _finish(db, task, {"error": "", "note": "已被用户取消"}, status=sts.STATUS_CANCELLED)
        return
    try:
        task.status = sts.STATUS_FAILED
        task.finished_at = shanghai_now()
        payload = sts.task_payload(task)
        payload["error"] = reason
        task.payload = payload
        db.commit()
    except Exception:  # noqa: BLE001 - 标失败本身不能再抛
        db.rollback()
        logger.warning("order sync: could not mark task failed")


# --------------------------------------------------------------------------- #
# 游标（账号级，存 system_settings；与商品同步的 goods_cursor_ms 同构）
# --------------------------------------------------------------------------- #
_CURSOR_KEY = "order_sync_cursor_ms"


def _load_order_cursor(db: Session, account_id: int) -> int:
    """上次成功同步的最早订单创建时间（**毫秒**），缺值/坏值 ⇒ 0 ⇒ 下一轮走全量。

    存 `system_settings`（用户级配置表），键按**账号归属用户**解析（同限流额度的
    口径，见 `account_owner_id` 的说明：账号属于谁就用谁的配置，不按登录用户）。
    """
    from backend.app.services.settings_service import account_owner_id, find_setting, decode_value

    try:
        owner = account_owner_id(account_id)
        if owner is None:
            return 0
        row = find_setting(db, owner, _CURSOR_KEY)
        if row is None:
            return 0
        return int(decode_value(row.value, row.value_type) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _save_order_cursor(db: Session, account_id: int, cursor_ms: int) -> None:
    """推进游标（**只增不减**：读旧值取 max，防乱序回退）。失败不阻断同步结果。"""
    from backend.app.services.settings_service import (
        account_owner_id,
        create_setting,
        decode_value,
        find_setting,
        update_setting,
    )

    try:
        owner = account_owner_id(account_id)
        if owner is None:
            return
        new_value = int(cursor_ms or 0)
        row = find_setting(db, owner, _CURSOR_KEY)
        if row is None:
            create_setting(
                db,
                user_id=owner,
                key=_CURSOR_KEY,
                value=new_value,
                value_type="int",
                label="订单同步游标（ms）",
                description="上次订单同步枚举到的最早订单创建时间；仅同步服务读写。",
            )
        else:
            old = int(decode_value(row.value, row.value_type) or 0)
            if new_value > old:
                update_setting(db, row, value=new_value)
        db.commit()
    except Exception:  # noqa: BLE001 - 游标写失败不阻断本轮同步结果
        db.rollback()
        logger.warning("order cursor save failed for account %s", account_id)


def _mark_credential_state(db: Session, account: PlatformAccount, message: str, payload: Any) -> None:
    """凭据失效 ⇒ 标记账号千帆态为 invalid（已有订单一行不删，与商品同步同口径）。"""
    from apis.xhs_qianfan_apis import XHS_Qianfan_Apis

    code = (payload or {}).get("code") if isinstance(payload, dict) else None
    if not XHS_Qianfan_Apis.is_credential_error(code):
        return
    from backend.app.services.shop_binding_service import set_shop_state

    set_shop_state(account, qianfan_state="invalid", qianfan_error=message or "千帆登录态失效")
    db.commit()


# --------------------------------------------------------------------------- #
# 读取层（端点用）
# --------------------------------------------------------------------------- #
def mask_sensitive(value: Optional[str]) -> str:
    """展示层脱敏：姓名留姓 + `*`；手机号留前 3 后 4；地址留省市 + `***`。

    ⚠️ **空值原样返回空**（不造 `***`：空与「有但脱敏」是两件事，界面要能区分）。
    """
    if not value:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if any(ch.isdigit() for ch in text) and len(text) >= 7:
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) >= 7:
            return text[:3] + "****" + text[-4:]
    if len(text) <= 2:
        return text[0] + "*"
    return text[0] + "*" * (min(len(text), 4) - 1)


def _shop_context(db: Session, platform_account_id: Optional[int]) -> dict[str, str]:
    """店铺维度上下文（店名 + seller_id）—— 订单列表 / 详情共用。

    ⚠️ ADR-021 后**店铺实体已退场**，「账号 = 店铺」1:1 ⇒ 店名取账号昵称
    （`shop_binding_service.shop_display_name` **唯一实现**，与商品页同源）。
    账号不存在（被删 / 传错 id）⇒ 返回空串（**不造店名**，硬约束⑤）。
    """
    from backend.app.services.shop_binding_service import shop_display_name

    if not platform_account_id:
        return {"shop_name": "", "seller_id": ""}
    account = db.get(PlatformAccount, int(platform_account_id))
    if account is None:
        return {"shop_name": "", "seller_id": ""}
    return {"shop_name": shop_display_name(account), "seller_id": account.seller_id or ""}


def order_summary(
    db: Session,
    order: Order,
    *,
    include_full: bool = False,
    shop_name: str = "",
    seller_id: str = "",
) -> dict[str, Any]:
    """订单 → 列表/详情用的 dict。``include_full=True`` 才给完整履约值。

    ## 店铺维度（2026-10-04 用户口径「订单模块也需要是店铺维度」）

    店铺实体已退场（ADR-021）⇒「账号 = 店铺」1:1，店名取账号昵称
    （`shop_binding_service.shop_display_name` 唯一实现）。`shop_name` /
    `seller_id` 由**调用方批量预取**后传入，不在本函数里逐行查账号
    （列表 N 行 ⇒ N 次查库 = N+1 查询）。
    """
    items = db.scalars(
        select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id.asc())
    ).all()
    shipments = db.scalars(
        select(OrderShipment).where(OrderShipment.order_id == order.id)
    ).all()
    after_sales = db.scalars(
        select(AfterSale).where(AfterSale.order_id == order.id).order_by(AfterSale.id.asc())
    ).all()
    remarks = db.scalars(
        select(OrderRemark).where(OrderRemark.order_id == order.id).order_by(OrderRemark.id.asc())
    ).all()
    full = bool(include_full)
    return {
        "id": order.id,
        "order_no": order.order_no,
        "package_id": order.package_id,
        "platform_account_id": order.platform_account_id,
        #: 店铺维度（设计 16 §7.2；ADR-021 后账号 = 店铺）—— 商品页同口径。
        "shop_name": shop_name,
        "seller_id": seller_id,
        "type_desc": order.type_desc,
        "buyer_nick": order.buyer_nick,
        "receiver_name": order.receiver_name_full if full else mask_sensitive(order.receiver_name_full),
        "receiver_phone": order.receiver_phone_full if full else mask_sensitive(order.receiver_phone_full),
        "receiver_address": order.receiver_address_full if full else mask_sensitive(order.receiver_address_full),
        "receiver_province": order.receiver_province,
        "receiver_city": order.receiver_city,
        "status": order.status,
        "platform_status": order.platform_status,
        "total_amount": _money(order.total_amount),
        "pay_amount": _money(order.pay_amount),
        "freight": _money(order.freight),
        "discount": _money(order.discount),
        "pay_method": order.pay_method,
        "order_source": order.order_source,
        "logistics_company": order.logistics_company,
        "tracking_no": order.tracking_no,
        "created_at": _iso(order.created_at),
        "pay_time": _iso(order.pay_time),
        "ship_time": _iso(order.ship_time),
        "finish_time": _iso(order.finish_time),
        "promise_ship_time": _iso(order.promise_ship_time),
        "synced_at": _iso(order.synced_at),
        "seller_mark": order.seller_mark,
        "items": [
            {
                "id": item.id,
                "item_id": item.item_id,
                "shop_product_id": item.shop_product_id,
                "item_name": item.item_name,
                "item_image": item.item_image,
                "sku": item.sku,
                "quantity": item.quantity,
                "unit_price": _money(item.unit_price),
                "amount": _money(item.amount),
                "refund_status": item.refund_status,
            }
            for item in items
        ],
        "shipments": [
            {
                "id": ship.id,
                "package_id": ship.package_id,
                "logistics_company": ship.logistics_company,
                "tracking_no": ship.tracking_no,
                "ship_time": _iso(ship.ship_time),
                "sign_time": _iso(ship.sign_time),
                "warehouse_desc": ship.warehouse_desc,
            }
            for ship in shipments
        ],
        "after_sales": [
            {
                "id": row.id,
                "after_sale_id": row.after_sale_id,
                "type": row.type,
                "reason": row.reason,
                "status": row.status,
                "amount": _money(row.amount),
                "apply_time": _iso(row.apply_time),
                "handle_time": _iso(row.handle_time),
                "handle_result": row.handle_result,
            }
            for row in after_sales
        ],
        "remarks": [
            {
                "id": row.id,
                "kind": row.kind,
                "content": row.content,
                "created_at": _iso(row.created_at),
                "updated_at": _iso(row.updated_at),
            }
            for row in remarks
        ],
    }


def _money(value: Any) -> Optional[float]:
    return None if value is None else round(float(value), 2)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else value.isoformat(sep=" ")


def list_orders(
    db: Session,
    *,
    user_id: int,
    platform_account_id: Optional[int] = None,
    status: str = "",
    keyword: str = "",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    include_full: bool = False,
) -> dict[str, Any]:
    """本地台账查询（筛选 / 排序 / 分页全在本地 SQL，与商品列表同口径）。"""
    statement = select(Order).where(Order.user_id == user_id)
    if platform_account_id:
        statement = statement.where(Order.platform_account_id == int(platform_account_id))
    if status and status in ORDER_STATUSES:
        statement = statement.where(Order.status == status)
    if keyword:
        like = f"%{keyword.strip()}%"
        item_ids = [
            row[0]
            for row in db.execute(
                select(OrderItem.order_id).where(
                    (OrderItem.item_name.like(like)) | (OrderItem.sku.like(like))
                )
            ).all()
        ]
        statement = statement.where(
            (Order.order_no.like(like))
            | (Order.buyer_nick.like(like))
            | (Order.tracking_no.like(like))
            | (Order.id.in_(item_ids) if item_ids else false())
        )
    if date_from:
        statement = statement.where(Order.created_at >= _parse_date(date_from))
    if date_to:
        statement = statement.where(Order.created_at <= _parse_date(date_to, end_of_day=True))

    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    # ⚠️ **先归一参数再回显**（2026-10-04 实测补）：原实现只把 `page_size` 收窄在
    # `LIMIT` 上，返回体里回的却是**原始入参** ⇒ 前端传 100000 时页面显示
    # 「每页 100000 条」而实际只拿到 100 条（分页器算出的总页数也是错的）。
    # 口径：返回体回显**服务端实际采用值**，调用方据此渲染分页器。
    page = max(int(page or 1), 1)
    page_size = max(min(int(page_size or 20), 100), 1)
    rows = db.scalars(
        statement.order_by(Order.created_at.desc())
        .offset(max((page - 1) * page_size, 0))
        .limit(page_size)
    ).all()
    #: 店铺维度上下文（批量预取一次，不在 order_summary 里逐行查账号）。
    shop_ctx = _shop_context(db, platform_account_id)
    return {
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "shop_name": shop_ctx.get("shop_name", ""),
        "seller_id": shop_ctx.get("seller_id", ""),
        "platform_account_id": int(platform_account_id) if platform_account_id else None,
        "items": [
            order_summary(
                db,
                row,
                include_full=include_full,
                shop_name=shop_ctx.get("shop_name", ""),
                seller_id=shop_ctx.get("seller_id", ""),
            )
            for row in rows
        ],
    }


def upsert_remark(
    db: Session,
    *,
    user_id: int,
    order: Order,
    content: str,
    kind: str = ORDER_REMARK_KIND_ORDER,
) -> Optional[OrderRemark]:
    """写一条**本地**订单备注（设计 16 §7.2）。

    ⚠️ **这是本地标记，不是平台写操作** —— 平台的「卖家备注」写接口未授权
    （硬约束②只读 + 用户 2026-09-19 授权清单里只有「删除笔记」一个写操作）。
    用户在这里写的备注**只存在于本机台账**，不会同步到千帆。

    ## 口径：一条订单 + 一种 kind ⇒ 一条备注（**覆盖，不追历史**）

    设计 16 §7.2 的表形状是「订单备注」而非「备注流水」；页面也是一个
    输入框（当前值 + 保存）。若改成 append，页面就得处理「显示哪一条」，
    且用户改三个字会产生三条记录 —— 不是这张表要表达的东西。

    `content` 为空 ⇒ 删除该备注（用户清空输入框 = 不备注，不是存一条空串；
    空串会让页面显示一个「有备注」的空行）。
    """
    row = db.scalar(
        select(OrderRemark).where(
            OrderRemark.order_id == order.id, OrderRemark.kind == kind
        )
    )
    text = (content or "").strip()
    if not text:
        if row is not None:
            db.delete(row)
            db.flush()
        return None
    if row is None:
        row = OrderRemark(
            order_id=order.id, user_id=user_id, kind=kind, content=text
        )
        db.add(row)
    else:
        row.content = text
    db.flush()
    return row


#: 导出 CSV 的列（**顺序固定** —— 用户拿去做透视/对账，列序变了脚本就崩）。
#: 收件人三列给**完整值**（设计 16 §7.2：履约场景需要原值，防护在展示层）。
EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("订单号", "order_no"),
    ("状态", "status"),
    ("买家昵称", "buyer_nick"),
    ("收件人", "receiver_name"),
    ("手机号", "receiver_phone"),
    ("收货地址", "receiver_address"),
    ("商品", "_items_text"),
    ("应付金额", "pay_amount"),
    ("订单金额", "total_amount"),
    ("运费", "freight"),
    ("优惠", "discount"),
    ("支付方式", "pay_method"),
    ("物流公司", "logistics_company"),
    ("快递单号", "tracking_no"),
    ("下单时间", "created_at"),
    ("付款时间", "pay_time"),
    ("发货时间", "ship_time"),
    ("完成时间", "finish_time"),
)


def export_filename() -> str:
    """导出文件名（带时间戳，避免两次导出互相覆盖）。"""
    return f"xhs_orders_{shanghai_now().strftime('%Y%m%d_%H%M%S')}.csv"


def _csv_cell(value: Any) -> str:
    """CSV 单元格 -> **只做公式注入防护**，引号/逗号交给 csv.writer。

    **不要在这里自己加引号**（2026-10-04 实测踩到）：csv.writer 已经按
    RFC 4180 处理引号与分隔符；这里再裹一层 => 值被双重转义，Excel 打开
    时多出可见引号。同一件事只有一个实现（DEF-044）：转义归 csv 模块，
    本函数只管安全前缀。

    **前导 = + - @ 要加单引号前缀**：Excel / WPS 会把它们当公式执行
    （CSV 注入）。订单号、买家昵称是平台来的外部数据，商家点开导出文件时
    不该有被当公式执行的风险。纯数字（如 -12.5）不算公式，放行。
    """
    if value is None:
        return ""
    text = str(value)
    head = text[:1]
    if head in ("=", "+", "@") or (head == "-" and not text[1:2].isdigit()):
        return "'" + text
    return text


def orders_to_csv(rows: list[dict[str, Any]]) -> str:
    """订单摘要 list → CSV 文本（含 BOM，Excel 打开不乱码）。

    ⚠️ 用 `csv` 模块而不是手工拼 `join`：买家昵称 / 商品名里的逗号、引号、
    换行都会破坏手工拼接的列结构（实测平台昵称里有带逗号的）。
    """
    import csv as _csv
    import io as _io

    buf = _io.StringIO()
    writer = _csv.writer(buf, lineterminator="\r\n")
    writer.writerow([label for label, _ in EXPORT_COLUMNS])
    for row in rows:
        line = []
        for _, key in EXPORT_COLUMNS:
            if key == "_items_text":
                items = row.get("items") or []
                text = " | ".join(
                    f"{i.get('item_name') or ''}"
                    + (f"（{i.get('sku')}）" if i.get("sku") else "")
                    + f" x{i.get('quantity') or 0}"
                    for i in items
                )
                line.append(text)
            else:
                line.append(row.get(key))
        writer.writerow([_csv_cell(c) for c in line])
    # BOM：Excel 认 UTF-8 BOM，否则中文列名乱码
    return "﻿" + buf.getvalue()


def _parse_date(value: str, *, end_of_day: bool = False) -> datetime:
    """`YYYY-MM-DD` → 当天起点 / 终点（坏值退到极值，不抛）。"""
    from datetime import timedelta

    try:
        day = datetime.strptime(value.strip()[:10], "%Y-%m-%d")
    except ValueError:
        return datetime.max if end_of_day else datetime.min
    return day + timedelta(days=1) - timedelta(seconds=1) if end_of_day else day


def order_metrics(db: Session, *, user_id: int, platform_account_id: Optional[int] = None) -> dict[str, Any]:
    """订单监控指标（设计 16 §7.7，全部本地聚合，零平台请求）。

    口径（缺值不造 0 之外，另有两处必须写清）：
    * **GMV** = `pay_amount` 之和（实付，不是 `total_amount` 商品总额）；
    * **退款率** = 有 `after_sales` 且 `amount>0` 的订单数 / 已完成订单数；
    * **客单价** = GMV / 已支付订单数（分母不含「待付款」）。
    """
    statement = select(Order).where(Order.user_id == user_id)
    if platform_account_id:
        statement = statement.where(Order.platform_account_id == int(platform_account_id))
    orders = db.scalars(statement).all()

    paid = [o for o in orders if o.pay_amount is not None]
    gmv = sum(float(o.pay_amount) for o in paid)  # type: ignore[arg-type]
    finished = [o for o in orders if o.status == ORDER_STATUS_MAP[7]]
    aftersale_order_ids = {
        row.order_id
        for row in db.scalars(
            select(AfterSale).where(AfterSale.user_id == user_id)
        ).all()
    }
    refunded = [o for o in orders if o.id in aftersale_order_ids]
    shipped = [o for o in orders if o.ship_time is not None and o.pay_time is not None]
    if shipped:
        ship_hours = [
            (o.ship_time - o.pay_time).total_seconds() / 3600.0  # type: ignore[operator]
            for o in shipped
        ]
        avg_ship_hours = round(sum(ship_hours) / len(ship_hours), 1)
    else:
        avg_ship_hours = None
    return {
        "order_count": len(orders),
        "gmv": round(gmv, 2),
        "avg_order_value": round(gmv / len(paid), 2) if paid else None,
        "paid_count": len(paid),
        "finished_count": len(finished),
        "refund_count": len(refunded),
        "refund_rate": round(len(refunded) / len(finished), 4) if finished else None,
        "avg_ship_hours": avg_ship_hours,
        "by_status": {
            status: sum(1 for o in orders if o.status == status) for status in ORDER_STATUSES
        },
    }
