"""模型类型归纳（v2.9，设计 17 §13.2.11.5）。

**解决什么问题**：上游 OpenAI 兼容的 `GET {base_url}/models` **只回模型 id**，
不回「这是文本模型还是图片模型」—— 而本系统要按能力类型路由（text 打
`/chat/completions`、image 打 `/images/generations`）。此前靠**用户手工标注**，
一次拉几十个模型就标到手软。

**本模块做什么**：拉取目录后，用该服务商自己的一个**文本模型**跑**一次** LLM 调用，
把整批 `model_name` 归纳成 `text` / `image` / `multimodal` / `video`。

⚠️ **纪律（沿用 §13.2.1a）**：
- **不猜**：LLM 不可用 / 返回非法值 ⇒ 该模型降级为 `text`（默认值），并在结果里
  如实带 `ok=False` + `reason`，让前端能提示「类型未归纳，已按文本处理，请人工核对」。
- **一次调用判整批**（用户 2026-10-02 拍板「拉取目录后自动批量归纳」）：逐个模型调
  = N 次账单，且慢；整批一次 = 可控、可记账（`FUNCTION_CLASSIFY`）。
- **记账**：走 `complete_text`（唯一文本汇聚点），`function=FUNCTION_CLASSIFY`、
  `platform` 由调用方下传 —— 漏传会静默落空桶（MEMORY 已记）。

⚠️ **本模块不做落库**：只返回「模型名 → 类型」的判读结果。落库由调用方
（`api/llm_gateway.py` 的同步动作）决定 —— 归纳是**建议**，落库是**决定**，
分开才能让「AI 归纳失败」不阻断「目录已拉到」这件事。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from backend.app.core.business_provider_keys import (
    MODEL_TYPE_LABELS,
    MODEL_TYPE_TEXT,
    MODEL_TYPES,
)
from backend.app.services.llm_gateway_usage_service import FUNCTION_CLASSIFY

logger = logging.getLogger(__name__)

#: 一次归纳最多送多少个模型名给 LLM。
#: ⚠️ 目录可能有几百个模型（中转站常见）⇒ 无上限会让 prompt 爆掉、费用不可控。
#: 超出部分**不归纳**（降级 text），并在结果里如实计数 —— 不静默丢弃、不假装全判了。
MAX_MODELS_PER_CALL = 120

#: 单次归纳调用的温度（判类型要的是稳定，不是创意）。
_CLASSIFY_TEMPERATURE = 0.0

_SYSTEM_PROMPT = (
    "你是模型能力分类助手。用户给你一批 OpenAI 兼容服务的模型 id，"
    "你要判断每个模型的能力类型。\n"
    "类型取值（只能从中选一个）：\n"
    f"- {MODEL_TYPE_TEXT}（{MODEL_TYPE_LABELS[MODEL_TYPE_TEXT]}）："
    "纯对话/文本生成模型，如 deepseek-chat、gpt-4、claude-3-sonnet、qwen-plus。\n"
    "- image（图片）：文生图/图生图模型，如 dall-e-3、stable-diffusion、flux、seedream、kolors。\n"
    "- multimodal（多态）：既能对话又能看图/出图的多模态模型，"
    "如 gpt-4o、gpt-4-turbo-vision、qwen-vl、gemini-pro-vision、claude-3-opus（带视觉）。\n"
    "- video（视频）：文生视频/图生视频模型，如 sora、kling、veo、runway、cogvideo。\n"
    "规则：\n"
    "1. 只能输出合法 JSON，不要任何解释、不要 markdown 代码块。\n"
    "2. 无法确定的模型，按 text 处理。\n"
    "3. 输出格式：{\"模型id\": \"类型\", ...}，每个输入模型都要出现。\n"
)


@dataclass
class ClassificationResult:
    """归纳结果。

    - `mapping`：模型名 → 类型（**只含被成功判读的**；未判读的用 `fallback` 取默认）。
    - `ok`：整批是否成功判读（`False` = LLM 不可用/返非法 ⇒ 全部按默认）。
    - `reason`：`ok=False` 时的一句话原因（给前端如实展示，不吞）。
    - `classified` / `total`：成功判读个数 / 送判个数（前端可提示「已归纳 N/M」）。
    """

    mapping: dict[str, str] = field(default_factory=dict)
    ok: bool = False
    reason: str = ""
    classified: int = 0
    total: int = 0


def _strip_code_fence(text: str) -> str:
    """去掉 LLM 常见的 ```json ... ``` 包裹（模型很爱加，加了就不是合法 JSON）。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    # 去掉首行 ```json 与末行 ```
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_classification(raw: str, candidates: list[str]) -> dict[str, str]:
    """把 LLM 返回解析成 `{model_name: model_type}`。

    ⚠️ 三条防呆（都是实测会踩的）：
    1. **只保留入参里出现过的模型名** —— LLM 会自作主张补/改名字，多出来的键丢弃
       （不然会凭空给库里加模型行）。
    2. **只接受 `MODEL_TYPES` 内的类型** —— 返回 `"文本"` / `"chat"` / `"audio"` 一律丢弃
       （丢弃 = 该模型降级默认，不是猜一个近似值）。
    3. 解析失败（非 JSON / 非 dict）⇒ 返回空 dict（调用方据此判 `ok=False`）。
    """
    try:
        data = json.loads(_strip_code_fence(raw))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}

    candidate_set = set(candidates)
    out: dict[str, str] = {}
    for name, value in data.items():
        if not isinstance(name, str) or name not in candidate_set:
            continue
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower()
        if normalized in MODEL_TYPES:
            out[name] = normalized
    return out


