"""aigc_media_assets 加商品关联两列（设计 17 §13.3.11，v2.30）

用户口径：「根据商品信息生成素材如何与商品产生关联」「进入素材库后无法再区分与产品的关系」

加 item_id（业务 ID，非台账主键）+ platform_account_id（多账号同 ID 防串号）。
两列**均可空**：不是所有生成都来自商品；**历史行不回填**（不知道就不写，不造假）。

Revision ID: d1e2f3a4b5c6
Revises: c9d0e1f2a3b4
"""

from alembic import op
import sqlalchemy as sa

revision = "d1e2f3a4b5c6"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {col["name"] for col in insp.get_columns(table)}


def upgrade() -> None:
    cols = _columns("aigc_media_assets")
    if not cols:
        return  # 表还不存在（更早的迁移会建）—— 不在这里越权建表
    with op.batch_alter_table("aigc_media_assets") as batch:
        if "item_id" not in cols:
            batch.add_column(sa.Column("item_id", sa.String(64), nullable=True))
        if "platform_account_id" not in cols:
            batch.add_column(sa.Column("platform_account_id", sa.Integer(), nullable=True))
    if "item_id" not in cols:
        op.create_index("ix_aigc_media_assets_item_id", "aigc_media_assets", ["item_id"])
    if "platform_account_id" not in cols:
        op.create_index(
            "ix_aigc_media_assets_platform_account_id",
            "aigc_media_assets",
            ["platform_account_id"],
        )


def downgrade() -> None:
    cols = _columns("aigc_media_assets")
    if not cols:
        return
    with op.batch_alter_table("aigc_media_assets") as batch:
        if "item_id" in cols:
            batch.drop_index("ix_aigc_media_assets_item_id")
            batch.drop_column("item_id")
        if "platform_account_id" in cols:
            batch.drop_index("ix_aigc_media_assets_platform_account_id")
            batch.drop_column("platform_account_id")
