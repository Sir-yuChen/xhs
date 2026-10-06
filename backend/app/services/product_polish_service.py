"""商品标题 / 卖点 AI 润色（设计 20 §3，M4 方向 4）。

## 为什么单独一个服务模块

润色是**文本链路**，但它是**商品侧**的文本链路 —— 与笔记侧的 `polish_text` /
`generate_title` 提示词完全不同（设计 20 §3.2 的四业务点论证）：
商品标题有平台字数上限（千帆实测 ≤ 60）、不能含广告法违禁词、必须保留核心卖点词；
卖点要「场景 + 痛点 + 解决方案」结构且**不得编造参数**。

把这段提示词塞进 `ai_service`（通用客户端）= 让通用层背一份商品特化业务知识，
与 `detail_image_prompt.py`（详情图提示词）是**同一种拆法**：提示词是业务知识，
住在服务层，不住在通用客户端里。

## 三条硬纪律

1. **必须走业务点映射**（`resolve_business_provider`，经 API 层的
   `_business_model_context`）—— **不许旁路直连上游**。api_key 在服务端、
   绝不下发；旁路 = 绕过「业务点 → 服务商」的租户隔离与类型校验。
2. **结果不落库**：本模块只回**备选**（标题 / 卖点各 3 个）。用户**采纳**才写
   `shop_products.ai_title` / `ai_selling_point`（走 `shop_products_service.edit_product`）。
   ⚠️ 宿主原为 `product_groups_service.edit_product`，2026-10-05 分组退场后迁到
   `shop_products_service`（分组服务整模块删除，但编辑能力本身保留）。
   润色**过程**在 `llm_gateway_usage_logs` 有调用记录（v1.7 台账口径），
   不为「润色历史」新建表（§13.2 同一语义不留两份实现）。
3. **缺值不编造**：商品没有的字段（品牌 / 卖点参数缺失）就不写进提示词 ——
   编造卖点 = 替用户宣称商品没有的属性（硬约束⑤）。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from backend.app.models.prompt import PromptTemplate
from backend.app.models.user import User
from backend.app.services.business_provider_keys import (
    BUSINESS_KEY_GENERATE_SELLING_POINT,
    BUSINESS_KEY_POLISH_PRODUCT_TITLE,
)

#: 一次给的备选数（设计 20 §3.1：只给一个等于没给，用户没有可比较的对象）。
CANDIDATE_COUNT = 3

#: 千帆商品标题字数上限（设计 20 §3.3 硬约束②）。
TITLE_MAX_LENGTH = 60

#: 卖点字数上限（同上）。
SELLING_POINT_MAX_LENGTH = 100

#: 商品标题润色的系统提示词（设计 20 §3.3 三条硬约束）。
TITLE_SYSTEM_PROMPT = (
    "你是电商商品标题优化专家。把用户给的商品标题润色成更适合小红书店铺的写法，"
    "严格遵守三条：\n"
    "1. 保留原意与核心卖点词，不能润色成另一种商品；\n"
    "2. 不得含「最」「第一」「国家级」等广告法违禁词；\n"
    f"3. 每个标题不超过 {TITLE_MAX_LENGTH} 个字（千帆商品标题上限，超了平台会拒）。\n"
    f"给出 {CANDIDATE_COUNT} 个备选，每行一个，不要编号不要解释。"
)

#: 卖点生成的系统提示词（同上，约束④：不得编造参数）。
SELLING_POINT_SYSTEM_PROMPT = (
    "你是电商卖点文案专家。为商品生成直接可用的卖点文案，严格遵守：\n"
    "1. 每条卖点是一个「场景 + 痛点 + 解决方案」的完整结构；\n"
    f"2. 每条不超过 {SELLING_POINT_MAX_LENGTH} 个字；\n"
    "3. 只用用户提供的商品信息 —— 不知道的参数（材质 / 成分 / 规格）**不要写**，"
    "编造参数等于替商家虚假宣称；\n"
    "4. 不得含广告法违禁词。\n"
    f"给出 {CANDIDATE_COUNT} 个备选，每行一个，不要编号不要解释。"
)

#: 解析备选时去掉的行首编号 / 项目符号（「1.」「-」「、」「①」）。
_LEADING_BULLET = re.compile(r"^[\s]*[0-9一二三四五六七八九①②③④⑤⑥⑦⑧⑨⑩][.、\)）]?\s*")


def _split_candidates(content: str, count: int = CANDIDATE_COUNT) -> list[str]:
    """模型回的整段文本 → 备选列表（去行首编号 / 空行，最多 `count` 条）。

    ⚠️ 模型经常不守「不要编号」的约定（回 `1. xxx` / `- xxx`），这里**无条件**剥
    行首符号 —— 不剥就会出现「1. 商品名」这种带编号的标题直接进备选，
    用户看着像半成品。
    """
    candidates: list[str] = []
    seen: set[str] = set()
    for raw in (content or "").splitlines():
        text = _LEADING_BULLET.sub("", raw).strip().strip("「」\"'").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        candidates.append(text)
        if len(candidates) >= count:
            break
    return candidates


def _item_brief(item: dict[str, Any]) -> str:
    """商品 → 给模型的简表（**只用真实字段**，缺值不编造）。"""
    lines: list[str] = []
    title = str(item.get("title") or "").strip()
    if title:
        lines.append(f"商品名称：{title}")
    article_no = str(item.get("article_no") or "").strip()
    if article_no:
        lines.append(f"货号：{article_no}")
    category = str(item.get("category") or "").strip()
    if category:
        lines.append(f"类目：{category}")
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
    stock = item.get("stock")
    if isinstance(stock, (int, float)) and stock:
        lines.append(f"库存：{int(stock)}")
    return "\n".join(lines)


def _render_template(
    db: Session, user: User, template_id: Optional[int], item: dict[str, Any]
) -> str:
    """渲染用户选的提示词模板（可选；模板 > 手输 > 内置兜底，同出图链路的尺子）。

    ⚠️ 模板必须是**本人的**且 `task_type=text`（越权 / 类型不对 ⇒ 结构化错，
    不静默回落 —— 回落会让用户以为用的是自己的模板，实际是内置默认）。
    """
    from sqlalchemy import select

    from backend.app.services.prompt_renderer import render_prompt

    if template_id is None:
        return ""
    template = db.scalars(
        select(PromptTemplate).where(
            PromptTemplate.id == int(template_id),
            PromptTemplate.user_id == user.id,
        )
    ).first()
    if template is None:
        raise ProductPolishError(
            "提示词模板不存在或不属于当前用户。", code="PROMPT_TEMPLATE_NOT_FOUND"
        )
    if template.task_type != "text":
        raise ProductPolishError(
            f"润色只能用 text 类型模板，这个模板是 {template.task_type}。",
            code="PROMPT_TEMPLATE_TYPE_MISMATCH",
        )
    from backend.app.models.prompt import PromptModule

    modules = (
        db.scalars(
            select(PromptModule).where(PromptModule.id.in_(template.module_ids or []))
        ).all()
        if template.module_ids
        else []
    )
    return render_prompt(
        skeleton=template.prompt_skeleton,
        modules=modules,
        product=item,
        include_base_instruction=False,
    )


class ProductPolishError(RuntimeError):
    """润色的结构化错误（带 `code`）。"""

    def __init__(self, message: str, *, code: str = "PRODUCT_POLISH_FAILED") -> None:
        super().__init__(message)
        self.code = code


def _complete(
    *,
    text_ai_client: Any,
    model_config: Any,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    function: str,
) -> str:
    """调一次文本模型（走 `_complete` 这一个汇聚点 ⇒ 自动记台账）。"""
    return text_ai_client._complete(
        model_config=model_config,
        api_key=api_key,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=0.7,
        platform="xhs",
        function=function,
    )


def polish_product(
    db: Session,
    *,
    current_user: User,
    platform_account_id: int,
    item_id: str,
    template_id: Optional[int],
    instruction: str,
    text_ai_client: Any,
    resolve_context: Callable[[str], tuple[Any, str]],
) -> dict[str, Any]:
    """商品标题 / 卖点润色（设计 20 §3.1）—— **不落库**，只回备选。

    `resolve_context` 由 API 层传入（`_business_model_context` 的闭包）——
    ⚠️ **本模块不自己 import `resolve_business_provider`**：调用链只允许从
    API 层那一个入口走（设计 20 §6 的守卫测的就是这条）。
    """
    from backend.app.services import shop_products_service

    detail = shop_products_service.item_detail(
        db, platform_account_id=platform_account_id, item_id=item_id
    )
    if detail is None:
        raise ProductPolishError(
            "本账号的商品台账里没有这个商品（先同步商品再试）。",
            code="PRODUCT_ITEM_NOT_FOUND",
        )
    item = detail.get("item") or {}
    brief = _item_brief(item)
    template_prompt = _render_template(db, current_user, template_id, item)
    extra = (instruction or "").strip()

    title_model, title_key = resolve_context(BUSINESS_KEY_POLISH_PRODUCT_TITLE.key)
    title_prompt = template_prompt or brief
    if extra:
        title_prompt = f"{title_prompt}\n\n补充要求：{extra}"
    title_content = _complete(
        text_ai_client=text_ai_client,
        model_config=title_model,
        api_key=title_key,
        system_prompt=TITLE_SYSTEM_PROMPT,
        user_prompt=f"润色这个商品标题：\n\n{title_prompt}",
        function="polish_product_title",
    )
    titles = _split_candidates(title_content)
    if not titles:
        # 模型回了空内容 ⇒ 如实报，不拿原标题冒充备选（那是「没润色」不是「备选」）。
        raise ProductPolishError(
            "标题润色没有返回内容，请检查服务商配置或换个说法重试。",
            code="PRODUCT_POLISH_EMPTY",
        )

    point_model, point_key = resolve_context(BUSINESS_KEY_GENERATE_SELLING_POINT.key)
    point_prompt = brief
    if extra:
        point_prompt = f"{point_prompt}\n\n补充要求：{extra}"
    point_content = _complete(
        text_ai_client=text_ai_client,
        model_config=point_model,
        api_key=point_key,
        system_prompt=SELLING_POINT_SYSTEM_PROMPT,
        user_prompt=f"为这个商品生成卖点：\n\n{point_prompt}",
        function="generate_selling_point",
    )
    points = _split_candidates(point_content)
    if not points:
        raise ProductPolishError(
            "卖点生成没有返回内容，请检查服务商配置或换个说法重试。",
            code="PRODUCT_POLISH_EMPTY",
        )

    return {
        "title_candidates": titles,
        "selling_point_candidates": points,
        #: 平台原标题（对照用 —— 用户要能看出 AI 改了哪里）。
        "source_title": str(item.get("title") or ""),
        "model_name": str(getattr(title_model, "model_name", "") or ""),
        #: ⚠️ 明确告诉前端「还没落库」：采纳要走 `PATCH /items/edit`。
        "persisted": False,
    }
