from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.api.publish import get_creator_publish_adapter_factory
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.time import shanghai_now
from backend.app.models import Task, User
from backend.app.schemas.common import paginated
from backend.app.services.scheduler_service import run_due_publish_jobs
from backend.app.services.task_cancel import signal_task_cancel

router = APIRouter(prefix="/tasks", tags=["tasks"])


def serialize_task(task: Task) -> dict:
    duration_ms = None
    if task.started_at and task.finished_at:
        duration_ms = int((task.finished_at - task.started_at).total_seconds() * 1000)
    return {
        "id": task.id,
        "platform": task.platform,
        "task_type": task.task_type,
        "status": task.status,
        "progress": task.progress,
        "payload": task.payload or {},
        "created_at": task.created_at.isoformat(),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "duration_ms": duration_ms,
        "error_type": task.error_type,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
        "parent_task_id": task.parent_task_id,
    }


def _get_owned_task(db: Session, current_user: User, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None or task.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


@router.get("")
def get_tasks(
    platform: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = select(Task).where(Task.user_id == current_user.id)
    if platform:
        statement = statement.where(Task.platform == platform)
    tasks = db.scalars(statement.order_by(Task.created_at.desc(), Task.id.desc())).all()
    return paginated([serialize_task(task) for task in tasks], page, page_size)


@router.post("/run-due")
def run_due_tasks(
    platform: str = Query("xhs"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_creator_publish_adapter_factory),
):
    return run_due_publish_jobs(
        db=db,
        current_user=current_user,
        now=None,
        platform=platform,
        adapter_factory=adapter_factory,
    )


# ⚠️ 2026-09-24（设计 15 §8）：`GET /tasks/scheduler/status` 已删除 —— 由
# `GET /api/schedule/status` 取代（调度器活值 + 全部任务行 + 状态计数）。
# 前端同轮改指；测试见 tests/backend/test_api.py 的调度状态用例（已改写）。


@router.get("/{task_id}")
def get_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = _get_owned_task(db, current_user, task_id)
    result = serialize_task(task)
    children = db.scalars(
        select(Task)
        .where(Task.parent_task_id == task.id, Task.user_id == current_user.id)
        .order_by(Task.created_at.asc())
    ).all()
    result["children"] = [serialize_task(c) for c in children]
    return result


@router.post("/{task_id}/cancel")
def cancel_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """取消一个**未完成**的任务（设计 13 §24.8：协作式中断）。

    三步一体，缺一即「假取消」：

    1. DB 行置 ``cancelled`` **并写 ``finished_at``**（耗时统计的终点）；
    2. 发进程内取消信号 —— 同步任务体的检查点（翻页每页 / 补详情每条）
       据此优雅退出，不再打平台；
    3. **条件更新**：只动仍在 ``pending`` / ``running`` 的行。线程刚提交
       done/failed 时旧实现会把终态覆写回 cancelled（last-writer-wins），
       条件更新堵掉这个竞态；重复取消幂等（行已是终态 ⇒ 原样返回、不重发信号）。
    """
    task = _get_owned_task(db, current_user, task_id)
    if task.status in {"pending", "running"}:
        result = db.execute(
            update(Task)
            .where(Task.id == task.id, Task.status.in_(("pending", "running")))
            .values(status="cancelled", finished_at=shanghai_now())
        )
        db.commit()
        if result.rowcount:
            signal_task_cancel(task_id)
        db.refresh(task)
    return serialize_task(task)


@router.post("/{task_id}/retry")
def retry_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = _get_owned_task(db, current_user, task_id)
    if task.status in {"failed", "exhausted"}:
        task.status = "pending"
        task.progress = 0
        task.retry_count = task.retry_count + 1
        task.error_type = None
        task.started_at = None
        task.finished_at = None
        db.commit()
        db.refresh(task)
    return serialize_task(task)
