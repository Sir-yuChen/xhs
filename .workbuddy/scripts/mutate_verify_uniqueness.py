"""唯一性守卫的变异验证（台账 §4.9）。

**为什么必须做**：一条「永远是绿的」断言等于没有断言。本脚本把每条修复
**逐个注回**，同一套用例必须变红；复原后基线必须重新全绿。

覆盖 8 类变异，对应三条不变量 I1 / I2 / I3：

| # | 注回的 bug | 期望变红的用例 |
|---|---|---|
| 1 | 删掉账号部分唯一索引 | 裸插重复账号 |
| 2 | 删掉笔记唯一索引 | 裸插重复笔记 |
| 3 | 不刷新笔记的 `platform_account_id` | 归属重挂 |
| 4 | 去掉「复活」分支 | 删除后重新导入 |
| 5 | 去掉空 `external_user_id` 的昵称兜底 | 空 id 不新建 |
| 6 | 列表不过滤软删 | 软删后列表隐藏 |
| 7 | 按 id 端点不查软删 | 软删后 404 |
| 8 | 删除改成物理删 | 软删后行仍在位 |

**脚本自身的坑**（踩过一次）：`old` 片段必须在文件里**唯一**，否则改的是别处、
变异「通过」却什么都没验证到 —— 每个 `old` 都在这里手工确认过出现次数。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = sys.executable
TEST_FILE = "tests/backend/test_uniqueness.py"

MODEL_ACCOUNT = ROOT / "backend/app/models/platform_account.py"
MODEL_NOTE = ROOT / "backend/app/models/note.py"
CRAWL = ROOT / "backend/app/api/platforms/xhs/crawl.py"
ACCOUNT_SERVICE = ROOT / "backend/app/services/account_service.py"
ACCOUNTS_API = ROOT / "backend/app/api/accounts.py"

T_DUP_ACCOUNT = f"{TEST_FILE}::test_db_rejects_duplicate_account_even_bypassing_service"
T_DUP_NOTE = f"{TEST_FILE}::test_db_rejects_duplicate_note_even_bypassing_service"
T_REPIN = f"{TEST_FILE}::test_note_upsert_repins_platform_account_id"
T_REVIVE = f"{TEST_FILE}::test_reimport_after_delete_revives_instead_of_creating"
T_EMPTY_ID = f"{TEST_FILE}::test_empty_external_id_falls_back_to_nickname"
T_HIDDEN = f"{TEST_FILE}::test_delete_account_is_soft_and_hides_it_from_list"
T_404 = f"{TEST_FILE}::test_deleted_account_id_endpoints_return_404"


MUTATIONS = [
    (
        "① 账号唯一索引改成非唯一（I1 的 DB 兜底没了）",
        MODEL_ACCOUNT,
        '            unique=True,\n            sqlite_where=text(',
        '            unique=False,\n            sqlite_where=text(',
        [T_DUP_ACCOUNT],
    ),
    (
        "② 删掉笔记唯一约束（I2 的 DB 兜底没了）",
        MODEL_NOTE,
        '        UniqueConstraint("user_id", "note_id", name="uq_notes_user_note"),\n',
        "",
        [T_DUP_NOTE],
    ),
    (
        "③ 覆盖更新时不重挂笔记归属（旧实现的 bug）",
        CRAWL,
        "            note.platform_account_id = account.id\n",
        "            pass\n",
        [T_REPIN],
    ),
    (
        "④ 不清理 deleted_at（软删行复活后仍是「已删」）",
        ACCOUNT_SERVICE,
        "        account.deleted_at = None\n        action = \"restored\"\n",
        "        pass\n        action = \"restored\"\n",
        [T_REVIVE],
    ),
    (
        "⑤ 去掉空 external_user_id 的昵称兜底（会无界新建）",
        ACCOUNT_SERVICE,
        "    elif nickname:",
        "    elif False:",
        [T_EMPTY_ID],
    ),
    (
        "⑥ 列表不按软删过滤（软删账号照样下发）",
        ACCOUNTS_API,
        "        PlatformAccount.deleted_at.is_(None),\n    )\n    if platform:",
        "    )\n    if platform:",
        [T_HIDDEN],
    ),
    (
        "⑦ 按 id 取账号不检查软删（旧页面 id 仍可用）",
        ACCOUNTS_API,
        "    if account is None or account.user_id != current_user.id or account.deleted_at is not None:",
        "    if account is None or account.user_id != current_user.id:",
        [T_404],
    ),
    (
        "⑧ 删除改成物理删（身份行消失 → 重新导入必新建）",
        ACCOUNTS_API,
        "    already_deleted = account.deleted_at is not None\n"
        "    if not already_deleted:\n"
        "        account.deleted_at = shanghai_now()\n"
        "        account.updated_at = account.deleted_at\n",
        "    already_deleted = False\n    db.delete(account)\n",
        [T_HIDDEN],
    ),
]


def run_pytest(node_ids: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", *node_ids, "-q", "-p", "no:cacheprovider", "--no-header", "-x"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    print("=== 基线（未变异）应当全绿 ===")
    all_tests = sorted({t for _, _, _, _, tests in MUTATIONS for t in tests})
    code, out = run_pytest(all_tests)
    tail = [line for line in out.strip().splitlines() if "passed" in line or "failed" in line]
    print("   ", tail[-1] if tail else out.strip()[-200:])
    if code != 0:
        print("❌ 基线没有全绿，先修用例再谈变异验证")
        return 1

    failures: list[str] = []
    print("\n=== 逐条变异（每条都必须变红）===")
    for label, path, old, new, tests in MUTATIONS:
        original = path.read_text(encoding="utf-8")
        occurrences = original.count(old)
        if occurrences != 1:
            print(f"❌ {label}\n   锚点在 {path.name} 里出现 {occurrences} 次（必须恰好 1 次）")
            failures.append(label + "（锚点不唯一）")
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            code, out = run_pytest(tests)
            tail = [line for line in out.strip().splitlines() if "passed" in line or "failed" in line]
            verdict = "红 ✅" if code != 0 else "还是绿的 ❌"
            print(f"  {verdict}  {label}\n        （{tail[-1] if tail else '无摘要'}）")
            if code == 0:
                failures.append(label)
        finally:
            path.write_text(original, encoding="utf-8")

    print("\n=== 复原后基线必须重新全绿 ===")
    code, out = run_pytest(all_tests)
    tail = [line for line in out.strip().splitlines() if "passed" in line or "failed" in line]
    print("   ", tail[-1] if tail else out.strip()[-200:])
    restored = code == 0

    print("\n================ 结论 ================")
    if failures or not restored:
        for item in failures:
            print("  ❌ 未按预期变红：", item)
        if not restored:
            print("  ❌ 复原后基线没有回到全绿")
        return 1
    print(f"  ✅ 变异验证 {len(MUTATIONS)}/{len(MUTATIONS)} 全部按预期变红；复原后基线全绿")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
