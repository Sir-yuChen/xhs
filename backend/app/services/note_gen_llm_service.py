"""LLM 全自动生成编排（设计 17 §2 链路 (5)–(7)，T4）。

## 职责边界

本模块只做**「文本生成 + 护栏 + 话题归一化」**这一段，即设计 17 §4.7 的
`generate_full_note()`：

```
组装 Prompt（prompt_renderer）
  → 调上游（llm_gateway_client，记账在 _complete 内自动发生）
    → 解析严格 JSON（_load_json_response / parse_generated_payload）
      → 护栏（product_note_service.run_guardrails）
        → 话题归一化（note_topic_service.parse_topic_tokens）
          → 返回结构化结果给 worker（worker 负责落库）
```

**不落库**：本模块是**纯生成器**，不写 `notes` / `generation_records`。
落库归 worker（`note_gen_llm_service`），这样「生成」与「持久化」的失败边界
清晰 —— 护栏拦截时压根没有半条记录可回滚（§31.4「不落库半条笔记」）。

## 为什么输出 `topics` 而不是一期的 `tags`

设计 17 §4.7 契约是 `{"title","body","topics"}`。一期 `parse_generated_payload`
返回的键是 `tags`（历史契约，服务商品笔记干跑链路）—— **不改它**（改了会连带
`product_note_service.generate_product_note` 与既有测试）。本模块在调用后做一次
**显式键名映射** `tags → topics`，并在文档里写清「`tags` 是模型方的措辞，
`topics` 是我们的领域名」—— 两处词表各自成立，不是两份实现（DEF-044 不适用：
这里不是同一语义的两个实现，而是**一次重命名**，且只有一处重命名）。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.app.services import llm_gateway_client, prompt_renderer
from backend.app.services.llm_gateway_client import (
    LLMGatewayError,
    PLATFORM_XHS,
)
from backend.app.services.llm_gateway_usage_service import (
    FUNCTION_GENERATE,
    STATUS_FAILED,
    STATUS_SUCCESS,
    record_usage_for_model,
)
from backend.app.services.note_topic_service import (
    parse_topic_tokens,
    strip_topic_marker,
)
from backend.app.services.product_note_service import (
    PRODUCT_NOTE_SYSTEM_PROMPT,
    parse_generated_payload,
    product_payload,
    run_guardrails,
)

logger = logging.getLogger(__name__)

#: 生成结果里话题的条数上限（与 `note_topic_service.NOTE_TOPICS_MAX_ITEMS` 同量级；
#: 这里只做一次「模型话太多」的兜底，最终归一化仍由 `parse_topic_tokens` 负责）。
TOPICS_MAX_ITEMS = 20
#: **话题默认区间**（用户口径 2026-10-18：*「话题默认改为6-10」*）——
#: 与 `copy_requirements.CopyRequirements.topic_min/topic_max` 同值。
#: 用于「模型给的数量不足 ⇒ 按商品事实补足到下限」这一处兜底。
#: ⚠️ 两处不同步会被守卫抓（`test_copy_requirements.py` 钉了默认值）。
TOPICS_DEFAULT_MIN = 6
TOPICS_DEFAULT_MAX = 10

#: 生成正文的目标长度（写进 Prompt 的**建议值**，不是硬门禁 ——
#: 门禁由 `validate_note_format` 的 300–800 负责，两者口径不同：
#: 这里是「希望模型写成多长」，那里是「不合格就拦」。）
PROMPT_BODY_RANGE = "600-900"


class NoteGenerationError(RuntimeError):
    """生成链路业务错误：`code` 给 worker 落 `generation_records.error_code`。"""

    def __init__(self, message: str, *, code: str = "LLM_GENERATE_FAILED") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class GeneratedNote:
    """一次生成的结构化产物（**未落库**）。"""

    title: str = ""
    body: str = ""
    #: 归一化后的话题名（已剥平台标记、保序去重）。
    topics: list[str] = field(default_factory=list)
    #: 标题备选（v2.25，设计 41 §11.1.3）：模板要求给 3 条，此处原样带出。
    #: 空 ⇒ 无备选（调用方可回落为 `[title]`）。
    titles: list[str] = field(default_factory=list)
    #: 封面大字文案（v2.25，≤15 字，含核心搜索词）。空 ⇒ 没有封面文案，不阻断。
    cover_text: str = ""
    #: 护栏结果（`GuardrailResult.as_dict()`）；未通过时 `ok=False`。
    guardrails: dict[str, Any] = field(default_factory=dict)
    #: 模型返回的原始文本（**不落库**，仅供排查；调用方别写进 DB，体积大）。
    raw: str = ""
    #: 实际使用的模型名（快照，供 `generation_records.model_name`）。
    model_name: str = ""
    #: 本次生成耗时（毫秒，含上游 + 解析 + 护栏）。
    latency_ms: int = 0
    #: 未通过护栏的原因摘要（供 `generation_records.error_message`）。
    block_reason: str = ""
    #: 话题是否**兜底派生**（模型没给时按商品类目/标题补的）。
    #: ⚠️ 前端必须如实标注「按商品信息补」，不能让人以为是模型原创。
    topics_derived: bool = False

    @property
    def ok(self) -> bool:
        return not self.block_reason

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "title": self.title,
            "body": self.body,
            "topics": self.topics,
            "topics_derived": self.topics_derived,
            "titles": self.titles,
            "cover_text": self.cover_text,
            "guardrails": self.guardrails,
            "model_name": self.model_name,
            "latency_ms": self.latency_ms,
            "block_reason": self.block_reason,
        }


def _guardrail_summary(guardrails: Any) -> str:
    """护栏结果 → 一行摘要（`error_message` 要短，供应商原文不落）。

    为什么自己拼而不直接 `str(guardrails)`：`GuardrailResult` 是 dataclass，
    `str()` 会打出整个对象（含所有命中明细的 repr），长度不可控。这里只取
    「命中了几类 + 前几条说明」，保证落库字段有界（设计 17 §3.5「摘要记」）。
    """
    hits = list(getattr(guardrails, "hits", ()) or ())
    parts: list[str] = []
    if hits:
        labels = [
            f"{getattr(hit, 'category', '')}「{getattr(hit, 'term', '')}」" for hit in hits[:3]
        ]
        parts.append(f"命中{len(hits)}处违禁表达：" + "、".join(labels))
    unsupported = list(getattr(guardrails, "unsupported_prices", ()) or ())
    if unsupported:
        parts.append("疑似编造价格：" + "、".join(unsupported[:3]))
    price_note = str(getattr(guardrails, "price_note", "") or "")
    if parts and price_note:
        parts.append(price_note)
    return "；".join(parts)


#: 类目词 → 平台常用同义/上位话题（**固定映射，不是自由拼词**）。
#:
#: ⚠️ 存在的理由：类目路径通常只有 3 级，派生只能出 3 个话题，
#: 而默认要求 6–10 个 ⇒ 用户设了 6–10 却只拿到 3 个（2026-10-18 真机）。
#: 补出来的每个词都必须是该品类的**真实搜索词**，否则会污染 `hash_tag`。
_TOPIC_SYNONYMS: dict[str, tuple[str, ...]] = {
    "梳妆台": ("化妆台", "梳妆桌", "卧室家具"),
    "桌": ("桌子", "书桌", "电脑桌"),
    "桌类": ("桌子", "书桌"),
    "椅": ("椅子", "凳子"),
    "椅类": ("椅子", "凳子"),
    "柜": ("柜子", "收纳柜"),
    "柜类": ("柜子", "收纳柜"),
    "床": ("床具", "卧室家具"),
    "床类": ("床具", "卧室家具"),
    "沙发": ("布艺沙发", "客厅家具"),
    "住宅家具": ("家具", "家居", "居家好物"),
    "家装主材": ("家装", "装修"),
}


def derive_topics_fallback(
    *,
    product: dict[str, Any],
    title: str,
    limit: int = 6,
) -> list[str]:
    """模型没给话题时，按**商品类目 + 标题**派生话题（用户口径 2026-10-18）。

    ## 为什么需要它（不是「多加一层规则」）

    实测 deepseek-v4.1-flash **稳定返回 `tags: []`** —— 提示词里写「必须给满、
    不许留空数组」也照旧返回空。这是**模型侧的行为**，改提示词改不动。

    而话题是发布必需品：`hash_tag` 靠它，空着这篇笔记发不出去。
    「模型不给 ⇒ 整篇作废」把模型的怪癖变成用户的损失，不合理。

    ## 派生依据（都是**商品事实**，不编造）

    1. **最末级类目**：`住宅家具 / 桌类 / 梳妆台/桌` ⇒ `梳妆台`（平台词最常见）；
    2. **类目上下级**：`桌类` ⇒ `桌子`；
    3. **标题里的品类词**：从标题尾部取 2–4 字的品类名（`梳妆台` / `化妆桌`）。

    去重、保序、截到 `limit`。**不造词、不拼营销词** —— 派生出的每个话题都能在
    商品事实里找到出处。
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        text = strip_topic_marker(str(name or "")).strip()
        #: 太短的（单字）和太长的（一整句标题）都不是话题
        if not (2 <= len(text) <= 12):
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(text)

    #: ① 类目路径（`/` 或 `>` 分隔），从**最末级**往前取
    category = str(product.get("category") or "")
    parts = [p.strip() for p in re.split(r"[/>]", category) if p.strip()]
    for part in reversed(parts):
        #: 「梳妆台/桌」这种一格多名的，拆开各算一个
        for piece in part.replace("、", "/").split("/"):
            _add(piece)
        if len(out) >= limit:
            return out[:limit]

    #: ①b **类目同义扩展**（2026-10-18 实测缺口）：类目路径往往只有 3 级
    #: （`住宅家具 / 桌类 / 梳妆台/桌`）⇒ 派生只有 3 个词，而默认要求 6–10 个，
    #: 用户设了 6–10 却只拿到 3 个。这里给**已出现的类目词**补上平台常用同义/上位词
    #: （桌子 / 家具 / 卧室家具 / 化妆台 …），⚠️ 只按下面的**固定映射表**补，
    #: 不做任何自由拼词 —— 每个补出来的词都必须是对应类目词在小红书上的真实叫法。
    for base, synonyms in _TOPIC_SYNONYMS.items():
        if base not in out:
            continue
        for synonym in synonyms:
            _add(synonym)
        if len(out) >= limit:
            return out[:limit]

    #: ② **不再从标题尾部截片段**。
    #:
    #: ⚠️ 实测反例（2026-10-18）：标题「一张梳妆台，搞定化妆+办公」按末尾截 2/3/4 字
    #: ⇒ `办公` / `+办公` / `妆+办公` —— 这些**不是话题**，是句子的碎片。
    #: 话题要能被搜索命中，碎片词只会污染 `hash_tag`。
    #: 所以只在这一步**匹配科目标品类词**：标题里**出现过**的类目词才收，
    #: 截片段这条路整条不走。
    text = str(title or "")
    for part in parts:
        for piece in part.replace("、", "/").split("/"):
            piece = piece.strip()
            if len(piece) >= 2 and piece in text:
                _add(piece)
        if len(out) >= limit:
            break

    return out[:limit]


