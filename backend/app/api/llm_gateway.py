from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.security import decrypt_text, encrypt_text
from backend.app.core.time import shanghai_now
from backend.app.models import LlmBusinessProvider, LlmGatewayConfig, User
from backend.app.schemas.common import paginated
from backend.app.services import llm_gateway_monitor_service

# ⚠️ 工作平台公共维度（设计 17 §7.1 v1.4，2026-09-29 用户口径「多平台公共的大模型网关」）：
# 网关是平台无关的基础设施，不挂 /xhs/ 前缀；数据层本就 (user_id, name) 隔离，零迁移。
#
# ⚠️ **v1.7（2026-09-30）去外挂化**：上游 = **任意 OpenAI 兼容端点**（三方服务商直连，
# 或用户自托管的 OpenAI 兼容网关仅作一个 base_url），项目内不再挂 New API 进程、
# 不再读它的本地 SQLite（`local_db_path` 字段弃用，见设计 17 修订 ⑰⑱⑳）。
router = APIRouter(prefix="/llm/gateway-configs", tags=["llm-gateway-configs"])

#: 前端新建时的建议值（v1.7：通用 OpenAI 兼容端点，非 New API 专属）。
#: ⚠️ v1.8（2026-09-30，设计 17 §7.1.0）：本值**本身带 `/v1`** —— 因 `base_url` 已是
#: 「模型调用地址本身」，后端**不再硬拼 `/v1`**（旧口径下此处与硬拼冲突，是 discovery 404 的同源缺陷之一）。
DEFAULT_BASE_URL = "https://api.openai.com/v1"

#: **「使用平台」枚举**（v1.9，设计 17 §13.1 决策 D3）。
#: ⚠️ 取值与 `api/ai.py` 既有的 `platform` 字面量集**同源**（xhs/douyin/kuaishou/weibo/xianyu/taobao）。
#: ⚠️ **与台账 `LlmGatewayUsageLog.platform` 不是同一个概念**（§3.8 ⚠️）：
#: 本枚举 = 「该服务商服务哪个业务平台」（静态配置）；台账 platform = 「该次调用的业务上下文」（动态）。
#: 两者**不许合并**、不许共用一份常量 —— 语义不同，合并会让「服务商适用范围」与「本次调用属于谁」互相污染。
#: 顺序即前端下拉顺序（xhs 在前：本项目主平台）。
PLATFORM_ENUM: tuple[tuple[str, str], ...] = (
    ("xhs", "小红书"),
    ("douyin", "抖音"),
    ("kuaishou", "快手"),
    ("weibo", "微博"),
    ("xianyu", "闲鱼"),
    ("taobao", "淘宝"),
)
PLATFORM_VALUES: frozenset[str] = frozenset(value for value, _label in PLATFORM_ENUM)

#: `provider_key` 的随机尾（字母表去易混字符 `0/O/1/I/L`，同 §35.13 批次号纪律）。
#: `provider_key` 会被业务代码引用、也会被人抄写/贴日志，混字符的代价是排查成本。
_KEY_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
#: 生成重试次数（两道防线的应用层那一半；DB 唯一索引兜底并发）。
_KEY_MAX_ATTEMPTS = 8


def _clean_base_url(raw: str | None) -> str:
    """`base_url` 落库前的唯一规范化（v1.8，设计 17 §7.1.0）。

    语义 = **模型调用地址本身**：用户填什么就是什么，**不重写 / 不补或剥 `/v1`**。
    此处只做两件**语法层**的事，不改用户语义：

    1. `strip()` —— 去首尾空白（粘贴常带）；
    2. `rstrip("/")` —— 去末尾斜杠，**仅为拼接子路径时不产生双子斜杠**（`…/v1//models`）。

    ⚠️ 第 2 步**不是**「归一化用户地址」：`https://x/v1/` 与 `https://x/v1` 视为同一地址本就合理，
    且记账反查（`llm_gateway_usage_service._normalize_base_url`）用同一口径才能命中同一条配置。
    """
    return (raw or "").strip().rstrip("/")


def _slug(name: str) -> str:
    """服务商名 → KEY 前缀：只留字母数字、转大写、截 24 位。

    ⚠️ 非 ASCII 名（如纯中文「本机网关」）过滤后可能为空 —— 此时返回 `PROVIDER`，
    保证 KEY 仍是合法可读的标识（**不返回空串**：空串会让 KEY 变成 `-ABCD`，前缀信息全丢）。
    与迁移 `d5e6f7a8b9c0` 的同名函数**同一口径**（KEY 一经生成即不可变，两处算法必须一致，
    否则「迁移生成的 KEY」与「新建生成的 KEY」风格分叉）。
    """
    import re as _re

    slug = _re.sub(r"[^A-Za-z0-9]", "", name or "").upper()[:24]
    return slug or "PROVIDER"


