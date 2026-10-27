"""数据中心与内容库**读路径完全拆分**（2026-10-27 用户口径）。

## 口径

> 「将数据中心的逻辑与内容库完全拆分」

两个功能各有一张自己的表，读路径**互不 join**：

| 功能 | 表 | 判据来源 |
|---|---|---|
| 内容库 | `notes` | `LIBRARY_NOTE_TYPES` + 排除已发布 |
| 数据中心 | `note_datacenter_stats` | 自己的 `platform_deleted_at` |

它们只在「同一个平台 `note_id`」上语义相关，**不是**互为展示附属。

## 为什么必须拆（拆分前的实际故障）

`get_offline_notes` 会回查 `notes` 拿标题/封面、并用 `notes.is_deleted` 做 EXISTS
过滤。后果：
1. 内容库删行（软删 / 存量清理）⇒ 数据中心对应行**在页面上消失**，
   而它的指标快照本来是独立的历史事实；
2. 回查拿不到 ⇒ 标题**静默变空**（页面只剩一片空白卡片），不报错。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOTES_API = ROOT / "backend" / "app" / "api" / "notes.py"
DATACENTER_PAGE = ROOT / "frontend" / "src" / "pages" / "platforms" / "xhs" / "datacenter-page.tsx"


def _code(path: Path) -> str:
    """剥掉注释与 docstring，只留代码（守卫不许被注释满足）。"""
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
    for idx, line in enumerate(source.splitlines(), start=1):
        if idx in drop or line.strip().startswith("#"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _datacenter_body() -> str:
    """切出数据中心列表端点的函数体。"""
    code = _code(NOTES_API)
    start = code.index("def get_offline_notes(")
    end = code.index("\n@router.", start)
    return code[start:end]


class TestDatacenterDoesNotDependOnNotes:
    def test_no_notes_lookup_for_display(self):
        """不许回查 `notes` 取标题 / 封面 —— 统计表自己有这些列。"""
        body = _datacenter_body()
        assert "note_by_id" not in body, "又在回查 notes 取展示字段了"
        assert "select(Note) where" not in body
        # 统计表查询 + 一个 Note 实体查询同时出现 = 回查
        assert not ("NoteDatacenterStat" in body and "db.scalars(select(Note)" in body)

    def test_no_soft_delete_cross_filter(self):
        """不许用 `notes.is_deleted` 过滤统计行 —— 那是跨表 EXISTS。"""
        body = _datacenter_body()
        assert "soft_deleted_subq" not in body
        assert "Note.is_deleted" not in body

    def test_uses_its_own_deleted_judgment(self):
        """数据中心自己的「已删除」判据 = 统计表 `platform_deleted_at`。"""
        body = _datacenter_body()
        assert "NoteDatacenterStat.platform_deleted_at" in body

    def test_title_comes_from_the_stat_table(self):
        """标题必须来自统计表自身，不回落到 notes。"""
        body = _datacenter_body()
        assert '"title": r.title or ""' in body

    def test_frontend_type_has_no_in_library_flag(self):
        """`in_library` 是「这条在不在内容库」的跨表判据，拆分后不存在。"""
        text = DATACENTER_PAGE.read_text(encoding="utf-8")
        assert "in_library" not in text
