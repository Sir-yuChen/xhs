from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Index, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

# 调度触发器与策略的取值常量（白名单从这里组装，API 层不重抄）
#
# 2026-09-24 用户口径（R14）：三档收敛为 **手动执行 / Cron 周期 / 定时一次**；
# 「间隔周期」（原 TRIGGER_INTERVAL）并入 Cron 周期，`interval_seconds` 列已删。
TRIGGER_MANUAL = "manual"  # 只能手动触发：不参与调度装配，"执行一次"立刻跑
TRIGGER_CRON = "cron"
TRIGGER_AT = "at"  # 定时一次性：到 run_at 执行一次，成功后自动 completed（§3.1）
TRIGGER_TYPES = (TRIGGER_MANUAL, TRIGGER_CRON, TRIGGER_AT)

MISFIRE_FIRE_NOW = "fire_now"  # 立即执行：积压全部补跑
MISFIRE_ONCE = "once"  # 执行一次：积压合并补一次（默认，= APScheduler coalesce 现状）
MISFIRE_DISCARD = "discard"  # 放弃执行：积压全丢弃
MISFIRE_POLICIES = (MISFIRE_FIRE_NOW, MISFIRE_ONCE, MISFIRE_DISCARD)

CONCURRENT_FORBID = "forbid"
CONCURRENT_ALLOW = "allow"
CONCURRENT_MODES = (CONCURRENT_FORBID, CONCURRENT_ALLOW)

JOB_STATUS_ACTIVE = "active"
JOB_STATUS_PAUSED = "paused"
JOB_STATUS_COMPLETED = "completed"  # 只由 at 一次性任务成功后自动写入
JOB_STATUSES = (JOB_STATUS_ACTIVE, JOB_STATUS_PAUSED, JOB_STATUS_COMPLETED)

# R20 / R22（2026-09-24 用户口径）：「任务不再分组，分组字段移除，相关逻辑移除」
# ⇒ `JOB_GROUP_DEFAULT` / `JOB_GROUP_SYSTEM` 两个常量、以及 `scheduled_jobs` /
#   `scheduled_job_logs` 两张表的 `job_group` 列**一律物理删除**（迁移 `k1l2m3n4o5p6`）。
#   调用目标的分类改由**代码侧注册表白名单**回答（设计 15 §5），不再靠库里的分组列。

# 日志状态（running = 执行中；进程被杀会留下 running 行，UI 可见，不静默丢失）
LOG_RUNNING = "running"
LOG_SUCCESS = "success"
LOG_FAILED = "failed"
LOG_SKIPPED = "skipped"
LOG_STATUSES = (LOG_RUNNING, LOG_SUCCESS, LOG_FAILED, LOG_SKIPPED)


