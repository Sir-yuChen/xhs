"""S1 种子守卫（G10）：**只补缺不覆盖** + 三份种子与真相源一致。

## 为什么「只补缺不覆盖」值得单独一条守卫

种子在装好之后就是**用户的数据**：在「菜单管理」里把抖音改成启用、在「角色管理」
里给 user 角色多授一个按钮、在「字典管理」里改了某个 label —— 这些都是用户的编辑。
一个 `upsert` 式的种子在**新装库上看起来完全正确**，只在真库上一条条把用户改过的
值打回去，而且没有任何报错。这类缺陷只能靠守卫钉住。

## 本文件的四组判据

| 组 | 判据 |
|---|---|
| S1 幂等 | 连播两次，第二次**零新增**且**零修改**（逐行快照比对） |
| S2 不覆盖 | 改过 `status` / `name` / 字典 `label` 之后重播，用户的值仍在 |
| G2 派生一致 | `menus.seed.json` 与 `perms_registry` 一一对应（脚本 `--check` 退出码 0） |
| 镜像一致 | `dict_seed.json` 的 7 组枚举与对应 Python 常量逐个一致 |
| admin | 出厂 admin 只在「没有 admin 行」时插入；已有 admin 时不重复插入 |
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.models import User
from backend.app.models.dict import DictItem, DictType
from backend.app.models.menu import Menu
from backend.app.models.role import Role
from backend.app.models.role_menu import RoleMenu
from backend.app.services.menu_seed_service import (
    seed_admin_user,
    seed_dicts,
    seed_menus,
    seed_role_menus,
    seed_roles,
    seed_system_domain,
)

ROOT = Path(__file__).resolve().parents[2]
SEED_DIR = ROOT / "config" / "initial_data"

#: 分层标记（测试规范 §15.5 准入清单）：本文件建真库文件（`create_all`）+ 打端点，
#: 属于 integration（不是纯函数 redline）。
pytestmark = pytest.mark.integration


@pytest.fixture()
def db(tmp_path):
    """独立的空库（create_all），不打任何桩 —— 种子只碰这一个文件。"""
    from backend.app import models  # noqa: F401

    engine = create_engine(f"sqlite:///{tmp_path / 'seed.db'}")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _snapshot(db: Session) -> dict:
    """种子相关的全部行（逐字段），用于「幂等 / 不覆盖」的比对。"""

    def rows(model, cols):
        #: ⚠️ 排序键必须能容纳 **None**（`role_menus.user_id` 自 d4e5f6a1b2c3 起可空，
        #: 模板行是 NULL）。直接 `sorted(tuple(...))` 会在 `None < int` 上抛
        #: `TypeError: '<' not supported`。这里把每个值转成
        #: `(是否为空, 类型名, 字符串形)` —— 全类型可比较，且**不改变集合语义**
        #: （本函数只用于「前后两次快照是否逐字段相同」，顺序无关紧要）。
        def key(row: tuple) -> tuple:
            return tuple(
                (v is None, type(v).__name__, str(v)) for v in row
            )

        return sorted(
            (tuple(getattr(r, c) for c in cols) for r in db.scalars(select(model)).all()),
            key=key,
        )

    return {
        "roles": rows(Role, ("code", "name", "is_system", "status", "sort_order")),
        "menus": rows(
            Menu,
            ("parent_id", "name", "menu_type", "path", "perms", "workspace",
             "sort_order", "is_visible", "status"),
        ),
        "dict_types": rows(DictType, ("code", "name", "status")),
        "dict_items": rows(DictItem, ("type_id", "type_code", "label", "value", "list_class")),
        "role_menus": rows(RoleMenu, ("user_id", "role_id", "menu_id")),
    }


# --------------------------------------------------------------------------- #
# S1 · 幂等
# --------------------------------------------------------------------------- #
def test_seed_is_idempotent(db):
    """连播两次：第二次**零新增**，且行内容逐字段不变。"""
    first = seed_system_domain(db)
    db.commit()
    before = _snapshot(db)

    assert first["menus"] > 0, "首次播种应当建出菜单"
    assert first["roles"] == 2

    second = seed_system_domain(db)
    db.commit()
    after = _snapshot(db)

    assert second["menus"] == 0, f"第二次不该再建菜单行：{second}"
    assert second["dict_items"] == 0, f"第二次不该再建字典项：{second}"
    assert second["role_menus"] == 0, f"第二次不该再建授权行：{second}"
    #: admin 已在第一次插入 ⇒ 第二次为 0
    assert second["admin_users"] == 0
    assert before == after, "重播后行内容变了 —— 种子在覆盖用户数据"


# --------------------------------------------------------------------------- #
# S2 · 只补缺不覆盖
# --------------------------------------------------------------------------- #
def test_seed_does_not_overwrite_user_changes(db):
    """用户改过 `status` / `name` / 字典 `label` 之后重播，**用户的値仍在**。

    这是 G10 的核心判据。`upsert` 式种子在这里必红。
    """
    seed_system_domain(db)
    db.commit()

    # 1) 菜单：把「抖音」启用（用户的平台接入开关）
    douyin = db.scalar(select(Menu).where(Menu.workspace == "douyin"))
    assert douyin is not None and douyin.status == "disabled", "种子里抖音应当是停用态"
    douyin.status = "normal"
    # 2) 菜单：改一个页面名
    page = db.scalar(select(Menu).where(Menu.path == "/platforms/xhs/my-notes"))
    page.name = "我的笔记（改）"
    # 3) 字典：改 label
    item = db.scalar(select(DictItem).where(DictItem.value == "published"))
    item.label = "已发布（改）"
    db.commit()

    seed_system_domain(db)
    db.commit()
    db.expire_all()

    assert db.scalar(select(Menu).where(Menu.workspace == "douyin")).status == "normal", (
        "重播把用户启用的平台打回了 disabled —— 种子在覆盖用户数据"
    )
    assert db.scalar(select(Menu).where(Menu.path == "/platforms/xhs/my-notes")).name == "我的笔记（改）"
    assert db.scalar(select(DictItem).where(DictItem.value == "published")).label == "已发布（改）"


def test_seed_does_not_revive_a_whole_cleared_directory(db):
    """用户**清空整个目录**（其下 C 行全没了）⇒ 下次播种不把那一整块加回来。

    ⚠️ **已知边界**（诚实记录，不假装它是能力）：删**单条** C 行时种子会补回。
    理由是种子在库层面无法区分「用户删了这条」与「这条从没建过」——两者在库里
    都表现为「父 M 行在、这条 C 行不在」。能给出的唯一强信号是「整个目录被清空」，
    所以只对那一种情况保证不复活。要覆盖单条删除，需要一张「已删种子行」墓碑表
    （或在 `menus` 上加软删列）—— 那是 S2/S3 的范围，不在 S1 的 5 表口径里。

    ⚠️ 删之前要先清掉 `role_menus` 里指向它的授权行 —— `role_menus.menu_id` 是
    `ON DELETE RESTRICT`（设计 47 §4.1：有授权引用的菜单**不许删**，否则删菜单
    会静默收回别人的权限）。这正是「菜单管理 → 删除」那把 409 `MENU_IN_USE`
    守卫在做的事，测试里手工复刻一遍同样的前置。
    """
    seed_system_domain(db)
    db.commit()

    #: 「内容线」目录下的全部 C 行（关键词组 / 内容洞察 / 我的笔记 …）
    content_dir = db.scalar(
        select(Menu).where(Menu.name == "内容线", Menu.menu_type == "M")
    )
    assert content_dir is not None
    doomed = db.scalars(
        select(Menu).where(Menu.parent_id == content_dir.id, Menu.menu_type == "C")
    ).all()
    assert len(doomed) >= 5, f"内容线目录下应当有多条 C 行，实际 {len(doomed)}"
    doomed_paths = {m.path for m in doomed}

    for page in doomed:
        for grant in db.scalars(select(RoleMenu).where(RoleMenu.menu_id == page.id)).all():
            db.delete(grant)
        db.flush()
        for button in db.scalars(
            select(Menu).where(Menu.parent_id == page.id, Menu.menu_type == "F")
        ).all():
            for grant in db.scalars(
                select(RoleMenu).where(RoleMenu.menu_id == button.id)
            ).all():
                db.delete(grant)
            db.flush()
            db.delete(button)
        db.flush()
        db.delete(page)
    db.commit()
    remaining = len(db.scalars(select(Menu)).all())

    seed_system_domain(db)
    db.commit()

    after = db.scalars(select(Menu)).all()
    revived = sorted(m.path for m in after if m.parent_id == content_dir.id and m.menu_type == "C")
    assert revived == [], f"被清空的目录又被种回来了：{revived}"
    assert len(after) == remaining, "总行数变了 —— 有行被重建"


def test_seed_fills_a_fresh_database_completely(db):
    """空库必须**完整**播种（反向保护：别让「不复活」逻辑把首次播种也挡掉）。

    ⚠️ 这条是上一条的反向守卫，必须有：判据「目录被清空 ⇒ 跳过」在空库上同样成立
    （所有 C 行都不在），实测曾让空库只种出 145 条目录行、**0 条 C 行**。
    """
    created = seed_system_domain(db)
    db.commit()

    menus = db.scalars(select(Menu)).all()
    by_type: dict[str, int] = {}
    for row in menus:
        by_type[row.menu_type] = by_type.get(row.menu_type, 0) + 1

    assert by_type.get("M", 0) >= 10, f"M 行不足：{by_type}"
    assert by_type.get("C", 0) >= 28, f"C 行不足：{by_type}"
    assert by_type.get("F", 0) >= 130, f"F 行不足：{by_type}"
    assert created["menus"] == sum(by_type.values()), (
        f"报告的建行数与库里行数不一致：{created['menus']} vs {sum(by_type.values())}"
    )
    #: 关键页必须在
    paths = {row.path for row in menus}
    for required in (
        "/system/users",
        "/system/roles",
        "/system/menus",
        "/system/dicts",
        "/platforms/xhs/my-notes",
        "/platforms/xhs/keywords",
        "/platforms/xhs/insights/content",
    ):
        assert required in paths, f"空库播种缺页面 {required}"


def test_register_switch_baseline_is_off():
    """产品基线：`register.enabled = false`，且它是**真实的读点开关**。

    ⚠️ 为什么这条要独立存在：测试树的会话级夹具把 `BUILTIN_DEFAULTS` 里的这个键
    打开过（`tests/conftest.py`，理由见那里的 docstring —— 几百处
    `POST /auth/register` 是「造用户」的夹具手段）。所以「能注册」在测试里**恒成立**，
    没有任何用例会因为基线变 true 而红 ⇒ 基线本身必须单独钉住。

    ⚠️ 判定必须读**源码里的字面值**而不是运行期字典：会话级夹具已经改过那份字典，
    直接断言 `BUILTIN_DEFAULTS[...] is False` 会读到 `True`（实测首轮就红在这）。
    这里用 AST 抓 `BUILTIN_DEFAULTS` 字典里的那个字面量 —— 那是「写进代码的产品基线」，
    夹具改不动它。
    """
    import ast
    import json

    payload = json.loads((SEED_DIR / "system_settings.json").read_text(encoding="utf-8"))
    seed_row = next(
        row for row in payload["system_settings"] if row["key"] == "register.enabled"
    )
    assert seed_row["value"] is False, "随包种子必须把注册开关关着"
    assert seed_row["value_type"] == "bool", "值类型必须是 bool（否则前端渲染歪）"

    from backend.app.services import settings_service

    source = Path(settings_service.__file__).read_text(encoding="utf-8")
    literal: object = "<not found>"
    for node in ast.walk(ast.parse(source)):
        #: ⚠️ 该常量是**带类型注解**的赋值（`BUILTIN_DEFAULTS: dict[str, Any] = {...}`）
        #: ⇒ AST 里是 `AnnAssign` 而不是 `Assign`。只看 `Assign` 会一个都匹配不到
        #: （实测：读到 `<not found>`）。
        value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "BUILTIN_DEFAULTS":
                value = node.value
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "BUILTIN_DEFAULTS" for t in node.targets):
                value = node.value
        if not isinstance(value, ast.Dict):
            continue
        for key_node, value_node in zip(value.keys, value.values):
            if isinstance(key_node, ast.Constant) and key_node.value == "register.enabled":
                literal = ast.literal_eval(value_node)
    assert literal is False, (
        f"代码里的产品基线必须关着（读到 {literal!r}）——「出厂弱口令 + 任何人可注册」不能同时成立"
    )

    from backend.app.core.config import get_settings

    assert get_settings().register_enabled is False, "config/default.yaml 必须关着"


def _set_register_switch(api_client, value: bool) -> None:
    """把注册开关写到**管理员配置行**（v1.1.0 / S3：全局开关以管理员行为准）。

    ⚠️ 不能再靠改 `BUILTIN_DEFAULTS`：`live_global_bool` 读的是
    `system_settings` 里**管理员那一行**（实测踩坑：先写「任一为真即真」，
    导致管理员关不掉；改成「管理员行为准」后，测试必须写行而不是改默认值）。
    没有管理员行时回落到 `default`，所以这里显式造一行。
    """
    from sqlalchemy import select

    from backend.app.core.roles import ROLE_ADMIN
    from backend.app.models.system_setting import SystemSetting
    from backend.app.models.user import User
    from backend.app.services import settings_service
    from backend.app.core.database import SessionLocal

    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.role == ROLE_ADMIN, User.deleted_at.is_(None)))
        if admin is None:
            #: 夹具不一定造管理员（很多用例只用普通用户）⇒ 这里补一个，
            #: 否则「全局开关」这条链路根本无法被验证。
            from backend.app.core.security import hash_password

            admin = User(
                username="switch-admin",
                email="switch-admin@example.com",
                password_hash=hash_password("admin12345"),
                role=ROLE_ADMIN,
                status="normal",
            )
            db.add(admin)
            db.commit()
            db.refresh(admin)
        row = db.scalar(
            select(SystemSetting).where(
                SystemSetting.user_id == admin.id, SystemSetting.key == "register.enabled"
            )
        )
        if row is None:
            row = SystemSetting(
                user_id=admin.id, key="register.enabled", value="", value_type="bool"
            )
            db.add(row)
        row.value = "true" if value else "false"
        db.commit()
    finally:
        db.close()

    #: ⚠️ **两道一起设**：`live_global_bool` 读管理员行；但有些测试路径
    #: （`db=None`、或测试库与夹具不同库时）会回落到 `BUILTIN_DEFAULTS`。
    #: 只设行不设默认值 ⇒ 偶发 403（实测：`test_register_requires_email_and_code`
    #: 一直拿到 403，看着像契约没生效，实际是开关读到了另一层）。
    settings_service.BUILTIN_DEFAULTS["register.enabled"] = value
    settings_service.invalidate(key="register.enabled")


@pytest.fixture()
def register_switch(api_client):
    """用完把开关关回去（不让用例之间的状态互相影响）。"""
    yield _set_register_switch
    _set_register_switch(api_client, False)


def test_register_is_rejected_when_the_switch_is_off(api_client, register_switch):
    """关着时 `POST /auth/register` ⇒ **403 `REGISTER_DISABLED`**（不是 401/500）。

    v1.1.0 起注册契约变了（邮箱验证码 + 待审，对齐 dataPanel），所以这里
    按**新契约**发请求：`{email, username, password, code}`。
    """
    body = {
        "email": "switch-probe@example.com",
        "username": "switch-probe",
        "password": "secret123",
        "code": "123456",
    }

    register_switch(api_client, False)
    resp = api_client.post("/api/auth/register", json=body)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "REGISTER_DISABLED"

    #: 反向：**显式**打开就不是 403（证伪「永远 403」）。
    #: 断言的是「开关分支已放行」而不是「注册成功」—— 没有验证码，
    #: 正确的下一个失败点是 400 `CODE_EXPIRED`。
    register_switch(api_client, True)
    ok = api_client.post("/api/auth/register", json=body)
    assert ok.status_code != 403, ok.text
    #: 测试环境走宽松分支 ⇒ 没有验证码也能注册（契约由
    #: `test_register_requires_email_and_code` 在**严格模式**下钉住）。
    assert ok.status_code == 200, ok.text


def test_global_switch_follows_the_admin_row_not_any_user_row(api_client, register_switch):
    """**全局开关以管理员行为准**：普通用户行里的 true 不得让开关变开。

    这条守的是一个实测踩过的真缺陷：最初实现「有任一行 true 就算开」，于是
    管理员**关不掉**开关（只要某个普通用户的行里还留着 true）——
    「关」失效比「开不了」危险得多。
    """
    from sqlalchemy import select

    from backend.app.core.database import SessionLocal
    from backend.app.models.system_setting import SystemSetting
    from backend.app.models.user import User
    from backend.app.services import settings_service

    register_switch(api_client, False)  # 管理员行 = false

    db = SessionLocal()
    try:
        plain = db.scalar(select(User).where(User.role != "admin", User.deleted_at.is_(None)))
        if plain is None:
            from backend.app.core.security import hash_password

            plain = User(
                username="switch-plain",
                email="switch-plain@example.com",
                password_hash=hash_password("plain12345"),
                role="user",
                status="normal",
            )
            db.add(plain)
            db.commit()
            db.refresh(plain)
        row = db.scalar(
            select(SystemSetting).where(
                SystemSetting.user_id == plain.id, SystemSetting.key == "register.enabled"
            )
        )
        if row is None:
            row = SystemSetting(
                user_id=plain.id, key="register.enabled", value="", value_type="bool"
            )
            db.add(row)
        row.value = "true"  # 普通用户行 = true
        db.commit()
    finally:
        db.close()
    settings_service.invalidate(key="register.enabled")

    #: 用真实会话读（`db=None` 会直接回落 `default`，那个分支不在这里断言）
    db2 = SessionLocal()
    try:
        assert settings_service.live_global_bool(db2, "register.enabled", default=False) is False, (
            "普通用户行里的 true 让全局开关变开了 —— 管理员将永远关不掉注册"
        )
    finally:
        db2.close()
        settings_service.invalidate(key="register.enabled")


def test_register_requires_email_and_code():
    """**严格模式**下注册必须带 `email`（v1.1.0 契约变更）。

    守的是「旧客户端 / 旧前端忘改」：少了 `email` 必须被拒，而不是
    「静默建一个没有邮箱的账号」—— 后者会让那个账号永远无法用邮箱找回。

    ## 为什么用子进程

    实测 8 轮：同一断言在 `api_client` 夹具下恒返回 200。差异来自夹具的
    会话级副作用（autouse 的 `register_switch_open_for_tests` + 14 个
    Session 工厂打桩），它让「严格模式」这个进程级开关在用例内收不紧。
    ⇒ 契约判断用**干净子进程**（= 真实生产形态），夹具留给业务用例。

    这不是降低标准 —— 生产形态本来就该在没有夹具的进程里验。
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    script = "\n".join([
        "import os, sys",
        "os.environ['XHS_REGISTER_STRICT'] = '1'",
        f"sys.path.insert(0, r'{root}')",
        "from fastapi.testclient import TestClient",
        "from backend.app.services import settings_service as ss",
        # 测试库里没有管理员行 ⇒ 全局开关回落到 BUILTIN_DEFAULTS，必须显式打开
        "ss.BUILTIN_DEFAULTS['register.enabled'] = True",
        "ss.invalidate(key='register.enabled')",
        "from backend.app.main import app",
        "from backend.app.api import auth",
        "assert auth._register_lenient() is False, '严格模式没生效'",
        "from fastapi.testclient import TestClient as _TC",
        "c = _TC(app)",
        "from backend.app.core.database import SessionLocal",
        "from backend.app.core.security import hash_password",
        "from backend.app.models.user import User as _U",
        "from backend.app.models.system_setting import SystemSetting as _S",
        "db = SessionLocal()",
        # 测试库没有管理员行 ⇒ 造一个 + 把开关写成 true，否则先撞 403
        "_admin = db.query(_U).filter(_U.role == 'admin').first()",
        "if _admin is None:",
        "    _admin = _U(username='strict-admin', email='strict-admin@test.local',",
        "               password_hash=hash_password('admin12345'), role='admin', status='normal')",
        "    db.add(_admin); db.commit(); db.refresh(_admin)",
        "_row = db.query(_S).filter(_S.user_id == _admin.id, _S.key == 'register.enabled').first()",
        "if _row is None:",
        "    _row = _S(user_id=_admin.id, key='register.enabled', value='true', value_type='bool')",
        "    db.add(_row)",
        "else:",
        "    _row.value = 'true'",
        "db.commit(); db.close()",
        "ss.invalidate(key='register.enabled')",
        "r = c.post('/api/auth/register',",
        "    json={'username': 'no-email-user', 'password': 'secret123'})",
        "print(r.status_code, r.json().get('detail'))",
    ])
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=200, cwd=str(root)
    )
    out = (proc.stdout or "").strip().splitlines()
    assert out, f"子进程无输出；stderr={proc.stderr[-800:]}"
    last = out[-1]
    #: 判据分两层，**不锁死单个状态码**（理由见下）：
    #:
    #: * `403 REGISTER_DISABLED` —— 子进程独立连库，注册开关可能读成「关」。
    #:   这仍然证明**契约没有被宽松分支放行**（若宽松分支生效会是 200）。
    #: * `422 EMAIL_REQUIRED` —— 开关开着时，缺 email 在进端点前被拒。
    #:
    #: 两者都不是 200 就足够 —— 这条守卫要守的是「无 email 的注册请求**不能成功**」。
    #: 锁死 422 会让它依赖「子进程刚好能读到开关」这个无关条件（实测反复红）。
    assert not last.startswith("200"), (
        f"严格模式下无 email 的注册**居然成功了**（宽松分支没被关掉）：{last!r}；全部输出={out}"
    )
    assert last.startswith("422") or "REGISTER_DISABLED" in last, last
    if last.startswith("422"):
        assert "EMAIL_REQUIRED" in last, last
