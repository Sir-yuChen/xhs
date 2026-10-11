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
from urllib.parse import quote
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import requests
from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.adapters.xhs.qianfan_api_adapter import (
    get_qianfan_api_adapter_factory,  # noqa: F401  重导出：`from ...xhs.qianfan import ...` 与测试打桩继续有效
)
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user, get_owned_active_account
from backend.app.core.time import shanghai_now
from backend.app.api.ai import get_image_ai_client, get_text_ai_client
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
from backend.app.models.order import AfterSale, Order
from backend.app.models.shop import NoteGoods
from backend.app.models.task import Task
from backend.app.services.shop_health_service import check_account_health
from backend.app.services import scheduled_job_runner, sync_task_service
from backend.app.services import order_sync_service
from backend.app.services import shop_goods_sync_service
from backend.app.services import shop_products_service
from backend.app.services import shop_item_operate_service as operate_service
from backend.app.services import shop_draft_package as draft_package
from backend.app.services import shop_draft_service as draft_service
from backend.app.services.job_registry import (
    JOB_KEY_SHOP_GOODS_SYNC,
    JOB_KEY_ORDER_SYNC,
)
#: 图片空间排序字段白名单 —— **唯一来源**（service 层）。本文件不得再另存一份
#: （2026-10-07 曾因复制一份漏 `updateTime` ⇒ 回收站视图 400）。
from backend.app.services.picture_space_service import (
    MATERIAL_ORDER_FIELDS as MATERIAL_ORDER_FIELDS,
)
from backend.app.services.note_queue_service import (
    SOURCE_ACCOUNT_CARD_GOODS_SYNC,
    SOURCE_ACCOUNT_CARD_ORDER_SYNC,
)
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

#: 批量**商品操作**（上架 / 下架 / 删除**平台商品**）单次上限（2026-10-05 需求 7）。
#: ⚠️ 与 `MAX_BATCH_DELETE` **不是同一个上限**，语义也不同：
#: * `MAX_BATCH_DELETE` 管「删本地台账行」（零平台请求，删错只是本地少一行，下次同步会回来）；
#: * 本值管「改**平台**商品」（下架影响在售、删除不可恢复）⇒ 更保守。
#: 与 `shop_item_operate_service.MAX_ITEMS_PER_TASK` **必须同值**（前后端同值纪律：
#: 两处不同值 = 前端放行后端拒，或反之静默截断）。
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
    #: ⚠️ 分组筛选（`group_id`）已随「彻底移除商品分组」退场（2026-10-05）。
    #: 删字段而不是留着 —— 留着会让 `group_id` 被静默忽略（用户以为筛过了）。


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
    """店铺展示名（**委托给服务层唯一实现**，不在 API 层再写一份）。

    见 `shop_binding_service.shop_display_name` —— 商品页与订单页共用同一份。
    """
    from backend.app.services.shop_binding_service import shop_display_name

    return shop_display_name(account)


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
    #: 店铺冻结标记（`seller_freeze`）：**本地无法从商品行推出**（它不随条目下发），
    #: 只能由写操作的失败回执写入 `shop_state`（见 `shop_item_operate_service`）。
    #: 在这里注入到每一条上，供 `allowed_actions` 判「能不能改」——
    #: 冻结店三个动作全不可做（千帆原文：「您的店铺被冻结，解冻后可发布、编辑商品」）。
    if state.get("seller_freeze"):
        for item in page["items"]:
            item["seller_freeze"] = True
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
#: ⚠️ 每张 = 一次独立的上游请求（串行）⇒ 有上限止损。
#: ⚠️ **下限 3 → 1**（用户口径 2026-10-03：「生成张数调整最低一张」）。
#: 原下限 3 的理由是「详情图通常成组使用」，但那**替用户做了决定** ——
#: 想先出一张看效果（尤其调试期、额度紧张时）也应当允许。
#: 默认值保持 3（多数场景仍要多张挑，不改默认不改变既有体验）。
#: ⚠️ 定义在**请求模型之前**：`GenerateDetailImageRequest.count` 的 `default/ge/le`
#: 直接引用它们（定义在后 = 类体求值时 NameError）。
#: 生成商品详情图的任务类型（`Task.task_type`；建任务处与本端点的反查**共用**，
#: 写死两处就是漂移源 —— 改一处忘一处会让「保存到素材库」静默查不到分组）。
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


