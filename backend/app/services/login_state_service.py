"""账号维度的**登录态**（设计 13 §18.5；2026-09-21 拆分为三平台）。

## 为什么要有这一层

账号卡片此前只有 `status`（业务健康）与 `metrics_reason`（互动计数），**没有任何令牌
维度的展示** —— 于是「千帆登录态过期了」这件事只在用户点「同步商品」那一刻才暴露，
页面在那之前看起来一切正常（同 T-31 当年的静默失败）。

## 三个平台、三条令牌（2026-09-21 拆分，用户拍板）

| 平台 | 通道 | 判据（L2） | 探针（L3，只读） |
|---|---|---|---|
| 网页版 | `www` | `web_session` + `id_token` | `GET /v2/user/me` |
| 创作者平台 | `creator` | creator 域令牌 / customer 单点登录 | `GET /api/galaxy/user/info` |
| 千帆 | `ark` | ark 域令牌 / customer 单点登录 | `GET /api/edith/seller/info/v2` |

⚠️ **千帆与创作者不再是同一份令牌**（真机实测，观隅好物 acct 8）：创作者扫码的
jar 只带 `access-token-creator.*`，打千帆一律 `-13000 缺失登录凭证`；千帆那份是
**超集**（同时带两个平台的域令牌），打创作者平台正常。所以：

* 探针**按平台各问各的**（创作者问创作者平台，千帆问千帆）；
* 千帆的探针与取票只认**账号自己的 `ark` 通道**
  （`shop_binding_service.qianfan_cookies_for_account`）。2026-09-22 用户拍板
  「每个账号只使用自己账号对应的平台有效登录令牌」后，原来的
  `shop_account_bindings` 兜底整条移除 —— 所以「千帆 未导入」是**实话**：
  这个账号确实没登录过千帆，不是「令牌藏在别处」。

## 三层判据，**三层都要给**（不能只给一个绿灯）

| 层 | 判据 | 请求数 | 回答的问题 | 状态值 |
|---|---|---|---|---|
| **L1 存没存** | 该账号有没有这个平台的信封 | 0 | 「这个平台从没登录过」 | `missing` |
| **L2 是不是登录态** | www：`web_session`+`id_token`；creator/ark：平台域或单点登录键 | 0 | 「只拿到了游客态 / 空壳」 | `guest` |
| **L3 还能不能用** | 只读探针（见上表） | 1 | 「令牌过期了」 | `valid` / `expired` |

三层问的是**三个不同的问题**，压成一个布尔值必然说谎：
「存了但没探过」既不是「可用」也不是「不可用」。

## ⚠️ `unknown` 绝不能渲染成「已失效」

`unknown` = 「**未确认**」：存了、也是登录态，但**还没探过**，或者**探活本身没得到结论**
（网络异常 / 平台结构变了）。它是 DEF-057 的同型红线 —— 用「没检查」冒充「不可用」，
用户会去做一次**根本不需要的重登**。所以：

* 探针发生在**进入 / 刷新账号页时**（批量，`probe=True`，v1.30 起由用户要求；
  仍是「一次页面动作一次探针」，**不进轮询、不进定时任务**）。v1.31 起账号卡片的
  「检查」按钮已移除（用户口径），探活不再有第二个手工入口 —— 真探不出结论时
  刷新页面即重试；
* **账号间并发、账号内串行**（§18.18.3，2026-09-28）：并行度复用
  `xhs_sync_account_parallelism`（默认 3，夹 1–8）。请求数不变（每账号 ≤3 次只读），
  只是不再一个账号一个账号地等 —— 改前 4 账号 4.29 s，改后见 §18.18.5；
* `成功=False` 且 **`payload is None`**（SDK 在传输异常时不抛异常、只把 `success` 置假）
  ⇒ 判 `unknown`，**不判 `expired`**。判据是**结构**（有没有拿到平台的 JSON），
  不是去匹配错误文案；
* 每一次结果都带上 `probed` 与 `reason`，让界面能说「上次检测未得到结论：<原因>」，
  而不是替平台下一个它没下过的结论。

## ⚠️ 探不出结论 ≠ 一直探（v1.32，2026-09-30）

「不许把 `unknown` 说成 `expired`」**不等于**「可以无限停在 `unknown`」。真机反馈：
千帆令牌卡片长期停在「未检测」，用户既不知道能不能用、也没有重登入口 —— 因为探测
**一次就放弃**，而千帆接口偶发慢（共享会话首次 TLS 握手 + 平台侧抖动）会稳定命中。

所以探针加**有界重试**：每平台最多 `PROBE_MAX_ATTEMPTS`(=3) 次，每次 `PROBE_ATTEMPT_TIMEOUT`
(=10s)；**只有拿到了平台的 JSON 答复**（哪怕是被拒绝）才提前结束重试。三次都拿不到
JSON ⇒ 判 `expired`（重登入口由前端状态映射自动出现），reason 明写「连续 N 次未得到结论」，
**不冒充**平台的拒绝原话。

⚠️ 这把尺子**只加在千帆（ark）上**：www 探针走 `apis/xhs_pc_apis`（每账号 jar 不同，
重试会翻倍请求面），creator 同理；千帆是**用户长期抱怨的那一个**，且它的重试面最小
（同 jar、同 host、同共享会话）。要推广到另两个平台时，把 `probe_max_attempts` /
`probe_attempt_timeout` 参数一并传下去即可，函数签名已预留。

* 请求数上限因此从「每账号 ≤3 次」变为「www ≤1 + creator ≤1 + ark ≤3」；
  **仍不进轮询、不进定时任务**，仍是一次页面动作一次探针。

## 只读边界

* 探针**只读**（三个 GET），**不落库**：L3 的结论只影响**这一次响应**里的展示 ——
  它**不得**删除 / 覆盖任何存量令牌，也**不得**动千帆侧状态（`shop_state`）（§18.9.3）；
  （原句为「也不得动店铺关系（`shop_id_fk` / `shop_state`）」—— `shop_id_fk` 已随
  ADR-021 退场；更早的原句「也不得清空 `shop_account_bindings`」—— 该表已于
  2026-09-22 整表移除。）
* **不进轮询、不进定时任务**（§18.9.5 风控）：一张卡片最多三次探活（www + creator + ark），
  且仅限页面动作触发。
"""
from __future__ import annotations