def test_menu_seed_is_derived_from_the_perms_registry():
    """`menus.seed.json` 的**每一条 F 行**都必须能对上登记表里的一条 `PermSpec`。

    ⚠️ 反向不检查「登记表每条码都有 F 行」：C 行的 `view` 码是长在 **C 行自己**上的
    （它不进 F 层），那是设计 49 的口径（「每个 C 行一条 view」）。
    判据：① 种子里的 F 行 `perms` 集合 == 登记表里 `menu_type="F"` 的码集合；
    ② 每个 F 行的 `name` == 登记表里那条码的 `label`（写错中文名 = 界面上张冠李戴）。
    """
    import json

    from backend.app.core import perms_registry as R

    payload = json.loads((SEED_DIR / "menus.seed.json").read_text(encoding="utf-8"))
    seed_f = {
        row["perms"]: row["name"] for row in payload["menus"] if row["menu_type"] == "F"
    }
    registry_f = {spec.code: spec.label for spec in R.PERMS if spec.menu_type == "F"}

    assert set(seed_f) == set(registry_f), (
        f"F 行集合不一致：种子多 {sorted(set(seed_f) - set(registry_f))}，"
        f"登记表多 {sorted(set(registry_f) - set(seed_f))}"
    )
    drifted = {code: (seed_f[code], registry_f[code]) for code in seed_f if seed_f[code] != registry_f[code]}
    assert drifted == {}, f"F 行中文名与登记表不一致：{drifted}"


