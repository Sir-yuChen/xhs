"""数据包**暂存**（设计 25 §4）：上传 → 落盘 → 预检 → 清理。

## 为什么需要「暂存」这一层

预检要 OCR 图片 ⇒ 后端必须**真的拿到图片字节**。但数据��在用户电脑上，
浏览器只能逐个文件 `POST`。所以：

```
前端 webkitdirectory ──逐文件 POST──▶ 暂存目录（<storage>/package_precheck/<token>/）
                                        │
                                        ├─ 预检解析（纯本地）
                                        ├─ OCR 违规词（纯本地）
                                        ▼
                                     预检结果返回（只带相对路径 + 坐标，不带字节）
```

⚠️ **暂存目录按 `user_id + token` 隔离**，且 token 由服务端生成
（不接受前端自带的路径，杜绝路径穿越）。
⚠️ **不长期留**：预检结果带 `expires_at`，过期由清理任务删。
"""

from __future__ import annotations

import logging
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.app.core.config import get_settings

logger = logging.getLogger(__name__)

#: 暂存目录名（放在 storage 下，与素材库同级但**独立**）。
STAGING_DIRNAME = "package_precheck"

#: 暂存保留时长（默认 2 小时）—— 预检只是过渡态，不该长期占盘。
STAGING_TTL = timedelta(hours=2)

#: 单文件上传上限（50 MB）—— 视频可能比图大，但再大就不是商品素材了。
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

#: 允许的扩展名（上传路径用）。
#:
#: ⚠️ 用户口径 2026-10-13（**方案 C**）：「展示与上传都按平台，但把不合规的
#: 列出来而不是丢掉」⇒ 与 `shop_draft_package.IMAGE_EXTS` / `VIDEO_EXTS` 同口径。
#: ⚠️ 那份「黑名单」（本轮中间口径「不限制后缀」，已**被方案 C 取代**）已撤掉 ——
#: 上传路径若放行平台不收的格式，会在发布侧才炸，那是更晚也更难查的失败点。
_ALLOWED_SUFFIXES = frozenset(
    {
        ".jpg", ".jpeg", ".png",
        #: 与 `shop_draft_package.VIDEO_EXTS` 保持一致（多格式视频，用户口径 2026-10-08）。
        ".mp4", ".m4v", ".mov", ".webm", ".ogv", ".ogg",
        ".avi", ".mkv", ".flv", ".wmv", ".ts", ".mpg", ".mpeg", ".3gp",
        ".xlsx", ".xlsm",
    }
)

#: **绝不能**出现在路径里的（路径穿越的基本形态）。
#:
#: ⚠️ `/` 与 `\` 是**合法的分隔符**，不能整串扫 —— 第一版这么写导致
#: `SKU/SKU01_….jpg` 这种**完全正常**的相对路径被判非法（实测踩到）。
#: 正确做法是**先按分隔符切开，再逐段校验**。
_FORBIDDEN_SEGMENTS: frozenset[str] = frozenset({".", "..", ""})
_FORBIDDEN_CHARS: tuple[str, ...] = ("\x00", ":", "*", "?", '"', "<", ">", "|")


class StagingError(ValueError):
    """暂存失败（路径非法 / 超大 / 扩展名不支持）。"""


@dataclass
class StagedPackage:
    token: str
    root: Path
    file_count: int
    expires_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "token": self.token,
            "file_count": self.file_count,
            "expires_at": self.expires_at.isoformat(),
        }


def staging_root() -> Path:
    """暂存根目录，**必须是绝对路径**。

    ⚠️ 这里原来直接 `Path(get_settings().storage_dir) / STAGING_DIRNAME`，
    而 `storage_dir` 本身是**相对**路径（实测就是
    `backend/app/storage/package_precheck/...`）。

    相对路径在 OCR 这条链上是**致命的**：暂存图路径会原样交给
    PowerShell 子进程，子进程按**自己的** cwd 解析 ⇒
    `Test-Path` 直接 False ⇒ 每张图都 `OCR_NO_OUTPUT`
    （实测：真实包 69 张走暂存路径 0/6 成功，走原始绝对路径 1/1 成功，
    同一进程、同一张图、同一份代码 —— 差别只有路径是相对还是绝对）。

    所以这里 `.resolve()` 一次，把「相对性」掐死在源头，而不是让每个
    调用方各自记得 resolve（漏一处就复现一次）。
    """
    return (Path(get_settings().storage_dir) / STAGING_DIRNAME).resolve()


def new_token() -> str:
    """服务端生成 token（**不用前端传的**，杜绝路径穿越）。"""
    return secrets.token_urlsafe(16)


# ---------------------------------------------------------------------------
# 本地目录引用（用户 2026-10-08 口径：**不上传**）
# ---------------------------------------------------------------------------

#: token → 本地绝对路径。**进程内内存**（不落盘）。
#:
#: ⚠️ 为什么用内存而不是写文件：这是「用户在自己电脑上指了个目录」这件事的
#: 引用，不是数据。重启进程 = 引用失效（前端会重新指一次），这是可接受的；
#: 写盘反而留下「谁在什么时候读了哪个目录」的历史记录，没有必要。
_LOCAL_REFS: dict[tuple[int, str], Path] = {}


