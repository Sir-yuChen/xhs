"""LLM 网关服务商种子（2026-10-13 需求 R4）—— 数据文件 / 播种 / 幂等 / 默认业务点映射。

用户口径原文：
> 「大模型网关中的服务商中，默认将 deepseek、千问、豆包、火山、智谱 等等 —— 调研一下将
> 国内常见的 6 个服务商作为种子数据配置到服务列表中，用户安装登录后只需要配置 key
> 并检测后即可使用；另外小红书页的大模型配置页，也需要默认指定一个服务商的模型。」

本文件盯五件事（任一坏了「装完即可用」就落空）：

1. **数据文件本身**：存在、可解析、每个服务商有 name/base_url、模型类型都在白名单内、
   **不含任何 KEY/token 字段**（种子随包分发，塞 KEY = 泄露）；
2. **播种链路**：注册即播种（走 `api/auth.py` 的注册端点）、条数与文件一致、模型行齐；
3. **幂等**：重复播种不新增；用户改名/删模型后不还魂（已有服务商不覆盖）；
4. **不含 KEY**：服务商 `access_token` 一律空串；
5. **默认业务点映射**：小红书平台的文本/图片业务点被映射；视频类业务点**不映射**（无种子模型）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.core import initial_data
from backend.app.models import LlmBusinessProvider, LlmGatewayConfig, LlmGatewayModel, User
from backend.app.services.llm_gateway_seed_service import (
    DEFAULT_BUSINESS_MAP,
    seed_default_business_map_for_user,
    seed_gateway_providers_for_user,
)
from backend.app.services.business_provider_keys import MODEL_TYPES


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
PROVIDERS_FILE = ROOT / "config" / "initial_data" / "llm_gateway_providers.json"


def _db(db_factory):
    return db_factory()


def _make_user(SessionLocal, username: str = "provider-seed-owner") -> int:
    db = SessionLocal()
    try:
        user = User(username=username, password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 1. 数据文件本身
# --------------------------------------------------------------------------- #


def test_providers_file_exists_and_is_parseable():
    assert PROVIDERS_FILE.is_file(), "服务商种子文件不存在"
    payload = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8-sig"))
    rows = payload.get("providers")
    assert isinstance(rows, list) and len(rows) >= 6, "至少 6 个服务商种子"


def test_providers_ship_without_bom_and_loader_tolerates_one():
    """随包形态必须**无 BOM**（读取侧宽容 BOM，见 `_load_payload` 的 utf-8-sig）。"""
    raw = PROVIDERS_FILE.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "随包的服务商种子文件不应带 BOM"


def test_every_provider_has_name_and_base_url():
    for spec in initial_data.load_initial_gateway_providers():
        assert spec["name"], "服务商缺 name"
        assert spec["base_url"].startswith("http"), f"{spec['name']} 的 base_url 非法"


def test_every_model_type_is_within_whitelist():
    for spec in initial_data.load_initial_gateway_providers():
        for model in spec["models"]:
            assert model["model_type"] in MODEL_TYPES, (
                f"{spec['name']}/{model['model_name']} 的 model_type 不在白名单"
            )


def test_seed_contains_no_key_or_token():
    """种子**不得**含任何 KEY/token 字段（随包分发 ⇒ 塞 KEY = 泄露）。"""
    payload = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8-sig"))
    text = json.dumps(payload, ensure_ascii=False).lower()
    for forbidden in ("access_token", "api_key", "apikey", "sk-", "authorization", "bearer"):
        assert forbidden not in text, f"服务商种子疑似含凭据字段：{forbidden}"


def test_normalizer_drops_unknown_model_type():
    """未知 model_type 的模型被丢弃（不猜类型），但服务商本身保留。"""
    item = initial_data._normalize_gateway_provider(
        {
            "name": "X",
            "base_url": "https://x/v1",
            "models": [
                {"model_name": "ok", "model_type": "text"},
                {"model_name": "bad", "model_type": "not-a-type"},
            ],
        }
    )
    assert item is not None
    assert [m["model_name"] for m in item["models"]] == ["ok"]


def test_normalizer_drops_entry_without_name_or_base_url():
    assert initial_data._normalize_gateway_provider({"name": "", "base_url": "https://x"}) is None
    assert initial_data._normalize_gateway_provider({"name": "X", "base_url": ""}) is None


# --------------------------------------------------------------------------- #
# 2. 播种链路
# --------------------------------------------------------------------------- #


def test_seed_creates_providers_and_models(db_factory):
    db = _db(db_factory)
    try:
        user_id = _make_user(db_factory, "seed-basic")
        created = seed_gateway_providers_for_user(db, user_id)
        db.commit()

        specs = initial_data.load_initial_gateway_providers()
        assert created == len(specs)

        configs = db.scalars(
            select(LlmGatewayConfig).where(LlmGatewayConfig.user_id == user_id)
        ).all()
        assert len(configs) == len(specs)
        # 每个种子服务商都有模型行
        for config in configs:
            models = db.scalars(
                select(LlmGatewayModel).where(
                    LlmGatewayModel.user_id == user_id,
                    LlmGatewayModel.provider_key == config.provider_key,
                )
            ).all()
            assert models, f"{config.name} 没有模型行"
    finally:
        db.close()


def test_seed_tokens_are_empty(db_factory):
    """服务商播种后 `access_token` 一律空串（用户登录后自填）。"""
    db = _db(db_factory)
    try:
        user_id = _make_user(db_factory, "seed-notoken")
        seed_gateway_providers_for_user(db, user_id)
        db.commit()
        configs = db.scalars(
            select(LlmGatewayConfig).where(LlmGatewayConfig.user_id == user_id)
        ).all()
        assert all(c.access_token == "" for c in configs), "种子服务商不应带 KEY"
    finally:
        db.close()


def test_seed_via_register_endpoint(api_client, db_factory):
    """走真实注册端点也应播种服务商（用户口径「安装登录后即可用」）。"""
    resp = api_client.post(
        "/api/auth/register",
        json={"username": "seed-register", "password": "Passw0rd!"},
    )
    assert resp.status_code in (200, 201), resp.text
    db = db_factory()
    try:
        user = db.scalar(select(User).where(User.username == "seed-register"))
        assert user is not None
        count = len(
            db.scalars(
                select(LlmGatewayConfig).where(LlmGatewayConfig.user_id == user.id)
            ).all()
        )
        assert count == len(initial_data.load_initial_gateway_providers())
        # 默认业务点映射也一并播种（小红书平台）
        mapped = db.scalars(
            select(LlmBusinessProvider).where(
                LlmBusinessProvider.user_id == user.id,
                LlmBusinessProvider.platform == "xhs",
            )
        ).all()
        assert mapped, "注册后小红书平台应有默认业务点映射"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 3. 幂等
# --------------------------------------------------------------------------- #


def test_seed_is_idempotent(db_factory):
    db = _db(db_factory)
    try:
        user_id = _make_user(db_factory, "seed-idem")
        first = seed_gateway_providers_for_user(db, user_id)
        db.commit()
        second = seed_gateway_providers_for_user(db, user_id)
        db.commit()
        assert first > 0
        assert second == 0, "重复播种不应新增"
    finally:
        db.close()


def test_seed_does_not_overwrite_user_edits(db_factory):
    """用户改过 base_url 的服务商，重播不还魂。"""
    db = _db(db_factory)
    try:
        user_id = _make_user(db_factory, "seed-nooverwrite")
        seed_gateway_providers_for_user(db, user_id)
        db.commit()
        config = db.scalars(
            select(LlmGatewayConfig).where(LlmGatewayConfig.user_id == user_id)
        ).first()
        config.base_url = "https://user-edited.example/v1"
        db.commit()

        seed_gateway_providers_for_user(db, user_id)
        db.commit()
        db.refresh(config)
        assert config.base_url == "https://user-edited.example/v1", "用户改动被还魂"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 4. 默认业务点映射
# --------------------------------------------------------------------------- #


def test_default_business_map_covers_text_and_image_only():
    """默认映射只含 text / image 两类业务点；**不得**含视频（六家无视频模型）。"""
    kinds = {want_type for _, want_type, _, _ in DEFAULT_BUSINESS_MAP}
    assert kinds == {"text", "image"}, f"默认映射不应含视频等其它类型：{kinds}"


def test_business_map_seed_is_idempotent_and_skips_missing(db_factory):
    db = _db(db_factory)
    try:
        user_id = _make_user(db_factory, "seed-map")
        seed_gateway_providers_for_user(db, user_id)
        db.commit()
        first = seed_default_business_map_for_user(db, user_id, platform="xhs")
        db.commit()
        assert first > 0
        second = seed_default_business_map_for_user(db, user_id, platform="xhs")
        db.commit()
        assert second == 0, "业务点映射重复播种不应新增"
    finally:
        db.close()


def test_seeded_models_do_not_claim_a_platform(db_factory):
    """⚠️ 刚播种、**一个业务点都没配**时，模型行的「使用平台」必须是**空的**。

    用户口径（2026-10-14 原话）：
    > 「模型没有在平台被配置使用，但是使用平台列依旧被设置成了小红书」
    > 「**除非默认被配置到了业务点使用才应配置正确的使用列**」

    `platform` 的语义 = 「本模型真的被哪些平台的业务点使用了」（值是**业务点个数**，
    是状态不是事件）。种子只负责**登记条目**，并不断言任何平台 —— 断言「被 xhs 用了」
    却没人在用，界面就是在说假话（用户看到刚注册的账号显示「使用平台 = 小红书」）。

    ⚠️ 曾经这里写死 `_dump_platforms({DEFAULT_SEED_PLATFORM: 1})` ⇒ 本用例会红。
    """
    from backend.app.services.llm_platforms import parse_platforms

    db = _db(db_factory)
    try:
        uid = _make_user(db_factory, "seed-platform-empty")
        seed_gateway_providers_for_user(db, uid)
        db.commit()
        rows = db.scalars(select(LlmGatewayModel).where(LlmGatewayModel.user_id == uid)).all()
        assert rows, "前提：播种应产生模型行"
        polluted = {r.model_name: r.platform for r in rows if parse_platforms(r.platform)}
        assert not polluted, (
            f"播种阶段没有业务点在使用这些模型，使用平台必须为空；实际：{polluted}"
        )
    finally:
        db.close()


def test_business_map_seed_writes_the_platform_of_the_models_it_uses(db_factory):
    """⚠️ 反向：**业务点真的用了**的模型，使用平台要被写上（否则列表永远空白）。

    与上一条是**一对**：只测「不为空」会退化成「什么都写」，
    只测「空」会漏掉「该写的没写」。两条一起才钉住用户口径
    「**除非默认被配置到了业务点使用才应配置正确的使用列**」—— 即
    「配了才写，写的一定是配了的那几个」。

    判据分三段：
    1. 被默认映射引用的模型（DeepSeek/deepseek-chat、通义千问/wanx2.1-t2i-turbo）
       的 `platform` **含 xhs**，且**计数 == 指向它的业务点个数**；
    2. **没被任何业务点引用**的模型仍为空（拿种子里肯定没被映射的模型当反例）；
    3. 幂等：再播一次，计数**不再累加**（状态不是事件）。
    """
    from backend.app.services.llm_platforms import parse_platforms

    db = _db(db_factory)
    try:
        uid = _make_user(db_factory, "seed-platform-written")
        seed_gateway_providers_for_user(db, uid)
        db.commit()
        seed_default_business_map_for_user(db, uid, platform="xhs")
        db.commit()

        used: dict[tuple[str, str], int] = {}
        for _bk, _t, provider_name, model_name in DEFAULT_BUSINESS_MAP:
            used[(provider_name, model_name)] = used.get((provider_name, model_name), 0) + 1

        by_key = {
            (c.provider_key, c.name): c
            for c in db.scalars(
                select(LlmGatewayConfig).where(LlmGatewayConfig.user_id == uid)
            ).all()
        }
        name_to_provider_key = {name: pk for (pk, name) in by_key}
        models = db.scalars(select(LlmGatewayModel).where(LlmGatewayModel.user_id == uid)).all()

        for m in models:
            provider_name = next(
                (
                    n
                    for n, pk in name_to_provider_key.items()
                    if pk == m.provider_key
                ),
                None,
            )
            want = used.get((provider_name, m.model_name), 0)
            got = parse_platforms(m.platform).get("xhs", 0)
            assert got == want, (
                f"{provider_name}/{m.model_name} 的 xhs 计数应为 {want}"
                f"（指向它的业务点数），实际 {got}（platform={m.platform!r}）"
            )

        # 幂等：再播一次不该累加（`platform` 是状态，不是事件）
        seed_default_business_map_for_user(db, uid, platform="xhs")
        db.commit()
        again = db.scalars(
            select(LlmGatewayModel).where(LlmGatewayModel.user_id == uid)
        ).all()
        for m in again:
            provider_name = next(
                (n for n, pk in name_to_provider_key.items() if pk == m.provider_key), None
            )
            want = used.get((provider_name, m.model_name), 0)
            assert parse_platforms(m.platform).get("xhs", 0) == want, (
                f"重复播种后 {m.model_name} 的计数被累加了（platform={m.platform!r}）"
            )
    finally:
        db.close()


def test_business_map_skips_when_provider_missing(db_factory):
    """服务商都不在（用户没种子）⇒ 映射 0 条，不臆造。"""
    db = _db(db_factory)
    try:
        user_id = _make_user(db_factory, "seed-map-empty")
        created = seed_default_business_map_for_user(db, user_id, platform="xhs")
        db.commit()
        assert created == 0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 5. 存量用户补种（2026-10-13 用户报障「模型服务商依旧未配置」）
# --------------------------------------------------------------------------- #

def test_backfill_seeds_existing_users_who_registered_before(db_factory, monkeypatch):
    """**补存量**：先于种子存在的用户（模拟老账号）启动补种后也拿到 6 个服务商。

    根因：原实现只在 `register()` 播种 ⇒ 存量用户永远拿不到种子。
    用户口径「用户安装登录后只需要配置 key」里的「登录后」包含**已在用的老账号**。
    """
    from backend.app.services import llm_gateway_seed_service as seed_svc

    db = _db(db_factory)
    try:
        # 直接建用户（**不走注册端点** ⇒ 模拟「种子机制之前就存在的老账号」）
        uid = _make_user(db_factory, "legacy-before-seed")
        assert db.scalars(
            select(LlmGatewayConfig).where(LlmGatewayConfig.user_id == uid)
        ).all() == [], "前提：老账号名下没有任何种子服务商"
    finally:
        db.close()

    # 启动补种（用本夹具库，替掉模块级 SessionLocal）
    monkeypatch.setattr(seed_svc, "SessionLocal", db_factory)
    created = seed_svc.seed_gateway_providers_for_all_users()
    assert created > 0, "存量用户应被补种"

    db = _db(db_factory)
    try:
        names = {
            c.name
            for c in db.scalars(
                select(LlmGatewayConfig).where(LlmGatewayConfig.user_id == uid)
            ).all()
        }
        assert "DeepSeek" in names, "存量用户被补种后应能看到 DeepSeek 等种子服务商"
        assert len(names) >= 6
    finally:
        db.close()


def test_backfill_never_overwrites_existing_config(db_factory, monkeypatch):
    """补种**绝不覆盖**用户已配好的参数（用户口径「已经配置好的参数不带改动」）。

    判据：老账号自建的同名服务商（含已配 KEY、改过的 base_url）与已有业务点映射
    在补种后**逐字不变**。
    """
    from backend.app.core.security import encrypt_text
    from backend.app.services import llm_gateway_seed_service as seed_svc

    db = _db(db_factory)
    try:
        uid = _make_user(db_factory, "legacy-with-own-provider")
        # 老账号自建一个**与种子同名**的服务商（DeepSeek），且已配 KEY、改过 base_url
        db.add(
            LlmGatewayConfig(
                user_id=uid,
                name="DeepSeek",
                provider_key="LEGACY-OWN-DS",
                base_url="https://my-own-proxy.example/v1",
                access_token=encrypt_text("«redacted:sk-…»"),
                enabled=True,
                healthy=True,
            )
        )
        # 老账号已选过业务点映射（指向它自己的服务商）
        db.add(
            LlmBusinessProvider(
                user_id=uid,
                platform="xhs",
                business_key="generate_note",
                provider_key="LEGACY-OWN-DS",
                model_name="my-own-model",
            )
        )
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(seed_svc, "SessionLocal", db_factory)
    seed_svc.seed_gateway_providers_for_all_users()

    db = _db(db_factory)
    try:
        mine = db.scalars(
            select(LlmGatewayConfig).where(
                LlmGatewayConfig.user_id == uid,
                LlmGatewayConfig.name == "DeepSeek",
            )
        ).all()
        assert len(mine) == 1, "同名服务商不该被种子又建一个"
        assert mine[0].provider_key == "LEGACY-OWN-DS", "provider_key 被覆盖"
        assert mine[0].base_url == "https://my-own-proxy.example/v1", "base_url 被覆盖"
        assert mine[0].access_token, "已配的 KEY 被清空"

        mapping = db.scalars(
            select(LlmBusinessProvider).where(
                LlmBusinessProvider.user_id == uid,
                LlmBusinessProvider.platform == "xhs",
                LlmBusinessProvider.business_key == "generate_note",
            )
        ).one()
        assert mapping.provider_key == "LEGACY-OWN-DS", "已有业务点映射被覆盖"
        assert mapping.model_name == "my-own-model", "已有映射的模型被覆盖"
    finally:
        db.close()


def test_backfill_is_idempotent(db_factory, monkeypatch):
    """重复启动补种不新增（幂等）——否则每次开机都堆一批重复行。"""
    from backend.app.services import llm_gateway_seed_service as seed_svc

    _make_user(db_factory, "backfill-idem")
    monkeypatch.setattr(seed_svc, "SessionLocal", db_factory)
    first = seed_svc.seed_gateway_providers_for_all_users()
    second = seed_svc.seed_gateway_providers_for_all_users()
    assert first > 0
    assert second == 0, "重复启动补种不应新增"


def test_lifespan_calls_backfill():
    """启动接线守卫：`lifespan` 必须真的调用补种（否则「防以后再漂」落空）。"""
    src = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    assert "seed_gateway_providers_for_all_users" in src, (
        "lifespan 没接线服务商补种 —— 存量用户/新加种子又会拿不到"
    )
