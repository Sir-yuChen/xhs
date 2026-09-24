"""变异验证：把 hydrate 通道加回来，「内容库只读本地」的守卫必须变红。

变异 = 在 `get_note` 里重新引入 `hydrate` 参数并调 `_hydrate_one_note`
（即被删掉的那条路）。两条守卫都必须转红：
  * test_detail_is_local_only_and_never_touches_the_platform（ Spy 适配器会炸 ）
  * test_detail_has_no_hydrate_parameter_in_the_openapi_contract（ 参数回来了 ）
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\work\XHS")
API = ROOT / "backend/app/api/notes.py"
BAK = ROOT / ".workbuddy/mutation-notes-api.bak"

ORIGINAL_ANCHOR = '''    note = _get_owned_note(db, current_user, note_id)
    return _serialize_note_with_tags(db, note)'''

MUTATED = '''    note = _get_owned_note(db, current_user, note_id)
    hydrate_state = _hydrate_one_note(db, current_user, note) if hydrate else None
    result = _serialize_note_with_tags(db, note)
    if hydrate_state is not None:
        result["hydrate"] = hydrate_state
    return result'''

HELPER = '''

def _hydrate_one_note(db, current_user, note):
    return {"status": "failed", "reason": "变异：平台通道被加回来了"}
'''

SIG_ANCHOR = '''def get_note(
    note_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):'''
SIG_MUTATED = '''def get_note(
    note_id: int,
    hydrate: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):'''

TARGETS = [
    "tests/backend/test_note_content_backfill.py::TestLibraryDetailEndpoint::test_detail_is_local_only_and_never_touches_the_platform",
    "tests/backend/test_note_content_backfill.py::TestLibraryDetailEndpoint::test_detail_has_no_hydrate_parameter_in_the_openapi_contract",
]


def run(target: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [r"C:\Users\admin\AppData\Local\Programs\Python\Python312\python.exe",
         "-m", "pytest", target, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True,
    )
    tail = (proc.stdout or "").strip().splitlines()
    return proc.returncode == 0, (tail[-1] if tail else "")


def main() -> int:
    shutil.copy2(API, BAK)
    original = API.read_text(encoding="utf-8")
    print(f"baseline backup -> {BAK}")
    ok = True
    try:
        mutated = original.replace(ORIGINAL_ANCHOR, MUTATED, 1)
        mutated = mutated.replace(SIG_ANCHOR, SIG_MUTATED, 1) + HELPER
        assert mutated != original and "hydrate: bool = False" in mutated, "变异没写进去"
        API.write_text(mutated, encoding="utf-8")
        for t in TARGETS:
            passed, summary = run(t)
            print(f"  {t.split('::')[-1]}: {'GREEN (假守卫 ✗)' if passed else 'RED (承重 ✓)'} | {summary}")
            if passed:
                ok = False
    finally:
        API.write_text(original, encoding="utf-8")
        print(f"\nrestored original ({len(original)} chars)")
    print("\nMUTATION RESULT:", "ALL LOAD-BEARING ✓" if ok else "SOME GUARD IS FAKE ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
