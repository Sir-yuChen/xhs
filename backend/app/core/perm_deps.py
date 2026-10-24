"""权限依赖 —— `Depends(require_perm("域:资源:动作"))`（设计 50 §3.4 / 57 S2）。

## 为什么是「依赖」而不是「中间件」

中间件拿不到 FastAPI 解析后的路径参数，而本项目的权限码**与具体资源实例无关**
（「能不能改商品」不取决于 `item_id` 是多少）。用依赖的好处：

1. `Depends(require_perm("xhs:item:edit"))` 写在函数签名里 ⇒ **一眼看到这个端点要什么码**，
   `route_perms.py` 也能静态扫出来做启动期守卫；
2. 可复用 `get_current_user`（已经处理过 401 / token_version）；
3. 不需要维护「路径 → 权限码」的正则表（第二份真相）。

## 三种拒绝路径必须能区分

| 情况 | 状态码 | 错误码 | 前端动作 |
|---|---|---|---|
| 未登录 / 令牌过期 | 401 | （由 `get_current_user` 给） | 跳登录页 |
| 已登录但无此码 | 403 | `PERMISSION_DENIED` | 提示 + 不跳登录 |
| 码写错了（未登记） | 500 | `PERM_UNREGISTERED` | **服务端 bug**，要炸出来 |

第三种尤其重要：如果写错码就静默 403，那么这个端点等于**对所有人关闭**，
排查时只会看到「管理员也点不了」。用 500 把它变成显式故障。

## 2026-10-24 实测补充

`require_perm` 返回的依赖函数带 `__perm_code__` 属性 —— `route_perms.py` 的
启动期扫描靠它把「路由 → 码」的对应关系反射出来，供 `assert_every_route_guarded`
检查「有没有写路由忘了挂码」。**不要改名**，那边按这个属性名读。
"""

from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.perms import ALL_PERMS, ROLE_ADMIN, normalize_role
from backend.app.models.user import User

__all__ = ["require_perm", "require_any_perm", "require_admin_user"]


def _deny(code: str, reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "PERMISSION_DENIED", "message": reason, "perm": code},
    )


def require_perm(code: str) -> Callable[..., User]:
    """要求当前用户拥有 `code`。返回 FastAPI 依赖可调用对象。"""
    if not code or code not in ALL_PERMS:
        # 编程错误，在**构造依赖时**（import 期）就炸，而不是等请求到来
        raise RuntimeError(
            f"require_perm({code!r}) 引用了未登记的权限码 —— "
            "权限码必须先在 core/perms_registry.py 登记（启动守卫会拦）"
        )

    def dependency(
        current_user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        from backend.app.core import permission_cache

        role = normalize_role(getattr(current_user, "role", None))
        if role == ROLE_ADMIN:
            return current_user
        if code in permission_cache.perms_of(db, current_user):
            return current_user
        raise _deny(code, "没有操作权限")

    dependency.__perm_code__ = code  # type: ignore[attr-defined]
    dependency.__name__ = f"require_perm_{code.replace(':', '_')}"
    return dependency


def require_any_perm(*codes: str) -> Callable[..., User]:
    """任一条即可（一个端点承担多语义时用，例如「批量」与「行内」共用处理器）。"""
    if not codes:
        raise RuntimeError("require_any_perm() 至少要一个权限码")
    for code in codes:
        if code not in ALL_PERMS:
            raise RuntimeError(f"require_any_perm({code!r}) 引用了未登记的权限码")

    def dependency(
        current_user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        from backend.app.core import permission_cache

        role = normalize_role(getattr(current_user, "role", None))
        if role == ROLE_ADMIN:
            return current_user
        owned = permission_cache.perms_of(db, current_user)
        if any(code in owned for code in codes):
            return current_user
        raise _deny(",".join(codes), "没有操作权限")

    dependency.__perm_code__ = codes[0]  # type: ignore[attr-defined]
    dependency.__perm_codes__ = codes  # type: ignore[attr-defined]
    dependency.__name__ = "require_any_perm_" + "_or_".join(
        c.replace(":", "_") for c in codes
    )
    return dependency


def require_admin_user(current_user: User = Depends(get_current_user)) -> User:
    """仅 admin 角色（**不是**权限码）。

    只给「系统管理域自身」的少数场景用（例如「一键应用到所有用户」这类
    影响全站数据且不该被授予任何普通角色的操作）。业务端一律用权限码。
    """
    if normalize_role(getattr(current_user, "role", None)) != ROLE_ADMIN:
        raise _deny("*", "需要超级管理员")
    return current_user
