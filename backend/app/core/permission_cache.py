"""权限缓存 —— 热路径的「用户 → 权限码集合」一层薄缓存（设计 50 §3.3 / 57 S2）。

## 为什么需要它

每条写请求都要过一次 `require_perm`。不缓存时，一次「保存 10 个商品」会查
10 次 `role_menus ⋈ menus`；缓存后是 0 次（命中）。

## 缓存键必须含 `token_version`

键 = `(user_id, role, token_version)`，**不是** `user_id`。

理由：`token_version` 是本项目「踢人 / 改密 / 改授权」的撤销信号
（`users.token_version`，DEF-022）。授权变更时后端会 `token_version += 1`
让旧 JWT 失效 —— 如果缓存键不带它，那么**新 JWT 会命中旧缓存**，
用户看到的是「授权改了但没生效」，且要等缓存 TTL 到期才恢复。
带上它，版本一变键就变，旧条目自然失效（并靠 LRU 淘汰掉）。

## 不做 TTL 定时任务

用 `functools.lru_cache` 式的容量上限即可。理由：失效是**主动**的
（写路径调 `clear()`），TTL 只是「万一漏了一次主动失效」的兜底，
给它配一个定时清理线程不值当（还要处理多副本，见设计 52 §4）。

## 多副本注意

进程内缓存 ⇒ 多副本时 A 副本改了权限、B 副本最多 `TTL_SECONDS` 内不知道。
本项目**当前只支持单副本**（设计 52 §4：调度靠文件租约，本身就不许扩副本），
所以这不是缺陷。**将来扩副本必须换成共享缓存**（Redis），此注释是那件事的锚点。
"""

from __future__ import annotations

import threading
import time
from typing import Final

from sqlalchemy.orm import Session

from backend.app.core.perms import ALL_PERMS, ROLE_ADMIN, normalize_role
from backend.app.models.user import User

__all__ = ["perms_of", "clear", "stats", "TTL_SECONDS", "MAX_ENTRIES"]

#: 兜底有效期（秒）。主动失效是主路径，这个只在**漏了失效**时救场。
#: 取 300s：足够长到命中率高，足够短到「漏失效」不会让用户等一整天。
TTL_SECONDS: Final = 300

#: 容量上限（用户数通常 << 这个数；超了按插入序淘汰最早的 1/4）。
MAX_ENTRIES: Final = 4096

_LOCK = threading.Lock()
#: key = (user_id, role, token_version) -> (expire_at, frozenset[str])
_STORE: dict[tuple, tuple[float, frozenset[str]]] = {}
_HITS = 0
_MISSES = 0


def _key(user: User) -> tuple:
    return (
        int(getattr(user, "id", 0) or 0),
        normalize_role(getattr(user, "role", None)),
        int(getattr(user, "token_version", 0) or 0),
    )


def perms_of(db: Session, user: User | None) -> frozenset[str]:
    """取用户权限码（带缓存）。`None` ⇒ 空集，不缓存。"""
    if user is None:
        return frozenset()

    role = normalize_role(getattr(user, "role", None))
    # admin 的权限集是常量，不值得进缓存（也不该被 clear 影响）
    if role == ROLE_ADMIN:
        return ALL_PERMS

    global _HITS, _MISSES
    key = _key(user)
    now = time.monotonic()

    with _LOCK:
        entry = _STORE.get(key)
        if entry is not None and entry[0] > now:
            _HITS += 1
            return entry[1]

    # 未命中：在锁外查库（查库可能慢，不能占着锁）
    from backend.app.core import rbac_service

    value = rbac_service.perms_for_user(db, user)

    with _LOCK:
        _MISSES += 1
        if len(_STORE) >= MAX_ENTRIES:
            # 淘汰最早过期的 1/4（够用，且不需要维护额外的 LRU 链表）
            victims = sorted(_STORE.items(), key=lambda kv: kv[1][0])[: MAX_ENTRIES // 4]
            for vkey, _ in victims:
                _STORE.pop(vkey, None)
        _STORE[key] = (now + TTL_SECONDS, value)
    return value


def clear(user_id: int | None = None, *, role: str | None = None) -> int:
    """清缓存。返回清掉的条目数。

    * 无参 ⇒ 全清（菜单启停、角色授权这种**低频全站**操作）；
    * `user_id` ⇒ 只清该用户（改密、单人改授权）；
    * `role` ⇒ 只清该角色的所有用户（角色 code 改了 —— 实际禁改，防御性保留）。

    ⚠️ **故意不支持「只清某用户某角色」**：键里还有 `token_version`，
    精确到那份粒度需要调用方知道版本号，容易写错反而漏清。
    """
    if user_id is None and role is None:
        with _LOCK:
            n = len(_STORE)
            _STORE.clear()
        return n

    normalized = normalize_role(role) if role else None
    with _LOCK:
        victims = [
            k
            for k in _STORE
            if (user_id is not None and k[0] == int(user_id))
            or (normalized is not None and k[1] == normalized)
        ]
        for k in victims:
            _STORE.pop(k, None)
    return len(victims)


def stats() -> dict[str, int]:
    """缓存状态（诊断端点 / 测试用）。"""
    with _LOCK:
        return {"entries": len(_STORE), "hits": _HITS, "misses": _MISSES}
