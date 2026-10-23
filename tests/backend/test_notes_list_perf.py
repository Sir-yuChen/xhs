"""内容库列表查询：SQL 分页下推 + 批量预取 + 稳定排序（设计 13 §41）。

盯的是**取数方式**，不是渲染 —— 改前真库 3620 篇实测 5774 ms / 16390 条 SQL
（只返回 20 条），改造后 15.5 ms / 7 条。本文件把那次改造的**四条不变量**钉住：

1. **分页下推**：本页 SQL 必须带 `LIMIT`，且 `ORDER BY` 含稳定键 `notes.id DESC`
   —— 守「不许退回全量加载 + 内存切片」；
2. **页间不重不漏**：逐页翻完的并集 == 全集、无重复（守 `LIMIT/OFFSET` 正确性）；
3. **同 `created_at` 也稳定**：种子全部同一秒时翻页仍不重不漏
   —— 守「翻页不重不漏」这条回归网（⚠️ 它**证明不了**稳定排序键承重，见该用例 docstring）；
4. **不退回 N+1**：SQL 条数**不随笔记数增长**（23 篇与 3 篇同条数）
   —— 守批量预取不被改回「逐篇各查 4 次」；
5. **批量预取与单篇同口径**：列表某篇的 `topics` / `goods` / `account_name` /
   `asset_urls` 与 `GET /notes/{id}` **逐字段相同** —— 守「一个语义一份实现」（DEF-044）。

⚠️ 本文件**不**另抄 engine 三件套（`create_engine` + `create_all` + `dependency_overrides`）：
用 `tests/backend/conftest.py` 的 `api_client` / `db_factory` / `db_engine`（2026-09-24 硬规矩，
由 `test_testing_spec_guards.py` 强制）。
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest
from sqlalchemy import event, select

from backend.app.models import (
    Note,
    NoteAsset,
    NoteGoods,
    NoteTopic,
    PlatformAccount,
    ShopProduct,
    Topic,
    User,
)


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration
#: 内容库列表端点（本文件全部断言都打它）。
NOTES_PATH = "/api/notes"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture
def seeded(api_client, db_factory):
    """一个用户（**经 API 注册**，令牌属于它）+ 一个账号。

    ⚠️ 用户必须走 `/api/auth/register` 建：直接插库再单独注册会得到**两个用户**，
    API 令牌指向的那个不是笔记的归属者 ⇒ 全部用例 404（`test_note_topics.py` 同款教训）。
    """
    response = api_client.post(
        "/api/auth/register", json={"username": "perf-owner", "password": "secret123"}
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    db = db_factory()
    try:
        user = db.scalars(select(User).where(User.username == "perf-owner")).one()
        account = PlatformAccount(
            user_id=user.id, platform="xhs", sub_type="pc", nickname="列表性能号", status="active"
        )
        db.add(account)
        db.commit()
        yield db, user, account, headers
    finally:
        db.close()


def _add_notes(db, account, count: int, *, prefix: str = "N", created_at=None) -> list[Note]:
    """批量落 `count` 篇笔记（**同一秒**：`created_at` 不传时全部用同一个时间）。

    ⚠️ 「同一秒」是**故意的** —— 真实库批量落库 / 同一轮同步就是这个形态，
    而它正是「单键排序会翻页漏行」的触发条件（设计 13 §41.5）。
    """
    stamp = created_at or datetime(2026, 9, 28, 12, 0, 0)
    notes = [
        Note(
            user_id=account.user_id,
            platform_account_id=account.id,
            platform="xhs",
            note_id=f"{prefix}-{i:03d}",
            title=f"标题 {prefix}-{i:03d}",
            content=f"正文 {i}",
            note_type="synced",
            publish_status="published",
            created_at=stamp,
        )
        for i in range(count)
    ]
    db.add_all(notes)
    db.commit()
    return notes


def _page(api_client, headers, *, page: int, page_size: int) -> dict:
    response = api_client.get(
        NOTES_PATH,
        headers=headers,
        params={"platform": "xhs", "page": page, "page_size": page_size},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _walk_all_pages(api_client, headers, *, page_size: int = 2) -> tuple[list[int], int]:
    """逐页翻完，返回（按序拿到的 id 列表, total）。"""
    ids: list[int] = []
    page = 1
    while True:
        payload = _page(api_client, headers, page=page, page_size=page_size)
        batch = [item["id"] for item in payload["items"]]
        if not batch:
            return ids, payload["total"]
        ids.extend(batch)
        page += 1
        assert page < 500, "翻页未收敛 —— 分页逻辑坏了"


# --------------------------------------------------------------------------- #
# 1. 分页下推（SQL 必须带 LIMIT + 稳定排序键）
# --------------------------------------------------------------------------- #
def test_notes_page_query_has_limit_and_stable_order(api_client, seeded, db_engine):
    """本页查询必须是「带 LIMIT 的单页取数」，且排序含稳定键 `notes.id DESC`。

    ⚠️ 为什么断言 SQL 文本而不是「耗时」：耗时在 CI 上不稳定，**断言不了**；
    而「有没有 LIMIT」「排序键是什么」是确定性的结构事实 —— 改回全量加载
    （`.all()` 不带 limit）会立刻让 `LIMIT` 消失，这条就红。
    """
    db, _user, _account, headers = seeded
    _add_notes(db, _account, 5)

    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        seen.append(statement)

    event.listen(db_engine, "before_cursor_execute", _record)
    try:
        _page(api_client, headers, page=1, page_size=2)
    finally:
        event.remove(db_engine, "before_cursor_execute", _record)

    page_queries = [
        sql for sql in seen if re.match(r"\s*SELECT notes\.id\s*,", sql, re.IGNORECASE)
    ]
    assert len(page_queries) == 1, f"应当恰好一条本页查询，实际 {len(page_queries)} 条：{seen}"
    sql = " ".join(page_queries[0].split())
    assert " LIMIT " in sql.upper(), f"本页查询没有 LIMIT ⇒ 退回全量加载了：{sql}"
    assert re.search(
        r"ORDER BY notes\.created_at DESC, notes\.id DESC", sql, re.IGNORECASE
    ), f"排序缺少稳定键 notes.id DESC（§41.5）：{sql}"


# --------------------------------------------------------------------------- #
# 2. 页间不重不漏
# --------------------------------------------------------------------------- #
def test_notes_pages_are_disjoint_and_complete(api_client, seeded):
    """逐页翻完：无重复、无遗漏、并集 == `total`，且 `total` 是命中总数不是本页条数。"""
    db, _user, _account, headers = seeded
    _add_notes(db, _account, 7)

    ids, total = _walk_all_pages(api_client, headers, page_size=2)

    assert total == 7, "total 必须是命中总数（前端 hasMore 靠它推）"
    assert len(ids) == 7, f"翻页共取到 {len(ids)} 条，应为 7 条（有遗漏）"
    assert len(set(ids)) == 7, f"翻页出现重复行：{ids}"

    first = _page(api_client, headers, page=1, page_size=2)
    assert first["total"] == 7 and len(first["items"]) == 2, (
        "total 不能被误改成本页条数（那会让 hasMore 恒假、列表加载不出第二页）"
    )


def test_notes_pages_are_disjoint_with_identical_created_at(api_client, seeded):
    """**全部笔记同一 `created_at`** 时翻页仍不重不漏。

    ⚠️ **本用例证明不了「稳定排序键是承重的」** —— 变异实测：去掉 `Note.id.desc()`
    后本用例**仍然通过**。原因是 SQLite 对同值行实际按 rowid 序返回（临时 B 树排序
    在同值时是稳定的），行为差异在「单线程、无并发插入」的测试形态下**观察不到**。

    所以守 `Note.id.desc()` 的是 `test_notes_page_query_has_limit_and_stable_order`
    的 **SQL 文本断言**（去掉键 ⇒ `ORDER BY` 不匹配 ⇒ 红，已实测捕获）。
    本用例守的是另一条更粗的回归网：**翻页不重不漏**（`LIMIT/OFFSET` 用错、
    `total` 用成本页条数、排序整个删掉 —— 这些都会在这里红）。
    """
    db, _user, _account, headers = seeded
    # 全部落在**同一个时间戳**（`_add_notes` 默认行为）
    _add_notes(db, _account, 9, prefix="TIE")

    ids, total = _walk_all_pages(api_client, headers, page_size=2)

    assert total == 9
    assert len(ids) == 9, f"同 created_at 下出现遗漏：拿到 {len(ids)} 条，应为 9 条"
    assert len(set(ids)) == 9, f"同 created_at 下出现重复：{ids}"


# --------------------------------------------------------------------------- #
# 3. 不退回 N+1
# --------------------------------------------------------------------------- #
def test_notes_query_count_does_not_grow_with_note_count(api_client, seeded, db_engine):
    """SQL 条数**不随笔记数增长** —— 23 篇与 3 篇必须同条数。

    改前每篇各查 assets / topics / goods / account（N+1）⇒ 20 篇比 3 篇多 17×4 条。
    改后是「每张表一条 `IN (…)`」，与条数无关。

    ⚠️ 用「两次测量相等」而不是「等于某个魔数」：魔数会随无关改动（比如加一条
    `stats` 查询）假红，而**不随条数增长**才是这次要守的性质。
    """
    db, _user, _account, headers = seeded
    _add_notes(db, _account, 3, prefix="SMALL")

    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        seen.append(statement)

    def _measure() -> int:
        # 预热一次（首次有 SQL 编译 / 连接建立开销，不计入）
        _page(api_client, headers, page=1, page_size=20)
        seen.clear()
        payload = _page(api_client, headers, page=1, page_size=20)
        assert payload["items"], "本页应有内容，否则测不到序列化路径"
        return len(seen)

    event.listen(db_engine, "before_cursor_execute", _record)
    try:
        small = _measure()
        _add_notes(db, _account, 20, prefix="BIG")
        big = _measure()
    finally:
        event.remove(db_engine, "before_cursor_execute", _record)

    assert small == big, (
        f"SQL 条数随笔记数增长了（3 篇 {small} 条 → 23 篇 {big} 条）⇒ N+1 回归了；"
        "列表必须走 _load_note_relations 的批量预取（设计 13 §41.4）"
    )
    assert small <= 12, f"单页 SQL 条数 {small} 偏多，检查是否又逐篇查询了"


# --------------------------------------------------------------------------- #
# 4. 批量预取与单篇同口径（DEF-044：一个语义一份实现）
# --------------------------------------------------------------------------- #
def test_list_relations_match_single_note_detail(api_client, seeded):
    """列表里某篇的关联字段与 `GET /notes/{id}` **逐字段相同**。

    列表走「整页一次 `IN`」、详情走「单篇（长度为 1 的批量）」—— 两条路径必须同口径。
    守的是：批量预取漏表（比如忘了 goods）、分组键写错（用本地 id 去配平台 id）、
    顺序被「顺手」改掉（`sort_order` / `NoteTopic.id` / `NoteGoods.id` 都是契约）。
    """
    db, user, account, headers = seeded
    notes = _add_notes(db, account, 1, prefix="REL")
    note = notes[0]

    # 素材（顺序敏感：两张图 + 一个视频，sort_order 与插入序**故意相反**）
    db.add_all(
        [
            NoteAsset(note_id=note.id, asset_type="video", url="https://x/v.mp4", sort_order=0),
            NoteAsset(note_id=note.id, asset_type="image", url="https://x/2.jpg", sort_order=2),
            NoteAsset(note_id=note.id, asset_type="image", url="https://x/1.jpg", sort_order=1),
        ]
    )
    # 话题（两枚，`NoteTopic.id` 序 = 落库序）
    for name in ("话题甲", "话题乙"):
        topic = Topic(user_id=user.id, name=name)
        db.add(topic)
        db.flush()
        db.add(NoteTopic(note_id=note.id, topic_id=topic.id))
    # 挂链商品：一件在台账里、一件**不在**（缺值口径：只给 item_id，不造假）
    #: ADR-021：台账归属键 = `platform_account_id`（店铺实体已退场）。
    db.add(
        ShopProduct(
            user_id=user.id,
            platform_account_id=account.id,
            item_id="ITEM-IN-LEDGER",
            item_title="台账里的商品",
            price="1990",
            stock=3,
            sales=7,
            images_json='["p1.jpg","p2.jpg"]',
        )
    )
    db.add_all(
        [
            NoteGoods(user_id=user.id, note_id=note.note_id, item_id="ITEM-IN-LEDGER"),
            NoteGoods(user_id=user.id, note_id=note.note_id, item_id="ITEM-NOT-IN-LEDGER"),
        ]
    )
    db.commit()

    listed = _page(api_client, headers, page=1, page_size=20)["items"]
    assert len(listed) == 1, "夹具应只有一篇笔记"
    row = listed[0]

    detail_response = api_client.get(f"{NOTES_PATH}/{note.id}", headers=headers)
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()

    for field in ("topics", "goods", "account_name", "asset_urls", "cover_url", "video_url", "title"):
        assert row[field] == detail[field], (
            f"列表与详情的 `{field}` 不一致 ⇒ 批量预取与单篇路径口径漂移了\n"
            f"  列表={row[field]!r}\n  详情={detail[field]!r}"
        )

    # 逐条钉住顺序与缺值口径（上面的相等断言在「两条路径一起错」时也能过）
    assert [asset for asset in row["asset_urls"]] == [
        "https://x/v.mp4",
        "https://x/1.jpg",
        "https://x/2.jpg",
    ], "asset_urls 必须按 sort_order 升序（sort_order 1 在 2 前、视频 0 最前）"
    assert row["video_url"] == "https://x/v.mp4"
    assert [topic["name"] for topic in row["topics"]] == ["话题甲", "话题乙"]
    assert [entry["item_id"] for entry in row["goods"]] == [
        "ITEM-IN-LEDGER",
        "ITEM-NOT-IN-LEDGER",
    ], "goods 必须按 NoteGoods.id 升序（= 落库序）"
    ledger, missing = row["goods"]
    assert ledger["item_title"] == "台账里的商品"
    assert ledger["price"] == "1990"
    assert ledger["images"] == ["p1.jpg", "p2.jpg"]
    assert ledger["stock"] == 3 and ledger["sales"] == 7
    # 台账里没有的那件：只给 item_id，其余留空（**不造假**、不拿缓存顶替）
    #: `variant`（规格）2026-10-06 起进返回体（设计 12 §26.7）—— 它来自
    #: `shop_note_goods.variant_desc`，夹具未种该映射行 ⇒ 缺值留空（不是缺字段）。
    assert missing == {
        "item_id": "ITEM-NOT-IN-LEDGER",
        "item_title": "",
        "price": "",
        "images": [],
        "stock": None,
        "sales": None,
        "variant": "",
    }, f"台账缺行的商品必须按缺值口径返回：{missing!r}"


def test_list_serialization_does_not_reload_relations_per_note(api_client, seeded, db_engine):
    """整页只发**一条** topics / assets / goods 查询（不是每篇一条）。

    与上面「条数不随笔记数增长」互补：那条测增长性，这条测**形态**
    （一条 `IN (…)` 而不是 N 条 `= ?`）。
    """
    db, _user, _account, headers = seeded
    _add_notes(db, _account, 6, prefix="SHAPE")

    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        seen.append(" ".join(statement.split()))

    event.listen(db_engine, "before_cursor_execute", _record)
    try:
        _page(api_client, headers, page=1, page_size=20)
    finally:
        event.remove(db_engine, "before_cursor_execute", _record)

    for table in ("note_assets", "note_topics", "note_goods"):
        hits = [sql for sql in seen if f"FROM {table}" in sql]
        assert len(hits) == 1, f"`{table}` 应只查 1 次（整页一次 IN），实际 {len(hits)} 次"
        assert " IN (" in hits[0].upper(), f"`{table}` 查询不是 IN (…) 批量形态：{hits[0]}"
