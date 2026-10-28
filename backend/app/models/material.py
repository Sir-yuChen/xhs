"""本地素材库（设计 17 §3.3 / §3.4 / §6）。

语义：用户把本机已有的图片/视频文件夹**登记**进来（只存路径，扫描只读）。
⚠️ **硬约束 C2（只读边界）**：绝不写、不移动、不删除用户的源文件；
缩略图副本另存 `storage/materials/thumbs/`，删除文件夹索引也**不碰**用户文件。

⚠️ **与笔记媒体的分工**（DEF-044 同一语义只留一份）：
笔记媒体行在 `xhs_publish_assets` 的 `row_type='note'`；
本表是**素材库索引**，只是「用户磁盘上有哪些可用图」的目录，两者不互相写入。

2026-10-28：`material_folders` + `material_files` 合并为 `xhs_material_rows`，
`row_type` 分 `folder` / `file` 两型（单表继承）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


class MaterialRow(Base):
    """素材库行（**文件夹与文件同表**，`row_type` 分型，小红书私有 L1）。

    标准 1:N ⇒ 一张自引用父子表。`MaterialFolder` / `MaterialFile` 是两个子类
    （单表继承），**表只由本类定义**。

    ⚠️ 全部列都在本类声明：单表继承下子类加列会落到同一张表，
    分开写反而容易漏 NOT NULL 默认值。
    """

    __tablename__ = "xhs_material_rows"
    __table_args__ = (
        Index("ix_xhs_material_rows_user_path", "user_id", "path"),
        Index("ix_xhs_material_rows_parent_relpath", "parent_id", "rel_path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 行类型：`folder` / `file`。
    row_type: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_users.id"), index=True, nullable=False)
    #: 所属文件夹 id（`folder` 行恒 NULL）。
    parent_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)

    # ---- folder 行字段（`file` 行留空）----
    #: 文件夹别名（展示名；可与真实目录名不同，用户怎么认怎么起）。
    name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    #: **绝对路径**（只存路径字符串，不读文件内容落库）。
    path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: `False` = 停用（不扫描不展示），但**不物理删**索引行。
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: 最近一次成功扫描时间；NULL = 尚未扫描（面板显示「未扫描」而非「0 个文件」）。
    last_scan_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: 最近一次扫描的文件计数。
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ---- file 行字段（`folder` 行留空）----
    #: 文件名（含扩展名）。
    filename: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    #: `image` / `video`。
    file_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    #: 相对文件夹路径（防同名冲突；用 `/` 归一，跨平台稳定）。
    rel_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: 文件修改时间（**增量扫描判据**：未变则跳过重索引）。
    mtime: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: 缩略图相对 URL。空串 = 无缩略图（面板显示类型图标而非破图）。
    thumb_url: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __mapper_args__ = {"polymorphic_on": row_type, "polymorphic_identity": "row"}


class MaterialFolder(MaterialRow):
    """素材库文件夹子类 —— 只读写 `row_type='folder'` 的行。"""

    __mapper_args__ = {"polymorphic_identity": "folder"}


class MaterialFile(MaterialRow):
    """素材库文件索引子类 —— 只读写 `row_type='file'` 的行。

    ⚠️ **只索引、不搬移**：文件本体只在用户磁盘原位；缩略图存
    `storage/materials/thumbs/`。源文件删除 ⇒ 直接删本行（用户源文件不动）。
    """

    __mapper_args__ = {"polymorphic_identity": "file"}
