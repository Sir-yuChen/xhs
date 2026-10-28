"""merge draft_assets + publish_assets into xhs_publish_assets

Revision ID: a5b6c7d8e9f1
Revises: d6e7f8a9b0c1

2026-10-28 用户口径「分析功能类似重复表合并……全部为小红书私有表」：

`draft_assets`（草稿素材，有序）与 `publish_assets`（发布素材 + 上传态 4 列）
列几乎一一对应，差别只是发布侧多上传态。`send-to-publish` 原本把素材整批复制
一遍 ⇒ 同一份素材两张表各存一行。合并为 `xhs_publish_assets`，`row_type` 分两型：

* `row_type='draft'` ⇒ `parent_id` 指 `ai_drafts.id`
* `row_type='publish'` ⇒ `parent_id` 指 `publish_jobs.id`

列合并：`draft_assets.url` + `publish_assets.file_path` 归一为 `url` / `local_path`。
⚠️ 判据是 `row_type` 而非「parent_id 落在哪张表」—— 同表两种父，靠列值分型。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a5b6c7d8e9f1"
down_revision: Union[str, None] = "d6e7f8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_DRAFT = "draft_assets"
OLD_PUBLISH = "publish_assets"
NEW = "xhs_publish_assets"


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if NEW not in tables:
        op.create_table(
            NEW,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("row_type", sa.String(8), nullable=False, server_default="draft"),
            sa.Column("parent_id", sa.Integer(), nullable=False),
            sa.Column("asset_type", sa.String(32), nullable=False, server_default="image"),
            sa.Column("url", sa.Text(), nullable=False, server_default=""),
            sa.Column("local_path", sa.Text(), nullable=False, server_default=""),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("upload_status", sa.String(32), nullable=False, server_default="pending"),
            sa.Column("creator_media_id", sa.String(128), nullable=False, server_default=""),
            sa.Column("upload_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("creator_upload_info", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_xhs_publish_assets_row_type", NEW, ["row_type"])
        op.create_index("ix_xhs_publish_assets_parent_id", NEW, ["parent_id"])
        op.create_index("ix_xhs_publish_assets_parent", NEW, ["row_type", "parent_id"])
        op.create_index("ix_xhs_publish_assets_sort", NEW, ["parent_id", "sort_order"])

    # 原 id 保留：两张表的 id 可能撞车 ⇒ 草稿行保留原 id，发布行整体后移。
    tables = _tables(bind)
    if OLD_DRAFT in tables:
        op.execute(
            f"""
            INSERT INTO {NEW} (
                id, row_type, parent_id, asset_type, url, local_path, sort_order,
                upload_status, creator_media_id, upload_error, creator_upload_info, created_at
            )
            SELECT
                d.id, 'draft', d.draft_id, d.asset_type,
                COALESCE(d.url, ''), COALESCE(d.local_path, ''), COALESCE(d.sort_order, 0),
                'pending', '', '', '{{}}', NOW()
            FROM {OLD_DRAFT} d
            WHERE NOT EXISTS (SELECT 1 FROM {NEW} n WHERE n.id = d.id)
            """
        )

    if OLD_PUBLISH in tables:
        # 发布行新分配 id（避开草稿行已占的 id）。
        op.execute(
            f"""
            INSERT INTO {NEW} (
                row_type, parent_id, asset_type, url, local_path, sort_order,
                upload_status, creator_media_id, upload_error, creator_upload_info, created_at
            )
            SELECT
                'publish', p.publish_job_id, p.asset_type,
                '', COALESCE(p.file_path, ''), 0,
                COALESCE(p.upload_status, 'pending'), COALESCE(p.creator_media_id, ''),
                COALESCE(p.upload_error, ''), COALESCE(p.creator_upload_info, '{{}}'), NOW()
            FROM {OLD_PUBLISH} p
            WHERE NOT EXISTS (
                SELECT 1 FROM {NEW} n
                WHERE n.row_type = 'publish'
                  AND n.parent_id = p.publish_job_id
                  AND n.local_path = COALESCE(p.file_path, '')
                  AND n.asset_type = p.asset_type
            )
            """
        )

    op.execute(
        f"""
        SELECT setval(
            pg_get_serial_sequence('{NEW}', 'id'),
            GREATEST(COALESCE((SELECT MAX(id) FROM {NEW}), 1), 1)
        )
        """
    )

    tables = _tables(bind)
    if OLD_PUBLISH in tables:
        op.drop_table(OLD_PUBLISH)
    if OLD_DRAFT in tables:
        op.drop_table(OLD_DRAFT)


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if OLD_DRAFT not in tables:
        op.create_table(
            OLD_DRAFT,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "draft_id",
                sa.Integer(),
                sa.ForeignKey("ai_drafts.id"),
                nullable=False,
            ),
            sa.Column("asset_type", sa.String(32), nullable=False),
            sa.Column("url", sa.Text(), nullable=False, server_default=""),
            sa.Column("local_path", sa.Text(), nullable=False, server_default=""),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_index("ix_draft_assets_draft_id", OLD_DRAFT, ["draft_id"])

    if OLD_PUBLISH not in _tables(bind):
        op.create_table(
            OLD_PUBLISH,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "publish_job_id",
                sa.Integer(),
                sa.ForeignKey("publish_jobs.id"),
                nullable=False,
            ),
            sa.Column("asset_type", sa.String(32), nullable=False),
            sa.Column("file_path", sa.Text(), nullable=False),
            sa.Column("upload_status", sa.String(32), nullable=False, server_default="pending"),
            sa.Column("creator_media_id", sa.String(128), nullable=False, server_default=""),
            sa.Column("upload_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("creator_upload_info", sa.Text(), nullable=False, server_default="{}"),
        )
        op.create_index("ix_publish_assets_publish_job_id", OLD_PUBLISH, ["publish_job_id"])

    if NEW in _tables(bind):
        op.execute(
            f"""
            INSERT INTO {OLD_DRAFT} (id, draft_id, asset_type, url, local_path, sort_order)
            SELECT id, parent_id, asset_type, url, local_path, sort_order
            FROM {NEW}
            WHERE row_type = 'draft'
              AND NOT EXISTS (SELECT 1 FROM {OLD_DRAFT} o WHERE o.id = {NEW}.id)
            """
        )
        op.execute(
            f"""
            INSERT INTO {OLD_PUBLISH} (
                publish_job_id, asset_type, file_path, upload_status,
                creator_media_id, upload_error, creator_upload_info
            )
            SELECT
                parent_id, asset_type, local_path, upload_status,
                creator_media_id, upload_error, creator_upload_info
            FROM {NEW}
            WHERE row_type = 'publish'
              AND NOT EXISTS (
                  SELECT 1 FROM {OLD_PUBLISH} o
                  WHERE o.publish_job_id = {NEW}.parent_id
                    AND o.asset_type = {NEW}.asset_type
                    AND o.file_path = {NEW}.local_path
              )
            """
        )
        op.drop_table(NEW)
