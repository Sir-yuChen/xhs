"""存量话题标记修复脚本的回归（`.hermes/scripts/merge_legacy_topic_markers.py`）。

守什么：**清洗是「修复」而不是「删除」**。老脚本只摘正文不补关系，跑完那 64 篇
零关系的笔记会变成「本地无话题」—— 而平台正文里 `#名字[话题]#` 本身就是平台的
话题声明（2026-09-25 用户口径：**以三方数据为准**）。本文件钉住四条：

1. 脏话题行（`拼车[话题`）并进干净行，**关系一条不少**（改指，不是删）；
2. 正文带标记但零关系的笔记 ⇒ 既摘净正文，**又补上关系**（名字无标记）；
3. **幂等**：跑第二遍零改动（防误重跑造成二次破坏）；
4. dry-run 一个字节都不写。

脚本是 `.hermes/scripts/` 下的运维件（不入生产包），故用 importlib 从文件加载，
再拿公共夹具 `db_factory` 的测试库直接调它的 `repair(db, apply=…)`。
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
from sqlalchemy import select

from backend.app.models import Note, NoteTopic, PlatformAccount, Topic, User



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract

SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2]
    / ".hermes"
    / "scripts"
    / "merge_legacy_topic_markers.py"
)


@pytest.fixture(scope="module")
def repair_module():
    spec = importlib.util.spec_from_file_location("_merge_legacy_topic_markers", SCRIPT)
    assert spec is not None and spec.loader is not None, f"脚本加载失败：{SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seeded(db_factory):
    """一个用户 + 一个账号（脏数据需要归属者；脚本按 `(user_id, name)` 判重）。"""
    db = db_factory()
    user = User(username="repair-owner", password_hash="x")
    db.add(user)
    db.flush()
    account = PlatformAccount(
        user_id=user.id,
        platform="xhs",
        sub_type="creator",
        nickname="我的号",
        status="active",
    )
    db.add(account)
    db.flush()
    return db, user, account


def _add_note(db, account, note_id: str, content: str) -> Note:
    note = Note(
        platform_account_id=account.id,
        user_id=account.user_id,
        note_id=note_id,
        platform="xhs",
        title=note_id,
        content=content,
        note_type="synced",
        raw_json={"note_url": f"https://www.xiaohongshu.com/explore/{note_id}"},
    )
    db.add(note)
    db.flush()
    return note


def _topic(db, user, name: str) -> Topic:
    row = Topic(user_id=user.id, name=name)
    db.add(row)
    db.flush()
    return row


def _names(db, note: Note) -> list[str]:
    return list(
        db.scalars(
            select(Topic.name)
            .join(NoteTopic, NoteTopic.topic_id == Topic.id)
            .where(NoteTopic.note_id == note.id)
            .order_by(NoteTopic.id)
        ).all()
    )


def test_dirty_topic_rows_merge_instead_of_losing_relations(repair_module, seeded):
    """脏行并进干净行：关系**改指**、篇数不变、脏行消失（不是删关系了事）。"""
    db, user, account = seeded
    clean = _topic(db, user, "拼车")
    dirty = _topic(db, user, "拼车[话题")
    note_a = _add_note(db, account, "n-a", "正文甲")
    note_b = _add_note(db, account, "n-b", "正文乙")
    db.add(NoteTopic(note_id=note_a.id, topic_id=dirty.id))
    db.add(NoteTopic(note_id=note_b.id, topic_id=dirty.id))
    db.flush()

    report = repair_module.repair(db, apply=True)
    db.commit()

    assert report["topics_merged"] == 1 and report["topics_renamed"] == 0
    assert [str(r.name) for r in db.scalars(select(Topic)).all()] == ["拼车"]
    assert _names(db, note_a) == ["拼车"]
    assert _names(db, note_b) == ["拼车"]


def test_dirty_row_without_a_clean_twin_is_renamed_in_place(repair_module, seeded):
    """没有干净行可并 ⇒ 原地改名（保 id，关系不用动）。"""
    db, user, account = seeded
    dirty = _topic(db, user, "顺风车[话题")
    note = _add_note(db, account, "n-c", "正文")
    db.add(NoteTopic(note_id=note.id, topic_id=dirty.id))
    db.flush()

    report = repair_module.repair(db, apply=True)
    db.commit()

    assert report["topics_merged"] == 0 and report["topics_renamed"] == 1
    assert _names(db, note) == ["顺风车"]


def test_marked_content_gets_topics_linked_not_just_stripped(repair_module, seeded):
    """正文带标记 + 零关系（库里 64 篇的真实形态）⇒ 摘净正文**且补上关系**。"""
    db, user, account = seeded
    note = _add_note(
        db,
        account,
        "n-d",
        "好穿的鞋\n#拖鞋[话题]# #鞋[话题]# #穿搭bot[话题]#",
    )
    db.flush()

    report = repair_module.repair(db, apply=True)
    db.commit()

    assert report["notes_stripped"] == 1
    assert "[话题]" not in str(note.content)
    assert "#" not in str(note.content)
    assert _names(db, note) == ["拖鞋", "鞋", "穿搭bot"]  # 保正文顺序；名字无标记


def test_repair_is_idempotent(repair_module, seeded):
    """跑第二遍必须零改动 —— 防误重跑把已修好的数据再动一遍。"""
    db, user, account = seeded
    _topic(db, user, "露营[话题")
    note = _add_note(db, account, "n-e", "正文\n#露营[话题]#")
    db.flush()

    repair_module.repair(db, apply=True)
    db.commit()
    content_before = str(note.content)
    names_before = _names(db, note)

    again = repair_module.repair(db, apply=True)
    db.commit()

    assert again["dirty_topics"] == 0 and again["marked_notes"] == 0
    assert again.get("topics_merged", 0) == 0 and again.get("relations_added", 0) == 0
    assert str(note.content) == content_before
    assert _names(db, note) == names_before


def test_dry_run_writes_nothing(repair_module, seeded):
    """dry-run 只出计划：话题行、正文、关系表都不动。"""
    db, user, account = seeded
    _topic(db, user, "拼车[话题")
    note = _add_note(db, account, "n-f", "正文\n#拼车[话题]#")
    db.flush()
    content_before = str(note.content)

    plan = repair_module.repair(db, apply=False)

    assert plan == {"dirty_topics": 1, "marked_notes": 1}
    assert str(note.content) == content_before
    assert [str(r.name) for r in db.scalars(select(Topic)).all()] == ["拼车[话题"]
    assert db.scalars(select(NoteTopic)).all() == []


def test_merge_never_crosses_tenant_boundary(repair_module, db_factory):
    """判重必须带 `user_id`：B 的脏行不许并进 A 的同名干净行（DEF-066 同族）。

    不带 `user_id` 时 `拼车[话题`（B）会找上 A 的 `拼车` ⇒ **B 的关系行改指 A 的话题**，
    多租户数据互串，而单用户库里看不出任何异常。
    """
    db = db_factory()
    owner_a = User(username="repair-a", password_hash="x")
    owner_b = User(username="repair-b", password_hash="x")
    db.add_all([owner_a, owner_b])
    db.flush()
    account_a = PlatformAccount(
        user_id=owner_a.id, platform="xhs", sub_type="creator", nickname="A", status="active"
    )
    account_b = PlatformAccount(
        user_id=owner_b.id, platform="xhs", sub_type="creator", nickname="B", status="active"
    )
    db.add_all([account_a, account_b])
    db.flush()

    a_clean = _topic(db, owner_a, "拼车")  # A 的干净行：不许被 B 的脏行并走
    b_dirty = _topic(db, owner_b, "拼车[话题")
    note_b = _add_note(db, account_b, "n-cross", "正文")
    db.add(NoteTopic(note_id=note_b.id, topic_id=b_dirty.id))
    db.flush()

    repair_module.repair(db, apply=True)
    db.commit()

    # B 的脏行**原地改名**（不并进 A 的行）；关系仍指 B 自己的话题行
    b_names = _names(db, note_b)
    assert b_names == ["拼车"]
    assert [str(r.name) for r in db.scalars(select(Topic).order_by(Topic.id)).all()] == [
        "拼车",
        "拼车",
    ]
    owners = {int(r.user_id) for r in db.scalars(select(Topic)).all()}
    assert owners == {owner_a.id, owner_b.id}, "两个租户各持自己的行"
    assert a_clean.id != b_dirty.id


def test_unmarked_notes_are_left_completely_alone(repair_module, seeded):
    """没标记的笔记不在处理范围（脚本按 `[话题]#` 选，不按「有 #」扫）。"""
    db, user, account = seeded
    note = _add_note(db, account, "n-g", "纯正文，一个井号都没有 #普通话题 也算")
    db.flush()
    before = str(note.content)

    report = repair_module.repair(db, apply=True)
    db.commit()

    assert report["marked_notes"] == 0
    assert str(note.content) == before
    assert _names(db, note) == []


def test_repair_script_reuses_the_single_topic_implementation():
    """脚本不许自造第二份解析/落库实现（改形态时只改一处）。"""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "apply_sync_topics" in source, "补关系必须走同步链路同一实现"
    assert "strip_topic_marker" in source, "归一必须走唯一实现"
    for forbidden in ("re.compile", "_TOKEN_RE", "INSERT INTO note_topics"):
        assert forbidden not in source, f"脚本里出现了自造实现：{forbidden}"
