"""笔记的「话题」关系表 + 商品信息走关系表查询（设计 13 §28 / §34）。

用户 2026-09-22 两条口径（② 取代 ① 的商品部分）：
① 「笔记内容字段增加话题和专有解耦……」；
② 「笔记表**不要再存商品信息**，直接通过笔记和商品关联表进行查询，同步落库时也进行
**拆分**，将笔记与商品的关系落在笔记和商品关系表中」。

⚠️ **2026-09-25 §34 起话题改走关系表**（`topics` + `note_topics`；`topics_json` 列已删，
原 `tags` / `note_tags` 标签表并入话题表）—— 本文件的话题断言全部改盯关系表，
专项测试在 `test_note_topics.py`。

本文件守的纪律：

1. **话题关系表**：详情 / 列表拿到话题 ⇒ 落 `topics` + `note_topics`（只增不删）；
2. **notes 表没有商品列**（源码级守卫）—— 商品信息只在 `shop_products` 存一份；
3. **查询走关系表**：`GET /notes` 的 `goods` 是 `note_goods` join `shop_products`
   的投影 ⇒ 商品表改字段，投影跟着变（不是笔记行里的快照）；
4. **序列化三处一致**：列表 / 详情 / 导出同一份输出带 `topics` 与 `goods`。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.api.platforms.xhs import pc
from backend.app.api.platforms.xhs.crawl import _save_normalized_notes
from backend.app.core.database import Base, get_db
from backend.app.main import app
from backend.app.models import Note, NoteGoods, NoteTopic, PlatformAccount, ShopProduct, Topic, User
from backend.app.services import note_sync_service as nss
from backend.app.services import note_topic_service as nts
from tests.backend.cookie_fixtures import cookie_version



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract

client = TestClient(app)

#: 真机形状的笔记 id（24 位 hex）。
HEX_NOTE_ID = "6a7d14b20000000028001cb8"

#: 账号自己的 www jar（详情补齐唯一合法来源）。
WWW_JAR = "a1=x; web_session=own-ws; id_token=own-it"


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
        user_id=user.id, platform="xhs", sub_type="creator",
        nickname="我的号", status="active",
    )
    db.add(account)
    db.flush()
    db.commit()
    return user, account


def _item(note_id: str, *, title: str = "", content: str = "", tags=None) -> dict:
    return {
        "note_id": note_id,
        "title": title or f"标题 {note_id}",
        "content": content,
        "author_name": "作者",
        "image_urls": [],
        **({"tags": tags} if tags is not None else {}),
    }


@pytest.fixture
def no_download(monkeypatch):
    monkeypatch.setattr(
        "backend.app.services.asset_downloader.download_asset_to_local", lambda *a, **k: None
    )


def _detail_payload(note_id: str, *, desc: str = "", tags=None) -> dict:
    card: dict = {"note_id": note_id, "desc": desc, "tag_list": tags or []}
    card["image_list"] = []
    return {"data": {"items": [{"id": note_id, "note_card": card}]}}


def _topic_names_of(db, note_pk: int) -> list[str]:
    """该笔记的话题名（关系行 id 排序 = 落库顺序）。"""
    return nts.note_topic_names(db, note_pk)


# --------------------------------------------------------------------------- #
# 1) 话题关系表（§34；原「专有列」断言已改盯关系表）
# --------------------------------------------------------------------------- #
def test_new_note_has_no_topic_relations(tmp_path, no_download):
    """新建行没有话题关系（采集条目不带话题时不写坏）。"""
    SessionLocal = _session(tmp_path, "topics-default.db")
    db = SessionLocal()
    try:
        _, account = _seed_account(db)
        _save_normalized_notes(db, account, [_item("N-1")])
        note = db.scalars(select(Note).where(Note.note_id == "N-1")).one()
        assert _topic_names_of(db, note.id) == []
    finally:
        db.close()


def test_list_item_topics_land_in_relations(tmp_path, no_download):
    """列表条目自带话题 ⇒ 落 topics + note_topics；没有则不碰本地已有关系。"""
    SessionLocal = _session(tmp_path, "topics-list.db")
    db = SessionLocal()
    try:
        _, account = _seed_account(db)
        _save_normalized_notes(db, account, [_item("N-1", tags=["家居", "好物"])])
        note = db.scalars(select(Note).where(Note.note_id == "N-1")).one()
        assert _topic_names_of(db, note.id) == ["家居", "好物"]

        # 第二次落库不带话题 ⇒ 不抹掉已写的关系（只增不删）
        _save_normalized_notes(db, account, [_item("N-1")])
        db.refresh(note)
        assert _topic_names_of(db, note.id) == ["家居", "好物"]
    finally:
        db.close()


def test_hydrate_writes_topics_from_detail(tmp_path, monkeypatch):
    """详情补齐：详情有非空话题 ⇒ 落关系；没有 ⇒ 不碰。"""
    SessionLocal = _session(tmp_path, "topics-hydrate.db")
    monkeypatch.setattr(
        "backend.app.services.asset_downloader.download_asset_to_local", lambda *a, **k: None
    )
    db = SessionLocal()
    try:
        _, account = _seed_account(db)
        db.add(cookie_version(account.id, WWW_JAR, "www"))
        note = Note(
            platform_account_id=account.id, user_id=account.user_id,
            note_id=HEX_NOTE_ID, platform="xhs", title="t",
            # §19.13.9：无令牌会被拦成 `no_token` ⇒ 夹具给令牌（真实形态）。
            raw_json={
                "note_url": f"https://www.xiaohongshu.com/explore/{HEX_NOTE_ID}",
                "xsec_token": "TK-TEST",
            },
        )
        db.add(note)
        db.commit()

        tags_seen = {"tags": ["收纳", "桌面"]}

        class FakeAdapter:
            def __init__(self, cookies: str) -> None:
                self.cookies = cookies

            def get_note_info(self, url: str):
                return True, "ok", _detail_payload(HEX_NOTE_ID, desc="正文", tags=tags_seen["tags"])

        monkeypatch.setattr(pc, "get_xhs_pc_api_adapter_factory", lambda: FakeAdapter)
        nss.sync_note_from_platform(db, note)

        assert _topic_names_of(db, note.id) == ["收纳", "桌面"]

        # 详情这次没给话题 ⇒ 不抹本地（只增不删）
        tags_seen["tags"] = []
        nss.sync_note_from_platform(db, note)
        assert _topic_names_of(db, note.id) == ["收纳", "桌面"]
    finally:
        db.close()


def test_migration_backfills_topics_from_raw_json():
    """（历史迁移）`s1t2u3v4w5x6` 必须含「从 `raw_json` 回填 topics」的逻辑。

    ⚠️ 该列已于 2026-09-25 删除（§34，迁移 `b2c3d4e5f6g7`）—— 本测试盯的是
    **历史迁移文件自身**的候选键与可逆性，不是现行列。
    """
    from pathlib import Path

    migrations = sorted(Path("backend/alembic/versions").glob("s1t2u3v4w5x6_add_note_topics.py"))
    assert migrations, "找不到 topics 迁移文件"
    source = migrations[0].read_text(encoding="utf-8")
    assert 'revision = "s1t2u3v4w5x6"' in source
    assert 'down_revision = "r0s1t2u3v4w5"' in source
    for key in ("tag_list", "tags", "topics"):
        assert key in source, f"回填候选键 {key} 不能丢"
    assert "drop_column" in source, "downgrade 必须删列（可逆）"


# --------------------------------------------------------------------------- #
# 2) 商品信息不落笔记列 + 走关系表查询
# --------------------------------------------------------------------------- #
def test_notes_model_has_no_goods_column():
    """**notes 表没有商品列**（用户 ② 口径）—— 商品在 shop_products、关系在 note_goods。

    同时守 §34：`topics_json` 列已删（话题走关系表），`tags` / `note_tags` 不在 metadata。
    """
    column_names = {column.name for column in Note.__table__.columns}
    assert "goods_json" not in column_names
    assert "goods" not in column_names
    assert "topics_json" not in column_names
    tables = set(Base.metadata.tables)
    assert "tags" not in tables and "note_tags" not in tables
    assert {"topics", "note_topics"} <= tables


@pytest.fixture
def api_env(tmp_path):
    """一个用户 + 一个账号 + 一条 synced 笔记 + 两件本地商品 + 两条关系行。"""
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
            user, account = _seed_account(db)
            account.seller_id = "SELLER-1"
            db.flush()
            note = Note(
                platform_account_id=account.id, user_id=user.id,
                note_id=HEX_NOTE_ID, platform="xhs", title="挂了商品的笔记",
                note_type="synced",
            )
            db.add(note)
            db.flush()
            # 话题（§34）：走关系表落库（原 topics_json 专有列已删）
            nts.apply_sync_topics(db, note, ["话题一", "话题二"])
            db.commit()
            for item_id, title in (("ITEM-1", "商品一"), ("ITEM-2", "商品二")):
                product = ShopProduct(
                    user_id=user.id, platform_account_id=account.id, item_id=item_id,
                    item_title=title, price="1999", images_json='["https://img/1.jpg"]',
                )
                db.add(product)
                db.flush()
                # §26.7：关系表按平台 ID 落（`note.note_id` ↔ `product.item_id`）
                db.add(
                    NoteGoods(
                        user_id=note.user_id, note_id=note.note_id, item_id=product.item_id
                    )
                )
            db.commit()
            note_id = note.id
        finally:
            db.close()
        yield headers, SessionLocal, note_id
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_serialized_goods_come_from_the_relation_join(api_env):
    """`goods` 走 `note_goods` join `shop_products`：商品表改标题，投影跟着变。"""
    headers, SessionLocal, note_id = api_env

    listed = client.get("/api/notes", headers=headers, params={"platform": "xhs"}).json()
    goods = listed["items"][0]["goods"]
    assert [g["item_title"] for g in goods] == ["商品一", "商品二"]
    assert goods[0]["price"] == "1999"  # 分为单位的字符串（展示层换算）

    # 改商品表的标题 ⇒ 再查，投影是**当下的真值**（不是笔记行里的快照）
    db = SessionLocal()
    try:
        product = db.scalars(select(ShopProduct).where(ShopProduct.item_id == "ITEM-1")).one()
        product.item_title = "改过标题的商品一"
        db.commit()
    finally:
        db.close()

    detail = client.get(f"/api/notes/{note_id}", headers=headers).json()
    assert [g["item_title"] for g in detail["goods"]] == ["改过标题的商品一", "商品二"]


def test_serialized_topics_come_from_the_relation_join(api_env):
    """`topics` 读关系表 join 话题字典（`[{id,name,color}]`；不再有专列 / tags 字段）。"""
    headers, _, note_id = api_env

    detail = client.get(f"/api/notes/{note_id}", headers=headers).json()
    assert [t["name"] for t in detail["topics"]] == ["话题一", "话题二"]
    assert all({"id", "name", "color"} <= set(t) for t in detail["topics"])
    assert "tags" not in detail


def test_note_without_goods_serializes_empty_list(api_env):
    """没挂商品的笔记 `goods == []`（不是错误，是「没挂」）。"""
    headers, SessionLocal, _ = api_env
    db = SessionLocal()
    try:
        user = db.scalars(select(User)).one()
        account = db.scalars(select(PlatformAccount)).one()
        db.add(Note(
            platform_account_id=account.id, user_id=user.id,
            note_id="N-NO-GOODS", platform="xhs", title="没挂商品", note_type="synced",
        ))
        db.commit()
    finally:
        db.close()

    listed = client.get("/api/notes", headers=headers, params={"platform": "xhs"}).json()
    row = next(item for item in listed["items"] if item["note_id"] == "N-NO-GOODS")
    assert row["goods"] == []
    assert row["topics"] == []

# --------------------------------------------------------------------------- #
# 5) 话题增删端点（设计 13 §29.3）：PUT /notes/{id}/topics，整列替换 + 实时落库
# --------------------------------------------------------------------------- #
def test_put_topics_replaces_and_persists(api_env):
    """整列替换：传新列表 ⇒ 返回行 `topics` 即新列表，关系行已落库。"""
    headers, SessionLocal, note_id = api_env

    response = client.put(
        f"/api/notes/{note_id}/topics",
        json={"topics": ["露营", "装备"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert [t["name"] for t in response.json()["topics"]] == ["露营", "装备"]

    # 落库是**即时**的（实时更新数据库）：另开连接读也是新值
    db = SessionLocal()
    try:
        stored = db.scalars(select(Note).where(Note.id == note_id)).one()
        assert nts.note_topic_names(db, stored.id) == ["露营", "装备"]
        # 话题字典行建了（find-or-create）
        assert {t.name for t in db.scalars(select(Topic)).all()} >= {"露营", "装备"}
    finally:
        db.close()


def test_put_topics_add_then_remove_roundtrip(api_env):
    """模拟前端「+ 新增」与「Tag 上 × 删除」两次调用，语义与界面一致。"""
    headers, _, note_id = api_env

    # 新增一个（列表尾部追加）
    response = client.put(
        f"/api/notes/{note_id}/topics",
        json={"topics": ["话题一", "话题二", "新话题"]},
        headers=headers,
    )
    assert [t["name"] for t in response.json()["topics"]] == ["话题一", "话题二", "新话题"]

    # 删掉中间的（其余原样传回，顺序保持）
    response = client.put(
        f"/api/notes/{note_id}/topics",
        json={"topics": ["话题一", "新话题"]},
        headers=headers,
    )
    assert [t["name"] for t in response.json()["topics"]] == ["话题一", "新话题"]


def test_put_topics_normalizes(api_env):
    """归一化（后端唯一实现）：trim / 去空 / 大小写不敏感去重 / 上限截断。"""
    headers, _, note_id = api_env

    raw = [
        "  露营  ",        # trim
        "",                # 去空
        "   ",             # 去空（trim 后为空）
        "露营 ",           # 与首项重复（trim 后同键）
        "CASE",            # 保留首次出现的大小写形态
        "case",            # 与上一项重复（小写同键）
        "x" * 60,          # 超长 ⇒ 截到 50 字
    ]
    raw += [f"话题{i}" for i in range(30)]  # 超 20 个 ⇒ 截到 20

    response = client.put(
        f"/api/notes/{note_id}/topics", json={"topics": raw}, headers=headers
    )
    assert response.status_code == 200, response.text
    topics = [t["name"] for t in response.json()["topics"]]
    assert topics[0] == "露营"
    assert "CASE" in topics and "case" not in topics
    assert len(topics) == 20
    assert all(len(t) <= 50 for t in topics)
    assert "" not in topics and "  " not in "".join(topics)


def test_put_topics_empty_list_clears(api_env):
    """空列表合法（= 清空话题）—— 删到最后一个不报错，不是错误态。"""
    headers, _, note_id = api_env

    response = client.put(
        f"/api/notes/{note_id}/topics", json={"topics": []}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["topics"] == []


def test_put_topics_other_users_note_404(api_env):
    """越权改别人的笔记 ⇒ 404（不暴露「存在但无权」）。"""
    headers, SessionLocal, _ = api_env
    db = SessionLocal()
    try:
        other_user = User(username="intruder", password_hash="x")
        db.add(other_user)
        db.flush()
        other_account = PlatformAccount(
            user_id=other_user.id, platform="xhs", sub_type="creator",
            nickname="别人的号", status="active",
        )
        db.add(other_account)
        db.flush()
        other_note = Note(
            platform_account_id=other_account.id, user_id=other_user.id,
            note_id="FOREIGN-NOTE", platform="xhs", title="别人的笔记",
            note_type="synced",
        )
        db.add(other_note)
        db.commit()
        foreign_id = other_note.id
    finally:
        db.close()

    response = client.put(
        f"/api/notes/{foreign_id}/topics", json={"topics": ["偷改"]}, headers=headers
    )
    assert response.status_code == 404, response.text

    db = SessionLocal()
    try:
        stored = db.scalars(select(Note).where(Note.id == foreign_id)).one()
        assert nts.note_topic_names(db, stored.id) == []  # 一个字节都没被改
        assert not db.scalars(select(NoteTopic).where(NoteTopic.note_id == foreign_id)).all()
    finally:
        db.close()
