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
from backend.app.models import (
    LlmBusinessProvider,
    LlmGatewayConfig,
    LlmGatewayModel,
    User,
)
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


def _next_model_key(db: Session, user_id: int, provider_key: str) -> str:
    """生成模型的人类可读唯一 ID（**服务商内**自然序号：`1`、`2`、`3`…）。

    用户口径（2026-10-02）：「模型也是跟服务商一样需要有一个人为可读的唯一ID，
    比如 1，2，3 的自然序号」。

    ⚠️ **服务商内唯一，不跨服务商全局递增** —— 每个服务商都从 `1` 开始，
    因为用户脑子里记的是「DeepSeek 那个服务商的第 2 个模型」，不是全局第 37 个。
    ⚠️ 取值 = 该服务商内**已有 `model_key` 的数值最大值 + 1**（不是行数 +1）：
    删掉中间某个模型后，序号**不回收**（回收会让新旧文档指向同一个编号，比留空号更糟）。
    ⚠️ 非数字的 `model_key`（理论上只有手工改库才会出现）**跳过不参与取最大值**，
    不让一条脏数据把计数打回 `1` 而造成重号。
    """
    existing = db.scalars(
        select(LlmGatewayModel.model_key).where(
            LlmGatewayModel.user_id == user_id,
            LlmGatewayModel.provider_key == provider_key,
        )
    ).all()
    largest = 0
    for value in existing:
        text = (value or "").strip()
        if text.isdigit():
            largest = max(largest, int(text))
    return str(largest + 1)


class GatewayModelInput(BaseModel):
    """服务商下**单个模型**的配置项（v2.7，§13.2.9.9）。

    承载**能力类型**（`model_type`）与**使用平台**（`platform`）—— 二者 v2.7 已从服务商层
    下沉到模型层（§13.2.9.4）。同一服务商可有多个模型、各带不同类型/平台。

    ⚠️ `model_type` / `platform` 都是**用户声明，不是上游回报**（§13.2.1a 纪律延续）：
    上游 `/models` 不返回能力类型 ⇒ 必须由用户标；**缺省不猜**，由端点校验兜底。
    """

    model_name: str = Field(min_length=1, max_length=128)
    #: 能力类型，须在 `MODEL_TYPES` 内（`text` / `image` / `multimodal` / `video`）。
    model_type: str = Field(default="", max_length=16)
    #: 使用平台（**v2.9 起为多值数组**，§13.2.11.3）：一个模型可被多个平台共用
    #: （用户口径「**支持多个平台公共一个服务商下的一个具体模型**」）。
    #: 每个元素须在 `PLATFORM_VALUES` 内；空数组 = 还没在任何平台配过（允许，展示为 `-`）。
    #: ⚠️ 写入时机不变（§13.2.9.7）：配置动作发生在哪个平台，就把该平台**并入**此数组
    #: （**并入不是覆盖** —— 覆盖会让多平台共用无法累积）。
    platform: list[str] = Field(default_factory=list)
    enabled: bool = True
    remark: str = Field(default="", max_length=255)

    model_config = ConfigDict(extra="forbid")


class GatewayConfigCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    base_url: str = Field(default=DEFAULT_BASE_URL, max_length=512)
    #: v1.9 必填（用户口径「访问令牌必填」）；空 ⇒ 400 LLM_PROVIDER_TOKEN_REQUIRED
    access_token: str = ""
    #: ⚠️ **v2.8 起无 `default_model`**（列已删，§13.2.10）：模型真相源 = `llm_gateway_models` 模型行；
    #: 业务点必须显式选模型（`resolve_business_provider` **不回落**任何默认）。旧前端继续传 ⇒ 422
    #: （`extra="forbid"`，有意为之）。
    #: ⚠️ **v2.7**：模型列表随服务商一并创建 —— 每个模型带自己的 type/platform/enabled/remark。
    #: 取代 v2.0 的「服务商级 `model_type` + `platform`」（§13.2.9.5）。
    #: 允许为空列表（先建服务商、稍后再配模型）—— 但此时业务点还指不了它（无类型可判）。
    models: list[GatewayModelInput] = Field(default_factory=list)

    # ⚠️ `extra="forbid"` 同时兜住两件事：① 前端误传 `provider_key` ⇒ 422（不可编辑）；
    # ② 拼错字段名不会静默忽略（否则「我填了 platform 怎么没生效」无从排查）。
    # ⚠️ **v2.7 起 `model_type` / `platform` 不再是本模型字段** ⇒ 旧前端若继续传会 422
    # （这是**有意**的：静默忽略会让「我配了类型怎么没生效」变成无解之谜）。
    model_config = ConfigDict(extra="forbid")


class GatewayConfigUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    base_url: Optional[str] = Field(default=None, max_length=512)
    # 改 token 时留空 = 保持不变（防误清）；传非空才更新
    access_token: Optional[str] = None
    #: v2.8：`default_model` 字段已随列删除（§13.2.10）—— 模型变更走 `models` 子列表 /
    #: 专用子资源端点；旧前端继续传 ⇒ 422（`extra="forbid"`）。
    enabled: Optional[bool] = None
    #: ⚠️ **v2.7**：模型列表 —— **只增改不删**（§13.2.9.9）：
    #: 传了的 (provider_key, model_name) ⇒ upsert；**未传的既有行不动**。
    #: 删除模型走专用端点 `DELETE /{id}/models/{model_id}`（避免「漏传一个就静默删行」）。
    #: `None`（缺省）= 完全不碰模型层；`[]` = 也什么都不做（不删）。
    models: Optional[list[GatewayModelInput]] = None
    # ⚠️ **刻意不声明 `provider_key`**（用户口径「新增后不可编辑」）：
    # 加上 `extra="forbid"` ⇒ 请求里带它一律 422。
    # ⚠️ 这是**服务端**约束 —— 前端把输入框藏起来只是表层（前端可绕过），
    # 「不可编辑」必须由服务端拒绝才算数。

    model_config = ConfigDict(extra="forbid")


class GatewayModelUpdateRequest(BaseModel):
    """改模型行（`PATCH /{id}/models/{model_id}`，v2.7）。

    ⚠️ **全部可选**（PATCH 语义：传什么改什么）—— 与 `GatewayConfigUpdateRequest` 同型。
    `model_name` **不在可改字段里**：它是唯一约束的一半，改名等于「换一个模型」，
    直接删旧行 + 建新行更诚实（改名会让映射以旧名指向的行突然换了身份）。
    """

    model_type: Optional[str] = Field(default=None, max_length=16)
    #: ⚠️ v2.9：改多值数组。传 `[]` = 清空该模型的平台列表；传 `None`（缺省）= 不动。
    platform: Optional[list[str]] = None
    enabled: Optional[bool] = None
    remark: Optional[str] = Field(default=None, max_length=255)

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


