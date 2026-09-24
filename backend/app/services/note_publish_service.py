"""待发布笔记的发布链路（设计 13 §31，2026-09-23 用户口径）。

## 用户口径原文（需求④）

> 「点击发布二次确认，然后再将笔记存储的媒体资源自动下载到本地临时文件夹，
> 每个笔记一个文件夹，发布笔记时重新上传，笔记发布成功后删除本地临时下载的
> 资源，并根据发布成功的笔记 ID，将三方真实的笔记 ID 更新到笔记表和笔记与
> 商品关系表中。如果笔记发布失败则记录失败原因……发布失败的笔记支持重复发布。」

## 流程（单篇；批量 = 逐篇走同一函数）

```
门禁（归属 / 类型 / 状态 / 媒体 / ark 令牌）
  → publish_status = publishing（先落库，防「发布中崩溃」被误判可重发）
  → 下载媒体到 data/tmp_note_publish/<note_id>/（一个笔记一个文件夹）
  → 上传：upload/web/permit 一次 → 逐文件 PUT ROS
  → note/publish（hash_tag 用平台话题 ID、images_list 用上传 file_id、
    biz_relations 挂商品 item_id）
  → 成功：note_id 回写平台 ID + published + published_at + 删临时目录
  → 失败：publish_status=failed + publish_error=平台原文 + **保留**临时目录
```

## 三条硬约定

1. **临时目录生命周期**：成功即删（用户口径）；失败保留（重发不必重下、现场可查）。
2. **失败原因是平台原文**（DEF-058：不替换成「请稍后重试」）；
   网络异常带异常类型。
3. **发布账号 = 生成时记录的账号**（笔记行 `platform_account_id` 自带）——
   不提供换账号入口（换账号 = 换店铺上下文，挂链会挂错）。
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.models.note import (
    PUBLISH_STATUS_FAILED,
    PUBLISH_STATUS_PUBLISHED,
    PUBLISH_STATUS_PUBLISHING,
    PUBLISHABLE_STATUSES,
    NOTE_TYPE_LOCAL_GENERATED,
    Note,
    set_note_publish_status,
)
from backend.app.models.shop import NoteGoods
from backend.app.services.shop_binding_service import qianfan_cookies_for_account

logger = logging.getLogger(__name__)

#: 临时下载根目录（项目数据目录内，不动系统临时目录）。
TMP_SUBDIR = "tmp_note_publish"

#: 单个媒体下载超时（秒）。
_DOWNLOAD_TIMEOUT = 30

#: 单篇笔记的媒体上限（张）。设计 13 §32.2.1（用户 2026-09-23 口径：图片最多 6 张）——
#: 与生成侧的 `product_note_gen_service.MAX_NOTE_IMAGES` 同值：生成时落几张，
#: 发布时就传几张，**上限只在一处定义会漂**，所以两侧常量必须一起看。
MAX_NOTE_IMAGES = 6

#: 视频上限（个）。视频笔记发布链路未接通（§32.7）⇒ 本值当前只用于**拒绝时的说明**，
#: 不参与上传（`_download_media` 遇到「只有视频」的笔记直接给可读失败）。
MAX_NOTE_VIDEOS = 1


class NotePublishError(Exception):
    """发布失败（门禁 / 下载 / 上传 / 平台）。`code` 给前端做结构化呈现。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _temp_root() -> Path:
    root = Path(get_settings().storage_dir) / TMP_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _note_temp_dir(note_id: str) -> Path:
    return _temp_root() / note_id


def _assets_of(db: Session, note_pk: int) -> list:
    from backend.app.models.note import NoteAsset

    return list(
        db.scalars(
            select(NoteAsset).where(NoteAsset.note_id == note_pk).order_by(NoteAsset.sort_order, NoteAsset.id)
        ).all()
    )


def _owned_note(db: Session, user_id: int, note_pk: int) -> Note:
    note = db.get(Note, note_pk)
    if note is None or note.user_id != user_id:
        raise NotePublishError("NOTE_NOT_FOUND", "笔记不存在或不属于当前用户。")
    return note


def _check_publishable(note: Note) -> None:
    if note.note_type != NOTE_TYPE_LOCAL_GENERATED:
        raise NotePublishError(
            "NOTE_NOT_PUBLISHABLE", "只有「本地生成」的笔记可以发布（采集 / 同步笔记不走本链路）。"
        )
    if note.publish_status == PUBLISH_STATUS_PUBLISHED:
        raise NotePublishError("NOTE_ALREADY_PUBLISHED", "这篇笔记已经发布过了，不能重复发布。")
    if note.publish_status not in PUBLISHABLE_STATUSES:
        raise NotePublishError(
            "NOTE_NOT_PUBLISHABLE", f"笔记当前状态（{note.publish_status or '未知'}）不可发布。"
        )


