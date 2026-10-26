from __future__ import annotations

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
# --------------------------------------------------------------------------- #
# ⚠️ **已退场的四个 ORM 类**（设计 75 S5，2026-10-27）
#
# `Order` / `OrderItem` / `OrderShipment` / `AfterSale` 全部删除：订单域改
# **平台实时查询**（`rt.list_orders` / `rt.get_order_detail`），四张表随迁移
# `n8o9p0q1r2s3` 整表 DROP。
#
# 「本地留一份订单副本」在本次改造里被判为**错的方向**：用户口径「订单全部实时查询」，
# 而副本与平台不一致正是要消灭的问题（待发货 / 已发货 / 退款中这些状态每天都在变，
# 本地副本第一时间就会说谎）。
#
# ⚠️ 留在这个文件里的是**纯函数与常量**（上面的状态映射 / `resolve_order_status`）——
# 它们是「千帆怎么说话」的知识，实时链路同样要用（`qianfan/orders.py` 与实时服务层
# 都 import 它），不属于任何一张表。
#
# ⚠️ 别把「这个文件还在」当成「表还在」的证据：`models/__init__.py` 不再导出这四个类，
# `Base.metadata.create_all()` 不会再把它们建回来。
# --------------------------------------------------------------------------- #