def _resolve_style_reference(
    db: Session,
    current_user: User,
    *,
    file_id: int | None,
) -> tuple[str, str]:
    """把**风格参考图**（素材库文件 ID）解析成上游可取的 URL + 提示词说明（§13.7）。

    返回 `(url, note)`；不传 ⇒ `("", "")`（可选增强，零回归）。

    ⚠️ **只收文件 ID，不收 URL**：出图端点已有 SSRF 防线（商品参考图必须过台账
    白名单），若风格图收 URL 就等于开了第二个「下载任意地址」的口子。
    文件 ID 的归属校验走既有唯一实现（与素材端点同口径）。

    ⚠️ **归属不符 ⇒ 404，不静默忽略**：静默忽略的观感是「出图成功了」，
    但用户精心挑的风格图**根本没参与**（与 §13.6.3「不静默回落」同一纪律）。
    """
    if not file_id:
        return "", ""

    from backend.app.models.material import MaterialFile

    row = db.scalar(
        select(MaterialFile).where(
            MaterialFile.id == file_id,
            MaterialFile.user_id == current_user.id,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "STYLE_REFERENCE_NOT_FOUND",
                "message": "风格参考图不存在或不属于当前用户",
            },
        )
    if row.file_type != "image":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "STYLE_REFERENCE_NOT_IMAGE",
                "message": "风格参考图必须是图片文件",
            },
        )
    # 复用素材端点同一个取字节 URL（`/api/xhs/materials/files/{id}/file`）——
    # `ai_service._resolve_image_ref` 认这个前缀并转 base64 送上游。
    return f"/api/xhs/materials/files/{row.id}/file", detail_image_prompt.style_reference_note()