def _generate_provider_key(db: Session, user_id: int, name: str) -> str:
    """生成 `{slug(name)}-{4位}` 并保证本用户内不重复（D1 + D2 的应用层那半）。

    ⚠️ 查重是**应用层给出清晰结果**的那一道；并发下真正的闸门是
    `uq_llm_gateway_configs_user_provider_key`（DB 层）—— 两道防线，缺一不可
    （只靠应用层：并发插入会漏；只靠 DB：报错信息是 `IntegrityError`，用户看不懂）。
    """
    import secrets

    for _ in range(_KEY_MAX_ATTEMPTS):
        candidate = f"{_slug(name)}-{''.join(secrets.choice(_KEY_ALPHABET) for _ in range(4))}"
        exists = db.scalar(
            select(LlmGatewayConfig.id).where(
                LlmGatewayConfig.user_id == user_id,
                LlmGatewayConfig.provider_key == candidate,
            )
        )
        if exists is None:
            return candidate
    # 8 次全撞（36^4 ≈ 168 万空间，实际不可能）—— 抛结构化错，**不静默返回重复值**
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "LLM_PROVIDER_KEY_GENERATION_FAILED",
            "message": "服务编码生成失败（连续重名），请换一个名称后重试",
        },
    )


class GatewayConfigCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    #: v1.9 必填（D3）：使用平台，须在 `PLATFORM_VALUES` 内
    platform: str = Field(min_length=1, max_length=32)
    #: v2.0 必填（§13.2.1）：模型类型，`text` / `image`。
    #: 「模型配置页」按它过滤下拉候选，M2 的类型匹配校验也读它。
    #: ⚠️ 新增时**必填**（前端下拉必选）：默认值 `text` 是留给存量行的，
    #: 新表单不给默认 —— 用户口径「新增使用平台，后端新增枚举下拉选择必选」同型。
    model_type: str = Field(default="", max_length=16)
    base_url: str = Field(default=DEFAULT_BASE_URL, max_length=512)
    #: v1.9 必填（用户口径「访问令牌必填」）；空 ⇒ 400 LLM_PROVIDER_TOKEN_REQUIRED
    access_token: str = ""
    #: v1.9 必填（用户口径「默认模型必填」）；空 ⇒ 400 LLM_PROVIDER_MODEL_REQUIRED
    default_model: str = Field(default="", max_length=128)

    # ⚠️ `extra="forbid"` 同时兜住两件事：① 前端误传 `provider_key` ⇒ 422（不可编辑）；
    # ② 拼错字段名不会静默忽略（否则「我填了 platform 怎么没生效」无从排查）。
    model_config = ConfigDict(extra="forbid")


class GatewayConfigUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    #: v1.9：使用平台可改（服务商换适用范围是合理操作）
    platform: Optional[str] = Field(default=None, min_length=1, max_length=32)
    #: v2.0：模型类型**可改**（但改了会让**已指向它的映射失效** —— 后端类型校验拦，
    #: 前端要在编辑弹窗提示这个后果，不许静默）。
    model_type: Optional[str] = Field(default=None, max_length=16)
    base_url: Optional[str] = Field(default=None, max_length=512)
    # 改 token 时留空 = 保持不变（防误清）；传非空才更新
    access_token: Optional[str] = None
    default_model: Optional[str] = Field(default=None, max_length=128)
    enabled: Optional[bool] = None
    # ⚠️ **刻意不声明 `provider_key`**（用户口径「新增后不可编辑」）：
    # 加上 `extra="forbid"` ⇒ 请求里带它一律 422。
    # ⚠️ 这是**服务端**约束 —— 前端把输入框藏起来只是表层（前端可绕过），
    # 「不可编辑」必须由服务端拒绝才算数。

    model_config = ConfigDict(extra="forbid")


def _parse_models_json(models_json: str | None) -> list[str]:
    """解析服务商 `models_json` 模型目录为模型名列表（坏数据 ⇒ `[]`，不抛）。

    v2.3：`upsert_business_provider` 用它校验「选的模型是否在该服务商目录里」。
    ⚠️ 坏 JSON / 非列表一律返回 `[]`（**不臆造模型名**）—— 调用方据此区分
    「目录为空」（放行，有些上游 /models 不全）与「目录非空但没这个模型」（400）。
    """
    import json as _json

    try:
        data = _json.loads(models_json or "[]")
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    names: list[str] = []
    for item in data:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            ident = item.get("id") or item.get("name")
            if ident:
                names.append(str(ident))
    return names


def _serialize_config(config: LlmGatewayConfig) -> dict:
    """序列化：令牌字段永不返回，只给 has_access_token 布尔（§10 安全设计）。

    v1.6：`models` = models_json 解析出的模型目录（供前端下拉；无则空列表）。
    v1.7：`local_db_path` 出参移除（New API 特化字段，列保留不删、新代码不读写）。
    v1.9：新增 `provider_key`（唯一 KEY，前端只读展示）与 `platform`（使用平台）。
    """
    models = _parse_models_json(config.models_json)
    return {
        "id": config.id,
        "name": config.name,
        "provider_key": config.provider_key,
        "platform": config.platform,
        # v2.0（§13.2.1）：模型类型 —— 「模型配置页」按它过滤下拉候选
        # （图像业务点只列 image 类型服务商），M2 的类型匹配校验也读它。
        "model_type": config.model_type,
        "base_url": config.base_url,
        "default_model": config.default_model,
        "models": models,
        "has_access_token": bool(config.access_token),
        "enabled": config.enabled,
        "healthy": config.healthy,
        "last_health_at": config.last_health_at.isoformat() if config.last_health_at else None,
        "created_at": config.created_at.isoformat() if config.created_at else None,
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
    }


