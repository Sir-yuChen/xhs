"""店铺违规管理 · 删除关联内容（设计 44，2026-10-18）

## 这个文件守什么

三条**顺序 / 语义**判据，每一条错了都不会报错、只会给用户错的结论：

1. **平台先行**：必须**先**调平台删除，**后**动本地；
2. **平台失败 ⇒ 本地不动**（否则两边对不上）；
3. **本地没有 ⇒ 跳过，不算失败**（本地是缓存，不是平台镜像）。

外加边界：不可删类型、归属校验、条数上限、去重、从属行清理。
"""
from __future__ import annotations

import inspect

import pytest
from sqlalchemy import func, select

from backend.app.core.database import get_db  # noqa: F401  (夹具依赖)
from backend.app.models import Note, NoteGoods, PlatformAccount, ShopProduct, User
from backend.app.services import violation_delete_service as vds


# --------------------------------------------------------------------------- #
# 纯判据
# --------------------------------------------------------------------------- #
class TestJudgements:
    def test_only_item_and_note_are_deletable(self):
        assert vds.is_deletable(1) is True   # 商品
        assert vds.is_deletable(3) is True   # 笔记
        for code in (0, 2, 4, None):
            assert vds.is_deletable(code) is False

    def test_entity_labels_are_human_readable(self):
        assert vds.entity_type_label(0) == "店铺"
        assert vds.entity_type_label(1) == "商品"
        assert vds.entity_type_label(2) == "账号"
        assert vds.entity_type_label(3) == "笔记"
        assert vds.entity_type_label(4) == "直播间"


# --------------------------------------------------------------------------- #
# 顺序：平台先行 —— 用 AST 钉住「平台调用在本地删除之前」
# --------------------------------------------------------------------------- #
def test_platform_delete_is_called_before_local_purge():
    """⚠️ 本需求的核心判据：**先平台，后本地**。

    用源码顺序断言（比跑一遍更直接）：`_one_target` 里
    `_delete_platform_*` 必须出现在 `purge_local_*` 之前。
    """
    # ⚠️ 2026-10-19 起加了前置检查：平台**查不到**时不调平台删除、直接看本地
    # （用户口径），所以「平台无」分支里的 `purge_local_*` 天然排在平台删除之前。
    # 这里只看「平台有」那段（锚点 `# ③ 平台有`）。
    src = inspect.getsource(vds._one_target)
    seg = src[src.index("# ③ 平台有"):]
    platform_at = min(
        seg.index("_delete_platform_item") if "_delete_platform_item" in seg else 10**9,
        seg.index("_delete_platform_note") if "_delete_platform_note" in seg else 10**9,
    )
    local_at = min(
        seg.index("purge_local_item") if "purge_local_item" in seg else 10**9,
        seg.index("purge_local_note") if "purge_local_note" in seg else 10**9,
    )
    assert platform_at < 10**9 and local_at < 10**9, "两个阶段都必须在 _one_target 里被调用"
    assert platform_at < local_at, "必须先调平台删除，再动本地（用户口径 2026-10-18）"


def test_platform_failure_does_not_touch_local():
    """平台失败 ⇒ 本地不动。用源码判据：失败分支必须在本地删除**之前** return。"""
    src = inspect.getsource(vds._one_target)
    seg = src[src.index("# ③ 平台有"):]
    fail_return = seg.index("平台删除失败")
    local_at = seg.index("purge_local_item")
    assert fail_return < local_at, "平台失败的 return 必须在本地删除之前"


# --------------------------------------------------------------------------- #
# 行为：用替身跑 execute_delete（不打真平台）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def seeded(db_factory):
    """造一个用户 + 账号 + 1 个商品 + 1 篇笔记（都归属该账号）。"""
    db = db_factory()
    try:
        user = User(username="del-test", password_hash="x")
        db.add(user)
        db.flush()
        account = PlatformAccount(
            user_id=user.id, platform="xhs", nickname="测试店", sub_type="creator"
        )
        db.add(account)
        db.flush()

        item = ShopProduct(
            user_id=user.id, platform_account_id=account.id, item_id="ITEM-1",
            item_title="测试商品", raw_json="{}",
        )
        note = Note(
            user_id=user.id, platform_account_id=account.id, platform="xhs",
            note_id="NOTE-1", title="测试笔记",
        )
        db.add_all([item, note])
        db.commit()
        return {"user_id": user.id, "account_id": account.id}
    finally:
        db.close()


