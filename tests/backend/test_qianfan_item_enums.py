"""千帆商品枚举的取证值与不可混淆关系（守卫）。

这批值的每一次错都**不会报错**，只会让平台静默用错行为或直接拒绝：

  · `deliveryMode` 填 `1`（当成「发货模式=现货」）⇒ 平台判「无物流发货」⇒
    用户实测 **「发布未通过：deliveryMode — 商品不支持设置无物流发货」**；
  · `skuData[].deliveryTime` 不传 ⇒ 用户配的「全款预售 15 天」被覆盖成「现货 48 小时」。

⇒ 本文件钉住三件事：
  ① 每个枚举的**数字值**必须与真机取证一致（改了就必须先重新取证）；
  ② **物流类型 ≠ 发货模式**（两者曾被混成一个字段，是上面第一条的根因）；
  ③ 发布体必须真的用上这些枚举（不是定义完放那儿不用）。
"""

from __future__ import annotations

import inspect

import pytest

from backend.app.core import qianfan_item_enums as enums
from backend.app.services import shop_item_publish as pub


class TestLogisticsType:
    """`deliveryMode` = **物流类型**（CDP 直读 radio `value`，2026-10-21）。"""

    def test_values_match_real_device(self):
        assert int(enums.LogisticsType.LOGISTICS) == 0, "物流配送 = 0（真机 radio value=\"0\"）"
        assert int(enums.LogisticsType.NOT_LOGISTICS) == 1, "无物流发货 = 1"

    def test_platform_default_is_logistics_not_not_logistics(self):
        """骨架默认必须是**物流配送**，绝不能是 1（= 无物流发货，平台会拒绝）。"""
        value = pub._PLATFORM_DEFAULTS["deliveryMode"]
        assert value == int(enums.LogisticsType.LOGISTICS), (
            f"骨架 deliveryMode={value}，应为 0（物流配送）。"
            "填 1 会让平台报「商品不支持设置无物流发货」"
        )
        assert value != int(enums.LogisticsType.NOT_LOGISTICS)


class TestDeliveryModeIsNotLogisticsType:
    """**物流类型 ≠ 发货模式** —— 曾经混成一个字段，是线上事故根因。"""

    def test_delivery_mode_enum_is_ui_only_and_never_used_as_delivery_mode_field(self):
        """`DeliveryMode`（现货/预售）只描述界面，**不许**出现在提交体的
        `deliveryMode` 字段上 —— 那个字段是物流类型。"""
        source = inspect.getsource(pub)
        #: 提交体里对 deliveryMode 的赋值只允许来自 LogisticsType。
        assert '"deliveryMode": int(LogisticsType.LOGISTICS)' in source
        assert '"deliveryMode": 1' not in source, "不许把发货模式的 1 直接写进 deliveryMode"

    def test_delivery_time_types_match_real_device(self):
        """`skuData[].deliveryTime.type`：当日=3 / 按小时=4 / 按日期=5（真机四种模式各切一遍）。"""
        assert int(enums.DeliveryTimeType.SAME_DAY) == 3
        assert int(enums.DeliveryTimeType.HOURS) == 4
        assert int(enums.DeliveryTimeType.DATE) == 5

    def test_build_delivery_time_uses_enum_values(self):
        """`build_delivery_time` 的产物必须是 3/4/5，且当日发货**不带 time**。"""
        same_day = pub.build_delivery_time(delivery_mode=pub.MODE_SPOT, ship_time="当日发货")
        assert same_day == {"type": 3}, same_day
        assert "time" not in same_day, "当日发货不带 time（真机证据）"

        h24 = pub.build_delivery_time(delivery_mode=pub.MODE_SPOT, ship_time="24小时")
        assert h24 == {"type": 4, "time": "24"}
        assert h24["type"] == int(enums.DeliveryTimeType.HOURS)

        presale = pub.build_delivery_time(
            delivery_mode=pub.MODE_PRESALE, presale_kind="时段预售", presale_days=15
        )
        assert presale == {"type": 4, "time": "360"}, "15 天 = 15×24 = 360 小时"


class TestOtherPlatformEnums:
    def test_sale_time_mode(self):
        assert int(enums.SaleTimeMode.MANUAL) == 2
        assert int(enums.SaleTimeMode.SCHEDULED) == 3

    def test_playback_widget(self):
        assert int(enums.PlaybackWidget.HIDDEN) == 0
        assert int(enums.PlaybackWidget.SHOWN) == 1

    def test_spot_ship_time_values(self):
        assert enums.SPOT_SHIP_TIME_VALUES == ("-1", "24", "48")
        assert enums.SPOT_SHIP_TIME_H48 == "48"

    def test_stock_type_and_free_return(self):
        assert int(enums.StockType.SPOT) == 1
        assert int(enums.FreeReturn.SUPPORTED) == 1

    def test_item_status(self):
        assert int(enums.ItemStatus.ON_SHELF) == 1
        assert int(enums.ItemStatus.OFF_SHELF) == 2


