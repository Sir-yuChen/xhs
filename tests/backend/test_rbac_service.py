"""S2 服务层的守卫 —— RBAC 解析 / 缓存失效 / 权限依赖（设计 50 §3）。

## 这里守的是「越权」和「锁死」这两类后果

S1 的守卫守的是数据形状（种子不覆盖、两张链不漂移）。S2 的守卫守的是**运行时行为**：

| 守卫 | 若失效的真实后果 |
|---|---|
| `test_admin_never_reads_role_menus` | 管理员误点「取消授权」⇒ 永久失去管理入口 ⇒ 只能改库救 |
| `test_user_perms_are_isolated_between_users` | A 的自定义授权漏给 B ⇒ 越权（最隐蔽的一类） |
| `test_unknown_role_gets_nothing` | 角色字段写错 ⇒ 拿到全量权限 |
| `test_cache_key_includes_token_version` | 授权改了不生效，用户要等 TTL 到期 |
| `test_require_perm_rejects_unregistered_code_at_import` | 写错码 ⇒ 端点对所有人静默关闭 |
| `test_template_rows_are_inherited_by_new_users` | 新注册用户登录后侧栏空白 |

## 为什么不用真 PG

这些全是**纯逻辑**判据（集合运算、键构造、异常类型），用内存 SQLite 就够，
而且快。真 PG 的结构一致性由 `test_two_chains_in_sync` 覆盖，不在这里重复。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core import perm_deps
from backend.app.core.perms import ALL_PERMS
from backend.app.models.user import User as _UserRow
from backend.app.services import permission_cache, rbac_service


# --------------------------------------------------------------------------- #
# 夹具：一个最小的 RBAC 库（角色 2 + 菜单 4 + 授权）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def rbac_db():
    engine = create_engine("sqlite://")
    from backend.app.core.database import Base
    import backend.app.models  # noqa: F401  注册全部模型

    # 只建本测试需要的表（全建太慢，且与本模块无关）
    names = ["users", "roles", "menus", "role_menus"]
    Base.metadata.create_all(bind=engine, tables=[Base.metadata.tables[n] for n in names])
    SessionLocal = sessionmaker(bind=engine)
    session: Session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个用例前后清缓存 —— 进程级全局状态会串味。"""
    permission_cache.clear()
    yield
    permission_cache.clear()


def _seed(session: Session) -> dict:
    """建两个角色 + 若干菜单 + 授权。返回 id 表。"""
    from backend.app.models.menu import Menu
    from backend.app.models.role import Role
    from backend.app.models.role_menu import RoleMenu

    admin_role = Role(name="超级管理员", code="admin", is_system=True, status="normal")
    user_role = Role(name="普通用户", code="user", is_system=True, status="normal")
    session.add_all([admin_role, user_role])
    session.flush()

    perms = ["xhs:item:view", "xhs:item:edit", "system:user:view"]
    menus = []
    for i, code in enumerate(perms):
        m = Menu(
            parent_id=0, name=code, menu_type="F", perms=code,
            path="", component="", icon="", workspace="xhs", sort_order=i,
            is_visible=True, status="normal",
        )
        session.add(m)
        menus.append(m)
    session.flush()

    # user 角色模板行：只有第一条 view 码
    session.add(RoleMenu(user_id=None, role_id=user_role.id, menu_id=menus[0].id))
    session.commit()
    return {
        "admin_role": admin_role.id,
        "user_role": user_role.id,
        "menus": [m.id for m in menus],
        "perms": perms,
    }


class _U:
    """轻量 User 替身（只要 id/role/token_version 三个属性）。

    ⚠️ 只用于**纯逻辑**断言。凡是要往 `role_menus` 插 user 行的用例，
    必须先 `_mk_user()` 建真实用户 —— `role_menus.user_id` 有外键，
    拿不存在的 id 去插会 `FOREIGN KEY constraint failed`（实测踩到）。
    """

    def __init__(self, uid: int, role: str, version: int = 0):
        self.id = uid
        self.role = role
        self.token_version = version


def _mk_user(session: Session, username: str, role: str) -> int:
    """建一个真实用户行，返回 id（满足 `role_menus.user_id` 的外键）。"""
    from backend.app.models.user import User

    user = User(username=username, password_hash="x", role=role)
    session.add(user)
    session.commit()
    return user.id


