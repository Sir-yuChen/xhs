"""界面文案守卫（设计 66 §5，2026-10-25 用户口径）。

用户口径：「页面中有很多说明都是占用页面空间，且啰嗦，应简短且只说明核心问题，
不必要的说明全部移除」「类似文案太多啰嗦，应简要明了或**收归到说明图标中**」。

## 判据（两条，都能机械检查）

1. **界面上不得出现设计文档章节号**（`设计 NN §x`）—— 那是开发溯源信息。
   代码注释里可以写（`//` / `/* */`），**渲染到 JSX 文本里不行**。
2. **配置管理页不得有常驻解释段落** —— 分区已由页面标题表达。

⚠️ 为什么只查这两条而不是「所有长文案」：用户能读到的大部分长文案是
**操作提示**（「会真实删除磁盘文件」「支持 JPG / PNG」），删了会让人误操作。
判据要能区分「啰嗦」与「必要」，不能一刀切。
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def _strip_comments(text: str) -> str:
    """去掉 `//` 行注释与 `/* */` 块注释 —— 注释里的设计章节号是允许的。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


def test_no_design_doc_reference_in_rendered_ui():
    """渲染文本里不得出现「设计 NN §x」（开发溯源信息不该给用户看）。"""
    pattern = re.compile(r"设计\s*\d+\s*§")
    offenders: list[str] = []
    for path in SRC.rglob("*.tsx"):
        clean = _strip_comments(path.read_text(encoding="utf-8", errors="ignore"))
        for lineno, line in enumerate(clean.splitlines(), 1):
            # 只查 JSX 文本形态：以 `>` 开头或纯文本行（不是 import / 变量赋值）
            if pattern.search(line) and not line.lstrip().startswith(("import", "//")):
                offenders.append(f"{path.relative_to(SRC)}:{lineno} {line.strip()[:70]}")
    assert not offenders, "界面文案里出现设计文档章节号：\n  " + "\n  ".join(offenders)


def test_settings_page_has_no_standing_explanation_paragraph():
    """配置管理页 / 个人配置页不得有常驻解释段落（分区由标题表达）。"""
    for rel in ("pages/system/system-settings-panel.tsx", "pages/settings/settings-page.tsx"):
        text = _strip_comments((SRC / rel).read_text(encoding="utf-8"))
        assert "Paragraph" not in text, (
            f"{rel} 里仍有 Paragraph 解释段落 —— 用户口径「不必要的说明全部移除」"
        )


def test_settings_table_has_no_standing_hints():
    """配置表格不得有**常驻**提示行（`HINTS` 常量与它的渲染）。

    ⚠️ 匹配要精确：`HINTS[scope]` 会连带命中 `DELETE_HINTS[scope]` /
    `EMPTY_HINTS[scope]`（那两者是**按需**出现 —— 删除确认 / 空表时才有），
    用宽匹配会误报。这里用词边界只抓独立的 `HINTS`。
    """
    text = _strip_comments((SRC / "components/settings/settings-table.tsx").read_text(encoding="utf-8"))
    assert not re.search(r"(?<![A-Z_])HINTS\[scope\]", text), "配置表格仍在渲染常驻提示行"
    assert not re.search(r"const\s+HINTS\b", text), "`HINTS` 常量应已删除"


def test_empty_hints_are_short():
    """配置表格的空态提示必须短（≤10 字）。"""
    text = (SRC / "components/settings/settings-table.tsx").read_text(encoding="utf-8")
    block = re.search(r"const EMPTY_HINTS[^{]*\{(.*?)\};", text, re.S)
    assert block, "找不到 EMPTY_HINTS"
    for m in re.finditer(r':\s*"([^"]+)"', block.group(1)):
        value = m.group(1)
        assert len(value) <= 10, f"空态提示过长（{len(value)} 字）：{value}"
