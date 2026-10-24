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
from backend.app.models import Note
from backend.app.services.note_purge_service import purge_note
from backend.app.models.no_traffic_cleanup_log import (
    CLEANUP_ITEM_RESULTS,
    RESULT_DELETED,
    RESULT_FAILED,
    RESULT_NOT_ATTEMPTED,
    RESULT_NOT_IN_POOL,
    RESULT_SKIPPED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    NoTrafficCleanupItem,
    NoTrafficCleanupLog,
)
from backend.app.models.platform_account import PlatformAccount
from backend.app.models.scheduled_job import LOG_FAILED, LOG_RUNNING, LOG_SUCCESS, ScheduledJobLog
from backend.app.models.task import Task
from backend.app.services.no_traffic_service import (
    CleanupQuery,
    TrafficThresholds,
    candidates_by_note_ids,
    list_no_traffic_candidates,
)
from backend.app.core.rate_limiter import CrawlRateLimiter

logger = logging.getLogger(__name__)

#: 单次清理批次上限（平台写操作风控）—— **普通笔记**单批删除上限（历史链路不变）。
DEFAULT_DELETE_BATCH = 20

#: **商业笔记**单批删除上限（2026-10-06）：千帆 `batch_delete_note` 支持批量，
#: 一次请求可删整组，50 与千帆分片上限对齐。普通笔记仍走 `DEFAULT_DELETE_BATCH`。
GOODS_DELETE_BATCH = 50

#: 「清理全部候选」时最多扫多少条。**必须有界**：清理是平台写操作，
#: 一次清空几千篇必撞风控；这个值同时决定了单次「全部清理」的上限口径，
#: 由前端如实告知用户（不是静默截断）。
MAX_CANDIDATE_SCAN = 500

#: 清理任务在 tasks 表的 task_type，用于去重与查询历史。
CLEANUP_TASK_TYPE = "xhs_no_traffic_cleanup"

#: 单批清理的 note_ids 上限（2026-10-13 用户口径「每批次最多 500 条」）。
#: 超过此数由 `run_no_traffic_cleanup_background` 自动按 500 切片成多个子批次。
MAX_NOTES_PER_BATCH = 500

#: 单篇失败即**中止整批**的错误特征（DEF-097）。
#:
#: 为什么要短路：平台回「账号存在风险，请重新登录」这类**账号级**错误时，
#: 剩下的每一篇都注定同样失败 —— 继续打就是朝一个**已被风控告警**的账号
#: 连发 19 次写前置请求，既无产出又加重风控。
#:
#: 判据用**平台原话子串**而不是错误码：创作者平台这批接口的错误码没有一个
#: 被本仓文档化过（千帆那套 `is_credential_error` 是另一条链路的判据，不通用），
#: 而平台原文是**实测拿到**的。⚠️ 加固手段：`_looks_like_account_level_failure`
#: 只认这几句，认不出时**照旧逐篇继续**（宁可多跑也不误判中止）。
ACCOUNT_LEVEL_FAILURE_MARKERS = (
    "账号存在风险",
    "请重新登录",
    "登录已过期",
    "登录凭证已过期",
    "未登录",
)

#: 清理任务状态（`pending` / `running` / `done` / `failed`）由模型层单点定义，
#: 本模块直接沿用那份常量 —— 不复写第二份（DEF-044）。


def _looks_like_account_level_failure(message: str) -> bool:
    """这条失败是**账号级**的吗（⇒ 本批应中止，DEF-097）。

    账号级 = 与「这一篇笔记」无关，换一篇也一样失败（风控 / 凭据 / 未登录）。
    单篇级 = 这一篇特有的问题（已被删、无权限、参数不合法）⇒ 继续跑下一篇。
    """
    text = str(message or "")
    if not text:
        return False
    return any(marker in text for marker in ACCOUNT_LEVEL_FAILURE_MARKERS)


