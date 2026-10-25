"""业务日志服务（设计 64 §2.2）：`record()` 双写 DB + 统一格式文本行。

## 为什么 DB 写自开 Session

`note_publish_service`（2026-10-18 注释）实测：**worker 线程里 logger 不落盘**
（启动器重定向的时序问题）—— 业务终态打点恰恰都发生在 worker 线程里。
`record()` 自己开一个短 Session 写 `business_logs`，与线程/重定向解耦；
文本行走 logger 只是"尽力而为"的第二份（DB 行才是查询真相源）。

## 埋点纪律

只埋**核心业务节点**（设计 64 §2.3：认证出入口 / 系统管理 CRUD / 配置变更 /
队列终态）—— 每个平台请求都打点就是 `api_logs` 干的事，别重复。
敏感值（密码/token）**不许**进 summary/detail（埋点侧负责，格式化层不脱敏）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.logfmt import BIZ_LOGGER_NAME, biz
from backend.app.core.time import shanghai_now
from backend.app.models.business_log import LEVEL_ERROR, LEVEL_INFO, LEVEL_WARNING, BusinessLog

logger = logging.getLogger(BIZ_LOGGER_NAME)

#: detail_json 的体积上限（超长截断 —— 审计行不是转储桶）。
DETAIL_MAX_CHARS = 4000


def _current_trace_id() -> str | None:
    """当前链路 id（设计 67）。没有返回 None —— 不造值。"""
    try:
        from backend.app.core import tracing

        return tracing.current_trace_id()
    except Exception:  # noqa: BLE001 - 追踪不可用不该阻断业务日志
        return None


def record(
    event: str,
    *,
    action: str = "",
    actor_id: Optional[int] = None,
    actor_name: str = "",
    workspace: Optional[str] = None,
    target_type: str = "",
    target_id: Any = None,
    summary: str = "",
    detail: Optional[dict[str, Any]] = None,
    level: str = LEVEL_INFO,
    db: Optional[Session] = None,
    #: 显式指定链路 id（中间件路径用 —— 见 `_current_trace_id` 的 docstring）。
    #: 传了就优先用它，否则自动取当前上下文。
    trace_id_override: Optional[str] = None,
) -> None:
    """记一条业务日志（双写；**永不抛** —— 日志失败不能打断业务）。

    * `db` 传入 ⇒ 用调用方会话（**不 commit**，随业务事务同生共死 —— 业务回滚日志也回滚，
      适合"随 CRUD 埋点"的形状）；`db=None` ⇒ 自开短会话立即提交（worker 线程 /
      与业务事务解耦的场景，如队列终态）。
    * 文本行走 `biz()` 统一格式（设计 64 §2.1），级别对齐 logger。
    """
    try:
        clean_event = str(event or "").strip()
        if not clean_event:
            return
        if level not in (LEVEL_INFO, LEVEL_WARNING, LEVEL_ERROR):
            level = LEVEL_INFO
        detail_text = json.dumps(detail, ensure_ascii=False, default=str)[:DETAIL_MAX_CHARS] if detail else ""
        target_text = str(target_id) if target_id is not None else ""

        row = BusinessLog(
            created_at=shanghai_now(),
            level=level,
            event=clean_event,
            action=str(action or ""),
            actor_id=actor_id,
            actor_name=str(actor_name or ""),
            #: 链路 id 自动取当前上下文（设计 67）—— 调用方**不用传**，
            #: 同一条链路上的所有业务日志自动共享同一个 id。
            trace_id=trace_id_override or _current_trace_id(),
            workspace=workspace,
            target_type=str(target_type or ""),
            target_id=target_text[:128],
            summary=str(summary or "")[:255],
            detail_json=detail_text,
        )
        owned_session = db is None
        session = db if db is not None else _open_session()
        try:
            if owned_session:
                session.add(row)
                session.commit()
            else:
                # ⚠️ 调用方会话 ⇒ **savepoint（begin_nested）**：写日志失败时只回滚
                # savepoint，外层业务事务不被毒死（PG 的 aborted-transaction 会把
                # 后续所有语句打到 25P02 —— 2026-10-25 实测踩坑）。
                try:
                    with session.begin_nested():
                        session.add(row)
                        session.flush()
                except Exception:  # noqa: BLE001 - savepoint 失败就地吞掉
                    pass
        finally:
            if owned_session and session is not None:
                session.close()

        log_fn = (
            logger.warning if level == LEVEL_WARNING else logger.error if level == LEVEL_ERROR else logger.info
        )
        log_fn(
            biz(
                clean_event,
                action=action,
                actor=actor_name or actor_id,
                workspace=workspace,
                target=f"{target_type}:{target_text}" if target_type else target_text,
                summary=summary,
            )
        )
    except Exception:  # noqa: BLE001 - 日志失败绝不打断业务
        logging.getLogger(__name__).warning("business_log: 记录 %s 失败", event, exc_info=True)


def register_with_audit() -> None:
    """把「系统域写操作」的写入实现注册给 `core/api_audit.py`（启动时调一次）。

    ## 为什么需要这个适配器

    `core/api_audit.py` 要在系统域写操作后记一条业务日志，但**合同 C1 规定
    `core/` 不得 import `services/`**（延迟 import 也算）。所以依赖方向必须反过来：
    services 知道 core，core 只认一个回调。

    这个函数就是那个回调的**唯一实现**：它把 audit 中间件给的原始参数
    （method / user_id / path / status_code）翻译成 `record()` 的业务语义。
    """
    from backend.app.core import api_audit

    def _record(
        event: str,
        *,
        method: str,
        user_id: int | None,
        path: str,
        status_code: int,
        trace_id: str | None = None,
    ) -> None:
        outcome = "ok" if status_code < 400 else "fail"
        record(
            event,
            action=str(method or "").lower(),
            actor_id=user_id,
            target_type="endpoint",
            target_id=str(path or "")[:128],
            summary=f"{method} {path} → {outcome}({status_code})",
            level=LEVEL_INFO if status_code < 400 else LEVEL_WARNING,
            detail={"status_code": status_code},
            #: 由中间件从 request.state 取（contextvar 在 call_next 后已恢复）。
            trace_id_override=trace_id,
        )

    api_audit.set_business_recorder(_record)


def _open_session():
    from backend.app.core.database import SessionLocal

    return SessionLocal()


def list_logs(
    db: Session,
    *,
    event: Optional[str] = None,
    action: Optional[str] = None,
    actor_id: Optional[int] = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[BusinessLog], int]:
    """业务日志分页查询（日志页第三 Tab 的数据源；新→旧）。"""
    from sqlalchemy import func

    conditions = []
    if event:
        conditions.append(BusinessLog.event.like(f"%{event.strip()}%"))
    if action:
        conditions.append(BusinessLog.action == action.strip())
    if actor_id is not None:
        conditions.append(BusinessLog.actor_id == actor_id)

    total = int(
        db.scalar(
            select(func.count()).select_from(BusinessLog).where(*conditions)
            if conditions
            else select(func.count()).select_from(BusinessLog)
        )
        or 0
    )
    statement = select(BusinessLog)
    for condition in conditions:
        statement = statement.where(condition)
    rows = db.scalars(
        statement.order_by(BusinessLog.id.desc())
        .offset(max(0, (page - 1)) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


def serialize_log(row: BusinessLog) -> dict[str, Any]:
    """一行 → API 形态（detail_json 解析失败原样给文本 —— 不造假）。"""
    detail: Any = row.detail_json
    if row.detail_json:
        try:
            detail = json.loads(row.detail_json)
        except (ValueError, TypeError):
            detail = row.detail_json
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "level": row.level,
        "event": row.event,
        "action": row.action,
        "actor_id": row.actor_id,
        "actor_name": row.actor_name,
        "workspace": row.workspace,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "summary": row.summary,
        "detail": detail,
        #: 链路 id（设计 67）—— 前端「链路」列据此可点开查整条。
        "trace_id": getattr(row, "trace_id", None),
    }