import logging
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from concurrent.futures.thread import _threads_queues, _worker
from typing import Any, Callable, Sequence

from sqlalchemy.orm import Session

from backend.app.core.time import shanghai_now
from backend.app.models import PlatformAccount
from backend.app.services.account_service import (
    CHANNEL_ARK,
    CHANNEL_CREATOR,
    CHANNEL_WWW,
    CHANNELS,
    has_ark_token,
    has_creator_token,
    has_web_session,
    latest_cookies_for_channels,
)

logger = logging.getLogger(__name__)

#: 状态取值（**互斥且完整**，§18.5）。
STATE_MISSING = "missing"
STATE_GUEST = "guest"
STATE_VALID = "valid"
STATE_EXPIRED = "expired"
STATE_UNKNOWN = "unknown"

#: 探针线程名前缀（§18.18.3）。**必须独立于同步 worker 的前缀** ——
#: `note_sync_service` 的看门狗按线程名前缀匹配、会去「补齐缺口」，两者混用
#: 会让看门狗把探针线程当成失联的同步 worker 而重建出重复线程。
PROBE_THREAD_PREFIX = "xhs-login-probe"

#: 单次探针的**超时兜底线程**前缀（v1.32）。同样必须独立 —— 它是短命线程，
#: 混进 `PROBE_THREAD_PREFIX` 会让「探针在飞」的统计把已超时的线程也算进去。
PROBE_TIMEOUT_THREAD_PREFIX = "xhs-login-probe-timeout"

#: 并行度读不到时的回落值（与 `note_sync_service.SYNC_DEFAULT_PARALLELISM` 同值）。
PROBE_DEFAULT_PARALLELISM = 3
#: 并行度上限（与设置页 `xhs_sync_account_parallelism` 的范围上界一致）。
PROBE_MAX_PARALLELISM = 8

