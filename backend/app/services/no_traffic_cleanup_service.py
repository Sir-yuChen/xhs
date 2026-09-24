"""无流量笔记异步清理执行器（后台跑，不占用请求线程）。

设计要点（全部为风控服务）：

* **有界批次**：单次最多删 ``DEFAULT_DELETE_BATCH`` 篇（默认 20），绝不一次性清完
  候选池 —— 平台写操作密集触发会直接撞风控。
* **限流**：复用 ``CrawlRateLimiter``，按账号维度的每分钟上限；超限就**停本次**，
  等下一轮调度（调度器 ``max_instances=1 + coalesce`` 保证不会重叠跑）。
* **推广笔记拦截**：删除前置权限校验返回 ``is_note_in_promotion=true`` 的笔记
  **跳过不删**（2026-09-19 实测该字段存在），避免删到正在投放的笔记。
* **两步执行链**：先 ``validate_delete_permission`` 再 ``note/delete``，
  权限校验不过**不**发删除请求。
* **任务表跟踪**：整批一行 ``tasks`` 记录（``task_type='xhs_no_traffic_cleanup'``），
  每篇结果落明细；失败原因保留平台原文，不猜。
* **幂等防重**：按 ``payload.account_id`` 查未完成任务行，同一账号同时只有一批在跑。

为什么 ``account_id`` 放 ``payload`` 而不是新加列：``tasks`` 表被一期发布链路使用，
加列要走迁移；清理任务生命周期短，payload 查询足够（见 ``_running_cleanup``）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select

from backend.app.core.database import SessionLocal
from backend.app.core.security import decrypt_text
from backend.app.core.time import shanghai_now
from backend.app.models.task import Task
from backend.app.services.no_traffic_service import (
    DEFAULT_BATCH_LIMIT,
    CleanupQuery,
    TrafficThresholds,
    list_no_traffic_candidates,
)
from backend.app.services.rate_limiter import CrawlRateLimiter

logger = logging.getLogger(__name__)

#: 单次清理批次上限（平台写操作风控）。
DEFAULT_DELETE_BATCH = 20

#: 清理任务在 tasks 表的 task_type，用于去重与查询历史。
CLEANUP_TASK_TYPE = "xhs_no_traffic_cleanup"

#: 清理任务状态。
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

_lock = CrawlRateLimiter()


def _payload_account(task: Task) -> Optional[int]:
    payload = task.payload if isinstance(task.payload, dict) else {}
    try:
        return int(payload.get("account_id"))
    except (TypeError, ValueError):
        return None


def _account_cookies(db: Any, account_id: int) -> Optional[str]:
    """取账号**创作者平台**的令牌（2026-09-21 拆分，§11.5；JSON 串转成请求头串）。

    无流量清理打的是创作者平台的笔记管理接口，所以按平台取 `creator`。
    """
    from backend.app.services.account_service import CHANNEL_CREATOR, latest_cookies_for_channel

    return latest_cookies_for_channel(db, account_id, CHANNEL_CREATOR) or None


def _running_cleanup(db: Any, account_id: int) -> Optional[Task]:
    """该账号是否已有未完成的清理任务（防重入）。

    用 ``payload->>'account_id'`` 匹配（SQLite/Postgres 均支持 JSON 取值）。
    """
    rows = db.scalars(
        select(Task).where(
            Task.task_type == CLEANUP_TASK_TYPE,
            Task.status.in_([STATUS_PENDING, STATUS_RUNNING]),
        )
    ).all()
    for row in rows:
        if _payload_account(row) == account_id:
            return row
    return None


def run_no_traffic_cleanup(
    account_id: int,
    *,
    thresholds: Optional[TrafficThresholds] = None,
    query: Optional[CleanupQuery] = None,
    batch_limit: int = DEFAULT_DELETE_BATCH,
    user_id: Optional[int] = None,
) -> dict[str, Any]:
    """同步执行一次无流量清理（供调度器/后台线程调用）。

    返回 ``{total, deleted, skipped, failed, task_id}``。任何异常都不向上抛，
    因为调度器调用方不期待清理失败拖垮整轮调度。
    """
    db = SessionLocal()
    task_id: Optional[int] = None
    try:
        existing = _running_cleanup(db, account_id)
        if existing is not None:
            return {
                "total": 0,
                "deleted": 0,
                "skipped": 0,
                "failed": 0,
                "task_id": existing.id,
                "message": "已有清理任务在执行，本次跳过（防重复触发）",
            }

        query = query or CleanupQuery()
        task = Task(
            user_id=user_id or 1,
            platform="xhs",
            task_type=CLEANUP_TASK_TYPE,
            status=STATUS_RUNNING,
            started_at=shanghai_now(),
            payload={
                "account_id": account_id,
                "thresholds": (thresholds or TrafficThresholds()).to_dict(),
                "query": query.to_dict(),
                "batch_limit": batch_limit,
            },
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id

        result = _cleanup_with_task(db, task, account_id, thresholds, query, batch_limit)
        return {**result, "task_id": task_id}
    except Exception as exc:  # noqa: BLE001 - 调度器调用，必须吞异常
        logger.error(f"run_no_traffic_cleanup failed (account={account_id}): {exc}")
        _mark_failed(db, task_id, str(exc))
        return {
            "total": 0,
            "deleted": 0,
            "skipped": 0,
            "failed": 0,
            "task_id": task_id,
            "message": f"清理失败：{exc}",
        }
    finally:
        db.close()


def _cleanup_with_task(
    db: Any,
    task: Task,
    account_id: int,
    thresholds: Optional[TrafficThresholds],
    query: Optional[CleanupQuery],
    batch_limit: int,
) -> dict[str, Any]:
    from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter

    candidates = list_no_traffic_candidates(
        db, account_id, thresholds or TrafficThresholds(), limit=batch_limit, query=query
    )
    items = candidates["candidates"]
    cookies = _account_cookies(db, account_id)
    if not cookies:
        _finish(task, deleted=0, skipped=0, failed=0, total=0, note="账号无可用 Cookie")
        db.commit()
        return {"total": 0, "deleted": 0, "skipped": 0, "failed": 0}

    adapter = XhsCreatorApiAdapter(cookies)
    deleted = skipped = failed = 0
    details: list[dict[str, Any]] = []

    for candidate in items:
        note_id = candidate.note_id
        if not note_id:
            continue
        # 限流：超限就停本次，剩下的等下一轮调度（coalesce 保证不重叠）。
        if not _lock.allow(account_id):
            details.append({"note_id": note_id, "ok": False, "reason": "触发限流，等待下一轮"})
            break

        try:
            ok, msg, payload = adapter.validate_delete_permission(note_id)
            if not ok:
                failed += 1
                details.append({"note_id": note_id, "ok": False, "reason": f"权限校验失败：{msg}"})
                continue
            # 推广中的笔记不删（风控红线）。
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict) and data.get("is_note_in_promotion"):
                skipped += 1
                details.append({"note_id": note_id, "ok": False, "reason": "笔记正在推广中，已跳过"})
                continue

            ok, msg, _ = adapter.delete_note(note_id)
            if ok:
                deleted += 1
                details.append({"note_id": note_id, "ok": True})
            else:
                failed += 1
                details.append({"note_id": note_id, "ok": False, "reason": msg})
        except Exception as exc:  # noqa: BLE001 - 单篇失败不中断整批
            failed += 1
            details.append({"note_id": note_id, "ok": False, "reason": str(exc)})

    _finish(
        task,
        deleted=deleted,
        skipped=skipped,
        failed=failed,
        total=len(items),
        details=details,
        note=candidates["coverage"]["reason"],
    )
    db.commit()
    return {
        "total": len(items),
        "deleted": deleted,
        "skipped": skipped,
        "failed": failed,
    }


def _finish(
    task: Task,
    *,
    deleted: int,
    skipped: int,
    failed: int,
    total: int,
    details: Optional[list[dict[str, Any]]] = None,
    note: str = "",
) -> None:
    task.status = STATUS_DONE if failed == 0 else STATUS_FAILED
    task.finished_at = shanghai_now()
    task.progress = 100
    payload = dict(task.payload or {})
    payload.update(
        {
            "deleted": deleted,
            "skipped": skipped,
            "failed": failed,
            "total": total,
            "note": note,
        }
    )
    if details is not None:
        payload["details"] = details
    task.payload = payload


def _mark_failed(db: Any, task_id: Optional[int], reason: str) -> None:
    if task_id is None:
        return
    try:
        task = db.get(Task, task_id)
        if task is None:
            return
        task.status = STATUS_FAILED
        task.finished_at = shanghai_now()
        payload = dict(task.payload or {})
        payload["error"] = reason
        task.payload = payload
        db.commit()
    except Exception:  # noqa: BLE001 - 标记失败本身不能再抛
        logger.warning("mark cleanup task failed: could not update task row")


def run_no_traffic_cleanup_background(
    account_id: int,
    *,
    thresholds: Optional[TrafficThresholds] = None,
    query: Optional[CleanupQuery] = None,
    batch_limit: int = DEFAULT_DELETE_BATCH,
    user_id: Optional[int] = None,
) -> int:
    """在**独立线程**里跑清理，立即返回任务 id（HTTP 请求不阻塞）。

    用法：API 收到「开始清理」→ 建任务行 → 起线程 → 立即返回。
    清理在后台慢慢跑，调度器下一轮 ``run_scheduled_cleanups`` 会接手没跑完的。
    """
    import threading

    query = query or CleanupQuery()
    task_db = SessionLocal()
    try:
        existing = _running_cleanup(task_db, account_id)
        if existing is not None:
            return existing.id
        task = Task(
            user_id=user_id or 1,
            platform="xhs",
            task_type=CLEANUP_TASK_TYPE,
            status=STATUS_PENDING,
            payload={
                "account_id": account_id,
                "thresholds": (thresholds or TrafficThresholds()).to_dict(),
                "query": query.to_dict(),
                "batch_limit": batch_limit,
                "source": "manual",
            },
        )
        task_db.add(task)
        task_db.commit()
        task_db.refresh(task)
        task_id = task.id
    finally:
        task_db.close()

    thread = threading.Thread(
        target=_run_in_thread,
        args=(account_id, thresholds, query, batch_limit, task_id, user_id),
        name=f"no-traffic-cleanup-{account_id}",
        daemon=True,
    )
    thread.start()
    return task_id


def _run_in_thread(
    account_id: int,
    thresholds: Optional[TrafficThresholds],
    query: Optional[CleanupQuery],
    batch_limit: int,
    task_id: int,
    user_id: Optional[int],
) -> None:
    """线程体：复用既有任务行，不重新建任务。"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if task is None:
            return
        task.status = STATUS_RUNNING
        task.started_at = shanghai_now()
        db.commit()
    finally:
        db.close()

    run_no_traffic_cleanup(
        account_id,
        thresholds=thresholds,
        query=query,
        batch_limit=batch_limit,
        user_id=user_id,
    )
