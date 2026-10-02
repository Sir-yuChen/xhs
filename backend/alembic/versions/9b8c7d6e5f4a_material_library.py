"""本地素材库建表：`material_folders` + `material_files`（设计 17 §3.3 / §3.4 / §6）

T3 落地（设计 17 §T3）的唯一迁移。零存量兼容、零回填 —— 两张**全新表**。

| 动作 | 对象 | 说明 |
|---|---|---|
| 建表 | `material_folders` | 用户登记的本地素材文件夹（§3.3），唯一键 `(user_id, path)` |
| 建表 | `material_files` | 文件夹内的图片/视频索引（§3.4），唯一键 `(folder_id, rel_path)` |

## 设计要点（迁移必须自己说清「为什么这样建」）

* **只读边界（硬约束 C2）**：本迁移只建**索引表**，不搬运任何用户文件。
  文件二进制本体始终在用户磁盘原位置；缩略图副本另存
  `backend/app/storage/materials/thumbs/`（磁盘目录由服务层按需创建，**不属于 schema**）。
* **`material_files` 无级联外键**（DB 层不写 `ON DELETE CASCADE`）：
  删文件夹时的清理由**服务层**显式执行（设计 17 §7.3「级联删除该文件夹的
  `material_files` 索引与缩略图副本」）—— 与一期 `note_goods` 的「业务层管关系」
  同款纪律（§26.7/§28：不靠 DB 级联，避免 SQLite `PRAGMA foreign_keys` 开关差异
  在不同连接上表现不一）。
* **时间戳一律带 `server_default`**：与 `f8a9b0c1d2e3` 同因 —— 裸 INSERT（迁移/脚本
  路径不走 ORM）时 `shanghai_now` 不生效，缺默认值会 `NOT NULL constraint failed`。

## 幂等（本项目硬纪律，设计 03 §9.5.1）

应用启动自动 `upgrade head`，SQLite DDL 不在事务里 ⇒ 中途失败会留「改了一半 +
版本没推进」。故建表 / 建唯一索引 / 建普通索引**各自独立判存在性**，可整体重跑。

## 可逆

`downgrade()` 逆序 drop 两张表。⚠️ 索引行**随表一起丢**是安全的 ——
本表只是「用户磁盘上有什么」的**缓存索引**，源文件在用户磁盘上**不受影响**，
重新扫描即可重建（这正是「索引」与「数据」的区别，不像 `model_configs` 的 drop
是不可逆的数据删除）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "9b8c7d6e5f4a"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None

_TABLE_FOLDERS = "material_folders"
_TABLE_FILES = "material_files"

_INDEX_FOLDERS_UNIQUE = "uq_material_folders_user_path"
_INDEX_FILES_UNIQUE = "uq_material_files_folder_relpath"


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": table},
    ).fetchone()
    return row is not None


def _index_exists(conn, index: str) -> bool:
    row = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='index' AND name=:n"),
        {"n": index},
    ).fetchone()
    return row is not None


def upgrade() -> None:
    conn = op.get_bind()

    # --------------------------------- ① material_folders 建表 #
    if not _table_exists(conn, _TABLE_FOLDERS):
        op.create_table(
            _TABLE_FOLDERS,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            #: 绝对路径（Text：Windows 长效路径可超 260 字符）
            sa.Column("path", sa.Text(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            #: NULL = 尚未扫描（面板据此显示「未扫描」而非「0 个文件」）
            sa.Column("last_scan_at", sa.DateTime(), nullable=True),
            sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            f"ix_{_TABLE_FOLDERS}_user_id",
            _TABLE_FOLDERS,
            ["user_id"],
        )

    # 唯一约束单独判存在性（表在、约束缺的中间态要能收敛）
    if not _index_exists(conn, _INDEX_FOLDERS_UNIQUE):
        op.create_index(
            _INDEX_FOLDERS_UNIQUE,
            _TABLE_FOLDERS,
            ["user_id", "path"],
            unique=True,
        )

    # ---------------------------------- ② material_files 建表 #
    if not _table_exists(conn, _TABLE_FILES):
        op.create_table(
            _TABLE_FILES,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("folder_id", sa.Integer(), nullable=False),
            sa.Column("filename", sa.String(length=255), nullable=False),
            #: image / video
            sa.Column("file_type", sa.String(length=16), nullable=False),
            #: 相对文件夹路径（防同名冲突；`/` 归一化）
            sa.Column("rel_path", sa.String(length=512), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
            #: 增量扫描判据
            sa.Column("mtime", sa.DateTime(), nullable=False),
            #: 图片尺寸（视频为空 —— 项目无 opencv）
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            #: 空串 = 无缩略图（视频占位，面板显示类型图标）
            sa.Column("thumb_url", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(f"ix_{_TABLE_FILES}_user_id", _TABLE_FILES, ["user_id"])
        op.create_index(f"ix_{_TABLE_FILES}_folder_id", _TABLE_FILES, ["folder_id"])
        #: mtime 索引：增量扫描与「按时间排序」都吃它
        op.create_index(f"ix_{_TABLE_FILES}_mtime", _TABLE_FILES, ["mtime"])

    if not _index_exists(conn, _INDEX_FILES_UNIQUE):
        op.create_index(
            _INDEX_FILES_UNIQUE,
            _TABLE_FILES,
            ["folder_id", "rel_path"],
            unique=True,
        )


def downgrade() -> None:
    conn = op.get_bind()

    # 逆序 drop（先子表后父表；虽无 DB 级联，语义上仍是「文件的归属是文件夹」）
    if _table_exists(conn, _TABLE_FILES):
        for idx in (
            _INDEX_FILES_UNIQUE,
            f"ix_{_TABLE_FILES}_mtime",
            f"ix_{_TABLE_FILES}_folder_id",
            f"ix_{_TABLE_FILES}_user_id",
        ):
            if _index_exists(conn, idx):
                op.drop_index(idx, table_name=_TABLE_FILES)
        op.drop_table(_TABLE_FILES)

    if _table_exists(conn, _TABLE_FOLDERS):
        for idx in (_INDEX_FOLDERS_UNIQUE, f"ix_{_TABLE_FOLDERS}_user_id"):
            if _index_exists(conn, idx):
                op.drop_index(idx, table_name=_TABLE_FOLDERS)
        op.drop_table(_TABLE_FOLDERS)
