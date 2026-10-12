from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

#: 数据窗口取值（butterfly/data 的 `dateSelectType` 语义，2026-10-12 定调）。
#: `recent_1` / `recent_7` / `recent_90` = 平台预设窗口；`custom` = 用户自定义日期范围。
WINDOW_RECENT_1 = "recent_1"
WINDOW_RECENT_7 = "recent_7"
WINDOW_RECENT_90 = "recent_90"
WINDOW_CUSTOM = "custom"
WINDOWS: tuple[str, ...] = (WINDOW_RECENT_1, WINDOW_RECENT_7, WINDOW_RECENT_90, WINDOW_CUSTOM)


class NoteDatacenterStat(Base):
    """千帆数据中心「笔记数据」统计行（设计 35，2026-10-12）。

    ## 为什么独立成表、且与 `notes` 解耦

    数据中心的数据与内容库的笔记是**两个正交实体**：
    * `notes` 是「笔记身份」（可能被删，`purge_note` 会真删行）；
    * 本表是「历史统计」（永在 —— 笔记删了，千帆数据中心的统计仍在）。

    故本表**不设外键**指向 `notes.id`：`note_id` 是**平台 note_id 字符串**，
    笔记删除不影响统计行。数据中心列表 `stats LEFT JOIN notes`，`notes` 缺行时
    用本表的**快照列**（title / cover_url / item_* / publish_time）渲染。

    ## 为什么指标落真实列（非 JSON）

    数据中心三个核心动作（列表展示 / 按指标筛选 / 阈值判定）都要 SQL 筛选排序。
    JSON 无法筛选；落列后「阅读 < 50 且支付订单 = 0」是一个 WHERE，无流量候选
    「查询即候选」，不再全表扫 + Python 过滤。

    ## `window` 为什么进唯一键

    `butterfly/data` 返回的是**所选窗口的累计值**，不是「当天值」。不记 window
    会把「近 30 天阅读」冒充「今日阅读」。同一笔记不同窗口各一行是诚实的，
    列表取用户所选窗口那一行。

    ## `note_flag` 是分类唯一权威

    `notes.note_category` 已退役（设计 35 §6），分类判据收敛为平台原生
    `note_flag`（`goods_note` = 商业笔记 / 空 = 普通或未知）。
    """

    __tablename__ = "note_datacenter_stats"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "platform_account_id",
            "note_id",
            "stat_date",
            "window",
            name="uq_datacenter_note_date_window",
        ),
        Index("ix_datacenter_account", "user_id", "platform_account_id"),
        Index("ix_datacenter_note_id", "note_id"),
        Index("ix_datacenter_stat_date", "stat_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 归属用户（租户隔离，与全站同一把尺子）。
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    #: 归属账号（数据中心按账号同步）。
    platform_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 平台 note_id（字符串，**非外键** —— 笔记删了统计仍在）。
    note_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 同步执行日（YYYY-MM-DD，上海时区）。
    stat_date: Mapped[str] = mapped_column(String(10), nullable=False)
    #: 数据窗口（recent_1/7/90/custom）。
    window: Mapped[str] = mapped_column(String(16), nullable=False, default=WINDOW_RECENT_1)

    #: 平台原生分类（`goods_note` / 空）。**唯一权威**（取代 notes.note_category）。
    note_flag: Mapped[str] = mapped_column(String(24), nullable=False, default="")

    # ---- 基础信息快照（notes 行不存在时渲染用）----
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    cover_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    item_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    item_title: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    variant_desc: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    publish_time: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # ---- 互动指标（真实列，可筛选排序；**可空** = 缺值，无流量判定时 null 不参与）----
    read_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    like_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    collect_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    comment_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    share_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    follow_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    danmaku_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # ---- 观看指标 ----
    video_duration: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_view_time: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    full_view_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ---- 商品指标 ----
    pay_gmv: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pay_order_cnt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pay_user_cnt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    item_click_cnt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    item_click_uv: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    item_click_pv_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pay_pv_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pay_uv_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ---- 退款 / 引流 ----
    refund_gmv: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    refund_order_cnt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    refund_user_cnt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cart_item_cnt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lead_user_page_view_cnt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lead_user_page_pay_gmv: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lead_live_view_cnt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lead_live_pay_gmv: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ---- 平台补充 ----
    browse_num_30d: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # ---- 删除标记 ----
    #: NULL = 未删；非空 = 已从平台清理（历史保留，列表默认隐藏）。
    platform_deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    synced_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
