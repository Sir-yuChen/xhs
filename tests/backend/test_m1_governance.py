"""M1 治理里程碑的回归测试。

覆盖二开 M1（治理与前置修复）已完成的 6 个任务，每个用例对应一条已修复缺陷。
设计原则：**每条断言都锁定「修复后不允许回退」的行为**，而不是复述实现细节。

| 用例 | 任务 | 缺陷 |
|---|---|---|
| :func:`test_config_env_var_overrides_yaml` | T-110 | DEF-004 配置优先级反转 |
| :func:`test_config_yaml_still_supplies_defaults` | T-110 | 同上（反向：不得把 YAML 弄失效） |
| :func:`test_config_source_order_puts_yaml_last` | T-110 | 同上（锁住来源顺序） |
| :func:`test_qianfan_naming_collision_resolved` | T-101 | DEF-015 双「千帆」 |
| :func:`test_qianfan_module_has_no_ark_url_literal` | T-101 | 同上（防止回归） |
| :func:`test_sub_type_normalize_rejects_unknown` | T-102 | sub_type 静默兜底 |
| :func:`test_select_adapter_raises_instead_of_falling_back_to_pc` | T-102 | 同上 |
| :func:`test_account_patch_endpoint_is_gone` | T-105 | DEF-007 假 PATCH |
| :func:`test_publish_account_gate_blocks_non_active_account` | T-109 | DEF-005 发布不校验账号状态 |
| :func:`test_publish_idempotency_key_is_deterministic` | T-103 | DEF-002 无幂等约束 |
| :func:`test_publish_idempotency_enforced_by_unique_index` | T-103 | 同上（库级兜底） |
| :func:`test_publish_idempotency_release_allows_reclaim` | T-103 | 同上（不误伤「取消后重建」） |
| :func:`test_users_role_column_declared` | T-104 | DEF-014 用户表无角色 |
| :func:`test_roles_normalize_and_is_admin` | T-104 | 同上 |
| :func:`test_require_admin_rejects_non_admin` | T-104 | 同上 |
| :func:`test_publish_jobs_idempotency_column_is_nullable_and_unique` | T-103 | 同上 |
"""

from __future__ import annotations

import ast
import importlib
import inspect
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.main import app

from tests.backend.conftest import (  # noqa: PLC0415 - schema 复用助手
    _BUILT_SCHEMAS,
    _SCHEMA_BY_ENGINE,
    make_engine,
    reset_test_data,
)



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract


def _isolated_session(tmp_path, name: str = "m1-governance.db"):
    """建一个隔离的 SQLite 库并返回 Session（每个用例一个库，互不干扰）。"""
    from backend.app.core.database import Base
    from tests.backend.conftest import seed_owner_rows

    engine = make_engine(tmp_path / name)
    #: 2026-10-26：同进程复用 schema ⇒ 表只建一次，之后只清数据。
    _schema = _SCHEMA_BY_ENGINE.get(engine)
    if _schema is not None and _BUILT_SCHEMAS.get(_schema):
        reset_test_data(engine)
    else:
        Base.metadata.create_all(bind=engine)
        if _schema is not None:
            _BUILT_SCHEMAS[_schema] = True
    # ⚠️ 全局 FK=ON 后必须播种父行（2026-10-04 实测 2 条红）。
    seed_owner_rows(engine, user_id=1, account_id=1)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


# ---------------------------------------------------------------------------
# T-110 · 配置优先级
# ---------------------------------------------------------------------------


def test_config_env_var_overrides_yaml(monkeypatch):
    """T-110 / DEF-004：环境变量必须能覆盖 default.yaml。

    这是缺陷的**核心判据**——修复前这三个变量全部被 YAML 压掉。
    """
    from backend.app.core.config import Settings

    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("SECRET_KEY", "env-secret-wins")
    monkeypatch.setenv("FERNET_KEY", "env-fernet-wins")
    monkeypatch.setenv("FRONTEND_SERVE_STATIC", "true")
    monkeypatch.setenv("SERVER_PORT", "9999")

    settings = Settings()

    assert settings.scheduler_enabled is True
    assert settings.secret_key == "env-secret-wins"
    assert settings.fernet_key == "env-fernet-wins"
    assert settings.frontend_serve_static is True
    assert settings.server_port == 9999


