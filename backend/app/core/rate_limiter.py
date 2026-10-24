from __future__ import annotations

#: ⚠️ 本模块自 v1.1.0（2026-10-24，设计 61 A7）起位于 **core**：限流是平台调用的
#: 基础设施横切件，adapters / services / api 三层都要用，依赖方向只能向下。
#: 原 `services/rate_limiter.py` 已删除（DEF-044：同一语义不留两份实现）。

import time
from collections import defaultdict
from collections.abc import Callable
from threading import Lock

from backend.app.core.config import get_settings


#: `wait_allow()` 的最长等待（秒）。滑动窗口是 60 秒 ⇒ **任何额度下最坏 60 秒必有空位**
#: （额度 ≥ 1 时；额度为 0 会被 `_KEY_BOUNDS` 夹到 1），120 给两倍余量。
#: 额度来源见 :func:`_settings_quota_fallback` 与 `settings_service`（2026-09-27 起
#: 优先读运行时配置，不再被写死的构造参数压住）。
WAIT_TIMEOUT_SECONDS = 120.0

#: `wait_allow()` 的轮询间隔（秒）。测试把它置 0 以免真等待。
WAIT_POLL_SECONDS = 1.0


#: 第 2 层额度（运行时配置）的**注入点**（依赖反转，设计 61 §三 A7）。
#:
#: core 不许 import services（依赖方向合同 C1），而「按账号归属用户读运行时配置」
#: 的真相源在 `settings_service` —— 由它在自己模块加载时把读取函数注册进来
#: （见 settings_service 末尾的接线），契约 = `provider(key, *, fallback) -> int`。
#: 未注册时（极早启动 / 独立脚本）退到
#: settings YAML 的额度值，行为与「没有配置行」一致。
QUOTA_PROVIDER: "Callable[..., int] | None" = None  # (key, *, fallback) -> int


#: 第 3/4 层额度（settings → 内置兜底 5）的唯一实现。
#:
#: ⚠️ 兜底值仍是 **5**（保守值，与改造前一致）：它只在「既没有运行时配置行、也没有
#: settings 字段」时生效 —— 那种情况下用最保守的数字，比用一个没测过的大数字安全。
def _settings_quota_fallback() -> int:
    try:
        return int(getattr(get_settings(), "crawl_rate_limit_per_minute", 0)) or 5
    except Exception:  # noqa: BLE001 - 配置读不到就用最保守值
        return 5


class CrawlRateLimiter:
    """按限流键的滑动窗口放行器（`allow` 判定 / `wait_allow` 等待式节流）。

    额度优先级（2026-09-27 设计 13 §38.2 修订）：

    1. **构造参数**（调用方 / 测试的显式意图，最高）；
    2. **运行时配置**（`system_settings` 行，页面可改、**立即生效**，设计 10 §7）；
    3. **settings**（`config/default.yaml` → `Settings.crawl_rate_limit_per_minute`，默认 8）；
    4. 内置兜底 **5**。

    ⚠️ 第 2 层是本次新增的，也是**修掉一个真实缺陷**的那一层：改造前
    `get_rate_limiter()` 把 5 写死在构造参数里（第 1 层），于是
    `config/default.yaml` 声明的 60 **永远读不到** —— 「www 详情 5 次/分钟」不是平台额度，
    而是这个写死值，1867 篇欠账因此要跑 6.2 小时（设计 13 §38.1 F1/F2）。
    ⚠️ 旧实现的兜底读的是 `scheduler_interval_seconds`（调度周期）—— 语义混乱，
    改限流要改调度周期；该实现早已废弃，此处只留记录。
    """

    def __init__(self, max_per_minute: int | None = None):
        self._override = max_per_minute
        self._lock = Lock()
        self._windows: dict[str, list[float]] = defaultdict(list)

    @property
    def max_per_minute(self) -> int:
        """**不区分账号**的额度（显式构造参数 > settings > 5）。

        ⚠️ 真实放行判据是 :meth:`quota_for`（按限流键里的账号找归属用户，再读运行时配置）；
        本属性保留给「只想问一句『大概是几』」的调用方与既有测试，
        **不要**拿它去判某个账号的额度。
        """
        if self._override:
            return int(self._override)
        return _settings_quota_fallback()

    def quota_for(self, account_id: int | str) -> int:
        """该限流键的**实际**额度：构造参数 > 运行时配置（按账号归属用户）> settings > 5。

        ⚠️ 每次 `allow()` 都算一次（内部有进程内缓存，不是每次查库）。这是「页面改完
        立即生效」的落点 —— 缓存不命中就现读，写路径会主动失效缓存（设计 10 §7.4）。
        """
        if self._override:
            return int(self._override)
        if QUOTA_PROVIDER is not None:
            try:
                #: ⚠️ `fallback` 必须走**关键字**实参 —— 真相源函数
                #: （`settings_service.rate_limit_per_minute_for_key`）的签名是
                #: `(rate_key, *, fallback)`，按位置传会 TypeError 且被下方
                #: except 吞成兜底值（实测踩坑：表现为「运行时配置永远读不到」）。
                return int(QUOTA_PROVIDER(str(account_id), fallback=_settings_quota_fallback()))
            except Exception:  # noqa: BLE001 - 读配置失败退 settings 值，绝不打断平台请求
                return _settings_quota_fallback()
        return _settings_quota_fallback()

    def allow(self, account_id: int | str) -> bool:
        """按**限流键**放行。键可以是账号 id，或 ``"<账号>:<源>"``。

        §19 多源同步起，键要带源：平台风控是**按平台各自独立**的，www 的限流
        与千帆的限流互不相干。共用一个账号键会让先跑的源把额度耗光、后跑的源
        第 1 页就被挡（真机实测：creator 翻页后 ark 源「第 1 页前触发限流」，
        千帆 0 条 —— 那是被饿死，不是没笔记）。
        """
        key = str(account_id)
        # 额度按**这个键的账号**现读（进程内缓存；写配置会主动失效）⇒ 设置页改完
        # 下一次平台请求就用新值（设计 10 §7.4）。
        quota = self.quota_for(key)
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            window = self._windows[key]
            self._windows[key] = [t for t in window if t > cutoff]
            if len(self._windows[key]) >= quota:
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
    """进程级单例（**不传构造参数** ⇒ 走运行时配置）。

    ⚠️ 这里曾经是 `CrawlRateLimiter(max_per_minute=5)` —— 那个 `5` 是本次修掉的缺陷：
    构造参数优先级最高，于是它把 `config/default.yaml` 的 60 **永久压住**，
    「www 详情 5 次/分钟」被当成平台额度写进了多份文档（设计 13 §38.1）。
    **新增调用方一律用本函数**，不要自己 `CrawlRateLimiter(数字)` ——
    那等于又开一条绕过配置的暗路。
    """
    global _global_limiter
    if _global_limiter is None:
        _global_limiter = CrawlRateLimiter()
    return _global_limiter
