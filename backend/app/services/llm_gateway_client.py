"""LLM 网关薄客户端（设计 17 §4.3，v1.7）。

## 这个模块为什么存在

v1.7 用户口径「**去掉外挂进程 / New API**：项目内建薄客户端直连任意
OpenAI 兼容端点」。于是「调用上游」这件事在本项目里**已经有一处实现** ——
`ai_service.OpenAICompatibleTextClient._complete()`，它是文本链路唯一汇聚点，
且台账记账（设计 17 §4.3.1）就挂在它身上：一次上游调用 ⇒ 一行台账。

⇒ 本模块**不写第二套 POST**（那是 DEF-044「同一语义不留两份实现」）。
它做的只有三件事：

1. **模型上下文解析**：按「平台 × 业务点」映射到服务商，取 base_url / 模型名 /
   解密后的 api_key（**v2.0 唯一入口** `resolve_business_provider`，设计 17 §13.2.4）；
2. **Prompt 渲染**：调 `prompt_renderer.render_prompt`（§5.1 唯一实现）；
3. **转交汇聚点**：带 `platform` / `function` 常量调 `_complete()`，
   让记账自动发生。

## 两条纪律

* **`platform` / `function` 必须用常量**（`FUNCTION_GENERATE` 等 + `PLATFORM_XHS`）：
  漏传**不报错**、静默落空桶，错误方式是「看起来一切正常但用量报表少一截」
  —— 比报错难查得多。
  ⚠️ **v1.9 订正**：本条原写作「查不到 `llm_gateway_tokens` 映射 ⇒ 台账 token_name 落空串」。
  令牌映射已整表退场（设计 17 §7.1a），`token_name` **现恒为空**；
  但 `platform` / `function` **仍是台账的聚合维度**（看板切片 + 预警定域），纪律本身不变。
* **参数校验失败不记账**：由 `_complete()` 自己保证（缺 base_url / model_name /
  api_key 时直接 `ValueError`，尚未发起上游调用）。本模块不重复记账，
  也不吞异常。
* **服务商解析失败要「响」**（v1.9）：`resolve_provider_context()` 在
  「服务商不存在」「服务商已停用」两种情况下**抛异常**，**绝不静默回落**
  到其它服务商或该模型配置自身的凭据（决策 D4）—— 回落会让台账把 A 的调用
  记在 B 名下，且用户以为停用生效了。
"""
from __future__ import annotations

from typing import Any

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models.llm_gateway import LlmGatewayConfig
from backend.app.models.user import User
from backend.app.services.ai_service import OpenAICompatibleTextClient

logger = logging.getLogger(__name__)

#: 平台标识（设计 17 §4.3 台账维度）。
#: ⚠️ 与 `llm_gateway_usage_service` 的令牌两级映射同口径 —— 该模块按
#: `(platform, function)` 反查 `token_name`，字面量写错 ⇒ 查不到令牌
#: ⇒ 台账 `token_name` 落空串（不报错，静默降级）。
PLATFORM_XHS = "xhs"

#: 文本模型的类型取值（与 `business_provider_keys.MODEL_TYPE_TEXT` 同值）。
#: ⚠️ v2.0 后只用于类型比对，**不再用于查 `model_configs`**（该表已 drop）。
MODEL_TYPE_TEXT = "text"


class LLMGatewayError(RuntimeError):
    """薄客户端错误基类：`code` 给调用方做结构化错误映射，`message` 给人看。"""

    def __init__(self, message: str, *, code: str = "LLM_GATEWAY_CALL_FAILED") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class BusinessProviderNotConfiguredError(LLMGatewayError):
    """该「平台 × 业务点」还没配服务商映射（`llm_business_providers` 无行）。

    ⚠️ **刻意不回落**任何其它服务商或其它业务点的映射（§13.2.3 定稿）：
    回落 = 用错了服务商还不报错，是「看起来正常但数据是错的」最难查的形态。
    正确的出路是让用户去「模型配置页」为这个业务点选一个服务商。
    """

    def __init__(self, business_key: str = "", platform: str = PLATFORM_XHS) -> None:
        super().__init__(
            f"该业务点还没配服务商（{platform}/{business_key}），请到模型配置页选择",
            code="LLM_BUSINESS_PROVIDER_NOT_CONFIGURED",
        )


