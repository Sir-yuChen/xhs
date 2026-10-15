"""自动化工作流执行引擎（设计 19 §4）。

职责**只有**三件事：
1. 顺序推进 DAG 节点（一个 run 在一个工作线程里跑完，引擎不并行）；
2. 状态落库（run / 步骤日志两表的终态回写）；
3. 跨节点上下文传递（上游节点的 ``detail`` 供下游消费）。

**不做**的事（全部下沉到一期底座，设计 19 §5）：
- 不新建队列 / 线程池 —— 节点内部的实际执行下沉为 ``tasks`` 行，由一期 worker 认领；
- 不实现账号互斥 —— 由 ``note_publish_service`` 的账号锁保证（引擎自加锁必与队列锁互不可见 → 死锁）；
- 不实现重试/退避 —— 由 ``tasks.max_retries`` + 一期调度器既有退避承担，引擎只透传结果；
- 不自动续跑 —— 进程被杀后只把僵死 run 收敛到 ``failed``（§5.3），重跑由人决定。

状态机（设计 19 §4.1）::

    pending ──start()──► running ──全部节点终态成功──► succeeded
       │                   │
       │                   ├──审核节点 waiting──► paused ──人工审完──► running
       │                   ├──任一节点 failed──► failed
       │                   └──用户取消──► cancelled
       └──节点重跑（rerun）──► running（从 current_node_id 起）
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models.workflow import (
    LOG_FAILED,
    LOG_RUNNING,
    LOG_SKIPPED,
    LOG_SUCCEEDED,
    LOG_WAITING,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_PENDING,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    WorkflowRun,
    WorkflowRunLog,
)

logger = logging.getLogger(__name__)

#: 单节点最长等待时长：超过即判 ``node_error``，不让慢节点把引擎线程挂死（设计 19 §15）。
NODE_TIMEOUT = timedelta(minutes=30)

#: ``$.global.xxx`` 参数引用前缀（设计 19 §3.8）
_GLOBAL_PREFIX = "$.global."


class WorkflowError(Exception):
    """可重跑的节点失败（§9.1 词表）。``code`` 落 ``workflow_run_logs.error_type``。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class NodeContext:
    """单次 run 内的跨节点上下文（进程内对象，不落库）。

    上游节点的 ``detail`` 通过 ``update()`` 写进来，下游节点用 ``get()`` 取。
    审核结果（``review_done``）也在这里：重跑下游时已通过的审核**不要求重审**（§9.3）。
    """

    def __init__(self) -> None:
        self._details: dict[str, Any] = {}
        self.review_done: bool = False
        self.review_results: dict[int, str] = {}  # note_id -> approve/reject/skip

    def update(self, node_key: str, detail: Any) -> None:
        self._details[node_key] = detail or {}

    def get(self, node_key: str) -> Any:
        return self._details.get(node_key)

    def upstream_notes(self) -> list[int]:
        """取最近一个产出 note_ids 的节点（generate）的产出。"""
        for key, detail in reversed(list(self._details.items())):
            if isinstance(detail, dict) and detail.get("note_ids"):
                return list(detail["note_ids"])
        return []


def resolve_params(config: dict, global_params: dict) -> dict:
    """解析 ``$.global.xxx`` 引用（设计 19 §3.8）。

    未命中 ⇒ 抛 ``param_unresolved``，**不静默用空值跑**（空值跑完才发现账号是空，
    已经是半发布事故）。解析结果不落库，只在节点 ``detail.resolved`` 里留痕。
    """
    if not isinstance(config, dict):
        return config

    def _resolve(value: Any) -> Any:
        if isinstance(value, str) and value.startswith(_GLOBAL_PREFIX):
            path = value[len(_GLOBAL_PREFIX):]
            if not path:
                raise WorkflowError("param_unresolved", f"参数引用路径为空：{value!r}")
            # 只支持一层键（§3.8 的语法就是 $.global.<key>，不递归）
            if path not in global_params:
                raise WorkflowError(
                    "param_unresolved",
                    f"参数引用未命中：{value!r}（global_params 里没有 {path!r}）",
                )
            return global_params[path]
        if isinstance(value, dict):
            return {k: _resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_resolve(v) for v in value]
        return value

    return _resolve(config)


