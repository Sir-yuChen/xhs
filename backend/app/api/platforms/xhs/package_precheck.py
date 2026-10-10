"""数据包**预检**端点（设计 25 §4；用户口径 2026-10-08）。

端点表（4 个）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/xhs/package-precheck` | 逐文件上传 + 解析 + OCR，一趟返回完整预检 |
| POST | `/xhs/package-precheck/{token}/scan` | 对已暂存的包重跑（改了词库想再扫一次） |
| GET | `/xhs/package-precheck/{token}/image` | 取单张图（给前端画红框 overlay） |
| DELETE | `/xhs/package-precheck/{token}` | 丢弃暂存包（关弹窗 / 发布完成） |

## 为什么要「一趟返回」而不是分三步

用户口径把预检描述成一个连续动作（选文件夹 → 加载 → 看到红框）。
拆成多次往返只会让前端多写一堆状态机，且中间态没有价值。
所以上传 + 解析 + OCR **一次做完**再返回 —— 慢，但**一致**。

⚠️ **零平台请求**：全链路只碰本地磁盘（用户自己上传的字节）。
"""

from __future__ import annotations

import logging
import mimetypes
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models.user import User
from backend.app.services import forbidden_word_service as fw
from backend.app.services import image_ocr_service as ocr
from backend.app.services import package_staging_service as staging
from backend.app.services.shop_package_precheck import (
    PrecheckError,
    MEDIA_GROUPS,
    precheck_package,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xhs/package-precheck", tags=["xhs-package-precheck"])

#: 一次最多传多少个文件（数据包实测 70 个；留一倍余量）。
MAX_FILES = 400

#: **执行进度**（用户口径 2026-10-09）：预检跑 OCR/抽帧要几十秒，
#: 期间前端按 token 轮询这里拿「已处理 / 总数」。
#:
#: ⚠️ 纯内存、进程内 —— 单机桌面应用够用；key 是预检会话 token。
#: 结构：`{token: {"phase": str, "done": int, "total": int}}`。
_PROGRESS: dict[str, dict[str, object]] = {}
_PROGRESS_LOCK = threading.Lock()


def _set_progress(token: str, phase: str, done: int, total: int) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS[token] = {"phase": phase, "done": done, "total": total}


def _clear_progress(token: str) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS.pop(token, None)


def _get_progress(token: str) -> dict[str, object] | None:
    with _PROGRESS_LOCK:
        return _PROGRESS.get(token)


def _err(code: str, message: str, http_status: int) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"code": code, "message": message})


def _ask_directory() -> str:
    """弹系统「选择文件夹」对话框，返回选中路径（取消 ⇒ 空串）。

    ⚠️ tkinter 只能在**主线程**跑，而 FastAPI 的同步端点跑在 anyio 工作线程
    （实测 `is_main=False`）。两条路：

    1. **主线程有活的 Tk 主循环**（GUI 宿主进程）⇒ 用 `after(0, …)` 把活排进去，
       主线程弹框，这边等 `Event`；
    2. 没有主循环（本项目常态：uvicorn 是控制台/裸进程）⇒ 退一步在**当前线程**
       临时开一个 Tk —— Tk 允许多个解释器，只是不能跨线程共享同一个根窗口。
       实测 uvicorn 工作线程里 `Tk()` 能正常创建并弹框。

    两种情况都给「弹不出来」留明确出口（`_err`），**不静默失败**。
    """
    def _dialog() -> str:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)  # 免得被浏览器盖住找不着
        except Exception:  # noqa: BLE001 - 某些 WM 不支持，不是错误
            pass
        try:
            return filedialog.askdirectory(title="选择商品数据包文件夹") or ""
        finally:
            root.destroy()

    #: ① 能联系上主线程正在跑的主循环 ⇒ 交给它弹（最稳，GUI 宿主进程走这条）
    loop_ref = _MAIN_LOOP.get("root")
    if loop_ref is not None:
        box: dict[str, str] = {}
        done = threading.Event()

        def _on_main() -> None:
            try:
                box["path"] = _dialog()
            except Exception:  # noqa: BLE001
                box["path"] = ""
            finally:
                done.set()

        try:
            loop_ref.after(0, _on_main)
            if done.wait(timeout=300):
                return box.get("path", "")
        except Exception:  # noqa: BLE001 - 主循环没了，走 ②
            pass

    #: ② 当前线程自己开（uvicorn 裸进程的常态）
    try:
        return _dialog()
    except Exception as error:  # noqa: BLE001 - 无桌面会话 / 无 DISPLAY
        raise _err(
            "NO_DESKTOP",
            "当前运行环境无法弹出文件夹选择框，请直接填写数据包目录的绝对路径",
            status.HTTP_400_BAD_REQUEST,
        ) from error


