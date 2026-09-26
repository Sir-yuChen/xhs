"""内容库「是否已发布过」标识（设计 13 §19.12 / 设计 03 §4.1 / 设计 05 §2.5）。

盯的是**判据与单调性**，不是渲染：

1. **默认值**：采集类入库（`notes.py::batch_save_notes`、`_save_normalized_notes`
   的默认参数）⇒ 待发布（`False`）；
2. **同步落库 ⇒ 已发布**：`run_note_sync` 跑完，同一批行 `published is True`。
   判据是**链路**而不是触发方式 —— 手动点「同步笔记」与定时轮跑的是同一条链路，
   按 `tasks.source == "scheduled"` 判会把「手动同步回来的已发布作品」错标成待发布；
3. **只升不降**：同步过的行再被采集链路命中**不降级**；采集过的行被同步命中升级；
4. **ark 填空合并**与发布状态并存：既有行升为已发布，正文不被抹掉（§19.3.2 不受影响）；
5. **接口**：`GET /notes?published=` 三态过滤；`stats` 是**全库口径**（不随筛选变化）；
   `PATCH /notes/{id}/published` 切换 + 幂等 + 越权 404。

⚠️ 本文件刻意**不**依赖「夹具没写这一列也会拿到 `False`」：每一条断言都显式读回
`note.published`（列有 DDL 默认值，只靠默认值会让新契约在测试里等于没测）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.api.platforms.xhs.crawl import _save_normalized_notes
from backend.app.core.database import Base, get_db
from backend.app.main import app
from backend.app.models import Note, PlatformAccount, User
from backend.app.services import note_sync_service as nss
from tests.backend import test_note_sync as nsync

client = TestClient(app)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _session(tmp_path, name: str):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _seed_account(db, *, username: str = "owner", external_user_id: str = ""):
    # 走 HTTP 注册的用例里用户已经存在 ⇒ 复用，别再插一行（username 唯一）。
    user = db.scalars(select(User).where(User.username == username)).first()
    if user is None:
        user = User(username=username, password_hash="x")
        db.add(user)
        db.flush()
    account = PlatformAccount(
        user_id=user.id,
        platform="xhs",
        sub_type="pc",
        nickname="我的号",
        status="active",
        external_user_id=external_user_id,
    )
    db.add(account)
    db.flush()
    return user, account


def _item(note_id: str, *, title: str = "", content: str = "") -> dict:
    """归一化条目（`_save_normalized_notes` / `_merge_ark_notes` 的输入形状）。"""
    return {
        "note_id": note_id,
        "title": title or f"标题 {note_id}",
        "content": content,
        "author_name": "作者",
        "image_urls": [],
    }


@pytest.fixture
def no_download(monkeypatch):
    """素材下载不联网（`_download_asset` 是函数内 import，改模块属性即可生效）。"""
    monkeypatch.setattr(
        "backend.app.services.asset_downloader.download_asset_to_local", lambda *a, **k: None
    )


# --------------------------------------------------------------------------- #
# 1) 默认值：采集类入库 ⇒ 待发布
# --------------------------------------------------------------------------- #
def test_save_normalized_notes_defaults_to_unpublished(tmp_path, no_download):
    """默认参数 = 采集语义 ⇒ 待发布（`False`）。"""
    SessionLocal = _session(tmp_path, "default-unpublished.db")
    db = SessionLocal()
    try:
        _, account = _seed_account(db)
        _save_normalized_notes(db, account, [_item("N-DEFAULT")])
        note = db.scalars(select(Note).where(Note.note_id == "N-DEFAULT")).one()
        assert note.published is False
    finally:
        db.close()


def test_batch_save_notes_defaults_to_unpublished(tmp_path):
    """采集结果批量入库（`POST /notes/batch-save`）⇒ 待发布。"""
    SessionLocal = _session(tmp_path, "batch-save.db")

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = client.post(
            "/api/auth/register", json={"username": "owner", "password": "secret123"}
        )
        assert response.status_code == 200, response.text
        headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

        db = SessionLocal()
        try:
            _, account = _seed_account(db)
            db.commit()
            account_id = account.id
        finally:
            db.close()

        saved = client.post(
            "/api/notes/batch-save",
            headers=headers,
            json={"account_id": account_id, "notes": [{"note_id": "N-BATCH", "title": "采集"}]},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["items"][0]["published"] is False
    finally:
        app.dependency_overrides.pop(get_db, None)


# --------------------------------------------------------------------------- #
# 2) 同步落库 ⇒ 已发布，且**只升不降**
# --------------------------------------------------------------------------- #
def test_sync_lane_marks_published(tmp_path, no_download):
    """`published=True`（同步语义）⇒ 新建行已发布。"""
    SessionLocal = _session(tmp_path, "sync-true.db")
    db = SessionLocal()
    try:
        _, account = _seed_account(db)
        _save_normalized_notes(db, account, [_item("N-SYNC")], published=True)
        note = db.scalars(select(Note).where(Note.note_id == "N-SYNC")).one()
        assert note.published is True
    finally:
        db.close()


def test_published_only_goes_up(tmp_path, no_download):
    """**只升不降**：同步过（已发布）→ 再被采集命中 ⇒ 仍是已发布。"""
    SessionLocal = _session(tmp_path, "only-up.db")
    db = SessionLocal()
    try:
        _, account = _seed_account(db)
        _save_normalized_notes(db, account, [_item("N-1")], published=True)
        # 采集链路命中同一条（默认 published=False）
        _save_normalized_notes(db, account, [_item("N-1")])

        note = db.scalars(select(Note).where(Note.note_id == "N-1")).one()
        assert note.published is True, "采集链路把已发布降级成了待发布（DEF 级）"
    finally:
        db.close()


def test_collected_row_is_upgraded_by_sync(tmp_path, no_download):
    """反向：先采集（待发布）→ 再同步命中 ⇒ 升为已发布。"""
    SessionLocal = _session(tmp_path, "upgrade.db")
    db = SessionLocal()
    try:
        _, account = _seed_account(db)
        _save_normalized_notes(db, account, [_item("N-2")])
        assert db.scalars(select(Note).where(Note.note_id == "N-2")).one().published is False

        _save_normalized_notes(db, account, [_item("N-2")], published=True)

        assert db.scalars(select(Note).where(Note.note_id == "N-2")).one().published is True
    finally:
        db.close()


def test_ark_merge_marks_published_and_keeps_content(tmp_path, no_download):
    """ark 填空合并：既有行升为已发布，**且正文不被抹掉**（§19.3.2 不受影响）。"""
    SessionLocal = _session(tmp_path, "ark-merge.db")
    db = SessionLocal()
    try:
        _, account = _seed_account(db)
        # PC 源先落库：带正文、待发布
        _save_normalized_notes(db, account, [_item("N-ARK", content="PC 落的正文")])
        db.commit()

        # ark 源后跑：无正文
        nss._merge_ark_notes(
            db,
            account,
            [
                {
                    "note_id": "N-ARK",
                    "title": "",
                    "content": "",
                    "author_name": "",
                    "likes": 3,
                }
            ],
        )

        note = db.scalars(select(Note).where(Note.note_id == "N-ARK")).one()
        assert note.published is True
        assert note.content == "PC 落的正文", "ark 填空合并把已落库的正文抹掉了"
    finally:
        db.close()


def test_ark_merge_new_row_is_published(tmp_path, no_download):
    """ark 源新建行同样是已发布（千帆商品笔记 = 账号已发布的作品）。"""
    SessionLocal = _session(tmp_path, "ark-new.db")
    db = SessionLocal()
    try:
        _, account = _seed_account(db)
        nss._merge_ark_notes(db, account, [{"note_id": "N-ARK-NEW", "title": "商品笔记"}])
        note = db.scalars(select(Note).where(Note.note_id == "N-ARK-NEW")).one()
        assert note.published is True
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 3) 端到端：`run_note_sync` 落库的行就是已发布
# --------------------------------------------------------------------------- #
@pytest.fixture
def creator_env(tmp_path, monkeypatch):
    """复用 `test_note_sync` 的三源夹具（不影响该文件自己的用例）。"""
    nsync.CURRENT[0] = nsync.CREATOR_COOKIES
    nsync.ENV["channel"] = "creator"
    headers, account_id, user_id, SessionLocal = nsync._setup(tmp_path, monkeypatch)
    yield headers, account_id, user_id, SessionLocal
    nsync._teardown()


def test_run_note_sync_persists_published_true(creator_env, monkeypatch):
    """创作者链路跑一次同步 ⇒ 落库的笔记全部 `published is True`。

    这是**判据同源**的守卫：同步链路是否真的把 `published=True` 传下去，
    只能由「跑完同步之后库里的值」回答，源码断言证明不了。
    """
    _, account_id, user_id, SessionLocal = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id, max_pages=1)

    assert result["status"] == "done", result
    assert result["stored"] > 0

    db = SessionLocal()
    try:
        notes = db.scalars(select(Note).where(Note.platform_account_id == account_id)).all()
        assert notes, "同步一条都没落库"
        assert all(note.published for note in notes), [
            (note.note_id, note.published) for note in notes
        ]
    finally:
        db.close()


def test_manual_trigger_also_marks_published(creator_env, monkeypatch):
    """**手动触发**（`source="manual"`）与定时轮同一条链路 ⇒ 同样标已发布。

    反面：若判据写成 `source == "scheduled"`，手动同步回来的已发布作品会被标成
    待发布 —— 语义直接错，且错的方向是「让用户看到假状态」。
    """
    _, account_id, user_id, SessionLocal = creator_env

    result = nss.run_note_sync(account_id, user_id=user_id, max_pages=1, source="manual")

    assert result["status"] == "done", result
    db = SessionLocal()
    try:
        notes = db.scalars(select(Note).where(Note.platform_account_id == account_id)).all()
        assert notes and all(note.published for note in notes)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 4) 接口：过滤 / 统计 / 切换
# --------------------------------------------------------------------------- #
@pytest.fixture
def api_env(tmp_path, no_download):
    """一个用户 + 两条笔记（一条已发布、一条待发布）。"""
    SessionLocal = _session(tmp_path, "api.db")

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = client.post(
            "/api/auth/register", json={"username": "owner", "password": "secret123"}
        )
        assert response.status_code == 200, response.text
        headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

        db = SessionLocal()
        try:
            _, account = _seed_account(db)
            _save_normalized_notes(db, account, [_item("N-PUB", title="已发布的")], published=True)
            _save_normalized_notes(db, account, [_item("N-UNPUB", title="待发布的")])
            db.commit()
        finally:
            db.close()
        yield headers, SessionLocal
    finally:
        app.dependency_overrides.pop(get_db, None)


def _seed_failed_note(SessionLocal) -> int:
    """再插一条**发布失败**的本地生成笔记（三档筛选与统计都要用到它）。"""
    db = SessionLocal()
    try:
        note = Note(
            user_id=1,
            platform_account_id=1,
            platform="xhs",
            note_id="local_failed01",
            title="发布失败的",
            note_type="local_generated",
            publish_status="failed",
            publish_error="平台拒发（测试）",
        )
        db.add(note)
        db.commit()
        return note.id
    finally:
        db.close()


def test_notes_filter_by_publish_status_four_states(api_env):
    """发布状态筛选**四档**（设计 13 §32.3.3）：`all` 不筛，另三档各回各的。

    守的是两个坑：① 「发布失败」这一档是新加的（旧的两态参数表达不了它）；
    ② 未知取值**不静默忽略**（不猜枚举 ⇒ 400 带支持列表）。
    """
    headers, SessionLocal = api_env
    _seed_failed_note(SessionLocal)

    all_items = client.get("/api/notes", headers=headers, params={"platform": "xhs"}).json()
    assert all_items["total"] == 3

    only_published = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "publish_status": "published"}
    ).json()
    assert [n["note_id"] for n in only_published["items"]] == ["N-PUB"]
    assert all(n["publish_status"] == "published" for n in only_published["items"])

    only_pending = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "publish_status": "pending"}
    ).json()
    assert [n["note_id"] for n in only_pending["items"]] == ["N-UNPUB"]
    assert all(n["publish_status"] == "pending" for n in only_pending["items"])

    # **本轮新增的那一档**：只看发布失败。
    only_failed = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "publish_status": "failed"}
    ).json()
    assert [n["note_id"] for n in only_failed["items"]] == ["local_failed01"]
    assert only_failed["items"][0]["publish_error"] == "平台拒发（测试）"

    # 显式 `all` 与不传等价
    explicit_all = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "publish_status": "all"}
    ).json()
    assert explicit_all["total"] == 3

    # 未知取值 ⇒ 400（不猜枚举）
    bad = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "publish_status": "archived"}
    )
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "NOTE_STATUS_UNSUPPORTED"


def test_notes_stats_is_whole_library(api_env):
    """`stats` 是**全库口径**的三档计数：叠加任何筛选都不变（设计 13 §32.3.3）。"""
    headers, SessionLocal = api_env
    _seed_failed_note(SessionLocal)

    plain = client.get("/api/notes", headers=headers, params={"platform": "xhs"}).json()
    assert plain["stats"] == {"pending": 1, "published": 1, "failed": 1}

    # 只看发布失败：items 只剩 1 条，但 stats 仍是全库的 1/1/1。
    filtered = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "publish_status": "failed"}
    ).json()
    assert filtered["total"] == 1
    assert filtered["stats"] == {"pending": 1, "published": 1, "failed": 1}

    # 关键词筛选同样不改变 stats。
    keyword = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "q": "待发布"}
    ).json()
    assert keyword["stats"] == {"pending": 1, "published": 1, "failed": 1}


def test_patch_publish_status_toggles_and_is_idempotent(api_env):
    """切换接口：改得动、返回整行（含 tags）、重复提交同一个值不报错。

    同时守「`published` 是 `publish_status` 的**投影**」（设计 13 §32.3.2）：
    写状态时两列必须同步，不允许出现「状态说已发布、投影说没发」。
    """
    headers, SessionLocal = api_env
    listed = client.get("/api/notes", headers=headers, params={"platform": "xhs"}).json()
    note_id = next(n["id"] for n in listed["items"] if n["note_id"] == "N-UNPUB")

    toggled = client.patch(
        f"/api/notes/{note_id}/publish-status", headers=headers, json={"publish_status": "published"}
    )
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["publish_status"] == "published"
    assert toggled.json()["published"] is True  # 投影同步
    assert "topics" in toggled.json(), "切换后应返回整行（与列表同形状）"

    # 幂等：再提交同一个值
    again = client.patch(
        f"/api/notes/{note_id}/publish-status", headers=headers, json={"publish_status": "published"}
    )
    assert again.status_code == 200 and again.json()["publish_status"] == "published"

    # 撤回
    back = client.patch(
        f"/api/notes/{note_id}/publish-status", headers=headers, json={"publish_status": "pending"}
    )
    assert back.json()["publish_status"] == "pending"
    assert back.json()["published"] is False

    # 落库确认（不只看响应）：状态与投影一起写
    db = SessionLocal()
    try:
        note = db.scalars(select(Note).where(Note.id == note_id)).one()
        assert note.publish_status == "pending"
        assert note.published is False
    finally:
        db.close()


def test_patch_publish_status_rejects_local_generated(api_env):
    """本地生成笔记**不接受手改**（400 `NOTE_STATUS_MANAGED`）：状态归发布链路。"""
    headers, SessionLocal = api_env
    note_pk = _seed_failed_note(SessionLocal)

    response = client.patch(
        f"/api/notes/{note_pk}/publish-status",
        headers=headers,
        json={"publish_status": "published"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "NOTE_STATUS_MANAGED"

    # 落库状态没被改动
    db = SessionLocal()
    try:
        assert db.scalars(select(Note).where(Note.id == note_pk)).one().publish_status == "failed"
    finally:
        db.close()


def test_patch_publish_status_validates_body(api_env):
    """请求体必填 + 取值白名单：缺字段 / 非法值都是 422，不静默兜底。"""
    headers, _ = api_env
    listed = client.get("/api/notes", headers=headers, params={"platform": "xhs"}).json()
    note_id = listed["items"][0]["id"]

    assert client.patch(f"/api/notes/{note_id}/publish-status", headers=headers, json={}).status_code == 422
    # `failed` 是链路写的状态，手切白名单里没有它（`Literal` 直接挡回）
    assert (
        client.patch(
            f"/api/notes/{note_id}/publish-status", headers=headers, json={"publish_status": "failed"}
        ).status_code
        == 422
    )


def test_patch_publish_status_other_users_note_is_404(api_env):
    """越权：别人的笔记一律 404（不泄漏存在性）。"""
    headers, SessionLocal = api_env

    db = SessionLocal()
    try:
        other_user = User(username="intruder", password_hash="x")
        db.add(other_user)
        db.flush()
        intruder_note = Note(
            user_id=other_user.id,
            platform_account_id=1,
            platform="xhs",
            note_id="N-FOREIGN",
        )
        db.add(intruder_note)
        db.commit()
        foreign_id = intruder_note.id
    finally:
        db.close()

    response = client.patch(
        f"/api/notes/{foreign_id}/publish-status",
        headers=headers,
        json={"publish_status": "published"},
    )
    assert response.status_code == 404
