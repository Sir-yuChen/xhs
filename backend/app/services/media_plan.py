"""媒体组合 `media_plan`（设计 41 §11.2 / §14，M-C1，v2.26）。

## 为什么（**推翻「六选一」**）

现状实测（2026-10-16）：`media_source` 是 `String(32)` **单值列**，6 个可选值，
后端 `_assets_for_media()` 是 **if/elif 链、命中即 return** ⇒ **天然单来源**。
用户口径（设计 41 §11.2）：*「本地素材、智能生成、商品图、智能生成+本地或/商品图等等组合方式」*
⇒ 当前形态**做不到组合**，必须改。

## 形态（**有序**，因为顺序决定笔记图序）

```
media_plan = [
  {"source": "ai_generate",   "role": "cover",   "count": 1, "template_id": …},
  {"source": "material",      "role": "content", "folder_id": …, "file_ids": [...]},
  {"source": "product_image", "role": "content", "count": 3, "pick": [1,3,5]},
]
```

## 四条定死的规则（判据，不是建议）

| # | 规则 | 为什么 |
|---|---|---|
| R1 | **封面唯一且必须排第 1** | 封面决定点击率；多个封面 = 判据不明。无封面 ⇒ **整篇失败**（不可半成功） |
| R2 | **总张数上限只有一处实现** | 各来源各判一次 = DEF-044 两份实现 |
| R3 | **同一来源可重复出现** | 否则「AI 封面 + AI 内页」用不同模板表达不了 |
| R4 | **视频与图片可共存，视频不占图片位** | 视频是异步的（可能几分钟），失败不回滚图文 |

⚠️ **本模块不重造出图逻辑**：每一段仍调 `media_generation._assets_for_media()` ——
组合层只负责「按顺序拆段 + 汇总张数 + 判封面」。
"""
from __future__ import annotations

from typing import Any, Optional

#: 段的两个角色。`cover` 有且只有一个且必须排首；其余都是 `content`。
ROLE_COVER = "cover"
ROLE_CONTENT = "content"
MEDIA_ROLES = (ROLE_COVER, ROLE_CONTENT)

#: 图片总张数上限（与既有 `MAX_NOTE_IMAGES` 同口径 —— 小红书笔记图上限 18，
#: 但生成链路刻意收窄；⚠️ **唯一定义在 media_generation**，这里 import 不重抄）。
VIDEO_ROLE = "video"

#: 组合总张数上限（图片）。⚠️ 这是**组合层唯一的总闸**（R2）。
#: ⚠️ **已被运行时配置取代**（用户口径 2026-10-16：*「笔记图的总数是固定的…配置到
#: config 表中支持动态调整」*）—— 真值走 `resolve_note_image_limit()`（读
#: `xhs_note_image_total_limit`）。本常量只作**读不到配置时的兜底**，不再直接判超限。
MAX_PLAN_IMAGES = 6
#: **平台硬顶**：官方 `xhs.postNote` 原文「图片可以多张（1-18）」。
#: 配置填再大也不能越过它 —— 越界 ⇒ 平台校验失败（不是「多发几张」那么轻）。
PLATFORM_MAX_NOTE_IMAGES = 18
#: 视频上限（R4：视频不占图片位，单独计数）。
MAX_PLAN_VIDEOS = 1


def resolve_note_image_limit(db: Any = None, user: Any = None) -> int:
    """**笔记图片总数上限**（唯一判据处，设计 41 §17）。

    走配置中心既有的三层取值（`live_int`：运行时配置 → `settings` → 常量兜底），
    再做**平台硬顶夹取**（`min(…, PLATFORM_MAX_NOTE_IMAGES)`）。

    ⚠️ 夹取在这里而不是只在配置写入侧：配置可能被直接改库，而**平台契约必须永远
    成立** —— 读的时候就夹，任何路径都越不过去（`_KEY_BOUNDS` 是第二道，不是唯一一道）。
    """
    try:
        from backend.app.services.settings_service import live_int  # noqa: PLC0415

        limit = live_int(db, int(user.id) if user is not None else None, "xhs_note_image_total_limit", default=MAX_PLAN_IMAGES)
    except Exception:  # noqa: BLE001 - 配置读不到不是致命错（回落常量，不阻断生成）
        limit = MAX_PLAN_IMAGES
    return max(1, min(int(limit), PLATFORM_MAX_NOTE_IMAGES))


