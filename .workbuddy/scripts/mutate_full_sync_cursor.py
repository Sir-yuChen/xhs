"""变异验证：把「全量也推进游标」改回旧实现，新守卫必须变红。

旧实现（bug）：
  note_sync_service._sync_source 里
      if incremental and items:      ← 全量模式不算游标
  _sync_with_task 里
      if incremental and cursor_advanced:   ← 全量模式不存游标
改回这两处后：
  * test_full_sync_advances_the_cursor_so_next_run_is_incremental 必须红
  * test_cursor_means_incremental / test_no_cursor_means_full 不应受影响（纯 resolver）
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\work\XHS")
SVC = ROOT / "backend/app/services/note_sync_service.py"
BAK = ROOT / ".workbuddy/mutation-note-sync.bak"

MUTATIONS = [
    (
        "A: _sync_source 里全量不算游标（改回 if incremental and items）",
        "        if items:\n"
        "            # D13：游标取「已成功落库行的最大值」",
        "        if incremental and items:\n"
        "            # D13：游标取「已成功落库行的最大值」",
    ),
    (
        "B: _sync_with_task 里全量不存游标（改回 if incremental and cursor_advanced）",
        "    if cursor_advanced:\n"
        "        # D13：游标只在「本次同步落库成功」之后推进。",
        "    if incremental and cursor_advanced:\n"
        "        # D13：游标只在「本次同步落库成功」之后推进。",
    ),
]

TARGET = "tests/backend/test_note_sync.py::test_full_sync_advances_the_cursor_so_next_run_is_incremental"
RESOLVER = [
    "tests/backend/test_incremental_sync.py::TestAutoModeResolution",
]


def run(target: str) -> bool:
    proc = subprocess.run(
        [
            r"C:\Users\admin\AppData\Local\Programs\Python\Python312\python.exe",
            "-m", "pytest", target, "-q", "--no-header", "-p", "no:cacheprovider",
        ],
        cwd=ROOT, capture_output=True, text=True,
    )
    tail = (proc.stdout or "").strip().splitlines()
    return proc.returncode == 0, (tail[-1] if tail else "")


def main() -> int:
    shutil.copy2(SVC, BAK)
    original = SVC.read_text(encoding="utf-8")
    print(f"baseline backup -> {BAK}")
    ok = True
    try:
        for label, old, new in MUTATIONS:
            assert old in original, f"anchor not found for {label}"
            SVC.write_text(original.replace(old, new, 1), encoding="utf-8")
            passed, summary = run(TARGET)
            status = "RED (承重 ✓)" if not passed else "GREEN (假守卫 ✗)"
            print(f"\n[{label}]\n  {TARGET.split('::')[-1]}: {status}  | {summary}")
            if passed:
                ok = False
            # resolver 用例是纯函数，不该被这两个变异影响
            for r in RESOLVER:
                rp, rs = run(r)
                print(f"  {r.split('::')[-1]}: {'still green' if rp else 'UNEXPECTED RED'} | {rs}")
                if not rp:
                    ok = False
    finally:
        SVC.write_text(original, encoding="utf-8")
        print(f"\nrestored original ({len(original)} chars)")
    print("\nMUTATION RESULT:", "ALL LOAD-BEARING ✓" if ok else "SOME GUARD IS FAKE ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
