"""进程级共享的浏览器模拟会话 —— 省掉「每个请求都重做一次握手」（设计 13 §18.18.8）。

## 为什么需要它

适配器每次调用都 `from_cookie()` 新建 Auth ⇒ 每次都新建一个 `curl_cffi.Session`
⇒ 每个请求都要重做 DNS + TLS + HTTP/2 握手。实测（只读探针链路）：

| | 耗时 |
|---|---|
| 新建会话 + 冷连接 | **157–213 ms** |
| 复用会话的连接 | **40–48 ms** |

## 为什么可以跨线程共享（实测 + 官方口径）

* curl_cffi 0.16.3 的 `Session` **官方声明 thread-safe**（`requests/session.py`：
  「thread-safe, but it's recommended to use a separate session for each thread」），
  且 `use_thread_local_curl=True` 是**默认值** ⇒ 内部 curl 句柄本身就是每线程一份，
  连接池天然按线程隔离 —— 共享一个 Session **不会**让两个线程抢同一个 C 层句柄；
* 本机实测：8 线程并发共享同一个 Session 发 16 次请求，**16/16 成功、无异常**、
  响应体一致，且连接复用确实生效（前 8 次 163–195 ms，同线程的第 2 次 40–74 ms）；
* 会话以 `discard_cookies=True` 创建、且每次请求都**显式传 Cookie 头** ⇒ 会话
  **不保存 cookie**，跨账号共享不会串号。

## 契约

* 每平台**一个**进程级会话：懒创建、创建加锁、线程安全；
* **谁都不许 close 它** —— 它是共享资源（`backend/app/` 内没有任何调用点会关它，
  由 `tests/backend/test_http_session_shared.py` 钉住）；
* 回退手段：把 `SHARED_SESSIONS_ENABLED` 置 `False` ⇒ 回到「每次新建会话」。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: 共享会话总开关。`False` = 每次新建（回退开关，行为与改造前一致）。
SHARED_SESSIONS_ENABLED = True

#: 会话标识（与平台通道同值；只作为登记键，不参与业务判断）。
SESSION_WWW = "www"
SESSION_CREATOR = "creator"

_lock = threading.Lock()
_clients: dict[str, Any] = {}


def shared_client(platform: str, factory: Callable[[], Any]) -> Any:
    """取该平台的进程级共享会话；没有就按 `factory()` 建一个。

    命中缓存时**不调用 factory**（那正是省下来的开销）。
    """
    if not SHARED_SESSIONS_ENABLED:
        return factory()
    client = _clients.get(platform)
    if client is not None:
        return client
    with _lock:
        client = _clients.get(platform)
        if client is None:
            client = factory()
            _clients[platform] = client
            logger.debug("shared http session created: platform=%s", platform)
    return client


def close_all() -> None:
    """关掉所有共享会话并清空登记。**仅供测试收尾**。

    生产链路不得调用：它关掉的是**所有线程**都在用的会话。
    """
    with _lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - 收尾失败不该掩盖测试结果
            logger.warning("shared http session close failed", exc_info=True)
