"""users + email / phone

⚠️ **顺序说明**（2026-10-24 实测修正）：本条原本排在播种迁移 `c3d4e5f6a1b2`
**之后**，但播种会调用 `seed_system_domain()` → `seed_admin_user()`，而那个函数
读写的 `User` 模型**已经**带 `email` / `phone` ⇒ 表结构必须先有这两列，否则
`no such column: users.email`（读）或 `no such column named email`（写）。
故本条**提前到播种之前**（`down_revision` 从 `d4e5f6a1b2c3` 改为 `b2c3d4e5f6a0`），
而 `d4e5f6a1b2c3`（role_menus 可空）改接到本条之后。 —— 支持邮箱验证码注册与登录（对齐 dataPanel）

v1.1.0 / 设计 57 S3（实测缺口）。

## 为什么必须加 `email`

用户口径（设计 46 §2）：**注册改为邮箱验证码 + 待审，与 dataPanel 一致**。
dataPanel 的 `t_user` 有 `email` 列，且它是**登录凭证**（邮箱 + 验证码，或邮箱 + 密码）。
XHS 的 `users` 表原先只有 `username`，没有邮箱 —— 没有邮箱就发不了验证码，
这个需求**无法实现**。

设计 47 曾写「`users` 已有 `uq_users_email`（既有）」——**这是错的**，
实测两库（SQLite 主库 / PG）都没有 `email` 列，也没有该唯一索引。
本条迁移把这个缺口补上。

## 为什么 `email` 可空但**非空值唯一**

* 可空：存量 27 个用户都没有邮箱（`zy晨` / `admin` 等），要求必填会让迁移失败；
* 非空唯一：邮箱是登录凭证，两个账号共用一个邮箱会导致「输一个邮箱进两个账号」
  的歧义。用**部分唯一索引**（`WHERE email <> ''`）表达 —— 与
  `platform_accounts` 的做法一致（设计 51 §3.1：部分索引必须**同时**写
  `sqlite_where` 与 `postgresql_where`，只写一边会让另一边静默不建）。

## `phone` 为什么也加

设计 55 §2 的用户列表/详情契约里有 `phone`（列表脱敏、详情给全），
而库里没有这一列。不加就只能从 DTO 里删掉 —— 但「管理员看得到用户手机号」
是这个页面的常规诉求，且后续短信通知也要它。故一并加上，同样非空唯一。

`phone` 不要求格式严格校验（允许为空，也允许带区号），
格式校验交给端点层的 `Field(max_length=32)` —— 迁移里不做业务校验。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5f6a1b2c3d4"
down_revision = "b2c3d4e5f6a0"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    try:
        return {c["name"] for c in inspector.get_columns(table)}
    except Exception:  # noqa: BLE001 - 最小夹具没有该表
        return set()


def _index_names(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    try:
        return {i["name"] for i in inspector.get_indexes(table)}
    except Exception:  # noqa: BLE001
        return set()


#: 部分唯一索引的谓词。`email <> ''` 而不是 `email IS NOT NULL`：
#: 空串是「没填」，不是「填了一个空邮箱」—— 两者都不该参与唯一性约束。
_EMAIL_WHERE = "email <> ''"
_PHONE_WHERE = "phone <> ''"


def upgrade() -> None:
    cols = _columns("users")
    if not cols:  # 表不存在（最小夹具）
        return

    with op.batch_alter_table("users") as batch:
        if "email" not in cols:
            batch.add_column(
                sa.Column("email", sa.String(255), nullable=False, server_default="")
            )
        if "phone" not in cols:
            batch.add_column(
                sa.Column("phone", sa.String(32), nullable=False, server_default="")
            )

    names = _index_names("users")
    if "uq_users_email_nonempty" not in names:
        op.create_index(
            "uq_users_email_nonempty",
            "users",
            ["email"],
            unique=True,
            sqlite_where=sa.text(_EMAIL_WHERE),
            postgresql_where=sa.text(_EMAIL_WHERE),
        )
    if "uq_users_phone_nonempty" not in names:
        op.create_index(
            "uq_users_phone_nonempty",
            "users",
            ["phone"],
            unique=True,
            sqlite_where=sa.text(_PHONE_WHERE),
            postgresql_where=sa.text(_PHONE_WHERE),
        )


def downgrade() -> None:
    cols = _columns("users")
    if not cols:
        return
    names = _index_names("users")
    if "uq_users_email_nonempty" in names:
        op.drop_index("uq_users_email_nonempty", table_name="users")
    if "uq_users_phone_nonempty" in names:
        op.drop_index("uq_users_phone_nonempty", table_name="users")
    with op.batch_alter_table("users") as batch:
        if "email" in cols:
            batch.drop_column("email")
        if "phone" in cols:
            batch.drop_column("phone")
