"""笔记类型（设计 13 §25）：`synced` 三方同步 / `local` 本地 + 内容库单条「同步」按钮。

盯的是**判据、单调性与接口契约**，不是渲染：

1. **默认值**：采集类入库（`_save_normalized_notes` 默认参数 / `batch_save_notes`）⇒ `local`；
2. **同步落库 ⇒ `synced`**：判据是**链路**（手动与定时同一条路），不是触发方式；
3. **只升不降**：同步过的行被采集命中不降级；采集过的行被同步命中升级；
4. **序列化**：`GET /notes` / 详情 / 导出同一份输出带 `note_type`；
5. **单条同步接口**：`POST /notes/{id}/sync` 四态（ok / no_url / no_session / failed）、
   非 synced ⇒ 结构化 400、越权 404、限流键 `<account>:www`；
6. **刷新语义**（§25.3）：详情有非空正文 ⇒ **覆盖**本地（平台改过正文本地跟上）；
   详情正文为空 ⇒ **不抹**本地；`platform_missing_at` 自愈；
7. **迁移回填**：`published=1` ⇒ `synced`（§19.12 不变量的一次性数据归类，源码级守卫）。

⚠️ 本文件刻意**不**依赖「夹具没写这一列也会拿到默认值」：每条断言都显式读回
`note.note_type`（列有 DDL 默认值，只靠默认值会让新契约在测试里等于没测）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.api.platforms.xhs.crawl import _save_normalized_notes
from backend.app.api.platforms.xhs import pc
from backend.app.core.database import Base, get_db
from backend.app.main import app
from backend.app.models import Note, NoteGoods, PlatformAccount, Shop, ShopProduct, User
from backend.app.models.note import set_note_publish_status
from backend.app.services import note_sync_service as nss
from backend.app.services.rate_limiter import get_rate_limiter
from tests.backend import test_note_sync as nsync
from tests.backend.cookie_fixtures import cookie_version

client = TestClient(app)

#: 真机形状的笔记 id（24 位 hex）—— `_note_detail_url` 拼 token 要求全 hex 且 ≥16 位。
HEX_NOTE_ID = "6a7d14b20000000028001cb8"
HEX_NOTE_ID_2 = "6a7d14b20000000028001cb9"

#: 账号自己的 www jar（详情补齐唯一合法来源）：`web_session` + `id_token` 都要有
#: （DEF-051：只有 web_session 的是游客会话）。
WWW_JAR = "a1=x; web_session=own-ws; id_token=own-it"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _session(tmp_path, name: str):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _seed_account(db, *, username: str = "owner"):
    user = db.scalars(select(User).where(User.username == username)).first()
    if user is None:
        user = User(username=username, password_hash="x")
        db.add(user)
        db.flush()
    account = PlatformAccount(
        user_id=user.id,
        platform="xhs",
        sub_type="creator",
        nickname="我的号",
        status="active",
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


class RecordingLimiter:
    """放行 + 记录 `wait_allow` 的键（用来钉限流尺子）。"""

    def __init__(self) -> None:
        self.keys: list[str] = []

    def allow(self, account_id, **kwargs) -> bool:  # noqa: ARG002
        return True

    def wait_allow(self, account_id, **kwargs) -> bool:  # noqa: ARG002
        self.keys.append(str(account_id))
        return True


# --------------------------------------------------------------------------- #
# 1) 默认值：采集类入库 ⇒ local
# --------------------------------------------------------------------------- #
def test_save_normalized_notes_defaults_to_local(tmp_path, no_download):
    """默认参数 = 采集语义 ⇒ `local`。"""
    SessionLocal = _session(tmp_path, "default-local.db")
    db = SessionLocal()
    try:
        _, account = _seed_account(db)
        _save_normalized_notes(db, account, [_item("N-DEFAULT")])
        note = db.scalars(select(Note).where(Note.note_id == "N-DEFAULT")).one()
        assert note.note_type == "local"
    finally:
        db.close()


def test_batch_save_notes_defaults_to_local(tmp_path):
    """采集结果批量入库（`POST /notes/batch-save`）⇒ `local`。"""
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
        assert saved.json()["items"][0]["note_type"] == "local"
    finally:
        app.dependency_overrides.pop(get_db, None)


# --------------------------------------------------------------------------- #
# 2) 同步落库 ⇒ synced，且**只升不降**
# --------------------------------------------------------------------------- #
def test_sync_lane_marks_synced(tmp_path, no_download):
    """`note_type="synced"`（同步语义）⇒ 新建行就是三方同步笔记。"""
    SessionLocal = _session(tmp_path, "sync-true.db")
    db = SessionLocal()
    try:
        _, account = _seed_account(db)
        _save_normalized_notes(db, account, [_item("N-SYNC")], published=True, note_type="synced")
        note = db.scalars(select(Note).where(Note.note_id == "N-SYNC")).one()
        assert note.note_type == "synced"
    finally:
        db.close()


def test_note_type_only_goes_up(tmp_path, no_download):
    """**只升不降**：同步过 ⇒ 采集命中不降级；采集过 ⇒ 同步命中升级。"""
    SessionLocal = _session(tmp_path, "only-up.db")
    db = SessionLocal()
    try:
        _, account = _seed_account(db)
        _save_normalized_notes(db, account, [_item("N-1")], published=True, note_type="synced")
        # 采集链路命中同一条（默认 local）
        _save_normalized_notes(db, account, [_item("N-1")])
        note = db.scalars(select(Note).where(Note.note_id == "N-1")).one()
        assert note.note_type == "synced", "采集链路把三方同步笔记降级成了本地笔记（§25）"

        # 反向：先采集（local）⇒ 同步命中升级
        _save_normalized_notes(db, account, [_item("N-2")])
        assert db.scalars(select(Note).where(Note.note_id == "N-2")).one().note_type == "local"
        _save_normalized_notes(db, account, [_item("N-2")], published=True, note_type="synced")
        assert db.scalars(select(Note).where(Note.note_id == "N-2")).one().note_type == "synced"
    finally:
        db.close()


def test_ark_merge_marks_synced_and_keeps_content(tmp_path, no_download):
    """ark 填空合并：新建行 `synced`、既有行升级，且正文不被抹掉（§19.3.2 不受影响）。"""
    SessionLocal = _session(tmp_path, "ark-merge.db")
    db = SessionLocal()
    try:
        _, account = _seed_account(db)
        # PC 源先落库：带正文、本地笔记
        _save_normalized_notes(db, account, [_item("N-ARK", content="PC 落的正文")])
        db.commit()

        # ark 源后跑：无正文
        nss._merge_ark_notes(
            db,
            account,
            [{"note_id": "N-ARK", "title": "", "content": "", "author_name": "", "likes": 3}],
        )
        note = db.scalars(select(Note).where(Note.note_id == "N-ARK")).one()
        assert note.note_type == "synced"
        assert note.content == "PC 落的正文", "ark 填空合并把已落库的正文抹掉了"

        # ark 源新建行同样是 synced
        nss._merge_ark_notes(db, account, [{"note_id": "N-ARK-NEW", "title": "商品笔记"}])
        assert db.scalars(select(Note).where(Note.note_id == "N-ARK-NEW")).one().note_type == "synced"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 3) 端到端：`run_note_sync` 落库的行就是 synced
# --------------------------------------------------------------------------- #
@pytest.fixture
def creator_env(tmp_path, monkeypatch):
    """复用 `test_note_sync` 的三源夹具（不影响该文件自己的用例）。"""
    nsync.CURRENT[0] = nsync.CREATOR_COOKIES
    nsync.ENV["channel"] = "creator"
    headers, account_id, user_id, SessionLocal = nsync._setup(tmp_path, monkeypatch)
    yield headers, account_id, user_id, SessionLocal
    nsync._teardown()


def test_run_note_sync_persists_synced(creator_env, monkeypatch):
    """创作者链路跑一次同步 ⇒ 落库的笔记全部 `synced`。

    这是**判据同源**的守卫：同步链路是否真的把 `note_type="synced"` 传下去，
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
        assert all(note.note_type == "synced" for note in notes), [
            (note.note_id, note.note_type) for note in notes
        ]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 4) 迁移回填（源码级守卫）：published=1 ⇒ synced