def _normalize_topics(raw_topics: Any) -> list[str]:
    """模型给的 `topics`（列表 / 逗号串 / 带 `#`）→ 归一化话题名列表。

    两步：① 先把模型可能写的 `#话题` / `#话题[话题]#` 形态统一（`parse_topic_tokens`
    对已带 `#` 的串能直接解析）；② 对纯名字列表逐条 `strip_topic_marker` 剥标记。
    最终**保序去重**（小写键），限量 `TOPICS_MAX_ITEMS`。
    """
    if raw_topics is None:
        return []
    if isinstance(raw_topics, str):
        # 模型偶发把话题写成 `#A #B` 一串：先按 token 解析（能同时剥 `[话题]` 标记）。
        items = parse_topic_tokens(raw_topics) or [
            part for part in raw_topics.replace("，", ",").split(",") if part.strip()
        ]
    elif isinstance(raw_topics, (list, tuple, set)):
        items = list(raw_topics)
    else:
        return []

    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        name = strip_topic_marker(str(item or ""))
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= TOPICS_MAX_ITEMS:
            break
    return out


def build_generation_skeleton_hint() -> str:
    """无模板时的**兜底** Prompt（商品字段插槽 + 严格 JSON 输出要求）。

    设计 17 里模板是可选的（`template_id` nullable）。没有模板时不能什么都不给
    —— 那会退化成「让模型自由发挥」，护栏必然大面积拦截。这里给一份内置骨架，
    口径与 `prompt_renderer.BASE_INSTRUCTION` + §4.7 的 JSON 契约一致。

    ⚠️ **v2.25（设计 41 §11.1.3）**：本兜底骨架**对齐**种子里「商品笔记文案（默认）」
    模板的六段位（人群场景 / 标题 3 备选 / 三段式正文 / 三三法则话题 / 封面文字 /
    JSON 契约）。理由：兜底若比模板弱，那么「用户没选模板」就等价于「拿到一份更差的
    提示词」—— 而没选模板恰恰是新用户第一次用时的**默认状态**（`template_id` nullable）。
    ⚠️ 两者是**同一口径的两份载体**（种子是用户可改的起点，本函数是兜底），
    不是两份实现：判据为「兜底六段位 ⊇ 模板六段位」，由守卫钉死。
    """
    return (
        "为小红书商品笔记撰写文案。\n\n"
        "【商品信息】（唯一事实来源，逐条只能引用，不得推算或补充）\n"
        "{item_id}\n{title}\n{article_no}\n{price_text}\n{stock}\n{sku_count}\n"
        "{on_sale_sku_count}\n{category}\n{sales_total}\n\n"
        "【① 人群与场景】\n"
        "先从类目与属性推断「谁会买、在什么场景用」，把人群词与场景词写进标题与正文开头。\n\n"
        "【② 标题】给出 3 个备选，每条 ≤20 字，核心搜索词必须前置（前 4–6 字内出现），"
        "分别用这 3 套写法：\n"
        "- 人群 + 痛点 + 解决方案；\n"
        "- 数字 + 结果；\n"
        "- 反常识或提问代入。\n\n"
        "【③ 正文】600–900 字，按三段式：\n"
        "- 开头 50 字内直击痛点，不要铺垫背景；\n"
        "- 中间分点讲 2–3 个核心卖点（不要把全部属性罗列一遍），每点要有具体使用场景；\n"
        "- 结尾总结 + 一个低门槛互动钩子。\n"
        "正文开头 100 字内必须出现一次核心搜索词；核心词全文出现 3–5 次，不要超过 10 次。\n"
        "价格只能写上面【商品信息】里的价格，不要算优惠、不要编造到手价。\n\n"
        "【④ 话题标签】给 9 个，按三三法则分三组：3 个核心词、3 个长尾词、3 个场景词"
        "（**只写名字，不要带 # 号**）。\n\n"
        "【⑤ 封面文字】给一句 ≤15 字的大字文案，含核心搜索词"
        "（这句会被写到封面上，平台会 OCR 识别计入关键词权重）。\n\n"
        "【⑥ 输出格式】只输出 JSON，不要任何解释文字：\n"
        '{"titles": ["备选1", "备选2", "备选3"], "title": "选定的那一个", '
        '"body": "正文", "topics": ["话题1", "话题2"], "cover_text": "封面大字"}\n\n'
        "【禁止】\n"
        "- 禁止医疗/功效宣称（治疗、根治、祛痘、美白、抗皱、除螨…）；\n"
        "- 禁止绝对化用语（最好、第一、顶级、全网最低、国家级…）；\n"
        "- 禁止编造认证、检测、销量、评价；禁止站外引流（微信/淘宝/链接）；\n"
        "- 禁止 AI 腔标志词（此外、至关重要、值得一提、综上所述、赋能、打造闭环）。"
    )


