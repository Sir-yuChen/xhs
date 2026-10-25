"""链路追踪域的响应模型（设计 67）。

`GET /api/system/logs/trace/{trace_id}` —— 按链路 id 查该链路的全部日志。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class TraceEvent(BaseModel):
    """链路里的一个事件（三类日志统一成同一形状，靠 `source` 区分）。"""

    #: `api` 端点面 / `business` 业务语义面 / `job` 定时任务
    source: str
    at: Optional[str] = Field(default=None, description="发生时间（ISO，升序排列）")
    #: `info` / `warning` / `error`
    level: str = "info"
    #: 操作人（日志必须能看出是谁做的）。
    actor_id: Optional[int] = None
    actor_name: str = ""
    title: str = Field(default="", description="api=方法+路径；business=事件键；job=任务名")
    summary: str = ""
    #: 结构化明细（形状随来源而变 ⇒ 刻意宽松）。
    detail: Any = None


class TraceTimelineResponse(BaseModel):
    """`GET /api/system/logs/trace/{trace_id}`。"""

    trace_id: str
    #: 本链路的事件总数（已按 limit 截断后的数量）。
    count: int = 0
    #: 按来源统计：`{"api": 3, "business": 2, "job": 1}`
    by_source: dict[str, int] = Field(default_factory=dict)
    #: 按时间升序的平铺时间线（为什么不是树：见 `trace_query_service` docstring）。
    events: list[TraceEvent] = Field(default_factory=list)
    #: `False` ⇒ 该 id 没有日志，或当前用户无权查看。
    found: bool = False
