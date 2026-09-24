"""变异验证：v1.28 的「失败呈现三分法 + 结构化原因不再被吞」守卫（DEF-058）。

## 为什么必须做

「断言写出来了」不等于「断言会红」。本项目已多次抓到**假绿**。这一轮尤其危险：
新增的大多是**源码级断言**（`"description={error.full}" in code` 这类），它们很容易
写成「看着在查、其实在查注释或查一个到处都有的短串」，从而永远不红。

## 八个注入

| # | 注入 | 期望变红的用例 |
|---|---|---|
| 1 | 拦截器回退成 `typeof detail === "string" ? detail : "请求失败…"` | 结构化 detail 必须上屏 |
| 2 | 去掉 interactions loader 的 `_silent` | 四个 loader 的失败展示权归页面 |
| 3 | `renderFailure` 一律安静一行（丢掉真异常红块） | 失败三分法 |
| 4 | 短文案恢复「请稍后再试」 | 文案必须可执行 |
| 5 | 后端原因复活「创作者平台不提供这三类数据」 | 不得重复已被抓包推翻的归因 |
| 6 | 两维度共用一句原因（`if borrow_same_identity:` → `if True:`） | 维度感知的原因必须真的不同 |
| 7 | `detailMessage` 的结构化分支失效（`if (false)`） | **行为**验证（真实打包产物） |
| 8 | 删掉 422 数组的还原分支 | 校验错误必须交出「字段路径 + 原因」 |

第 7 条是刻意加的：它**同时**打源码断言与行为断言 —— 如果只有前者，这条变异也会红，
但那说明不了任何行为。真正承重的是 `tests/frontend/detail_message.cjs` 里
「B1 真原因上屏」那一条。第 8 条同理：源码断言只能证明「这行还在」，
真正证明它干活的是脚本里的 A5/A7/A8 与 B3。

用完自动复原（含异常路径），最后确认基线恢复绿。

    python .workbuddy/scripts/mutate_verify_def058.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = r"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe"

PC = "backend/app/api/platforms/xhs/pc.py"
API = "frontend/src/lib/api.ts"
PAGE = "frontend/src/pages/platforms/xhs/my-notes-page.tsx"

PO = "tests/backend/test_page_optimizations.py"
SC = "tests/backend/test_self_content.py"

CASE_INTERCEPTOR = f"{PO}::test_api_interceptor_surfaces_structured_detail_instead_of_a_generic_toast"
CASE_SILENT = f"{PO}::test_self_content_loaders_keep_failure_ownership_local"
CASE_THREE_WAY = f"{PO}::test_failures_are_rendered_by_a_three_way_rule"
CASE_ACTIONABLE = f"{PO}::test_quiet_reason_is_actionable_not_a_wait_and_see"
CASE_BEHAVIOUR = f"{PO}::test_structured_detail_reaches_the_toast_via_a_real_bundle"
CASE_DISPROVEN = f"{SC}::test_web_session_reason_does_not_repeat_a_disproven_claim"
CASE_DIMENSION = f"{SC}::test_web_session_reason_is_dimension_aware_and_unique"

#: ③ 的锚点：三分法整段换回「一律安静一行」。
THREE_WAY_BLOCK = """  if (EXPECTED_FAILURE_CODES.has(error.code)) return quietHint(error.full, error.text);
  return (
    <div style={{ padding: "16px 0" }}>
      <Alert showIcon type="error" message="这一类数据加载失败" description={error.full} />
    </div>
  );
