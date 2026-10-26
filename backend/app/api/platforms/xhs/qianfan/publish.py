"""千帆 · 商品新增（草稿箱）与发布到线上

由 `qianfan.py`（6057 行 / 65 路由）按域拆分而来，**URL 与前缀一条未改**。
路由前缀由 `__init__.py` 的 `router` 统一挂载（仍是 `/xhs/qianfan`）。
"""
from __future__ import annotations

#: 跨域共用名（`_shared` 转发）—— 由拆分脚本按**自由名分析**生成，
#: 不手写，避免漏名。改动块归属后重跑脚本即可。
from ._shared import (
    APIRouter,
    Any,
    BaseModel,
    Body,
    ConfigDict,
    Depends,
    Field,
    HTTPException,
    Path,
    Session,
    User,
    _DRAFT_MAX_IMAGES,
    _DRAFT_MAX_SKUS,
    _ark_adapter_for,
    _owned_account,
    _ts_text,
    draft_package,
    draft_service,
    get_current_user,
    get_db,
    get_qianfan_api_adapter_factory,
    json,
    logger,
    status,
)

#: 本域独立 router —— 由 `__init__.py` 挂到父 router 下，**不带 prefix**
#:（路径里已含 `/picture-space` 这类段）。
#: ⚠️ 各域**各自**一个，不能共用 —— `include_router` 把父 router 挂到
#:    自己身上会让路由重复/错乱。
router = APIRouter()

class ItemDraftParseRequest(BaseModel):
    """解析商品数据包（**纯本地，零平台请求**）。"""

    model_config = ConfigDict(extra="forbid")

    platform_account_id: int = Field(ge=1, description="账号矩阵里的账号 id（归属校验用）")
    package_path: str = Field(
        min_length=1, max_length=512, description="商品数据包目录的绝对路径"
    )

class ItemDraftSku(BaseModel):
    """新增草稿时的一条 SKU。

    ⚠️ **`price_yuan` 单位是「元」**（不是分）—— 与商品列表筛选的 `price_from`
    （也是元）一致，但**与 `/items` 响应的 `price` 字段（分）相反**。
    命名里带 `_yuan` 就是为了让这个单位不可能被读错（草稿体写成分 ⇒
    价格变成一百倍，且**不报错** —— 静默错值，本链路最贵的坑）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120, description="SKU 名（规格名:规格值）")
    price_yuan: float | None = Field(default=None, gt=0, description="价格（元）；空 = 不填价格")
    #: 划线价（元，前端价格表「市场价」列）。⚠️ 2026-10-10 补：前端 `skus` state
    #: 一直带这个字段，但本模型 `extra="forbid"` 未声明 ⇒ 发布报
    #: `market_price_yuan: Extra inputs are not permitted`。
    market_price_yuan: float | None = Field(default=None, ge=0, description="划线价（元）；空 = 不填")
    #: **行级启用状态**（真机 `skuData[].available`；默认启用）。停用 = 不会被消费者看到。
    available: bool = Field(default=True, description="该 SKU 是否启用（默认启用）")
    stock: int | None = Field(default=None, ge=0, description="库存；空 = 0")
    image_path: str | None = Field(default=None, max_length=512, description="该 SKU 规格图本地路径")
    #: **条形码**（真机 `skuData[].barcode`；bundle 权威字段名，2026-10-10 对齐）。
    #: 空 = 不填（真机用户不填时省略该键）。
    barcode: str | None = Field(default=None, max_length=64, description="条形码；空 = 不填")
    #: **商家编码**（真机 `skuData[].erpCode`；⚠️ 字段名是 `erpCode` 不是 seller_code）。
    erpCode: str | None = Field(default=None, max_length=64, description="商家编码；空 = 不填")
    #: **规格图已上传对象**（2026-10-10 补，用户口径「上传成功后提交用远程 url」）。
    #: 非空时后端 `build_publish_image` 直接用它填 `skuData[].specImage`，跳过重新上传。
    spec_image_material: dict[str, Any] | None = Field(default=None, description="规格图已上传对象")
    #: ⚠️ **该行的发货时间文案**（2026-10-15 补，只「按规格设置发货时间」模式用）。
    #: 真机抓包：该模式下价格库存表多一列「发货时间」，**每行各自一条
    #: `skuData[].deliveryTime`**。值形如 `「现货 / 48小时」`、`「预售 / 15天」`，
    #: 由后端 `build_delivery_time` 解析成 `{type, time}`。
    #: ⚠️ 没有它，用户配的「全款预售15天」会被平台默认值覆盖成「现货48小时」
    #: （用户报障 2026-10-15）。
    delivery_label: str | None = Field(default=None, max_length=64, description="该行发货时间文案")
    #: 现货+预售模式那列「发货时效」（`15天` / `20天` …）。
    ship_aging: str | None = Field(default=None, max_length=32, description="该行发货时效")

class ItemDraftCreateRequest(BaseModel):
    """**新增商品 → 存草稿箱**（设计 23 §3.1）。

    ⚠️ **本模型刻意没有任何「发布 / 上架」字段**（红线）。想发布必须改后端代码，
    不是改一个布尔值。守卫断言字段名不含 `publish` / `available` / `on_shelf` /
    `shelf` —— 因为「加了个开关默认 false」是防线里最弱的一种（改一行就破）。
    """

    model_config = ConfigDict(extra="forbid")

    platform_account_id: int = Field(ge=1)
    #: **叶子类目** id（`isLeaf=true`；由 `/item-drafts/categories` 选出来）。
    category_id: str = Field(min_length=1, max_length=64)
    #: 草稿名（= 平台商品标题位；真机回读落在 `item.itemName`）。
    name: str = Field(min_length=1, max_length=60)
    #: ⚠️ 2026-10-10（用户口径「上传成功后提交/保存草稿均用远程 url」）：图片元素两种形态：
    #: `str` = 相对路径（旧链路，后端上传）；`dict` = 已上传对象（跳过上传直接用）。
    main_images: list[str | dict[str, Any]] = Field(default_factory=list, max_length=_DRAFT_MAX_IMAGES)
    detail_images: list[str | dict[str, Any]] = Field(default_factory=list, max_length=_DRAFT_MAX_IMAGES)
    #: ⚠️ **不设 SKU 条数上限**（用户口径 2026-10-21：「当前项目后端不设置限制」）。
    #: 原为 `max_length=_DRAFT_MAX_SKUS`（200）⇒ 456 行的真实数据包直接 422
    #: （用户实测「点击发布商品异常：Request failed with status code 422」）。
    #: 平台自身对 SKU 组合数的约束由**平台**在提交时判定并回错误，
    #: 我方不应在入口处先行拒绝（截断/拒绝都会让用户看到的行数与实际发布不一致）。
    skus: list[ItemDraftSku] = Field(min_length=1)
    #: 第一步「数据包预检」的暂存 token（设计 25 §6）。非空时
    #: `main_images` / `detail_images` / `skus[].image_path` 按**暂存内相对路径**解析
    #: （`<区域>/<文件名>`）。⚠️ 2026-10-10 补：此前保存草稿缺此字段，前端传
    #: 「主图/主图02.jpg」相对路径 ⇒ 后端 `Path().is_file()` 找不到 ⇒ 图片上传失败。
    precheck_token: str = Field(default="", max_length=64)
    #: **本地数据包文件夹绝对路径**（2026-10-10 补，同发布端点的 `package_path`）。
    #: 非空时**优先**用它拼绝对路径，`precheck_token` 兜底 —— 摆脱 `_LOCAL_REFS`
    #: 内存引用（后端重启即清空、token 失效）。
    package_path: str = Field(default="", max_length=512)
    #: 商品视频已上传对象（2026-10-11 补，真机 `itemData[0].videos[]`；空 = 无视频）。
    videos: list[dict[str, Any]] = Field(default_factory=list)
    #: 货号（真机 `itemData[0].articleNo`，2026-10-11 补；空 = 不传）。
    article_no: str = Field(default="", max_length=64)

@router.post("/item-drafts/parse")
def parse_item_draft_package(
    payload: ItemDraftParseRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**解析商品数据包**（设计 23 §4.1；**纯本地，零平台请求**）。

    读 xlsx（SKU 表）+ 列目录下的图片 / 视频名，回给前端预览。

    ⚠️ **零平台请求**：本端点只看本地磁盘 ⇒ 未登录千帆也能用
    （与 `/items` 读本地台账同口径）。归属校验仍走 `_owned_account`
    （账号得是自己账号，但**不校验千帆登录态**）。

    ⚠️ **如实列 `warnings`**：缺列 / 缺图 / 超 5M / 属性图都会被逐条列出。
    静默过滤掉的东西，用户到千帆页面才会发现少了什么。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    try:
        parsed = draft_package.parse_package(payload.package_path)
    except draft_package.PackageError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "DRAFT_PACKAGE_INVALID", "message": str(error)},
        ) from error

    return {
        "platform_account_id": account.id,
        "package_path": parsed["package_path"],
        "sku_rows": [
            {
                "name": row["name"],
                "price_yuan": row.get("price"),
                "stock": row.get("stock"),
                "image_path": row.get("image_path"),
            }
            for row in parsed["sku_rows"]
        ],
        "main_images": parsed["main_images"],
        "detail_images": parsed["detail_images"],
        "videos": parsed["videos"],
        "suggested_title": parsed["suggested_title"],
        "warnings": parsed["warnings"],
        #: 上限随解析结果一起下发（前端不抄常量）。
        "limits": {"max_images": _DRAFT_MAX_IMAGES, "max_skus": _DRAFT_MAX_SKUS},
    }

@router.get("/item-drafts/categories")
def search_item_draft_categories(
    platform_account_id: int,
    keyword: str = "",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**按关键词搜类目**（设计 23 §3；只读查询，无副作用）。

    ⚠️ **只回 `isLeaf=true` 的类目** —— 商品必须挂在叶子上（平台类目是分层的），
    回叶子之外的选项只会让用户选到一个建不出商品的类目。

    平台端点 `POST /api/edith/product/search_category`（**无需签名**，真机 200）。
    """
    account = _owned_account(db, current_user, platform_account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)
    ok, message, payload = adapter.search_category(str(keyword or "").strip())
    if not ok or not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "DRAFT_CATEGORY_SEARCH_FAILED", "message": message},
        )
    raw = (payload.get("data") or {}).get("categories") or []
    return {
        "platform_account_id": account.id,
        "categories": [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "level": item.get("level"),
                #: 类目是否强制品牌 —— 前端据此提示「该类目需要品牌资质」。
                "need_brand": bool(item.get("needBrand")),
            }
            for item in raw
            if item.get("isLeaf") and item.get("id")
        ],
    }

