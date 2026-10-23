"""无流量笔记清理日志（批次一行 + 笔记明细一行，独立保留）。

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

## 两层主体（2026-10-06 用户口径「所有主体应是笔记维度」）

用户口径原文：「清理无流量的主要对象是笔记，**所有主体应是笔记维度**，记录操作日志时
也应该是笔记维度」。⇒ 本表是**批次层**，逐篇明细下沉到 :class:`NoTrafficCleanupItem`：

| 层 | 表 | 回答的问题 |
|---|---|---|
| **批次** | `no_traffic_cleanup_logs`（本表） | 这一次操作**整体**清了几篇、为什么整体失败 |
| **笔记** | `no_traffic_cleanup_items` | **这一篇**被删了没有、没删是为什么 |

两层各有不可替代的问题：批次行承载「一次操作」的报账口径（`before_total` 挂在笔记行上
没有意义），笔记行承载「这篇当初为什么没删掉」的可检索答案（JSON 列查不了）。

## `reason` 的语义（DEF-095，2026-10-06 更正）

⚠️ 此前 `reason` 被错写成**候选池覆盖度文案**（`_finish(note=coverage["reason"])`）——
那是一句**正常态**提示（「这池子只覆盖挂了千帆链的笔记」），与本次为何失败毫无关系。
真机表现：平台风控失败时，界面「失败原因」列显示「候选池仅包含已同步千帆挂链映射的
笔记…」，把用户指向完全错误的方向。

现在两件事分家：

* `reason` = **本次失败原因**（首条失败笔记的平台原文；无失败则为空）；
* `coverage_note` = 候选池覆盖度说明（信息性备注，**不显示在「失败原因」列**）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

#: 批次状态。
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

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


class NoTrafficCleanupLog(Base):
    """一次无流量清理 = 一行（**批次层**）。"""

    __tablename__ = "no_traffic_cleanup_logs"
    #: ⚠️ 2026-10-23 补：`ix_no_traffic_cleanup_logs_batch_no` 此前只写在迁移
    #: `c0ffee000002` 里，模型层没声明 ⇒ `create_all` 建的库会丢（查询变慢，不报错）。
    __table_args__ = (
        Index("ix_no_traffic_cleanup_logs_batch_no", "batch_no"),
    )

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
    #: **批次号**（2026-10-13）：一次清理超过 500 条时后端自动拆成多个子批次，
    #: 每个子批次 batch_no = `{唯一批次号}-{自增序号}`（如 `20261013_0030-1` / `-2`）。
    #: 同一批次的多个子批次共享同一「基号」（去掉 `-N` 后缀），前端树状/分组按此聚合。
    #: 旧数据（单批不超 500）为**空串**（迁移前无此概念），渲染时回退用 log.id。
    batch_no: Mapped[str] = mapped_column(String(64), default="", server_default="")

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
    status: Mapped[str] = mapped_column(String(16), default=STATUS_RUNNING, index=True)
    #: **本次失败原因**（DEF-095：首条失败笔记的平台原文；无失败则为空）。
    #: ⚠️ 此前这里存的是**候选池覆盖度文案** —— 见模块 docstring。
    reason: Mapped[str] = mapped_column(Text, default="")
    #: 候选池覆盖度说明（「只覆盖挂了千帆链的笔记」）。信息性备注，**不是失败原因**。
    coverage_note: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: 逐篇明细（`[{note_id, ok, reason}]`）。
    #: ⚠️ 2026-10-06 起它仍是 `tasks.payload` 的**兼容快照**（前端旧渲染路径与
    #: `GET .../cleanup/{task_id}` 状态轮询仍在读）；**权威来源**是
    #: `no_traffic_cleanup_items`（可按笔记检索）。
    details: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    #: **本批被中止**（DEF-097）：平台风控 / 凭据类失败 ⇒ 后续笔记**不再发请求**。
    aborted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="0")
    #: 中止原因（平台原文，不猜 —— 硬约束⑤）。
    abort_reason: Mapped[str] = mapped_column(Text, default="", server_default="")

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


class NoTrafficCleanupItem(Base):
    """**一篇笔记一行**（2026-10-06 用户口径「所有主体应是笔记维度」）。

    分工见 `NoTrafficCleanupLog` 的模块 docstring：批次行答「这次整体怎么样」，
    本表答「**这一篇**被删了没有、为什么没删」。

    * `user_id` / `account_id` 冗余存快照（同批次行口径）：批次行被保留期删掉、
      或账号改名之后，**笔记行单独也能读懂**（不必 join 回批次表）。
    * `log_id` 带 `ondelete="SET NULL"`：删批次行**不**连带删笔记行 ——
      「某篇笔记当初为什么没删掉」这个问题的答案不该随批次记录过期而消失。
    * **不存 `ok` 布尔**：`result` 是五值枚举（`deleted` / `skipped` / `failed` /
      `not_in_pool` / `not_attempted`），布尔会把「跳过」与「没轮到」压成同一个 `false`。
    """

    __tablename__ = "no_traffic_cleanup_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 操作者快照（与批次行同口径，不带外键）。
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    #: 被清理的**平台账号**快照（用户口径「账号」）。
    account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    #: 账号名快照（当时叫什么）—— 不 join，账号改名不篡改历史。
    account_name: Mapped[str] = mapped_column(String(128), default="")
    #: 关联的**批次**日志行（删批次行置空，不影响本行。
    log_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("no_traffic_cleanup_logs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: 关联的 tasks 行（并发控制载体）。仅作追溯，删任务行置空。
    task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )

    #: **笔记主体**：平台笔记 ID。
    note_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    #: 标题快照（处置当时的标题）—— 平台侧笔记已被删掉时，这里仍答得出「删的是哪一篇」。
    note_title: Mapped[str] = mapped_column(String(512), default="", server_default="")
    #: 封面快照（缺值留空，不造假）。
    note_cover: Mapped[str] = mapped_column(Text, default="", server_default="")

    #: 处置结果，取值见 `CLEANUP_ITEM_RESULTS`。
    result: Mapped[str] = mapped_column(String(16), default=RESULT_FAILED, index=True)
    #: 结果原因（平台原文，不猜 —— 硬约束⑤）。成功（`deleted`）为空。
    reason: Mapped[str] = mapped_column(Text, default="", server_default="")

    #: 笔记发布时间快照（毫秒时间戳；缺则 NULL，不补 0）。
    #: ⚠️ Epoch 毫秒（实测 max 1782271294000，超 int32）⇒ **必须 BIGINT**（2026-10-23）。
    published_at: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    #: **操作时间**（用户口径「操作时间」）。
    operated_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now, index=True)
    #: 入库时间。
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "log_id": self.log_id,
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
