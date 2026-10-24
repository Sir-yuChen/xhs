"""系统菜单 / 权限码管理端点（设计 55 §4 / 设计 49 模块三）。

| 方法 | 路径 | 权限码 |
|---|---|---|
| `GET`    | `/api/system/menus/tree` | `system:menu:view` |
| `GET`    | `/api/system/menus/{menu_id}` | `system:menu:view` |
| `POST`   | `/api/system/menus` | `system:menu:create` |
| `PUT`    | `/api/system/menus/{menu_id}` | `system:menu:edit` |
| `DELETE` | `/api/system/menus/{menu_id}` | `system:menu:delete` |
| `POST`   | `/api/system/menus/reorder` | `system:menu:edit` |

## 菜单启停 ≠ 授权变更（这是两条不同的路）

| 动作 | 影响 | 是否踢人 |
|---|---|---|
| 改 `role_menus`（授权） | 「这些人能看到这页吗」 | **踢**（`token_version+1`） |
| 改 `menus.status`（启停） | 「这个功能在全站是否上线」 | **不踢**，只清缓存 |

理由：平台接入（抖音 / 闲鱼上线）这种**低频全站操作**不能让所有人掉线。
启停是「功能开关」，不是「谁能用」—— 后者才有权限语义。

⚠️ 但启停**不静默级联**：停用父目录时，子菜单的 `status` 保持原样。
级联停用会让「重新启用父目录」时无法知道子菜单原本该不该开
（用户改过的不该被系统悄悄覆盖，与种子「只补缺不覆盖」同一纪律）。

## 删除保护

* 有 `role_menus` 引用 ⇒ **409**（`role_menus.menu_id` 是 RESTRICT）。
  不能 CASCADE —— 那样删一个菜单会静默收回所有人的这块权限；
* 有子菜单 ⇒ **409**（先删子或先移动）；
* 内置菜单（`perms` 在登记表里的）⇒ 允许删，但**删了权限码就悬空** ——
  由 `route_perms.py` 的启动守卫在下次启动时报错。这里放宽是为了让
  「权限码退场」这个动作可以在界面上完成，而不是必须改代码。

## `parent_id = 0` 而不是 NULL

本项目用 `0` 表示「顶级」（`menus.parent_id` 无外键，S1 定稿）。
好处是索引 / `ORDER BY` 不用处理 NULL；坏处是不能用 FK 约束父子关系 ——
所以「父菜单必须存在」由端点层校验（见 `_assert_parent_ok`）。
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
from backend.app.core.perms import ALL_PERMS, ROLE_ADMIN, normalize_role
from backend.app.core.time import shanghai_now
from backend.app.models.menu import Menu
from backend.app.models.role_menu import RoleMenu
from backend.app.models.user import User
from backend.app.core import permission_cache, rbac_service

router = APIRouter(prefix="/system/menus", tags=["system"])

MENU_TYPES = ("M", "C", "F")


class MenuCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: int = 0
    name: str = Field(min_length=1, max_length=64)
    menu_type: str = Field(pattern="^[MCF]$")
    perms: str = Field(default="", max_length=128)
    path: str = Field(default="", max_length=255)
    component: str = Field(default="", max_length=255)
    icon: str = Field(default="", max_length=64)
    workspace: str = Field(default="", max_length=32)
    sort_order: int = 0
    is_visible: bool = True
    status: str = Field(default="normal", pattern="^(normal|disabled)$")


class MenuUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: Optional[int] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    perms: Optional[str] = Field(default=None, max_length=128)
    path: Optional[str] = Field(default=None, max_length=255)
    component: Optional[str] = Field(default=None, max_length=255)
    icon: Optional[str] = Field(default=None, max_length=64)
    workspace: Optional[str] = Field(default=None, max_length=32)
    sort_order: Optional[int] = None
    is_visible: Optional[bool] = None
    status: Optional[str] = Field(default=None, pattern="^(normal|disabled)$")


class MenuReorder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: `[{id, parent_id, sort_order}]` —— 前端拖拽后的完整顺序
    items: list[dict] = Field(default_factory=list)


def _serialize(menu: Menu, *, child_count: int = 0) -> dict:
    return {
        "id": menu.id,
        "parent_id": menu.parent_id,
        "name": menu.name,
        "menu_type": menu.menu_type,
        "perms": menu.perms or "",
        "path": menu.path or "",
        "component": menu.component or "",
        "icon": menu.icon or "",
        "workspace": menu.workspace or "",
        "sort_order": menu.sort_order,
        "is_visible": bool(menu.is_visible),
        "status": menu.status,
        "child_count": child_count,
        "created_at": menu.created_at.isoformat() if menu.created_at else None,
    }


def _build_tree(rows: list[Menu]) -> list[dict]:
    """把扁平行拼成树。**父不存在的行按顶级处理**（不丢数据，但会在日志里体现）。"""
    counts: dict[int, int] = {}
    for row in rows:
        counts[row.parent_id] = counts.get(row.parent_id, 0) + 1

    by_parent: dict[int, list[Menu]] = {}
    existing = {r.id for r in rows}
    for row in rows:
        key = row.parent_id if row.parent_id in existing else 0
        by_parent.setdefault(key, []).append(row)

    def build(parent: int) -> list[dict]:
        items = []
        for row in sorted(by_parent.get(parent, []), key=lambda r: (r.sort_order, r.id)):
            node = _serialize(row, child_count=counts.get(row.id, 0))
            node["children"] = build(row.id)
            items.append(node)
        return items

    return build(0)


def _get_or_404(db: Session, menu_id: int) -> Menu:
    menu = db.get(Menu, menu_id)
    if menu is None:
        raise HTTPException(404, detail={"code": "MENU_NOT_FOUND", "message": "菜单不存在"})
    return menu


def _assert_parent_ok(db: Session, parent_id: int, *, self_id: int | None = None) -> None:
    """父菜单必须存在（`parent_id=0` 表示顶级）；且不能把自己挂到自己子树下。"""
    if parent_id == 0:
        return
    if self_id is not None and parent_id == self_id:
        raise HTTPException(
            400, detail={"code": "MENU_PARENT_SELF", "message": "不能把菜单挂到自己下面"}
        )
    parent = db.get(Menu, parent_id)
    if parent is None:
        raise HTTPException(
            400, detail={"code": "MENU_PARENT_NOT_FOUND", "message": "父菜单不存在"}
        )
    # 防环：从 parent 往上爬，遇到 self 就说明成环
    cursor = parent
    seen: set[int] = set()
    while cursor is not None and cursor.parent_id and cursor.id not in seen:
        if self_id is not None and cursor.parent_id == self_id:
            raise HTTPException(
                400,
                detail={
                    "code": "MENU_PARENT_CYCLE",
                    "message": "不能把菜单挂到自己的子孙下面",
                },
            )
        seen.add(cursor.id)
        cursor = db.get(Menu, cursor.parent_id)


def _assert_perms_ok(perms: str, *, exclude_id: int | None = None) -> None:
    """`perms` 非空时必须**已登记**，且不与别的菜单重复。

    未登记的码挂上去，`require_perm` 会在 import 期就拒绝加载它 ——
    等于这个按钮永远授不出去、点了必然 500。
    """
    code = (perms or "").strip()
    if not code:
        return
    if code not in ALL_PERMS:
        raise HTTPException(
            400,
            detail={
                "code": "PERM_UNREGISTERED",
                "message": f"权限码 {code} 未在 core/perms_registry.py 登记",
            },
        )


# --------------------------------------------------------------------------- #
# 当前用户的菜单与权限（登录即可，无权限码）
# --------------------------------------------------------------------------- #
def _visible_menus_for(db: Session, user: User) -> list[Menu]:
    """该用户可见的菜单行（已排除停用 / 隐藏 / 未授权 / 待审）。

    ⚠️ **待审账号（`status='pending'`）返回空菜单**（v1.1.0 / S3 实测修正）。

    原因：`user` 角色的授权用的是**模板行**（`role_menus.user_id IS NULL`），
    它对所有持 `user` 角色的人生效。于是「刚注册、还没被批准」的人会立刻
    看到 41 个菜单 —— 那等于注册即授权，待审机制形同虚设。

    待审的正确语义是「能登录、能看『等待审核』、不能看任何业务页」。
    """
    if (getattr(user, "status", None) or "normal") == "pending":
        return []

    rows = list(db.scalars(select(Menu).where(Menu.status == "normal")).all())
    if normalize_role(getattr(user, "role", None)) == ROLE_ADMIN:
        # admin 是代码事实：不查授权表（防锁死，见 rbac_service docstring）
        return [r for r in rows if r.is_visible]

    granted = set(rbac_service.menu_ids_for_user(db, user))
    by_id = {r.id: r for r in rows}
    allowed = {mid for mid in granted if mid in by_id}

    # 补祖先链：只补「同一棵树里也合规」的祖先，避免把停用的目录带出来
    for mid in list(allowed):
        cursor = by_id.get(mid)
        seen: set[int] = set()
        while cursor is not None and cursor.parent_id and cursor.id not in seen:
            seen.add(cursor.id)
            parent = by_id.get(cursor.parent_id)
            if parent is None:
                break
            allowed.add(parent.id)
            cursor = parent

    return [r for r in rows if r.id in allowed and r.is_visible]


def _to_tree(rows: list[Menu]) -> list[dict]:
    by_parent: dict[int, list[Menu]] = {}
    existing = {r.id for r in rows}
    for row in rows:
        key = row.parent_id if row.parent_id in existing else 0
        by_parent.setdefault(key, []).append(row)

    def build(parent: int, depth: int = 0) -> list[dict]:
        out = []
        for row in sorted(by_parent.get(parent, []), key=lambda r: (r.sort_order, r.id)):
            out.append(
                {
                    "id": row.id,
                    "parent_id": row.parent_id,
                    "name": row.name,
                    "menu_type": row.menu_type,
                    "perms": row.perms or "",
                    "path": row.path or "",
                    "component": row.component or "",
                    "icon": row.icon or "",
                    "workspace": row.workspace or "",
                    "sort_order": row.sort_order,
                    "is_visible": bool(row.is_visible),
                    "status": row.status,
                    "children": build(row.id, depth + 1),
                }
            )
        return out

    return build(0)


@router.get("/mine")
def my_menus(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    workspace: Optional[str] = None,
) -> dict:
    """当前用户可见的菜单树（侧栏数据源）。**登录即可**，无需权限码。

    `workspace` 可选过滤（`xhs` / `ai` / `system`）—— 前端切工作区时只取其一片，
    减少一次性下发的数据量。
    """
    rows = _visible_menus_for(db, current_user)
    if workspace:
        # 按工作区过滤时**保留它的祖先**，否则过滤后树会断
        keep = {r.id for r in rows if (r.workspace or "") == workspace}
        by_id = {r.id: r for r in rows}
        for mid in list(keep):
            cursor = by_id.get(mid)
            while cursor is not None and cursor.parent_id and cursor.id:
                parent = by_id.get(cursor.parent_id)
                if parent is None:
                    break
                keep.add(parent.id)
                cursor = parent
        rows = [r for r in rows if r.id in keep]

    return {"items": _to_tree(rows), "total": len(rows)}



@router.get("/tree")
def get_menu_tree(
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:menu:view")),
    workspace: Optional[str] = None,
    include_disabled: bool = True,
) -> dict:
    """菜单树（M/C/F 三级）。

    `include_disabled=False` 时只返回 `status='normal'` 的行 ——
    前端「侧栏」用这个，管理页用默认（全部）。
    """
    stmt = select(Menu)
    if workspace:
        stmt = stmt.where(Menu.workspace == workspace)
    if not include_disabled:
        stmt = stmt.where(Menu.status == "normal")
    rows = db.scalars(stmt).all()
    return {"items": _build_tree(list(rows)), "total": len(rows)}


@router.get("/{menu_id}")
def get_menu(
    menu_id: int,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:menu:view")),
) -> dict:
    menu = _get_or_404(db, menu_id)
    child_count = db.scalar(
        select(func.count()).select_from(Menu).where(Menu.parent_id == menu.id)
    ) or 0
    return _serialize(menu, child_count=child_count)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_menu(
    payload: MenuCreate,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:menu:create")),
) -> dict:
    """新建菜单 / 按钮。"""
    _assert_parent_ok(db, payload.parent_id)
    _assert_perms_ok(payload.perms)

    if payload.perms:
        dup = db.scalar(select(Menu.id).where(Menu.perms == payload.perms))
        if dup is not None:
            raise HTTPException(
                409,
                detail={"code": "PERM_TAKEN", "message": f"权限码 {payload.perms} 已被占用"},
            )
    if payload.path:
        dup = db.scalar(select(Menu.id).where(Menu.path == payload.path))
        if dup is not None:
            raise HTTPException(
                409, detail={"code": "MENU_PATH_TAKEN", "message": "路由路径已被占用"}
            )

    menu = Menu(
        parent_id=payload.parent_id,
        name=payload.name,
        menu_type=payload.menu_type,
        perms=payload.perms,
        path=payload.path,
        component=payload.component,
        icon=payload.icon,
        workspace=payload.workspace,
        sort_order=payload.sort_order,
        is_visible=payload.is_visible,
        status=payload.status,
        created_at=shanghai_now(),
    )
    db.add(menu)
    db.commit()
    db.refresh(menu)
    permission_cache.clear()
    return _serialize(menu)


@router.put("/{menu_id}")
def update_menu(
    menu_id: int,
    payload: MenuUpdate,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:menu:edit")),
) -> dict:
    """更新菜单。

    ⚠️ **启停只清缓存，不踢人**（见模块 docstring）。
    ⚠️ `perms` 一旦被挂到菜单上就不建议再改 —— 改了等于把权限码换掉，
       已授权行的含义随之改变。这里允许改（配置错时要能修），但会清缓存。
    """
    menu = _get_or_404(db, menu_id)
    text_changed = False
    status_changed = False

    if payload.parent_id is not None and payload.parent_id != menu.parent_id:
        _assert_parent_ok(db, payload.parent_id, self_id=menu.id)
        menu.parent_id = payload.parent_id
        text_changed = True

    if payload.perms is not None and payload.perms != (menu.perms or ""):
        _assert_perms_ok(payload.perms)
        if payload.perms:
            dup = db.scalar(
                select(Menu.id).where(Menu.perms == payload.perms, Menu.id != menu.id)
            )
            if dup is not None:
                raise HTTPException(
                    409,
                    detail={"code": "PERM_TAKEN", "message": "权限码已被占用"},
                )
        menu.perms = payload.perms
        text_changed = True

    if payload.path is not None and payload.path != (menu.path or ""):
        if payload.path:
            dup = db.scalar(
                select(Menu.id).where(Menu.path == payload.path, Menu.id != menu.id)
            )
            if dup is not None:
                raise HTTPException(
                    409, detail={"code": "MENU_PATH_TAKEN", "message": "路由路径已被占用"}
                )
        menu.path = payload.path
        text_changed = True

    if payload.status is not None and payload.status != menu.status:
        menu.status = payload.status
        status_changed = True

    for field in ("name", "component", "icon", "workspace", "sort_order", "is_visible"):
        value = getattr(payload, field)
        if value is not None:
            setattr(menu, field, value)
            text_changed = True

    db.commit()
    db.refresh(menu)

    # 只清缓存，**不踢人** —— 菜单启停是低频全站功能开关，不是授权变更
    if text_changed or status_changed:
        permission_cache.clear()

    return _serialize(menu)


@router.delete("/{menu_id}")
def delete_menu(
    menu_id: int,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:menu:delete")),
) -> dict:
    """删菜单。有子菜单或有授权引用 ⇒ 409。"""
    menu = _get_or_404(db, menu_id)

    children = db.scalar(
        select(func.count()).select_from(Menu).where(Menu.parent_id == menu.id)
    ) or 0
    if children:
        raise HTTPException(
            409,
            detail={
                "code": "MENU_HAS_CHILDREN",
                "message": f"还有 {children} 个子菜单，请先删除或移走",
                "child_count": children,
            },
        )

    granted = db.scalar(
        select(func.count()).select_from(RoleMenu).where(RoleMenu.menu_id == menu.id)
    ) or 0
    if granted:
        raise HTTPException(
            409,
            detail={
                "code": "MENU_IN_USE",
                "message": f"还有 {granted} 条角色授权引用它，请先在角色里取消授权",
                "grant_count": granted,
            },
        )

    db.delete(menu)
    db.commit()
    permission_cache.clear()
    return {"ok": True, "id": menu_id, "deleted": True}


@router.post("/reorder")
def reorder_menus(
    payload: MenuReorder,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:menu:edit")),
) -> dict:
    """批量改 `sort_order` / `parent_id`（拖拽排序）。"""
    updated = 0
    for item in payload.items:
        menu_id = item.get("id")
        if not isinstance(menu_id, int):
            continue
        menu = db.get(Menu, menu_id)
        if menu is None:
            continue
        if "parent_id" in item and isinstance(item["parent_id"], int):
            if item["parent_id"] != menu.parent_id:
                _assert_parent_ok(db, item["parent_id"], self_id=menu.id)
                menu.parent_id = item["parent_id"]
        if "sort_order" in item and isinstance(item["sort_order"], int):
            menu.sort_order = item["sort_order"]
        updated += 1

    db.commit()
    permission_cache.clear()
    return {"ok": True, "updated": updated}
