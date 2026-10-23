"""笔记话题：正文拆分 + 话题表 + 关系表 + 标签表合并 + 发布回填（设计 13 §34）。

用户口径（2026-09-25 两条）：
① 「同步笔记和内容库页中的同步笔记按钮，执行同步笔记时，将笔记中的话题进行拆分出来，
写入到话题表中，并新增话题与笔记的关系，新增话题前先检查话题是否存在，存在则不新增
话题，只新增当前话题与笔记之间的关联关系，实现后笔记正文不再存在话题，话题与正文
进行拆分，发布笔记时注意查询该笔记下的话题进行添加」；
② 「将当前系统的笔记标签表合并成话题表，将内容库笔记详情弹窗中标题后展示标签tag和
添加标签tag合并到笔记话题功能中」。

本文件守的纪律：

1. **拆分**：同步后正文无 `#话题`（整词）；话题行进 `topics` + `note_topics`；
2. **去重**：同一话题两篇笔记 ⇒ `topics` 一行；二次同步不新建话题、不新建重复关系；
3. **兜底**：详情无 `tag_list` 但正文有 `#话题` ⇒ 仍拆出；`#1` 序号不当话题；
4. **只增不删**（同步）vs **整列替换**（手动 PUT）—— 两种写入语义不混；
5. **序列化**：`topics` 来自关系表 join（`[{id,name,color}]`）；**无 `tags` 字段**；
6. **发布**：`hash_tag` 带平台 ID；`desc` 末尾追加缺失的 `#名字`（不重复追加）；
7. **删除链**：删笔记 / 删话题词典项 ⇒ 关系行一并删；
8. **合并**：`tags` 行搬进 `topics`（name/color/user 原样）；标签与话题同名 ⇒ 一行一关系。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from backend.app.api.platforms.xhs import pc
from backend.app.models import Note, NoteTopic, PlatformAccount, Topic, User
from backend.app.services import note_sync_service as nss
from backend.app.services import note_topic_service as nts
from backend.app.services import note_publish_service as nps



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract

# --------------------------------------------------------------------------- #
# 夹具（公共件，见 conftest.py；本文件不另抄 engine 三件套）
# --------------------------------------------------------------------------- #


@pytest.fixture
def seeded(api_client, db_factory):
    """一个用户（**经 API 注册**，令牌属于它）+ 一个 creator 账号。

    ⚠️ 用户必须走 `/api/auth/register` 建：直接插库再单独注册会得到**两个用户**，
    API 令牌指向的那个不是笔记的归属者 ⇒ 所有 API 用例 404。
    """
    db = db_factory()
    try:
        response = api_client.post(
            "/api/auth/register", json={"username": "owner", "password": "secret123"}
        )
        assert response.status_code == 200, response.text
        headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        user = db.scalars(select(User).where(User.username == "owner")).one()
        account = PlatformAccount(
            user_id=user.id, platform="xhs", sub_type="creator",
            nickname="我的号", status="active",
        )
        db.add(account)
        db.commit()
        yield db, user, account, headers
    finally:
        db.close()


def _add_note(db, account, note_id: str, *, content: str = "", title: str = "") -> Note:
    note = Note(
        platform_account_id=account.id,
        user_id=account.user_id,
        note_id=note_id,
        platform="xhs",
        title=title or note_id,
        content=content,
        note_type="synced",
        # §19.13.9（2026-09-28）：详情请求**必须**带笔记级令牌 —— 无令牌现在会被拦成
        # `no_token`（无 token 的 explore 兜底已删除）⇒ 夹具按**真实形态**给令牌
        # （真实库里 71/71 篇都有）。
        raw_json={
            "note_url": f"https://www.xiaohongshu.com/explore/{note_id}",
            "xsec_token": "TK-TEST",
        },
    )
    db.add(note)
    db.commit()
    return note


def _topic_rows(db) -> list[Topic]:
    return list(db.scalars(select(Topic).order_by(Topic.id)).all())


def _relation_pairs(db) -> list[tuple[str, str]]:
    """(平台 note_id, 话题名) 对，按关系行 id 排序（= 落库顺序）。"""
    rows = db.execute(
        select(Note.note_id, Topic.name)
        .join(NoteTopic, NoteTopic.note_id == Note.id)
        .join(Topic, Topic.id == NoteTopic.topic_id)
        .order_by(NoteTopic.id)
    ).all()
    return [(str(note_id), str(name)) for note_id, name in rows]


# --------------------------------------------------------------------------- #
# 1) 解析规则（纯函数）
# --------------------------------------------------------------------------- #


def test_parse_topic_tokens_reads_hash_tokens_in_order():
    assert nts.parse_topic_tokens("今天去露营了 #露营 #周末去哪儿 真不错") == ["露营", "周末去哪儿"]


def test_parse_topic_tokens_dedupes_case_insensitively_keeping_first_form():
    assert nts.parse_topic_tokens("#Camping #camping #CAMPING") == ["Camping"]


def test_parse_topic_tokens_rejects_serial_numbers_and_short_tokens():
    """`#1` / `#5` 这类序号不是话题（§34.3：拆错比漏拆更坏）。"""
    assert nts.parse_topic_tokens("排名 #1 和 #5 的车") == []
    assert nts.parse_topic_tokens("一个字 #A 两个字 #Ab") == ["Ab"]


def test_parse_topic_tokens_trims_surrounding_punctuation():
    assert nts.parse_topic_tokens("推荐好物，（#露营）。") == ["露营"]


def test_parse_topic_tokens_strips_platform_marker():
    """平台形态 `#名字[话题]#` ⇒ 纯名字（2026-09-25 新增）。

    原实现只 `strip(_TRIM_CHARS)` ⇒ 解析出 `拼车[话题`（只掉了 `]`），库里因此落下
    118 行脏话题名。实证：从平台同步回来的正文原文就是 `#拼车[话题]#` 这个形态。
    """
    text = "周口回北京 #拼车[话题]# #顺风车[话题]#  #回家[话题]#"
    assert nts.parse_topic_tokens(text) == ["拼车", "顺风车", "回家"]


def test_split_topics_from_content_removes_platform_marker():
    """平台形态整块摘掉（标记 + 收尾 `#`），正文不留 `[话题]#` 残渣。"""
    text = "有座 #拼车[话题]# #顺风车[话题]# 到北京"
    cleaned, removed = nts.split_topics_from_content(text, nts.parse_topic_tokens(text))
    assert removed is True
    assert "[话题]" not in cleaned and "#" not in cleaned
    assert cleaned == "有座 到北京"


def test_strip_topic_marker_normalizes_dirty_names():
    """`strip_topic_marker` 是剥标记的**唯一实现**（解析 / 归一 / 落库共用）。"""
    assert nts.strip_topic_marker("拼车[话题") == "拼车"  # 存量脏行（未闭合）
    assert nts.strip_topic_marker("拼车[话题]") == "拼车"
    assert nts.strip_topic_marker("#拼车[话题]#") == "拼车"
    assert nts.strip_topic_marker("拼车") == "拼车"
    assert nts.topic_token("桌旗") == "#桌旗[话题]#"
    assert nts.topic_token("#桌旗[话题]#") == "#桌旗[话题]#"


def test_normalize_topic_names_strips_platform_marker():
    """手动增删传 `#名字[话题]#` / 脏名 ⇒ 库里只落纯名字（归一唯一入口）。"""
    assert nts.normalize_topic_names(["拼车[话题", "#顺风车[话题]#", "拼车", " 郑州 "]) == [
        "拼车",
        "顺风车",
        "郑州",
    ]


def test_parse_topic_tokens_accepts_single_char_only_with_platform_marker():
    """长度 1 的话题**只在带平台标记时**才认（标记 = 平台自己的认定）。

    `#鞋[话题]#` 是平台标的（认）；`#1` / `#2026` 无论有没有标记都不认
    （序号噪声不是话题，§34.3 诚实优先）。

    另一半盯**平台排版噪声**（`\\ufeff` / `\\xa0`）：它夹在话题标记前后，既不是
    空格也不是 `#` —— 不认它，「整词」边界断言会把摘除挡下（91 篇正文残留
    `[话题]#` 的真凶之一），摘完还会留下孤立的 `#`。
    """
    assert nts.parse_topic_tokens("#鞋[话题]# 大码鞋推荐") == ["鞋"]
    assert nts.parse_topic_tokens("#2026[话题]# 年份") == []
    assert nts.parse_topic_tokens("排名 #1 和 #5 的车") == []

    noisy = "文案。#九分裤推荐[话题]#\ufeff\ufeff\xa0\ufeff\ufeff#防晒穿搭[话题]#\ufeff\ufeff"
    cleaned, removed = nts.split_topics_from_content(noisy, nts.parse_topic_tokens(noisy))
    assert removed is True
    assert cleaned == "文案。"
    assert "\ufeff" not in cleaned and "\xa0" not in cleaned


def test_parse_topic_tokens_caps_at_twenty():
    text = " ".join(f"#话题{i}" for i in range(30))
    assert len(nts.parse_topic_tokens(text)) == 20


def test_split_topics_removes_token_and_one_separator_space():
    content = "今天去露营了 #露营 #周末去哪儿"
    cleaned, removed = nts.split_topics_from_content(content, ["露营", "周末去哪儿"])
    assert removed is True
    assert cleaned == "今天去露营了"


def test_split_topics_keeps_inline_text_with_single_space():
    cleaned, removed = nts.split_topics_from_content("正文 #露营 更多内容", ["露营"])
    assert (cleaned, removed) == ("正文 更多内容", True)


def test_split_topics_is_a_noop_when_nothing_matches():
    content = "这篇正文一个话题都没有"
    cleaned, removed = nts.split_topics_from_content(content, ["露营"])
    assert (cleaned, removed) == (content, False)


def test_split_topics_matches_whole_token_case_insensitively():
    """整词匹配：`#露营中` 不能被话题 `露营` 啃掉一半；拉丁名大小写不敏感。"""
    cleaned, removed = nts.split_topics_from_content("a #露营中 b #CAMPING", ["露营", "Camping"])
    assert (cleaned, removed) == ("a #露营中 b", True)


def test_split_topics_longest_name_first():
    cleaned, _ = nts.split_topics_from_content("x #露营装备 #露营 y", ["露营", "露营装备"])
    assert cleaned == "x y"


def test_normalize_topic_names_trims_dedupes_and_caps():
    out = nts.normalize_topic_names([" 露营 ", "", "露营", "CASE", "case", "x" * 60])
    assert out == ["露营", "CASE", "x" * 50]


# --------------------------------------------------------------------------- #
# 2) 同步链路：拆分 + 落表 + 补关系（两条路径共用 hydrate_note_detail）
# --------------------------------------------------------------------------- #


def test_hydrate_splits_topics_from_content(seeded, monkeypatch):
    """内容库「同步」按钮路径：详情正文带 `#话题` ⇒ 拆出、落表、补关系、正文干净。"""
    db, user, account, _ = seeded
    note = _add_note(db, account, "6a7d14b20000000028001cb8", content="旧正文")
    from tests.backend.cookie_fixtures import cookie_version

    db.add(cookie_version(account.id, "a1=x; web_session=own-ws; id_token=own-it", "www"))
    db.commit()

    class FakeAdapter:
        def __init__(self, cookies: str) -> None:
            self.cookies = cookies

        def get_note_info(self, url: str):
            payload = {
                "data": {
                    "items": [
                        {
                            "id": note.note_id,
                            "note_card": {
                                "note_id": note.note_id,
                                "desc": "周末去露营了，风景真好 #露营 #户外",
                                "tag_list": [{"name": "露营"}, {"name": "户外"}],
                                "image_list": [],
                            },
                        }
                    ]
                }
            }
            return True, "ok", payload

    monkeypatch.setattr(pc, "get_xhs_pc_api_adapter_factory", lambda: FakeAdapter)
    result = nss.sync_note_from_platform(db, note)

    assert result["status"] == "ok"
    db.refresh(note)
    # 正文不再有话题
    assert note.content == "周末去露营了，风景真好"
    assert "#" not in note.content
    # 话题表：两行（去重后）；关系：两条
    assert sorted(t.name for t in _topic_rows(db)) == ["户外", "露营"]
    assert _relation_pairs(db) == [(note.note_id, "露营"), (note.note_id, "户外")]


def test_hydrate_parses_content_when_detail_has_no_tag_list(seeded, monkeypatch):
    """详情没给 tag_list ⇒ 正文解析兜底（§34.3）。"""
    db, _, account, _ = seeded
    note = _add_note(db, account, "6a7d14b20000000028001cb9")
    from tests.backend.cookie_fixtures import cookie_version

    db.add(cookie_version(account.id, "a1=x; web_session=own-ws; id_token=own-it", "www"))
    db.commit()

    class FakeAdapter:
        def __init__(self, cookies: str) -> None:
            pass

        def get_note_info(self, url: str):
            payload = {
                "data": {
                    "items": [
                        {
                            "id": note.note_id,
                            "note_card": {
                                "note_id": note.note_id,
                                "desc": "通勤穿搭分享 #OOTD #穿搭",
                                "image_list": [],
                            },
                        }
                    ]
                }
            }
            return True, "ok", payload

    monkeypatch.setattr(pc, "get_xhs_pc_api_adapter_factory", lambda: FakeAdapter)
    result = nss.sync_note_from_platform(db, note)
    assert result["status"] == "ok"
    db.refresh(note)
    assert note.content == "通勤穿搭分享"
    assert sorted(t.name for t in _topic_rows(db)) == ["OOTD", "穿搭"]


def test_sync_is_idempotent_and_reuses_existing_topics(seeded):
    """同一话题两篇笔记 ⇒ topics 一行；重复解析不新建话题、不新建重复关系。"""
    db, _, account, _ = seeded
    note_a = _add_note(db, account, "N-A", content="a #露营 b")
    note_b = _add_note(db, account, "N-B", content="c #露营 d")
    nts.apply_sync_topics(db, note_a)
    nts.apply_sync_topics(db, note_b)
    nts.apply_sync_topics(db, note_a)  # 再来一次

    topics = _topic_rows(db)
    assert [t.name for t in topics] == ["露营"]  # 一行（不重复建）
    assert len(_relation_pairs(db)) == 2  # 两篇笔记各一条关系
    db.refresh(note_a)
    assert note_a.content == "a b"


def test_apply_sync_topics_keeps_relations_when_platform_drops_topic(seeded):
    """只增不删（§34.5）：平台撤了话题也不删关系行。"""
    db, _, account, _ = seeded
    note = _add_note(db, account, "N-KEEP", content="a #露营 b")
    nts.apply_sync_topics(db, note)
    note.content = "a b"  # 平台上话题被作者删了，下次同步正文不再带
    nts.apply_sync_topics(db, note)
    assert _relation_pairs(db) == [("N-KEEP", "露营")]


def test_save_normalized_notes_splits_list_item_topics(seeded, monkeypatch):
    """列表落库（PC 源 desc 自带 #话题）也走同一把尺子（§34.2）。"""
    from backend.app.api.platforms.xhs.crawl import _save_normalized_notes

    monkeypatch.setattr(
        "backend.app.services.asset_downloader.download_asset_to_local", lambda *a, **k: None
    )
    db, _, account, _ = seeded
    _save_normalized_notes(
        db,
        account,
        [
            {
                "note_id": "N-LIST",
                "title": "列表笔记",
                "content": "正文 #好物 #家居",
                "author_name": "作者",
                "image_urls": [],
            }
        ],
    )
    note = db.scalars(select(Note).where(Note.note_id == "N-LIST")).one()
    db.refresh(note)
    assert note.content == "正文"
    assert sorted(t.name for t in _topic_rows(db)) == ["好物", "家居"]


