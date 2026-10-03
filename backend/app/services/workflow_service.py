"""工作流 CRUD + 调度装配（设计 19 §6 / §7）。

service 层第一步一律 ``filter_by(user_id=...)``（§10.1 隔离）。
触发器装配走 ``scheduled_jobs``（§7.1），调度语义的权威源在 job 行。
"""

from __future__ import annotations

import logging
import secrets
import threading
from datetime import datetime
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal

#: 测试可注入的 session 工厂快照（conftest 的 `patch_service_sessions` 约定：
#: 函数级 import 拿不到模块属性可打桩，必须在模块层持有）。
_session_factory = SessionLocal
from backend.app.core.time import shanghai_now  # noqa: E402
from backend.app.models.scheduled_job import (
    CONCURRENT_FORBID,
    JOB_STATUS_ACTIVE,
    ScheduledJob,
    ScheduledJobLog,
)
from backend.app.models.workflow import (
    BATCH_NO_PREFIX,
    RUN_ACTIVE_STATUSES,
    WF_DISABLED,
    WF_ENABLED,
    Workflow,
    WorkflowNode,
    WorkflowRun,
    WorkflowRunLog,
)
from backend.app.services.workflow_engine import (
    WorkflowError,
    build_graph,
    execute_run,
    reset_downstream,
    snapshot_nodes,
)
from backend.app.services.workflow_nodes import validate_workflow

logger = logging.getLogger(__name__)

#: 工作流注册到 scheduled_jobs 的 job_key 前缀（设计 19 §7.1）
#: 与 ``job_registry.WORKFLOW_JOB_KEY_PREFIX`` 是同一常量，只在一处定义另一处引用。
JOB_KEY_PREFIX = "workflow."

_TRIGGER_MANUAL = "manual"
_TRIGGER_CRON = "cron"
_TRIGGER_AT = "at"


def _job_key(workflow_id: int) -> str:
    return f"{JOB_KEY_PREFIX}{workflow_id}"


# ---------------------------------------------------------------------------
# 调度器入口（§7.1）：由 job_registry 注册，APScheduler 按名引用
# ---------------------------------------------------------------------------


def run_workflow_job(workflow_id: int, user_id: Optional[int] = None) -> dict:
    """调度器入口：为一个工作流建 run 并执行（设计 19 §7.1）。

    ``user_id`` 由 runner 按 job 行归属注入（DEF-066 的 user_scoped 语义）。
    定时触发时 ``trigger_source`` 取 ``cron`` / ``at``，并回指调度日志行（§2.3）。
    """
    db = _session_factory()
    try:
        wf = db.scalars(select(Workflow).where(Workflow.id == workflow_id)).one_or_none()
        if wf is None:
            logger.warning("workflow job: workflow %s not found", workflow_id)
            return {"skipped": True, "reason": "workflow not found"}
        if wf.status != WF_ENABLED:
            return {"skipped": True, "reason": f"workflow {wf.status}"}

        owner = user_id if user_id is not None else wf.user_id

        # 回指调度日志（§2.3）：本 run 是哪次调度执行触发的
        job_log_id = None
        job = db.scalars(
            select(ScheduledJob).where(ScheduledJob.job_key == _job_key(wf.id))
        ).one_or_none()
        if job is not None:
            log = db.scalars(
                select(ScheduledJobLog)
                .where(ScheduledJobLog.job_id == job.id)
                .order_by(ScheduledJobLog.started_at.desc())
                .limit(1)
            ).one_or_none()
            if log is not None:
                job_log_id = log.id

        source = "at" if wf.trigger_type == _TRIGGER_AT else "cron"
        run = start_run(
            db, wf.id, owner, trigger_source=source, scheduled_job_log_id=job_log_id
        )
        return {"run_id": run.id, "workflow_id": wf.id}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def get_workflow(db: Session, workflow_id: int, user_id: int) -> Workflow:
    row = db.scalars(
        select(Workflow).where(Workflow.id == workflow_id, Workflow.user_id == user_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="工作流不存在")
    return row


def _last_run(db: Session, workflow_id: int) -> Optional[WorkflowRun]:
    return db.scalars(
        select(WorkflowRun)
        .where(WorkflowRun.workflow_id == workflow_id)
        .order_by(WorkflowRun.created_at.desc())
        .limit(1)
    ).one_or_none()


def _active_run(db: Session, wf: Workflow) -> Optional[WorkflowRun]:
    return db.scalars(
        select(WorkflowRun).where(
            WorkflowRun.workflow_id == wf.id,
            WorkflowRun.status.in_(RUN_ACTIVE_STATUSES),
        )
    ).one_or_none()


def _reject_active_run(db: Session, wf: Workflow) -> None:
    existing = _active_run(db, wf)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"工作流有运行中的实例（run #{existing.id}），无法修改/删除",
        )