def _render_detail_image_template(
    db: Session,
    current_user: User,
    *,
    template_id: int,
    item: dict[str, Any],
    skus: list[dict[str, Any]],
    extra_prompt: str = "",
) -> tuple[str, str]:
    """用公共提示词模板渲染详情图提示词（v2.24，设计 17 §13.5.4）。

    返回 `(提示词, 说明)`；说明供响应 `prompt_source` 如实标注用了哪个模板。

    ⚠️ **不静默回落**：模板不存在 / 不属于本用户 ⇒ 404；骨架含未知插槽 ⇒ 400。
    既然用户**显式选了**模板，「以为用了、其实没用」比报错更坏。
    """
    from backend.app.models.prompt import PromptModule, PromptTemplate
    from backend.app.services import prompt_renderer

    template = db.scalar(
        select(PromptTemplate).where(
            PromptTemplate.id == template_id,
            PromptTemplate.user_id == current_user.id,
        )
    )
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "PROMPT_TEMPLATE_NOT_FOUND",
                "message": f"提示词模板 #{template_id} 不存在或不属于当前用户",
            },
        )

    module_ids = [int(mid) for mid in (template.module_ids or []) if isinstance(mid, int)]
    modules: list[PromptModule] = []
    if module_ids:
        modules = list(
            db.scalars(
                select(PromptModule).where(
                    PromptModule.user_id == current_user.id,
                    PromptModule.id.in_(module_ids),
                    PromptModule.enabled.is_(True),
                )
            )
        )

    try:
        rendered = detail_image_prompt.render_template_prompt(
            template=template,
            modules=modules,
            item=item,
            skus=skus,
            extra_prompt=extra_prompt,
        )
    except prompt_renderer.PromptRenderError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": error.code, "message": error.message},
        ) from error

    return rendered, f"模板「{template.name}」"


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
    model_config, api_key = _business_model_context(
        db, current_user, BUSINESS_KEY_GENERATE_PRODUCT_DETAIL_IMAGE
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
            asset = AigcMediaAsset(
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
    #: `group_plans`（**不含 asset 明细**），`AigcMediaAsset` 更没有 group 列。
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
    # `AigcMediaAsset` 无 group 列 ⇒ 反查必落空、只能猜（猜错比没分组更坏）。
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

    **分组（主图 / 详情图）不在返回里** —— `AigcMediaAsset` 没有 `group` 列，
    分组事实只在生成当时的响应里。本版按**时间**列，不按组分块
    （按组看是独立需求，要再加列 + 迁移）。
    """
    from backend.app.models import AigcMediaAsset

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
        AigcMediaAsset.user_id == current_user.id,
        AigcMediaAsset.platform_account_id == payload.platform_account_id,
        AigcMediaAsset.item_id == payload.item_id,
    ]
    total = db.scalar(select(func.count(AigcMediaAsset.id)).where(*conditions)) or 0
    rows = db.scalars(
        select(AigcMediaAsset)
        .where(*conditions)
        .order_by(AigcMediaAsset.created_at.desc(), AigcMediaAsset.id.desc())
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


# =========================================================================== #
# M4 方向 4 · 商品编辑优化（设计 20，2026-10-04）
#
# ⚠️ **先说边界**：设计 16 §5.1 的 `(2b) 批量改价 / 上下架` **判为不可交付** ——
# 千帆 SDK 11 个端点逐个核对，**零个商品写接口**（详见设计 20 §0 的判据表）。
# 本组端点交付「可得的那一半」：分组 / 编辑（纯本地）/ AI 润色 / CSV 导出。
# 所有写操作**只动本地台账**，平台商品零修改。
# =========================================================================== #

#: 导出 CSV 的行数上限（设计 20 §2.2）。
#: ⚠️ **前后端同值**：常量只在后端这一处定义，前端读端点下发的值，不抄第二份
#: （`MAX_BATCH_DELETE` 同一纪律 —— 两份必然漂移，漂移就是「前端放行后端拒」）。
MAX_EXPORT_ROWS: int = 5000

#: 「不传 = 不动」的 sentinel —— **不是 `None`**（设计 20 §2.2 口径①）。
#: Pydantic 拿不到键时填它，API 层据此区分「省略键」与「显式传 null」。
_UNSET = object()


class ItemEditRequest(BaseModel):
    """商品本地编辑（设计 20 §2.1 端点 5）。

    ⚠️ **PATCH 语义的两态**（设计 20 §2.2 口径①）：
    * **省略键** = 保持原值（默认值是 `_UNSET` sentinel，**不是 `None`**）；
    * **显式传 `null`** = 清空。

    用 `Optional[X] = _UNSET` 而不是 `= None`：后者会把两件事混成一件
    （用户只想改卖点，结果标题被清空了 —— PATCH 最常见的静默错值）。

    ⚠️ `group_id` 已随「彻底移除商品分组」退场（2026-10-05）。它**不是**简单删字段
    就完事：`extra="forbid"` 会让旧前端继续传 `group_id` 时拿到 422（而不是静默丢掉），
    这正是我们要的 —— 静默丢弃会让用户以为「改了分组」。
    """

    model_config = ConfigDict(extra="forbid")
    platform_account_id: int = Field(ge=1)
    item_id: str = Field(min_length=1, max_length=64)
    #: 采纳的 AI 标题；`null` = 清空；省略 = 不动。
    ai_title: Optional[str] = Field(default=_UNSET, max_length=512)  # type: ignore[assignment]
    #: 采纳的 AI 卖点；`null` = 清空；省略 = 不动。
    ai_selling_point: Optional[str] = Field(default=_UNSET)  # type: ignore[assignment]


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
    #: ⚠️ 分组筛选（`group_id`）已随分组功能退场（2026-10-05，导出与列表同一条判据）。


class ItemPolishRequest(BaseModel):
    """商品标题 / 卖点 AI 润色（设计 20 §3.1）。

    ⚠️ **结果不落库**：润色只回**备选**（标题 3 个 + 卖点 3 个），用户**采纳**
    才写进 `ai_title` / `ai_selling_point`（走 `PATCH /items/edit`）。
    """

    model_config = ConfigDict(extra="forbid")
    platform_account_id: int = Field(ge=1)
    item_id: str = Field(min_length=1, max_length=64)
    #: 提示词模板 id（可选；`task_type=text` 的商品模板，设计 20 §3.3）。
    template_id: Optional[int] = Field(default=None, ge=1)
    #: 补充要求（没选模板时作为主提示词；选了模板则追加在末尾）。
    instruction: str = Field(default="", max_length=2000)


def _item_error_to_http(error: Exception) -> HTTPException:
    """商品本地编辑类错误 → HTTPException（code → 状态码，唯一映射点）。

    ⚠️ 宿主原为 `product_groups_service.ProductGroupError`；2026-10-05 分组退场后
    改为 `shop_products_service.ProductEditError`（同一语义、同一映射规则）。
    """
    from backend.app.services.shop_products_service import ProductEditError

    if not isinstance(error, ProductEditError):
        raise error
    code = error.code or ""
    if code.endswith("_NOT_FOUND"):
        status_code = status.HTTP_404_NOT_FOUND
    else:
        status_code = status.HTTP_400_BAD_REQUEST
    return HTTPException(status_code=status_code, detail={"code": code, "message": str(error)})


# --------------------------------------------------------------------------- #
# 商品分组已退场（2026-10-05 用户口径「彻底移除商品分组相关逻辑」）
#
# ⚠️ **墓碑不静默删路由**：直接删掉这 4 条路径的话，旧前端 / 旧书签拿到的是
# 「404 不存在」，分不清「从来没有过」与「我们撤了、原因是分组功能整体退场」。
# 所以保留路径 + `include_in_schema=False`（不进 OpenAPI ⇒ 端点数确实减少）
# + 显式 410 + 可读原因（与 `bind_shop_removed` / `unbind_shop_removed` 同款先例）。
# --------------------------------------------------------------------------- #

#: 分组退场的统一响应体（4 条路径共用一份 —— 同一件事不留 4 份文案）。
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


#: ⚠️ 两个「单次上限」**必须同值**（前后端同值纪律：前端放行后端拒 = 用户点了个
#: 注定失败的按钮；后端放行前端拦 = 静默少做）。这条断言在导入时就会响。
assert operate_service.MAX_ITEMS_PER_TASK == MAX_OPERATE_ITEMS, (
    "商品操作单次上限漂移：operate_service.MAX_ITEMS_PER_TASK="
    f"{operate_service.MAX_ITEMS_PER_TASK} vs qianfan.MAX_OPERATE_ITEMS={MAX_OPERATE_ITEMS}"
)


class ItemOperateRequest(BaseModel):
    """批量上架 / 下架 / 删除**平台商品**（2026-10-05 需求 7）。

    ⚠️ 与 `ShopItemsDeleteRequest`（删**本地台账行**）是**两件不同的事**：

    | 请求 | 动谁 |
    |---|---|
    | `POST /items/delete` | **本地台账行**（零平台请求），平台商品不动 |
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


class ItemsSyncRequest(BaseModel):
    """同步**指定商品**到本地台账（2026-10-05 需求 6）。

    ⚠️ 与 `SyncShopGoodsRequest`（整店同步）是两件事：这里只更新**命中的那几个**
    商品，**不删任何旧行**、不改台账范围。
    """

    model_config = ConfigDict(extra="forbid")
    platform_account_id: int = Field(ge=1)
    #: 要同步的商品 ID（去重保序；空 ⇒ 400；超 `MAX_SYNC_ITEMS` ⇒ 400）。
    item_ids: list[str] = Field(min_length=1, max_length=MAX_OPERATE_ITEMS)
    #: 2026-10-06：合并「批量同步商品信息」后，同一个条件弹窗的**时间区间**
    #: 对按 ID 同步这条路也要生效（原来只认 ID，区间无处可传）。
    #: `full` ⇒ 不设下界；`range` ⇒ 透给平台按**创建时间**过滤（与整店同口径）。
    scope: str = ""
    start_date: str = ""
    end_date: str = ""


@router.post("/items/sync")
def shop_items_sync(
    payload: ItemsSyncRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**按指定商品 ID 同步最新数据到本地台账**（2026-10-05 需求 6）。

    ## 平台侧没有「按 item_id 拉商品」的读接口 —— 这是本端点的**代价**

    真机核对（2026-10-05）：`product/search_item_v2` 只支持分页 + 筛选；
    `get_skus_by_item_ids` 只回规格、不回商品本体。⇒ 实现是**把指定 ID 当筛选条件
    发出去、按页扫描，只把精确命中的那几个落库**（`whole_keywords` 是模糊匹配，
    所以本地还要按精确 id 再筛一遍 —— 否则标题里凑巧含该串的商品也会被写进台账）。

    **同步执行**（不是后台任务）：这个动作的目的是「马上看到那几个商品的最新数据」，
    丢给后台就得再设计一套进度查询；代价是请求可能持续较久（要翻页），
    如实写在响应 `note` 与前端弹窗里（不假装是「精准单查」）。

    ⚠️ **只补不删**：命中之外的商品一行不动（与全量同步的「翻完删旧行」刻意不同 ——
    指定同步只对「用户点名的那几个」负责）。
    """
    account = _owned_account(db, current_user, payload.platform_account_id)

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
            detail={"code": "QIANFAN_ITEM_IDS_REQUIRED", "message": "没有要同步的商品。"},
        )
    if len(item_ids) > MAX_OPERATE_ITEMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_ITEM_IDS_TOO_MANY",
                "message": f"单次最多同步 {MAX_OPERATE_ITEMS} 个商品，本次填了 {len(item_ids)} 个。",
                "limit": MAX_OPERATE_ITEMS,
            },
        )

    from backend.app.services.shop_binding_service import has_ark_session

    if not has_ark_session(db, account.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "ARK_SESSION_REQUIRED",
                "message": "该账号没有可用的千帆（ark）登录态，无法同步商品。"
                           "先在账号矩阵导入其千帆登录态后再试。",
            },
        )

    #: ⚠️ 2026-10-11（用户口径「新增一个同步商品信息队列」）：同步指定商品改**入队**，
    #: 队列 worker 认领后异步执行，队列记录页可见执行记录详情。
    from backend.app.services import note_queue_service as nq

    result = shop_goods_sync_service.enqueue_goods_sync(
        db,
        user_id=current_user.id,
        account_id=account.id,
        item_ids=item_ids,
        source=nq.SOURCE_GOODS_SYNC_SPECIFIED,
        scope=payload.scope,
        start_date=payload.start_date,
        end_date=payload.end_date,
    )
    return {"platform_account_id": payload.platform_account_id, **result}


@router.post("/items/operate")
def shop_items_operate(
    payload: ItemOperateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**批量上架 / 下架 / 删除平台商品**（2026-10-05 需求 7；**真实写操作**）。

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

    # 去重保序（与 `/items/delete` 同一纪律：请求条数≠实际条数时不许虚报）。
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


@router.patch("/items/edit")
def edit_shop_item(
    payload: ItemEditRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**商品本地编辑**（设计 20 §2 端点 5；**纯本地写**，零平台请求）。

    可编辑两项（⚠️ 分组项已随分组功能退场，2026-10-05）：`ai_title`（采纳的 AI 标题）/
    `ai_selling_point`（采纳的 AI 卖点）。

    ## 什么**不**能在这里改（边界，写清避免误解）

    ⚠️ **商品真实标题 / 价格 / 库存 / 上下架状态都不改** —— 那些是平台数据。
    本端点改的是**本地台账上的用户标注**（`ai_title` / `ai_selling_point` 是用户
    **采纳**的 AI 草稿，平台标题仍在 `item_title`）。
    ⚠️ 平台商品本身的上下架 / 删除走 `POST /items/operate`（**写平台**，另一条链路）。

    ## PATCH 语义的两态（设计 20 §2.2 口径①）

    | 请求体 | 行为 |
    |---|---|
    | 省略键 | 保持原值 |
    | 显式 `null` | 清空 |

    `updated[]` 只回**真正变了**的字段（新值 != 旧值）—— 前端据此提示
    「已更新 AI 标题」而不是「已更新商品」（全字段都报「已更新」等于没说）。
    """
    from backend.app.services import shop_products_service

    account = _owned_account(db, current_user, payload.platform_account_id)

    touched: dict[str, bool] = {
        "ai_title": payload.ai_title is not _UNSET,
        "ai_selling_point": payload.ai_selling_point is not _UNSET,
    }
    try:
        return shop_products_service.edit_product(
            db,
            platform_account_id=account.id,
            item_id=payload.item_id,
            ai_title=payload.ai_title if touched["ai_title"] else None,
            ai_selling_point=payload.ai_selling_point if touched["ai_selling_point"] else None,
            touched=touched,
        )
    except Exception as exc:
        raise _item_error_to_http(exc) from exc


@router.post("/items/polish")
def polish_shop_item(
    payload: ItemPolishRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    text_ai_client: OpenAICompatibleTextClient = Depends(get_text_ai_client),
):
    """**商品标题 / 卖点 AI 润色**（设计 20 §3.1）。

    与「生成商品详情图」是**同一条纪律**（§13.3.3）：服务商 `api_key` 在服务端、
    绝不下发前端。本端点用**两个业务点**（`polish_product_title` /
    `generate_selling_point`，都是 `kind=text`），与笔记侧的 `polish_text` /
    `generate_title` **分开挂服务商** —— 商品标题有平台字数上限、不能含广告法
    违禁词，提示词与笔记不是同一套（设计 20 §3.2）。

    失败一律**结构化、不回落**（同 `_business_model_context`）。

    ## 返回

    ```json
    {
      "title_candidates": ["...", "...", "..."],
      "selling_point_candidates": ["...", "...", "..."],
      "source_title": "平台原标题（对照用）",
      "model_name": "本次用的模型"
    }
    ```

    ⚠️ **一次给 3 个备选**（市场惯例：只给一个等于没给，用户没有可比较的对象）。
    ⚠️ **不落库**：`ai_title` / `ai_selling_point` 只在用户**采纳**后写
    （`PATCH /items/edit`）。润色过程在 `llm_gateway_usage_logs` 有调用记录。
    """
    from backend.app.api.ai import _business_model_context
    from backend.app.services import product_polish_service

    _owned_account(db, current_user, payload.platform_account_id)

    return product_polish_service.polish_product(
        db,
        current_user=current_user,
        platform_account_id=payload.platform_account_id,
        item_id=payload.item_id,
        template_id=payload.template_id,
        instruction=payload.instruction,
        text_ai_client=text_ai_client,
        resolve_context=lambda key: _business_model_context(db, current_user, key),
    )


@router.post("/items/export")
def export_shop_items(
    payload: ItemsExportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**导出商品 CSV**（设计 20 §2 端点 6；**纯本地**，零平台请求）。

    ## 三个必须钉住的口径（设计 20 §2.2）

    1. **筛选 = 列表筛选**：复用 `shop_products_service.export_rows`（内部走的是
       列表同一个 `_apply_filters`），**不重新实现一份** —— 两份筛选实现 =
       「列表看到的」和「导出的」不是同一个集合（导出最常见的故障）。
    2. **排序 = 列表排序**（`_apply_order`）：CSV 与屏幕顺序一致，用户对账时
       眼睛在两处来回看，顺序不一致会误判「少了几行」。
    3. **字段 = 列表字段**：导出列与 `normalize_item_from_raw` **同一个构造函数**。
       `ai_title` 有值导它、没有导 `item_title`（用户要的是「能用的标题」）。

    ## 上限

    `> MAX_EXPORT_ROWS`（5000）⇒ 400 `QIANFAN_EXPORT_TOO_MANY`，**不静默截断**
    （截断的 CSV 是一个「看起来完整」的假全量，用户会拿它去对账）。
    """
    import csv
    import io

    from fastapi.responses import StreamingResponse

    account = _owned_account(db, current_user, payload.platform_account_id)

    try:
        time_from = _epoch_millis(payload.create_time_from, end_of_day=False)
        time_to = _epoch_millis(payload.create_time_to, end_of_day=True)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "QIANFAN_TIME_RANGE_INVALID", "message": str(error)},
        ) from error

    try:
        rows = shop_products_service.export_rows(
            db,
            payload,
            platform_account_id=account.id,
            from_ms=time_from,
            to_ms=time_to,
            price_from_fen=_price_fen(payload.price_from),
            price_to_fen=_price_fen(payload.price_to),
            max_rows=MAX_EXPORT_ROWS,
        )
    except shop_products_service.ExportTooManyError as error:
        # ⚠️ 不静默截断：截断的 CSV 是「看起来完整」的假全量，用户会拿它对账。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "QIANFAN_EXPORT_TOO_MANY",
                "message": str(error),
                "limit": error.limit,
                "actual": error.actual,
            },
        ) from error

    buffer = io.StringIO()
    # ⚠️ BOM（UTF-8 BOM）：Excel 中文不乱码（国产办公场景的硬需求）。
    buffer.write("﻿")
    writer = csv.writer(buffer)
    writer.writerow(shop_products_service.CSV_HEADERS)
    for row in rows:
        writer.writerow(row)

    # ⚠️ **文件名不得直接进响应头**（DEF-091）：Starlette 按 **latin-1** 编码响应头，
    # 中文 filename ⇒ `UnicodeEncodeError` ⇒ 导出**必 500**（任何一次导出都炸，
    # 不是边界情况）。修法 = RFC 5987 的 `filename*=UTF-8''<pct-encoded>`
    # 并**同时给一个纯 ASCII 的 `filename=` 回退**（老客户端认它）。
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ascii_name = f"shop-items-{stamp}.csv"
    utf8_name = quote(f"商品台账-{stamp}.csv", safe="")
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}'
            ),
            "X-Export-Rows": str(len(rows)),
        },
    )


# --------------------------------------------------------------------------- #
# 订单模块（设计 16 §7，M6 方向 6 · 一期只读）
#
# 与商品列表同构：列表 / 详情 / 指标全部读**本地台账**（`orders` 等 5 张表，
# 零平台请求）；同步是唯一打千帆的动作，且必须先有 ark 登录态。
# --------------------------------------------------------------------------- #


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


# ------------------------------------------------------------------ #
# 商品**新增（存草稿箱）**（设计 23，2026-10-07 真机打通）
#
# ⚠️ **红线：新增的商品只能存草稿，不允许发布到线上**（用户 2026-10-07 口径）。
# 落实为三层（任何一层单独都不够）：
#   ① 执行链里**没有「提交商品」这一步**（本模块只调 `create_product_draft`）；
#   ② **请求契约里没有那个开关**（下面三个模型的所有字段都不含
#      `publish` / `available` / `on_shelf`），`extra="forbid"` ⇒ 传了直接 422；
#   ③ 回执文案固定写明「已保存到草稿箱，未上架」。
# 守卫 `test_product_draft.py` 钉住 ① 与 ②。
# ------------------------------------------------------------------ #

#: 单次新增的图片 / SKU 上限 + 平台草稿限流（**前后端同值**：只在服务层定义一份，
#: 前端读端点下发的 `limits`，不抄第二份 —— 抄两份必然漂移成「前端放行后端拒」）。
_DRAFT_MAX_IMAGES = draft_service.MAX_DRAFT_IMAGES
_DRAFT_MAX_SKUS = draft_service.MAX_DRAFT_SKUS
_DRAFT_LIMIT_PER_MINUTE = draft_service.DRAFT_LIMIT_PER_MINUTE


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
    skus: list[ItemDraftSku] = Field(min_length=1, max_length=_DRAFT_MAX_SKUS)
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
    ledger_variant_ids: dict[str, str] = {}
    try:
        from backend.app.services.shop_products_service import (
            recent_item_ids_for_account,
        )

        item_ids = recent_item_ids_for_account(db, account.id, limit=3)
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
    skus: list[ItemDraftSku] = Field(min_length=1, max_length=_DRAFT_MAX_SKUS)
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

    #: ⚠️ **并行上传**（用户实测「顶级确认发布很慢」）：每张图要 1 次 permit + 1 次 PUT，
    #: 26 张串行 ⇒ 几十秒起步且**线性叠加**。用有上限的线程池并发（8 路）压掉这段；
    #: 单张仍是「失败逐条如实报」，**不静默丢图**（口径不变，只改并发度）。
    from concurrent.futures import ThreadPoolExecutor

    def _upload_one(path_str: str) -> tuple[str, Any, Any]:
        ok_img, reason, entry = draft_svc.upload_one_image(
            adapter, seller_id=seller_id, file_path=_abs(path_str)
        )
        return path_str, (ok_img, reason, entry), _abs(path_str)

    if _paths_to_upload:
        with ThreadPoolExecutor(max_workers=min(8, len(_paths_to_upload))) as pool:
            for path_str, (ok_img, reason, entry), abs_path in pool.map(_upload_one, _paths_to_upload):
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

    #: ⚠️ 2026-10-11（用户口径「上架成功的商品同步到本地库」+「走同步商品信息队列」）：
    #: 发布成功拿到 item_id 后，**入队**（同步商品信息队列），worker 异步拉进台账。
    #: 尽力而为 —— 入队失败不阻断发布结果（用户可再手动「同步商品」兜底）。
    if result.get("published") and result.get("item_id"):
        try:
            from backend.app.services import note_queue_service as nq

            shop_goods_sync_service.enqueue_goods_sync(
                db,
                user_id=current_user.id,
                account_id=account.id,
                item_ids=[str(result["item_id"])],
                source=nq.SOURCE_GOODS_SYNC_PUBLISHED,
            )
        except Exception as exc:  # noqa: BLE001 - 入队失败不阻断发布结果
            logger.warning("发布后入队同步商品失败（item_id=%s）：%s", result.get("item_id"), exc)

    return {
        "platform_account_id": account.id,
        "uploaded_images": len(uploaded),
        "failed_images": failed_images,
        **result,
    }


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


def payload_metrics_account_id(account: PlatformAccount) -> int:
    return account.id


# --------------------------------------------------------------------------- #
# 订单二期写操作（设计 16 §7.4，M7 方向 6）—— **真实平台写、不可撤回**
#
# ⚠️ 端点存在性已证实（§7.4.1 只读探测，2026-10-04）；**请求体 schema 待首发核对**。
# ⚠️ 与发布（`note/publish`）同性质：仅限本人店铺 + 用户**显式点击**触发，
#    **两层确认**缺一不可，且**不进定时任务 / 不进工作流**（绝不自动触发）。
# --------------------------------------------------------------------------- #


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


def _order_write_error_to_http(error: Any) -> HTTPException:
    """`OrderWriteError` → HTTP（结构化）。门禁类 400，找不到类 404。"""
    from backend.app.services.order_write_service import OrderWriteError

    if not isinstance(error, OrderWriteError):  # pragma: no cover
        return HTTPException(status_code=500, detail={"code": "INTERNAL", "message": str(error)})
    not_found = {"ORDER_NOT_FOUND", "ORDER_AFTERSALE_NOT_FOUND"}
    code = status.HTTP_404_NOT_FOUND if error.code in not_found else status.HTTP_400_BAD_REQUEST
    return HTTPException(status_code=code, detail={"code": error.code, "message": str(error), **error.extra})


def _owned_order(db: Session, current_user: User, account_id: int, order_no: str) -> Order:
    """取本账号的一笔订单（越权 / 不存在 ⇒ 404，不区分二者）。"""
    from backend.app.services.order_write_service import OrderWriteError

    order = db.scalars(
        select(Order).where(
            Order.user_id == current_user.id,
            Order.platform_account_id == account_id,
            Order.order_no == str(order_no or "").strip(),
        )
    ).first()
    if order is None:
        raise OrderWriteError(f"订单不存在或不属于当前账号：{order_no}", code="ORDER_NOT_FOUND")
    return order


def _owned_after_sale(db: Session, current_user: User, account_id: int, after_sale_id: str) -> Any:
    """取本账号的一条售后单（越权 / 不存在 ⇒ 404）。"""
    from backend.app.services.order_write_service import OrderWriteError

    row = db.scalars(
        select(AfterSale)
        .join(Order, Order.id == AfterSale.order_id)
        .where(
            AfterSale.user_id == current_user.id,
            AfterSale.after_sale_id == str(after_sale_id or "").strip(),
            Order.platform_account_id == account_id,
        )
    ).first()
    if row is None:
        raise OrderWriteError(
            f"售后单不存在或不属于当前账号：{after_sale_id}", code="ORDER_AFTERSALE_NOT_FOUND"
        )
    return row


def _ark_adapter_for(db: Session, account: PlatformAccount, factory: Any) -> Any:
    """按账号取 ark 适配器（**不借会话**：拿不到令牌 ⇒ 结构化 400）。

    ⚠️ 工厂返回的是**适配器类**，构造只吃 `cookies` 一个参数（与
    `shop_health_service` 的 `factory(cookies)` 同口径）。传 `account` 会在
    构造处 `TypeError` ⇒ 所有走本函数的端点（发货 / 售后 / 图片空间…）整体 500。
    """
    from backend.app.services.shop_binding_service import qianfan_cookies_for_account

    cookies = qianfan_cookies_for_account(db, account.id)
    if not cookies:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "ARK_SESSION_REQUIRED",
                "message": "该账号没有可用的千帆（ark）登录态，无法执行该操作。"
                "先在账号矩阵导入其千帆登录态。",
            },
        )
    return factory(cookies)


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


# --------------------------------------------------------------------------- #
# 图片空间（设计 22 §1）—— 千帆 material_space 系列接口的**只读代理**。
#
# ⚠️ 三条硬约定（写在这里免得后来者改坏）：
#   ① **零落库**：本组端点不写任何表、不落任何文件，平台数据现取现渲染
#      （用户 2026-10-06 口径「所有数据不落库本地」）；
#   ② **只读**：只有列表 / 树 / 配额三个查询语义，**没有**上传 / 移动 / 删除 / 下载
#      （那些是平台写接口，不进本模块）；
#   ③ **列表端点需要签名**（真机实测：无签名 `code=-1`），树 / 配额不需要 ——
#      签名只是机制，本组端点**都没有平台副作用**。
# --------------------------------------------------------------------------- #

#: ⚠️ 排序字段白名单**唯一来源**是 service 层（2026-10-07 修：本文件曾**另存一份**且漏了
#: `updateTime` ⇒ 回收站视图按删除时间排序时被自己的白名单拦成 400「加载失败」——
#: 两份清单必然漂移，禁止再复制）。下面在模块顶部 import，这里只留说明。


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


# --------------------------------------------------------------------------- #
# 图片空间**写链路**（设计 22 §1.5，2026-10-06 用户口径「操作按钮功能全部要做」）
#
# ⚠️ 只读边界（硬约束②）澄清：管的是「探测 / 调研 / 取证」一律只读；本组是
# **用户在本系统内对自己图片空间的显式操作**（与商品写链路同一性质）⇒
# 「用户显式点击 + 本店归属 + 两层确认」。全部需要 ark 签名（平台强制）。
# --------------------------------------------------------------------------- #


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


def _material_confirm_token(account_id: int, action: str, item_ids: list) -> str:
    """删除的二次确认指纹（`account + action + 排序后的 id 集`）。

    与订单写链路的 `confirm_token` 同一思路：把「这次要对什么动手」编码进 token，
    前端预览时拿到、确认时原样回传 —— 中间被改（多删 / 换账号）就不匹配。
    """
    import hashlib

    payload = json.dumps(
        {"account": int(account_id), "action": action, "ids": sorted(str(i) for i in item_ids)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


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


#: ⚠️ **平台单次 `upload/web/permit` 的 fileId 上限**（2026-10-11 真机实测）：
#: `file_count=10 → 10` / `20 → 14` / `38 → 14` / `50 → 14`。超过就只给 14 个。
#: ⇒ 批量上传必须**分批申请**（每批 ≤ 本值）才能拿齐 N 个 fileId。
_PERMIT_BATCH = 14

#: 每批凭证的 `uploadAddr`/`token` —— 一个 fileId 只能用**它那批**的凭证上传
#: （不同批是不同凭证，跨批用会 401）。键是 fileId。
_PERMIT_CRED: dict[str, tuple[str, str]] = {}


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