def _truncate(text: Optional[str], limit: int = 2000) -> Optional[str]:
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit]


def snapshot_nodes(workflow_id: int, db: Session) -> list[dict]:
    """构建执行时刻的 DAG 快照（§2.3 ``graph_snapshot`` 的节点部分）。

    从 ``workflow_nodes`` 实时读，按 ``seq`` 排序，输出纯 dict（不含 ORM 对象）。
    """
    from backend.app.models.workflow import WorkflowNode

    rows = db.scalars(
        select(WorkflowNode)
        .where(WorkflowNode.workflow_id == workflow_id)
        .order_by(WorkflowNode.seq, WorkflowNode.id)
    ).all()
    return [
        {
            "id": int(r.id),
            "node_type": r.node_type,
            "node_key": r.node_key,
            "node_name": r.node_name,
            "seq": int(r.seq),
            "config": dict(r.config or {}),
            "enabled": bool(r.enabled),
        }
        for r in rows
    ]


def build_graph(nodes: list[dict]) -> dict:
    """由有序节点列表派生 DAG 邻接表（设计 19 §2.1：线性链时 edges 由顺序自动派生）。

    ⚠️ 节点 id 在**建表时还没有**（WorkflowNode 还没 flush），此时用 node_key
    作为图节点标识；保存后引擎用 ``snapshot_nodes`` 读真实 id 重建快照。
    两种形态都能被 ``execute_run`` 消费（它只按 ``seq`` 排序）。
    """
    keys = [n.get("id") if n.get("id") is not None else n["node_key"] for n in nodes]
    edges = [[a, b] for a, b in zip(keys, keys[1:])]
    return {"node_ids": keys, "edges": edges}


def execute_run(run_id: int) -> None:
    """执行一个 run（在**调用方的**工作线程里同步跑完，设计 19 §4.2）。

    幂等：run 已是终态或正在被别处执行时直接返回（§4.3 并发与重入保护）。
    审核节点会把 run 留在 ``paused`` 然后返回 —— 等人工审完再调一次本函数继续。
    """
    from backend.app.services.workflow_nodes import REGISTRY

    db = SessionLocal()
    try:
        run = _lock_run(db, run_id)
        if run is None:
            return

        if run.status == RUN_PENDING:
            run.started_at = shanghai_now()
        # paused / failed(重跑) 进来都续跑，started_at 保留首次值
        run.status = RUN_RUNNING
        nodes = sorted(run.graph_snapshot.get("nodes", []), key=lambda n: n["seq"])
        ctx = NodeContext()

        for node in nodes:
            if not node.get("enabled", True):
                _upsert_log(db, run, node, status=LOG_SKIPPED)
                continue
            if _cancel_requested(db, run):
                _mark_cancelled(db, run)
                return

            run.current_node_id = node["id"]
            db.commit()

            log_row = _upsert_log(db, run, node, status=LOG_RUNNING)
            try:
                resolved = resolve_params(node["config"], run.global_params_snapshot or {})
                handler = REGISTRY.get(node["node_type"])
                if handler is None:
                    raise WorkflowError(
                        "node_error", f"未知节点类型：{node['node_type']!r}"
                    )

                # 审核节点：先问 handler 要不要等人（§3.3）
                if handler.needs_review and not ctx.review_done:
                    log_row.status = LOG_WAITING
                    run.status = RUN_PAUSED
                    hours = float(node["config"].get("review_timeout_hours") or 24)
                    run.review_deadline = shanghai_now() + timedelta(hours=hours)
                    db.commit()
                    return  # 等人工，不置 finished_at

                outcome = handler.run(resolved, ctx, run, db)
                log_row.status = LOG_SUCCEEDED
                log_row.detail = outcome.detail if outcome else {}
                if outcome and outcome.detail:
                    ctx.update(node["node_key"], outcome.detail)
            except WorkflowError as exc:
                log_row.status = LOG_FAILED
                log_row.error_type = exc.code
                log_row.error_message = _truncate(exc.message)
                run.status = RUN_FAILED
                run.error_type = exc.code
                run.error_message = _truncate(exc.message)
                log_row.finished_at = shanghai_now()
                run.finished_at = shanghai_now()
                db.commit()
                return
            except Exception as exc:  # noqa: BLE001 - 收口所有未知异常为 node_error
                logger.exception("workflow node %s failed", node.get("node_key"))
                log_row.status = LOG_FAILED
                log_row.error_type = "node_error"
                log_row.error_message = _truncate(f"{type(exc).__name__}: {exc}")
                run.status = RUN_FAILED
                run.error_type = "node_error"
                run.error_message = log_row.error_message
                log_row.finished_at = shanghai_now()
                run.finished_at = shanghai_now()
                db.commit()
                return

            log_row.finished_at = shanghai_now()
            run.finished_nodes += 1
            db.commit()

        run.status = RUN_SUCCEEDED
        run.finished_at = shanghai_now()
        db.commit()
    finally:
        db.close()


