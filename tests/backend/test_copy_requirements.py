"""文案要求（标题 / 正文 / 话题各自的提示词 + 区间 + 表情 + 数量）测试。

用户口径 2026-10-17：

> 「文案区域支持用户手动输入标题提示词和笔记正文，以及笔记话题，分析支持提供各自的
>   提示词，也可以为空，设置一个各自的文字数区间和是否带表情，以及生成的话题数量区间，
>   均需要限制」

判据落在**约束是否真的生效**上，不是「字段存在」上：
  - 区间倒挂必须 400（静默交换会让用户以为规则生效）；
  - 标题上限必须夹到平台硬顶 20（生成 50 字标题发布不了）；
  - 三块提示词可空（空 ⇒ 不产生任何额外要求）；
  - 追加段必须**同时**说出三组区间（只说一组 = 另两组等于没要求）。
"""

from __future__ import annotations

import pytest

from backend.app.services import copy_requirements as cr



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract


class TestDefaults:
    def test_empty_input_yields_platform_safe_defaults(self):
        """空输入 ⇒ 全部走默认，且**标题上限不超过平台硬顶**。"""
        req = cr.normalize({})
        assert req.title_min == 8 and req.title_max == cr.TITLE_MAX
        #: 用户口径 2026-10-17：正文默认 **30–300 字**（不是平台上限 1000）
        assert req.body_min == 30 and req.body_max == 300
        assert req.topic_min == 6 and req.topic_max == 10
        assert req.emoji == "auto"
        assert cr.TITLE_MAX == 20, "标题上限必须等于小红书平台的 20 字硬顶"
        assert cr.BODY_MAX == 1000, "正文上限必须等于平台 1000 字硬顶"

    def test_all_three_prompts_may_be_empty(self):
        """用户口径：*「也可以为空」* —— 三块都空是合法的，且**不产生任何补充**。"""
        req = cr.normalize({"title_prompt": "", "body_prompt": "  ", "topic_prompt": None})
        assert req.title_prompt == req.body_prompt == req.topic_prompt == ""
        #: ⚠️ 口径 2026-10-17（用户）：**模板是主体，三块是补充说明**
        #: 全默认 ⇒ 一个字都不加（否则会用默认区间覆盖模板自己的节奏）
        assert cr.build_instruction(req) == ""

    def test_empty_object_reports_is_empty(self):
        """全默认 ⇒ `is_empty` 为 True（入队时可省掉这份默认配置的落库）。"""
        assert cr.normalize({}).is_empty is True
        assert cr.normalize({"title_prompt": "要具体"}).is_empty is False


class TestRangeValidation:
    def test_inverted_title_range_is_rejected(self):
        """区间倒挂 ⇒ 400 错误（**不静默交换**：用户可能填反了）。

        ⚠️ 静默交换的后果：用户填「下限 15 / 上限 8」，界面看起来接受了，
        实际按 8–15 生成 —— 他永远不知道是自己填反还是系统乱来。
        """
        with pytest.raises(cr.CopyRequirementError) as excinfo:
            cr.normalize({"title_min": 15, "title_max": 8})
        assert excinfo.value.code == "LLM_COPY_RANGE_INVALID"
        assert "下限" in excinfo.value.message and "上限" in excinfo.value.message

    def test_inverted_body_range_is_rejected(self):
        with pytest.raises(cr.CopyRequirementError):
            cr.normalize({"body_min": 800, "body_max": 300})

    def test_inverted_topic_range_is_rejected(self):
        with pytest.raises(cr.CopyRequirementError) as excinfo:
            cr.normalize({"topic_min": 6, "topic_max": 2})
        assert "话题数量" in excinfo.value.message

    def test_title_max_is_clamped_to_platform_hard_limit(self):
        """标题上限填 50 ⇒ 夹到 20（平台发布不出去的数字给了等于骗人）。"""
        assert cr.normalize({"title_max": 50}).title_max == cr.TITLE_MAX

    def test_body_max_is_clamped_to_platform_hard_limit(self):
        assert cr.normalize({"body_max": 5000}).body_max == cr.BODY_MAX

    def test_topic_range_is_clamped(self):
        """话题数量夹在 1–10（0 个话题 = 放弃流量入口；100 个 = 模型没法照做）。"""
        assert cr.normalize({"topic_min": 0, "topic_max": 99}).topic_min == 1
        assert cr.normalize({"topic_min": 0, "topic_max": 99}).topic_max == 10

    def test_non_numeric_falls_back_to_default_without_raising(self):
        """非数字 ⇒ 退默认（这是「值本身不合法」，不是「用户表意矛盾」）。"""
        req = cr.normalize({"title_min": "abc", "title_max": None})
        assert req.title_min == 8 and req.title_max == cr.TITLE_MAX

    def test_prompt_length_is_capped(self):
        with pytest.raises(cr.CopyRequirementError):
            cr.normalize({"title_prompt": "字" * 501})


