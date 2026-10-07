"""add_note_category: 「我的笔记」离线数据的笔记分类（商业 / 普通）

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-10-06

## 为什么（2026-10-06 需求「我的笔记页面重构」）

> 「笔记类型与小红书平台一致分为商业笔记和普通笔记」

「商业笔记」= 千帆数据中心 `note-data/goods`（挂载商品的笔记）；「普通笔记」=
`note-data/normal`。两类数据由**下载入口**区分（不是「笔记类型」字段——那个是
图文/视频）。离线落库时需要把这个维度持久化到 `notes.note_category`，供「我的笔记」
页商业 / 普通两个 Tab 查本地库。

## 兼容性

* 纯加一列 `note_category`（`goods` / `normal` / 空）。
* **存量行不回填**：既有的线上同步 / 采集 / 本地生成笔记没有「商业/普通」这个维度，
  留空（缺值口径：空 = 不适用，不是「没解析到」）。
* `downgrade` drop 该列。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f4a5b6c7d8e9"
down_revision: Union[str, Sequence[str], None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    conn = op.get_bind()
    cols = _columns(conn, "notes")
    if "note_category" not in cols:
        op.add_column(
            "notes",
            sa.Column("note_category", sa.String(length=16), nullable=False, server_default=""),
        )


def downgrade() -> None:
    conn = op.get_bind()
    cols = _columns(conn, "notes")
    if "note_category" in cols:
        op.drop_column("notes", "note_category")
