"""add notes.hydrated_at (笔记详情补齐标记)

2026-09-22 修复「内容库笔记正文未展示、图片只有一张」（设计 13 §19.13）。

## 为什么需要这一列

三个源的**列表接口都不带正文**，图片也可能只有一张封面（千帆 `goods-note/list`
只给 `note_image`）。只有**详情接口**能同时给出正文与完整图集 —— 本机真机实测
（只读 GET 详情，账号 8）：`content` 481 字、`image_list` 4 张。

补齐动作**必须能跨轮累积**，否则修不动：增量同步的游标只拉新笔记，已经落库但
没补上的老行**永远不会再被尝试**（现网实测：95/101 行正文空，其中 70 行是当天
一次 ark 同步落的）。所以候选集合是「**该账号下 `hydrated_at IS NULL` 的行**」，
按 `id` 升序、本轮取有界条数 ⇒ 下一轮接着补，最终收敛。

同时这一列负责**防重复请求**：不限流额度只有 5 次/分钟（`CrawlRateLimiter`），
若每轮都对「正文为空」的行重试，额度会被长期浪费；而某条笔记的详情若**永远**
取不到（已删除、被限流挡死），没有标记就会**每轮重试到天黑**。

## 存量行置什么

**一律 NULL（= 未补齐）**，不做任何推断、不预先标记。依据：现网 101 行里 95 行
正文为空、71 行只有一张图，它们**确实**需要补 —— 置 NULL 让后续同步自然地把
它们补齐，不需要额外的数据修复脚本。

## 可逆

`downgrade()` 删列：补齐标记丢失，下一轮同步会重新补（补齐是幂等的，不依赖这列
的历史值）。**不删任何笔记数据**。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "o7p8q9r0s1t2"
down_revision = "n6o7p8q9r0s1"
branch_labels = None
depends_on = None

_TABLE = "notes"
_COLUMN = "hydrated_at"
_INDEX = "ix_notes_hydrated_at"


def _has_column(conn, table: str, column: str) -> bool:
    rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def _has_index(conn, index: str) -> bool:
    rows = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='index' AND name=:name"),
        {"name": index},
    ).fetchall()
    return bool(rows)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, _TABLE, _COLUMN):
        # 可空列，无需 server_default；存量行留 NULL = 未补齐（见模块 docstring）。
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(sa.Column(_COLUMN, sa.DateTime(), nullable=True))
    if not _has_index(conn, _INDEX):
        # 候选查询是 `WHERE platform_account_id = ? AND hydrated_at IS NULL ORDER BY id`
        # —— 这一列会随补齐推进越来越稀疏，建索引让筛选不必全表扫。
        with op.batch_alter_table(_TABLE) as batch:
            batch.create_index(_INDEX, [_COLUMN])


def downgrade() -> None:
    conn = op.get_bind()
    if _has_index(conn, _INDEX):
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_index(_INDEX)
    if _has_column(conn, _TABLE, _COLUMN):
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column(_COLUMN)