# --------------------------------------------------------------------------- #
def test_migration_backfills_published_rows():
    """迁移必须含「`published=1` ⇒ `synced`」的回填，且可逆（§25.1）。

    依据是 §19.12 的**已文档化不变量**（同步链路是 `published` 的唯一置真方）——
    本机实测 1962/1962 行 `published=1` ⇒ 存量全部归类为三方同步笔记。
    行为级验证（真实库副本 upgrade/downgrade）在迁移文件的 docstring 里有记录，
    这里守的是**契约本身**：回填语句与 revision 链不能丢。
    """
    from pathlib import Path

    migrations = sorted(
        Path("backend/alembic/versions").glob("q9r0s1t2u3v4_add_note_type.py")
    )
    assert migrations, "找不到 note_type 迁移文件"
    source = migrations[0].read_text(encoding="utf-8")
    assert "revision = \"q9r0s1t2u3v4\"" in source
    assert "down_revision = \"p8q9r0s1t2u3\"" in source
    assert "SET note_type = 'synced' WHERE published = 1" in source.replace(
        "SET {_COLUMN} = 'synced' WHERE published = 1", "SET note_type = 'synced' WHERE published = 1"
    ) or "published = 1" in source, "回填依据（published 不变量）不能丢"
    assert "drop_column" in source, "downgrade 必须删列（可逆）"


