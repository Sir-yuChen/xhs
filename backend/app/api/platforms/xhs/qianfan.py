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
import logging
import mimetypes
import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.adapters.xhs.qianfan_api_adapter import (
    get_qianfan_api_adapter_factory,  # noqa: F401  重导出：`from ...xhs.qianfan import ...` 与测试打桩继续有效
)
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user, get_owned_active_account
from backend.app.core.time import shanghai_now
from backend.app.api.ai import get_image_ai_client
from backend.app.services.ai_service import ImageAiClient, OpenAICompatibleTextClient
from backend.app.services.business_provider_keys import (
    BUSINESS_KEY_GENERATE_PRODUCT_DETAIL_IMAGE,
    BusinessKey,
)
from backend.app.services import detail_image_prompt, image_prompt
from backend.app.services.llm_gateway_client import _type_satisfies
from backend.app.models.material import MaterialFolder
from backend.app.services.material_scan_service import is_safe_material_path
from backend.app.services.media_generation import _media_dir
from backend.app.models.platform_account import PlatformAccount
from backend.app.models.note import Note, NoteAsset
from backend.app.models.shop import NoteGoods
from backend.app.models.task import Task
from backend.app.services.shop_health_service import check_account_health
from backend.app.services import scheduled_job_runner, sync_task_service
from backend.app.services import shop_goods_sync_service
from backend.app.services import shop_products_service
from backend.app.services.job_registry import JOB_KEY_SHOP_GOODS_SYNC
from backend.app.services.note_queue_service import SOURCE_ACCOUNT_CARD_GOODS_SYNC
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
    get_shop_state,
    has_ark_session,
    synced_note_count,
)
from backend.app.models.user import User

router = APIRouter(prefix="/xhs/qianfan", tags=["xhs-qianfan"])

logger = logging.getLogger(__name__)


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


class ItemDetailRequest(BaseModel):
    """商品详情弹窗的数据请求（v1.32，设计 13 §30.3.6；**纯本地**）。"""

    platform_account_id: int
    item_id: str = Field(min_length=1, max_length=64)

    model_config = ConfigDict(extra="forbid")


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
    就是 `item_id`（表格 `rowKey`），且归属维度已由 `platform_account_id` 限定 ⇒
    传别的账号的 item_id 只会删到 0 行，不会越权。
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

    model_config = ConfigDict(extra="forbid")


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