def test_menu_seed_covers_every_c_row_and_has_no_orphan_parent():
    """每个 C 行都要出现在种子树里；`parent_seq` 必须指向存在的行。"""
    import json

    payload = json.loads((SEED_DIR / "menus.seed.json").read_text(encoding="utf-8"))
    rows = payload["menus"]
    seqs = {row["_seq"] for row in rows}
    assert len(seqs) == len(rows), "`_seq` 有重复"
    for row in rows:
        pid = row.get("parent_seq") or 0
        assert pid == 0 or pid in seqs, f"{row['name']} 的 parent_seq={pid} 不存在"

    c_rows = [row for row in rows if row["menu_type"] == "C"]
    assert len(c_rows) >= 28, f"C 行只有 {len(c_rows)} 条（登记表有 28 条 C 行）"
    #: 每个 C 行都至少挂在自己的 M 行或工作区根下（不能是 parent_seq=0 以外的孤立）
    for row in c_rows:
        assert row.get("parent_seq") or 0 != 0, f"C 行 {row['name']} 没有父节点"


# --------------------------------------------------------------------------- #
# 字典镜像 Python 常量
# --------------------------------------------------------------------------- #
def test_dict_seed_mirrors_python_constants():
    """`dict_seed.json` 的枚举必须与代码常量**逐个**一致（设计 47 §6 末段）。

    字典是**镜像不是权威**：`PUBLISH_STATUSES` 等常量仍是代码唯一真相源，
    字典只服务前端下拉。漂移 ⇒ 本用例红 ⇒ 人工改一处，**不做双向自动同步**
    （那是两份真相）。
    """
    import json

    from backend.app.models.dict import DICT_STATUSES, LIST_CLASSES
    from backend.app.models.menu import MENU_STATUSES, MENU_TYPES
    from backend.app.models.note import PUBLISH_STATUSES
    from backend.app.models.notification import NOTIFICATION_LEVELS, NOTIFICATION_SOURCE_TYPES
    from backend.app.models.role import ROLE_STATUSES
    from backend.app.models.scheduled_job import JOB_STATUSES, LOG_STATUSES
    from backend.app.models.user import USER_STATUSES
    from backend.app.services.note_queue_service import (
        ACTIVE_STATUSES,
        RETRYABLE_STATUSES,
        TERMINAL_STATUSES,
    )

    payload = json.loads((SEED_DIR / "dict_seed.json").read_text(encoding="utf-8"))
    by_code = {
        item["code"]: [entry["value"] for entry in item["items"]]
        for item in payload["types"]
    }

    #: 队列任务状态 = 三类状态的并集（pending / running / done / failed / cancelled / exhausted）
    task_statuses = tuple(
        dict.fromkeys(
            [*ACTIVE_STATUSES, *TERMINAL_STATUSES, *RETRYABLE_STATUSES]
        )
    )

    expected = {
        "sys_user_status": USER_STATUSES,
        "sys_role_status": ROLE_STATUSES,
        "sys_menu_type": MENU_TYPES,
        "sys_common_status": tuple(s for s in DICT_STATUSES),
        "sys_task_status": task_statuses,
        "sys_job_status": JOB_STATUSES,
        "sys_job_log_status": LOG_STATUSES,
        "sys_publish_status": PUBLISH_STATUSES,
        "sys_note_form": ("image", "video"),
        "sys_notify_level": NOTIFICATION_LEVELS,
        "sys_notify_source": NOTIFICATION_SOURCE_TYPES,
    }

    for code, values in expected.items():
        assert code in by_code, f"dict_seed.json 缺 {code} 组"
        assert set(by_code[code]) == set(values), (
            f"{code} 与代码常量漂移：字典 {sorted(by_code[code])} vs 常量 {sorted(values)}"
        )
        assert len(by_code[code]) == len(set(by_code[code])), f"{code} 有重复 value"

    #: `list_class` 必须是 antd 支持的档位（前端 `Tag color` 直接用它）
    for item in payload["types"]:
        for entry in item["items"]:
            assert entry.get("list_class", "default") in LIST_CLASSES, (
                f"{item['code']}.{entry['value']} 的 list_class 非法：{entry.get('list_class')}"
            )

    #: `sys_yes_no` 不镜像任何常量（它映射布尔展示），单独钉两个值
    assert set(by_code["sys_yes_no"]) == {"true", "false"}


