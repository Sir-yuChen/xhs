from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


class LlmGatewayConfig(Base):
    """OpenAI 兼容网关/服务商接入配置（设计 17 §3.8，v1.3 定稿；v1.6 通用化；v1.8 base_url 语义定稿）。

    语义：项目侧对一个 **OpenAI 兼容上游**（三方服务商 DeepSeek/月之暗面/SiliconFlow/
    中转站，或自托管 New API/OneAPI）的**单行连接配置**（base_url + 访问令牌 +
    默认模型 + 健康状态 + 模型目录）。一切上游统一按 OpenAI 兼容协议接入
    （`GET {base_url}/models` / `POST {base_url}/chat/completions`），不绑定 New API 特有接口。

    ⚠️ **v1.8（设计 17 §7.1.0）**：~~`GET {base}/v1/models` / `POST {base}/v1/chat/completions`~~
    —— `base_url` = **模型调用地址本身**，`/v1` **不由后端硬拼**，由用户地址自带
    （填 `https://api.deepseek.com/v1` ⇒ 实际 `…/v1/models`）。

    ⚠️ `access_token` 用 `encrypt_text` 加密落库（🔒），响应只给 `has_access_token`
    布尔，**永不回传明文**（§10 安全设计）。

    ⚠️ **业务不同 ⇒ 模型不同是硬需求**（17 文档 §4.4）：`default_model` 只作兜底默认，
    每个生成任务按 task_type 显式指定 model_name；本表不承载「单一模型一条道走到黑」。

    ⚠️ v1.6：`models_json` = test 端点从 `{base_url}/models` 拉到的模型目录（JSON 数组，
    持久化 ⇒ 前端默认模型下拉由目录驱动）；`local_db_path` **废弃**（列保留不删，
    v1.6 起统计改读 `llm_usage_logs` 本地台账，与 base_url 是否本机无关）。
    """

    __tablename__ = "llm_gateway_configs"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_llm_gateway_configs_user_name"),
        #: v1.9（设计 17 §3.8）：服务商唯一 KEY —— 业务代码按它引用服务商（§4.3）。
        #: 两道防线之 DB 层（应用层生成后查重见 `api/llm_gateway.py`）。
        UniqueConstraint(
            "user_id", "provider_key",
            name="uq_llm_gateway_configs_user_provider_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    #: **服务商唯一 KEY**（v1.9）。⚠️ **系统生成、创建后不可编辑**（D1）：
    #: 格式 `{slug(name)}-{4位随机}`（字母表去 `0/O/1/I/L`，同 §35.13 批次号纪律）。
    #: 业务代码（`resolve_provider_context`）按它取服务商 —— 不再按 `base_url` 反查（D6）。
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: **使用平台**（v1.9，D3）：该服务商服务哪个业务平台（枚举，后端下发，见 `PLATFORM_ENUM`）。
    #: ⚠️ **与台账 `LlmGatewayUsageLog.platform` 不是同一个东西，不许合并**：
    #: 本列 = 服务商适用范围（静态配置）；台账 = 该次调用的业务上下文（动态）。
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="xhs")
    #: 上游 OpenAI 兼容 base_url（三方服务商或自托管网关；不带末尾斜杠）
    base_url: Mapped[str] = mapped_column(String(512), nullable=False, default="http://127.0.0.1:3000")
    #: 上游访问令牌（服务商 sk- 令牌；`encrypt_text` 加密落库）。
    #: ⚠️ **v1.9 起新建必填**（用户口径「访问令牌必填」）—— 列层仍允许空串，
    #: 是为了让迁移能在「列已加、回填未完」的中间态重跑（幂等纪律），
    #: 约束落在 API 层（`create_gateway_config` ⇒ 400 `LLM_PROVIDER_TOKEN_REQUIRED`）。
    access_token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: ⚠️ **废弃**（v1.6）：旧「New API 本地 SQLite 路径」，统计已改读 llm_usage_logs
    #: 本地台账；列保留不删（历史配置不动），新代码不得读写
    local_db_path: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    #: 生成默认模型名（模型目录中的名字；仅兜底，见类注释 ⚠️）
    default_model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    #: 模型目录（test 端点从 /v1/models 拉取后持久化的 JSON 数组；v1.6）
    models_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    #: **服务商的模型类型**（v2.0，设计 17 §13.2.1a）：`text` / `image`。
    #:
    #: 语义：**一条服务商 = 一种能力**。同一上游要供两种能力 ⇒ **建两条**
    #: （同 `base_url`、可同令牌；`name` 与 `provider_key` 各不相同）。
    #: 页面按类型分组展示，用户看得清。
    #:
    #: ⚠️ **用户声明，不是上游回报**：本列是「这条服务商拿来干什么」，
    #: `models_json` 是上游拉到什么。以声明为准（用户知道自己的中转站开了什么）。
    #: ⚠️ **v2.0 存量回填一律 `text`**（判不准时也回 `text` + 让用户到页面确认），
    #: **绝不猜 `image`** —— 猜错会让用户在「模型配置页」看到一条假的图像服务商。
    model_type: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: 最近一次健康探测结果（test 端点写入）
    healthy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_health_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now, onupdate=shanghai_now)


