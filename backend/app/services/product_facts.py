"""商品事实抽取层（设计 41 §11.1.3 第三层 / §13，M-B3）。

## 为什么需要它（**真库实测缺口**，2026-10-16）

生成链路喂给模型的商品信息来自 `product_note_service.product_payload()`，那是
`PRODUCT_FIELDS` 白名单的**平铺**（10 个字段：item_id / title / article_no /
price_min_fen / price_max_fen / stock / sku_count / on_sale_sku_count / category /
sales_total）。缺陷实测：

| # | 缺陷 | 实证 |
|---|---|---|
| 1 | **`seller_point` 从未被使用** | 平台在 `aigc-publish/attributes` 给**该商品** 20 个卖点词，`raw_json` 里也没有 ⇒ 目前**完全没用上** |
| 2 | **`description` 是「键：值」拼串** | 如 `是否礼盒：其它 适用场景：日常 风格：新中式穿搭 材质：PCTG+PP+硅胶 尺寸：中` —— 一整串丢给模型 |
| 3 | **`attributes` 与 `description` 内容重复** | 同一个键值对出现两次（一次在串里、一次在数组里）⇒ 模型看到重复噪声 |
| 4 | **SKU 名混着规格** | `… 藏青色 XL 【建议130-145斤】` ⇒ 直接当卖点会污染文案 |
| 5 | **没有「人群 / 场景」** | 模板第①段要求「从类目与属性推人群」，但输入里没有任何可直接用的依据 |

⇒ 本模块把 `raw_json` 里的**原始噪声**抽成**结构化事实**（核心卖点 top3 / 人群 / 场景 /
材质 / 尺寸 / 类目路径 / 价格区间），供 Prompt 注入。

## 判据（不可让步的三条）

1. **缺值留空，不编造**（硬约束⑤）：抽不到就 `""` / `[]`，**绝不**用「通用」「百搭」之类
   填充 —— 那会让模型以为有依据。
2. **不做语义推断**：本层只做「**取出平台已经给了的**」+「按关键词归位」，**不做** LLM 归纳
   （那是生成阶段的事）。这样本层**可单测、可复现、零成本**。
3. **单一实现**（DEF-044）：人群/场景词表只在本模块定义一份，调用方不得各自再抄。
"""
from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------------------- #
# ① 属性键位归类（**关键词 → 归类**；平台键名不固定，用包含匹配）
# --------------------------------------------------------------------------- #
#: 材质类属性键（用于抽「材质」事实）
_MATERIAL_KEYS = ("材质", "面料", "材料", "成分")
#: 尺寸类属性键
_SIZE_KEYS = ("尺寸", "规格", "大小", "容量", "重量", "直径", "长度", "宽度", "高度")
#: 人群/场景类属性键（平台有时直接在属性里给）
_AUDIENCE_KEYS = ("适用人群", "适用年龄", "目标人群", "人群")
_SCENE_KEYS = ("适用场景", "使用场景", "场景")

#: 人群词表（**从商品属性里命中才算数**，不是「猜」）。命中即作为事实输出。
#: ⚠️ 这张表是「识别器」，不是「生成器」—— 没命中就不输出（缺值留空，不编造）。
_AUDIENCE_TERMS = (
    "学生", "学生党", "租房", "宝妈", "孕", "婴", "儿童", "宝宝", "幼儿",
    "男士", "女士", "女性", "男性", "少女", "青年", "中年", "老年", "老人",
    "上班族", "打工人", "办公室", "户外", "旅行", "居家", "宿舍", "单身",
)
#: 场景词表（同上：只识别，不生成）
_SCENE_TERMS = (
    "日常", "通勤", "上班", "办公", "居家", "宿舍", "旅行", "出游", "露营", "户外",
    "宴会", "聚会", "约会", "送礼", "礼物", "节日", "生日", "婚庆", "开业",
    "厨房", "餐厅", "客厅", "卧室", "浴室", "卫生间", "阳台", "书房", "车载",
    "夏季", "夏季防晒", "冬季", "春秋", "开学", "毕业",
)

#: 卖点「有效性」加分词（出现在属性值里说明这条是可用卖点）。
#: ⚠️ 这是**排序依据**，不是过滤条件 —— 只影响前 3 条挑谁，不删任何事实。
_SELLING_POINT_BONUS = ("防", "防水", "防滑", "防烫", "透气", "加厚", "加固", "静音",
                        "轻", "便携", "折叠", "可拆", "易清洗", "耐用", "加固", "保温")

#: 拆分 `description` 的分隔符（实测是「空格分隔的『键：值』序列」）。
_DESC_SPLIT = re.compile(r"\s+")


def _clean(value: Any) -> str:
    """任意值 → 去空白字符串（`None` → `""`）。"""
    return str(value or "").strip()


