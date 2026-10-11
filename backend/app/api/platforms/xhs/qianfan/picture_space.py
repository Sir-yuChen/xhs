"""千帆 · 图片空间（只读代理 + 写链路）与视频上传

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
    MATERIAL_ORDER_FIELDS,
    Optional,
    Path,
    Session,
    User,
    _DRAFT_MAX_IMAGES,
    _PERMIT_BATCH,
    _PERMIT_CRED,
    _ark_adapter_for,
    _material_confirm_token,
    _owned_account,
    get_current_user,
    get_db,
    get_qianfan_api_adapter_factory,
    logger,
    status,
)

#: 本域独立 router —— 由 `__init__.py` 挂到父 router 下，**不带 prefix**
#:（路径里已含 `/picture-space` 这类段）。
#: ⚠️ 各域**各自**一个，不能共用 —— `include_router` 把父 router 挂到
#:    自己身上会让路由重复/错乱。
router = APIRouter()

class PictureSpaceItemsRequest(BaseModel):
    """图片空间列表请求（设计 22 §1.3）。"""

    platform_account_id: int
    keyword: str = ""
    #: 1=文件夹 2=文件；缺省 = 两者都取。
    basic_types: Optional[list[int]] = None
    father_directory_id: str = ""
    order_field: str = "name"
    order_asc: bool = True
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=500)
    #: 回收站视图（真机 `/pictureSpace/recycle`）：`statuses=[4]`、只取文件、按删除时间倒序。
    recycle: bool = False

    model_config = ConfigDict(extra="forbid")

@router.post("/picture-space/items")
def picture_space_items(
    payload: PictureSpaceItemsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**图片空间列表**（千帆 `material_space/search_directory_manageable`）—— 只读。

    | 约定 | 做法 |
    |---|---|
    | 落库 | **零**（平台数据现取现渲染，本端点不碰任何表） |
    | 门禁 | 必须该账号有 ark 令牌，否则 400 `ARK_SESSION_REQUIRED`（**不借会话**） |
    | 签名 | 列表端点**必须**签名（平台强制；无签名 `code=-1`，见 SDK docstring） |
    | 排序白名单 | `order_field` 表外 ⇒ 400 `MATERIAL_ORDER_UNSUPPORTED` |
    | 类型白名单 | `basic_types` 表外 ⇒ 400 `MATERIAL_TYPE_UNSUPPORTED` |
    | 分页 | `page_size` 上限 100（Pydantic 钳制，与平台页面同口径） |

    返回 `{total, items[]}`（归一化契约见 `picture_space_service.normalize_search_payload`）。
    """
    from backend.app.services import picture_space_service as ps

    if payload.order_field not in MATERIAL_ORDER_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "MATERIAL_ORDER_UNSUPPORTED",
                "message": f"不支持的排序字段：{payload.order_field}",
                "supported": list(MATERIAL_ORDER_FIELDS),
            },
        )
    if payload.basic_types:
        bad = [t for t in payload.basic_types if t not in ps.MATERIAL_BASIC_TYPES]
        if bad:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "MATERIAL_TYPE_UNSUPPORTED",
                    "message": f"不支持的素材类型：{bad}",
                    "supported": list(ps.MATERIAL_BASIC_TYPES),
                },
            )

    account = _owned_account(db, current_user, payload.platform_account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)
    # 回收站视图（真机 `/pictureSpace/recycle`）：只列文件（basicTypes=[2]）+ statuses=[4]
    # + 按删除时间（`updateTime`）倒序 —— 逐字对齐真机抓包。
    if payload.recycle:
        statuses: Optional[list[int]] = [ps.MATERIAL_STATUS_RECYCLE_BIN]
        basic_types = payload.basic_types or [ps.MATERIAL_TYPE_FILE]
        order_field = payload.order_field if payload.order_field != "name" else "updateTime"
        order_asc = payload.order_asc if payload.order_field != "name" else False
    else:
        statuses = [ps.MATERIAL_STATUS_NORMAL]
        basic_types = payload.basic_types
        order_field = payload.order_field
        order_asc = payload.order_asc
    body = ps.build_search_body(
        keyword=payload.keyword,
        basic_types=basic_types,
        father_directory_id=payload.father_directory_id,
        order_field=order_field,
        order_asc=order_asc,
        page=payload.page,
        page_size=payload.page_size,
        statuses=statuses,
    )
    ok, msg, raw = adapter.search_material_space(body)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "MATERIAL_SPACE_QUERY_FAILED", "message": msg},
        )
    result = ps.normalize_search_payload(raw)
    result["page"] = payload.page
    result["page_size"] = payload.page_size
    return result

@router.get("/picture-space/tree")
def picture_space_tree(
    platform_account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**图片空间目录树**（千帆 `material_space/get_directory`）—— 只读，零落库。

    `platform_account_id` 传 query。返回根节点归一化契约（见
    `picture_space_service.normalize_directory_payload`）。
    """
    from backend.app.services import picture_space_service as ps

    account = _owned_account(db, current_user, platform_account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)
    ok, msg, raw = adapter.get_material_directory(account.seller_id or "")
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "MATERIAL_SPACE_QUERY_FAILED", "message": msg},
        )
    return ps.normalize_directory_payload(raw)

@router.get("/picture-space/usage")
def picture_space_usage(
    platform_account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**图片空间配额**（千帆 `material_space/get_seller_space_usage`）—— 只读，零落库。

    返回 `{used_space, total_space, white_seller}`；取不到时前两者是 `None`
    （前端据此**不渲染**配额条，见设计 22 §1.7）。
    """
    from backend.app.services import picture_space_service as ps

    account = _owned_account(db, current_user, platform_account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)
    ok, msg, raw = adapter.get_material_space_usage()
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "MATERIAL_SPACE_QUERY_FAILED", "message": msg},
        )
    return ps.normalize_usage_payload(raw)

