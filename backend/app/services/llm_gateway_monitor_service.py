"""LLM 网关监控与预警服务（设计 17 §4.5 / §3.12，v1.6）。

架构 A（用户口径「预警做站内通知」「数据层全归主库」）：监控任务
`system.llm_gateway_monitor` 定时轮询**本地台账** `llm_gateway_usage_logs`，
对照 `llm_gateway_alerts` 规则判断，超阈值 → 写 `notifications` 站内通知。

- 指标：`calls`（窗口内调用量）/ `fail_rate`（失败率 0–1）/ `health`（网关健康）。
- 去重：同规则同日只发一次（`last_fired_at` 当天已发 ⇒ 跳过）。
- 通知：复用 `notification_service`（站内通知中心，不接外部推送）。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models import LlmGatewayAlert, LlmGatewayConfig, LlmGatewayUsageLog, User
from backend.app.services.notification_service import _create

logger = logging.getLogger(__name__)


def _metric_value(db: Session, alert: LlmGatewayAlert, user_id: int) -> float | None:
    """按规则维度聚合窗口内台账，返回指标值；无数据返回 None（不触发）。"""
    since = shanghai_now() - timedelta(seconds=alert.window_seconds)

    # health 指标：直接看网关健康标记，不看台账
    if alert.metric == "health":
        gw = db.get(LlmGatewayConfig, alert.gateway_id)
        if gw is None or gw.user_id != user_id:
            return None
        # ⚠️ v2.2 修复（设计 17 §4.5）：**「从未探测」≠「探测失败」**。
        # `healthy` 列默认 False，若直接按 `healthy=False` 触发，则**任何新建服务商在第一次
        # 点「测试连接」之前**都会命中 health eq 0 ⇒ 发出一条假的「健康探测失败（不可达）」通知。
        # 判据 = 探测过（last_health_at 有值）且结果为不健康，才返回 0.0（可触发）；
        # 从未探测 ⇒ 返回 None（不触发，与 fail_rate 无数据同型）。
        if gw.last_health_at is None:
            return None
        # 0 = 不健康（healthy=False），1 = 健康
        return 0.0 if not gw.healthy else 1.0

    stmt = select(LlmGatewayUsageLog).where(
        LlmGatewayUsageLog.user_id == user_id,
        LlmGatewayUsageLog.gateway_id == alert.gateway_id,
        LlmGatewayUsageLog.created_at >= since,
    )
    if alert.platform:
        stmt = stmt.where(LlmGatewayUsageLog.platform == alert.platform)
    if alert.function:
        stmt = stmt.where(LlmGatewayUsageLog.function == alert.function)

    row = db.execute(
        stmt.with_only_columns(
            func.count(LlmGatewayUsageLog.id),
            func.coalesce(
                func.sum(case((LlmGatewayUsageLog.status == "success", 1), else_=0)),
                0,
            ),
        )
    ).one()

    calls = int(row[0] or 0)
    success = int(row[1] or 0)

    if alert.metric == "calls":
        return float(calls)
    if alert.metric == "fail_rate":
        if calls == 0:
            return None  # 无调用 ⇒ 失败率无意义，不触发
        return round((calls - success) / calls, 6)
    return None


def _operator_hit(value: float, operator: str, threshold: float) -> bool:
    if operator == "gt":
        return value > threshold
    if operator == "gte":
        return value >= threshold
    if operator == "lt":
        return value < threshold
    if operator == "eq":
        return value == threshold
    return False


def _already_fired_today(alert: LlmGatewayAlert, now) -> bool:
    """同日去重：last_fired_at 在当天 ⇒ 已发，跳过。"""
    if alert.last_fired_at is None:
        return False
    return alert.last_fired_at.date() == now.date()


def _describe(alert: LlmGatewayAlert, value: float) -> tuple[str, str]:
    """生成通知标题与正文。"""
    scope = f"{alert.platform or '全平台'}/{alert.function or '全功能'}"
    metric_label = {"calls": "调用量", "fail_rate": "失败率", "health": "健康状态"}[alert.metric]
    title = f"LLM 网关预警: {metric_label}超限"
    if alert.metric == "fail_rate":
        body = f"网关 #{alert.gateway_id} {scope} 近 {alert.window_seconds // 3600}h 失败率 {value:.2%}，超过阈值 {alert.threshold:.2%}"
    elif alert.metric == "health":
        body = f"网关 #{alert.gateway_id} 健康探测失败（不可达）"
    else:
        body = f"网关 #{alert.gateway_id} {scope} 近 {alert.window_seconds // 3600}h {metric_label} {value:g}，超过阈值 {alert.threshold:g}"
    return title, body


def run_llm_gateway_monitor_once(user_id: int) -> dict[str, Any]:
    """调度入口：轮询**指定用户**的预警规则，超阈值发站内通知（设计 17 §4.5，DEF-066）。

    user_scoped：一行触发只跑行主人的规则；通知也发到行主人名下。
    """
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            raise RuntimeError(f"run_llm_gateway_monitor_once: user {user_id} not found")

        now = shanghai_now()
        alerts = db.scalars(
            select(LlmGatewayAlert)
            .where(LlmGatewayAlert.user_id == user.id, LlmGatewayAlert.enabled == True)
            .order_by(LlmGatewayAlert.id.asc())
        ).all()

        fired = 0
        for alert in alerts:
            value = _metric_value(db, alert, user.id)
            if value is None:
                continue
            if not _operator_hit(value, alert.operator, alert.threshold):
                continue
            if _already_fired_today(alert, now):
                continue

            title, body = _describe(alert, value)
            _create(
                db,
                user_id=user.id,
                title=title,
                body=body,
                level=alert.level or "warning",
                source_type="llm_gateway_alert",
                source_id=alert.id,
            )
            alert.last_fired_at = now
            fired += 1

        db.commit()
        return {"checked_rules": len(alerts), "fired": fired}
    finally:
        db.close()
