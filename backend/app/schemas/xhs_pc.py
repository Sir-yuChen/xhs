"""小红书 PC 域响应模型：无流量笔记清理（设计 61 §A6 第三批 之一）。

⚠️ 本文件**只覆盖形状确定的端点**。`pc.py` 里还有大量 `_normalize_*` 归一化
函数的产出（搜索结果 / 笔记详情 / 评论树）—— 那些形状随平台返回而变，
写死模型会在平台改字段时从「前端取到 undefined」变成「后端 500」，
是**把静默问题换成硬故障**。那批等确认稳定后再接（见设计 61 §A6 第三期备注）。

字段逐字段抄自实现：
* `NoTrafficCleanupActiveResponse` ← pc.py::no_traffic_cleanup_active
* `NoTrafficCleanupStatusResponse` ← pc.py::no_traffic_cleanup_status
* `NoTrafficCleanupLogItem` ← `NoTrafficCleanupLog.to_dict()`（:152-169）
* `NoTrafficCleanupLogsResponse` ← pc.py::no_traffic_cleanup_logs 的 return
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class NoTrafficCleanupActiveResponse(BaseModel):
    """`GET /users/no-traffic/cleanup/active` —— 此刻是否有清理在跑。

    `task_id` 让前端把弹窗关闭后剩下的轮询接回同一条任务。
    """

    running: bool = False
    task_id: Optional[int] = None
    log_id: Optional[int] = None
    #: 任务状态原文（`running` / `done` / `failed` …）。
    status: str = ""


class NoTrafficCleanupStatusResponse(BaseModel):
    """`GET /users/no-traffic/cleanup/{task_id}` —— 清理任务状态（前端轮询）。"""

    task_id: int
    status: str
    progress: int = 0
    #: 四态计数（用户口径「四态分别计数」）。
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    total: int = 0
    note: str = ""
    #: 逐条明细（形状随任务载荷，**刻意宽松**：诊断信息，不是契约字段）。
    details: list[Any] = Field(default_factory=list)
    #: 清理前 / 后候选池总数。清理后是**重算**的，不是 before - deleted
    #: （估算会把跳过/失败算成已清）。
    before_total: Optional[int] = None
    after_total: Optional[int] = None
    #: 勾选了但已不在候选池的笔记（改过阈值 / 已被删）—— 不删、如实报。
    not_in_pool: list[Any] = Field(default_factory=list)


class NoTrafficCleanupItem(BaseModel):
    """清理日志的**行内明细**（笔记维度，2026-10-06 用户口径）。

    用户要求「每条笔记的结果 / 为什么」不挤在 `details` JSON 列里等人解析，
    而是落到 `no_traffic_cleanup_items` 表按行给。
    ⚠️ 字段宽松：这张明细表会随清理策略演进加列，收紧会让加列变 500。
    """

    id: Optional[int] = None
    log_id: Optional[int] = None
    note_id: Optional[str] = None
    note_title: str = ""
    #: `deleted` / `skipped` / `failed`
    outcome: str = ""
    reason: str = ""


class NoTrafficCleanupLogItem(BaseModel):
    """一条清理操作日志（`NoTrafficCleanupLog.to_dict()` 的产物 + 行内 items）。"""

    id: int
    account_id: Optional[int] = None
    account_name: str = ""
    task_id: Optional[int] = None
    #: `manual` / `scheduled`
    trigger_type: str = ""
    batch_no: str = ""
    #: 本次使用的阈值快照（`{"likes": 5, "days": 30}` 之类）——宽松，随策略变。
    thresholds: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    note_ids: list[Any] = Field(default_factory=list)
    batch_limit: int = 0
    before_total: Optional[int] = None
    after_total: Optional[int] = None
    total: int = 0
    deleted: int = 0
    skipped: int = 0
    #: 每行**内联**笔记维度明细（pc.py:2198-2201 拼进去的）。
    items: list[NoTrafficCleanupItem] = Field(default_factory=list)


class NoTrafficCleanupLogsResponse(BaseModel):
    """`GET /users/no-traffic/cleanup-logs` —— 分页日志。"""

    total: int = 0
    page: int = 1
    limit: int = 20
    page_count: int = 1
    items: list[NoTrafficCleanupLogItem] = Field(default_factory=list)
