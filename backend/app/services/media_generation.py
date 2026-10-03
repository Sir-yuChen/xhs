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
from backend.app.models.generation import (
    AigcMediaAsset,
    MEDIA_SOURCE_AI_GENERATE,
    MEDIA_SOURCE_MATERIAL,
    MEDIA_SOURCE_PRODUCT_IMAGE,
    MEDIA_SOURCE_REFERENCE_IMAGE,
    MEDIA_SOURCE_VIDEO_AI,
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
#: 参考图最多送几张（商品图集动辄 10+ 张，全送会超服务商上限，同 §13.3.3 口径）。
MAX_REFERENCE_IMAGES = 1
#: 视频时长上限（秒）与默认值（§13.4.4）。
MAX_VIDEO_DURATION = 10
DEFAULT_VIDEO_DURATION = 5


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
        ids = options.get("material_file_ids")
        if not isinstance(ids, list) or not ids:
            raise MediaGenerationError(
                "媒体来源为素材库时必须选择至少一张素材图。",
                code="LLM_MEDIA_OPTIONS_INVALID",
            )


def _material_file_ids(media_options: Optional[dict]) -> list[int]:
    options = media_options if isinstance(media_options, dict) else {}
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
    import json

    try:
        urls = json.loads(row.images_json or "[]")
    except ValueError:
        return []
    return [str(url) for url in urls if isinstance(url, str) and url.strip()]


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
def _assets_from_product_image(row: ShopProduct) -> list[dict]:
    urls = _product_image_urls(row)[:6]
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


def _default_image_prompt(row: ShopProduct) -> str:
    """`ai_generate` 未给 prompt 时，按商品拼一个通用出图提示词（不造假、可追溯）。"""
    title = str(getattr(row, "item_title", "") or "").strip()
    return f"为小红书笔记生成一张干净背景的商品封面图，主体：{title or '商品'}，竖版构图，突出卖点"


def _assets_from_ai(
    db: Session,
    *,
    user: Any,
    row: ShopProduct,
    media_source: str,
    media_options: Optional[dict],
    record: Any,
) -> tuple[list[dict], list[AigcMediaAsset]]:
    """`ai_generate` / `reference_image`：调用图像模型 → 下载落盘 → 记溯源。

    ⚠️ **出图/下载失败 ⇒ 降级返回空媒体**（不废正文，§13.4.2）；只有**配置级**
    错误（业务点未配 / 类型不符）才抛 `MediaGenerationError`。
    """
    from backend.app.api.ai import _business_model_context
    from backend.app.services.ai_service import OpenAICompatibleImageClient
    from backend.app.services.business_provider_keys import BUSINESS_KEY_GENERATE_IMAGE

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
    model_config, api_key = _business_model_context(db, user, BUSINESS_KEY_GENERATE_IMAGE)

    client = OpenAICompatibleImageClient()
    assets: list[dict] = []
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
) -> tuple[list[dict], list[AigcMediaAsset]]:
    """`video_ai_generate` / `video_reference`：调视频模型 → 下载落盘 → 记溯源。

    ⚠️ **同一纪律**：视频失败 ⇒ 降级返回空媒体（不废正文，§13.4.2）；
    只有**配置级**错误（业务点未配 / 类型不符）才抛 `MediaGenerationError`。
    """
    from backend.app.api.ai import _business_model_context
    from backend.app.services.business_provider_keys import BUSINESS_KEY_GENERATE_VIDEO
    from backend.app.services.video_generation import (
        DEFAULT_VIDEO_SIZE,
        OpenAICompatibleVideoClient,
    )

    options = media_options if isinstance(media_options, dict) else {}
    prompt = str(options.get("prompt") or "").strip() or _default_video_prompt(row)

    # 时长夹到 [1, MAX_VIDEO_DURATION]；坏值退默认（不因为一个脏参数整条失败）。
    try:
        duration = int(options.get("duration") or DEFAULT_VIDEO_DURATION)
    except (TypeError, ValueError):
        duration = DEFAULT_VIDEO_DURATION
    duration = max(1, min(duration, MAX_VIDEO_DURATION))
    size = str(options.get("size") or "").strip() or DEFAULT_VIDEO_SIZE

    reference_image = ""
    if media_source == MEDIA_SOURCE_VIDEO_REFERENCE:
        # 图生视频：参考图 = 台账首图（同 §13.4.2 口径，不让前端传 URL）。
        reference_urls = _str_list(options, "reference_image_urls")
        if not reference_urls:
            reference_urls = _product_image_urls(row)[:MAX_REFERENCE_IMAGES]
        if not reference_urls:
            raise MediaGenerationError(
                "这个商品还没有同步到图片，无法作为参考图生成视频。",
                code="PRODUCT_IMAGE_MISSING",
            )
        reference_image = reference_urls[0]

    # 配置级错误（未配视频业务点 / 类型不符）⇒ 冒泡，由 worker 决定失败该条。
    model_config, api_key = _business_model_context(db, user, BUSINESS_KEY_GENERATE_VIDEO)

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
    """
    title = str(getattr(row, "title", "") or "").strip()
    if title:
        return f"电商商品展示短视频，商品：{title}；竖版构图，突出商品主体与使用场景"
    return "电商商品展示短视频；竖版构图，突出商品主体与使用场景"


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
) -> tuple[list[dict], list[AigcMediaAsset]]:
    """按 `media_source` 产出 `(note_assets 条目, aigc 溯源行)`（设计 17 §13.4.2）。

    ⚠️ 调用方持事务：本函数只 `db.add(...)`（溯源行），**不 commit**。
    """
    source = str(media_source or MEDIA_SOURCE_PRODUCT_IMAGE)
    if source == MEDIA_SOURCE_PRODUCT_IMAGE:
        return _assets_from_product_image(row), []
    if source == MEDIA_SOURCE_MATERIAL:
        return _assets_from_material(db, user_id=int(user.id), media_options=media_options), []
    if source in (MEDIA_SOURCE_AI_GENERATE, MEDIA_SOURCE_REFERENCE_IMAGE):
        return _assets_from_ai(
            db,
            user=user,
            row=row,
            media_source=source,
            media_options=media_options,
            record=record,
        )
    # 视频来源（§13.4.4 v2.14）：与图片**同一入口**，按 media_options 的 `media_kind`
    # 分流到视频链路。⚠️ 不新开一个入口参数 —— 那会让「图片/视频」两条链路各拿一份
    # media_source 校验（DEF-044）。
    if source in (MEDIA_SOURCE_VIDEO_AI, MEDIA_SOURCE_VIDEO_REFERENCE):
        return _assets_from_video(
            db,
            user=user,
            row=row,
            media_source=source,
            media_options=media_options,
            record=record,
        )
    # 未知来源：如实返回空（不造图）；门禁应在入队前拦下。
    logger.warning("unknown media_source=%s, no media produced", source)
    return [], []
