"""账号 / 笔记落库唯一性回归（台账 §4.9 · 不变量 I1 / I2 / I3）。

需求源：用户报出「相同账号重复落库、笔记也有类似问题」，并明确
**重复数据只保留最新**、**要一并考虑账号删除**。

本文件锁的是**三条不变量**，而不是复述实现：

* **I1 账号唯一** —— 同一 `(user_id, platform, sub_type, external_user_id)` 只能一行；
  `external_user_id` 为空时用 `(user_id, platform, sub_type, nickname)` 兜底，
  **既不伪造 id，也不无界新建**。
* **I2 笔记唯一** —— 同一 `(user_id, note_id)` 只能一行，落库即**覆盖更新**，
  且**不换 `notes.id`**；命中既有行时要**重挂归属**（旧实现漏了这步 → 账号删除后笔记永久悬空）。
* **I3 删除不留孤儿** —— 账号删除是**软删**：行留在原位、列表不出现、按 id 的端点 404，
  重新导入时**复活**（而不是新建）；它名下的 `notes` 不悬空。

⚠️ 本文件有两类用例，**都必须存在**：
① 走接口/服务层的**行为**测试（证明业务流程正确）；
② 直接对 DB 下手的**约束**测试（证明**绕过业务层**也拦得住 —— 这正是当初 7 条
   `ext-incr` 重复账号能落库的原因：当时只有业务层检查，表上没有任何约束）。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.api.platforms.xhs.crawl import _save_normalized_notes
from backend.app.core.database import Base, get_db
from backend.app.main import app
from backend.app.models import AccountCookieVersion, Note, PlatformAccount, User
from backend.app.services.account_service import upsert_platform_account_from_login

client = TestClient(app)

COOKIE_PAYLOAD = json.dumps({"a1": "uniq-a1", "web_session": "uniq-session"})


def _make_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'uniqueness.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _wire(tmp_path):
    """建库 + 覆盖 `get_db` + 注册一个用户；返回 (SessionLocal, headers, user_id)。"""
    SessionLocal = _make_db(tmp_path)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    response = client.post(
        "/api/auth/register", json={"username": "uniq-owner", "password": "secret123"}
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    db = SessionLocal()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "uniq-owner"))
    finally:
        db.close()
    return SessionLocal, headers, user_id


# ---------------------------------------------------------------------------
# I1 · 账号唯一
# ---------------------------------------------------------------------------


def test_upsert_same_identity_twice_keeps_exactly_one_row(tmp_path):
    """同一身份连续导入两次 → 仍只有一行，第二次是 `updated` 而不是 `created`。

    这是用户报出的原始症状的反面：修复前表上没有约束，任何绕过这段判断的路径
    （实测：7 条 `ext-incr`）都能造重复。
    """
    SessionLocal, _headers, user_id = _wire(tmp_path)
    info = {"external_user_id": "ext-1", "nickname": "一号店", "avatar_url": "", "profile": {}}
    db = SessionLocal()
    try:
        first, action_first = upsert_platform_account_from_login(
            db=db, user_id=user_id, platform="xhs", sub_type="pc",
            user_info=info, cookies_text=COOKIE_PAYLOAD,
        )
        db.commit()
        second, action_second = upsert_platform_account_from_login(
            db=db, user_id=user_id, platform="xhs", sub_type="pc",
            user_info=info, cookies_text=COOKIE_PAYLOAD,
        )
        db.commit()
        rows = db.scalars(select(PlatformAccount)).all()
    finally:
        db.close()

    assert action_first == "created"
    assert action_second == "updated"
    assert len(rows) == 1, f"同一身份落了两行：{[r.id for r in rows]}"
    assert first.id == second.id


def test_pc_and_creator_stay_two_rows_same_identity(tmp_path):
    """同一小红书账号的 `pc` / `creator` 必须**仍是两行**（别「顺手合并」）。

    两条链路是**有意**的（`_sync_creator_account_from_pc_cookie` 会从 PC Cookie 派生
    创作者账号；卡片上明确区分「类型」），且发布闸门要求 `sub_type == "creator"`。
    唯一键若不含 `sub_type`，这里会被合成一条、**丢掉发布能力**。
    """
    SessionLocal, _headers, user_id = _wire(tmp_path)
    info = {"external_user_id": "ext-same", "nickname": "双链路号", "avatar_url": "", "profile": {}}
    db = SessionLocal()
    try:
        for sub_type in ("pc", "creator"):
            upsert_platform_account_from_login(
                db=db, user_id=user_id, platform="xhs", sub_type=sub_type,
                user_info=info, cookies_text=COOKIE_PAYLOAD,
            )
            db.commit()
        rows = db.scalars(select(PlatformAccount)).all()
    finally:
        db.close()

    assert len(rows) == 2
    assert {r.sub_type for r in rows} == {"pc", "creator"}


def test_empty_external_id_falls_back_to_nickname(tmp_path):
    """`external_user_id` 为空时靠昵称兜底 —— **不无界新建**（空 id 不参与唯一索引）。

    这是部分唯一索引留下的口子，必须在业务层补上：没有这段兜底，每次导入都会
    新建一行，而 DB 层（正确地）拦不住它。
    """
    SessionLocal, _headers, user_id = _wire(tmp_path)
    info = {"external_user_id": "", "nickname": "没有ID的号", "avatar_url": "", "profile": {}}
    db = SessionLocal()
    try:
        for _ in range(3):
            upsert_platform_account_from_login(
                db=db, user_id=user_id, platform="xhs", sub_type="pc",
                user_info=info, cookies_text=COOKIE_PAYLOAD,
            )
            db.commit()
        rows = db.scalars(select(PlatformAccount)).all()
    finally:
        db.close()

    assert len(rows) == 1, f"空 external_user_id 被无界新建：{[r.id for r in rows]}"


def test_db_rejects_duplicate_account_even_bypassing_service(tmp_path):
    """**绕过业务层**直接插重复账号也必须被 DB 拒绝（I1 的兜底）。

    没有这条，就退回成「只有约定、没有约束」——而那正是 7 条重复账号落库的原因。
    """
    SessionLocal, _headers, user_id = _wire(tmp_path)
    db = SessionLocal()
    try:
        for _ in range(2):
            db.add(
                PlatformAccount(
                    user_id=user_id, platform="xhs", sub_type="creator",
                    external_user_id="ext-dup", nickname="裸插", status="active",
                )
            )
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------------------------
# I2 · 笔记唯一 + 覆盖更新 + 归属重挂
# ---------------------------------------------------------------------------


def _normalized(note_id: str, title: str) -> dict:
    return {
        "note_id": note_id,
        "title": title,
        "content": "",
        "author_name": "作者",
        "image_urls": [],
        "video_url": "",
        "raw": {},
    }


def test_note_upsert_overwrites_in_place_and_keeps_id(tmp_path):
    """同一 `note_id` 反复落库 → **只有一行**、`notes.id` 不变、字段被覆盖。

    `id` 不变是硬要求：`note_assets` / `note_tags` / 草稿都指向它，
    换 id 等于把用户的标签和草稿一起打断。
    """
    SessionLocal, _headers, user_id = _wire(tmp_path)
    db = SessionLocal()
    try:
        account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="creator",
            external_user_id="ext-note", nickname="笔记号", status="active",
        )
        db.add(account)
        db.commit()

        _save_normalized_notes(db, account, [_normalized("note-1", "第一版")])
        first_id = db.scalar(select(Note.id).where(Note.note_id == "note-1"))
        _save_normalized_notes(db, account, [_normalized("note-1", "第二版")])
        rows = db.scalars(select(Note).where(Note.note_id == "note-1")).all()
    finally:
        db.close()

    assert len(rows) == 1, f"同一 note_id 落了两行：{[r.id for r in rows]}"
    assert rows[0].id == first_id, "覆盖更新换了 notes.id —— 素材/标签/草稿的引用会断"
    assert rows[0].title == "第二版", "覆盖更新没写进新内容"


def test_note_upsert_repins_platform_account_id(tmp_path):
    """命中既有行时必须把归属**重挂**到当前账号（台账 §4.9 / 问题 P2）。

    旧实现只覆盖 `title/content/raw_json`，不刷新 `platform_account_id`：
    账号删了再导入会拿到新的 id，老笔记就**永久挂在已删除的账号上**，
    页面上表现为「同步成功但一条都看不到」。
    """
    SessionLocal, _headers, user_id = _wire(tmp_path)
    db = SessionLocal()
    try:
        old_account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="creator",
            external_user_id="ext-old", nickname="老账号", status="active",
        )
        new_account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="creator",
            external_user_id="ext-new", nickname="新账号", status="active",
        )
        db.add_all([old_account, new_account])
        db.commit()

        _save_normalized_notes(db, old_account, [_normalized("note-repin", "内容")])
        _save_normalized_notes(db, new_account, [_normalized("note-repin", "内容")])
        note = db.scalar(select(Note).where(Note.note_id == "note-repin"))
        # ⚠️ 必须在会话内取出裸值：`db.close()` 之后 ORM 实例已 detached，
        # 再读属性会抛 DetachedInstanceError（本文件第一版就是这么红的）。
        note_account_id = note.platform_account_id
        expected_account_id = new_account.id
    finally:
        db.close()

    assert note_account_id == expected_account_id, (
        "归属没重挂：账号换行后这条笔记仍指向旧账号（悬空）"
    )


def test_db_rejects_duplicate_note_even_bypassing_service(tmp_path):
    """直接插两条同 `(user_id, note_id)` 的笔记必须被 DB 拒绝。"""
    SessionLocal, _headers, user_id = _wire(tmp_path)
    db = SessionLocal()
    try:
        account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="pc",
            external_user_id="ext-dup-note", nickname="号", status="active",
        )
        db.add(account)
        db.commit()
        for _ in range(2):
            db.add(
                Note(
                    user_id=user_id, platform_account_id=account.id,
                    platform="xhs", note_id="note-dup", title="", content="",
                )
            )
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------------------------
# I3 · 删除不留孤儿（软删 + 复活）
# ---------------------------------------------------------------------------


def _seed_account(SessionLocal, user_id: int, note_id: str | None = None) -> int:
    db = SessionLocal()
    try:
        account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="creator",
            external_user_id="ext-soft", nickname="待删号", status="active",
        )
        db.add(account)
        db.commit()
        db.add(
            AccountCookieVersion(
                platform_account_id=account.id,
                encrypted_cookies=COOKIE_PAYLOAD,
            )
        )
        db.commit()
        if note_id:
            _save_normalized_notes(db, account, [_normalized(note_id, "删除前同步的笔记")])
        return account.id
    finally:
        db.close()


def test_delete_account_is_soft_and_hides_it_from_list(tmp_path):
    """删除 → 列表看不到、Cookie 被物理清掉、**行仍在位**（`deleted_at` 非空）。"""
    SessionLocal, headers, user_id = _wire(tmp_path)
    account_id = _seed_account(SessionLocal, user_id)

    response = client.delete(f"/api/accounts/{account_id}", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["soft"] is True

    listing = client.get("/api/accounts", headers=headers)
    assert listing.status_code == 200
    assert all(row["id"] != account_id for row in listing.json()["items"]), "软删账号仍出现在列表里"

    db = SessionLocal()
    try:
        row = db.get(PlatformAccount, account_id)
        cookies = db.scalars(
            select(AccountCookieVersion).where(
                AccountCookieVersion.platform_account_id == account_id
            )
        ).all()
    finally:
        db.close()

    assert row is not None, "账号被物理删除了 —— 唯一索引失去意义、笔记会变孤儿"
    assert row.deleted_at is not None
    assert cookies == [], "账号已删除，Cookie 版本必须清掉"


def test_deleted_account_id_endpoints_return_404(tmp_path):
    """软删后按 id 的端点必须 404 —— 用户手里的旧页面 id 不能继续可用。"""
    SessionLocal, headers, user_id = _wire(tmp_path)
    account_id = _seed_account(SessionLocal, user_id)
    client.delete(f"/api/accounts/{account_id}", headers=headers)

    assert client.get(f"/api/accounts/{account_id}/notes/sync/status", headers=headers).status_code == 404
    assert client.post(f"/api/accounts/{account_id}/check", headers=headers).status_code == 404


def test_reimport_after_delete_revives_instead_of_creating(tmp_path):
    """**「删了再导入又来一条」的根治**：重新导入同一身份 → 复活同一行，行数不变。

    这条用例就是当初 7 条重复账号的正解：不是「删掉时记得清理干净」，
    而是「身份行根本不会被删掉」。
    """
    SessionLocal, headers, user_id = _wire(tmp_path)
    account_id = _seed_account(SessionLocal, user_id)
    client.delete(f"/api/accounts/{account_id}", headers=headers)

    info = {"external_user_id": "ext-soft", "nickname": "待删号", "avatar_url": "", "profile": {}}
    db = SessionLocal()
    try:
        revived, action = upsert_platform_account_from_login(
            db=db, user_id=user_id, platform="xhs", sub_type="creator",
            user_info=info, cookies_text=COOKIE_PAYLOAD,
        )
        db.commit()
        rows = db.scalars(select(PlatformAccount)).all()
    finally:
        db.close()

    assert action == "restored"
    assert revived.id == account_id, "复活失败：新建了一行，旧笔记会永久悬空"
    assert revived.deleted_at is None
    assert len(rows) == 1, f"删除后重新导入又多了一行：{[r.id for r in rows]}"


def test_notes_survive_account_deletion_without_orphan(tmp_path):
    """删账号后它名下的笔记**不悬空**：行还在，且指向的账号行也还在（软删）。"""
    SessionLocal, headers, user_id = _wire(tmp_path)
    account_id = _seed_account(SessionLocal, user_id, note_id="note-survives")
    client.delete(f"/api/accounts/{account_id}", headers=headers)

    db = SessionLocal()
    try:
        note = db.scalar(select(Note).where(Note.note_id == "note-survives"))
        owner = db.get(PlatformAccount, note.platform_account_id) if note else None
    finally:
        db.close()

    assert note is not None, "删除账号把用户的笔记一起删了"
    assert owner is not None, "笔记的平台账号引用变成孤儿（指向不存在的行）"


def test_notes_come_back_after_import_of_the_same_identity(tmp_path):
    """端到端：删账号 → 重新导入同一身份 → 老笔记**自动重新可见**（无需再同步一次）。

    这正是「重挂归属」与「复活」两条修复合起来才成立的效果：
    复活保住了 id，重挂保证新同步的笔记也落在同一个 id 上。
    """
    SessionLocal, headers, user_id = _wire(tmp_path)
    account_id = _seed_account(SessionLocal, user_id, note_id="note-relink")
    client.delete(f"/api/accounts/{account_id}", headers=headers)

    info = {"external_user_id": "ext-soft", "nickname": "待删号", "avatar_url": "", "profile": {}}
    db = SessionLocal()
    try:
        account, _action = upsert_platform_account_from_login(
            db=db, user_id=user_id, platform="xhs", sub_type="creator",
            user_info=info, cookies_text=COOKIE_PAYLOAD,
        )
        db.commit()
        _save_normalized_notes(db, account, [_normalized("note-relink", "重新同步一次")])
        note = db.scalar(select(Note).where(Note.note_id == "note-relink"))
        # 会话内取裸值，避免 detached 实例（同上一处）
        note_account_id = note.platform_account_id
        note_title = note.title
        revived_id = account.id
    finally:
        db.close()

    assert revived_id == account_id, "重新导入没有复活同一行 —— 老笔记的归属又对不上了"
    assert note_account_id == account_id
    assert note_account_id == revived_id
    assert note_title == "重新同步一次"
