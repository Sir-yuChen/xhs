"""按 Cookie 形态回填 `account_cookie_versions.channel`（§11.5）。

迁移 `g7h8i9j0k1l2` 只能加列（Cookie 是加密存储的，SQL 层看不到键名），
分类必须在应用层用密钥解密后做 —— 就是这个脚本。

用法：
    python backend/scripts/backfill_cookie_channels.py --db data/spider_xhs.db [--apply]
默认 dry-run，只打印将怎么改。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.core.security import decrypt_text  # noqa: E402
from backend.app.services.account_service import CHANNEL_ARK, CHANNEL_WWW, channel_for_cookies  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/spider_xhs.db")
    parser.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    columns = [row[1] for row in db.execute("PRAGMA table_info(account_cookie_versions)")]
    if "channel" not in columns:
        raise SystemExit("还没有 channel 列 —— 先跑 alembic upgrade（迁移 g7h8i9j0k1l2）")

    changed = 0
    for row in db.execute("select id, platform_account_id, encrypted_cookies, channel from account_cookie_versions"):
        try:
            jar = json.loads(decrypt_text(row["encrypted_cookies"]))
        except Exception as exc:  # noqa: BLE001
            print(f"  id={row['id']} 解密失败，跳过：{exc}")
            continue
        want = channel_for_cookies("; ".join(f"{k}={v}" for k, v in jar.items()))
        if want != row["channel"]:
            changed += 1
            print(f"  id={row['id']} acct={row['platform_account_id']}: "
                  f"{row['channel']} → {want}（{len(jar)} 键）")
    print(f"\n共 {changed} 行需要改。")
    if changed and args.apply:
        for row in db.execute("select id, encrypted_cookies from account_cookie_versions"):
            try:
                jar = json.loads(decrypt_text(row["encrypted_cookies"]))
            except Exception:  # noqa: BLE001
                continue
            want = channel_for_cookies("; ".join(f"{k}={v}" for k, v in jar.items()))
            if want != db.execute("select channel from account_cookie_versions where id=?", (row["id"],)).fetchone()[0]:
                db.execute("update account_cookie_versions set channel=? where id=?", (want, row["id"]))
        db.commit()
        print("✅ 已写入")
    elif changed:
        print("（dry-run；加 --apply 才真正写）")
    else:
        print("无需改动")
    db.close()


if __name__ == "__main__":
    main()
