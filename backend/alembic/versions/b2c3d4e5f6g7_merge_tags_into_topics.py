"""merge tags into topics: 话题拆分 + 标签表并入话题表（设计 13 §34）

2026-09-25 用户口径（两条，全部落地在本迁移 + 同轮代码）：

> ① 「同步笔记和内容库页中的同步笔记按钮，执行同步笔记时，将笔记中的话题进行拆分出来，
> 写入到话题表中，并新增话题与笔记的关系，新增话题前先检查话题是否存在，存在则不新增
> 话题，只新增当前话题与笔记之间的关联关系，实现后笔记正文不再存在话题，话题与正文
> 进行拆分，发布笔记时注意查询该笔记下的话题进行添加」；
> ② 「将当前系统的笔记标签表合并成话题表，将内容库笔记详情弹窗中标题后展示标签tag和
> 添加标签tag合并到笔记话题功能中」。

## 本迁移做什么（幂等可续跑）

1. **建两张新表**：`topics`（话题字典，`(user_id, name)` 唯一）与 `note_topics`
   （笔记 ↔ 话题关系，`(note_id, topic_id)` 唯一）—— 与 `note_goods`（§26）同款分工；
2. **`tags` → `topics` 搬迁**：`user_id` / `name` / `color` 原样，`platform_id=""`；
3. **`note_tags` → `note_topics` 搬迁**：`tag_id` 经 2 的新 id 映射；
4. **`notes.topics_json` 回填**：按 `(user_id, name)` **找或建** topic + 补关系
   （与 2/3 天然合并 —— 同一 `(user, name)` 只一行，正是用户 ②「标签表合并成
   话题表」的归并点）；`local_generated` 行的 `platform_id` 从
   `raw_json.aigc.topics[{id,name}]` 取（发布链路 `hash_tag` 要平台 ID）；
5. **删 `note_tags` / `tags` 两表**；
6. **删 `notes.topics_json` 列**（batch 重建）：关系表落地后它就是同一语义的
   第二份实现（DEF-044/047/058）⇒ 删列，序列化的 `topics` 改走关系表 join。

⚠️ 本库 `PRAGMA foreign_keys` 实测为 0（`core/database.py` 只设 journal_mode），
重建表期间不必处理级联；FK 约束仍按模型声明（开着 FK 的库也合法）。

## 存量口径（不造假）

* `topics_json` 解析不出 ⇒ 该行不建关系（等下一轮同步详情补齐再写）；
* 同步拆出的话题没有平台 ID ⇒ `platform_id=""`；只有 `aigc.topics` 给的才写；
* `note_tags` 指向的 `tag_id` 在映射里不存在 ⇒ 跳过（搬迁按显式 id 映射，
  不做「按条件猜」的批量补）。

## 可逆

`downgrade` 重建 `tags` / `note_tags` 两表并把 `topics` / `note_topics` 搬回
（`topics` 全行 ⇒ `tags`，关系行 ⇒ `note_tags`），`notes.topics_json` 按关系表
重算（名字列表，JSON 串）。⚠️ **合并不可逆**：同名的「标签」与「话题」在 upgrade
时已归并成一行，downgrade 只能还原成一行 tags —— 那一行就是合并后的事实。
"""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from backend.app.core.migration_introspect import column_names, table_exists


revision: str = "b2c3d4e5f6g7"
down_revision: Union[str, None] = "a0b1c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(conn, table: str) -> bool:
    #: v1.1.0 PG 化：原为 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return table_exists(conn, table)


def _has_column(conn, table: str, column: str) -> bool:
    #: v1.1.0 PG 化：原为 `PRAGMA table_info` 取 `row[1]` 比较（SQLite 专属）。
    return column in column_names(conn, table)


def _create_topic_tables(conn) -> None:
    if not _has_table(conn, "topics"):
        conn.execute(
            sa.text(
                """
                CREATE TABLE topics (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    name VARCHAR(64) NOT NULL,
                    color VARCHAR(24) NOT NULL DEFAULT '#1677ff',
                    platform_id VARCHAR(64) NOT NULL DEFAULT '',
                    created_at TIMESTAMP,
                    CONSTRAINT uq_topics_user_name UNIQUE (user_id, name)
                )
                """
            )
        )
        conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_topics_user_id ON topics (user_id)"))
        conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_topics_id ON topics (id)"))
    if not _has_table(conn, "note_topics"):
        conn.execute(
            sa.text(
                """
                CREATE TABLE note_topics (
                    id INTEGER NOT NULL PRIMARY KEY,
                    note_id INTEGER NOT NULL,
                    topic_id INTEGER NOT NULL,
                    created_at TIMESTAMP,
                    CONSTRAINT uq_note_topics_note_topic UNIQUE (note_id, topic_id)
                )
                """
            )
        )
        conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_note_topics_note_id ON note_topics (note_id)"))
        conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_note_topics_topic_id ON note_topics (topic_id)"))
        conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_note_topics_id ON note_topics (id)"))


