"""add shop_products detail columns (sku detail backfill, design 13 §19)

2026-09-21 账号矩阵优化（设计 13 §19.4.2）：同步商品从「列表字段」扩展到
「SKU 级明细」。真机调研结论：商品编辑页加载既有商品走的
`product/publish_render` 必须带 `X-s`/`X-t`/`X-S-Common` 签名（无签名 406，
实测四组对照），复现签名违反只读边界 / C4；改用**无签名**的
`product/get_skus_by_item_ids`（批量上限 10，实测 20 个被服务端 500 挡回）
取 SKU 明细，归一化后落 `shop_products.detail_json`。

三列（老行取默认值，语义上就是「待补」）：
* `detail_json`       Text     default ''   —— 空串 = 没补过详情
* `sku_count`         Integer  nullable     —— 缺值不造 0
* `detail_synced_at`  DateTime nullable     —— NULL = 待补队列

可逆：降级删三列（数据随之丢弃，详情可由下一轮同步重新补齐）。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "k4m5n6o7p8q9"
down_revision = "j3k4l5m6n7o8"
branch_labels = None
depends_on = None

_TABLE = "shop_products"
_COLUMNS = (
    ("detail_json", sa.Text(), {"default": "", "nullable": False}),
    ("sku_count", sa.Integer(), {"nullable": True}),
    ("detail_synced_at", sa.DateTime(), {"nullable": True}),
)


def _has_column(conn, table: str, column: str) -> bool:
    rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


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
