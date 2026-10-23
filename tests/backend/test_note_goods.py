"""笔记 ↔ 商品关系表（设计 13 **§26.7**）：`note_goods` 多对多 + 同步时落库。

用户口径（2026-09-27，本轮）：

> 「同步笔记时，如果笔记挂有商品**必须**将笔记与商品的关系落库到笔记与商品关系表，
> **无需校验商品是否存在**，删除笔记时需删除笔记与商品之间的关系」
> ＋「**关系表改存平台商品 ID 和平台笔记 ID**」。

本文件守的纪律（与 §26.7.7 的测试要点表一一对应）：

1. **M2M 两个方向都成立**：一篇笔记挂多个商品 ⇒ 多行；一个商品被多篇笔记挂 ⇒ 多行；
2. **幂等**：同一关系重复解析不报错、不产生重复行（唯一键 `(user_id, note_id, item_id)`）；
3. **不校验商品存在**（本轮的核心变化）：本地 `shop_products` 里**没有**这件商品 ⇒
   关系行**照样落**（旧口径「任一侧缺失 ⇒ 跳过」已废）；唯一跳过条件是条目里
   **取不到 `item_id`**；
4. **只增不删**：二次解析时挂载变少 ⇒ 旧关系行仍在（§23：同步链路不做删除维度）；
5. **两条调用点都覆盖**（§44 后分属两个任务）：creator 源（证据缓存路径，**普通笔记任务**）
   与 ark 源（条目自带 `goods_infos`，**商品笔记任务**）；
6. **零平台请求**：解析过程只读缓存 / 调用方给的条目，不打平台；
7. **删除链**：笔记被删 ⇒ 指向它的关系行一并删（2026-09-23 用户口径）；
8. **读取退化**：台账缺行时 `GET /notes` 的 `goods` 只给 `item_id`（标题 / 价格留空）；
9. **发布改写笔记 ID 时关系跟着走**（本地生成笔记的 `local_…` ⇒ 平台真 ID）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import Base
from backend.app.models import Note, NoteGoods, PlatformAccount, ShopProduct, User
from backend.app.services import note_sync_service as nss
from backend.app.services import shop_goods_service as sgs
from tests.backend import test_note_sync as nsync
from tests.backend.cookie_fixtures import bind_shop_account



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


def _add_product(db, account, item_id: str, *, user_id: int) -> ShopProduct:
    """台账行按 **ADR-021 后的归属键** 落库：`platform_account_id`（不再是 `shop_id_fk`）。"""
    product = ShopProduct(
        user_id=user_id, platform_account_id=account.id, item_id=item_id,
        item_title=f"商品 {item_id}",
    )
    db.add(product)
    db.commit()
    return product


def _relations(db) -> list[tuple[str, str]]:
    """关系行 ⇒ (平台 note_id, 平台 item_id) 对（**直接读关系表**）。

    ⚠️ 2026-09-27（§26.7）：关系表两个字段都是平台 ID ⇒ 不再需要 join
    `notes` / `shop_products`（这也正是「台账缺行也有关系」的结构保证）。
    """
    rows = db.execute(select(NoteGoods.note_id, NoteGoods.item_id)).all()
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
    # 反查：ITEM-1 被几篇笔记挂（商品 → 笔记方向，按平台 ID 关联）
    notes_of_p1 = db.scalars(
        select(Note.note_id)
        .join(NoteGoods, NoteGoods.note_id == Note.note_id)
        .where(NoteGoods.item_id == p1.item_id)
    ).all()
    assert sorted(notes_of_p1) == ["NOTE-A", "NOTE-B"]
    # 正查：NOTE-A 挂了几个商品（笔记 → 商品方向）
    products_of_a = db.scalars(
        select(NoteGoods.item_id).where(NoteGoods.note_id == note_a.note_id)
    ).all()
    assert sorted(products_of_a) == ["ITEM-1", "ITEM-2"]
    assert p2.id != p1.id
    assert note_b.note_id == "NOTE-B"


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
# 3) 不校验商品存在（本轮核心口径）
# --------------------------------------------------------------------------- #
def test_links_even_when_product_missing(env):
    """商品台账里**没有**这件商品 ⇒ 关系行**照样落**（用户口径「无需校验商品是否存在」）。

    旧口径（~~§26.2-3「任一侧缺失 ⇒ 跳过」~~）的实际后果：真实库 1933 个挂载条目里
    **16 个**因台账缺行永远落不了库，内容库那条笔记的「挂载商品」区空着。
    """
    db, _user, account, _shop = env
    _add_note(db, account, "NOTE-A")

    result = sgs.link_note_goods(db, account, [("NOTE-A", [{"item_id": "ITEM-MISSING"}])])

    assert result == {"linked": 1, "skipped": 0}, "商品不在台账里就跳过 = 用户要推翻的那条"
    assert _relations(db) == [("NOTE-A", "ITEM-MISSING")]
    assert db.scalar(select(func.count()).select_from(ShopProduct)) == 0, "不得为落关系而造商品行"


def test_links_without_note_row_and_without_shop(env):
    """连本地笔记行都没有 / 账号没绑店 ⇒ 关系也照样落（两个字段都是平台 ID）。"""
    db, _user, account, _shop = env
    # 不建 note 行、也不解析店铺（旧实现会在 `account_shop_id` 这一步直接返回 skipped）
    result = sgs.link_note_goods(db, account, [("NOTE-NOT-LOCAL", [{"item_id": "ITEM-X"}])])

    assert result == {"linked": 1, "skipped": 0}
    assert _relations(db) == [("NOTE-NOT-LOCAL", "ITEM-X")]


def test_skips_only_when_item_id_missing(env):
    """唯一跳过条件：挂载条目里**取不到 `item_id`**（无键可落，不造假）。"""
    db, _user, account, _shop = env
    _add_note(db, account, "NOTE-A")

    result = sgs.link_note_goods(
        db,
        account,
        [
            ("NOTE-A", [{"id": "sku-level-id"}, {"item_title": "只有标题"}, {"item_id": ""}]),
            ("NOTE-A", [{"item_id": "ITEM-1"}]),
        ],
    )

    assert result == {"linked": 1, "skipped": 3}, "取不到 item_id 才跳过（`id` 是 SKU 级，不认）"
    assert _relations(db) == [("NOTE-A", "ITEM-1")]


def test_reader_degrades_to_item_id_when_product_missing(env):
    """读取端：台账缺行 ⇒ `goods` 只给 `item_id`，标题 / 价格留空（缺值口径）。"""
    from backend.app.api.notes import _note_goods

    db, user, account, shop = env
    note = _add_note(db, account, "NOTE-A")
    _add_product(db, shop, "ITEM-IN-LEDGER", user_id=user.id)
    sgs.link_note_goods(
        db,
        account,
        [("NOTE-A", [{"item_id": "ITEM-IN-LEDGER"}, {"item_id": "ITEM-MISSING"}])],
    )

    goods = {entry["item_id"]: entry for entry in _note_goods(db, note)}
    assert set(goods) == {"ITEM-IN-LEDGER", "ITEM-MISSING"}
    assert goods["ITEM-IN-LEDGER"]["item_title"] == "商品 ITEM-IN-LEDGER"
    # 台账缺行的那件：有 ID、没标题没价格（前端渲染「（无商品标题）」「价格待同步」）
    assert goods["ITEM-MISSING"]["item_title"] == ""
    assert goods["ITEM-MISSING"]["price"] == ""
    assert goods["ITEM-MISSING"]["images"] == []


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
# 5) 端到端：**两个任务各建自己那个源**的关系（2026-09-30 §44）
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

    # 预置本地商品（**只**为验证「台账里有的商品能带出标题」，不再是落关系的前提）
    db = SessionLocal()
    try:
        accounts = db.scalars(select(PlatformAccount).order_by(PlatformAccount.id)).all()
        account = accounts[0]
        for item_id in ("ITEM-C-N1", "ITEM-C-N2", "ITEM-ARK-N1"):
            db.add(
                ShopProduct(
                    user_id=user_id, platform_account_id=account.id,
                    item_id=item_id, item_title=item_id,
                )
            )
        db.commit()
    finally:
        db.close()
    yield headers, account_id, user_id, SessionLocal
    nsync._teardown()


def test_normal_note_sync_links_only_creator_goods(creator_env):
    """**普通笔记任务**（`only="creator"`）⇒ 只落 creator 源的挂载商品关系。

    ⚠️ 本用例原名 `test_note_sync_links_goods_from_both_sources`（2026-09-30 前）：
    那时一次同步会把两个源的关系都落库。拆成两个任务（设计 13 **§44**）后，每条链路
    **只跑自己那个源** ⇒ ark 的挂载关系归「同步商品笔记」任务（见下一条用例）。
    """
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
        # ark 源**不参与**本次任务 ⇒ 它带来的关系一行都不落
        assert not [row for row in relations if row[0].startswith("ARK-")]
    finally:
        db.close()


def test_goods_note_sync_links_only_ark_goods(creator_env):
    """**商品笔记任务**（`only="ark"`）⇒ 只落 ark 条目 inline 给出的挂载商品关系（§44）。"""
    _, account_id, user_id, SessionLocal = creator_env
    nsync.FakeQianfanAdapter.list_entries = [nsync._ark_note("ARK-N1")]

    result = nss.run_note_sync(
        account_id, user_id=user_id, max_pages=1, only=nss.SOURCE_ARK
    )

    assert result["status"] == "done", result
    db = SessionLocal()
    try:
        relations = _relations(db)
        # ark 源：ARK-N1 的挂载商品随条目 inline 给出
        assert ("ARK-N1", "ITEM-ARK-N1") in relations
        # creator 源**不参与**本次任务 ⇒ 它带来的关系一行都不落
        assert not [row for row in relations if row[0].startswith("C-")]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 7) 删除链：笔记被删 ⇒ 指向它的关系行一并删（2026-09-23 用户口径）
# --------------------------------------------------------------------------- #
def test_purge_note_deletes_its_goods_relations(env):
    """删笔记 ⇒ 该笔记的 `note_goods` 关系行一并删；**别的笔记的关系不受影响**。

    用户口径：「删除笔记时需要删除笔记与商品的关联关系」（2026-09-27 再次强调）。

    真实库曾留 **4 条孤儿行**（note_id 1963/1964/1965/1967 → product_id 1）——
    删掉那几条本地生成笔记时 `purge_note` 没清关系表。

    ⚠️ 2026-09-27（§26.7）：关系表改按**平台笔记 ID + 归属**存 ⇒ 删除判据是
    `(user_id, note.note_id)`（不再是本地主键）。行为与用户口径不变。
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