@router.get("/picture-space/resize/check")
def picture_space_resize_check(
    platform_account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**查扩容条件**（千帆 `material_space/check_resize_material_space`）—— 只读探测。

    平台「图片空间扩容」弹窗打开时调它。返回 `conditions[]` 逐条
    `{type, pass, desc, current}` + 派生 `can_apply`（全通过 ∧ 非白名单）
    + `remaining`（剩余扩容次数，平台页脚「剩余扩容次数：N次」）。
    """
    from backend.app.services import picture_space_service as ps

    account = _owned_account(db, current_user, platform_account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)
    ok, msg, raw = adapter.check_resize_material_space()
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "MATERIAL_RESIZE_CHECK_FAILED", "message": msg},
        )
    return ps.normalize_resize_conditions(raw)

@router.post("/picture-space/resize")
def picture_space_resize(
    payload: PictureSpaceResizeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**提交扩容申请**（千帆 `material_space/resize_material_space`）—— 需签名。

    真机 JS bundle：body **空 `{}`**（扩容额度由平台按条件判定）。前端在弹窗里先
    展示 `resize/check` 的条件结果，用户点「提交」后调本端点；成功后重取配额。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)
    ok, msg, raw = adapter.resize_material_space()
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "MATERIAL_RESIZE_FAILED", "message": msg},
        )
    return {"platform_account_id": account.id, "success": True}

class PictureSpaceResizeRequest(BaseModel):
    """图片空间**扩容**请求（2026-10-07 用户「新增空间扩容功能」）。

    ⚠️ 平台 `resize_material_space` **body 为空 `{}`** —— 扩容额度由平台按条件判定，
    前端只需给出账号。
    """

    platform_account_id: int

    model_config = ConfigDict(extra="forbid")

class PictureSpaceWriteRequest(BaseModel):
    """图片空间写操作请求（新建文件夹 / 移动 / 删除 / 下载共用）。

    `items` 传**归一化契约**条目（前端列表里已有的那些；后端反推成平台形状）。
    新建文件夹用 `name` 而非 `items`。
    """

    platform_account_id: int
    items: Optional[list[dict]] = None
    name: str = ""
    #: 移动的目标目录 id（`"0"` = 根）。
    father_directory_id: str = ""
    #: 回收站视图（列表端点用）：`statuses=[4]` + 只取文件。
    recycle: bool = False
    #: 预览的动作：`delete`（进回收站）或 `permanent-delete`（彻底删）—— 决定 token 指纹。
    action: str = "delete"
    #: 两层确认（仅删除 / 彻底删除需要）：必须 `confirmed=True` 且 `confirm_token` 匹配。
    confirmed: bool = False
    confirm_token: str = ""

    model_config = ConfigDict(extra="forbid")

@router.post("/picture-space/preview")
def picture_space_preview(
    payload: PictureSpaceWriteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**写操作预览**（纯本地、零平台请求）—— 算二次确认凭据 + 影响面。

    与订单写链路的 `orders/ship/preview` 同一形态：返回「将对哪几条做什么」+
    `confirm_token`。前端在确认框里展示这些信息，用户点「确定」再把 token 回传。
    """
    from backend.app.services import picture_space_service as ps

    account = _owned_account(db, current_user, payload.platform_account_id)
    items = ps.extract_items(payload.items)
    ids = [str(it.get("id")) for it in items]
    action = "permanent-delete" if payload.action == "permanent-delete" else "delete"
    return {
        "platform_account_id": account.id,
        "action": action,
        "count": len(ids),
        "item_ids": ids,
        "sample_names": [str((it or {}).get("display_name") or (it or {}).get("name") or "-") for it in (payload.items or [])[:5]],
        "confirm_token": _material_confirm_token(account.id, action, ids),
    }

@router.post("/picture-space/folders")
def picture_space_create_folder(
    payload: PictureSpaceWriteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**新建文件夹**（平台 `batch_add_directory_manageable`）—— 需签名。

    新建是**可逆的低风险**操作（多一个空文件夹不丢数据）⇒ 不需要两层确认。
    文件夹名 ≤20 字（与平台输入框 `maxlength` 同口径）。
    """
    from backend.app.services import picture_space_service as ps

    account = _owned_account(db, current_user, payload.platform_account_id)
    try:
        name = ps.validate_folder_name(payload.name)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "MATERIAL_BAD_NAME", "message": str(exc)},
        ) from exc
    adapter = _ark_adapter_for(db, account, adapter_factory)
    item = ps.build_add_item(account.seller_id or "", name)
    ok, msg, raw = adapter.add_material_directories(account.seller_id or "", [item])
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "MATERIAL_WRITE_FAILED", "message": msg},
        )
    return {"platform_account_id": account.id, "name": name, "success": True}

@router.post("/picture-space/move")
def picture_space_move(
    payload: PictureSpaceWriteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**移动到目录**（平台 `batch_edit_directory_manageable`）—— 需签名。

    ⚠️ **真机 JS bundle `332.d754c099.js` `case 3` 取证**：移动 = 回传完整原条目 +
    `fatherDirectoryId: <目标目录 id>`，**不动 `status`**。
    （曾错误地置 `status:4` ⇒ 那是 `RECYCLE_BIN` ⇒ 等于把文件删进回收站 —— 已纠正。）
    """
    from backend.app.services import picture_space_service as ps

    account = _owned_account(db, current_user, payload.platform_account_id)
    items = ps.extract_items(payload.items)
    try:
        ps.validate_batch(items)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "MATERIAL_BAD_SELECTION", "message": str(exc)},
        ) from exc
    target = payload.father_directory_id or "0"
    adapter = _ark_adapter_for(db, account, adapter_factory)
    platform_items = [ps.build_move_item(it, target) for it in items]
    ok, msg, raw = adapter.edit_material_manageables(account.seller_id or "", platform_items)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "MATERIAL_WRITE_FAILED", "message": msg},
        )
    return {
        "platform_account_id": account.id,
        "moved": len(platform_items),
        "father_directory_id": target,
        "success": True,
    }

