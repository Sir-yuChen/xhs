"""商品事实抽取层的守卫（设计 41 §11.1.3 第三层 / §13，M-B3，v2.25）。

## 缺口（真库实测，2026-10-16）

生成链路此前只喂 `PRODUCT_FIELDS` 白名单的**10 个平铺字段**，缺：

| # | 缺陷 | 后果 |
|---|---|---|
| 1 | `seller_point`（平台给的 20 个卖点词）**从未被使用** | 模型没有卖点依据 |
| 2 | `description` 是「键：值」拼串，**一整串**丢给模型 | 噪声大，输出空泛 |
| 3 | `attributes` 与 `description` **内容重复** | 同一键值出现两次 |
| 4 | SKU 名混规格（`… 藏青色 XL 【建议130-145斤】`） | 写进正文不像种草 |
| 5 | 没有「人群 / 场景」 | 模板第①段要求的输入不存在 |

## 三条不可让步的判据

1. **缺值留空不编造**（硬约束⑤）
2. **不做语义推断**（本层只「取出来 + 归位」，可单测、零成本）
3. **单一实现**（人群/场景词表只在本模块一份）
"""
from __future__ import annotations

import pytest

from backend.app.services.product_facts import (
    extract_product_facts,
    facts_to_prompt_lines,
)

#: 真库实测的商品原文（`shop_products.raw_json`，2026-10-16 取样）。
REAL_RAW = {
    "item_id": "6a581608ab42de00152e782c",
    "item_name": "中式椭圆形桌垫皮革桌布茶几实木圆桌垫防水防油防烫折叠圆形餐桌",
    "description": "是否礼盒：其它 适用场景：日常 风格：新中式穿搭 材质：PCTG+PP+硅胶 尺寸：中 流行元素：水墨风 只数：其他",
    "attributes": [
        {"attribute_name": "是否礼盒", "attribute_value": "其它"},
        {"attribute_name": "适用场景", "attribute_value": "日常"},
        {"attribute_name": "风格", "attribute_value": "新中式穿搭"},
        {"attribute_name": "材质", "attribute_value": "PCTG+PP+硅胶"},
        {"attribute_name": "尺寸", "attribute_value": "中"},
        {"attribute_name": "流行元素", "attribute_value": "水墨风"},
        {"attribute_name": "只数", "attribute_value": "其他"},
    ],
    "categories": [
        {"name": "居家布艺", "level": 1},
        {"name": "餐桌布艺", "level": 2},
        {"name": "桌布", "level": 3},
    ],
    "detail_json": [
        {
            "sku_name": "中式椭圆形桌垫… 金逸风华 【圆桌】直径100cm",
            "variant_name": "金逸风华 【圆桌】直径100cm",
        }
    ],
}


# --------------------------------------------------------------------------- #
# ① 取得到（正向）
# --------------------------------------------------------------------------- #


def test_extracts_category_material_size_from_real_payload():
    """真库原文 ⇒ 类目路径 / 材质 / 规格三件必须取到。"""
    facts = extract_product_facts(REAL_RAW)
    assert facts["category_path"] == "居家布艺 / 餐桌布艺 / 桌布", facts["category_path"]
    assert facts["material"] == "PCTG+PP+硅胶", facts["material"]
    assert facts["size"] == "中", facts["size"]


def test_extracts_selling_points_limit_three():
    """核心卖点**最多 3 条**（平台口径：卖点不要全，专攻 2–3 个）。

    ⚠️ 这是模板第③段的硬约束来源 —— 抽成 7 条就等于没约束。
    """
    facts = extract_product_facts(REAL_RAW)
    assert 0 < len(facts["selling_points"]) <= 3, facts["selling_points"]


def test_extracts_audiences_and_scenes():
    """人群 / 场景必须从属性里**识别出来**（平台有时直接给「适用场景」）。"""
    facts = extract_product_facts(REAL_RAW)
    assert "日常" in facts["scenes"], f"没识别出场景「日常」：{facts['scenes']}"
    # 「风格：新中式穿搭」里没有人群词 ⇒ audiences 允许为空（不编造）
    assert isinstance(facts["audiences"], list)


def test_prefers_structural_attributes_over_description_string():
    """`attributes[]` 与 `description` 内容重复时，**以结构化字段为准**。

    实测两者是同一批键值（一个拼串、一个数组）⇒ 必须去重；
    若实现只读拼串，键值冲突时就没有仲裁方。
    """
    raw = dict(REAL_RAW)
    # 故意让拼串与数组冲突：数组是结构化字段，应当赢
    raw["description"] = "材质：假的材质 尺寸：XL"
    facts = extract_product_facts(raw)
    assert facts["material"] == "PCTG+PP+硅胶", f"结构化字段没赢：{facts['material']}"
    assert facts["size"] == "中", f"结构化字段没赢：{facts['size']}"


def test_strips_sku_variant_noise():
    """SKU 规格名必须去掉 `【建议…】` 之类尾巴（否则会写进正文，不像种草）。"""
    facts = extract_product_facts(REAL_RAW)
    for name in facts["sku_variants"]:
        assert "【" not in name and "】" not in name, f"规格名没去噪：{name}"


# --------------------------------------------------------------------------- #
# ② 缺值不编造（反向 —— 最关键的一组）
# --------------------------------------------------------------------------- #


def test_empty_input_yields_empty_facts_not_fabricated():
    """空输入 ⇒ **全空**，不编造「通用」「百搭」之类填充。

    硬约束⑤：解析不到就留空。填充值会让模型以为「这里有依据」。
    """
    facts = extract_product_facts({})
    assert facts["material"] == ""
    assert facts["size"] == ""
    assert facts["audiences"] == []
    assert facts["scenes"] == []
    assert facts["selling_points"] == []
    assert facts["category_path"] == ""
    assert facts["attributes"] == {}


