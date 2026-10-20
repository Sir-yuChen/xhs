"""笔记媒体生成（设计 17 §13.4，T7 图片先行）。

语义：LLM 生成笔记时，按 `media_source` 产出**笔记媒体**（封面 / 配图）。

| `media_source` | 行为 |
|---|---|
| `product_image` | 商品图直链（**不下载**：平台侧可直接取千帆 CDN；与一期生成一致） |
| `material` | 从本地素材库取用户勾选的图 ⇒ `note_assets` 用**本地下载 URL** |
| `reference_image` | 参考图（前端给 or 台账首图）⇒ 图像模型**图生图** ⇒ 下载落盘 |
| `ai_generate` | 文生图（无参考）或图生图（带参考）⇒ 下载落盘 |

## 硬约束

- **只写 `storage/media/`**（§10 安全设计）：**绝不**写用户素材文件夹（C2 只读边界）。
- **失败不废正文**（§13.4.2）：出图失败 ⇒ 记日志 + 返回空媒体，**笔记照常落库**。
  调用方（worker）不得因本模块抛错而把整条生成打成失败 —— 本模块内部已吞掉
  **可归因**的媒体错误（出图失败 / 下载失败 / 归属不匹配），只在**配置级**错误
  （业务点未配 / 类型不符）时向上抛 `MediaGenerationError`，交由 worker 决定是否降级。
- **不造假**（§5 硬约束 5）：取不到就返回空列表，**不塞一张占位图**。

## 为什么独立成模块

`note_gen_llm_worker.py` 的 `_media_assets_for` 是**同步纯函数**（只读行体拼 URL）；
本轮要真正**调用上游出图 + 下载落盘 + 记溯源**，涉及网络 IO 与事务，塞进 worker
会让那一段变成「几百行的混合体」。独立成模块后 worker 只做「拿结果 → 落行」。
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.models import MaterialFile, ShopProduct
from backend.app.services import image_prompt
from backend.app.models.generation import (
    AigcMediaAsset,
    MEDIA_SOURCE_AI_GENERATE,
    MEDIA_SOURCE_MATERIAL,
    MEDIA_SOURCE_PRODUCT_IMAGE,
    MEDIA_SOURCE_REFERENCE_IMAGE,
    MEDIA_SOURCE_VIDEO_AI,
    MEDIA_SOURCE_VIDEO_MATERIAL,
    MEDIA_SOURCE_VIDEO_REFERENCE,
    MEDIA_SOURCES,
)

logger = logging.getLogger(__name__)

#: 单张媒体下载上限（防上游回传巨型文件拖垮 worker）。
MAX_MEDIA_BYTES = 40 * 1024 * 1024
#: 单个视频下载上限（§13.4.4）：视频比图片大得多，独立给上限（用图片的 40MB 会误杀正常视频）。
MAX_VIDEO_BYTES = 300 * 1024 * 1024
#: `ai_generate` 出图张数上限（§13.4.1：默认 1、上限 4）。
MAX_AI_GENERATE_COUNT = 4
#: 参考图最多送几张（用户口径 2026-10-16：*「支持多选最多4个」*）。
#: ⚠️ 原为 **1**（只送首图）；详情图可选后必须放宽 —— 否则用户勾了 4 张，
#: 服务端**静默只送第 1 张**（本仓最厌恶的静默失效）。前端也有同值上限（`MAX_REFERENCE_IMAGES`）。
MAX_REFERENCE_IMAGES = 4
#: 视频时长上限（秒）与默认值（§13.4.4）。
MAX_VIDEO_DURATION = 10
DEFAULT_VIDEO_DURATION = 5


#: 最近一次「出图调用失败」的可读原因（空 = 没报过错）。
#: ⚠️ 用模块级槽位而不是返回值/属性：内置 `list` 挂不了属性，改函数签名要动 9 处调用。
#: 同线程同步调用（worker 逐段跑），读完即由调用方取走，不存在跨请求污染。
_LAST_GENERATE_ERROR = ""


class MediaGenerationError(Exception):
    """媒体生成的**可归因**错误（配置级 / 门槛级）。

    与「出图失败就跳过」的内部降级不同：本异常表示「这件事本身不该发生」
    （如 `media_source` 非法、素材文件不属于本人），调用方应据此**明确失败**
    该条生成（而非默默产出无媒体的笔记）。
    """

    def __init__(self, message: str, *, code: str = "LLM_MEDIA_GENERATION_FAILED") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #
def validate_media_options(media_source: str, media_options: Optional[dict]) -> None:
    """同步段门禁：`media_options` 与 `media_source` 是否匹配（§13.4.1 错误口径）。

    ⚠️ 在**入队前**调用（端点同步段）⇒ 不匹配直接 400 `LLM_MEDIA_OPTIONS_INVALID`，
    不入队（与 `gate_for_llm_generation` 同性质的确定性答案）。
    """
    if media_source not in MEDIA_SOURCES:
        raise MediaGenerationError(
            f"未知的媒体来源：{media_source}", code="LLM_MEDIA_OPTIONS_INVALID"
        )
    options = media_options if isinstance(media_options, dict) else {}
    if media_source == MEDIA_SOURCE_MATERIAL:
        # ⚠️ 用 `_material_file_ids` 归一（`file_ids` / `material_file_ids` 两个键名等价）
        # —— 这里手写 `options.get("material_file_ids")` 会让组合段（发 `file_ids`）
        # 被判成「没选素材」，前端选得好好的却被 400 打回（静默不兼容）。
        if not _material_file_ids(options):
            raise MediaGenerationError(
                "媒体来源为素材库时必须选择至少一张素材图。",
                code="LLM_MEDIA_OPTIONS_INVALID",
            )
    if media_source == MEDIA_SOURCE_VIDEO_MATERIAL:
        #: 「本地素材」用于视频笔记：**必须选到视频素材**（用户口径 2026-10-16：
        #: *「使用本地素材这无需再生成视频」*）。
        #: ⚠️ 同步段只判「选没选」；**「选的是不是视频」在取数时判**
        #: （`_assets_from_material_video`）—— 那里能拿到行、能给出「你选的是图片」
        #: 这种可自救的原因。这里再判一次要多查一次库，且判据重复（DEF-044）。
        if not _material_file_ids(options):
            raise MediaGenerationError(
                "「本地素材」用于视频笔记时，必须选择一条视频素材。",
                code="LLM_MEDIA_OPTIONS_INVALID",
            )
    if media_source in (MEDIA_SOURCE_AI_GENERATE, MEDIA_SOURCE_REFERENCE_IMAGE):
        #: 参考图上限（用户口径 2026-10-16：*「支持多选最多4个」*）。
        #: ⚠️ **必须在后端拦**：前端限制只是提示，绕过前端（直接调 API）时
        #: 超量会被 `reference_urls[:MAX]` **静默截断** —— 用户以为送了 6 张、
        #: 实际只送 4 张（静默失效）。给结构化 400 让他知道。
        refs = _str_list(options, "reference_image_urls")
        if len(refs) > MAX_REFERENCE_IMAGES:
            raise MediaGenerationError(
                f"参考图最多 {MAX_REFERENCE_IMAGES} 张（实得 {len(refs)} 张）。",
                code="LLM_MEDIA_OPTIONS_INVALID",
            )


def _material_file_ids(media_options: Optional[dict]) -> list[int]:
    """素材文件 id 列表（**两个键名等价，只在此处归一** —— 组合段用 `file_ids`）。

    ⚠️ 为什么有两个名字：单来源时代端点传 `material_file_ids`（`media_options` 整体
    就是参数载体）；v2.27 组合段把参数**平铺在段上**，前端按 `LlmMediaOptions.file_ids`
    发（段的内聚命名）。两者是同一个东西，**归一在唯一读点**（不写两份实现，DEF-044）。
    """
    options = media_options if isinstance(media_options, dict) else {}
    raw = options.get("file_ids")
    if not isinstance(raw, list):
        raw = options.get("material_file_ids")
    if not isinstance(raw, list):
        return []
    cleaned: list[int] = []
    for value in raw:
        try:
            cleaned.append(int(value))
        except (TypeError, ValueError):
            continue
    # 去重且保序（用户勾选的顺序即展示顺序）。
    return list(dict.fromkeys(cleaned))


def _str_list(options: Optional[dict], key: str) -> list[str]:
    source = options if isinstance(options, dict) else {}
    raw = source.get(key)
    if not isinstance(raw, list):
        return []
    return [str(value).strip() for value in raw if isinstance(value, str) and str(value).strip()]


def _product_image_urls(row: ShopProduct) -> list[str]:
    """**商品原图**（用户口径 2026-10-16：*「商品原图是包括商品主图和详情图」*）。

    ⚠️ 顺序 = **主图在前、详情图在后** —— 这个顺序是**前后端共同的契约**：
    前端把两组按同样顺序拼成一个列表给用户选（返回的 `pick` 是这个合并列表的下标），
    后端必须用**同一个**列表去解析下标。两边顺序不一致 ⇒ 用户选第 3 张、实际出第 5 张
    （静默错位，最难查的那种）。改顺序 = 改契约，两处必须一起改。

    ⚠️ **必须去重、且保序**（用户口径 2026-10-17：*「商品原图实现图片去重」*）——
    真机实测：抽查 400 个商品 **400 个**都有重复图，典型是主图 10 张里
    有 5 张原样重复出现在详情图组（`20 张 → 15 张唯一`）⇒ 用户看到同一张图
    重复出现、选了两个位置其实是同一张。

    ⚠️ 去重**只能保序去重**（首次出现的位置为准），因为 `pick` 是**下标**：
    - 后端这里去重后，`_assets_from_product_image` 的 `pick` 按下标解析；
    - **前端必须用同一份去重后的列表**（`ProductImagePicker` 也去重），
      否则「用户选第 3 张、实际出第 5 张」（静默错位，最难查的那种）。
    改这里 = 改契约，两处必须一起改。
    """
    merged = _main_image_urls(row) + _detail_image_urls(row)
    #: 保序去重：`dict.fromkeys` 语义（首次出现为准），不排序。
    seen: set[str] = set()
    unique: list[str] = []
    for url in merged:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def product_image_name(url: str) -> str:
    """图片 URL → **图片名称**（URL 末段，去查询串）。

    商品图：`http://qimg.xiaohongshu.com/arkgoods/1040g3no…upuas2o` ⇒ `1040g3no…upuas2o`。

    ⚠️ 这是**前后端共同的命名契约**（`pick` 传名称）：
    前端 `productImageName()` 与本函数必须同一判据，否则「选中的图」与「实际出的图」
    对不上（改一处 = 改契约，两处一起改）。
    """
    text = str(url or "").strip()
    if not text:
        return ""
    #: 去掉查询串 / 片段（CDN 签名参数会让「同一个文件」有多个 URL）
    text = text.split("#", 1)[0].split("?", 1)[0]
    return text.rstrip("/").rsplit("/", 1)[-1]


def _main_image_urls(row: ShopProduct) -> list[str]:
    """商品**主图**（`raw_json.images`）—— 方图。"""
    import json

    try:
        urls = json.loads(row.images_json or "[]")
    except ValueError:
        return []
    return [str(url) for url in urls if isinstance(url, str) and url.strip()]


def _detail_image_urls(row: ShopProduct) -> list[str]:
    """商品**详情图**（`raw_json.image_descriptions[].link`）—— 竖长图（v2.15）。

    ⚠️ 与主图是**两套不同的图**（真机实测：主图 10 张方图、详情图 5 张竖长图），
    此前被混成一个列表 ⇒ 「原详情图从未被用作参考」。这里复用
    `shop_products_service.detail_image_links`（**同一实现**，不重抄）。
    """
    import json

    try:
        raw = json.loads(row.raw_json or "{}")
    except ValueError:
        return []
    if not isinstance(raw, dict):
        return []
    from backend.app.services.shop_products_service import detail_image_links

    return detail_image_links(raw)


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
def _media_dir() -> Path:
    return Path(get_settings().storage_dir) / "media"


def _download_to_media(url: str, *, user_id: int) -> tuple[str, str]:
    """把上游回传的图片 URL 下载到 `storage/media/`（`expected_kind="image"` 的薄包装）。"""
    return _download_media(url, user_id=user_id, expected_kind="image")


def _download_media(url: str, *, user_id: int, expected_kind: str) -> tuple[str, str]:
    """把上游回传的媒体 URL 下载到 `storage/media/`。

    返回 `(本地绝对路径, 本地下载 URL)`；失败抛 `ValueError`（由调用方降级）。

    ⚠️ **只写 `storage/media/`**，文件名带归属前缀 `xhs-asset-u{user_id}-`（与
    `files.py::_validate_owner_media_name` 的合法前缀白名单一致 —— 否则媒体
    文件服务会 404 这个文件）。

    ⚠️ `expected_kind` = `"image"` / `"video"`：**Content-Type 必须匹配**才落盘
    （上游回一个 HTML 错误页时不能当媒体存下来）。两种类型**共用这一份落盘实现**
    —— 分两份会在「大小上限 / 半条不留 / 空文件判定」上各写一遍（DEF-044）。
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("媒体 URL 为空")
    if url.startswith("/api/files/media/"):
        # 已是本地媒体文件（如素材库副本 / 上一轮已下载）：直接复用，不重复下载。
        file_name = url.split("/")[-1]
        local = _media_dir() / file_name
        if local.is_file():
            return str(local), url
        raise ValueError(f"本地媒体文件不存在：{file_name}")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError(f"无法识别的媒体 URL：{url[:64]}")
    try:
        response = requests.get(url, timeout=120, stream=True)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ValueError(f"媒体下载失败：{exc}") from exc
    content_type = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type and not content_type.startswith(f"{expected_kind}/"):
        raise ValueError(f"上游返回的不是{expected_kind}（Content-Type={content_type}）")
    default_ext = ".png" if expected_kind == "image" else ".mp4"
    extension = mimetypes.guess_extension(content_type or "") or default_ext
    if extension == ".jpe":
        extension = ".jpg"
    file_name = f"xhs-asset-u{user_id}-{uuid4().hex}{extension}"
    media_dir = _media_dir()
    media_dir.mkdir(parents=True, exist_ok=True)
    output_path = media_dir / file_name
    written = 0
    try:
        with output_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                # 视频比图片大得多，单独给上限（图片上限会误杀正常视频）。
                limit = MAX_MEDIA_BYTES if expected_kind == "image" else MAX_VIDEO_BYTES
                if written > limit:
                    raise ValueError("媒体文件超过大小上限")
                handle.write(chunk)
    except Exception:
        # 半条文件不留（DEF-048：不留中间态）。
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if written <= 0:
        output_path.unlink(missing_ok=True)
        raise ValueError("媒体下载得到空文件")
    return str(output_path), f"/api/files/media/{file_name}"


