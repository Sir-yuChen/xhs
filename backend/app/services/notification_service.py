from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from backend.app.models.notification import Notification
from backend.app.models.task import Task


def notify_task_failed(db: Session, task: Task) -> Notification:
    return _create(
        db,
        user_id=task.user_id,
        title=f"任务失败: {task.task_type}",
        body=(task.payload or {}).get("error", ""),
        level="warning",
        source_task_id=task.id,
        source_type="task",
        source_id=task.id,
    )


def notify_task_exhausted(db: Session, task: Task) -> Notification:
    return _create(
        db,
        user_id=task.user_id,
        title=f"任务重试耗尽: {task.task_type}",
        body=f"已重试 {task.retry_count} 次",
        level="error",
        source_task_id=task.id,
        source_type="task",
        source_id=task.id,
    )


def notify_target_paused(db: Session, user_id: int, target_name: str, target_id: int) -> Notification:
    return _create(
        db,
        user_id=user_id,
        title=f"监控已暂停: {target_name}",
        body="连续 3 次刷新失败，已自动暂停",
        level="error",
        source_type="target",
        source_id=target_id,
    )


def notify_account_skipped(
    db: Session, *, user_id: int, account_id: int, account_name: str, reason: str
) -> Notification:
    """**账号被跳过**（每日发布商业笔记任务：千帆无效 ⇒ 跳过 + 提醒）。

    设计 15 §19.4：发送时机 = **跳过那一刻**（与 `shop_health_service` 的
    「状态翻转 → 一条铃铛」同口径 —— 同一条规则不留两种触发时机），不是整轮
    结束后汇总。

    ⚠️ 与 `shop_health_service._notify_invalid` **不是重复通知**：那条说的是
    「这个账号的千帆失效了」，本条说的是「这一轮我因为失效跳过了它」——
    前者只在**状态翻转**时发（第二次失效不再响），后者**每一轮跳过都发**
    （否则用户永远不知道今天的任务又空跑了）。
    文案必须**可执行**：说清是谁、去做什么。
    """
    name = (account_name or "").strip() or f"账号 #{account_id}"
    detail = (reason or "").strip() or "千帆登录态无效"
    return _create(
        db,
        user_id=user_id,
        title=f"已跳过账号：{name}",
        body=f"{detail}。请到账号页重新登录该账号的千帆后重试。",
        level="warning",
        source_type="account",
        source_id=account_id,
    )


def _create(
    db: Session,
    *,
    user_id: int,
    title: str,
    body: str = "",
    level: str = "info",
    source_task_id: Optional[int] = None,
    source_type: Optional[str] = None,
    source_id: Optional[int] = None,
) -> Notification:
    n = Notification(
        user_id=user_id,
        title=title,
        body=body,
        level=level,
        source_task_id=source_task_id,
        source_type=source_type,
        source_id=source_id,
    )
    db.add(n)
    db.flush()
    return n