class MediaPlanError(ValueError):
    """组合非法（结构错 / 规则违反 / 超限）。`code` 供端点映射结构化 400。"""

    def __init__(self, message: str, *, code: str = "LLM_MEDIA_PLAN_INVALID") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def normalize_plan(raw: Any) -> list[dict[str, Any]]:
    """入参 → 规范化 `media_plan`（**校验 + 补默认**；非法即抛 `MediaPlanError`）。

    接受的两种入参形态：

    1. **组合**（新）：`[{"source": …, "role": …}, …]`（非空列表）
    2. **单来源**（向后兼容的**兼容层**）：`media_source` 字符串 ⇒ 合成一段计划

    ⚠️ 第 2 种是**为兼容既有调用方**（前端还没改、单测在用）而保留的**唯一**兼容点，
    它把「单来源」**降级为一段**（`role=cover`）—— 不是两套实现，是**入参归一化**。
    归一化后**全链路只认 plan**，调用方不得再读 `media_source`。
    """
    if isinstance(raw, str):
        source = raw.strip()
        if not source:
            raise MediaPlanError("媒体来源为空", code="LLM_MEDIA_SOURCE_INVALID")
        return [{"source": source, "role": ROLE_COVER}]
    if not isinstance(raw, list):
        raise MediaPlanError("media_plan 必须是数组", code="LLM_MEDIA_PLAN_INVALID")
    if not raw:
        raise MediaPlanError("media_plan 不能为空", code="LLM_MEDIA_PLAN_EMPTY")

    plan: list[dict[str, Any]] = []
    for index, seg in enumerate(raw):
        if not isinstance(seg, dict):
            raise MediaPlanError(f"第 {index + 1} 段不是对象", code="LLM_MEDIA_PLAN_INVALID")
        source = str(seg.get("source") or "").strip()
        if not source:
            raise MediaPlanError(f"第 {index + 1} 段缺 source", code="LLM_MEDIA_PLAN_INVALID")
        role = str(seg.get("role") or "").strip() or (
            ROLE_COVER if index == 0 else ROLE_CONTENT
        )
        if role not in MEDIA_ROLES:
            raise MediaPlanError(
                f"第 {index + 1} 段 role 非法：{role}（可选：{'/'.join(MEDIA_ROLES)}）",
                code="LLM_MEDIA_PLAN_INVALID",
            )
        item: dict[str, Any] = {"source": source, "role": role}
        # 透传其余参数（count / pick / folder_id / file_ids / template_id / prompt …）——
        # 参数语义归各来源自己（`media_generation` 是唯一判据处），这里**不解读**。
        for key, value in seg.items():
            if key in ("source", "role"):
                continue
            item[key] = value
        plan.append(item)
    return plan