def _lock_run(db: Session, run_id: int) -> Optional[WorkflowRun]:
    """取 run 并加写锁（防同一 run 被并发执行两次，§4.3）。

    SQLite 退化为 ``BEGIN IMMEDIATE`` —— 与一期 ``note_queue_service`` 同一封装。
    已是终态（succeeded/failed/cancelled）的 run 直接返回 None。
    """
    run = db.scalars(
        select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update()
    ).one_or_none()
    if run is None:
        logger.warning("workflow run %s not found", run_id)
        return None
    if run.status in (RUN_SUCCEEDED, RUN_CANCELLED):
        # failed 允许重跑（§9.3 rerun），paused 允许续跑（§3.3 审核完成）
        return None
    return run


def _cancel_requested(db: Session, run: WorkflowRun) -> bool:
    """协作式取消：只在节点边界检查（已下沉到队列的任务不强行终止，§4.1）。"""
    db.refresh(run)
    return run.status == RUN_CANCELLED or bool(run.error_type == "cancelled")


def _mark_cancelled(db: Session, run: WorkflowRun) -> None:
    run.finished_at = shanghai_now()
    db.commit()


def _upsert_log(db: Session, run: WorkflowRun, node: dict, *, status: str) -> WorkflowRunLog:
    """同 run 同节点只有一行（§2.4 唯一约束）：重跑改状态不新增行。"""
    row = db.scalars(
        select(WorkflowRunLog).where(
            WorkflowRunLog.run_id == run.id,
            WorkflowRunLog.node_key == node["node_key"],
        )
    ).one_or_none()
    now = shanghai_now()
    if row is None:
        row = WorkflowRunLog(
            run_id=run.id,
            user_id=run.user_id,
            node_id=node.get("id"),
            node_key=node["node_key"],
            node_type=node["node_type"],
            node_name=node.get("node_name") or "",
            seq=int(node.get("seq") or 0),
            status=status,
            started_at=now,
        )
        db.add(row)
    else:
        # 重跑：重置终态字段，保留 task_id（§9.3 幂等键命中时复用）
        row.status = status
        if status == LOG_RUNNING:
            row.started_at = now
            row.finished_at = None
            row.error_type = None
            row.error_message = None
    db.flush()
    return row


# ---------------------------------------------------------------------------
# 启动期恢复扫描（设计 19 §5.3）
# ---------------------------------------------------------------------------


