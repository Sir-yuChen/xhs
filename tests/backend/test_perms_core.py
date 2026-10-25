"""S0 地基守卫：161 条权限码 + 方言判定 + 路径绝对化 + 安全自检（设计 57 §S0）。

## 这一组为什么必须先有

S1–S8 全部建在这四件地基上：S3 的 251 条写路由按 161 条码挂、S2 的缓存按
`role_perms('admin')` 派生、S1 的迁移按 `db_dialect` 判方言、S4 的 11 个消费方
按 `paths.storage_dir()` 取目录。地基错一条，上面四段一起返工。

## 断言口径

数字 **161 / 25 / 114 / 22** 是**需求值**（设计 49 附 A），不是「代码里现在有多少」——
所以断言里写字面量是故意的：删一条码必须让本文件红（变异 `drop_one_perm`）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app.core import db_dialect, paths, perms
from backend.app.core import perms_registry as reg


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.contract

#: 分层标记（测试规范 §15.5 准入清单）：本文件是纯函数 + 源码文本守卫，
#: 不建库、不打端点 ⇒ redline（最快一层）。
pytestmark = pytest.mark.redline

APP_DIR = Path(__file__).resolve().parents[2] / "backend" / "app"


# ---------------------------------------------------------------------------
# 一、164 条码
# ---------------------------------------------------------------------------


def test_all_perms_has_exactly_162_codes():
    """需求值 **164**（原 162 + 设计 64 的 system:scheduler:view / manage）。

    ⚠️ 补这一条的原因（设计 49 附 A 未覆盖）：「个人资料」页早就有 UI 与路由，
    但没有页面码 ⇒ v1.1.0 侧栏由 `menus` 表下发时它**进不了侧栏**
    （用户只能手敲 `/profile`），且该页在权限体系里等于不存在。

    区分两件事（原注释混为一谈）：
    * **端点**不挂码 —— 每个人都要能改自己的密码，挂码会把人锁死（`EXEMPT_SELF`）；
    * **菜单**必须有码 —— 否则无法被 `role_menus` 授权、不会出现在侧栏。
    """
    assert len(perms.ALL_PERMS) == 164


def test_three_groups_are_countable_separately():
    """三组分别可数：系统 **27**（设计 64 +scheduler:view/manage）/ 业务写码 114 / 页级 view 23。"""
    assert len(reg.SYSTEM_PERMS) == 27
    assert len(reg.BUSINESS_PERMS) == 114
    assert len(reg.PAGE_VIEW_PERMS) == 23
    assert len(reg.SYSTEM_PERMS) + len(reg.BUSINESS_PERMS) + len(reg.PAGE_VIEW_PERMS) == 164


def test_system_group_matches_design_49_table():
    """系统域逐码核对（49 附 A.1）：user 7 / role 5 / menu 4 / dict 4 / setting 4 / log 1。"""
    by_resource: dict[str, int] = {}
    for spec in reg.SYSTEM_PERMS:
        domain, resource, _action = perms.parse_perm(spec.code)
        assert domain == "system", spec.code
        by_resource[resource] = by_resource.get(resource, 0) + 1
    assert by_resource == {
        "user": 7,
        "role": 5,
        "menu": 4,
        "dict": 4,
        "setting": 4,
        "log": 1,
        # 设计 64 §3.3：任务中心总览（跨工作区只读 + 管理动作）
        "scheduler": 2,
    }


def test_business_group_matches_design_49_module_table():
    """业务域逐前缀核对（49 §模块九 22 页表格）+ 末尾两行（提示词 / 生成）。"""
    expected = {
        "xhs:account": 9,
        "xhs:note": 10,
        "xhs:library": 5,
        "xhs:workshop": 6,
        "xhs:item": 10,
        "xhs:publish": 5,
        "xhs:order": 6,
        "xhs:violation": 1,
        "xhs:picture": 7,
        "xhs:monitor": 5,
        "xhs:autoops": 5,
        "xhs:crawl": 1,
        "xhs:keyword": 3,
        "xhs:topic": 3,
        "xhs:rule": 4,
        "xhs:forbiddenWord": 4,
        "xhs:material": 4,
        "xhs:job": 6,
        "xhs:record": 4,
        "ai:gateway": 6,
        "ai:model": 4,
        "ai:prompt": 3,
        "ai:generate": 3,
    }
    actual: dict[str, int] = {}
    for spec in reg.BUSINESS_PERMS:
        domain, resource, _action = perms.parse_perm(spec.code)
        key = f"{domain}:{resource}"
        actual[key] = actual.get(key, 0) + 1
    assert actual == expected
    assert sum(expected.values()) == 114


def test_every_code_matches_the_design_regex():
    """逐码格式符合 `^[a-z][a-z0-9_]*:[a-zA-Z][a-zA-Z0-9]*(:[a-zA-Z][a-zA-Z0-9]*)?$`。"""
    pattern = re.compile(r"^[a-z][a-z0-9_]*:[a-zA-Z][a-zA-Z0-9]*(:[a-zA-Z][a-zA-Z0-9]*)?$")
    bad = [code for code in perms.ALL_PERMS if not pattern.match(code)]
    assert bad == []


def test_codes_are_unique_and_three_segment():
    """无重复（重复会让 `menus.perms` 唯一索引在播种时炸）；每条都是三段。"""
    codes = [spec.code for spec in reg.PERMS]
    assert len(codes) == len(set(codes))
    two_segment = [code for code in codes if code.count(":") != 2]
    # 允许两段（`make_perm` 支持），但本轮 161 条**全部**是 `<域>:<资源>:<动作>`
    assert two_segment == []


def test_page_view_codes_are_all_menu_type_c():
    """页级 view 22 条必须是 C 行（种子里挂在自己那页上，不是按钮）。"""
    assert {spec.menu_type for spec in reg.PAGE_VIEW_PERMS} == {"C"}
    assert all(spec.code.endswith(":view") for spec in reg.PAGE_VIEW_PERMS)


def test_view_code_numbers_match_the_frontend_menu_rows():
    """C 行 ⟺ view 码：前端侧栏的每个可点击页都有 view 码（同一份枚举）。

    判据取 `app-shell.tsx` 里的 `key: "/...` **页面级**路径，与 `PAGE_VIEW_PERMS`
    的 `path` 集合比对。两类合法例外（都写死在这里，新增例外必须改本文件）：

    * `?tab=` 子页 —— 它们是同一页内的 Tab（素材库 / 提示词 / 四个工坊），
      共用父页的 `xhs:workshop:view`（Tab 不是独立页面，也不该单独授权）；
    * `/platforms/xhs/insights/shop` —— 与 content 共用 `xhs:insights:view`
      （设计 49 §9.1 明文「共用」）。
    """
    shell = (
        APP_DIR.parent.parent / "frontend" / "src" / "components" / "layout" / "app-shell.tsx"
    ).read_text(encoding="utf-8")
    frontend_paths = set(re.findall(r'key: "([^"]+)"', shell))
    registry_paths = {spec.parent_path for spec in reg.PAGE_VIEW_PERMS}

    tab_children = {p for p in frontend_paths if "?tab=" in p}
    shared_insight_pages = {"/platforms/xhs/insights/shop"}
    # 系统域 6 页已在 25 条里；个人资料 / 403 不挂码
    out_of_scope = {"/system/users", "/system/roles", "/system/menus", "/system/dicts", "/system/settings", "/system/logs"}
    page_paths = (
        {
            p
            for p in frontend_paths
            if (
                p.startswith("/platforms")
                or p.startswith("/llm-gateway")
                or p in ("/tasks", "/task-records", "/models", "/settings")
            )
        }
        - tab_children
        - shared_insight_pages
        - out_of_scope
    )
    assert page_paths - registry_paths == set(), (
        f"这些菜单行还没有 view 码：{sorted(page_paths - registry_paths)}"
    )


# ---------------------------------------------------------------------------
# 二、判定函数
# ---------------------------------------------------------------------------


def test_admin_gets_all_perms_without_touching_role_menus(monkeypatch):
    """`role_perms('admin')` 直接返回 ALL_PERMS，**不查 role_menus**（设计 57 关键判据）。

    变异 `admin_reads_table` 把这句话改成「查表」⇒ 本用例红（下面那个
    会抛的 resolver 就是那把尺子）。
    """

    class _ExplodingResolver:
        def perms_for_role(self, role: str):  # noqa: ARG002
            raise AssertionError("admin 不许查 role_menus —— 误删授权会把超管锁在门外")

    assert perms.role_perms("admin", db=_ExplodingResolver()) == perms.ALL_PERMS
    # 不传 db 也是全量（最常见路径）
    assert perms.role_perms("admin") == perms.ALL_PERMS


def test_admin_role_is_normalized_before_the_shortcut():
    """`"ADMIN"` / `" admin "` 也走派生分支（角色归一在 `core/roles`，这里只验证接上了）。"""
    assert perms.role_perms("ADMIN") == perms.ALL_PERMS
    assert perms.role_perms(" admin ") == perms.ALL_PERMS


def test_unknown_role_gets_nothing():
    """未知角色 ⇒ 空集（保守），而不是猜一个。"""
    assert perms.role_perms("viewer") == frozenset()
    assert perms.role_perms(None) == frozenset()


def test_has_perm_follows_admin_and_rejects_unregistered_codes():
    """未登记的码 ⇒ **恒 False**，即使角色是 admin。

    变异 `has_perm_allows_unknown` 拆掉这层判定 ⇒ 本用例红。
    为什么这层必须在：`require_perm("xhs:item:onShel")` 这种拼写错误
    若被放行，等于**整条路由对所有 admin 开放**，而启动守卫只校验
    `ROUTE_PERMS` 里的码，挂码时手写错的那一条它看不见。
    """

    class _Admin:
        role = "admin"

    class _User:
        role = "user"

    assert perms.has_perm(_Admin(), "xhs:item:onShelf") is True
    # admin 也不例外（这条之前漏了 —— 只断普通用户会被 admin 短路径绕过）。
    # ⚠️ 必须用**格式合法但未登记**的码（`xhs:item:archiveGoods`）：
    #    像 `onShel` 这种拼写错误虽然也未登记，但它被 `user_perms(admin)` 的
    #    成员判断挡住了 ⇒ 即使拆掉「未登记即 False」那层也照样 False，
    #    那种写法当尺子会**假绿**（实测踩过）。
    assert perms.has_perm(_Admin(), "xhs:item:archiveGoods") is False
    assert perms.has_perm(_User(), "xhs:item:archiveGoods") is False
    assert perms.has_perm(None, "xhs:item:onShelf") is False
    assert "xhs:item:archiveGoods" not in perms.ALL_PERMS


def test_make_perm_and_parse_perm_round_trip():
    assert perms.make_perm("system", "user", "view") == "system:user:view"
    assert perms.make_perm("system", "log") == "system:log"
    assert perms.parse_perm("system:user:view") == ("system", "user", "view")
    assert perms.parse_perm("system:log") == ("system", "log", "")
    assert perms.parse_perm("System:User:View") is None
    assert perms.parse_perm("") is None
    assert perms.parse_perm(None) is None


def test_assert_registry_sane_passes():
    perms.assert_registry_sane()


def test_assert_registry_sane_catches_orphan_button(monkeypatch):
    """挂不到任何页面的 F 行 ⇒ raise（否则那行权限在角色分配界面上永远授不了）。"""
    orphan = reg.PermSpec("xhs:ghost:create", "幽灵按钮", "F", "/platforms/xhs/nope", "xhs", "xhs")
    monkeypatch.setattr(perms, "PERMS", tuple(reg.PERMS) + (orphan,))
    with pytest.raises(ValueError, match="挂不到任何页面行"):
        perms.assert_registry_sane()


# ---------------------------------------------------------------------------
# 三、方言判定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("sqlite:///./data/x.db", "sqlite"),
        ("sqlite+pysqlite:///./data/x.db", "sqlite"),
        ("postgresql://u:p@h:5432/db", "postgresql"),
        ("postgresql+psycopg://u:p@h/db", "postgresql"),
        ("postgres://u:p@h/db", "postgresql"),
        ("mysql+pymysql://u:p@h/db", "mysql+pymysql"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_dialect_of(url, expected):
    assert db_dialect.dialect_of(url) == expected


def test_is_sqlite_and_is_postgres_agree_with_dialect_of():
    assert db_dialect.is_sqlite("sqlite:///x.db") is True
    assert db_dialect.is_sqlite("postgresql://h/d") is False
    assert db_dialect.is_postgres("postgresql+psycopg://h/d") is True
    assert db_dialect.is_postgres("sqlite:///x.db") is False


def test_bool_server_default_is_dialect_neutral():
    """布尔默认值两库同义：发 `text("true"/"false")`，不是 `"0"/"1"`。

    SQLite 把 `false` 当字面量求值成 0、PG 求值成布尔 false —— 两边语义一致。
    变异 `bool_default_zero` 改成 `"0"` ⇒ 本用例红（PG 上会 `invalid input syntax`）。
    """
    for value, expect in ((True, "true"), (False, "false")):
        default = db_dialect.bool_server_default(value)
        assert str(default) == expect


def test_no_raw_sqlite_sniffing_outside_the_dialect_module():
    """`backend/app` 里除 `db_dialect.py` 外不许出现 `startswith("sqlite")`。

    散落判据的问题：换 PG 时漏改一处，那段就**静默不生效**（最坏的是时区归一）。
    """
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        if path.name == "db_dialect.py" and path.parent.name == "core":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if 'startswith("sqlite")' in line and not line.lstrip().startswith("#"):
                offenders.append(f"{path.relative_to(APP_DIR.parent.parent)}:{lineno}")
    assert offenders == []


# ---------------------------------------------------------------------------
# 四、路径绝对化（设计 53 的 A2）
# ---------------------------------------------------------------------------


def test_paths_are_absolute_and_do_not_depend_on_cwd(monkeypatch, tmp_path):
    """`storage_dir` / `frontend_dist` 必须**与 CWD 无关**（A2 的判据）。"""
    before = (paths.app_root(), paths.storage_dir(), paths.frontend_dist())
    monkeypatch.chdir(tmp_path)  # 换一个完全无关的 CWD
    after = (paths.app_root(), paths.storage_dir(), paths.frontend_dist())
    assert before == after
    # 根目录 = 含 `backend/` 与 `frontend/` 的那一层（不靠名字，靠结构判）
    assert (after[0] / "backend" / "app").is_dir()
    assert (after[0] / "frontend").is_dir()
    assert after[1] == after[0] / "backend" / "app" / "storage"
    assert after[2] == after[0] / "frontend" / "dist"
    assert all(path.is_absolute() for path in after)


def test_settings_storage_dir_delegates_to_paths():
    """`Settings.storage_dir` 委托 `paths.storage_dir()`（S4 才逐消费方改直调）。"""
    from backend.app.core.config import get_settings

    assert get_settings().storage_dir == paths.storage_dir()
    assert get_settings().storage_dir.is_absolute()


def test_frontend_build_dir_defaults_to_paths_and_honours_absolute_override():
    from backend.app.core.config import Settings

    assert Settings().frontend_build_dir_resolved == paths.frontend_dist()
    override = str(paths.app_root() / "somewhere" / "dist")
    assert Settings(frontend_build_dir=override).frontend_build_dir_resolved == Path(override)
    # 相对路径按**仓库根**解析，不是 CWD
    rel = Settings(frontend_build_dir="frontend/dist")
    assert rel.frontend_build_dir_resolved == (paths.app_root() / "frontend" / "dist").resolve()


def test_no_cwd_relative_storage_paths_outside_paths_module():
    """除 `paths.py` 外，`backend/app` 不许出现相对 CWD 的 `Path("backend/` / `Path("./`。"""
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        if path.name == "paths.py" and path.parent.name == "core":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            if 'Path("backend/' in line or 'Path("./' in line:
                offenders.append(f"{path.relative_to(APP_DIR.parent.parent)}:{lineno}")
    assert offenders == []


# ---------------------------------------------------------------------------
# 五、安全自检（设计 53 的 C1）
# ---------------------------------------------------------------------------


def test_startup_refuses_default_secret_key_on_postgres():
    """默认 secret_key + PG ⇒ RuntimeError（伪造任意用户 JWT 的那种事故）。"""
    from backend.app.core.config import DEFAULT_SECRET_KEY, Settings

    settings = Settings(database_url="postgresql://u:p@h/db", secret_key=DEFAULT_SECRET_KEY)
    with pytest.raises(RuntimeError, match="secret_key"):
        settings.assert_production_ready()


def test_startup_refuses_default_secret_key_even_on_sqlite_url():
    """⚠️ v1.1.0（2026-10-24）退役守卫：SQLite 豁免已随桌面端一起退场。

    原判据「SQLite 不拦 —— 拦了会让已有安装升级后起不来」的前提是「桌面端还在」；
    桌面端退役后只有服务器形态，**任何方言**都必须真密钥（实现里连 if 都没了）。
    这里钉住「豁免退了就别回来」。
    """
    from backend.app.core.config import DEFAULT_SECRET_KEY, Settings

    with pytest.raises(RuntimeError, match="secret_key"):
        Settings(database_url="sqlite:///./data/x.db", secret_key=DEFAULT_SECRET_KEY).assert_production_ready()


def test_startup_allows_custom_secret_key_on_postgres():
    from backend.app.core.config import Settings

    Settings(database_url="postgresql://u:p@h/db", secret_key="a-real-secret").assert_production_ready()


def test_lifespan_calls_the_self_check_first():
    """`lifespan` 第一句就是安全自检（不允许「先写库再报错」）。"""
    source = (APP_DIR / "main.py").read_text(encoding="utf-8")
    body = source.split("async def lifespan(app: FastAPI):", 1)[1]
    body = body.split("\nyield", 1)[0]
    # A3b 起 lifespan 带 docstring（薄壳说明）—— 剥掉三引号块后再判「第一句」。
    in_doc = False
    code_lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if in_doc:
            if '"""' in stripped:
                in_doc = False
            continue
        if stripped.startswith('"""'):
            in_doc = '"""' not in stripped[3:]
            continue
        code_lines.append(stripped)
    assert code_lines[0] == "get_settings().assert_production_ready()", code_lines[:3]


