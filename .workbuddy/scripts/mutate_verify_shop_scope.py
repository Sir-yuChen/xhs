"""变异验证：`test_shop_nm.py` / `test_cookie_channels.py` 里新增的租户闸与软删闸守卫。

## 为什么必须做

「断言写出来了」不等于「断言会红」。本项目已经抓到过多次**假绿**：
断言的目标字符串在文件里出现第二次（对分支取反不敏感）、守卫根本不在执行路径上、
夹具自己把缺陷形态造错导致两边都通过。所以每组新守卫都要**把实现反过来改一次**，
确认它**按预期变红**，再复原确认变绿。

## 五个注入

| # | 注入 | 期望变红的用例 |
|---|---|---|
| 1 | `bindings_for_shop` 删**两条**租户闸 | 跨租户不计入 |
| 2 | `bindings_for_shop` 删 `PlatformAccount.deleted_at.is_(None)` | 软删不计入 + 不顶住孤儿清理 |
| 3 | `deps.get_owned_active_account` 删软删判定 | 软删账号按 id 端点 404 |
| 4 | `pc._borrowed_web_session` 删 `PlatformAccount.deleted_at.is_(None)` | 软删 jar 不进借用池 |
| 5 | `binding_for_account` 删软删判定 | 软删后解析不出绑定 |

### 一个被本轮变异验证纠正的设计直觉

第 1 条**必须一次删掉两条租户条件**才变红 —— 单删任一条时用例**仍然全绿**：
`ShopAccountBinding.user_id == user_id` 与 `PlatformAccount.user_id == user_id`
互为**冗余兜底**（前者挡「绑定行挂错租户」，后者挡「账号挂错租户」）。

这不是缺陷，是**有意的冗余**；但它意味着「删一行看变不变红」这种强度的变异
**证不出租户闸存在**。如果只做单行注入并看到全绿，很容易误判成「守卫是摆设」，
进而把闸门删掉 —— 所以这里把结论写进脚本：**冗余层要一起删，才算验证。**

用完自动复原（含异常路径），最后再确认基线恢复绿。

    python .workbuddy/scripts/mutate_verify_shop_scope.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = r"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe"

BINDING_SVC = "backend/app/services/shop_binding_service.py"
DEPS = "backend/app/core/deps.py"
PC = "backend/app/api/platforms/xhs/pc.py"

CASE_FOREIGN = "tests/backend/test_shop_nm.py::test_account_count_ignores_bindings_from_another_tenant"
CASE_SOFT_COUNT = "tests/backend/test_shop_nm.py::test_soft_deleted_account_is_excluded_and_does_not_block_orphan_cleanup"
CASE_SOFT_404 = "tests/backend/test_shop_nm.py::test_soft_deleted_account_is_404_on_account_scoped_endpoints"
CASE_SOFT_BORROW = "tests/backend/test_cookie_channels.py::TestSoftDeletedAccountsAreUnusable::test_soft_deleted_accounts_jar_is_never_borrowed"
CASE_SOFT_BINDING = "tests/backend/test_cookie_channels.py::TestSoftDeletedAccountsAreUnusable::test_binding_for_soft_deleted_account_is_none"

MUTATIONS: list[tuple[str, str, str, str, list[str]]] = [
    (
        "① bindings_for_shop 去掉**两条**租户闸",
        BINDING_SVC,
        "                ShopAccountBinding.user_id == user_id,\n                PlatformAccount.user_id == user_id,\n",
        "",
        [CASE_FOREIGN],
    ),
    (
        "② bindings_for_shop 去掉软删闸",
        BINDING_SVC,
        "                PlatformAccount.user_id == user_id,\n                PlatformAccount.deleted_at.is_(None),\n",
        "                PlatformAccount.user_id == user_id,\n",
        [CASE_SOFT_COUNT],
    ),
    (
        "③ 归属校验去掉软删闸（唯一实现）",
        DEPS,
        "if account is None or account.user_id != current_user.id or account.deleted_at is not None:",
        "if account is None or account.user_id != current_user.id:",
        [CASE_SOFT_404],
    ),
    (
        "④ 借用池去掉软删闸",
        PC,
        "            PlatformAccount.id != exclude_account_id,\n            PlatformAccount.deleted_at.is_(None),\n",
        "            PlatformAccount.id != exclude_account_id,\n",
        [CASE_SOFT_BORROW],
    ),
    (
        "⑤ binding_for_account 去掉软删闸",
        BINDING_SVC,
        "    if account is None or account.deleted_at is not None:\n        return None\n",
        "",
        [CASE_SOFT_BINDING],
    ),
]


def run(cases: list[str]) -> tuple[int, str]:
    """跑一次 pytest，返回 (退出码, 输出尾部 600 字符)。

    ⚠️ **不能只看退出码**：`pytest` 用 5 表示「没收集到用例」、4 表示「用法错误」，
    两者都非 0，会被误判成「守卫按预期变红」。本项目已吃过「假绿」的亏，
    这里反过来要防**假红** —— 所以退出码之外必须能看到 `failed` / `passed` 的真实汇总。
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
    """从 pytest 输出里抠出那行真实汇总（`N failed, M passed` / `not found` 等）。"""
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
        for label, rel, needle, replacement, cases in MUTATIONS:
            path = ROOT / rel
            if rel not in originals:
                originals[rel] = path.read_text(encoding="utf-8")
            source = originals[rel]
            if needle not in source:
                results.append((label, False, "注入锚点未命中（实现已改？）"))
                continue
            path.write_text(source.replace(needle, replacement, 1), encoding="utf-8")
            code, tail = run(cases)
            path.write_text(source, encoding="utf-8")
            # 期望：非 0 退出（有用例变红）
            summary = _summary_line(tail)
            results.append((label, code != 0, summary))
            state = "按预期变红" if code != 0 else "仍然全绿（守卫是摆设）"
            print(f"{'✅' if code != 0 else '❌'} {label} → {state}  [{summary}]")
    finally:
        for rel, source in originals.items():
            (ROOT / rel).write_text(source, encoding="utf-8")
        print("\n[已复原全部源文件]")

    all_cases = [CASE_FOREIGN, CASE_SOFT_COUNT, CASE_SOFT_404, CASE_SOFT_BORROW, CASE_SOFT_BINDING]
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