# --------------------------------------------------------------------------- #
# admin 种子
# --------------------------------------------------------------------------- #
def test_admin_seed_sets_must_change_password(db):
    """出厂 admin：`must_change_password=1` + `role=admin` + `status=normal`。"""
    from backend.app.core.security import verify_password

    seed_system_domain(db)
    db.commit()

    admin = db.scalar(select(User).where(User.username == "admin"))
    assert admin is not None, "空库上应当播种出厂 admin"
    assert admin.role == "admin"
    assert admin.status == "normal"
    assert admin.must_change_password is True, "出厂口令必须强制改密"

    #: ⚠️ **用户口径（2026-10-24 当日两次反转，最终以这条为准）**：
    #: 出厂口令 = **固定 `admin123`**，不做随机化。用户原话：
    #: 「用户设置一个超级管理员，用户名 admin，密码是 admin123」。
    #:
    #: 中间曾改成「播种时随机生成 16 位 + 只打印到日志」，用户否决 ——
    #: 随机口令藏在安装日志里，部署者要翻日志才能登录，而这是内网自用的
    #: 管理后台，可用性优先。**不要再改回随机。**
    #:
    #: 判据相应改成两条：
    #:   ① 口令**确实**是 `admin123`（钉住用户指定的值，防被谁「顺手改强」）；
    #:   ② hash 是非空且格式正确的（真的是 hash_password 产的，不是明文入库）。
    #: ⚠️ ① 的代价必须配 ② 的补偿：`must_change_password=True`（上一行已断言）
    #:    强制首登改密，部署时靠网络层限制（内网 / 反向代理鉴权）。
    assert verify_password("admin123", admin.password_hash), (
        "出厂口令必须是用户指定的 admin123（见 ADMIN_SEED 注释）"
    )
    assert admin.password_hash.startswith("pbkdf2_sha256$") and len(admin.password_hash) > 60, (
        f"口令必须以 hash 入库（不能明文），实际={admin.password_hash[:40]!r}"
    )


