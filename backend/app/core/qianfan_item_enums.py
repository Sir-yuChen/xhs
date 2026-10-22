"""千帆商品发布相关枚举 —— **平台真机口径的唯一真相源**。

## 为什么要有这个模块

这些值以前散落在前后端各处，且**多处是猜的**（注释写「未真机确认」）。
最贵的两次事故都是「枚举值错了但代码不报错」：

1. **`deliveryMode` 被当成「发货模式」**，填了 `1` ⇒ 平台判成「无物流发货」⇒
   用户发布时报 **「deliveryMode — 商品不支持设置无物流发货」**。
   真相：它是**物流类型**，物流配送 = `0`；发货模式的载体是 SKU 级 `deliveryTime`。
2. **`skuData[].deliveryTime.type`** 没传 ⇒ 用户配的「全款预售 15 天」被覆盖成
   「现货 48 小时」（用户实测）。

⇒ 本模块**只放已坐实的值**，每个值都带取证来源。未坐实的一律不写（缺值不造假）。

## 取证方式

CDP 直读千帆商品编辑页（`ark.xiaohongshu.com/app-item/good/edit/<id>`）
的 DOM：`input[type=radio/checkbox]` 的 `value` + 文案。
取证时间：2026-10-21。脚本：`.hermes/tmp/probe_enums.py`。

⚠️ **这些值属于平台契约，改之前必须先重新取证**（页面结构可能变，
但 `value` 是提交体字段，不能靠界面文案反推）。
"""

from __future__ import annotations

from enum import IntEnum

# ──────────────────────────────────────────────────────────────────────
# ① 物流类型（提交体 `itemData[].deliveryMode`）
# ──────────────────────────────────────────────────────────────────────


class LogisticsType(IntEnum):
    """**物流类型** —— 提交体字段 `itemData[].deliveryMode`。

    ⚠️ 这**不是**「发货模式」（现货/预售）：
    发货模式的载体是 **SKU 级 `skuData[].deliveryTime`**，两者别混。

    真机取证（2026-10-21，CDP 直读 radio `value`）：
      · 「物流配送」radio `value="0"`，**默认且通常唯一**（类目不支持无物流时不出现第二项）
      · 「无物流发货或物流配送」为 `1`（仅当类目支持时才出现在候选里）
    """

    #: 物流配送（需上传快递单号）。**默认值**，绝大多数类目只有这一档。
    LOGISTICS = 0
    #: 无物流发货或物流配送（无需快递单号）。仅类目支持时可选。
    NOT_LOGISTICS = 1


#: 兼容旧引用（`shop_item_publish.py` 里已有别名常量，保留避免大范围改名）。
LOGISTICS_TYPE_LOGISTICS: int = LogisticsType.LOGISTICS
LOGISTICS_TYPE_NOT_LOGISTICS_VALUE: int = LogisticsType.NOT_LOGISTICS


# ──────────────────────────────────────────────────────────────────────
# ② 发货模式（**界面**选项；提交体不用它，用 SKU 级 deliveryTime）
# ──────────────────────────────────────────────────────────────────────


class DeliveryMode(IntEnum):
    """**发货模式**（界面上的四档）。

    ⚠️ 真机抓包（2026-10-15，用户亲手切四种模式、6 次草稿）证明：
    **`deliveryMode` 6 次恒为 1** —— 提交体里**没有**这个语义的字段。
    真正的载体是 `skuData[].deliveryTime`（见 `DeliveryTimeType`）。

    ⇒ 本枚举只描述**界面选项**，供前端/后端文案与分支使用，
      **不要**拿去填提交体的任何顶层字段。
    """

    #: 现货模式
    SPOT = 1
    #: 全款预售模式
    PRESALE = 2
    #: 现货+预售模式
    SPOT_PLUS_PRESALE = 3
    #: 按规格设置发货时间
    BY_VARIANT = 4


#: 界面中文 → 枚举（前端/配置快照里传的是中文）。
DELIVERY_MODE_LABELS: dict[str, str] = {
    "现货模式": "spot",
    "全款预售模式": "presale",
    "现货+预售模式": "spot_plus",
    "按规格设置发货时间": "by_variant",
}


# ──────────────────────────────────────────────────────────────────────
# ③ SKU 级发货时间（提交体 `skuData[].deliveryTime`）
# ──────────────────────────────────────────────────────────────────────


