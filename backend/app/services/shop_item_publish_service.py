"""商品「提交商品」（发布到线上）的**执行器**（设计 24 §4.2）。

## ⚠️ 红线：发布成功**第一时间下架**

用户口径（2026-10-08）：

> 注意红线：**发布成功的商品必须第一时间下架**。

落实：`publish_item()` 内部 `提交 → 轮询终态 → 成功 ⇒ 立刻下架` **串行执行**，
**不可拆**（调用方拿不到「只提交不下架」的入口 —— 除非显式传
`off_shelf_after=False`，那是测试钩子，默认 False 即红线开启）。

## 形态：同步执行（不是后台任务）

与「批量上架 / 下架」（设计 20 §11）**刻意不同**：发布的耗时可控
（组装 + 1 次提交 + 轮询），且用户**需要当场看到**「taskId / 商品 id /
下架是否成功 / 平台拒了什么」—— 丢后台就得再设计一套进度查询。

## 骨架来自既有商品

发布体 25 个顶层字段，其中大半用户没填（平台必填默认 / `isGeneralSettings` 明细）。
凭空造必然缺字段 ⇒ 取**本店任一既有商品**的 `publish_render` 回读骨架做基底
（真机做法一致）。没有既有商品时如实提示用户（见 `_SKELETON_REQUIRED`）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from backend.app.services import shop_item_publish as pub
from backend.app.services import shop_draft_package as package

logger = logging.getLogger(__name__)

#: 发布任务轮询：最多 N 次、每次间隔 M 秒。
_PUBLISH_POLL_TRIES: int = 12
_PUBLISH_POLL_INTERVAL: float = 3.0

#: 发布体图片 / SKU 上限（与草稿体同口径，**前后端同值**）。
MAX_PUBLISH_IMAGES: int = 100
MAX_PUBLISH_SKUS: int = 200

#: 数据包默认物流模板 / 方案 —— **平台店级**，取不到就留空让平台补默认。
_SKELETON_MISSING = (
    "本店没有任何既有商品可作为发布体骨架 —— 请先在千帆页面建一个商品"
    "（或用「新增商品 → 草稿箱」建一个草稿），再回来发布。"
)


class PublishError(RuntimeError):
    """发布失败（带结构化 `code`，由端点映射成 HTTP）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _api_factory(cookies: str) -> Any:
    from apis.xhs_qianfan_apis import XHS_Qianfan_Apis

    return XHS_Qianfan_Apis(cookies)


def load_item_skeleton(api: Any, *, item_id: str = "") -> dict[str, Any] | None:
    """取一份**既有商品**的可编辑骨架（`publish_render` `sourceType=1`）。

    `item_id` 空 ⇒ 用本店**最近创建**的一个在售商品（真机发布页也是取既有商品）。
    返回 `productDetail.item`（20+ 顶层字段），或 `None`（本店无商品）。
    """
    import json

    target = str(item_id or "").strip()
    if not target:
        #: ⚠️ 方法名必须是 `search_items`（SDK 里没有 `list_recent_items`）——
        #: 2026-10-10 真机直调踩到 `AttributeError: no attribute 'list_recent_items'`。
        try:
            ok, _msg, page = api.search_items(
                page_no=1, page_size=1, status="on_shelf",
                sort_field="create_time", order="desc",
            )
        except Exception:  # noqa: BLE001 - SDK 异常按「取不到骨架」处理，别 500
            logger.exception("取既有商品骨架失败（search_items 异常）")
            return None
        rows = _page_rows(page)
        if not rows:
            return None
        target = str(rows[0].get("item_id") or "")
        if not target:
            return None
    try:
        ok, _msg, payload = api.publish_render(
            {"publishType": 2, "sourceType": 1, "itemId": target}
        )
    except Exception:  # noqa: BLE001 - SDK 异常按「取不到骨架」处理
        logger.exception("取既有商品骨架失败（publish_render 异常）")
        return None
    if not ok or not isinstance(payload, dict):
        return None
    raw = payload.get("data")
    if not isinstance(raw, str):
        return None
    try:
        tree = json.loads(raw)
    except ValueError:
        return None
    detail = ((tree.get("product") or {}).get("productDetail")) or {}
    item = detail.get("item") or {}
    #: 去掉身份类字段（沿用会让平台认成「编辑」而非「新建」）。
    for key in ("itemId", "spuId", "articleNo", "auditStatusList", "lockInfoList",
                "saleQty", "origItemId", "needSyncCategory", "groupChatChannelItem",
                "isChannel", "itemFreeze"):
        item.pop(key, None)
    return item or None


