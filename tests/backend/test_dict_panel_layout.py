"""字典管理页布局守卫（2026-10-26 用户截图口径）。

## 用户原话

> 「字典布局页面不合理，一些数据无法展示完成，一些没有数据占用大量空间，你再调整一下」

## 截图里能直接看到的三件事（都不是「审美」，是可量的）

| 现象 | 根因 |
|---|---|
| 右栏「值」列的 `note_publish_single` 被折成两行（`note_publish_sing` + `le`） | 列宽写死 **140px**，装不下最长真实值 |
| 右栏「备注」17 行全是 `-`，却吃掉整列 flex 剩余宽度 | 空列无条件渲染 |
| 左栏「项」列表头被压成单字「项」 | 「编码」写死 170px，把相邻列挤扁 |

## 判据（三条，都可机械检查）

1. **定长内容列必须给足宽度且不换行**：`值` ≥ 240px + `whiteSpace:nowrap`。
   （`note_publish_single` 19 字符 ≈ 190px，加 padding 与 code 字体余量）
2. **空列不占位**：`备注` 列必须在「本组有非空备注」时才进 `columns`，
   不能无条件下发一个恒 `-` 的列。
3. **类型列表要能筛**：43 种类型不许只能靠滚动找（需要有搜索框）。

⚠️ 这三条都是「列宽/条件渲染」层面的，不涉及数据 —— 所以用源码结构断言即可，
不需要渲染组件。**故意不写成精确 JSX 解析**：本仓页面里 JSX 形态变化频繁，
精确解析会变成维护负担；而「列宽写回 140」「备注无条件加上」这类退化都能抓住。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DICT_PANEL = ROOT / "frontend" / "src" / "pages" / "system" / "system-dict-panel.tsx"


def _src() -> str:
    return DICT_PANEL.read_text(encoding="utf-8")


def test_value_column_is_wide_enough_and_does_not_wrap():
    """「值」列要装得下**实测最长值**，且不折行。

    退化表现（截图实证）：`note_publish_single` 在 140px 下变成两行 ——
    用户读到的是一串被切断的字符，完全无法复制/比对。

    ⚠️ 宽度**不是拍脑袋**：实测全库 208 条字典项，最长值是
    `product_note_generate_llm_single`（**32 字符**，≈278px @12px monospace）。
    ⇒ 300 是下界。写成 260 这种「看着差不多」的值会漏掉那批最长的。
    """
    src = _src()
    assert 'dataIndex: "value"' in src, "找不到「值」列（结构变了，请同步本守卫）"
    start = src.index('dataIndex: "value"')
    block = src[start : start + 500]
    m = re.search(r"width:\s*(\d+)", block)
    assert m, "「值」列没写 width"
    assert int(m.group(1)) >= 300, (
        f"「值」列宽度 {m.group(1)} < 300 —— 实测最长值 32 字符需 ≈278px，"
        "再窄就会折行（截图里就是这个现象）"
    )
    assert 'whiteSpace: "nowrap"' in block, (
        "「值」列没有 whiteSpace:nowrap ⇒ 长值会被折行"
    )


def test_remark_column_is_conditional():
    """`备注` 列只在有非空备注时出现（17 行全 `-` 不许占一整列）。"""
    src = _src()
    assert "hasAnyRemark" in src, (
        "没有 hasAnyRemark 判据 ⇒ 「备注」列无条件渲染，空列吃掉整列横向空间"
    )
    #: 必须是「条件展开」形态（spread + 三元），不是普通列。
    assert "...(hasAnyRemark" in src, (
        "hasAnyRemark 没有用在 columns 的条件展开里（可能只是算了一下没用）"
    )


def test_type_list_has_a_search_box():
    """43 种字典类型必须能搜索（截图里只能滚动，底部类型被切掉）。"""
    src = _src()
    assert "typesFilter" in src, "左栏没有搜索状态 ⇒ 43 种类型只能滚动找"
    assert 'placeholder="搜类型名 / 编码"' in src, "搜索框只是摆设（没有实际 placeholder）"
    #: 搜索必须真的过滤 dataSource，不能只渲染一个框。
    assert "dataSource={filteredTypes}" in src, (
        "搜索框没有接到 dataSource 上（输入了但列表不变）"
    )


def test_both_tables_scroll_horizontally_instead_of_squeezing_columns():
    """两张表都要 `x: "max-content"`：窄屏时横向滚动，不把列挤成竖排。"""
    src = _src()
    hits = src.count('x: "max-content"')
    assert hits >= 2, f"只有 {hits} 处横向滚动配置（左右两张表都要有）"