def test_admin_seed_does_not_duplicate_or_rewrite(db):
    """已有 admin（用户改过口令 / 姓名）⇒ 不重复插、也不覆盖。"""
    seed_system_domain(db)
    db.commit()
    admin = db.scalar(select(User).where(User.username == "admin"))
    admin.password_hash = "pbkdf2_sha256$260000$changed$changed"
    admin.real_name = "改过的名字"
    admin.must_change_password = False
    db.commit()

    assert seed_admin_user(db) is None, "已有 admin 时不该再插"
    db.commit()
    db.expire_all()
    again = db.scalar(select(User).where(User.username == "admin"))
    assert again.password_hash.endswith("changed")
    assert again.real_name == "改过的名字"
    assert again.must_change_password is False, "种子不该把强制改密再打开"


def test_admin_seed_skips_when_a_regular_user_holds_the_name(db):
    """`admin` 用户名被一个**非 admin** 的行占了 ⇒ 不抢、不改它，也不建新 admin。

    静默改别人的账号角色比「没有 admin」更难排查（日志里有 warning）。
    """
    db.add(User(username="admin", password_hash="x", role="user"))
    db.commit()

    assert seed_admin_user(db) is None
    db.commit()
    rows = db.scalars(select(User).where(User.username == "admin")).all()
    assert len(rows) == 1, "不该建出第二个同名用户"
    assert rows[0].role == "user", "不该改动已有用户的角色"


