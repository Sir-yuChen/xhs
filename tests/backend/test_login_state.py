"""账号登录态（L1/L2/L3）与扫码续期（设计 13 §18；2026-09-21 三平台拆分）。

要钉死的契约（每条都对应 §18 里的一条「不许」）：

1. **列表零平台请求**：L1/L2 是纯本地判据，卡片一多也不能变成 N 次探针
   （风控 §8；探针只随页面动作发生 —— 进入/刷新账号页时批量探活，v1.30；
   v1.31 起「检查」按钮已移除，没有第二个手工入口）；
2. **三层各有各的答案**：`missing` / `guest` / `unknown` / `valid` / `expired` 互斥且完整 ——
   L2 过关**不等于**可用（那是 `unknown`），只有探针能给出 `valid` / `expired`；
3. **`unknown` 绝不冒充 `expired`**：探针**没有完成**（传输异常，payload 为 None）时
   必须留在 `unknown`，不许替平台宣布令牌已死（DEF-057 同型红线）；
4. **三个平台各问各的**（拆分）：千帆探千帆、创作者探创作者平台、网页版探网页版 ——
   创作者 jar 打千帆只会 `-13000`，拿它判「千帆已失效」是句假话（观隅好物回归）；
5. **续期只写目标平台**：其它平台一个字节都不动，账号行数不变
   （不走 upsert ⇒ 不会因为「扫成另一个号」静默新建账号）；
6. **续期身份不符 / 无法确认 ⇒ 什么都不写**（§18.8 决定 ③A：宁可失败，不可静默改目标）；
7. **软删账号不给续期**（DEF-055 口径）；
8. **假归因不许有第二份**：「创作者平台没有这三类数据」已被真机抓包推翻，
   影响原因是**凭据**不是平台能力 —— 该句全仓不得再出现（DEF-058 只清掉了一份）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.app.core.database import Base, get_db  # noqa: E402
from backend.app.core.security import decrypt_text, encrypt_text  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.models import (  # noqa: E402
    AccountCookieVersion,
    LoginSession,
    PlatformAccount,
    User,
)
from backend.app.services import login_state_service  # noqa: E402
from backend.app.services.account_service import (  # noqa: E402
    CHANNEL_ARK,
    CHANNEL_CREATOR,
    CHANNEL_WWW,
    XhsSelfProfileAdapter,
    channel_jars,
    dump_channel_envelope,
)
from backend.app.services.shop_binding_service import (  # noqa: E402
    qianfan_cookies_for_account,
    shop_for_account,
)
from tests.backend.cookie_fixtures import bind_shop_account  # noqa: E402

client = TestClient(app)

#: 真实形态的 jar（键名与真机一致，值为占位）。
WWW_JAR = "a1=www-a1; web_session=ws-1; id_token=it-1"
#: 只有 `web_session` = **游客会话**（平台对未登录访客也会种，DEF-051）。
GUEST_JAR = "a1=g-1; web_session=ws-guest"
#: 千帆 jar：带 ark 域令牌（真机绑定表那份的形态）。
ARK_JAR = "a1=ark-a1; customer-sso-sid=sso-1; access-token-ark.xiaohongshu.com=ark-1"
#: 创作者扫码登录的 jar（真机 acct 8 形态）：**只有 creator 域令牌**，打不了千帆。
CREATOR_JAR = (
    "a1=c-a1; customer-sso-sid=sso-1; galaxy_creator_session_id=gcs-1; "
    "access-token-creator.xiaohongshu.com=creator-1"
)


def _override_database(tmp_path: Path) -> Any:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'login-state.db'}", connect_args={"check_same_thread": False}
    )
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db() -> Any:
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return testing_session


def _register(username: str) -> str:
    response = client.post("/api/auth/register", json={"username": username, "password": "secret123"})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _seed_account(db: Any, *, user_id: int, nickname: str, external_user_id: str = "", **kwargs: Any) -> PlatformAccount:
    account = PlatformAccount(
        user_id=user_id,
        platform="xhs",
        sub_type=kwargs.pop("sub_type", "pc"),
        nickname=nickname,
        external_user_id=external_user_id,
        status="active",
        **kwargs,
    )
    db.add(account)
    db.commit()
    return account


def _add_cookies(db: Any, account: PlatformAccount, cookies_text: str, channel: str) -> None:
    """落一行**信封**（平台由调用方指定，与登录路径「按形态判定」互为补充）。

    信封是唯一的存储形态（用户 2026-09-21 口径：一个字段存 JSON，没有 channel 列）。
    """
    db.add(
        AccountCookieVersion(
            platform_account_id=account.id,
            encrypted_cookies=encrypt_text(dump_channel_envelope({channel: cookies_text})),
        )
    )
    db.commit()


def _channels(payload: dict) -> dict:
    """列表项是 `login_channels`，单账号端点回的是 `channels` —— 两种形状都要认。

    （这不是「两套接口」，是两处消费：列表随账号一起下发，端点单独回答。测试同时
    覆盖两者，免得只测一个、另一个悄悄少了字段。）
    """
    if "login_channels" in payload:
        return payload["login_channels"]
    return payload.get("channels") or {}


def _state(response_json: dict, channel: str) -> str:
    return _channels(response_json)[channel]["state"]


class _ProbeSpy:
    """记录 L3 探针被调了几次 —— 「零请求」必须是**被证明**的，不是被假设的。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def install(self, monkeypatch: Any, *, www_result=("valid", "ok"), ark_result=("valid", "ok"), creator_result=("valid", "ok")) -> "_ProbeSpy":
        def fake_www(cookies, profile_adapter):  # noqa: ANN001
            self.calls.append("www")
            return www_result

        def fake_ark(cookies, adapter_factory):  # noqa: ANN001
            self.calls.append("ark")
            return ark_result

        def fake_creator(cookies, adapter_factory):  # noqa: ANN001
            self.calls.append("creator")
            return creator_result

        monkeypatch.setattr(login_state_service, "_probe_www", fake_www)
        monkeypatch.setattr(login_state_service, "_probe_ark", fake_ark)
        monkeypatch.setattr(login_state_service, "_probe_creator", fake_creator)
        return self


class _FakeSelfProfile:
    """`XhsSelfProfileAdapter.probe_self_profile` 的桩（真实三元组的形状）。"""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls = 0

    def probe_self_profile(self, cookies_text: str) -> Any:
        self.calls += 1
        return self.result


class _FakeQianfanAdapter:
    def __init__(self, result: Any) -> None:
        self.result = result

    def get_seller_info(self) -> Any:
        return self.result


class _FakeCreatorAdapter:
    def __init__(self, result: Any) -> None:
        self.result = result

    def get_creator_profile(self) -> Any:
        return self.result


