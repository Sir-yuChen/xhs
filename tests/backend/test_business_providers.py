"""M4：v2.0「平台 × 业务点 → 服务商」映射用例（设计 17 §13.2）。

覆盖（§13.2.3 的四态 + §13.2.4 隔离 + §13.2.6 端点契约）：
  ① resolve_business_provider：命中 / 映射缺失 / 类型不匹配 / 服务商停用 / 服务商不存在
  ② **绝不回落**：映射缺失时不得返回任何其它服务商
  ③ 跨用户隔离：A 的映射不影响 B
  ④ 跨平台隔离：同 key 不同 platform 各自独立
  ⑤ upsert 幂等：二次 PUT 是更新不是新增
  ⑥ provider_key 归属校验：指向别人的服务商 ⇒ 404
  ⑦ 业务点清单端点与常量集逐字一致（含 kind）
  ⑧ 类型匹配实测：图像 key 指向 text 服务商 ⇒ 400
  ⑨ 退场守卫：model_configs 6 端点 410 + 表已不存在
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import inspect, select

from backend.app.models import LlmBusinessProvider, LlmGatewayConfig, User
from backend.app.api.llm_gateway import (
    _dump_platforms,
    _parse_platforms,
)
from backend.app.services.business_provider_keys import (
    BUSINESS_KEYS,
    BUSINESS_KEY_GENERATE_COVER,
    BUSINESS_KEY_GENERATE_NOTE,
    MODEL_TYPE_IMAGE,
    MODEL_TYPE_TEXT,
    MODEL_TYPES,
)
from backend.app.services.llm_gateway_client import (
    BusinessModelRequiredError,
    BusinessProviderNotConfiguredError,
    BusinessProviderTypeMismatchError,
    ProviderDisabledError,
    ProviderNotFoundError,
    resolve_business_provider,
)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

def _register(api_client, username: str) -> dict:
    api_client.post("/api/auth/register", json={"username": username, "password": "testpass123"})
    resp = api_client.post("/api/auth/login", json={"username": username, "password": "testpass123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_provider(
    api_client, headers, name: str, model_type: str = "text", models: list[str] | None = None
) -> dict:
    """建服务商 + 模型行。`models` 非空时**直接把它写进 `models_json` 模型目录**。

    ⚠️ `POST /gateway-configs` **不落目录**（目录由「测试连接 / 拉取模型」写入，见
    `_serialize_config` 的 `models` 来自 `models_json`）；测试里造「目录非空」用于验证
    v2.3 的目录校验分支，必须直连 ORM 写 `models_json`，不能指望创建端点。

    ⚠️ **v2.7/v2.8**：服务商层已无 `platform` / `model_type` / `default_model`
    （前两者下沉到模型行、第三者整列删除）—— 旧 payload 会因 `extra="forbid"` 得 **422**。
    故改为随创建带 `models` 子列表：目录里每个模型建一条**模型行**（`model_type` 由参数给定），
    首个模型名即旧的 `default_model` 口径（`deepseek-v4-flash`）。
    """
    catalog = models or ["deepseek-v4-flash"]
    resp = api_client.post(
        "/api/llm/gateway-configs",
        json={
            "name": name,
            "base_url": "http://127.0.0.1:3000",
            "access_token": "***",
            "models": [
                {"model_name": m, "model_type": model_type, "platform": ["xhs"]}
                for m in catalog
            ],
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    if models:
        _set_models_catalog(body["id"], models)
    return body


def _set_models_catalog(config_id: int, models: list[str]) -> None:
    """直连临时库把 `models_json` 写成给定目录（模拟「拉取模型」后的状态）。"""
    from backend.app.core.database import SessionLocal

    with SessionLocal() as db:
        row = db.get(LlmGatewayConfig, config_id)
        assert row is not None, f"服务商 #{config_id} 不存在"
        row.models_json = json.dumps(models, ensure_ascii=False)
        db.commit()


def _user_by_name(api_client, db_factory, username: str) -> User:
    """从临时库取用户（`resolve_business_provider` 要 ORM User 对象）。"""
    with db_factory() as db:
        return db.scalar(select(User).where(User.username == username))


def _upsert(
    api_client,
    headers,
    business_key: str,
    provider_key: str,
    platform: str = "xhs",
    model_name: str = "deepseek-v4-flash",
):
    """PUT 一条「业务点 → 服务商 + 具体模型」映射。

    ⚠️ **v2.3**：`model_name` 是**必填**（用户口径「两者都应是必须选择」）——
    默认给 `_create_provider` 目录里那个合法模型，让各用例只聚焦自己要验证的分支；
    要测「缺模型」的分支请显式传 `model_name=""`。
    """
    return api_client.put(
        "/api/llm/business-providers",
        json={
            "platform": platform,
            "business_key": business_key,
            "provider_key": provider_key,
            "model_name": model_name,
        },
        headers=headers,
    )


# --------------------------------------------------------------------------- #
# ① 四态 + ② 绝不回落
# --------------------------------------------------------------------------- #

def test_resolve_hit_returns_provider_context(api_client, db_factory):
    """命中：返回服务商上下文，`model_name` = **用户在映射里选的那个模型**。

    ⚠️ v2.3：断言「显式选的 ≠ 服务商兜底模型」—— 模型行里有 `deepseek-v4-flash`，
    这里是 `deepseek-v4-pro`。若实现偷偷回落，本用例即失败（防静默回落）。
    ⚠️ **v2.8**：兜底源（`default_model` 列）已删，判据落到**模型行**上。
    """
    headers = _register(api_client, "bp_hit")
    provider = _create_provider(
        api_client, headers, "hit-provider", MODEL_TYPE_TEXT,
        models=["deepseek-v4-flash", "deepseek-v4-pro"],
    )
    resp = _upsert(
        api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"],
        model_name="deepseek-v4-pro",
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["model_name"] == "deepseek-v4-pro"

    user = _user_by_name(api_client, db_factory, "bp_hit")
    with db_factory() as db:
        ctx = resolve_business_provider(db, user, BUSINESS_KEY_GENERATE_NOTE)
    assert ctx.provider_key == provider["provider_key"]
    assert ctx.model_name == "deepseek-v4-pro"
    assert ctx.model_name != "deepseek-v4-flash", "不得回落到服务商默认模型"


def test_resolve_missing_mapping_raises_and_never_falls_back(api_client, db_factory):
    """映射缺失 ⇒ 抛 `BusinessProviderNotConfiguredError`，**绝不回落**到库内其它服务商。

    这是本卡的核心断言：构造**多条**可用服务商，仍必须抛错 ——
    回落 = 用错服务商而不报错，是最难查的形态（§13.2.3 D4）。
    """
    headers = _register(api_client, "bp_miss")
    _create_provider(api_client, headers, "a-provider", MODEL_TYPE_TEXT)
    _create_provider(api_client, headers, "b-provider", MODEL_TYPE_TEXT)
    # 刻意不建任何映射

    user = _user_by_name(api_client, db_factory, "bp_miss")
    with db_factory() as db:
        with pytest.raises(BusinessProviderNotConfiguredError):
            resolve_business_provider(db, user, BUSINESS_KEY_GENERATE_NOTE)


def test_resolve_type_mismatch_raises(api_client, db_factory):
    """类型不匹配 ⇒ `BusinessProviderTypeMismatchError`（提前拦，不打上游）。

    ⚠️ **v2.7**：类型判据在**模型行**（`llm_gateway_models.model_type`）——
    故必须为该模型建档且标成 `image`，否则会先撞 `BusinessModelNotConfiguredError`
    （「目录里有名字但没建行」是另一种失败，本用例要测的是「建了行但类型不符」）。
    """
    headers = _register(api_client, "bp_type")
    image_provider = _create_provider(
        api_client, headers, "type-provider", MODEL_TYPE_IMAGE,
        models=["some-image-model"],
    )

    # 端点层会先拦（§13.2.6 类型校验）⇒ 直接用 ORM 造一条脏映射验证解析层也拦
    user = _user_by_name(api_client, db_factory, "bp_type")
    with db_factory() as db:
        db.add(
            LlmBusinessProvider(
                user_id=user.id,
                platform="xhs",
                business_key=BUSINESS_KEY_GENERATE_NOTE.key,
                provider_key=image_provider["provider_key"],
                model_name="some-image-model",
            )
        )
        db.commit()
        with pytest.raises(BusinessProviderTypeMismatchError):
            resolve_business_provider(db, user, BUSINESS_KEY_GENERATE_NOTE)


def test_resolve_provider_disabled_raises(api_client, db_factory):
    """服务商停用 ⇒ `ProviderDisabledError`（**不静默回落**，D4）。"""
    headers = _register(api_client, "bp_disabled")
    provider = _create_provider(api_client, headers, "disabled-provider", MODEL_TYPE_TEXT)
    assert _upsert(api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"]).status_code == 200

    # 停用
    resp = api_client.patch(
        f"/api/llm/gateway-configs/{provider['id']}", json={"enabled": False}, headers=headers
    )
    assert resp.status_code == 200, resp.text

    user = _user_by_name(api_client, db_factory, "bp_disabled")
    with db_factory() as db:
        with pytest.raises(ProviderDisabledError):
            resolve_business_provider(db, user, BUSINESS_KEY_GENERATE_NOTE)


def test_resolve_provider_not_found_raises(api_client, db_factory):
    """映射指向不存在的服务商 ⇒ `ProviderNotFoundError`（不猜、不回落）。"""
    headers = _register(api_client, "bp_notfound")
    user = _user_by_name(api_client, db_factory, "bp_notfound")
    with db_factory() as db:
        db.add(
            LlmBusinessProvider(
                user_id=user.id,
                platform="xhs",
                business_key=BUSINESS_KEY_GENERATE_NOTE.key,
                provider_key="NOPE-0000",
                model_name="whatever-model",
            )
        )
        db.commit()
        with pytest.raises(ProviderNotFoundError):
            resolve_business_provider(db, user, BUSINESS_KEY_GENERATE_NOTE)


# --------------------------------------------------------------------------- #
# ③ 跨用户隔离 / ④ 跨平台隔离
# --------------------------------------------------------------------------- #

def test_mapping_is_isolated_across_users(api_client, db_factory):
    """A 配了映射，B 同 key 同平台仍然「未配置」（DEF-066）。"""
    head_a = _register(api_client, "bp_user_a")
    head_b = _register(api_client, "bp_user_b")

    prov_a = _create_provider(api_client, head_a, "provider-a", MODEL_TYPE_TEXT)
    assert _upsert(api_client, head_a, BUSINESS_KEY_GENERATE_NOTE.key, prov_a["provider_key"]).status_code == 200

    # B 自己的列表仍显示未配置
    resp = api_client.get("/api/llm/business-providers?platform=xhs", headers=head_b)
    assert resp.status_code == 200
    rows = [r for r in resp.json()["items"] if r["business_key"] == BUSINESS_KEY_GENERATE_NOTE.key]
    assert rows and rows[0]["configured"] is False

    # B 的解析也必须抛错（不借用 A 的映射）
    user_b = _user_by_name(api_client, db_factory, "bp_user_b")
    with db_factory() as db:
        with pytest.raises(BusinessProviderNotConfiguredError):
            resolve_business_provider(db, user_b, BUSINESS_KEY_GENERATE_NOTE)


def test_mapping_is_isolated_across_platforms(api_client, db_factory):
    """同 key 不同 platform 各自独立：xhs 配了，douyin 仍「未配置」。"""
    headers = _register(api_client, "bp_platform")
    provider = _create_provider(api_client, headers, "plat-provider", MODEL_TYPE_TEXT)
    assert _upsert(api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"], "xhs").status_code == 200

    resp = api_client.get("/api/llm/business-providers?platform=douyin", headers=headers)
    assert resp.status_code == 200
    rows = [r for r in resp.json()["items"] if r["business_key"] == BUSINESS_KEY_GENERATE_NOTE.key]
    assert rows and rows[0]["configured"] is False

    user = _user_by_name(api_client, db_factory, "bp_platform")
    with db_factory() as db:
        with pytest.raises(BusinessProviderNotConfiguredError):
            resolve_business_provider(db, user, BUSINESS_KEY_GENERATE_NOTE, platform="douyin")


# --------------------------------------------------------------------------- #
# ⑤ upsert 幂等 / ⑥ 归属校验 / ⑦ 清单一致 / ⑧ 类型匹配
# --------------------------------------------------------------------------- #

def test_upsert_is_idempotent_not_insert(api_client, db_factory):
    """同 `(platform, business_key)` 二次 PUT 是**更新**，行数不增。"""
    headers = _register(api_client, "bp_idem")
    first = _create_provider(api_client, headers, "idem-1", MODEL_TYPE_TEXT)
    second = _create_provider(api_client, headers, "idem-2", MODEL_TYPE_TEXT)

    assert _upsert(api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, first["provider_key"]).status_code == 200
    assert _upsert(api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, second["provider_key"]).status_code == 200

    user = _user_by_name(api_client, db_factory, "bp_idem")
    with db_factory() as db:
        rows = db.scalars(
            select(LlmBusinessProvider).where(
                LlmBusinessProvider.user_id == user.id,
                LlmBusinessProvider.business_key == BUSINESS_KEY_GENERATE_NOTE.key,
            )
        ).all()
        assert len(rows) == 1, "二次 upsert 不得新增行"
        assert rows[0].provider_key == second["provider_key"], "应更新为后写的服务商"


def test_upsert_rejects_other_users_provider_404(api_client):
    """指向**别人**的服务商 ⇒ 404（且与「不存在」同码，不泄露他人编码）。"""
    head_a = _register(api_client, "bp_owner_a")
    head_b = _register(api_client, "bp_owner_b")
    prov_a = _create_provider(api_client, head_a, "owner-a-provider", MODEL_TYPE_TEXT)

    resp = _upsert(api_client, head_b, BUSINESS_KEY_GENERATE_NOTE.key, prov_a["provider_key"])
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "LLM_PROVIDER_NOT_FOUND"


def test_business_key_list_matches_constants(api_client):
    """业务点清单端点返回值与 §13.2.2 常量集**逐字一致**（含 kind）。"""
    headers = _register(api_client, "bp_keys")
    resp = api_client.get("/api/llm/business-provider-keys", headers=headers)
    assert resp.status_code == 200
    got = [(i["key"], i["kind"], i["label"]) for i in resp.json()["items"]]
    want = [(i.key, i.kind) for i in BUSINESS_KEYS]

    assert len(got) == len(want), f"业务点数不一致：{len(got)} != {len(want)}"
    for (gk, gkind, glabel), (wk, wkind) in zip(got, want):
        assert gk == wk, f"key 顺序/取值不一致：{gk} != {wk}"
        assert gkind == wkind, f"{gk} 的 kind 不一致：{gkind} != {wkind}"
        assert glabel, f"{gk} 缺中文 label"


def test_business_key_triggers_are_exposed_and_match_constants(api_client):
    """**触发源**（2026-10-01 新增）：清单端点与映射行两处都下发，且逐字来自常量。

    用户口径「在小红书模型配置页业务入口处加触发源」+「源列不支持修改」：
    触发源是**只读**展示字段（镜像代码里的调用点），页面不许提供编辑入口。

    判据三条：
    ① 清单端点 `business-provider-keys` 每个业务点都带 `triggers`，且与常量逐字一致；
    ② 映射行端点 `business-providers` 同样带 `label` / `triggers`（页面直接渲染，不必反查）；
    ③ 新增的 `generate_product_detail_image` 业务点存在、kind=image、
       触发源含「商品列表-商品详情-生成商品详情图」（用户点名的那个入口）。
    """
    headers = _register(api_client, "bp_triggers")

    resp = api_client.get("/api/llm/business-provider-keys", headers=headers)
    assert resp.status_code == 200
    got = {i["key"]: i for i in resp.json()["items"]}
    assert set(got) == {i.key for i in BUSINESS_KEYS}, "业务点集合与常量不一致"
    for item in BUSINESS_KEYS:
        assert got[item.key]["triggers"] == list(item.triggers), (
            f"{item.key} 的触发源与常量不一致：{got[item.key]['triggers']} != {list(item.triggers)}"
        )

    # ② 映射行（页面表格的数据源）同样带这两个只读字段
    resp = api_client.get("/api/llm/business-providers", headers=headers)
    assert resp.status_code == 200
    rows = {r["business_key"]: r for r in resp.json()["items"]}
    assert set(rows) == {i.key for i in BUSINESS_KEYS}, "映射行未覆盖全部业务点（新增业务点应自动出现）"
    for item in BUSINESS_KEYS:
        assert rows[item.key]["label"] == (item.label or item.key), f"{item.key} label 不一致"
        assert rows[item.key]["triggers"] == list(item.triggers), f"{item.key} triggers 不一致"

    # ③ 用户点名的那个业务点
    detail = rows["generate_product_detail_image"]
    assert detail["kind"] == MODEL_TYPE_IMAGE
    assert detail["triggers"] == ["商品列表-商品详情-生成商品详情图"]
    assert detail["label"] == "生成商品详情图"


def test_upsert_rejects_type_mismatch_400(api_client):
    """图像业务点指向 **text** 服务商 ⇒ 400 `LLM_BUSINESS_PROVIDER_TYPE_MISMATCH`。"""
    headers = _register(api_client, "bp_mismatch")
    text_provider = _create_provider(api_client, headers, "text-only", MODEL_TYPE_TEXT)

    resp = _upsert(api_client, headers, BUSINESS_KEY_GENERATE_COVER.key, text_provider["provider_key"])
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "LLM_BUSINESS_PROVIDER_TYPE_MISMATCH"


def test_upsert_rejects_unknown_business_key_400(api_client):
    """未知业务点 ⇒ 400 `LLM_BUSINESS_KEY_UNKNOWN`（不写脏行）。"""
    headers = _register(api_client, "bp_unknown")
    provider = _create_provider(api_client, headers, "uprov", MODEL_TYPE_TEXT)
    resp = _upsert(api_client, headers, "not_a_business_key", provider["provider_key"])
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "LLM_BUSINESS_KEY_UNKNOWN"


def test_clear_mapping_returns_to_unconfigured(api_client, db_factory):
    """`provider_key=""` ⇒ 回到「未配置」（清空映射，不是删行）。"""
    headers = _register(api_client, "bp_clear")
    provider = _create_provider(api_client, headers, "clear-provider", MODEL_TYPE_TEXT)
    assert _upsert(api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"]).status_code == 200
    resp = _upsert(api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, "")
    assert resp.status_code == 200
    assert resp.json()["configured"] is False

    user = _user_by_name(api_client, db_factory, "bp_clear")
    with db_factory() as db:
        # ⚠️ v2.3：清空后**行仍在、provider_key 与 model_name 都为空** ⇒ 解析层在
        # 「模型必选」这步先拦（比 provider_key 检查更靠前）⇒ `BusinessModelRequiredError`。
        # 这和「行不存在」（`BusinessProviderNotConfiguredError`）仍是两种错误，都要能定位。
        with pytest.raises(BusinessModelRequiredError):
            resolve_business_provider(db, user, BUSINESS_KEY_GENERATE_NOTE)


def test_list_includes_unconfigured_rows(api_client):
    """列表**含未配置业务点**（否则用户无从发现哪些还没配）。"""
    headers = _register(api_client, "bp_list")
    resp = api_client.get("/api/llm/business-providers?platform=xhs", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == len(BUSINESS_KEYS)
    assert all(i["configured"] is False for i in items)
    # 每个业务点都带 kind（前端按它过滤下拉候选）。
    # ⚠️ v2.14（2026-10-02）：新增 `generate_video`（kind=video）⇒ 白名单从
    # 「text/image 两值」扩为 `MODEL_TYPES` 全集。**不是放松断言** —— 仍要求 kind
    # 落在后端唯一枚举里（写错一个值照样红），只是枚举本身长大了。
    assert all(i["kind"] in MODEL_TYPES for i in items)


# --------------------------------------------------------------------------- #
# ⑨ 退场守卫
# --------------------------------------------------------------------------- #

def test_model_config_endpoints_are_gone_410(api_client):
    """一期 6 个 `model_configs` 端点 ⇒ 410 墓碑（带结构化 code，非 404）。

    ⚠️ 退场**不放宽鉴权**：未认证请求仍先被 401 拦下（与退场前语义一致），
    故本用例带 token 才能看到 410。
    """
    headers = _register(api_client, "bp_gone")
    resp = api_client.get("/api/model-configs", headers=headers)
    assert resp.status_code == 410, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "MODEL_CONFIGS_GONE"
    assert "模型配置" in detail["message"]


def test_model_configs_table_is_dropped(api_client, db_factory):
    """`model_configs` 表**整表退场**（不是「留着不用」）。

    两处判据缺一不可：
      ① 迁移已 drop（实测库内无此表）；
      ② ORM 类也已移除 ⇒ `create_all()` **不再重建**它。只做①会留下
         「测试库/新装库又被 create_all 建回来」的假象，这条断言随即失效。
    """
    from backend.app.core.database import Base

    assert "model_configs" not in Base.metadata.tables, "ORM 类未退场 ⇒ create_all 会重建该表"
    with db_factory() as db:
        assert not inspect(db.get_bind()).has_table("model_configs"), "model_configs 表应已退场"
        assert inspect(db.get_bind()).has_table("llm_business_providers"), "新映射表应存在"


def test_model_configs_endpoints_absent_from_openapi(api_client):
    """退场端点**不进 OpenAPI**（`include_in_schema=False`）。"""
    schema = api_client.get("/openapi.json").json()
    assert not any("model-config" in p for p in schema["paths"]), "退场端点不得出现在 schema"
    assert "/api/llm/business-providers" in schema["paths"]
    assert "/api/llm/business-provider-keys" in schema["paths"]


# --------------------------------------------------------------------------- #
# ⑩ v2.3：具体模型必选（用户口径「两者都应是必须选择，不指定具体模型无法调用成功」）
# --------------------------------------------------------------------------- #

def test_upsert_without_model_400(api_client):
    """只选服务商、不选模型 ⇒ 400 `LLM_BUSINESS_MODEL_REQUIRED`（不写脏行）。

    用户口径原文：**不指定具体的模型无法调用成功** —— 若端点放行，用户会配出一个
    「看起来配好了」却 100% 调不通的映射（最难查的形态）。
    """
    headers = _register(api_client, "bp_nomodel")
    provider = _create_provider(api_client, headers, "nomodel-provider", MODEL_TYPE_TEXT)

    resp = _upsert(
        api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"],
        model_name="",
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "LLM_BUSINESS_MODEL_REQUIRED"

    # 纯空白同样视为缺模型（不靠 strip 之外的宽松）
    resp = _upsert(
        api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"],
        model_name="   ",
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "LLM_BUSINESS_MODEL_REQUIRED"


def test_upsert_model_not_in_catalog_400(api_client):
    """模型不在服务商目录里 ⇒ 400 `LLM_BUSINESS_MODEL_NOT_IN_CATALOG`。

    目录非空时才校验（目录 = 上游 `/models` 拉到什么；空目录放行，见端点注释）。
    """
    headers = _register(api_client, "bp_badmodel")
    provider = _create_provider(
        api_client, headers, "cat-provider", MODEL_TYPE_TEXT,
        models=["deepseek-v4-flash", "deepseek-v4-pro"],
    )

    resp = _upsert(
        api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"],
        model_name="gpt-nonexistent",
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "LLM_BUSINESS_MODEL_NOT_IN_CATALOG"


def test_upsert_model_in_catalog_ok(api_client):
    """目录里的模型 ⇒ 200，且回显 `model_name` / `configured=True`。"""
    headers = _register(api_client, "bp_okmodel")
    provider = _create_provider(
        api_client, headers, "cat-ok", MODEL_TYPE_TEXT,
        models=["deepseek-v4-flash", "deepseek-v4-pro"],
    )
    resp = _upsert(
        api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"],
        model_name="deepseek-v4-pro",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model_name"] == "deepseek-v4-pro"
    assert body["configured"] is True


def test_clear_mapping_also_clears_model(api_client, db_factory):
    """清空 `provider_key` ⇒ 模型一并清空（无服务商则无模型可言）。"""
    headers = _register(api_client, "bp_clearmodel")
    provider = _create_provider(
        api_client, headers, "clearmodel", MODEL_TYPE_TEXT, models=["deepseek-v4-pro"],
    )
    assert _upsert(
        api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"],
        model_name="deepseek-v4-pro",
    ).status_code == 200

    resp = _upsert(api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, "", model_name="")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider_key"] == ""
    assert body["model_name"] == ""
    assert body["configured"] is False

    user = _user_by_name(api_client, db_factory, "bp_clearmodel")
    with db_factory() as db:
        row = db.scalar(
            select(LlmBusinessProvider).where(
                LlmBusinessProvider.user_id == user.id,
                LlmBusinessProvider.business_key == BUSINESS_KEY_GENERATE_NOTE.key,
            )
        )
        assert row is not None and row.model_name == "", "清空映射必须连模型一起清"


def test_list_exposes_model_name_and_provider_models(api_client):
    """映射行端点下发 `model_name` / `provider_models` / `configured`（前端下拉联动）。"""
    headers = _register(api_client, "bp_listmodel")
    provider = _create_provider(
        api_client, headers, "listmodel", MODEL_TYPE_TEXT,
        models=["deepseek-v4-flash", "deepseek-v4-pro"],
    )
    assert _upsert(
        api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"],
        model_name="deepseek-v4-pro",
    ).status_code == 200

    resp = api_client.get("/api/llm/business-providers?platform=xhs", headers=headers)
    assert resp.status_code == 200
    rows = {r["business_key"]: r for r in resp.json()["items"]}
    row = rows[BUSINESS_KEY_GENERATE_NOTE.key]
    assert row["model_name"] == "deepseek-v4-pro"
    assert row["provider_models"] == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert row["configured"] is True

    # 未配置的行：模型为空、目录为空、configured=False
    other = rows[BUSINESS_KEY_GENERATE_COVER.key]
    assert other["model_name"] == ""
    assert other["provider_models"] == []
    assert other["configured"] is False


def test_resolve_uses_mapping_model_and_ignores_disabled_default(api_client, db_factory):
    """解析层取映射里选的模型；模型层另有其它模型也不回落。"""
    headers = _register(api_client, "bp_resolvemodel")
    # 模型行含 deepseek-v4-flash（见 _create_provider 默认目录）
    provider = _create_provider(
        api_client, headers, "resolvemodel", MODEL_TYPE_TEXT,
        models=["deepseek-v4-flash", "deepseek-v4-pro"],
    )
    assert _upsert(
        api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"],
        model_name="deepseek-v4-pro",
    ).status_code == 200

    user = _user_by_name(api_client, db_factory, "bp_resolvemodel")
    with db_factory() as db:
        ctx = resolve_business_provider(db, user, BUSINESS_KEY_GENERATE_NOTE)
    assert ctx.model_name == "deepseek-v4-pro"


# --------------------------------------------------------------------------- #
# v2.26（§13.2.9.7「配置时同步」）：在平台配置模型 ⇒ 该平台并入模型行的 platform
# --------------------------------------------------------------------------- #


def _clear_model_platforms(db_factory, username: str) -> None:
    """把该用户所有模型行的 `platform` 清空（造「还没在任何平台配过」的起点）。

    ⚠️ 必须清空才有意义：`_create_provider` 建行时已写 `platform: ["xhs"]`
    （见其 docstring），不清空的话「并入」是**空操作**，用例会假绿。
    """
    from backend.app.models import LlmGatewayModel
    from sqlalchemy import select as _select

    user = _user_by_name(None, db_factory, username)
    with db_factory() as db:
        for row in db.scalars(_select(LlmGatewayModel).where(LlmGatewayModel.user_id == user.id)):
            # ⚠️ 列是 JSON 字符串（不是 SQLAlchemy JSON）⇒ 必须用收敛点，不能赋 list
            row.platform = "[]"
        db.commit()


def _model_platforms(db_factory, username: str, model_name: str) -> list[str]:
    from backend.app.models import LlmGatewayModel
    from sqlalchemy import select as _select

    user = _user_by_name(None, db_factory, username)
    with db_factory() as db:
        row = db.scalar(
            _select(LlmGatewayModel).where(
                LlmGatewayModel.user_id == user.id,
                LlmGatewayModel.model_name == model_name,
            )
        )
        return _parse_platforms(row.platform) if row else []


def test_upsert_merges_platform_into_model_row(api_client, db_factory):
    """**在平台配置模型 ⇒ 该平台并入模型行的 `platform`**（§13.2.9.7）。

    用户口径原话：「再具体平台配置该服务商下的具体模型时，将当前平台的设置为当前
    服务商下的模型的**使用平台**」。

    ⚠️ 落地前这是**漏实现**：端点读到了 `model_row` 却只用它做类型校验，没写
    `platform` ⇒ 真机现象 = 用户在模型配置页选了服务商+模型，服务商列表里该模型的
    「使用平台」仍是 `-`。本用例钉住「必须写」。

    起点必须**显式清空** `platform`（`_create_provider` 建行时已写 `xhs`，
    不清空则并入是空操作、用例恒真）。
    """
    headers = _register(api_client, "platform-merge")
    provider = _create_provider(api_client, headers, "合并平台", model_type=MODEL_TYPE_TEXT)
    _clear_model_platforms(db_factory, "platform-merge")
    assert _model_platforms(db_factory, "platform-merge", "deepseek-v4-flash") == []

    _upsert(api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"], platform="xhs")

    assert _model_platforms(db_factory, "platform-merge", "deepseek-v4-flash") == ["xhs"], (
        "在 xhs 平台配置了模型，模型行的 platform 却没写 xhs（§13.2.9.7 漏实现）"
    )


def test_upsert_platform_merge_is_idempotent(api_client, db_factory):
    """重复配置**不产生重复项**（并入是「去重追加」，不是「每次 append」）。

    否则同一个模型配两次就变成 `["xhs", "xhs"]` —— 展示会重复，且无法判断
    「配过几个平台」。
    """
    headers = _register(api_client, "platform-idem")
    provider = _create_provider(api_client, headers, "幂等平台", model_type=MODEL_TYPE_TEXT)
    _clear_model_platforms(db_factory, "platform-idem")

    for _ in range(3):
        _upsert(api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"], platform="xhs")

    assert _model_platforms(db_factory, "platform-idem", "deepseek-v4-flash") == ["xhs"]


def test_upsert_merge_accumulates_not_overwrites(api_client, db_factory):
    """**并入不是覆盖**（§13.2.9.7）：已有别的平台时**保留**它。

    覆盖会让「多平台共用一个模型」无法累积 —— 在 A 平台配过、再到 B 平台配，
    只剩 B，A 的记录凭空消失。
    """
    headers = _register(api_client, "platform-accum")
    provider = _create_provider(api_client, headers, "累积平台", model_type=MODEL_TYPE_TEXT)

    # 造「已在别的平台配过」：直接写一个非 xhs 平台（本用例只验合并语义，
    # 不依赖第二平台真的存在于白名单 —— 白名单校验只作用于**入参**）
    from backend.app.models import LlmGatewayModel
    from sqlalchemy import select as _select

    user = _user_by_name(None, db_factory, "platform-accum")
    with db_factory() as db:
        row = db.scalar(
            _select(LlmGatewayModel).where(
                LlmGatewayModel.user_id == user.id,
                LlmGatewayModel.model_name == "deepseek-v4-flash",
            )
        )
        row.platform = _dump_platforms(["douyin"])
        db.commit()

    _upsert(api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"], platform="xhs")

    merged = _model_platforms(db_factory, "platform-accum", "deepseek-v4-flash")
    assert "douyin" in merged, "原有的平台被覆盖掉了（并入≠覆盖）"
    assert "xhs" in merged, "新平台没并进去"
    assert len(merged) == len(set(merged)), f"出现重复项：{merged}"


def test_clearing_mapping_does_not_crash_or_touch_platform(api_client, db_factory):
    """**清空映射**（provider_key 为空）⇒ 不崩、**也不动** `platform`。

    ⚠️ 这条守的是实现细节的正确性：`model_row` 只在 `provider_key` 非空时赋值，
    若不在函数开头显式初始化，清空映射那条路会 `NameError`（不是业务错，是崩溃）。
    且「清空映射」改的是**映射层**，与「模型被用在哪些平台」无关 ⇒ 不该顺手清空。
    """
    headers = _register(api_client, "platform-clear")
    provider = _create_provider(api_client, headers, "清空平台", model_type=MODEL_TYPE_TEXT)

    _upsert(api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"], platform="xhs")
    before = _model_platforms(db_factory, "platform-clear", "deepseek-v4-flash")

    # 清空映射（provider_key=""）
    resp = _upsert(api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, "", platform="xhs")
    assert resp.status_code == 200, resp.text

    after = _model_platforms(db_factory, "platform-clear", "deepseek-v4-flash")
    assert after == before, "清空映射时顺手改了模型的使用平台（两者是不同的东西）"


def test_platform_dedupe_existing_platform_not_added_again(api_client, db_factory):
    """**使用平台去重：已存在的平台不再添加**（2026-10-03 用户口径，原话）。

    > 「使用平台需要去重，每个平台只需要添加一次，存在则不在添加」

    与 `test_upsert_platform_merge_is_idempotent` 的区别：那条在 **空列表起点** 上验
    （并入一次 → `["xhs"]`，只证明「写进去了」）；本条的起点**已经有 xhs**，
    验的是「**再配一次不追加第二个**」—— 判据是 `_merge_platforms` 的 `value not in merged`。

    ⚠️ 为什么单独立一条：不去重的实现（`[*existing, new]`）**能通过**空起点用例
    （`[] + xhs = ["xhs"]`）—— 只有「起点已含该平台」才暴露它。
    """
    from backend.app.models import LlmGatewayModel
    from sqlalchemy import select as _select

    headers = _register(api_client, "platform-dedupe")
    provider = _create_provider(api_client, headers, "去重平台", model_type=MODEL_TYPE_TEXT)

    # 起点：已含 xhs（模拟「上次已经在这个平台配过」）
    user = _user_by_name(None, db_factory, "platform-dedupe")
    with db_factory() as db:
        row = db.scalar(
            _select(LlmGatewayModel).where(
                LlmGatewayModel.user_id == user.id,
                LlmGatewayModel.model_name == "deepseek-v4-flash",
            )
        )
        row.platform = _dump_platforms(["xhs"])
        db.commit()

    # 再在同一平台配 3 次
    for _ in range(3):
        resp = _upsert(
            api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"],
            platform="xhs",
        )
        assert resp.status_code == 200, resp.text

    platforms = _model_platforms(db_factory, "platform-dedupe", "deepseek-v4-flash")
    assert platforms == ["xhs"], (
        f"已存在的平台被重复添加了（应去重）：{platforms}"
    )
