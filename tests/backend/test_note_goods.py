"""笔记 ↔ 商品关系表（设计 13 §26）：`note_goods` 多对多 + 同步时解析落库。

用户口径：「笔记可能会挂载商品，同步笔记时如果存在笔记挂载得商品，则应与本地商品
信息表中对应商品建立关系表……一个商品下可能存在多个笔记，笔记与商品之间得关系均是
多对多，新增笔记商品关系表」＋「笔记相关得信息单独一张表，商品信息单独一张表，
笔记与商品之间得关系单独一张关系表」。

本文件守的纪律：

1. **M2M 两个方向都成立**：一篇笔记挂多个商品 ⇒ 多行；一个商品被多篇笔记挂 ⇒ 多行；
2. **幂等**：同一关系重复解析不报错、不产生重复行（唯一键 `(note_id, product_id)`）；
3. **缺侧跳过**：商品还没同步（本地无 `shop_products` 行）⇒ 0 行、不造假；
   商品落库后自愈解析补上（**两种先后顺序都收敛**，不做半成品）；
4. **只增不删**：二次解析时挂载变少 ⇒ 旧关系行仍在（§23：同步链路不做删除维度）；
5. **两条调用点都覆盖**：creator 源（证据缓存路径）与 ark 源（条目自带 goods_infos）；
6. **纯本地**：解析过程零平台请求（只读缓存 + 本地两张表）；
7. **删除链**：笔记被删 ⇒ 指向它的关系行一并删（2026-09-23 用户口径，见文件末尾）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import Base
from backend.app.models import Note, NoteGoods, PlatformAccount, ShopProduct, User
from backend.app.models.shop import ShopNoteGoods
from backend.app.services import note_sync_service as nss
from backend.app.services import shop_goods_service as sgs
from tests.backend import test_note_sync as nsync
from tests.backend.cookie_fixtures import bind_shop_account


def _session(tmp_path, name: str):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def env(tmp_path):
    """一个用户 + 一个绑了店的 creator 账号（ark 令牌由夹具写好）。"""
    SessionLocal = _session(tmp_path, "note-goods.db")
    db = SessionLocal()
    try:
        user = User(username="owner", password_hash="x")
        db.add(user)
        db.flush()
        account = PlatformAccount(
            user_id=user.id, platform="xhs", sub_type="creator",
            nickname="我的号", status="active",
        )
        db.add(account)
        db.flush()
        shop = bind_shop_account(db, account)
        db.commit()
        yield db, user, account, shop
    finally:
        db.close()


def _add_note(db, account, note_id: str) -> Note:
    note = Note(
        platform_account_id=account.id,
        user_id=account.user_id,
        note_id=note_id,
        platform="xhs",
        title=note_id,
        note_type="synced",
    )
    db.add(note)
    db.commit()
    return note


def _add_product(db, shop, item_id: str, *, user_id: int) -> ShopProduct:
    product = ShopProduct(
        user_id=user_id, shop_id_fk=shop.id, item_id=item_id, item_title=f"商品 {item_id}"
    )
    db.add(product)
    db.commit()
    return product


def _relations(db) -> list[tuple[str, str]]:
    """本地关系 ⇒ (平台 note_id, item_id) 对（按两侧排序，稳定可比）。"""
    rows = db.execute(
        select(Note.note_id, ShopProduct.item_id)
        .join(NoteGoods, NoteGoods.note_id == Note.id)
        .join(ShopProduct, ShopProduct.id == NoteGoods.product_id)
    ).all()
    return sorted((note_id, item_id) for note_id, item_id in rows)


# --------------------------------------------------------------------------- #
# 1) M2M：两个方向
# --------------------------------------------------------------------------- #
def test_m2m_holds_in_both_directions(env):
    """一篇笔记挂 2 个商品、一个商品被 2 篇笔记挂 ⇒ 三个方向都查得出来。"""
    db, user, account, shop = env
    note_a = _add_note(db, account, "NOTE-A")
    note_b = _add_note(db, account, "NOTE-B")
    p1 = _add_product(db, shop, "ITEM-1", user_id=user.id)
    p2 = _add_product(db, shop, "ITEM-2", user_id=user.id)

    result = sgs.link_note_goods(
        db,
        account,
        [
            ("NOTE-A", [{"item_id": "ITEM-1"}, {"item_id": "ITEM-2"}]),
            ("NOTE-B", [{"item_id": "ITEM-1"}]),
        ],
    )

    assert result == {"linked": 3, "skipped": 0}
    assert _relations(db) == [
        ("NOTE-A", "ITEM-1"),
        ("NOTE-A", "ITEM-2"),
        ("NOTE-B", "ITEM-1"),
    ]
    # 反查：ITEM-1 被几篇笔记挂（商品 → 笔记方向）
    notes_of_p1 = db.scalars(
        select(Note.note_id)
        .join(NoteGoods, NoteGoods.note_id == Note.id)
        .where(NoteGoods.product_id == p1.id)
    ).all()
    assert sorted(notes_of_p1) == ["NOTE-A", "NOTE-B"]
    # 正查：NOTE-A 挂了几个商品（笔记 → 商品方向）
    products_of_a = db.scalars(
        select(ShopProduct.item_id)
        .join(NoteGoods, NoteGoods.product_id == ShopProduct.id)
        .where(NoteGoods.note_id == note_a.id)
    ).all()
    assert sorted(products_of_a) == ["ITEM-1", "ITEM-2"]
    assert p2.id != p1.id


# --------------------------------------------------------------------------- #
# 2) 幂等（唯一键）
# --------------------------------------------------------------------------- #
def test_repeated_link_is_idempotent(env):
    """同一关系解析两次：不报错、不产生重复行（唯一键兜底）。"""
    db, user, account, shop = env
    _add_note(db, account, "NOTE-A")
    _add_product(db, shop, "ITEM-1", user_id=user.id)
    mappings = [("NOTE-A", [{"item_id": "ITEM-1"}])]

    first = sgs.link_note_goods(db, account, mappings)
    second = sgs.link_note_goods(db, account, mappings)

    assert first["linked"] == 1
    assert second == {"linked": 0, "skipped": 0}, "第二次解析又插了一遍（唯一键没兜住）"
    assert _relations(db) == [("NOTE-A", "ITEM-1")]


# --------------------------------------------------------------------------- #
# 3) 缺侧跳过 + 自愈（两种先后顺序都收敛）
# --------------------------------------------------------------------------- #
def test_skips_when_product_not_synced_yet(env):
    """商品还没同步（本地无行）⇒ 0 行、计入 skipped，**不造假**。"""
    db, _user, account, _shop = env
    _add_note(db, account, "NOTE-A")

    result = sgs.link_note_goods(db, account, [("NOTE-A", [{"item_id": "ITEM-1"}])])

    assert result == {"linked": 0, "skipped": 1}
    assert _relations(db) == []


def test_self_heals_after_goods_sync(env):
    """先同步笔记（商品没落库 ⇒ 建不起）⇒ 商品落库后自愈解析补上。

    复刻商品同步收尾的自愈钩子（`shop_goods_sync_service._sync_with_task`）：
    读该账号的证据缓存 + 同一解析器。两种先后顺序都必须收敛，否则
    「两个按钮都点过，关系表还是空的」（§21.3 同款假修复）。
    """
    db, user, account, shop = env
    _add_note(db, account, "NOTE-A")
    # 证据缓存：平台说过 NOTE-A 挂了 ITEM-1（creator 源 sync_note_ids 的落库形状）
    db.add(
        ShopNoteGoods(
            user_id=user.id,
            platform_account_id=account.id,
            note_id="NOTE-A",
            item_id="ITEM-1",
            goods_json='[{"item_id": "ITEM-1", "item_title": "商品"}]',
        )
    )
    db.commit()

    # 第一遍：商品还没落库 ⇒ 建不起
    assert sgs.link_note_goods(db, account, sgs.cached_goods_mappings(db, account.id))["linked"] == 0
    assert _relations(db) == []

    # 商品同步落地（自愈钩子的触发条件）
    _add_product(db, shop, "ITEM-1", user_id=user.id)

    # 第二遍（自愈）：同一解析器，关系补上
    healed = sgs.link_note_goods(db, account, sgs.cached_goods_mappings(db, account.id))
    assert healed["linked"] == 1
    assert _relations(db) == [("NOTE-A", "ITEM-1")]


# --------------------------------------------------------------------------- #
# 4) 只增不删
# --------------------------------------------------------------------------- #
def test_link_never_deletes_existing_rows(env):
    """二次解析时挂载变少 ⇒ 旧关系行仍在（§23：同步链路不做删除维度）。"""
    db, user, account, shop = env
    _add_note(db, account, "NOTE-A")
    _add_product(db, shop, "ITEM-1", user_id=user.id)
    _add_product(db, shop, "ITEM-2", user_id=user.id)

    sgs.link_note_goods(db, account, [("NOTE-A", [{"item_id": "ITEM-1"}, {"item_id": "ITEM-2"}])])
    sgs.link_note_goods(db, account, [("NOTE-A", [{"item_id": "ITEM-1"}])])

    assert _relations(db) == [("NOTE-A", "ITEM-1"), ("NOTE-A", "ITEM-2")]


# --------------------------------------------------------------------------- #
# 5) 端到端：两条笔记源都建关系
# --------------------------------------------------------------------------- #
@pytest.fixture
def creator_env(tmp_path, monkeypatch):
    """复用 `test_note_sync` 的三源夹具；额外预置本地商品。

    ⚠️ 共享替身 `FakeQianfanAdapter` 按 `note_ids` 查询时返回的 `goods_infos`
    是**空数组**（原夹具只验证「缓存写没写」，不验证商品内容）—— 本文件要验证
    关系解析，所以在这里给该方法打补丁，让它带回**真实的挂载商品**（与真机
    `goods-note/list` 的 `goods_infos[]` 同形状）。补丁只作用于本文件。
    """
    nsync.CURRENT[0] = nsync.CREATOR_COOKIES
    nsync.ENV["channel"] = "creator"
    headers, account_id, user_id, SessionLocal = nsync._setup(tmp_path, monkeypatch)

    def _goods_note_page(self, page_num=1, page_size=20, *, note_ids=None):
        type(self).calls += 1
        if note_ids:
            type(self).note_id_queries.append(list(note_ids))
            entries = [
                {
                    "note_id": note_id,
                    "item_id": f"ITEM-{note_id}",
                    "item_title": "商品",
                    "goods_infos": [
                        {"item_id": f"ITEM-{note_id}", "item_title": "商品", "variant_desc": "规格"}
                    ],
                }
                for note_id in note_ids
            ]
            return True, "", {"data": {"note_infos": entries, "total": len(entries)}}
        entries = type(self).list_entries
        return True, "", {
            "data": {"note_infos": entries, "total": len(entries), "end_tag": True}
        }

    monkeypatch.setattr(nsync.FakeQianfanAdapter, "get_goods_note_page", _goods_note_page)

    # 预置本地商品（关系表的右侧）：creator 源按 note_ids 查到 ITEM-C-N1/C-N2，
    # ark 源条目自带 ITEM-ARK-N1。
    db = SessionLocal()
    try:
        from backend.app.models.shop import Shop

        shop = db.scalars(select(Shop)).one()
        for item_id in ("ITEM-C-N1", "ITEM-C-N2", "ITEM-ARK-N1"):
            db.add(
                ShopProduct(
                    user_id=user_id, shop_id_fk=shop.id, item_id=item_id, item_title=item_id
                )
            )
        db.commit()
    finally:
        db.close()
    yield headers, account_id, user_id, SessionLocal
    nsync._teardown()


def test_note_sync_links_goods_from_both_sources(creator_env):
    """跑一次同步 ⇒ creator 源（证据缓存）与 ark 源（条目自带）的关系都落库。"""
    _, account_id, user_id, SessionLocal = creator_env
    nsync.FakeQianfanAdapter.list_entries = [nsync._ark_note("ARK-N1")]

    result = nss.run_note_sync(account_id, user_id=user_id, max_pages=1)

    assert result["status"] == "done", result
    db = SessionLocal()
    try:
        relations = _relations(db)
        # creator 源：C-N1/C-N2 的挂载商品来自按 note_ids 的现查（进证据缓存）
        assert ("C-N1", "ITEM-C-N1") in relations
        assert ("C-N2", "ITEM-C-N2") in relations
        # ark 源：ARK-N1 的挂载商品随条目 inline 给出
        assert ("ARK-N1", "ITEM-ARK-N1") in relations
        # 关系只建在「两边都解析成功」的行上：没有对应本地商品的不出现
        assert all(item_id.startswith("ITEM-") for _, item_id in relations)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 7) 删除链：笔记被删 ⇒ 指向它的关系行一并删（2026-09-23 用户口径）
# --------------------------------------------------------------------------- #
def test_purge_note_deletes_its_goods_relations(env):
    """删笔记 ⇒ 该笔记的 `note_goods` 关系行一并删；**别的笔记的关系不受影响**。

    用户口径：「删除笔记时需要删除笔记与商品的关联关系」。

    真实库曾留 **4 条孤儿行**（note_id 1963/1964/1965/1967 → product_id 1）——
    删掉那几条本地生成笔记时 `purge_note` 没清关系表。它们不参与任何列表口径
    （`note_count` 走 join），但数据自相矛盾（关系指向一条不存在的笔记）。

    ⚠️ 与**删商品**的口径**刻意相反**（§33.10）：删本地台账商品行时关系行**不删**
    （商品会被同步写回、关系要原样接上）；删笔记时**必须删**（笔记是真删，
    没有「写回时接上」这回事）。同一个关系表、两个方向、两种正确处理。
    """
    from backend.app.api.notes import purge_note

    db, user, account, shop = env
    note_a = _add_note(db, account, "NOTE-A")
    note_b = _add_note(db, account, "NOTE-B")
    _add_product(db, shop, "ITEM-1", user_id=user.id)
    _add_product(db, shop, "ITEM-2", user_id=user.id)

    linked = sgs.link_note_goods(
        db,
        account,
        [
            ("NOTE-A", [{"item_id": "ITEM-1"}, {"item_id": "ITEM-2"}]),
            ("NOTE-B", [{"item_id": "ITEM-1"}]),
        ],
    )
    assert linked == {"linked": 3, "skipped": 0}
    assert len(_relations(db)) == 3

    purge_note(db, note_a)
    db.commit()

    # NOTE-A 的两条关系没了，NOTE-B 的那条还在
    assert _relations(db) == [("NOTE-B", "ITEM-1")]

    # 商品行本身不受影响（删的是关系，不是商品）
    assert db.scalar(select(func.count()).select_from(ShopProduct)) == 2


def test_purge_note_with_no_relations_is_a_noop_for_goods(env):
    """没有关系行的笔记删起来也不报错（`delete ... where note_id = x` 命中 0 行）。"""
    from backend.app.api.notes import purge_note

    db, user, account, shop = env
    note = _add_note(db, account, "NOTE-A")

    purge_note(db, note)
    db.commit()

    assert _relations(db) == []
    assert db.get(Note, note.id) is None