def _download_media(db: Session, note: Note) -> list[dict]:
    """下载媒体到临时文件夹（一个笔记一个文件夹），返回本地文件信息。

    媒体取舍（设计 13 §32.2.1 / §32.7）：

    * **图片**（当前唯一被平台接受的形态）：最多 `MAX_NOTE_IMAGES` 张；
    * **视频**：生成侧「视频优先」会产出「只有视频」的笔记，但视频笔记发布链路
      未接通 ⇒ 这里**明确拒绝**并给可读原因（`NOTE_VIDEO_UNSUPPORTED`），
      不发半成品、不静默失败。

    失败即抛 :class:`NotePublishError`——调用方记 `publish_error` 并保留已下文件。
    """
    assets = _assets_of(db, note.id)
    if not assets:
        raise NotePublishError(
            "NOTE_MEDIA_EMPTY", "这篇笔记没有媒体素材（商品图为空），平台不接受发布。请换有图的商品重新生成。"
        )
    images = [asset for asset in assets if asset.asset_type != "video"]
    videos = [asset for asset in assets if asset.asset_type == "video"]
    if not images and videos:
        raise NotePublishError(
            "NOTE_VIDEO_UNSUPPORTED",
            "这篇笔记只有视频素材：视频笔记发布链路尚未接通（设计 13 §32.7），请换有图片的商品重新生成。",
        )
    if len(images) > MAX_NOTE_IMAGES:
        images = images[:MAX_NOTE_IMAGES]
    temp_dir = _note_temp_dir(note.note_id)
    temp_dir.mkdir(parents=True, exist_ok=True)

    downloaded: list[dict] = []
    for index, asset in enumerate(images):
        if not asset.url:
            raise NotePublishError("NOTE_MEDIA_EMPTY", f"第 {index + 1} 个媒体素材没有链接，无法下载。")
        ext = _extension_for(asset.url)
        target = temp_dir / f"{index + 1:03d}{ext}"
        try:
            response = requests.get(asset.url, timeout=_DOWNLOAD_TIMEOUT, headers={"Referer": ""})
            response.raise_for_status()
            content = response.content
        except requests.RequestException as error:
            raise NotePublishError(
                "PUBLISH_UPSTREAM_ERROR", f"下载媒体失败（第 {index + 1} 个）：{type(error).__name__}: {error}"
            )
        if len(content) < 100:
            raise NotePublishError("PUBLISH_UPSTREAM_ERROR", f"下载媒体失败（第 {index + 1} 个）：内容过小或为空。")
        target.write_bytes(content)
        # ⚠️ **不回写 `asset.local_path`**：临时路径随目录删除而失效，落库只会留下
        # 一批指向不存在文件的路径（第二份真相源）。`downloads` 列表在本次调用内自足。
        downloaded.append({"asset": asset, "path": target})
    return downloaded


def _extension_for(url: str) -> str:
    lowered = url.lower().split("?")[0]
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov"):
        if lowered.endswith(ext):
            return ext
    return ".jpg"


def _upload_media(adapter, downloaded: list[dict]) -> list[dict]:
    """上传全部媒体（一次 permit + 逐文件 PUT），返回 `images_list` 条目。

    ⚠️ permit 结构（2026-09-23 实测）：响应有**两个条目**（storageType 5 / 1，
    不同上传域），每个条目的 `fileIds[]` 是**一文件一个 ID**（不是一条目一文件）。
    取第一个条目，用它的 `uploadAddr` + `token` 把所有文件传完。
    """
    ok, message, permit_raw = adapter.upload_web_permit(len(downloaded))
    if not ok or not isinstance(permit_raw, dict):
        raise NotePublishError("PUBLISH_UPSTREAM_ERROR", message or "申请上传凭证失败")
    permits = (permit_raw.get("data") or {}).get("uploadTempPermits") or []
    if not permits:
        raise NotePublishError("PUBLISH_UPSTREAM_ERROR", "平台没有返回上传凭证。")
    permit = permits[0]
    file_ids = permit.get("fileIds") or []
    if len(file_ids) < len(downloaded):
        raise NotePublishError(
            "PUBLISH_UPSTREAM_ERROR",
            f"上传凭证数量不足（需要 {len(downloaded)} 个，只返了 {len(file_ids)} 个）。",
        )
    upload_addr = str(permit.get("uploadAddr") or "")
    token = str(permit.get("token") or "")

    images: list[dict] = []
    for index, item in enumerate(downloaded):
        file_id = str(file_ids[index])
        # 单文件重试一次：ROS 偶发 SSL EOF / 连接重置（2026-09-23 实测第 6 张踩到），
        # 整篇重来代价太大，单文件重试足够。
        last_message = ""
        uploaded = False
        for attempt in range(2):
            ok, message, _ = adapter.upload_file(
                upload_addr=upload_addr,
                file_id=file_id,
                token=token,
                file_path=str(item["path"]),
            )
            if ok:
                uploaded = True
                break
            last_message = message
        if not uploaded:
            raise NotePublishError("PUBLISH_UPSTREAM_ERROR", f"上传第 {index + 1} 个媒体失败：{last_message}")
        # `url` 带上（不进请求体）：`_build_publish_body` 要用它按顺序对齐商品图宽高。
        images.append({"file_id": file_id, "url": str(item["asset"].url or ""), "width": 0, "height": 0})
    return images