# --------------------------------------------------------------------------- #
# 四种来源
# --------------------------------------------------------------------------- #
def _note_image_limit() -> int:
    """笔记图片总数上限（**唯一判据在 `media_plan.resolve_note_image_limit`**，此处只转发）。

    ⚠️ 惰性 import：`media_plan` 也 import 本模块的常量 ⇒ 模块级互相 import 会成环。
    """
    from backend.app.services.media_plan import resolve_note_image_limit

    return resolve_note_image_limit()


def _assets_from_product_image(row: ShopProduct, media_options: Optional[dict] = None) -> list[dict]:
    """商品图直链（**不下载**）。

    ## `pick` 的取值 = **图片名称**（用户口径 2026-10-18）

    > 「必须是图片名称」

    图片名称 = **URL 末段**（商品图全名形如
    `1040g3no326bdrpd5k6204bna396tqa7iupuas2o`，`http://qimg.xiaohongshu.com/arkgoods/`
    只是它所在的桶前缀）。传名称而不传整条 URL / 下标，两个理由：

    * **不依赖任何一侧的排序**（下标会因前后端列表口径不同而错位 —— 真机实证
      「选第 3 张、实际出第 5 张」）；
    * **不依赖 CDN 域名 / 协议**（换桶、加签名参数都不影响匹配）。

    ⚠️ 名称在台账里找不到 ⇒ 忽略该条（商品图可能被重新同步过）；
    全部找不到 ⇒ 回落到前 N 张（宁可多给，不让整段变空 —— 无图 = 整篇失败）。
    """
    urls = _product_image_urls(row)
    options = media_options if isinstance(media_options, dict) else {}
    pick = options.get("pick")
    if isinstance(pick, list) and pick:
        #: 名称 → 完整 URL（同一份台账里名称唯一；`ruff` 式显式构造而非推导式便于读）
        by_name: dict[str, str] = {}
        for url in urls:
            by_name[product_image_name(url)] = url
        chosen: list[str] = []
        for value in pick:
            name = str(value or "").strip()
            #: 容错：用户/前端若把整条 URL 传来，取末段再匹配（**不改变主口径**）
            if "/" in name:
                name = product_image_name(name)
            url = by_name.get(name)
            #: ⚠️ **选中结果不去重**（用户口径 2026-10-19：*「重复不要去重」*）——
            #: 用户在同一段里点两次同一张、或封面与内页都选了同一张，都是**有意的**：
            #: 小红书一篇笔记里放两张相同的图是合法排版（例如首图与结尾呼应）。
            #: 原先 `url not in chosen` 会把它悄悄吞成一张 ⇒「选了 6 张、实际只出 5 张」。
            #: （**候选列表**的去重保留不动 —— 那个是防「同一张图在候选里出现两次」，
            #:  和「用户主动选重复」是两件事。）
            if url:
                chosen.append(url)
        if chosen:
            urls = chosen
        else:
            #: 全部名称都不在台账里 ⇒ 回落到**前 N 张**（宁可多给，不让整段变空 ——
            #: 无图 = 整篇失败）。N 取配置上限而不是写死 6。
            urls = urls[: _note_image_limit()]
    else:
        #: 没给 `pick` ⇒ 取前 N 张（**必须截断**：不截断会把台账几十张全画成笔记图，
            #: 实测 42 张商品图全进笔记 —— 平台单篇上限 18，必然发布失败）。
        urls = urls[: _note_image_limit()]

    return [
        {"asset_type": "image", "url": url, "local_path": "", "sort_order": index}
        for index, url in enumerate(urls)
    ]


