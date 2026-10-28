"""API 日志 —— 已并入 `sys_log`（`log_type='api'`，2026-10-28）。

本模块只做**转发**：保留原导入路径 `from backend.app.models.api_log import ApiLog`
不改，模型实体与表在 :mod:`backend.app.models.sys_log`。
"""

from backend.app.models.sys_log import (
    API_STATUS_CLIENT_ERROR,
    API_STATUS_SERVER_ERROR,
    API_STATUS_SUCCESS,
    LOG_TYPE_API,
    ApiLog,
    SysLog,
)

__all__ = [
    "API_STATUS_CLIENT_ERROR",
    "API_STATUS_SERVER_ERROR",
    "API_STATUS_SUCCESS",
    "ApiLog",
    "LOG_TYPE_API",
    "SysLog",
]
