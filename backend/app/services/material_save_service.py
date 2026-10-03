"""生成结果**保存到素材库**（v2.29，设计 17 §13.3.10）。

用户口径（2026-10-03 原话）：

> 「生成的图使用缩小图展示，**支持选择性保存到指定素材库**，
>   支持**全部保存**到指定素材库，素材库内保存时你来决定一下如何创建子目录和图片名称，
>   **每次生成保存时放不同的子目录下**」

## 目录与命名（本节即口径，改动要同步设计文档）

```
<素材库根目录>/
  ai-生成/                       ← 固定前缀，与用户手放的文件区分开
    20261003-173045-<批次短码>/   ← **每次生成一个子目录**（用户口径）
      主图-01.png
      主图-02.png
      详情图-01.png
```

* **固定前缀 `ai-生成/`**：用户素材库里既有手放的文件、也有历史结构。把 AI 产物
  收在一个顶层目录下 ⇒ 用户一眼能分辨「哪些是机器生成的」，也方便整体删除。
  用中文前缀（而非 `ai-gen`）是因为**用户要直接看目录树**，中文比拼音缩写直观。
* **子目录 = `YYYYMMDD-HHMMSS-<短码>`**：时间戳让人能按时间排序/定位
  （「昨天下午那批」），短码（4 位十六进制）防**同一秒两次生成**撞名
  （同一秒触发的两个任务时间戳完全相同 —— 只靠时间戳会合并进同一目录，
  违背「每次生成放不同的子目录」）。
* **文件名 = `<组名>-<两位序号>.<ext>`**：组名用中文（`主图` / `详情图`，与界面一致），
  序号**两位补零**（`01`…`10`）⇒ 字典序=自然序，文件管理器里顺序正确。
  扩展名从响应 URL 推断，取不到回落 `.png`（**不臆造**：拿不到就按 png，
  这是下游模型的实际输出格式，见实测）。

## 为什么保存动作在**后端**做（不在前端下载再上传）

1. 出图结果是**上游临时 URL**（带签名、会过期）—— 前端「下载再上传」多一跳，
   且大图经浏览器内存来回，慢且可能失败；
2. 素材库是**本地绝对路径**（`material_folders.path`），只有后端进程能写；
3. 落盘后要**建 `material_files` 索引行**（否则素材库面板看不到新文件）。
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from typing import Any

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import AigcMediaAsset, MaterialFolder
from backend.app.services.material_storage import (
    MaterialStorageError,
    get_material_storage,
)

#: 顶层目录名（AI 产物与用户手放文件的分界）。
AI_ROOT_DIRNAME = "ai-生成"

#: 组标识 → 目录/文件名里的中文组名（与界面文案一致，不另起一套）。
GROUP_LABELS: dict[str, str] = {"product": "主图", "detail": "详情图"}

#: 下载超时（秒）。出图结果是 CDN 直链，正常 1~3 秒；给足避免大图被掐。
DOWNLOAD_TIMEOUT_SECONDS = 60


class MaterialSaveError(RuntimeError):
    """保存到素材库失败（结构化原因，不吞）。"""


def _ext_from_url(url: str) -> str:
    """从结果 URL 推扩展名（取不到回落 `.png` —— 上游实际输出格式，不臆造）。"""
    match = re.search(r"\.(png|jpe?g|webp|gif)(?:\?|$)", url or "", re.IGNORECASE)
    if not match:
        return ".png"
    ext = match.group(1).lower()
    return ".jpg" if ext == "jpeg" else f".{ext}"


def new_batch_dirname(now: datetime | None = None) -> str:
    """生成一个**本次专属**的子目录名（用户口径「每次生成放不同的子目录下」）。

    形态 `YYYYMMDD-HHMMSS-<4位短码>`：
    * 时间戳 ⇒ 可排序、可定位（「昨天下午那批」）；
    * 短码 ⇒ **防同秒撞名**（同一秒触发的两个任务时间戳相同，
      只靠时间戳会合并进同一目录、违背「每次」）。
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def save_assets_to_folder(
    db: Session,
    *,
    user_id: int,
    folder_id: int,
    asset_ids: list[int],
    asset_groups: dict[int, str] | None = None,
) -> dict[str, Any]:
    """把生成结果存进指定素材库文件夹（**新建一个本次专属子目录**）。

    返回 `{"folder_id", "dir", "saved": [{"asset_id", "rel_path", "filename"}], "errors": [...]}`。

    ⚠️ **部分成功按成功处理**（与出图一致）：某张下载失败不中断整批，
    失败原因如实进 `errors` —— 用户最需要的是「哪些成了、哪些为什么没成」，
    而不是整批回滚。
    """
    folder = db.scalar(
        select(MaterialFolder).where(
            MaterialFolder.id == folder_id,
            MaterialFolder.user_id == user_id,
        )
    )
    if folder is None:
        raise MaterialSaveError("素材库文件夹不存在或不属于当前用户")

    storage = get_material_storage()
    root = folder.path
    ok, reason = storage.validate_root(root)
    if not ok:
        raise MaterialSaveError(f"素材库路径不可用：{reason}")

    assets = list(
        db.scalars(
            select(AigcMediaAsset).where(
                AigcMediaAsset.id.in_(asset_ids),
                AigcMediaAsset.user_id == user_id,
            )
        ).all()
    )
    if not assets:
        raise MaterialSaveError("没有可保存的生成结果（可能不属于当前用户）")

    # v2.31：目录构造走**存储抽象**（本地 = 真建目录；将来云上是空操作，key 前缀天然如此）
    batch_dirname = new_batch_dirname()
    try:
        storage.make_dir(root, f"{AI_ROOT_DIRNAME}/{batch_dirname}")
    except MaterialStorageError as exc:
        raise MaterialSaveError(str(exc)) from exc

    saved: list[dict[str, Any]] = []
    errors: list[str] = []
    # 组内序号：同一组内从 01 起递增（跨组各自计数 —— 组名已经在文件名里区分了）
    counters: dict[str, int] = {}

    for asset in assets:
        group = _group_of_asset(asset, asset_groups or {})
        counters[group] = counters.get(group, 0) + 1
        label = GROUP_LABELS.get(group, group or "图")
        ext = _ext_from_url(asset.url or "")
        filename = f"{label}-{counters[group]:02d}{ext}"
        rel_path = f"{AI_ROOT_DIRNAME}/{batch_dirname}/{filename}"

        try:
            _download_to(storage, root, rel_path, asset.url or "")
        except Exception as exc:  # noqa: BLE001 —— 一张失败不拖累整批
            errors.append(f"{filename}：{exc}")
            continue
        # ⚠️ **不在这里建索引行**（v2.29 真机实测踩到）：
        # 手工行缺 `width`/`height`/**`thumb_url`** ⇒ 素材库面板渲染空白
        # （用户现象「素材库中的图片全部无法展示」）。而且**事后补跑扫描也救不回来** ——
        # 扫描是**按 mtime 增量**的，我这里写了 mtime ⇒ 扫描认为「没变化」直接跳过。
        # ⇒ 唯一正确做法：**只落文件，索引交给扫描**（建行 + 算尺寸 + 出缩略图
        # 的唯一实现；重写一遍必然在某条分支上分叉，DEF-044）。
        saved.append(
            {"asset_id": asset.id, "rel_path": rel_path, "filename": filename}
        )

    db.commit()

    # ⚠️ **必须再跑一次扫描**（v2.29，真机实测踩到）：
    # 手工写的索引行缺 `width` / `height` / **`thumb_url`** ⇒ 素材库面板
    # 显示不出缩略图（前端读 `thumb_url`，空串就渲染成空白）——
    # 用户的现象是「素材库中的图片全部无法展示」。
    #
    # 为什么不在这里自己算尺寸/生成缩略图：那是**扫描服务的职责**
    # （它已经处理了「视频无缩略图」「损坏图回落」等分支）。
    # 重写一遍 = 同一语义两份实现（DEF-044），且必然在某条分支上分叉。
    # 扫描是**按 mtime 增量**的：刚写的文件 mtime 未变 ⇒ 只补元数据，
    # 不会重复插入行（唯一键 `(folder_id, rel_path)` 也在兜底）。
    # ⚠️ 索引行**完全依赖**这次扫描（不再自建）⇒ 扫描失败必须**显式报出**：
    # 静默吞掉的话，用户看到「保存成功」却在素材库里找不到文件（最难查的一类）。
    scanned = None
    if saved:
        from backend.app.services.material_scan_service import scan_folder

        try:
            scanned = scan_folder(db, folder)
        except Exception as exc:  # noqa: BLE001
            raise MaterialSaveError(
                f"文件已写入磁盘，但素材库索引失败（文件不会被列出）：{exc}"
            ) from exc

    return {
        "folder_id": folder.id,
        "folder_name": folder.name,
        "dir": f"{AI_ROOT_DIRNAME}/{batch_dirname}",
        "saved": saved,
        **( {"errors": errors} if errors else {}),
        **( {"scan": scanned} if scanned else {}),
    }


