"""菜单行与权限登记表的一致性守卫（2026-10-26 G7）。

## 为什么需要这一条

种子的口径是「**只补缺不覆盖**」（用户的编辑不许被打回去，见
`test_seed_no_overwrite.py`）。这条口径是对的，但它有一个副作用：

> **码被删除后，库里那行没人负责清理。**

实测后果（真机截图报过来的）：`xhs:item:operate` 已拆成上架 / 下架 / 生成笔记三码
并从登记表删除，但 `menus` 表里 `id=201` 那行还在 ⇒ 权限管理页永远显示一个
「上下架」按钮，勾上它对任何接口都不生效（`require_perm` 里查不到这个码）。

**这是最坏的一类缺陷**：用户以为授权了，实际什么都没授权 —— 不报错、没日志、
只有「点了没反应」。同批清出 **27 行**这样的幽灵菜单。

## 三条判据

1. 库里每一条 `perms` 都必须在 `perms_registry` 里（否则是幽灵行）；
2. 登记表里每一条 `F`/`C` 码都必须有菜单行（否则授权页里勾不到）；
3. 菜单行的 `name` 必须等于 `PermSpec.label`（否则同一个权限两处叫法不同）。

第 3 条此前是 `test_seed_no_overwrite.py` 在查种子文件；这里查**库**，
因为种子的「只补缺」同样不会纠正库里被改乱的 `name`。
"""
from __future__ import annotations

import pytest

from backend.app.core import perms_registry as reg
from backend.app.core.database import SessionLocal
from backend.app.models.menu import Menu


@pytest.fixture()
def menu_rows():
    """库里的真实菜单行（只读）。"""
    db = SessionLocal()
    try:
        rows = db.query(Menu).order_by(Menu.id).all()
        yield [{"id": r.id, "name": r.name, "menu_type": r.menu_type,
                "perms": r.perms or "", "parent_id": r.parent_id} for r in rows]
    finally:
        db.close()


def test_every_menu_perm_exists_in_the_registry(menu_rows):
    """① 库里不许有登记表里没有的码 —— 幽灵菜单行。"""
    registry = {s.code for s in reg.PERMS}
    ghosts = [
        (r["id"], r["name"], r["perms"])
        for r in menu_rows
        if r["perms"] and r["perms"] not in registry
    ]
    assert not ghosts, (
        "库中存在权限码已从登记表删除的菜单行（勾上它对任何接口都不生效）：\n"
        + "\n".join("  id=%s %s -> %s" % g for g in ghosts)
        + "\n\n处置：删掉这些行（连带 role_menus），或把码加回 perms_registry。"
    )


def test_every_button_and_page_code_has_a_menu_row(menu_rows):
    """② 登记表里的每条 C/F 码都要有菜单行，否则授权页里勾不到。"""
    in_db = {r["perms"] for r in menu_rows if r["perms"]}
    missing = sorted(
        s.code for s in reg.PERMS
        if s.menu_type in ("C", "F") and s.code not in in_db
    )
    assert not missing, (
        "这些码有登记但库里没有菜单行 ⇒ 角色管理页里勾不到，等于无法授权：\n  "
        + "\n  ".join(missing)
    )


def test_menu_names_match_the_registry_labels(menu_rows):
    """③ 菜单名必须等于登记表的 `label`（同一个权限两处叫法不同会让用户困惑）。"""
    label = {s.code: s.label for s in reg.PERMS}
    drift = [
        (r["perms"], r["name"], label[r["perms"]])
        for r in menu_rows
        if r["perms"] in label and r["name"] != label[r["perms"]]
    ]
    assert not drift, (
        "菜单名与登记表 label 不一致：\n"
        + "\n".join("  %s: 库里 %r ≠ 登记表 %r" % d for d in drift)
    )