@router.post("/picture-space/delete")
def picture_space_delete(
    payload: PictureSpaceWriteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**批量删除**（平台 `batch_edit_directory_manageable` 置 `status=4`，进回收站）—— 需签名。

    **两层确认**（与订单写链路同一把尺子）：`confirmed=True` **且**
    `confirm_token` 与本地重算的一致，缺一 ⇒ 400，**一次都不打平台**。
    平台侧是**可还原**的删除（进回收站），不是物理抹除；彻底删除见
    `POST /picture-space/permanent-delete`。

    ⚠️ **真机 JS bundle `332.d754c099.js` `case 14` 取证**：删除 = `batch_edit` +
    `{...item, status: RECYCLE_BIN(4)}`（`batch_delete_*` 是彻底删除，键名不同）。
    """
    from backend.app.services import picture_space_service as ps

    account = _owned_account(db, current_user, payload.platform_account_id)
    items = ps.extract_items(payload.items)
    try:
        ps.validate_batch(items)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "MATERIAL_BAD_SELECTION", "message": str(exc)},
        ) from exc
    ids = [str(it.get("id")) for it in items]
    expected = _material_confirm_token(account.id, "delete", ids)
    if not payload.confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "MATERIAL_CONFIRM_REQUIRED", "message": "删除需要二次确认"},
        )
    if (payload.confirm_token or "") != expected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "MATERIAL_CONFIRM_MISMATCH", "message": "确认凭据不匹配（请重新预览）"},
        )
    adapter = _ark_adapter_for(db, account, adapter_factory)
    platform_items = [ps.build_delete_item(it) for it in items]
    # ⚠️ 真机实证：删除走 `batch_edit` 置 `status=4`（`4 = RECYCLE_BIN`，进回收站）。
    ok, msg, raw = adapter.edit_material_manageables(account.seller_id or "", platform_items)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "MATERIAL_WRITE_FAILED", "message": msg},
        )
    return {"platform_account_id": account.id, "deleted": len(platform_items), "success": True}

@router.post("/picture-space/download")
def picture_space_download(
    payload: PictureSpaceWriteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**批量下载**（平台 `batch_download_files`，打包成异步任务）—— 需签名。

    平台把选中文件打成 zip 任务，返回 `task_id`；前端轮询
    `GET /picture-space/task/{task_id}` 取结果地址（平台侧下载，本地不落副本）。
    """
    from backend.app.services import picture_space_service as ps

    account = _owned_account(db, current_user, payload.platform_account_id)
    items = ps.extract_items(payload.items)
    try:
        ps.validate_batch(items)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "MATERIAL_BAD_SELECTION", "message": str(exc)},
        ) from exc
    adapter = _ark_adapter_for(db, account, adapter_factory)
    name = payload.name or "下载"
    # ⚠️ 真机 JS bundle `332.d754c099.js` `case 9`：请求键 `links`（不是 directoryManageables），
    #    文件 {type:2,link,name,extension} / 文件夹 {type:1,directoryId}。
    links = ps.build_download_links(items)
    ok, msg, raw = adapter.download_material_files(account.seller_id or "", links, name=name)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "MATERIAL_WRITE_FAILED", "message": msg},
        )
    data = (raw or {}).get("data") or {}
    task_id = str(data.get("taskId") or data.get("task_id") or "")
    return {"platform_account_id": account.id, "task_id": task_id, "success": True}

@router.get("/picture-space/task/{task_id}")
def picture_space_task(
    task_id: str,
    platform_account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """查图片空间异步任务（下载打包进度 / 结果地址）—— 只读，零落库。"""
    account = _owned_account(db, current_user, platform_account_id)
    adapter = _ark_adapter_for(db, account, adapter_factory)
    ok, msg, raw = adapter.query_material_task(task_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "MATERIAL_TASK_FAILED", "message": msg},
        )
    data = (raw or {}).get("data") or {}
    task = data.get("task") if isinstance(data, dict) else None
    return {"task_id": task_id, "task": task or {}}

@router.post("/picture-space/restore")
def picture_space_restore(
    payload: PictureSpaceWriteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**从回收站还原**（平台 `batch_edit_directory_manageable` 置 `status=1`）—— 需签名。

    ⚠️ **真机 JS bundle `417.d61d3cd8.js` 取证**：还原 = `batch_edit` +
    `{...item, status: USABLE(1)}`，与删除同一端点、只差 status。
    还原是**可逆的低风险**操作 ⇒ 不需要两层确认。
    """
    from backend.app.services import picture_space_service as ps

    account = _owned_account(db, current_user, payload.platform_account_id)
    items = ps.extract_items(payload.items)
    try:
        ps.validate_batch(items)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "MATERIAL_BAD_SELECTION", "message": str(exc)},
        ) from exc
    adapter = _ark_adapter_for(db, account, adapter_factory)
    platform_items = [ps.build_restore_item(it) for it in items]
    ok, msg, raw = adapter.edit_material_manageables(account.seller_id or "", platform_items)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "MATERIAL_WRITE_FAILED", "message": msg},
        )
    return {"platform_account_id": account.id, "restored": len(platform_items), "success": True}

@router.post("/picture-space/permanent-delete")
def picture_space_permanent_delete(
    payload: PictureSpaceWriteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**彻底删除**（平台 `batch_delete_directory_manageable`）—— 需签名，**不可还原**。

    **两层确认**（与删除同一把尺子，但 action 不同 ⇒ token 不可跨用）：
    `confirmed=True` **且** `confirm_token` 匹配，缺一 ⇒ 400，**一次都不打平台**。

    ⚠️ **真机 JS bundle `417.d61d3cd8.js` 取证**：请求键是 `directoryManageableElements`
    （只 `{id,type}`），不是 `directoryManageables` —— 此前用错键被**误判**成
    「平台不接受该端点」。
    """
    from backend.app.services import picture_space_service as ps

    account = _owned_account(db, current_user, payload.platform_account_id)
    items = ps.extract_items(payload.items)
    try:
        ps.validate_batch(items)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "MATERIAL_BAD_SELECTION", "message": str(exc)},
        ) from exc
    ids = [str(it.get("id")) for it in items]
    expected = _material_confirm_token(account.id, "permanent-delete", ids)
    if not payload.confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "MATERIAL_CONFIRM_REQUIRED", "message": "彻底删除需要二次确认"},
        )
    if (payload.confirm_token or "") != expected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "MATERIAL_CONFIRM_MISMATCH", "message": "确认凭据不匹配（请重新预览）"},
        )
    adapter = _ark_adapter_for(db, account, adapter_factory)
    elements = ps.build_permadelete_elements(items)
    ok, msg, raw = adapter.delete_material_manageables(account.seller_id or "", elements)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "MATERIAL_WRITE_FAILED", "message": msg},
        )
    return {"platform_account_id": account.id, "deleted": len(elements), "success": True}