class TestPublishBodyUsesEnums:
    """枚举不能只是「定义完放那儿」—— 发布体必须真的取到它们。"""

    def test_platform_defaults_reference_enums(self):
        defaults = pub._PLATFORM_DEFAULTS
        assert defaults["stockType"] == int(enums.StockType.SPOT)
        assert defaults["freeReturn"] == int(enums.FreeReturn.SUPPORTED)
        assert defaults["usePlayback"] == int(enums.PlaybackWidget.SHOWN)
        assert defaults["deliveryMode"] == int(enums.LogisticsType.LOGISTICS)

    def test_delivery_time_fallback_is_spot_48h(self):
        """SKU 未给 deliveryTime 时兜底 = 现货 48 小时（`type 4 / time "48"`）。"""
        sku = pub.build_publish_sku(
            row_number=1,
            price_yuan=10,
            stock=1,
            logistics_plan_id="lp-1",
            variants=[{"attributeId": "v-1", "attributeName": "颜色", "attributeType": 1, "attributeValue": "红"}],
        )
        assert sku["deliveryTime"] == {"type": int(enums.DeliveryTimeType.HOURS), "time": "48"}


class TestPreallocateUploadPermits:
    """**批量先拿凭证再上传**（用户口径 2026-10-22）。

    旧链路每张图各申请一次 `upload_web_permit(count=1)`：
    45 张 = 45 次凭证 + 45 次 PUT ⇒ 用户实测「点击发布耗时 40 秒」。
    本组钉住：① 一次拿多个；② 平台单次上限 14 时分批；
    ③ 每个 fileId 带**自己那批**的 addr/token；④ 拿不齐如实返回已拿到部分。
    """

    @staticmethod
    def _api(calls: list[int], per_batch_cap: int = 14):
        class _Api:
            @staticmethod
            def upload_web_permit(count, **kwargs):
                calls.append(int(count))
                n = min(int(count), per_batch_cap)
                return True, "ok", {
                    "data": {
                        "uploadTempPermits": [
                            {
                                "qos": 1,
                                "fileIds": [f"fid-{len(calls)}-{i}" for i in range(n)],
                                "uploadAddr": f"https://ros/{len(calls)}",
                                "token": f"tok-{len(calls)}",
                            }
                        ]
                    }
                }

        return _Api()

    def test_single_call_when_within_platform_cap(self):
        from backend.app.services import shop_draft_service as svc

        calls: list[int] = []
        permits, reason = svc.preallocate_upload_permits(self._api(calls), 10)
        assert reason == ""
        assert len(permits) == 10
        assert calls == [10], "10 张 ≤ 平台单次上限 14 ⇒ 只申请一次"

    def test_batches_beyond_platform_cap_of_14(self):
        from backend.app.services import shop_draft_service as svc

        calls: list[int] = []
        permits, reason = svc.preallocate_upload_permits(self._api(calls), 24)
        assert reason == ""
        assert len(permits) == 24
        #: 24 张 ⇒ 14 + 10 两批（平台单次最多 14 个 fileId）。
        assert calls == [14, 10], calls

    def test_each_file_id_carries_its_own_batch_credentials(self):
        """分批拿到的凭证**互不通用** —— 每个 fileId 必须带自己那批的 addr/token。"""
        from backend.app.services import shop_draft_service as svc

        calls: list[int] = []
        permits, _ = svc.preallocate_upload_permits(self._api(calls), 20)
        #: 前 14 个来自第 1 批，后 6 个来自第 2 批。
        assert permits[0]["upload_addr"] == "https://ros/1"
        assert permits[13]["upload_addr"] == "https://ros/1"
        assert permits[14]["upload_addr"] == "https://ros/2"
        assert permits[19]["token"] == "tok-2"

    def test_batches_until_quota_met_even_if_platform_gives_fewer_per_call(self):
        """平台单批给得比要求少 ⇒ **继续要，直到拿齐或平台返回空批**。

        ⚠️ 这条用例原本写的是「拿不齐就少给」——那是**错的期望**：
        分批循环的意义就是「这批没给够，下一批接着要」。
        真正的边界是**平台返回空批**（再要也是空）⇒ 此时停下并如实返回已有部分，
        不能死循环、也不能谎报拿齐。
        """
        from backend.app.services import shop_draft_service as svc

        calls: list[int] = []
        permits, reason = svc.preallocate_upload_permits(self._api(calls, per_batch_cap=5), 12)
        assert reason == ""
        assert len(permits) == 12, "应从 5/批 一直要到拿满 12"
        assert len(calls) >= 3, f"12 张、每批最多 5 ⇒ 至少 3 次申请，实际 {calls}"

    def test_stops_on_empty_batch_without_looping_forever(self):
        """平台返回**空 fileIds** ⇒ 立即停（再要也是空），如实返回已拿到的部分。"""
        from backend.app.services import shop_draft_service as svc

        calls: list[int] = []

        class _Empty:
            @staticmethod
            def upload_web_permit(count, **kwargs):
                calls.append(int(count))
                return True, "ok", {
                    "data": {"uploadTempPermits": [{"qos": 1, "fileIds": [], "uploadAddr": "u", "token": "t"}]}
                }

        permits, reason = svc.preallocate_upload_permits(_Empty(), 8)
        assert permits == [], "空批 ⇒ 一个 fileId 都拿不到"
        assert reason == ""
        assert len(calls) == 1, f"空批后必须停，实际申请了 {len(calls)} 次"

    def test_zero_needs_no_request(self):
        from backend.app.services import shop_draft_service as svc

        calls: list[int] = []
        permits, reason = svc.preallocate_upload_permits(self._api(calls), 0)
        assert permits == [] and reason == ""
        assert calls == [], "0 张图不该发任何凭证申请"
