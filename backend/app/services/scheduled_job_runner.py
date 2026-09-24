"""调度执行包装器（设计 15 §6.5）——**所有**调度执行的唯一入口。

职责（按顺序）：

1. 载入任务行（独立 session）；行不存在 ⇒ 静默返回（任务刚被删）；
2. **失败退避门**：`backoff_until` 未到 ⇒ 写 `skipped` 日志（duration=0）后返回，
   不触达平台（修设计 08 §2.2-2 的“失败无退避 ⇒ 60s 重试风暴”）；
3. 插日志行 `running`（执行**前**插：进程被杀会留下 running 行，UI 可见，
   比“悄悄没有日志”好；调用方可预建行并用 `log_id` 传入）；
4. 调注册表函数 `spec.func(**params)`：
   - 成功 ⇒ `run_count+1`、`consecutive_failures=0`、`backoff_until=NULL`；
   - 异常 ⇒ `fail_count+1`、`consecutive_failures+1`、
     `backoff_until = now + min(5min * 2^(n-1), 6h)`；
5. 日志行改终态（success/failed + message/exception_info/duration），
   刷新 `last_run_at` / `last_status`。

⚠️ **账号维度隔离（DEF-066，设计 15 §4.1.1）**：`spec.user_scoped=True` 的任务，
   调用时为 `spec.func(user_id=job.user_id, **params)` —— 行归属者由 runner 注入，
   执行函数按它收口账号范围（`PlatformAccount.user_id == user_id`）。行归属为 NULL
   （迁移前的历史行 / 脏数据）⇒ **写 failed 日志后返回**，不静默退化成「全库账号」
   （安全网不能条件性，DEF-048/056 同型）。日志行落 `user_id` 快照（任务删了仍可归属）。

⚠️ `next_run_at` 不在这里维护：触发的权威在调度器侧，列值仅作展示兜底，
   由注册/重注册/启动同步路径刷新（`scheduler_service`）。
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models.scheduled_job import (
    JOB_STATUS_COMPLETED,
    LOG_FAILED,
    LOG_RUNNING,
    LOG_SKIPPED,
    LOG_SUCCESS,
    TRIGGER_AT,
    ScheduledJob,
    ScheduledJobLog,
)
from backend.app.services.job_registry import get_job_spec, validate_params

logger = logging.getLogger(__name__)

#: 失败退避：首次 5 分钟，每次翻倍，上限 6 小时（调度正确性的一部分，不进 config）
BACKOFF_BASE_SECONDS = 5 * 60
BACKOFF_MAX_SECONDS = 6 * 60 * 60


def _backoff_seconds(consecutive_failures: int) -> int:
    exponent = max(0, consecutive_failures - 1)
    return min(BACKOFF_BASE_SECONDS * (2**exponent), BACKOFF_MAX_SECONDS)


def _config_snapshot(job: ScheduledJob) -> dict[str, Any]:
    """执行那一刻的任务配置快照（设计 15 §4.2，2026-09-24 用户口径）。

    日志详情要能回答“当时是怎么配的”——任务行之后被改 / 删都不影响历史日志。
    """
    return {
        "name": job.name,
        "job_key": job.job_key,
        "trigger_type": job.trigger_type,
        "cron_expression": job.cron_expression,
        "run_at": job.run_at.isoformat() if job.run_at else None,
        "misfire_policy": job.misfire_policy,
        "concurrent": job.concurrent,
        "params": job.params or {},
        "status": job.status,
        "is_builtin": bool(job.is_builtin),
        "remark": job.remark,
    }


def _new_log_row(db: Session, job: ScheduledJob, *, trigger_type: str) -> ScheduledJobLog:
    log = ScheduledJobLog(
        job_id=job.id,
        # 归属快照（DEF-066）：任务删了历史日志仍能按用户收口；不 join scheduled_jobs
        user_id=job.user_id,
        job_name=job.name,
        job_key=job.job_key,
        # 类型快照：日志按任务类型筛选的唯一依据（任务删了也能筛，设计 15 §4.2）
        job_trigger_type=job.trigger_type,
        trigger_type=trigger_type,
        status=LOG_RUNNING,
        started_at=shanghai_now(),
        config_snapshot=_config_snapshot(job),
    )
    db.add(log)
    db.flush()
    return log


def _mark_failed(db: Session, job: ScheduledJob, log: ScheduledJobLog, exc: BaseException | str, *, message: str) -> None:
    """失败三件套：日志终态 + 计数 + 退避门（调用方负责 commit）。

    ⚠️ `at` 一次性任务**不进退避门**（设计 15 §6.5）：date 触发器用掉后不会再响，
    写了 `backoff_until` 只会把用户随后的「执行一次」也挡在门外。
    """
    finished = shanghai_now()
    log.status = LOG_FAILED
    log.finished_at = finished
    log.duration_ms = max(0, int((finished - log.started_at).total_seconds() * 1000))
    # 进度：失败的执行停在 0（任务侧未上报进度即不猜值，R13）
    log.progress = 0
    log.message = message[:1000]
    log.exception_info = str(exc)[:2000]
    job.fail_count = (job.fail_count or 0) + 1
    job.consecutive_failures = (job.consecutive_failures or 0) + 1
    if job.trigger_type != TRIGGER_AT:
        job.backoff_until = shanghai_now() + timedelta(seconds=_backoff_seconds(job.consecutive_failures))
    job.last_run_at = shanghai_now()
    job.last_status = LOG_FAILED


def _mark_success(db: Session, job: ScheduledJob, log: ScheduledJobLog, payload: Any) -> None:
    finished = shanghai_now()
    log.status = LOG_SUCCESS
    log.finished_at = finished
    log.duration_ms = max(0, int((finished - log.started_at).total_seconds() * 1000))
    result = payload if isinstance(payload, dict) else None
    # 进度：成功即 100（R13）。任务侧若在 result 里上报 progress 则尊重它。
    reported = result.get("progress") if result else None
    log.progress = 100 if not isinstance(reported, int) else max(0, min(100, reported))
    message = "执行成功"
    if result and result.get("keyword"):
        message = f"执行成功（关键词：{result['keyword']}）"
    log.message = message[:1000]
    if result is not None:
        log.result = result
    job.run_count = (job.run_count or 0) + 1
    job.consecutive_failures = 0
    job.backoff_until = None
    job.last_run_at = shanghai_now()
    job.last_status = LOG_SUCCESS
    # `at` 一次性任务：成功后自动办结（设计 15 §6.5）。
    # 调度器侧 date 触发器用掉后 next_run_time=None 不会再响；行状态改为
    # completed 后，下次启动同步自然不再注册它。
    if job.trigger_type == TRIGGER_AT:
        job.status = JOB_STATUS_COMPLETED


def execute_job(
    job_id: int,
    *,
    trigger_type: str = "scheduled",
    params_override: Optional[dict[str, Any]] = None,
    log_id: Optional[int] = None,
) -> dict[str, Any]:
    """执行一个任务行并写日志。返回 `{"log_id": …, "status": …}` 摘要。

    :param trigger_type: `scheduled`（调度触发）/ `manual`（执行一次）。
    :param params_override: 调用方覆盖参数（如手动“立即执行”强制
        `publish_mode="immediate"`），与行上 `params` 合并后传入注册表函数。
    :param log_id: 调用方预建的 running 日志行 id（手动路径为了能马上把
        日志 id 返回前端而预建）；不传则本函数自建。
    """
    db: Session = SessionLocal()
    try:
        job = db.get(ScheduledJob, job_id)
        if job is None:
            return {"log_id": log_id, "status": "missing"}

        try:
            spec = get_job_spec(job.job_key)
        except KeyError:
            # 注册表里没有这个键（任务类型随发版被移除）：留痕后放弃，不静默。
            logger.warning("scheduled job %s has unknown job_key %r", job.id, job.job_key)
            return {"log_id": log_id, "status": "unknown_key"}

        now = shanghai_now()

        # 取（或建）日志行 ----------------------------------------------------- #
        if log_id is not None:
            log = db.get(ScheduledJobLog, log_id)
            if log is None:
                return {"log_id": log_id, "status": "missing_log"}
        else:
            log = _new_log_row(db, job, trigger_type=trigger_type)
        active_log_id = log.id

        # 失败退避门 ----------------------------------------------------------- #
        # ⚠️ 只挡**调度触发**：手动「执行一次」是用户的显式动作，不该被自动退避
        # 拦在门外（若依的执行一次同样不看错过策略）。
        if (
            trigger_type == "scheduled"
            and job.backoff_until is not None
            and now < job.backoff_until
        ):
            remaining = int((job.backoff_until - now).total_seconds())
            finished = shanghai_now()
            log.status = LOG_SKIPPED
            log.finished_at = finished
            log.duration_ms = 0
            log.message = f"失败退避中，{remaining} 秒后放行"
            job.last_run_at = now
            job.last_status = LOG_SKIPPED
            db.commit()
            return {"log_id": active_log_id, "status": LOG_SKIPPED}

        # 参数浅校验 ----------------------------------------------------------- #
        params = dict(job.params or {})
        if params_override:
            params.update(params_override)
        try:
            validate_params(spec, params)
        except ValueError as exc:
            _mark_failed(db, job, log, exc, message="参数校验失败")
            db.commit()
            return {"log_id": active_log_id, "status": LOG_FAILED}

        # 账号维度隔离门（DEF-066） --------------------------------------------- #
        # user_scoped 任务必须知道「行归属者」才能收口账号范围；行归属为 NULL
        # （迁移前历史行 / 脏数据）⇒ 响亮失败，不静默跑成「全库账号」。
        call_kwargs: dict[str, Any] = {}
        if spec.user_scoped:
            if job.user_id is None:
                message = "任务行未归属用户（user_id 为空），拒绝执行（账号维度隔离）"
                logger.warning("scheduled job %s (%s): %s", job.id, job.job_key, message)
                _mark_failed(db, job, log, message, message=message)
                db.commit()
                return {"log_id": active_log_id, "status": LOG_FAILED}
            call_kwargs["user_id"] = job.user_id

        # 调注册表函数 --------------------------------------------------------- #
        try:
            payload = spec.func(**call_kwargs, **params)
        except Exception as exc:  # noqa: BLE001 - 调度路径必须吞异常并留痕
            logger.warning("scheduled job %s (%s) failed: %s", job.id, job.job_key, exc)
            _mark_failed(db, job, log, exc, message="执行失败")
            db.commit()
            return {"log_id": active_log_id, "status": LOG_FAILED}

        _mark_success(db, job, log, payload)
        db.commit()
        return {"log_id": active_log_id, "status": LOG_SUCCESS}
    finally:
        db.close()


def run_scheduled_job(job_id: int) -> None:
    """APScheduler 入口（module 级函数：持久化 store 按 `module:name` 文本引用）。"""
    try:
        execute_job(job_id, trigger_type="scheduled")
    except Exception as exc:  # noqa: BLE001 - 调度器调用，必须吞异常
        logger.error("run_scheduled_job %s crashed: %s", job_id, exc)


def run_job_in_background(
    job_id: int,
    *,
    trigger_type: str = "manual",
    params_override: Optional[dict[str, Any]] = None,
) -> int:
    """后台线程执行（手动“执行一次”路径），返回预建的 running 日志行 id。"""
    db: Session = SessionLocal()
    try:
        job = db.get(ScheduledJob, job_id)
        if job is None:
            raise LookupError(f"Scheduled job {job_id} not found")
        log = _new_log_row(db, job, trigger_type=trigger_type)
        db.commit()
        log_id = log.id
    finally:
        db.close()

    def _worker() -> None:
        try:
            execute_job(job_id, trigger_type=trigger_type, params_override=params_override, log_id=log_id)
        except Exception as exc:  # noqa: BLE001 - 线程内兜底，绝不让异常裸奔
            logger.error("background run of job %s crashed: %s", job_id, exc)

    threading.Thread(target=_worker, name=f"scheduled-job-{job_id}", daemon=True).start()
    return log_id