#: 平台返回「笔记不存在」的特征原文（2026-10-12，无流量清理软删除）。
#:
#: 用户口径：「删平台前校验笔记是否存在 → 不存在则只删本地」。校验走**删除请求本身**：
#: 平台回「笔记不存在」即视为已不存在（等价已删），**只软删本地**，不算失败。
#:
#: ⚠️ 只认这几句（保守）：认不出时**照旧算失败**（宁可少软删也不误判成「已删」
#: 而把一个实际还在平台的笔记从本地抹掉）。
NOTE_ABSENT_MARKERS = (
    "笔记不存在",
    "笔记已删除",
    "笔记已被删除",
    "note not found",
    "note not exist",
    "not exist",
    "已删除",
    "不存在",
)


def _looks_like_note_absent(message: str) -> bool:
    """平台说这条笔记**已经不存在**了吗（⇒ 软删本地，不算失败）。"""
    text = str(message or "")
    if not text:
        return False
    return any(marker in text.lower() for marker in NOTE_ABSENT_MARKERS)

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
    batch_no: str = "",
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
            batch_no=batch_no,
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


def _close_job_log(
    db: Any,
    job_log: ScheduledJobLog,
    *,
    ok: bool,
    message: str = "",
    exception: str = "",
) -> None:
    """收尾**定时任务日志**（清理任务也落任务中心，2026-10-13）。失败不抛。"""
    try:
        job_log.status = LOG_SUCCESS if ok else LOG_FAILED
        job_log.finished_at = shanghai_now()
        job_log.progress = 100
        job_log.message = message
        job_log.exception_info = exception
        if job_log.started_at is not None:
            job_log.duration_ms = int(
                (job_log.finished_at - job_log.started_at).total_seconds() * 1000
            )
        db.commit()
    except Exception as exc:  # noqa: BLE001 - 写日志失败不许拖垮清理
        logger.warning("close no-traffic cleanup job log failed: %s", exc)


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
    coverage_note: str = "",
    aborted: bool = False,
    abort_reason: str = "",
) -> None:
    """把日志行改成终态（失败**不抛** —— 清理结果已落 tasks 行，日志是附加账）。

    ⚠️ `reason` 是**本次失败原因**（DEF-095），不是覆盖度文案 —— 后者走
    `coverage_note`。两者分开传，缺省都不覆盖既有值（`if reason:` 守卫）。
    """
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
        if coverage_note:
            log.coverage_note = coverage_note
        log.aborted = bool(aborted)
        if abort_reason:
            log.abort_reason = abort_reason
        log.finished_at = shanghai_now()
        if log.started_at:
            log.duration_ms = int(
                (log.finished_at - log.started_at).total_seconds() * 1000
            )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("close no-traffic cleanup log failed: %s", exc)


def _first_failure_reason(details: list[dict[str, Any]]) -> str:
    """从逐篇明细里取**首条失败原因** —— 批次行 `reason` 的唯一来源（DEF-095）。

    ⚠️ 用户口径「失败原因」指的是**为什么没删掉**，不是候选池覆盖度。此前批次行的
    `reason` 被写成 `payload["note"]`（覆盖度文案）⇒ 平台风控失败时界面显示
    「候选池仅包含已同步千帆挂链映射的笔记…」，把用户指向完全错误的方向。

    只取**首条**而不是拼接：一页 20 篇失败时 20 条同样的话糊满一列表格（同一根因
    重复 20 遍没有任何信息增量）；逐篇原因在笔记明细行里各归各的。
    """
    for item in details or []:
        if not isinstance(item, dict):
            continue
        if item.get("ok"):
            continue
        text = str(item.get("reason") or "").strip()
        if text:
            return text
    return ""


