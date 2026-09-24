"""千帆（ark）登录态巡检的后端契约测试（T-31）。

关注点（对应设计文档 §24.3）：

1. **只读**：巡检只调 `get_seller_info`，**不碰**同步/商品列表等任何其他接口；
2. **失效可判别**：`-13000` → 账号的千帆状态标 `invalid`（`shop_state`）+ 可读原因 + 一次铃铛通知；
3. **不毁数据**：标 `invalid` **不清空**已同步的 `shop_note_goods`；
4. **低频有界**：按 `health_checked_at` 判到期，未到期直接跳过（手动触发也不刷请求）；
5. **通知只发一次**：状态翻转时通知，后续轮次只刷新时间与原因（否则 24 小时 4 条同样的铃铛）；
6. **鉴权**：越权 404、未绑定 404（没有凭据就没有可巡检的对象）。
"""
from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.api.platforms.xhs.qianfan import get_qianfan_api_adapter_factory
from backend.app.core.database import Base, get_db
from backend.app.core.time import shanghai_now
from backend.app.main import app
from backend.app.models import Notification, PlatformAccount, User
from backend.app.models.shop import ShopNoteGoods
from backend.app.services.shop_binding_service import get_shop_state, set_shop_state
from backend.app.services.shop_goods_service import existing_rows, store_entry
from backend.app.services.shop_health_service import (
    account_health_due,
    run_shop_health_checks,
)

client = TestClient(app)


class FakeArkAdapter:
    """Canned 千帆响应；把**每一次调用**都记下来（用来断言「只读、且只探查活」）。"""

    mode = "ok"
    calls: dict = {}

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def get_seller_info(self):
        FakeArkAdapter.calls["seller"] = FakeArkAdapter.calls.get("seller", 0) + 1
        if FakeArkAdapter.mode == "expired":
            return False, "缺失登录凭证, 请重新登录", {"code": -13000, "success": False}
        return True, "ok", {
            "code": 0,
            "success": True,
            "data": {"user_id": "SELLER-1", "seller_name": "观隅好物的店"},
        }

    def get_goods_note_page(self, page_num: int = 1, page_size: int = 20):
        FakeArkAdapter.calls["goods_page"] = FakeArkAdapter.calls.get("goods_page", 0) + 1
        return True, "ok", {
            "code": 0,
            "success": True,
            "data": {
                "note_infos": [
                    {
                        "note_id": "NOTE-1",
                        "note_title": "挂了商品的笔记",
                        "item_id": "ITEM-1",
                        "item_title": "外出野餐垫",
                        "item_image": "http://qimg.example/ITEM-1.jpg",
                        "variant_desc": "米白/2m",
                    }
                ],
                "total": 1,
                "end_tag": True,
            },
        }

    def search_items(self, **kwargs):
        # 巡检**不应该**走到这里：出现即视为越界（哪怕只是查价）。
        FakeArkAdapter.calls["search"] = FakeArkAdapter.calls.get("search", 0) + 1
        return True, "ok", {"code": 0, "success": True, "data": {"total": 0, "items": []}}


def _setup(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'shop-health.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    FakeArkAdapter.mode = "ok"
    FakeArkAdapter.calls = {}

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_qianfan_api_adapter_factory] = lambda: FakeArkAdapter

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
        owned = PlatformAccount(
            user_id=owner_id, platform="xhs", sub_type="creator", nickname="观隅好物", status="active"
        )
        foreign = PlatformAccount(
            user_id=intruder_id, platform="xhs", sub_type="pc", nickname="别人的号", status="active"
        )
        db.add_all([owned, foreign])
        db.commit()
        ids = {"owned": owned.id, "foreign": foreign.id}
    finally:
        db.close()
    return owner_headers, intruder_headers, ids, SessionLocal


def _teardown():
    app.dependency_overrides.clear()