@router.post("/item-drafts")
def create_item_draft(
    payload: ItemDraftCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**新增商品 → 保存到草稿箱**（设计 23 §4.2）。

    ⚠️ **只到草稿箱，不会上架**（用户红线）。执行链最后调的是
    `create_product_draft`（存档）；平台侧「提交商品」走的是另外两个端点
    （`publish_async` + `publish_async_operate`），本项目**不接入**。

    ## 执行链

    1. 逐张上传图片（permit → PUT → 登记图片空间）；
    2. 组装草稿体（**只含平台接受的五个键**）；
    3. 建草稿（限流时按平台节奏重试）；
    4. **回读校验**（接口 200 但字段被静默丢弃是常态）；
    5. 回执：草稿 id + 上传统计 + 逐条 warnings。

    ## 门禁

    * 归属：`_owned_account`（别人的账号 ⇒ 404）；
    * 登录态：**必须有千帆（ark）令牌**（这是写操作）—— 没令牌 ⇒ 400
      `DRAFT_ARK_REQUIRED`，**不静默降级成本地操作**（那会让人以为存了草稿）；
    * 平台侧还会拦：店铺冻结 / 商品总量上限 / 单日创建上限（如实回执）。

    ⚠️ **同步执行**，不是后台任务：新增的耗时可控，且用户需要当场看到
    「哪张图失败了 / 草稿 ID 是多少 / 哪些字段没落进去」。
    """
    from backend.app.services.account_service import (
        CHANNEL_ARK,
        latest_cookies_for_channel,
    )
    #: ⚠️ 2026-10-11 修「cannot access free variable 'staging'」：原来 `staging` 只在
    #: `elif payload.precheck_token:` 分支内 import，走 `package_path` 分支时未定义，
    #: `_abs_draft` 里用 `staging._safe_rel_path` 就抛 free variable。提到函数顶部无条件 import。
    from backend.app.services import package_staging_service as staging

    account = _owned_account(db, current_user, payload.platform_account_id)
    cookies = latest_cookies_for_channel(db, account.id, CHANNEL_ARK)
    if not cookies:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "DRAFT_ARK_REQUIRED",
                "message": "该账号还没有导入千帆登录态，无法保存草稿。"
                           "请先到「账号矩阵」为该账号导入千帆登录态。",
            },
        )

    seller_id = str(account.seller_id or "")
    if not seller_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "DRAFT_SELLER_MISSING",
                "message": "该账号没有千帆店铺信息（seller_id 为空），请先做一次店铺健康检查。",
            },
        )

    #: 规格字典取自**本店既有商品**（真机来源，设计 23 §4.4）。
    #: 扫不到 ⇒ 空 dict，规格留空 + warnings（**不猜 id**）。
    adapter = _ark_adapter_for(db, account, adapter_factory)
    #: ⚠️ 设计 75 S5（2026-10-27）：原来是「从本地台账挑最近 3 件商品，扫出规格名→
    #: 规格 ID 字典」。台账表已 DROP ⇒ 改为**从平台实时取**同一条判据（该账号最近的商品）。
    #: 取不到 / 平台失败 ⇒ 空 dict（规格留空 + warnings，**不猜 id**，与改造前同口径）。
    ledger_variant_ids: dict[str, str] = {}
    try:
        from backend.app.services import realtime_platform_query_service as rt

        recent = rt.list_products(
            adapter, status="all", page=1, page_size=3, sort_field="create_time", order="desc"
        )
        item_ids = [
            str(row.get("item_id") or "")
            for row in (recent.get("items") or [])
            if str(row.get("item_id") or "")
        ]
        if item_ids:
            ledger_variant_ids = draft_service.collect_ledger_variant_ids(
                api=adapter, item_ids=item_ids
            )
    except Exception as error:  # noqa: BLE001 - 取规格字典失败不阻断建档
        logger.warning("取本店规格字典失败（不影响建档）：%s", error)

    #: 0) **图片根目录解析**（2026-10-10 重构：优先 `package_path`，token 兜底）。
    #: 与发布端点同实现：前端只拿得到「暂存内相对路径」（`主图/主图02.jpg`），
    #: 这里拼成服务器绝对路径。⚠️ 旧实现只依赖 `precheck_token` 的内存引用
    #: `_LOCAL_REFS` —— 后端重启即清空、token 失效（「图片地址错误」历史根因）。
    #: 现在 `package_path` 非空时直接拼绝对路径，token 仅兜底。
    image_root: Path | None = None
    if payload.package_path.strip():
        local_root = Path(payload.package_path.strip())
        if not local_root.is_dir():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "PACKAGE_PATH_INVALID",
                    "message": "数据包目录不存在或已被移动，请重新选择数据包文件夹。",
                },
            )
        image_root = local_root
    elif payload.precheck_token:
        try:
            image_root = staging.resolve_package(current_user.id, payload.precheck_token)
        except staging.StagingError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "PRECHECK_TOKEN_INVALID",
                    "message": f"预检数据包已过期或不属于当前账号：{error}",
                },
            ) from error

    def _abs_draft(image_ref: str) -> str:
        text = str(image_ref or "").strip()
        if not text or image_root is None:
            return text
        return str(image_root / staging._safe_rel_path(text))

    #: ⚠️ 2026-10-10（用户口径「上传成功后保存草稿用远程 url」）：`main_images` /
    #: `detail_images` 元素两种形态：`str` 相对路径（后端上传）或 `dict` 已上传对象
    #: （跳过上传直接用）。这里把 dict 归一成 entry 塞进 `pre_uploaded`，路径列表里
    #: 对应位置放 materialId key。
    pre_uploaded: dict[str, dict[str, Any]] = {}

    def _draft_ref_key(ref: Any) -> str:
        if isinstance(ref, dict):
            entry = {
                "link": str(ref.get("link") or ref.get("url") or ""),
                "url": str(ref.get("url") or ""),
                "path": str(ref.get("path") or ""),
                "extension": str(ref.get("extension") or ""),
                "width": ref.get("width"),
                "height": ref.get("height"),
                "materialId": str(ref.get("materialId") or ""),
                "name": str(ref.get("file_name") or ref.get("name") or ""),
                "size": ref.get("size"),
            }
            key = str(ref.get("materialId") or ref.get("path") or ref.get("link") or "")
            if key:
                pre_uploaded[key] = entry
            return key
        return _abs_draft(str(ref or ""))

    try:
        #: 视频已上传对象 → `build_publish_video` 要的输入（与发布端点同形状，2026-10-11）。
        video_entries = [
            {
                "link": str(v.get("link") or v.get("url") or ""),
                "path": str(v.get("path") or ""),
                "extension": str(v.get("extension") or ""),
                "duration": v.get("duration"),
                "width": v.get("width"),
                "height": v.get("height"),
                "size": v.get("size"),
                "materialId": str(v.get("materialId") or ""),
            }
            for v in payload.videos
        ] if payload.videos else None

        result = draft_service.create_draft(
            api=adapter,
            seller_id=seller_id,
            category_id=payload.category_id,
            name=payload.name,
            main_image_paths=[_draft_ref_key(p) for p in payload.main_images],
            detail_image_paths=[_draft_ref_key(p) for p in payload.detail_images],
            skus=[
                {**item.model_dump(), "image_path": _abs_draft(item.image_path or "")}
                for item in payload.skus
            ],
            ledger_variant_ids=ledger_variant_ids,
            pre_uploaded=pre_uploaded,
            videos=video_entries,
            article_no=payload.article_no or None,
        )
    except draft_service.DraftError as error:
        status_code = (
            status.HTTP_429_TOO_MANY_REQUESTS
            if error.code == "DRAFT_CREATE_FAILED"
            and adapter.is_draft_rate_limited(error.message)
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(
            status_code=status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
    except Exception as error:  # noqa: BLE001 - 兜底：草稿保存的真实异常转结构化，别让前端只见「500 后台服务异常」
        #: ⚠️ 2026-10-11 用户报「保存草稿失败 500 后台服务异常」—— 根因是这里只捕获
        #: `DraftError`，其余异常（`build_item_from_request` 组装 KeyError / 骨架加载失败等）
        #: 冒泡成 FastAPI 默认 500，detail 空泛。兜底 + 落日志 + 转结构化 502。
        logger.exception("保存草稿失败（未预期异常）")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "DRAFT_INTERNAL_ERROR", "message": f"保存草稿时发生内部错误：{error}"},
        ) from error

    return {"platform_account_id": account.id, **result}

@router.get("/item-drafts")
def item_draft_list(
    platform_account_id: int,
    page_no: int = 1,
    page_size: int = 20,
    keyword: str = "",
    sort_field: str = "create_time",
    order: str = "desc",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**草稿箱列表**（设计 23 §10；承接设计 24 草稿箱页）。

    ⚠️ **实时查平台**，不落本地表：草稿的唯一真相源在平台（用户在千帆页面里
    继续填、平台审核、平台限流都在那边）。本地存一份镜像必然漂移，
    而漂移的表现是「本地有、平台没有」—— 用户按哪份都对不上。

    平台端点 `query_product_draft_list_v3`（**无需签名**，真机 200）。

    * `keyword` 按草稿名搜索；`sort_field`（`create_time` / `update_time`）+
      `order`（`asc` / `desc`）排序 —— 参数名取自 bundle `8591.80981179.js`。
    """
    account = _owned_account(db, current_user, platform_account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)
    search_param = {"keyword": keyword.strip()} if keyword.strip() else None
    ok, message, payload = adapter.query_product_draft_list(
        page_no=page_no, page_size=page_size, search_param=search_param,
        sort_field=sort_field, order=order,
    )
    if not ok or not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "DRAFT_LIST_FAILED", "message": message},
        )
    data = payload.get("data") or {}
    drafts = data.get("productDrafts") or []
    return {
        "platform_account_id": account.id,
        "total": int(data.get("total") or 0),
        "drafts": [
            {
                "draft_id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                #: `"0"` = 待提交 / `"-1"` = 已删除（bundle 取证）。
                "status": str(item.get("status") or ""),
                "status_label": "已删除" if str(item.get("status") or "") == "-1" else "待提交",
                "created_at": _ts_text(item.get("createTime")),
                "updated_at": _ts_text(item.get("updateTime")),
                "is_union": int(item.get("productUnionType") or 0) == 1,
                "cover": str(((item.get("image") or {}).get("path") or "")),
            }
            for item in drafts
        ],
    }