def _fmt_state_time(value: Any) -> str:
    """`shop_state` 里的 ISO 时间 → 展示用 `YYYY-MM-DD HH:MM`；空 / 坏值 → 空串。"""
    from backend.app.services.shop_binding_service import parse_state_time

    parsed = parse_state_time(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed is not None else ""


def _account_shop_summary(db: Session, account: PlatformAccount) -> dict[str, Any]:
    """账号的千帆能力概览（**纯本地**，不打千帆）。

    ## `bound` 字段名保留，语义已改（ADR-021 / 设计 18，2026-09-30）

    用户 2026-09-30 口径：*「商品页走的本地数据库数据，不应该校验是否存在千帆店铺，
    所有校验应该校验的是该小红书账号下是否存在千帆可用的会话」*。

    ⇒ **`bound` 现在回答的是「本账号有没有可用的 `ark` 会话」**，而不是
    「有没有绑店」。字段名刻意不变（省前端连带改），但语义写在这里，
    前端提示语也必须跟着改（旧文案「请先绑定千帆店铺」现在是错的）。

    * `shop_id_fk` 字段**保留但恒为 `None`** —— 店铺实体已退场。留着是给老前端
      一个明确的空值，而不是让它 `KeyError`；新前端不该再读它。
    * `shop_user_id` / `shop_name` 改从 `account.seller_id` / 账号昵称派生
      （`seller_id` 是账号属性了，店铺名就是账号的千帆卖家名）。

    ⚠️ **归属（seller_id）与能力（ark 令牌）是两件事**：有 `seller_id` 没有令牌
    ⇒ 仍然 `bound=False`（调不了千帆）；有令牌没有 `seller_id` ⇒ `bound=True`
    （能调，`seller_id` 等首次真调千帆时回填）。账号 9 就是后者。
    """
    state = get_shop_state(account)
    has_ark = has_ark_session(db, account.id)
    return {
        "platform_account_id": account.id,
        #: ⚠️ **语义已改为「有可用的 ark 会话」**（不是「已绑店」）。见 docstring。
        "bound": has_ark,
        #: 已退场字段（店铺实体不存在了）。恒 `None`，只为老前端不 `KeyError`。
        "shop_id_fk": None,
        "shop_user_id": account.seller_id or "",
        "shop_name": _shop_display_name(db, account),
        "status": str(state["status"]),
        "status_message": str(state["status_message"]),
        "goods_note_total": int(state["goods_note_total"] or 0),
        "synced_notes": synced_note_count(db, account.id),
        "goods_synced_at": _fmt_state_time(state["goods_synced_at"]),
        #: 最近一次千帆登录态巡检时间（T-31）；空 = 从未巡检。
        "health_checked_at": _fmt_state_time(state["health_checked_at"]),
    }


def _shop_display_name(db: Session, account: PlatformAccount) -> str:
    """店铺展示名 —— 店铺不是实体了，所以取「账号昵称」（千帆卖家名与之同源）。

    千帆 `get_seller_info()` 回的 `seller_name` 在登录时就写进了账号昵称的
    更新链路；这里不额外存一份名字（**同一语义不留两份实现**）。
    """
    return (account.nickname or "").strip()


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
    | 鉴权 | 账号不属于当前用户 → 404；**无千帆登录态门禁** —— 纯本地读，未登录也能判定已同步的数据 |
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
    # ⚠️ 无千帆登录态门禁：本接口**纯本地**（`note_goods` join `notes`，零平台
    # 请求），未登录千帆也能看已落库的挂链关系（用户 2026-09-30 口径）。

    state = get_shop_state(account) or {}
    # ⚠️ 2026-09-27（§26.7）：关系表按**平台 ID** 挂 ⇒ 直接按 `item_id` 查，
    # **不再先找台账行**（台账里没有这件商品时也有关系 —— 关系落库不校验商品存在）。
    relations = db.scalars(
        select(NoteGoods).where(
            NoteGoods.user_id == current_user.id,
            NoteGoods.item_id == payload.item_id,
        )
    ).all()
    note_ids = [str(relation.note_id or "") for relation in relations]
    note_ids = [note_id for note_id in dict.fromkeys(note_ids) if note_id]
    notes: list[dict[str, Any]] = []
    if note_ids:
        rows = db.scalars(
            select(Note).where(Note.user_id == current_user.id, Note.note_id.in_(note_ids))
        ).all()
        note_pks = [note.id for note in rows]
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
    | 归属门禁 | **无门禁** —— 本页读**本地台账** `shop_products`（零平台请求），未登录千帆也能看已同步的数据；登录态只决定「能不能同步新数据」 |
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

    # ⚠️ **本页无千帆登录态门禁**（用户 2026-09-30 明确口径）：商品列表读的是
    # **本地台账**（`shop_products`，零平台请求），未登录千帆也能看已同步的数据。
    # 登录态只决定「能不能从平台同步新数据」，不决定「本地已有数据能不能读」——
    # 无登录态就拦下整页 = 把已落库的数据锁死，是错的。
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
        platform_account_id=account.id,
        from_ms=time_from,
        to_ms=time_to,
        price_from_fen=_price_fen(payload.price_from),
        price_to_fen=_price_fen(payload.price_to),
    )
    state = get_shop_state(account)
    return {
        "platform_account_id": payload.platform_account_id,
        "source": "local",
        "shop_name": _shop_display_name(db, account),
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
    | 删什么 | 只删本账号 `shop_products` 的选中行；**平台商品不动**（SDK 无商品删除接口） |
    | 键 | `item_id`（平台商品 ID）；归属维度由 `platform_account_id` 限定 ⇒ 传别人的 item_id 只会删 0 行 |
    | 空 | `item_ids` 去重去空白后为空 → 400 `QIANFAN_ITEM_IDS_REQUIRED`（不静默成功） |
    | 上限 | `> MAX_BATCH_DELETE` → 400 `QIANFAN_ITEM_IDS_TOO_MANY`（不静默截断） |
    | 门禁 | **无门禁** —— 删的是**本地台账**行（零平台请求），未登录千帆也能删本地数据；越权 → 404 |
    | 连带 | `note_goods` 关系行**一并删**（⚠️ 2026-09-23 用户口径「删除商品时也需要删除笔记与商品的关联关系」，**取代** v1.36 的「关系行不动」；理由：关系指向已删商品就是孤儿行，与 §26.6 同一个病） |

    返回 `deleted` 是**实际**删除行数（`rowcount`），与 `requested` 对照即可看出
    有没有传进来不存在 / 别的账号的 item_id —— 这里不假定「请求几条就删了几条」。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    # ⚠️ 无千帆登录态门禁：删的是**本地台账行**（零平台请求），与 `/items` 同口径
    # （用户 2026-09-30：本地数据读 / 删不需要登录态）。

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
        db, platform_account_id=account.id, item_ids=item_ids
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
    | 404 | 账号不存在 / 不属于当前用户，或本账号没有这个商品 |

    ⚠️ **无千帆登录态门禁**：详情读本地台账（`detail_json`），零平台请求，
    与 `/items` 同口径 —— 未登录千帆也能看已同步的商品详情。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)

    detail = shop_products_service.item_detail(
        db, platform_account_id=account.id, item_id=payload.item_id
    )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="本地台账里没有这个商品（可能还没同步到它）",
        )
    return {
        "platform_account_id": payload.platform_account_id,
        "item_id": payload.item_id,
        "shop_name": _shop_display_name(db, account),
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
    #: 速率由 `CrawlRateLimiter.wait_allow()` 的**运行时额度**（默认 60 次/分钟，
    #: 设置页可改，设计 10 §7 / 设计 13 §38.2）保证。显式传数字时夹到
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
    #: **商品状态范围**（设计 13 §30.3.2，2026-10-02 用户口径「商品同步更新需求为同步
    #: 全部状态的商品」）：`all`（**默认**）= 平台侧全部状态；也可只同步某一个状态
    #: （取值 = `QIANFAN_ITEM_STATUSES` 的键，与平台页面 7 个 tab 同源）。
    #: 坏值 ⇒ `all`（退回信息最全的取值，不静默少同步）。
    item_status: str = "all"

    model_config = ConfigDict(extra="forbid")


@router.post("/sync-shop-goods")
def sync_shop_goods(
    payload: SyncShopGoodsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把该店铺的商品同步到本地 `shop_products` 表（需求 R2）。

    **语义 = 手动触发定时任务 `system.shop_goods_sync` 的「执行一次」**（设计 13 §35.17，
    2026-09-27 用户口径）：日志落 `scheduled_job_logs`（`trigger_type=manual` —— 任务中心
    「任务日志」看得到、执行期间带进度心跳），执行体是 `run_scheduled_shop_goods_syncs` 的
    **定向模式**（只跑本账号、**不**省流跳过、**不**重入；未绑店给结构化失败），业务任务行
    照旧落 `tasks`。同步本体在后台跑（**全量翻页** + 按账号限流**等待** + 页间停顿，§21）。

    **响应**：`{log_id, status, account_id, scope_note}` —— **不再回任务摄要**；任务号与
    进度由**首查** `GET …/sync-shop-goods/status` 给出（§35.17.4 的轮询纪律）。

    **完成语义**：只有翻完最后一页（或已存够总数）才删除本次未出现的旧行；
    半途而废**不删**（删 = 把没扫到的商品当已下架丢掉）。凭据失效（`-13000`）
    只把绑定标 `invalid`，**已有商品一行不删**。

    **内联详情**（2026-09-23 重构）：每页列表商品的 SKU 规格详情在落库前
    拉取（`get_skus_by_item_ids`，批量 10），列表数据 + 规格详情一次性 upsert。
    不再有独立的多轮补齐队列——列表翻完 = 详情也全了。详情失败不影响列表落库，
    失败的商品下轮同步时重试。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)
    if not has_ark_session(db, account.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "ARK_SESSION_REQUIRED",
                "message": "该账号没有可用的千帆（ark）登录态，无法同步商品。"
                           "先在账号矩阵导入其千帆登录态后再同步。",
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
    mode = sync_task_service.normalize_mode(payload.mode)
    max_pages = sync_task_service.clamp_pages(payload.max_pages)
    page_size = sync_task_service.clamp_page_size(payload.page_size)
    item_status = sync_task_service.normalize_item_status(payload.item_status)
    summary = shop_goods_sync_service.start_shop_goods_sync(
        payload.platform_account_id,
        user_id=current_user.id,
        # §35.12：来源要**具体到按钮**（遗留值 `manual` 会显示成「页面按钮（早期数据）」）。
        source=SOURCE_ACCOUNT_CARD_GOODS_SYNC,
        # 同步语义全部传下去（漏一个 = 用户选的全量 / 时间区间静默丢失）。
        mode=mode,
        time_from_ms=time_from_ms,
        time_to_ms=time_to_ms,
        item_status=item_status,
        max_pages=max_pages,
        page_size=page_size,
    )
    task_id = summary.get("task_id")
    log_id = None
    if task_id and not summary.get("reused"):
        log_id = scheduled_job_runner.run_targeted_job(
            user_id=current_user.id,
            job_key=JOB_KEY_SHOP_GOODS_SYNC,
            task_id=int(task_id),
            task_type=sync_task_service.SHOP_GOODS_SYNC_TASK_TYPE,
            # 账号快照（迁移 `j1k2l3m4n5o6`）：账号卡「同步中」的判据 = 该账号
            # **最新一条**同步日志的状态（2026-09-27 用户口径）⇒ 日志必须记账号。
            account_id=payload.platform_account_id,
            # 日志快照里留一份**本次实际生效**的语义（与任务行对得上）。
            params_extra={
                "account_id": payload.platform_account_id,
                # 账号名称快照（2026-09-27 用户口径「执行账号应是当前账号信息卡的店铺」）：
                # 「执行详情」的「执行账号」显示账号名称，与账号卡一一对应。
                "account_name": account.nickname or "",
                "mode": mode,
                "time_from_ms": time_from_ms,
                "time_to_ms": time_to_ms,
            },
            # §35.17.3：执行期间把该任务行的进度映到定时任务日志的 `progress` 列。
            progress_probe=sync_task_service.task_progress_probe(int(task_id)),
        )
    # §23：把「实际生效的同步范围」带回前端 —— 区间没解析出来时用户必须知道
    # 自己其实跑了全量，否则会以为只同步了一个时间段。
    return {**summary, "log_id": log_id, "scope_note": scope_note}


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
        db,
        SHOP_GOODS_SYNC_TASK_TYPE,
        platform_account_id,
        # 账号卡「同步中」按**该账号最新一条同步日志**判（2026-09-27 用户口径）。
        user_id=current_user.id,
        job_key=JOB_KEY_SHOP_GOODS_SYNC,
    )


#: 生成详情图时最多送几张参考图（§13.3.6）。
#: v2.23：5 → **6**（用户口径「最多选 6 张」；主图与详情图合并成**同一组**后共用这个上限）。
#: ⚠️ 定义在**请求模型之前**（`Field(max_length=…)` 在类体求值时引用它）。
MAX_DETAIL_IMAGE_REFERENCES: int = 6

#: v2.18/v2.20：生成张数范围（用户口径「最多 10 张最少 3 张」）。
#: ⚠️ 每张 = 一次独立的上游请求（串行）⇒ 有上限止损；下限 3 是因为生成详情图
#: 通常要成组使用（单张价值低，用户总要多来几张挑）。
#: ⚠️ 定义在**请求模型之前**：`GenerateDetailImageRequest.count` 的 `default/ge/le`
#: 直接引用它们（定义在后 = 类体求值时 NameError）。
MIN_DETAIL_IMAGE_COUNT: int = 3
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

    model_config = ConfigDict(extra="forbid")


def _default_detail_image_prompt(item: dict[str, Any], skus: list[dict[str, Any]]) -> str:
    """按商品信息拼默认提示词（用户没填 `prompt` 时用）。

    只用**台账里真实存在**的字段（标题 / 规格名 / 价格带），缺值就不写进提示词 ——
    编造卖点等于替用户宣称商品没有的属性。
    """
    title = str(item.get("title") or "").strip()
    parts = [f"电商商品详情图，商品：{title}" if title else "电商商品详情图"]
    variant_names = [
        str(sku.get("variant_name") or "").strip()
        for sku in skus
        if str(sku.get("variant_name") or "").strip()
    ]
    if variant_names:
        # 去重保序，最多取 4 个（再多提示词会失控）
        seen: list[str] = []
        for name in variant_names:
            if name not in seen:
                seen.append(name)
        parts.append("可选规格：" + "、".join(seen[:4]))
    parts.append("竖版长图构图，干净背景，突出商品主体与卖点排版")
    # v2.21（§13.3.7.7）：**一致性锁** —— 回落路径也带上公共约束库的防漂移三条。
    # ⚠️ 这段是**直接送出图模型的最终 prompt**（不经文本模型），故用公共库的
    # 防漂移与合规约束；构图那句上面已写，不重复。
    parts.extend(image_prompt.GENERATION_SAFETY_LINES)
    parts.extend(image_prompt.PLATFORM_SAFE_LINES)
    return "；".join(parts)


def _save_to_material_folder(
    db: Session,
    *,
    user_id: int,
    folder_id: int,
    url: str,
    filename_hint: str,
) -> dict[str, Any]:
    """把出图**下载到用户指定的素材库文件夹**（设计 17 §13.3.7，v2.19）。

    用户口径：「图片支持下载到本地指定的素材库中也就是本地指定文件夹下」。

    ⚠️ **与 `_download_media` 的区别**：那个写 `storage/media/`（本项目的托管目录，
    给「素材库面板的 media 来源」用）；本函数写**用户自己的绝对路径目录**
    （`material_folders.path`）—— 那是用户在素材库页配置的真实文件夹。

    ⚠️ **只落文件，不建 `material_files` 索引行**：索引由用户点「扫描」时按真实文件
    生成（`material_scan_service.scan_folder`）。在这里插索引行 = 与扫描抢同一份真相源，
    且会与「只索引不搬移」的既有口径打架（§3.4）。
    """
    folder = db.scalar(
        select(MaterialFolder).where(
            MaterialFolder.id == folder_id, MaterialFolder.user_id == user_id
        )
    )
    if folder is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="指定的素材库文件夹不存在（或不属于当前用户）",
        )

    safe, reason = is_safe_material_path(folder.path)
    if not safe:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"素材库文件夹路径不可写：{reason}",
        )

    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        # 本地 media URL ⇒ 直接复制（已在磁盘上）。
        if url.startswith("/api/files/media/"):
            source = _media_dir() / url.split("/")[-1]
            if not source.is_file():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="出图本地文件不存在，无法存入素材库",
                )
            target_dir = Path(folder.path)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{filename_hint}{source.suffix or '.png'}"
            shutil.copyfile(source, target)
            return {"path": str(target), "folder_id": folder.id, "filename": target.name}
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无法识别的出图地址：{str(url)[:64]}",
        )

    try:
        response = requests.get(url, timeout=120)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"出图下载失败，无法存入素材库：{exc}",
        ) from exc

    content_type = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"上游返回的不是图片（Content-Type={content_type}），未存入素材库",
        )
    extension = mimetypes.guess_extension(content_type or "") or ".png"
    if extension == ".jpe":
        extension = ".jpg"

    target_dir = Path(folder.path)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{filename_hint}{extension}"
    target.write_bytes(response.content)
    return {"path": str(target), "folder_id": folder.id, "filename": target.name}