def _assets_from_material(
    db: Session, *, user_id: int, media_options: Optional[dict]
) -> list[dict]:
    """素材库选取：按 `material_file_ids` 取行（**归属校验**，DEF-066）。

    取不到任何一张 ⇒ 抛 `MATERIAL_FILE_NOT_FOUND`（如实报缺，不造图）。
    素材取内容走 `/api/xhs/materials/files/{id}/file`（与本模块落盘 URL 语义一致）。
    """
    file_ids = _material_file_ids(media_options)
    if not file_ids:
        raise MediaGenerationError(
            "媒体来源为素材库时必须选择至少一张素材图。",
            code="LLM_MEDIA_OPTIONS_INVALID",
        )
    stmt = select(MaterialFile).where(
        MaterialFile.user_id == int(user_id), MaterialFile.id.in_(file_ids)
    )
    rows = {int(item.id): item for item in db.scalars(stmt).all()}
    missing = [fid for fid in file_ids if fid not in rows]
    if missing:
        raise MediaGenerationError(
            f"素材文件不存在或不属于当前用户：{missing}",
            code="MATERIAL_FILE_NOT_FOUND",
        )
    entries: list[dict] = []
    for index, file_id in enumerate(file_ids):
        item = rows[file_id]
        entries.append(
            {
                "asset_type": "image" if str(item.file_type) != "video" else "video",
                "url": f"/api/xhs/materials/files/{int(item.id)}/file",
                "local_path": str(item.rel_path or ""),
                "sort_order": index,
            }
        )
    return entries


