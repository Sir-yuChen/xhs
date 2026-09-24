"""校验 XHS_ALL_IN_ONE 三张统计表的「明细 vs 合计」自洽性。

用法：
    py -3.12 .workbuddy/skills/xhs-doc-stats-verify/verify_doc_stats.py

覆盖：
    1. docs/04_测试文档/04_测试跟踪与缺陷标记.md  §2.1 缺陷汇总表
    2. docs/04_测试文档/04_测试跟踪与缺陷标记.md  §3   测试进度表
    3. docs/04_测试文档/03_测试用例库.md          §14  分域统计表

设计要点（都是踩过的坑，改动前务必读）：
    * §2.5（P3）没有 `#### DEF-NNN` 块，用的是一张紧凑表，必须单独解析。
    * §2.5 表里的状态是带反引号的 `` `[OPEN]` ``。
    * 缺陷条目的明细行也以 `| 级别 |` 开头，不能拿它当汇总行的特征。
    * 表格行内含转义竖线 `\\|`（如 v1.8 版本说明），朴素的 split('|') 会数错列。
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LEDGER = ROOT / "docs" / "04_测试文档" / "04_测试跟踪与缺陷标记.md"
CASES = ROOT / "docs" / "04_测试文档" / "03_测试用例库.md"


def _int(cell: str) -> int:
    m = re.search(r"\d+", cell)
    return int(m.group(0)) if m else 0


def check_ledger_summary(lines: list[str]) -> bool:
    """§2.1 缺陷汇总表：以正文明细为唯一级别来源，反推后与合计行比对。"""
    section = None
    current = None
    records: list[dict] = []

    for line in lines:
        m = re.match(r"^### (2\.\d) (P\d)", line)
        if m:
            section = m.group(2)
            continue
        # §2.5 是紧凑表，没有 #### 块，交给下面的专门解析
        if re.match(r"^### 2\.5 ", line):
            section = None
            continue
        if re.match(r"^### (2\.\d)", line):
            section = None
            continue

        m = re.match(r"^#### DEF-(\d{3})", line)
        if m:
            current = {"id": m.group(1), "section": section, "status": None}
            records.append(current)
            continue

        if current and current["status"] is None:
            mm = re.search(r"\|\s*状态\s*\|\s*(.+?)\s*\|", line)
            if mm:
                current["status"] = mm.group(1)

    # §2.5 紧凑表：| DEF-026 | ... | 位置 | `[OPEN]` |
    start = next(i for i, l in enumerate(lines) if l.startswith("### 2.5"))
    for line in lines[start : start + 20]:
        m = re.match(r"^\|\s*DEF-(\d{3})\s*\|.*\|\s*`?\[([A-Z]+)\]`?\s*\|\s*$", line)
        if m:
            records.append(
                {"id": m.group(1), "section": "P3", "status": f"[{m.group(2)}]"})
            continue
        m2 = re.match(r"^\s*\|\s*\*\*P3 低\*\*\s*\|", line)  # 兼容其他写法
        if m2:
            pass

    buckets: dict[str, Counter] = {}
    for p in ("P0", "P1", "P2", "P3"):
        c: Counter = Counter()
        for r in records:
            if r["section"] != p:
                continue
            mm = re.search(r"\[(OPEN|FIXING|FIXED|VERIFIED|WONTFIX)\]", r["status"] or "")
            c[mm.group(1) if mm else "UNPARSED"] += 1
        buckets[p] = c

    total = sum(sum(c.values()) for c in buckets.values())
    merged: Counter = Counter()
    for c in buckets.values():
        merged.update(c)

    # 读声明
    i = next(k for k, l in enumerate(lines) if l.startswith("| 级别 | 总数"))
    derived = [0] * 6
    declared = None
    per_level: list[tuple[str, list[int]]] = []
    order = ["OPEN", "FIXING", "FIXED", "VERIFIED", "WONTFIX"]
    for line in lines[i + 2 : i + 10]:
        if not line.startswith("|"):
            break
        cells = [x.strip() for x in line.strip("|").split("|")]
        if cells[0].startswith("**合计"):
            declared = [_int(x) for x in cells[1:]]
            continue
        vals = [_int(x) for x in cells[1:]]
        per_level.append((cells[0], vals))
        derived = [a + b for a, b in zip(derived, vals)]

    print("§2.1 缺陷汇总表")
    for p in ("P0", "P1", "P2", "P3"):
        print(f"    {p}: {sum(buckets[p].values()):>3} 条  {dict(buckets[p])}")
    print(f"    反推合计: {total} 条  "
          f"{ {k: merged[k] for k in order if merged[k]} }")
    print(f"    列求和  : {derived}")
    print(f"    声明合计: {declared}")
    ok = derived == declared
    print(f"    MATCH   : {ok}")
    # ① 逐级别不变量：每一行的「总数」必须等于**它自己那一行**各状态列之和。
    #    ⚠️ 这一层原先漏了 —— 旧脚本只比「列求和 vs 声明合计」，而两者都由
    #    同一批声明值累加而来，于是某个级别**内部**「总数与列和对不上」被
    #    完全掩盖（v1.25 实测：P1 行声明 18 条、各列之和却是 19，藏了若干版本）。
    bad_rows = [name for name, vals in per_level if vals[0] != sum(vals[1:])]
    if bad_rows:
        print(f"    ⚠️ 这些级别的「总数」与各状态列之和不符：{'、'.join(bad_rows)}"
              f"（分级内部不自洽）")
        ok = False
    else:
        print(f"    逐级别自洽: True（{len(per_level)} 行，各自的总数 == 其各列之和）")
    # ② 逐条反推的分状态计数必须等于汇总表的列求和（明细 vs 汇总）。
    merged_list = [merged[k] for k in order]
    if merged_list != derived[1:]:
        print(f"    ⚠️ 逐条反推 {merged_list} != 列求和 {derived[1:]}（明细与汇总不符）")
        ok = False
    else:
        print(f"    反推==列求和: True {merged_list}")
    # ③ 声明的**分级总数**必须等于逐条反推的**分级条数**。
    #    ⚠️ 这一层原先漏了：①② 都只看「列的合计」或「某一行内部」，
    #    **没有任何一层把「某一级的声明总数」与「该级实际有几条」对上**。
    #    于是「条目放错章节」这类漂移完全隐形（2026-09-20 实测：DEF-046 声明 🔴P0
    #    却写在 §2.4（P2 章节）→ 反推得 P0=8 / P2=13，而表里声明 9 / 12；
    #    两侧**列求和都等于 46**，所以 MATCH 一直是 True）。
    level_names = ("P0", "P1", "P2", "P3")
    if len(per_level) != len(level_names):
        print(f"    ⚠️ 汇总表分级行数 {len(per_level)} != 4（表结构变了）")
        ok = False
    else:
        bad_levels = [
            f"{p} 声明 {vals[0]} 条 / 逐条反推 {sum(buckets[p].values())} 条"
            for (name, vals), p in zip(per_level, level_names)
            if vals[0] != sum(buckets[p].values())
        ]
        if bad_levels:
            print("    ⚠️ 分级总数与逐条反推不符：" + "；".join(bad_levels)
                  + "（常见成因：`#### DEF-NNN` 写在**不是它级别**的章节里）")
            ok = False
        else:
            print("    分级总数==反推: True "
                  f"{[sum(buckets[p].values()) for p in level_names]}")
    if total != (declared[0] if declared else total):
        print(f"    ⚠️ 明细条目数 {total} 与声明总数 {declared[0]} 不符"
              f"（差额通常意味着某章节用了非 `####` 的表格写法）")
        ok = False
    if declared and declared[0]:
        print(f"    关闭率  : {merged['VERIFIED']} / {total} = "
              f"{100 * merged['VERIFIED'] / total:.1f}%（口径：只算 [VERIFIED]）")
    return ok


def check_progress_table(lines: list[str]) -> bool:
    """§3 测试进度表：逐域求和 vs 合计行。"""
    i = next(k for k, l in enumerate(lines) if l.startswith("| 测试域 | 用例数"))
    total = [0] * 5
    declared = None
    for line in lines[i + 2 : i + 40]:
        if not line.startswith("|"):
            break
        cells = [x.strip() for x in line.strip("|").split("|")]
        if cells[0].startswith("**合计"):
            declared = [_int(x) for x in cells[1:6]]
            continue
        vals = [_int(x) for x in cells[1:6]]
        total = [a + b for a, b in zip(total, vals)]

    print("\n§3 测试进度表")
    print(f"    列求和  : {total}")
    print(f"    声明合计: {declared}")
    ok = total == declared
    print(f"    MATCH   : {ok}")
    if total[0]:
        print(f"    完成度  : {100 * total[1] / total[0]:.1f}%")
    return ok


def check_cases_table(lines: list[str]) -> bool:
    """§14 分域统计表：逐域求和 vs 合计行。"""
    i = next(k for k, l in enumerate(lines) if l.startswith("| 功能域 | 用例数"))
    n = p0 = 0
    declared = None
    for line in lines[i + 2 : i + 40]:
        if not line.startswith("|"):
            break
        cells = [x.strip() for x in line.strip("|").split("|")]
        a, b = _int(cells[1]), _int(cells[2])
        if cells[0].startswith("**合计"):
            declared = [a, b]
            continue
        n += a
        p0 += b

    print("\n§14 分域统计表（用例库）")
    print(f"    列求和  : [用例 {n}, P0 {p0}]")
    print(f"    声明合计: {declared}")
    ok = [n, p0] == declared
    print(f"    MATCH   : {ok}")
    return ok


def main() -> int:
    for path in (LEDGER, CASES):
        if not path.exists():
            print(f"缺少文件：{path}")
            return 2

    ledger_lines = LEDGER.read_text(encoding="utf-8").split("\n")
    cases_lines = CASES.read_text(encoding="utf-8").split("\n")

    results = [
        check_ledger_summary(ledger_lines),
        check_progress_table(ledger_lines),
        check_cases_table(cases_lines),
    ]

    print("\n" + "=" * 60)
    print("OVERALL:", "PASS" if all(results) else "FAIL")
    if not all(results):
        print("提示：先确认脚本的解析假设成立（见文件头「设计要点」），"
              "再判断文档是否需要修改。")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
