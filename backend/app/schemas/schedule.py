"""调度任务中心的共享请求模型与校验（设计 15 §4.1 / §6.2 / §8）。

`/api/schedule/jobs` 与 `/api/auto-tasks` 的调度区块共用本模块 ——
单一校验实现，两边行为不会漂移。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from backend.app.core.time import shanghai_now
from backend.app.models.scheduled_job import (
    CONCURRENT_FORBID,
    CONCURRENT_MODES,
    JOB_STATUS_ACTIVE,
    JOB_STATUS_PAUSED,
    MISFIRE_ONCE,
    MISFIRE_POLICIES,
    TRIGGER_CRON,
    TRIGGER_TYPES,
)
from backend.app.services.schedule_triggers import parse_cron_expression

TRIGGER_MANUAL_VALUE = "manual"


class ScheduleValidationError(ValueError):
    """调度字段校验失败。`code` 即 API 400 的 detail（如 `CRON_INVALID`）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class ScheduleIn(BaseModel):
    """调度设置（创建 / 更新共用；未传的字段用默认值，语义见各端点）。

    R14：`trigger_type` 三档 = `manual`（只能手动触发）/ `cron` / `at`；
    `interval_seconds` 随 interval 退役一并删除。
    """

    trigger_type: str = Field(default=TRIGGER_CRON)
    cron_expression: str = Field(default="", max_length=64)
    run_at: datetime | None = Field(default=None)
    misfire_policy: str = Field(default=MISFIRE_ONCE)
    concurrent: str = Field(default=CONCURRENT_FORBID)
    #: False = 建行即暂停（`manual` 档不参与调度，此项对它无意义但仍记录状态）
    enabled: bool = Field(default=True)


def validate_schedule(payload: ScheduleIn, *, registry_allows_concurrent: bool = False) -> None:
    """校验调度字段（cron 实解析；`at` 必须在未来；并发受注册表硬顶约束）。

    失败抛 `ScheduleValidationError`（API 层映射 400）。全部通过则不返回。

    `manual` 档不校验触发器字段（它没有触发器：只能手动执行）。
    """
    if payload.trigger_type not in TRIGGER_TYPES:
        raise ScheduleValidationError("TRIGGER_TYPE_UNSUPPORTED", payload.trigger_type)
    if payload.trigger_type == "cron":
        try:
            parse_cron_expression(payload.cron_expression)
        except ValueError as exc:
            raise ScheduleValidationError("CRON_INVALID", str(exc)) from exc
    elif payload.trigger_type == "at":
        if payload.run_at is None:
            raise ScheduleValidationError("RUN_AT_REQUIRED", "定时一次性任务必须指定执行时间")
        # 存的与比的一律是上海时间 naive（`shanghai_now()` 口径）
        if payload.run_at <= shanghai_now():
            raise ScheduleValidationError("RUN_AT_INVALID", "执行时间必须晚于当前时间")
    if payload.misfire_policy not in MISFIRE_POLICIES:
        raise ScheduleValidationError("MISFIRE_POLICY_UNSUPPORTED", payload.misfire_policy)
    if payload.concurrent not in CONCURRENT_MODES:
        raise ScheduleValidationError("CONCURRENT_UNSUPPORTED", payload.concurrent)
    if payload.concurrent != CONCURRENT_FORBID and not registry_allows_concurrent:
        raise ScheduleValidationError(
            "JOB_CONCURRENT_NOT_ALLOWED", "该任务类型由注册表禁止并发（平台写操作风控）"
        )


def apply_schedule(job, payload: ScheduleIn) -> None:
    """把校验过的调度设置写到任务行上（调用方负责 commit）。

    ⚠️ `manual` 档要清空 cron 与 run_at（切档后不留旧触发器的残值 ——
    否则「只能手动触发」的行还带着一个看起来会跑的 cron 表达式）。
    """
    job.trigger_type = payload.trigger_type
    job.cron_expression = payload.cron_expression.strip() if payload.trigger_type == "cron" else ""
    job.run_at = payload.run_at if payload.trigger_type == "at" else None
    job.misfire_policy = payload.misfire_policy
    job.concurrent = payload.concurrent
    job.status = JOB_STATUS_ACTIVE if payload.enabled else JOB_STATUS_PAUSED


class CronValidateRequest(BaseModel):
    cron_expression: str = Field(max_length=64)


class JobStatusChangeRequest(BaseModel):
    status: str = Field(pattern=f"^({JOB_STATUS_ACTIVE}|{JOB_STATUS_PAUSED})$")