def _split_description(description: Any) -> dict[str, str]:
    """`"键：值 键：值"` → `{键: 值}`。

    ⚠️ 实测形态（真库）：`是否礼盒：其它 适用场景：日常 风格：新中式穿搭 材质：PCTG+PP+硅胶 尺寸：中 流行元素：水墨风 只数：其他`
    ⇒ 按**空白**切，每段再按**第一个**「：」切键值。
    ⚠️ 值里可能自带「：」（如 `套餐：A：B`）⇒ 只切**第一个**冒号，后半整体作值。
    ⚠️ 切不出键值的段**丢弃**（不是塞进某个键 —— 那会造出假事实）。
    """
    text = _clean(description)
    if not text:
        return {}
    out: dict[str, str] = {}
    for part in _DESC_SPLIT.split(text):
        if "：" not in part and ":" not in part:
            continue
        sep = "：" if "：" in part else ":"
        key, _, value = part.partition(sep)
        key, value = key.strip(), value.strip()
        if key and value:
            out[key] = value
    return out


def _attributes_map(attributes: Any) -> dict[str, str]:
    """`attributes[]` → `{属性名: 属性值}`（**空值不代入**）。

    实测形态：`[{"attribute_name": "是否礼盒", "attribute_value": "其它"}, …]`
    """
    if not isinstance(attributes, list):
        return {}
    out: dict[str, str] = {}
    for row in attributes:
        if not isinstance(row, dict):
            continue
        name = _clean(row.get("attribute_name"))
        value = _clean(row.get("attribute_value"))
        if name and value:
            out[name] = value
    return out


def _pick_by_keys(facts: dict[str, str], keys: tuple[str, ...]) -> str:
    """按**键名包含**取第一个命中的值（平台键名不固定，故用包含而非相等）。"""
    for key, value in facts.items():
        if any(token in key for token in keys):
            return value
    return ""


def _match_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    """在文本里**逐词包含匹配**，保序去重。

    ⚠️ 匹配范围是**属性名 + 属性值拼起来的整串**（不是逐字段）——
    人群词可能出现在任何一项里（如 `适用人群：学生`、`风格：学生风`）。
    """
    out: list[str] = []
    for term in terms:
        if term in text and term not in out:
            out.append(term)
    return out


def _strip_sku_variant_noise(text: str) -> str:
    """SKU 名 → 去掉规格尾巴（`藏青色 XL 【建议130-145斤】` → 前半段）。

    ⚠️ 为什么要去：SKU 名是「商品名 + 规格 + 建议」，直接当卖点会污染文案
    （写进正文就是「建议130-145斤」这种不像种草的话）。
    ⚠️ 只做**保守切除**：`【…】` 整段去掉；末尾的「颜色 + 尺码」形态去掉。
    **不做**语义裁剪（切错了会丢真信息）。
    """
    value = _clean(text)
    if not value:
        return ""
    value = re.sub(r"【[^】]*】", "", value).strip()          # 去【建议…】
    value = re.sub(r"（[^）]*）$", "", value).strip()          # 去尾部括注
    return value.strip()


def _sku_variant_names(raw: dict[str, Any]) -> list[str]:
    """`detail_json`（SKU 明细）→ 去噪后的规格名列表（保序去重、最多 5 条）。

    实测形态：`[{"sku_name": "… 金逸风华 【圆桌】直径100cm", "variant_name": "金逸风华 【圆桌】直径100cm", …}]`
    """
    detail = raw.get("detail_json") or raw.get("sku_list") or []
    if isinstance(detail, str):
        # 台账列 `detail_json` 可能是 JSON 字符串（`load_product_dict_from_ledger`
        # 里未必已解析）⇒ 这里不猜，直接放弃（宁可少一个事实，不冒解析错的风险）。
        return []
    if not isinstance(detail, list):
        return []
    out: list[str] = []
    for row in detail:
        if not isinstance(row, dict):
            continue
        name = _strip_sku_variant_noise(row.get("variant_name") or row.get("sku_name"))
        if name and name not in out:
            out.append(name)
        if len(out) >= 5:
            break
    return out


