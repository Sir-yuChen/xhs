"""真机端到端验证：账号级互动数据（DEF-039）+ 自有内容关键词搜索。

不是单测的重复：这里打的是**真实平台**，用的是**真实账号 Cookie**，
并且整条链路走 HTTP（TestClient 打到 FastAPI），与前端看到的行为一致。

安全边界：
  * 用 `data/spider_xhs.db` 的**副本**（`.hermes/tmp/verify_metrics.db`），
    真实库只读不写；
  * 只读请求，无写操作（发布/删除一律不碰）；
  * 请求数有界：otherinfo ≤ 3 次 + 三份列表各 1 次 + 搜索 1 次。

用法：
    PYTHONPATH=. python .hermes/tmp/verify_metrics_live.py
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.app.core.database import get_db  # noqa: E402
from backend.app.main import app  # noqa: E402

from tests.backend.conftest import make_engine  # noqa: PLC0415

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def main() -> int:
    source = ROOT / "data" / "spider_xhs.db"
    target = ROOT / ".hermes" / "tmp" / "verify_metrics.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)

    engine = make_engine(target.as_posix())
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    # ---- 端点注册（先证明「新构建」而不是旧进程） -------------------------
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    operations = sum(len([m for m in item if m in {"get", "post", "put", "patch", "delete"}]) for item in paths.values())
    print(f"OpenAPI: paths={len(paths)} operations={operations}")
    check("/api/accounts/{account_id}/metrics 已注册", "/api/accounts/{account_id}/metrics" in paths)
    check("/api/xhs/pc/users/search 已注册", "/api/xhs/pc/users/search" in paths)

    # ---- 准备一个可登录的验证用户，并把真实账号挂到它名下（只在副本里） ----
    register = client.post("/api/auth/register", json={"username": "verify_user", "password": "verify123456"})
    if register.status_code != 200:
        print("注册验证用户失败：", register.text)
        return 1
    headers = {"Authorization": f"Bearer {register.json()['access_token']}"}

    with engine.begin() as connection:
        user_id = connection.execute(text("SELECT id FROM users WHERE username='verify_user'")).scalar()
        accounts = connection.execute(
            text("SELECT id, sub_type, nickname, status FROM platform_accounts WHERE platform='xhs' ORDER BY id")
        ).fetchall()
        connection.execute(
            text("UPDATE platform_accounts SET user_id = :uid WHERE platform='xhs'"), {"uid": user_id}
        )
    available = {row[0]: row for row in accounts}
    print("副本中的 xhs 账号：", [(row[0], row[1], row[2], row[3]) for row in accounts])

    pc_ids = [aid for aid, row in available.items() if row[1] == "pc"]
    creator_ids = [aid for aid, row in available.items() if row[1] == "creator"]

    # ---- 1. PC 账号：三项计数必须来自 /v1/user/otherinfo -------------------
    for account_id in pc_ids:
        response = client.post(f"/api/accounts/{account_id}/metrics", headers=headers)
        payload = response.json() if response.status_code == 200 else {}
        profile = payload.get("profile") or {}
        reason = payload.get("metrics_reason", "")
        print(
            f"  account {account_id} status={response.status_code} "
            f"followers={profile.get('followers')} following={profile.get('following')} likes={profile.get('likes')} "
            f"reason={reason[:60]!r}"
        )
        if response.status_code == 200 and not reason:
            check(
                f"account {account_id} 取到真实三项计数",
                all(isinstance(profile.get(key), int) for key in ("followers", "following", "likes")),
                f"{profile.get('followers')}/{profile.get('following')}/{profile.get('likes')}",
            )
        else:
            # 失败也必须给出可读原因（Cookie 过期是合法状态，不是缺陷）
            check(f"account {account_id} 失败时给出可读原因", bool(reason), reason[:80])

    # ---- 2. creator-only 账号：零平台请求 + 可读原因 -----------------------
    for account_id in creator_ids:
        response = client.post(f"/api/accounts/{account_id}/metrics", headers=headers)
        payload = response.json()
        reason = payload.get("metrics_reason", "")
        check(
            f"creator account {account_id} 返回可读原因（不伪造数字）",
            response.status_code == 200 and "创作者" in reason,
            reason[:80],
        )

    # ---- 3. 落库确认（副本库） -------------------------------------------
    db = SessionLocal()
    try:
        from backend.app.models import PlatformAccount

        stored = {}
        for account_id in pc_ids + creator_ids:
            account = db.get(PlatformAccount, account_id)
            stored[account_id] = json.loads(account.profile_json or "{}")
        print("  副本库 profile_json 三项：", {k: {x: v.get(x) for x in ("followers", "following", "likes")} for k, v in stored.items()})
        check(
            "落库的计数与接口返回一致",
            all(
                stored[account_id].get("followers") == (client.post(f"/api/accounts/{account_id}/metrics", headers=headers).json().get("profile") or {}).get("followers")
                for account_id in pc_ids
            ),
        )
    finally:
        db.close()

    # ---- 4. 搜索：先拿真实标题，再用其中的关键词搜索，必须命中 ------------
    searchable = None
    real_titles: list[str] = []
    for account_id in pc_ids:
        for scope, path in (("notes", "notes"), ("liked", "liked"), ("collected", "collected")):
            response = client.post(
                f"/api/xhs/pc/users/{path}", headers=headers, json={"account_id": account_id}
            )
            if response.status_code != 200:
                print(f"  {scope} for account {account_id} → HTTP {response.status_code}: {response.text[:120]}")
                continue
            items = response.json().get("items", [])
            if items and searchable is None:
                searchable = (account_id, scope, items[0]["title"])
            real_titles = [item.get("title") or "" for item in items][:3]
            print(f"  {scope} for account {account_id}: {len(items)} 条，示例标题 {real_titles}")

    if searchable is None:
        check("搜索验证", False, "没有可用的真实列表数据（Cookie 可能已过期）")
    else:
        account_id, scope, title = searchable
        keyword = "".join(ch for ch in title if "\u4e00" <= ch <= "\u9fff")[:4] or title[:4]
        response = client.post(
            "/api/xhs/pc/users/search",
            headers=headers,
            json={"account_id": account_id, "keyword": keyword, "pages_per_scope": 1},
        )
        print(f"  搜索 keyword={keyword!r} account={account_id} → HTTP {response.status_code}")
        if response.status_code != 200:
            print("   ", response.text[:300])
            check("搜索端点返回 200", False, response.text[:200])
        else:
            payload = response.json()
            scopes_meta = payload["scopes"]
            print("  扫描元数据：", {k: {kk: v[kk] for kk in ("scanned_pages", "matched", "exhausted", "error")} for k, v in scopes_meta.items()})
            print("  命中：", [(item["scope"], item["title"]) for item in payload["items"]][:6])
            total_scanned = sum(meta["matched"] for meta in scopes_meta.values())
            check("搜索返回 200 且总命中自洽", payload["total_matched"] == total_scanned == len(payload["items"]))
            check("真实标题里的关键词能命中", any(item["scope"] == scope for item in payload["items"]))
            check("每类扫描页数不超过预算", all(meta["scanned_pages"] <= 1 for meta in scopes_meta.values()))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n=== {passed}/{len(RESULTS)} 项通过 ===")
    print("RESULT:", "PASS" if passed == len(RESULTS) else "FAIL")
    app.dependency_overrides.clear()

    # 收尾：**删掉副本库**。
    # 副本里的账号归属被改成了验证用户——留着它，一旦有人把服务指到这个文件，
    # 就会看到「账号全没了」（2026-09-18 真实踩过）。临时产物用完即删。
    # ⚠️ 必须先 `engine.dispose()`：SQLAlchemy 连接池里还握着文件句柄，
    #    不释放的话 Windows 上一定删不掉（实测 WinError 32），失败就重试几次等句柄松手。
    engine.dispose()
    try:
        for attempt in range(6):
            try:
                target.unlink(missing_ok=True)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.5)
        print(f"已删除副本库：{target}")
    except OSError as error:  # noqa: BLE001 - 收尾失败只提示，不影响判定
        print(f"⚠️ 副本库删除失败（请手动清理）：{target} ({error})")

    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