def list_workflows(
    db: Session,
    user_id: int,
    *,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """列表（含最近一次 run 概要，§6 端点 1）。"""
    stmt = select(Workflow).where(Workflow.user_id == user_id)
    if status:
        stmt = stmt.where(Workflow.status == status)
    stmt = stmt.order_by(Workflow.updated_at.desc(), Workflow.id.desc())

    rows = db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all()
    items = []
    for wf in rows:
        last = _last_run(db, wf.id)
        nodes = db.scalars(
            select(WorkflowNode)
            .where(WorkflowNode.workflow_id == wf.id)
            .order_by(WorkflowNode.seq)
        ).all()
        items.append(
            {
                "id": wf.id,
                "name": wf.name,
                "status": wf.status,
                "trigger_type": wf.trigger_type,
                "cron_expression": wf.cron_expression,
                "run_at": wf.run_at.isoformat() if wf.run_at else None,
                "remark": wf.remark,
                "node_chain": [
                    {
                        "node_type": n.node_type,
                        "node_key": n.node_key,
                        "node_name": n.node_name,
                        "enabled": n.enabled,
                    }
                    for n in nodes
                ],
                "last_run": _serialize_run(last) if last else None,
                "run_count": wf.run_count,
                "fail_count": wf.fail_count,
                "next_run_at": wf.next_run_at.isoformat() if wf.next_run_at else None,
                "created_at": wf.created_at.isoformat() if wf.created_at else None,
                "updated_at": wf.updated_at.isoformat() if wf.updated_at else None,
            }
        )
    return {"items": items, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# 写入（建 / 改 / 删）
# ---------------------------------------------------------------------------


def _normalize_nodes(nodes: list[dict]) -> list[dict]:
    """没有 trigger 就补一个在链首（§3.1：建表时后端自动补，不可删）。"""
    nodes = list(nodes)
    if not any(n.get("node_type") == "trigger" for n in nodes):
        nodes.insert(
            0,
            {
                "node_type": "trigger",
                "node_key": "trigger",
                "node_name": "触发器",
                "seq": 0,
                "config": {},
            },
        )
    return nodes


def create_workflow(
    db: Session,
    user_id: int,
    *,
    name: str,
    trigger_type: str = _TRIGGER_MANUAL,
    cron_expression: str = "",
    run_at: Optional[datetime] = None,
    misfire_policy: str = "once",
    global_params: Optional[dict] = None,
    remark: str = "",
    nodes: Optional[list[dict]] = None,
    scheduler: Any = None,
) -> Workflow:
    """新建工作流：自动补 trigger 节点 + DAG 校验 + 调度装配。"""
    nodes = _normalize_nodes(list(nodes or []))
    try:
        validate_workflow(nodes)
        _validate_trigger(trigger_type, cron_expression, run_at)
    except WorkflowError as exc:
        # §1.3 / §4.2：DAG 与节点 schema 的非法输入必须在 API 层就是 400（不进库）。
        # 不接住 ⇒ 未捕获异常冒泡成 500，调用方误以为是服务端故障（TC-WF-011 同类闸）。
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    wf = Workflow(
        user_id=user_id,
        name=name,
        status=WF_ENABLED,
        trigger_type=trigger_type,
        cron_expression=cron_expression,
        run_at=run_at,
        misfire_policy=misfire_policy,
        global_params=global_params or {},
        remark=remark,
    )
    db.add(wf)
    db.flush()  # 拿 id

    for seq, node in enumerate(nodes):
        db.add(
            WorkflowNode(
                workflow_id=wf.id,
                user_id=user_id,
                node_type=node["node_type"],
                node_key=node["node_key"],
                node_name=node.get("node_name") or "",
                seq=seq,
                config=node.get("config") or {},
                enabled=bool(node.get("enabled", True)),
            )
        )
    wf.graph = build_graph(nodes)
    db.commit()

    _sync_scheduler(db, wf, scheduler=scheduler)
    return wf


def update_workflow(
    db: Session,
    workflow_id: int,
    user_id: int,
    *,
    name: Optional[str] = None,
    trigger_type: Optional[str] = None,
    cron_expression: Optional[str] = None,
    run_at: Optional[datetime] = None,
    misfire_policy: Optional[str] = None,
    global_params: Optional[dict] = None,
    remark: Optional[str] = None,
    status: Optional[str] = None,
    nodes: Optional[list[dict]] = None,
    scheduler: Any = None,
) -> Workflow:
    """改编排：有活跃 run 时 409（§6 端点 4）。"""
    wf = get_workflow(db, workflow_id, user_id)
    _reject_active_run(db, wf)

    trigger_changed = False
    if trigger_type is not None and wf.trigger_type != trigger_type:
        trigger_changed = True
    if cron_expression is not None and wf.cron_expression != cron_expression:
        trigger_changed = True
    if run_at is not None:
        trigger_changed = True

    if nodes is not None:
        nodes = _normalize_nodes(nodes)
        try:
            validate_workflow(nodes)
        except WorkflowError as exc:
            # 与新建同一条闸（§4.2）：改图时的非法输入也必须是 400，不落库。
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.execute(WorkflowNode.__table__.delete().where(WorkflowNode.workflow_id == wf.id))
        for seq, node in enumerate(nodes):
            db.add(
                WorkflowNode(
                    workflow_id=wf.id,
                    user_id=user_id,
                    node_type=node["node_type"],
                    node_key=node["node_key"],
                    node_name=node.get("node_name") or "",
                    seq=seq,
                    config=node.get("config") or {},
                    enabled=bool(node.get("enabled", True)),
                )
            )
        wf.graph = build_graph(nodes)

    if name is not None:
        wf.name = name
    if remark is not None:
        wf.remark = remark
    if global_params is not None:
        wf.global_params = global_params
    if misfire_policy is not None:
        wf.misfire_policy = misfire_policy
    if status is not None and status in (WF_ENABLED, WF_DISABLED):
        wf.status = status
    if trigger_type is not None:
        wf.trigger_type = trigger_type
    if cron_expression is not None:
        wf.cron_expression = cron_expression
    if run_at is not None:
        wf.run_at = run_at
    if trigger_type is not None or cron_expression is not None or run_at is not None:
        _validate_trigger(wf.trigger_type, wf.cron_expression, wf.run_at)

    db.commit()
    _sync_scheduler(db, wf, scheduler=scheduler, trigger_changed=trigger_changed)
    return wf


def delete_workflow(db: Session, workflow_id: int, user_id: int, *, scheduler: Any = None) -> None:
    """删除：有活跃 run 时 409；run 行因 SET NULL 保留（§6.1）。"""
    wf = get_workflow(db, workflow_id, user_id)
    _reject_active_run(db, wf)

    # 先摘调度注册（否则 job 行被 CASCADE 删掉，调度器里残留孤儿 job）
    _unsync_scheduler(db, wf.id, scheduler=scheduler)
    db.execute(WorkflowNode.__table__.delete().where(WorkflowNode.workflow_id == wf.id))
    db.delete(wf)
    db.commit()


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def _new_workflow_batch_no(db: Session, max_attempts: int = 8) -> str:
    """生成 WF 前缀批次号（复用一期随机段逻辑，§5.1）。"""
    from backend.app.services.note_queue_service import _BATCH_ALPHABET

    for _ in range(max_attempts):
        stamp = shanghai_now().strftime("%Y%m%d%H%M%S")
        tail = "".join(secrets.choice(_BATCH_ALPHABET) for _ in range(4))
        candidate = f"{BATCH_NO_PREFIX}{stamp}-{tail}"
        if db.scalar(select(WorkflowRun.id).where(WorkflowRun.batch_no == candidate)) is None:
            return candidate
    raise RuntimeError("生成唯一工作流批次号失败（连续重号，检查 ux_workflow_runs_batch_no）")


def start_run(
    db: Session,
    workflow_id: int,
    user_id: int,
    *,
    trigger_source: str = "manual",
    scheduled_job_log_id: Optional[int] = None,
) -> WorkflowRun:
    """立即执行（§6 端点 6）：建 run + 提交引擎。活跃 run 存在时 409。"""
    wf = get_workflow(db, workflow_id, user_id)
    if wf.status == WF_DISABLED:
        raise HTTPException(status_code=400, detail="工作流已停用，无法执行")
    existing = _active_run(db, wf)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"工作流已有运行中的实例（run #{existing.id}）",
        )

    nodes = snapshot_nodes(wf.id, db)
    if not nodes:
        raise HTTPException(status_code=400, detail="工作流没有任何节点")

    run = WorkflowRun(
        workflow_id=wf.id,
        user_id=user_id,
        workflow_name=wf.name,
        graph_snapshot={"nodes": nodes, **build_graph(nodes)},
        global_params_snapshot=dict(wf.global_params or {}),
        status="pending",
        trigger_source=trigger_source,
        scheduled_job_log_id=scheduled_job_log_id,
        batch_no=_new_workflow_batch_no(db),
        total_nodes=sum(1 for n in nodes if n.get("enabled", True)),
    )
    db.add(run)
    wf.run_count += 1
    wf.last_run_at = shanghai_now()
    db.commit()

    run_id = run.id
    threading.Thread(
        target=_execute_in_thread,
        args=(run_id,),
        name=f"workflow-run-{run_id}",
        daemon=True,
    ).start()
    return run


def _execute_in_thread(run_id: int) -> None:
    """引擎跑在一个守护线程里（一期所有 worker 同一风格：线程内吞异常 + 留痕）。"""
    try:
        execute_run(run_id)
    except Exception:  # noqa: BLE001
        logger.exception("workflow run %s crashed", run_id)


def _execute_with_review(run_id: int) -> None:
    """审核完成后续跑：把 waiting 日志行改回 running，再调引擎（§3.3）。"""
    db = SessionLocal()
    try:
        row = db.scalars(
            select(WorkflowRunLog).where(
                WorkflowRunLog.run_id == run_id,
                WorkflowRunLog.status == "waiting",
            )
        ).one_or_none()
        if row is not None:
            row.status = "running"
            db.commit()
    finally:
        db.close()
    try:
        execute_run(run_id)
    except Exception:  # noqa: BLE001
        logger.exception("workflow resume %s crashed", run_id)


def list_runs(
    db: Session, workflow_id: int, user_id: int, *, page: int = 1, page_size: int = 20
) -> dict:
    get_workflow(db, workflow_id, user_id)  # 404 隔离兜底
    stmt = (
        select(WorkflowRun)
        .where(WorkflowRun.workflow_id == workflow_id, WorkflowRun.user_id == user_id)
        .order_by(WorkflowRun.created_at.desc())
    )
    rows = db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [_serialize_run(r) for r in rows], "page": page, "page_size": page_size}


