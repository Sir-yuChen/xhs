"""商品分组 + 商品台账加列（设计 20 §1，M4 方向 4）

| 操作 | 对象 |
|---|---|
| **建表** | `product_groups` —— 商品的**本地分组**（用户自建，纯本地组织方式） |
| **加列** | `shop_products` + `group_id` / `ai_title` / `ai_selling_point` |

## 为什么 `product_groups` 挂 `user_id` 而不是 `platform_account_id`

分组是**用户的组织方式**，不是账号的属性 —— 用户可能把几个账号的商品放进同一个
「主推」分组里跨账号比较。商品侧的归属仍由 `shop_products.platform_account_id`
保证（不可越权）；分组只按 `user_id` 隔离。

## 为什么不给 `shop_products.group_id` 加 FK

SQLite 的 `ADD COLUMN` **不支持**加带 FK 的列（要表重建，代价大且迁移链复杂）。
改为**删分组时服务端显式清空**（`product_groups_service.delete_group`），
「清空」是**契约的一部分**（响应里报告影响面），比 FK 的 `ON DELETE SET NULL`
的隐式行为更可审计。服务层与 `delete_items` 的「关系行跟着走」是同一条纪律。

## 幂等（与 `e1f2a3b4c5d6` / `c9d0e1f2a3b4` 同一判据）

本项目「stamp 回早期 revision 再 upgrade」是**正常用法**（迁移用例就这么跑），
而那些夹具库是用 `Base.metadata.create_all()` 建的 —— 表 / 列**已经在了**。
不判存在就执行：

* `create_table` ⇒ `table ... already exists` 直接炸；
* `ADD COLUMN` ⇒ 加出 `group_id` / `group_id_2` 两列（SQLite 的 ADD COLUMN
  **本身不幂等**，重复执行就多一列，且名字带序号、不报错 —— 最隐蔽的一类迁移事故）。

⇒ 两件事都先查存在性（`sqlite_master` / `PRAGMA table_info`），存在就整段跳过。

## 加列必须 nullable

SQLite 的 `ADD COLUMN` 不允许 `NOT NULL` 且无 `server_default`。三列本来就要求
nullable（`NULL` = 未分组 / 没采纳过，见设计 20 §1.2），天然合规，**不需要回填**。

## `ai_title` / `ai_selling_point` 的 NULL 语义

**NULL = 没采纳过**，空串 = 采纳了但模型没返回内容。这是两个不同的界面状态
（「去润色」按钮 vs「已润色，内容为空」），用一列两种值区分（缺值口径，硬约束⑤）。
润色的**过程**不落这两列 —— 润色端点只回备选，用户**采纳**才写（设计 20 §3.1）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.app.core.migration_introspect import column_names, table_exists

revision = "g0a1b2c3d4e5"
down_revision = "f2a3b4c5d6e7"

_TABLE = "product_groups"
_TARGET = "shop_products"

_COLUMNS = (
    sa.Column("group_id", sa.Integer(), nullable=True),
    sa.Column("ai_title", sa.String(length=512), nullable=True),
    sa.Column("ai_selling_point", sa.Text(), nullable=True),
)


def _table_exists(conn: object) -> bool:
    #: v1.1.0 PG 化：原为 `sqlite_master` 查询（SQLite 专属），统一走内省助手。
    return table_exists(conn, _TABLE)


def _existing_columns(conn: object) -> set[str]:
    #: v1.1.0 PG 化：原为 `PRAGMA table_info`（SQLite 专属），统一走内省助手。
    return column_names(conn, _TARGET)


def upgrade() -> None:
    conn = op.get_bind()

    # ① 分组表（幂等：已存在则跳过）
    if not _table_exists(conn):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
            ),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            f"ix_{_TABLE}_user_id", _TABLE, ["user_id"], unique=False
        )
        # ⚠️ 唯一键 (user_id, name)：同一用户下分组名不重（重名让「按分组筛」歧义）。
        # 两列都 NOT NULL ⇒ 无 SQLite 唯一键 NULL 歧义。
        op.create_index(
            f"ux_{_TABLE}_user_name", _TABLE, ["user_id", "name"], unique=True
        )

    # ② shop_products 加三列（幂等：PRAGMA table_info 查列名，已存在的列跳过）
    existing = _existing_columns(conn)
    for column in _COLUMNS:
        if column.name in existing:
            continue
        op.add_column(_TARGET, column)


def downgrade() -> None:
    conn = op.get_bind()

    # 加列的逆操作：只删本次加的列（别的列不动）
    existing = _existing_columns(conn)
    for column in _COLUMNS:
        if column.name in existing:
            op.drop_column(_TARGET, column.name)

    if _table_exists(conn):
        op.drop_index(f"ux_{_TABLE}_user_name", table_name=_TABLE)
        op.drop_index(f"ix_{_TABLE}_user_id", table_name=_TABLE)
        op.drop_table(_TABLE)