def _assets_from_material_video(
    db: Session, *, user_id: int, media_options: Optional[dict]
) -> list[dict]:
    """**本地素材视频直接用**（不生成视频）—— 用户口径 2026-10-16。

    *「使用本地素材这无需再生成视频，后端根据商品，以及提示词，本地素材参考生成相关的
    文案标题话题等」* ⇒ 本条**只取素材里的视频当媒体**，**不调视频模型**（零成本、秒级）；
    文案链路照旧（借商品事实 + 提示词 + 该素材）。

    ⚠️ **只收视频**：图片素材对这个来源无意义（用户选的是「视频笔记」，拿到图就发不出视频）。
    选错类型 ⇒ 结构化 400 说清楚，不静默产出一张图。
    ⚠️ 取数复用 `_assets_from_material`（**同一实现**，不重抄一份归属校验 —— DEF-044）。
    """
    entries = _assets_from_material(db, user_id=user_id, media_options=media_options)
    videos = [e for e in entries if e.get("asset_type") == "video"]
    if not videos:
        raise MediaGenerationError(
            "「本地素材」用于视频笔记时，请选择视频素材（当前选的是图片）。",
            code="MATERIAL_VIDEO_REQUIRED",
        )
    #: ⚠️ 视频笔记平台**只接受 1 个视频** ⇒ 只取第一条，多的忽略
    #: （不静默多发 —— 多发会被平台校验拒，且用户以为选了几条就发几条）。
    return videos[:1]


def _default_image_prompt(row: ShopProduct) -> str:
    """`ai_generate` 未给 prompt 时，按商品拼一个通用出图提示词（不造假、可追溯）。

    ⚠️ v2.21：出图约束改走**公共约束库** `services/image_prompt.py`（用途 `USE_COVER`）
    —— 此前这里自己写「干净背景的商品封面图…竖版构图，突出卖点」，与详情图那份措辞
    不一致，且**没有防漂移约束**（同一条约束在 3 处各写一遍的典型后果）。
    """
    title = str(getattr(row, "item_title", "") or "").strip()
    parts = [f"为小红书笔记生成一张干净背景的商品封面图，主体：{title or '商品'}"]
    parts.append(image_prompt.requirement_inline(image_prompt.USE_COVER))
    return "；".join(parts)


