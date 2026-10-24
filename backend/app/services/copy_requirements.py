"""文案要求（标题 / 正文 / 话题各自的提示词 + 字数区间 + 表情 + 数量）。

设计来源：用户口径 2026-10-17 ——

> 「文案区域支持用户手动输入标题提示词和笔记正文，以及笔记话题，分析支持提供各自的
>   提示词，也可以为空，设置一个各自的文字数区间和是否带表情，以及生成的话题数量区间，
>   均需要限制」

## 为什么单独成模块（而不是塞进 worker）

**纯函数 + 无 IO** ⇒ 可以脱离数据库 / 上游模型单测（`tests/backend/test_copy_requirements.py`）。
若写在 worker 里，每验一条区间规则都要连带跑一次队列，测试成本高到没人会跑。

## 三层结构

1. `normalize()` —— **入参 → 规整后的约束**（区间夹取、互斥校验、缺省补默认）；
   API 同步段就调它，**非法立刻 400**（确定性答案，不入队）。
2. `build_instruction()` —— 约束 → **追加到提示词末尾的自然语言要求**。
3. `enforce()` —— 模型返回后**兜底裁剪**（模型不听话时的第二道闸）。

⚠️ 三层**同一份约束对象**贯穿：入参校验与落地执行不能两套数字（否则前端说 10-20 字、
后端按 50-100 字执行，用户永远调不准）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

#: 平台硬约束（小红书）：标题 ≤20 字 / 正文 ≤1000 字。
#: 与 `note_publish_service` 的发布前校验**同源**（那边是最后一道闸，这边是生成段的闸）。
TITLE_MAX = 20
BODY_MAX = 1000

#: 生成侧的**合理**范围（不是平台上限，而是「再宽就没意义」）。
#: ⚠️ 标题上限直接对齐平台 20 —— 生成出 25 字标题，发布时必然被裁，等于白生成。
TITLE_RANGE = (1, TITLE_MAX)
BODY_RANGE = (1, BODY_MAX)
TOPIC_COUNT_RANGE = (1, 10)

#: 表情开关（用户口径「是否带表情」）—— 三态而不是布尔：
#: `auto` 让模型按商品调性决定（美妆饰品适合、家具建材未必），硬给 true 会让
#: 「现代简约家具」也长出 🛋️✨，那是噪音。
EMOJI_MODES = ("auto", "on", "off")


class CopyRequirementError(ValueError):
    """文案要求非法（API 层映射 400）。"""

    def __init__(self, message: str, code: str = "LLM_COPY_INVALID") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    """宽松取整并夹取（非法值退默认，**不抛**）。

    ⚠️ 与 `normalize()` 的分工：这里只做「值本身不合法」（None / 非数字），
    退默认即可；「区间上下界矛盾」这类**用户表意错误**必须抛（见 `normalize`）。
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


@dataclass
class CopyRequirements:
    """一篇笔记的文案要求（**规整后**的形态）。"""

    #: 各自独立的提示词（空串 = 不额外要求，走模板默认）。
    title_prompt: str = ""
    body_prompt: str = ""
    topic_prompt: str = ""
    #: 字数区间（闭区间，字数 = `len()`，与平台计数口径一致）。
    title_min: int = 8
    title_max: int = TITLE_MAX
    #: 用户口径 2026-10-17：正文 **30–300 字**（不是平台上限 1000 ——
    #: 生成 1000 字的小红书正文没人看完，300 字是「说清楚 + 能读完」的实用区间）。
    body_min: int = 30
    body_max: int = 300
    #: 话题数量区间。
    #: 话题默认数量（用户口径 2026-10-18：*「话题默认改为6-10」*）——
    #: 原默认 3–6。⚠️ 平台侧话题本身没有硬上限，这里的 10 是**默认建议上限**
    #: （再多就有堆砌感，也会被平台判定关键词滥用）；用户仍可在弹窗里自行调。
    topic_min: int = 6
    topic_max: int = 10
    #: 表情：`auto` / `on` / `off`（正文与标题共用；话题不带表情）。
    emoji: str = "auto"

    def as_dict(self) -> dict[str, Any]:
        """落库形态（进队列行体 `payload["copy"]`）。"""
        return {
            "title_prompt": self.title_prompt,
            "body_prompt": self.body_prompt,
            "topic_prompt": self.topic_prompt,
            "title_min": self.title_min,
            "title_max": self.title_max,
            "body_min": self.body_min,
            "body_max": self.body_max,
            "topic_min": self.topic_min,
            "topic_max": self.topic_max,
            "emoji": self.emoji,
        }

    @property
    def is_empty(self) -> bool:
        """全是默认值 ⇒ 不写行体（省得每个队列项都背一份默认配置）。"""
        return self.as_dict() == CopyRequirements().as_dict()


