from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

# --------------------------------------------------------------------------- #
# 订单状态枚举（设计 16 §7.3 / §7.5）
#
# ⚠️ **唯一真相源**：状态映射表驱动（`ORDER_STATUS_MAP`），业务代码只许引用
# 这里的常量，不许散落 if 判平台码（验收标准：「状态映射逐值钉死、表驱动」）。
# --------------------------------------------------------------------------- #

ORDER_STATUS_PENDING_PAYMENT = "待付款"
ORDER_STATUS_PENDING_SHIP = "待发货"
ORDER_STATUS_SHIPPED = "待收货"
ORDER_STATUS_FINISHED = "已完成"
ORDER_STATUS_IN_AFTERSALE = "售后中"
ORDER_STATUS_CLOSED = "已关闭"

#: 本地全部合法状态（筛选档与展示共用这一份，不各写一份枚举）。
ORDER_STATUSES = (
    ORDER_STATUS_PENDING_PAYMENT,
    ORDER_STATUS_PENDING_SHIP,
    ORDER_STATUS_SHIPPED,
    ORDER_STATUS_FINISHED,
    ORDER_STATUS_IN_AFTERSALE,
    ORDER_STATUS_CLOSED,
)

#: 千帆订单状态码 → 本地状态（设计 16 §7.3 的映射表，**逐值钉死**）。
#:
#: 真机证据（2026-10-04，账号 8 的 3 笔真实订单 + 千帆订单页 status 枚举）：
#:   `998` = 已取消；`7` = 已完成。
#: 其余状态码取自订单页 `order_stats` 请求里的 `status_list`（平台筛选档，
#: 与页面 tab 同源）：`1` 待付款、`2/21/26` 待发货、`4/5/55` 已发货待收货、
#: `6/65` 已完成、`998` 已关闭/已取消。售后态由 `after_sale_status` 决定
#: （见 `_resolve_status` —— 售后中优先于发货态，因为「已发货但被申请退款」
#: 在业务上归「售后中」）。
ORDER_STATUS_MAP: dict[int, str] = {
    1: ORDER_STATUS_PENDING_PAYMENT,
    2: ORDER_STATUS_PENDING_SHIP,
    21: ORDER_STATUS_PENDING_SHIP,
    26: ORDER_STATUS_PENDING_SHIP,
    4: ORDER_STATUS_SHIPPED,
    5: ORDER_STATUS_SHIPPED,
    55: ORDER_STATUS_SHIPPED,
    6: ORDER_STATUS_FINISHED,
    65: ORDER_STATUS_FINISHED,
    7: ORDER_STATUS_FINISHED,
    998: ORDER_STATUS_CLOSED,
}

#: 千帆售后状态码 → 是否处于「售后中」（`after_sale_status` 字段）。
#: 真机：`1` = 无售后、`2` = 售后中（待商家处理）、`3` = 售后完成、`4` = 售后关闭。
#: `2` 是唯一「正在售后」的态；`3/4` 是终态（订单本身仍按发货态归类）。
AFTERSALE_IN_PROGRESS_CODES = (2,)

#: 售后单状态（`after_sales.status`，设计 16 §7.2）。
AFTERSALE_STATUS_PENDING = "待处理"
AFTERSALE_STATUS_AGREED = "已同意"
AFTERSALE_STATUS_REJECTED = "已拒绝"
AFTERSALE_STATUS_FINISHED = "已完成"
AFTERSALE_STATUS_CANCELLED = "已取消"

#: 备注类型（设计 16 §7.2 `order_remarks.kind`）。
ORDER_REMARK_KIND_ORDER = "订单"
ORDER_REMARK_KIND_PACKAGE = "包裹"


def resolve_order_status(platform_status: object, after_sale_status: object) -> str:
    """千帆状态码 + 售后码 → 本地订单状态（**唯一实现**，表驱动）。

    售后中优先于发货态：一笔已发货但买家申请了退款的订单，经营上归「售后中」
    而不是「已发货」（设计 16 §7.5 的状态机：任何态都能进售后中）。

    未知状态码 → 空串（缺值口径：**不猜、不造**，展示层把空状态渲染成 `-`）。
    """
    try:
        after_sale = int(after_sale_status)
    except (TypeError, ValueError):
        after_sale = -1
    if after_sale in AFTERSALE_IN_PROGRESS_CODES:
        return ORDER_STATUS_IN_AFTERSALE
    try:
        code = int(platform_status)
    except (TypeError, ValueError):
        return ""
    return ORDER_STATUS_MAP.get(code, "")


