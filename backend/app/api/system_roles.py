"""系统角色管理端点（设计 55 §3 / 设计 49 模块二）。

| 方法 | 路径 | 权限码 |
|---|---|---|
| `GET`  | `/api/system/roles` | `system:role:view` |
| `POST` | `/api/system/roles` | `system:role:create` |
| `PUT`  | `/api/system/roles/{role_id}` | `system:role:edit` |
| `DELETE` | `/api/system/roles/{role_id}` | `system:role:delete` |
| `GET`  | `/api/system/roles/{role_id}/menus` | `system:role:view` |
| `PUT`  | `/api/system/roles/{role_id}/menus` | `system:role:assign` |
| `GET`  | `/api/system/roles/{role_id}/users` | `system:role:view` |
| `PUT`  | `/api/system/roles/{role_id}/users` | `system:role:assign` |

## 内置角色保护（`is_system`）

`admin` / `user` 是代码依赖的角色（`core/roles.py` 的 `ROLES` 常量、
`normalize_role` 的降级目标、种子播种的目标）。对它们的限制：

* **不许删**（删了 `normalize_role` 的降级目标就不存在了）；
* **不许停用**（停用一个内置角色 = 把一整类用户锁在门外，而唯一能改回来的
  入口需要权限 —— 与「admin 不查授权表」是同一个防锁死思路）；
* **不许改 `code`**（代码里按 `code` 引用，改了等于把整类用户的权限映射断掉）。

`name` / `description` / `sort_order` **可以改** —— 那是显示层的东西。

> ⚠️ dataPanel 的 `UpdateRole` **漏了 `is_system` 拦截**（实测发现：
> 只拦 delete 不拦 update），本项目在 update 上也拦。

## 授权变更 ⇒ 踢人

`PUT /roles/{id}/menus` 与 `PUT /roles/{id}/users` 都会改变「谁能做什么」，
所以要对**受影响的用户**抬 `token_version`。不踢的后果：用户手上的旧 JWT
按旧权限继续干活，最长 30 分钟。

「受影响」的判定：角色授权变了 ⇒ 该角色的**所有**持有人；
角色成员变了 ⇒ 只对被移除的人（被加入的人本来就没有该角色的权限，
抬版本只是让他重新登录，属于多余打扰）。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.perm_deps import require_perm
from backend.app.core.roles import ROLES, normalize_role
from backend.app.core.time import shanghai_now
from backend.app.models.menu import Menu
from backend.app.models.role import Role
from backend.app.models.user import User
from backend.app.core import permission_cache, rbac_service, presence

router = APIRouter(prefix="/system/roles", tags=["system"])

ROLE_CODE_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"


class RoleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=64)
    code: str = Field(min_length=2, max_length=32, pattern=ROLE_CODE_PATTERN)
    description: str = Field(default="", max_length=255)
    sort_order: int = 0


class RoleUpdate(BaseModel):
    """`code` **不在**可改字段里 —— 代码按 code 引用，改了映射就断。"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=2, max_length=64)
    description: Optional[str] = Field(default=None, max_length=255)
    status: Optional[str] = Field(default=None, pattern="^(normal|disabled)$")
    sort_order: Optional[int] = None


class RoleMenusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    menu_ids: list[int] = Field(default_factory=list)


class RoleUsersUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_ids: list[int] = Field(default_factory=list)


def _serialize_role(role: Role, *, user_count: int, menu_count: int) -> dict:
    return {
        "id": role.id,
        "name": role.name,
        "code": role.code,
        "description": getattr(role, "description", "") or "",
        "is_system": bool(role.is_system),
        "status": role.status,
        "sort_order": role.sort_order,
        "user_count": user_count,
        "menu_count": menu_count,
        "created_at": role.created_at.isoformat() if role.created_at else None,
    }


def _counts(db: Session) -> tuple[dict[int, int], dict[int, int]]:
    """一次查出每个角色的用户数与**菜单数**（避免 N+1）。

    ⚠️ **菜单数只数模板行**（`user_id IS NULL`）—— 2026-10-24 实测踩坑：
    初版按 `role_id` 全量分组，把「模板行 + 每个持有者的用户行」都算进去，
    `admin` 显示 522（= 174 × 3：模板 + admin + zy晨）、`user` 显示 936（= 36 × 26）。
    那个数字不是「这角色有几个菜单」，而是「有多少条授权记录」——
    界面上「已授权 N 个菜单」会被理解成前者。
    """
    from backend.app.models.role_menu import RoleMenu

    users = dict(
        db.execute(
            select(User.role, func.count())
            .where(User.deleted_at.is_(None))
            .group_by(User.role)
        ).all()
    )
    menus = dict(
        db.execute(
            select(RoleMenu.role_id, func.count())
            .where(RoleMenu.user_id.is_(None))
            .group_by(RoleMenu.role_id)
        ).all()
    )
    return users, menus


