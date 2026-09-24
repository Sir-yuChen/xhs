"""视频笔记的可播放地址必须落库（2026-09-22，内容库「视频应支持播放」）。

**真机探针结论**（只读 GET 详情，账号 8 / 观隅好物）：
* 创作者**列表**只给 `video_info: {"duration": 12}` —— **不带流地址**；
* **详情**端点给 `sns-video-v2.xhscdn.com/stream/…​.mp4?sign=…`，归一化后
  `video_url` / `video_addr` 都有值。

**旧实现的缺口**（两条，叠加导致 843 条视频一条都播不了）：
1. `hydrate_note_detail` 拿到了 `video_url` 却只 merge 图片 ⇒ 地址被丢掉；
2. 补齐候选集是 `hydrated_at IS NULL`，而存量视频笔记的 `hydrated_at` 早就写上
   了 ⇒ 永远不会再被补。

修法：① 详情补齐时把视频流地址也写成 `asset_type="video"` 的资产；
② 候选口径（§19.14 起是 `_needs_hydration`）加上「视频笔记但没有视频资产」
这一类，存量自愈。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select

from backend.app.models import Note, NoteAsset


def _detail_payload_with_video(note_id: str, *, video: str, images: list[str] | None = None) -> dict:
    """带**视频流地址**的详情响应（形状抄自真机探针的归一化输入）。"""
    card: dict[str, Any] = {
        "note_id": note_id,
        "desc": "正文",
        "video_addr": video,
        "video_url": video,
    }
    card["image_list"] = [{"url": url} for url in (images or [])]
    return {"data": {"items": [{"id": note_id, "note_card": card}]}}


class _AllowLimiter:
    """测试替身：立即放行（§21.3 等待制在测试里不真等待）。"""

    def allow(self, key):  # noqa: ARG002
        return True

    def wait_allow(self, key, **kwargs):  # noqa: ARG002
        return True


class TestVideoAssetIsStored:
    """视频流地址必须变成本地视频资产，否则内容库无从播放。"""

    def test_detail_hydration_stores_the_video_asset(self, tmp_path: Path, monkeypatch: Any):
        """一次详情同时补正文 / 图 / **视频**；视频写成 `asset_type="video"`。"""
        import backend.app.services.note_sync_service as nss
        from tests.backend.test_note_content_backfill import (
            _add_note,
            _asset_urls,
            _build_db,
            _no_download,
        )

        _no_download(monkeypatch)
        _db_dep, db, creator = _build_db(tmp_path)
        try:
            note = _add_note(
                db, creator, "6a5c265300000000010310be",
                raw={"note_url": "https://x/v", "type": "video"},
            )

            class FakeAdapter:
                def __init__(self, cookies):
                    pass

                def get_note_info(self, url):
                    return True, "ok", _detail_payload_with_video(
                        "6a5c265300000000010310be",
                        video="http://sns-video-v2.xhscdn.com/stream/1/110/259/abc_259.mp4?sign=S",
                        images=["https://img/cover"],
                    )

            result = nss._hydrate_page_notes(
                db, creator, [note.note_id],
                cookies=nss._account_cookies(db, creator.id, channel="www"),
                adapter=FakeAdapter("x"),
                limiter=_AllowLimiter(),
            )

            stored = db.scalars(select(Note).where(Note.note_id == "6a5c265300000000010310be")).one()
            video_assets = [
                a for a in db.scalars(select(NoteAsset).where(NoteAsset.note_id == stored.id)).all()
                if a.asset_type == "video"
            ]
            assert len(video_assets) == 1, "视频流地址没落库 ⇒ 内容库播不了"
            assert video_assets[0].url.endswith(".mp4?sign=S")
            # 封面图也还在（视频笔记的列表封面）
            assert "https://img/cover" in _asset_urls(db, stored)
            assert result["filled"] == 1, "补到视频资产要计入本轮补齐条数"
        finally:
            db.close()

    def test_video_notes_without_video_asset_are_hydration_candidates(self, tmp_path: Path, monkeypatch: Any):
        """**存量自愈**：`hydrated_at` 已写但缺视频资产的视频笔记仍要进候选口径。

        这条钉的是缺口 ②：旧候选集只看 `hydrated_at IS NULL`，存量视频笔记
        早就「补过了」⇒ 永远不会再补 ⇒ 843 条视频永远播不了。
        """
        import backend.app.services.note_sync_service as nss
        from tests.backend.test_note_content_backfill import _add_note, _build_db

        _db_dep, db, creator = _build_db(tmp_path)
        try:
            # 已补齐过的视频笔记（hydrated_at 非空、没有视频资产）
            video_note = _add_note(
                db, creator, "6a5c265300000000010310be",
                raw={"note_url": "https://x/v", "type": "video"},
            )
            # 已补齐过的图文笔记 —— **不该**进候选集（没有欠账）
            image_note = _add_note(
                db, creator, "6a7d1c62000000002202cae1",
                raw={"note_url": "https://x/p", "type": "normal"},
            )
            from backend.app.core.time import shanghai_now

            for row in db.scalars(select(Note)).all():
                row.hydrated_at = shanghai_now()
            db.commit()

            assert nss._needs_hydration(db, video_note) is True, "缺视频资产的视频笔记没进候选口径"
            assert nss._needs_hydration(db, image_note) is False, "图文笔记没有欠账，不该进候选口径"
        finally:
            db.close()

    def test_image_note_never_becomes_a_candidate_just_for_having_no_video(
        self, tmp_path: Path, monkeypatch: Any
    ):
        """图文笔记**不会**因为「没有视频资产」被反复选中（否则每轮都白打详情）。"""
        import backend.app.services.note_sync_service as nss
        from tests.backend.test_note_content_backfill import _add_note, _build_db

        _db_dep, db, creator = _build_db(tmp_path)
        try:
            note = _add_note(
                db, creator, "6a7d1c62000000002202cae1",
                raw={"note_url": "https://x/p", "type": "normal"},
            )
            from backend.app.core.time import shanghai_now

            row = db.scalars(select(Note).where(Note.note_id == "6a7d1c62000000002202cae1")).one()
            row.hydrated_at = shanghai_now()
            db.commit()

            assert nss._needs_hydration(db, row) is False
        finally:
            db.close()