def _parse_raw(note: Note) -> dict:
    """`notes.raw_json` → dict。**JSON 列可能是 dict 也可能是历史遗留的字符串**
    （两种都吃过），统一在这里归一，别处不再各写一份。"""
    raw = note.raw_json
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _build_publish_body(db: Session, note: Note, images: list[dict]) -> dict:
    """组装 `note/publish` 请求体（设计 13 §31.5 步 5）。

    ⚠️ **结构照抄 2026-09-23 bundle 实抓**（`index.605d6c8b.js` 发布函数）**且键名
    全部转下划线**（bundle 的 camelCase 由页面 `transform` 层转换；直连平台只认
    下划线 —— camelCase 会被 -9150「技术升级中」打回，实测）：

    ```json
    {"common": {"type": "normal", "title": "…", "desc": "…",
                "hash_tag": [{"id","name","type":"topic"}], "ats": [],
                "goods_info": {"comment": "",
                               "extension": {"is_adjust_ai": "0",
                                             "check_content_id": "<sku_id>",
                                             "goods_note_publish_type": "single_ai_publish",
                                             "publish_source": "qian_fang_agi",
                                             "feature": "{\"sellerScene\":\"ARK_PC_NOTE_AIGC\",\"aigcTaskId\":\"…\"}"}},
                "biz_relations": [{"biz_type": "GOODS_SELLER_V2", "biz_id": "<sku_id>",
                                   "extra_info": "{\"goods_id\":…,\"goods_name\":…,…}"}]},
     "image_info": {"images": [{"file_id": "…", "width": 0, "height": 0, "metadata": {"source": 0}}]}}
    ```

    三个要点：① **挂链粒度是 SKU**（`biz_id` / `check_content_id` 都是 `sku_id`，
    不是 item_id）；② `feature` 带 **AI 溯源**（`aigcTaskId`）—— 平台据此认这是
    「千帆 AGI 生成的商笔」；③ `hash_tag` 的 id 必须是平台话题 ID（AI 生成结果自带）。
    """
    raw = _parse_raw(note)
    aigc = raw.get("aigc") or {}
    aigc_topics = [
        {"id": str(t.get("id") or ""), "name": str(t.get("name") or "")}
        for t in aigc.get("topics", [])
        if isinstance(t, dict) and t.get("name") and t.get("id")
    ]
    sku_id = str(aigc.get("sku_id") or "")
    sku_name = str(aigc.get("sku_name") or note.title or "")
    task_id = str(aigc.get("task_id") or "")

    # 宽高从商品 raw_json 回填（平台 image_info 要宽高；缺失传 0，不造假）。
    relations = list(db.scalars(select(NoteGoods).where(NoteGoods.note_id == note.id)).all())
    product = None
    if relations:
        from backend.app.models.shop_product import ShopProduct

        product = db.get(ShopProduct, relations[0].product_id)
    dims = (
        _image_dims_for(product, [str(image.get("url") or "") for image in images])
        if product is not None
        else [{}] * len(images)
    )
    image_entries = []
    for index, image in enumerate(images):
        dim = dims[index] if index < len(dims) else {}
        image_entries.append(
            {
                "file_id": image["file_id"],
                "width": int(dim.get("width") or 0),
                "height": int(dim.get("height") or 0),
                "metadata": {"source": 0},
            }
        )

    common: dict = {
        "type": "normal",
        "title": note.title or "",
        "desc": note.content or "",
        "hash_tag": [{"id": t["id"], "name": t["name"], "type": "topic"} for t in aigc_topics],
        "ats": [],
    }
    if sku_id:
        common["goods_info"] = {
            "comment": "",
            "extension": {
                "is_adjust_ai": "0",
                "check_content_id": sku_id,
                "goods_note_publish_type": "single_ai_publish",
                "publish_source": "qian_fang_agi",
                "feature": json.dumps(
                    {
                        "sellerScene": "ARK_PC_NOTE_AIGC",
                        "aigcTaskId": task_id,
                        "aigcImageTaskId": "",
                    },
                    ensure_ascii=False,
                ),
            },
        }
        common["biz_relations"] = [
            {
                "biz_type": "GOODS_SELLER_V2",
                "biz_id": sku_id,
                "extra_info": json.dumps(
                    {
                        "goods_id": sku_id,
                        "goods_name": sku_name,
                        "goods_type": "goods_seller",
                        "tab_id": 1,
                        "image_type": "spec",
                        "left_bottom_type": "BUY_GOODS",
                    },
                    ensure_ascii=False,
                ),
            }
        ]

    return {"common": common, "image_info": {"images": image_entries}}


