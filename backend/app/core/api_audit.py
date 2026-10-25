"""API 审计中间件（DEF-019 / T-18，2026-10-04）。

## 为什么

`api_logs` 表**从建表起就没有任何写入点** ⇒ 没有 API 审计。出问题时只能翻
uvicorn 的 stdout，而那里既没有 `user_id`、也不区分「谁在什么时候调了什么」。

## 三条口径（都有守卫）

1. **只记「平台相关」的写与失败**，不记全量读 —— 全量读会把 SQLite 写爆
   （一次页面加载十几个 GET），且没有审计价值。
   规则：`/api/xhs/**` 与 `/api/llm/**` 的**非 GET** 请求，**或**任何 4xx/5xx
   （含 GET，失败也要留痕）。
2. **敏感值不进表**：只落**路径模板**（`/api/xhs/qianfan/orders/ship`），
   **不落 query string / body / 响应体**；`meta` 只放状态码与耗时。
   （`api_logs` 没有请求体列 —— 这是刻意的，不是遗漏。）
3. **失败不阻断业务**：审计写库异常只 warning，绝不让一次日志失败把用户的
   写操作带崩（与「失败不落半截数据」同精神：审计是旁路）。

## 边界（如实说明）

- **不记 GET 成功**（除非失败）：这是**审计**用途，不是流量分析。
  要看调用量请走 LLM 网关台账 / `scheduled_job_logs`。
- **不记录请求体**：排查「请求内容对不对」仍需看前端与平台原文，
  本中间件只回答「谁、何时、调了哪个端点、结果如何」。
"""
from __future__ import annotations

from typing import Callable

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from backend.app.core.database import SessionLocal

#: 测试可注入的会话工厂快照（`conftest.patch_service_sessions` 的约定）。
#: ⚠️ 必须**模块层持有**：函数级 `import SessionLocal` 拿不到模块属性 ⇒ 桩打不到 ⇒
#: 测试会**静默写生产库**（`test_testing_spec_guards` 的守卫钉这条，2026-10-02 实测教训）。
_session_factory = SessionLocal

logger = logging.getLogger(__name__)

#: 业务日志写入回调（由 services 层在启动时注册，见 `set_business_recorder`）。
#:
#: 签名：`(event, *, method, user_id, path, status_code) -> None`
#: 这样 core 不需要认识 `business_log_service`（合同 C1）。
_BUSINESS_RECORDER: "Callable[..., None] | None" = None


def set_business_recorder(recorder: "Callable[..., None] | None") -> None:
    """注册业务日志写入实现（`main.py` lifespan 调用）。传 `None` = 注销（测试用）。"""
    global _BUSINESS_RECORDER
    _BUSINESS_RECORDER = recorder

#: 只审计这些前缀下的**写**操作（读全量记录没有审计价值且会写爆 SQLite）。
#: 设计 64 §2.3（2026-10-25）：系统管理域（`/api/system/**`）与配置（`/api/settings/**`）、
#: 调度（`/api/schedule/**`）的写操作**此前不在审计面** —— 增删改查零留痕
#: （用户口径「操作删除，修改新增等节点」）。补进审计前缀。
AUDITED_WRITE_PREFIXES = ("/api/xhs/", "/api/llm/", "/api/system/", "/api/settings/", "/api/schedule/")

#: 这些路径**永不**记录：认证（含凭据 —— 它的出入口语义由
#: `business_log_service` 的定向埋点记，见 auth.py）、健康检查（高频无价值）。
SKIP_PREFIXES = (
    "/api/auth/",
    "/api/health",
    "/docs",
    "/openapi.json",
)

#: 这些前缀下的写操作**额外**写一条业务日志（`business_logs`，设计 64 §2.3）——
#: 与 api_logs 的分工：api_logs = 端点面留痕，business_logs = 业务语义面
#: （"管理员改了系统域的什么"）。同一次请求两行各司其职。
BUSINESS_EVENT_PREFIXES = ("/api/system/", "/api/settings/", "/api/schedule/")

#: 响应的业务码 → 事务结论（表里 `status` 列）。
_STATUS_OK = "success"
_STATUS_CLIENT_ERROR = "client_error"
_STATUS_SERVER_ERROR = "server_error"


def _should_audit(method: str, path: str, status_code: int) -> bool:
    """是否落审计行（口径 1）。"""
    if any(path.startswith(p) for p in SKIP_PREFIXES):
        return False
    # 失败一律留痕（含 GET）—— 排查「为什么这个请求 500」时它是最先要看的东西。
    if status_code >= 400:
        return True
    # 成功：只记审计前缀下的写操作。
    if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
        return any(path.startswith(p) for p in AUDITED_WRITE_PREFIXES)
    return False