class BusinessProviderTypeMismatchError(LLMGatewayError):
    """业务点的 `kind` 与**所选模型**的 `model_type` 不一致（§13.2.9.6，v2.7）。

    ⚠️ **v2.7 起判据从「服务商 `model_type`」改为「模型行 `model_type`」**：
    能力类型已下沉到模型层（服务商层无 `model_type` 列），
    §13.2.1a 原口径「业务点 kind = 服务商 model_type」**已被取代**。

    ⚠️ 这个校验必须**提前**做：把 `generate_cover`（图像类）指到文本模型，
    不校验的后果是调用时拿文本模型去打 `/images/generations`，
    报一个看不懂的上游错；拦在校验层，错误才有指向性。
    """

    def __init__(self, business_key: str = "", want: str = "", got: str = "", model_name: str = "") -> None:
        super().__init__(
            (
                f"「{business_key}」是{_kind_label(want)}类业务，"
                f"不能指向{_kind_label(got)}模型「{model_name}」"
                if model_name
                else f"「{business_key}」是{_kind_label(want)}类业务，不能指向{_kind_label(got)}模型"
            ),
            code="LLM_BUSINESS_PROVIDER_TYPE_MISMATCH",
        )


class BusinessModelNotConfiguredError(LLMGatewayError):
    """映射所选模型**在模型层没有行**（`llm_gateway_models` 查不到）—— §13.2.9.6，v2.7 新增。

    ⚠️ v2.3 只要求模型名落在 `models_json` 目录里；v2.7 还要求它在**模型层有类型声明**
    （否则无法判 `business_key.kind` 是否匹配）。目录是「上游回报的候选池」，
    模型层才是「用户声明过类型/平台」的地方 —— 二者**不许混用**（§13.2.9.4）。

    出路：让用户去「服务商配置页」为该模型标注能力类型（**不猜**，同不造假纪律）。
    """

    def __init__(self, business_key: str = "", model_name: str = "", platform: str = PLATFORM_XHS) -> None:
        super().__init__(
            (
                f"模型「{model_name}」还没标注能力类型（{platform}/{business_key}），"
                f"请先到服务商配置页配置该模型"
                if model_name
                else "所选模型还没标注能力类型，请先到服务商配置页配置该模型"
            ),
            code="LLM_BUSINESS_MODEL_NOT_CONFIGURED",
        )


class BusinessModelRequiredError(LLMGatewayError):
    """业务入口映射未指定**具体模型**（`llm_business_providers.model_name` 为空）。

    v2.3（2026-10-02 用户口径「两者都应是必须选择，不指定具体的模型无法调用成功」）：
    业务入口必须**同时**选定「服务商 + 该服务商下的某个具体模型」。

    ⚠️ **刻意不回落 `default_model`**（v2.3 之前的口径）：回落 = 「看着正常，但这个
    业务点实际在用服务商的兜底默认模型，而不是用户以为的那个」—— 与
    `BusinessProviderNotConfiguredError` 同型的事故形态（用错了还不报错）。
    正确的出路是让用户去「模型配置页」为该业务点选一个具体模型。
    """

    def __init__(self, business_key: str = "", platform: str = PLATFORM_XHS) -> None:
        super().__init__(
            f"该业务点还没选具体模型（{platform}/{business_key}），请到模型配置页选择服务商与模型",
            code="LLM_BUSINESS_MODEL_REQUIRED",
        )


def _kind_label(kind: str) -> str:
    """`text`/`image`/`multimodal`/`video` → 中文（错误文案用）。

    ⚠️ v2.9：改读 `business_provider_keys.MODEL_TYPE_LABELS`（**唯一映射点**）——
    本函数原写死 `{text:文本, image:图像}`，扩枚举后会静默把 `multimodal`/`video`
    渲染成英文原值（不报错但文案退化，DEF-044 同型）。
    未知值原样返回，**不造名** —— DEF-057。
    """
    from backend.app.services.business_provider_keys import MODEL_TYPE_LABELS

    return MODEL_TYPE_LABELS.get(kind or "", kind or "")


