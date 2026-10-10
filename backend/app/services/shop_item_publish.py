"""商品「提交商品」（发布到线上）的**纯逻辑**（设计 24 §4.1）。

## 这个模块只做「组装」，不做「执行」

零平台请求、零数据库 —— 组装好的 body 由
`shop_item_publish_service` 交给 SDK 发出。这样「发布体的形状」可以**单测钉住**，
而不用起平台会话（与设计 23 的 `shop_draft_package` 同思路）。

## ⚠️⚠️ 单位：**发布体的 `price` 是「分」，不是「元」**

这与**草稿体相反**（设计 23 的 `build_sku_entry` 用「元」）：
* 草稿体：传 `1280` 回读 `1280`（元）；
* 发布体：页面填 **1280 元** ⇒ 真机抓包 body `price: 128000`（**分**）。

bundle `8091.*.js` 逐字佐证：`basePriceInfo.price/100`（读形态 分→元）。
⇒ 两条链路的单位不同，混用**平台不报错**，只是价格差 100 倍（静默错值）。
本模块的 `build_publish_sku` 是唯一换算点（×100），守卫钉死。
"""

from __future__ import annotations

from typing import Any

#: 普通发布（bundle `publishMode:function()`：非基础图创建、非 AI 助手 ⇒ 1）。
#: 2 = 基础图创建 / 3 = AI 助手 —— 本项目**只用 1**（设计 24 §1.4.2）。
PUBLISH_MODE_NORMAL: int = 1

#: 从骨架里**必须剥掉**的键（真机实测被平台拒 / 指向旧商品 / 形态不符）。
#: 骨架（`publish_render` 回读既有商品）带了一堆读形态字段，直接透传会被拒。
_DROP_FROM_SKELETON: frozenset[str] = frozenset({
    "standardSizeTableInfo",       # 平台：此类目不支持尺码表配置
    "sizeTableType",
    "recommendSizeTableType",
    "modelTryOnSizeTableType",
    "standardImages",              # 读/草稿体键名；发布体用 images
    "longImages",                  # 同上；发布体用 imageDescriptions
    "properties",                  # 读形态；发布体用 attributes
    "description",
    "sellerId",
    "itemName",                    # 发布体用 name
    "categoryPath",
    "unionType",
    "allowancePlan",
    "brandId",
    "isAutoOffShelf",
    "autoOffShelfTime",
    "expectedPurchaseTime",
    "instructionImages",
    "priceConfidential",
    "shippingGrossWeight",
    "o2oShowShopAddress",
    "stepPresaleDeliveryMode",
    "specialDeliveryRule",
    "enableAIOptimizeImage",
})

#: `itemData[]` 里我们**自己填**的顶层键；其余从 `publish_render` 骨架继承。
#: 平台强制的默认值（真机取证）—— 这些键不填平台会报缺字段或取错默认。
_PLATFORM_DEFAULTS: dict[str, Any] = {
    #: ⚠️ `brandId` 被 `_DROP_FROM_SKELETON` **刻意剥掉**（读形态里是别的键名），
    #: 但发布体**必须有它** —— 真机抓包实值 `"-999"`（= 无品牌，与页面「商品品牌：无品牌」一致）。
    #: 2026-10-10 真机体逐字段 diff 实测：缺它是我方唯一「真机有我无」的**顶层**键。
    "brandId": "-999",
    "stockType": 1,
    "deliveryMode": 1,          # 现货模式
    "freeReturn": 1,            # 支持七天无理由
    "usePlayback": 1,
    "enableMultiWarehouse": False,
    "source": 2,                # 真机抓包值
    "qualifications": [],
    "nutritionFacts": None,
    "isGeneralSettings": {
        "deliveryTime": True,
        "whcode": True,
        "imageBindSpu": True,
        "imagesDesc": True,
        "imagesDescV2": False,
        "logisticsPlanId": True,
    },
}


def yuan_to_fen(price_yuan: float | int | None) -> int | None:
    """元 → 分（发布体专用）。缺值返回 `None`（**不填 0** —— 0 元会被当真价格收下）。"""
    if price_yuan is None:
        return None
    return int(round(float(price_yuan) * 100))


