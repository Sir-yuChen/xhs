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
from backend.app.schemas.schedule_responses import (
    JobTargetListResponse,
    ScheduledJobDetailResponse,
    ScheduledJobItem,
    ScheduledJobListResponse,
    ScheduledJobLogListResponse,
)
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
    workspace_for_job,
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
    #
    # ⚠️ 2026-10-11 修：原写法是
    #     `try: spec = get_job_spec(...); params_editable = list(spec.params_editable)`
    #     `except KeyError: params_editable = []`
    #   —— `spec` **只在 try 块里赋值**。走 except 分支后 `spec` 从未被绑定，
    #   紧接着那句 `dict(spec.params_defaults)` 会抛 **`UnboundLocalError`**
    #   （`NameError` 的子类 —— 实测报错原文：
    #   `cannot access local variable 'spec' where it is not associated with a value`），
    #   **不是 `KeyError`**，所以**不会被这个 except 接住** ⇒ 整个任务列表端点 500。
    #
    #   触发场景是真实存在的：`job_key` 被改名/整条退场后，**存量行**还指着旧键
    #   （本项目已发生过两次 —— `queue_record_cleanup` 改名、`workflow.*` 退场）。
    #   那种行本该被「跳过 or 如实标注」，却会把**整页任务中心**打挂。
    #   修法 = `spec` 先绑 `None`，两个派生字段各自守卫（缺 spec ⇒ 空，绝不猜）。
    try:
        spec = get_job_spec(job.job_key)
    except KeyError:
        #: 未知 job_key（存量脏行 / 已退场的任务）⇒ `spec` 保持 `None`，
        #: 下面两个字段退化成空；**不抛异常**，别让一行脏数据拖垮整页。
        spec = None
    params_editable = list(spec.params_editable) if spec is not None else []
    return {
        "id": job.id,
        #: 设计 69：`user_id` 已降级为"最后修改人"（内置行 NULL）；
        #: 归属看 `workspace`（system / xhs）。
        "user_id": job.user_id,
        "workspace": job.workspace,
        "name": job.name,
        "job_key": job.job_key,
        "trigger_type": job.trigger_type,
        "cron_expression": job.cron_expression,
        "run_at": _iso(job.run_at),
        "misfire_policy": job.misfire_policy,
        "concurrent": job.concurrent,
        "params": job.params or {},
        "params_editable": params_editable,
        # 2026-10-07 用户口径「调用参数不允许随意添加和删除，必须有默认值」：
        # 前端据 `params_defaults` **预填固定行** —— 存量行（早于某参数上线）缺的键
        # 也能显示出来，不会「新增了参数但界面上看不见」。
        "params_defaults": dict(spec.params_defaults) if spec is not None else {},
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
        #: 设计 69：`user_id` = 最后修改人快照；`executed_for_user_id` = 这次跑谁的数据。
        "user_id": log.user_id,
        "executed_for_user_id": log.executed_for_user_id,
        "workspace": getattr(log, "workspace", None),
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
    """可见即可操作；越权的判据 = **任务归属的工作区 vs 调用者的权限**。

    ## 设计 69 后的两层判据

    1. **系统级任务**（`workspace="system"`）：只有 admin 能碰
       （路由层挂 `system:scheduler:manage`）。
    2. **工作区级任务**（`workspace="xhs"` 等）：该工作区的成员能操作
       （用户口径「工作区域任务在对应工作区内展示，以及相关的操作等」）。

    ⚠️ 这里必须显式挡住「工作区成员去操作系统级任务」——
    路由层只挂了 `system:scheduler:manage` 到写端点，而工作区成员要能执行
    本工作区任务，就不能让他卡在路由层。所以判据下移到本函数：
    **有 `xhs:taskcenter:manage` 的人只能动 `workspace="xhs"` 的行**。

    别人的行 / 不存在 ⇒ 404（不区分，避免越权探测）。
    """
    job = db.get(ScheduledJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="JOB_NOT_FOUND")

    from backend.app.core.perms import has_perm  # noqa: PLC0415

    #: admin 走通（`user_perms` 对 admin 派生全部权限码，见 core/perms.py）。
    if has_perm(current_user, "system:scheduler:manage"):
        return job

    job_workspace = (job.workspace or "").strip()
    if job_workspace == "system":
        #: 系统级任务只归系统管理 —— 工作区成员即使能执行本工作区任务，也碰不到这里。
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="JOB_SYSTEM_WORKSPACE_REQUIRES_ADMIN"
        )

    #: 工作区级任务：该工作区的成员可操作（只作用于本工作区数据）。
    if not has_perm(current_user, f"{job_workspace}:taskcenter:manage"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="JOB_WORKSPACE_PERM_REQUIRED"
        )
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
    - **内置任务**：只有 `spec.params_editable` 里的键可写，其余一律 400
      `JOB_PARAMS_NOT_EDITABLE`（代码拥有的参数不许用户改）；
    - **非内置任务**（2026-10-07 用户口径「非系统内置任务可删除，可添加调用参数」）：
      键集合不受白名单限制，用户可自由增删 —— 它自己建的、参数喂给它自己的执行函数；
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
    # ⚠️ 白名单**只对内置任务**生效（2026-10-07 用户口径，详见 `_validate_create_params`
    # 同一处注释）：非内置任务允许用户自由添加参数键。
    if spec.builtin and disallowed:
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
    #: 2026-10-07 用户口径：「**系统内置**任务的调用参数不可删除不可添加；**非系统内置**
    #: 任务可删除、可添加调用参数」。⇒ 白名单**只对内置任务生效**（内置的参数集合由
    #: 注册表 `JobSpec` 拥有，用户改不了键名）；非内置任务（用户自建的 `auto_ops` 等）
    #: 允许自由添加键 —— 它们由用户自己创建、参数只喂给它自己的执行函数，无跨租户面。
    if spec.builtin and disallowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"JOB_PARAMS_NOT_EDITABLE: 参数 {sorted(disallowed)} 由代码拥有，不可写入",
        )
    cleaned = dict(params)
    # 默认值兜底（2026-10-07 用户口径「调用参数……必须有默认值」）：建行时把**没写的
    # 键**按注册表默认值补齐 ⇒ 界面「显示什么」与「执行时用什么」从建行那一刻起就一致
    # （否则用户看到预填的默认值、库里却没有，回读时又得靠前端再预填一次）。
    for default_key, default_value in spec.params_defaults.items():
        if cleaned.get(default_key) is None:
            cleaned[default_key] = default_value
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
@router.get("/jobs", response_model=ScheduledJobListResponse)
def list_jobs(
    request: Request,
    job_status: Optional[str] = Query(default=None, alias="status"),
    trigger_type: Optional[str] = None,
    keyword: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    #: ⚠️ **设计 69（2026-10-25）后不再按 `user_id` 过滤**。
    #:
    #: 任务定义已收为公共（`job_key` 全表唯一，`user_id` 只是"最后修改人"且
    #: 内置行留空）—— 继续按 `user_id == 当前用户` 筛，admin 会一条都查不到
    #:（实测：422/空列表）。任务中心本身已收归 admin（路由挂
    #: `system:scheduler:view`），端点这里按工作区/状态筛即可。
    statement = select(ScheduledJob)
    workspace = str(request.query_params.get("workspace") or "").strip()
    if workspace:
        statement = statement.where(ScheduledJob.workspace == workspace)
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


@router.get("/targets", response_model=JobTargetListResponse)
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
    #: 设计 69：任务定义公共 ⇒ 计数按全表（不再是"我建了几个"）。
    for key in db.scalars(select(ScheduledJob.job_key)).all():
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
                # 2026-10-07：前端据它预填「新建」弹窗的固定参数行。
                "params_defaults": dict(spec.params_defaults),
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
    # 设计 69（2026-10-25）：`job_key` **全表唯一** —— 一种任务只有一份定义。
    #
    # ⚠️ 原实现（R18「内置目标也允许新建」）允许同 key 多行，那是「每用户一份定义」
    # 时代的形态。改成公共定义后，这里若不预检，重复建会走到 `db.commit()` 抛
    # `IntegrityError`（唯一索引 `uq_scheduled_jobs_job_key`）—— 表现为 **500**，
    # 用户看到「服务器内部错误」而不是「这个任务已存在」。
    # 500 会让人以为是代码 bug，实际问题在输入 ⇒ 必须显式 409。
    existing = db.query(ScheduledJob).filter(ScheduledJob.job_key == spec.key).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"任务 {spec.key} 已存在（{existing.name}）—— "
                "一种任务只有一份定义，请直接修改已有任务，不要重复创建"
            ),
        )
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
        workspace=workspace_for_job(spec.key),
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