def recover_workflow_runs(db: Optional[Session] = None) -> dict:
    """进程被杀后的恢复：把僵死 run 收敛到 ``failed``（**不自动续跑**）。

    三档（§5.3）：
    - run ``running`` 且日志全部终态 ⇒ 直接 ``failed(interrupted)``；
    - run ``running`` 且有 ``running`` 日志 ⇒ 看 ``task_id``：task 终态则收口，
      task 也 running ⇒ 一期队列的孤儿清理兜底后置 failed；
    - run ``paused`` ⇒ 原样保留（本来就等人）。

    幂等：每档都先判终态再改，重复执行无副作用（与一期恢复钩子同一风格）。
    返回各档计数，供启动日志和测试断言。
    """
    own = db is None
    db = db or SessionLocal()
    counts = {"interrupted": 0, "task_finished": 0, "kept_paused": 0}
    try:
        runs = db.scalars(
            select(WorkflowRun).where(WorkflowRun.status == RUN_RUNNING)
        ).all()
        for run in runs:
            logs = db.scalars(
                select(WorkflowRunLog).where(WorkflowRunLog.run_id == run.id)
            ).all()
            active = [lg for lg in logs if lg.status in (LOG_RUNNING, LOG_WAITING)]
            if not active:
                run.status = RUN_FAILED
                run.error_type = "interrupted"
                run.error_message = "进程被杀：恢复扫描发现 run 无进行中的步骤日志"
                run.finished_at = shanghai_now()
                counts["interrupted"] += 1
                continue

            for lg in active:
                tid = lg.task_id
                if tid is None:
                    # 未下沉到队列的节点（如 review 的 waiting）→ 视为中断
                    lg.status = LOG_FAILED
                    lg.error_type = "interrupted"
                    lg.error_message = "进程被杀：恢复扫描收敛"
                    lg.finished_at = shanghai_now()
                    counts["interrupted"] += 1
                else:
                    # 回查一期 task：终态则按结果收口，否则交给队列孤儿清理
                    from backend.app.models.task import Task

                    task = db.scalars(select(Task).where(Task.id == tid)).one_or_none()
                    if task is not None and task.status in ("succeeded", "failed", "cancelled"):
                        lg.status = (
                            LOG_SUCCEEDED if task.status == "succeeded" else LOG_FAILED
                        )
                        lg.error_type = None if task.status == "succeeded" else task.error_type
                        lg.finished_at = shanghai_now()
                        counts["task_finished"] += 1
                    # task 仍 running ⇒ 一期孤儿清理会兜底，此处不越俎代庖

            if any(lg.status == LOG_FAILED for lg in logs):
                run.status = RUN_FAILED
                run.error_type = "interrupted"
                run.finished_at = shanghai_now()

        paused = db.scalars(
            select(WorkflowRun).where(WorkflowRun.status == RUN_PAUSED)
        ).all()
        counts["kept_paused"] = len(paused)  # 原样保留

        db.commit()
    finally:
        if own:
            db.close()
    return counts


def cancel_run(run_id: int, user_id: int) -> bool:
    """协作式取消（§4.1）：只标记，不强行终止已下沉到队列的任务。"""
    db = SessionLocal()
    try:
        run = db.scalars(
            select(WorkflowRun).where(
                WorkflowRun.id == run_id, WorkflowRun.user_id == user_id
            )
        ).one_or_none()
        if run is None or run.status not in (RUN_RUNNING, RUN_PAUSED, RUN_PENDING):
            return False
        run.status = RUN_CANCELLED
        run.finished_at = shanghai_now()
        db.commit()
        return True
    finally:
        db.close()


def reset_downstream(run: WorkflowRun, node_key: str, db: Session) -> list[str]:
    """从失败节点重跑（§9.3）：重置该节点及全部下游的日志行，回退 finished_nodes。

    上游已成功的节点**不重跑**（重复执行生成/发布是事故）。返回被重置的节点 key 列表。
    """
    nodes = sorted(run.graph_snapshot.get("nodes", []), key=lambda n: n["seq"])
    keys: list[str] = []
    hit = False
    for node in nodes:
        if node["node_key"] == node_key:
            hit = True
        if not hit:
            continue
        row = db.scalars(
            select(WorkflowRunLog).where(
                WorkflowRunLog.run_id == run.id,
                WorkflowRunLog.node_key == node["node_key"],
            )
        ).one_or_none()
        if row is not None:
            row.status = LOG_RUNNING
            row.started_at = shanghai_now()
            row.finished_at = None
            row.error_type = None
            row.error_message = None
        keys.append(node["node_key"])
    if keys:
        run.finished_nodes = max(0, run.finished_nodes - len(keys))
        run.status = RUN_RUNNING
        run.error_type = None
        run.error_message = None
        db.commit()
    return keys


def register_recovery_hook() -> None:
    """把 ``recover_workflow_runs`` 挂到 FastAPI lifespan（设计 19 §7.3）。

    由 ``main.py`` 在一期恢复钩子之后调用。独立成函数是为了让测试能单独跑。
    """
    # main.py 直接调 recover_workflow_runs()；本函数保留为 hook 占位 + 文档锚点。
    return None


_HOOK: Optional[Callable[[], None]] = None


def set_recovery_hook(fn: Optional[Callable[[], None]]) -> None:
    global _HOOK
    _HOOK = fn


def run_recovery_hook() -> None:
    if _HOOK is not None:
        _HOOK()