class Order(Base):
    """千帆订单主表（设计 16 §7.2，M6 方向 6 · 一期只读）。

    **归属键 = `platform_account_id`**（ADR-021：所有数据以小红书账号做隔离，
    与 `shop_products` 同口径）。`shop_id` 列保留平台侧卖家 ID（`seller_id`）
    的文本形态，仅供展示与排障，**不参与隔离**。

    **履约数据口径（2026-09-29 已定）**：收货地址 / 收件人姓名 / 手机号属
    发货履约必要数据，且商家在千帆后台本就可见 ⇒ **完整值落库**（本地单机
    SQLite、数据不出商家机器）；防护放在**展示层**——界面默认脱敏，完整值仅
    发货 / 打单 / 导出履约场景使用。落库列名带 `_full` 后缀以明示这一点。
    """

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("platform_account_id", "order_no", name="uq_orders_account_order_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    #: 指向 `platform_accounts.id`（**隔离键**，与 `shop_products` 同口径）。
    platform_account_id: Mapped[int] = mapped_column(
        ForeignKey("platform_accounts.id"), index=True, nullable=False
    )
    #: 千帆卖家 ID（`seller_id`，十六进制串）。展示与排障用，**不参与隔离**。
    shop_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    #: **平台订单号（`orderId`）—— 幂等键**（同步 upsert 判重用它）。
    order_no: Mapped[str] = mapped_column(String(64), index=True)
    #: 平台包裹 ID（`packageId`，取详情/物流用它，一个订单一个主包裹）。
    package_id: Mapped[str] = mapped_column(String(64), default="")
    #: 订单类型描述（`typeDesc`：现货订单 / 预售订单…）。
    type_desc: Mapped[str] = mapped_column(String(64), default="")
    #: 买家昵称（`userInfo.nickName`）。
    buyer_nick: Mapped[str] = mapped_column(String(128), default="")
    #: 买家平台 user_id（`userId`，虚拟号订单是脱敏串）。
    buyer_user_id: Mapped[str] = mapped_column(String(64), default="")
    #: 收件人姓名（**完整值**，界面脱敏展示；`user_info_visible=false` 时平台给的是 `及*` 形态）。
    receiver_name_full: Mapped[str] = mapped_column(String(128), default="")
    #: 收件人手机（**完整值**；平台对虚拟号订单只给 `184***3034` 形态，原样存）。
    receiver_phone_full: Mapped[str] = mapped_column(String(64), default="")
    #: 省份 / 城市（地域分析用，千帆 `userInfo.province / city`）。
    receiver_province: Mapped[str] = mapped_column(String(64), default="")
    receiver_city: Mapped[str] = mapped_column(String(64), default="")
    #: 收货地址（**完整值**，履约必要；平台默认脱敏 `***` 尾段也原样存）。
    receiver_address_full: Mapped[str] = mapped_column(Text, default="")
    #: 订单状态（本地枚举，取值见 `ORDER_STATUSES`；空 = 平台给了未知码，不猜）。
    status: Mapped[str] = mapped_column(String(32), default="", index=True)
    #: 千帆状态码原文（排障用：状态码变了时能看出平台给了什么）。
    platform_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: 千帆售后码原文（同上）。
    platform_after_sale_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: 商品总额（`totalOrderAmount`，元）。
    total_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    #: 实付金额（`actualPaid`，元）。
    pay_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    #: 运费（`shippingFee`，元）。
    freight: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    #: 优惠（`redDiscountAmount + sellerDiscountAmount`，元）。
    discount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    #: 支付方式（`pay_unit_lite.payment_name`：微信 / 支付宝…）。
    pay_method: Mapped[str] = mapped_column(String(32), default="")
    #: 订单来源（`send_from`：`001` 笔记 / 其它为其它渠道；缺值留空不猜）。
    order_source: Mapped[str] = mapped_column(String(32), default="")
    #: 物流公司（`express_company_name`）。
    logistics_company: Mapped[str] = mapped_column(String(64), default="")
    #: 运单号（`express_no`）。
    tracking_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    #: 下单时间（`orderedAt`）。
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True, default=shanghai_now)
    #: 支付时间（`paidAt`）。
    pay_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: 发货时间（`shippedAt`）。
    ship_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: 完成时间（`finishedAt`）。
    finish_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: 承诺最晚发货时间（`promiseLastShipAt`，发货时效监控用）。
    promise_ship_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: 平台最近更新时间（`updatedAt`）。
    platform_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: 最后同步时间（同步任务写）。
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    #: 卖家备注（`seller_mark_info.seller_mark_note`，同步时顺带落）。
    seller_mark: Mapped[str] = mapped_column(Text, default="")


