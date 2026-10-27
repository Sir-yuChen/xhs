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

    ⚠️ **业务不同 ⇒ 模型不同是硬需求**（17 文档 §4.4）：~~`default_model` 只作兜底默认，
    每个生成任务按 task_type 显式指定 model_name~~ ——
    ⚠️ **v2.8（2026-10-02 用户口径「把默认模型移除掉」）**：`default_model` 列已删除，
    连兜底名也不再存在。业务点映射**必须显式选模型**（v2.3 起即如此），
    模型的真相源 = `llm_gateway_models`（模型行）。

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
    #: 上游 OpenAI 兼容 base_url（三方服务商或自托管网关；不带末尾斜杠）
    #:
    #: ⚠️ **v2.7：`platform` 已从本表移除**（下沉到 `llm_gateway_models.platform`）——
    #: 「使用平台」是**模型**的属性（同一服务商可有不同平台的模型），不是服务商的。
    #: ⚠️ **`platform` 语义有两个同名兄弟，不许混淆**：本表原列 = 服务商适用范围（已删）；
    #: `llm_gateway_usage_logs.platform` = 该次调用的业务上下文（动态，保留）；
    #: `llm_gateway_models.platform` = 该模型的适用范围（静态，v2.7 新家）。
    base_url: Mapped[str] = mapped_column(String(512), nullable=False, default="http://127.0.0.1:3000")
    #: 上游访问令牌（服务商 sk- 令牌；`encrypt_text` 加密落库）。
    #: ⚠️ **v1.9 起新建必填**（用户口径「访问令牌必填」）—— 列层仍允许空串，
    #: 是为了让迁移能在「列已加、回填未完」的中间态重跑（幂等纪律），
    #: 约束落在 API 层（`create_gateway_config` ⇒ 400 `LLM_PROVIDER_TOKEN_REQUIRED`）。
    access_token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: ⚠️ **废弃**（v1.6）：旧「New API 本地 SQLite 路径」，统计已改读 llm_usage_logs
    #: 本地台账；列保留不删（历史配置不动），新代码不得读写
    local_db_path: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    #: 生成默认模型名 —— ⚠️ **v2.8 已删除本列**（用户口径 2026-10-02「把默认模型移除掉」）。
    #: 原语义「该服务商没指定模型时的兜底名」在**v2.3 已被应用层废除**
    #: （业务点映射**必须显式选模型**、`resolve_business_provider` 不回落 `default_model`）
    #: ⇒ 本列自 v2.3 起**没有任何读点**，成死字段；v2.8 从 ORM 与 DB **彻底移除**。
    #: 「某服务商有哪些模型」的真相源 = `llm_gateway_models`（模型行），不再有兜底名。
    #:
    #: 模型目录（test 端点从 `{base_url}/models` 拉取后持久化的 JSON 字符串数组；v1.6）
    #:
    #: ⚠️ **v2.7：本列仍只是「上游回报的候选池」** —— 与模型层（`llm_gateway_models`）的分工：
    #: 本列 = 上游 `/models` 拉到什么（字符串数组，供下拉候选）；
    #: 模型表 = 用户**显式配置**过哪些模型（含 `model_type` / `platform`）。
    #: **目录里未被配置的模型不建行**（用户没表态 ⇒ 不给它猜类型，同 §13.2.9.4 纪律）。
    models_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    #: ⚠️ **v2.7：`model_type` 已从本表移除**（下沉到 `llm_gateway_models.model_type`）。
    #: 原语义（v2.0 §13.2.1a）「一条服务商 = 一种能力，同一上游要供两种能力 ⇒ 建两条」**已被取代**：
    #: 服务商唯一，能力类型由**模型行**承载。详见设计 17 §13.2.9。
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: 最近一次健康探测结果（test 端点写入）
    healthy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_health_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now, onupdate=shanghai_now)