class PictureSpaceUploadRequest(BaseModel):
    """上传本地图片到图片空间（设计 22 §1.5）。

    前端把文件读成 base64 传上来（避免 multipart 解析；图片本就不大）。
    后端做三步：申请凭证 → PUT 到 ROS → 登记条目。
    """

    platform_account_id: int
    #: 文件名。**留空则由后端按内容 MD5 生成**（设计 26：商品配置第一步，
    #: 用户口径「文件名称需使用 MD5 生成唯一名称」）——同图恒同名，天然去重。
    file_name: str = Field(default="", max_length=255)
    #: 文件字节的 base64（不含 data: 前缀）。
    content_base64: str = Field(min_length=1)
    father_directory_id: str = "0"

    model_config = ConfigDict(extra="forbid")

class VideoUploadRequest(BaseModel):
    """上传**商品视频**（千帆商品发布页「主图视频」上传）。

    ⚠️ 与图片上传**不是同一条路**（2026-10-09 实测 + bundle 逆出）：
    图片走图片空间（`scene=goods_image` → `arkgoods/` 桶 → `material_space/batch_add` 登记）；
    视频走**独立直传**（`scene=common_file` → `ark-common/` 桶，**不登记**图片空间）。
    视频误走图片空间会在登记一步答「获取上传文件[v]信息失败」。
    """

    platform_account_id: int
    file_name: str = Field(default="", max_length=255)
    content_base64: str = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")