def test_config_yaml_still_supplies_defaults(monkeypatch):
    """T-110 反向保护：修好优先级之后，**YAML 必须仍然生效**。

    如果修复方式变成「忽略 YAML」，那只是把一种错误换成另一种错误。
    """
    from backend.app.core.config import Settings, _load_yaml_config

    yaml_values = _load_yaml_config()
    assert yaml_values, "config/default.yaml 应能被解析出至少一个键"

    # 清掉可能存在的同名环境变量，确保读到的是 YAML 层
    for field in yaml_values:
        monkeypatch.delenv(field.upper(), raising=False)

    settings = Settings()

    assert settings.backend_cors_origins == yaml_values["backend_cors_origins"]
    assert settings.scheduler_enabled is bool(yaml_values.get("scheduler_enabled", False))
    assert settings.frontend_serve_static is bool(yaml_values.get("frontend_serve_static", False))


def test_config_source_order_puts_yaml_last():
    """T-110：锁住来源顺序为「init > env > .env > YAML > secrets」。

    顺序本身是缺陷的根因，必须被断言固定住，否则一次重构就可能悄悄改回去。
    """
    from backend.app.core.config import Settings, _YamlSettingsSource

    sources = Settings.settings_customise_sources(
        Settings, "init", "env", "dotenv", "secrets"
    )
    assert sources[:3] == ("init", "env", "dotenv")
    # YAML 必须是倒数第二位（仅高于 secrets），即「最低优先级之一」
    assert isinstance(sources[3], _YamlSettingsSource)
    assert sources[4] == "secrets"


# ---------------------------------------------------------------------------
# T-101 · 千帆 / 蒲公英命名冲突
# ---------------------------------------------------------------------------


def test_qianfan_naming_collision_resolved():
    """T-101 / DEF-015：旧的「千帆」模块必须消失，新模块必须存在且打 pgy 域名。

    v1.6（2026-09-19）更新：`apis/xhs_qianfan_apis.py` 这个**名字**当初被腾出来，
    正是为了给二开新增**真千帆**代码用（T-101 的动机）。现在真千帆已经落到该路径，
    所以断言从「该模块不存在」升级为「该模块必须是真千帆，且不得是蒲公英」——
    命名冲突依然被钉住，只是被钉的方向反了过来。
    """
    # 真千帆：存在、类名正确、打 ark 域名、不含蒲公英语义
    qianfan = importlib.import_module("apis.xhs_qianfan_apis")
    assert hasattr(qianfan, "XHS_Qianfan_Apis")
    assert qianfan.XHS_Qianfan_Apis.base_url == "https://ark.xiaohongshu.com"
    assert not hasattr(qianfan, "PuGongYingAPI")
    assert not hasattr(qianfan, "PGY_AUTHORITY")

    # 千帆所需的工具模块仍然不存在（真千帆不需要签名，也就没有 util）
    with pytest.raises(ImportError):
        importlib.import_module("xhs_utils.xhs_qianfan_util")

    module = importlib.import_module("apis.xhs_pugongying_distributor_apis")
    assert hasattr(module, "PugongyingDistributorAPI")
    assert not hasattr(module, "QianFanAPI")
    assert module.PGY_AUTHORITY == "pgy.xiaohongshu.com"


def test_qianfan_module_really_targets_ark() -> None:
    """真千帆模块里的 URL 必须**只**指向 ark，不得混入 pgy。"""
    module = importlib.import_module("apis.xhs_qianfan_apis")
    tree = ast.parse(inspect.getsource(module))

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    offenders = [
        text
        for text in literals
        if "pgy.xiaohongshu.com" in text and text not in docstrings
    ]
    assert offenders == [], f"真千帆模块不得出现蒲公英域名：{offenders}"


def test_qianfan_module_has_no_ark_url_literal():
    """T-101：重命名后必须仍然**只**请求蒲公英域名。

    用 AST 检查**代码里的字符串字面量**（排除 docstring）——
    文档里提到 ark 是必要的说明，但一旦出现在 URL 里就是接错了平台。
    """
    module = importlib.import_module("apis.xhs_pugongying_distributor_apis")
    tree = ast.parse(inspect.getsource(module))

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    offenders = [
        text
        for text in literals
        if "ark.xiaohongshu.com" in text and text not in docstrings
    ]
    assert offenders == [], f"不得在代码中硬编码真千帆域名：{offenders}"