class TestEmoji:
    def test_three_modes_are_accepted(self):
        for mode in ("auto", "on", "off"):
            assert cr.normalize({"emoji": mode}).emoji == mode

    def test_invalid_emoji_is_rejected(self):
        """表情取值非法 ⇒ 400（不静默退 auto —— 用户以为「关了表情」，实际还在加）。"""
        with pytest.raises(cr.CopyRequirementError) as excinfo:
            cr.normalize({"emoji": "yes"})
        assert "auto/on/off" in excinfo.value.message

    def test_emoji_on_and_off_produce_opposite_instructions(self):
        on_text = cr.build_instruction(cr.normalize({"emoji": "on"}))
        off_text = cr.build_instruction(cr.normalize({"emoji": "off"}))
        assert "带适量表情" in on_text
        assert "不要出现表情" in off_text
        assert on_text != off_text

    def test_instruction_has_no_markdown_bold(self):
        """提示词不进渲染层 ⇒ 不许留 `**`（会原样发给模型，看着像没清洗）。"""
        for mode in ("auto", "on", "off"):
            assert "**" not in cr.build_instruction(cr.normalize({"emoji": mode}))


class TestInstruction:
    def test_instruction_states_all_three_ranges_when_all_changed(self):
        """三组都**改过** ⇒ 三组都出现（只说一组等于另两组白改）。"""
        text = cr.build_instruction(
            cr.normalize(
                {"title_min": 10, "title_max": 18, "body_min": 200, "body_max": 400,
                 "topic_min": 2, "topic_max": 4}
            )
        )
        assert "10–18" in text
        assert "200–400" in text
        assert "2–4" in text

    def test_prompts_are_rendered_when_given(self):
        text = cr.build_instruction(
            cr.normalize(
                {"title_prompt": "突出材质", "body_prompt": "口语化", "topic_prompt": "家居"}
            )
        )
        assert "突出材质" in text
        assert "口语化" in text
        assert "家居" in text

    def test_empty_prompts_are_not_rendered_as_empty_lines(self):
        """空提示词不产生「- 标题：」这种空行（模型会当成「标题要为空」）。"""
        text = cr.build_instruction(cr.normalize({}))
        assert "- 标题：" not in text
        assert "- 正文：" not in text
        assert "- 话题：" not in text


class TestBuildSlots:
    """槽位（模板占位符）—— 用户口径 2026-10-17：

    > 「字数，表情以及话题设置均有默认值，公共模版中应无需固定添加固定提示词，
    >   调用大模型时需最终拼接」
    """

    def test_slots_always_carry_values_even_when_default(self):
        """**与 `build_instruction` 相反**：默认值也必须给（否则句子骨架残了）。"""
        slots = cr.build_slots(cr.normalize({}))
        assert slots["title_length"] == "8–20 字"
        assert slots["body_length"] == "30–300 字"
        assert slots["topic_count"] == "6–10 个"
        assert slots["emoji_rule"]
        assert slots["copy_supplement"] == ""  #: 补充段仍按「不填不加」

    def test_slots_follow_custom_values(self):
        slots = cr.build_slots(
            cr.normalize({"body_min": 500, "body_max": 800, "emoji": "off", "title_prompt": "x"})
        )
        assert slots["body_length"] == "500–800 字"
        assert "不要出现表情" in slots["emoji_rule"]
        assert "标题方向" in slots["copy_supplement"]

    def test_slots_are_whitelisted_in_renderer(self):
        """槽位名必须进 `ALLOWED_SLOTS`，否则渲染直接抛 `PROMPT_SLOT_UNKNOWN`。"""
        from backend.app.services import prompt_renderer as pr

        for name in ("title_length", "body_length", "topic_count", "emoji_rule", "copy_supplement"):
            assert name in pr.ALLOWED_SLOTS, f"{name} 不在白名单 ⇒ 模板一引用就报错"

    def test_renderer_actually_fills_them(self):
        """**端到端**：渲染后不许残留 `{title_length}` 这种字面量。

        ⚠️ 这条咬的是真 bug（2026-10-17）：槽位进了白名单、`build_slots` 也返回了值，
        但 `render_skeleton` 的取值循环只遍历 `TEXT_SLOTS + MEDIA_SLOTS`
        ⇒ 新槽位被替换成**空串**，句子里出现「字数 ，核心搜索词必须前置」。
        """
        from backend.app.services.prompt_renderer import render_skeleton

        req = cr.normalize({})
        skeleton = "标题字数 {title_length}；正文 {body_length}；话题 {topic_count}；{emoji_rule}"
        out = render_skeleton(skeleton, product={}, extra=cr.build_slots(req))
        assert "8–20 字" in out
        assert "30–300 字" in out
        assert "6–10 个" in out
        assert "{" not in out