def _assets_from_ai(
    db: Session,
    *,
    user: Any,
    row: ShopProduct,
    media_source: str,
    media_options: Optional[dict],
    record: Any,
    business_key: str = "",
) -> tuple[list[dict], list[AigcMediaAsset]]:
    """`ai_generate` / `reference_image`：调用图像模型 → 下载落盘 → 记溯源。

    ⚠️ **出图/下载失败 ⇒ 降级返回空媒体**（不废正文，§13.4.2）；只有**配置级**
    错误（业务点未配 / 类型不符）才抛 `MediaGenerationError`。
    """
    from backend.app.api.ai import _business_model_context
    from backend.app.services.ai_service import OpenAICompatibleImageClient
    from backend.app.services.business_provider_keys import (
        BUSINESS_KEY_LLM_NOTE_IMAGE,
        BUSINESS_KEY_WORKSHOP_IMAGE_GEN,
    )

    #: **按入口取模型**（用户口径 2026-10-18「各个功能单独配置」）。
    #: 调用方传它所属入口的 key；不传 ⇒ 笔记工坊·图片（本函数最初的唯一调用方）。
    image_key = business_key or BUSINESS_KEY_WORKSHOP_IMAGE_GEN
    _ = BUSINESS_KEY_LLM_NOTE_IMAGE  # 保留导入，供调用方按需引用

    options = media_options if isinstance(media_options, dict) else {}
    reference_urls = _str_list(options, "reference_image_urls")

    if media_source == MEDIA_SOURCE_REFERENCE_IMAGE:
        # 未给参考图 ⇒ 按台账首图自动取（同 §13.3.3 详情图口径：不让前端传 URL 是 SSRF 面）
        if not reference_urls:
            reference_urls = _product_image_urls(row)[:MAX_REFERENCE_IMAGES]
        if not reference_urls:
            raise MediaGenerationError(
                "这个商品还没有同步到图片，无法作为参考图生成。",
                code="PRODUCT_IMAGE_MISSING",
            )
        prompt = str(options.get("prompt") or "").strip() or _default_image_prompt(row)
        count = 1
    else:  # ai_generate
        prompt = str(options.get("prompt") or "").strip() or _default_image_prompt(row)
        try:
            count = int(options.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(count, MAX_AI_GENERATE_COUNT))

    # 配置级错误（未配图像业务点 / 类型不符）⇒ 冒泡，由 worker 决定失败该条。
    model_config, api_key = _business_model_context(db, user, image_key)

    client = OpenAICompatibleImageClient()
    assets: list[dict] = []
    last_error: Exception | None = None
    origins: list[AigcMediaAsset] = []
    reference_images = reference_urls[:MAX_REFERENCE_IMAGES] if reference_urls else None
    for index in range(count):
        try:
            result = client.generate_image(
                model_config=model_config,
                api_key=api_key,
                prompt=prompt,
                reference_images=reference_images,
            )
            remote_url = str(result.get("url") or "")
            local_path, local_url = _download_to_media(remote_url, user_id=int(user.id))
        except Exception as error:  # noqa: BLE001 - 出图/下载失败降级，不废正文
            if isinstance(error, MediaGenerationError):
                raise
            logger.warning(
                "media generation degraded (source=%s, product=%s): %s",
                media_source,
                getattr(row, "id", None),
                error,
            )
            #: ⚠️ **把真实原因带给上层**（2026-10-19 实测）：原先这里只 `continue`，
            #: 上层只能看到「产出 0 条」⇒ 记成「该来源没有产出任何媒体」，用户以为
            #: 是没配置/没选图。实际是上游出图 500（`master 上游生图错误（HTTP 500）`）
            #: —— 这种**外部故障必须说出来**，否则用户只能猜。
            #: 用 `last_generate_error` 挂在 assets 列表上：不改造函数签名（9 处调用），
            #: 也不影响「出图失败不废正文」的降级语义。
            last_error = error
            continue
        assets.append(
            {
                "asset_type": "image",
                "url": local_url,
                "local_path": local_path,
                "sort_order": index,
            }
        )
        origins.append(
            AigcMediaAsset(
                user_id=int(user.id),
                generation_record_id=(int(record.id) if record is not None else None),
                asset_type="image",
                local_path=local_path,
                url=remote_url or None,
                # `aigc_task_id` 本轮留空串：图片生成上游不回平台溯源号（§13.4.3）。
                aigc_task_id="",
                prompt=prompt,
                model_name=str(getattr(model_config, "model_name", "") or ""),
            )
        )
    #: **一张都没出但确实报过错** ⇒ 记下真原因（调用方据此给用户真原因，
    #: 而不是笼统的「该来源没有产出任何媒体」）。
    #: ⚠️ 内置 `list` **不能挂自定义属性**（`AttributeError`）⇒ 用模块级槽位，
    #: 由 `build_note_media` 在返回后立刻取走（同线程同步调用，无并发问题）。
    global _LAST_GENERATE_ERROR
    _LAST_GENERATE_ERROR = _error_text(last_error) if (not assets and last_error is not None) else ""
    return assets, origins