# ---------------------------------------------------------------------------
# T-102 · sub_type 显式枚举
# ---------------------------------------------------------------------------


def test_sub_type_normalize_rejects_unknown():
    """T-102：未知 sub_type 必须抛错，不能兜底。"""
    from backend.app.core.account_types import (
        SUB_TYPE_CREATOR,
        SUB_TYPE_PC,
        UnknownSubTypeError,
        normalize_sub_type,
    )

    assert normalize_sub_type("pc") == SUB_TYPE_PC
    assert normalize_sub_type("creator") == SUB_TYPE_CREATOR
    # 历史数据可能没写该字段 —— 这是唯一允许的隐式默认
    assert normalize_sub_type(None) == SUB_TYPE_PC
    assert normalize_sub_type("") == SUB_TYPE_PC

    with pytest.raises(UnknownSubTypeError):
        normalize_sub_type("shop")
    with pytest.raises(UnknownSubTypeError):
        normalize_sub_type("pgy")


def test_qianfan_link_is_known_but_not_an_unknown_value():
    """T-102 补充（2026-10-11）：千帆（ark）链路**不是**未知取值。

    真库里账号 9「开心睡大觉的店」`sub_type='qianfan'`，而它此前被当成未知值
    ⇒ 未捕获的 `UnknownSubTypeError` ⇒ **500**。

    两个类必须分开（这是本用例的全部意义）：

    * `UnknownSubTypeError` = 数据脏了 ⇒ 显式失败；
    * `SubTypeNotSupportedError` = 认识它，但这条链路没有该操作要用的东西
      ⇒ 业务结果，调用方给可读原因。

    同时钉住「不许放宽成兜底」：它**绝不能**静默返回 `pc`/`creator`。
    """
    from backend.app.core.account_types import (
        SUB_TYPES,
        SUB_TYPE_QIANFAN,
        SubTypeNotSupportedError,
        UnknownSubTypeError,
        normalize_sub_type,
    )

    assert SUB_TYPE_QIANFAN == "qianfan"
    # ⚠️ 刻意不进 SUB_TYPES —— 那个元组的契约是「按它选登录适配器」，
    # 千帆没有登录适配器（登录走 api/login_sessions.py）。
    assert "qianfan" not in SUB_TYPES

    with pytest.raises(SubTypeNotSupportedError) as excinfo:
        normalize_sub_type("qianfan")
    # 是「不支持」，不是「不认识」—— 两者继承同一个 ValueError，但类型不同。
    assert not isinstance(excinfo.value, UnknownSubTypeError)
    assert excinfo.value.code == "LINK_HAS_NO_ADAPTER"
    # 提示必须可执行（指向真正的下一步），且不许兜底成 pc/creator。
    assert "login_sessions" in str(excinfo.value)


def test_qianfan_account_does_not_500_on_login_probe():
    """2026-10-11 真缺陷回归：千帆链路账号的登录态探活**不许 500**。

    现场：账号 9。`_account_cookies_by_sub_type` 与 `_select_adapter` 两个口子
    都会碰到 `normalize_sub_type` —— 但本操作的真实答案是「**拿不到令牌**」
    （它只有 `ark`，探活要 www/creator），不是异常。
    """
    from types import SimpleNamespace

    from backend.app.api import accounts as accounts_module

    # ① 取令牌：千帆账号回落到 `ark` 通道（此前直接抛 ⇒ 500）。
    captured = {}

    def _fake_latest(db, account_id, channel):
        captured["channel"] = channel
        return "cookie-jar-text"

    import backend.app.api.accounts as mod

    original = mod.latest_cookies_for_channel
    mod.latest_cookies_for_channel = _fake_latest
    try:
        account = SimpleNamespace(id=9, sub_type="qianfan")
        assert mod._account_cookies_by_sub_type(None, account) == "cookie-jar-text"
        assert captured["channel"] == "ark", "千帆链路的唯一通道就是 ark"
    finally:
        mod.latest_cookies_for_channel = original

    # ② 选适配器：必须抛「已知但无适配器」，不得当成未知取值。
    from backend.app.core.account_types import SubTypeNotSupportedError

    with pytest.raises(SubTypeNotSupportedError):
        accounts_module._select_adapter("qianfan", object(), object())