def generate_full_note(
    *,
    db: Any,
    user_id: int,
    product: dict[str, Any],
    model_config: Any,
    api_key: str,
    skeleton: str = "",
    modules: list[Any] = (),
    extra: Optional[dict[str, Any]] = None,
    instruction: str = "",
    run_guardrail_check: bool = True,
    required_fields: tuple[str, ...] = ("title", "body"),
    copy_requirements: Any = None,
) -> GeneratedNote:
    """生成一篇完整笔记（标题 + 正文 + 话题），**不落库**。

    `product` 是商品字典（**必须**经过 `product_payload` / `normalize_item_from_raw`
    同口径构造 —— 键名要与 `PRODUCT_FIELDS` 对齐，否则插槽填不进去）。
    这里会再调一次 `product_payload()` 保证白名单（幂等：白名单提取可重复应用）。

    异常语义：
    * `NoteGenerationError` —— 业务可归因的失败（上游没配 / 渲染槽位错 / 模型没按
      JSON 回 / 护栏拦截）。`code` 直接落 `generation_records.error_code`。
    * 上游网络等未归类异常**原样抛出**，由 worker 的外层网兜住并落 `failed`。

    ⚠️ 记账在 `complete_text` → `_complete()` 内部发生（**只有一处**）。
    本函数**不重复记账**；护栏拦截发生在**上游返回之后**，因此那次调用照常记账
    （`status=success`，上游确实成功返回了）—— 这是对的：用量看板要反映真实上游
    消耗，护栏是**我们的**后置校验，不是上游失败。
    """
    started = time.perf_counter()
    facts = product_payload(product)

    # ---- ① 组装 Prompt —— 渲染失败（模板槽位写错）不调上游，直接归因 ----
    skeleton_text = skeleton or build_generation_skeleton_hint()
    try:
        prompt = prompt_renderer.render_prompt(
            skeleton=skeleton_text,
            modules=modules,
            product=facts,
            extra=extra,
        )
    except prompt_renderer.PromptRenderError as error:
        raise NoteGenerationError(error.message, code=error.code) from error

    if instruction.strip():
        prompt = f"{prompt}\n\n【额外风格要求】{instruction.strip()}"

    # ---- ② 调上游（记账自动发生；参数校验失败不记账） ----
    try:
        content = llm_gateway_client.complete_text(
            model_config=model_config,
            api_key=api_key,
            system_prompt=PRODUCT_NOTE_SYSTEM_PROMPT,
            user_prompt=prompt,
            temperature=0.8,
            platform=PLATFORM_XHS,
            function=FUNCTION_GENERATE,
        )
    except LLMGatewayError as error:
        # v2.0：业务点映射的失败都从这里出去，各自带结构化 code
        # （`LLM_BUSINESS_PROVIDER_*` / `LLM_PROVIDER_*`）。
        # ⚠️ 一期的 `TextModelNotConfiguredError` 已随 `model_configs` 退场删除
        # （v1.9 它就是本基类的子类，单列只会造死分支）。
        raise NoteGenerationError(error.message, code=error.code) from error
    except ValueError as error:
        # `_complete()` 的参数校验分支（缺 base_url / model_name / api_key）——
        # 归因到「配置不全」，而不是「上游挂了」（两者运维动作完全不同）。
        raise NoteGenerationError(str(error), code="LLM_GATEWAY_NOT_CONFIGURED") from error

    latency_ms = int((time.perf_counter() - started) * 1000)

    # ---- ③ 解析严格 JSON ----
    try:
        parsed = parse_generated_payload(content)
    except ValueError as error:
        raise NoteGenerationError(
            f"模型未按 JSON 格式返回：{error}", code="LLM_RESPONSE_NOT_JSON"
        ) from error

    title = str(parsed.get("title") or "").strip()
    body = str(parsed.get("body") or "").strip()
    #: **局部重生成只要求目标块**（用户口径 2026-10-18）：提示词里已明确
    #: 「只输出正文，其余不要输出」，模型照做后这里再按整篇判「缺标题」就自相矛盾。
    #: 缺的那几块由 `_merge_text_scope` 从原值补回，所以这里**不判**。
    #: ⚠️ 默认为整篇的 `("title", "body")` —— 整篇生成的口径一个字没变。
    missing = [
        label
        for field_name, label in (("title", "标题"), ("body", "正文"))
        if field_name in required_fields and not (title if field_name == "title" else body)
    ]
    if missing:
        raise NoteGenerationError(
            "模型返回缺少" + "或".join(missing), code="LLM_RESPONSE_INCOMPLETE"
        )

    # `tags` / `topics` 都认 —— **模型方措辞不统一**：
    # 默认文本模板的输出契约写的是 `topics`（设计 17 §4.7 的领域名），
    # 但上游模型（尤其 deepseek 系）常按自己的习惯返回 `tags`。
    # ⚠️ 曾经只读 `tags` ⇒ 模板要求 `topics`、模型照做 ⇒ **话题恒为 0**
    # （2026-10-18 真机：成功生成、正文 198 字、话题 0 个）。
    # 这里两个键都收，第一个非空者胜；都空才是真的没给。
    topics = _normalize_topics(parsed.get("topics") or parsed.get("tags"))
    #: 话题是不是**兜底派生**的（模型没给时按商品信息补）—— 前端要如实标注。
    topics_derived = False
    #: 话题为 0 时留一条**可诊断**的日志（不猜原因）：
    #: 模型可能真的没给，也可能给了别的键名（`hashtags` / `topic_list` / 嵌在正文末尾）。
    #: ⚠️ 只记**键名**，不记正文（日志不该成为内容副本）。
    if not topics:
        logger.warning(
            "LLM 返回里没有可识别的话题（键：%s；topics=%r；tags=%r）—— 检查模板输出契约与模型实际措辞",
            sorted(parsed.keys()),
            parsed.get("topics"),
            parsed.get("tags"),
        )
        #: **兜底补话题**（2026-10-18 实测：deepseek-v4.1-flash 稳定返回 `tags: []`，
        #: 连「必须给满、不许留空数组」的强指令也不遵守 —— 模型侧固执，改提示词无效）。
        #: 话题是发布必需品（`hash_tag` 靠它），空着等于这篇笔记发不出去，
        #: 所以由**商品类目 + 标题**派生补齐，并如实标记 `topics_derived=True`
        #: 让前端能说明「这些是按商品信息补的，不是模型原创」。
        derived = derive_topics_fallback(product=product, title=title)
        if derived:
            topics = derived
            topics_derived = True
    #: **数量不足也要补**（2026-10-18 真机缺陷）：用户设了话题 6–10 个，
    #: 模型只给 3 个（`[{梳妆台, 桌类, 住宅家具}]`）⇒ 界面上只有 3 个，
    #: 用户看到的是「设置无效」。这里是「不足则补足到下限」，不是「重写模型结果」：
    #: **模型给的前 N 个原样保留在前**，缺的才按商品事实派生补在后面，
    #: 补出来的部分同样标 `topics_derived=True`（前端如实说明来源）。
    elif len(topics) < TOPICS_DEFAULT_MIN:
        extra = derive_topics_fallback(
            product=product, title=title, limit=TOPICS_DEFAULT_MAX
        )
        seen_keys = {t.lower() for t in topics}
        for candidate in extra:
            if candidate.lower() in seen_keys:
                continue
            seen_keys.add(candidate.lower())
            topics.append(candidate)
            if len(topics) >= TOPICS_DEFAULT_MIN:
                break
        if len(topics) > len(parsed.get("topics") or parsed.get("tags") or []):
            topics_derived = True
    # v2.25：标题备选与封面大字（可选增强，缺省不影响既有契约）。
    # ⚠️ `titles` 里若**没有**选定的 `title`，必须把它补进去 —— 否则前端下拉里
    # 看不到当前实际生效的那一条（用户会以为选的和写的不一致）。
    titles = [t for t in (parsed.get("titles") or []) if t]
    if title and title not in titles:
        titles.insert(0, title)
    cover_text = str(parsed.get("cover_text") or "").strip()

    model_name = str(getattr(model_config, "model_name", "") or "")

    # ---- ④ 护栏（可关，见 `run_guardrail_check`） ----
    if not run_guardrail_check:
        return GeneratedNote(
            title=title,
            body=body,
            topics=topics,
            topics_derived=topics_derived,
            titles=titles,
            cover_text=cover_text,
            guardrails={},
            raw=content,
            model_name=model_name,
            latency_ms=latency_ms,
        )

    guardrails = run_guardrails(title, body, facts)
    # 被拦也把正文带回去（`ok=False` + `block_reason`）—— 上层要能落一条
    # `generation_records` 说明「为什么没成」，静默丢弃会让失败原因查不到。
    #: **兜底纠偏**（用户口径 2026-10-18：*「页面中的配置参数，应一并提供给大模型，作为提示词」*）：
    #: 参数已经进提示词了，但模型不一定遵守（实测要求正文 30–150 字、实际写了 769 字）。
    #: `enforce` 是最后一道闸：**标题超长就裁**、**话题超量就截**。
    #: ⚠️ 正文**不硬裁**（会把句子砍断，读起来像 bug）—— 正文长度靠提示词约束 + 发布前校验。
    if copy_requirements is not None:
        from backend.app.services.copy_requirements import trim_title

        title = trim_title(title, copy_requirements)
        if len(topics) > copy_requirements.topic_max:
            topics = topics[: copy_requirements.topic_max]

    block_reason = "" if guardrails.passed else _guardrail_summary(guardrails)
    if block_reason and not block_reason.strip():
        block_reason = "未通过内容护栏"
    return GeneratedNote(
        title=title,
        body=body,
        topics=topics,
            topics_derived=topics_derived,
        titles=titles,
        cover_text=cover_text,
        guardrails=guardrails.as_dict(),
        raw=content,
        model_name=model_name,
        latency_ms=latency_ms,
        block_reason=block_reason,
    )


