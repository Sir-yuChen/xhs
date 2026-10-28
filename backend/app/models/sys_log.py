"""系统日志（`api_logs` + `business_logs` 合并为 `sys_logs`，L0 项目骨架）。

2026-10-28 用户口径「business_logs 和 api_logs 功能重复合并一张表」+
「系统基础表新增 sys 前缀」⇒ 两表合并为 `sys_logs`，`log_type` 分型：

| 型 | 来源 | 记录什么 |
|---|---|---|
| `api` | `ApiAuditMiddleware` | 端点面：哪个端点、哪个状态码（不含业务语义） |
| `business` | `business_log_service.record` | 业务面：谁对什么做了什么（认证出入口/系统管理增删改/配置变更/队列终态） |

## 为什么合并

两表都是「带 `trace_id` 的只增审计行」，唯一的差别是各自只用了部分列：
`api` 行用 `endpoint` / `status` / `status_code` / `meta`，
`business` 行用 `level` / `event` / `action` / `actor` / `target` / `summary` / `detail_json`。
合到一张表后 `trace_id` 链路上的两类记录天然同源，少一张表也少一条清理规则。

## 单表继承

`ApiLog` / `BusinessLog` 是 `SysLog` 的两个子类（`polymorphic_on=log_type`）
⇒ `select(ApiLog)` 自动带 `WHERE log_type='api'`，现有查询**一行不用改**。

⚠️ 判据是 `log_type`，不是「某列是否为空」—— 空值不是类型标识。

## 双写

`record()` 同时写本表一行 + `logging.info` 一行（进 app.log）：DB 行供日志页查询，
文本行供 `grep`/告警。DB 写**自开 Session** ⇒ worker 线程可靠。

## 保留策略

`record_cleanup_service` 按 `log_retention_days`（默认 90 天）清理，
合并后**只剩一条清理规则**（原两条）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

#: 日志类型（**父子唯一判据**）。
LOG_TYPE_API = "api"
LOG_TYPE_BUSINESS = "business"

#: 业务事件的级别。
LEVEL_INFO = "info"
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"
LEVELS: tuple[str, ...] = (LEVEL_INFO, LEVEL_WARNING, LEVEL_ERROR)

#: `api` 行的结果档。
API_STATUS_SUCCESS = "success"
API_STATUS_CLIENT_ERROR = "client_error"
API_STATUS_SERVER_ERROR = "server_error"


class SysLog(Base):
    """系统日志行。**只增不改** —— 审计层没有「更新」语义。"""

    __tablename__ = "sys_logs"
    __table_args__ = (
        Index("ix_sys_logs_log_type", "log_type"),
        Index("ix_sys_logs_trace", "trace_id"),
        Index("ix_sys_logs_created", "created_at"),
        Index("ix_sys_logs_user", "user_id"),
        Index("ix_sys_logs_event", "event"),
        Index("ix_sys_logs_endpoint", "endpoint"),
        Index("ix_sys_logs_status_code", "status_code"),
        Index("ix_sys_logs_actor", "actor_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: **类型唯一判据**：`api` / `business`。
    log_type: Mapped[str] = mapped_column(String(16), nullable=False)

    #: 链路 id（W3C trace-id，32 位十六进制）。一次用户操作可能触发多个请求与
    #: 后台任务，靠它串成一条链路。可空：迁移前的存量行没有（缺值不造假）。
    trace_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    #: 操作者（`api` 行 = 请求用户；`business` 行的操作人在 `actor_id`）。
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: 平台域（`api` 行用；空 = 非平台域请求）。
    platform: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # ---- `api` 型字段 ----
    #: 端点路径。
    endpoint: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    #: 结果档：`success` / `client_error` / `server_error`。
    status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    #: HTTP 状态码（提成真列才能按 200/403/500 筛 + 建索引）。
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: 请求元信息（method / elapsed_ms / query / body，**不含敏感值**）。
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # ---- `business` 型字段 ----
    #: 级别：`info` / `warning` / `error`。
    level: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    #: 事件名（`<域>.<事件名>`，如 `auth.login_failed` / `settings.update`）。
    event: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: 动作词（create/update/delete/login/logout/run/…；UI 筛选用）。
    action: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    #: 操作人（系统动作如启动恢复为 NULL —— 不造假）。
    actor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    actor_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: 事件发生在哪个工作区上下文；无上下文为空。
    workspace: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    #: 操作对象（`user:7` / `setting:x` / `task:123` 这类定位串）。
    target_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    #: 一句话摘要（列表列；detail 只在详情看）。
    summary: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    #: 结构化补充（JSON 文本；敏感字段在埋点侧先脱敏）。
    detail_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now, nullable=False)

    __mapper_args__ = {"polymorphic_on": log_type, "polymorphic_identity": "log"}


class ApiLog(SysLog):
    """API 面日志子类 —— 只读写 `log_type='api'` 的行。"""

    __mapper_args__ = {"polymorphic_identity": LOG_TYPE_API}


class BusinessLog(SysLog):
    """业务面日志子类 —— 只读写 `log_type='business'` 的行。"""

    __mapper_args__ = {"polymorphic_identity": LOG_TYPE_BUSINESS}
