"""LLM 网关监控与预警服务（设计 17 §4.5 / §3.12，v1.6）。

架构 A（用户口径「预警做站内通知」「数据层全归主库」）：监控任务
`system.llm_gateway_monitor` 定时轮询**本地台账** `llm_gateway_usage_logs`，
对照 `llm_gateway_alerts` 规则判断，超阈值 → 写 `notifications` 站内通知。

- 指标：`calls`（窗口内调用量）/ `fail_rate`（失败率 0–1）/ `health`（网关健康）。
- 去重：同规则同日只发一次（`last_fired_at` 当天已发 ⇒ 跳过）。
- 通知：复用 `notification_service`（站内通知中心，不接外部推送）。

## ⚠️ 三级作用域（v2.3，2026-10-02 用户口径「预警规则挂可以控制到具体模型维度，
也可以服务商维度或全网关」）

| 作用域 | `alert.gateway_id` | `alert.model_name` | 判定方式 |
|---|---|---|---|
| 全网关 | `None` | `""` | **遍历该用户全部服务商逐个判**，任一命中即发（方案 A，见下） |
| 服务商 | 具体 id | `""` | 该服务商全部模型/平台/功能 |
| 具体模型 | 具体 id | 非空 | 台账再按 `model_name` 过滤 |

⚠️ **全网关为什么逐个判而不是合并判**（2026-10-02 推荐 A、用户采纳）：合并判会把
「一个服务商烂到底、其余全健康」稀释成「整体失败率不超阈」⇒ **漏报单点故障**；
预警的价值恰恰在「定位到出问题的那个服务商」。代价是异常多时会发多条通知
（通知正文带服务商名，可分辨）—— 可接受的显式代价。
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


def _metric_value_for_gateway(
    db: Session, alert: LlmGatewayAlert, user_id: int, gateway_id: int
) -> float | None:
    """按规则维度聚合**单个服务商**窗口内台账，返回指标值；无数据返回 None（不触发）。"""
    since = shanghai_now() - timedelta(seconds=alert.window_seconds)

    # health 指标：直接看服务商健康标记，不看台账
    if alert.metric == "health":
        gw = db.get(LlmGatewayConfig, gateway_id)
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
        LlmGatewayUsageLog.gateway_id == gateway_id,
        LlmGatewayUsageLog.created_at >= since,
    )
    if alert.platform:
        stmt = stmt.where(LlmGatewayUsageLog.platform == alert.platform)
    if alert.function:
        stmt = stmt.where(LlmGatewayUsageLog.function == alert.function)
    # ⚠️ v2.3：具体模型作用域 —— 台账再按 model_name 过滤（数据源列早已存在）。
    if alert.model_name:
        stmt = stmt.where(LlmGatewayUsageLog.model_name == alert.model_name)

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


def _iter_scope_gateway_ids(db: Session, alert: LlmGatewayAlert, user_id: int) -> list[int]:
    """解析规则的**判定目标服务商 id 列表**（v2.3 三级作用域）。

    - `alert.gateway_id` 非空 ⇒ 单服务商作用域，只判它（`[id]`）。
    - `alert.gateway_id` 为 `None` ⇒ **全网关**作用域，返回该用户名下**全部服务商**
      （逐个判，见模块 docstring「为什么逐个判」）；名下无服务商 ⇒ 空列表（不触发）。
    """
    if alert.gateway_id is not None:
        return [alert.gateway_id]
    rows = db.scalars(
        select(LlmGatewayConfig.id).where(LlmGatewayConfig.user_id == user_id)
    ).all()
    return list(rows)


def evaluate_alert(
    db: Session, alert: LlmGatewayAlert, user_id: int
) -> list[tuple[int, float]]:
    """对一条规则求「命中的 (服务商 id, 指标值) 列表」（v2.3 三级作用域）。

    单服务商 / 具体模型作用域 ⇒ 至多一项；全网关 ⇒ 遍历全部服务商，可多项。
    条件：指标有值（非 None）**且**操作符命中阈值。任一服务商不满足即不产出该项
    （**不使用 None 冒充 0**，同「无数据不触发」纪律）。
    """
    if alert.metric == "health":
        # 健康是「按服务商」的指标；具体模型作用域对 health 无意义（模型不单独探活），
        # 但仍允许（下方遍历时忽略 model_name —— health 分支本就不看台账）。
        pass

    hits: list[tuple[int, float]] = []
    for gid in _iter_scope_gateway_ids(db, alert, user_id):
        value = _metric_value_for_gateway(db, alert, user_id, gid)
        if value is None:
            continue
        if _operator_hit(value, alert.operator, alert.threshold):
            hits.append((gid, value))
    return hits


def _metric_value(db: Session, alert: LlmGatewayAlert, user_id: int) -> float | None:
    """兼容旧签名的**单值**取法（保留给测试与调用方直觉）：取第一个命中项的值。

    ⚠️ 正式判定路径是 `evaluate_alert()` —— 全网关作用域可能有**多个**命中项，
    本函数只返回其中一个，**不可用于「是否触发」的最终决策**（会漏掉其它服务商）。
    """
    hits = evaluate_alert(db, alert, user_id)
    return hits[0][1] if hits else None


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


def _describe(alert: LlmGatewayAlert, value: float, gateway_label: str) -> tuple[str, str]:
    """生成通知标题与正文。

    `gateway_label` = 命中的服务商的展示标识（v2.3：全网关作用域下每条通知要指明
    **是哪个服务商**超限，否则用户不知道去看谁）。具体模型作用域时正文带上模型名。
    """
    scope = f"{alert.platform or '全平台'}/{alert.function or '全功能'}"
    if alert.model_name:
        scope = f"{scope}/{alert.model_name}"
    metric_label = {"calls": "调用量", "fail_rate": "失败率", "health": "健康状态"}[alert.metric]
    title = f"LLM 网关预警: {metric_label}超限"
    if alert.metric == "fail_rate":
        body = f"服务商「{gateway_label}」{scope} 近 {alert.window_seconds // 3600}h 失败率 {value:.2%}，超过阈值 {alert.threshold:.2%}"
    elif alert.metric == "health":
        body = f"服务商「{gateway_label}」健康探测失败（不可达）"
    else:
        body = f"服务商「{gateway_label}」{scope} 近 {alert.window_seconds // 3600}h {metric_label} {value:g}，超过阈值 {alert.threshold:g}"
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

        # 服务商名映射（一次查完，通知正文要用；别在循环里 N+1）
        gw_name_of = {
            c.id: (c.name or c.provider_key or f"#{c.id}")
            for c in db.scalars(
                select(LlmGatewayConfig).where(LlmGatewayConfig.user_id == user.id)
            ).all()
        }

        fired = 0
        for alert in alerts:
            hits = evaluate_alert(db, alert, user.id)
            if not hits:
                continue
            if _already_fired_today(alert, now):
                continue

            # 全网关作用域可能命中多个服务商：逐个发通知（方案 A，定位到具体服务商）。
            # 单服务商 / 具体模型作用域恒为一项。
            for gid, value in hits:
                label = gw_name_of.get(gid, f"#{gid}")
                title, body = _describe(alert, value, label)
                _create(
                    db,
                    user_id=user.id,
                    title=title,
                    body=body,
                    level=alert.level or "warning",
                    source_type="llm_gateway_alert",
                    source_id=alert.id,
                )
                fired += 1
            alert.last_fired_at = now

        db.commit()
        return {"checked_rules": len(alerts), "fired": fired}
    finally:
        db.close()


def ensure_default_alerts_for_gateway(db: Session, *, user_id: int, gateway_id: int) -> int:
    """给某网关补种**默认预警规则**（幂等：同 `(user_id, gateway_id, metric, operator, platform, function)`
    已有行则不插）。返回新建条数。

    **为什么在网关创建时播、而不是 lifespan 全局播**：预警规则依附 `gateway_id`
    （非空外键），没有网关时播下去就是一批指向不存在网关的悬空行；且默认规则的
    载荷是**保守默认**（24h 失败率 > 50% / 健康探测失败），只在「明显异常」时打扰
    用户 —— 存量网关不主动补种（避免替用户『打开通知开关』），
    仅新建网关时按「规则设置后即生效」口径落库（`enabled=true`）。

    播种源 = `config/initial_data/llm_gateway_alerts.json`（设计 17 §4.5，随包分发）；
    文件缺失 / 坏 ⇒ 返回 0（不臆造默认值，与定时任务种子同纪律）。
    """
    from backend.app.core.initial_data import load_initial_gateway_alerts

    defaults = load_initial_gateway_alerts()
    if not defaults:
        return 0

    existing = set(
        db.execute(
            select(
                LlmGatewayAlert.metric,
                LlmGatewayAlert.operator,
                LlmGatewayAlert.platform,
                LlmGatewayAlert.function,
                LlmGatewayAlert.model_name,
            ).where(
                LlmGatewayAlert.user_id == user_id,
                LlmGatewayAlert.gateway_id == gateway_id,
            )
        ).all()
    )

    created = 0
    for rule in defaults:
        key = (
            rule["metric"],
            rule["operator"],
            rule["platform"],
            rule["function"],
            rule.get("model_name", ""),
        )
        if key in existing:
            continue
        db.add(
            LlmGatewayAlert(
                user_id=user_id,
                gateway_id=gateway_id,
                platform=rule["platform"],
                function=rule["function"],
                model_name=rule.get("model_name", ""),
                metric=rule["metric"],
                operator=rule["operator"],
                threshold=rule["threshold"],
                window_seconds=rule["window_seconds"],
                level=rule["level"],
                enabled=rule["enabled"],
            )
        )
        created += 1
    if created:
        db.commit()
        logger.info(
            "llm_gateway_monitor: 为 user %s 网关 %s 补种 %s 条默认预警规则",
            user_id,
            gateway_id,
            created,
        )
    return created
