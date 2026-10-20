"""业务点固定 key 常量（设计 17 §13.2.2，v2.0）。

## 为什么是常量而不是配置

用户口径（2026-10-01）：「服务商为单独配置，**业务中的使用点映射成固定 key**」。
业务点 key 是**代码与映射表之间的契约**：调用层用本模块的常量查
`llm_business_providers`，页面只让用户**选服务商**、不许改 key（改了等于
让代码里的调用点找不到映射）。同 §4.3.3 的 `FUNCTION_*` 纪律。

## 核心口径（用户 2026-10-18，三条同时生效）

> 「一个业务点支持用户配置**多个类型**模型，**每个类型只能配置一个**…该业务点必须
>   配置一个文本模型，一个图片模型，一个视频模型，用户可以手动添加不同类型的模型，
>   但是**相同类型的模型不允许重复添加**，同一个类型的模型支持切换」
>
> 「模型配置**以前端入口为父对象**，比如当前的 LLM 生成笔记功能应配置三种类型的模型」
>
> 「所有业务功能**不在共用配置**，改为**各个功能单独配置**」

⇒ **一个入口 = 一组 key（每类型一个），入口之间不共用 key**：

| 入口 | key |
|---|---|
| LLM 生成笔记 | `llm_note_text` / `llm_note_image` / `llm_note_video` |
| 笔记工坊 · 草稿 | `workshop_draft_text` / `workshop_rewrite_text` / `workshop_title_text` / `workshop_image` |
| 笔记工坊 · 图片 | `workshop_image_gen` / `workshop_image_understand` |
| 自动运营 | `auto_ops_text` |
| 商品详情 · AI 润色 | `product_title_polish` / `product_selling_point` |
| 商品详情 · 生成详情图 | `product_detail_image` |

⚠️ **旧的「一个 key + `needs` 声明多类型」写法已整条退场**：那正是「共用配置」的
来源（`generate_image` 同时被两个入口用，改一处影响两处）。现在类型是 key 自己的
属性（`kind`），一个入口要几类 = 该入口下有几个 key。

## kind 的语义与用途

映射时 `business_key` 的 kind **必须**与该映射所选**模型行**的
`llm_gateway_models.model_type` 相容（`_type_satisfies`），不等则 400
`LLM_BUSINESS_PROVIDER_TYPE_MISMATCH`。
⚠️ **v2.7 起判据在模型层** —— 服务商层不再有 `model_type` 列。

⚠️ 本模块是 API 层与前端下发共用的**唯一真相源** —— 前端业务点清单由
后端端点读这里的常量下发，不在前端再抄一份（DEF-044：同一语义不留两份实现）。
"""

from __future__ import annotations

from typing import Final, NamedTuple

#: 文本类（打 `POST {base_url}/chat/completions`）
MODEL_TYPE_TEXT: Final[str] = "text"
#: 图像类（打 `POST {base_url}/images/generations`）
MODEL_TYPE_IMAGE: Final[str] = "image"
#: 多态类（v2.9 新增）：既能对话又能出图/理解图片，如 gpt-4o、qwen-vl 系列。
#: ⚠️ **调用层路由**：统一走 `POST {base_url}/chat/completions`，
#: 与 `text` 同路；`image` 才是唯一走 `/images/generations` 的类型。
MODEL_TYPE_MULTIMODAL: Final[str] = "multimodal"
#: 视频类（v2.9 新增）：文生视频 / 图生视频，如 sora、kling、veo 系列。
MODEL_TYPE_VIDEO: Final[str] = "video"

#: 能力类型全集（§13.2.1a；新增能力只改这里）。
#: ⚠️ 本集合是**模型行** `model_type` 的枚举源（服务商层已无该列）。
#: 中文映射见 `MODEL_TYPE_LABELS`，**不要在别处再写一份**（DEF-044）。
MODEL_TYPES: Final[frozenset[str]] = frozenset(
    {MODEL_TYPE_TEXT, MODEL_TYPE_IMAGE, MODEL_TYPE_MULTIMODAL, MODEL_TYPE_VIDEO}
)

#: 类型 → 中文展示名（**唯一映射点**）。
#: ⚠️ 用户口径里的「图片」对应 `image`（能出图的模型）；「多态」对应 `multimodal`；
#: 「视频」「文本」顾名思义。前端类型下拉、表格类型列、模型配置页筛选**全部读这一份**
#: （随 `/business-provider-keys` 的 `model_types` 下发）。
MODEL_TYPE_LABELS: Final[dict[str, str]] = {
    MODEL_TYPE_TEXT: "文本",
    MODEL_TYPE_IMAGE: "图片",
    MODEL_TYPE_MULTIMODAL: "多态",
    MODEL_TYPE_VIDEO: "视频",
}

