"""本地素材库（设计 17 §3.3 / §3.4 / §6，v2.x 新增）。

语义：用户把本机已有的图片/视频文件夹**登记**进来（只存路径，扫描只读）。
⚠️ **硬约束 C2（只读边界）**：绝不写、不移动、不删除用户的源文件；
缩略图副本另存 `storage/materials/thumbs/`，删除文件夹索引也**不碰**用户文件。

⚠️ **与 `notes.note_assets` 的分工**（§3.3 L172，DEF-044 同一语义只留一份）：
- `note_assets` = **笔记媒体**的唯一落库（生成产物 / 发布用）；
- `material_files` = **素材库索引**，只是「用户磁盘上有哪些可用图」的目录。
两者不互相写入，生成时从素材库**引用**到笔记媒体。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now


class MaterialFolder(Base):
    """素材库文件夹（设计 17 §3.3）。

    用户配置的本地素材文件夹（**绝对路径**），扫描只读、不改用户文件。
    `enabled=False` = 停用（不扫描不展示），但**不物理删** `material_files` 索引行 ——
    重新启用即恢复，避免「关一下再开」要重扫全盘。
    """

    __tablename__ = "material_folders"
    __table_args__ = (
        #: 同 user 的路径唯一，防重复扫描（§3.3 L269）。
        UniqueConstraint("user_id", "path", name="uq_material_folders_user_path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    #: 文件夹别名（展示名；可与真实目录名不同，用户怎么认怎么起）
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: **绝对路径**（只存路径字符串，不读文件内容落库）
    path: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: 最近一次成功扫描时间；NULL = 尚未扫描（面板据此显示「未扫描」而非「0 个文件」）
    last_scan_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: 最近一次扫描的文件计数（面板显示用；与实体索引行数同源）
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=shanghai_now, onupdate=shanghai_now
    )


class MaterialFile(Base):
    """素材库文件索引（设计 17 §3.4）。

    ⚠️ **只索引、不搬移**：文件二进制本体只在用户磁盘原位置；
    缩略图副本存 `storage/materials/thumbs/`（`thumb_url` 指向它）。
    ⚠️ `mtime` 是**增量扫描判据**：mtime 未变则跳过重索引；
    源文件删除 ⇒ 删索引行（**软删语义 = 直接删本行**，用户源文件不动）。
    """

    __tablename__ = "material_files"
    __table_args__ = (
        #: 同一文件夹内相对路径唯一（§3.4 L289）。
        UniqueConstraint("folder_id", "rel_path", name="uq_material_files_folder_relpath"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    folder_id: Mapped[int] = mapped_column(
        ForeignKey("material_folders.id"), index=True, nullable=False
    )
    #: 文件名（含扩展名）
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    #: `image` / `video`（对齐一期 `ALLOWED_UPLOAD_EXTENSIONS` 的类型划分，§6.1）
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 相对文件夹路径（防同名冲突、面板展示用；用 `/` 归一化，跨平台稳定）
    rel_path: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 文件修改时间（增量扫描判据）
    mtime: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True, default=shanghai_now)
    #: 图片尺寸（Pillow 读；视频为空 —— 项目无 opencv，不引新依赖，§6.1）
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: 缩略图相对 URL（`/api/files/materials/thumbs/xxx`）。
    #: ⚠️ 空串 = 无缩略图（视频无首帧能力时的占位，面板显示类型图标而非破图）。
    thumb_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=shanghai_now)
