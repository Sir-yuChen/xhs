"""配置三级化：system_settings 支持 系统级/用户级/工作区级 三种归属（设计 64 §1）

2026-10-25 用户口径「配置优化为系统级--用户级--工作区级」。原表每用户一份
（`(user_id,key)` 唯一，26 用户 × 9 键冗余播种）⇒ 一张表三个归属：

* 系统级：`user_id IS NULL ∧ workspace IS NULL`（每 key 至多一行，admin 管理）；
* 用户级：`user_id` 非空（原语义不变，个人覆盖）；
* 工作区级：`workspace` 非空（String32，同 `menus.workspace` 词汇表）。

步骤：
1. `user_id` 转 nullable + 加 `workspace` 列；
2. 删全表唯一约束 `uq_system_settings_user_key`，换三条 partial unique
   （SQLite 不支持 DROP CONSTRAINT ⇒ batch 重建表；索引在 batch 外建）；
3. **数据去重**（设计 64 §1.4，不造假不丢数据）：值 == 内置默认的用户行删除
   （回落系统级，消除冗余播种）；值 != 默认的行**保留为用户级覆盖**（真改动）。
   内置默认值内联自 `settings_service.BUILTIN_DEFAULTS`（2026-10-25 快照，
   迁移自含不 import 应用模块 —— 与历史迁移同款纪律）。

幂等：是（IF EXISTS / 判空）。可逆：删三级新增行（系统/工作区）后换回旧约束
（幸存用户行都在旧值域内）；workspace 列保留不删（无害空列，避免 SQLite 再重建一次）。

Revision ID: b0c1d2e3f4a5
Revises: c9b8d7e6f5a4
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

revision = "b0c1d2e3f4a5"
down_revision = "c9b8d7e6f5a4"
branch_labels = None
depends_on = None

#: `settings_service.BUILTIN_DEFAULTS` 快照（改默认值必须同步这里，守卫会红提醒）。
_BUILTIN_JSON_VALUES: dict[str, str] = {
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

#: ⚠️ cols 必须是**列表**：`op.create_index` 把 str 当可迭代对象逐字符拆列
#:（实测 PG 报 `字段 "k" 不存在` —— 2026-10-25 起服务失败的真实根因）。
_IDX_DEFS = (
    ("uq_system_settings_system_key", ["key"], "user_id IS NULL AND workspace IS NULL"),
    ("uq_system_settings_user_key", ["user_id", "key"], "user_id IS NOT NULL"),
    ("uq_system_settings_workspace_key", ["workspace", "key"], "workspace IS NOT NULL"),
)


def _decode(raw: str, value_type: str):
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
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    if is_sqlite:
        with op.batch_alter_table("system_settings") as batch:
            batch.add_column(sa.Column("workspace", sa.String(32), nullable=True))
            batch.alter_column("user_id", existing_type=sa.Integer(), nullable=True)
            batch.drop_constraint("uq_system_settings_user_key", type_="unique")
    else:
        op.add_column("system_settings", sa.Column("workspace", sa.String(32), nullable=True))
        op.alter_column("system_settings", "user_id", existing_type=sa.Integer(), nullable=True)
        op.drop_constraint("uq_system_settings_user_key", "system_settings", type_="unique")

    op.create_index("ix_system_settings_workspace", "system_settings", ["workspace"])
    for name, cols, where in _IDX_DEFS:
        op.create_index(
            name,
            "system_settings",
            cols,
            unique=True,
            sqlite_where=sa.text(where),
            postgresql_where=sa.text(where),
        )

    # 数据去重：值 == 内置默认的用户行删除（回落系统级）；不同的保留为用户级覆盖。
    rows = bind.execute(
        sa.text("SELECT id, key, value, value_type FROM system_settings WHERE user_id IS NOT NULL")
    ).fetchall()
    dropped = 0
    for row_id, key, raw, value_type in rows:
        expected = _BUILTIN_JSON_VALUES.get(str(key))
        if expected is None:
            continue
        if _decode(str(raw), str(value_type)) == _decode(str(expected), str(value_type)):
            bind.execute(sa.text("DELETE FROM system_settings WHERE id = :i").bindparams(i=row_id))
            dropped += 1
    if dropped:
        print(f"[b0c1d2e3f4a5] 回落 {dropped} 行与内置默认相同的用户配置（消除冗余播种）")


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    op.drop_index("ix_system_settings_workspace", table_name="system_settings")
    for name, _cols, _where in _IDX_DEFS:
        op.drop_index(name, table_name="system_settings")

    # 旧约束要求 user_id 全表非空 ⇒ 三级新增行（系统级/工作区级）必须先删。
    bind.execute(sa.text("DELETE FROM system_settings WHERE user_id IS NULL"))

    if is_sqlite:
        with op.batch_alter_table("system_settings") as batch:
            batch.create_unique_constraint(
                "uq_system_settings_user_key", ["user_id", "key"]
            )
            batch.alter_column("user_id", existing_type=sa.Integer(), nullable=False)
            # workspace 列保留（无害空列）：省一次 SQLite 全表重建。
    else:
        op.create_unique_constraint(
            "uq_system_settings_user_key", "system_settings", ["user_id", "key"]
        )
        op.alter_column("system_settings", "user_id", existing_type=sa.Integer(), nullable=False)
