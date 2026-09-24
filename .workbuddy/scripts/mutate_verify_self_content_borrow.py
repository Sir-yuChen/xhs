"""变异验证：v1.27 的「自有会话缺失不再短路」+「三页签不按类型门禁」两类守卫。

## 为什么必须做

「断言写出来了」不等于「断言会红」。本项目已多次抓到**假绿**：断言的目标串在文件里
出现第二次、守卫不在执行路径上、夹具把缺陷形态造错导致两边都通过。所以每组新守卫都要
**把实现反过来改一次**，确认它**按预期变红**，再复原确认变绿。

## 五个注入

| # | 注入 | 期望变红的用例 |
|---|---|---|
| 1 | `_get_owned_pc_account_cookies` 恢复「取不到自有 jar 就抛」 | 只有 ark 通道也能借到会话读详情（DEF-056 回归） |
| 2 | 借不到时不再按维度选原因（`borrow_same_identity` 丢掉） | 笔记维度不套用用户维度那句话 |
| 3 | 兜底把**所有** HTTPException 都吞掉（去掉状态码判断） | 借用兜底不得吞掉归属错误（越权护栏） |
| 4 | 前端页签恢复 `disabled: !capability.available` | 页签不得按账号类型/能力门禁 |
| 5 | 详情失败改回 `message.error(...)`（丢掉安静降级） | 失败必须降级成一行说明 |

### 两个被本轮变异验证纠正的写法

* 第 3 条能红，靠的是**归属校验先于兜底**（`get_owned_active_account()` 在 catch 之外/之前）。
  若把归属校验也塞进同一个 `try`，这条就变成「越权也能进去」——**兜底顺序本身就是契约**。
* 第 5 条的锚点必须选**会被断言直接检查的代码形态**。最初想「删掉某处 `quietHint(` 调用」，
  但该标识符在文件里出现 4 次，删一处不影响 `"quietHint(" in code` —— 典型的**假红变异**
  （看着红了，红的却是别的原因，或者根本不红）。

用完自动复原（含异常路径），最后再确认基线恢复绿。

    python .workbuddy/scripts/mutate_verify_self_content_borrow.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = r"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe"

PC = "backend/app/api/platforms/xhs/pc.py"
PAGE = "frontend/src/pages/platforms/xhs/my-notes-page.tsx"

CASE_DETAIL_BORROW = (
    "tests/backend/test_self_content.py::test_creator_note_detail_borrows_a_usable_session_instead_of_failing"
)
CASE_NOTE_REASON = (
    "tests/backend/test_self_content.py"
    "::test_note_detail_without_any_usable_session_gives_the_note_scoped_reason"
)
CASE_OWNERSHIP = "tests/backend/test_self_content.py::test_borrowing_does_not_swallow_ownership_errors"
CASE_TABS = (
    "tests/backend/test_page_optimizations.py"
    "::test_my_notes_tabs_are_never_gated_by_account_type_or_capability"
)
CASE_QUIET = "tests/backend/test_page_optimizations.py::test_my_notes_failures_degrade_to_a_quiet_line"

#: ① 的锚点：容忍块整段换回「抛出去」。
TOLERANT_BLOCK = """    except HTTPException as exc:
        # 只吞「这个账号没存这条通道的令牌」；**归属类错误必须照旧抛出去**
        # （404 异租户 / 已软删，DEF-055）—— 兜底不能变成越权。
        if exc.status_code != status.HTTP_400_BAD_REQUEST:
            raise
        own_jar = ""
