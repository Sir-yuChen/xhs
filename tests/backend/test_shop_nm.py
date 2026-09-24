"""千帆「账号 ↔ 店铺」**1:1** 的后端契约测试（T-32；2026-09-22 口径大改）。

## ⚠️ 本文件的前身是 N:M 契约（原句保留、结论已被取代）

2026-09-22 之前，这个文件断言的是 **N:M**：

* *「多对多真的成立：两个账号绑同一店铺 → 一行店铺 + 两行绑定」*
  （`test_two_accounts_bind_one_shop_share_a_single_shop_row`）；
* *「同一账号绑两个店铺也允许（旧约束 `UNIQUE(platform_account_id)` 已解除）」*
  （`test_one_account_can_bind_two_shops`）；
* *「同一店铺的 `account_count` 必须能到 2」*（`test_account_count_ignores_bindings_from_another_tenant`）。

那套设计的理由是「**企业店主 + 多子账号**读同一个店」。**用户 2026-09-22 拍板否掉**：

> *不会再出现「同店铺、多账号」场景，一个店铺下只需存多个平台的会话令牌。*

⇒ 关系回到 **1:1**，并且**由 DB 强制**（部分唯一索引
`uq_platform_accounts_shop_id_fk`，见 `models/platform_account.py`）；关系从
`shop_account_bindings` 搬到 `platform_accounts.shop_id_fk`，那张表整表移除。
`shop_account_count` / `account_count` 因此恒 0/1，已从接口与页面一并删除。

## 现在盯什么

1. **1:1 真的成立**：第二个账号绑同一个店 → **409 `QIANFAN_SHOP_TAKEN`**（可读原因）；
2. **不能变成「随机挑一个店」**：账号只有一个店，重绑到另一个店 = **换店**
   （旧店与它的本地映射一起释放，不能串到新店的数据里）；
3. **解绑只解自己** + 孤儿店铺清理；
4. **软删闸不倒退**（DEF-055 的新形态）：软删账号既不占店、也不顶住别人绑店、
   更不得继续用按 id 的端点；
5. **别越界**：路由层不许自己 `select(...).where(platform_account_id == ...)` 挑店
   （1:1 之前那句是安全的，之后是「随机取一条」，且**不报错**）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.api.platforms.xhs.qianfan import get_qianfan_api_adapter_factory
from backend.app.core.database import Base, get_db
from backend.app.core.time import shanghai_now
from backend.app.main import app
from backend.app.models import PlatformAccount, User
from backend.app.models.shop import Shop, ShopNoteGoods
from backend.app.services.shop_binding_service import get_shop_state, set_shop_state
from backend.app.services.shop_goods_service import existing_rows, store_entry

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[2]

COOKIES = "a1=x; access-token-ark.xiaohongshu.com=y"


class FakeArkAdapter:
    """按 `seller_id` / `shop_name` 类属性返回不同店铺（用来验「换店」）。"""

    shop_name = "观隅好物的店"
    seller_id = "SELLER-1"
    calls: dict = {}

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def get_seller_info(self):
        FakeArkAdapter.calls["seller"] = FakeArkAdapter.calls.get("seller", 0) + 1
        return True, "ok", {
            "code": 0,
            "success": True,
            "data": {"user_id": FakeArkAdapter.seller_id, "seller_name": FakeArkAdapter.shop_name},
        }

    def get_goods_note_page(self, page_num: int = 1, page_size: int = 20, *, note_ids=None):
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
        return True, "ok", {"code": 0, "success": True, "data": {"total": 0, "items": []}}


def _setup(tmp_path, accounts: int = 2):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'shop-1to1.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    FakeArkAdapter.shop_name = "观隅好物的店"
    FakeArkAdapter.seller_id = "SELLER-1"
    FakeArkAdapter.calls = {}
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_qianfan_api_adapter_factory] = lambda: FakeArkAdapter

    response = client.post(
        "/api/auth/register", json={"username": "owner", "password": "secret123"}
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    db = SessionLocal()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "owner"))
        ids = []
        for index in range(accounts):
            account = PlatformAccount(
                user_id=user_id,
                platform="xhs",
                sub_type="creator",
                nickname=f"观隅好物{index + 1}",
                status="active",
            )
            db.add(account)
            ids.append(account)
        db.commit()
        account_ids = [account.id for account in ids]
    finally:
        db.close()
    return headers, account_ids, SessionLocal


def _teardown():
    app.dependency_overrides.clear()


def _bind(headers, account_id: int, *, expect: int = 200):
    response = client.post(
        "/api/xhs/qianfan/bind",
        json={"platform_account_id": account_id, "cookie_string": COOKIES},
        headers=headers,
    )
    assert response.status_code == expect, response.text
    return response.json()


#: 播种用的挂链映射条目（与 FakeArkAdapter.get_goods_note_page 的夹具同形）。
_GOODS_NOTE_ENTRY = {
    "note_id": "NOTE-1",
    "note_title": "挂了商品的笔记",
    "item_id": "ITEM-1",
    "item_title": "外出野餐垫",
    "item_image": "http://qimg.example/ITEM-1.jpg",
    "variant_desc": "米白/2m",
}


def _sync(session_local, account_id: int):
    """播种一条挂链映射（**直写存储层**）。

    v1.32 起 `POST /xhs/qianfan/sync-goods` 已移除（设计 13 §30.2）⇒ 测试
    不再经 HTTP 播种：`store_entry` 是按需补齐 / 笔记同步共用的唯一存储实现。
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