# --------------------------------------------------------------------------- #
# ② 主入口
# --------------------------------------------------------------------------- #
def extract_product_facts(raw: dict[str, Any] | None) -> dict[str, Any]:
    """`raw_json`（千帆商品原文）→ **结构化事实**（供 Prompt 注入）。

    返回结构（**键名固定**，缺值为 `""` / `[]`，绝不编造）：

    ```
    {
      "category_path": "居家布艺 / 餐桌布艺 / 桌布",
      "material": "PCTG+PP+硅胶",
      "size": "中",
      "audiences": ["学生"],          # 命中的人群词（可能为空）
      "scenes": ["日常", "居家"],      # 命中的场景词（可能为空）
      "selling_points": ["防水", "防烫", "折叠"],   # 最多 3 条，按可信度排序
      "attributes": {"风格": "新中式穿搭", …},      # 去重后的属性全表（供模板引用）
      "sku_variants": ["金逸风华 直径100cm"],        # 去噪规格名，最多 5 条
      "platform_seller_points": ["…"],              # 平台给的卖点词（若调用方提供了）
    }
    ```

    ⚠️ **不做语义推断**：抽不到就空。本层的价值是「把已有的取出来 + 归位」，
    不是「猜出商品是什么」—— 后者交给生成阶段（模型）。
    """
    if not isinstance(raw, dict):
        return _empty_facts()

    # ---- 属性表：`description`（键值串）与 `attributes[]`（数组）**合并去重** ----
    # ⚠️ 两个来源实测**内容重复**（同一键值出现两次）⇒ 用 dict 天然去重；
    #    冲突时**以 `attributes[]` 为准**（它是结构化字段，比拼串可信）。
    facts: dict[str, str] = dict(_split_description(raw.get("description")))
    facts.update(_attributes_map(raw.get("attributes")))

    # ---- 卖点：从属性值里挑，按「有效性加分词」排序，取前 3 ----
    candidates: list[tuple[int, str]] = []
    for name, value in facts.items():
        text = f"{name}{value}"
        score = sum(1 for token in _SELLING_POINT_BONUS if token in text)
        #: 值太短（1 字）或太长（>12 字）都不像卖点词 ⇒ 降权（不删，只是排后）
        if len(value) < 2 or len(value) > 12:
            score -= 1
        candidates.append((score, value))
    candidates.sort(key=lambda pair: (-pair[0], pair[1]))
    selling_points: list[str] = []
    for _, value in candidates:
        if value not in selling_points:
            selling_points.append(value)
        if len(selling_points) >= 3:
            break

    # ---- 人群 / 场景：在「属性名+值」整串里做包含匹配 ----
    haystack = " ".join(f"{name}{value}" for name, value in facts.items())
    audiences = _match_terms(haystack, _AUDIENCE_TERMS)
    scenes = _match_terms(haystack, _SCENE_TERMS)

    # ---- 材质 / 尺寸：按键名归类 ----
    material = _pick_by_keys(facts, _MATERIAL_KEYS)
    size = _pick_by_keys(facts, _SIZE_KEYS)

    # ---- 类目路径 ----
    category_path = _category_from_raw(raw)

    return {
        "category_path": category_path,
        "material": material,
        "size": size,
        "audiences": audiences,
        #: ⚠️ `audiences` 与 `scenes` 可能**有交集**（如「学生」既像人群又像场景）——
        #: 刻意不去重跨表：两个列表回答两个问题，合并会丢语义。
        "scenes": scenes,
        "selling_points": selling_points,
        "attributes": facts,
        "sku_variants": _sku_variant_names(raw),
        "platform_seller_points": [
            _clean(x) for x in (raw.get("_platform_seller_points") or []) if _clean(x)
        ],
    }


def _category_from_raw(raw: dict[str, Any]) -> str:
    """`categories[]` → 「一级 / 二级 / 三级」。

    ⚠️ 与 `shop_products_service._category_path` **同口径**：这里**不 import 对方**
    （避免 services 内部横向依赖环），而是复用同一段纯逻辑 —— 两者都是「按 level
    升序拼接非空段」。判据由测试钉死：两条路径对同一输入必须给出**相同**结果。
    """
    categories = raw.get("categories")
    if not isinstance(categories, list):
        return ""
    rows = [row for row in categories if isinstance(row, dict)]
    rows.sort(key=lambda row: row.get("level") or 0)
    names = [_clean(row.get("name")) for row in rows]
    return " / ".join(name for name in names if name)


def _empty_facts() -> dict[str, Any]:
    return {
        "category_path": "",
        "material": "",
        "size": "",
        "audiences": [],
        "scenes": [],
        "selling_points": [],
        "attributes": {},
        "sku_variants": [],
        "platform_seller_points": [],
    }


def facts_to_prompt_lines(facts: dict[str, Any]) -> str:
    """结构化事实 → Prompt 文本行（**缺值整段不出现**，不是打空标签）。

    ⚠️ 为什么缺值要**整段消失**而不是写「材质：（未知）」：
    后者会给模型一个「这里本该有东西」的暗示，反而诱发编造。缺失即不提。
    """
    lines: list[str] = []
    if facts.get("category_path"):
        lines.append(f"类目：{facts['category_path']}")
    if facts.get("material"):
        lines.append(f"材质：{facts['material']}")
    if facts.get("size"):
        lines.append(f"规格：{facts['size']}")
    if facts.get("audiences"):
        lines.append("可能人群：" + "、".join(facts["audiences"]))
    if facts.get("scenes"):
        lines.append("适用场景：" + "、".join(facts["scenes"]))
    if facts.get("selling_points"):
        lines.append("核心卖点（只讲这几个，不要铺全）：" + "、".join(facts["selling_points"]))
    if facts.get("sku_variants"):
        lines.append("在售规格：" + "、".join(facts["sku_variants"]))
    if facts.get("platform_seller_points"):
        lines.append(
            "平台推荐卖点词：" + "、".join(facts["platform_seller_points"][:10])
        )
    return "\n".join(lines)
