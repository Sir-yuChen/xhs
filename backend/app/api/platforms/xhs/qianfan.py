"""千帆（`ark.xiaohongshu.com`）只读接线：绑定店铺 + 商品同步触发 + 本地台账查询。

设计真相源：`docs/02_设计文档/12_功能设计_我的笔记与互动数据.md` §24 / §25、
`docs/02_设计文档/13_功能设计_账号矩阵同步.md` §19 / §30。

## 为什么需要这个模块

「我的笔记」要展示**笔记挂载的商品**，而这个数据在创作者/网页两条链路里**都不存在**：

| 链路 | 实测 | 证据 |
|---|---|---|
| 创作者笔记列表 | `goods_info` 恒为 `{"goods_name": []}` | 6 页 60 条全空（2026-09-19） |
| 创作者笔记管理页（平台自己的 UI） | 渲染后 DOM 里「商品」字样出现 **0 次** | 本机 Chrome 打开 `/new/note-manager` |
| 网页版笔记详情 `feed` | `note_card` 13 个字段，无任何商品字段 | 设计文档 §18.1 / §19.1 |
| **千帆商品笔记列表** | ✅ `goods_infos[]`（`item_id` / `item_title` / `item_image` / `variant_desc`） | `/api/edith/goods-note/list` |

所以商品数据只能来自千帆。本模块把「绑定千帆 → 同步商品」这条只读链路做完整：
「同步商品」后台任务落库 `shop_products`（v1.32 起商品列表**只读本地台账**，
见 `shop_products_service`），`/users/notes` 按 `note_id` **纯本地**补上 `goods`。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.adapters.xhs.qianfan_api_adapter import (
    get_qianfan_api_adapter_factory,  # noqa: F401  重导出：`from ...xhs.qianfan import ...` 与测试打桩继续有效
)
from backend.app.api.platforms.xhs.pc import _cookies_to_string
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user, get_owned_active_account
from backend.app.core.time import shanghai_now
from backend.app.models.platform_account import PlatformAccount
from backend.app.models.note import Note, NoteAsset
from backend.app.models.shop import NoteGoods, Shop, ShopNoteGoods
from backend.app.models.shop_product import ShopProduct
from backend.app.services.account_service import store_channel_cookies
from backend.app.services.shop_health_service import check_account_health
from backend.app.services import shop_goods_sync_service, sync_task_service
from backend.app.services import shop_products_service
from backend.app.services.sync_task_service import (
    ABSOLUTE_PAGE_CAP,
    DEFAULT_MAX_PAGES,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    SHOP_GOODS_SYNC_TASK_TYPE,
    SYNC_MODE_AUTO,
    SYNC_SCOPE_FULL,
)
from backend.app.services.note_cleanup_service import CandidateQuery, list_candidates
from backend.app.services.shop_binding_service import (
    ShopBindError,
    account_for_shop,
    account_shop_id,
    bind_shop_for_account,
    detach_shop,
    get_shop_state,
    qianfan_cookies_for_account,
    set_shop_state,
    shop_for_account,
    shops_for_user,
    synced_note_count,
)
from backend.app.models.user import User

router = APIRouter(prefix="/xhs/qianfan", tags=["xhs-qianfan"])


class BindShopRequest(BaseModel):
    platform_account_id: int
    #: 千帆自己的 Cookie 串（含 `access-token-ark.xiaohongshu.com`）。
    cookie_string: str = Field(min_length=8, max_length=8192)


class ShopAccountRequest(BaseModel):
    platform_account_id: int


class ItemNotesRequest(BaseModel):
    """反查某个商品挂了哪些笔记。"""

    platform_account_id: int
    item_id: str = Field(min_length=1, max_length=64)


class ItemDetailRequest(BaseModel):
    """商品详情弹窗的数据请求（v1.32，设计 13 §30.3.6；**纯本地**）。"""

    platform_account_id: int
    item_id: str = Field(min_length=1, max_length=64)


#: 挂链笔记数筛选的比较符白名单（v1.34，用户口径「商品列表搜索条件添加挂链笔记数，
#: 默认小于指定篇数」）。**默认 `lt`（小于）**；未知值由路由层挡成 400，不猜。
NOTE_COUNT_OPS: tuple[str, ...] = ("lt", "lte", "eq", "gte", "gt")

#: 批量删除单次上限（v1.36）。删除是**不可逆**的本地操作，给一个上限让「全选到
#: 一个不该删的集合」时至少还要再想一次；超过就 400 让客户端分批（不静默截断：
#: 悄悄只删前 200 条，用户看到「成功」却少了行 —— 那是更坏的结果）。
MAX_BATCH_DELETE: int = 200


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


class ShopItemsDeleteRequest(BaseModel):
    """批量删除本地台账行（v1.36，用户口径：「商品列表新增批量删除操作」）。

    删的是**本地 `shop_products` 行**，不是平台商品（千帆 SDK 无商品删除接口；
    平台侧删除另有不可逆风险，见设计 13 §33.10 不做边界）。口径与两个连带后果
    写在 `shop_products_service.delete_items` 的 docstring 里（唯一一份）。

    入参用 **`item_id`（平台商品 ID）** 而不是本地自增主键：跨页勾选时前端手里
    就是 `item_id`（表格 `rowKey`），且店铺维度已经由 `shop_id_fk` 限定 ⇒ 传别人
    店的 item_id 只会删到 0 行，不会越权。
    """

    model_config = ConfigDict(extra="forbid")

    platform_account_id: int
    #: 千帆商品 ID 列表。**上限 / 空值都由路由层挡成结构化 400**（不在模型层做成
    #: 422 数组）：与 `status` / `note_count_op` 的白名单同一条纪律 —— 客户端要能
    #: 拿到 `code` + `limit` 才能把「超了多少」讲清楚。
    item_ids: list[str] = Field(default_factory=list)


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


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _owned_account(db: Session, current_user: User, account_id: int) -> PlatformAccount:
    """（薄别名）**唯一实现见 `core/deps.py::get_owned_active_account`**（DEF-055）。

    本函数曾是同一语义的 **4 份拷贝之一，且漏了软删闸** —— 而它一家就有 **11 个调用点**
    （商品列表 / 商品同步 / 绑定 / 解绑 / 巡检 / item-notes / 商品挂链笔记…），
    等于把「已删除的账号仍能发起平台操作」这个口子放大了 11 倍。
    """
    return get_owned_active_account(db, current_user, account_id, platform="xhs")


def _account_shop(db: Session, account_id: int) -> Shop | None:
    """账号关联的千帆店铺实体。

    **唯一实现**在 `services/shop_binding_service.shop_for_account` —— 2026-09-22
    `shop_account_bindings` 整表移除后，「账号有没有店铺」由
    `platform_accounts.shop_id_fk` 回答（服务层里带软删闸 + 租户闸）。
    """
    return shop_for_account(db, account_id)


def _shop_from_seller_info(payload: Any) -> dict[str, str]:
    """`/api/edith/seller/info/v2` → 展示所需的三个字段。

    **白名单构造**，不整体透传：该响应里带 `user_email`（脱敏过的邮箱）、
    `config_values`（含支付商户号）等与展示无关且敏感的内容（实测）。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    return {
        "shop_user_id": _first_text(data.get("user_id"), data.get("seller_id")),
        "shop_name": _first_text(data.get("seller_name"), data.get("user_name")),
    }