def _record_items(
    db: Any,
    *,
    log: Optional[NoTrafficCleanupLog],
    task_id: Optional[int],
    account_id: int,
    user_id: Optional[int],
    rows: list[dict[str, Any]],
) -> None:
    """把逐篇处置结果落成**笔记维度**的行（用户口径「主体应是笔记维度」）。

    `rows` 的每一项形如::

        {"note_id": ..., "note_title": ..., "note_cover": ...,
         "published_at": int|None, "result": RESULT_*, "reason": str}

    为什么单独一次批量写而不是每篇写一次：清理是**平台写操作**，逐篇 commit 会把
    事务切得很碎（SQLite 上每片都要拿写锁）。这里只在收尾时写一次。

    `log` 为 `None`（日志表写失败）时**仍然落明细** —— 笔记行独立可读（自带
    `user_id` / `account_id` / `note_title` 快照），不依赖批次行存在。
    """
    if not rows:
        return
    try:
        for row in rows:
            result = str(row.get("result") or "")
            if result not in CLEANUP_ITEM_RESULTS:
                # 不造名：未知结果落 `failed` 并把原值带进 reason（硬约束⑤）。
                row = {**row, "result": RESULT_FAILED,
                       "reason": (row.get("reason") or "") or f"未知结果：{result!r}"}
            db.add(
                NoTrafficCleanupItem(
                    user_id=user_id,
                    account_id=account_id,
                    account_name=_account_name(db, account_id),
                    log_id=log.id if log is not None else None,
                    task_id=task_id,
                    note_id=str(row.get("note_id") or ""),
                    note_title=str(row.get("note_title") or ""),
                    note_cover=str(row.get("note_cover") or ""),
                    result=str(row.get("result")),
                    reason=str(row.get("reason") or ""),
                    published_at=row.get("published_at"),
                    operated_at=shanghai_now(),
                )
            )
        db.commit()
    except Exception as exc:  # noqa: BLE001 - 明细写失败不许抹掉已完成的清理结果
        logger.warning("record no-traffic cleanup items failed: %s", exc)


def _account_cookies(db: Any, account_id: int) -> Optional[str]:
    """取账号**创作者平台**的令牌（2026-09-21 拆分，§11.5；JSON 串转成请求头串）。

    无流量清理打的是创作者平台的笔记管理接口，所以按平台取 `creator`。
    """
    from backend.app.services.account_service import CHANNEL_CREATOR, latest_cookies_for_channel

    return latest_cookies_for_channel(db, account_id, CHANNEL_CREATOR) or None


