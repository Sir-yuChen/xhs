"""用户角色（RBAC 最小实现）。

## 为什么现在只需要两个角色

本系统的权限边界非常窄，只有**一处**真正需要区分身份：

* **全局级系统设置**（`scope=global` 的开关，如调度器总开关 `scheduler.enabled`）
  —— 这类设置影响所有用户，不能让任何登录用户随便改；
* 其余全部业务数据本来就按 `user_id` 隔离，**靠隔离而不靠角色**。

因此不引入完整的角色表 / 权限点体系：那会带来与收益不匹配的复杂度。
等真正出现「运营 / 审核 / 只读」这类分工需求时再升级（届时本模块是唯一改动点）。

## 与「账号 `sub_type`」的区别（易混）

* ``users.role`` —— **系统登录用户**的角色（本模块），控制后台管理权限；
* ``platform_accounts.sub_type`` —— **小红书账号**的协议链路（见 `core/account_types.py`）。

两者完全正交：一个管理员也可以绑定 pc 账号。不要混用。

## 引导（bootstrap）规则

迁移 `users.role` 时会把**最早创建的**那个用户提升为 ``admin``。
理由：迁移当下不存在任何管理员，若不提升，全局开关将永久无人可改；
选择「最早创建的用户」是因为它通常就是部署者本人。
"""

from __future__ import annotations

from typing import Any

#: 超级管理员（唯一，种子账号 `admin`）：权限由代码派生 `ALL_PERMS`，不查授权表。
ROLE_ADMIN = "admin"
#: 普通管理员（2026-10-26 用户口径「超级管理员，普通管理员，普通用户 内置三个角色」）：
#: **权限走 role_menus 授权**，默认给业务域全量（系统域管理除外）。
#: 与超级管理员的区别：超管的权限不受授权表影响（防锁死），普通管理员可被增删。
ROLE_MANAGER = "manager"
#: 普通用户：默认最小权限（只读可见部分业务页面）。
ROLE_USER = "user"

#: 全部已知角色（**内置三个**）。新增角色必须在此登记，并同步检查 `is_admin` 的判定语义。
ROLES: tuple[str, ...] = (ROLE_ADMIN, ROLE_MANAGER, ROLE_USER)

#: 默认角色：普通用户。**新的权限默认不开放**（最小权限原则）。
DEFAULT_ROLE = ROLE_USER


def normalize_role(role: Any) -> str:
    """把库中读出的 ``role`` 归一为合法取值。

    容忍 ``None`` / 空串（迁移前的历史记录）并按 :data:`DEFAULT_ROLE` 处理。
    **未知的非空取值同样降级为普通用户**——这里与 `account_types` 的策略相反：
    `sub_type` 是路由开关，猜错会打错域名，必须响亮失败；
    而 `role` 是权限开关，猜错只会「少给权限」，属于安全方向，
    因此选择保守降级而不是让整个请求 500。
    """
    if role is None:
        return DEFAULT_ROLE
    value = str(role).strip().lower()
    if value not in ROLES:
        return DEFAULT_ROLE
    return value


def is_admin(user: Any) -> bool:
    """判断用户是否为管理员。``None`` 用户（未登录）一律返回 False。"""
    if user is None:
        return False
    return normalize_role(getattr(user, "role", None)) == ROLE_ADMIN