def build_publish_image(material: dict[str, Any]) -> dict[str, Any]:
    """一条发布体图片条目（主图 / 详情图 / 规格图 **三处同形状**，设计 24 §1.5）。

    与草稿体图片条目（`shop_draft_package.build_image_entry`）的差别：
    发布体用 `key` + `type` + `uploaderInfoModel`，**没有** `sourceLink`。
    多出的 `errors: []` 是平台回读形态带上的，照带不报错。
    """
    material_id = str(material.get("materialId") or "").strip()
    link = str(material.get("link") or "").strip()
    entry: dict[str, Any] = {
        "key": material_id,
        "type": "spaceImage",
        "materialId": material_id,
        "name": str(material.get("name") or "").strip(),
    }
    width, height = material.get("width"), material.get("height")
    #: ⚠️ 尺寸拿不到就**整个键不发**（设计 22 `For input string: ""` 教训）。
    if width is not None:
        entry["width"] = int(width)
    if height is not None:
        entry["height"] = int(height)
    if material.get("size") is not None:
        entry["size"] = int(material["size"])
    entry["extension"] = str(material.get("extension") or "").strip()
    entry["path"] = str(material.get("path") or "").strip()
    entry["link"] = link
    entry["uploaderInfoModel"] = {
        "bizName": "ark",
        "cloudType": 4,
        "fileId": str(material.get("path") or "").strip(),
        "isSecret": False,
        "scene": "goods_image",
        "url": link,
    }
    entry["errors"] = []
    return entry


def build_publish_video(material: dict[str, Any]) -> dict[str, Any]:
    """一条发布体**视频**条目（真机 2026-10-10 抓包，`itemData[0].videos[]`）。

    ⚠️ 与图片条目**不同形状**：`type` 无 / `uploaderInfoModel.scene = "goods_video"`（不是
    `goods_image`）/ 有 `duration` / `bucket`。`src`/`file`/`uploadPercent` 是前端临时态，
    **不进提交体**。只保留 `path/link/extension/duration/width/height/size/uploaderInfoModel/materialId`。
    """
    material_id = str(material.get("materialId") or "").strip()
    link = str(material.get("link") or material.get("url") or "").strip()
    path = str(material.get("path") or "").strip()
    entry: dict[str, Any] = {
        "extension": str(material.get("extension") or "").strip(),
        "bucket": "sale-item",
        "path": path,
        "link": link,
        "uploaderInfoModel": {
            "url": link,
            "bizName": "ark",
            "scene": "goods_video",
            "fileId": path,
            "isSecret": True,
            "cloudType": 4,
        },
    }
    if material_id:
        entry["materialId"] = material_id
    if material.get("duration") is not None:
        entry["duration"] = int(material["duration"])
    if material.get("width") is not None:
        entry["width"] = int(material["width"])
    if material.get("height") is not None:
        entry["height"] = int(material["height"])
    if material.get("size") is not None:
        entry["size"] = int(material["size"])
    return entry


def build_publish_sku(
    *,
    row_number: int,
    price_yuan: float | int | None,
    stock: int | None,
    logistics_plan_id: str,
    variants: list[dict[str, Any]],
    spec_image: dict[str, Any] | None = None,
    delivery_time: dict[str, Any] | None = None,
    market_price_yuan: float | int | None = None,
    available: bool = True,
    barcode: str | None = None,
    erp_code: str | None = None,
) -> dict[str, Any]:
    """一条发布体 SKU（真机 10 字段，设计 24 §1.3）。

    ⚠️ **`price` 单位是「分」**（`yuan_to_fen`，唯一换算点）。
    ⚠️ `priceInfo.preTax` **必填** —— 缺了平台报「售价计算失败: 商品价格未存在」。
    ⚠️ `originPrice` = **划线价**（真机体 `{price:800000, originPrice:900000}`）——
    2026-10-10 真机体逐字段 diff 实测：漏它 = 用户填的划线价**从未提交**。
    """
    fen = yuan_to_fen(price_yuan)
    price: dict[str, Any] = {"price": fen, "includeTax": False, "priceInfo": {"preTax": fen}}
    #: 划线价：用户填了才带（缺值**不造假** —— 不拿售价冒充划线价）。
    market_fen = yuan_to_fen(market_price_yuan)
    if market_fen is not None:
        price["originPrice"] = market_fen
    entry: dict[str, Any] = {
        "rowNumber": int(row_number),
        "ipq": 1,
        "price": price,
        "logisticsPlanId": str(logistics_plan_id or ""),
        "variants": list(variants),
        "available": bool(available),
        #: ⚠️ 真机 `stockChange` 是**本次库存变化量**（真机体 = 1，不是 0）。
        #: 取该 SKU 的库存数（真机逐条一致）；无库存 ⇒ 0。
        "stockChange": int(stock or 0),
        "stock": int(stock or 0),
    }
    #: mainSpecEnable=1 时每个 SKU 都要规格图（真机：缺 ⇒ 「请上传规格图」）。
    if spec_image:
        entry["specImage"] = spec_image
    entry["deliveryTime"] = delivery_time or {"type": 4, "time": "48"}
    #: ⚠️ 2026-10-10 对齐真机（bundle 权威字段名）：条形码 / 商家编码是 SKU 合法字段，
    #: 用户填了才带（空 = 省略，真机不填时 `skuData[]` 无这两个键）。
    if barcode:
        entry["barcode"] = str(barcode)
    if erp_code:
        entry["erpCode"] = str(erp_code)
    return entry