#: 单平台探针的**最大尝试次数**（v1.32）与**单次超时**（秒）。
#:
#: ⚠️ 这两个数是**有意的保守值**：`3 × 10s = 30s` 是最坏墙钟（三次全超时），
#: 而账号之间并行（默认 3 并发）⇒ 一批 4 账号最坏约 40s。再往上调会让「刷新
#: 账号页」变成等半分钟，往下调则穿透不了千帆的真实抖动区间（实测首次请求
#: 1.5–8s）。
PROBE_MAX_ATTEMPTS = 3
PROBE_ATTEMPT_TIMEOUT = 10
#: 重试之间的停顿（秒）。**必须 > 0**：瞬时抖动立刻重试会打在同一个坏窗口上，
#: 三连失败退化成一次失败。0.6s 足够错过一次拥塞窗口，又不拖长最坏墙钟。
PROBE_RETRY_BACKOFF = 0.6


def _probe_parallelism() -> int:
    """账号间探针并行度 —— **复用同步的那把尺子**（§18.18.3）。

    「账号级并行度」只允许有**一个真相源**：`xhs_sync_account_parallelism`
    （设置页可改，默认 3，夹 1–8）。探针自己再开一个键 = 两把尺子，必然漂移。

    任何异常都退默认值：并行度读不到不该让令牌检测停摆。
    """
    try:
        from backend.app.services.note_sync_service import sync_parallelism

        return max(1, min(int(sync_parallelism()), PROBE_MAX_PARALLELISM))
    except Exception:  # noqa: BLE001 - 配置读不到就用默认并行度
        logger.warning(
            "login-state: 读并行度失败，本次用默认 %s",
            PROBE_DEFAULT_PARALLELISM,
            exc_info=True,
        )
        return PROBE_DEFAULT_PARALLELISM


#: 每个平台的展示名。**千帆与创作者分开**（2026-09-21 拆分）—— 合成一个名字
#: 会让用户以为一份令牌能同时打两边，正是本轮要消灭的误解。
#: ⚠️ 全名必须**严格长于**前端 tag 里的短名（`LOGIN_CHANNEL_SHORT_LABELS`）——
#: 相等就等于「tag 里塞的是全名」，昵称会被挤出卡片（`login_state.cjs` F2 钉住）。
CHANNEL_LABELS: dict[str, str] = {
    CHANNEL_WWW: "网页版（PC）",
    CHANNEL_CREATOR: "创作者平台",
    CHANNEL_ARK: "千帆商家后台",
}

#: 各状态的原因（**工具提示用全文**；短句由前端按状态映射，后端不重复给）。
_REASONS: dict[tuple[str, str], str] = {
    (CHANNEL_WWW, STATE_MISSING): (
        "该账号还没有网页版（www）令牌 —— 从未登录过，我赞过的 / 我收藏的 / 互动消息、"
        "笔记正文与评论都读不到。"
    ),
    (CHANNEL_WWW, STATE_GUEST): (
        "这条网页版令牌只拿到了游客态（有 web_session 但没有 id_token），读不到本人数据，"
        "需要重新登录网页版。"
    ),
    (CHANNEL_CREATOR, STATE_MISSING): (
        "该账号还没有创作者平台令牌 —— 从未在创作者平台登录过，"
        "创作者平台的笔记管理与发布用不了。"
    ),
    (CHANNEL_CREATOR, STATE_GUEST): (
        "这条令牌不含创作者平台的登录凭证（access-token-creator 等），"
        "需要重新登录创作者平台。"
    ),
    (CHANNEL_ARK, STATE_MISSING): (
        "该账号还没有千帆（ark）令牌 —— 商品列表、店铺商品同步、商品笔记反查都用不了。"
    ),
    (CHANNEL_ARK, STATE_GUEST): (
        "这条令牌不含千帆的登录凭证（access-token-ark 等），"
        "无法访问千帆商家后台，需要重新登录千帆。"
    ),
    (CHANNEL_WWW, STATE_VALID): "探活通过：这条网页版令牌现在可用。",
    (CHANNEL_CREATOR, STATE_VALID): "探活通过：这条创作者平台令牌现在可用。",
    (CHANNEL_ARK, STATE_VALID): "探活通过：千帆现在可用。",
    (CHANNEL_WWW, STATE_EXPIRED): (
        "令牌已失效，请在网页版（xiaohongshu.com）重新登录后扫码续期。"
    ),
    (CHANNEL_CREATOR, STATE_EXPIRED): (
        "令牌已失效，请在创作者平台（creator.xiaohongshu.com）重新登录后扫码续期。"
    ),
    (CHANNEL_ARK, STATE_EXPIRED): (
        "千帆登录态已失效，请在千帆商家后台（ark.xiaohongshu.com）重新登录后续期。"
    ),
    (CHANNEL_WWW, STATE_UNKNOWN): "已确认是登录态，但还没检测过它现在还能不能用（刷新页面会自动检测）。",
    (CHANNEL_CREATOR, STATE_UNKNOWN): "已确认是登录态，但还没检测过它现在还能不能用（刷新页面会自动检测）。",
    (CHANNEL_ARK, STATE_UNKNOWN): "已确认是登录态，但还没检测过它现在还能不能用（刷新页面会自动检测）。",
}


