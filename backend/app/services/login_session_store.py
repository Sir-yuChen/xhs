"""登录会话的**进程内存储**（2026-10-28 用户口径：扫码态走内存，不落库）。

## 为什么能进内存

登录状态是「一次登录动作的过程数据」，不是账号数据：
① 建会话时账号还**不存在**（扫码前不知道是谁）⇒ 无法挂到账号表；
② 寿命极短（用户扫码 → 确认，通常秒级），确认后唯一有价值的两个字段
   （`last_login_at` / `last_login_method`）已写进 `platform_accounts`；
③ `login_sessions` 表实测 226 行里 **204 条是 pending**（从未完成的扫码）
   ⇒ 落库只留下垃圾。

## 代价（已确认接受）

**服务重启会丢失所有进行中的扫码**，用户需要重新点「扫码登录」。
本项目 launcher（`scripts/run-app.py`）崩了会 5s 重启，扫码窗口被打断时用户需重扫一次。

## 设计

- 主键自增，与原来的 `login_sessions.id` 语义一致（前端仍按 `session_id` 轮询）；
- 带 TTL（默认 10 分钟）惰性过期：每次读写顺手清掉超时条目，不靠后台线程；
- 线程安全：同一进程多线程读写加锁（FastAPI 同步端点跑在线程池）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

#: 会话存活时间（秒）。扫码二维码本身在平台侧几分钟就失效，这里给足余量。
SESSION_TTL_SECONDS = 600


@dataclass
class LoginSessionState:
    """一次登录会话的全部状态（原 `login_sessions` 表 11 列减掉 user/platform）。"""

    id: int
    user_id: int
    sub_type: str
    status: str = "pending"
    login_method: str = "qr"
    qr_id: Optional[str] = None
    code: Optional[str] = None
    qr_url: Optional[str] = None
    #: 加密后的临时态（cookies / sync_creator / device_state / host_cookie_state / target_account_id）。
    #: 仍加密：这里面有平台下发的中间 Cookie，内存里也不该是明文。
    encrypted_temp_cookies: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def expired(self, ttl: int = SESSION_TTL_SECONDS) -> bool:
        return (time.time() - self.created_at) > ttl


class LoginSessionStore:
    """线程安全的内存会话表。"""

    def __init__(self, ttl: int = SESSION_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._items: dict[int, LoginSessionState] = {}
        self._next_id = 1

    def create(self, **kwargs: Any) -> LoginSessionState:
        with self._lock:
            self._purge_locked()
            session = LoginSessionState(id=self._next_id, **kwargs)
            self._items[session.id] = session
            self._next_id += 1
            return session

    def get(self, session_id: int) -> Optional[LoginSessionState]:
        with self._lock:
            self._purge_locked()
            return self._items.get(int(session_id))

    def get_owned(self, session_id: int, user_id: int) -> Optional[LoginSessionState]:
        """按归属取（越权一律 None —— 与原来 `session.user_id != current_user.id` 同判据）。"""
        session = self.get(session_id)
        if session is None or session.user_id != int(user_id):
            return None
        return session

    def purge(self) -> int:
        with self._lock:
            return self._purge_locked()

    def _purge_locked(self) -> int:
        stale = [sid for sid, s in self._items.items() if s.expired(self._ttl)]
        for sid in stale:
            self._items.pop(sid, None)
        return len(stale)

    def clear(self) -> None:
        """测试用：清空并重置 id 序列。"""
        with self._lock:
            self._items.clear()
            self._next_id = 1


#: 进程级单例（与 `scheduler` / worker 同层，不随请求创建）。
store = LoginSessionStore()