class LlmGatewayModel(Base):
    """服务商下的**具体模型**（模型层，设计 17 §13.2.9，v2.7 新增）。

    ## 为什么需要这一层

    用户口径（2026-10-02 原文）：「服务商信息是其下的模型**公用的**比如服务商接口 key、
    调用地址是公共的，**具体模型才需要分文本，多模态，图片，视频类似等等**。」

    ⇒ 服务商层只留**公共凭据**（`base_url` + `access_token` + 名称 + 健康 + 目录）；
    **能力类型**（`model_type`）与**使用平台**（`platform`）下沉到本表 ——
    它们是**模型**的属性，不是服务商的。

    ⚠️ **取代 v2.0 §13.2.1a「一条服务商 = 一种能力，同一上游要供两种能力 ⇒ 建两条」**：
    那个形态的代价是**凭据重复填**（同 base_url / 同令牌写两行，改一处漏一处）
    且服务商列表失真（一上游拆成多条）。见 §13.2.9.2 的实测案例。

    ## 与 `models_json` 的分工（**不许合并**）

    | 载体 | 来源 | 内容 |
    |---|---|---|
    | `LlmGatewayConfig.models_json` | **上游回报**（`GET {base_url}/models`） | 候选池（字符串数组） |
    | **本表** | **用户声明** | 用户**显式配置**过的模型行（含类型 / 平台 / 启停） |

    目录里**未被配置**的模型**不建行** —— 用户没表态就**不猜类型**
    （同 §13.2.1a「判不准时回 `text`，绝不猜 `image`」的纪律，只是现在连行都不建）。

    ## ⚠️ 唯一约束 `(user_id, provider_key, model_name)`

    同一服务商下同一模型名只能一行。`provider_key` **不做 DB 外键** ——
    与 `LlmBusinessProvider.provider_key` 同理：本项目迁移风格为**裸列 + 应用层归属校验**，
    且服务商**不可删除**（v1.9 已改 410 墓碑）⇒ 引用不会悬空。
    """

    __tablename__ = "llm_gateway_models"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "provider_key", "model_name",
            name="uq_llm_gateway_models_user_provider_model",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    #: 指向 `llm_gateway_configs.provider_key`（裸列，应用层校验归属与存在性）
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: **人类可读唯一 ID**（v2.10 新增，设计 17 §13.2.12）。
    #: 用户口径（2026-10-02）：「模型也是跟服务商一样需要有一个人为可读的**唯一ID**，
    #: 比如 1，2，3 的自然序号」。
    #: ⚠️ **服务商内**自然序号（每个服务商都从 `1` 开始），取值 = 该服务商内最大值 + 1。
    #: ⚠️ **与主键 `id` 不可互相替代**：`id` 是 DB 全局自增（跨服务商连续），用户看到
    #: 「第 37 号模型」没有意义；本列才是用户能口头/文档指代的编号。
    #: 存字符串（同 `provider_key` 的「编码」语义），比较时按需转 int。
    model_key: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    #: 模型名（上游 `/models` 目录里的名字，如 `deepseek-v4-flash-0731`）
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    #: **能力类型**（v2.7 下沉自服务商层）：`text` / `image` / `multimodal` / `video`
    #: （枚举源 `MODEL_TYPES`，v2.9 扩为四值）。
    #: ⚠️ **用户声明，不是上游回报** —— 上游 `/models` 不返回能力类型，必须由用户/本系统标注。
    #: ⚠️ **v2.9 起可由 AI 批量归纳**（§13.2.11）：同步服务商模型时一次 LLM 调用判定整批；
    #: 归纳不出 ⇒ 回 `text`（安全默认，同 §13.2.1a），**绝不猜 `image`**。
    model_type: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    #: **使用平台**（v2.7 下沉自服务商层；**v2.9 由单值改为多值**）：该模型服务哪些业务平台。
    #: ⚠️ **列形态 = JSON 数组字符串**（如 `["xhs","douyin"]`），**不是**逗号分隔：
    #: 平台码里不含逗号但可能含其它符号，JSON 是唯一不会被值内容歧义的编码。
    #: 解析/落库统一走 `api/llm_gateway._parse_platforms` / `_dump_platforms`（**唯一收敛点**）。
    #: ⚠️ **多平台共用**（用户 2026-10-02 口径「**支持多个平台公共一个服务商下的一个具体模型**」）：
    #: 一个模型可同时服务多个平台，故必须多值 —— 旧单值形态**已被取代**（§13.2.11.3）。
    #: ⚠️ 写入时机 = **用户在某个平台上下文里配置该模型时把当前平台并入**（§13.2.9.7 的
    #: 「配置时同步」扩展为「配置时**并入**」）：在第二个平台配同一个模型 = **追加**，不是挤掉。
    #: ⚠️ **不与服务商联动**（服务商已无 platform 列）—— 平台是模型的属性，各自表态、不继承。
    platform: Mapped[str] = mapped_column(String(255), nullable=False, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: 备注（用户自填，可选）
    remark: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    #: **最近一次逐模型健康探测结果**（v2.10 新增，设计 17 §13.2.12）。
    #: 用户口径（2026-10-02）：「服务商列表的健康，启用开关，**在模型维度展示**」——
    #: 健康描述的是「这个模型还能不能用」，不是「这家服务商整体如何」：
    #: 同一服务商下完全可能「文本模型正常、图片模型挂了」。
    #: ⚠️ **判据 = `last_health_at is not None AND healthy`**（与 `configs.healthy` 同形态）：
    #: 只看 `healthy` 会把「从未探测」误报成「不健康」（`default=False` 的老坑，DEF-067 同类）。
    healthy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 最近一次探测时间；**`NULL` = 从未探测**（迁移不回填 —— 历史行确实没被逐模型探过，
    #: 回填成「已探测」就是造假，同缺值口径）。
    last_health_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now, onupdate=shanghai_now)


