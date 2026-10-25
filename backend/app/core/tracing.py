"""链路追踪上下文（设计 67，2026-10-25 用户口径「日志应以链路式记录」）。

## 一、为什么用 W3C Trace Context（而不是自造一套）

用户要求「调研市场成熟方案」，市场事实：

* **W3C Trace Context**（`traceparent` 头）是事实标准 —— OpenTelemetry 默认传播器、
  Nginx / Envoy / 主流 APM / 云厂商网关都认这个格式；
* 它的 trace-id 是 **32 位小写十六进制**（128 bit），全局唯一、无需协调；
* 自带版本位与 flags 位，能向后兼容演进。

自造格式的代价：接 APM / 网关 / 日志平台时每一样都要写适配器。**用标准，
将来接什么都免费。** 参考：
* https://www.w3.org/TR/trace-context
* https://opentelemetry.io/docs/concepts/context-propagation/

## 二、本项目的特殊约束（决定实现形态）

项目里有 **49 处线程 / 队列型后台工作**（同步 worker、发布 worker、LLM 生成、
OCR 线程池……）。链路不能断在这些地方，否则「一次点击触发的后台任务」在日志里
就是孤儿。

三层传递，缺一不可：

1. **HTTP 层**（`TraceMiddleware`）：读入站 `traceparent`（没有就新建），
   写进 `contextvars`；
2. **contextvar**：同线程内（含 async、含任何函数调用）**自动**带下去 ——
   这是 `contextvars` 的设计目的，不需要到处传参；
3. **跨线程 / 跨任务**：`contextvars` **不会**自动跨线程传播（`ThreadPoolExecutor`
   提交时拿到的是提交瞬间的上下文副本，但**worker 线程自己有独立上下文**）⇒
   必须显式传：`spawn_traced(fn, ...)` / 任务行里存 `trace_id` 后再 `bind()`。

## 三、为什么「链路 ID」也落库而不是只落日志文件

用户要求「后台支持使用链路 id 查该链路请求的所有日志」⇒ 需要能**按 ID 检索**。

* `api_logs.trace_id` / `business_logs.trace_id` / `scheduled_job_logs.trace_id`
  三张表都加列 + 索引；
* 任务的 `payload` 里也存一份 —— 后台 worker 认领时据此**接回原链路**
  （HTTP 请求早就返回了，contextvar 已经没了）。

## 四、缺值口径

没有链路 ID 的日志（迁移前的存量、纯系统定时任务）`trace_id = NULL`。
**不补一个假 ID** —— 那会让「按链路查」查出无关的行。
"""

from __future__ import annotations

import contextvars
import os
import re
import secrets
import threading
from typing import Any, Callable, Optional

#: 当前链路 id（32 位十六进制，W3C 格式）。空 = 不在任何链路里。
_TRACE_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "trace_id", default=None
)
#: 当前 span id（16 位十六进制）。一个链路里有多个 span（HTTP / 后台任务各一个）。
_SPAN_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "span_id", default=None
)

#: W3C `traceparent`：`version-traceid-spanid-flags`（小写十六进制）。
_TRACEPARENT_RE = re.compile(
    r"^[ \t]*([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})"
)

#: 全零的 trace-id / span-id 按 W3C 规范**无效**（必须丢弃并新建）。
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16

TRACEPARENT_HEADER = "traceparent"
#: 回给前端的响应头 —— 出问题时用户能报出这个 id，运维据此查整条链路。
TRACE_ID_RESPONSE_HEADER = "X-Trace-Id"


def new_trace_id() -> str:
    """生成 32 位小写十六进制 trace id（W3C 格式，128 bit）。"""
    return secrets.token_hex(16)


def new_span_id() -> str:
    """生成 16 位小写十六进制 span id。"""
    return secrets.token_hex(8)