def test_qianfan_account_cookie_check_records_a_reason_not_a_crash():
    """2026-10-11 真缺陷回归：定时巡检遇到千帆账号要**留下可读原因**。

    此前它抛 `UnknownSubTypeError` ⇒ 被外层 per-account `except` 吞成一行
    warning，卡片上什么都看不到（用户只看到状态没变）。
    """
    from types import SimpleNamespace

    from backend.app.services import scheduler_service

    original = scheduler_service.normalize_sub_type

    def _boom(sub_type):
        from backend.app.core.account_types import SubTypeNotSupportedError

        raise SubTypeNotSupportedError("qianfan 没有登录适配器", code="LINK_HAS_NO_ADAPTER")

    account = SimpleNamespace(
        id=9,
        sub_type="qianfan",
        status="active",
        status_message="",
        updated_at=None,
    )
    from datetime import datetime

    marked_now = datetime(2026, 10, 11, 21, 30, 0)
    scheduler_service.normalize_sub_type = _boom
    try:
        result = scheduler_service._check_single_account(None, account, marked_now)
    finally:
        scheduler_service.normalize_sub_type = original

    # 状态没被改坏（它没过期，只是本操作做不了），但原因留在了卡片上。
    assert result == "active"
    assert account.status_message, "必须留下可读原因，不能静默"
    assert account.updated_at == marked_now, "巡检过就要盖时间戳（`now` 由调用方注入）"


def test_publish_gate_rejects_qianfan_link_without_raising():
    """2026-10-11 真缺陷回归：发布闸门遇到千帆链路账号要**业务拒绝**，不是 500。

    千帆链路没有 creator 适配器 ⇒ `normalize_sub_type` 抛
    `SubTypeNotSupportedError`。那个异常若漏出闸门就是 500 —— 但这里的语义是
    「这个账号不可发布」，所以必须翻成 `AccountNotPublishableError`（400 级）。

    ⚠️ 同时钉住「不借会话 / 不降级放行」：千帆账号**绝不能**被放过去当 creator。
    """
    from types import SimpleNamespace

    import pytest as _pytest

    from backend.app.services.account_service import (
        AccountNotPublishableError,
        ensure_creator_account_for_publish,
    )

    qianfan_account = SimpleNamespace(
        id=9,
        user_id=1,
        platform="xhs",
        sub_type="qianfan",
        status="active",
        status_message="",
        deleted_at=None,
    )

    with _pytest.raises(AccountNotPublishableError) as excinfo:
        ensure_creator_account_for_publish(qianfan_account, user_id=1)
    # 原因必须说清「是链路不支持（qianfan）」，而不是含糊的「Creator account required」。
    # ⚠️ 断言 `detail` 而不是 `str(exc)`：`AccountRejectedError.__init__` 先
    # `super().__init__(detail)` 再存 `self.detail`，HTTP 层读的是 `detail`。
    assert "qianfan" in excinfo.value.detail

    # 反向：真的 creator 账号不受影响（这道闸门不能变严）。
    creator_account = SimpleNamespace(
        id=8,
        user_id=1,
        platform="xhs",
        sub_type="creator",
        status="active",
        status_message="",
        deleted_at=None,
    )
    assert ensure_creator_account_for_publish(creator_account, user_id=1) is creator_account


def test_select_adapter_raises_instead_of_falling_back_to_pc():
    """T-102：适配器选择必须显式分支。

    原实现是 ``creator if sub_type == "creator" else pc`` ——
    新增协议链路时会**静默走 pc**，打错域名与 Cookie 域。
    """
    from backend.app.api import accounts as accounts_module
    from backend.app.core.account_types import UnknownSubTypeError

    pc_sentinel = object()
    creator_sentinel = object()

    assert accounts_module._select_adapter("creator", pc_sentinel, creator_sentinel) is creator_sentinel
    assert accounts_module._select_adapter("pc", pc_sentinel, creator_sentinel) is pc_sentinel

    with pytest.raises(UnknownSubTypeError):
        accounts_module._select_adapter("shop", pc_sentinel, creator_sentinel)


# ---------------------------------------------------------------------------
# T-105 · 假的 PATCH /accounts
# ---------------------------------------------------------------------------


def test_account_patch_endpoint_is_gone():
    """T-105 / DEF-007：恒返回成功的假 PATCH 必须已下线。

    它比 404 更危险——调用方会以为更新成功。这里断言路由**不存在**。
    """
    from backend.app.api.accounts import router

    patch_routes = [
        route
        for route in router.routes
        if "PATCH" in getattr(route, "methods", set())
    ]
    assert patch_routes == [], f"PATCH 路由不应再存在：{patch_routes}"


