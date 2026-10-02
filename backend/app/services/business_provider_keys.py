"""业务点固定 key 常量（设计 17 §13.2.2，v2.0）。

## 为什么是常量而不是配置

用户口径（2026-10-01）：「服务商为单独配置，**业务中的使用点映射成固定 key**」。
业务点 key 是**代码与映射表之间的契约**：调用层用本模块的常量查
`llm_business_providers`，页面只让用户**选服务商**、不许改 key（改了等于
让代码里的调用点找不到映射）。同 §4.3.3 的 `FUNCTION_*` 纪律。

## kind 的语义与用途

每个 key 带 `kind`（`text` / `image`），供 M2 的**类型匹配校验**使用：
映射时 `business_key` 的 kind **必须等于**服务商 `llm_gateway_configs.model_type`，
不等则 400 `LLM_BUSINESS_PROVIDER_TYPE_MISMATCH`（§13.2.1a）。

⚠️ kind 刻意只两值：`text` = 打 `POST {base_url}/chat/completions`，
`image` = 打 `POST {base_url}/images/generations`。新能力 = 新增一个枚举值，
**零表结构改动**（§13.2.1a 的可扩展性论证）。

⚠️ 本模块是 API 层与前端下发共用的**唯一真相源** —— 前端业务点清单由
后端端点读这里的常量下发，不在前端再抄一份（DEF-044：同一语义不留两份实现）。
"""

from __future__ import annotations

from typing import Final, NamedTuple

#: 文本类（打 `POST {base_url}/chat/completions`）
MODEL_TYPE_TEXT: Final[str] = "text"
#: 图像类（打 `POST {base_url}/images/generations`）
MODEL_TYPE_IMAGE: Final[str] = "image"

#: 服务商/业务点支持的模型类型全集（§13.2.1a；新增能力只改这里）。
MODEL_TYPES: Final[frozenset[str]] = frozenset({MODEL_TYPE_TEXT, MODEL_TYPE_IMAGE})


class BusinessKey(NamedTuple):
    """一个业务点的固定 key 与其模型类型。

    ⚠️ `label` / `triggers` 是**展示用的元数据**（2026-10-01 用户口径「加触发源」）：
    - `label` = 中文名（页面「业务点」列）
    - `triggers` = **触发源**，即这个业务点在界面上由哪些业务动作触发
      （页面路径链，如 `商品列表-商品详情-生成商品详情图`）。

    两者**只读**、不参与类型校验、不影响映射解析 —— 用户改不了 key，
    也改不了触发源（它们都是代码里的调用契约的镜像）。空元组 = 该业务点
    当前只由 API 直接调用、界面无入口（如实留空，**不编入口**）。
    """

    key: str
    kind: str
    label: str = ""
    triggers: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# 文本类（kind = `text`）：5 个
# --------------------------------------------------------------------------- #

BUSINESS_KEY_GENERATE_NOTE = BusinessKey(
    "generate_note",
    MODEL_TYPE_TEXT,
    "生成笔记",
    ("笔记工坊-草稿-生成草稿", "商品列表-商品详情-生成笔记"),
)
BUSINESS_KEY_REWRITE_NOTE = BusinessKey(
    "rewrite_note",
    MODEL_TYPE_TEXT,
    "改写笔记",
    ("笔记工坊-草稿-改写", "自动运营"),
)
BUSINESS_KEY_GENERATE_TITLE = BusinessKey(
    "generate_title",
    MODEL_TYPE_TEXT,
    "生成标题",
    ("笔记工坊-草稿-生成标题",),
)
BUSINESS_KEY_GENERATE_TAGS = BusinessKey("generate_tags", MODEL_TYPE_TEXT, "生成标签")
BUSINESS_KEY_POLISH_TEXT = BusinessKey("polish_text", MODEL_TYPE_TEXT, "文本润色")

# --------------------------------------------------------------------------- #
# 图像类（kind = `image`）：4 个
# --------------------------------------------------------------------------- #

BUSINESS_KEY_GENERATE_COVER = BusinessKey("generate_cover", MODEL_TYPE_IMAGE, "生成封面")
BUSINESS_KEY_GENERATE_IMAGE = BusinessKey(
    "generate_image",
    MODEL_TYPE_IMAGE,
    "生成配图",
    ("笔记工坊-图片-生成配图", "笔记工坊-草稿-生成配图"),
)
BUSINESS_KEY_DESCRIBE_IMAGE = BusinessKey(
    "describe_image",
    MODEL_TYPE_IMAGE,
    "图片理解",
    ("笔记工坊-图片-图片理解",),
)
#: 2026-10-01 用户新增（「商品列表-商品详情-生成商品详情图」）——
#: 商品详情图与「配图」不是一回事：配图服务笔记正文，详情图服务**商品**
#: （电商详情页长图），提示词与尺寸口径都不同 ⇒ 独立业务点，可分别挂服务商。
BUSINESS_KEY_GENERATE_PRODUCT_DETAIL_IMAGE = BusinessKey(
    "generate_product_detail_image",
    MODEL_TYPE_IMAGE,
    "生成商品详情图",
    ("商品列表-商品详情-生成商品详情图",),
)

#: 全部业务点（顺序即前端「模型配置页」的业务点清单顺序）。
#:
#: ⚠️ 加业务点**只改这里**：新增一个 `BusinessKey` 常量并加入本元组，
#: 下发端点、类型校验、前端清单自动跟进。
BUSINESS_KEYS: Final[tuple[BusinessKey, ...]] = (
    BUSINESS_KEY_GENERATE_NOTE,
    BUSINESS_KEY_REWRITE_NOTE,
    BUSINESS_KEY_GENERATE_TITLE,
    BUSINESS_KEY_GENERATE_TAGS,
    BUSINESS_KEY_POLISH_TEXT,
    BUSINESS_KEY_GENERATE_COVER,
    BUSINESS_KEY_GENERATE_IMAGE,
    BUSINESS_KEY_DESCRIBE_IMAGE,
    BUSINESS_KEY_GENERATE_PRODUCT_DETAIL_IMAGE,
)

#: key → BusinessKey 反查表（映射解析与校验用；O(1)，不在调用点线性扫描）。
_BUSINESS_KEY_BY_KEY: Final[dict[str, BusinessKey]] = {item.key: item for item in BUSINESS_KEYS}


def get_business_key(key: str) -> BusinessKey | None:
    """按 key 取业务点定义；不是已登记的 key ⇒ `None`。

    ⚠️ 调用层**必须用常量**而非字面量调映射解析；本函数只服务
    API 层（前端下发来的 key 要验是否合法）与迁移的存量兼容路径。
    """
    return _BUSINESS_KEY_BY_KEY.get(key)


def is_valid_business_key(key: str) -> bool:
    """key 是否已登记（API 层校验下发值用）。"""
    return key in _BUSINESS_KEY_BY_KEY
