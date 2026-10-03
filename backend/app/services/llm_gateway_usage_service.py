"""LLM 网关用量台账写入服务（设计 17 §3.13 / §4.3.1，v1.7）。

v1.7 二次转向（用户口径「项目内建、OpenAI 兼容直连」）：网关由项目**内建薄客户端**
直连上游，每次调用落一行台账 `llm_gateway_usage_logs` —— 本模块是**唯一写入点**。

与 v1.6 的关系：v1.6 的「New API 纯转发外挂进程 + 读 New API logs 库」**被本方案取代**，
台账语义（表结构、`platform`/`function`/`token_name` 维度）沿用不变，只换写入时机与来源：
由「事后从外挂库同步」改为「调用当场旁路记账」。

## 两个入口

- `record_usage()`：已知 `user_id` / `gateway_id` 时直调（唯一 INSERT 点）。
- `record_usage_for_model()`：`ai_service` 专用 —— 只拿到 `ModelConfig`，
  按 `(user_id, base_url)` 反查网关再委托 `record_usage()`。

## 安全网（DEF-048 纪律，不可条件性开关）

**记账失败绝不让业务调用失败**：两个入口都 `try/except` 兜住全部异常，
只 `logger.warning` 后返回 `False`。反查失败（模型配置指向的不是已登记网关）
⇒ 不落半条台账、不打脏数据，同样只 warning（§5 硬约束 5「宁缺勿造」）。
"""

from __future__ import annotations

from typing import Any

import logging

from sqlalchemy import select

from backend.app.core.database import SessionLocal
from backend.app.models import LlmGatewayConfig, LlmGatewayUsageLog
from backend.app.services.sqlite_retry import retry_on_lock

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 功能常量（设计 17 §3.11 / §4.3.1）
# ⚠️ 必须与 `llm_gateway_tokens.function` 逐字一致；调用点**不许就地写字面量**，
#    一律用这里的常量，否则令牌映射查不中（一个错字 = 台账 token_name 恒空）。
# --------------------------------------------------------------------------

FUNCTION_REWRITE = "rewrite"
FUNCTION_GENERATE = "generate"
FUNCTION_TITLE = "title"
FUNCTION_TAG = "tag"
FUNCTION_POLISH = "polish"
FUNCTION_DESCRIBE = "describe"
#: v2.9：模型类型归纳（拉取目录后由 AI 判 text/image/multimodal/video）
FUNCTION_CLASSIFY = "classify"

#: 状态取值（台账 `status` 列）
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"


def _normalize_base_url(base_url: str | None) -> str:
    """归一化 base_url 供反查。

    只做「去首尾空白 + 去末尾斜杠」—— 与 `ai_service` 拼 endpoint 的
    `base_url.rstrip('/')` **同口径**：凡能拼出同一 endpoint 的两个 base_url，
    在这里必须归一到同一个键，否则反查必然漏掉（设计 17 §4.3.1）。
    """
    return (base_url or "").strip().rstrip("/")


def resolve_gateway_id_for_model(model_config: Any) -> int | None:
    """按 `model_config.provider_key` 解析服务商 id；未命中/为空返回 `None`（v1.9）。

    ⚠️ **v1.9 取代 v1.8 的 `resolve_gateway_id(user_id, base_url)`**（用户口径
    「其它业务代码中将直接使用该服务商 key 直接调用」；决策 D6）：
    旧实现按 `base_url` **字符串归一化比对**，本质是「配置长得像就算命中」——
    用户改一个字符（补个 `/`、换个域名的写法）就静默不再记账。
    `provider_key` 是**显式契约**：业务引用它，不随地址写法漂移。

    ⚠️ **不做 base_url 兜底回落**（D6）：回落 = 两条真相源，改错了不报错、
    只静默用旧凭据（DEF-048 同型）。`provider_key` 为空 ⇒ 直接返回 `None`。

    ⚠️ **必须带 `user_id` 约束**：不同用户可以配同名 `provider_key`（唯一索引是
    `(user_id, provider_key)`），只按 key 查会串号（把 A 的调用记到 B 的台账上）。
    """
    user_id = model_config.user_id
    provider_key = (model_config.provider_key or "").strip()
    if not user_id or not provider_key:
        return None

    db = SessionLocal()
    try:
        row = db.scalars(
            select(LlmGatewayConfig.id).where(
                LlmGatewayConfig.user_id == user_id,
                LlmGatewayConfig.provider_key == provider_key,
            )
        ).first()
    except Exception:  # noqa: BLE001 —— 解析失败不得影响业务调用
        logger.warning(
            "LLM 台账解析服务商失败（user_id=%s, provider_key=%s）",
            user_id, provider_key, exc_info=True,
        )
        return None
    finally:
        db.close()

    return int(row) if row is not None else None


