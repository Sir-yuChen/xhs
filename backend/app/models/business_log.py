"""业务日志 —— 已并入 `sys_log`（`log_type='business'`，2026-10-28）。

本模块只做**转发**：保留原导入路径 `from backend.app.models.business_log import
BusinessLog` / `LEVELS` 不改，模型实体与表在 :mod:`backend.app.models.sys_log`。

## 为什么要合并

`api_logs` 与 `business_logs` 都是「带 `trace_id` 的只增审计行」，差别只是各自
只用了一部分列（`api` 用 endpoint/status/status_code/meta，`business` 用
level/event/action/actor/target/summary/detail_json）。合表后 `trace_id` 链路上的
两类记录同源，保留期也只剩一条规则。

## 双写（未变）

`business_log_service.record()` 同时写 DB 一行 + `logging.info` 一行（进 app.log）：
DB 行供日志页查询，文本行供 `grep`/告警。DB 写**自开 Session** ⇒ worker 线程可靠。
"""

from backend.app.models.sys_log import (
    LEVEL_ERROR,
    LEVEL_INFO,
    LEVEL_WARNING,
    LEVELS,
    LOG_TYPE_BUSINESS,
    BusinessLog,
    SysLog,
)

__all__ = [
    "BusinessLog",
    "LEVELS",
    "LEVEL_ERROR",
    "LEVEL_INFO",
    "LEVEL_WARNING",
    "LOG_TYPE_BUSINESS",
    "SysLog",
]
