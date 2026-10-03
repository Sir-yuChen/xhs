"""本地素材库端点（设计 17 §7.3，v2.x 新增，T3）。

端点表（8 个；§7.3 声明 7 个 + §7.3 L1090 明文要求的轮询端点 `GET /folders/{id}`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/xhs/materials/folders` | 文件夹列表 |
| GET | `/xhs/materials/folders/{id}` | 单个文件夹（刷新后轮询 `last_scan_at`） |
| POST | `/xhs/materials/folders` | 新增（安全校验 + 同 path 去重 + 异步首扫，202） |
| PATCH | `/xhs/materials/folders/{id}` | 改名 / 启用停用 |
| DELETE | `/xhs/materials/folders/{id}` | 删除（级联索引行 + 缩略图副本，**不删用户源文件**） |
| POST | `/xhs/materials/folders/{id}/scan` | 手动刷新（202 + 后台线程） |
| GET | `/xhs/materials/files` | 文件列表 + 搜索 / 筛选 / 分页 |
| GET | `/xhs/materials/files/{id}/file` | 取文件字节流（预览 / 发布用） |

## 归属纪律（与 `llm_gateway_monitor.py` 同款）

**每个端点第一条 SQL 都带 `user_id == current_user.id`**，命中不到即 404 ——
不用「先查后判 `row.user_id != me`」那种两段式（多一次查询且容易漏判）。

## 只读边界（硬约束 C2）

只写自己的 `material_files` 行与 `storage/materials/thumbs/` 副本；
**绝不**写 / 移动 / 删除用户源文件（DELETE 文件夹也**只删索引**，用户文件原封不动）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models import MaterialFile, MaterialFolder, User
from backend.app.schemas.common import paginated
from backend.app.services import material_scan_service as scan_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xhs/materials", tags=["xhs-materials"])

#: 排序白名单（§7.3：`sort` 参数）—— 禁把用户输入直接拼进 `order_by`
_SORT_FIELDS = {
    "mtime": MaterialFile.mtime,
    "filename": MaterialFile.filename,
    "size": MaterialFile.size_bytes,
}
_SORT_ORDERS = ("asc", "desc")


# ─────────────────────────── 请求体 ─────────────────────────── #

class MaterialFolderCreate(BaseModel):
    """`POST /xhs/materials/folders` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1)