class TestExecuteDelete:
    def test_local_present_deletes_both_sides(self, db_factory, seeded, monkeypatch):
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(vds, "_delete_platform_item",
                            lambda d, a, i: (calls.append(("item", i)), (True, ""))[1])
        monkeypatch.setattr(vds, "_delete_platform_note",
                            lambda d, u, a, n: (calls.append(("note", n)), (True, ""))[1])

        db = db_factory()
        try:
            user = db.get(User, seeded["user_id"])
            out = vds.execute_delete(db, user, seeded["account_id"], [
                {"entity_type": 1, "entity_id": "ITEM-1"},
                {"entity_type": 3, "entity_id": "NOTE-1"},
            ])
            db.commit()
            assert out["deleted"] == 2
            assert out["platform_only"] == 0
            assert out["failed"] == 0
            # 平台被调用了（先），本地被删了（后）
            assert calls == [("item", "ITEM-1"), ("note", "NOTE-1")]
            assert db.scalar(select(func.count()).select_from(ShopProduct)) == 0
            assert db.scalar(select(func.count()).select_from(Note)) == 0
        finally:
            db.close()

    def test_local_absent_is_skipped_not_failed(self, db_factory, seeded, monkeypatch):
        """⚠️ 本地没有 ⇒ `platform_only`（成功），**不是** `failed`。"""
        monkeypatch.setattr(vds, "_delete_platform_item", lambda d, a, i: (True, ""))
        monkeypatch.setattr(vds, "_delete_platform_note", lambda d, u, a, n: (True, ""))

        db = db_factory()
        try:
            user = db.get(User, seeded["user_id"])
            out = vds.execute_delete(db, user, seeded["account_id"], [
                {"entity_type": 1, "entity_id": "NOT-EXIST"},
                {"entity_type": 3, "entity_id": "NOT-EXIST-NOTE"},
            ])
            assert out["platform_only"] == 2
            assert out["deleted"] == 0
            assert out["failed"] == 0
            assert all(r["ok"] for r in out["results"])
            assert all("跳过" in r["reason"] for r in out["results"])
        finally:
            db.close()

    def test_platform_failure_leaves_local_intact(self, db_factory, seeded, monkeypatch):
        monkeypatch.setattr(vds, "_delete_platform_item", lambda d, a, i: (False, "平台 403"))

        db = db_factory()
        try:
            user = db.get(User, seeded["user_id"])
            out = vds.execute_delete(db, user, seeded["account_id"], [
                {"entity_type": 1, "entity_id": "ITEM-1"},
            ])
            db.commit()
            assert out["failed"] == 1
            assert out["deleted"] == 0
            # 本地商品**还在**（平台没删掉，本地不能先删）
            assert db.scalar(select(func.count()).select_from(ShopProduct)) == 1
            assert "403" in out["results"][0]["reason"]
        finally:
            db.close()

    def test_non_deletable_types_are_skipped_with_reason(self, db_factory, seeded, monkeypatch):
        called = []
        monkeypatch.setattr(vds, "_delete_platform_item",
                            lambda d, a, i: (called.append(i), (True, ""))[1])

        db = db_factory()
        try:
            user = db.get(User, seeded["user_id"])
            out = vds.execute_delete(db, user, seeded["account_id"], [
                {"entity_type": 0, "entity_id": "SHOP"},
                {"entity_type": 2, "entity_id": "ACCOUNT"},
                {"entity_type": 4, "entity_id": "LIVE"},
            ])
            assert out["skipped"] == 3
            assert called == [], "不可删类型不该打平台"
            assert all(r["status"] == "skipped" for r in out["results"])
        finally:
            db.close()

    def test_duplicate_targets_are_deduped(self, db_factory, seeded, monkeypatch):
        calls = []
        monkeypatch.setattr(vds, "_delete_platform_item",
                            lambda d, a, i: (calls.append(i), (True, ""))[1])

        db = db_factory()
        try:
            user = db.get(User, seeded["user_id"])
            out = vds.execute_delete(db, user, seeded["account_id"], [
                {"entity_type": 1, "entity_id": "ITEM-1"},
                {"entity_type": 1, "entity_id": "ITEM-1"},  # 同一条传两次
            ])
            db.commit()
            assert out["deleted"] == 1
            assert calls == ["ITEM-1"], "去重后只该调一次平台"
        finally:
            db.close()

    def test_invalid_targets_are_reported(self, db_factory, seeded):
        db = db_factory()
        try:
            user = db.get(User, seeded["user_id"])
            out = vds.execute_delete(db, user, seeded["account_id"], [
                {"entity_type": None, "entity_id": "X"},
                {"entity_type": 1, "entity_id": ""},
            ])
            assert out["failed"] == 2
            assert all(r["status"] == "failed" for r in out["results"])
        finally:
            db.close()


