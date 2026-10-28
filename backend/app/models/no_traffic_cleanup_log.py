"""无流量清理记录（**一张自引用父子表**，小红书私有 L1）。

2026-10-28 用户口径「no_traffic_cleanup_logs 和 no_traffic_cleanup_items 是否能合并成
一张表，父子结构，懒加载，且小红书私有」⇒ 两表合并为本表，表名加 `xhs_` 前缀（L1）。

| 层 | `row_type` | 回答的问题 |
|---|---|---|
| **批次** | `batch` | 这一次操作**整体**清了几篇、为什么整体失败 |
| **笔记** | `item` | **这一篇**被删了没有、没删是为什么 |

`before_total` 这类报账数挂在笔记行上没有意义；「这一篇当初为什么没删掉」需要
可检索的列（JSON 查不了）⇒ 两层不能压成一层，只能同表两型。

## 为什么用 `row_type` 判父行，不用 `parent_id IS NULL`

`parent_id` 是 `ondelete="SET NULL"`：批次行被保留期清理掉之后，它的明细行**必须**
还在（原意：「某篇笔记当初为什么没删掉」这个答案不该随批次记录过期而消失）。
若用 `parent_id IS NULL` 当父行判据，那些孤儿明细会被当成批次行读出来。

## 子行不级联删

保持原 `log_id` 的 `SET NULL` 语义（不是 CASCADE）：批次过期只删批次行，
明细行独立按 `operated_at` 判期（见 `record_cleanup_service`）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

#: 批次状态。
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

#: 行类型（**父子唯一判据**）。
ROW_BATCH = "batch"
ROW_ITEM = "item"

#: 笔记行的处置结果（**唯一定义处** —— 服务层与前端都引这一份）。
#: 缺值口径：`deleted` / `skipped` / `failed` / `not_in_pool` 之外**不造名**。
RESULT_DELETED = "deleted"
RESULT_SKIPPED = "skipped"
RESULT_FAILED = "failed"
#: 用户勾选了、但**已不在候选池**（阈值被改 / 已被删）⇒ 不删、如实留痕。
RESULT_NOT_IN_POOL = "not_in_pool"
#: 中止本批时**未执行到**的笔记（DEF-097 风控短路后剩下的那些）。
RESULT_NOT_ATTEMPTED = "not_attempted"

#: 结果取值集合（写入侧校验 + 前端渲染的单一来源）。
CLEANUP_ITEM_RESULTS = (
    RESULT_DELETED,
    RESULT_SKIPPED,
    RESULT_FAILED,
    RESULT_NOT_IN_POOL,
    RESULT_NOT_ATTEMPTED,
)


class NoTrafficCleanupRow(Base):
    """一次清理 = 一行 `batch` + N 行 `item`。"""

    __tablename__ = "xhs_no_traffic_cleanup_rows"
    __table_args__ = (
        Index("ix_xhs_cleanup_rows_batch_no", "batch_no"),
        Index("ix_xhs_cleanup_rows_parent", "parent_id"),
        Index("ix_xhs_cleanup_rows_type_created", "row_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: **父子唯一判据**：`batch` = 批次行，`item` = 笔记明细行。
    row_type: Mapped[str] = mapped_column(String(8), default=ROW_BATCH, index=True)
    #: 明细行所属批次；批次行恒 NULL。`SET NULL` 见模块 docstring。
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("xhs_no_traffic_cleanup_rows.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: 操作者快照（发起清理的登录用户）。无外键：用户删了历史仍可读。
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    #: 被清理的平台账号快照。账号删了/改名都不改历史。
    account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    account_name: Mapped[str] = mapped_column(String(128), default="")
    #: 关联的 tasks 行（并发控制载体）。仅作追溯，删任务行置空。
    task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("queue_jobs.id", ondelete="SET NULL"), nullable=True
    )

    # ---- 批次行字段 ----
    #: `manual` / `scheduled`。
    trigger_type: Mapped[str] = mapped_column(String(16), default="manual")
    #: 批次号：超过 500 条时拆子批次 `{基号}-{序号}`；旧数据为空串。
    batch_no: Mapped[str] = mapped_column(String(64), default="", server_default="")
    #: 执行时的筛选条件快照（阈值 + 查询条件 + 勾选的笔记）。
    thresholds: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    query: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    note_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    batch_limit: Mapped[int] = mapped_column(Integer, default=0)
    #: 报账四数 + 清理前/后。仅批次行。
    before_total: Mapped[int] = mapped_column(Integer, default=0)
    after_total: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    deleted: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    not_in_pool: Mapped[int] = mapped_column(Integer, default=0)
    #: `running` / `done` / `failed`。仅批次行有意义。
    status: Mapped[str] = mapped_column(String(16), default=STATUS_RUNNING)
    #: 本次失败原因（DEF-095：首条失败笔记的平台原文；无失败为空）。仅批次行。
    reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: 候选池覆盖度说明（信息性备注，**不是失败原因**）。仅批次行。
    coverage_note: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: 逐篇明细兼容快照（前端旧渲染路径仍在读）。权威来源是 `item` 行。
    details: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    #: 本批被中止（风控/凭据类失败 ⇒ 后续不再发请求）。仅批次行。
    aborted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0"
    )
    #: 中止原因（平台原文，不猜）。仅批次行。
    abort_reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: 清理结果时间。仅批次行。
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # ---- 明细行字段 ----
    #: **笔记主体**：平台笔记 ID。仅明细行。
    note_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    #: 标题快照（平台侧笔记已删时仍答得出「删的是哪一篇」）。
    note_title: Mapped[str] = mapped_column(String(512), default="", server_default="")
    #: 封面快照（缺值留空，不造假）。
    note_cover: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: 处置结果，取值见 `CLEANUP_ITEM_RESULTS`。仅明细行。
    result: Mapped[str] = mapped_column(String(16), default="", server_default="")
    #: 笔记发布时间快照（Epoch **毫秒**，超 int32 ⇒ BIGINT）。仅明细行。
    published_at: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    #: **操作时间**（明细行判保留期用）。仅明细行。
    operated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    #: 操作时间（批次行判保留期用；明细行同值冗余，便于排序）。
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=shanghai_now, index=True
    )

    #: 父子**懒加载**（用户口径「查询懒加载」）：查批次不自动拉明细。
    children: Mapped[list["NoTrafficCleanupRow"]] = relationship(
        "NoTrafficCleanupRow",
        back_populates="parent",
        lazy="select",
        foreign_keys=[parent_id],
    )
    parent: Mapped[Optional["NoTrafficCleanupRow"]] = relationship(
        "NoTrafficCleanupRow",
        back_populates="children",
        remote_side=[id],
        lazy="select",
        foreign_keys=[parent_id],
    )

    # ---- 读模型：两个视图各自只取自己的列（字段与合表前逐字段一致）----
    def to_dict(self) -> dict[str, Any]:
        """批次行视图（= 合表前 `NoTrafficCleanupLog.to_dict()`）。"""
        return {
            "id": self.id,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "task_id": self.task_id,
            "trigger_type": self.trigger_type,
            "batch_no": self.batch_no or "",
            "thresholds": self.thresholds or {},
            "query": self.query or {},
            "note_ids": self.note_ids or [],
            "batch_limit": self.batch_limit,
            "before_total": self.before_total,
            "after_total": self.after_total,
            "total": self.total,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "failed": self.failed,
            "not_in_pool": self.not_in_pool,
            "status": self.status,
            "reason": self.reason,
            "coverage_note": self.coverage_note or "",
            "aborted": bool(self.aborted),
            "abort_reason": self.abort_reason or "",
            "details": self.details or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
        }

    def item_to_dict(self) -> dict[str, Any]:
        """明细行视图（= 合表前 `NoTrafficCleanupItem.to_dict()`）。

        `log_id` 保留为 `parent_id` 的别名 —— 前端与响应模型零改动。
        """
        return {
            "id": self.id,
            "log_id": self.parent_id,
            "task_id": self.task_id,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "note_id": self.note_id,
            "note_title": self.note_title,
            "note_cover": self.note_cover,
            "result": self.result,
            "reason": self.reason,
            "published_at": self.published_at,
            "operated_at": self.operated_at.isoformat() if self.operated_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