def _ark_cookies(db: Any, account_id: int) -> Optional[str]:
    """取账号**千帆（ark）平台**的令牌（2026-10-06 加）。

    商业笔记（`note_flag == "goods_note"`）的删除走千帆 `note/batch_delete_note`，需要
    ark 通道 Cookie（与创作者平台的 `creator` 令牌互不通用）。
    """
    from backend.app.services.account_service import CHANNEL_ARK, latest_cookies_for_channel

    return latest_cookies_for_channel(db, account_id, CHANNEL_ARK) or None


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
            db, task, account_id, thresholds, query, batch_limit, note_ids=note_ids, log=log
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
            # DEF-095：`reason` = **本次失败原因**（首条失败笔记的平台原文）；
            # 一篇都没跑到时（无 Cookie）退 `abort_reason`。不再是候选池覆盖度文案。
            reason=_first_failure_reason(list(payload.get("details") or []))
            or str(payload.get("abort_reason") or ""),
            coverage_note=str(payload.get("coverage_note") or ""),
            aborted=bool(payload.get("aborted")),
            abort_reason=str(payload.get("abort_reason") or ""),
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
    *,
    log: Optional[NoTrafficCleanupLog] = None,
) -> dict[str, Any]:
    from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter
    from backend.app.adapters.xhs.qianfan_api_adapter import XhsQianfanApiAdapter

    # 两条入口共用同一条执行链（列表勾选清理 / 全量清理），差别**只在候选集**：
    # * `note_ids` 非空 ⇒ **指定笔记**：只删这些（且必须仍在候选池里，见下）；
    # * `note_ids` 为空 ⇒ **全量**：按阈值 + 查询条件取前 `batch_limit` 条。
    #: 勾选了但**查不到统计行**的笔记（只有指定清理才有）。
    #: 先声明，让下面无论走哪条分支都拿得到它（不要靠 `if note_ids` 短路「不会求值」）。
    missing: list[str] = []
    if note_ids:
        # 2026-10-12 修复：按勾选的 note_id **直接查统计表**构建候选，绕过候选池的
        # `MAX_CANDIDATE_SCAN=500` 分页截断（此前勾选的笔记排在 500 名之后就被
        # `not_in_pool` 误判「不在候选池」⇒ 不删 ⇒ 看起来像「清理没触发」）。
        found = candidates_by_note_ids(db, account_id, list(note_ids))
        by_id = {c.note_id: c for c in found}
        wanted = [nid for nid in note_ids if nid in by_id]
        # 勾了但**统计表里查不到**（数据源不在统计表 / 已软删）：**不删**并如实回 `not_in_pool`。
        missing = [nid for nid in note_ids if nid not in by_id]
        items = [by_id[nid] for nid in wanted]
        if missing:
            _mutate_task_payload(task, not_in_pool=missing[:50])
        candidates = {"candidates": found, "coverage": {"reason": ""}}
    else:
        # 候选取数上限按分类：商业（goods）单批 ≤ GOODS_DELETE_BATCH（50，千帆批量），
        # 普通/未指定仍按 batch_limit（默认 20，历史链路不变）。
        is_goods = bool(query and getattr(query, "category", "") == "goods")
        pool_limit = GOODS_DELETE_BATCH if is_goods else int(batch_limit)
        candidates = list_no_traffic_candidates(
            db, account_id, thresholds or TrafficThresholds(), limit=pool_limit, query=query
        )
        items = candidates["candidates"]
    #: 笔记维度明细的统一收集器（用户口径「主体应是笔记维度」）。
    #: 每一项 = 一篇笔记的处置事实：谁 / 哪篇 / 结果 / 为什么 / 何时。
    item_rows: list[dict[str, Any]] = []

    # 勾选但不在池：**留痕**（用户明确勾过它，必须答得出「为什么没删」）。
    for nid in missing:
        item_rows.append(
            {
                "note_id": nid,
                "note_title": "",
                "result": RESULT_NOT_IN_POOL,
                "reason": "已不在当前候选池（阈值已改或该笔记已被删除）",
            }
        )

    cookies = _account_cookies(db, account_id)
    if not cookies:
        # ⚠️ 批次行的失败原因 = **平台/账号侧事实**（此处是「账号无可用 Cookie」），
        #    不是覆盖度文案（DEF-095）。`coverage_note` 仍如实填覆盖度。
        reason = "账号无可用 Cookie"
        _finish(
            task,
            deleted=0,
            skipped=0,
            failed=0,
            total=0,
            note=candidates["coverage"]["reason"],
            aborted=True,
            abort_reason=reason,
        )
        db.commit()
        return {
            "total": 0,
            "deleted": 0,
            "skipped": 0,
            "failed": 0,
            "aborted": True,
            "abort_reason": reason,
        }

    adapter = XhsCreatorApiAdapter(cookies)
    # 2026-10-06：商业笔记（note_flag == "goods_note"）删除走千帆 `note/batch_delete_note`，
    # 需要 ark 通道 Cookie；取不到时商业笔记退回 creator 链路（与普通笔记同）。
    ark_cookies = _ark_cookies(db, account_id)
    qianfan_adapter = XhsQianfanApiAdapter(ark_cookies) if ark_cookies else None
    deleted = skipped = failed = 0
    aborted = False
    abort_reason = ""
    details: list[dict[str, Any]] = []

    # 分组：商业笔记（goods_note）走千帆批量删除，普通笔记（非 goods_note）走 creator 逐篇。
    # 2026-10-12 设计 35：分流判据由 `note_category` 改 `note_flag`（取代已退役列）。
    # 分类截断：商业单批 ≤ GOODS_DELETE_BATCH（50），普通单批 ≤ batch_limit（默认 20）。
    goods_items = [c for c in items if c.note_flag == "goods_note" and c.note_id][:GOODS_DELETE_BATCH]
    creator_items = [c for c in items if c.note_flag != "goods_note" and c.note_id][: int(batch_limit)]

    def _append_not_attempted(rest: list[Any], reason: str) -> None:
        for r in rest:
            item_rows.append(
                {
                    "note_id": r.note_id,
                    "note_title": r.title,
                    "note_cover": r.cover,
                    "published_at": r.publish_time,
                    "result": RESULT_NOT_ATTEMPTED,
                    "reason": reason,
                }
            )

    def _append_result(candidate: Any, result: str, reason: str = "") -> None:
        item_rows.append(
            {
                "note_id": candidate.note_id,
                "note_title": candidate.title,
                "note_cover": candidate.cover,
                "published_at": candidate.publish_time,
                "result": result,
                "reason": reason,
            }
        )

    # 2026-10-12 软删除重构：平台删除成功后**本地软删除**（`is_deleted=True`），
    # 不再 `purge_note` 硬删。软删后所有查询 / 同步都排除该行（见 `Note.is_deleted`）。
    # 本地已无此行 ⇒ 静默跳过，不误报失败。
    def _soft_delete_local(candidate: Any) -> None:
        if getattr(candidate, "local_id", 0):
            local_note = db.get(Note, candidate.local_id)
        else:
            local_note = db.scalars(
                select(Note).where(
                    Note.note_id == candidate.note_id,
                    Note.platform_account_id == account_id,
                )
            ).first()
        if local_note is not None:
            local_note.is_deleted = True

    # 商业笔记：千帆 `note/batch_delete_note` **分片批量**（每片 ≤ QIANFAN_DELETE_CHUNK），
    # 减少请求次数。无「推广中」/「权限校验」前置（千帆接口没有这两步，平台侧自行
    # 拦投放中的笔记）。取不到 ark Cookie 时退回 creator 链路（见下 `creator_items`）。
    if goods_items and qianfan_adapter is not None:
        # goods_items 已按 GOODS_DELETE_BATCH（50）截断 ⇒ 一次批量删整组。
        chunk_ids = [c.note_id for c in goods_items]
        if not _lock.allow(account_id):
            aborted = True
            abort_reason = "触发限流，等待下一轮"
            _append_not_attempted(goods_items, "触发限流，等待下一轮")
            _append_not_attempted(creator_items, "本批已中止（限流），未执行")
        else:
            try:
                ok, msg, _ = qianfan_adapter.batch_delete_notes(chunk_ids)
            except Exception as exc:  # noqa: BLE001 - 整批失败可恢复（下一轮重试）
                ok, msg = False, f"千帆删除异常：{type(exc).__name__}: {exc}"
            if ok:
                deleted += len(goods_items)
                for c in goods_items:
                    _soft_delete_local(c)
                    details.append({"note_id": c.note_id, "ok": True})
                    _append_result(c, RESULT_DELETED)
            else:
                failed += len(goods_items)
                for c in goods_items:
                    details.append({"note_id": c.note_id, "ok": False, "reason": msg})
                    _append_result(c, RESULT_FAILED, str(msg))
                if _looks_like_account_level_failure(str(msg)):
                    aborted = True
                    abort_reason = str(msg)
                    _append_not_attempted(creator_items, f"本批已中止（{msg}），未执行")
    elif goods_items:
        # 无 ark Cookie ⇒ 商业笔记退回 creator 逐篇，但**仍遵守 batch_limit 截断**
        # （退回后总数 ≤ batch_limit，超出部分标 NOT_ATTEMPTED 等下一轮）。
        merged = goods_items + creator_items
        creator_items = merged[: int(batch_limit)]

    for index, candidate in enumerate(creator_items):
        note_id = candidate.note_id
        # 限流：超限就停本次，剩下的等下一轮调度（coalesce 保证不重叠）。
        if not _lock.allow(account_id):
            details.append({"note_id": note_id, "ok": False, "reason": "触发限流，等待下一轮"})
            _append_result(candidate, RESULT_NOT_ATTEMPTED, "触发限流，等待下一轮")
            aborted = True
            abort_reason = "触发限流，等待下一轮"
            _append_not_attempted(creator_items[index + 1 :], "本批已中止（限流），未执行")
            break

        try:
            ok, msg, payload = adapter.validate_delete_permission(note_id)
            if not ok:
                # 2026-10-12：「笔记不存在」= 平台侧已无此笔记 ⇒ 等价已删，只软删本地。
                if _looks_like_note_absent(str(msg)):
                    deleted += 1
                    _soft_delete_local(candidate)
                    details.append({"note_id": note_id, "ok": True, "reason": "平台已不存在该笔记，仅本地软删除"})
                    item_rows.append(
                        {
                            "note_id": note_id,
                            "note_title": candidate.title,
                            "note_cover": candidate.cover,
                            "published_at": candidate.publish_time,
                            "result": RESULT_DELETED,
                            "reason": "平台已不存在该笔记，仅本地软删除",
                        }
                    )
                    continue
                reason = f"权限校验失败：{msg}"
                failed += 1
                details.append({"note_id": note_id, "ok": False, "reason": reason})
                item_rows.append(
                    {
                        "note_id": note_id,
                        "note_title": candidate.title,
                        "note_cover": candidate.cover,
                        "published_at": candidate.publish_time,
                        "result": RESULT_FAILED,
                        "reason": reason,
                    }
                )
                # DEF-097：账号级失败（风控 / 凭据）⇒ 剩下的**一篇都不再打**。
                if _looks_like_account_level_failure(str(msg)):
                    aborted = True
                    abort_reason = reason
                    _append_not_attempted(creator_items[index + 1 :], f"本批已中止（{reason}），未执行")
                    break
                continue
            # 推广中的笔记不删（风控红线）。
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict) and data.get("is_note_in_promotion"):
                skipped += 1
                details.append({"note_id": note_id, "ok": False, "reason": "笔记正在推广中，已跳过"})
                item_rows.append(
                    {
                        "note_id": note_id,
                        "note_title": candidate.title,
                        "note_cover": candidate.cover,
                        "published_at": candidate.publish_time,
                        "result": RESULT_SKIPPED,
                        "reason": "笔记正在推广中，已跳过",
                    }
                )
                continue

            ok, msg, _ = adapter.delete_note(note_id)
            if ok:
                deleted += 1
                _soft_delete_local(candidate)
                details.append({"note_id": note_id, "ok": True})
                item_rows.append(
                    {
                        "note_id": note_id,
                        "note_title": candidate.title,
                        "note_cover": candidate.cover,
                        "published_at": candidate.publish_time,
                        "result": RESULT_DELETED,
                        "reason": "",
                    }
                )
            else:
                # 2026-10-12：「笔记不存在」= 平台侧已无此笔记 ⇒ 等价已删，只软删本地。
                if _looks_like_note_absent(str(msg)):
                    deleted += 1
                    _soft_delete_local(candidate)
                    details.append({"note_id": note_id, "ok": True, "reason": "平台已不存在该笔记，仅本地软删除"})
                    item_rows.append(
                        {
                            "note_id": note_id,
                            "note_title": candidate.title,
                            "note_cover": candidate.cover,
                            "published_at": candidate.publish_time,
                            "result": RESULT_DELETED,
                            "reason": "平台已不存在该笔记，仅本地软删除",
                        }
                    )
                    continue
                failed += 1
                details.append({"note_id": note_id, "ok": False, "reason": msg})
                item_rows.append(
                    {
                        "note_id": note_id,
                        "note_title": candidate.title,
                        "note_cover": candidate.cover,
                        "published_at": candidate.publish_time,
                        "result": RESULT_FAILED,
                        "reason": str(msg),
                    }
                )
                if _looks_like_account_level_failure(str(msg)):
                    aborted = True
                    abort_reason = str(msg)
                    _append_not_attempted(creator_items[index + 1 :], f"本批已中止（{msg}），未执行")
                    break
        except Exception as exc:  # noqa: BLE001 - 单篇失败不中断整批
            failed += 1
            details.append({"note_id": note_id, "ok": False, "reason": str(exc)})
            item_rows.append(
                {
                    "note_id": note_id,
                    "note_title": candidate.title,
                    "note_cover": candidate.cover,
                    "published_at": candidate.publish_time,
                    "result": RESULT_FAILED,
                    "reason": str(exc),
                }
            )
            if _looks_like_account_level_failure(str(exc)):
                aborted = True
                abort_reason = str(exc)
                _append_not_attempted(creator_items[index + 1 :], f"本批已中止（{exc}），未执行")
                break

    # ⚠️ `note` 参数名自此**只承载覆盖度文案**（信息性备注）—— 失败原因由
    #    `coverage`/`failed` 分开传（DEF-095）。`_finish` 把两者分别写进 payload：
    #    `note`（兼容既有读方：`GET .../cleanup/{task_id}` 与旧前端）与
    #    `coverage_note`（新语义）。
    _finish(
        task,
        deleted=deleted,
        skipped=skipped,
        failed=failed,
        total=len(items),
        details=details,
        note=candidates["coverage"]["reason"],
        aborted=aborted,
        abort_reason=abort_reason,
    )
    db.commit()
    # 笔记维度明细：**唯一权威落库点**（用户口径「主体应是笔记维度」）。
    # 放在 `_finish` 之后、`db.commit()` 之前的位置**不改** batch 行的事务边界 ——
    # 明细写失败只告警（清理本身已完成，不许因记账问题回滚平台写操作的结论）。
    _record_items(
        db,
        log=log,
        task_id=task.id,
        account_id=account_id,
        user_id=task.user_id,
        rows=item_rows,
    )
    return {
        "total": len(items),
        "deleted": deleted,
        "skipped": skipped,
        "failed": failed,
        "aborted": aborted,
        "abort_reason": abort_reason,
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
    aborted: bool = False,
    abort_reason: str = "",
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
            #: ⚠️ **兼容键**：历史读方（与 2026-10-06 之前的语义）把它当「备注」。
            #: 新语义下它是**候选池覆盖度说明**，不是失败原因（DEF-095）。
            "note": note,
            "coverage_note": note,
            "aborted": bool(aborted),
            "abort_reason": abort_reason,
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
    """在**独立线程**里跑清理，立即返回首个任务 id（HTTP 请求不阻塞）。

    2026-10-13 拆分：`note_ids` 超过 `MAX_NOTES_PER_BATCH`（500）时**自动按 500 切片**，
    每个切片 = 一个子批次（独立 task + 独立线程 + 独立日志行），`batch_no` =
    `{时间戳基号}-{自增序号}`（如 `20261013_003000-1` / `-2`）。返回**首个子批次**的
    task id（前端据此轮询；同一账号的后续子批次由各线程自行推进）。

    ⚠️ `note_ids` 为空 = 全量清理（按指标区间），不切片、单批。
    """
    import threading

    query = query or CleanupQuery()
    ids = list(note_ids or [])
    # 空 note_ids（全量清理）= 单片；非空按 500 切片。
    chunks: list[list[str]] = (
        [ids[i : i + MAX_NOTES_PER_BATCH] for i in range(0, len(ids), MAX_NOTES_PER_BATCH)]
        if ids
        else [[]]
    )
    # 唯一批次基号（同一次清理的所有子批次共享；`-N` 后缀区分子批次）。
    base_no = shanghai_now().strftime("%Y%m%d%H%M%S")
    first_task_id: Optional[int] = None

    for index, chunk in enumerate(chunks, start=1):
        batch_no = f"{base_no}-{index}" if len(chunks) > 1 else base_no
        chunk_note_ids: Optional[list[str]] = chunk if ids else None

        task_db = SessionLocal()
        try:
            existing = _running_cleanup(task_db, account_id)
            if existing is not None:
                # 已有未完成任务 ⇒ 后续切片全部跳过（防重入）。
                if first_task_id is None:
                    first_task_id = existing.id
                continue
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
                    "note_ids": chunk_note_ids or [],
                    "batch_no": batch_no,
                    "source": "manual",
                },
            )
            task_db.add(task)
            task_db.commit()
            task_db.refresh(task)
            task_id = task.id
            if first_task_id is None:
                first_task_id = task_id
        finally:
            task_db.close()

        thread = threading.Thread(
            target=_run_in_thread,
            args=(account_id, thresholds, query, batch_limit, task_id, user_id, chunk_note_ids, batch_no),
            name=f"no-traffic-cleanup-{account_id}-{index}",
            daemon=True,
        )
        thread.start()

    return first_task_id or 0