def _fmt_state_time(value: Any) -> str:
    """`shop_state` 里的 ISO 时间 → 展示用 `YYYY-MM-DD HH:MM`；空 / 坏值 → 空串。"""
    from backend.app.services.shop_binding_service import parse_state_time

    parsed = parse_state_time(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed is not None else ""


def _account_shop_summary(db: Session, account: PlatformAccount) -> dict[str, Any]:
    """账号的千帆店铺状态（**纯本地**，不打千帆）。

    响应字段与「绑定行时代」基本相同，三处变化（都是本轮口径要求）：
    * **`shop_account_count` 已移除** —— 用户 2026-09-22 拍板店铺与账号 1:1，
      「同店多账号」不再存在，这个计数恒为 1，留着只会误导界面；
    * 店铺名 / 卖家 ID 来自 `shops` 表（不再是绑定行上的冗余字段）；
    * `status` / `goods_note_total` / 时间字段来自 `account.shop_state`。
    """
    shop = _account_shop(db, account.id)
    if shop is None:
        return {
            "platform_account_id": account.id,
            "bound": False,
            "shop_id_fk": None,
            "shop_user_id": "",
            "shop_name": "",
            "status": "",
            "status_message": "",
            "goods_note_total": 0,
            "synced_notes": 0,
            "goods_synced_at": "",
            "health_checked_at": "",
        }
    state = get_shop_state(account)
    return {
        "platform_account_id": account.id,
        "bound": True,
        #: 店铺实体主键（`shops.id`）。1:1 之后它同时也是「这个账号的店」的标识。
        "shop_id_fk": shop.id,
        "shop_user_id": shop.seller_id,
        "shop_name": shop.shop_name,
        "status": str(state["status"]),
        "status_message": str(state["status_message"]),
        "goods_note_total": int(state["goods_note_total"] or 0),
        "synced_notes": synced_note_count(db, account.id),
        "goods_synced_at": _fmt_state_time(state["goods_synced_at"]),
        #: 最近一次千帆登录态巡检时间（T-31）；空 = 从未巡检。
        "health_checked_at": _fmt_state_time(state["health_checked_at"]),
    }


@router.post("/bind")
def bind_shop(
    payload: BindShopRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory=Depends(get_qianfan_api_adapter_factory),
):
    """绑定千帆店铺：校验 Cookie 可用，再把**关系与凭据各写各的**。

    | 写什么 | 写哪 |
    |---|---|
    | 账号 ↔ 店铺关系 | `platform_accounts.shop_id_fk`（1:1 + UNIQUE，被占用 → 409） |
    | 千帆凭据 | **账号自己的通道**（`account_cookie_versions`；平台由 jar 形态判定） |
    | 状态 | `platform_accounts.shop_state`（刚验过 → `active`） |

    不逆向千帆登录（需求 §九 范围外）：Cookie 由用户在浏览器登录千帆后自行导入。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    cookies = _cookies_to_string(payload.cookie_string)

    adapter = adapter_factory(cookies)
    success, message, raw_payload = adapter.get_seller_info()
    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=message or "千帆登录态校验失败",
        )
    shop = _shop_from_seller_info(raw_payload)
    if not shop["shop_user_id"] and not shop["shop_name"]:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="千帆响应中既没有店铺名也没有店铺 ID，无法确认绑定对象",
        )

    # ①~③ 店铺实体 + 1:1 闸 + 关系 + 状态：**唯一实现**在 service 层
    #     （扫码 / 邮箱密码登录两条路走的是同一份 —— 2026-09-26 收敛）。
    try:
        bind_shop_for_account(
            db,
            user_id=current_user.id,
            account=account,
            shop_user_id=shop["shop_user_id"],
            shop_name=shop["shop_name"],
        )
    except ShopBindError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    # ④ 凭据：写账号自己的通道。**平台由 jar 形态判定**（千帆 jar 带
    #    `access-token-ark.*` ⇒ 落 `ark` 通道），不由这里写死 —— 写死 = 造第二份真相源。
    store_channel_cookies(db=db, account=account, cookies_text=cookies)

    db.commit()
    db.refresh(account)
    return _account_shop_summary(db, account)

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
    * 越权：账号不属于当前用户 → 404（与 bind/unbind 同一口径）；
    * 未绑定 → 404，提示先绑定（没有店铺就没有可巡检的对象）。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    if account_shop_id(db, account.id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="该账号还没有绑定千帆店铺，先绑定再巡检。",
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
    """「无流量笔记」候选清单（**纯本地，零平台请求**）。

    判定 = 千帆原文里 `browse_num`（阅读）/ `note_pay_order_cnt`（支付订单）/
    `note_pay_gmv`（支付金额）**三项都明确存在且都 ≤ 阈值**（默认全 0）。

    | 约定 | 做法 |
    |---|---|
    | **零平台请求** | 三项指标同步时已随 `entry_json` 落库，本地就有 |
    | 缺值不是 0 | 三项任一在原文里缺失 / 非数字 → **不进候选**（「读不到」≠「读到 0」） |
    | 条件可配置 | 时间区间、30 日阅读开关与上限、三项阈值全部可选；不是写死全库 |
    | 分页 | `page` / `page_size`（上限 100），响应带 `total` |
    | 诚实边界 | `coverage.pool`（本地映射条数）/ `coverage.goods_note_total`（店铺笔记总数）必须一起暴露 |
    | 鉴权 | 账号不属于当前用户 → 404；未绑定千帆 → 404（没有绑定就没有候选池） |
    | 只读 | 不写任何表 |
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    shop = _account_shop(db, account.id)
    if shop is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "QIANFAN_BINDING_REQUIRED",
                "message": "尚未绑定千帆商家后台，无法判定笔记流量。请先绑定并同步商品笔记。",
            },
        )

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
        "shop_name": shop.shop_name,
        "queried_at": shanghai_now().strftime("%Y-%m-%d %H:%M:%S"),
        **result,
    }


