"""Prompt 渲染器（设计 17 §5.1，唯一实现）。

```
final_prompt =
    基础指令（内置常量）
  + Σ(引用的 prompt_modules 按 sort_order 拼接)
  + prompt_templates.prompt_skeleton（含 {商品字段} / {素材引用} 插槽的模板骨架）
  + 商品白名单字段注入（复用 product_note_service.product_payload）
  + 素材/参考图引用
```

## 插槽白名单（§5.1 硬纪律）

只认一期定义的槽位；**未知插槽渲染时原样报错**（`PromptRenderError`）——
防模板注入静默吞内容，DEF-048「安全网不能条件性」的同源纪律。

## 为什么不复用 `str.format`

`str.format` 对未知 `{xxx}` 抛 `KeyError`（能报错）但也会**吃掉** `{{}}` 转义、
且对中文商品正文里的裸 `{` 敏感。这里用显式正则扫描 + 逐个替换：命中白名单才替换，
未知槽位收集起来一次性报错（**不静默、不部分替换**）。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from backend.app.services.product_note_service import PRODUCT_FIELDS

#: 基础指令（内置常量，§5.1 第一项）。
BASE_INSTRUCTION = "你是小红书种草内容专家，擅长写出真实、有画面感、符合平台调性的笔记文案。"

#: 商品白名单字段槽位 —— **直接复用** `product_note_service.PRODUCT_FIELDS`
#: （禁抄第二份字段清单，DEF-044「同一语义不留两份实现」），外加该服务派生的
#: `price_text`（`_price_text` 的展示口径，`product_payload` 会补上）。
PRODUCT_FIELD_SLOTS = tuple(PRODUCT_FIELDS) + ("price_text",)

#: 全部合法槽位（§5.1 白名单）。
TEXT_SLOTS = ("title", "body", "topics")
MEDIA_SLOTS = ("images", "video")

#: **文案约束槽位**（2026-10-17 用户口径：
#: *「字数、表情以及话题设置均有默认值，公共模板中应无需固定添加固定提示词，
#:   调用大模型时需最终拼接」*）。
#:
#: 模板里**只引用槽位名**，具体数值由 `copy_requirements` 在**调用时**填进来 ——
#: 于是模板不写死「30–300 字」，改一次前端设置就全局生效，不会两处打架。
#: 槽位解析不到时渲染成空串（**不报错**：老模板不需要它们）。
CONSTRAINT_SLOTS = (
    "title_length",       # 「8–20 字」
    "body_length",        # 「100–1000 字」
    "topic_count",        # 「3–6 个」
    "emoji_rule",         # 「带适量表情」/「不要表情」/「按调性自行决定」
    "copy_supplement",    # 用户填的三块提示词（空 ⇒ 空串）
)
ALLOWED_SLOTS = frozenset(
    PRODUCT_FIELD_SLOTS + TEXT_SLOTS + MEDIA_SLOTS + CONSTRAINT_SLOTS
)

#: 匹配 `{slot}`；只认 ASCII 标识符，避免误吞正文里的中文花括号内容。
_SLOT_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class PromptRenderError(ValueError):
    """渲染失败（未知插槽 / 引用模块缺失）。

    `message` 面向用户，`code` 供 API 层映射错误码。
    """

    def __init__(self, message: str, code: str = "PROMPT_RENDER_FAILED") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def find_unknown_slots(text: str, extra_slots: Iterable[str] = ()) -> list[str]:
    """返回骨架里**不在白名单**的槽位名（去重、保序）。

    `extra_slots` 供调用方按上下文追加合法槽位（如生成阶段的 `title/body/topics`
    可出现在模板骨架里作为**输出示例**，属于合法引用）。
    """
    allowed = ALLOWED_SLOTS | set(extra_slots)
    seen: list[str] = []
    for match in _SLOT_PATTERN.finditer(text or ""):
        name = match.group(1)
        if name not in allowed and name not in seen:
            seen.append(name)
    return seen


def render_modules(modules: Iterable[Any], *, sort_by_order: bool = True) -> str:
    """把引用的公共模块拼成一段（按 `sort_order`，同序按 id）。

    `modules` 元素须有 `.content` / `.sort_order` / `.id`（`PromptModule` 或等价对象）；
    停用的模块由调用方过滤（渲染器不做业务过滤，只做拼接）。
    """
    items = list(modules)
    if sort_by_order:
        items.sort(key=lambda m: (getattr(m, "sort_order", 0) or 0, getattr(m, "id", 0) or 0))
    parts = [str(getattr(m, "content", "") or "").strip() for m in items]
    return "\n\n".join(part for part in parts if part)


def render_skeleton(
    skeleton: str,
    *,
    product: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """替换骨架里的 `{slot}`；未知槽位 ⇒ `PromptRenderError`（不部分替换）。

    `product` 里缺失的字段渲染为空串（商品字段可能为空 —— 缺值口径：留空不造假）；
    `extra` 覆盖 `title/body/topics/images/video` 等输出/素材槽位。
    """
    unknown = find_unknown_slots(skeleton)
    if unknown:
        raise PromptRenderError(
            f"模板含未知插槽：{'、'.join('{' + name + '}' for name in unknown)}；"
            f"合法插槽：{'、'.join(sorted(ALLOWED_SLOTS))}",
            code="PROMPT_SLOT_UNKNOWN",
        )

    values: dict[str, Any] = {}
    for field in PRODUCT_FIELD_SLOTS:
        raw = (product or {}).get(field)
        values[field] = "" if raw is None else str(raw)
    for slot in TEXT_SLOTS + MEDIA_SLOTS + CONSTRAINT_SLOTS:
        raw = (extra or {}).get(slot)
        if raw is None:
            raw = (product or {}).get(slot)
        values[slot] = "" if raw is None else (raw if isinstance(raw, str) else "\n".join(map(str, raw)) if isinstance(raw, (list, tuple)) else str(raw))

    def _sub(match: re.Match[str]) -> str:
        return values.get(match.group(1), "")

    return _SLOT_PATTERN.sub(_sub, skeleton or "")


def render_prompt(
    *,
    skeleton: str,
    modules: Iterable[Any] = (),
    product: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    include_base_instruction: bool = True,
) -> str:
    """完整渲染（§5.1 四段拼接）：基础指令 + 模块 + 骨架 + 商品/素材注入。

    骨架里已显式写了 `{item_title}` 这类商品槽位的话，注入发生在骨架内部；
    为避免重复，本函数**不再二次追加商品段落** —— 商品信息只经骨架插槽进入。
    `extra` 的 `images`/`video` 同理（骨架未引用时不追加，保持「模板说了算」）。
    """
    segments: list[str] = []
    if include_base_instruction:
        segments.append(BASE_INSTRUCTION)

    module_text = render_modules(modules)
    if module_text:
        segments.append(module_text)

    body = render_skeleton(skeleton, product=product, extra=extra)
    if body.strip():
        segments.append(body)


    return "\n\n".join(segment for segment in segments if segment.strip())
