"""工作区选择页（设计 62 §2.2）与侧栏工作区隔离的端点测试。

三个契约：
1. `GET /system/menus/workspaces` —— 卡片动态下发：正常工作区按授权可进入，
   占位平台（disabled）保留为不可进入的卡片，用户无授权的工作区不出现；
2. `menus/mine?workspace=` —— 侧栏按工作区隔离：小红书工作区看不到
   系统管理 / 大模型网关，反之亦然；
3. `/profile` 行 workspace=common —— 不进任何工作区侧栏（入口在头像下拉）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.core import permission_cache, presence

import pytest

#: 测试分层标记（§15.5）：端点契约 —— 动态工作区卡片 / 侧栏隔离。
pytestmark = pytest.mark.contract
from backend.app.services import menu_seed_service

TEST_ADMIN_PASSWORD = "test-admin-pass-123"


@pytest.fixture()
def seeded_client(api_client, db_engine):
    """播种系统域（角色 / 菜单 / 字典 / admin / 授权）+ 把 admin 口令重置为已知值。

    ⚠️ 顺序敏感：**先播种（admin 行由播种创建）、再重置口令** ——
    不能拆成两个夹具（pytest 的夹具解析顺序不保证「重置」晚于「播种」，
    实测 `_seed_admin_password` 先跑会拿到空表）。
    """
    from backend.app.core.security import hash_password
    from backend.app.models.user import User

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    with SessionLocal() as db:
        menu_seed_service.seed_system_domain(db)
        admin = db.query(User).filter(User.username == "admin").first()
        assert admin is not None, "播种没建出 admin —— seed_system_domain 失效？"
        admin.password_hash = hash_password(TEST_ADMIN_PASSWORD)
        db.commit()

    permission_cache.clear()
    presence.clear()

    holder = type("SeededClient", (), {"client": api_client, "admin_password": TEST_ADMIN_PASSWORD, "engine": db_engine})()
    try:
        yield holder
    finally:
        permission_cache.clear()
        presence.clear()


def _login(client: TestClient, username: str, password: str) -> str:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _workspaces(client: TestClient, token: str) -> dict[str, dict]:
    resp = client.get("/api/system/menus/workspaces", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return {item["workspace"]: item for item in resp.json()["items"]}


def test_workspaces_lists_roots_with_enterable_flags(seeded_client):
    """admin：3 个正常工作区可进入；5 个占位平台在列但不可进入（Coming Soon）。"""
    client = seeded_client.client
    token = _login(client, "admin", seeded_client.admin_password)
    cards = _workspaces(client, token)

    # 2026-10-25 用户口径「停用的就不应该再返回给前端展示」：
    # 占位平台（disabled）不再下发 —— 工作区页只有启用中的工作区。
    assert set(cards) == {"xhs", "ai", "system"}
    for ws in ("xhs", "ai", "system"):
        assert cards[ws]["enterable"] is True, f"{ws} 对 admin 应可进入"
        assert cards[ws]["status"] == "normal"
    assert cards["xhs"]["name"] == "小红书"
    assert cards["system"]["name"] == "系统管理"


def test_workspaces_names_and_icons_come_from_the_menu_rows(seeded_client):
    """卡片字段（名称 / 图标）来自菜单行 —— 「菜单管理」页改名后卡片跟着变。"""
    from backend.app.models.menu import Menu

    client = seeded_client.client
    db = sessionmaker(autocommit=False, autoflush=False, bind=seeded_client.engine)()
    try:
        row = db.query(Menu).filter(Menu.workspace == "system", Menu.menu_type == "M").first()
        row.name = "系统运维"
        db.commit()
    finally:
        db.close()
    permission_cache.clear()

    token = _login(client, "admin", seeded_client.admin_password)
    cards = _workspaces(client, token)
    assert cards["system"]["name"] == "系统运维"


def test_mine_workspace_filter_isolates_platforms(seeded_client):
    """侧栏隔离：xhs 切片无系统管理/大模型网关/个人资料；system 切片只有系统管理树。"""
    client = seeded_client.client
    token = _login(client, "admin", seeded_client.admin_password)

    resp = client.get("/api/system/menus/mine", params={"workspace": "xhs"}, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    xhs_items = resp.json()["items"]
    names = {n["name"] for n in xhs_items}
    # 设计 62 §2.5：工作区根行（平台名）不下发 —— 侧栏直接从目录/页面层开始
    assert "小红书" not in names, "工作区内不再重复展示平台名（根行已摘除）"
    assert {"平台", "内容线", "店铺线", "总览"} <= names, "目录组与顶级页应顶到一级"
    assert "系统管理" not in names, "xhs 工作区不得混入系统管理（设计 62 隔离）"
    assert "大模型网关" not in names, "xhs 工作区不得混入大模型网关"
    paths = set()
    for n in xhs_items:
        if n.get("path"):
            paths.add(n["path"])
        for child in n.get("children") or []:
            if child.get("path"):
                paths.add(child["path"])
    assert "/profile" not in paths, "个人资料不进任何工作区侧栏（头像下拉直达）"

    resp = client.get("/api/system/menus/mine", params={"workspace": "system"}, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    sys_items = resp.json()["items"]
    sys_names = {n["name"] for n in sys_items}
    assert "系统管理" not in sys_names, "system 工作区根行同样不下发"
    assert {"用户管理", "角色管理"} <= sys_names, "系统域页面应顶到一级"
    assert "小红书" not in sys_names, "system 工作区不得混入小红书"


def test_mine_without_workspace_still_returns_full_tree_for_menu_admin(seeded_client):
    """不带 workspace = 菜单管理页的全树视图，行为不变（向后兼容）。"""
    client = seeded_client.client
    token = _login(client, "admin", seeded_client.admin_password)
    resp = client.get("/api/system/menus/mine", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    names = {n["name"] for n in resp.json()["items"]}
    assert {"小红书", "系统管理", "大模型网关"} <= names
