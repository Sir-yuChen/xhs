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

## 唯一的例外：账号卡的「同步」按钮（`run_targeted_job`，设计 13 §35.17）

上面说"所有调度执行的唯一入口"指的是**由本模块发起执行**的那条路。账号卡的
「同步笔记 / 同步商品」不发起执行 —— 同步的**执行方是账号任务 worker**（§35.16
单 worker 串行），端点自己经 `start_note_sync` 建行。本模块在那条路上只做两件事：
**记账**（建 `manual` 日志行）与**观测**（跟到业务任务终态再写日志终态）。

这么分是因为两边都试过且都错了：让本模块也调执行体 ⇒ 同一账号跑两遍；
让端点建完行就写 `success` ⇒ **几十毫秒的假成功**（§35.17.1 实测）。
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Any, Callable, Optional

from sqlalchemy import select
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

#: 进度泵的采样间隔（秒）。落在前端 5s 轮询之下 ⇒ 2s 足够让「任务进度」列有信息量，
#: 又不至于把库写爆（一次 30 分钟的同步 ≈ 900 次条件 UPDATE，且值没变就不写）。
PROGRESS_PUMP_INTERVAL_SECONDS = 2.0

#: 观测一个业务任务等待终态的轮询间隔（秒）。取与进度泵同值的理由：一次查库而已，
#: 且 2s 的终态延迟相对分钟级同步可忽略。
TASK_WATCH_POLL_SECONDS = 2.0


def _backoff_seconds(consecutive_failures: int) -> int:
    exponent = max(0, consecutive_failures - 1)
    return min(BACKOFF_BASE_SECONDS * (2**exponent), BACKOFF_MAX_SECONDS)


class _ProgressPump:
    """执行期间的进度心跳（设计 13 §35.17.3，2026-09-27 用户口径「任务执行中注意更新任务进度」）。

    为什么需要：`scheduled_job_logs.progress` 原先**只在终态**被写（成功 100 / 失败 0），
    而账号卡驱动的同步是**分钟级**的 —— 「任务进度」列在执行期间恒无信息。

    三条纪律（缺一条都会变成新 bug）：

    1. **只在日志行仍为 `running` 时写** —— 终态归 `_mark_success` / `_mark_failed`，
       泵不得覆盖（否则会出现「success 但 progress=40」的自相矛盾行）；
    2. `probe` 返回 `None` ⇒ **本次不写**（缺值口径，不造 0）；值没变也不写（省 UPDATE）；
    3. **进度只增不减**（2026-09-28 用户口径「**不允许出现 0→100 反复跳**」）：
       写之前与行上现值比，`percent <= 现值` 一律不写。为什么必须在**写侧**钉：
       进度是「本任务已完成的工作量占比」，任何会让它**倒退**的输入（分母在中途变大、
       阶段切换换了一套分子分母）都是**说谎**；写侧单调是唯一一处能对所有探针生效的闸门
       （任务行的 `payload.progress` 由执行体写，泵只是消费者）；
    4. 一切异常只 `logger.debug` —— 进度是**观测**，任何情况下不得影响执行本身。

    ⚠️ `stop()` 不 `join()`：daemon 线程最多多跑一个间隔，且 `_stop` 置位后不会再写库。
    执行体若已返回，日志行已进终态 ⇒ 第 1 条纪律天然拦住它。
    """

    def __init__(
        self,
        log_id: int,
        probe: Callable[[], Optional[int]],
        interval: float = PROGRESS_PUMP_INTERVAL_SECONDS,
    ):
        self.log_id = log_id
        self.probe = probe
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"progress-pump-{log_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                raw = self.probe()
            except Exception as exc:  # noqa: BLE001 - 观测不得影响执行
                logger.debug("progress probe failed for log %s: %s", self.log_id, exc)
                continue
            if raw is None:
                continue
            try:
                percent = max(0, min(100, int(raw)))
            except (TypeError, ValueError):
                continue
            db: Session = SessionLocal()
            try:
                row = db.get(ScheduledJobLog, self.log_id)
                if row is None or row.status != LOG_RUNNING:
                    continue
                # ⚠️ **只增不减**（纪律 3）：`percent <= 现值` ⇒ 不写。
                # 判据用「现值」而不是 `== percent`，两者一起管住（倒退与相等都不写）。
                if percent <= int(row.progress or 0):
                    continue
                row.progress = percent
                db.commit()
            except Exception as exc:  # noqa: BLE001 - 同上
                logger.debug("progress write failed for log %s: %s", self.log_id, exc)
                db.rollback()
            finally:
                db.close()


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


