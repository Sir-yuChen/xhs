"""add cookie channel (www / ark) to account_cookie_versions

一个账号、两条凭据通道（设计 12 §11.5 / v1.24；台账 §4.10.11）。

真机实测结论（§17.6.2）：**www 与 ark 是两套互不通用的令牌** ——
  * www：`web_session` + `id_token` → `www` / `edith` / `sns/web`
  * ark：`customer-sso-sid` / `access-token-ark` → **创作者平台与千帆共用**
所以同一个账号下要能并存两条令牌，并按「要打哪个平台的接口」选对应通道。

本迁移只做两件事（幂等可续跑）：
  1. 加 `channel` 列（默认 'www'）；
  2. 按 Cookie 形态回填：带 `customer-sso-sid` / `access-token-ark` /
     `galaxy_creator_session_id` 的判为 `ark`，其余 `www`。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "g7h8i9j0k1l2"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


ARK_MARKERS = ("customer-sso-sid", "access-token-ark", "galaxy_creator_session_id")


def _has_column(conn, table: str, column: str) -> bool:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "account_cookie_versions", "channel"):
        # SQLite 的 ADD COLUMN 必须带默认值（否则历史行拿到 NULL，读取会漏）。
        conn.exec_driver_sql(
            "ALTER TABLE account_cookie_versions ADD COLUMN channel VARCHAR(16) NOT NULL DEFAULT 'www'"
        )
    # 回填：解密在应用层做，这里只能按密文外的信息判 —— 而 Cookie 是加密存储的，
    # 所以回填交给 `backend/scripts/backfill_cookie_channels.py`（用应用层密钥解密后
    # 按形态分类）。迁移本身只保证列存在且有默认值。
    conn.exec_driver_sql(
        "UPDATE account_cookie_versions SET channel = 'www' WHERE channel IS NULL OR channel = ''"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn, "account_cookie_versions", "channel"):
        # SQLite 不支持直接 DROP COLUMN 的老版本用重建表方式；3.35+ 支持 DROP COLUMN。
        try:
            conn.exec_driver_sql("ALTER TABLE account_cookie_versions DROP COLUMN channel")
        except Exception:  # noqa: BLE001 - 老版本 SQLite：走重建
            conn.exec_driver_sql(
                "CREATE TABLE account_cookie_versions_old AS "
                "SELECT id, platform_account_id, encrypted_cookies, created_at "
                "FROM account_cookie_versions"
            )
            conn.exec_driver_sql("DROP TABLE account_cookie_versions")
            conn.exec_driver_sql(
                "CREATE TABLE account_cookie_versions ("
                "id INTEGER NOT NULL PRIMARY KEY, "
                "platform_account_id INTEGER NOT NULL REFERENCES platform_accounts(id), "
                "encrypted_cookies TEXT NOT NULL, "
                "created_at DATETIME)"
            )
            conn.exec_driver_sql(
                "INSERT INTO account_cookie_versions "
                "(id, platform_account_id, encrypted_cookies, created_at) "
                "SELECT id, platform_account_id, encrypted_cookies, created_at "
                "FROM account_cookie_versions_old"
            )
            conn.exec_driver_sql("DROP TABLE account_cookie_versions_old")
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_account_cookie_versions_platform_account_id "
                "ON account_cookie_versions (platform_account_id)"
            )
