"""add shop tables (qianfan binding + note goods mapping)

Revision ID: c7a4e51d9b02
Revises: b1e4d7c9a2f3
Create Date: 2026-09-19 12:30:00.000000

张两张表对应「我的笔记 → 挂载商品」的真实数据来源（千帆商家后台）：

* `shop_account_bindings`：账号 → 千帆店铺的只读绑定 + 千帆自己的登录态。
* `shop_note_goods`：千帆「商品笔记」列表的本地映射（note_id → 商品）。

依据见 `docs/02_设计文档/12_功能设计_我的笔记与互动数据.md` §19。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7a4e51d9b02'
down_revision: Union[str, None] = 'b1e4d7c9a2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'shop_account_bindings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('platform_account_id', sa.Integer(), nullable=False),
        sa.Column('shop_user_id', sa.String(length=64), nullable=False),
        sa.Column('shop_name', sa.String(length=128), nullable=False),
        sa.Column('encrypted_cookies', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('status_message', sa.Text(), nullable=False),
        sa.Column('goods_note_total', sa.Integer(), nullable=False),
        sa.Column('goods_synced_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['platform_account_id'], ['platform_accounts.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('platform_account_id'),
    )
    with op.batch_alter_table('shop_account_bindings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_shop_account_bindings_id'), ['id'], unique=False)
        batch_op.create_index(batch_op.f('ix_shop_account_bindings_user_id'), ['user_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_shop_account_bindings_platform_account_id'),
            ['platform_account_id'],
            unique=False,
        )

    op.create_table(
        'shop_note_goods',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('binding_id', sa.Integer(), nullable=False),
        sa.Column('note_id', sa.String(length=64), nullable=False),
        sa.Column('item_id', sa.String(length=64), nullable=False),
        sa.Column('item_title', sa.Text(), nullable=False),
        sa.Column('item_image', sa.Text(), nullable=False),
        sa.Column('variant_desc', sa.String(length=255), nullable=False),
        sa.Column('goods_json', sa.Text(), nullable=False),
        sa.Column('entry_json', sa.Text(), nullable=False),
        sa.Column('synced_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['binding_id'], ['shop_account_bindings.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('binding_id', 'note_id', name='uq_shop_note_goods_binding_note'),
    )
    with op.batch_alter_table('shop_note_goods', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_shop_note_goods_id'), ['id'], unique=False)
        batch_op.create_index(batch_op.f('ix_shop_note_goods_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_shop_note_goods_binding_id'), ['binding_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_shop_note_goods_note_id'), ['note_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('shop_note_goods', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_shop_note_goods_note_id'))
        batch_op.drop_index(batch_op.f('ix_shop_note_goods_binding_id'))
        batch_op.drop_index(batch_op.f('ix_shop_note_goods_user_id'))
        batch_op.drop_index(batch_op.f('ix_shop_note_goods_id'))
    op.drop_table('shop_note_goods')

    with op.batch_alter_table('shop_account_bindings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_shop_account_bindings_platform_account_id'))
        batch_op.drop_index(batch_op.f('ix_shop_account_bindings_user_id'))
        batch_op.drop_index(batch_op.f('ix_shop_account_bindings_id'))
    op.drop_table('shop_account_bindings')
