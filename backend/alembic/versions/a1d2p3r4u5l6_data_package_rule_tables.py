"""add data package rule tables (2026-10-14)

Revision ID: a1d2p3r4u5l6
Revises: c2d3e4f5a6b8, q8r9s0t1u2v3

设计 40 §3：新增「数据包列规则」父子两表。

⚠️ **revision id 必须唯一** —— 初稿曾用 `d3e4f5a6b7c8`，与既有的
`d3e4f5a6b7c8_backfill_queue_record_source.py` **撞车**（两个字面量同 id
⇒ alembic 报多 head / 解析歧义）。改用 `a1d2p3r4u5l6`（程序化比对 89 个迁移文件的 revision 后确认唯一）。

## 同轮追加：规则改「规则名称」、不再绑列

用户口径 2026-10-14：「**规则有规则名称不可重复，不是列名，规则不限制只针对于固定列，
使用时根据用户选择**」。

⇒ 子表直接按**最终形态**建：`column_name` → **`rule_name`**、`column_match` **不建**、
唯一约束为 **`(set_id, rule_name)`**。
⚠️ 表是本迁移刚建的且**尚无存量数据** ⇒ 不留「建表 → 立刻改表」的中间态，
也就**不另起一个 revision**（那会让 upgrade 路径上凭空多出两跳）。

⚠️ **不播种、不回填**：规则是**用户资产**（每人的数据包列名是私有的），
替用户猜一个规则反而是错的。新装用户的种子规则见 `config/initial_data/`
（与 `scheduled_jobs.json` 同惯例，由代码在注册时写入，**不在迁移里**——
避免「迁移里的种子」与「代码里的注册」两份真相源，R24 纪律）。

⚠️ **不 import 应用代码**：迁移必须能在「当时任意版本的代码」上跑通。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1d2p3r4u5l6"
#: ⚠️ **必须接两个父**（本轮实测：接单个会留下另一个 head）。
#: 现状是两条并行分支：`c2d3e4f5a6b8`（主线）与 `q8r9s0t1u2v3`（工作流那支）。
#: 只接 `c2d3e4f5a6b8` ⇒ alembic 报 **"Multiple head revisions"**（实测红过
#: `test_migration_no_longer_seeds_scheduled_jobs`）；只接 `q8r9s0t1u2v3`
#: ⇒ `c2d3e4f5a6b8` 又成了悬空 head。⇒ **两个都接**（alembic 的多父合并语义）。
#: ⚠️ **单父**（2026-10-15 修正）：原先写成双父 `("c2d3e4f5a6b8", "q8r9s0t1u2v3")`
#: —— 第二个父 `q8r9s0t1u2v3` 是**自动化工作流的建表迁移**，而那条特性已在
#: `1e09bf7`（2026-10-07）**整条移除**（含 `e6f7a8b9c0d1_drop_workflow_tables`）。
#: 把已删除的迁移重新挂进主链 ⇒ 迁移图里出现**已退场特性的建表迁移**，
#: 且与 drop 迁移语义相矛盾。
#: 现已恢复 `1e09bf7` 的意图：**工作流不在这条链上**。
down_revision = "c2d3e4f5a6b8"
branch_labels = None
depends_on = None


#: ⚠️ 存量「旧列名 → 人话规则名」的映射**按 `params` 反查**（不按旧列名：
#: 旧列名是用户数据包里任意列名，按它匹配会误伤）。这三条正是种子写入的形态。
OP_EXTRACT_SEGMENT_SEP_SEMI = '{"sep": ";", "index": 2}'
OP_EXTRACT_SEGMENT_SEP_COLON = '{"sep": ":", "index": 1}'
OP_EXTRACT_BETWEEN_PAREN = '{"open": "(", "close": ")", "occurrence": 1}'


def _columns(table: str) -> set[str]:
    """表已存在时取它的列名；表不存在返回空集。"""
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def _has_unique(table: str, name: str) -> bool:
    if not _columns(table):
        return False
    inspector = sa.inspect(op.get_bind())
    return any(uc.get("name") == name for uc in inspector.get_unique_constraints(table))


def upgrade() -> None:
    """建两表；**若本迁移已跑过一次（表在、但带旧的 `column_name`）则在原地改造成最终形态**。

    ⚠️ 为什么不做「纯新建」：本迁移的初版（同一天早些时候）已经跑进过真实库
    （26 个用户 × 播种 = 52 包 / 78 规则），直接改 `create_table` 会让
    「已升级的库」与「新装的库」形态不一致 —— 而把用户已配的规则删掉重来更糟。
    ⇒ 兼容两态：**表不存在 ⇒ 按最终形态建；表已存在 ⇒ 原地 `ALTER`**。
    （本项目既有的 `Alembic` 迁移也用过这个手法，见 `d5e6f7a8b9c0`。）
    """
    cols = _columns("data_package_rule_sets")

    #: ---- 父表：规则包 ----
    if not cols:
        op.create_table(
            "data_package_rule_sets",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("remark", sa.String(length=500), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            #: 同一用户下规则包不许重名（否则列头弹窗「选包」有歧义）。
            sa.UniqueConstraint("user_id", "name", name="uq_dp_rule_sets_user_name"),
        )
        op.create_index(
            "ix_data_package_rule_sets_user_id", "data_package_rule_sets", ["user_id"]
        )

    #: ---- 子表：规则 ----
    rcols = _columns("data_package_rules")
    if not rcols:
        op.create_table(
            "data_package_rules",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("set_id", sa.Integer(), nullable=False),
            #: **规则名称**（不是列名 —— 规则不绑列，用在哪列由用户在执行时选）。
            sa.Column("rule_name", sa.String(length=100), nullable=False),
            sa.Column("op", sa.String(length=30), nullable=False),
            sa.Column("params_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            #: 删包级联删规则（设计 40 §4.4）。
            sa.ForeignKeyConstraint(
                ["set_id"], ["data_package_rule_sets.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            #: 一个包里**规则名不可重复**（用户口径 2026-10-14）。
            sa.UniqueConstraint("set_id", "rule_name", name="uq_dp_rules_set_name"),
        )
        op.create_index("ix_data_package_rules_set_id", "data_package_rules", ["set_id"])
    else:
        #: ⚠️ 已跑过初版：原地改造（见 docstring）。
        #: ① 先丢旧唯一约束（否则改名时它还会按旧列名卡）。
        with op.batch_alter_table("data_package_rules") as batch:
            if _has_unique("data_package_rules", "uq_dp_rules_set_column"):
                batch.drop_constraint("uq_dp_rules_set_column", type_="unique")
        #: ② `column_name` → `rule_name`（**保留数据**：用户配的规则不丢）。
        if "column_name" in rcols and "rule_name" not in rcols:
            with op.batch_alter_table("data_package_rules") as batch:
                batch.alter_column("column_name", new_column_name="rule_name")
        #: ③ `column_match` 整列删除（规则不再绑列，没有「怎么匹配列名」这回事）。
        if "column_match" in rcols:
            with op.batch_alter_table("data_package_rules") as batch:
                batch.drop_column("column_match")
        #: ④ **接住「存量还是旧列名」这一步**（`column_name` = `SKU信息` / `sku名称` / `图片`
        #: 这类**列名**，语义上现在是「规则名」）。
        #: 不把旧列名当成规则名留着 —— 规则已经**不绑列**了（用户口径 2026-10-14），
        #: 留一个叫「SKU信息」的规则名会让用户以为它还按列匹配（正是要消掉的误解）。
        #: 按 `params` 反查这是哪条**种子规则**，直接改成人话规则名。
        #: ⚠️ 只动种子那三条；用户自建的（改了 `params` 的）**一律不碰**。
        conn = op.get_bind()
        renames = {
            OP_EXTRACT_SEGMENT_SEP_SEMI: "取颜色分类",
            OP_EXTRACT_SEGMENT_SEP_COLON: "取 SKU 名称",
            OP_EXTRACT_BETWEEN_PAREN: "取括号内名称",
        }
        for params_json, new_name in renames.items():
            conn.execute(
                sa.text(
                    "UPDATE data_package_rules SET rule_name = :new "
                    "WHERE params_json = :params "
                    "AND rule_name NOT IN ('取颜色分类', '取 SKU 名称', '取括号内名称')"
                ),
                {"new": new_name, "params": params_json},
            )
        #: ⑤ 补上新唯一约束（同一包内规则名不可重复）。
        with op.batch_alter_table("data_package_rules") as batch:
            batch.create_unique_constraint("uq_dp_rules_set_name", ["set_id", "rule_name"])


def downgrade() -> None:
    """先子后父（外键依赖顺序）。

    ⚠️ 这是**整表级**回退 —— 规则会随之消失（数据是用户资产）。
    生产上不跑它；测试里用来验证「建→降→再建」幂等。
    """
    if _columns("data_package_rules"):
        op.drop_index("ix_data_package_rules_set_id", table_name="data_package_rules")
        op.drop_table("data_package_rules")
    if _columns("data_package_rule_sets"):
        op.drop_index(
            "ix_data_package_rule_sets_user_id", table_name="data_package_rule_sets"
        )
        op.drop_table("data_package_rule_sets")
