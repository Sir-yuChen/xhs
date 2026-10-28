"""千帆 · 商品与店铺（绑定 / 巡检 / 商品列表 / 同步 / 编辑 / 润色 / 导出 / 详情图）

由 `qianfan.py`（6057 行 / 65 路由）按域拆分而来，**URL 与前缀一条未改**。
路由前缀由 `__init__.py` 的 `router` 统一挂载（仍是 `/xhs/qianfan`）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: 跨域共用名（`_shared` 转发）—— 由拆分脚本按**自由名分析**生成，
#: 不手写，避免漏名。改动块归属后重跑脚本即可。
from ._shared import (
    APIRouter,
    Any,
    BUSINESS_KEY_PRODUCT_DETAIL_IMAGE,
    BaseModel,
    CandidateQuery,
    ConfigDict,
    Depends,
    Field,
    HTTPException,
    ImageAiClient,
    MAX_PAGE_SIZE,
    OpenAICompatibleTextClient,
    Optional,
    PlatformAccount,
    Session,
    Task,
    User,
    _account_shop_summary,
    _datetime_from,
    _default_detail_image_prompt,
    _epoch_millis,
    _first_text,
    _owned_account,
    _price_fen,
    _render_detail_image_template,
    _resolve_style_reference,
    _shop_display_name,
    _text_model_context,
    _ts_text,
    check_account_health,
    datetime,
    detail_image_prompt,
    func,
    get_current_user,
    get_db,
    get_image_ai_client,
    get_qianfan_api_adapter_factory,
    get_shop_state,
    get_text_ai_client,
    has_ark_session,
    json,
    list_candidates,
    operate_service,
    quote,
    select,
    shanghai_now,
    shop_products_service,
    status,
)
from backend.app.core.perm_deps import require_perm

#: 本域独立 router —— 由 `__init__.py` 挂到父 router 下，**不带 prefix**
#:（路径里已含 `/picture-space` 这类段）。
#: ⚠️ 各域**各自**一个，不能共用 —— `include_router` 把父 router 挂到
#:    自己身上会让路由重复/错乱。
router = APIRouter()

class ShopAccountRequest(BaseModel):
    """按账号查询商品相关的请求（v2.13：补 `extra="forbid"`）。

    ⚠️ 与 `ShopItemsRequest` 同一条纪律：Pydantic 默认 `extra="ignore"` 会把未知字段
    **静默丢弃**（不报错、不写日志），表现与「代码写错」完全一样（REF §B 实踩过）。
    加 forbid ⇒ 新旧不匹配在第一跳就 422 炸出声。
    """

    platform_account_id: int

    model_config = ConfigDict(extra="forbid")

class ItemNotesRequest(BaseModel):
    """反查某个商品挂了哪些笔记。"""

    platform_account_id: int
    item_id: str = Field(min_length=1, max_length=64)

    model_config = ConfigDict(extra="forbid")


class NoteDetailRequest(BaseModel):
    """单篇笔记详情（2026-10-27，实时）。

    用户口径：「笔记数据不再落库，应实时查询，所有笔记全部应是实时数据」。
    正文只能按单篇取（列表接口 23 个字段里没有正文），所以前端**切到哪篇调哪篇**。
    """

    platform_account_id: int
    note_id: str = Field(min_length=1, max_length=64)

    model_config = ConfigDict(extra="forbid")

class ItemDetailRequest(BaseModel):
    """商品详情弹窗的数据请求（**实时查千帆**：`search_item_v2` + `get_skus_by_item_ids`）。"""

    platform_account_id: int
    item_id: str = Field(min_length=1, max_length=64)

    model_config = ConfigDict(extra="forbid")

NOTE_COUNT_OPS: tuple[str, ...] = ("lt", "lte", "eq", "gte", "gt")

MAX_BATCH_DELETE: int = 200

MAX_OPERATE_ITEMS: int = 200

class ShopItemsRequest(BaseModel):
    """商品列表查询条件。

    **参数名与语义全部来自千帆商品列表页自己发的请求**（2026-09-19 CDP 抓包），
    详见 `apis/xhs_qianfan_apis.py::search_items` 的 docstring。

    单位约定：前端与用户看到的都是**元**，千帆要的是**分** —— 转换只在
    `_price_fen()` 一处发生（避免「有的地方乘了 100、有的地方没乘」这类静默错值）。

    ⚠️ **`extra="forbid"`（v1.36 起）**：未知字段直接 422，不再静默丢弃。
    教训是实打实的 —— 挂链笔记数筛选上线时，跑着的后端还是**旧进程**（模型里
    没有 `note_count` 字段），Pydantic 默认把未知字段丢掉，于是前端填了条件、
    后端当没收到，界面表现为「筛选没生效」，**没有任何报错**。删除这个语义漏洞：
    新旧不匹配必须在第一跳就炸出声（与 §33 那条「不支持的取值静默退化成默认 =
    红线」同一纪律）。
    """

    model_config = ConfigDict(extra="forbid")

    platform_account_id: int
    page_no: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    #: 关键词：商品名称 / 商品 ID / 商品货号 / 商家编码，多条用逗号隔开（平台口径）。
    keyword: str = Field(default="", max_length=200)
    #: `all` / `on_shelf` / `off_shelf` / `frozen` / `auditing` /
    #: `audit_rejected` / `audit_passed` / `rectify`（见 SDK 的 `QIANFAN_ITEM_STATUSES`）。
    status: str = "on_shelf"
    price_from: float | None = Field(default=None, ge=0)
    price_to: float | None = Field(default=None, ge=0)
    stock_from: int | None = Field(default=None, ge=0)
    stock_to: int | None = Field(default=None, ge=0)
    sales_from: int | None = Field(default=None, ge=0)
    sales_to: int | None = Field(default=None, ge=0)
    #: 挂链笔记数筛选（v1.34）：比较符取 `NOTE_COUNT_OPS`，**默认 `lt`（小于）**；
    #: `note_count` 为空 ⇒ **不筛**（不是「小于 0」）。口径 = `note_goods` **关系表**
    #: （与列表「挂链笔记」列同一口径，设计 13 §33.8 / §32.4），零平台请求。
    note_count_op: str = "lt"
    note_count: int | None = Field(default=None, ge=0)
    #: 创建时间区间（千帆「创建时间」筛选）。接受 `YYYY-MM-DD`（按天：结束日自动含当天 23:59:59）
    #: 或 `YYYY-MM-DD HH:MM:SS`，也接受 Unix **秒**整数（千帆原生口径，见 SDK docstring 的坑 3）。
    create_time_from: str | int | None = None
    create_time_to: str | int | None = None
    sort_field: str = "create_time"
    order: str = "desc"
    #: 渠道商品筛选（千帆「商品类型」三档里的「渠道品」）。
    #: 平台请求里这个字段**恒发**且默认 `false`（= 只看普通商品，千帆自己也是这么发的），
    #: 所以默认值必须与平台一致，不能写成 `None` 让 SDK 省掉字段。
    is_channel: bool = False


class NoteCleanupCandidatesRequest(BaseModel):
    """「无流量笔记」候选查询条件（**全部可选** —— 条件可配置，不是写死全库）。

    判定口径：`browse_num`（阅读）且 `note_pay_order_cnt`（支付订单数）且
    `note_pay_gmv`（支付金额）**三项在千帆原文里都明确存在、且都 ≤ 阈值**。

    **缺值不是 0**（硬约束 ⑤）：`entry_json` 里缺某个键 → 这条笔记**不进候选**，
    因为「读不到」与「读到 0」是两件事，不能造一个 0 出来。只有三项**都明确是 0**
    才算无流量。

    为什么查询本身不新增平台请求：三项指标在同步时已随 `entry_json` 整条落库，
    本地就有，再打千帆只增风控面（且会把一个只读查询变成有副作用的接口）。
    """

    platform_account_id: int
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    #: 发布时间区间（千帆存的是**毫秒**时间戳）。空 = 不限。
    publish_from: str | None = Field(default=None, max_length=32)
    publish_to: str | None = Field(default=None, max_length=32)
    #: 是否把 30 日阅读窗口纳入筛选（默认关：它是附加条件，不参与「无流量」判定）。
    include_30d: bool = False
    #: 30 日阅读上限（仅在 `include_30d=true` 时生效；`None` → 按 0 处理）。
    browse_30d_max: float | None = Field(default=None, ge=0)
    #: 三项阈值（默认全 0 = 三项都明确是 0 才算无流量）。
    browse_num_max: float | None = Field(default=None, ge=0)
    pay_order_cnt_max: float | None = Field(default=None, ge=0)
    pay_gmv_max: float | None = Field(default=None, ge=0)

    model_config = ConfigDict(extra="forbid")

@router.post("/bind", include_in_schema=False)
def bind_shop_removed():
    """**已退场**（ADR-021 / 设计 18，2026-09-30）。

    原 `POST /qianfan/bind` 校验 Cookie 可用 → 建店铺实体 → 写 1:1 关系 → 存凭据。
    关系（`shop_id_fk`）与店铺实体一起退场后，**没有「绑定」这个动作了**：
    千帆能力现在完全由「本账号有没有可用的 `ark` 会话」决定 —— 用户要开通，就在
    账号矩阵里给该账号**导入千帆（ark）登录态**，那是凭据操作。

    `include_in_schema=False` + 显式 `410`：端点曾经存在但已不再是能力，所以
    ① 不再进 OpenAPI（后端端点计数只算真实能力）；② 留着老的 `POST /bind` 路径
    给老前端一个**能读懂的答复**，而不是 `404` 猜半天。
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "QIANFAN_BINDING_REMOVED",
            "message": "「绑定千帆店铺」已随店铺实体一并退场（ADR-021）。"
                       "千帆能力现在由该账号自己的 ark 登录态决定，请在账号矩阵导入其千帆登录态。",
        },
    )

@router.post("/health-check")
def check_shop_health(
    payload: ShopAccountRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """**立即巡检**千帆登录态（T-31）：一次只读探活 `GET /api/edith/seller/info/v2`。

    与定时巡检走**同一个** `check_account_health()`，差别只是「不判到期」——
    页面上点一次就得到确定答案，不必等下一个周期。

    * 只读：探测失败也只是把账号的千帆状态标 `invalid`，**不动已同步的映射**（不毁数据）；
    * 越权 / 软删：账号不属于当前用户或已删除 → 404（`get_owned_active_account` 统一承担）；
    * **没有 `ark` 会话 → 404 `ARK_SESSION_REQUIRED`**（ADR-021：判据是令牌，不是绑店）。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    if not has_ark_session(db, account.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "ARK_SESSION_REQUIRED",
                "message": "该账号没有可用的千帆（ark）登录态，先在账号矩阵导入其千帆登录态再巡检。",
            },
        )

    result = check_account_health(db, account, adapter_factory)
    db.commit()
    db.refresh(account)
    return {**_account_shop_summary(db, account), "checked_now": True, **{
        key: value for key, value in result.items() if key in ("previous_status", "flipped_to_invalid")
    }}

@router.post("/binding")
def shop_binding(
    payload: ShopAccountRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """当前账号的千帆店铺状态。**纯本地**，不打千帆。"""
    account = _owned_account(db, current_user, payload.platform_account_id)
    return _account_shop_summary(db, account)

@router.post("/note-cleanup/candidates")
def note_cleanup_candidates(
    payload: NoteCleanupCandidatesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """「无流量笔记」候选清单（**实时翻页查千帆**，2026-10-27 起）。

    ⚠️ 原文「**纯本地，零平台请求** / 三项指标已随 `entry_json` 落库，本地就有」
    那套口径**已被设计 75 取代**：`shop_note_goods`（含 `entry_json`）整表 DROP，
    指标不再有本地副本 ⇒ 只能是 `rt.list_goods_notes` 逐页拉平台数据（见
    `note_cleanup_service.list_candidates`）。

    判定 = 平台原文里 `browse_num`（阅读）/ `note_pay_order_cnt`（支付订单）/
    `note_pay_gmv`（支付金额）**三项都明确存在且都 ≤ 阈值**（默认全 0）。

    | 约定 | 做法 |
    |---|---|
    | **数据来源** | 千帆 `goods-note/list` 逐页扫（每页一次请求，最多 `MAX_SCAN_PAGES` 页） |
    | 缺值不是 0 | 三项任一在原文里缺失 / 非数字 → **不进候选**（「读不到」≠「读到 0」） |
    | 条件可配置 | 时间区间、30 日阅读开关与上限、三项阈值全部可选；不是写死全库 |
    | 分页 | `page` / `page_size`（上限 100），响应带 `total` |
    | 诚实边界 | `coverage.pool`（本地映射条数）/ `coverage.goods_note_total`（店铺笔记总数）必须一起暴露 |
    | 鉴权 | 账号不属于当前用户 → 404；**需要 `ark` 令牌** —— 无令牌返回空池 + `bound=false`（要查平台才判得出） |
    | 只读 | 不写任何表 |
    """
    account = _owned_account(db, current_user, payload.platform_account_id)

    try:
        publish_from = _datetime_from(payload.publish_from, end_of_day=False)
        publish_to = _datetime_from(payload.publish_to, end_of_day=True)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "NOTE_CLEANUP_TIME_RANGE_INVALID", "message": str(error)},
        )
    if publish_from is not None and publish_to is not None and publish_from > publish_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "NOTE_CLEANUP_TIME_RANGE_INVALID",
                "message": "发布时间的起始值晚于截止值，请调整区间。",
            },
        )

    query = CandidateQuery(
        publish_from=publish_from,
        publish_to=publish_to,
        include_30d=payload.include_30d,
        browse_30d_max=payload.browse_30d_max,
        thresholds={
            "browse_num": payload.browse_num_max,
            "note_pay_order_cnt": payload.pay_order_cnt_max,
            "note_pay_gmv": payload.pay_gmv_max,
        },
    )
    result = list_candidates(db, account.id, query, page=payload.page, page_size=payload.page_size)
    return {
        "platform_account_id": payload.platform_account_id,
        "shop_name": _shop_display_name(db, account),
        "queried_at": shanghai_now().strftime("%Y-%m-%d %H:%M:%S"),
        **result,
    }

@router.post("/unbind", include_in_schema=False)
def unbind_shop_removed():
    """**已退场**（ADR-021 / 设计 18，2026-09-30）。

    原动作是「清 `shop_id_fk` + 删本账号的商品映射 + 删店铺行」。关系与店铺实体
    都不在了 ⇒ 无「解绑」对象。停用某账号的千帆能力 = 在账号矩阵
    **移除该账号的 ark 登录态**（凭据操作）。

    与 `POST /bind` 同样保留显式 `410`（见其 docstring），不进 OpenAPI。
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "QIANFAN_BINDING_REMOVED",
            "message": "「解绑千帆店铺」已随店铺实体一并退场（ADR-021）。"
                       "要停用千帆能力，请在账号矩阵移除该账号的千帆登录态。",
        },
    )