def test_none_and_non_dict_input_do_not_raise():
    """`None` / 非 dict ⇒ 同样给空事实（**不抛**，不阻断生成）。"""
    for bad in (None, [], "字符串", 123):
        facts = extract_product_facts(bad)  # type: ignore[arg-type]
        assert facts["material"] == "" and facts["selling_points"] == []


def test_no_audience_when_attributes_have_none():
    """属性里没有人群词 ⇒ `audiences` **必须是空**（不许硬塞）。

    ⚠️ 这条守的是「识别器 vs 生成器」的边界：本层只识别，不生成。
    """
    raw = {
        "description": "材质：不锈钢 尺寸：大",
        "attributes": [{"attribute_name": "材质", "attribute_value": "不锈钢"}],
        "categories": [{"name": "厨具", "level": 1}],
    }
    facts = extract_product_facts(raw)
    assert facts["audiences"] == [], f"编造了人群：{facts['audiences']}"


def test_prompt_lines_omit_missing_sections_entirely():
    """Prompt 文本行里，**缺值的段整段不出现**（不是打「材质：（未知）」空标签）。

    ⚠️ 空标签会给模型「这里本该有东西」的暗示 ⇒ 诱发编造。
    ⚠️ 用「只有类目」的输入：此时材质/规格/卖点/规格名**都取不到**，
    才能验「缺值整段消失」。（首版用例用「材质：不锈钢」，结果那个材质**本身就是
    有效卖点** ⇒ 卖点段非空，断言前提不成立 —— 已改成真正缺卖点的输入。）
    """
    text = facts_to_prompt_lines(
        extract_product_facts({"categories": [{"name": "厨具", "level": 1}]})
    )
    assert "类目：厨具" in text
    assert "材质：" not in text, f"缺材质时不该出现空标签：{text}"
    assert "规格：" not in text, f"缺规格时不该出现空标签：{text}"
    assert "核心卖点" not in text, f"缺卖点时段落该整段消失：{text}"
    assert "适用场景" not in text, f"缺场景时不该出现空标签：{text}"


# --------------------------------------------------------------------------- #
# ③ 单一实现 / 同口径（防两份漂移）
# --------------------------------------------------------------------------- #


def test_category_path_matches_shop_products_service():
    """类目路径与 `shop_products_service._category_path` **必须同结果**。

    ⚠️ 该函数在商品列表页是**展示契约**（前端在用）。事实层若算出不同的串，
    同一商品在两处显示不同类目 = 用户看到两个真相（DEF-044 同型）。
    """
    from backend.app.services.shop_products_service import _category_path

    # 乱序 level：两边都要按 level 升序拼接
    raw = {
        "categories": [
            {"name": "桌布", "level": 3},
            {"name": "居家布艺", "level": 1},
            {"name": "餐桌布艺", "level": 2},
        ]
    }
    assert extract_product_facts(raw)["category_path"] == _category_path(raw["categories"])


def test_audience_and_scene_terms_are_not_empty_tables():
    """词表非空自检（防「守卫恒真」：词表被清空 ⇒ 测试仍绿）。"""
    from backend.app.services import product_facts

    assert len(product_facts._AUDIENCE_TERMS) >= 10, "人群词表被清空或过窄"
    assert len(product_facts._SCENE_TERMS) >= 10, "场景词表被清空或过窄"
    assert len(product_facts._SELLING_POINT_BONUS) >= 5, "卖点加分词表被清空"


def test_extracts_platform_seller_points_when_provided():
    """调用方传入平台卖点词 ⇒ 原样带出（这是「平台给的 20 词」的入口）。"""
    raw = dict(REAL_RAW)
    raw["_platform_seller_points"] = ["防水", "防烫", "折叠"]
    facts = extract_product_facts(raw)
    assert facts["platform_seller_points"] == ["防水", "防烫", "折叠"]


# --------------------------------------------------------------------------- #
# ④ 接缝：`product_payload()` 必须真的把事实带进 Prompt
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["raw", "raw_json"])
def test_product_payload_carries_facts_text(key):
    """`product_payload()` 必须产出 `facts_text`（两种载体键都认）。

    ⚠️ 这是**接缝两侧**的判据：事实层抽得再好，只要 payload 不带出去，
    模型就看不到 —— 「两层各自自洽、接缝没人管」正是本项目最常见的返工型。
    """
    from backend.app.services.product_note_service import product_payload

    payload = product_payload({"item_id": "x", "title": "标题", key: REAL_RAW})
    assert "facts_text" in payload, f"payload 没带 facts_text（{key}）"
    assert "材质：PCTG+PP+硅胶" in payload["facts_text"]


def test_product_payload_without_raw_has_no_facts_text():
    """无 `raw` ⇒ **不出现** `facts_text` 键（不是空串）—— 缺值不出现。"""
    from backend.app.services.product_note_service import product_payload

    payload = product_payload({"item_id": "x", "title": "标题"})
    assert "facts_text" not in payload, "无 raw 时不该造一个空的 facts_text"


def test_build_product_note_prompt_includes_facts():
    """商品 Prompt 里必须出现事实段（端到端接缝）。"""
    from backend.app.services.product_note_service import build_product_note_prompt

    prompt = build_product_note_prompt({"item_id": "x", "title": "标题", "raw": REAL_RAW})
    assert "材质：PCTG+PP+硅胶" in prompt, "Prompt 没含结构化事实"