def get_run(db: Session, run_id: int, user_id: int) -> dict:
    """运行详情（§6 端点 8）：状态 + 节点日志 + 每步 detail。"""
    run = db.scalars(
        select(WorkflowRun).where(WorkflowRun.id == run_id, WorkflowRun.user_id == user_id)
    ).one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="运行实例不存在")

    logs = db.scalars(
        select(WorkflowRunLog)
        .where(WorkflowRunLog.run_id == run.id)
        .order_by(WorkflowRunLog.seq)
    ).all()
    return {
        **_serialize_run(run),
        "nodes": [
            {
                "node_id": lg.node_id,
                "node_key": lg.node_key,
                "node_type": lg.node_type,
                "node_name": lg.node_name,
                "seq": lg.seq,
                "status": lg.status,
                "started_at": lg.started_at.isoformat() if lg.started_at else None,
                "finished_at": lg.finished_at.isoformat() if lg.finished_at else None,
                "error_type": lg.error_type,
                "error_message": lg.error_message,
                "detail": lg.detail,
                "task_id": lg.task_id,
            }
            for lg in logs
        ],
        "graph": run.graph_snapshot,
        "global_params": run.global_params_snapshot,
    }


def submit_review(
    db: Session,
    run_id: int,
    node_key: str,
    user_id: int,
    *,
    decisions: dict[int, str],
) -> dict:
    """提交人工审核结果（§6 端点 11）：note_id -> approve/reject/skip 批量。"""
    run = db.scalars(
        select(WorkflowRun).where(WorkflowRun.id == run_id, WorkflowRun.user_id == user_id)
    ).one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="运行实例不存在")
    if run.status != "paused":
        raise HTTPException(status_code=409, detail="当前运行不在等待审核状态")

    log_row = db.scalars(
        select(WorkflowRunLog).where(
            WorkflowRunLog.run_id == run.id, WorkflowRunLog.node_key == node_key
        )
    ).one_or_none()
    if log_row is None or log_row.status != "waiting":
        raise HTTPException(status_code=409, detail=f"节点 {node_key} 不在等待审核")

    for verdict in decisions.values():
        if verdict not in ("approve", "reject", "skip"):
            raise HTTPException(status_code=400, detail=f"审核结论非法：{verdict!r}")

    # 决策落日志 detail（引擎的 review 节点会读它判 reject）
    log_row.detail = {
        "review_results": {str(k): v for k, v in decisions.items()},
    }
    db.commit()

    _execute_with_review(run.id)
    return {"run_id": run.id, "reviewed": len(decisions)}


