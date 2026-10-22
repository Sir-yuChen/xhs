"""数据包列规则端点（设计 40，用户口径 2026-10-14）。

端点表（9 个 / 7 path）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/xhs/data-package-rules/sets` | 规则包清单（`enabled_only` / `with_rules` 可筛） |
| POST | `/xhs/data-package-rules/sets` | 新建规则包（重名 ⇒ **409**） |
| PATCH | `/xhs/data-package-rules/sets/{set_id}` | 改规则包（名 / 备注 / **启用停用** / 排序） |
| DELETE | `/xhs/data-package-rules/sets/{set_id}` | 删规则包（**级联删规则**，回被删条数） |
| GET | `/xhs/data-package-rules/rules` | 某包下的规则（`set_id` 必填） |
| POST | `/xhs/data-package-rules/rules` | 新建规则（同包同列重复 ⇒ **409**） |
| PATCH | `/xhs/data-package-rules/rules/{rule_id}` | 改规则（含启用停用） |
| DELETE | `/xhs/data-package-rules/rules/{rule_id}` | 删规则 |
| POST | `/xhs/data-package-rules/execute` | **执行一条规则**（后端算，前端不重复实现） |

## 为什么「启用/停用」走 `PATCH` 而不是独立端点

违规词库那边用了独立端点 `POST /{id}/enabled`，是因为**它不新增 path**（`/{id}` 已被 PATCH 占）。
这里同理可做，但**父/子两级都要**改 ⇒ 会多出两条 path。而本项目
LLM 网关的 `PATCH models/{model_id}` 已有「单字段改走 PATCH」的先例
⇒ 统一走 `PATCH`，**少两条 path**，且前端一次能改多个字段。

## 为什么「执行」在后端

前端另有「正则取值」那套（`applyPatternToColumn`），但**规则的执行语义必须单一实现**：
`extract_between` 的「只给 open 不给 close ⇒ 取到末尾」这类边界，前后端各写一遍必然漂移
（DEF-044 同型教训）。⇒ 后端算、前端只渲染。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models import User
from backend.app.services import data_package_rule_service as service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xhs/data-package-rules", tags=["xhs-data-package-rules"])


# --------------------------------------------------------------------------- #
# 入参模型
# --------------------------------------------------------------------------- #


class RuleSetCreateRequest(BaseModel):
    name: str = Field(..., description="规则包名（同一用户下唯一）")
    remark: str = Field("", description="备注")
    enabled: bool = Field(True, description="是否启用")


class RuleSetUpdateRequest(BaseModel):
    name: Optional[str] = None
    remark: Optional[str] = None
    enabled: Optional[bool] = Field(None, description="改启用状态（父停 ⇒ 整包失效）")
    sort_order: Optional[int] = None


class RuleCreateRequest(BaseModel):
    set_id: int = Field(..., description="所属规则包")
    rule_name: str = Field(..., description="规则名称（同一规则包内不可重复）")
    op: str = Field(..., description="操作类型，如 extract_segment")
    params: dict[str, Any] = Field({}, description="操作参数")
    enabled: bool = Field(True, description="是否启用")


class RuleUpdateRequest(BaseModel):
    rule_name: Optional[str] = None
    op: Optional[str] = None
    params: Optional[dict[str, Any]] = None
    enabled: Optional[bool] = None
    sort_order: Optional[int] = None


class ExecuteRequest(BaseModel):
    set_id: int = Field(..., description="规则包 id")
    rule_id: int = Field(..., description="规则 id")
    #: ⚠️ 整列原值由前端传（后端的**当前打开的数据包**并不在服务端状态里 —— 包是
    #: 用户本机磁盘上的文件，解析在前端）。传的是**原值**，不是上次结果。
    values: list[Any] = Field([], description="该列的原值（逐行）")
    #: **计算基础列**的下标（用户口径 2026-10-15：「运算类的规则需要指定计算的基础列，
    #: 默认当前列，可选择指定为其它列」）。
    #: ⚠️ 它**不参与计算**（`values` 已经是基础列的值），只是回传给前端做回显/落值判据
    #: —— 后端不持有「哪列用了哪条规则」的状态，目标列永远是**前端点开的那个表头**。
    source_column: Optional[int] = Field(
        None, description="计算基础列下标（默认当前列）"
    )
    #: ⚠️ 用户口径 2026-10-21：「固定数值或文本时应支持用户在 tip 窗口输入自定义文本，
    #: 新建规则输入的文本作为兜底」—— 现场敲的值**优先于**规则本身存的参数。
    params_override: Optional[dict] = Field(
        None, description="现场覆盖的规则参数（如 fixed_text 的 text），缺省用规则自身参数"
    )


def _fail(error: service.DataPackageRuleError) -> HTTPException:
    """服务层异常 ⇒ HTTP 异常（**结构化 code 透传给前端**）。"""
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


# --------------------------------------------------------------------------- #
# 规则包（父）
# --------------------------------------------------------------------------- #


@router.get("/sets")
def list_rule_sets(
    enabled_only: bool = Query(False, description="只列启用的（列头弹窗用）"),
    with_rules: bool = Query(False, description="一并带上规则（同样只带启用的）"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = service.list_sets(db, current_user.id, enabled_only=enabled_only)
    if not with_rules:
        return [service.serialize_set(r) for r in rows]
    out = []
    for row in rows:
        rules = service.list_rules(db, current_user.id, row.id, enabled_only=True)
        out.append(service.serialize_set(row, rules=rules))
    return out


@router.post("/sets", status_code=201)
def create_rule_set(
    payload: RuleSetCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        row = service.create_set(
            db,
            current_user.id,
            name=payload.name,
            remark=payload.remark,
            enabled=payload.enabled,
        )
    except service.DataPackageRuleError as error:
        raise _fail(error) from error
    return service.serialize_set(row)


@router.patch("/sets/{set_id}")
def update_rule_set(
    set_id: int,
    payload: RuleSetUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        row = service.update_set(
            db,
            current_user.id,
            set_id,
            name=payload.name,
            remark=payload.remark,
            enabled=payload.enabled,
            sort_order=payload.sort_order,
        )
    except service.DataPackageRuleError as error:
        raise _fail(error) from error
    return service.serialize_set(row)


@router.delete("/sets/{set_id}")
def delete_rule_set(
    set_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删规则包。回 `deleted_rules` = 被连带删掉的规则数（前端二次确认要用）。"""
    try:
        deleted_rules = service.delete_set(db, current_user.id, set_id)
    except service.DataPackageRuleError as error:
        raise _fail(error) from error
    return {"ok": True, "deleted_rules": deleted_rules}


