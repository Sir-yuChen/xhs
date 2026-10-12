"""「我的笔记」离线数据落库（`note_data_service` + 端点）测试。

覆盖：
* butterfly 响应 → `NoteDataRow` 的字段映射（解包 `{"value": ...}` 结构）
* xlsx 行 → `NoteDataRow`（含 `=HYPERLINK(...)` 公式提取 URL）
* `upsert_note_data_rows` 覆盖更新 + `note_category` 刷新 + `note_goods` 关系幂等
* `GET /notes/offline` 分类校验 + 查询
* `POST /notes/import-data` 分类校验
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.models import Note, NoteDatacenterStat, NoteGoods, User
from backend.app.services.note_data_service import (
    CATEGORY_GOODS,
    CATEGORY_NORMAL,
    NoteDataRow,
    _extract_url_from_hyperlink,
    _note_row_from_butterfly,
    _note_row_from_xlsx,
    parse_butterfly_response,
    upsert_note_data_rows,
)


# --------------------------------------------------------------------------- #
# 纯函数：字段映射
# --------------------------------------------------------------------------- #

class TestButterflyRowMapping:
    def test_maps_core_fields_and_unwraps_value(self):
        row = {
            "noteId": {"value": "6a616611000000000f009848"},
            "discoveryTitle": {"value": "弧形轨道浴帘顶装L形卫生间洗澡挂帘子"},
            "discoveryLink": {"value": "https://www.xiaohongshu.com/explore/6a616611000000000f009848"},
            "nickName": {"value": "被风吹过"},
            "redId": {"value": "710018246"},
            "noteType": {"value": "图文"},
            "notePublishTime": {"value": "2026-07-23 08:53:38"},
            "itemId": {"value": "6a51962619f8df0015b08cb2"},
            "nitemName": {"value": "弧形轨道浴帘"},
            "noteReadCnt": {"value": 38},
            "noteLikeCnt": {"value": 0},
        }
        parsed = _note_row_from_butterfly(row, CATEGORY_GOODS)
        assert parsed.note_id == "6a616611000000000f009848"
        assert parsed.title == "弧形轨道浴帘顶装L形卫生间洗澡挂帘子"
        assert parsed.note_url == "https://www.xiaohongshu.com/explore/6a616611000000000f009848"
        assert parsed.author_name == "被风吹过"
        assert parsed.author_xhs_id == "710018246"
        assert parsed.note_type == "图文"
        assert parsed.published_at == "2026-07-23 08:53:38"
        assert parsed.item_id == "6a51962619f8df0015b08cb2"
        assert parsed.item_title == "弧形轨道浴帘"
        assert parsed.category == CATEGORY_GOODS
        assert parsed.metrics["read_count"] == 38
        assert parsed.metrics["like_count"] == 0

    def test_author_falls_back_to_user_basic(self):
        row = {
            "noteId": {"value": "n1"},
            "userBasic": {
                "value": [{"nickName": "观隅好物", "redId": "293066917", "fansNum": 49}]
            },
            "nickName": {"value": ""},
            "redId": {"value": ""},
        }
        parsed = _note_row_from_butterfly(row, CATEGORY_NORMAL)
        assert parsed.author_name == "观隅好物"
        assert parsed.author_xhs_id == "293066917"

    def test_missing_metrics_are_none_and_excluded(self):
        row = {"noteId": {"value": "n1"}, "noteReadCnt": {"value": None}}
        parsed = _note_row_from_butterfly(row, CATEGORY_NORMAL)
        assert "read_count" not in parsed.metrics  # None 被剔除，不造假

    def test_title_falls_back_to_item_name(self):
        row = {
            "noteId": {"value": "n1"},
            "discoveryTitle": {"value": ""},
            "nitemName": {"value": "商品名"},
        }
        parsed = _note_row_from_butterfly(row, CATEGORY_GOODS)
        assert parsed.title == "商品名"


class TestXlsxRowMapping:
    def test_maps_xlsx_columns(self):
        row_dict = {
            "笔记标题": "弧形轨道浴帘",
            "笔记ID": "6a616611000000000f009848",
            "笔记链接": '=HYPERLINK("https://www.xiaohongshu.com/explore/6a61?xsec_token=abc","https://www.xiaohongshu.com/explore/6a61?xsec_token=abc")',
            "作者昵称": "被风吹过",
            "作者xhs_ID": "710018246",
            "笔记创建时间": "2026-07-23 08:53:38",
            "笔记类型": "图文",
            "关联商品名称": "弧形轨道浴帘",
            "关联商品ID": "6a51962619f8df0015b08cb2",
            "笔记阅读数": 38,
            "点赞次数": 0,
        }
        parsed = _note_row_from_xlsx(row_dict, CATEGORY_GOODS)
        assert parsed.note_id == "6a616611000000000f009848"
        assert parsed.title == "弧形轨道浴帘"
        assert parsed.note_url == "https://www.xiaohongshu.com/explore/6a61?xsec_token=abc"
        assert parsed.author_name == "被风吹过"
        assert parsed.author_xhs_id == "710018246"
        assert parsed.item_id == "6a51962619f8df0015b08cb2"
        assert parsed.metrics["read_count"] == 38
        assert parsed.metrics["like_count"] == 0

    def test_hyperlink_formula_extraction(self):
        formula = (
            '=HYPERLINK("https://www.xiaohongshu.com/explore/6a61?xsec_token=TB-abc=0&xsec_source=pc",'
            '"https://www.xiaohongshu.com/explore/6a61?xsec_token=TB-abc=0&xsec_source=pc")'
        )
        assert _extract_url_from_hyperlink(formula) == (
            "https://www.xiaohongshu.com/explore/6a61?xsec_token=TB-abc=0&xsec_source=pc"
        )

    def test_plain_url_passes_through(self):
        assert _extract_url_from_hyperlink("https://x.com/note/1") == "https://x.com/note/1"

    def test_float_metric_handles_integer(self):
        row_dict = {"笔记标题": "t", "笔记ID": "n1", "笔记支付金额": 0.0, "视频时长（秒）": 9.0}
        parsed = _note_row_from_xlsx(row_dict, CATEGORY_GOODS)
        assert parsed.metrics["pay_gmv"] == 0.0
        assert parsed.metrics["video_duration"] == 9


class TestParseButterflyResponse:
    def test_parses_data_block(self):
        payload = {
            "data": [
                {
                    "blockKey": "arkSellerNoteGoodsNoteDataList",
                    "data": [
                        {"noteId": {"value": "n1"}, "discoveryTitle": {"value": "标题1"}},
                        {"noteId": {"value": "n2"}, "discoveryTitle": {"value": "标题2"}},
                    ],
                }
            ]
        }
        rows = parse_butterfly_response(payload, CATEGORY_GOODS)
        assert len(rows) == 2
        assert [r.note_id for r in rows] == ["n1", "n2"]

    def test_empty_data_returns_empty(self):
        assert parse_butterfly_response({"data": []}, CATEGORY_GOODS) == []
        assert parse_butterfly_response({"data": [{"data": []}]}, CATEGORY_GOODS) == []


# --------------------------------------------------------------------------- #
# 落库：覆盖更新 + note_category + note_goods
# --------------------------------------------------------------------------- #

class TestUpsertNoteDataRows:
    def _seed(self, db):
        from backend.app.models.platform_account import PlatformAccount

        if db.get(User, 1) is None:
            db.add(User(id=1, username="u1", password_hash="x"))
            db.flush()
        if db.get(PlatformAccount, 10) is None:
            db.add(PlatformAccount(id=10, user_id=1, platform="xhs", nickname="acc10"))
        db.commit()

    def test_creates_and_updates(self, db_factory):
        db = db_factory()
        self._seed(db)

        rows = [NoteDataRow(note_id="n1", title="标题1", category=CATEGORY_GOODS)]
        result = upsert_note_data_rows(db, rows, user_id=1, account_id=10)
        db.commit()
        assert result == {"created": 1, "updated": 0, "total": 1, "skipped_deleted": 0}

        # 基础信息落 notes；指标/分类落统计表 note_flag == "goods_note"。
        note = db.scalar(select(Note).where(Note.note_id == "n1"))
        stat = db.scalar(select(NoteDatacenterStat).where(NoteDatacenterStat.note_id == "n1"))
        assert stat.note_flag == "goods_note"
        assert note.title == "标题1"

        # 再落一次（覆盖更新，category 从 goods → normal ⇒ note_flag 清空）
        rows2 = [NoteDataRow(note_id="n1", title="新标题", category=CATEGORY_NORMAL)]
        result2 = upsert_note_data_rows(db, rows2, user_id=1, account_id=10)
        db.commit()
        assert result2 == {"created": 0, "updated": 1, "total": 1, "skipped_deleted": 0}
        db.refresh(note)
        db.refresh(stat)
        assert stat.note_flag == ""
        assert note.title == "新标题"

    def test_note_goods_relation_idempotent(self, db_factory):
        db = db_factory()
        self._seed(db)

        row = NoteDataRow(
            note_id="n1", title="t", category=CATEGORY_GOODS,
            item_id="item1", item_title="商品1",
        )
        upsert_note_data_rows(db, [row], user_id=1, account_id=10)
        db.commit()
        # 重复落库（幂等，不产生重复关系行）
        upsert_note_data_rows(db, [row], user_id=1, account_id=10)
        db.commit()

        rels = db.scalars(
            select(NoteGoods).where(NoteGoods.note_id == "n1", NoteGoods.item_id == "item1")
        ).all()
        assert len(rels) == 1


# --------------------------------------------------------------------------- #
# 端点：分类校验
# --------------------------------------------------------------------------- #

class TestOfflineEndpoint:
    def _auth(self, api_client):
        r = api_client.post("/api/auth/register", json={"username": "note_data_user", "password": "secret123"})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def test_rejects_unknown_category(self, api_client):
        headers = self._auth(api_client)
        resp = api_client.get("/api/notes/offline", params={"category": "bogus"}, headers=headers)
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "NOTE_CATEGORY_UNSUPPORTED"

    def test_lists_offline_notes(self, api_client, db_factory):
        headers = self._auth(api_client)
        resp = api_client.get("/api/notes/offline", params={"category": "goods"}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["items"] == []


class TestImportEndpoint:
    def _auth(self, api_client):
        r = api_client.post("/api/auth/register", json={"username": "note_import_user", "password": "secret123"})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def test_rejects_unknown_category(self, api_client):
        headers = self._auth(api_client)
        resp = api_client.post(
            "/api/notes/import-data",
            data={"category": "bogus", "account_id": "10"},
            files={"file": ("d.xlsx", b"dummy", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "NOTE_CATEGORY_UNSUPPORTED"


# --------------------------------------------------------------------------- #
# 详情补齐（需求 4：根据笔记原链接匿名拉媒体/正文/话题落库）
# --------------------------------------------------------------------------- #

class TestHydrateOfflineNotes:
    """`hydrate_offline_notes` —— 无需登录、匿名 SSR、幂等、话题拆表。

    用 `session_factory` 注入测试库 + `fetch_payload` 替身，不碰真库、不打真网络。
    """

    def _seed(self, db):
        from backend.app.models.platform_account import PlatformAccount

        if db.get(User, 1) is None:
            db.add(User(id=1, username="u1", password_hash="x"))
            db.flush()
        if db.get(PlatformAccount, 10) is None:
            db.add(PlatformAccount(id=10, user_id=1, platform="xhs", nickname="acc10"))
        db.commit()

    def _make_note(self, db, note_id: str, *, category=CATEGORY_GOODS, xsec_token="tok1",
                   content="", hydrated_at=None):
        # note_id 必须是平台形态（全 hex 且 ≥16 位），`_note_detail_url` 才认（否则判 no_url）。
        raw = {"note_url": f"https://www.xiaohongshu.com/explore/{note_id}", "xsec_token": xsec_token}
        note = Note(
            user_id=1, platform_account_id=10, platform="xhs",
            note_id=note_id, title=f"标题{note_id}", content=content,
            raw_json=raw,
        )
        # 数据中心统计行（2026-10-12 设计 35：`hydrate_offline_notes` 按统计表存在性判离线）。
        db.add(NoteDatacenterStat(
            user_id=1, platform_account_id=10, note_id=note_id,
            stat_date="2026-10-12", window="recent_1",
            note_flag="goods_note" if category == CATEGORY_GOODS else "",
            title=f"标题{note_id}",
        ))
        if hydrated_at is not None:
            note.hydrated_at = hydrated_at
        db.add(note)
        db.commit()
        return note

    _HEX = "6a714259000000002403ced4"  # 平台形态 note_id（24 位 hex）

    def _payload(self, *, desc="正文内容", tags=("话题A", "话题B"), images=("http://img/1.jpg",)):
        return (True, "成功", {
            "data": {"items": [{"note_card": {
                "note_id": "x", "title": "标题", "desc": desc,
                "image_list": [{"url_default": u} for u in images],
                "tag_list": [{"name": t} for t in tags],
                "type": "normal",
            }}]},
        })

    def _patch_limiter(self, monkeypatch):
        class _FakeLimiter:
            def wait_allow(self, key):
                return True
        monkeypatch.setattr(
            "backend.app.services.rate_limiter.get_rate_limiter",
            lambda: _FakeLimiter(),
        )

    def test_fills_content_topics_and_media(self, db_factory, monkeypatch):
        db = db_factory()
        self._seed(db)
        self._make_note(db, self._HEX)
        self._patch_limiter(monkeypatch)

        calls = []
        def fake_fetch(url):
            calls.append(url)
            return self._payload()
        monkeypatch.setattr(
            "backend.app.adapters.xhs.note_detail_ssr.fetch_note_detail_payload",
            lambda url: fake_fetch(url),
        )

        from backend.app.services.note_data_service import hydrate_offline_notes
        from backend.app.models import NoteAsset, Topic, NoteTopic
        from backend.app.core.time import shanghai_now

        stats = hydrate_offline_notes(
            account_id=10, user_id=1, category=CATEGORY_GOODS,
            session_factory=db_factory, fetch_payload=fake_fetch,
        )
        assert stats["filled"] == 1
        assert stats["attempted"] == 1

        note = db.scalar(select(Note).where(Note.note_id == self._HEX))
        assert note.content == "正文内容"
        assert note.hydrated_at is not None
        # 话题拆表：topics + note_topics
        topics = db.scalars(select(Topic).where(Topic.user_id == 1, Topic.name.in_(["话题A", "话题B"]))).all()
        assert {t.name for t in topics} == {"话题A", "话题B"}
        rels = db.scalars(select(NoteTopic).where(NoteTopic.note_id == note.id)).all()
        assert len(rels) == 2
        # 媒体图 → note_assets
        assets = db.scalars(select(NoteAsset).where(NoteAsset.note_id == note.id)).all()
        assert [a.url for a in assets] == ["http://img/1.jpg"]
        db.close()

    def test_skips_already_hydrated(self, db_factory, monkeypatch):
        db = db_factory()
        self._seed(db)
        from backend.app.core.time import shanghai_now
        self._make_note(db, self._HEX, hydrated_at=shanghai_now())

        from backend.app.services.note_data_service import hydrate_offline_notes
        called = {"n": 0}
        def fake_fetch(url):
            called["n"] += 1
            return self._payload()
        stats = hydrate_offline_notes(
            account_id=10, user_id=1, category=CATEGORY_GOODS,
            session_factory=db_factory, fetch_payload=fake_fetch,
        )
        assert stats["attempted"] == 0
        assert called["n"] == 0, "已补齐（非视频）的笔记不重复拉"
        db.close()

    def test_no_token_counts_no_url(self, db_factory, monkeypatch):
        db = db_factory()
        self._seed(db)
        self._make_note(db, self._HEX, xsec_token="")  # 无 token

        from backend.app.services.note_data_service import hydrate_offline_notes
        called = {"n": 0}
        def fake_fetch(url):
            called["n"] += 1
            return self._payload()
        stats = hydrate_offline_notes(
            account_id=10, user_id=1, category=CATEGORY_GOODS,
            session_factory=db_factory, fetch_payload=fake_fetch,
        )
        assert stats["no_url"] == 1
        assert called["n"] == 0, "无 token 不发请求"
        note = db.scalar(select(Note).where(Note.note_id == self._HEX))
        assert note.hydrated_at is None, "无 token 不写标记（下次同步补到 token 还能重试）"
        db.close()

    def test_failure_does_not_mark_hydrated(self, db_factory, monkeypatch):
        db = db_factory()
        self._seed(db)
        self._make_note(db, self._HEX)

        from backend.app.services.note_data_service import hydrate_offline_notes
        def fake_fetch(url):
            return (False, "explore 页面返回 HTTP 404", None)
        stats = hydrate_offline_notes(
            account_id=10, user_id=1, category=CATEGORY_GOODS,
            session_factory=db_factory, fetch_payload=fake_fetch,
        )
        assert stats["failed"] == 1
        note = db.scalar(select(Note).where(Note.note_id == self._HEX))
        assert note.hydrated_at is None, "失败不写标记，下次重试"
        db.close()