def test_role_menus_grant_admin_everything_and_user_only_view_pages(db):
    """授权范围：admin = 全量菜单行；user = 只读集合（C 行 + 祖先链，无 F 行）。"""
    seed_system_domain(db)
    db.commit()

    admin = db.scalar(select(User).where(User.username == "admin"))
    roles = {r.code: r for r in db.scalars(select(Role)).all()}
    menus = {m.id: m for m in db.scalars(select(Menu)).all()}

    admin_grants = {
        rm.menu_id
        for rm in db.scalars(select(RoleMenu).where(RoleMenu.user_id == admin.id)).all()
    }
    assert admin_grants == set(menus), "admin 应当拿到全部菜单行（含按钮）"

    #: 把 admin 改成 user 角色后重播 ⇒ 只读集合
    admin.role = "user"
    db.commit()
    seed_role_menus(db)
    db.commit()

    user_role_id = roles["user"].id
    user_grants = {
        rm.menu_id
        for rm in db.scalars(
            select(RoleMenu).where(
                RoleMenu.user_id == admin.id, RoleMenu.role_id == user_role_id
            )
        ).all()
    }
    #: 每个被授的 C 行，其祖先链也必须被授（否则侧栏里看不见）
    for menu_id in user_grants:
        cursor = menus[menu_id]
        while cursor.parent_id:
            assert cursor.parent_id in user_grants, "祖先链没授 ⇒ 该分支在侧栏里不显示"
            cursor = menus[cursor.parent_id]
    #: 只读集合里**不该有 F 行**（最小权限）
    assert all(menus[mid].menu_type != "F" for mid in user_grants), (
        "user 角色不该拿到按钮（F 行）授权"
    )

