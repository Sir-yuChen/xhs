"""M7 订单二期**写操作**的口径与守卫（设计 16 §7.4）。

⚠️ 本文件**不打真实平台** —— 用假适配器记录调用，断言的是「**写之前**的纪律」：

| # | 纪律 | 断言 |
|---|---|---|
| 1 | **两层确认** | 缺 `confirmed` / `confirm_token` 不匹配 ⇒ 400，**一次都不打平台** |
| 2 | **门禁在写之前** | 非「待发货」/ 非「待处理」⇒ 400 `ORDER_NOT_SHIPPABLE` / `ORDER_AFTERSALE_NOT_PENDING`，**一次都不打平台** |
| 3 | **越权 404** | 别人的订单 / 售后单 ⇒ 404（不是 200、不是别人的数据） |
| 4 | **失败不落半截** | 平台拒绝 ⇒ 状态与物流行**都不变**，原因原样带出 |
| 5 | **批量分层回执** | 一条失败不拖垮整批，`succeeded[]` / `failed[]` 各自带原因 |
| 6 | **绝不自动触发** | 无定时任务 / 无工作流登记点（源码守卫） |
| 7 | **上限前后端同值** | `MAX_BATCH_SHIP` 与前端常量相等 |

⚠️ 用 `conftest` 公共夹具（`api_client` / `db_factory`）—— 项目硬规矩，不抄三件套。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.app.core.security import create_access_token
from backend.app.core.time import shanghai_now
from backend.app.models import AfterSale, Order, OrderShipment, PlatformAccount
from backend.app.services import order_write_service as ows
from tests.backend.conftest import seed_owner_rows
from tests.backend.cookie_fixtures import give_ark_cookies


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration

OWNER_ID = 2
OTHER_ID = 3
ACCOUNT_ID = 201
OTHER_ACCOUNT_ID = 202


class FakeAdapter:
    """记录调用（**绝不真打平台**）。`mode` 控制返回。"""

    mode = "ok"
    calls: list[dict] = []

    def __init__(self, account=None, cookies=None):  # noqa: ARG002 - 保持工厂签名
        pass

    def single_online_delivery(self, body):
        FakeAdapter.calls.append({"api": "ship", "body": body})
        if FakeAdapter.mode == "reject":
            return False, "平台拒绝：运单号已被使用", {"code": -1}
        return True, "ok", {"code": 0}

    def after_sale_audit(self, body):
        FakeAdapter.calls.append({"api": "audit", "body": body})
        if FakeAdapter.mode == "reject":
            return False, "平台拒绝：售后单状态已变更", {"code": -1}
        return True, "ok", {"code": 0}


@pytest.fixture()
def ctx(api_client, db_factory):
    """两用户（owner / other）+ 各自账号 + owner 的待发货订单与待处理售后单。"""
    from backend.app.main import app
    from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory

    FakeAdapter.mode = "ok"
    FakeAdapter.calls = []
    app.dependency_overrides[get_qianfan_api_adapter_factory] = lambda: FakeAdapter

    seed_owner_rows(db_factory().get_bind(), user_id=OWNER_ID, account_id=ACCOUNT_ID)
    seed_owner_rows(db_factory().get_bind(), user_id=OTHER_ID, account_id=OTHER_ACCOUNT_ID)

    db = db_factory()
    try:
        owner_account = db.get(PlatformAccount, ACCOUNT_ID)
        owner_account.nickname = "观隅好物"
        other_account = db.get(PlatformAccount, OTHER_ACCOUNT_ID)
        other_account.nickname = "别人的号"
        # 给 owner 账号一份 ark 令牌（写链路需要；**不建店铺关系** —— ADR-021 后无此物）
        give_ark_cookies(db, owner_account)
        db.commit()

        # owner 的待发货订单
        order = Order(
            user_id=OWNER_ID,
            platform_account_id=ACCOUNT_ID,
            order_no="O-SHIP-1",
            package_id="P-1",
            status="待发货",
            buyer_nick="买家",
            created_at=shanghai_now(),
            synced_at=shanghai_now(),
        )
        # 一个非待发货订单（门禁用）
        done = Order(
            user_id=OWNER_ID,
            platform_account_id=ACCOUNT_ID,
            order_no="O-DONE-1",
            package_id="P-2",
            status="已完成",
            buyer_nick="买家",
            created_at=shanghai_now(),
            synced_at=shanghai_now(),
        )
        # 别人的订单（越权用）
        foreign = Order(
            user_id=OTHER_ID,
            platform_account_id=OTHER_ACCOUNT_ID,
            order_no="O-FOREIGN",
            package_id="P-3",
            status="待发货",
            buyer_nick="别人",
            created_at=shanghai_now(),
            synced_at=shanghai_now(),
        )
        db.add_all([order, done, foreign])
        db.commit()

        # 售后单：待处理 / 已处理各一
        pending = AfterSale(
            order_id=order.id, user_id=OWNER_ID, after_sale_id="AS-1",
            type="仅退款", reason="不想要了", status="待处理", amount=Decimal("34.80"),
        )
        handled = AfterSale(
            order_id=order.id, user_id=OWNER_ID, after_sale_id="AS-2",
            type="退货退款", reason="质量问题", status="已完成", amount=Decimal("10.00"),
        )
        db.add_all([pending, handled])
        db.commit()
    finally:
        db.close()

    owner_headers = {"Authorization": f"Bearer {create_access_token(OWNER_ID)}"}
    other_headers = {"Authorization": f"Bearer {create_access_token(OTHER_ID)}"}
    yield api_client, owner_headers, other_headers, db_factory
    app.dependency_overrides.pop(get_qianfan_api_adapter_factory, None)


def _ship_token(db_factory, order_no="O-SHIP-1", *, company_code="SF", tracking_no="SF123"):
    """单笔发货的确认凭据（用**服务层同一份素材构造**算，与预览端点同源）。"""
    db = db_factory()
    try:
        order = db.scalars(select(Order).where(Order.order_no == order_no)).one()
        return ows.confirm_token_for(
            ows.ship_confirm_material(
                order, company_code=company_code, tracking_no=tracking_no
            )
        )
    finally:
        db.close()


def _as_row(db_factory, after_sale_id: str):
    db = db_factory()
    try:
        return db.scalars(
            select(AfterSale).where(AfterSale.after_sale_id == after_sale_id)
        ).one()
    finally:
        db.close()


def _batch_token(db_factory, items: list[dict]):
    """批量发货的确认凭据（用服务层同一份素材构造，与预览端点同源）。

    ⚠️ **与端点同样去重保序**：端点先按 `order_no` 去重再构造素材 ⇒
    这里不去重就会得到不同的指纹（token 对不上）。
    """
    db = db_factory()
    try:
        specs = []
        seen: set[str] = set()
        for item in items:
            key = str(item.get("order_no") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            order = db.scalars(
                select(Order).where(Order.order_no == key)
            ).one()
            specs.append(
                {
                    "order_no": order.order_no,
                    "package_id": order.package_id,
                    "company_code": item.get("company_code", ""),
                    "tracking_no": item.get("tracking_no", ""),
                }
            )
        return ows.confirm_token_for(ows.batch_ship_confirm_material(specs))
    finally:
        db.close()


def _ship_row(db_factory, order_no: str):
    db = db_factory()
    try:
        return db.scalars(select(Order).where(Order.order_no == order_no)).one()
    finally:
        db.close()


class TestDoubleConfirm:
    """纪律 1：**两层确认**（缺一层 ⇒ 400，**一次都不打平台**）。"""

    def test_ship_without_confirmed_is_400_and_never_calls_platform(self, ctx):
        client, headers, _, db_factory = ctx
        body = {
            "platform_account_id": ACCOUNT_ID, "order_no": "O-SHIP-1",
            "company_code": "SF", "tracking_no": "SF123",
            "confirmed": False, "confirm_token": "",
        }
        response = client.post("/api/xhs/qianfan/orders/ship", json=body, headers=headers)
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "ORDER_CONFIRM_REQUIRED"
        assert FakeAdapter.calls == [], "缺第一层确认却打了平台"

    def test_ship_with_wrong_token_is_400_and_never_calls_platform(self, ctx):
        client, headers, _, db_factory = ctx
        body = {
            "platform_account_id": ACCOUNT_ID, "order_no": "O-SHIP-1",
            "company_code": "SF", "tracking_no": "SF123",
            "confirmed": True, "confirm_token": "deadbeef",  # 凭据不匹配
        }
        response = client.post("/api/xhs/qianfan/orders/ship", json=body, headers=headers)
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "ORDER_CONFIRM_MISMATCH"
        assert FakeAdapter.calls == [], "凭据不匹配却打了平台"

    def test_token_is_content_bound(self, ctx):
        """⚠️ 改掉运单号 ⇒ 旧凭据失效（防「确认的是旧动作」）。"""
        client, headers, _, db_factory = ctx
        old_token = _ship_token(db_factory, tracking_no="SF-OLD")
        body = {
            "platform_account_id": ACCOUNT_ID, "order_no": "O-SHIP-1",
            "company_code": "SF", "tracking_no": "SF-NEW",  # 内容变了
            "confirmed": True, "confirm_token": old_token,
        }
        assert client.post(
            "/api/xhs/qianfan/orders/ship", json=body, headers=headers
        ).status_code == 400
        assert FakeAdapter.calls == []

    def test_preview_returns_matching_token(self, ctx):
        """预览端点下发的 token 必须能通过写端点的校验（否则永远发不出去）。"""
        client, headers, _, db_factory = ctx
        preview = client.post(
            "/api/xhs/qianfan/orders/ship/preview",
            json={
                "platform_account_id": ACCOUNT_ID, "kind": "ship",
                "order_no": "O-SHIP-1", "company_code": "SF", "tracking_no": "SF123",
            },
            headers=headers,
        )
        assert preview.status_code == 200, preview.text
        token = preview.json()["confirm_token"]
        assert preview.json()["shop_name"] == "观隅好物"

        ok = client.post(
            "/api/xhs/qianfan/orders/ship",
            json={
                "platform_account_id": ACCOUNT_ID, "order_no": "O-SHIP-1",
                "company_code": "SF", "tracking_no": "SF123",
                "confirmed": True, "confirm_token": token,
            },
            headers=headers,
        )
        assert ok.status_code == 200, ok.text


class TestGateBeforeWrite:
    """纪律 2：**门禁在写之前**（非目标状态一次都不打平台）。"""

    def test_shipping_a_finished_order_is_400_and_never_calls_platform(self, ctx):
        client, headers, _, db_factory = ctx
        token = _ship_token(db_factory, "O-DONE-1")
        body = {
            "platform_account_id": ACCOUNT_ID, "order_no": "O-DONE-1",
            "company_code": "SF", "tracking_no": "SF123",
            "confirmed": True, "confirm_token": token,
        }
        response = client.post("/api/xhs/qianfan/orders/ship", json=body, headers=headers)
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "ORDER_NOT_SHIPPABLE"
        assert FakeAdapter.calls == [], "非待发货却打了平台"

    def test_handling_a_done_after_sale_is_400_and_never_calls_platform(self, ctx):
        client, headers, _, db_factory = ctx
        token = ows.confirm_token_for(
            ows.after_sale_confirm_material(
                _as_row(db_factory, "AS-2"), action="agree", reject_reason=""
            )
        )
        body = {
            "platform_account_id": ACCOUNT_ID, "after_sale_id": "AS-2",
            "action": "agree", "confirmed": True, "confirm_token": token,
        }
        response = client.post(
            "/api/xhs/qianfan/after-sales/handle", json=body, headers=headers
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "ORDER_AFTERSALE_NOT_PENDING"
        assert FakeAdapter.calls == []

    def test_reject_without_reason_is_400(self, ctx):
        client, headers, _, db_factory = ctx
        token = ows.confirm_token_for(
            ows.after_sale_confirm_material(
                _as_row(db_factory, "AS-1"), action="reject", reject_reason=""
            )
        )
        body = {
            "platform_account_id": ACCOUNT_ID, "after_sale_id": "AS-1",
            "action": "reject", "reject_reason": "", "confirmed": True,
            "confirm_token": token,
        }
        response = client.post(
            "/api/xhs/qianfan/after-sales/handle", json=body, headers=headers
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "ORDER_AFTERSALE_REASON_REQUIRED"
        assert FakeAdapter.calls == []


class TestTenantIsolation:
    """纪律 3：**越权 404**（不区分「不存在」与「不属于你」）。"""

    def test_shipping_other_users_order_is_404(self, ctx):
        """**两层归属校验**都要承重：账号归属 + 订单 `user_id`。

        ⚠️ 本用例刻意造一行**脏数据**（挂在我的账号上、但 `user_id` 是别人的）——
        这是唯一能让「订单 `user_id` 过滤」变成承重判据的场景。只有账号过滤时
        它会漏进来（`_owned_account` 放行了账号，`_owned_order` 没看 `user_id`）。
        """
        client, headers, _, db_factory = ctx
        db = db_factory()
        try:
            db.add(
                Order(
                    user_id=OTHER_ID,                     # ← 别人的
                    platform_account_id=ACCOUNT_ID,       # ← 我的账号（脏数据）
                    order_no="O-DIRTY", package_id="P-DIRTY", status="待发货",
                    buyer_nick="脏数据", created_at=shanghai_now(), synced_at=shanghai_now(),
                )
            )
            db.commit()
        finally:
            db.close()

        token = _ship_token(db_factory, "O-DIRTY")
        body = {
            "platform_account_id": ACCOUNT_ID, "order_no": "O-DIRTY",
            "company_code": "SF", "tracking_no": "SF123",
            "confirmed": True, "confirm_token": token,
        }
        response = client.post("/api/xhs/qianfan/orders/ship", json=body, headers=headers)
        assert response.status_code == 404, response.text
        assert FakeAdapter.calls == [], "别人的订单被打到了平台"

    def test_account_layer_alone_blocks_dirty_cross_account_row(self, ctx):
        """**账号层要能单独承重**：造一行「`user_id` 是别人、却挂在我账号上」的脏订单。

        ⚠️ 这是唯一能让账号层（`get_owned_active_account` 的 `user_id` 过滤）变成
        承重判据的场景：订单层过滤在这个方向上**放行**（`user_id` 恰好等于请求方），
        只有账号层能挡住。两层是**互为兜底**的纵深防御，各自都必须单独成立。
        """
        client, _, other_headers, db_factory = ctx
        db = db_factory()
        try:
            db.add(
                Order(
                    user_id=OTHER_ID,                     # ← 请求方（别人）
                    platform_account_id=ACCOUNT_ID,       # ← 却是我的账号（脏数据）
                    order_no="O-XACC", package_id="P-XACC", status="待发货",
                    buyer_nick="脏数据", created_at=shanghai_now(), synced_at=shanghai_now(),
                )
            )
            db.commit()
            row = db.scalars(select(Order).where(Order.order_no == "O-XACC")).one()
            token = ows.confirm_token_for(
                ows.ship_confirm_material(row, company_code="SF", tracking_no="SF123")
            )
        finally:
            db.close()
        body = {
            "platform_account_id": ACCOUNT_ID, "order_no": "O-XACC",
            "company_code": "SF", "tracking_no": "SF123",
            "confirmed": True, "confirm_token": token,
        }
        assert client.post(
            "/api/xhs/qianfan/orders/ship", json=body, headers=other_headers
        ).status_code == 404
        assert FakeAdapter.calls == [], "越权请求打到了平台"

    def test_preview_of_other_users_order_is_404(self, ctx):
        client, _, other_headers, db_factory = ctx
        response = client.post(
            "/api/xhs/qianfan/orders/ship/preview",
            json={
                "platform_account_id": OTHER_ACCOUNT_ID, "kind": "ship",
                "order_no": "O-SHIP-1", "company_code": "SF", "tracking_no": "SF123",
            },
            headers=other_headers,
        )
        assert response.status_code == 404, response.text


class TestFailureLeavesNoPartialData:
    """纪律 4：**失败不落半截数据**。"""

    def test_platform_rejection_keeps_status_and_shipments_unchanged(self, ctx):
        client, headers, _, db_factory = ctx
        FakeAdapter.mode = "reject"
        token = _ship_token(db_factory)
        body = {
            "platform_account_id": ACCOUNT_ID, "order_no": "O-SHIP-1",
            "company_code": "SF", "tracking_no": "SF123",
            "confirmed": True, "confirm_token": token,
        }
        response = client.post("/api/xhs/qianfan/orders/ship", json=body, headers=headers)
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "ORDER_SHIP_PLATFORM_REJECTED"
        # 平台原文带出来了（可重试）
        assert "平台拒绝" in response.json()["detail"]["message"]

        db = db_factory()
        try:
            order = db.scalars(select(Order).where(Order.order_no == "O-SHIP-1")).one()
            assert order.status == "待发货", "失败后状态被改了"
            rows = db.scalars(
                select(OrderShipment).where(OrderShipment.order_id == order.id)
            ).all()
            assert rows == [], "失败后落了物流行（半截数据）"
        finally:
            db.close()

    def test_success_records_shipment(self, ctx):
        client, headers, _, db_factory = ctx
        token = _ship_token(db_factory)
        body = {
            "platform_account_id": ACCOUNT_ID, "order_no": "O-SHIP-1",
            "company_code": "SF", "tracking_no": "SF123",
            "confirmed": True, "confirm_token": token,
        }
        assert client.post(
            "/api/xhs/qianfan/orders/ship", json=body, headers=headers
        ).status_code == 200
        db = db_factory()
        try:
            rows = db.scalars(select(OrderShipment)).all()
            assert len(rows) == 1
            assert rows[0].tracking_no == "SF123"
            assert rows[0].logistics_company_code == "SF"
        finally:
            db.close()


class TestBatchLayeredReceipt:
    """纪律 5：**批量分层回执**（一条失败不拖垮整批）。"""

    def test_batch_mixes_success_and_failure(self, ctx):
        client, headers, _, db_factory = ctx
        # 一批两笔：待发货（会成功）+ 已完成（门禁拒）
        items = [
            {"order_no": "O-SHIP-1", "company_code": "SF", "tracking_no": "SF-1"},
            {"order_no": "O-DONE-1", "company_code": "SF", "tracking_no": "SF-2"},
        ]
        token = _batch_token(db_factory, items)
        body = {
            "platform_account_id": ACCOUNT_ID, "items": items,
            "confirmed": True, "confirm_token": token,
        }
        response = client.post(
            "/api/xhs/qianfan/orders/ship-batch", json=body, headers=headers
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["succeeded_count"] == 1
        assert data["failed_count"] == 1
        assert data["succeeded"][0]["order_no"] == "O-SHIP-1"
        assert data["failed"][0]["order_no"] == "O-DONE-1"
        assert data["failed"][0]["code"] == "ORDER_NOT_SHIPPABLE"

    def test_batch_empty_is_400(self, ctx):
        client, headers, _, db_factory = ctx
        response = client.post(
            "/api/xhs/qianfan/orders/ship-batch",
            json={"platform_account_id": ACCOUNT_ID, "items": [], "confirmed": True, "confirm_token": ""},
            headers=headers,
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "ORDER_SHIP_ITEMS_REQUIRED"

    def test_batch_over_limit_is_400_not_truncated(self, ctx):
        """超限 ⇒ 400，**先于**任何归属查询 / 平台调用（不静默截断）。

        ⚠️ 上限校验在最前（不必真有那些订单行）—— 本用例刻意用**不存在的单号**，
        正是为了证明「超限判定不依赖逐单查询」。
        """
        client, headers, _, _ = ctx
        items = [
            {"order_no": f"O-NOT-EXIST-{i}", "company_code": "SF", "tracking_no": f"T{i}"}
            for i in range(ows.MAX_BATCH_SHIP + 1)
        ]
        response = client.post(
            "/api/xhs/qianfan/orders/ship-batch",
            json={
                "platform_account_id": ACCOUNT_ID, "items": items,
                "confirmed": True, "confirm_token": "whatever",
            },
            headers=headers,
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "ORDER_SHIP_ITEMS_TOO_MANY"
        assert response.json()["detail"]["limit"] == ows.MAX_BATCH_SHIP
        assert FakeAdapter.calls == []

    def test_batch_dedupes_order_nos(self, ctx):
        """重复的订单号去重（同 `items/delete` 纪律）。"""
        client, headers, _, db_factory = ctx
        items = [
            {"order_no": "O-SHIP-1", "company_code": "SF", "tracking_no": "SF-1"},
            {"order_no": "O-SHIP-1", "company_code": "SF", "tracking_no": "SF-1"},
        ]
        response = client.post(
            "/api/xhs/qianfan/orders/ship-batch",
            json={
                "platform_account_id": ACCOUNT_ID, "items": items,
                "confirmed": True, "confirm_token": _batch_token(db_factory, items),
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["requested"] == 1


class TestAfterSaleHandle:
    """售后审核的成功面（同意 / 拒绝各一次）。"""

    def test_agree_marks_agreed_and_persists_handle_time(self, ctx):
        client, headers, _, db_factory = ctx
        token = ows.confirm_token_for(
            ows.after_sale_confirm_material(
                _as_row(db_factory, "AS-1"), action="agree", reject_reason=""
            )
        )
        response = client.post(
            "/api/xhs/qianfan/after-sales/handle",
            json={
                "platform_account_id": ACCOUNT_ID, "after_sale_id": "AS-1",
                "action": "agree", "confirmed": True, "confirm_token": token,
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        db = db_factory()
        try:
            row = db.scalars(select(AfterSale).where(AfterSale.after_sale_id == "AS-1")).one()
            assert row.status == "已同意"
            assert row.handle_time is not None
        finally:
            db.close()

    def test_reject_records_reason(self, ctx):
        client, headers, _, db_factory = ctx
        reason = "商品已拆封"
        token = ows.confirm_token_for(
            ows.after_sale_confirm_material(
                _as_row(db_factory, "AS-1"), action="reject", reject_reason=reason
            )
        )
        response = client.post(
            "/api/xhs/qianfan/after-sales/handle",
            json={
                "platform_account_id": ACCOUNT_ID, "after_sale_id": "AS-1",
                "action": "reject", "reject_reason": reason,
                "confirmed": True, "confirm_token": token,
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        db = db_factory()
        try:
            row = db.scalars(select(AfterSale).where(AfterSale.after_sale_id == "AS-1")).one()
            assert row.status == "已拒绝"
            assert row.handle_result == reason
        finally:
            db.close()


class TestNoAutoTrigger:
    """纪律 6：**绝不自动触发**（源码守卫）。"""

    def test_ship_service_is_not_referenced_by_scheduler(self):
        """发货 / 售后**不得**出现在定时任务注册表 / 调度种子里。"""
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        candidates = [
            root / "backend" / "app" / "services" / "job_registry.py",
            # ⚠️ 2026-10-07：`workflow_engine.py` 已随工作流特性整条移除（用户口径
            # 「把工作流相关逻辑表等完整移除」），候选里不再列它 —— 本用例本来
            # `if not path.exists(): continue`，但留着死路径会让「文件被删」看起来
            # 像「守卫被摘掉」。自动触发面**只剩**任务注册表 + 调度种子两处。
            root / "config" / "initial_data" / "scheduled_jobs.json",
        ]
        for path in candidates:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            assert "order_write_service" not in text, f"{path.name} 引用了发货服务（自动触发面）"
            assert "single_online_delivery" not in text, f"{path.name} 引用了发货接口"
            assert "after_sale_audit" not in text, f"{path.name} 引用了售后审核接口"

    def test_write_service_never_used_in_order_sync(self):
        """同步（定时任务跑的）**不得**调用写服务。"""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[2]
            / "backend" / "app" / "services" / "order_sync_service.py"
        ).read_text(encoding="utf-8")
        assert "order_write_service" not in src


class TestLimitSameValue:
    """纪律 7：上限**前后端同值**（不许两份数）。"""

    def test_frontend_constant_matches_backend(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        frontend = (root / "frontend" / "src" / "lib" / "order-write.ts").read_text(
            encoding="utf-8"
        )
        assert f"MAX_BATCH_SHIP = {ows.MAX_BATCH_SHIP}" in frontend, (
            "前端批量发货上限与后端不同值"
        )


class TestRequestModelsForbid:
    """结构守卫：新请求模型全部 `extra="forbid"`。"""

    @pytest.mark.parametrize(
        "path,body",
        [
            ("/api/xhs/qianfan/orders/ship", {"platform_account_id": 1, "order_no": "x", "zz": 1}),
            ("/api/xhs/qianfan/orders/ship-batch", {"platform_account_id": 1, "items": [], "zz": 1}),
            ("/api/xhs/qianfan/after-sales/handle", {"platform_account_id": 1, "after_sale_id": "x", "action": "agree", "zz": 1}),
            ("/api/xhs/qianfan/orders/ship/preview", {"platform_account_id": 1, "kind": "ship", "zz": 1}),
        ],
    )
    def test_unknown_fields_are_rejected(self, ctx, path, body):
        client, headers, _, db_factory = ctx
        assert client.post(path, json=body, headers=headers).status_code == 422
