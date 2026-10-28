"""本地素材库扫描 / 索引 / 缩略图服务（设计 17 §6）。

职责边界（**只读 + 索引**，硬约束 C2）：
- **读**用户登记的文件夹（`os.walk`），索引图片/视频；
- **写**只写自己的 `material_files` 行与 `storage/materials/thumbs/` 下的缩略图副本；
- ⛔ **绝不**写、移动、删除用户的源文件。

设计要点：
* 扫描放**后台 daemon 线程**（§7.3：端点 202 立即返回，前端轮询 `last_scan_at`）；
* **增量**：`mtime` 未变 ⇒ 跳过重索引；已消失的源文件 ⇒ 删索引行；
* **缩略图**：图片用 Pillow生成 80×80 cover；
  视频**不引新依赖**（项目无 opencv）⇒ `thumb_url=""` 占位，前端显示类型图标（§6.1）。

⚠️ **本服务不校验归属**：`user_id` 由调用方（API 层）传自己，扫描/查询一律带
`MaterialFile.user_id == user_id` 双保险（与 `_get_owned_alert` 同款纪律）。
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

try:  # 优先 Pillow 原生重采样枚举（Pillow ≥ 9.1）
    from PIL import Image, ImageOps
    from PIL.Image import Resampling

    _LANCZOS = Resampling.LANCZOS
except ImportError:  # pragma: no cover - 兼容旧版 Pillow / 缺 Pillow 时由调用处降级
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]
    _LANCZOS = None  # type: ignore[assignment]

from backend.app.core.config import get_settings
from backend.app.core.database import SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models import MaterialFile, MaterialFolder

logger = logging.getLogger(__name__)

#: 索引的扩展名 → file_type（**对齐一期 `ALLOWED_UPLOAD_EXTENSIONS`**，设计 17 §6.1 L824）。
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv"})

#: 缩略图规格（**720 × 720 cover**）。
#:
#: ## 尺寸是怎么定的（一次算清楚，别再来回调）
#:
#: 清晰度要求 = **渲染宽度 × 设备像素比（DPR）**：
#: * 六宫格下每张渲染宽度约 **150–260px**（取决于窗口宽）；
#: * 用户本机屏幕缩放 **175%** ⇒ DPR 约 **1.75**；
#: * ⇒ 需要 ≥ 260 × 1.75 ≈ **455px** 才不糊。
#:
#: 取 **720** 是为留下余量（宽屏下格子还会更宽、DPR 2.0 的设备也不少见），
#: 单张约 **80–150 KB** —— 仍比原图（1776×2368 / **6.5 MB**）小一到两个数量级，
#: 列表几十张一起加载不会卡。
#:
#: ⚠️ 演进：80（旧固定小卡片）→ 320（六宫格首版，**高 DPI 屏下仍糊** —— 用户实测
#: 「缩略图太模糊，至少需要高清」）→ **720**。
#: ⚠️ **改这个常量后必须清索引行重扫**：扫描按 `mtime` 增量，文件没变不会重做缩略图。
THUMB_SIZE = 720


def thumbs_dir() -> Path:
    """缩略图根目录 `backend/app/storage/materials/thumbs/`（§3.4 L290 明文）。"""
    return Path(get_settings().storage_dir) / "materials" / "thumbs"

#: 系统目录黑名单（**前缀匹配**，命中即拒绝）—— §6.1 安全边界。
#: ⚠️ Windows 盘符根（`C:\`）由 `is_safe_material_path` 单独判「就是根目录」的情况，
#: 不放进本表（否则 `C:\Users\...` 会被 `C:\` 前缀全灭）。
_UNSAFE_PREFIXES = (
    # Windows 系统
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\programdata",
    "c:\\$recycle.bin",
    "c:\\system volume information",
    # Unix / macOS 系统
    "/system",
    "/usr",
    "/bin",
    "/sbin",
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/lib",
    "/private",
    "/var",
    "/volumes",
)


def is_safe_material_path(raw: str) -> tuple[bool, str]:
    """素材库路径安全校验（设计 17 §6.1）。

    返回 `(ok, reason)`；`ok=False` 时 `reason` 是**面向用户的短路原因**
    （API 层据此拼 400 `MATERIAL_PATH_UNSAFE`）。

    拦截四条：
    1. 空 / 不存在；
    2. 不是目录（是文件或其他）；
    3. 命中系统目录黑名单（前缀匹配）或**就是盘符根**；
    4. 无可读权限（`os.access`，跨平台尽力而为）。
    """
    if not raw or not str(raw).strip():
        return False, "路径不能为空"

    text = str(raw).strip()
    # 归一化：`\` → `/` 便于跨平台前缀比较；末尾去斜杠
    probe = text.replace("\\", "/").rstrip("/")
    lower = probe.lower()

    # ③ 盘符根 / 文件系统根（`C:/`、`/`）—— 扫描它等于扫全盘
    if lower in ("", "/"):
        return False, "不允许扫描文件系统根目录"
    if len(lower) == 2 and lower[1] == ":":
        return False, "不允许扫描磁盘根目录"

    # ③ 系统目录黑名单（前缀匹配，带分隔符防 `C:\WindowsApps` 误伤）
    for prefix in _UNSAFE_PREFIXES:
        p = prefix.replace("\\", "/")
        if lower == p or lower.startswith(p + "/"):
            return False, f"不允许扫描系统目录（{text}）"

    path = Path(text)
    # ① 存在性
    if not path.exists():
        return False, "路径不存在"
    # ② 必须是目录
    if not path.is_dir():
        return False, "路径不是文件夹"
    # ④ 可读权限（Windows 上 `os.access` 对 ACL 不敏感，故只作尽力而为的一道）
    if not os.access(path, os.R_OK):
        return False, "文件夹无读取权限"

    return True, ""


def resolve_abs_path(raw: str) -> str:
    """落库前把用户输入规整为**绝对路径字符串**（`resolve()` 折叠 `..`）。

    ⚠️ 只规整**路径字符串**，不解析符号链接目标也不读写文件内容。
    """
    return str(Path(str(raw).strip()).resolve())


def _file_type_of(suffix: str) -> str | None:
    s = suffix.lower()
    if s in IMAGE_EXTENSIONS:
        return "image"
    if s in VIDEO_EXTENSIONS:
        return "video"
    return None


def _thumb_rel_url(folder_id: int, rel_path: str) -> str:
    """缩略图的 API 相对 URL（由 `folders/{id}/thumbs/{name}` 端点提供）。

    ⚠️ 用「文件夹 id + 文件名」而不是原始路径，避免把用户本机目录结构暴露到 URL 里。
    """
    safe_name = Path(rel_path).name
    return f"/api/xhs/materials/folders/{folder_id}/thumbs/{safe_name}"


def _make_image_thumb(source: Path, folder_id: int, file_id: int) -> tuple[int | None, int | None, str]:
    """生成 80×80 cover 缩略图；返回 `(width, height, thumb_url)`。

    失败（损坏图 / 非图片）⇒ 静默降级 `(None, None, "")` 并记 warning ——
    **一个坏文件不该让整次扫描失败**。
    """
    try:
        if Image is None:
            raise RuntimeError("Pillow 不可用，跳过缩略图")

        with Image.open(source) as im:
            width, height = im.size
            # 缩略图落 `storage/materials/thumbs/{folder_id}/`
            target_dir = thumbs_dir() / str(folder_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"f{folder_id}-{file_id}.jpg"

            # ⚠️ **只等比缩放，不裁切**（v2.29.6）。
            #
            # 原先用 `ImageOps.fit(..., (SIZE, SIZE))` = **居中裁成正方形** ——
            # 而前端格子也是 `objectFit: "cover"`（**再裁一次**）
            # ⇒ **双重裁切**：一张 1776×2368 的竖图，后端先切掉上下 25%，
            #    前端再从剩下的方图里切一次，商品主体很容易被切掉。
            #
            # 分工应该是**各管一段**：后端负责「变小」（等比缩放、保住全部画面），
            # 前端负责「显示成方形」（`cover` 裁一次，且**按格子形状裁**，
            # 格子宽高比变了裁切位置也跟着对）。
            #
            # `thumbnail()` 只缩不放（原图比上限小时保持原尺寸，不做无谓放大）。
            thumb = im.convert("RGB")
            thumb.thumbnail((THUMB_SIZE, THUMB_SIZE), _LANCZOS)
            thumb.save(target, format="JPEG", quality=85, optimize=True)
        return width, height, _thumb_rel_url(folder_id, str(file_id))
    except Exception as exc:  # noqa: BLE001 —— 单文件失败不影响整批（设计 17 §6.1 健壮性）
        logger.warning("素材缩略图生成失败 %s: %s", source, exc)
        return None, None, ""


def _iter_media_files(root: Path) -> Iterable[Path]:
    """遍历根目录下的图片/视频（跳过隐藏目录与常见噪声目录）。"""
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "$RECYCLE.BIN"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in skip_dirs and not d.startswith(".")
        ]
        base = Path(dirpath)
        for name in filenames:
            if name.startswith("."):
                continue
            if _file_type_of(Path(name).suffix) is None:
                continue
            yield base / name


def scan_folder(db: Session, folder: MaterialFolder) -> dict[str, Any]:
    """同步扫描一个文件夹并 upsert 索引（**供后台线程调用**）。

    流程：`os.walk` → 逐文件 `mtime` 比对 → 新增/更新 → 删除已消失的行。
    返回统计 `{scanned, added, updated, removed, skipped}`。

    ⚠️ 只读用户文件：本函数**不写**用户目录，只写 `material_files` 与缩略图副本。
    """
    root = Path(folder.path)
    now = shanghai_now()

    existing = {
        row.rel_path: row
        for row in db.scalars(
            select(MaterialFile).where(
                MaterialFile.parent_id == folder.id,
                MaterialFile.user_id == folder.user_id,
            )
        ).all()
    }

    scanned = added = updated = skipped = 0
    seen: set[str] = set()

    for abs_file in _iter_media_files(root):
        scanned += 1
        try:
            stat = abs_file.stat()
        except OSError:
            continue  # 扫描期间被删/无权限 ⇒ 跳过，不算失败
        rel = abs_file.relative_to(root).as_posix()
        seen.add(rel)
        mtime = datetime.fromtimestamp(stat.st_mtime)
        file_type = _file_type_of(abs_file.suffix)
        assert file_type is not None  # `_iter_media_files` 已过滤

        row = existing.get(rel)
        if row is not None and abs(stat.st_mtime - row.mtime.timestamp()) < 1.0:
            skipped += 1  # 增量：mtime 未变 ⇒ 不重索引
            continue

        if row is None:
            row = MaterialFile(
                user_id=folder.user_id,
                parent_id=folder.id,
                filename=abs_file.name,
                file_type=file_type,
                rel_path=rel,
                size_bytes=stat.st_size,
                mtime=mtime,
            )
            db.add(row)
            db.flush()  # 取 id 供缩略图命名
            added += 1
        else:
            existing[rel] = row
            updated += 1

        row.size_bytes = stat.st_size
        row.mtime = mtime

        if file_type == "image":
            w, h, url = _make_image_thumb(abs_file, folder.id, row.id)
            row.width, row.height = w, h
            row.thumb_url = url
        else:
            # 视频：不引 opencv ⇒ 尺寸与缩略图都留空（§6.1 明确降级）
            row.width = row.height = None
            row.thumb_url = ""

    # 源文件已消失 ⇒ 删索引行（用户源文件本来就不在库里，无需动）
    removed = 0
    for rel, row in existing.items():
        if rel not in seen:
            db.delete(row)
            removed += 1
    # ⚠️ 必须显式 flush：`db.delete()` 只是把对象标记为待删，autoflush 在 count 之前
    #    不一定推下去 ⇒ `file_count` 会把已删的行也数进去（实测差 1）。同时清掉
    #    identity map 里的残留，让 count 反映真实已落库状态。
    db.flush()
    db.expire_all()

    folder.last_scan_at = now
    folder.file_count = db.scalar(
        select(func.count(MaterialFile.id)).where(
            MaterialFile.parent_id == folder.id,
            MaterialFile.user_id == folder.user_id,
        )
    ) or 0
    db.commit()
    return {
        "scanned": scanned,
        "added": added,
        "updated": updated,
        "removed": removed,
        "skipped": skipped,
    }


def scan_folder_in_background(user_id: int, folder_id: int) -> None:
    """后台 daemon 线程扫描（§7.3：端点 202 立即返回，前端轮询 `last_scan_at`）。

    ⚠️ 自建 Session（不能复用请求的 Session —— 请求已返回、Session 已关）。
    ⚠️ 异常必须吞掉并记日志：daemon 线程里的异常无人接管，会静默丢。
    ⚠️ `SessionLocal` **必须模块层导入**（与其他服务同款）：`tests/backend/conftest.py`
       的 `patch_service_sessions()` 用 AST 扫**模块层**节点来打桩测试库；若在这里
       `from … import SessionLocal`，桩打不到 ⇒ 线程落到生产库、静默失败
       （2026-10-02 实测：`last_scan_at` 永远为 None，缩略图却已落盘）。
    """
    def _run() -> None:
        db = SessionLocal()
        try:
            folder = db.get(MaterialFolder, folder_id)
            if folder is None or folder.user_id != user_id:
                logger.warning("素材扫描：folder %s 不存在或不属于 user %s", folder_id, user_id)
                return
            if not folder.enabled:
                logger.info("素材扫描：folder %s 已停用，跳过", folder_id)
                return
            stats = scan_folder(db, folder)
            logger.info("素材扫描完成 folder=%s user=%s %s", folder_id, user_id, stats)
        except Exception as exc:  # noqa: BLE001
            logger.warning("素材扫描失败 folder=%s user=%s: %s", folder_id, user_id, exc)
        finally:
            db.close()

    threading.Thread(
        target=_run,
        name=f"material-scan-{folder_id}",
        daemon=True,
    ).start()