# --------------------------------------------------------------------------- #
# 本地删除链：从属行必须清（否则撞外键 —— 实测过真 bug）
# --------------------------------------------------------------------------- #
class TestLocalPurge:
    def test_purge_local_item_clears_dependent_rows(self, db_factory, seeded, monkeypatch):
        """⚠️ 只清 `note_goods` 会撞 FK（4 张表引用 shop_products.id）。

        这里断言「删得掉且不留外键异常」—— 从属行为空时也应正常。
        """
        monkeypatch.setattr(vds, "_delete_platform_item", lambda d, a, i: (True, ""))

        db = db_factory()
        try:
            user = db.get(User, seeded["user_id"])
            account = db.get(PlatformAccount, seeded["account_id"])
            removed = vds.purge_local_item(db, user, account, "ITEM-1")
            db.commit()
            assert removed is True
            assert db.scalar(select(func.count()).select_from(ShopProduct)) == 0
            assert db.scalar(select(func.count()).select_from(NoteGoods)) == 0
        finally:
            db.close()

    def test_purge_local_item_returns_false_when_absent(self, db_factory, seeded):
        db = db_factory()
        try:
            user = db.get(User, seeded["user_id"])
            account = db.get(PlatformAccount, seeded["account_id"])
            assert vds.purge_local_item(db, user, account, "NOT-EXIST") is False
        finally:
            db.close()


# --------------------------------------------------------------------------- #
# 全项目口径：「先平台，后本地；本地没有就跳过」也适用于【我的笔记】批量删除
# --------------------------------------------------------------------------- #
class TestNotesBatchDeleteFollowsSameRule:
    """`POST /notes/batch-delete` 必须遵守同一条口径（设计 44）。

    旧实现有两个病，都是「本地没数据就根本不打平台」：
    1. 分组阶段 `if note is None: failed + continue` ⇒ 平台有这条笔记也永远删不掉；
    2. creator 分支 `if note is None: continue` ⇒ 同上。

    这里用源码判据钉住（跑真平台不可行，但「有没有那个门」是可读的）。
    """

    def test_local_missing_does_not_skip_platform(self):
        src = inspect.getsource(
            __import__("backend.app.api.notes", fromlist=["x"]).batch_delete_notes_endpoint
        )
        # 只看「删平台」那一段：`delete_platform=False`（只删本地）里出现
        # 「本地无此笔记」是对的 —— 那时确实只操作本地，如实报告。
        platform_part = src[src.index("# 删本地 + 平台"):]
        assert "本地无此笔记" not in platform_part, "删平台路径上，本地没有不该直接判失败"
        # creator 分支不许在本地缺失时 continue 掉（那样根本不打平台）
        assert "if note is None:\n            continue" not in platform_part
        # 平台删成功 + 本地没有 ⇒ platform_only（跳过，不是失败）；两个分支各一处
        assert platform_part.count("platform_only") >= 2, "两个分支都要给出 platform_only 回执"

    def test_account_can_be_passed_explicitly(self):
        """本地一批都没记录时必须能指定账号，否则拿不到平台令牌。"""
        from backend.app.api.notes import BatchDeleteNotesRequest

        req = BatchDeleteNotesRequest(note_ids=["X"], delete_platform=True, platform_account_id=8)
        assert req.platform_account_id == 8
