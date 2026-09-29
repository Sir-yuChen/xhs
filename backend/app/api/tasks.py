from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.api.publish import get_creator_publish_adapter_factory
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.time import shanghai_now
from backend.app.models import Task, User
from backend.app.schemas.common import paginated
from backend.app.services.note_queue_service import QUEUE_BATCH_TASK_TYPES, SOURCE_RETRY
from backend.app.services.scheduler_service import run_due_publish_jobs
from backend.app.services.task_cancel import signal_task_cancel

router = APIRouter(prefix="/tasks", tags=["tasks"])

logger = logging.getLogger(__name__)


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
        # 唯一批次号（只有批次父行有值；队列项为 None）—— 前端据它识别批次行
        # （「这一行是不是批次」由数据回答，不在前端写死类型清单）。
        "batch_no": task.batch_no,
    }


def _get_owned_task(db: Session, current_user: User, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None or task.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


def _reject_batch_row(task: Task, action: str) -> None:
    """批次父行**不是可执行任务**：单独取消 / 重试它只改父行 ⇒ 父子树自相矛盾
    （批次说「已取消」，里面的篇还在跑 / 还在等）。

    ⚠️ 明确 400 而不是静默返回：静默会让前端以为操作成功（DEF-058 的「把没做成的
    事说成做成了」）。前端也不给批次行渲染这两个按钮 —— 这里只是边界
    （DEF-048：前端是体验，后端是安全网）。
    """
    if str(task.task_type or "") in QUEUE_BATCH_TASK_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "BATCH_ROW_NOT_OPERABLE",
                "message": f"批次行不能{action}：请到该批次的明细记录上操作（批次行只是一次入队动作的记账行）",
            },
        )


