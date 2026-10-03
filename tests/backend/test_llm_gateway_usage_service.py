"""LlmGatewayUsageService：台账唯一写入点（设计 17 §3.13 / §4.3.1，v1.7）。

v1.7 二次转向后，网关由项目内建薄客户端直连上游，每次调用当场旁路记账；
本文件的用例钉死四类契约：

1. **落库字段完整**：一次记账 ⇒ 一行台账，平台/功能/模型/耗时/状态逐字落库。
2. **令牌映射命中与未命中**：命中写 `token_name`，未命中写 `""`（不猜、不降级）。
3. **反查规则**：`(user_id, base_url)` 双约束 —— 归一化对齐拼 endpoint 口径，
   跨用户同地址**不串号**，查不中**不落半条台账**。
4. **安全网**（DEF-048）：记账任何失败都不抛异常，业务调用照常成功。

⚠️ **本模块的用例只用一个夹具：`db_factory`**（被服务层直接调用，不经 HTTP），
因此**不能**依赖 `api_client` 的自动打桩 —— `db_factory` **不碰** `SessionLocal`。
`usage_service` 模块级持有 `SessionLocal` 且**自开 Session 写台账**，若不显式打桩，
`record_usage()` 会把它那行写到**生产库**，而测试库永远查不到（2026-09-30 实测：
8 个用例全红在 `len([]) == 0`，同时 8 行脏台账落进了 `data/spider_xhs.db`）。

⇒ 本模块用 `_bind_usage_service_to_test_db` **autouse 夹具**统一打桩，并用
`_assert_service_session_is_patched` 把「桩确实打上了」变成**会红的断言**：
安全网不能靠「测试没炸」推断（那正是假绿的定义，见 `conftest.patch_service_sessions` docstring）。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select

from backend.app.models import (
    LlmGatewayConfig,
    LlmGatewayUsageLog,
    User,
)
class _ModelConfigStub:
    """`ModelConfig` 的鸭子类型替身（v2.0：ORM 类已随 model_configs 表退场）。

    服务层只按属性名读取（`user_id` / `provider_key` / `base_url` /
    `model_config_id`），v2.0 的真实载体是 `ResolvedModelContext`。
    """

    def __init__(self, *, user_id=1, model_type="text", provider="p", name="m",
                 base_url="", provider_key="", model_config_id=None, **kw):
        self.user_id = user_id
        self.model_type = model_type
        self.provider = provider
        self.name = name
        self.base_url = base_url
        self.provider_key = provider_key
        self.model_config_id = model_config_id
        for k, v in kw.items():
            setattr(self, k, v)


from backend.app.services import llm_gateway_usage_service as usage
from backend.app.services import sqlite_retry


@pytest.fixture(autouse=True)
def _bind_usage_service_to_test_db(db_factory, monkeypatch) -> Iterator[None]:
    """把 `usage` 模块的 `SessionLocal` 打到本用例的测试库上（autouse）。

    用例自己再 `monkeypatch.setattr(usage, "SessionLocal", …)` 会被本夹具的
    打桩**覆盖**——顺序上用例体在夹具之后执行，所以两个安全网用例照样能装自己的
    `BoomSession`（它们要测的正是「每次新建 Session 都炸」，用哪个库无所谓）。
    """
    monkeypatch.setattr(usage, "SessionLocal", db_factory)
    yield


def _assert_service_session_is_patched(db_factory) -> None:
    """断言 `usage.SessionLocal` 确实是夹具库的工厂（假绿探测器）。

    `_bind_usage_service_to_test_db` 一旦被删掉 / 打桩失效，本断言先红 ——
    否则症状会是「测试库查不到行」这种**看起来像业务 bug** 的假红。
    """
    assert usage.SessionLocal is db_factory, (
        "usage_service 的 SessionLocal 未被打到测试库上：记账会写生产库且测试库查不到"
    )


def _seed_user(db) -> User:
    user = User(username="usage_owner", password_hash="x")
    db.add(user)
    db.flush()
    return user


def _seed_gateway(
    db,
    user: User,
    *,
    base_url: str,
    name: str = "本机网关",
    provider_key: str = "PROVIDER-A1",
) -> LlmGatewayConfig:
    """种一条服务商（v1.9：`provider_key` 为必填列）。

    ⚠️ `provider_key` **显式传参**而非由 name 派生 —— 中文名 slug 化后为空/重复，
    派生会让两个不同服务商撞同一个 KEY，掩盖本用例要验证的反查语义。
    ⚠️ **v2.7/v2.8**：服务商层已无 `platform` / `model_type` / `default_model` 三列。
    """
    gateway = LlmGatewayConfig(
        user_id=user.id,
        name=name,
        provider_key=provider_key,
        base_url=base_url,
        access_token="encrypted",
        enabled=True,
    )
    db.add(gateway)
    db.flush()
    return gateway


# ---------------------------------------------------------------------------
# 1. 落库字段
# ---------------------------------------------------------------------------

def test_record_usage_persists_all_fields(db_factory):
    _assert_service_session_is_patched(db_factory)
    with db_factory() as db:
        user = _seed_user(db)
        gateway = _seed_gateway(db, user, base_url="https://api.example.test/v1")
        db.commit()
        user_id, gateway_id = user.id, gateway.id

    ok = usage.record_usage(
        user_id=user_id,
        gateway_id=gateway_id,
        platform="xhs",
        function=usage.FUNCTION_GENERATE,
        model_name="deepseek-v4-flash",
        prompt_tokens=120,
        completion_tokens=340,
        latency_ms=1234,
        status=usage.STATUS_SUCCESS,
    )
    assert ok is True

    with db_factory() as db:
        rows = db.scalars(select(LlmGatewayUsageLog)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.user_id == user_id
        assert row.gateway_id == gateway_id
        assert row.platform == "xhs"
        assert row.function == "generate"
        assert row.model_name == "deepseek-v4-flash"
        assert row.prompt_tokens == 120
        assert row.completion_tokens == 340
        assert row.latency_ms == 1234
        assert row.status == "success"
        assert row.error_message == ""


def test_record_usage_marks_failure_with_reason(db_factory):
    """失败也要记一行（设计 17 §4.3.1）：失败率口径靠它，漏记则失败率恒 0。"""
    with db_factory() as db:
        user = _seed_user(db)
        gateway = _seed_gateway(db, user, base_url="https://api.example.test/v1")
        db.commit()
        user_id, gateway_id = user.id, gateway.id

    usage.record_usage(
        user_id=user_id,
        gateway_id=gateway_id,
        platform="xhs",
        function=usage.FUNCTION_POLISH,
        model_name="deepseek-v4-flash",
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=88,
        status=usage.STATUS_FAILED,
        error_message="HTTPError: 401 Client Error",
    )

    with db_factory() as db:
        row = db.scalars(select(LlmGatewayUsageLog)).one()
        assert row.status == "failed"
        assert row.error_message == "HTTPError: 401 Client Error"
        # 缺用量记 0，不造假（§5 硬约束 5）
        assert row.prompt_tokens == 0
        assert row.completion_tokens == 0


# ---------------------------------------------------------------------------
# 2. 令牌维度（⚠️ v1.9：令牌映射退场 ⇒ token_name 恒空）
# ---------------------------------------------------------------------------

def test_token_name_is_always_blank_v1_9(db_factory):
    """v1.9：`token_name` **恒落空串** —— 令牌映射表已退场（设计 17 §7.1a）。

    ⚠️ 本用例是**取代** `test_token_name_from_mapping_and_blank_when_absent` 的那条
    （原用例断言「命中映射 ⇒ 写映射名」，随 `llm_gateway_tokens` 退场已不成立）。
    两条不同 function 都记空 —— 证明**不再有任何反查**（原实现会因映射差异产生两个值）。
    """
    with db_factory() as db:
        user = _seed_user(db)
        gateway = _seed_gateway(db, user, base_url="https://api.example.test/v1")
        db.commit()
        user_id, gateway_id = user.id, gateway.id

    for function in (usage.FUNCTION_GENERATE, usage.FUNCTION_TITLE):
        usage.record_usage(
            user_id=user_id,
            gateway_id=gateway_id,
            platform="xhs",
            function=function,
            model_name="m",
            prompt_tokens=1,
            completion_tokens=1,
            latency_ms=1,
            status=usage.STATUS_SUCCESS,
        )

    with db_factory() as db:
        rows = db.scalars(select(LlmGatewayUsageLog).order_by(LlmGatewayUsageLog.id.asc())).all()
        assert [row.token_name for row in rows] == ["", ""]


def test_resolve_token_name_is_gone_v1_9():
    """v1.9：`resolve_token_name` **函数已删除**（令牌映射退场，设计 17 §7.1a）。

    ⚠️ 这是**退场守卫**：有人把函数加回来（比如为了「兼容」）会立刻红。
    留着它 = 一条永远查不到映射的死路径，只会让人以为「令牌维度还能用」。
    """
    assert not hasattr(usage, "resolve_token_name"), (
        "resolve_token_name 应随令牌映射一起删除（v1.9）"
    )


# ---------------------------------------------------------------------------
# 3. 服务商解析规则（v1.9：按 provider_key，取代 base_url 反查）
# ---------------------------------------------------------------------------

def test_resolve_gateway_id_for_model_by_provider_key(db_factory):
    """按 `model_config.provider_key` 命中服务商 id（v1.9 主路径）。"""
    with db_factory() as db:
        user = _seed_user(db)
        gateway = _seed_gateway(db, user, base_url="https://api.example.test/v1", provider_key="AB-1234")
        db.commit()
        user_id, gateway_id = user.id, gateway.id

    model_config = _ModelConfigStub(
        user_id=user_id,
        name="m",
        model_type="text",
        provider="p",
        base_url="https://whatever.example.test/v1",  # ⚠️ 地址故意与网关不同
        provider_key="AB-1234",
    )
    # 命中 ⇒ 取 service id；证明**不再看 base_url**
    assert usage.resolve_gateway_id_for_model(model_config) == gateway_id


def test_resolve_gateway_id_for_model_ignores_base_url_v1_9(db_factory):
    """⚠️ v1.9 取代 v1.8 的 base_url 反查（决策 D6）：**地址对也不认**。

    旧实现按 `normalize(base_url)` 逐行比 ⇒ 本用例时代会命中；
    新实现只认 `provider_key` ⇒ 必须 `None`。
    """
    with db_factory() as db:
        user = _seed_user(db)
        _seed_gateway(db, user, base_url="https://api.example.test/v1", provider_key="AB-1234")
        db.commit()
        user_id = user.id

    model_config = _ModelConfigStub(
        user_id=user_id,
        name="m",
        model_type="text",
        provider="p",
        base_url="https://api.example.test/v1",  # 与网关完全一致
        provider_key=None,  # 但没填 provider_key
    )
    assert usage.resolve_gateway_id_for_model(model_config) is None


def test_resolve_gateway_id_for_model_is_user_scoped(db_factory):
    """跨用户同 `provider_key` **不串号**：A 的调用绝不能记到 B 的台账上。"""
    with db_factory() as db:
        owner = _seed_user(db)
        intruder = User(username="usage_intruder", password_hash="x")
        db.add(intruder)
        db.flush()
        gateway = _seed_gateway(db, owner, base_url="https://shared.example.test/v1", provider_key="SAME-99")
        db.commit()
        owner_id, intruder_id, gateway_id = owner.id, intruder.id, gateway.id

    def _model(uid: int) -> _ModelConfigStub:
        return _ModelConfigStub(
            user_id=uid, name="m", model_type="text", provider="p",
            base_url="https://shared.example.test/v1", provider_key="SAME-99",
        )

    assert usage.resolve_gateway_id_for_model(_model(owner_id)) == gateway_id
    assert usage.resolve_gateway_id_for_model(_model(intruder_id)) is None


def test_resolve_gateway_id_for_model_returns_none_without_match(db_factory):
    with db_factory() as db:
        user = _seed_user(db)
        _seed_gateway(db, user, base_url="https://api.example.test/v1", provider_key="AB-1234")
        db.commit()
        user_id = user.id

    def _model(**kw) -> _ModelConfigStub:
        base = dict(
            user_id=user_id, name="m", model_type="text", provider="p",
            base_url="https://api.example.test/v1", provider_key="AB-1234",
        )
        base.update(kw)
        return _ModelConfigStub(**base)

    assert usage.resolve_gateway_id_for_model(_model(provider_key="NOPE-0000")) is None
    assert usage.resolve_gateway_id_for_model(_model(provider_key="")) is None
    assert usage.resolve_gateway_id_for_model(_model(provider_key="   ")) is None
    assert usage.resolve_gateway_id_for_model(_model(user_id=None)) is None


def test_record_usage_for_model_skips_when_base_url_unmatched(db_factory):
    """未指向服务商 ⇒ **不落半条台账**（宁缺勿造）：`gateway_id` 是外键，塞错就是脏数据。"""
    with db_factory() as db:
        user = _seed_user(db)
        db.commit()
        user_id = user.id

    model_config = _ModelConfigStub(
        user_id=user_id,
        name="未登记模型",
        model_type="text",
        provider="openai-compatible",
        model_name="gpt-unknown",
        base_url="https://unregistered.example.test/v1",
        provider_key=None,  # v1.9：未指向服务商
        is_default=True,
    )

    ok = usage.record_usage_for_model(
        model_config=model_config,
        platform="xhs",
        function=usage.FUNCTION_REWRITE,
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=50,
        status=usage.STATUS_SUCCESS,
    )
    assert ok is False

    with db_factory() as db:
        assert db.scalars(select(LlmGatewayUsageLog)).all() == []


def test_record_usage_for_model_delegates_to_record_usage(db_factory):
    with db_factory() as db:
        user = _seed_user(db)
        gateway = _seed_gateway(db, user, base_url="https://api.example.test/v1", provider_key="AB-1234")
        db.commit()
        user_id, gateway_id = user.id, gateway.id

    model_config = _ModelConfigStub(
        id=999,
        user_id=user_id,
        name="已登记模型",
        model_type="text",
        provider="openai-compatible",
        model_name="deepseek-v4-flash",
        base_url="https://api.example.test/v1/",
        provider_key="AB-1234",  # v1.9：按 KEY 命中（不再靠 base_url 归一化）
        is_default=True,
    )

    ok = usage.record_usage_for_model(
        model_config=model_config,
        platform="xhs",
        function=usage.FUNCTION_REWRITE,
        prompt_tokens=7,
        completion_tokens=9,
        latency_ms=30,
        status=usage.STATUS_SUCCESS,
    )
    assert ok is True

    with db_factory() as db:
        row = db.scalars(select(LlmGatewayUsageLog)).one()
        assert row.gateway_id == gateway_id
        assert row.model_name == "deepseek-v4-flash"
        assert row.function == "rewrite"
        # v1.9：令牌映射退场 ⇒ 台账该列恒空
        assert row.token_name == ""


# ---------------------------------------------------------------------------
# 4. 安全网（DEF-048）
# ---------------------------------------------------------------------------

def test_record_usage_never_raises_on_db_failure(monkeypatch):
    """库坏了也不能抛穿 —— 记账失败绝不让一次已成功的业务调用失败。"""

    class BoomSession:
        def __enter__(self):
            raise RuntimeError("database is locked")

        def __exit__(self, *exc):
            return False

    def boom_factory():
        return BoomSession()

    # resolve_token_name 也走 SessionLocal，一并打桩
    monkeypatch.setattr(usage, "SessionLocal", boom_factory)

    assert (
        usage.record_usage(
            user_id=1,
            gateway_id=1,
            platform="xhs",
            function=usage.FUNCTION_GENERATE,
            model_name="m",
            prompt_tokens=1,
            completion_tokens=1,
            latency_ms=1,
            status=usage.STATUS_SUCCESS,
        )
        is False
    )


def test_record_usage_for_model_never_raises_on_resolve_failure(monkeypatch):
    def boom_resolve(model_config):
        raise RuntimeError("boom")

    monkeypatch.setattr(usage, "resolve_gateway_id_for_model", boom_resolve)
    model_config = _ModelConfigStub(
        user_id=1, name="x", model_type="text", provider="p",
        base_url="https://a.test/v1", provider_key="AB-1234",
    )

    assert (
        usage.record_usage_for_model(
            model_config=model_config,
            platform="xhs",
            function=usage.FUNCTION_GENERATE,
            prompt_tokens=1,
            completion_tokens=1,
            latency_ms=1,
            status=usage.STATUS_SUCCESS,
        )
        is False
    )


# ---------------------------------------------------------------------------
# 5. token 用量提取
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"usage": {"prompt_tokens": 12, "completion_tokens": 34}}, (12, 34)),
        ({"usage": {}}, (0, 0)),
        ({}, (0, 0)),
        (None, (0, 0)),
        ("not-a-dict", (0, 0)),
        ({"usage": {"prompt_tokens": None, "completion_tokens": "34"}}, (0, 0)),
        ({"usage": {"prompt_tokens": -5, "completion_tokens": 3.7}}, (0, 3)),
        ({"usage": {"prompt_tokens": True, "completion_tokens": 1}}, (0, 1)),
    ],
)
def test_extract_usage_tokens(payload, expected):
    """缺 / 非法一律记 0（不造假）；bool 不算数字（`True` 是 1 但语义不是用量）。"""
    assert usage.extract_usage_tokens(payload) == expected


def test_function_constants_match_contract():
    """功能常量是台账与令牌映射之间的**唯一接口**，取值本身即契约（§4.3.1）。"""
    assert (
        usage.FUNCTION_REWRITE,
        usage.FUNCTION_GENERATE,
        usage.FUNCTION_TITLE,
        usage.FUNCTION_TAG,
        usage.FUNCTION_POLISH,
        usage.FUNCTION_DESCRIBE,
    ) == ("rewrite", "generate", "title", "tag", "polish", "describe")


# ---------------------------------------------------------------------------
# 6. 撞锁重试（DEF-085）
# ---------------------------------------------------------------------------

def _operational_error(message: str):
    """构造一个 SQLAlchemy `OperationalError`（真类，不是替身）。"""
    from sqlalchemy.exc import OperationalError

    return OperationalError("INSERT INTO llm_gateway_usage_logs", {}, Exception(message))


def test_record_usage_retries_on_lock_then_succeeds(monkeypatch, db_factory):
    """撞锁**重试**，第 2 次成功 ⇒ 返回 True 且台账**真的有一行**。

    这是 DEF-085 的核心判据：修前「一次撞锁即静默丢台账」，修后必须落库。
    仅断言 `ok is True` 不够 —— 变异「把重试删掉但仍返回 True」会假绿，
    故同时断言库里的行数与真实的下游 Session 调用次数。
    """
    real_factory = db_factory  # autouse 夹具已把它打进 usage.SessionLocal
    with real_factory() as db:
        user = _seed_user(db)
        gateway = _seed_gateway(db, user, base_url="https://api.example.test/v1")
        db.commit()
        user_id, gateway_id = user.id, gateway.id

    commit_calls = {"n": 0}

    class FlakySession:
        """第 1 次 commit 抛锁异常（模拟并发写锁竞争），第 2 次放行。"""

        def __init__(self, session):
            self._session = session

        def add(self, obj):
            return self._session.add(obj)

        def commit(self):
            commit_calls["n"] += 1
            if commit_calls["n"] == 1:
                self._session.rollback()
                raise _operational_error("database is locked")
            return self._session.commit()

        def close(self):
            return self._session.close()

    monkeypatch.setattr(usage, "SessionLocal", lambda: FlakySession(real_factory()))
    monkeypatch.setattr(sqlite_retry.time, "sleep", lambda _s: None)

    ok = usage.record_usage(
        user_id=user_id, gateway_id=gateway_id, platform="xhs",
        function=usage.FUNCTION_GENERATE, model_name="m",
        prompt_tokens=1, completion_tokens=1, latency_ms=1,
        status=usage.STATUS_SUCCESS,
    )

    assert ok is True
    assert commit_calls["n"] == 2, "必须真的重试了一次再成功"

    with real_factory() as db:
        rows = db.scalars(select(LlmGatewayUsageLog)).all()
    assert len(rows) == 1, "重试成功后台账必须真的落库（不能只返回 True）"


def test_record_usage_gives_up_after_retries_and_still_returns_false(monkeypatch, db_factory):
    """锁一直不放 ⇒ 重试用尽后返回 False，且**不抛穿**（DEF-048 不变）。"""
    attempts = {"n": 0}

    class AlwaysLocked:
        def add(self, obj):
            pass

        def commit(self):
            attempts["n"] += 1
            raise _operational_error("database is locked")

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(usage, "SessionLocal", lambda: AlwaysLocked())
    monkeypatch.setattr(sqlite_retry.time, "sleep", lambda _s: None)

    ok = usage.record_usage(
        user_id=1, gateway_id=1, platform="xhs", function=usage.FUNCTION_GENERATE,
        model_name="m", prompt_tokens=1, completion_tokens=1, latency_ms=1,
        status=usage.STATUS_SUCCESS,
    )

    assert ok is False
    assert attempts["n"] == sqlite_retry.RETRY_ATTEMPTS, "重试次数必须等于常量，不多不少"


def test_record_usage_does_not_retry_non_lock_errors(monkeypatch):
    """确定性错误（no such table）**不重试** —— 重试多少次都一样，只浪费业务线程。"""
    attempts = {"n": 0}

    class BoomSession:
        def add(self, obj):
            pass

        def commit(self):
            attempts["n"] += 1
            raise _operational_error("no such table: llm_gateway_usage_logs")

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(usage, "SessionLocal", lambda: BoomSession())
    monkeypatch.setattr(sqlite_retry.time, "sleep", lambda _s: None)

    ok = usage.record_usage(
        user_id=1, gateway_id=1, platform="xhs", function=usage.FUNCTION_GENERATE,
        model_name="m", prompt_tokens=1, completion_tokens=1, latency_ms=1,
        status=usage.STATUS_SUCCESS,
    )

    assert ok is False
    assert attempts["n"] == 1, "非锁类错误必须一次即止（不得空转重试）"


@pytest.mark.parametrize(
    "message, expected",
    [
        ("database is locked", True),
        ("sqlite3.OperationalError: database is locked", True),
        ("no such table: foo", False),
        ("UNIQUE constraint failed: x.y", False),
        ("disk I/O error", False),
    ],
)
def test_is_lock_error_only_matches_lock_contention(message, expected):
    """判据必须**只认锁竞争**：把 `no such table` 也当锁 ⇒ 确定性错误被无谓重试。"""
    assert sqlite_retry.is_lock_error(_operational_error(message)) is expected


def test_is_lock_error_rejects_non_operational_errors():
    """非 SQLAlchemy 异常一律不重试（例如业务层 ValueError）。"""
    assert sqlite_retry.is_lock_error(ValueError("database is locked")) is False
