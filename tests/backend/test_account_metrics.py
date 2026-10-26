"""账号级互动数据（粉丝 / 关注 / 获赞）回归测试。

设计源：`docs/02_设计文档/12_功能设计_我的笔记与互动数据.md` §21
缺陷源：**DEF-039** —— 原实现从 `/v2/user/me` 读 `interactions`，而该响应
实测**没有**这个字段，于是三项恒为空（账号卡片永远显示 `-`）。

三条最容易被改回去的事实，每条都用断言钉住：

1. 计数**只**来自 `/v1/user/otherinfo`（改回 `/v2/user/me` 会立刻变红）；
2. **能力优先于类型**：creator-only 账号**零平台请求**直接返回可读原因；
3. 刷新失败**不得擦掉**已落库的好数据。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.api.accounts import (
    get_creator_account_adapter,
    get_pc_account_adapter,
    get_xhs_self_profile_adapter,
)
from backend.app.api.platforms.xhs.pc import (
    get_xhs_creator_api_adapter_factory,
    get_xhs_pc_api_adapter_factory,
)
from backend.app.core.database import Base, get_db
from backend.app.main import app
from backend.app.models import PlatformAccount, User
from backend.app.services.account_service import (
    CHANNEL_CREATOR,
    CHANNEL_WWW,
    clamp_reason,
    coerce_count,
    IDENTITY_UNKNOWN_METRICS_REASON,
    interaction_counts_from_otherinfo,
    METRICS_FETCH_ERROR_REASON,
    METRICS_REASON_MAX_CHARS,
    METRICS_REASON_MIN_CHARS,
    NO_COOKIE_METRICS_REASON,
    NO_WEB_SESSION_METRICS_REASON,
    PLATFORM_EMPTY_METRICS_REASON,
)
from tests.backend.cookie_fixtures import cookie_version, cookie_version_multi

from tests.backend.conftest import (  # noqa: PLC0415 - schema 复用助手
    _BUILT_SCHEMAS,
    _SCHEMA_BY_ENGINE,
    make_engine,
    reset_test_data,
)


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration

client = TestClient(app)

# 与 `tests/backend/test_self_content.py` 同一套 Cookie 形状：
# pc jar 带 web_session，creator jar 只有 galaxy_creator_session_id。
# `id_token` 必须一起给：只有 `web_session` 的是**游客会话**（DEF-051），
# 能力判定会（正确地）判成「不可认证网页版」。
PC_COOKIE_PAYLOAD = json.dumps(
    {"a1": "a1-value", "web_session": "web-session-value", "id_token": "id-token-value"}
)
CREATOR_COOKIE_PAYLOAD = json.dumps({"a1": "a1-value", "galaxy_creator_session_id": "creator-session"})

#: `creator_with_www` **自己的** www 会话（v1.31 夹具的修正，2026-09-26）。
#:
#: ⚠️ 原来它直接复用 `PC_COOKIE_PAYLOAD` —— 也就是 `metrics_pc` 那一份 www 会话。
#: `FakeProfileAdapter` 会把**被 check 的账号**回写成桩返回的 `user_id`，于是两个
#: 账号都被写成 `PC_EXTERNAL_USER_ID`：在旧唯一键（含 `sub_type`）下侥幸不冲突，
#: 2026-09-26 唯一键改成**按小红书 ID**之后立刻撞约束 —— 而夹具当时说的其实是
#: 「这两个账号是同一个人」，那本来就是错的：每个账号有**自己的** www 会话。
CREATOR_WITH_WWW_PC_COOKIE_PAYLOAD = json.dumps(
    {
        "a1": "creator-with-www-a1",
        "web_session": "creator-with-www-session",
        "id_token": "creator-with-www-id-token",
    }
)

#: `creator_with_www` 的 **creator** 通道 jar（也必须与 `creator_only` 的不同，
#: 理由同下：健康检查拿这条 jar 调 `get_user_info`，桩按 jar 给身份）。
CREATOR_WITH_WWW_CREATOR_COOKIE_PAYLOAD = json.dumps(
    {
        "a1": "creator-with-www-a1",
        "galaxy_creator_session_id": "creator-with-www-session",
    }
)

#: `failed_pc` 的 www 会话（同样必须与 `metrics_pc` 的不同 —— 见上）。
FAILED_PC_COOKIE_PAYLOAD = json.dumps(
    {"a1": "failed-pc-a1", "web_session": "failed-pc-session", "id_token": "failed-pc-id-token"}
)

PC_EXTERNAL_USER_ID = "5bdc4dddd7773d0001d028f2"
CREATOR_ONLY_EXTERNAL_USER_ID = "6a5f63a4000000000f03b802"
FAILED_PC_EXTERNAL_USER_ID = "6f2a1b3c4d5e6f708192a3b4"


def identity_for_jar(text: str) -> str:
    """一份 Cookie jar → 它**自己的**小红书 ID（两个桩共用的唯一实现）。

    ⚠️ 桩必须**按 jar 给身份**，不能一个常量返给所有账号：`/check` 会把探测到的
    身份**回写**账号行，常量于是把多行写成同一个小红书 ID —— 旧唯一键含 `sub_type`
    把这件事遮住了，2026-09-26 键改成「按小红书 ID」后立刻撞
    `uq_platform_accounts_xhs_identity`。语义上更是错的：**每个账号有自己的会话**。
    """
    if "creator-with-www" in text:
        return CREATOR_WITH_WWW_EXTERNAL_USER_ID
    if "galaxy_creator_session_id" in text:
        return CREATOR_ONLY_EXTERNAL_USER_ID
    if "failed-pc" in text:
        return FAILED_PC_EXTERNAL_USER_ID
    return PC_EXTERNAL_USER_ID

#: v1.31 夹具：creator_with_www 的 external_user_id。**不能与 PC_EXTERNAL_USER_ID
#: 相同** —— 唯一键含 sub_type 看似不冲突，但 `/check` 的登录适配器桩对**所有**
#: 账号都返回同一份 user_info（含 PC_EXTERNAL_USER_ID），会把 creator_only 也
#: 更新成同一个 id ⇒ 撞唯一约束。这正是「夹具共谋」的又一形态：桩返回固定身份。
CREATOR_WITH_WWW_EXTERNAL_USER_ID = "7c1d2e3f4a5b6c7d8e9f0a1b"

# 真实账号实测样本（逐字来自线上响应，见设计文档 §21.1；**不是编造的夹具**）
REAL_OTHERINFO_INTERACTIONS = [
    {"count": "258", "i18n_count": "258", "show": False, "type": "follows", "name": "关注"},
    {"show": False, "type": "fans", "name": "粉丝", "count": "49", "i18n_count": "49"},
    {"type": "interaction", "name": "获赞与收藏", "count": "39", "i18n_count": "39"},
]


class FakeProfileAdapter:
    """只桩两个取数点：身份（`/v2/user/me`）与账号资料（`/v1/user/otherinfo`）。

    记录调用次数是刻意的：`get_self_profile` 被调用 = 多花了一次请求，
    `get_other_info` 被调用 = 真的打了平台（creator-only 账号必须一次都不打）。
    """

    mode = "ok"  # ok | otherinfo_fail | empty | raise
    calls: dict = {}

    def get_self_profile(self, cookies_text: str):
        FakeProfileAdapter.calls["profile"] = FakeProfileAdapter.calls.get("profile", 0) + 1
        # 身份跟着**手里这份 www 会话**走，不是一个常量返给所有账号 ——
        # 常量会把两个账号回写成同一个小红书 ID（= 撞新唯一键，且语义上是错的）。
        user_id = (
            CREATOR_WITH_WWW_EXTERNAL_USER_ID
            if "creator-with-www" in cookies_text
            else PC_EXTERNAL_USER_ID
        )
        return True, "", {"data": {"user_id": user_id, "nickname": "我的号"}}

    def get_other_info(self, cookies_text: str, user_id: str):
        FakeProfileAdapter.calls.setdefault("otherinfo_users", []).append(user_id)
        if FakeProfileAdapter.mode == "otherinfo_fail":
            return False, "登录已过期", None
        if FakeProfileAdapter.mode == "raise":
            raise RuntimeError("boom")
        if FakeProfileAdapter.mode == "empty":
            return True, "", {"data": {"interactions": []}}
        return True, "", {
            "data": {
                "basic_info": {"red_id": "293066917"},
                "interactions": REAL_OTHERINFO_INTERACTIONS,
            }
        }


class FakePcLoginAdapter:
    """健康检查链路用的登录适配器桩（`/check` 会先调 get_user_info）。"""

    def get_user_info(self, cookies: dict):
        return {
            "external_user_id": identity_for_jar(json.dumps(cookies or {}, ensure_ascii=False)),
            "nickname": "我的号",
            "avatar_url": "https://cdn.example/avatar.jpg",
            "profile": {"red_id": "293066917"},
        }


def _setup(tmp_path, mode: str = "ok"):
    """建一个隔离库：owner（5 个账号）+ intruder（1 个账号）。

    | 账号            | sub_type | Cookie            | 预期                                   |
    |-----------------|----------|-------------------|----------------------------------------|
    | metrics_pc      | pc       | 带 web_session    | 取到真实计数并落库                     |
    | creator_only    | creator  | 仅 creator 通道   | **零平台请求** + 可读原因              |
    | creator_with_www| creator  | creator + www     | v1.31：计数走 **www 通道**取得到       |
    | no_cookie       | pc       | 无 Cookie 版本    | 200 + 可读原因（不报错）               |
    | failed_pc       | pc       | 带 web_session    | 平台失败 → 保留旧值                    |
    """
    engine = make_engine(tmp_path / 'account-metrics.db')
    #: 2026-10-26：同进程复用 schema ⇒ 表只建一次，之后只清数据。
    #: ⚠️ 不清会撞 `EMAIL_TAKEN`（本文件用固定用户名注册，上一轮的行还在）。
    _schema = _SCHEMA_BY_ENGINE.get(engine)
    if _schema is not None and _BUILT_SCHEMAS.get(_schema):
        reset_test_data(engine)
    else:
        Base.metadata.create_all(bind=engine)
        if _schema is not None:
            _BUILT_SCHEMAS[_schema] = True
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    FakeProfileAdapter.mode = mode
    FakeProfileAdapter.calls = {}
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_xhs_self_profile_adapter] = lambda: FakeProfileAdapter()
    app.dependency_overrides[get_pc_account_adapter] = lambda: FakePcLoginAdapter()
    app.dependency_overrides[get_creator_account_adapter] = lambda: FakePcLoginAdapter()
    # 适配器工厂保持真实实现：本文件的所有平台访问都收口在 profile adapter 上，
    # 真工厂不会被触发（如果被触发，说明接线走偏了，测试应当失败）。
    app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)
    app.dependency_overrides.pop(get_xhs_creator_api_adapter_factory, None)

    def register(username: str) -> str:
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "secret123"}
        )
        assert response.status_code == 200, response.text
        return response.json()["access_token"]

    owner_headers = {"Authorization": f"Bearer {register('owner')}"}
    intruder_headers = {"Authorization": f"Bearer {register('intruder')}"}

    db = SessionLocal()
    try:
        owner_id = db.scalar(select(User.id).where(User.username == "owner"))
        intruder_id = db.scalar(select(User.id).where(User.username == "intruder"))
        metrics_pc = PlatformAccount(
            user_id=owner_id,
            platform="xhs",
            sub_type="pc",
            external_user_id=PC_EXTERNAL_USER_ID,
            nickname="我的PC号",
            status="active",
            profile_json=json.dumps({"red_id": "293066917"}),
        )
        creator_only = PlatformAccount(
            user_id=owner_id,
            platform="xhs",
            sub_type="creator",
            external_user_id="6a5f63a4000000000f03b802",
            nickname="我的创作号",
            status="active",
            profile_json=json.dumps({"red_id": "49216354104"}),
        )
        # v1.31 夹具：**同一个 creator 账号同时存有 creator 与 www 两个通道**
        # （真实库 acct 8 正是这个形态）。旧实现按 sub_type 选 creator 通道 ⇒
        # 明明有 www 会话也取不到计数；新口径一律 www 通道 ⇒ 取得到。
        creator_with_www = PlatformAccount(
            user_id=owner_id,
            platform="xhs",
            sub_type="creator",
            external_user_id=CREATOR_WITH_WWW_EXTERNAL_USER_ID,
            nickname="带网页会话的创作号",
            status="active",
            profile_json=json.dumps({"red_id": "293066917"}),
        )
        no_cookie = PlatformAccount(
            user_id=owner_id, platform="xhs", sub_type="pc", nickname="没存Cookie的号", status="active"
        )
        failed_pc = PlatformAccount(
            user_id=owner_id,
            platform="xhs",
            sub_type="pc",
            # ⚠️ **必须与 `metrics_pc` 不同**（2026-09-20）。本夹具原来两个账号共用
            # `PC_EXTERNAL_USER_ID`，等于造了「同一小红书账号存在两条 pc 行」——
            # 这在现实中**不可能**，而它恰好和当时的缺陷（表上无唯一约束）互相掩护：
            # 约束一加上，夹具立刻插不进去。属于「**夹具变成缺陷的共谋**」的又一例。
            external_user_id=FAILED_PC_EXTERNAL_USER_ID,
            nickname="上次同步失败的号",
            status="active",
            # 上一次同步留下的好数据：本轮失败**不得**把它擦掉
            profile_json=json.dumps({"red_id": "293066917", "followers": 7, "following": 8, "likes": 9}),
        )
        foreign_pc = PlatformAccount(
            user_id=intruder_id, platform="xhs", sub_type="pc", nickname="别人的号", status="active"
        )
        db.add_all(
            [metrics_pc, creator_only, creator_with_www, no_cookie, failed_pc, foreign_pc]
        )
        db.commit()
        db.add_all(
            [
                cookie_version(metrics_pc.id, PC_COOKIE_PAYLOAD, CHANNEL_WWW),
                # creator-only：creator jar 落在 **creator 通道**（按 jar 形态判定，
                # 与生产写入同一条路径）—— 它没有 www 会话，取不到计数。
                cookie_version(creator_only.id, CREATOR_COOKIE_PAYLOAD, CHANNEL_CREATOR),
                # creator + www 两个通道并存（一行信封包两个平台）；
                # www 那一份是**它自己的**会话（见 CREATOR_WITH_WWW_PC_COOKIE_PAYLOAD）。
                cookie_version_multi(
                    creator_with_www.id,
                    {
                        CHANNEL_CREATOR: CREATOR_WITH_WWW_CREATOR_COOKIE_PAYLOAD,
                        CHANNEL_WWW: CREATOR_WITH_WWW_PC_COOKIE_PAYLOAD,
                    },
                ),
                cookie_version(failed_pc.id, FAILED_PC_COOKIE_PAYLOAD, CHANNEL_WWW),
            ]
        )
        db.commit()
        ids = {
            "pc": metrics_pc.id,
            "creator": creator_only.id,
            "creator_with_www": creator_with_www.id,
            "no_cookie": no_cookie.id,
            "failed": failed_pc.id,
            "foreign": foreign_pc.id,
        }
        return owner_headers, intruder_headers, ids, SessionLocal
    finally:
        db.close()


def _teardown():
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 解析层（单元）
# ---------------------------------------------------------------------------


def test_interaction_counts_map_real_platform_sample():
    """真实样本必须映射成 粉丝 49 / 关注 258 / 获赞 39（DEF-039 的验收数字）。"""
    counts = interaction_counts_from_otherinfo({"data": {"interactions": REAL_OTHERINFO_INTERACTIONS}})
    assert counts == {"followers": 49, "following": 258, "likes": 39}


def test_interaction_counts_return_empty_dict_not_zeros():
    """取不到就是**空 dict**：填 0 会把「没有值」伪装成「数值为零」。"""
    assert interaction_counts_from_otherinfo({"data": {}}) == {}
    assert interaction_counts_from_otherinfo({"data": {"interactions": []}}) == {}
    assert interaction_counts_from_otherinfo(None) == {}
    # 只认识 fans/follows/interaction 三种类型，未知类型不得污染字段
    assert interaction_counts_from_otherinfo(
        {"data": {"interactions": [{"type": "unknown", "count": "5"}]}}
    ) == {}


def test_coerce_count_handles_wans_and_rejects_garbage():
    assert coerce_count("258") == 258
    assert coerce_count(49) == 49
    assert coerce_count("1.2万") == 12000
    assert coerce_count("3w") == 30000
    assert coerce_count("1,234") == 1234
    assert coerce_count(None) is None
    assert coerce_count("") is None
    assert coerce_count("暂无") is None


def test_pages_per_scope_is_hard_capped_at_five():
    """扫描预算必须由请求模型硬夹紧（风控面 = 有界）。"""
    from backend.app.api.platforms.xhs.pc import SearchSelfContentRequest

    with pytest.raises(ValidationError):
        SearchSelfContentRequest(account_id=1, keyword="地垫", pages_per_scope=9)
    assert SearchSelfContentRequest(account_id=1, keyword="地垫").pages_per_scope == 3


# ---------------------------------------------------------------------------
# 端点：/api/accounts/{id}/metrics
# ---------------------------------------------------------------------------


def test_metrics_endpoint_persists_counts_read_from_otherinfo(tmp_path):
    """PC 链路：计数来自 `/v1/user/otherinfo`，并落库到 `profile_json`。"""
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        response = client.post(f"/api/accounts/{ids['pc']}/metrics", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["metrics_reason"] == ""
        assert payload["profile"]["followers"] == 49
        assert payload["profile"]["following"] == 258
        assert payload["profile"]["likes"] == 39

        # 落库（下次打开页面不需要再打平台）
        db = SessionLocal()
        try:
            account = db.get(PlatformAccount, ids["pc"])
            stored = json.loads(account.profile_json)
            assert stored["followers"] == 49 and stored["following"] == 258 and stored["likes"] == 39
            assert stored["red_id"] == "293066917"
        finally:
            db.close()
    finally:
        _teardown()


def test_metrics_endpoint_uses_stored_user_id_without_extra_identity_call(tmp_path):
    """`external_user_id` 可用时不额外调 `/v2/user/me`（省一次请求）。"""
    headers, _, ids, _ = _setup(tmp_path)
    try:
        assert client.post(f"/api/accounts/{ids['pc']}/metrics", headers=headers).status_code == 200
        assert FakeProfileAdapter.calls.get("profile") is None
        assert FakeProfileAdapter.calls["otherinfo_users"] == [PC_EXTERNAL_USER_ID]
    finally:
        _teardown()


def test_creator_only_account_makes_zero_platform_calls(tmp_path):
    """没有 www 会话 ⇒ **一次平台请求都不发**，直接给出可读原因（能力优先于类型）。

    v1.31（2026-09-21）：原因文案不再是「创作者平台不返回互动数据…」——
    那个归因已被真机抓包推翻（带 www 会话的 creator 账号走 www 通道取得到计数），
    错归因会让用户永远不去补登录。现在统一是「不含网页会话」。
    """
    headers, _, ids, _ = _setup(tmp_path)
    try:
        response = client.post(f"/api/accounts/{ids['creator']}/metrics", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["metrics_reason"]
        # 原因文案是「固定 + 短」的：25–30 字（R10 / D16），内容是「为什么是 0」+「怎么才能拿到」
        assert payload["metrics_reason"] == NO_WEB_SESSION_METRICS_REASON
        assert 25 <= len(payload["metrics_reason"]) <= 30
        # 关键断言：桩的调用记录必须是空的
        assert FakeProfileAdapter.calls == {}
        # 身份字段照旧可读（没有被这次「不可得」影响）
        assert payload["profile"]["red_id"] == "49216354104"
    finally:
        _teardown()


def test_creator_account_with_www_jar_gets_metrics_from_www_channel(tmp_path):
    """v1.31 核心行为：creator 账号**带 www 会话**时，计数走 www 通道取得到。

    旧实现按 `sub_type` 选通道（creator → creator 通道）⇒ 即使该账号存有 www jar
    也取不到计数，还给出「创作者平台不返回互动数据」的错原因（真实库 acct 8 实测）。
    新口径：**一律 www 通道 + 能力判定**，sub_type 不参与。
    """
    headers, _, ids, _ = _setup(tmp_path)
    try:
        response = client.post(f"/api/accounts/{ids['creator_with_www']}/metrics", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["metrics_reason"] == ""
        assert payload["profile"]["followers"] == 49
        assert payload["profile"]["following"] == 258
        assert payload["profile"]["likes"] == 39
        # 计数确实经过 PC 端接口（/v1/user/otherinfo）取回，且用的是账号自己的 id
        assert FakeProfileAdapter.calls.get("otherinfo_users") == [CREATOR_WITH_WWW_EXTERNAL_USER_ID]
    finally:
        _teardown()


def test_metrics_endpoint_keeps_previous_values_when_platform_fails(tmp_path):
    """平台失败：**保留旧值**，只回传原因（D：失败刷新不得擦数据）。"""
    headers, _, ids, SessionLocal = _setup(tmp_path, mode="otherinfo_fail")
    try:
        response = client.post(f"/api/accounts/{ids['failed']}/metrics", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["metrics_reason"]
        assert payload["profile"]["followers"] == 7
        assert payload["profile"]["likes"] == 9

        db = SessionLocal()
        try:
            stored = json.loads(db.get(PlatformAccount, ids["failed"]).profile_json)
            assert stored["followers"] == 7
        finally:
            db.close()
    finally:
        _teardown()


def test_metrics_endpoint_survives_adapter_exception(tmp_path):
    """适配器抛异常也必须降级为可读原因（可选步骤不能把整个请求打挂）。"""
    headers, _, ids, _ = _setup(tmp_path, mode="raise")
    try:
        response = client.post(f"/api/accounts/{ids['failed']}/metrics", headers=headers)
        assert response.status_code == 200, response.text
        assert "读取互动数据失败" in response.json()["metrics_reason"]
    finally:
        _teardown()


def test_metrics_endpoint_reports_missing_cookie_version(tmp_path):
    """没有 Cookie 版本的账号：200 + 可读原因（不是 500，也不是假的成功）。"""
    headers, _, ids, _ = _setup(tmp_path)
    try:
        response = client.post(f"/api/accounts/{ids['no_cookie']}/metrics", headers=headers)
        assert response.status_code == 200, response.text
        assert "Cookie" in response.json()["metrics_reason"]
    finally:
        _teardown()


def test_metrics_endpoint_rejects_other_users_account(tmp_path):
    headers, intruder_headers, ids, _ = _setup(tmp_path)
    try:
        assert client.post(f"/api/accounts/{ids['pc']}/metrics", headers=intruder_headers).status_code == 404
        assert client.post(f"/api/accounts/{ids['foreign']}/metrics", headers=headers).status_code == 404
    finally:
        _teardown()


def test_metrics_endpoint_requires_authentication(tmp_path):
    _, _, ids, _ = _setup(tmp_path)
    try:
        assert client.post(f"/api/accounts/{ids['pc']}/metrics").status_code == 401
    finally:
        _teardown()


def test_check_endpoint_shares_the_same_metrics_pipeline(tmp_path):
    """`/check` 与 `/metrics` 共用同一取数口径（同一事实不能有两个真相源）。"""
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        response = client.post(f"/api/accounts/{ids['pc']}/check", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["metrics_reason"] == ""
        assert payload["profile"]["followers"] == 49
        assert payload["status"] == "active"

        db = SessionLocal()
        try:
            stored = json.loads(db.get(PlatformAccount, ids["pc"]).profile_json)
            assert stored["following"] == 258
        finally:
            db.close()
    finally:
        _teardown()


def test_check_endpoint_reads_metrics_from_www_channel_for_creator(tmp_path):
    """v1.31：`/check` 对**带 www 会话的 creator 账号**也走 www 通道取计数。

    旧实现在 `/check` 里跟着健康检查的 creator 链路走 ⇒ get_self_profile 拿
    creator jar 打 PC 接口直接抛 ⇒ 原因变成「读取互动数据失败」，而账号其实
    取得到。现在健康检查（creator 链路）与互动数据（www 链路）分开选令牌。
    """
    headers, _, ids, _ = _setup(tmp_path)
    try:
        response = client.post(f"/api/accounts/{ids['creator_with_www']}/check", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["metrics_reason"] == ""
        assert payload["profile"]["followers"] == 49
        assert payload["profile"]["likes"] == 39
    finally:
        _teardown()


def test_check_endpoint_creator_without_www_makes_zero_metric_calls(tmp_path):
    """creator-only 账号 `/check`：健康检查照打（creator 链路），互动数据**零请求**。"""
    headers, _, ids, _ = _setup(tmp_path)
    try:
        response = client.post(f"/api/accounts/{ids['creator']}/check", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["metrics_reason"] == NO_WEB_SESSION_METRICS_REASON
        # 互动数据相关的一次都没打（profile adapter 完全没被调用）
        assert FakeProfileAdapter.calls == {}
        # 没有网页会话 ⇒ 不造计数（profile 里不会出现 followers 键）
        assert "followers" not in payload["profile"]
        # （red_id 会被健康检查链路的固定身份桩覆写，与本行为无关，不断言）
    finally:
        _teardown()


def test_enrich_keeps_existing_metrics_when_refresh_yields_nothing():
    """身份响应里没有计数而 `metrics=None` 时，已有值必须原样保留（DEF-039 的回退护栏）。"""
    from backend.app.services.account_service import enrich_user_info_with_xhs_self_profile

    user_info = {"profile": {"red_id": "293066917", "followers": 11, "following": 22, "likes": 33}}
    enriched = enrich_user_info_with_xhs_self_profile(
        user_info, {"data": {"basic_info": {"red_id": "293066917"}}}
    )
    assert enriched["profile"]["followers"] == 11
    assert enriched["profile"]["following"] == 22
    assert enriched["profile"]["likes"] == 33


# ---------------------------------------------------------------------------
# R10 / D16 · 缺值提示文案的 25–30 字窗口（用户 2026-09-18 晚拍板）
# ---------------------------------------------------------------------------
#
# 为什么值得钉住长度：最早的 creator 原因有 70 字（「创作者平台（creator.xiaohongshu.com）
# 不返回账号级互动数据：身份接口实测无任何计数，数据中心候选端点实测 404。需网页版登录的
# 账号才能读取该三项。」）——挂在卡片上等于没写。长度是**产品要求**，不是文案口味，
# 所以它必须是一条断言，否则下次改文案就悄悄长回去了。
#
# ⚠️ v1.31（2026-09-21）：creator 专用原因（`CREATOR_METRICS_REASON`）已**移除** ——
# 它的归因（「创作者平台不返回互动数据」）被真机抓包推翻，错归因比没有原因更糟
# （用户永远不去补登录）。现在「没有网页会话」对两类账号是同一句话。


def test_fixed_metrics_reasons_fit_the_25_to_30_char_window():
    """所有固定原因都落在 25–30 字窗口内（含卡片上出现的五种情形）。"""
    reasons = {
        "no_web_session": NO_WEB_SESSION_METRICS_REASON,
        "platform_empty": PLATFORM_EMPTY_METRICS_REASON,
        "fetch_error": METRICS_FETCH_ERROR_REASON,
        "identity_unknown": IDENTITY_UNKNOWN_METRICS_REASON,
        "no_cookie": NO_COOKIE_METRICS_REASON,
    }
    assert (METRICS_REASON_MIN_CHARS, METRICS_REASON_MAX_CHARS) == (25, 30)
    for name, reason in reasons.items():
        assert METRICS_REASON_MIN_CHARS <= len(reason) <= METRICS_REASON_MAX_CHARS, (
            f"{name} 的原因长度 {len(reason)} 超出 25–30 窗口：{reason}"
        )


def test_no_web_session_reason_says_why_and_how_without_internal_details():
    """取不到计数的原因要说清「缺什么」与「怎么补」，但不带域名 / 404 这类内部细节。

    v1.31 起这是**唯一**的「互动数据不可得」固定原因（不再有 creator 专用文案）——
    它指向的必须是**凭据**（网页会话），不能是平台能力（那句话是错的）。
    """
    assert "网页会话" in NO_WEB_SESSION_METRICS_REASON
    assert "网页版" in NO_WEB_SESSION_METRICS_REASON
    assert "创作者" not in NO_WEB_SESSION_METRICS_REASON  # 不按账号类型给原因
    assert "creator.xiaohongshu.com" not in NO_WEB_SESSION_METRICS_REASON  # 域名是内部细节
    assert "404" not in NO_WEB_SESSION_METRICS_REASON                      # 实测细节属于文档，不属于 UI


def test_creator_metrics_reason_constant_is_gone():
    """v1.31：`CREATOR_METRICS_REASON` 必须**不存在**（被推翻的归因不得复活）。

    用「import 不到」而不是「源码里没有」来断言：源码断言会被注释里的提及骗过
    （`account_service.py` 的迁移说明里就写着这个常量名）。
    """
    import backend.app.services.account_service as account_service

    assert not hasattr(account_service, "CREATOR_METRICS_REASON")


def test_clamp_reason_only_truncates_the_unbounded_parts():
    """动态部分（平台 msg / 异常文本）压到 30 字；短文案原样返回（不补齐、不加省略号）。"""
    assert clamp_reason("平台未返回互动数据：登录已过期") == "平台未返回互动数据：登录已过期"
    clamped = clamp_reason("平台未返回互动数据：" + "Cookie 已失效请重新登录" * 4)
    assert len(clamped) == METRICS_REASON_MAX_CHARS
    assert clamped.endswith("…")
    assert clamp_reason("") == ""
