"""prompt modules/templates + generation records/aigc assets/note links (LLM 生成链路 T2+T5)

设计 17 §3.1 / §3.2 / §3.5 / §3.6 / §3.7（v1.7 定稿）。T2 交付「模板两层结构可管理」，
T5 交付「生成审计可追溯」。两条任务共用一个迁移（同批落地、单一链尾，避免中间态）。

## 五张表

| 表 | 语义 |
|---|---|
| `prompt_modules` | 公共提示词模块（可复用片段，按 category 组织，`sort_order` 定拼接序） |
| `prompt_templates` | 提示词模板（含 `{商品字段}` 插槽的骨架 + 引用模块 id 列表 + 版本） |
| `generation_records` | LLM 生成审计（谁在何时用哪个供应商/模板生成什么；失败可重试） |
| `aigc_media_assets` | AIGC 媒体溯源（`aigc_task_id` 平台合规要求；不污染 `note_assets`） |
| `note_generation_links` | 生成笔记 ↔ 商品（**本地主键级**，与一期平台 ID 级 `note_goods` 并存） |

## 幂等（本项目硬纪律，设计 03 §9.5.1）

应用启动时自动 `upgrade head`，而 SQLite 的 DDL **不在事务里** ⇒ 中途失败会留下
「建了一半 + `alembic_version` 没推进」，下次启动从同一条 revision 重跑。
因此 `upgrade()` 先查 `PRAGMA table_info`：表已存在就直接返回。

## 可逆

`downgrade()` 按依赖序逆删：`note_generation_links` / `aigc_media_assets`
（引用 `generation_records`）→ `generation_records` → `prompt_templates`
（引用 `prompt_modules`）→ `prompt_modules`。

## 唯一约束

- `prompt_modules`：`(user_id, name)` —— user 内模块名唯一（§3.1）。
- `note_generation_links`：`(note_pk, shop_product_id)` —— 同笔记同商品只记一行（§3.7）。

⚠️ `prompt_templates` 「按 (user_id, task_type) 唯一默认」是**写时清旧**的**业务不变量**，
不是数据库唯一约束（同类型允许多模板，只有一个 `is_default=True`），故此处不建索引约束。

⚠️ FK 一律不带 `ondelete` 级联：本项目迁移风格为裸列 + 应用层归属/引用校验
（见 `m4n5o6p7q8r9`，FK 在 ORM 模型声明、迁移层不建约束）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

from backend.app.core.migration_introspect import column_names

revision = "c4d5e6f7a8b9"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None

_TABLE_MODULES = "prompt_modules"
_TABLE_TEMPLATES = "prompt_templates"
_TABLE_GEN_RECORDS = "generation_records"
_TABLE_AIGC_ASSETS = "aigc_media_assets"
_TABLE_GEN_LINKS = "note_generation_links"


def _columns(conn, table: str) -> set[str]:
    """表的列名集合（表不存在 ⇒ 空集）。"""
    #: v1.1.0 PG 化：原为 `PRAGMA table_info`（SQLite 专属），统一走内省助手。
    return column_names(conn, table)


def upgrade() -> None:
    conn = op.get_bind()

    # ---------------------------------------------------------------- T2 #
    if not _columns(conn, _TABLE_MODULES):
        op.create_table(
            _TABLE_MODULES,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("category", sa.String(length=64), nullable=False),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "name", name="uq_prompt_modules_user_name"),
        )
        op.create_index("ix_prompt_modules_user_id", _TABLE_MODULES, ["user_id"])
        op.create_index("ix_prompt_modules_category", _TABLE_MODULES, ["category"])

    if not _columns(conn, _TABLE_TEMPLATES):
        op.create_table(
            _TABLE_TEMPLATES,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("task_type", sa.String(length=32), nullable=False),
            sa.Column("prompt_skeleton", sa.Text(), nullable=False, server_default=""),
            sa.Column("module_ids", sa.JSON(), nullable=True),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_prompt_templates_user_id", _TABLE_TEMPLATES, ["user_id"])
        op.create_index("ix_prompt_templates_task_type", _TABLE_TEMPLATES, ["task_type"])

    # ---------------------------------------------------------------- T5 #
    if not _columns(conn, _TABLE_GEN_RECORDS):
        op.create_table(
            _TABLE_GEN_RECORDS,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("task_id", sa.Integer(), nullable=True),
            sa.Column("shop_product_id", sa.Integer(), nullable=True),
            sa.Column("template_id", sa.Integer(), nullable=True),
            sa.Column("gateway_config_id", sa.Integer(), nullable=True),
            sa.Column("model_name", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("media_source", sa.String(length=32), nullable=False, server_default="product_image"),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
            sa.Column("title", sa.Text(), nullable=False, server_default=""),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column("topics_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("error_code", sa.String(length=64), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("ai_generated_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_generation_records_user_id", _TABLE_GEN_RECORDS, ["user_id"])
        op.create_index("ix_generation_records_task_id", _TABLE_GEN_RECORDS, ["task_id"])
        op.create_index(
            "ix_generation_records_shop_product_id", _TABLE_GEN_RECORDS, ["shop_product_id"]
        )
        op.create_index(
            "ix_generation_records_gateway_config_id", _TABLE_GEN_RECORDS, ["gateway_config_id"]
        )
        op.create_index("ix_generation_records_status", _TABLE_GEN_RECORDS, ["status"])

    if not _columns(conn, _TABLE_AIGC_ASSETS):
        op.create_table(
            _TABLE_AIGC_ASSETS,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("generation_record_id", sa.Integer(), nullable=True),
            sa.Column("asset_type", sa.String(length=16), nullable=False),
            sa.Column("local_path", sa.Text(), nullable=False, server_default=""),
            sa.Column("url", sa.Text(), nullable=True),
            sa.Column("aigc_task_id", sa.Text(), nullable=False, server_default=""),
            sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
            sa.Column("model_name", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_aigc_media_assets_user_id", _TABLE_AIGC_ASSETS, ["user_id"])
        op.create_index(
            "ix_aigc_media_assets_generation_record_id", _TABLE_AIGC_ASSETS, ["generation_record_id"]
        )

    if not _columns(conn, _TABLE_GEN_LINKS):
        op.create_table(
            _TABLE_GEN_LINKS,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("note_pk", sa.Integer(), nullable=False),
            sa.Column("shop_product_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "note_pk", "shop_product_id", name="uq_note_generation_links_note_product"
            ),
        )
        op.create_index("ix_note_generation_links_user_id", _TABLE_GEN_LINKS, ["user_id"])
        op.create_index("ix_note_generation_links_note_pk", _TABLE_GEN_LINKS, ["note_pk"])
        op.create_index(
            "ix_note_generation_links_shop_product_id", _TABLE_GEN_LINKS, ["shop_product_id"]
        )


def downgrade() -> None:
    # 按依赖序逆删：引用方先走
    op.drop_table(_TABLE_GEN_LINKS)
    op.drop_table(_TABLE_AIGC_ASSETS)
    op.drop_table(_TABLE_GEN_RECORDS)
    op.drop_table(_TABLE_TEMPLATES)
    op.drop_table(_TABLE_MODULES)