def _run_in_thread(
    account_id: int,
    thresholds: Optional[TrafficThresholds],
    query: Optional[CleanupQuery],
    batch_limit: int,
    task_id: int,
    user_id: Optional[int],
    note_ids: Optional[list[str]] = None,
    batch_no: str = "",
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
    job_log: Optional[ScheduledJobLog] = None
    try:
        task = db.get(Task, task_id)
        if task is None:
            return
        # 「清理前」必须在**开跑之前**算（跑完再算就已经变了）—— 建任务行的那个函数
        # 不算这一步（它只写 payload、不做只读查询），所以在这里补。
        before_total = _pool_before_total(db, account_id, thresholds, query)
        _mutate_task_payload(task, before_total=before_total)
        db.commit()
        # 2026-10-13 用户口径：清理任务也要落**定时任务日志**（任务中心可见）。
        # 手动触发、无 scheduled_jobs 行 ⇒ job_id=None，job_name/key 用快照字符串。
        job_log = ScheduledJobLog(
            job_id=None,
            user_id=user_id,
            platform_account_id=account_id,
            task_id=task_id,
            job_name="清理无流量笔记",
            job_key="manual.no_traffic_cleanup",
            job_trigger_type="manual",
            trigger_type="manual",
            status=LOG_RUNNING,
            started_at=shanghai_now(),
            progress=0,
        )
        db.add(job_log)
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
            batch_no=batch_no,
        )
        if log is not None:
            # 挂到任务行上：`GET .../cleanup/active` 回它，前端关掉弹窗后仍能接住进度。
            _mutate_task_payload(task, log_id=log.id)
            db.commit()
        done = _cleanup_with_task(
            db, task, account_id, thresholds, query, batch_limit, note_ids=note_ids, log=log
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
            # DEF-095：同同步路径 —— `reason` 是失败原因，覆盖度另走 `coverage_note`。
            reason=_first_failure_reason(list(payload.get("details") or []))
            or str(payload.get("abort_reason") or ""),
            coverage_note=str(payload.get("coverage_note") or ""),
            aborted=bool(payload.get("aborted")),
            abort_reason=str(payload.get("abort_reason") or ""),
        )
        # 收尾定时任务日志：成功/失败 → 终态 + 进度 + 结果摘要。
        if job_log is not None:
            _close_job_log(
                db,
                job_log,
                ok=int(done.get("failed") or 0) == 0,
                message=f"删 {done.get('deleted', 0)} / 跳过 {done.get('skipped', 0)} / 失败 {done.get('failed', 0)}",
                exception=_first_failure_reason(list(payload.get("details") or []))
                or str(payload.get("abort_reason") or ""),
            )
    except Exception as exc:  # noqa: BLE001 - 后台线程不许把异常抛出去
        logger.error(f"no_traffic cleanup thread failed (task={task_id}): {exc}")
        _close_log(db, log, status=STATUS_FAILED, reason=str(exc))
        if job_log is not None:
            _close_job_log(db, job_log, ok=False, message="清理异常", exception=str(exc))
        _mark_failed(db, task_id, str(exc))
    finally:
        db.close()