def _layer12_state(channel: str, cookies: str) -> str:
    """L1/L2（**零平台请求**）。过不了 L2 就是 `guest`，**过了也不是 `valid`**。

    ⚠️ 这里最容易写成 `return STATE_VALID if ok else STATE_GUEST` —— 那等于**替平台
    宣布「还能用」，而依据只是「jar 里有两把钥匙」**。L2 只回答「是不是登录态」；
    「还能不能用」只有 L3 的探针能答（§18.5）。所以 L2 过关的正确取值是
    `unknown`（**未确认**），随后由 `probe=True` 把它推进到 `valid` / `expired`。
    """
    if not cookies:
        return STATE_MISSING
    if channel == CHANNEL_WWW:
        return STATE_UNKNOWN if has_web_session(cookies) else STATE_GUEST
    if channel == CHANNEL_CREATOR:
        return STATE_UNKNOWN if has_creator_token(cookies) else STATE_GUEST
    return STATE_UNKNOWN if has_ark_token(cookies) else STATE_GUEST


def _probe_www(cookies: str, profile_adapter: Any) -> tuple[str, str]:
    """L3 探活网页版（`/v2/user/me`，只读）。返回 `(state, reason)`。

    **三种结局，不能压成两种**：

    * `success=True` 且 `data.nickname` 非空 → `valid`；
    * `success=True` 但没有昵称 → `guest`（真机实测：游客会话正是这个形状，
      `ok=True / nickname=None`，设计 12 §17.6.2 的三组对照）；
    * `success=False`：
      - **`payload is None`** ⇒ SDK 连平台的 JSON 都没拿到（网络/超时/解析失败）
        ⇒ **`unknown`**。**这是本函数最容易写错的地方**：把它判成 `expired`
        就是又一次「用『没问到』冒充『不可用』」；
      - 拿到了 JSON ⇒ 平台**答复并拒绝**（真机口径：`msg = 登录已过期`）⇒ `expired`。
    """
    try:
        success, message, payload = profile_adapter.probe_self_profile(cookies)
    except Exception as exc:  # noqa: BLE001 - 探活失败本身是业务结果，不是程序错误
        logger.warning("[login-state] www probe raised: %s", exc)
        return STATE_UNKNOWN, f"检测网页版令牌时出错，状态未确定：{exc}"

    if success:
        data = payload.get("data") if isinstance(payload, dict) else None
        nickname = str((data or {}).get("nickname") or "").strip() if isinstance(data, dict) else ""
        if nickname:
            return STATE_VALID, _REASONS[(CHANNEL_WWW, STATE_VALID)]
        return STATE_GUEST, _REASONS[(CHANNEL_WWW, STATE_GUEST)]
    if payload is None:
        return STATE_UNKNOWN, f"检测网页版令牌未得到结论（请求未完成）：{message}"
    return STATE_EXPIRED, f"{_REASONS[(CHANNEL_WWW, STATE_EXPIRED)]}（平台原话：{message}）"


