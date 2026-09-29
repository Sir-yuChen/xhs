from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from collections.abc import Iterator


PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

_proxy_env_lock = threading.RLock()

#: 进程内**首次**调用时的判定结果（`None` = 还没判过）。见 `_proxy_env_present()`。
#: ⚠️ 名字**不能**叫 `_proxy_env_present` —— 那会与判定函数同名，函数定义覆盖变量，
#: 判定于是永远返回函数对象（恒真）、快速路径永不生效（本轮实测踩到过：表现为
#: 「无代理也不并发」）。
_PROXY_ENV_PRESENT: bool | None = None


def _proxy_env_present() -> bool:
    """本进程环境里**到底有没有**代理变量。只判一次，之后走缓存（设计 13 §18.18.4）。

    ⚠️ **必须只判一次**：环境变量会被 `direct_xhs_request_env` 在锁内**临时摘掉再恢复**。
    若每次调用都重判，正处于**另一个线程清理窗口**内的调用会得到「没有代理」的错误
    答案 ⇒ 它走快速路径、**绕过那把锁**，于是「不带代理打小红书」变成随机事件。

    首次判定本身也串行（双检锁），避免多线程同时初始化。
    """
    global _PROXY_ENV_PRESENT
    if _PROXY_ENV_PRESENT is None:
        with _proxy_env_lock:
            if _PROXY_ENV_PRESENT is None:
                _PROXY_ENV_PRESENT = any(os.environ.get(key) for key in PROXY_ENV_KEYS)
    return _PROXY_ENV_PRESENT


@contextmanager
def direct_xhs_request_env() -> Iterator[None]:
    """Run Spider_XHS SDK calls without inheriting a broken local proxy.

    **没有代理环境变量时零成本**（设计 13 §18.18.4，2026-09-28）：本函数此前
    **无条件**获取全局锁，而锁的范围包含网络往返 ⇒ 任何并发 SDK 调用都被串行化
    （令牌探针的账号间并发会被这把锁整个吃掉）。而它要做的只有一件事：把本机代理
    变量摘掉 —— 环境里本来就没有代理时，那是 no-op。

    现在：没有代理 ⇒ 不持锁、不改环境；有代理 ⇒ **完全保留**原行为（持锁 + 摘变量
    + 恢复），正确性优先，绝不静默降级为「带着本机代理打小红书」。
    """
    if not _proxy_env_present():
        yield
        return
    with _proxy_env_lock:
        original = {key: os.environ.get(key) for key in PROXY_ENV_KEYS}
        for key in PROXY_ENV_KEYS:
            os.environ.pop(key, None)
        try:
            yield
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
