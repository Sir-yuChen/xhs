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

import logging
from typing import Any, Optional

from sqlalchemy import select

from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models.no_traffic_cleanup_log import NoTrafficCleanupLog
from backend.app.models.platform_account import PlatformAccount
from backend.app.models.task import Task
from backend.app.services.no_traffic_service import (
    CleanupQuery,
    TrafficThresholds,
    list_no_traffic_candidates,
)
from backend.app.services.rate_limiter import CrawlRateLimiter

logger = logging.getLogger(__name__)

#: 单次清理批次上限（平台写操作风控）。
DEFAULT_DELETE_BATCH = 20

#: 「清理全部候选」时最多扫多少条。**必须有界**：清理是平台写操作，
#: 一次清空几千篇必撞风控；这个值同时决定了单次「全部清理」的上限口径，
#: 由前端如实告知用户（不是静默截断）。
MAX_CANDIDATE_SCAN = 500

#: 清理任务在 tasks 表的 task_type，用于去重与查询历史。
CLEANUP_TASK_TYPE = "xhs_no_traffic_cleanup"

#: 清理任务状态。
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

_lock = CrawlRateLimiter()


def _pool_before_total(
    db: Any,
    account_id: int,
    thresholds: Optional[TrafficThresholds],
    query: Optional[CleanupQuery],
) -> int:
    """「清理前有多少条候选」—— 候选池总数（`total` 字段，与分页无关）。

    ⚠️ **「清理后」不是重算这个函数**（2026-10-05 实测踩到）：删的是**平台侧**笔记，
    本地 `shop_note_goods` 行要等下一次「同步商品」才消失 —— 重算的话刚删完 20 篇
    它照样回 1915，用户看到「清理前 1915 → 清理后 1915」会以为一篇没删。

    正确口径 = **`before_total - deleted`**：
    * `skipped`（推广中）与 `failed`（权限校验不过）**都不减** —— 它们确实还在池子里，
      这样既不会高估清理效果，也不用编「还剩多少」的第三份真相源；
    * 判据只依赖两个已记账的数，不需要再扫一遍池子（少一次只读全表）。

    扫码上限用 `MAX_CANDIDATE_SCAN`：只读，但一次列全部候选会白读几千行。
    """
    pool = list_no_traffic_candidates(
        db,
        account_id,
        thresholds or TrafficThresholds(),
        limit=MAX_CANDIDATE_SCAN,
        query=query,
    )
    return int(pool["total"])


def _payload_account(task: Task) -> Optional[int]:
    payload = task.payload if isinstance(task.payload, dict) else {}
    try:
        return int(payload.get("account_id"))
    except (TypeError, ValueError):
        return None


def _mutate_task_payload(task: Task, **fields: Any) -> None:
    """改 JSON 列要**重新赋值整个 dict**（in-place 改 SQLAlchemy 看不到）。

    `_finish` / `after_total` / `not_in_pool` 三处都写 `payload`，
    各自 `dict(...)` + 赋值一次 —— 统一在这里，避免某处漏掉赋值而静默不落库。
    """
    task.payload = {**(task.payload or {}), **fields}


def _account_name(db: Any, account_id: int) -> str:
    """账号名**快照**（写日志用）。取不到就留空，不猜（硬约束⑤）。"""
    try:
        row = db.get(PlatformAccount, account_id)
    except Exception:  # noqa: BLE001 - 名字取不到不该让清理失败
        return ""
    if row is None:
        return ""
    return str(getattr(row, "nickname", "") or "")


