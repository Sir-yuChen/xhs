"""出图提示词**公共约束库**的测试（设计 17 §13.3.7.7，v2.21）。

用户口径：「（出图提示词）**是否可以作为公共提示词**」。

背景：此前项目里有 **4 处各自拼出图提示词**（详情图 / 笔记配图 / 笔记视频 / 文本业务点），
措辞互不一致，且**防漂移约束只加在详情图那一处**。本文件守住「公共」这件事：
一处定义、按用途组合、所有调用点同源。
"""

from __future__ import annotations

import pytest

from backend.app.services import image_prompt


# --------------------------------------------------------------------------- #
# ① 公共约束库本身
# --------------------------------------------------------------------------- #
def test_safety_lines_are_shared_across_uses():
    """防漂移三条**全用途共享**（这是「公共」的核心含义）。

    ⚠️ 变异守护：把某条从共享集合里摘掉（改成只在 DETAIL 里出现）⇒ 本用例必红。
    """
    for use in (image_prompt.USE_DETAIL, image_prompt.USE_COVER, image_prompt.USE_VIDEO):
        lines = image_prompt.requirement_lines(use)
        for safety in image_prompt.GENERATION_SAFETY_LINES:
            assert safety in lines, f"{use} 用途缺少公共防漂移约束：{safety}"


def test_uses_differ_in_layout_only():
    """不同用途**只在构图口径上不同**，公共约束一字不差。

    这是「可以做成公共」的**边界证明**：能共享的共享，不能共享的（3:4 静帧 vs 9:16 动态）
    用用途分档 —— 而不是为了共享把视频也写成「竖版 3:4 静帧」。
    """
    detail = set(image_prompt.requirement_lines(image_prompt.USE_DETAIL))
    video = set(image_prompt.requirement_lines(image_prompt.USE_VIDEO))
    # 差异只在构图行（详情图有 3:4，视频有 9:16 + 动态）
    assert any("3:4" in line for line in detail)
    assert any("9:16" in line for line in video)
    # 公共三条两边都在（共享，不是各写一份）
    for safety in image_prompt.GENERATION_SAFETY_LINES:
        assert safety in detail and safety in video


def test_cover_use_reserves_title_space():
    """封面/配图用途**要留出叠标题的位置**（与详情图的关键差异）。

    ⚠️ 详情图是直接进详情页的成品图，没有叠字需求；封面要叠标题 ——
    若两者共用一个「画面干净」的口径，封面图会被裁掉标题空间。
    """
    cover = image_prompt.requirement_lines(image_prompt.USE_COVER)
    assert any("留白" in line and "标题" in line for line in cover), (
        "封面用途缺少「留出标题位置」约束（会被后期叠字裁掉）"
    )
    detail = image_prompt.requirement_lines(image_prompt.USE_DETAIL)
    assert not any("留白" in line for line in detail), "详情图不该要求留白"


def test_unknown_use_falls_back_not_empty():
    """未知用途 ⇒ 回落详情图口径（**不静默返回空**）。

    ⚠️ 返回空的后果：出图 prompt 里「出图要求」整段消失，且**不报错** ——
    典型的静默退化（DEF-048 同型）。
    """
    assert image_prompt.layout_lines("no_such_use") == image_prompt.layout_lines(
        image_prompt.USE_DETAIL
    )
    assert image_prompt.requirement_lines("no_such_use"), "未知用途返回了空约束"


def test_inline_form_strips_bullets():
    """`requirement_inline` 去掉行首 `- `（单行形式里用 `；` 分隔，留着破折号像列点残留）。"""
    inline = image_prompt.requirement_inline(image_prompt.USE_DETAIL)
    assert "- " not in inline
    assert "；" in inline
    # 内容不丢：每条都还在
    for line in image_prompt.requirement_lines(image_prompt.USE_DETAIL):
        assert line.removeprefix("- ") in inline


def test_block_form_has_header():
    """`requirement_block` 带「出图要求：」标题（供分段拼装用）。"""
    block = image_prompt.requirement_block(image_prompt.USE_DETAIL)
    assert block.startswith("出图要求：")
    assert block.count("\n") == len(image_prompt.requirement_lines(image_prompt.USE_DETAIL))