@router.post("/video/upload", summary="上传商品视频（直传，不走图片空间）")
def video_upload(
    payload: VideoUploadRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**上传商品视频** —— 三步：申请凭证（`common_file`）→ PUT 到 ROS → 返回链接。

    ## 为什么单独一条（2026-10-09 修复「商品视频上传失败」）

    实测把 mp4 走图片链路（`scene=goods_image`）上传：PUT **成功**（预分配 `arkgoods/<id>`），
    但登记一步答 `{"success":false,"msg":"获取上传文件[v]信息失败"}` —— 两种 scene 都一样。
    根因：**图片空间是图片库**（登记条目 `type:2` 要 `width`/`height` 数值），
    视频没有尺寸、也不属于该库。真机 bundle（`9515.*.js`）里「上传视频」用的是
    `new Uploader({bizName:"ark", scene:"common_file"})` 的**独立直传**，成功响应直接给
    `{url, fileId}`，**不经过图片空间登记**。

    ⇒ 本端点只做「申请凭证 → PUT → 回链」，不做登记。

    ⚠️ **只上传视频字节，不碰发布提交**（红线）。
    """
    import base64
    import tempfile
    from pathlib import Path

    account = _owned_account(db, current_user, payload.platform_account_id)
    try:
        raw = base64.b64decode(payload.content_base64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "VIDEO_BAD_CONTENT", "message": f"文件内容不是合法 base64：{exc}"},
        ) from exc
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "VIDEO_EMPTY_FILE", "message": "文件内容为空"},
        )

    #: ⚠️ 用户口径（2026-10-09）：「视频和商品图一样，上传前改成 md5 唯一名称」——
    #: 一律按**内容 MD5** 命名（同内容重复上传得同名、不产生第二份），
    #: 与前端 `md5_file_name` / 图片链路**同一口径**（唯一真相源在后端，前端不必再算）。
    import hashlib

    ext = Path(payload.file_name.strip()).suffix or ".mp4"
    file_name = f"{hashlib.md5(raw).hexdigest()}{ext.lower()}"

    adapter = _ark_adapter_for(db, account, adapter_factory)
    # 1) 申请凭证 —— 视频场景是 `common_file`（bundle 逆出），并把扩展名带上。
    ok, msg, permit_raw = adapter.upload_web_permit(
        1, biz_name="ark", scene="common_file", referer="/app-item/good/create", file_format=ext
    )
    if not ok or not isinstance(permit_raw, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "VIDEO_UPLOAD_FAILED", "message": msg or "申请上传凭证失败"},
        )
    permits = (permit_raw.get("data") or {}).get("uploadTempPermits") or []
    if not permits:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "VIDEO_UPLOAD_FAILED", "message": "平台没有返回上传凭证"},
        )
    permit = sorted(permits, key=lambda x: x.get("qos") or 0, reverse=True)[0]
    file_ids = permit.get("fileIds") or []
    if not file_ids:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "VIDEO_UPLOAD_FAILED", "message": "上传凭证没有预分配文件 ID"},
        )
    file_id = str(file_ids[0])

    # 2) PUT 字节到 ROS
    tmp_dir = Path(tempfile.mkdtemp(prefix="video-upload-"))
    tmp_path = tmp_dir / Path(file_name).name
    video_width = video_height = video_duration = None
    try:
        tmp_path.write_bytes(raw)
        #: 读视频分辨率/时长（登记 body 需要 `width`/`height`，真机抓包有 `720`/`960`）。
        #: cv2 读不出（非标准容器等）就留 None —— 缺值口径：不造假。
        try:
            import cv2

            cap = cv2.VideoCapture(str(tmp_path))
            if cap.isOpened():
                video_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None
                video_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None
                fps = cap.get(cv2.CAP_PROP_FPS) or 0
                frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                if fps > 0 and frames > 0:
                    video_duration = int(round(frames / fps))
                cap.release()
        except Exception:  # noqa: BLE001
            pass
        ok, msg, put_meta = adapter.upload_file(
            upload_addr=str(permit.get("uploadAddr") or ""),
            file_id=file_id,
            token=str(permit.get("token") or ""),
            file_path=str(tmp_path),
        )
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "VIDEO_UPLOAD_FAILED", "message": msg},
        )
    preview_url = (put_meta or {}).get("preview_url", "") if isinstance(put_meta, dict) else ""

    #: ⚠️ 2026-10-11 真机抓包修正：视频**也要登记**，只是走 `type:3` + `scene=goods_video`
    #: （不是图片的 `type:2 / goods_image`）。之前（2026-10-09）误判「视频不登记」⇒ 拿不到
    #: `materialId`，导致提交体 `videos[]` 缺登记记录 id。
    material_id = ""
    path_value = file_id
    link_value = preview_url
    try:
        from app.services import picture_space_service as ps

        seller_id = str(account.seller_id or "")
        if seller_id:
            item = ps.build_upload_item(
                seller_id,
                file_id=file_id,
                file_name=file_name,
                size=len(raw),
                width=video_width,
                height=video_height,
                preview_url=preview_url,
                father_directory_id="0",
                item_type=ps.MATERIAL_TYPE_VIDEO,
                scene="goods_video",
                keep_name=True,
            )
            ok_r, _msg_r, raw_resp = adapter.register_material_files(seller_id, [item])
            if ok_r and isinstance(raw_resp, dict):
                data = raw_resp.get("data")
                if isinstance(data, dict):
                    items = data.get("directoryManageables") or []
                    if items and isinstance(items[0], dict):
                        material_id = str(items[0].get("id") or "")
                        #: 视频登记时 path 用上传凭证 fileId 原样（真机 `videos[0].path` 与
                        #: `uploaderInfoModel.fileId` 同值），不换 CDN id。
                        path_value = str(items[0].get("additionalData", {}).get("path") or file_id)
                        link_value = str(items[0].get("additionalData", {}).get("link") or preview_url)
    except Exception as exc:  # noqa: BLE001 - 登记失败不阻断上传，materialId 留空
        logger.warning("视频登记失败（materialId 留空）：%s", exc)

    return {
        "platform_account_id": account.id,
        "file_name": file_name,
        "file_id": file_id,
        "url": link_value,
        "materialId": material_id,
        "path": path_value,
        "link": link_value,
        "extension": ext,
        "size": len(raw),
        "duration": video_duration,
        "width": video_width,
        "height": video_height,
        "success": True,
    }

@router.post("/picture-space/upload")
def picture_space_upload(
    payload: PictureSpaceUploadRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**上传本地图片**到图片空间（设计 22 §1.5）—— 三步写链路。

    1. `upload_web_permit` 拿预分配 `fileId` + `uploadAddr` + `token`（仓库既有实现）；
    2. `upload_file` 把字节 PUT 到 ROS；
    3. `register_material_files`（`batch_add` + `type:2`）登记，列表才可见。

    ⚠️ 需要 ark 签名（平台强制）。
    """
    import base64
    import tempfile
    from pathlib import Path

    from backend.app.services import picture_space_service as ps

    account = _owned_account(db, current_user, payload.platform_account_id)
    try:
        raw = base64.b64decode(payload.content_base64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "MATERIAL_BAD_CONTENT", "message": f"文件内容不是合法 base64：{exc}"},
        ) from exc
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "MATERIAL_EMPTY_FILE", "message": "文件内容为空"},
        )

    #: 文件名：**留空则按内容 MD5 生成**（用户口径「文件名称需使用 MD5 生成唯一名称」）。
    #: 同内容恒同名 ⇒ 重复上传天然去重；扩展名沿用调用方给的（缺省 .jpg）。
    file_name = payload.file_name.strip()
    if not file_name:
        import hashlib

        #: PNG 魔数判扩展名（PNG 头 = 89 50 4E 47）；判不出按 jpg。
        ext = ".png" if raw[:4] == b"\x89PNG" else ".jpg"
        file_name = f"{hashlib.md5(raw).hexdigest()}{ext}"

    adapter = _ark_adapter_for(db, account, adapter_factory)
    # 1) 申请凭证（单文件）—— ⚠️ 图片空间必须 `biz_name=ark&scene=goods_image`
    #    （真机抓包）；用发布链路的 spectrum/image 会把文件 PUT 到别的桶 ⇒ 登记后取图 404。
    ok, msg, permit_raw = adapter.upload_web_permit(
        1, biz_name="ark", scene="goods_image", referer="/app-system/pictureSpace"
    )
    if not ok or not isinstance(permit_raw, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "MATERIAL_UPLOAD_FAILED", "message": msg or "申请上传凭证失败"},
        )
    permits = (permit_raw.get("data") or {}).get("uploadTempPermits") or []
    if not permits:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "MATERIAL_UPLOAD_FAILED", "message": "平台没有返回上传凭证"},
        )
    # 平台前端按 `qos` 降序取第一个（真机 bundle `sort((a,b)=>b.qos-a.qos)[0]`）。
    permit = sorted(permits, key=lambda x: x.get("qos") or 0, reverse=True)[0]
    file_ids = permit.get("fileIds") or []
    if not file_ids:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "MATERIAL_UPLOAD_FAILED", "message": "上传凭证没有预分配文件 ID"},
        )
    file_id = str(file_ids[0])
    upload_addr = str(permit.get("uploadAddr") or "")
    token = str(permit.get("token") or "")

    # 2) PUT 字节到 ROS（先落临时文件 —— SDK 接口吃路径）
    tmp_dir = Path(tempfile.mkdtemp(prefix="ps-upload-"))
    tmp_path = tmp_dir / Path(file_name).name
    try:
        tmp_path.write_bytes(raw)
        ok, msg, put_meta = adapter.upload_file(
            upload_addr=upload_addr, file_id=file_id, token=token, file_path=str(tmp_path)
        )
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "MATERIAL_UPLOAD_FAILED", "message": msg},
            )
        # PUT 响应头带 `X-Ros-Preview-Url`（带签名的预览地址）—— 登记时要回填给平台。
        preview_url = (put_meta or {}).get("preview_url", "") if isinstance(put_meta, dict) else ""
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass

    # 3) 登记条目（列表才可见）
    # ⚠️ 先从字节里读出**真实尺寸**再登记（2026-10-07 真机 bug「For input string: ""」）：
    # 平台是 Java 后端，`additionalData.width/height` 会被 `Integer.parseInt` 解析——
    # 传空串直接 `NumberFormatException`，整个上传失败。真机 JS 是 `new Image()` 读的尺寸；
    # 拿不到（异常 / 非图片）就**不带这两个键**（缺值口径：不造假、也不送空串）。
    width = height = None
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(raw)) as img:
            width, height = img.size
    except Exception:  # noqa: BLE001 —— 非图片或读不出，按缺值走
        pass
    item = ps.build_upload_item(
        account.seller_id or "",
        file_id=file_id,
        file_name=file_name,
        size=len(raw),
        width=width,
        height=height,
        preview_url=preview_url,
        father_directory_id=payload.father_directory_id,
    )
    ok, msg, raw_resp = adapter.register_material_files(account.seller_id or "", [item])
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "MATERIAL_UPLOAD_FAILED", "message": msg},
        )
    #: ⚠️ 2026-10-10（用户铁律「上传成功后后续所有对该图的使用必须用返回的 url/对象」）：
    #: 登记后回读记录，取平台分配的 `materialId` / `path` / `link` —— 这三个是**发布体
    #: 图片条目**必需的材料（`build_publish_image` 直接吃它们），此前端点只回了预览 url
    #: ⇒ 前端拿不到 materialId/path ⇒ 提交时只能传相对路径让后端重新上传。
    #: 现在补回，前端就能「上传一次、预览与提交共用同一份远程对象」，后端不再重复上传。
    from backend.app.services import shop_draft_service as draft_svc

    _material_id = draft_svc._registered_material_id(raw_resp) or file_id
    _record = draft_svc._fetch_material_record(
        adapter, name=str(item.get("name") or ""), material_id=_material_id
    )
    _path_value = str(_record.get("path") or (item.get("additionalData") or {}).get("path") or file_id)
    _link_value = str(
        _record.get("link")
        or (item.get("additionalData") or {}).get("link")
        or f"https://qimg.xiaohongshu.com/{_path_value}"
    )
    #: 回给前端的链接：优先带签名的预览地址（PUT 响应头给的），
    #: 没有则回图片空间的**检索名**（去后缀），由调用方按名再查一次拿正式链接。
    #: ⚠️ 静默上传契约（用户口径）：前端**不出现**图片空间界面，只拿链接做预览。
    #: ⚠️ 2026-10-10：`preview_url` 依赖 PUT 响应头 `X-Ros-Preview-Url`，该头可能缺失
    #: ⇒ `url` 空串 ⇒ 前端规格图预览不展示。兜底：用 `file_id` 拼公开 CDN 直链
    #: （SDK 注释：`https://qimg.xiaohongshu.com/arkgoods/<id>` 可正常显示）。
    _fallback_url = ""
    if not preview_url and file_id:
        _fallback_url = f"https://qimg.xiaohongshu.com/arkgoods/{file_id}"
    return {
        "platform_account_id": account.id,
        "file_name": file_name,
        "search_name": Path(file_name).stem,
        "url": preview_url or _fallback_url,
        #: ⚠️ **发布体材料**（2026-10-10 补）：前端持有后，提交/保存草稿直接传这组对象，
        #: 后端 `build_publish_image` 直接组装，不再重新上传。
        "materialId": _material_id,
        "path": _path_value,
        "link": _link_value,
        #: ⚠️ 下面几项是**标题生成**（`getRecommendItemTitle`）的入参材料 ——
        #: 平台是拿「带签名的图链 + 尺寸」去喂模型的（真机抓包），
        #: 缺了它们就没法复刻「上传主图后自动出标题」。
        "file_id": file_id,
        "width": width,
        "height": height,
        "size": len(raw),
        "extension": Path(file_name).suffix,
        "success": True,
    }