def _find_model_row(
    db: Session, user_id: int, provider_key: str, model_name: str
) -> LlmGatewayModel | None:
    """按 (用户, 服务商, 模型名) 取模型行 —— 引用计数**减**时要用（v2.28 §13.2.9.7c）。

    取不到返回 `None`（调用方跳过减计数 —— 模型行可能已被删除，
    那时它的 platform 跟着消失，本来也无需再减）。
    """
    if not provider_key or not model_name:
        return None
    return db.scalar(
        select(LlmGatewayModel).where(
            LlmGatewayModel.user_id == user_id,
            LlmGatewayModel.provider_key == provider_key,
            LlmGatewayModel.model_name == model_name,
        )
    )


def _parse_platforms(raw: str | None) -> dict[str, int]:
    """解析模型行 `platform` 列为**引用计数**（v2.28，§13.2.9.7c）。

    形态 = `{"平台": 使用次数}`（如 `{"xhs": 2}`）——**不是**纯标识数组。

    ## 宽入（三种历史形态都要能读，不静默丢数据）

    | 落库值 | 解析结果 | 来源 |
    |---|---|---|
    | `{"xhs": 2}` | `{"xhs": 2}` | **本版形态** |
    | `["xhs"]`（JSON 数组） | `{"xhs": 1}` | v2.9–v2.27 的纯标识数组 |
    | `"xhs"`（裸字符串） | `{"xhs": 1}` | v2.9 前的单值 |

    ⇒ 旧形态按「计数 = 1」读取。⚠️ **不做「旧 = 0」**：那会让存量模型的所有平台
    在第一次读取时就消失（把「兼容」做成「清空」）。
    坏 JSON / 非法类型 ⇒ `{}`（**不臆造平台**，同 `_parse_models_json` 纪律）。
    """
    import json as _json

    text = (raw or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = _json.loads(text)
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        counts: dict[str, int] = {}
        for key, value in data.items():
            name = str(key).strip()
            if not name:
                continue
            try:
                n = int(value)
            except (TypeError, ValueError):
                n = 1
            if n > 0:
                counts[name] = n
        return counts
    if text.startswith("["):
        try:
            data = _json.loads(text)
        except Exception:
            return {}
        if not isinstance(data, list):
            return {}
        return {str(item).strip(): 1 for item in data if str(item).strip()}
    # 旧单值形态（v2.9 前）。⚠️ 也可能是 JSON 字符串标量（带引号）⇒ 先试解析
    if text.startswith('"'):
        try:
            scalar = _json.loads(text)
            if isinstance(scalar, str) and scalar.strip():
                return {scalar.strip(): 1}
        except Exception:
            pass
    return {text: 1}


def _dump_platforms(counts: dict[str, int] | None) -> str:
    """引用计数 → 落库字符串（JSON 对象；**丢弃 ≤0 的键**，v2.28 §13.2.9.7c）。

    ⚠️ **归零即移除**（用户口径「当使用次数被减到为 0 时……该平台可以被移除」）：
    这里统一丢弃 `<= 0` 的项 —— 调用方不必自己 pop（**唯一收敛点**）。
    """
    import json as _json

    clean: dict[str, int] = {}
    for key, value in (counts or {}).items():
        name = str(key).strip()
        if not name:
            continue
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue
        if n > 0:
            clean[name] = n
    return _json.dumps(clean, ensure_ascii=False)


def _bump_platform(counts: dict[str, int], platform: str, delta: int) -> dict[str, int]:
    """引用计数增减（**唯一收敛点**，v2.28 §13.2.9.7c）。返回新 dict（不改入参）。

    * `delta=+1` ⇒ 该平台计数加一（不存在则从 1 起）；
    * `delta=-1` ⇒ 减一；**减到 0 即删键**（= 该平台下已无配置在用这个模型）。

    ⚠️ **计数语义 = 「该平台下指向该模型的业务点个数」**（**状态**），
    不是「被操作过几次」（**事件**）。所以幂等 upsert 同一业务点**不该** +1 ——
    调用方必须先判断「这条映射是不是改了模型」（见 `upsert_business_provider`）。
    """
    name = str(platform or "").strip()
    if not name:
        return dict(counts)
    result = dict(counts)
    next_value = int(result.get(name, 0)) + delta
    if next_value > 0:
        result[name] = next_value
    else:
        result.pop(name, None)
    return result


def _merge_platforms(existing: dict[str, int], incoming: dict[str, int]) -> dict[str, int]:
    """**并入**引用计数（不是覆盖）—— 用户口径「支持多个平台共用一个模型」（§13.2.11.3）。

    ⚠️ 语义：已有 `{"xhs": 1}` + 传入 `{"douyin": 1}` ⇒ `{"xhs": 1, "douyin": 1}`。
    这与「在某平台配置该模型时，把当前平台写入」的口径一致：**在第二个平台配置
    同一个模型，是「追加一个使用平台」，不是「把第一个平台挤掉」**。
    ⚠️ 清空平台走 `PATCH` 显式传 `platform={}`（见 `update_gateway_model`）——
    那时语义是**覆盖为空**，不经本函数。

    ⚠️ 计数按平台**取较大值**（不是相加）：本函数的语义是「并入一批已有事实」（配置目录
    批量写入），不是「再加一次引用」。真正的 +1 走 `_bump_platform`。

    ## 去重（2026-10-03 用户口径，原话）

    > 「**使用平台需要去重，每个平台只需要添加一次，存在则不在添加**」

    ⇒ 判据 = 平台键唯一（dict 天然满足）。这是「配置时并入」的下游保证：
    **同一平台配 N 次，列表里仍只有一个**。
    """
    merged = dict(existing)
    for name, value in (incoming or {}).items():
        key = str(name).strip()
        if not key:
            continue
        merged[key] = max(int(merged.get(key, 0)), int(value))
    return merged


def _serialize_model(model: LlmGatewayModel) -> dict:
    """序列化模型行（v2.7 建立；v2.9 起 `platform` 为**数组**；v2.10 补健康字段）。

    ⚠️ **没有令牌字段** —— 凭据在服务商层，模型行只有类型/平台/启停/备注/健康。
    """
    return {
        "id": model.id,
        "provider_key": model.provider_key,
        #: ⚠️ v2.10：人类可读唯一 ID（服务商内自然序号，如 `1` / `2`）—— 前端子表首列展示，
        #: 与 `id`（DB 全局自增）区分开：用户口头指代用这个。
        "model_key": model.model_key,
        "model_name": model.model_name,
        "model_type": model.model_type,
        #: ⚠️ v2.9：出参 `platform` 是**字符串数组**（前端多选标签直接渲染）。
        # v2.28：下发**引用计数**（`{"xhs": 2}`）；前端展示用 `platforms`（键列表，向后兼容）
        "platform": _parse_platforms(model.platform),
        "platforms": sorted(_parse_platforms(model.platform)),
        "enabled": model.enabled,
        "remark": model.remark,
        #: ⚠️ v2.10：健康下沉到模型维度（用户口径「健康，启用开关，在模型维度展示」）。
        #: 出参两个字段**必须一起给** —— 前端判据是 `last_health_at is not None AND healthy`，
        #: 只给 `healthy` 会让「从未探测」（默认 False）显示成「异常」。
        "healthy": model.healthy,
        "last_health_at": model.last_health_at.isoformat() if model.last_health_at else None,
        "created_at": model.created_at.isoformat() if model.created_at else None,
        "updated_at": model.updated_at.isoformat() if model.updated_at else None,
    }


def _load_models(db: Session, user_id: int, provider_keys: list[str]) -> dict[str, list[dict]]:
    """按服务商批量取模型行（**一次查完，别在循环里 N+1**）。

    ⚠️ 只返回**用户声明过**的模型行（`llm_gateway_models`），
    不是 `models_json` 的候选池 —— 两者分工见 §13.2.9.4。
    """
    if not provider_keys:
        return {}
    rows = db.scalars(
        select(LlmGatewayModel)
        .where(
            LlmGatewayModel.user_id == user_id,
            LlmGatewayModel.provider_key.in_(provider_keys),
        )
        .order_by(LlmGatewayModel.id.asc())
    ).all()
    # ⚠️ v2.10：模型行需要带「被哪些业务点使用」—— 前端据此把停用开关置灰
    # （用户口径「已在使用的模型无法停用」）。**一次查完所有映射**，别在循环里 N+1。
    mappings = db.scalars(
        select(LlmBusinessProvider).where(LlmBusinessProvider.user_id == user_id)
    ).all()
    usage_by_model: dict[tuple[str, str], list[dict[str, str]]] = {}
    from backend.app.services.business_provider_keys import _BUSINESS_KEY_BY_KEY as _BKEY

    for mp in mappings:
        name = (mp.model_name or "").strip()
        if not name:
            continue
        spec = _BKEY.get(mp.business_key)
        usage_by_model.setdefault((mp.provider_key, name), []).append({
            "business_key": mp.business_key,
            "label": (spec.label if spec else "") or mp.business_key,
            "kind": (spec.kind if spec else ""),
            "platform": mp.platform,
        })

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        item = _serialize_model(row)
        item["usages"] = usage_by_model.get((row.provider_key, row.model_name), [])
        grouped.setdefault(row.provider_key, []).append(item)
    return grouped


def _sync_provider_models_core(
    db: Session,
    current_user: User,
    config: LlmGatewayConfig,
    *,
    platform: str = "",
) -> dict:
    """**同步服务商模型**（v2.9）—— 拉目录 + AI 归纳类型 + upsert 模型行。**唯一实现**。

    用户口径（2026-10-02）：「配置服务商时应直接父子树展示该服务商下所有模型，
    不需要再进入编辑后添加模型，模型的类型……**你来调用总结**」+「添加服务商时默认拉取一次，
    操作列新增『同步服务商模型』按钮」。

    ⚠️ 本函数是「创建时自动拉一次」与「操作列同步按钮」的**同一份实现**
    （DEF-044：同一语义不留两份实现），故抽成 helper，两个调用点都走这里。

    动作序列（每步都可单独失败，失败语义见下）：
      ① 拉 `GET {base_url}/models`（**没有令牌 ⇒ 直接返回 `skipped`，不算失败**）；
      ② 落 `config.models_json`（目录真相源，前端候选池）；
      ③ 调一次 LLM 把整批模型名归纳成类型（`FUNCTION_CLASSIFY` 记账）；
      ④ upsert 模型行：**已存在的行只补 `model_type` 且仅在原来是空值时补**
         （不覆盖用户手工改过的类型 —— 用户的判断优先于 AI）；
         不存在的行新建（类型取归纳结果，无则 `text`），`platform` 并入当前平台。

    ⚠️ **失败不抛**：目录拉不到（网络/401）⇒ 返回 `ok=False` + `reason`，
    让**创建动作本身照常成功**（服务商能建起来，用户稍后重试同步）。
    只有「目录拉取失败」这一件事被如实回报，不把创建打成失败。

    returns: `{"ok", "reason", "catalog_count", "created", "updated", "classified", "total"}`
    """
    import requests as http_requests

    from backend.app.services.model_type_classifier import classify_model_types

    import json as _json

    base_url = (config.base_url or "").rstrip("/")
    access_token = decrypt_text(config.access_token) if config.access_token else ""

    summary = {
        "ok": False,
        "reason": "",
        "catalog_count": 0,
        "created": 0,
        "updated": 0,
        "classified": 0,
        "total": 0,
    }

    # ① 无令牌 ⇒ 拉不到目录，如实回报但不判失败（服务商先建好照样有意义）
    if not access_token:
        summary["reason"] = "未配置访问令牌，跳过模型同步"
        return summary

    try:
        mresp = http_requests.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001 —— 网络失败 ⇒ 回报，不抛
        summary["reason"] = f"服务商不可达: {str(exc)[:120]}"
        return summary

    if mresp.status_code >= 400:
        summary["reason"] = (
            "访问令牌无效或已过期" if mresp.status_code == 401
            else f"服务商不可达 (HTTP {mresp.status_code}) @ {base_url}/models"
        )
        return summary

    try:
        body = mresp.json()
    except Exception:  # noqa: BLE001
        summary["reason"] = f"服务商返回的不是 JSON @ {base_url}/models"
        return summary

    catalog = [item.get("id", "") for item in body.get("data", []) if item.get("id")]
    summary["catalog_count"] = len(catalog)

    # ② 目录落库（真相源）
    config.models_json = _json.dumps(catalog, ensure_ascii=False)
    db.flush()

    if not catalog:
        summary["ok"] = True
        summary["reason"] = "服务商未返回模型列表"
        db.commit()
        return summary

    # ③ AI 归纳整批类型（归纳需要一个文本模型来跑；优先用已声明过的 text/multimodal 行，
    #    否则用目录第一个模型当归纳器 —— 归纳只是「猜类型」，用哪个模型跑不影响结果正确性）
    existing_rows = {
        m.model_name: m
        for m in db.scalars(
            select(LlmGatewayModel).where(
                LlmGatewayModel.user_id == current_user.id,
                LlmGatewayModel.provider_key == config.provider_key,
            )
        ).all()
    }
    runner = next(
        (m for m in existing_rows.values() if m.model_type in ("text", "multimodal")),
        None,
    )
    runner_name = runner.model_name if runner is not None else catalog[0]

    classify_result = classify_model_types(
        model_config=_GatewayProbeConfig(
            base_url=base_url,
            model_name=runner_name,
            provider_key=config.provider_key,
            user_id=current_user.id,
        ),
        api_key=access_token,
        model_names=catalog,
        platform=platform,
    )
    summary["classified"] = classify_result.classified
    summary["total"] = classify_result.total

    # ④ upsert 模型行
    for name in catalog:
        inferred = classify_result.mapping.get(name, "text")
        row = existing_rows.get(name)
        if row is None:
            platform_list = [platform] if platform else []
            db.add(
                LlmGatewayModel(
                    user_id=current_user.id,
                    provider_key=config.provider_key,
                    # ⚠️ v2.10：人类可读唯一 ID（服务商内自然序号）
                    model_key=_next_model_key(db, current_user.id, config.provider_key),
                    model_name=name,
                    model_type=inferred,
                    platform=_dump_platforms({k: 1 for k in platform_list}),
                    enabled=True,
                )
            )
            summary["created"] += 1
        else:
            # 只补「原来为空」的类型 —— 用户手工标过的优先于 AI 判断（AI 是锦上添花，不是覆盖）
            if not row.model_type:
                row.model_type = inferred
                summary["updated"] += 1
            # platform 并入（多平台累积，不覆盖）
            if platform:
                merged = _merge_platforms(_parse_platforms(row.platform), {platform: 1})
                row.platform = _dump_platforms(merged)

    db.commit()

    summary["ok"] = True
    parts = [f"目录 {len(catalog)} 个模型"]
    if summary["created"] or summary["updated"]:
        parts.append(f"新建 {summary['created']} / 补类型 {summary['updated']}")
    if classify_result.ok:
        parts.append(f"AI 归纳 {classify_result.classified}/{classify_result.total}")
    else:
        parts.append(classify_result.reason or "AI 归纳不可用，类型按文本处理")
    summary["reason"] = "; ".join(parts)
    return summary


class _GatewayProbeConfig:
    """归纳调用用的最小模型上下文（`complete_text` 只读这四样）。

    ⚠️ 不复用 `ResolvedModelContext`：那个类型带业务语义（映射解析的产物），
    而归纳是「配置期」动作、**不走业务映射**（此刻还没有任何映射）。
    用一个 4 字段的哑对象最诚实 —— 不会让人误以为归纳经过了业务点解析。
    """

    __slots__ = ("base_url", "model_name", "provider_key", "user_id")

    def __init__(self, *, base_url: str, model_name: str, provider_key: str, user_id: int) -> None:
        self.base_url = base_url
        self.model_name = model_name
        self.provider_key = provider_key
        self.user_id = user_id


def _serialize_config(config: LlmGatewayConfig, models: list[dict] | None = None) -> dict:
    """序列化：令牌字段永不返回，只给 has_access_token 布尔（§10 安全设计）。

    v1.6：`models` = models_json 解析出的模型目录（供前端下拉；无则空列表）。
    v1.7：`local_db_path` 出参移除（New API 特化字段，列保留不删、新代码不读写）。
    v1.9：新增 `provider_key`（唯一 KEY，前端只读展示）与 `platform`（使用平台）。
    ⚠️ **v2.7（§13.2.9.9）**：
      - `platform` / `model_type` **出参移除**（已下沉到模型层）；
      - `models` 由「字符串数组（目录）」升级为**结构数组**（用户声明过的模型行，含
        type/platform/enabled）；原目录候选池改名为 `catalog`（拉上游 `/models` 的结果）。
    """
    catalog = _parse_models_json(config.models_json)
    return {
        "id": config.id,
        "name": config.name,
        "provider_key": config.provider_key,
        "base_url": config.base_url,
        #: v2.8：`default_model` 出参已移除（列已删，§13.2.10）。
        #: v2.7：**用户声明过的模型行**（结构数组，含 model_type / platform / enabled）。
        "models": models if models is not None else [],
        #: v2.7：上游 `/models` 拉到什么（字符串数组，下拉候选池；未配置的模型不在此列）。
        "catalog": catalog,
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


def _validate_model_row(item: "GatewayModelInput", *, platform_default: str = "") -> None:
    """校验单个模型行（v2.7 建立，§13.2.9.6）：类型 ∈ `MODEL_TYPES`、平台 ⊆ `PLATFORM_VALUES`。

    ⚠️ 类型**必填**（用户声明，不猜、不给默认）——
    缺省即 400，而不是静默填 `text`：静默填会让「我配的是图像，怎么按文本调用了」
    变成无解之谜（同 §13.2.1a「判不准时也不猜 image」的纪律）。
    ⚠️ **平台 v2.9 起允许多值也可为空**（用户口径「多个平台共用一个模型」）：
    旧口径「平台必填」**已被取代** —— 现在空数组是合法态（模型刚同步进来、还没在任何
    平台配过；展示为 `-`）。非空时**逐个**校验都在 `PLATFORM_VALUES` 内。

    `platform_default` = 平台上下文（§13.2.9.7）：调用方若在某个平台上下文里配置模型，
    可把当前平台作为**请求未带 platform 时**的兜底 —— **仅在此显式传入时生效**，
    不在本函数内隐式取全局值（隐式会让「谁写的这行」无从追查）。
    """
    from backend.app.services.business_provider_keys import MODEL_TYPES

    if not item.model_name.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "LLM_PROVIDER_MODEL_NAME_REQUIRED", "message": "模型名必填"},
        )
    model_type = (item.model_type or "").strip()
    if model_type not in MODEL_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "LLM_PROVIDER_MODEL_TYPE_REQUIRED",
                "message": (
                    f"模型「{item.model_name}」的能力类型必填，"
                    f"且需为 {sorted(MODEL_TYPES)} 之一"
                ),
            },
        )
    #: ⚠️ v2.9：platform 是多值数组；空数组合法（允许「还没配过平台」）。
    #: 兜底：请求未带 platform 且调用方给了平台上下文 ⇒ 用上下文平台（§13.2.9.7）。
    platforms = [str(p).strip() for p in (item.platform or []) if str(p).strip()]
    if not platforms and platform_default:
        platforms = [platform_default]
    for value in platforms:
        if value not in PLATFORM_VALUES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "LLM_PROVIDER_PLATFORM_REQUIRED",
                    "message": (
                        f"模型「{item.model_name}」的使用平台「{value}」非法，"
                        f"需为 {sorted(PLATFORM_VALUES)} 之一"
                    ),
                },
            )