@router.post("/shops", include_in_schema=False)
def list_shops_removed():
    """**已退场**（ADR-021 / 设计 18，2026-09-30）。

    原读侧「列当前用户的店铺、每个店挂哪个账号」建立在 `shops` 表上；店铺不再是
    实体 ⇒ 这个视图无对象。前端商品列表页的账号选择器现在**直接用账号列表**
    （账号即归属），不再需要「店铺 → 账号」的映射。

    `include_in_schema=False`：不再是能力，不进 OpenAPI；显式 `410` 给老前端
    一个能读懂的答复而不是 `404`。
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "QIANFAN_SHOPS_REMOVED",
            "message": "「店铺列表」已随店铺实体一并退场（ADR-021）。请改用账号列表"
                       "（账号即数据归属键）。",
        },
    )

@router.post("/item-notes")
def item_notes(
    payload: ItemNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """某个商品**挂了哪些笔记**（商品列表页点「N 篇」；设计 13 §31.6）。

    ## 数据来源：**全实时**（2026-10-27 用户口径「本地不保存笔记数据」）

    | 环节 | 来源 |
    |---|---|
    | 有哪些笔记 / 标题 / 发布时间 / 互动数 / 挂载商品 | 千帆 `goods-note/list?item_mix_search_word=<item_id>`（GET，只读） |
    | 正文 / 图集 / 话题 / 视频 | 点开某篇时再查 `POST /note-detail`（切到哪篇查哪篇） |

    ⇒ 本接口**不读任何本地表**：`notes` / `note_assets` / `note_goods` 都不参与
    （`note_goods` 已随迁移 DROP）。响应里也不再回 `synced_notes` /
    `goods_synced_at` 这类「本地同步了多少」的覆盖率数字 —— 没有同步这回事了。

    ⚠️ 平台失败时降级为 `error` + 空列表，不抛 500（列表页要能显示真实原因）。
    ⚠️ 一次请求只打平台一次（`page_size=100` 取该商品的全部笔记）。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    from backend.app.services import realtime_platform_query_service as rt

    platform_error = ""
    try:
        live = rt.list_goods_notes(
            _realtime_adapter(db, account, adapter_factory),
            item_id=payload.item_id,
            page=1,
            page_size=100,
        )
        rows = live.get("items") or []
    except HTTPException:
        #: 没有 ark 令牌 = 确定性答案，照旧 400 让用户补登录
        raise
    except Exception as error:  # noqa: BLE001 - 平台失败降级为可读原因，不抛 500
        rows = []
        platform_error = str(error)
        logger.warning("item_notes 平台查询失败（account=%s）：%s", account.id, error)

    #: 挂载商品的价格**只有 SKU 接口给**（`goods-note/list` 的 `goods_infos[]` 及
    #: 详情 `biz_relations` 都不含价格）。这里按商品 ID **批量取一次**（平台上限 10
    #: 个/批，本层自动分片）⇒ 不随笔记条数线性放大请求数。
    #: ⚠️ 拿不到价格不算失败：`price_fen = None` ⇒ 前端显示「未返回」（缺值口径）。
    sku_prices: dict[str, list[dict[str, Any]]] = {}
    goods_ids = list(
        dict.fromkeys(
            str(g.get("item_id") or "")
            for row in rows
            for g in (row.get("goods") or [])
            if str(g.get("item_id") or "").strip()
        )
    )
    if goods_ids:
        try:
            bundle = rt.get_product_detail(
                _realtime_adapter(db, account, adapter_factory), item_ids=goods_ids
            )
            for entry in bundle.get("items") or []:
                sku_prices[str(entry.get("item_id") or "")] = entry.get("skus") or []
        except Exception as error:  # noqa: BLE001 - 价格是附加值，失败降级为空
            logger.warning("item_notes 取 SKU 价格失败（account=%s）：%s", account.id, error)

    notes: list[dict[str, Any]] = []
    for row in rows:
        note_id = str(row.get("note_id") or "")
        if not note_id:
            continue
        token = _first_text(row.get("xsec_token"))
        note_url = f"https://www.xiaohongshu.com/explore/{note_id}"
        if token:
            note_url = f"{note_url}?xsec_token={token}&xsec_source=pc_creatormng"
        cover = row.get("cover") or ""
        notes.append(
            {
                "note_id": note_id,
                "title": row.get("title") or "",
                #: ⚠️ 正文恒空：它只随 `POST /note-detail` 到（一次一篇），列表不做 N 次请求。
                "content": "",
                "cover": cover,
                "images": [cover] if cover else [],
                "video_url": "",
                "publish_time": row.get("publish_time") or "",
                "browse_num": row.get("read_count"),
                "like_num": row.get("like_count"),
                "collect_num": row.get("collect_count"),
                "comment_num": row.get("comment_count"),
                "share_num": row.get("share_count"),
                "note_url": note_url,
                #: 平台自报的笔记类型（`goods_note` = 商业笔记）。**唯一可信标识**：
                #: 平台没有「这条笔记从哪同步来」的概念，本地 `note_type` / `publish_status`
                #: 一概不展示（那两列在本地表 DROP 后恒为空，渲染出来就是猜的）。
                "note_flag": row.get("note_flag") or "",
                #: 挂载商品（`goods_infos[]` 的 4 个字段 + 从 SKU 接口补的**价格**）。
                #: `price_fen` 为该商品**最低可售 SKU 价**（分）；`None` = 没查到。
                "goods": [
                    dict(
                        g,
                        price_fen=_min_sku_price(sku_prices.get(str(g.get("item_id") or "")) or []),
                    )
                    for g in (row.get("goods") or [])
                ],
            }
        )
    notes.sort(key=lambda note: str(note.get("publish_time") or ""), reverse=True)

    return {
        "item_id": payload.item_id,
        "count": len(notes),
        "notes": notes,
        #: 恒 `platform`（平台失败时 `platform_degraded`）；不再有「本地覆盖率」概念。
        "source": "platform" if not platform_error else "platform_degraded",
        #: 平台查询失败时的原因（空 = 成功）。**不静默**：前端要能显示「这次没问到平台」。
        "error": platform_error,
    }


def _min_sku_price(skus: list[dict[str, Any]]) -> int | None:
    """该商品**最低可售 SKU 价**（分）。没有可售 SKU / 没有价格 ⇒ `None`（不填 0）。

    ⚠️ 用最低价而不是第一条：一个商品可能有多个规格，取最低价才是列表上「起售价」的语义。
    ⚠️ 不取 `original_price_fen`（划线价）—— 展示真售价，划价另说。
    """
    prices = [
        int(sku["price_fen"])
        for sku in skus
        if isinstance(sku, dict)
        and sku.get("price_fen") is not None
        and sku.get("buyable") is not False
    ]
    return min(prices) if prices else None


def _realtime_adapter(db: Session, account, adapter_factory):
    """按账号自己的 `ark` 令牌造千帆 adapter（**不借任何别的凭据**）。

    与 `shop_goods_service` 同一条纪律：千帆凭据只认本账号的 `ark` 通道；
    没有令牌 ⇒ 抛 400 `ARK_SESSION_REQUIRED`（结构化原因 + 让用户补登录），
    而不是拿空 cookie 去调平台、再由平台回一个看不懂的 code。

    ⚠️ 改造前商品列表 / 详情「读本地台账、零平台请求、无门禁」，**该口径已被
    设计 75 §2.3 取代**：本页现在真的打千帆，登录态是硬前提。
    """
    from backend.app.services.shop_binding_service import qianfan_cookies_for_account

    cookies = qianfan_cookies_for_account(db, account.id)
    if not cookies:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "ARK_SESSION_REQUIRED",
                "message": "该账号还没有千帆（ark）令牌，请先登录千帆后台。",
            },
        )
    return adapter_factory(cookies)


def _realtime_query_error_detail(error, *, fallback_status: int = status.HTTP_502_BAD_GATEWAY):
    """`RealtimeQueryError` → `HTTPException`（**结构化原因，不猜**）。

    凭据失效走 **400**（用户能自救：去重新登录千帆），其余平台失败走 **502**
    （我们这边没错，平台没答上来）。这两个码与改造前同步链路的门禁口径一致
    （`ARK_SESSION_REQUIRED` 400 / 平台失败 502），前端无需改分流。
    """
    is_credential = getattr(error, "code", "") == "QIANFAN_CREDENTIAL_EXPIRED"
    detail = (
        error.as_dict()
        if hasattr(error, "as_dict")
        else {"code": "QIANFAN_QUERY_FAILED", "message": str(error)}
    )
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST if is_credential else fallback_status,
        detail=detail,
    )


def _decorate_realtime_item(item: dict) -> dict:
    """实时商品行 → **改造前逐字段相同**的展示契约（对齐 `normalize_item_from_raw`）。

    ## 为什么补在这一层，而不是改 `rt.normalize_product`

    `rt.normalize_product` 是实时链路的**数据层**（平台原文 → 白名单 dict），服务三域；
    本函数是**商品列表 / 详情这一层**的展示契约（与台账时代的
    `shop_products_service.normalize_item_from_raw` 对齐）。两件事混在一处 ⇒
    订单域被迫带上商品字段。

    | 补的字段 | 来源 | 说明 |
    |---|---|---|
    | `note_count` | `platform_note_count` | 列表页**不逐商品打平台**（一页 20 条 = 20 次请求），给平台侧原文计数 |
    | `note_count_source` | 固定 `"platform"` | 标明口径（真实篇数在 `/item-notes` 实时查） |
    | `category` | 平台 `categories[]` | 服务层已算好（与台账 `_category_path` 同一判据） |
    | `allowed_actions` | `shop_item_operate_gates.allowed_actions` | 判据的**唯一实现**，前端按它渲染按钮 |
    """
    from backend.app.services import shop_item_operate_gates

    item["note_count"] = int(item.get("platform_note_count") or 0)
    item["note_count_source"] = "platform"
    item["category"] = item.get("category") or ""
    item["ai_title"] = item.get("ai_title") or ""
    item["ai_selling_point"] = item.get("ai_selling_point") or ""
    #: ⚠️ 必须在 `seller_freeze` 之类的标记**打完之后**调用，否则按钮判据看不到它们。
    item["allowed_actions"] = shop_item_operate_gates.allowed_actions(
        item, status_key=str(item.get("status_key") or "")
    )
    return item


