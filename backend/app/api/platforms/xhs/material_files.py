"""素材文件端点（设计 17 §7.3 补章，v2.31）。

## 为什么单独成文件（不是塞进 `materials.py`）

`materials.py` 已有 8 个端点、文件头声明着「**绝不**删用户源文件」的只读边界。
本文件的三个端点是**新的语义**（会动用户磁盘），必须**显式另立门户**：

> **用户口径（2026-10-03）**：「按照你推荐的真删文件，但删得可逆执行」
> 「素材文件区域添加，删除，批量删除，大图预览」「删除会真实删除本地文件，添加提示二次校验」

## 边界变更（**显式记录，不静默改口**）

| | v2.30 及之前 | v2.31 起 |
|---|---|---|
| 删文件夹 | 只删索引行，**源文件不动** | **不变**（文件夹删除仍不动源文件） |
| 删文件 | **没有这个能力** | **真删源文件，但进回收站**（可逆） |

⚠️ 「可逆」不是修辞：走 `send2trash` ⇒ 用户在资源管理器里**能捞回来**。
绝不用 `os.unlink`（那是不可逆的，误删一个空格就没救了）。

## 三条设计决定（都写在代码里，便于日后回溯）

1. **删除 = 移到回收站**（用户口径「真删但可逆」）—— Windows 回收站 / macOS 废纸篓 /
   Linux `~/.local/share/Trash`。
2. **允许删任何文件**（包括不是本系统生成的）—— 目录是用户的，他说删就删；
   但响应里**如实标出 `managed` 来源**，前端在二次确认里显示（用户要知情）。
3. **批量上限 50**（`MATERIAL_DELETE_BATCH_LIMIT`）—— 防一次误勾整库。
   超限**整体拒绝**（不部分执行）—— 部分成功会让用户不知道到底删了几张。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models.material import MaterialFile, MaterialFolder
from backend.app.models.user import User
from backend.app.services.material_scan_service import thumbs_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xhs/materials", tags=["xhs-materials"])

#: 单次批量删除上限（用户口径要批量删，但一次误勾整库不可接受）
MATERIAL_DELETE_BATCH_LIMIT = 50

#: 上传大小上限（50 MB）—— 素材是图 / 短视频，再大就不该进这个区域
MATERIAL_UPLOAD_MAX_BYTES = 50 * 1024 * 1024

#: 允许上传的 MIME 前缀（图 / 视频；与 `material_files.file_type` 的两种取值对齐）
_ALLOWED_UPLOAD_PREFIXES = ("image/", "video/")

#: 上传文件名里**绝不能**出现的（路径穿越的两种基本形态）
_FORBIDDEN_NAME_CHARS = ("/", "\\", "\x00")


def _err(code: str, message: str, http_status: int) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"code": code, "message": message})


def _get_owned_file(db: Session, file_id: int, user: User) -> MaterialFile:
    """按归属取文件行（**第一条 SQL 就带 `user_id`** —— 与 `materials.py` 同款纪律）。

    不用「先查后判 `row.user_id != me`」两段式：多一次查询，且容易漏判。
    """
    row = db.scalar(
        select(MaterialFile).where(
            MaterialFile.id == file_id,
            MaterialFile.user_id == user.id,
        )
    )
    if row is None:
        raise _err("MATERIAL_FILE_NOT_FOUND", "素材文件不存在", status.HTTP_404_NOT_FOUND)
    return row


def _get_owned_folder(db: Session, folder_id: int, user: User) -> MaterialFolder:
    folder = db.scalar(
        select(MaterialFolder).where(
            MaterialFolder.id == folder_id,
            MaterialFolder.user_id == user.id,
        )
    )
    if folder is None:
        raise _err("MATERIAL_FOLDER_NOT_FOUND", "素材文件夹不存在", status.HTTP_404_NOT_FOUND)
    return folder


def _to_recycle_bin(target: Path) -> bool:
    """把文件/目录移进**回收站**（不是 `unlink`）。

    返回是否真的移走了。`send2trash` 缺失时**抛错而不是回落 `unlink`** ——
    「可逆」是用户明确要的性质（口径「真删文件，但删得可逆执行」），
    悄悄退化成不可逆删除是**比报错更坏的失败**（用户以为还能捞）。
    """
    if not target.exists():
        return False
    try:
        from send2trash import send2trash  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover —— 部署缺依赖时给出可执行提示
        raise _err(
            "MATERIAL_TRASH_UNAVAILABLE",
            "删除组件未安装（send2trash）—— 无法保证可恢复，已中止删除",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        ) from exc

    try:
        send2trash(str(target))
    except OSError as exc:
        raise _err(
            "MATERIAL_DELETE_FAILED",
            f"移入回收站失败：{exc}",
            status.HTTP_409_CONFLICT,
        ) from exc
    return True


def _file_abs_path(row: MaterialFile, folder: MaterialFolder) -> Path:
    """文件的**绝对路径**（`rel_path` 相对文件夹根）。

    ⚠️ 不在这里做「安全路径」校验：`rel_path` 是**扫描时自己写进索引的**
    （不是请求参数），扫描侧已经做过黑名单校验。这里做二次校验 = 两个实现。
    """
    return Path(folder.path) / row.rel_path


def _drop_index_row(db: Session, row: MaterialFile) -> None:
    """删索引行 + 缩略图副本（**不动源文件** —— 源文件由调用方决定怎么处理）。"""
    db.delete(row)
    # 缩略图副本：按 `thumb_url` 反推文件名（`thumb_url` 形如 `/api/xhs/materials/folders/{fid}/thumbs/{name}`）
    if row.thumb_url:
        try:
            name = row.thumb_url.rstrip("/").rsplit("/", 1)[-1]
            thumb = thumbs_dir() / str(row.folder_id) / name
            if thumb.is_file():
                thumb.unlink()
        except BaseException as exc:  # noqa: BLE001 —— 副本残留不影响正确性
            logger.warning("素材删除：缩略图清理失败 file=%s: %s", row.id, exc)


class MaterialFileDeleteBatch(BaseModel):
    """`POST /files/delete-batch` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    # ⚠️ **不在这里限 `max_length`**：pydantic 拒绝会返回 422 + 一堆英文校验细节，
    #    用户看到的是「List should have at most 50 items」而不是能自查的中文原因。
    #    上限校验放**应用层**，返回结构化 `MATERIAL_DELETE_BATCH_TOO_LARGE`（含实际个数）。
    file_ids: list[int] = Field(min_length=1)