class ItemDraftDeleteRequest(BaseModel):
    """**批量删除草稿**（设计 24 §9.4 草稿箱页）。

    ⚠️ **不可恢复**（草稿删了没有撤销入口）。上限前后端同值（`MAX_BATCH_DELETE`）。
    """

    model_config = ConfigDict(extra="forbid")

    platform_account_id: int = Field(ge=1)
    draft_ids: list[str] = Field(min_length=1, max_length=100)

@router.post("/item-drafts/delete")
def delete_item_drafts(
    payload: ItemDraftDeleteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**批量删除草稿**（`batch_delete_draft`，需签名；**不可恢复**）。"""
    account = _owned_account(db, current_user, payload.platform_account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)
    ids = [str(x).strip() for x in payload.draft_ids if str(x).strip()]
    ok, message, resp = adapter.batch_delete_draft(ids)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "DRAFT_DELETE_FAILED", "message": message},
        )
    return {
        "platform_account_id": account.id,
        "deleted": len(ids),
        "raw": resp if isinstance(resp, dict) else {},
    }

class ItemPublishRequest(BaseModel):
    """**提交商品 → 发布到线上**（设计 24 §3.1）。

    ⚠️ 与 `ItemDraftCreateRequest` **相反**：本模型**必然产生一个在售商品**
    （除非发布失败 / 或调用方选择发布后下架）。

    ## `off_shelf_after` 的口径变更（2026-10-11，**用户明确取消红线**）

    原口径（2026-10-08~2026-10-11）：**发布成功第一时间下架** 是红线，
    `off_shelf_after` 默认 `True` 且前端**不得**传 `False`（那是测试钩子）。

    新口径（用户 2026-10-11）：**红线取消** —— 提交按钮拆成两个业务动作：
      · 「发布商品」       = 直接上架，**发布后不再下架** ⇒ 传 `off_shelf_after=False`
      · 「发布并下架商品」 = 发布成功后立刻下架          ⇒ 传 `off_shelf_after=True`

    ⚠️ **默认值仍保留 `True`**（刻意不改）：这样任何漏传的调用方（旧前端、脚本、
       测试）拿到的仍是「立刻下架」这个更保守的行为 —— 拆按钮不该顺带把
       「默认安全」也拆掉。
    """

    model_config = ConfigDict(extra="forbid")

    platform_account_id: int = Field(ge=1)
    category_id: str = Field(min_length=1, max_length=64, description="叶子类目 id")
    name: str = Field(min_length=1, max_length=60, description="商品标题（唯一标题，用于回查）")
    #: ⚠️ 2026-10-10（用户铁律「上传成功后后续所有对该图的使用必须用返回的 url/对象」）：
    #: 图片元素**两种形态并存**：
    #:  · `str` = 相对路径 / 绝对路径（旧链路：草稿箱发布无预检产物，后端上传）；
    #:  · `dict` = **已上传对象**（`picture-space/upload` 返回的 `materialId/path/link/url/…`），
    #:    后端 `build_publish_image` 直接组装，**不再重新上传**。
    main_images: list[str | dict[str, Any]] = Field(default_factory=list, max_length=_DRAFT_MAX_IMAGES)
    detail_images: list[str | dict[str, Any]] = Field(default_factory=list, max_length=_DRAFT_MAX_IMAGES)
    #: ⚠️ **不设 SKU 条数上限**（用户口径 2026-10-21：「当前项目后端不设置限制」）。
    #: 原为 `max_length=_DRAFT_MAX_SKUS`（200）⇒ 456 行的真实数据包直接 422
    #: （用户实测「点击发布商品异常：Request failed with status code 422」）。
    #: 平台自身对 SKU 组合数的约束由**平台**在提交时判定并回错误，
    #: 我方不应在入口处先行拒绝（截断/拒绝都会让用户看到的行数与实际发布不一致）。
    skus: list[ItemDraftSku] = Field(min_length=1)
    #: 本店任一既有商品的 id（取发布体骨架）；空 ⇒ 自动取最近创建的在售商品。
    skeleton_item_id: str = Field(default="", max_length=64)
    #: ⚠️ **红线开关**：默认 True（发布成功立刻下架）。生产路径不得传 False。
    #: 发布成功后是否立即下架。**默认 True**（保守：不传就下架）。
    #: ⚠️ 2026-10-11 起红线取消，`False` 是**正式业务值**（「发布商品」按钮），
    #: 不再是「仅测试用」的钩子 —— 见类 docstring 的口径变更说明。
    off_shelf_after: bool = Field(
        default=True, description="发布成功后是否立即下架（默认 True；False = 发布后保持在售）"
    )

    #: 第一步「数据包预检」的暂存 token（设计 25 §6）。
    #:
    #: 非空时，`main_images` / `detail_images` / `skus[].image_path` 里的值
    #: 按**暂存内相对路径**解析（`<区域>/<文件名>`），由服务端拼成绝对路径
    #: —— 前端拿不到也不该拿到服务器绝对路径。
    #:
    #: ⚠️ **归属校验**：token 会过 `resolve_package(user_id, token)`，
    #: 别人的 token ⇒ 404。前端传绝对路径是**旧的**草稿发布路径，两种写法
    #: 并存（草稿箱发布没有预检产物）。
    precheck_token: str = Field(default="", max_length=64)

    #: **本地数据包文件夹绝对路径**（2026-10-10 补，用户口径「后端重启后图片
    #: 仍能正确解析」）。前端 `pickPackageFolder` 拿到的是 `D:\\...` 绝对路径，
    #: 与 `precheck_token` 的内存引用 `_LOCAL_REFS` 不同 —— **后端一重启
    #: `_LOCAL_REFS` 就清空，token 失效**，图片只能按相对路径盲试。
    #:
    #: 非空时**优先**用它拼绝对路径（`package_path / 相对路径`），
    #: 不再依赖易失的内存引用；`precheck_token` 仅作兜底（老路径）。
    package_path: str = Field(default="", max_length=512)

    #: **第二步配置快照**（用户口径 2026-10-10：「确认发布提交的参数应是第二步配置的
    #: 完整参数」）。发货模式 / 规格 / 开售时间 / 售后 / 英文名 / 描述等。
    #:
    #: ⚠️ 当前只**接收留档**（形状不定，避免前端一多发就 422）。逐项映射进
    #: `itemData` 需按真机发布体字段名做，且要过「发布 → 立即下架」红线验证，
    #: 故单独一步做 —— 先把**数据通路**打通（否则前端传了也白传）。
    config: dict[str, Any] = Field(default_factory=dict)

    #: 商品视频已上传对象（2026-10-11 补，真机 `itemData[0].videos[]`；空 = 无视频）。
    videos: list[dict[str, Any]] = Field(default_factory=list)
    #: 货号（真机 `itemData[0].articleNo`，2026-10-11 补；空 = 不传）。
    article_no: str = Field(default="", max_length=64)

@router.post("/items/publish")
def publish_item_to_online(
    payload: ItemPublishRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**提交商品 → 发布到线上**（设计 24 §4.2）。

    ## ⚠️ 红线（用户 2026-10-08）

    本端点是**真实上架写操作**：成功即产生一个**在售商品**。
    执行链 `提交 → 轮询终态 → 成功 ⇒ 立刻下架` **串行不可拆**
    （`off_shelf_after` 默认 True）。违反红线的写法是「只提交不下架」——
    本端点结构上不存在「只提交」的路径。

    ## 执行链

    1. 逐张上传图片（permit → PUT → 登记图片空间，与草稿同一实现）；
    2. 取**本店既有商品**的发布体骨架（`publish_render`）；
    3. 组装发布体（**`price` 单位是「分」** —— 与草稿体的「元」相反）；
    4. `batch_create_item` → taskId → 轮询终态；
    5. **成功 ⇒ 立刻下架**（红线）；回执含 task_id / item_id / off_shelf / 逐条失败原因。

    ## 门禁

    * 归属：`_owned_account`（别人的账号 ⇒ 404）；
    * 千帆（ark）令牌**必需**（写操作）—— 无 ⇒ 400 `PUBLISH_ARK_REQUIRED`；
    * 本店无既有商品可作骨架 ⇒ 400 `PUBLISH_SKELETON_REQUIRED`。
    """
    from backend.app.services.account_service import (
        CHANNEL_ARK,
        latest_cookies_for_channel,
    )
    from backend.app.services import shop_draft_service as draft_svc
    from backend.app.services import shop_item_publish_service as publish_svc
    #: ⚠️ 2026-10-11 同草稿端点：`staging` 无条件 import（`_abs` 里用 `staging._safe_rel_path`，
    #: 走 package_path 分支时局部 import 不到会抛 free variable）。
    from backend.app.services import package_staging_service as staging

    account = _owned_account(db, current_user, payload.platform_account_id)
    cookies = latest_cookies_for_channel(db, account.id, CHANNEL_ARK)
    if not cookies:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "PUBLISH_ARK_REQUIRED",
                "message": "该账号还没有导入千帆登录态，无法发布商品。"
                           "请先到「账号矩阵」为该账号导入千帆登录态。",
            },
        )
    seller_id = str(account.seller_id or "")
    if not seller_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "PUBLISH_SELLER_MISSING",
                "message": "该账号没有千帆店铺信息（seller_id 为空），请先做一次店铺健康检查。",
            },
        )

    adapter = _ark_adapter_for(db, account, adapter_factory)

    #: 0) **图片根目录解析**（2026-10-10 重构：优先 `package_path`，token 兜底）。
    #:
    #: 前端 `pickPackageFolder` 拿到的是本地文件夹**绝对路径**（`D:\\...`），
    #: 但旧实现只用 `precheck_token` 的内存引用 `_LOCAL_REFS` —— **后端一重启
    #: 该 dict 清空，token 失效**，图片只能按相对路径盲试（历史「图片地址错误」根因）。
    #:
    #: 现在 `package_path` 非空时**直接拼绝对路径**（`package_path / 相对路径`），
    #: 不再依赖易失内存；`precheck_token` 仅作兜底（老路径，草稿箱发布无预检产物时）。
    image_root: Path | None = None
    if payload.package_path.strip():
        #: 本地文件夹路径：校验存在性，非法/不存在 ⇒ 明确报错（不静默回落到相对路径盲试）。
        local_root = Path(payload.package_path.strip())
        if not local_root.is_dir():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "PACKAGE_PATH_INVALID",
                    "message": "数据包目录不存在或已被移动，请重新选择数据包文件夹。",
                },
            )
        image_root = local_root
    elif payload.precheck_token:
        try:
            image_root = staging.resolve_package(current_user.id, payload.precheck_token)
        except staging.StagingError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "PRECHECK_TOKEN_INVALID",
                    "message": f"预检数据包已过期或不属于当前账号：{error}",
                },
            ) from error

    def _abs(image_ref: str) -> str:
        """把图片引用变成**服务器绝对路径**（有 token 就按暂存相对路径解析）。"""
        text = str(image_ref or "").strip()
        if not text or image_root is None:
            return text
        return str(image_root / staging._safe_rel_path(text))

    #: 1) 上传图片（去重；单张失败逐条如实报，不静默丢）。
    sku_dicts = [item.model_dump() for item in payload.skus]
    uploaded: dict[str, dict[str, Any]] = {}
    failed_images: list[dict[str, str]] = []
    #: ⚠️ 2026-10-10（用户铁律「上传成功后后续所有对该图的使用必须用返回的对象」）：
    #: `main_images` / `detail_images` 元素两种形态：
    #:  · `str` = 相对路径 → 后端上传（旧链路）；
    #:  · `dict` = 已上传对象（含 materialId/path/link/…）→ 直接 `build_publish_image`。
    main_image_keys: list[str] = []
    detail_image_keys: list[str] = []
    _paths_to_upload: list[str] = []
    seen: set[str] = set()

    def _register_image_ref(ref: Any) -> str:
        """把单个图片引用（str 相对路径 或 dict 已上传对象）归一成 `uploaded` 的 key。"""
        if isinstance(ref, dict):
            #: 已上传对象 → 归一成 `upload_one_image` 的 entry 形状（字段名对齐），
            #: 存进 `uploaded`；`build_item_from_request` 会统一 `build_publish_image`。
            #: ⚠️ 前端对象字段：`materialId/path/link/url/file_name/width/height/size/extension`。
            entry = {
                "link": str(ref.get("link") or ref.get("url") or ""),
                "url": str(ref.get("url") or ""),
                "path": str(ref.get("path") or ""),
                "extension": str(ref.get("extension") or ""),
                "width": ref.get("width"),
                "height": ref.get("height"),
                "materialId": str(ref.get("materialId") or ""),
                "name": str(ref.get("file_name") or ref.get("name") or ""),
                "size": ref.get("size"),
            }
            key = str(ref.get("materialId") or ref.get("path") or ref.get("link") or "")
            if not key:
                return ""
            uploaded[key] = entry
            return key
        text = str(ref or "").strip()
        if not text:
            return ""
        abs_text = _abs(text)
        if abs_text not in seen:
            seen.add(abs_text)
            _paths_to_upload.append(text)
        return abs_text

    for ref in payload.main_images:
        k = _register_image_ref(ref)
        if k:
            main_image_keys.append(k)
    for ref in payload.detail_images:
        k = _register_image_ref(ref)
        if k:
            detail_image_keys.append(k)
    for s in sku_dicts:
        if s.get("image_path"):
            _register_image_ref(s["image_path"])

    #: ⚠️ **先批量拿齐凭证，再并发上传**（用户口径 2026-10-22：
    #: 「改为批量先获取所有图片的凭证，再上传」）。
    #:
    #: 旧实现每张图各申请一次 `upload_web_permit(count=1)`：
    #: 45 张 = 45 次凭证申请 + 45 次 PUT。用户实测**点击发布耗时 40 秒**，
    #: 慢在凭证那一跳（平台侧最慢的一环），不是带宽或图片大小。
    #: ⇒ 现在：一次（或按平台 14 个/批）拿到全部 fileId，再 8 路并发 PUT。
    #:   凭证申请次数从 N 降到 ceil(N/14)。
    from concurrent.futures import ThreadPoolExecutor

    permits, permit_reason = draft_svc.preallocate_upload_permits(
        adapter, len(_paths_to_upload)
    )
    if permit_reason:
        #: 凭证申请整体失败 ⇒ 全部图片如实记失败（不静默丢图）。
        for path_str in _paths_to_upload:
            failed_images.append({"path": path_str, "reason": permit_reason})
        _paths_to_upload = []
    elif len(permits) < len(_paths_to_upload):
        #: 拿不齐（平台封顶/异常）⇒ 多出来的那些如实报，前 N 张照传。
        for path_str in _paths_to_upload[len(permits) :]:
            failed_images.append(
                {
                    "path": path_str,
                    "reason": f"上传凭证不足（需要 {len(_paths_to_upload)} 个，平台只给了 {len(permits)} 个）",
                }
            )
        _paths_to_upload = _paths_to_upload[: len(permits)]

    def _upload_one(pair: tuple[str, dict[str, Any]]) -> tuple[str, Any, Any]:
        path_str, one_permit = pair
        ok_img, reason, entry = draft_svc.upload_one_image(
            adapter, seller_id=seller_id, file_path=_abs(path_str), permit=one_permit
        )
        return path_str, (ok_img, reason, entry), _abs(path_str)

    if _paths_to_upload:
        pairs = list(zip(_paths_to_upload, permits))
        with ThreadPoolExecutor(max_workers=min(8, len(pairs))) as pool:
            for path_str, (ok_img, reason, entry), abs_path in pool.map(_upload_one, pairs):
                if ok_img and entry:
                    uploaded[abs_path] = entry
                else:
                    failed_images.append({"path": path_str, "reason": str(reason)})

    #: 2) 发布（含红线：成功立刻下架）。
    try:
        #: 视频已上传对象 → `build_publish_video` 要的输入（字段名对齐，2026-10-11）。
        video_entries = [
            {
                "link": str(v.get("link") or v.get("url") or ""),
                "path": str(v.get("path") or ""),
                "extension": str(v.get("extension") or ""),
                "duration": v.get("duration"),
                "width": v.get("width"),
                "height": v.get("height"),
                "size": v.get("size"),
                "materialId": str(v.get("materialId") or ""),
            }
            for v in payload.videos
        ] if payload.videos else None

        #: ⚠️ **全链路详细日志**（2026-10-11）：真机排查「点发布商品却仍被下架」时，
        #: 每一步都留痕，就不必再靠猜（也不必挂调试端口抓包）。
        #: ⚠️ 必须用 **WARNING** 级：uvicorn 默认的 log config 不给应用 logger 开 INFO
        #: （实测前一版写的 `logger.info` **根本没落盘** —— .hermes/logs/app.log 里
        #:   grep `off_shelf_after` 命中 0 行，白费一轮真机）。
        logger.warning(
            "[items/publish] ① 进入端点 | off_shelf_after=%r | account=%s | category=%s | "
            "name=%r | skus=%d | main=%d | detail=%d | videos=%d | precheck_token=%r",
            payload.off_shelf_after,
            payload.platform_account_id,
            payload.category_id,
            payload.name[:40],
            len(payload.skus or []),
            len(payload.main_images or []),
            len(payload.detail_images or []),
            len(payload.videos or []),
            payload.precheck_token[:16],
        )
        result = publish_svc.publish_item(
            api=adapter,
            name=payload.name,
            category_id=payload.category_id,
            uploaded=uploaded,
            main_image_paths=main_image_keys,
            detail_image_paths=detail_image_keys,
            videos=video_entries,
            article_no=payload.article_no or None,
            skus=[{**s, **({"image_path": _abs(s["image_path"])} if s.get("image_path") else {})}
                  for s in sku_dicts],
            variant_ids=[],
            item_id=payload.skeleton_item_id,
            off_shelf_after=payload.off_shelf_after,
            #: **严格按第二步配置**（用户口径 2026-10-10）：属性用用户填的（缺则回落骨架）、
            #: 规格维度 id 用平台下发的 `saleProperties` ⇒ 逐维度组 `variants`。
            attributes=payload.config.get("attributes") or None,
            spec_attributes=payload.config.get("specAttributes") or None,
            #: 2026-10-10：其余第二步配置字段（发货模式/价格保密/详情/回看/英文名/
            #: 物流运费模板）一并透传，此前**只接了 attributes/specAttributes 两个**。
            config=payload.config,
        )
        #: ③ 发布结果（含 `off_shelf` —— **这是后端认为的最终状态**，与用户看到的一致吗？）
        logger.warning(
            "[items/publish] ③ publish_item 返回 | published=%s | item_id=%r | off_shelf=%s | "
            "fail_reasons=%d | 期望下架=%r",
            result.get("published"),
            result.get("item_id"),
            result.get("off_shelf"),
            len(result.get("fail_reasons") or []),
            payload.off_shelf_after,
        )
        #: ⚠️ **一致性自检**：用户要「不下架」时 `off_shelf` 必须是 False。
        #: 若这里打 True（或反过来），日志一眼就能看出是「服务层没遵守开关」。
        if not payload.off_shelf_after and result.get("off_shelf"):
            logger.warning(
                "[items/publish] ⚠️⚠️ 异常：用户要求保持在售（off_shelf_after=False），"
                "但返回值 off_shelf=True —— 服务层没遵守开关！"
            )
    except publish_svc.PublishError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": error.code, "message": error.message},
        ) from error
    except Exception as error:  # noqa: BLE001 - 兜底：把真实异常转结构化，别让前端只看到「Internal Server Error」
        #: ⚠️ 用户报「发布失败 500」但看不到原因（2026-10-10）—— 根因是这里只捕获
        #: `PublishError`，其余异常（SDK 网络错误 / 组装 KeyError / 类型错）直接冒泡成
        #: FastAPI 默认 500，detail 是空泛的 "Internal Server Error"，前端无从自救。
        #: 兜底捕获 + 落日志 + 转结构化 502，前端 `readReason` 才能显示真实原因。
        logger.exception("发布商品失败（未预期异常）")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "PUBLISH_INTERNAL_ERROR",
                "message": f"发布商品时发生内部错误：{error}",
            },
        ) from error

    #: ⚠️ **2026-10-27（设计 75 S5）：「发布成功后入队同步商品到本地台账」整段退场。**
    #: 商品列表 / 详情已改实时查平台，本地 `shop_products` 表已 DROP ⇒
    #: 这次入队既没有执行体、也没有落点。发布结果本身不受影响（发布成功即成功）。

    return {
        "platform_account_id": account.id,
        "uploaded_images": len(uploaded),
        "failed_images": failed_images,
        **result,
    }

