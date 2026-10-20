"""千帆 · 店铺违规管理（设计 42，2026-10-18）

一比一复刻千帆后台 `app-violation/violation-manage/*` 的**只读代理**。

## 边界（本模块只做读）

| 做 | 不做 |
|---|---|
| 违规 / 预警列表、详情、申诉记录、证据 | ✗ `ark/violation/read_mark`（标已读 —— 打开详情就改平台状态不是只读） |
| 三枚举（处罚状态 / 违规对象 / 违规原因 tag） | ✗ 申诉提交（`appeal/*`） |
| 店铺体检（违规风险 / 经营建议 / 负反馈风险 / 高价商品） | ✗ 任何批量删除 / 批量操作 |
| 赔付列表、仲裁列表与原因、抽检违规、免罚机会 | ✗ 任何落库（本模块不碰 Session 写操作） |

## 零落库

`db` 只用于**鉴权**（`_owned_account`：这个账号是不是当前用户的）与取 ark 令牌
（`_ark_adapter_for`）。平台数据现取现返回，不写任何表。

## 错误映射（可分辨，不合并）

| 情形 | HTTP | code |
|---|---|---|
| 账号不属于当前用户 | 404 | `ACCOUNT_NOT_FOUND`（由 `_owned_account` 给） |
| 该账号没有 ark 令牌 | 400 | `ARK_SESSION_REQUIRED`（由 `_ark_adapter_for` 给） |
| 平台返回失败 / 签名被拒 | 502 | `VIOLATION_UPSTREAM_FAILED`（message 透传平台原文） |
| 请求体字段不允许 | 422 | FastAPI 默认（所有模型 `extra="forbid"`） |
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from ._shared import (
    APIRouter,
    Any,
    BaseModel,
    ConfigDict,
    Depends,
    Field,
    HTTPException,
    Session,
    User,
    _ark_adapter_for,
    _owned_account,
    get_current_user,
    get_db,
    get_qianfan_api_adapter_factory,
    status,
)

from backend.app.services import violation_service as vs

router = APIRouter()

#: 平台失败统一码（前端据此区分「上游挂了」与「你没权限」）。
UPSTREAM_FAILED = "VIOLATION_UPSTREAM_FAILED"


def _fail(message: str) -> HTTPException:
    """上游失败 ⇒ 502 结构化（**透传平台原文**，不吞成「未知错误」）。"""
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"code": UPSTREAM_FAILED, "message": message or "千帆违规接口返回失败"},
    )


# --------------------------------------------------------------------- #
# 请求模型（全部 extra="forbid" —— 未知字段静默丢弃会让「筛选无效」无从追查）
# --------------------------------------------------------------------- #
class ViolationListRequest(BaseModel):
    """违规 / 预警列表（同一端点，`scene` 决定 `warnFlag`）。"""

    platform_account_id: int
    #: `violation` 违规列表 / `warning` 预警列表。
    scene: Literal["violation", "warning"] = "violation"
    page_no: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=100)
    keyword: str = Field(default="", max_length=128)
    entity_types: Optional[list[int]] = None
    reason_name: str = Field(default="", max_length=128)
    statuses: Optional[list[int]] = None
    severe_only: bool = False
    allow_appeal: Optional[bool] = None
    correctable: Optional[bool] = None
    unread_only: bool = False
    exam_only: bool = False
    pending_only: bool = False
    recent_days: bool = False
    date_start: str = Field(default="", max_length=32)
    date_end: str = Field(default="", max_length=32)
    #: 快捷筛选项（`all/pending/unread/...`）；给了就以它为准叠加在上面的条件之上。
    quick: str = Field(default="", max_length=32)

    model_config = ConfigDict(extra="forbid")


class ViolationCountsRequest(BaseModel):
    """角标计数：只查一次 `counts`（未读 N / 待处理 N）。"""

    platform_account_id: int
    scene: Literal["violation", "warning"] = "violation"
    quick: Literal[
        "all",
        "pending",
        "unread",
        "severe",
        "recent7",
        "correctable",
        "examinable",
        "appealable",
        "high_risk",
    ] = "all"

    model_config = ConfigDict(extra="forbid")


class ViolationDetailRequest(BaseModel):
    platform_account_id: int
    violation_id: str = Field(min_length=1, max_length=64)
    tenant: str = Field(default="", max_length=32)
    tag_id: str = Field(default="", max_length=64)

    model_config = ConfigDict(extra="forbid")


class ViolationEvidenceRequest(BaseModel):
    platform_account_id: int
    violation_id: str = Field(min_length=1, max_length=64)
    entity_id: str = Field(default="", max_length=64)
    entity_type: Optional[int] = None
    tag_id: str = Field(default="", max_length=64)
    tenant: str = Field(default="", max_length=32)

    model_config = ConfigDict(extra="forbid")


class ViolationMetaRequest(BaseModel):
    platform_account_id: int
    #: 抽检页的 tag（`bizScene=SPOT_CHECK`）—— 与违规列表那套分开取。
    include_spot_check: bool = True

    model_config = ConfigDict(extra="forbid")


class CheckupRisksRequest(BaseModel):
    platform_account_id: int
    page_no: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=200)

    model_config = ConfigDict(extra="forbid")


class CheckupAdviceRequest(BaseModel):
    platform_account_id: int

    model_config = ConfigDict(extra="forbid")


class NegativeFeedbackRequest(BaseModel):
    platform_account_id: int
    kind: Literal["quality", "logistics"]
    page_no: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=100)
    #: 列表视图（`rectify=1`）与汇总视图（`controlFlow=1`）是两个请求，前端各取一次。
    view: Literal["list", "summary"] = "list"
    time_window_days: int = Field(default=30, ge=1, le=365)

    model_config = ConfigDict(extra="forbid")


class PagedSellerRequest(BaseModel):
    """赔付 / 仲裁列表（都要 `sellerId`，由后端从账号上取，前端不传）。"""

    platform_account_id: int
    page_no: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=100)
    keyword: str = Field(default="", max_length=128)

    model_config = ConfigDict(extra="forbid")


class ArbitrationTagsRequest(BaseModel):
    platform_account_id: int

    model_config = ConfigDict(extra="forbid")


class SpotCheckRequest(BaseModel):
    platform_account_id: int
    page_no: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=100)
    keyword: str = Field(default="", max_length=128)
    reason_name: str = Field(default="", max_length=128)
    allow_appeal: Optional[bool] = None
    #: 抽检的处罚状态是**另一套**：待落罚 / 已落罚 / 已取消（真机下拉）。
    punish_status: str = Field(default="", max_length=32)

    model_config = ConfigDict(extra="forbid")


class ExemptionOverviewRequest(BaseModel):
    platform_account_id: int

    model_config = ConfigDict(extra="forbid")


class ExemptionItemsRequest(BaseModel):
    platform_account_id: int
    page_no: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=100)
    date_start: str = Field(default="", max_length=32)
    date_end: str = Field(default="", max_length=32)

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------- #
# 内部：取上下文
# --------------------------------------------------------------------- #
def _context(db: Session, current_user: User, account_id: int, factory: Any):
    """`(account, adapter)` —— 归属校验 + ark 门禁（两处都已结构化报错）。"""
    account = _owned_account(db, current_user, account_id)
    adapter = _ark_adapter_for(db, account, factory)
    return account, adapter


def _seller_id(account: Any) -> str:
    value = getattr(account, "seller_id", None)
    return str(value) if value else ""


# --------------------------------------------------------------------- #
# 违规 / 预警列表
# --------------------------------------------------------------------- #
@router.post("/violation/list")
def violation_list(
    payload: ViolationListRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """违规列表 / 预警列表（`warnFlag` 由 `scene` 派生）—— 只读、零落库。

    `quick` 非空时以快捷筛选为准（与千帆后台一致：点快捷会重置其余条件）。
    """
    account, adapter = _context(db, current_user, payload.platform_account_id, adapter_factory)
    scene = vs.SCENE_WARNING if payload.scene == "warning" else vs.SCENE_VIOLATION

    if payload.quick:
        body = vs.quick_filter_body(payload.quick, scene=scene)
        body["pageNo"] = payload.page_no
        body["pageSize"] = payload.page_size
    else:
        reason_tags = []
        if payload.reason_name:
            ok, msg, raw = adapter.post_violation_user_tag({"violationEntityType": vs.ENTITY_ALL})
            if not ok:
                raise _fail(msg)
            reason_tags = ((raw or {}).get("data") or {}).get("tags") or []
        body = vs.build_list_body(
            scene=scene,
            page_no=payload.page_no,
            page_size=payload.page_size,
            keyword=payload.keyword,
            entity_types=payload.entity_types,
            reason_name=payload.reason_name,
            reason_tags=reason_tags,
            statuses=payload.statuses,
            severe_only=payload.severe_only,
            allow_appeal=payload.allow_appeal,
            correctable=payload.correctable,
            unread_only=payload.unread_only,
            exam_only=payload.exam_only,
            pending_only=payload.pending_only,
            recent_days=payload.recent_days,
            date_start=payload.date_start,
            date_end=payload.date_end,
        )

    ok, msg, raw = adapter.post_violation_list(body)
    if not ok:
        raise _fail(msg)
    result = vs.normalize_list_payload(raw)
    result["scene"] = payload.scene
    result["query"] = body
    result["account"] = {"id": getattr(account, "id", None), "nickname": getattr(account, "nickname", None)}
    return result


@router.post("/violation/counts")
def violation_counts(
    payload: ViolationCountsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """角标计数（未读 N / 待处理 N）—— 千帆后台**单独发一次**列表请求取 `counts`。

    与列表共用同一份请求体组装，避免「角标读的是过滤后的数、列表读的是全量」这类漂移。
    """
    _account, adapter = _context(db, current_user, payload.platform_account_id, adapter_factory)
    scene = vs.SCENE_WARNING if payload.scene == "warning" else vs.SCENE_VIOLATION
    body = vs.quick_filter_body(payload.quick, scene=scene)
    body["pageNo"] = 1
    body["pageSize"] = 10
    ok, msg, raw = adapter.post_violation_list(body)
    if not ok:
        raise _fail(msg)
    data = (raw or {}).get("data") or {}
    counts = data.get("counts")
    return {"quick": payload.quick, "count": int(counts) if isinstance(counts, (int, float)) else 0}


# --------------------------------------------------------------------- #
# 详情 / 申诉记录 / 证据
# --------------------------------------------------------------------- #
@router.post("/violation/detail")
def violation_detail(
    payload: ViolationDetailRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """违规详情（只读）。

    ⚠️ **不调用 `read_mark`**：千帆后台打开详情会自动标已读，本系统不代劳
    （那是对平台状态的写操作）。前端可做「本地标记」，但不同步平台、不落库。
    """
    _account, adapter = _context(db, current_user, payload.platform_account_id, adapter_factory)
    body = {
        "violationKey": payload.violation_id,
        "tenant": payload.tenant,
        "tagId": payload.tag_id,
        "source": "violation_detail_ark",
        "dataFlag": True,
    }
    ok, msg, raw = adapter.post_violation_info(body)
    if not ok:
        raise _fail(msg)
    detail = vs.normalize_detail(raw)
    detail["violation_id"] = detail.get("violation_id") or payload.violation_id
    return detail


@router.post("/violation/appeal-record")
def violation_appeal_record(
    payload: ViolationDetailRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """申诉记录（只读）。平台没给就返回空数组（不造记录）。"""
    _account, adapter = _context(db, current_user, payload.platform_account_id, adapter_factory)
    ok, msg, raw = adapter.get_violation_appeal_record(payload.violation_id)
    if not ok:
        raise _fail(msg)
    infos = ((raw or {}).get("data") or {}).get("appealInfos") or []
    return {"items": [r for r in infos if isinstance(r, dict)]}


@router.post("/violation/evidence")
def violation_evidence(
    payload: ViolationEvidenceRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """违规证据（只读）。

    平台对部分违规不给证据（真机账号类 `data:null`）—— 如实返回 `null`，
    前端渲染「平台未提供证据」，**不造占位内容**。
    """
    _account, adapter = _context(db, current_user, payload.platform_account_id, adapter_factory)
    body = {
        "violationKey": payload.violation_id,
        "violationEntityId": payload.entity_id,
        "violationEntityType": payload.entity_type,
        "tagId": payload.tag_id,
        "tenant": payload.tenant,
    }
    ok, msg, raw = adapter.post_violation_evidence(body)
    if not ok:
        raise _fail(msg)
    return {"evidence": (raw or {}).get("data")}


# --------------------------------------------------------------------- #
# 枚举（前端不硬编码）
# --------------------------------------------------------------------- #
@router.post("/violation/meta")
def violation_meta(
    payload: ViolationMetaRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """三类下拉枚举一次取齐（处罚状态 / 违规对象 / 违规原因 tag）。

    ⚠️ 原因 tag 的 `tagName` **原样下发**（可能带尾空格）—— 前端按字符串精确匹配。
    """
    _account, adapter = _context(db, current_user, payload.platform_account_id, adapter_factory)
    ok, msg, statuses = adapter.get_violation_status_enum()
    if not ok:
        raise _fail(msg)
    ok, msg, entities = adapter.get_violation_entity_enum()
    if not ok:
        raise _fail(msg)
    ok, msg, tags = adapter.post_violation_user_tag({"violationEntityType": vs.ENTITY_ALL})
    if not ok:
        raise _fail(msg)

    spot_tags = None
    if payload.include_spot_check:
        ok2, _msg2, spot = adapter.post_violation_user_tag(
            {"violationEntityType": vs.ENTITY_ALL, "bizScene": "SPOT_CHECK"}
        )
        if ok2:
            spot_tags = ((spot or {}).get("data") or {}).get("tags") or []

    return vs.normalize_meta(
        statuses=((statuses or {}).get("data") or {}).get("violationStatus") or [],
        entities=((entities or {}).get("data") or {}).get("violationEntity") or [],
        reason_tags=((tags or {}).get("data") or {}).get("tags") or [],
        spot_check_tags=spot_tags,
    )


# --------------------------------------------------------------------- #
# 店铺体检
# --------------------------------------------------------------------- #
@router.post("/checkup/risks")
def checkup_risks(
    payload: CheckupRisksRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """店铺体检 · 违规风险（只读）。"""
    _account, adapter = _context(db, current_user, payload.platform_account_id, adapter_factory)
    body = {
        "dataFlag": True,
        "warnFlag": 0,
        "pageNo": payload.page_no,
        "pageSize": payload.page_size,
    }
    ok, msg, raw = adapter.post_violation_risk_list(body)
    if not ok:
        raise _fail(msg)
    return vs.normalize_list_payload(raw)


@router.post("/checkup/advice")
def checkup_advice(
    payload: CheckupAdviceRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """店铺体检 · 经营建议（只读；另附高价商品概览）。

    高价商品是独立接口；它失败**不阻断**经营建议（缺就 `None`，前端不渲染那张卡）。
    """
    _account, adapter = _context(db, current_user, payload.platform_account_id, adapter_factory)
    ok, msg, raw = adapter.post_violation_risk_advice({})
    if not ok:
        raise _fail(msg)
    stats = ((raw or {}).get("data") or {}).get("stats") or []

    high_price = None
    ok2, _msg2, hp = adapter.post_seller_high_price_info({})
    if ok2:
        high_price = ((hp or {}).get("data") or {}).get("sellerHighPriceInfo")

    return {"items": [r for r in stats if isinstance(r, dict)], "high_price": high_price}


@router.post("/checkup/negative-feedback")
def checkup_negative_feedback(
    payload: NegativeFeedbackRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """店铺体检 · 负反馈风险（`quality` 商品品质 / `logistics` 物流）。"""
    _account, adapter = _context(db, current_user, payload.platform_account_id, adapter_factory)
    is_list = payload.view == "list"
    body = vs.build_negative_feedback_body(
        kind=payload.kind,
        page_no=payload.page_no,
        page_size=payload.page_size,
        time_window_days=payload.time_window_days,
        control_flow=0 if is_list else 1,
        rectify=1 if is_list else 0,
    )
    ok, msg, raw = adapter.post_negative_feedback_badcase(payload.kind, body)
    if not ok:
        raise _fail(msg)
    return {
        "kind": payload.kind,
        "view": payload.view,
        "total": vs.list_total(raw),
        "items": vs.normalize_plain_list(raw, "badCaseList"),
    }


# --------------------------------------------------------------------- #
# 赔付 / 仲裁 / 抽检 / 免罚
# --------------------------------------------------------------------- #
@router.post("/compensations")
def compensations(
    payload: PagedSellerRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """赔付列表（只读，`sellerId` 由后端从账号取）。"""
    account, adapter = _context(db, current_user, payload.platform_account_id, adapter_factory)
    body = {
        "sellerId": _seller_id(account),
        "pageNo": payload.page_no,
        "pageSize": payload.page_size,
    }
    ok, msg, raw = adapter.post_compensate_list(body)
    if not ok:
        raise _fail(msg)
    return {
        "total": vs.list_total(raw),
        "items": vs.normalize_plain_list(raw, "compensateList"),
    }


@router.post("/arbitrations")
def arbitrations(
    payload: PagedSellerRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """仲裁列表（只读）。"""
    account, adapter = _context(db, current_user, payload.platform_account_id, adapter_factory)
    body = {
        "sellerId": _seller_id(account),
        "pageNo": payload.page_no,
        "pageSize": payload.page_size,
    }
    ok, msg, raw = adapter.post_arbitration_list(body)
    if not ok:
        raise _fail(msg)
    return {
        "total": vs.list_total(raw),
        "items": vs.normalize_plain_list(raw, "records"),
    }


@router.post("/arbitration-tags")
def arbitration_tags(
    payload: ArbitrationTagsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """仲裁原因下拉（只读）。"""
    _account, adapter = _context(db, current_user, payload.platform_account_id, adapter_factory)
    ok, msg, raw = adapter.post_arbitration_taglist({})
    if not ok:
        raise _fail(msg)
    data = (raw or {}).get("data")
    rows = data if isinstance(data, list) else vs.normalize_plain_list(raw, "tags", "tagList", "records")
    return {"items": rows or []}


@router.post("/spot-checks")
def spot_checks(
    payload: SpotCheckRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """抽检违规列表（只读，`bizScene=SPOT_CHECK`）。

    ⚠️ 抽检的**处罚状态是另一套枚举**（待落罚 / 已落罚 / 已取消），与违规列表的
    `待处罚/处罚中/已处罚/已取消` 不是同一个字段语义。平台该端点只按
    `pageNo/pageSize/bizScene` 过滤，其余条件由前端在当页筛选（如实标注）。
    """
    _account, adapter = _context(db, current_user, payload.platform_account_id, adapter_factory)
    body = {
        "pageNo": payload.page_no,
        "pageSize": payload.page_size,
        "bizScene": "SPOT_CHECK",
    }
    ok, msg, raw = adapter.post_spot_check_list(body)
    if not ok:
        raise _fail(msg)
    result = vs.normalize_list_payload(raw)
    result["query"] = body
    return result


@router.post("/exemptions/overview")
def exemption_overview(
    payload: ExemptionOverviewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """免罚机会 · 现货爆单豁免概览（只读）。"""
    _account, adapter = _context(db, current_user, payload.platform_account_id, adapter_factory)
    ok, msg, raw = adapter.post_exemption_overview({})
    if not ok:
        raise _fail(msg)
    return {"overview": (raw or {}).get("data")}


@router.post("/exemptions/items")
def exemption_items(
    payload: ExemptionItemsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter_factory: Any = Depends(get_qianfan_api_adapter_factory),
):
    """免罚机会 · 爆单明细（只读；日期是 `YYYY-MM-DD`，不是毫秒）。"""
    _account, adapter = _context(db, current_user, payload.platform_account_id, adapter_factory)
    body = vs.build_exemption_body(
        page_no=payload.page_no,
        page_size=payload.page_size,
        start=payload.date_start,
        end=payload.date_end,
    )
    ok, msg, raw = adapter.post_exemption_items(body)
    if not ok:
        raise _fail(msg)
    rows = vs.normalize_plain_list(raw, "details", "items", "records")
    return {"total": vs.list_total(raw), "items": rows, "query": body}


# --------------------------------------------------------------------------- #
# 关联内容删除（设计 44，2026-10-18）
# --------------------------------------------------------------------------- #
class ViolationDeleteTarget(BaseModel):
    """一条要删的关联对象（来自违规行的 `entity_type` + `entity_id`）。"""

    model_config = ConfigDict(extra="forbid")

    #: 违规对象类型码：1 商品 / 3 笔记（其余类型没有可删的本地实体）。
    entity_type: int
    entity_id: str = Field(min_length=1, max_length=64)


class ViolationDeleteRequest(BaseModel):
    """删除关联内容。

    **单条与批量共用本端点** —— 传 1 个 target 就是单条，传 N 个就是批量。
    不按条数分两个端点：同一动作两份实现必然漂移（DEF-044）。
    """

    model_config = ConfigDict(extra="forbid")

    platform_account_id: int
    targets: list[ViolationDeleteTarget] = Field(min_length=1)


@router.post("/violation/related-delete")
def violation_related_delete(
    payload: ViolationDeleteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除违规**关联的商品 / 笔记**：**先操作平台，再处理本地**。单条与批量共用。

    ## 顺序（用户口径 2026-10-18，全项目通用）

    > 「先操作平台数据再检查本地是否存在对应数据，存在则执行操作，不存在则跳过」

    | 阶段 | 商品（`entity_type=1`） | 笔记（`entity_type=3`） |
    |---|---|---|
    | ① 平台删除 | 千帆 `product/batch_delete_sku` | 商业笔记 `note/batch_delete_note` / 普通笔记创作者 `delete_note` |
    | ② 本地核对 | 查 `shop_products` | 查 `notes` |
    | ③ 本地删除 | 有 ⇒ 删台账行 + 从属行 | 有 ⇒ `purge_note` |
    | 本地没有 | **跳过**（`platform_only`） | **跳过**（`platform_only`） |

    ⚠️ **平台失败 ⇒ 本地不动**：平台还留着而本地已删，两边就对不上了。
    ⚠️ **本地没有不算失败**：本地是缓存，用户可能从没同步过这条数据。

    ## 回执四态（`results[].status`）

    | status | 含义 |
    |---|---|
    | `deleted` | 平台删成功 + 本地也有 ⇒ 本地一并删了 |
    | `platform_only` | 平台删成功 + 本地没有 ⇒ 跳过本地（**不是失败**） |
    | `failed` | 平台删除失败 ⇒ 本地未改动 |
    | `skipped` | 店铺 / 账号 / 直播间：没有可删实体 |

    ⚠️ **违规记录本身删不掉** —— 千帆违规域没有删除接口，违规单是平台处罚凭证。
    本端点删的是违规**指向的商品 / 笔记**，文案与回执都按这个口径写，
    避免用户以为「平台处罚被撤销了」。

    ## 边界

    * 归属校验：账号不属于当前用户 ⇒ 404 `ACCOUNT_NOT_FOUND`
    * 条数上限：`MAX_DELETE_TARGETS`，超出 400（不静默截断）
    * 需要对应平台的登录态：缺千帆 / 创作者令牌 ⇒ 该条 `failed` 并带原因
    """
    from backend.app.services.violation_delete_service import (
        MAX_DELETE_TARGETS,
        execute_delete,
    )

    if len(payload.targets) > MAX_DELETE_TARGETS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "VIOLATION_DELETE_TOO_MANY",
                "message": f"单次最多删除 {MAX_DELETE_TARGETS} 条，请分批操作",
            },
        )

    result = execute_delete(
        db,
        current_user,
        payload.platform_account_id,
        [t.model_dump() for t in payload.targets],
    )
    db.commit()
    return result