class TestLayer12IsLocalAndComplete:
    """L1/L2：零平台请求，且五种取值都给得出来。"""

    def test_list_endpoint_reports_all_three_local_states_without_any_probe(
        self, tmp_path: Path, monkeypatch: Any
    ):
        session_factory = _override_database(tmp_path)
        spy = _ProbeSpy().install(monkeypatch)
        try:
            token = _register("login-state-owner")
            headers = {"Authorization": f"Bearer {token}"}
            db = session_factory()
            try:
                user = db.scalar(select(User).where(User.username == "login-state-owner"))
                bare = _seed_account(db, user_id=user.id, nickname="从未导入", external_user_id="u-1")
                guest = _seed_account(db, user_id=user.id, nickname="游客态", external_user_id="u-2")
                _add_cookies(db, guest, GUEST_JAR, CHANNEL_WWW)
                real = _seed_account(db, user_id=user.id, nickname="真登录", external_user_id="u-3")
                _add_cookies(db, real, WWW_JAR, CHANNEL_WWW)
                ark_only = _seed_account(db, user_id=user.id, nickname="只有ark", external_user_id="u-4")
                _add_cookies(db, ark_only, ARK_JAR, CHANNEL_ARK)
                creator_only = _seed_account(db, user_id=user.id, nickname="只有创作者", external_user_id="u-5")
                _add_cookies(db, creator_only, CREATOR_JAR, CHANNEL_CREATOR)
                ids = [bare.id, guest.id, real.id, ark_only.id, creator_only.id]
            finally:
                db.close()

            response = client.get("/api/accounts?platform=xhs", headers=headers)
            assert response.status_code == 200, response.text
            by_id = {item["id"]: item for item in response.json()["items"]}

            # ① 一个平台都没有 ⇒ 三个平台都 missing（**不是** unknown）
            for channel in (CHANNEL_WWW, CHANNEL_CREATOR, CHANNEL_ARK):
                assert _state(by_id[ids[0]], channel) == "missing"
            # ② 只有 web_session = 游客态
            assert _state(by_id[ids[1]], CHANNEL_WWW) == "guest"
            # ③ jar 是登录态，但**没探过** ⇒ unknown（L2 过关 ≠ 可用）
            assert _state(by_id[ids[2]], CHANNEL_WWW) == "unknown"
            # ④ ark 平台有值、www / creator 没有
            assert _state(by_id[ids[3]], CHANNEL_ARK) == "unknown"
            assert _state(by_id[ids[3]], CHANNEL_WWW) == "missing"
            assert _state(by_id[ids[3]], CHANNEL_CREATOR) == "missing"
            # ⑤ creator 平台有值、ark **没有**（拆分后两者不再是同一份）
            assert _state(by_id[ids[4]], CHANNEL_CREATOR) == "unknown"
            assert _state(by_id[ids[4]], CHANNEL_ARK) == "missing"

            # 每个平台都必须带 label 与 reason（前端不自己拼文案）
            entry = _channels(by_id[ids[2]])[CHANNEL_WWW]
            assert entry["label"]
            assert entry["reason"]
            assert entry["probed"] is False

            # **核心**：列表加载一个探针都没发。
            assert spy.calls == [], f"列表加载不得发探针，实际发了：{spy.calls}"
        finally:
            app.dependency_overrides.clear()

    def test_probe_only_runs_for_channels_that_are_actually_logged_in(
        self, tmp_path: Path, monkeypatch: Any
    ):
        """`missing` / `guest` 没有可用令牌，探针只会白花一次请求。"""
        session_factory = _override_database(tmp_path)
        spy = _ProbeSpy().install(monkeypatch)
        try:
            token = _register("probe-scope-owner")
            db = session_factory()
            try:
                user = db.scalar(select(User).where(User.username == "probe-scope-owner"))
                bare = _seed_account(db, user_id=user.id, nickname="空", external_user_id="b-1")
                guest = _seed_account(db, user_id=user.id, nickname="游客", external_user_id="b-2")
                _add_cookies(db, guest, GUEST_JAR, CHANNEL_WWW)
                ids = (bare.id, guest.id)
            finally:
                db.close()

            for account_id in ids:
                response = client.post(
                    f"/api/accounts/{account_id}/login-state",
                    json={"probe": True},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 200, response.text
            assert spy.calls == [], f"没有可用令牌时不得发探针，实际：{spy.calls}"
        finally:
            app.dependency_overrides.clear()


class TestLayer3TellsValidFromExpiredFromUnknown:
    """L3 的三种结局**不能压成两种** —— 这是本文件最重要的一组。"""

    def _probe(self, tmp_path: Path, profile_result: Any, account_nickname: str) -> dict:
        session_factory = _override_database(tmp_path)
        try:
            token = _register(f"probe-{account_nickname}")
            db = session_factory()
            try:
                user = db.scalar(select(User).where(User.username == f"probe-{account_nickname}"))
                account = _seed_account(db, user_id=user.id, nickname=account_nickname, external_user_id="p-1")
                _add_cookies(db, account, WWW_JAR, CHANNEL_WWW)
                account_id = account.id
            finally:
                db.close()
            fake = _FakeSelfProfile(profile_result)
            from backend.app.services.account_service import get_xhs_self_profile_adapter

            app.dependency_overrides[get_xhs_self_profile_adapter] = lambda: fake
            response = client.post(
                f"/api/accounts/{account_id}/login-state",
                json={"probe": True},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200, response.text
            assert fake.calls == 1, "显式探活必须真的发一次（否则下面的结论全是假的）"
            return response.json()
        finally:
            app.dependency_overrides.clear()

    def test_platform_answered_with_identity_is_valid(self, tmp_path: Path):
        payload = self._probe(
            tmp_path,
            (True, "ok", {"success": True, "data": {"nickname": "观隅好物", "user_id": "5bdc"}}),
            "validcase",
        )
        assert _state(payload, CHANNEL_WWW) == "valid"
        assert _channels(payload)[CHANNEL_WWW]["probed"] is True

    def test_platform_answered_without_identity_is_guest(self, tmp_path: Path):
        """真机口径：游客会话 `ok=True` 但 `nickname=None`（设计 12 §17.6.2 三组对照）。"""
        payload = self._probe(tmp_path, (True, "ok", {"success": True, "data": {"nickname": None}}), "guestcase")
        assert _state(payload, CHANNEL_WWW) == "guest"

    def test_platform_refused_with_a_body_is_expired(self, tmp_path: Path):
        payload = self._probe(
            tmp_path,
            (False, "登录已过期", {"success": False, "code": -100, "msg": "登录已过期"}),
            "expiredcase",
        )
        assert _state(payload, CHANNEL_WWW) == "expired"
        assert "登录已过期" in _channels(payload)[CHANNEL_WWW]["reason"]

    def test_probe_that_never_completed_stays_unknown_and_never_expired(self, tmp_path: Path):
        """⚠️ **红线**：`payload is None` = 平台根本没答复 ⇒ 不许说「已失效」。

        SDK 在传输异常时**不抛异常**，只把 `success` 置假、`res_json` 留 `None`
        （`apis/xhs_pc_apis.py::get_user_me`）。把它判成 `expired` 就是拿
        「没问到」冒充「不可用」—— 用户会去做一次**根本不需要**的重登（DEF-057 同型）。
        """
        payload = self._probe(tmp_path, (False, "HTTPSConnectionPool: Read timed out", None), "timeoutcase")
        state = _state(payload, CHANNEL_WWW)
        assert state == "unknown", f"传输失败不得判 expired，实际 {state}"
        assert state != "expired"
        assert _channels(payload)[CHANNEL_WWW]["probed"] is True

    def test_ark_probe_splits_credential_error_from_other_failures(self):
        """千帆：SDK 已把「凭据失效」（`code=-13000`）与「接口变了」分开，这里只在
        前者判 `expired`。"""
        expired, _ = login_state_service._probe_ark(
            ARK_JAR, lambda cookies: _FakeQianfanAdapter((False, "缺失登录凭证", {"code": -13000}))
        )
        assert expired == "expired"

        unknown, _ = login_state_service._probe_ark(
            ARK_JAR, lambda cookies: _FakeQianfanAdapter((False, "接口结构变化", {"code": -1}))
        )
        assert unknown == "unknown"

        unknown_none, _ = login_state_service._probe_ark(
            ARK_JAR, lambda cookies: _FakeQianfanAdapter((False, "连接超时", None))
        )
        assert unknown_none == "unknown"

        valid, _ = login_state_service._probe_ark(
            ARK_JAR, lambda cookies: _FakeQianfanAdapter((True, "ok", {"code": 0, "data": {"seller_name": "店"}}))
        )
        assert valid == "valid"

    def test_creator_probe_splits_the_three_outcomes(self):
        """创作者平台探针（`/api/galaxy/user/info`）同样三种结局，不能压成两种。"""
        valid, _ = login_state_service._probe_creator(
            CREATOR_JAR, lambda cookies: _FakeCreatorAdapter((True, "ok", {"success": True}))
        )
        assert valid == "valid"

        expired, _ = login_state_service._probe_creator(
            CREATOR_JAR, lambda cookies: _FakeCreatorAdapter((False, "登录态无效", {"success": False}))
        )
        assert expired == "expired"

        unknown, _ = login_state_service._probe_creator(
            CREATOR_JAR, lambda cookies: _FakeCreatorAdapter((False, "连接超时", None))
        )
        assert unknown == "unknown", "传输失败不得判 expired"

    def test_creator_jar_is_never_judged_valid_for_qianfan(self, tmp_path: Path, monkeypatch: Any):
        """**观隅好物回归（探针层）**：创作者 jar 打千帆探针 ⇒ `expired`，且**不是** valid。

        真机实测（acct 8）：这份 jar 打 `/api/edith/seller/info/v2` 拿到
        `code=-13000 缺失登录凭证`。拆分前它被存进 ark 通道，界面于是显示
        「千帆已失效」—— 而用户的千帆功能其实好好的（令牌在绑定表里）。
        """
        session_factory = _override_database(tmp_path)
        spy = _ProbeSpy().install(monkeypatch)
        try:
            token = _register("guanyu-probe")
            headers = {"Authorization": f"Bearer {token}"}
            db = session_factory()
            try:
                user = db.scalar(select(User).where(User.username == "guanyu-probe"))
                account = _seed_account(db, user_id=user.id, nickname="观隅好物", external_user_id="g-1")
                _add_cookies(db, account, CREATOR_JAR, CHANNEL_CREATOR)
                account_id = account.id
            finally:
                db.close()

            response = client.post(
                f"/api/accounts/{account_id}/login-state",
                json={"probe": True},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            channels = response.json()["channels"]
            # 千帆平台没有自己的令牌 ⇒ missing，**不会**拿创作者 jar 去探
            assert channels[CHANNEL_ARK]["state"] == "missing"
            # 创作者平台用自己的 jar 探 ⇒ 走了 creator 探针
            assert channels[CHANNEL_CREATOR]["state"] == "valid"
            assert spy.calls == ["creator"], f"只该探 creator，实际：{spy.calls}"
        finally:
            app.dependency_overrides.clear()

    def test_qianfan_without_own_ark_says_missing_instead_of_borrowing(self, tmp_path: Path):
        """账号只有 creator 令牌时，千帆如实说「还没有 ark 令牌」——**不借任何人的**。

        ⚠️ **被取代的旧断言（原句保留）**：本用例原名
        `test_qianfan_falls_back_to_binding_when_account_has_no_ark_platform`，断言
        *「账号只有 creator 平台时，千帆探针退回绑定表」* → `entry["state"] == "valid"`
        且 `"绑定表" in entry["reason"]`。

        用户 2026-09-22 拍板 **「每个账号只使用自己账号对应的平台有效登录令牌」**，
        绑定表兜底整条删除（连表都没了）。所以现在钉的是**否命题**：
        没有自有 ark 令牌 ⇒ 状态是 `missing` + 一句能自救的原因，
        且**一个平台请求都不发**（探活要靠令牌，没令牌就没什么可探的）。
        """
        session_factory = _override_database(tmp_path)
        try:
            token = _register("guanyu-binding")
            headers = {"Authorization": f"Bearer {token}"}
            db = session_factory()
            try:
                user = db.scalar(select(User).where(User.username == "guanyu-binding"))
                account = _seed_account(db, user_id=user.id, nickname="观隅好物", external_user_id="g-2")
                _add_cookies(db, account, CREATOR_JAR, CHANNEL_CREATOR)
                #: 绑了店（关系）但**不给** ark 令牌（凭据）—— 这两件事现在分开，
                #: 正是本用例要分开验证的东西（`cookies_text=""` 就是不写凭据）。
                bind_shop_account(db, account, cookies_text="", shop_name="观隅好物的店")
                db.commit()
                account_id = account.id
            finally:
                db.close()

            from backend.app.adapters.xhs.qianfan_api_adapter import (
                get_qianfan_api_adapter_factory,
            )

            calls: list = []
            app.dependency_overrides[get_qianfan_api_adapter_factory] = (
                lambda: lambda cookies: calls.append(cookies) or _FakeQianfanAdapter((True, "ok", {"code": 0}))
            )
            response = client.post(
                f"/api/accounts/{account_id}/login-state",
                json={"probe": True},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            entry = response.json()["channels"][CHANNEL_ARK]
            assert entry["state"] == "missing", f"没有自有令牌就必须说 missing：{entry}"
            assert "ark" in entry["reason"] or "千帆" in entry["reason"], entry["reason"]
            assert calls == [], "没有令牌时不该发探活请求（发出去也只会拿到 -13000）"
        finally:
            app.dependency_overrides.clear()

    def test_batch_endpoint_answers_every_account_in_one_request(self, tmp_path: Path, monkeypatch: Any):
        """批量端点（v1.30）：一次请求问完所有账号，探针按平台各发各的。"""
        session_factory = _override_database(tmp_path)
        spy = _ProbeSpy().install(monkeypatch)
        try:
            token = _register("batch-owner")
            headers = {"Authorization": f"Bearer {token}"}
            db = session_factory()
            try:
                user = db.scalar(select(User).where(User.username == "batch-owner"))
                one = _seed_account(db, user_id=user.id, nickname="一号", external_user_id="b-1")
                _add_cookies(db, one, WWW_JAR, CHANNEL_WWW)
                _add_cookies(db, one, CREATOR_JAR, CHANNEL_CREATOR)
                two = _seed_account(db, user_id=user.id, nickname="二号", external_user_id="b-2")
                _add_cookies(db, two, ARK_JAR, CHANNEL_ARK)
                ids = {one.id, two.id}
            finally:
                db.close()

            response = client.post("/api/accounts/login-states", json={"probe": True}, headers=headers)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["probe"] is True
            by_id = {item["platform_account_id"]: item for item in payload["accounts"]}
            assert set(by_id) == ids
            assert by_id[one.id]["channels"][CHANNEL_WWW]["state"] == "valid"
            assert by_id[one.id]["channels"][CHANNEL_CREATOR]["state"] == "valid"
            assert by_id[two.id]["channels"][CHANNEL_ARK]["state"] == "valid"
            # 一号探了 www + creator，二号探了 ark —— 没有令牌的平台不探
            assert sorted(spy.calls) == ["ark", "creator", "www"]

            # probe=false 时一个探针都不发（列表语义）
            spy.calls.clear()
            response = client.post("/api/accounts/login-states", json={"probe": False}, headers=headers)
            assert response.status_code == 200, response.text
            assert spy.calls == []
            assert response.json()["accounts"][0]["channels"][CHANNEL_WWW]["probed"] is False
        finally:
            app.dependency_overrides.clear()


class TestLoginPathStoresTheRightChannel:
    """DEF-059：**所有登录路径**落的 jar 是 **JSON 形态**，通道判定必须认识它。

    原实现只按 `;` / `=` 切分，而 JSON 串里既没有 `;` 也没有 `=` ⇒ `cookie_names()`
    恒为空集 ⇒ `channel_for_cookies()` 对每一份登录来的 jar 都返回 `www`。
    后果不是报错而是**静默归错通道**：创作者 / 千帆的 ark 令牌被写进 www，
    随后 `latest_cookies_for_channel(ark)` 取不到 ——「一个账号两条令牌」在登录路径上
    从未真正生效。既有用例全用 `k=v` 字符串写夹具，所以这条路径一次都没被走到
    （「夹具缺一个字段 = 长期替缺陷打掩护」的同型）。
    """

    def test_both_stored_shapes_are_understood(self):
        from backend.app.services.account_service import channel_for_cookies, has_web_session

        ark_json = json.dumps(
            {"a1": "x", "customer-sso-sid": "sso", "access-token-ark.xiaohongshu.com": "ark"},
            ensure_ascii=False,
        )
        creator_json = json.dumps(
            {"a1": "x", "customer-sso-sid": "sso", "access-token-creator.xiaohongshu.com": "c"},
            ensure_ascii=False,
        )
        www_json = json.dumps({"a1": "x", "web_session": "ws", "id_token": "it"}, ensure_ascii=False)
        assert channel_for_cookies(ark_json) == CHANNEL_ARK
        assert channel_for_cookies(creator_json) == CHANNEL_CREATOR
        assert channel_for_cookies(www_json) == CHANNEL_WWW
        # 两种形态对同一个问题的答案必须一致
        for text in (ark_json, "a1=x; customer-sso-sid=sso; access-token-ark.xiaohongshu.com=ark"):
            assert channel_for_cookies(text) == CHANNEL_ARK
        for text in (creator_json, "a1=x; customer-sso-sid=sso; access-token-creator.xiaohongshu.com=c"):
            assert channel_for_cookies(text) == CHANNEL_CREATOR
        for text in (www_json, "a1=x; web_session=ws; id_token=it"):
            assert channel_for_cookies(text) == CHANNEL_WWW
        assert has_web_session(www_json) is True, "JSON 形态的 www jar 也必须被认得"
        assert has_web_session(ark_json) is False

    def test_malformed_jar_does_not_crash_the_classifier(self):
        """判不出来 = 判不出来；不该让导入 / 登录链路 500。"""
        from backend.app.services.account_service import channel_for_cookies, cookie_names

        assert cookie_names("{这不是 JSON") == set()
        assert channel_for_cookies("{这不是 JSON") == CHANNEL_WWW

    def test_creator_qr_login_lands_in_creator_and_pc_qr_login_in_www(self, tmp_path: Path):
        session_factory = _override_database(tmp_path)
        try:
            from backend.app.api.login_sessions import get_creator_login_adapter, get_pc_login_adapter

            class _Pc:
                def create_qrcode(self):
                    return {
                        "cookies": {"a1": "t"},
                        "qr_id": "q",
                        "code": "c",
                        "qr_url": "https://example.test/q",
                        "device_state": {},
                        "host_cookie_state": {},
                    }

                def check_qrcode_status(self, qr_id, code, cookies, device_state=None, host_cookie_state=None):
                    return {
                        "status": "confirmed",
                        "cookies": {"a1": "n", "web_session": "ws", "id_token": "it"},
                    }

                def get_user_info(self, cookies):
                    return {"external_user_id": "pc-1", "nickname": "网页号", "avatar_url": ""}

            class _Creator:
                def create_qrcode(self):
                    return {"cookies": {"a1": "ct"}, "qr_id": "cq", "qr_url": "https://example.test/cq"}

                def check_qrcode_status(self, qr_id, cookies):
                    return {
                        "status": "confirmed",
                        "cookies": {
                            "a1": "cn",
                            "customer-sso-sid": "sso",
                            "access-token-creator.xiaohongshu.com": "c-token",
                        },
                    }

                def get_user_info(self, cookies):
                    return {"external_user_id": "cr-1", "nickname": "创作者号", "avatar_url": ""}

            app.dependency_overrides[get_pc_login_adapter] = lambda: _Pc()
            app.dependency_overrides[get_creator_login_adapter] = lambda: _Creator()

            token = _register("channel-on-login")
            headers = {"Authorization": f"Bearer {token}"}
            for kind in ("pc", "creator"):
                created = client.post(
                    f"/api/xhs/login-sessions/{kind}/qrcode", json={}, headers=headers
                ).json()
                polled = client.get(f"/api/xhs/login-sessions/{created['session_id']}", headers=headers).json()
                assert polled["account"] is not None, polled

            db = session_factory()
            try:
                from backend.app.core.security import decrypt_text
                from backend.app.services.account_service import parse_channel_envelope

                by_sub_type: dict[str, set[str]] = {}
                for row in db.scalars(select(AccountCookieVersion)).all():
                    account = db.get(PlatformAccount, row.platform_account_id)
                    platforms = set(parse_channel_envelope(decrypt_text(row.encrypted_cookies)))
                    by_sub_type.setdefault(account.sub_type, set()).update(platforms)
                assert by_sub_type["pc"] == {CHANNEL_WWW}
                assert by_sub_type["creator"] == {CHANNEL_CREATOR}, (
                    "创作者扫码登录的 jar 必须落 creator 平台 —— 它没有 ark 域令牌，"
                    "落进 ark 就等于告诉系统「它能打千帆」，而千帆只会回 -13000（观隅好物的教训）"
                )
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()


class TestRenewalWritesOnlyTheTargetChannel:
    """续期（设计 13 §18.8 决定 ③A）：只改我点开的这一个。"""

    class _PcAdapter:
        """扫码产物固定；身份可参数化，用来构造「扫成了另一个号」。"""

        def __init__(self, external_user_id: str = "5bdc-target", nickname: str = "观隅好物") -> None:
            self.external_user_id = external_user_id
            self.nickname = nickname

        def create_qrcode(self):
            return {
                "cookies": {"a1": "temp"},
                "qr_id": "renew-qr",
                "code": "renew-code",
                "qr_url": "https://example.test/qr",
                "device_state": {},
                "host_cookie_state": {},
            }

        def check_qrcode_status(self, qr_id, code, cookies, device_state=None, host_cookie_state=None):
            return {
                "status": "confirmed",
                "cookies": {
                    "a1": "renewed",
                    "web_session": "ws-new",
                    "id_token": "it-new",
                },
            }

        def get_user_info(self, cookies):
            return {
                "external_user_id": self.external_user_id,
                "nickname": self.nickname,
                "avatar_url": "",
            }

    class _CreatorAdapter:
        def create_qrcode(self):
            return {"cookies": {"a1": "c-temp"}, "qr_id": "c-qr", "qr_url": "https://example.test/c-qr"}

        def check_qrcode_status(self, qr_id, cookies):
            return {
                "status": "confirmed",
                "cookies": {
                    "a1": "c-renewed",
                    "customer-sso-sid": "sso-new",
                    "access-token-creator.xiaohongshu.com": "c-token-new",
                },
            }

        def get_user_info(self, cookies):
            return {"external_user_id": "ark-target", "nickname": "观隅好物", "avatar_url": ""}

    def _seed(self, tmp_path: Path, username: str) -> tuple[Any, dict, dict]:
        """两个账号：一个有 www + ark（用来验证「另一个平台不动」），一个只有 creator。

        ⚠️ **id 必须在 `close()` 之前取出来**：默认 `expire_on_commit=True`，
        commit 之后属性是过期状态，会话一关再读 `.id` 就是 `DetachedInstanceError`。
        """
        session_factory = _override_database(tmp_path)
        token = _register(username)
        db = session_factory()
        try:
            user = db.scalar(select(User).where(User.username == username))
            user_id = user.id
            dual = _seed_account(db, user_id=user_id, nickname="双平台", external_user_id="5bdc-target")
            _add_cookies(db, dual, "a1=old-www; web_session=ws-old; id_token=it-old", CHANNEL_WWW)
            _add_cookies(db, dual, "a1=old-ark; customer-sso-sid=sso-old; access-token-ark.xiaohongshu.com=ark-old", CHANNEL_ARK)
            ark_acct = _seed_account(
                db, user_id=user_id, nickname="创作者号", external_user_id="ark-target", sub_type="creator"
            )
            _add_cookies(db, ark_acct, "a1=old-creator; customer-sso-sid=sso-old2; access-token-creator.xiaohongshu.com=c-old2", CHANNEL_CREATOR)
            ids = {"dual": dual.id, "ark": ark_acct.id, "user": user_id}
        finally:
            db.close()
        return session_factory, {"Authorization": f"Bearer {token}"}, ids

    def _renew(self, headers: dict, account_id: int, adapter: Any, kind: str = "pc") -> dict:
        from backend.app.api.login_sessions import get_creator_login_adapter, get_pc_login_adapter

        app.dependency_overrides[get_pc_login_adapter] = lambda: adapter
        app.dependency_overrides[get_creator_login_adapter] = lambda: self._CreatorAdapter()
        created = client.post(
            f"/api/xhs/login-sessions/{kind}/qrcode",
            json={"target_account_id": account_id},
            headers=headers,
        )
        assert created.status_code == 200, created.text
        polled = client.get(f"/api/xhs/login-sessions/{created.json()['session_id']}", headers=headers)
        assert polled.status_code == 200, polled.text
        return polled.json()

    def test_renew_appends_to_www_and_leaves_ark_untouched(self, tmp_path: Path):
        session_factory, headers, ids = self._seed(tmp_path, "renew-www")
        try:
            polled = self._renew(headers, ids["dual"], self._PcAdapter())
            assert polled["renew_error"] is None, polled
            assert polled["renewed_channel"] == CHANNEL_WWW
            assert polled["account"]["id"] == ids["dual"]
            assert polled["account"]["action"] == "renewed"

            db = session_factory()
            try:
                from backend.app.core.security import decrypt_text
                from backend.app.services.account_service import parse_channel_envelope

                versions = db.scalars(
                    select(AccountCookieVersion)
                    .where(AccountCookieVersion.platform_account_id == ids["dual"])
                    .order_by(AccountCookieVersion.id)
                ).all()
                by_platform: dict[str, list[str]] = {}
                for row in versions:
                    for platform, jar in parse_channel_envelope(decrypt_text(row.encrypted_cookies)).items():
                        by_platform.setdefault(platform, []).append(jar)
                # www 多了一版；ark **一个字节都没动**
                assert len(by_platform[CHANNEL_WWW]) == 2
                assert by_platform[CHANNEL_ARK] == [
                    "a1=old-ark; customer-sso-sid=sso-old; access-token-ark.xiaohongshu.com=ark-old"
                ]
                assert "ws-new" in by_platform[CHANNEL_WWW][-1]
                # 账号行数不变（续期不走 upsert ⇒ 不会新建）
                assert len(db.scalars(select(PlatformAccount)).all()) == 2
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_renew_creator_platform_uses_creator_qr_and_keeps_www(self, tmp_path: Path):
        """创作者扫码续期落在 **creator 平台**（拆分后不再是 ark）。

        ⚠️ 这正是观隅好物的回归点：拆分前创作者 jar 落在 ark 通道上，把账号原有的
        千帆令牌顶掉 —— 千帆探针随后拿到一份打不开千帆的 jar，界面显示「千帆已失效」，
        而令牌其实好好的。
        """
        session_factory, headers, ids = self._seed(tmp_path, "renew-creator")
        try:
            polled = self._renew(headers, ids["ark"], self._PcAdapter(), kind="creator")
            assert polled["renew_error"] is None, polled
            assert polled["renewed_channel"] == CHANNEL_CREATOR

            db = session_factory()
            try:
                from backend.app.core.security import decrypt_text
                from backend.app.services.account_service import parse_channel_envelope

                rows = db.scalars(
                    select(AccountCookieVersion).where(
                        AccountCookieVersion.platform_account_id == ids["ark"]
                    )
                ).all()
                assert len(rows) == 2
                platforms = [set(parse_channel_envelope(decrypt_text(row.encrypted_cookies))) for row in rows]
                assert platforms == [{CHANNEL_CREATOR}, {CHANNEL_CREATOR}]
                assert "c-token-new" in parse_channel_envelope(decrypt_text(rows[-1].encrypted_cookies))[CHANNEL_CREATOR]
                # 另一个账号的 www / ark 平台完全没被碰过
                other = db.scalars(
                    select(AccountCookieVersion).where(
                        AccountCookieVersion.platform_account_id == ids["dual"]
                    )
                ).all()
                assert len(other) == 2
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_renew_refuses_a_different_account_and_writes_nothing(self, tmp_path: Path):
        """扫成另一个号 ⇒ 报错 + **一个字节都不写**，账号行数也不变。"""
        session_factory, headers, ids = self._seed(tmp_path, "renew-mismatch")
        try:
            polled = self._renew(
                headers, ids["dual"], self._PcAdapter(external_user_id="someone-else", nickname="别人的号")
            )
            assert polled["account"] is None
            assert polled["renewed_channel"] == ""
            assert polled["renew_error"]["code"] == "RENEW_IDENTITY_MISMATCH"
            assert "别人的号" in polled["renew_error"]["message"]
            assert "没有保存" in polled["renew_error"]["message"]

            db = session_factory()
            try:
                rows = db.scalars(select(AccountCookieVersion)).all()
                assert len(rows) == 3, "身份不符时不得写入任何令牌"
                assert len(db.scalars(select(PlatformAccount)).all()) == 2
                assert not any(
                    row.platform_account_id == ids["dual"] and "ws-new" in str(row.encrypted_cookies)
                    for row in rows
                )
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_renew_refuses_when_identity_cannot_be_verified(self, tmp_path: Path):
        """⚠️ 「没检查」不能当成「通过」：两边都拿不到可比字段 ⇒ 也算不一致。"""
        session_factory, headers, ids = self._seed(tmp_path, "renew-unverifiable")
        try:
            polled = self._renew(
                headers, ids["dual"], self._PcAdapter(external_user_id="", nickname="")
            )
            assert polled["account"] is None
            assert polled["renew_error"]["code"] == "RENEW_IDENTITY_MISMATCH"
            assert "无法确认" in polled["renew_error"]["message"]

            db = session_factory()
            try:
                assert len(db.scalars(select(AccountCookieVersion)).all()) == 3
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_renew_refuses_a_soft_deleted_or_foreign_account(self, tmp_path: Path):
        session_factory, headers, ids = self._seed(tmp_path, "renew-soft-deleted")
        try:
            db = session_factory()
            try:
                from backend.app.core.time import shanghai_now

                account = db.get(PlatformAccount, ids["dual"])
                account.deleted_at = shanghai_now()
                db.commit()
            finally:
                db.close()

            polled = self._renew(headers, ids["dual"], self._PcAdapter())
            assert polled["account"] is None
            assert polled["renew_error"]["code"] == "RENEW_TARGET_UNAVAILABLE"
            db = session_factory()
            try:
                assert len(db.scalars(select(AccountCookieVersion)).all()) == 3
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()


class TestNoSecondCopyOfTheDisprovenAttribution:
    """DEF-058 只清掉了一份；同一句话的第二份曾长期留在 `user_scope_reason`。"""

    def test_user_scope_reason_is_the_same_object_not_a_second_copy(self, tmp_path: Path):
        """**关系断言**，不是找某个词 —— 找词的断言会因别处改文案而静默失效。

        用户维度的「拿不到网页会话」原因必须**复用** `_web_session_required_detail`，
        两处各写一版就是「一处改、一处漏」的温床（DEF-058 实证）。
        """
        from backend.app.api.platforms.xhs.pc import (
            _self_content_capabilities,
            _web_session_required_detail,
        )

        session_factory = _override_database(tmp_path)
        db = session_factory()
        try:
            user = User(username="reason-owner", password_hash="x")
            db.add(user)
            db.flush()
            account = _seed_account(db, user_id=user.id, nickname="只有ark", external_user_id="r-1")
            _add_cookies(db, account, ARK_JAR, CHANNEL_ARK)
            capabilities = _self_content_capabilities(db, account, ARK_JAR)
            expected = _web_session_required_detail()["message"]
            # 三个用户维度**共用同一份原因**，且那一份就是 `_web_session_required_detail`
            for key in ("liked", "collected", "interactions"):
                assert capabilities["capabilities"][key]["available"] is False
                assert capabilities["capabilities"][key]["reason"] == expected
        finally:
            db.close()
            app.dependency_overrides.clear()

    def test_disproven_phrase_is_nowhere_in_the_backend(self):
        """该归因已被真机抓包推翻（创作者账号持 www 会话时三类端点全部可达）。

        留着它的代价不是「文案不美」而是**用户永远不会去重登** —— 一个错的归因
        比不说更糟。所以它是**全仓扫描**，不是只盯某一处。

        ⚠️ 判据走 **AST 的字符串常量**，并且**排除 docstring**：

        * 找子串的写法会被**注释**误伤（本仓库到处在讲这段历史）；
        * 而 docstring 是**说明**、不是可能被下发的文案 —— 真正的红线是
          「不许出现在任何**字符串字面量**里」（那才是会被渲染、被引用、被抄走的东西）。
        """
        import ast

        phrase = "创作者平台没有这三类数据"
        phrase_alt = "创作者平台不提供这三类数据"
        offenders = []
        for path in (ROOT / "backend").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = getattr(node, "body", None)
                    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                        if isinstance(body[0].value.value, str):
                            docstrings.add(id(body[0].value))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if id(node) in docstrings:
                    continue
                if phrase in node.value or phrase_alt in node.value:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        assert offenders == [], f"已被推翻的归因不得出现在任何字符串里：{offenders}"


def test_user_scope_reason_is_reused_not_rewritten():
    """**结构断言**：`user_scope_reason` 只能由**调用**得来，不能是一段字面量。

    为什么上面那条「值相等」还不够（变异验证实测出来的洞）：把
    `_web_session_required_detail` 的整段文案**逐字抄一遍**，值断言**照样绿** ——
    而逐字抄写正是「第二份实现」最阴的形态：今天一模一样，明天改一处就漂移，
    且没有任何一条断言会在那一刻变红。

    所以查的是**赋值右侧的形态**：必须是「对 `_web_session_required_detail` 的调用」
    （允许 `f(...)["message"]` 这种取字段，那仍然是复用同一份），
    而 `ast.Constant` / `ast.JoinedStr`（f-string 与隐式字符串拼接）都算抄写。
    """
    import ast

    def _reused_from(node: ast.AST) -> str | None:
        """`f(...)` 或 `f(...)[...]` → 被调函数名；字面量 → `None`。"""
        if isinstance(node, ast.Subscript):
            node = node.value
        if not isinstance(node, ast.Call):
            return None
        return getattr(node.func, "id", None) or getattr(node.func, "attr", None)

    tree = ast.parse((ROOT / "backend/app/api/platforms/xhs/pc.py").read_text(encoding="utf-8"))
    targets = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "user_scope_reason" for t in node.targets)
    ]
    assert targets, "`user_scope_reason` 不见了 —— 实现已改名？请同步本守卫，别直接删"
    for value in targets:
        assert _reused_from(value) == "_web_session_required_detail", (
            "`user_scope_reason` 必须**复用** `_web_session_required_detail(...)` 的返回值，"
            "不得抄成字面量（第二份实现 ⇒ 一处改一处漏）；"
            f"实际节点：{type(value).__name__} / 调用名：{_reused_from(value)}"
        )


class TestQrPanelHasNoSecondDetailParser:
    """`qr-login-panel.tsx` 曾自带第二份 `detail` 解析 + 硬编码的通用兜底。"""

    def _panel_source(self) -> str:
        return (ROOT / "frontend/src/components/account/qr-login-panel.tsx").read_text(encoding="utf-8")

    def _panel_code(self) -> str:
        """去掉注释后再断言 —— 注释里会**引用**被禁的写法来讲历史（本仓库的习惯），
        用全文找子串会被自己的说明文字误伤（同「字符串在文件里 ≠ 断言有效」）。"""
        source = self._panel_source()
        without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        return re.sub(r"//[^\n]*", "", without_block)

    def test_panel_uses_the_shared_parser(self):
        source = self._panel_source()
        assert "detailMessage" in source, "失败文案解析必须走唯一实现 lib/api.ts::detailMessage"

    def test_panel_does_not_reparse_detail_on_its_own(self):
        code = self._panel_code()
        assert "isAxiosError" not in code, "不得自带第二份 detail 解析（DEF-058 的教训）"
        assert "请稍后重试" not in code, "不得给做不到的建议（会话失效等一会儿不会变好）"

    def test_panel_has_no_creator_sync_checkbox_at_all(self):
        """「同步 Creator 账号」勾选**整条移除**（2026-09-23，§18.15.2）。

        ⚠️ **前提变化，断言改写（原文保留）**：旧断言
        `'accountType === "pc" && !renewing' in source` 守护的是「续期模式下
        隐藏勾选」；用户口径把勾选**从所有模式里移除**后，隐藏条件不复存在。
        新断言更强：面板代码里不允许再出现 `syncCreator` / `sync_creator`
        （查代码不查注释 —— 注释里说明历史正是本仓库的习惯）。
        """
        code = self._panel_code()
        assert "syncCreator" not in code, (
            "「登录 PC 后同步 Creator 账号」勾选回到了二维码面板；它已于 2026-09-23 "
            "整条移除（§18.15.2：一次登录只更新自己那个平台，PC / 创作者平台是两个入口）"
        )
        assert "sync_creator" not in code


class TestFrontendBehaviourScripts:
    """行为验证：源码断言证明不了「函数怎么跑」（DEF-058 的教训）。"""

    def _run(self, script: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["node", str(ROOT / script)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env={**os.environ, **(env or {})},
        )

    def test_login_state_mapping_is_driven_for_real(self):
        result = self._run("tests/frontend/login_state.cjs")
        if result.returncode != 0 and "node" in (result.stderr or "").lower() and "not found" in (result.stderr or "").lower():
            import pytest

            pytest.fail("需要 node 运行前端行为验证脚本，但 node 不可用")
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        # 20 条 = A(1) + B(4) + C(3) + D(2) + E(4) + F(2) + G(4)（§18.5 映射 + §18.14 标签 + tip 精简）
        assert "20 PASS / 0 FAIL" in result.stdout

    def test_unknown_never_renders_as_expired_even_under_mutation(self):
        """变异：把 `unknown` 的文案改成「已失效」⇒ 脚本必须变红。

        这条是**守卫承重**的证据：脚本若对这类改动无感，它就没在守任何东西。
        """
        result = self._run("tests/frontend/login_state.cjs", {"LOGIN_STATE_MUTATE": "label"})
        assert result.returncode != 0, "unknown 与 expired 撞文案时脚本必须失败"
        assert "B1 unknown 的文案 ≠ expired 的文案" in result.stdout

    def test_tag_labels_are_guarded_under_mutation(self):
        """变异：两条通道短名撞车 / 短名等于全名 ⇒ 脚本必须变红（设计 13 §18.14）。

        §18.14 把登录态从「卡片里单开两行」改成了「昵称后面两个 tag」。位置是省下来了，
        代价是每个 tag 只剩十来个字符 —— 于是「两条通道的短名必须互相可区分」
        （否则用户分不清「已失效」说的是千帆还是网页版）和「短名必须严格短于全名」
        （否则昵称被挤出卡片）**成了「标签化」这件事能不能成立的前提**。

        断言写成 `FAIL  F1` 这种**带前缀**的形式：只找 `F1` 的话，全绿时那行
        `PASS  F1 …` 同样命中，等于什么都没断言。
        """
        for mutate, expected in (("short", "F1"), ("shortlong", "F2")):
            result = self._run("tests/frontend/login_state.cjs", {"LOGIN_STATE_MUTATE": mutate})
            assert result.returncode != 0, f"{mutate} 变异必须让脚本失败"
            assert f"FAIL  {expected}" in result.stdout, f"stdout={result.stdout}"

    # ------------------------------------------------------------------ #
    # 2026-09-23 绑定弹窗改造（设计 13 §18.15）：行为脚本 add_account_modal.cjs
    # ------------------------------------------------------------------ #

    def test_binding_dialog_contract_holds(self):
        """弹窗 50vw×75vh / 无 Cookie 档 / 创作者平台文案 / 千帆双入口 / 无同步勾选。"""
        result = self._run("tests/frontend/add_account_modal.cjs")
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "ALL DIALOG CONTRACT HOLDS" in result.stdout

    def test_binding_dialog_size_is_guarded_under_mutation(self):
        """变异：把 50vw/75vh 改回旧尺寸 ⇒ 脚本必须变红（守卫承重的证据）。"""
        result = self._run("tests/frontend/add_account_modal.cjs", {"MODAL_MUTATE": "drawer"})
        assert result.returncode != 0, "尺寸回退时脚本必须失败"
        assert "FAIL  A1" in result.stdout, f"stdout={result.stdout}"

    # ------------------------------------------------------------------ #
    # 2026-09-23 补登弹窗对齐绑定弹窗（设计 13 §18.16）：行为脚本增补场景
    # ------------------------------------------------------------------ #

    def test_renew_dialog_matches_binding_matrix(self):
        """§18.16：补登弹窗按平台给登录方式（千帆 = 二维码/邮箱密码；网页版 /
        创作者 = 二维码/手机验证码），默认扫码，无 Cookie 粘贴入口。"""
        result = self._run("tests/frontend/add_account_modal.cjs")
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "ALL DIALOG CONTRACT HOLDS" in result.stdout

    def test_renew_dialog_default_qr_is_guarded_under_mutation(self):
        """变异：默认方式从「二维码」改成「手机验证码」⇒ A7/A8 必须变红。"""
        result = self._run("tests/frontend/add_account_modal.cjs", {"RENEW_MUTATE": "default"})
        assert result.returncode != 0, "默认方式被改时脚本必须失败"
        assert "FAIL  A7 www renew shows a QR by default" in result.stdout, f"stdout={result.stdout}"

    def test_renew_dialog_matrix_is_guarded_under_mutation(self):
        """变异：网页版 / 创作者档的「手机验证码」换成「邮箱密码」⇒ A7/A8 必须变红。"""
        result = self._run("tests/frontend/add_account_modal.cjs", {"RENEW_MUTATE": "matrix"})
        assert result.returncode != 0, "登录方式矩阵被改时脚本必须失败"
        assert "FAIL  A7 www renew offers" in result.stdout, f"stdout={result.stdout}"

    def test_renew_dialog_target_wiring_is_guarded_under_mutation(self):
        """变异：补登面板不再带 targetAccountId（续期退化成建号）⇒ A6/A7/A8 必须变红。"""
        result = self._run("tests/frontend/add_account_modal.cjs", {"RENEW_MUTATE": "notarget"})
        assert result.returncode != 0, "target 接线被拆时脚本必须失败"
        assert "FAIL  A7 send-code submits target_account_id in renew mode" in result.stdout, (
            f"stdout={result.stdout}"
        )


def test_renew_does_not_touch_the_ark_channel(tmp_path: Path):
    """创作者扫码续期**只写 creator 通道**，账号自己的 ark 令牌一个字节都不动。

    ⚠️ **被取代的旧断言（原句保留）**：本用例原名
    `test_renew_does_not_touch_the_binding_table`，钉的是
    *「§18.4：千帆取票是「账号 ark 平台优先、绑定表兜底」⇒ 续期**不需要**动绑定表」*
    —— 断言 `decrypt_text(binding.encrypted_cookies)` 原样保留。

    2026-09-22 绑定表整表移除，同一件事的落点换成 `account_cookie_versions` 的
    **ark 通道**：续期是「一次登录只更新它自己那个平台」，别的通道不受影响。

    ⚠️ 2026-09-21 拆分后还有第二半：**创作者扫码续期不再喂给千帆**。原断言是
    「千帆取票优先吃到新的 ark 令牌」—— 那正是观隅好物的事故形态（创作者 jar 被
    当成千帆令牌用，平台回 `-13000`）。现在创作者扫码落 creator 通道，
    千帆继续吃 ark 通道那份，两边互不打扰。
    """
    session_factory = _override_database(tmp_path)
    try:
        token = _register("binding-untouched")
        db = session_factory()
        try:
            user = db.scalar(select(User).where(User.username == "binding-untouched"))
            account = _seed_account(db, user_id=user.id, nickname="有绑定", external_user_id="ark-target")
            _add_cookies(db, account, "a1=old; customer-sso-sid=sso-old; access-token-ark.xiaohongshu.com=ark-old", CHANNEL_ARK)
            bind_shop_account(
                db,
                account,
                cookies_text="",
                shop_name="店",
            )
            db.commit()
            account_id, shop_id = account.id, account.shop_id_fk
        finally:
            db.close()

        from backend.app.api.login_sessions import get_creator_login_adapter, get_pc_login_adapter
        from backend.app.services.account_service import CHANNEL_CREATOR as _CREATOR

        class _Creator:
            def create_qrcode(self):
                return {"cookies": {"a1": "t"}, "qr_id": "q", "qr_url": "https://example.test/q"}

            def check_qrcode_status(self, qr_id, cookies):
                return {
                    "status": "confirmed",
                    "cookies": {"a1": "new", "customer-sso-sid": "sso-new",
                                "access-token-creator.xiaohongshu.com": "c-new"},
                }

            def get_user_info(self, cookies):
                return {"external_user_id": "ark-target", "nickname": "有绑定", "avatar_url": ""}

        app.dependency_overrides[get_pc_login_adapter] = lambda: None
        app.dependency_overrides[get_creator_login_adapter] = lambda: _Creator()
        created = client.post(
            "/api/xhs/login-sessions/creator/qrcode",
            json={"target_account_id": account_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        polled = client.get(
            f"/api/xhs/login-sessions/{created.json()['session_id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert polled.json()["renew_error"] is None, polled.text
        # 拆分后：创作者扫码落 **creator 平台**，不是 ark
        assert polled.json()["renewed_channel"] == _CREATOR

        db = session_factory()
        try:
            #: ark 通道原样保留（不因一次创作者续期被清空 / 改写）。
            #: `channel_jars` 是「按通道取最新一版」的唯一读法（原断言直读绑定行的
            #: `encrypted_cookies`，那张表 2026-09-22 已移除）。
            jars = channel_jars(db, account_id)
            assert "ark-old" in jars.get(CHANNEL_ARK, ""), jars
            assert "ark-bound" not in jars.get(CHANNEL_ARK, ""), "夹具没有第二份 ark 令牌"
            assert shop_id is not None
            shop = shop_for_account(db, account_id)
            assert shop is not None and shop.id == shop_id, "续期不得动店铺关系"

            # 账号自己的 ark 通道还在 ⇒ 千帆继续用它；创作者那份新令牌**不喂给千帆**
            cookies = qianfan_cookies_for_account(db, account_id)
            assert "ark-old" in cookies
            assert "c-new" not in cookies, "创作者通道的令牌不得被千帆取走（观隅好物回归）"
        finally:
            db.close()
    finally:
        app.dependency_overrides.clear()


def test_login_state_endpoint_is_documented_in_the_openapi_schema():
    """端点必须真的挂上去（而不是只在源码里「看起来存在」）。"""
    schema = client.get("/openapi.json").json()
    assert "/api/accounts/{account_id}/login-state" in schema["paths"]
    body = schema["paths"]["/api/accounts/{account_id}/login-state"]["post"]
    assert body["summary"] or body["description"]


def test_creating_a_session_without_a_target_behaves_exactly_as_before(tmp_path: Path):
    """**回归护栏**：不传 `target_account_id` 时，行为与改造前完全一致（仍然建号）。"""
    session_factory = _override_database(tmp_path)
    try:
        from backend.app.api.login_sessions import get_creator_login_adapter, get_pc_login_adapter

        class _Pc:
            def create_qrcode(self):
                return {
                    "cookies": {"a1": "t"},
                    "qr_id": "q",
                    "code": "c",
                    "qr_url": "https://example.test/q",
                    "device_state": {},
                    "host_cookie_state": {},
                }

            def check_qrcode_status(self, qr_id, code, cookies, device_state=None, host_cookie_state=None):
                return {"status": "confirmed", "cookies": {"a1": "n", "web_session": "ws", "id_token": "it"}}

            def get_user_info(self, cookies):
                return {"external_user_id": "brand-new", "nickname": "新号", "avatar_url": ""}

        app.dependency_overrides[get_pc_login_adapter] = lambda: _Pc()
        app.dependency_overrides[get_creator_login_adapter] = lambda: None
        token = _register("plain-add")
        created = client.post(
            "/api/xhs/login-sessions/pc/qrcode", json={}, headers={"Authorization": f"Bearer {token}"}
        )
        polled = client.get(
            f"/api/xhs/login-sessions/{created.json()['session_id']}",
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        assert polled["account"]["nickname"] == "新号"
        assert polled["account"]["action"] == "created"
        assert polled["renewed_channel"] == ""
        assert polled["renew_error"] is None
    finally:
        app.dependency_overrides.clear()


def test_target_account_id_survives_every_poll_round(tmp_path: Path):
    """续期目标必须**带过每一轮**：状态每轮重写一次，漏掉它就会退化成「添加新账号」。

    这里模拟「第一轮还没确认」：目标 id 写进临时状态后必须还在，否则确认轮
    会走 `_create_account_from_login()` —— 扫成别的号时就会**静默新建一行账号**。
    """
    session_factory = _override_database(tmp_path)
    try:
        from backend.app.api.login_sessions import get_creator_login_adapter, get_pc_login_adapter
        from backend.app.services.account_service import channel_for_cookies as _c

        class _PendingThenConfirmed:
            def __init__(self) -> None:
                self.rounds = 0

            def create_qrcode(self):
                return {
                    "cookies": {"a1": "t"},
                    "qr_id": "q",
                    "code": "c",
                    "qr_url": "https://example.test/q",
                    "device_state": {},
                    "host_cookie_state": {},
                }

            def check_qrcode_status(self, qr_id, code, cookies, device_state=None, host_cookie_state=None):
                self.rounds += 1
                if self.rounds == 1:
                    return {"status": "scanned", "cookies": {"a1": "t"}}
                return {"status": "confirmed", "cookies": {"a1": "n", "web_session": "ws", "id_token": "it"}}

            def get_user_info(self, cookies):
                return {"external_user_id": "5bdc-target", "nickname": "双通道", "avatar_url": ""}

        adapter = _PendingThenConfirmed()
        app.dependency_overrides[get_pc_login_adapter] = lambda: adapter
        app.dependency_overrides[get_creator_login_adapter] = lambda: None

        token = _register("renew-multi-round")
        db = session_factory()
        try:
            user = db.scalar(select(User).where(User.username == "renew-multi-round"))
            account = _seed_account(db, user_id=user.id, nickname="双通道", external_user_id="5bdc-target")
            account_id = account.id
        finally:
            db.close()

        created = client.post(
            "/api/xhs/login-sessions/pc/qrcode",
            json={"target_account_id": account_id},
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        headers = {"Authorization": f"Bearer {token}"}

        first = client.get(f"/api/xhs/login-sessions/{created['session_id']}", headers=headers).json()
        assert first["status"] == "scanned"
        assert first["renewed_channel"] == ""

        second = client.get(f"/api/xhs/login-sessions/{created['session_id']}", headers=headers).json()
        assert second["status"] == "confirmed"
        # 仍是续期（不是添加新账号）
        assert second["renewed_channel"] == _c("a1=n; web_session=ws; id_token=it")
        assert second["account"]["action"] == "renewed"

        db = session_factory()
        try:
            assert len(db.scalars(select(PlatformAccount)).all()) == 1
            assert len(db.scalars(select(AccountCookieVersion)).all()) == 1
        finally:
            db.close()
    finally:
        app.dependency_overrides.clear()


def test_login_state_module_has_no_ui_wording_duplication():
    """状态→文案的色彩/动作映射**只有前端一份**（`lib/login-state.ts`）。

    后端只给「状态 + 原因」，不给「该显示什么按钮」—— 那属于呈现，放在后端
    等于改一次版式就要动 API。这条守卫防的是「顺手在后端也写一份 label 映射」。
    """
    source = (ROOT / "backend/app/services/login_state_service.py").read_text(encoding="utf-8")
    # 真正的守卫：后端不得下发「动作」概念（那是呈现，放后端等于改版式就要动 API）
    assert '"action"' not in source
    assert "actionLabel" not in source
    # 前端的唯一实现确实存在
    frontend = (ROOT / "frontend/src/lib/login-state.ts").read_text(encoding="utf-8")
    assert "export function loginStateView" in frontend
    assert "export function loginChannelRows" in frontend


def test_frontend_channel_labels_are_the_fallback_copy_of_the_backend():
    """通道展示名只能有**一份真值**（后端 `CHANNEL_LABELS`），前端字典是它的回退副本。

    两份副本今天一样不代表明天一样：后端把「千帆 / 创作者」改个名字，卡片 tag 会跟着变
    （它读的是下发值），而弹窗标题走的仍是前端字典 —— **同一屏上同一个通道出现两个名字**，
    且没有任何断言会在那一刻变红。所以直接断言两者相等。
    """
    from backend.app.services.login_state_service import CHANNEL_LABELS

    source = (ROOT / "frontend/src/lib/login-state.ts").read_text(encoding="utf-8")
    # `[^\n=]*`（而不是 `[^=]*`）把匹配锁在同一行：否则从**注释里提到这个名字**的地方起
    # 可以一路跨行吞到下一个 `=`，把别的字典当成这一份读。
    block = re.search(r"export const LOGIN_CHANNEL_FALLBACK_LABELS[^\n=]*=\s*\{(.*?)\}", source, flags=re.S)
    assert block, "前端通道名回退字典（LOGIN_CHANNEL_FALLBACK_LABELS）不见了"
    pairs = dict(re.findall(r'(\w+):\s*"([^"]*)"', block.group(1)))
    assert pairs == CHANNEL_LABELS, f"通道展示名漂移：前端 {pairs} ≠ 后端 {CHANNEL_LABELS}"


def test_channel_display_name_has_no_second_implementation():
    """通道名映射**只许留在 `lib/login-state.ts`**（设计 13 §18.14）。

    收敛前它有三份：卡片行的回退、续期弹窗的 `CHANNEL_TEXT`、登录面板成功提示里的
    三元表达式。三份里改一份，屏幕上就会同时出现新旧两个名字 ——
    与 DEF-044/047「同一语义不留两份实现」同型。

    断言前**必须剥掉注释**：本仓库的习惯是在注释里引用被禁的写法讲历史
    （`renew-login-modal.tsx` 的文档注释里就写着「千帆 / 创作者」），
    直接对全文找子串会被自己的说明文字误伤。
    """
    offenders = []
    for rel in (
        "frontend/src/components/account/renew-login-modal.tsx",
        "frontend/src/components/account/qr-login-panel.tsx",
        "frontend/src/pages/platforms/xhs/accounts-page.tsx",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", text, flags=re.S))
        if "千帆 / 创作者" in code or "网页版（PC）" in code:
            offenders.append(rel)
    assert offenders == [], f"通道名出现了第二份实现，应收敛到 lib/login-state.ts：{offenders}"


def test_login_state_is_not_polled_or_scheduled():
    """§18.9.5：探针**不进轮询、不进定时任务**（风控）。

    守卫方式：调度器 / 定时任务模块不得引用登录态探针。
    """
    offenders = []
    for path in (ROOT / "backend/app/services").glob("*scheduler*.py"):
        if "login_state" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    for path in (ROOT / "backend/app/services").glob("*health*.py"):
        text = path.read_text(encoding="utf-8")
        if "login_state_service" in text:
            offenders.append(path.name)
    assert offenders == [], f"登录态探针不得进入定时巡检：{offenders}"


def test_list_payload_serialises_login_channels_for_the_card(tmp_path: Path):
    """列表里的 `login_channels` 必须是可 JSON 序列化的普通结构（前端直接吃）。"""
    session_factory = _override_database(tmp_path)
    try:
        token = _register("json-shape-owner")
        db = session_factory()
        try:
            user = db.scalar(select(User).where(User.username == "json-shape-owner"))
            account = _seed_account(db, user_id=user.id, nickname="序列化", external_user_id="j-1")
            _add_cookies(db, account, WWW_JAR, CHANNEL_WWW)
        finally:
            db.close()
        response = client.get("/api/accounts?platform=xhs", headers={"Authorization": f"Bearer {token}"})
        item = response.json()["items"][0]
        # 三个平台**永远都在**（哪怕 missing）—— 缺一个前端就分不清
        # 「这个账号没有这个平台的令牌」和「后端没算这个平台」
        assert set(item["login_channels"]) == {CHANNEL_WWW, CHANNEL_CREATOR, CHANNEL_ARK}
        # 往返一遍 JSON，确保没有 datetime / ORM 对象混进去
        assert json.loads(json.dumps(item["login_channels"])) == item["login_channels"]
    finally:
        app.dependency_overrides.clear()


def test_probe_self_profile_returns_the_raw_triple_not_a_raise():
    """`probe_self_profile` 的**形状**是契约：L3 靠 `payload is None` 区分
    「平台拒绝了」与「根本没问到」。若它改成 `raise`（像 `get_self_profile` 那样），
    这两种情况就再也分不开，`unknown` 那条红线会静默失守。

    ⚠️ 判据走 **AST**，不是找字符串：本函数的 docstring 里就写着 `raise` 这个词，
    用 `"raise" in source` 判会**恒假绿**（同一类事故见 DEF-056：文案里含某词 ≠ 行为如此）。
    """
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(XhsSelfProfileAdapter.probe_self_profile))
    tree = ast.parse(source)
    raises = [node for node in ast.walk(tree) if isinstance(node, ast.Raise)]
    assert raises == [], "探针不得抛异常 —— 抛了就分不清「平台拒绝」与「没问到」"
    assert "get_self_profile()" in source, "必须复用同一个取数口，不得另写一份请求"


# ---------------------------------------------------------------------------
# 千帆扫码续期（2026-09-21，用户要求「千帆也能扫码登录」；真机抓包验证）
# ---------------------------------------------------------------------------
class TestQianfanQrLogin:
    """千帆扫码 = 与创作者同一套 CAS 流程，service 指向 ark。

    真机抓包（隔离 profile + CDP，用户本人扫码）证明：
    `customer.xiaohongshu.com/login?service=https://ark.xiaohongshu.com/...`
    扫码成功后种下 `access-token-ark.xiaohongshu.com` 等千帆域令牌。
    """

    #: 真机抓包拿到的 jar 形态（键名与真机一致）。
    ARK_QR_JAR = {
        "a1": "qr-a1",
        "customer-sso-sid": "qr-sso",
        "access-token-ark.xiaohongshu.com": "qr-ark-token",
        "x-user-id-ark.xiaohongshu.com": "qr-ark-uid",
        "beaker.session.id": "qr-beaker",
    }

    def _fake_adapter(self, shop: dict | None = None):
        class _Qianfan:
            def create_qrcode(self):
                return {
                    "cookies": {"a1": "temp"},
                    "qr_id": "qf-qr",
                    "qr_url": "https://customer.xiaohongshu.com/loginconfirm?qrCodeId=qf-qr",
                }

            def check_qrcode_status(self, qr_id, cookies):
                return {"status": "confirmed", "cookies": self.jar}

            def get_shop_info(self, cookies):
                if shop is None:
                    raise RuntimeError("千帆登录态校验失败")
                return shop

        _Qianfan.jar = dict(self.ARK_QR_JAR)
        return _Qianfan()

    def _env(self, tmp_path: Path, username: str):
        session_factory = _override_database(tmp_path)
        token = _register(username)
        db = session_factory()
        try:
            user = db.scalar(select(User).where(User.username == username))
            user_id = user.id
            account = _seed_account(db, user_id=user_id, nickname="观隅好物", external_user_id="qf-1")
            ids = {"account": account.id, "user": user_id}
        finally:
            db.close()
        return session_factory, {"Authorization": f"Bearer {token}"}, ids

    def test_qr_code_uses_the_ark_service_url(self):
        """二维码必须带 `service=<ark URL>` —— 少了它，扫码拿到的是创作者令牌。

        ⚠️ **必须经适配器的 `_api()` 间接验证**：直接 `sdk.XHSCreatorLoginApi(
        service_url=...)` 自己传参，测的是 SDK 而不是适配器 —— 适配器忘了传时
        这条用例照样绿（变异验证 ⑯ 实测抓到的洞：注入「不传 service_url」全绿）。
        """
        import apis.xhs_creator_login_apis as sdk

        from backend.app.adapters.xhs.qianfan_login_adapter import (
            ARK_SERVICE_URL,
            XhsQianfanLoginAdapter,
        )

        captured: dict = {}
        original = sdk.XHSCreatorLoginApi.__init__

        def spy_init(self, **kwargs):
            captured.update(kwargs)
            original(self, **kwargs)

        sdk.XHSCreatorLoginApi.__init__ = spy_init
        try:
            # 走适配器自己的构造路径（不发真实请求：只到 init 就够）
            XhsQianfanLoginAdapter()._api()
        finally:
            sdk.XHSCreatorLoginApi.__init__ = original
        assert captured.get("service_url") == ARK_SERVICE_URL == "https://ark.xiaohongshu.com"
        # 默认值必须仍是创作者 URL（创作者扫码行为不变）
        default_api = sdk.XHSCreatorLoginApi()
        assert default_api.service_url == default_api.creator_url, (
            "不传 service_url 时必须与从前逐字节一致（创作者扫码）"
        )

    def test_qianfan_renewal_writes_the_ark_platform(self, tmp_path: Path):
        session_factory, headers, ids = self._env(tmp_path, "qf-renew-ok")
        try:
            from backend.app.api.login_sessions import get_qianfan_login_adapter

            app.dependency_overrides[get_qianfan_login_adapter] = lambda: self._fake_adapter(
                {"shop_user_id": "SELLER-1", "shop_name": "观隅好物的店"}
            )
            created = client.post(
                "/api/xhs/login-sessions/qianfan/qrcode",
                json={"target_account_id": ids["account"]},
                headers=headers,
            )
            assert created.status_code == 200, created.text
            polled = client.get(
                f"/api/xhs/login-sessions/{created.json()['session_id']}", headers=headers
            ).json()
            assert polled["renew_error"] is None, polled
            assert polled["renewed_channel"] == "ark", "千帆扫码必须落 ark 平台"
            assert polled["renewed_shop"]["shop_name"] == "观隅好物的店"

            db = session_factory()
            try:
                jars = channel_jars(db, ids["account"])
                assert set(jars) == {"ark"}, f"只该写 ark 平台，实际：{set(jars)}"
                assert "access-token-ark.xiaohongshu.com" in jars["ark"]
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_qianfan_renewal_refuses_a_different_shop(self, tmp_path: Path):
        """账号已绑定 A 店，扫了 B 店的码 ⇒ 报错 + **一个字节都不写**（§18.8 ③A 同型）。"""
        session_factory, headers, ids = self._env(tmp_path, "qf-renew-mismatch")
        db = session_factory()
        try:
            #: 2026-09-22：店铺关系写 `platform_accounts.shop_id_fk`（原来是绑定表一行），
            #: 凭据写账号自己的 `ark` 通道。
            account = db.get(PlatformAccount, ids["account"])
            assert account is not None
            bind_shop_account(
                db, account, cookies_text=ARK_JAR, shop_name="我的店", seller_id="SELLER-1"
            )
            db.commit()
            #: 续期前的令牌快照 —— 用来断言「对不上店铺时一个字节都没写」。
            #: （旧断言是 `channel_jars(...) == {}`，那依赖「夹具只有绑定表、没有 acv 行」
            #: 这个旧形状；现在凭据本来就在通道里，所以改成比快照。）
            jars_before = channel_jars(db, ids["account"])
        finally:
            db.close()
        try:
            from backend.app.api.login_sessions import get_qianfan_login_adapter

            app.dependency_overrides[get_qianfan_login_adapter] = lambda: self._fake_adapter(
                {"shop_user_id": "SELLER-OTHER", "shop_name": "别人家的店"}
            )
            created = client.post(
                "/api/xhs/login-sessions/qianfan/qrcode",
                json={"target_account_id": ids["account"]},
                headers=headers,
            )
            polled = client.get(
                f"/api/xhs/login-sessions/{created.json()['session_id']}", headers=headers
            ).json()
            assert polled["account"] is None
            assert polled["renewed_channel"] == ""
            assert polled["renew_error"]["code"] == "RENEW_IDENTITY_MISMATCH"
            assert "别人家的店" in polled["renew_error"]["message"]
            assert "没有保存" in polled["renew_error"]["message"]

            db = session_factory()
            try:
                assert channel_jars(db, ids["account"]) == jars_before, (
                    "对不上店铺时不得写入任何令牌（与续期前逐字节相同）"
                )
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_qianfan_renewal_accepts_first_time_topup(self, tmp_path: Path):
        """账号**没有绑定**时放行（第一次补千帆令牌）—— 点开的就是这张卡片。"""
        session_factory, headers, ids = self._env(tmp_path, "qf-renew-first")
        try:
            from backend.app.api.login_sessions import get_qianfan_login_adapter

            app.dependency_overrides[get_qianfan_login_adapter] = lambda: self._fake_adapter(
                {"shop_user_id": "SELLER-NEW", "shop_name": "新店"}
            )
            created = client.post(
                "/api/xhs/login-sessions/qianfan/qrcode",
                json={"target_account_id": ids["account"]},
                headers=headers,
            )
            polled = client.get(
                f"/api/xhs/login-sessions/{created.json()['session_id']}", headers=headers
            ).json()
            assert polled["renew_error"] is None, polled
            assert polled["renewed_channel"] == "ark"
            assert polled["renewed_shop"]["shop_name"] == "新店"
        finally:
            app.dependency_overrides.clear()

    def test_qianfan_qrcode_without_target_binds_a_new_account(self, tmp_path: Path):
        """千帆扫码**无 target = 建号**（2026-09-23，设计 13 §18.15.3）。

        ⚠️ **前提被推翻，断言改写（原文保留）**：旧契约「不传 target_account_id
        直接 422（千帆只能续期，建号需要小红书用户身份）」—— 用户口径「新增千帆
        平台登录」后，扫码与邮箱密码都能直接建号（店铺名兜底，不伪造
        `external_user_id`）。原断言 `status_code == 422` 保留在 docstring 里。
        """
        session_factory, headers, ids = self._env(tmp_path, "qf-no-target")
        try:
            from backend.app.api.login_sessions import get_qianfan_login_adapter

            app.dependency_overrides[get_qianfan_login_adapter] = lambda: self._fake_adapter(
                {"shop_user_id": "SELLER-NEW", "shop_name": "观隅好物的店"}
            )
            created = client.post(
                "/api/xhs/login-sessions/qianfan/qrcode",
                json={},
                headers=headers,
            )
            assert created.status_code == 200, created.text
            polled = client.get(
                f"/api/xhs/login-sessions/{created.json()['session_id']}", headers=headers
            ).json()
            assert polled["renew_error"] is None, polled
            # 建号：账号行 sub_type=qianfan，昵称取店铺名，ark 信封落库
            account = polled["account"]
            assert account is not None and account["sub_type"] == "qianfan"
            assert account["nickname"] == "观隅好物的店"

            db = session_factory()
            try:
                jars = channel_jars(db, account["id"])
                assert set(jars) == {"ark"}, f"只该写 ark 平台，实际：{set(jars)}"
                assert "access-token-ark.xiaohongshu.com" in jars["ark"]
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_qianfan_qrcode_rejects_foreign_account(self, tmp_path: Path):
        """目标账号不属于当前用户 ⇒ 二维码都不生成（别让用户扫完才被告知）。"""
        session_factory = _override_database(tmp_path)
        try:
            token = _register("qf-foreign")
            db = session_factory()
            try:
                other = User(username="someone-else", password_hash="x")
                db.add(other)
                db.flush()
                account = _seed_account(
                    db, user_id=other.id, nickname="别人的号", external_user_id="qf-other"
                )
                foreign_id = account.id
            finally:
                db.close()
            response = client.post(
                "/api/xhs/login-sessions/qianfan/qrcode",
                json={"target_account_id": foreign_id},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 404, response.text
        finally:
            app.dependency_overrides.clear()


#: 真机抓包 + 服务端实证（2026-09-23）拿到的 ark jar 键名集合。
#: 邮箱密码链路与扫码链路的落点相同：`access-token-ark.xiaohongshu.com` 等。
EMAIL_LOGIN_ARK_JAR = {
    "a1": "email-a1",
    "customer-sso-sid": "email-sso",
    "access-token-ark.xiaohongshu.com": "email-ark-token",
    "x-user-id-ark.xiaohongshu.com": "email-ark-uid",
    "beaker.session.id": "email-beaker",
}

#: 测试用邮箱（**不是**真实凭据；真机调研用的账号不进测试）。
TEST_EMAIL = "tester@example.com"


class TestQianfanEmailLogin:
    """千帆邮箱密码登录（2026-09-23，设计 13 §18.15.3；真机抓包 + 服务端实证）。

    实证结论（本机复现，每一步 200）：
    emailPwd → ticket/route → 导航换票 → ssologin → seller/login ⇒
    `access-token-ark.xiaohongshu.com` 等 ark 域令牌 + 只读店铺校验通过。
    §18.14.3「服务端复现 ssologin 不可交付」的边界被推翻（原句保留在该节）。
    """

    def _fake_adapter(self, *, shop: dict | None = None, fail: str = ""):
        class _Qianfan:
            def login_by_email_pwd(self, email, pwd):
                if fail:
                    raise RuntimeError(fail)
                return {"status": "confirmed", "cookies": dict(EMAIL_LOGIN_ARK_JAR)}

            def get_shop_info(self, cookies):
                if shop is None:
                    raise RuntimeError("千帆登录态校验失败")
                return shop

        return _Qianfan()

    def _env(self, tmp_path: Path, username: str):
        session_factory = _override_database(tmp_path)
        token = _register(username)
        db = session_factory()
        try:
            user = db.scalar(select(User).where(User.username == username))
            user_id = user.id
            account = _seed_account(db, user_id=user_id, nickname="观隅好物", external_user_id="qf-1")
            ids = {"account": account.id, "user": user_id}
        finally:
            db.close()
        return session_factory, {"Authorization": f"Bearer {token}"}, ids

    def test_email_login_binds_a_new_qianfan_account(self, tmp_path: Path):
        """无 target = 建号：`sub_type=qianfan` + ark 信封；邮箱密码不进任何落库结构。"""
        session_factory, headers, _ids = self._env(tmp_path, "qf-email-bind")
        try:
            from backend.app.api.login_sessions import get_qianfan_login_adapter

            app.dependency_overrides[get_qianfan_login_adapter] = lambda: self._fake_adapter(
                shop={"shop_user_id": "SELLER-1", "shop_name": "观隅好物的店"}
            )
            response = client.post(
                "/api/xhs/login-sessions/qianfan/email-login",
                json={"email": TEST_EMAIL, "pwd": "not-a-real-password"},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["status"] == "confirmed"
            assert payload["session_id"] is None, "同步端点不建登录会话行"
            account = payload["account"]
            assert account["sub_type"] == "qianfan"
            assert account["nickname"] == "观隅好物的店"
            assert account["action"] == "created"
            assert payload["renewed_shop"]["shop_name"] == "观隅好物的店"

            db = session_factory()
            try:
                jars = channel_jars(db, account["id"])
                assert set(jars) == {"ark"}, f"只该写 ark 平台，实际：{set(jars)}"
                assert "access-token-ark.xiaohongshu.com" in jars["ark"]
                # 邮箱与密码不得落库：会话行零条，信封里不出现凭据文本
                assert db.scalar(select(func.count()).select_from(LoginSession)) == 0
                row = db.scalar(select(AccountCookieVersion))
                assert TEST_EMAIL not in row.encrypted_cookies
                assert "not-a-real-password" not in row.encrypted_cookies
                # 账号行同样不存邮箱 / 密码
                stored = db.get(PlatformAccount, account["id"])
                assert TEST_EMAIL not in (stored.profile_json or "")
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_email_login_renews_the_target_account(self, tmp_path: Path):
        """带 target = 续期：写进这一个账号的 ark 通道，店铺名随响应回传。"""
        session_factory, headers, ids = self._env(tmp_path, "qf-email-renew")
        try:
            from backend.app.api.login_sessions import get_qianfan_login_adapter

            app.dependency_overrides[get_qianfan_login_adapter] = lambda: self._fake_adapter(
                shop={"shop_user_id": "SELLER-1", "shop_name": "观隅好物的店"}
            )
            response = client.post(
                "/api/xhs/login-sessions/qianfan/email-login",
                json={
                    "email": TEST_EMAIL,
                    "pwd": "not-a-real-password",
                    "target_account_id": ids["account"],
                },
                headers=headers,
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["renewed_channel"] == "ark"
            assert payload["account"]["id"] == ids["account"]

            db = session_factory()
            try:
                jars = channel_jars(db, ids["account"])
                assert set(jars) == {"ark"}, f"只该写 ark 平台，实际：{set(jars)}"
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_email_login_refuses_a_different_shop(self, tmp_path: Path):
        """账号已绑 A 店、登录的是 B 店 ⇒ 拒绝 + 一个字节都不写（§18.8 ③A 同型）。"""
        session_factory, headers, ids = self._env(tmp_path, "qf-email-mismatch")
        db = session_factory()
        try:
            account = db.get(PlatformAccount, ids["account"])
            bind_shop_account(
                db, account, cookies_text=ARK_JAR, shop_name="我的店", seller_id="SELLER-1"
            )
            db.commit()
            jars_before = channel_jars(db, ids["account"])
        finally:
            db.close()
        try:
            from backend.app.api.login_sessions import get_qianfan_login_adapter

            app.dependency_overrides[get_qianfan_login_adapter] = lambda: self._fake_adapter(
                shop={"shop_user_id": "SELLER-OTHER", "shop_name": "别人家的店"}
            )
            response = client.post(
                "/api/xhs/login-sessions/qianfan/email-login",
                json={
                    "email": TEST_EMAIL,
                    "pwd": "not-a-real-password",
                    "target_account_id": ids["account"],
                },
                headers=headers,
            )
            assert response.status_code == 400, response.text
            detail = response.json()["detail"]
            assert detail["code"] == "RENEW_IDENTITY_MISMATCH"
            assert "没有保存" in detail["message"]

            db = session_factory()
            try:
                assert channel_jars(db, ids["account"]) == jars_before, (
                    "对不上店铺时不得写入任何令牌（与续期前逐字节相同）"
                )
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_email_login_rejects_foreign_target(self, tmp_path: Path):
        """目标账号属于别人 ⇒ 登录链都不跑（与扫码同一条闸）。"""
        session_factory = _override_database(tmp_path)
        try:
            token = _register("qf-email-foreign")
            db = session_factory()
            try:
                other = User(username="someone-else", password_hash="x")
                db.add(other)
                db.flush()
                account = _seed_account(
                    db, user_id=other.id, nickname="别人的号", external_user_id="qf-other"
                )
                foreign_id = account.id
            finally:
                db.close()
            calls: list[tuple[str, str]] = []

            class _Spy:
                def login_by_email_pwd(self, email, pwd):
                    calls.append((email, pwd))
                    return {"status": "confirmed", "cookies": dict(EMAIL_LOGIN_ARK_JAR)}

                def get_shop_info(self, cookies):
                    return {"shop_user_id": "SELLER-1", "shop_name": "店"}

            from backend.app.api.login_sessions import get_qianfan_login_adapter

            app.dependency_overrides[get_qianfan_login_adapter] = _Spy
            response = client.post(
                "/api/xhs/login-sessions/qianfan/email-login",
                json={
                    "email": TEST_EMAIL,
                    "pwd": "not-a-real-password",
                    "target_account_id": foreign_id,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 404, response.text
            assert calls == [], "异主目标必须在发登录请求前拦掉，适配器一次都不该被调"
        finally:
            app.dependency_overrides.clear()

    def test_email_login_failure_returns_platform_reason(self, tmp_path: Path):
        """适配器失败（密码错 / 平台拒绝）⇒ 400 + 平台原话，不写任何库。"""
        session_factory, headers, _ids = self._env(tmp_path, "qf-email-fail")
        try:
            from backend.app.api.login_sessions import get_qianfan_login_adapter

            app.dependency_overrides[get_qianfan_login_adapter] = lambda: self._fake_adapter(
                fail="邮箱或密码错误"
            )
            response = client.post(
                "/api/xhs/login-sessions/qianfan/email-login",
                json={"email": TEST_EMAIL, "pwd": "wrong"},
                headers=headers,
            )
            assert response.status_code == 400, response.text
            assert "邮箱或密码错误" in response.json()["detail"]

            db = session_factory()
            try:
                assert db.scalar(select(func.count()).select_from(PlatformAccount)) == 1, (
                    "登录失败不得建号（env 里那一个种子账号之外没有新行）"
                )
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_email_login_rejects_unknown_fields(self, tmp_path: Path):
        """未知字段 422（`extra="forbid"`）：防「新旧不匹配被静默丢弃」（§33.10 同型）。"""
        session_factory = _override_database(tmp_path)
        try:
            token = _register("qf-email-extra")
            response = client.post(
                "/api/xhs/login-sessions/qianfan/email-login",
                json={"email": TEST_EMAIL, "pwd": "x", "sync_creator": True},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 422, response.text
        finally:
            app.dependency_overrides.clear()


class TestPhoneLoginRenew:
    """手机验证码登录的续期语义（2026-09-23，设计 13 §18.16）。

    账号信息卡的补登弹窗与绑定账号弹窗对齐后（§18.16），网页版 / 创作者平台的
    补登多了「手机验证码」这一档 —— 后端随之给四个手机登录端点加上
    `target_account_id`：

    * 发码即校验归属（异主 / 软删 404），target 落进临时态（confirm 兜底用）；
    * confirm 带 target（或缺省从临时态兜底）⇒ 只写这一个账号的对应通道，
      身份不符 ⇒ 400 + 结构化原因，**一个字节都不写**（同扫码续期口径）；
    * 不带 target ⇒ 老的建号行为完全不变。
    """

    PHONE = "13800138000"
    CODE = "123456"

    class _PcPhoneAdapter:
        """PC 手机登录桩：产物固定为 www jar；身份可参数化（构造「登成了另一个号」）。"""

        def __init__(self, external_user_id: str = "5bdc-target", nickname: str = "观隅好物") -> None:
            self.external_user_id = external_user_id
            self.nickname = nickname

        def create_phone_session(self, phone):
            return {"cookies": {"a1": "temp-a1", "webId": "w"}, "message": "sent"}

        def confirm_phone_login(self, phone, code, cookies):
            return {
                "status": "confirmed",
                "cookies": {"a1": "p-a1", "web_session": "ws-new", "id_token": "it-new"},
            }

        def get_user_info(self, cookies):
            return {
                "external_user_id": self.external_user_id,
                "nickname": self.nickname,
                "avatar_url": "",
            }

    class _CreatorPhoneAdapter:
        """创作者手机登录桩：产物固定为 creator jar（creator 域强键）。"""

        def __init__(self, external_user_id: str = "creator-target", nickname: str = "观隅好物") -> None:
            self.external_user_id = external_user_id
            self.nickname = nickname

        def create_phone_session(self, phone):
            return {"cookies": {"a1": "temp-c-a1"}, "message": "sent"}

        def confirm_phone_login(self, phone, code, cookies):
            return {
                "status": "confirmed",
                "cookies": {
                    "a1": "p-c-a1",
                    "customer-sso-sid": "sso-new",
                    "access-token-creator.xiaohongshu.com": "c-token-new",
                },
            }

        def get_user_info(self, cookies):
            return {
                "external_user_id": self.external_user_id,
                "nickname": self.nickname,
                "avatar_url": "",
            }

    def _env(
        self,
        tmp_path: Path,
        username: str,
        *,
        sub_type: str = "pc",
        external_user_id: str = "5bdc-target",
    ):
        """一个种子账号 + www / ark 两平台旧令牌（用来验证「另一个平台不动」）。"""
        session_factory = _override_database(tmp_path)
        token = _register(username)
        db = session_factory()
        try:
            user = db.scalar(select(User).where(User.username == username))
            user_id = user.id
            account = _seed_account(
                db,
                user_id=user_id,
                nickname="观隅好物",
                external_user_id=external_user_id,
                sub_type=sub_type,
            )
            _add_cookies(db, account, "a1=old-www; web_session=ws-old; id_token=it-old", CHANNEL_WWW)
            _add_cookies(db, account, ARK_JAR, CHANNEL_ARK)
            ids = {"account": account.id, "user": user_id}
        finally:
            db.close()
        return session_factory, {"Authorization": f"Bearer {token}"}, ids

    def _send(self, headers: dict, kind: str, *, target: int | None = None) -> dict:
        body = {"phone": self.PHONE}
        if target is not None:
            body["target_account_id"] = target
        return client.post(
            f"/api/xhs/login-sessions/{kind}/phone/send-code",
            json=body,
            headers=headers,
        ).json()

    def _confirm(self, headers: dict, kind: str, session_id: int, *, target: int | None = None):
        body = {"session_id": session_id, "phone": self.PHONE, "code": self.CODE}
        if target is not None:
            body["target_account_id"] = target
        return client.post(
            f"/api/xhs/login-sessions/{kind}/phone/confirm",
            json=body,
            headers=headers,
        )

    def _jars_by_channel(self, db: Any, account_id: int) -> dict:
        from backend.app.services.account_service import parse_channel_envelope

        rows = db.scalars(
            select(AccountCookieVersion)
            .where(AccountCookieVersion.platform_account_id == account_id)
            .order_by(AccountCookieVersion.id)
        ).all()
        by_channel: dict[str, list[str]] = {}
        for row in rows:
            for platform, jar in parse_channel_envelope(decrypt_text(row.encrypted_cookies)).items():
                by_channel.setdefault(platform, []).append(jar)
        return by_channel

    def test_pc_phone_renew_writes_only_www_and_leaves_ark_untouched(self, tmp_path: Path):
        """PC 手机验证码补登：只给 www 追加一版，ark 一个字节都不动，不建新账号行。"""
        session_factory, headers, ids = self._env(tmp_path, "phone-renew-pc")
        try:
            from backend.app.api.login_sessions import get_pc_login_adapter

            app.dependency_overrides[get_pc_login_adapter] = lambda: self._PcPhoneAdapter()
            created = client.post(
                "/api/xhs/login-sessions/pc/phone/send-code",
                json={"phone": self.PHONE, "target_account_id": ids["account"]},
                headers=headers,
            )
            assert created.status_code == 200, created.text

            # 发码即把 target 落进临时态（confirm 缺省时兜底用）
            db = session_factory()
            try:
                row = db.get(LoginSession, created.json()["session_id"])
                temp = json.loads(decrypt_text(row.encrypted_temp_cookies))
                assert temp.get("target_account_id") == ids["account"]
            finally:
                db.close()

            confirmed = self._confirm(headers, "pc", created.json()["session_id"], target=ids["account"])
            assert confirmed.status_code == 200, confirmed.text
            payload = confirmed.json()
            assert payload["renew_error"] is None, payload
            assert payload["renewed_channel"] == CHANNEL_WWW
            assert payload["account"]["id"] == ids["account"]
            assert payload["account"]["action"] == "renewed"

            db = session_factory()
            try:
                jars = self._jars_by_channel(db, ids["account"])
                assert len(jars[CHANNEL_WWW]) == 2, "www 应该多了一版"
                assert jars[CHANNEL_ARK] == [ARK_JAR], "ark 一个字节都没动"
                assert db.scalar(select(func.count()).select_from(PlatformAccount)) == 1, (
                    "续期不建新账号行"
                )
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_creator_phone_renew_writes_creator_channel(self, tmp_path: Path):
        """创作者手机验证码补登：写 creator 通道，ark 仍然不动。"""
        session_factory, headers, ids = self._env(
            tmp_path, "phone-renew-creator", sub_type="creator", external_user_id="creator-target"
        )
        try:
            from backend.app.api.login_sessions import get_creator_login_adapter

            app.dependency_overrides[get_creator_login_adapter] = lambda: self._CreatorPhoneAdapter()
            created = self._send(headers, "creator", target=ids["account"])
            assert "session_id" in created, created
            confirmed = self._confirm(headers, "creator", created["session_id"], target=ids["account"])
            assert confirmed.status_code == 200, confirmed.text
            payload = confirmed.json()
            assert payload["renewed_channel"] == CHANNEL_CREATOR
            assert payload["account"]["action"] == "renewed"

            db = session_factory()
            try:
                jars = self._jars_by_channel(db, ids["account"])
                assert len(jars[CHANNEL_CREATOR]) == 1
                assert "access-token-creator.xiaohongshu.com" in jars[CHANNEL_CREATOR][0]
                assert jars[CHANNEL_ARK] == [ARK_JAR], "ark 仍然不动"
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_phone_renew_refuses_identity_mismatch_and_writes_nothing(self, tmp_path: Path):
        """登成了另一个号 ⇒ 400 + RENEW_IDENTITY_MISMATCH，一个字节都不写。"""
        session_factory, headers, ids = self._env(tmp_path, "phone-renew-mismatch")
        try:
            from backend.app.api.login_sessions import get_pc_login_adapter

            app.dependency_overrides[get_pc_login_adapter] = lambda: self._PcPhoneAdapter(
                external_user_id="someone-else", nickname="别的号"
            )
            created = self._send(headers, "pc", target=ids["account"])
            confirmed = self._confirm(headers, "pc", created["session_id"], target=ids["account"])
            assert confirmed.status_code == 400, confirmed.text
            detail = confirmed.json()["detail"]
            assert detail["code"] == "RENEW_IDENTITY_MISMATCH"
            assert "没有保存任何令牌" in detail["message"]

            db = session_factory()
            try:
                jars = self._jars_by_channel(db, ids["account"])
                assert jars[CHANNEL_WWW] == ["a1=old-www; web_session=ws-old; id_token=it-old"]
                assert jars[CHANNEL_ARK] == [ARK_JAR]
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_phone_send_code_rejects_foreign_or_deleted_target(self, tmp_path: Path):
        """发码即拦异主 / 软删目标（§18.16）：不让用户等一条短信才被告知账号不对。"""
        session_factory, headers, ids = self._env(tmp_path, "phone-renew-foreign")
        try:
            from backend.app.api.login_sessions import get_pc_login_adapter

            app.dependency_overrides[get_pc_login_adapter] = lambda: self._PcPhoneAdapter()
            # 异主：另一个用户名下的账号 id
            _register("phone-renew-other")
            other_db = session_factory()
            try:
                other_user = other_db.scalar(select(User).where(User.username == "phone-renew-other"))
                other_user_id = other_user.id
                foreign = _seed_account(
                    other_db, user_id=other_user_id, nickname="别人的号", external_user_id="other-1"
                )
                foreign_id = foreign.id
            finally:
                other_db.close()
            response = client.post(
                "/api/xhs/login-sessions/pc/phone/send-code",
                json={"phone": self.PHONE, "target_account_id": foreign_id},
                headers=headers,
            )
            assert response.status_code == 404, response.text

            # 软删自己的账号同样 404（DEF-055 口径）
            db = session_factory()
            try:
                from backend.app.core.time import shanghai_now

                account = db.get(PlatformAccount, ids["account"])
                account.deleted_at = shanghai_now()
                db.commit()
            finally:
                db.close()
            response = client.post(
                "/api/xhs/login-sessions/pc/phone/send-code",
                json={"phone": self.PHONE, "target_account_id": ids["account"]},
                headers=headers,
            )
            assert response.status_code == 404, response.text
        finally:
            app.dependency_overrides.clear()

    def test_phone_renew_rejects_target_deleted_after_send(self, tmp_path: Path):
        """confirm 时的兜底闸：发码后账号被软删 ⇒ 400 + RENEW_TARGET_UNAVAILABLE，不写库。"""
        session_factory, headers, ids = self._env(tmp_path, "phone-renew-gone")
        try:
            from backend.app.api.login_sessions import get_pc_login_adapter

            app.dependency_overrides[get_pc_login_adapter] = lambda: self._PcPhoneAdapter()
            created = self._send(headers, "pc", target=ids["account"])
            db = session_factory()
            try:
                from backend.app.core.time import shanghai_now

                account = db.get(PlatformAccount, ids["account"])
                account.deleted_at = shanghai_now()
                db.commit()
            finally:
                db.close()
            confirmed = self._confirm(headers, "pc", created["session_id"], target=ids["account"])
            assert confirmed.status_code == 400, confirmed.text
            detail = confirmed.json()["detail"]
            assert detail["code"] == "RENEW_TARGET_UNAVAILABLE"

            db = session_factory()
            try:
                jars = self._jars_by_channel(db, ids["account"])
                assert jars[CHANNEL_WWW] == ["a1=old-www; web_session=ws-old; id_token=it-old"]
                assert jars[CHANNEL_ARK] == [ARK_JAR]
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_phone_confirm_without_target_behaves_exactly_as_before(self, tmp_path: Path):
        """不带 target ⇒ 老的建号行为完全不变（新账号行 + created，种子账号不动）。"""
        session_factory, headers, ids = self._env(tmp_path, "phone-renew-none")
        try:
            from backend.app.api.login_sessions import get_pc_login_adapter

            app.dependency_overrides[get_pc_login_adapter] = lambda: self._PcPhoneAdapter(
                external_user_id="new-phone-user", nickname="手机号新账号"
            )
            created = self._send(headers, "pc")
            confirmed = self._confirm(headers, "pc", created["session_id"])
            assert confirmed.status_code == 200, confirmed.text
            payload = confirmed.json()
            assert payload["renewed_channel"] == ""
            assert payload["renew_error"] is None
            assert payload["account"]["action"] == "created"
            assert payload["account"]["nickname"] == "手机号新账号"

            db = session_factory()
            try:
                assert db.scalar(select(func.count()).select_from(PlatformAccount)) == 2, (
                    "建号路径应该多一行"
                )
                jars = self._jars_by_channel(db, ids["account"])
                assert jars[CHANNEL_WWW] == ["a1=old-www; web_session=ws-old; id_token=it-old"], (
                    "种子账号的旧令牌不该被建号路径动过"
                )
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_target_in_temp_state_is_used_when_confirm_omits_it(self, tmp_path: Path):
        """confirm 缺省 target_account_id 时，用发码时落进的临时态兜底（同 sync_creator 形态）。"""
        session_factory, headers, ids = self._env(tmp_path, "phone-renew-fallback")
        try:
            from backend.app.api.login_sessions import get_pc_login_adapter

            app.dependency_overrides[get_pc_login_adapter] = lambda: self._PcPhoneAdapter()
            created = self._send(headers, "pc", target=ids["account"])
            # confirm **不带** target_account_id
            confirmed = self._confirm(headers, "pc", created["session_id"])
            assert confirmed.status_code == 200, confirmed.text
            payload = confirmed.json()
            assert payload["renewed_channel"] == CHANNEL_WWW
            assert payload["account"]["id"] == ids["account"]
            assert payload["account"]["action"] == "renewed"

            db = session_factory()
            try:
                assert db.scalar(select(func.count()).select_from(PlatformAccount)) == 1, (
                    "兜底生效才不会建新行"
                )
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()


class TestLoginCredentialsNeverPersisted:
    """登录凭据不落库（2026-09-23 用户口径）。

    > 「登录时手机号和邮箱密码等均不落库，数据库只存登录成功后的账号信息和会话临牌」

    邮箱 / 密码 / 手机号 / 短信验证码都只有「换令牌」一个用途。本类把四个入口
    （手机发码 / 手机确认 / 邮箱密码 / 结构层）各钉一条，任何一条开始落凭据就红。
    """

    #: 测试专用号码（**不是**真实凭据；真机调研账号不进测试）。
    PHONE = "13800138000"
    CODE = "123456"

    def test_phone_send_code_stores_neither_phone_nor_code(self, tmp_path: Path):
        """发码：库里只剩设备 Cookie；手机号（连掩码）与验证码都不落。"""
        session_factory = _override_database(tmp_path)
        token = _register("cred-phone-send")
        try:
            from backend.app.api.login_sessions import get_pc_login_adapter

            seen: list[str] = []

            class _FakePc:
                def create_phone_session(self, phone):
                    # 适配器**确实收到了**完整手机号（要发给平台发短信）——
                    # 本用例断言的是它不能出现在任何落盘结构里。
                    seen.append(phone)
                    return {"cookies": {"a1": "temp-a1", "webId": "w"}, "message": "sent"}

            app.dependency_overrides[get_pc_login_adapter] = _FakePc
            created = client.post(
                "/api/xhs/login-sessions/pc/phone/send-code",
                json={"phone": self.PHONE},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert created.status_code == 200, created.text
            assert seen == [self.PHONE], "打桩没接到手机号，用例失去意义"

            db = session_factory()
            try:
                row = db.get(LoginSession, created.json()["session_id"])
                assert row is not None
                # ① phone_mask 列已移除（模型侧同步）
                assert not hasattr(LoginSession, "phone_mask")
                # ② 解密临时态后也没有手机号 / 验证码
                temp = json.loads(decrypt_text(row.encrypted_temp_cookies))
                blob = json.dumps(temp, ensure_ascii=False)
                assert self.PHONE not in blob
                assert self.CODE not in blob
                # ③ 全表再扫一遍（防「换一列藏」）
                for other in db.scalars(select(LoginSession)).all():
                    text = " ".join(
                        str(x)
                        for x in (other.qr_id, other.code, other.qr_url, other.encrypted_temp_cookies)
                    )
                    assert self.PHONE not in text
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_phone_confirm_stores_no_phone_or_code(self, tmp_path: Path):
        """确认登录：验证码只透传；成功后只留账号行 + 会话令牌信封。"""
        session_factory = _override_database(tmp_path)
        token = _register("cred-phone-confirm")
        # ⚠️ 闭包捕获而不是 `self.PHONE`：嵌套类方法里的 self 是**适配器实例**，
        # 没有 PHONE 属性（第一版就栽在这：AttributeError 被端点 except 吞成 400）。
        expected_phone, expected_code = self.PHONE, self.CODE
        try:
            from backend.app.api.login_sessions import get_pc_login_adapter

            class _FakePc:
                def create_phone_session(self, phone):
                    return {"cookies": {"a1": "temp-a1"}, "message": "sent"}

                def confirm_phone_login(self, phone, code, cookies):
                    assert phone == expected_phone and code == expected_code
                    return {
                        "status": "confirmed",
                        "cookies": {"a1": "final-a1", "web_session": "s", "id_token": "t"},
                    }

                def get_user_info(self, cookies):
                    return {
                        "external_user_id": "cred-phone-user",
                        "nickname": "凭据账号",
                        "avatar_url": "",
                    }

            app.dependency_overrides[get_pc_login_adapter] = _FakePc
            created = client.post(
                "/api/xhs/login-sessions/pc/phone/send-code",
                json={"phone": self.PHONE},
                headers={"Authorization": f"Bearer {token}"},
            ).json()
            confirmed = client.post(
                "/api/xhs/login-sessions/pc/phone/confirm",
                json={"session_id": created["session_id"], "phone": self.PHONE, "code": self.CODE},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["account"]["nickname"] == "凭据账号"

            db = session_factory()
            try:
                # 该有的一定有：账号行 + 会话信封
                account = db.scalar(select(PlatformAccount))
                assert account is not None and account.nickname == "凭据账号"
                version = db.scalar(select(AccountCookieVersion))
                assert version is not None
                # 不该有的一定没有：手机号 / 验证码不出现在任何文本列
                blob = " ".join(
                    str(x)
                    for row in db.scalars(select(LoginSession)).all()
                    for x in (row.qr_id, row.code, row.qr_url, row.encrypted_temp_cookies)
                )
                blob += account.profile_json or ""
                assert self.PHONE not in blob, "手机号落盘了"
                assert self.CODE not in blob, "短信验证码落盘了"
            finally:
                db.close()
        finally:
            app.dependency_overrides.clear()

    def test_model_and_migration_agree_phone_mask_is_gone(self):
        """结构守卫：模型无 `phone_mask`，移除它的迁移在场（只改代码 = 假修复）。"""
        assert not hasattr(LoginSession, "phone_mask")
        migration = (
            ROOT / "backend" / "alembic" / "versions" / "x7y8z9a0b1c2_drop_login_sessions_phone_mask.py"
        )
        assert migration.exists(), "移除 phone_mask 列的迁移必须在场"

    def test_debug_dump_redacts_credentials(self):
        """脱敏函数本体：密码 / 验证码 / 手机号 / 邮箱一律 `[REDACTED]`。"""
        from apis.xhs_creator_login_apis import XHSCreatorLoginApi

        body = json.dumps(
            {"service": "s", "email": "a@b.c", "pwd": "s3cret!", "source": "", "type": "emailPwd"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        redacted = XHSCreatorLoginApi._redact_login_body(body)
        assert "s3cret!" not in redacted, "密码不能进 debug 日志"
        assert "a@b.c" not in redacted, "邮箱不能进 debug 日志"
        assert "[REDACTED]" in redacted
        # 非 JSON 输入原样返回（脱敏器自己不能抛）
        assert XHSCreatorLoginApi._redact_login_body("not-json") == "not-json"
"unknown"