def _clean_prompt(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if len(text) > 500:
        raise CopyRequirementError(f"{label}提示词最多 500 字（实得 {len(text)} 字）")
    return text


def normalize(raw: Optional[dict[str, Any]]) -> CopyRequirements:
    """入参 → `CopyRequirements`（**同步段调用：非法立即 400**）。

    判据（逐条与用户口径对齐）：

    1. 三块提示词**各自可空**（*「也可以为空」*）—— 空 ⇒ 不加任何额外要求。
    2. 字数/数量**必须成区间**且 `min <= max`（*「设置一个各自的文字数区间」*）。
       `min > max` ⇒ 400（不静默交换：用户可能填反了，静默纠正会让他以为规则生效）。
    3. **标题上限夹到 20**（平台硬顶）—— 用户填 50 也不给，因为生成出来发布不了。
    4. **话题数量至少 1**（*「均需要限制」*）：0 个话题等于「不生成话题」，
       但那与「话题区留空」是两件事，这里按「必须有话题」处理（平台侧话题是流量入口）。
    """
    data = raw if isinstance(raw, dict) else {}

    result = CopyRequirements()
    result.title_prompt = _clean_prompt(data.get("title_prompt"), "标题")
    result.body_prompt = _clean_prompt(data.get("body_prompt"), "正文")
    result.topic_prompt = _clean_prompt(data.get("topic_prompt"), "话题")

    emoji = str(data.get("emoji") or "auto").strip().lower()
    if emoji not in EMOJI_MODES:
        raise CopyRequirementError(
            f"表情取值只能是 {'/'.join(EMOJI_MODES)}（实得 {emoji!r}）"
        )
    result.emoji = emoji

    def _pair(key_min: str, key_max: str, rng: tuple[int, int], label: str,
              default_min: int, default_max: int) -> tuple[int, int]:
        lo = _clamp_int(data.get(key_min), rng[0], rng[1], default_min)
        hi = _clamp_int(data.get(key_max), rng[0], rng[1], default_max)
        if lo > hi:
            raise CopyRequirementError(
                f"{label}的字数下限（{lo}）不能大于上限（{hi}）"
                if label != "话题"
                else f"话题数量下限（{lo}）不能大于上限（{hi}）",
                code="LLM_COPY_RANGE_INVALID",
            )
        return lo, hi

    result.title_min, result.title_max = _pair(
        "title_min", "title_max", TITLE_RANGE, "标题", result.title_min, result.title_max
    )
    result.body_min, result.body_max = _pair(
        "body_min", "body_max", BODY_RANGE, "正文", result.body_min, result.body_max
    )
    result.topic_min, result.topic_max = _pair(
        "topic_min", "topic_max", TOPIC_COUNT_RANGE, "话题", result.topic_min, result.topic_max
    )
    return result


def build_instruction(req: CopyRequirements) -> str:
    """约束 → **对模板的补充说明**（**没有一项被改动 ⇒ 返回空串**）。

    ## 层级关系（用户口径 2026-10-17）

    用户原话：

    > 「已经有一个提示词模板与下面的各个部分的提示词以及相关的配置是否有冲突，
    >   但是各个部分的提示词用户是**可选填**的，关系应是**对顶部公共模板的补充说明**」

    ⇒ **模板是主体，这三块是补充**：
      - 模板决定骨架、写法、输出格式（有模板就按模板走）；
      - 下面三块的提示词**选填**，填了才有、没填**一个字都不加**；
      - 字数/数量/表情这些「配置项」同理：**用户没动过默认值就不下发给模型**
        （否则模板里写死的「600–900 字」会和默认的「30–300 字」在同一个提示词里
        **互相打架**，模型不知道听谁的 —— 这正是用户指出的「冲突」）。

    ## 实现判据

    逐项与 `DEFAULTS`（即 `CopyRequirements()` 的默认值）比较，**只输出与默认不同的项**；
    全部相同 ⇒ 返回空串（提交时连这一段都不拼）。

    ⚠️ 不这样做的话：每个模板都在被一段固定的「标题 8–20 字 / 正文 30–300 字 /
    话题 3–6 个 / 表情自动」覆盖，模板自己的节奏（比如「三段式 600–900 字」）
    永远失效，而用户看不出是哪里覆盖的。
    """
    base = CopyRequirements()
    lines: list[str] = []

    #: 三块提示词：**填了才算补充**
    if req.title_prompt:
        lines.append(f"- 标题方向：{req.title_prompt}")
    if req.body_prompt:
        lines.append(f"- 正文方向：{req.body_prompt}")
    if req.topic_prompt:
        lines.append(f"- 话题方向：{req.topic_prompt}")

    #: 区间：**只在用户改过默认值时下发**（未改 ⇒ 交给模板自己说）
    if (req.title_min, req.title_max) != (base.title_min, base.title_max):
        lines.append(f"- 标题字数改为 {req.title_min}–{req.title_max} 字（含标点）")
    if (req.body_min, req.body_max) != (base.body_min, base.body_max):
        lines.append(f"- 正文字数改为 {req.body_min}–{req.body_max} 字（含标点）")
    if (req.topic_min, req.topic_max) != (base.topic_min, base.topic_max):
        lines.append(f"- 话题数量改为 {req.topic_min}–{req.topic_max} 个")

    #: 表情：`auto` 是默认（交给模板/模型判断）⇒ 只有明确 on/off 才说
    if req.emoji == "on":
        lines.append("- 标题与正文请带适量表情符号（话题不带）")
    elif req.emoji == "off":
        lines.append("- 标题与正文不要出现表情符号")

    if not lines:
        #: **空 ⇒ 完全按模板走**（这是「补充说明」的语义：不补充就一个字都不加）
        return ""

    header = "【补充说明】以下要求是在上述模板基础之上的补充，与模板冲突时以此处为准："
    return header + chr(10) + chr(10).join(lines)


def build_regen_instruction(
    scope: str,
    *,
    snapshot: Optional[dict[str, Any]] = None,
) -> str:
    """**局部重生成的专属说明**（用户口径 2026-10-18 追问：*「生成局部的
    局部时的提示词如何给，你确认一下」*）。

    ## 为什么需要（不这么做的实际后果）

    局部重生成原来**直接复用整篇生成的提示词** —— 模板说的是「写一篇完整笔记」，
    模型就照写完整三块，代码事后把不需要的两块丢掉。两个代价：

    1. **省钱但不省 token**：让它写三块再用一块，输出长度是 3 倍；
    2. **更严重：读不到上下文**。只改正文时，模型不知道当前标题是什么 ⇒
       新正文与标题的呼应（标题里的核心词、语气）会脱节，用户看到的是
       「正文换了，但跟标题对不上」。

    ## 给法（**保持模板主体地位**，与 `build_instruction` 同一层级）

    仍以模板为骨架，只在末尾追加一段**上下文 + 本次只要哪一块**：

    * 已知内容（要保留的两块）**原样贴出**，让模型有据可依；
    * 明确「只重写 X，其余不要输出」；
    * 其余两块用原文，不要改写 —— 因为它们的最终值由代码从原值搬回，
      模型多写也只是浪费（但告诉它，能显著改善新块的衔接）。

    `scope == "media"` 不调文本模型 ⇒ 返回空串。
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    title = str(snapshot.get("title") or "").strip()
    body = str(snapshot.get("body") or "").strip()
    topics = [str(t) for t in (snapshot.get("topics") or []) if str(t).strip()]

    known: list[str] = []
    if title:
        known.append(f"当前标题：{title}")
    if body:
        known.append(f"当前正文：{body}")
    if topics:
        known.append("当前话题：" + "、".join(topics))

    focus = {
        "title": "标题",
        "body": "正文",
        "topics": "话题",
    }.get(scope)
    if not focus:
        return ""

    lines = [
        "【局部重生成】本次不是重写整篇，只重写**" + focus + "**。",
        "要求：",
        f"1. 只输出「{focus}」的新内容，其余部分不要输出、也不要改写；",
        "2. 新" + focus + "必须与下方已有内容在语气、核心词、叙述上保持一致（它们不会被改动）；",
        "3. 换一种写法，不要复述原文。",
    ]
    if known:
        lines.append("")
        lines.append("【已有内容（保持不变）】")
        lines.extend(known)
    if scope == "topics":
        #: 话题单独说清楚：输出的是**话题名**，不带 `#`
        lines.append("")
        lines.append("输出：`{\"topics\": [\"话题1\", \"话题2\"]}`（话题名不带 `#`）")
    elif scope == "title":
        lines.append("")
        lines.append("输出：`{\"title\": \"新标题\"}`")
    elif scope == "body":
        lines.append("")
        lines.append("输出：`{\"body\": \"新正文\"}`")
    return chr(10).join(lines)


def build_slots(req: CopyRequirements) -> dict[str, str]:
    """约束 → **提示词模板插槽值**（用户口径 2026-10-17）。

    > 「字数，表情以及话题设置均有**默认值**，公共模板中应无需**固定添加固定提示词**，
    >   调用大模型时需**最终拼接**」

    ## 与 `build_instruction` 的分工（别混）

    | | 用在哪 | 有默认值时 |
    |---|---|---|
    | `build_slots()` | 模板里的 `{title_length}` 等**占位符** | **永远给值**（默认值也要填） |
    | `build_instruction()` | 追加到提示词**末尾**的补充段 | 空串（不补充就不加） |

    ⚠️ 为什么这里「默认值也要给」：模板写的是「标题不超过 {title_length}」这种
    **句子骨架**，槽位空着句子就残了。而 `build_instruction` 是可选补充段，
    默认值下再重复一遍才是噪音 —— 两者语义不同，不能合并成一份。
    """
    emoji_text = {
        "on": "标题与正文需带适量表情符号（话题不带）",
        "off": "标题与正文不要出现表情符号",
    }.get(req.emoji, "表情符号按文案调性自行决定")

    return {
        "title_length": f"{req.title_min}–{req.title_max} 字",
        "body_length": f"{req.body_min}–{req.body_max} 字",
        "topic_count": f"{req.topic_min}–{req.topic_max} 个",
        "emoji_rule": emoji_text,
        "copy_supplement": build_instruction(req),
    }


def trim_title(title: str, req: CopyRequirements) -> str:
    """标题超长 ⇒ 裁到上限（不截断半个词，直接按字符裁）。"""
    text = (title or "").strip()
    if len(text) <= req.title_max:
        return text
    return text[: req.title_max].rstrip()


def enforce(generated: Any, req: CopyRequirements) -> Any:
    """模型返回后**兜底**：裁标题、裁话题条数。

    ⚠️ **不重写正文**：正文超长时硬裁会把句子砍断（读起来像 bug），
    且正文超长在发布前还有一道校验会拦。这里只做「无损可做的两件事」：
    标题裁到平台上限、话题条数压到区间上限。
    ⚠️ 底线是**不造假**：不因为「要求 5 个话题、模型只给 2 个」就编三个出来。
    """
    if generated is None:
        return generated
    try:
        if getattr(generated, "title", None):
            generated.title = trim_title(generated.title, req)
        topics = getattr(generated, "topics", None)
        if isinstance(topics, list) and len(topics) > req.topic_max:
            generated.topics = topics[: req.topic_max]
    except Exception:  # noqa: BLE001 - 兜底失败不该废掉整篇
        #: 兜底是「尽力而为」：模型返回体是 dataclass，若将来换成别的形态，
        #: 这里静默跳过比让整篇失败更合理（真正的硬校验在发布前）。
        pass
    return generated
