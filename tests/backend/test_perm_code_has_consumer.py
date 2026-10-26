"""按钮码「有没有真实判权点」的守卫（设计 72 · 参照若依纪律，2026-10-26）。

## 为什么需要这一条（本轮实测踩到的误判）

清理「零路由码」时，`xhs:settings:edit` 看起来是典型的空壳码：
`ROUTE_PERMS` 里查不到它、菜单种子里有它。差点被直接删掉。

实际它在 `api/settings.py` 里是**真实判权点**：

```python
# 工作区级配置的写权限属于该工作区
if not (has_perm(current_user, f"{clean_ws}:settings:edit") or ...):
```

它不占 `ROUTE_PERMS` 一行的原因是：配置写端点是 `EXEMPT_SELF`
（每人自己的配置不许挂码），工作区那一层在**函数内**用 `has_perm` 判。

⇒ **「零路由」≠「没判权」**。判据必须落在「这个码在代码里有没有被用来判权」：

| 形态 | 例子 | 能不能删 |
|---|---|---|
| 有 `Depends(require_perm("X"))` | 绝大多数 | 不能 |
| 有 `has_perm(user, "X")` / `f"{x}:y"` 拼接 | `xhs:settings:edit` | 不能 |
| 只在菜单种子 + 前端出现，代码里零引用 | `xhs:draft:view` | **可以删** |

本守卫把这两种分开：**代码里有判权点但没登记路由** ⇒ 必须进白名单并写清判权位置；
**代码里零引用** ⇒ 不许留在登记表里（该删）。

## 与若依的对照

若依的码写在 `@PreAuthorize` 注解上，天然「有码必有判权点」；
本项目的码写在登记表里，所以**必须额外查一遍「代码里到底用没用它」**。
这是本项目引入 `route_perms.py` 这张第二真相源的必要代价。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend" / "app"
FRONTEND = ROOT / "frontend" / "src"

#: 「代码里有判权点、但没登记在 `ROUTE_PERMS` 里」的码（键 = 码，值 = 判权位置）。
#:
#: ⚠️ 登记前提：代码里**确实**有 `has_perm(...)` / `require_perm(...)` 用到它。
#: 只写进菜单种子和前端的不算 —— 那种码该删（若依纪律一）。
INLINE_PERM_CHECKS: dict[str, str] = {
    "xhs:settings:edit": (
        "backend/app/api/settings.py 工作区级配置写权限，"
        "按 f\"{workspace}:settings:edit\" 拼接后 has_perm —— "
        "端点本身是 EXEMPT_SELF，故不占 ROUTE_PERMS 一行"
    ),
}


def _code_text(path: Path) -> str:
    """剥注释与 docstring（本仓 docstring 大量写反例，不剥会自命中）。"""
    src = path.read_text(encoding="utf-8", errors="ignore")
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"'''[\s\S]*?'''", "", src)
    return re.sub(r"#[^\n]*", "", src)


def _backend_sources() -> dict[str, str]:
    return {str(p.relative_to(ROOT)): _code_text(p) for p in BACKEND.rglob("*.py")}


def _frontend_sources() -> dict[str, str]:
    out: dict[str, str] = {}
    for pattern in ("*.ts", "*.tsx"):
        for p in FRONTEND.rglob(pattern):
            out[str(p.relative_to(ROOT))] = _code_text(p)
    return out


def test_inline_perm_checks_are_really_used_in_code():
    """白名单里的每条都必须**真的在代码里被判**（不是靠菜单种子冒充）。

    退化表现：有人把 `settings.py` 里那段 `has_perm` 删了，注释还留着 ——
    那时这个码就真成了空壳，应该删掉而不是继续挂在白名单里。
    """
    sources = _backend_sources()
    missing: list[str] = []
    for code, why in INLINE_PERM_CHECKS.items():
        #: 两种形态都算：字面量 `has_perm(u, "X")` 与 `f"{x}:y"` 拼接的尾段。
        tail = code.split(":", 1)[1]  # settings:edit
        hit = any(
            f'"{code}"' in text
            or f"'{code}'" in text
            or f':{tail}"' in text  # f"{workspace}:settings:edit"
            or f":{tail}'" in text
            for text in sources.values()
        )
        if not hit:
            missing.append(f"{code}（{why}）")
    assert not missing, (
        "这些码登记在 INLINE_PERM_CHECKS 里，但代码里已经找不到判权点：\n  "
        + "\n  ".join(missing)
        + "\n\n要么补回判权、要么把码从登记表与白名单里一起删掉（不许留空壳）。"
    )


def test_no_dead_perm_codes_without_any_consumer():
    """**代码里零引用的按钮码必须删掉**（若依纪律一：`perms` 只在有动作时才有值）。

    判据：一个 `F` 型码如果**同时**满足三条 ⇒ 它是空壳，应该删除：
    ① `ROUTE_PERMS` 里没有它；
    ② 后端源码里没有 `has_perm(...)` / `require_perm(...)` 用它（含拼接形态）；
    ③ 不在 `INLINE_PERM_CHECKS` 白名单里。

    ⚠️ **前端引用不救它**：前端有 `hasPermission("x")` 只证明「这个按钮按它显隐」，
    不证明「后端按它判权」。若依里这种码根本不存在 —— 纯前端显隐靠菜单行
    `visible`，不占 `perms`。

    ### 为什么这条重要

    空壳码的真实危害：授权页上勾了它，管理员以为「给了这个能力」，
    实际后端按别的码放行。**表现是「权限配了不生效」，且没有任何报错。**
    """
    from backend.app.core import perms_registry as reg
    from backend.app.core.route_perms import ROUTE_PERMS

    routed = set(ROUTE_PERMS.values())
    sources = _backend_sources()
    code_text = "\n".join(sources.values())

    dead: list[str] = []
    for spec in reg.PERMS:
        if spec.menu_type != "F":
            continue
        if spec.code in routed or spec.code in INLINE_PERM_CHECKS:
            continue
        tail = spec.code.split(":", 1)[1]
        used = (
            f'"{spec.code}"' in code_text
            or f"'{spec.code}'" in code_text
            or f':{tail}"' in code_text
            or f":{tail}'" in code_text
        )
        if not used:
            dead.append(spec.code)

    #: 存量欠账：这些码确实还没落判权点，但**已经有处置结论**（见
    #: `test_orphan_perm_classification.py` 的分类表）。只许变短 ——
    #: 每补完一条就从这里删掉，新增空壳码即红。
    from tests.backend.test_orphan_perm_classification import (  # noqa: PLC0415
        DISCRIMINATED,
        ENTRY_ONLY,
        REMAP_PENDING,
        SERVICE_PICKED,
        UNDECIDED,
    )

    known = (
        set(REMAP_PENDING)
        | set(DISCRIMINATED)
        | set(SERVICE_PICKED)
        | set(ENTRY_ONLY)
        | set(UNDECIDED)
    )
    new_dead = sorted(set(dead) - known)
    assert not new_dead, (
        f"新增了 {len(new_dead)} 个空壳按钮码（没路由、也没任何后端判权点）：\n  "
        + "\n  ".join(new_dead)
        + "\n\n若依纪律一：`perms` 只在「有后端动作」时才有值。"
        "纯前端显隐不该编权限码 —— 删掉码、前端改用页面码或直接不判。"
        "\n若确实在函数内判权（不占路由），登记到 INLINE_PERM_CHECKS 并写清位置。"
    )

    #: ⚠️ **不在这里**判「分类表是否过时」—— 那个判据归
    #: `test_orphan_perm_classification.py::test_classification_covers_every_orphan`
    #: （它按「有没有 `ROUTE_PERMS` 行」判，与这里的「有没有判权点」是两个口径）。
    #: 两个口径混在一个断言里会互相误报：`system:scheduler:run` 这类码
    #: **有判权点（服务层选码）但不占路由**，在分类表里是对的，在这里也会被判 stale。
