"""LLM 网关令牌两级映射 + 预警规则 + 用量展示端点（设计 17 §7.1a/§7.1b/§7.5，v1.6）。

架构 A（2026-09-29 用户口径「令牌按平台发、平台下按功能发；预警做站内通知」）：
- 令牌映射 `llm_gateway_tokens`：业务功能 (platform × function) → 上游令牌名。
  v1.7：上游是任意 OpenAI 兼容端点，令牌密钥在上游服务商侧签发，本组端点只管映射关系。
- 预警规则 `llm_gateway_alerts`：用量阈值 / 失败率阈值 / 网关健康，监控任务轮询
  本地台账对照触发，超限写 `notifications` 站内通知。
- 用量展示：从本地台账 `llm_gateway_usage_logs` 聚合（唯一真相源），不再依赖
  `llm_gateway_snapshots` 也不打任何上游管理 API（取代 v1.3 方案）。

端点前缀 `/api/llm/`（工作平台公共维度，与 gateway-configs 一致）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.time import shanghai_now
from backend.app.models import LlmGatewayAlert, LlmGatewayConfig, LlmGatewayUsageLog, User
from backend.app.schemas.common import paginated

router = APIRouter(prefix="/llm", tags=["llm-gateway-monitor"])

#: 指标白名单（§7.1b 契约）
ALERT_METRICS = ("calls", "fail_rate", "health")
#: 操作符白名单
ALERT_OPERATORS = ("gt", "gte", "lt", "eq")


# ───────── 令牌映射（§7.1a）—— ⚠️ v1.9 **整组退场**，保留 410 墓碑 ─────────
#
# 用户口径「令牌映射页面移除」（设计 17 §7.1a）。退场理由：令牌映射的唯一作用
# 是给台账 `token_name` 打业务标签（`resolve_token_name()` 反查），**不参与任何
# 发给上游的请求** —— 既不控制调用也不影响计费，只产生一份手工维护的映射表。
# v1.9 起服务商自身已带 `provider_key` + `platform`，业务维度由「哪个服务商」直接表达。
#
# ⚠️ **墓碑而非删路由**：回 410 而非 404 —— 404 无法区分「id 不存在」与「功能已退场」。
# ⚠️ `llm_gateway_usage_logs.token_name` 列**保留不删**（历史行审计），v1.9 起新行恒落 `""`。

def _tokens_gone() -> None:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "LLM_GATEWAY_TOKENS_GONE",
            "message": "令牌映射功能已移除（v1.9）；服务商自带唯一 KEY 与使用平台",
        },
    )


@router.get("/gateway-tokens", include_in_schema=False)
def list_gateway_tokens() -> None:
    """⚠️ v1.9 退场 ⇒ 410。~~原：按 gateway_id/platform/function 过滤的映射列表~~"""
    _tokens_gone()


@router.post("/gateway-tokens", include_in_schema=False)
def create_gateway_token() -> None:
    """⚠️ v1.9 退场 ⇒ 410。~~原：新增 (platform, function) → token_name 映射~~"""
    _tokens_gone()


@router.patch("/gateway-tokens/{token_id}", include_in_schema=False)
def update_gateway_token(token_id: int) -> None:
    """⚠️ v1.9 退场 ⇒ 410。~~原：改 token_name / enabled~~"""
    _tokens_gone()


@router.delete("/gateway-tokens/{token_id}", include_in_schema=False)
def delete_gateway_token(token_id: int) -> None:
    """⚠️ v1.9 退场 ⇒ 410。~~原：删除一条映射~~"""
    _tokens_gone()


# ───────────────────────── 预警规则（§7.1b）─────────────────────────

def _serialize_alert(a: LlmGatewayAlert) -> dict:
    return {
        "id": a.id,
        "gateway_id": a.gateway_id,
        "platform": a.platform,
        "function": a.function,
        "metric": a.metric,
        "operator": a.operator,
        "threshold": a.threshold,
        "window_seconds": a.window_seconds,
        "level": a.level,
        "enabled": a.enabled,
        "last_fired_at": a.last_fired_at.isoformat() if a.last_fired_at else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


class GatewayAlertCreateRequest(BaseModel):
    gateway_id: int
    platform: str = ""
    function: str = ""
    metric: str = Field(min_length=1, max_length=16)
    operator: str = "gt"
    threshold: float
    window_seconds: int = 86400
    level: str = "warning"
    enabled: bool = True

    model_config = ConfigDict(extra="forbid")


class GatewayAlertUpdateRequest(BaseModel):
    threshold: Optional[float] = None
    operator: Optional[str] = None
    window_seconds: Optional[int] = None
    level: Optional[str] = None
    enabled: Optional[bool] = None

    model_config = ConfigDict(extra="forbid")


def _validate_alert(payload_metric: str, payload_operator: str) -> None:
    if payload_metric not in ALERT_METRICS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "LLM_ALERT_METRIC_UNSUPPORTED", "message": f"metric 需为 {ALERT_METRICS} 之一"},
        )
    if payload_operator not in ALERT_OPERATORS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "LLM_ALERT_OPERATOR_UNSUPPORTED", "message": f"operator 需为 {ALERT_OPERATORS} 之一"},
        )


def _get_owned_alert(db: Session, current_user: User, alert_id: int) -> LlmGatewayAlert:
    alert = db.get(LlmGatewayAlert, alert_id)
    if alert is None or alert.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "LLM_GATEWAY_ALERT_NOT_FOUND", "message": "预警规则不存在"})
    return alert


@router.get("/gateway-alerts")
def list_gateway_alerts(
    gateway_id: Optional[int] = None,
    enabled: Optional[bool] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(LlmGatewayAlert).where(LlmGatewayAlert.user_id == current_user.id)
    if gateway_id is not None:
        stmt = stmt.where(LlmGatewayAlert.gateway_id == gateway_id)
    if enabled is not None:
        stmt = stmt.where(LlmGatewayAlert.enabled == enabled)
    items = db.scalars(stmt.order_by(LlmGatewayAlert.id.desc())).all()
    return paginated([_serialize_alert(a) for a in items], page, page_size)


@router.post("/gateway-alerts", status_code=status.HTTP_201_CREATED)
def create_gateway_alert(
    payload: GatewayAlertCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _validate_alert(payload.metric, payload.operator)
    gw = db.get(LlmGatewayConfig, payload.gateway_id)
    if gw is None or gw.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "LLM_GATEWAY_NOT_FOUND", "message": "网关配置不存在"})

    alert = LlmGatewayAlert(
        user_id=current_user.id,
        gateway_id=payload.gateway_id,
        platform=payload.platform,
        function=payload.function,
        metric=payload.metric,
        operator=payload.operator,
        threshold=payload.threshold,
        window_seconds=payload.window_seconds,
        level=payload.level,
        enabled=payload.enabled,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return _serialize_alert(alert)


@router.patch("/gateway-alerts/{alert_id}")
def update_gateway_alert(
    alert_id: int,
    payload: GatewayAlertUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    alert = _get_owned_alert(db, current_user, alert_id)
    if payload.operator is not None:
        if payload.operator not in ALERT_OPERATORS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "LLM_ALERT_OPERATOR_UNSUPPORTED", "message": f"operator 需为 {ALERT_OPERATORS} 之一"},
            )
        alert.operator = payload.operator
    if payload.threshold is not None:
        alert.threshold = payload.threshold
    if payload.window_seconds is not None:
        alert.window_seconds = payload.window_seconds
    if payload.level is not None:
        alert.level = payload.level
    if payload.enabled is not None:
        alert.enabled = payload.enabled
    db.commit()
    db.refresh(alert)
    return _serialize_alert(alert)


@router.delete("/gateway-alerts/{alert_id}")
def delete_gateway_alert(
    alert_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    alert = _get_owned_alert(db, current_user, alert_id)
    db.delete(alert)
    db.commit()
    return {"id": alert_id, "status": "deleted"}


# ───────────────────────── 用量展示（§7.5）─────────────────────────

def _usage_filter(
    stmt,
    current_user: User,
    gateway_id: Optional[int],
    platform: Optional[str],
    function: Optional[str],
    start: Optional[datetime],
    end: Optional[datetime],
):
    stmt = stmt.where(LlmGatewayUsageLog.user_id == current_user.id)
    if gateway_id is not None:
        stmt = stmt.where(LlmGatewayUsageLog.gateway_id == gateway_id)
    if platform:
        stmt = stmt.where(LlmGatewayUsageLog.platform == platform)
    if function:
        stmt = stmt.where(LlmGatewayUsageLog.function == function)
    if start is not None:
        stmt = stmt.where(LlmGatewayUsageLog.created_at >= start)
    if end is not None:
        stmt = stmt.where(LlmGatewayUsageLog.created_at <= end)
    return stmt


def _usage_bucket(db: Session, stmt) -> dict:
    row = db.execute(
        stmt.with_only_columns(
            func.count(LlmGatewayUsageLog.id),
            func.coalesce(
                func.sum(
                    case((LlmGatewayUsageLog.status == "success", 1), else_=0)
                ),
                0,
            ),
            func.coalesce(func.sum(LlmGatewayUsageLog.prompt_tokens), 0),
            func.coalesce(func.sum(LlmGatewayUsageLog.completion_tokens), 0),
            func.coalesce(func.avg(LlmGatewayUsageLog.latency_ms), 0),
        )
    ).one()
    calls = int(row[0] or 0)
    success = int(row[1] or 0)
    fail = calls - success
    p = int(row[2] or 0)
    c = int(row[3] or 0)
    return {
        "calls": calls,
        "success": success,
        "fail": fail,
        "fail_rate": round(fail / calls, 4) if calls else 0.0,
        "total_tokens": p + c,
        "avg_latency_ms": round(float(row[4] or 0), 1),
    }


@router.get("/usage/overview")
def usage_overview(
    gateway_id: Optional[int] = None,
    platform: Optional[str] = None,
    function: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """总览：今日/近 7 日/近 30 日（从本地台账聚合）。"""
    now = shanghai_now()

    def _bucket(days: int) -> dict:
        stmt = _usage_filter(
            select(LlmGatewayUsageLog), current_user, gateway_id, platform, function,
            now - timedelta(days=days), None,
        )
        return _usage_bucket(db, stmt)

    gw_healthy = None
    if gateway_id is not None:
        gw = db.get(LlmGatewayConfig, gateway_id)
        if gw is not None and gw.user_id == current_user.id:
            gw_healthy = gw.healthy

    return {
        "today": _bucket(1),
        "week": _bucket(7),
        "month": _bucket(30),
        "gateway_healthy": gw_healthy,
    }


@router.get("/usage/daily")
def usage_daily(
    gateway_id: Optional[int] = None,
    platform: Optional[str] = None,
    function: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """按日序列（供折线图）。"""
    if start_date is None:
        start_date = (shanghai_now() - timedelta(days=29)).date()
    if end_date is None:
        end_date = shanghai_now().date()

    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())

    stmt = _usage_filter(
        select(LlmGatewayUsageLog), current_user, gateway_id, platform, function,
        start_dt, end_dt,
    )
    rows = db.execute(
        stmt.with_only_columns(
            func.date(LlmGatewayUsageLog.created_at),
            func.count(LlmGatewayUsageLog.id),
            func.coalesce(
                func.sum(case((LlmGatewayUsageLog.status == "success", 1), else_=0)),
                0,
            ),
            func.coalesce(func.sum(LlmGatewayUsageLog.prompt_tokens), 0),
            func.coalesce(func.sum(LlmGatewayUsageLog.completion_tokens), 0),
            func.coalesce(func.avg(LlmGatewayUsageLog.latency_ms), 0),
        ).group_by(func.date(LlmGatewayUsageLog.created_at))
    ).all()

    result = []
    for day, calls, success, p, c, avg_ms in rows:
        calls = int(calls or 0)
        success = int(success or 0)
        fail = calls - success
        p = int(p or 0)
        c = int(c or 0)
        result.append({
            "date": str(day),
            "calls": calls,
            "success": success,
            "fail": fail,
            "total_tokens": p + c,
            "avg_latency_ms": round(float(avg_ms or 0), 1),
        })
    return result


@router.get("/usage/breakdown")
def usage_breakdown(
    dimension: str = Query(..., description="platform|function|model"),
    gateway_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """维度拆分：按平台/功能/模型分组聚合。

    ⚠️ **v1.9**：`dimension=token` **从白名单移除**（令牌映射退场，设计 17 §7.1a）。
    台账 `token_name` 列保留（历史行审计）但**不再是可选维度** —— v1.9 起新行恒空，
    留着它只会让用户看到一个永远为空的维度。
    """
    col_map = {
        "platform": LlmGatewayUsageLog.platform,
        "function": LlmGatewayUsageLog.function,
        "model": LlmGatewayUsageLog.model_name,
    }
    if dimension not in col_map:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "LLM_USAGE_DIMENSION_UNSUPPORTED", "message": f"dimension 需为 {list(col_map)} 之一"},
        )
    col = col_map[dimension]

    if start_date is None:
        start_date = (shanghai_now() - timedelta(days=29)).date()
    if end_date is None:
        end_date = shanghai_now().date()
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())

    stmt = _usage_filter(
        select(LlmGatewayUsageLog), current_user, gateway_id, None, None, start_dt, end_dt,
    )
    rows = db.execute(
        stmt.with_only_columns(
            col,
            func.count(LlmGatewayUsageLog.id),
            func.coalesce(
                func.sum(case((LlmGatewayUsageLog.status == "success", 1), else_=0)),
                0,
            ),
            func.coalesce(func.sum(LlmGatewayUsageLog.prompt_tokens), 0),
            func.coalesce(func.sum(LlmGatewayUsageLog.completion_tokens), 0),
        ).group_by(col)
    ).all()

    result = []
    for key, calls, success, p, c in rows:
        calls = int(calls or 0)
        success = int(success or 0)
        fail = calls - success
        p = int(p or 0)
        c = int(c or 0)
        result.append({
            "key": key or "",
            "calls": calls,
            "success": success,
            "fail": fail,
            "fail_rate": round(fail / calls, 4) if calls else 0.0,
            "total_tokens": p + c,
        })
    return result