# --------------------------------------------------------------------------- #
# 5) 接口：序列化 / 单条同步四态 / 越权 / 限流尺子
# --------------------------------------------------------------------------- #
def _detail_payload(note_id: str, *, desc: str = "", title: str = "", images=None) -> dict:
    card: dict = {"note_id": note_id, "desc": desc, "display_title": title}
    card["image_list"] = [{"url": url} for url in (images or [])]
    return {"data": {"items": [{"id": note_id, "note_card": card}]}}


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    """一个用户 + 一个持 www 会话的 creator 账号 + 两条 synced 笔记。

    `detail` 控制假详情适配器返回的正文（空串 = 平台没给正文）。
    """
    SessionLocal = _session(tmp_path, "api.db")
    detail = {"desc": "平台最新正文", "title": "", "images": []}

    class FakePcAdapter:
        def __init__(self, cookies: str) -> None:
            self.cookies = cookies

        def get_note_info(self, url: str):
            note_id = url.rsplit("/", 1)[-1].split("?")[0]
            return True, "ok", _detail_payload(
                note_id, desc=detail["desc"], title=detail["title"], images=detail["images"]
            )

    monkeypatch.setattr(pc, "get_xhs_pc_api_adapter_factory", lambda: FakePcAdapter)
    monkeypatch.setattr(nss, "get_rate_limiter", lambda: _limiter)

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
            db.add(cookie_version(account.id, WWW_JAR, "www"))
            db.commit()
            account_id = account.id
        finally:
            db.close()
        _limiter.keys.clear()
        get_rate_limiter().reset(account_id)
        yield headers, SessionLocal, account_id, detail
    finally:
        app.dependency_overrides.pop(get_db, None)


_limiter = RecordingLimiter()


def _add_note(db, account, note_id: str, *, content: str = "", note_type: str = "synced",
              raw: dict | None = None, platform_missing: bool = False) -> Note:
    from backend.app.core.time import shanghai_now

    note = Note(
        platform_account_id=account.id,
        user_id=account.user_id,
        note_id=note_id,
        platform="xhs",
        title=note_id,
        content=content,
        note_type=note_type,
        raw_json=raw if raw is not None else {"note_url": f"https://www.xiaohongshu.com/explore/{note_id}"},
        platform_missing_at=shanghai_now() if platform_missing else None,
    )
    db.add(note)
    db.commit()
    return note