def _validate_models(items: list["GatewayModelInput"], *, platform_default: str = "") -> list["GatewayModelInput"]:
    """校验一批模型行：逐个过 `_validate_model_row` + **同批重名拦**（v2.7）。

    ⚠️ **同批重名**（同一请求里同一 `model_name` 出现两次）⇒ 400：
    DB 唯一约束 `(user_id, provider_key, model_name)` 会拦，但那会抛
    `IntegrityError`（500 且文案不可读）；提前拦能给出**可读的** 400。
    """
    seen: set[str] = set()
    cleaned: list[GatewayModelInput] = []
    for item in items:
        _validate_model_row(item, platform_default=platform_default)
        name = item.model_name.strip()
        if name in seen:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "LLM_PROVIDER_MODEL_DUPLICATE",
                    "message": f"模型「{name}」在本次请求里重复出现",
                },
            )
        seen.add(name)
        if name != item.model_name:
            item = item.model_copy(update={"model_name": name})
        cleaned.append(item)
    return cleaned


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
    """新增服务商（v1.9；v2.7 模型列表随建）。

    ⚠️ **两件必填**（用户口径「访问令牌和默认模型必填」）：
    任一为空/非法 ⇒ 400 结构化错误（**不静默用默认值补齐** —— 补齐等于替用户
    编造一份凭据，第一次调用才发现是错的）。
    ⚠️ `provider_key` **由服务端生成**（D1），请求体不收该字段。
    ⚠️ **v2.7（§13.2.9.9）**：`platform` / `model_type` 不再是服务商字段 ——
    改为 `models: [{model_name, model_type, platform, ...}]`，每个模型自带类型与平台。
    每个模型行都要过一次校验（类型 ∈ `MODEL_TYPES`、平台 ∈ `PLATFORM_VALUES`）。
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

    # v2.7：校验每个模型行的类型与平台（任一非法 ⇒ 400，不静默纠偏）
    model_rows = _validate_models(payload.models)
    if not (payload.access_token or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "LLM_PROVIDER_TOKEN_REQUIRED", "message": "访问令牌必填"},
        )
    # v2.8：`LLM_PROVIDER_MODEL_REQUIRED`（默认模型必填）校验已随 `default_model` 列删除（§13.2.10）。

    config = LlmGatewayConfig(
        user_id=current_user.id,
        provider_key=_generate_provider_key(db, current_user.id, payload.name),
        name=payload.name,
        base_url=_clean_base_url(payload.base_url or DEFAULT_BASE_URL),
        access_token=encrypt_text(payload.access_token) if payload.access_token else "",
    )
    db.add(config)
    db.flush()  # 取 config.id / provider_key 后再落模型行（同一事务）
    # ⚠️ v2.10：一批模型在同请求里创建 ⇒ 先取一次基点，再按位置递增。
    # 不能每行都调 `_next_model_key` —— 前面的行还没 flush，查库看不到它们，
    # 会让整批拿到同一个序号（重号）。
    next_key = int(_next_model_key(db, current_user.id, config.provider_key))
    for offset, item in enumerate(model_rows):
        db.add(
            LlmGatewayModel(
                user_id=current_user.id,
                provider_key=config.provider_key,
                model_key=str(next_key + offset),
                model_name=item.model_name,
                model_type=item.model_type,
                # ⚠️ v2.9：platform 是多值列表 ⇒ 落库为 JSON 数组字符串（去重 + 保序）
                platform=_dump_platforms({k: 1 for k in item.platform}),
                enabled=item.enabled,
                remark=item.remark,
            )
        )
    db.commit()
    db.refresh(config)
    # 设计 17 §4.5（2026-10-02）：新网关按「默认预警规则」补种（enabled=true 即生效）。
    # 幂等：同网关已有同名规则不重复插。存量网关**不**主动补种（不替用户开通知开关）。
    llm_gateway_monitor_service.ensure_default_alerts_for_gateway(
        db, user_id=current_user.id, gateway_id=config.id
    )
    # ⚠️ v2.9（用户口径 2026-10-02「**添加服务商时默认拉取一次**」）：
    # 建完即拉一次目录 + AI 归纳类型。**失败不抛**（`_sync_provider_models_core` 内部收敛）——
    # 服务商能建起来是第一位的，同步失败只如实回报，用户可在操作列点「同步服务商模型」重试。
    sync = _sync_provider_models_core(db, current_user, config)
    result = _serialize_config(
        config, _load_models(db, current_user.id, [config.provider_key]).get(config.provider_key, [])
    )
    # ⚠️ 把同步结果挂在响应上（不额外查库）：前端据此提示「已同步 N 个模型」或失败原因
    result["sync"] = sync
    return result


@router.patch("/{config_id}")
def update_gateway_config(
    config_id: int,
    payload: GatewayConfigUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """编辑服务商（v1.9；v2.7 支持随改 upsert 模型行）。

    ⚠️ **停用/开启**（`enabled`）的业务语义（D4，§3.8）：停用 ⇒ **业务取不到该服务商**
    —— 调用链按 `provider_key` 解析时命中 `enabled=False` ⇒ 抛结构化错误
    `LLM_PROVIDER_DISABLED`，**不静默回落到其它服务商**（回落 = 用错凭据调用，
    用户看不到任何提示，只会困惑「为什么调用的是 A 却报 B 的错」）。
    前端表格的开关即此字段的唯一入口。

    ⚠️ **v2.7（§13.2.9.9）**：`platform` / `model_type` 不再是服务商字段；
    改为 `models` 列表 —— **只增改不删**（未传的既有模型行不动；删除走
    `DELETE /{id}/models/{model_id}`）。
    """
    config = _get_owned_config(db, current_user, config_id)

    if payload.name is not None:
        config.name = payload.name
    if payload.base_url is not None:
        new_base = _clean_base_url(payload.base_url)
        # 改 base_url 后 healthy 自动置 False（需重新探测，§7.1 契约）
        if new_base != config.base_url:
            config.base_url = new_base
            config.healthy = False
    if payload.access_token is not None:
        config.access_token = encrypt_text(payload.access_token) if payload.access_token else config.access_token
    # v2.8：`default_model` 处理已随列删除（§13.2.10）。
    if payload.enabled is not None:
        config.enabled = payload.enabled
    # ⚠️ `provider_key` **不在此处更新**（用户口径「新增后不可编辑」）。
    # 请求体带它 ⇒ 422（`GatewayConfigUpdateRequest` 未声明该字段 + extra="forbid"）。
    # 服务编码一旦生成即是对外契约（业务代码引用它），改名会让引用悬空。

    # v2.7：模型行 upsert（**只增改不删**）
    if payload.models:
        cleaned = _validate_models(payload.models)
        existing = {
            m.model_name: m
            for m in db.scalars(
                select(LlmGatewayModel).where(
                    LlmGatewayModel.user_id == current_user.id,
                    LlmGatewayModel.provider_key == config.provider_key,
                )
            ).all()
        }
        # ⚠️ v2.10：本批新增的行要拿**连续且不与既有重号**的 `model_key` ——
        # 同 `create` 里那条：一次性取基点，再按位置递增（逐行查库会因未 flush 而重号）。
        next_key = int(_next_model_key(db, current_user.id, config.provider_key))
        for item in cleaned:
            row = existing.get(item.model_name)
            if row is None:
                db.add(
                    LlmGatewayModel(
                        user_id=current_user.id,
                        provider_key=config.provider_key,
                        model_key=str(next_key),
                        model_name=item.model_name,
                        model_type=item.model_type,
                        # ⚠️ v2.9：多值列表 ⇒ JSON 数组字符串
                        platform=_dump_platforms({k: 1 for k in item.platform}),
                        enabled=item.enabled,
                        remark=item.remark,
                    )
                )
                next_key += 1
            else:
                row.model_type = item.model_type
                # ⚠️ v2.28：改为**并入**（不是覆盖）—— 与「配置时同步」同一口径。
                # 旧 v2.9 的覆盖语义会让「PATCH 服务商时漏传 platform」静默清空
                # 该模型已有的使用平台（用户报告的「平台莫名被移除」的真身）。
                row.platform = _dump_platforms(
                    _merge_platforms(
                        _parse_platforms(row.platform), {k: 1 for k in item.platform}
                    )
                )
                row.enabled = item.enabled
                row.remark = item.remark

    db.commit()
    db.refresh(config)
    return _serialize_config(
        config,
        _load_models(db, current_user.id, [config.provider_key]).get(config.provider_key, []),
    )


@router.post("/{config_id}/sync-models")
def sync_gateway_models(
    config_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**同步服务商模型**（v2.9，设计 17 §13.2.11.5）—— 操作列的「同步服务商模型」按钮。

    用户口径（2026-10-02）：「配置服务商时应直接父子树展示该服务商下所有模型，
    不需要再进入编辑后添加模型，模型的类型……你来调用总结」+「操作列新增『同步服务商模型』按钮」。

    动作 = 拉目录 + AI 归纳类型 + upsert 模型行（**与创建时自动拉取同一份实现**，
    见 `_sync_provider_models_core`，DEF-044）。

    ⚠️ **总是 200**（除非越权 404）：同步结果的好坏在响应体的 `ok` / `reason` 里，
    不靠 HTTP 状态码 —— 这是「动作已执行、结果是另一件事」的语义（同 `/test` 的口径）。
    前端据 `ok` 决定提示色（成功绿 / 失败黄，且**必须**展示 `reason`，不吞）。
    """
    config = _get_owned_config(db, current_user, config_id)
    sync = _sync_provider_models_core(db, current_user, config)
    db.refresh(config)
    return {
        "id": config.id,
        "sync": sync,
        "catalog": _parse_models_json(config.models_json),
        "models": _load_models(db, current_user.id, [config.provider_key]).get(config.provider_key, []),
    }


