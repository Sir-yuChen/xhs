"""add note_goods (笔记 ↔ 商品多对多关系表)

2026-09-22 用户口径（设计 13 §26）：「笔记可能会挂载商品，同步笔记时如果存在笔记挂载
得商品，则应与本地商品信息表中对应商品建立关系表……一个商品下可能存在多个笔记，
笔记与商品之间得关系均是多对多，新增笔记商品关系表」＋「笔记相关得信息单独一张表，
商品信息单独一张表，笔记与商品之间得关系单独一张关系表」。

## 三张表的分工

| 表 | 装什么 | 键 |
|---|---|---|
| `notes`（既有） | 笔记信息 | 本地 PK `id`；平台身份 `(user_id, note_id)` |
| `shop_products`（既有） | 商品信息 | 本地 PK `id`；平台身份 `(shop_id_fk, item_id)` |
| `note_goods`（本迁移） | **纯关系行**，不存任何业务字段 | `(note_id, product_id)` 唯一 |

M2M 由唯一键 `(note_id, product_id)` 强制：一篇笔记挂多个商品、一个商品被多篇笔记
挂载，两种方向都是多行；重复解析幂等（同步链路重复落同一条关系不报错、不重复）。

## 与 `shop_note_goods` 的分工（易混）

* `shop_note_goods` = **平台证据缓存**：千帆说「这条平台笔记挂了什么」的原文，
  键是 `(platform_account_id, 平台 note_id)`；
* `note_goods` = **本地实体关系**：本地这篇笔记 ↔ 本地这个商品，键是
  `(notes.id, shop_products.id)`。

两者不是同一语义的两份实现：解析器（`shop_goods_service.link_note_goods`）只读缓存、
只写本表，**零平台请求**。任一侧本地行缺失时跳过（不造假），由后续同步自然补上。

## 存量 / 可逆

新表无存量数据（关系由解析器在同步时建立）。`downgrade()` 删表：关系行丢弃，
`notes` / `shop_products` 一行不受影响；下一轮同步会重新解析（幂等）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "r0s1t2u3v4w5"
down_revision = "q9r0s1t2u3v4"
branch_labels = None
depends_on = None

_TABLE = "note_goods"


def _has_table(conn, table: str) -> bool:
    rows = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table},
    ).fetchall()
    return bool(rows)


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _TABLE):
        return
    # 显式建表（不依赖 metadata 反射）：SQLite 下 batch_alter_table 的
    # create_table 语义与直接 DDL 等价，但这里是一次性建新表，直接写更直观。
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("note_id", sa.Integer(), sa.ForeignKey("notes.id"), nullable=False, index=True),
        sa.Column(
            "product_id", sa.Integer(), sa.ForeignKey("shop_products.id"), nullable=False, index=True
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("note_id", "product_id", name="uq_note_goods_note_product"),
    )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _TABLE):
        op.drop_table(_TABLE)