# --------------------------------------------------------------------------- #
# 视频来源（§13.4.4 v2.14）：异步链路（提交 → 轮询 → 下载）
# --------------------------------------------------------------------------- #
def _assets_from_video(
    db: Session,
    *,
    user: Any,
    row: ShopProduct,
    media_source: str,
    media_options: Optional[dict],
    record: Any,
    business_key: str = "",
) -> tuple[list[dict], list[AigcMediaAsset]]:
    """`video_ai_generate` / `video_reference`：调视频模型 → 下载落盘 → 记溯源。

    ⚠️ **同一纪律**：视频失败 ⇒ 降级返回空媒体（不废正文，§13.4.2）；
    只有**配置级**错误（业务点未配 / 类型不符）才抛 `MediaGenerationError`。
    """
    from backend.app.api.ai import _business_model_context
    from backend.app.services.business_provider_keys import BUSINESS_KEY_LLM_NOTE_VIDEO
    from backend.app.services.video_generation import (
        DEFAULT_VIDEO_SIZE,
        OpenAICompatibleVideoClient,
    )

    options = media_options if isinstance(media_options, dict) else {}
    # v2.26（§13.8）：**公共提示词模板优先**（与图片链路同一把尺子）：
    # 模板 > 手输 prompt > 内置兜底。选了模板 ⇒ 手输降级为「补充要求」。
    #
    # ⚠️ `int()` 转换与「渲染模板」**分开处理**：若把两者裹在同一个 try 里，
    # `MediaGenerationError`（模板不存在 / 越权 / 插槽错）会被误报成「参数无效」
    # —— 那是两个完全不同的自救动作（改参数 vs 去看模板）。
    prompt = ""
    raw_template_id = options.get("template_id")
    if raw_template_id:
        try:
            template_id = int(raw_template_id)
        except (TypeError, ValueError) as exc:
            raise MediaGenerationError(
                f"视频提示词模板参数无效：{raw_template_id!r}", code="PROMPT_TEMPLATE_INVALID"
            ) from exc
        prompt = _template_video_prompt(
            db,
            user,
            template_id=template_id,
            row=row,
            extra_prompt=str(options.get("prompt") or "").strip(),
        )
    if not prompt:
        prompt = str(options.get("prompt") or "").strip() or _default_video_prompt(row)

    # 时长夹到 [1, MAX_VIDEO_DURATION]；坏值退默认（不因为一个脏参数整条失败）。
    try:
        duration = int(options.get("duration") or DEFAULT_VIDEO_DURATION)
    except (TypeError, ValueError):
        duration = DEFAULT_VIDEO_DURATION
    duration = max(1, min(duration, MAX_VIDEO_DURATION))
    size = str(options.get("size") or "").strip() or DEFAULT_VIDEO_SIZE

    reference_image = ""
    if media_source in (MEDIA_SOURCE_VIDEO_REFERENCE, MEDIA_SOURCE_VIDEO_AI):
        #: 图生视频：参考图。
        #: ⚠️ 口径已扩（2026-10-16 用户）：*「智能生成视频需支持选择参考图，参考图可为
        #: 本地素材也可以是商品原图，最多一共4张」* ⇒ `video_ai_generate` 也读参考图
        #: （此前只有 `video_reference` 读 ⇒ 用户选了参考图却静默不用）。
        #: 参考图**由前端传 URL**（商品图是公开 CDN 直链，模型侧可直接抓）；
        #: 前端没传就回落台账首图（老行为保留，不因没选而失败）。
        reference_urls = _str_list(options, "reference_image_urls")[:MAX_REFERENCE_IMAGES]
        if not reference_urls:
            reference_urls = _product_image_urls(row)[:MAX_REFERENCE_IMAGES]
        #: ⚠️ **没有参考图不再报错**（与 `video_reference` 的区别）：`video_ai_generate`
        #: 是**文生视频**，不带图也能生成（那是它的主用法）；`video_reference` 的语义
        #: 就是「拿商品图生视频」⇒ 没图才是真错误。混成一条会把「文生视频」误拦。
        reference_image = reference_urls[0] if reference_urls else ""

    # 配置级错误（未配视频业务点 / 类型不符）⇒ 冒泡，由 worker 决定失败该条。
    #: **按入口取模型**（「各个功能单独配置」）：调用方传它所属入口的 key；
    #: 不传 ⇒ LLM 生成笔记的视频模型（视频目前只有这一个入口）。
    model_config, api_key = _business_model_context(
        db, user, business_key or BUSINESS_KEY_LLM_NOTE_VIDEO
    )

    client = OpenAICompatibleVideoClient()
    try:
        result = client.generate_video(
            model_config=model_config,
            api_key=api_key,
            prompt=prompt,
            reference_image=reference_image,
            duration=duration,
            size=size,
        )
        remote_url = str(result.get("url") or "")
        local_path, local_url = _download_media(remote_url, user_id=int(user.id), expected_kind="video")
    except Exception as error:  # noqa: BLE001 - 生成/下载失败降级，不废正文
        if isinstance(error, MediaGenerationError):
            raise
        logger.warning(
            "video generation degraded (source=%s, product=%s): %s",
            media_source,
            getattr(row, "id", None),
            error,
        )
        return [], []

    return (
        [{"asset_type": "video", "url": local_url, "local_path": local_path, "sort_order": 0}],
        [
            AigcMediaAsset(
                user_id=int(user.id),
                generation_record_id=(int(record.id) if record is not None else None),
                asset_type="video",
                local_path=local_path,
                url=remote_url,
                prompt=prompt,
                model_name=str(getattr(model_config, "model_name", "") or ""),
            )
        ],
    )


def _default_video_prompt(row: ShopProduct) -> str:
    """按商品信息拼默认视频提示词（用户没填 prompt 时用）。

    只用台账里**真实存在**的字段（同 `_default_image_prompt` 纪律：不编卖点）。

    ⚠️ v2.21：约束段走公共库的 `USE_VIDEO` 用途 —— 视频的构图口径（9:16 + 动态场景）
    与图片（3:4 + 静帧）**不同**，故用用途分档而不是照抄图片那份（这正是公共库
    设计成「按用途组合」而非「一个大字符串」的原因）。
    """
    title = str(getattr(row, "title", "") or "").strip()
    parts = ["电商商品展示短视频"]
    if title:
        parts.append(f"商品：{title}")
    parts.append(image_prompt.requirement_inline(image_prompt.USE_VIDEO))
    return "；".join(parts)