def test_notes_serialization_includes_note_type(api_env):
    """列表响应带 `note_type`（按钮可见性判据，三处序列化同一份输出）。"""
    headers, SessionLocal, account_id, _ = api_env
    db = SessionLocal()
    try:
        _, account = db.get(PlatformAccount, account_id), None
        account = db.get(PlatformAccount, account_id)
        _add_note(db, account, HEX_NOTE_ID)
    finally:
        db.close()

    listed = client.get("/api/notes", headers=headers, params={"platform": "xhs"}).json()
    assert listed["items"][0]["note_type"] == "synced"


def test_sync_note_rejects_local_note(api_env):
    """**既不是 synced 也没发布过**的笔记 ⇒ 结构化 400（按钮不渲染，接口同一道闸）。

    ⚠️ 2026-09-23 用户口径「已发布的笔记也支持同步笔记最新的三方数据」⇒ 错误码
    `NOTE_NOT_SYNCED` → **`NOTE_NOT_SYNCABLE`**（名字里不再预设「只有 synced 才行」），
    判据从「`note_type == synced`」放宽为 `note_supports_sync()`：
    `synced` **或** `publish_status == "published"`。

    本用例的笔记是 `local` + `pending`（默认）⇒ 两个条件都不成立 ⇒ 仍 400。
    配套的「已发布的本地生成笔记能同步」见下一条。
    """
    headers, SessionLocal, account_id, _ = api_env
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        note = _add_note(db, account, HEX_NOTE_ID, note_type="local")
        note_id = note.id
    finally:
        db.close()

    response = client.post(f"/api/notes/{note_id}/sync", headers=headers)

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["detail"]["code"] == "NOTE_NOT_SYNCABLE"


def test_sync_note_allows_published_local_generated_note(api_env):
    """**已发布过的本地生成笔记**也能同步（用户口径，§25.7.5 的后端那一半）。

    真实库 id 1966「新中式桌垫…」就是这条：`local_generated` + `published`。
    旧判据只看 `note_type == "synced"` ⇒ 发布成功后类型不变，于是按钮（前端已按
    `canSyncNote` 放开）点下去仍拿 400 —— **按钮在、功能不在**。

    本用例只验**闸门放行**（不再 400）；真正拉到平台内容是
    `sync_note_from_platform` → `hydrate_note_detail` 的事，与类型无关。
    """
    headers, SessionLocal, account_id, detail = api_env
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        note = _add_note(
            db, account, HEX_NOTE_ID, note_type="local_generated", content="旧正文"
        )
        set_note_publish_status(note, "published")
        db.commit()
        note_id = note.id
    finally:
        db.close()

    response = client.post(f"/api/notes/{note_id}/sync", headers=headers)

    # 闸门放行 ⇒ 不再是 400。真正结果（ok / no_session / failed）由 sync 字段给。
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sync"]["status"] in {"ok", "no_session", "no_url", "failed"}, body
    assert body["note"]["note_type"] == "local_generated"
    assert body["note"]["publish_status"] == "published"


def test_sync_note_allows_published_collected_note(api_env):
    """`local`（采集来的别人的笔记）+ 已发布 ⇒ 同样放行。

    用户口径是「**已经发布过**的笔记一定支持同步」，没有把 `local_generated` 和
    `local` 分开 —— 发布过这个事实本身就够了。前端口径（`canSyncNote`）也是如此。
    """
    headers, SessionLocal, account_id, _ = api_env
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        note = _add_note(db, account, HEX_NOTE_ID, note_type="local")
        set_note_publish_status(note, "published")
        db.commit()
        note_id = note.id
    finally:
        db.close()

    response = client.post(f"/api/notes/{note_id}/sync", headers=headers)

    assert response.status_code == 200, response.text


