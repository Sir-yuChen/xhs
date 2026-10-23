"""千帆「商品列表」页的后端契约测试（`POST /api/xhs/qianfan/items` + `/item-detail`）。

> **v1.32 起改本地**（设计 13 §30.3）：`/items` 不再打千帆，改查 `shop_products`
> 台账。本文件的关注点随之改写：
> 1. **零平台请求** —— 适配器调用计数在查询前后必须一模一样（风控面归零）；
> 2. **展示契约不变** —— `items[]` 逐字段与改造前同构（`normalize_item_from_raw`）；
> 3. **筛选 / 排序 / 分页的本地口径** —— 关键词 OR、状态按行推导、区间「空值行
>    不命中」、NULL 排后（设计 13 §30.3.2）；
> 4. **门禁四件套不回归** —— 未绑店 400 / 状态白名单 400 / 时间坏 400 / 越权 404；
> 5. **详情弹窗数据源** —— `/item-detail` 纯本地，SKU 白名单，没补详情 = 空态字段。
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.api.platforms.xhs.qianfan import (
    MAX_BATCH_DELETE,
    get_qianfan_api_adapter_factory,
)
from backend.app.core.database import Base, get_db
from backend.app.core.time import shanghai_now
from backend.app.main import app
from backend.app.models import Note, NoteAsset, NoteGoods, PlatformAccount, User
from backend.app.models.shop_product import ShopProduct
from backend.app.services.shop_goods_service import existing_rows, store_entry
from backend.app.services.shop_goods_sync_service import store_products
from tests.backend.cookie_fixtures import give_ark_cookies

import pytest


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration
client = TestClient(app)

#: `_setup()` 建起的会话工厂（供模块级辅助函数使用；每个用例自己 `_setup` 一次）。
_SESSION_LOCAL = None


def _session_for(account_id: int):  # noqa: ARG001  (形参保留给将来的按账号分库)
    """开一个测试库会话（`_setup` 必须先跑过）。"""
    assert _SESSION_LOCAL is not None, "先调 _setup(tmp_path)"
    return _SESSION_LOCAL()


def _ark_item(item_id: str, title: str, **overrides) -> dict:
    """千帆商品条目的**真实字段名**（2026-09-19 从页面响应逐字段核对）。

    ⚠️ **不含 `card_type`** —— 与真机一致（1974 条台账全量核对：平台列表响应
    没有这个键，v1.32 补记）。状态由 `shop_products_service.status_key_from_raw`
    从生命周期信号推导（在售 = 可购买 + 有上架时间）。
    """
    base = {
        "item_id": item_id,
        "item_name": title,
        "article_no": "1056576341942",
        "min_price": 3226,          # 分
        "max_price": 5402,          # 分
        "total_stock": 700,
        "sku_count": 7,
        "on_sale_sku_count": 7,
        "product_note_num": 1,
        "product_note_ids": ["6a5d4ef90000000013027665"],
        "buyable": True,
        "item_freeze": False,
        "deleted": False,
        "item_audit_status": 2,
        "check_status": 1,
        "is_channel": False,
        "create_time": 1784082313000,
        "on_shelf_time": 1784084996000,
        "update_time": 1784082446000,
        "xsec_token": "ARK-ITEM-TOKEN",
        "seller_id": "6a3250e48dd4d00015ca26a0",   # 不该出现在响应契约里
        "sale_qty_info": {"acc_sale_qty": 12, "sale_qty30": 3},
        "inventory_risk_info": {"has_inventory_risk": True, "warning_sku_count": 2},
        "images": [{"link": f"http://qimg.example/{item_id}-1.jpg", "width": 1440, "height": 1440}],
        "image_descriptions": [{"link": f"http://qimg.example/{item_id}-2.jpg"}],
        "categories": [
            {"id": "c1", "name": "餐饮具", "level": 1},
            {"id": "c2", "name": "野餐垫", "level": 3},
        ],
    }
    base.update(overrides)
    return base


def _off_shelf_item(item_id: str, title: str) -> dict:
    """已下架商品（真机形状：不可购买 + 无上架时间 + 有下架时间，v1.32 实测 2 条）。"""
    return _ark_item(
        item_id,
        title,
        buyable=False,
        on_shelf_time=None,
        off_shelf_time=1790060618000,
        on_sale_sku_count=0,
    )


class FakeArkAdapter:
    """Canned 千帆响应；把调用参数原样记下来给断言用。

    v1.32 起 `/items` 与 `/item-detail` **不允许**碰它（本地读）——
    用例用 `calls` 计数断言「零平台请求」。
    """

    mode = "ok"
    total = 1994
    items: list = []
    goods_pages: dict = {}
    calls: dict = {}

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def get_seller_info(self):
        return True, "ok", {
            "code": 0,
            "success": True,
            "data": {"user_id": "SELLER-1", "seller_name": "观隅好物的店"},
        }

    def search_items(self, **kwargs):
        FakeArkAdapter.calls["search"] = FakeArkAdapter.calls.get("search", 0) + 1
        if FakeArkAdapter.mode == "expired":
            return False, "缺失登录凭证, 请重新登录", {"code": -13000, "success": False}
        return True, "ok", {
            "code": 200,
            "success": True,
            "data": {"total": FakeArkAdapter.total, "items": FakeArkAdapter.items},
        }

    def get_goods_note_page(self, page_num: int = 1, page_size: int = 20):
        FakeArkAdapter.calls["goods_page"] = FakeArkAdapter.calls.get("goods_page", 0) + 1
        entries = FakeArkAdapter.goods_pages.get(page_num) or []
        return True, "ok", {
            "code": 0,
            "success": True,
            "data": {"note_infos": entries, "total": len(entries), "end_tag": True},
        }


def _setup(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'shop-items.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    global _SESSION_LOCAL
    _SESSION_LOCAL = SessionLocal

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    FakeArkAdapter.mode = "ok"
    FakeArkAdapter.calls = {}
    FakeArkAdapter.total = 1994
    FakeArkAdapter.items = [_ark_item("ITEM-1", "外出野餐垫防潮垫")]
    FakeArkAdapter.goods_pages = {}

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
        # 自己的账号但**没绑店**：门禁四件套里「未绑定 → 400」要用它（越权是 404，
        # 与它是两回事）。
        owned_unbound = PlatformAccount(
            user_id=owner_id, platform="xhs", sub_type="creator", nickname="没绑店的号", status="active"
        )
        foreign = PlatformAccount(
            user_id=intruder_id, platform="xhs", sub_type="pc", nickname="别人的号", status="active"
        )
        db.add_all([owned, owned_unbound, foreign])
        db.commit()
        ids = {"owned": owned.id, "owned_unbound": owned_unbound.id, "foreign": foreign.id}
    finally:
        db.close()
    return owner_headers, intruder_headers, ids, SessionLocal


def _teardown():
    app.dependency_overrides.clear()


def _bind(headers, account_id: int) -> None:
    """给账号导入千帆（ark）登录态（ADR-021：`POST /qianfan/bind` 已退场回 410）。

    原实现打 `/api/xhs/qianfan/bind` 建店铺实体 + 关系。现在「开通千帆能力」
    就是给账号写 `ark` 通道凭据（账号矩阵里的凭据操作），故直接走存储层。
    """
    db = _session_for(account_id)
    try:
        account = db.get(PlatformAccount, account_id)
        assert account is not None, account_id
        give_ark_cookies(db, account)
        db.commit()
    finally:
        db.close()


def _seed_products(session_local, account_id: int, items: list[dict]) -> None:
    """把千帆条目**走真实归一化**落进 `shop_products`（同步任务的同一入口）。

    ADR-021：归属键 = `platform_account_id`（即账号主键），店铺实体已退场。
    """
    db = session_local()
    try:
        account = db.get(PlatformAccount, account_id)
        stored = store_products(db, account.user_id, account.id, items)
        assert len(stored) == len(items), stored
        db.commit()
    finally:
        db.close()


def _seed_detail(session_local, account_id: int, item_id: str, skus: list[dict]) -> None:
    """直写 SKU 详情（等价于同步任务的详情补齐段落库后的形状）。"""
    db = session_local()
    try:
        row = db.scalar(
            select(ShopProduct).where(
                ShopProduct.item_id == item_id,
                ShopProduct.platform_account_id == account_id,
            )
        )
        row.detail_json = json.dumps(skus, ensure_ascii=False)
        row.sku_count = len(skus)
        row.detail_synced_at = shanghai_now()
        db.commit()
    finally:
        db.close()


def _items(headers, account_id: int, **overrides) -> dict:
    body = {"platform_account_id": account_id, **overrides}
    response = client.post("/api/xhs/qianfan/items", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 1. 本地读：契约不变 + 零平台请求
# --------------------------------------------------------------------------- #
def test_items_reads_local_ledger_without_touching_the_platform(tmp_path):
    """v1.32：`/items` 查本地台账，`items[]` 契约与改造前逐字段一致，**零平台请求**。"""
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        _seed_products(SessionLocal, ids["owned"], [_ark_item("ITEM-1", "外出野餐垫防潮垫")])
        FakeArkAdapter.calls = {}  # 绑定那次的 seller 探活不计入

        payload = _items(headers, ids["owned"], keyword="野餐")
        assert payload["source"] == "local"
        assert payload["shop_name"] == "观隅好物", "ADR-021：店铺名 = 账号昵称"
        assert payload["total"] == 1
        assert payload["has_more"] is False
        # 同步时间即查询时间口径（本地台账没有「当下」）
        assert payload["queried_at"], "同步时间必须回给前端（空 = 从没同步过）"
        item = payload["items"][0]
        assert item["item_id"] == "ITEM-1"
        assert item["title"] == "外出野餐垫防潮垫"
        # 金额**原样是分**（换算留给展示层，避免两处各乘一次 100）
        assert item["price_min_fen"] == 3226 and item["price_max_fen"] == 5402
        assert item["stock"] == 700 and item["sku_count"] == 7
        assert item["sales_total"] == 12 and item["sales_30d"] == 3
        # 挂链笔记数**走关系表口径**（设计 13 §32.4）：本 fixture 没有 `note_goods`
        # 关系行 ⇒ `note_count = 0`；平台原文的 `product_note_num=1` 落在
        # `platform_note_count` 里作对照。本地生成的笔记在平台侧还不存在，
        # 若继续用平台值，用户点完「生成笔记」会看到列上数字不动。
        assert item["note_count"] == 0
        assert item["platform_note_count"] == 1
        assert item["note_ids"] == ["6a5d4ef90000000013027665"]
        assert item["category"] == "餐饮具 / 野餐垫"      # 按 level 升序拼路径
        assert item["status_label"] == "售卖中"           # 按行推导（平台不给 card_type）
        assert item["create_time"].startswith("2026-07-1")
        assert item["stock_warning_sku_count"] == 2
        assert item["image"].endswith("-1.jpg") and item["image_count"] == 2
        # 白名单构造：千帆的内部字段不进入契约
        assert "seller_id" not in item and "sale_qty_info" not in item
        # **零平台请求**：适配器一次都不该被碰
        assert FakeArkAdapter.calls == {}
    finally:
        _teardown()


def test_items_needs_no_ark_token_for_local_read(tmp_path):
    """读本地台账**不涉及凭据**：账号没有 ark 令牌也能看（用户 2026-09-30 口径）。

    用户口径：「**商品列表（千帆）页查询的本地数据库，无需校验千帆是否可用**」
    —— 本地读链路**零门禁**（ADR-021 把「本地台账读取」与「平台触碰」彻底分开）。
    实时链路时代这里会 400 `QIANFAN_CREDENTIAL_MISSING`；本地化后「看货」不再
    依赖一次可用的千帆会话 —— 只有「同步商品」才需要令牌。
    """
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        #: 不再「绑店」：账号本身就是归属（ADR-021）。这里刻意**不给**它 ark 凭据，
        #: 用来证明本地读取确实零门禁。
        _seed_products(SessionLocal, ids["owned"], [_ark_item("ITEM-9", "没有令牌也能看")])

        payload = _items(headers, ids["owned"])
        assert payload["total"] == 1
        assert payload["items"][0]["title"] == "没有令牌也能看"
        assert FakeArkAdapter.calls == {}
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 2. 筛选 / 排序 / 分页的本地口径
# --------------------------------------------------------------------------- #
def test_items_filters_locally_with_documented_semantics(tmp_path):
    """本地筛选口径：关键词 OR（标题/ID/货号）、状态按行、区间**空值行不命中**。"""
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        _seed_products(
            SessionLocal,
            ids["owned"],
            [
                _ark_item("ITEM-A", "外出野餐垫防潮垫", article_no="A-001", min_price=3226, max_price=5402, total_stock=700),
                _ark_item("ITEM-B", "车载收纳箱", article_no="B-002", min_price=9900, max_price=19900, total_stock=30),
                # 缺价格 / 缺库存的行：区间筛选时**不命中**（无法确认即不代入）
                _ark_item("ITEM-C", "没有价格的怪东西", min_price=None, max_price=None, total_stock=None),
            ],
        )
        FakeArkAdapter.calls = {}

        # 关键词：命中标题 / 商品 ID / 货号三个字段，逗号多值取 OR
        assert _items(headers, ids["owned"], keyword="野餐")["total"] == 1
        assert _items(headers, ids["owned"], keyword="ITEM-B")["total"] == 1
        assert _items(headers, ids["owned"], keyword="A-001")["total"] == 1
        assert _items(headers, ids["owned"], keyword="野餐,收纳箱")["total"] == 2

        # 价格区间（分）：[min,max] 与 [from,to] 有交集即命中；None 行不命中
        assert _items(headers, ids["owned"], price_from=30, price_to=60)["total"] == 1     # ITEM-A 32.26~54.02
        assert _items(headers, ids["owned"], price_from=100, price_to=200)["total"] == 1   # ITEM-B 99~199
        assert _items(headers, ids["owned"], price_from=1, price_to=100000)["total"] == 2  # ITEM-C 仍不命中

        # 库存区间
        assert _items(headers, ids["owned"], stock_from=100)["total"] == 1
        assert _items(headers, ids["owned"], stock_to=100)["total"] == 1

        # 状态：按行推导（platform 不给 card_type）—— 三条都在售
        assert _items(headers, ids["owned"], status="on_shelf")["total"] == 3
        assert _items(headers, ids["owned"], status="all")["total"] == 3
        assert _items(headers, ids["owned"], status="off_shelf")["total"] == 0

        # 全程零平台请求
        assert FakeArkAdapter.calls == {}
    finally:
        _teardown()


def test_items_status_filter_matches_derived_off_shelf_rows(tmp_path):
    """下架行（无上架时间 + 有下架时间）在状态筛选里归「已下架」，标签按行展示。"""
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        _seed_products(
            SessionLocal,
            ids["owned"],
            [
                _ark_item("ITEM-ON", "在售商品"),
                _off_shelf_item("ITEM-OFF", "下架商品"),
            ],
        )
        payload = _items(headers, ids["owned"], status="all")
        labels = {item["item_id"]: item["status_label"] for item in payload["items"]}
        assert labels == {"ITEM-ON": "售卖中", "ITEM-OFF": "已下架"}
        assert _items(headers, ids["owned"], status="off_shelf")["total"] == 1
        assert _items(headers, ids["owned"], status="on_shelf")["total"] == 1
    finally:
        _teardown()


def test_items_sorts_with_nulls_last(tmp_path):
    """排序：五个字段映射到列；**NULL 排后**（两个方向都是），同值按 id 稳定。

    用 `status="all"`：ITEM-2 没有上架时间 ⇒ 推导为「已下架」，默认的
    「售卖中」筛选会把它滤掉（那是对的，见状态筛选用例）。
    """
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        _seed_products(
            SessionLocal,
            ids["owned"],
            [
                _ark_item("ITEM-1", "商品一", create_time=1784082313000, on_shelf_time=1784084996000),
                _ark_item("ITEM-2", "商品二", create_time=1784089999000, on_shelf_time=None),   # 没上架过
                _ark_item("ITEM-3", "商品三", create_time=1784070000000, on_shelf_time=1784080000000),
            ],
        )
        desc = [i["item_id"] for i in _items(headers, ids["owned"], status="all", sort_field="create_time", order="desc")["items"]]
        assert desc == ["ITEM-2", "ITEM-1", "ITEM-3"]
        asc = [i["item_id"] for i in _items(headers, ids["owned"], status="all", sort_field="create_time", order="asc")["items"]]
        assert asc == ["ITEM-3", "ITEM-1", "ITEM-2"]
        # 按上架时间排序：ITEM-2 没有上架时间 ⇒ 排后（desc 也在后）
        shelf = [i["item_id"] for i in _items(headers, ids["owned"], status="all", sort_field="on_shelf_time", order="desc")["items"]]
        assert shelf == ["ITEM-1", "ITEM-3", "ITEM-2"]
    finally:
        _teardown()


def test_items_defaults_to_create_time_when_no_sort_given(tmp_path):
    """不传排序参数 ⇒ 后端用它自己的**稳定默认序**（`create_time` 倒序）。

    v1.37（用户 2026-09-23 口径「默认不排序，只有点击触发后进行排序，参考 antd
    组件中的排序」）：前端**默认不带** `sort_field` / `order` 了 —— 于是「默认序是什么」
    从「前端的约定」变成了**后端契约**，必须在这里钉住。前端自己编一个默认字段、或送
    空串，都是抢走后端对兜底序的选择权（第二份真相源）。
    """
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        _seed_products(
            SessionLocal,
            ids["owned"],
            [
                _ark_item("ITEM-1", "商品一", create_time=1784082313000, on_shelf_time=1784084996000),
                _ark_item("ITEM-2", "商品二", create_time=1784089999000, on_shelf_time=None),
                _ark_item("ITEM-3", "商品三", create_time=1784070000000, on_shelf_time=1784080000000),
            ],
        )
        # ① 完全不传排序字段：按 create_time 倒序，且**后端回传生效口径**（不是空）
        default = _items(headers, ids["owned"], status="all")
        assert [i["item_id"] for i in default["items"]] == ["ITEM-2", "ITEM-1", "ITEM-3"]
        assert default["sort_field"] == "create_time" and default["order"] == "desc"
        # ② 显式传 create_time 倒序 ⇒ 与不传走同一条路（结果逐项相同）
        explicit = _items(
            headers, ids["owned"], status="all", sort_field="create_time", order="desc"
        )
        assert [i["item_id"] for i in default["items"]] == [i["item_id"] for i in explicit["items"]]
        # ③ 未知字段值 ⇒ 兜回默认字段（前端不许靠猜来与后端达成一致）
        unknown = _items(
            headers, ids["owned"], status="all", sort_field="no_such_field", order="desc"
        )
        assert unknown["sort_field"] == "create_time" and unknown["order"] == "desc"
    finally:
        _teardown()


def test_items_paginates_locally(tmp_path):
    """分页：本地切片；`has_more` / `total` 口径与改造前一致。"""
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        _seed_products(
            SessionLocal,
            ids["owned"],
            [_ark_item(f"ITEM-{index:02d}", f"商品{index}") for index in range(1, 6)],
        )
        page1 = _items(headers, ids["owned"], page_no=1, page_size=2)
        assert page1["total"] == 5 and page1["count"] == 2 and page1["has_more"] is True
        page3 = _items(headers, ids["owned"], page_no=3, page_size=2)
        assert page3["count"] == 1 and page3["has_more"] is False
    finally:
        _teardown()


def test_items_create_time_range_filters_by_day(tmp_path):
    """创建时间区间：日期 → 毫秒窗（结束日含当天）；空值行不命中。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        _seed_products(
            SessionLocal,
            ids["owned"],
            [
                _ark_item("ITEM-JUL", "七月商品", create_time=1784082313000),   # 2026-07-15 附近
                _ark_item("ITEM-AUG", "八月商品", create_time=1786760313000),   # 2026-08-15 附近
            ],
        )
        payload = _items(
            headers, ids["owned"], create_time_from="2026-07-01", create_time_to="2026-07-16"
        )
        assert payload["total"] == 1 and payload["items"][0]["item_id"] == "ITEM-JUL"
        # 边界：结束日含当天（23:59:59.999）—— 选 7-15 应收到 7-15 的商品
        same_day = _items(
            headers, ids["owned"], create_time_from="2026-07-15", create_time_to="2026-07-15"
        )
        assert same_day["total"] == 1
        assert FakeArkAdapter.calls == {}
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 3. 本地读的门禁口径：**零门禁**（ADR-021 反转了原来的「门禁四件套」）
# --------------------------------------------------------------------------- #
def test_items_needs_no_ark_session_for_local_read(tmp_path):
    """无 ark 会话的账号读 `/items` 也是 200（空台账），**不是 400**。

    ⚠️ **被取代的旧断言（原句保留）**：本用例原名 `test_items_requires_binding`，
    断言 *「未绑定：给结构化 400（码 + 可执行文案），不是空列表」* +
    *「`detail["code"] == "QIANFAN_BINDING_REQUIRED"`」*。

    用户 2026-09-30 口径推翻它：*「商品页走的本地数据库数据，不应该校验是否存在千帆
    店铺，所有校验应该校验的是该小红书账号下是否存在千帆可用的会话」* ——
    而 `/items` 读的是**本地台账**（零平台请求）⇒ **连「有没有 ark 会话」都不是门禁**，
    未登录千帆也该能看已同步的数据（登录态只决定「能不能同步新数据」）。
    """
    headers, _, ids, _ = _setup(tmp_path)
    try:
        response = client.post(
            "/api/xhs/qianfan/items",
            json={"platform_account_id": ids["owned"]},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["source"] == "local"
        assert payload["items"] == [], "没同步过 ⇒ 空台账（不是错误）"
        assert FakeArkAdapter.calls == {}          # 本地读**不发**平台请求
    finally:
        _teardown()


def test_items_rejects_unknown_status_instead_of_silently_widening(tmp_path):
    """未知状态 → 400（附支持列表）。悄悄按「全部」返回才是真正的坏结果。"""
    headers, _, ids, _ = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        response = client.post(
            "/api/xhs/qianfan/items",
            json={"platform_account_id": ids["owned"], "status": "goods_note"},
            headers=headers,
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["code"] == "QIANFAN_STATUS_UNSUPPORTED"
        assert "on_shelf" in detail["supported"] and "all" in detail["supported"]
    finally:
        _teardown()


def test_items_rejects_bad_or_reversed_time_range(tmp_path):
    headers, _, ids, _ = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        bad = client.post(
            "/api/xhs/qianfan/items",
            json={"platform_account_id": ids["owned"], "create_time_from": "昨天"},
            headers=headers,
        )
        assert bad.status_code == 400
        assert bad.json()["detail"]["code"] == "QIANFAN_TIME_RANGE_INVALID"

        reversed_range = client.post(
            "/api/xhs/qianfan/items",
            json={
                "platform_account_id": ids["owned"],
                "create_time_from": "2026-07-16",
                "create_time_to": "2026-07-01",
            },
            headers=headers,
        )
        assert reversed_range.status_code == 400
        assert reversed_range.json()["detail"]["code"] == "QIANFAN_TIME_RANGE_INVALID"
    finally:
        _teardown()


def test_items_requires_owned_account(tmp_path):
    headers, intruder_headers, ids, _ = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        assert (
            client.post(
                "/api/xhs/qianfan/items",
                json={"platform_account_id": ids["owned"]},
                headers=intruder_headers,
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/xhs/qianfan/items",
                json={"platform_account_id": 999999},
                headers=headers,
            ).status_code
            == 404
        )
    finally:
        _teardown()


def test_items_page_size_is_clamped_by_request_model(tmp_path):
    """`page_size` 超出 1..100 → 422（有界），本地查询也不放过界页。"""
    headers, _, ids, _ = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        assert (
            client.post(
                "/api/xhs/qianfan/items",
                json={"platform_account_id": ids["owned"], "page_size": 500},
                headers=headers,
            ).status_code
            == 422
        )
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 4. 商品详情弹窗（/item-detail，纯本地）
# --------------------------------------------------------------------------- #
def test_item_detail_returns_row_with_skus_and_images(tmp_path):
    """详情弹窗数据源：基本信息同构 + 图集 + SKU 白名单 + 详情补齐时间。"""
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        _seed_products(SessionLocal, ids["owned"], [_ark_item("ITEM-1", "外出野餐垫防潮垫")])
        _seed_detail(
            SessionLocal,
            ids["owned"],
            "ITEM-1",
            [
                {
                    "sku_id": "SKU-1",
                    "sku_name": "米白 40*60cm",
                    "variant_name": "米白",
                    "barcode": "6901234567890",
                    "erp_code": "ERP-1",
                    "price_fen": 3226,
                    "original_price_fen": 4999,
                    "stock": 100,
                    "spec_image": "http://qimg.example/sku-1.jpg",
                    "weight": 360,
                    "buyable": True,
                    "freeze": False,
                }
            ],
        )
        FakeArkAdapter.calls = {}

        response = client.post(
            "/api/xhs/qianfan/item-detail",
            json={"platform_account_id": ids["owned"], "item_id": "ITEM-1"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["item_id"] == "ITEM-1"
        assert payload["shop_name"] == "观隅好物", "ADR-021：店铺名 = 账号昵称"
        assert payload["item"]["title"] == "外出野餐垫防潮垫"
        assert payload["item"]["sku_count"] == 7
        assert payload["sku_count"] == 1                       # 详情补齐到的 SKU 数
        assert payload["images"] == [
            "http://qimg.example/ITEM-1-1.jpg",
            "http://qimg.example/ITEM-1-2.jpg",
        ]
        assert len(payload["skus"]) == 1
        sku = payload["skus"][0]
        assert sku["sku_name"] == "米白 40*60cm" and sku["price_fen"] == 3226
        assert sku["stock"] == 100 and sku["barcode"] == "6901234567890"
        assert payload["detail_synced_at"], "补过详情必须带回时间"
        assert payload["platform_missing_at"] == ""
        assert FakeArkAdapter.calls == {}                     # 纯本地
    finally:
        _teardown()


def test_item_detail_splits_product_images_and_detail_images(tmp_path):
    """v2.15（§13.3.6）：`/item-detail` **分开**下发主图与详情图。

    真机实证：`images`（主图）与 `image_descriptions`（详情图）是**两套不同的图**
    （10 张方图 vs 5 张竖长图）。此前 `_image_links` 把两者合并，导致「生成详情图」
    只能拿到主图 —— **原商品详情图从未被用作参考**（这正是本需求的由来）。
    """
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        _seed_products(
            SessionLocal,
            ids["owned"],
            [
                _ark_item(
                    "ITEM-1",
                    "外出野餐垫防潮垫",
                    images=[
                        {"link": "http://qimg.example/main-1.jpg"},
                        {"link": "http://qimg.example/main-2.jpg"},
                    ],
                    image_descriptions=[
                        {"link": "http://qimg.example/detail-1.jpg"},
                        {"link": "http://qimg.example/detail-2.jpg"},
                        {"link": "http://qimg.example/detail-3.jpg"},
                    ],
                )
            ],
        )
        response = client.post(
            "/api/xhs/qianfan/item-detail",
            json={"platform_account_id": ids["owned"], "item_id": "ITEM-1"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        # 主图 / 详情图各自成组（不混、不互相包含）
        assert payload["product_images"] == [
            "http://qimg.example/main-1.jpg",
            "http://qimg.example/main-2.jpg",
        ]
        assert payload["detail_images"] == [
            "http://qimg.example/detail-1.jpg",
            "http://qimg.example/detail-2.jpg",
            "http://qimg.example/detail-3.jpg",
        ]
        # 合并口径 `images` 仍保留（详情弹窗展示用）——两者都在里面
        assert "http://qimg.example/main-1.jpg" in payload["images"]
        assert "http://qimg.example/detail-1.jpg" in payload["images"]
        assert FakeArkAdapter.calls == {}  # 纯本地
    finally:
        _teardown()


def test_item_detail_empty_when_never_backfilled(tmp_path):
    """没补过详情：`skus=[]` + `detail_synced_at=""`（**不是 0 行已同步**）。"""
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        _seed_products(SessionLocal, ids["owned"], [_ark_item("ITEM-1", "没补详情的商品")])
        payload = client.post(
            "/api/xhs/qianfan/item-detail",
            json={"platform_account_id": ids["owned"], "item_id": "ITEM-1"},
            headers=headers,
        ).json()
        assert payload["skus"] == []
        assert payload["detail_synced_at"] == ""
        assert payload["sku_count"] is None
        assert payload["item"]["title"] == "没补详情的商品"
    finally:
        _teardown()


def test_item_detail_404_for_unknown_item_and_ownership(tmp_path):
    headers, intruder_headers, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        _seed_products(SessionLocal, ids["owned"], [_ark_item("ITEM-1", "外出野餐垫防潮垫")])

        missing = client.post(
            "/api/xhs/qianfan/item-detail",
            json={"platform_account_id": ids["owned"], "item_id": "ITEM-NONE"},
            headers=headers,
        )
        assert missing.status_code == 404

        assert (
            client.post(
                "/api/xhs/qianfan/item-detail",
                json={"platform_account_id": ids["owned"], "item_id": "ITEM-1"},
                headers=intruder_headers,
            ).status_code
            == 404
        )

        unbound = client.post(
            "/api/xhs/qianfan/item-detail",
            json={"platform_account_id": ids["owned_unbound"], "item_id": "ITEM-1"},
            headers=headers,
        )
        #: ⚠️ **被取代的旧断言（原句保留）**：原来断言 *「无 ark 会话 → 400
        #: `QIANFAN_BINDING_REQUIRED`」*。ADR-021 反转（本地读零门禁）⇒ 现在
        #: 因为**这个账号名下没有 ITEM-1** 而 404 —— 与「归属不对」同一码，
        #: 但原因不同：这里是「本账号没同步过这件商品」。
        assert unbound.status_code == 404, unbound.text
        assert "QIANFAN_BINDING_REQUIRED" not in unbound.text, (
            "本地读不该再有千帆门禁（404 的原因应是「本账号没有这件商品」）"
        )
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 5. 挂链笔记反查（不变：本地 join，零平台请求）
# --------------------------------------------------------------------------- #
def test_item_notes_reads_relation_table_without_touching_the_platform(tmp_path):
    """商品 → 挂链笔记反查：**关系表源**（`note_goods` join `notes`，设计 13 §31.6），
    零平台请求，并诚实报覆盖率。"""
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        db = SessionLocal()
        try:
            account = db.get(PlatformAccount, ids["owned"])
            # 台账商品（关系表的商品侧）
            store_products(db, account.user_id, account.id, [_ark_item("ITEM-1", "外出野餐垫防潮垫")])
            # 同步链路落库的笔记 + 关系（= link_note_goods 的产物形状）
            note = Note(
                user_id=account.user_id,
                platform_account_id=account.id,
                platform="xhs",
                note_id="NOTE-A",
                title="野餐垫真香",
                content="去野餐带了这块垫子，防潮效果不错。",
                author_name="观隅好物",
                published=True,
                note_type="synced",
                raw_json={"xsec_token": "TOKEN-A", "publish_time": 1784082313000},
            )
            db.add(note)
            db.flush()
            db.add(NoteAsset(note_id=note.id, asset_type="image", url="http://ci.example/note-a.jpg", sort_order=0))
            product = db.scalar(select(ShopProduct).where(ShopProduct.item_id == "ITEM-1"))
            # §26.7：关系表按平台 ID 落（笔记平台 ID ↔ 商品平台 ID）
            db.add(
                NoteGoods(user_id=note.user_id, note_id=note.note_id, item_id=product.item_id)
            )
            # 平台证据缓存一行（覆盖率的计数口径：synced_notes 数的是它）
            store_entry(
                db,
                account.user_id,
                account.id,
                existing_rows(db, account.id),
                {
                    "note_id": "NOTE-A",
                    "note_title": "野餐垫真香",
                    "note_image": "http://ci.example/note-a.jpg",
                    "xsec_token": "TOKEN-A",
                    "publish_time": 1784082313000,
                    "browse_num": 286,
                    "like_num": 12,
                    "item_id": "ITEM-1",
                    "item_title": "外出野餐垫防潮垫",
                    "variant_desc": "40*60CM",
                    "goods_infos": [{"item_id": "ITEM-1", "item_title": "外出野餐垫防潮垫"}],
                },
            )
            db.commit()
        finally:
            db.close()

        before = dict(FakeArkAdapter.calls)
        payload = client.post(
            "/api/xhs/qianfan/item-notes",
            json={"platform_account_id": ids["owned"], "item_id": "ITEM-1"},
            headers=headers,
        ).json()
        assert payload["count"] == 1
        note = payload["notes"][0]
        assert note["note_id"] == "NOTE-A"
        assert note["title"] == "野餐垫真香"
        assert note["content"].startswith("去野餐带了这块垫子")
        assert note["note_type"] == "synced"
        # 发布状态（设计 13 §32.3）：**所有笔记都有值**了（原来是 `""` 不适用态）。
        # 这行是测试直接构造的本地行（没标过 published）⇒ 待发布。
        assert note["publish_status"] == "pending"
        assert note["note_url"].startswith("https://www.xiaohongshu.com/explore/NOTE-A")
        assert "TOKEN-A" in note["note_url"]
        assert note["images"] == ["http://ci.example/note-a.jpg"]
        # 覆盖率：本地只映射了同步过的那部分，必须报出来
        assert payload["synced_notes"] == 1 and payload["goods_note_total"] == 0
        # **零平台请求**：调用计数与查询前完全一致
        assert FakeArkAdapter.calls == before

        # 没挂链的商品 → 0 条（而不是报错）
        empty = client.post(
            "/api/xhs/qianfan/item-notes",
            json={"platform_account_id": ids["owned"], "item_id": "ITEM-NONE"},
            headers=headers,
        ).json()
        assert empty["count"] == 0 and empty["notes"] == []
    finally:
        _teardown()


def test_item_notes_needs_no_ark_session_but_enforces_ownership(tmp_path):
    """`/item-notes` 本地读**零门禁**，但**越权仍然 404**（两件事要分开）。

    ⚠️ **被取代的旧断言（原句保留）**：本用例原名
    `test_item_notes_requires_binding_and_ownership`，断言 *「未绑定 → 400
    `QIANFAN_BINDING_REQUIRED`」*。ADR-021 + 用户 2026-09-30 口径反转：
    本接口纯本地（`note_goods` join `notes`，零平台请求）⇒ 无 ark 会话也返回 200。

    ⚠️ **归属闸不放松**：`platform_account_id` 是隔离键（ADR-021）——
    拿别人的账号 id 仍必须 404，否则「本地读免门禁」就变成了越权口子。
    """
    headers, intruder_headers, ids, _ = _setup(tmp_path)
    try:
        unbound = client.post(
            "/api/xhs/qianfan/item-notes",
            json={"platform_account_id": ids["owned"], "item_id": "ITEM-1"},
            headers=headers,
        )
        assert unbound.status_code == 200, unbound.text
        assert unbound.json()["count"] == 0, "没落过关系 ⇒ 空结果（不是错误）"

        _bind(headers, ids["owned"])
        assert (
            client.post(
                "/api/xhs/qianfan/item-notes",
                json={"platform_account_id": ids["owned"], "item_id": "ITEM-1"},
                headers=intruder_headers,
            ).status_code
            == 404
        )
    finally:
        _teardown()


def test_sync_goods_endpoint_is_gone(tmp_path):
    """`POST /xhs/qianfan/sync-goods` 已移除（v1.32，设计 13 §30.2）⇒ 404。"""
    headers, _, ids, _ = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        response = client.post(
            "/api/xhs/qianfan/sync-goods",
            json={"platform_account_id": ids["owned"], "max_pages": 1, "page_size": 50},
            headers=headers,
        )
        assert response.status_code == 404, "端点必须真的不存在（不是 200 空转）"
    finally:
        _teardown()


def test_sdk_builds_ark_body_exactly(monkeypatch):
    """SDK 层的单测：**body 形状**（`card_type` 缺失语义、分换算、分页钳制）。

    同步商品任务仍走这个 SDK（本地列表的数据来源），契约不能因列表本地化而松。
    """
    from apis.xhs_qianfan_apis import XHS_Qianfan_Apis

    captured: dict = {}

    def fake_post(self, api, body, referer):
        captured["api"] = api
        captured["body"] = body
        captured["referer"] = referer
        return True, "ok", {"code": 200, "data": {"total": 1, "items": []}}

    monkeypatch.setattr(XHS_Qianfan_Apis, "_post", fake_post)
    api = XHS_Qianfan_Apis("a1=x")

    api.search_items(keyword="地垫", status="on_shelf", price_from_fen=10000, page_size=999)
    body = captured["body"]
    assert captured["api"] == "/api/edith/product/search_item_v2"
    assert body["search_filter"]["card_type"] == 2          # on_shelf
    assert body["search_filter"]["whole_keywords"] == "地垫"
    assert body["search_filter"]["price_from"] == 10000
    assert body["page_size"] == 100                          # 钳到上限
    assert body["search_order"] == {"sort_field": "create_time", "order": "desc"}

    api.search_items(status="all")
    assert "card_type" not in captured["body"]["search_filter"]   # 「全部」= 不传


# --------------------------------------------------------------------------- #
# 6. 挂链笔记数筛选（v1.34，**关系表口径**）
# --------------------------------------------------------------------------- #
def _link_notes(session_local, account_id: int, item_id: str, count: int) -> None:
    """给某个台账商品挂 `count` 篇本地笔记（= `link_note_goods` 的产物形状）。"""
    db = session_local()
    try:
        account = db.get(PlatformAccount, account_id)
        product = db.scalar(
            select(ShopProduct).where(
                ShopProduct.item_id == item_id,
                ShopProduct.platform_account_id == account.id,
            )
        )
        assert product is not None, item_id
        for index in range(count):
            note = Note(
                user_id=account.user_id,
                platform_account_id=account.id,
                platform="xhs",
                note_id=f"NOTE-{item_id}-{index}",
                title=f"{item_id} 的第 {index + 1} 篇",
                note_type="local_generated",
                raw_json={},
            )
            db.add(note)
            db.flush()
            db.add(
                NoteGoods(user_id=note.user_id, note_id=note.note_id, item_id=product.item_id)
            )
        db.commit()
    finally:
        db.close()


def test_items_filters_by_relation_note_count(tmp_path):
    """挂链笔记数筛选（设计 13 §33.8，用户口径「商品列表搜索条件添加挂链笔记数，
    默认小于指定篇数」）。

    口径必须是 `note_goods` **关系表** —— 与列表「挂链笔记」列显示的**同一个数**；
    按平台原文 `product_note_num` 筛会出现「列上写 0 篇、却按 1 篇筛」的自相矛盾界面
    （构造数据里三个商品的平台值都是 1，专门用来钉这条）。

    `note_count` 不传 ⇒ **不筛**（不是「小于 0」）：兜成 0 会让用户一进页面就
    只看到「一篇都没挂」的商品。
    """
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        _seed_products(
            SessionLocal,
            ids["owned"],
            [
                _ark_item("ITEM-0", "一篇都没挂"),
                _ark_item("ITEM-2", "挂两篇"),
                _ark_item("ITEM-5", "挂五篇"),
            ],
        )
        _link_notes(SessionLocal, ids["owned"], "ITEM-0", 0)
        _link_notes(SessionLocal, ids["owned"], "ITEM-2", 2)
        _link_notes(SessionLocal, ids["owned"], "ITEM-5", 5)

        listed = _items(headers, ids["owned"], status="all")["items"]
        # 显示口径 = 关系表；平台原文另存、不参与显示
        assert {i["item_id"]: i["note_count"] for i in listed} == {
            "ITEM-0": 0,
            "ITEM-2": 2,
            "ITEM-5": 5,
        }
        assert {i["item_id"]: i["platform_note_count"] for i in listed} == {
            "ITEM-0": 1,
            "ITEM-2": 1,
            "ITEM-5": 1,
        }

        def matched(**overrides) -> list[str]:
            payload = _items(headers, ids["owned"], status="all", **overrides)
            return sorted(i["item_id"] for i in payload["items"])

        # 不传篇数 / 只给比较符 ⇒ 不筛
        assert matched() == ["ITEM-0", "ITEM-2", "ITEM-5"]
        assert matched(note_count_op="lt") == ["ITEM-0", "ITEM-2", "ITEM-5"]
        # 默认比较符 = `lt`（小于）
        assert matched(note_count=2) == ["ITEM-0"]
        assert matched(note_count_op="lt", note_count=3) == ["ITEM-0", "ITEM-2"]
        assert matched(note_count_op="lte", note_count=2) == ["ITEM-0", "ITEM-2"]
        assert matched(note_count_op="eq", note_count=0) == ["ITEM-0"]
        assert matched(note_count_op="gte", note_count=2) == ["ITEM-2", "ITEM-5"]
        assert matched(note_count_op="gt", note_count=2) == ["ITEM-5"]
        # 总数与列表同一份筛选（分页口径不许各有各的 WHERE）
        assert _items(headers, ids["owned"], status="all", note_count=1)["total"] == 1
        assert _items(headers, ids["owned"], status="all", note_count_op="gte", note_count=2)["total"] == 2
    finally:
        _teardown()


def test_items_rejects_unknown_note_count_op(tmp_path):
    """未知比较符 → 400 + 支持列表（与 `status` 同一条纪律：不悄悄退化成默认值）。"""
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        response = client.post(
            "/api/xhs/qianfan/items",
            json={
                "platform_account_id": ids["owned"],
                "note_count_op": "whatever",
                "note_count": 1,
            },
            headers=headers,
        )
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "QIANFAN_NOTE_COUNT_OP_UNSUPPORTED"
        assert detail["supported"] == ["lt", "lte", "eq", "gte", "gt"]
    finally:
        _teardown()


# --------------------------------------------------------------------------- #
# 6. 批量删除（v1.36，设计 13 §33.10）+ 请求模型的未知字段硬闸
# --------------------------------------------------------------------------- #
def _delete(headers, account_id: int, item_ids: list[str]):
    return client.post(
        "/api/xhs/qianfan/items/delete",
        json={"platform_account_id": account_id, "item_ids": item_ids},
        headers=headers,
    )


def test_items_delete_removes_only_local_rows(tmp_path):
    """批量删除：**只删本账号台账行**，关系行跟着删（设计 13 §33.10 + §26.6）。

    四条一起钉住（少一条这个接口的语义就是错的）：
    1. **零平台请求** —— 千帆 SDK 根本没有商品删除接口，删的是本地行；
    2. **只删本账号** —— 同一用户**另一个账号**里的同 item_id 行必须活下来
       （WHERE 里 `platform_account_id` 与 `item_id` 是一对条件，不是只按 item_id）；
    3. **`note_goods` 关系行跟着删**（⚠️ 2026-09-23 用户口径「删除商品时也需要删除
       笔记与商品的关联关系」，**推翻** v1.36 的「关系行不动」）—— 但**只删指向
       被删商品的那些**：幸存商品的关系必须留下，另一个账号的同名商品关系也必须留下；
    4. **笔记本体不连坐** —— 删的是关系，不是笔记（笔记有它自己的删除链）。

    ⚠️ **被取代的旧口径（原句保留）**：本用例原来围绕「**同一用户的另一家店**」构造
    —— 原文 *「同一个用户另一家店里的同 item_id 行必须活下来（WHERE 里 `shop_id_fk`
    与 `item_id` 是一对条件…）」*。ADR-021（2026-09-30）店铺实体退场后
    「另一家店」**不再存在**，等价场景改为「**同一用户的另一个账号**」
    —— 收口维度从店铺换成账号，纪律（同 item_id 跨归属不许误删）一模一样。
    """
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        _seed_products(
            SessionLocal,
            ids["owned"],
            [
                _ark_item("ITEM-A", "A 商品"),
                _ark_item("ITEM-B", "B 商品"),
                _ark_item("ITEM-C", "C 商品"),
            ],
        )
        # ITEM-A 挂 2 篇、ITEM-C 挂 1 篇（**幸存商品**的关系必须留下）
        _link_notes(SessionLocal, ids["owned"], "ITEM-A", 2)
        _link_notes(SessionLocal, ids["owned"], "ITEM-C", 1)

        # 同一用户的**另一个账号**，里面也有一个 ITEM-B（同 item_id、不同账号），
        # 并且也挂 1 篇 —— 删本账号 ITEM-B 时，那个账号的关系**必须留下**。
        db = SessionLocal()
        try:
            owner = db.get(PlatformAccount, ids["owned"])
            other_shop = PlatformAccount(
                user_id=owner.user_id,
                platform="xhs",
                sub_type="creator",
                nickname="另一个号",
                seller_id="SELLER-OTHER",
                status="active",
            )
            db.add(other_shop)
            db.commit()
            other_shop_id = other_shop.id
            store_products(db, owner.user_id, other_shop_id, [_ark_item("ITEM-B", "B 商品")])
            db.commit()
        finally:
            db.close()
        _link_notes(SessionLocal, ids["owned"], "ITEM-B", 1)

        # 给**另一个账号**那个 ITEM-B 也挂一篇（`_link_notes` 只认本账号的台账，
        # 所以这里直接建行）—— 它是「指向另一个商品行」的关系，删本账号 ITEM-B 时
        # 必须留下，否则就是把无关数据一起删了。
        db = SessionLocal()
        try:
            owner = db.get(PlatformAccount, ids["owned"])
            other_product = db.scalar(
                select(ShopProduct).where(
                    ShopProduct.item_id == "ITEM-B",
                    ShopProduct.platform_account_id == other_shop_id,
                )
            )
            assert other_product is not None
            other_note = Note(
                user_id=owner.user_id,
                platform_account_id=owner.id,
                platform="xhs",
                note_id="NOTE-OTHER-SHOP-B",
                title="另一家店的挂载笔记",
                note_type="local_generated",
                raw_json={},
            )
            db.add(other_note)
            db.flush()
            db.add(
                NoteGoods(
                    user_id=other_note.user_id,
                    note_id=other_note.note_id,
                    item_id=other_product.item_id,
                )
            )
            db.commit()
        finally:
            db.close()

        FakeArkAdapter.calls = {}
        response = _delete(headers, ids["owned"], ["ITEM-A", "ITEM-B", "NOT-EXIST"])
        assert response.status_code == 200, response.text
        assert response.json() == {
            "deleted": 2,
            "requested": 3,
            "item_ids": ["ITEM-A", "ITEM-B", "NOT-EXIST"],
        }
        # `deleted` 是**实际**行数：NOT-EXIST 不存在 ⇒ 2 < 3（不假装全删成功）
        assert FakeArkAdapter.calls == {}, "批量删除必须零平台请求"

        left = [item["item_id"] for item in _items(headers, ids["owned"], status="all")["items"]]
        assert left == ["ITEM-C"]

        db = SessionLocal()
        try:
            kept_other_shop = db.scalar(
                select(func.count())
                .select_from(ShopProduct)
                .where(ShopProduct.platform_account_id == other_shop_id)
            )
            assert kept_other_shop == 1, "另一个账号里的同 item_id 行不许被删"

            # 只剩 1 条关系：ITEM-C 那 1 条。
            # ⚠️ 2026-09-27（§26.7）：关系表按**平台商品 ID** 挂、**不带归属维度**
            # ⇒ 「另一个账号里同名的 ITEM-B」的关系行**也会被删**（关系回答的是
            # 「这条笔记挂了哪个平台商品」，与它属哪个账号无关）。本用例里两个账号
            # 同属一个用户、item_id 撞名是刻意构造的退化场景：**商品行**（上面那条
            # `kept_other_shop == 1`）才是按账号收口的，关系行不是。
            assert db.scalar(select(func.count()).select_from(NoteGoods)) == 1, (
                "关系行按 item_id 删（ITEM-A / ITEM-B 的全部关系）；ITEM-C 的必须留下"
            )
            # 笔记本体不连坐（删的是关系不是笔记）：ITEM-A 2 篇 + ITEM-B 1 篇
            # + ITEM-C 1 篇 + 另一个账号 1 篇 = 5 篇都该活着
            assert db.scalar(select(func.count()).select_from(Note)) == 5, (
                "批量删除商品不该删笔记（5 篇挂载笔记都该活着）"
            )
        finally:
            db.close()
    finally:
        _teardown()


def test_items_delete_rejects_empty_and_oversized(tmp_path):
    """批量删除的三道门：空 / 全空白 → 400；超上限 → 400（**不静默截断**）；越权 → 404。

    ⚠️ **被取代的旧断言（原句保留）**：原 docstring 写 *「批量删除的四道门：
    空 / 全空白 → 400；超上限 → 400（不静默截断）；**未绑店 → 400**；越权 → 404」*。
    第四道「未绑店 400」随 ADR-021 退场（本地删零门禁，用户 2026-09-30 口径），
    改为「无 ark 会话 ⇒ 也能删本地行（200，删 0 行）」。
    """
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])

        for empty in ([], ["  "], ["", "\t"]):
            response = _delete(headers, ids["owned"], empty)
            assert response.status_code == 400, response.text
            assert response.json()["detail"]["code"] == "QIANFAN_ITEM_IDS_REQUIRED"

        oversized = [f"I-{index}" for index in range(MAX_BATCH_DELETE + 1)]
        response = _delete(headers, ids["owned"], oversized)
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "QIANFAN_ITEM_IDS_TOO_MANY"
        assert detail["limit"] == MAX_BATCH_DELETE, "要回上限值，客户端才能说清「超了多少」"

        #: 自己的号但没 ark 会话（ADR-021：本地删零门禁）⇒ 也能删本地行，
        #: 只是没同步过 ⇒ 删 0 行（**不是** 400）。
        response = _delete(headers, ids["owned_unbound"], ["ITEM-A"])
        assert response.status_code == 200, response.text
        assert response.json()["deleted"] == 0, "没同步过 ⇒ 没有行可删"

        # 别人的账号 → 404（越权与「本地有没有数据」是两回事）
        response = _delete(headers, ids["foreign"], ["ITEM-A"])
        assert response.status_code == 404, response.text
    finally:
        _teardown()


def test_shop_items_request_forbids_unknown_fields(tmp_path):
    """`ShopItemsRequest` 的 `extra="forbid"`（v1.36）。

    **这条是拿真实事故换来的**：挂链笔记数筛选上线时，跑着的后端还是旧进程
    （模型里没有 `note_count`），Pydantic 默认把未知字段**静默丢掉** ⇒ 用户填了
    条件、后端当没收到，界面表现为「筛选没生效」而**没有任何报错**，排查只能去
    比对进程年龄。删掉这个语义漏洞：新旧不匹配必须在第一跳炸出声（422），
    与「不支持的取值静默退化成默认 = 红线」同一条纪律。
    """
    headers, _, ids, SessionLocal = _setup(tmp_path)
    try:
        _bind(headers, ids["owned"])
        response = client.post(
            "/api/xhs/qianfan/items",
            json={"platform_account_id": ids["owned"], "note_counts": 3},
            headers=headers,
        )
        assert response.status_code == 422, response.text
        assert any(
            error.get("type") == "extra_forbidden"
            for error in response.json()["detail"]
        ), response.text
        # 反面对照：已知字段照常 200（`extra="forbid"` 不是把正常请求也挡了）
        assert _items(headers, ids["owned"], status="all", note_count=1)["total"] == 0
    finally:
        _teardown()
