"""真机端到端验证：内容库正文/图集补齐（设计 13 §19.13）。

只做两件事（均为只读语义）：
  1) 用本机既有用户铸一个访问令牌（不新建用户、不改库）；
  2) 调 `GET /api/notes/{id}?hydrate=true`，打印补齐前后正文长度与图集数量。

平台侧只发**详情 GET**（只读边界 / C4）；令牌值与 Cookie 一律不打印。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.core.security import create_access_token  # noqa: E402

DB = ROOT / "data" / "spider_xhs.db"
BASE = "http://127.0.0.1:8000"


def _snapshot(db, note_id: int) -> dict:
    row = db.execute(
        """SELECT id, note_id, length(coalesce(content,'')) AS clen, hydrated_at,
                  (SELECT COUNT(*) FROM note_assets a WHERE a.note_id = notes.id) AS acount
           FROM notes WHERE id = ?""",
        (note_id,),
    ).fetchone()
    return {"id": row[0], "note_id": row[1], "content_len": row[2],
            "hydrated_at": row[3], "assets": row[4]}


def main() -> int:
    note_id = int(sys.argv[1]) if len(sys.argv) > 1 else 32

    db = sqlite3.connect(DB)
    owner = db.execute("SELECT user_id FROM notes WHERE id = ?", (note_id,)).fetchone()
    if owner is None:
        print(f"note {note_id} not found")
        return 1
    user_id = owner[0]  # 用**笔记自己的归属用户**铸令牌（跨用户会 404）

    before = _snapshot(db, note_id)
    print("BEFORE:", json.dumps(before, ensure_ascii=False))

    token = create_access_token(user_id)  # 不打印
    req = urllib.request.Request(
        f"{BASE}/api/notes/{note_id}?hydrate=true",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')[:400]}")
        return 2

    print("RESP hydrate:", json.dumps(body.get("hydrate"), ensure_ascii=False))
    print("RESP content_len:", len(body.get("content") or ""))
    print("RESP account_name:", repr(body.get("account_name")))
    print("RESP asset_urls:", len(body.get("asset_urls") or []))

    after = _snapshot(sqlite3.connect(DB), note_id)
    print("AFTER :", json.dumps(after, ensure_ascii=False))

    ok = (after["content_len"] > before["content_len"] or after["assets"] > before["assets"])
    print("HYDRATE_EFFECTIVE:", ok)
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
