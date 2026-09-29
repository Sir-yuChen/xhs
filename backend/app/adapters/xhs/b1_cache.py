"""跨请求复用的 b1（设备指纹）缓存 —— 省掉「每次请求都起一次 node 子进程」。

## 为什么需要它（设计 13 §18.18.7）

b1 由 `xhs_utils/*/runtime.py::generate_b1` **现算**，实现是
`subprocess.run(['node', b1.js, …])` —— 实测 node 冷启动 **480–530 ms**，而脚本执行
本身只有约 140 ms（探针实测 `b1 node 145 ms`）。每一次 HTTP 请求都要付一次。

SDK 里确实有 `_named_b1_values` 缓存，但它挂在**设备档案实例**上
（`PcDeviceProfile._named_b1_values` / `CreatorDeviceProfile._named_b1_values`），
而适配器每次调用都 `from_cookie()` **新建档案** ⇒ 那个缓存**跨请求永远命中不了**
（每个请求都拿到一个空缓存）。本模块把缓存提到**进程级**。

## 契约

* key = `(platform, sha256(cookies)[:16])` —— 同一账号、同一平台、同一条令牌
  ⇒ 同一个 b1；
* TTL = 300 s，与 DS 签名材料的 `max-age=300` 对齐：浏览器里 b1 也是
  「一次页面加载一份」，不是「一次请求一份」；
* **只在 SDK 没有显式固定值时才注入**：`profile.fixed_b1` 非空 ⇒ 尊重它，直接返回；
* 线程安全（`threading.Lock`）；探针的账号间并发会同时进来；
* **回退手段**：把 `B1_TTL_SECONDS` 置 `0` ⇒ 每次都重新生成，恢复改前行为
  （一行回退，不需要动调用点）。

⚠️ 本模块只做缓存，**不改 b1 算法**：算法唯一实现仍在 `xhs_utils/**/js/b1.js`。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

#: b1 复用窗口（秒）。`0` = 关闭缓存（回退开关）。
B1_TTL_SECONDS = 300

#: 缓存键里的平台标识。与 `account_service.CHANNEL_WWW` / `CHANNEL_CREATOR` 同值 ——
#: 这里刻意用字面量，避免适配器层为一个字符串反向依赖服务层。
PLATFORM_WWW = "www"
PLATFORM_CREATOR = "creator"

_cache: dict[tuple[str, str], tuple[float, str]] = {}
_lock = threading.Lock()


def _cache_key(platform: str, cookies: str) -> tuple[str, str]:
    digest = hashlib.sha256(str(cookies or "").encode("utf-8")).hexdigest()[:16]
    return (str(platform), digest)


def cached_b1(platform: str, cookies: str, profile: Any) -> str:
    """拿到该账号该平台的 b1；**命中缓存时不起 node**。

    未命中 ⇒ 让 `profile.current_b1()` 生成一次（这一份给本次请求用），并写进缓存；
    命中 ⇒ 直接把缓存值返回，调用方应把它注入 `profile.fixed_b1`，于是本次请求
    也不会再生成。

    任何异常都**不吞**：b1 取不到时调用方需要看到真实失败（与 SDK 原行为一致）。
    """
    fixed = str(getattr(profile, "fixed_b1", "") or "")
    if fixed:
        return fixed
    if B1_TTL_SECONDS <= 0:
        return profile.current_b1()

    key = _cache_key(platform, cookies)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < B1_TTL_SECONDS:
        return hit[1]

    value = profile.current_b1()
    if value:
        with _lock:
            _cache[key] = (now, value)
        logger.debug("b1 cached for platform=%s (ttl=%ss)", platform, B1_TTL_SECONDS)
    return value


def clear() -> None:
    """清空缓存（测试与令牌变更后调用）。"""
    with _lock:
        _cache.clear()
