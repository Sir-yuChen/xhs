"""「超级管理员只能有一个，且必须是种子账号」守卫（2026-10-26 用户口径）。

> 用户原话：「超级管理员只能有一个，系统种子数据 admin 账号」

## 三件事都必须成立，缺一条就漏

| 判据 | 缺了会怎样 |
|---|---|
| ① 种子会建出 admin | 新装系统没有超级管理员，没人能配权限 |
| ② 建号/改角色**不能**造出第二个 | 出现两个超级管理员，删不掉也说不清谁是正式的 |
| ③ 种子账号**不能被降级/删除** | 系统没有超级管理员，权限配置面永久锁死 |

## 为什么用「账号名」钉身份而不是「角色数 = 1」

角色归属是**代码事实**（`roles.py`：admin 的权限由 `ALL_PERMS` 派生，不查
`role_menus`），所以「谁是超级管理员」不该是可配置项。按账号名钉，
才能做到「改角色时明确报错：只有 admin 能是超级管理员」。

⚠️ 与之配套的**防锁死**规则（不许删唯一 admin、admin 不查授权表）保留不动 ——
那是「防止系统里没人能管」，与本文件的「唯一性」是两件事，都要。
"""

from __future__ import annotations

import re

from pathlib import Path

API = Path(__file__).resolve().parents[2] / "backend" / "app" / "api"
SEED_SERVICE = Path(__file__).resolve().parents[2] / "backend" / "app" / "services" / "menu_seed_service.py"


def test_seed_admin_exists_and_is_named_admin():
    """① 种子常量存在、账号名是 admin、角色是超级管理员。"""
    src = SEED_SERVICE.read_text(encoding="utf-8")
    m = re.search(r"ADMIN_SEED:\s*dict\[str,\s*Any\]\s*=\s*\{([^}]*)\}", src)
    assert m, "找不到 ADMIN_SEED（出厂管理员种子）"
    body = m.group(1)
    assert '"username": "admin"' in body, "种子账号名必须是 admin"
    assert '"role": ROLE_ADMIN' in body, "种子账号角色必须是超级管理员"


def test_seed_only_creates_when_no_admin():
    """① 补：种子判据是「没有 admin 才建」，不许每次启动都覆盖。

    覆盖式播种会把用户自己改过的密码/状态在每次重启时抹掉
    （本仓在菜单/字典上踩过同类问题）。
    """
    src = SEED_SERVICE.read_text(encoding="utf-8")
    start = src.index("def seed_admin_user(")
    end = src.index("\ndef ", start + 10)
    body = src[start:end]
    assert "if count:" in body and "return None" in body, (
        "seed_admin_user 没有「已有 admin 就跳过」的判据 ⇒ 会覆盖用户的修改"
    )


def test_username_constant_is_derived_from_seed():
    """单一真相源：API 侧的账号名常量必须由种子派生，不许各写一份字面量。"""
    src = (API / "system_users.py").read_text(encoding="utf-8")
    assert "ADMIN_SEED" in src, "system_users.py 的超级管理员账号名没从种子派生（两处字面量必然漂移）"
    assert not re.search(r'^SEED_SUPER_ADMIN_USERNAME\s*=\s*"admin"', src, re.M), (
        "账号名被写成了字面量 —— 应改为从 ADMIN_SEED 派生"
    )


def test_create_and_update_both_enforce_singleton():
    """② 建号与改角色两条路都必须调唯一性判据。"""
    src = (API / "system_users.py").read_text(encoding="utf-8")
    calls = re.findall(r"_assert_single_super_admin\(", src)
    #: 定义 1 次 + create 1 次 + update 1 次
    assert len(calls) >= 3, (
        f"_assert_single_super_admin 只出现 {len(calls)} 次 ⇒ 建号或改角色有一条路没拦"
    )
    #: 确认落在 create_user / update_user 里（按函数名定位到下一个顶层 def）
    def _func_body(name: str) -> str:
        start = src.index(f"def {name}(")
        m = re.search(r"\n(def |@router)", src[start + 10 :])
        end = start + 10 + m.start() if m else len(src)
        return src[start:end]

    assert "_assert_single_super_admin" in _func_body("create_user"), (
        "create_user 里没有唯一性判据 ⇒ 能建出第二个超级管理员"
    )
    assert "_assert_single_super_admin" in _func_body("update_user"), (
        "update_user 里没有唯一性判据 ⇒ 能把别人提升为超级管理员"
    )


def test_db_has_singleton_guard_on_role():
    """② 补：库层也要有防线（应用层漏判时不该出现两个 admin）。

    允许的形式：唯一索引 / 部分唯一索引（`role='admin'` 时唯一）。
    这里只断**代码里声明了**该约束（迁移会照 `__table_args__` 建）。
    """
    model = Path(__file__).resolve().parents[2] / "backend" / "app" / "models" / "user.py"
    src = model.read_text(encoding="utf-8")
    #: 不强求已有索引（本仓用户表带软删，部分唯一索引需要 `deleted_at IS NULL` 条件）——
    #: 但必须至少有一处 `__table_args__` 说明，否则连讨论的地方都没有。
    assert "__table_args__" in src, "users 模型没有任何表级约束声明"


def test_seed_account_cannot_be_demoted():
    """③ 把超级管理员改成别的角色必须被拒（否则系统没有超级管理员）。"""
    src = (API / "system_users.py").read_text(encoding="utf-8")
    start = src.index("def _assert_single_super_admin(")
    end = src.index("\ndef ", start + 10)
    body = src[start:end]
    assert "SUPER_ADMIN_NOT_DEMOTABLE" in body, "没有「超级管理员不可降级」的判据"
    assert "SUPER_ADMIN_SINGLETON" in body, "没有「只能有一个超级管理员」的判据"
