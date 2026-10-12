"""商品「新增 → 草稿箱」的**执行器**（设计 23 §4.2）。

## 一条不可跨越的红线，写进代码而不是注释里

用户口径（2026-10-07）：

> 注意红线：**新增的商品只能保存到草稿箱内，不允许直接发布到线上**。

落实方式有三层，**任何一层单独都不够**：

1. **执行链里根本没有「提交商品」这一步** —— 本模块最后调的是
   `create_product_draft`（存档），平台侧「提交上架」走的是另外两个端点
   （`publish_async` + `publish_async_operate`），本项目**不接入**；
2. **请求契约里没有那个开关**（`ItemDraftCreateRequest` 的全部字段都不含
   `publish` / `available` / `on_shelf`），`extra="forbid"` ⇒ 传了直接 422；
3. **回执文案固定写明「已存入草稿箱，未上架」** —— 用户看到的每一处都一致。

## 形态：同步执行（不是后台任务）

与「批量上架 / 下架」（设计 20 §11.3，手动触发式后台任务）**刻意不同**：
新增商品的耗时是可控的（≤20 张图 + 1 次建档），而且用户**需要当场看到**
「哪张图传失败了 / 草稿 ID 是多少 / 哪些字段没落进去」。丢后台就得再设计一套
进度查询，而这条链路的产出本来就该当场呈现。

⚠️ **平台限流**：单店铺**每分钟最多 2 个草稿**（真机原文
`提示：单店铺每分钟限制发布2个草稿！`）。批量新增时按此排队，
**不硬闯**（写操作密集触发会撞风控）。
"""

from __future__ import annotations

import logging
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from backend.app.services import shop_draft_package as package
from backend.app.services import shop_item_publish as pub

logger = logging.getLogger(__name__)

#: 单次新增的图片 / SKU 上限（**前后端同值**：常量只在这里定义，前端读端点下发值）。
#: 图片上限 **100** —— 取自平台新增页原文「已选择 0 / 100 项」（真机抓包），
#: 不是我们自己拍的值；定小了会让「一个完整数据包」传不完（真机实测一个家具包
#: 就有 69 张：5 主图 + 38 详情图 + 26 SKU 图）。
#: SKU 200 与商品操作的单任务上限同口径（`MAX_OPERATE_ITEMS`）。
MAX_DRAFT_IMAGES: int = 100
MAX_DRAFT_SKUS: int = 200

#: 单张图片上传的**重试次数**（2026-10-07 真机实测：本机有系统代理
#: `127.0.0.1:8899`，大 body 的 ROS PUT 偶发 `ProxyError` / `SSLEOFError`）。
#: ⚠️ 这是**环境级抖动**不是逻辑错，重试即可；不重试的表现是
#: 「同样一个包，这次少两张图，下次又好了」—— 用户完全无法归因。
IMAGE_UPLOAD_RETRIES: int = 3
IMAGE_UPLOAD_RETRY_WAIT: float = 1.5

#: 图片空间**登记后回读**的重试（真机实测搜索最终一致：登记后立刻搜 0 命中，
#: 约 1–2 秒可见）。读回 `path`/`link` 是「平台新分配的 CDN id」的唯一来源。
_MATERIAL_READBACK_TRIES: int = 6
_MATERIAL_READBACK_WAIT: float = 1.0

#: 平台草稿限流（每分钟 N 个）。判据复用 SDK 的 `is_draft_rate_limited`
#: （唯一实现），这里只放「重试几次 / 等多久」。
DRAFT_LIMIT_PER_MINUTE: int = 2
_RATE_LIMIT_WAIT_SECONDS: int = 31
_MAX_RATE_LIMIT_RETRIES: int = 3


