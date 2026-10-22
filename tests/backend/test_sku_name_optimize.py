"""SKU 规格名称超长压缩（用户口径 2026-10-20）的守卫。

用户原话：

> 「导入 sku 数据 / 导入 sku 预检通过的数据时，如果发现 sku 名称超过 30 字
>   （这个数值配置的设置中动态调整实时生效），则调用大模型将标题优化为 30 字以内，
>   文本内容意思不可改变，优化后使用弹窗展示优化前后的对应以供用户预览比较；
>   如果不满意可单独对一个 sku 规格名称重新生成并覆盖历史值」
>
> 补充：「30 字 60 个字符」「以 60 字符为准」

本文件钉住四件事：

1. **长度口径**（汉字计 2、ASCII 计 1）—— 前后端必须同一套，否则静默错位；
2. **触发判据**是「达到即触发」（`>=`），不是「超过」（`>`）；
3. **语义保护**：模型压不够时本地截断并标 `truncated`；失败项退回原文带原因；
4. **配置实时生效**：上限读 `xhs_sku_name_max_chars`（`LIVE_KEYS`），不写死。
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from backend.app.services import sku_name_optimize_service as svc


# ---------------------------------------------------------------- 长度口径


class TestDisplayWidth:
    def test_chinese_counts_as_two(self):
        #: 「中古色」3 个汉字 = 6 宽度（用 len 会得到 3）。
        assert svc.display_width("中古色") == 6

    def test_ascii_counts_as_one(self):
        assert svc.display_width("ABC-123") == 7

    def test_mixed_width(self):
        #: A(1) + 中(2) + B(1) = 4
        assert svc.display_width("A中B") == 4

    def test_empty(self):
        assert svc.display_width("") == 0

    def test_thirty_chinese_equals_sixty(self):
        """用户口径「30 字 60 个字符」的**直接断言**。"""
        name = "中" * 30
        assert svc.display_width(name) == 60

    def test_full_width_punctuation_counts_as_two(self):
        #: 全角括号（U+FF08）属宽字符。
        assert svc.display_width("（") == 2


class TestIsTooLong:
    def test_reaches_limit_does_not_trigger(self):
        """**恰好 60 不触发**（用户口径 2026-10-22：
        「小于 60 字符就不应该展示在 SKU 规格名称优化预览弹窗列表中」）。

        ⚠️ 原断言是 `is_too_long("中"*30, 60) is True`（「达到即触发」）——
        那条口径已被本轮取代：60 本身合法（平台原文「不能超过 30 个字（60 个字符）」）。
        """
        assert svc.is_too_long("中" * 30, 60) is False

    def test_over_limit_triggers(self):
        """**超过** 60 才触发 —— 这是弹窗列表的唯一入选判据。"""
        assert svc.is_too_long("中" * 31, 60) is True

    def test_below_limit_does_not_trigger(self):
        assert svc.is_too_long("中" * 29, 60) is False

    def test_default_is_sixty(self):
        assert svc.DEFAULT_MAX_CHARS == 60


class TestTruncateToWidth:
    def test_truncates_without_breaking_chars(self):
        out = svc.truncate_to_width("中" * 40, 59)
        assert svc.display_width(out) <= 59
        assert out == "中" * 29  # 29*2=58，再加一个就 60 超了

    def test_never_exceeds_limit(self):
        for limit in (1, 5, 59, 60):
            out = svc.truncate_to_width("中文abc" * 20, limit)
            assert svc.display_width(out) <= limit


# ---------------------------------------------------------------- 语义保护 / 失败不造假


class TestOptimizeNamesNoLlm:
    def test_short_names_returned_untouched(self):
        """没超长的项**不调模型**、原样返回（省额度，且行为可预测）。"""
        out = svc.optimize_names(db=None, user_id=1, items=[("k", "短名字")], max_chars=60)
        assert len(out) == 1
        assert out[0].changed is False
        assert out[0].optimized == out[0].original
        assert out[0].reason == ""

    def test_llm_unavailable_reports_reason_not_silent(self):
        """服务商不可用 ⇒ 带原因 + **新规格名留空**（不许拿原文冒充结果）。

        ⚠️ 用户口径 2026-10-22：「调用失败新规格名**应该还是空**不应该使用原规格名」。
        ⇒ 原断言 `out[0].optimized == long_name`（退回原文）已被取代：
        退回原文会让界面看起来「模型给了个和原来一样的名字」，
        用户分不清是失败还是没压动；留空 + `reason` 才是可区分的失败态。
        """
        long_name = "中" * 40  # 80 宽，触发
        out = svc.optimize_names(db=None, user_id=999999, items=[("k", long_name)], max_chars=60)
        assert len(out) == 1
        assert out[0].changed is False
        assert out[0].optimized == "", "失败时新规格名必须留空，不能退回原文"
        assert out[0].original == long_name, "原规格名要保留（用户要靠它手动改）"
        assert out[0].reason, "失败必须给出原因，不能空着"


# ---------------------------------------------------------------- 配置项接线


class TestSettingsWiring:
    def test_key_is_live(self):
        """键必须在 `LIVE_KEYS` 里 —— 否则设置页改了不生效（假「实时生效」）。"""
        from backend.app.services import settings_service

        assert "xhs_sku_name_max_chars" in settings_service.LIVE_KEYS

    def test_builtin_default_is_sixty(self):
        from backend.app.services import settings_service

        assert settings_service.BUILTIN_DEFAULTS["xhs_sku_name_max_chars"] == 60

    def test_default_matches_service_constant(self):
        """两处默认值必须同值（写两处是有意的冗余，漂移就是静默错值）。"""
        from backend.app.services import settings_service

        assert (
            settings_service.BUILTIN_DEFAULTS["xhs_sku_name_max_chars"]
            == svc.DEFAULT_MAX_CHARS
        )


# ---------------------------------------------------------------- 业务点


class TestBusinessKey:
    def test_business_key_exists(self):
        from backend.app.services import business_provider_keys as keys

        assert keys.BUSINESS_KEY_PRODUCT_SKU_NAME_OPTIMIZE.key == "product_sku_name_optimize"

    def test_business_key_registered(self):
        from backend.app.services import business_provider_keys as keys

        assert keys.BUSINESS_KEY_PRODUCT_SKU_NAME_OPTIMIZE in keys.BUSINESS_KEYS


# ---------------------------------------------------------------- 端点


class TestEndpoints:
    def test_routes_registered(self):
        from backend.app.api.platforms.xhs import package_precheck

        #: router 挂了 `prefix="/xhs/package-precheck"` ⇒ 取到的 path 是**完整**路径。
        paths = {r.path for r in package_precheck.router.routes}
        assert "/xhs/package-precheck/sku-name-optimize" in paths
        assert "/xhs/package-precheck/sku-name-regenerate" in paths

    def test_optimize_does_not_write_db(self):
        """**只影响本次导入**：服务层不得出现任何 commit / add（不写库）。"""
        src = io.open(
            Path(svc.__file__), encoding="utf-8"
        ).read()
        for banned in ("db.commit()", "db.add(", "session.commit()"):
            assert banned not in src, f"优化服务不应写库，但出现了 `{banned}`"
