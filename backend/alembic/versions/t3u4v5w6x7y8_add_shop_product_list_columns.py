"""add shop_products list query columns (goods list local read, design 13 §30)

2026-09-22 商品列表本地化改造（设计 13 §30.3）：`POST /xhs/qianfan/items`
从「实时打千帆」改为「读本地 `shop_products` 台账」。展示字段由 `raw_json`
现场派生（契约不变），但**筛选 / 排序必须落在列上** —— WHERE 认列不认
`raw_json`，读取时兜底等于把同一语义写成两份实现（DEF-044 同型）。

九列（老行取默认值，语义上即「待回填」）：
* `article_no`        String(64) default ""   货号（关键词筛选 + 展示）
* `card_type`         Integer nullable        平台状态枚举（状态筛选；`status`
                                             列仍是展示标签，两者不互推）
* `price_min_fen` / `price_max_fen`  Integer nullable   价格区间筛选（分）
* `create_time_ms`    Integer nullable        创建时间筛选 + 默认排序（毫秒）
* `on_shelf_time_ms` / `off_shelf_time_ms` /
  `last_audit_time_ms`                        Integer nullable   排序（毫秒）
* `sales_30d`         Integer nullable        近 30 天销量（item_sale_qty_30 排序）

老行的回填**不需要迁移里做**：同步商品任务的「字段补足 pass」从 `raw_json`
本地派生回填（设计 13 §30.3.4，零平台请求）—— 迁移只负责结构。

可逆：降级删九列（数据随之丢弃，可由下一轮同步 / 回填重新生成）。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

from backend.app.core.migration_introspect import column_names

revision = "t3u4v5w6x7y8"
down_revision = "s1t2u3v4w5x6"
branch_labels = None
depends_on = None

_TABLE = "shop_products"
#: ⚠️ `article_no` 是 NOT NULL ⇒ 必须给 **server_default**（SQLite 的
#: `ADD COLUMN ... NOT NULL` 不认 SQLAlchemy 的客户端 `default=`，非空表上
#: 直接报 "Cannot add a NOT NULL column with default value NULL"）。
#: 其余八列 nullable，不需要默认值。
_COLUMNS = (
    ("article_no", sa.String(64), {"server_default": "", "nullable": False}),
    ("card_type", sa.Integer(), {"nullable": True}),
    ("price_min_fen", sa.Integer(), {"nullable": True}),
    ("price_max_fen", sa.Integer(), {"nullable": True}),
    ("create_time_ms", sa.Integer(), {"nullable": True}),
    ("on_shelf_time_ms", sa.Integer(), {"nullable": True}),
    ("off_shelf_time_ms", sa.Integer(), {"nullable": True}),
    ("last_audit_time_ms", sa.Integer(), {"nullable": True}),
    ("sales_30d", sa.Integer(), {"nullable": True}),
)


def _has_column(conn, table: str, column: str) -> bool:
    #: v1.1.0 PG 化：原为 `PRAGMA table_info`（SQLite 专属），统一走内省助手。
    return column in column_names(conn, table)


def upgrade() -> None:
    conn = op.get_bind()
    for name, type_, kwargs in _COLUMNS:
        if _has_column(conn, _TABLE, name):
            continue  # 幂等：重跑不重复加列
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(sa.Column(name, type_, **kwargs))


def downgrade() -> None:
    conn = op.get_bind()
    for name, _type, _kwargs in reversed(_COLUMNS):
        if not _has_column(conn, _TABLE, name):
            continue
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column(name)
