"""系统日志域响应模型：业务日志（设计 64 §2.2 / 设计 61 §A6 第二期）。

⚠️ 审计日志（`GET /logs/audit`）已有模型（`AuditLogDetailOut` 等），本文件
只补**业务日志**这条新链路 —— 它是设计 64 加的语义审计层，与 API 审计分工：
* `api_logs` = 端点面留痕（谁调了哪个端点）
* `business_logs` = 业务面语义（登录/登出、系统管理增删改、配置变更、队列终态）

字段逐字段抄自 `services/business_log_service.serialize_log()`（:198-219）。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class BusinessLogItem(BaseModel):
    """一条业务日志。"""

    id: int
    created_at: Optional[str] = None
    #: `info` / `warning` / `error`
    level: str = "info"
    #: 事件键，如 `auth.login_failed` / `queue.finished` / `systemops.request`
    event: str = ""
    #: 动作（`create` / `update` / `delete` / `login` …）
    action: str = ""
    actor_id: Optional[int] = None
    actor_name: str = ""
    workspace: Optional[str] = None
    target_type: str = ""
    target_id: Any = Field(default=None, description="目标标识（路径 / 行 id，类型随域）")
    summary: str = ""
    #: 结构化明细。**可能是 dict 也可能是字符串** —— `detail_json` 解析失败时
    #: 原样给文本（服务层口径「不造假」），所以这里只能 `Any`。
    detail: Any = None


class BusinessLogListResponse(BaseModel):
    """`GET /api/system/logs/business` —— 分页（信封对齐 `paginated_with_total`）。"""

    items: list[BusinessLogItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
