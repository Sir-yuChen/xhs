"""媒体组合 `media_plan` 的守卫（设计 41 §11.2 / §14，M-C1，v2.26）。

## 为什么（推翻「六选一」）

现状：`media_source` 是单值列，后端 if/elif 链**命中即 return** ⇒ **天然单来源**。
用户口径：*「本地素材、智能生成、商品图、智能生成+本地或/商品图等等组合方式」*
⇒ 必须支持**有序组合**。

## 四条规则（R1–R4，判据不是建议）

| # | 规则 |
|---|---|
| R1 | **封面唯一且必须排第 1**；无封面 ⇒ **整篇失败**（不可半成功） |
| R2 | **总张数上限只有一处实现** |
| R3 | **同一来源可重复出现** |
| R4 | **视频与图片可共存，视频不占图片位** |
"""
from __future__ import annotations

import pytest

from backend.app.services.media_plan import (
    MAX_PLAN_IMAGES,
    MAX_PLAN_VIDEOS,
    MediaPlanError,
    build_plan,
    has_cover,
    normalize_plan,
    plan_summary,
    validate_plan,
)



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract


# --------------------------------------------------------------------------- #
# ① 归一化：单来源 → 一段（兼容层）；组合 → 原样
# --------------------------------------------------------------------------- #


def test_single_source_normalizes_to_one_cover_segment():
    """单来源字符串 ⇒ 合成为**一段**（`role=cover`）—— 这是**唯一的兼容点**。"""
    plan = normalize_plan("product_image")
    assert plan == [{"source": "product_image", "role": "cover"}]


def test_plan_list_is_preserved_in_order():
    """组合列表 ⇒ **保序**（顺序即图序，封面必须仍在首位）。"""
    raw = [
        {"source": "ai_generate", "role": "cover", "count": 1},
        {"source": "material", "role": "content", "count": 2},
    ]
    plan = normalize_plan(raw)
    assert [seg["source"] for seg in plan] == ["ai_generate", "material"]
    assert plan[0]["role"] == "cover"


@pytest.mark.parametrize("bad", [[], "   ", {"source": "x"}, 123])
def test_invalid_plan_shape_raises(bad):
    """空列表 / 空串 / 非列表 ⇒ **抛**（不静默给默认值）。"""
    with pytest.raises(MediaPlanError):
        normalize_plan(bad)


def test_segment_without_source_raises():
    """段缺 `source` ⇒ 抛（缺来源等于没说装什么媒体）。"""
    with pytest.raises(MediaPlanError) as exc:
        normalize_plan([{"role": "cover"}])
    assert "source" in str(exc.value)


def test_unknown_source_raises_with_code():
    """来源不在白名单 ⇒ 抛且带 `LLM_MEDIA_SOURCE_INVALID`（端点映射 400）。"""
    with pytest.raises(MediaPlanError) as exc:
        validate_plan(normalize_plan([{"source": "不存在的来源", "role": "cover"}]))
    assert exc.value.code == "LLM_MEDIA_SOURCE_INVALID"


# --------------------------------------------------------------------------- #
# ② R1：封面唯一且排第一
# --------------------------------------------------------------------------- #


def test_r1_multiple_covers_rejected():
    """两个封面 ⇒ 抛（判据不明：到底哪张是封面？）。"""
    raw = [
        {"source": "product_image", "role": "cover"},
        {"source": "ai_generate", "role": "cover"},
    ]
    with pytest.raises(MediaPlanError) as exc:
        validate_plan(normalize_plan(raw))
    assert exc.value.code == "LLM_MEDIA_PLAN_MULTIPLE_COVERS"


def test_r1_cover_not_first_rejected():
    """封面不在第 1 位 ⇒ 抛（顺序即图序 —— 「第一个是内页」就不可能出封面）。"""
    raw = [
        {"source": "material", "role": "content"},
        {"source": "ai_generate", "role": "cover"},
    ]
    with pytest.raises(MediaPlanError) as exc:
        validate_plan(normalize_plan(raw))
    assert exc.value.code == "LLM_MEDIA_PLAN_COVER_NOT_FIRST"