def _find_or_create_topic(conn, user_id: int, name: str, *, color: str = "", platform_id: str = "") -> int:
    """按 (user_id, name) 找或建话题行，返回 id。存在则**不新建**（用户口径）。"""
    row = conn.execute(
        sa.text("SELECT id, platform_id FROM topics WHERE user_id = :uid AND name = :name"),
        {"uid": user_id, "name": name},
    ).first()
    if row is not None:
        topic_id, existing_platform_id = int(row[0]), str(row[1] or "")
        # platform_id 已知则补填（不覆盖已有非空值 —— 同一次搬迁里多个来源不互相冲）。
        if platform_id and not existing_platform_id:
            conn.execute(
                sa.text("UPDATE topics SET platform_id = :pid WHERE id = :id"),
                {"pid": platform_id, "id": topic_id},
            )
        return topic_id
    #: v1.1.0 PG 化：id 取自 `RETURNING id`（原 `result.lastrowid` 是 SQLite
    #: 驱动的产物，psycopg 上恒为 None）。
    result = conn.execute(
        sa.text(
            "INSERT INTO topics (user_id, name, color, platform_id, created_at) "
            "VALUES (:uid, :name, :color, :pid, :now) RETURNING id"
        ),
        {
            "uid": user_id,
            "name": name,
            "color": color or "#1677ff",
            "pid": platform_id or "",
            "now": "2026-09-25 00:00:00",
        },
    )
    return int(result.scalar_one())


def _link(conn, note_pk: int, topic_id: int) -> None:
    """补关系行（幂等：唯一键冲突即已存在，跳过）。

    ⚠️ v1.1.0 PG 化：原为 `INSERT OR IGNORE`（SQLite 专属）；
    PG 等价物 = `ON CONFLICT DO NOTHING`（不指定冲突目标 = 任何唯一约束冲突都跳过，
    与 OR IGNORE 语义一致）。
    """
    conn.execute(
        sa.text(
            "INSERT INTO note_topics (note_id, topic_id, created_at) "
            "VALUES (:nid, :tid, :now) ON CONFLICT DO NOTHING"
        ),
        {"nid": note_pk, "tid": topic_id, "now": "2026-09-25 00:00:00"},
    )


def _topic_names_from_json(text: object) -> list[str]:
    """`topics_json`（Text 列存 JSON 数组）⇒ 名字列表；坏 JSON / 非数组 ⇒ []。"""
    if not isinstance(text, str) or not text.strip():
        return []
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
    return list(dict.fromkeys(names))


