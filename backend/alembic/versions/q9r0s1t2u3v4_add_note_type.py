"""add notes.note_type (笔记类型：三方同步 / 本地)

2026-09-22 内容库「同步」按钮的前置列（设计 13 §25）。用户口径原文：

> 「再笔记表新增笔记类型，三方同步笔记、本地笔记两种，定时任务落库得笔记为三方
> 同步笔记类型，再内容库页如果为三方同步类型得笔记，详情卡新增同步按钮……」

## 语义

| 值 | 含义 | 写入方 |
|---|---|---|
| `synced` | 三方同步笔记 | 同步链路：`note_sync_service._sync_source`（creator / pc 源）、`_merge_ark_notes`（ark 源）、`reconcile_service` 补落库 |
| `local` | 本地笔记 | 采集（`crawl.py` ×3）、监控采集（`monitoring_crawl_service`）、`notes.py::batch_save_notes` |

判据是**落库链路**，与 `published`（§19.12）同一把尺子：不按触发方式判（手动与定时
走同一条链路）、不按 `raw_json` 反推（反推 = 造第二份真相源）。**只升不降**：同步命中
既有行升 `synced`；采集命中既有行不碰。

## 存量行为什么按 `published` 回填

本次变更前 `notes` 只有两条写入路径：**采集保存**与**笔记同步**。§19.12 已文档化的
不变量：**同步链路是 `published` 的唯一置真方，采集侧不碰既有行** ⇒
`published = 1 ⟺ 该行由同步链路落库`。所以存量回填是一**次性数据归类**，依据是既有
契约，不是新猜的判据：

```sql
UPDATE notes SET note_type = 'synced' WHERE published = 1;
```

⚠️ 与迁移 `n6o7p8q9r0s1`（published 存量一律置 True，依据是**本机实测**）不同：
本迁移的依据是**契约级不变量**，对任何部署都成立。

## 可逆

`downgrade()` 删列：类型信息丢弃，数据行本身不受影响；下一轮同步会按链路重新写入
（同步是幂等的，不依赖这列的历史值）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

from backend.app.core.migration_introspect import column_names

revision = "q9r0s1t2u3v4"
down_revision = "p8q9r0s1t2u3"
branch_labels = None
depends_on = None

_TABLE = "notes"
_COLUMN = "note_type"


def _has_column(conn, table: str, column: str) -> bool:
    #: v1.1.0 PG 化：原为 `PRAGMA table_info`（SQLite 专属），统一走内省助手。
    return column in column_names(conn, table)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, _TABLE, _COLUMN):
        # SQLite 加 NOT NULL 列必须带 DDL 默认值（否则重建表时报
        # "Cannot add a NOT NULL column with default value NULL"）。ORM 侧另有
        # `default="local"`，两者一致；裸 SQL 插入也不会炸。
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(
                sa.Column(_COLUMN, sa.String(length=16), server_default="local", nullable=False)
            )
    # 存量回填：`published=1` 的行 = 同步链路落库（§19.12 不变量，见模块 docstring）。
    # 幂等 —— 重复执行写入同一个值。
    # v1.1.0 PG 化：`published` 是布尔列，比较写 TRUE（`= 1` 是 SQLite 写法，PG 报错）。
    conn.execute(sa.text(f"UPDATE {_TABLE} SET {_COLUMN} = 'synced' WHERE published = TRUE"))


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn, _TABLE, _COLUMN):
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column(_COLUMN)
