"""一个账号、三个平台的令牌（设计 12 §11.5 / §11.6；2026-09-21 拆分，用户拍板）。

要钉死的契约：
1. **平台按 Cookie 形态判定**，不靠调用方传（分错的后果是指向完全错误的错误信息）；
2. **千帆与创作者平台不再是同一份令牌**（真机实测：创作者 jar 打千帆一律
   `-13000 缺失登录凭证`）—— 这条是本文件存在的**主要**理由；
3. **同一账号下三个平台的令牌互不覆盖**，各取自己平台的最新一版；
4. **一个字段存信封 JSON**（`{"v":1,"channels":{<平台>:<jar>}}`），没有 channel 列；
5. **一次导入可以拿两份 Cookie**，落库后仍是**一个账号**（不再一个身份两行）；
6. **能力按平台上报**：有 creator / ark 平台的令牌就能走创作者链路，与 `sub_type` 无关；
7. **取数按目标平台选令牌**：千帆要 ark 平台的 jar，创作者平台要 creator 平台的 jar。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.app.core.database import Base, get_db  # noqa: E402
from backend.app.core.security import decrypt_text, encrypt_text  # noqa: E402
from backend.app.core.time import shanghai_now  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.models import (  # noqa: E402
    AccountCookieVersion,
    PlatformAccount,
    User,
)
from backend.app.services import account_service  # noqa: E402
from backend.app.services.account_service import (  # noqa: E402
    CHANNEL_ARK,
    CHANNEL_CREATOR,
    CHANNEL_WWW,
    channel_for_cookies,
    dump_channel_envelope,
    latest_cookies_for_channel,
    parse_channel_envelope,
    store_channel_cookies,
)

import pytest


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration
client = TestClient(app)

#: 四种真实形态的 jar（键名与真机一致，值为占位）。
WWW_JAR = {"a1": "www-a1", "web_session": "ws-1", "id_token": "it-1"}
#: 千帆 jar = 真机绑定表那份的**超集**形态（同时带 creator 域与 ark 域令牌）。
ARK_JAR = {
    "a1": "ark-a1",
    "customer-sso-sid": "sso-1",
    "access-token-ark.xiaohongshu.com": "ark-1",
    "access-token-creator.xiaohongshu.com": "creator-1",
    "galaxy.creator.beaker.session.id": "gcb-1",
}
#: 创作者扫码登录的 jar（真机 acct 8 形态）：**只有 creator 域令牌**。
#: 令牌值刻意与 ark jar 不同（`CREATOR-ONLY`），好让回归用例能分辨「拿到的是哪一份」。
CREATOR_JAR = {
    "a1": "c-a1",
    "customer-sso-sid": "sso-1",
    "galaxy_creator_session_id": "gcs-1",
    "access-token-creator.xiaohongshu.com": "CREATOR-ONLY",
    "x-user-id-creator.xiaohongshu.com": "uid-1",
}
#: 只带 customer 单点登录弱键、没有任何平台域令牌的 jar。
WEAK_SSO_JAR = {"a1": "w-a1", "customer-sso-sid": "sso-1", "galaxy_creator_session_id": "gcs-1"}


def _cookie_text(jar: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def _write_row(db: Any, account_id: int, channels: dict[str, str]) -> None:
    """直接落一行信封（测试替身：等价于一次登录写入）。"""
    db.add(
        AccountCookieVersion(
            platform_account_id=account_id,
            encrypted_cookies=encrypt_text(dump_channel_envelope(channels)),
        )
    )


def _row_channels(db: Any) -> list[set[str]]:
    """每行信封里包了哪些平台（断言「互不覆盖」用）。"""
    return [
        set(parse_channel_envelope(decrypt_text(row.encrypted_cookies)).keys())
        for row in db.scalars(select(AccountCookieVersion)).all()
    ]


def _override_database(tmp_path: Path) -> Any:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'channel-test.db'}", connect_args={"check_same_thread": False}
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


class TestPlatformClassification:
    """平台判定：只看 Cookie 形态，ark 优先。"""

    def test_www_jar_is_www(self):
        assert channel_for_cookies(_cookie_text(WWW_JAR)) == CHANNEL_WWW

    def test_ark_jar_is_ark(self):
        assert channel_for_cookies(_cookie_text(ARK_JAR)) == CHANNEL_ARK

    def test_creator_jar_is_creator(self):
        """创作者 jar **不再是** ark（2026-09-21 拆分）：它没有 ark 域令牌。

        真机实测（观隅好物 acct 8）：这份 jar 打千帆一律 `-13000 缺失登录凭证`，
        归到 ark 就等于告诉系统「它能打千帆」—— 一句假话。
        """
        assert channel_for_cookies(_cookie_text(CREATOR_JAR)) == CHANNEL_CREATOR

    def test_weak_sso_only_jar_is_creator_not_www(self):
        """只有单点登录弱键 ⇒ 显然不是 www（没有 web_session），归 creator。"""
        assert channel_for_cookies(_cookie_text(WEAK_SSO_JAR)) == CHANNEL_CREATOR

    def test_both_present_prefers_ark(self):
        """同时带两套平台域令牌时按 ark 归位：那份是超集，www 那半边在 ark 接口上用不上。"""
        both = {**CREATOR_JAR, **ARK_JAR}
        assert channel_for_cookies(_cookie_text(both)) == CHANNEL_ARK

    def test_empty_falls_back_to_www(self):
        assert channel_for_cookies("") == CHANNEL_WWW

    def test_json_shape_jar_is_classified_too(self):
        """DEF-059 不回归：JSON 形态的 jar 也要能判（登录路径落的都是 JSON）。"""
        import json

        assert channel_for_cookies(json.dumps(CREATOR_JAR)) == CHANNEL_CREATOR
        assert channel_for_cookies(json.dumps(ARK_JAR)) == CHANNEL_ARK
        assert channel_for_cookies(json.dumps(WWW_JAR)) == CHANNEL_WWW


class TestThreePlatformsOnOneAccount:
    """同一个账号下三个平台的令牌并存、互不覆盖（信封一行一个/多个平台）。"""

    def _account(self, tmp_path: Path) -> tuple[Any, PlatformAccount]:
        session_factory = _override_database(tmp_path)
        db = session_factory()
        user = User(username="owner", password_hash="x")
        db.add(user)
        db.flush()
        account = PlatformAccount(
            user_id=user.id, platform="xhs", sub_type="pc", nickname="观隅好物", status="active"
        )
        db.add(account)
        db.commit()
        return db, account

    def test_platforms_do_not_overwrite_each_other(self, tmp_path: Path):
        db, account = self._account(tmp_path)
        try:
            store_channel_cookies(db=db, account=account, cookies_text=_cookie_text(WWW_JAR))
            store_channel_cookies(db=db, account=account, cookies_text=_cookie_text(ARK_JAR))
            store_channel_cookies(db=db, account=account, cookies_text=_cookie_text(CREATOR_JAR))
            db.commit()

            assert latest_cookies_for_channel(db, account.id, CHANNEL_WWW) == _cookie_text(WWW_JAR)
            assert latest_cookies_for_channel(db, account.id, CHANNEL_ARK) == _cookie_text(ARK_JAR)
            assert latest_cookies_for_channel(db, account.id, CHANNEL_CREATOR) == _cookie_text(CREATOR_JAR)
            # 同一个账号、三行 Cookie 版本、三个平台各一行 —— 不是三个账号。
            rows = db.scalars(select(AccountCookieVersion)).all()
            assert len(rows) == 3
            assert db.scalar(select(PlatformAccount.id).where(PlatformAccount.id == account.id)) == account.id
        finally:
            db.close()
            app.dependency_overrides.clear()

    def test_creator_login_does_not_touch_other_platforms(self, tmp_path: Path):
        """**观隅好物回归**：创作者扫码只更新 creator 平台，www / ark 一个字节都不动。

        拆分前创作者 jar 落在 ark 通道上，把账号原有的千帆令牌**顶掉**——
        千帆探针随后拿到一份打不开千帆的 jar，界面显示「千帆已失效」，
        而令牌其实好好的（用户 2026-09-21 实测）。
        """
        db, account = self._account(tmp_path)
        try:
            store_channel_cookies(db=db, account=account, cookies_text=_cookie_text(WWW_JAR))
            store_channel_cookies(db=db, account=account, cookies_text=_cookie_text(ARK_JAR))
            # 用户在创作者平台扫了一次码
            store_channel_cookies(db=db, account=account, cookies_text=_cookie_text(CREATOR_JAR))
            db.commit()

            assert latest_cookies_for_channel(db, account.id, CHANNEL_ARK) == _cookie_text(ARK_JAR), (
                "创作者扫码不得动千帆的令牌"
            )
            assert latest_cookies_for_channel(db, account.id, CHANNEL_WWW) == _cookie_text(WWW_JAR)
            assert latest_cookies_for_channel(db, account.id, CHANNEL_CREATOR) == _cookie_text(CREATOR_JAR)
        finally:
            db.close()
            app.dependency_overrides.clear()

    def test_latest_wins_within_a_platform(self, tmp_path: Path):
        db, account = self._account(tmp_path)
        try:
            store_channel_cookies(db=db, account=account,
                                  cookies_text=_cookie_text({**WWW_JAR, "web_session": "ws-old"}))
            store_channel_cookies(db=db, account=account,
                                  cookies_text=_cookie_text({**WWW_JAR, "web_session": "ws-new"}))
            db.commit()
            assert "ws-new" in latest_cookies_for_channel(db, account.id, CHANNEL_WWW)
        finally:
            db.close()
            app.dependency_overrides.clear()

    def test_missing_platform_returns_empty(self, tmp_path: Path):
        db, account = self._account(tmp_path)
        try:
            store_channel_cookies(db=db, account=account, cookies_text=_cookie_text(WWW_JAR))
            db.commit()
            assert latest_cookies_for_channel(db, account.id, CHANNEL_ARK) == ""
            assert latest_cookies_for_channel(db, account.id, CHANNEL_CREATOR) == ""
        finally:
            db.close()
            app.dependency_overrides.clear()

    def test_one_row_can_carry_two_platforms(self, tmp_path: Path):
        """一次写入涉及两个平台（如导入两份 Cookie）时，一行信封装得下。"""
        db, account = self._account(tmp_path)
        try:
            _write_row(db, account.id, {CHANNEL_WWW: _cookie_text(WWW_JAR),
                                        CHANNEL_ARK: _cookie_text(ARK_JAR)})
            db.commit()
            assert latest_cookies_for_channel(db, account.id, CHANNEL_WWW) == _cookie_text(WWW_JAR)
            assert latest_cookies_for_channel(db, account.id, CHANNEL_ARK) == _cookie_text(ARK_JAR)
            assert _row_channels(db) == [{CHANNEL_WWW, CHANNEL_ARK}]
        finally:
            db.close()
            app.dependency_overrides.clear()

    def test_merge_across_rows_newest_wins_per_platform(self, tmp_path: Path):
        """跨行合并：**每个平台各取最新**，不是「最新一行说了算」。"""
        db, account = self._account(tmp_path)
        try:
            _write_row(db, account.id, {CHANNEL_WWW: "ws-old", CHANNEL_ARK: "ark-old"})
            db.commit()
            _write_row(db, account.id, {CHANNEL_WWW: "ws-new"})
            db.commit()
            jars = {CHANNEL_WWW: latest_cookies_for_channel(db, account.id, CHANNEL_WWW),
                    CHANNEL_ARK: latest_cookies_for_channel(db, account.id, CHANNEL_ARK)}
            assert "ws-new" in jars[CHANNEL_WWW]
            assert "ark-old" in jars[CHANNEL_ARK], "新一行没提 ark，旧行的 ark 必须还在"
        finally:
            db.close()
            app.dependency_overrides.clear()

    def test_stored_form_is_one_json_field(self, tmp_path: Path):
        """落库形态 = 一个字段的 JSON 信封；没有 channel 列（用户 2026-09-21 口径）。"""
        db, account = self._account(tmp_path)
        try:
            store_channel_cookies(db=db, account=account, cookies_text=_cookie_text(CREATOR_JAR))
            db.commit()
            row = db.scalars(select(AccountCookieVersion)).one()
            columns = {
                column["name"]
                for column in db.execute(text("PRAGMA table_info(account_cookie_versions)")).mappings()
            }
            assert "channel" not in columns, "channel 列已去掉，平台信息在 JSON 里"
            envelope = parse_channel_envelope(decrypt_text(row.encrypted_cookies))
            assert set(envelope) == {CHANNEL_CREATOR}
            assert "customer-sso-sid" in envelope[CHANNEL_CREATOR]
            # 密文里不出现明文键名
            assert "customer-sso-sid" not in row.encrypted_cookies
        finally:
            db.close()
            app.dependency_overrides.clear()


class TestImportTwoTokensKeepsOneAccount:
    """一次导入两份 Cookie ⇒ 库里只有一个账号。"""

    def test_second_cookie_lands_on_the_same_account(self, tmp_path: Path):
        session_factory = _override_database(tmp_path)
        db = session_factory()
        try:
            _register("dual-token-owner")
            user = db.scalar(select(User).where(User.username == "dual-token-owner"))

            account, action = account_service.upsert_platform_account_from_login(
                db=db,
                user_id=user.id,
                platform="xhs",
                sub_type="pc",
                user_info={"nickname": "观隅好物", "external_user_id": "5bdc4dddd7773d0001d028f2"},
                cookies_text=_cookie_text(WWW_JAR),
            )
            assert action == "created"
            # 第二次：只追加一个平台，不新建账号、不改资料。
            account_service.store_channel_cookies(
                db=db, account=account, cookies_text=_cookie_text(ARK_JAR)
            )
            db.commit()

            rows = db.scalars(select(PlatformAccount)).all()
            assert len(rows) == 1, "同一身份不允许留下两行账号"
            assert latest_cookies_for_channel(db, account.id, CHANNEL_ARK) == _cookie_text(ARK_JAR)
        finally:
            db.close()
            app.dependency_overrides.clear()

    def test_capabilities_report_platforms_and_route_by_capability(self, tmp_path: Path):
        """有 ark 平台的令牌 ⇒ `notes_link=creator`，哪怕 sub_type 是 pc（能力驱动，不按类型猜）。"""
        from backend.app.api.platforms.xhs import pc as pc_module

        session_factory = _override_database(tmp_path)
        db = session_factory()
        try:
            token = _register("cap-owner")
            user = db.scalar(select(User).where(User.username == "cap-owner"))
            account = PlatformAccount(
                user_id=user.id, platform="xhs", sub_type="pc", nickname="观隅好物", status="active"
            )
            db.add(account)
            db.flush()
            _write_row(db, account.id, {CHANNEL_ARK: _cookie_text(ARK_JAR)})
            db.commit()

            result = pc_module._self_content_capabilities(db, account, "")
            assert result["channels"] == {"www": False, "creator": True, "ark": True}
            assert result["notes_link"] == "creator"
            assert result["capabilities"]["notes"]["available"] is True
        finally:
            db.close()
            app.dependency_overrides.clear()


class TestQianfanTakesOnlyTheAccountsOwnArkChannel:
    """千帆取票：**只认账号自己的 `ark` 通道**，没有任何兜底（2026-09-22 口径）。

    ⚠️ **被取代的旧契约（原句保留）**：本节原为
    *「千帆取票：账号 ark 平台优先，`shop_account_bindings` 只兜底（§11.6 选 C）」*，
    并有两条用例钉住兜底（`test_ark_platform_wins_over_binding` /
    `test_falls_back_to_binding_when_no_ark_platform`）。

    用户 2026-09-22 拍板 **「每个账号只使用自己账号对应的平台有效登录令牌」**，
    兜底整条被删（连同 `binding_for_account` / `qianfan_cookies_for_binding`）。
    所以本节改为钉住**否命题**：取不到就如实返回空串，让调用方给结构化原因 ——
    **绝不向绑定表 / 其它账号借令牌**。
    """

    def _env(self, tmp_path: Path):
        """一个账号：`seller_id` 已填（**归属**），但**没有** `ark` 令牌（**能力**）。

        ⚠️ ADR-021（2026-09-30）起 `shops` 表与 `shop_id_fk` 列都已退场 ——
        原实现建的 `Shop` 行（`Shop(user_id=…, seller_id="SELLER-1", shop_name=…)`）
        与 `account.shop_id_fk = shop.id` 两行**无处可写**。店铺标识下沉成账号自己的
        `seller_id`，而「绑没绑店」这个状态本身已不存在（判据只剩令牌）。
        本节的否命题（**取不到票就返回空、绝不借**）与此无关，故原样保留。
        """
        session_factory = _override_database(tmp_path)
        db = session_factory()
        user = User(username="shop-owner", password_hash="x")
        db.add(user)
        db.flush()
        account = PlatformAccount(
            user_id=user.id, platform="xhs", sub_type="pc", nickname="观隅好物",
            status="active", seller_id="SELLER-1",
        )
        db.add(account)
        db.commit()
        return db, account

    def test_own_ark_channel_is_used(self, tmp_path: Path):
        from backend.app.services.shop_binding_service import qianfan_cookies_for_account

        db, account = self._env(tmp_path)
        try:
            store_channel_cookies(db=db, account=account, cookies_text=_cookie_text(ARK_JAR))
            db.commit()
            cookies = qianfan_cookies_for_account(db, account.id)
            assert cookies == _cookie_text(ARK_JAR), "账号自己的 ark 通道就是取票来源"
        finally:
            db.close()
            app.dependency_overrides.clear()

    def test_no_ark_channel_returns_empty_and_never_borrows(self, tmp_path: Path):
        """绑了店 ≠ 有票：没有 ark 通道 ⇒ 空串（**不向店铺/其它账号借**）。"""
        from backend.app.services.shop_binding_service import qianfan_cookies_for_account

        db, account = self._env(tmp_path)
        try:
            # 另一个账号有 ark 令牌 → 也不得被借用
            other = PlatformAccount(
                user_id=account.user_id, platform="xhs", sub_type="creator", nickname="别的", status="active"
            )
            db.add(other)
            db.flush()
            store_channel_cookies(db=db, account=other, cookies_text=_cookie_text(ARK_JAR))
            db.commit()

            assert qianfan_cookies_for_account(db, account.id) == "", (
                "没有自有 ark 令牌就必须如实返回空 —— 借令牌会让「扫错店」静默成立"
            )
            assert qianfan_cookies_for_account(db, other.id), "前置条件：另一个账号确实有票"
            # ADR-021：归属（`seller_id`）与能力（`ark` 令牌）是两件事 ——
            # 有归属不等于有票（旧实现用 `shop.id == account.shop_id_fk` 表达这条）。
            assert account.seller_id == "SELLER-1"
            assert qianfan_cookies_for_account(db, account.id) == ""
        finally:
            db.close()
            app.dependency_overrides.clear()

    def test_creator_jar_is_not_used_for_qianfan(self, tmp_path: Path):
        """**观隅好物回归（核心）**：creator 通道的 jar 不得被千帆取票拿到。

        拆分前创作者扫码的 jar 落在 ark 通道上，千帆取票「有行就用」地把它拿走 ——
        平台回 `-13000 缺失登录凭证`，界面说「千帆已失效」。现在 creator jar 落在
        creator 通道，千帆取票自然拿不到（也**没有兜底**可以把它捞回来）。
        """
        from backend.app.services.shop_binding_service import qianfan_cookies_for_account

        db, account = self._env(tmp_path)
        try:
            store_channel_cookies(db=db, account=account, cookies_text=_cookie_text(CREATOR_JAR))
            db.commit()

            cookies = qianfan_cookies_for_account(db, account.id)
            assert "CREATOR-ONLY" not in cookies, "千帆取票不得拿到创作者通道那份 jar"
            assert cookies == "", "创作者 jar 落 creator 通道 ⇒ 千帆这里就是没有票"
        finally:
            db.close()
            app.dependency_overrides.clear()


class TestSoftDeletedAccountsAreUnusable:
    """DEF-055：**删除数据不应再使用，包括软删的数据**（用户 2026-09-21 给出的口径）。

    账号删除是软删（台账 §4.9 / I3）：行留在原位、唯一索引因此永久成立、重新导入时**复活**它。
    代价是**每一处读路径都必须自己记得过滤 `deleted_at`** —— 漏一处就等于「删了还能用」，
    而且静默。本轮实测就漏了四处（归属校验的 3 份拷贝、绑定解析、店铺计数、
    凭据候选），所以这里把「绑定解析」钉住
    （端点层与计数层在 `test_shop_nm.py`；凭据侧的守卫见 `test_api.py`）。
    """

    def _env(self, tmp_path: Path):
        session_factory = _override_database(tmp_path)
        db = session_factory()
        user = User(username="softdel-owner", password_hash="x")
        db.add(user)
        db.flush()
        # 目标账号：creator，自己没有 www 会话
        target = PlatformAccount(
            user_id=user.id, platform="xhs", sub_type="creator",
            nickname="观隅好物", external_user_id="5bdc4dddd7773d0001d028f2", status="active",
        )
        # 另一个持有 www 会话的账号（软删后它的 jar 不得再被任何路径使用）
        helper = PlatformAccount(
            user_id=user.id, platform="xhs", sub_type="pc",
            nickname="备用号", external_user_id="69737c750000000021025f17", status="active",
        )
        db.add_all([target, helper])
        db.flush()
        _write_row(db, helper.id, {CHANNEL_WWW: _cookie_text(WWW_JAR)})
        db.commit()
        return db, user, target, helper

    def test_ark_session_for_soft_deleted_account_is_none(self, tmp_path: Path):
        """账号软删后，它不得再被判定为「有可用千帆会话」（否则凭据仍会被拿去调千帆）。

        ⚠️ 原用例名 `test_binding_for_soft_deleted_account_is_none`（绑定解析）
        已被取代：`shop_account_bindings` 整表移除，同一道闸曾迁到
        `shop_for_account` / `account_shop_id` 上；**2026-09-30 ADR-021 又进一步** ——
        店铺实体退场，闸门本身收敛为「本账号 ark 令牌可用否」，即 `has_ark_session`。
        语义不变：软删 ⇒ 不再解析出凭证。
        """
        from backend.app.services.shop_binding_service import has_ark_session

        db, _user, target, _helper = self._env(tmp_path)
        try:
            # 归属列（seller_id）与能力判据（ark 令牌）分写，正是 ADR-021 的口径。
            target.seller_id = "SELLER-1"
            _write_row(db, target.id, {CHANNEL_ARK: "a1=x; access-token-ark.xiaohongshu.com=y"})
            db.commit()
            assert has_ark_session(db, target.id), "前置条件：软删前应有可用 ark 会话"

            target.deleted_at = shanghai_now()
            db.commit()
            assert not has_ark_session(db, target.id)
        finally:
            db.close()
            app.dependency_overrides.clear()
