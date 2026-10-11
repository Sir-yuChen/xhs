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
    JOB_KEY_ORDER_SYNC,
    Optional,
    Order,
    SOURCE_ACCOUNT_CARD_ORDER_SYNC,
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
    has_ark_session,
    order_sync_service,
    payload_metrics_account_id,
    scheduled_job_runner,
    select,
    status,
    sync_task_service,
)

#: 本域独立 router —— 由 `__init__.py` 挂到父 router 下，**不带 prefix**
#:（路径里已含 `/picture-space` 这类段）。
#: ⚠️ 各域**各自**一个，不能共用 —— `include_router` 把父 router 挂到
#:    自己身上会让路由重复/错乱。
router = APIRouter()

class OrderListRequest(BaseModel):
    """订单列表入参（全部本地筛选，与 `ShopItemsRequest` 同口径）。"""

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

class OrderRemarkRequest(BaseModel):
    """写一条**本地**订单备注（设计 16 §7.2）。

    ⚠️ **不碰平台**：千帆的卖家备注写接口未授权（硬约束②只读）。
    这里写的备注只存在于本机台账，不同步到平台。
    """

    model_config = ConfigDict(extra="forbid")

    platform_account_id: int = Field(..., description="账号矩阵里的账号 id")
    order_no: str = Field(..., min_length=1, description="平台订单号")
    content: str = Field("", description="备注内容；空串 = 删除该备注")
    kind: str = Field("订单", description="备注类型：订单 / 包裹（设计 16 §7.2）")

class OrderExportRequest(OrderListRequest):
    """订单导出入参 —— **筛选条件与列表完全同构**（导出的就是筛出来的那批）。

    继承而非另写一份：两处各写一套筛选，必然分叉成「导出的和看到的不一样」。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    limit: int = Field(
        1000, ge=1, le=10000, description="导出行数上限（防一次拉爆内存）"
    )

class OrderSyncRequest(BaseModel):
    """触发一次订单同步（语义 = 手动跑一次定时任务，与 `/sync-shop-goods` 同构）。"""

    model_config = ConfigDict(extra="forbid")

    platform_account_id: int = Field(..., description="账号矩阵里的账号 id")
    max_pages: Optional[int] = Field(None, ge=1, description="空 = 翻完为止")

@router.post("/orders")
def order_list(
    payload: OrderListRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """订单列表（**本地台账**，零平台请求；未登录千帆也能看已同步的数据）。

    收件人信息默认**脱敏**（`mask_sensitive`）；`include_full=True` 才给原值。
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
    result = order_sync_service.list_orders(
        db,
        user_id=current_user.id,
        platform_account_id=account.id,
        status=status_key,
        keyword=(payload.keyword or "").strip(),
        date_from=payload.date_from,
        date_to=payload.date_to,
        page=payload.page,
        page_size=payload.page_size,
        include_full=bool(payload.include_full),
    )
    return {
        "platform_account_id": payload.platform_account_id,
        "source": "local",
        # 店铺维度：**直接透传服务层的值**，不在这里另算一份
        # （DEF-044：端点再拼一次 = 两份实现，且实测会漏字段 —— 上一版
        #  这里自己拼 `shop_name` 却把 `seller_id` 丢了，返回恒 `None`）。
        "shop_name": result["shop_name"],
        "seller_id": result["seller_id"],
        "status": status_key,
        "page": result["page"],
        "page_size": result["page_size"],
        "total": result["total"],
        "count": len(result["items"]),
        "items": result["items"],
    }

@router.post("/orders/sync")
def order_sync_trigger(
    payload: OrderSyncRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """触发一次订单同步（后台跑，与 `/sync-shop-goods` 同构）。

    响应 `{log_id, status, account_id}` —— 任务进度走**首查**
    `GET …/sync-shop-goods/status`（按 task_id 查，与商品同步同接口）。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    if not has_ark_session(db, account.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "ARK_SESSION_REQUIRED",
                "message": "该账号没有可用的千帆（ark）登录态，无法同步订单。"
                           "先在账号矩阵导入其千帆登录态后再同步。",
            },
        )
    max_pages = sync_task_service.clamp_pages(payload.max_pages)
    summary = order_sync_service.start_order_sync(
        payload.platform_account_id,
        user_id=current_user.id,
        source=SOURCE_ACCOUNT_CARD_ORDER_SYNC,
        max_pages=max_pages,
    )
    task_id = summary.get("task_id")
    log_id = None
    if task_id and not summary.get("reused"):
        log_id = scheduled_job_runner.run_targeted_job(
            user_id=current_user.id,
            job_key=JOB_KEY_ORDER_SYNC,
            task_id=int(task_id),
            task_type=order_sync_service.ORDER_SYNC_TASK_TYPE,
            account_id=payload.platform_account_id,
            params_extra={
                "account_id": payload.platform_account_id,
                "account_name": account.nickname or "",
                "max_pages": max_pages,
            },
            progress_probe=sync_task_service.task_progress_probe(int(task_id)),
        )
    return {**summary, "log_id": log_id}

@router.post("/orders/detail")
def order_detail_endpoint(
    payload: OrderDetailRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """订单详情（**本地台账**，零平台请求；收件人默认脱敏）。

    越权 / 不存在 ⇒ 404 且**不区分**「不是你的」与「不存在」——
    区分了就等于告诉调用方「这个订单号是别人的」。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    order = db.scalar(
        select(Order).where(
            Order.user_id == current_user.id,
            Order.platform_account_id == account.id,
            Order.order_no == payload.order_no,
        )
    )
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "ORDER_NOT_FOUND",
                "message": f"订单不存在或不属于当前账号：{payload.order_no}",
            },
        )
    shop_ctx = order_sync_service._shop_context(db, account.id)
    return {
        "platform_account_id": payload.platform_account_id,
        "source": "local",
        "shop_name": shop_ctx["shop_name"],
        "seller_id": shop_ctx["seller_id"],
        "order": order_sync_service.order_summary(
            db,
            order,
            include_full=bool(payload.include_full),
            shop_name=shop_ctx["shop_name"],
            seller_id=shop_ctx["seller_id"],
        ),
    }