def _probe_creator(cookies: str, adapter_factory: Any) -> tuple[str, str]:
    """L3 探活创作者平台（`GET /api/galaxy/user/info`，只读）。返回 `(state, reason)`。

    与 `_probe_www` 同构：三种结局（valid / 平台答复并拒绝 → expired /
    没拿到 JSON → unknown）。⚠️ 千帆那份**超集** jar 也能过这个探针（它带
    creator 域令牌）—— 那是平台事实，不是漏洞。
    """
    try:
        adapter = (adapter_factory or _default_creator_factory())(cookies)
        success, message, payload = adapter.get_creator_profile()
    except Exception as exc:  # noqa: BLE001 - 探活失败本身是业务结果
        logger.warning("[login-state] creator probe raised: %s", exc)
        return STATE_UNKNOWN, f"检测创作者平台令牌时出错，状态未确定：{exc}"

    if success:
        return STATE_VALID, _REASONS[(CHANNEL_CREATOR, STATE_VALID)]
    if payload is None:
        return STATE_UNKNOWN, f"检测创作者平台令牌未得到结论（请求未完成）：{message}"
    return STATE_EXPIRED, f"{_REASONS[(CHANNEL_CREATOR, STATE_EXPIRED)]}（平台原话：{message}）"


class _DaemonThreadPool(ThreadPoolExecutor):
    """单线程池，但**线程是 daemon**（`_call_with_timeout` 专用）。

    `concurrent.futures.ThreadPoolExecutor` 没有 daemon 开关。它的后台线程由
    `_adjust_thread_count()` 创建并**立即 `start()`**，所以「先 `super()` 建完
    再改 `daemon`」必抛 `RuntimeError: cannot set daemon status of active thread`
    （v1.32 实测踩过）。**唯一可行的位置是「建好线程、`start()` 之前」** ——
    所以这里把父类那段逻辑整个搬过来自己写，只多了 `thread.daemon = True` 一行。

    ⚠️ 别用「给实例赋一个函数」（`pool._adjust_thread_count = f`）：那是**非描述符**
    赋值，父类内部按 `self._adjust_thread_count()` 调用时 `self` 会被当成第一个
    位置参数，patch 静默失效（实测线程仍是 `daemon=False`，pytest 收尾照样等 30s）。
    必须走**继承**。

    ⚠️ 为什么非要有这个类：默认线程是**非 daemon**，Python 退出（含 pytest 收尾）
    会 `join()` 它们 —— 挂住的探针线程于是又变回「阻塞整个进程 30s」。
    daemon 线程随进程一起消失，这才是「不阻塞调用方」的完整含义。

    逻辑与 `concurrent.futures.thread.ThreadPoolExecutor._adjust_thread_count`
    （CPython 3.12）逐行对齐，只为可控性而内联；改动 CPython 版本时要复核。
    """

    def _adjust_thread_count(self) -> None:  # type: ignore[override]
        if self._idle_semaphore.acquire(timeout=0):  # type: ignore[attr-defined]
            return

        def weakref_cb(_, q=self._work_queue):  # type: ignore[attr-defined]
            q.put(None)

        num_threads = len(self._threads)  # type: ignore[attr-defined]
        if num_threads < self._max_workers:  # type: ignore[attr-defined]
            thread_name = "%s_%d" % (self._thread_name_prefix or self, num_threads)  # type: ignore[attr-defined]
            thread = threading.Thread(
                name=thread_name,
                target=_worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._work_queue,  # type: ignore[attr-defined]
                    self._initializer,  # type: ignore[attr-defined]
                    self._initargs,  # type: ignore[attr-defined]
                ),
            )
            thread.daemon = True  # ⬅️ 本子类存在的**唯一**理由
            thread.start()
            self._threads.add(thread)  # type: ignore[attr-defined]
            _threads_queues[thread] = self._work_queue  # type: ignore[attr-defined]


def _daemon_pool() -> ThreadPoolExecutor:
    """建一个**线程是 daemon** 的单线程池。"""
    return _DaemonThreadPool(
        max_workers=1, thread_name_prefix=PROBE_TIMEOUT_THREAD_PREFIX
    )