def _template_video_prompt(
    db: Session,
    user: Any,
    *,
    template_id: int,
    row: ShopProduct,
    extra_prompt: str = "",
) -> str:
    """用公共提示词模板渲染**视频**提示词（v2.26，设计 17 §13.8）。

    与图片链路同一条尺子（§13.6.3）：渲染交给 `prompt_renderer`（§5.1 唯一实现），
    用途档用 `USE_VIDEO`（竖版 9:16 + 动态场景 —— 与图片的 3:4 静帧不同）。

    ⚠️ **不静默回落**：模板不存在 / 不属于本用户 ⇒ 抛 `MediaGenerationError`
    （不降级成手输）—— 用户显式选了模板，「以为用了、其实没用」比报错更坏。
    ⚠️ **不做「模板 + 兜底约束」的去重**：视频的兜底是 `_default_video_prompt`（整段），
    而模板渲染自带模块；两者不叠加，模板就是模板（图片链路那边的判重是因为
    服务层会无条件追加，这里没有那个追加）。
    """
    from backend.app.models.prompt import PromptModule, PromptTemplate
    from backend.app.services import prompt_renderer

    template = db.scalar(
        select(PromptTemplate).where(
            PromptTemplate.id == template_id,
            PromptTemplate.user_id == getattr(user, "id", None),
        )
    )
    if template is None:
        raise MediaGenerationError(
            f"提示词模板 #{template_id} 不存在或不属于当前用户", code="PROMPT_TEMPLATE_NOT_FOUND"
        )

    module_ids = [int(mid) for mid in (template.module_ids or []) if isinstance(mid, int)]
    modules: list[Any] = []
    if module_ids:
        modules = list(
            db.scalars(
                select(PromptModule).where(
                    PromptModule.user_id == getattr(user, "id", None),
                    PromptModule.id.in_(module_ids),
                    PromptModule.enabled.is_(True),
                )
            )
        )

    # ⚠️ 商品映射**必须复用** `load_product_dict_from_ledger`（唯一实现）：
    # ORM 列名与槽位名**不同**（`item_title` vs `title`、`sales` vs `sales_total`），
    # 自己 `getattr(row, field)` 取会**一个都取不到**（静默少信息、不报错）——
    # 这正是该函数 docstring 里写的那类漂移（DEF-044）。
    from backend.app.services.shop_products_service import load_product_dict_from_ledger

    product = load_product_dict_from_ledger(row)

    try:
        body = prompt_renderer.render_prompt(
            skeleton=str(getattr(template, "prompt_skeleton", "") or ""),
            modules=modules,
            product=product,
        )
    except prompt_renderer.PromptRenderError as error:
        raise MediaGenerationError(error.message, code=error.code) from error

    parts = [body.strip()] if body.strip() else []
    # 视频构图约束（USE_VIDEO）—— 模板管风格，约束管成图硬要求（同 §13.6.2 分工）。
    # 判重走公共实现（§13.6.3 同一套归一化，唯一实现）
    missing = image_prompt.missing_requirement_lines(image_prompt.USE_VIDEO, body)
    if missing:
        parts.append("\n".join(("出图要求：", *missing)))
    if extra_prompt.strip():
        parts.append(f"【补充要求】{extra_prompt.strip()}")
    return "\n\n".join(part for part in parts if part.strip())


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build_note_media(
    db: Session,
    *,
    user: Any,
    row: ShopProduct,
    media_source: str,
    media_options: Optional[dict],
    record: Any,
    business_key: str = "",
) -> tuple[list[dict], list[AigcMediaAsset]]:
    """按 `media_source` 产出 `(note_assets 条目, aigc 溯源行)`（设计 17 §13.4.2）。

    ⚠️ 调用方持事务：本函数只 `db.add(...)`（溯源行），**不 commit**。

    ⚠️ **v2.26（M-C1）**：本函数是**单来源**实现（一段）。**组合**走
    `build_media_plan_assets()`（下方）—— 它按段循环调用本函数，
    **不重造出图逻辑**。调用方**不要**直接调本函数来表达组合。
    """
    source = str(media_source or MEDIA_SOURCE_PRODUCT_IMAGE)
    if source == MEDIA_SOURCE_PRODUCT_IMAGE:
        #: ⚠️ 必须把 `media_options` 传下去 —— `pick`（用户选了第几张）在里面，
        #: 不传 = 用户的选择被无视（本函数的 `pick` 分支就是为此存在）。
        return _assets_from_product_image(row, media_options), []
    if source == MEDIA_SOURCE_MATERIAL:
        return _assets_from_material(db, user_id=int(user.id), media_options=media_options), []
    if source in (MEDIA_SOURCE_AI_GENERATE, MEDIA_SOURCE_REFERENCE_IMAGE):
        _reset_last_generate_error()
        return _assets_from_ai(
            db,
            user=user,
            row=row,
            media_source=source,
            media_options=media_options,
            record=record,
            #: ⚠️ **必须传**（2026-10-18 实测）：漏传 ⇒ 走「智能生成」时函数内
            #: `business_key` 未定义 ⇒ `name 'business_key' is not defined`
            #: ⇒ AI 图一张都出不来（用户看到的是「图没生成」，而不是原因）。
            business_key=business_key,
        )
    # 视频来源（§13.4.4 v2.14）：与图片**同一入口**，按 media_options 的 `media_kind`
    # 分流到视频链路。⚠️ 不新开一个入口参数 —— 那会让「图片/视频」两条链路各拿一份
    # media_source 校验（DEF-044）。
    if source == MEDIA_SOURCE_VIDEO_MATERIAL:
        #: **本地素材直接用**（用户口径 2026-10-16：*「使用本地素材这无需再生成视频，
        #: 后端根据商品，以及提示词，本地素材参考生成相关的文案标题话题等」*）——
        #: 这一条**只取素材视频当媒体**，**不调视频模型**（零成本、秒级）；
        #: 文案仍走 LLM（借商品事实 + 提示词 + 该素材）。
        #: ⚠️ 与图片的 `material` 共用同一取数实现（`_assets_from_material`），
        #: 但**只取视频**（图片素材对这个来源无意义 ⇒ 取错类型会让「笔记形态=视频」落空）。
        return _assets_from_material_video(db, user_id=int(user.id), media_options=media_options), []
    if source in (MEDIA_SOURCE_VIDEO_AI, MEDIA_SOURCE_VIDEO_REFERENCE):
        return _assets_from_video(
            db,
            user=user,
            row=row,
            media_source=source,
            media_options=media_options,
            record=record,
            business_key=business_key,
        )
    # 未知来源：如实返回空（不造图）；门禁应在入队前拦下。
    logger.warning("unknown media_source=%s, no media produced", source)
    return [], []