def rerun_from_node(db: Session, run_id: int, node_key: str, user_id: int) -> dict:
    """从失败节点重跑（§6 端点 10 / §9.3）。"""
    run = db.scalars(
        select(WorkflowRun).where(WorkflowRun.id == run_id, WorkflowRun.user_id == user_id)
    ).one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="运行实例不存在")
    if run.status != "failed":
        raise HTTPException(status_code=409, detail="只有失败（failed）的运行才能重跑")

    keys = reset_downstream(run, node_key, db)
    if not keys:
        raise HTTPException(status_code=404, detail=f"运行里没有节点 {node_key!r}")
    run.trigger_source = "rerun"
    db.commit()

    threading.Thread(
        target=_execute_in_thread,
        args=(run.id,),
        name=f"workflow-rerun-{run.id}",
        daemon=True,
    ).start()
    return {"run_id": run.id, "reset": keys}


def cancel_run(db: Session, run_id: int, user_id: int) -> dict:
    """协作式取消（§6 端点 9 / §4.1）。"""
    from backend.app.services.workflow_engine import cancel_run as engine_cancel

    ok = engine_cancel(run_id, user_id)
    if not ok:
        raise HTTPException(status_code=409, detail="运行不在可取消状态")
    return {"run_id": run_id, "status": "cancelled"}


