"""变异验证：证明 `verify_doc_counts.py` 的「绿」是**承重**的，而不是空跑。

用法：
    py -3.12 .workbuddy/scripts/mutate_verify_doc_counts.py

做法：往**文档**里注入一个错数（或破坏一个锚点），跑校验脚本 —— **必须变红**；
还原后**必须变绿**。任何一条「注入后仍绿」= 该检查项没在承重。

⚠️ 为什么必须做这件事：本项目栽过两次同一型的坑 ——
    * `verify_doc_stats.py` 只比「列求和 vs 声明合计」，**分级总数与逐条反推**这一层没有，
      于是 DEF-046 条目放错章节藏了很久（两侧列和都等于 46，`MATCH` 一直 True）。
    * 收紧判据后**大面积红且多半是夹具**（DEF-051：45 条全红、无一是生产代码的错）。
    ⇒ 一个**恒绿**的校验脚本比没有脚本更坏：它给的是**虚假的安全感**。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
CHECKER = ROOT / ".workbuddy" / "skills" / "xhs-doc-stats-verify" / "verify_doc_counts.py"

AGENTS = ROOT / "AGENTS.md"
README = ROOT / "docs" / "README.md"
MODEL_DOC = ROOT / "docs" / "02_设计文档" / "03_数据模型设计.md"
API_DOC = ROOT / "docs" / "02_设计文档" / "05_功能设计_后端API与服务层.md"
ARCH_DOC = ROOT / "docs" / "02_设计文档" / "02_系统架构设计.md"
CODEGRAPH_DOC = ROOT / "docs" / "04_测试文档" / "02_代码图谱_codegraph.md"

# (文件, 说明, 原文, 替换, replace_all)
MUTATIONS: list[tuple[Path, str, str, str, bool]] = [
    (AGENTS,
     "M1 AGENTS §5 端点 operations 155 → 156（权威表本身写错）",
     "| 后端端点 | **155** operations", "| 后端端点 | **156** operations", False),
    (README,
     "M2 README §五 请求函数 132 → 128（回到修之前的旧值）",
     "| 前端请求函数 | **132 个**", "| 前端请求函数 | **128 个**", False),
    (README,
     "M3 README §五 死代码页数 1 → 2（数字对、但模型判据错）",
     "其中 **1** 个是死代码", "其中 **2** 个是死代码", False),
    (MODEL_DOC,
     "M4 数据模型设计 头部口径 25 → 21（口径写回旧值）",
     "模型表 **25** 张", "模型表 **21** 张", False),
    (API_DOC,
     "M5 API 设计 规模行 path 135 → 127（只改一处、漏改另一处）",
     "path 条数 135；", "path 条数 127；", False),
    # M6 测的是**锚点失配分支**：措辞被改到正则匹配不到时，校验器必须报「锚点命中 0 次」并变红，
    # 而不是把「找不到」当成「没问题」静默放过。（注：单纯去掉加粗**不会**破锚 —— 正则已放宽为
    # `\D{0,2}` 容忍格式变化，那是设计意图，不是漏洞。）
    (README,
     "M6 改掉措辞（path 条数 → 路径条数）→ 正则匹配不到，应报锚点失配并变红",
     "path 条数 135", "路径条数 135", False),
    # M7/M8 覆盖「ASCII 分层图里的数字」——这类锚点靠**间距**区分同词多处，最容易写成
    # 「看着在查、其实没承重」（正则太松 → 注入了也不红）。
    (ARCH_DOC,
     "M7 架构设计 分层图 router 22 → 20（ASCII 图内数字）",
     "│  22 个 router  ·  155 个端点", "│  20 个 router  ·  155 个端点", False),
    (CODEGRAPH_DOC,
     "M8 代码图谱 L4 端点 155 → 136（ASCII 图内数字）",
     "（22 个 router / 155 端点）", "（22 个 router / 136 端点）", False),
]


def run_checker() -> tuple[int, str]:
    p = subprocess.run([PY, str(CHECKER)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    if not CHECKER.exists():
        print(f"缺少校验脚本：{CHECKER}")
        return 2

    print("=" * 72)
    print("基线：注入任何变异之前，校验脚本必须为 GREEN")
    rc, out = run_checker()
    baseline_ok = rc == 0 and "OVERALL: PASS" in out
    print(f"    exit={rc} -> {'GREEN ✅' if baseline_ok else 'RED ❌（基线就不绿，变异验证无意义）'}")
    if not baseline_ok:
        print(out[-1500:])
        return 1

    failures: list[str] = []
    for path, label, needle, repl, all_ in MUTATIONS:
        rel = path.relative_to(ROOT).as_posix()
        original = path.read_bytes()
        text = original.decode("utf-8")

        n = text.count(needle)
        if n == 0:
            failures.append(f"{label} —— ❌ 脚本锚点失效：`{needle[:50]}` 在 {rel} 里找不到")
            print(f"\n{label}\n    ⚠️ 锚点 0 命中，无法注入（**这本身是脚本 bug**，不是校验器的问题）")
            continue

        mutated = text.replace(needle, repl) if all_ else text.replace(needle, repl, 1)
        path.write_bytes(mutated.encode("utf-8"))
        try:
            rc_mut, out_mut = run_checker()
        finally:
            path.write_bytes(original)   # 无论发生什么，**必须**还原

        red = rc_mut != 0 and "OVERALL: FAIL" in out_mut
        # 顺带确认文案里真的指出了出问题的那份文件
        blamed = rel in out_mut
        print(f"\n{label}")
        print(f"    注入命中 {n} 处；校验器 exit={rc_mut} -> "
              f"{'RED ✅' if red else '仍 GREEN ❌（该检查项没承重）'}"
              f"{'；且报告里点名了 ' + rel + ' ✅' if blamed else '；⚠️ 报告未点名该文件'}")

        if not red:
            failures.append(f"{label} —— 注入后校验器仍为 GREEN（该检查项形同虚设）")
        elif not blamed:
            failures.append(f"{label} —— 变红了但没点名 {rel}（红得不由它负责，结论不可信）")

    print("\n" + "=" * 72)
    print("还原后复跑（必须回到 GREEN，证明还原是干净的）")
    rc, out = run_checker()
    restored_ok = rc == 0 and "OVERALL: PASS" in out
    print(f"    exit={rc} -> {'GREEN ✅' if restored_ok else 'RED ❌（还原不干净！）'}")
    if not restored_ok:
        failures.append("还原后未回到 GREEN —— 变异脚本把文档写坏了")

    print("\n" + "=" * 72)
    total = len(MUTATIONS)
    if failures:
        print(f"MUTATION VERIFY: FAIL（{len(failures)}/{total} 项有问题）")
        for f in failures:
            print("  -", f)
        return 1
    print(f"MUTATION VERIFY: PASS（{total}/{total} 变异全部按预期变红，基线/还原均 GREEN）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
