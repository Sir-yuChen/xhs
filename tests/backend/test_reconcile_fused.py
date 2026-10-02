"""对账融合进同步链路（设计 13 §27）：标记「三方不存在」+ 自愈，匹配键 = 唯一 ID。

用户口径：「数据对账功能融合到同步笔记和同步商品中，每次根据时间区间和全量同步时，
对本次指定得区间进行数据对账保证数据得一致性，如果数据不一致，则将三方不存在得数据
再本地数据行将数据得同步状态标记为三方不存在数据状态」＋「使用数据得唯一ID进行数据对账」。

本文件守的纪律：

1. **标记与自愈**：同步范围内「本地有、平台无」⇒ `platform_missing_at` 非空；
   平台又有了 ⇒ 清回 `NULL`；
2. **匹配键 = 唯一 ID**：笔记按 `note_id`、商品按 `item_id`；标题变了但 ID 在
   ⇒ **不标**；ID 不同即使标题完全相同也标；
3. **范围 = 同步范围**：区间外的行一个字节都不碰；取不到发布时间的行不判定；
4. **枚举不完整不对账**（DEF-057）：限流超时 / 页数兜底 / 显式 `max_pages` ⇒
   零标记 + 任务行明说「对账未执行」；
5. **源类型判据**：只有「能枚举全量笔记的源」（creator / pc）完整翻完才标；
   ark 源完整也不标（它只看得到商品笔记，会误标普通笔记）；
6. **独立端点已拆除**：OpenAPI 里不再有 reconcile 路径（§27.1）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now
from backend.app.main import app
from backend.app.models import Note, PlatformAccount, User
from backend.app.models.shop_product import ShopProduct
from backend.app.services import note_sync_service as nss
from backend.app.services import reconcile_service as rs
from backend.app.services import sync_task_service as sts
from tests.backend import test_note_sync as nsync
from tests.backend import test_shop_goods_sync as sgtest


def _session(tmp_path, name: str):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _seed(db) -> tuple[User, PlatformAccount]:
    user = User(username="owner", password_hash="x")
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


def _add_note(db, account, note_id: str, *, title: str = "", raw: dict | None = None,
              platform_missing: bool = False) -> Note:
    note = Note(
        platform_account_id=account.id,
        user_id=account.user_id,
        note_id=note_id,
        platform="xhs",
        title=title or note_id,
        # 默认带一个可解析的发布时间（creator 源真实形状）：没有它，
        # `_local_note_time_ms` 判「时间未知」⇒ 不纳入对账（不造假）。
        raw_json=raw if raw is not None else {"time": "2026-08-13 09:30"},
        platform_missing_at=shanghai_now() if platform_missing else None,
    )
    db.add(note)
    db.commit()
    return note


# --------------------------------------------------------------------------- #
# 1) 标记通道（纯本地）：标记 / 自愈 / 范围 / 唯一 ID 匹配
# --------------------------------------------------------------------------- #
def test_marks_local_only_rows_and_heals_common(tmp_path):
    """范围内「本地有、平台无」⇒ 标记；平台有的 ⇒ 清标记（自愈）。"""
    SessionLocal = _session(tmp_path, "mark.db")
    db = SessionLocal()
    try:
        _, account = _seed(db)
        _add_note(db, account, "N-ON-PLATFORM")
        _add_note(db, account, "N-GONE", platform_missing=False)
        _add_note(db, account, "N-HEAL", platform_missing=True)
        # 平台本轮只返回前两条
        outcome = rs.mark_notes_missing(db, account, ["N-ON-PLATFORM", "N-HEAL"])

        assert outcome == {"marked": 1, "healed": 1, "unknown_time": 0}
        assert db.get(Note, db.scalars(select(Note.id).where(Note.note_id == "N-ON-PLATFORM")).one()).platform_missing_at is None
        assert db.scalars(select(Note).where(Note.note_id == "N-GONE")).one().platform_missing_at is not None
        assert db.scalars(select(Note).where(Note.note_id == "N-HEAL")).one().platform_missing_at is None
    finally:
        db.close()


def test_match_is_by_unique_id_not_title(tmp_path):
    """**唯一 ID 匹配**（用户追加口径）：标题变了但 ID 在 ⇒ 不标；ID 不同即使标题完全相同也标。"""
    SessionLocal = _session(tmp_path, "unique-id.db")
    db = SessionLocal()
    try:
        _, account = _seed(db)
        # 同 ID、标题被平台改过 ⇒ 仍在平台集合里 ⇒ 不标
        _add_note(db, account, "N-SAME-ID", title="改过的标题")
        # 不同 ID、标题与平台条目完全相同 ⇒ 不在集合里 ⇒ 标
        _add_note(db, account, "N-OTHER-ID", title="平台上的标题")

        outcome = rs.mark_notes_missing(
            db, account, ["N-SAME-ID", "N-PLATFORM-ONE"]
        )

        assert outcome["marked"] == 1
        assert db.scalars(select(Note).where(Note.note_id == "N-SAME-ID")).one().platform_missing_at is None
        assert db.scalars(select(Note).where(Note.note_id == "N-OTHER-ID")).one().platform_missing_at is not None
    finally:
        db.close()


def test_scope_excludes_out_of_range_and_unknown_time(tmp_path):
    """范围 = 同步范围：区间外的行不碰；取不到发布时间的行不判定（计入 unknown_time）。"""
    SessionLocal = _session(tmp_path, "scope.db")
    db = SessionLocal()
    try:
        _, account = _seed(db)
        # 2026-08-13 09:30（ creator 源真实形状的字符串）
        in_range = _add_note(db, account, "N-IN", raw={"time": "2026-08-13 09:30"})
        too_old = _add_note(db, account, "N-OLD", raw={"time": "2026-08-01 09:30"})
        too_new = _add_note(db, account, "N-NEW", raw={"time": "2026-08-20 09:30"})
        unknown = _add_note(db, account, "N-UNKNOWN", raw={})
        # 平台只有区间内那一条（区间 2026-08-10 ~ 2026-08-15，上海时区）
        from_ms = int(datetime(2026, 8, 10, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
        to_ms = int(datetime(2026, 8, 15, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)

        outcome = rs.mark_notes_missing(
            db, account, ["N-IN"], time_from_ms=from_ms, time_to_ms=to_ms
        )

        assert outcome["marked"] == 0 and outcome["unknown_time"] == 1
        assert in_range.platform_missing_at is None
        assert too_old.platform_missing_at is None, "区间外的行被标记了"
        assert too_new.platform_missing_at is None, "区间外的行被标记了"
        assert unknown.platform_missing_at is None, "时间未知的行被标记了（不造假）"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 2) 端到端（笔记同步）：标记 / 自愈 / 不完整不标 / ark 不标
# --------------------------------------------------------------------------- #
@pytest.fixture
def creator_env(tmp_path, monkeypatch):
    nsync.CURRENT[0] = nsync.CREATOR_COOKIES
    nsync.ENV["channel"] = "creator"
    headers, account_id, user_id, SessionLocal = nsync._setup(tmp_path, monkeypatch)
    yield headers, account_id, user_id, SessionLocal
    nsync._teardown()


def test_full_sync_marks_missing_and_heals(creator_env):
    """全量同步翻完 ⇒ 平台没有的本地行打标记；平台有的行自愈。"""
    _, account_id, user_id, SessionLocal = creator_env
    nsync.FakeCreatorAdapter.note_ids = ["C-N1", "C-N2"]
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        # 本地有三条：两条平台有（其中一条带着旧标记）、一条平台没有
        _add_note(db, account, "C-N1", raw={"time": "2026-08-13 09:30"}, platform_missing=True)
        _add_note(db, account, "C-N2", raw={"time": "2026-08-13 09:30"})
        _add_note(db, account, "C-GONE", raw={"time": "2026-08-13 09:30"})
    finally:
        db.close()

    result = nss.run_note_sync(account_id, user_id=user_id, max_pages=1)

    assert result["status"] == "done", result
    assert "对账：标记三方不存在 1 条，自愈 1 条" in result["note"], result["note"]
    db = SessionLocal()
    try:
        assert db.scalars(select(Note).where(Note.note_id == "C-GONE")).one().platform_missing_at is not None
        assert db.scalars(select(Note).where(Note.note_id == "C-N1")).one().platform_missing_at is None
        assert db.scalars(select(Note).where(Note.note_id == "C-N2")).one().platform_missing_at is None
    finally:
        db.close()


def test_rate_limited_sync_does_not_mark(creator_env, monkeypatch):
    """限流超时 = 没翻完 ⇒ **零标记**，任务行明说「对账未执行」（DEF-057）。"""
    _, account_id, user_id, SessionLocal = creator_env
    monkeypatch.setattr(nss, "get_rate_limiter", lambda: nsync.DenyLimiter())
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        _add_note(db, account, "C-LOCAL-ONLY", raw={"time": "2026-08-13 09:30"})
    finally:
        db.close()

    result = nss.run_note_sync(account_id, user_id=user_id, max_pages=1)

    assert "对账未执行" in result["note"], result["note"]
    db = SessionLocal()
    try:
        assert db.scalars(select(Note).where(Note.note_id == "C-LOCAL-ONLY")).one().platform_missing_at is None
    finally:
        db.close()


def test_ark_only_completion_does_not_reconcile(creator_env):
    """**商品笔记任务**（`only="ark"`）跑完**不做对账**：ark 只看得到商品笔记的子集，
    凭它去做账号级对账会把普通笔记全标错。

    ⚠️ 本用例原名 `test_ark_only_completion_does_not_mark`（2026-09-30 前）：那时
    creator 与 ark 同处一个**混合任务**，所以要专门构造「creator 失败、ark 完整」来验证
    **不拿 ark 的枚举去做账号级对账**。拆成两个任务（设计 13 **§44**）后该规则由
    「ark 链路整条不写账号级副作用」直接保证，本用例改为直接钉住 ark 链路不对账。
    """
    _, account_id, user_id, SessionLocal = creator_env
    nsync.FakeQianfanAdapter.list_entries = [nsync._ark_note("ARK-N1")]
    db = SessionLocal()
    try:
        account = db.get(PlatformAccount, account_id)
        _add_note(db, account, "PLAIN-NOTE", raw={"time": "2026-08-13 09:30"})
    finally:
        db.close()

    result = nss.run_note_sync(
        account_id, user_id=user_id, max_pages=1, only=nss.SOURCE_ARK
    )

    assert result["status"] == "done", result
    assert "不做账号级正文补齐与对账" in result["note"], result["note"]
    db = SessionLocal()
    try:
        # 普通笔记不在 ark 的商品笔记集合里，但**不能**因此被标「三方不存在」。
        assert db.scalars(select(Note).where(Note.note_id == "PLAIN-NOTE")).one().platform_missing_at is None
    finally:
        db.close()


def test_incremental_sync_only_marks_newer_than_cursor(creator_env):
    """增量同步的对账范围 = 游标以后：旧行不碰，新的本地行才标。"""
    _, account_id, user_id, SessionLocal = creator_env
    # 预置游标（2026-08-13 09:30 上海）⇒ auto 模式落成 incremental
    cursor_ms = int(datetime(2026, 8, 13, 9, 30, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
    db = SessionLocal()
    try:
        sts.save_note_cursor(db, account_id, cursor_ms, "C-N1")
        account = db.get(PlatformAccount, account_id)
        _add_note(db, account, "C-OLD", raw={"time": "2026-08-01 09:30"})
        _add_note(db, account, "C-NEW-GONE", raw={"time": "2026-08-20 09:30"})
    finally:
        db.close()

    result = nss.run_note_sync(account_id, user_id=user_id, max_pages=1)

    db = SessionLocal()
    try:
        # 游标以前的旧行：不在本轮范围 ⇒ 不标
        assert db.scalars(select(Note).where(Note.note_id == "C-OLD")).one().platform_missing_at is None
        # 游标以后、平台没有的本地行：标
        assert db.scalars(select(Note).where(Note.note_id == "C-NEW-GONE")).one().platform_missing_at is not None
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 3) 端到端（商品同步）：标记 + 不完整不标
# --------------------------------------------------------------------------- #
def test_goods_sync_marks_missing_by_item_id(tmp_path, monkeypatch):
    """商品同步同构：范围内「本地有、平台无」按 **item_id** 标记。

    刻意用**时间区间**同步（非全量无界）：范围外的行不删、范围内平台没有的
    打标记（§27.5：删除只发生在全量无界同步；区间 / 增量用标记）。
    """
    _headers, account_id, user_id, SessionLocal = sgtest._setup(tmp_path, monkeypatch)
    try:
        sgtest.FakeItemsAdapter.pages = [([sgtest._item("I1")], 1, False)]
        db = SessionLocal()
        try:
            #: ADR-021：台账归属键 = 账号（无店铺实体可查）。
            db.add(
                ShopProduct(
                    user_id=user_id,
                    platform_account_id=account_id,
                    item_id="I-OLD",
                    item_title="旧商品",
                    raw_json='{"create_time": 1786584162000}',
                )
            )
            db.commit()
        finally:
            db.close()

        day_ms = 24 * 3600 * 1000
        result = sgtest.sgs.run_shop_goods_sync(
            account_id,
            user_id=user_id,
            max_pages=3,
            adapter_factory=sgtest.FakeItemsAdapter,
            time_from_ms=1786584162000 - day_ms,
            time_to_ms=1786584162000 + day_ms,
        )

        assert result["status"] == "done", result
        assert result["removed"] == 0, "区间同步删了范围外的行（§27.5 不许）"
        assert "对账：标记三方不存在 1 件" in result["note"], result["note"]
        db = SessionLocal()
        try:
            assert db.scalars(select(ShopProduct).where(ShopProduct.item_id == "I-OLD")).one().platform_missing_at is not None
            assert db.scalars(select(ShopProduct).where(ShopProduct.item_id == "I1")).one().platform_missing_at is None
        finally:
            db.close()
    finally:
        sgtest._teardown()


def test_goods_sync_rate_limited_does_not_mark(tmp_path, monkeypatch):
    """商品同步被限流 ⇒ 零标记 + 任务行明说。"""
    _headers, account_id, user_id, SessionLocal = sgtest._setup(tmp_path, monkeypatch)
    try:
        sgtest.FakeItemsAdapter.pages = [([sgtest._item("I1")], 5, True)]
        sgtest.LIMITER["impl"] = sgtest.DenyLimiter()
        db = SessionLocal()
        try:
            db.add(
                ShopProduct(
                    user_id=user_id, platform_account_id=account_id, item_id="I-OLD", item_title="旧商品",
                    raw_json='{"create_time": 1786584162000}',
                )
            )
            db.commit()
        finally:
            db.close()

        result = sgtest.sgs.run_shop_goods_sync(
            account_id, user_id=user_id, max_pages=3, adapter_factory=sgtest.FakeItemsAdapter
        )

        assert "对账未执行" in result["note"], result["note"]
        db = SessionLocal()
        try:
            assert db.scalars(select(ShopProduct).where(ShopProduct.item_id == "I-OLD")).one().platform_missing_at is None
        finally:
            db.close()
    finally:
        sgtest.LIMITER["impl"] = sgtest.AllowLimiter()
        sgtest._teardown()


# --------------------------------------------------------------------------- #
# 4) 独立端点已拆除（§27.1）
# --------------------------------------------------------------------------- #
def test_reconcile_endpoints_are_gone():
    """OpenAPI 里不再有独立对账端点（融合进同步后拆除，原句保留在设计文档）。"""
    paths = app.openapi()["paths"]
    assert not any("reconcile" in path for path in paths), [
        path for path in paths if "reconcile" in path
    ]
    # 同步端点还在（对账融合的对象）
    assert any(path.endswith("/notes/sync") for path in paths)
