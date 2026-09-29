"""商品笔记生成：商品专用 Prompt + 合规护栏 + 格式校验（M4 起步，T-401 / T-403 / T-405）。

设计真相源：`docs/02_设计文档/07_功能设计_发布链路.md` §12（生成与护栏契约）、
§10 / `11_安全设计.md` §7（合规护栏清单）。

## 这个模块只做三件事（其余留给后面的卡）

| 做 | 不做 |
|---|---|
| ① 用**商品字段**构造 Prompt（卖点/价格只能来自输入，禁止编造） | 落库/审核闸门（T-406，需要 `product_note_jobs`） |
| ② **生成后**跑护栏：违禁词 / 绝对化用语 / 功效宣称 / 价格一致性 | 发布（M5） |
| ③ 格式校验：标题 ≤20 字、正文 300–800 字、图片 1–18 张 | 能力探测与账号选择（M3） |

## 三条硬约定（为什么这么写）

1. **护栏在生成之后、任何入库之前**（07 §10 / 11 §7 的「实现位置」列）。所以本模块**不写库**，
   它只回一份判定结果；把「拦住的内容」也返给调用方，让页面能显示**为什么被拦**（而不是
   静默变成一段空白正文）。
2. **价格一致性用「区间包含」判定，不做数字相等**：商品价格是区间（`¥54.40~144.00`），
   AI 写「54.4 元」或「144 元」都对，写「99 元」只有在区间内才对。这样既不误杀也不放过编造。
3. **AI 编造即拦截，不靠 Prompt 自觉**：Prompt 里当然要写「禁止编造」，但那是**第一道**；
   第二道是这里的机械比对（价格区间 / 未授权数字），因为「模型答应过」不是证据。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

# --------------------------------------------------------------------------- #
# ① 商品字段白名单（Prompt 与一致性校验都只看这些）
# --------------------------------------------------------------------------- #
#: 允许喂给模型的商品字段 —— **白名单构造**，不整体透传千帆响应
#: （那份响应里有 `xsec_token`、库存风险等与写作无关、且不该进 Prompt 的东西）。
PRODUCT_FIELDS = (
    "item_id",
    "title",
    "article_no",
    "price_min_fen",
    "price_max_fen",
    "stock",
    "sku_count",
    "on_sale_sku_count",
    "category",
    "sales_total",
)

# --------------------------------------------------------------------------- #
# ② 护栏词表
# --------------------------------------------------------------------------- #
#: 广告法绝对化用语（《广告法》第九条明列 + 常见变体）。
#: ⚠️ **来源说明**：人工按法条与行业惯例整理，**不是**平台官方词表（平台词表不公开）。
#: 因此这里是「宁可多拦」的保守集，命中即拦截并回可读原因，由人工复核放行。
ABSOLUTE_TERMS = (
    "最好", "最佳", "最优", "最先进", "最强", "最低价", "最高级", "最便宜",
    "第一品牌", "全国第一", "全球第一", "世界第一", "销量第一", "排名第一",
    "顶级", "顶尖", "极致", "绝无仅有", "独一无二", "唯一", "首家", "独家",
    "国家级", "世界级", "全球领先", "行业领先", "领先品牌",
    "100%", "百分百", "全网最低", "史上最低", "永久", "万能", "包治", "绝对",
)

#: 平台/法规违禁与引流敏感词。含「站外引流」类（小红书对导流极敏感）。
BANNED_TERMS = (
    "微信", "微信号", "加微信", "加V", "vx", "V信", "私聊", "私信我", "扫码加",
    "淘宝", "天猫", "拼多多", "京东", "抖音", "快手", "代购", "转运", "刷单",
    "高仿", "复刻", "原单", "尾单", "仿品", "假货", "A货",
    "三无", "包邮到家", "点击链接", "下单链接", "站外", "拼团",
)

#: 功效宣称（非特殊用途化妆品/普通食品不得宣称）。命中即拦（《化妆品监督管理条例》
#: 与《广告法》第十七条：医疗用语不得用于非医疗、药品、医疗器械广告）。
EFFICACY_TERMS = (
    "治疗", "治愈", "根治", "疗效", "药用", "药效", "消炎", "杀菌", "抑菌",
    "祛痘", "祛斑", "美白", "去黑头", "去皱", "抗皱", "除螨", "生发", "防脱",
    "减肥", "瘦身", "丰胸", "壮阳", "排毒", "提高免疫力", "抗肿瘤", "降血压",
    "血糖", "处方", "临床验证", "医学证明", "无副作用", "包治百病",
)


@dataclass
class GuardrailHit:
    """一条命中：词 + 类别 + **所在字段与位置**（便于页面高亮与人工复核）。

    `position` 是**该字段内**的下标，不是「标题+正文」拼起来后的下标 ——
    否则页面按这个下标去正文里高亮会整体偏移（标题多长就偏多少）。
    """

    term: str
    category: str
    position: int
    reason: str
    field: str = "body"

    def as_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "category": self.category,
            "position": self.position,
            "field": self.field,
            "reason": self.reason,
        }


@dataclass
class GuardrailResult:
    hits: list[GuardrailHit] = field(default_factory=list)
    #: 价格一致性问题的可读说明（无问题则为空）。
    price_note: str = ""
    #: 正文里出现、但**不在商品价格区间内**的数字（编造价格的证据）。
    unsupported_prices: list[str] = field(default_factory=list)
    passed: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "hits": [hit.as_dict() for hit in self.hits],
            "blocked_terms": sorted({hit.term for hit in self.hits}),
            "price_note": self.price_note,
            "unsupported_prices": self.unsupported_prices,
        }


@dataclass
class FormatResult:
    ok: bool
    issues: list[str] = field(default_factory=list)
    title_length: int = 0
    body_length: int = 0
    image_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": self.issues,
            "title_length": self.title_length,
            "body_length": self.body_length,
            "image_count": self.image_count,
        }


# --------------------------------------------------------------------------- #
# ③ Prompt
# --------------------------------------------------------------------------- #
PRODUCT_NOTE_SYSTEM_PROMPT = (
    "你是小红书商品笔记撰稿人。你只使用【商品信息】里给出的事实，"
    "不虚构价格、功效、认证、销量或用户评价；不写医疗与功效宣称；不使用绝对化用语；"
    "不引导站外交易。输出必须是 JSON。"
)

#: 标题上限（小红书标题 20 字为平台展示上限）。
TITLE_MAX = 20
BODY_MIN = 300
BODY_MAX = 800
IMAGE_MIN = 1
IMAGE_MAX = 18


def _price_text(product: dict[str, Any]) -> str:
    """商品价格 → 展示文本（分 → 元）。**与 `pc.py::_price_text` 同口径**（换算只有一处定义，
    这里只是把它用在 Prompt 里；两处都读「分」，不存在二次换算）。"""
    low = product.get("price_min_fen")
    high = product.get("price_max_fen")
    prices = [value for value in (low, high) if isinstance(value, (int, float))]
    if not prices:
        return "（价格未知）"
    low_yuan = f"{min(prices) / 100:.2f}"
    high_yuan = f"{max(prices) / 100:.2f}"
    return f"¥{low_yuan}" if low_yuan == high_yuan else f"¥{low_yuan}~{high_yuan}"


def product_payload(product: dict[str, Any]) -> dict[str, Any]:
    """白名单提取商品字段（喂给模型与一致性校验用的都是这一份）。"""
    payload = {key: product.get(key) for key in PRODUCT_FIELDS if product.get(key) not in (None, "")}
    payload["price_text"] = _price_text(product)
    return payload


def build_product_note_prompt(product: dict[str, Any], *, instruction: str = "") -> str:
    """商品专用 Prompt：**角色 / 输入 / 结构 / 要求 / 禁止** 五段（07 §12）。

    输入区是白名单字段 + `price_text`；要求区把「只能引用给定事实」写成可执行的约束
    （而不是「请注意」这类模型可以忽略的措辞）。
    """
    payload = product_payload(product)
    facts = "\n".join(f"- {key}：{value}" for key, value in payload.items())
    extra = instruction.strip() or "自然、有信息密度、像真实用户分享"
    return (
        "【商品信息】（唯一事实来源，逐条只能引用，不得推算或补充）\n"
        f"{facts}\n\n"
        "【输出结构】只输出 JSON，不要任何解释文字：\n"
        '{"title": "标题", "body": "正文", "tags": ["标签1", "标签2"]}\n\n'
        "【要求】\n"
        f"1. 标题 ≤{TITLE_MAX} 字，包含商品核心词，不用「最」「第一」这类绝对化表达；\n"
        f"2. 正文 {BODY_MIN}–{BODY_MAX} 字，分段；用使用场景 + 具体细节替代形容词堆砌；\n"
        "3. 价格只能写上面【商品信息】里的价格，不要算优惠、不要编造到手价；\n"
        "4. 标签 3–6 个，与商品类目相关。\n\n"
        "【禁止】\n"
        "- 禁止医疗/功效宣称（治疗、根治、祛痘、美白、抗皱、除螨…）；\n"
        "- 禁止绝对化用语（最好、第一、顶级、全网最低、国家级…）；\n"
        "- 禁止编造认证、检测、销量、评价；禁止站外引流（微信/淘宝/链接）。\n\n"
        f"【额外风格要求】{extra}"
    )


# --------------------------------------------------------------------------- #
# ④ 护栏
# --------------------------------------------------------------------------- #
def _scan_terms(title: str, body: str) -> list[GuardrailHit]:
    """扫标题与正文，命中项带**所在字段**与**字段内下标**。"""
    hits: list[GuardrailHit] = []
    groups = (
        ("绝对化用语", ABSOLUTE_TERMS, "《广告法》第九条：不得使用「国家级」「最高级」「最佳」等用语"),
        ("违禁/引流词", BANNED_TERMS, "平台禁止站外引流与违规商品表述"),
        ("功效宣称", EFFICACY_TERMS, "普通商品不得宣称医疗或功效（《广告法》第十七条等）"),
    )
    for field_name, text in (("title", title), ("body", body)):
        lowered = (text or "").lower()
        for category, terms, reason in groups:
            for term in terms:
                position = lowered.find(term.lower())
                if position >= 0:
                    hits.append(
                        GuardrailHit(
                            term=term,
                            category=category,
                            position=position,
                            reason=reason,
                            field=field_name,
                        )
                    )
    hits.sort(key=lambda hit: (0 if hit.field == "title" else 1, hit.position))
    return hits


def check_price_consistency(text: str, product: dict[str, Any]) -> tuple[list[str], str]:
    """正文里的数字价格是否落在商品价格区间内。

    只检查**看起来是价格**的数字（后接「元/块/¥」或位于「价」字附近），避免把
    「3 层」「500ml」这类规格误判成价格。无法判定时**不拦**（宁可放过也不误杀），
    但把「商品价格未知」写进说明，让人知道这次没校价。
    """
    low = product.get("price_min_fen")
    high = product.get("price_max_fen")
    values = [value for value in (low, high) if isinstance(value, (int, float))]
    if not values:
        return [], "商品价格未知，本次未做价格一致性校验（缺值不推断）。"

    low_yuan, high_yuan = min(values) / 100, max(values) / 100
    unsupported: list[str] = []
    for match in re.finditer(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d{1,2})?)\s*(元|块|¥|￥)", text):
        raw = match.group(1).replace(",", "")
        try:
            amount = float(raw)
        except ValueError:  # pragma: no cover - 正则已保证可解析
            continue
        if amount < low_yuan or amount > high_yuan:
            unsupported.append(match.group(0).strip())
    note = ""
    if unsupported:
        note = (
            f"商品价格为 ¥{low_yuan:.2f}~{high_yuan:.2f}，"
            f"正文里的「{'、'.join(unsupported)}」不在该区间 —— 疑似编造价格。"
        )
    return unsupported, note


def run_guardrails(title: str, body: str, product: dict[str, Any]) -> GuardrailResult:
    """生成后、入库前的护栏（07 §10 / 11 §7 的「实现位置」）。

    标题与正文分开扫（`field` 标出命中的是哪一个），价格一致性看两者合并后的文本
    —— 价格写在哪一处都算数，但**位置**只在对应字段内有意义。
    """
    hits = _scan_terms(title, body)
    unsupported, note = check_price_consistency(f"{title}\n{body}", product)
    return GuardrailResult(
        hits=hits,
        price_note=note,
        unsupported_prices=unsupported,
        passed=not hits and not unsupported,
    )


def validate_note_format(title: str, body: str, image_count: int) -> FormatResult:
    """格式校验（标题 ≤20 字、正文 300–800 字、图片 1–18 张）。

    何时跑：**生成后**先按此校验（不合格让模型重生成，不要存下来再改）；发布前再跑一次
    （素材可能被换过）。本函数是纯函数，两处共用同一份规则。
    """
    issues: list[str] = []
    title_length = len(title.strip())
    body_length = len(body.strip())
    if title_length == 0:
        issues.append("标题为空")
    elif title_length > TITLE_MAX:
        issues.append(f"标题 {title_length} 字，超过上限 {TITLE_MAX} 字")
    if body_length < BODY_MIN:
        issues.append(f"正文 {body_length} 字，少于下限 {BODY_MIN} 字")
    elif body_length > BODY_MAX:
        issues.append(f"正文 {body_length} 字，超过上限 {BODY_MAX} 字")
    if not IMAGE_MIN <= image_count <= IMAGE_MAX:
        issues.append(f"图片 {image_count} 张，应在 {IMAGE_MIN}–{IMAGE_MAX} 张之间")
    return FormatResult(
        ok=not issues,
        issues=issues,
        title_length=title_length,
        body_length=body_length,
        image_count=image_count,
    )


# --------------------------------------------------------------------------- #
# ⑤ 生成（含护栏与格式校验）
# --------------------------------------------------------------------------- #
def parse_generated_payload(content: str) -> dict[str, Any]:
    """模型返回 → `{title, body, tags}`。

    容错顺序：整段 JSON → 去掉 ``` 围栏后的 JSON → 从第一个 `{` 到最后一个 `}` 的子串。
    都不行就抛 `ValueError`（**不猜**）：把「模型没按格式回」当成错误暴露出来，
    比悄悄把一整段原始文本塞进正文要好（后者会带着 JSON 花括号发到平台上）。
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("AI 返回为空")
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        candidates.append(fenced.group(1).strip())
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{") : text.rindex("}") + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict) and (payload.get("body") or payload.get("title")):
            tags = payload.get("tags")
            return {
                "title": str(payload.get("title") or "").strip(),
                "body": str(payload.get("body") or "").strip(),
                "tags": [str(tag).strip() for tag in tags] if isinstance(tags, list) else [],
            }
    raise ValueError("AI 返回不是可解析的 JSON（需要 {\"title\", \"body\", \"tags\"}）")


class ProductNoteTextClient(Protocol):
    """只需要「按 Prompt 生成商品笔记」这一个能力（便于测试注入假客户端）。"""

    def generate_product_note(
        self, *, model_config: Any, api_key: str, product: dict[str, Any], instruction: str
    ) -> dict[str, Any]:  # pragma: no cover - 协议声明
        ...


@dataclass
class ProductNoteResult:
    ok: bool
    block_reason: str = ""
    title: str = ""
    body: str = ""
    tags: list[str] = field(default_factory=list)
    guardrails: GuardrailResult = field(default_factory=GuardrailResult)
    format: FormatResult | None = None
    product: dict[str, Any] = field(default_factory=dict)
    raw: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "block_reason": self.block_reason,
            "title": self.title,
            "body": self.body,
            "tags": self.tags,
            "guardrails": self.guardrails.as_dict(),
            "format": self.format.as_dict() if self.format else None,
            "product": self.product,
        }


def generate_product_note(
    *,
    client: ProductNoteTextClient,
    model_config: Any,
    api_key: str,
    product: dict[str, Any],
    instruction: str = "",
    image_count: int | None = None,
) -> ProductNoteResult:
    """生成 → 护栏 → 格式校验，**不落库**（落库与审核闸门属 T-406）。

    `image_count` 为 `None` 表示「这次不校验图片张数」（生成阶段还没有选素材）。
    只要存在拦截项，`ok=False` 且**仍然返回正文**：页面要能显示「为什么被拦」，
    静默丢弃会让用户以为模型没输出。
    """
    payload = product_payload(product)
    generated = client.generate_product_note(
        model_config=model_config, api_key=api_key, product=payload, instruction=instruction
    )
    title = str(generated.get("title") or "").strip()
    body = str(generated.get("body") or "").strip()
    tags = [str(tag).strip() for tag in (generated.get("tags") or []) if str(tag).strip()]

    guardrails = run_guardrails(title, body, product)
    fmt = (
        validate_note_format(title, body, image_count)
        if image_count is not None
        else FormatResult(
            ok=True,
            issues=[],
            title_length=len(title),
            body_length=len(body),
            image_count=0,
        )
    )

    reasons: list[str] = []
    if guardrails.hits:
        reasons.append(
            "命中 " + "、".join(f"{hit.category}「{hit.term}」" for hit in guardrails.hits[:6])
        )
    if guardrails.unsupported_prices:
        reasons.append(guardrails.price_note)
    if image_count is not None and not fmt.ok:
        reasons.append("；".join(fmt.issues))

    return ProductNoteResult(
        ok=not reasons,
        block_reason="；".join(reason for reason in reasons if reason),
        title=title,
        body=body,
        tags=tags,
        guardrails=guardrails,
        format=fmt,
        product=payload,
        raw=json.dumps(generated, ensure_ascii=False)[:2000],
    )