class RecommendItemTitleRequest(BaseModel):
    """按主图推荐商品标题（设计 26 · 商品配置第一步）。"""

    platform_account_id: int
    #: 上传后的主图对象（至少一张）。字段与平台 `getRecommendItemTitle`
    #: 的入参同形：link/name/width/height/size/extension/file_id。
    main_images: list[dict] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")

class RecommendCategoryRequest(BaseModel):
    """按标题+主图推荐类目（设计 26 · 商品配置第一步）。"""

    platform_account_id: int
    #: 商品标题（采用后的那个）。
    name: str = Field(min_length=1, max_length=60)
    #: 主图的**签名链接**（平台拿它喂模型）。
    image_url: str = ""
    #: 主图的 uploaderInfoModel（与标题生成同一份材料）。
    uploader_info_model: dict | None = None

    model_config = ConfigDict(extra="forbid")

class ProductPropertyConfigRequest(BaseModel):
    """取类目的**属性配置**（设计 27 · 第二部分）。"""

    platform_account_id: int
    #: 叶子类目 id（`/item-drafts/categories` 选出来的那个）。
    category_id: str = Field(min_length=1, max_length=64)

    model_config = ConfigDict(extra="forbid")

@router.post("/product/property-config")
def product_property_config(
    payload: ProductPropertyConfigRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**取类目的属性配置** —— 第二部分「商品属性」按类目动态下发的来源。

    真机实测（2026-10-08）：`getProductPropertyConfig {categoryId}`，**需签名**。
    实测响应里 `product.productProperty.productPropertyConfigDTO` 只给两个开关
    （`enablePropertyPredict` / `supportPropertyHosting`），**属性项本身**由
    平台的属性预测 / 渲染链路带出 —— 本项目按「取不到就不渲染」处理（缺值口径）。

    ⚠️ **只调这一个只读语义的 action**，不碰发布提交（红线）。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)

    body = {
        "data": json.dumps(
            {
                "publishType": 1,
                "sourceType": 1,
                "actions": [
                    {
                        #: ⚠️ 属性定义**不在** `getProductPropertyConfig`（它只回两个
                        #: 开关）。真机取证（2026-10-08 实测 133KB 响应）：
                        #: `getCategoryProperty {categoryId}` 才回属性定义，
                        #: 落在 `category.categoryPropertyInfo`：
                        #:   descProperties[]  —— 其他属性（20 项）
                        #:   saleProperties[]  —— 销售属性 = 规格类型（6 项）
                        "action": "getCategoryProperty",
                        "param": {"categoryId": payload.category_id},
                    }
                ],
            },
            ensure_ascii=False,
        )
    }
    ok, message, payload_raw = adapter._api()._signed_post(  # noqa: SLF001
        "/api/edith/product/publish_async", body, "/app-item/good/create"
    )
    if not ok or not isinstance(payload_raw, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "PROPERTY_CONFIG_FAILED", "message": message},
        )

    raw_data = payload_raw.get("data")
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except ValueError:
            raw_data = {}
    info = ((raw_data or {}).get("category") or {}).get("categoryPropertyInfo") or {}

    def _normalize(items: Any) -> list[dict[str, Any]]:
        """平台属性项 → 前端契约（**只搬不造**）。

        * `propertyInputType`：1 = 单选（下拉）/ 2 = 多选 / 0 = 文本；
        * ⚠️ **关键属性判据 = `requiredProp`**（真机 2026-10-09 复测）：
          本类目 `importantProp` / `searchImportantProp` **全为 false**，
          而真机页面显示「关键属性 3/4」—— 有值的 3 项 + 必填的 4 项
          恰好 = `requiredProp=true` 的那批（材质/风格/是否组装/门数量）。
          早期按 `importantProp` 取关键属性 ⇒ 关键属性恒为空、全落「其他属性」
          （用户报的「未分两部分」正是这个）。
        * `values[]` 给出候选项与 `valueId`（提交要用 id 而不是名）。
        """
        out: list[dict[str, Any]] = []
        if not isinstance(items, list):
            return out
        for item in items:
            if not isinstance(item, dict):
                continue
            values = [
                {"name": str(v.get("name") or ""), "value_id": str(v.get("valueId") or "")}
                for v in (item.get("values") or [])
                if isinstance(v, dict)
            ]
            input_type = item.get("propertyInputType")
            if values and input_type == 2:
                kind = "multi"
            elif values:
                kind = "select"
            elif item.get("propertyDataType") == 1:
                kind = "bool"
            else:
                kind = "text"
            out.append(
                {
                    "id": str(item.get("propertyId") or ""),
                    "name": str(item.get("propertyName") or ""),
                    "kind": kind,
                    "options": [v["name"] for v in values],
                    "values": values,
                    #: 关键属性 = 必填属性（真机判据）；`importantProp` 作补充信号。
                    "key": bool(item.get("requiredProp")) or bool(item.get("importantProp")),
                    "required": bool(item.get("requiredProp")),
                    #: ⚠️ 2026-10-10：是否允许**自定义输入**新值（真机 `customizable`）。
                    #:  `customizable=false` ⇒ 只能从 `values` 候选里选（如「材质」）；
                    #:  `customizable=true` ⇒ 下拉可输入新值（如「颜色分类/尺寸」）。
                    "customizable": bool(item.get("customizable")),
                    "sequence": item.get("sequence"),
                    "units": item.get("units") or [],
                }
            )
        #: 平台给了 `sequence` 就按它排（页面顺序与平台一致）。
        out.sort(key=lambda x: (x["sequence"] is None, x["sequence"] or 0))
        return out

    desc = _normalize(info.get("descProperties"))
    sale = _normalize(info.get("saleProperties"))

    #: ⚠️ 用户口径（2026-10-09）：「物流类型应和类目有直接关系」。
    #: 真机 bundle（`8091.*.js`）日志：`logisticsTypeOptions` 只有两类分支 ——
    #:  `supportNoLogistics`（含编辑模式已是「无物流」/ 无发货模式报错 / 已选无物流）
    #:  时给 **「物流配送」**；否则给「无物流发货或物流配送」。
    #: 该类目是否支持「无物流」由平台按类目下发（`virtualCategoryTypes`：虚拟卡券、
    #:  同城交易性商品等）。**离线证据里 `getCategoryProperty` 未带该字段** ⇒
    #:  返回 `null` 表示「平台没给」——前端据此**退回两个候选都展示**（不造假）。
    support_no_logistics = info.get("supportNoLogistics")
    if support_no_logistics is None:
        support_no_logistics = ((info.get("category") or {}).get("supportNoLogistics"))

    return {
        "platform_account_id": account.id,
        "category_id": payload.category_id,
        #: 其他属性（真机 `descProperties[20]`）。
        "desc_properties": desc,
        #: 销售属性 = 规格类型（真机 `saleProperties[6]`）。
        "sale_properties": sale,
        #: 关键属性 = `importantProp=true` 的那批（供页面分组）。
        "key_properties": [p for p in desc if p["key"]],
        "conditional_required_rules": info.get("conditionalRequiredRules") or [],
        #: 该类目是否支持「无物流发货」（`True`/`False`/`None`=平台未下发）。
        "support_no_logistics": support_no_logistics,
    }