def _status_bucket(status_code: int) -> str:
    if status_code >= 500:
        return _STATUS_SERVER_ERROR
    if status_code >= 400:
        return _STATUS_CLIENT_ERROR
    return _STATUS_OK


class ApiAuditMiddleware(BaseHTTPMiddleware):
    """把「谁在什么时候调了哪个端点、结果如何」写进 `api_logs`。

    ⚠️ **不落 query string / body**（口径 2）：`path` 是**路径模板的近似**
    （真实路径，但已去掉 query）。要看请求内容请查对应的业务表。
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        started = time.perf_counter()
        response: Response
        try:
            response = await call_next(request)
        except Exception:
            # 未捕获异常（500 会在外层处理，这里兜住中间件链上的抛出）——
            # 先落一条 server_error 再抛，否则这类失败零痕迹。
            self._write(request, 500, (time.perf_counter() - started) * 1000)
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        path = request.url.path
        should = _should_audit(request.method, path, response.status_code)
        if should:
            self._write(request, response.status_code, elapsed_ms)
        #: 业务语义面（设计 64 §2.3）：系统域写操作进 business_logs
        #:（成功与失败都记 —— 失败的越权尝试正是要看的）。
        if should and request.method.upper() in ("POST", "PUT", "PATCH", "DELETE") and any(
            path.startswith(p) for p in BUSINESS_EVENT_PREFIXES
        ):
            self._write_business(request, response.status_code)
        return response

    @staticmethod
    def _write_business(request: Request, status_code: int) -> None:
        """系统域写操作 → 业务日志。**失败只 warning**（绝不阻断请求）。

        ⚠️ 实现**不在本模块**：这里只调注入进来的回调（见 `set_business_recorder`）。

        为什么不直接 `from backend.app.services import business_log_service`：
        合同 C1「`core/` 不得 import `services/`」——**函数内延迟 import 同样计入**
        （运行时不可达 ≠ 依赖不存在，见 test_arch_dependency_direction 模块头）。
        A5 刚把 C1 清零，不能为了图省事立刻破例再加一条豁免；
        依赖方向要单向：**services 知道 core，core 不知道 services**。

        由 `main.py` 的 lifespan 在启动时把 services 的实现注册进来
        （`business_log_service.register_with_audit()`）。没注册 ⇒ 静默跳过
        （单元测试里不带 services 跑 audit 中间件时就是这种情形）。
        """
        recorder = _BUSINESS_RECORDER
        if recorder is None:
            return
        try:
            recorder(
                "systemops.request",
                method=request.method.upper(),
                user_id=_current_user_id(request),
                path=request.url.path,
                status_code=status_code,
            )
        except Exception as exc:  # noqa: BLE001 - 业务日志失败绝不阻断请求
            logger.warning("business log write failed for %s: %s", request.url.path, exc)

    @staticmethod
    def _write(request: Request, status_code: int, elapsed_ms: float) -> None:
        try:
            from backend.app.models.api_log import ApiLog

            user_id = _current_user_id(request)
            # ⚠️ 用模块级快照（不是再 import 一次）：测试打桩才生效。
            db = _session_factory()
            try:
                db.add(
                    ApiLog(
                        user_id=user_id,
                        platform=_platform_of(request.url.path),
                        # ⚠️ 只落路径，**不带 query**（口径 2：敏感值不进表）。
                        endpoint=request.url.path[:256],
                        status=_status_bucket(status_code),
                        meta={
                            "method": request.method,
                            "status_code": status_code,
                            "elapsed_ms": round(elapsed_ms, 1),
                        },
                    )
                )
                db.commit()
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 - 审计失败绝不阻断业务（口径 3）
            logger.warning("api audit write failed for %s: %s", request.url.path, exc)


def _current_user_id(request: Request) -> int | None:
    """从请求里解出 `user_id`（**解不出就留空，不猜**；硬约束⑤）。"""
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    try:
        from backend.app.core.security import decode_token

        payload = decode_token(token)
        if payload.get("token_type") != "access":
            return None
        uid = payload.get("user_id")
        return int(uid) if isinstance(uid, int) else None
    except Exception:  # noqa: BLE001 - 坏令牌 / 过期都只是「不知道是谁」
        return None


def _platform_of(path: str) -> str | None:
    """从路径推断平台（`/api/xhs/...` → `xhs`；推不出留空）。"""
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "api":
        candidate = parts[1]
        if candidate in ("xhs", "llm"):
            return candidate
    return None