# ---------------------------------------------------------------------------
# T-109 · 发布前账号状态闸门
# ---------------------------------------------------------------------------


def _fake_account(**overrides):
    base = dict(
        id=1,
        user_id=7,
        platform="xhs",
        sub_type="creator",
        status="active",
        status_message="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_publish_account_gate_blocks_non_active_account():
    """T-109 / DEF-005：发布链路必须校验 status，不只校验 sub_type。"""
    from backend.app.services.account_service import (
        AccountNotFoundError,
        AccountNotPublishableError,
        ensure_creator_account_for_publish,
    )

    # 正常路径
    assert ensure_creator_account_for_publish(_fake_account(), user_id=7).status == "active"

    # 账号不存在 / 越权 —— 都按 404 处理，避免越权探测
    with pytest.raises(AccountNotFoundError):
        ensure_creator_account_for_publish(None, user_id=7)
    with pytest.raises(AccountNotFoundError):
        ensure_creator_account_for_publish(_fake_account(user_id=99), user_id=7)

    # 链路不符
    with pytest.raises(AccountNotPublishableError):
        ensure_creator_account_for_publish(_fake_account(sub_type="pc"), user_id=7)
    with pytest.raises(AccountNotPublishableError):
        ensure_creator_account_for_publish(_fake_account(platform="dy"), user_id=7)

    # 状态不符 —— 核心用例
    with pytest.raises(AccountNotPublishableError):
        ensure_creator_account_for_publish(
            _fake_account(status="expired", status_message="Cookie 已失效"), user_id=7
        )
    with pytest.raises(AccountNotPublishableError):
        ensure_creator_account_for_publish(_fake_account(status="unknown"), user_id=7)

    # 错误信息必须能指导用户（含原始 status_message）
    with pytest.raises(AccountNotPublishableError) as excinfo:
        ensure_creator_account_for_publish(
            _fake_account(status="expired", status_message="Cookie 已失效"), user_id=7
        )
    assert "Cookie 已失效" in excinfo.value.detail
    assert "expired" in excinfo.value.detail


# ---------------------------------------------------------------------------
# T-103 · 发布幂等
# ---------------------------------------------------------------------------


def test_publish_idempotency_key_is_deterministic():
    """T-103 / DEF-002：键必须只由「账号 × 内容」决定，且对字典序不敏感。"""
    from backend.app.services.publish_service import build_publish_idempotency_key

    def key(**overrides):
        params = dict(
            platform="xhs",
            platform_account_id=5,
            title="标题",
            body="正文",
            publish_options="{}",
        )
        params.update(overrides)
        return build_publish_idempotency_key(**params)

    base = key()
    assert key() == base
    # 同内容但 JSON 键顺序不同 —— 必须得到同一个键
    assert key(publish_options='{"b":1,"a":2}') == key(publish_options='{"a":2,"b":1}')
    # 换账号 / 换标题 / 换正文 / 换参数 —— 都必须是不同的键
    assert key(platform_account_id=6) != base
    assert key(title="另一个标题") != base
    assert key(body="另一段正文") != base
    assert key(publish_options='{"a":3}') != base


def _make_job(**overrides):
    from backend.app.models import PublishJob

    base = dict(
        user_id=1,
        platform="xhs",
        platform_account_id=1,
        title="同款标题",
        body="同款正文",
        publish_mode="immediate",
        publish_options="{}",
        status="pending",
    )
    base.update(overrides)
    return PublishJob(**base)


def test_publish_idempotency_enforced_by_unique_index(tmp_path):
    """T-103：应用层给可读提示，数据库唯一索引做最终兜底。"""
    from backend.app.models import PublishJob
    from backend.app.services.publish_service import (
        DuplicatePublishError,
        claim_publish_idempotency,
        find_duplicate_publish,
    )

    db = _isolated_session(tmp_path)
    try:
        first = _make_job(status="published")
        key = claim_publish_idempotency(db, job=first)
        db.add(first)
        db.commit()
        first_id = first.id

        # 应用层：可读的重复拦截
        assert find_duplicate_publish(db, idempotency_key=key) is not None
        with pytest.raises(DuplicatePublishError):
            claim_publish_idempotency(db, job=_make_job())

        # 库级兜底：绕过应用层直接写同一个键也必须被拒
        intruder = _make_job(status="published")
        intruder.idempotency_key = key
        db.add(intruder)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # 自身排除：同一个 job 重新认领必须放行（重试路径依赖这一点）
        again = db.get(PublishJob, first_id)
        assert claim_publish_idempotency(db, job=again) == key
        db.rollback()
    finally:
        db.close()


def test_publish_idempotency_release_allows_reclaim(tmp_path):
    """T-103：释放之后槽位必须可被重新认领。

    防止「建了 → 取消 → 再建同样内容」被唯一索引无故挡死（合法流程）。
    同时验证 NULL 键可以有多条。
    """
    from backend.app.models import PublishJob
    from backend.app.services.publish_service import (
        claim_publish_idempotency,
        release_publish_idempotency,
    )

    db = _isolated_session(tmp_path)
    try:
        first = _make_job(status="pending")
        key = claim_publish_idempotency(db, job=first)
        db.add(first)
        db.commit()
        first_id = first.id

        # 未占用键的任务可以有很多条（多条 NULL 不违反唯一索引）
        for _ in range(3):
            db.add(_make_job())
            db.commit()

        # 从 pending 取消 —— 可证明从未提交，释放键
        released = db.get(PublishJob, first_id)
        released.status = "cancelled"
        release_publish_idempotency(released)
        db.commit()
        assert db.get(PublishJob, first_id).idempotency_key is None

        # 释放后，同内容的新任务必须能正常认领同一个键
        reclaimed = _make_job()
        assert claim_publish_idempotency(db, job=reclaimed) == key
        db.rollback()
    finally:
        db.close()


def test_publish_jobs_idempotency_column_is_nullable_and_unique():
    """T-103：列必须 nullable（否则「建了又取消」会被挡住）且 unique（否则防线失效）。"""
    from backend.app.models import PublishJob

    column = PublishJob.__table__.columns["idempotency_key"]
    assert column.nullable is True
    assert column.unique is True


# ---------------------------------------------------------------------------
# T-104 · 用户角色
# ---------------------------------------------------------------------------


def test_roles_normalize_and_is_admin():
    """T-104 / DEF-014：角色归一化 + 管理员判定。

    注意角色与 sub_type 的策略**相反**：未知角色保守降级为普通用户
    （权限方向安全），而不是抛错。这里把这条差异固定下来。
    """
    from backend.app.core.roles import ROLE_ADMIN, ROLE_USER, is_admin, normalize_role

    assert normalize_role("admin") == ROLE_ADMIN
    assert normalize_role("ADMIN") == ROLE_ADMIN
    assert normalize_role(" user ") == ROLE_USER
    # 历史数据缺该字段
    assert normalize_role(None) == ROLE_USER
    # 未知非空取值 → 保守降级（少给权限，而不是 500）
    assert normalize_role("root") == ROLE_USER

    assert is_admin(SimpleNamespace(role="admin")) is True
    assert is_admin(SimpleNamespace(role="user")) is False
    assert is_admin(SimpleNamespace(role=None)) is False
    assert is_admin(None) is False


def test_users_role_column_declared():
    """T-104：users.role 必须存在，且带 server_default（迁移后旧行立即有值）。"""
    from backend.app.models import User

    assert "role" in User.__table__.columns
    assert User.__table__.columns["role"].server_default is not None


def test_require_admin_rejects_non_admin():
    """T-104：require_admin 对普通用户返回 403，对管理员放行。"""
    from fastapi import HTTPException

    from backend.app.core.deps import require_admin

    with pytest.raises(HTTPException) as excinfo:
        require_admin(SimpleNamespace(id=1, username="u", role="user"))
    assert excinfo.value.status_code == 403

    admin = SimpleNamespace(id=1, username="a", role="admin")
    assert require_admin(admin) is admin


def test_app_imports_cleanly_with_new_modules():
    """整体冒烟：新增/改动的模块必须都能被应用正常装配。"""
    from backend.app.core.account_types import SUB_TYPES
    from backend.app.core.roles import ROLES
    from backend.app.services import publish_service

    assert SUB_TYPES == ("pc", "creator")
    assert set(ROLES) == {"admin", "user"}
    assert callable(publish_service.claim_publish_idempotency)
    assert app is not None