#: 走「对话」入口的类型（`POST {base_url}/chat/completions`）。
#: ⚠️ 调用层路由**唯一判据** —— 凡是需要决定「打 chat 还是打 images」的地方读这个集合，
#: 不要散写 `== MODEL_TYPE_TEXT`（扩枚举后那样写会把 multimodal 漏掉）。
CHAT_ENTRY_MODEL_TYPES: Final[frozenset[str]] = frozenset(
    {MODEL_TYPE_TEXT, MODEL_TYPE_MULTIMODAL}
)


class BusinessKey(NamedTuple):
    """一个业务点：固定 key + 它要的模型类型 + 所属前端入口。

    ⚠️ `label` / `triggers` / `entry` / `role` 是**展示用的元数据**：
    - `label` = 中文名（页面子行的能力名）
    - `triggers` = 触发源（**只读诊断**，不再上屏 —— 用户 2026-10-18 要求移除该列）
    - `entry` = **所属前端入口**（页面的父对象；空串 = 仅 API 调用）
    - `role` = 该 key 在入口里扮演的角色（缺省 = `kind` 的中文名）

    它们都**不参与类型校验、不影响映射解析** —— 用户改不了 key，也改不了入口。
    """

    key: str
    kind: str
    label: str = ""
    triggers: tuple[str, ...] = ()
    #: **所属前端入口**（模型配置页的父行）。
    #: 一个入口下可以有多个 key（每类型一个），它们一起满足该入口的模型需求。
    #: 空串 = 该 key 没有对应前端入口（仅 API 调用），单独成行、不参与分组。
    entry: str = ""
    #: 该 key 在这个入口里扮演的角色（展示用；缺省 = `kind` 的中文名）。
    #: 例：「LLM 生成笔记」入口下 `llm_note_image` 的角色是「图片」。
    role: str = ""

    @property
    def role_label(self) -> str:
        return self.role or MODEL_TYPE_LABELS.get(self.kind, self.kind)


# --------------------------------------------------------------------------- #
# 入口 ①：LLM 生成笔记（商品列表操作列「LLM 生成」）
#
# 这个入口一次要三类模型：写文案（文本）+ 配图（图片）+ 出视频（视频）。
# --------------------------------------------------------------------------- #

#: 正文 / 标题 / 话题的文本生成（该入口的主模型）。
BUSINESS_KEY_LLM_NOTE_TEXT = BusinessKey(
    "llm_note_text",
    MODEL_TYPE_TEXT,
    #: 子行展示的是**用途**（用户口径 2026-10-18：*「子行应展示为用处，
    #: 比如生成标题、正文、话题」*）—— 写清它到底产出什么，不是「文本」这种类型名。
    "生成标题、正文、话题",
    ("商品列表-LLM生成-生成文案",),
    entry="LLM 生成笔记",
)
#: 智能生成配图（媒体来源选「智能生成」时用）。
BUSINESS_KEY_LLM_NOTE_IMAGE = BusinessKey(
    "llm_note_image",
    MODEL_TYPE_IMAGE,
    "生成配图",
    ("商品列表-LLM生成-智能生成配图",),
    entry="LLM 生成笔记",
)
#: 智能生成视频（媒体来源选「智能生成视频」时用）。
BUSINESS_KEY_LLM_NOTE_VIDEO = BusinessKey(
    "llm_note_video",
    MODEL_TYPE_VIDEO,
    "生成视频",
    ("商品列表-LLM生成-智能生成视频",),
    entry="LLM 生成笔记",
)

# --------------------------------------------------------------------------- #
# 入口 ②：笔记工坊 · 草稿
# --------------------------------------------------------------------------- #

BUSINESS_KEY_WORKSHOP_DRAFT_TEXT = BusinessKey(
    "workshop_draft_text",
    MODEL_TYPE_TEXT,
    "生成草稿",
    ("笔记工坊-草稿-生成草稿",),
    entry="笔记工坊 · 草稿",
)
BUSINESS_KEY_WORKSHOP_REWRITE_TEXT = BusinessKey(
    "workshop_rewrite_text",
    MODEL_TYPE_TEXT,
    "改写草稿",
    ("笔记工坊-草稿-改写",),
    entry="笔记工坊 · 草稿",
)
BUSINESS_KEY_WORKSHOP_TITLE_TEXT = BusinessKey(
    "workshop_title_text",
    MODEL_TYPE_TEXT,
    "生成标题",
    ("笔记工坊-草稿-生成标题",),
    entry="笔记工坊 · 草稿",
)
BUSINESS_KEY_WORKSHOP_IMAGE = BusinessKey(
    "workshop_image",
    MODEL_TYPE_IMAGE,
    "生成配图",
    ("笔记工坊-草稿-生成配图",),
    entry="笔记工坊 · 草稿",
)