def _type_satisfies(*, kind: str, model_type: str) -> bool:
    """业务点能力需求 `kind` 是否被模型声明的 `model_type` 满足（§13.2.11.4，v2.9）。

    判据（**只有这一处**，调用层不许散写相等比较）：

    | 业务点 kind | 可被哪些 model_type 满足 |
    |---|---|
    | `text` | `text` ✅ · `multimodal` ✅（多态模型必然支持对话）· `image` ❌ · `video` ❌ |
    | `image` | `image` ✅ · `multimodal` ✅（多态模型可出图）· `text` ❌ · `video` ❌ |
    | `video` | `video` ✅（当前无此 kind 的业务点，先备着） |

    ⚠️ **为什么 `multimodal` 要放宽**：v2.9 前判据是 `==`（严格相等）。扩枚举后若仍用
    严格相等，用户在目录里把一个「多态」模型指到「改写笔记」（text 类）业务点会被拒 ——
    但多态模型打 `/chat/completions` 完全可用，拒绝是**误伤**（把能用的配不出）。
    ⚠️ **为什么 `image` 不认 `text`**：文本模型打 `/images/generations` 必失败，
    这类「方向性」不兼容必须拦住（否则报一个看不懂的上游错，§13.2.9.6 原口径）。
    ⚠️ 未知 `model_type`（不该发生，写入时已校验枚举）⇒ 判不满足，**不猜**（DEF-057）。
    """
    if not model_type:
        # 空类型 = 用户没标 ⇒ 上面已用 `model_model_type and ...` 短路，这里兜底不满足
        return False
    if model_type == kind:
        return True
    # 多态模型（multimodal）同时可服务 text 与 image 两类业务点
    if model_type == "multimodal" and kind in ("text", "image"):
        return True
    return False


class ProviderDisabledError(LLMGatewayError):
    """服务商存在但已**停用**（`enabled=False`）—— 决策 D4。

    ⚠️ **刻意不回落**其它服务商：回落会让台账把 A 的调用记在 B 名下，
    而用户以为「停用生效了」—— 「看起来一切正常但数据是错的」是最难查的形态。
    """

    def __init__(self, name: str = "") -> None:
        super().__init__(
            f"服务商「{name}」已停用" if name else "该服务商已停用",
            code="LLM_PROVIDER_DISABLED",
        )


class ProviderNotFoundError(LLMGatewayError):
    """`provider_key` 在本用户名下查不到（或模型配置压根没填 `provider_key`）。"""

    def __init__(self, provider_key: str = "") -> None:
        super().__init__(
            f"服务商不存在：{provider_key}" if provider_key else "模型配置未指向任何服务商",
            code="LLM_PROVIDER_NOT_FOUND",
        )


@dataclass(frozen=True)
class ProviderContext:
    """业务调用所需的**服务商上下文**（v1.9，设计 17 §4.3.1）。

    ⚠️ **v2.7：`platform` 字段移除**（设计 17 §13.2.9.7）——
    「使用平台」已从服务商层下沉到模型层，服务商上下文不再承载平台信息。
    调用方若确需平台，请读**模型行**的 `platform`（`resolve_business_provider` 内部按需读）；
    全局默认平台见 `PLATFORM_XHS`。

    ⚠️ **v2.8：`default_model` 字段移除**（设计 17 §13.2.10）—— 该列已删，
    服务商上下文不再承载任何模型名；模型真相源 = `llm_gateway_models` 模型行，
    由 `resolve_business_provider` 按业务点显式取。

    `api_key` 是**解密后**的明文令牌 —— ⚠️ **不得进日志、不得进响应体**
    （§10 安全设计：日志/响应一律 `[REDACTED]`）。
    """

    provider_id: int
    provider_key: str
    base_url: str
    api_key: str