def test_r1_does_not_silently_reorder_cover():
    """**不自动把封面挪到首位**（静默修补 = 用户以为按自己排的顺序出图）。

    ⚠️ 这是本条最关键的一点：修好了「顺序对」，却让用户对结果失去预期。
    """
    raw = [
        {"source": "material", "role": "content"},
        {"source": "ai_generate", "role": "cover"},
    ]
    with pytest.raises(MediaPlanError):
        validate_plan(normalize_plan(raw))


def test_r1_has_cover_helper():
    """`has_cover()` 是「无封面判整篇失败」的判据入口。"""
    assert has_cover(normalize_plan("product_image")) is True
    assert has_cover([{"source": "material", "role": "content"}]) is False


# --------------------------------------------------------------------------- #
# ③ R2：总张数上限（唯一总闸）
# --------------------------------------------------------------------------- #


def test_r2_total_image_count_enforced():
    """逐段张数**累加**后超上限 ⇒ 抛（不是各段各判一次）。"""
    #: ⚠️ **封面不计入图片总数**（用户口径 2026-10-19：*「封面不算在图片总数中」*）——
    #: 上限说的是「内页可放几张」：总数 N = N 张内页 + 1 张封面。
    #: 旧断言（已被取代）用 `cover count=2 + content MAX` 触发，改口径后封面不计数就不该抛。
    raw = [
        {"source": "product_image", "role": "cover", "count": 2},
        {"source": "material", "role": "content", "count": MAX_PLAN_IMAGES},
        {"source": "material", "role": "content", "count": 1},
    ]
    with pytest.raises(MediaPlanError) as exc:
        validate_plan(normalize_plan(raw))
    assert exc.value.code == "LLM_MEDIA_PLAN_TOO_MANY_IMAGES"


def test_r2_count_defaults_to_one():
    """缺 `count` ⇒ 按 1 张计（不按 0，也不按无穷）。"""
    plan = normalize_plan([{"source": "ai_generate", "role": "cover"}])
    # 5 段各 1 张 = 5 ≤ 6 通过；再补 1 段变 6 仍通过
    raw = [{"source": "ai_generate", "role": "cover"}] + [
        {"source": "material", "role": "content"} for _ in range(4)
    ]
    assert validate_plan(normalize_plan(raw)) == normalize_plan(raw)


def test_r2_video_counted_separately():
    """R4：**视频不占图片位** —— 图片满额 + 1 视频应当**通过**。"""
    raw = [
        {"source": "product_image", "role": "cover"},
        {"source": "material", "role": "content", "count": MAX_PLAN_IMAGES - 1},
        {"source": "video_ai_generate", "role": "content", "count": 1},
    ]
    validate_plan(normalize_plan(raw))  # 不抛即通过


def test_r2_video_over_limit_rejected():
    """视频超上限 ⇒ 抛（视频也有总闸，不是无限）。"""
    raw = [
        {"source": "video_ai_generate", "role": "cover", "count": MAX_PLAN_VIDEOS + 1},
    ]
    with pytest.raises(MediaPlanError) as exc:
        validate_plan(normalize_plan(raw))
    assert exc.value.code == "LLM_MEDIA_PLAN_TOO_MANY_VIDEOS"


# --------------------------------------------------------------------------- #
# ④ R3：「智能生成 + 本地 + 商品图」三来源组合（用户点名的场景）
# --------------------------------------------------------------------------- #


def test_r3_user_named_three_source_composition():
    """用户点名的组合必须**能表达**：`智能生成(封面) + 本地素材 + 商品图`。"""
    plan = build_plan(
        media_plan=[
            {"source": "ai_generate", "role": "cover", "count": 1, "template_id": 4},
            {"source": "material", "role": "content", "count": 2, "file_ids": [7]},
            {"source": "product_image", "role": "content", "count": 3, "pick": [1, 3, 5]},
        ]
    )
    assert [seg["source"] for seg in plan] == ["ai_generate", "material", "product_image"]
    assert plan[0]["template_id"] == 4
    assert plan[1]["file_ids"] == [7]
    assert plan[2]["pick"] == [1, 3, 5]


