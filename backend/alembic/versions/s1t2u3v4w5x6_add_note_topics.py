"""add notes.topics_json (话题专有解耦列)

2026-09-22 用户口径（设计 13 §28）：「笔记内容字段增加话题和专有解耦」。

## 这一列是什么

``topics_json``：Text 列存 JSON 数组（话题 / #标签名列表），默认 ``'[]'``。

**为什么解耦成专有列**：话题是本系统自己要渲染的语义，此前只活在 ``raw_json``
（平台原文）里，每个读方都得自己挖一遍形状（creator 扁平 / PC 嵌套）—— 挖法漂移
就是显示漂移。专有列 + 唯一写入方 = 一份真相源。⚠️ ``raw_json`` **原样保留**
（证据链不动），本列是它的**投影**，不是替代。

## 写入方（运行期，不在本迁移里做）

* 详情补齐（``hydrate_note_detail``）：详情拿到非空话题 ⇒ 覆盖（最新真相）；为空 ⇒ 不碰；
* 列表落库（``_save_normalized_notes``）：条目自带话题才写。

## ⚠️ 商品信息不进 notes 表（用户 2026-09-22 追加口径）

「笔记表不要再存商品信息，直接通过笔记和商品关联表进行查询，同步落库时也进行拆分，
将笔记与商品的关系落在笔记和商品关系表中」⇒ 本迁移**只加话题列**；挂载商品在
``shop_products``（商品信息单独一张表）、关系在 ``note_goods``（多对对关系表），
查询走 ``note_goods`` join ``shop_products``（``api/notes.py::_serialize_note`` 的
``goods`` 字段即该 join 的投影）。**不在笔记表冗余一份会漂的商品快照。**

## 存量回填

从 ``raw_json`` 里已有的 ``tag_list`` / ``tags`` / ``topics`` 尽力回填（采集落库的
行带着它们；同步链路的行话题只在详情里有，而详情归一的 tags 历史上没落库 ⇒ 那些行
回填为 ``[]``，等下次详情补齐写入）。**不造假**：解析不出就留 ``[]``。

## 可逆

``downgrade`` 删列：投影信息丢弃，``raw_json`` 证据链一行不受影响；运行期写入方
下次同步 / 补齐会重新投影（幂等）。
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

from backend.app.core.migration_introspect import column_names

revision = "s1t2u3v4w5x6"
down_revision = "r0s1t2u3v4w5"
branch_labels = None
depends_on = None

_TABLE = "notes"
_COLUMN = "topics_json"


def _has_column(conn, table: str, column: str) -> bool:
    #: v1.1.0 PG 化：原为 `PRAGMA table_info`（SQLite 专属），统一走内省助手。
    return column in column_names(conn, table)


def _topic_names(raw: object) -> list[str]:
    """从 `raw_json`（平台原文）里挖话题名 —— 与前端 `getNoteTags` 同一套候选键。

    只认字符串或 `{name}` 形状的条目；解析不出返回 `[]`（不造假）。
    """
    if not isinstance(raw, dict):
        return []
    for key in ("tag_list", "tags", "topics"):
        value = raw.get(key)
        if not isinstance(value, list) or not value:
            continue
        names: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
            elif isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name.strip():
                    names.append(name.strip())
        if names:
            # 去重保序（同一话题在平台响应里重复出现是常态）
            return list(dict.fromkeys(names))
    return []


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, _TABLE, _COLUMN):
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(
                sa.Column(_COLUMN, sa.Text(), server_default="[]", nullable=False)
            )

    # 存量回填（只做 topics；商品信息按用户口径走关系表，不落笔记列）
    rows = conn.execute(sa.text(f"SELECT id, raw_json FROM {_TABLE}")).fetchall()
    backfilled = 0
    for row_id, raw_text in rows:
        try:
            raw = json.loads(raw_text) if isinstance(raw_text, str) else None
        except (TypeError, json.JSONDecodeError):
            raw = None
        names = _topic_names(raw)
        if not names:
            continue
        conn.execute(
            sa.text(f"UPDATE {_TABLE} SET {_COLUMN} = :topics WHERE id = :id"),
            {"topics": json.dumps(names, ensure_ascii=False), "id": row_id},
        )
        backfilled += 1
    print(f"  [s1t2u3v4w5x6] topics backfilled: {backfilled}/{len(rows)} rows")  # noqa: T201


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn, _TABLE, _COLUMN):
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column(_COLUMN)
