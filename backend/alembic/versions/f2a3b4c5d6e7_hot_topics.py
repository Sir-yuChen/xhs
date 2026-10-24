"""热点驱动生成的表（设计 17 §13.11，M3 方向 2，2026-10-04）

| 操作 | 对象 |
|---|---|
| **建表** | `hot_topics` —— 平台**按商品推荐**的热度话题快照 |
| **建表** | `topic_goods_match` —— 话题↔商品匹配（含可解释成分）|

## 数据源真机结论（决定了表形状，勿按想象改）

`POST /api/edith/aigc-publish/attributes`（**只读**）返回
`data.topic_list.topic_list_info[]`：`{"topic": "餐垫", "type": 1, "hot_value": 4673}`。

⚠️ 该接口**按 `item_id` 调用** ⇒ 话题是「**与该商品相关**的平台热度词」，
**不是全站热搜总榜** ⇒ `hot_topics` 唯一键必须含 `item_id`。

⚠️ `type=2` 是活动话题，**平台不给 `hot_value`** ⇒ 落 `0`（不猜热度）。

⚠️ **`hot_value` 存平台原值，不归一化**：归一化后用户无法与平台后台对账。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from backend.app.core.migration_introspect import table_exists

# revision identifiers, used by Alembic.
revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None

_HOT = "hot_topics"
_MATCH = "topic_goods_match"


def _table_exists(conn, table: str) -> bool:
    #: v1.1.0 PG 化：原为 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return table_exists(conn, table)


def upgrade() -> None:
    conn = op.get_bind()

    # ⚠️ **幂等：表已存在则整段跳过**（与 `c9d0e1f2a3b4` / `e1f2a3b4c5d6` 同一判据）。
    # 「stamp 回早期 revision 再 upgrade」是本项目的正常用法（迁移用例就这么跑），
    # 而那些夹具库由 `Base.metadata.create_all()` 建 ⇒ 表**已经在了**。
    if _table_exists(conn, _HOT) and _table_exists(conn, _MATCH):
        return

    if not _table_exists(conn, _HOT):
        op.create_table(
            _HOT,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("item_id", sa.String(length=64), nullable=False),
            sa.Column("item_title", sa.String(length=512), nullable=False, server_default=""),
            sa.Column("topic", sa.String(length=128), nullable=False),
            #: 平台 `type`：1 = 热度词 / 2 = 活动话题。
            sa.Column("topic_type", sa.Integer(), nullable=False, server_default="1"),
            #: 平台热度**原值**（type=2 / 缺字段 ⇒ 0）。
            sa.Column("hot_value", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "platform_account_id",
                sa.Integer(),
                sa.ForeignKey("platform_accounts.id"),
                nullable=True,
            ),
            sa.Column("stat_date", sa.String(length=10), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            # ⚠️ 同日同商品同话题只一行（覆盖，不追加）—— 追加会让热榜每次刷新翻倍。
            sa.UniqueConstraint(
                "user_id", "item_id", "topic", "stat_date", name="uq_hot_topic_item_topic_date"
            ),
        )
        op.create_index(f"ix_{_HOT}_user_id", _HOT, ["user_id"])
        op.create_index(f"ix_{_HOT}_item_id", _HOT, ["item_id"])
        op.create_index(f"ix_{_HOT}_topic", _HOT, ["topic"])
        op.create_index(f"ix_{_HOT}_stat_date", _HOT, ["stat_date"])

    if not _table_exists(conn, _MATCH):
        op.create_table(
            _MATCH,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("item_id", sa.String(length=64), nullable=False),
            sa.Column(
                "product_pk", sa.Integer(), sa.ForeignKey("shop_products.id"), nullable=True
            ),
            sa.Column("topic", sa.String(length=128), nullable=False),
            #: 综合相关度（可解释复合分，不是概率）。
            sa.Column("relevance_score", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("hot_value", sa.Integer(), nullable=False, server_default="0"),
            #: 可解释成分（命中字段）—— 排序质疑时的唯一依据。
            sa.Column("match_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("stat_date", sa.String(length=10), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "user_id", "topic", "item_id", "stat_date", name="uq_topic_goods_match"
            ),
        )
        op.create_index(f"ix_{_MATCH}_user_id", _MATCH, ["user_id"])
        op.create_index(f"ix_{_MATCH}_item_id", _MATCH, ["item_id"])
        op.create_index(f"ix_{_MATCH}_topic", _MATCH, ["topic"])
        op.create_index(f"ix_{_MATCH}_stat_date", _MATCH, ["stat_date"])


def downgrade() -> None:
    conn = op.get_bind()
    for table in (_MATCH, _HOT):
        if not _table_exists(conn, table):
            continue
        for index in (
            f"ix_{table}_user_id",
            f"ix_{table}_item_id",
            f"ix_{table}_topic",
            f"ix_{table}_stat_date",
        ):
            op.drop_index(index, table_name=table)
        op.drop_table(table)