def resolve_provider_context(db: Session, user_id: int, provider_key: str | None) -> ProviderContext:
    """按 `(user_id, provider_key)` 解析服务商 → 调用上下文（**v1.9 主路径**）。

    用户口径「其它业务代码中将直接使用该服务商 key 直接调用」（设计 17 §4.3.1）。

    三条硬规则（决策 D4/D6）：

    1. `provider_key` 为空 ⇒ `ProviderNotFoundError`（**不回落**到 base_url 反查）；
    2. 查不到 ⇒ `ProviderNotFoundError`（**不回落**到该模型配置自身的凭据）；
    3. 命中但 `enabled=False` ⇒ `ProviderDisabledError`（**不回落**到其它服务商）。

    三者都不静默降级 —— 「静默用旧凭据」是 DEF-048 同型的事故形态：
    改错了不报错，只在计费/台账上表现为「明明配了 A 却在用 B」。
    """
    from backend.app.core.security import decrypt_text

    key = (provider_key or "").strip()
    if not user_id or not key:
        raise ProviderNotFoundError(key)

    config = db.scalars(
        select(LlmGatewayConfig).where(
            LlmGatewayConfig.user_id == user_id,
            LlmGatewayConfig.provider_key == key,
        )
    ).first()
    if config is None:
        raise ProviderNotFoundError(key)
    if not config.enabled:
        raise ProviderDisabledError(config.name)

    return ProviderContext(
        provider_id=config.id,
        provider_key=config.provider_key,
        base_url=config.base_url,
        api_key=decrypt_text(config.access_token) if config.access_token else "",
    )


@dataclass(frozen=True)
class ResolvedModelContext:
    """业务调用所需的**模型上下文**（v2.0，设计 17 §13.2.4）。

    字段名刻意与 `ModelConfig` 对齐（`base_url` / `model_name` / `user_id` /
    `provider_key`）—— 它不是 ORM 行，而是「调用点要的那几个值」的**标量载体**：
    汇聚点 `ai_service`、记账链路 `resolve_gateway_id_for_model` / `record_usage_for_model`
    都按属性名读取，换掉载体即可解耦 `model_configs`，**两处零改动**。

    `api_key` 是**解密后**的明文令牌 —— ⚠️ **不得进日志、不得进响应体**
    （§10 安全设计：日志/响应一律 `[REDACTED]`）。

    `model_config_id` 仅用于**审计**（`task.payload` 里记「用了哪条配置」）；
    业务点映射路径下恒为 `None`（映射表没有「配置行」这个概念）。
    """

    base_url: str
    model_name: str
    user_id: int
    api_key: str
    provider_key: str = ""
    model_config_id: int | None = None

    @property
    def id(self) -> int | None:
        """`ModelConfig.id` 的**同名替身**（审计用）。

        ⚠️ 存在的唯一理由：调用点（`api/ai.py` 的 `task.payload["model_config_id"]`）
        原本读的是 `ModelConfig.id`。载体换掉后若不保留这个别名，
        8 个端点都得改一行；保留它则**调用点零改动**，风险面缩到最小。
        v2.0 走业务点映射时它是 `None`（映射表没有「配置行」），
        审计字段记 `null` 是**诚实的**（那条配置已退场，不该编一个 id 出来）。
        """
        return self.model_config_id


