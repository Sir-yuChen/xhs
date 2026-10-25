"""调度域**响应**模型（设计 61 §A6 第三批）。

⚠️ 本文件此前只有**请求**模型（`ScheduleIn` / `CronValidateRequest` /
`JobStatusChangeRequest`），响应侧零覆盖 ⇒ 4 个 GET 端点的 200 schema 为空。
本批补齐。

字段逐字段抄自 `api/schedule.py`：
* `ScheduledJobItem` ← `_serialize_job()`（:103-131）
* `ScheduledJobLogItem` ← `serialize_log()`（:134+）
* `JobTargetItem` ← `list_targets()` 的 return
* 分页信封 ← `schemas/common.paginated`：`{total, page, page_size, items}`
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ScheduledJobItem(BaseModel):
    """一个定时任务（`_serialize_job()` 的产物）。"""

    id: int
    user_id: Optional[int] = Field(default=None, description="内置任务按用户各持一份，此处为归属者")
    name: str
    job_key: str
    #: `cron` / `at` / `manual`
    trigger_type: Optional[str] = None
    cron_expression: Optional[str] = None
    run_at: Optional[str] = None
    misfire_policy: Optional[str] = None
    concurrent: Optional[str] = None
    params: dict[str, Any] = Field(default_factory=dict)
    #: 前端据此渲染自定义参数表单（不硬编码键名）。
    params_editable: list[str] = Field(default_factory=list)
    #: 参数的默认值 —— 前端**预填固定行**，存量行缺的键也能显示出来。
    params_defaults: dict[str, Any] = Field(default_factory=dict)
    #: `active` / `paused`
    status: Optional[str] = None
    remark: Optional[str] = None
    #: 内置任务：禁删除、禁改 key/params。
    is_builtin: Optional[bool] = None
    last_run_at: Optional[str] = None
    #: 活值优先（调度器知道真正的下次触发），列值兜底。
    next_run_at: Optional[str] = None
    run_count: Optional[int] = None
    fail_count: Optional[int] = None
    consecutive_failures: Optional[int] = None
    backoff_until: Optional[str] = None
    last_status: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ScheduledJobListResponse(BaseModel):
    """`GET /api/schedule/jobs`。"""

    total: int = 0
    page: int = 1
    page_size: int = 20
    items: list[ScheduledJobItem] = Field(default_factory=list)


class ScheduledJobLogItem(BaseModel):
    """一条任务执行日志（`serialize_log()`:143-170 的逐字段产物）。

    ⚠️ 归属快照字段（`user_id` / `job_key` / `job_name` / `job_trigger_type`）是
    执行时**拷贝**进日志行的：任务删了、改名了，历史日志仍按当时的归属与名字展示
    （DEF-066 隔离依据；任务类型列与筛选也靠这个快照，不靠 join）。
    """

    id: int
    job_id: Optional[int] = None
    user_id: Optional[int] = None
    #: 业务任务号（`tasks.id`）。`None` ⇒ 该日志没有对应业务任务行
    #: （cron 系统作业 / 存量日志）⇒ 前端**不渲染**取消按钮（缺值口径：不猜 id）。
    task_id: Optional[int] = None
    job_name: Optional[str] = None
    job_key: Optional[str] = None
    trigger_type: Optional[str] = None
    #: `running` / `done` / `failed` / `skipped`
    status: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    #: 进度百分比（日志表「任务进度 + 耗时或异常」合并列渲染进度条）。
    progress: Optional[int] = None
    message: Optional[str] = None
    exception_info: Optional[str] = None
    #: ⚠️ 是 **dict** 不是字符串（`serialize_log` 取 `log.result or {}`）。
    result: dict[str, Any] = Field(default_factory=dict)
    #: 执行时的任务类型快照（任务类型列与筛选的唯一依据）。
    job_trigger_type: Optional[str] = None
    #: 执行时的配置快照（详情弹窗用；该列新增前的执行为 null）。
    config_snapshot: Optional[dict[str, Any]] = None
    created_at: Optional[str] = None


class ScheduledJobLogListResponse(BaseModel):
    """`GET /api/schedule/logs`。"""

    total: int = 0
    page: int = 1
    page_size: int = 20
    items: list[ScheduledJobLogItem] = Field(default_factory=list)


class JobTargetItem(BaseModel):
    """一个可调度目标（注册表全量列出，含当前用户已建任务数）。"""

    job_key: str
    name: str
    builtin: bool = False
    allow_concurrent: bool = False
    params_required: list[str] = Field(default_factory=list)
    params_editable: list[str] = Field(default_factory=list)
    params_defaults: dict[str, Any] = Field(default_factory=dict)
    default_trigger_type: str = "cron"
    default_cron: str = ""
    #: 当前用户已建的同目标任务数（前端提示「已存在 N 个」）。
    existing_count: int = 0


class JobTargetListResponse(BaseModel):
    """`GET /api/schedule/targets`。"""

    items: list[JobTargetItem] = Field(default_factory=list)
    total: int = 0


class ScheduledJobDetailResponse(ScheduledJobItem):
    """`GET /api/schedule/jobs/{job_id}` —— 任务详情 = 任务行 + 最近 10 条日志。"""

    recent_logs: list[ScheduledJobLogItem] = Field(default_factory=list)