@router.post("/product/after-sale-rule", summary="售后服务规则（只读，按类目）")
def product_after_sale_rule(
    payload: ProductPropertyConfigRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """取类目的**售后服务规则** —— 「七天无理由是否可编辑」的判据（`getSupportAfterSale`）。

    ⚠️ 真机实测（2026-10-09，八个类目对过）：`mustSupport` 按类目不同 ——
    `true` = 平台强制支持（勾选不可取消），`false` = 用户可自选。
    规则集本身也按类目变（如服务券给「未核销随时退 + 超期自动退」），
    ⇒ 前端按本接口的 `name` / `type` 渲染，**不写死单条**。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)
    ok, message, payload_raw = adapter.get_support_after_sale(payload.category_id)
    if not ok or not isinstance(payload_raw, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "AFTER_SALE_RULE_FAILED", "message": message},
        )
    raw_data = payload_raw.get("data")
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except ValueError:
            raw_data = {}
    info = ((raw_data or {}).get("fulfillmentInfo") or {})
    info = info if isinstance(info, dict) else {}
    rules = info.get("afterSaleRule") or []
    return {
        "platform_account_id": account.id,
        "category_id": payload.category_id,
        #: 每条：{name, type, default_support, must_support, ...}
        "rules": [
            {
                "name": str(r.get("name") or ""),
                "type": str(r.get("type") or ""),
                "default_support": bool(r.get("defaultSupport")),
                #: ⚠️ 平台强制支持 ⇒ 前端勾选禁用（这是「是否可编辑」的唯一判据）。
                "must_support": bool(r.get("mustSupport")),
            }
            for r in rules
            if isinstance(r, dict)
        ],
    }

class GenerateShortTitleRequest(BaseModel):
    """生成导购短标题（设计 27 · 第二部分）。"""

    platform_account_id: int
    #: 商品标题（短标题由它派生）。
    name: str = Field(min_length=1, max_length=60)
    #: ⚠️ 叶子类目 id —— 真机抓包 `generateItemShortTitle` 的入参是
    #: `{title, categoryId}`，**没有** `mainImages`（2026-10-09 复测）。
    #: 缺它平台会返回空短标题。
    category_id: str = Field(default="", max_length=64)

    model_config = ConfigDict(extra="forbid")

class PredictPropertiesRequest(BaseModel):
    """属性智能生成请求。"""

    model_config = ConfigDict(extra="forbid")

    account_id: int
    category_id: str
    title: str = ""
    #: ⚠️ 主图对象数组 —— 真机抓包 `predictProductProperty` 的入参含 `imageUrls`，
    #: **不给图平台恒回空数组**（2026-10-09 复测：无图 = `predictItemProperties: []`）。
    #: 字段与平台同形：`{key,type,materialId,name,width,height,size,extension,
    #: path,link,uploaderInfoModel,errors}`。
    image_urls: list[dict] = Field(default_factory=list)

@router.post("/product/predict-properties", summary="属性智能生成（只读预测）")
def product_predict_properties(
    payload: PredictPropertiesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """按类目 + 标题 + 主图**预测属性值**（千帆 `predictProductProperty`）。

    ⚠️ **必须带主图**（`imageUrls`）—— 无图平台恒回空数组（2026-10-09 复测），
    错误/缺失入参会表现为「平台未给出可填充的属性值」，看起来像没做。
    """
    account = _owned_account(db, current_user, payload.account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)
    ok, msg, body = adapter.predict_product_property(
        payload.category_id,
        payload.title or "",
        image_urls=payload.image_urls or None,
    )
    if not ok or not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "PREDICT_PROPERTIES_FAILED", "message": msg},
        )
    data = body.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    data = data if isinstance(data, dict) else {}
    #: 平台把预测属性放进 `data.product.productProperty.predictItemProperties`（真机
    #: 2026-10-09 实测）；兼容旧候选 `properties` / `propertyList`。按 propertyName 归一成 {名: 值}
    product = data.get("product") or {}
    product = product if isinstance(product, dict) else {}
    pp = product.get("productProperty") or {}
    pp = pp if isinstance(pp, dict) else {}
    raw = (
        pp.get("predictItemProperties")
        or data.get("properties")
        or data.get("propertyList")
        or []
    )
    out: dict = {}
    #: ⚠️ 真机每条是 `{propertyId, propertyName, propertyValueList:[{value,valueId}]}`
    #: —— 值是**数组**不是标量（2026-10-09 复测）。早期按 `propertyValue` / `value`
    #: 标量取 ⇒ 恒取不到 ⇒ 前端永远提示「平台未给出可填充的属性值」。
    if isinstance(raw, list):
        for it in raw:
            if not isinstance(it, dict):
                continue
            name = it.get("propertyName") or it.get("name")
            if not name:
                continue
            cand = it.get("propertyValueList")
            val: Any = None
            if isinstance(cand, list):
                vals = [
                    str(v.get("value"))
                    for v in cand
                    if isinstance(v, dict) and v.get("value") not in (None, "")
                ]
                if vals:
                    #: 多条候选取第一条（平台按 fillType 给的是「建议值」）。
                    val = vals[0] if len(vals) == 1 else vals
            if val is None:
                val = it.get("propertyValue") or it.get("value")
            if val not in (None, ""):
                out[name] = val
    elif isinstance(raw, dict):
        out = {k: v for k, v in raw.items() if v not in (None, "")}
    return {"properties": out}

@router.post("/product/shipping-templates", summary="物流模板候选（只读）")
def product_shipping_templates(
    account_id: int = Body(..., embed=True),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """取**物流模板候选**（千帆 `getSellerCarriageTemplate`）。"""
    account = _owned_account(db, current_user, account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)
    ok, msg, payload = adapter.get_seller_carriage_template()
    if not ok or not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "SHIPPING_TEMPLATE_FAILED", "message": msg},
        )
    data = payload.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    data = data if isinstance(data, dict) else {}
    #: 物流模板数据在 `data.fulfillmentInfo` 里（不是 `data` 顶层，真机 2026-10-09 实测）。
    fulfillment = data.get("fulfillmentInfo") or {}
    fulfillment = fulfillment if isinstance(fulfillment, dict) else {}
    items = fulfillment.get("shippingTemplateList") or []
    default = fulfillment.get("defaultShippingTemplate") or {}
    return {
        "templates": [
            {"template_id": it.get("templateId"), "name": it.get("templateName")}
            for it in items
            if isinstance(it, dict)
        ],
        "default_template_id": default.get("templateId") if isinstance(default, dict) else None,
    }

@router.post("/product/logistics-templates", summary="物流计划（发货仓）候选（只读）")
def product_logistics_templates(
    account_id: int = Body(..., embed=True),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """取**物流计划（发货仓）候选**（千帆 `publish_render` 空白表单）。

    真机 2026-10-09 实测：`data.fulfillmentInfo.logisticsPlanList` 是「物流模板」
    下拉的数据源（每项 `planId` / `name`（"发货仓"）/ `logisticsMode`（"内贸"）…），
    与 `shipping-templates`（运费模板 `shippingTemplateList`）是**两个不同数据源**。
    """
    account = _owned_account(db, current_user, account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)
    ok, msg, payload = adapter.get_logistics_templates()
    if not ok or not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "LOGISTICS_TEMPLATE_FAILED", "message": msg},
        )
    data = payload.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    data = data if isinstance(data, dict) else {}
    fulfillment = data.get("fulfillmentInfo") or {}
    fulfillment = fulfillment if isinstance(fulfillment, dict) else {}
    items = fulfillment.get("logisticsPlanList") or []
    return {
        "templates": [
            {
                "plan_id": it.get("planId"),
                "name": it.get("name"),
                "logistics_mode": it.get("logisticsMode"),
            }
            for it in items
            if isinstance(it, dict)
        ],
        #: 千帆空白表单不下发「默认计划」，前端取第一个（或留空）。
        "default_plan_id": None,
    }

@router.post("/product/short-title")
def generate_short_title(
    payload: GenerateShortTitleRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**生成导购短标题** —— 与商品标题同款口径：能自动生成的就走平台。

    真机（2026-10-08）：发布页「导购短标题」右侧有「智能生成」按钮，
    对应 `publish_async` 的 `generateItemShortTitle`（**需签名**）。

    ⚠️ **只调这一个生成文案的 action**，不碰发布提交（红线）。
    ⚠️ 取不到就是空串（**缺值口径**：不造假、不拿商品标题截断冒充）。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)

    ok, message, payload_raw = adapter.generate_item_short_title(
        payload.name, category_id=payload.category_id
    )
    if not ok or not isinstance(payload_raw, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "SHORT_TITLE_FAILED", "message": message},
        )

    #: `data` 是字符串化 JSON（与整个 `publish_async` 同形）。
    raw_data = payload_raw.get("data")
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except ValueError:
            raw_data = {}

    #: ⚠️ 真机字段落点（2026-10-09 复测）：
    #: `data.product.productTitle.itemShortTitle.generatedShortTitle`。
    #: 早期版本按 `itemShortTitle` / `shortTitle` **顶层键**取 —— 那些键在真机响应里
    #: 恒不存在（`itemShortTitle` 是个**对象**不是字符串）⇒ 短标题恒为空。
    #: 这里按真机路径取，并保留 `shortTitle` 作为平台改版时的次选。
    short = ""
    node = (
        ((raw_data or {}).get("product") or {})
        .get("productTitle", {})
        .get("itemShortTitle", {})
    )
    if isinstance(node, dict):
        for key in ("generatedShortTitle", "itemShortTitle", "shortTitle"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                short = value.strip()
                break
    if not short:
        node = (raw_data or {}).get("productTitle", {})
        if isinstance(node, dict):
            inner = node.get("itemShortTitle")
            if isinstance(inner, dict):
                value = inner.get("generatedShortTitle")
                if isinstance(value, str) and value.strip():
                    short = value.strip()
    if not short:
        for key in ("generatedShortTitle", "shortTitle"):
            value = (raw_data or {}).get(key)
            if isinstance(value, str) and value.strip():
                short = value.strip()
                break

    return {"platform_account_id": account.id, "short_title": short}

@router.post("/product/recommend-category")
def recommend_product_category(
    payload: RecommendCategoryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**按标题+主图推荐类目** —— 千帆发布页标题采用后自动带出类目的那条 action。

    真机取证（2026-10-08）：点「立即使用」后页面立刻出现「已选类目」
    且**零类目请求**；bundle 逆出真正的调用是 `getRecommendCategory`
    （入参 `{name, imageUrl, uploaderInfoModel}`，**要签名**）。

    响应 `data.recommendCategories[]` → 归一化成
    `[{category_id, name, score, marketable, path[]}]`。

    ⚠️ **只调这一个推荐 action**，不碰发布提交（红线）。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)

    ok, message, payload_raw = adapter.recommend_category(
        payload.name,
        image_url=payload.image_url,
        uploader_info_model=payload.uploader_info_model,
    )
    if not ok or not isinstance(payload_raw, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "RECOMMEND_CATEGORY_FAILED", "message": message},
        )

    #: ⚠️ `data` 是**字符串化 JSON**（与整个 `publish_async` 同形，真机实测）。
    raw_data = payload_raw.get("data")
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except ValueError:
            raw_data = {}
    if not isinstance(raw_data, dict):
        raw_data = {}

    #: ⚠️ 候选**不在** `recommendCategories` —— 真机（2026-10-08）实测落在
    #: `category.recommendCategoryInfo.extraMap.algoOriginRes`，且它本身是
    #: **字符串化的 JSON 数组**：
    #:   [{"cascade_category_name_pred": "住宅家具-桌类-书桌",
    #:     "leaf_category_id_pred": "65f997323e946300016b2fa8",
    #:     "leaf_category_name_pred": "书桌",
    #:     "leaf_category_score_pred": "0.656069"}, …]
    category_info = (raw_data.get("category") or {}).get("recommendCategoryInfo") or {}
    extra = (category_info.get("extraMap") or {}) if isinstance(category_info, dict) else {}
    raw_list = extra.get("algoOriginRes") if isinstance(extra, dict) else None

    #: 兼容「结构变了直接给数组」的情形（不赌单一形状）。
    if raw_list is None:
        raw_list = raw_data.get("recommendCategories")
    if isinstance(raw_list, str):
        try:
            raw_list = json.loads(raw_list)
        except ValueError:
            raw_list = []
    if not isinstance(raw_list, list):
        raw_list = []

    out: list[dict[str, Any]] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        leaf_name = str(item.get("leaf_category_name_pred") or "")
        leaf_id = str(item.get("leaf_category_id_pred") or "")
        cascade = str(item.get("cascade_category_name_pred") or "")
        #: `cascade_category_name_pred` 用 `-` 连层级（住宅家具-桌类-书桌）。
        path = [seg for seg in cascade.split("-") if seg] if cascade else []
        score_raw = item.get("leaf_category_score_pred")
        try:
            score: float | None = float(score_raw) if score_raw is not None else None
        except (TypeError, ValueError):
            score = None
        out.append(
            {
                "category_id": leaf_id,
                "name": leaf_name,
                "score": score,
                "path": path,
            }
        )

    #: 平台自己的顺序未必按分排序 ⇒ 显式按 score **降序**（None 排最后）。
    out.sort(key=lambda c: (c["score"] is None, -(c["score"] or 0.0)))

    return {"platform_account_id": account.id, "categories": out}

@router.post("/product/recommend-title")
def recommend_product_title(
    payload: RecommendItemTitleRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**按主图生成商品标题** —— 千帆发布页上传主图后自动触发的同一个 action。

    真机抓包（2026-10-08）确认平台走的是 `publish_async` 里的
    `getRecommendItemTitle`，入参是**带签名链接的主图对象数组**。
    平台把图喂给模型 ⇒ 标题与图相符；只传文本线索会生成与图无关的标题。

    ⚠️ **只调这一个 action**，不碰发布链路的提交类 action（红线）。
    ⚠️ 需要该账号有 ark 令牌（400 `ARK_SESSION_REQUIRED`），不借会话。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)

    #: 把前端给的精简对象补成**平台认识**的形状（缺的字段不编造，按需省略）。
    images: list[dict] = []
    for raw in payload.main_images:
        link = str(raw.get("link") or raw.get("url") or "").strip()
        if not link:
            continue
        item: dict[str, Any] = {
            "type": "uploadImage",
            "name": str(raw.get("name") or ""),
            "link": link,
            "status": "success",
        }
        for key, src_key in (("width", "width"), ("height", "height"), ("size", "size")):
            value = raw.get(src_key)
            if isinstance(value, int) and value > 0:
                item[key] = value
        ext = str(raw.get("extension") or "").strip()
        if ext:
            item["extension"] = ext
        file_id = str(raw.get("file_id") or "").strip()
        if file_id:
            item["path"] = file_id
            item["uploaderInfoModel"] = {
                "url": link,
                "bizName": "ark",
                "scene": "goods_image",
                "fileId": file_id,
                "isSecret": True,
                "cloudType": 4,
            }
        images.append(item)

    if not images:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "RECOMMEND_TITLE_NO_IMAGE", "message": "没有可用于生成的图片链接"},
        )

    ok, message, payload_raw = adapter.recommend_item_title(images)
    if not ok or not isinstance(payload_raw, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "RECOMMEND_TITLE_FAILED", "message": message},
        )

    #: ⚠️ `data` 是**字符串化 JSON**（真机实测，与整个 `publish_async` 同形）。
    raw_data = payload_raw.get("data")
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except ValueError:
            raw_data = {}
    if not isinstance(raw_data, dict):
        raw_data = {}

    #: 标题在 `product.productTitle.productRecommendTitleDTO.recommendItemTitle`
    #: （2026-10-08 真机实测路径）。逐层取，缺一层就回空串 ——
    #: **缺值口径**：不造假、不拿别的字段冒充。
    dto = (
        (raw_data.get("product") or {})
        .get("productTitle", {})
        .get("productRecommendTitleDTO", {})
    )
    title = ""
    if isinstance(dto, dict):
        value = dto.get("recommendItemTitle")
        if isinstance(value, str):
            title = value.strip()

    return {
        "platform_account_id": account.id,
        "title": title,
        #: 回传**用了哪张图**生成（便于前端核对是否与主图相符）。
        "source_image": (dto.get("recommendItemTitleSourceImage") if isinstance(dto, dict) else None)
        or {},
    }
