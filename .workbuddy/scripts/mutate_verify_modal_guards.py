"""定向变异：验证改指共用组件后的弹窗守卫**仍然有效**（不是恒真的假守卫）。

每个变异：备份 -> 改写 -> 只跑目标用例 -> 断言必须 FAIL -> 还原。
变异刻意分两类：
  * **超串型**（把 token 变成「以它开头」的长串）—— 专抓 `x in source` 这种子串断言；
  * **真删除/改名型** —— 抓「实现真的不在了」。
任何一步不符预期就报错退出，避免留下被改坏的源码。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
MODAL = ROOT / "frontend/src/components/platforms/note-detail-modal.tsx"
LIBRARY = ROOT / "frontend/src/pages/platforms/xhs/library-page.tsx"
MY_NOTES = ROOT / "frontend/src/pages/platforms/xhs/my-notes-page.tsx"

CLASSES = "test_note_detail_uses_the_shared_layout_classes"
GREY = "test_note_detail_modal_body_does_not_scroll"
QUIET = "test_quiet_reason_is_actionable_not_a_wait_and_see"
THREE = "test_failures_are_rendered_by_a_three_way_rule"
DEGRADE = "test_my_notes_failures_degrade_to_a_quiet_line"

MUTATIONS = [
    ("M1 弹窗高度 72vh→70vh", MODAL, 'height: "72vh"', 'height: "70vh"', GREY),
    ("M2 类名改真名（子串破坏）", MODAL, '"note-detail-body"', '"nd-body-mut"', CLASSES),
    ("M2b 类名加后缀（超串，曾蒙过）", MODAL, '"note-detail-body"', '"note-detail-body-mut"', CLASSES),
    ("M3 标签加后缀（超串）", LIBRARY, "<NoteDetailModal", "<NoteDetailModalMut", CLASSES),
    ("M3b 内容库改用别的弹窗（真移除）", LIBRARY, "<NoteDetailModal", "<OtherDetailView", CLASSES),
    (
        "M3c 内容库改从别处导入",
        LIBRARY,
        'from "../../../components/platforms/note-detail-modal"',
        'from "../../../components/platforms/other-modal"',
        CLASSES,
    ),
    (
        "M6 我的笔记改从别处导入",
        MY_NOTES,
        'from "../../../components/platforms/note-detail-modal"',
        'from "../../../components/platforms/other-modal"',
        DEGRADE,
    ),
    ("M4 短文案变成「请稍后再试」", MODAL, "需在网页版重新登录", "请稍后再试", QUIET),
    ("M5 三分法入口改名", MODAL, "function renderFailure(", "function renderFailureMut(", THREE),
]


def run_one(test_name: str) -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", f"tests/backend/test_page_optimizations.py::{test_name}", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    bad: list[str] = []
    for label, path, old, new, test_name in MUTATIONS:
        data = path.read_bytes()
        text = data.decode("utf-8")
        if old not in text:
            print(f"[SKIP] {label} -- 锚点不存在：{old!r}")
            bad.append(label)
            continue
        try:
            path.write_bytes(text.replace(old, new).encode("utf-8"))
            code, out = run_one(test_name)
            tail = [ln for ln in out.splitlines() if "passed" in ln or "failed" in ln][-1:]
            if code != 0:
                print(f"[OK  ] {label} -> 变红（守卫有效） {' '.join(tail)}")
            else:
                print(f"[BAD ] {label} -> 仍然绿（守卫失效！） {' '.join(tail)}")
                bad.append(label)
        finally:
            path.write_bytes(data)
        if path.read_bytes() != data:
            print(f"[FATAL] {path} 还原失败")
            return 2
    print()
    if bad:
        print(f"MUTATION: FAIL —— {len(bad)} 个变异未被抓住：")
        for b in bad:
            print("  -", b)
        return 1
    print(f"MUTATION: PASS —— {len(MUTATIONS)} 个变异全部被抓住")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