# ---------------------------------------------------------------------------
# 六、启动守卫骨架
# ---------------------------------------------------------------------------


def test_route_guard_collects_unregistered_perm_codes():
    """`route_guard` 能报出「登记了但不在 161 条里」的码（写错字母）。"""
    from backend.app.core import route_guard, route_perms

    original = dict(route_perms.ROUTE_PERMS)
    try:
        route_perms.ROUTE_PERMS["/api/__probe__"] = "xhs:item:onShel"
        problems = route_guard.collect_violations(_empty_app())
        assert any("未登记的码" in p for p in problems)
    finally:
        route_perms.ROUTE_PERMS.clear()
        route_perms.ROUTE_PERMS.update(original)


def test_route_guard_is_clean_after_s3_and_reports_when_emptied():
    """守卫的两个方向都要验（2026-10-24 改写，原判据基于 S0 空表已失效）。

    原句：*「S0 阶段 `ROUTE_PERMS` 为空 ⇒ 每条写路由都要被报出来」*，
    断言 `len(unguarded) > 200`。S3 补全 **278 条**映射后这句必然为红 ——
    但它守的意图（「守卫真的会检查写路由」）仍然成立，只是**判据反过来了**：

    | 方向 | 断言 |
    |---|---|
    | 现在（已补全） | `collect_violations` **0 条**「漏挂码」 |
    | 清空 `ROUTE_PERMS` 后 | 每条写路由都要被报出来（证明它不是恒真） |

    第二个方向是关键：只断言「0 违规」的话，一个**永远返回空**的守卫也会通过。
    """
    from backend.app.core import route_guard, route_perms
    from backend.app.main import create_app

    app = create_app()
    write_ops = sum(
        1
        for operations in app.openapi()["paths"].values()
        for method in operations
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    )
    assert write_ops > 200, f"写路由数量异常（{write_ops} 条，口径应为 270 量级）"

    # ① 现状：0 条漏挂码
    problems = route_guard.collect_violations(app)
    unguarded = [p for p in problems if p.startswith("[漏挂码]")]
    assert unguarded == [], f"S3 后仍有写路由漏挂码：{unguarded[:5]}"

    # ② 清空后必须全报出来（证明守卫不是恒真）
    original = dict(route_perms.ROUTE_PERMS)
    try:
        route_perms.ROUTE_PERMS.clear()
        emptied = [p for p in route_guard.collect_violations(app) if p.startswith("[漏挂码]")]
        assert len(emptied) > 200, (
            f"清空 ROUTE_PERMS 后只报出 {len(emptied)} 条漏挂码 —— 守卫是恒真的"
        )
        assert len(emptied) <= write_ops
    finally:
        route_perms.ROUTE_PERMS.update(original)


def test_assert_every_route_guarded_raises_route_guard_error():
    from backend.app.core import route_guard

    with pytest.raises(route_guard.RouteGuardError):
        route_guard.assert_every_route_guarded(_empty_app())
    # 继承 RuntimeError —— 设计 50 §2.4 的判据是「拒绝启动」
    assert issubclass(route_guard.RouteGuardError, RuntimeError)


def _empty_app():
    from fastapi import FastAPI

    return FastAPI()