def classify_model_types(
    *,
    model_config: object,
    api_key: str,
    model_names: list[str],
    platform: str = "",
) -> ClassificationResult:
    """用一次 LLM 调用归纳整批模型的能力类型（v2.9）。

    `model_config` = 该服务商的**文本模型**上下文（需有 `base_url` / `model_name`
    / `provider_key` / `user_id`）—— 归纳本身也是一次模型调用，必须有个模型来跑它。
    `api_key` = 该服务商解密后的令牌。

    ⚠️ **绝不抛异常**（除参数缺失外的所有失败都吞成 `ok=False`）：归纳是**锦上添花**，
    拉目录成功但归纳失败时，正确行为是「目录照常落库、类型按默认 text、提示用户核对」，
    而不是把整次同步动作打成失败（那会让用户以为连接坏了）。
    """
    total = len(model_names)
    if total == 0:
        return ClassificationResult(ok=True, classified=0, total=0)

    to_classify = model_names[:MAX_MODELS_PER_CALL]
    truncated = total > len(to_classify)

    user_prompt = (
        "请判断以下模型的能力类型，只输出 JSON：\n"
        + json.dumps(to_classify, ensure_ascii=False)
    )

    # 局部导入：避免 `ai_service` ↔ 本模块的循环导入
    from backend.app.services.llm_gateway_client import complete_text

    try:
        raw = complete_text(
            model_config=model_config,
            api_key=api_key,
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=_CLASSIFY_TEMPERATURE,
            platform=platform,
            function=FUNCTION_CLASSIFY,
        )
    except Exception as exc:  # noqa: BLE001 —— 归纳失败不阻断同步（见 docstring）
        logger.warning("模型类型归纳调用失败（%d 个模型）：%s", total, str(exc)[:200])
        return ClassificationResult(
            ok=False,
            reason=f"类型归纳调用失败：{str(exc)[:120]}",
            classified=0,
            total=total,
        )

    mapping = _parse_classification(raw, to_classify)
    if not mapping:
        return ClassificationResult(
            ok=False,
            reason="类型归纳未返回可解析结果（已按文本处理，请人工核对）",
            classified=0,
            total=total,
        )

    reason = ""
    if truncated:
        reason = f"模型过多，仅归纳前 {len(to_classify)} 个（其余按文本处理）"
    return ClassificationResult(
        mapping=mapping,
        ok=True,
        reason=reason,
        classified=len(mapping),
        total=total,
    )
