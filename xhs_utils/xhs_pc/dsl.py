# encoding: utf-8
"""
小红书 ds 接口（sec sdk 时间戳锚点）拉取器。

背景
====
XHS PC web 签名的 X-S-Common.x12 字段结构为 "<dsllt>;<_dsl>"：
  - dsllt = 本地 Date.now()（页面加载覆盖 localStorage.dsllt）
  - _dsl  = 服务端下发的伪时间戳（`window._dsl`）

_dsl 来源逆向确认
================
访问 https://as.xiaohongshu.com/api/sec/v1/ds?appId=xhs-pc-web，
接口返回 JS（obfuscator.io 混淆的 Sabo VMP），前几行明文：

    function getdss() { return '<13位时间戳>'; };
    var _0x341b=[...混淆字符串表...];
    var __$c='<VMP 字节码 hex>';
    glb['_BHjFmfUMEtxhI'](__$c, [,,undefined,Uint8Array,getdss])

VMP 内部把 `getdss()` 返回值直接写入 `window._dsl`。
在 Node V8 sandbox 里跑该 JS，能拿到：
  - window._dsl = "1783929625569"  ← 就是 getdss() 值
  - window._dsn = "a3"              ← 静态字符串
  - window._dsf = <function>        ← 某个签名相关闭包
  - window.__bc = "<字节码 hex>"    ← 内部复制

因此，_dsl 不属于本地算法参数：直接从 ds 接口提取 getdss() 值。

服务端行为
==========
  - cache-control: max-age=300 (5 分钟)
  - 不同 CDN 节点返回不同 getdss() 值（有 3 天前、3 周前等）
  - 服务端并非返回 Date.now()，而是某个"锚点时间戳"

策略
====
  - 首次调 API 前 fetch 一次，缓存到内存
  - 5 分钟内复用（浏览器一致行为）
  - 过期后重新 fetch
  - 失败时用上次值继续（不阻塞签名）
"""
from __future__ import annotations

import logging
import re
import time
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_GETDSS_RE = re.compile(r"function\s+getdss\s*\(\s*\)\s*\{\s*return\s+'(\d+)'")
_DS_URL = "https://as.xiaohongshu.com/api/sec/v1/ds?appId=xhs-pc-web"
_TTL = 300  # 与服务端 max-age 对齐

#: 旧值**最长可容忍**的过期时间（秒，设计 13 §18.18.9）。超过它就不再「用旧值继续」，
#: 回到阻塞刷新 —— 避免「后台刷新一直失败 + 一直用几小时前的值」这种静默退化。
_MAX_STALE = 1800

#: 后台刷新线程名。⚠️ 必须避开 `note-batch-sync-worker` 前缀：同步 worker 的看门狗
#: 按线程名前缀匹配并会「补齐缺口」，名字撞上会被当成失联 worker 而重建重复线程。
_REFRESH_THREAD_NAME = "xhs-ds-refresh"

#: 单次 fetch 的超时（秒）。原为 8 —— 实测 CDN 抖动时「8 秒只收到 1152 字节后断开」
#: （curl 28），一次失败就把整篇同步判死。放宽到 12 秒。
_FETCH_TIMEOUT = 12
#: 总尝试次数（首次 + 重试）。DS 值是**签名材料**，取不到就没法发任何业务请求，
#: 所以值得重试；它自己也有 5 分钟缓存，重试只发生在「首次」或「过期刷新」时。
_FETCH_ATTEMPTS = 3
#: 重试退避基数（秒）：第 n 次重试前等 n × 基数（0.6s / 1.2s）。
_FETCH_RETRY_BACKOFF = 0.6