class ModelsPreviewRequest(BaseModel):
    """**未保存**时拉模型目录（v1.9，设计 17 §7.1）。

    ⚠️ 入参是**地址 + 令牌**而非 `id`：新增弹窗里服务商还没有 id；编辑时用户可能改了地址/重填令牌，
    必须按**表单当前值**拉，不能用库里旧值。
    `access_token` 为空且给了 `id` ⇒ 用该行的令牌兜底（编辑态「留空 = 不修改」的常见形态）。
    """

    base_url: str = Field(min_length=1, max_length=512)
    access_token: str = ""
    #: 编辑态可选：令牌留空时用该行的令牌（**只读，不落库**）
    id: Optional[int] = None

    model_config = ConfigDict(extra="forbid")


def _validate_platform(value: str | None) -> str:
    """校验「使用平台」在枚举内（D3）。非法值 ⇒ 400 `LLM_PROVIDER_PLATFORM_REQUIRED`。

    ⚠️ 错误码沿用 `…_REQUIRED`（而非 `…_UNSUPPORTED`）：对用户而言
    「没选/选了个不在列表里的」是同一件事 —— 都需要从下拉里重选，
    分成两个码只会让前端多写一条分支而用户感受不到差别。
    """
    if not value or value not in PLATFORM_VALUES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "LLM_PROVIDER_PLATFORM_REQUIRED",
                "message": f"使用平台必填，且需为 {sorted(PLATFORM_VALUES)} 之一",
            },
        )
    return value


def _validate_model_type(value: str | None, *, required: bool) -> str | None:
    """校验模型类型（§13.2.1）。

    `required=True`（新增）⇒ 空值 400；`required=False`（编辑）⇒ 空值视为「不修改」，
    由调用方决定是否落库。取值须在 `MODEL_TYPES` 内。
    """
    from backend.app.services.business_provider_keys import MODEL_TYPES

    if not value:
        if required:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "LLM_PROVIDER_MODEL_TYPE_REQUIRED",
                    "message": f"模型类型必填，且需为 {sorted(MODEL_TYPES)} 之一",
                },
            )
        return None
    if value not in MODEL_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "LLM_PROVIDER_MODEL_TYPE_REQUIRED",
                "message": f"模型类型须为 {sorted(MODEL_TYPES)} 之一",
            },
        )
    return value


def _get_owned_config(db: Session, current_user: User, config_id: int) -> LlmGatewayConfig:
    config = db.get(LlmGatewayConfig, config_id)
    if config is None or config.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "LLM_GATEWAY_NOT_FOUND", "message": "网关配置不存在"})
    return config


