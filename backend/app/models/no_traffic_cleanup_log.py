"""无流量笔记清理日志（每次清理一行，独立保留）。

**为什么单独一张表**（2026-10-05 用户口径「新增清理无流量笔记日志表……日志表独立」）：

* `tasks` 行是**并发控制载体**（`_running_cleanup` 靠它防重入），会被公共
  「清理记录表」任务按保留期删掉 ⇒ 删完就再也答不出「上个月清了多少篇」。
* `scheduled_job_logs` 是**定时任务**日志：本清理是**手动触发**（入口只有
  「我的笔记」页按钮），手动触发不落那张表。
* 用户要的信息（账号 / 阈值 / 清理前多少条 / 删了几篇 / 失败原因 / 操作时间）
  落在 `tasks.payload` 里只是「顺便」，没有索引、没有类型、还会随 tasks 行一起消失。

**独立保留**：本表不被自己清理，由公共 `system.queue_record_cleanup` 任务按
保留期处理（用户口径「日志表独立」）。

**无外键的列**：`account_id` / `user_id` 是**执行时快照** —— 账号被删、用户改名
都不影响历史日志可读（同 `ScheduledJobLog` 的口径）。`task_id` 例外，带
`ondelete="SET NULL"`：那条 tasks 行没了不影响日志本身。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


class NoTrafficCleanupLog(Base):
    """一次无流量清理 = 一行。"""

    __tablename__ = "no_traffic_cleanup_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 操作者（发起清理的登录用户）。快照，不带外键。
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    #: 被清理的**平台账号**（用户口径「账号」）。快照：账号删了历史仍可读。
    account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    #: 账号名快照（当时叫什么）—— 不 join，账号改名不篡改历史。
    account_name: Mapped[str] = mapped_column(String(128), default="")
    #: 关联的 tasks 行（并发控制载体）。删除时置空，不影响本日志。
    task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: 触发方式 `manual` / `scheduled`（用户口径：入口只有我的笔记页按钮 ⇒ 当前恒
    #: manual；留字段以便将来接入定时任务时不用再改表）。
    trigger_type: Mapped[str] = mapped_column(String(16), default="manual")

    #: 执行时的筛选条件快照（阈值 + 查询条件 + 勾选的笔记）。
    thresholds: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    query: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    note_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    batch_limit: Mapped[int] = mapped_column(Integer, default=0)

    #: 报账四数 + 清理前/后（用户口径「清理结果」）。
    before_total: Mapped[int] = mapped_column(Integer, default=0)
    after_total: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    deleted: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    not_in_pool: Mapped[int] = mapped_column(Integer, default=0)

    #: `running` / `done` / `failed`。
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    #: 失败原因 / 备注（平台原文，不猜 —— 硬约束⑤）。
    reason: Mapped[str] = mapped_column(Text, default="")
    #: 逐篇明细（`[{note_id, ok, reason}]`）。
    details: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    #: 操作时间（用户口径「操作时间」）。
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: 清理结果时间（用户口径「清理结果时间」）。
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "task_id": self.task_id,
            "trigger_type": self.trigger_type,
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
            "details": self.details or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
        }