def _text_model_context(
    db: Session, user: User, business_key: BusinessKey
) -> tuple[Any | None, Any | None, str]:
    """取**文本/多态模型**上下文（详情图提示词整理用，v2.19）。

    ⚠️ **与 `_business_model_context` 的关键区别：缺配置不抛异常，返回 `(None, None, "")`**
    —— 整理是可选的增强层，缺失时由调用方**回落本地拼接**。
    用同一个业务点 key 解析：用户可能给该业务点挂的是**多态**模型（既能整理又能出图），
    也可能只挂了 image 类 ⇒ 那时类型不符 ⇒ 同样回落（不报错）。

    ⚠️ **不复用 `_business_model_context`**：那个按业务点 `kind` 校验（image 类业务点
    只接受 image/multimodal 模型，且失败即 400）—— 我们要的是「拿得到就用、拿不到就算」，
    语义不同。但**解析入口仍是同一份** `resolve_business_provider`（不另写一份查找）。
    """
    try:
        from backend.app.services.llm_gateway_client import resolve_business_provider

        ctx = resolve_business_provider(db, user, business_key.key)
    except Exception as exc:  # noqa: BLE001 - 未配 / 停用 / 类型不符都回落
        logger.debug("详情图提示词整理：文本模型不可用（%s）", exc)
        return None, None, ""

    model_type = str(getattr(ctx, "model_type", "") or "")
    if not _type_satisfies(kind="text", model_type=model_type):
        # 挂的是纯 image 模型（打不了 /chat/completions）⇒ 回落，不报错。
        return None, None, ""

    config = SimpleNamespace(
        base_url=ctx.base_url,
        model_name=ctx.model_name,
    )
    return OpenAICompatibleTextClient(), config, ctx.api_key