def _realtime_items_page(
    db: Session,
    account,
    payload: ShopItemsRequest,
    *,
    adapter_factory,
    time_from,
    time_to,
    price_from_fen: int | None,
    price_to_fen: int | None,
) -> dict:
    """商品列表一页（**实时查千帆**，设计 75 §2.3 / §4.1 G6）。

    取代 v1.32 起的「读本地台账 `shop_products`」。返回**改造前逐字段相同的形状**
    （`page_no` / `page_size` / `total` / `count` / `has_more` / `sort_field` /
    `order` / `items`），只有三处语义升级：`source` 由 `local` → `platform`、
    `queried_at` 改为**本次查询时刻**、每行多一个 `note_count_source`。

    ## 四个必须记住的口径

    1. **挂链笔记数（`note_count`）逐商品实时查是禁止的** —— 平台只能按单个商品查
       （`goods-note/list?item_mix_search_word=<itemId>`），一页 20 条 = 20 次请求
       （外加 20 次限流等待），列表会被拖成几十秒。⇒ 列表给平台侧的
       `product_note_num` 并标 `note_count_source="platform"`；**真实篇数在
       `/item-notes` 里实时查**（用户点开才发生的单次请求）。
    2. **`note_count` 筛选因此失效**：改造前按本地关系表 SQL 过滤，实时链路没这个能力
       （同上）⇒ 带该条件时**如实回 400**，不静默忽略（「筛了没生效且不报错」是红线）。
    3. **`status` 的合法取值由路由层白名单校验**（未知键 400）；`all` / 空串是合法的
       「全部」口径 ⇒ SDK 收到 `"all"`（= 不发 `card_type`，与平台页面点「全部」同形）。
    4. **平台失败 ⇒ 空页 + `error`，不抛 500 / 502**（本卡硬约束）：列表页该显示
       空态与真实原因，而不是一个坏掉的页面。**不回退本地台账** —— 那是两份真相。
    """
    from backend.app.services import realtime_platform_query_service as rt

    if payload.note_count is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_NOTE_COUNT_FILTER_UNAVAILABLE",
                "message": "挂链笔记数筛选已随实时查询下线（平台无批量查询口子）。请在商品详情查看真实篇数。",
            },
        )

    page_no = max(1, int(payload.page_no))
    page_size = max(1, min(int(payload.page_size), 100))
    sort_field = payload.sort_field or "create_time"
    order = payload.order or "desc"

    def _page(items: list[dict], total) -> dict:
        return {
            "page_no": page_no,
            "page_size": page_size,
            "total": total,
            "count": len(items),
            "has_more": (
                page_no * page_size < total if total is not None else len(items) >= page_size
            ),
            #: 实时链路：真的是「本次查询时刻」（改造前是台账 max(synced_at)）。
            "queried_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sort_field": sort_field,
            "order": order,
            "items": items,
        }

    try:
        adapter = _realtime_adapter(db, account, adapter_factory)
        result = rt.list_products(
            adapter,
            keyword=(payload.keyword or "").strip() or None,
            status=(payload.status or "on_shelf").strip(),
            page=page_no,
            page_size=page_size,
            price_min_fen=price_from_fen,
            price_max_fen=price_to_fen,
            stock_min=payload.stock_from,
            stock_max=payload.stock_to,
            sales_min=payload.sales_from,
            sales_max=payload.sales_to,
            #: ⚠️ 传**毫秒**：`rt` 内部 `_to_epoch_s` 转平台要的 Unix **秒**
            #: （传毫秒会被平台 ES 拒成 HTTP 500，见 SDK `search_items` 的坑 3）。
            create_from=time_from,
            create_to=time_to,
            sort_field=sort_field,
            order=order,
            is_channel=bool(payload.is_channel),
        )
    except HTTPException:
        #: 没有 ark 令牌是**确定性答案**（用户能自救），照旧 400 —— 不是平台失败。
        raise
    except Exception as error:  # noqa: BLE001 - 平台侧任何异常都转成空页 + error
        logger.warning("商品列表实时查询失败（account=%s）：%s", account.id, error)
        page = _page([], 0)
        page["error"] = (
            error.as_dict()
            if hasattr(error, "as_dict")
            else {"code": "QIANFAN_QUERY_FAILED", "message": str(error)}
        )
        return page

    items = [_decorate_realtime_item(item) for item in (result.get("items") or [])]

    #: ⚠️ 平台没给 total 时**不拿本页条数冒充分母**（缺值口径）。
    raw_total = result.get("total")
    total = int(raw_total) if isinstance(raw_total, int) else None
    return _page(items, total)


@router.post("/note-detail")
def note_detail(
    payload: NoteDetailRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """**单篇笔记详情**（实时查千帆，2026-10-27）。

    用户口径：「笔记数据不再落库，应实时查询，所有笔记全部应是实时数据」。

    | 项 | 说明 |
    |---|---|
    | 数据源 | **千帆实时**（`postgw/note/detail`，强签名）—— 本地 `notes` 表**不再参与** |
    | 为什么按单篇 | 列表接口 `goods-note/list` 的 23 个字段**没有正文**；正文只有这条接口给 |
    | 调用时机 | 前端**切到哪篇查哪篇**（tab 切换触发），不预加载 ⇒ 打开弹窗只打 1 次列表 |
    | 返回 | 标题 / **正文全文** / 图片列表 / 封面 / 发布时间 / 话题 / 挂载商品 / 四个互动数 / `raw`（平台 39 字段） |
    | 失败 | 平台失败 → 502 + `code`（前端显示真实原因，不静默给空正文） |

    ⚠️ **不落库**：本接口只读，结果不写任何表 —— 「本地缓存与平台不一致」正是这轮
    改造要消灭的问题。
    """
    from backend.app.services import realtime_platform_query_service as rt

    account = _owned_account(db, current_user, payload.platform_account_id)
    try:
        return rt.get_note_detail(
            _realtime_adapter(db, account, adapter_factory),
            note_id=payload.note_id,
        )
    except HTTPException:
        #: 没有 ark 令牌 = 确定性答案（用户能自救），照旧 400。
        raise
    except Exception as error:  # noqa: BLE001 - 平台侧异常统一转结构化 502
        raise _realtime_query_error_detail(error) from error


@router.post("/items")
def shop_items(
    payload: ShopItemsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """商品列表（**实时查千帆**，设计 75 §2.3 / §4.1 G6）。

    | 约定 | 做法 |
    |---|---|
    | 数据源 | 每次请求实时 `POST /api/edith/product/search_item_v2`（只读）；**不读本地台账** |
    | 有界 | 一次一页，`page_size` 上限 100（Pydantic 钳制）；翻页由用户驱动 |
    | 状态白名单 | 未知 `status` → 400 `QIANFAN_STATUS_UNSUPPORTED`，不猜枚举（`all` / 空 = 不发 `card_type`） |
    | 挂链笔记数 | ⚠️ **筛选已下线**（平台只能按单商品查，一页 20 条 = 20 次请求）⇒ 带 `note_count` 条件时 400 `QIANFAN_NOTE_COUNT_FILTER_UNAVAILABLE`；列值给平台侧计数并标 `note_count_source="platform"`，真实篇数在 `/item-notes` |
    | 归属门禁 | 账号必须属于当前用户（否则 404）；**没有 ark 令牌 → 400 `ARK_SESSION_REQUIRED`**（本页现在真的打平台，登录态是硬前提 —— 改造前「无门禁」的口径已被取代） |
    | 金额单位 | 入参是**元**、出参是**分**，换算只发生在 `_price_fen()` |
    | 时效诚实 | `queried_at` = **本次查询时刻**（改造前是台账同步时间）；`products_synced_at` 恒空（实时链路没有「同步」这个概念） |
    | 平台失败 | **200 + 空 `items` + `error`**（`code` + 人话原因），不抛 500/502、不回退本地 |

    展示契约与改造前**逐字段一致**（对齐 `shop_products_service.normalize_item_from_raw`，
    见 `_decorate_realtime_item`）；补齐动作在 `_realtime_items_page`（唯一实现）。
    """
    from apis.xhs_qianfan_apis import (
        QIANFAN_ITEM_STATUS_LABELS,
        QIANFAN_ITEM_STATUSES,
        SORTABLE_ITEM_FIELDS,
    )

    account = _owned_account(db, current_user, payload.platform_account_id)
    status_key = (payload.status or "on_shelf").strip()
    if status_key != "all" and status_key not in QIANFAN_ITEM_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_STATUS_UNSUPPORTED",
                "message": f"不支持的商品状态：{status_key}",
                "supported": ["all", *QIANFAN_ITEM_STATUSES.keys()],
            },
        )
    status_label = QIANFAN_ITEM_STATUS_LABELS.get(status_key, status_key)

    #: 排序字段**白名单**（2026-10-27 实测）：平台 `search_item_v2` 只认这 7 个
    #: `sort_field`，其余字段名（如 `price_min_fen` / `total_stock` / `last_audit_time`）
    #: 一律回 `服务端处理失败` ⇒ 整页列表变红。这里提前 400 并回支持列表，
    #: 与 `status` / `note_count_op` 同一条纪律：**不许静默退化成默认**。
    sort_field = (payload.sort_field or "create_time").strip()
    if sort_field not in SORTABLE_ITEM_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_SORT_FIELD_UNSUPPORTED",
                "message": f"不支持的排序字段：{sort_field}",
                "supported": list(SORTABLE_ITEM_FIELDS),
            },
        )

    #: 挂链笔记数的比较符同样是**白名单**（v1.34）：未知值 400 并回支持列表 ——
    #: 「不支持的取值静默退化成默认」会让用户以为筛过了（与 status 同一条纪律）。
    note_count_op = (payload.note_count_op or "lt").strip().lower()
    if note_count_op not in NOTE_COUNT_OPS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_NOTE_COUNT_OP_UNSUPPORTED",
                "message": f"不支持的挂链笔记数比较符：{payload.note_count_op}",
                "supported": list(NOTE_COUNT_OPS),
            },
        )

    try:
        time_from = _epoch_millis(payload.create_time_from, end_of_day=False)
        time_to = _epoch_millis(payload.create_time_to, end_of_day=True)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "QIANFAN_TIME_RANGE_INVALID", "message": str(error)},
        )
    if time_from is not None and time_to is not None and time_from > time_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_TIME_RANGE_INVALID",
                "message": "创建时间的起始值晚于截止值，请调整区间。",
            },
        )

    page = _realtime_items_page(
        db,
        account,
        payload,
        adapter_factory=adapter_factory,
        #: ⚠️ 传**毫秒**（本层契约不变）：`rt.list_products` 再经 `_to_epoch_s`
        #: 转成平台要的**秒**。单位换算仍然只有这两处显式转换，不散落。
        time_from=time_from,
        time_to=time_to,
        price_from_fen=_price_fen(payload.price_from),
        price_to_fen=_price_fen(payload.price_to),
    )
    #: 店铺冻结标记（`seller_freeze`）：**本地无法从商品行推出**（它不随条目下发），
    #: 只能由写操作的失败回执写入 `shop_state`（见 `shop_item_operate_service`）。
    #: 必须在 `_decorate_realtime_item` **之前**打到行上 —— `allowed_actions` 要看到它，
    #: 冻结店三个动作全不可做（千帆原文：「您的店铺被冻结，解冻后可发布、编辑商品」）。
    if get_shop_state(account).get("seller_freeze"):
        for item in page["items"]:
            item["seller_freeze"] = True
            _decorate_realtime_item(item)

    #: 缺值口径：平台没给总数 ⇒ `total=0` 占位 + `total_known=False`，
    #: 让前端能说「总数未知」而不是谎报「0 件」。
    response = {
        "platform_account_id": payload.platform_account_id,
        #: 实时链路：数据来自千帆，不是本地台账（改造前是 "local"）。
        "source": "platform",
        "shop_name": _shop_display_name(db, account),
        "status": status_key,
        "status_label": status_label,
        "page_no": page["page_no"],
        "page_size": page["page_size"],
        "total": page["total"] if page["total"] is not None else 0,
        "total_known": page["total"] is not None,
        "count": page["count"],
        "has_more": page["has_more"],
        "queried_at": page["queried_at"],
        #: 实际生效的排序口径（设计 13 §32.1）：后端是排序的唯一权威，前端表头
        #: 显示的就是这两个值 —— 用户点箭头换了排序，勾选与展示都以此为准。
        "sort_field": page["sort_field"],
        "order": page["order"],
        #: 实时链路下就是本页的 `total`（改造前读台账 `shop_state`）。
        "products_total": page["total"] if page["total"] is not None else 0,
        #: 改造前是台账同步时间；实时链路**没有「同步时间」这个概念了** ——
        #: 留空而不是填 `queried_at`（那会让前端误以为「数据是那时同步的」）。
        "products_synced_at": "",
        "items": page["items"],
    }
    #: ⚠️ 平台失败 ⇒ **200 + 空 `items` + `error`**（本卡硬约束：列表页显示空态与
    #: 真实原因，不抛 500/502）。`error.code` 是稳定机器判据（凭据失效
    #: `QIANFAN_CREDENTIAL_EXPIRED` / 其余 `QIANFAN_QUERY_FAILED`）⇒ 前端据此
    #: 决定「去重新登录」还是「重试」。
    if page.get("error"):
        response["error"] = page["error"]
    return response


def _find_platform_product(adapter, item_id: str) -> dict | None:
    """按商品 ID **实时**取单件商品的列表行（`search_item_v2` + 精确匹配）。

    ## 为什么用「关键词搜索」而不是另找「按 ID 查商品」的接口

    千帆**没有**「按商品 ID 查商品」的独立口子（`get_skus_by_item_ids` 只给 SKU 面，
    不含名称 / 价格区间 / 分类 / 主图宽高）。而商品列表页的关键词框 `whole_keywords`
    **认商品 ID**（SDK docstring：名称 / ID / 货号 / 商家编码）；2026-10-27 真机实测
    传 `6adaf9dd238c6b00019c0dd7` 返回 `total=1` 且正是该商品。

    ⚠️ 它仍是**搜索**（不是精确查询）⇒ 必须按 `item_id` **逐条精确匹配**再返回。
    搜不到 / 只搜到别的商品 ⇒ `None`（调用方转 404）。**绝不拿第一条顶替** ——
    那会把 A 商品的详情渲染成 B 的，用户看不出，是最贵的一类静默错误。

    `status="all"`：详情弹窗要能打开任何状态的商品（已下架 / 审核中 / 已冻结）。
    """
    from backend.app.services import realtime_platform_query_service as rt

    ok, message, raw = adapter.search_items(
        page_no=1, page_size=10, keyword=item_id, status="all"
    )
    if not ok:
        rt._raise_if_failed(ok, message, raw, what="商品详情获取")
    data = (raw or {}).get("data") if isinstance(raw, dict) else {}
    rows = [row for row in ((data or {}).get("items") or []) if isinstance(row, dict)]
    for row in rows:
        if str(row.get("item_id") or "").strip() == item_id:
            return row
    return None


@router.post("/item-detail")
def item_detail(
    payload: ItemDetailRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """商品详情弹窗的数据（设计 75 §2.3 / §4.1 G6：**实时查千帆**）。

    两条并行的平台查询（都只读、无签名）：

    | 用在哪 | 平台接口 | 给什么 |
    |---|---|---|
    | `item`（基本信息，与 `/items` 的 `items[]` 同构） | `search_item_v2`（关键词 = 商品 ID，**精确匹配**） | 名称 / 价格区间 / 库存 / 状态 / 分类 / 主图（**含宽高**） |
    | `skus`（SKU 规格明细） | `get_skus_by_item_ids`（单件） | SKU 名称 / 规格 / 价格 / 库存 / 规格图 |

    ## 三个必须记住的口径

    1. **`product_images` / `detail_images` 恢复了真正的分离**：改造前读台账
       `raw_json`（`images` / `image_descriptions`），实时链路同样能拿这两个数组
       （实测某商品 `images=5` / `image_descriptions=50`）⇒「生成详情图」弹窗的
       两组参考图**完整**，不必退化成「只有几张 SKU 规格图」。
    2. **`item.note_count` 不是实时篇数**：本端点不额外调 `goods-note/list`
       （详情弹窗的「挂链笔记」走专门的 `/item-notes`）。这里沿用平台侧计数并标
       `note_count_source="platform"`，与列表**同一个数**。
    3. **平台查不到 ⇒ 404；平台请求失败 ⇒ 502（凭据失效 400）**。详情弹窗没有
       「空态」可言 —— 拿不到就是拿不到，不能渲染成「这个商品没有 SKU」。

    | 错误 | 语义 |
    |---|---|
    | 404 | 账号不存在 / 不属于当前用户；或平台上没有这个商品（可能已删除） |
    | 400 `ARK_SESSION_REQUIRED` | 该账号没有千帆（ark）令牌 |
    | 400 `QIANFAN_CREDENTIAL_EXPIRED` | 令牌过期，需重新登录千帆 |
    | 502 `QIANFAN_QUERY_FAILED` | 其余平台失败 |
    """
    from backend.app.services import realtime_platform_query_service as rt

    account = _owned_account(db, current_user, payload.platform_account_id)
    item_id = payload.item_id.strip()
    adapter = _realtime_adapter(db, account, adapter_factory)

    try:
        raw_product = _find_platform_product(adapter, item_id)
        detail = rt.get_product_detail(adapter, item_ids=[item_id])
    except HTTPException:
        raise
    except Exception as error:  # noqa: BLE001 - 平台侧异常统一转结构化
        raise _realtime_query_error_detail(error) from error

    rows = detail.get("items") or []
    entry = rows[0] if rows else {}
    skus = entry.get("skus") or []

    #: **平台查不到**（搜索无精确命中 / `skusMap` 没这一项）≠ 请求失败 ⇒ 404，
    #: 与改造前「台账里没有这个商品」同一个语义。
    if raw_product is None and (item_id in (detail.get("missing") or []) or not skus):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="千帆上没有这个商品（可能已被删除，或商品 ID 有误）",
        )

    item = rt.normalize_product(raw_product or {})
    #: 店铺冻结标记**本地无法从商品行推出**（不由条目下发），只能由写操作的失败回执
    #: 写进 `shop_state`。必须在 `_decorate_realtime_item` 之前打上 —— 按钮判据要看到它
    #: （千帆原文：「您的店铺被冻结，解冻后可发布、编辑商品」）。
    if get_shop_state(account).get("seller_freeze"):
        item["seller_freeze"] = True
    item = _decorate_realtime_item(item)

    raw_item = raw_product or {}
    product_images = shop_products_service.product_image_links(raw_item)
    detail_images = shop_products_service.detail_image_links(raw_item)
    videos = shop_products_service.product_video_links(raw_item)
    #: 「图集」= 主图 + 详情图合并去重（两处都是
    #: `shop_products_service` 的**纯函数**：平台原文 → 链接列表，零 DB）。
    #: SKU 规格图**不混进商品图集**。
    images = product_images + [url for url in detail_images if url not in product_images]

    return {
        "platform_account_id": payload.platform_account_id,
        "item_id": item_id,
        "shop_name": _shop_display_name(db, account),
        "item": item,
        "images": images,
        #: 商品主图 —— 「生成详情图」弹窗参考图分组 ①。
        "product_images": product_images,
        #: 原商品详情图 —— 参考图分组 ②。
        "detail_images": detail_images,
        #: 商品视频（`raw_json.videos`）—— 2026-10-27 新增：改造前整条链路没取过。
        "videos": videos,
        "skus": skus,
        #: **平台原始行**（`search_item_v2` 的 `items[i]` 原样，只去掉嵌套媒体数组）。
        #: 2026-10-27 用户口径「根据 json 自动生成一张实时表，展示接口返回的所有数据」：
        #: 前端拿它把平台的**每一个字段**都列出来，不再由我方挑选展示哪些。
        #: 媒体（images / image_descriptions / videos）已单独提取成上面三个键，
        #: 这里剔除以免同一个数据在表里重复出现几十行。
        "raw_fields": {
            key: value
            for key, value in raw_item.items()
            if key not in {"images", "image_descriptions", "videos"}
        },
        "sku_count": entry.get("sku_count") if entry.get("sku_count") is not None else len(skus),
        #: 实时链路：刚刚查过，恒「有详情」。保留字段名，前端渲染逻辑零改动
        #: （改造前的空态判据「空串 = 没补过详情」随本地化一起退场）。
        "detail_synced_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        #: 改造前是台账 `synced_at`；实时链路没有这个概念 ⇒ 空串（不谎报同步时间）。
        "synced_at": "",
        #: 对账标记随本地台账一起退场（`platform_missing_at`）⇒ 恒空。
        "platform_missing_at": "",
        "source": "platform",
        "queried_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# --------------------------------------------------------------------------- #
# 同步店铺商品：**整段退场**（设计 75 §2.4 / §4.5，2026-10-27，S2）
#
# 商品列表 / 详情已改实时查平台（`/items` / `/item-detail`）⇒「把商品同步到本地
# 台账」这个动作**没有数据消费者了**。本地 `shop_products` 只剩两列我方数据
# （AI 采纳的标题 / 卖点）还被读到，那两列的处置归 S5（随表 DROP 一起做）。
#
# ⚠️ 两个端点改 `include_in_schema=False` + **410 墓碑**，而不是直接删除：
#   ① 不进 OpenAPI ⇒ 端点计数只算真实能力（399 → 397）；
#   ② 老前端 / 开着的页签点到它，能拿到一句读得懂的答复，而不是 404 猜半天。
# 原执行体（`shop_goods_sync_service`）按本卡要求**保留文件并标记退场**，
# 供 S5 统一清理 —— 这里只摘入口。
# --------------------------------------------------------------------------- #
@router.post("/sync-shop-goods", include_in_schema=False)
def sync_shop_goods_removed():
    """**已退场**（设计 75 §2.4，2026-10-27）。

    原 `POST /qianfan/sync-shop-goods` 手动触发定时任务 `system.shop_goods_sync`，
    把整店商品翻页落进本地 `shop_products`。商品列表改**实时查平台**后这个动作
    不再有任何数据消费者 ⇒ 退场。
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "SHOP_GOODS_SYNC_REMOVED",
            "message": "「同步店铺商品」已随商品实时查询下线：商品列表 / 详情现在直接查千帆，"
                       "不再需要同步到本地。",
        },
    )


@router.get("/sync-shop-goods/status", include_in_schema=False)
def sync_shop_goods_status_removed():
    """**已退场**（设计 75 §2.4，2026-10-27）—— 原商品同步的进度轮询。

    同步动作本身已退场，进度自然没有意义（前端也不再轮询它）。
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "SHOP_GOODS_SYNC_REMOVED",
            "message": "「同步店铺商品」已随商品实时查询下线，没有同步任务可查询进度。",
        },
    )

MAX_DETAIL_IMAGE_REFERENCES: int = 6

DETAIL_IMAGE_TASK_TYPE = "ai_product_detail_image"

MIN_DETAIL_IMAGE_COUNT: int = 1

MAX_DETAIL_IMAGE_COUNT: int = 10

DEFAULT_DETAIL_IMAGE_COUNT: int = 3

class GenerateDetailImageRequest(BaseModel):
    """商品详情图生成请求（设计 17 §13.3.3，v2.1；v2.15 加参考图勾选）。

    ⚠️ **刻意不给任意 `image_url`**：候选参考图只能来自**该商品台账里的图**
    （主图 `images` 或详情图 `image_descriptions`），由服务端按白名单校验
    （`reference_image_urls` 里的每个 URL 必须能在台账里找到）。
    让前端自由传 URL = 后端变成「下载任意地址」的跳板（SSRF 面）。
    """

    platform_account_id: int
    item_id: str = Field(min_length=1, max_length=64)
    #: 用户补充要求；为空 ⇒ 后端按商品标题/规格拼默认提示词（不硬编码在端点里，
    #: 见 `_default_detail_image_prompt`）。
    prompt: str = Field(default="", max_length=2000)
    #: 参考图（v2.15，§13.3.6）：从该商品的**主图 / 详情图**里勾选。
    #: 空 ⇒ 沿用旧行为（取商品首图）。**每个 URL 都要过白名单**，见类 docstring。
    #: v2.23：上限 5 → **6**（用户口径「最多选 6 张」；两类合并成一组后共用这个上限）。
    reference_image_urls: list[str] = Field(default_factory=list, max_length=MAX_DETAIL_IMAGE_REFERENCES)
    #: v2.18：**生成张数**（3~10，默认 3）。用户口径「生成商品详情图功能新增一个条件，
    #: 生成多少张图片」；v2.20 改范围「最多 10 张最少 3 张」。
    #: ⚠️ 这不是所有服务商都认 `n` 参数 ⇒ 实现上**逐张发请求**（`count` 次）。
    #: ⚠️ v2.22：**每组**各自一张数（见 `group_settings`）；本字段是**兜底**，
    #: 未在 `group_settings` 里指定的组用它（保持旧调用方兼容）。
    count: int = Field(default=DEFAULT_DETAIL_IMAGE_COUNT, ge=MIN_DETAIL_IMAGE_COUNT, le=MAX_DETAIL_IMAGE_COUNT)
    #: v2.22：**每组独立配置**（用户口径「主图和详情图完全拆分开…各自的提示词模板、
    #: 补充要求、生成张数」）。形如：
    #: `{"product": {"prompt": "…", "count": 5}, "detail": {"prompt": "…", "count": 6}}`。
    #: 未出现的组 ⇒ 回落顶层 `prompt` / `count`。
    group_settings: dict | None = None
    #: v2.19：**落盘到的素材库文件夹**（`material_folders.id`）。
    #: 用户口径「图片支持下载到本地指定的素材库中也就是本地指定文件夹下」。
    #: 不传 ⇒ 只落 AIGC 资产（旧行为不变）；传了但文件夹不属于本用户 ⇒ 404。
    material_folder_id: int | None = None
    #: v2.21：**单张重做**（设计 17 §13.3.7.8）。指定某组第几张时**只重跑那一张**
    #: （不重跑整组）—— 市场共识做法（Pixii `Spot Edit` / GreenOnion redo individual），
    #: 避免「一张不满意、整组白烧」。
    #: 形如 `{"group": "product", "index": 2}`；给了它就**忽略 `count`**（只出一张）。
    regenerate: dict | None = None
    #: v2.19：是否先用**文本模型**把商品信息整理成提示词（用户口径「全部整理扔给大模型」）。
    #: 默认 True；整理失败**自动回落**本地拼接（不阻断出图），原因在响应 `prompt_source`。
    refine_prompt: bool = True
    #: v2.24：**公共提示词模板**（设计 17 §13.5.4 / §7.2，用户口径「生成AI商品图 没有
    #: 公共提示模板相关逻辑」）。传 `prompt_templates.id` ⇒ 用该模板**渲染**出提示词
    #: （基础指令 + 引用模块 + 骨架 + 商品字段注入，`prompt_renderer` 唯一实现）。
    #:
    #: ⚠️ **优先级**：模板 > 手输 `prompt` > 内置默认。选了模板就按模板走 ——
    #: 否则「选了模板却还用手输」会让人以为模板没生效（判据只能有一个赢家）。
    #: 模板不属于本用户 ⇒ 404；模板不存在 ⇒ 404（不静默回落，避免「以为用了模板」）。
    template_id: int | None = Field(default=None, ge=1)
    #: v2.24：**每组各自的模板**（主图 / 详情图可各选一个）。形如
    #: `{"product": {"template_id": 3}, "detail": {"template_id": 5}}`；
    #: 未出现的组回落顶层 `template_id`。
    group_templates: dict | None = None
    #: v2.25：**风格参考图**（设计 17 §13.7，借鉴 gcc-model-gen 的「风格继承」思路）。
    #: 值是**素材库文件 ID**（`material_files.id`），**不是 URL** —— 出图端点已有
    #: SSRF 防线（参考图须过台账白名单），收 URL 等于开第二个口子。
    #: 语义：把这张图作为**风格样板**附给模型（借它的光位/色调/质感），
    #: 商品外观仍以商品参考图为准。不传 ⇒ 行为与旧版完全一致。
    style_reference_file_id: int | None = Field(default=None, ge=1)

    model_config = ConfigDict(extra="forbid")

@router.post("/generate-detail-image")
def generate_detail_image(
    payload: GenerateDetailImageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    image_ai_client: ImageAiClient = Depends(get_image_ai_client),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """**生成商品详情图**（设计 17 §13.3.3，v2.1；用户口径 2026-10-01）。

    「商品列表 → 商品详情 → 生成商品详情图」这条链路的执行体。

    ## 为什么必须后端代理（不能前端直连服务商）

    1. 服务商 `api_key` 在服务端（`llm_gateway_configs`），**绝不下发前端**；
    2. 商品图是**千帆 CDN 直链**，服务商侧取不到 ⇒ 必须由后端取商品图作参考图送入；
    3. 出图要落 `aigc_media_assets` 溯源（发布时平台要求 `aigcTaskId`）。

    ## 业务点与错误口径

    走 `generate_product_detail_image` 业务点（**与「生成配图」是两个业务点**：
    配图服务笔记正文，详情图服务商品 —— 提示词与尺寸口径都不同，可分别挂服务商）。
    失败一律**结构化、不回落**（同 `ai.py::_business_model_context`）。
    """
    from backend.app.models.generation import PublishAsset
    from backend.app.api.ai import _business_model_context

    account = _owned_account(db, current_user, payload.platform_account_id)

    #: ⚠️ 设计 75 S5（2026-10-27）：商品图从**平台实时**取（原读本地台账 `detail_json`）。
    #: 参考图白名单的判据不变：**只接受这件商品自己的图**（挡住 SSRF 与"随便传个 URL"）。
    adapter = _realtime_adapter(db, account, adapter_factory)
    raw_product = _find_platform_product(adapter, payload.item_id)
    if raw_product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="千帆上没有这个商品（可能已被删除，或商品 ID 有误）",
        )
    product_images = shop_products_service.product_image_links(raw_product)
    detail_images = shop_products_service.detail_image_links(raw_product)
    images = product_images + [url for url in detail_images if url not in product_images]
    #: 提示词模板 / 默认提示词要的是**归一化**的商品与 SKU（原 `detail` 来自本地台账
    #: `detail_json`，台账退场后这条链断了 ⇒ 下面 `detail.get(...)` 恒 NameError）。
    #: 这里有平台行就够：模板只用得上 `title` 与 `variant_name`。
    from backend.app.services import realtime_platform_query_service as _rt
    sku_bundle = _rt.get_product_detail(adapter, item_ids=[payload.item_id])
    detail = {
        "item": _rt.normalize_product(raw_product),
        "skus": [row for row in (sku_bundle.get("items") or [{}])[0].get("skus") or [] if row],
    }
    # v2.15（§13.3.6）：参考图可选 —— 勾选的主图 / 详情图，**必须过白名单**。
    # v2.23：主图与详情图**合并为一组「可选参考图」**（用户口径）⇒ 白名单合成一个集合，
    # 不再按来源归组（分组的语义已移到「生成哪一类图」的 Tab 上）。
    allowed: set[str] = set(product_images) | set(detail_images)
    picked: list[str] = []
    for url in payload.reference_image_urls or []:
        candidate = str(url or "").strip()
        if not candidate:
            continue
        if candidate not in allowed:
            # 不在台账里 ⇒ 拒绝（保留 SSRF 防线：不接受任意 URL）。
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "REFERENCE_IMAGE_NOT_FOUND",
                    "message": "勾选的参考图不在这个商品的台账图集里（可能已变更，刷新后重试）",
                },
            )
        if candidate not in picked:
            picked.append(candidate)
    # ⚠️ v2.19：**必须勾选参考图**（用户口径「必须选择原始参考图才可以点击生成按钮」）。
    # 去掉「不勾就回落商品首图」的旧行为 —— 前后端同一判据，不留「能点但被拒」的不一致。
    if not picked:
        # 如实报缺：没有参考图就没有生成依据，不凭空生成一张。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "REFERENCE_IMAGE_REQUIRED",
                "message": "请先勾选至少一张参考图（商品主图或原商品详情图）再生成",
            },
        )
    if not images:
        # 台账里根本没图 ⇒ 引导去同步商品（与上一条区分：这是「没得选」，不是「没选」）。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "PRODUCT_IMAGE_MISSING",
                "message": "这个商品还没有同步到图片，先执行「同步商品」补齐图集再试",
            },
        )
    reference_images = picked[:MAX_DETAIL_IMAGE_REFERENCES]

    # v2.25（§13.7）：**风格参考图**（可选）—— 素材库文件 ID 解析成可取的 URL，
    # 作为**最后一张**附给上游（说明句会点名「最后一张是风格图」）。
    # ⚠️ 不占商品参考图的名额：两者是两个字段、两个上限（否则选风格图会少选商品图）。
    style_url, style_note = _resolve_style_reference(
        db, current_user, file_id=payload.style_reference_file_id
    )
    if style_url:
        reference_images = [*reference_images, style_url]

    # 业务点 → 服务商（不回落；未配置 / 类型不符 ⇒ 400 结构化）。
    # ⚠️ 复用 `ai.py::_business_model_context`（**同一份**错误映射实现）：
    # 自己再写一遍 `resolve + except LLMGatewayError → 400` = 同一语义两份实现（DEF-044），
    # 两份会在「新增错误码」时分叉。
    #
    # ⚠️ **必须最早显式求值**（2026-10-13 修，实测 502 的根因）：
    # 本函数与 `ai.py` 的 8 个业务端点不同 —— 它的出图循环**不走**
    # `_recorded_image_task`，而是直接调 `image_ai_client.generate_images(...)`
    # 并自己 catch `except Exception` → 502（见下方「一张都没成」）。而
    # `model_config` / `api_key` 在**上一个版本里**只出现在那个实参列表里（属性访问
    # 时才求值）⇒ 解析失败发生在 `except Exception` 的地界上 ⇒ **被告成 502
    # 「生成商品详情图失败」**，真实原因（未配服务商）连同结构化 code 一起丢掉。
    # 实测原文（临时探针，商品在、图集有、勾了一张、业务点未配服务商）：
    #   502 {"detail":"生成商品详情图失败：原始主图 第 1~3 张：Image model api_key is required;
    #              原始详情图 第 1~3 张：Image model api_key is required"}
    # 显式求值把它挪回**请求处理**地界 ⇒ `_business_model_context` 自己抛结构化 400
    # （与设计 17 §13.3.3 错误口径表「业务点未配服务商 ⇒ 400」逐字一致）。
    #
    # ⚠️ **放在参考图/模板/提示词组装之前**：那些分支**全是纯本地校验与拼装**
    # （不触上游、不落库），排在它们后面会让「未配服务商」这个**必然失败**的结论
    # 被一个无关的本地报错抢答（一个 400 换另一个 400，但 `code` 不同 ⇒ 前端拿到的
    # 指向性提示也换了一个）。越权 / 商品缺失（404）已在更上面先判，不受影响。
    # ⚠️ 也**不依赖 `payload.refine_prompt`**：那条 `_text_model_context` 分支用
    # `is not None` 把失败吞成「回落本地拼接」，是**有意**的（不因润色模型没配把
    # 整个功能判死）；出图服务商没配则必须响 —— 两者判据不同，不许合并。
    model_config, api_key = _business_model_context(
        db, current_user, BUSINESS_KEY_PRODUCT_DETAIL_IMAGE
    )

    # v2.24（§13.5.4）：**公共提示词模板优先**（用户口径「生成图片时应选择公共提示词
    # 模板，保证图片商品的一致性」）。模板渲染出提示词 ⇒ 手输 `prompt` 降为
    # 「补充要求」追加在末尾；没选模板时才回落到「手输 > 内置默认」的旧链路。
    prompt = ""
    template_note = ""
    if payload.template_id is not None:
        template_prompt, template_note = _render_detail_image_template(
            db, current_user, template_id=payload.template_id,
            item=detail.get("item") or {}, skus=detail.get("skus") or [],
            extra_prompt=payload.prompt or "",
        )
        prompt = template_prompt
    if not prompt:
        prompt = (payload.prompt or "").strip() or _default_detail_image_prompt(
            detail.get("item") or {}, detail.get("skus") or []
        )
    # v2.25（§13.7）：风格参考说明 —— 加在 `prompt` 上（组循环从它派生），
    # 于是**每个组**都带上（风格是「怎么画」的约束，与画哪一组无关）。
    if style_note:
        prompt = f"{prompt}\n\n{style_note}"
    # v2.18/v2.20：张数收敛到常量区间（Pydantic 的 ge/le 已拦非法值，这里再夹一次 ——
    # 常量改小时端点仍然安全，不依赖模型的字段约束）。
    count = max(
        MIN_DETAIL_IMAGE_COUNT, min(int(payload.count or DEFAULT_DETAIL_IMAGE_COUNT), MAX_DETAIL_IMAGE_COUNT)
    )

    # v2.19（§13.3.7）：用户口径「将商品名称、商品参数信息和选择的参考图以及提示词和
    # 生成的图片要求**全部整理扔给大模型**」。用**文本/多态模型**把商品信息整理成
    # 一段结构化出图提示词；整理失败**回落本地拼接**（`prompt_source` 如实说明），
    # 不因「润色模型没配」把整个功能判死。
    prompt_source = "local"
    refine_note = ""
    if payload.refine_prompt:
        text_client, text_config, text_key = _text_model_context(
            db, current_user, BUSINESS_KEY_PRODUCT_DETAIL_IMAGE
        )
        if text_client is not None:
            refined, refine_note = detail_image_prompt.refine_prompt(
                item=detail.get("item") or {},
                skus=detail.get("skus") or [],
                prompt=payload.prompt or "",
                reference_images=reference_images,
                text_client=text_client,
                model_config=text_config,
                api_key=text_key,
            )
            if refined:
                prompt = refined
                prompt_source = "llm"

    # v2.22（§13.3.7.9）：**每组独立配置**（用户口径「主图和详情图完全拆分开，
    # 使用 tab，各自的提示词模板、补充要求、生成张数」）。
    #
    # ⚠️ 组设置从 `payload.group_settings` 取，**缺项回落顶层** `prompt`/`count`
    # （旧调用方只传顶层也照常工作 —— 不破坏兼容）。
    # ⚠️ 张数/提示词都在**请求时**定下来，写进 `groups`，后面循环直接读 ——
    # 不再在循环里回落判断（一处判断，避免两份实现）。
    def _group_setting(key: str) -> dict[str, Any]:
        raw = (payload.group_settings or {}).get(key)
        return raw if isinstance(raw, dict) else {}

    # v2.23（用户口径「把原始主图、原始详情图**放一组内**，该组为**可选参考图**，
    # 只有用户选了就一并扔给大模型，**最多选 6 张**」）：
    # 参考图**不再按来源分组** —— 前端只有一组「可选参考图」，勾中的一并送模型。
    # ⇒ 组的「存在性」改为由 `group_settings`（即该 Tab 有没有被要求生成）决定，
    # 参考图则**统一用全部已勾图**（`fallback_refs`）。
    fallback_refs = picked[:MAX_DETAIL_IMAGE_REFERENCES]
    # v2.25（§13.7）：风格图对**每个组**都生效（它是「怎么画」的约束，不是「画什么」）。
    #
    # ⚠️ **必须在截断之后追加**：`fallback_refs` 是「商品图 ≤6 张」，而下游
    # `group_refs[:MAX_DETAIL_IMAGE_REFERENCES]` 还会再切一次 —— 若先把风格图
    # 追加成 7 张再切，切掉的正好是**最后一张 = 风格图**（真机实测发现的缺陷：
    # 风格图永远送不到上游，而「看起来是对的」）。
    if style_url:
        fallback_refs = [*fallback_refs, style_url]

    groups: list[dict[str, Any]] = []
    for key, label in (
        ("product", "原始主图"),
        ("detail", "原始详情图"),
    ):
        setting = _group_setting(key)
        # 组存在 ⇒ 该组被显式要求生成（前端只送有勾图的组 + 本次目标组）。
        # ⚠️ 顶层没传 `group_settings` 时（旧调用方）⇒ **两组都跑**（保持兼容）。
        if not setting and payload.group_settings is not None:
            continue
        effective_refs = fallback_refs
        if not effective_refs:
            continue
        # v2.24：**组级模板优先**（主图 / 详情图可各选一个模板 —— 用户口径「使用 tab，
        # 各自的提示词模板」）。组没指定模板 ⇒ 用顶层模板渲染出的 `prompt`。
        group_prompt = str(setting.get("prompt") or "").strip() or prompt
        group_template_id = None
        if isinstance(payload.group_templates, dict):
            raw_tid = (payload.group_templates.get(key) or {})
            if isinstance(raw_tid, dict) and raw_tid.get("template_id"):
                try:
                    group_template_id = int(raw_tid["template_id"])
                except (TypeError, ValueError):
                    group_template_id = None
        if group_template_id is not None:
            group_prompt, _ = _render_detail_image_template(
                db, current_user, template_id=group_template_id,
                item=detail.get("item") or {}, skus=detail.get("skus") or [],
                extra_prompt=str(setting.get("prompt") or "").strip(),
            )
        try:
            group_count = int(setting.get("count") or count)
        except (TypeError, ValueError):
            group_count = count
        # 夹到合法区间（前端已限，这里再夹一次 —— 不依赖前端）。
        group_count = max(MIN_DETAIL_IMAGE_COUNT, min(group_count, MAX_DETAIL_IMAGE_COUNT))
        groups.append(
            {
                "key": key,
                "label": label,
                # ⚠️ v2.25：不在此截断 —— `effective_refs` 已是「商品图 ≤6 + 风格图」，
                # 再切会把末尾的风格图切掉（下游 1687 行同样不切）。
                "refs": effective_refs,
                "prompt": group_prompt,
                "count": group_count,
            }
        )

    # v2.21（§13.3.7.8）：**单张重做** —— 只重跑指定的那一张。
    # ⚠️ 用途：一组里某张不满意时，不必整组重跑（整组重跑会白烧已成功的那几张，
    # 且新图换掉满意的旧图）。市场共识做法（Pixii `Spot Edit` / GreenOnion redo）。
    # ⚠️ 本段必须在 `task = Task(...)` **之前**：payload 里要写重做目标。
    regen_group = ""
    regen_index = 0
    if isinstance(payload.regenerate, dict):
        regen_group = str(payload.regenerate.get("group") or "").strip()
        try:
            regen_index = int(payload.regenerate.get("index") or 0)
        except (TypeError, ValueError):
            regen_index = 0
        valid_groups = {item["key"] for item in groups}
        if regen_group not in valid_groups or regen_index < 1:
            # 如实报错，不静默退化成「整组重跑」（那会烧掉用户没打算动的图）。
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "REGENERATE_TARGET_INVALID",
                    "message": "单张重做的目标无效（该组没有勾选参考图，或序号不是正整数）",
                },
            )
        # 只跑目标那一组，且只出一张。
        groups = [item for item in groups if item["key"] == regen_group]
        groups[0]["count"] = 1

    task = Task(
        user_id=current_user.id,
        platform="xhs",
        task_type=DETAIL_IMAGE_TASK_TYPE,
        status="running",
        progress=10,
        payload={
            "platform_account_id": account.id,
            "item_id": payload.item_id,
            "prompt": prompt,
            "model_config_id": model_config.id,
            "reference_images": reference_images,
            # v2.21：单张重做时记录目标（排障能区分「整组生成」与「重做某张」）。
            **({"regenerate": {"group": regen_group, "index": regen_index}} if regen_group else {}),
            # v2.18：生成张数一并入 payload（排障时能看出「这次要了几张」）。
            "count": count,
            # v2.22：**每组最终的提示词与张数**（排障时能看出每组实际用了什么；
            # 组设置与顶层兜底的合并在请求时就已完成，这里记录结果）。
            "group_plans": [
                {
                    "group": item["key"],
                    "count": item["count"],
                    "prompt": item["prompt"][:300],
                    "refs": item["refs"][:MAX_DETAIL_IMAGE_REFERENCES],
                }
                for item in groups
            ],
            # v2.19：提示词来源（llm = 文本模型整理；local = 回落本地拼接）+ 整理说明。
            "prompt_source": prompt_source,
            **( {"template_note": template_note} if template_note else {}),
            **( {"prompt_preview": prompt[:300]} if template_note else {}),
            **( {"refine_note": refine_note} if refine_note else {}),
            **({"material_folder_id": payload.material_folder_id} if payload.material_folder_id else {}),
        },
    )
    db.add(task)
    db.flush()

    # v2.20：**按组拆分生成**（设计 17 §13.3.7.5，用户口径「生成的结果也要分主图和
    # 详情图，这是两大块」）。
    #
    # ⚠️ **为什么不在一次调用里混着送**：出图请求把两组参考图混在一起时，上游返回的
    # 结果**不带「这张是按哪组生成的」标记** —— 事后无法可靠分组（只能靠猜）。
    # 按组各发各的请求，分组就成了**事实**（请求时就定的），而不是事后推断。
    #
    # ⚠️ 张数是**每组**的张数（勾了两组 ⇒ 最多 2 × count 张）。
    # ⚠️ `groups` / 重做解析已在 `task = Task(...)` 之前完成（payload 要写它们）。
    assets: list[dict[str, Any]] = []
    errors: list[str] = []
    total = sum(item["count"] for item in groups)
    done = 0
    for item in groups:
        group_key = item["key"]
        group_label = item["label"]
        group_refs = item["refs"]
        group_prompt = item["prompt"]
        group_count = item["count"]
        # ⚠️ **v2.29：一次请求要 N 张**（用户口径「为何是串行」）。
        # 真机实测（2026-10-03）：当前服务商认 `n` —— `n=3` 一次回 3 张、57.8 秒；
        # 逐张串行要 3 × ~40s ≈ 120s，且**请求数 3 倍**（白烧限流额度）。
        # 回落见下：服务商忽略 `n` 时按缺张逐张补（旧的串行实现保留为补张路径）。
        try:
            batch = image_ai_client.generate_images(
                model_config=model_config,
                api_key=api_key,
                prompt=group_prompt,
                # 参考图 = **本组**勾选的（不跨组混送）。
                # ⚠️ v2.25：这里**不再截断** —— `group_refs` 在组装时已经
                # 截成「商品图 ≤6 + 风格图 ≤1」，再切一次会把风格图切掉
                # （它在列表**末尾**，正是被切的位置）。
                reference_images=group_refs,
                count=group_count,
            )
        except Exception as exc:  # noqa: BLE001 - 服务商五花八门，一律结构化转出
            errors.append(f"{group_label} 第 1~{group_count} 张：{exc}")
            done += group_count
            continue

        # 服务商可能忽略 `n`（只回 1 张）⇒ 按缺张逐张补足（不重试已拿到的）
        while len(batch) < group_count:
            try:
                batch.append(
                    image_ai_client.generate_image(
                        model_config=model_config,
                        api_key=api_key,
                        prompt=group_prompt,
                        reference_images=group_refs,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{group_label} 第 {len(batch) + 1} 张：{exc}")
                break

        for index, result in enumerate(batch):
            # 单张重做时只跑一次（count=1），用**原始序号**标记结果。
            output_index = regen_index if regen_group else index + 1
            url = result.get("url") or ""
            asset = PublishAsset(
                row_type=ASSET_AIGC,
                user_id=current_user.id,
                asset_type="image",
                # ⚠️ 缺值口径：服务商没回本地路径时留空串（**不拿 url 冒充 local_path** ——
                # 那是两个语义，冒充会让「素材库」显示一条不存在的本地文件）。
                local_path=result.get("local_path") or "",
                url=url or None,
                prompt=prompt,
                model_name=model_config.model_name,
                # v2.30（§13.3.11）：**与商品建立关联** —— 用户口径「进入素材库后
                # 无法再区分与产品的关系」。关联是**资产的固有属性**，写在资产行上
                # （不是寄存在 tasks.payload —— 那是过程记录、有保留期）。
                item_id=payload.item_id,
                platform_account_id=payload.platform_account_id,
            )
            db.add(asset)
            db.flush()
            assets.append(
                {
                    "id": asset.id,
                    "local_path": asset.local_path,
                    "url": asset.url,
                    "prompt": asset.prompt,
                    "model_name": asset.model_name,
                    #: 分组标记（前端按它分两大块渲染；`product` / `detail`）。
                    "group": group_key,
                    "group_label": group_label,
                    #: 组内第几张（1 起）；单张重做时是**原始序号**（该组里的位置）。
                    "group_index": output_index,
                }
            )
            done += 1
            task.progress = 10 + int(90 * done / max(1, total))

    if not assets:
        # 一张都没成 ⇒ 整次失败（把上游原文如实带出，不吞）。
        task.status = "failed"
        task.progress = 100
        task.payload = {**(task.payload or {}), "error": "; ".join(errors)[:500]}
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"生成商品详情图失败：{'; '.join(errors) or '未知原因'}",
        )

    # 半成功：有成功的就按成功处理（`errors` 如实下发，前端给提示）。
    task.status = "completed"
    task.progress = 100
    task.payload = {
        **(task.payload or {}),
        "asset_ids": [a["id"] for a in assets],
        **({"errors": errors} if errors else {}),
    }
    db.commit()

    # v2.20：**分组汇总**（前端按它直接分两大块渲染）。
    # ⚠️ 由后端算好下发，而不是让前端自己 `groupBy` —— 分组口径（含「某组一张没成」
    # 时仍要出现该组）属于业务语义，放前端 = 同一套规则再实现一遍（DEF-044）。
    group_summaries = [
        {
            "group": item["key"],
            "group_label": item["label"],
            "requested": item["count"],
            "succeeded": len([a for a in assets if a["group"] == item["key"]]),
            "assets": [a for a in assets if a["group"] == item["key"]],
        }
        for item in groups
    ]

    return {
        # `url` = 首张（**向后兼容**旧前端：单张时代它是唯一返回值）。
        "url": assets[0]["url"] or "",
        "assets": assets,
        #: v2.20：按组的结果（**主图 / 详情图两大块**）。
        "groups": group_summaries,
        "errors": errors,
        "asset": assets[0],
    }

class SaveDetailImagesRequest(BaseModel):
    """把生成结果存进素材库（v2.29 §13.3.10）。

    用户口径（2026-10-03）：
    > 「**支持选择性保存**到指定素材库，支持**全部保存**到指定素材库」

    ⇒ **一个端点两种语义**：`asset_ids` 给几张就存几张（给全部 id = 「全部保存」）。
    不另开一个 `save-all` 端点 —— 那是同一件事的两份实现（DEF-044），
    且「全部」的定义在前端手里（它才知道当前展示了哪些）。
    """

    folder_id: int = Field(ge=1, description="素材库文件夹 id（material_folders.id）")
    asset_ids: list[int] = Field(min_length=1, max_length=200, description="要保存的生成结果 id 列表")
    #: v2.29：**资产 → 分组**（`{"7": "product", "8": "detail"}`）—— 由**前端**传。
    #:
    #: ⚠️ 为什么不是后端自己查：分组事实（哪张是主图、哪张是详情图）只存在于
    #: **生成那一刻的响应**里；落库的 `Task.payload` 只有平铺的 `asset_ids` +
    #: `group_plans`（**不含 asset 明细**），`PublishAsset` 更没有 group 列。
    #: 后端反查 = 查不到还得猜（猜错比「没分组」更坏）；前端手上正好有
    #: `assets[].group` ⇒ **谁有事实谁提供**。
    #: 缺省 / 缺失项 ⇒ 文件名用 `图-NN`（如实反映「不知道分组」，不造）。
    asset_groups: dict[str, str] | None = None

    # v2.13 的硬约束（见 test_shop_goods_sync 的参数化白名单）：请求模型必须
    # `extra="forbid"` —— 客户端多传字段时 422 炸出声，而不是被静默吞掉。
    model_config = ConfigDict(extra="forbid")

@router.post("/save-detail-images")
def save_detail_images(
    payload: SaveDetailImagesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**把生成结果保存到素材库**（v2.29 §13.3.10）。

    ## 落盘策略（目录与命名 —— 用户口径「你来决定」）

    ```
    <素材库根>/ai-生成/20261003-173045-a1b2/主图-01.png
    <素材库根>/ai-生成/20261003-173045-a1b2/详情图-01.png
    ```

    * 顶层 `ai-生成/`：与用户手放的文件**分界**，一眼能分辨机器产物；
    * **每次保存一个新子目录**（用户口径）：`时间戳-短码`，
      短码防「同一秒两批生成」撞进同一目录；
    * 文件名 `组名-两位序号.ext`：组名用中文（与界面一致），
      两位补零 ⇒ 字典序=自然序。

    ⚠️ **分组由前端传入**（`asset_groups`）—— 生成响应里每张都带 `group`，
    前端手上就有；后端反查不到（`Task.payload` 没有 asset 明细）。
    取不到就写 `图-NN`，**不猜**（缺值不造假）。
    """
    from backend.app.services.material_save_service import (
        MaterialSaveError,
        save_assets_to_folder,
    )

    # 资产 → 分组：**前端传的**（它手上有生成响应的 `assets[].group`）。
    # ⚠️ 后端**不反查**：`Task.payload` 只有平铺 `asset_ids`（无 asset 明细）、
    # `PublishAsset` 无 group 列 ⇒ 反查必落空、只能猜（猜错比没分组更坏）。
    asset_groups = {
        int(key): str(value)
        for key, value in (payload.asset_groups or {}).items()
        if str(key).isdigit()
    }

    try:
        result = save_assets_to_folder(
            db,
            user_id=current_user.id,
            folder_id=payload.folder_id,
            asset_ids=payload.asset_ids,
            asset_groups=asset_groups,
        )
    except MaterialSaveError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "MATERIAL_SAVE_FAILED", "message": str(exc)},
        ) from exc
    return result

class ItemGenerationsRequest(BaseModel):
    """查某商品的 AI 生成记录（v2.30 §13.3.11）。"""

    platform_account_id: int = Field(ge=1)
    item_id: str = Field(min_length=1, max_length=64)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    # 同上（v2.13）：未知字段必须 422，不静默吞。
    model_config = ConfigDict(extra="forbid")

@router.post("/item-generations")
def item_generations(
    payload: ItemGenerationsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**某商品的 AI 生成记录**（v2.30 §13.3.11）—— 商品列表「生成记录」入口的数据源。

    ## 为什么需要它（用户口径的原话）

    > 「根据商品信息生成素材如何与商品产生关联，在商品列表操作列新增查看入口」
    > 「因为**进入素材库后无法再区分与产品的关系**」

    素材库存的是**文件**，不是**来源**；而生成结果表 `aigc_media_assets`
    此前**没有商品字段** —— 关联**根本不存在**（结构缺口，不是「不好查」）。
    v2.30 给资产行加了 `item_id` + `platform_account_id`，本端点按它查。

    ## 口径

    * **只读**、纯本地（零平台请求）；
    * 按 `created_at` **倒序**（最近生成的在最前 —— 用户点进来最想看的）；
    * ⚠️ **只返回有关联的**（`item_id` 为空的老行不出现）：老图只有任务 payload
      里可能有 item_id，但「payload 里有」≠「这张图属于这个商品」
      ⇒ 不猜、不回溯（如实反映「不知道」，同「缺值不造假」口径）。

    ## 已知缺口

    **分组（主图 / 详情图）不在返回里** —— `PublishAsset` 没有 `group` 列，
    分组事实只在生成当时的响应里。本版按**时间**列，不按组分块
    （按组看是独立需求，要再加列 + 迁移）。
    """
    from backend.app.models import ASSET_AIGC, PublishAsset

    # 归属校验：账号必须是本人的（否则改个 id 就能看别人的生成记录）
    account = db.scalar(
        select(PlatformAccount).where(
            PlatformAccount.id == payload.platform_account_id,
            PlatformAccount.user_id == current_user.id,
        )
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ACCOUNT_NOT_FOUND", "message": "平台账号不存在或不属于当前用户"},
        )

    conditions = [
        PublishAsset.user_id == current_user.id,
        PublishAsset.platform_account_id == payload.platform_account_id,
        PublishAsset.item_id == payload.item_id,
    ]
    total = db.scalar(select(func.count(PublishAsset.id)).where(*conditions)) or 0
    rows = db.scalars(
        select(PublishAsset)
        .where(*conditions)
        .order_by(PublishAsset.created_at.desc(), PublishAsset.id.desc())
        .offset((payload.page - 1) * payload.page_size)
        .limit(payload.page_size)
    ).all()

    items = [
        {
            "id": row.id,
            "asset_type": row.asset_type,
            "url": row.url or "",
            "local_path": row.local_path or "",
            "prompt": (row.prompt or "")[:300],
            "model_name": row.model_name or "",
            "created_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S")
            if row.created_at
            else "",
        }
        for row in rows
    ]
    return {"items": items, "total": total, "page": payload.page, "page_size": payload.page_size}

MAX_EXPORT_ROWS: int = 5000

_UNSET = object()

class ItemsExportRequest(BaseModel):
    """CSV 导出（设计 20 §2.1 端点 6）。

    筛选字段与 `ShopItemsRequest` **逐字段一致**（复用同一份 `_apply_filters`，
    不重新实现 —— 两份筛选实现 = 「列表看到的」和「导出的」不是同一个集合，
    这是导出最常见的故障）。
    """

    model_config = ConfigDict(extra="forbid")
    platform_account_id: int = Field(ge=1)
    #: 关键词（与列表同口径：名称 / ID / 货号 / 商家编码，逗号分隔多条）。
    keyword: str = Field(default="", max_length=200)
    status: str = "all"
    price_from: float | None = Field(default=None, ge=0)
    price_to: float | None = Field(default=None, ge=0)
    stock_from: int | None = Field(default=None, ge=0)
    stock_to: int | None = Field(default=None, ge=0)
    sales_from: int | None = Field(default=None, ge=0)
    sales_to: int | None = Field(default=None, ge=0)
    note_count_op: str = "lt"
    note_count: int | None = Field(default=None, ge=0)
    create_time_from: str | int | None = None
    create_time_to: str | int | None = None
    sort_field: str = "create_time"
    order: str = "desc"

_PRODUCT_GROUPS_GONE = {
    "code": "PRODUCT_GROUPS_REMOVED",
    "message": "商品分组功能已整体移除（2026-10-05）。商品的本地组织请用商品列表的搜索条件 + 导出 CSV。",
}

@router.get("/product-groups", include_in_schema=False)
def list_product_groups():
    """**已退场**：商品分组列表（2026-10-05 分组功能整体移除）。"""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_PRODUCT_GROUPS_GONE)

@router.post("/product-groups", include_in_schema=False)
def create_product_group():
    """**已退场**：建分组（2026-10-05 分组功能整体移除）。"""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_PRODUCT_GROUPS_GONE)

@router.patch("/product-groups/{group_id}", include_in_schema=False)
def update_product_group(group_id: int):
    """**已退场**：改分组名（2026-10-05 分组功能整体移除）。"""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_PRODUCT_GROUPS_GONE)

@router.delete("/product-groups/{group_id}", include_in_schema=False)
def delete_product_group(group_id: int):
    """**已退场**：删分组（2026-10-05 分组功能整体移除）。"""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_PRODUCT_GROUPS_GONE)

_OPERATE_ACTION_PERMS: dict[str, str] = {
    #: 上架 / 下架 / 删除平台商品 —— 三个动作三个码（2026-10-26）。
    "on_shelf": "xhs:item:onShelf",
    "off_shelf": "xhs:item:offShelf",
    "delete": "xhs:item:delete",
}


def _assert_action_permitted(db: Session, current_user: User, action: str) -> None:
    """按 `action` **选码**再判权（唯一实现；`/items/operate` 的执行前咽喉）。

    ## 为什么不写成三个端点

    平台只给一个批量动作接口，拆成三个端点 = 三份「入队 + 门禁 + 任务行 + 轮询」
    的重复实现（DEF-044 同型）。所以端点一个、判权三份 —— 但**判据必须只有一处**，
    否则单行按钮与批量按钮迟早各判一套。

    ## 为什么不是「按归属/身份分流」

    设计 70：归属与身份**只许用来选码**，放不放行只由**权限判定**决定。
    这里正是「选码」：动作 → 码是固定映射，与谁在调用无关。

    ## ⚠️ 为什么走 `permission_cache.perms_of` 而不是 `core.perms.has_perm`

    `perms_of(db, user)` 是**查库 + TTL 缓存**的唯一实现，`require_perm` 依赖走的也是它。
    `core.perms.has_perm` 只是它的薄包装（2026-10-26 起必须传 `db=`；不传时对非 admin
    ——`raise`，因为早期版本在那里静默返回 False，导致非 admin 一律被拒且不报错）。
    本函数直接用 `perms_of` 是为了**一次查询判一个动作**，不走 db 参数传递。

    ⚠️ **未知 `action` 不在这里报错**（返回 None 放行）—— 白名单校验在上游
    先跑，重复判会得到两个 400 来源。调用方必须**先**做白名单、再调本函数。
    """
    from backend.app.core import permission_cache  # noqa: PLC0415
    from backend.app.core.perms import ROLE_ADMIN, normalize_role  # noqa: PLC0415

    code = _OPERATE_ACTION_PERMS.get(action)
    if code is None:  # 上游已拦过未知动作（400），这里只处理已知动作
        return
    #: admin 由代码派生全量码（`perms.role_perms` 的口径），不查授权表 ——
    #: 与 `require_perm` 完全一致，不另立一套。
    if normalize_role(getattr(current_user, "role", None)) == ROLE_ADMIN:
        return
    if code in permission_cache.perms_of(db, current_user):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "PERMISSION_DENIED",
            "message": "没有操作权限",
            "perm": code,
        },
    )


class ItemOperateRequest(BaseModel):
    """批量上架 / 下架 / 删除**平台商品**（2026-10-05 需求 7）。

    ⚠️ 与已退场的「删除本地台账行」是**两件不同的事**：

    | 请求 | 动谁 |
    |---|---|
    | `POST /items/operate` | **平台商品**（真实写操作），台账由下一轮同步对账 |

    ⚠️ 本请求是**交易侧写操作**：下架会影响在售商品、删除**不可恢复**。
    前端已做「显式确认 + 二次确认」（`Modal.confirm`），服务端这里只做参数校验收口。
    """

    model_config = ConfigDict(extra="forbid")
    platform_account_id: int = Field(ge=1)
    #: 动作（白名单 = `shop_item_operate_gates.ALL_ACTIONS`，未知 ⇒ 400 不猜）。
    action: str = Field(min_length=1, max_length=16)
    #: 商品 ID 列表（去重保序；空 ⇒ 400；超 `MAX_ITEMS_PER_TASK` ⇒ 400）。
    item_ids: list[str] = Field(min_length=1, max_length=MAX_OPERATE_ITEMS)

@router.post("/items/operate")
def shop_items_operate(
    payload: ItemOperateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    #: ⚠️ **这里刻意不挂静态 `require_perm` 依赖**（与本仓其它端点不同）——
    #: 原因是 FastAPI 的依赖**先于函数体执行**，而本端点要按 `action` 选码：
    #: * 挂一个码（如 `onShelf`）⇒ 有下架权的人点下架也 403；
    #: * 挂 `require_any_perm(三个)` ⇒ 未知动作本该报 400，却会先被报成 403
    #:   「没权限」，让用户去找管理员要一个根本不存在的码。
    #: ⇒ 判权整体下沉到函数内 `_assert_action_permitted`（白名单之后、任何写操作之前），
    #: 那里是**唯一**咽喉，且同样走 `permission_cache`（与 `require_perm` 同源）。
    #: 路由表的静态登记仍是 `xhs:item:onShelf`（`ROUTE_PERMS` 只能挂一个值），
    #: 守卫 `test_item_perms_split.py` 钉住「三个动作三个码且真的判」。
):
    """**批量上架 / 下架 / 删除平台商品**（2026-10-05 需求 7；**真实写操作**）。

    ## 三个动作 = 三个权限码（2026-10-26 用户口径「拆分成三个权限码」）

    平台只给**一个**批量动作接口（`batch_update_availability_v3` / 删除共用入口），
    所以端点只有一个、`action` 决定语义。路由表只能挂一个码
    ⇒ 静态挂 `xhs:item:onShelf` 作声明，真正的判据在这里按 `action` **选码**
    再 `has_perm`：

    | `action` | 权限码 |
    |---|---|
    | `on_shelf` | `xhs:item:onShelf` |
    | `off_shelf` | `xhs:item:offShelf` |
    | `delete` | `xhs:item:delete` |

    ⚠️ **不这样做的话**：只挂一个码（原 `xhs:item:operate`）⇒ 想让某人上架，
    就得连**平台删除**一起授予；这是红线「一按钮一码」要拦的形态。
    ⚠️ 归属/身份**只用来选码**，放不放行只由**权限判定**决定（设计 70）。

    ## 为什么是「手动触发式任务」而不是同步循环

    用户口径（需求 7）：统一走**手动触发式任务**，任务中心**任务列表不展示**，
    参照「清理无流量笔记」。⇒ 本端点只**建任务行 + 起后台线程 + 立即返回**
    （`task_id`），逐条执行在 `shop_item_operate_service.run_item_operate` 里，
    进度 / 结果由 `GET /items/operate/{task_id}` 给（前端弹窗轮询）。

    ## 门禁（需求 7「不支持的只记录不执行」）

    每条商品先过 `shop_item_operate_gates.support_reason(action, item)`：
    不支持的**只写进结果**（`result="skipped"` + 中文原因），**不发写请求**。
    判据的**唯一实现**在那一个模块（前端按列表下发的 `allowed_actions[]` 渲染按钮，
    不在前端复算）。

    ## 错误

    | 错误 | 语义 |
    |---|---|
    | 400 `QIANFAN_OPERATE_ACTION_UNSUPPORTED` | `action` 不在白名单（回支持列表） |
    | 400 `QIANFAN_ITEM_IDS_REQUIRED` | 去重去空白后为空（不静默成功） |
    | 400 `QIANFAN_ITEM_IDS_TOO_MANY` | 超上限（**不静默截断**） |
    | 400 `ARK_SESSION_REQUIRED` | 该账号没有可用的千帆登录态（写操作硬门禁） |
    | 403 `PERMISSION_DENIED` | 没有该 `action` 对应的权限码 |

    ⚠️ **写操作必须有登录态**（与「删本地台账行」不同）：后者零平台请求、无门禁；
    前者打平台，没令牌就是「点了也不会发生」。
    """
    from backend.app.services import shop_item_operate_service as operate_service
    from backend.app.services.shop_binding_service import has_ark_session

    account = _owned_account(db, current_user, payload.platform_account_id)

    action = (payload.action or "").strip()
    if action not in operate_service.gates.ALL_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_OPERATE_ACTION_UNSUPPORTED",
                "message": f"不支持的商品操作：{payload.action}",
                "supported": list(operate_service.gates.ALL_ACTIONS),
            },
        )

    #: 选码 → 判权。**顺序是判据**：先判「这个动作认不认得」（400），
    #: 再判「你有没有这个动作的码」（403）—— 反过来会把「参数写错」报成「没权限」。
    _assert_action_permitted(db, current_user, action)

    # 去重保序（**请求条数 ≠ 实际条数时不许虚报**，与平台删除同一条纪律）。
    seen: set[str] = set()
    item_ids: list[str] = []
    for raw in payload.item_ids:
        value = str(raw or "").strip()
        if value and value not in seen:
            seen.add(value)
            item_ids.append(value)
    if not item_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "QIANFAN_ITEM_IDS_REQUIRED", "message": "没有选择要操作的商品。"},
        )
    if len(item_ids) > MAX_OPERATE_ITEMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_ITEM_IDS_TOO_MANY",
                "message": f"单次最多操作 {MAX_OPERATE_ITEMS} 个商品，本次选了 {len(item_ids)} 个。",
                "limit": MAX_OPERATE_ITEMS,
            },
        )

    # ⚠️ 写平台 ⇒ 必须有该账号自己的 ark 令牌（不借会话、不发探活）。
    if not has_ark_session(db, account.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "ARK_SESSION_REQUIRED",
                "message": "该账号没有可用的千帆（ark）登录态，无法执行商品操作。"
                           "先在账号矩阵导入其千帆登录态后再试。",
            },
        )

    summary = operate_service.start_item_operate(
        platform_account_id=account.id,
        user_id=current_user.id,
        action=action,
        item_ids=item_ids,
    )
    return summary

@router.get("/items/operate/{task_id}")
def shop_items_operate_status(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """商品操作任务的**逐条结果**（前端弹窗轮询；需求 7「接口同步结果在弹窗中展示」）。

    非本用户的任务 ⇒ **404**（不泄露存在性 —— 与分组时代同一条纪律）。
    """
    from backend.app.services import shop_item_operate_service as operate_service

    snapshot = operate_service.task_snapshot(db, task_id, user_id=current_user.id)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="商品操作任务不存在。",
        )
    return snapshot

@router.post("/items/export")
def export_shop_items(
    payload: ItemsExportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """**导出商品 CSV**（设计 20 §2 端点 6；设计 75 S5 起**实时查平台**）。

    ## 与列表同一套判据（三条不能分叉）

    1. **筛选 / 排序 = 列表的**：直接复用 `POST /items` 的 `_realtime_items_page`
       （把导出请求体转成列表请求体）—— 两份筛选实现 = 「列表看到的」和「导出的」
       不是同一个集合，这是导出最常见的故障。
    2. **翻页直到取够或平台没有下一页**：每页走 `rt.list_products`（只读），
       页大小取平台上限 100。
    3. **字段 = 列表字段**：列头由 `CSV_HEADERS` 决定（与列表响应同一套键），
       逐字段从实时归一化 dict 取值 —— 不在本文件另写一套映射。

    ## 上限

    `> MAX_EXPORT_ROWS`（5000）⇒ 400 `QIANFAN_EXPORT_TOO_MANY`，**不静默截断**
    （截断的 CSV 是一个「看起来完整」的假全量，用户会拿它去对账）。

    ⚠️ 改造前这里是**零平台请求**的本地导出；现在每页一次平台请求，
    实时数据必然如此 —— 代价写在响应头 `X-Export-Pages` 里（可核对是否取全）。
    """
    import csv
    import io

    from fastapi.responses import StreamingResponse
    from backend.app.services import realtime_platform_query_service as rt

    account = _owned_account(db, current_user, payload.platform_account_id)

    try:
        time_from = _epoch_millis(payload.create_time_from, end_of_day=False)
        time_to = _epoch_millis(payload.create_time_to, end_of_day=True)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "QIANFAN_TIME_RANGE_INVALID", "message": str(error)},
        ) from error

    adapter = _realtime_adapter(db, account, adapter_factory)
    page_size = 100
    rows: list[list[str]] = []
    total = 0
    pages = 0
    while True:
        pages += 1
        if pages > MAX_EXPORT_PAGES:
            #: ⚠️ 翻页上限不是「取够就算了」：超过就说明命中量远超可导出的上限，
            #: 直接 400（下面的行数检查也会拦，这里先兜住「无限翻页」）。
            break
        result = rt.list_products(
            adapter,
            keyword=payload.keyword or None,
            status=payload.status or "all",
            page=pages,
            page_size=page_size,
            price_min_fen=_price_fen(payload.price_from),
            price_max_fen=_price_fen(payload.price_to),
            stock_min=payload.stock_from,
            stock_max=payload.stock_to,
            sales_min=payload.sales_from,
            sales_max=payload.sales_to,
            create_from=time_from,
            create_to=time_to,
            sort_field=payload.sort_field,
            order=payload.order,
        )
        items = result.get("items") or []
        if not items:
            break
        for item in items:
            rows.append(_csv_row(item))
        total = int(result.get("total") or 0)
        if len(rows) > MAX_EXPORT_ROWS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "QIANFAN_EXPORT_TOO_MANY",
                    "message": f"导出上限 {MAX_EXPORT_ROWS} 行，当前条件命中超过该上限，请收窄筛选。",
                    "limit": MAX_EXPORT_ROWS,
                    "actual": len(rows),
                },
            )
        if total and len(rows) >= total:
            break
        if len(items) < page_size:
            break

    buffer = io.StringIO()
    # ⚠️ BOM（UTF-8 BOM）：Excel 中文不乱码（国产办公场景的硬需求）。
    buffer.write("﻿")
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADERS)
    for row in rows:
        writer.writerow(row)

    # ⚠️ **文件名不得直接进响应头**（DEF-091）：Starlette 按 **latin-1** 编码响应头，
    # 中文 filename ⇒ `UnicodeEncodeError` ⇒ 导出**必 500**（任何一次导出都炸，
    # 不是边界情况）。修法 = RFC 5987 的 `filename*=UTF-8''<pct-encoded>`
    # 并**同时给一个纯 ASCII 的 `filename=` 回退**（老客户端认它）。
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ascii_name = f"shop-items-{stamp}.csv"
    utf8_name = quote(f"商品列表-{stamp}.csv", safe="")
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; filename*=UTF-8''{utf8_name}'
            ),
            "X-Export-Rows": str(len(rows)),
            "X-Export-Pages": str(pages),
        },
    )


#: CSV 列头（与列表响应**同一套键**，顺序即导出顺序）。
#: ⚠️ 改这里 = 改导出契约；列值由 `_csv_row` 从实时归一化 dict 取，两处必须同步。
CSV_HEADERS: list[str] = [
    "商品ID",
    "商品名称",
    "货号",
    "价格区间(分)",
    "库存",
    "销量",
    "近30天销量",
    "SKU数",
    "可售SKU数",
    "分类",
    "状态",
    "创建时间",
    "上架时间",
]

#: 导出翻页硬上限（100 页 × 100 条 = 1 万，已高于 `MAX_EXPORT_ROWS` 的 5000）。
MAX_EXPORT_PAGES: int = 100


def _csv_row(item: dict) -> list[str]:
    """实时归一化商品 → CSV 一行（**唯一实现**：键名与 `CSV_HEADERS` 一一对应）。"""

    def text(value: object) -> str:
        return "" if value is None else str(value)

    price_min = item.get("price_min_fen")
    price_max = item.get("price_max_fen")
    if price_min is None and price_max is None:
        price = ""
    elif price_max is None or price_max == price_min:
        price = text(price_min)
    else:
        price = f"{price_min}~{price_max}"
    return [
        text(item.get("item_id")),
        text(item.get("title")),
        text(item.get("article_no")),
        price,
        text(item.get("stock")),
        text(item.get("sales_total")),
        text(item.get("sales_30d")),
        text(item.get("sku_count")),
        text(item.get("on_sale_sku_count")),
        text(item.get("category")),
        text(item.get("status_label")),
        text(item.get("create_time")),
        text(item.get("on_shelf_time")),
    ]

