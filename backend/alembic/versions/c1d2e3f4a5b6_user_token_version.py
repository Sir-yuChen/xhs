"""users.token_version (DEF-022 / T-19): 无状态 refresh token 的撤销机制

Revision ID: c1d2e3f4a5b6
Revises: a1b2c3d4e5f7
Create Date: 2026-10-04

## 为什么

`refresh_token` 是**无状态签名令牌**（HMAC，无服务端存储）⇒ 此前**没有任何撤销手段**：
登出只是前端删本地存储，令牌本身在 7 天内一直有效 —— 谁拿到它谁就能持续续期。

修法 = 给 `users` 加 `token_version` 并**把它签进令牌**（`security._create_token`）：
- 登录 / 刷新时把 `user.token_version` 写进 payload 的 `ver`；
- 每次认证（`deps.get_current_user`）与 `/auth/refresh` 比对 `ver == user.token_version`；
- `/auth/logout` 与 `/auth/revoke-all-sessions` 把 `token_version` +1 ⇒ **该用户此代
  全部令牌立即失效**。

## 为什么这样做而不是黑名单表

黑名单要存每个 jti、要清理过期项、且每次请求多一次查表。版本号是**一行一列**的等价能力，
且天然回答了「改密后怎么让旧令牌全失效」这个更常见的问题（黑名单方案还要额外做一轮全量）。

## 兼容性

- `server_default='0'`：既有行迁移后立即有值（读出不必判空）。
- **老令牌兼容**：`ver` 缺失时按 0 处理（`deps` 里 `payload.get("ver") or 0`）⇒
  迁移后已登录用户不会被动掉线；但**一旦该用户登出**（版本抬到 1），
  他手上所有老令牌（ver=0）随即失效 —— 这正是期望行为。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    conn = op.get_bind()
    # 幂等：列已存在（重跑 / 手工加过）就跳过 —— 迁移必须在真库上可重入。
    if "token_version" not in _columns(conn, "users"):
        with op.batch_alter_table("users") as batch:
            batch.add_column(
                sa.Column(
                    "token_version",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )


def downgrade() -> None:
    conn = op.get_bind()
    if "token_version" in _columns(conn, "users"):
        with op.batch_alter_table("users") as batch:
            batch.drop_column("token_version")
