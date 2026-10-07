"""定时任务中心 API（设计 15 §8）——若依式任务管理 + 调度日志。

端点族（全部挂 `/api/schedule`，均需登录）：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/jobs` | 任务列表（筛选 + 分页，带调度器活值 next_run_time） |
| GET | `/targets` | 可调度目标清单（注册表全量 + 各目标已建任务数，R18） |
| POST | `/jobs` | 新建（job_key 白名单；触发器实校验） |
| GET | `/jobs/{id}` | 详情 + 最近 10 条日志 |
| PUT | `/jobs/{id}` | 修改（builtin 禁改 key/params） |
| DELETE | `/jobs/{id}` | 删除（builtin 禁删；日志保留、job_id 置空） |
| PATCH | `/jobs/{id}/status` | 暂停 / 恢复 |
| POST | `/jobs/{id}/run` | 执行一次（后台线程，返回日志行 id） |
| GET | `/logs` | 调度日志列表（筛选 + 分页） |
| DELETE | `/logs` | 按 id 批量删除 |
| DELETE | `/logs/clean` | 清空（可按 job_id 收窄） |
| POST | `/cron/validate` | cron 实解析 + 未来 3 次触发时间 |
| GET | `/status` | 调度器状态 + 全部 job 活值 + 计数 |

写路径统一顺序：**先 DB commit，后同步调度器**（`sync_job_registration`）；
调度器同步失败只记日志，下次启动自愈（`sync_jobs_from_db`）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.time import shanghai_now
from backend.app.models.scheduled_job import (
    LOG_STATUSES,
    TRIGGER_TYPES,
    ScheduledJob,
    ScheduledJobLog,
)
from backend.app.models import PlatformAccount, User
from backend.app.schemas.common import paginated
from backend.app.schemas.schedule import (
    CronValidateRequest,
    JobStatusChangeRequest,
    ScheduleIn,
    ScheduleValidationError,
    apply_schedule,
    validate_schedule,
)
from backend.app.services.job_registry import (
    get_job_spec,
    job_registry,
    validate_params,
)
from backend.app.services.schedule_triggers import (
    SCHEDULER_TIMEZONE,
    parse_cron_expression,
)
from backend.app.services.scheduler_service import (
    scheduler_thread_alive,
    sync_job_registration,
)
from backend.app.services.scheduled_job_runner import run_job_in_background

router = APIRouter(prefix="/schedule", tags=["schedule"])


# --------------------------------------------------------------------------- #
# 序列化与查找
# --------------------------------------------------------------------------- #
def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _serialize_job(job: ScheduledJob, live_next_run_at: Optional[datetime] = None) -> dict:
    # 任务类型声明（前端据此渲染自定义参数表单，不硬编码键名）
    try:
        spec = get_job_spec(job.job_key)
        params_editable = list(spec.params_editable)
    except KeyError:
        params_editable = []
    return {
        "id": job.id,
        "user_id": job.user_id,
        "name": job.name,
        "job_key": job.job_key,
        "trigger_type": job.trigger_type,
        "cron_expression": job.cron_expression,
        "run_at": _iso(job.run_at),
        "misfire_policy": job.misfire_policy,
        "concurrent": job.concurrent,
        "params": job.params or {},
        "params_editable": params_editable,
        "status": job.status,
        "remark": job.remark,
        "is_builtin": bool(job.is_builtin),
        "last_run_at": _iso(job.last_run_at),
        # 活值优先（调度器知道真正的下次触发）；列值兜底
        "next_run_at": _iso(live_next_run_at or job.next_run_at),
        "run_count": job.run_count,
        "fail_count": job.fail_count,
        "consecutive_failures": job.consecutive_failures,
        "backoff_until": _iso(job.backoff_until),
        "last_status": job.last_status,
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
    }


def serialize_log(log: ScheduledJobLog) -> dict:
    return {
        "id": log.id,
        "job_id": log.job_id,
        # 归属快照（DEF-066）：日志按用户隔离的唯一依据（任务删了仍在）
        "user_id": log.user_id,
        # 业务任务号（迁移 `k2l3m4n5o6p7`）：任务日志列表的「取消任务」按钮按它调
        # `POST /tasks/{id}/cancel`（2026-09-27 用户口径「任务日志列表操作列新增取消
        # 任务中断任务执行功能按钮和逻辑」）。`null` = 该日志没有对应业务任务行
        # （cron 系统作业 / 存量日志）⇒ 前端**不渲染**取消按钮（缺值口径：不猜一个 id，
        # 猜错就会去取消别人的任务）。
        "task_id": log.task_id,
        "job_name": log.job_name,
        "job_key": log.job_key,
        "trigger_type": log.trigger_type,
        "status": log.status,
        "started_at": _iso(log.started_at),
        "finished_at": _iso(log.finished_at),
        "duration_ms": log.duration_ms,
        # 任务进度百分比（R13：日志表「任务进度 + 耗时或异常」合并列渲染进度条）
        "progress": log.progress,
        "message": log.message,
        "exception_info": log.exception_info,
        "result": log.result or {},
        # 执行时的任务类型快照（日志表「任务类型」列与类型筛选的唯一依据）
        "job_trigger_type": log.job_trigger_type,
        # 执行时的配置快照（详情弹窗用；本列新增前的执行为 null）
        "config_snapshot": log.config_snapshot,
        "created_at": _iso(log.created_at),
    }


def _live_next_run_map(request: Request) -> dict[int, datetime]:
    """调度器里各 job 的活值 next_run_time（按行 id 索引）。"""
    scheduler = getattr(request.app.state, "scheduler", None)
    result: dict[int, datetime] = {}
    if scheduler is None:
        return result
    for job in scheduler.get_jobs():
        if not job.id.startswith("job_") or job.next_run_time is None:
            continue
        try:
            result[int(job.id[len("job_"):])] = job.next_run_time
        except ValueError:  # pragma: no cover - 旧形态 id 不匹配前缀
            continue
    return result


def _get_visible_job(db: Session, current_user: User, job_id: int) -> ScheduledJob:
    """可见即可操作：**只认本人的行**（DEF-066，设计 15 §4.1.1）。

    原实现放行 `user_id IS NULL` 的全局内置行（所有登录用户可见、可暂停、可删、
    可执行）—— 那就是多用户互相操作同一份数据的入口，已随全局行退役一并删除。
    别人的行 ⇒ 404（不区分「不存在」与「不属于你」，避免越权探测）。
    """
    job = db.get(ScheduledJob, job_id)
    if job is None or job.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="JOB_NOT_FOUND")
    return job


def _validate_account_ids(db: Session, current_user: User, account_ids: object) -> list[int]:
    """自定义参数 `account_ids` 校验：整数数组 + **只能选自己的存活 xhs 账号**。

    DEF-066（2026-09-24）：建（POST）与改（PUT）**两条路都过这里** —— 原实现只在
    PUT 的 `_apply_custom_params` 里调，于是可以建一条引用别人账号的任务行
    （`system.no_traffic_cleanup` 是平台删除操作 ⇒ 跨租户破坏性触发）。
    系统任务行按用户各持一份后，「账号归属按当前用户收口」仍是硬约束：
    行的 params 由行主写，执行时按行主收口，两边都不许引用别人的账号。

    2026-10-06 KV 改造：也接受**逗号分隔字符串**（`"1,2,3"`）—— 前端 KV 输入框
    提交多值用逗号分隔，这里统一解析成 int 数组再走归属校验。
    """
    if isinstance(account_ids, str):
        raw_values: list[object] = [part.strip() for part in account_ids.split(",") if part.strip()]
        account_ids = raw_values
    if not isinstance(account_ids, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ACCOUNT_IDS_INVALID: account_ids 必须是数组或逗号分隔字符串",
        )
    ids: list[int] = []
    for value in account_ids:
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="ACCOUNT_IDS_INVALID: 账号 id 必须是整数",
            )
        ids.append(value)
    if not ids:
        return []
    owned = set(
        db.scalars(
            select(PlatformAccount.id).where(
                PlatformAccount.id.in_(ids),
                PlatformAccount.user_id == current_user.id,
                PlatformAccount.deleted_at.is_(None),
            )
        ).all()
    )
    missing = [value for value in ids if value not in owned]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"ACCOUNT_NOT_OWNED: 账号 {missing} 不存在或不属于当前用户",
        )
    return ids


def _coerce_days(value: object) -> int:
    """`days` 参数解析（KV 提交字符串 → int，上限 90）。"""
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="DAYS_INVALID: days 必须是整数",
        )
    if days < 1 or days > 90:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="DAYS_OUT_OF_RANGE: days 必须在 1..90 之间",
        )
    return days


def _coerce_retention_days(value: object) -> int:
    """`retention_days` 参数解析（KV 提交字符串 → int）。"""
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="RETENTION_DAYS_INVALID: retention_days 必须是整数",
        )
    if days < 1 or days > 3650:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="RETENTION_DAYS_OUT_OF_RANGE: retention_days 必须在 1..3650 之间",
        )
    return days


def _validate_run_params(
    db: Session,
    current_user: User,
    job: ScheduledJob,
    incoming: dict,
) -> dict:
    """校验「执行一次」的参数覆盖，返回**已收敛**的 override（设计 22 §5）。

    与 `_apply_custom_params` 共用**同一把尺子**（白名单 + 归属 + 深校验），
    区别只有一条：本函数**不写回** `job.params` —— 参数只对这一次执行生效。

    白名单是**硬要求**（不只是 UI 约定）：`auto_task_id` / `workflow_id` 这类
    参数由代码拥有，放行等于让用户越权跑别人的任务。
    """
    try:
        spec = get_job_spec(job.job_key)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="JOB_KEY_UNSUPPORTED"
        ) from None
    editable = set(spec.params_editable)
    disallowed = [key for key in incoming if key not in editable]
    if disallowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"JOB_PARAMS_NOT_EDITABLE: 参数 {sorted(disallowed)} 由代码拥有，不可修改",
        )
    # 合并**行配置 + 本次覆盖**后整体深校验 —— 只校验覆盖部分会漏「行里本就有非法值」。
    merged: dict = dict(job.params or {})
    for key, value in incoming.items():
        if key == "account_ids":
            merged[key] = _validate_account_ids(db, current_user, value)
        elif key == "days":
            merged[key] = _coerce_days(value)
        elif key == "retention_days":
            merged[key] = _coerce_retention_days(value)
        else:
            merged[key] = value
    if spec.params_validator is not None:
        try:
            spec.params_validator(merged, db, current_user)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return merged


def _apply_custom_params(
    db: Session,
    current_user: User,
    job: ScheduledJob,
    incoming: dict,
) -> None:
    """把用户提交的参数键合并进行内 params（白名单收敛 + 深校验）。

    规则（设计 15 §4.1 / §5 / §4.1.1）：
    - 只有 `spec.params_editable` 里的键可写，其余一律 400
      `JOB_PARAMS_NOT_EDITABLE`（代码拥有的参数不许用户改）；
    - `account_ids` 过归属校验；合并后的整体过 `spec.params_validator` 深校验。
    """
    try:
        spec = get_job_spec(job.job_key)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="JOB_KEY_UNSUPPORTED"
        ) from None
    editable = set(spec.params_editable)
    disallowed = [key for key in incoming if key not in editable]
    if disallowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"JOB_PARAMS_NOT_EDITABLE: 参数 {sorted(disallowed)} 由代码拥有，不可修改",
        )
    merged = dict(job.params or {})
    for key, value in incoming.items():
        if key == "account_ids":
            merged[key] = _validate_account_ids(db, current_user, value)
        elif key == "days":
            merged[key] = _coerce_days(value)
        elif key == "retention_days":
            merged[key] = _coerce_retention_days(value)
        else:
            merged[key] = value
    if spec.params_validator is not None:
        try:
            spec.params_validator(merged, db, current_user)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    job.params = merged


def _validate_create_params(
    db: Session,
    current_user: User,
    spec,
    params: dict,
) -> dict:
    """新建行的 params 校验（DEF-066：与 PUT **同一把尺子**，两条路都收口）。

    原 `create_job` 只过 `validate_params`（必填键存在性）—— `account_ids`
    的归属校验**只在 PUT 有**，于是可以建一条引用别人账号的任务行
    （`system.no_traffic_cleanup` 是平台删除操作 ⇒ 跨租户破坏性触发）。
    现在：键白名单（`params_editable ∪ params_required`，代码拥有的键不可写）
    + `account_ids` 归属校验 + `params_validator` 深校验，三样与 PUT 对齐。
    """
    editable = set(spec.params_editable) | set(spec.params_required)
    disallowed = [key for key in params if key not in editable]
    if disallowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"JOB_PARAMS_NOT_EDITABLE: 参数 {sorted(disallowed)} 由代码拥有，不可写入",
        )
    cleaned = dict(params)
    if "account_ids" in cleaned:
        cleaned["account_ids"] = _validate_account_ids(db, current_user, cleaned["account_ids"])
    if "days" in cleaned:
        cleaned["days"] = _coerce_days(cleaned["days"])
    if "retention_days" in cleaned:
        cleaned["retention_days"] = _coerce_retention_days(cleaned["retention_days"])
    if spec.params_validator is not None:
        try:
            spec.params_validator(cleaned, db, current_user)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return cleaned


# --------------------------------------------------------------------------- #
# 任务管理
# --------------------------------------------------------------------------- #
@router.get("/jobs")
def list_jobs(
    request: Request,
    job_status: Optional[str] = Query(default=None, alias="status"),
    trigger_type: Optional[str] = None,
    keyword: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = select(ScheduledJob).where(ScheduledJob.user_id == current_user.id)
    if job_status:
        statement = statement.where(ScheduledJob.status == job_status)
    if trigger_type:
        statement = statement.where(ScheduledJob.trigger_type == trigger_type)
    if keyword:
        statement = statement.where(ScheduledJob.name.like(f"%{keyword.strip()}%"))
    jobs = db.scalars(
        statement.order_by(ScheduledJob.is_builtin.desc(), ScheduledJob.id.desc())
    ).all()
    live = _live_next_run_map(request)
    return paginated([_serialize_job(job, live.get(job.id)) for job in jobs], page, page_size)


@router.get("/targets")
def list_targets(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """可调度目标清单（R18，2026-09-24 用户口径）。

    注册表**全量**列出（内置 + 普通），每项附当前用户已建任务数 —— 前端据此
    在「调用目标」下拉里标注「已创建 N 个」，并允许**对同一目标多次新建**
    （同一目标可以有多条不同调度的任务行，互不冲突。

    ⚠️ 与 `POST /jobs` 的分工：本端点只**描述**目标，不替前端决定可不可建；
    真正的白名单校验仍在 `create_job`（未知键 400 `JOB_KEY_UNSUPPORTED`）。
    """
    # 已建任务数按 job_key 聚合一次查完（别 N+1；行数是十位数，全量载入无压力）
    counts: dict[str, int] = {}
    for key in db.scalars(
        select(ScheduledJob.job_key).where(ScheduledJob.user_id == current_user.id)
    ).all():
        counts[key] = counts.get(key, 0) + 1
    return {
        "items": [
            {
                "job_key": spec.key,
                "name": spec.name,
                "builtin": bool(spec.builtin),
                "allow_concurrent": bool(spec.allow_concurrent),
                "params_required": list(spec.params_required),
                "params_editable": list(spec.params_editable),
                "default_trigger_type": spec.default_trigger_type,
                "default_cron": spec.default_cron,
                "default_misfire_policy": spec.default_misfire_policy,
                "existing_count": counts.get(spec.key, 0),
            }
            for spec in job_registry().values()
        ]
    }


class JobCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    job_key: str = Field(min_length=1, max_length=128)
    schedule: Optional[ScheduleIn] = None
    params: dict = Field(default_factory=dict)
    remark: str = Field(default="", max_length=500)


#: 未显式提供 schedule 时的默认：每天 9:00、启用（若依新建任务同样给一份可跑的默认）
_DEFAULT_JOB_SCHEDULE = ScheduleIn(
    trigger_type="cron",
    cron_expression="0 0 9 * * *",
    misfire_policy="once",
    concurrent="forbid",
    enabled=True,
)


@router.post("/jobs")
def create_job(
    payload: JobCreateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        spec = get_job_spec(payload.job_key.strip())
    except KeyError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="JOB_KEY_UNSUPPORTED") from None
    # R18（2026-09-24 用户口径「调用目标应支持所有的任务目标」）：内置目标**也允许
    # 新建**。原先这里一律 400 `JOB_BUILTIN_PROTECTED`，导致下拉里只剩 1 项可选。
    # 内置性只约束**种子行**本身（禁删 / 禁改 key/params，见 `DELETE` / `PUT`），
    # 不约束「用户新建一条同目标、不同调度的任务行」—— 二者是并行的两条行。
    schedule = payload.schedule or _DEFAULT_JOB_SCHEDULE
    try:
        validate_schedule(schedule, registry_allows_concurrent=spec.allow_concurrent)
        validate_params(spec, payload.params)
        # DEF-066：建行与改行同一把尺子 —— 键白名单 + account_ids 归属校验
        payload.params = _validate_create_params(db, current_user, spec, payload.params)
    except (ScheduleValidationError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    job = ScheduledJob(
        user_id=current_user.id,
        name=payload.name.strip(),
        job_key=spec.key,
        params=payload.params,
        remark=payload.remark.strip(),
        # 用户口径（2026-09-24）「所有任务都是内置的」⇒ 新建行一律标记 builtin。
        # 原先写死 False，配合「内置不可删/不可建」形成两套待遇；现在待遇统一，
        # 该列只剩「这行是本系统管理的任务」这一层含义。
        is_builtin=True,
    )
    apply_schedule(job, schedule)
    db.add(job)
    db.commit()
    db.refresh(job)
    sync_job_registration(getattr(request.app.state, "scheduler", None), job)
    return _serialize_job(job, _live_next_run_map(request).get(job.id))


@router.get("/jobs/{job_id}")
def get_job(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_visible_job(db, current_user, job_id)
    result = _serialize_job(job)
    recent = db.scalars(
        select(ScheduledJobLog)
        .where(ScheduledJobLog.job_id == job.id)
        .order_by(ScheduledJobLog.created_at.desc(), ScheduledJobLog.id.desc())
        .limit(10)
    ).all()
    result["recent_logs"] = [serialize_log(log) for log in recent]
    return result


class JobUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    schedule: Optional[ScheduleIn] = None
    params: Optional[dict] = None
    remark: Optional[str] = Field(default=None, max_length=500)


@router.put("/jobs/{job_id}")
def update_job(
    job_id: int,
    payload: JobUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_visible_job(db, current_user, job_id)
    if payload.schedule is not None:
        try:
            spec = get_job_spec(job.job_key)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="JOB_KEY_UNSUPPORTED") from None
        try:
            validate_schedule(payload.schedule, registry_allows_concurrent=spec.allow_concurrent)
        except ScheduleValidationError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    if payload.params is not None:
        # 自定义参数走白名单收敛（builtin 与普通行同一把尺子：只有
        # spec.params_editable 里的键可写，account_ids 过归属校验）
        _apply_custom_params(db, current_user, job, payload.params)

    if payload.name is not None:
        job.name = payload.name.strip()
    if payload.remark is not None:
        job.remark = payload.remark.strip()
    trigger_changed = False
    if payload.schedule is not None:
        schedule = payload.schedule
        trigger_changed = (
            job.trigger_type != schedule.trigger_type
            or job.cron_expression != schedule.cron_expression.strip()
            or job.run_at != schedule.run_at
            or job.misfire_policy != schedule.misfire_policy
            or job.concurrent != schedule.concurrent
            or job.status != ("active" if schedule.enabled else "paused")
        )
        apply_schedule(job, schedule)
    db.commit()
    db.refresh(job)
    sync_job_registration(
        getattr(request.app.state, "scheduler", None), job, trigger_changed=trigger_changed
    )
    return _serialize_job(job, _live_next_run_map(request).get(job.id))


@router.delete("/jobs/{job_id}")
def delete_job(
    job_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_visible_job(db, current_user, job_id)
    # 用户口径（2026-09-24）：「所有任务都是内置的」⇒ 内置不再是一种特权，
    # 一律可删（原先这里 400 `JOB_BUILTIN_PROTECTED`）。原文保留：
    #     if job.is_builtin:
    #         raise HTTPException(400, "JOB_BUILTIN_PROTECTED: 内置任务禁止删除（可暂停）")
    # 日志保留（若依同）：只把 job_id 置空，快照字段仍在。
    # ⚠️ SQLite 未开 foreign_keys PRAGMA，FK 的 ON DELETE SET NULL 不会自动触发，
    # 这里显式置空；MySQL 生产环境有 FK 兜底（双保险，不是两份实现）。
    db.execute(
        update(ScheduledJobLog).where(ScheduledJobLog.job_id == job.id).values(job_id=None)
    )
    db.delete(job)
    db.commit()
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None and scheduler.running:
        try:
            scheduler.remove_job(f"job_{job.id}")
        except Exception:  # noqa: BLE001 - 没注册过本就是正常路径
            pass
    return {"id": job_id, "status": "deleted"}


@router.patch("/jobs/{job_id}/status")
def change_job_status(
    job_id: int,
    payload: JobStatusChangeRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_visible_job(db, current_user, job_id)
    job.status = payload.status
    db.commit()
    db.refresh(job)
    sync_job_registration(getattr(request.app.state, "scheduler", None), job)
    return _serialize_job(job, _live_next_run_map(request).get(job.id))


class RunJobRequest(BaseModel):
    """「执行一次」请求体（设计 22 §5）。

    `params` 缺省 = 直接按任务行配置执行（无参数任务走这条路，前端二次确认即可）。
    传了就**只对这一次**生效，不写回任务行 —— 用户改的是「这次用什么参数跑」，
    不是「把定时配置改掉」。
    """

    params: Optional[dict] = None


@router.post("/jobs/{job_id}/run")
def run_job_once(
    job_id: int,
    body: Optional[RunJobRequest] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """执行一次（若依「执行一次」）：后台线程跑，立即返回日志行 id。

    **2026-10-06 设计 22 §5 新增**：可带 `params` 覆盖本次执行的参数 ——
    例如执行账号（`account_ids`）、记录表保留天数（`retention_days`）都能在
    手动触发前改成这次要用的值。

    | 约定 | 做法 |
    |---|---|
    | 白名单 | `params` 只允许 `spec.params_editable` 里的键，表外 ⇒ 400 `JOB_PARAMS_NOT_EDITABLE` |
    | 归属 | `account_ids` 过账号归属校验（`_validate_account_ids`） |
    | 深校验 | 行配置 + 本次覆盖**合并后**过 `spec.params_validator`（如 `retention_days` 范围） |
    | **不写回** | 覆盖参数**不 commit 回 `job.params`**，只用于本次（下一次定时/手动仍按行配置） |
    """
    job = _get_visible_job(db, current_user, job_id)
    override: Optional[dict] = None
    if body is not None and body.params:
        override = _validate_run_params(db, current_user, job, body.params)
    log_id = run_job_in_background(job.id, trigger_type="manual", params_override=override)
    return {"log_id": log_id, "status": "running"}


# --------------------------------------------------------------------------- #
# 调度日志
# --------------------------------------------------------------------------- #
@router.get("/logs")
def list_logs(
    job_id: Optional[int] = None,
    job_key: Optional[str] = None,
    trigger_type: Optional[str] = None,
    log_status: Optional[str] = Query(default=None, alias="status"),
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """日志列表（DEF-066：按 `user_id` 快照列收口 —— 只见本人任务的执行日志）。

    筛选：`job_id` / `job_key` / `status` / `trigger_type`（`cron`/`at`，R17 不含
    `manual`）/ 时间范围 + 分页。原「全库日志零过滤」的形态已废：调度日志含别人
    任务的 message / result / 配置快照，跨用户可见即泄漏。
    """
    statement = select(ScheduledJobLog).where(
        ScheduledJobLog.user_id == current_user.id
    )
    if job_id is not None:
        statement = statement.where(ScheduledJobLog.job_id == job_id)
    if job_key:
        statement = statement.where(ScheduledJobLog.job_key == job_key)
    if trigger_type:
        # 按**任务类型**筛选（快照列口径：任务删了历史日志也能筛；空快照的旧日志
        # 不进任何类型结果，缺值口径不猜）
        if trigger_type not in TRIGGER_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="TRIGGER_TYPE_UNSUPPORTED"
            )
        statement = statement.where(ScheduledJobLog.job_trigger_type == trigger_type)
    if log_status:
        if log_status not in LOG_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="LOG_STATUS_UNSUPPORTED"
            )
        statement = statement.where(ScheduledJobLog.status == log_status)
    if date_from is not None:
        statement = statement.where(ScheduledJobLog.created_at >= date_from)
    if date_to is not None:
        statement = statement.where(ScheduledJobLog.created_at <= date_to)
    logs = db.scalars(
        statement.order_by(ScheduledJobLog.created_at.desc(), ScheduledJobLog.id.desc())
    ).all()
    return paginated([serialize_log(log) for log in logs], page, page_size)


class LogDeleteRequest(BaseModel):
    ids: list[int] = Field(min_length=1)


@router.delete("/logs")
def delete_logs(
    payload: LogDeleteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """按 id 批量删除（DEF-066：只删**本人**日志 —— 越权 id 静默跳过）。

    行整条删，不涉及孤儿（删了就没有了）。
    """
    logs = db.scalars(
        select(ScheduledJobLog).where(
            ScheduledJobLog.id.in_(payload.ids),
            ScheduledJobLog.user_id == current_user.id,
        )
    ).all()
    for log in logs:
        db.delete(log)
    db.commit()
    return {"requested": len(payload.ids), "deleted": len(logs)}


@router.delete("/logs/clean")
def clean_logs(
    job_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """清空调度日志（可按 job_id 收窄；DEF-066：只清**本人**的，缺省清本人全部）。"""
    statement = select(ScheduledJobLog).where(
        ScheduledJobLog.user_id == current_user.id
    )
    if job_id is not None:
        statement = statement.where(ScheduledJobLog.job_id == job_id)
    deleted = 0
    for log in db.scalars(statement).all():
        db.delete(log)
        deleted += 1
    db.commit()
    return {"deleted": deleted}


# --------------------------------------------------------------------------- #
# cron 校验与调度器状态
# --------------------------------------------------------------------------- #
@router.post("/cron/validate")
def validate_cron(payload: CronValidateRequest, current_user: User = Depends(get_current_user)):
    """cron 实解析（唯一校验器）+ 未来 3 次触发时间（上海时间 naive）。"""
    try:
        trigger = parse_cron_expression(payload.cron_expression)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"CRON_INVALID: {exc}") from None
    # ⚠️ 迭代预览必须把上一次结果当下限（`prev or now`）：APScheduler 的
    # `get_next_fire_time(previous, now)` 在 previous 晚于 now 时会**退回从 now
    # 起算**（防时钟跳变的内部逻辑），直接拿固定 now 迭代会拿到三个相同时间。
    now = shanghai_now()
    times: list[str] = []
    previous = None
    for _ in range(3):
        nxt = trigger.get_next_fire_time(previous, previous or now)
        if nxt is None:
            break
        times.append(nxt.replace(tzinfo=None).isoformat())
        previous = nxt
    return {"valid": True, "next_fire_times": times}


@router.get("/status")
def schedule_status(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """调度器状态（DEF-066：计数与活值均按**当前用户行**收口）。

    调度器本身是进程级单例（所有用户的任务都注册在同一个 scheduler 里），但
    「我的任务中心」只应看到自己的计数与下次运行；别人的 job id 不外泄。
    """
    from backend.app.core.config import get_settings

    settings = get_settings()
    scheduler = getattr(request.app.state, "scheduler", None)
    # 本人的行 id 集合（计数 + 活值过滤共用）
    my_ids = set(
        db.scalars(
            select(ScheduledJob.id).where(ScheduledJob.user_id == current_user.id)
        ).all()
    )
    jobs = []
    if scheduler is not None:
        for job in scheduler.get_jobs():
            if not job.id.startswith("job_"):
                continue
            try:
                row_id = int(job.id[len("job_"):])
            except ValueError:  # pragma: no cover - 旧形态 id 不匹配前缀
                continue
            if row_id not in my_ids:
                continue
            jobs.append({"id": job.id, "next_run_time": _iso(job.next_run_time)})
    # 状态计数一次查完（别 N+1；行数是十位数，全量载入无压力）
    counts: dict[str, int] = {}
    for job in db.scalars(
        select(ScheduledJob).where(ScheduledJob.user_id == current_user.id)
    ).all():
        counts[job.status] = counts.get(job.status, 0) + 1
    return {
        "enabled": bool(settings.scheduler_enabled),
        # ⚠️ 别用 `scheduler.running`（DEF-067）：3.11 的 `state` 只在 `shutdown()`
        # 里改，主循环线程因异常死掉后它**仍是 True** ⇒ 这里会报「运行中」而作业
        # 一个都不跑（2026-09-25 实测静默停摆 14 分钟，状态端点毫无异常）。
        # 唯一可信的判据是「主循环线程在不在」（看门狗用的是同一把尺子）。
        "running": bool(scheduler is not None and scheduler_thread_alive()),
        "timezone": SCHEDULER_TIMEZONE,
        "jobs": jobs,
        "stats": {
            "total": sum(counts.values()),
            "active": counts.get("active", 0),
            "paused": counts.get("paused", 0),
            "completed": counts.get("completed", 0),
        },
    }