# --------------------------------------------------------------------------- #
# 入口 ③：笔记工坊 · 图片
# --------------------------------------------------------------------------- #

BUSINESS_KEY_WORKSHOP_IMAGE_GEN = BusinessKey(
    "workshop_image_gen",
    MODEL_TYPE_IMAGE,
    "生成配图",
    ("笔记工坊-图片-生成配图",),
    entry="笔记工坊 · 图片",
)
BUSINESS_KEY_WORKSHOP_IMAGE_UNDERSTAND = BusinessKey(
    "workshop_image_understand",
    MODEL_TYPE_IMAGE,
    "图片理解",
    ("笔记工坊-图片-图片理解",),
    entry="笔记工坊 · 图片",
)

# --------------------------------------------------------------------------- #
# 入口 ④：自动运营
# --------------------------------------------------------------------------- #

BUSINESS_KEY_AUTO_OPS_TEXT = BusinessKey(
    "auto_ops_text",
    MODEL_TYPE_TEXT,
    "自动改写",
    ("自动运营",),
    entry="自动运营",
)

# --------------------------------------------------------------------------- #
# 入口 ⑤：商品详情 · AI 润色
# --------------------------------------------------------------------------- #

#: ⚠️ 与 `polish_text`（笔记文本润色）/ `workshop_title_text`（笔记标题）
#: 是**不同的业务点**：那两个服务**笔记**，本组服务**商品** —— 提示词、长度约束、
#: 平台口径全不同（商品标题有平台字数上限、不能含广告法违禁词；笔记标题没有）。
BUSINESS_KEY_PRODUCT_TITLE_POLISH = BusinessKey(
    "product_title_polish",
    MODEL_TYPE_TEXT,
    "标题润色",
    ("商品列表-商品详情-AI润色-标题",),
    entry="商品详情 · AI 润色",
)
BUSINESS_KEY_PRODUCT_SELLING_POINT = BusinessKey(
    "product_selling_point",
    MODEL_TYPE_TEXT,
    "卖点生成",
    ("商品列表-商品详情-AI润色-卖点",),
    entry="商品详情 · AI 润色",
)

# --------------------------------------------------------------------------- #
# 入口 ⑥：商品详情 · 生成详情图
# --------------------------------------------------------------------------- #

#: 商品详情图与「配图」不是一回事：配图服务笔记正文，详情图服务**商品**
#: （电商详情页长图），提示词与尺寸口径都不同 ⇒ 独立业务点。
BUSINESS_KEY_PRODUCT_DETAIL_IMAGE = BusinessKey(
    "product_detail_image",
    MODEL_TYPE_IMAGE,
    "生成详情图",
    ("商品列表-商品详情-生成商品详情图",),
    entry="商品详情 · 生成详情图",
)

# --------------------------------------------------------------------------- #
# 无前端入口（仅 API 调用）—— 如实留空 entry，单独成行，**不编入口**
# --------------------------------------------------------------------------- #

#: 草稿工坊「生成标签」按钮（`generateTagOptions` → `POST /ai/generate-tags`）。
#: ⚠️ 实测**有前端调用**（`drafts-workshop-panel.tsx`）⇒ 归「笔记工坊 · 草稿」入口，
#: 不再是「仅 API 调用」。
BUSINESS_KEY_GENERATE_TAGS = BusinessKey(
    "generate_tags",
    MODEL_TYPE_TEXT,
    "生成标签",
    ("笔记工坊-草稿-生成标签",),
    entry="笔记工坊 · 草稿",
)
#: ⚠️ **已移除**（2026-10-18 用户口径「没有入口是否被真实使用，未使用直接移除」）：
#: * `polish_text`（文本润色）/ `generate_cover`（生成封面）—— 端点还在
#:   （`POST /ai/polish-text` / `POST /ai/images/generate-cover`），但**前端零调用**、
#:   后端也没有任何业务链路调它（实测 grep 全仓无调用方）。
#:   留着只会让用户在模型配置页配一个永远不生效的模型。
#: * 端点本身暂不删（可能有外部调用方 / 后续功能要接），但**不再占业务点**：
#:   它们若被调用会抛 `BusinessProviderNotConfiguredError`（业务点未登记）——
#:   这是**如实报错**，不是静默回落到别的模型。


