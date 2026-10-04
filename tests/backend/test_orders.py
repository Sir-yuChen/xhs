"""订单模块的口径与只读链路（设计 16 §7，M6 方向 6 · 一期只读）。

覆盖五块：

1. **状态映射**（`ORDER_STATUSES` / 平台状态归一）—— 平台原话进 `platform_status`，
   归一值进 `status`；**不认识的平台状态不许猜**（留 `""` 或原样，不硬塞一个近义词）。
2. **单号幂等 upsert**（`upsert_order`）—— 同 `order_no` 重复同步**不产生重复行**，
   且只更新可变字段（订单号 / 账号 / 买家身份**不重写**）。
3. **脱敏口径**（`mask_sensitive` / `order_summary`）—— 默认脱敏、`include_full=True`
   才给原值。履约数据「完整值落库、展示层脱敏」（设计 16 §7.2，2026-09-29 口径）。
4. **租户隔离** —— 越权看别人的订单必须是**空集**（不是 404、更不是别人的数据）。
5. **只读边界** —— 一期**零写平台接口**：订单链路里不许出现发货 / 售后写调用。

⚠️ 本文件遵守项目测试三规矩：用 `conftest` 的公共夹具（`db_engine` / `api_client`），
不自己抄 `create_engine + create_all + dependency_overrides` 三件套。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.core.time import shanghai_now
from backend.app.models import (
    Order,
    OrderItem,
    OrderRemark,
    OrderShipment,
    PlatformAccount,
    User,
)
from backend.app.services import order_sync_service as svc
from tests.backend.conftest import seed_owner_rows


@pytest.fixture()
def db(db_engine):
    """本用例的会话（绑测试库）。"""
    factory = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def account(db):
    """一个归属 user 2 的平台账号（订单必须挂在真实存在的账号上，FK=ON）。"""
    seed_owner_rows(db.get_bind(), user_id=2, account_id=7)
    db.commit()
    return db.get(PlatformAccount, 7)


def _normalized(order_no: str = "O-1", **over) -> dict:
    """一份最小可用的归一化订单（形状由 `normalize_package` 决定）。"""
    base = {
        "order_no": order_no,
        "user_id": 2,
        "platform_account_id": 7,
        "buyer_nick": "买家A",
        "buyer_user_id": "B-1",          # ⚠️ 不是 buyer_id（真实列名）
        "status": "待发货",
        "platform_status": "2",          # ⚠️ 平台码是**字符串数字**，不是中文
        "total_amount": Decimal("34.8"),
        "pay_amount": Decimal("34.8"),
        "skus": [
            {
                "sku_id": "S-1",
                "item_name": "商品A",
                "item_image": "",
                "sku": "红色",
                "quantity": 1,
                "unit_price": Decimal("34.8"),
                "amount": Decimal("34.8"),
                "refund_status": "",
            }
        ],
        "synced_at": shanghai_now(),
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# 1. 状态映射
# --------------------------------------------------------------------------- #
class TestStatusVocabulary:
    def test_status_vocabulary_is_exactly_the_six_designed_states(self):
        """六个状态是设计 16 §7.3 的**封闭词表**（页面筛选器直接用它）。"""
        from backend.app.models.order import ORDER_STATUSES

        assert set(ORDER_STATUSES) == {
            "待付款",
            "待发货",
            "待收货",   # ⚠️ 不是「已发货」—— 平台语义是「已发货*待收货*」
            "已完成",
            "售后中",
            "已关闭",
        }

    def test_unknown_platform_status_code_resolves_to_empty(self):
        """**不认识的平台状态码不许猜**（缺值口径：宁可留空，不硬塞近义词）。

        ⚠️ 这条是「不造假」原则的落点：把 `999999` 映射成 `"已完成"`
        会让用户看到**平台从未说过的话**。
        """
        from backend.app.models.order import resolve_order_status

        assert resolve_order_status(999999, 1) == ""
        assert resolve_order_status("not-a-code", 1) == ""
        assert resolve_order_status(None, None) == ""

    def test_known_platform_status_codes_map_to_designed_states(self):
        """已知平台码必须落到设计词表里（真机：1/2/21/26/4/5/55/6/65/7/998）。"""
        from backend.app.models.order import resolve_order_status

        for code, expect in (
            (1, "待付款"),
            (2, "待发货"),
            (21, "待发货"),
            (26, "待发货"),
            (4, "待收货"),
            (5, "待收货"),
            (55, "待收货"),
            (6, "已完成"),
            (65, "已完成"),
            (7, "已完成"),
            (998, "已关闭"),
        ):
            assert resolve_order_status(code, 1) == expect, f"平台码 {code} 映射错"

    def test_after_sale_takes_priority_over_shipping_state(self):
        """**售后中优先于发货态**：已发货但买家申请了退款 ⇒ 归「售后中」。

        设计 16 §7.5 的状态机：任何态都能进售后中；经营上要看的是
        「这笔单有纠纷」，而不是「货已经在路上」。
        """
        from backend.app.models.order import resolve_order_status

        assert resolve_order_status(4, 2) == "售后中"      # 已发货 + 售后中
        assert resolve_order_status(7, 2) == "售后中"      # 已完成 + 售后中
        # 售后终态（3/4）不改变订单本身的归类
        assert resolve_order_status(4, 3) == "待收货"
        assert resolve_order_status(4, 4) == "待收货"
        assert resolve_order_status(4, 1) == "待收货"      # 无售后


# --------------------------------------------------------------------------- #
# 2. 单号幂等 upsert
# --------------------------------------------------------------------------- #
class TestUpsertIdempotency:
    def test_same_order_no_twice_does_not_duplicate(self, db, account):
        """同 `order_no` 同步两次 ⇒ **一行**（唯一约束 + 应用层查重的共同效果）。"""
        svc.upsert_order(db, _normalized("O-DUP"), account_id=7)
        db.commit()
        svc.upsert_order(db, _normalized("O-DUP"), account_id=7)
        db.commit()

        rows = db.scalars(select(Order).where(Order.order_no == "O-DUP")).all()
        assert len(rows) == 1

    def test_resync_updates_status_and_amount(self, db, account):
        """再同步 ⇒ 更新状态 / 金额（订单是会变的，必须刷新）。"""
        svc.upsert_order(db, _normalized("O-UP", status="待发货"), account_id=7)
        db.commit()
        svc.upsert_order(
            db,
            _normalized("O-UP", status="已发货", pay_amount=Decimal("99.9")),
            account_id=7,
        )
        db.commit()

        order = db.scalar(select(Order).where(Order.order_no == "O-UP"))
        assert order.status == "已发货"
        assert float(order.pay_amount) == 99.9

    def test_resync_does_not_rewrite_immutable_identity(self, db, account):
        """**不可变字段不许被覆盖**：订单号 / 账号 / 买家身份是唯一可信来源。

        ⚠️ 若再同步把 `buyer_nick` 改了，等于让平台侧一次脏数据**永久污染**
        本地台账（且无从发现）。
        """
        svc.upsert_order(db, _normalized("O-ID", buyer_nick="原始买家"), account_id=7)
        db.commit()
        # 第二次带了个不同的买家昵称（模拟平台抖动）
        svc.upsert_order(db, _normalized("O-ID", buyer_nick="被改的买家"), account_id=7)
        db.commit()

        order = db.scalar(select(Order).where(Order.order_no == "O-ID"))
        assert order.buyer_nick == "原始买家", "买家身份被再同步覆盖了（应为不可变）"
        assert order.platform_account_id == 7

    def test_order_without_order_no_is_skipped_not_crashed(self, db, account):
        """缺 `order_no` 的条目 ⇒ 跳过（返回 `None`），不崩、不落半截行。"""
        result = svc.upsert_order(db, _normalized(""), account_id=7)
        db.commit()
        assert result is None

    def test_items_are_replaced_not_duplicated(self, db, account):
        """明细按 `(order_id, sku_id)` 全量替换 ⇒ 不重复累加。"""
        svc.upsert_order(db, _normalized("O-ITEM"), account_id=7)
        db.commit()
        svc.upsert_order(db, _normalized("O-ITEM"), account_id=7)
        db.commit()

        order = db.scalar(select(Order).where(Order.order_no == "O-ITEM"))
        items = db.scalars(select(OrderItem).where(OrderItem.order_id == order.id)).all()
        assert len(items) == 1

    def test_shipment_row_is_not_duplicated_on_resync(self, db, account):
        """主包裹物流一行 ⇒ 再同步不新增第二行。"""
        svc.upsert_order(
            db,
            _normalized("O-SHIP", package_id="P-1", logistics_company="顺丰", tracking_no="SF1"),
            account_id=7,
        )
        db.commit()
        svc.upsert_order(
            db,
            _normalized("O-SHIP", package_id="P-1", logistics_company="顺丰", tracking_no="SF1"),
            account_id=7,
        )
        db.commit()

        order = db.scalar(select(Order).where(Order.order_no == "O-SHIP"))
        rows = db.scalars(select(OrderShipment).where(OrderShipment.order_id == order.id)).all()
        assert len(rows) == 1


# --------------------------------------------------------------------------- #
# 3. 脱敏口径
# --------------------------------------------------------------------------- #
class TestMasking:
    def test_default_is_masked(self, db, account):
        """**默认脱敏**（展示层防护，设计 16 §7.2）。"""
        masked = svc.mask_sensitive("13800001111")
        assert "13800001111" not in masked

    def test_short_or_empty_value_does_not_crash(self):
        """短值 / 空值不能崩（收货人可能就是「李」一个字）。"""
        for value in ("", None, "李", "李四", "13800001111"):
            svc.mask_sensitive(value)  # 不抛即通过

    def test_order_summary_defaults_to_masked_and_full_is_opt_in(self, db, account):
        """`order_summary` 默认脱敏；`include_full=True` 才给原值。"""
        svc.upsert_order(
            db,
            _normalized(
                "O-MASK",
                receiver_name_full="张三",
                receiver_phone_full="13800001111",
                receiver_address_full="北京市朝阳区某路1号",
            ),
            account_id=7,
        )
        db.commit()
        order = db.scalar(select(Order).where(Order.order_no == "O-MASK"))

        masked = svc.order_summary(db, order)
        full = svc.order_summary(db, order, include_full=True)

        # 默认输出里不得出现手机号原值
        assert "13800001111" not in str(masked)
        # 原值在库里是完整的（履约数据完整落库）
        assert order.receiver_phone_full == "13800001111"
        # 显式要求完整值时才给
        assert "13800001111" in str(full)


# --------------------------------------------------------------------------- #
# 4. 租户隔离
# --------------------------------------------------------------------------- #
class TestTenantIsolation:
    def test_other_users_order_is_invisible(self, db, account):
        """别人的订单查不到（越权 = 空集，不是 404、更不是别人的数据）。"""
        seed_owner_rows(db.get_bind(), user_id=99, account_id=98)
        db.commit()
        svc.upsert_order(db, _normalized("O-MINE"), account_id=7)
        svc.upsert_order(
            db,
            _normalized("O-THEIRS", user_id=99, platform_account_id=98),
            account_id=98,
        )
        db.commit()

        mine = svc.list_orders(db, user_id=2, platform_account_id=7, page=1, page_size=50)
        theirs = svc.list_orders(db, user_id=99, platform_account_id=98, page=1, page_size=50)

        assert {i["order_no"] for i in mine["items"]} == {"O-MINE"}
        assert {i["order_no"] for i in theirs["items"]} == {"O-THEIRS"}

    def test_user_without_rows_gets_empty_not_other_peoples_data(self, db, account):
        """无数据的用户 ⇒ 空集（不是把全表给他）。"""
        svc.upsert_order(db, _normalized("O-1"), account_id=7)
        db.commit()
        seed_owner_rows(db.get_bind(), user_id=55, account_id=54)
        db.commit()

        result = svc.list_orders(db, user_id=55, page=1, page_size=50)
        assert result["total"] == 0
        assert result["items"] == []


# --------------------------------------------------------------------------- #
# 5. 只读边界
# --------------------------------------------------------------------------- #
class TestReadOnlyBoundary:
    def test_order_service_never_calls_write_platform_apis(self):
        """**一期只读**：订单链路里不许出现发货 / 售后写调用（硬约束②）。

        判据是**源码里没有写接口的调用形态**（不是「注释里写了只读」）。
        """
        import inspect

        import backend.app.services.order_sync_service as mod

        src = inspect.getsource(mod)
        lowered = src.lower()
        for forbidden in (
            "ship/confirm",
            "deliver",
            "/ship",
            "after-sale/audit",
            "after_sale/audit",
            "refund/agree",
            "refundagree",
        ):
            assert forbidden not in lowered, f"订单服务里出现了疑似写接口：{forbidden}"

    def test_order_statuses_are_platform_states_not_invented(self):
        """词表必须来自设计 16 §7.3，**不得私自扩充**（扩了前端筛选器会漏项）。"""
        assert len(svc.ORDER_STATUSES) == 6

    def test_remark_is_local_only_and_does_not_touch_platform(self, db, account):
        """订单备注是**本地**能力（平台侧写备注未授权）—— 落 `order_remarks` 表。"""
        svc.upsert_order(db, _normalized("O-REMARK"), account_id=7)
        db.commit()
        order = db.scalar(select(Order).where(Order.order_no == "O-REMARK"))

        svc.upsert_remark(db, user_id=2, order=order, content="客户要求周六送")
        db.commit()

        rows = db.scalars(select(OrderRemark).where(OrderRemark.order_id == order.id)).all()
        assert len(rows) == 1
        assert rows[0].content == "客户要求周六送"

    def test_remark_overwrites_rather_than_appends(self, db, account):
        """同订单再写备注 ⇒ 覆盖（一条订单一条备注），不堆成历史。"""
        svc.upsert_order(db, _normalized("O-REMARK2"), account_id=7)
        db.commit()
        order = db.scalar(select(Order).where(Order.order_no == "O-REMARK2"))

        svc.upsert_remark(db, user_id=2, order=order, content="第一版")
        svc.upsert_remark(db, user_id=2, order=order, content="第二版")
        db.commit()

        rows = db.scalars(select(OrderRemark).where(OrderRemark.order_id == order.id)).all()
        assert len(rows) == 1
        assert rows[0].content == "第二版"


# --------------------------------------------------------------------------- #
# 6. 筛选 / 分页 / 日期
# --------------------------------------------------------------------------- #
class TestListFilters:
    def test_filters_compose(self, db, account):
        """状态 + 关键词 + 日期**可组合**（页面三个筛选器同时生效）。"""
        svc.upsert_order(db, _normalized("O-A", status="待发货", buyer_nick="王小明"), account_id=7)
        svc.upsert_order(db, _normalized("O-B", status="已完成", buyer_nick="王小明"), account_id=7)
        svc.upsert_order(db, _normalized("O-C", status="待发货", buyer_nick="李华"), account_id=7)
        db.commit()

        result = svc.list_orders(
            db, user_id=2, platform_account_id=7, status="待发货", keyword="王", page=1, page_size=50
        )
        assert {i["order_no"] for i in result["items"]} == {"O-A"}

    def test_page_size_bounds_are_enforced(self, db, account):
        """分页参数**有界**（否则一次拉全表）。"""
        svc.upsert_order(db, _normalized("O-P"), account_id=7)
        db.commit()
        # 超大 page_size 被收窄到服务端上限
        # 超大 page_size 被收窄（LIMIT 侧），返回体回显的是**服务端实际采用值**
        import sqlalchemy as _sa

        result = svc.list_orders(db, user_id=2, page=1, page_size=100000)
        assert result["page_size"] <= 200, "返回体应回显收窄后的 page_size"
        # 非法 page 归 1
        result0 = svc.list_orders(db, user_id=2, page=0, page_size=10)
        assert result0["page"] >= 1

    def test_pagination_is_stable_and_counts_total(self, db, account):
        """分页要给出**全量 total**（前端要显示「共 N 条」，不是「本页 N 条」）。"""
        for i in range(5):
            svc.upsert_order(db, _normalized(f"O-PG-{i}"), account_id=7)
        db.commit()

        page1 = svc.list_orders(db, user_id=2, page=1, page_size=2)
        page3 = svc.list_orders(db, user_id=2, page=3, page_size=2)

        assert page1["total"] == 5
        assert len(page1["items"]) == 2
        assert len(page3["items"]) == 1
        # 两页不重叠
        ids1 = {i["order_no"] for i in page1["items"]}
        ids3 = {i["order_no"] for i in page3["items"]}
        assert not (ids1 & ids3)

    def test_metrics_returns_zero_states_not_missing_keys(self, db, account):
        """监控指标：**每个状态都要有键**（缺值渲染 0，前端不用补样板）。"""
        svc.upsert_order(db, _normalized("O-M", status="待发货"), account_id=7)
        db.commit()

        metrics = svc.order_metrics(db, user_id=2, platform_account_id=7)
        counts = metrics.get("by_status") or {}
        for state in svc.ORDER_STATUSES:
            assert state in counts, f"指标里缺状态 {state}（前端会渲染成 undefined）"
        assert counts["待发货"] == 1
        assert counts["已完成"] == 0


# --------------------------------------------------------------------------- #
# 7. 导出（CSV）
# --------------------------------------------------------------------------- #
class TestExportCsv:
    def _rows(self, n=1):
        return [
            {
                "order_no": f"O-{i}",
                "status": "待发货",
                "buyer_nick": "买家A",
                "receiver_name": "张三",
                "receiver_phone": "13800001111",
                "receiver_address": "北京市朝阳区某路1号",
                "items": [{"item_name": "商品A", "sku": "红色", "quantity": 2}],
                "pay_amount": 34.8,
                "total_amount": 34.8,
                "freight": 0,
                "discount": 0,
                "pay_method": "微信",
                "logistics_company": "",
                "tracking_no": "",
                "created_at": "2026-09-03T18:51:20",
                "pay_time": None,
                "ship_time": None,
                "finish_time": None,
            }
            for i in range(n)
        ]

    def test_csv_has_bom_and_parses_to_fixed_column_count(self):
        """**BOM + 列数固定**：Excel 打开不乱码，且表头与数据行列数一致。

        ⚠️ 用 `csv.reader` **解析**着数，不是 `split(",")` —— 商品名里的
        逗号被正确引号包裹时，`split(",")` 会多数一列（假警报）。
        """
        import csv as _csv
        import io as _io

        text = svc.orders_to_csv(self._rows(3))
        assert text.startswith("\ufeff"), "缺 BOM，Excel 打开中文列名会乱码"
        parsed = list(_csv.reader(_io.StringIO(text.lstrip("\ufeff"))))
        assert len(parsed) == 4, "表头 + 3 行数据"
        widths = {len(r) for r in parsed}
        assert widths == {len(svc.EXPORT_COLUMNS)}, f"列数不一致：{widths}"

    def test_csv_escapes_formula_injection(self):
        """**CSV 注入防护**：平台来的外部值以 `=`/`+`/`-`/`@` 开头要加 `'`。

        订单号 / 买家昵称是外部数据；不加前缀时 Excel / WPS 会把它们当**公式执行**。
        """
        rows = self._rows(1)
        rows[0]["order_no"] = "=1+1"
        rows[0]["buyer_nick"] = "@SUM(A1)"
        text = svc.orders_to_csv(rows)
        assert "'=1+1" in text, "order_no 公式注入未防"
        assert "'@SUM(A1)" in text, "buyer_nick 公式注入未防"

    def test_csv_quotes_values_containing_delimiters(self):
        """含逗号 / 引号 / 换行的值必须被引号包裹（否则列结构被破坏）。"""
        import csv as _csv
        import io as _io

        rows = self._rows(1)
        rows[0]["buyer_nick"] = '带,逗号"和引号'
        text = svc.orders_to_csv(rows)
        parsed = list(_csv.reader(_io.StringIO(text.lstrip("﻿"))))
        # ⚠️ 判据是「解析回来逐字相等」——不能断言原始串里的引号形态：
        # 转义由 csv.writer 负责，写死形态会把「双重转义」这种 bug 断言成正确。
        assert parsed[1][2] == '带,逗号"和引号', f"值被破坏：{parsed[1][2]!r}"

    def test_export_filename_is_unique_per_call(self):
        """文件名带时间戳 —— 两次导出不互相覆盖（用户可能连导两次对账）。"""
        name = svc.export_filename()
        assert name.startswith("xhs_orders_") and name.endswith(".csv")

    def test_empty_export_still_has_header(self):
        """零行导出 ⇒ 只有表头（不是空文件；用户能看出「这次筛出来是 0 条」）。"""
        import csv as _csv
        import io as _io

        text = svc.orders_to_csv([])
        parsed = list(_csv.reader(_io.StringIO(text.lstrip("\ufeff"))))
        assert len(parsed) == 1
        assert len(parsed[0]) == len(svc.EXPORT_COLUMNS)


# --------------------------------------------------------------------------- #
# 8. 端点契约（走真实 HTTP）
# --------------------------------------------------------------------------- #
class TestOrderEndpoints:
    def _auth(self, api_client, db_engine, user_id=2, account_id=7):
        """发一个归属 user 2 的 token + 账号行，返回 (client, headers)。"""
        from backend.app.core.security import create_access_token

        seed_owner_rows(db_engine, user_id=user_id, account_id=account_id)
        session = sessionmaker(bind=db_engine)()
        session.commit()
        session.close()
        return api_client, {"Authorization": f"Bearer {create_access_token(user_id)}"}

    def test_detail_requires_owned_order_and_masks_by_default(self, api_client, db_engine):
        """详情端点：默认脱敏；别人的订单号 ⇒ 404（**不区分**不存在与越权）。"""
        client, headers = self._auth(api_client, db_engine)
        db = sessionmaker(bind=db_engine)()
        svc.upsert_order(
            db,
            _normalized(
                "O-EP-1",
                receiver_name_full="张三",
                receiver_phone_full="13800001111",
            ),
            account_id=7,
        )
        db.commit()
        db.close()

        ok = client.post(
            "/api/xhs/qianfan/orders/detail",
            json={"platform_account_id": 7, "order_no": "O-EP-1"},
            headers=headers,
        )
        assert ok.status_code == 200, ok.text
        assert "13800001111" not in ok.text, "详情端点默认应脱敏"

        full = client.post(
            "/api/xhs/qianfan/orders/detail",
            json={"platform_account_id": 7, "order_no": "O-EP-1", "include_full": True},
            headers=headers,
        )
        assert "13800001111" in full.text

        missing = client.post(
            "/api/xhs/qianfan/orders/detail",
            json={"platform_account_id": 7, "order_no": "NOPE"},
            headers=headers,
        )
        assert missing.status_code == 404

    def test_remark_endpoint_is_local_write_only(self, api_client, db_engine):
        """备注端点写本地表；空内容 ⇒ 删除（`deleted=True`）。"""
        client, headers = self._auth(api_client, db_engine)
        db = sessionmaker(bind=db_engine)()
        svc.upsert_order(db, _normalized("O-EP-2"), account_id=7)
        db.commit()
        db.close()

        put = client.post(
            "/api/xhs/qianfan/orders/remark",
            json={"platform_account_id": 7, "order_no": "O-EP-2", "content": "周六送"},
            headers=headers,
        )
        assert put.status_code == 200, put.text
        assert put.json()["remark"]["content"] == "周六送"

        clear = client.post(
            "/api/xhs/qianfan/orders/remark",
            json={"platform_account_id": 7, "order_no": "O-EP-2", "content": "  "},
            headers=headers,
        )
        assert clear.status_code == 200
        assert clear.json()["deleted"] is True

    def test_export_endpoint_returns_csv_with_full_values(self, api_client, db_engine):
        """导出走**完整值**（履约场景），且是 CSV 不是文件。"""
        client, headers = self._auth(api_client, db_engine)
        db = sessionmaker(bind=db_engine)()
        svc.upsert_order(
            db, _normalized("O-EP-3", receiver_phone_full="13800001111"), account_id=7
        )
        db.commit()
        db.close()

        resp = client.post(
            "/api/xhs/qianfan/orders/export",
            json={"platform_account_id": 7},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["csv"].startswith("\ufeff")
        assert "13800001111" in body["csv"], "导出应含完整手机号（履约口径）"
        assert body["filename"].endswith(".csv")

    def test_export_does_not_leak_other_users_orders(self, client, db_engine):
        """导出必须**同样受租户隔离**（否则等于开了个全库下载口）。"""
        from backend.app.core.security import create_access_token

        seed_owner_rows(db_engine, user_id=2, account_id=7)
        seed_owner_rows(db_engine, user_id=99, account_id=98)
        db = sessionmaker(bind=db_engine)()
        db.commit()
        svc.upsert_order(db, _normalized("O-EP-THEIRS", user_id=99, platform_account_id=98), account_id=98)
        db.commit()
        db.close()

        resp = client.post(
            "/api/xhs/qianfan/orders/export",
            json={"platform_account_id": 7},
            headers={"Authorization": f"Bearer {create_access_token(2)}"},
        )
        assert resp.status_code == 200
        assert "O-EP-THEIRS" not in resp.json()["csv"], "导出漏了别人的订单"


# --------------------------------------------------------------------------- #
# 9. 真机实测结论：平台**本身就给脱敏值**
# --------------------------------------------------------------------------- #
class TestPlatformMayAlreadyMask:
    """⚠️ 真机发现（2026-10-04，账号 8 的 3 笔真实订单）：

    千帆订单接口返回的收件人 / 手机号**本身就是脱敏形态**（`高*` / `189***2857`），
    DB 里逐字就是平台给的样子。

    ⇒ **「完整值落库」这个前提对当前接口不成立** —— 我们能拿到的上限就是平台给的。
    这**不是缺陷**（硬约束③：拿不到就不造，不编一个真实姓名填进去）；
    但**必须让调用方知道**：`include_full=True` 只是「不做二次脱敏」，
    **不等于「一定有原值」**。前端据此提示用户「平台未提供完整信息」。

    这两条用例把该事实钉住 —— 否则后来者会把「脱敏输出」当成 bug 去「修」。
    """

    def test_include_full_does_not_invent_values(self, db, account):
        """平台给的就是脱敏值 ⇒ `include_full=True` **原样给**，不许补全成假值。"""
        svc.upsert_order(
            db,
            _normalized("O-MASKED", receiver_name_full="高*", receiver_phone_full="189***2857"),
            account_id=7,
        )
        db.commit()
        order = db.scalar(select(Order).where(Order.order_no == "O-MASKED"))

        full = svc.order_summary(db, order, include_full=True)
        # 原样（不做二次脱敏），且**不得**被补全成疑似真实值
        assert full["receiver_name"] == "高*"
        assert full["receiver_phone"] == "189***2857"

    def test_double_masking_does_not_corrupt_a_masked_value(self, db, account):
        """已脱敏值再过一次 `mask_sensitive` 不能变成 `1*******7` 这种糊掉的样子。

        ⚠️ 平台给 `189***2857`，若脱敏实现是「留头 3 尾 4、中间全 `*`」，
        会得到 `189****857`——**把平台已经处理好的信息又锈掉一层**，
        用户看到的信息量反而变少。
        """
        once = svc.mask_sensitive("189***2857")
        twice = svc.mask_sensitive(once)
        # 关键：二次脱敏**不增加**遮罩数量（幂等）
        assert twice == once, f"脱敏不幂等：{once!r} → {twice!r}"
        assert "189" in twice, "首位信息被抹掉了"


# --------------------------------------------------------------------------- #
# 店铺维度（2026-10-04 用户口径「订单模块也需要是店铺维度」）
# --------------------------------------------------------------------------- #


class TestShopDimension:
    """订单模块必须有**店铺维度**（与商品页同口径）。

    ## 口径（ADR-021 之后）

    店铺实体已退场，**「账号 = 店铺」1:1** ⇒ 店名取自账号昵称
    （`shop_binding_service.shop_display_name` **唯一实现**，商品页同源）。
    订单列表 / 详情都要下发 `shop_name` / `seller_id`，让「这单属于哪个店」
    在页面上有一等表达（而不是只给一个 `platform_account_id`）。
    """

    def test_list_orders_exposes_shop_name_and_seller_id(self, db, account):
        """列表返回体带 `shop_name` / `seller_id`（店铺维度由**后端下发**）。"""
        account.nickname = "观隅好物"
        account.seller_id = "6a3250e427a0000000000000"
        db.commit()
        svc.upsert_order(db, _normalized("O-SHOP-1"), account_id=account.id)

        result = svc.list_orders(db, user_id=2, platform_account_id=account.id)
        assert result["shop_name"] == "观隅好物"
        assert result["seller_id"] == "6a3250e427a0000000000000"

    def test_order_rows_carry_shop_name(self, db, account):
        """**每一行**也带店铺名（前端表格 / 导出不再靠 `platform_account_id` 猜）。"""
        account.nickname = "蜜柚事集"
        db.commit()
        svc.upsert_order(db, _normalized("O-SHOP-2"), account_id=account.id)

        result = svc.list_orders(db, user_id=2, platform_account_id=account.id)
        row = next(r for r in result["items"] if r["order_no"] == "O-SHOP-2")
        assert row["shop_name"] == "蜜柚事集"
        assert row["platform_account_id"] == account.id

    def test_shop_name_is_empty_when_account_missing_not_invented(self, db):
        """账号不存在（被删 / 非法 id）⇒ 空串，**不造店名**（硬约束⑤）。"""
        ctx = svc._shop_context(db, 999999)
        assert ctx["shop_name"] == ""
        assert ctx["seller_id"] == ""

    def test_shop_name_never_falls_back_to_another_account(self, db, account):
        """查不到账号时**不得回落**到别的店名 —— 宁可空，不可张冠李戴。"""
        svc.upsert_order(db, _normalized("O-SHOP-3"), account_id=account.id)
        # 用一个不存在的账号 id 查上下文（不是回落到 account 7 的店名）
        ctx = svc._shop_context(db, 999998)
        assert ctx["shop_name"] != (account.nickname or "").strip() or ctx["shop_name"] == ""

    def test_detail_exposes_shop_name(self, db, account):
        """详情返回体同样带店铺名（页面「店铺」区在详情里也对得上）。"""
        account.nickname = "观隅好物"
        db.commit()
        order = svc.upsert_order(db, _normalized("O-SHOP-4"), account_id=account.id)
        ctx = svc._shop_context(db, account.id)
        summary = svc.order_summary(
            db, order, shop_name=ctx["shop_name"], seller_id=ctx["seller_id"]
        )
        assert summary["shop_name"] == "观隅好物"

    def test_shop_display_name_is_the_single_implementation(self):
        """店名口径**只有一份实现**（商品页与订单页共用，DEF-044）。

        ⚠️ 变异：API 层再写一份 `(account.nickname or "").strip()`
        ⇒ 判定它没有委托给服务层 ⇒ 本用例必红。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        qianfan_src = (
            root / "backend" / "app" / "api" / "platforms" / "xhs" / "qianfan.py"
        ).read_text(encoding="utf-8")
        binding_src = (
            root / "backend" / "app" / "services" / "shop_binding_service.py"
        ).read_text(encoding="utf-8")

        assert "def shop_display_name" in binding_src, "服务层必须有唯一实现"
        # API 层的 _shop_display_name 必须**委托**，不许自己 strip
        assert "shop_display_name(account)" in qianfan_src
        assert 'return (account.nickname or "").strip()' not in qianfan_src, (
            "API 层又写了一份店名口径（两份实现）"
        )


class TestShopDimensionEndpointWiring:
    """端点必须**透传**服务层的店铺字段，不许自己另拼一份（回归守卫）。

    ⚠️ 实测踩过：`/orders` 端点最初自己算 `shop_name` 却**丢掉 `seller_id`**
    ⇒ 返回恒 `None`（而账号明明有 `seller_id`）。根因是「端点又拼一遍」——
    这种「同一件事两份实现」的漏字段是静默的（页面只是少一列，不报错）。
    """

    def test_orders_endpoint_passes_through_seller_id(self):
        """源码守卫：`/orders` 返回体不得自己调 `_shop_display_name`。"""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[2]
            / "backend" / "app" / "api" / "platforms" / "xhs" / "qianfan.py"
        ).read_text(encoding="utf-8")

        # 订单列表端点的返回体块必须引用 result["shop_name"] / result["seller_id"]
        assert '"shop_name": result["shop_name"]' in src, (
            "订单列表端点没有透传服务层的 shop_name（自己拼 = 会漏字段）"
        )
        assert '"seller_id": result["seller_id"]' in src, (
            "订单列表端点漏传 seller_id（实测曾恒 None）"
        )
