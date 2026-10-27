"""商品详情弹窗底部按钮（2026-10-27 用户口径）。

用户口径：「重抓此商品按钮和查看挂链笔记按钮功能移除，新增一个接口刷新功能」。

三个判据：
1. 两个旧按钮的**文案**不许再出现（它们分别是本地台账同步、挂链笔记弹窗的入口）；
2. 必须有一个「刷新数据」按钮；
3. 那个按钮的 onClick **要真的重发详情请求**，不能只是关窗 ——
   只查文案会被「按钮在、点了没反应」骗过。
"""

from __future__ import annotations

import re
from pathlib import Path

PAGE = Path("frontend/src/pages/platforms/xhs/shop-items-page.tsx")


def _code(path: Path) -> str:
    """剥掉注释后再断言。

    ⚠️ 本仓的历史做法是「在注释里写明为什么删掉 X」——直接扫原文会被
    自己的注释命中（删掉的按钮名正好写在注释里解释原因）。
    ⚠️ 但**不能**用 `\\{\\s*/\\*.*?\\*/\\s*\\}` 整块删 JSX 注释：那会连注释后面的
    兄弟元素一起吃掉（本文件实测把「刷新数据」按钮连同文字删了，断言恒假）。
    只删注释体本身，保留外层花括号。
    """
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"\{/\*.*?\*/\}", "", text, flags=re.S)  # JSX 注释 → 空
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)  # 块注释
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)  # 行注释
    return text


def _detail_modal_footer_block() -> str:
    """截出 `<ProductDetailModal ... footer={...} />` 那一段。

    只在这一段里断言：页面别处也有按钮，全文件搜「关闭」之类必然误命中。

    ⚠️ 结束位置**不能**用 `src.index("/>")` —— 那样会命中 `footer` 里第一个
    `<Button ... />` 的自闭合标签，截出来只有「关闭」一个按钮（实测过）。
    正确做法：从 `footer={` 起做花括号配对，取到配对的那个 `}`。
    """
    src = _code(PAGE)
    start = src.index("<ProductDetailModal")
    brace_at = src.index("footer={", start)
    depth = 0
    for i in range(brace_at + len("footer="), len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace_at : i + 1]
    raise AssertionError("`footer={...}` 花括号没有配对")


def test_old_detail_buttons_are_gone() -> None:
    """「重抓此商品」「查看挂链笔记」两个按钮**彻底移除**。"""
    block = _detail_modal_footer_block()
    assert "重抓此商品" not in block, "「重抓此商品」按钮还在（它走的是已退场的本地台账同步）"
    assert "查看挂链笔记" not in block, "「查看挂链笔记」按钮还在（挂链笔记入口在列表操作列，详情里不重复）"


def test_refresh_button_exists() -> None:
    """必须有一个「刷新数据」按钮。"""
    block = _detail_modal_footer_block()
    assert "刷新数据" in block, "详情弹窗底部缺「刷新数据」按钮"


def test_refresh_button_actually_refetches() -> None:
    """刷新按钮的 onClick 必须**真的重新取数**（不是只关窗）。

    ⚠️ 钉具体调用：`openDetail(detail.item)` 会重发 `/item-detail`。
    变异把它换成 `setDetailOpen(false)` 这类「看起来像动作」的语句，
    只查文案的守卫抓不住。
    ⚠️ loading 要钉在**这个 Button 自己的属性上**：`detailLoading` 在
    `<ProductDetailModal loading={detailLoading}>` 也出现，全文/整段搜会被它满足
    （实测变异 `static` 逃逸过一次）。
    """
    block = _detail_modal_footer_block()
    assert re.search(r"openDetail\(\s*detail\.item\s*\)", block), (
        "「刷新数据」没有重新调用 openDetail —— 按钮在但点了不会重新拉平台数据"
    )
    #: 取「刷新数据」**所属**那个 <Button ...> 的属性区。
    #: ⚠️ 必须从「关闭」按钮之后开始找最后一个 `<Button`：直接 `rindex(..., 0, btn_at)`
    #: 拿到的是「关闭」按钮自己，于是断言去查了关闭按钮的属性，刷新按钮的
    #: `loading` 删掉也照样通过（变异 `static` 逃过两次）。
    btn_at = block.index("刷新数据")
    close_at = block.index("关闭")
    btn_start = block.rindex("<Button", close_at, btn_at)
    attrs = block[btn_start:btn_at]
    assert "loading={detailLoading}" in attrs, (
        "刷新按钮没接 loading 态（连点会叠请求、也看不出在刷新）"
    )


def test_dead_sync_return_state_is_removed() -> None:
    """`syncReturnItem` 整套状态随「重抓此商品」退场。

    只删按钮、留着 state 的话，`setSyncReturnItem` 的赋值点没了 ⇒ 它是死代码，
    下一次改这块的人会以为还有入口。
    """
    src = _code(PAGE)
    assert "syncReturnItem" not in src, "`syncReturnItem` 残留（「重抓此商品」的回头重开逻辑已无用）"