@router.delete("/files/{file_id}")
def delete_material_file(
    file_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """删除一个素材文件（**真删源文件，但进回收站**，v2.31）。

    ⚠️ 与「删除文件夹」**语义不同**（那是只删索引）：用户口径明确要求
    「删除会真实删除本地文件」，同时「删得可逆」。
    """
    row = _get_owned_file(db, file_id, current_user)
    folder = _get_owned_folder(db, row.folder_id, current_user)
    target = _file_abs_path(row, folder)

    moved = _to_recycle_bin(target)
    _drop_index_row(db, row)
    db.commit()

    return {
        "ok": True,
        "id": file_id,
        "filename": row.filename,
        # 如实交代：文件不在磁盘上（可能已被用户手工删掉）时不谎报「已删除」
        "trashed": moved,
        "path": str(target),
    }


@router.post("/files/delete-batch")
def delete_material_files_batch(
    payload: MaterialFileDeleteBatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """批量删除（**上限 50**，超限整体拒绝 —— 不部分执行）。

    ⚠️ 「超限整体拒绝」是刻意的：部分成功会让用户**不知道到底删了几张**
    （页面显示「已删 50 张」而实际他想删 80 张 ⇒ 剩下 30 张静默留着，
    而用户以为删干净了）。宁可整体报错重来。
    """
    ids = list(dict.fromkeys(payload.file_ids))  # 去重保序（同一个 id 传两次不该删两次）
    if len(ids) > MATERIAL_DELETE_BATCH_LIMIT:
        raise _err(
            "MATERIAL_DELETE_BATCH_TOO_LARGE",
            f"一次最多删除 {MATERIAL_DELETE_BATCH_LIMIT} 个（本次 {len(ids)} 个）—— 请分批删除",
            status.HTTP_400_BAD_REQUEST,
        )

    rows = db.scalars(
        select(MaterialFile).where(
            MaterialFile.id.in_(ids),
            MaterialFile.user_id == current_user.id,
        )
    ).all()
    by_id = {row.id: row for row in rows}

    # 归属过滤掉的不算错误（可能已被别处删掉），但要如实报数
    deleted: list[dict] = []
    skipped: list[int] = []
    for file_id in ids:
        row = by_id.get(file_id)
        if row is None:
            skipped.append(file_id)
            continue
        folder = _get_owned_folder(db, row.folder_id, current_user)
        target = _file_abs_path(row, folder)
        moved = _to_recycle_bin(target)
        _drop_index_row(db, row)
        deleted.append({"id": file_id, "filename": row.filename, "trashed": moved})

    db.commit()
    return {"ok": True, "deleted": len(deleted), "items": deleted, "skipped": skipped}


@router.post("/files/upload", status_code=status.HTTP_201_CREATED)
async def upload_material_file(
    folder_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """上传一个文件进素材文件夹（v2.31）。

    三条校验（都**先校验后落盘** —— 不产生「垃圾文件已落盘但请求失败」的中间态）：

    ① **文件夹归属**（别人的文件夹不给自己写）；
    ② **文件名**不含路径分隔符 / `..`（**穿越防线**：`../../x` 会把文件写到文件夹外）；
    ③ **Content-Type** 必须是 `image/*` 或 `video/*`（本区域只收这两类）。

    ⚠️ 落盘后**立即建索引行**（与扫描不同：扫描是「发现已有文件」，上传是「我知道我刚写了什么」）。
    否则用户上传完刷新页面看不到自己的文件（要等他手动点「刷新」），体验是断的。
    """
    folder = _get_owned_folder(db, folder_id, current_user)

    raw_name = (file.filename or "").strip()
    if not raw_name:
        raise _err("MATERIAL_UPLOAD_NO_FILENAME", "上传的文件没有文件名", status.HTTP_400_BAD_REQUEST)
    # 只取末段（浏览器有时给完整路径），再校验剩余部分不含分隔符
    name = Path(raw_name.replace("\\", "/")).name
    if not name or name in (".", "..") or any(c in name for c in _FORBIDDEN_NAME_CHARS):
        raise _err(
            "MATERIAL_UPLOAD_BAD_FILENAME",
            "文件名非法（不允许包含路径分隔符）",
            status.HTTP_400_BAD_REQUEST,
        )

    content_type = (file.content_type or "").lower()
    file_type = "image" if content_type.startswith("image/") else "video" if content_type.startswith("video/") else ""
    if not content_type.startswith(_ALLOWED_UPLOAD_PREFIXES):
        raise _err(
            "MATERIAL_UPLOAD_BAD_TYPE",
            f"只支持图片或视频（收到的类型：{content_type or '未知'}）",
            status.HTTP_400_BAD_REQUEST,
        )

    folder_root = Path(folder.path)
    target = folder_root / name
    if target.exists():
        # 不覆盖（覆盖会**不可逆地毁掉**用户已有文件 —— 与「可逆」口径正面冲突）
        raise _err(
            "MATERIAL_UPLOAD_EXISTS",
            f"该文件夹下已有同名文件「{name}」—— 请改名后重试（不覆盖已有文件）",
            status.HTTP_409_CONFLICT,
        )

    payload = await file.read()
    if len(payload) > MATERIAL_UPLOAD_MAX_BYTES:
        raise _err(
            "MATERIAL_UPLOAD_TOO_LARGE",
            f"文件超过 {MATERIAL_UPLOAD_MAX_BYTES // 1024 // 1024} MB 上限",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    except OSError as exc:
        raise _err("MATERIAL_UPLOAD_WRITE_FAILED", f"写入失败：{exc}", status.HTTP_409_CONFLICT) from exc

    head, _ = _probe_dimensions(target, file_type)
    stat = target.stat()
    # ⚠️ `mtime` 列是 `DateTime` ⇒ **必须 `fromtimestamp`**，不能塞裸 float
    #    （塞 float 会 `SQLite DateTime type only accepts ...` 直接 500，
    #     与扫描侧 `material_scan_service.py:261` 的写法保持一致）
    row = MaterialFile(
        user_id=current_user.id,
        folder_id=folder.id,
        filename=name,
        file_type=file_type,
        rel_path=name,
        size_bytes=len(payload),
        mtime=datetime.fromtimestamp(stat.st_mtime),
        width=head[0],
        height=head[1],
        thumb_url=None,  # 缩略图留给扫描侧生成（不在上传路径里重复实现一份）
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return {
        "id": row.id,
        "folder_id": folder.id,
        "filename": name,
        "file_type": file_type,
        "size_bytes": row.size_bytes,
        "path": str(target),
    }


def _probe_dimensions(path: Path, file_type: str) -> tuple[tuple[Optional[int], Optional[int]], bool]:
    """读宽高（**失败不阻断上传** —— 尺寸是展示信息，不是正确性判据）。

    只对图片探测（视频要另一套解析，本轮不做 ⇒ 缺值留 `None`，口径「不造假」）。
    """
    if file_type != "image":
        return (None, None), False
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(path) as image:
            return image.size, True
    except Exception as exc:  # noqa: BLE001 —— 尺寸读不到不影响文件已上传的事实
        logger.info("素材上传：读取尺寸失败 %s: %s", path, exc)
        return (None, None), False