"""

#: ⑦ 的锚点：结构化 detail 的分支。
STRUCTURED_BRANCH = '  if (detail && typeof detail === "object") {'

#: ⑧ 的锚点：422 数组的还原分支。
VALIDATION_BRANCH = "  if (Array.isArray(detail)) return validationDetailMessage(detail);"

MUTATIONS: list[tuple[str, str, str, str, list[str], bool]] = [
    (
        "① 拦截器回退：结构化 detail 再被降级成通用文案",
        API,
        '        message.error(detailMessage(error.response?.data?.detail) || "请求失败，请稍后重试");',
        "        const detail = error.response?.data?.detail;\n"
        '        message.error(typeof detail === "string" ? detail : "请求失败，请稍后重试");',
        [CASE_INTERCEPTOR],
        False,
    ),
    (
        "② 去掉 interactions loader 的 `_silent`",
        API,
        '  const response = await http.post<XhsInteractionResponse>("/xhs/pc/users/interactions", payload, {\n'
        "    _silent: true,\n"
        "  } as never);",
        '  const response = await http.post<XhsInteractionResponse>("/xhs/pc/users/interactions", payload);',
        [CASE_SILENT],
        False,
    ),
    (
        "③ renderFailure 一律安静一行（丢掉真异常的红块）",
        PAGE,
        THREE_WAY_BLOCK,
        "  return quietHint(error.full, error.text);\n",
        [CASE_THREE_WAY],
        False,
    ),
    (
        "④ 短文案恢复「请稍后再试」（一个做不到的建议）",
        PAGE,
        '  WEB_SESSION_REQUIRED: "该账号缺少网页版登录态，这类数据读不到，需在网页版重新登录",',
        '  WEB_SESSION_REQUIRED: "该账号暂无可用网页会话，这一类数据暂时取不到，请稍后再试",',
        [CASE_ACTIONABLE],
        False,
    ),
    (
        "⑤ 后端原因复活「创作者平台不提供这三类数据」（已被抓包推翻）",
        PC,
        '                "只能由网页版读取。请用该账号在网页版（xiaohongshu.com）登录后重新导入 Cookie。"',
        '                "无法读取这一分类的内容（创作者平台不提供这三类数据）。"',
        [CASE_DISPROVEN],
        False,
    ),
    (
        "⑥ 两个维度共用一句原因（笔记维度因此变成假话）",
        PC,
        "    if borrow_same_identity:\n        return {",
        "    if True:\n        return {",
        [CASE_DIMENSION],
        False,
    ),
    (
        "⑦ detailMessage 的结构化分支失效（源码 + 行为双红）",
        API,
        STRUCTURED_BRANCH,
        "  if (false) {",
        [CASE_BEHAVIOUR],
        False,
    ),
    (
        "⑧ 422 数组分支被删（校验错误只剩英文 axios 兜底）",
        API,
        VALIDATION_BRANCH + "\n",
        "",
        [CASE_BEHAVIOUR, CASE_INTERCEPTOR],
        False,
    ),
]


def run(cases: list[str]) -> tuple[int, str]:
    """跑一次 pytest，返回 (退出码, 输出尾部 900 字符)。

    ⚠️ **不能只看退出码**：pytest 用 5 表示「没收集到用例」、4 表示「用法错误」，
    两者都非 0，会被误判成「守卫按预期变红」。所以必须同时看到 `failed` 汇总行。
    """
    result = subprocess.run(
        [PYTHON, "-m", "pytest", *cases, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, (result.stdout + result.stderr)[-900:]


def _summary_line(output: str) -> str:
    for line in reversed(output.strip().splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if " passed" in stripped or " failed" in stripped or "error" in stripped.lower():
            return stripped[:150]
    return output.strip().splitlines()[-1][:150] if output.strip() else "(无输出)"


def _really_failed(output: str) -> bool:
    """确认是**真的有用例失败**，而不是「没收集到」/「用法错误」造成的非 0。"""
    return "failed" in output or "FAILED" in output or "assert" in output


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
            red = code != 0 and _really_failed(tail)
            results.append((label, red, summary))
            state = "按预期变红" if red else "仍然全绿（守卫是摆设）"
            print(f"{'✅' if red else '❌'} {label} → {state}  [{summary}]")
    finally:
        for rel, source in originals.items():
            (ROOT / rel).write_text(source, encoding="utf-8")
        print("\n[已复原全部源文件]")

    all_cases = [
        CASE_INTERCEPTOR,
        CASE_SILENT,
        CASE_THREE_WAY,
        CASE_ACTIONABLE,
        CASE_BEHAVIOUR,
        CASE_DISPROVEN,
        CASE_DIMENSION,
    ]
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