# --------------------------------------------------------------------------- #
# 1. 1:1 真的成立（**被 DB 强制**，不只是靠约定）
# --------------------------------------------------------------------------- #
def test_second_account_cannot_bind_the_same_shop(tmp_path):
    """一个店只能挂一个账号：第二个账号绑同一个店 → 409 + 可读原因。

    ⚠️ **被取代的旧断言（原句保留）**：原来这里（
    `test_two_accounts_bind_one_shop_share_a_single_shop_row`）断言的是
    *「同一店铺不该建出多行：len(shops) == 1」* + *「len(bindings) == 2」* +
    *「`second["shop_account_count"] == 2`」* —— 也就是 **N:1 的「多」这一侧有数**。
    """
    headers, account_ids, SessionLocal = _setup(tmp_path)
    try:
        first = _bind(headers, account_ids[0])
        body = _bind(headers, account_ids[1], expect=409)

        assert body["detail"]["code"] == "QIANFAN_SHOP_TAKEN"
        assert "一个店铺只能绑一个账号" in body["detail"]["message"]
        # 第二个账号**不该**被写进去
        assert first["shop_id_fk"] is not None

        db = SessionLocal()
        try:
            shops = db.scalars(select(Shop)).all()
            accounts = db.scalars(select(PlatformAccount).order_by(PlatformAccount.id)).all()
        finally:
            db.close()

        assert len(shops) == 1, f"同一店铺不该建出多行：{shops}"
        assert shops[0].seller_id == "SELLER-1"
        assert first["shop_id_fk"] == shops[0].id
        holders = [a.shop_id_fk for a in accounts if a.shop_id_fk is not None]
        assert holders == [shops[0].id], f"这个店只能被一个账号引用：{holders}"
        assert accounts[1].shop_id_fk is None, "被拒绝的账号必须保持未绑店"
    finally:
        _teardown()


def test_bind_is_idempotent_for_the_same_pair(tmp_path):
    """同一个账号重复绑同一个店 = 重验一次凭据，不建第二行店、不建第二份关系。"""
    headers, account_ids, SessionLocal = _setup(tmp_path)
    try:
        first = _bind(headers, account_ids[0])
        again = _bind(headers, account_ids[0])

        db = SessionLocal()
        try:
            assert len(db.scalars(select(Shop)).all()) == 1
        finally:
            db.close()
        assert again["bound"] is True
        assert again["shop_id_fk"] == first["shop_id_fk"]
    finally:
        _teardown()