def _datetime_from(value: str | None, *, end_of_day: bool) -> datetime | None:
    """发布时间入参 → `datetime`（东八区）。

    支持日期 `YYYY-MM-DD`（`end_of_day=True` 取当天 23:59:59）、
    完整时刻 `YYYY-MM-DD HH:MM:SS`、以及**毫秒 / 秒**时间戳整数
    （千帆 `publish_time` 原文是毫秒）。无法解析 → `ValueError`（调用方转 400）。
    """
    from datetime import datetime, timedelta

    from backend.app.core.time import SHANGHAI_TZ

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        stamp = int(text)
        #: 纯数字按千帆口径当**毫秒**；小于 10 位（看起来是秒）自动按秒处理，
        #: 否则 1970 年附近的毫秒值会把整段区间筛成空集。
        if stamp < 10_000_000_000:
            stamp = stamp * 1000
        return datetime.fromtimestamp(stamp / 1000.0, tz=SHANGHAI_TZ)

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(
            f"无法解析的发布时间：{text}（支持 YYYY-MM-DD / YYYY-MM-DD HH:MM:SS / 时间戳）"
        )
    if fmt == "%Y-%m-%d" and end_of_day:
        parsed = parsed + timedelta(hours=23, minutes=59, seconds=59)
    return parsed.replace(tzinfo=SHANGHAI_TZ)