@router.get("/jobs/{job_id}", response_model=ScheduledJobDetailResponse)
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
    #: ⚠️ **2026-10-07 用户口径恢复了这条守卫**：「定时任务新增表字段是否为内置任务，
    #: 列表新增展示，内容任务不允许删除，前后端添加校验逻辑」—— 内置任务
    #: （`is_builtin=True`）**不可删除**，可暂停 / 可改调度与参数。
    #:
    #: 被取代的原句（2026-09-24「所有任务都是内置的 ⇒ 一律可删」）保留在此，不静默删除：
    #:     用户口径（2026-09-24）：「所有任务都是内置的」⇒ 内置不再是一种特权，一律可删
    #:     （原先这里 400 `JOB_BUILTIN_PROTECTED`）。
    #: 为什么现在区分：内置行是**代码拥有的种子**（注册表 `JobSpec`），删掉后重启会按
    #: 种子重新播种回来 ——「删了又自己回来」比「明确不让删」更难理解。
    if job.is_builtin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="JOB_BUILTIN_PROTECTED: 内置任务禁止删除（可暂停或改调度）",
        )
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
@router.get("/logs", response_model=ScheduledJobLogListResponse)
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
    #: 设计 69：日志不再按 `user_id`（那是"最后修改人"）过滤 —— fail-out 后
    #: 每行带 `executed_for_user_id` 表示"为谁跑的"。任务中心已收归 admin，
    #: 该端点的可见性由路由权限码收口。
    statement = select(ScheduledJobLog)
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
        select(ScheduledJobLog).where(ScheduledJobLog.id.in_(payload.ids))
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
    """清空调度日志（可按 job_id 收窄；设计 69 后为全表操作，admin 专属）。"""
    statement = select(ScheduledJobLog)
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