#: GUI 宿主进程注册自己主循环的地方（没有就是 `None`）。本项目常态为 `None`。
_MAIN_LOOP: dict[str, object] = {}


def _run_precheck(
    user: User,
    db,
    root: Path,
    package_name: str,
    *,
    inspect: bool = True,
    groups: list[str] | None = None,
    progress_token: str | None = None,
) -> dict:
    """预检主流程：解析（+ 可选 OCR）（纯本地）。

    ⚠️ **词库按 `user_id` 取**（别人的词不该影响你的预检 —— 数据隔离红线）。

    ## `inspect` 两段式（用户口径 2026-10-08）

    > 「先将媒体和数据包全部在弹窗展示，点击预检，预检通过才可以进去参数配置第二步」

    - `inspect=False`：**只解析**（媒体清单 + 表格）。快（真包 ~1s，不碰 OCR），
      用于「打开弹窗就把东西全列出来」；
    - `inspect=True`：再跑违规词 OCR / 视频抽帧（真包 ~50s），用于「预检」按钮。

    两段共用同一份 `precheck_package` 解析结果形状，所以前端拿到的东西长得一样，
    差别只在 `ocr_ok` / `hits` 是否已填。**没跑检测的条目 `ocr_ok=None`**，
    前端按「未检测」呈现、默认不勾选 —— 绝不能因为「还没检」就当成合规。
    """
    try:
        result = precheck_package(root, package_name=package_name)
    except PrecheckError as error:
        raise _err("PRECHECK_FAILED", str(error), status.HTTP_400_BAD_REQUEST) from error

    #: ⚠️ 只取**启用中**的词（停用的词不参与匹配 —— 这就是「停用」的意义）。
    #: 传 `groups` ⇒ 再按分组收窄（空 = 全部分组，用户口径的默认）。
    groups = [str(g) for g in (groups or []) if str(g) != ""]
    entries = fw.enabled_entries(db, user.id, groups=groups or None)
    payload = result.to_dict()

    #: 只读取模式：每条媒体落「未检测」的全套键就返回（形状与检过的一致）。
    if not inspect:
        for item in payload["media"]:
            item["ocr_ok"] = None
            item["error"] = None
            item["error_message"] = None
            item["width"] = None
            item["height"] = None
            item["hits"] = []
            item["has_forbidden"] = False
            item["unverified_reason"] = "尚未预检"
            item["frame_count"] = None
            item["frames_failed"] = None
            item["frames"] = []
        payload["inspected"] = False
        return payload

    #: ⚠️ 「全部媒体 OCR」的口径（用户 2026-10-08）：**连视频和属性图也要**。
    #: 视频走**抽帧 OCR**（`scan_video`：等间隔抽帧 → 逐帧识别 → 合并命中）；
    #: 抽帧失败才退回「显式未验证」（`ocr_ok=None` + `unverified_reason`，默认不勾选）。
    image_items = [m for m in result.media if not m.unsupported and m.group != "video"]
    video_items = [m for m in result.media if m.group == "video"]
    image_paths = [str(root / m.rel_path) for m in image_items]
    total_media = len(image_paths) + len(video_items)
    #: **执行进度**（用户口径 2026-10-09）：图片逐张 +，视频按每个完成 +1。
    _done = {"n": 0}

    def _bump() -> None:
        _done["n"] += 1
        if progress_token:
            _set_progress(progress_token, "检测违规词", _done["n"], total_media)

    if progress_token:
        _set_progress(progress_token, "检测违规词", 0, total_media)
    scan = ocr.scan_images(image_paths, entries, on_one_done=_bump)
    by_path = {r["path"]: r for r in scan["results"]}
    #: ⚠️ 视频抽帧**并发**（用户口径 2026-10-09：并发处理 OCR）——
    #: 此前是串行 for，每个视频抽帧 + 逐帧识别在各 ~10s 量级，
    #: 多视频时会叠加。用同一并发数（`MAX_WORKERS`）跑，单个失败不中断整批。
    if video_items:
        video_paths = [str(root / v.rel_path) for v in video_items]

        def _scan_video_one(vp: str) -> dict[str, Any]:
            try:
                return ocr.scan_video(vp, entries)
            except Exception as error:  # noqa: BLE001
                logger.warning("视频抽帧失败 %s: %s", os.path.basename(vp), error)
                return next(iter(ocr.note_unverified_videos([vp]).values()))

        with ThreadPoolExecutor(max_workers=ocr.MAX_WORKERS) as pool:
            for res in pool.map(_scan_video_one, video_paths):
                by_path[res["path"]] = res
                _bump()
    for item in payload["media"]:
        #: **每个**媒体都先落这几个键 —— 视频与属性图不进 OCR（`by_path` 取不到），
        #: 但前端要按同一套键渲染（否则 `m.ocr_ok` 在视频行上是 `undefined`，
        #: 「默认勾选」逻辑会把 `undefined` 当成「检过且合规」而**误勾违规视频**）。
        item["ocr_ok"] = None
        item["error"] = None
        item["error_message"] = None
        item["width"] = None
        item["height"] = None
        item["hits"] = []
        item["has_forbidden"] = False
        #: 不进 OCR 的原因要说清楚（前端角标：「未检测」≠「已检过」）。
        item["unverified_reason"] = None
        #: 视频抽帧信息（图片为 None）—— 前端据此显示「已抽 N 帧」与逐帧命中时点。
        item["frame_count"] = None
        item["frames_failed"] = None
        item["frames"] = []

        found = by_path.get(str(root / item["rel_path"]))
        if not found:
            continue
        item["ocr_ok"] = found["ocr_ok"]
        item["unverified_reason"] = found.get("unverified_reason")
        item["error"] = found.get("error")
        item["error_message"] = found.get("error_message")
        item["width"] = found.get("width")
        item["height"] = found.get("height")
        item["hits"] = found["hits"]
        item["has_forbidden"] = bool(found["hits"])
        #: 视频才有这两个键 —— 图片保持 None（形状统一，前端不必判 undefined）。
        if item["group"] == "video":
            item["frame_count"] = found.get("frame_count")
            item["frames_failed"] = found.get("frames_failed")
            item["frames"] = found.get("frames") or []

    #: 词库为空要**说清楚**，不能让用户对着「全绿」误以为已经检过了。
    if not entries:
        payload["warnings"].append(
            "违规词库为空，本次未做任何违规词检测（请先在商品台账「违规词」里添加词条）"
        )
    if scan["summary"]["failed"]:
        payload["warnings"].append(
            f"{scan['summary']['failed']} 张图片未能完成 OCR 检测（原因见各图 error 列）"
        )
    if scan["summary"]["truncated"]:
        payload["warnings"].append(
            f"图片超过单次上限 {ocr.MAX_IMAGES} 张，仅检测了前 {ocr.MAX_IMAGES} 张"
        )
    payload["inspected"] = True
    return payload