@router.post("/orders/remark")
def order_remark_endpoint(
    payload: OrderRemarkRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """写一条**本地**订单备注（设计 16 §7.2）。

    ⚠️ **这是本机台账写入，不是平台写操作**：千帆卖家备注写接口未授权
    （硬约束②只读 + 用户 2026-09-19 授权清单仅「删除笔记」一项）。
    页面必须如实告知用户「此备注只存本地」。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    order = db.scalar(
        select(Order).where(
            Order.user_id == current_user.id,
            Order.platform_account_id == account.id,
            Order.order_no == payload.order_no,
        )
    )
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "ORDER_NOT_FOUND",
                "message": f"订单不存在或不属于当前账号：{payload.order_no}",
            },
        )
    row = order_sync_service.upsert_remark(
        db, user_id=current_user.id, order=order, content=payload.content, kind=payload.kind
    )
    db.commit()
    return {
        "ok": True,
        "deleted": row is None,
        "remark": (
            None
            if row is None
            else {
                "id": row.id,
                "kind": row.kind,
                "content": row.content,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        ),
    }

@router.post("/orders/export")
def order_export_endpoint(
    payload: OrderExportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """订单导出（**CSV 文本**，不落文件 —— 由前端触发下载）。

    ⚠️ **导出走完整值**（收件人姓名 / 手机 / 地址）：设计 16 §7.2 口径 ——
    履约数据完整落库，防护在**展示层**；打单 / 发货场景本来就需要原值。
    页面在触发导出前必须显式提示「将包含完整收件信息」。
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
    result = order_sync_service.list_orders(
        db,
        user_id=current_user.id,
        platform_account_id=account.id,
        status=status_key,
        keyword=(payload.keyword or "").strip(),
        date_from=payload.date_from,
        date_to=payload.date_to,
        page=1,
        page_size=min(payload.limit, 100),
        include_full=True,
    )
    # 上限之外再翻页捞（list_orders 单页上限 100）；`limit` 封顶 10000。
    rows = list(result["items"])
    page = 2
    while len(rows) < payload.limit and len(result["items"]) == 100:
        result = order_sync_service.list_orders(
            db,
            user_id=current_user.id,
            platform_account_id=account.id,
            status=status_key,
            keyword=(payload.keyword or "").strip(),
            date_from=payload.date_from,
            date_to=payload.date_to,
            page=page,
            page_size=100,
            include_full=True,
        )
        if not result["items"]:
            break
        rows.extend(result["items"])
        page += 1
    rows = rows[: payload.limit]
    return {
        "platform_account_id": payload.platform_account_id,
        "count": len(rows),
        "truncated": len(rows) >= payload.limit,
        "filename": order_sync_service.export_filename(),
        "csv": order_sync_service.orders_to_csv(rows),
    }

@router.get("/orders/metrics")
def order_metrics_endpoint(
    platform_account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """订单监控指标（设计 16 §7.7，本地聚合，零平台请求）。"""
    account = _owned_account(db, current_user, platform_account_id)
    return {
        "platform_account_id": payload_metrics_account_id(account),
        "metrics": order_sync_service.order_metrics(
            db, user_id=current_user.id, platform_account_id=account.id
        ),
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

@router.post("/after-sales/handle")
def after_sale_handle(
    payload: AfterSaleHandleRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**售后审核**（同意 / 拒绝；设计 16 §7.4 B）—— **真实平台写、不可撤回**。

    与发货同一把尺子：两层确认 → 门禁（必须 `待处理`）→ 打平台 → 落库。
    """
    from backend.app.services import order_write_service as ows

    account = _owned_account(db, current_user, payload.platform_account_id)
    try:
        row = _owned_after_sale(db, current_user, account.id, payload.after_sale_id)
    except Exception as exc:  # noqa: BLE001
        raise _order_write_error_to_http(exc) from exc
    adapter = _ark_adapter_for(db, account, adapter_factory)
    try:
        result = ows.handle_after_sale(
            db,
            adapter=adapter,
            after_sale=row,
            action=payload.action,
            reject_reason=payload.reject_reason,
            confirmed=payload.confirmed,
            confirm_token=payload.confirm_token,
        )
    except Exception as exc:  # noqa: BLE001
        raise _order_write_error_to_http(exc) from exc
    return {"platform_account_id": account.id, "source": "platform", "result": result}