@router.get("/overview")
def schedule_overview(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """跨工作区定时任务总览（设计 64 §3.3，**admin 专属**）。

    决策背景：调度器是**公共单例**（不按工作区拆分，企业级对照见设计 64 §3.1）——
    行级 `user_id` 隔离管「我的任务中心」，`workspace` 列管归属聚合。
    本端点把全部 job 按工作区/归属人聚合给管理员；普通用户 403。
    """
    from backend.app.core.roles import ROLE_ADMIN, normalize_role

    if normalize_role(current_user.role) != ROLE_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "SCHEDULE_OVERVIEW_FORBIDDEN",
                "message": "定时任务总览只有超级管理员可以查看",
            },
        )

    from backend.app.models import User

    rows = db.execute(
        select(ScheduledJob, User.username)
        .join(User, User.id == ScheduledJob.user_id, isouter=True)
        .order_by(ScheduledJob.workspace.asc(), ScheduledJob.id.asc())
    ).all()
    items = []
    for job, owner_name in rows:
        items.append(
            {
                "id": job.id,
                "workspace": job.workspace,
                "name": job.name,
                "job_key": job.job_key,
                "owner_id": job.user_id,
                "owner_name": owner_name or "-",
                "status": job.status,
                "trigger_type": job.trigger_type,
                "cron_expression": job.cron_expression,
                "is_builtin": bool(job.is_builtin),
                "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
                "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
                "run_count": int(job.run_count or 0),
                "fail_count": int(job.fail_count or 0),
                "last_status": job.last_status,
            }
        )
    by_workspace: dict[str, int] = {}
    for item in items:
        key = item["workspace"] or "-"
        by_workspace[key] = by_workspace.get(key, 0) + 1
    return {
        "items": items,
        "total": len(items),
        "by_workspace": by_workspace,
    }


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
    #: 全表行 id 集合（设计 69：任务定义公共，总览不再只看"我的"）
    my_ids = set(db.scalars(select(ScheduledJob.id)).all())
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
    for job in db.scalars(select(ScheduledJob)).all():
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
