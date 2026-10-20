"""LLM 生成笔记的版本历史（每次生成一条记录，可基于某版本只重生成正文 / 只重配图片）

用户口径 2026-10-17：

> 「千帆生成的笔记不存，LLM 生成笔记记录，且还有一个问题，比如基于指定生成的某一版本，
>   用户只想重新生成正文或只重新配置生成图片」

## 改了什么

`generation_records` 原先只有「一次生成本身」的字段（标题/正文/话题），无法表达：

1. **这是第几版**（同一商品先后生成 3 次，哪条是最新）；
2. **基线版本是谁**（「基于第 2 版只重生成正文」⇒ 需要 `parent_record_id`）；
3. **这一版只重做了什么**（正文 / 图片 / 全部）；
4. **当前采用的版本**（哪一版被写成了 `notes` 里那篇）。

新增 5 列：

| 列 | 用途 |
|---|---|
| `version_no` | 同商品内的版本序号（1,2,3…；**同商品内唯一**） |
| `parent_record_id` | 基线版本（全量生成 ⇒ NULL；局部重生成 ⇒ 指向那一版） |
| `regen_scope` | `all` / `text` / `media`（这一版重做了什么） |
| `is_adopted` | 是否是当前采用版（同商品**最多一条**为 1） |
| `copy_requirements_json` | 当时的**文案要求快照**（区间/表情/提示词）—— 模板改了也能还原 |

## 为什么 `is_adopted` 用「同商品最多一条为 1」而不是「最新一条」

用户可能生成 3 版后选回**第 1 版**采用（第 3 版更新但不代表被采用）。
⇒ 「采用」是**用户的选择**，不是时间序；用时间序会永久丢掉这个信息。

## 存量数据

`version_no` 默认 1、`regen_scope` 默认 `all`、`is_adopted` 默认 1（存量 2 条都是失败记录，
没有「采用」概念，默认 1 不会造成「多条 adopted」）。

Revision ID: c1d2e3f4a5b7
Revises: b3e4f5a6c7d8
Create Date: 2026-10-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c1d2e3f4a5b7"
down_revision = "b3e4f5a6c7d8"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    """现有列名（幂等判据：迁移可能被重复执行 / stamp 到中途再 upgrade）。"""
    inspector = sa.inspect(op.get_bind())
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    # ⚠️ **全部用 batch mode**：SQLite 不支持 `ALTER TABLE ... ADD CONSTRAINT`，
    # 直接 `op.create_foreign_key()` 会抛
    # `NotImplementedError: No support for ALTER of constraints in SQLite dialect`。
    # batch mode 是 SQLite 官方推荐做法（建新表 → 拷数据 → 换名），顺带保留数据。

    #: `note_generation_links` 补「指向生成记录」—— 版本历史 ↔ 笔记的唯一桥。
    #: 没有它，「只重写文案」时无法从基线记录反查它产出的笔记（沿用其媒体）。
    link_cols = _columns("note_generation_links")
    if "generation_record_id" not in link_cols:
        with op.batch_alter_table("note_generation_links") as batch:
            batch.add_column(sa.Column("generation_record_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_note_generation_links_record",
                "generation_records",
                ["generation_record_id"],
                ["id"],
                ondelete="SET NULL",
            )
        op.create_index(
            "ix_note_generation_links_record",
            "note_generation_links",
            ["generation_record_id"],
            if_not_exists=True,
        )

    existing = _columns("generation_records")
    with op.batch_alter_table("generation_records") as batch:
        if "version_no" not in existing:
            batch.add_column(
                sa.Column("version_no", sa.Integer(), nullable=False, server_default="1")
            )
        if "parent_record_id" not in existing:
            batch.add_column(sa.Column("parent_record_id", sa.Integer(), nullable=True))
        if "regen_scope" not in existing:
            batch.add_column(
                sa.Column(
                    "regen_scope", sa.String(length=16), nullable=False, server_default="all"
                )
            )
        if "is_adopted" not in existing:
            batch.add_column(
                sa.Column("is_adopted", sa.Boolean(), nullable=False, server_default=sa.false())
            )
        if "copy_requirements_json" not in existing:
            batch.add_column(
                sa.Column(
                    "copy_requirements_json", sa.Text(), nullable=False, server_default="{}"
                )
            )
        if "parent_record_id" not in existing:
            #: 自引用外键（基线版本被删时置空，不连带删掉整条历史）
            batch.create_foreign_key(
                "fk_generation_records_parent",
                "generation_records",
                ["parent_record_id"],
                ["id"],
                ondelete="SET NULL",
            )

    #: 版本序号查询索引：按商品列历史（最常用的一条查询）
    op.create_index(
        "ix_generation_records_product_version",
        "generation_records",
        ["user_id", "shop_product_id", "version_no"],
        unique=False,
        if_not_exists=True,
    )
    #: 采用版唯一（**部分唯一索引**：只约束 is_adopted=1 的行）
    #: ⇒ 同商品最多一条「采用版」，其余版本可以都是 0。
    op.create_index(
        "uq_generation_records_adopted",
        "generation_records",
        ["user_id", "shop_product_id"],
        unique=True,
        sqlite_where=sa.text("is_adopted = 1"),
        postgresql_where=sa.text("is_adopted = 1"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_note_generation_links_record", table_name="note_generation_links")
    with op.batch_alter_table("note_generation_links") as batch:
        batch.drop_constraint("fk_note_generation_links_record", type_="foreignkey")
        batch.drop_column("generation_record_id")

    op.drop_index("uq_generation_records_adopted", table_name="generation_records")
    op.drop_index("ix_generation_records_product_version", table_name="generation_records")
    existing = _columns("generation_records")
    with op.batch_alter_table("generation_records") as batch:
        for name in (
            "copy_requirements_json",
            "is_adopted",
            "regen_scope",
            "parent_record_id",
            "version_no",
        ):
            if name in existing:
                batch.drop_column(name)
