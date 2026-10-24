"""LLM 网关预警规则的**值域常量**（设计 61 A5 下沉，2026-10-24）。

为什么在 core：`core/initial_data.py` 的种子归一化（`_normalize_gateway_alert`）
要在启动期校验预警条目的 metric / operator —— 常量属于**值域定义**，被 api 层
（`llm_gateway_monitor` 的请求校验）与 core 层（种子）共用，依赖方向只能向下。
api 模块 re-export 保持既有 import 路径（实现只有一份）。
"""
from __future__ import annotations

ALERT_METRICS = ("calls", "fail_rate", "health")
#: 操作符白名单
ALERT_OPERATORS = ("gt", "gte", "lt", "eq")
