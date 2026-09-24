"""定向变异：验证「详情补齐 + 来源账号」这批新守卫**真的会红**。

守护对象（`tests/backend/test_note_content_backfill.py`）：

| 变异 | 应该抓住它的用例 | 抓的是什么 |
|---|---|---|
| M1 图片改成重复追加 | `test_images_are_merged_not_replaced` | 填空语义（不破坏已有资产） |
| M2 失败不写 `hydrated_at` | `test_failure_is_silent_but_marks_attempted` | 防饿死（失败也占名额一次） |
| M3 候选只挑「已补齐」的行 | `test_candidates_cover_older_rows_across_rounds` | **跨轮累积**（老行必须能补） |
| M4 `attempted` 计数提前 | `test_rate_limit_stops_the_batch_and_reports_pending` | 限流被挡的条数要如实上报 |
| M5 详情 URL 不拼 token | `test_detail_url_carries_the_note_level_token` | DEF-053：无 token = 假成功 |
| M6 端点总去补齐 | `test_detail_without_hydrate_never_touches_the_platform` | 不带 hydrate 必须零平台请求 |
| M7 `account_name` 恒空 | `test_detail_reports_the_source_account` | 来源账号确实回传 |

每个变异：备份 -> 改写 -> 只跑目标用例 -> 必须 FAIL -> 还原。
任何一步不符预期就报错退出，避免留下被改坏的源码。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
SERVICE = ROOT / "backend/app/services/note_sync_service.py"
API = ROOT / "backend/app/api/notes.py"
TARGET_FILE = "tests/backend/test_note_content_backfill.py"

MUTATIONS = [
    (
        "M1 图片改成重复追加（去掉去重）",
        SERVICE,
        "        if not url or url in existing:\n            continue",
        "        if not url:\n            continue",
        "test_images_are_merged_not_replaced",
    ),
    (
        "M2 失败不写 hydrated_at",
        SERVICE,
        "        logger.warning(\"note sync: detail hydrate failed (%s): %s\", note.note_id, exc)\n"
        "        note.hydrated_at = shanghai_now()\n",
        "        logger.warning(\"note sync: detail hydrate failed (%s): %s\", note.note_id, exc)\n",
        "test_failure_is_silent_but_marks_attempted",
    ),
    (
        "M3 候选只挑已补齐的行",
        SERVICE,
        "                Note.hydrated_at.is_(None),",
        "                Note.hydrated_at.is_not(None),",
        "test_candidates_cover_older_rows_across_rounds",
    ),
    (
        "M4 attempted 在限流判断之前累加",
        SERVICE,
        "        if not limiter.allow(f\"{account.id}:www\"):\n            break\n        attempted += 1",
        "        attempted += 1\n        if not limiter.allow(f\"{account.id}:www\"):\n            break",
        "test_rate_limit_stops_the_batch_and_reports_pending",
    ),
    (
        "M5 详情 URL 不拼笔记级 token",
        SERVICE,
        '    token = str(raw.get("xsec_token") or "").strip()',
        '    token = ""',
        "test_detail_url_carries_the_note_level_token",
    ),
    (
        "M6 不带 hydrate 也去补齐",
        API,
        "    hydrate_state = _hydrate_one_note(db, current_user, note) if hydrate else None",
        "    hydrate_state = _hydrate_one_note(db, current_user, note)",
        "test_detail_without_hydrate_never_touches_the_platform",
    ),
    (
        "M7 account_name 恒为空串",
        API,
        '        "account_name": (account.nickname or "") if account is not None else "",',
        '        "account_name": "",',
        "test_detail_reports_the_source_account",
    ),
]


def run_one(test_name: str) -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", f"{TARGET_FILE}::{test_name}", "-q"],
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
            print(f"[SKIP] {label} -- 锚点不存在：{old[:60]!r}")
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
        for item in bad:
            print("  -", item)
        return 1
    print(f"MUTATION: PASS —— {len(MUTATIONS)} 个变异全部被抓住")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
