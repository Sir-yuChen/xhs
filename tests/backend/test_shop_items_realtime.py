"""商品域改实时查询（设计 75 §2.3 / §4.1 G6 / §2.4）—— 功能点测试。

`tests/backend/test_shop_items_realtime.py`（单文件、四段结构，见
`docs/04_测试文档/12_功能点测试规范.md`）：

① 鉴权与越权 —— 未登录 401 / 他人账号 404 / 无 ark 令牌 400。
② 主链路成功 —— `/items` 与 `/item-detail` 走**真实端点 → 服务层 → 假 adapter**，
   断言「响应逐字段与改造前同形」+「平台参数按契约映射」+「重复调用幂等」。
③ 关键失败面（≤3）—— 只能导致**静默错误**的那几种：
   平台失败变成 500 / 凭据失效说不清 / 商品 ID 搜到别的商品却照渲染。
④ 不变量 —— 已退场的东西**真的退场了**（410 墓碑 + 注册表摘除），以及
   「列表页不逐商品打平台」这条成本红线。

## 替身处在哪一层（重要）

替身是 **adapter**（`get_qianfan_api_adapter_factory` 依赖注入），**不是**服务层：
`realtime_platform_query_service` 的归一化、`_decorate_realtime_item` 的补齐、
`_find_platform_product` 的精确匹配全部**真跑**。替身若放在服务层，这三处的
缺陷一条都测不出来（这是「测实现而不是测行为」的典型）。

用例预算：20（本文件 15 条）
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory
from backend.app.models.platform_account import AccountCookieVersion, PlatformAccount
from backend.app.models.shop_product import ShopProduct
from backend.app.services.account_service import dump_channel_envelope
from backend.app.core.security import encrypt_text

#: 断言里用的固定商品 ID（形如千帆真实 ID，长度也对得上）。
ITEM_ID = "6adaf9dd238c6b00019c0dd7"
OTHER_ITEM_ID = "6ad0000000000000000000ff"


def _envelope(data: dict) -> dict:
    """SDK 的真实返回形状：**完整响应体**，业务数据在 `data` 下。

    ⚠️ 替身若不模拟这一层包裹，`_data_of` 的剥离逻辑就没被验证 ——
    而「直接读 `payload['items']` 恒为空且不报错」正是本仓踩过的静默缺陷。
    """
    return {"code": 0, "success": True, "msg": "成功", "data": data}


def _raw_product(**overrides) -> dict:
    """千帆 `search_item_v2` 的一条商品原文（字段按真机响应裁剪，够覆盖契约即可）。"""
    row = {
        "item_id": ITEM_ID,
        "item_name": "美式乡村实木抽屉柜",
        "article_no": "ART-001",
        "min_price": 230400,
        "max_price": 260000,
        "total_stock": 42,
        "sku_count": 3,
        "on_sale_sku_count": 2,
        "sale_qty_info": {"acc_sale_qty": 17, "sale_qty30": 5},
        "product_note_num": 4,
        "product_note_ids": ["n1", "n2"],
        "images": [
            {"link": "https://img/1.jpg", "width": 800, "height": 800},
            {"link": "https://img/2.jpg", "width": 800, "height": 800},
        ],
        "image_descriptions": [{"link": "https://img/d1.jpg"}],
        "categories": [
            {"name": "柜类", "level": 2},
            {"name": "住宅家具", "level": 1},
            {"name": "斗柜", "level": 3},
        ],
        "on_shelf_time": 1790784000000,
        "create_time": 1790700000000,
        "buyable": True,
        "is_item_attend_promotion": False,
        "item_punish_result": 1,
        "item_lock_infos": [],
        "item_audit_status": 1,
        "check_status": 1,
        "xsec_token": "tok",
    }
    row.update(overrides)
    return row


def _sku(sku_id: str, **overrides) -> dict:
    sku = {
        "skuId": sku_id,
        "skuName": "胡桃色",
        "variantName": "规格一",
        "barcode": "690",
        "erpCode": "ERP-1",
        "basePriceInfo": {"price": 230400, "originalPrice": 260000},
        "stock": 21,
        "specImage": {"link": "https://img/sku.jpg"},
        "weight": 1200,
        "buyable": True,
        "freeze": False,
    }
    sku.update(overrides)
    return sku


class FakeAdapter:
    """只实现这两个端点用到的三个只读方法，并记录调用参数。"""

    def __init__(self, *, items=None, skus_map=None, fail_on=None, fail_code=-1):
        self.items = items if items is not None else [_raw_product()]
        self.skus_map = skus_map if skus_map is not None else {ITEM_ID: [_sku("s1"), _sku("s2")]}
        self.fail_on = fail_on
        self.fail_code = fail_code
        self.calls: list[tuple[str, dict]] = []

    def _maybe_fail(self, name: str, kwargs: dict):
        if self.fail_on != name:
            return None
        return (False, "千帆接口失败", {"code": self.fail_code, "success": False, "msg": "千帆接口失败"})

    def search_items(self, **kwargs):
        self.calls.append(("search_items", kwargs))
        failed = self._maybe_fail("search_items", kwargs)
        if failed:
            return failed
        return True, "成功", _envelope({"items": self.items, "total": len(self.items)})

    def get_skus_by_item_ids(self, item_ids):
        kwargs = {"item_ids": list(item_ids)}
        self.calls.append(("get_skus_by_item_ids", kwargs))
        failed = self._maybe_fail("get_skus_by_item_ids", kwargs)
        if failed:
            return failed
        chunk = {i: self.skus_map[i] for i in item_ids if i in self.skus_map}
        return True, "成功", _envelope({"skusMap": chunk})

    #: 商品列表 / 详情之外的域不应被调用 —— 记下来便于断言。
    def get_order_page(self, **kwargs):  # pragma: no cover - 不该走到
        raise AssertionError("商品端点不应调用订单接口")

    def get_goods_note_page(self, **kwargs):  # pragma: no cover - 不该走到
        raise AssertionError("商品端点不应调用挂链笔记接口")


@pytest.fixture()
def make_user_and_account(client, db):
    """造「一个用户 + 一个带 ark 令牌的 xhs 账号」并返回其令牌请求头。

    令牌走**真实存储形态**（`AccountCookieVersion.encrypted_cookies` 里的信封
    JSON + 真实加密）⇒ `qianfan_cookies_for_account` → `latest_for_channel` 真跑，
    「令牌拿不到」这条判据才有意义。

    ⚠️ **写用自己的 Session**：端点在自己的会话上提交，用例的 `db` 若先开过事务就
    看不到那批行（PG 快照隔离）。同理，同一个会话里「先 INSERT 再读」也会把旧快照
    顶回来（实测报 `ObjectDeletedError` / FK 违例）。写完即关，读交给各自的会话。
    """

    def _make(username: str = "rt-user", *, with_ark: bool = True):
        registered = client.post(
            "/api/auth/register", json={"username": username, "password": "secret123"}
        )
        assert registered.status_code == 200, registered.text
        login = client.post(
            "/api/auth/login", json={"username": username, "password": "secret123"}
        )
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]
        user_id = login.json()["user"]["id"]

        #: ⚠️ **必须用夹具的 `db` 写**（与端点共用同一条连接）：本基座把端点与用例
        #: 绑在同一条 `conn` 上，端点提交只落在该连接的事务里 ⇒ 另开 Session 读到的是
        #: 一个「用户不存在」的世界（实测 FK 违例）。所以写入走 `db`，再 `flush` 让端点
        #: 那边的下一次查询看得到（同连接、同事务）。
        account = PlatformAccount(
            user_id=user_id,
            platform="xhs",
            sub_type="qianfan",
            nickname=f"{username}-店",
            external_user_id=f"ext-{username}",
        )
        db.add(account)
        db.flush()
        account_id = account.id
        if with_ark:
            db.add(
                AccountCookieVersion(
                    platform_account_id=account_id,
                    encrypted_cookies=encrypt_text(
                        dump_channel_envelope(
                            {"ark": "access-token-ark.xiaohongshu.com=ark-token"}
                        )
                    ),
                )
            )
            db.flush()
        return {"Authorization": f"Bearer {token}"}, account_id

    return _make


@pytest.fixture()
def fake_adapter(client):
    """把 adapter 工厂换成替身（依赖注入层，服务层与端点全部真跑）。"""
    state = {"adapter": FakeAdapter()}

    def _factory(cookies):  # noqa: ARG001 - 只证明「按账号令牌造」这一层被走到
        return state["adapter"]

    client.app.dependency_overrides[get_qianfan_api_adapter_factory] = lambda: _factory
    try:
        yield state
    finally:
        client.app.dependency_overrides.pop(get_qianfan_api_adapter_factory, None)


def _items(client, headers, account_id, **payload):
    body = {"platform_account_id": account_id, **payload}
    return client.post("/api/xhs/qianfan/items", json=body, headers=headers)


def _detail(client, headers, account_id, item_id=ITEM_ID):
    return client.post(
        "/api/xhs/qianfan/item-detail",
        json={"platform_account_id": account_id, "item_id": item_id},
        headers=headers,
    )


# --------------------------------------------------------------------------- #
# ① 鉴权与越权
# --------------------------------------------------------------------------- #
def test_items_requires_login(client, db):
    """未登录 ⇒ 401（两个端点同判据）。"""
    assert client.post("/api/xhs/qianfan/items", json={"platform_account_id": 1}).status_code == 401
    assert (
        client.post(
            "/api/xhs/qianfan/item-detail", json={"platform_account_id": 1, "item_id": ITEM_ID}
        ).status_code
        == 401
    )


def test_other_users_account_is_404(client, db, make_user_and_account):
    """别人的账号 ⇒ 404（**不是** 403，避免暴露账号存在性；与 `get_owned_active_account` 一致）。"""
    headers_a, account_id = make_user_and_account("owner-a")
    headers_b, _ = make_user_and_account("intruder-b")
    assert _items(client, headers_b, account_id).status_code == 404
    assert _detail(client, headers_b, account_id).status_code == 404


def test_account_without_ark_token_is_400(client, db, make_user_and_account, fake_adapter):
    """没有 ark 令牌 ⇒ 400 `ARK_SESSION_REQUIRED`，且**一个平台请求都不发**。

    改造前本页读本地台账、无门禁；改实时后登录态是硬前提 —— 拿空 cookie 去打平台
    只会得到一句看不懂的平台 code（`-13000`），所以拦在出口之前。
    """
    headers, account_id = make_user_and_account("no-ark", with_ark=False)
    response = _items(client, headers, account_id)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "ARK_SESSION_REQUIRED"
    assert fake_adapter["adapter"].calls == []


# --------------------------------------------------------------------------- #
# ② 主链路成功
# --------------------------------------------------------------------------- #
def test_items_returns_platform_rows_with_legacy_shape(
    client, db, make_user_and_account, fake_adapter
):
    """`/items` 走真端点 → 实时服务 → 假 adapter，返回**逐字段同形**的行。

    判据（本卡原文「响应字段与改造前逐字段相同」）：`normalize_item_from_raw`
    下发的每个键都在，且值一致；`source` 由 `local` 升为 `platform`。
    """
    headers, account_id = make_user_and_account()
    response = _items(client, headers, account_id, status="on_shelf")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["source"] == "platform"
    assert body["total"] == 1 and body["count"] == 1
    assert body["total_known"] is True
    assert body["products_synced_at"] == ""  # 实时链路没有「同步时间」
    item = body["items"][0]

    #: 改造前 `normalize_item_from_raw` 的**全部**键（少一个都是前端静默退化）。
    expected_keys = {
        "item_id", "title", "article_no", "image", "images", "image_count",
        "price_min_fen", "price_max_fen", "stock", "sku_count", "on_sale_sku_count",
        "sales_total", "sales_30d", "platform_note_count", "note_ids", "category",
        "status_label", "status_key", "buyable", "frozen", "deleted", "audit_status",
        "check_status", "is_channel", "stock_warning_sku_count", "create_time",
        "on_shelf_time", "update_time", "xsec_token", "is_item_attend_promotion",
        "item_lock_infos", "item_punish_result",
        #: 列表层补齐的（`_decorate_realtime_item`）
        "note_count", "note_count_source", "ai_title", "ai_selling_point", "allowed_actions",
    }
    assert expected_keys <= set(item), f"缺字段：{sorted(expected_keys - set(item))}"

    assert item["item_id"] == ITEM_ID
    assert item["title"] == "美式乡村实木抽屉柜"
    assert item["price_min_fen"] == 230400
    assert item["status_key"] == "on_shelf"
    assert item["status_label"] == "售卖中"
    assert item["category"] == "住宅家具 / 柜类 / 斗柜"  # 按 level 升序拼
    assert item["image_count"] == 2  # 商品图 2 + 详情图 1 ⇒ 合并去重口径见 /item-detail
    assert item["note_ids"] == ["n1", "n2"]
    #: 三个动作都在 `allowed_actions` 里（含**不可做**的，附原因 —— 前端靠它渲染按钮）
    assert {row["action"] for row in item["allowed_actions"]} == {"on_shelf", "off_shelf", "delete"}


def test_items_maps_platform_params_exactly(client, db, make_user_and_account, fake_adapter):
    """请求体 → 平台参数**按契约映射**（价格元→分、时间→秒、排序、状态）。

    这些映射错了不会报错，只会静默筛出另一个集合 —— 所以逐个对齐原始入参。
    """
    headers, account_id = make_user_and_account()
    response = _items(
        client,
        headers,
        account_id,
        keyword="  地垫  ",
        status="off_shelf",
        price_from=100,
        price_to=200,
        stock_from=5,
        sales_to=50,
        create_time_from="2026-07-01",
        sort_field="price",
        order="asc",
        page_no=2,
        page_size=10,
    )
    assert response.status_code == 200, response.text
    calls = dict(fake_adapter["adapter"].calls)
    kwargs = calls["search_items"]
    assert kwargs["keyword"] == "地垫"          # 首尾空白已剪
    assert kwargs["status"] == "off_shelf"      # 状态键透传
    assert kwargs["price_from_fen"] == 10000    # 元 → 分
    assert kwargs["price_to_fen"] == 20000
    assert kwargs["stock_from"] == 5
    assert kwargs["sales_to"] == 50
    #: 创建时间：服务层转平台要的 **Unix 秒**（传毫秒平台回 HTTP 500）
    assert kwargs["create_time_from"] == 1782835200
    assert kwargs["sort_field"] == "price" and kwargs["order"] == "asc"
    assert kwargs["page_no"] == 2 and kwargs["page_size"] == 10


def test_item_detail_is_realtime_and_splits_image_groups(
    client, db, make_user_and_account, fake_adapter
):
    """`/item-detail` 打两个平台接口，图集两组**分开**下发 + SKU 归一化。

    ⚠️ 这同时是「搜到别的商品也不能拿去渲染」的判据：替身额外塞一条
    不同 item_id 的商品在前，端点必须挑**精确匹配**的那条。
    """
    headers, account_id = make_user_and_account()
    fake_adapter["adapter"].items = [
        _raw_product(item_id=OTHER_ITEM_ID, item_name="别的商品"),
        _raw_product(),
    ]
    response = _detail(client, headers, account_id)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["source"] == "platform"
    assert body["item"]["item_id"] == ITEM_ID
    assert body["item"]["title"] == "美式乡村实木抽屉柜"  # 不是「别的商品」
    assert body["product_images"] == ["https://img/1.jpg", "https://img/2.jpg"]
    assert body["detail_images"] == ["https://img/d1.jpg"]
    assert body["images"] == ["https://img/1.jpg", "https://img/2.jpg", "https://img/d1.jpg"]
    assert [sku["sku_id"] for sku in body["skus"]] == ["s1", "s2"]
    assert body["sku_count"] == 2
    assert body["skus"][0]["price_fen"] == 230400
    assert body["detail_synced_at"]  # 刚查过 ⇒ 恒「有详情」
    assert body["synced_at"] == "" and body["platform_missing_at"] == ""

    called = [name for name, _ in fake_adapter["adapter"].calls]
    assert called.count("search_items") == 1 and called.count("get_skus_by_item_ids") == 1


def test_repeat_calls_are_idempotent_and_write_nothing(
    client, db, make_user_and_account, fake_adapter
):
    """连调两次结果一致，且**不落库**（实时查询的立身之本：本地不留副本）。"""
    headers, account_id = make_user_and_account()
    first = _items(client, headers, account_id).json()
    second = _items(client, headers, account_id).json()
    assert [row["item_id"] for row in first["items"]] == [row["item_id"] for row in second["items"]]
    _detail(client, headers, account_id)
    _detail(client, headers, account_id)
    assert db.scalars(select(ShopProduct)).all() == []


def test_ai_text_still_comes_from_local_table(
    client, db, make_user_and_account, fake_adapter
):
    """`ai_title` / `ai_selling_point` 是**我方数据**（平台不存在）⇒ 仍从 `shop_products` 取。

    这两个字段是列表「AI 标题」列的来源；实时化不能把它们弄丢，也不能编造。
    """
    headers, account_id = make_user_and_account()
    user_id = db.scalar(select(PlatformAccount.user_id).where(PlatformAccount.id == account_id))
    db.add(
        ShopProduct(
            user_id=user_id,
            platform_account_id=account_id,
            item_id=ITEM_ID,
            item_title="旧标题",
            ai_title="AI 采纳标题",
            ai_selling_point="AI 卖点",
        )
    )
    db.commit()

    body = _items(client, headers, account_id).json()
    item = body["items"][0]
    #: 商品主体是**平台**的（不是台账里的「旧标题」），只有 AI 两列来自本地。
    assert item["title"] == "美式乡村实木抽屉柜"
    assert item["ai_title"] == "AI 采纳标题"
    assert item["ai_selling_point"] == "AI 卖点"


# --------------------------------------------------------------------------- #
# ③ 关键失败面（≤3）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fail_code, expected_status, expected_code",
    [
        (-1, 502, "QIANFAN_QUERY_FAILED"),           # 普通平台失败
        (-13004, 400, "QIANFAN_CREDENTIAL_EXPIRED"), # 凭据过期（DEF-084 的 code）
    ],
)
def test_item_detail_maps_platform_failure_to_structured_http(
    client, db, make_user_and_account, fake_adapter, fail_code, expected_status, expected_code
):
    """详情页没有「空态」可言 ⇒ 平台失败转**结构化**错误，且凭据失效要能分辨。

    ⚠️ 凭据失效走 400、其余走 502：两者前端**自救路径不同**（重新登录 vs 重试）。
    若都塌成 500，用户只能看到「Internal Server Error」。
    """
    headers, account_id = make_user_and_account()
    fake_adapter["adapter"].fail_on = "search_items"
    fake_adapter["adapter"].fail_code = fail_code
    response = _detail(client, headers, account_id)
    assert response.status_code == expected_status, response.text
    assert response.json()["detail"]["code"] == expected_code


@pytest.mark.parametrize("fail_code, expected_code", [(-1, "QIANFAN_QUERY_FAILED"), (-13004, "QIANFAN_CREDENTIAL_EXPIRED")])
def test_items_returns_empty_page_with_error_not_5xx(
    client, db, make_user_and_account, fake_adapter, fail_code, expected_code
):
    """列表页平台失败 ⇒ **200 + 空 items + error**（本卡硬约束：显示空态，不抛 500）。

    ⚠️ 不许「静默空列表」：`error` 里必须带稳定 `code`，否则用户看到空表格会以为
    「店里没商品」。也**不许回退本地台账** —— 那是两份真相。
    """
    headers, account_id = make_user_and_account()
    fake_adapter["adapter"].fail_on = "search_items"
    fake_adapter["adapter"].fail_code = fail_code
    response = _items(client, headers, account_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == [] and body["count"] == 0
    assert body["error"]["code"] == expected_code
    assert body["error"]["message"]  # 人话原因必须在


def test_item_detail_404_when_platform_has_no_such_item(
    client, db, make_user_and_account, fake_adapter
):
    """平台查不到 ⇒ 404；**搜到别的商品不算查到**（不能拿第一条顶替）。"""
    headers, account_id = make_user_and_account()
    fake_adapter["adapter"].items = [_raw_product(item_id=OTHER_ITEM_ID)]
    fake_adapter["adapter"].skus_map = {}
    response = _detail(client, headers, account_id)
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# ④ 不变量
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "method, path",
    [
        ("post", "/api/xhs/qianfan/sync-shop-goods"),
        ("get", "/api/xhs/qianfan/sync-shop-goods/status"),
    ],
)
def test_retired_sync_endpoints_are_410_and_out_of_openapi(client, db, method, path):
    """两个同步端点改 **410 墓碑** 且**不进 OpenAPI**（端点计数只算真实能力）。

    410 而不是 404：老前端 / 开着的页签能拿到一句读得懂的答复。
    """
    response = getattr(client, method)(path)
    assert response.status_code == 410, response.text
    assert response.json()["detail"]["code"] == "SHOP_GOODS_SYNC_REMOVED"
    assert path not in client.app.openapi()["paths"]


def test_retired_sync_job_is_gone_from_registry_and_seed():
    """定时任务 `system.shop_goods_sync` 已从**注册表**与**随包种子**里摘除。

    只删注册表不删种子 ⇒ 下次播种会 warning 后跳过（不算错），但种子文件里留着一条
    「注册表里没有」的死行，会引诱人把它加回去。两边必须同批。
    """
    from backend.app.services.job_registry import job_registry

    registry = job_registry()
    assert "system.shop_goods_sync" not in registry
    #: 反面对照：同域**未退场**的同步任务仍在（避免「整片删空」式假绿）。
    #: ⚠️ `system.order_sync` 也被设计 75 S3 摘除了（订单域全实时），
    #: 所以这里只留仍在的 `goods_note_sync`（笔记同步属于 S4 范围）。
    assert "system.goods_note_sync" in registry

    seed = json.loads(
        (__import__("pathlib").Path(__file__).resolve().parents[2]
         / "config/initial_data/scheduled_jobs.json").read_text(encoding="utf-8")
    )
    keys = {row["job_key"] for row in seed["scheduled_jobs"]}
    assert "system.shop_goods_sync" not in keys


@pytest.mark.parametrize(
    "status, expected_platform_status",
    [
        #: `all` = 平台「全部」tab 的口径 ⇒ 不发 `card_type`（SDK 收到 "all"）。
        ("all", "all"),
        #: 空串按模型默认值处理（`or "on_shelf"`）——「不填」就是「售卖中」，
        #: 与前端默认选中第一个 tab 同口径，不是「全部」。
        ("", "on_shelf"),
    ],
    ids=["all", "empty-defaults-to-on_shelf"],
)
def test_status_values_never_500(
    client, db, make_user_and_account, fake_adapter, status, expected_platform_status
):
    """`status` 的两个边界取值都不 500，且落到**确定的**平台口径（本卡判据）。

    - `all` ⇒ SDK 收到 `"all"`（不发 `card_type`，与平台页面点「全部」同形）；
    - 空串 ⇒ 退回模型默认 `on_shelf`（缺省即第一个 tab），**不是**「全部」。
    两者都不是异常路径，所以都不能 500。
    """
    headers, account_id = make_user_and_account()
    response = _items(client, headers, account_id, status=status)
    assert response.status_code == 200, response.text
    assert dict(fake_adapter["adapter"].calls)["search_items"]["status"] == expected_platform_status


def test_unsupported_status_key_is_rejected_400(client, db, make_user_and_account):
    """而**未知的状态键**仍如实 400（`QIANFAN_STATUS_UNSUPPORTED`）—— 退回默认与此分界。

    分界判据：`all` / 空串是**合法的「全部」口径**；`no_such_status` 是**拼错**，
    静默当成「全部」会让用户以为筛过了。
    """
    headers, account_id = make_user_and_account()
    response = _items(client, headers, account_id, status="no_such_status")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "QIANFAN_STATUS_UNSUPPORTED"


def test_note_count_is_not_queried_per_item(client, db, make_user_and_account, fake_adapter):
    """列表页**不逐商品打平台**查挂链笔记数（成本红线：一页 20 条 = 20 次请求）。

    判据两条：① 只调了 `search_items` 一次，**没调** `get_goods_note_page`；
    ② `note_count` 与 `platform_note_count` 同值且 `note_count_source="platform"`
    （标明口径，而不是假装它是实时篇数）。
    """
    headers, account_id = make_user_and_account()
    fake_adapter["adapter"].items = [_raw_product() for _ in range(20)]
    body = _items(client, headers, account_id, page_size=20).json()

    called = [name for name, _ in fake_adapter["adapter"].calls]
    assert called == ["search_items"], f"列表页多打了平台：{called}"
    for item in body["items"]:
        assert item["note_count"] == item["platform_note_count"] == 4
        assert item["note_count_source"] == "platform"