def _new_log_row(
    db: Session,
    job: ScheduledJob,
    *,
    trigger_type: str,
    account_id: Optional[int] = None,
    task_id: Optional[int] = None,
) -> ScheduledJobLog:
    log = ScheduledJobLog(
        job_id=job.id,
        # 归属快照（DEF-066）：任务删了历史日志仍能按用户收口；不 join scheduled_jobs
        user_id=job.user_id,
        # 账号快照（迁移 `j1k2l3m4n5o6`）：账号卡「同步中」= 该账号**最新一条**日志的
        # 状态（2026-09-27 用户口径）。非账号级任务（cron 全量、系统作业）传 None。
        account_id=account_id,
        # 业务任务号快照（迁移 `k2l3m4n5o6p7`）：**孤儿日志收口**要靠它精确回查任务行
        # 真实终态 —— 进程重启会带走观测线程，日志若停在 `running` 就再没人写终态，
        # 账号卡会一直显示「同步中」而任务其实早已结束（2026-09-27 实测）。
        task_id=task_id,
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


def _mark_failed(
    db: Session,
    job: ScheduledJob,
    log: ScheduledJobLog,
    exc: BaseException | str,
    *,
    message: str,
    progress: Optional[int] = None,
) -> None:
    """失败三件套：日志终态 + 计数 + 退避门（调用方负责 commit）。

    ⚠️ `at` 一次性任务**不进退避门**（设计 15 §6.5）：date 触发器用掉后不会再响，
    写了 `backoff_until` 只会把用户随后的「执行一次」也挡在门外。

    ⚠️ **失败不把进度清零**（2026-09-27 用户口径「任务执行失败后任务进度不要变，
    应展示执行的进度比例」）：旧实现无条件 `log.progress = 0`，用户看到进度条被打回
    0%（既不是「没跑」也不是「跑完了」，是最没信息量的一个数）。现在：

    * 调用方给了 `progress`（观测线程从任务行终态算出来的百分比）⇒ 用它；
    * 没给 ⇒ **原样保留**泵最后一次写入的值（可能仍是 0，那说明执行期间也没上报过
      进度 —— 缺值口径，不猜）。
    """
    finished = shanghai_now()
    log.status = LOG_FAILED
    log.finished_at = finished
    log.duration_ms = max(0, int((finished - log.started_at).total_seconds() * 1000))
    if progress is not None:
        log.progress = max(0, min(100, int(progress)))
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
    progress_probe: Optional[Callable[[], Optional[int]]] = None,
) -> dict[str, Any]:
    """执行一个任务行并写日志。返回 `{"log_id": …, "status": …}` 摘要。

    :param trigger_type: `scheduled`（调度触发）/ `manual`（执行一次）。
    :param params_override: 调用方覆盖参数（如手动“立即执行”强制
        `publish_mode="immediate"`），与行上 `params` 合并后传入注册表函数。
    :param log_id: 调用方预建的 running 日志行 id（手动路径为了能马上把
        日志 id 返回前端而预建）；不传则本函数自建。
    :param progress_probe: 可选的进度探针（设计 13 §35.17.3）：执行期间每
        `PROGRESS_PUMP_INTERVAL_SECONDS` 调一次，取 0–100 写进日志行 `progress`。
        不传 ⇒ 行为与改造前**逐字一致**（只在终态写 progress）。
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
        # 进度心跳（§35.17.3）：泵**必须在写终态之前停** —— 否则会出现
        # 「已 success / failed 又被泵改成 40」的自相矛盾行（泵自己也有 running 判定兜底）。
        pump = _ProgressPump(active_log_id, progress_probe) if progress_probe is not None else None
        if pump is not None:
            pump.start()
        try:
            payload = spec.func(**call_kwargs, **params)
        except Exception as exc:  # noqa: BLE001 - 调度路径必须吞异常并留痕
            logger.warning("scheduled job %s (%s) failed: %s", job.id, job.job_key, exc)
            _mark_failed(db, job, log, exc, message="执行失败")
            db.commit()
            return {"log_id": active_log_id, "status": LOG_FAILED}
        finally:
            if pump is not None:
                pump.stop()

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
    progress_probe: Optional[Callable[[], Optional[int]]] = None,
) -> int:
    """后台线程执行（手动“执行一次”路径），返回预建的 running 日志行 id。

    :param params_override: 定向执行用（设计 13 §35.17.2）—— 账号卡按钮借此把
        `account_ids` 收窄到单个账号。**代码侧内部注入**，不经 API 的 params 白名单。
    :param progress_probe: 可选的进度探针（§35.17.3），执行期间写日志行 `progress`。
    """
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
            execute_job(
                job_id,
                trigger_type=trigger_type,
                params_override=params_override,
                log_id=log_id,
                progress_probe=progress_probe,
            )
        except Exception as exc:  # noqa: BLE001 - 线程内兜底，绝不让异常裸奔
            logger.error("background run of job %s crashed: %s", job_id, exc)

    threading.Thread(target=_worker, name=f"scheduled-job-{job_id}", daemon=True).start()
    return log_id

def run_targeted_job(
    *,
    user_id: int,
    job_key: str,
    task_id: int,
    task_type: str,
    account_id: Optional[int] = None,
    params_extra: Optional[dict[str, Any]] = None,
    progress_probe: Optional[Callable[[], Optional[int]]] = None,
) -> int:
    """把一个**已建好的业务任务行**记成对应定时任务的「手动执行一次」（设计 13 §35.17）。

    账号卡的「同步笔记」/「同步商品」走这里。**执行不由本函数发起** —— 任务行由调用方
    （端点）经 `start_note_sync` / `start_shop_goods_sync` 建好并交给账号任务 worker；
    本函数只负责记账与观测：

    * **同步**建一行 `scheduled_job_logs`（`trigger_type=manual`）⇒ 任务中心「任务日志」
      立刻看得到「正在跑」，而不是跑完才凭空冒出来；
    * 起一个**观测线程**：每 `TASK_WATCH_POLL_SECONDS` 把任务行进度写进日志 `progress`
      （§35.17.3），任务到终态后按**真实结果**写日志终态。

    为什么是「观测」而不是「执行」（两条都踩过）：

    * 若本函数自己再调一次执行体，同一个账号会被跑两遍 —— §35.16 的单 worker 串行
      要求「建行的一方」就是执行方，两边都建就是两倍平台额度；
    * 若它建完行立刻返回，`_mark_success` 会在**几十毫秒内**把日志写成 `success`，
      而同步其实才刚开始（§35.17.1 实测的「0–133ms 假成功」就是这么来的）。

    ⚠️ 调用方必须先确认「本次确实触发了新执行」（`reused=False`）。同一个任务行记两次账
    会让任务中心的执行次数翻倍，且两份日志的终态可能一个 success 一个 failed。

    ⚠️ `params_extra` 写进日志 `config_snapshot.params_override`：它记录**这次点击**
    实际生效的同步语义（模式 / 时间窗），任务行与日志各留一份才不会对不上。

    ⚠️ 传了 `account_id` ⇒ 快照里的 `params.account_ids` **收窄成这一个账号**
    （2026-09-27 用户口径「执行账号应是当前账号信息卡的店铺」）：快照要回答的是
    「**这次**跑的是谁」，而 job 行的 `[]`（本人全部账号）是**任务配置** ——
    详情弹窗照抄配置就会把一次单账号执行说成「全部账号」。

    :returns: 新建的日志行 id（前端可据此跳「任务日志」）。
    """
    from backend.app.services import scheduler_service

    db: Session = SessionLocal()
    try:
        # job 行**兜底播种**（幂等）：用户没进过任务中心也能点按钮，不因缺行而 500。
        scheduler_service.seed_builtin_jobs_for_user(db, user_id)
        job = db.scalars(
            select(ScheduledJob).where(
                ScheduledJob.user_id == user_id,
                ScheduledJob.job_key == job_key,
            )
        ).first()
        if job is None:
            raise LookupError(f"job {job_key!r} for user {user_id} not found after seeding")
        log = _new_log_row(
            db,
            job,
            trigger_type="manual",
            account_id=account_id,
            task_id=int(task_id),
        )
        # 快照里的「执行账号」必须回答**这一次跑的是谁**（2026-09-27 用户口径「执行账号应是
        # 当前账号信息卡的店铺」）：job 行上的 `account_ids` 是**任务配置**（空 = 本人全部
        # 账号），而账号卡按钮这次只跑了 `account_id` 这一个 —— 照抄配置就会让「执行详情」
        # 把执行账号显示成「全部账号」（用户实测报障）。
        # ⚠️ 收窄的是**快照**，不是 job 行：任务行仍按原配置留给下一次定时轮转。
        snapshot = dict(log.config_snapshot or {})
        if account_id is not None:
            params = dict(snapshot.get("params") or {})
            params["account_ids"] = [int(account_id)]
            snapshot["params"] = params
        if params_extra:
            snapshot["params_override"] = dict(params_extra)
        # 整体赋值而非原地改：JSON 列的原地变更不会进 SQLAlchemy 的脏检查。
        log.config_snapshot = snapshot
        db.commit()
        log_id = log.id
        job_id = job.id
    finally:
        db.close()

    # 进度心跳（§35.17.3）：泵在**独立线程**里按 task_id 自查库；`_watch_task_run` 负责停它。
    pump = _ProgressPump(log_id, progress_probe or _no_progress_probe)
    pump.start()
    threading.Thread(
        target=_watch_task_run,
        args=(log_id, job_id, task_id, pump, params_extra),
        name=f"task-watch-{task_id}",
        daemon=True,
    ).start()
    return log_id


def _no_progress_probe() -> Optional[int]:
    """没给探针时的默认：恒 `None` ⇒ 泵不写（缺值口径，不造 0）。"""
    return None


def _terminal_fallback_reason(status: str) -> str:
    """终态**没有真原因**时的兜底文案（2026-09-27 用户口径「无需展示任务状态 cancelled」）。

    旧实现拼 `f"任务状态 {status}"` ⇒ 用户在「任务进度」列看到的是 `任务状态 cancelled`
    这种**状态码原文**（英文、且没告诉用户发生了什么）。这里换成可读中文。

    ⚠️ 只兜底「连 `error` / `note` 都没有」这一种情况：任务 payload 里有真原因时
    **永远走不到这里**（失败三分法不变：真异常要给真原因）。
    """
    return {
        "cancelled": "已被用户取消",
        "timeout": "执行超时（任务仍在后台继续）",
        "missing": "任务记录已不存在",
    }.get(str(status or ""), "同步未成功")


def _failure_reason(terminal: dict[str, Any]) -> str:
    """业务任务终态 ⇒ 日志里的失败原因文案（**唯一实现**，可被单测直接驱动）。

    优先级：`error`（真异常）→ `note`（业务说明）→ `_terminal_fallback_reason(status)`。
    ⚠️ 兜底**绝不摆状态码原文**（旧实现的 `任务状态 cancelled` 就是从这里漏出去的）。
    """
    return (
        str(terminal.get("error") or "")
        or str(terminal.get("note") or "")
        or _terminal_fallback_reason(str(terminal.get("status") or ""))
    )


def _await_task_terminal(task_id: int) -> dict[str, Any]:
    """等业务任务行走到终态（**实现住在 `sync_task_service`**，这里只注入观测口径）。

    放那边的理由：`scheduler_service` 的定向执行也要同一种等待 —— 两个调用方各写一份
    「等终态 + 认哪三个状态算终态」的实现，迟早一边漏掉 `cancelled`（DEF-044）。

    ⚠️ **观测线程不做超时校验**（2026-09-27 用户口径「任务无需超时校验」）：
    `timeout_seconds=None` ⇒ **无限等**。理由是实测出来的 —— 一次「同步笔记」要把该账号
    的正文与商品**一次拿全**（页内联补齐 + 令牌刷新翻页），实测 **105 分钟仍在中途**是
    正常形态；旧的 60 分钟超时会把日志提前写成 `failed`（而那句失败文案自己写着「任务
    仍在后台继续执行」），于是用户同时看到「日志失败」与「卡片同步中」两个相反结论。
    """
    from backend.app.services import sync_task_service as sts

    return sts.await_task_terminal(
        task_id,
        timeout_seconds=None,
        poll_seconds=TASK_WATCH_POLL_SECONDS,
    )


def _watch_task_run(
    log_id: int,
    job_id: int,
    task_id: int,
    pump: _ProgressPump,
    params_extra: Optional[dict[str, Any]],
) -> None:
    """观测线程：等终态 → 停泵 → 写日志终态（**只写仍为 `running` 的日志行**）。

    `done` ⇒ `_mark_success`；其余（`failed` / `cancelled` / `missing` / `timeout`）
    ⇒ `_mark_failed`。**取消也算「没成功」**：用户点取消是显式动作，日志再报 success
    就是自相矛盾（与 `latest_finished_task` 含 cancelled 同一口径）。

    ⚠️ 日志行已被写成终态（进程被杀的恢复路径 / 任务中心手动改过）⇒ **不覆写**。
    """
    from backend.app.services import sync_task_service as sts

    try:
        # ⚠️ **超时 ≠ 终态**（2026-09-27 用户实测报出：「日志记录依旧是失败，为何实际
        # 任务未终止，状态与实际任务不一致」）。`await_task_terminal` 的 `timeout` 返回体
        # **自己就写着**「任务仍在后台继续执行」—— 那不是失败，是「还没跑完」。
        # 旧实现把它归入 `else`（failed）⇒ 长同步任务（一次拿全正文与商品，实测 105 分钟
        # 仍在中途）会在第 60 分钟被写一条「失败」日志，而任务行继续跑 ⇒ **日志说失败、
        # 账号卡说同步中**，两个相反的结论同时摆在用户面前。
        #
        # 现在：超时只**继续等**（每轮一个 timeout 周期，轮询开销可忽略），日志保持
        # `running`、进度泵继续写心跳 —— 直到任务行**真的**走到终态才写日志终态。
        # 「日志状态 = 实际任务状态」这条不变量由这里保证（用户口径「只有任务日志状态
        # 在『正在执行』，才展示同步中」）。
        while True:
            terminal = _await_task_terminal(task_id)
            if str(terminal.get("status")) != "timeout":
                break
    finally:
        pump.stop()

    db: Session = SessionLocal()
    try:
        job = db.get(ScheduledJob, job_id)
        log = db.get(ScheduledJobLog, log_id)
        if job is None or log is None or log.status != LOG_RUNNING:
            return
        result = {**terminal, "params_override": dict(params_extra or {})}
        if terminal.get("status") == sts.STATUS_DONE:
            _mark_success(db, job, log, result)
        else:
            reason = _failure_reason(terminal)
            # 失败**保留已执行的进度比例**（用户口径）：取任务行终态进度换算，
            # 拿不到（`total` 未知 / 没上报）⇒ 传 None，`_mark_failed` 保留泵的值。
            _mark_failed(
                db,
                job,
                log,
                reason,
                message="同步未成功",
                progress=sts.progress_percent(terminal.get("progress")),
            )
        db.commit()
    except Exception as exc:  # noqa: BLE001 - 后台线程，绝不让异常裸奔
        logger.error("watching task %s for log %s crashed: %s", task_id, log_id, exc)
        db.rollback()
    finally:
        db.close()