"""

MUTATIONS: list[tuple[str, str, str, str, list[str], bool]] = [
    (
        "① 恢复「取不到自有 jar 就抛」（DEF-056 的那个短路）",
        PC,
        TOLERANT_BLOCK,
        "    except HTTPException:\n        raise\n",
        [CASE_DETAIL_BORROW],
        False,
    ),
    (
        "② 借不到时不再按维度选原因",
        PC,
        "detail=_web_session_required_detail(borrow_same_identity=probe_identity),",
        "detail=_web_session_required_detail(),",
        [CASE_NOTE_REASON],
        False,
    ),
    (
        "③ 兜底吞掉**所有** HTTPException（去掉状态码判断）",
        PC,
        TOLERANT_BLOCK,
        "    except HTTPException:\n        own_jar = \"\"\n",
        [CASE_OWNERSHIP],
        False,
    ),
    (
        "④ 前端页签恢复按能力置灰/禁用",
        PAGE,
        "    return { key, label, children };",
        "    return { key, label, children, disabled: !capability.available };",
        [CASE_TABS],
        False,
    ),
    (
        "⑤ 详情失败改回弹 toast（丢掉安静降级）",
        PAGE,
        "      setDetailError(quietError(reason, code));",
        "      message.error(reason);",
        [CASE_QUIET],
        # ⚠️ 必须**全部**替换：这一行在 `refreshNoteDetail` 与 `openNote` 里各有一份，
        # 只改第一处时 `"setDetailError(quietError(" in code` 仍为真 ——
        # 变异会「不红」，看起来像守卫是摆设，实际是注入强度不够。
        #
        # 锚点 v1.28 起改为 `quietError(...)`：v1.27 的 `{ text, full }` 对象字面量
        # 已收敛成构造函数（失败信息还要带 `code` 供三分法判断）。**锚点跟着实现走**，
        # 否则脚本第一次跑就会报「锚点未命中」，而不是报「守卫是摆设」——
        # 前者是维护问题、后者才是安全问题，两者不能混。
        True,
    ),
]


def run(cases: list[str]) -> tuple[int, str]:
    """跑一次 pytest，返回 (退出码, 输出尾部 600 字符)。

    ⚠️ **不能只看退出码**：pytest 用 5 表示「没收集到用例」、4 表示「用法错误」，
    两者都非 0，会被误判成「守卫按预期变红」。所以必须同时看到真实汇总行。
    """
    result = subprocess.run(
        [PYTHON, "-m", "pytest", *cases, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, (result.stdout + result.stderr)[-600:]


def _summary_line(output: str) -> str:
    """从 pytest 输出里抠出那行真实汇总（`N failed, M passed` 等）。"""
    for line in reversed(output.strip().splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if " passed" in stripped or " failed" in stripped or "error" in stripped.lower():
            return stripped[:140]
    return output.strip().splitlines()[-1][:140] if output.strip() else "(无输出)"


def main() -> int:
    originals: dict[str, str] = {}
    results: list[tuple[str, bool, str]] = []

    try:
        for label, rel, needle, replacement, cases, replace_all in MUTATIONS:
            path = ROOT / rel
            if rel not in originals:
                originals[rel] = path.read_text(encoding="utf-8")
            source = originals[rel]
            if needle not in source:
                results.append((label, False, "注入锚点未命中（实现已改？）"))
                print(f"❌ {label} → 注入锚点未命中（实现已改？）")
                continue
            mutated = (
                source.replace(needle, replacement)
                if replace_all
                else source.replace(needle, replacement, 1)
            )
            path.write_text(mutated, encoding="utf-8")
            code, tail = run(cases)
            path.write_text(source, encoding="utf-8")
            summary = _summary_line(tail)
            results.append((label, code != 0, summary))
            state = "按预期变红" if code != 0 else "仍然全绿（守卫是摆设）"
            print(f"{'✅' if code != 0 else '❌'} {label} → {state}  [{summary}]")
    finally:
        for rel, source in originals.items():
            (ROOT / rel).write_text(source, encoding="utf-8")
        print("\n[已复原全部源文件]")

    all_cases = [CASE_DETAIL_BORROW, CASE_NOTE_REASON, CASE_OWNERSHIP, CASE_TABS, CASE_QUIET]
    code, tail = run(all_cases)
    print(f"基线复跑：exit={code} → {'全部通过 ✅' if code == 0 else '仍有红 ❌'}")
    if code != 0:
        print(tail)

    caught = sum(1 for _, red, _ in results if red)
    print(f"\n变异验证结果：{caught}/{len(MUTATIONS)} 按预期变红；基线 {'绿' if code == 0 else '红'}")
    for label, red, note in results:
        if not red:
            print(f"  ⚠️ {label}：{note}")
    return 0 if caught == len(MUTATIONS) and code == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
