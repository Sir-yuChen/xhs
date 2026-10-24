"""商品指标日快照表（设计 17 §13.9，M2 数据仪表盘切片 2）

| 操作 | 对象 |
|---|---|
| **建表** | `shop_metric_snapshots` —— 商品侧指标的**日快照**（销量/库存/价格/加购） |

## 为什么与 `note_metric_snapshots` 分表而不合表

两者的**主键语义不同**（笔记按 `note_pk`，商品按 `product_pk`），且指标集合**零重叠**
（笔记是内容侧的曝光/互动，商品是交易侧的销量/库存/价格）。合表会让每一列对另一半
行恒为 0 —— 那正是「用 0 冒充缺值」，违反缺值口径（硬约束⑤）。

⇒ 两张**同构**的快照表（`(实体主键, stat_date)` 唯一键 + upsert + 存快照值不存增量），
每一条判据与 §13.5 对 `note_metric_snapshots` 的写法**逐字一致**。

## 列判据

| 列 | 语义 | 说明 |
|---|---|---|
| `user_id` | 归属用户 | **租户隔离**（趋势查询一律按它收口） |
| `product_pk` | `shop_products.id` | ⚠️ **不用平台 `item_id`**（平台 ID 会变，本地主键稳定） |
| `stat_date` | `YYYY-MM-DD` | 字符串：字典序即时间序，无时区歧义 |
| `stock` / `sales` / `sales_30d` | 库存 / 销量 / 近 30 天销量 | 解析不到 ⇒ `0`（缺值口径） |
| `price_min_fen` / `price_max_fen` | 价格区间（**分**） | ⚠️ **直接复用 `shop_products` 现成的分列**，不自己解析 `price` 字符串 |
| `cart_count` | 加购数 | 平台侧当前无该指标 ⇒ 恒 `0`（列留位，**不造假**） |

⚠️ **价格用「分」存整数**（真机实测）：`shop_products` 已有 `price_min_fen` /
`price_max_fen` 两个 INTEGER 列（1982/1983 行都有值），**不需要**解析 `price`
那个 `"10680~55977"` 形态的字符串 —— 也不该解析（字符串格式是平台的展示形态，
会变；分列是同步时就归一化好的稳定口径）。浮点聚合 `19.9 + 0.1 != 20.0`，
金额一律整数分。

## 不回填历史

与切片 1 同口径：`shop_products` 只有「最后一次同步的值」，拿它当「历史某天」
就是**编造数据**（硬约束⑤）。表从空表开始，由采集逐日沉淀。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from backend.app.core.migration_introspect import table_exists

# revision identifiers, used by Alembic.
revision = "e1f2a3b4c5d6"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None

_TABLE = "shop_metric_snapshots"


def _table_exists(conn, table: str) -> bool:
    #: v1.1.0 PG 化：原为 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return table_exists(conn, table)


def upgrade() -> None:
    conn = op.get_bind()

    # ⚠️ **幂等：表已存在则整段跳过**（与切片 1 的 `c9d0e1f2a3b4` 同一判据）。
    # 「stamp 回早期 revision 再 upgrade」是本项目的**正常用法**（迁移用例就这么跑），
    # 而那些夹具库是用 `Base.metadata.create_all()` 建的 —— 表**已经在了**。
    # 不跳过 ⇒ `table ... already exists` 直接炸（实测：`test_dedupe_seeded_scheduled_jobs_migration`
    # 报 `OperationalError: table shop_metric_snapshots already exists`）。
    if _table_exists(conn, _TABLE):
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "product_pk", sa.Integer(), sa.ForeignKey("shop_products.id"), nullable=False
        ),
        # 字符串日期（非 Date 类型）：字典序即时间序，且无 SQLite 的时区/格式歧义。
        sa.Column("stat_date", sa.String(length=10), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sales", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sales_30d", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("price_min_fen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("price_max_fen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cart_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        f"ix_{_TABLE}_user_id", _TABLE, ["user_id"], unique=False
    )
    op.create_index(
        f"ix_{_TABLE}_stat_date", _TABLE, ["stat_date"], unique=False
    )
    # ⚠️ 唯一键 (product_pk, stat_date)：一天一行，重复采集**覆盖**（upsert），
    # 不是追加 —— 追加会把趋势线画成锯齿（同 §13.5 的笔记表判据）。
    op.create_index(
        f"ux_{_TABLE}_product_date",
        _TABLE,
        ["product_pk", "stat_date"],
        unique=True,
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    op.drop_index(f"ux_{_TABLE}_product_date", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_stat_date", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_user_id", table_name=_TABLE)
    op.drop_table(_TABLE)
