"""账号域三表合一的**数据搬迁 DDL**（2026-10-28 用户口径「小红书账号只保留一张表」）。

## 动作

| 表 | 处理 |
|---|---|
| `platform_accounts` | **保留**，加 3 列：`cookies_json` / `last_login_at` / `last_login_method` |
| `account_cookie_versions` | 搬数据（每账号每通道取最新一份）后 **DROP** |
| `login_sessions` | 只搬 `confirmed` 的登录时间/方式，然后 **DROP**（扫码态改内存，见 `login_sessions.py`） |

## 数据搬迁判据

`cookies_json` = 该账号**每个通道最新一份**信封的合并结果（JSONText，加密值原样搬）。
多行历史按 `created_at desc, id desc` 取首个出现的通道 —— 与 `latest_cookies_for_channels`
改造前完全一致（先出现者赢，防止旧信封顶掉新令牌）。

`last_login_at` / `last_login_method` = 该用户+sub_type 下 `confirmed` 会话的最大 `created_at`。
⚠️ `login_sessions` 只有 `user_id` 没有账号外键 ⇒ 按 `(user_id, sub_type)` 匹配账号。

## 为什么不搬 pending

`login_sessions` 实测 226 行里 194 行 `pending` + 6 行 `expired` + 4 行 `scanned`
是**未完成的扫码尝试**，按用户口径不保留。

Revision ID: v8w9x0y1z2a3
Revises: u7v8w9x0y1z2
"""

from alembic import op
import sqlalchemy as sa

revision = "v8w9x0y1z2a3"
down_revision = "u7v8w9x0y1z2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    cols = {c["name"] for c in insp.get_columns("platform_accounts")}
    if "cookies_json" not in cols:
        op.add_column(
            "platform_accounts",
            sa.Column("cookies_json", sa.Text(), nullable=False, server_default="{}"),
        )
    if "last_login_at" not in cols:
        op.add_column("platform_accounts", sa.Column("last_login_at", sa.DateTime(), nullable=True))
    if "last_login_method" not in cols:
        op.add_column(
            "platform_accounts",
            sa.Column("last_login_method", sa.String(length=32), nullable=False, server_default=""),
        )

    # ---- 搬 Cookie：每账号每通道取最新一份 ----
    if "account_cookie_versions" in tables:
        rows = bind.execute(
            sa.text(
                "SELECT platform_account_id, encrypted_cookies "
                "FROM account_cookie_versions "
                "ORDER BY created_at DESC, id DESC"
            )
        ).all()
        #: 用 Python 解信封再合并（SQL 里做不了「按通道取首次出现」）。
        from backend.app.core.security import decrypt_text, encrypt_text
        from backend.app.services.account_service import dump_channel_envelope, parse_channel_envelope

        merged: dict[int, dict[str, str]] = {}
        for account_id, encrypted in rows:
            try:
                envelope = parse_channel_envelope(decrypt_text(encrypted))
            except Exception:  # noqa: BLE001 - 历史坏行直接跳过（缺值口径：不造假）
                continue
            bucket = merged.setdefault(int(account_id), {})
            for channel, jar in envelope.items():
                bucket.setdefault(channel, jar)  # 先出现者赢 = 最新一份
        for account_id, channels in merged.items():
            bind.execute(
                sa.text("UPDATE platform_accounts SET cookies_json = :v WHERE id = :i"),
                {"v": encrypt_text(dump_channel_envelope(channels)), "i": account_id},
            )

    # ---- 搬登录历史：confirmed 的最大时间 ----
    if "login_sessions" in tables:
        bind.execute(
            sa.text(
                "UPDATE platform_accounts a SET "
                "  last_login_at = s.last_at, "
                "  last_login_method = s.last_method "
                "FROM ("
                "  SELECT DISTINCT ON (user_id, sub_type) "
                "         user_id, sub_type, created_at AS last_at, login_method AS last_method "
                "  FROM login_sessions "
                "  WHERE status = 'confirmed' "
                "  ORDER BY user_id, sub_type, created_at DESC, id DESC"
                ") s "
                "WHERE a.user_id = s.user_id "
                "  AND (a.sub_type = s.sub_type OR (a.sub_type IS NULL AND s.sub_type IS NULL))"
            )
        )

    # ---- 删两张表 ----
    if "account_cookie_versions" in tables:
        op.drop_table("account_cookie_versions")
    if "login_sessions" in tables:
        op.drop_table("login_sessions")


def downgrade() -> None:
    """重建两张空表结构（数据不恢复 —— 原历史行已被 DROP）。"""
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "account_cookie_versions" not in tables:
        op.create_table(
            "account_cookie_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "platform_account_id",
                sa.Integer(),
                sa.ForeignKey("platform_accounts.id"),
                nullable=False,
            ),
            sa.Column("encrypted_cookies", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_account_cookie_versions_platform_account_id",
            "account_cookie_versions",
            ["platform_account_id"],
        )

    if "login_sessions" not in tables:
        op.create_table(
            "login_sessions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("platform", sa.String(length=32), nullable=False),
            sa.Column("sub_type", sa.String(length=32), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("login_method", sa.String(length=32), nullable=False),
            sa.Column("qr_id", sa.String(length=128), nullable=True),
            sa.Column("code", sa.String(length=128), nullable=True),
            sa.Column("qr_url", sa.Text(), nullable=True),
            sa.Column("encrypted_temp_cookies", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )

    for col in ("cookies_json", "last_login_at", "last_login_method"):
        try:
            op.drop_column("platform_accounts", col)
        except Exception:  # noqa: BLE001 - 已不存在即跳过
            pass
