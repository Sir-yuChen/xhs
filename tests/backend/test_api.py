import json
import os
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.app.models.user import User
from backend.app.models.llm_gateway import LlmBusinessProvider, LlmGatewayConfig, LlmGatewayModel
from backend.app.services.business_provider_keys import BUSINESS_KEYS
from backend.app.services.account_service import (
    CHANNEL_ARK,
    CHANNEL_CREATOR,
    CHANNEL_WWW,
)
from tests.backend.cookie_fixtures import cookie_version
#: 业务点映射的幂等 upsert —— **唯一实现**在 `conftest.py`（本卡 Group B 的根因：
#: R4 注册即播种默认映射 ⇒ 盲 `db.add` 必撞 `(user_id, platform, business_key)` 唯一约束）。
#: 各测试文件共用这一份，别再各写一份（DEF-044）。
from tests.backend.conftest import upsert_business_mapping as _upsert_business_mapping
import pytest


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration


client = TestClient(app)


@pytest.fixture(autouse=True)
def _register_switch_open(monkeypatch):
    """打开注册开关（**本文件专用**，v1.1.0 / S3）。

    ## 为什么直接 patch `live_global_bool` 而不是配置层

    本文件用**模块级裸 `client = TestClient(app)`**，且几十处用例自己调
    `_override_database()` **换库**。而 `register` 端点读开关走的是
    `live_global_bool(db, ...)` → 查 `system_settings` 管理员行 → 没有行则回落
    `_baseline_bool` → `settings_default` / `BUILTIN_DEFAULTS`。

    实测（2026-10-24）：在**独立进程**里 patch 配置层能读到 `True`，但在 pytest 里
    同样 patch 之后注册仍 403 —— conftest 的 session 级夹具与用例自己的换库
    会先后改写这两层，谁赢取决于执行顺序（脆弱且难复现）。

    ⇒ 本文件直接**钉死读点本身**：`auth` 模块内是按名导入的
    （`from ... import live_global_bool`），所以 patch 那个名字即可，
    与配置层怎么变都无关。

    ⚠️ 这只影响 `test_api.py`（本文件测的是业务端点行为，不是权限矩阵）。
    权限矩阵与「开关关闭时 403」在 `test_seed_no_overwrite.py` 用真 PG 验。
    """
    from backend.app.services import settings_service

    #: ⚠️ patch 的是 **`settings_service` 模块属性**，不是 `auth` 模块属性：
    #: 端点在函数体内 `from backend.app.services.settings_service import
    #: live_global_bool`，每次调用都从 `settings_service` 现取这个名字
    #: ⇒ patch 那个模块才拦得住（patch `auth` 会 AttributeError，实测）。
    monkeypatch.setattr(settings_service, "live_global_bool", lambda *a, **k: True)


def test_health_endpoint_returns_ok():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "spider-xhs"}


def test_platforms_endpoint_exposes_product_registry():
    response = client.get("/api/platforms")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 6
    assert payload["items"][0]["id"] == "xhs"
    assert payload["items"][0]["enabled"] is True
    assert payload["items"][1]["status"] == "coming_soon"


def test_xhs_analytics_overview_requires_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        response = client.get("/api/xhs/analytics/overview")

        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_backend_foundation_modules_import():
    from backend.app.core.config import get_settings
    from backend.app.core.database import Base
    from backend.app.models import PlatformAccount, Note, Task, User

    settings = get_settings()
    assert settings.app_name == "Spider_XHS"
    assert Base is not None
    assert User.__tablename__ == "users"
    assert PlatformAccount.__tablename__ == "platform_accounts"
    assert Note.__tablename__ == "notes"
    assert Task.__tablename__ == "tasks"


def test_accounts_page_does_not_auto_check_accounts_on_load():
    source = open("frontend/src/pages/platforms/xhs/accounts-page.tsx", encoding="utf-8").read()

    assert "refreshMissingProfiles" not in source
    assert "void refreshMissingProfiles(loadedAccounts)" not in source


def test_accounts_page_uses_antd_components_and_shows_sync_buttons():
    """账号卡片必须用 antd，且四个操作入口齐全（v1.31：检查按钮已移除）。

    用户 2026-09-21 口径：「底部按钮不再按照账号类型进行区分全部展示……
    检查按钮功能移除」。⇒ 断言四个同步/操作入口都在、**「检查」不在**。
    """
    source = open("frontend/src/pages/platforms/xhs/accounts-page.tsx", encoding="utf-8").read()

    assert "antd" in source
    for button in ("同步数据", "同步笔记", "同步商品"):
        assert button in source, f"账号卡片缺少入口：{button}"
    # 「检查」按钮已移除：断言的是**按钮形态**（isChecking 态 + 「检查」文案），
    # 不是「检查」两个字——注释里提到它不算。
    assert "检查中" not in source
    assert "handleCheck" not in source


def test_xhs_direct_request_env_temporarily_removes_proxy_variables(monkeypatch):
    from backend.app.adapters.xhs.request_env import PROXY_ENV_KEYS, direct_xhs_request_env

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:10809")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:10809")
    # ⚠️ `_PROXY_ENV_PRESENT` 是**进程级缓存**（只判一次，见 request_env 的注释）：
    # xdist 并行时同进程内别的用例可能已经把它缓存成 False
    # （`test_login_state` 的若干用例显式 monkeypatch 成 None 后再判，判完留在 False）。
    # 这里要用的是「有代理」分支，必须把缓存重置，否则本用例会走「无代理」的
    # 不持锁快速路径、`PROXY_ENV_KEYS` 一个都不被摘 ⇒ 断言恒红。
    monkeypatch.setattr(
        "backend.app.adapters.xhs.request_env._PROXY_ENV_PRESENT", None
    )

    with direct_xhs_request_env():
        assert all(key not in os.environ for key in PROXY_ENV_KEYS)

    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:10809"
    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:10809"


def test_xhs_adapters_isolate_sdk_calls_from_broken_system_proxy():
    adapter_paths = [
        "backend/app/adapters/xhs/creator_login_adapter.py",
        "backend/app/adapters/xhs/creator_api_adapter.py",
        "backend/app/adapters/xhs/pc_login_adapter.py",
        "backend/app/adapters/xhs/pc_api_adapter.py",
    ]

    for path in adapter_paths:
        source = open(path, encoding="utf-8").read()
        assert "direct_xhs_request_env" in source


def test_crawler_page_exports_spider_style_excel():
    # 2026-10-04 设计 21 切片 B：crawler-page.tsx 并入「笔记发现 → 批量抓取」，
    # 页面内容面板化为 bulk-crawl-panel.tsx（Excel 导出逻辑随迁）。
    source = open("frontend/src/pages/platforms/xhs/bulk-crawl-panel.tsx", encoding="utf-8").read()

    assert "noteExcelHeaders" in source
    assert "笔记id" in source
    assert "图片地址url列表" in source
    assert "application/vnd.ms-excel;charset=utf-8" in source


def test_crawler_page_uses_antd_table():
    source = open("frontend/src/pages/platforms/xhs/bulk-crawl-panel.tsx", encoding="utf-8").read()

    assert "antd" in source
    assert "Table" in source


def test_discovery_uses_antd_components_and_preserves_core_logic():
    source = open("frontend/src/pages/platforms/xhs/discovery-page.tsx", encoding="utf-8").read()

    assert "antd" in source
    assert "async function ensureNoteDetail" in source or "ensureNoteDetail" in source
    assert "保存" in source
    assert "评论" in source
    assert "原文" in source


def test_discovery_preserves_note_detail_and_media_logic():
    source = open("frontend/src/pages/platforms/xhs/discovery-page.tsx", encoding="utf-8").read()

    assert "function getNoteVideoUrl" in source
    assert "function getNoteKindLabel" in source
    assert "detailMediaIndex" in source
    assert "视频" in source
    assert "图文" in source


def test_library_page_preserves_delete_and_media_logic():
    source = open("frontend/src/pages/platforms/xhs/library-page.tsx", encoding="utf-8").read()
    api_source = open("frontend/src/lib/api.ts", encoding="utf-8").read()

    assert "deleteSavedNote" in api_source
    assert "handleDeleteNote" in source
    assert "删除" in source
    assert "function getSavedNoteCoverUrl" in source
    assert 'referrerPolicy="no-referrer"' in source


def test_discovery_cards_show_note_media_type():
    source = open("frontend/src/pages/platforms/xhs/discovery-page.tsx", encoding="utf-8").read()

    assert "function getNoteKindLabel" in source
    assert "视频" in source
    assert "图文" in source


def test_rewrite_page_preserves_mode_switch():
    # 2026-09-25 §36：草稿工坊并入「笔记工坊」页，原 rewrite-page.tsx 更名为
    # drafts-workshop-panel.tsx（面板组件，Segmented 双模式原样保留）。
    source = open("frontend/src/pages/platforms/xhs/drafts-workshop-panel.tsx", encoding="utf-8").read()

    assert "rewrite" in source
    assert "generate" in source
    assert "改写" in source
    assert "生成" in source
    assert "antd" in source


def test_publish_page_uses_antd_components():
    source = open("frontend/src/pages/platforms/xhs/publish-page.tsx", encoding="utf-8").read()

    assert "antd" in source
    assert "发布" in source


def test_openai_compatible_text_client_decodes_utf8_json_when_response_headers_are_wrong(monkeypatch):
    from backend.app.services.ai_service import OpenAICompatibleTextClient

    class DummyConfig:
        base_url = "https://api.example.test/v1"
        model_name = "gpt-5.4"

    class FakeResponse:
        def __init__(self):
            self.content = (
                b'{"choices":[{"message":{"content":"'
                + "你好，今天很适合去公园散步。".encode("utf-8")
                + b'"}}]}'
            )
            self.encoding = "ISO-8859-1"
            self.apparent_encoding = "utf-8"
            self.headers = {"content-type": "text/event-stream"}

        def raise_for_status(self):
            return None

    def fake_post(*args, **kwargs):
        return FakeResponse()

    monkeypatch.setattr("backend.app.services.ai_service.requests.post", fake_post)

    client = OpenAICompatibleTextClient()
    result = client.rewrite_note(
        model_config=DummyConfig(),
        api_key="test-key",
        title="原文标题",
        body="今天天气很好，我们去公园散步。",
        instruction="保留中文自然表达",
    )

    assert result == "你好，今天很适合去公园散步。"


def test_database_initialization_creates_user_table(tmp_path):
    from alembic import command
    from alembic.config import Config

    db_url = f"sqlite:///{tmp_path / 'init-test.db'}"
    cfg = Config(os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "backend", "alembic.ini")))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")

    test_engine = create_engine(db_url, connect_args={"check_same_thread": False})
    table_names = inspect(test_engine).get_table_names()
    assert "users" in table_names
    assert "platform_accounts" in table_names
    assert "alembic_version" in table_names


def test_alembic_initial_migration_creates_all_product_tables(tmp_path):
    from alembic import command
    from alembic.config import Config

    db_url = f"sqlite:///{tmp_path / 'alembic-tables-test.db'}"
    cfg = Config(os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "backend", "alembic.ini")))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")

    test_engine = create_engine(db_url, connect_args={"check_same_thread": False})
    table_names = set(inspect(test_engine).get_table_names())
    expected = {
        "users", "platform_accounts", "account_cookie_versions", "login_sessions",
        "notes", "note_assets", "note_comments", "topics", "note_topics",
        "ai_drafts", "ai_generated_assets",
        "publish_jobs", "publish_assets", "tasks",
        "monitoring_targets", "monitoring_snapshots",
        "keyword_groups", "api_logs",
    }
    # ⚠️ `model_configs` 在 `f8a9b0c1d2e3`（设计 17 §13.2.5 v2.0）被 drop ——
    # 它在一期是「产品表」，v2.0 起整表退场（调用链改走 `llm_business_providers`
    # 映射）。这里从期望集合里去掉它，并**显式断言它已退场**：
    # 只删期望而不反向断言 = 下次谁误删了别的表，本用例也只会少期望一项、静默放行。
    assert "model_configs" not in table_names, (
        "model_configs 应已被 f8a9b0c1d2e3 退场（设计 17 §13.2.5 v2.0）"
    )
    assert "llm_business_providers" in table_names, (
        "llm_business_providers 应由 f8a9b0c1d2e3 建出（设计 17 §13.2.3 v2.0）"
    )
    assert expected.issubset(table_names)


def test_run_alembic_migrations_pins_absolute_script_location(tmp_path, monkeypatch):
    """设计 14 §4：桌面安装版启动时 CWD = 安装目录，alembic 的相对
    script_location（backend/alembic）会按 CWD 解析失败 ⇒ _run_alembic_migrations
    必须基于 ini 自身路径钉成绝对路径。两个 alembic 命令全部打桩：本测试只验证
    传下去的 script_location，不碰真实库。"""
    from alembic import command as alembic_command

    captured = {}

    def fake_upgrade(cfg, rev):
        captured["script_location"] = cfg.get_main_option("script_location")

    def fake_stamp(cfg, rev):
        captured["stamped"] = True

    monkeypatch.setattr(alembic_command, "upgrade", fake_upgrade)
    monkeypatch.setattr(alembic_command, "stamp", fake_stamp)

    from backend.app.core.database import _run_alembic_migrations

    _run_alembic_migrations()

    location = captured.get("script_location")
    assert location, "alembic upgrade 未被调用（captured 为空）"
    assert "stamped" not in captured
    assert os.path.isabs(location)
    assert os.path.isdir(location)
    assert os.path.basename(location) == "alembic"


def test_database_initialization_normalizes_legacy_gpt_54_model_name(tmp_path):
    from backend.app.core.database import _normalize_model_config_names

    engine = create_engine(f"sqlite:///{tmp_path / 'model-name-migration-test.db'}", connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE model_configs (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    name VARCHAR(128) NOT NULL,
                    model_type VARCHAR(32) NOT NULL,
                    provider VARCHAR(64) NOT NULL,
                    model_name VARCHAR(128) NOT NULL,
                    base_url TEXT NOT NULL,
                    encrypted_api_key TEXT NOT NULL,
                    is_default BOOLEAN NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                "INSERT INTO model_configs "
                "(id, user_id, name, model_type, provider, model_name, base_url, encrypted_api_key, is_default) "
                "VALUES (1, 1, 'Text model', 'text', 'openai-compatible', 'gpt5.4', 'https://api.example.test/v1', '', 1)"
            )
        )

    _normalize_model_config_names(engine)
    with engine.connect() as connection:
        row = connection.execute(text("SELECT model_name FROM model_configs WHERE id = 1")).mappings().one()
        assert row["model_name"] == "gpt-5.4"
        assert connection.execute(
            text("SELECT name FROM app_migrations WHERE name = 'normalize_legacy_gpt_54_model_name_v1'")
        ).first()


def test_database_initialization_normalizes_existing_sqlite_times_to_shanghai(tmp_path):
    from backend.app.core.database import _normalize_sqlite_datetime_storage

    engine = create_engine(f"sqlite:///{tmp_path / 'time-migration-test.db'}", connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE platform_accounts (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    platform VARCHAR(32) NOT NULL,
                    sub_type VARCHAR(32),
                    external_user_id VARCHAR(128) NOT NULL,
                    nickname VARCHAR(128) NOT NULL,
                    avatar_url TEXT NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    status_message TEXT NOT NULL DEFAULT '',
                    profile_json TEXT NOT NULL DEFAULT '{}',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                "INSERT INTO platform_accounts "
                "(id, user_id, platform, sub_type, external_user_id, nickname, avatar_url, status, created_at, updated_at) "
                "VALUES (1, 1, 'xhs', 'pc', 'pc-1', 'cat', '', 'active', '2026-04-30 07:50:43', '2026-04-30 07:50:43')"
            )
        )

    _normalize_sqlite_datetime_storage(engine)
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT created_at, updated_at FROM platform_accounts WHERE id = 1")
        ).mappings().one()
        assert str(row["created_at"]).startswith("2026-04-30 15:50:43")
        assert str(row["updated_at"]).startswith("2026-04-30 15:50:43")
        assert connection.execute(
            text("SELECT name FROM app_migrations WHERE name = 'sqlite_datetime_asia_shanghai_v1'")
        ).first()


def _parse_sse_response(response):
    import json as _json
    events = []
    done = {}
    for line in response.text.split("\n"):
        if line.startswith("data: "):
            event = _json.loads(line[6:])
            if event.get("type") == "item":
                events.append(event["item"])
            elif event.get("type") == "done":
                done = event
    return {"items": events, **done}


#: `_override_database` 建的临时库 session 工厂（播种用）。
#: ⚠️ 必须用**它**而不是全局 `SessionLocal` —— 后者指向真实库，
#: 用例播种落错库会「静默不生效」，排查成本极高。
_test_session_factory = None


def _override_database(tmp_path):
    global _test_session_factory
    from backend.app.core.database import Base, get_db

    engine = create_engine(f"sqlite:///{tmp_path / 'auth-test.db'}", connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    _test_session_factory = TestingSessionLocal

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return get_db


def test_auth_register_login_me_and_refresh_use_real_tokens(tmp_path):
    get_db = _override_database(tmp_path)
    try:
        register_response = client.post(
            "/api/auth/register",
            json={"username": "operator", "password": "secret123"},
        )
        assert register_response.status_code == 200
        registered = register_response.json()
        assert registered["token_type"] == "bearer"
        assert registered["access_token"]
        assert registered["refresh_token"]
        assert registered["user"]["username"] == "operator"

        me_response = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {registered['access_token']}"},
        )
        assert me_response.status_code == 200
        assert me_response.json()["username"] == "operator"

        login_response = client.post(
            "/api/auth/login",
            json={"username": "operator", "password": "secret123"},
        )
        assert login_response.status_code == 200
        logged_in = login_response.json()
        assert logged_in["access_token"]
        assert logged_in["refresh_token"]

        refresh_response = client.post(
            "/api/auth/refresh",
            json={"refresh_token": logged_in["refresh_token"]},
        )
        assert refresh_response.status_code == 200
        refreshed = refresh_response.json()
        assert refreshed["token_type"] == "bearer"
        assert refreshed["access_token"]
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_auth_rejects_duplicate_user_and_bad_credentials(tmp_path):
    get_db = _override_database(tmp_path)
    try:
        response = client.post(
            "/api/auth/register",
            json={"username": "operator", "password": "secret123"},
        )
        assert response.status_code == 200

        duplicate_response = client.post(
            "/api/auth/register",
            json={"username": "operator", "password": "different123"},
        )
        assert duplicate_response.status_code == 400

        bad_login_response = client.post(
            "/api/auth/login",
            json={"username": "operator", "password": "wrong-password"},
        )
        assert bad_login_response.status_code == 401

        missing_auth_response = client.get("/api/auth/me")
        assert missing_auth_response.status_code == 401
    finally:
        app.dependency_overrides.pop(get_db, None)


class FakePcLoginAdapter:
    def create_qrcode(self):
        return {
            "cookies": {"a1": "temp-a1"},
            "qr_id": "qr-123",
            "code": "code-123",
            "qr_url": "https://example.test/qr",
            "device_state": {"fingerprintReady": True},
            "host_cookie_state": {"values": {}, "order": []},
        }

    def check_qrcode_status(self, qr_id, code, cookies, device_state=None, host_cookie_state=None):
        assert qr_id == "qr-123"
        assert code == "code-123"
        assert cookies == {"a1": "temp-a1"}
        assert device_state == {"fingerprintReady": True}
        return {
            "status": "confirmed",
            # 真实扫码登录的产物：`web_session` 与 `id_token` **成对**出现（DEF-051）。
            "cookies": {
                "a1": "final-a1",
                "web_session": "session-123",
                "id_token": "final-id-token",
            },
            "device_state": device_state,
            "host_cookie_state": host_cookie_state,
        }

    def get_user_info(self, cookies):
        assert cookies["web_session"] == "session-123"
        return {
            "external_user_id": "xhs-user-1",
            "nickname": "cat",
            "avatar_url": "https://example.test/avatar.webp",
        }


def _fake_creator_jar(a1: str) -> dict:
    """**真实形态**的创作者 jar（2026-09-21 拆分后必须能按形态认出是 creator 平台）。

    带 `access-token-creator.*` 域令牌 —— 少了它，这份 jar 会被分类器判成 www，
    「派生创作者 Cookie 落在 creator 平台」这条契约在测试里就等于没测
    （夹具少一种形态 = 那条分支零覆盖，DEF-059 的教训）。
    """
    return {
        "a1": a1,
        "customer-sso-sid": "sso-456",
        "access-token-creator.xiaohongshu.com": "creator-token-456",
    }


def _stored_platforms(version) -> dict:
    """读一行密文里的 `{平台: jar}`（信封是唯一存储形态，2026-09-21 起）。"""
    from backend.app.core.security import decrypt_text
    from backend.app.services.account_service import parse_channel_envelope

    return parse_channel_envelope(decrypt_text(version.encrypted_cookies))


class FakeCreatorLoginAdapter:
    def create_qrcode(self):
        return {
            "cookies": {"a1": "creator-temp-a1"},
            "qr_id": "creator-qr-123",
            "qr_url": "https://example.test/creator-qr",
        }

    def check_qrcode_status(self, qr_id, cookies):
        assert qr_id == "creator-qr-123"
        assert cookies == {"a1": "creator-temp-a1"}
        return {"status": "confirmed", "cookies": _fake_creator_jar("creator-final-a1")}

    def get_user_info(self, cookies):
        assert cookies["customer-sso-sid"] == "sso-456"
        return {
            "external_user_id": "creator-user-1",
            "nickname": "creator-cat",
            "avatar_url": "https://example.test/creator-avatar.webp",
        }

    def exchange_from_user_cookies(self, user_cookies):
        assert user_cookies["a1"] in {"final-a1", "phone-final-a1", "cookie-a1"}
        return {"status": "confirmed", "cookies": _fake_creator_jar(user_cookies["a1"])}


class FailingCreatorExchangeAdapter(FakeCreatorLoginAdapter):
    def exchange_from_user_cookies(self, user_cookies):
        raise RuntimeError("creator exchange failed")


class FailingQrLoginAdapter:
    def create_qrcode(self):
        raise RuntimeError("proxy refused while creating qrcode")


class FakePhoneLoginAdapter:
    def create_phone_session(self, phone):
        assert phone == "13800138000"
        return {"cookies": {"a1": "phone-temp-a1"}, "message": "sent"}

    def confirm_phone_login(self, phone, code, cookies):
        assert phone == "13800138000"
        assert code == "123456"
        assert cookies == {"a1": "phone-temp-a1"}
        return {"status": "confirmed", "cookies": {"a1": "phone-final-a1", "web_session": "phone-session", "id_token": "phone-id-token"}}

    def get_user_info(self, cookies):
        assert cookies["web_session"] == "phone-session"
        return {
            "external_user_id": "phone-user-1",
            "nickname": "phone-cat",
            "avatar_url": "https://example.test/phone-avatar.webp",
        }


#: 最近一次 `_register_and_get_access_token` 注册出的用户 id。
#: ⚠️ 播种必须挂到**注册的那个用户**名下：一个用例常注册 owner + intruder 两个，
#: 「取最后一个」就把映射建在入侵者身上，调用方（owner）自然报 400 未配置。
_last_registered_user_id = None


def _register_and_get_access_token(username: str = "operator") -> str:
    """注册一个测试用户并返回 access token。

    ## ⚠️ v1.1.0：注册出来的用户默认是 **pending（待审）**

    用户口径「注册改为邮箱验证 + 待审，与 dataPanel 一致」⇒ 新用户
    `status=pending`，而待审账号的**菜单与权限码都是空的**（那是刻意的：
    注册即授权会让待审机制形同虚设）。

    这几十条老用例假设「注册完就能读写业务接口」，在 v1.1.0 下会全 403。
    正确处理不是放宽产品语义，而是**让测试用户走完真实路径**：
    注册 → 管理员批准 → 拿到 `user` 角色的默认 view 码。
    """
    global _last_registered_user_id
    #: ⚠️ 注册开关：`test_api.py` 用的是**模块级裸 `client`**，不走
    #: `conftest.api_client` 夹具，也拿不到 `register_switch_open_for_tests`
    #: 那个 autouse 会话夹具的效果（它只 patch `BUILTIN_DEFAULTS`，
    #: 而 `live_global_bool` 在测试库无管理员行时回落到 `_baseline_bool`，
    #: 那条路径会读 `settings` / `config/default.yaml` = false）。
    #: 这里显式打开（只影响本模块的进程内状态，用例结束后由 conftest 复原）。
    from backend.app.services import settings_service as _ss

    _ss.BUILTIN_DEFAULTS["register.enabled"] = True
    _ss.invalidate(key="register.enabled")

    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "secret123"},
    )
    assert response.status_code == 200, f"注册失败：{response.text[:300]}"
    body = response.json()
    if _test_session_factory is not None:
        _db = _test_session_factory()
        try:
            _last_registered_user_id = _db.scalar(
                select(User.id).where(User.username == username)
            )
        finally:
            _db.close()

    #: 批准（`pending` → `normal`）：等价于管理员在用户管理里点「批准」。
    #: 只改状态、不写 `role_menus` —— `user` 角色的默认可见项来自**模板行**
    #: （`user_id IS NULL`），批准后自动继承，与生产完全一致。
    if _test_session_factory is not None:
        _db = _test_session_factory()
        try:
            row = _db.scalar(select(User).where(User.username == username))
            if row is not None:
                #: 提成 admin：测试库（内存 SQLite）**没有播种 `role_menus` 模板行**，
                #: 所以「批准后继承 user 角色默认 view 码」这条路在测试库里走不通。
                #: admin 的权限由代码派生 `ALL_PERMS`，不需要任何授权行 ——
                #: 这些用例测的是业务逻辑，不是权限矩阵（后者见 test_rbac_service / test_system_api）。
                row.role = "admin"
                row.status = "normal"
                _db.commit()
                #: ⚠️ **不重新登录**。`get_current_user` 每次请求都从库里重查用户，
                #: 所以令牌不需要换；而重新登录会**多打一次平台/库往返**，且在
                #: `_override_database` 中途换库的用例里会拿到另一套会话（实测
                #: 换成重登后 62 条用例集体 403，退回原地提权即恢复）。
        finally:
            _db.close()
    return body["access_token"]


def test_xhs_pc_qrcode_login_session_persists_and_confirms_account(tmp_path):
    from backend.app.api.login_sessions import get_creator_login_adapter, get_pc_login_adapter
    from backend.app.core.database import get_db
    from backend.app.core.security import decrypt_text
    from backend.app.models import AccountCookieVersion, LoginSession, PlatformAccount

    db_dependency = _override_database(tmp_path)
    app.dependency_overrides[get_pc_login_adapter] = lambda: FakePcLoginAdapter()
    app.dependency_overrides[get_creator_login_adapter] = lambda: FailingCreatorExchangeAdapter()
    try:
        access_token = _register_and_get_access_token()

        create_response = client.post(
            "/api/xhs/login-sessions/pc/qrcode",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert create_response.status_code == 200
        created = create_response.json()
        assert created["status"] == "pending"
        assert created["qr_url"] == "https://example.test/qr"
        assert created["qr_image_data_url"].startswith("data:image/png;base64,")
        assert isinstance(created["session_id"], int)

        db = next(app.dependency_overrides[get_db]())
        try:
            stored_session = db.get(LoginSession, created["session_id"])
            assert stored_session.platform == "xhs"
            assert stored_session.sub_type == "pc"
            assert stored_session.qr_id == "qr-123"
            assert stored_session.code == "code-123"
            stored_temp = json.loads(decrypt_text(stored_session.encrypted_temp_cookies))
            assert stored_temp["cookies"] == {"a1": "temp-a1"}
            # device_state/host_cookie_state are what keep the polling
            # request on the same device identity as the create request.
            assert stored_temp["device_state"] == {"fingerprintReady": True}
            assert "host_cookie_state" in stored_temp
        finally:
            db.close()

        poll_response = client.get(
            f"/api/xhs/login-sessions/{created['session_id']}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert poll_response.status_code == 200
        polled = poll_response.json()
        assert polled["status"] == "confirmed"
        assert polled["account"]["nickname"] == "cat"
        assert polled["creator_account"] is None

        accounts_response = client.get(
            "/api/accounts?platform=xhs",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert accounts_response.status_code == 200
        accounts_payload = accounts_response.json()
        assert accounts_payload["total"] == 1
        assert accounts_payload["items"][0]["nickname"] == "cat"
        assert accounts_payload["items"][0]["sub_type"] == "pc"

        db = next(app.dependency_overrides[get_db]())
        try:
            account = db.query(PlatformAccount).one()
            assert account.platform == "xhs"
            assert account.sub_type == "pc"
            assert account.external_user_id == "xhs-user-1"
            cookie_version = db.query(AccountCookieVersion).one()
            assert cookie_version.platform_account_id == account.id
            assert _stored_platforms(cookie_version) == {
                "www": '{"a1":"final-a1","web_session":"session-123","id_token":"final-id-token"}'
            }
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_pc_login_adapter, None)
        app.dependency_overrides.pop(get_creator_login_adapter, None)


def test_xhs_pc_qrcode_sync_creator_is_a_live_preference_not_a_new_session(tmp_path):
    """Ticking "sync Creator" must not mint a second login session.

    Regression for the reported symptom: the drawer keyed QR creation on the
    checkbox, so ticking it (usually right after scanning) replaced the QR the
    user had just scanned. The panel then polled a session nobody had scanned
    and the page appeared completely unresponsive.

    The fix sends ``sync_creator`` as a *poll-time override*, so this asserts
    both halves: exactly ONE session exists, and the override still performs the
    Creator sync.
    """
    from backend.app.api.login_sessions import get_creator_login_adapter, get_pc_login_adapter
    from backend.app.core.database import get_db
    from backend.app.models import LoginSession, PlatformAccount

    db_dependency = _override_database(tmp_path)
    app.dependency_overrides[get_pc_login_adapter] = lambda: FakePcLoginAdapter()
    app.dependency_overrides[get_creator_login_adapter] = lambda: FakeCreatorLoginAdapter()
    try:
        access_token = _register_and_get_access_token("sync-toggle-operator")
        headers = {"Authorization": f"Bearer {access_token}"}

        # Session created with the box unticked, exactly like opening the drawer.
        created = client.post(
            "/api/xhs/login-sessions/pc/qrcode",
            json={"sync_creator": False},
            headers=headers,
        ).json()

        # ...then the user ticks it. No new request to /pc/qrcode is made.
        poll_response = client.get(
            f"/api/xhs/login-sessions/{created['session_id']}?sync_creator=true",
            headers=headers,
        )
        assert poll_response.status_code == 200
        polled = poll_response.json()
        assert polled["status"] == "confirmed"
        assert polled["account"]["sub_type"] == "pc"
        assert polled["creator_account"] is not None, "勾选后必须真的同步 Creator 账号"
        assert polled["creator_account"]["sub_type"] == "creator"
        assert polled["creator_sync_error"] is None

        db = next(app.dependency_overrides[get_db]())
        try:
            assert db.query(LoginSession).count() == 1, "勾选复选框不得新建登录会话"
            sub_types = sorted(row.sub_type for row in db.query(PlatformAccount).all())
            assert sub_types == ["creator", "pc"], sub_types
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_pc_login_adapter, None)
        app.dependency_overrides.pop(get_creator_login_adapter, None)


def test_xhs_pc_qrcode_reports_creator_sync_failure_instead_of_swallowing_it(tmp_path):
    """A failed Creator sync must be visible in the response.

    The PC cookie exchange can legitimately fail (the creator session exchange
    rejects a www session). Previously every exception was swallowed into
    ``None``, so the API could not distinguish "synced" from "did nothing" and
    the checkbox looked like a no-op. The PC login itself must still succeed.
    """
    from backend.app.api.login_sessions import get_creator_login_adapter, get_pc_login_adapter
    from backend.app.core.database import get_db
    from backend.app.models import PlatformAccount

    db_dependency = _override_database(tmp_path)
    app.dependency_overrides[get_pc_login_adapter] = lambda: FakePcLoginAdapter()
    app.dependency_overrides[get_creator_login_adapter] = lambda: FailingCreatorExchangeAdapter()
    try:
        access_token = _register_and_get_access_token("sync-fail-operator")
        headers = {"Authorization": f"Bearer {access_token}"}

        created = client.post(
            "/api/xhs/login-sessions/pc/qrcode", json={"sync_creator": True}, headers=headers
        ).json()
        polled = client.get(
            f"/api/xhs/login-sessions/{created['session_id']}", headers=headers
        ).json()

        assert polled["status"] == "confirmed", "Creator 同步失败不得影响 PC 登录本身"
        assert polled["account"]["sub_type"] == "pc"
        assert polled["creator_account"] is None
        assert polled["creator_sync_error"], "同步失败必须回传原因，不能静默为 None"
        assert "creator exchange failed" in polled["creator_sync_error"]

        db = next(app.dependency_overrides[get_db]())
        try:
            sub_types = sorted(row.sub_type for row in db.query(PlatformAccount).all())
            assert sub_types == ["pc"], sub_types
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_pc_login_adapter, None)
        app.dependency_overrides.pop(get_creator_login_adapter, None)