def parse_traceparent(value: str | None) -> tuple[Optional[str], Optional[str]]:
    """解析入站 `traceparent`，返回 `(trace_id, parent_span_id)`。

    不合规一律返回 `(None, None)` —— 按 W3C 规范「必须丢弃非法头并新建」，
    **不尝试修复**（修复会让不同实现的 bug 互相传染）。
    """
    if not value:
        return None, None
    match = _TRACEPARENT_RE.match(str(value))
    if not match:
        return None, None
    trace_id, span_id = match.group(2), match.group(3)
    if trace_id == _ZERO_TRACE or span_id == _ZERO_SPAN:
        return None, None
    return trace_id, span_id


def format_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    """拼出出站 `traceparent`（调用外部平台 / 下一个服务时用）。"""
    return f"00-{trace_id}-{span_id}-{'01' if sampled else '00'}"


def current_trace_id() -> Optional[str]:
    """当前链路 id（没有 ⇒ `None`，**不造值**）。"""
    return _TRACE_ID.get()


def current_span_id() -> Optional[str]:
    return _SPAN_ID.get()


def start_trace(*, trace_id: Optional[str] = None) -> tuple[str, str]:
    """开启一个新链路（或接续给定的 trace_id），返回 `(trace_id, span_id)`。"""
    tid = trace_id or new_trace_id()
    sid = new_span_id()
    _TRACE_ID.set(tid)
    _SPAN_ID.set(sid)
    return tid, sid


def bind(trace_id: Optional[str], span_id: Optional[str] = None) -> None:
    """把当前上下文绑到指定链路（后台 worker 认领任务时用）。

    ⚠️ 不传 `trace_id` 时**什么都不做** —— 不要「顺手新建一个」：那会让
    「这个任务本来就没有链路」看起来像「它自己开了一条链路」。
    """
    if not trace_id:
        return
    _TRACE_ID.set(trace_id)
    _SPAN_ID.set(span_id or new_span_id())


def clear() -> None:
    """清空当前上下文（测试与线程收尾用）。"""
    _TRACE_ID.set(None)
    _SPAN_ID.set(None)


def child_span() -> str:
    """在当前链路下开一个子 span（后台任务的每一步各算一个 span）。"""
    sid = new_span_id()
    _SPAN_ID.set(sid)
    return sid


# ---------------------------------------------------------------------------
# 跨线程 / 跨任务传递
# ---------------------------------------------------------------------------


def spawn_traced(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> threading.Thread:
    """起一个**继承当前链路**的线程（项目里 49 处后台工作的统一入口）。

    ## 为什么需要它

    `contextvars` 在**同线程**内自动传播，但 `threading.Thread` **不继承**
    父线程的 context —— 新线程从空上下文开始。不显式传的话，后台任务的日志
    全部 `trace_id=NULL`，「点一次按钮触发的同步任务」在链路里查不到。

    ## 用法

    ```python
    # 原先：threading.Thread(target=worker, daemon=True).start()
    spawn_traced(worker)          # 之后 worker 里 current_trace_id() 有值
    ```
    """
    ctx = contextvars.copy_context()

    def _run() -> None:
        ctx.run(fn, *args, **kwargs)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def run_traced(trace_id: Optional[str], fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """在**指定链路**下同步执行（线程池 worker 里用）。

    与 `spawn_traced` 的分工：那个继承**当前**上下文，这个接续**任务行里存的**
    trace_id（HTTP 早已返回，当前上下文是空的）。
    """
    if not trace_id:
        return fn(*args, **kwargs)
    ctx = contextvars.copy_context()

    def _wrapped() -> Any:
        bind(trace_id)
        return ctx.run(fn, *args, **kwargs)

    return _wrapped()


#: 环境变量：允许**外部**（网关 / 浏览器扩展）指定链路 id 便于联合排查。
#: 默认开启 —— 与 W3C 规范一致（服务端应接受上游的 traceparent）。
ACCEPT_INBOUND = os.environ.get("XHS_TRACE_ACCEPT_INBOUND", "1") != "0"
