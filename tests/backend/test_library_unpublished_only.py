"""内容库只放未发布的本地笔记（2026-10-27 用户口径）。

## 口径原文

1. 「只显示用户本地生成未发布的笔记，已发布的笔记不展示在内容库中，
   应到我的笔记页查看实时数据」
2. 「物理删除，会更新」

## 落到代码的三件事

| 事 | 落点 |
|---|---|
| 列表只出本地 + 未发布 | `api/notes.py::list_notes` 的两道 `where` |
| 发布成功后物理删除该行 | `note_publish_service::_purge_published_note` |
| 删除前清完从属行 | `note_purge_service::purge_note`（本轮补 `note_generation_links`） |

## 为什么需要这些守卫

`purge_note` 漏清 `note_generation_links` 是个**必然踩到**的坑：本地生成笔记
几乎都有这条关联（实测已发布的 19 篇里 18 篇有），漏清就是删笔记撞外键 500。
而发布链路是「平台已成功」之后再删本地 —— 撞外键会让平台成功的事被记成失败。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

NOTES_API = ROOT / "backend" / "app" / "api" / "notes.py"
NOTE_MODEL = ROOT / "backend" / "app" / "models" / "note.py"
PUBLISH_SERVICE = ROOT / "backend" / "app" / "services" / "note_publish_service.py"
PURGE_SERVICE = ROOT / "backend" / "app" / "services" / "note_purge_service.py"


def _code(path: Path) -> str:
    """剥掉注释与 docstring，只留真正的代码（守卫不许被自己的注释满足）。"""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = source.splitlines()
    drop: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    drop.update(range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1))
    kept = []
    for idx, line in enumerate(lines, start=1):
        if idx in drop:
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        kept.append(line)
    return "\n".join(kept)


class TestLibraryShowsOnlyUnpublishedLocalNotes:
    """列表端点的两道 where —— 唯一判据在 `models/note.py` 的常量。"""

    def test_library_note_types_excludes_synced(self):
        """三方同步源已退场 ⇒ 内容库只装 local / local_generated。"""
        import sys

        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from backend.app.models.note import (
            NOTE_TYPE_LOCAL,
            NOTE_TYPE_LOCAL_GENERATED,
            NOTE_TYPE_SYNCED,
            LIBRARY_NOTE_TYPES,
        )

        assert set(LIBRARY_NOTE_TYPES) == {NOTE_TYPE_LOCAL, NOTE_TYPE_LOCAL_GENERATED}
        assert NOTE_TYPE_SYNCED not in LIBRARY_NOTE_TYPES

    def test_failed_notes_stay_visible(self):
        """⚠️ `failed` 必须可见 —— 它是「没发出去、可重试」，白名单 pending 会吞掉它。"""
        import sys

        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from backend.app.models.note import (
            PUBLISH_STATUS_FAILED,
            PUBLISH_STATUS_PENDING,
            LIBRARY_EXCLUDED_PUBLISH_STATUSES,
        )

        assert PUBLISH_STATUS_FAILED not in LIBRARY_EXCLUDED_PUBLISH_STATUSES
        assert PUBLISH_STATUS_PENDING not in LIBRARY_EXCLUDED_PUBLISH_STATUSES

    def test_published_is_the_only_excluded_status(self):
        import sys

        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from backend.app.models.note import (
            PUBLISH_STATUS_PUBLISHED,
            LIBRARY_EXCLUDED_PUBLISH_STATUSES,
        )

        assert LIBRARY_EXCLUDED_PUBLISH_STATUSES == (PUBLISH_STATUS_PUBLISHED,)

    def test_list_endpoint_applies_both_filters(self):
        """两个常量都必须真的进 SQL —— 定义了不用等于没做。"""
        code = _code(NOTES_API)
        assert "Note.note_type.in_(LIBRARY_NOTE_TYPES)" in code
        assert "Note.publish_status.not_in_(LIBRARY_EXCLUDED_PUBLISH_STATUSES)" in code


class TestPublishedNoteIsPhysicallyPurged:
    """发布成功 ⇒ 本地行物理删除；且删失败绝不判发布失败。"""

    def test_success_path_calls_purge(self):
        code = _code(PUBLISH_SERVICE)
        assert "_purge_published_note(db, note)" in code

    def test_purge_uses_the_single_delete_chain(self):
        """不许自己写 `db.delete(note)` —— 那样必撞外键。"""
        code = _code(PUBLISH_SERVICE)
        assert "purge_note(db, note)" in code
        assert "db.delete(note)" not in code

    def test_purge_failure_never_becomes_publish_failure(self):
        """删本地失败只降级记日志，绝不 return 失败。"""
        source = PUBLISH_SERVICE.read_text(encoding="utf-8")
        start = source.index("def _purge_published_note(")
        end = source.index("\ndef publish_queue_snapshot(", start)
        body = source[start:end]
        # 只有 except 分支，没有 re-raise、没有 return False
        assert "raise" not in body
        assert "return False" not in body
        assert "db.rollback()" in body

    def test_return_values_are_captured_before_purge(self):
        """删行后再读 note.id 是失效对象 ⇒ 必须先取值。"""
        code = _code(PUBLISH_SERVICE)
        assert "published_note_pk = int(note.id)" in code
        assert code.index("published_note_pk = int(note.id)") < code.index(
            "_purge_published_note(db, note)"
        )


class TestPurgeChainClearsEveryDependentTable:
    """`purge_note` 必须清完全部 6 张有外键指向 notes 的表。"""

    #: 6 张表里必须出现在删除链中的 ORM 类名 → 说明
    REQUIRED = {
        "NoteTopic": "话题关系行（话题字典本身不动）",
        "NoteAsset": "资产行",
        "NoteComment": "评论行",
        "NoteMetricSnapshot": "指标日快照",
        "NoteGenerationLink": "生成版本桥（本轮补上，漏了必撞外键）",
    }

    @pytest.mark.parametrize("model_name,why", list(REQUIRED.items()))
    def test_dependent_table_is_cleared(self, model_name, why):
        code = _code(PURGE_SERVICE)
        assert f"delete({model_name})" in code, f"{model_name}（{why}）没有被清理"

    def test_ai_draft_is_only_detached_not_deleted(self):
        """草稿是用户资产 —— 只解除引用，不连坐删掉。"""
        code = _code(PURGE_SERVICE)
        assert 'update({"source_note_id": None})' in code
        assert "delete(AiDraft)" not in code