def record_generation_usage_failure(
    *,
    model_config: Any,
    error_message: str,
    platform: str = PLATFORM_XHS,
    function: str = FUNCTION_GENERATE,
) -> None:
    """给**渲染/解析阶段**的失败补一行台账（可选，供 worker 调用）。

    什么时候需要：Prompt 渲染失败时上游**没被调用** ⇒ 不该记账（假账）；
    但若我们决定把「渲染失败」也计入「生成失败率」，就显式调用本函数记一行
    `failed`。默认 worker **不调用**（口径：台账记「上游调用」，不记「我们的
    前置校验失败」—— 与 §4.3.1「参数校验失败不记账」同纪律）。

    走 `record_usage_for_model`（**不是** `record_usage`）：后者要求
    `gateway_id: int` 必填，而前置失败时我们手里只有 `model_config`，
    反查网关 id 这一步正是它替我们做的（反查不中则跳过，宁缺勿造）。
    """
    try:
        record_usage_for_model(
            model_config=model_config,
            platform=platform,
            function=function,
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=0,
            status=STATUS_FAILED,
            error_message=error_message,
        )
    except Exception:  # noqa: BLE001 —— 记账旁路绝不抛出（DEF-048）
        logger.warning("补记生成失败台账时异常（已忽略）", exc_info=True)


__all__ = [
    "GeneratedNote",
    "NoteGenerationError",
    "TOPICS_MAX_ITEMS",
    "STATUS_SUCCESS",
    "build_generation_skeleton_hint",
    "generate_full_note",
    "record_generation_usage_failure",
]