# --------------------------------------------------------------------------- #
# ① admin 永不查授权表（防锁死）
# --------------------------------------------------------------------------- #
def test_admin_never_reads_role_menus(rbac_db):
    """admin 的权限 = `ALL_PERMS`，即使 `role_menus` 一行都没有。

    若这条失效（改成查库），管理员在界面上取消掉自己的授权后
    就**永久**进不去系统了 —— 这是「防锁死」的唯一保证。
    """
    info = _seed(rbac_db)
    # admin 角色没有任何授权行
    n = rbac_db.execute(
        text("select count(*) from role_menus where role_id = :r"),
        {"r": info["admin_role"]},
    ).scalar()
    assert n == 0

    assert rbac_service.perms_for_role(rbac_db, "admin") == ALL_PERMS
    assert rbac_service.perms_for_user(rbac_db, _U(1, "admin")) == ALL_PERMS


# --------------------------------------------------------------------------- #
# ② 用户之间的授权隔离
# --------------------------------------------------------------------------- #
def test_user_perms_are_isolated_between_users(rbac_db):
    """A 的额外授权不能漏给 B（`role_menus.user_id` 过滤的真实判据）。"""
    from backend.app.models.role_menu import RoleMenu

    info = _seed(rbac_db)
    uid_a = _mk_user(rbac_db, "alice", "user")
    uid_b = _mk_user(rbac_db, "bob", "user")
    # 给 A 单独加授第三条菜单
    rbac_db.add(
        RoleMenu(user_id=uid_a, role_id=info["user_role"], menu_id=info["menus"][2])
    )
    rbac_db.commit()

    a = rbac_service.perms_for_user(rbac_db, rbac_db.get(_UserRow, uid_a))
    b = rbac_service.perms_for_user(rbac_db, rbac_db.get(_UserRow, uid_b))

    assert "system:user:view" in a, "A 自己该有"
    assert "system:user:view" not in b, "B 不能因为 A 有而跟着有"
    # 模板行的那条两人都有
    assert "xhs:item:view" in a and "xhs:item:view" in b


# --------------------------------------------------------------------------- #
# ③ 未知角色 / 未登录 一律空集
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["bogus", "", None, "  "])
def test_unknown_role_degrades_to_default_role(rbac_db, role):
    """未知 / 空 / None 角色一律降级为 `DEFAULT_ROLE`（= `user`），**不是**全量。

    ⚠️ 这里的期望值曾经写错成「空集」。真实设计（`core/roles.py:normalize_role`）
    是**保守降级**：`role` 是权限开关，猜错只该「少给权限」。降级到 `user`
    意味着拿到 `user` 的模板权限（本夹具里 1 条 view 码），比「管理员」安全得多。

    这条守卫真正要防的是**另一个方向**：降级绝不能变成 `admin`。
    """
    _seed(rbac_db)
    for resolver in (
        rbac_service.perms_for_role,
        lambda db, r: rbac_service.perms_for_user(db, _U(1, r)),
    ):
        got = resolver(rbac_db, role)
        assert got != ALL_PERMS, "未知角色绝不能降级成管理员（越权）"
        assert got == rbac_service.perms_for_role(rbac_db, "user"), (
            "未知角色应降级为默认角色 user"
        )


def test_none_user_gets_nothing(rbac_db):
    """未登录（`user is None`）⇒ 空集。这与「未知角色」是两件事。"""
    _seed(rbac_db)
    assert rbac_service.perms_for_user(rbac_db, None) == frozenset()


def test_unknown_role_has_no_admin_powers(rbac_db):
    """单独再钉一次最危险的边界：未知角色不得拿到任何写码。"""
    _seed(rbac_db)
    for role in ("bogus", "", None):
        got = rbac_service.perms_for_role(rbac_db, role)
        write_codes = {c for c in got if not c.endswith(":view")}
        assert write_codes == set(), f"未知角色 {role!r} 拿到了写权限 {write_codes}"


# --------------------------------------------------------------------------- #
# ④ 库里残留的旧码不生效
# --------------------------------------------------------------------------- #
def test_stale_perms_in_db_are_filtered_out(rbac_db):
    """历史授权里可能留着已退场的码 —— 必须被过滤掉。

    否则 `has_perm` 的「未登记一律 False」形同虚设（库里有什么就认什么）。
    """
    from backend.app.models.menu import Menu
    from backend.app.models.role_menu import RoleMenu

    info = _seed(rbac_db)
    stale = Menu(
        parent_id=0, name="legacy", menu_type="F", perms="legacy:gone:edit",
        path="", component="", icon="", workspace="xhs", sort_order=99,
        is_visible=True, status="normal",
    )
    rbac_db.add(stale)
    rbac_db.flush()
    rbac_db.add(RoleMenu(user_id=None, role_id=info["user_role"], menu_id=stale.id))
    rbac_db.commit()

    got = rbac_service.perms_for_role(rbac_db, "user")
    assert "legacy:gone:edit" not in got
    assert "xhs:item:view" in got