@router.post("/generate-detail-image")
def generate_detail_image(
    payload: GenerateDetailImageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    image_ai_client: ImageAiClient = Depends(get_image_ai_client),
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
    from backend.app.models.generation import AigcMediaAsset
    from backend.app.api.ai import _business_model_context

    account = _owned_account(db, current_user, payload.platform_account_id)

    detail = shop_products_service.item_detail(
        db, platform_account_id=account.id, item_id=payload.item_id
    )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="本账号的商品台账里没有这个商品（先同步商品再试）",
        )

    images = [url for url in (detail.get("images") or []) if url]
    # v2.15（§13.3.6）：参考图可选 —— 勾选的主图 / 详情图，**必须过台账白名单**。
    # v2.23：主图与详情图**合并为一组「可选参考图」**（用户口径）⇒ 白名单合成一个集合，
    # 不再按来源归组（分组的语义已移到「生成哪一类图」的 Tab 上）。
    allowed: set[str] = set(detail.get("product_images") or []) | set(
        detail.get("detail_images") or []
    )
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

    # 业务点 → 服务商（不回落；未配置 / 类型不符 ⇒ 400 结构化）。
    # ⚠️ 复用 `ai.py::_business_model_context`（**同一份**错误映射实现）：
    # 自己再写一遍 `resolve + except LLMGatewayError → 400` = 同一语义两份实现（DEF-044），
    # 两份会在「新增错误码」时分叉。
    model_config, api_key = _business_model_context(
        db, current_user, BUSINESS_KEY_GENERATE_PRODUCT_DETAIL_IMAGE
    )

    prompt = (payload.prompt or "").strip() or _default_detail_image_prompt(
        detail.get("item") or {}, detail.get("skus") or []
    )
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
            db, current_user, BUSINESS_KEY_GENERATE_PRODUCT_DETAIL_IMAGE
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
        group_prompt = str(setting.get("prompt") or "").strip() or prompt
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
                "refs": effective_refs[:MAX_DETAIL_IMAGE_REFERENCES],
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
        task_type="ai_product_detail_image",
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
            **({"refine_note": refine_note} if refine_note else {}),
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
        for index in range(group_count):
            # 单张重做时只跑一次（count=1），用**原始序号**标记结果。
            output_index = regen_index if regen_group else index + 1
            try:
                result = image_ai_client.generate_image(
                    model_config=model_config,
                    api_key=api_key,
                    # v2.22：**本组自己的提示词**（每组可独立设置）。
                    prompt=group_prompt,
                    # 参考图 = **本组**勾选的（不跨组混送）。
                    reference_images=group_refs[:MAX_DETAIL_IMAGE_REFERENCES],
                )
            except Exception as exc:  # noqa: BLE001 - 服务商五花八门，一律结构化转出
                errors.append(f"{group_label} 第 {output_index} 张：{exc}")
                done += 1
                continue

            url = result.get("url") or ""
            asset = AigcMediaAsset(
                user_id=current_user.id,
                asset_type="image",
                # ⚠️ 缺值口径：服务商没回本地路径时留空串（**不拿 url 冒充 local_path** ——
                # 那是两个语义，冒充会让「素材库」显示一条不存在的本地文件）。
                local_path=result.get("local_path") or "",
                url=url or None,
                prompt=prompt,
                model_name=model_config.model_name,
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
