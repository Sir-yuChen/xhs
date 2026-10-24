"""Fernet 密钥轮换：旧 secret_key 派生密钥解密 → 新（当前）密钥重加密。

## 事故（2026-10-25，用户报「小红书后端大量接口 500」）

v1.1.0 安全加固轮换了 `security.secret_key`（1.0.7 时代 → 迁移时换新值）。
`fernet_key` 一直为空 ⇒ 加密密钥从 `secret_key` 派生（`core/security.py::
_derive_fernet_key`）—— **secret_key 一换，派生密钥全变**，而
`sqlite_to_pg` 迁移过来的密文（Cookie / 网关令牌）还是旧密钥加密的
⇒ 凡走 `decrypt_text` 的接口全线 500（`/api/accounts` 等）。

## 本脚本做什么（幂等，可重复跑）

对 4 张含密文的表：每行先试**当前密钥**（已轮换过的行原样跳过），
再试**旧 secret_key 派生密钥**（命中 ⇒ 用当前密钥重加密写回）；
两边都解不开 ⇒ 记入 corrupt 清单**原样保留**（不删不猜 —— 缺值口径，
由调用方的逐行兜底降级）。跑前必须已有表级备份（`.hermes/backups/`）。

## 旧 secret_key 的来源

`git show 1eaaeb4:config/default.yaml`（1.0.7 发版，桌面数据用它加密）。
从 git 历史现场读取，不硬编码进本文件。

用法::

    python backend/scripts/rotate_fernet_key.py --old-secret-file <路径或 ->（- = git show）
    python backend/scripts/rotate_fernet_key.py --old-secret-file - --verify   # 只报告不写
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.fernet import Fernet, InvalidToken  # noqa: E402

from backend.app.core.config import get_settings  # noqa: E402
from backend.app.core.security import get_fernet  # noqa: E402

#: (表, 主键列, 密文列)
TARGETS: tuple[tuple[str, str, str], ...] = (
    ("account_cookie_versions", "id", "encrypted_cookies"),
    # ⚠️ platform_accounts 没有密文列（encrypted_cookies 只在 account_cookie_versions；
    #    模型上的字段是历史遗留认知）—— 2026-10-25 以 information_schema 实测为准。
    ("login_sessions", "id", "encrypted_temp_cookies"),
    ("llm_gateway_configs", "id", "access_token"),
)


def _derive(secret: str) -> Fernet:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def old_fernet_from_git(rev: str) -> Fernet:
    """从 git 历史现场读旧 secret_key 并派生 Fernet（密钥不落盘、不打印）。"""
    out = subprocess.run(
        ["git", "show", f"{rev}:config/default.yaml"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8")
    import yaml

    secret = (yaml.safe_load(out).get("security") or {}).get("secret_key")
    if not secret:
        raise SystemExit(f"{rev} 的 config/default.yaml 没有 security.secret_key")
    return _derive(secret)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-secret-file", default="-",
                        help="旧 secret_key 来源：'-' = git show 1eaaeb4（1.0.7 发版），或一个文件路径")
    parser.add_argument("--old-rev", default="1eaaeb4")
    parser.add_argument("--verify", action="store_true", help="只报告，不写库")
    args = parser.parse_args(argv)

    if args.old_secret_file == "-":
        old_fernet = old_fernet_from_git(args.old_rev)
    else:
        old_fernet = _derive(Path(args.old_secret_file).read_text(encoding="utf-8").strip())

    current_fernet = get_fernet()
    assert current_fernet != old_fernet, "新旧密钥相同 —— 无需轮换"

    import psycopg

    conn = psycopg.connect(get_settings().database_url.replace("+psycopg", ""), connect_timeout=10)
    conn.autocommit = False
    cur = conn.cursor()
    rotated = corrupt = 0
    for table, pk, column in TARGETS:
        cur.execute(f"SELECT {pk}, {column} FROM {table} WHERE {column} IS NOT NULL AND {column} <> ''")
        for row_id, ciphertext in cur.fetchall():
            if isinstance(ciphertext, str) is False:
                continue
            # ① 当前密钥能解开 ⇒ 已是目标态（幂等重跑）
            try:
                current_fernet.decrypt(ciphertext.encode("utf-8"))
                continue
            except InvalidToken:
                pass
            # ② 旧密钥能解开 ⇒ 轮换
            try:
                plain = old_fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
            except InvalidToken:
                print(f"[corrupt] {table}.{pk}={row_id} —— 新旧密钥都解不开，原样保留（调用方逐行降级）")
                corrupt += 1
                continue
            if args.verify:
                rotated += 1
                continue
            re_encrypted = current_fernet.encrypt(plain.encode("utf-8")).decode("utf-8")
            cur.execute(f"UPDATE {table} SET {column} = %s WHERE {pk} = %s", (re_encrypted, row_id))
            rotated += 1
    if args.verify:
        conn.rollback()
        print(f"[verify] 可轮换 {rotated} 行，corrupt {corrupt} 行（未写库）")
    else:
        conn.commit()
        print(f"[done] 轮换 {rotated} 行，corrupt {corrupt} 行（corrupt 行原样保留）")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
