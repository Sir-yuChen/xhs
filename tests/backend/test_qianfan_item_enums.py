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