def _bind(headers, account_id: int) -> None:
    response = client.post(
        "/api/xhs/qianfan/bind",
        json={
            "platform_account_id": account_id,
            "cookie_string": "a1=x; access-token-ark.xiaohongshu.com=y",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text


#: 播种用的挂链映射条目（与 FakeArkAdapter.get_goods_note_page 的夹具同形）。
_GOODS_NOTE_ENTRY = {
    "note_id": "NOTE-1",
    "note_title": "挂了商品的笔记",
    "item_id": "ITEM-1",
    "item_title": "外出野餐垫",
    "item_image": "http://qimg.example/ITEM-1.jpg",
    "variant_desc": "米白/2m",
}


def _sync(session_local, account_id: int) -> None:
    """播种一条挂链映射（**直写存储层**）。

    v1.32 起 `POST /xhs/qianfan/sync-goods` 已移除（设计 13 §30.2）⇒ 测试
    不再经 HTTP 播种：`store_entry` 是按需补齐 / 笔记同步共用的唯一存储实现，
    直调它语义不变，还顺带守住「端点真的没了」。
    """
    db = session_local()
    try:
        account = db.get(PlatformAccount, account_id)
        rows = existing_rows(db, account_id)
        store_entry(db, account.user_id, account_id, rows, _GOODS_NOTE_ENTRY)
        set_shop_state(
            account, goods_note_total=1, goods_synced_at=shanghai_now().isoformat()
        )
        db.commit()
    finally:
        db.close()


def _account(db, account_id: int) -> PlatformAccount:
    """巡检对象 2026-09-22 从「绑定行」换成「账号行」。

    `shop_account_bindings` 整表移除后，千帆侧状态住在
    `platform_accounts.shop_state`（读写只经 `get_shop_state` / `set_shop_state`）。
    """
    return db.get(PlatformAccount, account_id)


def _state(db, account_id: int) -> dict:
    return get_shop_state(_account(db, account_id))


def test_health_check_marks_invalid_and_keeps_synced_mapping(tmp_path):
    """失效 → `invalid` + 可读原因 + 一次通知；**已同步映射必须原样保留**（不毁数据）。"""
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        _sync(SessionLocal, ids["owned"])
        FakeArkAdapter.calls = {}
        FakeArkAdapter.mode = "expired"

        response = client.post(
            "/api/xhs/qianfan/health-check",
            json={"platform_account_id": ids["owned"]},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["status"] == "invalid"
        assert "登录凭证" in payload["status_message"], "必须带平台原文，而不是空原因"
        assert payload["health_checked_at"], "巡检时间必须回写"
        assert payload["flipped_to_invalid"] is True

        db = SessionLocal()
        try:
            assert _state(db, ids["owned"])["status"] == "invalid"
            # 映射一个都没少 —— 失效不等于数据作废（T-31 既有结论）。
            assert len(db.scalars(select(ShopNoteGoods.id)).all()) == 1
            notices = db.scalars(select(Notification)).all()
            assert len(notices) == 1
            assert notices[0].title == "千帆登录态失效"
            assert notices[0].level == "warning"
            #: ⚠️ **被取代的旧断言（原句保留）**：原来断言
            #: `"重新绑定千帆" in notices[0].body`。失效要补的是**令牌**，
            #: 不是重新绑店 —— 2026-09-22 关系（`shop_id_fk`）与凭据（`ark`
            #: 通道）已经拆开，所以「重新绑定」是句**指错方向**的建议。
            assert "重新登录千帆并导入令牌" in notices[0].body
        finally:
            db.close()
    finally:
        _teardown()


def test_health_check_is_read_only(tmp_path):
    """巡检只允许打 `seller/info/v2`：不得出现同步、商品列表或任何其他调用。"""
    headers, _, ids, _ = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        FakeArkAdapter.calls = {}

        client.post(
            "/api/xhs/qianfan/health-check",
            json={"platform_account_id": ids["owned"]},
            headers=headers,
        )
        assert FakeArkAdapter.calls == {"seller": 1}, f"越界调用：{FakeArkAdapter.calls}"
    finally:
        _teardown()


def test_health_check_keeps_active_shop_state(tmp_path):
    """有效 → 仍 `active`，`status_message` 清空，并记下巡检时间。"""
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        db = SessionLocal()
        try:
            set_shop_state(
                _account(db, ids["owned"]), status="invalid", status_message="上一次的旧原因"
            )
            db.commit()
        finally:
            db.close()

        payload = client.post(
            "/api/xhs/qianfan/health-check",
            json={"platform_account_id": ids["owned"]},
            headers=headers,
        ).json()
        assert payload["status"] == "active"
        assert payload["status_message"] == ""
        assert payload["health_checked_at"]
        assert payload["flipped_to_invalid"] is False
    finally:
        _teardown()


def test_scheduled_check_skips_accounts_checked_recently(tmp_path):
    """低频有界：周期未到就跳过（`checked=0`，且平台一次都没被调）。"""
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        db = SessionLocal()
        try:
            now = shanghai_now()
            # DEF-066：巡检按行主收口（user_id 必传）
            owner_id = db.scalar(select(User.id).where(User.username == "owner"))
            assert run_shop_health_checks(db, now=now, user_id=owner_id)["checked"] == 1
            FakeArkAdapter.calls = {}
            # 紧接着再跑一轮：还没到 6 小时 → 必须整轮跳过。
            second = run_shop_health_checks(db, now=now + timedelta(hours=1), user_id=owner_id)
            assert second["checked"] == 0 and second["skipped"] == 1
            assert FakeArkAdapter.calls == {}, "未到期的账号不得产生平台调用"
            # 超过周期 → 到期。
            assert account_health_due(_account(db, ids["owned"]), now + timedelta(hours=7))
        finally:
            db.close()
    finally:
        _teardown()


def test_scheduled_check_notifies_only_on_transition(tmp_path):
    """失效只通知一次：第二个周期仍失效时只刷新时间，不再写第二条铃铛。"""
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        FakeArkAdapter.mode = "expired"
        db = SessionLocal()
        try:
            now = shanghai_now()
            # DEF-066：巡检按行主收口（user_id 必传）
            owner_id = db.scalar(select(User.id).where(User.username == "owner"))
            first = run_shop_health_checks(db, now=now, user_id=owner_id)
            assert first["checked"] == 1 and first["flipped_to_invalid"] == 1
            second = run_shop_health_checks(db, now=now + timedelta(hours=7), user_id=owner_id)
            assert second["checked"] == 1 and second["flipped_to_invalid"] == 0
            assert len(db.scalars(select(Notification)).all()) == 1, "不得重复刷铃铛"
        finally:
            db.close()
    finally:
        _teardown()


def test_health_check_requires_a_shop_and_ownership(tmp_path):
    """越权 404、未绑定 404 —— 与 bind/unbind 同一口径。"""
    headers, intruder_headers, ids, _ = _setup(tmp_path)
    try:
        unbound = client.post(
            "/api/xhs/qianfan/health-check",
            json={"platform_account_id": ids["owned"]},
            headers=headers,
        )
        assert unbound.status_code == 404
        assert "先绑定" in unbound.json()["detail"]

        _bind(headers, ids["owned"])
        stranger = client.post(
            "/api/xhs/qianfan/health-check",
            json={"platform_account_id": ids["owned"]},
            headers=intruder_headers,
        )
        assert stranger.status_code == 404
    finally:
        _teardown()