def test_purge_note_does_not_touch_topic_dictionary(env):
    """用户口径「**内容库页点击删除笔记时，话题只删关系表**」：字典行必须留着。

    与商品关系同一条删除链：`note_topics` 只删**关系行**，`topics` 是用户资产
    （别的笔记还在用、话题筛选下拉还要列它）—— 删一篇笔记不该让话题消失。
    """
    from backend.app.api.notes import purge_note
    from backend.app.models import NoteTopic, Topic
    from backend.app.services import note_topic_service as nts

    db, _user, account, _shop = env
    note = _add_note(db, account, "NOTE-A")
    nts.apply_sync_topics(db, note, ["露营", "徒步"])
    db.commit()
    assert db.scalar(select(func.count()).select_from(NoteTopic)) == 2

    purge_note(db, note)
    db.commit()

    assert db.scalar(select(func.count()).select_from(NoteTopic)) == 0, "关系行要删"
    assert sorted(db.scalars(select(Topic.name)).all()) == ["徒步", "露营"], "字典行不能删"


def test_purge_note_with_no_relations_is_a_noop_for_goods(env):
    """没有关系行的笔记删起来也不报错（`delete ... where note_id = x` 命中 0 行）。"""
    from backend.app.api.notes import purge_note

    db, user, account, shop = env
    note = _add_note(db, account, "NOTE-A")

    purge_note(db, note)
    db.commit()

    assert _relations(db) == []
    assert db.get(Note, note.id) is None