@router.post("/pick-folder", status_code=status.HTTP_200_OK)
def pick_folder(current_user: User = Depends(get_current_user)) -> dict:
    """**弹出系统原生「选择文件夹」对话框**，返回用户选中的绝对路径。

    用户 2026-10-08 口径：「点击加载数据包**选择本地文件夹**，获得本地夹地址后，
    直接读取该数据包内的商品数据」。

    ## 为什么必须由**后端**弹这个框

    浏览器（含 Electron 渲染进程）出于安全**拿不到 `File` 的真实路径**：
    `<input webkitdirectory>` 只给文件名和浏览器内的相对路径，`file.path`
    早已被移除。所以「点开系统选择器、拿到 `D:\\...` 这样的绝对路径」这件事
    在桌面端只能由**后端进程**做（本项目后端与用户在同一台机器上）。

    ⚠️ 线程问题：tkinter 必须在**主线程**跑，而 FastAPI 的同步端点跑在线程池里。
    若当前不是主线程，就切到主线程执行（`_run_on_main_thread`）；拿不到主线程
    （比如某些无 GUI 环境）就明确报错，**不静默失败**。

    ⚠️ 无头环境（服务端部署）没有桌面 ⇒ 会报 `NO_DESKTOP`，前端应回落到手填路径。
    """
    try:
        import tkinter  # noqa: F401
    except ImportError as error:  # pragma: no cover - 依赖缺失
        raise _err(
            "NO_DESKTOP",
            "当前环境没有可用的图形界面，请直接填写数据包目录的绝对路径",
            status.HTTP_400_BAD_REQUEST,
        ) from error

    chosen = _ask_directory()
    if not chosen:
        #: 用户点了取消 —— 不是错误（前端静默处理，不弹红条）。
        return {"package_path": "", "cancelled": True}
    return {"package_path": str(Path(chosen).resolve()), "cancelled": False}


