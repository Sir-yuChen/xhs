"""业务日志（`business_logs`，设计 64 §2.2）—— 核心业务节点的结构化审计层。

## 与 `api_logs`（API 审计）的分工

* `api_logs`（中间件 `ApiAuditMiddleware`）：**API 面** —— 非读请求与失败请求的
  通用留痕（哪个端点、哪个状态码），不含业务语义；
* 本表（`business_log_service.record`）：**业务面** —— 核心业务节点主动打点
  （认证出入口、系统管理增删改、配置变更、队列终态），一句话说清"谁对什么做了什么"。

## 双写

`record()` 同时写本表一行 + `logging.info` 统一格式一行（进 app.log）——
DB 行供日志页查询（分页/筛选），文本行供 `grep`/告警（`logfmt` 唯一格式，设计 64 §2.1）。
DB 写**自开 Session** ⇒ worker 线程可靠（绕开"worker 里 logger 不落盘"的实测坑，
`note_publish_service` 2026-10-18 注释为证）。

## 保留策略

`record_cleanup_service.CLEANERS` 注册表按 `log_retention_days`（系统级配置，默认 90 天）
清理 —— 与 `api_logs`/`scheduled_job_logs` 同一批机制（设计 64 §2.4）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

#: 事件域（`event` 的前缀约定：`<域>.<事件名>`，如 `auth.login` / `settings.update`）。
#: 白名单只约束**注册语义**，`record()` 不强校验（新事件随代码走，漏登记只是筛选不便）。
LEVEL_INFO = "info"
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"
LEVELS: tuple[str, ...] = (LEVEL_INFO, LEVEL_WARNING, LEVEL_ERROR)


class BusinessLog(Base):
    """业务日志行（设计 64 §2.2）。只增不改 —— 审计层没有"更新"语义。"""

    __tablename__ = "business_logs"
    __table_args__ = (
        Index("ix_business_logs_event", "event"),
        Index("ix_business_logs_actor", "actor_id"),
        Index("ix_business_logs_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now, nullable=False)
    level: Mapped[str] = mapped_column(String(16), default=LEVEL_INFO, nullable=False)
    #: 事件名（`<域>.<事件名>`，如 `auth.login_failed` / `settings.update`）。
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 动作词（create/update/delete/login/logout/run/…；UI 筛选用）。
    action: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    #: 操作人（系统动作如启动恢复为 NULL —— 不造假）。
    actor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    actor_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: 工作区（事件发生在哪个工作区上下文；无上下文为空）。
    workspace: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    #: 操作对象（`user:7` / `setting:crawl_rate_limit_per_minute` / `task:123` 这类定位串）。
    target_type: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    #: 一句话摘要（日志页列表列；detail 只在详情看）。
    summary: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    #: 结构化补充（JSON 文本；含敏感值的字段在埋点侧先脱敏）。
    detail_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
