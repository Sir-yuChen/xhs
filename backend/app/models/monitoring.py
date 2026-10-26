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

# ⚠️ **已退场**（设计 75 S5，2026-10-27）：`ShopMetricSnapshot`（`shop_metric_snapshots`）整类删除。
#
# 两件事同时成立：① 采集链路早已退场（2026-10-13「指标快照子系统整块移除」：服务 / 定时任务 /
# `POST|GET /analytics/shop-metrics/*` 全删，当时**刻意保留表与历史行**）；
# ② 它的 `product_pk` 是 **NOT NULL FK → `shop_products.id`**，而商品台账已随同一迁移
# `n8o9p0q1r2s3` 整表 DROP ⇒ 留着这张表连 `DROP TABLE shop_products` 都过不去。
#
# 留着「以后可能要采」的表是最贵的空壳：每次有人看 ER 图都要重新判断一次它是不是活的。