class PictureSpaceUploadBatchRequest(BaseModel):
    """**批量**上传本地图片（方案 B：一次 permit + 服务端并发 PUT）。

    用户口径（2026-10-11）：自动加载参数的详情图 / 主图，以及「从预检 SKU 导入」
    的规格图，都要并发上传；**并发数默认 5、可在 config 表配置**
    （`xhs_image_upload_parallelism`）。

    ⚠️ **为什么可以并发（不触发风控）**：上传是两段式 ——
      ① `upload/web/permit` 打**小红书**（低频：**整批只申请一次**，N 个 fileId 一次拿齐）；
      ② PUT 字节打 **ROS/COS 对象存储**（`https://<uploadAddr>/<fileId>`，**不是小红书域名**）。
    单张 ~2.5s 的耗时基本全在 ②（传 100~230KB）⇒ 并发第二节**不产生任何平台请求**。
    平台侧证据：笔记发布链路 `note_publish_service._upload_media` 早已用
    `upload_web_permit(len(downloaded))` 一次批量申请 —— 说明「一次请求换 N 个上传位」
    正是平台自身的用法。
    """

    platform_account_id: int = Field(ge=1)
    #: 每个元素 = `{file_name, content_base64}`；`file_name` 留空则按内容 MD5 生成。
    files: list[dict[str, Any]] = Field(min_length=1, max_length=_DRAFT_MAX_IMAGES)
    father_directory_id: str = Field(default="0", max_length=64)
    #: 覆盖配置的并发数（缺省 0 = 读 `xhs_image_upload_parallelism`）。
    concurrency: int = Field(default=0, ge=0, le=32)

    #: ⚠️ `extra="forbid"`（2026-10-11 由 `test_qianfan_request_models_all_forbid_unknown_fields`
    #: 抓出补齐）：Pydantic 默认 `ignore` 会**静默丢弃**未知字段 —— 「挂链笔记数筛选
    #: 未生效」那类事故就是这么来的（跑着的后端模型里没有新字段，参数被悄悄吞掉，
    #: 表现与代码写错一模一样）。加 forbid ⇒ 新旧进程不匹配在第一跳就 422 炸出声。
    model_config = ConfigDict(extra="forbid")