# --------------------------------------------------------------------------- #
# 规则（子）
# --------------------------------------------------------------------------- #


@router.get("/rules")
def list_rules(
    set_id: int = Query(..., description="所属规则包"),
    enabled_only: bool = Query(False, description="只列启用的"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        rows = service.list_rules(db, current_user.id, set_id, enabled_only=enabled_only)
    except service.DataPackageRuleError as error:
        raise _fail(error) from error
    return [service.serialize_rule(r) for r in rows]


@router.post("/rules", status_code=201)
def create_rule(
    payload: RuleCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        row = service.create_rule(
            db,
            current_user.id,
            set_id=payload.set_id,
            rule_name=payload.rule_name,
            op=payload.op,
            params=payload.params,
            enabled=payload.enabled,
        )
    except service.DataPackageRuleError as error:
        raise _fail(error) from error
    return service.serialize_rule(row)


@router.patch("/rules/{rule_id}")
def update_rule(
    rule_id: int,
    payload: RuleUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        row = service.update_rule(
            db,
            current_user.id,
            rule_id,
            rule_name=payload.rule_name,
            op=payload.op,
            params=payload.params,
            enabled=payload.enabled,
            sort_order=payload.sort_order,
        )
    except service.DataPackageRuleError as error:
        raise _fail(error) from error
    return service.serialize_rule(row)


@router.delete("/rules/{rule_id}")
def delete_rule(
    rule_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        service.delete_rule(db, current_user.id, rule_id)
    except service.DataPackageRuleError as error:
        raise _fail(error) from error
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #


@router.post("/execute")
def execute_rule(
    payload: ExecuteRequest = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """执行一条规则（**后端算**）。

    返回 `{values, missed, total, summary, reason, source_column, rule_id, set_id, updated_at}`。

    ⚠️ `values` 里 `null` = 该行提不出 ⇒ 前端**留空**（不是填原值，见服务层 docstring）。
    ⚠️ **运算类也会 `null`**：`math_scale` 遇到空 / 非数值行 ⇒ 留空
    （用户口径 2026-10-21「不应该再默认为 0 进行计算，应红框提示，并取消勾选」），
    行下标在 `missed_indexes` 里，供前端标红 + 取消勾选。
    ⚠️ `source_column` 原样回传（计算基础列），供前端回显与落值判据。
    ⚠️ 同时回 `updated_at`：前端据此判断「规则被改过了，要重算」
    （用户口径「数据包规则调整后实时生效」）。
    """
    try:
        return service.execute_for_column(
            db,
            current_user.id,
            set_id=payload.set_id,
            rule_id=payload.rule_id,
            values=list(payload.values or []),
            source_column=payload.source_column,
            params_override=payload.params_override,
        )
    except service.DataPackageRuleError as error:
        raise _fail(error) from error
