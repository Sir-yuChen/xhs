"""运行时配置中心（设计 10 §7，2026-09-27）：CRUD 契约 + **实时生效** + 缓存失效。

本文件覆盖四组：

1. **API CRUD**：增 / 查 / 改 / 删（删 = 回落默认）＋ 越权 404 ＋ 重复键 409；
2. **值类型**：`int` / `bool` / `str` 各自 round-trip（存进去什么、读回来还是什么类型）；
3. **实时生效**（P-3 的落点）：写配置 ⇒ **同进程内立即**读到新值（不等 TTL）；
4. **白名单守卫**：`LIVE_KEYS` 里的每个键都必须在**它声称的读点函数**里真的被引用 ——
   否则 UI 会把「仅存档」说成「实时生效」（负向失真同样是造假）。

⚠️ 夹具用 `tests/backend/conftest.py` 的公共夹具（三条硬规矩之二）；`settings_service`
是模块级持有 `SessionLocal` 的服务模块，`api_client` 会**默认打桩**它（见 conftest
docstring），所以用例不会写到真库。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from backend.app.services import settings_service


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration

#: 仓库根（`tests/backend/x.py` → 上两级）
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """每个用例前后清一次进程内缓存。

    ⚠️ 必需：缓存是**进程级**的（`(user_id, key) -> 值`），不清会让上一个用例写下的
    值漏到下一个用例 —— 那类失败极难定位（「单跑绿、连跑红」）。
    """
    settings_service.reset_cache_for_tests()
    yield
    settings_service.reset_cache_for_tests()


def _register(api_client, username: str) -> dict[str, str]:
    response = api_client.post(
        "/api/auth/register", json={"username": username, "password": "pw-123456"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# --------------------------------------------------------------------------- #
# 1) CRUD
# --------------------------------------------------------------------------- #
def test_settings_crud_round_trip(api_client):
    """增 → 查（列表 + 单条）→ 改 → 删（回落）全链路。"""
    headers = _register(api_client, "settings-crud")

    created = api_client.post(
        "/api/settings",
        json={
            "key": "custom_probe_value",
            "value": 12,
            "value_type": "int",
            "category": "sync",
            "label": "自定义探针",
            "description": "只用于测试",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["value"] == 12
    assert created.json()["is_live"] is False, "自定义键没有运行时读点 ⇒ 仅存档"

    listed = api_client.get("/api/settings", headers=headers).json()
    keys = [item["key"] for item in listed["items"]]
    assert "custom_probe_value" in keys
    assert listed["total"] == len(listed["items"])
    # 注册即播种（设计 10 §7.3）：新用户开箱就有那几个**有读点**的键
    assert "crawl_rate_limit_per_minute" in keys, "新用户必须已播种基线配置"

    single = api_client.get("/api/settings/custom_probe_value", headers=headers)
    assert single.status_code == 200 and single.json()["value"] == 12

    updated = api_client.put(
        "/api/settings/custom_probe_value", json={"value": 34}, headers=headers
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["value"] == 34
    # 只改 value ⇒ 元数据不许被冲掉（`None` = 不改）
    assert updated.json()["label"] == "自定义探针"

    removed = api_client.delete("/api/settings/custom_probe_value", headers=headers)
    assert removed.status_code == 200, removed.text
    assert removed.json()["status"] == "deleted"
    assert removed.json()["effective_value"] is None, "自定义键删掉后没有默认值可回落"

    assert (
        api_client.get("/api/settings/custom_probe_value", headers=headers).status_code == 404
    )


def test_delete_falls_back_to_the_configured_default(api_client):
    """删掉**有读点**的键 ⇒ 回落到 settings / 内置默认，并把回落值回给前端。"""
    headers = _register(api_client, "settings-fallback")

    api_client.put(
        "/api/settings/crawl_rate_limit_per_minute", json={"value": 7}, headers=headers
    )
    removed = api_client.delete("/api/settings/crawl_rate_limit_per_minute", headers=headers)
    assert removed.status_code == 200
    # 回落值来自 `Settings.crawl_rate_limit_per_minute`（YAML 默认 **8**，2026-09-27 基线下调）
    assert removed.json()["effective_value"] == 8
    assert removed.json()["is_live"] is True


def test_duplicate_key_is_409_not_silent_update(api_client):
    """重复建键 ⇒ 409（不许静默当成「更新」—— 那会让用户以为新建成功了）。"""
    headers = _register(api_client, "settings-dup")
    payload = {"key": "dup_key", "value": 1, "value_type": "int"}
    assert api_client.post("/api/settings", json=payload, headers=headers).status_code == 201
    again = api_client.post("/api/settings", json=payload, headers=headers)
    assert again.status_code == 409, again.text


def test_other_users_key_is_404(api_client):
    """别人的键 ⇒ 404（不泄露「这个键存在」）。"""
    owner = _register(api_client, "settings-owner")
    intruder = _register(api_client, "settings-intruder")
    api_client.post(
        "/api/settings", json={"key": "private_key", "value": 1}, headers=owner
    )
    assert api_client.get("/api/settings/private_key", headers=intruder).status_code == 404
    assert (
        api_client.put(
            "/api/settings/private_key", json={"value": 2}, headers=intruder
        ).status_code
        == 404
    )
    assert api_client.delete("/api/settings/private_key", headers=intruder).status_code == 404


def test_unknown_field_is_rejected(api_client):
    """`extra="forbid"`：未知字段 422（不许静默丢弃 —— 旧进程会把新参数悄悄吃掉）。"""
    headers = _register(api_client, "settings-extra")
    response = api_client.post(
        "/api/settings",
        json={"key": "strict_key", "value": 1, "value_type": "int", "nope": True},
        headers=headers,
    )
    assert response.status_code == 422, response.text


# --------------------------------------------------------------------------- #
# 2) 值类型 round-trip
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "value_type", "expected"),
    [
        (60, "int", 60),
        (True, "bool", True),
        (False, "bool", False),
        ("hello", "str", "hello"),
        (1.5, "float", 1.5),
    ],
)
def test_value_types_round_trip(api_client, value, value_type, expected):
    """值类型必须 round-trip：存什么类型，读回来还是什么类型（`"60"` ≠ `60`）。"""
    headers = _register(api_client, f"settings-type-{value_type}-{str(value)[:4]}")
    key = f"typed_{value_type}"
    created = api_client.post(
        "/api/settings",
        json={"key": key, "value": value, "value_type": value_type},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    fetched = api_client.get(f"/api/settings/{key}", headers=headers).json()
    assert fetched["value"] == expected
    assert isinstance(fetched["value"], type(expected))


def test_bool_string_input_is_parsed_strictly(api_client):
    """`"false"` 必须解成 `False` —— 宽松的「非空即真」会把「关」存成「开」。"""
    headers = _register(api_client, "settings-bool-text")
    api_client.post(
        "/api/settings",
        json={"key": "bool_text", "value": "false", "value_type": "bool"},
        headers=headers,
    )
    assert api_client.get("/api/settings/bool_text", headers=headers).json()["value"] is False

    bad = api_client.post(
        "/api/settings",
        json={"key": "bool_bad", "value": "maybe", "value_type": "bool"},
        headers=headers,
    )
    assert bad.status_code == 422, "无法判定的布尔值必须 422，不许猜"


def test_numeric_bounds_are_clamped(api_client):
    """限流额度这类键有合理上界：手滑填 100000 要被夹住（否则一次同步打爆平台）。"""
    headers = _register(api_client, "settings-bounds")
    created = api_client.post(
        "/api/settings",
        json={"key": "crawl_rate_limit_per_minute", "value": 100000, "value_type": "int"},
        headers=headers,
    )
    assert created.status_code == 409, "注册时已播种该键 ⇒ 应走 PUT"


def test_update_clamps_out_of_range_value(api_client):
    """PUT 超上界 ⇒ 夹到上界（不是 422，也不是照单全收）。"""
    headers = _register(api_client, "settings-clamp")
    updated = api_client.put(
        "/api/settings/crawl_rate_limit_per_minute", json={"value": 99999}, headers=headers
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["value"] == 600


# --------------------------------------------------------------------------- #
# 3) 实时生效 + 缓存失效（P-3 的落点）
# --------------------------------------------------------------------------- #
def test_write_is_visible_immediately(api_client, db_factory):
    """写配置 ⇒ **同进程内立即**读到新值（不许等 TTL，不许重启）。

    这是「页面实时配置」这句需求的可执行判据：`live_value` 读的是 DB 行，
    而写路径会主动失效缓存 —— 中间没有等待窗口。
    """
    from backend.app.models import User
    from sqlalchemy import select

    headers = _register(api_client, "settings-live")
    db = db_factory()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "settings-live"))
    finally:
        db.close()

    assert (
        settings_service.live_int(None, user_id, "crawl_rate_limit_per_minute", default=0) == 8
    ), "新注册用户的额度 = 随包种子基线（2026-09-27 由 60 下调为 8，设计 13 §38.7）"
    api_client.put(
        "/api/settings/crawl_rate_limit_per_minute", json={"value": 7}, headers=headers
    )
    assert (
        settings_service.live_int(None, user_id, "crawl_rate_limit_per_minute", default=0) == 7
    ), "写完必须立即生效（缓存失效），不许等 TTL"


def test_rate_limiter_reads_the_configured_quota(api_client, db_factory):
    """限流器**真的**按配置放行（改造前它被构造参数写死的 5 压住，设计 13 §38.1 F1）。"""
    from sqlalchemy import select

    from backend.app.models import PlatformAccount, User
    from backend.app.services.rate_limiter import CrawlRateLimiter

    headers = _register(api_client, "settings-quota")
    db = db_factory()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "settings-quota"))
        account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="creator", nickname="限流店", status="active"
        )
        db.add(account)
        db.commit()
        account_id = account.id
    finally:
        db.close()

    api_client.put("/api/settings/crawl_rate_limit_per_minute", json={"value": 7}, headers=headers)

    limiter = CrawlRateLimiter()  # ⚠️ 不传构造参数 —— 传了就等于把配置压住（老缺陷）
    assert limiter.quota_for(f"{account_id}:www") == 7

    # 放行 7 次之后第 8 次必须被拒（额度是真的在用，不是只回了个数）
    assert all(limiter.allow(f"{account_id}:www") for _ in range(7))
    assert limiter.allow(f"{account_id}:www") is False


def test_explicit_constructor_argument_still_wins(api_client):
    """显式构造参数**仍**高于运行时配置（§35.8 的老契约不许被新一层破掉）。"""
    from backend.app.services.rate_limiter import CrawlRateLimiter

    headers = _register(api_client, "settings-explicit")
    api_client.put("/api/settings/crawl_rate_limit_per_minute", json={"value": 60}, headers=headers)
    assert CrawlRateLimiter(max_per_minute=5).quota_for("1:www") == 5
    assert CrawlRateLimiter(max_per_minute=5).max_per_minute == 5


def test_publish_guard_switch_reads_the_configured_value(api_client, db_factory):
    """商品重复发布开关**真的**按配置读（`live_bool` 的读点，设计 13 §31.5.2）。

    ① 新注册用户 = 随包种子基线 **关**（用户口径「默认关闭」）；
    ② `PUT` 打开 ⇒ **同进程内立即**读到开（缓存失效，不许等 TTL）；
    ③ 宽松文本按**严格**口径解：值被改成 `str` 且文本是 `"false"` ⇒ 仍是**关**
       —— 「非空即真」会把关读成开（静默错值，开关直接形同虚设）。
    """
    from sqlalchemy import select

    from backend.app.models import User

    headers = _register(api_client, "settings-guard")
    db = db_factory()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "settings-guard"))
    finally:
        db.close()

    key = "xhs_allow_duplicate_goods_publish"
    # ⚠️ `default` 故意给 **True**：读到 False 才说明真的读了配置行，而不是被 default 兜住
    assert settings_service.live_bool(None, user_id, key, default=True) is False, (
        "新注册用户的开关 = 随包种子基线（默认关闭）"
    )

    api_client.put(f"/api/settings/{key}", json={"value": True}, headers=headers)
    assert settings_service.live_bool(None, user_id, key, default=False) is True, (
        "写完必须立即生效（缓存失效），不许等 TTL"
    )

    db = db_factory()
    try:
        row = settings_service.find_setting(db, user_id, key)
        settings_service.update_setting(db, row, value="false", value_type="str")
        db.commit()
    finally:
        db.close()
    assert settings_service.live_bool(None, user_id, key, default=True) is False, (
        "宽松文本按严格口径：'false' 是关（非空即真会把关读成开）"
    )


# --------------------------------------------------------------------------- #
# 4) 种子 + 白名单守卫
# --------------------------------------------------------------------------- #
def test_seeding_is_idempotent_and_never_overwrites(db_factory):
    """播种只补缺失、**不覆盖**（升级不许重置用户调过的值）。"""
    from backend.app.models import User

    db = db_factory()
    try:
        user = User(username="seed-user", password_hash="x")
        db.add(user)
        db.commit()
        user_id = user.id

        created = settings_service.seed_settings_for_user(db, user_id)
        db.commit()
        assert created, "首次播种必须建行"
        assert settings_service.seed_settings_for_user(db, user_id) == [], "第二次必须零新增"

        row = settings_service.find_setting(db, user_id, "crawl_rate_limit_per_minute")
        row.value = "7"
        db.commit()
        settings_service.seed_settings_for_user(db, user_id)
        db.commit()
        assert (
            settings_service.find_setting(db, user_id, "crawl_rate_limit_per_minute").value == "7"
        ), "播种不许覆盖用户改过的值"
    finally:
        db.close()


def test_initial_data_file_is_loadable():
    """`config/initial_data/system_settings.json` 必须能解析出条目（随包分发的基线）。"""
    from backend.app.core.initial_data import load_initial_system_settings

    rows = load_initial_system_settings()
    assert rows, "初始化数据必须存在且可解析（否则新装用户设置页是空的）"
    keys = {row["key"] for row in rows}
    assert keys <= set(settings_service.LIVE_KEYS), (
        "初始化数据只允许声明**有运行时读点**的键 —— "
        "种一批没读点的行等于给「改了没反应」制造现场"
    )


def test_live_keys_are_real_read_points():
    """`LIVE_KEYS` 里每个键都必须在**它声称的读点函数**里真的被引用。

    ⚠️ 为什么要有这条：`is_live` 是 UI 上「实时生效 / 仅存档」的唯一判据。白名单里
    混进一个**没有读点**的键 ⇒ 页面告诉用户「改了立即生效」，实际什么都不发生 ——
    负向失真同样是造假（硬约束⑤），而且比「漏标注」更难发现。

    读点映射写在断言里（而不是产品代码里）：这份映射是**测试的期望**，
    改了读点位置就该同步改这里（改不动 = 有人换了实现却没换白名单）。
    """
    read_points = {
        "crawl_rate_limit_per_minute": (
            "backend.app.services.settings_service",
            "rate_limit_per_minute_for_key",
        ),
        "xhs_sync_account_parallelism": (
            "backend.app.services.note_sync_service",
            "sync_parallelism",
        ),
        "xhs_sync_account_gap_seconds": (
            "backend.app.services.scheduler_service",
            "_sync_account_gap_seconds",
        ),
        "xhs_allow_duplicate_goods_publish": (
            "backend.app.services.note_publish_service",
            "_allow_duplicate_goods_publish",
        ),
        #: ⚠️ 这条的读点形态与上面四条**不同**（2026-10-11 补）：前四条是「模块里有个
        #: 私有函数包了一层」，而这条直接在**端点里**调
        #: `settings_svc.live_int(db, uid, "xhs_image_upload_parallelism")` ——
        #: 键名字面量就写在端点函数体里。
        #: ⇒ 读点 = 那个端点的**调用方函数**（守卫是「函数体里找得到键名」，不是
        #: 「谁的实现里通用地读配置」）。写 `settings_service.live_int` 会被守卫判红：
        #: 它是通用读点，函数体里没有这个键名（实测踩过）。
        "xhs_image_upload_parallelism": (
            "backend.app.api.platforms.xhs.qianfan.picture_space",
            "picture_space_upload_batch",
        ),
        #: 违规词检测 OCR 的起始并发数（2026-10-13 换 RapidOCR 时新增）。
        #: 读点在 `image_ocr_service.resolve_parallelism()` —— 函数体里
        #: 字面量调 `settings_service.live_int(None, None, "xhs_ocr_parallelism", ...)`。
        "xhs_ocr_parallelism": (
            "backend.app.services.image_ocr_service",
            "resolve_parallelism",
        ),
        #: ⚠️ 下面两个是**他人未提交改动带来的既有红**（2026-10-20 复核：`HEAD` 的
        #: `settings_service.py` 已含这两个键，但 `test_app_settings` 的映射表与
        #: `config/initial_data/system_settings.json` 都没跟上）。本模块的 OCR 轮次
        #: 不认领它们，登记在此只为让本文件的映射表与 `LIVE_KEYS` 保持一致 ——
        #: **它们的读点待实现方补齐**（`xhs_sku_name_max_chars` 有真实读点，
        #: 缺的是种子行；`xhs_note_image_total_limit` 读点在 `media_plan.py`）。
        #: 删掉这两行 = 让守卫重新变红，不要静默移除。
        "xhs_sku_name_max_chars": (
            "backend.app.api.platforms.xhs.package_precheck",
            "optimize_sku_names",
        ),
        "xhs_note_image_total_limit": (
            "backend.app.services.media_plan",
            "resolve_note_image_limit",
        ),
        }
    assert set(read_points) == set(settings_service.LIVE_KEYS), (
        "LIVE_KEYS 与测试里的读点映射必须一一对应：新增读点要两边一起加"
    )

    import importlib

    for key, (module_name, func_name) in read_points.items():
        module = importlib.import_module(module_name)
        assert hasattr(module, func_name), f"{module_name} 缺少读点函数 {func_name}"
        # ⚠️ 断言落在**函数体**而不是模块源码：模块里出现键名可能只是常量表 /
        # 文档字符串（那种「读点」永远不会被执行），函数体里出现才是真的在读它。
        source = inspect.getsource(getattr(module, func_name))
        assert key in source, (
            f"{module_name}.{func_name} 的函数体里找不到 {key} —— 该键没有真实读点"
        )


def test_builtin_defaults_cover_exactly_the_live_keys():
    """内置兜底只给**有读点**的键编默认值（给没人读的键编默认值毫无意义）。"""
    assert set(settings_service.BUILTIN_DEFAULTS) == set(settings_service.LIVE_KEYS)


# --------------------------------------------------------------------------- #
# 5) 前端行为脚本（必须**真的被 pytest 执行** —— 只写脚本不算接线）
# --------------------------------------------------------------------------- #
SETTINGS_VIEW_SCRIPT = "tests/frontend/settings_view.cjs"

#: 五个变异开关：每个都必须让脚本变红（「守卫要过变异验证」，见 AGENTS §2 三条硬规矩）。
SETTINGS_VIEW_MUTATIONS = ("booltext", "dashzero", "boolraw", "guesslive", "category")


def test_settings_view_behaviour_script(frontend_runner):
    """设置页纯逻辑（值渲染 / 类型分派 / 生效性 / 分组筛选）+ 五个变异各变一次红。"""
    result = frontend_runner("settings_view.cjs")
    assert result.returncode == 0, result.stdout
    assert "23 PASS / 0 FAIL" in result.stdout

    for mutate in SETTINGS_VIEW_MUTATIONS:
        mutated = frontend_runner("settings_view.cjs", {"SETTINGS_VIEW_MUTATE": mutate})
        assert mutated.returncode != 0, (
            f"变异 {mutate} 没让脚本变红 —— 该断言是空的（锚点失配 / 断言不承重）\n"
            f"{mutated.stdout}"
        )
        assert "FAIL" in mutated.stdout, mutated.stdout


SETTINGS_PAGE = ROOT / "frontend" / "src" / "pages" / "settings" / "settings-page.tsx"


def _strip_ts_noise(source: str) -> str:
    """去掉注释（`//` 与 `/* */`）—— 免得「注释里提一句」被当成接线证据。

    ⚠️ 两种都要剥：只剥 `//` 时，写在 JSDoc 里的 `setFieldsValue` 会把下面的
    负向断言打红（本项目在 `test_page_optimizations` 的 H9 守卫上踩过同一个坑）。
    """
    import re

    without_block = re.sub(r"/\*[\s\S]*?\*/", "", source)
    return re.sub(r"(?m)//.*$", "", without_block)


def _function_body(source: str, name: str) -> str:
    """取一个**箭头函数**的函数体（到下一个顶层 `};` 为止）。

    够用即可：本文件的断言只需要「这段代码里有没有某个调用」，不需要真解析 TS。
    """
    marker = f"const {name} = ("
    start = source.find(marker)
    assert start != -1, f"页面里找不到 {name}（实现改了？守卫要跟着改）"
    end = source.find("\n  };", start)
    assert end != -1, f"{name} 的函数体没找到结束标记"
    return source[start:end]


def test_settings_modal_fills_the_form_after_it_opens():
    """编辑弹窗必须**在打开之后**灌值（打开前灌 = 字段全空，真机复现过）。

    现场（2026-09-27，用户报「编辑配置弹窗未回显参数」）：CDP 读真实 DOM ——
    弹窗标题正确（`编辑配置 · xhs_sync_account_gap_seconds`，说明 `editing` 生效了），
    但 `key` / `value_type` / `category` / `number_value` / `label` / `description`
    **六个字段全空**。根因：弹窗带 `destroyOnClose` ⇒ 关闭时 `Form` **整个被卸载**；
    在 `setModalOpen(true)` **之前**调 `form.setFieldsValue()`，值虽写进 FormStore，
    但 Form 挂载时被丢掉。

    修法 = 把灌值搬进 `useEffect(..., [modalOpen, editing])`（打开后的那次 commit
    Form 已挂载、字段已注册）。修完 CDP 复验：六个字段全部回显。

    ⚠️ 这条只能靠**结构守卫**钉住：纯逻辑脚本（`settings_view.cjs`）证明不了
    「值有没有真的进到输入框」，那是 React 挂载时序的事。
    """
    code = _strip_ts_noise(SETTINGS_PAGE.read_text(encoding="utf-8"))

    # ① 两个打开入口里**不许**再出现 setFieldsValue（那就是旧写法）
    for opener in ("openCreate", "openEdit"):
        body = _function_body(code, opener)
        assert "setFieldsValue" not in body, (
            f"{opener} 里又出现 setFieldsValue 了 —— 弹窗打开前灌值 = 字段全空"
            "（destroyOnClose 会把 Form 卸载）。灌值要放在打开后的 useEffect 里。"
        )

    # ② 必须存在「打开后灌值」的那段：useEffect 同时出现 modalOpen 与 setFieldsValue
    assert "useEffect(() => {" in code, "灌值逻辑不见了（页面改成什么了？）"
    fill_effects = [
        block
        for block in code.split("useEffect(() => {")[1:]
        if "setFieldsValue" in block and "modalOpen" in block
    ]
    assert fill_effects, (
        "找不到「弹窗打开后灌值」的 useEffect（判据：同一段里同时有 modalOpen 与 setFieldsValue）"
    )


def test_settings_modal_does_not_use_preserve_false():
    """弹窗里的 Form **不写** `preserve={false}`。

    它会在字段卸载时清掉 store 里的值，与「打开后再灌值」的时序叠在一起，只会让
    「值什么时候还在」更难判断（真机排查时这一点额外花了时间）。弹窗本来就有
    `destroyOnClose`（每次打开都是新的），不需要再加一层。
    """
    code = _strip_ts_noise(SETTINGS_PAGE.read_text(encoding="utf-8"))
    assert "preserve={false}" not in code, (
        "`preserve={false}` 会让字段卸载时清值 —— 与「打开后灌值」的时序叠加，"
        "属于自找的复杂度。要么删掉它，要么把这里的理由写清楚再改断言。"
    )
