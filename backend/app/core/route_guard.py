"""启动期路由权限守卫 —— **本次改造最关键的守卫**（设计 50 §2.4）。

## 为什么在启动期拦而不是靠测试

`251 个`写端点（`openapi()` 口径，2026-10-23 实测 350 ops 里的写方法）逐个挂码是
机械活，漏挂一条的后果是**那个接口对任何登录用户都开放**，且界面上完全看不出来
（按钮还在、点下去也成功）。事后跑测试才发现，等于把风险留到发版之后。

⇒ 判据放进 `lifespan`：**缺一条 ⇒ `RuntimeError`，应用起不来**。
开发期第一次启动就发现，不是上线后才发现。

## 判据

1. `app.openapi()["paths"]` 里每个 `method ∈ WRITE_METHODS` 的 `path`
   **必须** 出现在 `route_perms.ROUTE_PERMS` 或 `route_perms.EXEMPT`；
2. `READONLY_PAGE_GUARDS prefix → code`：该前缀下**所有** method 都必须登记且等于该码；
3. 登记表里的码**必须**在 `perms.ALL_PERMS` 里（写错字母 ⇒ 报错，不是静默放行）；
4. `perms.assert_registry_sane()`（登记表自身无水货：格式 / 重复 / 孤儿 F 行）。

`ROUTE_PERMS` 的反向检查（登记了但路由不存在）也要做：那说明端点被删了而码没删，
码会一直留在「角色分配菜单」里骗人。
"""

from __future__ import annotations

from fastapi import FastAPI

from backend.app.core.perms import ALL_PERMS, assert_registry_sane
from backend.app.core.route_perms import (
    EXEMPT,
    READONLY_PAGE_GUARDS,
    ROUTE_PERMS,
    WRITE_METHODS,
)


class RouteGuardError(RuntimeError):
    """守卫失败。**继承 RuntimeError** —— 设计 50 §2.4 的判据是「拒绝启动」。"""


def _iter_operations(app: FastAPI):
    """产出 `(method_upper, path_template)`。用 OpenAPI 而不是 `app.routes`：

    `app.routes` 里同一个 path 会因 `include_router` 出现多次，且拿不到
    `{id}` 的模板写法（`route.path` 有，但 `route.path_format` 才是模板）；
    OpenAPI 的 key 就是前端看到的形状，比对最直白。
    """
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            yield method.upper(), path


def _iter_routes(app: FastAPI):
    """产出 `(path, methods, dependant)`，**覆盖 include_router 进来的路由**。

    ⚠️ FastAPI 0.141 把 `include_router` 改成了延迟展开：`app.routes` 里只有
    42 个 `_IncludedRouter` 包装对象，真实路由在 `effective_route_contexts()`
    里。所以「遍历 app.routes 找 path」在 0.141 上**一条都找不到** —— 直接后果是
    下面那个「登记了但没挂码」的检查会静默地永远通过（因为找不到路由就 continue）。
    这是实测踩到的：守卫报 8 条「只登记未挂码」，而代码里明明挂了 `Depends`。
    """
    from fastapi.routing import _IncludedRouter

    for route in app.routes:
        if isinstance(route, _IncludedRouter):
            for ctx in route.effective_route_contexts():
                yield getattr(ctx, "path", None), getattr(ctx, "methods", set()) or set(), getattr(ctx, "dependant", None)
        else:
            yield (
                getattr(route, "path", None),
                getattr(route, "methods", set()) or set(),
                getattr(route, "dependant", None),
            )


def _route_has_perm_dependency(app: FastAPI, path: str, method: str) -> bool:
    """该路由的依赖链里是否有 `require_perm(...)`（= 真的挂了码）。

    只在 `READONLY_ROUTES` 的逐条校验里用：**登记了不等于挂上了**，
    不查这一步，守卫会给出「已登记」的假结论。
    """
    for rpath, rmethods, dependant in _iter_routes(app):
        if rpath != path or method.upper() not in rmethods:
            continue
        if dependant is None:
            continue
        for dep in dependant.dependencies:
            #: `require_perm` 返回的是闭包，`__qualname__` 恒为
            #: `require_perm.<locals>.dependency` ⇒ 用它在**构造时**打上的标记判断。
            if getattr(dep.call, "__perm_code__", None):
                return True
    return False