def extract_usage_tokens(payload: object) -> tuple[int, int]:
    """从上游响应里读 token 用量，返回 `(prompt_tokens, completion_tokens)`。

    缺字段或非法值一律记 `0`（§5 硬约束 5「缺值不造假」）——
    注意是**记 0 而不是记 None**：台账两列 `nullable=False`，且用量的
    "上游没报" 与 "真的 0" 在统计上同为无消耗，前端按 0 参与求和。
    """
    if not isinstance(payload, dict):
        return 0, 0
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0, 0

    def _as_int(value: object) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value if value > 0 else 0
        if isinstance(value, float):
            return int(value) if value > 0 else 0
        return 0

    return _as_int(usage.get("prompt_tokens")), _as_int(usage.get("completion_tokens"))


def record_usage(
    *,
    user_id: int,
    gateway_id: int,
    platform: str,
    function: str,
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    status: str,
    error_message: str = "",
) -> bool:
    """台账唯一 INSERT 点（设计 17 §4.3.1）。

    返回是否写入成功；**从不抛异常** —— 记账失败只 warning，
    绝不让一次已成功的业务调用因为台账问题而失败（DEF-048）。

    ⚠️ **v1.9**：`token_name` **恒落空串** —— 令牌映射（`llm_gateway_tokens`）已整表退场
    （设计 17 §7.1a），不再有映射可查。列保留（历史行审计），只是不再写入。

    ⚠️ **撞锁重试（DEF-085）**：本函数**另开一条连接**，而它总是在业务主会话
    尚未 `commit` 时被调用（`ai_service._complete` 在 action 内记账）—— 业务会话
    持有的写锁与本 INSERT 直接冲突。常驻服务下定时任务 / 同步 worker 并发写时
    `database is locked` 频繁出现，一次失败即**静默丢台账** —— 而台账是
    **唯一用量真相源**（用量看板 / 预警全依赖它），静默丢数据比业务失败更难发现。

    ⇒ 撞锁**重试**（指数退避）；只有重试用尽才 warning + 返回 False。
    **不得**改成「抛出去让业务失败」—— 记账是旁路，业务语义不受影响（DEF-048 不变）。
    """
    def _insert_once() -> None:
        db = SessionLocal()
        try:
            db.add(
                LlmGatewayUsageLog(
                    user_id=user_id,
                    gateway_id=gateway_id,
                    platform=platform or "",
                    function=function or "",
                    # v1.9：令牌映射退场 ⇒ 恒空（列保留，见 docstring）
                    token_name="",
                    model_name=model_name or "",
                    prompt_tokens=int(prompt_tokens or 0),
                    completion_tokens=int(completion_tokens or 0),
                    latency_ms=int(latency_ms or 0),
                    status=status or STATUS_SUCCESS,
                    error_message=error_message or "",
                )
            )
            db.commit()
        finally:
            db.close()

    try:
        # 撞锁重试走**共用实现**（`sqlite_retry.retry_on_lock`）——
        # 判据与退避只在那一处，本项目已有三处同型链路（台账 + 两个快照采集）。
        retry_on_lock(
            _insert_once,
            what=f"LLM 台账写入（user_id={user_id}, gateway_id={gateway_id}）",
        )
        return True
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：安全网不得抛穿
        logger.warning(
            "LLM 台账写入失败（user_id=%s, gateway_id=%s, %s/%s）：%s",
            user_id, gateway_id, platform, function, exc, exc_info=True,
        )
        return False


def record_usage_for_model(
    *,
    model_config: Any,
    platform: str,
    function: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    status: str,
    error_message: str = "",
) -> bool:
    """`ai_service` 专用入口：由模型上下文解析服务商后委托 `record_usage()`。

    ⚠️ **v2.0**：`model_config` 参数的载体从 `ModelConfig`（ORM 行，表已 drop）
    换成 `llm_gateway_client.ResolvedModelContext`（纯 dataclass）。本函数只读
    `user_id` / `provider_key` / `model_name` 三属性 —— 载体换掉，**本函数零改动**
    （`resolve_gateway_id_for_model` 同理，见它的 docstring）。

    ⚠️ **v1.9**：解析键由 `base_url` 改为 `provider_key`（见
    `resolve_gateway_id_for_model`）。`provider_key` 为空 ⇒ **跳过记账**：
    宁可少一条台账，也不给台账塞 `gateway_id` 来源不明的行（`gateway_id` 是外键，
    塞错就是脏数据）。与 v1.8 的「反查不中就不记账」行为等价。
    """
    try:
        user_id = model_config.user_id
        gateway_id = resolve_gateway_id_for_model(model_config)
        if gateway_id is None:
            # ⚠️ v2.0：映射路径下 `provider_key` 恒非空（`resolve_business_provider`
            # 拿它从 `llm_business_providers` 命中才有上下文），此分支实际只服务
            # 「载体是老 `ModelConfig` 且未指向服务商」的历史路径 —— 为稳健保留。
            logger.warning(
                "LLM 台账跳过：模型上下文未指向任何服务商（user_id=%s, provider_key=%r）",
                user_id, getattr(model_config, "provider_key", None),
            )
            return False
        return record_usage(
            user_id=user_id,
            gateway_id=gateway_id,
            platform=platform,
            function=function,
            model_name=model_config.model_name or "",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            status=status,
            error_message=error_message,
        )
    except Exception:  # noqa: BLE001
        logger.warning("LLM 台账记账失败（model_config=%s）", getattr(model_config, "id", None), exc_info=True)
        return False
