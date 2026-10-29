"""菜单种子与「平台」目录退场（侧栏信息架构）。

## 判据

- 种子由 `scripts/generate_menu_seed.py` 从 `perms_registry` **派生**：`--check` 必须通过；
- 小红书工作区一级项里**没有「平台」目录**，账号页叫「账号管理」且与「总览」平级；
- 库里不得有「父菜单已退场」的孤儿 F 行（菜单树会把它们当顶级项渲染出去）。

## 为什么行为断言测不到（L2）

这三条都是**跨层静默错**：`/api/system/menus/mine` 永远 200，错的是树形结构 ——
少一层目录看不出来（页面还在，只是藏在不同分组），孤儿按钮也照样能返回（挂在错误的父级下）。
只能靠「树结构 + 库里父子完整性」直接断言。

## 用例预算：6
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SEED = ROOT / "config" / "initial_data" / "menus.seed.json"


def _seed_rows() -> list[dict]:
    return json.loads(SEED.read_text(encoding="utf-8"))["menus"]


def _seed_parent(row: dict) -> dict:
    """按 `parent_seq` 找父行（`_seq=0` 表示工作区根之外，返回空 dict）。"""
    return next((r for r in _seed_rows() if r["_seq"] == row["parent_seq"]), {})


def _seed_menus(db) -> None:
    """真播一次菜单种子 —— 断言对象是「播种后的库」，不是手工插入的行。"""
    from backend.app.services.menu_seed_service import seed_menus

    seed_menus(db)
    db.flush()


# --------------------------------------------------------------------- 种子层
def test_seed_file_matches_the_registry() -> None:
    """种子与登记表必须逐字节一致（手改种子 = 第二份真相源）。"""
    from scripts.generate_menu_seed import build

    assert json.loads(SEED.read_text(encoding="utf-8")) == build()


@pytest.mark.parametrize("dir_name", ["平台"])
def test_platform_directory_is_gone(dir_name: str) -> None:
    """「平台」目录行不得再出现在种子里。"""
    dirs = [r["name"] for r in _seed_rows() if r["menu_type"] == "M" and r["workspace"] == "xhs"]
    assert dir_name not in dirs, f"xhs 工作区仍有目录 {dir_name!r}：{dirs}"


def test_accounts_page_is_top_level_and_renamed() -> None:
    """账号页 = 小红书工作区一级项，名字是「账号管理」，排在「总览」之后。"""
    rows = _seed_rows()
    xhs_root = next(r for r in rows if r["menu_type"] == "M" and r["workspace"] == "xhs" and r["parent_seq"] == 0)

    page = next(r for r in rows if r["path"] == "/platforms/xhs/accounts" and r["perms"] == "xhs:account:view")
    assert page["name"] == "账号管理"
    assert page["parent_seq"] == xhs_root["_seq"], "账号页没挂在工作区根下（还在某个目录里）"

    dashboard = next(r for r in rows if r["path"] == "/platforms/xhs/dashboard")
    assert dashboard["parent_seq"] == xhs_root["_seq"], "「总览」不在工作区根下"
    assert dashboard["sort_order"] < page["sort_order"], "「总览」应排在「账号管理」之前"


def test_sibling_c_row_on_the_same_path_keeps_its_own_name(db) -> None:
    """账号页下的「登录记录」不得被改名成「账号管理」。

    ⚠️ 改名按**权限码**覆盖，不按 path：那个 path 上挂着两条 C 行
    （本体 `xhs:account:view` 与 `xhs:login:view`；后者 path 为空，靠父行归属）。
    """
    from sqlalchemy import text

    _seed_menus(db)
    rows = dict(
        db.execute(
            text("SELECT perms, name FROM sys_menus WHERE perms IN ('xhs:account:view', 'xhs:login:view')")
        ).all()
    )
    assert rows.get("xhs:account:view") == "账号管理"
    assert rows.get("xhs:login:view") == "登录记录", f"登录记录被改名成 {rows.get('xhs:login:view')!r}"


# --------------------------------------------------------------------- 库里（真跑迁移后的结构）
def test_db_has_no_orphan_button_rows(db) -> None:
    """库里没有「父已退场」的 F 行 —— 它们会被菜单树当顶级项渲染。"""
    from sqlalchemy import text

    _seed_menus(db)
    orphan = db.scalar(
        text(
            "SELECT count(*) FROM sys_menus WHERE parent_id <> 0 "
            "AND parent_id NOT IN (SELECT id FROM sys_menus)"
        )
    )
    assert orphan == 0, f"库里有 {orphan} 条父菜单不存在的行（菜单树会把它们挂到顶级）"


def test_db_has_no_platform_directory_and_accounts_is_top_level(db) -> None:
    """真库结构：无「平台」目录；账号页挂小红书目录根（`parent_id=0`）且叫「账号管理」。"""
    from sqlalchemy import text

    _seed_menus(db)
    assert db.scalar(text("SELECT count(*) FROM sys_menus WHERE name = '平台' AND menu_type = 'M'")) == 0

    parent_id = db.scalar(
        text("SELECT parent_id FROM sys_menus WHERE path = :path AND perms = :perms"),
        {"path": "/platforms/xhs/accounts", "perms": "xhs:account:view"},
    )
    assert parent_id is not None, "库里没有账号页菜单行"
    name, workspace = db.execute(
        text("SELECT name, workspace FROM sys_menus WHERE path = :path AND perms = :perms"),
        {"path": "/platforms/xhs/accounts", "perms": "xhs:account:view"},
    ).first()
    assert name == "账号管理"
    assert workspace == "xhs"

    #: 一级项 = 父行是**工作区根**（`parent_id=0` 的 M 行），不是任何目录。
    parent = db.execute(
        text("SELECT name, menu_type, parent_id, workspace FROM sys_menus WHERE id = :pid"),
        {"pid": parent_id},
    ).first()
    assert parent is not None, "账号页的父行不存在（孤儿）"
    assert parent[1] == "M" and parent[2] == 0, f"账号页仍挂在目录 {parent[0]!r} 下"
    assert parent[3] == "xhs"


def test_migration_moves_the_page_and_cleans_orphans(db) -> None:
    """迁移本身：对着**改动前的库**跑一次 upgrade，账号页提到一级 + 孤儿按钮被清。

    ⚠️ 这条不播种子、不调生成器 —— 它验的是「存量库升级后长什么样」。
    只断言种子的话，迁移写错（改名漏了 / 没重挂 / 不清孤儿）一条都测不出来。
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    from backend.alembic.versions import n1e2x3t4m5u6_accounts_to_top_level as mig

    db.execute(
        text(
            "INSERT INTO sys_menus (parent_id, name, menu_type, path, component, perms, icon, "
            "workspace, sort_order, is_visible, status, created_at, updated_at) "
            "VALUES (0, '小红书', 'M', '', '', '', 'xiaohongshu', 'xhs', 0, true, 'normal', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )
    root_id = db.scalar(text("SELECT id FROM sys_menus WHERE name = '小红书'"))
    #: 另一个工作区根 —— 让「小红书」**不是**第一条顶级行，避免 id 取值巧合
    db.execute(
        text(
            "INSERT INTO sys_menus (parent_id, name, menu_type, path, component, perms, icon, "
            "workspace, sort_order, is_visible, status, created_at, updated_at) "
            "VALUES (0, '系统管理', 'M', '', '', '', 'setting', 'system', 1, true, 'normal', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )
    db.execute(
        text(
            "INSERT INTO sys_menus (parent_id, name, menu_type, path, component, perms, icon, "
            "workspace, sort_order, is_visible, status, created_at, updated_at) "
            "VALUES (:root, '平台', 'M', '', '', '', 'safety-certificate', 'xhs', 0, true, "
            "'normal', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"root": root_id},
    )
    dir_id = db.scalar(text("SELECT id FROM sys_menus WHERE name = '平台'"))
    db.execute(
        text(
            "INSERT INTO sys_menus (parent_id, name, menu_type, path, component, perms, icon, "
            "workspace, sort_order, is_visible, status, created_at, updated_at) "
            "VALUES (:dir, '账号矩阵', 'C', '/platforms/xhs/accounts', '', 'xhs:account:view', "
            "'safety-certificate', 'xhs', 0, true, 'normal', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"dir": dir_id},
    )
    #: 目录行带一条授权：`role_menus.menu_id` 是 RESTRICT ⇒ 迁移不先清就会撞外键
    db.execute(
        text(
            "INSERT INTO sys_roles (code, name, description, is_system, status, sort_order, "
            "created_at, updated_at) VALUES ('admin', '超级管理员', '', true, 'normal', 0, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )
    role_id = db.scalar(text("SELECT id FROM sys_roles WHERE code = 'admin'"))
    db.execute(
        text("INSERT INTO sys_role_menus (user_id, role_id, menu_id) VALUES (NULL, :rid, :mid)"),
        {"rid": role_id, "mid": dir_id},
    )

    #: 父菜单已退场的孤儿按钮（真库里的监控 / 自动运营 / 关键词组就是这形态）
    db.execute(
        text(
            "INSERT INTO sys_menus (parent_id, name, menu_type, path, component, perms, icon, "
            "workspace, sort_order, is_visible, status, created_at, updated_at) "
            "VALUES (99999, '新建监控', 'F', '', '', 'xhs:monitor:create', '', '', 0, true, "
            "'normal', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )
    db.flush()

    #: `op.get_bind()` 依赖 alembic 运行上下文 ⇒ 用 MigrationContext + Operations 桥接一次，
    #: 让 `upgrade()` 原样执行（复制一份 SQL 到测试里就等于测测试自己）。
    context = MigrationContext.configure(db.connection())
    with Operations.context(context):
        mig.upgrade()

    #: 先查出**账号页自己的 id** 再按 id 定位：按 `path` 查会因「同 path 还有另一行
    #: 挂着别的权限码」拿到不确定的一行（真库里账号页就有 `xhs:login:view` 那一行）。
    page_id = db.scalar(
        text("SELECT id FROM sys_menus WHERE path = '/platforms/xhs/accounts' AND perms = 'xhs:account:view'")
    )
    row = db.execute(
        text("SELECT name, parent_id, sort_order FROM sys_menus WHERE id = :pid"), {"pid": page_id}
    ).first()
    assert row[0] == "账号管理", "迁移没改名"
    #: ⚠️ 用「父行是谁」判，不用 `parent_id == root_id`：两者都是小整数，
    #: 断言相等时「迁移压根没改父级」也能成立（实测就这样漏掉过一条变异）。
    parent = db.execute(
        text("SELECT name, menu_type, parent_id FROM sys_menus WHERE id = :pid"), {"pid": row[1]}
    ).first()
    assert parent is not None, "账号页父行不存在"
    assert parent[0] == "小红书" and parent[1] == "M" and parent[2] == 0, (
        f"迁移没把账号页挂到工作区根（当前父行 = {parent[0]!r}）"
    )
    assert db.scalar(text("SELECT count(*) FROM sys_menus WHERE name = '平台' AND menu_type = 'M'")) == 0
    assert db.scalar(text("SELECT count(*) FROM sys_menus WHERE perms = 'xhs:monitor:create'")) == 0