def _call_with_timeout(call: Callable[[], Any], timeout: float) -> Any:
    """在**独立线程**里跑 `call()`，超过 `timeout` 秒就抛 `TimeoutError`（v1.32）。

    为什么需要它：千帆 SDK 的 `timeout=` 只管**单个 socket 读/写**，连接建立、
    重定向、以及被平台挂住（半开连接）都不受它约束 —— 真机上「一直未返回检测结果」
    就是这种形态。探针是**串行**跑一个账号的三个平台的，一个卡住 = 整张卡片没有结论。

    ⚠️ 超时后**底层线程无法被强杀**（Python 没有安全的线程取消）：它会作为 daemon
    线程自然结束。这正是我们能接受的代价 —— 探针是只读 GET，最坏情形是它自己
    超时退出，不会写任何东西、不会占住 DB（`cookie_map` 是内存快照）。

    ⚠️⚠️ **不许用 `with ThreadPoolExecutor(...)`，也不许在 `finally` 里
    `shutdown(wait=True)`**：两者都会在退出时 `join()`，于是「超时」变成
    「等它真的跑完」—— 实测挂 30s 的 adapter 让本函数等了整整 30s（v1.32 反例）。
    正确做法是 `shutdown(wait=False)`，且**只调用一次**（不能既 `with` 又手动
    `shutdown`）。`cancel_futures` 也无效 —— 它只取消**尚未开始**的任务。

    ⚠️⚠️ **线程必须是 daemon**（`_daemon_pool()`）：`ThreadPoolExecutor` 默认
    建**非 daemon** 线程，Python 在退出（含 pytest 收尾）时会 `join()` 它们 ——
    于是挂住的 adapter 又把 30s 等回来了。daemon 线程随进程一起消失，这才是
    「不阻塞调用方」的完整含义。
    """
    pool = _daemon_pool()
    try:
        future = pool.submit(call)
    except BaseException:
        pool.shutdown(wait=False)
        raise
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        raise TimeoutError(f"探针超过 {timeout}s 未返回") from None
    finally:
        # ✅ 不 join：超时后立刻把控制权还给调用方，挂在里面的 daemon 线程由它自己收场。
        #    **这是本函数存在的全部理由。**
        pool.shutdown(wait=False)


