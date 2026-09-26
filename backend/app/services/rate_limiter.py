from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock

from backend.app.core.config import get_settings


#: `wait_allow()` 的最长等待（秒）。默认额度 60 次/分钟 ⇒ 最坏 60 秒必有空位，
#: 120 给两倍余量（额度由 `settings.crawl_rate_limit_per_minute` 配，DEF-069）。
WAIT_TIMEOUT_SECONDS = 120.0

#: `wait_allow()` 的轮询间隔（秒）。测试把它置 0 以免真等待。
WAIT_POLL_SECONDS = 1.0


class CrawlRateLimiter:
    """按限流键的滑动窗口放行器（`allow` 判定 / `wait_allow` 等待式节流）。

    额度优先级（DEF-069，2026-09-25）：**构造参数 > settings > 5**。构造参数是
    调用方 / 测试的显式意图，必须优先；settings 的 `crawl_rate_limit_per_minute`
    是产品配置（默认 60）；都读不到才兜底 5。⚠️ 旧实现的兜底读的是
    `scheduler_interval_seconds`（调度周期）—— 语义混乱，改限流要改调度周期。
    """

    def __init__(self, max_per_minute: int | None = None):
        self._override = max_per_minute
        self._lock = Lock()
        self._windows: dict[str, list[float]] = defaultdict(list)

    @property
    def max_per_minute(self) -> int:
        if self._override:
            return int(self._override)
        try:
            return int(getattr(get_settings(), "crawl_rate_limit_per_minute", 0)) or 5
        except Exception:
            return 5

    def allow(self, account_id: int | str) -> bool:
        """按**限流键**放行。键可以是账号 id，或 ``"<账号>:<源>"``。

        §19 多源同步起，键要带源：平台风控是**按平台各自独立**的，www 的限流
        与千帆的限流互不相干。共用一个账号键会让先跑的源把额度耗光、后跑的源
        第 1 页就被挡（真机实测：creator 翻页后 ark 源「第 1 页前触发限流」，
        千帆 0 条 —— 那是被饿死，不是没笔记）。
        """
        key = str(account_id)
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            window = self._windows[key]
            self._windows[key] = [t for t in window if t > cutoff]
            if len(self._windows[key]) >= self.max_per_minute:
                return False
            self._windows[key].append(now)
            return True

    def reset(self, account_id: int | str) -> None:
        """清掉这个账号的**所有**限流键（裸账号键 + ``"<账号>:<源>"``）。

        键从 §19 起带源（``allow("<id>:<源>")``），只清裸账号键会**静默漏掉**
        带源的那些 —— 调用方看到的是「明明重置了却还被限流」。
        """
        target = str(account_id)
        with self._lock:
            for key in [k for k in self._windows if k == target or k.startswith(f"{target}:")]:
                self._windows.pop(key, None)

    def wait_allow(self, account_id: int | str, *, timeout: float = WAIT_TIMEOUT_SECONDS) -> bool:
        """**等到**额度可用再放行；超时返回 ``False``（调用方据此停本轮）。

        与 `allow()` 的区别是**判定 vs 节流**（设计 13 §21.3）：全量翻页时额度用完
        不能 `break`，否则「取消页数上限」只是把上限从 3 页变成 5 页（5 次/分钟 ×
        3 秒/页 ⇒ 20 秒烧完额度）。这里阻塞等到窗口腾出位置再继续。

        ⚠️ 只在**同步后台线程**里调用。`allow()` 成功才写窗口，所以轮询本身
        不消耗额度。
        """
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while True:
            if self.allow(account_id):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(WAIT_POLL_SECONDS)


_global_limiter: CrawlRateLimiter | None = None


def get_rate_limiter() -> CrawlRateLimiter:
    global _global_limiter
    if _global_limiter is None:
        _global_limiter = CrawlRateLimiter(max_per_minute=5)
    return _global_limiter