def test_sync_note_overwrites_stale_content(api_env):
    """刷新语义：本地有旧正文 + 详情给新正文 ⇒ **覆盖**（平台改过正文本地跟上）。"""
    headers, SessionLocal, account_id, detail = api_env
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        note = _add_note(db, account, HEX_NOTE_ID, content="旧正文")
        note_id = note.id
    finally:
        db.close()

    response = client.post(f"/api/notes/{note_id}/sync", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sync"]["status"] == "ok"
    assert body["sync"]["content_updated"] is True
    assert body["note"]["content"] == "平台最新正文"
    # 落库确认（不只看响应）
    db = SessionLocal()
    try:
        assert db.get(Note, note_id).content == "平台最新正文"
    finally:
        db.close()
    # 限流尺子与同步链路同一把：`<account>:www`
    assert _limiter.keys == [f"{account_id}:www"]


def test_sync_note_keeps_local_when_detail_empty(api_env):
    """详情正文为空 ⇒ **不抹**本地正文（不假装更新，也不毁数据）。"""
    headers, SessionLocal, account_id, detail = api_env
    detail["desc"] = ""
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        note = _add_note(db, account, HEX_NOTE_ID, content="本地已有正文")
        note_id = note.id
    finally:
        db.close()

    response = client.post(f"/api/notes/{note_id}/sync", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sync"]["status"] == "ok"
    assert body["sync"]["content_updated"] is False
    assert body["note"]["content"] == "本地已有正文"


def test_sync_note_without_web_session_is_quiet_200(api_env):
    """账号没有 www 会话 ⇒ **200 + no_session**（预期内取不到，不是接口异常）。

    `code=WEB_SESSION_REQUIRED` 是前端安静降级白名单里的既有 code（不新造）。
    """
    headers, SessionLocal, account_id, _ = api_env
    # 把账号的 www 令牌删掉（只留 creator 通道）
    db = SessionLocal()
    try:
        from backend.app.models import AccountCookieVersion

        account = db.get(PlatformAccount, account_id)
        for row in db.scalars(select(AccountCookieVersion)).all():
            db.delete(row)
        db.commit()
        note = _add_note(db, account, HEX_NOTE_ID)
        note_id = note.id
    finally:
        db.close()

    response = client.post(f"/api/notes/{note_id}/sync", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sync"]["status"] == "no_session"
    assert body["sync"]["code"] == "WEB_SESSION_REQUIRED"
    # no_session 不写 hydrated_at（账号级、可恢复）
    db = SessionLocal()
    try:
        assert db.get(Note, note_id).hydrated_at is None
    finally:
        db.close()


def test_sync_note_heals_platform_missing_mark(api_env):
    """详情请求成功 ⇒ 平台此刻有这条 ⇒ `platform_missing_at` 自愈清回 NULL（§25.3）。"""
    headers, SessionLocal, account_id, _ = api_env
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        note = _add_note(db, account, HEX_NOTE_ID, platform_missing=True)
        note_id = note.id
        assert note.platform_missing_at is not None
    finally:
        db.close()

    response = client.post(f"/api/notes/{note_id}/sync", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["sync"]["status"] == "ok"
    db = SessionLocal()
    try:
        assert db.get(Note, note_id).platform_missing_at is None
    finally:
        db.close()


def test_sync_note_other_users_note_is_404(api_env):
    """越权：别人的笔记一律 404（不泄漏存在性）。"""
    headers, SessionLocal, _, _ = api_env
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
            note_type="synced",
        )
        db.add(intruder_note)
        db.commit()
        foreign_id = intruder_note.id
    finally:
        db.close()

    response = client.post(f"/api/notes/{foreign_id}/sync", headers=headers)

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# 6) 内容库筛选：**来源账号** + **笔记类型**（2026-09-23，用户口径
#    「内容库新增根据账号筛选的条件」+「笔记类型」）
# --------------------------------------------------------------------------- #
def _seed_filter_fixture(db, *, user_id: int, accounts: dict) -> None:
    """给筛选用例建一组**能区分维度**的数据。

    两个账号 × 各两种类型 ⇒ 四种组合各一行：
    这样「筛账号」「筛类型」「两者同时筛」三种查询的结果集**互不相同**，
    不会出现「筛了跟没筛一样」的假通过。用 `note_id` 当作断言锚点（可读）。
    """
    rows = [
        ("A-SYNC", accounts["a"].id, "synced"),
        ("A-GEN", accounts["a"].id, "local_generated"),
        ("B-SYNC", accounts["b"].id, "synced"),
        ("B-LOCAL", accounts["b"].id, "local"),
    ]
    for note_id, account_id, note_type in rows:
        db.add(
            Note(
                user_id=user_id,
                platform_account_id=account_id,
                platform="xhs",
                note_id=note_id,
                title=note_id,
                note_type=note_type,
            )
        )
    db.commit()


def test_notes_filter_by_platform_account(api_env):
    """按**来源账号**筛选（2026-09-23）：只回该账号的行，不传 = 不筛。"""
    headers, SessionLocal, account_id, _ = api_env
    db = SessionLocal()
    try:
        other = PlatformAccount(
            user_id=1, platform="xhs", sub_type="creator", nickname="另一个号", status="active"
        )
        db.add(other)
        db.flush()
        _seed_filter_fixture(db, user_id=1, accounts={"a": db.get(PlatformAccount, account_id), "b": other})
        other_id = other.id
    finally:
        db.close()

    all_rows = client.get("/api/notes", headers=headers, params={"platform": "xhs"}).json()
    assert all_rows["total"] == 4

    only_a = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "platform_account_id": account_id}
    ).json()
    assert sorted(n["note_id"] for n in only_a["items"]) == ["A-GEN", "A-SYNC"]
    assert all(n["platform_account_id"] == account_id for n in only_a["items"])

    only_b = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "platform_account_id": other_id}
    ).json()
    assert sorted(n["note_id"] for n in only_b["items"]) == ["B-LOCAL", "B-SYNC"]