class DeliveryTimeType(IntEnum):
    """`skuData[].deliveryTime.type` —— **发货模式的真身**。

    真机抓包（2026-10-15，四种模式各切一遍）：

    | 界面选择 | 提交体 |
    |---|---|
    | 现货 · 当日发货 | `{"type": 3}` |
    | 现货 · 24小时 | `{"type": 4, "time": "24"}` |
    | 现货 · 48小时 | `{"type": 4, "time": "48"}` |
    | 全款预售 · 时段预售 15天 | `{"type": 4, "time": "360"}`（15×24） |
    | 全款预售 · 定时预售 某日 | `{"type": 5, "time": "2026/10/31"}` |
    """

    #: 当日发货（**不带 `time`**）
    SAME_DAY = 3
    #: 按小时数发货（`time` = 小时；预售按 天×24 换算）
    HOURS = 4
    #: 按固定日期发货（`time` = `YYYY/MM/DD`）
    DATE = 5


# ──────────────────────────────────────────────────────────────────────
# ④ 现货发货时效（界面 radio；提交体走 `DeliveryTimeType.HOURS` 的 time）
# ──────────────────────────────────────────────────────────────────────


#: 现货发货时效的**界面取值**（真机 radio `value`，2026-10-21 CDP 直读）：
#: 当日发货 `-1` / 24 小时 `24` / 48 小时（推荐，默认选中）`48`。
SPOT_SHIP_TIME_SAME_DAY = "-1"
SPOT_SHIP_TIME_H24 = "24"
SPOT_SHIP_TIME_H48 = "48"

#: 现货发货时效候选（真机文案顺序：当日发货 / 24小时 / 推荐48小时）
SPOT_SHIP_TIME_VALUES: tuple[str, ...] = (
    SPOT_SHIP_TIME_SAME_DAY,
    SPOT_SHIP_TIME_H24,
    SPOT_SHIP_TIME_H48,
)


# ──────────────────────────────────────────────────────────────────────
# ⑤ 开售时间（提交体 `expectedPurchaseTime` / 界面 radio）
# ──────────────────────────────────────────────────────────────────────


class SaleTimeMode(IntEnum):
    """**开售时间**（界面三档，真机 radio `value`）。

    真机取证（2026-10-21）：
      · 「不设置，手工上架开售」radio `value="2"`（默认）
      · 「定时开售」radio `value="3"`
      · 「审核通过后立即上架开售」—— 该档在页面存在但本次商品未渲染为 radio，
        取值**未坐实** ⇒ 不在此处写死（缺值不造假），由前端按界面文案传，
        后端按文案透传。
    """

    #: 不设置，手工上架开售（默认）
    MANUAL = 2
    #: 定时开售
    SCHEDULED = 3


# ──────────────────────────────────────────────────────────────────────
# ⑥ 回看讲解挂件（提交体 `usePlayback`）
# ──────────────────────────────────────────────────────────────────────


class PlaybackWidget(IntEnum):
    """**回看讲解挂件**（真机 radio `value`）。

    真机取证（2026-10-21）：「不展示回看讲解挂件」`value="0"`、
    「展示回看讲解挂件」`value="1"`。
    """

    #: 不展示
    HIDDEN = 0
    #: 展示
    SHOWN = 1


# ──────────────────────────────────────────────────────────────────────
# ⑦ 售后 · 七天无理由（提交体 `freeReturn`）
# ──────────────────────────────────────────────────────────────────────


class FreeReturn(IntEnum):
    """`freeReturn` —— 真机骨架值 `1` = 支持七天无理由（页面默认勾选）。"""

    #: 支持七天无理由退货
    SUPPORTED = 1


# ──────────────────────────────────────────────────────────────────────
# ⑧ 商品状态 / 库存类型（提交体顶层）
# ──────────────────────────────────────────────────────────────────────


class StockType(IntEnum):
    """`stockType` —— 真机骨架值 `1`（现货，按 SKU 库存售卖）。"""

    #: 现货
    SPOT = 1


class ItemStatus(IntEnum):
    """`itemStatus` —— 上架状态。

    真机抓包（`batch_create_item_body.json`）：
    `1` = 上架可售；`2` = 下架（草稿箱里保存的初始态）。
    """

    #: 上架（可售）
    ON_SHELF = 1
    #: 下架
    OFF_SHELF = 2


__all__ = [
    "DeliveryMode",
    "DeliveryTimeType",
    "DELIVERY_MODE_LABELS",
    "FreeReturn",
    "ItemStatus",
    "LOGISTICS_TYPE_LOGISTICS",
    "LOGISTICS_TYPE_NOT_LOGISTICS_VALUE",
    "LogisticsType",
    "PlaybackWidget",
    "SaleTimeMode",
    "SPOT_SHIP_TIME_H24",
    "SPOT_SHIP_TIME_H48",
    "SPOT_SHIP_TIME_SAME_DAY",
    "SPOT_SHIP_TIME_VALUES",
    "StockType",
]
