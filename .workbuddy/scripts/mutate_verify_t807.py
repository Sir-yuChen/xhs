"""变异验证：把 bug 注回 accounts-page.tsx，守卫必须变红。

守卫的价值不在「当前是绿的」，而在「改坏了会红」。本项目已有先例（DEF 系列）：
不加这一步，你只是在断言一个巧合。

用法：python .workbuddy/tmp/mutate_verify_t807.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "frontend/src/pages/platforms/xhs/accounts-page.tsx"
PY = r"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe"
TEST_FILE = "tests/backend/test_page_optimizations.py"

# (说明, 原文, 替换为, 期望变红的用例)
MUTATIONS = [
    (
        "去掉原因行的 `…` + tip",
        "ellipsis={{ tooltip: metricsNote }}",
        "",
        "test_accounts_long_lines_collapse_with_tooltip_instead_of_wrapping",
    ),
    (
        "去掉时间行的 `…` + tip",
        "ellipsis={{ tooltip: updatedText }}",
        "",
        "test_accounts_long_lines_collapse_with_tooltip_instead_of_wrapping",
    ),
    (
        "商品同步按钮不再限定 Creator",
        "{isCreator ? (",
        "{true ? (",
        "test_shop_goods_sync_button_is_creator_only",
    ),
    (
        "轮询间隔被改成 3 秒",
        "SYNC_POLL_INTERVAL_MS = 5000",
        "SYNC_POLL_INTERVAL_MS = 3000",
        "test_accounts_sync_polling_uses_five_seconds_and_stops_on_unmount",
    ),
    (
        "轮询依赖漏掉节拍（会静默停在第 2 次）",
        "[noteSyncIds, goodsSyncIds, syncPollTick]",
        "[noteSyncIds, goodsSyncIds]",
        "test_accounts_sync_polling_uses_five_seconds_and_stops_on_unmount",
    ),
    (
        "卸载时不清定时器",
        "return () => clearTimeout(timer);",
        "return undefined;",
        "test_accounts_sync_polling_uses_five_seconds_and_stops_on_unmount",
    ),
    (
        "未知总数从 `-` 改成 0",
        'total ?? "-"',
        "total ?? 0",
        "test_accounts_sync_progress_renders_dash_for_unknown_total",
    ),
    (
        "PC 分支不再要求主页链接（分支取反）",
        'if (account.sub_type === "creator") {',
        'if (account.sub_type !== "creator") {',
        "test_note_sync_requires_profile_url_on_pc",
    ),
    (
        "把令牌持久化进 localStorage",
        "onChange={(event) => setLinkInput(event.target.value)}",
        'onChange={(event) => { setLinkInput(event.target.value); window.localStorage.setItem("xhs_profile_url", event.target.value); }}',
        "test_note_sync_requires_profile_url_on_pc",
    ),
    (
        "把「摘除账号」裹回 finally（第一轮轮询即停摆）",
        """      }
      // ⚠️ **摘除账号必须写在这里，不能写在 `finally` 里**：上面「仍在跑」的分支是
      // `return`，而 `finally` 对 `return` 一样生效 —— 写进 `finally` 就会在**第一轮**
      // 把账号移出集合，轮询当场停摆，卡片进度行永久冻在「已落库 N / 共 M」
      // （症状是「看着像在跑，其实再也没更新过」）。只有真的到终态或这次查询失败才摘除。
      setIds((current) => {
        const next = new Set(current);
        next.delete(accountId);
        return next;
      });
    };""",
        """      } finally {
      setIds((current) => {
        const next = new Set(current);
        next.delete(accountId);
        return next;
      });
      }
    };""",
        "test_accounts_sync_polling_keeps_running_until_terminal",
    ),
    (
        "`running` 不再算「仍在跑」（第一轮就显示结果行）",
        'if (status.status === "pending" || status.status === "running") {',
        'if (status.status === "pending") {',
        "test_accounts_sync_polling_keeps_running_until_terminal",
    ),
]


def run(test_name: str) -> bool:
    """返回 True 表示用例通过。"""
    proc = subprocess.run(
        [PY, "-m", "pytest", f"{TEST_FILE}::{test_name}", "-q", "--tb=no", "-p", "no:cacheprovider"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def main() -> int:
    original = TARGET.read_text(encoding="utf-8")
    failures: list[str] = []

    try:
        for label, old, new, test_name in MUTATIONS:
            if old not in original:
                print(f"[SKIP] {label} —— 原文没找到：{old!r}")
                failures.append(label)
                continue
            TARGET.write_text(original.replace(old, new, 1), encoding="utf-8")
            passed = run(test_name)
            status = "红 ✅" if not passed else "还是绿的 ❌（守卫无效）"
            print(f"[{status}] {label}  →  {test_name}")
            if passed:
                failures.append(label)
    finally:
        TARGET.write_text(original, encoding="utf-8")

    # 复原后再确认一次基线仍是绿的（防止脚本自己把文件改坏）。
    baseline = run("test_accounts_long_lines_collapse_with_tooltip_instead_of_wrapping")
    print(f"\n复原后基线：{'绿 ✅' if baseline else '红 ❌'}")
    print(f"变异验证：{len(MUTATIONS) - len(failures)}/{len(MUTATIONS)} 条按预期变红")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