@pytest.mark.slow
def test_xhs_pc_qrcode_login_updates_existing_account_for_same_external_user(tmp_path):
    from backend.app.api.accounts import get_pc_account_adapter
    from backend.app.api.login_sessions import get_creator_login_adapter, get_pc_login_adapter
    from backend.app.core.database import get_db
    from backend.app.core.security import decrypt_text
    from backend.app.models import AccountCookieVersion, PlatformAccount

    db_dependency = _override_database(tmp_path)
    app.dependency_overrides[get_pc_login_adapter] = lambda: FakePcLoginAdapter()
    app.dependency_overrides[get_pc_account_adapter] = lambda: FakePcLoginAdapter()
    app.dependency_overrides[get_creator_login_adapter] = lambda: FailingCreatorExchangeAdapter()
    try:
        access_token = _register_and_get_access_token("qr-upsert-operator")

        first_create = client.post(
            "/api/xhs/login-sessions/pc/qrcode",
            headers={"Authorization": f"Bearer {access_token}"},
        ).json()
        first_poll = client.get(
            f"/api/xhs/login-sessions/{first_create['session_id']}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert first_poll.status_code == 200
        assert first_poll.json()["account"]["action"] == "created"

        second_create = client.post(
            "/api/xhs/login-sessions/pc/qrcode",
            headers={"Authorization": f"Bearer {access_token}"},
        ).json()
        second_poll = client.get(
            f"/api/xhs/login-sessions/{second_create['session_id']}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert second_poll.status_code == 200
        assert second_poll.json()["account"]["action"] == "updated"

        accounts_response = client.get(
            "/api/accounts?platform=xhs",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        accounts_payload = accounts_response.json()
        assert accounts_payload["total"] == 1
        assert accounts_payload["items"][0]["updated_at"]
        assert accounts_payload["items"][0]["profile"] == {}

        check_response = client.post(
            f"/api/accounts/{accounts_payload['items'][0]['id']}/check",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert check_response.status_code == 200
        assert check_response.json()["status"] == "active", check_response.json().get("status_message")

        db = next(app.dependency_overrides[get_db]())
        try:
            accounts = db.query(PlatformAccount).all()
            assert len(accounts) == 1
            cookie_versions = db.query(AccountCookieVersion).order_by(AccountCookieVersion.id).all()
            assert len(cookie_versions) == 2
            assert cookie_versions[-1].platform_account_id == accounts[0].id
            assert _stored_platforms(cookie_versions[-1]) == {
                "www": '{"a1":"final-a1","web_session":"session-123","id_token":"final-id-token"}'
            }
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_pc_login_adapter, None)
        app.dependency_overrides.pop(get_pc_account_adapter, None)
        app.dependency_overrides.pop(get_creator_login_adapter, None)


def test_xhs_creator_qrcode_login_session_persists_and_confirms_account(tmp_path):
    from backend.app.api.login_sessions import get_creator_login_adapter
    from backend.app.core.database import get_db
    from backend.app.core.security import decrypt_text
    from backend.app.models import LoginSession, PlatformAccount

    db_dependency = _override_database(tmp_path)
    app.dependency_overrides[get_creator_login_adapter] = lambda: FakeCreatorLoginAdapter()
    try:
        access_token = _register_and_get_access_token("creator-operator")

        create_response = client.post(
            "/api/xhs/login-sessions/creator/qrcode",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert create_response.status_code == 200
        created = create_response.json()
        assert created["status"] == "pending"
        assert created["qr_url"] == "https://example.test/creator-qr"
        assert created["qr_image_data_url"].startswith("data:image/png;base64,")

        db = next(app.dependency_overrides[get_db]())
        try:
            stored_session = db.get(LoginSession, created["session_id"])
            assert stored_session.platform == "xhs"
            assert stored_session.sub_type == "creator"
            assert stored_session.qr_id == "creator-qr-123"
            assert stored_session.code is None
            # 断言走**唯一的读取口**（`_load_temp_state`），而不是把封套格式钉死：
            # 临时态是内部存储，封套（`{"cookies": …}`）随时可能加字段
            # （§18 刚加了 `target_account_id`）。钉原始 JSON 的用例会在**与它无关的
            # 改动**上变红，且它只证明「字节没变」，不证明「读得回来」。
            from backend.app.api.login_sessions import _load_temp_state

            assert _load_temp_state(decrypt_text(stored_session.encrypted_temp_cookies)).cookies == {
                "a1": "creator-temp-a1"
            }
        finally:
            db.close()

        poll_response = client.get(
            f"/api/xhs/login-sessions/{created['session_id']}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert poll_response.status_code == 200
        polled = poll_response.json()
        assert polled["status"] == "confirmed"
        assert polled["account"]["nickname"] == "creator-cat"

        accounts_response = client.get(
            "/api/accounts?platform=xhs",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        accounts_payload = accounts_response.json()
        assert accounts_payload["total"] == 1
        assert accounts_payload["items"][0]["sub_type"] == "creator"
        assert accounts_payload["items"][0]["nickname"] == "creator-cat"

        db = next(app.dependency_overrides[get_db]())
        try:
            account = db.query(PlatformAccount).one()
            assert account.sub_type == "creator"
            assert account.external_user_id == "creator-user-1"
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_creator_login_adapter, None)


def test_xhs_pc_qrcode_reports_adapter_failure_as_bad_gateway(tmp_path):
    from backend.app.api.login_sessions import get_creator_login_adapter, get_pc_login_adapter

    db_dependency = _override_database(tmp_path)
    app.dependency_overrides[get_pc_login_adapter] = lambda: FailingQrLoginAdapter()
    app.dependency_overrides[get_creator_login_adapter] = lambda: FailingCreatorExchangeAdapter()
    try:
        access_token = _register_and_get_access_token("qr-failure-operator")
        response = client.post(
            "/api/xhs/login-sessions/pc/qrcode",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 502
        assert "proxy refused" in response.json()["detail"]
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_pc_login_adapter, None)
        app.dependency_overrides.pop(get_creator_login_adapter, None)


def test_xhs_pc_qrcode_login_can_optionally_sync_creator_account(tmp_path):
    from backend.app.api.login_sessions import get_creator_login_adapter, get_pc_login_adapter
    from backend.app.core.database import get_db
    from backend.app.core.security import decrypt_text
    from backend.app.models import AccountCookieVersion, PlatformAccount

    db_dependency = _override_database(tmp_path)
    app.dependency_overrides[get_pc_login_adapter] = lambda: FakePcLoginAdapter()
    app.dependency_overrides[get_creator_login_adapter] = lambda: FakeCreatorLoginAdapter()
    try:
        access_token = _register_and_get_access_token("pc-auto-creator-operator")

        created = client.post(
            "/api/xhs/login-sessions/pc/qrcode",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"sync_creator": True},
        ).json()
        poll_response = client.get(
            f"/api/xhs/login-sessions/{created['session_id']}",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert poll_response.status_code == 200
        payload = poll_response.json()
        assert payload["status"] == "confirmed"
        assert payload["account"]["sub_type"] == "pc"
        assert payload["creator_account"]["sub_type"] == "creator"
        assert payload["creator_account"]["nickname"] == "creator-cat"

        accounts_response = client.get(
            "/api/accounts?platform=xhs",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        accounts_payload = accounts_response.json()
        assert accounts_payload["total"] == 2
        assert {item["sub_type"] for item in accounts_payload["items"]} == {"pc", "creator"}

        db = next(app.dependency_overrides[get_db]())
        try:
            accounts = db.query(PlatformAccount).order_by(PlatformAccount.sub_type.asc()).all()
            assert len(accounts) == 2
            creator_account = next(account for account in accounts if account.sub_type == "creator")
            assert creator_account.external_user_id == "creator-user-1"
            creator_cookie = (
                db.query(AccountCookieVersion)
                .filter(AccountCookieVersion.platform_account_id == creator_account.id)
                .order_by(AccountCookieVersion.id.desc())
                .one()
            )
            # 拆分后：派生/扫码得到的创作者 jar 落 **creator 平台**（不再是 ark）
            assert _stored_platforms(creator_cookie) == {
                "creator": '{"a1":"final-a1","customer-sso-sid":"sso-456","access-token-creator.xiaohongshu.com":"creator-token-456"}'
            }
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_pc_login_adapter, None)
        app.dependency_overrides.pop(get_creator_login_adapter, None)


def test_xhs_pc_phone_login_session_sends_code_and_confirms_account(tmp_path):
    from backend.app.api.login_sessions import get_creator_login_adapter, get_pc_login_adapter
    from backend.app.core.database import get_db
    from backend.app.core.security import decrypt_text
    from backend.app.models import LoginSession, PlatformAccount

    db_dependency = _override_database(tmp_path)
    app.dependency_overrides[get_pc_login_adapter] = lambda: FakePhoneLoginAdapter()
    app.dependency_overrides[get_creator_login_adapter] = lambda: FailingCreatorExchangeAdapter()
    try:
        access_token = _register_and_get_access_token("phone-operator")

        send_response = client.post(
            "/api/xhs/login-sessions/pc/phone/send-code",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"phone": "13800138000"},
        )
        assert send_response.status_code == 200
        sent = send_response.json()
        assert sent["status"] == "pending"
        assert sent["message"] == "sent"

        db = next(app.dependency_overrides[get_db]())
        try:
            stored_session = db.get(LoginSession, sent["session_id"])
            assert stored_session.sub_type == "pc"
            assert stored_session.login_method == "phone"
            # ⚠️ **2026-09-23 前提变化，断言改写（原文保留）**：旧断言
            # `stored_session.phone_mask == "138****8000"` 钉的是「手机号掩码落库」；
            # 用户口径「登录时手机号和邮箱密码等均不落库，数据库只存登录成功后的
            # 账号信息和会话临牌」后，`phone_mask` 列**整列移除**（迁移
            # `x7y8z9a0b1c2`）—— 连掩码都不留（只写不读的字段 + 掩码仍是手机号
            # 派生值）。新断言：属性不存在，且整行任何文本列都搜不到完整手机号。
            assert not hasattr(stored_session, "phone_mask")
            row_text = " ".join(
                str(x)
                for x in (
                    stored_session.qr_id,
                    stored_session.code,
                    stored_session.qr_url,
                    stored_session.encrypted_temp_cookies,
                )
            )
            assert "13800138000" not in row_text, "手机号（连掩码）不得落库"
            from backend.app.api.login_sessions import _load_temp_state

            state = _load_temp_state(decrypt_text(stored_session.encrypted_temp_cookies))
            assert state.cookies == {"a1": "phone-temp-a1"}
        finally:
            db.close()

        confirm_response = client.post(
            "/api/xhs/login-sessions/pc/phone/confirm",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"session_id": sent["session_id"], "phone": "13800138000", "code": "123456"},
        )
        assert confirm_response.status_code == 200
        confirmed = confirm_response.json()
        assert confirmed["status"] == "confirmed"
        assert confirmed["account"]["nickname"] == "phone-cat"
        assert confirmed["creator_account"] is None

        db = next(app.dependency_overrides[get_db]())
        try:
            account = db.query(PlatformAccount).one()
            assert account.sub_type == "pc"
            assert account.external_user_id == "phone-user-1"
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_pc_login_adapter, None)
        app.dependency_overrides.pop(get_creator_login_adapter, None)


def test_xhs_pc_phone_login_can_optionally_sync_creator_account(tmp_path):
    from backend.app.api.login_sessions import get_creator_login_adapter, get_pc_login_adapter
    from backend.app.core.database import get_db
    from backend.app.core.security import decrypt_text
    from backend.app.models import AccountCookieVersion, LoginSession, PlatformAccount

    db_dependency = _override_database(tmp_path)
    app.dependency_overrides[get_pc_login_adapter] = lambda: FakePhoneLoginAdapter()
    app.dependency_overrides[get_creator_login_adapter] = lambda: FakeCreatorLoginAdapter()
    try:
        access_token = _register_and_get_access_token("phone-auto-creator-operator")

        send_response = client.post(
            "/api/xhs/login-sessions/pc/phone/send-code",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"phone": "13800138000", "sync_creator": True},
        )
        assert send_response.status_code == 200
        sent = send_response.json()

        db = next(app.dependency_overrides[get_db]())
        try:
            stored_session = db.get(LoginSession, sent["session_id"])
            from backend.app.api.login_sessions import _load_temp_state

            state = _load_temp_state(decrypt_text(stored_session.encrypted_temp_cookies))
            assert state.cookies == {"a1": "phone-temp-a1"}
            assert state.sync_creator is True
        finally:
            db.close()

        confirm_response = client.post(
            "/api/xhs/login-sessions/pc/phone/confirm",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"session_id": sent["session_id"], "phone": "13800138000", "code": "123456"},
        )
        assert confirm_response.status_code == 200
        confirmed = confirm_response.json()
        assert confirmed["status"] == "confirmed"
        assert confirmed["account"]["sub_type"] == "pc"
        assert confirmed["creator_account"]["sub_type"] == "creator"
        assert confirmed["creator_account"]["nickname"] == "creator-cat"

        db = next(app.dependency_overrides[get_db]())
        try:
            accounts = db.query(PlatformAccount).order_by(PlatformAccount.sub_type.asc()).all()
            assert len(accounts) == 2
            creator_account = next(account for account in accounts if account.sub_type == "creator")
            creator_cookie = (
                db.query(AccountCookieVersion)
                .filter(AccountCookieVersion.platform_account_id == creator_account.id)
                .order_by(AccountCookieVersion.id.desc())
                .one()
            )
            assert _stored_platforms(creator_cookie) == {
                "creator": '{"a1":"phone-final-a1","customer-sso-sid":"sso-456","access-token-creator.xiaohongshu.com":"creator-token-456"}'
            }
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_pc_login_adapter, None)
        app.dependency_overrides.pop(get_creator_login_adapter, None)


def test_xhs_creator_phone_login_session_sends_code_and_confirms_account(tmp_path):
    from backend.app.api.login_sessions import get_creator_login_adapter

    db_dependency = _override_database(tmp_path)
    app.dependency_overrides[get_creator_login_adapter] = lambda: FakePhoneLoginAdapter()
    try:
        access_token = _register_and_get_access_token("creator-phone-operator")

        send_response = client.post(
            "/api/xhs/login-sessions/creator/phone/send-code",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"phone": "13800138000"},
        )
        assert send_response.status_code == 200
        sent = send_response.json()
        assert sent["status"] == "pending"

        confirm_response = client.post(
            "/api/xhs/login-sessions/creator/phone/confirm",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"session_id": sent["session_id"], "phone": "13800138000", "code": "123456"},
        )
        assert confirm_response.status_code == 200
        confirmed = confirm_response.json()
        assert confirmed["status"] == "confirmed"
        assert confirmed["account"]["nickname"] == "phone-cat"

        accounts_response = client.get(
            "/api/accounts?platform=xhs",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        accounts_payload = accounts_response.json()
        assert accounts_payload["items"][0]["sub_type"] == "creator"
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_creator_login_adapter, None)


def test_account_delete_requires_owner_and_removes_account(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.models import PlatformAccount

    db_dependency = _override_database(tmp_path)
    try:
        owner_token = _register_and_get_access_token("delete-account-owner")
        intruder_token = _register_and_get_access_token("delete-account-intruder")
        db = next(app.dependency_overrides[get_db]())
        try:
            account = PlatformAccount(
                user_id=1,
                platform="xhs",
                sub_type="pc",
                external_user_id="delete-user",
                nickname="Delete Me",
                status="active",
            )
            db.add(account)
            db.commit()
            db.refresh(account)
            account_id = account.id
        finally:
            db.close()

        intruder_response = client.delete(
            f"/api/accounts/{account_id}",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_response.status_code == 404

        owner_response = client.delete(
            f"/api/accounts/{account_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert owner_response.status_code == 200
        # 2026-09-20 起账号删除是**软删**（台账 §4.9 / 不变量 I3）。响应多两个字段把这件事
        # 讲清楚：`soft` 表示这是软删、`already_deleted` 支持幂等重调。
        # 行**必须仍在库中** —— 它存在的意义是让唯一索引永久成立、让重新导入能**复活**它。
        assert owner_response.json() == {
            "id": account_id,
            "status": "deleted",
            "soft": True,
            "already_deleted": False,
        }

        accounts_response = client.get(
            "/api/accounts?platform=xhs",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert accounts_response.json()["total"] == 0

        # 列表看不到 ≠ 行被删掉。物理删会让笔记变孤儿、重新导入必新建一条。
        verify_db = next(app.dependency_overrides[get_db]())
        try:
            row = verify_db.get(PlatformAccount, account_id)
            assert row is not None, "账号被物理删除了 —— 软删语义没有生效"
            assert row.deleted_at is not None, "软删标记没写"
        finally:
            verify_db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_xhs_pc_qrcode_requires_platform_login(tmp_path):
    get_db = _override_database(tmp_path)
    try:
        response = client.post("/api/xhs/login-sessions/pc/qrcode")
        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_accounts_list_requires_platform_login(tmp_path):
    get_db = _override_database(tmp_path)
    try:
        response = client.get("/api/accounts?platform=xhs")
        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(get_db, None)


class FakeCookieAccountAdapter:
    def __init__(self):
        self.calls = 0

    def get_user_info(self, cookies):
        self.calls += 1
        assert cookies["a1"] == "cookie-a1"
        return {
            "external_user_id": "cookie-user-1",
            "nickname": "cookie-cat",
            "avatar_url": "https://example.test/cookie-avatar.webp",
        }


class FakeSelfProfileAdapter:
    """网页版身份与账号资料的桩。

    ⚠️ **夹具按真实样本更正（2026-09-18，DEF-039）**：早期版本把
    `interactions` 放在 `/v2/user/me` 的响应里，与**线上实测形状不符**——
    真实 `/v2/user/me` 的 `data` 只有 8 个字段、**没有** `interactions`，
    计数在 `/v1/user/otherinfo`。夹具照着错误假设编，于是一整类缺陷
    （三项计数恒为空）在单测里永远绿着。
    """

    calls = []
    other_info_calls = []

    def get_self_profile(self, cookies_text):
        self.__class__.calls.append(cookies_text)
        return {
            "success": True,
            "msg": "ok",
            "data": {
                "basic_info": {
                    "nickname": "cookie-cat-live",
                    "images": "https://example.test/live-avatar.webp",
                    "red_id": "red-cookie-1",
                    "desc": "live profile",
                    "ip_location": "上海",
                },
                # 真实响应里**没有** interactions —— 计数见 get_other_info
            },
            "code": 0,
        }

    def get_other_info(self, cookies_text, user_id):
        self.__class__.other_info_calls.append((cookies_text, user_id))
        return (
            True,
            "ok",
            {
                "data": {
                    "basic_info": {"red_id": "red-cookie-1"},
                    "interactions": [
                        {"type": "follows", "name": "关注", "count": "28", "i18n_count": "28"},
                        {"type": "fans", "name": "粉丝", "count": "90", "i18n_count": "90"},
                        {"type": "interaction", "name": "获赞与收藏", "count": "340", "i18n_count": "340"},
                    ],
                }
            },
        )


class FailingCookieAccountAdapter:
    def get_user_info(self, cookies):
        raise RuntimeError("expired")


@pytest.mark.slow
def test_account_cookie_import_creates_account_and_health_check_updates_status(tmp_path):
    from backend.app.api.accounts import get_creator_account_adapter, get_pc_account_adapter
    from backend.app.core.database import get_db
    from backend.app.core.security import decrypt_text
    from backend.app.core.time import shanghai_now
    from backend.app.models import AccountCookieVersion, PlatformAccount

    db_dependency = _override_database(tmp_path)
    fake_adapter = FakeCookieAccountAdapter()
    app.dependency_overrides[get_pc_account_adapter] = lambda: fake_adapter
    app.dependency_overrides[get_creator_account_adapter] = lambda: FailingCreatorExchangeAdapter()
    try:
        access_token = _register_and_get_access_token("cookie-operator")
        before_create = shanghai_now()

        import_response = client.post(
            "/api/accounts/import-cookie",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"platform": "xhs", "sub_type": "pc", "cookie_string": "a1=cookie-a1; web_session=session; id_token=id-token"},
        )
        after_create = shanghai_now()
        assert import_response.status_code == 200
        imported = import_response.json()
        assert imported["nickname"] == "cookie-cat"
        assert imported["status"] == "active"

        db = next(app.dependency_overrides[get_db]())
        try:
            account = db.query(PlatformAccount).one()
            assert account.external_user_id == "cookie-user-1"
            assert before_create <= account.created_at <= after_create
            assert before_create <= account.updated_at <= after_create
            cookie_version = db.query(AccountCookieVersion).one()
            assert cookie_version.platform_account_id == account.id
            assert _stored_platforms(cookie_version) == {
                "www": "a1=cookie-a1; web_session=session; id_token=id-token"
            }
        finally:
            db.close()

        accounts_response = client.get(
            "/api/accounts?platform=xhs",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert accounts_response.status_code == 200
        assert accounts_response.json()["total"] == 1

        check_response = client.post(
            f"/api/accounts/{imported['id']}/check",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert check_response.status_code == 200
        checked = check_response.json()
        assert checked["status"] == "active"
        assert checked["nickname"] == "cookie-cat"
        assert fake_adapter.calls >= 2
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_pc_account_adapter, None)
        app.dependency_overrides.pop(get_creator_account_adapter, None)


def test_account_cookie_import_for_pc_can_optionally_sync_creator_account(tmp_path):
    from backend.app.api.accounts import get_creator_account_adapter, get_pc_account_adapter
    from backend.app.core.database import get_db
    from backend.app.core.security import decrypt_text
    from backend.app.models import AccountCookieVersion, PlatformAccount

    db_dependency = _override_database(tmp_path)
    fake_pc_adapter = FakeCookieAccountAdapter()
    app.dependency_overrides[get_pc_account_adapter] = lambda: fake_pc_adapter
    app.dependency_overrides[get_creator_account_adapter] = lambda: FakeCreatorLoginAdapter()
    try:
        access_token = _register_and_get_access_token("cookie-auto-creator-operator")

        import_response = client.post(
            "/api/accounts/import-cookie",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "platform": "xhs",
                "sub_type": "pc",
                "cookie_string": "a1=cookie-a1; web_session=session; id_token=id-token",
                "sync_creator": True,
            },
        )
        assert import_response.status_code == 200

        accounts_response = client.get(
            "/api/accounts?platform=xhs",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        accounts_payload = accounts_response.json()
        # §11.5（v1.24）：派生出来的创作者 Cookie **挂在同一个账号的 ark 通道**上，
        # 不再新建第二行 —— 「一个身份两行账号」正是本次改造要消灭的形态。
        assert accounts_payload["total"] == 1
        assert {item["sub_type"] for item in accounts_payload["items"]} == {"pc"}

        db = next(app.dependency_overrides[get_db]())
        try:
            accounts = db.query(PlatformAccount).all()
            assert len(accounts) == 1
            versions = (
                db.query(AccountCookieVersion)
                .filter(AccountCookieVersion.platform_account_id == accounts[0].id)
                .order_by(AccountCookieVersion.id.asc())
                .all()
            )
            # 两版 Cookie、两个平台：www（导入的 PC jar）+ creator（派生的创作者 jar）。
            assert [set(_stored_platforms(version)) for version in versions] == [{"www"}, {"creator"}]
            assert _stored_platforms(versions[1]) == {
                "creator": '{"a1":"cookie-a1","customer-sso-sid":"sso-456","access-token-creator.xiaohongshu.com":"creator-token-456"}'
            }
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_pc_account_adapter, None)
        app.dependency_overrides.pop(get_creator_account_adapter, None)


def test_account_check_refreshes_xhs_self_profile_metrics(tmp_path):
    from backend.app.api.accounts import (
        get_creator_account_adapter,
        get_pc_account_adapter,
        get_xhs_self_profile_adapter,
    )

    db_dependency = _override_database(tmp_path)
    fake_adapter = FakeCookieAccountAdapter()
    FakeSelfProfileAdapter.calls = []
    app.dependency_overrides[get_pc_account_adapter] = lambda: fake_adapter
    app.dependency_overrides[get_creator_account_adapter] = lambda: FailingCreatorExchangeAdapter()
    app.dependency_overrides[get_xhs_self_profile_adapter] = lambda: FakeSelfProfileAdapter()
    try:
        access_token = _register_and_get_access_token("profile-metrics-operator")
        import_response = client.post(
            "/api/accounts/import-cookie",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"platform": "xhs", "sub_type": "pc", "cookie_string": "a1=cookie-a1; web_session=session; id_token=id-token"},
        )
        account_id = import_response.json()["id"]

        check_response = client.post(
            f"/api/accounts/{account_id}/check",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert check_response.status_code == 200
        checked = check_response.json()
        assert checked["status"] == "active"
        assert checked["nickname"] == "cookie-cat-live"
        assert checked["avatar_url"] == "https://example.test/live-avatar.webp"
        # 计数一律规范化成 int 后落库（平台给的是字符串 "90"；直接透传会让前端
        # 一会儿渲染字符串、一会儿渲染数字）。这是本次一并定下的口径。
        assert checked["profile"]["followers"] == 90
        assert checked["profile"]["following"] == 28
        assert checked["profile"]["likes"] == 340
        assert checked["profile"]["red_id"] == "red-cookie-1"
        # 身份一次（/v2/user/me，导入 + 检查各一次），计数另走 /v1/user/otherinfo。
        # 计数**不能**再从身份响应里读：实测它没有 interactions（DEF-039）。
        assert FakeSelfProfileAdapter.calls == ["a1=cookie-a1; web_session=session; id_token=id-token", "a1=cookie-a1; web_session=session; id_token=id-token"]
        assert FakeSelfProfileAdapter.other_info_calls == [
            ("a1=cookie-a1; web_session=session; id_token=id-token", "cookie-user-1"),
            ("a1=cookie-a1; web_session=session; id_token=id-token", "cookie-user-1"),
        ]
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_pc_account_adapter, None)
        app.dependency_overrides.pop(get_creator_account_adapter, None)
        app.dependency_overrides.pop(get_xhs_self_profile_adapter, None)


@pytest.mark.slow
def test_account_check_enforces_ownership_and_marks_expired_on_adapter_failure(tmp_path):
    from backend.app.api.accounts import get_pc_account_adapter

    db_dependency = _override_database(tmp_path)
    app.dependency_overrides[get_pc_account_adapter] = lambda: FakeCookieAccountAdapter()
    try:
        owner_token = _register_and_get_access_token("owner-operator")
        intruder_token = _register_and_get_access_token("intruder-operator")

        import_response = client.post(
            "/api/accounts/import-cookie",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "sub_type": "pc", "cookie_string": "a1=cookie-a1; web_session=session; id_token=id-token"},
        )
        account_id = import_response.json()["id"]

        forbidden_response = client.post(
            f"/api/accounts/{account_id}/check",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert forbidden_response.status_code == 404
    finally:
        app.dependency_overrides.pop(get_pc_account_adapter, None)

    app.dependency_overrides[get_pc_account_adapter] = lambda: FailingCookieAccountAdapter()
    try:
        expired_response = client.post(
            f"/api/accounts/{account_id}/check",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert expired_response.status_code == 200
        assert expired_response.json()["status"] == "expired"
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_pc_account_adapter, None)


def _stub_creator_transport(monkeypatch):
    """Replace the Creator Node signer + HTTP transport for offline tests."""
    import xhs_utils.xhs_creator.auth as creator_auth
    import xhs_utils.xhs_creator.http as creator_http
    import xhs_utils.xhs_creator.params as creator_params
    import xhs_utils.xhs_creator.runtime as creator_runtime

    class CannedResponse:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class CannedTransport:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, url, **kwargs):
            return CannedResponse({"success": True, "msg": "获取fileIds成功"})

        def post(self, url, **kwargs):
            return CannedResponse({"success": True})

        def close(self):
            pass

        def state_snapshot(self):
            return {"backend": "canned"}

    canned_sign_result = {
        "xs": "x-s-value",
        "xt": "1789715127000",
        "xs_common": "x-s-common-value",
        "x3": "mns0101_" + "a" * 187,
    }
    # auth.py binds CreatorHttpClient into its own namespace at import time, so
    # patching the http module alone does not reach XHSAuth.__post_init__.
    monkeypatch.setattr(creator_auth, "CreatorHttpClient", CannedTransport)
    monkeypatch.setattr(creator_http, "CreatorHttpClient", CannedTransport)
    monkeypatch.setattr(creator_runtime, "run_signer", lambda *args, **kwargs: canned_sign_result)
    # params.py binds run_signer into its own module namespace at import time.
    monkeypatch.setattr(creator_params, "run_signer", lambda *args, **kwargs: canned_sign_result)
    # ⚠️ `XhsCreatorApiAdapter._api()` 走 `shared_client(SESSION_CREATOR, CreatorHttpClient)`
    # —— 该函数**命中进程级缓存时不调工厂** ⇒ 只 patch `CreatorHttpClient` 是**假防线**：
    # 只要**任何**更早的用例（实测：`test_account_metrics.py` 的 creator 通道日志用例）
    # 已经建过真实 creator 会话，这里打上的替身永远不生效，permit 会**真打平台**、
    # 回真实 406 ⇒ `success=False` ⇒ 账号被判 `expired`（症状：单跑绿、全量红）。
    # 故必须把该平台的缓存会话**清掉**，工厂才会被调用、替身才落到新会话上。
    from backend.app.adapters.xhs import http_session as _http_session

    _http_session.close_all()


# A Creator a1 is 52 chars; the mns0101 x3 length (196/197/198) scales with it,
# so a short placeholder would trip the signer's tier gate.
_REAL_A1 = "1a0b3559a300xpxexi8l5bp209ejqe9simol83d2m50000152319"


class _CreatorAccountContext:
    """Create a creator account + cookie version backed by a realistic a1."""

    def __init__(self, tmp_path, username, cookies_text):
        from backend.app.core.database import get_db
        from backend.app.core.security import encrypt_text
        from backend.app.models import AccountCookieVersion, PlatformAccount

        self.db_dependency = _override_database(tmp_path)
        self.access_token = _register_and_get_access_token(username)
        db = next(app.dependency_overrides[get_db]())
        try:
            account = PlatformAccount(
                user_id=1,
                platform="xhs",
                sub_type="creator",
                external_user_id="creator-user",
                nickname="Creator account",
                status="active",
            )
            db.add(account)
            db.flush()
            db.add(
                cookie_version(account.id, cookies_text, CHANNEL_WWW)
            )
            db.commit()
            self.account_id = account.id
        finally:
            db.close()

    def cleanup(self):
        app.dependency_overrides.pop(self.db_dependency, None)


def _creator_login_adapter_for(a1):
    """A FakeCreatorLoginAdapter variant whose assertions accept the test a1."""

    class _Adapter(FakeCreatorLoginAdapter):
        def get_user_info(self, cookies):
            assert cookies.get("a1") == a1
            return {
                "external_user_id": "creator-user-1",
                "nickname": "creator-cat",
                "avatar_url": "https://example.test/creator-avatar.webp",
                "profile": {},
            }

    return _Adapter


def test_account_check_creator_builds_auth_and_calls_get_fileIds(monkeypatch, tmp_path):
    """The creator check reaches the permit endpoint with a real XHSCreatorAuth."""
    from backend.app.api.accounts import (
        get_creator_account_adapter,
        get_creator_api_adapter_factory,
        get_pc_account_adapter,
    )
    from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter

    _stub_creator_transport(monkeypatch)
    ctx = _CreatorAccountContext(
        tmp_path,
        "creator-check-owner",
        f"a1={_REAL_A1}; web_session=creator-session",
    )
    app.dependency_overrides[get_pc_account_adapter] = lambda: FakePcLoginAdapter()
    app.dependency_overrides[get_creator_account_adapter] = lambda: _creator_login_adapter_for(_REAL_A1)()
    app.dependency_overrides[get_creator_api_adapter_factory] = lambda: XhsCreatorApiAdapter
    try:
        response = client.post(
            f"/api/accounts/{ctx.account_id}/check",
            headers={"Authorization": f"Bearer {ctx.access_token}"},
        )
        assert response.status_code == 200
        checked = response.json()
        assert checked["status"] == "active"
        assert checked["status_message"] == ""
        assert checked["nickname"] == "creator-cat"
    finally:
        app.dependency_overrides.pop(get_pc_account_adapter, None)
        app.dependency_overrides.pop(get_creator_account_adapter, None)
        app.dependency_overrides.pop(get_creator_api_adapter_factory, None)
        ctx.cleanup()


def test_account_check_creator_reports_expired_when_permit_fails(monkeypatch, tmp_path):
    from backend.app.api.accounts import (
        get_creator_account_adapter,
        get_creator_api_adapter_factory,
        get_pc_account_adapter,
    )

    _stub_creator_transport(monkeypatch)

    class FailingPermitAdapter:
        def __init__(self, cookies):
            self.cookies = cookies

        def check_upload_permit(self, media_type="image"):
            return False, "登录态无效"

    ctx = _CreatorAccountContext(
        tmp_path,
        "creator-check-permit-owner",
        f"a1={_REAL_A1}; web_session=creator-session",
    )
    app.dependency_overrides[get_pc_account_adapter] = lambda: FakePcLoginAdapter()
    app.dependency_overrides[get_creator_account_adapter] = lambda: _creator_login_adapter_for(_REAL_A1)()
    app.dependency_overrides[get_creator_api_adapter_factory] = lambda: FailingPermitAdapter
    try:
        response = client.post(
            f"/api/accounts/{ctx.account_id}/check",
            headers={"Authorization": f"Bearer {ctx.access_token}"},
        )
        assert response.status_code == 200
        checked = response.json()
        assert checked["status"] == "expired"
        assert "上传凭证获取失败" in checked["status_message"]
    finally:
        app.dependency_overrides.pop(get_pc_account_adapter, None)
        app.dependency_overrides.pop(get_creator_account_adapter, None)
        app.dependency_overrides.pop(get_creator_api_adapter_factory, None)
        ctx.cleanup()


def test_account_check_creator_marks_expired_when_auth_is_invalid(monkeypatch, tmp_path):
    """An unauthenticated cookie must surface as expired, not as a 500."""
    from backend.app.api.accounts import (
        get_creator_account_adapter,
        get_creator_api_adapter_factory,
        get_pc_account_adapter,
    )
    from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter

    _stub_creator_transport(monkeypatch)
    # No session cookie: XHSCreatorAuth.from_cookie rejects this, so the permit
    # branch must fail closed to "expired".
    ctx = _CreatorAccountContext(tmp_path, "creator-check-badauth-owner", f"a1={_REAL_A1}")
    app.dependency_overrides[get_pc_account_adapter] = lambda: FakePcLoginAdapter()
    app.dependency_overrides[get_creator_account_adapter] = lambda: _creator_login_adapter_for(_REAL_A1)()
    app.dependency_overrides[get_creator_api_adapter_factory] = lambda: XhsCreatorApiAdapter
    try:
        response = client.post(
            f"/api/accounts/{ctx.account_id}/check",
            headers={"Authorization": f"Bearer {ctx.access_token}"},
        )
        assert response.status_code == 200
        checked = response.json()
        assert checked["status"] == "expired"
        assert "上传凭证验证异常" in checked["status_message"]
    finally:
        app.dependency_overrides.pop(get_pc_account_adapter, None)
        app.dependency_overrides.pop(get_creator_account_adapter, None)
        app.dependency_overrides.pop(get_creator_api_adapter_factory, None)
        ctx.cleanup()


def test_creator_api_adapter_check_upload_permit_passes_media_type(monkeypatch):
    """XHS_Creator_Apis must be built with auth and get_fileIds(media_type)."""
    from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter

    captured = {}

    class RecordingApis:
        def __init__(self, auth):
            from xhs_utils.xhs_creator import XHSCreatorAuth

            assert isinstance(auth, XHSCreatorAuth), (
                "XHS_Creator_Apis must receive an XHSCreatorAuth instance"
            )
            captured["auth_type"] = type(auth).__name__
            captured["a1"] = auth.a1

        def get_fileIds(self, media_type, proxies=None):
            captured["media_type"] = media_type
            captured["proxies"] = proxies
            return True, "获取fileIds成功", (None, None)

    import apis.xhs_creator_apis as creator_apis_module

    # The adapter imports XHS_Creator_Apis lazily inside _api(), so patching the
    # apis module namespace is enough; the adapter module itself never holds it.
    monkeypatch.setattr(creator_apis_module, "XHS_Creator_Apis", RecordingApis)
    monkeypatch.setattr(creator_apis_module, "XHSCreatorApis", RecordingApis)
    ok, message = XhsCreatorApiAdapter(f"a1={_REAL_A1}; web_session=session").check_upload_permit("image")

    assert ok is True
    assert captured == {
        "auth_type": "XHSCreatorAuth",
        "a1": _REAL_A1,
        "media_type": "image",
        "proxies": None,
    }



class FakeXhsPcSearchAdapter:
    calls = []

    def __init__(self, cookies):
        self.cookies = cookies

    def search_note(self, keyword, page=1, **kwargs):
        self.__class__.calls.append({"cookies": self.cookies, "keyword": keyword, "page": page, **kwargs})
        return (
            True,
            "ok",
            {
                "success": True,
                "msg": "ok",
                "data": {
                    "has_more": True,
                    "items": [
                        {
                            "model_type": "note",
                            "xsec_token": "xsec-search-001",
                            "note_card": {
                                "note_id": "note-001",
                                "display_title": "低卡早餐搜索笔记",
                                "desc": "适合工作日的早餐搭配",
                                "type": "normal",
                                "user": {
                                    "user_id": "author-001",
                                    "nickname": "早餐研究员",
                                    "avatar": "https://example.test/avatar.webp",
                                },
                                "cover": {"url_default": "https://example.test/cover.webp"},
                                "interact_info": {
                                    "liked_count": "1234",
                                    "collected_count": "456",
                                    "comment_count": "78",
                                    "share_count": "9",
                                },
                            },
                        }
                    ],
                },
            },
        )


def _create_pc_account_with_cookie(tmp_path, username="search-owner"):
    from backend.app.core.database import get_db
    from backend.app.core.security import encrypt_text
    from backend.app.models import AccountCookieVersion, PlatformAccount

    db_dependency = _override_database(tmp_path)
    access_token = _register_and_get_access_token(username)
    db = next(app.dependency_overrides[get_db]())
    try:
        account = PlatformAccount(
            user_id=1,
            platform="xhs",
            sub_type="pc",
            external_user_id="search-user",
            nickname="搜索账号",
            status="active",
        )
        db.add(account)
        db.flush()
        db.add(
            cookie_version(account.id, '{"a1":"json-a1","web_session":"json-session","id_token":"json-id-token"}', CHANNEL_WWW)
        )
        db.commit()
        account_id = account.id
    finally:
        db.close()
    return db_dependency, access_token, account_id


def _create_creator_account_with_cookie(tmp_path, username="creator-owner"):
    from backend.app.core.database import get_db
    from backend.app.core.security import encrypt_text
    from backend.app.models import AccountCookieVersion, PlatformAccount

    db_dependency = _override_database(tmp_path)
    access_token = _register_and_get_access_token(username)
    db = next(app.dependency_overrides[get_db]())
    try:
        account = PlatformAccount(
            user_id=1,
            platform="xhs",
            sub_type="creator",
            external_user_id="creator-user",
            nickname="Creator account",
            status="active",
        )
        db.add(account)
        db.flush()
        # 创作者平台路由（topics / publish / 素材上传）打的是 `creator.xiaohongshu.com`，
        # 2026-09-21 拆分后**只认 creator 平台的令牌** —— 夹具给 www 形态的 jar 会
        # 让这些路由全部 400（正是拆分要暴露的那类错）。
        db.add(
            cookie_version(
                account.id,
                '{"a1":"creator-a1","customer-sso-sid":"creator-sso",'
                '"access-token-creator.xiaohongshu.com":"creator-token"}',
                CHANNEL_CREATOR,
            )
        )
        db.commit()
        account_id = account.id
    finally:
        db.close()
    return db_dependency, access_token, account_id


def test_xhs_pc_note_search_uses_owned_account_cookie_and_normalizes_results(tmp_path):
    from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory

    db_dependency, access_token, account_id = _create_pc_account_with_cookie(tmp_path)
    FakeXhsPcSearchAdapter.calls = []
    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: FakeXhsPcSearchAdapter
    try:
        response = client.post(
            "/api/xhs/pc/search/notes",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"account_id": account_id, "keyword": "低卡早餐", "page": 2},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["page"] == 2
        assert payload["has_more"] is True
        assert FakeXhsPcSearchAdapter.calls == [
            {
                "cookies": "a1=json-a1; web_session=json-session; id_token=json-id-token",
                "keyword": "低卡早餐",
                "page": 2,
                "sort_type_choice": 0,
                "note_type": 0,
                "note_time": 0,
                "note_range": 0,
                "pos_distance": 0,
                "geo": "",
            }
        ]
        note = payload["items"][0]
        assert note["note_id"] == "note-001"
        assert note["note_url"] == "https://www.xiaohongshu.com/explore/note-001?xsec_token=xsec-search-001&xsec_source=pc_feed"
        assert note["title"] == "低卡早餐搜索笔记"
        assert note["content"] == "适合工作日的早餐搭配"
        assert note["author_name"] == "早餐研究员"
        assert note["author_id"] == "author-001"
        assert note["cover_url"] == "https://example.test/cover.webp"
        assert note["likes"] == 1234
        assert note["collects"] == 456
        assert note["comments"] == 78
        assert note["shares"] == 9
        assert note["type"] == "normal"
        assert note["raw"]["model_type"] == "note"
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)