class LocalPackageRequest(BaseModel):
    """按**本地绝对路径**预检（用户 2026-10-08 口径：「不要上传」）。"""

    #: 数据包目录绝对路径，例如 `C:/Users/xxx/Desktop/商品ID_935947169189`。
    package_path: str = Field(min_length=1, max_length=1024)
    #: 展示名；空 ⇒ 取目录名。
    package_name: str = Field(default="", max_length=128)
    #: ⚠️ `False`（默认）= **只读取展示**，不跑 OCR。
    #: 用户口径 2026-10-08：「先将媒体和数据包全部在弹窗展示，点击预检，
    #: 预检通过才可以进去参数配置第二步」。
    #: `True` = 连违规词 OCR 一起跑（点在「预检」按钮上）。
    inspect: bool = Field(default=False)
    #: ⚠️ **只用这些分组**做匹配（用户口径 2026-10-08：「点击预检支持选择指定
    #: 违规词分组，支持多组，**默认全部分组**」）。空列表 = 全部分组。
    groups: list[str] = Field(default_factory=list)


@router.post("/local", status_code=status.HTTP_200_OK)
def precheck_local(
    payload: LocalPackageRequest,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    """**直接读本地数据包目录** → 预检（不上传，主路径）。

    用户 2026-10-08 口径：「图片全部展示加载本地数据包内的媒体资源不要上传」。

    ⚠️ 零字节离开本机：只把目录**路径**登记一下，媒体与表格都由服务端
    在本地读，图片经 `/image` 端点按需取（前端拿到的仍是受控的相对路径）。
    ⚠️ 词库按 `user_id` 取（数据隔离）。
    """
    try:
        token, root = staging.register_local(current_user.id, payload.package_path)
    except staging.StagingError as error:
        raise _err("LOCAL_PACKAGE_INVALID", str(error), status.HTTP_400_BAD_REQUEST) from error

    try:
        return {
            "token": token,
            "package_path": str(root),
            "precheck": _run_precheck(
                current_user,
                db,
                root,
                payload.package_name or root.name,
                inspect=payload.inspect,
                groups=payload.groups,
                progress_token=token,
            ),
        }
    finally:
        if payload.inspect:
            _clear_progress(token)


@router.post("", status_code=status.HTTP_200_OK)
async def upload_and_precheck(
    package_name: str = Form(default=""),
    files: list[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    """上传数据包 → 预检（解析 + OCR 违规词），一趟返回。"""
    if not files or len(files) > MAX_FILES:
        raise _err(
            "BAD_FILE_COUNT",
            f"请选择 1–{MAX_FILES} 个文件的数据包目录（当前 {len(files)} 个）",
            status.HTTP_400_BAD_REQUEST,
        )

    payload: list[tuple[str, bytes]] = []
    for upload in files:
        rel = getattr(upload, "filename", "") or ""
        data = await upload.read()
        if len(data) > staging.MAX_UPLOAD_BYTES:
            raise _err(
                "FILE_TOO_LARGE",
                f"「{rel}」{len(data) / 1048576:.1f}M 超过单文件 "
                f"{staging.MAX_UPLOAD_BYTES // 1048576}M 上限",
                status.HTTP_400_BAD_REQUEST,
            )
        payload.append((rel, data))

    try:
        staged = staging.stage_package(current_user.id, files=payload)
    except staging.StagingError as error:
        raise _err("STAGE_FAILED", str(error), status.HTTP_400_BAD_REQUEST) from error

    return {
        **staged.to_dict(),
        "precheck": _run_precheck(current_user, db, staged.root, package_name),
    }


class RescanRequest(BaseModel):
    """重跑预检的入参（**用 body 传参**，因为分组是个列表）。"""

    inspect: bool = Field(default=True, description="是否跑 OCR / 抽帧")
    #: 只用这些分组（空 = 全部分组）。
    groups: list[str] = Field(default_factory=list)


@router.post("/{token}/scan", status_code=status.HTTP_200_OK)
def rescan(
    token: str,
    payload: RescanRequest | None = None,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    """对已登记的数据包重跑预检。

    - `inspect=true`（默认）：跑违规词 OCR / 视频抽帧（改了词库后再扫一次）；
    - `inspect=false`：只重新解析（媒体清单 + 表格）；
    - `groups`：只用这几个分组匹配（空 = 全部分组）。

    ⚠️ 入参走 **body**（不是 query）—— 分组是列表，塞 query 会退化成
    `?groups=a&groups=b` 的重复键，FastAPI 读起来和前端拼起来都容易错。
    """
    try:
        root = staging.resolve_package(current_user.id, token)
    except staging.StagingError as error:
        raise _err("STAGE_GONE", str(error), status.HTTP_404_NOT_FOUND) from error

    opts = payload or RescanRequest()
    try:
        return {
            "token": token,
            "precheck": _run_precheck(
                current_user,
                db,
                root,
                root.name,
                inspect=opts.inspect,
                groups=opts.groups,
                progress_token=token,
            ),
        }
    finally:
        _clear_progress(token)


@router.get("/{token}/progress", status_code=status.HTTP_200_OK)
def get_progress(token: str, current_user: User = Depends(get_current_user)) -> dict:
    """**执行进度**（用户口径 2026-10-09）：预检跑 OCR/抽帧期间前端轮询。

    返回 `{phase, done, total, percent}`；不在跑时 `phase=null`、`percent=0`。
    """
    info = _get_progress(token)
    if not info:
        return {"phase": None, "done": 0, "total": 0, "percent": 0}
    total = int(info.get("total") or 0)
    done = int(info.get("done") or 0)
    percent = int(done * 100 / total) if total > 0 else 0
    return {"phase": info.get("phase"), "done": done, "total": total, "percent": percent}


#: 扩展名 → 浏览器可用的 MIME（用户口径 2026-10-08：「支持多种视频格式预览播放」）。
#:
#: ⚠️ 为什么不能只靠 `FileResponse` 自己猜：实测本机 `mimetypes.guess_type`
#: 对 `.flv` 返回 **None**（⇒ 响应没有 Content-Type，`<video>` 直接不播），
#: `.avi`/`.mkv` 给的是 `video/avi` / `video/x-matroska`（不是标准注册类型）。
#: 表里优先给**标准** MIME，避免「文件没问题但播放器不认」。
_MEDIA_TYPES: dict[str, str] = {
    # 图片
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    # 视频（浏览器原生可播的这几个最重要）
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".ogg": "video/ogg",
    # 可抽帧、但浏览器多半放不了（仍给个正确类型，前端据此提示下载）
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".ts": "video/mp2t",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".3gp": "video/3gpp",
}


def _media_type_for(target: Path) -> str:
    """按扩展名定 MIME；表里没有就回退 `mimetypes`，再不行给 octet-stream。"""
    known = _MEDIA_TYPES.get(target.suffix.lower())
    if known:
        return known
    guessed, _ = mimetypes.guess_type(target.name)
    return guessed or "application/octet-stream"


@router.get("/{token}/image")
def get_image(token: str, path: str, current_user: User = Depends(get_current_user)):
    """取包内的图片 / 视频（前端画红框、播视频都用它）。

    ⚠️ **路径必须在包根目录内** —— `path` 来自前端，直接拼就是路径穿越。
    ⚠️ 返回 `FileResponse`（**不是** `-> FileResponse` 注解）：视频要支持
    HTTP Range（拖动进度条 / 边下边播），Starlette 的 `FileResponse` 原生
    处理 `Range` 头，但前提是别用自定义 `StreamingResponse` 把它绕掉。
    """
    try:
        root = staging.resolve_package(current_user.id, token).resolve()
    except staging.StagingError as error:
        raise _err("STAGE_GONE", str(error), status.HTTP_404_NOT_FOUND) from error

    #: ⚠️ 路径必须在包根目录内 —— `path` 来自前端，直接拼就是路径穿越。
    #: （`resolve()` 之后再判父目录，`..` 已被解析掉，所以这个判断是真的兜底。）
    target = (root / str(path or "")).resolve()
    if root not in target.parents or not target.is_file():
        raise _err("IMAGE_NOT_FOUND", "资源不存在或路径越界", status.HTTP_404_NOT_FOUND)

    return FileResponse(
        str(target),
        #: 显式给 MIME —— 靠猜会把 `.flv` 猜成 None（实测），
        #: 那样 `<video>` 收到没有 Content-Type 的响应，直接不播。
        media_type=_media_type_for(target),
        #: 让浏览器能按 Range 分段取（视频拖动/续播的硬要求）。
        headers={"Accept-Ranges": "bytes"},
    )


@router.delete("/{token}", status_code=status.HTTP_200_OK)
def discard(token: str, current_user: User = Depends(get_current_user)) -> dict:
    """丢弃暂存包。"""
    staging.drop_staged(current_user.id, token)
    return {"token": token, "dropped": True}


__all__ = ["router", "MEDIA_GROUPS"]