@router.post("/unbind")
def unbind_shop(
    payload: ShopAccountRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """解除**本账号**的绑定并清掉它的本地商品映射（凭据不保留）。

    1:1 之后只剩本账号一件事：清空 `platform_accounts.shop_id_fk`、删掉本账号的
    本地商品映射与千帆状态；店铺行若因此没人用了，一并删掉 —— 否则「解绑了却还在
    店铺列表里」会被读成「还有一个账号绑着」。

    别的账号的数据**一律不动**；店铺若仍被本租户的存活账号引用（历史残留），
    也如实回报，不谎报 0。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    shop_pk = account.shop_id_fk
    if shop_pk is None:
        return {"platform_account_id": payload.platform_account_id, "bound": False, "removed": 0}

    removed = detach_shop(db, account)
    #: 只数**本租户存活账号**（DEF-054 / DEF-055）：把别的租户的残留行算进来，
    #: 用户解绑后仍会看到「店铺还有账号绑着」，与实际情况不符。
    remaining = (
        1
        if account_for_shop(db, shop_pk, user_id=current_user.id) is not None
        else 0
    )
    db.commit()
    return {
        "platform_account_id": payload.platform_account_id,
        "bound": False,
        "removed": removed,
        #: 店铺是否还有别的账号绑着（页面据此决定要不要刷新店铺信息）。
        "shop_remaining_accounts": remaining,
    }


class ShopListRequest(BaseModel):
    """列当前用户的店铺（1:1 的读侧）。全字段可选。"""

    #: 只看与某个账号相关的店铺（不传 = 全部）。
    platform_account_id: int | None = None


@router.post("/shops")
def list_shops(
    payload: ShopListRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """当前用户有哪些千帆店铺、每个店铺挂着**哪个账号**（**纯本地**，不打千帆）。

    | 约定 | 做法 |
    |---|---|
    | 零平台请求 | 只读 `shops` / `platform_accounts` / `shop_note_goods` |
    | **1:1** | 用户 2026-09-22 口径：一个店铺只挂一个账号，所以 `accounts` 至多一项 |
    | 覆盖度口径一致 | `synced_notes` 是该账号的映射条数；`goods_note_total` 取它的 `shop_state` |
    | 不合并用户 | 只看当前用户的店铺；别的用户绑同一个千帆 seller 是另一个实体 |

    消费方：商品列表页的账号选择器（标注每个创作者账号属于哪个店铺）。
    """
    if payload.platform_account_id:
        account = _owned_account(db, current_user, payload.platform_account_id)
        shops = [shop for shop in shops_for_user(db, current_user.id) if shop.id == account.shop_id_fk]
    else:
        shops = shops_for_user(db, current_user.id)

    out: list[dict[str, Any]] = []
    for shop in shops:
        #: 租户闸 + 软删闸（DEF-054 / DEF-055）都由 `account_for_shop` 承担：
        #: 少了它们，一条别的用户 / 已删账号的残留引用会被当成这个店的占用者，
        #: 界面就会显示一个**当前用户根本选不到**的账号。
        account = account_for_shop(db, shop.id, user_id=current_user.id)
        state = get_shop_state(account) if account is not None else None
        out.append(
            {
                "shop_id_fk": shop.id,
                "seller_id": shop.seller_id,
                "shop_id": shop.shop_id,
                "shop_name": shop.shop_name,
                "status": shop.status,
                #: `account_count` **已移除**（2026-09-22）：同店多账号被否决后它恒 0/1，
                #: 留着只会让人以为还有「多个账号读同一个店」的场景。
                "accounts": (
                    []
                    if account is None
                    else [
                        {
                            "platform_account_id": account.id,
                            "nickname": account.nickname or "",
                            "binding_status": str(state["status"]),
                        }
                    ]
                ),
                "synced_notes": synced_note_count(db, account.id) if account is not None else 0,
                "goods_note_total": int(state["goods_note_total"] or 0) if state else 0,
            }
        )
    return {"count": len(out), "shops": out}


@router.post("/item-notes")
def item_notes(
    payload: ItemNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """某个商品**挂了哪些笔记**（商品列表页点「N 篇」看详情；设计 13 §31.6）。

    | 约定 | 做法 |
    |---|---|
    | **关系表源**（2026-09-23 起，需求①） | 走 `note_goods` join `notes`（本地实体关系）—— **不再**读 `shop_note_goods` 平台证据缓存。理由：挂链笔记要展示的是「本地这条笔记」的正文 / 图集 / 发布态，关系表 + notes 表才有这些；缓存表只有平台条目的展示字段 |
    | 覆盖范围 | 同步链路落库的笔记（ark 源解析挂链，§26）+ **本地生成的待发布笔记**（§31） |
    | 诚实报覆盖率 | 关系表只覆盖「同步过 + 本地生成」的笔记，响应同时回 `synced_notes` / `goods_note_total`，前端必须把「可能不全」讲清楚 |
    | 只读 | 不写任何表 |

    响应每篇笔记带：标题 / 正文 / 封面 / 图集 / 视频 / 发布时间 / 互动指标 /
    链接 / **`note_type` + `publish_status` + `publish_error`**（本地生成笔记的
    发布态，弹窗里与内容库同源展示）。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    shop_pk = account_shop_id(db, account.id)
    if shop_pk is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_BINDING_REQUIRED",
                "message": "尚未绑定千帆商家后台，无法反查该商品挂了哪些笔记。",
            },
        )

    state = get_shop_state(account) or {}
    product = db.scalars(
        select(ShopProduct).where(ShopProduct.shop_id_fk == shop_pk, ShopProduct.item_id == payload.item_id)
    ).first()
    if product is None:
        # 台账里没有这件商品 ⇒ 关系表无从查起（空列表，不是错误）。
        return {
            "item_id": payload.item_id,
            "count": 0,
            "notes": [],
            "synced_notes": synced_note_count(db, account.id),
            "goods_note_total": int(state.get("goods_note_total") or 0),
            "goods_synced_at": _fmt_state_time(state.get("goods_synced_at")),
        }

    relations = db.scalars(select(NoteGoods).where(NoteGoods.product_id == product.id)).all()
    note_pks = [relation.note_id for relation in relations]
    notes: list[dict[str, Any]] = []
    if note_pks:
        rows = db.scalars(select(Note).where(Note.id.in_(note_pks))).all()
        asset_rows = db.scalars(
            select(NoteAsset)
            .where(NoteAsset.note_id.in_(note_pks))
            .order_by(NoteAsset.sort_order, NoteAsset.id)
        ).all()
        images_by_note: dict[int, list[str]] = {}
        video_by_note: dict[int, str] = {}
        for asset in asset_rows:
            if not asset.url:
                continue
            if asset.asset_type == "video":
                video_by_note.setdefault(asset.note_id, asset.url)
            else:
                images_by_note.setdefault(asset.note_id, []).append(asset.url)

        for note in rows:
            entry: dict[str, Any] = {}
            raw_value = note.raw_json
            if isinstance(raw_value, dict):
                entry = raw_value
            elif isinstance(raw_value, str) and raw_value.strip():
                try:
                    parsed = json.loads(raw_value)
                    entry = parsed if isinstance(parsed, dict) else {}
                except ValueError:
                    entry = {}
            images = images_by_note.get(note.id, [])
            note_url = f"https://www.xiaohongshu.com/explore/{note.note_id}"
            token = _first_text(entry.get("xsec_token"))
            if token:
                note_url = f"{note_url}?xsec_token={token}&xsec_source=pc_creatormng"
            notes.append(
                {
                    "note_id": note.note_id,
                    "title": note.title or "",
                    "content": note.content or "",
                    "cover": images[0] if images else _first_text(entry.get("cover_url")),
                    "images": images,
                    "video_url": video_by_note.get(note.id, ""),
                    "publish_time": _ts_text(entry.get("publish_time"))
                    or (note.created_at.strftime("%Y-%m-%d %H:%M") if note.created_at else ""),
                    "browse_num": entry.get("browse_num"),
                    "like_num": entry.get("like_num"),
                    "collect_num": entry.get("collect_num"),
                    "comment_num": entry.get("comment_num"),
                    "share_num": entry.get("share_num"),
                    "note_url": note_url,
                    "note_type": note.note_type,
                    "publish_status": note.publish_status or "",
                    "publish_error": note.publish_error or "",
                    "synced_at": note.created_at.strftime("%Y-%m-%d %H:%M") if note.created_at else "",
                }
            )
    notes.sort(key=lambda note: str(note.get("publish_time") or ""), reverse=True)

    return {
        "item_id": payload.item_id,
        "count": len(notes),
        "notes": notes,
        "synced_notes": synced_note_count(db, account.id),
        "goods_note_total": int(state.get("goods_note_total") or 0),
        "goods_synced_at": _fmt_state_time(state.get("goods_synced_at")),
    }