def resolve_business_provider(
    db: Session,
    user: User,
    business_key: "str | BusinessKey",
    platform: str = PLATFORM_XHS,
) -> ResolvedModelContext:
    """按「平台 × 业务点」解析到服务商 → 调用上下文（**v2.0 唯一入口**）。

    用户口径：「业务中的使用点映射成固定 key，不同平台和不同的业务可以实时切换服务商」
    （设计 17 §13.2）。

    ⚠️ **v2.3**：口径补完为「**服务商 + 具体模型**两者都必须选定，不指定具体模型
    无法调用成功」（2026-10-02）。故第 1.5 步新增「映射必须有 model_name」校验。

    ⚠️ **v2.7（§13.2.9.6）**：判据从「服务商 `model_type`」改为「**模型行 `model_type`**」——
    能力类型已下沉到模型层（服务商层无该列）。映射所选模型**必须已在模型层配置成行**，
    否则抛 `BusinessModelNotConfiguredError`（目录里有名字不等于用户标过类型）。

    四步，任一步不满足都**抛错、绝不回落**（§13.2.3 定稿）：

    1. 查 `llm_business_providers` 的 `(user_id, platform, business_key)`
       —— 无行 ⇒ `BusinessProviderNotConfiguredError`；
    1.5. 映射的 `model_name` 为空 ⇒ `BusinessModelRequiredError`
       （**不回落 `default_model`** —— 那是 v2.3 前的静默兜底，已废除）；
    2. 业务点的 `kind`（见 `services/business_provider_keys`）必须等于
       **模型行** `model_type` —— 模型行不存在 ⇒ `BusinessModelNotConfiguredError`；
       不等 ⇒ `BusinessProviderTypeMismatchError`
       （**提前拦**：否则会拿文本模型去打图像接口，报一个看不懂的上游错）；
    3. 复用 `resolve_provider_context()` 取凭据 —— 它自己负责
       「服务商不存在」与「服务商停用」两种结构化错误。

    ⚠️ **跨用户隔离**：映射与凭据都带 `user_id` 约束（DEF-066 / §13.2.4）。
    """
    from backend.app.models.llm_gateway import LlmBusinessProvider
    from backend.app.services.business_provider_keys import BUSINESS_KEYS, BusinessKey

    # 接受 `BusinessKey` 常量或裸字符串：调用点直接传常量（拼错就 ImportError，
    # 比手写字面量可靠）；也容忍字符串，方便测试与将来的外部调用。
    key = business_key.key if isinstance(business_key, BusinessKey) else (business_key or "").strip()
    spec = next((k for k in BUSINESS_KEYS if k.key == key), None)
    if spec is None:
        # 不认识业务点 ⇒ 也是「没配」，但我们不静默——按未配置表达（同一种出路：去页面配）
        raise BusinessProviderNotConfiguredError(key, platform)

    mapping = db.scalars(
        select(LlmBusinessProvider).where(
            LlmBusinessProvider.user_id == user.id,
            LlmBusinessProvider.platform == platform,
            LlmBusinessProvider.business_key == key,
        )
    ).first()
    if mapping is None:
        raise BusinessProviderNotConfiguredError(key, platform)

    # v2.3：必须显式选模型（不回落 default_model）。
    model_name = (mapping.model_name or "").strip()
    if not model_name:
        raise BusinessModelRequiredError(key, platform)

    ctx = resolve_provider_context(db, user.id, mapping.provider_key)

    # 类型匹配：业务点的能力类别必须与**所选模型**声明的一致（§13.2.9.6，v2.7）
    # ⚠️ v2.7 起判据在**模型层**（`llm_gateway_models`），不再是服务商 `model_type`。
    from backend.app.models.llm_gateway import LlmGatewayModel

    model_row = db.scalars(
        select(LlmGatewayModel).where(
            LlmGatewayModel.user_id == user.id,
            LlmGatewayModel.provider_key == ctx.provider_key,
            LlmGatewayModel.model_name == model_name,
        )
    ).first()
    if model_row is None:
        # 目录里有名字、但用户从没在模型层标注过 ⇒ 判不了类型，**不猜**
        raise BusinessModelNotConfiguredError(key, model_name, platform)
    model_model_type = (model_row.model_type or "").strip()
    if model_model_type and not _type_satisfies(kind=spec.kind, model_type=model_model_type):
        raise BusinessProviderTypeMismatchError(key, spec.kind, model_model_type, model_name)

    return ResolvedModelContext(
        base_url=ctx.base_url,
        model_name=model_name,
        user_id=user.id,
        api_key=ctx.api_key,
        provider_key=ctx.provider_key,
    )


def complete_text(
    *,
    model_config: Any,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.7,
    platform: str = PLATFORM_XHS,
    function: str,
) -> str:
    """薄封装：转交 `OpenAICompatibleTextClient._complete()`（**文本唯一汇聚点**）。

    记账在 `_complete()` 内部完成，本函数**不重复记账**。
    `function` 由调用方用 `llm_gateway_usage_service.FUNCTION_*` 常量显式下传。
    """
    client = OpenAICompatibleTextClient()
    return client._complete(  # noqa: SLF001 —— 设计口径就是复用同一汇聚点
        model_config=model_config,
        api_key=api_key,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=temperature,
        platform=platform,
        function=function,
    )
