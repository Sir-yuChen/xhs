"""实时平台查询服务（设计 75 §2.1）—— 功能点测试。

`tests/backend/test_realtime_query.py`（单文件、四段结构，见
`docs/04_测试文档/12_功能点测试规范.md`）：

① 鉴权与越权 —— 本层无 HTTP 端点，鉴权在路由层；这一段的**等价判据**是
   「非法入参不发平台请求」（确定性答案不打平台，与鉴权同为「拦在出口之前」）。
② 主链路 —— mock adapter，断言**参数映射**与**返回归一化**。
③ 关键失败面（≤3）—— 会导致静默错误的那几种。
④ 不变量 —— 订单窗口必有界 / SKU 分片 ≤10 / 响应形状。

用例预算：20（本文件 15 条）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.services import realtime_platform_query_service as rt

#: 断言里用的固定"现在"（避免依赖真实时钟导致边界抖动）。
NOW = datetime(2026, 10, 27, 12, 0, 0)


def _localtime(year: int, month: int, day: int) -> datetime:
    """东八区某天的零点，去掉 tzinfo（与服务层 `_to_epoch_s` 的口径一致）。"""
    return datetime(year, month, day, tzinfo=timezone(timedelta(hours=8))).replace(tzinfo=None)


def _envelope(data: dict) -> dict:
    """SDK 的真实返回形状：**完整响应体**，业务数据在 `data` 下。

    ⚠️ 这是本文件最重要的一件事 —— 旧实现直接读 `payload.get("items")`，
    对真机响应恒为空且**不报错**。替身若不模拟 `data` 包裹，这个缺陷就测不出来。
    """
    return {"code": 0, "success": True, "msg": "成功", "data": data}


class FakeAdapter:
    """只实现本服务调用的五个只读方法，记录调用参数。"""

    def __init__(self, *, items=None, skus_map=None, packages=None, notes=None, fail=None):
        self._items = items or []
        self._skus_map = skus_map if skus_map is not None else {}
        self._packages = packages or []
        self._notes = notes or []
        self._fail = fail  # 方法名 → 失败时的响应体
        self.calls: list[tuple[str, dict]] = []

    def _fail_for(self, name, kwargs):
        if self._fail == name:
            self.calls.append((name, kwargs))
            return (-1, "千帆接口返回 code=-1"), {
                "code": -1,
                "success": False,
                "msg": "千帆接口返回 code=-1",
            }
        return None

    def search_items(self, **kwargs):
        self.calls.append(("search_items", kwargs))
        failed = self._fail_for("search_items", kwargs)
        if failed:
            return False, failed[0], failed[1]
        return True, "成功", _envelope({"items": self._items, "total": len(self._items)})

    def get_skus_by_item_ids(self, item_ids):
        kwargs = {"item_ids": list(item_ids)}
        self.calls.append(("get_skus_by_item_ids", kwargs))
        failed = self._fail_for("get_skus_by_item_ids", kwargs)
        if failed:
            return False, failed[0], failed[1]
        chunk = {i: self._skus_map[i] for i in item_ids if i in self._skus_map}
        return True, "成功", _envelope({"skusMap": chunk})

    def get_order_page(self, **kwargs):
        self.calls.append(("get_order_page", kwargs))
        failed = self._fail_for("get_order_page", kwargs)
        if failed:
            return False, failed[0], failed[1]
        return True, "成功", _envelope({"packages": self._packages, "total": len(self._packages)})

    def get_order_detail(self, package_id):
        kwargs = {"package_id": package_id}
        self.calls.append(("get_order_detail", kwargs))
        failed = self._fail_for("get_order_detail", kwargs)
        if failed:
            return False, failed[0], failed[1]
        return True, "成功", _envelope(
            {"fulfillment_package": {"packageId": package_id, "orderId": "O-" + package_id}}
        )

    def get_goods_note_page(self, page_num=1, page_size=20, **kwargs):
        merged = {"page_num": page_num, "page_size": page_size, **kwargs}
        self.calls.append(("get_goods_note_page", merged))
        failed = self._fail_for("get_goods_note_page", merged)
        if failed:
            return False, failed[0], failed[1]
        return True, "成功", _envelope(
            {"note_infos": self._notes, "total": len(self._notes), "end_tag": True}
        )


# --------------------------------------------------------------------------- #
# ① 入参校验（等价于「鉴权」：拦在出口之前，不发平台请求）
# --------------------------------------------------------------------------- #
class TestSegment1_InputGuards:
    def test_order_window_inverted_rejected_without_platform_call(self):
        """起点晚于终点 ⇒ 抛错，**一个平台请求都不发**（无界/非法窗口实测挂死 15s）。"""
        fake = FakeAdapter()
        with pytest.raises(rt.OrderWindowRequired) as exc:
            rt.list_orders(fake, start_time="2026-10-20", end_time="2026-10-01", now=NOW)
        assert exc.value.code == "ORDER_WINDOW_REQUIRED"
        assert fake.calls == []

    @pytest.mark.parametrize("item_ids", [[], ["  ", ""], None])
    def test_empty_item_ids_does_not_call_platform(self, item_ids):
        fake = FakeAdapter()
        with pytest.raises(rt.RealtimeQueryError) as exc:
            rt.get_product_detail(fake, item_ids=item_ids)
        assert exc.value.code == "INVALID_ARGUMENT"
        assert "item_ids" in exc.value.message
        assert fake.calls == []

    def test_goods_notes_without_any_condition_rejected(self):
        """三个条件全空 ⇒ 抛错（不带条件翻页 = 把整店铺笔记当结果，绝不是调用方要的）。"""
        fake = FakeAdapter()
        with pytest.raises(rt.RealtimeQueryError) as exc:
            rt.list_goods_notes(fake)
        assert exc.value.code == "INVALID_ARGUMENT"
        assert fake.calls == []


# --------------------------------------------------------------------------- #
# ② 主链路（参数映射 + 归一化）
# --------------------------------------------------------------------------- #
class TestSegment2_MainPaths:
    def test_list_products_maps_params_and_reads_data_envelope(self):
        """商品列表：参数按平台口径映射（分 / 秒 / card_type）+ 从 `data` 取行。"""
        fake = FakeAdapter(
            items=[
                {
                    "item_id": "A1",
                    "item_name": "商品甲",
                    "min_price": 1280,
                    "create_time": 1_790_000_000_000,
                    "on_shelf_time": 1_790_000_000_000,
                    "sale_qty_info": {"sale_qty30": 7},
                }
            ]
        )
        result = rt.list_products(
            fake,
            keyword="甲",
            status="on_shelf",
            page=2,
            page_size=500,  # 超上限 ⇒ 截到 100
            price_min_fen=100,
            create_from="2026-07-01",
        )

        assert result["source"] == "platform"
        assert result["total"] == 1
        row = result["items"][0]
        assert (row["item_id"], row["title"], row["price_min_fen"]) == ("A1", "商品甲", 1280)
        assert row["status_key"] == "on_shelf" and row["status_label"] == "售卖中"
        assert row["sales_30d"] == 7
        # 毫秒 → 东八区文本（不渲染 1970）
        assert row["create_time"].startswith("2026-")

        _, kwargs = fake.calls[0]
        assert (kwargs["page_no"], kwargs["page_size"]) == (2, 100)
        assert kwargs["keyword"] == "甲"
        assert kwargs["status"] == "on_shelf"
        assert kwargs["price_from_fen"] == 100
        # 创建时间转 Unix **秒**（毫秒会被下游 ES 拒成 HTTP 500）：本地 2026-07-01 00:00
        assert kwargs["create_time_from"] == int(_localtime(2026, 7, 1).timestamp())

    def test_list_products_accepts_millis_and_seconds(self):
        """创建时间三种入参都落到「Unix 秒」：日期串 / 毫秒 / 秒（**不许静默丢掉**）。

        实测过的坑：端点侧算出来的是毫秒，只认日期串时会被当成「没给」
        静默丢弃 ⇒ 页面筛了时间却返回全量，且零报错。
        """
        fake = FakeAdapter()
        expected = int(_localtime(2026, 7, 1).timestamp())

        rt.list_products(fake, create_from="2026-07-01")
        rt.list_products(fake, create_from=expected * 1000)  # 13 位毫秒
        rt.list_products(fake, create_from=expected)  # 10 位秒
        rt.list_products(fake, create_from=0)  # 0 = 没给

        got = [c[1]["create_time_from"] for c in fake.calls]
        assert got == [expected, expected, expected, None]

    def test_list_products_unknown_status_means_all(self):
        """`status` 坏值 / `all` ⇒ SDK 收到 `all`（= 不传 card_type，不静默少状态）。"""
        fake = FakeAdapter()
        rt.list_products(fake, status="all")
        rt.list_products(fake, status="不认识的状态")
        assert [c[1]["status"] for c in fake.calls] == ["all", "all"]

    def test_get_product_detail_splits_and_normalizes(self):
        """23 个 ID ⇒ 3 片，每片 ≤10；SKU 归一化白名单；`missing` 如实报。"""
        fake = FakeAdapter(
            skus_map={
                f"i{n}": [
                    {
                        "skuId": f"s{n}",
                        "skuName": "规格",
                        "basePriceInfo": {"price": 990},
                        "baseStockInfos": [{"stock": 5}],
                    }
                ]
                for n in range(23)
                if n != 7  # i7 平台没返回 ⇒ 进 missing
            }
        )
        ids = [f"i{n}" for n in range(23)]
        result = rt.get_product_detail(fake, item_ids=ids)

        chunks = [c[1]["item_ids"] for c in fake.calls]
        assert len(chunks) == 3 and all(len(c) <= rt.SKU_BATCH_SIZE for c in chunks)
        assert result["total"] == 22 and result["missing"] == ["i7"]
        sku = result["items"][0]["skus"][0]
        assert (sku["sku_id"], sku["price_fen"], sku["stock"]) == ("s0", 990, 5)

    def test_get_product_detail_dedupes_ids(self):
        """同一 ID 传两次 ⇒ 只查一次、只回一条（不产生重复明细）。"""
        fake = FakeAdapter(skus_map={"A1": [{"skuId": "s1"}]})
        result = rt.get_product_detail(fake, item_ids=["A1", " A1 ", "A1"])
        assert [c[1]["item_ids"] for c in fake.calls] == [["A1"]]
        assert result["total"] == 1

    def test_list_goods_notes_uses_item_mix_search_word(self):
        """按商品查笔记只走 `item_mix_search_word`（与 note_ids / mix_search_word 互斥）。"""
        fake = FakeAdapter(
            notes=[
                {
                    "note_id": "N1",
                    "note_title": "笔记一",
                    "browse_num": {"value": 40},
                    "goods_infos": [{"item_id": "A1", "item_title": "商品甲"}],
                    "note_flag": "goods_note",
                }
            ]
        )
        result = rt.list_goods_notes(fake, item_id="A1", page=1, page_size=20)

        assert result["total"] == 1 and result["has_more"] is False
        note = result["items"][0]
        assert note["note_id"] == "N1" and note["read_count"] == 40
        assert note["note_flag"] == "goods_note"
        assert note["goods"] == [{"item_id": "A1", "title": "商品甲", "image": ""}]

        _, kwargs = fake.calls[0]
        assert kwargs["item_mix_search_word"] == "A1"
        assert "mix_search_word" not in kwargs and "note_ids" not in kwargs

    @pytest.mark.parametrize(
        "call_kwargs,expected_key,expected_value",
        [
            ({"note_ids": ["N1", "N2"]}, "note_ids", ["N1", "N2"]),
            ({"mix_search_word": "地垫"}, "mix_search_word", "地垫"),
        ],
    )
    def test_list_goods_notes_alternate_entries(self, call_kwargs, expected_key, expected_value):
        """反向（note_ids，复数）/ 标题关键词两个入口各自映射到平台参数名。"""
        fake = FakeAdapter()
        rt.list_goods_notes(fake, **call_kwargs)
        _, kwargs = fake.calls[0]
        assert kwargs[expected_key] == expected_value
        assert "item_mix_search_word" not in kwargs

    def test_list_orders_fills_bounded_window_and_normalizes(self):
        """缺窗口 ⇒ 自动补成有界区间；订单行归一化（金额两位文本 / 脱敏原样）。"""
        fake = FakeAdapter(
            packages=[
                {
                    "orderId": "O1",
                    "packageId": "P1",
                    "status": 7,
                    "statusDesc": "已完成",
                    "totalOrderAmount": 61.7,
                    "userInfo": {"nickName": "买家", "phone": "184***3034"},
                    "skus": [{"skuId": "s1", "skuQuantity": 2, "skuSoldPrice": 30}],
                }
            ]
        )
        result = rt.list_orders(fake, now=NOW)

        assert result["total"] == 1
        assert result["end_time"] == "2026-10-27 12:00:00"
        assert result["start_time"] == "2025-10-27 00:00:00"
        order = result["items"][0]
        assert (order["order_no"], order["amount"], order["paid_amount"]) == ("O1", "61.70", None)
        assert order["receiver"]["phone"] == "184***3034"  # 平台脱敏值原样，不补
        assert order["skus"][0]["quantity"] == 2

        _, kwargs = fake.calls[0]
        assert kwargs["start_time_ms"] < kwargs["end_time_ms"]
        assert kwargs["end_time_ms"] == int(NOW.timestamp() * 1000)

    def test_get_order_detail_forwards_package_id(self):
        """入参是 `package_id`（传订单号平台回 code=-1：required param check）。"""
        fake = FakeAdapter()
        result = rt.get_order_detail(fake, package_id="P123")
        assert fake.calls[0][1]["package_id"] == "P123"
        assert result["total"] == 1
        assert result["item"]["order_no"] == "O-P123"
        assert result["items"][0]["delivery_packages"] == []


# --------------------------------------------------------------------------- #
# ③ 关键失败面（≤3：平台失败 / 凭据失效 / 响应结构异常）
# --------------------------------------------------------------------------- #
class TestSegment3_FailureSurfaces:
    def test_platform_failure_raises_structured_error(self):
        """平台失败 ⇒ 抛带 code 的结构化错误（**不回退本地、不给静默空列表**）。"""
        fake = FakeAdapter(fail="search_items")
        with pytest.raises(rt.RealtimeQueryError) as exc:
            rt.list_products(fake)
        assert exc.value.code == "QIANFAN_QUERY_FAILED"
        assert exc.value.as_dict()["code"] == "QIANFAN_QUERY_FAILED"

    def test_credential_expiry_has_its_own_code(self):
        """凭据失效走 SDK 唯一判据（-13000 / -13004）⇒ 独立 code，前端给「重新登录」。"""

        class Expired(FakeAdapter):
            def get_order_page(self, **kwargs):
                self.calls.append(("get_order_page", kwargs))
                return False, "登录凭证已过期, 请重新登录", {"code": -13004, "msg": "登录凭证已过期"}

        fake = Expired()
        with pytest.raises(rt.RealtimeQueryError) as exc:
            rt.list_orders(fake, now=NOW)
        assert exc.value.code == "QIANFAN_CREDENTIAL_EXPIRED"

    def test_missing_data_layer_yields_zero_rows_not_crash(self):
        """响应体没有 `data`（结构变化）⇒ 零行 + `total=None`，不抛也不造假行。"""

        class NoData(FakeAdapter):
            def get_goods_note_page(self, page_num=1, page_size=20, **kwargs):
                return True, "成功", {"code": 0, "success": True}

        result = rt.list_goods_notes(NoData(), item_id="A1")
        assert result["items"] == [] and result["total"] is None
        assert result["source"] == "platform"


# --------------------------------------------------------------------------- #
# ④ 不变量
# --------------------------------------------------------------------------- #
class TestSegment4_Invariants:
    @pytest.mark.parametrize(
        "start,end,expect_start,expect_end",
        [
            # 两端都缺 ⇒ 近 365 天
            (None, None, "2025-10-27 00:00:00", "2026-10-27 12:00:00"),
            # 只给起点 ⇒ 终点取"现在"（不把终点留成 0）
            ("2026-01-01", None, "2026-01-01 00:00:00", "2026-10-27 12:00:00"),
            # 只给终点 ⇒ 起点按 lookback 补
            (None, "2026-03-01 08:30:00", "2025-10-27 00:00:00", "2026-03-01 08:30:00"),
        ],
    )
    def test_order_window_always_bounded(self, start, end, expect_start, expect_end):
        """窗口**任何输入组合下都有界**（无界 = 平台挂死 15 秒，红线）。"""
        got_start, got_end = rt.resolve_order_window(start, end, now=NOW)
        assert got_start.strftime("%Y-%m-%d %H:%M:%S") == expect_start
        assert got_end.strftime("%Y-%m-%d %H:%M:%S") == expect_end
        assert got_start <= got_end

    def test_every_list_exit_carries_source_platform(self):
        """所有列表出口都带 `source=platform` —— 前端 / 排查靠它区分数据来源。"""
        fake = FakeAdapter(skus_map={"A1": [{"skuId": "s1"}]})
        assert rt.list_products(fake)["source"] == "platform"
        assert rt.list_orders(fake, now=NOW)["source"] == "platform"
        assert rt.list_goods_notes(fake, item_id="A1")["source"] == "platform"
        assert rt.get_product_detail(fake, item_ids=["A1"])["source"] == "platform"
        assert rt.get_order_detail(fake, package_id="P1")["source"] == "platform"

    def test_total_missing_is_none_not_page_length(self):
        """平台没给 `total` ⇒ `None`（不拿本页条数冒充分母，否则分页器算错总页数）。"""

        class NoTotal(FakeAdapter):
            def search_items(self, **kwargs):
                self.calls.append(("search_items", kwargs))
                return True, "成功", _envelope({"items": [{"item_id": "A1"}]})

        assert rt.list_products(NoTotal())["total"] is None