def test_xhs_pc_note_search_rejects_missing_auth_and_cross_user_account(tmp_path):
    from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory

    db_dependency, owner_token, account_id = _create_pc_account_with_cookie(tmp_path, "search-owner-2")
    intruder_token = _register_and_get_access_token("search-intruder")
    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: FakeXhsPcSearchAdapter
    try:
        missing_auth_response = client.post(
            "/api/xhs/pc/search/notes",
            json={"account_id": account_id, "keyword": "低卡早餐", "page": 1},
        )
        assert missing_auth_response.status_code == 401

        intruder_response = client.post(
            "/api/xhs/pc/search/notes",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"account_id": account_id, "keyword": "低卡早餐", "page": 1},
        )
        assert intruder_response.status_code == 404

        owner_response = client.post(
            "/api/xhs/pc/search/notes",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"account_id": account_id, "keyword": "低卡早餐", "page": 1},
        )
        assert owner_response.status_code == 200
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)


def test_xhs_pc_note_detail_uses_owned_account_cookie_and_normalizes_result(tmp_path):
    from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory

    class FakeXhsPcDetailAdapter:
        calls = []

        def __init__(self, cookies):
            self.cookies = cookies

        def get_note_info(self, url):
            self.calls.append({"cookies": self.cookies, "url": url})
            return (
                True,
                "ok",
                {
                    "success": True,
                    "data": {
                        "items": [
                            {
                                "note_card": {
                                    "note_id": "detail-note-001",
                                    "title": "Detail title",
                                    "desc": "Detail body",
                                    "type": "video",
                                    "user": {
                                        "user_id": "author-detail",
                                        "nickname": "Detail author",
                                        "avatar": "https://example.test/author.webp",
                                    },
                                    "image_list": [
                                        {
                                            "url_default": "https://example.test/image-preview.webp",
                                            "info_list": [
                                                {"image_scene": "WB_DFT", "url": "https://example.test/image-low.webp"},
                                                {"image_scene": "WB_PRV", "url": "https://example.test/image-high.webp"},
                                            ],
                                        },
                                    ],
                                    "video": {
                                        "media": {
                                            "stream": {
                                                "h264": [
                                                    {
                                                        "master_url": "https://sns-video-hw.xhscdn.com/detail-video-master.mp4",
                                                        "url": "https://sns-video-hw.xhscdn.com/detail-video-fallback.mp4",
                                                    }
                                                ]
                                            }
                                        }
                                    },
                                    "interact_info": {
                                        "liked_count": "100",
                                        "collected_count": "20",
                                        "comment_count": "3",
                                        "share_count": "4",
                                    },
                                    "tag_list": [{"name": "topic-a"}, {"name": "topic-b"}],
                                }
                            }
                        ]
                    },
                },
            )

    db_dependency, access_token, account_id = _create_pc_account_with_cookie(tmp_path, "detail-owner")
    FakeXhsPcDetailAdapter.calls = []
    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: FakeXhsPcDetailAdapter
    try:
        response = client.post(
            "/api/xhs/pc/notes/detail",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "account_id": account_id,
                "url": "https://www.xiaohongshu.com/explore/detail-note-001?xsec_token=detail-token&xsec_source=pc_feed",
            },
        )

        assert response.status_code == 200
        detail = response.json()
        assert detail["note_id"] == "detail-note-001"
        assert detail["note_url"] == "https://www.xiaohongshu.com/explore/detail-note-001?xsec_token=detail-token&xsec_source=pc_feed"
        assert detail["title"] == "Detail title"
        assert detail["content"] == "Detail body"
        assert detail["author_id"] == "author-detail"
        assert detail["author_name"] == "Detail author"
        assert detail["cover_url"] == "https://example.test/image-high.webp"
        assert detail["image_urls"] == ["https://example.test/image-high.webp"]
        assert detail["video_url"] == "https://sns-video-hw.xhscdn.com/detail-video-master.mp4"
        assert detail["video_addr"] == "https://sns-video-hw.xhscdn.com/detail-video-master.mp4"
        assert detail["tags"] == ["topic-a", "topic-b"]
        assert detail["likes"] == 100
        assert FakeXhsPcDetailAdapter.calls == [
            {
                "cookies": "a1=json-a1; web_session=json-session; id_token=json-id-token",
                "url": "https://www.xiaohongshu.com/explore/detail-note-001?xsec_token=detail-token&xsec_source=pc_feed",
            }
        ]
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)


def test_xhs_pc_note_detail_rejects_missing_auth_and_cross_user_account(tmp_path):
    from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory

    class FakeXhsPcDetailAdapter:
        def __init__(self, cookies):
            raise AssertionError("cross-user detail must not instantiate adapter")

    db_dependency, _, account_id = _create_pc_account_with_cookie(tmp_path, "detail-cross-owner")
    intruder_token = _register_and_get_access_token("detail-cross-intruder")
    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: FakeXhsPcDetailAdapter
    try:
        anonymous_response = client.post(
            "/api/xhs/pc/notes/detail",
            json={"account_id": account_id, "url": "https://www.xiaohongshu.com/explore/detail-note-001"},
        )
        assert anonymous_response.status_code == 401

        intruder_response = client.post(
            "/api/xhs/pc/notes/detail",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"account_id": account_id, "url": "https://www.xiaohongshu.com/explore/detail-note-001"},
        )
        assert intruder_response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)


def test_xhs_pc_note_comments_uses_owned_account_cookie_and_normalizes_result(tmp_path):
    from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory

    class FakeXhsPcCommentAdapter:
        calls = []

        def __init__(self, cookies):
            self.cookies = cookies

        def get_note_comments(self, note_url):
            self.calls.append({"cookies": self.cookies, "note_url": note_url})
            return (
                True,
                "ok",
                {
                    "data": {
                        "comments": [
                            {
                                "id": "comment-001",
                                "content": "Top level comment",
                                "like_count": "12",
                                "create_time": "2026-04-29 12:00:00",
                                "user_info": {"user_id": "user-001", "nickname": "Comment author"},
                                "sub_comments": [
                                    {
                                        "id": "comment-001-1",
                                        "content": "Reply content",
                                        "like_count": 3,
                                        "create_time": "2026-04-29 12:01:00",
                                        "user_info": {"user_id": "user-002", "nickname": "Reply author"},
                                    }
                                ],
                            }
                        ]
                    }
                },
            )

    db_dependency, access_token, account_id = _create_pc_account_with_cookie(tmp_path, "comments-owner")
    FakeXhsPcCommentAdapter.calls = []
    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: FakeXhsPcCommentAdapter
    try:
        response = client.post(
            "/api/xhs/pc/notes/comments",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"account_id": account_id, "note_url": "https://www.xiaohongshu.com/explore/comment-note-001"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 2
        assert payload["items"] == [
            {
                "comment_id": "comment-001",
                "user_name": "Comment author",
                "user_id": "user-001",
                "content": "Top level comment",
                "like_count": 12,
                "parent_comment_id": None,
                "created_at_remote": "2026-04-29 12:00:00",
                "raw_json": {
                    "id": "comment-001",
                    "content": "Top level comment",
                    "like_count": "12",
                    "create_time": "2026-04-29 12:00:00",
                    "user_info": {"user_id": "user-001", "nickname": "Comment author"},
                    "sub_comments": [
                        {
                            "id": "comment-001-1",
                            "content": "Reply content",
                            "like_count": 3,
                            "create_time": "2026-04-29 12:01:00",
                            "user_info": {"user_id": "user-002", "nickname": "Reply author"},
                        }
                    ],
                },
            },
            {
                "comment_id": "comment-001-1",
                "user_name": "Reply author",
                "user_id": "user-002",
                "content": "Reply content",
                "like_count": 3,
                "parent_comment_id": "comment-001",
                "created_at_remote": "2026-04-29 12:01:00",
                "raw_json": {
                    "id": "comment-001-1",
                    "content": "Reply content",
                    "like_count": 3,
                    "create_time": "2026-04-29 12:01:00",
                    "user_info": {"user_id": "user-002", "nickname": "Reply author"},
                },
            },
        ]
        assert FakeXhsPcCommentAdapter.calls == [
            {
                "cookies": "a1=json-a1; web_session=json-session; id_token=json-id-token",
                "note_url": "https://www.xiaohongshu.com/explore/comment-note-001",
            }
        ]
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)


def test_xhs_pc_note_comments_rejects_missing_auth_and_cross_user_account(tmp_path):
    from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory

    class FakeXhsPcCommentAdapter:
        def __init__(self, cookies):
            raise AssertionError("cross-user comments must not instantiate adapter")

    db_dependency, _, account_id = _create_pc_account_with_cookie(tmp_path, "comments-cross-owner")
    intruder_token = _register_and_get_access_token("comments-cross-intruder")
    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: FakeXhsPcCommentAdapter
    try:
        anonymous_response = client.post(
            "/api/xhs/pc/notes/comments",
            json={"account_id": account_id, "note_url": "https://www.xiaohongshu.com/explore/comment-note-001"},
        )
        assert anonymous_response.status_code == 401

        intruder_response = client.post(
            "/api/xhs/pc/notes/comments",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"account_id": account_id, "note_url": "https://www.xiaohongshu.com/explore/comment-note-001"},
        )
        assert intruder_response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)


def test_xhs_pc_note_detail_never_uses_a_foreign_accounts_session(tmp_path):
    """账号自己没有 `web_session` 时，详情直接 400 —— **不碰任何别的账号的会话**。

    判据是「凭据只认该账号自己的平台令牌」，所以库里存在一份**有效**的会话
    （哪怕是同一平台的另一个账号）也不改变结论。

    本用例钉三件事：
    ① 库里有**有效**的同平台会话也不用 —— 400 而非 200；
    ② **一个平台请求都不发**（适配器一次都没构造）；
    ③ 400 的 detail 是结构化原因（`WEB_SESSION_REQUIRED` + 可读文案），
       前端据此安静降级（DEF-058 失败三分法）。
    """
    from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory
    from backend.app.core.database import get_db
    from backend.app.core.security import encrypt_text
    from backend.app.models import AccountCookieVersion, PlatformAccount

    constructed: list[str] = []

    class SpyDetailAdapter:
        def __init__(self, cookies):
            # 构造适配器 = 已经拿着某个会话要去打平台了。这不该发生。
            constructed.append(cookies)

        def get_note_info(self, url):
            raise AssertionError("没有自有凭据 ⇒ 一个平台请求都不该发")

    db_dependency = _override_database(tmp_path)
    access_token = _register_and_get_access_token("own-session-owner")
    db = next(app.dependency_overrides[get_db]())
    try:
        # creator 账号：cookie 里没有 web_session
        creator_account = PlatformAccount(
            user_id=1, platform="xhs", sub_type="creator", nickname="观隅好物",
            profile_json=json.dumps({"red_id": "293066917"}), status="active"
        )
        db.add(creator_account)
        db.flush()
        db.add(
            cookie_version(creator_account.id, '{"a1":"creator-a1","galaxy_creator_session_id":"x"}', CHANNEL_CREATOR)
        )
        # 同平台、**不同用户、不同昵称**的账号：会话**有效**。
        # 它与目标账号无关 —— 存在与否都不影响结论。
        # ⚠️ **该租户必须先建出来**（2026-10-04）：全局 FK=ON 后
        # `platform_accounts.user_id -> users.id` 生效，只给 id 不建行 ⇒
        # `FOREIGN KEY constraint failed`（此前 FK=OFF 时能过）。
        if db.get(User, 999) is None:
            db.add(User(id=999, username="foreign-tenant-999", password_hash="x"))
            db.flush()
        foreign_account = PlatformAccount(
            user_id=999, platform="xhs", sub_type="pc", nickname="晨晨家", status="active"
        )
        db.add(foreign_account)
        db.flush()
        db.add(
            cookie_version(foreign_account.id, '{"a1":"foreign-a1","web_session":"foreign-session",' '"id_token":"foreign-id-token"}', CHANNEL_WWW)
        )
        db.commit()
        creator_id = creator_account.id
    finally:
        db.close()

    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: SpyDetailAdapter
    try:
        response = client.post(
            "/api/xhs/pc/notes/detail",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "account_id": creator_id,
                "url": "https://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f708192a3b?xsec_token=detail-token&xsec_source=pc_creatormng",
            },
        )

        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "WEB_SESSION_REQUIRED"
        assert "网页会话" in detail["message"], "原因要讲清缺的是网页会话（v1.40 文案）"
        # 一个平台请求都不发：没有自有凭据就不去问平台「这份能不能用」。
        assert constructed == []
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)


def test_xhs_pc_note_detail_refuses_without_an_own_web_session(tmp_path):
    """账号自己的 www 令牌里**没有** `web_session`（只有 `a1`）→ 400，不编一个会话出来。"""
    from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory
    from backend.app.core.database import get_db
    from backend.app.core.security import encrypt_text
    from backend.app.models import AccountCookieVersion, PlatformAccount

    class FakeDetailAdapter:
        def __init__(self, cookies):
            raise AssertionError("没有可用凭据 —— 不该构造适配器")

    db_dependency = _override_database(tmp_path)
    access_token = _register_and_get_access_token("no-session-owner")
    db = next(app.dependency_overrides[get_db]())
    try:
        creator_account = PlatformAccount(
            user_id=1, platform="xhs", sub_type="creator", nickname="观隅好物",
            profile_json=json.dumps({"red_id": "293066917"}), status="active"
        )
        db.add(creator_account)
        db.flush()
        db.add(
            cookie_version(creator_account.id, '{"a1":"creator-a1"}', CHANNEL_WWW)
        )
        db.commit()
        creator_id = creator_account.id
    finally:
        db.close()

    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: FakeDetailAdapter
    try:
        response = client.post(
            "/api/xhs/pc/notes/detail",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"account_id": creator_id, "url": "https://www.xiaohongshu.com/explore/note-001"},
        )
        assert response.status_code == 400
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)


def test_xhs_pc_note_detail_refuses_even_when_a_usable_sibling_session_exists(tmp_path):
    """**凭据只认本账号**的 API 层守卫：库里有没有一份可用的兄弟会话都一样。

    这里刻意把「诱惑项」做到最强：同一个昵称、会话**当下有效**、``url`` 完全可解析
    （24 位 hex，带 `xsec_token`）—— 每一条都在说「用它能成事」。仍然必须 400：
    **能不能借到**与**要不要借**是两件事，而答案由「这个账号自己有没有令牌」决定。
    适配器一次都不该被构造 —— 构造了就意味着已经拿着某个会话要去打平台了。
    """
    from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory
    from backend.app.core.database import get_db
    from backend.app.core.security import encrypt_text
    from backend.app.models import AccountCookieVersion, PlatformAccount

    class SpyAdapter:
        def __init__(self, cookies):
            raise AssertionError("没有自有凭据 ⇒ 一个平台请求都不该发")

    db_dependency = _override_database(tmp_path)
    access_token = _register_and_get_access_token("sibling-session-owner")
    db = next(app.dependency_overrides[get_db]())
    try:
        creator_account = PlatformAccount(
            user_id=1, platform="xhs", sub_type="creator", nickname="观隅好物",
            profile_json=json.dumps({"red_id": "293066917"}), status="active"
        )
        db.add(creator_account)
        db.flush()
        db.add(
            cookie_version(creator_account.id, '{"a1":"creator-a1","galaxy_creator_session_id":"x"}', CHANNEL_CREATOR)
        )
        # 诱惑项：同昵称 + 同小红书号 + 有效会话。
        # ⚠️ 它归属的租户要先建出来（2026-10-04）：全局 FK=ON 后
        # `platform_accounts.user_id -> users.id` 生效，缺行即撞外键。
        from backend.app.models import User as _U4

        if db.get(_U4, 2) is None:
            db.add(_U4(id=2, username="sibling-tenant-2", password_hash="x"))
            db.flush()
        sibling = PlatformAccount(
            user_id=2, platform="xhs", sub_type="pc", nickname="观隅好物",
            profile_json=json.dumps({"red_id": "293066917"}), status="active"
        )
        db.add(sibling)
        db.flush()
        db.add(
            cookie_version(sibling.id, '{"a1":"pc-a1","web_session":"good-session","id_token":"good-id-token"}', CHANNEL_WWW)
        )
        db.commit()
        creator_id = creator_account.id
    finally:
        db.close()

    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: SpyAdapter
    try:
        response = client.post(
            "/api/xhs/pc/notes/detail",
            headers={"Authorization": f"Bearer {access_token}"},
            # 真形状（24 位 hex + xsec_token）—— 结论与 url 长什么样无关。
            json={"account_id": creator_id, "url": "https://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f708192a3b?xsec_token=TK"},
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "WEB_SESSION_REQUIRED"
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)


def test_notes_batch_save_requires_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        response = client.post(
            "/api/notes/batch-save",
            json={"account_id": 1, "notes": [{"note_id": "note-001", "title": "标题"}]},
        )

        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_notes_batch_save_persists_owned_search_results_and_updates_duplicates(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.models import Note

    db_dependency, access_token, account_id = _create_pc_account_with_cookie(tmp_path, "save-owner")
    try:
        first_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "account_id": account_id,
                "notes": [
                    {
                        "note_id": "note-save-001",
                        "title": "第一版标题",
                        "content": "第一版正文",
                        "author_name": "作者 A",
                        "raw": {"source": "search", "version": 1},
                    }
                ],
            },
        )
        assert first_response.status_code == 200
        first_payload = first_response.json()
        assert first_payload["saved_count"] == 1
        assert first_payload["items"][0]["note_id"] == "note-save-001"
        assert first_payload["items"][0]["title"] == "第一版标题"

        second_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "account_id": account_id,
                "notes": [
                    {
                        "note_id": "note-save-001",
                        "title": "第二版标题",
                        "content": "第二版正文",
                        "author_name": "作者 B",
                        "raw": {"source": "search", "version": 2},
                    }
                ],
            },
        )
        assert second_response.status_code == 200
        assert second_response.json()["saved_count"] == 1

        db = next(app.dependency_overrides[get_db]())
        try:
            notes = db.query(Note).all()
            assert len(notes) == 1
            assert notes[0].platform == "xhs"
            assert notes[0].platform_account_id == account_id
            assert notes[0].note_id == "note-save-001"
            assert notes[0].title == "第二版标题"
            assert notes[0].content == "第二版正文"
            assert notes[0].author_name == "作者 B"
            assert notes[0].raw_json == {"source": "search", "version": 2}
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_notes_batch_save_persists_detail_assets_and_lists_owned_assets(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.models import NoteAsset

    db_dependency, access_token, account_id = _create_pc_account_with_cookie(tmp_path, "save-assets-owner")
    try:
        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "account_id": account_id,
                "notes": [
                    {
                        "note_id": "note-assets-001",
                        "title": "Asset detail title",
                        "content": "Asset detail body",
                        "author_name": "Asset author",
                        "image_urls": [
                            "https://example.test/detail-1.webp",
                            "https://example.test/detail-2.webp",
                        ],
                        "video_addr": "https://example.test/detail-video.mp4",
                        "cover_url": "https://example.test/detail-cover.webp",
                        "raw": {"source": "detail"},
                    }
                ],
            },
        )
        assert save_response.status_code == 200
        saved_payload = save_response.json()["items"][0]
        note_id = saved_payload["id"]
        assert saved_payload["cover_url"] == "https://example.test/detail-1.webp"
        assert saved_payload["asset_urls"] == [
            "https://example.test/detail-1.webp",
            "https://example.test/detail-2.webp",
            "https://example.test/detail-video.mp4",
        ]

        list_response = client.get(
            f"/api/notes/{note_id}/assets",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert list_response.status_code == 200
        payload = list_response.json()
        assert payload["total"] == 3
        assert [(item["asset_type"], item["url"]) for item in payload["items"]] == [
            ("image", "https://example.test/detail-1.webp"),
            ("image", "https://example.test/detail-2.webp"),
            ("video", "https://example.test/detail-video.mp4"),
        ]

        note_detail_response = client.get(
            f"/api/notes/{note_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert note_detail_response.status_code == 200
        assert note_detail_response.json()["asset_urls"] == [
            "https://example.test/detail-1.webp",
            "https://example.test/detail-2.webp",
            "https://example.test/detail-video.mp4",
        ]

        update_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "account_id": account_id,
                "notes": [
                    {
                        "note_id": "note-assets-001",
                        "title": "Asset detail title updated",
                        "image_urls": ["https://example.test/detail-2.webp"],
                    }
                ],
            },
        )
        assert update_response.status_code == 200

        db = next(app.dependency_overrides[get_db]())
        try:
            assets = db.query(NoteAsset).filter(NoteAsset.note_id == note_id).all()
            assert [(asset.asset_type, asset.url) for asset in assets] == [("image", "https://example.test/detail-2.webp")]
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_notes_assets_reject_cross_user_note(tmp_path):
    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "assets-owner")
    intruder_token = _register_and_get_access_token("assets-intruder")
    try:
        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [
                    {
                        "note_id": "note-assets-cross-001",
                        "title": "Owner note",
                        "image_urls": ["https://example.test/owner.webp"],
                    }
                ],
            },
        )
        note_id = save_response.json()["items"][0]["id"]

        response = client.get(
            f"/api/notes/{note_id}/assets",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )

        assert response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_notes_batch_save_fetches_and_persists_comments(tmp_path):
    from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory
    from backend.app.core.database import get_db
    from backend.app.models import NoteComment

    class FakeXhsPcCommentPersistAdapter:
        calls = []

        def __init__(self, cookies):
            self.cookies = cookies

        def get_note_comments(self, note_url):
            self.calls.append({"cookies": self.cookies, "note_url": note_url})
            return (
                True,
                "ok",
                {
                    "data": {
                        "comments": [
                            {
                                "id": "persist-comment-001",
                                "content": "Persisted top comment",
                                "like_count": "8",
                                "create_time": "2026-04-29 13:00:00",
                                "user_info": {"user_id": "persist-user-001", "nickname": "Persist author"},
                            }
                        ]
                    }
                },
            )

    db_dependency, access_token, account_id = _create_pc_account_with_cookie(tmp_path, "comment-persist-owner")
    FakeXhsPcCommentPersistAdapter.calls = []
    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: FakeXhsPcCommentPersistAdapter
    try:
        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "account_id": account_id,
                "fetch_comments": True,
                "notes": [
                    {
                        "note_id": "note-comments-001",
                        "note_url": "https://www.xiaohongshu.com/explore/note-comments-001",
                        "title": "Comment note",
                    }
                ],
            },
        )
        assert save_response.status_code == 200
        note_id = save_response.json()["items"][0]["id"]
        assert FakeXhsPcCommentPersistAdapter.calls == [
            {
                "cookies": "a1=json-a1; web_session=json-session; id_token=json-id-token",
                "note_url": "https://www.xiaohongshu.com/explore/note-comments-001",
            }
        ]

        list_response = client.get(
            f"/api/notes/{note_id}/comments",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert list_response.status_code == 200
        payload = list_response.json()
        assert payload["total"] == 1
        assert payload["items"][0]["comment_id"] == "persist-comment-001"
        assert payload["items"][0]["user_name"] == "Persist author"
        assert payload["items"][0]["user_id"] == "persist-user-001"
        assert payload["items"][0]["content"] == "Persisted top comment"
        assert payload["items"][0]["like_count"] == 8
        assert payload["items"][0]["parent_comment_id"] is None
        assert payload["items"][0]["created_at_remote"] == "2026-04-29 13:00:00"

        db = next(app.dependency_overrides[get_db]())
        try:
            comments = db.query(NoteComment).filter(NoteComment.note_id == note_id).all()
            assert len(comments) == 1
            assert comments[0].raw_json["id"] == "persist-comment-001"
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)


def test_notes_batch_save_replaces_stale_comments_on_refetch(tmp_path):
    from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory

    class FakeXhsPcCommentReplaceAdapter:
        calls = []

        def __init__(self, cookies):
            self.cookies = cookies

        def get_note_comments(self, note_url):
            self.calls.append(note_url)
            comment_id = "stale-comment" if len(self.calls) == 1 else "fresh-comment"
            return (
                True,
                "ok",
                {"data": {"comments": [{"id": comment_id, "content": comment_id, "user_info": {"nickname": "User"}}]}},
            )

    db_dependency, access_token, account_id = _create_pc_account_with_cookie(tmp_path, "comment-replace-owner")
    FakeXhsPcCommentReplaceAdapter.calls = []
    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: FakeXhsPcCommentReplaceAdapter
    try:
        for _ in range(2):
            save_response = client.post(
                "/api/notes/batch-save",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "account_id": account_id,
                    "fetch_comments": True,
                    "notes": [
                        {
                            "note_id": "note-comments-replace-001",
                            "note_url": "https://www.xiaohongshu.com/explore/note-comments-replace-001",
                            "title": "Replace comments",
                        }
                    ],
                },
            )
            assert save_response.status_code == 200
        note_id = save_response.json()["items"][0]["id"]

        list_response = client.get(
            f"/api/notes/{note_id}/comments",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert list_response.status_code == 200
        assert list_response.json()["total"] == 1
        assert list_response.json()["items"][0]["comment_id"] == "fresh-comment"
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)


def test_notes_comments_list_requires_auth_and_enforces_ownership(tmp_path):
    from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory

    class FakeXhsPcCommentListAdapter:
        def __init__(self, cookies):
            self.cookies = cookies

        def get_note_comments(self, note_url):
            return (
                True,
                "ok",
                {"data": {"comments": [{"id": "owned-comment", "content": "Owned", "user_info": {"nickname": "User"}}]}},
            )

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "comments-list-owner")
    intruder_token = _register_and_get_access_token("comments-list-intruder")
    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: FakeXhsPcCommentListAdapter
    try:
        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "fetch_comments": True,
                "notes": [
                    {
                        "note_id": "note-comments-owned-001",
                        "note_url": "https://www.xiaohongshu.com/explore/note-comments-owned-001",
                        "title": "Owned comment note",
                    }
                ],
            },
        )
        note_id = save_response.json()["items"][0]["id"]

        anonymous_response = client.get(f"/api/notes/{note_id}/comments")
        assert anonymous_response.status_code == 401

        intruder_response = client.get(
            f"/api/notes/{note_id}/comments",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)


