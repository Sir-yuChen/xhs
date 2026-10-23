"""店铺商品同步（T-805 / §19）的后端契约测试。

盯的是这条链路的三条纪律 + 一套风控 + 内联详情补齐：

1. **完整同步才删旧行**：翻完最后一页（或存够 `total`）才删本次未出现的行；
   半途而废（到页数上限 / 被限流 / 翻页失败）**一行不删**——删 = 把没扫到的商品
   当已下架丢掉；
2. **解不出店铺就不同步**：`shop_id_fk` 是 NOT NULL，拿不到就 `failed`，
   **不编店铺**（缺值口径）；
3. **凭据失效不清数据**：`CREDENTIAL_ERROR_CODES`（`-13000` 缺失 / `-13004` 已过期）→ 绑定标 `invalid` + 保留全部已有商品；
4. **风控**：有界翻页、按账号限流（超限停本次、已同步的保留）、页间停顿；
5. **缺值不造假**：`item_id` 缺失的条目跳过；价格取不到留空串；状态取不到留空；
6. **§19.4 内联详情补齐**：每页列表商品落库前**先拉 SKU 明细**（批量 10），
   列表数据 + 规格详情一次性 upsert——白名单归一化（raw 不透传）、批量硬上限 10、
   详情失败不阻塞列表（失败商品留空，下轮重试）。
"""
from __future__ import annotations

import ast
import inspect
import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text as sa_text
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import Base, get_db
from backend.app.core.time import shanghai_now
from backend.app.main import app
from backend.app.models import PlatformAccount, Task, User
from backend.app.models.shop_product import ShopProduct
from backend.app.services import shop_goods_sync_service as sgs
from backend.app.services import account_task_service as ats
from backend.app.services.shop_binding_service import get_shop_state
from tests.backend.cookie_fixtures import bind_shop_account, give_ark_cookies
from tests.backend.conftest import patch_service_sessions

import pytest


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration
client = TestClient(app)

ARK_COOKIES = "access-token-ark.xiaohongshu.com=abc123"


class AllowLimiter:
    def allow(self, account_id: int) -> bool:  # noqa: ARG002
        return True

    def wait_allow(self, account_id: int, **kwargs) -> bool:  # noqa: ARG002
        """全量翻页用的是**等待制**（设计 13 §21.3）：测试里立即放行、不真等待。"""
        return True


class DenyLimiter:
    """额度**永远**没有：`wait_allow` 立即超时（不真等待）= 模拟「等到超时仍无额度」。"""

    def allow(self, account_id: int) -> bool:  # noqa: ARG002
        return False

    def wait_allow(self, account_id: int, **kwargs) -> bool:  # noqa: ARG002
        return False


LIMITER: dict = {"impl": AllowLimiter()}


class FakeItemsAdapter:
    """千帆商品列表假实现：按页脚本回应，并记录每次调用的参数。"""

    pages: list = []
    calls: list = []
    #: §19：SKU 明细假实现按 item_id 回应；不在表里 = 平台没返回这个商品。
    skus_map: dict = {}
    #: 记录每次 `get_skus_by_item_ids` 收到的批量（用来钉 ≤10）。
    sku_batches: list = []
    #: 置 True 时详情一律失败（用来钉「详情失败不阻塞列表」）。
    sku_fail: bool = False

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def search_items(self, **kwargs):
        type(self).calls.append(kwargs)
        index = len(type(self).calls) - 1
        if index >= len(type(self).pages):
            return True, "", {"code": 0, "data": {"items": [], "total": 0, "has_more": False}}
        items, total, has_more = type(self).pages[index]
        return True, "", {
            "code": 0,
            "data": {"items": items, "total": total, "has_more": has_more},
        }

    def get_skus_by_item_ids(self, item_ids):
        batch = list(item_ids)
        type(self).sku_batches.append(batch)
        if type(self).sku_fail:
            return False, "商品详情获取失败", {"code": 500, "success": False}
        skus_map = {
            item_id: type(self).skus_map[item_id]
            for item_id in batch
            if item_id in type(self).skus_map
        }
        return True, "", {"code": 200, "data": {"skusMap": skus_map}}


class InvalidCredentialAdapter:
    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def search_items(self, **kwargs):
        return False, "登录凭证缺失，请重新登录", {"code": -13000, "success": False}


class ExpiredCredentialAdapter:
    """令牌**已过期**（`code=-13004`）—— 真机实测的 msg 就是下面这句。

    ⚠️ 与 `InvalidCredentialAdapter` 是**同一类事实**（凭据问题），必须得到同一个结局
    （DEF-084，2026-09-30）。
    """

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def search_items(self, **kwargs):
        return False, "登录凭证已过期, 请重新登录", {"code": -13004, "success": False}


def _item(
    item_id: str,
    *,
    title: str = "商品",
    min_price=1999,
    max_price=1999,
    stock=10,
    sales=5,
    card_type=2,
    categories=None,
    images=None,
    article_no="1056576341942",
    on_shelf_time=1784084996000,
    item_audit_time=1784089999000,
    sale_qty30=3,
) -> dict:
    return {
        "item_id": item_id,
        "item_name": title,
        "min_price": min_price,
        "max_price": max_price,
        "total_stock": stock,
        "sale_qty_info": {"acc_sale_qty": sales, "sale_qty30": sale_qty30},
        "card_type": card_type,
        "categories": (
            categories
            if categories is not None
            else [{"name": "家居", "level": 1}, {"name": "地垫", "level": 2}]
        ),
        "images": images if images is not None else [{"link": f"http://img.example/{item_id}.jpg"}],
        # v1.32（§30.3）：筛选 / 排序九列的来源字段（真实响应形状，2026-09-22 核对）。
        "article_no": article_no,
        "create_time": 1784082313000,
        "on_shelf_time": on_shelf_time,
        "item_audit_time": item_audit_time,
    }