def validate_plan(db_plan: list[dict[str, Any]], *, max_images: Optional[int] = None) -> list[dict[str, Any]]:
    """规则校验（R1–R4）。**非法即抛**，不静默修补。

    ⚠️ **不自动修正**（如「把 cover 挪到首位」）：静默修补 = 用户以为按自己排的顺序出图，
    实际不是（DEF-058 同型：真异常要报出来）。

    `max_images` 缺省 ⇒ 走 `resolve_note_image_limit()`（运行时配置，平台硬顶 18）。
    调用方**可以**显式传入（端点已解析过一次时避免重复读配置）。
    """
    validate_sources(db_plan)

    covers = [seg for seg in db_plan if seg["role"] == ROLE_COVER]
    if len(covers) > 1:
        raise MediaPlanError(
            f"封面只能有一个（实得 {len(covers)} 个）", code="LLM_MEDIA_PLAN_MULTIPLE_COVERS"
        )
    if covers and db_plan[0]["role"] != ROLE_COVER:
        raise MediaPlanError("封面必须排在第一位", code="LLM_MEDIA_PLAN_COVER_NOT_FIRST")

    #: ⚠️ **封面不计入图片总数**（用户口径 2026-10-19：*「封面不算在图片总数中」*）——
    #: 上限说的是「内页可放几张」：总数 6 = 6 张内页 + 1 张封面 = 7 张图。
    #: 前端 `pickCapFor` 用同一口径（两边不同步会出现「前端能选、后端 400」）。
    #: ⚠️ **智能生成的图不预先占位**（用户口径 2026-10-19：*「大模型生成的图不一定全部
    #: 可以用，生成后才可以供用户选择，所以它的生成张数不应该算该片笔记的图片总数，
    #: 应是生成后选择的图片算该片笔记的图片总数」*）。
    #: 生成 6 张 ≠ 笔记放 6 张：它是**备选池**，实际进笔记的是用户挑出来的那几张
    #: （落在该段的 `pick` 里）。按 `count` 算 = 凭一个还没发生的选择扣位，必然误拦。
    def _occupies(seg: dict[str, Any]) -> int:
        if _is_video(seg) or seg["role"] == ROLE_COVER:
            return 0
        if seg.get("source") == "ai_generate":
            pick = seg.get("pick")
            return max(0, len(pick)) if isinstance(pick, list) else 0
        return _count_of(seg)

    images = sum(_occupies(seg) for seg in db_plan)
    videos = sum(_count_of(seg) for seg in db_plan if _is_video(seg))
    limit = max_images if max_images is not None else resolve_note_image_limit()
    if images > limit:
        raise MediaPlanError(
            f"组合图片总数 {images} 超过上限 {limit}",
            code="LLM_MEDIA_PLAN_TOO_MANY_IMAGES",
        )
    if videos > MAX_PLAN_VIDEOS:
        raise MediaPlanError(
            f"组合视频总数 {videos} 超过上限 {MAX_PLAN_VIDEOS}",
            code="LLM_MEDIA_PLAN_TOO_MANY_VIDEOS",
        )
    return db_plan


def validate_sources(db_plan: list[dict[str, Any]]) -> None:
    """每段 source 必须在白名单内（**非法即 400**，不静默回落）。

    ⚠️ 惰性 import `media_generation` 的常量：那份常量是**唯一真相源**，
    这里不重抄一份（漂移即缺陷）。
    """
    from backend.app.models.generation import MEDIA_SOURCES

    for index, seg in enumerate(db_plan):
        if seg["source"] not in MEDIA_SOURCES:
            raise MediaPlanError(
                f"第 {index + 1} 段媒体来源不支持：{seg['source']}"
                f"（可选：{'/'.join(MEDIA_SOURCES)}）",
                code="LLM_MEDIA_SOURCE_INVALID",
            )


def has_cover(db_plan: list[dict[str, Any]]) -> bool:
    """计划里是否有封面段（**生成后校验用**：无封面 ⇒ 整篇失败，R1）。"""
    return any(seg["role"] == ROLE_COVER for seg in db_plan)


def _count_of(seg: dict[str, Any]) -> int:
    """段要出几张（**缺省 1**；视频段恒 1）。

    ⚠️ **`product_image` 段要按 `pick` 算**（用户口径 2026-10-16）—— 用户选了 3 张
    商品图就是 3 张，不是 1 张。少了这一步，总闸会把「3 商品图 + 3 智能生成」
    算成 1+3=4（实际 6），**上限形同虚设**（该拦的没拦）。
    """
    pick = seg.get("pick")
    if isinstance(pick, list) and pick:
        return max(1, len(pick))
    try:
        value = int(seg.get("count") or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, value)


def _is_video(seg: dict[str, Any]) -> bool:
    """是否是视频段（按 source 判 —— 与 `models/generation.VIDEO_MEDIA_SOURCES` 同源）。"""
    from backend.app.models.generation import VIDEO_MEDIA_SOURCES

    return seg["source"] in VIDEO_MEDIA_SOURCES