def _price_fen(value: float | None) -> int | None:
    """元 → 分（千帆口径）。**全链路只有这一处做换算**。"""
    if value is None:
        return None
    return int(round(float(value) * 100))


def _ts_text(value: Any) -> str:
    """毫秒时间戳 → `YYYY-MM-DD HH:MM`（千帆列表给的是毫秒整数）。

    与 `shop_products_service._ts_text` 同形：本模块自用（item-notes 的
    `publish_time`），契约函数只有 `normalize_item_from_raw` 那一份。
    """
    try:
        stamp = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    from datetime import datetime, timezone, timedelta

    return (datetime.fromtimestamp(stamp / 1000, tz=timezone(timedelta(hours=8)))).strftime(
        "%Y-%m-%d %H:%M"
    )


def _epoch_millis(value: str | int | None, *, end_of_day: bool) -> int | None:
    """创建时间入参 → Unix **毫秒**（v1.32 起本地筛选用，设计 13 §30.3.2）。

    改造前这个函数返回**秒**（千帆 `search_item_v2` 只认秒，传毫秒被 ES 500）；
    现在列表查本地 `shop_products.create_time_ms`（平台原文是毫秒），所以同一套
    解析、另一个单位。单位换算**仍然只有这一处** —— 「有的地方乘了 1000」
    这类错值不会报错，只会静默筛出空集。

    - `YYYY-MM-DD`：`end_of_day=False` 取 00:00:00.000；`end_of_day=True` 取
      23:59:59.999（用户选「截止 7-16」的自然含义是**含当天**）。
    - `YYYY-MM-DD HH:MM:SS`：按给定时刻取。
    - 整数/数字字符串：按 Unix **秒**透传并换算成毫秒（契约口径不变）。
    - 空值：`None`。无法解析：抛 `ValueError`（由调用方转成 400，不静默忽略）。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text) * 1000

    from datetime import datetime, timedelta

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"无法解析的时间：{text}（支持 YYYY-MM-DD / YYYY-MM-DD HH:MM:SS / Unix 秒）")

    if fmt == "%Y-%m-%d" and end_of_day:
        parsed = parsed + timedelta(hours=23, minutes=59, seconds=59, milliseconds=999)
    # 平台按东八区理解这些时刻（本系统所有展示时间也统一东八区）
    from backend.app.core.time import SHANGHAI_TZ

    return int(parsed.replace(tzinfo=SHANGHAI_TZ).timestamp() * 1000)


@router.post("/items")
def shop_items(
    payload: ShopItemsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """商品列表（**v1.32 起走本地台账**，设计 13 §30.3.2；零平台请求）。

    | 约定 | 做法 |
    |---|---|
    | 本地 | 查 `shop_products`（账号店铺的台账）；**不打千帆** —— 502 不再可能出现 |
    | 有界 | 一次一页，`page_size` 上限 100（Pydantic 钳制）；本地分页无风控问题 |
    | 状态白名单 | 未知 `status` → 400 `QIANFAN_STATUS_UNSUPPORTED`，不猜枚举 |
    | 挂链笔记数（v1.34） | `note_count_op`（`lt`/`lte`/`eq`/`gte`/`gt`，**默认 `lt`**）+ `note_count`；口径 = `note_goods` 关系表（与列表该列同一个数）；未知比较符 → 400 `QIANFAN_NOTE_COUNT_OP_UNSUPPORTED`；`note_count` 为空 ⇒ 不筛 |
    | 绑定门禁 | 未绑店 → 400 `QIANFAN_BINDING_REQUIRED`（与 item-notes 同口径；**不需要千帆令牌** —— 读本地不涉及凭据） |
    | 金额单位 | 入参是**元**、出参是**分**，换算只发生在 `_price_fen()` |
    | 时效诚实 | `queried_at` = 本店 `max(synced_at)`（台账同步时间，不是查询时间）；平台总数 / 同步时间另回 `products_total` / `products_synced_at` |

    筛选 / 排序 / 分页的执行体在 `services/shop_products_service.query_items`
    （唯一实现）；展示契约与改造前**逐字段一致**（`normalize_item_from_raw`）。
    """
    from apis.xhs_qianfan_apis import QIANFAN_ITEM_STATUS_LABELS, QIANFAN_ITEM_STATUSES

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

    # 挂链笔记数的比较符同样是**白名单**（v1.34）：未知值 400 并回支持列表 ——
    # 「不支持的取值静默退化成默认」会让用户以为筛过了（与 status 同一条纪律）。
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

    shop_id = account_shop_id(db, account.id)
    if shop_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_BINDING_REQUIRED",
                "message": "尚未绑定千帆商家后台，无法读取商品列表。请先绑定后再同步商品。",
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

    page = shop_products_service.query_items(
        db,
        account,
        payload,
        shop_id_fk=int(shop_id),
        from_ms=time_from,
        to_ms=time_to,
        price_from_fen=_price_fen(payload.price_from),
        price_to_fen=_price_fen(payload.price_to),
    )
    shop = _account_shop(db, account.id)
    state = get_shop_state(account)
    return {
        "platform_account_id": payload.platform_account_id,
        "source": "local",
        "shop_name": shop.shop_name if shop is not None else "",
        "status": status_key,
        "status_label": status_label,
        "page_no": page["page_no"],
        "page_size": page["page_size"],
        "total": page["total"],
        "count": page["count"],
        "has_more": page["has_more"],
        "queried_at": page["queried_at"],
        #: 实际生效的排序口径（设计 13 §32.1）：后端是排序的唯一权威，前端表头
        #: 显示的就是这两个值 —— 用户点箭头换了排序，勾选与展示都以此为准。
        "sort_field": page["sort_field"],
        "order": page["order"],
        #: 平台商品总数与台账同步时间（§30.3.5）：让「本地有时效」可见。
        #: 0 / 空 = 还没同步过（缺值口径：不是「平台 0 件」）。
        "products_total": int(state["products_total"] or 0),
        "products_synced_at": _fmt_state_time(state["products_synced_at"]),
        "items": page["items"],
    }


@router.post("/items/delete")
def shop_items_delete(
    payload: ShopItemsDeleteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """批量删除**本地台账行**（v1.36，设计 13 §33.10；零平台请求）。

    | 约定 | 做法 |
    |---|---|
    | 删什么 | 只删本店 `shop_products` 的选中行；**平台商品不动**（SDK 无商品删除接口） |
    | 键 | `item_id`（平台商品 ID）；店铺维度由 `shop_id_fk` 限定 ⇒ 传别人的 item_id 只会删 0 行 |
    | 空 | `item_ids` 去重去空白后为空 → 400 `QIANFAN_ITEM_IDS_REQUIRED`（不静默成功） |
    | 上限 | `> MAX_BATCH_DELETE` → 400 `QIANFAN_ITEM_IDS_TOO_MANY`（不静默截断） |
    | 门禁 | 未绑店 → 400 `QIANFAN_BINDING_REQUIRED`（与 `/items` 同口径）；越权 → 404 |
    | 连带 | `note_goods` 关系行**一并删**（⚠️ 2026-09-23 用户口径「删除商品时也需要删除笔记与商品的关联关系」，**取代** v1.36 的「关系行不动」；理由：关系指向已删商品就是孤儿行，与 §26.6 同一个病） |

    返回 `deleted` 是**实际**删除行数（`rowcount`），与 `requested` 对照即可看出
    有没有传进来不存在 / 别的店的 item_id —— 这里不假定「请求几条就删了几条」。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    shop_id = account_shop_id(db, account.id)
    if shop_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_BINDING_REQUIRED",
                "message": "尚未绑定千帆商家后台，无法删除台账商品。请先绑定后再操作。",
            },
        )

    # 去重保序：勾选框重复给同一个 item_id（跨页勾选、手改请求）不该删两次、
    # 也不该把「请求条数」算虚 —— `requested` 是校验过的**有效**条数。
    seen: set[str] = set()
    item_ids: list[str] = []
    for raw in payload.item_ids:
        item_id = str(raw or "").strip()
        if item_id and item_id not in seen:
            seen.add(item_id)
            item_ids.append(item_id)
    if not item_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_ITEM_IDS_REQUIRED",
                "message": "没有选择要删除的商品。",
            },
        )
    if len(item_ids) > MAX_BATCH_DELETE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_ITEM_IDS_TOO_MANY",
                "message": f"单次最多删除 {MAX_BATCH_DELETE} 个商品，本次选了 {len(item_ids)} 个。",
                "limit": MAX_BATCH_DELETE,
            },
        )

    deleted = shop_products_service.delete_items(
        db, shop_id_fk=int(shop_id), item_ids=item_ids
    )
    return {"deleted": deleted, "requested": len(item_ids), "item_ids": item_ids}