@router.get("")
def get_gateway_configs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = (
        select(LlmGatewayConfig)
        .where(LlmGatewayConfig.user_id == current_user.id)
        .order_by(LlmGatewayConfig.id.desc())
    )
    configs = db.scalars(statement).all()
    return paginated([_serialize_config(config) for config in configs], page, page_size)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_gateway_config(
    payload: GatewayConfigCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """新增服务商（v1.9）。

    ⚠️ **三件必填**（用户口径「访问令牌和默认模型必填」「使用平台必选」）：
    任一为空/非法 ⇒ 400 结构化错误（**不静默用默认值补齐** —— 补齐等于替用户
    编造一份凭据，第一次调用才发现是错的）。
    ⚠️ `provider_key` **由服务端生成**（D1），请求体不收该字段。
    """
    exists = db.scalar(
        select(LlmGatewayConfig).where(
            LlmGatewayConfig.user_id == current_user.id,
            LlmGatewayConfig.name == payload.name,
        )
    )
    if exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "LLM_GATEWAY_NAME_EXISTS", "message": "服务商名称已存在"},
        )

    _validate_platform(payload.platform)
    # v2.0（§13.2.1）：新增时模型类型**必填**（下拉必选，同「使用平台」纪律）
    model_type = _validate_model_type(payload.model_type, required=True)
    if not (payload.access_token or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "LLM_PROVIDER_TOKEN_REQUIRED", "message": "访问令牌必填"},
        )
    if not (payload.default_model or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "LLM_PROVIDER_MODEL_REQUIRED", "message": "默认模型必填"},
        )

    config = LlmGatewayConfig(
        user_id=current_user.id,
        provider_key=_generate_provider_key(db, current_user.id, payload.name),
        name=payload.name,
        platform=payload.platform,
        model_type=model_type,
        base_url=_clean_base_url(payload.base_url or DEFAULT_BASE_URL),
        access_token=encrypt_text(payload.access_token) if payload.access_token else "",
        default_model=payload.default_model,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    # 设计 17 §4.5（2026-10-02）：新网关按「默认预警规则」补种（enabled=true 即生效）。
    # 幂等：同网关已有同名规则不重复插。存量网关**不**主动补种（不替用户开通知开关）。
    llm_gateway_monitor_service.ensure_default_alerts_for_gateway(
        db, user_id=current_user.id, gateway_id=config.id
    )
    return _serialize_config(config)


@router.patch("/{config_id}")
def update_gateway_config(
    config_id: int,
    payload: GatewayConfigUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """编辑服务商（v1.9）。

    ⚠️ **停用/开启**（`enabled`）的业务语义（D4，§3.8）：停用 ⇒ **业务取不到该服务商**
    —— 调用链按 `provider_key` 解析时命中 `enabled=False` ⇒ 抛结构化错误
    `LLM_PROVIDER_DISABLED`，**不静默回落到其它服务商**（回落 = 用错凭据调用，
    用户看不到任何提示，只会困惑「为什么调用的是 A 却报 B 的错」）。
    前端表格的开关即此字段的唯一入口。
    """
    config = _get_owned_config(db, current_user, config_id)

    # v2.0：模型类型可改；留空 = 不修改（同 access_token 的「编辑态留空」口径）。
    new_model_type = _validate_model_type(payload.model_type, required=False)
    if new_model_type is not None:
        config.model_type = new_model_type

    if payload.name is not None:
        config.name = payload.name
    if payload.platform is not None:
        config.platform = _validate_platform(payload.platform)
    if payload.base_url is not None:
        new_base = _clean_base_url(payload.base_url)
        # 改 base_url 后 healthy 自动置 False（需重新探测，§7.1 契约）
        if new_base != config.base_url:
            config.base_url = new_base
            config.healthy = False
    if payload.access_token is not None:
        config.access_token = encrypt_text(payload.access_token) if payload.access_token else config.access_token
    if payload.default_model is not None:
        config.default_model = payload.default_model
    if payload.enabled is not None:
        config.enabled = payload.enabled
    # ⚠️ `provider_key` **不在此处更新**（用户口径「新增后不可编辑」）。
    # 请求体带它 ⇒ 422（`GatewayConfigUpdateRequest` 未声明该字段 + extra="forbid"）。
    # 服务编码一旦生成即是对外契约（业务代码引用它），改名会让引用悬空。

    db.commit()
    db.refresh(config)
    return _serialize_config(config)


@router.delete("/{config_id}", include_in_schema=False)
def delete_gateway_config(
    config_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """⚠️ **v1.9 退场**（用户口径「移除删除功能」）—— 保留路由为 **410 墓碑**。

    为什么是 410 而不是删掉路由：删掉会回 **404**，而 404 无法区分
    「这个服务商不存在」与「删除功能已退场」—— 排障时会把两者混为一谈
    （同 ADR-021 / 设计 18 的退场惯例）。

    为什么**不真删数据**：`provider_key` 已被业务数据引用（`model_configs.provider_key`），
    删行会让引用悬空 ⇒ 只提供**停用/开启**（`PATCH {enabled: false}`）。

    ⚠️ `include_in_schema=False` ⇒ 本端点**不计入 OpenAPI**（paths/operations 都不算），
    与 T7 的端点回数口径一致。

    ~~旧行为（v1.8 及更早）：`db.delete(config)` 真删 + 级联清理 `llm_gateway_snapshots`~~
    （原句保留，见设计 17 §7.1 对照表）。
    """
    _get_owned_config(db, current_user, config_id)  # 越权/不存在仍走 404（归属语义不变）
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "LLM_PROVIDER_DELETE_GONE",
            "message": "服务商已不支持删除，请改用停用",
        },
    )


#: 枚举下发用的**独立前缀**（设计 17 §7.1：`GET /api/llm/providers/platforms`）。
#: ⚠️ 不挂在 `/llm/gateway-configs` 下：枚举描述的是「服务商」维度的公共字典，
#: 与具体某条配置无关；放配置前缀下会让「公共字典」看起来像「配置的子资源」。
providers_router = APIRouter(prefix="/llm/providers", tags=["llm-gateway-configs"])


@providers_router.get("/platforms")
def get_provider_platforms(
    current_user: User = Depends(get_current_user),
):
    """**「使用平台」枚举下发**（v1.9，D3）。

    用户口径「后端新增枚举下拉选择必选」⇒ 枚举**由后端下发**，前端不许就地写死选项
    （写死 ⇒ 新增平台要改两处，DEF-044 同一语义不留两份实现）。

    ⚠️ 路径 = `/api/llm/providers/platforms`（**不**挂在 `/gateway-configs` 下，
    与设计 17 §7.1 契约一致）。
    """
    return {"items": [{"value": value, "label": label} for value, label in PLATFORM_ENUM]}


@router.post("/models-preview")
def preview_provider_models(
    payload: ModelsPreviewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**未保存**时拉取服务商的模型目录（v1.9，设计 17 §7.1）。

    用户口径：「配置服务商地址和 key 之后是否可以拉取到该服务商下所有模型，然后在模型输入框改为
    下拉框选择」。

    语义 = 「`/{id}/test` 的第 ① 步，**去掉持久化**」：同样打 `GET {base_url}/models`
    （同一口径，**`/v1` 不硬拼**），同样 401 / 400 错误码。

    ⚠️ **不落库、不建行**：这是预览（用户可能取消）。目录落库只发生在 `POST`（新建）
    与 `/{id}/test`（已知 id）。

    ⚠️ **路由顺序**：本路由必须在 `/{config_id}` 系列**之前**声明 —— 否则 `models-preview`
    会被当成 `config_id` 去解析（`config_id: int` 会直接 422 而不是走到这里）。
    """
    import requests as http_requests

    from backend.app.core.security import decrypt_text

    base_url = _clean_base_url(payload.base_url)
    access_token = (payload.access_token or "").strip()

    # 编辑态令牌留空 ⇒ 用该行的令牌兜底（只读；越权/不存在 ⇒ 404，不静默当无令牌）
    if not access_token and payload.id is not None:
        config = _get_owned_config(db, current_user, payload.id)
        access_token = decrypt_text(config.access_token) if config.access_token else ""

    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "LLM_GATEWAY_UNREACHABLE", "message": "请先填写模型调用地址"},
        )
    if not access_token:
        # ⚠️ 与 `/{id}/test` 的「无令牌跳过探测」不同：那是**已保存配置的巡检**（跳过是合理的），
        # 这里是**用户主动点「拉取模型」** —— 没令牌就拉不到，直接说清楚比返回空列表有用。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "LLM_PROVIDER_TOKEN_REQUIRED", "message": "请先填写访问令牌再拉取模型"},
        )

    try:
        mresp = http_requests.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "LLM_GATEWAY_UNREACHABLE",
                "message": f"服务商不可达: {str(exc)[:120]} @ {base_url}/models",
            },
        )

    if mresp.status_code == 401:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "LLM_GATEWAY_AUTH_FAILED", "message": "访问令牌无效或过期"},
        )
    if mresp.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "LLM_GATEWAY_UNREACHABLE",
                # v1.8 口径：带上实际请求 URL —— 「路径双拼」与「真的不通」现象相同，靠 URL 区分
                "message": f"服务商不可达 (HTTP {mresp.status_code}) @ {base_url}/models",
            },
        )

    try:
        body = mresp.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "LLM_GATEWAY_UNREACHABLE",
                "message": f"服务商返回的不是 JSON @ {base_url}/models",
            },
        )
    models = [item.get("id", "") for item in body.get("data", []) if item.get("id")]
    return {
        "models": models,
        "message": f"拉到 {len(models)} 个模型" if models else "服务商未返回模型列表",
    }


@router.get("/{config_id}")
def get_gateway_config(
    config_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """单条服务商详情（v1.9）。

    ⚠️ `config_id: int` 是**必要的类型约束** —— 本前缀下还有 `POST /models-preview`
    这个**字面量路径**；若把 `config_id` 声明成 `str`，`models-preview` 会被它抢先匹配
    （FastAPI 按声明类型不匹配才跳过，`str` 永不失败）。
    ⚠️ v1.9 的「使用平台枚举下发」**不在此前缀**（独立 `/api/llm/providers/platforms`，
    见 `providers_router`）。
    """
    return _serialize_config(_get_owned_config(db, current_user, config_id))


@router.post("/{config_id}/test")
def test_gateway_config(
    config_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """健康探测 + 模型列表（§7.1 v1.7：纯 OpenAI 兼容，无 New API 特化探测）。

    顺序：① `GET {base_url}/models`（带 access_token，取模型目录；401 ⇒ 令牌错）
    →（可选）② 一次极小 chat 请求验证 default_model。
    结果写 healthy / last_health_at；令牌错 401 LLM_GATEWAY_AUTH_FAILED。

    ⚠️ v1.7 移除 v1.4–v1.6 的第①步 `GET {base}/api/status`（New API 可达性探测）——
    那是 New API 专有接口，非 OpenAI 标准；上游任何形态（三方直连 / 自托管网关）统一走
    `/models` 判活。**无令牌时跳过探测**、只置健康（无法验证，不臆造失败）。

    ⚠️ **v1.8（设计 17 §7.1.0）**：`base_url` = **模型调用地址本身**，`/v1` **不再由后端硬拼**
    —— 用户填 `https://x/v1` ⇒ 实际打 `https://x/v1/models`；填 `http://127.0.0.1:3000`
    ⇒ 实际打 `http://127.0.0.1:3000/models`。旧口径「硬拼 `/v1`」使「地址自带 `/v1`」的上游
    （书生 discovery、moonshot 等）必然 404，且看起来像令牌错 —— 故本条要求失败 `message`
    **带上实际请求 URL**，便于一眼识别路径双拼。
    """
    import requests as http_requests

    config = _get_owned_config(db, current_user, config_id)
    # v1.8（设计 17 §7.1.0）：base_url = 模型调用地址本身，后端**不硬拼 /v1**
    # ⇒ 子路径直接拼在用户地址后（用户填 …/v1 ⇒ …/v1/models）。
    base_url = config.base_url.rstrip("/")
    access_token = decrypt_text(config.access_token) if config.access_token else ""

    # ① 模型目录（带 access_token；无令牌则跳过——不探测、不判失败）
    models: list[str] = []
    if access_token:
        try:
            mresp = http_requests.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if mresp.status_code == 401:
                config.healthy = False
                config.last_health_at = None
                db.commit()
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={"code": "LLM_GATEWAY_AUTH_FAILED", "message": "访问令牌无效或过期"},
                )
            if mresp.status_code >= 400:
                config.healthy = False
                config.last_health_at = None
                db.commit()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "LLM_GATEWAY_UNREACHABLE",
                        # v1.8：带上实际请求 URL —— 「路径双拼」与「真的不通」现象相同，靠 URL 区分
                        "message": f"网关不可达 (HTTP {mresp.status_code}) @ {base_url}/models",
                    },
                )
            body = mresp.json()
            models = [item.get("id", "") for item in body.get("data", []) if item.get("id")]
        except HTTPException:
            raise
        except Exception as exc:
            config.healthy = False
            config.last_health_at = None
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "LLM_GATEWAY_UNREACHABLE",
                    "message": f"网关不可达: {str(exc)[:120]} @ {base_url}/models",
                },
            )

    # ② 极小 chat 请求验证 default_model（仅当配置了 default_model 且有令牌）
    chat_message = ""
    if access_token and config.default_model:
        try:
            cresp = http_requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json={"model": config.default_model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5},
                timeout=15,
            )
            if cresp.status_code < 400:
                chat_message = "chat 连通"
            else:
                chat_message = f"chat 返回 HTTP {cresp.status_code} @ {base_url}/chat/completions"
        except Exception as exc:
            chat_message = f"chat 探测异常: {str(exc)[:80]} @ {base_url}/chat/completions"

    config.healthy = True
    config.last_health_at = shanghai_now()
    # v1.6：模型目录持久化到 models_json（前端默认模型下拉由目录驱动）
    import json as _json

    config.models_json = _json.dumps(models, ensure_ascii=False)
    db.commit()
    return {
        "id": config.id,
        "healthy": True,
        "message": (
            f"网关可达; 模型 {len(models)} 个" + (f"; {chat_message}" if chat_message else "")
            if access_token
            else "未配置访问令牌，跳过探测; 请填写令牌后重新测试"
        ),
        "models": models,
    }


@router.get("/{config_id}/stats")
def get_gateway_stats(
    config_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """网关用量统计（v1.6：读本地台账 llm_gateway_usage_logs 聚合）。

    ⚠️ **v1.6 取代 v1.3 的「直读 New API 本地 logs 库」**（用户口径「两个数据库合并」）：
    用量真相源 = 本地台账 `LlmGatewayUsageLog`，任意 base_url（三方/自托管）都支持，
    与是否本机 New API 彻底脱钩（v1.7 起 `local_db_path` 字段已整体弃用）。

    聚合窗口：today / week 两档（`created_at` 落在窗口内的行）。
    台账空 ⇒ `available=False`（空态，不是错误，§缺值口径）。
    """
    from backend.app.models import LlmGatewayUsageLog

    config = _get_owned_config(db, current_user, config_id)
    now = shanghai_now()

    def _agg(since):
        row = db.execute(
            select(
                func.count(LlmGatewayUsageLog.id),
                func.coalesce(func.sum(LlmGatewayUsageLog.prompt_tokens), 0),
                func.coalesce(func.sum(LlmGatewayUsageLog.completion_tokens), 0),
                func.coalesce(func.avg(LlmGatewayUsageLog.latency_ms), 0),
                func.coalesce(
                    func.sum(
                        case((LlmGatewayUsageLog.status == "success", 1), else_=0)
                    ),
                    0,
                ),
            ).where(
                LlmGatewayUsageLog.user_id == current_user.id,
                LlmGatewayUsageLog.gateway_id == config.id,
                LlmGatewayUsageLog.created_at >= since,
            )
        ).one()
        calls = int(row[0] or 0)
        ok_calls = int(row[4] or 0)
        p = int(row[1] or 0)
        c = int(row[2] or 0)
        return {
            "calls": calls,
            "success_calls": ok_calls,
            "fail_calls": calls - ok_calls,
            "prompt_tokens": p,
            "completion_tokens": c,
            "total_tokens": p + c,
            "quota": p + c,  # v1.6：本地台账无 quota 单位，用总 token 近似（展示口径）
            "avg_latency_ms": round(float(row[3] or 0), 1),
        }

    from datetime import timedelta

    today = _agg(now - timedelta(days=1))
    week = _agg(now - timedelta(days=7))
    available = today["calls"] > 0 or week["calls"] > 0
    return {
        "id": config.id,
        "available": available,
        "message": f"今日 {today['calls']} 次调用 / 近 7 日 {week['calls']} 次" if available else "本地台账暂无调用记录",
        "today": today,
        "week": week,
    }


# =========================================================================== #
# 「平台 × 业务点 → 服务商」映射（设计 17 §13.2.6，v2.0）
# =========================================================================== #
#: 独立前缀：映射是**业务点维度**的公共字典 + 映射表，不是某条服务商的子资源。
#: ⚠️ 路径 `/api/llm/business-providers`（与 `/llm/gateway-configs` 平级）。
business_router = APIRouter(prefix="/llm", tags=["llm-business-providers"])


class BusinessProviderUpsertRequest(BaseModel):
    """PUT upsert 入参（§13.2.6）。

    ⚠️ `business_key` 与 `platform` 一起构成**唯一键**（`(user_id, platform, business_key)`）；
    改这一行 = 实时切换服务商。

    ⚠️ **v2.3 必选「服务商 + 具体模型」**（2026-10-02 用户口径「两者都应是必须选择，
    不指定具体的模型无法调用成功」）：`provider_key` 非空时 `model_name` **必须非空**，
    且必须落在该服务商的模型目录（`models_json`）里。

    ⚠️ `provider_key` 传**空串** = 清空映射（回到「未配置」态）—— 刻意不另开 DELETE 端点：
    「删一条映射」与「把映射指向空」在数据上同形（§13.2.3 唯一键决定一行一业务点），
    多一个端点只会多一处语义分叉。此时 `model_name` 一并清空（无服务商则无模型可言）。
    """

    platform: str = Field(min_length=1, max_length=32)
    business_key: str = Field(min_length=1, max_length=48)
    provider_key: str = Field(default="", max_length=64)
    #: v2.3：具体模型名；`provider_key` 非空时必填（见端点校验）。
    model_name: str = Field(default="", max_length=128)

    model_config = ConfigDict(extra="forbid")


@business_router.get("/business-providers")
def list_business_providers(
    platform: Optional[str] = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """映射列表（§13.2.6）。

    返回该用户**所有业务点**（含未配置的）—— 未配置项 `provider_key=""`、`provider_name=None`，
    前端据此渲染「未配置」空态行。**不是**只返回已配置行：只返回已配置行会让
    「还没配的业务点」在页面上消失，用户无从发现（同 §7.1 平台枚举全量下发的纪律）。
    """
    from backend.app.services.business_provider_keys import BUSINESS_KEYS

    if platform is not None:
        _validate_platform(platform)

    rows = db.scalars(
        select(LlmBusinessProvider).where(LlmBusinessProvider.user_id == current_user.id)
    ).all()
    by_key = {(r.platform, r.business_key): r for r in rows}

    # 服务商名映射（一次查完，别在循环里 N+1）
    configs = db.scalars(
        select(LlmGatewayConfig).where(LlmGatewayConfig.user_id == current_user.id)
    ).all()
    name_of = {c.provider_key: c.name for c in configs}
    enabled_of = {c.provider_key: bool(c.enabled) for c in configs}
    type_of = {c.provider_key: c.model_type for c in configs}
    #: v2.3：服务商 → 模型目录（前端模型下拉联动用；一次查完，别在循环里 N+1）
    models_of = {c.provider_key: _parse_models_json(c.models_json) for c in configs}

    items = []
    for item in BUSINESS_KEYS:
        key = (platform or PLATFORM_ENUM[0][0], item.key)
        row = by_key.get(key)
        pk = (row.provider_key if row else "") or ""
        model_name = (row.model_name if row else "") or ""
        items.append({
            "platform": key[0],
            "business_key": item.key,
            "kind": item.kind,
            #: 业务点中文名与**触发源**（常量元数据一并下发，页面的「业务点 / 触发源」
            #: 两列直接渲染本行字段，不必再拿 `keys` 反查一遍；两者都只读）。
            "label": item.label or item.key,
            "triggers": list(item.triggers),
            "provider_key": pk,
            "provider_name": name_of.get(pk) if pk else None,
            "provider_enabled": enabled_of.get(pk) if pk else None,
            #: 映射指向的服务商当前类型（诊断「类型不匹配」用；配置正确时应恒等于 kind）
            "provider_model_type": type_of.get(pk) if pk else None,
            #: v2.3：已选具体模型 + 该服务商的模型目录（前端模型下拉候选）
            "model_name": model_name,
            "provider_models": models_of.get(pk, []) if pk else [],
            #: 已配置 = 服务商与模型**都**选了（只选服务商不算配好，调用会失败）
            "configured": bool(pk and model_name),
            "enabled": bool(row.enabled) if row else True,
        })
    return {"items": items, "platforms": [{"value": v, "label": l} for v, l in PLATFORM_ENUM]}


@business_router.put("/business-providers")
def upsert_business_provider(
    payload: BusinessProviderUpsertRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**实时切换服务商 + 具体模型**（§13.2.6）—— 改一行即生效，无「保存」按钮。

    校验（任一不过都 400/404，**不写脏引用**）：
      1. `business_key` 必须是已登记的业务点（`is_valid_business_key`）；
      2. `provider_key` 非空时必须在**本用户**的服务商里存在 ⇒ 否则 404；
      3. `provider_key` 非空时其 `model_type` 必须等于业务点的 `kind`
         ⇒ 否则 400（否则会拿文本模型去打图像接口，报一个看不懂的上游错）；
      4. **v2.3**：`provider_key` 非空时 `model_name` **必须非空**且落在该服务商的
         模型目录（`models_json`）里 ⇒ 否则 400 —— 用户口径「两者都应是必须选择，
         不指定具体的模型无法调用成功」。
    """
    from backend.app.services.business_provider_keys import get_business_key

    _validate_platform(payload.platform)
    item = get_business_key(payload.business_key)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "LLM_BUSINESS_KEY_UNKNOWN",
                "message": f"未知业务点：{payload.business_key}",
            },
        )

    provider_key = (payload.provider_key or "").strip()
    model_name = (payload.model_name or "").strip()

    if provider_key:
        config = db.scalar(
            select(LlmGatewayConfig).where(
                LlmGatewayConfig.user_id == current_user.id,
                LlmGatewayConfig.provider_key == provider_key,
            )
        )
        # ⚠️ 跨用户服务商与「不存在」同样 404：区分二者会泄露他人服务商编码
        if config is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "LLM_PROVIDER_NOT_FOUND",
                    "message": "服务商不存在或不属于当前用户",
                },
            )
        if config.model_type != item.kind:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "LLM_BUSINESS_PROVIDER_TYPE_MISMATCH",
                    "message": (
                        f"业务点「{item.key}」需要 {item.kind} 类型服务商，"
                        f"而「{config.name}」是 {config.model_type} 类型"
                    ),
                },
            )
        # v2.3：必须显式选具体模型（两者都必选）。
        if not model_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "LLM_BUSINESS_MODEL_REQUIRED",
                    "message": (
                        f"业务点「{item.label or item.key}」必须选择具体模型"
                        f"（服务商「{config.name}」）"
                    ),
                },
            )
        # 模型必须在该服务商的目录里（目录为空时放行 —— 目录是「上游拉到什么」，
        # 有些上游的 /models 不含全部可用模型，硬拦会误伤；只在目录非空时校验）。
        available = _parse_models_json(config.models_json)
        if available and model_name not in available:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "LLM_BUSINESS_MODEL_NOT_IN_CATALOG",
                    "message": (
                        f"模型「{model_name}」不在服务商「{config.name}」的模型目录里"
                    ),
                },
            )
    else:
        # 清空映射 ⇒ 模型一并清空（无服务商则无模型可言）
        model_name = ""

    row = db.scalar(
        select(LlmBusinessProvider).where(
            LlmBusinessProvider.user_id == current_user.id,
            LlmBusinessProvider.platform == payload.platform,
            LlmBusinessProvider.business_key == item.key,
        )
    )
    if row is None:
        row = LlmBusinessProvider(
            user_id=current_user.id,
            platform=payload.platform,
            business_key=item.key,
            provider_key=provider_key,
            model_name=model_name,
        )
        db.add(row)
    else:
        row.provider_key = provider_key
        row.model_name = model_name
    db.commit()
    db.refresh(row)
    return {
        "platform": row.platform,
        "business_key": row.business_key,
        "kind": item.kind,
        "provider_key": row.provider_key,
        "model_name": row.model_name,
        "configured": bool(row.provider_key and row.model_name),
    }


