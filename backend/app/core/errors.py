"""统一业务错误（设计 61 §三 A2，2026-10-24）。

## 形状（沿用本项目事实契约，前端零改动）

```json
428 {"detail": {"code": "X_NO_SESSION", "message": "需要网页会话",
               "platform_message": "平台原文", "details": {"account_id": 7}}}
```

前端 `lib/api.ts::detailMessage()` 的三分支解析（string / 数组 / `{message, platform_message}`）
命中第三分支 —— `platform_message` 与 `details` 是**可选扩展字段**，缺省不下发。

## 为什么继承 `Exception` 而不是 `HTTPException`

2026-10-24 调研实测（fastapi 0.141 / starlette 1.6）：`BizError` 注册 handler 后，
`HTTPException` **不被截胡**（非其子类，走内置 handler），`BizError` 子类被父类
handler 按 MRO 接住；422（RequestValidationError）有专用 handler，形状原样保留。
继承 `Exception` ⇒ 与存量 553 处 `raise HTTPException` **零纠缠**：
BizError 只影响显式 `raise BizError(...)` 的代码路径，渐进迁移（A6 之后逐域替换）无风险。

## 为什么不做 RFC 9457 / 兜底 Exception handler

* RFC 9457 把 `detail` 规约为字符串，与本项目结构化 `detail={code,message}` 语义错位；
  422 形状会被改写，前端数组分支与既有契约测试同时破坏 —— 不采纳。
* 兜底 `Exception` handler 挂在最外层 ServerErrorMiddleware 上、**穿透用户中间件**
  （调研实测：审计中间件的 dispatch 回调对 500 不执行），且前端对纯 500 已有通用兜底
  ⇒ 不注册。
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class BizError(Exception):
    """业务错误第一公民：替代「手拼 detail dict 的 HTTPException」。

    `code` 用**稳定的机器可读串**（如 `X_NO_SESSION`）；`status_code` 不进枚举 ——
    同一 code 在不同上下文可配不同状态码。存量散落的 ~40 个 code 字典按域逐步
    收编为本模块的常量登记表（后续轮次），新代码一律 `raise BizError(...)`。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        platform_message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.platform_message = platform_message
        self.details = details

    def to_detail(self) -> dict[str, Any]:
        """序列化成响应包络的可选字段形态（缺省字段不下发，省一个「空串 vs 缺失」歧义）。"""
        detail: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.platform_message:
            detail["platform_message"] = self.platform_message
        if self.details:
            detail["details"] = self.details
        return detail


def register_error_handlers(app: FastAPI) -> None:
    """注册统一错误 handler（`create_app` 里调用一次）。

    ⚠️ 只注册 `BizError`：HTTPException / RequestValidationError 照走 FastAPI 默认
    （继承关系与 422 形状均已实测确认，见模块 docstring）。
    """

    @app.exception_handler(BizError)
    async def _biz_error_handler(request: Request, exc: BizError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.to_detail()})