@router.post("/item-detail")
def item_detail(
    payload: ItemDetailRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """商品详情弹窗的数据（v1.32 新增，设计 13 §30.3.6；**纯本地**，零平台请求）。

    返回基本信息（与 `/items` 的 `items[]` 元素同构）+ 图集 + SKU 规格明细
    （`detail_json`，同步任务的详情补齐落库）。`skus` 为空且
    `detail_synced_at` 为空串 = **没补过详情**（待补队列中），由前端渲染
    空态 + 说明，**不渲染成 0 行**（缺值口径）。

    | 错误 | 语义 |
    |---|---|
    | 404 | 账号不存在 / 不属于当前用户，或本店没有这个商品 |
    | 400 `QIANFAN_BINDING_REQUIRED` | 未绑定千帆（没有店铺就没有台账） |
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    shop_id = account_shop_id(db, account.id)
    if shop_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_BINDING_REQUIRED",
                "message": "尚未绑定千帆商家后台，无法读取商品详情。请先绑定后再同步商品。",
            },
        )

    detail = shop_products_service.item_detail(
        db, shop_id_fk=int(shop_id), item_id=payload.item_id
    )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="本地台账里没有这个商品（可能还没同步到它）",
        )
    shop = _account_shop(db, account.id)
    return {
        "platform_account_id": payload.platform_account_id,
        "item_id": payload.item_id,
        "shop_name": shop.shop_name if shop is not None else "",
        **detail,
    }


class SyncShopGoodsRequest(BaseModel):
    """「同步店铺商品」的触发参数。

    v1.32 起 `/xhs/qianfan/items` **只读本任务落库的台账**（设计 13 §30.3）——
    这个任务就是商品列表的**唯一数据来源**：翻页到底 + 每页商品列表数据 +
    SKU 规格详情一次性落库（2026-09-23 重构：不再有多轮补齐队列）。
    """

    platform_account_id: int
    #: **不传 = 全量**（设计 13 §21，用户 2026-09-22 口径）：翻到平台没有下一页为止，
    #: 速率由 `CrawlRateLimiter.wait_allow()` 的 5 次/分钟保证。显式传数字时夹到
    #: `[1, ABSOLUTE_PAGE_CAP]`（小批量调试用）。
    max_pages: Optional[int] = Field(default=DEFAULT_MAX_PAGES, ge=1, le=ABSOLUTE_PAGE_CAP)
    #: 每页条数。千帆商品列表实测上限 100。
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    #: 同步模式（设计 13 §17.8 / §22）：`full` = 全量重扫；`incremental` = 只取
    #: `create_time` 在时间窗内的新增/变更商品；`auto`（**默认**）= 按账号有没有
    #: 同步历史自动选（没有 ⇒ 全量，有 ⇒ 从最新数据时间增量）。
    mode: str = SYNC_MODE_AUTO
    #: 同步范围（设计 13 §23）：`full`（默认）= 不设时间界；`range` = 只取
    #: `[start_date, end_date]` 之间**创建**的商品。
    scope: str = SYNC_SCOPE_FULL
    #: 起始日期 `YYYY-MM-DD`（`scope=range` 时生效）。
    start_date: str = ""
    #: 结束日期 `YYYY-MM-DD`（`scope=range` 时生效，含当天一整天）。
    end_date: str = ""


@router.post("/sync-shop-goods")
def sync_shop_goods(
    payload: SyncShopGoodsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把该店铺的商品同步到本地 `shop_products` 表（需求 R2）。

    **异步**：立即返回任务摄要（`pending`），同步在后台线程里慢慢跑（**全量翻页** +
    按账号限流**等待** + 页间停顿，见设计 13 §21）。

    **防重（需求 N3）**：该账号已有未完成的商品同步任务 → 返回**已有任务**，
    不新建、不重复打平台。

    **完成语义**：只有翻完最后一页（或已存够总数）才删除本次未出现的旧行；
    半途而废**不删**（删 = 把没扫到的商品当已下架丢掉）。凭据失效（`-13000`）
    只把绑定标 `invalid`，**已有商品一行不删**。

    **内联详情**（2026-09-23 重构）：每页列表商品的 SKU 规格详情在落库前
    拉取（`get_skus_by_item_ids`，批量 10），列表数据 + 规格详情一次性 upsert。
    不再有独立的多轮补齐队列——列表翻完 = 详情也全了。详情失败不影响列表落库，
    失败的商品下轮同步时重试。
    """
    _owned_account(db, current_user, payload.platform_account_id)
    if account_shop_id(db, payload.platform_account_id) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_BINDING_REQUIRED",
                "message": "尚未绑定千帆商家后台，无法同步商品。请先绑定后再同步。",
            },
        )

    # §23：弹窗选的「全量 / 时间区间」→ 毫秒窗口；解析不了 ⇒ (0,0) 并在任务行说明。
    time_from_ms, time_to_ms = sync_task_service.resolve_time_window(
        payload.scope, payload.start_date, payload.end_date
    )
    scope_note = ""
    if sync_task_service.normalize_scope(payload.scope) == sync_task_service.SYNC_SCOPE_RANGE:
        scope_note = (
            f"按时间区间同步：{payload.start_date} ~ {payload.end_date}"
            if time_from_ms and time_to_ms
            else "未识别到有效的时间区间，已按全量同步"
        )
    summary = shop_goods_sync_service.start_shop_goods_sync(
        payload.platform_account_id,
        user_id=current_user.id,
        max_pages=sync_task_service.clamp_pages(payload.max_pages),
        page_size=sync_task_service.clamp_page_size(payload.page_size),
        source="manual",
        mode=sync_task_service.normalize_mode(payload.mode),
        time_from_ms=time_from_ms,
        time_to_ms=time_to_ms,
    )
    summary.pop("reused", None)
    # §23：把「实际生效的同步范围」带回前端 —— 区间没解析出来时用户必须知道
    # 自己其实跑了全量，否则会以为只同步了一个时间段。
    if scope_note:
        summary["scope_note"] = scope_note
    return summary


@router.get("/sync-shop-goods/status")
def sync_shop_goods_status(
    platform_account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """商品同步的进度查询（前端轮询）。

    无进行中任务时 `task_id: null, status: "idle"` —— 前端据此停轮询。
    """
    _owned_account(db, current_user, platform_account_id)
    return sync_task_service.status_payload(
        db, SHOP_GOODS_SYNC_TASK_TYPE, platform_account_id
    )