def _aigc_topic_ids(raw_text: object) -> dict[str, str]:
    """`raw_json.aigc.topics[{id,name}]` ⇒ {name: id}（发布 hash_tag 要平台 ID）。"""
    if not isinstance(raw_text, str) or not raw_text.strip():
        return {}
    try:
        raw = json.loads(raw_text)
    except (TypeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    aigc = raw.get("aigc")
    if not isinstance(aigc, dict):
        return {}
    out: dict[str, str] = {}
    for topic in aigc.get("topics") or []:
        if isinstance(topic, dict):
            name = topic.get("name")
            topic_id = topic.get("id")
            if isinstance(name, str) and name.strip() and topic_id:
                out[name.strip()] = str(topic_id)
    return out


def upgrade() -> None:
    conn = op.get_bind()
    _create_topic_tables(conn)

    # 2) tags → topics（记 old_tag_id → new_topic_id 映射，3 要用）
    tag_map: dict[int, int] = {}
    if _has_table(conn, "tags"):
        for tag_id, user_id, name, color in conn.execute(
            sa.text("SELECT id, user_id, name, color FROM tags")
        ).fetchall():
            new_id = _find_or_create_topic(
                conn, int(user_id), str(name), color=str(color or "")
            )
            tag_map[int(tag_id)] = new_id
        print(f"  [b2c3d4e5f6g7] tags -> topics: {len(tag_map)} rows")  # noqa: T201

    # 3) note_tags → note_topics（经映射；缺一侧跳过，不造假）
    linked_from_tags = 0
    if _has_table(conn, "note_tags"):
        for note_id, tag_id in conn.execute(
            sa.text("SELECT note_id, tag_id FROM note_tags")
        ).fetchall():
            new_topic_id = tag_map.get(int(tag_id))
            if new_topic_id is None:
                continue
            _link(conn, int(note_id), new_topic_id)
            linked_from_tags += 1
        print(f"  [b2c3d4e5f6g7] note_tags -> note_topics: {linked_from_tags} rows")  # noqa: T201

    # 4) notes.topics_json 回填（与 2/3 按 (user_id, name) 天然合并）
    if _has_column(conn, "notes", "topics_json"):
        backfilled = 0
        rows = conn.execute(
            sa.text("SELECT id, user_id, topics_json, raw_json, note_type FROM notes")
        ).fetchall()
        for note_pk, user_id, topics_text, raw_text, note_type in rows:
            names = _topic_names_from_json(topics_text)
            platform_ids = _aigc_topic_ids(raw_text) if note_type == "local_generated" else {}
            for name in names:
                topic_id = _find_or_create_topic(
                    conn, int(user_id), name, platform_id=platform_ids.get(name, "")
                )
                _link(conn, int(note_pk), topic_id)
            if names:
                backfilled += 1
        print(f"  [b2c3d4e5f6g7] topics_json backfilled: {backfilled}/{len(rows)} notes")  # noqa: T201

    # 5) 删旧表（先关系后实体，顺序不能反）
    if _has_table(conn, "note_tags"):
        conn.execute(sa.text("DROP TABLE note_tags"))
    if _has_table(conn, "tags"):
        conn.execute(sa.text("DROP TABLE tags"))

    # 6) 删 notes.topics_json 列（batch 重建；序列化改走关系表 join）
    if _has_column(conn, "notes", "topics_json"):
        with op.batch_alter_table("notes", schema=None) as batch_op:
            batch_op.drop_column("topics_json")


def downgrade() -> None:
    conn = op.get_bind()

    # 重建 tags / note_tags（按现行 topics / note_topics 搬回）
    if not _has_table(conn, "tags"):
        conn.execute(
            sa.text(
                """
                CREATE TABLE tags (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    name VARCHAR(64) NOT NULL,
                    color VARCHAR(24) DEFAULT '#111111'
                )
                """
            )
        )
        conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_tags_user_id ON tags (user_id)"))
    if not _has_table(conn, "note_tags"):
        conn.execute(
            sa.text(
                """
                CREATE TABLE note_tags (
                    note_id INTEGER NOT NULL,
                    tag_id INTEGER NOT NULL,
                    PRIMARY KEY (note_id, tag_id)
                )
                """
            )
        )

    tag_map: dict[int, int] = {}
    if _has_table(conn, "topics"):
        for topic_id, user_id, name, color in conn.execute(
            sa.text("SELECT id, user_id, name, color FROM topics")
        ).fetchall():
            result = conn.execute(
                sa.text("INSERT INTO tags (user_id, name, color) VALUES (:uid, :name, :color)"),
                {"uid": int(user_id), "name": str(name), "color": str(color or "#111111")},
            )
            tag_map[int(topic_id)] = int(result.lastrowid)
    if _has_table(conn, "note_topics"):
        for note_id, topic_id in conn.execute(
            sa.text("SELECT note_id, topic_id FROM note_topics")
        ).fetchall():
            new_tag_id = tag_map.get(int(topic_id))
            if new_tag_id is None:
                continue
            conn.execute(
                sa.text(
                    "INSERT INTO note_tags (note_id, tag_id) VALUES (:nid, :tid) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"nid": int(note_id), "tid": new_tag_id},
            )

    # notes.topics_json 按关系表重算（名字列表）
    if not _has_column(conn, "notes", "topics_json"):
        with op.batch_alter_table("notes", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("topics_json", sa.Text(), server_default="[]", nullable=False)
            )
    if _has_table(conn, "note_topics") and _has_table(conn, "topics"):
        names_by_note: dict[int, list[str]] = {}
        rows = conn.execute(
            sa.text(
                "SELECT nt.note_id, t.name FROM note_topics nt "
                "JOIN topics t ON t.id = nt.topic_id ORDER BY nt.id"
            )
        ).fetchall()
        for note_id, name in rows:
            names_by_note.setdefault(int(note_id), []).append(str(name))
        for note_pk, names in names_by_note.items():
            conn.execute(
                sa.text("UPDATE notes SET topics_json = :names WHERE id = :id"),
                {"names": json.dumps(names, ensure_ascii=False), "id": note_pk},
            )

    # 删新表（先关系后实体）
    if _has_table(conn, "note_topics"):
        conn.execute(sa.text("DROP TABLE note_topics"))
    if _has_table(conn, "topics"):
        conn.execute(sa.text("DROP TABLE topics"))