def _open_log(
    db: Any,
    *,
    account_id: int,
    user_id: Optional[int],
    task_id: Optional[int],
    thresholds: Optional[TrafficThresholds],
    query: Optional[CleanupQuery],
    batch_limit: int,
    note_ids: Optional[list[str]],
) -> Optional[NoTrafficCleanupLog]:
    """开一条清理日志（`status=running`），返回它。

    为什么**先建后改**而不是跑完再插：跑的过程中进程被杀 / 线程异常退出时，
    日志表里留一行 `running` 比"悄悄没有日志"好（用户能看出「这次没跑完」——
    同 `ScheduledJobLog` 的纪律）。失败则返回 `None`，清理本身不受影响。
    """
    try:
        row = NoTrafficCleanupLog(
            user_id=user_id,
            account_id=account_id,
            account_name=_account_name(db, account_id),
            task_id=task_id,
            trigger_type="manual",
            thresholds=(thresholds or TrafficThresholds()).to_dict(),
            query=(query or CleanupQuery()).to_dict(),
            note_ids=list(note_ids or []),
            batch_limit=int(batch_limit),
            status=STATUS_RUNNING,
            created_at=shanghai_now(),
            started_at=shanghai_now(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except Exception as exc:  # noqa: BLE001 - 写日志失败不许拖垮清理
        logger.warning("open no-traffic cleanup log failed: %s", exc)
        return None


def _close_log(
    db: Any,
    log: Optional[NoTrafficCleanupLog],
    *,
    status: str,
    result: Optional[dict[str, Any]] = None,
    before_total: Optional[int] = None,
    after_total: Optional[int] = None,
    not_in_pool: Optional[list[str]] = None,
    details: Optional[list[dict[str, Any]]] = None,
    reason: str = "",
) -> None:
    """把日志行改成终态（失败**不抛** —— 清理结果已落 tasks 行，日志是附加账）。"""
    if log is None:
        return
    try:
        result = result or {}
        log.status = status
        log.total = int(result.get("total") or 0)
        log.deleted = int(result.get("deleted") or 0)
        log.skipped = int(result.get("skipped") or 0)
        log.failed = int(result.get("failed") or 0)
        if before_total is not None:
            log.before_total = int(before_total)
        if after_total is not None:
            log.after_total = int(after_total)
        if not_in_pool is not None:
            log.not_in_pool = len(not_in_pool)
        if details is not None:
            log.details = details
        if reason:
            log.reason = reason
        log.finished_at = shanghai_now()
        if log.started_at:
            log.duration_ms = int(
                (log.finished_at - log.started_at).total_seconds() * 1000
            )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("close no-traffic cleanup log failed: %s", exc)


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


def running_cleanup_for_account(
    db: Any, account_id: int, user_id: Optional[int] = None
) -> Optional[Task]:
    """该账号此刻是否有未完成的清理任务（端点用：按钮态判据）。

    `_running_cleanup` 是模块内的防重入判据（只认 `account_id`）；跨用户查询要在
    它之上再收一道 `user_id`（多用户场景下「别人的清理在跑」不该让本用户按钮变灰）。
    """
    task = _running_cleanup(db, account_id)
    if task is None:
        return None
    if user_id is not None and task.user_id != user_id:
        return None
    return task


def run_no_traffic_cleanup(
    account_id: int,
    *,
    thresholds: Optional[TrafficThresholds] = None,
    query: Optional[CleanupQuery] = None,
    batch_limit: int = DEFAULT_DELETE_BATCH,
    note_ids: Optional[list[str]] = None,
    user_id: Optional[int] = None,
) -> dict[str, Any]:
    """同步执行一次无流量清理（供调度器/后台线程调用）。

    返回 ``{total, deleted, skipped, failed, task_id}``。任何异常都不向上抛，
    因为调度器调用方不期待清理失败拖垮整轮调度。
    """
    db = SessionLocal()
    task_id: Optional[int] = None
    log: Optional[NoTrafficCleanupLog] = None
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
                #: 非空 = **指定笔记清理**（列表勾选）；空 = 全量清理。
                "note_ids": list(note_ids or []),
                #: 清理前候选总数（「清理前 / 清理后 多少条」的前半 —— 页面据此报账）。
                "before_total": _pool_before_total(db, account_id, thresholds, query),
            },
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id

        # 用户口径「日志表…清理结果…」：同步路径（调度器调用）落同一张日志表。
        log = _open_log(
            db,
            account_id=account_id,
            user_id=user_id,
            task_id=task_id,
            thresholds=thresholds,
            query=query,
            batch_limit=batch_limit,
            note_ids=note_ids,
        )
        result = _cleanup_with_task(
            db, task, account_id, thresholds, query, batch_limit, note_ids=note_ids
        )
        # 「清理后」= 重算一次候选池（**同一口径**，不是 before - deleted 的估算 ——
        # 估算会把「跳过 / 失败」也算成已清，用户拿它当结论就会高估清理效果）。
        # 「清理后」= before - deleted（见 `_pool_before_total` 的 docstring：
        # 重算本地池是错的 —— 平台删了、本地行还在，要等下次同步）。
        before_total = int((task.payload or {}).get("before_total") or 0)
        after_total = max(0, before_total - int(result.get("deleted") or 0))
        _mutate_task_payload(task, after_total=after_total)
        db.commit()
        payload = task.payload if isinstance(task.payload, dict) else {}
        _close_log(
            db,
            log,
            status=STATUS_DONE if int(result.get("failed") or 0) == 0 else STATUS_FAILED,
            result=result,
            before_total=before_total,
            after_total=after_total,
            not_in_pool=list(payload.get("not_in_pool") or []),
            details=list(payload.get("details") or []),
            reason=str(payload.get("note") or ""),
        )
        return {
            **result,
            "task_id": task_id,
            "before_total": before_total,
            "after_total": after_total,
        }
    except Exception as exc:  # noqa: BLE001 - 调度器调用，必须吞异常
        logger.error(f"run_no_traffic_cleanup failed (account={account_id}): {exc}")
        _close_log(db, locals().get("log"), status=STATUS_FAILED, reason=str(exc))
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
    note_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter

    # 两条入口共用同一条执行链（列表勾选清理 / 全量清理），差别**只在候选集**：
    # * `note_ids` 非空 ⇒ **指定笔记**：只删这些（且必须仍在候选池里，见下）；
    # * `note_ids` 为空 ⇒ **全量**：按阈值 + 查询条件取前 `batch_limit` 条。
    if note_ids:
        pool = list_no_traffic_candidates(
            db, account_id, thresholds or TrafficThresholds(), limit=MAX_CANDIDATE_SCAN, query=query
        )
        by_id = {c.note_id: c for c in pool["candidates"]}
        wanted = [nid for nid in note_ids if nid in by_id]
        # ⚠️ 勾选的笔记若**已不在候选池**（阈值被改小/已被删），**不删**、如实报数：
        #    删一个「不在当前筛选口径内」的笔记 = 用户以为自己只删了看到的那批。
        # 勾了但不在池子里的：**不删**并如实回 `not_in_pool`（不是静默跳过）
        missing = [nid for nid in note_ids if nid not in by_id]
        items = [by_id[nid] for nid in wanted]
        if missing:
            _mutate_task_payload(task, not_in_pool=missing[:50])
        candidates = pool
    else:
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
    note_ids: Optional[list[str]] = None,
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
                "note_ids": list(note_ids or []),
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
        args=(account_id, thresholds, query, batch_limit, task_id, user_id, note_ids),
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
    note_ids: Optional[list[str]] = None,
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

    # ⚠️ 复用**已有的任务行**：`run_no_traffic_cleanup` 里那句 `_running_cleanup`
    #    会看到这一行（status=running）而直接返回「已有任务」—— 所以必须走
    #    `_cleanup_with_task` 这条路，把 task_id 传下去。
    db = SessionLocal()
    log: Optional[NoTrafficCleanupLog] = None
    try:
        task = db.get(Task, task_id)
        if task is None:
            return
        # 「清理前」必须在**开跑之前**算（跑完再算就已经变了）—— 建任务行的那个函数
        # 不算这一步（它只写 payload、不做只读查询），所以在这里补。
        before_total = _pool_before_total(db, account_id, thresholds, query)
        _mutate_task_payload(task, before_total=before_total)
        db.commit()
        # 用户口径「新增清理无流量笔记日志表……操作时间、账号、清理结果」：独立一行。
        log = _open_log(
            db,
            account_id=account_id,
            user_id=user_id,
            task_id=task_id,
            thresholds=thresholds,
            query=query,
            batch_limit=batch_limit,
            note_ids=note_ids,
        )
        if log is not None:
            # 挂到任务行上：`GET .../cleanup/active` 回它，前端关掉弹窗后仍能接住进度。
            _mutate_task_payload(task, log_id=log.id)
            db.commit()
        done = _cleanup_with_task(
            db, task, account_id, thresholds, query, batch_limit, note_ids=note_ids
        )
        after_total = max(0, before_total - int(done.get("deleted") or 0))
        _mutate_task_payload(task, after_total=after_total)
        db.commit()
        payload = task.payload if isinstance(task.payload, dict) else {}
        _close_log(
            db,
            log,
            status=STATUS_DONE if int(done.get("failed") or 0) == 0 else STATUS_FAILED,
            result=done,
            before_total=before_total,
            after_total=after_total,
            not_in_pool=list(payload.get("not_in_pool") or []),
            details=list(payload.get("details") or []),
            reason=str(payload.get("note") or ""),
        )
    except Exception as exc:  # noqa: BLE001 - 后台线程不许把异常抛出去
        logger.error(f"no_traffic cleanup thread failed (task={task_id}): {exc}")
        _close_log(db, log, status=STATUS_FAILED, reason=str(exc))
        _mark_failed(db, task_id, str(exc))
    finally:
        db.close()
