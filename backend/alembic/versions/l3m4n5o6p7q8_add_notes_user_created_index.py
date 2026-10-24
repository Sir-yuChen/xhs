"""add composite index notes(user_id, created_at) (内容库列表排序下推)

2026-09-28，设计 13 §41.6。

## 为什么需要这个索引

`GET /api/notes` 的列表排序是 `ORDER BY created_at DESC, id DESC`（§41.5 加了稳定键），
筛选条件恒定含 `user_id = ?`。改前只有单列 `ix_notes_user_id`，SQLite 拿它做
`SEARCH notes USING INDEX ix_notes_user_id (user_id=?)` 之后**排不上序**，
执行计划里多一步 `USE TEMP B-TREE FOR ORDER BY` —— 把该用户**全部**命中行灌进临时
B 树再取前 N 条。

真库（3620 篇）实测该步骤 **6.7 ms**（`ORDER BY created_at DESC LIMIT 20`）。
建成本索引后同一条查询走 `SEARCH notes USING COVERING INDEX ix_notes_user_created
(user_id=?)`，排序步骤 **0.0 ms**（索引本身有序，取前 N 条即停）。

⚠️ 为什么现在才做：改造前这个排序**根本不是瓶颈**（瓶颈是「全量加载 + 逐篇 N+1」，
实测 5774 ms / 16390 条 SQL，见 §41.1）。下推分页后列表降到 15.5 ms，
**排序成了唯一随笔记数线性恶化的一环** —— 现在做它才有意义，也才量得出来。

## 代价与可逆

建索引实测 98 ms（一次性），磁盘增量远小于笔记表本身（111 MB）。
`downgrade()` 删索引：只影响查询计划，**不丢任何数据**，删后列表照常工作
（只是退回 TEMP B-TREE 排序）。

## 与既有索引的关系

不删 `ix_notes_user_id`：它的前缀就是本索引的前缀，但 `uq_notes_user_note`
`(user_id, note_id)` 与 `ix_notes_user_id` 仍在被别的查询（按 note_id 定位、
`GET /notes/ids`）使用。SQLite 的 `ANALYZE` 统计会自行择优，多留一个单列索引的
代价只是写入时多维护一棵小 B 树 —— 换「不改动既有查询计划」这个确定性。

⚠️ **本索引只存在于迁移、不写进模型**（`models/note.py::Note.__table_args__` 不加）——
这与 `ix_notes_hydrated_at`（迁移 `o7p8q9r0s1t2` 建，模型里也没声明）是**同一惯例**：
纯性能索引由迁移负责，模型只管列与业务唯一键。⇒ 测试库（`create_all`）里没有这个索引，
**不影响任何用例的正确性**（它是优化，不是正确性前提），但也就意味着
「测试里快」不能证明「线上快」—— 线上性能判据仍要打真库（见设计 13 §41.1）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

from backend.app.core.migration_introspect import index_exists

revision = "l3m4n5o6p7q8"
down_revision = "k2l3m4n5o6p7"
branch_labels = None
depends_on = None

_TABLE = "notes"
_INDEX = "ix_notes_user_created"
_COLUMNS = ["user_id", "created_at"]


def _has_index(conn, index: str) -> bool:
    #: v1.1.0 PG 化：原为 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return index_exists(conn, index)


def upgrade() -> None:
    conn = op.get_bind()
    if _has_index(conn, _INDEX):
        return
    op.create_index(_INDEX, _TABLE, _COLUMNS)
    # 让优化器立刻用上新索引的统计（否则可能要等下次 ANALYZE 才改计划）。
    conn.execute(sa.text("ANALYZE"))


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_index(conn, _INDEX):
        return
    op.drop_index(_INDEX, table_name=_TABLE)
    conn.execute(sa.text("ANALYZE"))
