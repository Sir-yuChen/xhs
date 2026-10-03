from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


class MonitoringTarget(Base):
    __tablename__ = "monitoring_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True, default="xhs")
    target_type: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    value: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default="active")
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    last_refreshed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now, onupdate=shanghai_now)
    platform_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("platform_accounts.id"), nullable=True)
    crawl_interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_crawl_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class MonitoringSnapshot(Base):
    __tablename__ = "monitoring_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("monitoring_targets.id"), index=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)


class NoteMetricSnapshot(Base):
    """笔记指标**日快照**（设计 17 §13.5.1，M2 方向 3，2026-10-03）。

    ## 为什么需要这张表

    平台侧指标（点赞/收藏/评论/分享/阅读）此前**只存在于 `notes.raw_json`**，是
    「最后一次同步时的值」—— **覆盖写、无历史** ⇒ 做不出任何趋势（「发布后 7 天
    互动增长曲线」「哪篇在涨」这类问题一律答不了）。M2 的数据仪表盘以趋势为核心，
    没有历史快照就无从谈起。

    ## 判据与口径

    | 列 | 语义 | 缺值口径 |
    |---|---|---|
    | `stat_date` | **统计日**（`YYYY-MM-DD`，上海时区） | 必填；同 note 同日唯一 |
    | `likes`/`collects`/`comments`/`shares`/`views` | 该日**快照值**（不是增量） | 解析不到 ⇒ `0`（与 `_note_metrics` 同口径） |
    | `engagement` | 四项互动之和（**冗余存**，趋势查询要按它排序求和） | 由写入方算好 |

    ⚠️ **存的是「快照值」不是「增量」**：平台给的就是累计值；存快照让「取某天数据」
    与「算区间增量」两种查询都成立（增量 = 区间末 − 区间初）。
    存增量则丢失绝对值，且**漏采一天就永久算不出**那天的存量。

    ⚠️ **同 (note_id, stat_date) 唯一**：一天之内多次采集**覆盖**同一条（取当天最终值），
    不是追加 —— 否则同日多采几次会把趋势线画成锯齿。用 `uq_note_metric_note_date` 强制。
    """

    __tablename__ = "note_metric_snapshots"
    __table_args__ = (
        UniqueConstraint("note_pk", "stat_date", name="uq_note_metric_note_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 归属用户（**租户隔离**：趋势查询一律按它收口，与全站同一把尺子）。
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    #: 指向 `notes.id`（本地主键，不是平台 `note_id` —— 平台 ID 可能变，本地主键稳定）。
    note_pk: Mapped[int] = mapped_column(ForeignKey("notes.id"), index=True, nullable=False)
    #: 统计日（`YYYY-MM-DD` 字符串 —— SQLite 按字符串比大小即日期序，且无时区歧义）。
    stat_date: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    collects: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shares: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 阅读/曝光量（`view_count`）。**v2.24 新增口径**：此前 `METRIC_KEYS` 漏了它，
    #: 而「阅读」是笔记监控里最基础的一个量（没有它算不出点击率/互动率的分母）。
    views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 四项互动之和（冗余；避免每次查询都写 `likes+collects+comments+shares`）。
    engagement: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)


class ShopMetricSnapshot(Base):
    """商品指标**日快照**（设计 17 §13.9，M2 方向 3 切片 2，2026-10-03）。

    ## 为什么与 `NoteMetricSnapshot` 分表

    两者**主键语义不同**（笔记按 `note_pk`，商品按 `product_pk`），指标集合**零重叠**
    （笔记是内容侧的曝光/互动，商品是交易侧的销量/库存/价格）。合表会让每一列对
    另一半行恒为 0 —— 那正是「用 0 冒充缺值」，违反缺值口径（硬约束⑤）。

    ⇒ **两张同构的快照表**，每一条判据与 `NoteMetricSnapshot` 逐字一致：
    `(实体主键, stat_date)` 唯一 + upsert + **存快照值不存增量**。

    ## 判据

    | 列 | 语义 | 缺值口径 |
    |---|---|---|
    | `stat_date` | **统计日**（`YYYY-MM-DD`，上海时区） | 必填；同商品同日唯一 |
    | `stock` / `sales` / `sales_30d` | 该日**快照值** | 解析不到 ⇒ `0` |
    | `price_min_fen` / `price_max_fen` | 价格区间（**整数分**） | 同上 |
    | `cart_count` | 加购数 | ⚠️ 平台侧当前**无该指标** ⇒ 恒 `0`（列留位，不造假） |

    ⚠️ **价格用「分」存整数**：直接复用 `shop_products.price_min_fen` /
    `price_max_fen`（真机实测两列都是 INTEGER 且 1982/1983 行有值），
    **不解析** `price` 那个 `"10680~55977"` 形态的字符串 —— 字符串是平台的展示形态、
    会变；分列是同步时归一化好的稳定口径。金额一律整数分（浮点累加不精确）。

    ⚠️ **不回填历史**：`shop_products` 只有「最后一次同步的值」，拿它当「历史某天」
    就是编造数据。表从空表开始，由采集逐日沉淀。
    """

    __tablename__ = "shop_metric_snapshots"
    __table_args__ = (
        UniqueConstraint("product_pk", "stat_date", name="uq_shop_metric_product_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 归属用户（**租户隔离**：趋势查询一律按它收口）。
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    #: 指向 `shop_products.id`（本地主键，不是平台 `item_id` —— 平台 ID 会变）。
    product_pk: Mapped[int] = mapped_column(
        ForeignKey("shop_products.id"), index=True, nullable=False
    )
    #: 统计日（`YYYY-MM-DD` 字符串 —— 字典序即日期序，且无时区歧义）。
    stat_date: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sales: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 近 30 天销量（平台 `sale_qty30`，同步时已解析进 `shop_products.sales_30d`）。
    sales_30d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 价格区间（**分**）。直接取自 `shop_products` 的同名列，不做任何换算。
    price_min_fen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    price_max_fen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 加购数 —— 平台侧当前无此指标，列留位（恒 0），**不造假**。
    cart_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
