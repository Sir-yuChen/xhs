"""PG 镜像：配置三级化（设计 64 §1，SQLite 链 b0c1d2e3f4a5）

user_id 转 nullable + workspace 列 + 三条 partial unique + 数据去重
（值==内置默认的用户行删、≠保留为覆盖）。

Revision ID: pg_0005_settings_three_scopes
Revises: pg_0004_business_logs
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision: str = "pg_0005_settings_three_scopes"
down_revision: str | None = "pg_0004_business_logs"
branch_labels = None
depends_on = None

_BUILTIN_TEXTS = {
    "crawl_rate_limit_per_minute": "8",
    "xhs_sync_account_parallelism": "3",
    "xhs_sync_account_gap_seconds": "10",
    "xhs_allow_duplicate_goods_publish": "false",
    "xhs_image_upload_parallelism": "8",
    "xhs_ocr_parallelism": "5",
    "xhs_note_image_total_limit": "6",
    "xhs_sku_name_max_chars": "60",
    "register.enabled": "false",
}


def _decode(raw, value_type):
    try:
        if value_type == "int":
            return int(float(raw))
        if value_type == "float":
            return float(raw)
        if value_type == "bool":
            return raw.strip().lower() in ("true", "1", "yes", "on")
        if value_type == "json":
            return json.loads(raw)
        return raw
    except (ValueError, TypeError):
        return None


def upgrade() -> None:
    op.add_column("system_settings", sa.Column("workspace", sa.String(32), nullable=True))
    op.alter_column("system_settings", "user_id", existing_type=sa.Integer(), nullable=True)
    op.drop_constraint("uq_system_settings_user_key", "system_settings", type_="unique")
    op.create_index(
        "uq_system_settings_system_key", "system_settings", ["key"], unique=True,
        postgresql_where=sa.text("user_id IS NULL AND workspace IS NULL"),
    )
    op.create_index(
        "uq_system_settings_user_key", "system_settings", ["user_id", "key"], unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_system_settings_workspace_key", "system_settings", ["workspace", "key"], unique=True,
        postgresql_where=sa.text("workspace IS NOT NULL"),
    )
    op.create_index("ix_system_settings_workspace", "system_settings", ["workspace"])
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, key, value, value_type FROM system_settings WHERE user_id IS NOT NULL")
    ).fetchall()
    for row_id, key, raw, value_type in rows:
        expected = _BUILTIN_TEXTS.get(str(key))
        if expected is None:
            continue
        if _decode(str(raw), str(value_type)) == _decode(str(expected), str(value_type)):
            bind.execute(sa.text("DELETE FROM system_settings WHERE id = :i").bindparams(i=row_id))


def downgrade() -> None:
    for name in ("uq_system_settings_system_key", "uq_system_settings_user_key", "uq_system_settings_workspace_key"):
        op.drop_index(name, table_name="system_settings")
    op.execute("DELETE FROM system_settings WHERE user_id IS NULL")
    op.create_unique_constraint("uq_system_settings_user_key", "system_settings", ["user_id", "key"])
    op.alter_column("system_settings", "user_id", existing_type=sa.Integer(), nullable=False)