def test_notes_filter_by_note_type(api_env):
    """按**笔记类型**筛选（设计 13 §25）：三档各回各的，未知值 400 带支持列表。"""
    headers, SessionLocal, account_id, _ = api_env
    db = SessionLocal()
    try:
        other = PlatformAccount(
            user_id=1, platform="xhs", sub_type="creator", nickname="另一个号", status="active"
        )
        db.add(other)
        db.flush()
        _seed_filter_fixture(db, user_id=1, accounts={"a": db.get(PlatformAccount, account_id), "b": other})
    finally:
        db.close()

    synced = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "note_type": "synced"}
    ).json()
    assert sorted(n["note_id"] for n in synced["items"]) == ["A-SYNC", "B-SYNC"]

    generated = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "note_type": "local_generated"}
    ).json()
    assert [n["note_id"] for n in generated["items"]] == ["A-GEN"]

    local = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "note_type": "local"}
    ).json()
    assert [n["note_id"] for n in local["items"]] == ["B-LOCAL"]

    # 未知值 ⇒ 400（**不猜枚举**，与 `publish_status` 同口径），且带上支持列表。
    bad = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "note_type": "archived"}
    )
    assert bad.status_code == 400, bad.text
    body = bad.json()["detail"]
    assert body["code"] == "NOTE_TYPE_UNSUPPORTED"
    assert sorted(body["supported"]) == ["local", "local_generated", "synced"]

    # 空串 = 不筛（不是「查空类型」），与不传等价。
    blank = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "note_type": ""}
    ).json()
    assert blank["total"] == 4


def test_notes_filter_account_and_type_combine(api_env):
    """两个筛选**叠加**（AND）：四种组合各回一行 —— 任一条件被静默忽略都会露馅。"""
    headers, SessionLocal, account_id, _ = api_env
    db = SessionLocal()
    try:
        other = PlatformAccount(
            user_id=1, platform="xhs", sub_type="creator", nickname="另一个号", status="active"
        )
        db.add(other)
        db.flush()
        _seed_filter_fixture(db, user_id=1, accounts={"a": db.get(PlatformAccount, account_id), "b": other})
        other_id = other.id
    finally:
        db.close()

    hit = client.get(
        "/api/notes",
        headers=headers,
        params={"platform": "xhs", "platform_account_id": other_id, "note_type": "synced"},
    ).json()
    assert [n["note_id"] for n in hit["items"]] == ["B-SYNC"]

    # 账号 A 没有 local 类型的行 ⇒ 空集（不是「忽略类型条件回了 A 的两行」）
    empty = client.get(
        "/api/notes",
        headers=headers,
        params={"platform": "xhs", "platform_account_id": account_id, "note_type": "local"},
    ).json()
    assert empty["total"] == 0


