from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

# ---------------------------------------------------------------------------
# 节点类型白名单（设计 19 §3）
#
# 只在一处定义：模型层常量 + `workflow_nodes.py` 的 REGISTRY。API 层不重抄
# （与 `scheduled_jobs.job_registry` 同一纪律）。
# ---------------------------------------------------------------------------
NODE_TRIGGER = "trigger"  # 触发器：链首，唯一，不可删除（§3.1）
NODE_TOPIC = "topic"  # 选题（热点匹配，方向 2/3，只读热点结果）
NODE_GENERATE = "generate"  # 生成（方向 1 LLM 链路，下沉队列）
NODE_REVIEW = "review"  # 人工审核：默认人工，不自动放行（§3.3 硬约束）
NODE_SCHEDULE = "schedule"  # 定时发布：只排期，不发布（§3.4）
NODE_PUBLISH = "publish"  # 发布：复用一期发布队列（§3.5）
NODE_MONITOR = "monitor"  # 监控回填：触发指标快照任务（§3.6）
NODE_TYPES = (
    NODE_TRIGGER,
    NODE_TOPIC,
    NODE_GENERATE,
    NODE_REVIEW,
    NODE_SCHEDULE,
    NODE_PUBLISH,
    NODE_MONITOR,
)

# ---------------------------------------------------------------------------
# 状态词表（与 `tasks` / `scheduled_job_logs` 同构，API 层不重抄）
# ---------------------------------------------------------------------------
WF_ENABLED = "enabled"
WF_DISABLED = "disabled"
WF_STATUSES = (WF_ENABLED, WF_DISABLED)

RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_PAUSED = "paused"  # 只由审核节点产生，只由人工审核解除
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_ACTIVE_STATUSES = (RUN_PENDING, RUN_RUNNING, RUN_PAUSED)
RUN_STATUSES = RUN_ACTIVE_STATUSES + (RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELLED)

LOG_RUNNING = "running"
LOG_WAITING = "waiting"  # 审核节点等人
LOG_SUCCEEDED = "succeeded"
LOG_FAILED = "failed"
LOG_SKIPPED = "skipped"  # enabled=False 的节点
LOG_STATUSES = (LOG_RUNNING, LOG_WAITING, LOG_SUCCEEDED, LOG_FAILED, LOG_SKIPPED)

TRIGGER_MANUAL = "manual"
TRIGGER_CRON = "cron"
TRIGGER_AT = "at"
TRIGGER_TYPES = (TRIGGER_MANUAL, TRIGGER_CRON, TRIGGER_AT)

MISFIRE_ONCE = "once"
MISFIRE_POLICIES = (MISFIRE_FIRE_NOW, MISFIRE_ONCE, MISFIRE_DISCARD) = (
    "fire_now",
    "once",
    "discard",
)

RUN_SOURCE_MANUAL = "manual"
RUN_SOURCE_CRON = "cron"
RUN_SOURCE_AT = "at"
RUN_SOURCE_RERUN = "rerun"
RUN_SOURCES = (
    RUN_SOURCE_MANUAL,
    RUN_SOURCE_CRON,
    RUN_SOURCE_AT,
    RUN_SOURCE_RERUN,
)

#: 批次号前缀（一期队列用 NB，工作流用 WF；同一个生成器，见 §5.1）
BATCH_NO_PREFIX = "WF"

#: 注册到 `scheduled_jobs` 的 job_key 前缀（设计 19 §7.1）
JOB_KEY_PREFIX = "workflow."


class Workflow(Base):
    """工作流主表（设计 19 §2.1）。

    一张 DAG：触发器在头，业务节点按边依次执行。图结构存在 ``graph`` 里，
    线性链时由 ``node_ids`` 顺序自动派生 ``edges``。

    ⚠️ 触发语义**不存本表**：``trigger_type`` / ``cron_expression`` / ``run_at``
    只是**编辑态镜像**，装配时复制到 ``scheduled_jobs`` 行（§7.1，单向）。
    调度语义的权威源在 job 行，与 ``auto_tasks`` 引用 job 的纪律一致。
    """

    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=WF_ENABLED, index=True)
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False, default=TRIGGER_MANUAL)
    cron_expression: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    misfire_policy: Mapped[str] = mapped_column(String(16), nullable=False, default=MISFIRE_ONCE)
    #: DAG 邻接表：{"node_ids": [...], "edges": [[a, b], ...]}
    #: 列表式编排（§11.3）时 edges 由 node_ids 顺序自动派生，不需要用户画边。
    graph: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: 全局参数；节点 config 可用 "$.global.xxx" 引用（§3.8）
    global_params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    remark: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    # 运行态镜像（展示用；权威在 scheduled_jobs 行，由执行函数顺手回写）
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=shanghai_now, onupdate=shanghai_now
    )

    __table_args__ = (
        Index("ix_workflows_user_status", "user_id", "status"),
    )


