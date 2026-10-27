"""platform_accounts.disabled —— 用户手动控制的停用位

Revision ID: q3r4s5t6u7v8
Revises: p2q3r4s5t6u7
Create Date: 2026-10-27

「该账号是否继续使用」必须是**人工写**的一列。原 `status` 由巡检与登录刷新自动写，
两者混在一列会互相覆盖（巡检一跑就冲掉用户设的停用）。因此：

* `disabled = false`（默认）= 正常使用；`true` = 已停用；
* 定时任务 / 探活 / 登录刷新一律不写它（守卫 `test_account_disabled_switch.py`）；
* 停用不删数据、不吊销令牌。

`server_default false` ⇒ 存量行无需回填（升级前所有账号都是「正常使用」）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "q3r4s5t6u7v8"
down_revision = "p2q3r4s5t6u7"
branch_labels = None
depends_on = None

TABLE = "platform_accounts"
COLUMN = "disabled"
INDEX = "ix_platform_accounts_disabled"


def _has_column(bind: sa.engine.Connection) -> bool:
    inspector = sa.inspect(bind)
    return COLUMN in {col["name"] for col in inspector.get_columns(TABLE)}


def _has_index(bind: sa.engine.Connection) -> bool:
    inspector = sa.inspect(bind)
    return INDEX in {idx["name"] for idx in inspector.get_indexes(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(
            TABLE,
            sa.Column(
                COLUMN,
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
    if not _has_index(bind):
        op.create_index(INDEX, TABLE, [COLUMN])


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind):
        op.drop_index(INDEX, table_name=TABLE)
    if _has_column(bind):
        op.drop_column(TABLE, COLUMN)
