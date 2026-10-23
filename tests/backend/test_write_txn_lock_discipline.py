"""写事务不得跨网络 I/O 持有（设计 13 §24.7：任务隔离）。

**守卫方法**：把 `download_asset_to_local` 打桩成「下载的那一刻用**第二个
连接**读库」。WAL 模式下未提交的写对其它连接不可见 ⇒

* 实现是「先落笔记行（commit）→ 再下载」⇒ 第二个连接**看得到**那条笔记；
* 实现退回「flush 后下载、页末才 commit」⇒ 第二个连接看不到（写锁还被
  持着）⇒ 测试红。

这条纪律盯的是用户 2026-09-23 口径「不同类型的任务应互不影响执行，不应该
所有类型的任务均排序等待」：旧实现在一页 50 条 × 约 4 图的下载期间持有
SQLite 写锁数十秒~数分钟，商品同步 / 发布 / 巡检等一切写者撞
`busy_timeout=5000` 后 `database is locked` 失败。
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from backend.app.api.platforms.xhs.crawl import _save_normalized_notes
from backend.app.core.database import Base
from backend.app.models import PlatformAccount, User
from backend.app.services import note_sync_service as nss



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract


def _session(tmp_path, name: str):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False}
    )
    # WAL：读者（第二个连接）不挡写者，且**看不到写者未提交的行** ——
    # 这正是「下载时笔记行是否已提交」的判别基础。
    @event.listens_for(engine, "connect")
    def _wal(dbapi_connection, connection_record):  # noqa: ARG001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=3000")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine), engine


def _seed_account(db):
    user = User(username="owner", password_hash="x")
    db.add(user)
    db.flush()
    account = PlatformAccount(
        user_id=user.id,
        platform="xhs",
        sub_type="creator",
        nickname="创作者",
        status="active",
    )
    db.add(account)
    db.commit()
    return user, account


def _item(note_id: str) -> dict:
    return {
        "note_id": note_id,
        "title": f"标题 {note_id}",
        "content": "",
        "author_name": "作者",
        "image_urls": ["https://example.com/1.jpg", "https://example.com/2.jpg"],
    }


def _note_visible(db_path, note_id: str) -> bool:
    """第二个连接能否看到这条笔记（未提交 ⇒ 看不到）。"""
    conn = sqlite3.connect(db_path, timeout=3)
    try:
        row = conn.execute(
            "SELECT 1 FROM notes WHERE note_id = ? LIMIT 1", (note_id,)
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def test_save_normalized_notes_commits_note_before_downloading(tmp_path, monkeypatch):
    """`_save_normalized_notes`：下载发生时笔记行必须**已提交**（§24.7）。"""
    SessionLocal, engine = _session(tmp_path, "lock-save.db")
    db = SessionLocal()
    try:
        _seed_account(db)
        account = db.scalars(select(PlatformAccount)).first()
    finally:
        db.close()

    seen: list[bool] = []
    db_path = str(tmp_path / "lock-save.db")

    def spy_download(url, user_id, asset_type):  # noqa: ARG001
        seen.append(_note_visible(db_path, "N-1"))
        return None

    monkeypatch.setattr(
        "backend.app.services.asset_downloader.download_asset_to_local", spy_download
    )

    db = SessionLocal()
    try:
        saved = _save_normalized_notes(db, account, [_item("N-1")])
    finally:
        db.close()

    assert len(saved) == 1
    assert seen, "打桩没被调用过 —— 条目没带图片，测试形同虚设"
    assert all(seen), "下载时笔记行还没提交：写锁跨网络持有（§24.7 回归）"
    engine.dispose()


def test_merge_ark_notes_commits_note_before_downloading(tmp_path, monkeypatch):
    """`_merge_ark_notes` 新建分支：同样先落行再下图（§24.7）。"""
    SessionLocal, engine = _session(tmp_path, "lock-ark.db")
    db = SessionLocal()
    try:
        _seed_account(db)
        account = db.scalars(select(PlatformAccount)).first()
    finally:
        db.close()

    seen: list[bool] = []
    db_path = str(tmp_path / "lock-ark.db")

    def spy_download(url, user_id, asset_type):  # noqa: ARG001
        seen.append(_note_visible(db_path, "N-ARK"))
        return None

    monkeypatch.setattr(
        "backend.app.services.asset_downloader.download_asset_to_local", spy_download
    )

    db = SessionLocal()
    try:
        merged = nss._merge_ark_notes(db, account, [_item("N-ARK")])
    finally:
        db.close()

    assert merged == 1
    assert seen, "打桩没被调用过 —— 条目没带图片，测试形同虚设"
    assert all(seen), "下载时笔记行还没提交：写锁跨网络持有（§24.7 回归）"
    engine.dispose()
