"""商品详情图的**提示词整理链路**（设计 17 §13.3.7，v2.19）。

用户口径（2026-10-03）：「调用大模型时，将商品名称、商品参数信息和选择的参考图
以及提示词和生成的图片要求**全部整理扔给大模型**，最后返回大模型生成的结果
展示在页面」。

## 为什么需要这一层

v2.18 及以前：端点把「商品标题 + 规格名 + 价格」拼成一句提示词，**直接丢给出图模型**。
问题是出图模型（`/images/generations`）只吃一句 prompt，**看不了参考图之外的上下文**，
也理解不了「这个商品适合什么样的详情图」—— 商品参数（库存/销量/类目/尺码）全被丢掉。

本模块改成**两步**：

1. **整理**（本模块）：把商品名 / 参数 / 用户补充要求 / 出图要求 / 参考图一起交给
   **文本（多模态）模型**，让它产出一段**结构化的详情图提示词**；
2. **出图**（`qianfan.py` 端点）：用整理好的提示词 + 参考图去叫图像模型。

⚠️ **整理失败不阻断出图**：多模态模型没配 / 调用失败 ⇒ **回落**到旧的本地拼接提示词
（`_default_detail_image_prompt`），并如实把原因带回前端。理由：整理是**增强**，
不是必要条件；因为「润色提示词的模型没配」而让整个功能不可用，是把可选层做成了硬门禁。

⚠️ **参考图怎么给文本模型**：给的是**图片 URL**（千帆 CDN 直链 / 本地 media URL）。
部分服务商抓不到外链 ⇒ 整理失败时同样回落，**不因此把功能判死**。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.app.services import image_prompt

logger = logging.getLogger(__name__)

#: 交给文本模型时，参考图最多附几张（再多提示词会失控，且部分服务商有上限）。
MAX_PROMPT_IMAGES: int = 3

#: 整理后的提示词长度上限（出图模型普遍有 prompt 长度限制；超了截断而不是报错）。
MAX_PROMPT_CHARS: int = 1200


def _sku_lines(skus: list[dict[str, Any]], limit: int = 12) -> list[str]:
    """SKU 明细 → 可读的参数行（**只用台账里真实存在的字段**，缺值不编）。

    带上价格与库存（用户最关心的两个参数），规格名与编码能帮助模型理解品类。
    """
    lines: list[str] = []
    for sku in skus[:limit]:
        if not isinstance(sku, dict):
            continue
        parts: list[str] = []
        for key in ("variant_name", "sku_name", "spec", "name"):
            value = str(sku.get(key) or "").strip()
            if value:
                parts.append(value)
                break
        price = sku.get("price")
        if isinstance(price, (int, float)) and price:
            # 台账价格单位是**分**（与 `normalize_item_from_raw` 同口径）。
            parts.append(f"¥{price / 100:.2f}")
        stock = sku.get("stock")
        if isinstance(stock, (int, float)):
            parts.append(f"库存{int(stock)}")
        if parts:
            lines.append(" / ".join(parts))
    return lines


#: 出图要求（**一致性锁**，设计 17 §13.3.7.7，v2.21）。
#:
#: ⚠️ **v2.21 起改为复用公共约束库** `services/image_prompt.py` —— 原先这里自己写了一
#: 份，导致「详情图有防漂移约束、笔记配图/视频没有」，且「竖版构图」这类句子在 3 处
#: 各写一遍。公共约束库按**用途**分档（`USE_DETAIL` / `USE_COVER` / `USE_VIDEO`），
#: 防漂移三条全用途共享。
IMAGE_REQUIREMENT_LINES: tuple[str, ...] = image_prompt.requirement_lines(image_prompt.USE_DETAIL)


def requirement_block() -> str:
    """出图要求段（**唯一实现**）—— 供文本模型整理与本地拼接共用。

    ⚠️ 两处必须同源：`build_item_brief`（喂文本模型）与 `_default_detail_image_prompt`
    （回落本地拼接）若各写一份，「整理成功」与「整理失败」两条路的出图约束会不一致 ——
    用户会看到「有时风格统一、有时不统一」，且无法归因。
    """
    return "\n".join(("出图要求：", *IMAGE_REQUIREMENT_LINES))


def build_item_brief(item: dict[str, Any], skus: list[dict[str, Any]], prompt: str) -> str:
    """把商品名 / 参数 / 用户要求 / 出图要求整理成**给文本模型的一段中文说明**。

    ⚠️ 只用真实字段：解析不到的（价格缺失、无 SKU）就不写进这段 ——
    编造卖点等于替用户宣称商品没有的属性（硬约束 ⑤ 缺值口径）。
    """
    lines: list[str] = ["请为下面这个电商商品设计一张**商品详情图**的画面提示词。", ""]

    title = str(item.get("title") or "").strip()
    if title:
        lines.append(f"商品名称：{title}")

    brand = str(item.get("brand_name") or item.get("brand") or "").strip()
    if brand:
        lines.append(f"品牌：{brand}")

    category = str(item.get("category") or "").strip()
    if category:
        lines.append(f"商品类目：{category}")

    price_min = item.get("price_min_fen")
    price_max = item.get("price_max_fen")
    if isinstance(price_min, (int, float)) and price_min:
        if isinstance(price_max, (int, float)) and price_max and price_max != price_min:
            lines.append(f"价格区间：¥{price_min / 100:.2f} ~ ¥{price_max / 100:.2f}")
        else:
            lines.append(f"价格：¥{price_min / 100:.2f}")

    sales = item.get("sales_total")
    if isinstance(sales, (int, float)) and sales:
        lines.append(f"累计销量：{int(sales)}")

    sku_lines = _sku_lines(skus or [])
    if sku_lines:
        lines.append("")
        lines.append("商品规格参数：")
        lines.extend(f"- {line}" for line in sku_lines)

    user_prompt = str(prompt or "").strip()
    if user_prompt:
        lines.append("")
        lines.append(f"用户补充要求（优先满足）：{user_prompt}")

    lines.append(requirement_block())
    lines.extend(
        [
            "",
            "请只输出这段画面提示词本身（中文，200 字以内），不要解释、不要分点。",
        ]
    )
    return "\n".join(lines)


def render_template_prompt(
    *,
    template: Any,
    modules: list[Any],
    item: dict[str, Any],
    skus: list[dict[str, Any]],
    extra_prompt: str = "",
) -> str:
    """把**公共提示词模板**渲染成详情图提示词（v2.24，设计 17 §13.5.4）。

    用户口径「生成AI商品图 没有公共提示模板相关逻辑」—— 此前弹窗的提示词是
    **用户手输的自由文本**，与项目公共模板系统（`prompt_templates` /
    `prompt_modules` / `prompt_renderer`）没有任何关系；于是「在模板页精心调好的
    风格约束」对商品图出图**完全不起作用**，用户得在两个页面各维护一份。

    拼接顺序**完全交给** `prompt_renderer.render_prompt`（§5.1 唯一实现）：
    基础指令 + Σ(引用模块) + 骨架（商品字段经插槽注入）。本函数**只做两件事**：
    ① 把商品 / 规格整理成 `product` 映射喂给渲染器；② 拼上出图约束与用户补充要求。

    渲染失败（骨架里有未知插槽）**原样抛 `PromptRenderError`** —— 交给端点映射
    成 400。⚠️ **不回落**：选了模板却静默用默认提示词，等于让用户以为模板生效了。
    """
    from backend.app.services import prompt_renderer

    # 商品映射：键名必须与 `prompt_renderer.PRODUCT_FIELD_SLOTS` **严格一致**
    # （即 `product_note_service.PRODUCT_FIELDS` + `price_text`）——
    # ⚠️ 自造键（如 `item_title`）会被渲染器当**未知插槽**直接 400，
    # 而合法槽位清单由渲染器给出，这里不许另立一套名字（DEF-044）。
    product: dict[str, Any] = {}
    for field in prompt_renderer.PRODUCT_FIELD_SLOTS:
        raw = item.get(field)
        if raw not in (None, ""):
            product[field] = raw
    # 商品名在台账里叫 `title`（真实字段名），模板用 `{title}` 引用
    if not product.get("title"):
        product["title"] = str(item.get("title") or "").strip()

    skeleton = str(getattr(template, "prompt_skeleton", "") or "")
    body = prompt_renderer.render_prompt(skeleton=skeleton, modules=modules, product=product)

    parts = [body.strip()] if body.strip() else []
    # 出图约束（公共库，v2.21）：**只补模板没覆盖到的那部分**。
    #
    # ⚠️ **为什么要逐条判重**：种子模板本身已引用「一致性锁 / 平台合规」两个模块，
    # 若这里再无脑追加 `requirement_block()`，同一组约束会在 prompt 里出现**两遍**
    # （真机实测：一致性三条与合规一条各重复一次）。重复不是「更保险」——
    # 它让 prompt 变长，且同一约束出现两种措辞时模型会各取一半。
    #
    # ⚠️ 比对必须**归一化**，三层差异都要抹掉（真机逐层实测出来的）：
    # ① 行首 `- ` 前缀（库常量有、模块正文没有）；
    # ② 行尾标点（库 `；`、模块 `。`）；
    # ③ **指代词差异**（库「**图中**不要出现文字水印」、种子里写「**画面中**不要出现…」）
    #    —— 措辞几乎一样但逐字不等 ⇒ 仍会重复。
    # 所以判据取「去掉装饰 + 去掉常见指代词」后的核心串比对。
    _DECOR = ("- ", "；", "。", ";", ".", " ", "图中", "画面中", "画面上")

    def _bare(text: str) -> str:
        out = text
        for token in _DECOR:
            out = out.replace(token, "")
        return out

    body_bare = _bare(body)
    missing = [line for line in image_prompt.requirement_lines(image_prompt.USE_DETAIL) if _bare(line) not in body_bare]
    if missing:
        parts.append("\n".join(("出图要求：", *missing)))
    if extra_prompt.strip():
        parts.append(f"【补充要求】{extra_prompt.strip()}")
    return "\n\n".join(part for part in parts if part.strip())


def refine_prompt(
    *,
    item: dict[str, Any],
    skus: list[dict[str, Any]],
    prompt: str,
    reference_images: list[str],
    text_client: Any | None,
    model_config: Any | None,
    api_key: str,
    platform: str = "xhs",
) -> tuple[str, str]:
    """调文本模型把商品信息整理成详情图提示词。

    返回 `(提示词, 整理状态说明)`：
    - 成功 ⇒ `("<整理后的提示词>", "")`
    - 未配 / 失败 ⇒ `("", "<原因>")`，**由调用方回落**到本地拼接提示词。

    ⚠️ 本函数**不抛异常**（除了编程错误）：整理是可选的增强层，它的失败不该让
    出图整体失败 —— 但**也不静默**，原因如实回传给上层记进任务 payload 与响应。
    """
    if text_client is None or model_config is None or not api_key:
        return "", "未配置文本类服务商，已回落本地拼接提示词"

    brief = build_item_brief(item, skus, prompt)
    images = [str(u).strip() for u in (reference_images or []) if str(u).strip()]
    images = images[:MAX_PROMPT_IMAGES]

    try:
        content = text_client.chat(
            model_config=model_config,
            api_key=api_key,
            instruction=brief,
            image_urls=images,
            platform=platform,
            function="product_detail_image_prompt",
        )
    except Exception as exc:  # noqa: BLE001 - 整理失败一律回落，不阻断出图
        logger.warning("详情图提示词整理失败，回落本地拼接：%s", exc)
        return "", f"提示词整理失败（{exc}），已回落本地拼接提示词"

    text = str(content or "").strip()
    if not text:
        return "", "提示词整理返回空，已回落本地拼接提示词"
    if len(text) > MAX_PROMPT_CHARS:
        text = text[:MAX_PROMPT_CHARS]
    return text, ""


def brief_preview(item: dict[str, Any], skus: list[dict[str, Any]], prompt: str) -> dict[str, Any]:
    """把「整理给模型的内容」以结构化形式回给前端（供弹窗展示）。

    ⚠️ 前端展示的必须是**真实送出的那份内容**（同一实现 `build_item_brief`），
    不能前端自己再拼一遍 —— 那是同一语义两份实现，必然漂移（DEF-044）。
    """
    sku_lines = _sku_lines(skus or [])
    return {
        "brief": build_item_brief(item, skus, prompt),
        "sku_lines": sku_lines,
        "sku_total": len(skus or []),
        "truncated": len(skus or []) > len(sku_lines),
    }


__all__ = [
    "IMAGE_REQUIREMENT_LINES",
    "MAX_PROMPT_CHARS",
    "MAX_PROMPT_IMAGES",
    "brief_preview",
    "build_item_brief",
    "refine_prompt",
    "requirement_block",
]