# --------------------------------------------------------------------------- #
# ② 各调用点**同源**（这是「公共」的落地证明）
# --------------------------------------------------------------------------- #
def test_detail_module_reuses_public_library():
    """详情图模块不再自己写一份约束（改为复用公共库）。

    ⚠️ 这是本轮重构的核心断言：改公共库 ⇒ 详情图跟着变，不会漏。
    """
    from backend.app.services import detail_image_prompt

    assert detail_image_prompt.IMAGE_REQUIREMENT_LINES == image_prompt.requirement_lines(
        image_prompt.USE_DETAIL
    )
    # 详细图的两条调用路径同源：喂文本模型的 brief 与回落的本地拼接
    assert image_prompt.requirement_block(image_prompt.USE_DETAIL) in (
        detail_image_prompt.build_item_brief({}, [], "")
    )


def test_media_generation_prompts_include_safety_lines():
    """笔记配图 / 视频的默认提示词也带**防漂移约束**（此前只有详情图有）。

    ⚠️ 变异守护：把 `_default_image_prompt` 改回自己写一句「竖版构图，突出卖点」
    ⇒ 本用例必红（那条约束本来在 3 处各写一遍，且都没有防漂移）。
    """
    from backend.app.services.media_generation import (
        _default_image_prompt,
        _default_video_prompt,
    )

    class _Row:
        item_title = "白橘运动包"
        title = "白橘运动包"

    image_prompt_text = _default_image_prompt(_Row())
    for safety in image_prompt.GENERATION_SAFETY_LINES:
        assert safety.removeprefix("- ") in image_prompt_text, "配图提示词缺防漂移约束"

    video_prompt_text = _default_video_prompt(_Row())
    for safety in image_prompt.GENERATION_SAFETY_LINES:
        assert safety.removeprefix("- ") in video_prompt_text, "视频提示词缺防漂移约束"
    # 视频用 9:16 口径（不是照抄图片的 3:4）
    assert "9:16" in video_prompt_text


def test_library_has_no_business_titles():
    """公共库**不含具体商品信息**（商品名等由调用点注入）。

    ⚠️ 若把某个商品名写进公共库，所有商品都会带上它 —— 这是最容易出的错。
    """
    text = "\n".join(
        (*image_prompt.GENERATION_SAFETY_LINES, *image_prompt.PLATFORM_SAFE_LINES, *image_prompt.QUALITY_LINES)
    )
    for marker in ("白橘", "运动包", "收纳盒"):
        assert marker not in text, f"公共库出现了商品信息：{marker}"


# --------------------------------------------------------------------------- #
# ③ 端到端：详情图端点的提示词确实带上约束
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("use", [image_prompt.USE_DETAIL, image_prompt.USE_COVER, image_prompt.USE_VIDEO])
def test_every_use_has_shape_and_safety(use):
    """三种用途都**非空、有构图、有防漂移**（防止将来加用途时漏配）。"""
    lines = image_prompt.requirement_lines(use)
    assert len(lines) >= len(image_prompt.GENERATION_SAFETY_LINES) + 1
    assert any("构图" in line for line in lines), f"{use} 缺少构图行"


def test_requirement_dedupe_is_shared_between_image_and_video_links():
    """判重是**唯一实现**，图片与视频链路共用（防两份实现分叉，DEF-044）。

    背景：v2.26 落地视频模板时，图片链路与视频链路**各自写了一份**归一化判重
    （行首前缀 / 行尾标点 / 指代词），两份措辞不同、迟早分叉。已收敛到
    `image_prompt.missing_requirement_lines`，本用例钉住「不要再分叉」。
    """
    import pathlib as _pathlib

    from backend.app.services import image_prompt

    # ① 公共函数存在且语义正确：已覆盖的行不返回
    rendered = image_prompt.requirement_block(image_prompt.USE_VIDEO).replace("- ", "")
    missing = image_prompt.missing_requirement_lines(image_prompt.USE_VIDEO, rendered)
    assert missing == (), f"已覆盖的约束仍被判为缺失：{missing}"

    # ② 未覆盖 ⇒ 全部返回（安全网不能条件性）
    assert image_prompt.missing_requirement_lines(image_prompt.USE_VIDEO, "") == (
        image_prompt.requirement_lines(image_prompt.USE_VIDEO)
    )

    # ③ 两个调用点都**没有**自己的 _bare / _DECOR（有 = 又分叉了）
    root = _pathlib.Path("backend/app/services")
    for name in ("detail_image_prompt.py", "media_generation.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "_DECOR" not in source, f"{name} 又写了一份归一化常量"
        assert "missing_requirement_lines" in source, f"{name} 没有复用公共判重"
