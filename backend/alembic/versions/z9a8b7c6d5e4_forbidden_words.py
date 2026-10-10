"""forbidden_words 违规词库表（设计 25 §2）。

用户口径（2026-10-08）：商品台账新增「违规词」功能 —— 新增 / 修改 / 停用 /
启用，且**违规词不可重复**。

## 为什么是**本地自维护词库**，不接平台 `check_forbidden_keyword`

平台端点 `POST /api/edith/product/check_forbidden_keyword` 是**校验器**（拿文本问
平台「有没有违规」），它**不是词库**、不支持增删改查、不给「停用/启用」语义。
用户要的是一套自己能维护的词库 ⇒ 本地表；平台校验器可作**发布前的补充把关**
（但那是只读查询，不影响本表语义）。

## 表设计要点

* `word` 唯一索引 —— 「不可重复」由**数据库**保证，不是应用层 if（应用层检查
  会被并发写绕过；DEF-088 同型：空值落行撞唯一约束整批崩）。
* `enabled` 停用/启用 —— **软开关**而非删除：停用的词要保留历史（曾经命中过
  哪些图要能追溯），删了就查不到了。
* `remark` 备注 —— 运营要知道「为什么禁这个词」。
* `user_id` 账号维度隔离（ADR-021：所有数据按平台账号维度隔离）——
  词库是**每个登录用户自己维护**的，不共享。
* `match_mode` 预留：1 = 精确（整段相等）、2 = 包含（子串命中）。
  **默认 2（包含）** —— 真实场景是「图里印了『最佳』两个字」，
  精确匹配永远匹配不上。但留出字段是因为将来可能出现需要精确的词。

⚠️ **不给「创建人 user_id 唯一」**：词库按 user 隔离即可，同一 user 内
`word` 唯一（复合唯一索引 `(user_id, word)`）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

#: revision id
revision = "z9a8b7c6d5e4"
#: revises（2026-10-08 实测 `alembic heads` 唯一 head）
down_revision = "q1r2s3t4u5v6"
branch_labels = None
depends_on = None

#: 匹配模式：1 = 精确（整段相等）、2 = 包含（子串命中，**默认**）。
MATCH_MODE_EXACT = 1
MATCH_MODE_CONTAINS = 2


def upgrade() -> None:
    op.create_table(
        "forbidden_words",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        #: 词本身。**用户可见、不允许改大小写**（中文无大小写；英文统一按用户原文存，
        #: 匹配时再归一化 —— 存归一化值会让用户看不懂自己填了什么）。
        sa.Column("word", sa.String(length=120), nullable=False),
        #: 匹配模式（1 精确 / 2 包含）。默认 2 —— 见模块 docstring。
        sa.Column(
            "match_mode",
            sa.Integer(),
            nullable=False,
            server_default=str(MATCH_MODE_CONTAINS),
        ),
        #: 停用/启用。**软开关**：停用不删行（历史命中可追溯）。
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("remark", sa.String(length=255), nullable=False, server_default=""),
        #: ADR-021 账号维度隔离：词库按登录用户各持一份。
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    #: ⚠️ **「不可重复」由数据库保证**（`(user_id, word)` 复合唯一）。
    #: 应用层先查后插会被并发写绕过 —— 那是 DEF-088 的同型错误。
    op.create_index(
        "ux_forbidden_words_user_word", "forbidden_words", ["user_id", "word"], unique=True
    )
    #: 列表按启用状态 + 词排序（列表端点的默认序）。
    op.create_index(
        "ix_forbidden_words_user_enabled",
        "forbidden_words",
        ["user_id", "enabled"],
    )


def downgrade() -> None:
    op.drop_index("ix_forbidden_words_user_enabled", table_name="forbidden_words")
    op.drop_index("ux_forbidden_words_user_word", table_name="forbidden_words")
    op.drop_table("forbidden_words")
