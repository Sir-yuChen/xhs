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

    @property
    def ok(self) -> bool:
        return not self.block_reason

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "title": self.title,
            "body": self.body,
            "topics": self.topics,
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
    """
    return (
        "【商品信息】（唯一事实来源，逐条只能引用，不得推算或补充）\n"
        "{item_id}\n{title}\n{article_no}\n{price_text}\n{stock}\n{sku_count}\n"
        "{on_sale_sku_count}\n{category}\n{sales_total}\n\n"
        "【输出结构】只输出 JSON，不要任何解释文字：\n"
        f'{{"title": "标题", "body": "正文", "topics": ["话题1", "话题2"]}}\n\n'
        "【要求】\n"
        "1. 标题 ≤20 字，包含商品核心词；\n"
        f"2. 正文 {PROMPT_BODY_RANGE} 字，分段，用使用场景 + 具体细节；\n"
        "3. 价格只能写上面【商品信息】里的价格，不要算优惠、不要编造到手价；\n"
        "4. 话题 3–6 个，与商品类目相关（**只写名字，不要带 # 号**）。\n\n"
        "【禁止】\n"
        "- 禁止医疗/功效宣称（治疗、根治、祛痘、美白、抗皱、除螨…）；\n"
        "- 禁止绝对化用语（最好、第一、顶级、全网最低、国家级…）；\n"
        "- 禁止编造认证、检测、销量、评价；禁止站外引流（微信/淘宝/链接）。"
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
    if not title or not body:
        raise NoteGenerationError("模型返回缺少标题或正文", code="LLM_RESPONSE_INCOMPLETE")

    # `tags`（模型方措辞）→ `topics`（领域名）—— 唯一的显式重命名处。
    topics = _normalize_topics(parsed.get("tags"))

    model_name = str(getattr(model_config, "model_name", "") or "")

    # ---- ④ 护栏（可关，见 `run_guardrail_check`） ----
    if not run_guardrail_check:
        return GeneratedNote(
            title=title,
            body=body,
            topics=topics,
            guardrails={},
            raw=content,
            model_name=model_name,
            latency_ms=latency_ms,
        )

    guardrails = run_guardrails(title, body, facts)
    # 被拦也把正文带回去（`ok=False` + `block_reason`）—— 上层要能落一条
    # `generation_records` 说明「为什么没成」，静默丢弃会让失败原因查不到。
    block_reason = "" if guardrails.passed else _guardrail_summary(guardrails)
    if block_reason and not block_reason.strip():
        block_reason = "未通过内容护栏"
    return GeneratedNote(
        title=title,
        body=body,
        topics=topics,
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