#: 全部业务点（顺序即模型配置页父行的顺序）。
#:
#: ⚠️ 加业务点**只改这里**：新增一个 `BusinessKey` 常量并加入本元组，
#: 下发端点、类型校验、前端清单自动跟进。
BUSINESS_KEYS: Final[tuple[BusinessKey, ...]] = (
    # ① LLM 生成笔记（三类）
    BUSINESS_KEY_LLM_NOTE_TEXT,
    BUSINESS_KEY_LLM_NOTE_IMAGE,
    BUSINESS_KEY_LLM_NOTE_VIDEO,
    # ② 笔记工坊 · 草稿
    BUSINESS_KEY_WORKSHOP_DRAFT_TEXT,
    BUSINESS_KEY_WORKSHOP_REWRITE_TEXT,
    BUSINESS_KEY_WORKSHOP_TITLE_TEXT,
    BUSINESS_KEY_GENERATE_TAGS,
    BUSINESS_KEY_WORKSHOP_IMAGE,
    # ③ 笔记工坊 · 图片
    BUSINESS_KEY_WORKSHOP_IMAGE_GEN,
    BUSINESS_KEY_WORKSHOP_IMAGE_UNDERSTAND,
    # ④ 自动运营
    BUSINESS_KEY_AUTO_OPS_TEXT,
    # ⑤ 商品详情 · AI 润色
    BUSINESS_KEY_PRODUCT_TITLE_POLISH,
    BUSINESS_KEY_PRODUCT_SELLING_POINT,
    # ⑥ 商品详情 · 生成详情图
    BUSINESS_KEY_PRODUCT_DETAIL_IMAGE,
)

#: key → BusinessKey 反查表（映射解析与校验用；O(1)，不在调用点线性扫描）。
_BUSINESS_KEY_BY_KEY: Final[dict[str, BusinessKey]] = {item.key: item for item in BUSINESS_KEYS}

#: **旧 key → 新 key**（2026-10-18 按入口拆分）。
#:
#: 为什么需要：拆之前 `(platform, business_key)` 存着用户的配置，key 名如
#: `generate_note`；拆之后变成 `llm_note_text` 等。**存量配置不能丢** ——
#: 用户配好的模型在升级后必须继续生效（否则每个入口都变「未配置」）。
#:
#: ⚠️ 被多个入口共用的旧 key（`generate_note` / `generate_image` / `rewrite_note`）
#: 会**复制成多行**（每个新 key 一行，初始值相同）。这是「拆分」的必然代价，
#: 也正合用户口径 —— 拆开之后本来就可以各配各的，初始值相同不改变「可以不同」。
LEGACY_KEY_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "generate_note": ("llm_note_text", "workshop_draft_text"),
    "generate_image": ("llm_note_image", "workshop_image", "workshop_image_gen"),
    "generate_video": ("llm_note_video",),
    "rewrite_note": ("workshop_rewrite_text", "auto_ops_text"),
    "generate_title": ("workshop_title_text",),
    "describe_image": ("workshop_image_understand",),
    "polish_product_title": ("product_title_polish",),
    "generate_selling_point": ("product_selling_point",),
    "generate_product_detail_image": ("product_detail_image",),
    #: `generate_tags` 同名沿用（只是从「无入口」归入「笔记工坊 · 草稿」）。
    "generate_tags": ("generate_tags",),
}


def get_business_key(key: str) -> BusinessKey | None:
    """按 key 取业务点定义；不是已登记的 key ⇒ `None`。

    ⚠️ 调用层**必须用常量**而非字面量调映射解析；本函数只服务
    API 层（前端下发来的 key 要验是否合法）与迁移的存量兼容路径。
    """
    return _BUSINESS_KEY_BY_KEY.get((key or "").strip())


def is_valid_business_key(key: str) -> bool:
    """key 是否已登记（API 层校验下发值用）。"""
    return (key or "").strip() in _BUSINESS_KEY_BY_KEY


# --------------------------------------------------------------------------- #
# **前端入口聚合**（用户口径 2026-10-18：*「模型配置以前端入口为父对象」*）
# --------------------------------------------------------------------------- #

def entry_kinds() -> dict[str, tuple[str, ...]]:
    """入口名 → 该入口需要的**能力类型**（按 `BUSINESS_KEYS` 顺序去重）。

    父行的「共需 N 类模型」与「哪几类」都读它 —— 前端**不许自己数**：
    将来加一个类型只改后端，页面若自己数就会静默少一行，缺的模型没人配、
    调用时才 500。
    """
    out: dict[str, list[str]] = {}
    for spec in BUSINESS_KEYS:
        if not spec.entry:
            continue
        bucket = out.setdefault(spec.entry, [])
        if spec.kind not in bucket:
            bucket.append(spec.kind)
    return {name: tuple(kinds) for name, kinds in out.items()}