# --------------------------------------------------------------------------- #
# 8) 发布改写笔记 ID：关系行跟着走（本地生成笔记 `local_…` ⇒ 平台真 ID）
# --------------------------------------------------------------------------- #
def test_publish_rewrite_moves_relations_to_platform_id(env):
    """发布成功把 `notes.note_id` 改成平台真 ID ⇒ 关系行的笔记键**跟着改**。

    不迁的实际后果：笔记一发出去，挂链商品就查不到了（关系行还在，但指向一个
    不再存在的笔记 ID）。发布路径 `_execute_publish` 在改写那一行调用本函数。
    """
    from backend.app.services.note_publish_service import _move_note_goods_relations

    db, _user, account, _shop = env
    note = _add_note(db, account, "local_abc123")
    sgs.link_note_goods(db, account, [("local_abc123", [{"item_id": "ITEM-1"}])])
    assert _relations(db) == [("local_abc123", "ITEM-1")]

    note.note_id = "6ab2c3d40000000000000001"
    moved = _move_note_goods_relations(
        db, note, old_note_id="local_abc123", new_note_id=note.note_id
    )
    db.commit()

    assert moved == 1
    assert _relations(db) == [("6ab2c3d40000000000000001", "ITEM-1")]


def test_publish_rewrite_dedupes_when_new_key_already_linked(env):
    """新键上**已有**同一关系（同步链路按真 ID 落过）⇒ 旧行删掉、不撞唯一键。"""
    from backend.app.services.note_publish_service import _move_note_goods_relations

    db, _user, account, _shop = env
    note = _add_note(db, account, "local_abc123")
    sgs.link_note_goods(
        db,
        account,
        [
            ("local_abc123", [{"item_id": "ITEM-1"}]),
            ("6ab2c3d40000000000000001", [{"item_id": "ITEM-1"}]),
        ],
    )
    assert len(_relations(db)) == 2

    note.note_id = "6ab2c3d40000000000000001"
    moved = _move_note_goods_relations(
        db, note, old_note_id="local_abc123", new_note_id=note.note_id
    )
    db.commit()

    assert moved == 0, "新键上已有同一关系 ⇒ 不新建"
    assert _relations(db) == [("6ab2c3d40000000000000001", "ITEM-1")]