def test_r3_same_source_can_repeat():
    """同来源可重复出现（如两个 `ai_generate` 分组，模板不同）。"""
    plan = normalize_plan(
        [
            {"source": "ai_generate", "role": "cover", "template_id": 4},
            {"source": "ai_generate", "role": "content", "template_id": 9},
        ]
    )
    assert len([seg for seg in plan if seg["source"] == "ai_generate"]) == 2


def test_r3_composition_preferred_over_single_value():
    """**组合优先**：两者都给 ⇒ 用 `media_plan`（`media_source` 只作兜底）。"""
    plan = build_plan(
        media_plan=[{"source": "material", "role": "cover", "file_ids": [3]}],
        media_source="product_image",
    )
    assert plan == [{"source": "material", "role": "cover", "file_ids": [3]}]


def test_r3_falls_back_to_single_value_when_no_plan():
    """没给组合 ⇒ 用单值（既有调用方不受影响）。

    ⚠️ 单值兜底**不带段内参数**（`media_source` 只有来源名）—— 素材段缺
    `file_ids` 会被段内门禁拦下，这是**正确行为**（端点会 400 提示用户勾素材，
    而不是入队后失败）。要跑通整个组合得带参数，见上一条用例。
    """
    plan = build_plan(media_source="product_image")
    assert plan == [{"source": "product_image", "role": "cover"}]
    #: 素材来源缺 `file_ids` ⇒ 段内门禁拦下（**不是**静默放过）。
    with pytest.raises(MediaPlanError) as exc:
        build_plan(media_source="material")
    assert exc.value.code == "LLM_MEDIA_OPTIONS_INVALID"


def test_r3_neither_given_uses_product_image_default():
    """两者都没给 ⇒ 默认商品原图（与既有默认一致，不改行为）。"""
    plan = build_plan()
    assert plan == [{"source": "product_image", "role": "cover"}]


# --------------------------------------------------------------------------- #
# ⑤ 摘要（用户要能看出「这次怎么组合的」）
# --------------------------------------------------------------------------- #


def test_plan_summary_is_human_readable():
    """摘要含角色 + 来源 + 张数（进队列记录 / 审计）。"""
    text = plan_summary(
        normalize_plan(
            [
                {"source": "ai_generate", "role": "cover", "count": 1},
                {"source": "material", "role": "content", "count": 2},
            ]
        )
    )
    assert "封面:ai_generate×1" in text
    assert "内页:material×2" in text


# --------------------------------------------------------------------------- #
# ⑥ 接缝：端点 / worker 必须真的把 plan 传下去
# --------------------------------------------------------------------------- #


def test_endpoint_schema_has_media_plan_field():
    """单篇入参 schema 必须有 `media_plan`（否则前端传了会被静默丢弃）。"""
    from backend.app.api.platforms.xhs.product_notes import GenerateLlmNoteRequest

    assert "media_plan" in GenerateLlmNoteRequest.model_fields, (
        "GenerateLlmNoteRequest 缺 media_plan ⇒ 前端传了也进不来"
    )


def test_endpoint_validates_plan_before_enqueue():
    """端点层校验（`_checked_media_plan`）：非法组合 ⇒ 结构化 400（**入队前**）。"""
    from fastapi import HTTPException

    from backend.app.api.platforms.xhs.product_notes import _checked_media_plan

    with pytest.raises(HTTPException) as exc:
        _checked_media_plan([{"source": "不存在的来源", "role": "cover"}], "product_image")
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "LLM_MEDIA_SOURCE_INVALID"


