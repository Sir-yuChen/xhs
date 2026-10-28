"""merge note_assets + aigc_media_assets into xhs_publish_assets; material tables into rows

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1

2026-10-28 用户口径「能合并的合并，参考之前合并的经验」：

1. `note_assets`（134 行）→ `xhs_publish_assets` 的 `row_type='note'`，父 = `notes.id`。
   两表列逐列对应，且建草稿时本就是整批复制 ⇒ 真重复。
2. `aigc_media_assets`（42 行，**全部**挂在已删的生成记录上）→ `row_type='aigc'`，
   父 = `xhs_generation_records.id`。列是子集 + 生成态几列（prompt/model/item_id）。
3. `material_folders`(1) + `material_files`(4) → `xhs_material_rows` 父子表
   （`row_type='folder'/'file'`）。标准 1:N。

⚠️ 判据仍是 `row_type`，不用 `parent_id IS NULL`：`xhs_publish_assets` 有 4 种父、
`xhs_material_rows` 的 `parent_id` 对 folder 行恒 NULL（顶层），二者都靠列值分型。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, None] = "b6c7d8e9f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ASSETS = "xhs_publish_assets"
MAT_ROWS = "xhs_material_rows"
OLD_NOTE_ASSETS = "note_assets"
OLD_AIGC = "aigc_media_assets"
OLD_MAT_FOLDERS = "material_folders"
OLD_MAT_FILES = "material_files"


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, t: str) -> set[str]:
    insp = sa.inspect(bind)
    if t not in set(insp.get_table_names()):
        return set()
    return {c["name"] for c in insp.get_columns(t)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    # ---- 1. xhs_publish_assets 补 4 型所需列 ----
    if ASSETS in tables:
        cols = _columns(bind, ASSETS)
        additions = [
            ("user_id", sa.Integer()),
            ("prompt", sa.Text()),
            ("model_name", sa.String(128)),
            ("params", sa.Text()),
            ("item_id", sa.String(128)),
            ("platform_account_id", sa.Integer()),
            ("aigc_task_id", sa.String(128)),
        ]
        for name, typ in additions:
            if name not in cols:
                op.add_column(ASSETS, sa.Column(name, typ, nullable=True))
        op.execute(f"UPDATE {ASSETS} SET prompt = '' WHERE prompt IS NULL")
        op.execute(f"UPDATE {ASSETS} SET model_name = '' WHERE model_name IS NULL")
        op.execute(f"UPDATE {ASSETS} SET params = '{{}}' WHERE params IS NULL")
        op.execute(f"UPDATE {ASSETS} SET item_id = '' WHERE item_id IS NULL")
        op.execute(f"UPDATE {ASSETS} SET aigc_task_id = '' WHERE aigc_task_id IS NULL")
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_xhs_publish_assets_user_id "
            f"ON {ASSETS} (user_id)"
        )

    # ---- 2. 搬 note_assets ----
    if OLD_NOTE_ASSETS in _tables(bind):
        op.execute(
            f"""
            INSERT INTO {ASSETS} (
                row_type, parent_id, asset_type, url, local_path, sort_order,
                upload_status, creator_media_id, upload_error, creator_upload_info,
                prompt, model_name, params, item_id, aigc_task_id, created_at
            )
            SELECT
                'note', a.note_id, a.asset_type, COALESCE(a.url, ''), COALESCE(a.local_path, ''),
                COALESCE(a.sort_order, 0), 'pending', '', '', '{{}}', '', '', '{{}}', '', '', NOW()
            FROM {OLD_NOTE_ASSETS} a
            WHERE NOT EXISTS (
                SELECT 1 FROM {ASSETS} n
                WHERE n.row_type = 'note' AND n.parent_id = a.note_id
                  AND COALESCE(n.url, '') = COALESCE(a.url, '')
                  AND COALESCE(n.local_path, '') = COALESCE(a.local_path, '')
            )
            """
        )

    # ---- 3. 搬 aigc_media_assets ----
    if OLD_AIGC in _tables(bind):
        op.execute(
            f"""
            INSERT INTO {ASSETS} (
                row_type, parent_id, user_id, asset_type, url, local_path, sort_order,
                upload_status, creator_media_id, upload_error, creator_upload_info,
                prompt, model_name, params, item_id, platform_account_id, aigc_task_id, created_at
            )
            SELECT
                'aigc', COALESCE(g.generation_record_id, 0), g.user_id, g.asset_type,
                COALESCE(g.url, ''), COALESCE(g.local_path, ''), 0,
                'pending', '', '', '{{}}',
                COALESCE(g.prompt, ''), COALESCE(g.model_name, ''),
                '{{}}', COALESCE(g.item_id, ''),
                g.platform_account_id, COALESCE(g.aigc_task_id, ''), NOW()
            FROM {OLD_AIGC} g
            WHERE NOT EXISTS (
                SELECT 1 FROM {ASSETS} n
                WHERE n.row_type = 'aigc' AND n.parent_id = COALESCE(g.generation_record_id, 0)
                  AND COALESCE(n.local_path, '') = COALESCE(g.local_path, '')
            )
            """
        )

    # ---- 4. material 两表 → xhs_material_rows 父子表 ----
    if MAT_ROWS not in _tables(bind):
        op.create_table(
            MAT_ROWS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("row_type", sa.String(8), nullable=False, server_default="file"),
            sa.Column(
                "parent_id",
                sa.Integer(),
                sa.ForeignKey(f"{MAT_ROWS}.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("user_id", sa.Integer(), nullable=True),
            # folder 行
            sa.Column("name", sa.String(256), nullable=False, server_default=""),
            sa.Column("path", sa.Text(), nullable=False, server_default=""),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("last_scan_at", sa.DateTime(), nullable=True),
            sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
            # file 行
            sa.Column("filename", sa.String(256), nullable=False, server_default=""),
            sa.Column("file_type", sa.String(32), nullable=False, server_default=""),
            sa.Column("rel_path", sa.Text(), nullable=False, server_default=""),
            sa.Column("size_bytes", sa.BigInteger(), nullable=True),
            sa.Column("mtime", sa.DateTime(), nullable=True),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.Column("thumb_url", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_xhs_material_rows_row_type", MAT_ROWS, ["row_type"])
        op.create_index("ix_xhs_material_rows_parent", MAT_ROWS, ["parent_id"])
        op.create_index("ix_xhs_material_rows_user", MAT_ROWS, ["user_id"])

    if OLD_MAT_FOLDERS in _tables(bind):
        # 文件夹 id 保留原值，文件行 parent_id 直接复用。
        op.execute(
            f"""
            INSERT INTO {MAT_ROWS} (
                id, row_type, parent_id, user_id, name, path, enabled,
                last_scan_at, file_count, created_at, updated_at
            )
            SELECT
                f.id, 'folder', NULL, f.user_id, f.name, f.path, COALESCE(f.enabled, true),
                f.last_scan_at, COALESCE(f.file_count, 0), f.created_at, f.updated_at
            FROM {OLD_MAT_FOLDERS} f
            WHERE NOT EXISTS (SELECT 1 FROM {MAT_ROWS} n WHERE n.id = f.id)
            """
        )
        op.execute(
            f"""SELECT setval(pg_get_serial_sequence('{MAT_ROWS}','id'),
                 GREATEST(COALESCE((SELECT MAX(id) FROM {MAT_ROWS}),1),1))"""
        )

    if OLD_MAT_FILES in _tables(bind):
        op.execute(
            f"""
            INSERT INTO {MAT_ROWS} (
                row_type, parent_id, user_id, filename, file_type, rel_path,
                size_bytes, mtime, width, height, thumb_url, created_at
            )
            SELECT
                'file', f.folder_id, f.user_id, f.filename, f.file_type, f.rel_path,
                f.size_bytes, f.mtime, f.width, f.height, COALESCE(f.thumb_url, ''), f.created_at
            FROM {OLD_MAT_FILES} f
            WHERE NOT EXISTS (
                SELECT 1 FROM {MAT_ROWS} n
                WHERE n.row_type = 'file' AND n.rel_path = f.rel_path
                  AND n.user_id = f.user_id
            )
            """
        )

    # ---- 5. 删旧表 ----
    for old in (OLD_NOTE_ASSETS, OLD_AIGC, OLD_MAT_FILES, OLD_MAT_FOLDERS):
        if old in _tables(bind):
            op.drop_table(old)


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if OLD_NOTE_ASSETS not in tables:
        op.create_table(
            OLD_NOTE_ASSETS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("note_id", sa.Integer(), sa.ForeignKey("notes.id"), nullable=False),
            sa.Column("asset_type", sa.String(32), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("local_path", sa.Text(), nullable=False, server_default=""),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        )
        op.execute(
            f"""
            INSERT INTO {OLD_NOTE_ASSETS} (note_id, asset_type, url, local_path, sort_order)
            SELECT parent_id, asset_type, COALESCE(url, ''), COALESCE(local_path, ''),
                   COALESCE(sort_order, 0)
            FROM {ASSETS} WHERE row_type = 'note'
            """
        )

    if OLD_AIGC not in _tables(bind):
        op.create_table(
            OLD_AIGC,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("generation_record_id", sa.Integer(), nullable=True),
            sa.Column("asset_type", sa.String(16), nullable=False),
            sa.Column("local_path", sa.Text(), nullable=False, server_default=""),
            sa.Column("url", sa.Text(), nullable=True),
            sa.Column("aigc_task_id", sa.Text(), nullable=False, server_default=""),
            sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
            sa.Column("model_name", sa.String(128), nullable=False, server_default=""),
            sa.Column("item_id", sa.String(64), nullable=True),
            sa.Column("platform_account_id", sa.Integer(), nullable=True),
            sa.Column("params", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.execute(
            f"""
            INSERT INTO {OLD_AIGC} (
                user_id, generation_record_id, asset_type, local_path, url, aigc_task_id,
                prompt, model_name, item_id, platform_account_id, created_at
            )
            SELECT user_id, NULLIF(parent_id, 0), asset_type, COALESCE(local_path, ''),
                   NULLIF(url, ''), COALESCE(aigc_task_id, ''), COALESCE(prompt, ''),
                   COALESCE(model_name, ''), NULLIF(item_id, ''), platform_account_id, created_at
            FROM {ASSETS} WHERE row_type = 'aigc'
            """
        )

    if OLD_MAT_FOLDERS not in _tables(bind):
        op.create_table(
            OLD_MAT_FOLDERS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("name", sa.String(256), nullable=False, server_default=""),
            sa.Column("path", sa.Text(), nullable=False, server_default=""),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("last_scan_at", sa.DateTime(), nullable=True),
            sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_table(
            OLD_MAT_FILES,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("folder_id", sa.Integer(), nullable=True),
            sa.Column("filename", sa.String(256), nullable=False, server_default=""),
            sa.Column("file_type", sa.String(32), nullable=False, server_default=""),
            sa.Column("rel_path", sa.Text(), nullable=False, server_default=""),
            sa.Column("size_bytes", sa.BigInteger(), nullable=True),
            sa.Column("mtime", sa.DateTime(), nullable=True),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.Column("thumb_url", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
    if MAT_ROWS in _tables(bind):
        op.execute(
            f"""
            INSERT INTO {OLD_MAT_FOLDERS} (id, user_id, name, path, enabled, last_scan_at, file_count, created_at, updated_at)
            SELECT id, user_id, name, path, COALESCE(enabled, true), last_scan_at,
                   COALESCE(file_count, 0), created_at, updated_at
            FROM {MAT_ROWS} WHERE row_type = 'folder'
            """
        )
        op.execute(
            f"""
            INSERT INTO {OLD_MAT_FILES} (user_id, folder_id, filename, file_type, rel_path,
                                         size_bytes, mtime, width, height, thumb_url, created_at)
            SELECT user_id, parent_id, filename, file_type, rel_path, size_bytes, mtime,
                   width, height, COALESCE(thumb_url, ''), created_at
            FROM {MAT_ROWS} WHERE row_type = 'file'
            """
        )
        op.drop_table(MAT_ROWS)

    # 合并表补的列反向删掉（多型共表时列不能删，保留）
