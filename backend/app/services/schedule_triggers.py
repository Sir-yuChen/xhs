"""调度触发器与策略的 APScheduler 映射（设计 15 §6.2~§6.4）。

**唯一权威来源**：cron 合法性由 APScheduler 实解析判定（后端
`POST /api/schedule/cron/validate` 与写入路径共用本模块），前端不造第二个
校验器；misfire → APScheduler 参数的映射表也只在这一处。

⚠️ cron 口径 = **6 位**（秒 分 时 日 月 周），与若依前端组件 / Quartz 习惯
一致。刻意不用 `CronTrigger.from_crontab()`：它只收 **5 位**（无秒），会把
"0 * * * * *"（每分钟）误判为非法。逐字段构造让 6 位成为唯一口径。

⚠️ 2026-09-24 用户口径（R14）：三档 = **手动执行 / Cron 周期 / 定时一次**。
「间隔周期」（IntervalTrigger）已退役 —— 本模块**不再为它构造触发器**；
老 interval 行由迁移 `h8i9j0k1l2m3` 换算成等价 cron。`manual` 档不在本模块
构造触发器（它根本不进调度器装配，见 `scheduler_service.sync_jobs_from_db`）。
"""

from __future__ import annotations

from typing import Optional

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from backend.app.models.scheduled_job import (
    CONCURRENT_ALLOW,
    MISFIRE_DISCARD,
    MISFIRE_FIRE_NOW,
    MISFIRE_ONCE,
    TRIGGER_AT,
    TRIGGER_MANUAL,
    ScheduledJob,
)

#: 调度器时区（设计 15 §6.6：取代 UTC，与 `shanghai_now()` 统一东八区）
SCHEDULER_TIMEZONE = "Asia/Shanghai"

#: 允许并发的 job 的 max_instances 固定值（注册表白名单外的并发上限不存在）
MAX_INSTANCES_WHEN_ALLOWED = 3

#: misfire → (misfire_grace_time, coalesce)。once 是默认且等于现有全部 job 的现状
#: （coalesce=True），换用本映射不改变既有行为。
_MISFIRE_PARAMS: dict[str, tuple[Optional[int], bool]] = {
    MISFIRE_FIRE_NOW: (None, False),  # 立即执行：积压全部补跑
    MISFIRE_ONCE: (None, True),  # 执行一次：积压合并补一次
    MISFIRE_DISCARD: (1, True),  # 放弃执行：超过 1 秒没跑就丢弃
}

#: 6 位 cron 的字段名（顺序即位置）
_CRON_FIELDS = ("second", "minute", "hour", "day", "month", "day_of_week")


def parse_cron_expression(expression: str) -> CronTrigger:
    """实解析 6 位 cron（秒 分 时 日 月 周）。非法抛 `ValueError`（原文可读）。"""
    text = (expression or "").strip()
    if not text:
        raise ValueError("cron 表达式不能为空")
    parts = text.split()
    if len(parts) != 6:
        raise ValueError(f"cron 需要 6 段（秒 分 时 日 月 周），收到 {len(parts)} 段")
    try:
        return CronTrigger(
            timezone=SCHEDULER_TIMEZONE,
            **dict(zip(_CRON_FIELDS, parts)),
        )
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def misfire_params(policy: str) -> tuple[Optional[int], bool]:
    """错过策略 → `(misfire_grace_time, coalesce)`。未知值按 `once`（默认档）。"""
    return _MISFIRE_PARAMS.get(policy, _MISFIRE_PARAMS[MISFIRE_ONCE])


def max_instances_for(job: ScheduledJob, *, registry_allows_concurrent: bool) -> int:
    """并发开关 → `max_instances`。

    **注册表硬顶**（设计 15 §6.4）：`registry_allows_concurrent=False` 的 job 类型
    （首批全部 8 个都是平台读写链路）恒为 1，与行上的 `concurrent` 取值无关 ——
    安全网不做成条件性的。
    """
    if registry_allows_concurrent and job.concurrent == CONCURRENT_ALLOW:
        return MAX_INSTANCES_WHEN_ALLOWED
    return 1


def build_trigger(job: ScheduledJob):
    """任务行 → APScheduler 触发器（cron / at 两形态，设计 15 §6.2）。

    ⚠️ `manual` 档**不进这里** —— 它不参与调度装配（`sync_jobs_from_db` 会跳过）；
    真被传到就是调用方的逻辑错误，直接抛错而不是悄悄按 cron 兜底（安全网不做
    成条件性的）。
    """
    if job.trigger_type == TRIGGER_MANUAL:
        raise ValueError(
            "手动执行任务不参与调度装配（trigger_type=manual 不应构造触发器）"
        )
    if job.trigger_type == TRIGGER_AT:
        if job.run_at is None:
            raise ValueError("定时一次性任务必须指定执行时间（run_at）")
        return DateTrigger(run_date=job.run_at, timezone=SCHEDULER_TIMEZONE)
    return parse_cron_expression(job.cron_expression)