@router.post("/picture-space/upload-batch")
def picture_space_upload_batch(
    payload: PictureSpaceUploadBatchRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """**批量并发上传**（方案 B）—— 与单文件端点同契约，逐项返回。

    ⚠️ **顺序稳定性**：返回的 `results[]` **与请求 `files[]` 逐位对齐**（前端要按下标
       回填主图/详情图/规格图），因此**不**用 `as_completed` 的完成顺序。
    ⚠️ **单项失败不整批失败**：每项独立带 `success` + `message`（前端逐项提示、
       失败的图不阻断其余），与单文件端点的错误语义一致。

    设计 24 §22。
    """
    import base64
    import hashlib
    import io
    import tempfile
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path

    from backend.app.services import picture_space_service as ps
    from backend.app.services import settings_service as settings_svc

    account = _owned_account(db, current_user, payload.platform_account_id)

    #: 并发数：显式覆盖 > 运行时配置 > 内置默认 5（`BUILTIN_DEFAULTS`）。
    #: ⚠️ 用 `live_int`（**不是** `get_int`）—— 它是 settings_service 的运行时读点，
    #: 带「写后立即失效」缓存；`get_int` 不存在（实测 AttributeError）。
    concurrency = payload.concurrency or settings_svc.live_int(
        db, current_user.id, "xhs_image_upload_parallelism", default=5
    )
    concurrency = max(1, min(32, concurrency))

    #: 先解码 + 定名（CPU 侧，很快；放主线程便于**先行失败**给出确定原因）。
    prepared: list[dict[str, Any]] = []
    for index, item in enumerate(payload.files):
        raw = b""
        name = str(item.get("file_name") or "").strip()
        try:
            raw = base64.b64decode(str(item.get("content_base64") or ""), validate=True)
        except Exception as exc:  # noqa: BLE001
            prepared.append({"index": index, "error": f"内容不是合法 base64：{exc}"})
            continue
        if not raw:
            prepared.append({"index": index, "error": "文件内容为空"})
            continue
        if not name:
            ext = ".png" if raw[:4] == b"\x89PNG" else ".jpg"
            name = f"{hashlib.md5(raw).hexdigest()}{ext}"
        prepared.append({"index": index, "raw": raw, "file_name": name})

    adapter = _ark_adapter_for(db, account, adapter_factory)

    #: ① **先把凭证一次拿齐，再并发上传**（用户口径 2026-10-11）。
    #:
    #: ⚠️⚠️ **平台单次最多预分配 14 个 fileId**（2026-10-11 真机实测）：
    #:   `file_count=10 → 10` / `20 → 14` / `38 → 14` / `50 → 14`（封顶 14）。
    #:   旧实现只申请**一次**就开工 ⇒ 38 张里只有 14 张能传，
    #:   剩下 24 张全部落「上传凭证数量不足」（= 用户实测的 14/38）。
    #:   ⚠️ 笔记发布链路（`note_publish_service._upload_media`）也是单次申请，
    #:   它没暴露是因为笔记媒体 ≤10 张，从没超过 14。
    #: ⇒ 修法：**分批申请直到拿齐**（每批 ≤ `_PERMIT_BATCH`），把 fileId 攒齐再并发下载/上传。
    needed = len(prepared)
    file_ids: list[str] = []
    upload_addr = ""
    token = ""
    #: 分批申请的上限保护：别因平台异常返回 0 个而无限循环。
    for _round in range(0, (needed // _PERMIT_BATCH) + 3):
        if len(file_ids) >= needed:
            break
        want = min(_PERMIT_BATCH, needed - len(file_ids))
        ok, msg, permit_raw = adapter.upload_web_permit(
            want,
            biz_name="ark",
            scene="goods_image",
            referer="/app-system/pictureSpace",
        )
        if not ok or not isinstance(permit_raw, dict):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "MATERIAL_UPLOAD_FAILED", "message": msg or "申请上传凭证失败"},
            )
        permits = (permit_raw.get("data") or {}).get("uploadTempPermits") or []
        if not permits:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "MATERIAL_UPLOAD_FAILED", "message": "平台没有返回上传凭证"},
            )
        #: 平台前端按 `qos` 降序取第一个（真机 bundle `sort((a,b)=>b.qos-a.qos)[0]`）。
        permit = sorted(permits, key=lambda x: x.get("qos") or 0, reverse=True)[0]
        got = [str(x) for x in (permit.get("fileIds") or [])]
        #: ⚠️ 每一轮的 `uploadAddr` / `token` **可能不同**（不同批次是不同凭证）——
        #: 但一个 fileId 只能用**它那批的** addr/token 上传，所以按批留存映射。
        for one_fid in got:
            file_ids.append(one_fid)
            _PERMIT_CRED[one_fid] = (str(permit.get("uploadAddr") or ""), str(permit.get("token") or ""))
        #: 记下首批的 addr/token（兼容旧路径；实际上传按上面的映射取）。
        if not upload_addr:
            upload_addr = str(permit.get("uploadAddr") or "")
            token = str(permit.get("token") or "")
        if not got:
            #: 平台给了空批次 ⇒ 再要也是空，直接停（避免死循环）。
            break
    if len(file_ids) < needed:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "MATERIAL_UPLOAD_FAILED",
                "message": (
                    f"上传凭证不足（需要 {needed} 个，平台只预分配了 {len(file_ids)} 个）"
                ),
            },
        )

    results: list[dict[str, Any]] = [{} for _ in payload.files]

    def _one(slot: int, slot_index: int) -> None:
        """上传单张（**线程内**）：PUT 字节 + 登记 + 读回材料。"""
        entry = prepared[slot]
        index = int(entry["index"])
        if entry.get("error"):
            results[index] = {"index": index, "success": False, "message": entry["error"]}
            return
        raw = entry["raw"]
        file_name = str(entry["file_name"])
        if slot_index >= len(file_ids):
            results[index] = {
                "index": index,
                "success": False,
                "message": "上传凭证数量不足（平台预分配的 fileId 比文件少）",
            }
            return
        file_id = file_ids[slot_index]
        tmp_dir = Path(tempfile.mkdtemp(prefix="ps-batch-"))
        tmp_path = tmp_dir / Path(file_name).name
        try:
            tmp_path.write_bytes(raw)
            #: 单张重试一次（ROS 偶发 SSL EOF / 连接重置，与笔记链路同口径）。
            ok2, msg2, put_meta = False, "", None
            #: ⚠️ 用**这个 fileId 自己那批**的 addr/token（分批申请后它们不再相同）。
            _addr, _tok = _PERMIT_CRED.get(file_id, (upload_addr, token))
            for _attempt in range(2):
                ok2, msg2, put_meta = adapter.upload_file(
                    upload_addr=_addr, file_id=file_id, token=_tok, file_path=str(tmp_path)
                )
                if ok2:
                    break
            if not ok2:
                results[index] = {"index": index, "success": False, "message": msg2}
                return
            preview_url = (
                (put_meta or {}).get("preview_url", "") if isinstance(put_meta, dict) else ""
            )
            width = height = None
            try:
                from PIL import Image

                with Image.open(io.BytesIO(raw)) as img:
                    width, height = img.size
            except Exception:  # noqa: BLE001 —— 非图片/读不出按缺值走（不造假）
                pass
            reg_item = ps.build_upload_item(
                account.seller_id or "",
                file_id=file_id,
                file_name=file_name,
                size=len(raw),
                width=width,
                height=height,
                preview_url=preview_url,
                father_directory_id=payload.father_directory_id,
            )
            ok3, msg3, raw_resp = adapter.register_material_files(
                account.seller_id or "", [reg_item]
            )
            if not ok3:
                results[index] = {"index": index, "success": False, "message": msg3}
                return
            from backend.app.services import shop_draft_service as draft_svc

            material_id = draft_svc._registered_material_id(raw_resp) or file_id
            record = draft_svc._fetch_material_record(
                adapter, name=str(reg_item.get("name") or ""), material_id=material_id
            )
            path_value = str(
                record.get("path")
                or (reg_item.get("additionalData") or {}).get("path")
                or file_id
            )
            link_value = str(
                record.get("link")
                or (reg_item.get("additionalData") or {}).get("link")
                or f"https://qimg.xiaohongshu.com/{path_value}"
            )
            fallback_url = ""
            if not preview_url and file_id:
                fallback_url = f"https://qimg.xiaohongshu.com/arkgoods/{file_id}"
            results[index] = {
                "index": index,
                "platform_account_id": account.id,
                "file_name": file_name,
                "search_name": Path(file_name).stem,
                "url": preview_url or fallback_url,
                "materialId": material_id,
                "path": path_value,
                "link": link_value,
                "file_id": file_id,
                "width": width,
                "height": height,
                "size": len(raw),
                "extension": Path(file_name).suffix,
                "success": True,
            }
        except Exception as exc:  # noqa: BLE001 —— 单张异常不拖垮整批
            results[index] = {"index": index, "success": False, "message": f"{type(exc).__name__}: {exc}"}
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
                tmp_dir.rmdir()
            except OSError:
                pass

    #: 只对**已解码**的项并发（解码失败的项直接落结果，不占线程）。
    slots = [i for i, e in enumerate(prepared) if "raw" in e]
    if slots:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for n, slot in enumerate(slots):
                pool.submit(_one, slot, n)

    #: ⚠️ **兜底**：解码失败的项不在 `slots` 里 ⇒ `_one` 从没被调用 ⇒ 它的
    #: `results[index]` 还是空 `{}`（**缺 `success` 键**）。前端按 `results[].success`
    #: 判成功/失败 ⇒ 空对象既不是成功也不是失败，会被当成「静默成功」放过去。
    #: 这里统一补齐（用例实测抓出：坏 base64 那项 `results[0]` 没有 `success` 字段）。
    for slot, entry in enumerate(prepared):
        if "raw" in entry:
            continue
        index = int(entry["index"])
        results[index] = {
            "index": index,
            "success": False,
            "message": str(entry.get("error") or "该项无法解码"),
        }
    #: 再兜一层：任何仍未填的槽位（理论不可达）也要有确定判据，不留空对象。
    for index, item in enumerate(results):
        if not item:
            results[index] = {
                "index": index,
                "success": False,
                "message": "未知原因（未处理）",
            }

    #: 用完即清（凭证映射只为本次批量服务，常驻会一直涨）。
    for _fid in file_ids:
        _PERMIT_CRED.pop(_fid, None)

    return {
        "platform_account_id": account.id,
        "concurrency": concurrency,
        "total": len(payload.files),
        "succeeded": sum(1 for r in results if r.get("success")),
        "failed": sum(1 for r in results if not r.get("success")),
        "results": results,
    }
