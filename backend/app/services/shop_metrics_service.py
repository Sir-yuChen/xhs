"""商品指标的口径与采集（设计 17 §13.9，M2 方向 3 切片 2，2026-10-03）。

## 为什么与 `note_metrics_service` 分开

两者**实体不同**（笔记 vs 商品）、**指标集合零重叠**（内容侧曝光/互动 vs 交易侧
销量/库存/价格）、**主键语义不同**（`note_pk` vs `product_pk`）。硬塞进一个模块会让
每个函数都要按实体分叉，且「笔记口径」与「商品口径」的守卫互相牵连。

⇒ **两个平行模块**，结构与判据逐字对齐（`collect_snapshots` / `trend_series` /
`stat_date_of` 同名同形），好处是「知道一个就知道另一个」。

## 口径（缺值一律 0，不造假）

⚠️ **价格直接取 `shop_products.price_min_fen` / `price_max_fen`，不解 `price` 字符串**：

真机实测（1983 行）——`shop_products` 的 `price` 列是**字符串**
（形如 `"10680~55977"`），而 `price_min_fen` / `price_max_fen` 是**已有的 INTEGER 列**
（1982/1983 行有值，单位：分）。同步链路早已把平台的展示字符串归一化成整数分了。

⇒ 本模块**只读分列**。解析字符串 = 复刻一遍平台展示格式的脆弱解析（格式一变就崩），
且与同步链路口径分叉（DEF-044 同型）。

⚠️ **`cart_count`（加购）平台侧当前无此指标** ⇒ 恒 `0`（列留位，不造假）。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend.app.core.time import shanghai_now
from backend.app.models.monitoring import ShopMetricSnapshot
from backend.app.models.shop_product import ShopProduct
from backend.app.services.sqlite_retry import retry_on_lock

#: 每批写入行数（与笔记侧同值同理由 —— 见 `note_metrics_service._SNAPSHOT_BATCH`）。
_SNAPSHOT_BATCH = 200

#: 指标 → `shop_products` 上的列名。**每一项都是真实的库列**（不是候选键）——
#: 商品侧不像 `notes.raw_json` 那样是松散 JSON，它是强 schema 表，列名唯一且稳定。
METRIC_COLUMNS: dict[str, str] = {
    "stock": "stock",
    "sales": "sales",
    "sales_30d": "sales_30d",
    "price_min_fen": "price_min_fen",
    "price_max_fen": "price_max_fen",
}

#: 参与「商品概览」求和的列（当前未使用，留作后续聚合口径的锚点）。
AGGREGATE_COLUMNS: tuple[str, ...] = ("stock", "sales", "sales_30d")


def _as_int(value: Any) -> int:
    """宽容转整数（缺值 ⇒ 0，与 `note_metrics_service._as_int` 同口径）。"""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return 0
    return 0


def stat_date_of(moment: Any) -> str:
    """把「现在」转成快照的 `stat_date`（`YYYY-MM-DD`，上海时区）。

    ⚠️ **必须与 `note_metrics_service.stat_date_of` 同口径**：两张表按日期聚合、
    页面上要并排展示，日期口径分叉会让同一天在两张表里差一天。
    """
    return moment.strftime("%Y-%m-%d")


def shop_metrics(product: ShopProduct) -> dict[str, int]:
    """从一行商品取指标（**口径唯一实现**：采集与快照都从这里取）。

    全部列都是可空 INTEGER ⇒ 缺值一律 0（不造假）。
    """
    values = {
        name: _as_int(getattr(product, column, None))
        for name, column in METRIC_COLUMNS.items()
    }
    #: 加购：平台侧无该指标 ⇒ 恒 0（列留位，见模块 docstring）。
    values["cart_count"] = 0
    return values


def collect_snapshots(
    db: Session, *, user_id: int, stat_date: str | None = None
) -> int:
    """采集该用户全部商品当日的指标快照，返回**写入行数**。

    ⚠️ **覆盖语义**（同日重复采集取最新值）：`(product_pk, stat_date)` 上有唯一键，
    这里用 `ON CONFLICT DO UPDATE` 更新 —— 不是先删后插（两次写、中间有窗口），
    也不是 `INSERT OR IGNORE`（那会永远保留当天第一次的旧值，越采越不准）。
    与 `note_metrics_service.collect_snapshots` 同一判据。

    ⚠️ **`stat_date` 必须可注入**：跨零点跑用例时，`23:59` 采集 + `00:00` 断言会红 ——
    那是**假 flaky**（不是真缺陷）。留这个参数是为了让测试能钉住日期。
    """
    day = stat_date or stat_date_of(shanghai_now())

    products = db.scalars(
        select(ShopProduct).where(ShopProduct.user_id == user_id)
    ).all()

    rows = []
    for product in products:
        metrics = shop_metrics(product)
        rows.append(
            {
                "user_id": product.user_id,
                "product_pk": product.id,
                "stat_date": day,
                "stock": metrics["stock"],
                "sales": metrics["sales"],
                "sales_30d": metrics["sales_30d"],
                "price_min_fen": metrics["price_min_fen"],
                "price_max_fen": metrics["price_max_fen"],
                "cart_count": metrics["cart_count"],
            }
        )
    if not rows:
        return 0

    # ⚠️ **批量写 + 撞锁重试** —— 与 `note_metrics_service.collect_snapshots`
    # **同一根因、同一修法**（DEF-085，2026-10-04 实测）：逐行 execute 持锁太久，
    # 而定时任务是每用户一个 job ⇒ 多用户同分钟并发采集必然互撞。
    # 判据与常量都在 `sqlite_retry`（共用实现，不各写一份）。
    written = 0
    for start in range(0, len(rows), _SNAPSHOT_BATCH):
        chunk = rows[start : start + _SNAPSHOT_BATCH]
        batch = sqlite_insert(ShopMetricSnapshot).values(chunk)
        batch = batch.on_conflict_do_update(
            index_elements=["product_pk", "stat_date"],
            set_={
                "stock": batch.excluded.stock,
                "sales": batch.excluded.sales,
                "sales_30d": batch.excluded.sales_30d,
                "price_min_fen": batch.excluded.price_min_fen,
                "price_max_fen": batch.excluded.price_max_fen,
                "cart_count": batch.excluded.cart_count,
            },
        )
        retry_on_lock(
            lambda b=batch: db.execute(b),
            what=f"商品指标快照采集（user_id={user_id}，第 {start // _SNAPSHOT_BATCH + 1} 批）",
        )
        written += len(chunk)

    retry_on_lock(db.commit, what=f"商品指标快照提交（user_id={user_id}）")
    return written


def trend_series(
    db: Session, *, user_id: int, days: int = 30, product_pk: int | None = None
) -> list[dict[str, Any]]:
    """按日聚合的商品趋势序列（该用户；可选只取某个商品）。

    每一天返回该日**所有商品快照的合计**（库存/销量求和；价格取**区间的最值**：
    `min(price_min_fen)` ~ `max(price_max_fen)` —— 价格求和没有业务含义）。

    ⚠️ 只返回**有快照的天**（不补零）：与 `note_metrics_service.trend_series` 同口径，
    缺失的天用 0 填充 = 把「那天没采」画成「那天销量掉到 0」，是伪造数据。
    """
    statement = (
        select(
            ShopMetricSnapshot.stat_date,
            func.sum(ShopMetricSnapshot.stock).label("stock"),
            func.sum(ShopMetricSnapshot.sales).label("sales"),
            func.sum(ShopMetricSnapshot.sales_30d).label("sales_30d"),
            #: 价格取区间最值，不求和（求和无业务含义）。
            func.min(ShopMetricSnapshot.price_min_fen).label("price_min_fen"),
            func.max(ShopMetricSnapshot.price_max_fen).label("price_max_fen"),
            func.sum(ShopMetricSnapshot.cart_count).label("cart_count"),
        )
        .where(ShopMetricSnapshot.user_id == user_id)
        .group_by(ShopMetricSnapshot.stat_date)
        .order_by(ShopMetricSnapshot.stat_date)
    )
    if product_pk is not None:
        statement = statement.where(ShopMetricSnapshot.product_pk == product_pk)
    if days > 0:
        # 只取最近 N 天（`YYYY-MM-DD` 的字典序即时间序）。
        cutoff = (shanghai_now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        statement = statement.where(ShopMetricSnapshot.stat_date >= cutoff)

    return [
        {
            "stat_date": row.stat_date,
            "stock": int(row.stock or 0),
            "sales": int(row.sales or 0),
            "sales_30d": int(row.sales_30d or 0),
            "price_min_fen": int(row.price_min_fen or 0),
            "price_max_fen": int(row.price_max_fen or 0),
            "cart_count": int(row.cart_count or 0),
        }
        for row in db.execute(statement).all()
    ]


__all__ = [
    "AGGREGATE_COLUMNS",
    "METRIC_COLUMNS",
    "collect_snapshots",
    "shop_metrics",
    "stat_date_of",
    "trend_series",
]