@router.get("")
def get_tasks(
    platform: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    task_types: str = Query(
        "",
        description=(
            "按任务类型筛选（**逗号分隔**，如 `xhs_note_sync,xhs_shop_goods_sync`）。"
            "任务中心「账号级任务」页签用它筛三类账号级任务（设计 13 §35.16）。"
            "⚠️ 刻意用逗号串而不是可重复参数：axios 对数组默认序列化成 `task_types[]=`，"
            "FastAPI 不认（静默筛不到 ⇒ 页面空列表却不报错），逗号串两端都无歧义。"
        ),
    ),
    tree: bool = Query(
        False,
        description="队列记录页的父子树模式（批次行 + 内嵌 children + 派生状态）",
    ),
    batch_no: str = Query("", description="按唯一批次号过滤（tree 模式；不区分大小写的包含匹配）"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """任务列表。两种形状（**默认形状不变**，老调用方零影响）：

    * `tree=false`（默认）：平铺 + 分页，原样（历史行为，谁在用都不受影响）；
    * `tree=true`：**队列记录页要的父子树** —— 顶级只给批次父行（一次入队动作一行），
      批次行内嵌 `children`，并带上按子行现算的 `summary`（状态 / 计数 / 耗时）。

    为什么做成同一个端点的参数而不是新端点：这一页要的是「`tasks` 的另一种视图」，
    不是另一份资源；新开端点等于让「任务的权限 / 归属收口」多一处要同步维护
    （DEF-044 同型：同一语义不留两份实现）。形状的切换由 `tree` 明确声明，
    读代码时不会猜。
    """
    if tree:
        return _queue_record_tree(
            db,
            current_user,
            limit=page_size,
            batch_no=batch_no,
        )
    statement = select(Task).where(Task.user_id == current_user.id)
    if platform:
        statement = statement.where(Task.platform == platform)
    if task_types:
        # §35.16：筛选必须在**后端**做 —— 前端过滤会把账号级行从分页里切掉。
        wanted = [item.strip() for item in str(task_types).split(",") if item.strip()]
        if wanted:
            statement = statement.where(Task.task_type.in_(wanted))
    tasks = db.scalars(statement.order_by(Task.created_at.desc(), Task.id.desc())).all()
    return paginated([serialize_task(task) for task in tasks], page, page_size)


def _queue_record_tree(
    db: Session,
    current_user: User,
    *,
    limit: int,
    batch_no: str,
) -> dict:
    """队列记录页的**父子树**（2026-09-26 用户口径「批次+详情父子树风格展示」）。

    * 顶级行 = **批次父行**（一次入队动作 = 一个唯一批次号），按创建时间倒序取
      `limit` 条。`list_queue_record_roots` 只列批次行 —— 用户口径「不做兼容逻辑，
      数据已清空」：不再有「无父的队列项行」这种存量形态（批次行是队列项的唯一
      入口），顶级行 = 批次行是这一页的不变量；
    * 有 `batch_no` 时只列批次号匹配的批次行（**唯一批次号查询**，同日用户口径
      「新增列表支持使用唯一批次号查询」）；
    * 每个批次行内嵌 `children`（FIFO 序）+ `summary`，且 `status` / `progress` /
      `duration_ms` / `started_at` / `finished_at` **一律用按子行现算的值覆盖** ——
      库里那份是冗余写回（`refresh_batch_status`），读取路径只认现算的（这样即使
      写回漏了一次，页面也不会显示陈旧状态）。⚠️ 批次行**自己的 `created_at` 不覆盖**
      —— 它是批次落库的时刻（= 用户点下那个按钮的时刻），「批次创建时间」列就取它。
    * 另派生 `accounts`（= `batch_account_ids(children)`：把子行 `payload.account_id`
      去重保序）—— 「**账号**」列取它（2026-09-26 用户口径「批次记录执行的账号」）；
      明细行不带这个键，它看自己 `payload.account_id`。
    """
    from backend.app.services import note_queue_service as nqs

    roots = nqs.list_queue_record_roots(
        db, user_id=current_user.id, limit=limit, batch_no=batch_no
    )
    items: list[dict] = []
    for root in roots:
        node = serialize_task(root)
        children_rows = nqs.children_of(db, parent_id=int(root.id), user_id=current_user.id)
        node["children"] = [serialize_task(child) for child in children_rows]
        summary = nqs.batch_summary(children_rows)
        node["status"] = summary["status"]
        node["progress"] = summary["progress"]
        node["duration_ms"] = summary["duration_ms"]
        node["started_at"] = summary["started_at"].isoformat() if summary["started_at"] else None
        node["finished_at"] = summary["finished_at"].isoformat() if summary["finished_at"] else None
        # 计数给前端渲染「共 N 条明细 · 成功 x · 失败 y …」（时间字段已单独覆盖，
        # 不重复塞进 summary）
        node["summary"] = {
            key: value
            for key, value in summary.items()
            if key not in ("started_at", "finished_at")
        }
        # 账号（2026-09-26 用户口径「批次记录执行的账号」）：**同样由子行派生**
        # （`batch_account_ids`，去重保序）—— 一次批量可跨账号勾选，批次级不存在
        # 「唯一账号」这回事；明细各自的账号看它自己 `payload.account_id`。
        node["accounts"] = nqs.batch_account_ids(children_rows)
        items.append(node)
    return {"total": len(items), "page": 1, "page_size": limit, "items": items}


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


class CleanTasksRequest(BaseModel):
    """手动清理的 id 列表（单条 = 一个 id；批量 = 选中的多个）。"""

    task_ids: list[int]

    model_config = {"extra": "forbid"}


@router.post("/clean")
def clean_tasks(
    payload: CleanTasksRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """手动清理**指定**队列记录（单条 / 批量选中；2026-09-26 用户口径）。

    ⚠️ **待重试（`failed` / `exhausted`）与进行中（`pending` / `running`）不删**：
    删了用户就失去重试入口 / 丢任务。后端按状态判据硬拦（`skipped` 计数）——
    前端禁用复选框只是体验，这里才是边界（DEF-048：安全网不能条件性）。
    """
    from backend.app.services.note_queue_service import clean_records

    return clean_records(db, user_id=current_user.id, task_ids=payload.task_ids)


@router.post("/clean-all")
def clean_all_tasks(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """一键清除**全部可清**队列记录（`done` / `cancelled`；待重试 / 进行中不碰）。"""
    from backend.app.services.note_queue_service import clean_records

    return clean_records(db, user_id=current_user.id, task_ids=None)


# ⚠️ 2026-09-24（设计 15 §8）：`GET /tasks/scheduler/status` 已删除 —— 由
# `GET /api/schedule/status` 取代（调度器活值 + 全部任务行 + 状态计数）。
# 前端同轮改指；测试见 tests/backend/test_api.py 的调度状态用例（已改写）。


@router.get("/queues")
def list_queues(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """队列清单 + 各队列实时计数（设计 13 §35.11，队列记录页用）。

    **为什么由后端下发**：队列不止同步 / 发布两种（后端服务可能有很多不同的队列）。
    前端写死类型常量 ⇒ 第三种队列上线时界面不显示（「按钮在、功能不在」的老问题）。
    这里是代码侧白名单（`note_queue_service.QUEUE_REGISTRY`）：新增队列 = 加一行，
    前端从本端点拿清单渲染，**零前端改动**。

    计数按当前用户收口（DEF-066），且只统计**保留期内**（15 天）的行 —— 与清理任务
    同口径，页面文案说「近 15 天」而不是「累计」。
    """
    from backend.app.services.note_queue_service import queue_summaries

    return {"items": queue_summaries(db, user_id=current_user.id)}


@router.get("/{task_id}")
def get_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # ⚠️ children 查询保留（通用端点）：**抓取 / 监控任务树**仍在用
    # `parent_task_id`（`monitoring_crawl` 父 + `note_crawl` 子）。2026-09-26
    # 常驻化只移除了**笔记队列的批次层**（队列项不再有父行），不影响别的任务类型。
    # 队列记录页的抽屉按 `hasChildren` 判据渲染：没有子行就显示这一行本身。
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
    _reject_batch_row(task, "取消")
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
        # 队列项被取消 ⇒ 它的批次父行派生状态跟着变（写回是冗余，读取仍按子行现算）
        if result.rowcount:
            _refresh_parent_batch(db, task)
            # 任务日志**同步收口**（2026-09-27 用户口径「点击取消按钮二次确认后真实取消
            # 再执行的任务并修改对应任务日志记录相关状态和数据」）：取消是用户可见的终态，
            # 日志不能等观测线程（它可能已随进程消失、或还在轮询周期里）—— 否则又会出现
            # 「日志说正在执行、任务已取消」两个相反结论。
            #
            # ⚠️ **收口失败不许让整个请求失败**（2026-09-27 用户报障「操作列点击取消任务
            # 提示请求失败，但任务实际已经被取消了」）：上面那次 `db.commit()` 已经把取消
            # **落库**了 —— 它是不可回滚的既成事实。收口只是**附加动作**，它抛异常时返回 500
            # 等于把「日志没同步」说成「取消失败」，是**错误归因**（用户会重复点，而任务早已
            # 取消；更糟的是他可能以为没取消掉）。留痕 + 让请求正常返回；残留的 running 日志
            # 由启动时的 `close_orphan_running_logs` 兜底收口（安全网不条件性，DEF-048）。
            from backend.app.services import sync_task_service

            try:
                if sync_task_service.close_running_logs_for_task(db, task.id):
                    db.commit()
            except Exception:  # noqa: BLE001 - 主操作已落库，附加动作不许翻盘
                logger.exception("取消任务 %s：日志收口失败（取消本身已生效）", task.id)
                db.rollback()
    return serialize_task(task)


def _refresh_parent_batch(db: Session, task: Task) -> None:
    """本端点直接改了行状态 ⇒ 刷新它的批次父行（2026-09-26 批次层）。

    `cancel` / `retry` 是**绕开 worker 记账路径**改状态的（`finish_item` 那条路
    自己会刷），所以必须在这里补一次；无父行 / 父行已不在 ⇒ 静默跳过（脏数据不该
    让取消 / 重试失败）。
    """
    from backend.app.services.note_queue_service import refresh_batch_status

    parent_id = int(task.parent_task_id or 0)
    if not parent_id:
        return
    parent = db.get(Task, parent_id)
    if parent is None:
        return
    refresh_batch_status(db, parent)
    db.commit()


@router.post("/{task_id}/retry")
def retry_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """重试一个**失败 / 重试用尽**的队列项：本行复位 `pending`，worker 5 秒内认领重跑。

    2026-09-26 常驻化后一行 = 一个队列项（没有批次父行）⇒ 不再有「批次行连带
    复位子行」的分支；`payload.source` 标成 `retry`（用户口径「给每个来源固定
    值」）：这一跑来自用户在队列记录里点「重试」，不是当初入队那次。

    ⚠️ 不显式唤醒 worker：三个 worker 空闲时都是 `wait(timeout=5s)` 兜底巡检，
    复位后的行最迟 5 秒被认领（唤醒只是提示，不是唯一入口 —— DEF-048）。
    """
    task = _get_owned_task(db, current_user, task_id)
    _reject_batch_row(task, "重试")
    if task.status in {"failed", "exhausted"}:
        _reset_for_retry(task)
        db.commit()
        db.refresh(task)
        # 复位成 pending ⇒ 批次从「失败」回到「进行中」（同在批次里，不另开新批次：
        # 它对那次动作的归属没变，本轮只是重跑）
        _refresh_parent_batch(db, task)
    return serialize_task(task)


def _reset_for_retry(task: Task) -> None:
    """把一行复位成可重跑状态（状态 / 进度 / 计时 / 来源标 retry）。不 commit。"""
    payload = dict(task.payload or {})
    payload["source"] = SOURCE_RETRY
    task.payload = payload
    task.status = "pending"
    task.progress = 0
    task.retry_count = int(task.retry_count or 0) + 1
    task.error_type = None
    task.started_at = None
    task.finished_at = None
