"""判权点必须真的能判权（2026-10-26 实测缺陷的守卫）。

## 缺陷原文

`core/perms.py::has_perm` 的**签名里没有 `db`**，内部 `role_perms(role)` 也不带 db
⇒ 落到 `role_perms` 的「拿不到 db ⇒ 保守给空集」分支 ⇒ **非 admin 用户查任何码恒 False**。

## 为什么它能活很久

返回 `False` 与「用户确实没这个权限」**在调用方看来完全一样** ——
不抛错、不进日志、不做任何区分。真机表现只是「按钮点了没反应」，
而且**只影响非 admin**（admin 走 `ALL_PERMS` 分支，永远放行）⇒
开发者自己用 admin 测，一切正常。

实测受害点：`schedule.py` 的「立即执行 / 编辑任务」、`settings.py` 的
「工作区配置编辑」共 **15 处**函数内判权 —— 普通管理员与普通用户一律被拒。

## 两条判据

1. **行为**：非 admin + 不传 db ⇒ 必须 `raise`（不是 `False`）；传 db ⇒ 按库里的授权返回。
2. **静态**：`backend/app` 里每个 `has_perm(...)` 调用点都必须带 `db=`。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app.core import perms as perms_mod

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "backend" / "app"


class _FakeUser:
    def __init__(self, role: str) -> None:
        self.role = role


class _FakeSession:
    """最小 db 替身：`perms_of` 只需要 `db` 能被传下去（打桩掉真实查询）。"""

    def __init__(self, codes: set[str]) -> None:
        self._codes = codes
        self.calls = 0

    # permission_cache.perms_of 会调 rbac_service.perms_for_user(db, user)，
    # 但那需要真表；这里直接让 cache 层被 monkeypatch 掉（见下面用例）。


def test_non_admin_without_db_raises_instead_of_silently_denying(monkeypatch):
    """① 非 admin 不传 db ⇒ **raise**。

    这条是本缺陷的根因判据：早期版本返回 `False`，与「真没权限」无法区分。
    """
    user = _FakeUser("user")
    with pytest.raises(perms_mod.PermissionResolutionError) as ei:
        perms_mod.has_perm(user, "xhs:item:view")
    msg = str(ei.value)
    assert "db" in msg, "报错必须告诉调用方缺 db"
    assert "xhs:item:view" in msg, "报错必须点出是哪个码"


def test_non_admin_with_db_reads_the_real_grants(monkeypatch):
    """② 传 db ⇒ 按库里授权返回（打桩 `perms_of`，证明走的确实是它）。"""
    import backend.app.core.permission_cache as pc

    monkeypatch.setattr(pc, "perms_of", lambda db, user: frozenset({"xhs:item:view"}))
    user = _FakeUser("user")
    sentinel = object()
    assert perms_mod.has_perm(user, "xhs:item:view", db=sentinel) is True
    assert perms_mod.has_perm(user, "xhs:item:delete", db=sentinel) is False


def test_admin_needs_no_db():
    """③ admin 由 `ALL_PERMS` 派生 ⇒ 不需要 db（也不该为此查库）。"""
    assert perms_mod.has_perm(_FakeUser("admin"), "system:user:delete") is True
    assert perms_mod.has_perm(_FakeUser("admin"), "随便:不存在:码") is False


def test_unregistered_code_is_false_even_for_admin():
    """④ 未登记的码对 admin 也是 False（写错字母不会意外放行）。"""
    assert perms_mod.has_perm(_FakeUser("admin"), "xhs:item:notARealCode") is False


def test_has_any_and_has_all_forward_db(monkeypatch):
    """⑤ `has_any` / `has_all` 必须把 db 转下去，否则它们也是残废的。

    ⚠️ 用的码必须在登记表里 —— `has_perm` 对未登记的码恒 `False`（防写错字母放行），
    拿假码测会得到「看起来没转发 db」的假红。
    """
    import backend.app.core.permission_cache as pc

    granted = "xhs:item:view"
    denied = "xhs:item:delete"
    seen = []

    def fake_perms_of(db, user):
        seen.append(db)
        return frozenset({granted})

    monkeypatch.setattr(pc, "perms_of", fake_perms_of)
    user = _FakeUser("user")
    sentinel = object()
    assert perms_mod.has_any(user, [granted, denied], db=sentinel) is True
    assert perms_mod.has_any(user, [denied], db=sentinel) is False
    assert perms_mod.has_all(user, [granted], db=sentinel) is True
    assert perms_mod.has_all(user, [granted, denied], db=sentinel) is False
    assert seen, "db 没被转发到 perms_of"
    assert all(s is sentinel for s in seen), "传下去的不是调用方的 db"


# --------------------------------------------------------------------------- #
# 静态判据
# --------------------------------------------------------------------------- #

CALL_NAMES = {"has_perm", "has_any", "has_all"}


def _iter_perm_calls(tree):
    """AST 找出所有对三个 helper 的**真实调用**（docstring / 注释天然被忽略）。"""
    import ast

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name in CALL_NAMES:
            yield node


def test_every_inline_perm_call_passes_db():
    """⑥ `backend/app` 里每个 `has_perm(...)` 调用都必须带 `db=`。

    ⚠️ 这条是**静态判据**，它抓得住「新写的判权点又忘了传 db」；
    行为判据（①）只在真正被调用时才会炸，漏一个就又是一个静默缺陷。

    用 AST 而不是正则：本仓 docstring 里大量出现 `has_perm(...)` 作为说明文字，
    正则会把它们当成调用（实测误报 3 处）。
    """
    import ast

    offenders = []
    for f in APP.rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in _iter_perm_calls(tree):
            kw = {k.arg for k in node.keywords}
            if "db" in kw:
                continue
            #: 定义本身（`def has_perm(...)`）不是 Call，不用管
            offenders.append("%s:%d  %s(...)" % (
                f.relative_to(ROOT), node.lineno,
                getattr(node.func, "id", None) or getattr(node.func, "attr", "?")))
    assert not offenders, (
        "这些函数内判权点没传 db= ⇒ 对非 admin 用户恒 False（静默失效）：\n  "
        + "\n  ".join(offenders)
    )