# --------------------------------------------------------------------------- #
# ⑤ 缓存：键含 token_version
# --------------------------------------------------------------------------- #
def test_cache_key_includes_token_version(rbac_db):
    """改授权时 `token_version += 1` ⇒ 新 JWT 必须拿到新权限，不能命中旧缓存。

    若键里不含 `token_version`，后果是「授权改了但用户看不到」，
    且要等 `TTL_SECONDS` 才恢复 —— 实测最难排查的一类。
    """
    from backend.app.models.role_menu import RoleMenu

    info = _seed(rbac_db)
    uid = _mk_user(rbac_db, "carol", "user")
    user = _U(uid, "user", version=0)

    first = permission_cache.perms_of(rbac_db, user)
    assert "xhs:item:edit" not in first

    # 加授权 + 版本号 +1（真实写路径就这么做）
    rbac_db.add(
        RoleMenu(user_id=None, role_id=info["user_role"], menu_id=info["menus"][1])
    )
    rbac_db.commit()
    user.token_version = 1

    second = permission_cache.perms_of(rbac_db, user)
    assert "xhs:item:edit" in second, "token_version 变了必须重新查库"


def test_cache_hits_and_clear(rbac_db):
    """缓存确实命中（不是每次都查库），且 `clear` 能失效。"""
    _seed(rbac_db)
    permission_cache.clear()
    uid = _mk_user(rbac_db, "dave", "user")
    user = _U(uid, "user", version=0)

    permission_cache.perms_of(rbac_db, user)
    before = permission_cache.stats()
    for _ in range(10):
        permission_cache.perms_of(rbac_db, user)
    after = permission_cache.stats()
    assert after["hits"] > before["hits"], "重复取同一用户应命中缓存"

    cleared = permission_cache.clear(user_id=uid)
    assert cleared >= 1
    assert permission_cache.stats()["entries"] == 0


def test_admin_never_enters_the_cache(rbac_db):
    """admin 的权限是常量，不该占缓存（也不该被 clear 影响）。"""
    _seed(rbac_db)
    permission_cache.clear()
    permission_cache.perms_of(rbac_db, _U(1, "admin"))
    assert permission_cache.stats()["entries"] == 0
    assert permission_cache.perms_of(rbac_db, _U(1, "admin")) == ALL_PERMS


# --------------------------------------------------------------------------- #
# ⑥ require_perm：未登记码在 import 期就炸
# --------------------------------------------------------------------------- #
def test_require_perm_rejects_unregistered_code_at_import():
    """写错权限码必须在**构造依赖时**就抛，而不是等请求到达。

    若静默 403，那个端点等于对所有人（含管理员）关闭，
    排查时只会看到「管理员也点不了」，误导方向。
    """
    with pytest.raises(RuntimeError, match="未登记"):
        perm_deps.require_perm("not:a:real:code")


def test_require_perm_exposes_code_for_route_scanning():
    """`route_perms.py` 的启动期扫描靠这个属性把「路由 → 码」反射出来。"""
    code = sorted(ALL_PERMS)[0]
    dep = perm_deps.require_perm(code)
    assert getattr(dep, "__perm_code__", None) == code


# --------------------------------------------------------------------------- #
# ⑦ 模板行 → 新用户继承
# --------------------------------------------------------------------------- #
def test_template_rows_are_inherited_by_new_users(rbac_db):
    """新用户（无任何自己的授权行）必须继承模板行。

    若失效 ⇒ 新注册用户登录后**侧栏空白**、直输 URL 403。
    """
    info = _seed(rbac_db)
    fresh = _U(99999, "user")  # 库里没有这个人，但有 user 角色
    got = rbac_service.perms_for_user(rbac_db, fresh)
    assert "xhs:item:view" in got
    assert got == rbac_service.perms_for_role(rbac_db, "user")


# --------------------------------------------------------------------------- #
# ⑧ set_role_menus：整体替换语义
# --------------------------------------------------------------------------- #
def test_set_role_menus_replaces_not_appends(rbac_db):
    """授权是**集合**语义：传什么就是什么，不是增量叠加。"""
    info = _seed(rbac_db)
    role_id = info["user_role"]

    rbac_service.set_role_menus(rbac_db, role_id, [info["menus"][0], info["menus"][1]])
    rbac_db.commit()
    assert rbac_service.perms_for_role(rbac_db, "user") == {
        "xhs:item:view", "xhs:item:edit",
    }

    rbac_service.set_role_menus(rbac_db, role_id, [info["menus"][2]])
    rbac_db.commit()
    assert rbac_service.perms_for_role(rbac_db, "user") == {"system:user:view"}


def test_set_role_menus_with_empty_list_clears(rbac_db):
    info = _seed(rbac_db)
    rbac_service.set_role_menus(rbac_db, info["user_role"], [])
    rbac_db.commit()
    assert rbac_service.perms_for_role(rbac_db, "user") == frozenset()
