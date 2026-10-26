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

from backend.app.core.time import SHANGHAI_TZ, shanghai_now
from sqlalchemy.orm import Session

#: ⚠️ 设计 75 S5：本模块不 import 任何**已退场**的 ORM 类（四个订单表已 DROP）。
#: 状态常量由消费方（`qianfan/orders.py`）自行从 `models.order` 取；
#: `PlatformAccount` 保留 —— `_shop_context` 仍要取账号昵称。
from backend.app.models.platform_account import PlatformAccount

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

# --------------------------------------------------------------------------- #
# ⚠️ **已退场的同步链路**（设计 75 S5，2026-10-27）
#
# 从这里到 `export_filename` 之间的全部内容已删除：
#   `normalize_sku` / `normalize_package` / `normalize_shipment` / `normalize_after_sale`
#   / `_link_shop_product_ids` / `upsert_order` / `upsert_after_sales_from_detail`
#   / `start_order_sync` / `run_order_sync` / 进度与游标 / `_mark_credential_state`
#   / `order_summary`
#
# 用户口径（2026-10-26）：「订单全部实时查询」⇒ 订单域**本地零副本**：
#   * 订单列表 / 详情 → `rt.list_orders` / `rt.get_order_detail`
#   * 同步任务 `system.order_sync` 已从注册表移除（S3），端点 410 墓碑
#   * `orders` / `order_items` / `order_shipments` / `after_sales` 四张表随迁移
#     `n8o9p0q1r2s3` 整表 DROP
#
# 保留下来的都是**纯函数**（订单域仍在用）：
#   `mask_sensitive`（展示层脱敏）、`_shop_context`（店名 / seller_id）、
#   `export_filename` + `_csv_cell` + `orders_to_csv`（导出 CSV——输入是**实时查询**的
#   订单行，不再是本地行）。
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 展示层（**纯函数**，订单域仍用）
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


#: 导出列（`label, key`）——**唯一实现**。key 取自端点的 `_annotate_order_rows`
#: 归一后的实时订单行（`order_no` / `status` / `receiver_*` / `items[]` …）。
#: `_items_text` 是虚拟键：由 `orders_to_csv` 把 `items[]` 拼成一格。
EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("订单号", "order_no"),
    ("订单状态", "status"),
    ("下单时间", "create_time"),
    ("支付时间", "pay_time"),
    ("发货时间", "ship_time"),
    ("买家昵称", "buyer_nick"),
    ("收件人", "receiver_name"),
    ("手机号", "receiver_phone"),
    ("收货地址", "receiver_address"),
    ("商品明细", "_items_text"),
    ("实付金额(元)", "pay_amount"),
    ("快递公司", "express_company"),
    ("运单号", "express_no"),
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