class TestSupplementSemantics:
    """模板是主体，三块是**补充说明**（用户口径 2026-10-17）。

    > 「已经有一个提示词模板与下面的各个部分的提示词以及相关的配置是否有冲突，
    >   但是各个部分的提示词用户是可选填的，关系应是对顶部公共模板的补充说明」
    """

    def test_defaults_produce_no_supplement(self):
        """全默认 ⇒ **空串**：完全按模板走（这是「不填就不补充」的判据）。"""
        assert cr.build_instruction(cr.normalize({})) == ""

    def test_only_filled_items_are_emitted(self):
        """只填标题提示词 ⇒ 只出标题一条，不带正文字数/话题数量/表情。"""
        text = cr.build_instruction(cr.normalize({"title_prompt": "突出材质"}))
        assert "标题方向" in text
        assert "正文字数" not in text
        assert "话题数量" not in text
        assert "表情" not in text

    def test_changed_range_is_emitted_even_without_prompt(self):
        """用户改了区间（没填提示词）⇒ 也算补充，必须下发（否则白改了）。"""
        text = cr.build_instruction(cr.normalize({"body_min": 500, "body_max": 800}))
        assert "500–800" in text
        #: 没改的项不出现（避免与模板冲突）
        assert "标题字数" not in text
        assert "话题数量" not in text

    def test_emoji_auto_is_silent_but_on_off_speak(self):
        """`auto` 是默认 ⇒ 不提（模板自己判断）；明确 on/off 才说。"""
        assert "表情" not in cr.build_instruction(cr.normalize({"emoji": "auto"}))
        assert "带适量表情" in cr.build_instruction(cr.normalize({"emoji": "on"}))
        assert "不要出现表情" in cr.build_instruction(cr.normalize({"emoji": "off"}))

    def test_supplement_declares_its_precedence(self):
        """补充段必须**自称是补充**并声明冲突时以此为准（否则模型不知道听谁）。"""
        text = cr.build_instruction(cr.normalize({"title_prompt": "x"}))
        assert "补充说明" in text
        assert "上述模板基础之上的补充" in text

    def test_supplement_has_no_markdown_bold(self):
        """提示词不进渲染层 ⇒ 不许留 `**`（会原样发给模型）。"""
        assert "**" not in cr.build_instruction(cr.normalize({"title_prompt": "x", "emoji": "on"}))


class TestEnforce:
    def test_overlong_title_is_trimmed_to_platform_limit(self):
        """模型给了 30 字标题 ⇒ 裁到 20（否则发布时被平台拒/被裁）。"""

        class _Gen:
            def __init__(self):
                self.title = "这是一个非常非常长的商品标题超过了平台限制二十个字以上真的很长"
                self.topics: list[str] = []

        gen = _Gen()
        cr.enforce(gen, cr.normalize({}))
        #: ⚠️ 断言**实例**的 title（上一版写成类属性 —— 那测的是测试自己）
        assert len(gen.title) <= cr.TITLE_MAX, f"标题没被裁：{gen.title}"

    def test_too_many_topics_are_trimmed(self):
        class _Gen:
            title = "标题"
            topics = [f"话题{i}" for i in range(20)]

        gen = cr.enforce(_Gen(), cr.normalize({"topic_min": 2, "topic_max": 4}))
        assert len(gen.topics) == 4

    def test_short_topics_are_not_invented(self):
        """模型只给 2 个话题而要求 5 个 ⇒ **不编造**（缺值口径：不造假）。"""

        class _Gen:
            title = "标题"
            topics = ["a", "b"]

        gen = cr.enforce(_Gen(), cr.normalize({"topic_min": 5, "topic_max": 6}))
        assert gen.topics == ["a", "b"]

    def test_enforce_is_safe_on_unexpected_shapes(self):
        """兜底失败不该废掉整篇（真正的硬校验在发布前）。"""
        assert cr.enforce(None, cr.normalize({})) is None


class TestRegenInstruction:
    """**局部重生成的提示词**（用户口径 2026-10-18：*「生成局部时的提示词如何给」*）。

    ⚠️ 这一组钉的是「顺序」而不是「文案」：局部说明必须能**压过模板的输出契约**。
    """

    def test_regen_instruction_names_only_the_target_block(self):
        out = cr.build_regen_instruction(
            "body", snapshot={"title": "旧标题", "body": "旧正文", "topics": ["话题A"]}
        )
        assert "只重写**正文**" in out
        assert "旧标题" in out and "旧正文" in out and "话题A" in out
        assert '"body"' in out

    def test_regen_instruction_is_empty_for_media(self):
        """只重配图不调文本模型 ⇒ 不该有这段。"""
        assert cr.build_regen_instruction("media", snapshot={}) == ""

    def test_supplement_comes_before_regen_block(self):
        """用户补充说明在前、局部说明在后（后出现的指令权重更高）。"""
        import backend.app.services.note_gen_llm_worker as worker

        req = cr.normalize({"body_prompt": "口语一点"})
        out = worker._compose_instruction(
            req, scope="body", snapshot={"title": "T", "body": "B", "topics": []}
        )
        assert out.index("正文方向：口语一点") < out.index("【局部重生成】")
