"""数据包列规则：执行引擎（纯逻辑）+ 端点契约（设计 40）。

## 为什么要这一组用例

用户口径 2026-10-14：
> 「每一类数据包的一列是取值规则是可以固定的，是否应加表固定这些规则，
>   后续再有新的数据包时只需要配置规则」

⇒ 规则是**用户配置的资产**，配错了他自己也要能看出来。所以判据分三层：

1. **纯逻辑**（执行引擎）：每个 op 的边界都要钉死（段数不够 / 没有包裹符 / 负数段号…）；
2. **端点契约**：4xx 的**结构化 code**、409 的**友好化**（不回 500）、**数据隔离**；
3. **真实数据**：用两个真机数据包的形态跑一遍（设计 40 §1.1）。

⚠️ **提不出 ⇒ 留空** 是本组最重要的一条（AGENTS.md 硬约束 5「缺值不造假」）：
把原值留在格子里，用户会以为规则生效了，拿着旧值去发布。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from backend.app.models.data_package_rule import (
    OP_EXTRACT_BETWEEN,
    OP_EXTRACT_SEGMENT,
    OP_EXTRACT_STRIP,
    OP_FIXED_TEXT,
    OP_COPY_COLUMN,
    OP_COPY_EXTRACT,
    OPS_NEED_SOURCE_COLUMN,
    RULE_OPS,
    OP_MATH_SCALE,
)
from backend.app.services import data_package_rule_service as service


# =========================================================================== #
# 一、纯逻辑：extract_segment（按分隔符切段）
# =========================================================================== #


class TestExtractSegment:
    def test_takes_the_nth_segment_of_semicolon_three_parts(self):
        """真机 包1：`木;中古色-实木三斗柜;上门安装` 取第 2 段（设计 40 §1.1）。"""
        params = {"sep": ";", "index": 2}
        got = service.apply_extract_segment("木;中古色-实木三斗柜;上门安装", params)
        assert got == "中古色-实木三斗柜"

    def test_takes_the_first_segment_of_colon_two_parts(self):
        """真机 包2：`颜色分类:【中古风】实木三斗柜` 取第 1 段。"""
        params = {"sep": ":", "index": 1}
        got = service.apply_extract_segment("颜色分类:【中古风】实木三斗柜", params)
        assert got == "颜色分类"

    def test_negative_index_takes_from_the_end(self):
        """`-1` = 最后一段（真机 包1 的 `上门安装` 也常要取）。"""
        params = {"sep": ";", "index": -1}
        got = service.apply_extract_segment("木;中古色-实木三斗柜;上门安装", params)
        assert got == "上门安装"

    def test_not_enough_segments_returns_none(self):
        """⚠️ 段数不够 ⇒ **None**（留空），**不是原值**（硬约束 5）。"""
        params = {"sep": ";", "index": 3}
        assert service.apply_extract_segment("木;甲", params) is None

    def test_nonexistent_negative_index_returns_none(self):
        params = {"sep": ";", "index": -5}
        assert service.apply_extract_segment("木;甲", params) is None

    def test_separator_absent_returns_none(self):
        """不含分隔符 ⇒ 整串就是第 1 段；但取第 2 段就该提不出。"""
        assert service.apply_extract_segment("没有分隔符", {"sep": ";", "index": 2}) is None
        assert service.apply_extract_segment("没有分隔符", {"sep": ";", "index": 1}) == "没有分隔符"

    def test_strips_surrounding_spaces(self):
        params = {"sep": "|", "index": 2}
        assert service.apply_extract_segment("a | b | c", params) == "b"

    def test_index_accepts_string_digits(self):
        """JSON 里可能是 `"2"`（前端表单没转数字）—— 宽松接受，不因此报错。"""
        assert service.apply_extract_segment("a;b;c", {"sep": ";", "index": "2"}) == "b"


# =========================================================================== #
# 二、纯逻辑：extract_between（取包裹符之间）
# =========================================================================== #


class TestExtractBetween:
    def test_extracts_inside_parentheses(self):
        """真机 包2 图片名：`SKU01_颜色分类(【中古风】实木三斗柜).jpg`。"""
        params = {"open": "(", "close": ")"}
        got = service.apply_extract_between(
            "SKU01_颜色分类(【中古风】实木三斗柜).jpg", params
        )
        assert got == "【中古风】实木三斗柜"

    def test_open_only_takes_rest_of_line(self):
        """只给 open ⇒ 取到该行末尾（用户只想要「某符号之后」）。"""
        got = service.apply_extract_between("前缀-要的内容", {"open": "-"})
        assert got == "要的内容"

    def test_missing_open_returns_none(self):
        assert service.apply_extract_between("无括号", {"open": "("}) is None

    def test_incomplete_pair_returns_none(self):
        """⚠️ 只有起始符没有结束符 ⇒ 提不出（**不**猜「到末尾」——
        那是 `close` 为空的语义，用户没这么配就不该这么解释）。"""
        assert service.apply_extract_between("甲(乙", {"open": "(", "close": ")"}) is None

    def test_occurrence_picks_the_nth_pair(self):
        got = service.apply_extract_between(
            "a(1)b(2)c", {"open": "(", "close": ")", "occurrence": 2}
        )
        assert got == "2"

    def test_occurrence_beyond_available_returns_none(self):
        got = service.apply_extract_between(
            "a(1)", {"open": "(", "close": ")", "occurrence": 3}
        )
        assert got is None


# =========================================================================== #
# 三、纯逻辑：extract_strip（去前后缀）
# =========================================================================== #


class TestExtractStrip:
    def test_removes_literal_suffix(self):
        got = service.apply_extract_strip("图.jpg", {"suffix": ".jpg"})
        assert got == "图"

    def test_removes_literal_prefix(self):
        got = service.apply_extract_strip("SKU01_甲", {"prefix": "SKU01_"})
        assert got == "甲"

    def test_literal_prefix_is_not_a_regex(self):
        """⚠️ **默认按字面** —— `.` 不该被当通配符（正则不做默认工具，设计 40 §1.2）。"""
        got = service.apply_extract_strip("a.b", {"prefix": "a."})
        assert got == "b"
        #: 若 `.` 被当正则，`aXb` 也会被削掉前缀 —— 断言它**不会**。
        assert service.apply_extract_strip("aXb", {"prefix": "a."}) == "aXb"

    def test_regex_mode_when_explicitly_enabled(self):
        """真机 包1 图片名前缀含序号（每个不同）⇒ 必须显式开正则。"""
        got = service.apply_extract_strip(
            "SKU图_01_中古色-实木三斗柜.jpg",
            {"prefix": r"^SKU图_\d+_", "suffix": r"\.jpg$", "is_regex": True},
        )
        assert got == "中古色-实木三斗柜"

    def test_regex_mode_anchors_at_both_ends(self):
        """⚠️ 前缀只削**开头**一个、后缀只削**结尾**一个 —— 否则中段会被误削。"""
        got = service.apply_extract_strip(
            "甲_甲_甲", {"prefix": "甲_", "suffix": "_甲", "is_regex": True}
        )
        assert got == "甲"

    def test_missing_prefix_does_nothing(self):
        """前缀对不上 ⇒ **原样返回**（不是 None）—— 「没削到」与「提不出」不同。"""
        assert service.apply_extract_strip("abc", {"prefix": "xyz"}) == "abc"

    def test_regex_prefix_only_strips_at_the_start(self):
        """⚠️ 正则前缀**只削开头**（`^` 锚定）。

        ⚠️ 用例设计要点（**首轮写成 `甲_乙_甲_丙` 没抓住变异，第二轮才修对**）：
        目标的**第一次出现必须在开头** ⇒ 此时「有 `^`」与「没 `^` 但
        `count=1`」结果相同，变异逃逸。要让变异暴露，得让**首次匹配不在开头**：

        ```
        value = "头甲_尾"      # `甲_` 第一次出现在下标 1（不在开头）
        带 ^  ⇒ 不匹配 ⇒ 原样 "头甲_尾"
        无 ^  ⇒ 削掉   ⇒ "头尾"        ← 变异体，静默改错值
        ```
        """
        #: 首次出现**不在开头** ⇒ 只有 `^` 锚定才不会被削。
        got = service.apply_extract_strip(
            "头甲_尾", {"prefix": "甲_", "is_regex": True}
        )
        assert got == "头甲_尾", "正则前缀不在开头却被削了（缺 `^` 锚定）"
        #: 反向：出现在开头时确实要削。
        assert (
            service.apply_extract_strip("甲_尾", {"prefix": "甲_", "is_regex": True})
            == "尾"
        )

    def test_regex_suffix_only_strips_at_the_end(self):
        """后缀同理：只削**结尾**（`$` 锚定）—— 首次出现的**末尾**匹配才算。

        ⚠️ 同上：得让「末尾匹配」不是「首次匹配」（`count=1` 只替换第一次）。
        """
        #: `_甲` 首次出现在下标 1（不是末尾）⇒ 带 `$` 时不该削。
        assert (
            service.apply_extract_strip("头_甲尾", {"suffix": "_甲", "is_regex": True})
            == "头_甲尾"
        )
        #: 出现在末尾时确实要削。
        assert (
            service.apply_extract_strip("头_甲", {"suffix": "_甲", "is_regex": True})
            == "头"
        )


# =========================================================================== #
# 四、纯逻辑：参数校验（配错要在保存时就知道）
# =========================================================================== #


class TestValidateParams:
    def test_unknown_op_rejected(self):
        with pytest.raises(service.DataPackageRuleError) as ei:
            service.validate_params("没这个操作", {})
        assert ei.value.code == "DP_RULE_BAD_OP"

    def test_fixed_text_and_math_scale_are_implemented_now(self):
        """⚠️ 2026-10-15 用户口径把两条一并要了 ⇒ **不再是「尚未实现」**。

        用户原话：
        > 「提取数据规则中**新增规则未支持：固定文本 + 批量运算（+ - × ×+ ×−）**
        >   相关的规则」

        ⇒ 本用例是**方向的守卫**：谁把 `_IMPLEMENTED_OPS` 改回去（或又写一个
        「尚未实现」分支），这里必红。旧断言（`assert "尚未实现" in message`）
        随口径一起退场 —— 留着会拦住新实现。
        """
        service.validate_params(OP_FIXED_TEXT, {"text": "100"})
        service.validate_params(OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}, {"op": "add", "value": 5}]})

    def test_empty_separator_rejected(self):
        with pytest.raises(service.DataPackageRuleError) as ei:
            service.validate_params(OP_EXTRACT_SEGMENT, {"sep": "", "index": 1})
        assert ei.value.code == "DP_RULE_BAD_PARAM"

    def test_zero_index_rejected(self):
        """段号 1 起数，0 无意义 —— 拦在保存时（而不是执行后整列留空）。"""
        with pytest.raises(service.DataPackageRuleError) as ei:
            service.validate_params(OP_EXTRACT_SEGMENT, {"sep": ";", "index": 0})
        assert ei.value.code == "DP_RULE_BAD_PARAM"

    def test_non_integer_index_rejected(self):
        with pytest.raises(service.DataPackageRuleError) as ei:
            service.validate_params(OP_EXTRACT_SEGMENT, {"sep": ";", "index": "第二段"})
        assert ei.value.code == "DP_RULE_BAD_PARAM"

    def test_bool_index_rejected(self):
        """⚠️ `bool` 是 `int` 的子类 —— 不挡会被 `True` 当 `1` 静默通过。"""
        with pytest.raises(service.DataPackageRuleError):
            service.validate_params(OP_EXTRACT_SEGMENT, {"sep": ";", "index": True})

    def test_empty_open_rejected(self):
        with pytest.raises(service.DataPackageRuleError):
            service.validate_params(OP_EXTRACT_BETWEEN, {"open": ""})

    def test_strip_needs_at_least_one_side(self):
        with pytest.raises(service.DataPackageRuleError) as ei:
            service.validate_params(OP_EXTRACT_STRIP, {"prefix": "", "suffix": ""})
        assert ei.value.code == "DP_RULE_BAD_PARAM"

    def test_bad_regex_rejected_when_enabled(self):
        with pytest.raises(service.DataPackageRuleError) as ei:
            service.validate_params(
                OP_EXTRACT_STRIP, {"prefix": "([", "is_regex": True}
            )
        assert ei.value.code == "DP_RULE_BAD_PARAM"


# =========================================================================== #
# 三·补、纯逻辑：validate_params —— `fixed_text` / `math_scale` 参数校验
#        （用户口径 2026-10-15：「运算类需要指定计算的基础列」「计算时为空的行默认为 0」）
# =========================================================================== #


class TestValidateParamsFixedText:
    def test_missing_text_rejected(self):
        """⚠️ **没配 `text`** ⇒ 400（不是「静默把整列清空」）。"""
        with pytest.raises(service.DataPackageRuleError) as ei:
            service.validate_params(OP_FIXED_TEXT, {})
        assert ei.value.code == "DP_RULE_BAD_PARAM"

    def test_none_text_rejected(self):
        with pytest.raises(service.DataPackageRuleError) as ei:
            service.validate_params(OP_FIXED_TEXT, {"text": None})
        assert ei.value.code == "DP_RULE_BAD_PARAM"

    def test_empty_text_allowed(self):
        """⚠️ `text=""` 与「缺 `text`」**不是一回事**：前者是用户**故意**整列清空。"""
        service.validate_params(OP_FIXED_TEXT, {"text": ""})

    def test_text_number_is_accepted_as_text(self):
        """`{"text": 100}`（用户手抖传了数字）⇒ 当文本「100」，不报错。"""
        service.validate_params(OP_FIXED_TEXT, {"text": 100})

    def test_overlong_text_rejected(self):
        with pytest.raises(service.DataPackageRuleError) as ei:
            service.validate_params(
                OP_FIXED_TEXT, {"text": "x" * (service.MAX_FIXED_TEXT_LEN + 1)}
            )
        assert ei.value.code == "DP_RULE_BAD_PARAM"


class TestValidateParamsMathScale:
    def test_factor_required(self):
        with pytest.raises(service.DataPackageRuleError) as ei:
            service.validate_params(OP_MATH_SCALE, {})
        assert ei.value.code == "DP_RULE_BAD_PARAM"

    def test_non_number_factor_rejected(self):
        with pytest.raises(service.DataPackageRuleError) as ei:
            service.validate_params(OP_MATH_SCALE, {"steps": [{"op": "mul", "value": "两倍"}]})
        assert ei.value.code == "DP_RULE_BAD_PARAM"

    def test_bool_factor_rejected(self):
        """⚠️ `True` 是 `int` 的子类 —— 不挡就变成「整列 ×1」静默通过。"""
        with pytest.raises(service.DataPackageRuleError):
            service.validate_params(OP_MATH_SCALE, {"steps": [{"op": "mul", "value": True}]})

    def test_infinite_factor_rejected(self):
        """`NaN` / `inf` 能过 `float()`，但写进列就是一片 nan。"""
        with pytest.raises(service.DataPackageRuleError):
            service.validate_params(OP_MATH_SCALE, {"steps": [{"op": "mul", "value": float("inf")}]})

    def test_over_max_factor_rejected(self):
        with pytest.raises(service.DataPackageRuleError):
            service.validate_params(OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1e12}]})

    def test_offset_optional(self):
        """纯乘法不需要 `offset`（三种合法 op2 见下）。"""
        service.validate_params(OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}]})

    def test_bad_step_op_rejected(self):
        """步骤里的运算符只能是 乘 / 加 / 减。"""
        with pytest.raises(service.DataPackageRuleError) as ei:
            service.validate_params(
                OP_MATH_SCALE, {"steps": [{"op": "div", "value": 2}]}
            )
        assert ei.value.code == "DP_RULE_BAD_PARAM"

    def test_empty_steps_rejected(self):
        """⚠️ 空步骤列表 ⇒ 400（`apply` 无步可跑，结果会等于原值 —— 静默错）。"""
        with pytest.raises(service.DataPackageRuleError):
            service.validate_params(OP_MATH_SCALE, {"steps": []})

    def test_too_many_steps_rejected(self):
        with pytest.raises(service.DataPackageRuleError):
            service.validate_params(
                OP_MATH_SCALE,
                {"steps": [{"op": "add", "value": 1}] * 21},
            )

    def test_add_with_zero_value_is_allowed(self):
        """⚠️ `+0` **合法**（用户显式配的）—— 「随意搭配」口径下不给「+0 是漏填」加特例。

        原用例叫 `test_add_without_offset_rejected`（`factor/offset/op2` 时代：
        选了 add 却不给 offset ⇒ 400）。改成步骤列表后，「每一步都自带 value」
        是结构上的保证 ⇒ 那个前提**消失**，改测「`+0` 能存下来」。
        """
        service.validate_params(
            OP_MATH_SCALE,
            {"steps": [{"op": "mul", "value": 1.7}, {"op": "add", "value": 0}]},
        )

    def test_negative_offset_is_allowed(self):
        """负数 `offset` 合法（用户也可能这么配）。"""
        service.validate_params(OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}, {"op": "sub", "value": -5}]})

    def test_all_four_combinations_accepted(self):
        """四种组合（× / +− / ×+ / ×−）都要能存下来。"""
        service.validate_params(OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}]})
        service.validate_params(OP_MATH_SCALE, {"steps": [{"op": "add", "value": 5}]})
        service.validate_params(OP_MATH_SCALE, {"steps": [{"op": "sub", "value": 5}]})
        service.validate_params(OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}, {"op": "add", "value": 5}]})
        service.validate_params(OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}, {"op": "sub", "value": 5}]})


class TestParseParams:
    def test_bad_json_does_not_explode(self):
        """⚠️ 坏 JSON **不抛给用户**（列表接口不该因一行脏数据 500）。"""
        assert service.parse_params("{不是 json") == {}
        assert service.parse_params("") == {}
        assert service.parse_params(None) == {}

    def test_valid_json(self):
        assert service.parse_params('{"sep": ";"}') == {"sep": ";"}

    def test_non_dict_json_returns_empty(self):
        assert service.parse_params("[1,2]") == {}


class TestApplyRule:
    def test_empty_value_returns_none(self):
        """原值本身为空 ⇒ 没什么可提取的（留空）。"""
        assert service.apply_rule("", OP_EXTRACT_SEGMENT, {"sep": ";", "index": 1}) is None
        assert service.apply_rule(None, OP_EXTRACT_SEGMENT, {"sep": ";", "index": 1}) is None

    def test_pure_function_semantics(self):
        """⚠️ 同输入必得同输出（不在上次结果上迭代）—— 反复执行不会越算越偏。"""
        params = {"sep": ":", "index": 1}
        first = service.apply_rule("颜色分类:甲", OP_EXTRACT_SEGMENT, params)
        second = service.apply_rule("颜色分类:甲", OP_EXTRACT_SEGMENT, params)
        assert first == second == "颜色分类"

    def test_column_apply_counts_misses(self):
        out = service.apply_rule_to_column(
            ["a;b", "c", "d;e"], OP_EXTRACT_SEGMENT, {"sep": ";", "index": 2}
        )
        assert out == ["b", None, "e"]


# =========================================================================== #
# 四·补、纯逻辑：fixed_text / math_scale 的执行
#        （用户口径 2026-10-15：「固定文本」无条件整列设值；
#          「批量运算（+ - × ×+ ×−）」「计算时为空的行默认为 0」）
# =========================================================================== #


class TestApplyFixedText:
    """**整列设为固定文本** —— 与 `extract_*` 的关键区别：原值为空的行**也要写**。"""

    def test_sets_every_row_including_empty_ones(self):
        """⚠️ 用户口径「固定文本」= **无条件设为**。

        与 `extract_*` 的方向**相反**：那条是「从原值里取一段，取不到留空」，
        这条是「这一列统统写成这个值」—— 原值为 `None` / `""` 的行**也要写**，
        否则用户看到「大部分行写上了、空行没写」，会以为规则漏跑了。
        """
        out = service.apply_rule_to_column(
            ["木;甲;上门安装", None, "", "任意垃圾内容"], OP_FIXED_TEXT, {"text": "100"}
        )
        assert out == ["100", "100", "100", "100"]

    def test_apply_rule_single_value(self):
        assert service.apply_rule(None, OP_FIXED_TEXT, {"text": "包邮"}) == "包邮"

    def test_numeric_text_becomes_text(self):
        """`{"text": 100}` ⇒ `"100"`（数字手抖传进来也当文本）。"""
        assert service.apply_fixed_text({"text": 100}) == "100"

    def test_text_with_spaces_kept_verbatim(self):
        """⚠️ **不 strip**：空格是用户想要的内容的一部分时不能被吃掉。"""
        assert service.apply_fixed_text({"text": " 100 "}) == " 100 "

    def test_empty_text_clears_the_column(self):
        """`text=""` ⇒ 整列清空（**这是用户配的**，不是提不出）。"""
        assert service.apply_rule_to_column(["a", None], OP_FIXED_TEXT, {"text": ""}) == [
            "",
            "",
        ]

    def test_no_none_in_result(self):
        """⚠️ 结果里**不允许有 `None`** —— 有的话前端会当「提不出 ⇒ 留空」渲染。"""
        out = service.apply_rule_to_column([None, "x"], OP_FIXED_TEXT, {"text": "9"})
        assert all(v is not None for v in out)


class TestApplyMathScale:
    """**批量运算** —— 四种组合 + 空值按 0（用户口径 2026-10-15）。"""

    def test_pure_multiply(self):
        out = service.apply_rule_to_column(["100", "200"], OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}]})
        assert out == ["170", "340"]

    def test_pure_multiply_matches_user_example(self):
        """用户原话里的例子：`1.7 × 3` —— 不能写成 `5.1000000000000005`。"""
        assert service.apply_rule("3", OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}]}) == "5.1"

    def test_add_fixed_value(self):
        """「加固定值」= `factor=1` + `op2="add"`（不额外造模式）。"""
        out = service.apply_rule_to_column(["100", "200"], OP_MATH_SCALE, {"steps": [{"op": "add", "value": 5}]})
        assert out == ["105", "205"]

    def test_subtract_fixed_value(self):
        out = service.apply_rule_to_column(["100", "200"], OP_MATH_SCALE, {"steps": [{"op": "sub", "value": 5}]})
        assert out == ["95", "195"]

    def test_multiply_then_add(self):
        """「×+」：**先乘后加**（不是先加后乘）。"""
        out = service.apply_rule_to_column(["10", "20"], OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}, {"op": "add", "value": 5}]})
        assert out == ["22", "39"]

    def test_multiply_then_subtract(self):
        """「×−」：先乘后减。"""
        out = service.apply_rule_to_column(["10"], OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 2}, {"op": "sub", "value": 5}]})
        assert out == ["15"]

    def test_empty_value_counts_as_zero(self):
        """⚠️ **核心口径**：「计算时为空的行**默认为 0**」（用户原话）。

        与 `extract_*` 的「提不出 ⇒ 留空」**方向相反**，是刻意的：
        运算里整列都要有一个数，留空会让半列结果空洞。
        """
        out = service.apply_rule_to_column(
            [None, "", "  ", "100"], OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 2}]}
        )
        assert out == ["0", "0", "0", "200"]

    def test_unrecognized_content_also_counts_as_zero(self):
        """⚠️ 认不出的内容（`"abc"`）也按 0 —— 与「为空的行默认为 0」同向。

        用户在表里看到的就是「这格没数」；但要**记原因行**（`count_unrecognized`）。

        ⚠️ 判据只认**纯数字形态**（允许 `￥¥` 与千分位逗号，见 `_as_number_or_none`）——
        表格里那一格本来就是数字，不是「100元」这种带中文单位的写法。
        ⇒ 认不出的走 0、认得出的照算，**两者在同一列里并存**。
        """
        out = service.apply_rule_to_column(
            ["abc", "100", "￥1,200"], OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 2}]}
        )
        assert out == ["0", "200", "2400"]

    def test_count_unrecognized_reports_those_rows(self):
        """认不出的行数要数出来（执行结果的 `reason` 用它，硬约束 5 同型静默错）。"""
        values = ["abc", None, "", "100"]
        assert service.count_unrecognized(values, OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 2}]}) == 3
        #: 非运算类一律 0（那些 op 的缺值语义是「留空」，由 `missed` 表达）。
        assert service.count_unrecognized(values, OP_EXTRACT_SEGMENT, {"sep": ";", "index": 1}) == 0

    def test_no_none_in_result(self):
        """⚠️ 运算结果**不会有 `None`**（这是与 extract 家族最容易被搞混的一点）。"""
        out = service.apply_rule_to_column([None, "abc"], OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}]})
        assert all(v is not None for v in out)

    def test_strips_currency_and_thousand_separators(self):
        """`￥1,200` 这种从表里读出来的写法要认（本项目 OCR/表格的常见形态）。"""
        assert service.apply_rule("￥1,200", OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1}]}) == "1200"

    def test_decimal_result_is_rounded_to_two_places(self):
        """⚠️ 结果**最多两位小数**（先 round 再判整，见 `format_math_result`）。

        ⚠️ **不要用 `1 × 1.005` 当判据** —— `1.005` 在二进制里是 `1.00499999…`，
        `round(1.005, 2) == 1.0` 是**浮点本身的正确行为**，不是缺陷
        （实测踩过：拿它当「保留两位」的判据会得到一个永远红的用例）。
        改用不会踩浮点的值：`21 × 1.7 = 35.7`（一位小数）+ `1.7 × 3 = 5.1`。
        """
        assert service.apply_rule("21", OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}]}) == "35.7"
        assert service.apply_rule("3", OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}]}) == "5.1"
        #: 真需要两位的：`10 × 1.05 = 10.5`（一位）与 `3 × 1.11 = 3.33`（两位）。
        assert service.apply_rule("3", OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.11}]}) == "3.33"

    def test_integer_result_has_no_decimal_point(self):
        """⚠️ 整数结果**不带 `.0`**（表格里 `200.0` 与 `200` 是两个观感）。

        这是 `format_math_result` 的**真承重面**：把 `str(int(rounded))` 改成
        `f"{rounded:.2f}"` ⇒ 整列变 `200.00`（实测会被本用例咬住）。

        ⚠️ 反面教训（实测踩过，写在这里防回潮）：**不要**拿「去掉 `round()`
        会不会漏浮点尾数」当判据 —— 末尾的 `f"{...:.2f}"` 已经截了，
        去掉 `round()` **输出逐字不变**，那种变异是**哑弹**（永远绿）。
        """
        assert service.apply_rule("100", OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 2}]}) == "200"
        assert service.apply_rule("21", OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}]}) == "35.7"
        #: 整数结果里不许有小数点。
        for value, factor in (("100", 2), ("0.7", 10), ("4.2", 5)):
            got = service.apply_rule(value, OP_MATH_SCALE, {"steps": [{"op": "mul", "value": factor}]})
            assert got is not None and "." not in got, (
                f"{value} × {factor} 是整数结果，却带了小数点：{got!r}"
            )

    def test_result_keeps_at_most_two_decimals(self):
        """⚠️ 结果**最多两位小数**（第三位起截掉）。

        这是第二个真承重面：`_RESULT_DECIMALS` 若从 2 改 3 ⇒ `3 × 1.111 = 3.333`
        会被写进列（实测会被本用例咬住）。

        ⚠️ 顺带记一个**浮点陷阱**（差点写成永远红的用例）：
        `1 × 1.005 = 1.00499999…`，`round(1.005, 2) == 1.0` 是**浮点本身的正确行为**，
        不是缺陷 ⇒ 别拿 `1.005` 当「保留两位」的判据。
        """
        #: 两位的照常保留。
        assert service.apply_rule("3", OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.11}]}) == "3.33"
        #: 三位的截到两位。
        assert service.apply_rule("3", OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.111}]}) == "3.33"
        assert service.apply_rule("7", OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.111}]}) == "7.78"
        #: 逐字：结果里小数点后不超过两位。
        for value, factor in (("3", 1.111), ("7", 1.111), ("1", 1.23456)):
            got = service.apply_rule(value, OP_MATH_SCALE, {"steps": [{"op": "mul", "value": factor}]})
            assert got is not None
            if "." in got:
                assert len(got.split(".")[1]) <= 2, (
                    f"{value} × {factor} 的结果超过两位小数：{got!r}"
                )


class TestSummary:
    def test_segment_summary_is_human(self):
        got = service.make_summary(OP_EXTRACT_SEGMENT, {"sep": ";", "index": 2})
        assert got == "按「;」取第 2 段"
        assert "{" not in got, "摘要不能是 JSON（用户看不懂）"

    def test_between_summary(self):
        got = service.make_summary(OP_EXTRACT_BETWEEN, {"open": "(", "close": ")"})
        assert got == "取 ( ) 之间的内容"

    def test_between_without_close_says_after(self):
        got = service.make_summary(OP_EXTRACT_BETWEEN, {"open": "-"})
        assert got == "取 - 之后的内容"

    def test_strip_summary_marks_regex(self):
        got = service.make_summary(
            OP_EXTRACT_STRIP, {"prefix": "x", "is_regex": True}
        )
        assert got == "去前缀（正则）"

    def test_fixed_text_summary(self):
        """「固定为「100」」—— 用户要能一眼看出整列会变成什么。"""
        assert service.make_summary(OP_FIXED_TEXT, {"text": "100"}) == "固定为「100」"
        assert service.make_summary(OP_FIXED_TEXT, {"text": "包邮"}) == "固定为「包邮」"

    def test_math_scale_summaries_for_common_combinations(self):
        """⚠️ 常见组合各有人话文案（顺次拼接：`×1.7` / `+5` / `−5` / `×1.7 +5` / `×1.7 −5`）。"""
        assert service.make_summary(OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}]}) == "×1.7"
        assert (
            service.make_summary(OP_MATH_SCALE, {"steps": [{"op": "add", "value": 5}]})
            == "+5"
        )
        assert (
            service.make_summary(OP_MATH_SCALE, {"steps": [{"op": "sub", "value": 5}]})
            == "−5"
        )
        assert (
            service.make_summary(OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}, {"op": "add", "value": 5}]})
            == "×1.7 +5"
        )
        assert (
            service.make_summary(OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}, {"op": "sub", "value": 5}]})
            == "×1.7 −5"
        )

    def test_math_scale_summary_follows_arbitrary_order(self):
        """⚠️ 摘要**逐字对应执行顺序**（用户口径「加减乘可随意搭配」）。

        原用例叫 `test_math_scale_summary_omits_noop_offset`（`factor/offset/op2` 时代：
        「不参与」的运算不进摘要）。改成步骤列表后**每一步都是用户显式加的**
        ⇒ 不再有「不参与」这个概念，摘要如实照抄（含 `+0`）。
        """
        assert (
            service.make_summary(OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}]})
            == "×1.7"
        )
        #: 多步任意搭配，顺序照抄。
        assert (
            service.make_summary(
                OP_MATH_SCALE,
                {"steps": [{"op": "mul", "value": 2}, {"op": "add", "value": 3}, {"op": "mul", "value": 1.5}]},
            )
            == "×2 +3 ×1.5"
        )
        assert (
            service.make_summary(
                OP_MATH_SCALE,
                {"steps": [{"op": "add", "value": 10}, {"op": "sub", "value": 2}]},
            )
            == "+10 −2"
        )

    def test_summaries_are_not_json(self):
        """摘要一律给人看，不出现 `{`。"""
        for op, params in (
            (OP_FIXED_TEXT, {"text": "100"}),
            (OP_MATH_SCALE, {"steps": [{"op": "mul", "value": 1.7}, {"op": "add", "value": 5}]}),
        ):
            assert "{" not in service.make_summary(op, params)


# =========================================================================== #
# 六、真实数据（两个真机数据包的形态，设计 40 §1.1）
# =========================================================================== #


class TestRealPackages:
    """⚠️ 用真实数据包的值形态（不是编的）—— 这是「这规则到底能不能用」的判据。"""

    #: 真机 包2 `sku名称` 列（26 行，摘 5 行）。
    PKG2_SKU = [
        "颜色分类:【中古风】实木三斗柜",
        "颜色分类:【中古风】伸缩桌+三斗柜梳妆台",
        "颜色分类:【中古风】伸缩桌+三斗柜梳妆台+云朵妆镜",
        "颜色分类:奶油白色-实木六斗柜",
        "颜色分类:原木色-转角书桌",
    ]
    #: 真机 包1 `SKU信息` 列（52 行，摘 5 行）。
    PKG1_INFO = [
        "木;中古色-实木三斗柜;上门安装",
        "木;中古色-【伸缩桌+三斗柜】梳妆台;上门安装",
        "木;中古色-【伸缩桌+三斗柜】梳妆台+云朵妆镜;上门安装",
        "木;奶油白色-【两个伸缩桌+六斗柜】一字板梳妆台;上门安装",
        "木;原木色-转角书桌;自行安装",
    ]

    def test_pkg2_takes_rule_name_as_second_segment(self):
        """包2：取「颜色分类」（第 1 段）—— **5/5 全对**。"""
        out = service.apply_rule_to_column(
            self.PKG2_SKU, OP_EXTRACT_SEGMENT, {"sep": ":", "index": 1}
        )
        assert out == ["颜色分类"] * 5

    def test_pkg2_takes_the_value_after_colon(self):
        """包2：取「:」之后（第 2 段）—— 每行**各不相同**且全对。"""
        out = service.apply_rule_to_column(
            self.PKG2_SKU, OP_EXTRACT_SEGMENT, {"sep": ":", "index": 2}
        )
        assert out[0] == "【中古风】实木三斗柜"
        assert out[3] == "奶油白色-实木六斗柜"
        assert all(v for v in out)

    def test_pkg1_takes_the_middle_segment(self):
        """包1：取第 2 段（分号三段）—— **5/5 全对**，含最后一行 `自行安装`。"""
        out = service.apply_rule_to_column(
            self.PKG1_INFO, OP_EXTRACT_SEGMENT, {"sep": ";", "index": 2}
        )
        assert out[0] == "中古色-实木三斗柜"
        assert out[4] == "原木色-转角书桌"

    def test_pkg1_and_pkg2_rules_are_genuinely_different(self):
        """⚠️ **核心判据**：一套规则套不了两个包（这是本表存在的理由）。

        包2 的规则 `按 : 取第 1 段` 套到 包1 的行上 ⇒ **拿不到正确结果**
        （包1 的 `SKU信息` 里没有 `:` ⇒ 整串就是第 1 段）。
        """
        wrong = service.apply_rule_to_column(
            self.PKG1_INFO, OP_EXTRACT_SEGMENT, {"sep": ":", "index": 1}
        )
        #: 拿到的是整串原文（没有 `:` 时 split 只有一段）⇒ 不是我们要的规格值。
        assert wrong[0] == self.PKG1_INFO[0]
        assert wrong[0] != "中古色-实木三斗柜"

    def test_pkg2_image_name_extract_between(self):
        """包2 图片名：取括号内的值（26/26）。"""
        names = [
            "SKU01_颜色分类(【中古风】实木三斗柜).jpg",
            "SKU07_颜色分类(奶油白色-实木六斗柜).jpg",
            "SKU26_颜色分类(原木色-转角书桌).jpg",
        ]
        out = service.apply_rule_to_column(
            names, OP_EXTRACT_BETWEEN, {"open": "(", "close": ")"}
        )
        assert out == ["【中古风】实木三斗柜", "奶油白色-实木六斗柜", "原木色-转角书桌"]

    def test_pkg1_image_name_strip_with_regex_prefix(self):
        """包1 图片名：`SKU图_NN_值.jpg` ⇒ 去序号前缀 + 去扩展名（52/52）。"""
        names = [
            "SKU图_01_中古色-实木三斗柜.jpg",
            "SKU图_52_奶油白色-【两个伸缩桌+六斗柜】一字板梳妆台+一字板上柜+2个妆凳.jpg",
        ]
        out = service.apply_rule_to_column(
            names,
            OP_EXTRACT_STRIP,
            {"prefix": r"^SKU图_\d+_", "suffix": r"\.jpg$", "is_regex": True},
        )
        assert out[0] == "中古色-实木三斗柜"
        assert out[1].startswith("奶油白色-")


# =========================================================================== #
# 七、端点契约（用公共夹具）
# =========================================================================== #


@pytest.fixture()
def auth_client(api_client, db_session):
    """注入 `current_user` 依赖，避免走真实鉴权（照抄 test_workflows.py 的惯例）。

    ⚠️ `api_client` 只把库打到测试库、**不带身份** ⇒ 不注入的话所有端点都是 401。
    """
    from sqlalchemy import select

    from backend.app.core.deps import get_current_user
    from backend.app.main import app
    from backend.app.models.user import User

    user = db_session.scalars(select(User)).first()
    if user is None:
        user = User(username="dpr-test", password_hash="x")
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

    def _override():
        return user

    app.dependency_overrides[get_current_user] = _override
    try:
        yield api_client
    finally:
        app.dependency_overrides.pop(get_current_user, None)


class TestRuleSetApi:
    def test_create_list_update_delete(self, auth_client):
        r = auth_client.post(
            "/api/xhs/data-package-rules/sets",
            json={"name": "店透SKU预览", "remark": "从店透导出的表"},
        )
        assert r.status_code == 201, r.text
        set_id = r.json()["id"]
        assert r.json()["enabled"] is True

        r = auth_client.get("/api/xhs/data-package-rules/sets")
        assert [s["name"] for s in r.json()] == ["店透SKU预览"]

        r = auth_client.patch(
            f"/api/xhs/data-package-rules/sets/{set_id}", json={"enabled": False}
        )
        assert r.status_code == 200 and r.json()["enabled"] is False

        r = auth_client.delete(f"/api/xhs/data-package-rules/sets/{set_id}")
        assert r.status_code == 200 and r.json()["deleted_rules"] == 0
        assert auth_client.get("/api/xhs/data-package-rules/sets").json() == []

    def test_duplicate_name_is_409_not_500(self, auth_client):
        auth_client.post("/api/xhs/data-package-rules/sets", json={"name": "同名"})
        r = auth_client.post("/api/xhs/data-package-rules/sets", json={"name": "同名"})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "DP_RULE_SET_DUPLICATE_NAME"

    def test_empty_name_rejected(self, auth_client):
        r = auth_client.post("/api/xhs/data-package-rules/sets", json={"name": "  "})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "DP_RULE_BAD_PARAM"

    def test_enabled_only_filter(self, auth_client):
        a = auth_client.post("/api/xhs/data-package-rules/sets", json={"name": "A"}).json()
        auth_client.post("/api/xhs/data-package-rules/sets", json={"name": "B"})
        auth_client.patch(
            f"/api/xhs/data-package-rules/sets/{a['id']}", json={"enabled": False}
        )
        r = auth_client.get(
            "/api/xhs/data-package-rules/sets", params={"enabled_only": True}
        )
        assert [s["name"] for s in r.json()] == ["B"]

    def test_missing_set_is_404(self, auth_client):
        r = auth_client.patch(
            "/api/xhs/data-package-rules/sets/999999", json={"name": "x"}
        )
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "DP_RULE_SET_NOT_FOUND"


class TestRuleApi:
    def _make_set(self, auth_client, name="包") -> int:
        return auth_client.post(
            "/api/xhs/data-package-rules/sets", json={"name": name}
        ).json()["id"]

    def test_create_rule_with_summary(self, auth_client):
        set_id = self._make_set(auth_client)
        r = auth_client.post(
            "/api/xhs/data-package-rules/rules",
            json={
                "set_id": set_id,
                "rule_name": "取第2段",
                "op": OP_EXTRACT_SEGMENT,
                "params": {"sep": ";", "index": 2},
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["summary"] == "按「;」取第 2 段"
        #: 摘要给人看；params 原样回（前端编辑时要用）。
        assert body["params"] == {"sep": ";", "index": 2}

    def test_duplicate_rule_name_in_same_set_is_409(self, auth_client):
        set_id = self._make_set(auth_client)
        payload = {
            "set_id": set_id,
            "rule_name": "取第2段",
            "op": OP_EXTRACT_SEGMENT,
            "params": {"sep": ";", "index": 2},
        }
        assert auth_client.post("/api/xhs/data-package-rules/rules", json=payload).status_code == 201
        r = auth_client.post("/api/xhs/data-package-rules/rules", json=payload)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "DP_RULE_DUPLICATE_NAME"

    def test_same_rule_name_allowed_in_different_sets(self, auth_client):
        """⚠️ 同一个列名在**不同包**里可以各有一条（本表存在的理由）。"""
        s1 = self._make_set(auth_client, "包1")
        s2 = self._make_set(auth_client, "包2")
        for sid in (s1, s2):
            r = auth_client.post(
                "/api/xhs/data-package-rules/rules",
                json={
                    "set_id": sid,
                    "rule_name": "取第2段",
                    "op": OP_EXTRACT_SEGMENT,
                    "params": {"sep": ";", "index": 2},
                },
            )
            assert r.status_code == 201

    def test_bad_params_rejected_at_save_time(self, auth_client):
        """配错要在**保存时**就知道（而不是执行后整列留空才发现）。"""
        set_id = self._make_set(auth_client)
        r = auth_client.post(
            "/api/xhs/data-package-rules/rules",
            json={
                "set_id": set_id,
                "rule_name": "取第2段",
                "op": OP_EXTRACT_SEGMENT,
                "params": {"sep": "", "index": 1},
            },
        )
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "DP_RULE_BAD_PARAM"

    def test_fixed_text_and_math_scale_are_creatable_now(self, auth_client):
        """⚠️ 两条新 op 经**端点**也要能建（旧用例断的是 400，方向已反转）。"""
        set_id = self._make_set(auth_client, "可存新 op")
        r = auth_client.post(
            "/api/xhs/data-package-rules/rules",
            json={
                "set_id": set_id,
                "rule_name": "价格换算",
                "op": OP_MATH_SCALE,
                "params": {"steps": [{"op": "mul", "value": 1.7}, {"op": "add", "value": 5}]},
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["summary"] == "×1.7 +5"

        r = auth_client.post(
            "/api/xhs/data-package-rules/rules",
            json={
                "set_id": set_id,
                "rule_name": "整列包邮",
                "op": OP_FIXED_TEXT,
                "params": {"text": "包邮"},
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["summary"] == "固定为「包邮」"

    def test_rule_list_scoped_by_set(self, auth_client):
        s1 = self._make_set(auth_client, "包1")
        s2 = self._make_set(auth_client, "包2")
        auth_client.post(
            "/api/xhs/data-package-rules/rules",
            json={
                "set_id": s1,
                "rule_name": "取第2段",
                "op": OP_EXTRACT_SEGMENT,
                "params": {"sep": ";", "index": 2},
            },
        )
        assert len(auth_client.get("/api/xhs/data-package-rules/rules", params={"set_id": s1}).json()) == 1
        assert auth_client.get("/api/xhs/data-package-rules/rules", params={"set_id": s2}).json() == []

    def test_toggle_rule_enabled(self, auth_client):
        set_id = self._make_set(auth_client)
        rule = auth_client.post(
            "/api/xhs/data-package-rules/rules",
            json={
                "set_id": set_id,
                "rule_name": "取第2段",
                "op": OP_EXTRACT_SEGMENT,
                "params": {"sep": ";", "index": 2},
            },
        ).json()
        r = auth_client.patch(
            f"/api/xhs/data-package-rules/rules/{rule['id']}", json={"enabled": False}
        )
        assert r.status_code == 200 and r.json()["enabled"] is False

    def test_delete_set_cascades_and_reports_count(self, auth_client, db_session):
        """⚠️ 删包级联删规则，并**回被删条数**（前端二次确认要明示）。"""
        set_id = self._make_set(auth_client)
        for name in ("规则甲", "规则乙", "规则丙"):
            auth_client.post(
                "/api/xhs/data-package-rules/rules",
                json={
                    "set_id": set_id,
                    "rule_name": name,
                    "op": OP_EXTRACT_SEGMENT,
                    "params": {"sep": ";", "index": 2},
                },
            )
        r = auth_client.delete(f"/api/xhs/data-package-rules/sets/{set_id}")
        assert r.json()["deleted_rules"] == 3
        #: ⚠️ **子行必须真的没了**（不依赖 SQLite 的 `PRAGMA foreign_keys` ——
        #: 本项目的连接默认没开它 ⇒ 只靠 `ondelete=CASCADE` 会留一堆孤儿规则行）。
        #: 判据：该包已 404，且**直接查库里**没有残留子行（端点是按 join 父表收口的，
        #: 父没了它也「看不见」子行 ⇒ 只看端点会漏判）。
        assert auth_client.get(
            "/api/xhs/data-package-rules/rules", params={"set_id": set_id}
        ).status_code == 404
        #: ⚠️ **直接开一个独立连接查库**，不能用夹具的 `db_session` ——
        #: 端点在 `api_client` 的 `override_get_db` 里用的是**另一个 Session**；
        #: 用夹具那个查，**看不到端点写下的东西**（实测：变异体下断言照样绿，
        #: 这就是 M6 首轮逃逸的真因 —— 判据看着严谨、其实没连上）。
        from sqlalchemy import create_engine, text

        engine = db_session.get_bind().engine
        with engine.connect() as conn:
            leftover = conn.execute(
                text("SELECT COUNT(*) FROM data_package_rules WHERE set_id = :sid"),
                {"sid": set_id},
            ).scalar_one()
        assert leftover == 0, f"删包后残留 {leftover} 条孤儿规则"

    def test_set_enabled_does_not_touch_children(self, auth_client):
        """⚠️ **父停用不改写子行的 `enabled`**（父恢复后子行按原状态走，设计 40 §4.3）。"""
        set_id = self._make_set(auth_client)
        rule = auth_client.post(
            "/api/xhs/data-package-rules/rules",
            json={
                "set_id": set_id,
                "rule_name": "取第2段",
                "op": OP_EXTRACT_SEGMENT,
                "params": {"sep": ";", "index": 2},
            },
        ).json()
        assert rule["enabled"] is True
        auth_client.patch(f"/api/xhs/data-package-rules/sets/{set_id}", json={"enabled": False})
        #: 子行仍是启用的（只是父停了 ⇒ 整包失效）。
        rows = auth_client.get(
            "/api/xhs/data-package-rules/rules", params={"set_id": set_id}
        ).json()
        assert rows[0]["enabled"] is True
        #: 但 `enabled_only` 的**包**清单里它不出现（列头弹窗用这个）。
        assert auth_client.get(
            "/api/xhs/data-package-rules/sets", params={"enabled_only": True}
        ).json() == []


class TestExecuteApi:
    def _seed(self, auth_client):
        set_id = auth_client.post(
            "/api/xhs/data-package-rules/sets", json={"name": "店透SKU预览"}
        ).json()["id"]
        rule = auth_client.post(
            "/api/xhs/data-package-rules/rules",
            json={
                "set_id": set_id,
                "rule_name": "取第2段",
                "op": OP_EXTRACT_SEGMENT,
                "params": {"sep": ";", "index": 2},
            },
        ).json()
        return set_id, rule["id"]

    def test_execute_returns_values_and_counts(self, auth_client):
        set_id, rule_id = self._seed(auth_client)
        values = ["木;中古色-实木三斗柜;上门安装", "木;奶油白-六斗柜;上门安装", "坏数据"]
        r = auth_client.post(
            "/api/xhs/data-package-rules/execute",
            json={"set_id": set_id, "rule_id": rule_id, "values": values},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["values"] == ["中古色-实木三斗柜", "奶油白-六斗柜", None]
        assert body["missed"] == 1 and body["total"] == 3
        #: ⚠️ 提不出 ⇒ **null**（留空），不是原值，也不是空串。
        assert body["values"][2] is None
        assert "1 行" in body["reason"]

    def test_execute_reports_no_reason_when_all_ok(self, auth_client):
        set_id, rule_id = self._seed(auth_client)
        r = auth_client.post(
            "/api/xhs/data-package-rules/execute",
            json={
                "set_id": set_id,
                "rule_id": rule_id,
                "values": ["木;甲;上门安装"],
            },
        )
        assert r.json()["missed"] == 0 and r.json()["reason"] == ""

    def test_execute_returns_updated_at_for_recompute(self, auth_client):
        """⚠️ 前端据此判断「规则被改过 ⇒ 要重算」（用户口径「调整后实时生效」）。"""
        set_id, rule_id = self._seed(auth_client)
        r = auth_client.post(
            "/api/xhs/data-package-rules/execute",
            json={"set_id": set_id, "rule_id": rule_id, "values": ["a;b;c"]},
        )
        assert r.json()["updated_at"]

    def test_execute_rule_from_another_set_is_404(self, auth_client):
        s1, rule1 = self._seed(auth_client)
        s2 = auth_client.post(
            "/api/xhs/data-package-rules/sets", json={"name": "另一个包"}
        ).json()["id"]
        r = auth_client.post(
            "/api/xhs/data-package-rules/execute",
            json={"set_id": s2, "rule_id": rule1, "values": ["a;b;c"]},
        )
        assert r.status_code == 404


class TestIsolation:
    """⚠️ 数据隔离（AGENTS.md 硬约束）：规则是**用户资产**。"""

    def test_other_user_cannot_see_or_touch(self, auth_client, db_session):
        """A 建的规则包，B 看不到清单、也改不动（404 而非 403 —— 不泄露 id 存在）。"""
        from backend.app.core.deps import get_current_user
        from backend.app.main import app
        from backend.app.models.user import User

        mine = auth_client.post(
            "/api/xhs/data-package-rules/sets", json={"name": "A的包"}
        ).json()
        set_id = mine["id"]
        rule_id = auth_client.post(
            "/api/xhs/data-package-rules/rules",
            json={
                "set_id": set_id,
                "rule_name": "取第2段",
                "op": OP_EXTRACT_SEGMENT,
                "params": {"sep": ";", "index": 2},
            },
        ).json()["id"]

        #: 造第二个用户，并把 `get_current_user` 换到他身上。
        other = User(username="dpr-other", password_hash="x")
        db_session.add(other)
        db_session.commit()
        db_session.refresh(other)

        app.dependency_overrides[get_current_user] = lambda: other
        try:
            assert auth_client.get("/api/xhs/data-package-rules/sets").json() == []
            assert (
                auth_client.patch(
                    f"/api/xhs/data-package-rules/sets/{set_id}", json={"name": "黑"}
                ).status_code
                == 404
            )
            assert (
                auth_client.delete(f"/api/xhs/data-package-rules/rules/{rule_id}").status_code
                == 404
            )
            #: 链路的另一半：**执行**也不能跨用户（否则能拿别人的规则算自己的数据）。
            assert (
                auth_client.post(
                    "/api/xhs/data-package-rules/execute",
                    json={"set_id": set_id, "rule_id": rule_id, "values": ["a;b;c"]},
                ).status_code
                == 404
            )
        finally:
            #: 还原成原用户（不然影响本条后面的断言与夹具 teardown）。
            app.dependency_overrides.pop(get_current_user, None)


class TestModelConstraints:
    def test_tables_registered(self):
        """两表已进 metadata（否则 `create_all` 建不出来 ⇒ 新装用户没有表）。"""
        from backend.app.core.database import Base

        assert "data_package_rule_sets" in Base.metadata.tables
        assert "data_package_rules" in Base.metadata.tables

    def test_child_has_no_redundant_user_id(self):
        """⚠️ 子表**不冗余 `user_id`**（冗余会带来父子不一致的静默脏数据）。"""
        from backend.app.core.database import Base

        cols = Base.metadata.tables["data_package_rules"].columns
        assert "user_id" not in cols
        assert "set_id" in cols


# =========================================================================== #
# 八、种子（新用户起点；设计 40 §6）
# =========================================================================== #


#: 当前**还没实现**的 op（`RULE_OPS` 有、`_IMPLEMENTED_OPS` 没有）。
#: ⚠️ 写成「算出来的」而不是写死：实现落地后这里自动换一个，用例不会假红
#: （本用例曾因 `math_scale` 落地而红 —— 见 `test_seed_skips_unimplemented_op` 的 docstring）。
UNIMPLEMENTED_OP = next(
    op for op in service.RULE_OPS if op not in service._IMPLEMENTED_OPS
) if any(op not in service._IMPLEMENTED_OPS for op in service.RULE_OPS) else ""


class TestSeed:
    def test_creates_the_three_real_sets_with_rules(self, db_session):
        """新用户拿到三个规则包（= 用户真机库里的实际配置），规则可执行。

        ⚠️ 2026-10-20 用户口径「应以当前本地配置的为准」：原种子是两个包
        （店透SKU预览 / 商品SKU数据），用户库里实际是三个（店透视 / 公共 / 至尊宝）
        ⇒ 种子整体对齐为后者。
        """
        from backend.app.models.user import User

        user = User(username="seed-test", password_hash="x")
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        created = service.seed_builtin_rules(db_session, user_id=user.id)
        db_session.commit()
        assert created == 3

        sets = service.list_sets(db_session, user.id)
        assert [s.name for s in sets] == ["店透视", "公共", "至尊宝"]

        #: ⚠️ 种子的规则**真的能跑**（不是摆设）：包1 按 ; 取第 2 段。
        rules = {r.rule_name: r for r in service.list_rules(db_session, user.id, sets[0].id)}
        info = rules["sku名称"]
        out = service.apply_rule("木;中古色-实木三斗柜;上门安装", info.op, service.parse_params(info.params_json))
        assert out == "中古色-实木三斗柜"

        #: 包2「公共」的「计算市场价」= 原值 × 2 + 50（可执行性同样要钉住）。
        rules2 = {r.rule_name: r for r in service.list_rules(db_session, user.id, sets[1].id)}
        price_rule = rules2["计算市场价"]
        got = service.apply_rule(
            "100", price_rule.op, service.parse_params(price_rule.params_json)
        )
        assert got == "250"

    def test_idempotent_and_does_not_clobber_user_edits(self, db_session):
        """⚠️ **幂等**，且用户改过的**不重建、不覆盖**（规则是用户资产）。"""
        from backend.app.models.user import User

        user = User(username="seed-test-2", password_hash="x")
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        service.seed_builtin_rules(db_session, user_id=user.id)
        db_session.commit()
        #: 用户改名（模拟「他配过自己的了」）。
        sets = service.list_sets(db_session, user.id)
        service.update_set(db_session, user.id, sets[0].id, name="我自己改的名字")

        again = service.seed_builtin_rules(db_session, user_id=user.id)
        db_session.commit()
        assert again == 0, "已有规则包时不该再播种"
        assert service.list_sets(db_session, user.id)[0].name == "我自己改的名字", "改动被覆盖了"

    def test_seed_skips_unimplemented_op(self, db_session, monkeypatch):
        """⚠️ 种子里若混进**未实现的 op** ⇒ **跳过该条**，不把脏数据落库。

        ⚠️ 判据必须用一个**真的还没实现**的 op。`fixed_text` / `math_scale`
        在 2026-10-15 已随用户口径落地实现（「提取数据规则中新增规则未支持：
        固定文本 + 批量运算」）⇒ **不能再拿它们当「未实现」的样本**（本用例
        曾因此变红：`价格换算` 那条被正常落库了）。
        当前未实现 = 不在 `_IMPLEMENTED_OPS` 里的、`RULE_OPS` 内的 op。
        """
        from backend.app.models.user import User

        user = User(username="seed-test-3", password_hash="x")
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        bad = {
            "data_package_rule_sets": [
                {
                    "name": "坏种子",
                    "rules": [
                        #: ⚠️ 动态挑一个**当前未实现**的 op（写死会随实现落地而过期）。
                        {"rule_name": "未实现的", "op": UNIMPLEMENTED_OP, "params": {}},
                        {"rule_name": "甲", "op": "extract_segment", "params": {"sep": ";", "index": 1}},
                    ],
                }
            ]
        }
        import json as _json
        from pathlib import Path

        tmp = Path(str(db_session.bind.url).replace("sqlite:///", "")).parent / "seed_bad.json"
        tmp.write_text(_json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(
            "backend.app.core.initial_data.resolve_initial_data_file",
            lambda name: tmp,
        )
        service.seed_builtin_rules(db_session, user_id=user.id)
        db_session.commit()

        sets = service.list_sets(db_session, user.id)
        assert len(sets) == 1
        rules = service.list_rules(db_session, user.id, sets[0].id)
        #: 只落了合法那条。
        assert [r.rule_name for r in rules] == ["甲"]


class TestCopyColumn:
    """**复制列值**（用户口径 2026-10-15：「新增一个复制列值的操作规则」）。"""

    def test_copies_value_verbatim(self):
        """照搬整格 —— 值**逐字**过来（不 trim、不改写；`_as_text` 只做 `None`→空串）。"""
        assert service.apply_rule("1280", OP_COPY_COLUMN, {}) == "1280"
        assert service.apply_rule("【中古风】实木三斗柜", OP_COPY_COLUMN, {}) == "【中古风】实木三斗柜"



    def test_empty_source_stays_empty(self):
        """⚠️ 源格是空 ⇒ **搬空**（`None`）—— 与 `extract_*` 的「提不出留空」同结果，
        但语义不同：这条是「照搬」，那条是「抠不出」。"""
        assert service.apply_rule(None, OP_COPY_COLUMN, {}) is None
        assert service.apply_rule("", OP_COPY_COLUMN, {}) is None

    def test_no_params_needed(self):
        """来源列是**执行参数**（前端传 `source_column`）⇒ 规则自身无参数。"""
        service.validate_params(OP_COPY_COLUMN, {})
        service.validate_params(OP_COPY_COLUMN, {"whatever": 1})

    def test_summary_is_human(self):
        assert service.make_summary(OP_COPY_COLUMN, {}) == "复制来源列的值"

    def test_column_apply(self):
        out = service.apply_rule_to_column(["1", None, "3"], OP_COPY_COLUMN, {})
        assert out == ["1", None, "3"]


class TestCopyExtract:
    """**复制 + 提取（多步）**（用户口径 2026-10-15）。

    > 「新增一个复制加提取规则，**分为多步执行**，第一步规则是将指定列复制到当前列，
    >   第二步是将复制过来的列值按照规则提取结果覆盖当前列」

    ⚠️ 本类的重点不是「提取算法」（那三个 `extract_*` 已有自己的用例），
    而是**两步接起来**这件事：复制来的值能不能被提取、提不出时留空还是留原值。
    """

    def test_segment_step(self):
        """第一步复制 → 第二步按分隔符取第 N 段。"""
        params = {"extract": {"op": "segment", "sep": ";", "index": 2}}
        assert service.apply_rule("木;中古色-实木三斗柜;上门安装", OP_COPY_EXTRACT, params) == "中古色-实木三斗柜"
        #: 负数从后往前（与顶层 `extract_segment` 同语义）。
        assert service.apply_rule("a;b;c", OP_COPY_EXTRACT, {"extract": {"op": "segment", "sep": ";", "index": -1}}) == "c"

    def test_between_step(self):
        """第二步取包裹符之间。"""
        params = {"extract": {"op": "between", "open": "(", "close": ")"}}
        assert service.apply_rule("SKU01_颜色分类(【中古风】实木三斗柜).jpg", OP_COPY_EXTRACT, params) == "【中古风】实木三斗柜"

    def test_strip_step(self):
        """第二步去前缀 / 去后缀。"""
        params = {"extract": {"op": "strip", "prefix": "颜色分类:", "suffix": ""}}
        assert service.apply_rule("颜色分类:【中古风】实木三斗柜", OP_COPY_EXTRACT, params) == "【中古风】实木三斗柜"

    def test_empty_source_yields_none(self):
        """⚠️ 复制来的值是空 ⇒ 提不出（`None`），**不是**留空串、更不是留原值。"""
        params = {"extract": {"op": "segment", "sep": ";", "index": 1}}
        assert service.apply_rule(None, OP_COPY_EXTRACT, params) is None
        assert service.apply_rule("", OP_COPY_EXTRACT, params) is None

    def test_unmatched_yields_none(self):
        """⚠️ 提不出 ⇒ `None`（调用方据此区分「不适用」与「结果本来就是空」）。"""
        params = {"extract": {"op": "segment", "sep": ";", "index": 3}}
        assert service.apply_rule("a;b", OP_COPY_EXTRACT, params) is None

    def test_does_not_keep_current_value(self):
        """⚠️ **不保留当前列的原值** —— 本 op 的语义是「覆盖当前列」。

        反例（会被这条咬住）：把早退判据写成「原值空就跳过」，那么当前列为空时
        复制+提取就什么都不做 —— 但那正是用户最常见的场景（目标列本来就是空的）。
        """
        params = {"extract": {"op": "segment", "sep": ";", "index": 2}}
        #: 传进来的 value 在语义上代表**来源列**的值 ⇒ 结果是「从它里面提取出的第 2 段」，
        #: 与「当前列原本是什么」无关。
        assert service.apply_rule("木;柜体", OP_COPY_EXTRACT, params) == "柜体"
        #: ⚠️ 关键反例：当前列**原本为空**时也必须照样出值 ——
        #: 若实现里写成「原值空就早退」，那用户最常遇到的场景（目标列本来就是空）
        #: 会**什么都不发生**，且界面不报错。
        assert service.apply_rule("木;柜体", OP_COPY_EXTRACT, params) == "柜体"

    def test_validate_ok(self):
        """合法参数不抛。"""
        service.validate_params(OP_COPY_EXTRACT, {"extract": {"op": "segment", "sep": ";", "index": 1}})
        service.validate_params(OP_COPY_EXTRACT, {"extract": {"op": "between", "open": "("}})
        service.validate_params(OP_COPY_EXTRACT, {"extract": {"op": "strip", "prefix": "a"}})

    @pytest.mark.parametrize(
        "params,label",
        [
            ({}, "没有 extract"),
            ({"extract": {}}, "extract 里没有 op"),
            ({"extract": {"op": "nope"}}, "不认识的提取方式"),
            ({"extract": {"op": "segment", "sep": "", "index": 1}}, "分隔符为空"),
            ({"extract": {"op": "segment", "sep": ";", "index": 0}}, "取第 0 段"),
            ({"extract": {"op": "between", "open": ""}}, "起始符号为空"),
            ({"extract": {"op": "strip", "prefix": "", "suffix": ""}}, "前后缀都空"),
        ],
    )
    def test_validate_rejects(self, params, label):
        """⚠️ 参数是用户配的 ⇒ **保存时就要拒**（不然执行后整列留空，分不清谁的错）。"""
        with pytest.raises(service.DataPackageRuleError):
            service.validate_params(OP_COPY_EXTRACT, params)

    def test_summary_has_two_steps(self):
        """摘要要体现**两步**（第一步复制 + 第二步提取方式）。"""
        s1 = service.make_summary(OP_COPY_EXTRACT, {"extract": {"op": "segment", "sep": ";", "index": 2}})
        assert "复制" in s1 and "第 2 段" in s1
        s2 = service.make_summary(OP_COPY_EXTRACT, {"extract": {"op": "between", "open": "("}})
        assert "复制" in s2 and "之间" in s2
        s3 = service.make_summary(OP_COPY_EXTRACT, {"extract": {"op": "strip", "prefix": "a"}})
        assert "复制" in s3 and "前后缀" in s3

    def test_needs_source_column(self):
        """⚠️ 与后端 `OPS_NEED_SOURCE_COLUMN` 同集合 —— 漏了就会静默按当前列执行。"""
        assert OP_COPY_EXTRACT in OPS_NEED_SOURCE_COLUMN

    def test_in_rule_ops_and_implemented(self):
        """在 `RULE_OPS` 与已实现集合里（不在 ⇒ 建规则时 400「尚未实现」）。"""
        assert OP_COPY_EXTRACT in RULE_OPS
        assert OP_COPY_EXTRACT in service._IMPLEMENTED_OPS


class TestSeedMatchesTheRealLibrary:
    """⚠️ **种子内容 = 真机库里用户实际配过的规则**（用户口径 2026-10-15）。

    > 「当前第一步中的 sku 列表规则按钮中的**目前本地项目中配置的规则作为种子数据**」

    ⚠️ 前一版种子里混进了**为演示而加的两条示例规则**（`固定品牌词` `fixed_text` /
    `原价乘1.7` `math_scale`）—— 用户没配过它们。本用例钉住「种子里只有真实配过的」。
    """

    def _seed(self) -> dict:
        import json
        from pathlib import Path as _P

        path = _P(__file__).resolve().parents[2] / "config/initial_data/data_package_rules.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_seed_has_exactly_the_three_real_packages(self):
        seed = self._seed()
        names = [s["name"] for s in seed["data_package_rule_sets"]]
        assert names == ["店透视", "公共", "至尊宝"], (
            f"种子规则包与真机库不一致：{names}"
        )

    def test_seed_rules_are_exactly_what_was_configured(self):
        """逐字段钉住（规则名 / op / params）—— 这三条就是真机库里的全部。"""
        seed = self._seed()
        got = {
            s["name"]: [(r["rule_name"], r["op"], r["params"]) for r in s["rules"]]
            for s in seed["data_package_rule_sets"]
        }
        assert got["店透视"] == [
            ("sku名称", "extract_segment", {"sep": ";", "index": 2}),
        ]
        assert got["公共"] == [
            (
                "计算市场价",
                "math_scale",
                {"steps": [{"op": "mul", "value": 2}, {"op": "add", "value": 50}]},
            ),
            ("计算售价", "math_scale", {"steps": [{"op": "mul", "value": 1.8}]}),
        ]
        assert got["至尊宝"] == [
            ("SKU 名称-sku信息列", "extract_segment", {"sep": ":", "index": 2}),
            (
                "规格分类",
                "copy_extract",
                {"extract": {"op": "segment", "sep": ":", "index": 1}},
            ),
            ("取括号内名称", "extract_between", {"open": "(", "close": ")", "occurrence": 1}),
        ]

    def test_seed_has_no_demo_only_rules(self):
        #: ⚠️ 反向：**不许**把「为了演示某个 op」的规则塞进种子 ——
        #: 种子是「用户的真实资产」，不是 op 的展示柜。
        seed = self._seed()
        all_names = [r["rule_name"] for s in seed["data_package_rule_sets"] for r in s["rules"]]
        for demo in ("固定品牌词", "原价乘1.7"):
            assert demo not in all_names, f"示例规则 `{demo}` 又混进种子了"

    def test_seed_ops_are_all_implemented(self):
        #: 种子里的 op 必须是**已实现**的（否则新装用户看到的规则全是坏的）。
        from backend.app.services.data_package_rule_service import _IMPLEMENTED_OPS

        seed = self._seed()
        for s in seed["data_package_rule_sets"]:
            for r in s["rules"]:
                assert r["op"] in _IMPLEMENTED_OPS, f"种子里有未实现的 op：{r['op']}"