def _group_of_asset(asset: AigcMediaAsset, asset_groups: dict[int, str]) -> str:
    """资产属于哪一组（`product` / `detail`）—— **由调用方从任务 payload 传进来**。

    ⚠️ `AigcMediaAsset` **没有 group 列**：分组事实只在生成那一刻的任务响应里
    （`payload.groups`）。所以本函数**不自作推断** —— 调用方给什么就是什么，
    给不出就返回空串 ⇒ 文件名用 `图-NN`（**不猜**：猜错会让用户以为分组是权威的，
    那比「没分组」更坏 —— 同「缺值不造假」口径）。
    """
    return asset_groups.get(asset.id, "")


def _download_to(storage, root: str, relative: str, url: str) -> None:
    """把一个结果 URL 写到「根 + 相对标识」处（v2.31：走**存储抽象**）。

    ⚠️ 流式写入（**不整张进内存**）—— 原图实测 6.5 MB，几十张一起下会把内存打满。
    抽象层的 `write_stream` 就是为了保住这个性质（云实现里对应 multipart 上传）。
    """
    if not url:
        raise MaterialSaveError("结果没有图片地址（服务商未返回 url）")
    if url.startswith("data:"):
        # 个别服务商回 b64 data URL —— 解出二进制直接写
        import base64

        _, _, payload = url.partition(",")
        storage.write(root, relative, base64.b64decode(payload))
        return
    response = requests.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS, stream=True)
    response.raise_for_status()
    storage.write_stream(root, relative, response.iter_content(chunk_size=64 * 1024))