def _setup(tmp_path, monkeypatch, *, with_binding: bool = True, shop_id_fk: bool = True):
    """建库 + 注册用户 + 一个账号。

    ⚠️ `shop_id_fk` 形参**已失去物理意义**（ADR-021：店铺实体退场，台账归属键就是
    账号主键），保留只为不改众多调用点。它现在只控制**是否导入 ark 登录态**：
    `shop_id_fk=False` ⇒ 账号「没凭据」（`ARK_SESSION_REQUIRED`），
    这是旧的「有凭据但没绑店」状态在现行模型下的**唯一等价物** ——
    因为「没绑店」这个状态已不存在（凭据即能力，隔离键 ≠ 能力判据）。
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'shop-goods-sync.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(sgs, "SessionLocal", SessionLocal)
    # ⚠️ 端点链路还会走到 `scheduled_job_runner` / `sync_task_service` 等模块，它们各自
    # 持有模块级 SessionLocal —— 只 patch `sgs` 的话这些模块**照样写生产库**
    # （2026-09-27 真实发生过）。按 AST 扫出的全量清单逐一打桩。
    patch_service_sessions(monkeypatch, SessionLocal)
    monkeypatch.setattr(sgs, "PAGE_GAP_SECONDS", 0.0)
    monkeypatch.setattr(sgs, "get_rate_limiter", lambda: LIMITER["impl"])

    FakeItemsAdapter.pages = []
    FakeItemsAdapter.calls = []
    FakeItemsAdapter.skus_map = {}
    FakeItemsAdapter.sku_batches = []
    FakeItemsAdapter.sku_fail = False
    LIMITER["impl"] = AllowLimiter()

    response = client.post(
        "/api/auth/register", json={"username": "owner", "password": "secret123"}
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    db = SessionLocal()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "owner"))
        account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="creator", nickname="我的号"
        )
        db.add(account)
        db.flush()
        if with_binding:
            #: ADR-021：开通千帆能力 = 给账号导入 `ark` 登录态（不再有「绑店」动作）。
            #: `shop_id_fk=False` 复现「没有凭据」：同步必须失败得可读。
            if shop_id_fk:
                bind_shop_account(db, account, cookies_text=ARK_COOKIES, shop_name="观隅好物的店")
            else:
                give_ark_cookies(db, account, ARK_COOKIES)
        db.commit()
        account_id = account.id
        user_id_out = user_id
    finally:
        db.close()
    return headers, account_id, user_id_out, SessionLocal


def _teardown() -> None:
    app.dependency_overrides.clear()


def _seed_product(SessionLocal, shop_id_fk: int, item_id: str, user_id: int = 1) -> None:
    """落一行台账。形参名保留（调用点众多），实参现在传的是**账号主键**。"""
    db = SessionLocal()
    try:
        db.add(
            ShopProduct(
                user_id=user_id,
                platform_account_id=shop_id_fk,
                item_id=item_id,
                item_title="旧的",
                synced_at=shanghai_now(),
            )
        )
        db.commit()
    finally:
        db.close()


def _shop_pk(SessionLocal) -> int:
    """账号主键（ADR-021：台账归属键）。原名保留以免大量调用点改签名。"""
    db = SessionLocal()
    try:
        return int(db.scalar(select(PlatformAccount.id)))
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 归一化：千帆条目 → shop_products
# --------------------------------------------------------------------------- #
def test_items_are_normalized_into_product_rows(tmp_path, monkeypatch):
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [([_item("I1", title="编织地垫", min_price=1999, max_price=2999)], 1, False)]

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3, adapter_factory=FakeItemsAdapter
    )

    assert result["status"] == sgs.sts.STATUS_DONE, result
    assert result["stored"] == 1

    db = SessionLocal()
    try:
        product = db.scalars(select(ShopProduct).where(ShopProduct.item_id == "I1")).one()
        assert product.item_title == "编织地垫"
        # 价格是**分**的字符串：同价给单值，区间给 `low~high`（展示层换算成元）。
        assert product.price == "1999~2999"
        assert product.stock == 10
        assert product.sales == 5
        assert product.category == "家居 / 地垫"
        assert json.loads(product.images_json) == ["http://img.example/I1.jpg"]
        assert product.status == "售卖中"
        # 整条原文留底（字段集还会变，真值留本地）。
        assert json.loads(product.raw_json)["item_id"] == "I1"
    finally:
        db.close()


def test_sync_asks_for_all_statuses_and_stays_on_one_page_at_a_time(tmp_path, monkeypatch):
    """`status="all"` 同步全部状态；一次只翻一页（page_no 递增），不并发不错页。"""
    _headers, account_id, user_id, _ = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [([_item("I1")], 2, True), ([_item("I2")], 2, False)]

    sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3, adapter_factory=FakeItemsAdapter
    )

    assert [call["status"] for call in FakeItemsAdapter.calls] == ["all", "all"]
    assert [call["page_no"] for call in FakeItemsAdapter.calls] == [1, 2]


def test_sync_defaults_to_all_statuses_when_status_is_omitted(tmp_path, monkeypatch):
    """v2.13（设计 13 §30.3.2）：**默认同步全部状态**。

    2026-10-02 用户口径「商品同步更新需求为同步全部状态的商品」。判据是**平台请求
    里的 `status`**，不是「函数签名上有没有默认值」—— 台账是列表页的唯一数据源，
    少了已下架 / 已冻结，状态筛选与批量删除的连带判断会静默失真。
    """
    _headers, account_id, user_id, _ = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [([_item("I1")], 1, False)]

    sgs.run_shop_goods_sync(account_id, user_id=user_id, max_pages=1, adapter_factory=FakeItemsAdapter)

    assert [call["status"] for call in FakeItemsAdapter.calls] == ["all"]


def test_sync_can_be_narrowed_to_one_status(tmp_path, monkeypatch):
    """显式传状态键 ⇒ 只同步那一种（与平台页面 7 个 tab 同源）。"""
    _headers, account_id, user_id, _ = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [([_item("I1")], 1, False)]

    sgs.run_shop_goods_sync(
        account_id,
        user_id=user_id,
        max_pages=1,
        adapter_factory=FakeItemsAdapter,
        item_status="off_shelf",
    )

    assert [call["status"] for call in FakeItemsAdapter.calls] == ["off_shelf"]


def test_bad_item_status_falls_back_to_all_not_to_a_narrower_scope(tmp_path, monkeypatch):
    """坏值 ⇒ `all`，**不是**退成某个具体状态。

    把拼写错误退成「只同步售卖中」= 一次手滑让台账静默少掉已下架 / 已冻结的商品
    （与 `normalize_mode` 同一条纪律：坏值退回**信息最全**的取值）。
    """
    _headers, account_id, user_id, _ = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [([_item("I1")], 1, False)]

    sgs.run_shop_goods_sync(
        account_id,
        user_id=user_id,
        max_pages=1,
        adapter_factory=FakeItemsAdapter,
        item_status="sold_out_typo",
    )

    assert [call["status"] for call in FakeItemsAdapter.calls] == ["all"]


def test_qianfan_request_models_all_forbid_unknown_fields():
    """qianfan 路由**每个**请求模型都必须 `extra="forbid"`（v2.13 补齐 6 处）。

    **这条是拿真实事故换来的**（REF §B）：Pydantic 默认 `extra="ignore"` 把未知字段
    **静默丢弃**（不报错、不写日志）。「挂链笔记数筛选未生效」那次，代码从头就是对的，
    错在跑着的后端是旧进程 —— 它的模型里没有新字段，参数被静默吞掉，表现与「代码写错」
    完全一样，排查比的是进程年龄。加 forbid ⇒ 新旧不匹配在第一跳就 422 炸出声。

    ⚠️ 断言的是**全部**模型（枚举本模块的 `BaseModel` 子类），不是只挑 `ShopItemsRequest`
    —— 只测一个的话，下次新增模型漏加 forbid 照样静默。
    """
    import importlib
    import inspect
    import pkgutil

    from pydantic import BaseModel

    from backend.app.api.platforms import xhs as xhs_pkg

    qf = importlib.import_module("backend.app.api.platforms.xhs.qianfan")

    #: ⚠️ **必须遍历整包**（2026-10-11 拆包后修）：原来写死 `vars(qf)` +
    #: `obj.__module__ == qf.__name__`，`qianfan` 变包之后模型全在子模块里
    #: （`__module__` 是 `...qianfan.goods` 之类）⇒ 枚举结果**空集**，
    #: 守卫自己就先炸「没找到请求模型」。改成扫包内全部子模块。
    models: dict[str, type[BaseModel]] = {}
    for info in pkgutil.iter_modules(qf.__path__):
        sub = importlib.import_module(f"{qf.__name__}.{info.name}")
        for name, obj in vars(sub).items():
            if (
                inspect.isclass(obj)
                and issubclass(obj, BaseModel)
                and obj is not BaseModel
                and obj.__module__ == sub.__name__
                # 响应模型（只出不进）不受这条约束：未知字段不会被客户端传进来。
                and not name.endswith(("Response", "Result"))
            ):
                models[name] = obj
    assert models, "没找到请求模型 —— 守卫自己失效了"
    missing = [
        name for name, cls in models.items() if cls.model_config.get("extra") != "forbid"
    ]
    assert not missing, f"这些请求模型缺 extra=\"forbid\"：{missing}"


def test_items_without_id_are_skipped_not_counted(tmp_path, monkeypatch):
    """没有 `item_id` 的条目跳过，且**不计入 stored**（没有主键就没有行的身份）。"""
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [([_item(""), _item("I1")], 1, False)]

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=1, adapter_factory=FakeItemsAdapter
    )

    assert result["stored"] == 1  # 无主键的条目不算成功
    db = SessionLocal()
    try:
        assert {row.item_id for row in db.scalars(select(ShopProduct)).all()} == {"I1"}
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# §19.4：SKU 详情补齐（第二段）
# --------------------------------------------------------------------------- #
def _sku(sku_id: str, *, price: int = 21152, stock=None) -> dict:
    """一条 SKU 明细（形状抄自 2026-09-21 真机抓包的 `get_skus_by_item_ids`）。

    刻意带一堆**白名单外**字段（`boutiqueInfo` / `logisticsInfo` / `faqs`）——
    归一化必须把它们挡在 `detail_json` 之外（10 个商品 5.4MB，不透传 raw）。
    """
    return {
        "skuId": sku_id,
        "skuName": f"商品 {sku_id}",
        "variantName": "金逸风华",
        "barcode": f"XHS-{sku_id}",
        "erpCode": f"tb_{sku_id}",
        "basePriceInfo": {"price": price, "originalPrice": 22620, "taxType": 0},
        "baseStockInfos": [{"stock": stock}] if stock is not None else None,
        "specImage": {"link": f"https://qimg.example/{sku_id}.jpg"},
        "weight": 0,
        "buyable": True,
        "freeze": False,
        # —— 以下都是白名单外字段 ——
        "boutiqueInfo": {"isBoutique": False},
        "logisticsInfo": {"legal": True, "logistics": "auto"},
        "faqs": [],
        "skuDetails": [],
    }


def test_sku_details_are_normalized_with_whitelist(tmp_path, monkeypatch):
    """SKU 明细按白名单归一化：raw 字段不进 `detail_json`（不透传 5.4MB）。"""
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [([_item("I1")], 1, False)]
    FakeItemsAdapter.skus_map = {"I1": [_sku("S1", price=21152, stock=7)]}

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3,
        adapter_factory=FakeItemsAdapter,
    )

    assert result["status"] == sgs.sts.STATUS_DONE, result
    assert result["detail_done"] == 1

    db = SessionLocal()
    try:
        product = db.scalars(select(ShopProduct).where(ShopProduct.item_id == "I1")).one()
        details = json.loads(product.detail_json)
        assert len(details) == 1
        entry = details[0]
        # 白名单内的值都在
        assert entry["sku_id"] == "S1"
        assert entry["sku_name"] == "商品 S1"
        assert entry["variant_name"] == "金逸风华"
        assert entry["barcode"] == "XHS-S1"
        assert entry["price_fen"] == 21152
        assert entry["original_price_fen"] == 22620
        assert entry["stock"] == 7
        assert entry["spec_image"] == "https://qimg.example/S1.jpg"
        assert entry["buyable"] is True and entry["freeze"] is False
        # 白名单外字段一个都不在
        assert set(entry) == {
            "sku_id", "sku_name", "variant_name", "barcode", "erp_code",
            "price_fen", "original_price_fen", "stock", "spec_image", "weight",
            "buyable", "freeze",
        }
        assert product.sku_count == 1
        assert product.detail_synced_at is not None
    finally:
        db.close()


def test_sku_missing_stock_stays_null_not_zero(tmp_path, monkeypatch):
    """SKU 响应里没有库存字段（实测常态）⇒ 留 null，不造 0（硬约束⑤）。"""
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [([_item("I1")], 1, False)]
    FakeItemsAdapter.skus_map = {"I1": [_sku("S1")]}  # baseStockInfos=None

    sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3, adapter_factory=FakeItemsAdapter
    )

    db = SessionLocal()
    try:
        product = db.scalars(select(ShopProduct).where(ShopProduct.item_id == "I1")).one()
        assert json.loads(product.detail_json)[0]["stock"] is None
        assert product.sku_count == 1  # 有 SKU 行但库存未知 ≠ 没补过
    finally:
        db.close()


def test_sku_batches_never_exceed_ten(tmp_path, monkeypatch):
    """批量硬上限 10（实测 20 个被服务端 500 挡回）：12 个商品 ⇒ 2 批。"""
    _headers, account_id, user_id, _ = _setup(tmp_path, monkeypatch)
    items = [_item(f"I{index:02d}") for index in range(12)]
    FakeItemsAdapter.pages = [(items, 12, False)]
    FakeItemsAdapter.skus_map = {f"I{index:02d}": [_sku(f"S{index}")] for index in range(12)}

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3,
        adapter_factory=FakeItemsAdapter,
    )

    assert result["detail_done"] == 12
    assert [len(batch) for batch in FakeItemsAdapter.sku_batches] == [10, 2]


def test_sku_not_returned_stays_empty(tmp_path, monkeypatch):
    """平台没返回某商品的 SKU（可能刚删除）⇒ detail_json 留空，detail_synced_at 留 NULL。"""
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [([_item("I1")], 1, False)]
    FakeItemsAdapter.skus_map = {}  # 平台没返回 I1 的 SKU

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3,
        adapter_factory=FakeItemsAdapter,
    )

    assert result["detail_done"] == 0
    db = SessionLocal()
    try:
        product = db.scalars(select(ShopProduct).where(ShopProduct.item_id == "I1")).one()
        # 空串 = 没补过（与「补过但 0 SKU」的 `[]` 是两件事）。
        assert product.detail_json == ""
        assert product.detail_synced_at is None
        assert product.sku_count is None
    finally:
        db.close()


def test_detail_total_reports_remaining_after_sync(tmp_path, monkeypatch):
    """`detail_total` = 全店还剩多少商品没详情（增量同步可能只覆盖到新商品）。"""
    _headers, account_id, user_id, _ = _setup(tmp_path, monkeypatch)
    items = [_item(f"I{index}") for index in range(5)]
    FakeItemsAdapter.pages = [(items, 5, False)]
    FakeItemsAdapter.skus_map = {f"I{index}": [_sku(f"S{index}")] for index in range(5)}

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3,
        adapter_factory=FakeItemsAdapter,
    )

    # 5 个全补上了
    assert result["detail_done"] == 5
    assert result["detail_total"] == 0


def test_sku_fetch_reports_progress_per_batch(tmp_path, monkeypatch):
    """SKU 明细**逐批**回调进度（每批 10 个 ≈ 一次限流等待 ≈ 7.5 秒）。

    2026-09-28 用户口径「进度条慢慢增加」：不接回调的话一页 50 个商品要 40 秒才动一格，
    用户看到的还是「不动」；接上之后页内也在一格一格涨。
    """
    batches: list[list[str]] = []

    class _StubAdapter:
        def get_skus_by_item_ids(self, batch):
            batches.append(list(batch))
            return True, "", {"data": {"skusMap": {i: [_sku("S")] for i in batch}}}

    class _Account:
        id = 7

    seen: list[int] = []
    out = sgs._fetch_page_skus(
        _Account(),
        _StubAdapter(),
        item_ids=[f"I{index}" for index in range(25)],
        limiter=AllowLimiter(),
        on_progress=seen.append,
    )

    assert [len(b) for b in batches] == [10, 10, 5], f"分批不对：{[len(b) for b in batches]}"
    assert seen == [10, 20, 25], f"每批都要回调（累计已尝试数），实得 {seen}"
    assert out["done"] == 25


def test_progress_is_task_scoped_and_monotonic(tmp_path, monkeypatch):
    """2026-09-28 用户口径：进度条**慢慢增加**且**绝不 0→100 反复跳**（设计 13 §42.4）。

    判据：任务行 `payload.progress` 必须带 `processed` / `work_total`（进度条看的那一对），
    且 `processed ≤ work_total`、百分比走这一对 —— 而不是「一页内 0→100」的
    `detail_done / detail_total`。
    """
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    items = [_item(f"I{index}") for index in range(5)]
    FakeItemsAdapter.pages = [(items, 5, False)]
    FakeItemsAdapter.skus_map = {f"I{index}": [_sku(f"S{index}")] for index in range(5)}

    sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3, adapter_factory=FakeItemsAdapter
    )

    db = SessionLocal()
    try:
        task = db.scalars(
            select(Task)
            .where(Task.task_type == sgs.sts.SHOP_GOODS_SYNC_TASK_TYPE)
            .order_by(Task.id.desc())
        ).first()
        progress = sgs.sts.task_payload(task)["progress"]
    finally:
        db.close()

    processed = int(progress.get("processed") or 0)
    work_total = int(progress.get("work_total") or 0)
    assert work_total == 5, f"预计工作量 = 平台报告的商品总数：{progress}"
    assert processed == 5, f"5 个商品都处理过（SKU 批次尝试过）：{progress}"
    assert processed <= work_total, f"已处理数不得超过预计工作量：{progress}"
    assert sgs.sts.progress_percent(progress) == 100, f"跑完就是 100%：{progress}"


def test_bounded_progress_denominator_is_the_platform_total(tmp_path, monkeypatch):
    """有界（增量 / 时间区间）同步的分母 = **平台按同一条查询条件报出的 `total`**。

    2026-09-28 用户报障（设计 13 §43 的 D2）：「任务进度列的进度条**自动达到了 100%**
    但是此时**任务刚刚开始**」。真库实测 `task=82`：`{"stored":100,"total":713,
    "processed":100,"work_total":100}` —— 窗口内还有 600+ 件没处理，进度条已经 100%。

    根因是**分母与分子同源**：旧实现在有界分支取 `work_total = new_count`（本轮范围内
    枚举数），而分子 `processed` 数的正是**同一批商品**（本页枚举出来、立刻被 SKU 批次
    尝试的那批）⇒ 每个页边界都恰好 `processed == work_total` ⇒ 恒 100%。

    判据（本用例的锚点）：平台报 `total=10`、只跑 1 页（本页 5 件）⇒ 分母必须是 **10**
    （= 本次查询条件下的条数），百分比 **50%** —— 旧实现会给 5 ⇒ 100%。
    """
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    items = [_item(f"I{index}") for index in range(5)]
    # 平台报告：本条查询条件（时间窗口）下共 10 件；本页只返回 5 件且还有下一页。
    FakeItemsAdapter.pages = [(items, 10, True)]
    FakeItemsAdapter.skus_map = {f"I{index}": [_sku(f"S{index}")] for index in range(5)}

    sgs.run_shop_goods_sync(
        account_id,
        user_id=user_id,
        max_pages=1,
        adapter_factory=FakeItemsAdapter,
        # 时间区间（上界足够大 ⇒ 本夹具 5 件全在窗口内）⇒ 走**有界**分支。
        time_from_ms=0,
        time_to_ms=2**50,
    )

    db = SessionLocal()
    try:
        task = db.scalars(
            select(Task)
            .where(Task.task_type == sgs.sts.SHOP_GOODS_SYNC_TASK_TYPE)
            .order_by(Task.id.desc())
        ).first()
        progress = sgs.sts.task_payload(task)["progress"]
    finally:
        db.close()

    assert progress.get("work_total") == 10, (
        f"有界同步的分母 = 平台报告的本条查询条件下的条数（不是本页枚举数）：{progress}"
    )
    assert int(progress.get("processed") or 0) == 5, f"本页 5 件都处理过：{progress}"
    assert sgs.sts.progress_percent(progress) == 50, (
        f"跑了 1 页（5/10）就该是 50%，而不是同源分母算出来的 100%：{progress}"
    )


def test_detail_failure_keeps_products_and_says_so(tmp_path, monkeypatch):
    """详情拉取失败 ⇒ 列表照常落库、detail 留空、原因写进 note（不阻塞）。"""
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [([_item("I1")], 1, False)]
    FakeItemsAdapter.sku_fail = True

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3, adapter_factory=FakeItemsAdapter
    )

    assert result["status"] == sgs.sts.STATUS_DONE, result
    assert result["stored"] == 1  # 列表没受影响
    assert result["detail_done"] == 0
    assert "详情获取失败" in result["note"]
    db = SessionLocal()
    try:
        product = db.scalars(select(ShopProduct).where(ShopProduct.item_id == "I1")).one()
        assert product.detail_json == ""  # 没补过就别写
    finally:
        db.close()


def test_sdk_truncates_item_ids_to_batch_size(monkeypatch):
    """SDK 层硬截断到 `SKU_BATCH_SIZE`（20 个会被服务端 500 挡回，实测）。"""
    from apis.xhs_qianfan_apis import SKU_BATCH_SIZE, XHS_Qianfan_Apis

    captured: dict = {}

    class FakeResponse:
        def json(self):
            return {"code": 200, "success": True, "msg": "ok", "data": {"skusMap": {}}}

    def fake_post(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["body"] = kwargs.get("json")
        return FakeResponse()

    # 打桩点跟着实现走：SDK 现在走**进程级共享会话**（`_session_request`），
    # 不再直接调 `requests.post`（设计 13 §18.18.8 —— 只借连接、不借凭据）。
    monkeypatch.setattr("apis.xhs_qianfan_apis._session_request", fake_post)
    ids = [f"{index:024x}" for index in range(15)]
    success, _message, _payload = XHS_Qianfan_Apis("web_session=x").get_skus_by_item_ids(ids)

    assert success is True
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/edith/product/get_skus_by_item_ids")
    assert SKU_BATCH_SIZE == 10
    assert len(captured["body"]["itemIds"]) == 10
    assert captured["body"]["itemIds"] == ids[:10]


def test_missing_price_and_status_are_left_empty(tmp_path, monkeypatch):
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [
        ([{"item_id": "I9", "item_name": "无价无状态", "card_type": 999}], 1, False)
    ]

    sgs.run_shop_goods_sync(account_id, user_id=user_id, max_pages=1, adapter_factory=FakeItemsAdapter)

    db = SessionLocal()
    try:
        row = db.scalars(select(ShopProduct).where(ShopProduct.item_id == "I9")).one()
        assert row.price == ""
        assert row.status == ""  # 未知 card_type 不猜标签
        assert row.stock is None and row.sales is None  # 缺值留空，不造 0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 完整同步才删旧行
# --------------------------------------------------------------------------- #
def test_complete_sync_drops_rows_that_did_not_come_back(tmp_path, monkeypatch):
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    shop_pk = _shop_pk(SessionLocal)
    _seed_product(SessionLocal, shop_pk, "STALE")
    FakeItemsAdapter.pages = [([_item("I1"), _item("I2")], 2, False)]

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3, adapter_factory=FakeItemsAdapter
    )

    assert result["removed"] == 1
    db = SessionLocal()
    try:
        ids = set(db.scalars(select(ShopProduct.item_id)).all())
        assert ids == {"I1", "I2"}
    finally:
        db.close()


def test_partial_sync_never_drops_rows(tmp_path, monkeypatch):
    """到页数上限 = 没翻完 → 一行都不许删。"""
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    shop_pk = _shop_pk(SessionLocal)
    _seed_product(SessionLocal, shop_pk, "STALE")
    # 第 1 页说还有更多，但 max_pages=1 到顶了。
    FakeItemsAdapter.pages = [([_item("I1")], 999, True)]

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=1, adapter_factory=FakeItemsAdapter
    )

    assert result["complete"] is False
    assert result["removed"] == 0
    assert "保留既有行" in result["note"]
    db = SessionLocal()
    try:
        ids = set(db.scalars(select(ShopProduct.item_id)).all())
        assert "STALE" in ids
    finally:
        db.close()


def test_incremental_empty_page_never_drops_rows(tmp_path, monkeypatch):
    """**增量追平后平台回空页（complete=True）也一行都不许删**（§27.5，2026-09-22）。

    增量下「本次未出现」的正确解释是「**没有新的商品**」，不是「商品都没了」——
    旧实现在这条路上会走到 `complete=True` + `stored_ids=∅` ⇒ 把整个店铺的商品
    清空。删除只允许发生在**全量、无时间窗**的同步里。
    """
    from backend.app.services.shop_binding_service import set_shop_state

    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    shop_pk = _shop_pk(SessionLocal)
    _seed_product(SessionLocal, shop_pk, "EXISTING")
    # 有同步历史 ⇒ auto 落成 incremental；平台一上来就回空页（没有新商品）。
    db = SessionLocal()
    try:
        set_shop_state(db.get(PlatformAccount, account_id), goods_cursor_ms=1784157704000)
        db.commit()
    finally:
        db.close()
    FakeItemsAdapter.pages = []

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3, adapter_factory=FakeItemsAdapter
    )

    assert result["complete"] is True, "空页 = 平台没有更新的商品（翻完了）"
    assert result["removed"] == 0, "增量同步删了行（会把整个店铺清空）"
    db = SessionLocal()
    try:
        ids = set(db.scalars(select(ShopProduct.item_id)).all())
        assert ids == {"EXISTING"}
    finally:
        db.close()


def test_range_sync_keeps_out_of_range_rows(tmp_path, monkeypatch):
    """**区间同步**：范围外的既有行保留不删（用户只让同步这个区间，没让删别的）。"""
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    shop_pk = _shop_pk(SessionLocal)
    _seed_product(SessionLocal, shop_pk, "OUT-OF-RANGE")
    FakeItemsAdapter.pages = [([_item("I1")], 1, False)]

    day_ms = 24 * 3600 * 1000
    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3, adapter_factory=FakeItemsAdapter,
        time_from_ms=1786584162000 - day_ms, time_to_ms=1786584162000 + day_ms,
    )

    assert result["removed"] == 0
    db = SessionLocal()
    try:
        ids = set(db.scalars(select(ShopProduct.item_id)).all())
        assert "OUT-OF-RANGE" in ids and "I1" in ids
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 2026-09-23 用户口径：增量 / 区间同步只补「条件区间内」的商品信息和详情
# --------------------------------------------------------------------------- #
def test_range_sync_does_not_fetch_details_for_out_of_window_items(tmp_path, monkeypatch):
    """**区间同步：比结束日期更新的商品既不拉详情、也不落库**（用户口径
    「增量同步只补条件区间内的商品信息和详情」）。"""
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    window_to_ms = 1784082313000 + 1000  # 结束日：只比商品创建时间晚 1 秒
    later = _item("LATER")
    later["create_time"] = window_to_ms + 999999  # 比窗口上界还新（范围外）
    in_window = _item("IN-WINDOW")
    in_window["create_time"] = 1784082313000  # 在窗口内
    FakeItemsAdapter.pages = [([later, in_window], 2, False)]
    FakeItemsAdapter.skus_map = {
        "LATER": [_sku("S-LATER")],
        "IN-WINDOW": [_sku("S-IN")],
    }

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3, adapter_factory=FakeItemsAdapter,
        mode="full", time_to_ms=window_to_ms,
    )

    # 范围外的商品**详情请求一个都不该发**
    fetched = {item_id for batch in FakeItemsAdapter.sku_batches for item_id in batch}
    assert fetched == {"IN-WINDOW"}, f"范围外的商品被拉了详情：{fetched}"
    assert result["detail_done"] == 1

    db = SessionLocal()
    try:
        ids = set(db.scalars(select(ShopProduct.item_id)).all())
        assert ids == {"IN-WINDOW"}, f"范围外的商品被落库了：{ids}"
    finally:
        db.close()


def test_incremental_sync_does_not_fetch_details_for_items_behind_cursor(tmp_path, monkeypatch):
    """**增量同步：比游标旧、被平台秒级窗口圈回来的商品不拉详情、不落库。**

    平台的 `create_time_from` 单位是**秒**且按 `>=` 比较（DEF-045）⇒ 同一秒内
    毫秒数更小的旧行也会被圈回来。那些行**不属于**本次增量范围。
    """
    from backend.app.services.shop_binding_service import set_shop_state

    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    cursor_ms = 1784082313000
    db = SessionLocal()
    try:
        set_shop_state(db.get(PlatformAccount, account_id), goods_cursor_ms=cursor_ms)
        db.commit()
    finally:
        db.close()

    stale = _item("STALE")  # 比游标旧（被秒级窗口圈回的旧行）
    stale["create_time"] = cursor_ms - 500
    fresh = _item("FRESH")
    fresh["create_time"] = cursor_ms + 1000
    FakeItemsAdapter.pages = [([stale, fresh], 2, False)]
    FakeItemsAdapter.skus_map = {
        "STALE": [_sku("S-STALE")],
        "FRESH": [_sku("S-FRESH")],
    }

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3,
        mode="incremental", adapter_factory=FakeItemsAdapter,
    )

    fetched = {item_id for batch in FakeItemsAdapter.sku_batches for item_id in batch}
    assert fetched == {"FRESH"}, f"游标之前（范围外）的商品被拉了详情：{fetched}"
    assert result["detail_done"] == 1

    db = SessionLocal()
    try:
        ids = set(db.scalars(select(ShopProduct.item_id)).all())
        assert ids == {"FRESH"}, f"游标之前的商品被落库了：{ids}"
    finally:
        db.close()


def test_rate_limit_stops_the_page_loop(tmp_path, monkeypatch):
    _headers, account_id, user_id, _SessionLocal = _setup(tmp_path, monkeypatch)
    LIMITER["impl"] = DenyLimiter()

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3, adapter_factory=FakeItemsAdapter
    )

    assert FakeItemsAdapter.calls == []
    assert result["status"] == sgs.sts.STATUS_DONE  # 不是异常，是「本次到此为止」
    assert "限流" in result["note"]


def test_page_cap_bounds_the_run(tmp_path, monkeypatch):
    _headers, account_id, user_id, _SessionLocal = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [
        ([_item("I1")], 999, True),
        ([_item("I2")], 999, True),
        ([_item("I3")], 999, True),
    ]

    sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=2, adapter_factory=FakeItemsAdapter
    )

    assert len(FakeItemsAdapter.calls) == 2


def test_stops_early_when_stored_reaches_total(tmp_path, monkeypatch):
    _headers, account_id, user_id, _SessionLocal = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [([_item("I1")], 1, True)]

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=20, adapter_factory=FakeItemsAdapter
    )

    assert result["complete"] is True
    assert len(FakeItemsAdapter.calls) == 1


# --------------------------------------------------------------------------- #
# 能力门禁（ADR-021：判据是令牌，不是绑店）+ 凭据失效
# --------------------------------------------------------------------------- #
def test_missing_ark_session_fails_without_touching_the_platform(tmp_path, monkeypatch):
    """**没有 ark 登录态** → 任务失败、一个平台请求都不发（ADR-021 门禁）。

    被取代的旧断言（原句保留）：本用例原名 `test_missing_binding_fails_without_touching_the_platform`，
    断言 `"ARK_SESSION_REQUIRED" in result["error"]`。旧判据是「有没有绑店」；
    2026-09-30 ADR-021 把判据换成「本账号有没有可用的 ark 会话」——「绑店」这个
    动作本身已随店铺实体退场，错误码随之改为 `ARK_SESSION_REQUIRED`
    （唯一凭据判据 `XHS_Qianfan_Apis.is_credential_error()`，见设计 18 / §17.10）。
    """
    _headers, account_id, user_id, _SessionLocal = _setup(tmp_path, monkeypatch, with_binding=False)

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3, adapter_factory=FakeItemsAdapter
    )

    assert result["status"] == sgs.sts.STATUS_FAILED
    assert "ARK_SESSION_REQUIRED" in result["error"]
    assert FakeItemsAdapter.calls == []


def test_account_unresolved_fails_and_invents_nothing(tmp_path, monkeypatch):
    """**账号解不出**（软删 / 越权）→ 任务失败且**一行都不编**（`platform_account_id` NOT NULL）。

    ⚠️ **被取代的旧断言（原句保留）**：本用例原名 `test_shop_unresolved_fails_and_invents_nothing`，
    更早的名字是 `test_binding_without_shop_entity_fails_and_invents_nothing`，
    断言 `"SHOP_UNRESOLVED" in result["error"]`。那个 code 存在的理由，是旧模型里
    **「有绑定行、但绑定的 `shop_id_fk` 为空」**是一个**独立状态**
    （`shop_account_bindings` 一行 + 一个 NULL 外键）。2026-09-22 该表移除后
    「没绑店」与「解不出店」合成同一个状态；**2026-09-30 ADR-021 之后连这个状态
    也不存在了** —— 归属键就是账号主键，**没有任何关系需要解析**，剩下能「解不出」
    的只有账号本身（`account_for_ark_gate` 的软删 / 租户闸 ⇒ `ACCOUNT_NOT_FOUND`）。

    所以本用例改为**扣住账号被软删**这个真实可复现的失败态，守住同一条纪律：
    解不出归属 ⇒ 一行不编、一个平台请求不发。
    """
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)

    db = SessionLocal()
    try:
        db.get(PlatformAccount, account_id).deleted_at = shanghai_now()
        db.commit()
    finally:
        db.close()

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3, adapter_factory=FakeItemsAdapter
    )

    assert result["status"] == sgs.sts.STATUS_FAILED
    assert "ACCOUNT_NOT_FOUND" in result["error"]
    assert FakeItemsAdapter.calls == [], "解不出账号时一个平台请求都不该发"
    db = SessionLocal()
    try:
        assert db.scalar(select(ShopProduct.id)) is None  # 没编出任何一行
    finally:
        db.close()


def test_invalid_credential_marks_shop_state_but_keeps_products(tmp_path, monkeypatch):
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    shop_pk = _shop_pk(SessionLocal)
    _seed_product(SessionLocal, shop_pk, "KEEP-ME")

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3, adapter_factory=InvalidCredentialAdapter
    )

    assert result["status"] == sgs.sts.STATUS_FAILED
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        #: 千帆侧状态已从绑定行搬到 `account.shop_state`（2026-09-22）。
        assert get_shop_state(account)["status"] == "invalid"  # 页面的「重新绑定」提示据此接管
        # 登录态过期 ≠ 商品下架。
        assert db.scalars(select(ShopProduct.item_id)).all() == ["KEEP-ME"]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# API：防重 / 鉴权 / 参数 / 状态
# --------------------------------------------------------------------------- #
def test_expired_credential_also_marks_shop_state(tmp_path, monkeypatch):
    """⚠️ **DEF-084**（2026-09-30）：令牌**过期**时千帆回的是 `code=-13004`。

    它与 `-13000`（缺失）是**同一类事实**，必须得到同一个结局：账号的千帆状态标
    `invalid`（页面的「重新登录千帆」提示据此接管）。原实现只认 `-13000` ⇒
    「已过期」被当成**普通失败** ⇒ 页面既不提示重登，账号卡的千帆标签也永远停在
    「未检测」—— 与用户报的「一直卡未检测」是同一个根因。
    """
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    shop_pk = _shop_pk(SessionLocal)
    _seed_product(SessionLocal, shop_pk, "KEEP-TOO")

    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=3, adapter_factory=ExpiredCredentialAdapter
    )

    assert result["status"] == sgs.sts.STATUS_FAILED
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        assert get_shop_state(account)["status"] == "invalid", (
            "-13004（已过期）必须与 -13000 同判为凭据失效，否则重登提示永不出现"
        )
        assert db.scalars(select(ShopProduct.item_id)).all() == ["KEEP-TOO"]
    finally:
        db.close()


def test_second_trigger_reuses_the_active_task(tmp_path, monkeypatch):
    headers, account_id, _user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(ats, "ensure_account_task_worker", lambda *a, **k: None)

    first = client.post(
        "/api/xhs/qianfan/sync-shop-goods",
        json={"platform_account_id": account_id},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/xhs/qianfan/sync-shop-goods",
        json={"platform_account_id": account_id},
        headers=headers,
    )
    assert second.status_code == 200, second.text

    assert first.json()["task_id"] == second.json()["task_id"]
    assert first.json()["task_type"] == "xhs_shop_goods_sync"
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(Task).where(Task.task_type == sgs.sts.SHOP_GOODS_SYNC_TASK_TYPE)
        ).all()
        assert len(rows) == 1
    finally:
        db.close()


def test_unbound_account_is_400(tmp_path, monkeypatch):
    headers, account_id, _user_id, _SessionLocal = _setup(tmp_path, monkeypatch, with_binding=False)

    response = client.post(
        "/api/xhs/qianfan/sync-shop-goods",
        json={"platform_account_id": account_id},
        headers=headers,
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "ARK_SESSION_REQUIRED"


def test_other_users_account_is_404(tmp_path, monkeypatch):
    _headers, account_id, _user_id, _SessionLocal = _setup(tmp_path, monkeypatch)
    other = client.post("/api/auth/register", json={"username": "intruder", "password": "secret123"})
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}

    response = client.post(
        "/api/xhs/qianfan/sync-shop-goods",
        json={"platform_account_id": account_id},
        headers=other_headers,
    )
    assert response.status_code == 404


def test_out_of_range_params_are_rejected(tmp_path, monkeypatch):
    headers, account_id, _user_id, _SessionLocal = _setup(tmp_path, monkeypatch)

    # §21：`21` 现在**合法**（上限已放到 `ABSOLUTE_PAGE_CAP=500`）。
    for body in ({"max_pages": 0}, {"max_pages": 501}, {"page_size": 0}, {"page_size": 101}):
        response = client.post(
            "/api/xhs/qianfan/sync-shop-goods",
            json={"platform_account_id": account_id, **body},
            headers=headers,
        )
        assert response.status_code == 422, (body, response.text)


def test_status_endpoint_is_idle_then_reports_the_result(tmp_path, monkeypatch):
    """`last_result` 的来源 = **该账号最新一条同步日志**（2026-09-27 判据变更，设计 13 §35.17）。

    ⚠️ **被取代的原断言（保留备查）**：改造前跑完 `sgs.run_shop_goods_sync(...)` 就直接断言
    `last_result["status"] == STATUS_DONE` 且 `progress["stored"] == 1` —— 那时 `last_result`
    读的是**业务任务行**。判据改成「该账号最新一条同步日志」后**不做兼容回落** ⇒ 任务行在、
    日志不在 ⇒ 端点回**空态**。账号卡完整链路由 `test_account_card_sync_job_log.py` 覆盖；
    这里钉的是**端点读的是日志**。
    """
    from backend.app.models.scheduled_job import ScheduledJobLog
    from backend.app.services.job_registry import JOB_KEY_SHOP_GOODS_SYNC

    headers, account_id, user_id, _SessionLocal = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [([_item("I1")], 1, False)]

    idle = client.get(
        f"/api/xhs/qianfan/sync-shop-goods/status?platform_account_id={account_id}",
        headers=headers,
    )
    assert idle.status_code == 200
    assert idle.json()["status"] == "idle"

    # 只落业务任务行（不写日志）⇒ 端点**仍是空态**：判据只认日志
    sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=1, adapter_factory=FakeItemsAdapter
    )
    task_only = client.get(
        f"/api/xhs/qianfan/sync-shop-goods/status?platform_account_id={account_id}",
        headers=headers,
    )
    assert task_only.json()["status"] == "idle"
    assert task_only.json()["last_result"] is None, "判据只认日志，不回读业务任务行"

    # 补一条终态日志（账号卡路径由 `scheduled_job_runner.run_targeted_job` 写）⇒ 端点回它
    db = _SessionLocal()
    try:
        db.add(
            ScheduledJobLog(
                user_id=user_id,
                platform_account_id=account_id,
                job_key=JOB_KEY_SHOP_GOODS_SYNC,
                job_name="同步商品",
                trigger_type="manual",
                status="success",
                progress=100,
                duration_ms=1234,
                started_at=shanghai_now(),
                finished_at=shanghai_now(),
                result={"stored": 1, "total": 1},
            )
        )
        db.commit()
    finally:
        db.close()

    after = client.get(
        f"/api/xhs/qianfan/sync-shop-goods/status?platform_account_id={account_id}",
        headers=headers,
    )
    body = after.json()
    assert body["status"] == "idle"
    assert body["last_result"]["status"] == sgs.sts.STATUS_DONE
    assert body["last_result"]["progress"] == 100


# --------------------------------------------------------------------------- #
# 硬约束 C5：AST 守卫（按 Attribute 调用判定，不被文档字符串误伤）
# --------------------------------------------------------------------------- #
def test_service_never_calls_a_full_loop():
    tree = ast.parse(inspect.getsource(sgs))
    banned = {"get_all_items", "get_all_products", "iter_all_items"}
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called & banned), f"商品同步不得调用全量循环方法：{called & banned}"


# --------------------------------------------------------------------------- #
# v1.32（设计 13 §30.3）：筛选 / 排序九列 + 字段补足 pass
# --------------------------------------------------------------------------- #
def test_normalize_product_fills_list_columns(tmp_path, monkeypatch):
    """九列逐字段断言：来源 raw_json 的键；缺值留空 / None，**不造 0**。"""
    normalized = sgs.normalize_product(
        {
            "item_id": "I1",
            "item_name": "编织地垫",
            "article_no": "A-1",
            "min_price": 1999,
            "max_price": 2999,
            "total_stock": 10,
            "sale_qty_info": {"acc_sale_qty": 5, "sale_qty30": 2},
            "card_type": 2,
            "on_shelf_time": 1784084996000,
            "off_shelf_time": None,
            "create_time": 1784082313000,
            "item_audit_time": 1784089999000,
        }
    )
    assert normalized is not None
    assert normalized["article_no"] == "A-1"
    assert normalized["card_type"] == 2
    assert normalized["price_min_fen"] == 1999 and normalized["price_max_fen"] == 2999
    assert normalized["create_time_ms"] == 1784082313000
    assert normalized["on_shelf_time_ms"] == 1784084996000
    assert normalized["off_shelf_time_ms"] is None
    assert normalized["last_audit_time_ms"] == 1784089999000
    assert normalized["sales_30d"] == 2

    # 缺值：价格 / 货号 / 状态都没有 ⇒ 空，不是 0（硬约束⑤）。
    # ⚠️ card_type 例外：平台不给时按生命周期推导（真机 1974 条 0 命中 card_type）——
    # 无上架时间 ⇒ 推导成「已下架」(3)，这是**有依据的推导**不是猜（§30.3.2）。
    bare = sgs.normalize_product({"item_id": "I2", "item_name": "空的"})
    assert bare["price_min_fen"] is None and bare["price_max_fen"] is None
    assert bare["article_no"] == ""
    assert bare["create_time_ms"] is None and bare["sales_30d"] is None
    assert bare["card_type"] == 3
    assert bare["status"] == "已下架"
    # 没有主键就没有行的身份
    assert sgs.normalize_product({"item_name": "没有 ID"}) is None


def test_list_columns_are_backfilled_from_raw_json(tmp_path, monkeypatch):
    """字段补足：老行（九列为空、raw_json 有值）→ 本地派生回填；**零平台请求**。"""
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [
        (
            [
                _item(
                    "I1",
                    title="编织地垫",
                    min_price=1999,
                    max_price=2999,
                    stock=10,
                    sales=5,
                    card_type=2,
                )
            ],
            1,
            False,
        )
    ]
    result = sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=1, adapter_factory=FakeItemsAdapter
    )
    assert result["stored"] == 1

    # 模拟「迁移前的老行」：九列被清空（raw_json 完好 = 真机存量行的形状）
    db = SessionLocal()
    try:
        db.execute(
            sa_text(
                "UPDATE shop_products SET article_no='', card_type=NULL, price_min_fen=NULL,"
                " price_max_fen=NULL, create_time_ms=NULL, on_shelf_time_ms=NULL,"
                " off_shelf_time_ms=NULL, last_audit_time_ms=NULL, sales_30d=NULL"
            )
        )
        db.commit()
    finally:
        db.close()

    # 同步任务的字段补足 pass 再跑一次：从 raw_json 本地回填（不打平台）
    db = SessionLocal()
    try:
        shop_pk = db.scalar(select(PlatformAccount.id))
        changed = sgs.backfill_list_columns(db, shop_pk)
        assert changed == 1
        row = db.scalars(select(ShopProduct).where(ShopProduct.item_id == "I1")).one()
        assert row.article_no == "1056576341942"
        assert row.price_min_fen == 1999 and row.price_max_fen == 2999
        assert row.card_type == 2
        assert row.on_shelf_time_ms == 1784084996000
        assert row.last_audit_time_ms == 1784089999000
        assert row.sales_30d == 3
        assert row.create_time_ms == 1784082313000
        assert FakeItemsAdapter.calls != [], "前面刚同步过（调用发生在同步，不发生在补足）"
        calls_before = len(FakeItemsAdapter.calls)
        sgs.backfill_list_columns(db, shop_pk)
        assert len(FakeItemsAdapter.calls) == calls_before, "字段补足不得打平台"
    finally:
        db.close()


def test_backfill_respects_the_cap(tmp_path, monkeypatch):
    """字段补足有界：每轮最多 `LIST_BACKFILL_CAP` 行，多轮收敛。"""
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [([_item("I1"), _item("I2"), _item("I3")], 3, False)]
    sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=1, adapter_factory=FakeItemsAdapter
    )

    db = SessionLocal()
    try:
        db.execute(
            sa_text(
                "UPDATE shop_products SET article_no='', card_type=NULL, price_min_fen=NULL,"
                " price_max_fen=NULL, create_time_ms=NULL, on_shelf_time_ms=NULL,"
                " off_shelf_time_ms=NULL, last_audit_time_ms=NULL, sales_30d=NULL"
            )
        )
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        shop_pk = db.scalar(select(PlatformAccount.id))
        assert sgs.backfill_list_columns(db, shop_pk, cap=1) == 1
        assert sgs.backfill_list_columns(db, shop_pk, cap=1) == 1
        assert sgs.backfill_list_columns(db, shop_pk, cap=1) == 1
        assert sgs.backfill_list_columns(db, shop_pk, cap=1) == 0, "补完就该收敛"
        filled = db.scalar(
            sa_text("SELECT COUNT(*) FROM shop_products WHERE price_min_fen IS NOT NULL")
        )
        assert filled == 3
    finally:
        db.close()


def test_sync_records_products_total_and_synced_at_in_shop_state(tmp_path, monkeypatch):
    """同步把平台总数 + 同步时间写进 `shop_state`（页面「本地台账 N 件 · 同步于 X」）。"""
    _headers, account_id, user_id, SessionLocal = _setup(tmp_path, monkeypatch)
    FakeItemsAdapter.pages = [([_item("I1")], 1, False)]
    sgs.run_shop_goods_sync(
        account_id, user_id=user_id, max_pages=1, adapter_factory=FakeItemsAdapter
    )

    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        state = get_shop_state(account)
        assert state["products_total"] == 1
        assert state["products_synced_at"], "同步时间必须落 state（页面据此展示时效）"
    finally:
        db.close()