# ---------------------------------------------------------------------------
# 调度装配（§7.1）
# ---------------------------------------------------------------------------


def _sync_scheduler(
    db: Session,
    wf: Workflow,
    *,
    scheduler: Any = None,
    trigger_changed: bool = True,
) -> None:
    """把工作流的触发器装配成 scheduled_jobs 行（manual 档不注册）。"""
    from backend.app.services.scheduler_service import sync_job_registration

    if wf.status == WF_DISABLED or wf.trigger_type == _TRIGGER_MANUAL:
        _remove_job_row(db, wf.id, scheduler)
        return

    row = db.scalars(
        select(ScheduledJob).where(ScheduledJob.job_key == _job_key(wf.id))
    ).one_or_none()
    if row is None:
        row = ScheduledJob(
            user_id=wf.user_id,
            name=f"工作流 · {wf.name}",
            job_key=_job_key(wf.id),
            trigger_type=wf.trigger_type,
            cron_expression=wf.cron_expression,
            run_at=wf.run_at,
            misfire_policy=wf.misfire_policy,
            concurrent=CONCURRENT_FORBID,
            params={"workflow_id": wf.id},
            status=JOB_STATUS_ACTIVE,
            is_builtin=False,
        )
        db.add(row)
    else:
        row.trigger_type = wf.trigger_type
        row.cron_expression = wf.cron_expression
        row.run_at = wf.run_at
        row.misfire_policy = wf.misfire_policy
        row.name = f"工作流 · {wf.name}"
        row.status = JOB_STATUS_ACTIVE
    db.commit()

    sync_job_registration(scheduler, row, trigger_changed=trigger_changed)
    db.refresh(row)
    wf.next_run_at = row.next_run_at
    db.commit()