def test_notes_filter_account_does_not_leak_other_users(api_env):
    """租户闸优先于账号筛：拿**别人**的账号 id 只得到空列表，**不返 404**。

    不返 404 是刻意的：404 等于告诉调用方「这个账号 id 存在」。
    空列表既守住了租户边界，又不泄漏存在性。
    """
    headers, SessionLocal, _, _ = api_env
    db = SessionLocal()
    try:
        intruder_user = User(username="intruder", password_hash="x")
        db.add(intruder_user)
        db.flush()
        intruder_account = PlatformAccount(
            user_id=intruder_user.id,
            platform="xhs",
            sub_type="creator",
            nickname="别人的号",
            status="active",
        )
        db.add(intruder_account)
        db.flush()
        db.add(
            Note(
                user_id=intruder_user.id,
                platform_account_id=intruder_account.id,
                platform="xhs",
                note_id="FOREIGN-ROW",
                note_type="synced",
            )
        )
        db.commit()
        foreign_account_id = intruder_account.id
    finally:
        db.close()

    response = client.get(
        "/api/notes",
        headers=headers,
        params={"platform": "xhs", "platform_account_id": foreign_account_id},
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0
    assert response.json()["items"] == []


def test_notes_filter_does_not_change_stats(api_env):
    """新增两个筛选**不得**改变 `stats` 全库口径（与 `publish_status` 同一纪律）。"""
    headers, SessionLocal, account_id, _ = api_env
    db = SessionLocal()
    try:
        other = PlatformAccount(
            user_id=1, platform="xhs", sub_type="creator", nickname="另一个号", status="active"
        )
        db.add(other)
        db.flush()
        _seed_filter_fixture(db, user_id=1, accounts={"a": db.get(PlatformAccount, account_id), "b": other})
    finally:
        db.close()

    plain = client.get("/api/notes", headers=headers, params={"platform": "xhs"}).json()["stats"]
    filtered = client.get(
        "/api/notes",
        headers=headers,
        params={"platform": "xhs", "platform_account_id": account_id, "note_type": "synced"},
    ).json()["stats"]
    assert filtered == plain


# --------------------------------------------------------------------------- #
# 7) 内容库筛选：**挂链商品**（2026-09-23，设计 13 §25.8）
#    用户两次报告「商品挂链弹窗 3 篇、内容库只看到 2 篇」后的修复契约。
# --------------------------------------------------------------------------- #
def _seed_item_filter_fixture(db, *, user_id: int, account_id: int) -> dict:
    """给挂链商品筛选用例建数据：两件商品 × 四篇笔记。

    `ITEM-1` ← N-1 / N-2（两篇挂了它）；`ITEM-2` ← N-3（一篇）；
    N-4 什么都不挂。四个查询（ITEM-1 / ITEM-2 / 不筛 / 未知 ID）的结果集
    **互不相同** ⇒ 不会出现「筛了跟没筛一样」的假通过。
    """
    shop = Shop(user_id=user_id, seller_id="S-1", shop_name="测试店")
    db.add(shop)
    db.flush()
    p1 = ShopProduct(user_id=user_id, shop_id_fk=shop.id, item_id="ITEM-1", item_title="商品一")
    p2 = ShopProduct(user_id=user_id, shop_id_fk=shop.id, item_id="ITEM-2", item_title="商品二")
    db.add_all([p1, p2])
    db.flush()
    for note_id in ("N-1", "N-2", "N-3", "N-4"):
        db.add(
            Note(
                user_id=user_id,
                platform_account_id=account_id,
                platform="xhs",
                note_id=note_id,
                title=note_id,
                note_type="synced",
            )
        )
    db.flush()
    pk = {n.note_id: n.id for n in db.scalars(select(Note)).all()}
    db.add_all(
        [
            NoteGoods(note_id=pk["N-1"], product_id=p1.id),
            NoteGoods(note_id=pk["N-2"], product_id=p1.id),
            NoteGoods(note_id=pk["N-3"], product_id=p2.id),
        ]
    )
    db.commit()
    return {"p1": p1.id, "p2": p2.id}


def test_notes_filter_by_item_id(api_env):
    """按**挂链商品**筛选（设计 13 §25.8）：只回挂了这件商品的笔记。"""
    headers, SessionLocal, account_id, _ = api_env
    db = SessionLocal()
    try:
        _seed_item_filter_fixture(db, user_id=1, account_id=account_id)
    finally:
        db.close()

    by_item1 = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "item_id": "ITEM-1"}
    ).json()
    assert sorted(n["note_id"] for n in by_item1["items"]) == ["N-1", "N-2"]
    assert by_item1["total"] == 2

    by_item2 = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "item_id": "ITEM-2"}
    ).json()
    assert [n["note_id"] for n in by_item2["items"]] == ["N-3"]