def test_one_account_holds_exactly_one_shop(tmp_path):
    """账号**只有一个店**：重绑到另一个店 = 换店（旧店被释放，不是并存）。

    ⚠️ **被取代的旧断言（原句保留）**：原来这里（`test_one_account_can_bind_two_shops`）
    断言 *「旧的 `UNIQUE(platform_account_id)` 必须真的解除」* →
    *「len(shops) == 2」* + *「len(bindings) == 2」*。

    现在反过来：重绑到第二个店之后 **`shop_id_fk` 换成新的**，旧的**孤儿店行被删掉**，
    `len(shops) == 1`。这是 1:1 的直接推论，也是「`shop_id_fk` 是唯一关系」的证据。
    """
    headers, account_ids, SessionLocal = _setup(tmp_path, accounts=1)
    account_id = account_ids[0]
    try:
        first = _bind(headers, account_id)
        FakeArkAdapter.seller_id = "SELLER-2"
        FakeArkAdapter.shop_name = "第二个店"
        second = _bind(headers, account_id)

        assert first["shop_id_fk"] != second["shop_id_fk"]

        db = SessionLocal()
        try:
            shops = db.scalars(select(Shop).order_by(Shop.id)).all()
            account = db.get(PlatformAccount, account_id)
        finally:
            db.close()

        assert [shop.seller_id for shop in shops] == ["SELLER-2"], (
            f"旧店已成孤儿，该被清掉：{[(s.id, s.seller_id) for s in shops]}"
        )
        assert account.shop_id_fk == shops[0].id
    finally:
        _teardown()


def test_rebinding_to_another_shop_releases_the_old_mappings(tmp_path):
    """换店时旧店的本地映射必须清掉 —— 不区分店的映射会串到新店的数据里。

    `shop_note_goods` 是按**账号**挂的（不区分店），所以「换店不清映射」表现为
    「新店的商品列表里出现旧店的挂链笔记」，而页面上看不出错。
    """
    headers, account_ids, SessionLocal = _setup(tmp_path, accounts=1)
    account_id = account_ids[0]
    try:
        _bind(headers, account_id)
        _sync(SessionLocal, account_id)
        db = SessionLocal()
        try:
            assert len(db.scalars(select(ShopNoteGoods)).all()) == 1, "前置条件：先有一条映射"
            old_shop_pk = db.get(PlatformAccount, account_id).shop_id_fk
        finally:
            db.close()

        FakeArkAdapter.seller_id = "SELLER-2"
        FakeArkAdapter.shop_name = "第二个店"
        _bind(headers, account_id)

        db = SessionLocal()
        try:
            assert db.scalars(select(ShopNoteGoods)).all() == [], "换店必须清掉旧店的映射"
            assert db.get(Shop, old_shop_pk) is None, "旧店该被清掉"
        finally:
            db.close()
    finally:
        _teardown()