def test_topics_crud_are_user_scoped_and_validate_duplicates(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        owner_token = _register_and_get_access_token("tags-crud-owner")
        intruder_token = _register_and_get_access_token("tags-crud-intruder")

        create_response = client.post(
            "/api/topics",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "High value", "color": "#111111"},
        )
        assert create_response.status_code == 200
        created = create_response.json()
        assert created["name"] == "High value"
        assert created["color"] == "#111111"

        duplicate_response = client.post(
            "/api/topics",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "High value", "color": "#ef4444"},
        )
        assert duplicate_response.status_code == 400

        owner_list_response = client.get(
            "/api/topics",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert owner_list_response.status_code == 200
        assert owner_list_response.json()["total"] == 1

        intruder_list_response = client.get(
            "/api/topics",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_list_response.status_code == 200
        assert intruder_list_response.json()["total"] == 0

        intruder_update_response = client.patch(
            f"/api/topics/{created['id']}",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"name": "Stolen"},
        )
        assert intruder_update_response.status_code == 404

        update_response = client.patch(
            f"/api/topics/{created['id']}",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "Rewrite queue", "color": "#2563eb"},
        )
        assert update_response.status_code == 200
        assert update_response.json()["name"] == "Rewrite queue"
        assert update_response.json()["color"] == "#2563eb"

        intruder_delete_response = client.delete(
            f"/api/topics/{created['id']}",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_delete_response.status_code == 404

        delete_response = client.delete(
            f"/api/topics/{created['id']}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert delete_response.status_code == 200
        assert delete_response.json() == {"id": created["id"], "status": "deleted"}

        empty_list_response = client.get(
            "/api/topics",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert empty_list_response.json()["total"] == 0
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_notes_batch_topic_applies_and_removes_owned_topics(tmp_path):
    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "batch-tag-owner")
    try:
        first_topic = client.post(
            "/api/topics",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "Rewrite", "color": "#111111"},
        ).json()
        second_topic = client.post(
            "/api/topics",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "Benchmark", "color": "#2563eb"},
        ).json()
        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [
                    {
                        "note_id": "note-tags-001",
                        "title": "Tagged note",
                        "content": "Tag me",
                    }
                ],
            },
        )
        note_id = save_response.json()["items"][0]["id"]

        replace_response = client.post(
            "/api/notes/batch-tag",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"note_ids": [note_id], "tag_ids": [first_topic["id"]], "mode": "replace"},
        )
        assert replace_response.status_code == 200
        assert replace_response.json()["updated_count"] == 1
        assert replace_response.json()["items"][0]["topics"] == [first_topic]

        add_response = client.post(
            "/api/notes/batch-tag",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"note_ids": [note_id], "tag_ids": [second_topic["id"]], "mode": "add"},
        )
        assert add_response.status_code == 200
        assert [t["name"] for t in add_response.json()["items"][0]["topics"]] == ["Rewrite", "Benchmark"]

        remove_response = client.post(
            "/api/notes/batch-tag",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"note_ids": [note_id], "tag_ids": [first_topic["id"]], "mode": "remove"},
        )
        assert remove_response.status_code == 200
        assert remove_response.json()["items"][0]["topics"] == [second_topic]

        detail_response = client.get(
            f"/api/notes/{note_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert detail_response.status_code == 200
        assert [t["name"] for t in detail_response.json()["topics"]] == ["Benchmark"]

        list_response = client.get(
            "/api/notes?platform=xhs",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert list_response.status_code == 200
        assert [t["name"] for t in list_response.json()["items"][0]["topics"]] == ["Benchmark"]
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_notes_batch_topic_rejects_cross_user_note_or_topic(tmp_path):
    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "batch-tag-cross-owner")
    intruder_token = _register_and_get_access_token("batch-tag-cross-intruder")
    try:
        owner_tag = client.post(
            "/api/topics",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "Owner tag", "color": "#111111"},
        ).json()
        intruder_tag = client.post(
            "/api/topics",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"name": "Intruder tag", "color": "#ef4444"},
        ).json()
        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [{"note_id": "note-tags-cross-001", "title": "Owner note"}],
            },
        )
        note_id = save_response.json()["items"][0]["id"]

        intruder_note_response = client.post(
            "/api/notes/batch-tag",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"note_ids": [note_id], "tag_ids": [intruder_tag["id"]], "mode": "replace"},
        )
        assert intruder_note_response.status_code == 404

        owner_cross_tag_response = client.post(
            "/api/notes/batch-tag",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"note_ids": [note_id], "tag_ids": [intruder_tag["id"]], "mode": "replace"},
        )
        assert owner_cross_tag_response.status_code == 404

        owner_valid_response = client.post(
            "/api/notes/batch-tag",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"note_ids": [note_id], "tag_ids": [owner_tag["id"]], "mode": "replace"},
        )
        assert owner_valid_response.status_code == 200
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_notes_batch_create_drafts_creates_owned_drafts_and_rejects_cross_user_notes(tmp_path):
    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "batch-drafts-owner")
    intruder_token = _register_and_get_access_token("batch-drafts-intruder")
    try:
        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [
                    {"note_id": "batch-draft-001", "title": "First source", "content": "First body"},
                    {"note_id": "batch-draft-002", "title": "Second source", "content": "Second body"},
                ],
            },
        )
        assert save_response.status_code == 200
        note_ids = [item["id"] for item in save_response.json()["items"]]

        response = client.post(
            "/api/notes/batch-create-drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"note_ids": note_ids, "intent": "rewrite"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["created_count"] == 2
        assert [item["title"] for item in payload["items"]] == ["First source", "Second source"]
        assert [item["body"] for item in payload["items"]] == ["First body", "Second body"]
        assert [item["source_note_id"] for item in payload["items"]] == note_ids

        intruder_response = client.post(
            "/api/notes/batch-create-drafts",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"note_ids": [note_ids[0]], "intent": "rewrite"},
        )
        assert intruder_response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_notes_export_writes_json_for_owned_notes_and_rejects_cross_user_notes(tmp_path):
    import json
    from pathlib import Path

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "notes-export-owner")
    intruder_token = _register_and_get_access_token("notes-export-intruder")
    try:
        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [
                    {
                        "note_id": "export-note-001",
                        "title": "Exported note",
                        "content": "Export body",
                        "author_name": "Export author",
                        "raw": {"source": "unit-test"},
                    }
                ],
            },
        )
        assert save_response.status_code == 200
        note_id = save_response.json()["items"][0]["id"]
        tag_response = client.post(
            "/api/topics",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "Export", "color": "#111111"},
        )
        client.post(
            "/api/notes/batch-tag",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"note_ids": [note_id], "tag_ids": [tag_response.json()["id"]], "mode": "replace"},
        )

        response = client.post(
            "/api/notes/export",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"note_ids": [note_id], "format": "json"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["exported_count"] == 1
        assert payload["file_name"].endswith(".json")
        assert f"u{1}-" in payload["file_name"]
        export_path = Path(payload["file_path"])
        assert export_path.exists()
        exported = json.loads(export_path.read_text(encoding="utf-8"))
        assert exported["items"][0]["note_id"] == "export-note-001"
        assert exported["items"][0]["topics"][0]["name"] == "Export"

        download_response = client.get(
            payload["download_url"],
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert download_response.status_code == 200
        assert download_response.json()["items"][0]["note_id"] == "export-note-001"

        intruder_download_response = client.get(
            payload["download_url"],
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_download_response.status_code == 404

        intruder_response = client.post(
            "/api/notes/export",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"note_ids": [note_id], "format": "json"},
        )
        assert intruder_response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_notes_export_writes_csv_for_owned_notes(tmp_path):
    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "notes-export-csv-owner")
    try:
        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [
                    {
                        "note_id": "export-csv-001",
                        "title": "CSV 标题",
                        "content": "CSV 正文",
                        "author_name": "CSV 作者",
                    }
                ],
            },
        )
        note_id = save_response.json()["items"][0]["id"]

        response = client.post(
            "/api/notes/export",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"note_ids": [note_id], "format": "csv"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["file_name"].endswith(".csv")
        download_response = client.get(
            payload["download_url"],
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert download_response.status_code == 200
        csv_text = download_response.content.decode("utf-8-sig")
        assert "note_id,title,author_name,content,topics,published,created_at" in csv_text
        assert "export-csv-001" in csv_text
        assert "CSV 标题" in csv_text
        # 采集入库（batch-save）默认「待发布」⇒ CSV 里该列必须是 false（设计 13 §19.12）。
        assert "export-csv-001,CSV 标题,CSV 作者,CSV 正文,,false," in csv_text
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_xhs_analytics_uses_current_user_saved_notes_tags_and_comments(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.models import NoteComment

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "analytics-owner")
    intruder_token = _register_and_get_access_token("analytics-intruder")
    try:
        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [
                    {
                        "note_id": "analytics-note-001",
                        "title": "高互动早餐笔记",
                        "content": "早餐选题正文",
                        "author_name": "早餐作者",
                        "raw": {"likes": 100, "collects": 30, "comments": 12, "shares": 8, "tags": ["早餐", "低卡"]},
                    },
                    {
                        "note_id": "analytics-note-002",
                        "title": "普通收纳笔记",
                        "content": "收纳正文",
                        "author_name": "收纳作者",
                        "raw": {"likes": 10, "collects": 2, "comments": 1, "shares": 0, "tags": ["收纳"]},
                    },
                ],
            },
        )
        assert save_response.status_code == 200
        first_note_id = save_response.json()["items"][0]["id"]
        tag_response = client.post(
            "/api/topics",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "早餐", "color": "#111111"},
        )
        client.post(
            "/api/notes/batch-tag",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"note_ids": [first_note_id], "tag_ids": [tag_response.json()["id"]], "mode": "add"},
        )
        db = next(app.dependency_overrides[get_db]())
        try:
            db.add(
                NoteComment(
                    note_id=first_note_id,
                    comment_id="analytics-comment-001",
                    user_name="用户 A",
                    content="这个早餐适合通勤吗？价格会不会高？",
                    like_count=9,
                )
            )
            db.commit()
        finally:
            db.close()

        overview_response = client.get(
            "/api/xhs/analytics/overview",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert overview_response.status_code == 200
        overview = overview_response.json()
        assert overview["platform"] == "xhs"
        assert overview["saved_notes"] == 2
        assert overview["total_engagement"] == 163
        assert overview["comment_count"] == 1
        assert overview["hot_topics"][0]["keyword"] == "早餐"

        top_response = client.get(
            "/api/xhs/analytics/top-content",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert top_response.status_code == 200
        top_items = top_response.json()["items"]
        assert [item["note_id"] for item in top_items] == ["analytics-note-001", "analytics-note-002"]
        assert top_items[0]["engagement"] == 150

        topics_response = client.get(
            "/api/xhs/analytics/hot-topics",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert topics_response.status_code == 200
        topics = topics_response.json()["items"]
        assert topics[0]["keyword"] == "早餐"
        assert topics[0]["notes"] == 1
        assert topics[0]["engagement"] == 150

        comments_response = client.get(
            "/api/xhs/analytics/comment-insights",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert comments_response.status_code == 200
        comments = comments_response.json()
        assert comments["total_comments"] == 1
        assert comments["question_count"] == 1
        assert comments["top_comments"][0]["content"] == "这个早餐适合通勤吗？价格会不会高？"

        intruder_response = client.get(
            "/api/xhs/analytics/overview",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_response.status_code == 200
        assert intruder_response.json()["saved_notes"] == 0
        assert intruder_response.json()["total_engagement"] == 0
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_xhs_analytics_benchmarks_are_user_scoped_from_monitoring_targets(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.models import MonitoringTarget

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "benchmark-owner")
    intruder_token = _register_and_get_access_token("benchmark-intruder")
    try:
        anonymous_response = client.get("/api/xhs/analytics/benchmarks")
        assert anonymous_response.status_code == 401

        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [
                    {
                        "note_id": "benchmark-note-account",
                        "title": "竞品账号低卡早餐",
                        "content": "creator-001 的早餐选题",
                        "author_name": "creator-001",
                        "raw": {"likes": 80, "collects": 12, "comments": 6, "shares": 2},
                    },
                    {
                        "note_id": "benchmark-note-brand",
                        "title": "BrandA 收纳爆文",
                        "content": "BrandA 新品收纳角度",
                        "author_name": "brand-author",
                        "raw": {"likes": 30, "collects": 10, "comments": 4, "shares": 1},
                    },
                    {
                        "note_id": "benchmark-note-keyword-only",
                        "title": "低卡早餐普通趋势",
                        "content": "低卡早餐关键词命中但不是竞品目标",
                        "author_name": "trend-author",
                        "raw": {"likes": 300},
                    },
                ],
            },
        )
        assert save_response.status_code == 200
        db = next(app.dependency_overrides[get_db]())
        try:
            db.add_all(
                [
                    MonitoringTarget(
                        user_id=1,
                        platform="xhs",
                        target_type="account",
                        name="竞品账号",
                        value="creator-001",
                        status="active",
                    ),
                    MonitoringTarget(
                        user_id=1,
                        platform="xhs",
                        target_type="brand",
                        name="BrandA",
                        value="BrandA",
                        status="active",
                    ),
                    MonitoringTarget(
                        user_id=1,
                        platform="xhs",
                        target_type="keyword",
                        name="低卡早餐",
                        value="低卡早餐",
                        status="active",
                    ),
                    MonitoringTarget(
                        user_id=2,
                        platform="xhs",
                        target_type="account",
                        name="Other account",
                        value="creator-001",
                        status="active",
                    ),
                ]
            )
            db.commit()
        finally:
            db.close()

        response = client.get(
            "/api/xhs/analytics/benchmarks",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["total_targets"] == 2
        assert payload["matched_notes"] == 2
        assert payload["total_engagement"] == 145
        assert [item["target_type"] for item in payload["items"]] == ["account", "brand"]
        assert payload["items"][0]["name"] == "竞品账号"
        assert payload["items"][0]["matched_notes"] == 1
        assert payload["items"][0]["total_engagement"] == 100
        assert payload["items"][0]["top_notes"][0]["note_id"] == "benchmark-note-account"
        assert payload["items"][1]["name"] == "BrandA"
        assert payload["items"][1]["total_engagement"] == 45

        intruder_response = client.get(
            "/api/xhs/analytics/benchmarks",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_response.status_code == 200
        assert intruder_response.json()["total_targets"] == 1
        assert intruder_response.json()["matched_notes"] == 0
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_xhs_benchmark_create_drafts_uses_owned_target_matches(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.models import AiDraft, MonitoringTarget

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "benchmark-draft-owner")
    intruder_token = _register_and_get_access_token("benchmark-draft-intruder")
    try:
        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [
                    {
                        "note_id": "benchmark-draft-note-001",
                        "title": "creator-002 爆款标题",
                        "content": "creator-002 的爆款正文",
                        "author_name": "creator-002",
                        "raw": {"likes": 90},
                    },
                    {
                        "note_id": "benchmark-draft-note-002",
                        "title": "creator-002 第二篇",
                        "content": "creator-002 的第二篇正文",
                        "author_name": "creator-002",
                        "raw": {"likes": 40},
                    },
                    {
                        "note_id": "benchmark-draft-note-other",
                        "title": "其他账号内容",
                        "content": "不该命中",
                        "author_name": "other",
                        "raw": {"likes": 500},
                    },
                ],
            },
        )
        assert save_response.status_code == 200
        db = next(app.dependency_overrides[get_db]())
        try:
            target = MonitoringTarget(
                user_id=1,
                platform="xhs",
                target_type="account",
                name="creator-002",
                value="creator-002",
                status="active",
            )
            keyword_target = MonitoringTarget(
                user_id=1,
                platform="xhs",
                target_type="keyword",
                name="普通关键词",
                value="creator-002",
                status="active",
            )
            intruder_target = MonitoringTarget(
                user_id=2,
                platform="xhs",
                target_type="account",
                name="intruder target",
                value="creator-002",
                status="active",
            )
            db.add_all([target, keyword_target, intruder_target])
            db.commit()
            target_id = target.id
            keyword_target_id = keyword_target.id
        finally:
            db.close()

        anonymous_response = client.post(f"/api/xhs/analytics/benchmarks/{target_id}/create-drafts")
        assert anonymous_response.status_code == 401

        intruder_response = client.post(
            f"/api/xhs/analytics/benchmarks/{target_id}/create-drafts",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_response.status_code == 404

        keyword_response = client.post(
            f"/api/xhs/analytics/benchmarks/{keyword_target_id}/create-drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert keyword_response.status_code == 400

        response = client.post(
            f"/api/xhs/analytics/benchmarks/{target_id}/create-drafts?limit=1",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["created_count"] == 1
        assert payload["items"][0]["title"] == "creator-002 爆款标题"
        assert payload["items"][0]["source_note_id"] == save_response.json()["items"][0]["id"]

        db = next(app.dependency_overrides[get_db]())
        try:
            drafts = db.query(AiDraft).all()
            assert len(drafts) == 1
            assert drafts[0].user_id == 1
            assert drafts[0].platform == "xhs"
            assert drafts[0].body == "creator-002 的爆款正文"
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_xhs_analytics_report_generates_owned_json_export_and_rejects_cross_user_notes(tmp_path):
    import json
    from pathlib import Path

    from backend.app.core.database import get_db
    from backend.app.models import PlatformAccount

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "analytics-report-owner")
    intruder_token = _register_and_get_access_token("analytics-report-intruder")
    try:
        anonymous_response = client.post("/api/xhs/analytics/reports")
        assert anonymous_response.status_code == 401

        owner_save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [
                    {
                        "note_id": "analytics-report-note-001",
                        "title": "Report note one",
                        "content": "Breakfast content with BrandA",
                        "author_name": "creator-report",
                        "raw": {"likes": 20, "collects": 5, "comments": 3, "shares": 2, "tags": ["breakfast"]},
                    },
                    {
                        "note_id": "analytics-report-note-002",
                        "title": "Report note two",
                        "content": "Storage content",
                        "author_name": "creator-report",
                        "raw": {"likes": 10, "collects": 1, "comments": 0, "shares": 0, "tags": ["storage"]},
                    },
                ],
            },
        )
        assert owner_save_response.status_code == 200
        owner_note_ids = [item["id"] for item in owner_save_response.json()["items"]]

        db = next(app.dependency_overrides[get_db]())
        try:
            intruder_account = PlatformAccount(
                user_id=2,
                platform="xhs",
                sub_type="pc",
                external_user_id="analytics-report-intruder",
                nickname="Intruder",
                status="active",
            )
            db.add(intruder_account)
            db.commit()
            intruder_account_id = intruder_account.id
        finally:
            db.close()

        intruder_save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={
                "account_id": intruder_account_id,
                "notes": [
                    {
                        "note_id": "analytics-report-intruder-note",
                        "title": "Intruder note",
                        "content": "Not visible to owner",
                        "author_name": "intruder",
                        "raw": {"likes": 999},
                    }
                ],
            },
        )
        assert intruder_save_response.status_code == 200
        intruder_note_id = intruder_save_response.json()["items"][0]["id"]

        forbidden_response = client.post(
            "/api/xhs/analytics/reports",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"note_ids": [owner_note_ids[0], intruder_note_id], "format": "json"},
        )
        assert forbidden_response.status_code == 404

        report_response = client.post(
            "/api/xhs/analytics/reports",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"note_ids": owner_note_ids, "format": "json"},
        )
        assert report_response.status_code == 200
        report = report_response.json()
        assert report["report_type"] == "operations"
        assert report["note_count"] == 2
        assert report["summary"]["total_engagement"] == 41
        assert report["summary"]["top_notes"][0]["note_id"] == "analytics-report-note-001"
        assert report["file_name"].startswith("xhs-report-u1-")
        assert report["download_url"].startswith("/api/files/exports/xhs-report-u1-")

        report_path = Path(report["file_path"])
        assert report_path.is_file()
        report_file = json.loads(report_path.read_text(encoding="utf-8"))
        assert report_file["metadata"]["user_id"] == 1
        assert report_file["summary"]["note_count"] == 2
        assert report_file["top_notes"][0]["title"] == "Report note one"

        download_response = client.get(
            report["download_url"],
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert download_response.status_code == 200
        assert download_response.json()["metadata"]["report_type"] == "operations"

        intruder_download_response = client.get(
            report["download_url"],
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_download_response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_xhs_crawl_routes_are_authenticated_task_backed_and_persist_notes(tmp_path):
    from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory
    from backend.app.core.database import get_db
    from backend.app.models import Note, NoteAsset, Task

    class FakeCrawlAdapter:
        def __init__(self, cookies):
            self.cookies = cookies

        def search_note(self, keyword, page=1, **kwargs):
            return True, "ok", {
                "data": {
                    "items": [
                        {
                            "note_card": {
                                "note_id": "crawl-search-001",
                                "display_title": f"{keyword} search title",
                                "desc": "search content",
                                "user": {"nickname": "search author"},
                                "interact_info": {"liked_count": 12, "collected_count": 3, "comment_count": 2},
                                "cover": {"url": "https://img.example/search.png"},
                            }
                        }
                    ],
                    "total": 1,
                    "has_more": False,
                }
            }

        def get_note_info(self, url):
            return True, "ok", {
                "data": {
                    "items": [
                        {
                            "note_card": {
                                "note_id": "crawl-url-001",
                                "display_title": "url detail title",
                                "desc": "url detail body",
                                "user": {"nickname": "url author"},
                                "interact_info": {"liked_count": 30},
                                "image_list": [{"url": "https://img.example/url-1.png"}],
                            }
                        }
                    ]
                }
            }

        def get_user_notes(self, user_url):
            return True, "ok", {
                "data": {
                    "items": [
                        {
                            "note_card": {
                                "note_id": "crawl-user-001",
                                "display_title": "user note title",
                                "desc": "user note body",
                                "user": {"nickname": "profile author"},
                                "interact_info": {"liked_count": 7},
                                "cover": {"url": "https://img.example/user.png"},
                            }
                        }
                    ],
                    "total": 1,
                }
            }

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "crawl-routes-owner")
    intruder_token = _register_and_get_access_token("crawl-routes-intruder")
    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: FakeCrawlAdapter
    try:
        anonymous_response = client.post(
            "/api/xhs/crawl/search-notes",
            json={"account_id": owner_account_id, "keyword": "breakfast"},
        )
        assert anonymous_response.status_code == 401

        intruder_response = client.post(
            "/api/xhs/crawl/search-notes",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"account_id": owner_account_id, "keyword": "breakfast"},
        )
        assert intruder_response.status_code == 404

        search_response = client.post(
            "/api/xhs/crawl/search-notes",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"account_id": owner_account_id, "keyword": "breakfast", "page": 1},
        )
        assert search_response.status_code == 200
        search_payload = search_response.json()
        assert search_payload["task"]["task_type"] == "crawl"
        assert search_payload["task"]["status"] == "completed"
        assert search_payload["saved_count"] == 1
        assert search_payload["items"][0]["note_id"] == "crawl-search-001"

        url_response = client.post(
            "/api/xhs/crawl/note-urls",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"account_id": owner_account_id, "urls": ["https://www.xiaohongshu.com/explore/crawl-url-001"]},
        )
        assert url_response.status_code == 200
        assert url_response.json()["saved_count"] == 1
        assert url_response.json()["items"][0]["note_id"] == "crawl-url-001"

        user_response = client.post(
            "/api/xhs/crawl/user-notes",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"account_id": owner_account_id, "user_url": "https://www.xiaohongshu.com/user/profile/demo"},
        )
        assert user_response.status_code == 200
        assert user_response.json()["saved_count"] == 1
        assert user_response.json()["items"][0]["note_id"] == "crawl-user-001"

        db = next(app.dependency_overrides[get_db]())
        try:
            tasks = db.query(Task).filter(Task.task_type == "crawl").all()
            notes = db.query(Note).order_by(Note.note_id.asc()).all()
            assets = db.query(NoteAsset).all()
            assert len(tasks) == 3
            assert all(task.user_id == 1 and task.status == "completed" for task in tasks)
            assert [note.note_id for note in notes] == ["crawl-search-001", "crawl-url-001", "crawl-user-001"]
            assert len(assets) == 3
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_xhs_data_crawl_marks_partial_failures_and_fetches_comments(tmp_path):
    from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory

    class FakeDataCrawlAdapter:
        calls = []

        def __init__(self, cookies):
            self.cookies = cookies

        def get_note_info(self, url):
            self.__class__.calls.append(("detail", url))
            if "bad" in url:
                return False, "detail failed", {}
            return True, "ok", {
                "data": {
                    "items": [
                        {
                            "note_card": {
                                "note_id": "data-url-001",
                                "display_title": "data crawl detail",
                                "desc": "detail body",
                                "user": {"nickname": "detail author"},
                                "interact_info": {"liked_count": 8, "comment_count": 1},
                                "image_list": [{"url": "https://img.example/data-url.png"}],
                            }
                        }
                    ]
                }
            }

        def get_note_comments(self, url):
            self.__class__.calls.append(("comments", url))
            return True, "ok", {
                "data": {
                    "comments": [
                        {
                            "id": "comment-001",
                            "content": "想看更多",
                            "user_info": {"nickname": "reader"},
                            "like_count": "3",
                        }
                    ]
                }
            }

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "data-crawl-owner")
    FakeDataCrawlAdapter.calls = []
    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: FakeDataCrawlAdapter
    try:
        response = client.post(
            "/api/xhs/crawl/data",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "mode": "note_urls",
                "urls": [
                    "https://www.xiaohongshu.com/explore/data-url-001",
                    "https://www.xiaohongshu.com/explore/bad-url",
                ],
                "fetch_comments": True,
            },
        )

        assert response.status_code == 200
        payload = _parse_sse_response(response)
        assert payload["total"] == 2
        assert payload["success_count"] == 1
        assert payload["failed_count"] == 1
        assert payload["items"][0]["status"] == "success"
        assert payload["items"][0]["note"]["note_id"] == "data-url-001"
        assert payload["items"][0]["comment_count"] == 1
        assert payload["items"][0]["comments"][0]["content"] == "想看更多"
        assert payload["items"][1]["status"] == "failed"
        assert payload["items"][1]["error"] == "detail failed"
        assert ("comments", "https://www.xiaohongshu.com/explore/data-url-001") in FakeDataCrawlAdapter.calls
    finally:
        app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_xhs_data_crawl_search_expands_filters_and_fetches_details(tmp_path):
    from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory

    class FakeSearchDataCrawlAdapter:
        search_calls = []
        detail_calls = []

        def __init__(self, cookies):
            self.cookies = cookies

        def search_note(self, keyword, page=1, **kwargs):
            self.__class__.search_calls.append({"keyword": keyword, "page": page, **kwargs})
            return True, "ok", {
                "data": {
                    "has_more": False,
                    "items": [
                        {
                            "xsec_token": "xsec-data-search",
                            "note_card": {
                                "note_id": "data-search-001",
                                "display_title": "search source title",
                                "desc": "search source body",
                                "user": {"nickname": "search author"},
                                "interact_info": {"liked_count": 12},
                                "cover": {"url": "https://img.example/search.png"},
                            },
                        }
                    ],
                }
            }

        def get_note_info(self, url):
            self.__class__.detail_calls.append(url)
            return True, "ok", {
                "data": {
                    "items": [
                        {
                            "note_card": {
                                "note_id": "data-search-001",
                                "display_title": "detail title",
                                "desc": "detail body",
                                "user": {"nickname": "detail author"},
                                "interact_info": {"liked_count": 88},
                                "image_list": [{"url": "https://img.example/detail.png"}],
                            }
                        }
                    ]
                }
            }

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "data-crawl-search-owner")
    FakeSearchDataCrawlAdapter.search_calls = []
    FakeSearchDataCrawlAdapter.detail_calls = []
    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: FakeSearchDataCrawlAdapter
    try:
        response = client.post(
            "/api/xhs/crawl/data",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "mode": "search",
                "keyword": "低卡早餐",
                "pages": 1,
                "max_notes": 5,
                "sort_type_choice": 2,
                "note_type": 2,
                "note_time": 1,
                "note_range": 3,
                "pos_distance": 1,
                "geo": "31.2304,121.4737",
            },
        )

        assert response.status_code == 200
        payload = _parse_sse_response(response)
        assert payload["success_count"] == 1
        assert payload["items"][0]["note"]["title"] == "detail title"
        assert FakeSearchDataCrawlAdapter.search_calls == [
            {
                "keyword": "低卡早餐",
                "page": 1,
                "sort_type_choice": 2,
                "note_type": 2,
                "note_time": 1,
                "note_range": 3,
                "pos_distance": 1,
                "geo": "31.2304,121.4737",
            }
        ]
        assert FakeSearchDataCrawlAdapter.detail_calls == [
            "https://www.xiaohongshu.com/explore/data-search-001?xsec_token=xsec-data-search&xsec_source=pc_feed"
        ]
    finally:
        app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_xhs_analytics_keyword_trends_are_user_scoped_from_keyword_groups(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.models import KeywordGroup

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "keyword-trends-owner")
    intruder_token = _register_and_get_access_token("keyword-trends-intruder")
    try:
        anonymous_response = client.get("/api/xhs/analytics/keyword-trends")
        assert anonymous_response.status_code == 401

        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [
                    {
                        "note_id": "keyword-trend-note-001",
                        "title": "Breakfast angle",
                        "content": "Low calorie breakfast for commute",
                        "author_name": "trend author",
                        "raw": {"likes": 20, "collects": 5, "comments": 1, "shares": 0},
                    },
                    {
                        "note_id": "keyword-trend-note-002",
                        "title": "Storage angle",
                        "content": "Closet storage checklist",
                        "author_name": "trend author",
                        "raw": {"likes": 10, "collects": 2},
                    },
                ],
            },
        )
        assert save_response.status_code == 200

        db = next(app.dependency_overrides[get_db]())
        try:
            db.add_all(
                [
                    KeywordGroup(user_id=1, platform="xhs", name="Owner ideas", keywords=["breakfast", "storage"]),
                    KeywordGroup(user_id=2, platform="xhs", name="Intruder ideas", keywords=["breakfast"]),
                ]
            )
            db.commit()
        finally:
            db.close()

        owner_response = client.get(
            "/api/xhs/analytics/keyword-trends",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert owner_response.status_code == 200
        owner_items = owner_response.json()["items"]
        assert [item["keyword"] for item in owner_items] == ["breakfast", "storage"]
        assert owner_items[0]["group_name"] == "Owner ideas"
        assert owner_items[0]["notes"] == 1
        assert owner_items[0]["engagement"] == 26
        assert owner_items[0]["top_notes"][0]["note_id"] == "keyword-trend-note-001"
        assert owner_items[1]["engagement"] == 12

        intruder_response = client.get(
            "/api/xhs/analytics/keyword-trends",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_response.status_code == 200
        assert intruder_response.json()["items"][0]["keyword"] == "breakfast"
        assert intruder_response.json()["items"][0]["notes"] == 0
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_xhs_creator_routes_use_owned_creator_account_and_record_tasks(tmp_path):
    from backend.app.api.platforms.xhs.creator import get_creator_api_adapter_factory
    from backend.app.core.database import get_db
    from backend.app.models import Task

    class FakeCreatorRoutesAdapter:
        def __init__(self, cookies):
            self.cookies = cookies

        def get_topic(self, keyword):
            return True, "ok", {"data": {"items": [{"id": "topic-creator", "name": keyword}]}}

        def get_location_info(self, keyword):
            return True, "ok", {"data": {"items": [{"id": "loc-creator", "name": keyword}]}}

        def upload_media(self, file_path, media_type):
            return {"fileIds": "file-creator-001", "width": 1080, "height": 1440, "media_type": media_type}

        def post_note(self, note_info):
            return {"success": True, "data": {"note_id": "creator-direct-note"}, "note_info": note_info}

    db_dependency, owner_token, creator_account_id = _create_creator_account_with_cookie(
        tmp_path, "creator-routes-owner"
    )
    intruder_token = _register_and_get_access_token("creator-routes-intruder")
    app.dependency_overrides[get_creator_api_adapter_factory] = lambda: FakeCreatorRoutesAdapter
    try:
        anonymous_response = client.post(
            "/api/xhs/creator/topics/search",
            json={"account_id": creator_account_id, "keyword": "breakfast"},
        )
        assert anonymous_response.status_code == 401

        intruder_response = client.post(
            "/api/xhs/creator/topics/search",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"account_id": creator_account_id, "keyword": "breakfast"},
        )
        assert intruder_response.status_code == 404

        topic_response = client.post(
            "/api/xhs/creator/topics/search",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"account_id": creator_account_id, "keyword": "breakfast"},
        )
        assert topic_response.status_code == 200
        assert topic_response.json()["items"][0]["name"] == "breakfast"

        location_response = client.post(
            "/api/xhs/creator/locations/search",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"account_id": creator_account_id, "keyword": "Shanghai"},
        )
        assert location_response.status_code == 200
        assert location_response.json()["items"][0]["id"] == "loc-creator"

        upload_response = client.post(
            "/api/xhs/creator/assets/upload",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"account_id": creator_account_id, "file_path": "storage/media/cover.png", "media_type": "image"},
        )
        assert upload_response.status_code == 200
        assert upload_response.json()["payload"]["fileIds"] == "file-creator-001"
        assert upload_response.json()["task"]["task_type"] == "creator_direct_upload"

        publish_response = client.post(
            "/api/xhs/creator/publish/image",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": creator_account_id,
                "title": "Direct title",
                "body": "Direct body",
                "image_file_infos": [{"fileIds": "file-creator-001", "width": 1080, "height": 1440}],
            },
        )
        assert publish_response.status_code == 200
        assert publish_response.json()["payload"]["data"]["note_id"] == "creator-direct-note"
        assert publish_response.json()["task"]["task_type"] == "creator_direct_publish"

        video_response = client.post(
            "/api/xhs/creator/publish/video",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": creator_account_id,
                "title": "Video title",
                "body": "Video body",
                "video_info": {"video_id": "video-001"},
            },
        )
        assert video_response.status_code == 200
        assert video_response.json()["payload"]["note_info"]["media_type"] == "video"

        db = next(app.dependency_overrides[get_db]())
        try:
            tasks = db.query(Task).filter(Task.task_type.in_(["creator_direct_upload", "creator_direct_publish"])).all()
            assert [task.status for task in tasks] == ["completed", "completed", "completed"]
            assert all(task.user_id == 1 for task in tasks)
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_creator_api_adapter_factory, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_xhs_creator_direct_publish_accepts_optional_publish_parameters(tmp_path):
    from backend.app.api.platforms.xhs.creator import get_creator_api_adapter_factory

    class FakeCreatorOptionalAdapter:
        calls = []

        def __init__(self, cookies):
            self.cookies = cookies

        def post_note(self, note_info):
            self.calls.append({"cookies": self.cookies, "note_info": note_info})
            return {"success": True, "data": {"note_id": "optional-note"}, "note_info": note_info}

    db_dependency, owner_token, creator_account_id = _create_creator_account_with_cookie(
        tmp_path, "creator-optional-owner"
    )
    FakeCreatorOptionalAdapter.calls = []
    app.dependency_overrides[get_creator_api_adapter_factory] = lambda: FakeCreatorOptionalAdapter
    try:
        image_response = client.post(
            "/api/xhs/creator/publish/image",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": creator_account_id,
                "title": "Direct optional title",
                "image_file_infos": [{"fileIds": "file-optional", "width": 1080, "height": 1440}],
                "topics": ["早餐", "通勤"],
                "location": "上海",
                "is_private": False,
            },
        )

        assert image_response.status_code == 200
        assert FakeCreatorOptionalAdapter.calls == [
            {
                "cookies": "a1=creator-a1; customer-sso-sid=creator-sso; access-token-creator.xiaohongshu.com=creator-token",
                "note_info": {
                    "title": "Direct optional title",
                    "desc": "",
                    "media_type": "image",
                    "image_file_infos": [{"fileIds": "file-optional", "width": 1080, "height": 1440}],
                    "type": 0,
                    "postTime": None,
                    "topics": ["早餐", "通勤"],
                    "location": "上海",
                },
            }
        ]
    finally:
        app.dependency_overrides.pop(get_creator_api_adapter_factory, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_monitoring_targets_crud_are_user_scoped(tmp_path):
    db_dependency = _override_database(tmp_path)
    owner_token = _register_and_get_access_token("monitoring-owner")
    intruder_token = _register_and_get_access_token("monitoring-intruder")
    try:
        anonymous_response = client.get("/api/xhs/monitoring/targets")
        assert anonymous_response.status_code == 401

        create_response = client.post(
            "/api/xhs/monitoring/targets",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "target_type": "keyword",
                "name": "早餐趋势",
                "value": "低卡早餐",
                "config": {"frequency": "daily"},
            },
        )
        assert create_response.status_code == 200
        created = create_response.json()
        assert created["target_type"] == "keyword"
        assert created["name"] == "早餐趋势"
        assert created["value"] == "低卡早餐"
        assert created["status"] == "active"

        owner_list_response = client.get(
            "/api/xhs/monitoring/targets",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert owner_list_response.status_code == 200
        assert owner_list_response.json()["total"] == 1

        intruder_list_response = client.get(
            "/api/xhs/monitoring/targets",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_list_response.status_code == 200
        assert intruder_list_response.json()["total"] == 0

        update_response = client.patch(
            f"/api/xhs/monitoring/targets/{created['id']}",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "早餐趋势组", "status": "paused"},
        )
        assert update_response.status_code == 200
        assert update_response.json()["name"] == "早餐趋势组"
        assert update_response.json()["status"] == "paused"

        intruder_update_response = client.patch(
            f"/api/xhs/monitoring/targets/{created['id']}",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"name": "偷看"},
        )
        assert intruder_update_response.status_code == 404

        delete_response = client.delete(
            f"/api/xhs/monitoring/targets/{created['id']}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert delete_response.status_code == 200
        assert delete_response.json() == {"id": created["id"], "status": "deleted"}
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_monitoring_targets_refresh_creates_owned_task(tmp_path):
    from backend.app.api.platforms.xhs.pc import get_xhs_pc_api_adapter_factory

    class FakeMonitoringAdapter:
        def __init__(self, cookies):
            self.cookies = cookies

        def get_user_notes(self, user_url):
            return True, "ok", {
                "data": {
                    "items": [
                        {
                            "note_card": {
                                "note_id": "crawled-note-001",
                                "display_title": "竞品账号低卡早餐",
                                "desc": "creator-001 的早餐选题",
                                "user": {"nickname": "creator-001"},
                                "interact_info": {"liked_count": 50, "collected_count": 20, "comment_count": 3},
                                "cover": {"url": "https://img.example/cover.png"},
                            }
                        }
                    ],
                    "total": 1,
                    "has_more": False,
                }
            }

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "monitoring-refresh-owner")
    intruder_token = _register_and_get_access_token("monitoring-refresh-intruder")
    app.dependency_overrides[get_xhs_pc_api_adapter_factory] = lambda: FakeMonitoringAdapter
    try:
        created = client.post(
            "/api/xhs/monitoring/targets",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"target_type": "account", "name": "竞品账号", "value": "creator-001"},
        ).json()

        refresh_response = client.post(
            f"/api/xhs/monitoring/targets/{created['id']}/refresh",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert refresh_response.status_code == 200
        payload = refresh_response.json()
        assert payload["target"]["id"] == created["id"]
        assert payload["task"]["task_type"] == "monitoring_crawl"
        assert payload["task"]["status"] == "completed"
        assert payload["task"]["payload"]["target_id"] == created["id"]
        assert payload["task"]["payload"]["crawled_count"] == 1
        assert payload["snapshot"]["payload"]["matched_count"] >= 1
        assert payload["snapshot"]["payload"]["top_notes"][0]["note_id"] == "crawled-note-001"
        assert payload["target"]["consecutive_failures"] == 0

        tasks_response = client.get(
            "/api/tasks?platform=xhs",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert tasks_response.status_code == 200
        assert tasks_response.json()["items"][0]["task_type"] == "monitoring_crawl"

        notes_response = client.get(
            f"/api/xhs/monitoring/targets/{created['id']}/notes",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert notes_response.status_code == 200
        assert len(notes_response.json()["items"]) >= 1

        snapshots_response = client.get(
            f"/api/xhs/monitoring/targets/{created['id']}/snapshots",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert snapshots_response.status_code == 200
        assert snapshots_response.json()["items"][0]["payload"]["matched_count"] >= 1

        intruder_response = client.post(
            f"/api/xhs/monitoring/targets/{created['id']}/refresh",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_response.status_code == 404
    finally:
        app.dependency_overrides.pop(get_xhs_pc_api_adapter_factory, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_crawl_rate_limiter_blocks_after_max_requests():
    from backend.app.services.rate_limiter import CrawlRateLimiter

    limiter = CrawlRateLimiter(max_per_minute=3)
    assert limiter.allow(1) is True
    assert limiter.allow(1) is True
    assert limiter.allow(1) is True
    assert limiter.allow(1) is False

    assert limiter.allow(2) is True

    limiter.reset(1)
    assert limiter.allow(1) is True


def test_keyword_groups_require_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        response = client.get("/api/keyword-groups")

        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_keyword_groups_crud_are_user_scoped(tmp_path):
    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "keyword-group-owner")
    intruder_token = _register_and_get_access_token("keyword-group-intruder")
    try:
        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [
                    {
                        "note_id": "keyword-note-001",
                        "title": "低卡早餐模板",
                        "content": "适合通勤的低卡早餐搭配。",
                        "author_name": "早餐作者",
                        "raw": {"likes": 30, "collects": 10, "comments": 4, "shares": 1, "tags": ["低卡", "早餐"]},
                    },
                    {
                        "note_id": "keyword-note-002",
                        "title": "家居收纳灵感",
                        "content": "收纳工具清单。",
                        "author_name": "收纳作者",
                        "raw": {"likes": 2, "collects": 1, "comments": 0, "shares": 0, "tags": ["收纳"]},
                    },
                ],
            },
        )
        assert save_response.status_code == 200

        create_response = client.post(
            "/api/keyword-groups",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "name": "早餐机会", "keywords": ["低卡", "早餐"]},
        )
        assert create_response.status_code == 200
        created = create_response.json()
        assert created["name"] == "早餐机会"
        assert created["keywords"] == ["低卡", "早餐"]

        owner_list_response = client.get("/api/keyword-groups", headers={"Authorization": f"Bearer {owner_token}"})
        assert owner_list_response.status_code == 200
        assert owner_list_response.json()["total"] == 1

        intruder_list_response = client.get("/api/keyword-groups", headers={"Authorization": f"Bearer {intruder_token}"})
        assert intruder_list_response.status_code == 200
        assert intruder_list_response.json()["total"] == 0

        detail_response = client.get(
            f"/api/keyword-groups/{created['id']}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert detail["trend"]["total_matches"] == 1
        assert detail["trend"]["total_engagement"] == 45
        assert detail["trend"]["keywords"][0]["keyword"] == "低卡"
        assert detail["trend"]["matched_notes"][0]["note_id"] == "keyword-note-001"

        patch_response = client.patch(
            f"/api/keyword-groups/{created['id']}",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "早餐选题池", "keywords": ["早餐"]},
        )
        assert patch_response.status_code == 200
        assert patch_response.json()["name"] == "早餐选题池"
        assert patch_response.json()["keywords"] == ["早餐"]

        cross_get_response = client.get(
            f"/api/keyword-groups/{created['id']}",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert cross_get_response.status_code == 404

        cross_patch_response = client.patch(
            f"/api/keyword-groups/{created['id']}",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"name": "偷看"},
        )
        assert cross_patch_response.status_code == 404

        delete_response = client.delete(
            f"/api/keyword-groups/{created['id']}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert delete_response.status_code == 200
        assert delete_response.json()["status"] == "deleted"
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_notes_batch_save_rejects_cross_user_account(tmp_path):
    db_dependency, _, account_id = _create_pc_account_with_cookie(tmp_path, "save-owner-2")
    intruder_token = _register_and_get_access_token("save-intruder")
    try:
        response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"account_id": account_id, "notes": [{"note_id": "note-001", "title": "标题"}]},
        )

        assert response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_notes_library_list_requires_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        response = client.get("/api/notes?platform=xhs")

        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_notes_library_list_returns_only_current_user_saved_notes(tmp_path):
    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "library-owner")
    intruder_token = _register_and_get_access_token("library-intruder")
    try:
        owner_save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [
                    {
                        "note_id": "owner-note-001",
                        "title": "我的内容库笔记",
                        "content": "这条应该只属于 owner。",
                        "author_name": "作者 Owner",
                        "raw": {"source": "owner"},
                    }
                ],
            },
        )
        assert owner_save_response.status_code == 200

        intruder_response = client.get(
            "/api/notes?platform=xhs",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_response.status_code == 200
        assert intruder_response.json()["total"] == 0

        owner_response = client.get(
            "/api/notes?platform=xhs",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert owner_response.status_code == 200
        payload = owner_response.json()
        assert payload["total"] == 1
        assert payload["page"] == 1
        assert payload["page_size"] == 20
        assert payload["items"][0]["note_id"] == "owner-note-001"
        assert payload["items"][0]["title"] == "我的内容库笔记"
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_notes_library_filters_by_keyword_tag_assets_and_comments(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.models import NoteComment

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "library-filter-owner")
    intruder_token = _register_and_get_access_token("library-filter-intruder")
    try:
        first_save = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [
                    {
                        "note_id": "filter-note-assets",
                        "title": "低卡早餐灵感",
                        "content": "适合通勤前快速准备。",
                        "author_name": "早餐研究员",
                        "image_urls": ["https://example.test/filter-assets.webp"],
                    },
                    {
                        "note_id": "filter-note-comments",
                        "title": "旅行收纳清单",
                        "content": "评论里有很多问题。",
                        "author_name": "收纳作者",
                    },
                ],
            },
        )
        assert first_save.status_code == 200
        asset_note_id = first_save.json()["items"][0]["id"]
        comment_note_id = first_save.json()["items"][1]["id"]

        tag_response = client.post(
            "/api/topics",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "Breakfast", "color": "#111111"},
        )
        tag_id = tag_response.json()["id"]
        client.post(
            "/api/notes/batch-tag",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"note_ids": [asset_note_id], "tag_ids": [tag_id], "mode": "replace"},
        )
        db = next(app.dependency_overrides[get_db]())
        try:
            db.add(
                NoteComment(
                    note_id=comment_note_id,
                    comment_id="filter-comment-001",
                    user_name="评论用户",
                    content="这条笔记有评论",
                )
            )
            db.commit()
        finally:
            db.close()

        keyword_response = client.get(
            "/api/notes?platform=xhs&q=早餐",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert keyword_response.status_code == 200
        assert [item["note_id"] for item in keyword_response.json()["items"]] == ["filter-note-assets"]

        tag_filter_response = client.get(
            f"/api/notes?platform=xhs&topic_id={tag_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert tag_filter_response.status_code == 200
        assert [item["note_id"] for item in tag_filter_response.json()["items"]] == ["filter-note-assets"]

        assets_response = client.get(
            "/api/notes?platform=xhs&has_assets=true",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert assets_response.status_code == 200
        assert [item["note_id"] for item in assets_response.json()["items"]] == ["filter-note-assets"]

        comments_response = client.get(
            "/api/notes?platform=xhs&has_comments=true",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert comments_response.status_code == 200
        assert [item["note_id"] for item in comments_response.json()["items"]] == ["filter-note-comments"]

        intruder_response = client.get(
            f"/api/notes?platform=xhs&topic_id={tag_id}",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_notes_library_detail_enforces_ownership(tmp_path):
    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "library-detail-owner")
    intruder_token = _register_and_get_access_token("library-detail-intruder")
    try:
        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [
                    {
                        "note_id": "detail-note-001",
                        "title": "详情页笔记",
                        "content": "详情正文",
                        "author_name": "详情作者",
                        "raw": {"source": "detail"},
                    }
                ],
            },
        )
        note_id = save_response.json()["items"][0]["id"]

        owner_response = client.get(
            f"/api/notes/{note_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert owner_response.status_code == 200
        assert owner_response.json()["note_id"] == "detail-note-001"
        assert owner_response.json()["raw_json"] == {"source": "detail"}

        intruder_response = client.get(
            f"/api/notes/{note_id}",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_notes_library_delete_removes_owned_note_children_and_rejects_cross_user(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.models import AiDraft, Note, NoteAsset, NoteComment, NoteTopic, Topic

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "library-delete-owner")
    intruder_token = _register_and_get_access_token("library-delete-intruder")
    try:
        db = next(app.dependency_overrides[get_db]())
        try:
            note = Note(
                user_id=1,
                platform_account_id=owner_account_id,
                platform="xhs",
                note_id="delete-note-001",
                title="Delete me",
            )
            topic = Topic(user_id=1, name="待删除")
            db.add_all([note, topic])
            db.flush()
            db.add(NoteAsset(note_id=note.id, asset_type="image", url="https://example.test/delete.webp"))
            db.add(NoteComment(note_id=note.id, comment_id="delete-comment", content="删除评论"))
            db.add(AiDraft(user_id=1, platform="xhs", title="Draft", body="Body", source_note_id=note.id))
            db.add(NoteTopic(note_id=note.id, topic_id=topic.id))
            db.commit()
            note_id = note.id
        finally:
            db.close()

        intruder_response = client.delete(
            f"/api/notes/{note_id}",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_response.status_code == 404

        owner_response = client.delete(
            f"/api/notes/{note_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert owner_response.status_code == 200
        assert owner_response.json() == {"id": note_id, "status": "deleted"}

        detail_response = client.get(
            f"/api/notes/{note_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert detail_response.status_code == 404

        db = next(app.dependency_overrides[get_db]())
        try:
            assert db.get(Note, note_id) is None
            assert db.query(NoteAsset).filter(NoteAsset.note_id == note_id).count() == 0
            assert db.query(NoteComment).filter(NoteComment.note_id == note_id).count() == 0
            assert db.scalars(select(NoteTopic).where(NoteTopic.note_id == note_id)).first() is None
            assert db.query(AiDraft).filter(AiDraft.source_note_id == note_id).count() == 0
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_drafts_api_requires_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        response = client.post(
            "/api/drafts",
            json={"platform": "xhs", "title": "草稿标题", "body": "草稿正文"},
        )

        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_drafts_api_creates_from_owned_note_and_lists_current_user_only(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.models import AiDraft

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "draft-owner")
    intruder_token = _register_and_get_access_token("draft-intruder")
    try:
        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [
                    {
                        "note_id": "draft-source-note",
                        "title": "源笔记标题",
                        "content": "源笔记正文",
                        "author_name": "源作者",
                        "raw": {"source": "draft"},
                    }
                ],
            },
        )
        source_note_id = save_response.json()["items"][0]["id"]

        create_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "source_note_id": source_note_id, "intent": "rewrite"},
        )
        assert create_response.status_code == 200
        created = create_response.json()
        assert created["platform"] == "xhs"
        assert created["title"] == "源笔记标题"
        assert created["body"] == "源笔记正文"
        assert created["source_note_id"] == source_note_id

        owner_list_response = client.get(
            "/api/drafts?platform=xhs",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert owner_list_response.status_code == 200
        owner_payload = owner_list_response.json()
        assert owner_payload["total"] == 1
        assert owner_payload["items"][0]["id"] == created["id"]

        intruder_list_response = client.get(
            "/api/drafts?platform=xhs",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_list_response.status_code == 200
        assert intruder_list_response.json()["total"] == 0

        db = next(app.dependency_overrides[get_db]())
        try:
            draft = db.query(AiDraft).one()
            assert draft.source_note_id == source_note_id
            assert draft.title == "源笔记标题"
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_drafts_api_rejects_cross_user_source_note(tmp_path):
    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "draft-source-owner")
    intruder_token = _register_and_get_access_token("draft-source-intruder")
    try:
        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [{"note_id": "foreign-source", "title": " чужая", "content": "nope"}],
            },
        )
        source_note_id = save_response.json()["items"][0]["id"]

        response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"platform": "xhs", "source_note_id": source_note_id, "intent": "publish"},
        )

        assert response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_drafts_update_persists_owned_changes_and_rejects_cross_user(tmp_path):
    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "draft-update-owner")
    intruder_token = _register_and_get_access_token("draft-update-intruder")
    try:
        save_response = client.post(
            "/api/notes/batch-save",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "account_id": owner_account_id,
                "notes": [{"note_id": "update-source", "title": "旧标题", "content": "旧正文"}],
            },
        )
        source_note_id = save_response.json()["items"][0]["id"]
        create_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "source_note_id": source_note_id},
        )
        draft_id = create_response.json()["id"]

        update_response = client.patch(
            f"/api/drafts/{draft_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"title": "新标题", "body": "新正文"},
        )
        assert update_response.status_code == 200
        updated = update_response.json()
        assert updated["title"] == "新标题"
        assert updated["body"] == "新正文"

        list_response = client.get(
            "/api/drafts?platform=xhs",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        listed = list_response.json()["items"][0]
        assert listed["id"] == draft_id
        assert listed["title"] == "新标题"
        assert listed["body"] == "新正文"

        intruder_response = client.patch(
            f"/api/drafts/{draft_id}",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"title": "攻击标题", "body": "攻击正文"},
        )
        assert intruder_response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_model_configs_require_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        response = client.get("/api/model-configs")

        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_model_configs_endpoints_are_gone_410(tmp_path):
    """⚠️ v2.0（设计 17 §13.2.4/§13.2.6）：`model_configs` 6 端点整模块退场为 410 墓碑。

    保留路由而非删除：删掉会回 404，而 404 无法区分「端点从未存在」与
    「功能已退场」（同 ADR-021 / `llm_gateway.py` DELETE 410 的退场惯例）。
    `include_in_schema=False` ⇒ 不计入 OpenAPI，但实打仍回 410 + 结构化 code。
    """
    db_dependency = _override_database(tmp_path)
    owner_token = _register_and_get_access_token("model-owner")
    intruder_token = _register_and_get_access_token("model-intruder")
    try:
        # 鉴权语义不因退场放松：未认证仍然先被 401 拦下（端点依赖照常声明）。
        unauth = client.get("/api/model-configs")
        assert unauth.status_code == 401

        # 6 个端点逐一回 410 Gone + 结构化 {code, message}（路径缺一个就退化成 404）
        create_response = client.post(
            "/api/model-configs",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "name": "OpenAI Text",
                "model_type": "text",
                "provider": "openai-compatible",
                "model_name": "gpt-test",
                "base_url": "https://api.example.test/v1",
                "api_key": "***",
                "is_default": True,
            },
        )
        assert create_response.status_code == 410
        assert create_response.json()["detail"]["code"] == "MODEL_CONFIGS_GONE"

        list_response = client.get(
            "/api/model-configs?model_type=text",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert list_response.status_code == 410

        # 跨用户也是 410（退场对所有用户一致，不存在「这个用户还能用」）
        intruder_list_response = client.get(
            "/api/model-configs",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_list_response.status_code == 410

        test_response = client.post(
            "/api/model-configs/1/test",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert test_response.status_code == 410

        update_response = client.patch(
            "/api/model-configs/1",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "stolen"},
        )
        assert update_response.status_code == 410

        delete_response = client.delete(
            "/api/model-configs/1",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert delete_response.status_code == 410

        set_default_response = client.post(
            "/api/model-configs/1/set-default",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert set_default_response.status_code == 410

        # ⚠️ **一行数据都没被写**：退场是墓碑，不是「写进去再标记不可用」。
        # ⚠️ 2026-10-13 修正：R4 服务商种子（设计 37）在**注册时**就为新用户播种了
        # 默认业务点映射 ⇒ 「绝对为 0」不再成立（原断言因此恒红）。
        # 判据改为**「这 6 次 410 调用一行都没新增」** —— 那才是本用例真正要守的语义
        # （墓碑不写库），且不受注册播种的影响。
        from backend.app.core.database import get_db
        from backend.app.models import LlmBusinessProvider

        db = next(app.dependency_overrides[get_db]())
        try:
            rows = db.query(LlmBusinessProvider).all()
            # 注册播种只产生**内置种子服务商**的映射（provider_key 属于种子集合）；
            # 410 调用若真写了库，会出现 `is_default=True` 那条请求体对应的痕迹或
            # 行数比播种基线多。用「行数 == 本用户播种可见数，且无一条因本次调用而变」表达。
            # 简单可靠的做法：与「只注册、不调 410」的对照用户比行数。
            assert all(row.business_key for row in rows), "映射行不许有空业务点键"
            # 对照：intruder 同样只注册（未调 410）⇒ 两者行数必须相等
            from backend.app.models import User as _User

            owner_ids = {
                u.id for u in db.query(_User).filter(_User.username == "model-owner").all()
            }
            intruder_ids = {
                u.id for u in db.query(_User).filter(_User.username == "model-intruder").all()
            }
            owner_rows = [r for r in rows if r.user_id in owner_ids]
            intruder_rows = [r for r in rows if r.user_id in intruder_ids]
            assert len(owner_rows) == len(intruder_rows), (
                "410 墓碑写了库：owner（调过 6 个 410 端点）的映射行数 != "
                "intruder（没调过）的行数"
            )
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_text_model_config_default_name_rule_is_gone_410(tmp_path):
    """⚠️ v2.0：一期「文本模型默认名 gpt-5.4」的归一化规则随端点一起退场。

    该规则属于 `model_configs` 语义（新建时补默认模型名）；v2.0 的模型名来自
    「平台 × 业务点」映射**显式选定的模型**（§13.2.4，v2.3 起不再有服务商兜底名，
    v2.8 `default_model` 列已删），不需要端点侧归一化 ⇒ 连端点一起 410。
    """
    db_dependency = _override_database(tmp_path)
    owner_token = _register_and_get_access_token("model-default-owner")
    try:
        response = client.post(
            "/api/model-configs",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "name": "Default Text",
                "model_type": "text",
                "provider": "openai-compatible",
                "base_url": "https://api.example.test/v1",
                "api_key": "sk-default",
                "is_default": True,
            },
        )

        assert response.status_code == 410
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_model_configs_update_paths_are_gone_410(tmp_path):
    """⚠️ v2.0：`PATCH` / `set-default` / 越权 404 判据全部随端点退场为 410。

    旧用例的两条判据（改默认的互斥、跨用户 404）在 v2.0 已无对应实现：
    「默认」概念被「平台 × 业务点 → 服务商」映射取代（§13.2.2），
    越权隔离改由映射的 `user_id` 约束保证（`resolve_business_provider`）。
    """
    db_dependency = _override_database(tmp_path)
    owner_token = _register_and_get_access_token("model-update-owner")
    intruder_token = _register_and_get_access_token("model-update-intruder")
    try:
        update_response = client.patch(
            "/api/model-configs/1",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": "Text Two Updated", "api_key": "secret-b2"},
        )
        assert update_response.status_code == 410

        # ⚠️ 退场对所有用户一致：旧判据「跨用户 404」被 410 取代（404 已无法表达）
        intruder_update_response = client.patch(
            "/api/model-configs/1",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"name": "stolen"},
        )
        assert intruder_update_response.status_code == 410

        default_response = client.post(
            "/api/model-configs/1/set-default",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert default_response.status_code == 410

        intruder_default_response = client.post(
            "/api/model-configs/1/set-default",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_default_response.status_code == 410
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_ai_rewrite_note_requires_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        response = client.post("/api/ai/rewrite-note", json={"draft_id": 1})

        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_ai_rewrite_note_requires_owned_draft_and_default_text_model(tmp_path):
    db_dependency = _override_database(tmp_path)
    owner_token = _register_and_get_access_token("ai-rewrite-owner")
    intruder_token = _register_and_get_access_token("ai-rewrite-intruder")
    try:
        create_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "title": "Original title", "body": "Original body"},
        )
        draft_id = create_response.json()["id"]

        no_model_response = client.post(
            "/api/ai/rewrite-note",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"draft_id": draft_id},
        )
        assert no_model_response.status_code == 400
        # v2.0（设计 17 §13.2）：判据从「没有默认文本模型」改为「该业务点没配可用服务商」，
        # 且响应体是结构化 {code,message}（不再是一期那句英文）。
        # ⚠️ 2026-10-13 契约更新（R4 服务商种子落地带来的行为变更）：
        # 注册时 `seed_default_business_map_for_user` 会给新用户播种默认业务点映射
        # （设计 37）⇒ 新用户此刻的状态是「映射在、种子服务商没配 API Key」，
        # 故 code 是 `LLM_PROVIDER_KEY_MISSING`（本轮新增）而非
        # `LLM_BUSINESS_PROVIDER_NOT_CONFIGURED`（那条留给「真的没有映射」）。
        # 两者都必须是**结构化 400 + 指向性文案**，不许落成 502。
        _detail = no_model_response.json()["detail"]
        assert _detail["code"] == "LLM_PROVIDER_KEY_MISSING", _detail
        assert "API Key" in _detail["message"], _detail

        intruder_response = client.post(
            "/api/ai/rewrite-note",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"draft_id": draft_id},
        )
        assert intruder_response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_ai_rewrite_note_uses_default_text_model_and_updates_owned_draft(tmp_path):
    from backend.app.api.ai import get_text_ai_client

    class FakeTextAiClient:
        def __init__(self):
            self.calls = []

        def rewrite_note(self, *, model_config, api_key, title, body, instruction, platform="", function=""):
            self.calls.append(
                {
                    "model_name": model_config.model_name,
                    "api_key": api_key,
                    "title": title,
                    "body": body,
                    "instruction": instruction,
                    "platform": platform,
                    "function": function,
                }
            )
            return f"改写结果：{title} / {instruction}"

    fake_client = FakeTextAiClient()
    db_dependency = _override_database(tmp_path)
    owner_token = _register_and_get_access_token("ai-rewrite-success")
    try:
        app.dependency_overrides[get_text_ai_client] = lambda: fake_client

        # v2.0（设计 17 §13.2）：一期「默认文本模型」随 `model_configs` 退场，
        # 改配「平台 × 业务点 → 服务商」映射。
        # ⚠️ 播种必须在**再注册任何用户之前** —— 辅助函数按「最近注册的用户」定位。
        _seed_provider_and_mappings(owner_token, "text", "gpt-rewrite-test")
        intruder_token = _register_and_get_access_token("ai-rewrite-intruder-success")

        draft_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "title": "爆款标题", "body": "原始正文"},
        )
        draft_id = draft_response.json()["id"]

        rewrite_response = client.post(
            "/api/ai/rewrite-note",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"draft_id": draft_id, "instruction": "更适合小红书种草"},
        )
        assert rewrite_response.status_code == 200
        rewritten = rewrite_response.json()
        assert rewritten["id"] == draft_id
        assert rewritten["body"] == "改写结果：爆款标题 / 更适合小红书种草"
        _want0 = {'model_name': 'gpt-rewrite-test', 'api_key': '***', 'title': '爆款标题',
                  'body': '原始正文', 'instruction': '更适合小红书种草',
                  'platform': 'xhs', 'function': 'rewrite'}
        assert fake_client.calls == [
            {
                "model_name": "gpt-rewrite-test",
                "api_key": "***",
                "title": "爆款标题",
                "body": "原始正文",
                "instruction": "更适合小红书种草",
                "platform": "xhs",
                "function": "rewrite",
            }
        ]

        # 改名（本期用例原步骤，被一次误编辑删掉后补回）：改完再从列表读，断言新标题。
        patch_response = client.patch(
            f"/api/drafts/{draft_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"title": "新标题", "body": "新正文"},
        )
        assert patch_response.status_code == 200

        list_response = client.get(
            "/api/drafts?platform=xhs",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        listed = list_response.json()["items"][0]
        assert listed["id"] == draft_id
        assert listed["title"] == "新标题"
        assert listed["body"] == "新正文"

        intruder_response = client.patch(
            f"/api/drafts/{draft_id}",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"title": "攻击标题", "body": "攻击正文"},
        )
        assert intruder_response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_ai_text_generation_requires_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        response = client.post("/api/ai/generate-note", json={"topic": "低卡早餐"})

        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_ai_text_generation_endpoints_use_business_model_and_create_tasks(tmp_path):
    from backend.app.api.ai import get_text_ai_client

    class FakeTextGenerationClient:
        def __init__(self):
            self.calls = []

        def rewrite_note(self, *, model_config, api_key, title, body, instruction, platform="", function=""):
            raise AssertionError("rewrite_note should not be called")

        def generate_note(self, *, model_config, api_key, topic, reference, instruction, platform="", function=""):
            self.calls.append(
                ("generate_note", model_config.model_name, api_key, topic, reference, instruction, platform, function)
            )
            return {"title": f"{topic} 标题", "body": f"{topic} 正文 {reference} {instruction}".strip()}

        def generate_titles(self, *, model_config, api_key, title, body, count, platform="", function=""):
            self.calls.append(
                ("generate_titles", model_config.model_name, api_key, title, body, count, platform, function)
            )
            return ["标题 A", "标题 B"][:count]

        def generate_tags(self, *, model_config, api_key, title, body, count, platform="", function=""):
            self.calls.append(
                ("generate_tags", model_config.model_name, api_key, title, body, count, platform, function)
            )
            return ["低卡", "早餐", "通勤"][:count]

        def polish_text(self, *, model_config, api_key, text, instruction, platform="", function=""):
            self.calls.append(("polish_text", model_config.model_name, api_key, text, instruction, platform, function))
            return f"润色：{text} / {instruction}"

    fake_client = FakeTextGenerationClient()
    db_dependency = _override_database(tmp_path)
    owner_token = _register_and_get_access_token("ai-generate-owner")
    try:
        app.dependency_overrides[get_text_ai_client] = lambda: fake_client
        # v2.0（设计 17 §13.2）：一期「默认文本模型」随 `model_configs` 退场，
        # 改配「平台 × 业务点 → 服务商」映射。8 个业务点全部指向同一条文本服务商。
        _seed_provider_and_mappings(owner_token, "text", "gpt-generate-test", api_key="sk-generation-secret")

        generate_response = client.post(
            "/api/ai/generate-note",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "topic": "低卡早餐", "reference": "参考笔记", "instruction": "更具体"},
        )
        assert generate_response.status_code == 200
        generated = generate_response.json()
        assert generated["title"] == "低卡早餐 标题"
        assert generated["body"] == "低卡早餐 正文 参考笔记 更具体"

        titles_response = client.post(
            "/api/ai/generate-title",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"title": "旧标题", "body": "正文", "count": 2},
        )
        assert titles_response.status_code == 200
        assert titles_response.json()["items"] == ["标题 A", "标题 B"]

        tags_response = client.post(
            "/api/ai/generate-tags",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"title": "早餐", "body": "低卡早餐正文", "count": 3},
        )
        assert tags_response.status_code == 200
        assert tags_response.json()["items"] == ["低卡", "早餐", "通勤"]

        polish_response = client.post(
            "/api/ai/polish-text",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"text": "原文", "instruction": "更自然"},
        )
        assert polish_response.status_code == 200
        assert polish_response.json()["text"] == "润色：原文 / 更自然"

        tasks_response = client.get("/api/tasks?platform=xhs", headers={"Authorization": f"Bearer {owner_token}"})
        assert tasks_response.status_code == 200
        task_types = [item["task_type"] for item in tasks_response.json()["items"]]
        assert task_types == ["ai_polish_text", "ai_generate_tags", "ai_generate_title", "ai_generate_note"]
        assert fake_client.calls[0] == (
            "generate_note",
            "gpt-generate-test",
            "sk-generation-secret",
            "低卡早餐",
            "参考笔记",
            "更具体",
            # 台账记账维度（设计 17 §4.3.1）：generate-note 的 platform 来自请求体
            "xhs",
            "generate",
        )
    finally:
        app.dependency_overrides.pop(get_text_ai_client, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_ai_image_routes_require_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        response = client.post("/api/ai/images/generate-cover", json={"prompt": "低卡早餐封面"})

        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_ai_image_routes_use_business_model_store_assets_and_enforce_scope(tmp_path):
    from backend.app.api.ai import get_image_ai_client

    class FakeImageAiClient:
        def __init__(self):
            self.calls = []

        def generate_cover(self, *, model_config, api_key, prompt, size, style):
            self.calls.append(("generate_cover", model_config.model_name, api_key, prompt, size, style))
            return {"url": "https://cdn.example.test/cover.png", "raw": {"seed": 1}}

        def describe_image(self, *, model_config, api_key, image_url, instruction, platform="", function=""):
            self.calls.append(
                ("describe_image", model_config.model_name, api_key, image_url, instruction, platform, function)
            )
            return "这是一张低卡早餐封面"

    fake_client = FakeImageAiClient()
    db_dependency = _override_database(tmp_path)
    owner_token = _register_and_get_access_token("ai-image-owner")
    try:
        app.dependency_overrides[get_image_ai_client] = lambda: fake_client
        # v2.0：图像业务点同样改走映射（服务商 `model_type=image`）。
        # ⚠️ 播种必须在**再注册任何用户之前** —— 辅助函数按「最近注册的用户」定位。
        _seed_provider_and_mappings(owner_token, "image", "image-generate-test", api_key="sk-image-secret")
        intruder_token = _register_and_get_access_token("ai-image-intruder")

        generate_response = client.post(
            "/api/ai/images/generate-cover",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"prompt": "低卡早餐封面", "size": "1024x1024", "style": "clean"},
        )
        assert generate_response.status_code == 200
        generated = generate_response.json()
        assert generated["file_path"] == "https://cdn.example.test/cover.png"
        assert generated["prompt"] == "低卡早餐封面"
        assert generated["model_name"] == "image-generate-test"

        list_response = client.get("/api/ai/images/assets", headers={"Authorization": f"Bearer {owner_token}"})
        assert list_response.status_code == 200
        assert list_response.json()["total"] == 1
        assert list_response.json()["items"][0]["id"] == generated["id"]

        intruder_list_response = client.get("/api/ai/images/assets", headers={"Authorization": f"Bearer {intruder_token}"})
        assert intruder_list_response.status_code == 200
        assert intruder_list_response.json()["total"] == 0

        describe_response = client.post(
            "/api/ai/images/describe",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"image_url": generated["file_path"], "instruction": "描述卖点"},
        )
        assert describe_response.status_code == 200
        assert describe_response.json()["text"] == "这是一张低卡早餐封面"

        tasks_response = client.get("/api/tasks?platform=xhs", headers={"Authorization": f"Bearer {owner_token}"})
        assert tasks_response.status_code == 200
        task_types = [item["task_type"] for item in tasks_response.json()["items"]]
        assert task_types == ["ai_image_describe", "ai_image_generate_cover"]
        assert fake_client.calls == [
            ("generate_cover", "image-generate-test", "sk-image-secret", "低卡早餐封面", "1024x1024", "clean"),
            (
                "describe_image",
                "image-generate-test",
                "sk-image-secret",
                "https://cdn.example.test/cover.png",
                "描述卖点",
                # 台账记账维度（设计 17 §4.3.1）：图片理解固定 xhs/describe
                "xhs",
                "describe",
            ),
        ]
    finally:
        app.dependency_overrides.pop(get_image_ai_client, None)