def plan_form(db_plan: list[dict[str, Any]]) -> str:
    """计划 → **笔记形态**（`image` / `video`）—— 平台侧的唯一判据（设计 41 §15）。

    ⚠️ **判定规则是「有没有视频段」而不是「视频排第几」**：平台契约是
    `image_resources` / `video_resources` **只能二选一**（多传即校验失败），
    所以只要计划里有视频段，这篇笔记就是**视频笔记**，图片段退化为它的**封面**。

    这与 R4（视频与图片可共存、视频不占图片位）**不矛盾**：R4 说的是
    「生成时都能产出」（视频异步失败不回滚图文），本函数说的是
    「发布时按哪种形态提交」—— 同一个计划，生成阶段全出，发布阶段按形态取。

    ⚠️ 惰性 import 形态常量（`models.note` 是唯一定义处，不重抄）。
    """
    from backend.app.models.note import NOTE_FORM_IMAGE, NOTE_FORM_VIDEO

    #: ⚠️ **必须扫全部段，不能只看第 1 段**（2026-10-16 修）—— 原先写
    #: `_is_video(db_plan[0])`，与本文档字符串的承诺**正好相反**：
    #: 典型视频计划是「封面=商品原图 + 视频段」，第 1 段不是视频 ⇒ 判成 `image`
    #: ⇒ 发布时按图文提交、视频被丢弃（**静默丢媒体**，最难查的那种）。
    #: 判据：`any(...)` —— 只要计划里**有一段**是视频，这篇就是视频笔记。
    return (
        NOTE_FORM_VIDEO if any(_is_video(seg) for seg in db_plan) else NOTE_FORM_IMAGE
    )


def plan_summary(db_plan: list[dict[str, Any]]) -> str:
    """计划 → 一行摘要（进队列记录 / 审计，供用户看清「这次是怎么组合的」）。"""
    parts: list[str] = []
    for seg in db_plan:
        role = "封面" if seg["role"] == ROLE_COVER else "内页"
        parts.append(f"{role}:{seg['source']}×{_count_of(seg)}")
    return " + ".join(parts)


def build_plan(
    *,
    media_plan: Any = None,
    media_source: Optional[str] = None,
) -> list[dict[str, Any]]:
    """端点入参 → 已校验的 plan（**组合优先，单值兜底**）。

    ⚠️ **两参数都给** ⇒ **以 `media_plan` 为准**（新形态优先），不报错 ——
    前端灰度期间可能同时带上二者（老字段没删干净），报错会让灰度不可行；
    但**必须以后者为准并留痕**（摘要里能看出用的是哪种）。

    ⚠️ **逐段参数也要校验**（用户口径 2026-10-16：*「后端也需要添加对应逻辑，进行校验」*）
    —— `validate_plan` 只管**跨段规则**（封面唯一 / 总数上限）；「素材选没选」
    「参考图超没超 4 张」是**段内**的事，必须另过 `validate_media_options`。
    ⚠️ 此前该函数**零调用方**（死代码）⇒ 用户选了 5 张参考图会被服务端
    **静默截断**成 4 张，前端限制一被绕开就没人拦（本仓最厌恶的静默失效）。
    """
    raw = media_plan if media_plan else (media_source or None)
    if raw is None:
        # 两者都没给 ⇒ 用默认单来源（与既有默认一致：商品原图）。
        from backend.app.models.generation import MEDIA_SOURCE_PRODUCT_IMAGE

        raw = MEDIA_SOURCE_PRODUCT_IMAGE
    normalized = normalize_plan(raw)
    #: ⚠️ **顺序有意义**：先 `validate_plan`（含 source 白名单 —— 最基础的那类错），
    #: 再逐段参数。反过来的话「来源写错」会被报成「参考图超限」之类，误导用户
    #: （他真正要改的是来源名）。判据 = **错误信息必须指向真正的错因**。
    checked = validate_plan(normalized)
    _validate_segment_options(checked)
    return checked


def _validate_segment_options(db_plan: list[dict[str, Any]]) -> None:
    """逐段过 `media_generation.validate_media_options`（段内参数门禁）。

    ⚠️ **在端点同步段调用**（非 worker）：这些是**确定性的用户输入错误**，
    必须入队前给结构化 400（入队后失败 = 用户要去队列页翻原因）。
    抛的是 `MediaGenerationError`，这里**归一成 `MediaPlanError`** ——
    端点只 catch 后者（两种异常混着抛会让 500 漏出去）。
    """
    from backend.app.services.media_generation import (
        MediaGenerationError,
        validate_media_options,
    )

    for seg in db_plan:
        source = str(seg.get("source") or "")
        options = {k: v for k, v in seg.items() if k not in ("source", "role")}
        try:
            validate_media_options(source, options)
        except MediaGenerationError as error:
            raise MediaPlanError(error.message, code=error.code) from error