class ScheduledJob(Base):
    """定时任务行（若依 `sys_job` 的本项目化，设计 15 §4.1）。

    调度语义的**唯一权威源**：触发器（manual/cron/at）只存在本表；
    `auto_tasks` 等业务配置通过 `params.auto_task_id` 引用本行，不存任何调度字段。

    ## 归属语义（2026-10-25 设计 69 **取代** DEF-066「每用户各持一份」）

    用户口径：「任务中心分为系统级别和工作区域级别，工作区域为每个区域中私有」
    「暂时不允许用户创建任务」「收为公共」。

    * **`job_key` 全表唯一** —— 一种任务只有一行定义，**不再按用户复制**。
      DEF-066 当时按用户复制是为了解决「全局共享行被多用户互相操作」，但那条整改
      把「这个任务服务于谁的数据」和「谁有权改它」压进了同一个 `user_id`，
      结果 403 行（31 用户 × 13 任务）里同一个「清理记录表」重复 31 次，
      改一次 Cron 要改 31 处，系统管理页看不出任务内容。

      现在两条语义分开：
      - **归属哪个级别** ⇒ `workspace`（`system` / `xhs` / 未来平台）；
      - **执行时跑谁的数据** ⇒ 由 `JobSpec.user_scoped` 在运行时扇出（fan-out），
        **不是存储字段**（见 `scheduled_job_runner`）。

    * `user_id` 降级为**最后修改人**（审计用，可空）。它不再表达归属，
      也不用于「我的任务」过滤 —— 用户侧任务中心已下线。
    """

    __tablename__ = "scheduled_jobs"

    #: **一种任务只有一行定义**（设计 69 §2，2026-10-25 用户口径「收为公共」）。
    #:
    #: ⚠️ 这条唯一约束是本次重构的**核心不变量**：它让「按用户复制定义行」
    #: 在数据库层就不可能（旧形态 403 行 = 31 用户 × 13 任务）。
    #: 旧约束 `(job_key, user_id)` 已由迁移 `a9b8c7d6e5f4` 删除。
    __table_args__ = (
        Index("uq_scheduled_jobs_job_key", "job_key", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: **最后修改人**（设计 69：不再是归属人；归属看 `workspace`）。
    #: 内置种子行播种时为 NULL（没有人"改过"它）。
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    job_key: Mapped[str] = mapped_column(String(128), index=True)
    #: 工作区归属（设计 64 §3.3）：调度器是**公共单例**（不按工作区拆分），
    #: 行级 user_id 隔离管权限面，`workspace` 管归属展示 ——
    #: 任务中心按本人（不变），系统管理「定时任务」总览按工作区聚合（admin）。
    #: 取值同 `menus.workspace` 词汇表；由 `job_registry.workspace_for_job` 写入。
    workspace: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    trigger_type: Mapped[str] = mapped_column(String(16), default=TRIGGER_CRON)
    cron_expression: Mapped[str] = mapped_column(String(64), default="")
    #: trigger_type="at"（定时一次性）时必填：执行时刻，必须晚于当前时间
    run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    misfire_policy: Mapped[str] = mapped_column(String(16), default=MISFIRE_ONCE)
    concurrent: Mapped[str] = mapped_column(String(16), default=CONCURRENT_FORBID)
    params: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, default=dict)
    status: Mapped[str] = mapped_column(String(16), default=JOB_STATUS_ACTIVE, index=True)
    remark: Mapped[str] = mapped_column(String(500), default="")
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)

    # 运行态（展示/退避门用；触发的权威在调度器侧）
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    backoff_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=shanghai_now, onupdate=shanghai_now
    )