# ⚠️ **路由顺序**：`/{config_id}/models` 与 `/{config_id}/models/{model_id}` 之间
# **不能插进 `@router.get("/models")` 这类字面量同级路径** —— `/models` 会被
# `/{config_id}/models` 抢走（Starlette 按声明顺序匹配，`config_id="models"` 解析 int 失败 ⇒ 404）。
# v2.9 的「服务商 × 模型」**两级列表**就落在本文件内，用**一个**端点
# `GET /llm/gateway-configs/models-tree`（在下方 `def list_gateway_models` 之后定义）——
# 为什么不让前端自己把 `GET /` + 每服务商 `GET /{id}/models` 拼起来：
#   服务商 N 个 ⇒ N+1 次请求，且中间的失败态要前端自己合成（第 3 个失败算整表失败还是空？）。
#   一个端点 = 一次查库（`_load_models` 已支持多 provider_key 批量取）+ 一个确定的失败语义。


@router.get("/{config_id}/models")
def list_gateway_models(
    config_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出该服务商下**用户声明过**的模型行（v2.7，§13.2.9.9）。

    ⚠️ 返回的是 `llm_gateway_models` 的行（含 type/platform），**不是** `models_json`
    目录候选池 —— 后者在 `GET /{id}` 的 `catalog` 字段里（§13.2.9.4 分工）。
    """
    config = _get_owned_config(db, current_user, config_id)
    models = _load_models(db, current_user.id, [config.provider_key]).get(config.provider_key, [])
    return {"items": models}


@router.get("/models-tree")
def get_gateway_models_tree(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """服务商 × 模型**两级列表**（v2.9，用户口径「服务商使用父子列表展示」）。

    出参 = 服务商数组，每项 = `_serialize_config` 的服务商字段 + `models`（该服务商下
    的全部模型行）。前端据此渲染**父子两张独立表**（父表列 = 服务商维度；
    子表列 = 模型维度），**不需要再为每个服务商各发一次 `GET /{id}/models`**。

    ⚠️ **为什么要有这个端点**（而不是让前端拼 N+1 次请求）：
      ① 一次查库：`_load_models` 本就按 provider_key 批量取（**不做 N+1**）；
      ② 失败语义确定：前端拼装时「第 3 个服务商的模型没拉到」到底是整表算失败还是
         某行算空？这里没有这个模糊地带 —— 取不到就是整个端点失败（一次请求一个结果）。
    ⚠️ 语义与 `GET /` **完全一致**（同样的过滤 / 排序 / 分页 / 序列化），只是每项的
    `models` 真的装了模型行 —— `GET /` 出于「列表不必带子表」的历史原因下发了空数组
    （见 `_serialize_config` 的 `models` 形参默认 `None ⇒ []`），前端不想为每行再拉一次时
    就用本端点。两者共用同一个序列化函数，**同一语义不留两份实现**。

    ⚠️ 出参**不带令牌**（`_serialize_config` 只给 `has_access_token`）；
    模型行也没有令牌字段（凭据在服务商层）。
    """
    configs = db.scalars(
        select(LlmGatewayConfig)
        .where(LlmGatewayConfig.user_id == current_user.id)
        .order_by(LlmGatewayConfig.id.desc())
    ).all()
    models_of = _load_models(db, current_user.id, [c.provider_key for c in configs])
    items = [
        _serialize_config(c, models_of.get(c.provider_key, []))
        for c in configs
    ]
    return paginated(items, page, page_size)


@router.post("/{config_id}/models", status_code=status.HTTP_201_CREATED)
def create_gateway_model(
    config_id: int,
    payload: GatewayModelInput,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """为服务商新增一个模型行（v2.7，§13.2.9.9）。

    校验：类型 ∈ `MODEL_TYPES`、平台 ∈ `PLATFORM_VALUES`（`_validate_model_row`）；
    同服务商下同名模型已存在 ⇒ 409（不静默覆盖，避免「我以为改了其实没改」）。
    """
    config = _get_owned_config(db, current_user, config_id)
    _validate_model_row(payload)
    exists = db.scalar(
        select(LlmGatewayModel).where(
            LlmGatewayModel.user_id == current_user.id,
            LlmGatewayModel.provider_key == config.provider_key,
            LlmGatewayModel.model_name == payload.model_name.strip(),
        )
    )
    if exists is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "LLM_PROVIDER_MODEL_EXISTS",
                "message": f"模型「{payload.model_name}」已在本服务商下配置过",
            },
        )
    row = LlmGatewayModel(
        user_id=current_user.id,
        provider_key=config.provider_key,
        # ⚠️ v2.10：人类可读唯一 ID（服务商内自然序号）
        model_key=_next_model_key(db, current_user.id, config.provider_key),
        model_name=payload.model_name.strip(),
        model_type=payload.model_type,
        # ⚠️ v2.9：多值列表 ⇒ JSON 数组字符串
        platform=_dump_platforms({k: 1 for k in payload.platform}),
        enabled=payload.enabled,
        remark=payload.remark,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_model(row)


def _business_usages_of_model(
    db: Session, user_id: int, provider_key: str, model_name: str
) -> list[dict[str, str]]:
    """该**模型行**正在被哪些业务点引用（v2.10，2026-10-02 用户口径）。

    用户口径：「已在使用的模型无法停用」—— 判据是「有没有业务点映射指向它」，
    真相源 = `llm_business_providers`（v2.3 起业务点必须显式选定服务商 + 模型）。

    返回结构数组（供前端/测试展示「被谁在用」）：`[{business_key, label, kind, platform}]`。
    ⚠️ 匹配口径 = `(provider_key, model_name)` **两者都命中**（同一模型名可能存在于
    多个服务商下，只按名字匹配会误伤）。空 `model_name` 不参与（不可能被引用）。
    """
    from backend.app.services.business_provider_keys import _BUSINESS_KEY_BY_KEY

    if not provider_key or not model_name:
        return []
    rows = db.scalars(
        select(LlmBusinessProvider).where(
            LlmBusinessProvider.user_id == user_id,
            LlmBusinessProvider.provider_key == provider_key,
            LlmBusinessProvider.model_name == model_name,
        )
    ).all()
    out: list[dict[str, str]] = []
    for row in rows:
        spec = _BUSINESS_KEY_BY_KEY.get(row.business_key)
        out.append({
            "business_key": row.business_key,
            "label": (spec.label if spec else "") or row.business_key,
            "kind": (spec.kind if spec else ""),
            "platform": row.platform,
        })
    return out


def _get_owned_model(db: Session, current_user: User, config: LlmGatewayConfig, model_id: int) -> LlmGatewayModel:
    """取该服务商下的模型行（三重归属：用户 / 服务商 / 行 id）。越权或不存在 ⇒ 404。"""
    row = db.get(LlmGatewayModel, model_id)
    if (
        row is None
        or row.user_id != current_user.id
        or row.provider_key != config.provider_key
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "LLM_PROVIDER_MODEL_NOT_FOUND", "message": "模型配置不存在"},
        )
    return row


@router.patch("/{config_id}/models/{model_id}")
def update_gateway_model(
    config_id: int,
    model_id: int,
    payload: "GatewayModelUpdateRequest",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """改模型行（类型 / 平台 / 启停 / 备注）（v2.7，§13.2.9.9）。

    ⚠️ **改 `model_type` 会让已指向该模型的业务点映射失效** —— 后端类型校验
    （`resolve_business_provider`）会拦，前端须在编辑弹窗提示这个后果（不许静默）。
    """
    config = _get_owned_config(db, current_user, config_id)
    row = _get_owned_model(db, current_user, config, model_id)

    if payload.model_type is not None:
        from backend.app.services.business_provider_keys import MODEL_TYPES

        if payload.model_type not in MODEL_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "LLM_PROVIDER_MODEL_TYPE_REQUIRED",
                    "message": f"模型类型须为 {sorted(MODEL_TYPES)} 之一",
                },
            )
        row.model_type = payload.model_type
    if payload.platform is not None:
        # ⚠️ v2.9：platform 是多值数组。
        # `[]`（显式传空）= **清空**该模型的平台列表（PATCH 覆盖语义）；
        # 非空时逐值校验 ∈ `PLATFORM_VALUES`。
        platforms = [str(p).strip() for p in payload.platform if str(p).strip()]
        for value in platforms:
            _validate_platform(value)
        row.platform = _dump_platforms({k: 1 for k in platforms})
    if payload.enabled is not None:
        # ⚠️ v2.10（2026-10-02 用户口径「已在使用的模型无法停用」）：
        # 停用前必须检查有没有业务点映射指向它 —— 有 ⇒ 400 结构化拒绝（**不静默停用**）。
        # 为什么拦在**停用**这一侧（而不是允许停用、等调用时报错）：
        #   1. 映射指向已停用模型 = 该业务点**每次调用都必失败**（`resolve_business_provider`
        #      会按业务点直接取到它），属于「配好了但永远跑不通」的死配置；
        #   2. 允许停用会让故障延迟到「用户点生成笔记时」才暴露，那时现场已离开配置页，
        #      排查成本高得多（同 DEF-048「看起来正常但实际不可用」的事故形态）。
        # 出路：先到「模型配置页」把指向它的业务点改到别的模型，再回来停用。
        # ⚠️ 启用（False→True）**不拦**：那是在修复上面那个死配置。
        if payload.enabled is False and row.enabled:
            usages = _business_usages_of_model(
                db, current_user.id, row.provider_key, row.model_name
            )
            if usages:
                labels = "、".join(u["label"] for u in usages)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "LLM_MODEL_IN_USE_BY_BUSINESS",
                        "message": (
                            f"模型「{row.model_name}」正在被 {len(usages)} 个业务点使用"
                            f"（{labels}），请先在模型配置页改到别的模型再停用"
                        ),
                        "usages": usages,
                    },
                )
        row.enabled = payload.enabled
    if payload.remark is not None:
        row.remark = payload.remark

    db.commit()
    db.refresh(row)
    return _serialize_model(row)


@router.delete("/{config_id}/models/{model_id}", status_code=status.HTTP_200_OK)
def delete_gateway_model(
    config_id: int,
    model_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除模型行（v2.7，§13.2.9.9）。

    ⚠️ **真删**（与服务商「410 墓碑」不同）：模型行是**用户声明**，删了不影响凭据；
    但若已有业务点映射指向它，`resolve_business_provider` 会抛
    `LLM_BUSINESS_MODEL_NOT_CONFIGURED`（**明确报错，不静默降级**）——
    删除前前端应提示「有 N 个业务点正在用它」。
    """
    config = _get_owned_config(db, current_user, config_id)
    row = _get_owned_model(db, current_user, config, model_id)
    db.delete(row)
    db.commit()
    return {"deleted": model_id}


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
    →（可选）② 一次极小 chat 请求验证**模型行的第一个模型**。
    结果写 healthy / last_health_at；令牌错 401 LLM_GATEWAY_AUTH_FAILED。

    ⚠️ **v2.8（§13.2.10）**：第②步的探活模型不再是 `default_model`（列已删）——
    改为取该服务商**模型行的第一条 text 类型模型**（按 id 升序）；
    **无 text 模型行时跳过 chat 探测**（只探 `/models` 目录），不臆造一个模型名去试。

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

    # ② 极小 chat 请求验证模型（v2.8：取模型行第一条 text 类型模型；无则跳过）
    # ⚠️ **不猜模型名** —— 目录里有名字 ≠ 用户标过它是 text 类型（§13.2.1a 纪律延续）。
    probe_model = db.scalars(
        select(LlmGatewayModel)
        .where(
            LlmGatewayModel.user_id == current_user.id,
            LlmGatewayModel.provider_key == config.provider_key,
            LlmGatewayModel.model_type == "text",
        )
        .order_by(LlmGatewayModel.id.asc())
    ).first()
    chat_message = ""
    if access_token and probe_model is not None:
        try:
            cresp = http_requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json={"model": probe_model.model_name, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5},
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
    # ⚠️ **v2.10：健康下沉到模型维度**（用户口径「健康，启用开关，在模型维度展示」）。
    # 本次探测只要走到这里，就说明**该服务商的凭据与地址是通的** —— 而凭据/地址是
    # 其下**所有模型共用**的（§13.2.9.2 用户口径「服务商信息是其下的模型公用的」），
    # 故服务商下的模型行统一置为「已探测且正常」。
    # ⚠️ **不逐个模型发 chat 请求**：N 个模型 = N 次上游调用，探测端点会变成慢接口，
    # 且多数上游对不存在的模型名返回 404 而非「不健康」（会把探测结果污染成假阴性）。
    # 判据仍严格：`healthy=True` **且** `last_health_at` 非空才算「正常」（前端同判）。
    # ⚠️ 失败路径（上面各 `except`）**不给模型行盖章** —— 保持原有值：
    # 「探测失败」不等于「模型坏了」（可能只是网络抖动），不该把上一轮的好结果抹掉。
    probe_time = shanghai_now()
    for row in db.scalars(
        select(LlmGatewayModel).where(
            LlmGatewayModel.user_id == current_user.id,
            LlmGatewayModel.provider_key == config.provider_key,
        )
    ).all():
        row.healthy = True
        row.last_health_at = probe_time
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
    #: v2.7：服务商 → **模型行列表**（含 name/type/platform/enabled）。
    #: 取代 v2.0 的 `type_of = {provider_key: config.model_type}`（服务商已无 model_type）。
    #: 前端据此做「选完服务商后，模型下拉按业务点 kind 过滤」（§13.2.9.6）。
    provider_models = _load_models(db, current_user.id, [c.provider_key for c in configs])
    #: v2.3：服务商 → 模型**目录**（上游候选池，字符串数组；供前端提示「还没配的候选」）
    models_of = {c.provider_key: _parse_models_json(c.models_json) for c in configs}

    items = []
    for item in BUSINESS_KEYS:
        key = (platform or PLATFORM_ENUM[0][0], item.key)
        row = by_key.get(key)
        pk = (row.provider_key if row else "") or ""
        model_name = (row.model_name if row else "") or ""
        #: v2.7：该映射所选模型在模型层的类型（诊断「类型不匹配」用；配置正确时应恒等于 kind）
        model_type = ""
        for m in provider_models.get(pk, []):
            if m["model_name"] == model_name:
                model_type = m["model_type"]
                break
        #: v2.17：**兼容性判据由后端唯一裁定并下发**（`_type_satisfies`）——
        #: 多态模型（multimodal）可服务 text / image 两类业务点。
        #: ⚠️ 前端**不许**自己写 `model_type == kind` 比较：那是本判据的第二份实现，
        #: 必然与 `_type_satisfies` 漂移（2026-10-03 用户实测：多模态服务商在模型配置页
        #: 被误报「类型不匹配」，而后端写入校验**允许**该配置 —— 两处判据不一致，
        #: 正是 DEF-044「同一语义不留两份实现」的违约）。
        from backend.app.services.llm_gateway_client import _type_satisfies

        type_satisfied = (
            bool(model_type) and _type_satisfies(kind=item.kind, model_type=model_type)
        )
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
            #: v2.7：映射所选**模型**的当前类型（诊断用；恒等于 kind 才是配好的）
            "provider_model_type": model_type or None,
            #: v2.17：**兼容性判据**（后端唯一裁定）—— 前端「类型不匹配」只看这个布尔，
            #: 不再自己比较 `provider_model_type === kind`（多态模型会被误报）。
            "type_satisfied": type_satisfied,
            #: v2.3：已选具体模型 + 该服务商的模型目录（前端模型下拉候选）
            "model_name": model_name,
            "provider_models": models_of.get(pk, []) if pk else [],
            #: v2.7：该服务商的**模型行**（带 type/platform，供「模型下拉按 kind 过滤」）
            "provider_model_rows": provider_models.get(pk, []) if pk else [],
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
      3. ⚠️ **v2.7**：`provider_key` 非空时 `model_name` 必须**已配置成模型行**
         （`llm_gateway_models`）⇒ 否则 400 `LLM_BUSINESS_MODEL_NOT_CONFIGURED`；
         且该模型行的 `model_type` 必须等于业务点的 `kind`
         ⇒ 否则 400（否则会拿文本模型去打图像接口，报一个看不懂的上游错）。
         ~~旧口径：服务商的 `model_type` 必须等于 kind~~（v2.7 已被取代，判据下沉到模型层）。
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
    #: 命中的模型行（**显式初始化**）：下面「配置时同步」要用它写 `platform`，
    #: 而清空映射那条路不会赋值 ⇒ 不初始化会在 `model_row is not None` 处 NameError。
    #: 不靠「provider_key 非空才走到」的隐式依赖 —— 那种正确性一改条件就断。
    model_row: LlmGatewayModel | None = None

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
        # ⚠️ v2.7（§13.2.9.6）：类型匹配改判**模型行** —— 目录里有名字 ≠ 用户标过类型。
        model_row = db.scalar(
            select(LlmGatewayModel).where(
                LlmGatewayModel.user_id == current_user.id,
                LlmGatewayModel.provider_key == provider_key,
                LlmGatewayModel.model_name == model_name,
            )
        )
        if model_row is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "LLM_BUSINESS_MODEL_NOT_CONFIGURED",
                    "message": (
                        f"模型「{model_name}」还没标注能力类型，"
                        f"请先到服务商配置页配置该模型"
                    ),
                },
            )
        # ⚠️ v2.9（§13.2.11.4）：判据与 `resolve_business_provider` **同一份**
        # （`_type_satisfies`）—— 多态模型（multimodal）可服务 text/image 两类业务点。
        # ⚠️ 两处判据**必须同改**：本端点拦在写入前，调用层拦在解析时；
        # 若这里用严格相等而调用层用放宽，会出现「配得进但调不通」（最难查的形态）。
        from backend.app.services.llm_gateway_client import _type_satisfies

        if not _type_satisfies(kind=item.kind, model_type=model_row.model_type):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "LLM_BUSINESS_PROVIDER_TYPE_MISMATCH",
                    "message": (
                        f"业务点「{item.key}」需要 {item.kind} 类型模型，"
                        f"而「{config.name}」下的「{model_name}」是 {model_row.model_type} 类型"
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
    # ⚠️ v2.28（§13.2.9.7c）：**使用平台改为引用计数** —— 增减必须知道「改之前指向谁」，
    # 所以这段必须放在**读出旧行之后**（早期版本写在上面，看不到旧值，只能盲目并入）。
    # 计数语义 = 「该平台下指向该模型的业务点个数」（**状态**，不是被操作次数）。
    previous_provider_key = row.provider_key if row is not None else ""
    previous_model_name = row.model_name if row is not None else ""

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

    # 只有「指向的模型真的变了」才增减（幂等 upsert 同一目标 ⇒ 计数不动）
    changed = (previous_provider_key, previous_model_name) != (provider_key, model_name)
    if changed:
        # ① **减**：旧模型在该平台的引用 −1（减到 0 即移除该平台 —— 用户口径）
        if False and previous_provider_key and previous_model_name:
            old_row = _find_model_row(db, current_user.id, previous_provider_key, previous_model_name)
            if old_row is not None:
                old_row.platform = _dump_platforms(
                    _bump_platform(_parse_platforms(old_row.platform), payload.platform, -1)
                )
        # ② **加**：新模型在该平台的引用 +1
        if provider_key and model_name and model_row is not None:
            model_row.platform = _dump_platforms(
                _bump_platform(_parse_platforms(model_row.platform), payload.platform, +1)
            )
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
    from backend.app.services.business_provider_keys import (
        BUSINESS_KEYS,
        MODEL_TYPE_LABELS,
        MODEL_TYPES,
    )

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
        #: ⚠️ v2.7：`model_types` 是**模型级**能力类型枚举（下拉用），与业务点 `kind` 同源
        #: （`MODEL_TYPES`）。原字面量已改为从常量读 —— 避免与 `business_provider_keys`
        #: 各留一份（DEF-044：同一语义不留两份实现）。
        #: ⚠️ v2.9：中文映射改读 `MODEL_TYPE_LABELS`（本端点原写死 `{text:文本,image:图像}`，
        #: 扩枚举后会静默漏掉 multimodal/video ⇒ 改成读唯一映射点）。
        "model_types": [
            {"value": value, "label": MODEL_TYPE_LABELS.get(value, value)}
            for value in sorted(MODEL_TYPES)
        ],
    }
