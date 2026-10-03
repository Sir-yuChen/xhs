"""SQLite 写锁竞争的**共用重试**（DEF-085，2026-10-03）。

## 为什么需要共用

`database is locked` 在本项目是**结构性**问题，不是偶发：

1. 常驻服务 + 定时任务 + 同步 worker 并发写同一个 SQLite 文件；
2. 大量「另开一条 Session 旁路写」的链路（台账记账、指标快照采集）；
3. 业务事务与旁路写的事务窗口天然重叠。

DEF-085 实测到的两处（`llm_gateway_usage_service.record_usage` 与
`note_metrics_service.collect_snapshots`）**根因相同、修法相同** ——
若各写一份「重试 3 次」的循环，改一处忘一处（DEF-044 同型）。

⇒ 判据与循环**只在本次实现**，调用点只管「拿它包一下」。

## 判据为什么不是「所有 OperationalError」

只有**锁竞争**值得重试：`no such table` / 外键缺失 / SQL 语法错这类是**确定性**的，
重试多少次都一样，只会白占业务线程（并放大延迟）。

⚠️ **判据用「类 + 文案」双确认**：`OperationalError` 类过滤掉业务异常，
文案 `is locked` / `database is locked` 过滤掉其它 OperationalError。
"""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

#: 撞锁重试次数与退避基数。SQLite 写锁竞争是**瞬时**的（另一事务持锁数百毫秒量级），
#: 3 次指数退避（0.1 / 0.2 / 0.4s，合计 0.7s）足以跨过一次业务事务的提交窗口，
#: 且总等待远低于用户对一次生成/采集调用的感知阈值。
#:
#: ⚠️ **不要为了「更稳」把它调到几十次**：重试是给**瞬时**竞争用的；
#: 若某条链路持续撞锁，说明它的**事务设计**有问题（持锁太久），
#: 调大重试只会把「设计缺陷」伪装成「偶发慢」。
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.1

#: 判定「这次失败是不是写锁竞争」的文案标记（小写匹配）。
_LOCK_MARKERS = ("database is locked", "is locked", "locked")

T = TypeVar("T")


def is_lock_error(exc: BaseException) -> bool:
    """该异常是否为 SQLite 写锁竞争（**唯一值得重试的类别**）。"""
    try:
        from sqlalchemy.exc import OperationalError
    except Exception:  # noqa: BLE001 —— 拿不到基类时退化为纯文案判据
        OperationalError = ()  # type: ignore[assignment]
    if OperationalError and not isinstance(exc, OperationalError):
        return False
    message = str(exc).lower()
    return any(marker in message for marker in _LOCK_MARKERS)


def retry_on_lock(
    operation: Callable[[], T],
    *,
    what: str,
    attempts: int = RETRY_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """执行 `operation()`，撞锁则指数退避重试。

    ⚠️ **非锁类异常立即抛出**（不吞、不改语义）—— 本函数**不是**安全网，
    它只解决「瞬时竞争」这一种可恢复失败。调用点若需要「失败不抛穿」的语义，
    自己在这一层外面包（见 `llm_gateway_usage_service.record_usage`）。

    `sleep` 可注入 ⇒ 测试不必真的等（也不必依赖假时钟）。
    """
    last_error: BaseException | None = None
    for attempt in range(max(1, attempts)):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 —— 判据在 is_lock_error，非锁类原样抛
            last_error = exc
            if not is_lock_error(exc):
                # 确定性错误：原样抛（调用点自己决定怎么包装）。
                raise
            if attempt == max(1, attempts) - 1:
                # 锁类但用尽 ⇒ 包成**可重试**的语义异常（见 LockRetryExhausted）。
                raise LockRetryExhausted(what, max(1, attempts), exc) from exc
            delay = base_delay * (2**attempt)
            logger.info(
                "%s 撞写锁（第 %s/%s 次），%.2fs 后重试",
                what, attempt + 1, attempts, delay,
            )
            sleep(delay)
    # 理论不可达（循环内要么 return 要么 raise），仅为类型完整性。
    assert last_error is not None
    raise last_error


class LockRetryExhausted(RuntimeError):
    """撞锁重试用尽（**可重试**的瞬时失败，不是服务端 bug）。

    存在的理由：调用点要能把它映射成 **503 / 429 之类的「稍后重试」**，
    而不是让它冒到 FastAPI 变成裸 **500**（「服务器内部错误」）——
    后者会让用户以为功能坏了，也会污染错误率口径（瞬时竞争被算成服务端缺陷）。
    """

    def __init__(self, what: str, attempts: int, cause: BaseException) -> None:
        super().__init__(f"{what}：撞写锁重试 {attempts} 次仍失败（{cause}）")
        self.what = what
        self.attempts = attempts
        self.cause = cause


__all__ = [
    "RETRY_ATTEMPTS",
    "RETRY_BASE_DELAY",
    "LockRetryExhausted",
    "is_lock_error",
    "retry_on_lock",
]