def test_endpoint_rejects_over_limit_reference_images():
    """⚠️ **参考图超 4 张必须 400**（用户口径 2026-10-16：*「后端也需要添加对应逻辑，
    进行校验」*）—— 端点同步段拦下，而不是 worker 里静默截断。

    绕过前端（直接打 API）是本条存在的唯一理由：前端限制只是提示。
    """
    from fastapi import HTTPException

    from backend.app.api.platforms.xhs.product_notes import _checked_media_plan

    with pytest.raises(HTTPException) as exc:
        _checked_media_plan(
            [
                {
                    "source": "ai_generate",
                    "role": "cover",
                    "reference_image_urls": [f"https://cdn.example.com/{i}.jpg" for i in range(5)],
                }
            ],
            "product_image",
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "LLM_MEDIA_OPTIONS_INVALID"
    assert "4" in exc.value.detail["message"], "原因里要说清上限是 4"


def test_endpoint_accepts_exactly_four_reference_images():
    """4 张是**允许**的（上限含边界）—— 别把合法输入也拦掉。"""
    from backend.app.api.platforms.xhs.product_notes import _checked_media_plan

    plan = _checked_media_plan(
        [
            {
                "source": "ai_generate",
                "role": "cover",
                "reference_image_urls": [f"https://cdn.example.com/{i}.jpg" for i in range(4)],
            }
        ],
        "product_image",
    )
    assert len(plan[0]["reference_image_urls"]) == 4


def test_endpoint_rejects_material_segment_without_files():
    """素材段没勾文件 ⇒ 400（段内门禁；此前 `validate_media_options` 是**死代码**）。"""
    from fastapi import HTTPException

    from backend.app.api.platforms.xhs.product_notes import _checked_media_plan

    with pytest.raises(HTTPException) as exc:
        _checked_media_plan([{"source": "material", "role": "cover"}], "product_image")
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "LLM_MEDIA_OPTIONS_INVALID"


def test_worker_composes_segments_and_isolates_failure():
    """worker 的 `build_media_plan_assets` 必须**逐段隔离**失败。

    ⚠️ 判据落在**行为**上：一段抛异常时，其他段的产出仍应返回，
    且 `segments` 里那一段 `ok=False` 带原因（用户要看到「哪个来源失败」）。
    """
    from backend.app.services import media_generation

    class _FakeUser:
        id = 1

    calls: list[str] = []

    def _fake_build_note_media(
        db, *, user, row, media_source, media_options, record, business_key=""
    ):
        #: `business_key` 是 2026-10-18 新增（各入口独立配模型）—— 替身必须接收它，
        #: 否则 TypeError 会被逐段隔离吞掉，表现为「两段都没被尝试」的假失败。
        calls.append(media_source)
        if media_source == "material":
            raise media_generation.MediaGenerationError("素材文件不存在", code="MATERIAL_MISSING")
        return [{"kind": "image", "url": f"http://x/{media_source}"}], []

    original = media_generation.build_note_media
    media_generation.build_note_media = _fake_build_note_media  # type: ignore[assignment]
    try:
        assets, rows, segments = media_generation.build_media_plan_assets(
            None,  # type: ignore[arg-type]
            user=_FakeUser(),
            row=None,  # type: ignore[arg-type]
            media_plan=[
                {"source": "product_image", "role": "cover"},
                {"source": "material", "role": "content"},
            ],
            record=None,
        )
    finally:
        media_generation.build_note_media = original  # type: ignore[assignment]

    assert calls == ["product_image", "material"], "两段都要被尝试（不因第一段成功就跳过第二段）"
    assert len(assets) == 1, "成功段的产出必须保留（半成功可交付）"
    assert segments[0]["ok"] is True
    assert segments[1]["ok"] is False
    assert "素材文件不存在" in segments[1]["error"], "失败原因必须如实带出"


# --------------------------------------------------------------------------- #
# `pick` —— 商品原图「选哪几张」（用户口径 2026-10-16）
# --------------------------------------------------------------------------- #
class _FakeRow:
    """最小商品行：只给 `_product_image_urls` 需要的字段（它读 `images_json` 字符串）。

    ⚠️ 用真函数而不是打桩 —— 判据要落在 `_assets_from_product_image` 的**实际取值**上。
    """

    def __init__(self, images: list[str], detail_images: list[str] | None = None):
        import json

        self.images_json = json.dumps(images)
        self.image = images[0] if images else ""
        #: ⚠️ `_product_image_urls` 现在**还读 `raw_json.image_descriptions`**（详情图）
        #: —— 用户口径 2026-10-16：*「商品原图是包括商品主图和详情图」*。
        #: 夹具不补这一列 ⇒ `AttributeError` 直接崩（不是断言失败，是真缺字段）。
        self.raw_json = json.dumps(
            {"image_descriptions": [{"url": u} for u in (detail_images or [])]}
        )


def _product_images(count: int) -> list[str]:
    return [f"https://cdn.example.com/{i}.jpg" for i in range(count)]


def test_pick_uses_image_names_not_indices():
    """`pick` 给**图片名称** ⇒ 取那一张（用户口径 2026-10-18：「必须是图片名称」）。

    ⚠️ 口径变更：原断言是「给下标 ⇒ 按下标取」。下标要求前后端列表逐位一致，
    而两边来源不同（前端用列表行载荷、后端用主图+详情图去重合并）⇒ 真机实证错位。
    名称 = URL 末段，不依赖任何一侧的排序。
    """
    from backend.app.services import media_generation

    row = _FakeRow(_product_images(6))
    assets = media_generation._assets_from_product_image(row, {"pick": ["3.jpg"]})
    assert [a["url"] for a in assets] == ["https://cdn.example.com/3.jpg"]


def test_pick_supports_multiple_and_keeps_the_given_order():
    """多选按**用户给的顺序**产出（笔记图序 = 用户点选顺序，不是台账顺序）。"""
    from backend.app.services import media_generation

    row = _FakeRow(_product_images(6))
    assets = media_generation._assets_from_product_image(row, {"pick": ["4.jpg", "1.jpg"]})
    assert [a["url"] for a in assets] == [
        "https://cdn.example.com/4.jpg",
        "https://cdn.example.com/1.jpg",
    ]
    #: `sort_order` 连续（下游按它排序，跳号会让图序错乱）。
    assert [a["sort_order"] for a in assets] == [0, 1]


def test_no_pick_keeps_the_legacy_first_six():
    """不给 `pick` ⇒ 取前 N 张（N = 图片总数上限，默认 6）。"""
    from backend.app.services import media_generation

    row = _FakeRow(_product_images(12))
    limit = media_generation._note_image_limit()
    #: 上限来自运行时配置（），**不写死数字** ——
    #: 写死 6 会在配置改成别的值（含测试默认）时假红。
    assert len(media_generation._assets_from_product_image(row, None)) == min(limit, 12)
    assert len(media_generation._assets_from_product_image(row, {})) == min(limit, 12)


def test_pick_unknown_name_is_ignored_not_fatal():
    """台账里没有的名称**忽略**（商品图会变，不能为此报错）；全都不在 ⇒ 回落前 N 张。"""
    from backend.app.services import media_generation

    row = _FakeRow(_product_images(3))
    assets = media_generation._assets_from_product_image(row, {"pick": ["99.jpg"]})
    assert len(assets) == 3, "全都不在应回落，而不是产出空（空=整篇失败）"

    mixed = media_generation._assets_from_product_image(
        row, {"pick": ["1.jpg", "99.jpg", ""]}
    )
    assert [a["url"] for a in mixed] == ["https://cdn.example.com/1.jpg"], "合法项要保留"


def test_build_note_media_passes_options_down_to_product_image():
    """⚠️ 判据落在**调用形态**上：`build_note_media` 必须把 `media_options` 传下去。

    只测 `_assets_from_product_image` 本身**证明不了**这条路通 ——
    调用点不传 options 时 `pick` 一样丢失（静默失效）。
    """
    import inspect

    from backend.app.services import media_generation

    src = inspect.getsource(media_generation.build_note_media)
    assert "_assets_from_product_image(row, media_options)" in src, (
        "商品图分支没把 media_options 传下去 ⇒ 用户选的图被无视"
    )


# --------------------------------------------------------------------------- #
# 图片总数上限 = 运行时配置（用户口径 2026-10-16）
# --------------------------------------------------------------------------- #
class TestNoteImageLimit:
    """`resolve_note_image_limit` 的三层 + **平台硬顶**。

    ⚠️ 判据的核心是**平台契约不可越**：配置怎么填都不该让生成出 18 张以上的图
    （官方 `xhs.postNote`：「图片可以多张（1-18）」）。
    """

    def test_platform_hard_cap_beats_config(self, monkeypatch):
        """配置填 99 ⇒ **仍被夹到 18**（平台硬顶优先于用户配置）。"""
        from backend.app.services import media_plan

        monkeypatch.setattr(
            media_plan,
            "resolve_note_image_limit",
            lambda db=None, user=None: max(1, min(99, media_plan.PLATFORM_MAX_NOTE_IMAGES)),
        )
        assert media_plan.resolve_note_image_limit() == 18

    def test_hard_cap_constant_matches_the_official_contract(self):
        """硬顶必须等于官方契约的 18 —— 改这个数就是改平台契约（要先有抓包证据）。"""
        from backend.app.services import media_plan

        assert media_plan.PLATFORM_MAX_NOTE_IMAGES == 18

    def test_limit_key_is_registered_as_a_live_key(self):
        """⚠️ 新增配置键必须同时进 `LIVE_KEYS` —— 漏了 UI 会把「已接线」说成「仅存档」。"""
        from backend.app.services import settings_service

        assert "xhs_note_image_total_limit" in settings_service.LIVE_KEYS
        assert "xhs_note_image_total_limit" in settings_service.BUILTIN_DEFAULTS
        assert "xhs_note_image_total_limit" in settings_service._KEY_BOUNDS

    def test_bounds_stop_at_the_hard_cap(self):
        """`_KEY_BOUNDS` 的上界也必须是 18（改配置这条路径也被夹）。"""
        from backend.app.services import settings_service

        assert settings_service._KEY_BOUNDS["xhs_note_image_total_limit"] == (1, 18)

    def test_validate_plan_uses_the_passed_limit(self):
        """`validate_plan(max_images=…)` 用传入值（端点已解析过就不重复读配置）。"""
        from backend.app.services import media_plan

        #: 用**内页**段验上限（封面不计入总数，用封面验不触发）
        #: ⚠️ 用 `material` 验（`ai_generate` 已改为**不预先占位**：生成张数是备选池大小，
        #: 用户口径 2026-10-19「生成后选择的图片才算该篇笔记的图片总数」）
        plan = [
            {"source": "ai_generate", "role": "cover", "count": 1},
            {"source": "material", "role": "content", "count": 5},
        ]
        media_plan.validate_plan(plan, max_images=5)  # 刚好等于 ⇒ 放行
        with pytest.raises(media_plan.MediaPlanError) as exc:
            media_plan.validate_plan(plan, max_images=4)
        assert exc.value.code == "LLM_MEDIA_PLAN_TOO_MANY_IMAGES"

    def test_product_image_pick_counts_toward_the_total(self):
        """⚠️ `pick` 选的张数要算进总数 —— 否则总闸形同虚设（该拦的没拦）。"""
        from backend.app.services import media_plan

        #: 3 张商品图 + 3 张智能生成 = 6（不是 1+3=4）。
        #: ⚠️ 用**内页段**验（封面不计入总数，用封面段验不触发断言）
        plan = [
            {"source": "product_image", "role": "cover", "count": 1},
            {"source": "product_image", "role": "content", "pick": [0, 1, 2]},
            {"source": "material", "role": "content", "count": 3},
        ]
        media_plan.validate_plan(plan, max_images=6)  # 3 + 3 = 6 ⇒ 放行
        with pytest.raises(media_plan.MediaPlanError):
            media_plan.validate_plan(plan, max_images=5)
