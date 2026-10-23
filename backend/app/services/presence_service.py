"""在线状态追踪（设计 55 §2.1「online 字段的正确实现」方案 A）。

## 为什么不用 `login_sessions`

`login_sessions` 是**小红书账号**的登录会话表（`platform` / `sub_type` / `qr_id` /
`encrypted_temp_cookies`）—— 记录的是「绑定小红书账号时扫的那个码」，
**不是系统用户的登录态**。设计初稿按它判 `online`，实测读模型后推翻。

## 采用方案 A：进程内 `{user_id: last_seen_at}`

* 每次带令牌的请求更新一次 `last_seen_at`；
* `online` = `last_seen_at` 距今 ≤ `ONLINE_WINDOW_SECONDS`。

### 如实标注这是**近似值**

判据是「最近有没有请求」，不是「浏览器窗口是否还开着」：

* 一个人把页面晾着不动 ⇒ 5 分钟后显示离线（**这不是缺陷，是判据的边界**）；
* 多副本部署时各副本各记一份 ⇒ 只能看到本副本的活动（本项目只支持单副本，
  见设计 52 §4；扩副本时必须换成共享存储，此注释是那件事的锚点）。

前端 tooltip 必须写明「5 分钟内是否有活动」，不要让用户以为这是实时在线状态。

## 为什么不做成中间件

中间件拿不到 `get_current_user` 已经解析好的用户（它还要重解一遍 token）。
放在 `get_current_user` 里更新一次即可 —— 那是**所有**已登录请求的必经之路，
且只有一个写入点（DEF-044：同一件事不留两份实现）。
"""

from __future__ import annotations

import threading
import time
from typing import Final

__all__ = ["touch", "is_online", "online_user_ids", "forget", "clear", "ONLINE_WINDOW_SECONDS"]

#: 多久没请求就算离线（秒）。取 300s：与「用户切走一个页面再回来」的间隔相当。
ONLINE_WINDOW_SECONDS: Final = 300

_LOCK = threading.Lock()
_LAST_SEEN: dict[int, float] = {}


def touch(user_id: int | None) -> None:
    """记一次活动（`get_current_user` 每次成功鉴权后调用）。"""
    if not user_id:
        return
    now = time.monotonic()
    with _LOCK:
        _LAST_SEEN[int(user_id)] = now


def is_online(user_id: int | None) -> bool:
    """该用户是否在 `ONLINE_WINDOW_SECONDS` 内有过请求。"""
    if not user_id:
        return False
    cutoff = time.monotonic() - ONLINE_WINDOW_SECONDS
    with _LOCK:
        last = _LAST_SEEN.get(int(user_id))
    return last is not None and last >= cutoff


def online_user_ids() -> set[int]:
    """当前在线的用户 id 集合（列表批量判定用，避免逐行加锁）。"""
    cutoff = time.monotonic() - ONLINE_WINDOW_SECONDS
    with _LOCK:
        return {uid for uid, ts in _LAST_SEEN.items() if ts >= cutoff}


def forget(user_id: int | None) -> None:
    """移除该用户的活动记录（强制下线 / 删除用户时调用）。"""
    if not user_id:
        return
    with _LOCK:
        _LAST_SEEN.pop(int(user_id), None)


def clear() -> None:
    """全清（测试 / 进程重启）。"""
    with _LOCK:
        _LAST_SEEN.clear()
