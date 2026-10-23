"""未使用 import 守卫（2026-09-28）：`backend/app` 不许再出现「导入了却一次没用」的名字。

**为什么值得一条守卫**：死导入不影响功能，所以 review 时最容易被放过去；但它会拉长
「这个符号从哪来」的阅读路径、并掩盖重构后的残留 —— 本次实测就是例子：删掉一个端点后
`Query` 立刻没人用了，而**没有任何机制会提醒**。清理是一次性的，守卫才让结果保持住。

**判据**（与 `.hermes/tmp/scan_unused_imports.py` 同一份，保守防误报）：
名字在「**去掉所有 import 行之后**的源码」里一次都没出现（整词匹配）。因此字符串注解
（`-> "Foo"`）、`__all__` 重导出、属性访问（`mod.attr`）都算「用到了」—— 只报铁定没用的。

**逃生口**：该行写 `# noqa` 即豁免（用于「导入即注册」这类副作用导入）。

**范围**：只扫 `backend/app`（业务代码）。`apis/` 是逆向 SDK（大量历史形态）、
`xhs_utils/` 是运行时工具，都不纳入 —— 本次不扩大战场。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest




#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract
BACKEND_APP = Path(__file__).resolve().parents[2] / "backend" / "app"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _unused_imports(path: Path) -> list[tuple[str, int]]:
    """该文件里「导入了但源码其余部分一次没出现」的名字（按行号升序）。"""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    lines = text.splitlines()

    imported: dict[str, int] = {}
    skip_lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        end = node.end_lineno or node.lineno
        for i in range(node.lineno, end + 1):
            skip_lines.add(i)
        # `from __future__ import annotations` 是**编译器指令**（不绑定任何名字）⇒ 不参与判定。
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("__future__"):
            continue
        if "# noqa" in "\n".join(lines[node.lineno - 1 : end]):
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            if isinstance(node, ast.Import):
                name = alias.asname or alias.name.split(".")[0]
            else:
                name = alias.asname or alias.name
            imported[name] = node.lineno

    body = "\n".join(line for idx, line in enumerate(lines, 1) if idx not in skip_lines)
    return [
        (name, lineno)
        for name, lineno in sorted(imported.items(), key=lambda kv: kv[1])
        if not re.search(r"\b%s\b" % re.escape(name), body)
    ]


def test_backend_app_has_no_unused_imports():
    """`backend/app` 里不得存在未被使用的 import（发现即红，附文件与行号）。"""
    offenders: list[str] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        if "__pycache__" in str(path) or path.name == "__init__.py":
            continue
        for name, lineno in _unused_imports(path):
            rel = path.relative_to(REPO_ROOT).as_posix()
            offenders.append(f"{rel}:{lineno}  {name}")

    assert not offenders, (
        "发现未使用的 import —— 删掉它；若确属副作用导入（导入即注册），"
        "在该行加 `# noqa` 并写明原因：\n  " + "\n  ".join(offenders)
    )


def test_guard_is_not_vacuous(tmp_path: Path):
    """守卫自检：判据必须真的能认出「导入了却没用」的名字。

    ⚠️ 这条不是锦上添花 —— 本项目的 `.cjs` 接线守卫曾因「只查字符串」空转数月
    （`note_filters.cjs` 就是先例）。守卫必须先证明自己会红。
    """
    sample = tmp_path / "sample.py"
    sample.write_text("import json\nimport os\n\nprint(os.getcwd())\n", encoding="utf-8")
    assert _unused_imports(sample) == [("json", 1)]

    # `__future__` 与 `# noqa` 两个逃生口都不许被误报。
    future_sample = tmp_path / "future_sample.py"
    future_sample.write_text(
        "from __future__ import annotations\n\nx: int = 1\n", encoding="utf-8"
    )
    assert _unused_imports(future_sample) == []

    noqa_sample = tmp_path / "noqa_sample.py"
    noqa_sample.write_text("import json  # noqa\n\nx = 1\n", encoding="utf-8")
    assert _unused_imports(noqa_sample) == []

    # 字符串注解算「用到了」（保守，不误报）。
    anno_sample = tmp_path / "anno_sample.py"
    anno_sample.write_text('from a import Foo\n\ndef f() -> "Foo": ...\n', encoding="utf-8")
    assert _unused_imports(anno_sample) == []