def build_publish_variant(
    *, variant_id: str, variant_name: str, variant_value: str, variant_type: int = 1
) -> dict[str, Any]:
    """发布体 SKU 的 `variants[]` 条目（比草稿体少 `attributeValueId`）。"""
    return {
        "attributeId": str(variant_id),
        "attributeName": str(variant_name),
        "attributeType": int(variant_type),
        "attributeValue": str(variant_value),
    }


def build_variant_sequence(
    variants: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """`variantSequence`（规则维度 + 全部规格值）。

    `variants` 形如 `[{"variantId": ..., "variantName": "颜色分类", "variantValue": "实木三斗柜"}]`，
    同名规格的值**去重保序**合并成一条。
    """
    order: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for v in variants:
        vid = str(v.get("variantId") or "")
        if not vid:
            continue
        if vid not in by_id:
            by_id[vid] = {"variantId": vid, "variantName": str(v.get("variantName") or ""),
                          "variantValues": []}
            order.append(vid)
        val = str(v.get("variantValue") or "")
        if val and val not in by_id[vid]["variantValues"]:
            by_id[vid]["variantValues"].append(val)
    return [by_id[vid] for vid in order]


def build_item_body(
    *,
    skeleton: dict[str, Any],
    name: str,
    category_id: str,
    attributes: list[dict[str, Any]] | None,
    main_images: list[dict[str, Any]],
    detail_images: list[dict[str, Any]],
    videos: list[dict[str, Any]] | None = None,
    article_no: str | None = None,
    variant_ids: list[str],
    skus: list[dict[str, Any]],
    variant_sequence: list[dict[str, Any]] | None = None,
    shipping_template_id: str | None = None,
    logistics_plan_id: str | None = None,
    item_short_title: str | None = None,
    delivery_mode: int | None = None,
    price_confidential: bool | None = None,
    description: str | None = None,
    use_playback: int | None = None,
    english_name: str | None = None,
) -> dict[str, Any]:
    """组装 `itemData[0]`（设计 24 §4.1）。

    **以 `publish_render` 的既有商品骨架为基底**，覆盖用户填的值 ——
    因为发布体有 25 个顶层字段，其中大半（`categoryId` 之外的平台必填默认、
    `isGeneralSettings` 明细、`variantIds` …）用户根本没填，凭空造必然缺字段。
    真机做法也是「取一个既有商品的骨架再改」。

    `skeleton` 为空时退化为「只按平台默认值组装」（仍可用，但字段可能不全，
    调用方应在没有既有商品时如实提示用户）。
    """
    body: dict[str, Any] = {
        k: v for k, v in dict(skeleton or {}).items() if k not in _DROP_FROM_SKELETON
    }
    #: 平台强制默认值（覆盖骨架里可能过期的旧值）。
    body.update(_PLATFORM_DEFAULTS)
    body["categoryId"] = str(category_id)
    body["name"] = str(name)
    body["itemShortTitle"] = str(item_short_title or body.get("itemShortTitle") or "")
    if attributes is not None:
        body["attributes"] = list(attributes)
    body["images"] = list(main_images)
    body["imageDescriptions"] = list(detail_images)
    #: ⚠️ 2026-10-11：视频 `videos[]`（真机抓包确认，`itemData[0].videos`）。
    #: 只填用户传的（空则省略该键 —— 真机不带视频时无 `videos`）。
    #: 每条经 `build_publish_video` 归一成真机形状（剔除前端临时态 src/file/uploadPercent）。
    if videos:
        body["videos"] = [build_publish_video(v) for v in videos]
    #: 货号 `articleNo`（真机字段，2026-10-10 补）。
    if article_no:
        body["articleNo"] = str(article_no)
    body["variantIds"] = [str(v) for v in variant_ids]
    #: mainSpecEnable=1 ⇒ 必须有主规格 id（真机取第一个 variantId）。
    body["mainSpecEnable"] = 1 if variant_ids else 0
    if variant_ids:
        body["mainSpecId"] = str(variant_ids[0])
    body["skuData"] = list(skus)
    if variant_sequence is not None:
        body["variantSequence"] = list(variant_sequence)
    if shipping_template_id:
        body["shippingTemplateId"] = str(shipping_template_id)
    if logistics_plan_id:
        body["logisticsPlanId"] = str(logistics_plan_id)
    #: ⚠️ 2026-10-10：用户第二步填的配置字段覆盖进提交体（缺则留骨架旧值）。
    #: 这些字段此前**从未接入** ⇒ 提交体恒用骨架（上一个商品）的旧值。
    if delivery_mode is not None:
        body["deliveryMode"] = int(delivery_mode)
    if price_confidential is not None:
        body["priceConfidential"] = bool(price_confidential)
    if description is not None:
        body["description"] = str(description)
    if use_playback is not None:
        body["usePlayback"] = int(use_playback)
    if english_name is not None:
        #: ⚠️ 2026-10-10：真机字段是 **`ename`**（不是 `englishName`）—— 之前写错字段名，
        #: 英文名提交后平台收不到。
        body["ename"] = str(english_name)
    return body


def build_publish_payload(
    item_bodies: list[dict[str, Any]], *, publish_mode: int = PUBLISH_MODE_NORMAL
) -> dict[str, Any]:
    """组最终请求体（真机逐字，设计 24 §1.1）。

    ⚠️ `publishMode` **恒 1**（普通发布）；`options` 三键缺一不可
    （`retrieveResult=True` 才能在轮询里拿到失败原因）。
    """
    return {
        "itemData": list(item_bodies),
        "options": {"asyncExec": True, "withChild": True, "retrieveResult": True},
        "publishMode": int(publish_mode),
    }


def parse_task_result(payload: dict[str, Any] | None) -> tuple[bool, bool, list[dict[str, Any]]]:
    """从 `query_task_status` 回执解析 `(是否终态, 是否成功, 逐条失败原因)`。

    ⚠️ 终态判据用 `status ∈ {3,4,5}`（真机：3 = 成功终态）。
    `failData[]` 逐条如实报 —— **不静默**（用户靠它知道平台拒了什么）。
    """
    task = ((payload or {}).get("data") or {}).get("task") or {}
    status = task.get("status")
    final = status in (3, 4, 5)
    result = task.get("result") or {}
    extra = result.get("extraJson") or ""
    fails: list[dict[str, Any]] = []
    item_ids: list[str] = []
    if extra:
        import json

        try:
            parsed = json.loads(extra)
            fails = parsed.get("failData") or []
            item_ids = parsed.get("itemIds") or []
        except (ValueError, TypeError):
            fails = []
    #: ⚠️⚠️ **成功判据**：`failData` 为空 **且** 有 `itemIds`（真有商品产生）。
    #: **不能**用 `result.success` —— 真机实测发布成功时它是 `false`！
    success = final and not fails and bool(item_ids)
    return final, success, fails


def parse_item_ids(payload: dict[str, Any] | None) -> list[str]:
    """从任务回执里取本次产生的**商品 id 列表**（`extraJson.itemIds`）。

    ⚠️ 这是发布成功后**拿到真实 item_id 的正路** —— 比「按标题反查列表」
    更直接、且不受列表搜索延迟影响（真机 2026-10-08）。
    """
    import json

    task = ((payload or {}).get("data") or {}).get("task") or {}
    extra = (task.get("result") or {}).get("extraJson") or ""
    if not extra:
        return []
    try:
        return [str(x) for x in (json.loads(extra).get("itemIds") or [])]
    except (ValueError, TypeError):
        return []


def parse_operation_success(payload: dict[str, Any] | None) -> bool:
    """解析**上下架 / 删除**类任务的成败（与发布任务**判据不同**）。

    真机 2026-10-08：`batch_update_availability` / `batch_delete_sku` 的任务回执
    形如 `{"failCount":0,"successCount":3,"total":3}` —— **没有** `failData`/`itemIds`。
    ⇒ 判据 = `failCount == 0`（**不能**复用发布任务的判据，否则会判成失败）。
    """
    import json

    task = ((payload or {}).get("data") or {}).get("task") or {}
    extra = (task.get("result") or {}).get("extraJson") or ""
    if not extra:
        #: 没有明细 ⇒ 退回 `result.success`。
        return bool((task.get("result") or {}).get("success"))
    try:
        parsed = json.loads(extra)
    except (ValueError, TypeError):
        return False
    if "failCount" in parsed:
        return int(parsed.get("failCount") or 0) == 0
    if "failData" in parsed:
        return not (parsed.get("failData") or [])
    return bool((task.get("result") or {}).get("success"))


def _data_tree(payload: Any) -> dict[str, Any]:
    """把平台回执的 `data`（可能是 JSON 字符串）解成 dict；取不到返回 `{}`。"""
    import json

    raw = (payload or {}).get("data") if isinstance(payload, dict) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def parse_fulfillment_info(
    precheck_payload: Any, carriage_payload: Any = None
) -> dict[str, str]:
    """取**店级物流方案 id / 运费模板 id**（发布体必填，骨架里常缺）。

    ⚠️ **两个值来自两个不同接口**（真机 2026-10-09 实测，主账号 8）：

    * **物流方案（发货仓）** `logisticsPlanId` —— 空白
      `publish_render({publishType:1, sourceType:1})` 回
      `data.fulfillmentInfo.logisticsPlanList[0].planId`。
    * **运费模板** `shippingTemplateId` —— ⚠️ **`publish_render` 不返回该字段**：
      空白表单的 `fulfillmentInfo` 只有 `logisticsPlanList` / `logisticsPlanStatus`
      （既无 `shippingTemplateList` 也无 `defaultShippingTemplate`）。它只在
      `getSellerCarriageTemplate` 的 `data.fulfillmentInfo.shippingTemplateList`
      （+ `defaultShippingTemplate`）里 ⇒ 调用方**必须**把该回执单独传进来
      （`carriage_payload`）。少传一次 ⇒ 运费模板恒空。

    `carriage_payload` 给了就优先用它取运费模板；没给时若 `precheck_payload`
    恰好带了 `shippingTemplateList`（编辑读回 `publishType:2` 形态会带）也接受 ——
    但**不再假设** `publish_render` 会带它（那正是本函数此前的缺陷）。
    """
    out: dict[str, str] = {}

    #: 物流方案（发货仓）—— 空白 / 编辑 `publish_render` 都带 `logisticsPlanList`。
    info = _data_tree(precheck_payload).get("fulfillmentInfo") or {}
    plans = info.get("logisticsPlanList") or []
    if plans and isinstance(plans[0], dict):
        out["logistics_plan_id"] = str(plans[0].get("planId") or "")

    #: 运费模板 —— carriage 优先；其次 precheck（编辑读回形态）。
    tmpl_info: dict[str, Any] = {}
    for source in (carriage_payload, precheck_payload):
        candidate = _data_tree(source).get("fulfillmentInfo") or {}
        if candidate.get("shippingTemplateList") or candidate.get("defaultShippingTemplate"):
            tmpl_info = candidate
            break
    templates = tmpl_info.get("shippingTemplateList") or []
    default_template = tmpl_info.get("defaultShippingTemplate") or {}
    out["shipping_template_id"] = str(
        (default_template.get("templateId") if isinstance(default_template, dict) else "")
        or (templates[0].get("templateId") if templates and isinstance(templates[0], dict) else "")
    )
    return out