class ScheduledJobLog(Base):
    """调度执行日志（若依 `sys_job_log` 的本项目化，设计 15 §4.2）。

    **无外键**：任务删除后日志保留；`job_name/job_key/job_trigger_type`
    是执行时快照（任务类型筛选靠它，删掉的任务历史日志也不丢）。
    每次执行一行：先插 `running`，完成后改终态 —— 进程被杀留下 running 行，
    比"悄悄没有日志"好（UI 可按 status 筛出）。

    `config_snapshot` = **执行那一刻**的任务配置快照（2026-09-24 用户口径：
    日志详情要能看到当时的配置）—— 任务行后来被改 / 删都不影响历史日志可读。

    ⚠️ **2026-09-24 隔离整改（DEF-066）新增 `user_id`**：调度日志此前**零隔离**
    （表无归属列，列表 / 删除 / 清空三个端点不过滤用户 ⇒ 任何登录用户可见、可删
    全库日志）。本列 = 执行时从任务行**快照**的归属者（不 join ⇒ 任务删了历史
    日志仍可归属）。列可空只为迁移兼容：NULL = 归属不明（迁移兜底归首个用户），
    API 的 `user_id == 当前用户` 过滤天然排除它们。
    """

    __tablename__ = "scheduled_job_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("scheduled_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: 执行时快照的任务**最后修改人**（DEF-066 建列；设计 69 后语义改为审计，
    #: 不再表达归属）。存量行的值 = 当时的归属人，保留作历史快照。
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    #: **这次执行跑的是谁的数据**（设计 69 §4.1，迁移 `f2a3b4c5d6e7`）。
    #:
    #: 为什么要单独一列、不复用 `user_id`：`user_scoped` 任务收成一份定义后，
    #: 一次触发会**扇出到多个用户**（每个用户一行日志），而"谁改的任务"与
    #: "为谁跑的"是两件事。系统级任务（清理类）没有执行主体 ⇒ NULL。
    #:
    #: 前端「执行日志」的「目标用户」列读这一列；账号卡「同步中」判据仍用
    #: `platform_account_id`（不受影响）。
    executed_for_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    #: 执行时快照的**平台账号**（迁移 `j1k2l3m4n5o6` 建列，`b3c4d5e6f7a8` 随 ADR-021
    #: ——设计 18 §6.3「列名漂移」——改名为 `platform_account_id`，与全库隔离键命名
    #: 统一）：账号卡「同步中」的判据 =
    #: 「该账号**最新一条**同步任务日志的状态」（2026-09-27 用户口径「账号信息卡的
    #: 判断逻辑应只需要关注最新一条的同步笔记任务日志的状态即可」）—— 没有这一列就
    #: 只能靠 `task_id` 关联，而账号卡路径上那个字段是空的（两条路都断）。
    #: **刻意不加外键**：日志是历史快照（同 `user_id` 的理由 —— 账号删了，日志仍要
    #: 能回答「当时是哪个账号」）。存量行为 NULL ⇒ 不参与「最新一条」候选。
    platform_account_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )
    #: 执行时快照的**业务任务号**（迁移 `k2l3m4n5o6p7`）：`run_targeted_job` 一直收得到它，
    #: 只是此前没有列可存 ⇒ 日志与任务行只能靠「同秒启动」猜（重启重跑后 `started_at`
    #: 差了近 2 小时，猜法直接失效）。有了它，**孤儿日志收口**才能精确回查任务行真实终态
    #: （设计 15 §15）。不加外键：任务行被记录清理删掉后，日志仍要能回答「当时是哪次执行」。
    task_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    job_name: Mapped[str] = mapped_column(String(128), default="")
    job_key: Mapped[str] = mapped_column(String(128), default="")
    #: 执行时快照：任务的**触发器类型**（manual/cron/at）—— 日志按任务类型
    #: 筛选的唯一依据（不 join scheduled_jobs：任务删了历史日志也要能筛）
    job_trigger_type: Mapped[str] = mapped_column(String(16), default="", index=True)
    #: 执行时快照的**任务归属工作区**（设计 69 两级任务中心；迁移 `b7c8d9e0f1a2`）。
    #:
    #: 为什么日志要自带这一列、不 join 定义表：工作区任务页（`/platforms/xhs/tasks`）
    #: 必须只显示本工作区的执行记录，而日志是**分页**的 —— 前端过滤会让页码错乱
    #: （「本页 20 条里只有 3 条属于本工作区」）。且任务被删后历史日志仍要能按工作区筛。
    #: 存量行按当时 `job_key` 回填；查不到对应定义的留空（缺值口径：不猜）。
    workspace: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, index=True
    )
    #: 链路 id（W3C trace-id；设计 67）。定时任务若由用户操作触发（立刻执行一次），
    #: 继承那次点击的链路；纯系统定时任务为 NULL（没有发起人，不造假）。
    trace_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    trigger_type: Mapped[str] = mapped_column(String(16), default="scheduled")
    status: Mapped[str] = mapped_column(String(16), default=LOG_RUNNING, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: 任务执行进度百分比（0~100，R13）：日志表「任务进度 + 耗时或异常」合并列
    #: 用它渲染进度条。执行前插行时为 0；任务侧未上报进度则保持 0（不猜值）。
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    exception_info: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    #: 执行时的任务配置快照（设计 15 §4.2）：name/key/触发器/策略/参数/状态。
    #: 本列新增之前的执行为 NULL ⇒ 前端按缺值口径展示（不造假）。
    config_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now, index=True)
