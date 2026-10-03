"""自动化工作流 API（设计 19 §6，11 个端点）。

全部挂在 ``/api/platforms/xhs/`` 下，全部走 ``current_user`` 依赖、
全部 ``user_id`` 过滤（§10.1）。调度器实例从 ``request.app.state`` 显式传入 service。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models.user import User
from backend.app.services import workflow_service

router = APIRouter(prefix="/platforms/xhs", tags=["workflow"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class NodeIn(BaseModel):
    node_type: str
    node_key: str
    node_name: str = ""
    seq: int = 0
    config: dict[str, Any] = {}
    enabled: bool = True


class WorkflowIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    trigger_type: str = "manual"
    cron_expression: str = ""
    run_at: Optional[datetime] = None
    misfire_policy: str = "once"
    global_params: dict[str, Any] = {}
    remark: str = ""
    nodes: list[NodeIn] = []


class WorkflowUpdate(BaseModel):
    name: Optional[str] = None
    trigger_type: Optional[str] = None
    cron_expression: Optional[str] = None
    run_at: Optional[datetime] = None
    misfire_policy: Optional[str] = None
    global_params: Optional[dict[str, Any]] = None
    remark: Optional[str] = None
    status: Optional[str] = None
    nodes: Optional[list[NodeIn]] = None


class ReviewIn(BaseModel):
    decisions: dict[int, str] = Field(..., description="note_id -> approve/reject/skip")


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get("/workflows")
def list_workflows(
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """列表（含最近一次 run 概要）。"""
    return workflow_service.list_workflows(
        db, current_user.id, status=status, page=page, page_size=page_size
    )


@router.post("/workflows")
def create_workflow(
    body: WorkflowIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """新建工作流（校验 DAG + 节点 schema，自动补 trigger 节点）。"""
    scheduler = getattr(request.app.state, "scheduler", None)
    wf = workflow_service.create_workflow(
        db,
        current_user.id,
        name=body.name,
        trigger_type=body.trigger_type,
        cron_expression=body.cron_expression,
        run_at=body.run_at,
        misfire_policy=body.misfire_policy,
        global_params=body.global_params,
        remark=body.remark,
        nodes=[n.model_dump() for n in body.nodes],
        scheduler=scheduler,
    )
    return {"id": wf.id, "name": wf.name, "status": wf.status}


@router.get("/workflows/{workflow_id}")
def get_workflow(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """详情（节点列表 + 图 + 最近一次 run 概要）。"""
    wf = workflow_service.get_workflow(db, workflow_id, current_user.id)
    from backend.app.models.workflow import WorkflowNode

    nodes = (
        db.query(WorkflowNode)
        .filter(WorkflowNode.workflow_id == wf.id)
        .order_by(WorkflowNode.seq)
        .all()
    )
    last = workflow_service._last_run(db, wf.id)
    return {
        "id": wf.id,
        "name": wf.name,
        "status": wf.status,
        "trigger_type": wf.trigger_type,
        "cron_expression": wf.cron_expression,
        "run_at": wf.run_at.isoformat() if wf.run_at else None,
        "misfire_policy": wf.misfire_policy,
        "global_params": wf.global_params,
        "remark": wf.remark,
        "graph": wf.graph,
        "nodes": [
            {
                "id": n.id,
                "node_type": n.node_type,
                "node_key": n.node_key,
                "node_name": n.node_name,
                "seq": n.seq,
                "config": n.config,
                "enabled": n.enabled,
            }
            for n in nodes
        ],
        "last_run": workflow_service._serialize_run(last) if last else None,
        "run_count": wf.run_count,
        "fail_count": wf.fail_count,
        "next_run_at": wf.next_run_at.isoformat() if wf.next_run_at else None,
        "created_at": wf.created_at.isoformat() if wf.created_at else None,
        "updated_at": wf.updated_at.isoformat() if wf.updated_at else None,
    }


@router.put("/workflows/{workflow_id}")
def update_workflow(
    workflow_id: int,
    body: WorkflowUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """改编排（有活跃 run 时 409）。"""
    scheduler = getattr(request.app.state, "scheduler", None)
    wf = workflow_service.update_workflow(
        db,
        workflow_id,
        current_user.id,
        name=body.name,
        trigger_type=body.trigger_type,
        cron_expression=body.cron_expression,
        run_at=body.run_at,
        misfire_policy=body.misfire_policy,
        global_params=body.global_params,
        remark=body.remark,
        status=body.status,
        nodes=[n.model_dump() for n in body.nodes] if body.nodes is not None else None,
        scheduler=scheduler,
    )
    return {"id": wf.id, "name": wf.name, "status": wf.status}


@router.delete("/workflows/{workflow_id}")
def delete_workflow(
    workflow_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除（有活跃 run 时 409；run 行因 SET NULL 保留）。"""
    scheduler = getattr(request.app.state, "scheduler", None)
    workflow_service.delete_workflow(db, workflow_id, current_user.id, scheduler=scheduler)
    return {"id": workflow_id, "deleted": True}


@router.post("/workflows/{workflow_id}/run")
def run_workflow(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """立即执行（生成 run，提交引擎；活跃 run 存在时 409）。"""
    run = workflow_service.start_run(db, workflow_id, current_user.id, trigger_source="manual")
    return {"run_id": run.id, "status": run.status, "batch_no": run.batch_no}


@router.get("/workflows/{workflow_id}/runs")
def list_runs(
    workflow_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """运行历史列表。"""
    return workflow_service.list_runs(
        db, workflow_id, current_user.id, page=page, page_size=page_size
    )


@router.get("/workflow-runs/{run_id}")
def get_run(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """运行详情（状态 + 节点日志 + 每步 detail）。"""
    return workflow_service.get_run(db, run_id, current_user.id)


@router.post("/workflow-runs/{run_id}/cancel")
def cancel_run(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """取消运行（协作式）。"""
    return workflow_service.cancel_run(db, run_id, current_user.id)


@router.post("/workflow-runs/{run_id}/nodes/{node_key}/rerun")
def rerun_node(
    run_id: int,
    node_key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """从失败节点重跑。"""
    return workflow_service.rerun_from_node(db, run_id, node_key, current_user.id)


@router.post("/workflow-runs/{run_id}/nodes/{node_key}/review")
def submit_review(
    run_id: int,
    node_key: str,
    body: ReviewIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """提交人工审核结果（note_id -> approve/reject/skip 批量）。"""
    return workflow_service.submit_review(
        db, run_id, node_key, current_user.id, decisions=body.decisions
    )
