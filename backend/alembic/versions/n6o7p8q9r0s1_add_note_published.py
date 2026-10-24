"""add notes.published (内容库「是否已发布过」标识)

2026-09-22 内容库发布状态工作流（设计 13 §19.12 / 设计 03 §4.1）：`notes` 加一列
布尔 `published`，把「**已经在平台上发过的作品**」与「**待发布的素材**」分开。

## 判据是链路，不是触发方式

列的值由**落库链路**决定，不在读取侧猜（猜 = 造第二份真相源）：

| 链路 | 写入点 | published |
|---|---|---|
| 笔记同步（www / creator / ark 三源） | `note_sync_service` → `_save_normalized_notes(published=True)` / `_merge_ark_notes` | `True` |
| 采集（发现页搜索 / URL / 详情落库） | `crawl.py` ×3（默认参数） | `False` |
| 监控采集 | `monitoring_crawl_service`（默认参数） | `False` |
| 采集结果批量入库 | `notes.py::batch_save_notes`（ORM 默认） | `False` |

**「只升不降」**：`_save_normalized_notes` 的 `published=True` 会无条件把既有行置 `True`，
而默认 `False` **只作用于新建行**。否则「先同步（已发布）→ 事后在发现页又采集到同一条」
会把「已发布」降级成「待发布」，用户看到的是「同步过的笔记自己变回了待发布」。

## 存量行为什么置 True（依据 + 可回退）

本次变更前 `notes` 只有两条写入路径：**采集保存**与**笔记同步**。本机实测
`data/spider_xhs.db` 的 `notes` **101/101 行**均由同步链路产生
（`platform_account_id` 全部指向同一个创作者账号，无采集保存的存量行），故存量
按「已发布过」起。

⚠️ 该依据来自**本机实测**，不是普遍事实。若某部署下混有采集保存的存量行，两种回退
方式都可以（不删数据、不换 id）：

```sql
-- ① 逐条：在内容库列表/详情里把该条切成「待发布」
-- ② 一次性：按 id 批量回退
UPDATE notes SET published = 0 WHERE id IN (...);
```

**可逆**：`downgrade()` 删列，该列的判定结果随之丢弃；下一次同步会重新写入
（同步是幂等的，不依赖这列的历史值）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

from backend.app.core.migration_introspect import column_names, index_exists

revision = "n6o7p8q9r0s1"
down_revision = "m5n6o7p8q9r0"
branch_labels = None
depends_on = None

_TABLE = "notes"
_COLUMN = "published"
_INDEX = "ix_notes_published"


def _has_column(conn, table: str, column: str) -> bool:
    #: v1.1.0 PG 化：原为 `PRAGMA table_info`（SQLite 专属），统一走内省助手。
    return column in column_names(conn, table)


def _has_index(conn, index: str) -> bool:
    #: v1.1.0 PG 化：原为 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return index_exists(conn, index)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, _TABLE, _COLUMN):
        # `server_default=sa.false()` 是 SQLite 加 NOT NULL 列的必要条件（否则重建表时报
        # "Cannot add a NOT NULL column with default value NULL"）。ORM 侧另有
        # `default=False`，两者一致；此处保留 DDL 默认值，裸 SQL 插入也不会炸。
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(
                sa.Column(_COLUMN, sa.Boolean(), server_default=sa.false(), nullable=False)
            )
        # 存量行置 True：依据见模块 docstring（本机实测 101/101 来自同步链路）。
        # v1.1.0 PG 化：布尔列写 TRUE（`= 1` 是 SQLite 无类型时代的写法，PG 报错）。
        conn.execute(sa.text(f"UPDATE {_TABLE} SET {_COLUMN} = TRUE"))
    if not _has_index(conn, _INDEX):
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