@business_router.get("/business-provider-keys")
def list_business_provider_keys(
    current_user: User = Depends(get_current_user),
):
    """**业务点清单下发**（§13.2.6，同 §7.1 平台枚举纪律）。

    用户口径「业务中的使用点映射成固定 key」⇒ 清单的唯一真相源在后端常量
    （`services/business_provider_keys.BUSINESS_KEYS`）。前端**不许**就地写死业务点列表：
    写死 ⇒ 新增业务点要改两处，而「两处」里前端那份会静默过期（DEF-044）。

    `label` 给中文名、`triggers` 给**触发源**（2026-10-01 用户口径「加触发源」）——
    两者都**来自常量**（`BusinessKey.label` / `.triggers`），端点与前端都不再维护
    第二份「key → 中文 / key → 入口」映射表：那份表加了新业务点会静默过期（DEF-044）。

    `triggers` = 该业务点在界面上由哪些业务动作触发（页面路径链，如
    `商品列表-商品详情-生成商品详情图`）。**只读展示**，空列表 = 只由 API 直接调用、
    界面无入口 —— **如实留空，不编入口**（同「缺值不造假」纪律）。
    """
    from backend.app.services.business_provider_keys import BUSINESS_KEYS

    return {
        "items": [
            {
                "key": item.key,
                "kind": item.kind,
                "label": item.label or item.key,
                "triggers": list(item.triggers),
            }
            for item in BUSINESS_KEYS
        ],
        "model_types": [
            {"value": "text", "label": "文本"},
            {"value": "image", "label": "图像"},
        ],
    }
