"""链路追踪中间件（设计 67，2026-10-25）。

## 职责

1. 读入站 `traceparent`（W3C 标准）—— 有就接续，没有就新建；
2. 把 `trace_id` / `span_id` 放进 `contextvars`（后续同线程代码自动可见）；
3. 响应头回写 `X-Trace-Id` —— 用户遇到问题时能报出这个 id，运维据此查整条链路；
4. 请求结束清空上下文（避免线程复用把上一次的链路带给下一个请求）。

## 为什么单独一个中间件而不是塞进 `ApiAuditMiddleware`

* **顺序**：trace 必须在**最外层**（先于其它中间件建立上下文），否则审计中间件
  的日志拿不到 trace_id；
* **职责**：审计关心「记什么」，追踪关心「这是哪条链路的第几步」。混在一起后，
  将来想给 trace 加采样 / 加 OpenTelemetry 导出，会被审计的记录条件干扰。

## ⚠️ 为什么必须清空上下文

uvicorn 用线程池跑请求，**同一线程会服务多个请求**。不清空的话，一个没有
`traceparent` 的请求会**继承上一个请求的 trace_id** ⇒ 两条无关的链路在日志里
串成一条，比没有 trace 更糟（误导排查方向）。
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from backend.app.core import tracing


class TraceMiddleware(BaseHTTPMiddleware):
    """为每个 HTTP 请求建立（或接续）一条链路。"""

    async def dispatch(self, request: Request, call_next):
        inbound = request.headers.get(tracing.TRACEPARENT_HEADER)
        trace_id, parent_span = (
            tracing.parse_traceparent(inbound) if tracing.ACCEPT_INBOUND else (None, None)
        )
        #: 入站头非法或没有 ⇒ 新建（W3C 规定：非法值必须丢弃，不尝试修复）。
        trace_id, span_id = tracing.start_trace(trace_id=trace_id)
        if parent_span:
            #: 记录父子关系（目前只用于日志可读性；接 OTel 后是 parent_span_id）。
            request.state.parent_span_id = parent_span
        request.state.trace_id = trace_id
        try:
            response: Response = await call_next(request)
        finally:
            #: ⚠️ 清空必须在**任何**出口做（含异常）—— 见模块 docstring。
            #: 但响应头要在清空前写，所以先算好值。
            pass
        response.headers[tracing.TRACE_ID_RESPONSE_HEADER] = trace_id
        tracing.clear()
        return response
