"""链路日志查询（设计 67，2026-10-25 用户口径）。

用户要求：「日志应以链路式记录」「后台支持使用链路 id 查该链路请求的所有日志」。

## 一条链路里有哪些日志

一次用户操作（如「点同步按钮」）会产生**三类**日志，分在三张表：

| 表 | 记什么 | 触发时机 |
|---|---|---|
| `api_logs` | 端点面：谁调了哪个端点、状态码、耗时 | 每个 HTTP 请求 |
| `business_logs` | 业务语义面：登录、配置变更、队列终态 | 业务代码显式埋点 |
| `scheduled_job_logs` | 定时任务执行（用户触发时继承该次点击的链路） | 每次任务执行 |

**只有按 trace_id 把三张表合起来按时间排，才是用户眼里的「一次操作的完整过程」。**
分开看任何一张都会漏：只看 api_logs 会漏掉异步跑的业务动作，只看 business_logs
会漏掉端点与耗时。

## 为什么不返回「一棵树」而是**平铺时间线**

企业级方案（Jaeger / Tempo）返回的是 span 树，因为它们的 span 有显式父子关系
（`parent_span_id`）。本项目只在 HTTP 入口记了一个 span，后台任务的父子关系
（哪个任务由哪次点击触发）**没有可靠数据来源** —— 硬造一棵树会给出错误的层级。
⇒ 返回**按时间排序的平铺时间线 + 每条的来源表**，用户按时间读就是真实顺序。
将来接 OTel 有了真实 span 关系再升级成树。

## 安全边界

按 trace_id 查**不做用户过滤**？→ 必须做。日志里含各用户的操作记录，
非管理员只能看**自己**的（`user_id == 本人`）。admin 可看全部 —— 这是运维排查
的前提（用户报一个 X-Trace-Id，管理员要能查完整链路）。
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models.api_log import ApiLog
from backend.app.models.business_log import BusinessLog
from backend.app.models.scheduled_job import ScheduledJobLog
from backend.app.models.user import User
from backend.app.services.business_log_service import serialize_log as serialize_business
from backend.app.core.roles import ROLE_ADMIN


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _timeline(
    db: Session, trace_id: str, *, user_id: Optional[int], limit: int
) -> list[dict[str, Any]]:
    """把三张表里该链路的记录合成一条时间线（按时间升序）。

    `user_id=None` ⇒ 不过滤（管理员）；否则只看该用户的记录。
    """
    events: list[dict[str, Any]] = []

    # ── ① 端点面 ──────────────────────────────────────────────
    stmt = select(ApiLog).where(ApiLog.trace_id == trace_id)
    if user_id is not None:
        stmt = stmt.where(ApiLog.user_id == user_id)
    for row in db.scalars(stmt).all():
        meta = row.meta if isinstance(row.meta, dict) else {}
        events.append({
            "source": "api",
            "at": _iso(row.created_at),
            "level": "error" if row.status == "server_error" else (
                "warning" if row.status == "client_error" else "info"
            ),
            "actor_id": row.user_id,
            "actor_name": "",
            "title": f"{meta.get('method', '')} {row.endpoint}".strip(),
            "summary": f"{row.status}"
            + (f" ({meta.get('status_code')})" if meta.get("status_code") else "")
            + (f" {meta.get('elapsed_ms')}ms" if meta.get("elapsed_ms") is not None else ""),
            "detail": meta,
        })

    # ── ② 业务语义面 ──────────────────────────────────────────
    stmt = select(BusinessLog).where(BusinessLog.trace_id == trace_id)
    if user_id is not None:
        stmt = stmt.where(BusinessLog.actor_id == user_id)
    for row in db.scalars(stmt).all():
        data = serialize_business(row)
        events.append({
            "source": "business",
            "at": data.get("created_at"),
            "level": data.get("level") or "info",
            "actor_id": data.get("actor_id"),
            "actor_name": data.get("actor_name") or "",
            "title": data.get("event") or "",
            "summary": data.get("summary") or "",
            "detail": data.get("detail"),
        })

    # ── ③ 定时任务 ────────────────────────────────────────────
    stmt = select(ScheduledJobLog).where(ScheduledJobLog.trace_id == trace_id)
    if user_id is not None:
        stmt = stmt.where(ScheduledJobLog.user_id == user_id)
    for row in db.scalars(stmt).all():
        events.append({
            "source": "job",
            "at": _iso(row.started_at or row.created_at),
            "level": "error" if row.status == "failed" else "info",
            "actor_id": row.user_id,
            "actor_name": "",
            "title": row.job_name or row.job_key,
            "summary": f"{row.status} {row.message or ''}".strip(),
            "detail": row.result if isinstance(row.result, dict) else None,
        })

    events.sort(key=lambda e: (e.get("at") or "", e.get("source") or ""))
    return events[:limit]


def trace_timeline(
    db: Session, trace_id: str, *, current_user: User, limit: int = 500
) -> dict[str, Any]:
    """按链路 id 取完整时间线（含操作人账号）。

    :param current_user: 非管理员只看自己的记录（安全边界见模块 docstring）。
    """
    clean = str(trace_id or "").strip().lower()
    is_admin = (getattr(current_user, "role", "") or "") == ROLE_ADMIN
    events = _timeline(
        db, clean, user_id=None if is_admin else current_user.id, limit=limit
    )

    #: 补操作人账号名（api_logs / scheduled_job_logs 只存了 id）。
    ids = {e["actor_id"] for e in events if e.get("actor_id")}
    names: dict[int, str] = {}
    if ids:
        for uid, username in db.execute(
            select(User.id, User.username).where(User.id.in_(ids))
        ).all():
            names[int(uid)] = username or ""
    for e in events:
        if not e.get("actor_name") and e.get("actor_id") in names:
            e["actor_name"] = names[e["actor_id"]]

    by_source: dict[str, int] = {}
    for e in events:
        by_source[e["source"]] = by_source.get(e["source"], 0) + 1

    return {
        "trace_id": clean,
        "count": len(events),
        "by_source": by_source,
        "events": events,
        #: 缺值口径：id 不在上面说明这条链路没有日志（或无权查看）。
        "found": bool(events),
    }
