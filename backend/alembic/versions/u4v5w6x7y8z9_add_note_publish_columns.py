"""add notes publish_status/publish_error/publish_attempts/published_at

2026-09-23 商品笔记智能生成与发布（设计 13 §31）。用户口径原文：

> 「生成的笔记存到内容库对应表中以及生成商品与笔记关系（此时笔记 ID 需项目自动
> 生成一个唯一 ID……），笔记状态为待发布……如果笔记发布失败则记录失败原因，
> 在内容库页的笔记状态修改为发布失败……」

## 语义（设计 13 §31.3.1）

四列构成**本地生成笔记**的发布状态机，只有 `note_type='local_generated'` 的笔记会写：

| 列 | 值 |
|---|---|
| `publish_status` | `''`（不适用，其余笔记恒空）/ `pending` / `publishing` / `published` / `failed` |
| `publish_error` | 最近一次失败原因（平台原文，DEF-058：不换通用文案）；成功清空 |
| `publish_attempts` | 发布尝试次数（含失败） |
| `published_at` | 三方发布成功时间（本地时区） |

⚠️ 与 `published`（§19.12，用户可手动切的工作流标识）是**两列**：发布成功时两个都置位，
其余链路谁也不碰谁。存量行一律 `''`/`0`/NULL —— 本迁移**不做数据回填**（本功能上线前
没有本地生成笔记，没有可归类对象；编造历史状态才是造假）。

## 可逆

`downgrade()` 删四列：状态机信息丢弃，笔记行本身不受影响。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "u4v5w6x7y8z9"
down_revision = "t3u4v5w6x7y8"
branch_labels = None
depends_on = None

_TABLE = "notes"
_COLUMNS = (
    ("publish_status", sa.String(length=16), ""),
    ("publish_error", sa.Text, ""),
)


def _has_column(conn, table: str, column: str) -> bool:
    rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, _TABLE, "publish_status"):
        # SQLite 加 NOT NULL 列必须带 DDL 默认值（与 ORM 侧 server_default 一致；
        # 裸 SQL 插入也不会炸）。
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(
                sa.Column("publish_status", sa.String(length=16), server_default="", nullable=False)
            )
    if not _has_column(conn, _TABLE, "publish_error"):
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(
                sa.Column("publish_error", sa.Text(), server_default="", nullable=False)
            )
    if not _has_column(conn, _TABLE, "publish_attempts"):
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(
                sa.Column("publish_attempts", sa.Integer(), server_default="0", nullable=False)
            )
    if not _has_column(conn, _TABLE, "published_at"):
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(sa.Column("published_at", sa.DateTime(), nullable=True))
    # `publish_status` 建索引：内容库「待发布 / 发布失败」筛选与批量发布候选集都按它查。
    indexes = {row[1] for row in conn.execute(sa.text("PRAGMA index_list(notes)")).fetchall()}
    if "ix_notes_publish_status" not in indexes:
        conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_notes_publish_status ON notes (publish_status)"))


def downgrade() -> None:
    conn = op.get_bind()
    for column in ("publish_status", "publish_error", "publish_attempts", "published_at"):
        if _has_column(conn, _TABLE, column):
            with op.batch_alter_table(_TABLE) as batch:
                batch.drop_column(column)
