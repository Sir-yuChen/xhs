"""系统字典管理端点（设计 55 §5 / 设计 49 模块四）。

| 方法 | 路径 | 权限码 |
|---|---|---|
| `GET`    | `/api/system/dict/options` | **无（登录即可）** —— 前端统一读出口 |
| `GET`    | `/api/system/dict/types` | `system:dict:view` |
| `POST`   | `/api/system/dict/types` | `system:dict:create` |
| `PUT`    | `/api/system/dict/types/{type_id}` | `system:dict:edit` |
| `DELETE` | `/api/system/dict/types/{type_id}` | `system:dict:delete` |
| `GET`    | `/api/system/dict/items` | `system:dict:view` |
| `POST`   | `/api/system/dict/items` | `system:dict:create` |
| `PUT`    | `/api/system/dict/items/{item_id}` | `system:dict:edit` |
| `DELETE` | `/api/system/dict/items/{item_id}` | `system:dict:delete` |

## 与 dataPanel 骨架的对应

| dataPanel | 本项目 | 说明 |
|---|---|---|
| `t_dict_type` | `dict_types` | 类型（`code` 唯一） |
| `t_dict_data` | `dict_items` | 项（`(type_id, value)` 唯一） |
| `belong`（多平台列） | **不搬** | 本项目多平台靠 `menus.workspace` 分层，字典是全域的 |

## 三条判据

### ① 改类型 `code` ⇒ 必须清缓存

dataPanel 在 `UpdateDictType` 里调 `ClearDictCache`。本项目同理：
字典项按 `type_code` 反查（前端拿 `code` 去取项），改了 `code` 之后
旧缓存里的 `code → items` 映射就全错了。

### ② 删类型 ⇒ **级联删项**，但必须在响应里报条数

`dict_items.type_id` 是 CASCADE（设计 47 定稿），但**静默级联等于数据丢失**：
用户点「删除类型」时看不到会连带删掉 12 个选项。所以端点先数一遍，
把条数放进响应，前端据此提示「已删除类型及其 N 个选项」。

### ③ 内置字典（`is_system`）不许删

`sys_identity_type` / `sys_status` 这类被代码引用的类型，删了会让
前端取不到选项而静默显示空（比报错更难查）。允许改项，不许删类型。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.perm_deps import require_perm
from backend.app.core.time import shanghai_now
from backend.app.models.dict import LIST_CLASS_PATTERN, DictItem, DictType
from backend.app.models.user import User
from backend.app.schemas.system import DictItemListOut, DictOptionsOut, DictTypeListOut

router = APIRouter(prefix="/system/dict", tags=["system"])

DICT_CODE_PATTERN = r"^[a-z][a-z0-9_]{1,63}$"


class DictTypeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=2, max_length=64, pattern=DICT_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=64)
    status: str = Field(default="normal", pattern="^(normal|disabled)$")
    remark: str = Field(default="", max_length=255)


class DictTypeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Optional[str] = Field(default=None, min_length=2, max_length=64,
                                pattern=DICT_CODE_PATTERN)
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    status: Optional[str] = Field(default=None, pattern="^(normal|disabled)$")
    remark: Optional[str] = Field(default=None, max_length=255)


class DictItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type_id: int
    label: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=64)
    sort_order: int = 0
    #: 值域受 `ck_dict_items_list_class` 约束（设计 63 §2.3 放宽到 antd 全量预设色）。
    #: ⚠️ 默认必须是 `"default"` 而不是 `""` —— 空串不满足 CHECK，实测建项直接
    #: `IntegrityError: CHECK constraint failed: ck_dict_items_list_class`。
    list_class: str = Field(default="default", pattern=LIST_CLASS_PATTERN)
    status: str = Field(default="normal", pattern="^(normal|disabled)$")
    remark: str = Field(default="", max_length=255)


class DictItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Optional[str] = Field(default=None, min_length=1, max_length=64)
    value: Optional[str] = Field(default=None, min_length=1, max_length=64)
    sort_order: Optional[int] = None
    list_class: Optional[str] = Field(
        default=None, pattern=LIST_CLASS_PATTERN
    )
    status: Optional[str] = Field(default=None, pattern="^(normal|disabled)$")
    remark: Optional[str] = Field(default=None, max_length=255)


def _serialize_type(row: DictType, *, item_count: int = 0) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "status": row.status,
        "remark": getattr(row, "remark", "") or "",
        "is_system": bool(getattr(row, "is_system", False)),
        "item_count": item_count,
        "created_at": row.created_at.isoformat() if getattr(row, "created_at", None) else None,
    }


def _serialize_item(row: DictItem) -> dict:
    return {
        "id": row.id,
        "type_id": row.type_id,
        "type_code": getattr(row, "type_code", "") or "",
        "label": row.label,
        "value": row.value,
        "sort_order": row.sort_order,
        "list_class": getattr(row, "list_class", "") or "",
        "status": row.status,
        "remark": getattr(row, "remark", "") or "",
    }


def _clear_dict_cache() -> None:
    """清字典缓存。

    ⚠️ 现状：字典读取目前**没有进程内缓存**（每次查库）。此函数保留为
    唯一失效入口 —— 将来加缓存时只改这里，不在各处散写（DEF-044）。
    权限缓存也要清：字典项参与「菜单类型 / 状态」这类下拉，
    但**不影响** `perms`，故不清权限缓存。
    """
    return None


def _get_type_or_404(db: Session, type_id: int) -> DictType:
    row = db.get(DictType, type_id)
    if row is None:
        raise HTTPException(
            404, detail={"code": "DICT_TYPE_NOT_FOUND", "message": "字典类型不存在"}
        )
    return row


# --------------------------------------------------------------------------- #
# 统一读出口（前端所有枚举文案/下拉的唯一取数点）
# --------------------------------------------------------------------------- #
@router.get("/options", response_model=DictOptionsOut)
def list_dict_options(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    codes: Optional[str] = None,
) -> dict:
    """批量取字典选项 —— **前端统一出入口**（登录即可、无权限码）。

    与管理端 `GET /items`（`system:dict:view`）的差异：
    - 权限：任何登录用户可读（枚举文案是每个页面的展示依赖，不是管理能力）；
    - 形状：只下发消费方需要的四个字段（value/label/list_class/sort_order），
      管理字段（status/remark/type_id）不下发；
    - 过滤：类型与项都只出 `normal`（停用组整组不出，停用项单条不出），
      管理端要看到停用行，这里不要。

    `codes`：逗号分隔的类型 code 白名单；缺省 = 全量 normal 组
    （表就几十行，一次全取比按页散取更省往返 —— 前端统一 store 一次灌满）。
    每次读库、无进程内缓存（设计 59 口径）—— 字典改完即生效，「实时可变」
    的服务端侧由这一点保证。
    """
    wanted: Optional[set[str]] = None
    if codes:
        wanted = {c.strip() for c in codes.split(",") if c.strip()}

    type_rows = db.scalars(
        select(DictType).where(DictType.status == "normal").order_by(DictType.id)
    ).all()
    if wanted is not None:
        type_rows = [t for t in type_rows if t.code in wanted]

    items_by_type: dict[int, list[dict]] = {}
    if type_rows:
        item_rows = db.scalars(
            select(DictItem)
            .where(
                DictItem.type_id.in_([t.id for t in type_rows]),
                DictItem.status == "normal",
            )
            .order_by(DictItem.type_id, DictItem.sort_order, DictItem.id)
        ).all()
        for row in item_rows:
            items_by_type.setdefault(row.type_id, []).append(
                {
                    "value": row.value,
                    "label": row.label,
                    "list_class": getattr(row, "list_class", "") or "default",
                    "sort_order": row.sort_order,
                }
            )

    return {
        "items": {
            t.code: items_by_type.get(t.id, [])
            for t in type_rows
        }
    }


# --------------------------------------------------------------------------- #
# 类型
# --------------------------------------------------------------------------- #
@router.get("/types", response_model=DictTypeListOut)
def list_dict_types(
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:dict:view")),
    name: Optional[str] = None,
    code: Optional[str] = None,
) -> dict:
    """字典类型列表（带项数）。"""
    stmt = select(DictType)
    if name:
        stmt = stmt.where(func.lower(DictType.name).like(f"%{name.strip().lower()}%"))
    if code:
        stmt = stmt.where(func.lower(DictType.code).like(f"%{code.strip().lower()}%"))
    rows = db.scalars(stmt.order_by(DictType.id)).all()

    counts = dict(
        db.execute(select(DictItem.type_id, func.count()).group_by(DictItem.type_id)).all()
    )
    return {
        "items": [_serialize_type(r, item_count=counts.get(r.id, 0)) for r in rows],
        "total": len(rows),
    }


@router.post("/types", status_code=status.HTTP_201_CREATED)
def create_dict_type(
    payload: DictTypeCreate,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:dict:create")),
) -> dict:
    if db.scalar(select(DictType.id).where(DictType.code == payload.code)) is not None:
        raise HTTPException(
            409, detail={"code": "DICT_CODE_TAKEN", "message": "字典类型编码已存在"}
        )
    row = DictType(
        code=payload.code,
        name=payload.name,
        status=payload.status,
        created_at=shanghai_now(),
    )
    if hasattr(row, "remark"):
        row.remark = payload.remark
    db.add(row)
    db.commit()
    db.refresh(row)
    _clear_dict_cache()
    return _serialize_type(row)


@router.put("/types/{type_id}")
def update_dict_type(
    type_id: int,
    payload: DictTypeUpdate,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:dict:edit")),
) -> dict:
    """改类型。**改 `code` 必须清缓存**（dataPanel `ClearDictCache` 同款判据）。"""
    row = _get_type_or_404(db, type_id)
    code_changed = False

    if payload.code is not None and payload.code != row.code:
        if getattr(row, "is_system", False):
            raise HTTPException(
                400,
                detail={
                    "code": "DICT_SYSTEM_CODE_LOCKED",
                    "message": "内置字典类型的编码不允许修改",
                },
            )
        if db.scalar(
            select(DictType.id).where(DictType.code == payload.code, DictType.id != row.id)
        ) is not None:
            raise HTTPException(
                409, detail={"code": "DICT_CODE_TAKEN", "message": "字典类型编码已存在"}
            )
        # 项上的 type_code 是冗余快照列，改 code 要同步（否则前端按 code 取不到项）
        db.query(DictItem).filter(DictItem.type_id == row.id).update(
            {"type_code": payload.code}, synchronize_session=False
        )
        row.code = payload.code
        code_changed = True

    for field in ("name", "status", "remark"):
        value = getattr(payload, field)
        if value is not None and hasattr(row, field):
            setattr(row, field, value)

    db.commit()
    db.refresh(row)
    if code_changed:
        _clear_dict_cache()
    item_count = db.scalar(
        select(func.count()).select_from(DictItem).where(DictItem.type_id == row.id)
    ) or 0
    return _serialize_type(row, item_count=item_count)


@router.delete("/types/{type_id}")
def delete_dict_type(
    type_id: int,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:dict:delete")),
) -> dict:
    """删类型 ⇒ **级联删项**（`dict_items.type_id` CASCADE）。响应里报条数。"""
    row = _get_type_or_404(db, type_id)
    if getattr(row, "is_system", False):
        raise HTTPException(
            400,
            detail={
                "code": "DICT_SYSTEM_PROTECTED",
                "message": "内置字典类型不允许删除（删了前端会静默显示空选项）",
            },
        )

    item_count = db.scalar(
        select(func.count()).select_from(DictItem).where(DictItem.type_id == row.id)
    ) or 0
    db.delete(row)
    db.commit()
    _clear_dict_cache()
    return {
        "ok": True,
        "id": type_id,
        "deleted": True,
        # ⚠️ 必须报出级联删了多少项，否则用户不知道点这一下丢了什么
        "deleted_items": item_count,
    }


# --------------------------------------------------------------------------- #
# 项
# --------------------------------------------------------------------------- #
@router.get("/items", response_model=DictItemListOut)
def list_dict_items(
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:dict:view")),
    type_id: Optional[int] = None,
    type_code: Optional[str] = None,
    label: Optional[str] = None,
    include_disabled: bool = True,
) -> dict:
    """字典项列表。`type_code` 与 `type_id` 二选一（前端按 code 取更方便）。"""
    stmt = select(DictItem)
    if type_id is not None:
        stmt = stmt.where(DictItem.type_id == type_id)
    if type_code:
        stmt = stmt.where(DictItem.type_code == type_code)
    if label:
        stmt = stmt.where(func.lower(DictItem.label).like(f"%{label.strip().lower()}%"))
    if not include_disabled:
        stmt = stmt.where(DictItem.status == "normal")

    rows = db.scalars(stmt.order_by(DictItem.type_id, DictItem.sort_order, DictItem.id)).all()
    return {"items": [_serialize_item(r) for r in rows], "total": len(rows)}


@router.post("/items", status_code=status.HTTP_201_CREATED)
def create_dict_item(
    payload: DictItemCreate,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:dict:create")),
) -> dict:
    """新建字典项。`(type_id, value)` 唯一。"""
    parent = _get_type_or_404(db, payload.type_id)
    dup = db.scalar(
        select(DictItem.id).where(
            DictItem.type_id == payload.type_id, DictItem.value == payload.value
        )
    )
    if dup is not None:
        raise HTTPException(
            409,
            detail={
                "code": "DICT_VALUE_TAKEN",
                "message": f"该类型下 value={payload.value} 已存在",
            },
        )

    #: ⚠️ `DictItem` **没有 `created_at` 列**（只有 `DictType` 有，实测确认）——
    #: 传了会 `TypeError: 'created_at' is an invalid keyword argument`。
    row = DictItem(
        type_id=payload.type_id,
        type_code=parent.code,
        label=payload.label,
        value=payload.value,
        sort_order=payload.sort_order,
        status=payload.status,
    )
    for field in ("list_class", "remark"):
        if hasattr(row, field):
            setattr(row, field, getattr(payload, field))
    db.add(row)
    db.commit()
    db.refresh(row)
    _clear_dict_cache()
    return _serialize_item(row)


@router.put("/items/{item_id}")
def update_dict_item(
    item_id: int,
    payload: DictItemUpdate,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:dict:edit")),
) -> dict:
    row = db.get(DictItem, item_id)
    if row is None:
        raise HTTPException(
            404, detail={"code": "DICT_ITEM_NOT_FOUND", "message": "字典项不存在"}
        )

    if payload.value is not None and payload.value != row.value:
        dup = db.scalar(
            select(DictItem.id).where(
                DictItem.type_id == row.type_id,
                DictItem.value == payload.value,
                DictItem.id != row.id,
            )
        )
        if dup is not None:
            raise HTTPException(
                409,
                detail={"code": "DICT_VALUE_TAKEN", "message": "该类型下 value 已存在"},
            )
        row.value = payload.value

    for field in ("label", "sort_order", "list_class", "status", "remark"):
        value = getattr(payload, field)
        if value is not None and hasattr(row, field):
            setattr(row, field, value)

    db.commit()
    db.refresh(row)
    _clear_dict_cache()
    return _serialize_item(row)


@router.delete("/items/{item_id}")
def delete_dict_item(
    item_id: int,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:dict:delete")),
) -> dict:
    row = db.get(DictItem, item_id)
    if row is None:
        raise HTTPException(
            404, detail={"code": "DICT_ITEM_NOT_FOUND", "message": "字典项不存在"}
        )
    db.delete(row)
    db.commit()
    _clear_dict_cache()
    return {"ok": True, "id": item_id, "deleted": True}
