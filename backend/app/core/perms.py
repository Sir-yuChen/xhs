"""权限判定 —— **全仓唯一实现**（设计 50 §2.1）。

## 铁律

**任何模块都不许自己写 `role == "admin"`。** 判定只走这里，理由是它比
「每个调用点各自判角色」多三件必须的事：

1. **admin 由代码派生**（`role_perms("admin")` 直接返回 `ALL_PERMS`，不查
   `role_menus`）。若 admin 靠 role_menus 里有那 161 行才有权限，
   一次误点「分配菜单」把某些行取消，超级管理员就把自己锁在门外了 ——
   **且再也改不回来**（改权限的入口本身需要权限）。
2. **`ALL_PERMS` 只增不减**：新增一条码，admin 自动有，不需要补种子。
3. **未知角色 = 零权限**：`role_perms("viewer")` 返回空集而不是猜一个。

## 缓存口径（S2 落地，本段只留接缝）

本模块是**纯函数层**（L0）：不 import 服务层、不 import 模型层。
`role_perms(..., db=...)` 已经是「查库」的唯一入口，S2 的
`services.permission_cache` 在它外面套 `(user_id, token_version)` 缓存 ——
抬版本自动失效，本文件不需要知道缓存存在。

`user_perms` / `has_perm` 只读 `user.role`，**不发查询**；它们把「角色 → 码」
委托给 `role_perms`，因此 S2 换缓存实现时这两个函数零改动。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from backend.app.core.perms_registry import (  # noqa: F401
    BY_CODE,
    CODES,
    PERMS,
    SEP,
    PermSpec,
)
from backend.app.core.roles import ROLE_ADMIN, normalize_role

#: 权限码分隔符 —— **唯一**（前端 `"system:user:view"` 必须用同一个）
PERM_SEP = ":"

#: 合法码格式（设计 50 §2.1 原文）。注意 action 段允许内部大写（`resetPassword`），
#: 但**首字母必须是小写或大写字母**、后续只能字母数字 —— 下划线只出现在 domain 段
#: （`forbiddenWord` / `autoops` 这类不用下划线，`xhs_record` 也不存在）。
PERM_PATTERN = re.compile(r"^[a-z][a-z0-9_]*:[a-zA-Z][a-zA-Z0-9]*(:[a-zA-Z][a-zA-Z0-9]*)?$")

#: 权限码全集（编译期常量）。**只增不减** —— 改它就是改需求口径（设计 49 附 A）。
ALL_PERMS: frozenset[str] = CODES

#: 便于报错时给出人话（`system:user:delete` ⇒ 「删除用户」）
PERM_LABELS: dict[str, str] = {spec.code: spec.label for spec in PERMS}


def make_perm(domain: str, resource: str, action: str = "") -> str:
    """拼权限码。**唯一拼接点** —— 直接写 `f"{a}:{b}:{c}"` 会漏掉空段校验。

    >>> make_perm("system", "user", "view")
    'system:user:view'
    >>> make_perm("system", "log")          # 只有查看、没有按钮时允许两段
    'system:log'
    """
    parts = [p for p in (domain, resource, action) if p]
    if not parts:
        raise ValueError("domain 不能为空")
    return PERM_SEP.join(parts)


def parse_perm(code: str) -> tuple[str, str, str] | None:
    """拆权限码为 `(domain, resource, action)`；两段码的 action 返回 `""`。

    非法码返回 `None`（**不抛**）：调用方常需要对用户提交的码做「宽松解析」，
    抛异常会让一个错误输入变成 500。
    """
    if not isinstance(code, str):
        return None
    text = code.strip()
    if not is_valid_perm(text):
        return None
    parts = text.split(PERM_SEP)
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return parts[0], parts[1], parts[2]


def is_valid_perm(code: Any) -> bool:
    """格式校验（只校格式，**不校是否已登记** —— 那由启动守卫做，见 `route_guard`）。"""
    return isinstance(code, str) and bool(PERM_PATTERN.match(code.strip()))


def is_registered(code: str) -> bool:
    """码是否在 161 条登记表里。写错一个字母 ⇒ `False`（启动守卫据此拦住）。"""
    return code in BY_CODE


def describe(code: str) -> PermSpec | None:
    """取登记信息（中文名 / 所属页 / 工作区），未登记返回 `None`。"""
    return BY_CODE.get(code)


def role_perms(role_code: str | None, *, db: Any = None) -> frozenset[str]:
    """某角色拥有的权限码。

    * ``admin`` ⇒ **直接返回 `ALL_PERMS`，不查 `role_menus`**（见模块 docstring 第 1 条）；
    * 其他已知角色 ⇒ `user` 当前 = 只读角色，查库后返回授权页的 `menus.perms`。
      为了保持本模块（L0）不依赖模型层，**查库动作委托给注入的 `db`**：
      S2 的 `rbac_service` 会把 `role_menus ⋈ menus.perms` 的结果传进来；
      本段（S0）拿不到 db 时返回空集（保守：不给权限）。
    * 未知角色 ⇒ 空集。

    :param db: 可选的「角色 → 码集合」解析器。传 `Session` 时由 S2 提供的
        适配函数解析；传可调用对象时直接调用。本段（S0）两种都还没落地。
    """
    normalized = normalize_role(role_code)
    if normalized == ROLE_ADMIN:
        return ALL_PERMS
    if db is None:
        return frozenset()
    resolver = getattr(db, "perms_for_role", None)
    if callable(resolver):
        return frozenset(resolver(normalized) or ())
    if callable(db):
        return frozenset(db(normalized) or ())
    # 传了 Session 但 S2 的解析器还没接上 —— 保守给空集，绝不「猜成全量」
    return frozenset()


def user_perms(user: Any) -> frozenset[str]:
    """该用户的有效权限码。``None``（未登录）⇒ 空集。"""
    if user is None:
        return frozenset()
    return role_perms(getattr(user, "role", None))


def has_perm(user: Any, code: str) -> bool:
    """是否拥有**某一条**权限。未登记的码一律 `False`（写错字母不会意外放行）。"""
    if user is None or not code:
        return False
    if code not in ALL_PERMS:
        return False
    return code in user_perms(user)


def has_any(user: Any, codes: Iterable[str]) -> bool:
    """拥有其中**任意一条**即可（侧栏过滤 / `hasAnyPermission` 用）。"""
    return any(has_perm(user, code) for code in codes)


def has_all(user: Any, codes: Iterable[str]) -> bool:
    """必须**全部**拥有（极少数组合操作）。"""
    return all(has_perm(user, code) for code in codes)


def assert_registry_sane() -> None:
    """登记表自检 —— 启动早期调用一次（`main.py` 的 lifespan 里）。

    查四件事（任何一条不成立都是**代码级错误**，不是用户输入问题，故直接 raise）：
    1. 每条码格式合法；
    2. 每条 F 行能挂到某个 C 行（`menu_parent_path` 存在）。挂不上的表现是那行权限
       在角色分配界面上**看不见**，等于永远授不了；
    3. 没有重复码（重复会让 `menus.perms` 唯一索引在播种时炸）；
    4. 每个 F 行前缀都有对应的 view 码（`<域>:<资源>:view`）—— 「每页必有一条查看码」
       的落地判据；否则那条页面对没被授任何码的用户**开着**。
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    bad_format: list[str] = []
    c_paths = {spec.parent_path for spec in PERMS if spec.menu_type == "C"}
    view_prefixes = {spec.code.rsplit(SEP, 1)[0] for spec in PERMS if spec.code.endswith(":view")}

    for spec in PERMS:
        if spec.code in seen:
            duplicates.append(spec.code)
        seen.add(spec.code)
        if not is_valid_perm(spec.code):
            bad_format.append(spec.code)
        if spec.menu_type not in ("C", "F"):
            raise ValueError(f"权限码 {spec.code} 的 menu_type 只能是 C 或 F，实际 {spec.menu_type!r}")

    if duplicates:
        raise ValueError(f"权限码重复登记：{sorted(set(duplicates))}")
    if bad_format:
        raise ValueError(f"权限码格式非法：{sorted(bad_format)}")

    orphans = sorted(
        spec.code for spec in PERMS if spec.menu_type == "F" and spec.menu_parent_path not in c_paths
    )
    if orphans:
        raise ValueError(f"这些按钮码挂不到任何页面行（parent_path 不存在）：{orphans}")

    no_view = sorted(
        {
            spec.code.rsplit(SEP, 1)[0]
            for spec in PERMS
            if spec.menu_type == "F"
            # 借宿别人的页（素材库 / 提示词 / 生成 / 数据包规则 …）用的是**宿主页**的
            # view 码，不该再要一条自己的 —— 否则「每页一条查看码」会变成「每个资源一条」
            and not spec.host_path
            and spec.code.rsplit(SEP, 1)[0] not in view_prefixes
        }
    )
    if no_view:
        raise ValueError(f"这些资源有按钮码但没有查看码 <域>:<资源>:view：{no_view}")
