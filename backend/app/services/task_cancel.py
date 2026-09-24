"""同步任务的**协作式中断**（设计 13 §24.8）。

为什么需要这个模块：Python 没有任何框架能强制杀死一个运行中的线程
（Celery 的 `revoke(terminate=True)` 也只是 SIGKILL worker 进程，官方文档明确
「不要程序化调用」；见设计 13 §24.10 调研）。所以「取消」的唯一正确形态是：

1. 取消端点把任务行置终态 `cancelled` + 写 `finished_at`，**并**在这里发信号；
2. 任务体在**检查点**（同步翻页每页、详情补齐每条）读信号，命中即优雅退出。

只有第 1 步没有第 2 步 = 「假取消」（DB 说 cancelled，线程还在打平台）；
只有第 2 步没有第 1 步 = 用户点了没反馈。两步必须同时在。

登记表是**进程内**的：同步任务本来就是 API 进程里的 daemon 线程，进程重启
线程即消亡（任务行由取消端点 / 巡检兜底终态化），不需要跨进程共享。
"""

from __future__ import annotations

import threading
from typing import Any, Optional

_lock = threading.Lock()
_events: dict[int, threading.Event] = {}


def _key(task_id: Any) -> Optional[int]:
    try:
        return int(task_id)
    except (TypeError, ValueError):
        return None


def register(task_id: Any) -> Optional[threading.Event]:
    """为任务登记一个取消事件（建行时调用；重复登记返回同一个）。"""
    key = _key(task_id)
    if key is None:
        return None
    with _lock:
        event = _events.get(key)
        if event is None:
            event = threading.Event()
            _events[key] = event
        return event


def signal(task_id: Any) -> None:
    """发取消信号（取消端点调用）。未登记 = 任务不在本进程跑，静默跳过。"""
    key = _key(task_id)
    if key is None:
        return
    with _lock:
        event = _events.get(key)
    if event is not None:
        event.set()


#: `signal` 的别名（API 层 import 用）：避免与标准库 `signal` 模块撞名误导读者。
signal_task_cancel = signal


def is_cancelled(task_id: Any) -> bool:
    """任务体检查点调用：是否已被取消。"""
    key = _key(task_id)
    if key is None:
        return False
    with _lock:
        event = _events.get(key)
    return bool(event and event.is_set())


def clear(task_id: Any) -> None:
    """任务终态后清理（防止登记表无限增长）。终态后再取消由端点幂等处理。"""
    key = _key(task_id)
    if key is None:
        return
    with _lock:
        _events.pop(key, None)


def clear_all() -> None:
    """清空整个登记表。

    生产不需要调（进程退出即消亡）；**测试隔离**用 —— task_id 是库内自增，
    每个用例一个临时库时 id 从 1 重新开始，不清就会读到上一个用例的信号。
    """
    with _lock:
        _events.clear()