# --------------------------------------------------------------------------- #
# 商品详情图（设计 17 §13.3.3，v2.1）—— 「商品列表 → 商品详情 → 生成商品详情图」
# --------------------------------------------------------------------------- #


def _seed_shop_product(
    *,
    platform_account_id: int,
    item_id: str,
    images: list[str],
    title: str = "测试商品",
    detail_images: list[str] | None = None,
) -> None:
    """播种一条本地商品台账行（商品详情图的数据来源）。

    ⚠️ 展示字段（`title` / `images`）由 `raw_json` 现场派生（设计 13 §30.3.3）——
    只写 `item_title` 列**不够**：`item_detail()` 读的是 `raw_json`。这里两者都写，
    与真实同步落库的形状一致。

    ⚠️ **v2.15 修正**：`raw_json` 必须用**平台真实字段名** `images` /
    `image_descriptions`（此前写的是自造的 `image_links`，而平台根本没有这个键
    ⇒ 一切「从 raw 取图」的断言都在对着空列表做，**看着绿但没测到东西**）。
    真机形状：`images: [{link, width, height, …}]`、`image_descriptions: [{link, …}]`。
    """
    from backend.app.models.shop_product import ShopProduct

    db = _test_session_factory()
    try:
        db.add(
            ShopProduct(
                user_id=_last_registered_user_id,
                platform_account_id=platform_account_id,
                item_id=item_id,
                item_title=title,
                images_json=json.dumps(images),
                raw_json=json.dumps(
                    {
                        "item_id": item_id,
                        "item_name": title,
                        "images": [{"link": url} for url in images],
                        "image_descriptions": [
                            {"link": url} for url in (detail_images or [])
                        ],
                    }
                ),
            )
        )
        db.commit()
    finally:
        db.close()


def _seed_account_for_last_user() -> int:
    """给「刚注册那个用户」建一个平台账号，返回它的 id。"""
    from backend.app.models.platform_account import PlatformAccount

    db = _test_session_factory()
    try:
        account = PlatformAccount(
            user_id=_last_registered_user_id,
            platform="xhs",
            nickname="详情图测试账号",
            sub_type="pc",
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        return account.id
    finally:
        db.close()


def test_generate_detail_image_requires_authentication(tmp_path):
    """未鉴权一律 401（与其它 ai 路由同口径）。"""
    db_dependency = _override_database(tmp_path)
    try:
        response = client.post(
            "/api/xhs/qianfan/generate-detail-image",
            json={"platform_account_id": 1, "item_id": "ITEM-1"},
        )
        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_generate_detail_image_uses_own_business_point_and_persists_asset(tmp_path):
    """核心链路：走**自己的**业务点（非 generate_image）、商品图由服务端取、落素材库。

    变异守护：把业务点换成 `generate_image` ⇒ 本用例必红（见种子只映射详情图业务点时的
    「未配置」断言 —— 若代码走配图点，第二次调用会成功而不是 400）。
    """
    from backend.app.api.ai import get_image_ai_client
    from backend.app.models.generation import AigcMediaAsset
    from backend.app.services.business_provider_keys import (
        BUSINESS_KEY_PRODUCT_DETAIL_IMAGE,
    )
    from backend.app.models.generation import AigcMediaAsset as _Asset  # noqa: F401

    class FakeImageClient:
        def __init__(self):
            self.calls = []

        def generate_image(self, *, model_config, api_key, prompt, reference_images=None):
            self.calls.append((model_config.model_name, prompt, list(reference_images or [])))
            return {"url": "https://cdn.example.test/detail.png", "local_path": ""}

        # v2.29：详情图链路改调批量接口（一次要 n 张）；记录与逐张路径等价，
        # 既有 `len(fake.calls)` 断言不用改。
        def generate_images(self, *, model_config, api_key, prompt, reference_images=None, count=1):
            return [
                self.generate_image(
                    model_config=model_config,
                    api_key=api_key,
                    prompt=prompt,
                    reference_images=reference_images,
                )
                for _ in range(count)
            ]

    fake = FakeImageClient()
    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("detail-image-owner")
    try:
        app.dependency_overrides[get_image_ai_client] = lambda: fake
        # 只映射**详情图**业务点（不是全 image 类）：这样「代码是否走了对的业务点」
        # 变成可判定 —— 走错点会拿到未配置的 400。
        from backend.app.core.security import encrypt_text
        from backend.app.models import LlmBusinessProvider, LlmGatewayConfig, LlmGatewayModel

        db = _test_session_factory()
        try:
            db.add(
                LlmGatewayConfig(
                    user_id=_last_registered_user_id,
                    name="test-image",
                    provider_key="TEST-IMAG",
                    base_url="https://api.example.test/v1",
                    access_token=encrypt_text("«redacted:sk-…»"),
                    models_json=json.dumps(["detail-image-model"]),
                    enabled=True,
                    healthy=True,
                )
            )
            db.flush()
            # ⚠️ v2.7/v2.8：`platform` / `model_type` / `default_model` 已从服务商层移除 ——
            # 能力类型与使用平台改由**模型行**承载（v2.7），兜底名整列删除（v2.8）。
            db.add(
                LlmGatewayModel(
                    user_id=_last_registered_user_id,
                    provider_key="TEST-IMAG",
                    model_name="detail-image-model",
                    model_type="image",
                    platform="xhs",
                    enabled=True,
                )
            )
            db.flush()
            # ⚠️ 2026-10-13：改走幂等 upsert（R4 注册即播种默认映射 ⇒ 盲 add 会撞唯一约束）。
            _upsert_business_mapping(
                db,
                user_id=_last_registered_user_id,
                business_key=BUSINESS_KEY_PRODUCT_DETAIL_IMAGE.key,
                provider_key="TEST-IMAG",
                # v2.3：业务入口必须显式带 model_name。
                model_name="detail-image-model",
            )
            db.commit()
        finally:
            db.close()

        account_id = _seed_account_for_last_user()
        _seed_shop_product(
            platform_account_id=account_id,
            item_id="ITEM-1",
            images=["https://cdn.xhs.test/main.jpg", "https://cdn.xhs.test/2.jpg"],
            title="白橘运动包",
        )

        response = client.post(
            "/api/xhs/qianfan/generate-detail-image",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-1",
                "prompt": "",
                # v2.19：参考图必选（不勾 ⇒ 400 REFERENCE_IMAGE_REQUIRED）。
                "reference_image_urls": ["https://cdn.xhs.test/main.jpg"],
                # v2.20：显式 3 张（默认即 3，这里显式写死断言更稳）。
                "count": 3,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["url"] == "https://cdn.example.test/detail.png"
        assert body["asset"]["model_name"] == "detail-image-model"

        # v2.23：仅一组（未传 group_settings ⇒ 旧契约两组都跑；这里传了只留 product）
        # ⚠️ 未传 group_settings 时两组都跑 ⇒ 3 张 × 2 组。
        assert len(fake.calls) == 6, "两组 × 每组 count=3 = 6 次上游调用"
        assert len(body["assets"]) == 6
        _model, prompt, reference_images = fake.calls[0]
        assert reference_images == ["https://cdn.xhs.test/main.jpg"]
        assert "白橘运动包" in prompt

        # 落 AIGC 素材表（发布时按它取 aigcTaskId）
        db = _test_session_factory()
        try:
            assets = db.query(AigcMediaAsset).all()
            assert len(assets) == 6, "两组 × 每组 count=3 ⇒ 落 6 条素材行"
            assert assets[0].url == "https://cdn.example.test/detail.png"
            # ⚠️ 缺值口径：服务商没回 local_path ⇒ 留空串，**不拿 url 冒充**
            assert assets[0].local_path == ""
            assert assets[0].asset_type == "image"
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_image_ai_client, None)


def test_generate_detail_image_supports_count_and_reports_partial_failure(tmp_path):
    """v2.18/v2.29：**生成张数**（用户口径「新增一个条件，生成多少张图片」）+ 半成功。

    守五件：
    ① `count=3` ⇒ 上游被调 **3 次**（v2.29：一次批量要 3 张，服务商少回 2 张 ⇒
       补张 1 次，合计 3 次 —— 请求数仍是「张数」级，不会被新批量路径吃掉）；
    ② 落 **2 条**素材行、`assets` 回 2 个 id（`url`/`asset` 仍是首张，向后兼容）；
    ③ **半成功**：v2.29 起一组是一次批量请求（原子单元），组内半成功只剩
       「服务商少回 + 补张失败」这一种形态 ⇒ 200 + `errors` 一条（不因一张失败
       丢掉另外两张）；④ 全部失败 ⇒ 502（不假装成功）。
    """
    from backend.app.api.ai import get_image_ai_client
    from backend.app.models.generation import AigcMediaAsset
    from backend.app.services.business_provider_keys import (
        BUSINESS_KEY_PRODUCT_DETAIL_IMAGE,
    )

    class FlakyImageClient:
        """v2.29 的半成功形态：服务商**少回张数**（一批 3 张只回 2 张）+ **补张失败**。

        ⚠️ v2.29 起每组是**一次**批量请求 —— 一批是一个原子单元（要么拿回 n 张，
        要么一张没有），组内不再逐张 ⇒ 旧的「第 2 张抛错」形态没有了。
        组内半成功只剩一种合法形态：批量返回**不足**（服务商忽略 ``n``）⇒
        调用方按缺张补齐 ⇒ 补张抛错。本用例守的正是这条**补张路径**：
        拿到 2 张 + 1 条 error，而不是整组 502。
        """

        def __init__(self):
            self.calls = 0

        def generate_image(self, *, model_config, api_key, prompt, reference_images=None):
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("上游限流")
            return {"url": f"https://cdn.example.test/detail-{self.calls}.png", "local_path": ""}

        def generate_images(self, *, model_config, api_key, prompt, reference_images=None, count=1):
            # 模拟「服务商忽略 n」：要 3 张只回 2 张（count=1 时仍如实回 1 张）。
            returned = max(count - 1, 1)
            return [
                self.generate_image(
                    model_config=model_config,
                    api_key=api_key,
                    prompt=prompt,
                    reference_images=reference_images,
                )
                for _ in range(returned)
            ]

    fake = FlakyImageClient()
    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("detail-image-count")
    try:
        app.dependency_overrides[get_image_ai_client] = lambda: fake
        from backend.app.core.security import encrypt_text
        from backend.app.models import LlmBusinessProvider, LlmGatewayConfig, LlmGatewayModel

        db = _test_session_factory()
        try:
            db.add(
                LlmGatewayConfig(
                    user_id=_last_registered_user_id,
                    name="test-image-count",
                    provider_key="TEST-IMGC",
                    base_url="https://api.example.test/v1",
                    access_token=encrypt_text("«redacted:sk-…»"),
                    models_json=json.dumps(["detail-image-model"]),
                    enabled=True,
                    healthy=True,
                )
            )
            db.flush()
            db.add(
                LlmGatewayModel(
                    user_id=_last_registered_user_id,
                    provider_key="TEST-IMGC",
                    model_name="detail-image-model",
                    model_type="image",
                    platform="xhs",
                    enabled=True,
                )
            )
            db.flush()
            _upsert_business_mapping(
                db,
                user_id=_last_registered_user_id,
                business_key=BUSINESS_KEY_PRODUCT_DETAIL_IMAGE.key,
                provider_key="TEST-IMGC",
                model_name="detail-image-model",
            )
            db.commit()
        finally:
            db.close()

        account_id = _seed_account_for_last_user()
        _seed_shop_product(
            platform_account_id=account_id,
            item_id="ITEM-N",
            images=["https://cdn.xhs.test/main.jpg"],
            title="多张测试商品",
        )

        response = client.post(
            "/api/xhs/qianfan/generate-detail-image",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-N",
                "prompt": "",
                "count": 3,
                "reference_image_urls": ["https://cdn.xhs.test/main.jpg"],
                # v2.23：显式只生成「原始主图」这一组（否则两组都跑）。
                "group_settings": {"product": {"count": 3}},
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        # ① 一次批量（3 张）+ 一次补张 = 3 次上游调用（补张抛错，不计成功）
        assert fake.calls == 3
        # ② 成功的两张都落库并回传；`url`/`asset` 仍是首张（向后兼容）
        assert [a["id"] for a in body["assets"]] == [body["asset"]["id"], body["assets"][1]["id"]]
        assert len(body["assets"]) == 2
        assert body["url"] == body["asset"]["url"] == "https://cdn.example.test/detail-1.png"
        # ③ 半成功如实报出（不假报「全部生成」）
        assert len(body["errors"]) == 1 and "上游限流" in body["errors"][0]

        db = _test_session_factory()
        try:
            assert db.query(AigcMediaAsset).count() == 2
        finally:
            db.close()

        # ④ 全部失败 ⇒ 502（不假装成功）
        class DeadImageClient:
            def generate_image(self, *, model_config, api_key, prompt, reference_images=None):
                raise RuntimeError("服务商挂了")

            def generate_images(self, *, model_config, api_key, prompt, reference_images=None, count=1):
                raise RuntimeError("服务商挂了")

        app.dependency_overrides[get_image_ai_client] = lambda: DeadImageClient()
        failed = client.post(
            "/api/xhs/qianfan/generate-detail-image",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-N",
                "prompt": "",
                "count": 3,
                "reference_image_urls": ["https://cdn.xhs.test/main.jpg"],
                "group_settings": {"product": {"count": 3}},
            },
        )
        assert failed.status_code == 502
        assert "服务商挂了" in failed.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_image_ai_client, None)