def _get_role_or_404(db: Session, role_id: int) -> Role:
    role = db.get(Role, role_id)
    if role is None:
        raise HTTPException(404, detail={"code": "ROLE_NOT_FOUND", "message": "角色不存在"})
    return role


def _assert_not_system(role: Role, action: str) -> None:
    if role.is_system:
        raise HTTPException(
            400,
            detail={
                "code": "ROLE_SYSTEM_PROTECTED",
                "message": f"内置角色不允许{action}",
            },
        )


def _kick_role_holders(db: Session, role_code: str) -> int:
    """让持有该角色的用户**重新登录**（授权变更后旧令牌必须失效）。

    ⚠️ **`admin` 角色不踢**（2026-10-24 实测踩坑）：
    admin 的权限由代码派生（`ALL_PERMS`），改 `role_menus` 对他**毫无影响**；
    而踢掉他的后果是「管理员一保存授权就被踢下线」，第二次请求 401 ——
    界面上表现为「保存成功但页面立刻跳登录页」，实测就是这么红的
    （`test_admin_never_blocked_by_permission_codes` 报 `Token has been revoked`）。

    这是「防锁死」的又一面：**对权限不受影响的人，不要动他的令牌**。
    """
    from backend.app.core.roles import ROLE_ADMIN

    if normalize_role(role_code) == ROLE_ADMIN:
        return 0

    affected = db.scalars(
        select(User).where(User.role == role_code, User.deleted_at.is_(None))
    ).all()
    for user in affected:
        user.token_version = int(user.token_version or 0) + 1
        permission_cache.clear(user_id=user.id)
        presence.forget(user.id)
    return len(affected)


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #
@router.get("")
def list_roles(
    *, db: Session = Depends(get_db), _: User = Depends(require_perm("system:role:view"))
) -> dict:
    """角色列表（带用户数 / 菜单数）。"""
    roles = db.scalars(select(Role).order_by(Role.sort_order, Role.id)).all()
    user_counts, menu_counts = _counts(db)
    return {
        "items": [
            _serialize_role(
                r,
                user_count=user_counts.get(r.code, 0),
                menu_count=menu_counts.get(r.id, 0),
            )
            for r in roles
        ]
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_role(
    payload: RoleCreate,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:role:create")),
) -> dict:
    """新建角色。`code` 全局唯一。"""
    if payload.code in ROLES:
        raise HTTPException(
            409,
            detail={"code": "ROLE_CODE_RESERVED", "message": f"{payload.code} 是内置角色 code"},
        )
    if rbac_service.role_by_code(db, payload.code) is not None:
        raise HTTPException(
            409, detail={"code": "ROLE_CODE_TAKEN", "message": "角色 code 已存在"}
        )

    role = Role(
        name=payload.name,
        code=payload.code,
        description=payload.description,
        is_system=False,
        status="normal",
        sort_order=payload.sort_order,
        created_at=shanghai_now(),
    )
    db.add(role)
    db.commit()
    db.refresh(role)
    return _serialize_role(role, user_count=0, menu_count=0)


@router.put("/{role_id}")
def update_role(
    role_id: int,
    payload: RoleUpdate,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:role:edit")),
) -> dict:
    """更新角色。内置角色**不许停用**，但可以改显示名。"""
    role = _get_role_or_404(db, role_id)
    kicked = 0

    if payload.status is not None and payload.status != role.status:
        # 内置角色不许停用（防锁死，见模块 docstring）
        _assert_not_system(role, "停用")
        role.status = payload.status
        # 停用角色 ⇒ 该角色用户的有效权限集变了 ⇒ 踢人
        kicked = _kick_role_holders(db, role.code)

    for field in ("name", "description", "sort_order"):
        value = getattr(payload, field)
        if value is not None:
            setattr(role, field, value)

    db.commit()
    db.refresh(role)
    user_counts, menu_counts = _counts(db)
    data = _serialize_role(
        role,
        user_count=user_counts.get(role.code, 0),
        menu_count=menu_counts.get(role.id, 0),
    )
    data["kicked_users"] = kicked
    return data


@router.delete("/{role_id}")
def delete_role(
    role_id: int,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:role:delete")),
) -> dict:
    """删角色。内置角色不许删；**有用户持有时也不许删**（否则那批人没有角色）。"""
    role = _get_role_or_404(db, role_id)
    _assert_not_system(role, "删除")

    holders = db.scalar(
        select(func.count()).select_from(User).where(
            User.role == role.code, User.deleted_at.is_(None)
        )
    ) or 0
    if holders:
        raise HTTPException(
            409,
            detail={
                "code": "ROLE_IN_USE",
                "message": f"还有 {holders} 个用户使用该角色，请先调整他们的角色",
                "holder_count": holders,
            },
        )

    db.delete(role)  # role_menus 走 CASCADE（设计 47：删角色 ⇒ 授权无意义）
    db.commit()
    permission_cache.clear()
    return {"ok": True, "id": role_id, "deleted": True}


@router.get("/{role_id}/menus")
def get_role_menus(
    role_id: int,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:role:view")),
) -> dict:
    """该角色当前被授权的菜单 id（模板行，不含某用户的增量）。"""
    role = _get_role_or_404(db, role_id)
    ids = rbac_service.menu_ids_for_role(db, role.code)
    return {"role_id": role.id, "role_code": role.code, "menu_ids": sorted(ids)}


@router.put("/{role_id}/menus")
def set_role_menus(
    role_id: int,
    payload: RoleMenusUpdate,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:role:assignMenu")),
) -> dict:
    """整体替换角色授权。**会踢掉该角色的所有用户**。"""
    role = _get_role_or_404(db, role_id)

    # 校验 menu_id 都存在（防止前端传脏 id 静默丢授权）
    if payload.menu_ids:
        existing = set(
            db.scalars(select(Menu.id).where(Menu.id.in_(payload.menu_ids))).all()
        )
        missing = sorted(set(payload.menu_ids) - existing)
        if missing:
            raise HTTPException(
                400,
                detail={
                    "code": "MENU_NOT_FOUND",
                    "message": f"菜单不存在：{missing[:10]}",
                },
            )

    granted = rbac_service.set_role_menus(db, role.id, payload.menu_ids)
    kicked = _kick_role_holders(db, role.code)
    db.commit()

    return {
        "ok": True,
        "role_id": role.id,
        "granted": granted,
        "kicked_users": kicked,
    }


@router.get("/{role_id}/users")
def get_role_users(
    role_id: int,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:role:view")),
) -> dict:
    """持有该角色的用户。"""
    role = _get_role_or_404(db, role_id)
    users = db.scalars(
        select(User)
        .where(User.role == role.code, User.deleted_at.is_(None))
        .order_by(User.id)
    ).all()
    return {
        "role_id": role.id,
        "role_code": role.code,
        "items": [
            {
                "id": u.id,
                "username": u.username,
                "real_name": getattr(u, "real_name", "") or "",
                "status": getattr(u, "status", "normal"),
            }
            for u in users
        ],
    }


@router.put("/{role_id}/users")
def set_role_users(
    role_id: int,
    payload: RoleUsersUpdate,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:role:assignMenu")),
) -> dict:
    """把一批用户设为该角色。

    **只做加**（把 `user_ids` 里的人都改成该角色），不把别人移出该角色 ——
    因为 `users.role` 是单值，「移出」等于「改成别的角色」，需要知道改成哪个。
    移除走 `PUT /system/users/{id}` 改 role。

    ⇒ 与 `set_role_menus` 的「整体替换」语义**故意不同**：
    菜单是一组集合（可以整体替换），角色是单人单值（替换会误伤）。
    """
    role = _get_role_or_404(db, role_id)
    if role.status != "normal":
        raise HTTPException(
            400, detail={"code": "ROLE_DISABLED", "message": "角色已停用，不能分配"}
        )

    # 内置 admin 角色不给通过这个端点批量塞人（防「一键把所有用户变成管理员」）
    from backend.app.core.roles import ROLE_ADMIN

    if role.code == ROLE_ADMIN:
        raise HTTPException(
            400,
            detail={
                "code": "ROLE_ADMIN_BATCH_FORBIDDEN",
                "message": "超级管理员只能逐个指定，不支持批量分配",
            },
        )

    users = db.scalars(
        select(User).where(User.id.in_(payload.user_ids), User.deleted_at.is_(None))
    ).all()
    found = {u.id for u in users}
    missing = sorted(set(payload.user_ids) - found)
    if missing:
        raise HTTPException(
            404,
            detail={"code": "USER_NOT_FOUND", "message": f"用户不存在：{missing[:10]}"},
        )

    kicked = 0
    for user in users:
        if user.role != role.code:
            user.role = role.code
            user.token_version = int(user.token_version or 0) + 1
            permission_cache.clear(user_id=user.id)
            presence.forget(user.id)
            kicked += 1
    db.commit()
    return {"ok": True, "role_id": role.id, "assigned": len(users), "kicked_users": kicked}
