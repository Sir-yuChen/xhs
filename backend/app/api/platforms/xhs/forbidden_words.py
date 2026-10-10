"""违规词库端点（设计 25 §2；用户口径 2026-10-08）。

端点表（5 个）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/xhs/forbidden-words` | 词条列表（`enabled_only` 可筛） |
| POST | `/xhs/forbidden-words` | 新增（重复 ⇒ **409**） |
| PATCH | `/xhs/forbidden-words/{id}` | 修改（词 / 匹配模式 / 备注） |
| POST | `/xhs/forbidden-words/{id}/enabled` | **停用 / 启用**（`enabled: bool`） |
| DELETE | `/xhs/forbidden-words/{id}` | 删除 |

## 为什么「停用/启用」是独立端点而不是 PATCH 的一种

用户口径把「新增 / 修改 / **停用** / **启用**」并列为四个动作，前端是四个按钮。
塞进 PATCH 的 `enabled` 字段也能做，但那样前端要**先 GET 单条**才知道当前态才能
把按钮文案摆对（「停用」还是「启用」）。独立端点 + `enabled` 布尔入参，
前端不读状态也能正确表达意图，且**不新增 path**（`/{id}` 已被 PATCH 占位）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models import User
from backend.app.services import forbidden_word_service as service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xhs/forbidden-words", tags=["xhs-forbidden-words"])


class ForbiddenWordCreateRequest(BaseModel):
    word: str = Field(..., description="违规词原文")
    match_mode: Optional[int] = Field(None, description="1=精确（默认） 2=包含")
    remark: str = Field("", description="备注")
    enabled: bool = Field(True, description="是否启用")
    #: 分组名（用户自建）。空串 = 未分组（默认）。
    group_name: str = Field("", description="分组名；空 = 未分组")


class ForbiddenWordUpdateRequest(BaseModel):
    word: Optional[str] = None
    match_mode: Optional[int] = None
    remark: Optional[str] = None
    enabled: Optional[bool] = None
    group_name: Optional[str] = Field(None, description="改分组；空串 = 移到未分组")


class ForbiddenWordEnabledRequest(BaseModel):
    enabled: bool = Field(..., description="true=启用 false=停用")


def _fail(error: service.ForbiddenWordError) -> HTTPException:
    """服务层异常 ⇒ HTTP 异常（**结构化 code 透传给前端**）。"""
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


@router.get("")
def list_forbidden_words(
    enabled_only: bool = Query(False, description="只列启用中的（OCR 匹配用）"),
    group_name: Optional[str] = Query(None, description="只看某个分组（空 = 不过滤）"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    groups = [group_name] if group_name else None
    return [
        service.serialize(row)
        for row in service.list_words(
            db, current_user.id, enabled_only=enabled_only, groups=groups
        )
    ]


@router.get("/groups")
def list_forbidden_word_groups(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**已有分组**清单（名字 + 词条数）。

    预检页的「指定分组」下拉用它 —— 只列**真有词**的分组（空分组选它等于不检测）。
    """
    return service.list_groups(db, current_user.id)


@router.post("", status_code=201)
def create_forbidden_word(
    payload: ForbiddenWordCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return service.create_word(
            db,
            current_user.id,
            word=payload.word,
            match_mode=payload.match_mode,
            remark=payload.remark,
            enabled=payload.enabled,
            group_name=payload.group_name,
        )
    except service.ForbiddenWordError as error:
        raise _fail(error) from error


class ForbiddenWordGroupEnabledRequest(BaseModel):
    """整组启用 / 停用。"""

    enabled: bool = Field(..., description="true=启用 false=停用（组内词全部退出匹配）")


@router.post("/groups/{name}/enabled")
def set_forbidden_word_group_enabled(
    name: str,
    payload: ForbiddenWordGroupEnabledRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**整组启用 / 停用**（用户口径 2026-10-08：「组新增启用停用功能」）。

    ⚠️ 组停用 ⇒ **组内所有词立即退出 OCR 匹配**（不必逐条停用）。
    这是 OCR 侧的真开关，不是界面装饰。

    `name` 用路径参数；「未分组」传空串（`/groups//enabled` 不合法，所以
    未分组走 `?` 也别扭 ⇒ 前端对未分组传一个占位符 `__ungrouped__`，
    这里翻译回空串）。
    """
    raw = "" if name == "__ungrouped__" else name
    try:
        return service.set_group_enabled(db, current_user.id, raw, payload.enabled)
    except service.ForbiddenWordError as error:
        raise _fail(error) from error


@router.patch("/{word_id}")
def update_forbidden_word(
    word_id: int,
    payload: ForbiddenWordUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return service.update_word(
            db,
            current_user.id,
            word_id,
            word=payload.word,
            match_mode=payload.match_mode,
            remark=payload.remark,
            enabled=payload.enabled,
            group_name=payload.group_name,
        )
    except service.ForbiddenWordError as error:
        raise _fail(error) from error


@router.post("/{word_id}/enabled")
def set_forbidden_word_enabled(
    word_id: int,
    payload: ForbiddenWordEnabledRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """**停用 / 启用**（软开关；停用后不参与 OCR 匹配）。"""
    try:
        return service.set_enabled(db, current_user.id, word_id, payload.enabled)
    except service.ForbiddenWordError as error:
        raise _fail(error) from error


@router.delete("/{word_id}")
def delete_forbidden_word(
    word_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        service.delete_word(db, current_user.id, word_id)
    except service.ForbiddenWordError as error:
        raise _fail(error) from error
    return {"deleted": word_id}
