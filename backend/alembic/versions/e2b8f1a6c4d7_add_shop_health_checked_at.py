"""add health_checked_at to shop_account_bindings (T-31 千帆登录态巡检)

Revision ID: e2b8f1a6c4d7
Revises: c7a4e51d9b02
Create Date: 2026-09-19 14:05:00.000000

千帆登录态是**独立凭据**（`access-token-ark.xiaohongshu.com`），会过期且不能用账号
Cookie 复用（实测 `code=-13000`）。本列记录「最近一次主动巡检时间」，巡检按它判到期，
从而把「手动点一次」与「定时跑一次」合并成同一条有界路径（不会变成刷请求）。

依据见 `docs/02_设计文档/12_功能设计_我的笔记与互动数据.md` §24.3。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2b8f1a6c4d7'
down_revision: Union[str, None] = 'c7a4e51d9b02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('shop_account_bindings', schema=None) as batch_op:
        # 可空：老绑定行没有巡检记录 —— 视为「从未巡检」，下一次巡检会立刻覆盖它。
        batch_op.add_column(sa.Column('health_checked_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('shop_account_bindings', schema=None) as batch_op:
        batch_op.drop_column('health_checked_at')