class MaterialFolderUpdate(BaseModel):
    """`PATCH /xhs/materials/folders/{id}` 请求体（改名 / 启用停用）。"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    enabled: Optional[bool] = None


# ─────────────────────────── 序列化 ─────────────────────────── #

def _folder_json(folder: MaterialFolder) -> dict:
    """`MaterialFolder` → §7.3 声明字段（`last_scan_at` NULL 原样透出）。"""
    return {
        "id": folder.id,
        "name": folder.name,
        "path": folder.path,
        "enabled": folder.enabled,
        "last_scan_at": folder.last_scan_at,
        "file_count": folder.file_count,
    }


def _file_json(row: MaterialFile) -> dict:
    """`MaterialFile` → §7.3 声明字段（`width`/`height` 可为 null）。"""
    return {
        "id": row.id,
        "folder_id": row.folder_id,
        "filename": row.filename,
        "file_type": row.file_type,
        "rel_path": row.rel_path,
        "size_bytes": row.size_bytes,
        "mtime": row.mtime,
        "width": row.width,
        "height": row.height,
        "thumb_url": row.thumb_url,
    }


def _err(code: str, message: str, http_status: int) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"code": code, "message": message})


def _get_owned_folder(db: Session, folder_id: int, user: User) -> MaterialFolder:
    """取**本用户**的文件夹；不属于自己或不存在 ⇒ 一律 404（不区分，防枚举）。"""
    folder = db.scalar(
        select(MaterialFolder).where(
            MaterialFolder.id == folder_id,
            MaterialFolder.user_id == user.id,
        )
    )
    if folder is None:
        raise _err("MATERIAL_FOLDER_MISSING", "素材文件夹不存在", status.HTTP_404_NOT_FOUND)
    return folder


# ───────────────────── 文件夹：GET / POST ───────────────────── #

@router.get("/folders")
def list_material_folders(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[dict]:
    """文件夹列表（§7.3）。按 `id` 升序（建序稳定）。"""
    rows = db.scalars(
        select(MaterialFolder)
        .where(MaterialFolder.user_id == current_user.id)
        .order_by(MaterialFolder.id)
    ).all()
    return [_folder_json(f) for f in rows]


@router.get("/folders/{folder_id}")
def get_material_folder(
    folder_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """单个文件夹（§7.3 L1090：手动刷新后**轮询本端点**看 `last_scan_at`）。

    ⚠️ 该端点由 §7.3 明文要求（刷新后的轮询判据），但 §7.6 的「folders 4」计数
    当年漏了它 —— 已按「设计先行」补记（见 §7.3 / §7.6 修订）。
    """
    return _folder_json(_get_owned_folder(db, folder_id, current_user))


@router.post("/folders", status_code=status.HTTP_202_ACCEPTED)
def create_material_folder(
    payload: MaterialFolderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """新增文件夹（§7.3）：安全校验 → 同 path 去重 → 落库 → **异步首扫**（202）。

    ⚠️ 落库路径走 `resolve_abs_path()` 规整 ⇒ 唯一约束判的是**规整后**的路径，
    否则 `D:\\media` 与 `D:\\media\\` 会绕过 `(user_id, path)` 唯一约束建出两行。
    """
    ok, reason = scan_svc.is_safe_material_path(payload.path)
    if not ok:
        raise _err("MATERIAL_PATH_UNSAFE", reason, status.HTTP_400_BAD_REQUEST)

    abs_path = scan_svc.resolve_abs_path(payload.path)

    exists = db.scalar(
        select(MaterialFolder.id).where(
            MaterialFolder.user_id == current_user.id,
            MaterialFolder.path == abs_path,
        )
    )
    if exists is not None:
        raise _err("MATERIAL_FOLDER_EXISTS", "该文件夹已登记", status.HTTP_409_CONFLICT)

    folder = MaterialFolder(
        user_id=current_user.id,
        name=payload.name.strip(),
        path=abs_path,
        enabled=True,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)

    # 异步首扫：端点立即返回，前端轮询 `last_scan_at`
    scan_svc.scan_folder_in_background(current_user.id, folder.id)
    return _folder_json(folder)


# ──────────────── 文件夹：PATCH / DELETE / scan ──────────────── #

@router.patch("/folders/{folder_id}")
def update_material_folder(
    folder_id: int,
    payload: MaterialFolderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """编辑文件夹（§7.3）：改名 / 启用停用。

    ⚠️ `enabled=False` **不物理删** `material_files` 行（重启用即恢复，免重扫全盘）。
    """
    folder = _get_owned_folder(db, folder_id, current_user)

    if payload.name is not None:
        folder.name = payload.name.strip()
    if payload.enabled is not None:
        folder.enabled = payload.enabled

    db.commit()
    db.refresh(folder)
    return _folder_json(folder)


@router.delete("/folders/{folder_id}")
def delete_material_folder(
    folder_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """删除文件夹（§7.3）：级联删索引行 + 缩略图副本。

    ⚠️ **用户源文件不删**（硬约束 C2）—— 只删库里的索引行与 `storage/` 下的缩略图副本。
    ⚠️ 被 `generation_records` 引用**不拦**（历史记录靠冗余字段仍可查，§7.3 明说）。
    """
    folder = _get_owned_folder(db, folder_id, current_user)

    removed = db.execute(
        sa_delete(MaterialFile).where(
            MaterialFile.folder_id == folder.id,
            MaterialFile.user_id == current_user.id,
        )
    ).rowcount or 0
    db.delete(folder)
    db.commit()

    # 缩略图副本整目录清理（失败只记日志 —— 残留不影响正确性）
    # ⚠️ 必须 `except BaseException`（不是 `except OSError`）：删文件/删目录在装了
    #    「批删守卫」的环境下会抛 **`SystemExit`**（`BaseException` 直系，**不是**
    #    `OSError`），`except OSError` 兜不住 ⇒ 异常穿透 ⇒ 整个 DELETE 请求 500。
    #    注释写着「失败只记日志」就必须真的兜住一切（DEF-048：安全网不得条件性）。
    #    2026-10-02 实测：全量回归下本端点因 `SystemExit` 崩，串行跑不触发（守卫按轮计次）。
    try:
        thumb_dir = scan_svc.thumbs_dir() / str(folder.id)
        if thumb_dir.is_dir():
            for child in thumb_dir.iterdir():
                if child.is_file():
                    child.unlink()
            thumb_dir.rmdir()
    except BaseException as exc:  # noqa: BLE001
        logger.warning("素材库删除：缩略图清理失败 folder=%s: %s", folder.id, exc)

    return {"ok": True, "removed_files": removed}


@router.post("/folders/{folder_id}/scan", status_code=status.HTTP_202_ACCEPTED)
def scan_material_folder(
    folder_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """手动刷新（§7.3）：202 + 后台线程；前端轮询 `last_scan_at` 判完成。"""
    folder = _get_owned_folder(db, folder_id, current_user)

    if not folder.enabled:
        raise _err(
            "MATERIAL_FOLDER_DISABLED",
            "文件夹已停用，请先启用再扫描",
            status.HTTP_400_BAD_REQUEST,
        )

    without_scan = bool(folder.last_scan_at is None)
    scan_svc.scan_folder_in_background(current_user.id, folder.id)
    return {"ok": True, "status": "scanning", "first_scan": without_scan}


# ───────────────────── 文件：列表 / 取内容 ───────────────────── #

@router.get("/files")
def list_material_files(
    folder_id: Optional[int] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    file_type: Optional[str] = Query(default=None),
    sort: str = Query(default="mtime"),
    order: str = Query(default="desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """文件列表 + 搜索（§7.3）。

    筛选项：`folder_id?` / `keyword`（filename 模糊）/ `file_type`（image|video）/
    `sort`（mtime|filename|size）/ `order`（asc|desc）/ 分页。
    """
    stmt = select(MaterialFile).where(MaterialFile.user_id == current_user.id)

    if folder_id is not None:
        stmt = stmt.where(MaterialFile.folder_id == folder_id)
    if keyword:
        stmt = stmt.where(MaterialFile.filename.like(f"%{keyword.strip()}%"))
    if file_type:
        if file_type not in ("image", "video"):
            raise _err(
                "MATERIAL_FILE_TYPE_INVALID",
                "file_type 只能是 image 或 video",
                status.HTTP_400_BAD_REQUEST,
            )
        stmt = stmt.where(MaterialFile.file_type == file_type)

    column = _SORT_FIELDS.get(sort, MaterialFile.mtime)
    direction = column.desc() if order not in ("asc",) else column.asc()
    #: 次序键：`mtime` 大量并列 ⇒ 不补 `id` 的话分页会出现重复/漏行
    stmt = stmt.order_by(direction, MaterialFile.id.desc())

    rows = db.scalars(stmt).all()
    return paginated([_file_json(r) for r in rows], page=page, page_size=page_size)


@router.get("/files/{file_id}/file")
def get_material_file_content(
    file_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FileResponse:
    """取文件字节流（§7.3，面板预览 / 发布用）。

    ⚠️ 三条 404 统一 `MATERIAL_FILE_MISSING`：
    ① 索引行不属于本用户；② 索引行不存在；③ **索引行在、但磁盘源文件已不在**
    —— 后者正是「索引与磁盘脱钩」的真实场景（用户手动删了文件、还没重扫）。
    """
    row = db.scalar(
        select(MaterialFile).where(
            MaterialFile.id == file_id,
            MaterialFile.user_id == current_user.id,
        )
    )
    if row is None:
        raise _err("MATERIAL_FILE_MISSING", "素材文件不存在", status.HTTP_404_NOT_FOUND)

    folder = db.scalar(
        select(MaterialFolder).where(
            MaterialFolder.id == row.folder_id,
            MaterialFolder.user_id == current_user.id,
        )
    )
    if folder is None:
        raise _err("MATERIAL_FILE_MISSING", "素材文件不存在", status.HTTP_404_NOT_FOUND)

    abs_path = Path(folder.path) / row.rel_path
    if not abs_path.is_file():
        raise _err(
            "MATERIAL_FILE_MISSING",
            "源文件已不在磁盘上，请刷新该文件夹",
            status.HTTP_404_NOT_FOUND,
        )

    return FileResponse(
        abs_path,
        filename=row.filename,
        media_type=_guess_media_type(row.filename),
    )


def _guess_media_type(filename: str) -> str:
    """按扩展名给 `Content-Type`（复用一期 `files.py` 同款映射口径）。"""
    suffix = Path(filename).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska",
    }.get(suffix, "application/octet-stream")


@router.get("/folders/{folder_id}/thumbs/{file_id}")
def get_material_thumb(
    folder_id: int,
    file_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """素材缩略图（**`thumb_url` 指向的就是这里**，设计 17 §3.4）。

    ## ⚠️ 这个端点此前**根本不存在**（2026-10-03 真机实测发现）

    `material_scan_service._thumb_rel_url()` 生成的 URL 一直是
    `/api/xhs/materials/folders/{folder_id}/thumbs/{file_id}`，
    但**没有任何路由提供它** —— 扫描把文件、尺寸、缩略图都算好了、索引行也写对了，
    前端拿到的 `thumb_url` 却是 **404** ⇒ **素材库里的图片全部显示不出来**
    （用户现象：「正常图在素材库中无法展示」）。

    这类 bug 的形态很典型：**生产者（URL 字符串）与消费者（路由）各写各的**，
    两边单独看都对，中间没有一条断言把它们绑在一起。

    契约：`{file_id}` 是 `material_files.id`（不是文件名）——
    与 `_thumb_rel_url(folder_id, str(file_id))` 的拼装一致。
    缩略图**按 id 命名**（`f{folder_id}-{file_id}.jpg`）⇒ 取文件不必查 rel_path，
    但仍要**校验归属**（否则任何人改 id 就能看别人的图）。
    """
    from backend.app.services.material_scan_service import thumbs_dir

    # 归属校验：文件夹必须是本人的
    folder = db.scalar(
        select(MaterialFolder).where(
            MaterialFolder.id == folder_id,
            MaterialFolder.user_id == current_user.id,
        )
    )
    if folder is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "MATERIAL_FOLDER_NOT_FOUND", "message": "素材库文件夹不存在"},
        )
    # 行也必须是本人的（**双重校验**：文件夹对了、行不属于自己仍然要拦）
    row = db.scalar(
        select(MaterialFile).where(
            MaterialFile.id == file_id,
            MaterialFile.folder_id == folder_id,
            MaterialFile.user_id == current_user.id,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "MATERIAL_FILE_NOT_FOUND", "message": "素材文件不存在"},
        )

    target = thumbs_dir() / str(folder_id) / f"f{folder_id}-{file_id}.jpg"
    if not target.is_file():
        # 缩略图缺失（老索引行 / 生成失败过）⇒ 404 + 可执行原因。
        # ⚠️ **不回落原图**：原图可能几十 MB，回落等于把「缩略图」语义偷偷换掉，
        #    且会把用户本机大文件直接吐给浏览器（两者都不该在「缩略图」名下发生）。
        #    正确出路是重扫（`POST /folders/{id}/scan`）补齐。
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "MATERIAL_THUMB_MISSING",
                "message": "缩略图不存在，请对该素材库执行一次「刷新」（会重新生成）",
            },
        )
    return FileResponse(target, media_type="image/jpeg")