def _unsync_scheduler(db: Session, workflow_id: int, scheduler: Any = None) -> None:
    row = db.scalars(
        select(ScheduledJob).where(ScheduledJob.job_key == _job_key(workflow_id))
    ).one_or_none()
    if row is not None:
        from backend.app.services.scheduler_service import sync_job_registration

        sync_job_registration(scheduler, row)
        db.delete(row)
        db.commit()


def _remove_job_row(db: Session, workflow_id: int, scheduler: Any = None) -> None:
    row = db.scalars(
        select(ScheduledJob).where(ScheduledJob.job_key == _job_key(workflow_id))
    ).one_or_none()
    if row is not None:
        from backend.app.services.scheduler_service import sync_job_registration

        sync_job_registration(scheduler, row)
        db.delete(row)
        db.commit()


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _validate_trigger(trigger_type: str, cron: str, run_at: Optional[datetime]) -> None:
    if trigger_type not in (_TRIGGER_MANUAL, _TRIGGER_CRON, _TRIGGER_AT):
        raise HTTPException(status_code=400, detail=f"非法触发器类型：{trigger_type!r}")
    if trigger_type == _TRIGGER_CRON and not cron.strip():
        raise HTTPException(status_code=400, detail="cron 触发器必须填 cron 表达式")
    if trigger_type == _TRIGGER_AT:
        if run_at is None:
            raise HTTPException(status_code=400, detail="at 触发器必须填执行时刻")
        if run_at <= shanghai_now():
            raise HTTPException(status_code=400, detail="at 触发器的执行时刻必须晚于当前时间")


def _serialize_run(run: Optional[WorkflowRun]) -> dict:
    if run is None:
        return {}
    return {
        "id": run.id,
        "workflow_id": run.workflow_id,
        "workflow_name": run.workflow_name,
        "status": run.status,
        "trigger_source": run.trigger_source,
        "batch_no": run.batch_no,
        "total_nodes": run.total_nodes,
        "finished_nodes": run.finished_nodes,
        "error_type": run.error_type,
        "error_message": run.error_message,
        "current_node_id": run.current_node_id,
        "review_deadline": run.review_deadline.isoformat() if run.review_deadline else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }
