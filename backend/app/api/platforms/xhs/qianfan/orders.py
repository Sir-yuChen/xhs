"""千帆 · 订单与售后（一期只读 + 二期写操作）

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
    ConfigDict,
    Depends,
    Field,
    HTTPException,
    Optional,
    Session,
    User,
    _ark_adapter_for,
    _order_write_error_to_http,
    _owned_account,
    _owned_after_sale,
    _owned_order,
    _shop_display_name,
    get_current_user,
    get_db,
    get_qianfan_api_adapter_factory,
    order_sync_service,
    realtime_query_error_to_http,
    payload_metrics_account_id,
    status,
)

#: 本域独立 router —— 由 `__init__.py` 挂到父 router 下，**不带 prefix**
#:（路径里已含 `/picture-space` 这类段）。
#: ⚠️ 各域**各自**一个，不能共用 —— `include_router` 把父 router 挂到
#:    自己身上会让路由重复/错乱。
from backend.app.core.perm_deps import require_perm  # noqa: E402

router = APIRouter()

#: 订单详情「订单号 → package_id」的翻页上限：一页 20 条 × 20 页 = 400 单。
#: 超出如实 404，**不无限翻**（无刹车全量在本项目是禁止的）。
DETAIL_LOOKUP_MAX_PAGES = 20
DETAIL_LOOKUP_PAGE_SIZE = 20

#: 监控指标最多翻多少页（100 条/页 × 30 页 = 3000 单，单账号 365 天量级足够）。
#: 「扫不全」是**如实可见**的：响应带 `scanned`，不假装统计了全部。
METRICS_MAX_PAGES = 30

class OrderListRequest(BaseModel):
    """订单列表入参。

    ⚠️ 2026-10-27 起**不是本地筛选**了：`date_from` / `date_to` 是**发给平台的查询窗口**
    （两端都空 ⇒ 近 365 天），`status` / `keyword` 仍是**按行再筛**（平台的状态过滤
    走数字码，未真机取证）。原句「全部本地筛选」已被设计 75 §4.4 取代。
    """

    model_config = ConfigDict(extra="forbid")

    platform_account_id: int = Field(..., description="账号矩阵里的账号 id")
    status: str = Field("", description="订单状态白名单（ORDER_STATUSES 的 key，空 = 不筛）")
    keyword: str = Field("", description="订单号 / 买家昵称 / 商品名 / SKU / 快递单号 模糊搜")
    date_from: Optional[str] = Field(None, description="YYYY-MM-DD，按 created_at 筛")
    date_to: Optional[str] = Field(None)
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)
    include_full: bool = Field(
        False,
        description="True 才回完整收件人信息（姓名 / 手机 / 地址）；默认脱敏。",
    )

class OrderDetailRequest(BaseModel):
    """订单详情入参（**本地台账**，零平台请求）。"""

    model_config = ConfigDict(extra="forbid")

    platform_account_id: int = Field(..., description="账号矩阵里的账号 id")
    order_no: str = Field(..., min_length=1, description="平台订单号")
    include_full: bool = Field(
        False, description="True 才回完整收件人信息；默认脱敏。"
    )

# ⚠️ `OrderRemarkRequest` 已随订单备注退场（设计 75，2026-10-27）——
# 用户口径「订单备注不保留直接移除」：入参模型、端点、`order_remarks` 表、前端备注区
# 四处同批删除。原模型保留在 git 历史里，**不要恢复**。


class OrderExportRequest(OrderListRequest):
    """订单导出入参 —— **筛选条件与列表完全同构**（导出的就是筛出来的那批）。

    继承而非另写一份：两处各写一套筛选，必然分叉成「导出的和看到的不一样」。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    limit: int = Field(
        1000, ge=1, le=10000, description="导出行数上限（防一次拉爆内存）"
    )

# ⚠️ `OrderSyncRequest` 已随 `POST /orders/sync` 退场（设计 75，2026-10-27）——
# 订单域全实时，没有「触发一次同步」这件事。原模型保留在 git 历史里。


def _find_package_by_order_no(
    adapter: Any, order_no: str, *, lookback_days: int = 365
) -> dict | None:
    """按**订单号**在平台订单列表里找到对应 package（找不到 ⇒ `None`）。

    ## 为什么需要这一步

    平台详情的入参是 `package_id`，而前端手里只有订单号（`orderId`）——
    传订单号平台直接回 `code=-1, required param check: package_id`。

    ## 窗口与上限

    `order/page` **不给窗口就挂死 15 秒**（真机实测）⇒ 窗口默认近 `lookback_days`
    天。翻页上限 :data:`DETAIL_LOOKUP_MAX_PAGES`（400 单），超出如实返回 `None`，
    **不无限翻**（本项目禁止「无刹车全量」）。

    平台失败照原样抛 `RealtimeQueryError`（凭据失效 / 网络），由调用方转结构化 HTTP ——
    「查不到」与「问不到」是两件事，不能都变成 404。
    """
    from backend.app.services import realtime_platform_query_service as rt

    wanted = str(order_no or "").strip()
    if not wanted:
        return None
    for page_no in range(1, DETAIL_LOOKUP_MAX_PAGES + 1):
        result = rt.list_orders(
            adapter,
            page=page_no,
            page_size=DETAIL_LOOKUP_PAGE_SIZE,
            lookback_days=lookback_days,
        )
        rows = result.get("items") or []
        for package in rows:
            if str(package.get("order_no") or "") == wanted:
                return package
        if not rows or result.get("has_more") is False:
            break
    return None


@router.post("/orders")
def order_list(
    payload: OrderListRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """订单列表 —— **千帆实时查询**（设计 75 §4.4；零本地副本）。

    | 约定 | 做法 |
    |---|---|
    | 数据源 | `rt.list_orders`（`fulfillment/order/page`，只读） |
    | 时间窗口 | `date_from` / `date_to` 给了就按给定区间；**两端都空 ⇒ 近 365 天**（平台对无界窗口挂死 15 秒） |
    | 无界 / 反向窗口 | 400 `ORDER_WINDOW_REQUIRED`，**一个平台请求都不发**（`resolve_order_window` 在发请求前判） |
    | 状态白名单 | 未知状态键 ⇒ 400 `QIANFAN_ORDER_STATUS_UNSUPPORTED`（与改造前同判据） |
    | 状态 / 关键词筛选 | **按行在本地再筛**（平台的状态过滤走数字码 `status: []`，未真机取证 ⇒ 不臆造）；`total` 是平台报的总数，筛选后的条数以 `count` 为准 |
    | 隐私 | 平台回的是**脱敏原值**（`及*` / `184***3034`）；`include_full` 保留在契约里，但平台不给原值 ⇒ 不补不猜（缺值口径） |

    「无界不响应」是本端点的硬判据：给无界窗口时平台**不报错而是挂到超时**，
    所以必须先算窗口、非法就 400 返回，不发请求。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    status_key = (payload.status or "").strip()
    if status_key and status_key not in order_sync_service.ORDER_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_ORDER_STATUS_UNSUPPORTED",
                "message": f"不支持的订单状态：{status_key}",
                "supported": ["", *order_sync_service.ORDER_STATUSES],
            },
        )
    from backend.app.services import realtime_platform_query_service as rt

    adapter = _ark_adapter_for(db, account, adapter_factory)
    try:
        live = rt.list_orders(
            adapter,
            start_time=payload.date_from or None,
            end_time=payload.date_to or None,
            page=payload.page,
            page_size=payload.page_size,
        )
    except rt.RealtimeQueryError as error:
        raise realtime_query_error_to_http(error) from error

    items = _annotate_order_rows(
        live.get("items") or [],
        status_key=status_key,
        keyword=(payload.keyword or "").strip(),
    )
    return {
        "platform_account_id": account.id,
        "source": live.get("source") or "platform",
        "shop_name": _shop_display_name(db, account),
        "seller_id": account.seller_id or "",
        "status": status_key,
        "page": live.get("page") or payload.page,
        "page_size": live.get("page_size") or payload.page_size,
        "total": live.get("total"),
        #: 筛后条数（`total` 是平台的窗口总数，两者不同 —— 都下发，前端按 count 渲染分页当前页）。
        "count": len(items),
        "items": items,
        "window": {
            "start_time": live.get("start_time"),
            "end_time": live.get("end_time"),
        },
    }


def _annotate_order_rows(rows: list[dict], *, status_key: str = "", keyword: str = "") -> list[dict]:
    """实时订单行 → 前端契约（补 `status_key` + 按状态 / 关键词**行内再筛**）。

    ## 为什么要补 `status_key`

    平台给的是数字 `status`（`2` / `4` / `7`…）+ 中文 `status_desc`。改造前本地行有
    `status` 这一列，存的是**归一化后的中文键**（`待发货` / `待收货`…）—— 前端表格与
    标签颜色都按下标取色。实时行若直接把数字塞进 `status`，页面会渲染成裸数字且取
    不到颜色（静默的观感退化）。所以：`status` = 归一化中文键（与改造前**同值**），
    `platform_status` = 平台原始码。

    归一的**唯一实现**是 `models.order.resolve_order_status`（售后中优先于发货态），
    不在这里另写一份。

    ## 为什么在本地再筛

    `order/page` 的状态过滤走数字码数组 + `order_tag_list`，未真机取证 —— 只读边界内
    不臆造参数。所以状态与关键词按行筛：诚实、可测，且与前端语义一致
    （关键词匹配订单号 / 买家 / 运单号 / 商品名，与改造前 `list_orders` 同一匹配域）。
    """
    from backend.app.models.order import resolve_order_status
    from backend.app.services.order_sync_service import mask_sensitive

    word = keyword.lower()
    annotated: list[dict] = []
    for row in rows:
        status_text = resolve_order_status(row.get("status"), row.get("after_sale_status"))
        item = dict(row)
        item["platform_status"] = row.get("status")
        item["status"] = status_text
        receiver = item.get("receiver") if isinstance(item.get("receiver"), dict) else {}
        #: 收件人三列与改造前同形（`order_summary` 的字段名）；平台已脱敏 ⇒ 原样下发。
        item["receiver_name"] = mask_sensitive(receiver.get("name") or "")
        item["receiver_phone"] = mask_sensitive(receiver.get("phone") or "")
        item["receiver_address"] = mask_sensitive(receiver.get("address") or "")
        item["buyer_nick"] = item.get("buyer") or ""
        #: 商品行补齐前端读的键（平台 `skus[]` → `items[]` 契约）
        item["items"] = [
            {
                "item_id": sku.get("item_id") or "",
                "item_name": sku.get("name") or "",
                "sku": sku.get("spec") or "",
                "quantity": sku.get("quantity"),
                "unit_price": sku.get("unit_price"),
                "amount": sku.get("amount"),
                "item_image": sku.get("image") or "",
            }
            for sku in (item.get("skus") or [])
            if isinstance(sku, dict)
        ]
        if status_key and status_text != status_key:
            continue
        if word:
            haystack = " ".join(
                str(part or "")
                for part in (
                    item.get("order_no"),
                    item.get("buyer"),
                    item.get("buyer_nick"),
                    item.get("tracking_no"),
                    " ".join(str(sku.get("name") or "") for sku in (item.get("skus") or [])),
                    " ".join(str(sku.get("spec") or "") for sku in (item.get("skus") or [])),
                )
            ).lower()
            if word not in haystack:
                continue
        annotated.append(item)
    return annotated


@router.post("/orders/detail")
def order_detail_endpoint(
    payload: OrderDetailRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
    #: ⚠️ 写方法（POST）**必须挂权限码** —— 启动守卫 `route_guard` 会在缺码时
    #: 拒绝启动（「缺权限码的写接口对任何登录用户开放」）。
    _perm: None = Depends(require_perm("xhs:order:view")),
):
    """订单详情（**实时查千帆**，设计 75 §4.4）。

    | 约定 | 做法 |
    |---|---|
    | 数据源 | `fulfillment/order/detail`（只读），本地零副本 |
    | 入参 | 前端给**订单号**；平台详情要 `package_id` ⇒ 先按订单号在列表里定位 |
    | 越权 / 不存在 | 404 且**不区分**「不是你的」与「不存在」 |
    | 收件人 | 平台可能部分脱敏（`及*` / `184***3034`）；给什么给什么，不猜不补 |

    ⚠️ 两个真机坑（2026-10-27 实测）：
    1. 详情入参是 **`package_id`** 不是订单号（传 orderId 平台回 `code=-1`）；
    2. 详情响应**没有订单号**、且字段是**下划线**（`package_id`），与列表的驼峰不同
       ⇒ 归一化时必须把列表里的订单号带进去，否则整页空白**且不报错**。
    """
    from backend.app.services import realtime_platform_query_service as rt

    account = _owned_account(db, current_user, payload.platform_account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)

    package = _find_package_by_order_no(adapter, payload.order_no)
    if package is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "ORDER_NOT_FOUND",
                "message": f"订单不存在或不属于当前账号：{payload.order_no}",
            },
        )

    detail = rt.get_order_detail(
        adapter,
        package_id=package.get("package_id") or "",
        order_no=str(package.get("order_no") or payload.order_no),
    )
    shop_ctx = order_sync_service._shop_context(db, account.id)
    return {
        "platform_account_id": payload.platform_account_id,
        "source": "platform",
        "shop_name": shop_ctx["shop_name"],
        "seller_id": shop_ctx["seller_id"],
        "order": detail.get("item") or {},
        "summary": package,
        #: ⚠️ **没有 `remark` 字段** —— 订单备注整条退场（设计 75）。
    }


@router.get("/orders/metrics")
def order_metrics_endpoint(
    platform_account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """订单监控指标（设计 16 §7.7）—— **实时聚合**（设计 75 §4.4 O6）。

    口径（与改造前一致，只是数据源从本地台账换成平台的同一窗口）：

    * 统计对象 = 平台近 `ORDER_DEFAULT_LOOKBACK_DAYS`（365）天的订单，翻页上限
      `METRICS_MAX_PAGES`，**与列表页默认窗口同源** —— 两处窗口不同会让
      「列表 30 单、监控 40 单」；
    * 平台失败 ⇒ 502 结构化原因，**不回退 0**（0 会被读成「真没订单」）；
    * 响应带 `scanned`：统计覆盖了多少单**如实可见**，不假装扫了全部。
    """
    account = _owned_account(db, current_user, platform_account_id)
    from backend.app.services import realtime_platform_query_service as rt

    adapter = _ark_adapter_for(db, account, adapter_factory)
    rows: list[dict] = []
    seen: set[str] = set()
    try:
        for page_no in range(1, METRICS_MAX_PAGES + 1):
            live = rt.list_orders(adapter, page=page_no, page_size=rt.MAX_PAGE_SIZE)
            batch = live.get("items") or []
            fresh = [row for row in batch if str(row.get("order_no")) not in seen]
            if not fresh:
                break
            for row in fresh:
                seen.add(str(row.get("order_no")))
                rows.append(row)
            if live.get("has_more") is False:
                break
    except rt.RealtimeQueryError as error:
        raise realtime_query_error_to_http(error) from error
    return {
        "platform_account_id": payload_metrics_account_id(account),
        "source": rt.SOURCE_PLATFORM,
        "scanned": len(rows),
        "metrics": rt.order_metrics(rows),
    }


@router.post("/orders/export")
def order_export_endpoint(
    payload: OrderExportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """订单导出（**CSV 文本**，不落文件 —— 由前端触发下载）—— 实时取数。

    ⚠️ 导出含**平台能给的**全部收件信息（平台回的是脱敏原值，本系统无从补全）；
    打单 / 发货场景需要原值时去千帆后台。页面在触发前仍显式提示。

    翻页**有上限**：`limit`（≤10000）+ 每页 100，最多翻 `ceil(limit/100)` 页后停 ——
    平台没有 `end_tag`，按 `has_more`（`total` 推断）与空页双判据收敛。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    status_key = (payload.status or "").strip()
    if status_key and status_key not in order_sync_service.ORDER_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_ORDER_STATUS_UNSUPPORTED",
                "message": f"不支持的订单状态：{status_key}",
                "supported": ["", *order_sync_service.ORDER_STATUSES],
            },
        )
    from backend.app.services import realtime_platform_query_service as rt

    adapter = _ark_adapter_for(db, account, adapter_factory)
    page_size = min(100, payload.limit)
    max_pages = max(1, -(-payload.limit // page_size))
    rows: list[dict] = []
    try:
        for page_no in range(1, max_pages + 1):
            live = rt.list_orders(
                adapter,
                start_time=payload.date_from or None,
                end_time=payload.date_to or None,
                page=page_no,
                page_size=page_size,
            )
            batch = _annotate_order_rows(
                live.get("items") or [],
                status_key=status_key,
                keyword=(payload.keyword or "").strip(),
            )
            if not (live.get("items") or []):
                break
            rows.extend(batch)
            if len(rows) >= payload.limit or live.get("has_more") is False:
                break
    except rt.RealtimeQueryError as error:
        raise realtime_query_error_to_http(error) from error
    rows = rows[: payload.limit]
    return {
        "platform_account_id": account.id,
        "count": len(rows),
        "truncated": len(rows) >= payload.limit,
        "filename": order_sync_service.export_filename(),
        "csv": order_sync_service.orders_to_csv(rows),
    }


class OrderShipRequest(BaseModel):
    """**单个订单发货**（设计 16 §7.4 A；真实写、不可撤回）。"""

    model_config = ConfigDict(extra="forbid")

    platform_account_id: int = Field(..., description="账号矩阵里的账号 id")
    order_no: str = Field(..., min_length=1, description="平台订单号")
    company_code: str = Field("", description="物流公司编码（express_company_code）")
    tracking_no: str = Field("", description="运单号（express_no）")
    confirmed: bool = Field(False, description="第一层确认：用户已知这是真实写操作")
    confirm_token: str = Field("", description="第二层确认：预览接口下发的指纹，原样回传")

class OrderShipBatchItem(BaseModel):
    """批量发货的一条（订单号 + 各自物流 / 运单号）。"""

    model_config = ConfigDict(extra="forbid")

    order_no: str = Field(..., min_length=1)
    company_code: str = Field("")
    tracking_no: str = Field("")

class OrderShipBatchRequest(BaseModel):
    """**批量发货**（设计 16 §7.4 A.2/A.6）—— 一次确认覆盖整批。"""

    model_config = ConfigDict(extra="forbid")

    platform_account_id: int = Field(..., description="账号矩阵里的账号 id")
    items: list[OrderShipBatchItem] = Field(default_factory=list)
    confirmed: bool = Field(False)
    confirm_token: str = Field("")

class AfterSaleHandleRequest(BaseModel):
    """**售后审核**（设计 16 §7.4 B；真实写、不可撤回）。"""

    model_config = ConfigDict(extra="forbid")

    platform_account_id: int = Field(..., description="账号矩阵里的账号 id")
    after_sale_id: str = Field(..., min_length=1, description="千帆售后单 ID")
    action: str = Field(..., description="agree（同意）/ reject（拒绝）")
    reject_reason: str = Field("", description="拒绝理由（action=reject 时必填）")
    confirmed: bool = Field(False)
    confirm_token: str = Field("")

class OrderWritePreviewRequest(BaseModel):
    """**写操作预览**（算二次确认凭据；**纯本地、零平台请求**）。

    前端在确认弹窗里先调它拿到 `confirm_token` + 影响面，用户确认后原样带回。
    """

    model_config = ConfigDict(extra="forbid")

    platform_account_id: int = Field(..., ge=1)
    kind: str = Field(..., description="ship / ship_batch / after_sale")
    order_no: str = Field("", description="单笔发货的目标单号")
    items: list[OrderShipBatchItem] = Field(default_factory=list, description="批量发货用")
    after_sale_id: str = Field("", description="售后审核用")
    action: str = Field("", description="售后审核动作")
    reject_reason: str = Field("")
    company_code: str = Field("")
    tracking_no: str = Field("")

@router.post("/orders/ship/preview")
def order_ship_preview(
    payload: OrderWritePreviewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**发货 / 售后预览**（纯本地、零平台请求）—— 算二次确认凭据 + 影响面。

    前端在确认弹窗里先调它：拿到 `confirm_token` 与「将对哪几笔做什么」，
    用户点「确认」后把 token 原样回传给真正的写端点。
    ⚠️ 本端点**不校验状态门禁**（那是写端点的事），只做「内容 → 指纹」。
    """
    from backend.app.services import order_write_service as ows

    account = _owned_account(db, current_user, payload.platform_account_id)
    kind = (payload.kind or "").strip()

    if kind == "ship":
        try:
            order = _owned_order(db, current_user, account.id, payload.order_no)
        except Exception as exc:  # noqa: BLE001
            raise _order_write_error_to_http(exc) from exc
        # ⚠️ 用**同一份素材构造**（`ows.ship_confirm_material`）算指纹 ——
        # 这里另拼一份 = 预览下发的 token 永远过不了写端点的校验。
        material = ows.ship_confirm_material(
            order,
            company_code=payload.company_code,
            tracking_no=payload.tracking_no,
        )
        return {
            "platform_account_id": account.id,
            "shop_name": _shop_display_name(db, account),
            "kind": kind,
            "count": 1,
            "confirm_token": ows.confirm_token_for(material),
            "items": [material],
        }

    if kind == "ship_batch":
        specs = []
        for raw in payload.items:
            try:
                order = _owned_order(db, current_user, account.id, raw.order_no)
            except Exception as exc:  # noqa: BLE001
                raise _order_write_error_to_http(exc) from exc
            specs.append(
                {
                    "order_no": order.order_no,
                    "package_id": order.package_id,
                    "company_code": raw.company_code,
                    "tracking_no": raw.tracking_no,
                }
            )
        material = ows.batch_ship_confirm_material(specs)
        return {
            "platform_account_id": account.id,
            "shop_name": _shop_display_name(db, account),
            "kind": kind,
            "count": len(specs),
            "confirm_token": ows.confirm_token_for(material),
            "items": specs,
        }

    if kind == "after_sale":
        try:
            row = _owned_after_sale(db, current_user, account.id, payload.after_sale_id)
        except Exception as exc:  # noqa: BLE001
            raise _order_write_error_to_http(exc) from exc
        material = ows.after_sale_confirm_material(
            row, action=payload.action, reject_reason=payload.reject_reason
        )
        return {
            "platform_account_id": account.id,
            "shop_name": _shop_display_name(db, account),
            "kind": kind,
            "count": 1,
            "confirm_token": ows.confirm_token_for(material),
            "items": [material],
        }

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": "ORDER_WRITE_BAD_KIND", "message": f"不支持的预览类型：{kind}"},
    )

@router.post("/orders/ship")
def order_ship(
    payload: OrderShipRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**发货单笔订单**（设计 16 §7.4 A）—— **真实平台写、不可撤回**。

    | 约定 | 做法 |
    |---|---|
    | 两层确认 | `confirmed=True` **且** `confirm_token` = 请求体指纹；缺一 ⇒ 400，**一次都不打平台** |
    | 门禁 | 必须 `待发货`，否则 400 `ORDER_NOT_SHIPPABLE`，**一次都不打平台** |
    | 归属 | 越权 / 不存在 ⇒ 404 |
    | 失败 | 先 `rollback()`，状态与物流行都不变，原因原样带出可重试 |
    | 自动触发 | **绝不**（不进定时任务 / 工作流） |
    """
    from backend.app.services import order_write_service as ows

    account = _owned_account(db, current_user, payload.platform_account_id)
    # ⚠️ 归属校验的 `OrderWriteError` 也要映射成 404（否则逃逸成 500，实测踩过）
    try:
        order = _owned_order(db, current_user, account.id, payload.order_no)
    except Exception as exc:  # noqa: BLE001
        raise _order_write_error_to_http(exc) from exc
    # ⚠️ 适配器解析**在 try 之外**：拿不到 ark 令牌要回 400 结构化原因，
    # 不能被下面的通用异常处理吞成 500（实测踩过）。
    adapter = _ark_adapter_for(db, account, adapter_factory)
    try:
        result = ows.ship_order(
            db,
            adapter=adapter,
            order=order,
            company_code=payload.company_code,
            tracking_no=payload.tracking_no,
            confirmed=payload.confirmed,
            confirm_token=payload.confirm_token,
        )
    except Exception as exc:  # noqa: BLE001 - 统一映射成结构化 HTTP
        raise _order_write_error_to_http(exc) from exc
    return {"platform_account_id": account.id, "source": "platform", "result": result}

@router.post("/orders/ship-batch")
def order_ship_batch(
    payload: OrderShipBatchRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**批量发货**（设计 16 §7.4 A.2/A.6）—— 逐条独立、回执**分层**。

    ⚠️ 上限 `MAX_BATCH_SHIP`（+ 前端同值）；空 ⇒ 400；去重保序（同 `items/delete`）。
    一条失败不拖垮整批（`succeeded[]` / `failed[]` 各自带原因）。
    """
    from backend.app.services import order_write_service as ows

    account = _owned_account(db, current_user, payload.platform_account_id)
    seen: set[str] = set()
    order_nos: list[str] = []
    for raw in payload.items:
        key = str(raw.order_no or "").strip()
        if key and key not in seen:
            seen.add(key)
            order_nos.append(key)
    if not order_nos:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "ORDER_SHIP_ITEMS_REQUIRED", "message": "没有选择要发货的订单。"},
        )
    if len(order_nos) > ows.MAX_BATCH_SHIP:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "ORDER_SHIP_ITEMS_TOO_MANY",
                "message": f"单次最多发货 {ows.MAX_BATCH_SHIP} 笔，本次选了 {len(order_nos)} 笔。",
                "limit": ows.MAX_BATCH_SHIP,
            },
        )
    try:
        orders = [_owned_order(db, current_user, account.id, no) for no in order_nos]
    except Exception as exc:  # noqa: BLE001
        raise _order_write_error_to_http(exc) from exc
    adapter = _ark_adapter_for(db, account, adapter_factory)
    specs = {
        str(raw.order_no).strip(): {
            "company_code": (raw.company_code or "").strip(),
            "tracking_no": (raw.tracking_no or "").strip(),
        }
        for raw in payload.items
        if str(raw.order_no or "").strip()
    }
    result = ows.ship_orders_batch(
        db,
        adapter_for=lambda order: adapter,
        orders=orders,
        specs_by_order_no=specs,
        confirmed=payload.confirmed,
        confirm_token=payload.confirm_token,
    )
    return {"platform_account_id": account.id, "source": "platform", **result}

@router.post("/after-sales/handle", include_in_schema=False)
def after_sale_handle_removed():
    """**已退场**（设计 75，2026-10-27）：售后审核端点随 `after_sales` 表一起移除。

    ## 为什么必须退场（不是「还没做」）

    本端点原本靠**本地 `after_sales` 行**做三件事：归属校验（越权 404）、状态门禁
    （必须「待处理」）、以及 `confirm_token` 指纹。订单域改实时后本地不再有售后行 ——
    继续留着它就是「拿一个查不到的表做门禁」，每次调用都以 404 结束，看起来像 bug。

    平台侧写接口（售后同意 / 拒绝）本身仍存在，但当前**没有可用的门禁与确认数据源**：
    订单详情实时返回的 `after_sale_list` 尚未真机对过字段（`after_sale_id` / 状态码），
    在没有取证的前提下动平台写操作 = 拿用户真实售后单试错。

    ⇒ 结论：**在有真机取证之前不做**（设计 75 §4.4 O2 的同一条纪律）。要恢复它，
    先补「实时详情 → 售后单门禁」的取证与测试，再一次性落地；不要恢复本墓碑。

    错误码：`ORDER_AFTER_SALE_HANDLE_REMOVED`。
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "ORDER_AFTER_SALE_HANDLE_REMOVED",
            "message": "售后审核已退场（设计 75）：订单域改为实时查询后不再有本地售后单，"
            "请在千帆后台处理售后。",
        },
    )


@router.post("/orders/sync", include_in_schema=False)
def order_sync_removed():
    """**已退场**（设计 75，2026-10-27）：订单同步任务全链路移除。

    原 `POST /qianfan/orders/sync` 把平台订单落库到 `orders` / `order_items` /
    `order_shipments` / `after_sales`，列表再读本地 —— 这正是「本地与平台迟早
    不一致」的根源（同步有间隔、失败会静默、平台侧改了本地不知道）。

    现在订单列表与详情**直接实时查千帆**（`fulfillment/order/page` +
    `/detail`），订单是**平台的事实**，不该有本地副本。
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "ORDER_SYNC_REMOVED",
            "message": "订单同步已退场（设计 75）：订单列表现在实时查询千帆，不再落本地。",
        },
    )


@router.post("/orders/remark", include_in_schema=False)
def order_remark_removed():
    """**已退场**（设计 75，2026-10-27）：订单备注功能移除。

    用户口径（2026-10-26）：「订单备注不保留直接移除」。

    改造前备注存在本地 `order_remarks` 表（平台没有备注字段），它是本方案里
    唯一一处「平台不存在但我方要留」的订单数据 —— 用户决定不留，所以整条退场：
    表 DROP、端点 410、前端备注区删除。
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "ORDER_REMARK_REMOVED",
            "message": "订单备注功能已移除（设计 75）：订单域全实时，本地不保留订单数据。",
        },
    )