class DsFetcher:
    """
    ds 接口 getdss() 值拉取 + 5 分钟内存缓存。

    - 线程安全（单进程内多线程共享 1 份缓存）
    - 缓存策略：命中即返回，未命中或过期则同步 fetch
    - 失败降级：fetch 失败时若有旧值则复用旧值 + 记 warn

    典型用法：
        dsl = get_dsl()
        # dsl_pair = str(int(time.time()*1000)) + ';' + dsl
    """

    def __init__(self, ttl: int = _TTL, url: str = _DS_URL):
        self._ttl = ttl
        self._url = url
        self._value: Optional[str] = None
        self._fetched_at: float = 0.0
        self._lock = threading.Lock()
        #: 后台刷新去重（同一时刻只允许一轮）：过期窗口内并发进来的请求不该
        #: 每个都起一个刷新线程（设计 13 §18.18.9）。
        self._refresh_lock = threading.Lock()
        self._refreshing = False

    def get(
        self,
        proxies: Optional[dict] = None,
        force: bool = False,
        http_client=None,
    ) -> str:
        """
        拿 getdss() 值（=_dsl）。缓存命中直接返回，否则见下。

        ## 过期时**不再阻塞**（设计 13 §18.18.9，2026-09-28）

        有旧值、且过期未超 `_MAX_STALE` ⇒ **立刻返回旧值**，刷新交给后台守护线程。
        两层理由：

        * 本模块的设计本来就允许用旧值继续（见文件头「失败时用上次值继续
          （不阻塞签名）」）—— 「过期」只是**刷新时机到了**，不是「旧值不可用」；
        * 阻塞刷新的最坏情况是 `12s × 3 次尝试 + 退避 ≈ 38s`，而它**持着锁**，
          于是并发的令牌探针 / 同步翻页一起卡住 —— 那正是「偶发卡很久」的来源
          （实测见设计 13 §18.18.1）。

        旧值过期超过 `_MAX_STALE`（30 分钟）⇒ 不再用它，回到阻塞刷新。

        Args:
            proxies: 可选，透传给 HTTP 客户端
            force:   强制刷新（跳过缓存，走阻塞路径）
            http_client: 可选，复用 Auth 的浏览器传输 Session

        Returns:
            13 位数字字符串
        """
        now = time.time()
        if not force and self._value and now - self._fetched_at < self._ttl:
            return self._value

        if not force and self._value and now - self._fetched_at < _MAX_STALE:
            self._refresh_in_background(proxies=proxies, http_client=http_client)
            return self._value

        return self._fetch_blocking(
            proxies=proxies, http_client=http_client, force=force
        )

    def _fetch_blocking(
        self,
        *,
        proxies: Optional[dict] = None,
        http_client=None,
        force: bool = False,
    ) -> str:
        """阻塞式取新值（首次 / 超过 `_MAX_STALE` / `force=True` 走这里）。"""
        with self._lock:
            # 双重检查（多线程下另一线程可能刚 fetch 完）
            now = time.time()
            if not force and self._value and now - self._fetched_at < self._ttl:
                return self._value
            last_error: Optional[Exception] = None
            for attempt in range(1, _FETCH_ATTEMPTS + 1):
                try:
                    new_val = self._fetch(
                        proxies=proxies,
                        http_client=http_client,
                    )
                except Exception as e:  # noqa: BLE001 - 单次失败进入重试
                    last_error = e
                    if attempt < _FETCH_ATTEMPTS:
                        # 退避后重试（2026-09-25）：CDN 抖动是常态，一次超时不
                        # 该等于「本次同步失败」—— 重试成本只在首次/过期刷新。
                        logger.warning(
                            "dsl fetch failed (attempt %s/%s): %s",
                            attempt,
                            _FETCH_ATTEMPTS,
                            e,
                        )
                        time.sleep(_FETCH_RETRY_BACKOFF * attempt)
                        continue
                else:
                    self._value = new_val
                    self._fetched_at = now
                    return new_val
            if self._value:
                # 失败降级：返回旧值
                return self._value
            raise RuntimeError(f'ds 接口首次 fetch 失败：{last_error}') from last_error

    def _refresh_in_background(
        self,
        *,
        proxies: Optional[dict] = None,
        http_client=None,
    ) -> None:
        """后台刷新一次（daemon 线程；同一时刻只允许一轮，失败只记日志）。

        ⚠️ 失败**不影响业务**：调用方拿到的仍是旧值，与原「失败降级」同一取向。
        """
        with self._refresh_lock:
            if self._refreshing:
                return
            self._refreshing = True

        def worker() -> None:
            try:
                self._fetch_blocking(
                    proxies=proxies, http_client=http_client, force=True
                )
            except Exception as exc:  # noqa: BLE001 - 后台刷新失败不算业务失败
                logger.warning("ds 后台刷新失败，继续用旧值：%s", exc)
            finally:
                with self._refresh_lock:
                    self._refreshing = False

        threading.Thread(
            target=worker,
            name=_REFRESH_THREAD_NAME,
            daemon=True,
        ).start()

    def _fetch(
        self,
        proxies: Optional[dict] = None,
        http_client=None,
    ) -> str:
        """
        实际发请求 + 正则提 getdss() 值。
        """
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/150.0.0.0 Safari/537.36',
            'Referer': 'https://www.xiaohongshu.com/',
            'Accept': '*/*',
        }
        if http_client is None:
            from .http import PcHttpClient

            client = PcHttpClient(proxies=proxies)
            try:
                r = client.get(
                    self._url,
                    headers=headers,
                    proxies=proxies,
                    timeout=_FETCH_TIMEOUT,
                )
            finally:
                client.close()
        else:
            r = http_client.get(
                self._url,
                headers=headers,
                proxies=proxies,
                timeout=8,
            )
        r.raise_for_status()
        m = _GETDSS_RE.search(r.text)
        if not m:
            raise ValueError('ds 响应未匹配 getdss() 模式')
        return m.group(1)

    def peek(self) -> Optional[str]:
        """不发请求，返回当前缓存值（可能为 None）。"""
        return self._value


# 模块级单例（与浏览器 window._dsl 语义对齐：一次页面一份）
_default = DsFetcher()


def get_dsl(
    proxies: Optional[dict] = None,
    force: bool = False,
    http_client=None,
) -> str:
    """
    模块级快捷接口，多数场景直接用这个。

    示例：
        from xhs_utils.xhs_pc.dsl import get_dsl
        _dsl = get_dsl()
    """
    return _default.get(
        proxies=proxies,
        force=force,
        http_client=http_client,
    )


# _dsn 是静态字符串，不需要拉接口（VMP 直接写入 "a3"）。
# 未来若 XHS 改版可能变，暂 hardcode。
DSN_STATIC = 'a3'