def collect_violations(app: FastAPI) -> list[str]:
    """返回违规清单（空 = 通过）。**不抛**，方便测试逐条断言。"""
    problems: list[str] = []

    try:
        assert_registry_sane()
    except ValueError as exc:  # 登记表本身有问题
        problems.append(f"[登记表] {exc}")

    #: `EXEMPT_SELF` / `EXEMPT_ADMIN` 不是权限码而是**标记**（见 route_perms.py）。
    #: 前者 = 本人操作（改自己密码/配置），后者 = 影响全站但无对应码（端点内判 admin）。
    #: 判定时排除它们，但**仍要求它们出现在映射里** —— 「豁免」必须显式写出来，
    #: 不能靠「忘了挂码」。这正是本守卫存在的目的。
    _MARKERS = {"EXEMPT_SELF", "EXEMPT_ADMIN"}
    from backend.app.core.route_perms import READONLY_ROUTES

    registered = (
        {c for c in ROUTE_PERMS.values() if c not in _MARKERS}
        | set(READONLY_PAGE_GUARDS.values())
        | set(READONLY_ROUTES.values())
    )
    unknown = sorted(code for code in registered if code not in ALL_PERMS)
    if unknown:
        problems.append(f"[未登记的码] {unknown} —— 写错字母或漏进 perms_registry")

    #: ⚠️ `ROUTE_PERMS` 的键是 **`METHOD path`** 而不是裸 path（2026-10-24）：
    #: 同一 path 上可以有多个写方法（`POST /materials/files` 与
    #: `DELETE /materials/files`），裸 path 做键会让后写的覆盖前一条，
    #: 于是「一个方法挂了码、另一个没挂」这种情况**查不出来**。
    seen_keys: set[str] = set()
    for method, path in _iter_operations(app):
        key = f"{method} {path}"
        seen_keys.add(key)
        if method in WRITE_METHODS:
            if path in EXEMPT:
                continue
            if key not in ROUTE_PERMS:
                problems.append(f"[漏挂码] {method} {path}")

    #: 逐条登记的只读路由（`READONLY_ROUTES`）：**每条都必须真的挂上**。
    #: 光登记不挂 = 数据能被看到但守卫说「已登记」—— 那是假守卫。
    for key, code in READONLY_ROUTES.items():
        method, path = key.split(" ", 1)
        if key not in seen_keys:
            problems.append(f"[只读路由不存在] {key} —— 端点被改名或删除后没同步守卫表")
        elif ROUTE_PERMS.get(key) != code:
            problems.append(f"[只读页缺码] {key} 应为 {code}，实际 {ROUTE_PERMS.get(key) or '未登记'}")
        elif not _route_has_perm_dependency(app, path, method):
            problems.append(f"[只读页只登记未挂码] {key} —— ROUTE_PERMS 有、路由上没有 Depends(require_perm)")

    for prefix, code in READONLY_PAGE_GUARDS.items():
        hit = False
        for method, path in _iter_operations(app):
            if not path.startswith(prefix):
                continue
            hit = True
            actual = ROUTE_PERMS.get(f"{method} {path}") or ROUTE_PERMS.get(path)
            if actual != code:
                problems.append(f"[只读页缺码] {method} {path} 应为 {code}，实际 {actual or '未登记'}")
        if not hit:
            problems.append(f"[只读页前缀不存在] {prefix} —— 端点被改名或删除后没同步守卫表")

    stale = sorted(key for key in ROUTE_PERMS if key not in seen_keys)
    if stale:
        problems.append(f"[码指向不存在的路由] {stale}")

    seen_paths = {k.split(" ", 1)[-1] for k in seen_keys}
    exempt_stale = sorted(path for path in EXEMPT if path not in seen_paths)
    if exempt_stale:
        problems.append(f"[豁免了不存在的路由] {exempt_stale}")

    return problems


def assert_every_route_guarded(app: FastAPI) -> None:
    """启动自检入口（`main.py::lifespan` 调用）。有任何违规 ⇒ `RouteGuardError`。"""
    problems = collect_violations(app)
    if problems:
        detail = "\n".join(f"  - {line}" for line in problems)
        raise RouteGuardError(
            "路由权限守卫未通过（缺权限码的写接口对任何登录用户开放，拒绝启动）：\n" + detail
        )
