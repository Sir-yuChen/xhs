"""purge orphaned note_goods rows left by note deletion

2026-09-23 用户口径：

> 「删除笔记时需要删除笔记与商品的关联关系」

## 问题

`purge_note()`（内容库「唯一的删除链实现」）原先清标签 / 资产 / 评论 / 草稿引用，
**漏了 `note_goods` 关系行**（§26 的笔记↔商品多对多表）。两个外键都没开级联
（`PRAGMA foreign_keys` 未启用，SQLite 默认关），于是删掉笔记后关系行变成孤儿。

**真实库实测**：删掉 4 条本地生成笔记后留下 4 条孤儿行
（`note_id` 1963 / 1964 / 1965 / 1967 → `product_id` 1）。

## 影响面

它们**不参与任何列表口径**（`note_count` 走 `join(NoteGoods)`，孤儿行 join 不上
`notes` 自然被滤掉），所以不是「数字算错」那种可见故障；坏在**数据自相矛盾** ——
关系指向一条已经不存在的笔记，且每次 join 都要多扫这几行。

## 修法（两半，本迁移是其中一半）

1. **代码**：`purge_note()` 显式 `delete(NoteGoods).where(note_id == note.id)`；
2. **存量**（本迁移）：删掉已经存在的孤儿行。

⚠️ 只做一半 = 假修复：代码修好但存量孤儿永远留在库里。

## 口径（与「删商品」刻意相反，§33.10）

* **删商品**（本地台账行）⇒ 关系行**不删**：商品会被同步写回，关系要原样接上；
* **删笔记** ⇒ 关系行**必须删**：笔记是真删（`purge_note` 末尾 `db.delete(note)`），
  没有「写回时接上」这回事。

即「只增不删」（§23）管的是**同步链路不因平台变动而删**，不是「任何删除都豁免」。

## 可逆

`downgrade()` **无法恢复**被删的关系行（真值已丢，且原 note_id 对应的笔记也没了）。
刻意不写「假恢复」：留空并在 docstring 说明，比造一份对不上的数据诚实。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "w6x7y8z9a0b1"
down_revision = "v5w6x7y8z9a0"
branch_labels = None
depends_on = None

_TABLE = "note_goods"


def upgrade() -> None:
    """删掉指向不存在笔记的关系行（孤儿）。

    `NOT IN (SELECT id FROM notes)` 而不是 `LEFT JOIN ... WHERE notes.id IS NULL`：
    语义等价，但 `NOT IN` 在 SQLite 上对这两张表的量级更快，且不依赖 join 顺序。
    子查询里的 `id` 是 `notes` 主键，`NULL` 不可能出现 ⇒ 没有 `NOT IN` 遇 NULL
    导致「全不匹配」那个经典坑。
    """
    conn = op.get_bind()
    conn.execute(
        sa.text(
            f"DELETE FROM {_TABLE} WHERE note_id NOT IN (SELECT id FROM notes)"
        )
    )


def downgrade() -> None:
    """不可逆：被删的关系行是真值，且对应的笔记也已不存在。

    刻意不写「假恢复」—— 造一份对不上的数据比留空更糟。
    """
    return None