class LlmGatewaySnapshot(Base):
    """New API 用量快照（设计 17 §3.9，v1.3 定稿；⚠️ v1.6 废弃写入）。

    语义：~~New API 数据看板 + `/api/log/stat` 统计 API 承载实时用量；项目侧**按日快照**
    缓存到本地，供 XHS 前端「大模型用量」页展示~~。
    ⚠️ **v1.6 取代（用户口径「两个数据库合并」）**：用量真相源改为本地
    `LlmGatewayUsageLog` 台账，本表**不再写入**（历史行保留，仅向前兼容）。

    唯一键 `(user_id, gateway_id, stat_date)`：每网关每日一行，upsert 幂等。
    """

    __tablename__ = "llm_gateway_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "gateway_id", "stat_date",
            name="uq_llm_gateway_snapshots_user_gateway_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    gateway_id: Mapped[int] = mapped_column(ForeignKey("llm_gateway_configs.id"), index=True, nullable=False)
    stat_date: Mapped[date] = mapped_column(Date, nullable=False)
    call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 当日配额消耗（New API quota 单位）
    total_quota: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="newapi")
    #: 网关统计 API 原文快照（证据链）
    raw_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now, onupdate=shanghai_now)


class LlmGatewayAlert(Base):
    """网关用量/健康预警规则（设计 17 §3.12，v1.6 新增）。

    语义：监控任务 `system.llm_gateway_monitor` 轮询本地台账，对照规则判断，
    超阈值 → 写 `notifications` 表（站内通知）。规则分：用量阈值 / 失败率阈值 / 网关健康。
    `platform`/`function` 为空 = 该维度全量（空 platform = 该网关全平台）。
    `last_fired_at` 用于同日去重防抖（同规则同日只发一次）。
    """

    __tablename__ = "llm_gateway_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    gateway_id: Mapped[int] = mapped_column(ForeignKey("llm_gateway_configs.id"), index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    function: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    #: 指标：quota（未在台账，用 calls 代替）/ calls / fail_rate / health
    metric: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 操作符：gt / gte / lt / eq（health 用 eq）
    operator: Mapped[str] = mapped_column(String(8), nullable=False, default="gt")
    #: 阈值（calls 绝对值 / fail_rate 0–1 / health 0=down）
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    #: 统计窗口（秒；默认 86400 = 24h）
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=86400)
    #: 通知级别（info / warning / error）
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="warning")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_fired_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now, onupdate=shanghai_now)


class LlmGatewayUsageLog(Base):
    """本地 LLM 调用台账（设计 17 §3.13，v1.6 新增）。

    语义：**项目侧每次经网关发起的 LLM 调用记一行**（成功或失败都记），是**唯一用量
    真相源**（用户口径「两个数据库合并 —— 数据层全归主库」）。用量展示 / 预警都聚合
    本表，与 New API logs 彻底解耦（任意 base_url 均支持）。
    `platform`/`function`/`token_name` 从令牌映射命中的结果快照（映射后改也保留审计）。
    """

    __tablename__ = "llm_gateway_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    gateway_id: Mapped[int] = mapped_column(ForeignKey("llm_gateway_configs.id"), index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    function: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    token_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="success")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now, index=True)


class LlmBusinessProvider(Base):
    """「平台 × 业务点」→ 服务商的映射（设计 17 §13.2.3，v2.0 新增）。

    语义：**映射层** —— 用户在「模型配置页」（原模型配置页改造）为某个平台的某个
    业务点选一个服务商；业务代码用**固定 key**（`services/business_provider_keys.py`
    的常量）查本表 → 取服务商凭据调用（§13.2.4 调用链）。

    ⚠️ **唯一约束 `(user_id, platform, business_key)`**：同用户同平台同业务点只能
    指向一个服务商 —— 「实时切换」= **改这一行**，不是并存多条（用户口径
    「不同平台和不同的业务可以实时切换服务商」）。

    ⚠️ `provider_key` **不做 DB 外键**（与 `LlmGatewayConfig` 同理：本项目迁移风格
    为裸列 + 应用层归属/引用校验），且服务商**不可删除**（v1.9 已改 410 墓碑）⇒
    引用不会悬空。M2 的 `resolve_business_provider` 负责解析 + 类型匹配校验。

    ⚠️ **未命中映射不回落**：抛 `LLM_BUSINESS_PROVIDER_NOT_CONFIGURED`
    （「该业务点还没配服务商，请到模型配置页选择」）。回落 = 用错服务商而不报错，
    是最难查的形态（同 D4「停用不静默回落」）。
    """

    __tablename__ = "llm_business_providers"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "platform", "business_key",
            name="uq_llm_business_providers_user_platform_business_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    #: 业务平台（与 `llm_gateway_configs.platform` / §3.8 的 `PLATFORM_ENUM` 同源）
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="xhs")
    #: 业务点固定 key（`BUSINESS_KEY_*` 常量之一，用户不可改）
    business_key: Mapped[str] = mapped_column(String(48), nullable=False)
    #: 指向 `llm_gateway_configs.provider_key`（裸列，应用层校验归属与类型匹配）
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now, onupdate=shanghai_now)