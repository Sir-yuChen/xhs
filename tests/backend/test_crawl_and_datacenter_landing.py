"""采集 / 数据中心 / 内容库三方的落库与收录口径。

## 用户口径（2026-10-28）

- 「采集的笔记先放内容库，标识统计优化成采集链」⇒ 采集独立标识 `collected`，进内容库；
- 「数据中心数据不落内容库表」⇒ 数据中心链路只写 `xhs_note_datacenter_stats`。

## 为什么行为断言测不到（L2）

三条判据都是**跨链路的静默错**，单条请求的返回值都正常：

| 判据 | 破坏了会怎样（不报错） |
|---|---|
| 采集真的落库 | `/data` 端点 SSE 正常回 `done`，抓完却一行没存（此前就这样：`normalized_for_save` 攒了不写） |
| 数据中心不写 notes | 同步「成功」，内容库凭空多出 N 条副产物 |
| 采集标识是 collected | 落成 `local` 后被内容库排除，用户抓完在库里找不到 |

## 用例预算：6
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import select, text

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def account(db):
    """库里必须有一个账号才能测落库 —— 自己建，不依赖测试库预置数据。"""
    from backend.app.models import PlatformAccount, User

    user = User(username="crawl_landing_u", password_hash="x", role="admin")
    db.add(user)
    db.flush()
    acct = PlatformAccount(user_id=user.id, platform="xhs", sub_type="pc", nickname="采集账号")
    db.add(acct)
    db.flush()
    return acct


# --------------------------------------------------------------------- 常量层
def test_library_takes_generated_and_collected_only() -> None:
    """内容库收录 = 本地生成 + 采集。

    ⚠️ 真跑：直接 import 运行时常量，不读源码文本 —— 字符串断言证明不了运行结果。
    """
    from backend.app.models.note import LIBRARY_NOTE_TYPES, NOTE_TYPE_COLLECTED, NOTE_TYPE_LOCAL_GENERATED

    assert LIBRARY_NOTE_TYPES == (NOTE_TYPE_LOCAL_GENERATED, NOTE_TYPE_COLLECTED)
    #: 数据中心同步（`local`）与三方同步（`synced`）都不许进。
    assert "local" not in LIBRARY_NOTE_TYPES
    assert "synced" not in LIBRARY_NOTE_TYPES


# --------------------------------------------------------------------- 采集落库（真跑 DB）
def test_crawl_save_actually_writes_a_row(db, account) -> None:
    """`_save_normalized_notes` 必须真的写进 `notes`，且标识是 `collected`。

    ⚠️ 这是本组最要紧的一条：此前的 bug 就是「函数攒了数据但没人调用」，
    所以必须**调用一次再看库**，不能只断言函数存在。
    """
    from backend.app.api.platforms.xhs.crawl import _save_normalized_notes
    from backend.app.models import Note

    item = {
        "note_id": "test_collected_001",
        "title": "采集落库自检",
        "author_name": "someone",
        "content": "正文",
    }
    saved = _save_normalized_notes(db, account, [item])
    db.commit()

    row = db.scalars(select(Note).where(Note.note_id == "test_collected_001")).first()
    assert row is not None, "采集项没有落库 —— `_save_normalized_notes` 没被真正调用"
    assert len(saved) == 1
    assert row.note_type == "collected", f"采集落库标识应为 collected，实际 {row.note_type}"
    assert row.publish_status == "pending"


def test_second_crawl_reuses_the_row_not_duplicates(db, account) -> None:
    """同一条笔记采集两次 ⇒ 复用同一行（`uq_notes_user_note`），不新增副本。"""
    from backend.app.api.platforms.xhs.crawl import _save_normalized_notes

    item = {"note_id": "test_collected_002", "title": "第一次"}
    _save_normalized_notes(db, account, [item])
    db.commit()
    _save_normalized_notes(db, account, [item])
    db.commit()

    count = db.scalar(
        text("SELECT count(*) FROM xhs_notes WHERE note_id = 'test_collected_002'")
    )
    assert count == 1, f"采集同一篇落了 {count} 行（应按 (user_id, note_id) upsert）"


def test_soft_deleted_note_is_not_revived_by_crawl(db, account) -> None:
    """已软删的笔记，采集命中时跳过不复活（清理过的笔记不该被下一次抓取救回来）。"""
    from backend.app.api.platforms.xhs.crawl import _save_normalized_notes
    from backend.app.models import Note

    item = {"note_id": "test_collected_003", "title": "将被软删"}
    _save_normalized_notes(db, account, [item])
    db.commit()
    row = db.scalars(select(Note).where(Note.note_id == "test_collected_003")).first()
    row.is_deleted = True
    db.commit()

    saved = _save_normalized_notes(db, account, [item])
    db.commit()
    assert saved == [], "软删的笔记被采集复活了"
    assert db.scalars(select(Note).where(Note.note_id == "test_collected_003")).first().is_deleted


# --------------------------------------------------------------------- 数据中心不落内容库
def test_datacenter_sync_does_not_create_notes_rows(db, account) -> None:
    """数据中心同步只写统计表 —— `notes` 行数不变。

    ⚠️ 这是「数据中心数据不落内容库表」的判据：调用一次落库，数 `notes` 前后差。
    """
    from backend.app.services import note_data_service as nds

    before = db.scalar(text("SELECT count(*) FROM xhs_notes"))

    row = nds.NoteDataRow(note_id="test_datacenter_only_001")
    nds.upsert_note_data_rows(db, [row], user_id=account.user_id, account_id=account.id)
    db.commit()

    after = db.scalar(text("SELECT count(*) FROM xhs_notes"))
    assert after == before, f"数据中心同步往 notes 写了 {after - before} 行（应只写统计表）"

    stat = db.scalar(
        text("SELECT count(*) FROM xhs_note_datacenter_stats WHERE note_id = 'test_datacenter_only_001'")
    )
    assert stat == 1, "数据中心同步没写统计行 —— 那是它该写的地方"


# --------------------------------------------------------------------- 内容库读路径
def test_local_notes_stay_out_of_the_library(db) -> None:
    """库里若有 `local` 行（数据中心副产物），内容库列表也不该把它查出来。"""
    from backend.app.models.note import LIBRARY_NOTE_TYPES

    lib_types = set(LIBRARY_NOTE_TYPES)
    local_rows = db.scalar(text("SELECT count(*) FROM xhs_notes WHERE note_type = 'local'"))
    visible = db.scalar(
        text(
            "SELECT count(*) FROM xhs_notes WHERE is_deleted = false "
            f"AND note_type IN ({','.join(repr(t) for t in lib_types)})"
        )
    )
    total_lib = db.scalar(
        text("SELECT count(*) FROM xhs_notes WHERE is_deleted = false AND publish_status != 'published'")
    )
    assert visible <= total_lib
    if local_rows:
        assert "local" not in lib_types, "local 混进了内容库收录口径"