class WorkflowNode(Base):
    """工作流节点（设计 19 §2.2）。

    节点**独立成表**而非塞进 ``workflows.graph``：编排页列表式渲染直接查节点表，
    图结构集中存 ``workflows.graph``，DAG 校验在一处。
    """

    __tablename__ = "workflow_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: CASCADE —— 工作流删了节点一起删；run 快照里另有全量节点信息（§2.4）
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: 冗余但必要：列表/校验免 join，与一期所有业务表一致
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    node_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 同工作流内唯一（用户可见的节点标识，如 publish_matrix_a）
    node_key: Mapped[str] = mapped_column(String(64), nullable=False)
    node_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    #: 链上顺序（列表式编排的排序键）；trigger 节点恒为 0
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 参数 schema 由 node_type 决定（设计 19 §3 逐类给出）
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now)

    __table_args__ = (
        Index("ux_workflow_nodes_key", "workflow_id", "node_key", unique=True),
    )


class WorkflowRun(Base):
    """运行实例（设计 19 §2.3）。

    ⚠️ ``workflow_id`` 是 **SET NULL** 而非 CASCADE（设计 19 §6.1 修正）：
    用户删工作流时**保留历史**，靠 ``workflow_name`` 快照可读 —— 与
    ``scheduled_job_logs`` 无 FK + 快照名同一纪律。
    """

    __tablename__ = "workflow_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: 执行时快照：工作流后来归属变化/删除不影响历史 run（DEF-066 同纪律）
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    workflow_name: Mapped[str] = mapped_column(String(128), nullable=False)

    #: **执行那一刻的 DAG 快照**：节点 id/type/key/name/config/seq/enabled 全量
    graph_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    global_params_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RUN_PENDING, index=True)
    trigger_source: Mapped[str] = mapped_column(String(16), nullable=False, default=RUN_SOURCE_MANUAL)
    #: 定时触发时回指调度日志：工作流执行与调度执行是两条记录，互查不断链
    scheduled_job_log_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("scheduled_job_logs.id", ondelete="SET NULL"), nullable=True
    )
    #: 复用一期批次号语义（前缀 WF），下沉到 tasks.batch_no（§5.2）
    batch_no: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, unique=True)
    #: 当前执行到的节点（graph_snapshot 里的节点 id）
    current_node_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_nodes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    finished_nodes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    #: 审核节点 paused 时的催办截止（config.review_timeout_hours 算出，纯展示，不自动放行）
    review_deadline: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now)

    __table_args__ = (
        Index("ix_workflow_runs_workflow_created", "workflow_id", "created_at"),
        # 工作流级互斥（§8.3）：同一工作流同时只允许一个活跃 run。
        # 部分唯一索引 —— Postgres 原生支持；SQLite 用 WHERE 过滤的条件唯一索引，
        # 两者同一写法（§10.3 双库同形）。
        Index(
            "ux_workflows_active",
            "workflow_id",
            "status",
            unique=True,
            sqlite_where=text("status IN ('pending', 'running', 'paused')"),
            postgresql_where="status IN ('pending', 'running', 'paused')",
        ),
    )


class WorkflowRunLog(Base):
    """步骤日志（设计 19 §2.4）。

    同一 run 同一节点**只有一行**（``ux_workflow_run_logs_node`` 唯一约束）：
    重跑改状态不新增行，避免「重跑一次多一行日志」造成计数错乱。
    无终态的 running 行 = 进程被杀的现场，比「悄悄没有日志」好（§5.3）。
    """

    __tablename__ = "workflow_run_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)

    #: 快照值，可空（节点被删后历史日志仍可读，靠下面四个快照列定位）
    node_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    node_key: Mapped[str] = mapped_column(String(64), nullable=False)
    node_type: Mapped[str] = mapped_column(String(32), nullable=False)
    node_name: Mapped[str] = mapped_column(String(128), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=LOG_RUNNING, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    #: 截断 2000 字符，且必须过一期日志脱敏过滤器（§10.2）
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: 节点产出摘要（生成笔记数、发布 note_ids…），只放摘要不放大对象
    detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    #: 本节点下沉到一期队列时回指 task 行（§5.2），断了也能靠 batch_no 找回
    task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ux_workflow_run_logs_node", "run_id", "node_key", unique=True),
    )


class WorkflowScheduledPublish(Base):
    """工作流排期表（设计 19 §8.4 方案 A）。

    **为什么不复用 ``notes.scheduled_publish_at``**：``notes`` 是全项目最热的表，
    一期/二期多个方向都在加列，改它属于碰撞热点。方案 A 让工作流的排期
    **完全不碰 notes**，一期 ``system.due_publish`` 只认 ``notes.scheduled_publish_at``
    ⇒ 天然零冲突，且 ``notes`` 零加列。

    一条 = 「这个 run 的这条笔记定在这个时刻发」。发布完成后置 ``published=True``
    （由 publish 节点或重跑收敛时回写），用于重跑幂等判定（§9.3）。
    """

    __tablename__ = "workflow_scheduled_publishes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), nullable=False)
    #: 发布账号（一期账号矩阵的账号 id）
    account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now)

    __table_args__ = (
        # §9.3 的发布幂等键：batch_no + note_id + account_id。
        # batch_no 在 run 上，这里用 (run_id, note_id, account_id) 等价且更快。
        Index(
            "ux_workflow_scheduled_publishes", "run_id", "note_id", "account_id", unique=True
        ),
    )