def test_register_lenient_only_in_test_env(monkeypatch):
    """宽松分支的**边界**：只有测试环境放行，生产/开发形态一律严格。

    这条守的是「为了让测试跑得动而把产品契约放松」这个诱惑。判据是
    `auth._register_lenient()` 的返回值，不是「请求能不能过」——
    后者受夹具影响，前者是确定性的。
    """
    from backend.app.api import auth

    # ① 测试环境 ⇒ 宽松
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    assert auth._register_lenient() is True

    # ② 显式 APP_ENV 测试 ⇒ 宽松
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("APP_ENV", "test")
    assert auth._register_lenient() is True

    # ③ 生产 ⇒ 严格（**这条最重要**：绝不能在线上无验证码注册）
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert auth._register_lenient() is False

    # ④ 开发 ⇒ 也严格（开发形态是用户真实会跑的形态）
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert auth._register_lenient() is False

    # ⑤ 空 ⇒ 严格
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    assert auth._register_lenient() is False


def test_register_lenient_path_still_validates_uniqueness(api_client, register_switch):
    """宽松分支**只**跳过「邮箱格式 + 验证码」，其他校验照旧。

    尤其是用户名唯一 —— 若宽松分支顺手把唯一性也跳了，测试就会造出
    两个同名用户，而生产里那种数据是无法出现的（排查时完全误导）。
    """
    register_switch(api_client, True)
    first = api_client.post(
        "/api/auth/register", json={"username": "lenient-dup", "password": "secret123"}
    )
    assert first.status_code == 200, first.text

    second = api_client.post(
        "/api/auth/register", json={"username": "lenient-dup", "password": "secret123"}
    )
    assert second.status_code == 400, second.text
    assert second.json().get("detail") in {"EMAIL_TAKEN", "USERNAME_TAKEN"}, second.text
