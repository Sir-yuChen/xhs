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

import pytest
from sqlalchemy import inspect, select

from backend.app.models import LlmBusinessProvider, LlmGatewayConfig, User
from backend.app.services.business_provider_keys import (
    BUSINESS_KEYS,
    BUSINESS_KEY_GENERATE_COVER,
    BUSINESS_KEY_GENERATE_NOTE,
    MODEL_TYPE_IMAGE,
    MODEL_TYPE_TEXT,
)
from backend.app.services.llm_gateway_client import (
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


def _create_provider(api_client, headers, name: str, model_type: str = "text") -> dict:
    resp = api_client.post(
        "/api/llm/gateway-configs",
        json={
            "name": name,
            "platform": "xhs",
            "model_type": model_type,
            "base_url": "http://127.0.0.1:3000",
            "access_token": "***",
            "default_model": "deepseek-v4-flash",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _user_by_name(api_client, db_factory, username: str) -> User:
    """从临时库取用户（`resolve_business_provider` 要 ORM User 对象）。"""
    with db_factory() as db:
        return db.scalar(select(User).where(User.username == username))


def _upsert(api_client, headers, business_key: str, provider_key: str, platform: str = "xhs"):
    return api_client.put(
        "/api/llm/business-providers",
        json={"platform": platform, "business_key": business_key, "provider_key": provider_key},
        headers=headers,
    )


# --------------------------------------------------------------------------- #
# ① 四态 + ② 绝不回落
# --------------------------------------------------------------------------- #

def test_resolve_hit_returns_provider_context(api_client, db_factory):
    """命中：返回服务商上下文，且 model_name 来自该服务商。"""
    headers = _register(api_client, "bp_hit")
    provider = _create_provider(api_client, headers, "hit-provider", MODEL_TYPE_TEXT)
    assert _upsert(api_client, headers, BUSINESS_KEY_GENERATE_NOTE.key, provider["provider_key"]).status_code == 200

    user = _user_by_name(api_client, db_factory, "bp_hit")
    with db_factory() as db:
        ctx = resolve_business_provider(db, user, BUSINESS_KEY_GENERATE_NOTE)
    assert ctx.provider_key == provider["provider_key"]
    assert ctx.model_name == "deepseek-v4-flash"


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
    """类型不匹配 ⇒ `BusinessProviderTypeMismatchError`（提前拦，不打上游）。"""
    headers = _register(api_client, "bp_type")
    image_provider = _create_provider(api_client, headers, "type-provider", MODEL_TYPE_IMAGE)

    # 端点层会先拦（§13.2.6 类型校验）⇒ 直接用 ORM 造一条脏映射验证解析层也拦
    user = _user_by_name(api_client, db_factory, "bp_type")
    with db_factory() as db:
        db.add(
            LlmBusinessProvider(
                user_id=user.id,
                platform="xhs",
                business_key=BUSINESS_KEY_GENERATE_NOTE.key,
                provider_key=image_provider["provider_key"],
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
        # ⚠️ 清空后**行仍在、provider_key 为空** ⇒ 解析层走到 resolve_provider_context，
        # 因 key 为空抛 ProviderNotFoundError（与「行不存在」是两种错误，都要能定位）。
        with pytest.raises(ProviderNotFoundError):
            resolve_business_provider(db, user, BUSINESS_KEY_GENERATE_NOTE)


def test_list_includes_unconfigured_rows(api_client):
    """列表**含未配置业务点**（否则用户无从发现哪些还没配）。"""
    headers = _register(api_client, "bp_list")
    resp = api_client.get("/api/llm/business-providers?platform=xhs", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == len(BUSINESS_KEYS)
    assert all(i["configured"] is False for i in items)
    # 每个业务点都带 kind（前端按它过滤下拉候选）
    assert all(i["kind"] in (MODEL_TYPE_TEXT, MODEL_TYPE_IMAGE) for i in items)


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
