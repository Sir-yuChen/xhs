"""drop local ledgers for goods / orders / note-goods links（设计 75 §2.4，去本地化实时查询 S5）

三个域（商品 / 订单 / 笔记↔商品关系）改**平台实时查询**后，本地副本没有任何数据消费者 ——
反而只会在两边不同步时骗人（用户口径 2026-10-26：「不再同步这些数据到本地，转变为展示
线上数据，实时查询线上数据……避免数据不一致问题」）。

## 本迁移 DROP 的 9 张表

| 表 | 原用途 | 现在的数据源 |
|---|---|---|
| `shop_products` | 商品台账 | `rt.list_products` / `rt.get_product_detail` |
| `note_goods` | 笔记↔商品（内容库侧） | `rt.list_goods_notes(note_ids=…)` |
| `shop_note_goods` | 笔记↔商品（千帆侧映射） | 同上 |
| `orders` | 订单 | `rt.list_orders` |
| `order_items` | 订单商品行 | 同上（`skus[]` 内嵌） |
| `order_shipments` | 物流行 | `rt.get_order_detail` 的 `shipments[]` |
| `after_sales` | 售后单 | 同上 `after_sale_list[]` |
| `shop_metric_snapshots` | 商品指标日快照 | 平台实时计数 |
| `topic_goods_match` | 热点↔商品匹配 | 实时匹配（`hot_topic_service`） |

## 保留的表里指向 `shop_products` 的外键

`generation_records` 与 `note_generation_links` 原用 `shop_product_id`（本地主键）。
台账没了这个外键必然悬空 ⇒ 同批改成**平台 `item_id`**（普通列，无外键），
并重建两个带 `shop_product_id` 的索引 / 唯一约束。

⚠️ **不改这两张表的话 metadata 直接 `NoReferencedTableError`**（应用起不来），
所以「DROP 表」与「拆外键」必须在同一个迁移里、且**先拆后删**。

⚠️ **DROP 顺序**：先子表后父表。`after_sales` / `order_items` / `order_shipments`
都引用 `orders` ⇒ 三个子表先删。

Revision ID: n8o9p0q1r2s3
Revises: m7n8o9p0q1r2
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa

revision = "n8o9p0q1r2s3"
down_revision = "m7n8o9p0q1r2"
branch_labels = None
depends_on = None


#: 按依赖顺序排列（子表在前）。**DROP TABLE IF EXISTS** —— 存量库可能早已手工清过。
_DROP_ORDER = (
    "topic_goods_match",
    "shop_metric_snapshots",
    "after_sales",
    "order_shipments",
    "order_items",
    "orders",
    "note_goods",
    "shop_note_goods",
    "shop_products",
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    """表的现有列名（**先看表在不在** —— 存量库可能早已手工删过列）。"""
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _index_names(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(table)}


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # ① 保留表：`shop_product_id` → `item_id`（必须先松外键，否则删父表会失败）
    # ------------------------------------------------------------------ #
    generation_columns = _columns("generation_records")
    if "shop_product_id" in generation_columns and "item_id" not in generation_columns:
        op.add_column("generation_records", sa.Column("item_id", sa.String(length=64), nullable=True))
        #: 存量回填：台账表此时**还在**（本函数末尾才删），能 join 回填。
        #: 台账里没有对应行（商品早被删）⇒ 留 NULL，**不造值**（缺值口径）。
        if "shop_products" in _tables():
            op.execute(
                sa.text(
                    """
                    UPDATE generation_records AS g
                       SET item_id = p.item_id
                      FROM shop_products AS p
                     WHERE p.id = g.shop_product_id
                    """
                )
            )
    #: 带 `shop_product_id` 的两个索引 / 唯一约束改成 `item_id` 版本。
    #: `batch_alter_table` 保证 SQLite 也能重建（它不支持裸 ALTER + 约束）。
    generation_indexes = _index_names("generation_records")
    with op.batch_alter_table("generation_records") as batch:
        for name in ("uq_generation_records_adopted", "ix_generation_records_product_version"):
            if name in generation_indexes:
                batch.drop_index(name)
        if "shop_product_id" in _columns("generation_records"):
            batch.drop_column("shop_product_id")
        if "ix_generation_records_item_version" not in generation_indexes:
            batch.create_index("ix_generation_records_item_version", ["item_id"], unique=False)
        if "uq_generation_records_adopted" not in generation_indexes:
            batch.create_index(
                "uq_generation_records_adopted",
                ["item_id", "is_adopted"],
                unique=True,
                postgresql_where=sa.text("is_adopted = true"),
            )

    link_columns = _columns("note_generation_links")
    if "shop_product_id" in link_columns:
        with op.batch_alter_table("note_generation_links") as batch:
            if "item_id" not in link_columns:
                batch.add_column(sa.Column("item_id", sa.String(length=64), nullable=True))
            batch.drop_column("shop_product_id")

    # ------------------------------------------------------------------ #
    # ② 9 张台账表整表 DROP（子表在前）
    # ------------------------------------------------------------------ #
    for table in _DROP_ORDER:
        op.execute(sa.text(f'DROP TABLE IF EXISTS "{table}"'))


def downgrade() -> None:
    """**表不重建**（数据源已是平台，重建出来的空表只会更难点查）。

    只把两张保留表的列名改回去，好让 `alembic downgrade` 不至于在 `ALTER` 上炸。
    """
    generation_columns = _columns("generation_records")
    if "item_id" in generation_columns and "shop_product_id" not in generation_columns:
        with op.batch_alter_table("generation_records") as batch:
            batch.add_column(sa.Column("shop_product_id", sa.Integer(), nullable=True))
            batch.drop_column("item_id")
    link_columns = _columns("note_generation_links")
    if "item_id" in link_columns and "shop_product_id" not in link_columns:
        with op.batch_alter_table("note_generation_links") as batch:
            batch.add_column(sa.Column("shop_product_id", sa.Integer(), nullable=True))
            batch.drop_column("item_id")
