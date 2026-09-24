"""在真实库副本上验证对账链路（只读模式，一个字节都不写）。

验两件事：
1. `reconcile_notes` 能跑通（源枚举 → 本地比对 → 出报告）；
2. `apply_fixes=False` 时**确实不写库**（用副本库前后行数/校验和比对）。
"""
import shutil
import sqlite3
import sys

sys.path.insert(0, r"D:\work\XHS")

SRC = r"D:\work\XHS\data\spider_xhs.db"
DST = r"D:\work\XHS\.workbuddy\reconcile-probe.db"

shutil.copy2(SRC, DST)


def fingerprint(path: str) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


before = fingerprint(DST)

# 让 reconcile_service 用副本库
import backend.app.core.config as cfg  # noqa: E402

cfg.get_settings.cache_clear() if hasattr(cfg.get_settings, "cache_clear") else None

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import backend.app.core.database as database  # noqa: E402
from backend.app.models.platform_account import PlatformAccount  # noqa: E402
from backend.app.services import reconcile_service as rs  # noqa: E402

engine = create_engine(f"sqlite:///{DST}", connect_args={"check_same_thread": False})
Session = sessionmaker(bind=engine)
db = Session()
try:
    account = db.get(PlatformAccount, 8)
    print("账号:", account.id, account.nickname, account.sub_type)

    print("\n=== A) 只读对账（apply_fixes=False，scope=full） ===")
    report = rs.reconcile_notes(db, account, scope="full", apply_fixes=False)
    for key in ("checked", "matched", "marked", "unknown_time", "platform_complete", "applied"):
        print(f"  {key} = {report[key]}")
    print("  platform_only 条数:", len(report["platform_only"]))
    print("  local_only    条数:", len(report["local_only"]))
    print("  field_mismatch 条数:", len(report["field_mismatch"]))
    print("  note:", report["note"][:160].encode("ascii","replace").decode())

    print("\n=== B) 时间区间对账（scope=range，2026-08-01 ~ 2026-08-31） ===")
    r2 = rs.reconcile_notes(db, account, scope="range",
                            start_date="2026-08-01", end_date="2026-08-31", apply_fixes=False)
    for key in ("checked", "matched", "unknown_time", "platform_complete"):
        print(f"  {key} = {r2[key]}")
    print("  note:", r2["note"][:160].encode("ascii","replace").decode())
finally:
    db.close()

after = fingerprint(DST)
print("\n库文件是否被改动:", "是（不该！）" if before != after else "否 ✓ 只读模式确实零写入")
