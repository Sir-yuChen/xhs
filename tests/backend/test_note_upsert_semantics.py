"""「已存在的笔记只更新、不摧毁」契约（§23，2026-09-22 用户口径：
「如果数据库存在对应的笔记id或商品id则只需要更新本地数据即可」）。

盯的是一个**真bug**：`_save_normalized_notes` 旧实现无条件
``note.content = str(normalized.get("content") or "")``，并且**先删光 assets 再按
列表 URL 重建**。而三个列表源（www / creator / ark）**都不带正文**、图片常常只有
一张封面 ⇒ 一轮重同步会把「详情补齐过的 481 字正文 + 4 张图」**全部抹掉**；
更糟的是 `hydrated_at` 已经写过 ⇒ 候选集（`hydrated_at IS NULL`）再也挑不到它
⇒ **正文永久丢失**。

这与 `_merge_ark_notes` / `hydrate_note_detail` 既有的「填空，不覆盖」纪律相反，
是本文件要钉住的东西。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.api.platforms.xhs.crawl import _save_normalized_notes
from backend.app.core.database import Base
from backend.app.core.time import shanghai_now
from backend.app.models import Note, NoteAsset, PlatformAccount, User



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'upsert.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _account(db) -> PlatformAccount:
    user = User(username="upsert-owner", password_hash="x")
    db.add(user)
    db.flush()
    account = PlatformAccount(
        user_id=user.id, platform="xhs", sub_type="creator", nickname="t", status="active"
    )
    db.add(account)
    db.commit()
    return account


def _list_item(note_id: str, *, images: int = 1) -> dict:
    """**列表源**形状：没有 content、图片只有封面（三个源都这样）。"""
    return {
        "note_id": note_id,
        "title": f"标题 {note_id}",
        "content": "",                     # 列表源恒为空
        "author_name": "作者",
        "image_urls": [f"https://img.example/{note_id}-{i}.jpg" for i in range(images)],
        "raw": {"note_url": f"https://x/{note_id}"},
    }


class TestExistingNoteIsUpdatedNotDestroyed:
    """重同步同一批 note_id ⇒ 更新，不摧毁详情补齐过的数据。"""

    def test_resync_keeps_the_hydrated_body(self, tmp_path):
        """详情补到的正文**不被重同步抹掉**（列表源没有正文，不是「平台删了正文」）。"""
        db = _db(tmp_path)
        try:
            account = _account(db)
            _save_normalized_notes(db, account, [_list_item("N1")], published=True)
            db.commit()

            # 模拟详情补齐：写正文 + 4 张图 + hydrated_at
            note = db.scalars(select(Note).where(Note.note_id == "N1")).one()
            note.content = "补齐到的 481 字正文"
            note.hydrated_at = shanghai_now()
            db.add(NoteAsset(note_id=note.id, asset_type="image", url="https://img.example/N1-0.jpg"))
            for i in range(1, 4):
                db.add(NoteAsset(note_id=note.id, asset_type="image", url=f"https://img.example/N1-h{i}.jpg"))
            db.commit()

            # 同一批 note_id 再同步一次（全量 / 增量都会走到这里）
            _save_normalized_notes(db, account, [_list_item("N1")], published=True)
            db.commit()

            again = db.scalars(select(Note).where(Note.note_id == "N1")).one()
            assert again.content == "补齐到的 481 字正文", "重同步把详情补到的正文抹掉了"
        finally:
            db.close()

    def test_resync_never_shrinks_the_image_set(self, tmp_path):
        """重同步**只增不删**图片：详情补到的 4 张不能被列表的 1 张封面挤掉。"""
        db = _db(tmp_path)
        try:
            account = _account(db)
            _save_normalized_notes(db, account, [_list_item("N1", images=1)], published=True)
            db.commit()
            note = db.scalars(select(Note).where(Note.note_id == "N1")).one()
            for i in range(1, 4):
                db.add(NoteAsset(note_id=note.id, asset_type="image", url=f"https://img.example/N1-h{i}.jpg"))
            db.commit()
            before = db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id)).all()
            assert len(before) == 4

            _save_normalized_notes(db, account, [_list_item("N1", images=1)], published=True)
            db.commit()

            after = db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id)).all()
            assert len(after) == 4, f"重同步把图集从 4 张删成 {len(after)} 张"
        finally:
            db.close()

    def test_resync_still_refreshes_the_platform_fields(self, tmp_path):
        """「不摧毁」不等于「不更新」：标题等平台字段必须照最新值刷新。"""
        db = _db(tmp_path)
        try:
            account = _account(db)
            _save_normalized_notes(db, account, [_list_item("N1")], published=True)
            db.commit()

            changed = _list_item("N1")
            changed["title"] = "改过的标题"
            _save_normalized_notes(db, account, [changed], published=True)
            db.commit()

            note = db.scalars(select(Note).where(Note.note_id == "N1")).one()
            assert note.title == "改过的标题", "平台字段没刷新 = 同步失去了意义"
            # 只有一行，没有重复插入
            assert db.scalars(select(Note).where(Note.note_id == "N1")).all().__len__() == 1
        finally:
            db.close()

    def test_published_flag_still_only_goes_up(self, tmp_path):
        """§19.12 的单调性不因本次修改失效：采集（published=False）不降级。"""
        db = _db(tmp_path)
        try:
            account = _account(db)
            _save_normalized_notes(db, account, [_list_item("N1")], published=True)
            db.commit()
            _save_normalized_notes(db, account, [_list_item("N1")], published=False)
            db.commit()

            note = db.scalars(select(Note).where(Note.note_id == "N1")).one()
            assert note.published is True, "采集链路把已发布降级成了待发布"
        finally:
            db.close()