def _image_dims_for(product, image_urls: list[str]) -> list[dict]:
    """按**上传顺序**取商品图宽高（`raw_json.images[]` 的 link 与资产 URL 对齐）。

    入参是 URL 列表而不是「资产行 + 数量」：图片可能被 `MAX_NOTE_IMAGES` 截断、
    也可能与视频资产在同一篇里，按下标取资产行会错位（第 N 张配到别人的宽高）。
    """
    try:
        raw_images = json.loads(product.raw_json or "{}").get("images")
    except ValueError:
        raw_images = None
    dims_by_url: dict[str, dict] = {}
    if isinstance(raw_images, list):
        for entry in raw_images:
            if isinstance(entry, dict) and entry.get("link"):
                dims_by_url[str(entry["link"])] = {
                    "width": entry.get("width") or 0,
                    "height": entry.get("height") or 0,
                }
    return [dims_by_url.get(url, {}) for url in image_urls]


def _cleanup_temp(note: Note) -> None:
    shutil.rmtree(_note_temp_dir(note.note_id), ignore_errors=True)


def _clear_temp_paths(db: Session, note: Note) -> None:
    """清掉历史版本写在资产行上的临时路径（`local_path`）。

    当前实现**不再回写** `local_path`（本模块 `_download_media` 的约定：临时路径
    随目录删除即失效，落库只会留下指向不存在文件的路径）。这个清理是给老数据
    收尾的 —— 留着它，「这篇笔记有没有本地文件」就有两个答案。
    """
    for asset in _assets_of(db, note.id):
        if asset.local_path:
            asset.local_path = ""


def _preaudit(adapter, note: Note, raw: dict, images: list[dict], topic_names: list[str]) -> None:
    """发布前预审核（平台侧 moderation；设计 13 §31.1.2 链路补步）。

    页面自身流程：**先 preaudit、后 publish**。任一 `check_pass=false` ⇒
    抛 :class:`NotePublishError` 带平台 `fail_msg` 原文（比本地护栏更权威的
    内容 moderation，用户看到的就是平台原话）。

    ⚠️ 响应键名是**下划线风格**（实测响应：`audit_result` / `check_pass` /
    `fail_msg` / `all_pass`）—— 与请求侧一致，别按 bundle 的 camelCase 读。
    """
    ok, message, result = adapter.preaudit_note(
        title=note.title or "",
        content=note.content or "",
        topic_names=topic_names,
        image_list=images,
        text_gen_task_id=str((raw.get("aigc") or {}).get("task_id") or ""),
    )
    if not ok or not isinstance(result, dict):
        raise NotePublishError("PUBLISH_UPSTREAM_ERROR", message or "平台预审核失败")
    audit_result = (result.get("data") or {}).get("audit_result") or {}
    if not audit_result:
        return
    title_result = audit_result.get("title_result") or {}
    content_result = audit_result.get("content_result") or {}
    topic_result = audit_result.get("topic_result") or {}
    image_results = audit_result.get("image_results") or []
    image_failed = [r for r in image_results if isinstance(r, dict) and r.get("check_pass") is False]
    if (
        title_result.get("check_pass") is False
        or content_result.get("check_pass") is False
        or topic_result.get("check_pass") is False
        or image_failed
    ):
        reason = (
            title_result.get("fail_msg")
            or content_result.get("fail_msg")
            or topic_result.get("fail_msg")
            or (image_failed[0].get("fail_msg") if image_failed else "")
            or "笔记内容未通过平台预审核"
        )
        raise NotePublishError("PUBLISH_AUDIT_REJECTED", str(reason))