class LlmGatewayAlert(Base):
    """网关用量/健康预警规则（设计 17 §3.12，v1.6 新增）。

    语义：监控任务 `system.llm_gateway_monitor` 轮询本地台账，对照规则判断，
    超阈值 → 写 `notifications` 表（站内通知）。规则分：用量阈值 / 失败率阈值 / 网关健康。
    `platform`/`function` 为空 = 该维度全量（空 platform = 该服务商全平台）。
    `last_fired_at` 用于同日去重防抖（同规则同日只发一次）。

    ## ⚠️ 三级作用域（v2.3，2026-10-02 用户口径「预警规则挂可以控制到具体模型维度，
    也可以服务商维度或全网关」）

    | 作用域 | `gateway_id` | `model_name` | 含义 |
    |---|---|---|---|
    | **全网关** | `NULL` | 必须 `""` | 该用户**名下所有服务商**分别判（逐个判，见监控服务） |
    | **服务商** | 具体 id | `""` | 该服务商**全部模型** |
    | **具体模型** | 具体 id | 非空 | 该服务商下**某一个模型** |

    ⚠️ **`gateway_id` 可空是「全网关」的表达**（`NULL` = 不绑单个服务商）——
    刻意不用哨兵值（`0` 之类）：哨兵在 JOIN / `db.get()` 上语义不明，且一眼看不出是魔法值。
    ⚠️ **`model_name` 非空时 `gateway_id` 必须非空**（全网关下谈「某个模型」无意义）——
    该互斥由 API 层校验，DB 层只保证两列各自形态。
    """

    __tablename__ = "llm_gateway_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    #: ⚠️ **可空**（v2.3）：`NULL` = 全网关（不绑单个服务商，遍历该用户所有服务商逐个判）。
    gateway_id: Mapped[Optional[int]] = mapped_column(ForeignKey("llm_gateway_configs.id"), index=True, nullable=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    function: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    #: ⚠️ **v2.3 新增**：具体模型名（`""` = 该服务商全模型）。
    #: 非空 = 只对台账里 `model_name` 等于本值的调用算指标（数据源 `llm_gateway_usage_logs.model_name` 早有）。
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
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

    ## ⚠️ v2.3：**必须同时选定「服务商 + 具体模型」**（2026-10-02 用户口径
    「两者都应是必须选择，不指定具体的模型无法调用成功」）

    - `provider_key` = 服务商（映射到 `llm_gateway_configs`）；
    - `model_name` = **该服务商下的某个具体模型**（必须非空）。

    两者都必填 —— 只给服务商不指定模型，`resolve_business_provider` 会抛
    `LLM_BUSINESS_MODEL_REQUIRED`，**刻意不回落 `default_model`**：
    回落 = 「看起来正常但这个业务点实际在用别的模型」，与「未配服务商不回落」同型的事故。
    """

    __tablename__ = "llm_business_providers"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "platform", "business_key", "kind",
            name="uq_llm_business_providers_user_platform_business_key_kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    #: 业务平台（与 `llm_gateway_configs.platform` / §3.8 的 `PLATFORM_ENUM` 同源）
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="xhs")
    #: 业务点固定 key（`BUSINESS_KEY_*` 常量之一，用户不可改）
    business_key: Mapped[str] = mapped_column(String(48), nullable=False)
    #: **这一行服务的能力类型**（`text` / `image` / `video` / `multimodal`）。
    #:
    #: 用户口径 2026-10-18：
    #: > 「一个业务点支持用户配置**多个类型**模型，**每个类型只能配置一个**…
    #: >   该业务点必须配置一个文本模型，一个图片模型，一个视频模型，
    #: >   用户可以手动添加不同类型的模型，但是**相同类型的模型不允许重复添加**，
    #: >   同一个类型的模型支持切换」
    #:
    #: ⇒ 一个业务点 = N 行（每类型一行），唯一键随之扩为
    #: `(user_id, platform, business_key, kind)`。
    #: ⚠️ 为什么允许「一行 kind=`text`」之后再插 `image`：`generate_note` 一次要
    #: 写文案 + 配图 + 出视频，三类缺一不可（见 `BusinessKey.needs`）。
    #:
    #: 存量行由迁移回填为**该业务点自身的** `kind`（`BusinessKey.kind`），
    #: 语义与改造前完全一致 —— 老配置照旧生效，不会因为多了这一列而失效。
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    #: 指向 `llm_gateway_configs.provider_key`（裸列，应用层校验归属与类型匹配）
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: ⚠️ **v2.3 新增**：业务点显式选定的**具体模型名**（必须落在该服务商的模型目录里）。
    #: 空串 = 未配置（`resolve_business_provider` 会抛 `LLM_BUSINESS_MODEL_REQUIRED`）。
    #: 存量行由迁移回填为该服务商当时的 `default_model`（只回填一次，之后强制显式）。
    #: ⚠️ **v2.8**：`default_model` 列已删除 ⇒ 回填来源不复存在，但**本列语义不变**
    #: （业务点必须显式选模型）；存量已在 v2.7 迁移里回填完毕。
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now, onupdate=shanghai_now)