class DraftError(RuntimeError):
    """新增商品失败（带结构化 `code`，由端点映射成 HTTP）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _api_factory(cookies: str) -> Any:
    """延迟 import（SDK 层依赖 Node 签名运行时，测试要能打桩）。"""
    from apis.xhs_qianfan_apis import XHS_Qianfan_Apis

    return XHS_Qianfan_Apis(cookies)


def upload_one_image(
    api: Any, *, seller_id: str, file_path: str
) -> tuple[bool, str, dict[str, Any] | None]:
    """上传**一张**图片到图片空间 → (成功, 原因, 归一化条目)。

    三步写链路（与 `picture_space_upload` 端点**同一实现**，不重写一份）：
    permit → PUT → 登记。返回的条目字段与 `build_image_entry` 对得上。

    ⚠️ `biz_name=ark&scene=goods_image`（设计 22 真机结论）：用发布链路的
    `spectrum/image` 会把文件 PUT 到别的桶 ⇒ 登记后取图 404（裂图）。
    """
    from backend.app.services import picture_space_service as ps

    path = Path(file_path)
    if not path.is_file():
        return False, f"图片文件不存在：{file_path}", None
    size = path.stat().st_size
    if size > package.MAX_IMAGE_BYTES:
        return False, f"图片超过平台 5M 上限（{size // 1024 // 1024}M）", None

    ok, msg, permit_raw = api.upload_web_permit(
        1, biz_name="ark", scene="goods_image", referer="/app-system/pictureSpace"
    )
    if not ok or not isinstance(permit_raw, dict):
        return False, msg or "申请上传凭证失败", None
    permits = (permit_raw.get("data") or {}).get("uploadTempPermits") or []
    if not permits:
        return False, "平台没有返回上传凭证", None
    # 平台前端按 `qos` 降序取第一个（真机 bundle `sort((a,b)=>b.qos-a.qos)[0]`）。
    permit = sorted(permits, key=lambda x: x.get("qos") or 0, reverse=True)[0]
    file_ids = permit.get("fileIds") or []
    if not file_ids:
        return False, "上传凭证没有预分配文件 ID", None
    file_id = str(file_ids[0])
    upload_addr = str(permit.get("uploadAddr") or "")
    token = str(permit.get("token") or "")

    ok, msg, put_meta = api.upload_file(
        upload_addr=upload_addr, file_id=file_id, token=token, file_path=str(path)
    )
    if not ok:
        return False, msg or "上传文件失败", None
    preview_url = (put_meta or {}).get("preview_url", "") if isinstance(put_meta, dict) else ""

    # 尺寸读不到就**不带那两个键**（设计 22 的 `For input string: ""` 教训）。
    width = height = None
    try:
        from PIL import Image

        with Image.open(path) as img:
            width, height = img.size
    except Exception:  # noqa: BLE001 - 非图片 / 读不出，按缺值走
        pass

    item = ps.build_upload_item(
        seller_id,
        file_id=file_id,
        file_name=path.name,
        size=size,
        width=width,
        height=height,
        preview_url=preview_url,
        father_directory_id="0",
    )
    ok, msg, raw_resp = api.register_material_files(seller_id, [item])
    if not ok:
        return False, msg or "登记图片到图片空间失败", None

    # 归一化成 `build_image_entry` / `build_publish_image` 要的形状。
    additional = item.get("additionalData") or {}
    #: ⚠️⚠️ `materialId` **必须**是登记返回的**记录 id**（`directoryManageables[0].id`，
    #: 形如 `6ac6928e075fea0001d39c0a`），**不是**上传凭证里的 `fileId`
    #: （`arkgoods/1040g…`）。真机 2026-10-08 实测：用 fileId 作 materialId 时
    #: 发布被平台拒「获取图片信息失败，请检查图片是否正确后重试」。
    registered_id = _registered_material_id(raw_resp) or file_id

    #: ⚠️⚠️ 登记的 `path` / `link` 是平台在**登记时新分配的 CDN id**
    #: （`arkgoods/1040g…`），**不是**上传凭证的 `fileId`（`arkgoods/XOARE…`）。
    #: 前端发布体里 `path`/`link`/`uploaderInfoModel.fileId` 三处并用同一个 CDN path；
    #: 凭证 id 会让平台同样报「获取图片信息失败」。⇒ 登记后**回读该记录**取真值。
    record = _fetch_material_record(api, name=str(item.get("name") or ""),
                                    material_id=registered_id)
    path_value = str(record.get("path") or additional.get("path") or file_id)
    link_value = str(record.get("link") or additional.get("link")
                     or f"https://qimg.xiaohongshu.com/{path_value}")
    return True, "成功", {
        "link": link_value,
        "path": path_value,
        "extension": additional.get("extension") or path.suffix,
        "width": width,
        "height": height,
        "materialId": registered_id,
        "name": str(item.get("name") or ""),
        "size": size,
    }


def _material_additional(record: dict[str, Any]) -> dict[str, Any]:
    """图片空间记录里的 `additionalData`（可能是 dict，也可能是 JSON 字符串）。"""
    raw = record.get("additionalData") or {}
    if isinstance(raw, str):
        try:
            import json as _json

            raw = _json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _fetch_material_record(api: Any, *, name: str, material_id: str) -> dict[str, Any]:
    """登记后回读该图片记录，取平台分配的 path / link（真机 2026-10-08）。

    登记响应只回 id，而 path / link 是登记时新分配的 CDN id —— 不读回就会把
    上传凭证的 id 当成素材 id 发出去，平台报「获取图片信息失败」。
    按名称搜索 + id 精确匹配（名称可能重名，故必须用 id 判等）。

    ⚠️ 图片空间搜索最终一致：真机实测登记后立刻搜搜不到（n=0），约 1–2 秒后可见，
    轮询重试（最多 6 次 × 1s），避免「偶发读到空」的 flaky。

    ⚠️ 2026-10-13 注：发布链路已不再依赖这里的回读 path（`build_publish_image`
    直接从带签名 url 提取短格式 fileId）。本函数仍服务于上传端点返回 path/link
    给前端，保持「按 name 搜单页」的原始口径，不做空 keyword 拉全量。
    """
    if not name:
        return {}
    from backend.app.services import picture_space_service as ps

    for _ in range(_MATERIAL_READBACK_TRIES):
        try:
            ok, _msg, raw = api.search_material_space(
                ps.build_search_body(keyword=name, page=1, page_size=50)
            )
        except Exception:  # noqa: BLE001 - 回读失败按缺值走（不阻断上传）
            return {}
        if ok and isinstance(raw, dict):
            for item in ((raw.get("data") or {}).get("directoryManageables") or []):
                if not isinstance(item, dict) or str(item.get("id") or "") != material_id:
                    continue
                additional = _material_additional(item)
                path_value = str(additional.get("path") or "").strip()
                if path_value:
                    return {
                        "path": path_value,
                        "link": str(additional.get("link") or "").strip(),
                    }
        time.sleep(_MATERIAL_READBACK_WAIT)
    return {}


def _registered_material_id(raw_resp: Any) -> str:
    """登记响应里的**图片空间记录 id**（`data.directoryManageables[0].id`）。

    真机响应：`{"code":0,"success":true,"data":{"directoryManageables":[{"id":"6ac6…"}]}}`。
    """
    if isinstance(raw_resp, dict):
        data = raw_resp.get("data")
        if isinstance(data, dict):
            items = data.get("directoryManageables") or []
            if items and isinstance(items[0], dict):
                return str(items[0].get("id") or "")
    return ""


def _link_from_response(raw_resp: Any, file_id: str) -> str:
    """登记响应里拿公开链接（拿不到就按图片空间的 CDN 规则构造）。

    ⚠️ 构造是**兜底**不是首选：登记响应里有就用响应里的（那是平台的真相）。
    """
    if isinstance(raw_resp, dict):
        data = raw_resp.get("data")
        if isinstance(data, dict):
            items = data.get("directoryManageables") or []
            if items and isinstance(items[0], dict):
                additional = items[0].get("additionalData") or {}
                if additional.get("link"):
                    return str(additional["link"])
    return f"https://qimg.xiaohongshu.com/{file_id}"


def resolve_variant_id(
    *, variant_name: str, ledger_variant_ids: dict[str, str]
) -> str:
    """规格名 → 平台规格 id（**降级链，取不到不猜**，设计 23 §4.4）。

    `ledger_variant_ids` 是「本店既有商品扫出的规格名 → id」映射，
    由调用方从台账 / `publish_render` 取（`_collect_ledger_variant_ids`）。

    ⚠️ **绝不硬编码 id**：平台的规格 id 是店铺级 / 类目级的，
    写死一个值在别的店 / 别的类目上必然错。取不到就返回空串，
    由调用方决定是报 400 还是留空（**不编一个「颜色分类」的 id 出来**）。
    """
    key = package._variant_match_key(variant_name)
    if not key:
        return ""
    for name, vid in (ledger_variant_ids or {}).items():
        if package._variant_match_key(name) == key:
            return str(vid)
    return ""


def collect_ledger_variant_ids(
    *, api: Any, item_ids: list[str], max_items: int = 3
) -> dict[str, str]:
    """从**本店既有商品**的 `publish_render` 里扫出「规格名 → 规格 id」。

    真机来源：编辑任一既有商品，回读 `productDetail.skuList[].skuVariantInfos[]`
    的 `variantName` / `variantId`（如 `颜色分类` = `60404e0de85df80001991224`）。

    ⚠️ **扫本店数据是唯一诚实的做法**（见 `resolve_variant_id` 的说明）。
    扫不到 ⇒ 返回空 dict，调用方据此提示用户先去千帆建一个商品 ——
    **不编 id**。最多扫 `max_items` 个商品（这是「取规格字典」不是「同步商品」，
    扫三个足够覆盖本店在用的规格，且把写链路的读请求压到最小）。
    """
    mapping: dict[str, str] = {}
    for item_id in list(item_ids)[:max_items]:
        ok, _msg, payload = api.publish_render(
            {"publishType": 2, "sourceType": 1, "itemId": str(item_id)}
        )
        if not ok or not isinstance(payload, dict):
            continue
        raw = payload.get("data")
        if not isinstance(raw, str):
            continue
        try:
            import json

            tree = json.loads(raw)
        except ValueError:
            continue
        detail = ((tree.get("product") or {}).get("productDetail")) or {}
        for sku in detail.get("skuList") or []:
            for variant in sku.get("skuVariantInfos") or []:
                name = str(variant.get("variantName") or "").strip()
                vid = str(variant.get("variantId") or "").strip()
                if name and vid:
                    mapping.setdefault(name, vid)
    return mapping


def read_draft(api: Any, draft_id: str) -> dict[str, Any]:
    """回读草稿（**校验真的落进去了**，设计 23 §4.2 第 4 步）。

    返回归一化的计数，供 `diff_draft_readback` 比对。读不到 ⇒ 全 0
    （差集会把每一项都报出来 —— 这是对的，读不到就该报警）。
    """
    import json

    ok, _msg, payload = api.publish_render(
        {"publishType": 1, "sourceType": 4, "draftId": str(draft_id)}
    )
    if not ok or not isinstance(payload, dict):
        return {"category_id": "", "name": "", "image_count": 0,
                "long_image_count": 0, "sku_count": 0, "read_ok": False}
    raw = payload.get("data")
    if not isinstance(raw, str):
        return {"category_id": "", "name": "", "image_count": 0,
                "long_image_count": 0, "sku_count": 0, "read_ok": False}
    try:
        tree = json.loads(raw)
    except ValueError:
        return {"category_id": "", "name": "", "image_count": 0,
                "long_image_count": 0, "sku_count": 0, "read_ok": False}
    detail = ((tree.get("product") or {}).get("productDetail")) or {}
    item = detail.get("item") or {}
    return {
        "category_id": str(item.get("categoryId") or ""),
        "name": str(item.get("itemName") or ""),
        "image_count": len(item.get("standardImages") or []),
        "long_image_count": len(item.get("longImages") or []),
        "sku_count": len(detail.get("skuList") or []),
        "read_ok": True,
    }


def create_draft(
    *,
    api: Any,
    seller_id: str,
    category_id: str,
    name: str,
    main_image_paths: list[str],
    detail_image_paths: list[str],
    skus: list[dict[str, Any]],
    ledger_variant_ids: dict[str, str] | None = None,
    #: ⚠️ 2026-10-10（用户口径「上传成功后保存草稿用远程 url」）：已上传对象
    #: `{materialId 或 path → entry}`，`main_image_paths` / `detail_image_paths` 里的
    #: 元素若是这个 key（非文件路径），就**跳过上传直接用**。
    pre_uploaded: dict[str, dict[str, Any]] | None = None,
    #: ⚠️ 2026-10-11（草稿与发布一致，方案 A）：视频已上传对象 / 货号 / 第二步配置。
    #: 草稿体复用发布链路的 `build_item_body`（完整 itemData），不再用旧的 5 键精简体。
    videos: list[dict[str, Any]] | None = None,
    article_no: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """**新增商品（存草稿）** 的完整执行链。

    `skus` 每条：`{"name": str, "price_yuan": float|None, "stock": int|None,
    "image_path": str|None}`。

    ⚠️ `ledger_variant_ids` 为空且 SKU 需要规格时 —— **不猜 id**，
    挂进 `warnings` 让用户去千帆页面补（草稿本身建得出来，规格留空）。
    这比「编一个 id」诚实：编出来的 id 平台会拒或落到错误的规格上。
    """
    warnings: list[str] = []
    uploaded: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    pre_uploaded = pre_uploaded or {}

    # 去重（同一路径可能既是主图又是 SKU 图 —— 传两次是白耗一次写请求）
    seen: set[str] = set()
    ordered: list[str] = []
    for path_str in [*main_image_paths, *detail_image_paths,
                     *[str(s.get("image_path") or "") for s in skus]]:
        text = str(path_str or "").strip()
        if text and text not in seen:
            seen.add(text)
            ordered.append(text)

    if len(ordered) > MAX_DRAFT_IMAGES:
        warnings.append(
            f"图片共 {len(ordered)} 张，超过单次 {MAX_DRAFT_IMAGES} 张上限，"
            f"只上传前 {MAX_DRAFT_IMAGES} 张"
        )
        ordered = ordered[:MAX_DRAFT_IMAGES]

    by_path: dict[str, dict[str, Any]] = {}
    for path_str in ordered:
        #: ⚠️ 2026-10-10：已上传对象（key 命中 `pre_uploaded`）跳过上传直接用。
        if path_str in pre_uploaded:
            by_path[path_str] = pre_uploaded[path_str]
            uploaded.append(pre_uploaded[path_str])
            continue
        #: ⚠️ **上传要重试**（真机实测：本机系统代理下大 body 的 ROS PUT 偶发
        #: `ProxyError` / `SSLEOFError`）。这是环境抖动，一次失败就放弃的表现是
        #: 「同一份数据包这次少两张图、下次又好了」—— 用户无法归因。
        last_reason = ""
        for attempt in range(IMAGE_UPLOAD_RETRIES):
            ok, reason, entry = upload_one_image(
                api, seller_id=seller_id, file_path=path_str
            )
            if ok and entry:
                by_path[path_str] = entry
                uploaded.append(entry)
                break
            last_reason = reason
            if attempt < IMAGE_UPLOAD_RETRIES - 1:
                time.sleep(IMAGE_UPLOAD_RETRY_WAIT * (attempt + 1))
        else:
            #: ⚠️ 单张失败**不阻断**整单（部分成功照建），但必须逐条如实报 ——
            #: 静默丢一张主图的后果是「商品图不全，用户不知道」。
            failed.append({"path": path_str, "reason": last_reason})
            warnings.append(
                f"图片上传失败（{Path(path_str).name}，重试 {IMAGE_UPLOAD_RETRIES} 次）："
                f"{last_reason}"
            )

    if not [p for p in main_image_paths if p in by_path]:
        warnings.append(
            "没有任何主图上传成功 —— 草稿已建但无商品图，"
            "需在千帆商品编辑页补充主图后才能提交"
        )

    #: ⚠️ 2026-10-11（方案 A：草稿与发布一致）—— 草稿体**复用发布链路**的
    #: `build_item_from_request`（同一份组装逻辑），产出的 itemData 与发布体逐字一致
    #: （含 `videos` / `articleNo` / `ename` / `attributes` / `skuData` 全字段），
    #: 再走真机新接口 `save_product_drafts`。**不再用**旧的 5 键精简体
    #: `build_draft_body`（那是已被真机淘汰的 `create_product_drafts` 形状）。
    from backend.app.services import shop_item_publish_service as publish_svc

    #: 取骨架（既有商品当模板）+ 店级物流/运费模板 —— 与发布链路同源。
    skeleton = publish_svc.load_item_skeleton(api, item_id="") or {}
    fulfillment: dict[str, str] = {}
    try:
        ok_pc, _msg_pc, pc = api.publish_render({"publishType": 1, "sourceType": 1})
        carriage: Any = None
        try:
            ok_ct, _msg_ct, carriage = api.get_seller_carriage_template()
            if not ok_ct:
                carriage = None
        except Exception:  # noqa: BLE001
            carriage = None
        if ok_pc and isinstance(pc, dict):
            fulfillment = pub.parse_fulfillment_info(pc, carriage)
    except Exception:  # noqa: BLE001
        fulfillment = {}

    #: SKU 行归一成 `build_item_from_request` 要的形态（保留 ledger 的 variant_id 回落）。
    sku_rows: list[dict[str, Any]] = []
    for row in skus[:MAX_DRAFT_SKUS]:
        name_text = str(row.get("name") or "")
        variant_name, _value = package.split_sku_name(name_text)
        variant_id = resolve_variant_id(variant_name=variant_name, ledger_variant_ids=ledger_variant_ids)
        if variant_name and not variant_id:
            warnings.append(
                f"SKU「{name_text[:30]}」的规格「{variant_name}」在本店既有商品里没找到，"
                "规格留空（需在千帆页面补选）"
            )
        sku_rows.append({**row, "variant_id": variant_id})

    body = publish_svc.build_item_from_request(
        skeleton=skeleton,
        name=name,
        category_id=category_id,
        uploaded=by_path,
        main_image_paths=main_image_paths,
        detail_image_paths=detail_image_paths,
        videos=videos,
        article_no=article_no,
        skus=sku_rows,
        variant_ids=[],
        fulfillment=fulfillment,
        attributes=None,
        spec_attributes=(config or {}).get("specAttributes") or None,
        config=config,
    )

    # 建草稿（限流时按平台节奏重试 —— 不硬闯）
    write_result: dict[str, Any] = {}
    ok = False
    message = ""
    for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
        #: ⚠️ 2026-10-11 真机 bundle：**新建**走 `create_product_drafts`（返回 `productDraftId`），
        #: **更新**（有 draftId）才走 `save_product_drafts`（不回 id）。本函数是「新建商品
        #: → 存草稿」链路 ⇒ 走 create。两个接口 body 都是完整 itemData 对象。
        ok, message, payload = api.create_product_draft(body)
        if ok:
            write_result = payload if isinstance(payload, dict) else {}
            break
        if api.is_draft_rate_limited(message) and attempt < _MAX_RATE_LIMIT_RETRIES:
            warnings.append(
                f"平台限流（每分钟 {DRAFT_LIMIT_PER_MINUTE} 个草稿），"
                f"等待 {_RATE_LIMIT_WAIT_SECONDS}s 后重试（第 {attempt + 1} 次）"
            )
            time.sleep(_RATE_LIMIT_WAIT_SECONDS)
            continue
        break

    if not ok:
        raise DraftError("DRAFT_CREATE_FAILED", message or "平台拒绝了建草稿请求")

    #: ⚠️ 2026-10-11 真机：`create_product_drafts`（新建）响应**回 `productDraftId`**；
    #: `save_product_drafts`（更新）才不回。本函数走新建 ⇒ 从 `productDraftId` 取。
    _data = (write_result.get("data") or {}) if write_result else {}
    draft_id = str(_data.get("productDraftId") or _data.get("draftId") or "")

    # ⚠️ 回读校验（**不是可选项**）：接口 200 但字段被静默丢弃是常态。
    readback: dict[str, Any] = {}
    if draft_id:
        readback = read_draft(api, draft_id)

    return {
        "draft_id": draft_id,
        "name": name,
        "uploaded_images": len(uploaded),
        "failed_images": failed,
        "sku_count": len(sku_rows),
        "warnings": warnings,
        "readback": readback,
        #: ⚠️ **固定文案**：无论哪条路径，回执都写清「没上架」——
        #: 用户看到「成功」时最容易脑补成「已发布」。
        "note": "已保存到千帆草稿箱，未上架（不会出现在商品列表中）。"
                "请在千帆「商品管理 → 草稿箱」里继续补充信息后自行提交。",
    }


def _tmp_name(prefix: str, suffix: str) -> str:
    """临时文件名（把上传的字节落到磁盘 —— SDK 的 `upload_file` 吃路径）。"""
    return str(Path(tempfile.gettempdir()) / f"{prefix}-{uuid.uuid4().hex}{suffix}")


__all__ = [
    "DraftError",
    "DRAFT_LIMIT_PER_MINUTE",
    "MAX_DRAFT_IMAGES",
    "MAX_DRAFT_SKUS",
    "collect_ledger_variant_ids",
    "create_draft",
    "read_draft",
    "resolve_variant_id",
    "upload_one_image",
]
