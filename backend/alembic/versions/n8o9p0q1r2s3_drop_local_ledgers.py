"""设计 75 S5 收尾：丢弃三域本地台账（商品 / 订单 / 笔记↔商品关系 + 两张残留快照表）

用户口径（2026-10-26）：「不再同步这些数据到本地，转变为展示线上数据，实时查询线上
数据……避免数据不一致问题」「同步商品和同步笔记任务全链路移除」。

## 为什么是这一批表

| 表 | 依据（设计 75 §2.4） |
|---|---|
| `shop_products` | 商品列表 / 详情改实时 `search_items` / `get_skus_by_item_ids` |
| `orders` / `order_items` / `order_shipments` / `after_sales` | 订单改实时 `get_order_page` / `get_order_detail` |
| `note_goods` / `shop_note_goods` | 笔记↔商品挂链改实时 `goods-note/list?item_mix_search_word=` |

⚠️ **顺带两张**（不是遗漏，是必须同批，否则 `shop_products` 删不掉）：

* `shop_metric_snapshots`：`product_pk` **NOT NULL** FK → `shop_products.id`；
* `topic_goods_match`：`product_pk` NULL FK → `shop_products.id`。

两张表的采集子系统已在 2026-10-13「指标快照子系统整块移除」轮退场
（端点 / 定时任务 / 服务都删了），只剩表在库里 —— 它们是**已经死掉的表的残留**，
FK 会把 `DROP TABLE shop_products` 卡死。

## 外键反查（2026-10-27 真机库实测，**5 张**引用表，不是历史记的 4 张）

```
generation_records.shop_product_id      NO ACTION   → 保留表，本迁移**删外键 + 删列**
note_generation_links.shop_product_id   NO ACTION   → 保留表，本迁移**删外键 + 删列**
order_items.shop_product_id             NO ACTION   → 整表 DROP
shop_metric_snapshots.product_pk        NO ACTION   → 整表 DROP
topic_goods_match.product_pk            NO ACTION   → 整表 DROP
```

⚠️ **保留的两张表必须删列而不是留着列**：留着 `shop_product_id` 指向一张已删的表，
`create_all()` 与 alembic autogenerate 都会各说一套（模型说没有、库说有），
下次任何一次迁移都会莫名爆 FK 错。

⚠️ **数据影响（真机实测行数）**：`shop_products` 2020 / `note_goods` 2142 /
`shop_note_goods` 1977 / `orders` 3 / `order_items` 3 / `order_shipments` 3 /
`after_sales` 0 / `shop_metric_snapshots` 3972 / `topic_goods_match` 33；
两张保留表的 `shop_product_id` 非空行 = 47 / 44 —— **这些值随列一起删**
（它们只是本地自增主键，平台商品 ID 在 `generation_records.input_json`
与队列 payload 里另有留存，可人工追查，故不迁移到新列）。

⚠️ **本迁移不新建表**：`ai_title` / `ai_selling_point` 是全库 0 个非空值
（实测），随 `shop_products` 一起退场，不搬到别处 —— 搬一张空表只是换个地方留死代码。

## 为什么 downgrade 只建空表

原表数据已按用户口径丢弃（本地不再维护这些数据），回滚能恢复的是**结构**而非数据；
写「回填数据」在这里是做不到的（源就是被删的那张表）。⚠️ 因此本迁移的 downgrade
只保证 `alembic downgrade` 不报错、`upgrade` 可重跑幂等。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "n8o9p0q1r2s3"
down_revision = "m7n8o9p0q1r2"
branch_labels = None
depends_on = None

#: 整表 DROP（**按外键从属顺序**：先删引用方，再删被引用方）。
DROP_TABLES = (
    "after_sales",
    "order_shipments",
    "order_items",
    "orders",
    "shop_metric_snapshots",
    "topic_goods_match",
    "note_goods",
    "shop_note_goods",
    "shop_products",
)

#: 保留表上要拆掉的「指向 shop_products」外键列。
#: 顺序无依赖（都指同一个父表），但**必须在 `shop_products` 之前**处理。
DETACH_COLUMNS = (
    ("note_generation_links", "shop_product_id"),
    ("generation_records", "shop_product_id"),
)


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(sa.text("SELECT to_regclass(:t)"), {"t": table}).scalar()
    )


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": table},
    ).all()
    return {row[0] for row in rows}


def upgrade() -> None:
    conn = op.get_bind()

    # ① 拆掉两张**保留表**上指向 `shop_products` 的外键列。
    #    ⚠️ `batch_alter_table`：SQLite 不支持 DROP COLUMN 带外键（桌面冻结分支仍可能
    #    跑 SQLite），batch 形态两边都成立。
    for table, column in DETACH_COLUMNS:
        if not _table_exists(conn, table):
            continue
        if column not in _columns(conn, table):
            continue
        with op.batch_alter_table(table) as batch:
            batch.drop_column(column)

    # ② 整表 DROP（已按从属顺序排好；缺表跳过 —— 幂等）。
    for table in DROP_TABLES:
        if _table_exists(conn, table):
            op.drop_table(table)


def downgrade() -> None:
    """**只恢复结构**，不恢复数据（数据源就是被删的表，无法凭空还原）。"""
    conn = op.get_bind()

    if not _table_exists(conn, "shop_products"):
        op.create_table(
            "shop_products",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("platform_account_id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.String(64)),
            sa.Column("item_title", sa.String(512)),
            sa.Column("price", sa.String(32)),
            sa.Column("stock", sa.Integer()),
            sa.Column("sales", sa.Integer()),
            sa.Column("category", sa.String(128)),
            sa.Column("images_json", sa.Text()),
            sa.Column("status", sa.String(32)),
            sa.Column("synced_at", sa.DateTime()),
            sa.Column("platform_missing_at", sa.DateTime()),
            sa.Column("raw_json", sa.Text()),
            sa.Column("detail_json", sa.Text()),
            sa.Column("sku_count", sa.Integer()),
            sa.Column("detail_synced_at", sa.DateTime()),
            sa.Column("article_no", sa.String(64)),
            sa.Column("card_type", sa.Integer()),
            sa.Column("price_min_fen", sa.Integer()),
            sa.Column("price_max_fen", sa.Integer()),
            sa.Column("create_time_ms", sa.BigInteger()),
            sa.Column("on_shelf_time_ms", sa.BigInteger()),
            sa.Column("off_shelf_time_ms", sa.BigInteger()),
            sa.Column("last_audit_time_ms", sa.BigInteger()),
            sa.Column("sales_30d", sa.Integer()),
        )

    for table, column in reversed(DETACH_COLUMNS):
        if not _table_exists(conn, table):
            continue
        if column in _columns(conn, table):
            continue
        #: ⚠️ **不加回外键**：`shop_products` 是空表（downgrade 刚建），加 FK 只会让下次
        #: `upgrade` 再撞一次；老值本来也没了。
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column(column, sa.Integer(), nullable=True))