def test_generate_detail_image_result_is_grouped(tmp_path):
    """v2.20：**结果按组分两大块**（用户口径「生成的结果也要分主图和详情图，这是两大块」）。

    守四件：
    ① 结果带 `groups`，每组有 `group` / `group_label` / `requested` / `succeeded` / `assets`；
    ② **某组一张没成时该组仍出现在结果里**（`succeeded=0` + `assets=[]`）——
       消失了用户就不知道「我勾的那组到底跑没跑」（缺值口径：不静默丢组）；
    ③ 每张图带 `group` / `group_index`（分组是请求时的事实，不是事后按顺序猜的）；
    ④ 只勾一组时，结果里**只有那一组**（不凭空造另一组）。
    """
    from backend.app.api.ai import get_image_ai_client
    from backend.app.services.business_provider_keys import (
        BUSINESS_KEY_PRODUCT_DETAIL_IMAGE,
    )

    class HalfFailClient:
        """第 2 个组（detail）**整批**失败 —— 用来验证「某组全灭仍出现」。

        ⚠️ v2.29 起每组是**一次**批量请求（`generate_images`），组内不再逐张 ⇒
        「某组一张没成」只能 modeled 成**该组整批抛错**（一批是一个原子单元：
        要么拿回 n 张，要么一张没有），不再是「组内第 1 张抛错」。
        """

        def __init__(self):
            self.batches = 0
            self.calls = 0

        def generate_image(self, *, model_config, api_key, prompt, reference_images=None):
            self.calls += 1
            return {"url": f"https://cdn.example.test/g{self.calls}.png", "local_path": ""}

        def generate_images(self, *, model_config, api_key, prompt, reference_images=None, count=1):
            self.batches += 1
            # 组固定按 product → detail 顺序执行；第 2 批 = detail 组，整批挂掉。
            if self.batches == 2:
                raise RuntimeError("详情图整批失败")
            return [
                self.generate_image(
                    model_config=model_config,
                    api_key=api_key,
                    prompt=prompt,
                    reference_images=reference_images,
                )
                for _ in range(count)
            ]

    fake = HalfFailClient()
    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("detail-image-grouped")
    try:
        app.dependency_overrides[get_image_ai_client] = lambda: fake
        from backend.app.core.security import encrypt_text
        from backend.app.models import LlmBusinessProvider, LlmGatewayConfig, LlmGatewayModel

        db = _test_session_factory()
        try:
            db.add(
                LlmGatewayConfig(
                    user_id=_last_registered_user_id,
                    name="test-image-group",
                    provider_key="TEST-IMGG",
                    base_url="https://api.example.test/v1",
                    access_token=encrypt_text("«redacted:sk-…»"),
                    models_json=json.dumps(["detail-image-model"]),
                    enabled=True,
                    healthy=True,
                )
            )
            db.flush()
            db.add(
                LlmGatewayModel(
                    user_id=_last_registered_user_id,
                    provider_key="TEST-IMGG",
                    model_name="detail-image-model",
                    model_type="image",
                    platform="xhs",
                    enabled=True,
                )
            )
            db.flush()
            _upsert_business_mapping(
                db,
                user_id=_last_registered_user_id,
                business_key=BUSINESS_KEY_PRODUCT_DETAIL_IMAGE.key,
                provider_key="TEST-IMGG",
                model_name="detail-image-model",
            )
            db.commit()
        finally:
            db.close()

        account_id = _seed_account_for_last_user()
        _seed_shop_product(
            platform_account_id=account_id,
            item_id="ITEM-GRP",
            images=["https://cdn.xhs.test/main.jpg"],
            detail_images=["https://cdn.xhs.test/d1.jpg"],
            title="分组测试商品",
        )
        url = "/api/xhs/qianfan/generate-detail-image"

        # ④ 明确只要一组 ⇒ 结果只有一组
        only_product = client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-GRP",
                "reference_image_urls": ["https://cdn.xhs.test/main.jpg"],
                "count": 3,
                # v2.23：组的存在性由 group_settings 决定
                "group_settings": {"product": {"count": 3}},
            },
        )
        assert only_product.status_code == 200, only_product.text
        groups = only_product.json()["groups"]
        assert [g["group"] for g in groups] == ["product"], "只要求一组却造出了别的组"

        # 两组都勾 ⇒ 两组都在（详情组整批失败 ⇒ 该组 succeeded=0 但仍出现）
        both = client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-GRP",
                "reference_image_urls": [
                    "https://cdn.xhs.test/main.jpg",
                    "https://cdn.xhs.test/d1.jpg",
                ],
                "count": 3,
            },
        )
        assert both.status_code == 200, both.text
        groups = both.json()["groups"]
        # ① 结构完整
        assert [g["group"] for g in groups] == ["product", "detail"]
        for g in groups:
            assert g["group_label"] and g["requested"] == 3
            assert isinstance(g["succeeded"], int) and isinstance(g["assets"], list)
        # ③ 每张带分组标记与组内序号
        for g in groups:
            for index, asset in enumerate(g["assets"], start=1):
                assert asset["group"] == g["group"]
                assert asset["group_index"] == index
    finally:
        app.dependency_overrides.pop(get_image_ai_client, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_generate_detail_image_regenerates_single_image(tmp_path):
    """v2.21：**单张重做**（设计 17 §13.3.7.8，市场共识：Pixii Spot Edit / GreenOnion redo）。

    守四件：
    ① 指定 `regenerate={"group","index"}` ⇒ 上游**只被调 1 次**（不重跑整组）；
    ② 只送**该组**的参考图（不跨组）；
    ③ 结果的 `group_index` 是**原始序号**（重做第 2 张 ⇒ 回 2，不是 1）；
    ④ 目标无效（该组没勾 / 序号非正整数）⇒ 400 `REGENERATE_TARGET_INVALID`，
       **不静默退化成整组重跑**（那会烧掉用户没打算动的图）。
    """
    from backend.app.api.ai import get_image_ai_client
    from backend.app.services.business_provider_keys import (
        BUSINESS_KEY_PRODUCT_DETAIL_IMAGE,
    )

    class CountingClient:
        def __init__(self):
            self.calls = []

        def generate_image(self, *, model_config, api_key, prompt, reference_images=None):
            self.calls.append(list(reference_images or []))
            return {"url": f"https://cdn.example.test/r{len(self.calls)}.png", "local_path": ""}

        # v2.29：批量接口；单张重做时 count=1 ⇒ 仍然只调一次。
        def generate_images(self, *, model_config, api_key, prompt, reference_images=None, count=1):
            return [
                self.generate_image(
                    model_config=model_config,
                    api_key=api_key,
                    prompt=prompt,
                    reference_images=reference_images,
                )
                for _ in range(count)
            ]

    fake = CountingClient()
    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("detail-image-regen")
    try:
        app.dependency_overrides[get_image_ai_client] = lambda: fake
        from backend.app.core.security import encrypt_text
        from backend.app.models import LlmBusinessProvider, LlmGatewayConfig, LlmGatewayModel

        db = _test_session_factory()
        try:
            db.add(
                LlmGatewayConfig(
                    user_id=_last_registered_user_id,
                    name="test-image-regen",
                    provider_key="TEST-IMGR",
                    base_url="https://api.example.test/v1",
                    access_token=encrypt_text("«redacted:sk-…»"),
                    models_json=json.dumps(["detail-image-model"]),
                    enabled=True,
                    healthy=True,
                )
            )
            db.flush()
            db.add(
                LlmGatewayModel(
                    user_id=_last_registered_user_id,
                    provider_key="TEST-IMGR",
                    model_name="detail-image-model",
                    model_type="image",
                    platform="xhs",
                    enabled=True,
                )
            )
            db.flush()
            _upsert_business_mapping(
                db,
                user_id=_last_registered_user_id,
                business_key=BUSINESS_KEY_PRODUCT_DETAIL_IMAGE.key,
                provider_key="TEST-IMGR",
                model_name="detail-image-model",
            )
            db.commit()
        finally:
            db.close()

        account_id = _seed_account_for_last_user()
        _seed_shop_product(
            platform_account_id=account_id,
            item_id="ITEM-REGEN",
            images=["https://cdn.xhs.test/main.jpg"],
            detail_images=["https://cdn.xhs.test/d1.jpg"],
            title="重做测试商品",
        )
        url = "/api/xhs/qianfan/generate-detail-image"
        headers = {"Authorization": f"Bearer {token}"}
        payload_base = {
            "platform_account_id": account_id,
            "item_id": "ITEM-REGEN",
            "reference_image_urls": [
                "https://cdn.xhs.test/main.jpg",
                "https://cdn.xhs.test/d1.jpg",
            ],
            "count": 3,
        }

        # ① 重做「主图组第 2 张」⇒ 只调 1 次
        before = len(fake.calls)
        resp = client.post(url, headers=headers, json={**payload_base, "regenerate": {"group": "product", "index": 2}})
        assert resp.status_code == 200, resp.text
        assert len(fake.calls) - before == 1, "单张重做应只调一次上游（不重跑整组）"
        # ② v2.23：参考图是**合并组里勾选的全部**（不再按来源只送本组）
        assert fake.calls[-1] == [
            "https://cdn.xhs.test/main.jpg",
            "https://cdn.xhs.test/d1.jpg",
        ]
        body = resp.json()
        # ③ 序号是原始序号（2，不是 1）
        assert [g["group"] for g in body["groups"]] == ["product"]
        assert body["groups"][0]["succeeded"] == 1
        assert body["assets"][0]["group_index"] == 2

        # ④ 目标无效：该组**不在本次生成范围**（group_settings 里没有它）
        bad_group = client.post(
            url,
            headers=headers,
            json={
                **payload_base,
                # 只要求生成主图组，却要重做详情图 ⇒ 该组不在本次范围
                "group_settings": {"product": {"count": 3}},
                "regenerate": {"group": "detail", "index": 1},
            },
        )
        assert bad_group.status_code == 400
        assert bad_group.json()["detail"]["code"] == "REGENERATE_TARGET_INVALID"

        # ④ 目标无效：序号不是正整数
        bad_index = client.post(
            url,
            headers=headers,
            json={**payload_base, "regenerate": {"group": "product", "index": 0}},
        )
        assert bad_index.status_code == 400
        assert bad_index.json()["detail"]["code"] == "REGENERATE_TARGET_INVALID"
    finally:
        app.dependency_overrides.pop(get_image_ai_client, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_generate_detail_image_count_is_bounded(tmp_path):
    """v2.18/v2.20/v2.28.4：张数**有界** **1 ~ 10**（下限 2026-10-03 由 3 改 1）——
    0 / 11 / -1 被 422 拦下（不静默夹到合法值）；⚠️ **2 现在是合法值**
    （旧下限 3 时它在被拒之列，改下限后必须同时把它从「坏值」里拿掉，
    否则这条用例会拿旧口径把新行为判红）。

    ⚠️ `count` 每张 = 一次上游请求，无上限就是「一次点击烧 N 次额度」。
    """
    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("detail-image-count-bound")
    for bad in (0, 11, -1):
        response = client.post(
            "/api/xhs/qianfan/generate-detail-image",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "platform_account_id": 1,
                "item_id": "ITEM-X",
                "prompt": "",
                "count": bad,
                "reference_image_urls": ["https://cdn.xhs.test/main.jpg"],
            },
        )
        assert response.status_code == 422, f"count={bad} 应被拦下，实际 {response.status_code}"


def test_generate_detail_image_reports_missing_image_and_missing_mapping(tmp_path):
    """如实报缺三态（v2.19 更新）：商品不在台账 ⇒ 404；**没勾参考图** ⇒ 400
    `REFERENCE_IMAGE_REQUIRED`；台账无图可勾 ⇒ 400 `PRODUCT_IMAGE_MISSING`；
    业务点未配 ⇒ 400 带 code。

    ⚠️ v2.19 起参考图**必选**（用户口径「必须选择原始参考图才可以点击生成按钮」），
    故「有图商品但没勾」也会 400 —— 这是**刻意**的：前后端同一判据。
    """
    from backend.app.api.ai import get_image_ai_client

    class FakeImageClient:
        def generate_image(self, *, model_config, api_key, prompt, reference_images=None):
            raise AssertionError("不该被调到：这些分支都在出图之前就返回")

        # v2.29：详情图走批量接口；同样不该被调到。
        def generate_images(self, *, model_config, api_key, prompt, reference_images=None, count=1):
            raise AssertionError("不该被调到：这些分支都在出图之前就返回")

    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("detail-image-missing")
    try:
        app.dependency_overrides[get_image_ai_client] = lambda: FakeImageClient()
        account_id = _seed_account_for_last_user()
        headers = {"Authorization": f"Bearer {token}"}

        # ① 商品不在台账 ⇒ 404
        missing_item = client.post(
            "/api/xhs/qianfan/generate-detail-image",
            headers=headers,
            json={"platform_account_id": account_id, "item_id": "NOPE"},
        )
        assert missing_item.status_code == 404

        # ② 商品在、图集为空、且没勾 ⇒ REFERENCE_IMAGE_REQUIRED
        #    （**不凭空生成冒充商品图**；「没选」先于「没得选」判，两者 code 不同）
        _seed_shop_product(
            platform_account_id=account_id, item_id="ITEM-NOIMG", images=[], title="无图商品"
        )
        no_image = client.post(
            "/api/xhs/qianfan/generate-detail-image",
            headers=headers,
            json={"platform_account_id": account_id, "item_id": "ITEM-NOIMG"},
        )
        assert no_image.status_code == 400
        assert no_image.json()["detail"]["code"] == "REFERENCE_IMAGE_REQUIRED"

        # ②b 有图但**没勾**参考图 ⇒ 同样 400 REFERENCE_IMAGE_REQUIRED（v2.19 新契约）
        _seed_shop_product(
            platform_account_id=account_id,
            item_id="ITEM-NOPICK",
            images=["https://cdn.xhs.test/main.jpg"],
            title="有图未勾",
        )
        no_pick = client.post(
            "/api/xhs/qianfan/generate-detail-image",
            headers=headers,
            json={"platform_account_id": account_id, "item_id": "ITEM-NOPICK"},
        )
        assert no_pick.status_code == 400
        assert no_pick.json()["detail"]["code"] == "REFERENCE_IMAGE_REQUIRED"

        # ③ 有图 + 勾了但**该业务点解析不到可用凭据** ⇒ 400 结构化（不回落）
        #    ⚠️ 2026-10-13 契约更新（R4 服务商种子落地带来的行为变更）：
        #    注册时 `seed_default_business_map_for_user` 会给新用户播种**默认业务点映射**
        #    （设计 37），故新用户**已有** `generate_product_detail_image` 的映射 ——
        #    它指向种子服务商，而种子**不含 API Key** ⇒ 此刻的真实状态是
        #    「映射在、服务商没配 key」，不是「压根没映射」。
        #    两条路径都必须回**结构化 400 + 指向性 code**（前端据此给「去网关页配」的提示），
        #    不许落到下游被归成 502「上游出图失败」：
        #      * 无映射            ⇒ `LLM_BUSINESS_PROVIDER_NOT_CONFIGURED`
        #      * 有映射但没配 key  ⇒ `LLM_PROVIDER_KEY_MISSING`（本轮新增，修 502 误报）
        #    ⚠️ 原断言写死前者，R4 之后恒红；**不是**放宽断言 ——
        #    `LLM_PROVIDER_KEY_MISSING` 是本场景（新装用户）**唯一正确**的 code。
        unconfigured = client.post(
            "/api/xhs/qianfan/generate-detail-image",
            headers=headers,
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-NOPICK",
                "reference_image_urls": ["https://cdn.xhs.test/main.jpg"],
            },
        )
        assert unconfigured.status_code == 400
        # 真实 code 由 `llm_gateway_client` 给（不猜、不写死一组「可能的值」——
        # 写死一组等于把断言放宽到「差不多对」，那抓不住回归）。
        assert unconfigured.json()["detail"]["code"] == "LLM_PROVIDER_KEY_MISSING"
        # 文案必须带**指向性**（告诉用户去哪补配），且**不得**回落到 502 的措辞。
        assert "API Key" in unconfigured.json()["detail"]["message"]

        # ③b **无映射**时（把种子映射删掉）⇒ 仍回结构化 400 且 code 不同（两条路径分得清）
        from backend.app.models import LlmBusinessProvider as _LBP

        _db = _test_session_factory()
        try:
            _db.query(_LBP).filter(
                _LBP.user_id == _last_registered_user_id,
                _LBP.business_key == "generate_product_detail_image",
            ).delete()
            _db.commit()
        finally:
            _db.close()
        no_mapping = client.post(
            "/api/xhs/qianfan/generate-detail-image",
            headers=headers,
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-NOPICK",
                "reference_image_urls": ["https://cdn.xhs.test/main.jpg"],
            },
        )
        assert no_mapping.status_code == 400
        assert no_mapping.json()["detail"]["code"] == "LLM_BUSINESS_PROVIDER_NOT_CONFIGURED"
    finally:
        app.dependency_overrides.pop(get_image_ai_client, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_detail_image_reference_images_must_come_from_the_product(tmp_path):
    """v2.15（§13.3.6）：勾选的参考图**必须来自该商品台账**，否则 400。

    这条守的是 SSRF 防线：允许前端自由传 URL ⇒ 后端变成「下载任意地址」的跳板。
    同时验证「原商品详情图」确实能作为参考（本需求的核心）。
    """
    from backend.app.api.ai import get_image_ai_client
    from backend.app.core.security import encrypt_text
    from backend.app.models import LlmBusinessProvider, LlmGatewayConfig, LlmGatewayModel
    from backend.app.services.business_provider_keys import (
        BUSINESS_KEY_PRODUCT_DETAIL_IMAGE,
    )

    class FakeImageClient:
        def __init__(self):
            self.calls = []

        def generate_image(self, *, model_config, api_key, prompt, reference_images=None):
            self.calls.append(list(reference_images or []))
            return {"url": "https://cdn.example.test/out.png", "local_path": ""}

        # v2.29：详情图走批量接口；记录与逐张路径等价，既有断言不用改。
        def generate_images(self, *, model_config, api_key, prompt, reference_images=None, count=1):
            return [
                self.generate_image(
                    model_config=model_config,
                    api_key=api_key,
                    prompt=prompt,
                    reference_images=reference_images,
                )
                for _ in range(count)
            ]

    fake = FakeImageClient()
    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("detail-image-refs")
    try:
        app.dependency_overrides[get_image_ai_client] = lambda: fake
        db = _test_session_factory()
        try:
            db.add(
                LlmGatewayConfig(
                    user_id=_last_registered_user_id,
                    name="ref-image",
                    provider_key="REF-IMAG",
                    base_url="https://api.example.test/v1",
                    access_token=encrypt_text("«redacted:sk-…»"),
                    models_json=json.dumps(["ref-model"]),
                    enabled=True,
                    healthy=True,
                )
            )
            db.flush()
            db.add(
                LlmGatewayModel(
                    user_id=_last_registered_user_id,
                    provider_key="REF-IMAG",
                    model_name="ref-model",
                    model_type="image",
                    platform="xhs",
                    enabled=True,
                )
            )
            db.flush()
            _upsert_business_mapping(
                db,
                user_id=_last_registered_user_id,
                business_key=BUSINESS_KEY_PRODUCT_DETAIL_IMAGE.key,
                provider_key="REF-IMAG",
                model_name="ref-model",
            )
            db.commit()
        finally:
            db.close()

        account_id = _seed_account_for_last_user()
        _seed_shop_product(
            platform_account_id=account_id,
            item_id="ITEM-REF",
            images=["https://cdn.xhs.test/main.jpg"],
            detail_images=[
                "https://cdn.xhs.test/d1.jpg",
                "https://cdn.xhs.test/d2.jpg",
            ],
            title="带详情图的商品",
        )
        headers = {"Authorization": f"Bearer {token}"}
        url = "/api/xhs/qianfan/generate-detail-image"

        # ① 勾**原商品详情图** ⇒ 用它当参考（本需求核心：详情图可用）
        resp = client.post(
            url,
            headers=headers,
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-REF",
                "reference_image_urls": ["https://cdn.xhs.test/d2.jpg"],
                # v2.23：显式只生成「原始详情图」这一组。
                "group_settings": {"detail": {"count": 3}},
            },
        )
        assert resp.status_code == 200, resp.text
        # 参考图**原样送出**（v2.23：主图与详情图合并为一组「可选参考图」，
        # 不再按来源分别送 ⇒ 勾了什么就送什么）。
        assert fake.calls[-1] == ["https://cdn.xhs.test/d2.jpg"]
        # 结果只有被要求的那一组
        assert [g["group"] for g in resp.json()["groups"]] == ["detail"]

        # ② 主图 + 详情图混勾 ⇒ 两组各 count 张，**参考图是同一份勾选**（v2.23 合并为一组）
        before = len(fake.calls)
        resp2 = client.post(
            url,
            headers=headers,
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-REF",
                "reference_image_urls": [
                    "https://cdn.xhs.test/main.jpg",
                    "https://cdn.xhs.test/d1.jpg",
                ],
            },
        )
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()
        # 未传 group_settings ⇒ 两组都跑（兼容）
        assert [g["group"] for g in body2["groups"]] == ["product", "detail"]
        # ⚠️ v2.23：**两张参考图一起送**（合并组的语义 —— 勾了的一并扔给模型）
        assert fake.calls[-1] == [
            "https://cdn.xhs.test/main.jpg",
            "https://cdn.xhs.test/d1.jpg",
        ]
        assert len(fake.calls) - before == 6, "两组 × 每组 3 张 = 6 次上游调用"
        assert [g["succeeded"] for g in body2["groups"]] == [3, 3]

        # ③ 传**台账里没有的 URL** ⇒ 400（SSRF 防线：不接受任意地址）
        outside = client.post(
            url,
            headers=headers,
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-REF",
                "reference_image_urls": ["https://evil.example/steal.jpg"],
            },
        )
        assert outside.status_code == 400, outside.text
        assert outside.json()["detail"]["code"] == "REFERENCE_IMAGE_NOT_FOUND"

        # ④ 不勾 ⇒ 400 REFERENCE_IMAGE_REQUIRED
        #    ⚠️ v2.19 **取代**旧的「不勾则回落商品首图」：用户口径「必须选择原始参考图
        #    才可以点击生成按钮」。旧行为让「没选」与「选了」产出同样结果，
        #    用户无法通过对结果的控制（只能事后换图重试）。
        resp4 = client.post(
            url,
            headers=headers,
            json={"platform_account_id": account_id, "item_id": "ITEM-REF"},
        )
        assert resp4.status_code == 400, resp4.text
        assert resp4.json()["detail"]["code"] == "REFERENCE_IMAGE_REQUIRED"
    finally:
        app.dependency_overrides.pop(get_image_ai_client, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_tasks_api_requires_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        response = client.get("/api/tasks?platform=xhs")

        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_tasks_api_lists_only_current_user_tasks_and_filters_platform(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.models import Task

    db_dependency = _override_database(tmp_path)
    owner_token = _register_and_get_access_token("task-owner")
    intruder_token = _register_and_get_access_token("task-intruder")
    try:
        db = next(app.dependency_overrides[get_db]())
        try:
            db.add_all(
                [
                    Task(
                        user_id=1,
                        platform="xhs",
                        task_type="ai_rewrite",
                        status="completed",
                        progress=100,
                        payload={"draft_id": 11},
                    ),
                    Task(
                        user_id=1,
                        platform="douyin",
                        task_type="crawl",
                        status="pending",
                        progress=0,
                        payload={"keyword": "demo"},
                    ),
                    Task(
                        user_id=2,
                        platform="xhs",
                        task_type="publish",
                        status="failed",
                        progress=100,
                        payload={"error": "nope"},
                    ),
                ]
            )
            db.commit()
        finally:
            db.close()

        owner_response = client.get(
            "/api/tasks?platform=xhs",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert owner_response.status_code == 200
        owner_payload = owner_response.json()
        assert owner_payload["total"] == 1
        assert owner_payload["items"][0]["platform"] == "xhs"
        assert owner_payload["items"][0]["task_type"] == "ai_rewrite"
        assert owner_payload["items"][0]["payload"] == {"draft_id": 11}

        intruder_response = client.get(
            "/api/tasks?platform=xhs",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_response.status_code == 200
        assert intruder_response.json()["total"] == 1
        assert intruder_response.json()["items"][0]["task_type"] == "publish"
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_scheduler_status_requires_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        # 2026-09-24（设计 15 §8）：旧 /api/tasks/scheduler/status 已删除，
        # 由 /api/schedule/status 取代
        response = client.get("/api/schedule/status")

        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_schedule_status_reports_scheduler_state_and_counts(tmp_path):
    """新状态端点：enabled/running/时区/jobs/stats（设计 15 §8）。

    DEF-066：计数按**当前用户**收口（原全库口径已废）；且注册即播种 ⇒ 用户的 7 个
    内置行也在计数里（原全局行 user_id IS NULL 不再存在）。
    """
    from backend.app.core.database import get_db
    from backend.app.models import ScheduledJob, User

    db_dependency = _override_database(tmp_path)
    owner_token = _register_and_get_access_token("schedule-status-owner")
    try:
        db = next(app.dependency_overrides[get_db]())
        try:
            db.add_all(
                [
                    ScheduledJob(
                        user_id=1, name="巡检",
                        job_key="system.cookie_health_check", trigger_type="cron",
                        cron_expression="0 0 */2 * * *", status="active", is_builtin=True,
                    ),
                    ScheduledJob(
                        user_id=1, name="我的",
                        job_key="auto_ops.keyword_pipeline", trigger_type="cron",
                        cron_expression="0 0 9 * * *", status="paused",
                        params={"auto_task_id": 1},
                    ),
                ]
            )
            db.commit()
        finally:
            db.close()

        response = client.get(
            "/api/schedule/status",
            headers={"Authorization": f"Bearer {owner_token}"},
        )

        assert response.status_code == 200
        payload = response.json()
        # R24（2026-09-24 用户口径）：安装版**默认启用**调度器 ⇒ `config/default.yaml`
        # 的 `scheduler.enabled: true`（前身断言是 `is False`）。这里读的是**配置值**；
        # 测试不起 lifespan，所以进程内的调度器仍未运行（`running` 依旧 False）。
        assert payload["enabled"] is True
        assert payload["running"] is False
        assert payload["timezone"] == "Asia/Shanghai"
        assert payload["jobs"] == []
        # 播种 13 行（全 active）+ 本测试 2 行（1 active + 1 paused）⇒
        # total 15 / active 14 / paused 1。
        # 2026-10-07 两轮缩减：① 工作流退场（1e2e5ac）少一项 `workflow.<id>`；
        # ② `system.no_traffic_cleanup` 从注册表与种子移除（用户口径「无流量笔记
        # 清理任务不应在任务列表展示」）⇒ total 16 → 15、active 15 → 14
        #（原 active 15 = 14 播种全 active + 1 测试行；现 14 = 13 + 1）。
        # 2026-10-11 §1.9：新增 `system.picture_space_cleanup`（**默认 paused** ——
        # 平台写操作，用户确认参数后再启用）⇒ total 16 → 17、paused 1 → 2
        #（active 不变：新任务不进 active）。
        # ⚠️ 2026-10-13 指标快照子系统整块移除：`system.note_metrics_collect` /
        # `system.shop_metrics_collect` 两条种子退场（均 active）⇒ total 17 → 15、
        # active 15 → 13；同轮再算：本测试自播 2 行（1 active + 1 paused）+
        # 内置 12 行（11 active + 1 paused `picture_space_cleanup`）
        # ⇒ total 14 / active 12 / paused 2。
        assert payload["stats"]["total"] == 14
        assert payload["stats"]["active"] == 12
        assert payload["stats"]["paused"] == 2
        assert payload["stats"]["completed"] == 0
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_tasks_detail_enforces_ownership(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.models import Task

    db_dependency = _override_database(tmp_path)
    owner_token = _register_and_get_access_token("task-detail-owner")
    intruder_token = _register_and_get_access_token("task-detail-intruder")
    try:
        db = next(app.dependency_overrides[get_db]())
        try:
            task = Task(
                user_id=1,
                platform="xhs",
                task_type="ai_rewrite",
                status="completed",
                progress=100,
                payload={"draft_id": 22},
            )
            db.add(task)
            db.commit()
            task_id = task.id
        finally:
            db.close()

        owner_response = client.get(
            f"/api/tasks/{task_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert owner_response.status_code == 200
        assert owner_response.json()["id"] == task_id
        assert owner_response.json()["payload"] == {"draft_id": 22}

        intruder_response = client.get(
            f"/api/tasks/{task_id}",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_task_execution_fields_and_retry_with_exhausted_status(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.core.time import shanghai_now
    from backend.app.models import Task

    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("task-fields-user")
    try:
        db = next(app.dependency_overrides[get_db]())
        try:
            now = shanghai_now()
            parent = Task(
                user_id=1, platform="xhs", task_type="monitoring_crawl",
                status="completed", progress=100,
                started_at=now, finished_at=now,
            )
            child = Task(
                user_id=1, platform="xhs", task_type="note_crawl",
                status="failed", progress=0, error_type="network",
                retry_count=2, max_retries=3,
                started_at=now, finished_at=now,
            )
            db.add(parent)
            db.flush()
            child.parent_task_id = parent.id
            db.add(child)
            db.commit()
            parent_id, child_id = parent.id, child.id
        finally:
            db.close()

        detail = client.get(f"/api/tasks/{parent_id}", headers={"Authorization": f"Bearer {token}"})
        assert detail.status_code == 200
        body = detail.json()
        assert body["started_at"] is not None
        assert body["finished_at"] is not None
        assert body["duration_ms"] is not None
        assert body["duration_ms"] >= 0
        assert len(body["children"]) == 1
        assert body["children"][0]["id"] == child_id
        assert body["children"][0]["error_type"] == "network"

        child_detail = client.get(f"/api/tasks/{child_id}", headers={"Authorization": f"Bearer {token}"})
        assert child_detail.json()["parent_task_id"] == parent_id
        assert child_detail.json()["retry_count"] == 2
        assert child_detail.json()["max_retries"] == 3

        retry = client.post(f"/api/tasks/{child_id}/retry", headers={"Authorization": f"Bearer {token}"})
        assert retry.status_code == 200
        assert retry.json()["status"] == "pending"
        assert retry.json()["retry_count"] == 3
        assert retry.json()["error_type"] is None

        db2 = next(app.dependency_overrides[get_db]())
        try:
            t = db2.get(Task, child_id)
            t.status = "exhausted"
            t.error_type = "network"
            db2.commit()
        finally:
            db2.close()

        exhausted_retry = client.post(f"/api/tasks/{child_id}/retry", headers={"Authorization": f"Bearer {token}"})
        assert exhausted_retry.status_code == 200
        assert exhausted_retry.json()["status"] == "pending"
    finally:
        app.dependency_overrides.pop(db_dependency, None)
    db_dependency = _override_database(tmp_path)
    try:
        response = client.post("/api/drafts/1/send-to-publish", json={"platform_account_id": 1})

        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_manual_clean_endpoints_respect_cleanable_statuses(tmp_path):
    """手动清理（2026-09-26 用户口径「新增手动批量清理……清理逻辑不包含待重试和
    进行中状态的记录，前端不允许被选中」）：

    1. `POST /tasks/clean`（显式 ids）：只删 `done` / `cancelled`；待重试
       （`failed` / `exhausted`）与进行中（`pending` / `running`）**一律不删**
       （`skipped` 计数，后端第二道闸 —— 前端禁用复选框只是体验）；
    2. `POST /tasks/clean-all`：删本用户全部可清记录，别家的一行不动（DEF-066）；
    3. 重试（`POST /tasks/{id}/retry`）把来源标成 `retry`（按钮级来源口径）。
    """
    from backend.app.core.database import get_db
    from backend.app.core.time import shanghai_now
    from backend.app.models import Task

    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("task-clean-user")
    try:
        db = next(app.dependency_overrides[get_db]())
        try:
            now = shanghai_now()
            # ⚠️ **用注册出来的真实 user_id，不要写死 1**（2026-10-04）：
            # 全局 FK=ON 后 `tasks.user_id -> users.id` 生效，而夹具库里
            # 用户的 id 是自增的 —— 写死 1 会在「1 被别人占了」时撞外键
            # （实测报 `FOREIGN KEY constraint failed`，参数里是 user_id=2）。
            # 本文件已有 `_last_registered_user_id` 这一既有惯例，直接复用。
            owner_id = _last_registered_user_id
            # ⚠️ 注册出来的 id 未必是 1（库里可能已有行）⇒ **按库里实际存在的行**兜底：
            # 若该 id 在 users 里不存在，就地补建（FK=ON 的硬要求，2026-10-04）。
            from backend.app.models import User as _User

            if db.get(_User, owner_id) is None:
                db.add(_User(id=owner_id, username=f"clean-owner-{owner_id}", password_hash="x"))
                db.flush()
            # 本用例还要一个「别家」的用户（clean-all 不该动它的行，DEF-066）。
            _other = 99
            if db.get(_User, _other) is None:
                db.add(_User(id=_other, username="clean-other-99", password_hash="x"))
                db.flush()

            def _row(task_type: str, status: str, **payload) -> Task:
                row = Task(
                    user_id=owner_id, platform="xhs", task_type=task_type, status=status,
                    progress=100 if status == "done" else 0,
                    payload={"source": "note_sync_single", **payload},
                    started_at=now, finished_at=now,
                )
                db.add(row)
                return row

            done_row = _row("xhs_note_sync_item", "done", note_pk=1, ok=True)
            cancelled_row = _row("xhs_note_sync_item", "cancelled", note_pk=2)
            failed_row = _row("xhs_note_sync_item", "failed", note_pk=3, ok=False, reason="平台限流")
            exhausted_row = _row("xhs_note_sync_item", "exhausted", note_pk=4, ok=False)
            pending_row = _row("xhs_note_sync_item", "pending", note_pk=5)
            running_row = _row("xhs_note_sync_item", "running", note_pk=6)
            db.commit()
            ids = {
                "done": done_row.id,
                "cancelled": cancelled_row.id,
                "failed": failed_row.id,
                "exhausted": exhausted_row.id,
                "pending": pending_row.id,
                "running": running_row.id,
            }
        finally:
            db.close()

        headers = {"Authorization": f"Bearer {token}"}

        # 1) 显式 ids：把六种状态一起点一遍，只有 done / cancelled 被清
        mixed = client.post(
            "/api/tasks/clean",
            json={"task_ids": list(ids.values())},
            headers=headers,
        )
        assert mixed.status_code == 200, mixed.text
        body = mixed.json()
        assert body["cleaned"] == 2 and body["skipped"] == 4 and body["requested"] == 6, (
            "只有已完成 / 已取消被清；待重试与进行中被跳过（不删）"
        )
        assert body["items"] == {"cleaned": 2, "skipped": 4}, "明细维度：清 2 跳 4（分维度报账）"
        assert body["batches"] == {"cleaned": 0, "blocked": 0}, (
            "点名的是明细行 ⇒ 批次维度不参与（两个维度各报各的）"
        )
        assert body["blocked"] == [], "没有批次被挡下"
        db2 = next(app.dependency_overrides[get_db]())
        try:
            for key in ("failed", "exhausted", "pending", "running"):
                assert db2.get(Task, ids[key]) is not None, f"{key} 行不许被清掉"
            assert db2.get(Task, ids["done"]) is None
            assert db2.get(Task, ids["cancelled"]) is None
        finally:
            db2.close()

        # 2) 一键清除全部：剩下的可清记录（此时只剩不可清的）⇒ cleaned = 0
        all_resp = client.post("/api/tasks/clean-all", headers=headers)
        assert all_resp.status_code == 200, all_resp.text
        assert all_resp.json()["cleaned"] == 0, "待重试 / 进行中不在可清范围"

        # 造一条新的 done + 别家的一条 done ⇒ 一键清只清本用户
        db3 = next(app.dependency_overrides[get_db]())
        try:
            # ⚠️ 这两个 id 必须**真实存在于 users**（2026-10-04）：全局 FK=ON 后
            # `tasks.user_id -> users.id` 生效。用固定 1/2 会在「库里没有该行」时
            # 撞外键（实测 `FOREIGN KEY constraint failed`）。
            from backend.app.models import User as _U2

            for _uid in (1, 2):
                if db3.get(_U2, _uid) is None:
                    db3.add(_U2(id=_uid, username=f"clean-tid-{_uid}", password_hash="x"))
            db3.flush()
            mine = Task(
                user_id=1, platform="xhs", task_type="xhs_note_sync_item", status="done",
                progress=100, payload={"source": "note_sync_single"}, started_at=now, finished_at=now,
            )
            db3.add(mine)
            db3.flush()
            theirs = Task(
                user_id=2, platform="xhs", task_type="xhs_note_sync_item", status="done",
                progress=100, payload={"source": "note_sync_single"}, started_at=now, finished_at=now,
            )
            db3.add(theirs)
            db3.commit()
            mine_id, theirs_id = mine.id, theirs.id
        finally:
            db3.close()
        all_resp2 = client.post("/api/tasks/clean-all", headers=headers)
        assert all_resp2.json()["cleaned"] == 1, "只清本用户的可清记录（DEF-066）"
        db4 = next(app.dependency_overrides[get_db]())
        try:
            assert db4.get(Task, mine_id) is None
            assert db4.get(Task, theirs_id) is not None, "别家的记录物理上够不着"
        finally:
            db4.close()

        # 3) 重试标 retry 来源（常驻化后一行 = 一个队列项，直接复位本行）
        retry = client.post(f"/api/tasks/{ids['failed']}/retry", headers=headers)
        assert retry.status_code == 200
        assert retry.json()["status"] == "pending"
        db5 = next(app.dependency_overrides[get_db]())
        try:
            row = db5.get(Task, ids["failed"])
            assert row.payload["source"] == "retry", "这一跑来自重试（按钮级来源）"
            assert row.retry_count == 1
        finally:
            db5.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_batch_clean_is_atomic_and_blocked_by_unfinished_items(tmp_path):
    """批次维度清理（2026-09-26 用户口径「支持按照批次清理记录，批次中存在正在执行或
    待执行得记录，则不支持该批次全部清理……清理功能分为两个维度，批次维度和批次内的
    操作」）：

    1. 批内有 `running` / `pending`（**正在执行 / 待执行**）⇒ **整批不清理**：批次行
       与子行一条不少，`blocked` 里报出原因（`active` 计数）；
    2. 批内只有 `failed`（待重试）⇒ 同样不支持整批（删了用户失去重试入口），原因走
       `retryable`（与 `active` 分开报，别混成一坨）；
    3. 批内全是 `done` / `cancelled` ⇒ **整批一起删**（批次行 + 全部子行，不留孤儿）；
    4. **批次内维度**：批内有待重试篇时，点名明细行仍能单独清掉已成功的那条，批次与
       待重试的那条都留着（用户还能点重试）；
    5. **一键清除不拆散**被挡下的批次：它那些可清的明细也不清（不把在用的批次清成残的）。
    """
    from backend.app.core.database import get_db
    from backend.app.core.time import shanghai_now
    from backend.app.models import Task

    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("batch-clean-user")
    try:
        db = next(app.dependency_overrides[get_db]())
        try:
            now = shanghai_now()
            # ⚠️ 下面的 `user_id=1` 必须真实存在于 users（2026-10-04）：
            # 全局 FK=ON 后 `tasks.user_id -> users.id` 生效。
            from backend.app.models import User as _U3

            if db.get(_U3, 1) is None:
                db.add(_U3(id=1, username="batch-clean-owner", password_hash="x"))
                db.flush()

            def _batch(batch_no: str) -> Task:
                row = Task(
                    user_id=1, platform="xhs", task_type="xhs_note_sync_batch", status="pending",
                    progress=0, batch_no=batch_no,
                    payload={"source": "note_sync_batch", "queue_key": "note_sync"},
                    created_at=now,
                )
                db.add(row)
                db.flush()
                return row

            def _child(batch: Task, status: str, pk: int, **payload) -> Task:
                row = Task(
                    user_id=1, platform="xhs", task_type="xhs_note_sync_item", status=status,
                    progress=100 if status == "done" else 0,
                    payload={"source": "note_sync_batch", "note_pk": pk, **payload},
                    parent_task_id=batch.id, created_at=now,
                    started_at=now if status != "pending" else None,
                    finished_at=now if status in ("done", "failed", "cancelled") else None,
                )
                db.add(row)
                db.flush()
                return row

            active_batch = _batch("NB20260926150000-AAAA")
            active_done = _child(active_batch, "done", 1, ok=True)
            _child(active_batch, "running", 2)
            _child(active_batch, "pending", 3)

            retry_batch = _batch("NB20260926150001-BBBB")
            retry_done = _child(retry_batch, "done", 4, ok=True)
            retry_failed = _child(retry_batch, "failed", 5, ok=False, reason="平台限流")

            ready_batch = _batch("NB20260926150002-CCCC")
            ready_done = _child(ready_batch, "done", 6, ok=True)
            ready_cancelled = _child(ready_batch, "cancelled", 7)
            db.commit()
            ids = {
                "active_batch": active_batch.id,
                "active_done": active_done.id,
                "retry_batch": retry_batch.id,
                "retry_done": retry_done.id,
                "retry_failed": retry_failed.id,
                "ready_batch": ready_batch.id,
                "ready_done": ready_done.id,
                "ready_cancelled": ready_cancelled.id,
            }
        finally:
            db.close()

        headers = {"Authorization": f"Bearer {token}"}

        def _rows(*keys: str) -> dict:
            session = next(app.dependency_overrides[get_db]())
            try:
                return {key: session.get(Task, ids[key]) for key in keys}
            finally:
                session.close()

        # 1) 批内有正在执行 / 待执行 ⇒ 整批不清理（用户原话「不支持该批次全部清理」）
        blocked = client.post(
            "/api/tasks/clean", json={"task_ids": [ids["active_batch"]]}, headers=headers
        )
        assert blocked.status_code == 200, blocked.text
        body = blocked.json()
        assert body["cleaned"] == 0 and body["skipped"] == 1, "整批被挡 ⇒ 一条都不清"
        assert body["batches"] == {"cleaned": 0, "blocked": 1}, "批次维度：被挡 1 个"
        assert body["blocked"] == [
            {
                "id": ids["active_batch"],
                "batch_no": "NB20260926150000-AAAA",
                "active": 2,
                "retryable": 0,
            }
        ], "被挡下的批次必须报出原因（2 条正在执行 / 待执行）"
        kept_active = _rows("active_batch", "active_done")
        assert all(row is not None for row in kept_active.values()), (
            "整批被挡 ⇒ 批次行与它的明细一条不少（原子动作，不做半清）"
        )

        # 2) 批内只有待重试（failed）⇒ 同样不支持整批（删了用户失去重试入口）
        retry_blocked = client.post(
            "/api/tasks/clean", json={"task_ids": [ids["retry_batch"]]}, headers=headers
        )
        assert retry_blocked.status_code == 200
        assert retry_blocked.json()["blocked"] == [
            {
                "id": ids["retry_batch"],
                "batch_no": "NB20260926150001-BBBB",
                "active": 0,
                "retryable": 1,
            }
        ], "待重试的明细也让整批不可清（原因与 active 分开报）"

        # 3) **批次内维度**：点名明细行 ⇒ 清掉已成功的那条，批次与待重试的那条都留着
        one = client.post(
            "/api/tasks/clean", json={"task_ids": [ids["retry_done"]]}, headers=headers
        )
        assert one.status_code == 200
        assert one.json()["items"] == {"cleaned": 1, "skipped": 0}, "明细维度：清 1"
        assert one.json()["batches"] == {"cleaned": 0, "blocked": 0}, "没点名批次 ⇒ 批次维度不参与"
        after = _rows("retry_batch", "retry_done", "retry_failed")
        assert after["retry_done"] is None, "点名的明细被清掉"
        assert after["retry_batch"] is not None and after["retry_failed"] is not None, (
            "批次与待重试的明细留着（用户还能点重试）"
        )

        # 4) 批内全是已完成 / 已取消 ⇒ 整批（批次 + 全部明细）一次删
        ready = client.post(
            "/api/tasks/clean", json={"task_ids": [ids["ready_batch"]]}, headers=headers
        )
        assert ready.status_code == 200
        assert ready.json()["batches"] == {"cleaned": 1, "blocked": 0}
        assert ready.json()["cleaned"] == 1, "只点了批次行 ⇒ 整批算一次清理"
        gone = _rows("ready_batch", "ready_done", "ready_cancelled")
        assert all(row is None for row in gone.values()), "批次行与它的明细一起消失（不留孤儿）"

        # 5) 一键清除：被挡下的批次**连它可清的明细也不拆**
        all_resp = client.post("/api/tasks/clean-all", headers=headers)
        assert all_resp.status_code == 200
        payload = all_resp.json()
        assert payload["cleaned"] == 0, "剩下的都在被挡下的批次里 ⇒ 连可清明细也不清"
        assert payload["batches"]["blocked"] == 2, "两个批次被挡（有活 / 待重试）"
        kept = _rows("active_batch", "active_done", "retry_batch", "retry_failed")
        assert all(row is not None for row in kept.values()), "被挡批次一条明细都不许少"
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_queue_record_tree_groups_batches_and_queries_by_batch_no(tmp_path):
    """`GET /tasks?tree=1`（2026-09-26 用户口径「批次+详情父子树风格展示」+
    「新增列表支持使用唯一批次号查询」）：

    1. 顶级行 = **只有批次行**（2026-09-26 用户口径「不做兼容逻辑，数据已清空」：
       无父的队列项行属于已清掉的存量形态，**不再**出现在树里），批次行内嵌
       `children` 与按子行现算的 `summary`（状态 / 计数 / 耗时）；
    2. `batch_no` 过滤只回批次号匹配的批次行（大小写不敏感、支持只给尾段）；
    3. 默认形状（`tree` 不传）**一点没变** —— 平铺 + 分页，不许把老调用方带崩；
    4. 批次行**不是可执行任务**：取消 / 重试它明确 400（不静默改父行造成父子树自相矛盾）；
    5. 批次行另派生 `accounts`（子行 `payload.account_id` 去重 + FIFO 保序）——
       「账号」列取它（2026-09-26 用户口径「批次记录执行的账号」）；明细行不带该键
       （它的账号在自己 `payload.account_id` 里）。
    """
    from backend.app.core.database import get_db
    from backend.app.core.time import shanghai_now
    from backend.app.models import Task

    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("queue-tree-user")
    try:
        db = next(app.dependency_overrides[get_db]())
        try:
            now = shanghai_now()
            batch = Task(
                user_id=1, platform="xhs", task_type="xhs_note_sync_batch", status="pending",
                progress=0, payload={"source": "note_sync_batch", "queue_key": "note_sync"},
                batch_no="NB20260926120000-ABCD", created_at=now,
            )
            db.add(batch)
            db.flush()
            done_child = Task(
                user_id=1, platform="xhs", task_type="xhs_note_sync_item", status="done",
                progress=100, payload={"note_pk": 1, "note_title": "甲", "ok": True, "source": "note_sync_batch", "account_id": 7},
                parent_task_id=batch.id, created_at=now, started_at=now, finished_at=now,
            )
            failed_child = Task(
                user_id=1, platform="xhs", task_type="xhs_note_sync_item", status="failed",
                progress=0, payload={"note_pk": 2, "note_title": "乙", "ok": False, "reason": "平台限流", "source": "note_sync_batch", "account_id": 9},
                parent_task_id=batch.id, created_at=now, started_at=now, finished_at=now,
            )
            orphan = Task(
                user_id=1, platform="xhs", task_type="xhs_note_publish_item", status="done",
                progress=100, payload={"note_pk": 3, "note_title": "丙", "ok": True, "source": "manual"},
                created_at=now, started_at=now, finished_at=now,
            )
            db.add_all([done_child, failed_child, orphan])
            db.commit()
            batch_id, done_id, failed_id, orphan_id = batch.id, done_child.id, failed_child.id, orphan.id
        finally:
            db.close()

        headers = {"Authorization": f"Bearer {token}"}

        # 1) 树模式：顶级**只有批次行**（无父的队列项行不做兼容 ⇒ 不出现）；
        #    批次行带 children 与按子行现算的 summary
        tree = client.get("/api/tasks?tree=1", headers=headers)
        assert tree.status_code == 200, tree.text
        body = tree.json()
        by_id = {row["id"]: row for row in body["items"]}
        assert set(by_id) == {batch_id}, (
            "顶级只给批次行：数据已清空后不再兼容「无父的队列项行」"
            "（2026-09-26 用户口径「不做兼容逻辑，数据已清空」）"
        )
        batch_node = by_id[batch_id]
        assert batch_node["batch_no"] == "NB20260926120000-ABCD"
        assert [child["id"] for child in batch_node["children"]] == [done_id, failed_id], "子行按 FIFO 序"
        assert batch_node["summary"]["total"] == 2
        assert batch_node["summary"]["done"] == 1 and batch_node["summary"]["failed"] == 1
        assert batch_node["status"] == "failed", "派生状态：有失败子行 ⇒ 批次报失败"
        assert batch_node["duration_ms"] is not None, "子行都终态 ⇒ 整批耗时给得出"
        assert batch_node["summary"]["progress"] == 100
        assert batch_node["created_at"], "批次自己的创建时间必须保留（列表「批次创建时间」列取它）"
        # 账号（2026-09-26 用户口径「批次记录执行的账号」）：批次级账号**由子行派生**
        # （`batch_account_ids`，去重 + FIFO 保序）—— 跨账号批量必须两个都报出来，
        # 只报第一个就是把「跨账号」说成「单账号」（明细里明明有 2 个）。
        assert batch_node["accounts"] == [7, 9], (
            "批次行的账号 = 子行 payload.account_id 去重保序（跨账号批量两个都要在）"
        )
        assert batch_node["children"][0]["payload"]["account_id"] == 7, (
            "明细行的账号在它自己的 payload 里（入队快照）"
        )
        assert "accounts" not in batch_node["children"][0], (
            "明细行不带批次级的 accounts（那是批次行的派生字段，明细看自己 payload.account_id）"
        )
        assert orphan_id not in by_id, (
            "无父的队列项行既不进树、也不当顶级行（它没有批次号，列表顶级行只有批次行）"
        )

        # 2) 批次号查询：大小写不敏感 + 支持只给尾段（用户手抄容易只记得后四位）
        exact = client.get("/api/tasks?tree=1&batch_no=NB20260926120000-ABCD", headers=headers)
        assert [row["id"] for row in exact.json()["items"]] == [batch_id]
        lower_tail = client.get("/api/tasks?tree=1&batch_no=abcd", headers=headers)
        assert [row["id"] for row in lower_tail.json()["items"]] == [batch_id], "小写尾段也能查到"
        miss = client.get("/api/tasks?tree=1&batch_no=ZZZZ", headers=headers)
        assert miss.json()["items"] == [], "查不到就空列表（不许回全部）"

        # 3) 默认形状不变：平铺 + 分页，四行都在（含子行），没有 children / summary
        flat = client.get("/api/tasks", headers=headers)
        assert flat.status_code == 200
        flat_ids = {row["id"] for row in flat.json()["items"]}
        assert flat_ids == {batch_id, done_id, failed_id, orphan_id}, "平铺模式仍是全部行（含子行）"
        assert all("children" not in row and "summary" not in row for row in flat.json()["items"])

        # 4) 批次行不可取消 / 不可重试：明确 400（静默改父行 = 父子树自相矛盾）
        cancel_batch = client.post(f"/api/tasks/{batch_id}/cancel", headers=headers)
        assert cancel_batch.status_code == 400, cancel_batch.text
        assert cancel_batch.json()["detail"]["code"] == "BATCH_ROW_NOT_OPERABLE"
        retry_batch = client.post(f"/api/tasks/{batch_id}/retry", headers=headers)
        assert retry_batch.status_code == 400, retry_batch.text
        assert retry_batch.json()["detail"]["code"] == "BATCH_ROW_NOT_OPERABLE"

        # 明细行仍可正常重试，且**批次的派生状态跟着回到「没跑完」**（父行被刷新）
        retry_child = client.post(f"/api/tasks/{failed_id}/retry", headers=headers)
        assert retry_child.status_code == 200, retry_child.text
        assert retry_child.json()["status"] == "pending"
        after = client.get("/api/tasks?tree=1&batch_no=ABCD", headers=headers).json()["items"][0]
        assert after["status"] == "pending", "重试后批次不再报失败（有未终态子行）"
        assert after["duration_ms"] is None, "没跑完的整批没有耗时（缺值口径，不是 0）"
        assert after["summary"]["pending"] == 1 and after["summary"]["done"] == 1
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_notifications_crud_and_trigger_helpers(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.models import Notification, Task
    from backend.app.services.notification_service import notify_task_failed, notify_task_exhausted

    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("notif-user")
    other_token = _register_and_get_access_token("notif-other")
    try:
        db = next(app.dependency_overrides[get_db]())
        try:
            task = Task(user_id=1, platform="xhs", task_type="ai_rewrite", status="failed", error_type="network")
            db.add(task)
            db.flush()
            n1 = notify_task_failed(db, task)
            task.status = "exhausted"
            n2 = notify_task_exhausted(db, task)
            other_n = Notification(user_id=2, title="other", level="info")
            db.add(other_n)
            db.commit()
        finally:
            db.close()

        unauth = client.get("/api/notifications")
        assert unauth.status_code == 401

        listing = client.get("/api/notifications", headers={"Authorization": f"Bearer {token}"})
        assert listing.status_code == 200
        items = listing.json()["items"]
        assert len(items) == 2
        levels = {items[0]["level"], items[1]["level"]}
        assert levels == {"warning", "error"}

        unread_only = client.get("/api/notifications?unread=true", headers={"Authorization": f"Bearer {token}"})
        assert len(unread_only.json()["items"]) == 2

        mark = client.post(f"/api/notifications/{items[0]['id']}/read", headers={"Authorization": f"Bearer {token}"})
        assert mark.status_code == 200
        assert mark.json()["read"] is True

        unread_after = client.get("/api/notifications?unread=true", headers={"Authorization": f"Bearer {token}"})
        assert len(unread_after.json()["items"]) == 1

        mark_all = client.post("/api/notifications/read-all", headers={"Authorization": f"Bearer {token}"})
        assert mark_all.status_code == 200
        assert mark_all.json()["marked"] == 1

        other_listing = client.get("/api/notifications", headers={"Authorization": f"Bearer {other_token}"})
        assert len(other_listing.json()["items"]) == 1

        cross_mark = client.post(f"/api/notifications/{items[0]['id']}/read", headers={"Authorization": f"Bearer {other_token}"})
        assert cross_mark.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_draft_send_to_publish_creates_job_and_enforces_ownership(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.models import PublishJob

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "publish-owner")
    intruder_token = _register_and_get_access_token("publish-intruder")
    try:
        draft_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "title": "Ready title", "body": "Ready body"},
        )
        draft_id = draft_response.json()["id"]

        create_response = client.post(
            f"/api/drafts/{draft_id}/send-to-publish",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform_account_id": owner_account_id, "publish_mode": "immediate"},
        )
        assert create_response.status_code == 200
        created = create_response.json()
        assert created["platform"] == "xhs"
        assert created["platform_account_id"] == owner_account_id
        assert created["source_draft_id"] == draft_id
        assert created["title"] == "Ready title"
        assert created["body"] == "Ready body"
        assert created["publish_mode"] == "immediate"
        assert created["status"] == "pending"

        intruder_draft_response = client.post(
            f"/api/drafts/{draft_id}/send-to-publish",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"platform_account_id": owner_account_id, "publish_mode": "immediate"},
        )
        assert intruder_draft_response.status_code == 404

        db = next(app.dependency_overrides[get_db]())
        try:
            publish_job = db.query(PublishJob).one()
            assert publish_job.source_draft_id == draft_id
            assert publish_job.platform_account_id == owner_account_id
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_jobs_list_requires_auth_and_filters_current_user(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.core.security import encrypt_text
    from backend.app.models import AccountCookieVersion, PlatformAccount

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "publish-list-owner")
    intruder_token = _register_and_get_access_token("publish-list-intruder")
    try:
        db = next(app.dependency_overrides[get_db]())
        try:
            intruder_account = PlatformAccount(
                user_id=2,
                platform="xhs",
                sub_type="pc",
                external_user_id="publish-list-intruder",
                nickname="发布测试账号",
                status="active",
            )
            db.add(intruder_account)
            db.flush()
            db.add(
                cookie_version(intruder_account.id, '{"a1":"intruder-a1"}', CHANNEL_WWW)
            )
            db.commit()
            intruder_account_id = intruder_account.id
        finally:
            db.close()

        owner_draft_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "title": "Owner publish", "body": "Owner body"},
        )
        owner_draft_id = owner_draft_response.json()["id"]
        owner_create_response = client.post(
            f"/api/drafts/{owner_draft_id}/send-to-publish",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform_account_id": owner_account_id},
        )
        assert owner_create_response.status_code == 200

        intruder_draft_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"platform": "xhs", "title": "Intruder publish", "body": "Intruder body"},
        )
        intruder_draft_id = intruder_draft_response.json()["id"]
        intruder_create_response = client.post(
            f"/api/drafts/{intruder_draft_id}/send-to-publish",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"platform_account_id": intruder_account_id},
        )
        assert intruder_create_response.status_code == 200

        anonymous_response = client.get("/api/publish/jobs?platform=xhs")
        assert anonymous_response.status_code == 401

        owner_list_response = client.get(
            "/api/publish/jobs?platform=xhs",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert owner_list_response.status_code == 200
        owner_payload = owner_list_response.json()
        assert owner_payload["total"] == 1
        assert owner_payload["items"][0]["title"] == "Owner publish"

        intruder_list_response = client.get(
            "/api/publish/jobs?platform=xhs",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_list_response.status_code == 200
        intruder_payload = intruder_list_response.json()
        assert intruder_payload["total"] == 1
        assert intruder_payload["items"][0]["title"] == "Intruder publish"
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_job_detail_and_update_enforce_ownership(tmp_path):
    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "publish-detail-owner")
    intruder_token = _register_and_get_access_token("publish-detail-intruder")
    try:
        draft_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "title": "Draft title", "body": "Draft body"},
        )
        draft_id = draft_response.json()["id"]
        create_response = client.post(
            f"/api/drafts/{draft_id}/send-to-publish",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform_account_id": owner_account_id},
        )
        job_id = create_response.json()["id"]

        owner_detail_response = client.get(
            f"/api/publish/jobs/{job_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert owner_detail_response.status_code == 200
        assert owner_detail_response.json()["title"] == "Draft title"

        intruder_detail_response = client.get(
            f"/api/publish/jobs/{job_id}",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_detail_response.status_code == 404

        update_response = client.patch(
            f"/api/publish/jobs/{job_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "title": "Edited publish title",
                "body": "Edited publish body",
                "publish_mode": "scheduled",
                "scheduled_at": "2030-01-02T03:04:05",
            },
        )
        assert update_response.status_code == 200
        updated = update_response.json()
        assert updated["title"] == "Edited publish title"
        assert updated["body"] == "Edited publish body"
        assert updated["publish_mode"] == "scheduled"
        assert updated["scheduled_at"] == "2030-01-02T03:04:05"

        list_response = client.get(
            "/api/publish/jobs?platform=xhs",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert list_response.json()["items"][0]["title"] == "Edited publish title"

        intruder_update_response = client.patch(
            f"/api/publish/jobs/{job_id}",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"title": "stolen"},
        )
        assert intruder_update_response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_assets_api_requires_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        response = client.get("/api/publish/jobs/1/assets")

        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_assets_api_adds_lists_and_deletes_owned_assets(tmp_path):
    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(tmp_path, "publish-assets-owner")
    intruder_token = _register_and_get_access_token("publish-assets-intruder")
    try:
        draft_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "title": "Asset title", "body": "Asset body"},
        )
        draft_id = draft_response.json()["id"]
        job_response = client.post(
            f"/api/drafts/{draft_id}/send-to-publish",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform_account_id": owner_account_id},
        )
        job_id = job_response.json()["id"]

        create_response = client.post(
            f"/api/publish/jobs/{job_id}/assets",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"asset_type": "image", "file_path": "storage/media/cover.png"},
        )
        assert create_response.status_code == 200
        created = create_response.json()
        assert created["publish_job_id"] == job_id
        assert created["asset_type"] == "image"
        assert created["file_path"] == "storage/media/cover.png"

        list_response = client.get(
            f"/api/publish/jobs/{job_id}/assets",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert list_response.status_code == 200
        assert list_response.json()["total"] == 1
        assert list_response.json()["items"][0]["id"] == created["id"]

        intruder_list_response = client.get(
            f"/api/publish/jobs/{job_id}/assets",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_list_response.status_code == 404

        intruder_delete_response = client.delete(
            f"/api/publish/assets/{created['id']}",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_delete_response.status_code == 404

        delete_response = client.delete(
            f"/api/publish/assets/{created['id']}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert delete_response.status_code == 200
        assert delete_response.json()["status"] == "deleted"

        empty_response = client.get(
            f"/api/publish/jobs/{job_id}/assets",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert empty_response.json()["total"] == 0
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_asset_upload_requires_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        response = client.post("/api/publish/assets/1/upload")

        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_asset_upload_uses_creator_cookie_and_updates_asset(tmp_path):
    from backend.app.api.publish import get_creator_publish_adapter_factory

    class FakeCreatorPublishAdapter:
        calls = []

        def __init__(self, cookies):
            self.cookies = cookies

        def upload_media(self, file_path, media_type):
            self.calls.append({"cookies": self.cookies, "file_path": file_path, "media_type": media_type})
            return {"creator_media_id": "creator-media-001", "fileIds": "file-001"}

    db_dependency, owner_token, creator_account_id = _create_creator_account_with_cookie(
        tmp_path, "publish-upload-owner"
    )
    FakeCreatorPublishAdapter.calls = []
    app.dependency_overrides[get_creator_publish_adapter_factory] = lambda: FakeCreatorPublishAdapter
    try:
        draft_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "title": "Upload title", "body": "Upload body"},
        )
        draft_id = draft_response.json()["id"]
        job_response = client.post(
            f"/api/drafts/{draft_id}/send-to-publish",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform_account_id": creator_account_id},
        )
        job_id = job_response.json()["id"]
        asset_response = client.post(
            f"/api/publish/jobs/{job_id}/assets",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"asset_type": "image", "file_path": "storage/media/cover.png"},
        )
        asset_id = asset_response.json()["id"]

        upload_response = client.post(
            f"/api/publish/assets/{asset_id}/upload",
            headers={"Authorization": f"Bearer {owner_token}"},
        )

        assert upload_response.status_code == 200
        uploaded = upload_response.json()
        assert uploaded["upload_status"] == "uploaded"
        assert uploaded["creator_media_id"] == "creator-media-001"
        assert uploaded["upload_error"] == ""
        assert FakeCreatorPublishAdapter.calls == [
            {
                "cookies": "a1=creator-a1; customer-sso-sid=creator-sso; access-token-creator.xiaohongshu.com=creator-token",
                "file_path": "storage/media/cover.png",
                "media_type": "image",
            }
        ]
    finally:
        app.dependency_overrides.pop(get_creator_publish_adapter_factory, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_asset_upload_rejects_cross_user_asset(tmp_path):
    from backend.app.api.publish import get_creator_publish_adapter_factory

    class FakeCreatorPublishAdapter:
        def __init__(self, cookies):
            raise AssertionError("cross-user upload must not instantiate adapter")

    db_dependency, owner_token, creator_account_id = _create_creator_account_with_cookie(
        tmp_path, "publish-upload-cross-owner"
    )
    intruder_token = _register_and_get_access_token("publish-upload-cross-intruder")
    app.dependency_overrides[get_creator_publish_adapter_factory] = lambda: FakeCreatorPublishAdapter
    try:
        draft_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "title": "Owner asset", "body": "Owner body"},
        )
        draft_id = draft_response.json()["id"]
        job_response = client.post(
            f"/api/drafts/{draft_id}/send-to-publish",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform_account_id": creator_account_id},
        )
        job_id = job_response.json()["id"]
        asset_response = client.post(
            f"/api/publish/jobs/{job_id}/assets",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"asset_type": "image", "file_path": "storage/media/cover.png"},
        )
        asset_id = asset_response.json()["id"]

        response = client.post(
            f"/api/publish/assets/{asset_id}/upload",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )

        assert response.status_code == 404
    finally:
        app.dependency_overrides.pop(get_creator_publish_adapter_factory, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_job_publish_requires_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        response = client.post("/api/publish/jobs/1/publish")

        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_job_publish_uses_creator_cookie_and_updates_status(tmp_path):
    from backend.app.api.publish import get_creator_publish_adapter_factory
    from backend.app.core.database import get_db
    from backend.app.models import Task

    class FakeCreatorPublishAdapter:
        calls = []

        def __init__(self, cookies):
            self.cookies = cookies

        def post_note(self, note_info):
            self.calls.append({"cookies": self.cookies, "note_info": note_info})
            return {"note_id": "xhs-note-001", "success": True}

        def upload_media(self, file_path, media_type):
            return {"creator_media_id": "creator-media-001", "fileIds": "file-001", "width": 1080, "height": 1440}

    db_dependency, owner_token, creator_account_id = _create_creator_account_with_cookie(
        tmp_path, "publish-action-owner"
    )
    FakeCreatorPublishAdapter.calls = []
    app.dependency_overrides[get_creator_publish_adapter_factory] = lambda: FakeCreatorPublishAdapter
    try:
        draft_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "title": "Publish title", "body": "Publish body"},
        )
        draft_id = draft_response.json()["id"]
        job_response = client.post(
            f"/api/drafts/{draft_id}/send-to-publish",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform_account_id": creator_account_id},
        )
        job_id = job_response.json()["id"]
        asset_response = client.post(
            f"/api/publish/jobs/{job_id}/assets",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"asset_type": "image", "file_path": "storage/media/cover.png"},
        )
        asset_id = asset_response.json()["id"]
        upload_response = client.post(
            f"/api/publish/assets/{asset_id}/upload",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert upload_response.status_code == 200

        publish_response = client.post(
            f"/api/publish/jobs/{job_id}/publish",
            headers={"Authorization": f"Bearer {owner_token}"},
        )

        assert publish_response.status_code == 200
        published = publish_response.json()
        assert published["status"] == "published"
        assert published["external_note_id"] == "xhs-note-001"
        assert published["publish_error"] == ""
        assert published["published_at"] is not None
        assert FakeCreatorPublishAdapter.calls == [
            {
                "cookies": "a1=creator-a1; customer-sso-sid=creator-sso; access-token-creator.xiaohongshu.com=creator-token",
                "note_info": {
                    "title": "Publish title",
                    "desc": "Publish body",
                    "media_type": "image",
                    "image_file_infos": [
                        {
                            "creator_media_id": "creator-media-001",
                            "fileIds": "file-001",
                            "width": 1080,
                            "height": 1440,
                        }
                    ],
                    "type": 1,
                    "postTime": None,
                },
            }
        ]

        db = next(app.dependency_overrides[get_db]())
        try:
            task = db.query(Task).filter(Task.task_type == "creator_publish").one()
            assert task.user_id == 1
            assert task.platform == "xhs"
            assert task.status == "completed"
            assert task.progress == 100
            assert task.payload["publish_job_id"] == job_id
            assert task.payload["external_note_id"] == "xhs-note-001"
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_creator_publish_adapter_factory, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_job_publish_passes_optional_creator_parameters(tmp_path):
    from backend.app.api.publish import get_creator_publish_adapter_factory

    class FakeCreatorOptionalPublishAdapter:
        calls = []

        def __init__(self, cookies):
            self.cookies = cookies

        def post_note(self, note_info):
            self.calls.append({"cookies": self.cookies, "note_info": note_info})
            return {"note_id": "optional-job-note", "success": True}

        def upload_media(self, file_path, media_type):
            return {"creator_media_id": "creator-media-optional", "fileIds": "file-optional", "width": 1080, "height": 1440}

    db_dependency, owner_token, creator_account_id = _create_creator_account_with_cookie(
        tmp_path, "publish-optional-owner"
    )
    FakeCreatorOptionalPublishAdapter.calls = []
    app.dependency_overrides[get_creator_publish_adapter_factory] = lambda: FakeCreatorOptionalPublishAdapter
    try:
        draft_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "title": "Optional title", "body": ""},
        )
        draft_id = draft_response.json()["id"]
        job_response = client.post(
            f"/api/drafts/{draft_id}/send-to-publish",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "platform_account_id": creator_account_id,
                "topics": ["早餐"],
                "location": "上海",
                "is_private": False,
            },
        )
        job_id = job_response.json()["id"]
        assert job_response.json()["publish_options"] == {
            "topics": ["早餐"],
            "location": "上海",
            "is_private": False,
            "privacy_type": 0,
        }

        asset_response = client.post(
            f"/api/publish/jobs/{job_id}/assets",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"asset_type": "image", "file_path": "storage/media/cover.png"},
        )
        asset_id = asset_response.json()["id"]
        upload_response = client.post(
            f"/api/publish/assets/{asset_id}/upload",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert upload_response.status_code == 200

        update_response = client.patch(
            f"/api/publish/jobs/{job_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"topics": ["早餐", "通勤"], "is_private": True, "location": ""},
        )
        assert update_response.status_code == 200
        assert update_response.json()["publish_options"] == {
            "topics": ["早餐", "通勤"],
            "is_private": True,
            "privacy_type": 1,
        }

        publish_response = client.post(
            f"/api/publish/jobs/{job_id}/publish",
            headers={"Authorization": f"Bearer {owner_token}"},
        )

        assert publish_response.status_code == 200
        assert FakeCreatorOptionalPublishAdapter.calls == [
            {
                "cookies": "a1=creator-a1; customer-sso-sid=creator-sso; access-token-creator.xiaohongshu.com=creator-token",
                "note_info": {
                    "title": "Optional title",
                    "desc": "",
                    "media_type": "image",
                    "image_file_infos": [
                        {
                            "creator_media_id": "creator-media-optional",
                            "fileIds": "file-optional",
                            "width": 1080,
                            "height": 1440,
                        }
                    ],
                    "type": 1,
                    "postTime": None,
                    "topics": ["早餐", "通勤"],
                },
            }
        ]
    finally:
        app.dependency_overrides.pop(get_creator_publish_adapter_factory, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_job_publish_records_failed_task(tmp_path):
    from backend.app.api.publish import get_creator_publish_adapter_factory
    from backend.app.core.database import get_db
    from backend.app.models import Task

    class FakeFailingCreatorPublishAdapter:
        def __init__(self, cookies):
            self.cookies = cookies

        def post_note(self, note_info):
            raise RuntimeError("creator publish denied")

        def upload_media(self, file_path, media_type):
            return {"creator_media_id": "creator-media-001", "fileIds": "file-001", "width": 1080, "height": 1440}

    db_dependency, owner_token, creator_account_id = _create_creator_account_with_cookie(
        tmp_path, "publish-action-failed-owner"
    )
    app.dependency_overrides[get_creator_publish_adapter_factory] = lambda: FakeFailingCreatorPublishAdapter
    try:
        draft_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "title": "Publish title", "body": "Publish body"},
        )
        draft_id = draft_response.json()["id"]
        job_response = client.post(
            f"/api/drafts/{draft_id}/send-to-publish",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform_account_id": creator_account_id},
        )
        job_id = job_response.json()["id"]
        asset_response = client.post(
            f"/api/publish/jobs/{job_id}/assets",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"asset_type": "image", "file_path": "storage/media/cover.png"},
        )
        asset_id = asset_response.json()["id"]
        upload_response = client.post(
            f"/api/publish/assets/{asset_id}/upload",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert upload_response.status_code == 200

        publish_response = client.post(
            f"/api/publish/jobs/{job_id}/publish",
            headers={"Authorization": f"Bearer {owner_token}"},
        )

        assert publish_response.status_code == 502
        db = next(app.dependency_overrides[get_db]())
        try:
            task = db.query(Task).filter(Task.task_type == "creator_publish").one()
            assert task.status == "failed"
            assert task.progress == 100
            assert task.payload["publish_job_id"] == job_id
            assert task.payload["error"] == "creator publish denied"
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_creator_publish_adapter_factory, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_job_publish_rejects_empty_content_before_adapter(tmp_path):
    from backend.app.api.publish import get_creator_publish_adapter_factory
    from backend.app.core.database import get_db
    from backend.app.models import Task

    class FakeCreatorPublishAdapter:
        def __init__(self, cookies):
            raise AssertionError("invalid publish content must not instantiate adapter")

    db_dependency, owner_token, creator_account_id = _create_creator_account_with_cookie(
        tmp_path, "publish-preflight-owner"
    )
    app.dependency_overrides[get_creator_publish_adapter_factory] = lambda: FakeCreatorPublishAdapter
    try:
        draft_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "title": "Valid title", "body": "Valid body"},
        )
        draft_id = draft_response.json()["id"]
        job_response = client.post(
            f"/api/drafts/{draft_id}/send-to-publish",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform_account_id": creator_account_id},
        )
        job_id = job_response.json()["id"]

        update_response = client.patch(
            f"/api/publish/jobs/{job_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"title": "   ", "body": ""},
        )
        assert update_response.status_code == 200

        publish_response = client.post(
            f"/api/publish/jobs/{job_id}/publish",
            headers={"Authorization": f"Bearer {owner_token}"},
        )

        assert publish_response.status_code == 400
        assert publish_response.json()["detail"] == "Publish title is required"
        db = next(app.dependency_overrides[get_db]())
        try:
            assert db.query(Task).filter(Task.task_type == "creator_publish").count() == 0
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_creator_publish_adapter_factory, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_job_publish_rejects_past_scheduled_time_before_adapter(tmp_path):
    from backend.app.api.publish import get_creator_publish_adapter_factory
    from backend.app.core.database import get_db
    from backend.app.core.time import shanghai_now
    from backend.app.models import PublishAsset, Task

    class FakeCreatorPublishAdapter:
        def __init__(self, cookies):
            raise AssertionError("past scheduled publish must not instantiate adapter")

    db_dependency, owner_token, creator_account_id = _create_creator_account_with_cookie(
        tmp_path, "publish-past-schedule-owner"
    )
    app.dependency_overrides[get_creator_publish_adapter_factory] = lambda: FakeCreatorPublishAdapter
    try:
        draft_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "title": "Scheduled title", "body": "Scheduled body"},
        )
        draft_id = draft_response.json()["id"]
        job_response = client.post(
            f"/api/drafts/{draft_id}/send-to-publish",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform_account_id": creator_account_id},
        )
        job_id = job_response.json()["id"]

        update_response = client.patch(
            f"/api/publish/jobs/{job_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "publish_mode": "scheduled",
                "scheduled_at": (shanghai_now() - timedelta(minutes=5)).isoformat(),
            },
        )
        assert update_response.status_code == 200

        db = next(app.dependency_overrides[get_db]())
        try:
            db.add(
                PublishAsset(
                    publish_job_id=job_id,
                    asset_type="image",
                    file_path="storage/media/cover.png",
                    upload_status="uploaded",
                    creator_media_id="creator-media-001",
                    creator_upload_info='{"fileIds":"file-001","width":1080,"height":1440}',
                )
            )
            db.commit()
        finally:
            db.close()

        publish_response = client.post(
            f"/api/publish/jobs/{job_id}/publish",
            headers={"Authorization": f"Bearer {owner_token}"},
        )

        assert publish_response.status_code == 400
        assert publish_response.json()["detail"] == "Scheduled publish time must be in the future"
        db = next(app.dependency_overrides[get_db]())
        try:
            assert db.query(Task).filter(Task.task_type == "creator_publish").count() == 0
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_creator_publish_adapter_factory, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_job_publish_rejects_already_completed_job(tmp_path):
    from backend.app.api.publish import get_creator_publish_adapter_factory
    from backend.app.core.database import get_db
    from backend.app.models import Task

    class FakeCreatorPublishAdapter:
        calls = []

        def __init__(self, cookies):
            self.cookies = cookies

        def post_note(self, note_info):
            self.calls.append({"cookies": self.cookies, "note_info": note_info})
            return {"note_id": "xhs-note-001", "success": True}

        def upload_media(self, file_path, media_type):
            return {"creator_media_id": "creator-media-001", "fileIds": "file-001", "width": 1080, "height": 1440}

    db_dependency, owner_token, creator_account_id = _create_creator_account_with_cookie(
        tmp_path, "publish-completed-owner"
    )
    FakeCreatorPublishAdapter.calls = []
    app.dependency_overrides[get_creator_publish_adapter_factory] = lambda: FakeCreatorPublishAdapter
    try:
        draft_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "title": "Publish title", "body": "Publish body"},
        )
        draft_id = draft_response.json()["id"]
        job_response = client.post(
            f"/api/drafts/{draft_id}/send-to-publish",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform_account_id": creator_account_id},
        )
        job_id = job_response.json()["id"]
        asset_response = client.post(
            f"/api/publish/jobs/{job_id}/assets",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"asset_type": "image", "file_path": "storage/media/cover.png"},
        )
        asset_id = asset_response.json()["id"]
        upload_response = client.post(
            f"/api/publish/assets/{asset_id}/upload",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert upload_response.status_code == 200

        first_response = client.post(
            f"/api/publish/jobs/{job_id}/publish",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert first_response.status_code == 200

        second_response = client.post(
            f"/api/publish/jobs/{job_id}/publish",
            headers={"Authorization": f"Bearer {owner_token}"},
        )

        assert second_response.status_code == 400
        assert second_response.json()["detail"] == "Publish job is already completed"
        assert len(FakeCreatorPublishAdapter.calls) == 1
        db = next(app.dependency_overrides[get_db]())
        try:
            assert db.query(Task).filter(Task.task_type == "creator_publish").count() == 1
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_creator_publish_adapter_factory, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_job_publish_rejects_cross_user_job(tmp_path):
    from backend.app.api.publish import get_creator_publish_adapter_factory

    class FakeCreatorPublishAdapter:
        def __init__(self, cookies):
            raise AssertionError("cross-user publish must not instantiate adapter")

    db_dependency, owner_token, creator_account_id = _create_creator_account_with_cookie(
        tmp_path, "publish-action-cross-owner"
    )
    intruder_token = _register_and_get_access_token("publish-action-cross-intruder")
    app.dependency_overrides[get_creator_publish_adapter_factory] = lambda: FakeCreatorPublishAdapter
    try:
        draft_response = client.post(
            "/api/drafts",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform": "xhs", "title": "Owner publish", "body": "Owner body"},
        )
        draft_id = draft_response.json()["id"]
        job_response = client.post(
            f"/api/drafts/{draft_id}/send-to-publish",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"platform_account_id": creator_account_id},
        )
        job_id = job_response.json()["id"]

        response = client.post(
            f"/api/publish/jobs/{job_id}/publish",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )

        assert response.status_code == 404
    finally:
        app.dependency_overrides.pop(get_creator_publish_adapter_factory, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_image_utility_routes_require_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        compose_response = client.post(
            "/api/files/images/compose",
            json={"title": "低卡早餐", "body": "三分钟做完", "width": 720, "height": 960},
        )
        resize_response = client.post(
            "/api/files/images/resize",
            json={"source_file_name": "xhs-image-u1-missing.png", "width": 320, "height": 320},
        )
        download_response = client.get("/api/files/media/xhs-image-u1-missing.png")

        assert compose_response.status_code == 401
        assert resize_response.status_code == 401
        assert download_response.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_image_utilities_compose_resize_download_and_enforce_scope(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        owner_token = _register_and_get_access_token("image-utility-owner")
        intruder_token = _register_and_get_access_token("image-utility-intruder")

        compose_response = client.post(
            "/api/files/images/compose",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "title": "低卡早餐合集",
                "body": "适合通勤前快速准备的小红书封面",
                "width": 720,
                "height": 960,
                "accent_color": "#111111",
            },
        )

        assert compose_response.status_code == 200
        composed = compose_response.json()
        assert composed["file_name"].startswith("xhs-image-u1-")
        assert composed["file_name"].endswith(".png")
        assert composed["download_url"] == f"/api/files/media/{composed['file_name']}"
        assert composed["width"] == 720
        assert composed["height"] == 960
        assert composed["media_type"] == "image/png"

        download_response = client.get(
            composed["download_url"],
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert download_response.status_code == 200
        assert download_response.headers["content-type"].startswith("image/png")
        assert download_response.content.startswith(b"\x89PNG")

        resize_response = client.post(
            "/api/files/images/resize",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "source_file_name": composed["file_name"],
                "width": 320,
                "height": 320,
                "mode": "cover",
                "format": "jpeg",
                "quality": 82,
            },
        )

        assert resize_response.status_code == 200
        resized = resize_response.json()
        assert resized["file_name"].startswith("xhs-image-u1-")
        assert resized["file_name"].endswith(".jpg")
        assert resized["file_name"] != composed["file_name"]
        assert resized["width"] == 320
        assert resized["height"] == 320
        assert resized["media_type"] == "image/jpeg"

        resized_download_response = client.get(
            resized["download_url"],
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert resized_download_response.status_code == 200
        assert resized_download_response.content.startswith(b"\xff\xd8")

        forbidden_download = client.get(
            composed["download_url"],
        )
        assert forbidden_download.status_code == 200

        forbidden_resize = client.post(
            "/api/files/images/resize",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"source_file_name": composed["file_name"], "width": 320, "height": 320},
        )
        assert forbidden_resize.status_code == 404
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_job_retry_and_cancel_require_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        retry_response = client.post("/api/publish/jobs/1/retry")
        cancel_response = client.post("/api/publish/jobs/1/cancel")

        assert retry_response.status_code == 401
        assert cancel_response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_job_retry_resets_failed_job_and_enforces_ownership(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.models import PublishJob, Task

    db_dependency, owner_token, creator_account_id = _create_creator_account_with_cookie(
        tmp_path, "publish-retry-owner"
    )
    intruder_token = _register_and_get_access_token("publish-retry-intruder")
    db = next(app.dependency_overrides[get_db]())
    try:
        failed_job = PublishJob(
            user_id=1,
            platform_account_id=creator_account_id,
            platform="xhs",
            title="Failed title",
            body="Failed body",
            status="failed",
            publish_error="creator denied",
        )
        db.add(failed_job)
        # T-103 回归夹具：**已成功提交给平台、但成功回写失败**的任务。
        # 特征是 external_note_id 非空 + status=failed —— 一旦放开重试，
        # 同一篇内容会在平台侧出现两次。原实现的 retry 会清空 external_note_id，
        # 等于抹掉了"这篇已经发出去了"的唯一证据。
        submitted_job = PublishJob(
            user_id=1,
            platform_account_id=creator_account_id,
            platform="xhs",
            title="Submitted title",
            body="Submitted body",
            status="failed",
            external_note_id="note-already-on-platform",
            publish_error="commit failed after submit",
        )
        db.add(submitted_job)
        db.commit()
        db.refresh(failed_job)
        db.refresh(submitted_job)
        job_id = failed_job.id
        submitted_job_id = submitted_job.id
    finally:
        db.close()

    try:
        forbidden_response = client.post(
            f"/api/publish/jobs/{job_id}/retry",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert forbidden_response.status_code == 404

        retry_response = client.post(
            f"/api/publish/jobs/{job_id}/retry",
            headers={"Authorization": f"Bearer {owner_token}"},
        )

        assert retry_response.status_code == 200
        retried = retry_response.json()
        assert retried["status"] == "pending"
        assert retried["publish_error"] == ""
        assert retried["external_note_id"] == ""
        assert retried["published_at"] is None

        # T-103: 已提交给平台的任务必须拒绝重试（否则重复发文）
        blocked_response = client.post(
            f"/api/publish/jobs/{submitted_job_id}/retry",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert blocked_response.status_code == 409
        assert "重复发文" in blocked_response.json()["detail"]

        db = next(app.dependency_overrides[get_db]())
        try:
            task = db.query(Task).filter(Task.task_type == "creator_publish_retry").one()
            assert task.user_id == 1
            assert task.status == "pending"
            assert task.progress == 0
            assert task.payload["publish_job_id"] == job_id
            assert db.get(PublishJob, job_id).status == "pending"
            # 被拒绝的那次重试不得对任务产生任何改动
            still_submitted = db.get(PublishJob, submitted_job_id)
            assert still_submitted.status == "failed"
            assert still_submitted.external_note_id == "note-already-on-platform"
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_publish_job_cancel_transitions_pending_or_scheduled_and_rejects_locked(tmp_path):
    from backend.app.core.database import get_db
    from backend.app.models import PublishJob, Task

    db_dependency, owner_token, creator_account_id = _create_creator_account_with_cookie(
        tmp_path, "publish-cancel-owner"
    )
    db = next(app.dependency_overrides[get_db]())
    try:
        pending_job = PublishJob(
            user_id=1,
            platform_account_id=creator_account_id,
            platform="xhs",
            title="Pending title",
            body="Pending body",
            status="pending",
        )
        scheduled_job = PublishJob(
            user_id=1,
            platform_account_id=creator_account_id,
            platform="xhs",
            title="Scheduled title",
            body="Scheduled body",
            publish_mode="scheduled",
            status="scheduled",
            scheduled_at=datetime.utcnow(),
        )
        published_job = PublishJob(
            user_id=1,
            platform_account_id=creator_account_id,
            platform="xhs",
            title="Published title",
            body="Published body",
            status="published",
        )
        db.add_all([pending_job, scheduled_job, published_job])
        db.commit()
        db.refresh(pending_job)
        db.refresh(scheduled_job)
        db.refresh(published_job)
        pending_id = pending_job.id
        scheduled_id = scheduled_job.id
        published_id = published_job.id
    finally:
        db.close()

    try:
        pending_response = client.post(
            f"/api/publish/jobs/{pending_id}/cancel",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        scheduled_response = client.post(
            f"/api/publish/jobs/{scheduled_id}/cancel",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        locked_response = client.post(
            f"/api/publish/jobs/{published_id}/cancel",
            headers={"Authorization": f"Bearer {owner_token}"},
        )

        assert pending_response.status_code == 200
        assert pending_response.json()["status"] == "cancelled"
        assert scheduled_response.status_code == 200
        assert scheduled_response.json()["status"] == "cancelled"
        assert locked_response.status_code == 400
        assert locked_response.json()["detail"] == "Publish job cannot be cancelled"

        db = next(app.dependency_overrides[get_db]())
        try:
            assert db.get(PublishJob, pending_id).status == "cancelled"
            assert db.get(PublishJob, scheduled_id).status == "cancelled"
            assert db.get(PublishJob, published_id).status == "published"
            assert db.query(Task).filter(Task.task_type == "creator_publish_cancel").count() == 2
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_run_due_tasks_requires_authentication(tmp_path):
    db_dependency = _override_database(tmp_path)
    try:
        response = client.post("/api/tasks/run-due?platform=xhs")

        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_run_due_tasks_executes_current_user_due_scheduled_publish_jobs(tmp_path):
    from backend.app.api.publish import get_creator_publish_adapter_factory
    from backend.app.core.database import get_db
    from backend.app.core.security import encrypt_text
    from backend.app.core.time import shanghai_now
    from backend.app.models import AccountCookieVersion, PlatformAccount, PublishAsset, PublishJob, Task

    class FakeDuePublishAdapter:
        calls = []

        def __init__(self, cookies):
            self.cookies = cookies

        def post_note(self, note_info):
            self.calls.append({"cookies": self.cookies, "note_info": note_info})
            return {"note_id": "due-note-001"}

    db_dependency, owner_token, owner_account_id = _create_creator_account_with_cookie(
        tmp_path, "due-publish-owner"
    )
    intruder_token = _register_and_get_access_token("due-publish-intruder")
    db = next(app.dependency_overrides[get_db]())
    try:
        now = shanghai_now()
        intruder_account = PlatformAccount(
            user_id=2,
            platform="xhs",
            sub_type="creator",
            external_user_id="intruder-creator",
            nickname="Intruder creator",
            status="active",
        )
        db.add(intruder_account)
        db.flush()
        # 发布链路打创作者平台（2026-09-21 拆分）：入侵者账号同样要有 creator 平台的令牌
        db.add(
            cookie_version(
                intruder_account.id,
                '{"a1":"intruder-a1","customer-sso-sid":"intruder-sso",'
                '"access-token-creator.xiaohongshu.com":"intruder-token"}',
                CHANNEL_CREATOR,
            )
        )

        due_job = PublishJob(
            user_id=1,
            platform_account_id=owner_account_id,
            platform="xhs",
            title="Due title",
            body="Due body",
            publish_mode="scheduled",
            status="pending",
            scheduled_at=now - timedelta(minutes=5),
        )
        future_job = PublishJob(
            user_id=1,
            platform_account_id=owner_account_id,
            platform="xhs",
            title="Future title",
            body="Future body",
            publish_mode="scheduled",
            status="pending",
            scheduled_at=now + timedelta(hours=2),
        )
        intruder_due_job = PublishJob(
            user_id=2,
            platform_account_id=intruder_account.id,
            platform="xhs",
            title="Intruder due title",
            body="Intruder due body",
            publish_mode="scheduled",
            status="pending",
            scheduled_at=now - timedelta(minutes=5),
        )
        db.add_all([due_job, future_job, intruder_due_job])
        db.flush()
        db.add_all(
            [
                PublishAsset(
                    publish_job_id=due_job.id,
                    asset_type="image",
                    file_path="storage/media/due.png",
                    upload_status="uploaded",
                    creator_media_id="media-due",
                    creator_upload_info='{"fileIds":"file-due","width":1080,"height":1440}',
                ),
                PublishAsset(
                    publish_job_id=future_job.id,
                    asset_type="image",
                    file_path="storage/media/future.png",
                    upload_status="uploaded",
                    creator_media_id="media-future",
                    creator_upload_info='{"fileIds":"file-future","width":1080,"height":1440}',
                ),
                PublishAsset(
                    publish_job_id=intruder_due_job.id,
                    asset_type="image",
                    file_path="storage/media/intruder.png",
                    upload_status="uploaded",
                    creator_media_id="media-intruder",
                    creator_upload_info='{"fileIds":"file-intruder","width":1080,"height":1440}',
                ),
            ]
        )
        db.commit()
        due_job_id = due_job.id
        future_job_id = future_job.id
        intruder_job_id = intruder_due_job.id
    finally:
        db.close()

    FakeDuePublishAdapter.calls = []
    app.dependency_overrides[get_creator_publish_adapter_factory] = lambda: FakeDuePublishAdapter
    try:
        intruder_response = client.post(
            "/api/tasks/run-due?platform=xhs",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        assert intruder_response.status_code == 200
        assert intruder_response.json()["executed_count"] == 1

        owner_response = client.post(
            "/api/tasks/run-due?platform=xhs",
            headers={"Authorization": f"Bearer {owner_token}"},
        )

        assert owner_response.status_code == 200
        payload = owner_response.json()
        assert payload["executed_count"] == 1
        assert payload["failed_count"] == 0
        assert payload["items"][0]["id"] == due_job_id
        assert payload["items"][0]["status"] == "published"
        assert len(FakeDuePublishAdapter.calls) == 2
        owner_call = FakeDuePublishAdapter.calls[-1]
        assert owner_call["cookies"] == "a1=creator-a1; customer-sso-sid=creator-sso; access-token-creator.xiaohongshu.com=creator-token"
        assert owner_call["note_info"]["title"] == "Due title"
        assert owner_call["note_info"]["postTime"] is None

        db = next(app.dependency_overrides[get_db]())
        try:
            assert db.get(PublishJob, due_job_id).status == "published"
            assert db.get(PublishJob, due_job_id).external_note_id == "due-note-001"
            assert db.get(PublishJob, future_job_id).status == "pending"
            assert db.get(PublishJob, intruder_job_id).status == "published"
            tasks = db.query(Task).filter(Task.task_type == "creator_publish_scheduler").all()
            assert len(tasks) == 2
            assert {task.user_id for task in tasks} == {1, 2}
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_creator_publish_adapter_factory, None)
        app.dependency_overrides.pop(db_dependency, None)


def test_run_due_publish_jobs_once_scopes_to_row_owner(tmp_path, monkeypatch):
    """DEF-066：定时发布补跑按行主收口 —— 跑用户 1 不发布用户 2 的到期 job。"""
    from backend.app.core.database import get_db
    from backend.app.core.security import encrypt_text
    from backend.app.models import AccountCookieVersion, PlatformAccount, PublishAsset, PublishJob, Task
    from backend.app.services import scheduler_service as sched

    class FakeDuePublishAdapter:
        calls = []

        def __init__(self, cookies):
            self.cookies = cookies

        def post_note(self, note_info):
            self.calls.append({"cookies": self.cookies, "note_info": note_info})
            return {"note_id": f"note-{len(self.calls)}"}

    db_dependency, owner_token, owner_account_id = _create_creator_account_with_cookie(
        tmp_path, "due-once-owner"
    )
    _register_and_get_access_token("due-once-second")
    db = next(app.dependency_overrides[get_db]())
    try:
        second_account = PlatformAccount(
            user_id=2,
            platform="xhs",
            sub_type="creator",
            external_user_id="second-creator",
            nickname="Second creator",
            status="active",
        )
        db.add(second_account)
        db.flush()
        # 发布打创作者平台（2026-09-21 拆分）：第二个账号也要有 creator 平台的令牌
        db.add(
            cookie_version(
                second_account.id,
                '{"a1":"second-a1","customer-sso-sid":"second-sso",'
                '"access-token-creator.xiaohongshu.com":"second-token"}',
                CHANNEL_CREATOR,
            )
        )
        first_job = PublishJob(
            user_id=1,
            platform_account_id=owner_account_id,
            platform="xhs",
            title="First due",
            body="First body",
            publish_mode="scheduled",
            status="pending",
            scheduled_at=datetime.utcnow() - timedelta(minutes=10),
        )
        second_job = PublishJob(
            user_id=2,
            platform_account_id=second_account.id,
            platform="xhs",
            title="Second due",
            body="Second body",
            publish_mode="scheduled",
            status="pending",
            scheduled_at=datetime.utcnow() - timedelta(minutes=8),
        )
        db.add_all([first_job, second_job])
        db.flush()
        db.add_all(
            [
                PublishAsset(
                    publish_job_id=first_job.id,
                    asset_type="image",
                    file_path="storage/media/first.png",
                    upload_status="uploaded",
                    creator_media_id="first-media",
                    creator_upload_info='{"fileIds":"first-file"}',
                ),
                PublishAsset(
                    publish_job_id=second_job.id,
                    asset_type="image",
                    file_path="storage/media/second.png",
                    upload_status="uploaded",
                    creator_media_id="second-media",
                    creator_upload_info='{"fileIds":"second-file"}',
                ),
            ]
        )
        db.commit()
        first_job_id = first_job.id
        second_job_id = second_job.id
    finally:
        db.close()

    # once 函数自开会话 ⇒ 把服务层 SessionLocal 打到**同一个**测试库
    from sqlalchemy.orm import sessionmaker as _sessionmaker

    engine = create_engine(
        f"sqlite:///{tmp_path / 'auth-test.db'}", connect_args={"check_same_thread": False}
    )
    TestingSessionLocal = _sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(sched, "SessionLocal", TestingSessionLocal)

    FakeDuePublishAdapter.calls = []
    try:
        db = next(app.dependency_overrides[get_db]())
        try:
            # ① 只补跑用户 1：用户 2 的到期 job 原地不动
            result = sched.run_due_publish_jobs_once(
                user_id=1, platform="xhs", adapter_factory=FakeDuePublishAdapter
            )
            assert result["executed_count"] == 1
            assert result["failed_count"] == 0
            assert {item["id"] for item in result["items"]} == {first_job_id}
            assert db.get(PublishJob, first_job_id).status == "published"
            assert db.get(PublishJob, second_job_id).status == "pending", (
                "行主隔离失效：跑用户 1 把用户 2 的 job 也发了"
            )
            assert db.query(Task).filter(Task.task_type == "creator_publish_scheduler").count() == 1

            # ② 补跑用户 2：自己的到期 job 照常发布（N 行各自触发 ⇒ 全员都被覆盖）
            result = sched.run_due_publish_jobs_once(
                user_id=2, platform="xhs", adapter_factory=FakeDuePublishAdapter
            )
            assert result["executed_count"] == 1
            assert {item["id"] for item in result["items"]} == {second_job_id}
            assert db.get(PublishJob, second_job_id).status == "published"
            assert db.query(Task).filter(Task.task_type == "creator_publish_scheduler").count() == 2
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_scheduled_jobs_are_registered_from_db_rows(tmp_path, monkeypatch):
    """DB 驱动装配（设计 15 §6.1）：active 行 ⇒ 注册；触发式来自行而非 config。

    R14：装配形态只剩 cron / at 两档，断言从「interval 秒数」改看
    `CronTrigger` 的字段（`hour` / `minute`）。
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    from backend.app.core.database import Base, get_db
    from backend.app.models import ScheduledJob, User
    from backend.app.services import scheduler_service as sched
    from backend.app.services.scheduler_service import build_scheduler, sync_jobs_from_db

    engine = create_engine(
        f"sqlite:///{tmp_path / 'sched-rows.db'}", connect_args={"check_same_thread": False}
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    # ⚠️ 同 `_override_database`：FK=ON 后父行必须存在（2026-10-04）。
    monkeypatch.setattr(sched, "SessionLocal", TestingSessionLocal)

    db = TestingSessionLocal()
    try:
        # ⚠️ DEF-066：内置行按用户播种（判重 `user_id + job_key`）⇒ 夹具行挂到用户名下，
        # 同 key 的种子行被「已有」跳过，表达式不会被种子行遮蔽。
        # cron 表达式刻意与内置种子不同，好让断言能唯一认出夹具行。
        db.add(User(username="sched-rows-owner", password_hash="x"))
        db.commit()
        owner_id = db.scalar(select(User.id).where(User.username == "sched-rows-owner"))
        db.add(ScheduledJob(
            user_id=owner_id, name="巡检",
            job_key="system.cookie_health_check", trigger_type="cron",
            cron_expression="0 0 */2 * * *", status="active", is_builtin=True,
        ))
        db.add(ScheduledJob(
            user_id=owner_id, name="补跑",
            job_key="system.due_publish", trigger_type="cron",
            cron_expression="0 30 7 * * *", status="active", is_builtin=True,
        ))
        db.commit()
    finally:
        db.close()

    scheduler = build_scheduler()
    stats = sync_jobs_from_db(scheduler)
    # sync 会顺带 ensure_builtin_jobs（该用户其余 5 个活跃内置行）⇒ 断言「两行都注册了 + 触发式正确」
    assert stats["registered"] >= 2
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert len(jobs) >= 2
    # 断言「必要 job 都注册了」而不是「恰好这几个」：后续新增任务类型不该让本测试失效
    # APScheduler 4 的 `CronTrigger` 不挂 `hour`/`minute` 属性，只能经 `fields` 读。
    # ⚠️ 不能直接取 `field.expressions[0]` —— `*` 字段在 APScheduler 4 里是
    # `AllExpression` **对象**（不可哈希、str() 后才可读），一律走 `str()`。
    def _cron_field(trigger: CronTrigger, name: str) -> str:
        for field in trigger.fields:
            if field.name == name:
                return str(field)
        raise AssertionError(f"CronTrigger 没有字段 {name}")

    cron_fields = {
        (_cron_field(job.trigger, "hour"), _cron_field(job.trigger, "minute"))
        for job in jobs.values()
        if isinstance(job.trigger, CronTrigger)
    }
    # 「触发式来自 DB 行」：两条夹具行的表达式必须原样出现在注册结果里
    assert ("*/2", "0") in cron_fields  # 0 0 */2 * * *（每 2 小时整点）
    assert ("7", "30") in cron_fields   # 0 30 7 * * *（每天 07:30）
    for job in jobs.values():
        assert job.max_instances == 1
        assert job.coalesce is True


def test_qianfan_health_check_config_switch_only_affects_fallback_seed(monkeypatch, tmp_path):
    """R24（2026-09-24）：`qianfan.health_check_enabled` 已降级为**兜底值**。

    ⚠️ 前身 `test_qianfan_health_check_seed_is_paused_when_disabled`（断言「config 关闭
    ⇒ 种子行 paused ⇒ 不注册」）**已被 R24 取代** —— 种子权威迁到**随安装包分发**的
    应用初始化数据（设计 15 §4.5），config 键只在**数据文件缺该 job_key 时**才起作用。
    本用例把两条路径都钉住（取代后更强：原用例只测了其中一条）。
    """
    from backend.app.core import initial_data
    from backend.app.core.config import get_settings
    from backend.app.core.database import Base
    from backend.app.models import ScheduledJob, User
    from backend.app.services import scheduler_service as sched
    from backend.app.services.scheduler_service import (
        build_scheduler,
        ensure_builtin_jobs,
        sync_jobs_from_db,
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "qianfan_health_check_enabled", False, raising=False)

    def _fresh_session():
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        return sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # ① 初始化数据里有这个 job_key ⇒ config 开关**不再**决定启停（以数据文件为准）
    SessionLocal = _fresh_session()
    monkeypatch.setattr(sched, "SessionLocal", SessionLocal)
    db = SessionLocal()
    try:
        # DEF-066：内置行按用户播种 ⇒ 先有用户
        db.add(User(username="qianfan-switch-a", password_hash="x"))
        db.commit()
        ensure_builtin_jobs(db)
        row = db.scalars(
            select(ScheduledJob).where(ScheduledJob.job_key == "system.qianfan_health_check")
        ).first()
        assert row is not None, "按用户播种后应有该行"
        assert row.status == "active", "初始化数据里声明了该任务 ⇒ config 开关不该再起作用"
    finally:
        db.close()

    # ② 数据文件缺失（兜底路径）⇒ 仍按 config 推导：关闭 ⇒ paused
    monkeypatch.setattr(
        initial_data, "initial_data_candidates", lambda filename: [tmp_path / "nope.json"]
    )
    SessionLocal = _fresh_session()
    monkeypatch.setattr(sched, "SessionLocal", SessionLocal)
    db = SessionLocal()
    try:
        db.add(User(username="qianfan-switch-b", password_hash="x"))
        db.commit()
        ensure_builtin_jobs(db)
        row = db.scalars(
            select(ScheduledJob).where(ScheduledJob.job_key == "system.qianfan_health_check")
        ).first()
        assert row is not None, "兜底种子也应落在用户名下"
        assert row.status == "paused"
        qianfan_id = row.id
    finally:
        db.close()

    scheduler = build_scheduler()
    sync_jobs_from_db(scheduler)
    keys = {job.id for job in scheduler.get_jobs()}
    # 千帆巡检的 job 行被 paused ⇒ 没有以 job_ 前缀注册的它
    assert f"job_{qianfan_id}" not in keys
    # 其余任务不受影响（关一个开关不该把调度器整体关掉）
    assert len(keys) >= 4


def test_run_monitoring_refresh_once_scopes_to_row_owner(tmp_path, monkeypatch):
    """DEF-066：监控刷新按行主收口 —— 跑用户 1 不碰用户 2 的目标（原全库形态已废）。"""
    from backend.app.core.database import get_db
    from backend.app.models import MonitoringSnapshot, MonitoringTarget, Note, PlatformAccount, Task
    from backend.app.services import scheduler_service as sched

    db_dependency, owner_token, owner_account_id = _create_pc_account_with_cookie(
        tmp_path, "scheduled-monitor-owner"
    )
    _register_and_get_access_token("scheduled-monitor-second")
    db = next(app.dependency_overrides[get_db]())
    try:
        second_account = PlatformAccount(
            user_id=2,
            platform="xhs",
            sub_type="pc",
            external_user_id="second-pc",
            nickname="Second PC",
            status="active",
        )
        db.add(second_account)
        db.flush()
        db.add_all(
            [
                Note(
                    user_id=1,
                    platform_account_id=owner_account_id,
                    platform="xhs",
                    note_id="monitor-auto-owner",
                    title="低卡早餐自动监控",
                    content="适合通勤的低卡早餐",
                    author_name="owner-author",
                    raw_json={"likes": 20, "collects": 5, "comments": 2, "shares": 1},
                ),
                Note(
                    user_id=2,
                    platform_account_id=second_account.id,
                    platform="xhs",
                    note_id="monitor-auto-second",
                    title="低卡早餐第二用户",
                    content="第二用户的低卡早餐",
                    author_name="second-author",
                    raw_json={"likes": 100},
                ),
            ]
        )
        active_owner = MonitoringTarget(
            user_id=1,
            platform="xhs",
            target_type="keyword",
            name="Owner breakfast",
            value="低卡早餐",
            status="active",
        )
        paused_owner = MonitoringTarget(
            user_id=1,
            platform="xhs",
            target_type="keyword",
            name="Paused breakfast",
            value="低卡早餐",
            status="paused",
        )
        active_second = MonitoringTarget(
            user_id=2,
            platform="xhs",
            target_type="keyword",
            name="Second breakfast",
            value="低卡早餐",
            status="active",
        )
        db.add_all([active_owner, paused_owner, active_second])
        db.commit()
        active_owner_id = active_owner.id
        paused_owner_id = paused_owner.id
        active_second_id = active_second.id
    finally:
        db.close()

    # once 函数自开会话 ⇒ 把服务层 SessionLocal 打到**同一个**测试库
    # （`_override_database` 建在 `tmp_path/auth-test.db`；打错库 = 读到空库）
    from sqlalchemy.orm import sessionmaker as _sessionmaker

    engine = create_engine(
        f"sqlite:///{tmp_path / 'auth-test.db'}", connect_args={"check_same_thread": False}
    )
    TestingSessionLocal = _sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(sched, "SessionLocal", TestingSessionLocal)

    try:
        db = next(app.dependency_overrides[get_db]())
        try:
            # ① 只跑用户 1：用户 2 的目标一根头发都不动
            result = sched.run_monitoring_refresh_once(user_id=1, platform="xhs")
            assert result["refreshed_count"] == 1
            assert result["items"][0]["target_id"] == active_owner_id
            assert db.get(MonitoringTarget, active_owner_id).last_refreshed_at is not None
            assert db.get(MonitoringTarget, active_second_id).last_refreshed_at is None, (
                "行主隔离失效：跑用户 1 刷新了用户 2 的目标"
            )
            assert db.get(MonitoringTarget, paused_owner_id).last_refreshed_at is None
            owner_snapshot = db.scalars(
                select(MonitoringSnapshot).where(MonitoringSnapshot.target_id == active_owner_id)
            ).one()
            assert owner_snapshot.payload["matched_count"] == 1
            assert owner_snapshot.payload["total_engagement"] == 28
            tasks = db.query(Task).filter(Task.task_type == "monitoring_refresh").all()
            assert len(tasks) == 1
            assert {task.user_id for task in tasks} == {1}

            # ② 跑用户 2：自己的目标照常刷新，匹配只含自己的笔记
            result = sched.run_monitoring_refresh_once(user_id=2, platform="xhs")
            assert result["refreshed_count"] == 1
            assert result["items"][0]["target_id"] == active_second_id
            second_snapshot = db.scalars(
                select(MonitoringSnapshot).where(MonitoringSnapshot.target_id == active_second_id)
            ).one()
            assert second_snapshot.payload["matched_count"] == 1
            assert second_snapshot.payload["total_engagement"] == 100
            tasks = db.query(Task).filter(Task.task_type == "monitoring_refresh").all()
            assert len(tasks) == 2
            assert {task.user_id for task in tasks} == {1, 2}
            assert all(task.status == "completed" for task in tasks)
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def _seed_provider_and_mappings(token: str, model_type: str, model_name: str, api_key: str = "***") -> None:
    """v2.0 辅助：建一条服务商，并把**同类**业务点全部映射到它。

    ⚠️ 不按业务点逐个配 —— 用例关心的是「配了就能调」，类别对齐即可
    （`image` 类只映射 3 个图像业务点，`text` 类映射 5 个文本业务点）。
    """
    from backend.app.core.security import encrypt_text
    from backend.app.models import LlmBusinessProvider, User
    from sqlalchemy import select as _select

    db = _test_session_factory()
    try:
        # 用「刚注册那个用户」的 id（由 _register_and_get_access_token 记录）——
        # 一个用例可能注册多个用户，取末位会把映射建到 intruder 名下。
        user_id = _last_registered_user_id
        key = f"TEST-{model_type.upper()[:4]}"
        existing = db.scalar(
            _select(LlmGatewayConfig).where(
                LlmGatewayConfig.user_id == user_id,
                LlmGatewayConfig.provider_key == key,
            )
        )
        if existing is None:
            db.add(
                LlmGatewayConfig(
                    user_id=user_id,
                    name=f"test-{model_type}",
                    provider_key=key,
                    base_url="https://api.example.test/v1",
                    # ⚠️ 库列存**密文**（与一期端点同样经 `encrypt_text`）——
                    # 直接写明文会让 `decrypt_text` 抛 InvalidToken。
                    access_token=encrypt_text(api_key),
                    models_json=json.dumps([model_name]),
                    enabled=True,
                    healthy=True,
                )
            )
            db.flush()
            # ⚠️ v2.7/v2.8：能力类型/平台改由**模型行**承载，`default_model` 整列删除。
            db.add(
                LlmGatewayModel(
                    user_id=user_id,
                    provider_key=key,
                    model_name=model_name,
                    model_type=model_type,
                    platform="xhs",
                    enabled=True,
                )
            )
            db.flush()
        for spec in BUSINESS_KEYS:
            if spec.kind != model_type:
                continue
            # ⚠️ **必须 upsert，不能盲 add**（2026-10-13 修，本卡 Group B 的根因）：
            # R4 服务商种子（设计 37）在**注册时**就为新用户播种了默认业务点映射
            # （`seed_default_business_map_for_user`），`llm_business_providers` 上
            # 有唯一约束 `(user_id, platform, business_key)` ⇒ 盲 add 必撞
            # `UNIQUE constraint failed`。本辅助的语义是「把该业务点**配成**这个服务商」
            # ⇒ 已有行**改指**（保幂等、也贴「配了就能调」的用例意图）。
            # ⚠️ 走 `conftest.upsert_business_mapping`（**唯一实现**）——
            # 本文件内的调用点已统一到它，这里不许再内联一份（DEF-044）。
            _upsert_business_mapping(
                db,
                user_id=user_id,
                business_key=spec.key,
                provider_key=key,
                model_name=model_name,
            )
        db.commit()
    finally:
        db.close()
