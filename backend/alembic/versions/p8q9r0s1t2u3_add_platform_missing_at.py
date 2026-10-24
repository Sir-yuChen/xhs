"""add platform_missing_at (对账标记：平台列表里已没有这条数据)

2026-09-22 数据对账（设计 13 §24）。用户口径演变记录（原句保留，不静默删除）：

1. 「需要做数据对账，以三方数据为主」；
2. ~~「本地多得直接删除」~~ → **被下一条取代**；
3. 「本地多得数据不直接删除，打上状态」← **现行契约**；
4. 「对账，只对同步指定的时间区间内的数据，或指定的全量数据，根据同步条件来确定」
   ⇒ 对账范围与同步入口同源（`scope` / `start_date` / `end_date`）。

## 为什么用「可空时间列」而不是布尔或删除

* **不是布尔**：需要知道「什么时候发现它不见了」，否则用户无法判断这条标记
  是不是上一次对账的陈旧结论（平台可能后来又有了）。
* **不删行**：平台列表可能因筛选 / 分页 / 风控不完整，「本地多」很可能是
  **平台没返回完**而不是**平台没有**；删本地数据不可逆。标记是**自愈**的 ——
  同一轮对账里平台又返回了这条 ⇒ 立即清回 `NULL`，同步链路重新落库同一条时也清。

## 语义

`NULL` = 正常（平台有它，或还没对账过）；
非空 = 最近一次对账发现「本地有、平台无」的时间。

## 存量行置什么

**一律 NULL**，不做任何推断。存量数据没有对账过，就不能替它下「平台没有」
这个结论（缺值口径：不造假）。

## 可逆

`downgrade()` 删列：标记丢失，数据行本身不受影响。下一轮对账会重新打标记。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

from backend.app.core.migration_introspect import column_names

revision = "p8q9r0s1t2u3"
down_revision = "o7p8q9r0s1t2"
branch_labels = None
depends_on = None

_TABLES = ("notes", "shop_products")
_COLUMN = "platform_missing_at"


def _has_column(conn, table: str, column: str) -> bool:
    #: v1.1.0 PG 化：原为 `PRAGMA table_info`（SQLite 专属），统一走内省助手。
    return column in column_names(conn, table)


def upgrade() -> None:
    conn = op.get_bind()
    for table in _TABLES:
        if not _has_column(conn, table, _COLUMN):
            # 可空列，无需 server_default；存量行留 NULL = 未对账（见模块 docstring）。
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column(_COLUMN, sa.DateTime(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    for table in reversed(_TABLES):
        if _has_column(conn, table, _COLUMN):
            with op.batch_alter_table(table) as batch:
                batch.drop_column(_COLUMN)