def test_routes_do_not_pick_a_shop_by_account_id_directly():
    """源码级护栏：路由层必须走 `shop_for_account()` / `account_shop_id()`。

    ⚠️ **被取代的旧断言（原句保留）**：原来断言的是
    *「路由层必须走 `binding_for_account()`」* —— 即
    `assert "binding_for_account" in pc_source`（函数名已随绑定表一起删除）。

    护栏的**形状**不变（1:1 之前 `select(...).where(platform_account_id == account.id)`
    是安全的、之后是随机取一条且不报错），只是现在禁的符号换成了
    `ShopAccountBinding`：那张表已不存在，任何人再写它就是**引用一个不存在的真相源**。
    """
    for relative in (
        "backend/app/api/platforms/xhs/pc.py",
        "backend/app/api/platforms/xhs/qianfan.py",
        "backend/app/api/platforms/xhs/product_notes.py",
        "backend/app/api/login_sessions.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "ShopAccountBinding" not in source, f"{relative} 仍在引用已删除的绑定表"
        assert "binding_for_account" not in source, f"{relative} 仍在引用已删除的取绑定函数"

    qianfan_source = (ROOT / "backend/app/api/platforms/xhs/qianfan.py").read_text(encoding="utf-8")
    pc_source = (ROOT / "backend/app/api/platforms/xhs/pc.py").read_text(encoding="utf-8")
    assert "shop_for_account" in qianfan_source
    assert "shop_for_account" in pc_source


# --------------------------------------------------------------------------- #
# 2. 解绑只解自己 / 孤儿店铺清理
# --------------------------------------------------------------------------- #
def test_unbind_clears_own_mapping_and_removes_the_orphan_shop(tmp_path):
    headers, account_ids, SessionLocal = _setup(tmp_path, accounts=1)
    account_id = account_ids[0]
    try:
        _bind(headers, account_id)
        _sync(SessionLocal, account_id)

        response = client.post(
            "/api/xhs/qianfan/unbind",
            json={"platform_account_id": account_id},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["removed"] == 1, "本账号的映射该被清掉"
        assert payload["shop_remaining_accounts"] == 0, "1:1 下解绑后没人再占这个店"

        db = SessionLocal()
        try:
            assert db.scalars(select(Shop)).all() == [], "没有账号挂着的店铺行该清掉"
            assert db.scalars(select(ShopNoteGoods)).all() == []
            account = db.get(PlatformAccount, account_id)
            assert account.shop_id_fk is None
            assert get_shop_state(account)["status"] == "", "解绑要把千帆状态一并复位"
        finally:
            db.close()
    finally:
        _teardown()


def test_unbind_is_a_noop_when_the_account_has_no_shop(tmp_path):
    headers, account_ids, SessionLocal = _setup(tmp_path, accounts=1)
    try:
        payload = client.post(
            "/api/xhs/qianfan/unbind",
            json={"platform_account_id": account_ids[0]},
            headers=headers,
        ).json()
        assert payload == {
            "platform_account_id": account_ids[0],
            "bound": False,
            "removed": 0,
        }
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 3. 店铺列表端点（1:1 的读侧）
# --------------------------------------------------------------------------- #
def test_shops_endpoint_marks_the_one_account_behind_a_shop(tmp_path):
    headers, account_ids, SessionLocal = _setup(tmp_path, accounts=1)
    try:
        _bind(headers, account_ids[0])
        _sync(SessionLocal, account_ids[0])

        response = client.post("/api/xhs/qianfan/shops", json={}, headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["count"] == 1
        shop = payload["shops"][0]
        assert shop["shop_name"] == "观隅好物的店"
        #: ⚠️ 原来断言 `shop["account_count"] == 2`（N:M 的「多」）；1:1 之后恒 1，
        #: 且 `account_count` 已从响应里删除（`accounts` 至多一项）。
        assert "account_count" not in shop, "已删除的计数不该回来"
        assert [row["platform_account_id"] for row in shop["accounts"]] == [account_ids[0]]
        assert shop["synced_notes"] == 1
        assert shop["goods_note_total"] == 1
        assert FakeArkAdapter.calls.get("seller", 0) == 1, "店铺列表必须是纯本地读（不重打千帆）"
    finally:
        _teardown()


def test_shops_endpoint_is_user_scoped(tmp_path):
    """别人绑的同一个千帆 seller 不是我的店铺。"""
    headers, account_ids, SessionLocal = _setup(tmp_path, accounts=1)
    try:
        _bind(headers, account_ids[0])
        response = client.post("/api/auth/register", json={"username": "other", "password": "secret123"})
        other_headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

        db = SessionLocal()
        try:
            other_user = db.scalar(select(User.id).where(User.username == "other"))
            account = PlatformAccount(
                user_id=other_user, platform="xhs", sub_type="creator", nickname="别人", status="active"
            )
            db.add(account)
            db.commit()
            other_account_id = account.id
        finally:
            db.close()

        # 另一个用户绑「同一个 seller_id」→ 应得到**他自己**的店铺行
        _bind(other_headers, other_account_id)
        db = SessionLocal()
        try:
            assert len(db.scalars(select(Shop)).all()) == 2
        finally:
            db.close()

        mine = client.post("/api/xhs/qianfan/shops", json={}, headers=headers).json()
        theirs = client.post("/api/xhs/qianfan/shops", json={}, headers=other_headers).json()
        assert mine["count"] == 1 and theirs["count"] == 1
        assert mine["shops"][0]["shop_id_fk"] != theirs["shops"][0]["shop_id_fk"]
        assert len(mine["shops"][0]["accounts"]) == 1
    finally:
        _teardown()


def test_shops_endpoint_filter_returns_only_that_accounts_shop(tmp_path):
    """`platform_account_id` 过滤只在**它自己的店**上有结果（未绑店 → 空列表）。"""
    headers, account_ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, account_ids[0])
        mine = client.post(
            "/api/xhs/qianfan/shops", json={"platform_account_id": account_ids[0]}, headers=headers
        ).json()
        assert mine["count"] == 1

        none = client.post(
            "/api/xhs/qianfan/shops", json={"platform_account_id": account_ids[1]}, headers=headers
        ).json()
        assert none == {"count": 0, "shops": []}
    finally:
        _teardown()


def test_shop_summary_reports_health_checked_at(tmp_path):
    """巡检时间也要在店铺摘要里（页面的「巡检 MM-DD HH:MM」依赖它）。

    ⚠️ 原用例名 `test_binding_summary_reports_health_checked_at`，把 `health_checked_at`
    写在**绑定行**上。现在它住在 `account.shop_state`，所以夹具改走 `set_shop_state`。
    """
    headers, account_ids, SessionLocal = _setup(tmp_path, accounts=1)
    try:
        _bind(headers, account_ids[0])
        db = SessionLocal()
        try:
            set_shop_state(
                db.get(PlatformAccount, account_ids[0]), health_checked_at=shanghai_now().isoformat()
            )
            db.commit()
        finally:
            db.close()

        payload = client.post(
            "/api/xhs/qianfan/binding",
            json={"platform_account_id": account_ids[0]},
            headers=headers,
        ).json()
        assert payload["health_checked_at"], "店铺摘要必须带巡检时间"
        assert payload["shop_id_fk"], "店铺摘要必须带店铺实体主键"
        assert "shop_account_count" not in payload, "已删除的计数不该回来"
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 4. 闸门：软删账号（DEF-055 的新形态）与跨租户残留引用（DEF-054）
# --------------------------------------------------------------------------- #
def test_soft_deleted_account_stops_holding_the_shop(tmp_path):
    """软删账号既不再占店，也**不得顶住**别人绑这个店。

    为什么必须成立：`UNIQUE(shop_id_fk)` 是加在整列上的（若不排除软删行），
    一条删掉的账号会**永久**占住这个店 —— 用户重新导入同一账号时
    `attach_shop` 直接撞 IntegrityError（表现为 500，而不是一句能自救的话）。
    所以唯一索引是**部分索引**：`WHERE shop_id_fk IS NOT NULL AND deleted_at IS NULL`。

    ⚠️ **被取代的旧断言（原句保留）**：原用例
    `test_soft_deleted_account_is_excluded_and_does_not_block_orphan_cleanup` 断言
    *「shop["account_count"] == 1，已软删账号的绑定不得再计入」* —— 计数字段已删除，
    同一件事现在直接用「能不能绑上」来证。
    """
    headers, account_ids, SessionLocal = _setup(tmp_path)
    first, second = account_ids
    try:
        _bind(headers, first)
        db = SessionLocal()
        try:
            db.get(PlatformAccount, first).deleted_at = shanghai_now()
            db.commit()
        finally:
            db.close()

        # 软删后：同一个店可以被另一个账号接管（不撞唯一索引）
        second_payload = _bind(headers, second)
        assert second_payload["bound"] is True

        db = SessionLocal()
        try:
            shops = db.scalars(select(Shop)).all()
            assert len(shops) == 1
            assert db.get(PlatformAccount, second).shop_id_fk == shops[0].id
        finally:
            db.close()
    finally:
        _teardown()


def test_foreign_tenant_residue_gives_a_readable_409_not_an_integrity_error(tmp_path):
    """**别的租户**的残留引用也必须是 409 + 人话，不能是 500。

    为什么必须单独测：归属闸（`account_for_shop`）带**租户闸**，而唯一索引是
    **全局**的 —— 一条别的租户留下的引用（真实库出现过：脚本直写把 `user_id=1` 的
    测试账号挂到 `user_id=2` 的店铺上）会被租户闸滤掉，于是本地检查放行、
    DB 约束拒绝 ⇒ 用户拿到的是 IntegrityError 原文。`any_account_for_shop`
    就是为这一步补的「全库范围占用检查」。

    ⚠️ **被取代的旧断言（原句保留）**：原用例
    `test_account_count_ignores_bindings_from_another_tenant` 断言 *「别的租户的绑定
    不得计入「N 个账号」；同租户的两个必须仍然算 2」* —— 计数字段与「同店多账号」
    一起被否决，现在钉的是「跨租户残留不当场炸成 500」。
    """
    headers, account_ids, SessionLocal = _setup(tmp_path, accounts=1)
    try:
        #: 先手工落一行**本租户的**店铺（不挂任何账号），再让**别的租户**的账号
        #: 引用它 —— 这正是真实库里那条残留的形状（「增量测试号」挂在观隅好物的店上）。
        #: ⚠️ 不能反过来（先让本租户账号绑上、再插残留）：部分唯一索引会当场拒绝，
        #: 那种「残留」在新模型下根本造不出来。
        db = SessionLocal()
        try:
            owner_id = db.scalar(select(User.id).where(User.username == "owner"))
            shop_pk = int(
                db.execute(
                    text(
                        "insert into shops (user_id, seller_id, shop_id, shop_name, status, "
                        "created_at, updated_at) values (:u, 'SELLER-1', '', '观隅好物的店', "
                        "'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"u": owner_id},
                ).lastrowid
            )
            foreign_user = User(username="foreign-tenant", password_hash="x")
            db.add(foreign_user)
            db.flush()
            db.add(
                PlatformAccount(
                    user_id=foreign_user.id,
                    platform="xhs",
                    sub_type="creator",
                    nickname="增量测试号",
                    external_user_id="ext-incr",
                    status="active",
                    shop_id_fk=shop_pk,
                )
            )
            second = PlatformAccount(
                user_id=owner_id,
                platform="xhs",
                sub_type="creator",
                nickname="第二个",
                status="active",
            )
            db.add(second)
            db.commit()
            second_id = second.id
        finally:
            db.close()

        body = _bind(headers, account_ids[0], expect=409)
        assert body["detail"]["code"] == "QIANFAN_SHOP_TAKEN"
        assert "另一个账号占用" in body["detail"]["message"]

        #: 本租户的那个账号一次都没被写进去（409 是「什么都没发生」）。
        db = SessionLocal()
        try:
            assert db.get(PlatformAccount, account_ids[0]).shop_id_fk is None
            assert db.get(PlatformAccount, second_id).shop_id_fk is None
        finally:
            db.close()
    finally:
        _teardown()


def test_soft_deleted_account_is_404_on_account_scoped_endpoints(tmp_path):
    """DEF-055：按 id 的端点必须一律 404 —— 「看不见」不等于「不能用」。

    账号删除是软删，用户手里可能还留着**旧页面上的 id**（另一个标签页、刚点开的下拉），
    所以每个按 id 取账号的端点都得过闸（与台账 §4.9 / I3 同源）。
    这里覆盖千帆侧 6 个入口，它们此前各自抄了一份**不带软删闸**的归属校验。
    """
    headers, account_ids, SessionLocal = _setup(tmp_path, accounts=1)
    try:
        _bind(headers, account_ids[0])
        db = SessionLocal()
        try:
            db.get(PlatformAccount, account_ids[0]).deleted_at = shanghai_now()
            db.commit()
        finally:
            db.close()

        cases = (
            ("/api/xhs/qianfan/binding", {"platform_account_id": account_ids[0]}),
            ("/api/xhs/qianfan/bind", {"platform_account_id": account_ids[0], "cookie_string": COOKIES}),
            ("/api/xhs/qianfan/health-check", {"platform_account_id": account_ids[0]}),
            ("/api/xhs/qianfan/shops", {"platform_account_id": account_ids[0]}),
            ("/api/xhs/qianfan/unbind", {"platform_account_id": account_ids[0]}),
            (
                "/api/xhs/qianfan/items",
                {"platform_account_id": account_ids[0], "page_no": 1, "page_size": 20},
            ),
        )
        for path, body in cases:
            response = client.post(path, json=body, headers=headers)
            assert response.status_code == 404, (
                f"{path} 未过软删闸：{response.status_code} {response.text[:200]}"
            )
    finally:
        _teardown()
