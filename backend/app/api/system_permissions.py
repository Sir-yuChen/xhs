"""当前用户的权限码端点（设计 55 §6）。

| 方法 | 路径 | 权限码 |
|---|---|---|
| `GET` | `/api/system/permissions/mine` | **登录即可**（无权限码） |

## 为什么不挂权限码

它是**登录后第一件事**：前端拿它渲染按钮可见性（`hasPermission`）。
如果给它挂 `system:role:view` 之类的码，那么一个只有业务权限的用户
一登录就 403，连自己能点的按钮都算不出来 —— 那是死锁。

它的「过滤」就是权限判定本身：返回的集合已经是该用户有权使用的部分。

## 为什么单独一个文件而不是塞进 system_logs.py

路由前缀不同（这里是 `/system`，logs 是 `/system/logs` 的一部分），
且它与日志是两件无关的事。放在 `system_logs.py` 里只因为当初一起写的，
属于「顺手放」，现已独立。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.perms import ROLE_ADMIN, normalize_role
from backend.app.models.user import User
from backend.app.core import permission_cache
from backend.app.schemas.system import PermissionsMineOut

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/permissions/mine", response_model=PermissionsMineOut)
def my_permissions(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """当前用户的权限码集合（按钮可见性数据源）。**登录即可**。"""
    codes = sorted(permission_cache.perms_of(db, current_user))
    return {
        "role": normalize_role(current_user.role),
        "is_admin": normalize_role(current_user.role) == ROLE_ADMIN,
        "permissions": codes,
        "total": len(codes),
    }