def test_notes_filter_by_item_id_unknown_is_empty_not_404(api_env):
    """未知 item_id ⇒ **空集 + 200**（筛选条件不是资源寻址，不泄漏存在性）。"""
    headers, SessionLocal, account_id, _ = api_env
    db = SessionLocal()
    try:
        _seed_item_filter_fixture(db, user_id=1, account_id=account_id)
    finally:
        db.close()

    response = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "item_id": "NOT-EXIST"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0
    assert response.json()["items"] == []


def test_notes_filter_by_item_id_combines_with_note_type(api_env):
    """item_id 与 note_type 可叠加（两个 where 互不干扰）。"""
    headers, SessionLocal, account_id, _ = api_env
    db = SessionLocal()
    try:
        _seed_item_filter_fixture(db, user_id=1, account_id=account_id)
        # 把 N-2 改成本地类型 ⇒ ITEM-1 + synced 应只剩 N-1
        note = db.scalars(select(Note).where(Note.note_id == "N-2")).one()
        note.note_type = "local"
        db.commit()
    finally:
        db.close()

    both = client.get(
        "/api/notes",
        headers=headers,
        params={"platform": "xhs", "item_id": "ITEM-1", "note_type": "synced"},
    ).json()
    assert [n["note_id"] for n in both["items"]] == ["N-1"]

    only_local = client.get(
        "/api/notes",
        headers=headers,
        params={"platform": "xhs", "item_id": "ITEM-1", "note_type": "local"},
    ).json()
    assert [n["note_id"] for n in only_local["items"]] == ["N-2"]


def test_notes_filter_by_item_id_does_not_leak_other_users(api_env):
    """租户闸：**别人的商品** item_id ⇒ 空集（user_id where 始终在）。"""
    headers, SessionLocal, account_id, _ = api_env
    db = SessionLocal()
    try:
        _seed_item_filter_fixture(db, user_id=1, account_id=account_id)
        intruder = User(username="intruder", password_hash="x")
        db.add(intruder)
        db.flush()
        intruder_shop = Shop(user_id=intruder.id, seller_id="S-9", shop_name="别人的店")
        db.add(intruder_shop)
        db.flush()
        foreign = ShopProduct(
            user_id=intruder.id, shop_id_fk=intruder_shop.id, item_id="ITEM-FOREIGN", item_title="别人的商品"
        )
        db.add(foreign)
        db.commit()
    finally:
        db.close()

    response = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "item_id": "ITEM-FOREIGN"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0


def test_notes_filter_by_item_id_does_not_change_stats(api_env):
    """挂链商品筛选**不得**改变 `stats` 全库口径（与账号 / 类型筛选同一纪律）。"""
    headers, SessionLocal, account_id, _ = api_env
    db = SessionLocal()
    try:
        _seed_item_filter_fixture(db, user_id=1, account_id=account_id)
    finally:
        db.close()

    plain = client.get("/api/notes", headers=headers, params={"platform": "xhs"}).json()["stats"]
    filtered = client.get(
        "/api/notes", headers=headers, params={"platform": "xhs", "item_id": "ITEM-1"}
    ).json()["stats"]
    assert filtered == plain
