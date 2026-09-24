"""变异验证：证明 verify_doc_stats.py 的三层检查各自真的承重。

背景：v1.25 登记 DEF-043 时发现 §2.1 的 P1 行**总数 18 与各列之和 19 长期不符**，
旧脚本只比「列求和 vs 声明合计」（两者都由同一批声明值累加）→ 看不见**分级内部**的不自洽。
2026-09-20 又发现**第三类**漂移：**DEF-046 声明 🔴P0 却物理写在 §2.4（P2 章节）** ——
逐条反推得 P0=8 / P2=13，表里声明 9 / 12，但**两侧列求和都等于 46**、`MATCH` 仍为 True。
所以脚本又补了第 ③ 层：「声明分级总数 vs 逐条反推分级条数」。

本脚本把三类「容易骗过前几层」的错配分别注回去，期望：
    A → 逐级别自洽变红
    B → 反推==列求和变红（明细改了、汇总没改）
    C → 分级总数==反推变红，**且 MATCH 仍为 True**（证明旧的三层看不见它）

用法：py -3.12 .workbuddy/scripts/mutate_verify_docstats.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path("D:/work/XHS")
LEDGER = ROOT / "docs" / "04_测试文档" / "04_测试跟踪与缺陷标记.md"
PY = r"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe"
VERIFY = ROOT / ".workbuddy" / "skills" / "xhs-doc-stats-verify" / "verify_doc_stats.py"

# ⚠️ 锚点必须跟着 §2.1 的当前数字走：数字一改，这里不更新就会 assert 失败
# （2026-09-20 实测：脚本里还留着 P1 **19** / 合计 **43**，早已失效）。
P1 = "| 🟠 P1 高 | **22** | **7** | 0 | **4** | **10** | 1 |"
P2 = "| 🟡 P2 中 | 12 | 9 | 0 | 0 | 3 | 0 |"
TOTAL = "| **合计** | **48** | **22** | **1** | **4** | **20** | **1** |"

#: 正文里 DEF-047 的状态标记（改它＝改明细、不改汇总）
DEF047_STATUS = "**`[FIXED]`**（2026-09-20 登记并修复"


def move_block(text: str, head: str, target_marker: str) -> str:
    """把 `head` 开头的条目块整块搬到 `target_marker` 之前（模拟「写错章节」）。"""
    start = text.index(head)
    end = text.index("#### DEF-", start + len(head))
    block = text[start:end]
    text = text[:start] + text[end:]
    return text.replace(target_marker, block + target_marker, 1)


def mutation_a(text: str) -> str:
    """A：P1 总数 22→21 且合计 48→47（列求和仍 == 声明合计）。"""
    assert P1 in text and TOTAL in text
    text = text.replace(P1, "| 🟠 P1 高 | **21** | **7** | 0 | **4** | **10** | 1 |", 1)
    return text.replace(TOTAL, "| **合计** | **47** | **22** | **1** | **4** | **20** | **1** |", 1)


def mutation_b(text: str) -> str:
    """B：正文里把 DEF-047 的状态 `[FIXED]` 改成 `[OPEN]`（明细改了、汇总没跟着改）。"""
    assert DEF047_STATUS in text
    return text.replace(
        DEF047_STATUS, "**`[OPEN]`**（2026-09-20 登记并修复", 1
    )


def mutation_c(text: str) -> str:
    """C：把 DEF-047 块从 §2.3（P1）搬到 §2.4（P2）—— 只改章节，不改任何数字。

    这是 DEF-046 漂移的原样复刻：**声明数字一个没动**，所以 A/B 两层都看不见。

    ⚠️ 落点必须是 §2.4 **内部**（这里锚 `#### DEF-016`）。第一版锚的是 §2.4 的
    **章节标题行**，而 DEF-047 本来就排在 §2.3 末尾、紧邻 §2.4 —— 搬过去等于没搬，
    变异「通过」了却什么都没验证到。**变异的落点选错，验证本身就变成假绿。**
    """
    return move_block(text, "#### DEF-047 · ", "#### DEF-016 · ")


MUTATIONS = [
    (
        "A: P1 总数 22→21 且合计 48→47（列求和仍 == 声明合计）",
        mutation_a,
        "分级内部不自洽",
    ),
    (
        "B: 正文 DEF-047 状态 FIXED→OPEN（明细改了、汇总没改）",
        mutation_b,
        "明细与汇总不符",
    ),
    (
        "C: DEF-047 块搬进 §2.4（只改章节、数字一个没动）",
        mutation_c,
        "分级总数与逐条反推不符",
    ),
]


def run_verify() -> str:
    proc = subprocess.run(
        [PY, str(VERIFY)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
    )
    return (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    original = LEDGER.read_text(encoding="utf-8")
    failures: list[str] = []
    try:
        for label, mutate, expected_hit in MUTATIONS:
            LEDGER.write_text(mutate(original), encoding="utf-8")
            out = run_verify()
            verdict = "PASS" if "OVERALL: PASS" in out else "FAIL"
            hit = expected_hit in out and "⚠️" in out
            good = verdict == "FAIL" and hit
            print(f"[{'红 ✅' if good else '未按预期 ❌'}] {label}")
            print(f"    结论={verdict}  期望命中的层「{expected_hit}」命中={hit}")
            for line in out.splitlines():
                if "⚠️" in line or "MATCH" in line or "逐级别自洽" in line \
                        or "反推==列求和" in line or "分级总数==反推" in line:
                    print("      " + line.strip())
            print()
            if not good:
                failures.append(label)
            # C 的额外证据：MATCH 必须**仍为 True**，否则说明它被更早的层拦住了，
            # 就证明不了「旧三层看不见这类漂移」。
            if expected_hit == "分级总数与逐条反推不符" and "MATCH   : True" not in out:
                print("      ⚠️ MATCH 不是 True —— 这条变异被更早的层拦住了，"
                      "无法证明第 ③ 层承重")
                failures.append(label + "（MATCH 非 True）")
    finally:
        LEDGER.write_text(original, encoding="utf-8")

    out = run_verify()
    print("[复原后基线]")
    print("    结论=" + ("PASS" if "OVERALL: PASS" in out else "FAIL") + "（期望 PASS）")
    for line in out.splitlines():
        if "逐级别自洽" in line or "反推==列求和" in line or "分级总数==反推" in line:
            print("      " + line.strip())
    print(f"\n变异验证：{len(MUTATIONS) - len(failures)}/{len(MUTATIONS)} 条按预期变红")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
