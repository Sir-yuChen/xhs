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


def collect_violations(app: FastAPI) -> list[str]:
    """返回违规清单（空 = 通过）。**不抛**，方便测试逐条断言。"""
    problems: list[str] = []

    try:
        assert_registry_sane()
    except ValueError as exc:  # 登记表本身有问题
        problems.append(f"[登记表] {exc}")

    registered = set(ROUTE_PERMS.values()) | set(READONLY_PAGE_GUARDS.values())
    unknown = sorted(code for code in registered if code not in ALL_PERMS)
    if unknown:
        problems.append(f"[未登记的码] {unknown} —— 写错字母或漏进 perms_registry")

    seen_paths: set[str] = set()
    for method, path in _iter_operations(app):
        seen_paths.add(path)
        if method in WRITE_METHODS:
            if path in EXEMPT:
                continue
            if path not in ROUTE_PERMS:
                problems.append(f"[漏挂码] {method} {path}")

    for prefix, code in READONLY_PAGE_GUARDS.items():
        hit = False
        for method, path in _iter_operations(app):
            if not path.startswith(prefix):
                continue
            hit = True
            if ROUTE_PERMS.get(path) != code:
                problems.append(
                    f"[只读页缺码] {method} {path} 应为 {code}，实际 {ROUTE_PERMS.get(path) or '未登记'}"
                )
        if not hit:
            problems.append(f"[只读页前缀不存在] {prefix} —— 端点被改名或删除后没同步守卫表")

    stale = sorted(path for path in ROUTE_PERMS if path not in seen_paths)
    if stale:
        problems.append(f"[码指向不存在的路由] {stale}")

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
