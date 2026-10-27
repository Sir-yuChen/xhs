"""页签不得有自己的菜单项。

一个页面 = 一个菜单项；页签只在页内 `Tabs` 体现（`?tab=` 仍进 URL，深链可用）。
页签若各占一行，侧栏会显示成 N 个伪页面，且每个都要各配一份授权行。

判据：`_TAB_CHILDREN` 必须为空 / 种子里无 `?tab=` 行 / 页面的 Tabs 必须还在 /
迁移先删授权行且判据用 path（name 可被用户改）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
#: 生成器在**仓库根 `scripts/`**（进版本库）；`.hermes/scripts/` 那份是转发壳。
GENERATOR = ROOT / "scripts" / "generate_menu_seed.py"
SEED = ROOT / "config" / "initial_data" / "menus.seed.json"
WORKSHOP_PAGE = ROOT / "frontend" / "src" / "pages" / "platforms" / "xhs" / "note-workshop-page.tsx"
MIGRATION = ROOT / "backend" / "alembic" / "versions" / "r4s5t6u7v8w9_drop_tab_menu_rows.py"


def test_tab_children_registry_is_empty() -> None:
    """页签行的来源表必须为空 —— 要加页签就改页面的 `Tabs`，不要加菜单行。"""
    src = GENERATOR.read_text(encoding="utf-8")
    m = re.search(r"_TAB_CHILDREN:[^=]*=\s*\{", src)
    assert m, "找不到 `_TAB_CHILDREN` 声明（改名了就要同步改本守卫）"
    #: 取声明的右值，必须是空字典。
    tail = src[m.end() :]
    end = tail.index("}") + 1
    body = tail[:end]
    assert body.strip() == "}", f"`_TAB_CHILDREN` 不是空的，又给页签加菜单项了：{body[:120]!r}"


def test_seed_has_no_tab_menu_rows() -> None:
    """生成的种子里不许有 `path` 带 `?tab=` 的菜单行。"""
    payload = json.loads(SEED.read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else payload.get("menus") or payload.get("items") or []
    offenders = [r for r in rows if isinstance(r, dict) and "?tab=" in str(r.get("path") or "")]
    assert not offenders, f"种子里还有页签菜单行：{[r.get('name') for r in offenders]}"


def test_workshop_page_still_has_its_tabs() -> None:
    """页签**功能本身**必须还在 —— 撤菜单行不等于撤页签。

    用户要的是「归属到同一个页中」，不是「不要这些功能」。这条反向守着：
    页面仍用 antd `Tabs`、仍把 `?tab=` 写进 URL（深链与书签不死）。
    """
    src = WORKSHOP_PAGE.read_text(encoding="utf-8")
    assert "<Tabs" in src, "笔记工坊页的页签被删了（用户要的是归属同页，不是砍功能）"
    assert 'next.set("tab", key)' in src or 'set("tab"' in src, "页签不再写进 URL，深链会死"


def test_migration_deletes_tab_rows_and_their_grants() -> None:
    """迁移必须**先删授权行再删菜单行**（外键方向），且判据用 path 不用 name。

    用 `name` 判会漏 —— 菜单名是用户可改的字段。
    """
    src = MIGRATION.read_text(encoding="utf-8")
    assert "role_menus" in src and "DELETE FROM role_menus" in src, "没删授权行（外键会拦）"
    #: 先 role_menus 后 menus。
    assert src.index("DELETE FROM role_menus") < src.index("DELETE FROM menus WHERE id IN"), (
        "删除顺序反了：必须先清授权行"
    )
    assert "path LIKE" in src, "判据不是 path（用 name 会漏掉改过名的行）"
