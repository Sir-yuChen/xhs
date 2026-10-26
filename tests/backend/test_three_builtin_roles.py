"""内置三角色守卫（2026-10-26 用户口径）。

> 用户原话：「超级管理员，普通管理员，普通用户 内置三个角色」

## 三个角色的定义

| 角色 | code | 权限来源 | 范围 |
|---|---|---|---|
| 超级管理员 | `admin` | **代码派生** `ALL_PERMS`（不查授权表，防锁死） | 全部 |
| 普通管理员 | `manager` | `role_menus` 授权 | **业务域全量 + 系统域只读** |
| 普通用户 | `user` | `role_menus` 授权 | 默认最小权限（只看页面，无按钮） |

三者都是**内置**（`is_system=True`）：不可删、不可改 code、不可停用，
但普通管理员/普通用户的**授权可以被调整**（这才是角色的意义）。

## 本守卫防的退化

本轮实测踩到一个**静默**错误：按 `Menu.workspace` 判「系统域」时，
149 个按钮行的 `workspace` 是**空串**（只有 C 行带值），于是系统域的写按钮
被当成业务域 ⇒ 普通管理员拿到了 `system:user:delete`、`system:role:create`。
接口不报错、界面也不报错，只是**权限给多了**（安全方向的反面）。

⇒ 归属必须沿**祖先链**判，且系统域只给「C 页面 + `:view` 按钮」。
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend" / "app"
SEED_SERVICE = BACKEND / "services" / "menu_seed_service.py"

#: 普通管理员在系统域里**允许**有的权限码（只读）。
MANAGER_SYSTEM_READ_ONLY = (
    "system:dict:view",
    "system:log:view",
    "system:menu:view",
    "system:role:view",
    "system:scheduler:view",
    "system:setting:view",
    "system:user:view",
)


def test_three_builtin_roles_registered():
    """① 三个角色常量都登记在 `ROLES` 里（否则 `normalize_role` 会把 manager 降级成 user）。"""
    src = (BACKEND / "core" / "roles.py").read_text(encoding="utf-8")
    assert 'ROLE_ADMIN = "admin"' in src
    assert 'ROLE_MANAGER = "manager"' in src
    assert 'ROLE_USER = "user"' in src
    m = re.search(r"ROLES:\s*tuple\[str,\s*\.\.\.\]\s*=\s*\(([^)]*)\)", src)
    assert m, "找不到 ROLES 常量"
    for role in ("ROLE_ADMIN", "ROLE_MANAGER", "ROLE_USER"):
        assert role in m.group(1), f"{role} 没登记进 ROLES ⇒ normalize_role 会把它降级成普通用户"


def test_three_builtin_roles_in_seed():
    """② 三个角色都要在种子表里，且都是内置（不可删）。"""
    src = SEED_SERVICE.read_text(encoding="utf-8")
    start = src.index("ROLE_SEEDS:")
    end = src.index("\n)\n", start)
    body = src[start:end]
    for code, name in (("ROLE_ADMIN", "超级管理员"), ("ROLE_MANAGER", "普通管理员"), ("ROLE_USER", "普通用户")):
        assert code in body, f"角色种子缺 {code}"
        assert name in body, f"角色种子缺名称「{name}」"
    assert body.count('"is_system": True') == 3, "内置角色必须都是 is_system=True（不可删）"


def test_manager_uses_ancestor_workspace_not_own_column():
    """③ 判归属必须走祖先链 —— 行自己的 `workspace` 列在 F 行上大多是空串。"""
    src = SEED_SERVICE.read_text(encoding="utf-8")
    start = src.index("manager_menu_ids")
    body = src[start : start + 3000]
    assert "workspace_of(" in body or "def workspace_of" in src, (
        "manager 授权集合没有沿祖先链判归属 ⇒ 系统域的写按钮会被当成业务域"
    )
    #: 直接读 row.workspace 的写法应该只出现在 workspace_of 内部
    assert body.count("row.workspace ==") == 0, (
        "manager 授权里仍在直接比较 row.workspace（F 行是空串，会漏判）"
    )


def test_manager_system_domain_is_read_only():
    """④ 系统域只给「C 页面 + :view 按钮」，不给任何写码。"""
    src = SEED_SERVICE.read_text(encoding="utf-8")
    start = src.index("manager_menu_ids")
    body = src[start : start + 3000]
    assert ':view' in body, "manager 的系统域过滤里没有「只给 view」的判据"
    assert 'row.menu_type == "C" or' in body, "manager 的系统域没有放行 C 页面行"


def test_admin_does_not_depend_on_grant_table():
    """⑤ 超级管理员的权限由代码派生 —— 授权表被清空也不能把他锁在门外。"""
    src = (BACKEND / "core" / "perms.py").read_text(encoding="utf-8")
    start = src.index("def role_perms(")
    body = src[start : start + 1200]
    assert "ROLE_ADMIN" in body and "ALL_PERMS" in body, (
        "role_perms 里 admin 没有直接返回 ALL_PERMS（防锁死的核心判据）"
    )