def build_item_from_request(
    *,
    skeleton: dict[str, Any],
    name: str,
    category_id: str,
    uploaded: dict[str, dict[str, Any]],
    main_image_paths: list[str],
    detail_image_paths: list[str],
    videos: list[dict[str, Any]] | None = None,
    article_no: str | None = None,
    skus: list[dict[str, Any]],
    variant_ids: list[str],
    fulfillment: dict[str, str] | None = None,
    attributes: list[dict[str, Any]] | None = None,
    spec_attributes: list[dict[str, str]] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把「上传成功的图片 + SKU 行」组装成 `itemData[0]`（设计 24 §4.1）。

    `fulfillment` 是店级物流方案 / 运费模板（`parse_fulfillment_info` 取的），
    骨架里常缺 ⇒ 由它补（平台报「商品必填信息[logisticsPlanId]未填写」）。
    """
    #: ⚠️⚠️ 用户报「接口的媒体资源已经上传，为何 main_images / detail_images 用的
    #: 均不是**已经上传后返回的地址**」（2026-10-10）。
    #: 真根因：`uploaded` 里存的是 `shop_draft_service.upload_one_image` 的**简化 entry**
    #: （缺 `key`/`type`/`uploaderInfoModel`/`errors`）—— 发布体要的是
    #: `build_publish_image()` 形状（设计 24 §1.5，真机抓包 `images[0]` 逐字比对过）。
    #: 这里**从没调过它**（全仓零调用）⇒ 平台认不出「已上传的图」，
    #: 表现为 `images[]` 里没有上传返回的 `path`/`link`/`materialId` 结构。
    #: 修法：统一在**唯一出口**做形状转换（main / detail / 规格图三处同一函数，
    #: 真机三处形状一致）。
    main_images = [pub.build_publish_image(uploaded[p]) for p in main_image_paths if p in uploaded]
    detail_images = [pub.build_publish_image(uploaded[p]) for p in detail_image_paths if p in uploaded]
    #: ⚠️ 2026-10-10：物流/运费模板用**用户第二步选的值**优先（`config` 里有），
    #: 缺才回落平台拉的默认值 / 骨架。此前恒用 `fulfillment`（平台默认）⇒
    #: 用户第二步选的模板**从未生效**。
    cfg = config or {}
    logistics_plan_id = str(
        cfg.get("logisticsPlanId")
        or (fulfillment or {}).get("logistics_plan_id")
        or skeleton.get("logisticsPlanId") or ""
    )
    shipping_template_id = str(
        cfg.get("freightTemplateId")
        or (fulfillment or {}).get("shipping_template_id")
        or skeleton.get("shippingTemplateId") or ""
    )

    variant_seq_src: list[dict[str, str]] = []
    sku_rows: list[dict[str, Any]] = []
    #: `规格名 → 平台 attributeId`（第二步的 `saleProperties` 下发）。
    spec_id_by_name: dict[str, str] = {
        str(x.get("name") or ""): str(x.get("id") or "") for x in (spec_attributes or [])
    }
    for index, row in enumerate(skus, start=1):
        variants: list[dict[str, Any]] = []
        #: ⚠️ 用户口径（2026-10-10）：「提交时的规格**严格按照第二步参数配置中填写的规格**」。
        #: SKU 名形如 `颜色分类:白 / 尺寸:1.2m`（多维度用 `/` 分隔）⇒ **逐维度**取 id。
        #: 只认第一个冒号、所有行共用同一个 id 的旧写法，会把多维规格塌成一维
        #: ⇒ 真机报「variants - 当前商品存在重复的规格，请删除重复规格后重试」。
        #: ⚠️⚠️ **只按 `" / "`（空格-斜杠-空格）切维度**，不按裸 `/` 切。
        #: 真机数据反例：`颜色分类:奇遇童话01 / 款式:尺寸/图案任意定制【联系客服】`
        #: —— 值本身**含 `/`**（「尺寸/图案任意定制」）。按裸 `/` 切会把它劈成
        #: `款式:尺寸` + `图案任意定制【联系客服】` 两段 ⇒ **款式值被截成「尺寸」**，
        #: 后半段丢失且不报错（上架后规格值就是错的，属于最坏的静默错）。
        #: 拼接侧（`precheck-publish.skuSpecTextOf` / `spec_combinations`）用的是
        #: `" / "`，两侧必须**同一约定**。
        for part in str(row.get("name") or "").split(" / "):
            head, sep, tail = part.partition(":")
            if not sep:
                head, sep, tail = part.partition("：")
            if sep and head.strip() and tail.strip() and head.strip() in spec_id_by_name:
                dim_id = spec_id_by_name[head.strip()]
                if dim_id:
                    variants.append(pub.build_publish_variant(
                        variant_id=dim_id, variant_name=head.strip(),
                        variant_value=tail.strip()))
                    variant_seq_src.append({
                        "variantId": dim_id, "variantName": head.strip(),
                        "variantValue": tail.strip(),
                    })
        #: 回落：名字拆不出 / 平台没下发 id 时，沿用旧行为（单维度 + 骨架 id）。
        if not variants:
            variant_name, variant_value = package.split_sku_name(str(row.get("name") or ""))
            variant_id = str(row.get("variant_id") or (variant_ids[0] if variant_ids else ""))
            if variant_id:
                variants.append(pub.build_publish_variant(
                    variant_id=variant_id, variant_name=variant_name or "颜色分类",
                    variant_value=variant_value))
                variant_seq_src.append({
                    "variantId": variant_id, "variantName": variant_name or "颜色分类",
                    "variantValue": variant_value,
                })
        #: 规格图：与主图/详情图**同一形状**（真机三处一致）；空则不传该键。
        #: ⚠️ 2026-10-10：优先用「已上传对象」`spec_image_material`（跳过重新上传），
        #: 否则回落 `image_path` 相对路径查 `uploaded`。
        _spec_material = row.get("spec_image_material")
        if _spec_material and isinstance(_spec_material, dict):
            spec_img = pub.build_publish_image({
                "materialId": _spec_material.get("materialId") or "",
                "path": _spec_material.get("path") or "",
                "link": _spec_material.get("link") or _spec_material.get("url") or "",
                "url": _spec_material.get("url") or "",
                "name": _spec_material.get("file_name") or _spec_material.get("name") or "",
                "width": _spec_material.get("width"),
                "height": _spec_material.get("height"),
                "size": _spec_material.get("size"),
                "extension": _spec_material.get("extension") or "",
            })
        else:
            _raw_spec = uploaded.get(str(row.get("image_path") or ""))
            spec_img = pub.build_publish_image(_raw_spec) if _raw_spec else None
        sku_rows.append(pub.build_publish_sku(
            row_number=index,
            price_yuan=row.get("price_yuan"),
            #: 划线价（真机体 `originPrice`）—— 前端价格表「市场价」列，
            #: 字段名 `market_price_yuan`（⚠️ 2026-10-10 修：此前误读 `market_price`）。
            market_price_yuan=row.get("market_price_yuan"),
            stock=row.get("stock"),
            #: 行级启用状态（真机 `available`；默认启用）。
            available=bool(row.get("available", True)),
            #: 条形码 / 商家编码（真机 `skuData[].barcode` / `.erpCode`，2026-10-10 对齐）。
            barcode=row.get("barcode") or None,
            erp_code=row.get("erpCode") or None,
            logistics_plan_id=logistics_plan_id,
            variants=variants,
            spec_image=spec_img,
            #: ⚠️ **发货时间按用户选的模式算**（用户报障 2026-10-15：
            #: 「发货模式配置的是全款预售模式15天内发货，但是发布后变成了现货模式48小时发货」）。
            #: 此前 `delivery_time` **从未被传** ⇒ `build_publish_sku` 里
            #: `or {"type": 4, "time": "48"}` 的**默认值生效** ⇒ 一律现货48小时。
            #: 真机抓包（用户亲手切四种模式，6 次草稿）证明载体是 **SKU 级
            #: `skuData[].deliveryTime`**，`deliveryMode` 恒为 1（不是它）。
            #: ⚠️ 「按规格设置发货时间」时每行各自有文案（`row.delivery_label`）。
            delivery_time=pub.build_delivery_time(
                delivery_mode=str(cfg.get("deliveryMode") or ""),
                ship_time=str(cfg.get("shipTime") or ""),
                presale_kind=str(cfg.get("presaleKind") or ""),
                presale_days=cfg.get("presaleDays") if isinstance(cfg.get("presaleDays"), int) else None,
                ship_aging=str(cfg.get("shipAging") or ""),
                sale_time=str(cfg.get("saleTime") or ""),
                row_ship_aging=str(row.get("delivery_label") or ""),
            ),
        ))

    #: ⚠️⚠️ **`deliveryMode` 是「物流类型」的字段，不是「发货模式」**。
    #:
    #: 真机实测（CDP 直读千帆商品编辑页，2026-10-21）：
    #:   · 「物流配送」的 radio `value = "0"`；
    #:   · 「无物流发货或物流配送」只在类目支持时才出现（另有一档值）。
    #: 而「物流配送」一直是唯一/默认选项，**提交体必须发 0**。
    #:
    #: 旧实现把它当发货模式、填 `1`（骨架默认），平台收到 1 判成
    #: 「无物流发货」⇒ 用户实测报错
    #: **「发布未通过（1 条原因）：deliveryMode — 商品不支持设置无物流发货」**。
    #:
    #: 发货模式的真身是 **SKU 级 `skuData[].deliveryTime`**（见 `build_delivery_time`），
    #: 与 `deliveryMode` 无关 —— 两者此前被混成同一个字段。
    _delivery_mode: int | None = pub.LOGISTICS_TYPE_LOGISTICS
    _logistics = str(cfg.get("logisticsType") or "").strip()
    if _logistics == "无物流发货或物流配送":
        #: 只有该类目**确实支持**「无物流发货」时用户才可能选到它
        #: （前端按类目下发候选），此时才发对应的数字值。
        _delivery_mode = pub.LOGISTICS_TYPE_NOT_LOGISTICS_VALUE

    return pub.build_item_body(
        skeleton=skeleton,
        name=name,
        category_id=category_id,
        #: ⚠️ 用户口径（2026-10-10）：「提交时的规格/属性严格按第二步参数配置中填写的」。
        #: 早期直接抄**骨架**的 `attributes`（= 上一个商品的属性）⇒ 用户在第二步填的
        #: 材质/风格等**从没进过提交体** ⇒ 真机报「attributes - 材质属性必须填写」。
        #: 传了就**以提交的为准**；没传（旧调用点）才回落骨架。
        attributes=attributes if attributes is not None else skeleton.get("attributes"),
        main_images=main_images,
        detail_images=detail_images,
        videos=videos,
        article_no=article_no,
        variant_ids=variant_ids,
        skus=sku_rows,
        variant_sequence=pub.build_variant_sequence(variant_seq_src),
        shipping_template_id=shipping_template_id,
        logistics_plan_id=logistics_plan_id,
        item_short_title=str(skeleton.get("itemShortTitle") or ""),
        #: 2026-10-10：用户第二步配置字段覆盖进提交体（缺则留骨架旧值）。
        delivery_mode=_delivery_mode,
        price_confidential=cfg.get("priceConfidential") if isinstance(cfg.get("priceConfidential"), bool) else None,
        description=str(cfg.get("desc") or "") if cfg.get("desc") else None,
        use_playback=1 if cfg.get("showPlayback") else (0 if isinstance(cfg.get("showPlayback"), bool) else None),
        english_name=str(cfg.get("englishName") or "") if cfg.get("englishName") else None,
    )


def _poll_task(
    api: Any, task_id: str, *, kind: str = "publish"
) -> tuple[bool, list[dict[str, Any]], list[str]]:
    """轮询任务终态 → `(成功, 逐条失败原因, 产生的商品 id 列表)`。

    ⚠️ **两类任务的判据不同**（真机 2026-10-08）：
    * `kind="publish"`（`batch_create_item`）：回执含 `failData` + `itemIds`；
      **成功 = failData 空 且 itemIds 非空** —— ⚠️ **不能**看 `result.success`
      （真机成功时它是 `false`！），看它会把「发布成功」判成失败、
      从而**跳过硬线（下架）**。
    * `kind="operate"`（`batch_update_availability` / `batch_delete_sku`）：
      回执是 `{"failCount":0,"successCount":3,"total":3}` ⇒ 成功 = `failCount==0`。
    """
    #: ⚠️ **先查再睡**（原实现每轮先 `sleep(3)` ⇒ 无论任务多快都要白等 3s；
    #: 发布 + 下架两段串起来就是 6s 起。用户实测「顶级确认发布很慢」有此成分）。
    for attempt in range(_PUBLISH_POLL_TRIES):
        if attempt:
            time.sleep(_PUBLISH_POLL_INTERVAL)
        ok, _msg, payload = api.query_publish_task(task_id)
        if not ok:
            continue
        final, success, fails = pub.parse_task_result(payload)
        if not final:
            continue
        if kind == "operate":
            return pub.parse_operation_success(payload), fails, []
        return success, fails, pub.parse_item_ids(payload)
    return False, [{"index": 0, "name": "轮询超时",
                    "message": f"任务 {task_id} 在 {_PUBLISH_POLL_TRIES} 次轮询内未到终态"}], []


def _page_rows(page: Any) -> list[dict[str, Any]]:
    """从一页商品列表回执里取行（平台/适配器两处键名都兼容）。

    ⚠️ 真机 `search_item_v2` 回 `data.items`；早期代码按 `data.item_list` 读
    （那是另一个端点形状）⇒ 两个键名都试，避免「列表拿到却读成空」的静默错。
    """
    data = (page or {}).get("data") or {}
    return list(data.get("items") or data.get("item_list") or [])


def _find_item_id_by_title(api: Any, title: str) -> str:
    """按唯一标题精确反查刚发布的商品 id（**不用快照差集** —— 曾误删真实商品）。"""
    ok, _msg, page = api.search_items(
        page_no=1, page_size=20, status="all", keyword=title,
        sort_field="create_time", order="desc",
    )
    rows = _page_rows(page)
    for row in rows:
        if title and title in str(row.get("item_name") or row.get("title") or ""):
            return str(row.get("item_id") or "")
    return ""


def publish_item(
    *,
    api: Any,
    name: str,
    category_id: str,
    uploaded: dict[str, dict[str, Any]],
    main_image_paths: list[str],
    detail_image_paths: list[str],
    videos: list[dict[str, Any]] | None = None,
    article_no: str | None = None,
    skus: list[dict[str, Any]],
    variant_ids: list[str],
    item_id: str = "",
    off_shelf_after: bool = True,
    attributes: list[dict[str, Any]] | None = None,
    spec_attributes: list[dict[str, str]] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """**提交商品并（红线）立刻下架** 的完整执行链（设计 24 §4.2）。

    ⚠️ `off_shelf_after` 默认 `True`（保守：不传就下架）。
    ⚠️ 2026-10-11 用户**明确取消红线**：`False` 成为**正式业务值**
    （前端「发布商品」按钮 = 直接上架、发布后不再下架）。
    原句「显式传 `False` 只用于『发布前后对比』类测试，**生产路径恒 True**」
    **已被取代** —— 保留于此以标记口径变更的边界（设计 24 §23）。

    返回 `{published, item_id, off_shelf, task_id, fail_reasons[]}`。
    """
    skeleton = load_item_skeleton(api, item_id=item_id)
    if not skeleton:
        raise PublishError("PUBLISH_SKELETON_REQUIRED", _SKELETON_MISSING)

    #: 店级物流方案 / 运费模板（骨架里常缺 ⇒ 平台报 logisticsPlanId 未填写）。
    #: ⚠️ 两个值来自**两个接口**：`logisticsPlanList` 来自空白 `publish_render`，
    #: `shippingTemplateList` 来自 `getSellerCarriageTemplate` —— 后者不在
    #: `publish_render` 回执里（单独调一次，否则运费模板恒空）。
    fulfillment: dict[str, str] = {}
    try:
        ok_pc, _msg_pc, pc = api.publish_render({"publishType": 1, "sourceType": 1})
        carriage: Any = None
        try:
            ok_ct, _msg_ct, carriage = api.get_seller_carriage_template()
            if not ok_ct:
                carriage = None
        except Exception:  # noqa: BLE001 - 取不到就靠骨架 / 平台默认
            carriage = None
        if ok_pc and isinstance(pc, dict):
            fulfillment = pub.parse_fulfillment_info(pc, carriage)
    except Exception:  # noqa: BLE001 - 取不到就靠骨架 / 平台默认
        fulfillment = {}

    item_body = build_item_from_request(
        skeleton=skeleton,
        name=name,
        category_id=category_id,
        uploaded=uploaded,
        main_image_paths=main_image_paths,
        detail_image_paths=detail_image_paths,
        videos=videos,
        article_no=article_no,
        skus=skus,
        variant_ids=variant_ids,
        fulfillment=fulfillment,
        attributes=attributes,
        spec_attributes=spec_attributes,
        config=config,
    )
    #: ⚠️ 平台规则：`mainSpecEnable=1`（启用规格大图）时**每个 SKU 都要有规格图**
    #: （真机拒因「启用规格大图需上传所有规格的规格大图」）。有 SKU 缺图 ⇒ 关掉，
    #: 让商品仍能发布（规格图是可选增强，不是必须）。
    if any("specImage" not in s for s in item_body.get("skuData") or []):
        item_body["mainSpecEnable"] = 0
    payload = pub.build_publish_payload([item_body])
    #: ⚠️ **转储实际发出的发布体**（2026-10-11）：排查「发布后商品是已下架状态」时，
    #: 必须看**我们到底发了什么**。`itemData[0]` 的顶层键与关键上架相关字段全打出来。
    try:
        _body0 = (payload.get("itemData") or [{}])[0]
        logger.warning(
            "[publish_item] 发布体转储 | 顶层键(%d)=%s",
            len(_body0),
            sorted(_body0.keys()),
        )
        for _k in (
            "isAutoOffShelf", "autoOffShelfTime", "shelfTime", "saleTime",
            "expectedPurchaseTime", "deliveryMode", "stockType", "itemStatus",
            "available", "quantity", "skuData",
        ):
            if _k in _body0:
                _v = _body0[_k]
                if _k == "skuData" and isinstance(_v, list):
                    _v = f"<{len(_v)} 条> 首条可用={(_v[0].get('available') if _v else None)}"
                logger.warning("[publish_item]   发布体.%s = %r", _k, _v)
        #: 临时排查「获取图片信息失败」：dump 首条 sku 的 specImage 全文。
        _skus = _body0.get("skuData") or []
        if _skus and isinstance(_skus[0], dict):
            import json as _json
            logger.warning(
                "[publish_item]   首条 sku.specImage = %s",
                _json.dumps(_skus[0].get("specImage"), ensure_ascii=False),
            )
        logger.warning(
            "[publish_item] 发布体转储结束 | 缺失的上架相关键 = %s",
            [k for k in ("isAutoOffShelf", "autoOffShelfTime", "shelfTime", "expectedPurchaseTime")
             if k not in _body0],
        )
    except Exception as _dump_err:  # noqa: BLE001 —— 转储失败不能影响发布
        logger.warning("[publish_item] 发布体转储失败：%r", _dump_err)
    ok, msg, resp = api.submit_item(payload)
    task_id = str(((resp or {}).get("data") or {}).get("taskId") or "") if isinstance(resp, dict) else ""
    if not ok or not task_id:
        raise PublishError("PUBLISH_SUBMIT_FAILED", str(msg or "提交商品失败")[:300])

    published, fails, item_ids = _poll_task(api, task_id)
    #: 临时排查「获取图片信息失败」：dump 平台 failData 原文。
    if fails:
        import json as _json
        logger.warning("[publish_item] 平台 failData 原文 = %s", _json.dumps(fails, ensure_ascii=False))
    result: dict[str, Any] = {
        "published": published,
        "task_id": task_id,
        "item_id": item_ids[0] if item_ids else "",
        "item_ids": item_ids,
        "off_shelf": False,
        "fail_reasons": fails,
    }
    if not published:
        return result

    #: ⚠️ 红线：发布成功**第一时间下架**（不可拆）。
    #: `itemIds` 从任务回执直接拿（比反查列表更可靠，不受搜索延迟影响）；
    #: 拿不到就按唯一标题反查（**不用快照差集** —— 曾误删真实商品）。
    target_ids = list(item_ids)
    if not target_ids:
        found = _find_item_id_by_title(api, name)
        target_ids = [found] if found else []
    if len(target_ids) == 1:
        result["item_id"] = target_ids[0]
    #: ⚠️ **下架分支的判据必须留痕**（真机排查「点发布商品却仍被下架」的核心一步）：
    #: 这行打出 `off_shelf_after`（开关）与 `target_ids`（有对象可下架）——
    #: 若开关是 False 却进来了，说明代码判据写错；若压根没这行，说明确实没下架。
    logger.warning(
        "[publish_item] 下架判定 | off_shelf_after=%r | target_ids=%s | 将下架=%s",
        off_shelf_after,
        target_ids,
        bool(off_shelf_after and target_ids),
    )
    if off_shelf_after and target_ids:
        ok2, msg2, resp2 = api.set_items_availability(target_ids, available=False)
        off_task = str(((resp2 or {}).get("data") or {}).get("taskId") or "") if isinstance(resp2, dict) else ""
        if off_task:
            off_ok, _, _ = _poll_task(api, off_task, kind="operate")
            result["off_shelf"] = off_ok
        else:
            result["off_shelf"] = bool(ok2)
        if not result["off_shelf"]:
            result["fail_reasons"] = list(fails) + [
                {"index": 0, "name": "下架", "message": str(msg2 or "发布成功但下架失败")[:200]}
            ]
    return result
