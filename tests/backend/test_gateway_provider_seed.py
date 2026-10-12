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
