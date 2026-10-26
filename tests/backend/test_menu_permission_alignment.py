"""菜单-权限码全量对账（2026-10-26 用户口径「菜单管理中有很多缺失，重新全面梳理并补全」）。

## 三条独立判据

用户看到的是「菜单管理里缺东西」，但它背后有三类彼此独立的问题，只补一类仍然不完整：

### ① 菜单行 ↔ 权限码 双向对齐
* **权限码有、菜单行无** ⇒ 角色授权页勾不到该按钮 ⇒ 没人能获得这个权限
  （永远是「点了 403」，而管理员不知道为什么）。
* **菜单页面行无权限码** ⇒ 授权页勾了这个页面 = 发了个空壳（后端照样 403）。

### ② 页面级码 ↔ 后端路由
页面级（`C` 型）权限码必须至少挂一条后端路由；否则它是「看不见的码」——
前端按它渲染按钮/页面，后端却用它拦不住任何东西。

### ③ 按钮级码 ↔ 父页面
按钮必须是某个页面（`C` 型，或同一 URL 的 Tab 页）的子节点。
挂错父节点 ⇒ 授权页里出现在错误的页面下，管理员会勾错。

## 为什么必须有这条守卫

权限体系是三处数据（`perms_registry.py` / `menus` 表 / `route_perms.py`）
+ 前端 `hasPermission()` 调用点的四方耦合，任何一处新增而另一处漏改，
**都不会报错**，只会静默降级成「按钮看不见 / 点了 403 / 勾了没用」。
本轮实测第一版就漏了 15 个码与 13 个页面行。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "config" / "initial_data" / "menus.seed.json"


def _registry():
    from backend.app.core.perms_registry import PERMS

    return PERMS


def _routes() -> dict[str, str]:
    from backend.app.core.route_perms import ROUTE_PERMS

    return ROUTE_PERMS


def _seed_rows() -> list[dict]:
    return json.loads(SEED.read_text(encoding="utf-8-sig"))["menus"]


def _seed_by_seq() -> dict[int, dict]:
    return {r["_seq"]: r for r in _seed_rows()}


def _codes_with_routes() -> set[str]:
    return set(_routes().values())


def test_every_permission_code_has_a_menu_row():
    """① 权限码都必须有菜单行（否则授权页勾不到）。"""
    perms = {p.code for p in _registry()}
    rows = {r["perms"] for r in _seed_rows() if r.get("perms")}
    missing = sorted(perms - rows)
    assert not missing, (
        "以下权限码在菜单种子里没有对应行 ⇒ 角色授权页勾不到，永远没人能获得它：\n  "
        + "\n  ".join(missing)
    )


def test_every_page_menu_row_has_a_permission_code():
    """② 页面行必须有权限码（否则勾了页面也是空壳）。"""
    bad = sorted(
        (r["_seq"], r["name"], r.get("path", ""))
        for r in _seed_rows()
        if r["menu_type"] in ("C", "M") and not r.get("perms") and r.get("path")
    )
    assert not bad, (
        "以下带路径的页面菜单没有权限码 ⇒ 勾选后后端仍会 403：\n  "
        + "\n  ".join(f"seq={s} {n} {p}" for s, n, p in bad)
    )


#: 纯前端聚合页：自己没有后端接口（数据都来自别的域），所以「页面级码必须挂路由」
#: 这条对它不成立 —— 它拦的是**前端入口**，不是接口。登记要带理由。
PURE_FRONTEND_PAGE_CODES: dict[str, str] = {
    "xhs:dashboard:view": "总览页，数据来自各业务域的既有接口",
    "xhs:datacenter:view": "数据中心页，读 notes 域接口",
    "xhs:workshop:view": "笔记工坊页，Tab 内各面板读各自域的接口",
    "xhs:insights:shop": "商品洞察页，读 analytics 域接口",
    "xhs:profile:view": "个人资料页，读 auth 接口",
    "system:setting:view": "配置管理页，实际读写 /api/settings（挂 xhs:settings:view）",
}

#: 「一个接口按归属双码」：静态路由表只能挂一个码，另一个在**服务层**按归属判。
#: 值 = (实际挂的码, 服务层判据所在文件)。
SHARED_ROUTE_CODES: dict[str, str] = {
    "xhs:taskcenter:view": (
        "GET /api/schedule/jobs 挂 system:scheduler:view；"
        "工作区用户由 schedule.py::list_jobs 按归属放行（只听 taskcenter:view 时只看非系统级）"
    ),
}


def test_every_page_level_code_guards_at_least_one_route():
    """③ 页面级码必须真的拦得住路由（否则是「看不见的码」）。

    例外：`PURE_FRONTEND_PAGE_CODES`（纯前端聚合页，无自己的接口，带理由登记）。
    """
    guarded = _codes_with_routes()
    page_codes = {p.code for p in _registry() if p.menu_type == "C"}
    orphan = sorted(
        page_codes - guarded - set(PURE_FRONTEND_PAGE_CODES) - set(SHARED_ROUTE_CODES)
    )
    assert not orphan, (
        "以下页面级权限码没有挂任何后端路由 ⇒ 前端用它控制可见性，后端却拦不住：\n  "
        + "\n  ".join(orphan)
    )


def test_button_rows_hang_under_an_existing_page_row():
    """④ 按钮必须挂在存在的页面上（挂到目录/不存在节点 ⇒ 授权页显示错位）。"""
    by_seq = _seed_by_seq()
    bad = []
    for r in _seed_rows():
        if r["menu_type"] != "F":
            continue
        parent = by_seq.get(r.get("parent_seq") or 0)
        if parent is None:
            bad.append((r["name"], r["perms"], "父节点不存在"))
        elif parent["menu_type"] not in ("C", "M"):
            #: 允许挂 M（Tab 组合页，如工坊 5 个 Tab 挂在 M 行下）——
            #: 但**不允许挂 F**（按钮套按钮）。
            bad.append((r["name"], r["perms"], f"父是 {parent['menu_type']}"))
    assert not bad, "以下按钮行挂错父节点：\n  " + "\n  ".join(map(str, bad))


def test_no_duplicate_permission_codes_in_seed():
    """⑤ 种子内不许重复的码（重复 = 授权页出现两行同名权限）。"""
    seen: dict[str, list[str]] = {}
    for r in _seed_rows():
        code = r.get("perms")
        if code:
            seen.setdefault(code, []).append(r["name"])
    dups = {k: v for k, v in seen.items() if len(v) > 1}
    assert not dups, f"种子内权限码重复：{dups}"


def test_seed_matches_database():
    """⑥ 种子与**运行库**一致（否则新装与存量两套菜单）。

    只在能连库时校验：CI 无库时跳过，但本机必须跑（改完种子要同步库）。
    """
    try:
        from sqlalchemy import create_engine, text

        from backend.app.core.config import settings
    except Exception:  # pragma: no cover
        pytest.skip("无数据库配置")

    try:
        engine = create_engine(settings.database_url)
        with engine.connect() as conn:
            db_codes = {
                r[0]
                for r in conn.execute(text("select perms from menus where perms is not null"))
            }
    except Exception:  # pragma: no cover
        pytest.skip("数据库不可达")

    seed_codes = {r["perms"] for r in _seed_rows() if r.get("perms")}
    assert not (seed_codes - db_codes), (
        "种子有、库中没有（新装与存量不一致）：\n  " + "\n  ".join(sorted(seed_codes - db_codes))
    )
