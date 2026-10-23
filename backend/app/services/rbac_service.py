"""RBAC 服务 —— 「角色 → 权限码」的**唯一**解析实现（设计 50 §3.2 / 57 S2）。

## 这一层为什么存在

`core/perms.py` 是 L0（纯函数 + 静态登记表），它**故意不 import 模型层** ——
否则任何用到权限的地方都会被拖进 ORM 依赖。代价是「角色到底有哪些码」
必须由外面注入：`perms.role_perms(role, db=resolver)`。

本模块就是那个 resolver 的产地，也是**唯一**允许写
`role_menus ⋈ menus` 这条 SQL 的地方。别处再写一份就是第二套真相。

## 一条铁律：admin 不查库

`admin` 的权限集由代码派生（`perms.ALL_PERMS`），**绝不查 `role_menus`**。

理由不是性能，是**防锁死**：管理员能在界面上给自己「取消授权」，
一旦授权表是唯一来源，点错一次就永久失去管理入口，只能改库救回来。
所以「超管」是一个**代码事实**，不是一条数据。

⚠️ 别把它「优化」成查库（例如为了统计「实际授了多少条」）—— 那会把
防锁死换成一次统计上的便利，不划算。要统计就单独写只读查询。

## 缓存不在这里

本模块每次调用都查库（正确但慢）。**缓存**由 `permission_cache` 负责，
它在写路径上失效、在热路径上命中。两者分开是为了让「改权限」这件事
只有一个入口可以忘记清缓存 —— 而不是两处。

## 2026-10-24 实测补充：`user_perms` 的 scope

`role_menus` 带 `user_id`（授权按用户隔离，S1 决策），所以解析某用户的权限时
必须**同时**按 `user_id` 过滤：`role_menus.user_id == user.id`。

* `user_id IS NULL` 的行是**内置角色模板**（`is_system=true` 的角色）；
* 运行时只读**该用户自己的行 + 模板行**（模板行给出「新用户默认看得见什么」，
  用户自己的行是之后显式授予的增量）。

⚠️ 实测踩坑：只按 `role_id` 过滤会把 A 用户的自定义授权漏给 B 用户
（同一角色 code 全局唯一，但授权行 per-user），那是最隐蔽的越权。
"""

from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.perms import ALL_PERMS, ROLE_ADMIN, normalize_role
from backend.app.models.menu import Menu
from backend.app.models.role import Role
from backend.app.models.role_menu import RoleMenu
from backend.app.models.user import User

__all__ = [
    "perms_for_role",
    "perms_for_user",
    "menu_ids_for_role",
    "menu_ids_for_user",
    "role_by_code",
    "set_role_menus",
    "clear_permission_cache",
]


def role_by_code(db: Session, code: str) -> Optional[Role]:
    """按 code 取角色（`code` 有唯一索引）。"""
    return db.execute(select(Role).where(Role.code == code)).scalar_one_or_none()


def perms_for_role(db: Session, role_code: str | None) -> frozenset[str]:
    """某角色（模板行，`user_id IS NULL`）拥有的权限码。

    * `admin` ⇒ `ALL_PERMS`（不查库，见模块 docstring）；
    * 未知角色 ⇒ 空集。
    """
    normalized = normalize_role(role_code)
    if normalized == ROLE_ADMIN:
        return ALL_PERMS
    role = role_by_code(db, normalized)
    if role is None:
        return frozenset()

    rows = db.execute(
        select(Menu.perms)
        .join(RoleMenu, RoleMenu.menu_id == Menu.id)
        .where(RoleMenu.role_id == role.id, RoleMenu.user_id.is_(None))
    ).scalars()
    # 只保留登记表里存在的码：库里可能留着已被退场的旧码（历史授权），
    # 放进来会让 has_perm 的「未登记一律 False」形同虚设。
    return frozenset(code for code in rows if code and code in ALL_PERMS)


def perms_for_user(db: Session, user: User | None) -> frozenset[str]:
    """某用户的**有效**权限码（模板行 ∪ 他自己的授权行）。"""
    if user is None:
        return frozenset()
    normalized = normalize_role(getattr(user, "role", None))
    if normalized == ROLE_ADMIN:
        return ALL_PERMS
    role = role_by_code(db, normalized)
    if role is None:
        return frozenset()

    rows = db.execute(
        select(Menu.perms)
        .join(RoleMenu, RoleMenu.menu_id == Menu.id)
        .where(
            RoleMenu.role_id == role.id,
            # 模板行（全站默认）或该用户自己的行 —— 缺 user_id 过滤会串号
            (RoleMenu.user_id.is_(None)) | (RoleMenu.user_id == user.id),
        )
    ).scalars()
    return frozenset(code for code in rows if code and code in ALL_PERMS)


def menu_ids_for_role(db: Session, role_code: str | None) -> list[int]:
    """角色被授权的菜单 id（侧栏树用；`admin` 返回**全部**菜单 id）。"""
    role = role_by_code(db, normalize_role(role_code))
    if role is None:
        return []
    return list(
        db.execute(
            select(RoleMenu.menu_id).where(
                RoleMenu.role_id == role.id, RoleMenu.user_id.is_(None)
            )
        ).scalars()
    )


def menu_ids_for_user(db: Session, user: User | None) -> list[int]:
    """用户可见的菜单 id。`admin` ⇒ 全部（含已停用的？**不含** —— 停用的不该显示）。"""
    if user is None:
        return []
    if normalize_role(getattr(user, "role", None)) == ROLE_ADMIN:
        return list(db.execute(select(Menu.id).where(Menu.status == "normal")).scalars())
    role = role_by_code(db, normalize_role(getattr(user, "role", None)))
    if role is None:
        return []
    return list(
        db.execute(
            select(RoleMenu.menu_id).where(
                RoleMenu.role_id == role.id,
                (RoleMenu.user_id.is_(None)) | (RoleMenu.user_id == user.id),
            )
        ).scalars()
    )


def set_role_menus(
    db: Session,
    role_id: int,
    menu_ids: Iterable[int],
    *,
    user_id: int | None = None,
) -> int:
    """整体替换某角色（在某用户范围内）的授权集合，返回新授权条数。

    **整体替换**而不是增量 diff：授权是「一组集合」语义，diff 出来的中间态
    （先删后加、部分失败）会让同一角色在不同时间点看到不同菜单，
    排查时无法复现。一次删干净再整批插入，失败整体回滚。

    ⚠️ 调用方负责 `db.commit()` 与**清缓存**（`clear_permission_cache`）。
    本函数不动缓存 —— 它可能在事务里被调用，此时清缓存会让并发读者读到旧库。
    """
    db.query(RoleMenu).filter(
        RoleMenu.role_id == role_id,
        RoleMenu.user_id.is_(None) if user_id is None else RoleMenu.user_id == user_id,
    ).delete(synchronize_session=False)

    unique_ids = sorted({int(mid) for mid in menu_ids})
    if not unique_ids:
        return 0
    db.bulk_save_objects(
        [RoleMenu(role_id=role_id, menu_id=mid, user_id=user_id) for mid in unique_ids]
    )
    return len(unique_ids)


def clear_permission_cache() -> None:
    """清权限缓存 —— 委托给 `permission_cache`（避免两处各写一份失效逻辑）。"""
    from backend.app.services import permission_cache

    permission_cache.clear()