# --------------------------------------------------------------------------- #
# 组合入口（v2.26，M-C1，设计 41 §11.2 / §14）
# --------------------------------------------------------------------------- #
def build_media_plan_assets(
    db: Session,
    *,
    user: Any,
    row: ShopProduct,
    media_plan: list[dict],
    media_options: Optional[dict] = None,
    record: Any = None,
    business_key: str = "",
) -> tuple[list[dict], list[AigcMediaAsset], list[dict]]:
    """**组合**媒体计划 → `(note_assets 条目, aigc 溯源行, 逐段结果)`。

    ## 为什么不重造出图逻辑

    每一段都调 `build_note_media()`（单来源实现）—— 组合层只做三件事：
    ① **按顺序拆段**（顺序即图序，封面在第 1 段）；② **汇总张数**（R2 总闸在
    `media_plan.validate_plan`，**这里不重判**）；③ **逐段隔离失败**（R1/R4 的落地）。

    ## 失败语义（设计 41 §11.2.4 的失败点 9–13）

    | 情形 | 行为 |
    |---|---|
    | 某**内页**段失败，其他成功 | **半成功可交付**：成功的落库，失败的如实记进 `segments` |
    | **封面**段失败 | ⚠️ `cover_ok=False` —— **由调用方整篇判失败**（本函数不自己抛，因为「整篇失败」要连笔记一起回滚，那是 worker 的事务边界） |
    | 视频段超时 | **不阻塞**图文（视频是异步的 ⇒ 它本来就在后面等） |

    ## 返回值第三项 `segments`

    逐段结果，**用户要能看到「哪个来源失败 + 为什么」**（不是笼统「生成失败」）：

    ```
    [{"index": 0, "role": "cover", "source": "ai_generate", "ok": True,  "count": 1},
     {"index": 1, "role": "content", "source": "material",    "ok": False, "count": 2,
      "error": "素材文件不存在：…"}]
    ```
    """
    assets: list[dict] = []
    aigc_rows: list[AigcMediaAsset] = []
    segments: list[dict] = []
    cover_ok = False

    for index, seg in enumerate(media_plan or []):
        source = str(seg.get("source") or "")
        role = str(seg.get("role") or "content")
        # 段级参数：把 role/count 之外的键当 media_options 传下去
        # （⚠️ 合并调用方的全局 media_options —— 段内键优先，因为段更具体）。
        seg_options = {k: v for k, v in seg.items() if k not in ("source", "role")}
        merged = {**(media_options or {}), **seg_options}
        entry: dict = {"index": index, "role": role, "source": source, "ok": False}
        try:
            seg_assets, seg_rows = build_note_media(
                db,
                user=user,
                row=row,
                media_source=source,
                media_options=merged or None,
                record=record,
                business_key=business_key,
            )
        except Exception as error:  # noqa: BLE001 — 逐段隔离（R1：一段坏不连坐其他段）
            entry["error"] = _error_text(error)
            segments.append(entry)
            logger.warning("media plan segment %s(%s) failed: %s", index, source, error)
            continue
        if not seg_assets:
            #: 来源合法但产出 0 条。⚠️ **区分两种 0 条**（2026-10-19 实测）：
            #:   · 真的没东西（素材库勾了空集合）⇒「没有产出任何媒体」
            #:   · **调用过但失败了**（上游 500 / 超时 / Key 失效）⇒ 必须带出真实原因，
            #:     否则用户看到「没产出」会去反复改配置，而问题在上游。
            upstream = _take_last_generate_error()
            entry["error"] = (
                f"生成失败：{upstream}" if upstream else "该来源没有产出任何媒体"
            )
            segments.append(entry)
            continue
        entry["ok"] = True
        entry["count"] = len(seg_assets)
        #: **给每张图打上「来自哪一段、哪个来源」**（用户口径 2026-10-19：
        #: *「应记录图片内页来源，回显来源，并在来源图上回显勾选」*）——
        #: 只存图不存来源，回显时就只能猜（实测 `media_json` 里
        #: `role`/`source` 全空 ⇒ 补出来的内页段不知道来源，
        #: 缩略图下面的标签也只能一律写「封面」）。
        for asset in seg_assets:
            asset["source"] = source
            asset["role"] = role
            asset["slot_index"] = index
        assets.extend(seg_assets)
        aigc_rows.extend(seg_rows)
        if role == "cover":
            cover_ok = True
        segments.append(entry)

    return assets, aigc_rows, segments


def _reset_last_generate_error() -> None:
    """清空上一段的失败记录（**每段开始前清**，否则上一段的错误会挂到下一段）。"""
    global _LAST_GENERATE_ERROR
    _LAST_GENERATE_ERROR = ""


def _take_last_generate_error() -> str:
    """取走本段的失败原因（取完即清，避免串段）。"""
    global _LAST_GENERATE_ERROR
    value = _LAST_GENERATE_ERROR
    _LAST_GENERATE_ERROR = ""
    return value


def _error_text(error: Exception) -> str:
    """异常 → 可读原因（优先取业务 `message`，其次 `str`）。"""
    message = getattr(error, "message", None)
    return str(message or error)
