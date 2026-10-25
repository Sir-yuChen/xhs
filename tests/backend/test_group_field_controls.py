"""配置表「分组」控件守卫（2026-10-25 用户口径）。

用户原话：「分组支持下拉加新增」→「相关的系统配置弹窗均保持相同逻辑」
→「相关的 config 表的分组逻辑保持新增或选择」。

判据落点：**分组字段必须是「既能选已有、又能输新值」的控件**。

⚠️ 为什么这不是「控件选型洁癖」：`Select` 只开 `showSearch` 时是**搜索已有项**，
搜不到就选不了 —— 而 placeholder 往往写着「选择或输入」，用户照着输入却发现
新增不了。这是实测发现的问题（提示词类别原本就是这样）。
`AutoComplete` / `Select mode="tags"` 才能真新增；本项目统一用前者
（分组是单值字符串，`tags` 的数组形态要靠 normalize 硬收敛，回显/清空/粘贴
都会出边界情况）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "src"

#: 三处「分组/类别」入口（用户口径「相关的都保持相同逻辑」）。
GROUP_FIELDS = [
    ("components/settings/settings-table.tsx", "分组"),
    ("pages/platforms/xhs/forbidden-words-modal.tsx", "分组"),
    ("pages/platforms/xhs/prompt-template-panel.tsx", "类别"),
]


def _form_item_span(text: str, label: str) -> str | None:
    """截出 label="<分组>" 那个 Form.Item 的整段源码。"""
    pattern = re.compile(
        r'<Form\.Item[^>]*?label="' + re.escape(label) + r'"[\s\S]*?</Form\.Item>'
    )
    match = pattern.search(text)
    return match.group(0) if match else None


def test_every_group_field_can_add_a_new_value():
    """每个分组字段都必须**能新增**（不能只是搜索已有项）。"""
    for rel, label in GROUP_FIELDS:
        text = (FRONTEND / rel).read_text(encoding="utf-8")
        span = _form_item_span(text, label)
        assert span, f"{rel} 里找不到「{label}」字段（判据前提不成立）"
        assert "<AutoComplete" in span or 'mode="tags"' in span, (
            f"{rel} 的「{label}」用了不能新增的控件 —— "
            "只开 showSearch 的 Select 搜不到就选不了，用户无法新建分组"
        )


def test_no_select_pretends_to_accept_new_values():
    """不许出现「placeholder 写着选择或输入、控件却新增不了」的组合。

    这条抓的是最坑的形态：**看起来能输入**，实际输完发现选不上。
    """
    for rel, label in GROUP_FIELDS:
        text = (FRONTEND / rel).read_text(encoding="utf-8")
        span = _form_item_span(text, label)
        if not span:
            continue
        if "选择或输入" in span or "输入新" in span:
            assert "<AutoComplete" in span or 'mode="tags"' in span, (
                f"{rel} 的「{label}」文案承诺可以输入新值，但控件做不到"
            )


def test_group_fields_share_one_control_shape():
    """三处必须用**同一种**控件形态（用户口径「保持相同逻辑」）。

    分散成 tags / AutoComplete / 裸 Select 三种，用户在三处得到三种交互，
    每处都要重新学一遍 —— 那正是用户要求统一的原因。
    """
    shapes = set()
    for rel, label in GROUP_FIELDS:
        text = (FRONTEND / rel).read_text(encoding="utf-8")
        span = _form_item_span(text, label)
        assert span, f"{rel} 缺「{label}」字段"
        if "<AutoComplete" in span:
            shapes.add("AutoComplete")
        elif 'mode="tags"' in span:
            shapes.add("tags")
        else:
            shapes.add("other")
    assert shapes == {"AutoComplete"}, (
        f"分组控件形态不统一：{shapes} —— 用户要求三处保持相同逻辑"
    )


def test_system_settings_form_offers_existing_groups_as_options():
    """系统配置的分组候选要包含**列表里出现过的**分组，不只是内置常量。

    否则用户自建过一个分组，下次新增时下拉里选不到它 —— 只能再敲一遍，
    敲错一个字就变成两个分组。
    """
    text = (FRONTEND / "components/settings/settings-table.tsx").read_text(encoding="utf-8")
    assert "categorySelectOptions" in text, "没有派生可选分组"
    assert "BASE_CATEGORY_OPTIONS" in text, "没有内置分组常量"
    #: 必须合并了列表里的实际分组（判据落在「遍历 settings」上）。
    assert re.search(r"for \(const item of settings\)", text), (
        "可选分组没有合并列表里已有的分组"
    )
