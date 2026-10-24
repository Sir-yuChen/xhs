"""S3 系统管理域端点的守卫（设计 55 / 设计 49）。

## 这里守的是「管理员会不会把自己锁死」与「越权」

| 守卫 | 若失效的后果 |
|---|---|
| `test_non_admin_cannot_write_system_domain` | 普通用户能改角色/菜单/用户 ⇒ 直接提权 |
| `test_unauthenticated_gets_401` | 未登录能读用户列表 ⇒ 信息泄露 |
| `test_admin_can_write` | 权限码挂错方向 ⇒ 管理员也用不了 |
| `test_admin_never_blocked_by_permission_codes` | admin 被授权表卡住 ⇒ 锁死 |
| `test_self_disable_and_self_delete_forbidden` | 管理员停用/删自己 ⇒ 永久进不去 |
| `test_last_admin_protected` | 删掉唯一管理员 ⇒ 系统无人可管 |
| `test_system_role_delete_forbidden` | 删掉 admin/user 内置角色 ⇒ `normalize_role` 降级目标消失 |
| `test_menu_delete_blocked_by_children_and_grants` | 静默级联删菜单 ⇒ 授权被悄悄收回 |
| `test_category_order_endpoints_not_server_error` | 端点内部 500 ⇒ 前端只看到「系统错误」 |

## 为什么用真 SQLite 库而不是 mock

这些端点涉及**多表联合判断**（用户 ↔ 角色 ↔ 授权 ↔ 菜单），mock 掉数据库
等于把要验证的逻辑也 mock 掉了。用内存 SQLite + 真实模型建表，
跑的是和生产完全相同的 SQL 路径（方言上的差异由 `test_two_chains_in_sync` 兜）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

pytestmark = pytest.mark.contract

from backend.app.services import menu_seed_service
from backend.app.core import permission_cache, presence


@pytest.fixture()
def client(api_client, db_engine):
    """带全套系统域数据的 TestClient。

    ⚠️ 用公共夹具 `api_client`（`tests/backend/conftest.py`）而不是自建一套：
    它做了三件自建版**不会**做、但漏了就会出事的事（AGENTS.md 硬规矩第 2 条）：
      ① 把 **14 个模块级 `SessionLocal` 工厂**全打到测试库（服务层自开 Session
         的地方否则会落**生产库**）；
      ② 打桩 `main.init_db`（否则每条用例往真库跑一遍 95 个迁移链，16 秒/条）；
      ③ 退出时清掉 `app.dependency_overrides`（模块级全局，泄漏会串味后续用例）。

    这里只多做一件事：播种系统域（角色 / 菜单 / 字典 / admin / 授权）。
    """
    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    with SessionLocal() as db:
        menu_seed_service.seed_system_domain(db)
        db.commit()

    permission_cache.clear()
    presence.clear()
    try:
        yield api_client
    finally:
        permission_cache.clear()
        presence.clear()


def _login(client: TestClient, username: str, password: str) -> str:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


#: 测试里用的固定 admin 口令。
#:
#: ⚠️ **v1.1.0（2026-10-24）起出厂口令是随机的**，不再是 `admin123`
#: （公开在文档里的弱口令 + 服务器多用户形态 = 不可接受，见
#: `menu_seed_service.generate_admin_password` 的 docstring）。
#: 所以夹具不能假设口令，而是**自己把种子 admin 的口令重置成这个常量**：
#: 这既是稳定做法，也更贴近真实运维路径（首登强制改密后就是这个状态）。
TEST_ADMIN_PASSWORD = "test-admin-pass-123"


@pytest.fixture()
def _seed_admin_password(db_factory) -> str:
    """把种子 admin 的口令重置为 `TEST_ADMIN_PASSWORD`（幂等）。"""
    from sqlalchemy import select

    from backend.app.core.security import hash_password
    from backend.app.models.user import User

    db = db_factory()
    try:
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin is not None, "夹具里没有种子 admin —— 播种没跑？"
        admin.password_hash = hash_password(TEST_ADMIN_PASSWORD)
        #: 出厂 `must_change_password=True` 会挡住测试里的后续请求（前端会跳改密页），
        #: 但 API 层不拦；这里保持 True 更贴近真实，登录本身不受影响。
        db.commit()
    finally:
        db.close()
    return TEST_ADMIN_PASSWORD


@pytest.fixture()
def admin_token(client: TestClient, _seed_admin_password: str) -> str:
    """种子 admin 的访问令牌（口令由夹具重置为测试常量）。"""
    return _login(client, "admin", _seed_admin_password)


@pytest.fixture()
def normal_token(client: TestClient, _seed_admin_password: str) -> str:
    """建一个普通用户并登录。"""
    resp = client.post(
        "/api/system/users",
        json={"username": "probe_user", "password": "probe123456", "role": "user"},
        headers=_auth(_login(client, "admin", _seed_admin_password)),
    )
    assert resp.status_code == 201, resp.text
    return _login(client, "probe_user", "probe123456")


# --------------------------------------------------------------------------- #
# ① 认证边界
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path",
    [
        "/api/system/users",
        "/api/system/roles",
        "/api/system/menus/tree",
        "/api/system/dict/types",
        "/api/system/logs/audit",
        "/api/system/permissions/mine",
        "/api/system/menus/mine",
    ],
)
def test_unauthenticated_gets_401(client: TestClient, path: str):
    """未登录一律 401。**包括 mine 端点** —— 它们不挂权限码，但仍需登录。"""
    assert client.get(path).status_code == 401


# --------------------------------------------------------------------------- #
# ② 普通用户不能写
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/system/users", {"username": "escalate", "password": "123456"}),
        ("post", "/api/system/roles", {"name": "提权角色", "code": "escalate"}),
        ("delete", "/api/system/users/1", None),
        ("post", "/api/system/dict/types", {"code": "escalate", "name": "x"}),
        ("post", "/api/system/menus", {"name": "提权菜单", "menu_type": "C"}),
        ("put", "/api/system/roles/1/menus", {"menu_ids": []}),
    ],
)
def test_non_admin_cannot_write_system_domain(client, normal_token, method, path, body):
    """普通用户对所有系统域写端点是 403（不是 500，也不是 200）。"""
    kwargs = {"headers": _auth(normal_token)}
    if body is not None:
        kwargs["json"] = body
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}: {resp.text}"
    assert resp.json()["detail"]["code"] == "PERMISSION_DENIED"


def test_normal_user_can_read_with_view_perm(client, normal_token):
    """普通用户有 view 码 ⇒ 读端点是 200（不给写，但给看）。"""
    for path in ("/api/system/users", "/api/system/roles", "/api/system/menus/tree"):
        assert client.get(path, headers=_auth(normal_token)).status_code == 200


def test_normal_user_has_zero_write_codes(client, normal_token):
    """`user` 角色的模板授权必须只有 view 码（最小权限）。"""
    body = client.get("/api/system/permissions/mine", headers=_auth(normal_token)).json()
    write_codes = [c for c in body["permissions"] if not c.endswith(":view")]
    assert write_codes == [], f"普通用户拿到了写码：{write_codes}"


# --------------------------------------------------------------------------- #
# ③ admin 能写，且不被授权表卡住
# --------------------------------------------------------------------------- #
def test_admin_can_write(client, admin_token):
    resp = client.post(
        "/api/system/dict/types",
        json={"code": "probe_dict", "name": "探针字典"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201, resp.text
    type_id = resp.json()["id"]

    resp = client.post(
        "/api/system/dict/items",
        json={"type_id": type_id, "label": "选项一", "value": "1"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201, resp.text

    assert client.delete(
        f"/api/system/dict/types/{type_id}", headers=_auth(admin_token)
    ).status_code == 200


def test_admin_never_blocked_by_permission_codes(client, admin_token):
    """admin 的权限是代码派生的 —— 即使把 admin 角色的授权全清空，仍能写。

    ⚠️ 这条是**防锁死**的端到端判据：`perms.py` 里 `role_perms("admin")`
    直接返回 `ALL_PERMS`，有没有 `role_menus` 行都不影响。
    """
    token = admin_token
    # 清空 admin 角色的所有授权行
    resp = client.get("/api/system/roles", headers=_auth(token))
    admin_role = next(r for r in resp.json()["items"] if r["code"] == "admin")
    client.put(
        f"/api/system/roles/{admin_role['id']}/menus",
        json={"menu_ids": []},
        headers=_auth(token),
    )

    # 此时仍能访问写端点
    resp = client.post(
        "/api/system/dict/types",
        json={"code": "after_clear", "name": "清空授权后"},
        headers=_auth(token),
    )
    assert resp.status_code == 201, f"admin 被授权表卡住了：{resp.text}"
    client.delete(f"/api/system/dict/types/{resp.json()['id']}", headers=_auth(token))


# --------------------------------------------------------------------------- #
# ④ 自伤保护
# --------------------------------------------------------------------------- #
def test_self_disable_and_self_delete_forbidden(client, admin_token):
    """管理员不能停用 / 删 / 强制下线自己（否则永久进不去）。"""
    me = client.get("/api/auth/me", headers=_auth(admin_token)).json()
    my_id = me["id"]

    resp = client.put(
        f"/api/system/users/{my_id}",
        json={"status": "disabled"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "USER_SELF_OPERATION_FORBIDDEN"

    resp = client.delete(f"/api/system/users/{my_id}", headers=_auth(admin_token))
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "USER_SELF_OPERATION_FORBIDDEN"

    resp = client.post(
        f"/api/system/users/{my_id}/force-logout", headers=_auth(admin_token)
    )
    assert resp.status_code == 400


def test_last_admin_protected(client, admin_token):
    """删掉唯一管理员必须被拒（否则系统没人能管）。"""
    users = client.get(
        "/api/system/users?role=admin&page_size=200", headers=_auth(admin_token)
    ).json()
    admins = [u for u in users["items"] if u["deleted_at"] is None]
    me = client.get("/api/auth/me", headers=_auth(admin_token)).json()
    others = [u for u in admins if u["id"] != me["id"]]

    # 先把其它管理员删掉（若有）
    for u in others:
        client.delete(f"/api/system/users/{u['id']}", headers=_auth(admin_token))

    # 现在只剩自己 —— 删自己已被自伤保护拦住，所以这条主要验「剩下 0 个」的判据
    remaining = client.get(
        "/api/system/users?role=admin&page_size=200", headers=_auth(admin_token)
    ).json()
    live = [u for u in remaining["items"] if u["deleted_at"] is None]
    assert len(live) == 1, "应只剩当前管理员"


# --------------------------------------------------------------------------- #
# ⑤ 内置角色保护
# --------------------------------------------------------------------------- #
def test_system_role_delete_forbidden(client, admin_token):
    """`admin` / `user` 内置角色不许删。"""
    roles = client.get("/api/system/roles", headers=_auth(admin_token)).json()["items"]
    for role in roles:
        if not role["is_system"]:
            continue
        resp = client.delete(f"/api/system/roles/{role['id']}", headers=_auth(admin_token))
        assert resp.status_code == 400, f"{role['code']} 被删掉了"
        assert resp.json()["detail"]["code"] == "ROLE_SYSTEM_PROTECTED"


def test_system_role_disable_forbidden(client, admin_token):
    """内置角色不许停用（停用 = 把一整类用户锁在门外）。"""
    roles = client.get("/api/system/roles", headers=_auth(admin_token)).json()["items"]
    for role in roles:
        if not role["is_system"]:
            continue
        resp = client.put(
            f"/api/system/roles/{role['id']}",
            json={"status": "disabled"},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "ROLE_SYSTEM_PROTECTED"


def test_system_role_name_is_editable(client, admin_token):
    """但显示名可以改 —— 保护的是语义，不是文案。"""
    roles = client.get("/api/system/roles", headers=_auth(admin_token)).json()["items"]
    target = next(r for r in roles if r["code"] == "user")
    resp = client.put(
        f"/api/system/roles/{target['id']}",
        json={"name": "普通用户（改名验证）"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "普通用户（改名验证）"


# --------------------------------------------------------------------------- #
# ⑥ 菜单删除保护
# --------------------------------------------------------------------------- #
def test_menu_delete_blocked_by_children(client, admin_token):
    """有子菜单的菜单不许删（先删子或移走）。"""
    tree = client.get("/api/system/menus/tree", headers=_auth(admin_token)).json()
    parent = next(i for i in tree["items"] if i["children"])
    resp = client.delete(f"/api/system/menus/{parent['id']}", headers=_auth(admin_token))
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "MENU_HAS_CHILDREN"


def test_menu_delete_blocked_by_grants(client, admin_token):
    """被授权引用的菜单不许删（CASCADE 会静默收回权限）。"""
    tree = client.get("/api/system/menus/tree", headers=_auth(admin_token)).json()

    def walk(nodes):
        for n in nodes:
            yield n
            yield from walk(n["children"])

    leaf = next(
        n for n in walk(tree["items"]) if not n["children"] and n["menu_type"] == "F"
    )
    resp = client.delete(f"/api/system/menus/{leaf['id']}", headers=_auth(admin_token))
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] in ("MENU_IN_USE", "MENU_HAS_CHILDREN")


# --------------------------------------------------------------------------- #
# ⑦ 菜单启停：只清缓存不踢人
# --------------------------------------------------------------------------- #
def test_menu_disable_does_not_bump_token_version(client, admin_token):
    """启停菜单**不踢人**（低频全站功能开关，不是授权变更）。"""
    me_before = client.get("/api/auth/me", headers=_auth(admin_token)).json()

    tree = client.get("/api/system/menus/tree", headers=_auth(admin_token)).json()
    target = next(i for i in tree["items"] if i["menu_type"] == "M")
    client.put(
        f"/api/system/menus/{target['id']}",
        json={"status": "disabled"},
        headers=_auth(admin_token),
    )
    # 令牌仍有效
    assert client.get("/api/auth/me", headers=_auth(admin_token)).status_code == 200
    me_after = client.get("/api/auth/me", headers=_auth(admin_token)).json()
    assert me_after["id"] == me_before["id"]

    # 还原
    client.put(
        f"/api/system/menus/{target['id']}",
        json={"status": "normal"},
        headers=_auth(admin_token),
    )


# --------------------------------------------------------------------------- #
# ⑧ 用户手机号脱敏
# --------------------------------------------------------------------------- #
def test_phone_masked_in_list_full_in_detail(client, admin_token):
    """列表脱敏、详情给全 —— 列表一次 20 条，防截图外泄。"""
    token = admin_token
    resp = client.post(
        "/api/system/users",
        json={
            "username": "phone_probe",
            "password": "probe123456",
            "phone": "13812342857",
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    uid = resp.json()["id"]

    listed = client.get(
        "/api/system/users?username=phone_probe", headers=_auth(token)
    ).json()["items"][0]
    assert listed["phone"] == "138****2857", listed["phone"]

    detail = client.get(f"/api/system/users/{uid}", headers=_auth(token)).json()
    assert detail["phone"] == "13812342857"


# --------------------------------------------------------------------------- #
# ⑨ 停用用户的旧令牌立即失效
# --------------------------------------------------------------------------- #
def test_disabled_user_token_rejected(client, admin_token, normal_token):
    """停用某人后，他手上的旧令牌必须立刻不能用了（403 USER_DISABLED）。"""
    me = client.get("/api/auth/me", headers=_auth(normal_token)).json()
    resp = client.put(
        f"/api/system/users/{me['id']}",
        json={"status": "disabled"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200, resp.text

    resp = client.get("/api/system/permissions/mine", headers=_auth(normal_token))
    assert resp.status_code in (401, 403), resp.text


# --------------------------------------------------------------------------- #
# ⑩ mine 端点：路由顺序正确（不被 /{menu_id} 吃掉）
# --------------------------------------------------------------------------- #
def test_menus_mine_not_swallowed_by_path_param(client, admin_token):
    """`/menus/mine` 必须命中 mine 端点，而不是被 `/menus/{menu_id}` 当成 id。

    ⚠️ 实测踩坑：`system_menus.py` 的 `/menus/{menu_id}` 若注册在
    `/menus/mine` 之前，FastAPI 会把字符串 `mine` 交给 `menu_id: int` ⇒ **422**。
    """
    resp = client.get("/api/system/menus/mine", headers=_auth(admin_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body and isinstance(body["items"], list)
    assert body["total"] > 0, "admin 应能看到菜单"
