"""自动运营任务的业务配置 API（设计 15 §7.1）。

⚠️ 2026-09-24 重构：**调度字段从本表移除**（`schedule_type` / `schedule_time` /
`schedule_days` / `schedule_interval_hours` 四列已删），统一改为可选 `schedule`
对象（`ScheduleIn`，与 `/api/schedule/jobs` 同一校验实现），落到 1:1 关联的
`scheduled_jobs` 行上。`manual` 语义由 job 行 `status=paused` 表达。

调度生效条件（与旧口径一致）：`auto_tasks.status == active` ∧ job 行
`status == active`（`schedule.enabled`）。业务暂停会连带暂停调度行。

「立即执行」不再创建 `tasks` 表行（`auto_ops_run`）——执行记录以调度日志
（`scheduled_job_logs`）为准，返回体同时带日志行与管线富详情。
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.schedule import serialize_log
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user, get_owned_active_account
from backend.app.models import AutoTask, ScheduledJob, ScheduledJobLog, User
from backend.app.schemas.common import paginated
from backend.app.schemas.schedule import (
    ScheduleIn,
    ScheduleValidationError,
    apply_schedule,
    validate_schedule,
)
from backend.app.services.job_registry import get_job_spec
from backend.app.services.scheduler_service import sync_job_registration
from backend.app.services.scheduled_job_runner import execute_job

router = APIRouter(prefix="/auto-tasks", tags=["auto-tasks"])

#: 自动运营管线的注册表键（params 带 auto_task_id；调度触发 publish_mode=scheduled，
#: 手动「立即执行」覆盖为 immediate —— 生成待发布任务，发布页审核后发）
AUTO_OPS_JOB_KEY = "auto_ops.keyword_pipeline"


class AutoTaskCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    keywords: list[str] = Field(min_length=1)
    pc_account_id: int
    creator_account_id: int
    ai_instruction: str = Field(default="", max_length=2000)
    #: 不传 = 不自动执行（等价旧的 schedule_type="manual"：建行即暂停）
    schedule: Optional[ScheduleIn] = None


class AutoTaskUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    keywords: Optional[list[str]] = None
    ai_instruction: Optional[str] = Field(default=None, max_length=2000)
    status: Optional[str] = Field(default=None, pattern="^(active|paused|completed)$")
    schedule: Optional[ScheduleIn] = None


def _verify_account_ownership(db: Session, current_user: User, account_id: int, expected_sub_type: str):
    """（薄别名）**唯一实现见 `core/deps.py::get_owned_active_account`**（DEF-055）。"""
    return get_owned_active_account(
        db,
        current_user,
        account_id,
        platform="xhs",
        sub_type=expected_sub_type,
        detail=f"XHS {expected_sub_type} account not found",
    )


def _get_owned_auto_task(db: Session, current_user: User, task_id: int) -> AutoTask:
    auto_task = db.scalars(
        select(AutoTask).where(AutoTask.id == task_id, AutoTask.user_id == current_user.id)
    ).first()
    if auto_task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Auto task not found")
    return auto_task


def _assert_can_toggle_auto_task(db: Session, current_user: User) -> None:
    """启停自动运营任务需要 `xhs:autoops:enable`（2026-10-26 G8）。

    为什么单独一个码：**「启用/停用」与「改任务内容」是两件事**。
    只想让某人按开关、不想让他改关键词与 AI 指令时，只勾 `enable` 就够 ——
    以前两者共用一个 `xhs:autoops:edit`，想给启停权就得连改内容的权一起给。

    与 `require_perm` 同源（`permission_cache.perms_of`），不另走一套查库。
    """
    from backend.app.core import permission_cache  # noqa: PLC0415
    from backend.app.core.perms import ROLE_ADMIN, normalize_role  # noqa: PLC0415

    if normalize_role(getattr(current_user, "role", None)) == ROLE_ADMIN:
        return
    if "xhs:autoops:enable" in permission_cache.perms_of(db, current_user):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "AUTO_TASK_ENABLE_PERM_REQUIRED",
            "message": "缺少「启用/停用自动运营任务」权限（xhs:autoops:enable）",
        },
    )


def _schedule_summary(job: Optional[ScheduledJob]) -> Optional[dict[str, Any]]:
    if job is None:
        return None
    return {
        "job_id": job.id,
        "trigger_type": job.trigger_type,
        "cron_expression": job.cron_expression,
        "run_at": job.run_at.isoformat() if job.run_at else None,
        "misfire_policy": job.misfire_policy,
        "concurrent": job.concurrent,
        "status": job.status,
        "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
        "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
        "run_count": job.run_count,
        "last_status": job.last_status,
    }


def _serialize_auto_task(task: AutoTask, job: Optional[ScheduledJob]) -> dict[str, Any]:
    return {
        "id": task.id,
        "user_id": task.user_id,
        "name": task.name,
        "keywords": task.keywords or [],
        "pc_account_id": task.pc_account_id,
        "creator_account_id": task.creator_account_id,
        "ai_instruction": task.ai_instruction,
        "status": task.status,
        "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
        "total_published": task.total_published,
        "created_at": task.created_at.isoformat(),
        "schedule": _schedule_summary(job),
    }


def _ensure_schedule_job(db: Session, task: AutoTask) -> ScheduledJob:
    """取（缺则建）1:1 调度行。手动-only 的老任务也建行，只是 paused。"""
    if task.schedule_job_id is not None:
        job = db.get(ScheduledJob, task.schedule_job_id)
        if job is not None:
            return job
    spec = get_job_spec(AUTO_OPS_JOB_KEY)
    job = ScheduledJob(
        user_id=task.user_id,
        name=f"自动运营 · {task.name}",
        job_key=spec.key,
        params={"auto_task_id": task.id, "publish_mode": "scheduled"},
        is_builtin=False,
        remark="自动运营管线（设计 15 §7.1；手动立即执行时 publish_mode 覆盖为 immediate）",
    )
    db.add(job)
    db.flush()
    task.schedule_job_id = job.id
    return job


def _apply_schedule(db: Session, task: AutoTask, schedule: ScheduleIn) -> ScheduledJob:
    """校验 + 落调度设置到 job 行（调用方负责 commit）。

    生效条件与旧口径一致：任务本身 `active` ∧ `schedule.enabled` —— 业务暂停的
    任务即使调度设置开着也不跑（`apply_schedule` 后按任务状态覆写）。
    """
    spec = get_job_spec(AUTO_OPS_JOB_KEY)
    try:
        validate_schedule(schedule, registry_allows_concurrent=spec.allow_concurrent)
    except ScheduleValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    job = _ensure_schedule_job(db, task)
    apply_schedule(job, schedule)
    if task.status != "active":
        job.status = "paused"
    return job


@router.get("")
def list_auto_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tasks = db.scalars(
        select(AutoTask)
        .where(AutoTask.user_id == current_user.id)
        .order_by(AutoTask.created_at.desc(), AutoTask.id.desc())
    ).all()
    job_ids = [task.schedule_job_id for task in tasks if task.schedule_job_id]
    jobs: dict[int, ScheduledJob] = {}
    if job_ids:
        for job in db.scalars(select(ScheduledJob).where(ScheduledJob.id.in_(job_ids))).all():
            jobs[job.id] = job
    return paginated(
        [_serialize_auto_task(task, jobs.get(task.schedule_job_id)) for task in tasks],
        page,
        page_size,
    )


@router.post("")
def create_auto_task(
    payload: AutoTaskCreateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _verify_account_ownership(db, current_user, payload.pc_account_id, "pc")
    _verify_account_ownership(db, current_user, payload.creator_account_id, "creator")

    auto_task = AutoTask(
        user_id=current_user.id,
        name=payload.name.strip(),
        keywords=payload.keywords,
        pc_account_id=payload.pc_account_id,
        creator_account_id=payload.creator_account_id,
        ai_instruction=payload.ai_instruction,
        status="active",
    )
    db.add(auto_task)
    db.flush()

    schedule = payload.schedule or ScheduleIn(
        trigger_type="cron", cron_expression="0 0 9 * * *", enabled=False
    )
    job = _apply_schedule(db, auto_task, schedule)
    db.commit()
    db.refresh(auto_task)
    db.refresh(job)
    sync_job_registration(getattr(request.app.state, "scheduler", None), job)
    return _serialize_auto_task(auto_task, job)


@router.patch("/{task_id}")
def update_auto_task(
    task_id: int,
    payload: AutoTaskUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """部分更新自动运营任务。

    ## 一个端点在按字段分派两个权限码（2026-10-26 G8）

    静态路由表只能挂一个码，而这里有两种性质完全不同的写：
    * 改 `name` / `keywords` / `ai_instruction` / `schedule` ⇒ **编辑**（`xhs:autoops:edit`）
    * 只改 `status`（启用/停用）⇒ **启停**（`xhs:autoops:enable`）

    路由表挂 `edit` 作为**下限**（有编辑权即可进来），`status` 单独一个码再判一次：
    只想让人启停、不想让人改任务内容时，不勾 edit 就只有 enable —— 这是「一按钮一码」
    在本端点的落地方式（同 `qianfan/items/operate` 的 `_assert_action_permitted`）。

    ⚠️ 为什么用 `permission_cache.perms_of` 而不是 `core.perms.has_perm`：
    与 `require_perm` 同一份真相源（查库 + TTL 缓存），且不受「非 admin 必须传 db」
    这个约定的调用形态约束。
    """
    auto_task = _get_owned_auto_task(db, current_user, task_id)

    #: 只改 status 且确实变了 ⇒ 需要启停码。与 edit 分开判，且**只判一次**。
    status_only_change = (
        payload.status is not None
        and payload.status != auto_task.status
        and all(
            getattr(payload, field) is None
            for field in ("name", "keywords", "ai_instruction", "schedule")
        )
    )
    if status_only_change:
        _assert_can_toggle_auto_task(db, current_user)

    if payload.name is not None:
        auto_task.name = payload.name.strip()
    if payload.keywords is not None:
        auto_task.keywords = payload.keywords
    if payload.ai_instruction is not None:
        auto_task.ai_instruction = payload.ai_instruction
    if payload.status is not None:
        auto_task.status = payload.status

    job = None
    if payload.schedule is not None:
        job = _apply_schedule(db, auto_task, payload.schedule)
    elif auto_task.schedule_job_id is not None:
        job = db.get(ScheduledJob, auto_task.schedule_job_id)
        if job is not None:
            if payload.name is not None:
                job.name = f"自动运营 · {auto_task.name}"
            # 业务状态联动调度（旧口径：run_due_auto_tasks 只跑 active 的任务）
            if payload.status is not None:
                job.status = "active" if auto_task.status == "active" else "paused"

    db.commit()
    if job is not None:
        db.refresh(job)
        sync_job_registration(
            getattr(request.app.state, "scheduler", None),
            job,
            trigger_changed=payload.schedule is not None,
        )
    db.refresh(auto_task)
    return _serialize_auto_task(auto_task, job)


@router.delete("/{task_id}")
def delete_auto_task(
    task_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    auto_task = _get_owned_auto_task(db, current_user, task_id)
    job = None
    if auto_task.schedule_job_id is not None:
        job = db.get(ScheduledJob, auto_task.schedule_job_id)
    if job is not None:
        # 日志保留：job_id 置空（SQLite 无 FK 级联，显式做；MySQL 有 FK 兜底）
        for log in db.scalars(select(ScheduledJobLog).where(ScheduledJobLog.job_id == job.id)).all():
            log.job_id = None
        db.delete(job)
    db.delete(auto_task)
    db.commit()
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None and scheduler.running and job is not None:
        try:
            scheduler.remove_job(f"job_{job.id}")
        except Exception:  # noqa: BLE001 - 没注册过本是正常路径
            pass
    return {"id": task_id, "status": "deleted"}


@router.post("/{task_id}/run")
def run_auto_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """立即执行（手动触发 ⇒ `publish_mode="immediate"`：生成待发布任务，发布页审核后发）。

    走统一执行入口（`execute_job`）：调度日志 + 计数一体；退避门只挡调度触发，
    手动执行永远放行。返回 `{auto_task, log, detail}`：detail 是管线富详情
    （keyword / source_note / draft / publish_job）。
    """
    auto_task = _get_owned_auto_task(db, current_user, task_id)
    _verify_account_ownership(db, current_user, auto_task.pc_account_id, "pc")
    _verify_account_ownership(db, current_user, auto_task.creator_account_id, "creator")

    if auto_task.schedule_job_id is None:
        # 防御：老数据没有 job 行（迁移漏网）——补一行 paused 的，只为挂日志
        _ensure_schedule_job(db, auto_task)
        db.commit()
        db.refresh(auto_task)

    outcome = execute_job(
        auto_task.schedule_job_id,
        trigger_type="manual",
        params_override={"publish_mode": "immediate"},
    )
    log = db.get(ScheduledJobLog, outcome["log_id"]) if outcome.get("log_id") else None
    if log is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=outcome.get("status") or "Auto task run failed",
        )
    if log.status == "failed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=log.exception_info or "Auto task run failed",
        )
    return {
        "auto_task": _serialize_auto_task(auto_task, db.get(ScheduledJob, auto_task.schedule_job_id)),
        "log": serialize_log(log),
        "detail": log.result or {},
    }
