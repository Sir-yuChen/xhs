"""M2：`resolve_business_provider` —— 「平台 × 业务点 → 服务商」映射解析
（设计 17 §13.2.3 / §13.2.4，v2.0）。

本文件钉死四条契约，对应卡内要求「四种失败各有实测，都报可读错且不回落」：

1. **映射不存在** ⇒ `LLM_BUSINESS_PROVIDER_NOT_CONFIGURED`（§13.2.3 定稿：
   **不回落**到其它服务商或其它业务点的映射 —— 回落 = 用错服务商还不报错）。
2. **类型不匹配** ⇒ `LLM_BUSINESS_PROVIDER_TYPE_MISMATCH`（§13.2.1a：
   业务点的 `kind` 必须匹配模型行的 `model_type`；提前拦，否则会拿文本模型
   打 `/images/generations`，报一个看不懂的上游错）。
   ⚠️ **v2.9**：判据由「严格相等」改为 `_type_satisfies` —— `multimodal` 同时满足
   `text` 与 `image`（多态模型两头都能用）；`text` / `image` / `video` 之间仍互不通用。
3. **服务商不存在** ⇒ `LLM_PROVIDER_NOT_FOUND`（`provider_key` 指向的行没了）。
4. **服务商停用** ⇒ `LLM_PROVIDER_DISABLED`（D4：**不回落**到其它服务商）。

外加两条正路径断言（命中文本 / 命中图片）与一条跨用户隔离。

⚠️ 直接测**服务层函数**（不经 HTTP）：这是「唯一入口」契约，端点层的映射
（`api/ai.py::_business_model_context` → 400 + 结构化 code）由 `test_api.py`
的 AI 用例覆盖，这里只管解析本身的语义。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from backend.app.core.security import encrypt_text
from backend.app.models import LlmGatewayConfig, User
from tests.backend.conftest import upsert_business_mapping
from backend.app.models.llm_gateway import LlmGatewayModel
from backend.app.services import llm_gateway_client
from backend.app.services.business_provider_keys import (
    BUSINESS_KEY_GENERATE_COVER,
    BUSINESS_KEY_LLM_NOTE_TEXT,
)
from backend.app.services.llm_gateway_client import (
    BusinessProviderNotConfiguredError,
    BusinessProviderTypeMismatchError,
    LLMGatewayError,
    ProviderDisabledError,
    ProviderNotFoundError,
)


def _seed_user(db: Session, username: str = "bp_tester") -> User:
    user = User(username=username, password_hash="x")
    db.add(user)
    db.flush()
    return user


def _seed_gateway(
    db: Session,
    user: User,
    *,
    provider_key: str = "BP-TEXT",
    model_type: str = "text",
    enabled: bool = True,
    default_model: str = "deepseek-v4-flash",
) -> LlmGatewayConfig:
    """种一条服务商行 + 一条模型行。

    ⚠️ **v2.7/v2.8**：服务商层已无 `platform` / `model_type` / `default_model`
    三列（前两者下沉到 `llm_gateway_models`，`default_model` 整列删除）。
    故本辅助函数把「能力类型 / 模型名」写进**模型行**——`resolve_business_provider`
    的类型匹配判据自 v2.7 起也读模型行（§13.2.9.6）。
    `default_model` 参数保留只为**兼容旧调用点签名**：它现在只用于给模型行取名，
    不再代表「服务商兜底模型」（该语义 v2.8 已废除）。
    """
    config = LlmGatewayConfig(
        user_id=user.id,
        name=f"bp-{model_type}-{provider_key}",
        provider_key=provider_key,
        base_url="https://api.example.test/v1",
        access_token=encrypt_text("***"),
        models_json="[]",
        enabled=enabled,
        healthy=True,
    )
    db.add(config)
    db.commit()
    db.refresh(config)

    # ⚠️ v2.7：能力类型在模型行；本用例的默认模型就是映射将要选的那个名字口径
    db.add(
        LlmGatewayModel(
            user_id=user.id,
            provider_key=provider_key,
            model_name=default_model,
            model_type=model_type,
            platform="xhs",
            enabled=True,
        )
    )
    db.commit()
    return config


def _seed_mapping(
    db: Session,
    user: User,
    business_key: str,
    provider_key: str,
    *,
    model_name: str = "deepseek-v4-flash",
) -> None:
    """种一条映射行。

    ⚠️ v2.3（设计 17 §13.2.3）：映射必须带**具体模型**（`model_name` 非空），
    否则 `resolve_business_provider` 会抛 `LLM_BUSINESS_MODEL_REQUIRED` —— 默认给一个
    合法值，让「四态」用例聚焦各自想测的失败分支（要测缺模型请显式传 `model_name=""`）。
    """
    upsert_business_mapping(
        db,
        user_id=user.id,
        business_key=business_key,
        provider_key=provider_key,
        model_name=model_name,
    )
    db.commit()


# --------------------------------------------------------------------------- #
# ① 映射不存在 ⇒ NOT_CONFIGURED（不回落）
# --------------------------------------------------------------------------- #


def test_missing_mapping_raises_not_configured(db_factory):
    """没配映射 ⇒ `LLM_BUSINESS_PROVIDER_NOT_CONFIGURED`，**不回落**任何服务商。

    ⚠️ 落点判据：用户名下**有**一条可用文本服务商，但 `generate_note` 这个业务点
    没指向它 —— 不回落意味着「宁可报未配置，也不自作主张用那条」。
    """
    with db_factory() as db:
        user = _seed_user(db)
        _seed_gateway(db, user)  # 有服务商，但没映射

        with pytest.raises(BusinessProviderNotConfiguredError) as excinfo:
            llm_gateway_client.resolve_business_provider(db, user, BUSINESS_KEY_LLM_NOTE_TEXT)

    err = excinfo.value
    assert err.code == "LLM_BUSINESS_PROVIDER_NOT_CONFIGURED"
    # 可读：文案里带 platform / business_key，用户知道去哪里配
    assert "generate_note" in err.message
    assert "xhs" in err.message


def test_unknown_business_key_also_reports_not_configured(db_factory):
    """不是已登记的 key（写错常量）⇒ 同样报未配置，不静默放行。"""
    with db_factory() as db:
        user = _seed_user(db)
        with pytest.raises(BusinessProviderNotConfiguredError) as excinfo:
            llm_gateway_client.resolve_business_provider(db, user, "not_a_real_key")

    assert excinfo.value.code == "LLM_BUSINESS_PROVIDER_NOT_CONFIGURED"


# --------------------------------------------------------------------------- #
# ② 类型不匹配 ⇒ TYPE_MISMATCH（提前拦，不留给上游报看不懂的错）
# --------------------------------------------------------------------------- #


def test_type_mismatch_raises_before_any_upstream_call(db_factory):
    """图像业务点指向文本服务商 ⇒ `LLM_BUSINESS_PROVIDER_TYPE_MISMATCH`。

    ⚠️ 这个校验必须**提前**做（§13.2.1a）：不校验的后果是调用时拿文本模型打
    `/images/generations`，报一个看不懂的上游错；拦在校验层，错误才有指向性。
    本用例不给任何真实上游，**若解析返回了上下文就是校验没做**。
    """
    with db_factory() as db:
        user = _seed_user(db)
        _seed_gateway(db, user, provider_key="BP-TEXT", model_type="text")
        _seed_mapping(db, user, BUSINESS_KEY_GENERATE_COVER.key, "BP-TEXT")

        with pytest.raises(BusinessProviderTypeMismatchError) as excinfo:
            llm_gateway_client.resolve_business_provider(db, user, BUSINESS_KEY_GENERATE_COVER)

    err = excinfo.value
    assert err.code == "LLM_BUSINESS_PROVIDER_TYPE_MISMATCH"
    # 可读：告诉用户「这个业务点是图片类，不能指向文本服务商」
    # ⚠️ v2.9：类型中文标签统一到 `MODEL_TYPE_LABELS`（四值 text/image/multimodal/video），
    # `image` = 「图片」。断言跟随唯一映射点，不在测试里另立措辞。
    assert "图片" in err.message
    assert "文本" in err.message
    assert "generate_cover" in err.message


# --------------------------------------------------------------------------- #
# ③ 服务商不存在 ⇒ PROVIDER_NOT_FOUND（不回落到别的服务商）
# --------------------------------------------------------------------------- #


def test_dangling_provider_key_raises_not_found(db_factory):
    """映射指向一个**已不存在**的 `provider_key` ⇒ `LLM_PROVIDER_NOT_FOUND`。

    ⚠️ 不回落到该用户名下的其它服务商（这里故意还种了第二条可用服务商，
    落回去就是「用错服务商还不报错」）。
    """
    with db_factory() as db:
        user = _seed_user(db)
        _seed_gateway(db, user, provider_key="BP-REAL", model_type="text")
        _seed_mapping(db, user, BUSINESS_KEY_LLM_NOTE_TEXT.key, "BP-GONE")

        with pytest.raises(ProviderNotFoundError) as excinfo:
            llm_gateway_client.resolve_business_provider(db, user, BUSINESS_KEY_LLM_NOTE_TEXT)

    assert excinfo.value.code == "LLM_PROVIDER_NOT_FOUND"
    assert "BP-GONE" in excinfo.value.message


# --------------------------------------------------------------------------- #
# ④ 服务商停用 ⇒ PROVIDER_DISABLED（D4：不回落）
# --------------------------------------------------------------------------- #


def test_disabled_provider_raises_and_does_not_fall_back(db_factory):
    """映射命中的服务商被**停用** ⇒ `LLM_PROVIDER_DISABLED`，**不回落**到别的服务商。

    ⚠️ 落点判据：用户名下另有一条**同类型且可用**的服务商 —— 回落会静默用它，
    台账也会把 A 的调用记在 B 名下（D4 的原话：「看起来正常但数据是错的」）。
    """
    with db_factory() as db:
        user = _seed_user(db)
        _seed_gateway(db, user, provider_key="BP-OTHER", model_type="text")
        _seed_gateway(db, user, provider_key="BP-OFF", model_type="text", enabled=False)
        _seed_mapping(db, user, BUSINESS_KEY_LLM_NOTE_TEXT.key, "BP-OFF")

        with pytest.raises(ProviderDisabledError) as excinfo:
            llm_gateway_client.resolve_business_provider(db, user, BUSINESS_KEY_LLM_NOTE_TEXT)

    assert excinfo.value.code == "LLM_PROVIDER_DISABLED"


# --------------------------------------------------------------------------- #
# 正路径
# --------------------------------------------------------------------------- #


def test_text_business_key_resolves_to_selected_model_not_default(db_factory):
    """命中 ⇒ 上下文用**映射行显式选择的模型**，**不是**服务商的兜底名（v2.3）。

    ⚠️ 这条用例在 v2.3 改写了语义：旧版（§13.2.4）是「模型名从服务商的 `default_model` 来」，
    现在改为「业务入口必须显式选模型」。
    ⚠️ **v2.8**：`default_model` 列已删，判据变为「模型行里配了哪个模型」—— 故这里
    在模型层给 `deepseek-v4-pro` 建行（映射选它），并**另建一条** `deepseek-v4-flash`
    干扰行，证明实现取的是**映射选的**、不是随便挑一条模型行。
    """
    with db_factory() as db:
        user = _seed_user(db)
        # 干扰行：模型层里已有 `deepseek-v4-flash`（若实现回落到「第一条模型行」本用例必红）
        _seed_gateway(db, user, provider_key="BP-TEXT", model_type="text",
                      default_model="deepseek-v4-flash")
        _seed_mapping(db, user, BUSINESS_KEY_LLM_NOTE_TEXT.key, "BP-TEXT",
                      model_name="deepseek-v4-pro")
        # 映射选的模型也必须在模型层建行（v2.7：判类型读模型行）
        db.add(
            LlmGatewayModel(
                user_id=user.id, provider_key="BP-TEXT", model_name="deepseek-v4-pro",
                model_type="text", platform="xhs", enabled=True,
            )
        )
        db.commit()

        ctx = llm_gateway_client.resolve_business_provider(db, user, BUSINESS_KEY_LLM_NOTE_TEXT)

    assert ctx.base_url == "https://api.example.test/v1"
    assert ctx.model_name == "deepseek-v4-pro"
    assert ctx.model_name != "deepseek-v4-flash", "不许回落到模型的兜底名"
    assert ctx.api_key == "***"  # 解密后的明文（服务层内，不进日志/响应）
    assert ctx.provider_key == "BP-TEXT"


def test_mapping_without_model_raises_model_required(db_factory):
    """映射**只选了服务商、没选模型** ⇒ `LLM_BUSINESS_MODEL_REQUIRED`，**不回落**。

    ⚠️ 落点判据：服务商名下**有**可用模型行，映射行 `model_name=""` ——
    回落会「看起来正常但用的不是用户选的模型」，正是 v2.3 要禁的静默回落。
    ⚠️ **v2.8**：兜底源（`default_model` 列）已删，本用例只需证明「有模型行也不回落」。
    """
    from backend.app.services.llm_gateway_client import BusinessModelRequiredError

    with db_factory() as db:
        user = _seed_user(db)
        _seed_gateway(db, user, provider_key="BP-TEXT", model_type="text",
                      default_model="deepseek-v4-flash")
        _seed_mapping(db, user, BUSINESS_KEY_LLM_NOTE_TEXT.key, "BP-TEXT", model_name="")

        with pytest.raises(BusinessModelRequiredError) as excinfo:
            llm_gateway_client.resolve_business_provider(db, user, BUSINESS_KEY_LLM_NOTE_TEXT)

    err = excinfo.value
    assert err.code == "LLM_BUSINESS_MODEL_REQUIRED"
    # 可读：带上 platform / business_key，用户知道去哪一行补选模型
    assert "generate_note" in err.message
    assert "xhs" in err.message


def test_whitespace_model_is_treated_as_missing(db_factory):
    """模型名只有空白 ⇒ 等同缺模型（`.strip()` 后判空，不给空白字符串开后门）。"""
    from backend.app.services.llm_gateway_client import BusinessModelRequiredError

    with db_factory() as db:
        user = _seed_user(db)
        _seed_gateway(db, user, provider_key="BP-TEXT", model_type="text")
        _seed_mapping(db, user, BUSINESS_KEY_LLM_NOTE_TEXT.key, "BP-TEXT", model_name="   ")

        with pytest.raises(BusinessModelRequiredError):
            llm_gateway_client.resolve_business_provider(db, user, BUSINESS_KEY_LLM_NOTE_TEXT)


def test_mapping_is_user_scoped(db_factory):
    """跨用户隔离（DEF-066）：B 的映射对 A 不可见。"""
    with db_factory() as db:
        owner = _seed_user(db, "bp_owner")
        intruder = _seed_user(db, "bp_intruder")
        _seed_gateway(db, owner, provider_key="BP-OWNER", model_type="text")
        _seed_mapping(db, owner, BUSINESS_KEY_LLM_NOTE_TEXT.key, "BP-OWNER")

        # intruder 名下既无服务商也无映射
        with pytest.raises(LLMGatewayError) as excinfo:
            llm_gateway_client.resolve_business_provider(db, intruder, BUSINESS_KEY_LLM_NOTE_TEXT)

    assert excinfo.value.code == "LLM_BUSINESS_PROVIDER_NOT_CONFIGURED"


def test_all_four_failures_are_subclasses_of_the_gateway_base(db_factory):
    """四种失败都是 `LLMGatewayError` 的子类 ⇒ 调用方用基类一兜即可（§13.2.3）。"""
    assert issubclass(BusinessProviderNotConfiguredError, LLMGatewayError)
    assert issubclass(BusinessProviderTypeMismatchError, LLMGatewayError)
    assert issubclass(ProviderNotFoundError, LLMGatewayError)
    assert issubclass(ProviderDisabledError, LLMGatewayError)
    # 且各自带不同的 code（调用方按 code 给指向性提示，不能撞码）
    codes = {
        BusinessProviderNotConfiguredError().code,
        BusinessProviderTypeMismatchError().code,
        ProviderNotFoundError().code,
        ProviderDisabledError().code,
    }
    assert len(codes) == 4, f"四种失败的 code 必须互不相同，实际 {codes}"


# --------------------------------------------------------------------------- #
# ⑤ v2.9：多态模型（multimodal）同时服务 text / image 业务点
# --------------------------------------------------------------------------- #


def test_multimodal_model_satisfies_text_business_key(db_factory):
    """`multimodal` 模型可指向**文本**业务点 ⇒ 解析成功（v2.9 放宽判据）。

    ⚠️ v2.9 把类型匹配从「严格相等」改为 `_type_satisfies`：`multimodal` 同时服务
    `text` 与 `image`。若仍用严格相等，用户把「多态」模型配到生成笔记（text）上会被
    误拒 —— 而多态模型本就是既能出文又能出图的那一类。
    """
    with db_factory() as db:
        user = _seed_user(db)
        _seed_gateway(db, user, provider_key="BP-MM", model_type="multimodal")
        _seed_mapping(db, user, BUSINESS_KEY_LLM_NOTE_TEXT.key, "BP-MM")

        ctx = llm_gateway_client.resolve_business_provider(db, user, BUSINESS_KEY_LLM_NOTE_TEXT)

    assert ctx.model_name == "deepseek-v4-flash"
    assert ctx.provider_key == "BP-MM"


def test_multimodal_model_satisfies_image_business_key(db_factory):
    """`multimodal` 模型可指向**图片**业务点 ⇒ 解析成功（v2.9 放宽判据）。

    ⚠️ 反向同理：`generate_cover`（image）指向多态模型不该被拒 —— 多态模型能出图。
    与上一条合起来钉死「多态两头都通」，防有人只放宽一半。
    """
    with db_factory() as db:
        user = _seed_user(db)
        _seed_gateway(db, user, provider_key="BP-MM2", model_type="multimodal")
        _seed_mapping(db, user, BUSINESS_KEY_GENERATE_COVER.key, "BP-MM2")

        ctx = llm_gateway_client.resolve_business_provider(db, user, BUSINESS_KEY_GENERATE_COVER)

    assert ctx.model_name == "deepseek-v4-flash"
    assert ctx.provider_key == "BP-MM2"


def test_text_model_still_rejected_for_image_business_key(db_factory):
    """放宽**不等于**取消校验：纯 `text` 模型指向图片业务点仍必拒（防放宽过头）。

    ⚠️ 这条与 `test_type_mismatch_raises_before_any_upstream_call` 的区别在语义护栏：
    后者证明「不匹配会被拒」，本条证明「新增的放宽分支没有把 text→image 也放进来」——
    `_type_satisfies` 只对 `multimodal` 开口子，text / image 互不通用。
    """
    with db_factory() as db:
        user = _seed_user(db)
        _seed_gateway(db, user, provider_key="BP-T2", model_type="text")
        _seed_mapping(db, user, BUSINESS_KEY_GENERATE_COVER.key, "BP-T2")

        with pytest.raises(BusinessProviderTypeMismatchError):
            llm_gateway_client.resolve_business_provider(db, user, BUSINESS_KEY_GENERATE_COVER)


def test_video_model_does_not_satisfy_text_business_key(db_factory):
    """`video` 模型指向文本业务点 ⇒ 仍拒（v2.9 新增枚举不得成为万能通行证）。

    ⚠️ 这是枚举扩到四值后最容易出的错：`_type_satisfies` 若写成「multimodal 之外的
    非 text 也放行」，video 就会漏进 text 业务点。本按例把 video 钉在门外。
    """
    with db_factory() as db:
        user = _seed_user(db)
        _seed_gateway(db, user, provider_key="BP-VID", model_type="video")
        _seed_mapping(db, user, BUSINESS_KEY_LLM_NOTE_TEXT.key, "BP-VID")

        with pytest.raises(BusinessProviderTypeMismatchError):
            llm_gateway_client.resolve_business_provider(db, user, BUSINESS_KEY_LLM_NOTE_TEXT)