def _probe_ark(
    cookies: str,
    adapter_factory: Any,
    *,
    max_attempts: int | None = None,
    attempt_timeout: int | None = None,
) -> tuple[str, str]:
    """L3 探活千帆（`GET /api/edith/seller/info/v2`，只读）。返回 `(state, reason)`。

    SDK 已经把「凭据失效」（`CREDENTIAL_ERROR_CODES`）与「接口变了」分开（`is_credential_error`），
    这里只在**明确是凭据问题**时才判 `expired`，其余失败一律 `unknown`。

    ⚠️ 2026-09-21 拆分后，**创作者平台的 jar 打这个探针只会得到 `-13000`**
    （它没有 ark 域令牌）—— 那是实话：那份令牌确实打不开千帆。所以取票必须
    按平台分清楚（`qianfan_cookies_for_account`），不能让千帆探针拿到创作者 jar。

    ## 有界重试（v1.32，2026-09-30 —— 修「一直未返回检测结果」）

    旧实现在任何失败上都**一次即弃**，于是千帆接口偶发慢（首次 TLS 握手 + 平台抖动）
    会让卡片永久停在「未检测」，用户既没有结论也没有重登入口。现在：

    | 结局 | 条件 | 是否继续重试 |
    |---|---|---|
    | `valid` | `success=True` | ❌ 得到结论，结束 |
    | `expired` | 平台答复 `code=-13000` | ❌ **平台明确拒绝**，重试无意义，结束 |
    | `unknown` | 平台答复了别的 code | ❌ 拿到 JSON 就说明链路通，重试只会拿到同一句 |
    | `unknown` | **没拿到 JSON**（超时 / 连接失败 / 解析失败） | ✅ 重试，最多 `max_attempts` 次 |
    | **`expired`** | **连续 N 次都没拿到 JSON** | ❌ 放弃并**给出重登入口** |

    ⚠️ 第一张表的 `expired` 行**必须走 SDK 的唯一判据**（`XHS_Qianfan_Apis.is_credential_error`），
    不能自己写 `code == -13000` —— DEF-084（2026-09-30）：平台令牌过期时回的是 **`-13004`**，
    只认 `-13000` 会把「已过期」错归成「未得到结论」⇒ 卡片永久停在「未检测」、
    而且 `unknown` **不可点** ⇒ 用户既没结论也没有重登入口（这正是用户报的「一直卡未检测」）。

    ⚠️ 最后一行的判据说清楚：这不是「替平台宣布失效」，而是**「我们连不上它，
    所以请用户重新登录以确保万无一失」** —— reason 里明写尝试次数与最后一次的
    错误原话，**绝不伪造平台原话**。这就是用户要的「三次重试强制重新登录」。

    ⚠️ `max_attempts <= 1` 时退化为**旧行为**（单次探测，失败即 `unknown`）——
    测试与「关掉重试」的调用方都能用它拿到老语义。
    """
    from apis.xhs_qianfan_apis import XHS_Qianfan_Apis

    is_credential_error = XHS_Qianfan_Apis.is_credential_error
    attempts_limit = PROBE_MAX_ATTEMPTS if max_attempts is None else int(max_attempts)
    attempts_limit = max(1, attempts_limit)
    timeout = PROBE_ATTEMPT_TIMEOUT if attempt_timeout is None else attempt_timeout

    last_message = ""
    for attempt in range(1, attempts_limit + 1):
        try:
            # ⚠️ **构造 adapter 也必须进超时保护**（v1.32 实测踩过）：工厂里可能
            #    带 IO（真机 SDK 的 `__init__` 目前不联网，但把「建对象」和
            #    「发请求」分开包会让挂住点从保护圈里漏出去 —— 测试正是用一个
            #    会 sleep 的工厂抓出了这个洞，卡了整整 30s）。
            def _attempt() -> Any:
                adapter = (adapter_factory or _default_qianfan_factory())(cookies)
                return adapter.get_seller_info()

            success, message, payload = _call_with_timeout(_attempt, timeout)
        except TimeoutError:
            # ⚠️ 墙钟超时 = **我们自己切断了这次尝试**，等价于「没拿到 JSON」
            #    ⇒ 与传输失败同路：可重试，而不是直接给 unknown。
            last_message = f"探针超过 {timeout}s 未返回"
            logger.info(
                "[login-state] ark probe timed out (attempt %s/%s)", attempt, attempts_limit
            )
            if attempt < attempts_limit:
                time.sleep(PROBE_RETRY_BACKOFF)
            continue
        except Exception as exc:  # noqa: BLE001 - 探活失败本身是业务结果，不是程序错误
            logger.warning("[login-state] ark probe raised (attempt %s): %s", attempt, exc)
            return STATE_UNKNOWN, f"检测千帆令牌时出错，状态未确定：{exc}"

        if success:
            return STATE_VALID, _REASONS[(CHANNEL_ARK, STATE_VALID)]

        code = payload.get("code") if isinstance(payload, dict) else None
        if is_credential_error(code):
            # 平台**答复并拒绝** —— 这是最确定的结论，重试没有意义。
            return STATE_EXPIRED, f"{_REASONS[(CHANNEL_ARK, STATE_EXPIRED)]}（平台原话：{message}）"

        if payload is not None:
            # 拿到了 JSON 但不认识 ⇒ 平台/接口变了，重试也是同一句。
            return STATE_UNKNOWN, f"检测千帆令牌未得到结论（{message}）"

        # —— 到这里 = **连平台的 JSON 都没拿到**（超时 / 连接失败），这是可重试的。
        last_message = str(message or "")
        logger.info(
            "[login-state] ark probe got no JSON (attempt %s/%s): %s",
            attempt,
            attempts_limit,
            last_message or "无消息",
        )
        if attempt < attempts_limit:
            time.sleep(PROBE_RETRY_BACKOFF)

    # 三次都没拿到 JSON ⇒ 放弃探测，**给出重登入口**（reason 不伪造平台原话）。
    return STATE_EXPIRED, (
        f"{_REASONS[(CHANNEL_ARK, STATE_EXPIRED)]}"
        f"（连续 {attempts_limit} 次检测都未得到结论，最后一次：{last_message or '请求未完成'}）"
    )


def _default_creator_factory():
    from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter

    return XhsCreatorApiAdapter


def _default_qianfan_factory():
    from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory

    return get_qianfan_api_adapter_factory()