def test_generation_links_aigc_topics_with_platform_ids(seeded):
    """商品笔记生成：aigc 话题落表且带 platform_id（发布 hash_tag 要用）。"""
    db, _, account, _ = seeded
    note = _add_note(db, account, "local_gen_1", content="AI 正文 #露营装备")
    outcome = nts.apply_sync_topics(
        db,
        note,
        ["露营装备"],
        platform_ids={"露营装备": "platform-topic-1"},
    )
    assert outcome["topics"] == ["露营装备"]
    topic = _topic_rows(db)[0]
    assert topic.name == "露营装备"
    assert topic.platform_id == "platform-topic-1"
    db.refresh(note)
    assert note.content == "AI 正文"


# --------------------------------------------------------------------------- #
# 3) 手动增删：PUT /notes/{id}/topics（整列替换 + find-or-create）
# --------------------------------------------------------------------------- #


def test_put_topics_replaces_relations_and_creates_missing(api_client, seeded):
    """整列替换：关系行对齐；新名字 find-or-create 建典再关联（§34.4）。"""
    db, user, account, headers = seeded
    note = _add_note(db, account, "N-PUT")
    nts.apply_sync_topics(db, note, ["旧话题"])
    db.commit()

    response = api_client.put(
        f"/api/notes/{note.id}/topics",
        json={"topics": ["露营", "装备", "露营"]},  # 去重由后端做
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [t["name"] for t in body["topics"]] == ["露营", "装备"]
    assert all({"id", "name", "color"} <= set(t) for t in body["topics"])
    # 旧话题的关系没了；话题典例行仍在（字典不随单篇笔记删）
    assert _relation_pairs(db) == [("N-PUT", "露营"), ("N-PUT", "装备")]
    assert "旧话题" in {t.name for t in _topic_rows(db)}
    # 响应里没有 tags 字段（§34.4）
    assert "tags" not in body


def test_put_topics_empty_clears_relations(api_client, seeded):
    db, _, account, headers = seeded
    note = _add_note(db, account, "N-PUT-EMPTY")
    nts.apply_sync_topics(db, note, ["露营"])
    db.commit()
    response = api_client.put(
        f"/api/notes/{note.id}/topics", json={"topics": []}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["topics"] == []
    assert _relation_pairs(db) == []


# --------------------------------------------------------------------------- #
# 4) 序列化：关系表 join + 无 tags 字段
# --------------------------------------------------------------------------- #


def test_serialized_topics_come_from_relations(api_client, seeded):
    db, _, account, headers = seeded
    note = _add_note(db, account, "N-SER")
    nts.apply_sync_topics(db, note, ["话题一", "话题二"])
    db.commit()

    detail = api_client.get(f"/api/notes/{note.id}", headers=headers).json()
    assert [t["name"] for t in detail["topics"]] == ["话题一", "话题二"]
    assert "tags" not in detail

    listed = api_client.get("/api/notes", headers=headers, params={"platform": "xhs"}).json()
    row = next(item for item in listed["items"] if item["note_id"] == "N-SER")
    assert [t["name"] for t in row["topics"]] == ["话题一", "话题二"]


def test_topic_filter_uses_relation_table(api_client, seeded):
    """`topic_id` 筛选（原 tag_id）走 note_topics 关系表。"""
    db, _, account, headers = seeded
    note_a = _add_note(db, account, "N-FILTER-A")
    _add_note(db, account, "N-FILTER-B")
    nts.apply_sync_topics(db, note_a, ["露营"])
    db.commit()
    topic = db.scalars(select(Topic).where(Topic.name == "露营")).one()

    listed = api_client.get(
        "/api/notes",
        headers=headers,
        params={"platform": "xhs", "topic_id": topic.id},
    ).json()
    assert [item["note_id"] for item in listed["items"]] == ["N-FILTER-A"]


# --------------------------------------------------------------------------- #
# 5) 发布链路：查询该笔记话题并添加
# --------------------------------------------------------------------------- #


def test_desc_with_topics_appends_only_missing():
    """回填**平台形态** `#名字[话题]#`（2026-09-25 修订）。

    原口径（保留备查）：`assert nps._desc_with_topics("正文", ["露营"]) == "正文 #露营"`
    及「` #露营 #装备`」—— 平台正文认的是 `#名字[话题]#` 这个富文本形态，`#名字`
    被当普通文本 ⇒ 发布的笔记一个话题都没有（用户 2026-09-25 报障），故改。
    """
    assert nps._desc_with_topics("正文", ["露营"]) == "正文 #露营[话题]#"
    assert nps._desc_with_topics("正文 #露营", ["露营"]) == "正文 #露营"  # 已有则不动
    assert nps._desc_with_topics("正文 #露营[话题]#", ["露营"]) == "正文 #露营[话题]#"
    assert nps._desc_with_topics("正文", ["露营", "装备"]) == "正文 #露营[话题]# #装备[话题]#"
    assert nps._desc_with_topics("正文", []) == "正文"


def test_build_publish_body_queries_note_topics(seeded):
    """hash_tag 带平台 ID；desc 回填**全部**话题的平台形态（2026-09-25 修订）。

    原口径（保留备查）：`assert body["common"]["desc"] == "AI 正文 #装备"`
    —— 只回填「没进 hash_tag 的」且写成 `#名字`。两处都错：`hash_tag` 是结构化
    参数、`desc` 才是平台正文的展示 / 解析载体，**两者要同时给**；形态必须是
    `#名字[话题]#`（用户报障「发布成功的笔记均没有话题」）。
    """
    db, _, account, _ = seeded
    note = _add_note(db, account, "local_pub_1", content="AI 正文", title="标题")
    note.raw_json = {
        "aigc": {
            "task_id": "task-1",
            "topics": [{"id": "pid-1", "name": "露营"}],
            "sku_id": "sku-1",
            "sku_name": "商品",
        }
    }
    db.commit()
    # 话题表：露营（aigc 自带 ID）+ 装备（无 ID，只能走 desc 文本）
    nts.apply_sync_topics(db, note, ["露营", "装备"], platform_ids={"露营": "pid-1"})

    body = nps._build_publish_body(db, note, [])
    hash_tag = body["common"]["hash_tag"]
    assert hash_tag == [{"id": "pid-1", "name": "露营", "type": "topic"}]
    assert body["common"]["desc"] == "AI 正文 #露营[话题]# #装备[话题]#"


def test_build_publish_body_normalizes_legacy_dirty_topic_names(seeded):
    """存量脏话题名（`拼车[话题`）不许把标记带进发布体（2026-09-25 新增）。

    历史解析器把平台标记当名字的一部分，库里落下 118 行 `xxx[话题`。发布时若不剥，
    desc 会写出 `#拼车[话题][话题]#`、hash_tag 也会带脏 name。
    """
    from backend.app.models.note import NoteTopic, Topic

    db, _, account, _ = seeded
    note = _add_note(db, account, "local_pub_dirty", content="正文", title="标题")
    dirty = Topic(user_id=account.user_id, name="拼车[话题", color="#1677ff", platform_id="pid-x")
    db.add(dirty)
    db.flush()
    db.add(NoteTopic(note_id=note.id, topic_id=dirty.id))
    db.commit()

    body = nps._build_publish_body(db, note, [])
    assert body["common"]["desc"] == "正文 #拼车[话题]#"
    assert body["common"]["hash_tag"] == [{"id": "pid-x", "name": "拼车", "type": "topic"}]


def test_creator_adapter_reuses_the_platform_token_helper():
    """创作者端发布回填也走唯一实现（`topic_token`）—— 平台形态不留第二份拼接。

    这条链路**一直是** `#名字[话题]#`（正是商品笔记链路 2026-09-25 对齐的形态依据）。
    抽成唯一实现后，谁再手写 f-string 拼接都会红。
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2]
        / "backend/app/adapters/xhs/creator_api_adapter.py"
    ).read_text(encoding="utf-8")
    assert "topic_token(" in source
    assert "[话题]# " not in source, "创作者端把手写拼接写回来了（应走 topic_token）"


def test_build_publish_body_no_topics_keeps_desc_untouched(seeded):
    db, _, account, _ = seeded
    note = _add_note(db, account, "local_pub_2", content="AI 正文")
    body = nps._build_publish_body(db, note, [])
    assert body["common"]["desc"] == "AI 正文"
    assert body["common"]["hash_tag"] == []


# --------------------------------------------------------------------------- #
# 6) 删除链：删笔记 / 删话题词典项 ⇒ 关系行一并删
# --------------------------------------------------------------------------- #


def test_purge_note_removes_topic_relations(api_client, seeded):
    db, _, account, headers = seeded
    note = _add_note(db, account, "N-PURGE")
    nts.apply_sync_topics(db, note, ["露营"])
    db.commit()
    response = api_client.delete(f"/api/notes/{note.id}", headers=headers)
    assert response.status_code == 200, response.text
    assert _relation_pairs(db) == []
    # 话题典例行不受影响（字典是用户资产，不随单篇笔记删）
    assert [t.name for t in _topic_rows(db)] == ["露营"]


def test_delete_topic_dictionary_row_removes_relations(api_client, seeded):
    db, _, account, headers = seeded
    note = _add_note(db, account, "N-TOPIC-DEL")
    nts.apply_sync_topics(db, note, ["露营"])
    db.commit()
    topic = db.scalars(select(Topic).where(Topic.name == "露营")).one()
    response = api_client.delete(f"/api/topics/{topic.id}", headers=headers)
    assert response.status_code == 200, response.text
    assert _relation_pairs(db) == []
    assert _topic_rows(db) == []


# --------------------------------------------------------------------------- #
# 7) 话题字典 CRUD（原 /tags）
# --------------------------------------------------------------------------- #


def test_topics_crud_and_duplicate_guard(api_client, seeded):
    _, _, _, headers = seeded
    created = api_client.post("/api/topics", json={"name": "露营"}, headers=headers)
    assert created.status_code == 200, created.text
    topic_id = created.json()["id"]

    duplicate = api_client.post("/api/topics", json={"name": "露营"}, headers=headers)
    assert duplicate.status_code == 400

    listed = api_client.get("/api/topics", headers=headers).json()
    assert [t["name"] for t in listed["items"]] == ["露营"]

    patched = api_client.patch(
        f"/api/topics/{topic_id}", json={"color": "#ff0000"}, headers=headers
    )
    assert patched.json()["color"] == "#ff0000"

    deleted = api_client.delete(f"/api/topics/{topic_id}", headers=headers)
    assert deleted.status_code == 200


def test_topics_crud_is_user_scoped(api_client, seeded):
    db, user, account, headers = seeded
    note = _add_note(db, account, "N-SCOPE")
    nts.apply_sync_topics(db, note, ["露营"])
    db.commit()
    topic = db.scalars(select(Topic).where(Topic.name == "露营")).one()

    intruder = api_client.post(
        "/api/auth/register", json={"username": "topics-intruder", "password": "secret123"}
    )
    intruder_headers = {"Authorization": f"Bearer {intruder.json()['access_token']}"}
    # 别人的话题 ⇒ 404（不暴露「存在但无权」）
    assert api_client.delete(f"/api/topics/{topic.id}", headers=intruder_headers).status_code == 404
    assert api_client.patch(
        f"/api/topics/{topic.id}", json={"name": "偷改"}, headers=intruder_headers
    ).status_code == 404
    # 别人的笔记 ⇒ 404，且一个字节都没被改
    assert api_client.put(
        f"/api/notes/{note.id}/topics", json={"topics": ["偷改"]}, headers=intruder_headers
    ).status_code == 404
    assert _relation_pairs(db) == [("N-SCOPE", "露营")]
    assert headers  # owner 令牌（正向路径由其它用例覆盖）


def test_topics_keyword_fuzzy_search(api_client, seeded):
    """`GET /topics?keyword=` 远程模糊搜索（§34.4，2026-09-25 用户口径
    「搜索区域的话题下拉框支持输入模糊查询」）：SQL 层 LIKE 过滤再分页；
    空 / 全空白 = 全量；命中大小写不敏感（SQLite LIKE 对 ASCII）；**别人的
    话题不被捞到**（user_id 归属不变）。"""
    db, user, account, headers = seeded
    for name in ("露营灯", "露营椅", "户外桌", "CAMPING TENT"):
        api_client.post("/api/topics", json={"name": name}, headers=headers)

    def _names(**params):
        response = api_client.get("/api/topics", params=params, headers=headers)
        assert response.status_code == 200, response.text
        return [t["name"] for t in response.json()["items"]]

    assert _names(keyword="露营") == ["露营灯", "露营椅"]  # 前缀子串命中
    assert _names(keyword="camping") == ["CAMPING TENT"]  # 大小写不敏感（ASCII）
    assert _names(keyword="不存在的话题") == []  # 无命中 = 空列表（不是全量）
    assert _names(keyword="   ") == sorted(["CAMPING TENT", "户外桌", "露营灯", "露营椅"]) or set(
        _names(keyword="   ")
    ) == {"CAMPING TENT", "户外桌", "露营灯", "露营椅"}  # 全空白 = 不过滤
    assert _names() == _names(keyword="")  # 不传与空串同义

    # 别人建的同名话题 ⇒ keyword 也捞不到（归属隔离）
    intruder = api_client.post(
        "/api/auth/register", json={"username": "kw-intruder", "password": "secret123"}
    )
    intruder_headers = {"Authorization": f"Bearer {intruder.json()['access_token']}"}
    api_client.post("/api/topics", json={"name": "露营大户"}, headers=intruder_headers)
    assert "露营大户" not in _names(keyword="露营")


# --------------------------------------------------------------------------- #
# 8) 标签表合并（迁移 b2c3d4e5f6g7）
# --------------------------------------------------------------------------- #


def test_migration_merges_tags_into_topics():
    """迁移必须含「tags → topics / note_tags → note_topics / topics_json 回填 /
    删旧表旧列」的逻辑（§34.4；源码断言 + 建表可达性）。"""
    from pathlib import Path

    migration = Path("backend/alembic/versions/b2c3d4e5f6g7_merge_tags_into_topics.py")
    assert migration.exists(), "找不到标签合并迁移文件"
    source = migration.read_text(encoding="utf-8")
    assert 'revision: str = "b2c3d4e5f6g7"' in source
    assert 'down_revision: Union[str, None] = "a0b1c2d3e4f5"' in source
    for fragment in (
        "CREATE TABLE topics",
        "CREATE TABLE note_topics",
        "FROM tags",
        "FROM note_tags",
        "topics_json",
        "DROP TABLE note_tags",
        "DROP TABLE tags",
        'drop_column("topics_json")',
    ):
        assert fragment in source, f"迁移缺少关键片段：{fragment}"


def test_models_have_no_tags_table_and_no_topics_json_column():
    """模型层：`tags` / `note_tags` 不在 metadata；`notes` 无 `topics_json` 列。"""
    from backend.app.core.database import Base

    tables = set(Base.metadata.tables)
    assert "tags" not in tables
    assert "note_tags" not in tables
    assert {"topics", "note_topics"} <= tables
    note_columns = {column.name for column in Note.__table__.columns}
    assert "topics_json" not in note_columns


def test_tag_and_topic_with_same_name_merge_into_one_row(seeded):
    """标签与话题同名（同一用户）⇒ 一行一关系（合并归并点，§34.4）。"""
    db, user, account, _ = seeded
    note = _add_note(db, account, "N-MERGE", content="正文 #露营")
    # 模拟「先有标签露营、再同步拆出话题露营」：典庶行已存在 ⇒ 只补关系
    existing = Topic(user_id=user.id, name="露营", color="#111111")
    db.add(existing)
    db.commit()
    outcome = nts.apply_sync_topics(db, note)
    assert outcome["topics_created"] == 0  # 没有新建话题行
    assert outcome["relations_added"] == 1
    topics = _topic_rows(db)
    assert len(topics) == 1 and topics[0].color == "#111111"  # 原标签行原样保留
    assert _relation_pairs(db) == [("N-MERGE", "露营")]
