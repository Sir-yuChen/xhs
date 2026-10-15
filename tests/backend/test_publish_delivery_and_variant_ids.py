"""`deliveryTime` / `variantIds` 修复的守卫（用户报障 2026-10-15，真机抓包实证）。

## 两个 bug

**① 发货模式**：用户配「全款预售模式15天内发货」，发布后变「现货模式48小时发货」。
   · 根因：`build_publish_sku` 里 `delivery_time or {"type": 4, "time": "48"}`
     —— 默认写死现货48小时，而 `delivery_time` **从未被调用方传过**。
   · 真机抓包（用户亲手把四种模式各切一遍，6 次 `save_product_drafts`）：
     **`deliveryMode` 6 次恒为 `1`** —— 它不是模式载体；真正的字段是
     `skuData[].deliveryTime`。

**② 规格图**：平台显示「添加规格图开关关闭 + 每个规格均无规格图」，但发布体里
   `skuData[].specImage` 明明有值。
   · 根因：API 层**写死** `variant_ids=[]` ⇒ `variantIds: []` + `mainSpecEnable: 0`
     ⇒ 平台认为「没有主规格」⇒ 规格图开关关闭。
   · 真机对照：平台自己的草稿体是 `variantIds: ["60404e0d…"]` + `mainSpecEnable: 1`。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.services import shop_item_publish as pub  # noqa: E402


class TestDeliveryTimeFromRealCapture:
    """⚠️ 每条用例的期望值都来自**真机抓包**（用户 2026-10-15 亲手切换四种模式）。"""

    def test_spot_same_day_is_type_3_without_time(self):
        #: 真机：现货 · 当日发货 ⇒ `{"type": 3}`（**不带 time**）。
        assert pub.build_delivery_time(
            delivery_mode=pub.MODE_SPOT, ship_time="当日发货") == {"type": 3}

    def test_spot_24_and_48_hours(self):
        #: 真机：现货 · 「24小时」⇒ 24；「48小时（推荐）」⇒ 48（**归一化掉「（推荐）」**）。
        assert pub.build_delivery_time(
            delivery_mode=pub.MODE_SPOT, ship_time="24小时") == {"type": 4, "time": "24"}
        assert pub.build_delivery_time(
            delivery_mode=pub.MODE_SPOT, ship_time="48小时（推荐）") == {"type": 4, "time": "48"}
        assert pub.build_delivery_time(
            delivery_mode=pub.MODE_SPOT, ship_time="48小时") == {"type": 4, "time": "48"}

    def test_full_presale_relative_converts_days_to_hours(self):
        #: 真机：**全款预售 · 时段预售 · 15天 ⇒ `{"type": 4, "time": "360"}`**（15×24）。
        #: ⚠️ 这条正是用户报障的场景（他配 15 天，结果发布成现货48小时）。
        assert pub.build_delivery_time(
            delivery_mode=pub.MODE_PRESALE, presale_kind="时段预售", presale_days=15,
        ) == {"type": 4, "time": "360"}
        assert pub.build_delivery_time(
            delivery_mode=pub.MODE_PRESALE, presale_kind="时段预售", presale_days=3,
        ) == {"type": 4, "time": "72"}

    def test_full_presale_absolute_uses_type_5_date(self):
        #: 真机：**全款预售 · 定时预售 · 2026/10/31 ⇒ `{"type": 5, "time": "2026/10/31"}`**。
        assert pub.build_delivery_time(
            delivery_mode=pub.MODE_PRESALE, presale_kind="定时预售", sale_time="2026/10/31",
        ) == {"type": 5, "time": "2026/10/31"}

    def test_by_spec_mode_uses_the_row_level_label(self):
        #: 「按规格设置发货时间」：**每行各自一条**（用户截图：价格库存表多一列「发货时间」）。
        assert pub.build_delivery_time(
            delivery_mode=pub.MODE_BY_SPEC, row_ship_aging="现货 / 48小时",
        ) == {"type": 4, "time": "48"}
        assert pub.build_delivery_time(
            delivery_mode=pub.MODE_BY_SPEC, row_ship_aging="预售 / 15天",
        ) == {"type": 4, "time": "360"}

    def test_unknown_returns_none_and_does_not_fabricate(self):
        #: ⚠️ **缺值不造假**：算不出来就返回 `None`（由调用方回落），
        #: 不许编一个看似合理的值蒙混过去。
        assert pub.build_delivery_time(delivery_mode="") is None
        assert pub.build_delivery_time(delivery_mode=pub.MODE_PRESALE) is None
        assert pub.build_delivery_time(
            delivery_mode=pub.MODE_SPOT, ship_time="无法识别的文案") is None

    def test_sku_carries_the_computed_delivery_time(self):
        #: 端到端：算出来的值真的落进 `skuData[].deliveryTime`。
        dt = pub.build_delivery_time(
            delivery_mode=pub.MODE_PRESALE, presale_kind="时段预售", presale_days=15)
        sku = pub.build_publish_sku(
            row_number=1, price_yuan=100, stock=1, logistics_plan_id="p",
            variants=[], delivery_time=dt)
        assert sku["deliveryTime"] == {"type": 4, "time": "360"}

    def test_sku_falls_back_to_spot_48_only_when_nothing_given(self):
        #: ⚠️ 默认值**只在完全没给时**生效（旧行为保留给老调用点），
        #: 但它**不再能覆盖**用户配的模式。
        sku = pub.build_publish_sku(
            row_number=1, price_yuan=100, stock=1, logistics_plan_id="p", variants=[])
        assert sku["deliveryTime"] == {"type": 4, "time": "48"}


class TestVariantIdsDerivedFromSequence:
    """⚠️ 规格图不生效的根因：`variantIds: []` + `mainSpecEnable: 0`。"""

    def test_variant_ids_derived_from_sequence_when_empty(self):
        #: API 层传 `variant_ids=[]`（写死）时，只要 `variantSequence` 解析出了维度，
        #: `variantIds` / `mainSpecEnable` / `mainSpecId` 就必须跟着有值 ——
        #: 否则平台认为「没有主规格」⇒ **规格图开关关闭**（用户报障）。
        body = pub.build_item_body(
            skeleton={}, name="x", category_id="c", attributes=None,
            main_images=[], detail_images=[], skus=[], variant_ids=[],
            variant_sequence=[{"variantId": "60404e0de85df80001991224",
                               "variantName": "颜色分类", "variantValues": ["斗柜"]}],
        )
        assert body["variantIds"] == ["60404e0de85df80001991224"]
        assert body["mainSpecEnable"] == 1
        assert body["mainSpecId"] == "60404e0de85df80001991224"

    def test_explicit_variant_ids_still_win(self):
        #: 调用方**显式传了**就以前者为准（派生只是兜底，不抢真相源）。
        body = pub.build_item_body(
            skeleton={}, name="x", category_id="c", attributes=None,
            main_images=[], detail_images=[], skus=[], variant_ids=["explicit"],
            variant_sequence=[{"variantId": "derived", "variantName": "n", "variantValues": []}],
        )
        assert body["variantIds"] == ["explicit"]

    def test_no_spec_no_main_spec(self):
        #: 真没有规格（`variantSequence` 也空）⇒ 保持 0，**不许编一个 id 出来**。
        body = pub.build_item_body(
            skeleton={}, name="x", category_id="c", attributes=None,
            main_images=[], detail_images=[], skus=[], variant_ids=[], variant_sequence=[],
        )
        assert body["variantIds"] == []
        assert body["mainSpecEnable"] == 0
        assert "mainSpecId" not in body


class TestServiceActuallyPassesItDown:
    """⚠️ **纯函数对 ≠ 接线对**（变异 M4 实测 ESCAPED ⇒ 这条是补的）。

    只测 `build_delivery_time()` 的返回值，证明不了 `shop_item_publish_service`
    真的把它**传给了** `build_publish_sku` —— 把接线删掉（`delivery_time=None`）
    纯函数用例照样全绿，而**用户的实际报障照旧**（配了预售还是发成现货48小时）。

    ⇒ 判据必须落在**调用点**上：service 里那次 `pub.build_publish_sku(` 必须带
    `delivery_time=pub.build_delivery_time(...)`，且**参数取自 `cfg`**。
    """

    def test_service_wires_delivery_time_into_every_sku(self):
        src = (ROOT / "backend/app/services/shop_item_publish_service.py").read_text(encoding="utf-8")
        i = src.find("pub.build_publish_sku(")
        assert i > 0, "找不到 build_publish_sku 调用点"
        call = src[i : i + 2200]
        assert "delivery_time=pub.build_delivery_time(" in call, (
            "service 没把算好的 delivery_time 传给 build_publish_sku ⇒ "
            "skuData[].deliveryTime 恒为默认的「现货48小时」（用户报障的原样）"
        )
        #: ⚠️ 参数必须**真的取自 `cfg`**（不是写死），否则用户配什么都会被忽略。
        for key in ("deliveryMode", "shipTime", "presaleKind", "presaleDays", "shipAging"):
            assert f'cfg.get("{key}")' in call, f"delivery_time 没读 cfg 的 {key}"
        #: 「按规格」模式每行各自有文案 ⇒ 必须读行级 `delivery_label`。
        assert 'row.get("delivery_label")' in call, (
            "没读行级 delivery_label ⇒ 「按规格设置发货时间」模式下每行算不出自己的时效"
        )

    def test_api_layer_passes_the_row_level_label_through(self):
        #: 端到端第二跳：API 模型必须收 `delivery_label`（`extra="forbid"` ⇒
        #: 不声明就 422，前端传了也白传）。
        api = (ROOT / "backend/app/api/platforms/xhs/qianfan/publish.py").read_text(encoding="utf-8")
        assert "delivery_label:" in api, "ItemDraftSku 没声明 delivery_label（前端传了会 422）"

    def test_frontend_sends_delivery_label(self):
        #: 端到端第三跳：前端提交白名单里必须带它（白名单会剥字段）。
        wiz = (ROOT / "frontend/src/pages/platforms/xhs/product-publish-wizard.tsx").read_text(
            encoding="utf-8")
        assert "delivery_label: s.delivery_label" in wiz, (
            "前端提交白名单没带 delivery_label ⇒ 后端收不到该行时效"
        )