def register_local(
    user_id: int,
    package_path: str | Path,
    token: str | None = None,
) -> tuple[str, Path]:
    """登记一个**本地数据包目录**，返回可读的绝对路径。

    用户 2026-10-08 口径：「图片全部展示加载本地数据包内的媒体资源不要上传」
    —— 所以这条路径是主路径，上传那条是兼容的老路径。

    ⚠️ **只读**：我们只在本地读，绝不写、绝不删用户目录。
    ⚠️ **token 仍由服务端生成** —— 前端传来的路径只是「指哪」，token 才是后续
    取图时会再校验一次「请求的文件在这个目录内」。

    返回 `(token, 绝对路径)` —— token 必须由服务端生成并回给前端，
    前端后续所有取图都靠它。
    """
    raw = str(package_path or "").strip().strip('"')
    if not raw:
        raise StagingError("请填写商品数据包目录")
    if any(bad in raw for bad in ("\x00",)):  # NUL 字节
        raise StagingError("路径含非法字符")
    root = Path(raw)
    if not root.is_absolute():
        raise StagingError("请填写绝对路径（例如 C:/Users/xxx/Desktop/商品ID_935947169189）")
    root = root.resolve()
    if not root.is_dir():
        raise StagingError(f"目录不存在：{root}")
    token = token or new_token()
    if any(bad in token for bad in ("/", "\\", "..")):
        raise StagingError("token 含非法字符")
    _LOCAL_REFS[(int(user_id), token)] = root
    return token, root


def resolve_local(user_id: int, token: str) -> Path:
    """取登记过的本地目录（**归属校验在这一行**：键含 `user_id`）。"""
    if any(bad in str(token or "") for bad in ("/", "\\", "..")):
        raise StagingError("token 含非法字符")
    root = _LOCAL_REFS.get((int(user_id), str(token)))
    if root is None:
        raise StagingError("数据包会话已失效，请重新选择数据包目录")
    if not root.is_dir():
        _LOCAL_REFS.pop((int(user_id), str(token)), None)
        raise StagingError("数据包目录已不存在或已被移动，请重新选择")
    return root


def _safe_rel_path(raw: str) -> str:
    """上传的相对路径 → 安全的暂存内相对路径（**逐段校验**）。

    只保留 `<区域>/<文件名>` 两级（预检只需要这个粒度），丢掉任何
    `..`、绝对路径前缀、盘符、隐藏目录。

    ⚠️ 分隔符 `/` `\\` 本身**合法**（`主图/主图01.jpg` 是常态），
    所以校验必须**切段后逐段做**，不能整串扫分隔符。
    """
    text = str(raw or "").replace("\\", "/").strip()
    if not text:
        raise StagingError("上传文件缺少相对路径")
    for bad in _FORBIDDEN_CHARS:
        if bad in text:
            raise StagingError(f"上传路径含非法字符：{raw}")
    parts = text.split("/")
    #: 绝对路径（`/x`、`C:/x`）的形态：首段为空，或首段是盘符
    if parts and parts[0] in _FORBIDDEN_SEGMENTS:
        raise StagingError(f"上传路径必须是相对路径：{raw}")
    parts = [p for p in parts if p not in _FORBIDDEN_SEGMENTS]
    if not parts:
        raise StagingError(f"上传路径无效：{raw}")
    if len(parts) > 2:
        # 只取最后两级（区域/文件名）—— 数据包不会有更深层
        parts = parts[-2:]
    suffix = Path(parts[-1]).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise StagingError(f"不支持的文件类型：{suffix or '（无扩展名）'}")
    return "/".join(parts)


def stage_package(
    user_id: int,
    token: str | None = None,
    files: list[tuple[str, bytes]] | None = None,
) -> StagedPackage:
    """把一批 `(相对路径, 字节)` 落盘到暂存目录。

    ⚠️ **同 token 重复调用 = 覆盖**（前端重传同一批时不必先清）。
    """
    token = token or new_token()
    if any(bad in token for bad in ("/", "\\", "..")):
        raise StagingError("token 含非法字符")

    root = staging_root() / f"u{user_id}" / token
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    count = 0
    for raw_rel, payload in files or []:
        rel = _safe_rel_path(raw_rel)
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        count += 1

    expires = datetime.now(timezone.utc) + STAGING_TTL
    return StagedPackage(token=token, root=root, file_count=count, expires_at=expires)


def resolve_package(user_id: int, token: str) -> Path:
    """**统一**取包根目录：先查本地引用，回落暂存目录（老的上传路径）。

    这样上层端点不必关心包是怎么来的。
    """
    try:
        return resolve_local(user_id, token)
    except StagingError:
        return resolve_staged(user_id, token)


def resolve_staged(user_id: int, token: str) -> Path:
    """取某用户某 token 的暂存目录（**归属校验在这一行**）。"""
    if any(bad in str(token or "") for bad in ("/", "\\", "..")):
        raise StagingError("token 含非法字符")
    root = staging_root() / f"u{user_id}" / token
    if not root.is_dir():
        raise StagingError("数据包已过期或不存在，请重新选择文件夹")
    return root


def drop_staged(user_id: int, token: str) -> None:
    """丢弃一个数据包会话。

    ⚠️ **本地目录只解除引用，绝不删用户的目录**（那是他自己的数据）。
    暂存的才真删。
    """
    if _LOCAL_REFS.pop((int(user_id), str(token)), None) is not None:
        return
    try:
        root = resolve_staged(user_id, token)
    except StagingError:
        return
    shutil.rmtree(root, ignore_errors=True)


def purge_expired(user_id: int | None = None) -> int:
    """清过期暂存（按目录 mtime 判龄）。返回删除的目录数。"""
    base = staging_root()
    if not base.is_dir():
        return 0
    removed = 0
    users = [base / f"u{user_id}"] if user_id is not None else list(base.iterdir())
    for user_dir in users:
        if not user_dir.is_dir():
            continue
        for pkg_dir in list(user_dir.iterdir()):
            if not pkg_dir.is_dir():
                continue
            age = datetime.now(timezone.utc).timestamp() - pkg_dir.stat().st_mtime
            if age > STAGING_TTL.total_seconds():
                shutil.rmtree(pkg_dir, ignore_errors=True)
                removed += 1
    return removed