def publish_note(db: Session, user_id: int, note_pk: int, *, adapter_factory) -> dict:
    """发布一篇待发布笔记（设计 13 §31.5）。返回发布结果摘要。"""
    note = _owned_note(db, user_id, note_pk)
    _check_publishable(note)

    account_id = note.platform_account_id
    cookies = qianfan_cookies_for_account(db, account_id)
    if not cookies:
        raise NotePublishError(
            "QIANFAN_CREDENTIAL_MISSING", "该账号还没有千帆（ark）令牌，请先在浏览器登录千帆后导入。"
        )
    adapter = adapter_factory(cookies)

    # 状态先落库：发布中崩溃重启后不会被误判为可重发。状态与 `published` 投影
    # 一次写入（唯一入口，§32.3.2）。
    set_note_publish_status(note, PUBLISH_STATUS_PUBLISHING)
    note.publish_attempts = (note.publish_attempts or 0) + 1
    db.commit()

    try:
        downloaded = _download_media(db, note)
        images = _upload_media(adapter, downloaded)
        body = _build_publish_body(db, note, images)
        raw = _parse_raw(note)
        topic_names = [
            str(t.get("name") or "")
            for t in (raw.get("aigc") or {}).get("topics", [])
            if isinstance(t, dict) and t.get("name")
        ]
        _preaudit(adapter, note, raw, images, topic_names)
        ok, message, result = adapter.publish_note(body)
        if not ok or not isinstance(result, dict):
            raise NotePublishError("PUBLISH_UPSTREAM_ERROR", message or "平台发布失败（未给原因）")
        # 平台 ID 在 `data.id`（实测响应：`{"success":true,"data":{"id":"6ab2…","note_id":"…"}}`）。
        data = result.get("data") or {}
        platform_note_id = str(data.get("id") or data.get("note_id") or "")
        if not platform_note_id:
            raise NotePublishError("PUBLISH_UPSTREAM_ERROR", "平台发布成功但没有返回笔记 ID。")
    except NotePublishError as error:
        set_note_publish_status(note, PUBLISH_STATUS_FAILED, error=error.message)
        _clear_temp_paths(db, note)
        db.commit()
        logger.info("note publish failed (note_pk=%s): %s / %s", note_pk, error.code, error.message)
        return {"ok": False, "note_pk": note.id, "note_id": note.note_id, "error": error.message, "code": error.code}
    except Exception as error:  # noqa: BLE001 - 未预期异常也要变成可读失败，不 500
        set_note_publish_status(note, PUBLISH_STATUS_FAILED, error=f"{type(error).__name__}: {error}")
        db.commit()
        logger.warning("note publish unexpected error (note_pk=%s): %s", note_pk, error)
        return {"ok": False, "note_pk": note.id, "note_id": note.note_id, "error": note.publish_error, "code": "PUBLISH_FAILED"}

    # ---- 成功回写（一个事务）----
    note.note_id = platform_note_id
    set_note_publish_status(note, PUBLISH_STATUS_PUBLISHED)
    _clear_temp_paths(db, note)
    db.commit()
    _cleanup_temp(note)
    logger.info("note published (note_pk=%s -> %s)", note_pk, platform_note_id)
    return {
        "ok": True,
        "note_pk": note.id,
        "note_id": note.note_id,
        "platform_note_id": platform_note_id,
        "published_at": note.published_at.strftime("%Y-%m-%d %H:%M:%S") if note.published_at else "",
    }


def batch_publish_notes(
    db: Session,
    user_id: int,
    note_pks: list[int],
    *,
    adapter_factory,
    max_items: int = 20,
) -> dict:
    """批量发布（顺序处理，单项失败不阻断；设计 13 §31.5 步 8）。"""
    cleaned = [int(pk) for pk in note_pks if str(pk).strip()]
    distinct = list(dict.fromkeys(cleaned))[:max_items]
    results: list[dict] = []
    for note_pk in distinct:
        try:
            outcome = publish_note(db, user_id, note_pk, adapter_factory=adapter_factory)
            results.append(outcome)
        except NotePublishError as error:
            results.append({"ok": False, "note_pk": note_pk, "error": error.message, "code": error.code})
    succeeded = sum(1 for r in results if r.get("ok"))
    return {"total": len(distinct), "succeeded": succeeded, "failed": len(distinct) - succeeded, "results": results}