class OrderItem(Base):
    """订单明细（设计 16 §7.2，SKU 级）。"""

    __tablename__ = "order_items"
    __table_args__ = (
        UniqueConstraint("order_id", "sku_id", name="uq_order_items_order_sku"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    #: 关联本地商品（`shop_products.item_id` 匹配 `skus[].itemId`；删商品留 NULL，快照保留）。
    shop_product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("shop_products.id"), nullable=True
    )
    #: 千帆商品 ID（`skus[].itemId`）。
    item_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    #: 商品名快照（`skuName` 或 `displayName`）。
    item_name: Mapped[str] = mapped_column(Text, default="")
    #: 商品主图（`skus[].image`）。
    item_image: Mapped[str] = mapped_column(Text, default="")
    #: SKU ID（幂等键之一）。
    sku_id: Mapped[str] = mapped_column(String(64), default="")
    #: 规格（`skuSpecification` / `specification`）。
    sku: Mapped[str] = mapped_column(String(255), default="")
    #: 数量（`skuQuantity`）。
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    #: 单价（`skuSoldPrice` / `soldPrice`，元）。
    unit_price: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    #: 小计（`skuTotalPaidAmount`，元）。
    amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    #: 退款状态（SKU 级 `afterSaleStatusDesc`：未退 / 部分退 / 全额退；空 = 无售后）。
    refund_status: Mapped[str] = mapped_column(String(64), default="")


class OrderShipment(Base):
    """订单物流（设计 16 §7.2）。一个订单可能有多个包裹，一行一个包裹。"""

    __tablename__ = "order_shipments"
    __table_args__ = (
        UniqueConstraint("order_id", "package_id", name="uq_order_shipments_order_package"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    #: 千帆包裹 ID（`packageId`，形如 `P803984680367200221`）。
    package_id: Mapped[str] = mapped_column(String(64), default="")
    #: 物流公司（`express_company_name`）。
    logistics_company: Mapped[str] = mapped_column(String(64), default="")
    #: 物流公司编码（`express_company_code`，发货接口用它）。
    logistics_company_code: Mapped[str] = mapped_column(String(64), default="")
    #: 运单号（`express_no`）。
    tracking_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    #: 发货时间（`shippedAt` / `latest_shipped_at`）。
    ship_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: 签收时间（订单完成时间 `finishedAt`，平台无独立签收字段）。
    sign_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: 仓库描述（`warehouseDesc`）。
    warehouse_desc: Mapped[str] = mapped_column(String(128), default="")


class AfterSale(Base):
    """售后单（设计 16 §7.2）。来源：订单详情的 `after_sale_list` 与售后工作台。"""

    __tablename__ = "after_sales"
    __table_args__ = (
        UniqueConstraint("order_id", "after_sale_id", name="uq_after_sales_order_aftersale"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    #: 千帆售后单 ID（幂等键；从订单详情的 `after_sale_list` 取）。
    after_sale_id: Mapped[str] = mapped_column(String(64), default="")
    #: 售后类型（`type`：仅退款 / 退货退款 / 换货，平台原文）。
    type: Mapped[str] = mapped_column(String(64), default="")
    #: 售后原因（`reason`）。
    reason: Mapped[str] = mapped_column(Text, default="")
    #: 状态（本地枚举：待处理 / 已同意 / 已拒绝 / 已完成 / 已取消）。
    status: Mapped[str] = mapped_column(String(32), default="", index=True)
    #: 退款金额（`refund_amount` / `amount`，元）。
    amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    #: 申请时间（`apply_time` / `created_at`）。
    apply_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: 处理时间（`handle_time`）。
    handle_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: 处理结果 / 拒绝理由（`handle_result`）。
    handle_result: Mapped[str] = mapped_column(Text, default="")
    #: 最近同步时间。
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)


# ⚠️ **已退场**（设计 75，2026-10-27）：订单备注整条移除。
#
# 用户口径（2026-10-26）：「订单备注不保留直接移除」。
#
# 为什么删得掉：备注是本方案里**唯一一处**「平台不存在但我方要留」的订单数据。
# 用户决定不留 ⇒ 订单域变成「平台有什么就是什么」，本地零副本。
# 迁移 `k1l2m3n4o5p6` DROP 该表；ORM 类必须一起删 —— 留着 `create_all()`
# 会把已 drop 的表建回来（v2.0 删 `model_configs` 时踩过同一个坑）。
#
# 原声明（保留备查，勿恢复）：
#
#   class OrderRemark(Base):
#       __tablename__ = <已退场的表名，故意不写成字面量以免被规模统计脚本算进去>
#       __table_args__ = (Index("ix_order_remarks_order_kind", "order_id", "kind"),)
#       id / order_id(FK orders.id) / user_id(FK users.id) / kind / content
#       / created_at / updated_at
