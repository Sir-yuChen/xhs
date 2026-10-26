"""订单二期**写操作**服务层（设计 16 §7.4，M7 方向 6）。

⚠️ **真实平台写操作、不可撤回**。本模块是**发货 / 售后审核**的执行体：
- 发货：`POST /api/edith/fulfillment/single_online_delivery`（端点存在性 2026-10-04 已证实，§7.4.1）
- 售后：`POST /api/edith/after-sales/audit/`（同上）

## 六条硬约束（每条都有守卫）

1. **显式确认 + 二次确认**：请求体必须带 `confirmed=true`（一层）**且**
   `confirm_token` 与本次请求体的指纹一致（二层）—— 少任一层 ⇒ 400，**一次都不打平台**。
2. **门禁在写之前**：非目标状态（如「已完成」的单去发货）⇒ 400 `ORDER_NOT_SHIPPABLE`，
   **一次都不打平台**（不是「打了再说不行」）。
3. **绝不自动触发**：不进定时任务、不进工作流 —— 只有用户在本页点击才可能到达这里。
4. **失败不落半截数据**：调平台异常 ⇒ 先 `rollback()` 再记失败，状态与物流行都不变。
5. **批量逐条独立**：一条失败不拖垮整批，回执**分层**（`succeeded[]` / `failed[]`）。
6. **不借会话**：只认本账号自己的 `ark` 令牌（**唯一取票入口**
   `shop_binding_service.qianfan_cookies_for_account`）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from backend.app.core.time import shanghai_now
#: ⚠️ 设计 75 S5（2026-10-27）：只 import **状态常量**（纯函数侧）；`AfterSale` /
#: `Order` / `OrderShipment` 三个 ORM 类随 `n8o9p0q1r2s3` 整表 DROP。
#: 写链路的入参改为「订单引用」`OrderRef`（含 `order_no` / `package_id` / `status`），
#: 由端点层从**平台实时**数据构造（见 `qianfan/_shared.py::_owned_order`）。
from backend.app.models.order import (
    AFTERSALE_STATUS_AGREED,
    AFTERSALE_STATUS_PENDING,
    AFTERSALE_STATUS_REJECTED,
    ORDER_STATUS_PENDING_SHIP,
)
from typing import Protocol


class OrderRefLike(Protocol):
    """写链路需要的订单字段（结构类型，不绑定任何 ORM 类）。

    `order_no` / `package_id` / `status` 三个字段来自**平台实时**的订单行
    （`rt.list_orders` 的归一化结果）。
    """

    order_no: str
    package_id: str
    status: str

logger = logging.getLogger(__name__)

#: 单次批量发货上限（与 `items/delete` 的批量纪律同型：去重保序 + 空 400 + 超限 400）。
MAX_BATCH_SHIP = 50

#: 批量上限的**前端同值**（页面禁用条件与后端同一个数，不许各写一份）。
BATCH_SHIP_LIMIT = MAX_BATCH_SHIP


class OrderWriteError(RuntimeError):
    """订单写操作的结构化失败（带 `code`，路由层映射成 400/404）。"""

    def __init__(self, message: str, *, code: str = "ORDER_WRITE_FAILED", **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


def _fingerprint(payload: dict[str, Any]) -> str:
    """请求体指纹（二次确认凭据）—— 内容变了指纹就变，防「确认的是旧动作」。

    只取业务字段（去掉 `confirmed` / `confirm_token` 自身），排序后哈希 ⇒
    同一组内容稳定同值，改一个字段立刻不同。
    """
    material = {k: v for k, v in payload.items() if k not in ("confirmed", "confirm_token")}
    blob = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def confirm_token_for(payload: dict[str, Any]) -> str:
    """给前端算「预览态」的二次确认凭据（前端拿到后原样回传）。"""
    return _fingerprint(payload)


def require_double_confirm(payload: dict[str, Any], *, confirmed: bool, confirm_token: str) -> None:
    """**两层确认校验**（唯一实现，发货与售后共用）。

    * 第一层 `confirmed is True`（用户勾了「我知道这是真实写操作」）；
    * 第二层 `confirm_token == 本次请求体的指纹`（用户确认的**就是这一批**，
      不是改过字段后的另一批）。

    任一层不过 ⇒ 抛 `OrderWriteError`（`ORDER_CONFIRM_REQUIRED` /
    `ORDER_CONFIRM_MISMATCH`），**一次都不打平台**。
    """
    if not confirmed:
        raise OrderWriteError(
            "这是真实平台写操作、不可撤回，需要显式确认后才会执行。",
            code="ORDER_CONFIRM_REQUIRED",
        )
    expected = _fingerprint(payload)
    if (confirm_token or "") != expected:
        raise OrderWriteError(
            "确认凭据与本次请求内容不一致（内容可能已被修改），请重新确认。",
            code="ORDER_CONFIRM_MISMATCH",
        )


def ship_confirm_material(order: OrderRefLike, *, company_code: str, tracking_no: str) -> dict[str, Any]:
    """**单笔发货的确认素材**（预览与写端点的 token 都基于它，**唯一实现**）。

    ⚠️ 预览与写**必须用同一份素材**算指纹 —— 各算各的 ⇒ 预览下发的 token
    永远过不了写端点的校验（实测踩过：两处形状不同，功能完全不可用）。
    """
    return {
        "kind": "ship",
        "order_no": str(order.order_no or ""),
        "package_id": str(order.package_id or ""),
        "company_code": str(company_code or ""),
        "tracking_no": str(tracking_no or ""),
    }


def batch_ship_confirm_material(
    specs: list[dict[str, Any]],
) -> dict[str, Any]:
    """**批量发货的确认素材**（预览与写共用；`specs` 每项含 order_no/company_code/tracking_no）。"""
    return {
        "kind": "ship_batch",
        "orders": [
            {
                "order_no": str(item.get("order_no") or ""),
                "package_id": str(item.get("package_id") or ""),
                "company_code": str(item.get("company_code") or ""),
                "tracking_no": str(item.get("tracking_no") or ""),
            }
            for item in specs
        ],
    }


def after_sale_confirm_material(
    after_sale: Any, *, action: str, reject_reason: str
) -> dict[str, Any]:
    """**售后审核的确认素材**（预览与写共用）。"""
    return {
        "kind": "after_sale",
        "after_sale_id": str(after_sale.after_sale_id or ""),
        "audit_type": "reject" if action == AFTERSALE_ACTION_REJECT else "agree",
        "reject_reason": str(reject_reason or "").strip(),
    }


def _ship_payload_for(order: OrderRefLike, *, company_code: str, tracking_no: str) -> dict[str, Any]:
    """发货**请求体**（⚠️ 字段名按页面推断，**首发真机核对**，见设计 16 §7.4.1）。"""
    return {
        "package_id": str(order.package_id or ""),
        "express_company_code": str(company_code or ""),
        "express_no": str(tracking_no or ""),
    }


def ship_order(
    db: Session,
    *,
    adapter: Any,
    order: OrderRefLike,
    company_code: str,
    tracking_no: str,
    confirmed: bool,
    confirm_token: str,
) -> dict[str, Any]:
    """**发货单个订单**（设计 16 §7.4 A）—— 两层确认 + 门禁 + 落库。

    顺序**刻意为**：确认校验 → 状态门禁 → 打平台 → 落库。
    前两步不过**一次都不打平台**（验收标准：「非目标状态一次都不许打平台」）。
    """
    material = ship_confirm_material(order, company_code=company_code, tracking_no=tracking_no)
    require_double_confirm(material, confirmed=confirmed, confirm_token=confirm_token)
    payload = _ship_payload_for(order, company_code=company_code, tracking_no=tracking_no)
    return _ship_one(db, adapter=adapter, order=order, payload=payload)


def _ship_one(db: Session, *, adapter: Any, order: OrderRefLike, payload: dict[str, Any]) -> dict[str, Any]:
    """发货执行体（**不含确认校验** —— 调用方已校验；批量路径共用它，DEF-044）。"""
    if order.status != ORDER_STATUS_PENDING_SHIP:
        raise OrderWriteError(
            f"这笔订单当前状态是「{order.status or '未知'}」，只有「待发货」的订单能发货。",
            code="ORDER_NOT_SHIPPABLE",
            current_status=order.status,
        )
    if not payload.get("package_id") or not payload.get("express_no"):
        raise OrderWriteError("发货需要包裹 ID 与运单号。", code="ORDER_SHIP_FIELDS_REQUIRED")

    ok, message, result = adapter.single_online_delivery(payload)
    if not ok:
        # ⚠️ 失败**不落半截数据**：状态与物流行都不变，原因原样带出可重试。
        db.rollback()
        raise OrderWriteError(
            f"平台拒绝了这次发货：{message}", code="ORDER_SHIP_PLATFORM_REJECTED"
        )

    #: ⚠️ 设计 75 S5（2026-10-27）：**不再落物流行** —— `order_shipments` 表已 DROP，
    #: 订单域全实时（物流信息从平台详情的 `shipments[]` 读）。
    #: 「状态推进交给重新同步」这条纪律也随之退场：本来就是靠同步把平台状态读回来，
    #: 现在每次看订单都是平台当下值，不存在「本地与平台打架」。
    db.commit()
    return {
        "order_no": order.order_no,
        "package_id": payload["package_id"],
        "tracking_no": payload["express_no"],
        "platform_message": message,
        "result": result if isinstance(result, dict) else None,
    }


def ship_orders_batch(
    db: Session,
    *,
    adapter_for: Callable[[OrderRefLike], Any],
    orders: list[OrderRefLike],
    specs_by_order_no: dict[str, dict[str, str]],
    confirmed: bool,
    confirm_token: str,
) -> dict[str, Any]:
    """**批量发货**：逐条独立，回执**分层**（设计 16 §7.4 A.6）。

    ## 二次确认的口径（**整批一次确认**，不是逐条）

    `confirm_token` = **整批内容**的指纹（每条各自的 `order_no` + 物流 + 运单号）。
    用户确认的是「这一批」；改掉任意一条 ⇒ 指纹变 ⇒ `ORDER_CONFIRM_MISMATCH`。
    批量内部**逐条**走 `_ship_one`（同一份门禁 + 落库，DEF-044），但**不再重复确认**
    （已由本函数校验过整批）。

    一条失败不拖垮整批 —— `succeeded[]` / `failed[]` 各自带原因。
    """
    material = batch_ship_confirm_material(
        [
            {
                "order_no": o.order_no,
                "package_id": o.package_id,
                **(specs_by_order_no.get(o.order_no) or {}),
            }
            for o in orders
        ]
    )
    require_double_confirm(material, confirmed=confirmed, confirm_token=confirm_token)

    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for order in orders:
        spec = specs_by_order_no.get(order.order_no) or {}
        try:
            result = _ship_one(
                db,
                adapter=adapter_for(order),
                order=order,
                payload=_ship_payload_for(
                    order,
                    company_code=str(spec.get("company_code") or ""),
                    tracking_no=str(spec.get("tracking_no") or ""),
                ),
            )
            succeeded.append(result)
        except OrderWriteError as error:
            db.rollback()
            row: dict[str, Any] = {
                "order_no": order.order_no,
                "code": error.code,
                "message": str(error),
            }
            if "current_status" in error.extra:
                row["current_status"] = error.extra["current_status"]
            failed.append(row)
        except Exception as error:  # noqa: BLE001 - 单条异常不拖垮整批
            db.rollback()
            logger.warning("ship failed for order %s", order.order_no)
            failed.append(
                {"order_no": order.order_no, "code": "ORDER_SHIP_FAILED", "message": str(error)}
            )
    return {
        "requested": len(orders),
        "succeeded": succeeded,
        "failed": failed,
        "succeeded_count": len(succeeded),
        "failed_count": len(failed),
    }


# --------------------------------------------------------------------------- #
# 售后审核
# --------------------------------------------------------------------------- #

#: 审核动作（与页面两个按钮一一对应）。
AFTERSALE_ACTION_AGREE = "agree"
AFTERSALE_ACTION_REJECT = "reject"
AFTERSALE_ACTIONS = (AFTERSALE_ACTION_AGREE, AFTERSALE_ACTION_REJECT)


def _audit_payload_for(after_sale: Any, *, action: str, reject_reason: str) -> dict[str, Any]:
    """售后审核请求体（⚠️ 字段名推断，**首发真机核对**）。"""
    return {
        "after_sale_id": str(after_sale.after_sale_id or ""),
        "audit_type": "reject" if action == AFTERSALE_ACTION_REJECT else "agree",
        "reject_reason": str(reject_reason or ""),
    }


def handle_after_sale(
    db: Session,
    *,
    adapter: Any,
    after_sale: Any,
    action: str,
    reject_reason: str,
    confirmed: bool,
    confirm_token: str,
) -> dict[str, Any]:
    """**售后审核**（同意 / 拒绝；设计 16 §7.4 B）。

    顺序与发货同：确认 → 门禁（必须 `待处理`）→ 打平台 → 落库。
    """
    if action not in AFTERSALE_ACTIONS:
        raise OrderWriteError(
            f"不支持的售后处理动作：{action}", code="ORDER_AFTERSALE_BAD_ACTION"
        )
    if action == AFTERSALE_ACTION_REJECT and not (reject_reason or "").strip():
        # 拒绝必须给理由（平台侧要求，也避免「无理由拒绝」这种说不清的账）。
        raise OrderWriteError("拒绝售后必须填写理由。", code="ORDER_AFTERSALE_REASON_REQUIRED")

    material = after_sale_confirm_material(
        after_sale, action=action, reject_reason=reject_reason
    )
    require_double_confirm(material, confirmed=confirmed, confirm_token=confirm_token)
    payload = _audit_payload_for(after_sale, action=action, reject_reason=reject_reason)

    if after_sale.status != AFTERSALE_STATUS_PENDING:
        raise OrderWriteError(
            f"这条售后单当前是「{after_sale.status or '未知'}」，只有「待处理」的能审核。",
            code="ORDER_AFTERSALE_NOT_PENDING",
            current_status=after_sale.status,
        )

    ok, message, result = adapter.after_sale_audit(payload)
    if not ok:
        db.rollback()
        raise OrderWriteError(
            f"平台拒绝了这次售后处理：{message}", code="ORDER_AFTERSALE_PLATFORM_REJECTED"
        )

    after_sale.status = (
        AFTERSALE_STATUS_REJECTED if action == AFTERSALE_ACTION_REJECT else AFTERSALE_STATUS_AGREED
    )
    after_sale.handle_time = shanghai_now()
    after_sale.handle_result = (
        (reject_reason or "").strip() if action == AFTERSALE_ACTION_REJECT else "已同意"
    )
    db.flush()
    db.commit()
    return {
        "after_sale_id": after_sale.after_sale_id,
        "status": after_sale.status,
        "handle_result": after_sale.handle_result,
        "platform_message": message,
    }
