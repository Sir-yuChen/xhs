"""drop login_sessions.phone_mask — 登录凭据不落库

2026-09-23 用户口径：

> 「登录时手机号和邮箱密码等均不落库，数据库只存登录成功后的账号信息和会话临牌」

## 问题

`login_sessions.phone_mask` 存的是手机号掩码（`138****8000`）。它是**只写不读**
的字段——前后端零消费方（`grep phone_mask` 只剩模型定义与两处写入），留着没有
任何收益；而「掩码」仍是手机号的派生值，与上述口径「手机号不落库」相抵。

## 修法（两半，本迁移是其中一半）

1. **代码**：`pc_phone_send_code` / `creator_phone_send_code` 停写 `phone_mask`，
   `_mask_phone()` 辅助函数删除，模型列定义移除（原句以注释保留）；
2. **存量**（本迁移）：`ALTER TABLE login_sessions DROP COLUMN phone_mask`
   （SQLite ≥ 3.35 原生支持；batch 模式与项目其他迁移一致）。

⚠️ 只改代码不做迁移 = 假修复：老行里的掩码永远留在库里。

短信验证码**从不落库**（只随 confirm 请求透传给平台）；邮箱密码登录是同步端点、
根本不建 `login_sessions` 行 —— 两边的凭据都只有「换令牌」这一个用途。
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'x7y8z9a0b1c2'
down_revision: Union[str, None] = 'w6x7y8z9a0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('login_sessions', schema=None) as batch_op:
        batch_op.drop_column('phone_mask')


def downgrade() -> None:
    with op.batch_alter_table('login_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('phone_mask', sa.String(length=32), nullable=True))