def _probe_channels_for_account(
    account_id: int,
    cookie_map: dict[tuple[int, str], str],
    *,
    probe: bool,
    profile_adapter: Any,
    qianfan_adapter_factory: Any,
    creator_adapter_factory: Any,
) -> dict[str, dict[str, Any]]:
    """**一个账号**的三平台登录态（串行与并发**共用这一份实现**，§18.18.3）。

    ⚠️ 只读 `cookie_map`（并发前一次性读好的内存快照）——**线程内不碰 DB**：
    SQLAlchemy `Session` 不是线程安全的，把会话带进线程池等于埋一颗随机崩溃。
    """
    channels: dict[str, dict[str, Any]] = {}
    for channel in CHANNELS:
        cookies = cookie_map.get((account_id, channel), "")
        state = _layer12_state(channel, cookies)
        entry = {
            "state": state,
            "reason": _REASONS.get((channel, state), ""),
            "probed": False,
            "label": CHANNEL_LABELS.get(channel, channel),
        }
        # L3：只在**显式**探活、且 L2 已经确认「是登录态」时打那一次只读请求。
        # `missing` / `guest` 探也没有意义 —— 没有可用令牌，探针只会告诉我们
        # 已知的事实，白花一次请求（风控：请求数越少越好）。
        if probe and state == STATE_UNKNOWN:
            if channel == CHANNEL_WWW:
                probed_state, reason = _probe_www(cookies, profile_adapter)
            elif channel == CHANNEL_CREATOR:
                probed_state, reason = _probe_creator(cookies, creator_adapter_factory)
            else:
                probed_state, reason = _probe_ark(cookies, qianfan_adapter_factory)
            entry["state"] = probed_state
            entry["reason"] = reason
            entry["probed"] = True
        channels[channel] = entry
    return channels


def login_states_for_accounts(
    db: Session,
    accounts: Sequence[PlatformAccount],
    *,
    probe: bool = False,
    profile_adapter: Any = None,
    qianfan_adapter_factory: Any = None,
    creator_adapter_factory: Any = None,
) -> dict[int, dict[str, Any]]:
    """一批账号的登录态。`probe=False`（列表加载）**零平台请求**。

    返回 `{account_id: {"channels": {www: {...}, creator: {...}, ark: {...}}, "checked_at": iso|None}}`。
    `channels` 里三个平台**都在**（哪怕 `missing`）—— 缺项消失会让前端误以为
    「这个账号没有这个平台」和「后端没算」是两回事。

    ## 并发（§18.18.3，2026-09-28）

    `probe=True` 时**账号之间并行**（线程池，并行度 = `xhs_sync_account_parallelism`），
    而**同一账号的三个平台仍然串行** —— 三个不同域同时打会把单账号的风控面放大，
    账号间并发则打的是**不同账号**，每账号请求数与串行时完全一致。

    * 结论与串行**完全相同**（`pool.map` 保序，逐账号结果各自独立）；
    * `checked_at` 仍是本批**统一的一个时刻**；
    * 并行度 ≤1 或只有 1 个账号 ⇒ 走原串行路径（单账号零线程开销）；
    * 令牌在进线程池**之前**一次性读进内存，线程内不碰 DB。
    """
    cookie_map = latest_cookies_for_channels(db, [account.id for account in accounts])
    checked_at = shanghai_now().isoformat() if probe else None
    account_ids = [account.id for account in accounts]

    def channels_for(account_id: int) -> dict[str, dict[str, Any]]:
        return _probe_channels_for_account(
            account_id,
            cookie_map,
            probe=probe,
            profile_adapter=profile_adapter,
            qianfan_adapter_factory=qianfan_adapter_factory,
            creator_adapter_factory=creator_adapter_factory,
        )

    parallelism = _probe_parallelism() if probe else 1
    if parallelism > 1 and len(account_ids) > 1:
        with ThreadPoolExecutor(
            max_workers=min(parallelism, len(account_ids)),
            thread_name_prefix=PROBE_THREAD_PREFIX,
        ) as pool:
            channel_sets = list(pool.map(channels_for, account_ids))
    else:
        channel_sets = [channels_for(account_id) for account_id in account_ids]

    return {
        account_id: {"channels": channels, "checked_at": checked_at}
        for account_id, channels in zip(account_ids, channel_sets)
    }


# ⚠️ 「哪些状态该给「重新登录」入口」**刻意不在这里**：那是**呈现**决定（改版就意味着改后端），
# 而状态本身才是业务判断。映射只有一份实现：`frontend/src/lib/login-state.ts::loginStateView()`。
