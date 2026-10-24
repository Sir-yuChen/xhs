"""应用初始化数据（设计 15 §4.5，R24）—— 数据文件 / 播种链路 / 打包 / 迁移守卫。

用户口径（2026-09-24）：「将当前的定时任务配置数据定义为应用初始化数据，
随应用的安装自动附有」。
用户口径（2026-09-27）：「重新打包 config 表的当前数据添加到种子数据中」
⇒ 同一套机制覆盖**运行时配置**（`config/initial_data/system_settings.json`，
设计 10 §7.3），本文件同时盯住它（§1b）。

本文件盯四件事，**任何一件坏了「装完即有用」就落空**：

1. **数据文件本身**：存在、可解析、`job_key` 集合 = 注册表 builtin 全集（不多不少）、
   字段全在白名单内；运行时配置侧同理（键集合 = `LIVE_KEYS`、**三处基线同值**）；
2. **播种链路**：启动即播种（**与调度器开关解耦**）、值取自数据文件、幂等、
   **已有行不覆盖**（升级不重置用户配置）、文件坏/缺 ⇒ 退化代码默认并 **warning**（不静默）；
3. **打包链路**：`build.ps1` 与 `SpiderXHS.spec` 都收了 `config/initial_data`（漏一处 = 装完空），
   且**每个数据文件都有一条正向断言**（构建自检 + 冒烟）；
4. **迁移侧**：`y8z9a0b1c2d3` 不再播种（否则判重会挡住初始化数据，修都修不掉）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.core import initial_data
from backend.app.main import app
from backend.app.models import ScheduledJob, User



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.integration

client = TestClient(app)

#: 仓库根（不依赖 cwd）
ROOT = Path(__file__).resolve().parents[2]

#: 数据文件与打包脚本的真实路径
INITIAL_DATA_FILE = ROOT / "config" / "initial_data" / "scheduled_jobs.json"
SETTINGS_DATA_FILE = ROOT / "config" / "initial_data" / "system_settings.json"
BUILD_PS1 = ROOT / "packaging" / "build.ps1"
SPEC_FILE = ROOT / "packaging" / "SpiderXHS.spec"
SMOKE_TEST_PS1 = ROOT / "packaging" / "smoke-test.ps1"


def _make_user(SessionLocal, username: str = "seed-owner") -> int:
    """建一个用户行（DEF-066：内置行按用户播种 ⇒ 播种用例必须先有用户）。"""
    db = SessionLocal()
    try:
        user = User(username=username, password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _logging_stays_usable():
    """复位被**别的用例**破坏的 logging 状态 —— 否则 `caplog` 一条 warning 也抓不到。

    实测（2026-09-24 全量回归）：先跑的真 alembic 用例里 `backend/alembic/env.py` 调
    `fileConfig(..., disable_existing_loggers=True)`，而 `backend.app.*` 不在 ini 的
    `[loggers]` 名单里 ⇒ 这些 logger 被 **disable**，之后 `caplog.records` 恒空。
    症状极像「断言写错了」：**单独跑本文件 14 passed，全量跑连红 3 条**。

    这里只做一件事 —— 让 logging 回到可用（**不是**放宽断言：warning 仍必须真打）。

    ⚠️ 2026-10-07 补 `backend.app.services.settings_service`：它的种子回落 warning 是
    `test_settings_seed_falls_back_to_builtin_when_file_value_is_illegal` 的判据，而
    在**同进程**里跑过 `command.upgrade()` 的用例（`test_api.py::test_database_*`）
    会经 `alembic/env.py::fileConfig(disable_existing_loggers=True)` 关掉它 ——
    名单漏一个 logger，就是一条「单跑绿、全量红」的顺序依赖。
    """
    import logging

    logging.disable(logging.NOTSET)
    for name in (
        "backend.app.core.initial_data",
        "backend.app.services.scheduler_service",
        "backend.app.services.settings_service",
    ):
        logger = logging.getLogger(name)
        logger.disabled = False
        logger.propagate = True
    yield


def _override_database(tmp_path):
    from backend.app.core.database import Base, get_db

    engine = create_engine(
        f"sqlite:///{tmp_path / 'initial-data-test.db'}", connect_args={"check_same_thread": False}
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestingSessionLocal


def _point_initial_data_at(tmp_path, monkeypatch, payload) -> Path:
    """把初始化数据指向一个自造文件（测「文件坏/缺/含未知 key」这些分支）。

    打在 `initial_data_candidates` 上（模块内全局查找，等价于换掉整个定位链）——
    比改环境变量干净：`CONFIG_FILE` 是进程级全局，改了会污染同批其它用例。
    """
    path = tmp_path / "scheduled_jobs.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(initial_data, "initial_data_candidates", lambda filename: [path])
    return path


# --------------------------------------------------------------------------- #
# 1. 数据文件本身
# --------------------------------------------------------------------------- #
def test_initial_data_file_exists_and_is_parseable():
    assert INITIAL_DATA_FILE.is_file(), f"应用初始化数据文件不存在：{INITIAL_DATA_FILE}"
    payload = json.loads(INITIAL_DATA_FILE.read_text(encoding="utf-8"))
    rows = payload["scheduled_jobs"]
    assert isinstance(rows, list)
    #: ⚠️ 12（原 14）—— 2026-10-13 第二轮用户口径：指标快照子系统整块移除 ⇒
    #: `system.note_metrics_collect` / `system.shop_metrics_collect` 两条种子退场
    #: （注册表 builtin 全集 14→12，种子同步删条目）。
    #: 前值 14 的原注：2026-10-13 R2 `system.llm_gateway_monitor` 退场（用户口径
    #: 「移除 LLM 网关预警任务」）⇒ 15→14；再前值 15：`system.picture_space_cleanup`
    #: （图片空间清理，设计 22）落地时漏改硬编码数字导致本条长期红，2026-10-11 按
    #: 注册表 builtin 全集审阅后订正为 15。
    assert len(rows) == 13, "定时任务初始化数据条数变化必须同步审阅（注册表 builtin 全集）"  # 13 = 12 + system.package_staging_cleanup（6ef8654，数据包暂存清理）


def test_initial_data_keys_match_registry_builtin_set():
    """`job_key` 集合 **= 注册表 builtin 全集**：少一个 = 新装漏任务，多一个 = 死行。"""
    from backend.app.services.job_registry import job_registry

    registry_builtin = {key for key, spec in job_registry().items() if spec.builtin}
    from_file = {row["job_key"] for row in initial_data.load_initial_scheduled_jobs()}
    assert from_file == registry_builtin


def test_initial_data_fields_are_within_whitelists():
    """逐条字段必须在白名单内（白名单从 models 常量取，不在这里重抄）。"""
    from backend.app.models.scheduled_job import (
        CONCURRENT_MODES,
        JOB_STATUSES,
        MISFIRE_POLICIES,
        TRIGGER_CRON,
        TRIGGER_TYPES,
    )

    for row in initial_data.load_initial_scheduled_jobs():
        key = row["job_key"]
        assert row["trigger_type"] in TRIGGER_TYPES, key
        assert row["status"] in JOB_STATUSES, key
        assert row["misfire_policy"] in MISFIRE_POLICIES, key
        assert row["concurrent"] in CONCURRENT_MODES, key
        assert isinstance(row["params"], dict), key
        assert row["name"], key
        if row["trigger_type"] == TRIGGER_CRON:
            assert row["cron_expression"], f"{key} 是 cron 档却没有表达式"


def test_initial_data_names_match_registry_spec_names():
    """种子里的**中文名**必须 == 注册表 `JobSpec.name`。

    ⚠️ 为什么单独立一条（2026-10-13 实测漏网）：已有的两条守卫只钉
    ①「条数 == 注册表 builtin 全集」②「`job_key` 集合 == 注册表 builtin 全集」——
    都**不看 `name`**。而 `ensure_builtin_jobs` 写新行时用的是 `row["name"]`
    （种子优先、`JobSpec.name` 只在种子缺行时兜底）⇒ **改名只改代码不改种子**时，
    已有用户的旧行被迁移 UPDATE 成新名、而**新装用户的种子行仍是旧名**：
    两种安装形态显示不同名字，且所有测试照绿（本轮实际发生：`system.note_data_sync`
    的 JobSpec 改了名、种子文件漏改，靠人工比对才发现）。

    ⚠️ 变异守护：只改 `JobSpec.name` 不改种子（或反之）⇒ 本用例必红。
    """
    from backend.app.services.job_registry import job_registry

    registry = job_registry()
    for row in initial_data.load_initial_scheduled_jobs():
        key = row["job_key"]
        spec = registry.get(key)
        if spec is None:  # 键集合由上面那条守卫负责，这里只比名字
            continue
        assert row["name"] == spec.name, (
            f"{key} 的中文名两处不一致：种子={row['name']!r} / 注册表={spec.name!r}"
            f"（改名必须两处同改 —— 种子是写入真相源，注册表是文档/兜底真相源）"
        )



# --------------------------------------------------------------------------- #
# 1b. 数据文件本身：运行时配置（设计 10 §7.3，2026-09-27 起随包）
# --------------------------------------------------------------------------- #
def _point_settings_initial_data_at(tmp_path, monkeypatch, payload) -> Path:
    """把**运行时配置**初始化数据指向自造文件（测「值非法 / 文件坏」这些分支）。

    与 `_point_initial_data_at` 同型，只是文件名不同 —— 定位链按**文件名**取，所以替身
    必须只对 `system_settings.json` 生效（否则定时任务那份也被换掉，两个用例互相污染）。
    """
    path = tmp_path / "system_settings.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        initial_data,
        "initial_data_candidates",
        lambda filename: [path] if filename == initial_data.SYSTEM_SETTINGS_FILENAME else [],
    )
    return path


def test_settings_initial_data_covers_every_live_key():
    """配置表种子的键集合 **= `LIVE_KEYS`**（少一个 ⇒ 新装那项回落到代码默认 = 基线漂移）。"""
    from backend.app.services import settings_service

    payload = json.loads(SETTINGS_DATA_FILE.read_text(encoding="utf-8"))
    assert {row["key"] for row in payload["system_settings"]} == set(settings_service.LIVE_KEYS)


def test_settings_initial_data_fields_are_within_whitelists():
    """逐条字段必须在白名单内（白名单从 models 常量取，不在这里重抄）。"""
    from backend.app.models.system_setting import (
        CATEGORY_AI,
        CATEGORY_FEATURE,
        CATEGORY_NOTE,
        CATEGORY_PUBLISH,
        CATEGORY_SCHEDULER,
        CATEGORY_SYNC,
        VALUE_TYPES,
    )

    categories = {
        CATEGORY_SYNC,
        CATEGORY_SCHEDULER,
        CATEGORY_PUBLISH,
        CATEGORY_AI,
        CATEGORY_FEATURE,
        CATEGORY_NOTE,
    }
    payload = json.loads(SETTINGS_DATA_FILE.read_text(encoding="utf-8"))
    for row in payload["system_settings"]:
        key = row["key"]
        assert row["value_type"] in VALUE_TYPES, key
        assert row["category"] in categories, key
        assert row["label"], key
        assert isinstance(row["requires_restart"], bool), key
        assert isinstance(row["is_dangerous"], bool), key


def _literal_builtin_defaults() -> dict:
    """从**源码字面量**里解析 `BUILTIN_DEFAULTS`（不读运行时的 dict）。

    为什么要这么绕：`conftest.py` 的 session 级夹具会把
    `settings_service.BUILTIN_DEFAULTS["register.enabled"]` 改成 `True`
    （否则几十处「注册造用户」的用例全 403）。这条守卫要验的是**产品基线**，
    必须拿到模块定义时那一刻的值，而不是被测试改过之后的。

    做法：`ast.parse` 源码 → 找 `BUILTIN_DEFAULTS` 的赋值 → 只取能字面量求值的项
    （跳过带注解的复杂表达式就报出来，避免「解析不到 ⇒ 悄悄少检查一个键」）。
    """
    import ast
    import pathlib

    import backend.app.services.settings_service as module

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign):
            continue
        target = node.target
        if not (isinstance(target, ast.Name) and target.id == "BUILTIN_DEFAULTS"):
            continue
        if not isinstance(node.value, ast.Dict):
            break
        out: dict = {}
        for k, v in zip(node.value.keys, node.value.values):
            if k is None:
                continue
            try:
                out[ast.literal_eval(k)] = ast.literal_eval(v)
            except ValueError:
                #: 值不是字面量（引用了别的常量）⇒ 用运行时值兜底，但不静默跳过
                out[ast.literal_eval(k)] = module.BUILTIN_DEFAULTS[ast.literal_eval(k)]
        return out
    raise AssertionError("`settings_service.BUILTIN_DEFAULTS` 的字面量定义没找到 —— 守卫会假绿")


def test_settings_baseline_is_one_number_everywhere():
    """「产品基线」三处必须同值：**随包种子 = `BUILTIN_DEFAULTS` = `config/default.yaml`**。

    ⚠️ 这条守卫的来由：额度曾经「声明 60、实际跑 5」（设计 13 §38.1 F2）—— 同一语义留
    两份数字，迟早互相矛盾。2026-09-27 基线下调为 8（设计 13 §38.7）就是三处一起改。
    """
    from backend.app.core.config import get_settings
    from backend.app.services import settings_service

    payload = json.loads(SETTINGS_DATA_FILE.read_text(encoding="utf-8"))
    baseline = {row["key"]: row["value"] for row in payload["system_settings"]}
    settings_obj = get_settings()

    #: ⚠️ **不能用运行时的 `settings_service.BUILTIN_DEFAULTS`**（2026-10-24）。
    #: `tests/backend/conftest.py` 的 session 级夹具会把 `register.enabled` 改成
    #: `True`（否则全仓几十处 `POST /auth/register` 造用户的用例全 403）。
    #: 那条夹具改的是**这个字典本身** ⇒ 拿它跟随包种子比，等于拿「被测试改过的值」
    #: 去比「产品基线」，必然在 `register.enabled` 上红，而代码其实没错。
    #: 判据要的是**模块定义时的字面量**，所以从源码里重新解析一次。
    literal_defaults = _literal_builtin_defaults()

    for key, value in baseline.items():
        assert literal_defaults[key] == value, f"{key}：内置兜底与随包种子不同值"
        #: 配置键带点（`register.enabled`）不能直接 `getattr` —— 那是 YAML 路径名，
        #: 对应的 `Settings` 字段是下划线形态（`register_enabled`）。转换在这里做一次，
        #: 而不是给每个带点的键写特例（`system_settings` 里将来还会有别的带点键）。
        attr = key.replace(".", "_")
        assert hasattr(settings_obj, attr), (
            f"{key}：`Settings` 没有对应字段 `{attr}` —— 随包种子里的键没有配置基线"
        )
        assert getattr(settings_obj, attr) == value, f"{key}：config/default.yaml 与随包种子不同值"


def test_initial_data_files_ship_without_bom_and_loader_tolerates_one(tmp_path, monkeypatch):
    """随包形态 = **无 BOM 的纯 UTF-8**；读取侧则宽容 BOM（用户可见、可编辑，编辑器会加）。

    ⚠️ 为什么这条要单独钉：带 BOM 的副本会让 `json.load` 抛 `JSONDecodeError`
    ⇒ **整份种子静默退化成代码默认**（新装用户拿到的是兜底值，不是基线）。
    2026-09-27 实测踩到过一次（改文件时被加上了 BOM）。
    """
    for path in (INITIAL_DATA_FILE, SETTINGS_DATA_FILE):
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} 带 BOM（随包形态必须无 BOM）"
        raw.decode("utf-8")  # 能按 utf-8 解出 = 不是别的编码

    bom_path = tmp_path / "system_settings.json"
    bom_path.write_bytes(b"\xef\xbb\xbf" + SETTINGS_DATA_FILE.read_bytes())
    monkeypatch.setattr(
        initial_data,
        "initial_data_candidates",
        lambda filename: [bom_path] if filename == initial_data.SYSTEM_SETTINGS_FILENAME else [],
    )
    assert len(initial_data.load_initial_system_settings()) == len(
        json.loads(SETTINGS_DATA_FILE.read_text(encoding="utf-8"))["system_settings"]
    ), "带 BOM 的副本也必须能读全（条数与源文件一致；不写死数字，新增键不用改这条）"


def test_settings_seed_value_comes_from_the_data_file(db_factory, tmp_path, monkeypatch):
    """播种值来自数据文件（不是 `BUILTIN_DEFAULTS` / YAML 推导值）。"""
    from backend.app.services import settings_service

    _point_settings_initial_data_at(
        tmp_path,
        monkeypatch,
        {
            "version": 1,
            "system_settings": [
                {"key": "crawl_rate_limit_per_minute", "value": 42, "value_type": "int"},
            ],
        },
    )
    db = db_factory()
    try:
        user = User(username="settings-seed-owner", password_hash="x")
        db.add(user)
        db.commit()
        settings_service.seed_settings_for_user(db, user.id)
        db.commit()
        row = settings_service.find_setting(db, user.id, "crawl_rate_limit_per_minute")
        assert row is not None and row.value == "42", "播种值必须取自数据文件"
    finally:
        db.close()


def test_settings_seed_falls_back_to_builtin_when_file_value_is_illegal(
    db_factory, tmp_path, monkeypatch, caplog
):
    """文件里的值非法（int 键给了 `"abc"`）⇒ 内置默认 + **显式 warning**（不静默、不抛）。"""
    from backend.app.services import settings_service

    _point_settings_initial_data_at(
        tmp_path,
        monkeypatch,
        {
            "version": 1,
            "system_settings": [
                {"key": "crawl_rate_limit_per_minute", "value": "abc", "value_type": "int"},
            ],
        },
    )
    db = db_factory()
    try:
        user = User(username="settings-bad-value", password_hash="x")
        db.add(user)
        db.commit()
        with caplog.at_level("WARNING"):
            settings_service.seed_settings_for_user(db, user.id)
            db.commit()
        row = settings_service.find_setting(db, user.id, "crawl_rate_limit_per_minute")
        fallback = settings_service.BUILTIN_DEFAULTS["crawl_rate_limit_per_minute"]
        assert row.value == str(fallback)
        assert any("非法" in record.message for record in caplog.records)
    finally:
        db.close()


def test_settings_missing_or_broken_data_file_degrades_with_warning(tmp_path, monkeypatch, caplog):
    """文件缺失 / 顶层缺数组 ⇒ 空列表 + warning（初始化数据是增强，坏掉不该让应用起不来）。"""
    monkeypatch.setattr(
        initial_data, "initial_data_candidates", lambda filename: [tmp_path / "nope.json"]
    )
    with caplog.at_level("WARNING"):
        assert initial_data.load_initial_system_settings() == []
    assert any("不存在" in record.message for record in caplog.records)

    path = tmp_path / "system_settings.json"
    path.write_text(json.dumps({"version": 1}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(initial_data, "initial_data_candidates", lambda filename: [path])
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert initial_data.load_initial_system_settings() == []
    assert any("缺少 system_settings" in record.message for record in caplog.records)


# --------------------------------------------------------------------------- #
# 2. 播种链路
# --------------------------------------------------------------------------- #
def test_startup_seeding_is_decoupled_from_scheduler_switch(tmp_path, monkeypatch):
    """启动播种**不依赖调度器开关**（桌面版默认 `scheduler.enabled=false`）。

    这是「随安装自动附有」的核心判据：若播种只挂在 `start_scheduler()` 里，
    关着调度器的安装版打开任务中心会是空的。
    """
    from backend.app.services import scheduler_service

    TestingSessionLocal = _override_database(tmp_path)
    monkeypatch.setattr(scheduler_service, "SessionLocal", TestingSessionLocal)
    try:
        # DEF-066：内置行按用户播种 ⇒ 先有用户才有 10 行（原全局行形态已废）
        _make_user(TestingSessionLocal)
        # 直接调启动播种（它就是 lifespan 里那一步），不启调度器
        created = scheduler_service.seed_builtin_jobs_from_initial_data()
        assert created == 13  # 注册表 builtin 全集的条数（14→12 指标快照退场，再 +1 = system.package_staging_cleanup，6ef8654）

        db = TestingSessionLocal()
        keys = set(db.scalars(select(ScheduledJob.job_key)).all())
        rows = db.scalars(select(ScheduledJob)).all()
        db.close()
        assert keys == {row["job_key"] for row in initial_data.load_initial_scheduled_jobs()}
        assert all(row.user_id is not None for row in rows), "DEF-066：不许再有全局行"
    finally:
        app.dependency_overrides.clear()


def test_seed_values_come_from_the_data_file(tmp_path, monkeypatch):
    """播种值逐字段来自数据文件（不是代码默认 / config 推导值）。"""
    from backend.app.services.scheduler_service import ensure_builtin_jobs

    TestingSessionLocal = _override_database(tmp_path)
    try:
        user_id = _make_user(TestingSessionLocal)
        db = TestingSessionLocal()
        ensure_builtin_jobs(db)
        rows = {job.job_key: job for job in db.scalars(select(ScheduledJob)).all()}
        db.close()
    finally:
        app.dependency_overrides.clear()

    for expected in initial_data.load_initial_scheduled_jobs():
        row = rows[expected["job_key"]]
        assert row.name == expected["name"]
        assert row.trigger_type == expected["trigger_type"]
        assert row.cron_expression == expected["cron_expression"]
        assert row.status == expected["status"]
        assert row.params == expected["params"]
        assert row.misfire_policy == expected["misfire_policy"]
        assert row.concurrent == expected["concurrent"]
        assert row.is_builtin is True
        # DEF-066：归属播种时的那个用户（原全局行 user_id IS NULL 已废）
        assert row.user_id == user_id


def test_existing_rows_are_never_overwritten(tmp_path, monkeypatch):
    """已有行**一律不覆盖**：升级安装不许把用户调过的配置重置回初始化值。"""
    from backend.app.services.scheduler_service import ensure_builtin_jobs

    TestingSessionLocal = _override_database(tmp_path)
    try:
        user_id = _make_user(TestingSessionLocal)
        db = TestingSessionLocal()
        db.add(
            ScheduledJob(
                user_id=user_id,
                name="我改过的名字",
                job_key="system.note_sync",
                trigger_type="cron",
                cron_expression="0 30 23 * * *",  # 用户改过的时刻
                status="paused",  # 用户手动停的
                params={"account_ids": [42]},
                is_builtin=True,
            )
        )
        db.commit()

        created = ensure_builtin_jobs(db)
        # ⚠️ **从真相源推导**，不写死数字（写死的数字随种子增减漂移，历史上已多次踩到）：
        # 新建条数 = 注册表 builtin 全集 − 已存在的那 1 行（`system.note_sync`）。
        # 演变留档：11 →13 →14 →13（10-07 `no_traffic_cleanup` 移除）
        # →14（10-11 订正）→13（2026-10-13 R2 `llm_gateway_monitor` 移除）。
        from backend.app.services.job_registry import job_registry
        from backend.app.core.initial_data import load_initial_scheduled_jobs

        seed_total = len(load_initial_scheduled_jobs())
        builtin_total = sum(1 for spec in job_registry().values() if spec.builtin)
        assert builtin_total == seed_total, "注册表 builtin 全集应等于种子条数"
        assert len(created) == seed_total - 1, "已存在的那一行不该再插"

        row = db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == "system.note_sync"))
        assert row.name == "我改过的名字"
        assert row.cron_expression == "0 30 23 * * *"
        assert row.status == "paused"
        assert row.params == {"account_ids": [42]}
        db.close()
    finally:
        app.dependency_overrides.clear()


def test_missing_data_file_falls_back_to_code_defaults_with_warning(tmp_path, monkeypatch, caplog):
    """文件缺失 ⇒ 退化代码默认 + **显式 warning**（不静默、不抛）。"""
    from backend.app.services.scheduler_service import ensure_builtin_jobs

    monkeypatch.setattr(
        initial_data, "initial_data_candidates", lambda filename: [tmp_path / "nope.json"]
    )
    TestingSessionLocal = _override_database(tmp_path)
    try:
        assert initial_data.load_initial_scheduled_jobs() == []
        with caplog.at_level("WARNING"):
            db = TestingSessionLocal()
            _make_user(TestingSessionLocal)
            created = ensure_builtin_jobs(db)
            db.close()
        assert len(created) == 13, "文件缺失也要有兜底种子（不能少任务）"
        # ⚠️ 12 = 注册表 builtin 全集的条数（2026-10-13 指标快照两任务退场 ⇒ 14→12）。
        assert any("不存在" in record.message for record in caplog.records)
    finally:
        app.dependency_overrides.clear()


def test_unknown_job_key_in_data_file_is_skipped(tmp_path, monkeypatch, caplog):
    """数据文件里出现注册表没有的 key ⇒ **跳过 + warning**（不插没有 func 的死行）。"""
    from backend.app.services.scheduler_service import ensure_builtin_jobs

    _point_initial_data_at(
        tmp_path,
        monkeypatch,
        {
            "version": 1,
            "scheduled_jobs": [
                {
                    "job_key": "system.due_publish",
                    "name": "定时发布补跑",
                    "trigger_type": "cron",
                    "cron_expression": "0 * * * * *",
                    "status": "active",
                },
                {
                    "job_key": "system.not_in_registry",
                    "name": "野任务",
                    "trigger_type": "cron",
                    "cron_expression": "0 0 * * * *",
                    "status": "active",
                },
            ],
        },
    )
    TestingSessionLocal = _override_database(tmp_path)
    try:
        with caplog.at_level("WARNING"):
            db = TestingSessionLocal()
            _make_user(TestingSessionLocal)
            ensure_builtin_jobs(db)
            keys = set(db.scalars(select(ScheduledJob.job_key)).all())
            db.close()
        assert "system.not_in_registry" not in keys
        assert any("不在注册表" in record.message for record in caplog.records)
    finally:
        app.dependency_overrides.clear()


def test_invalid_entry_is_dropped_with_warning(tmp_path, monkeypatch, caplog):
    """单条字段非法 ⇒ 丢该条 + warning，其余照常（部分可用胜过全丢）。"""
    _point_initial_data_at(
        tmp_path,
        monkeypatch,
        {
            "version": 1,
            "scheduled_jobs": [
                {
                    "job_key": "system.due_publish",
                    "trigger_type": "interval",  # R14 已退役的档位
                    "cron_expression": "",
                    "status": "active",
                },
                {
                    "job_key": "system.monitoring_refresh",
                    "trigger_type": "cron",
                    "cron_expression": "0 */5 * * * *",
                    "status": "active",
                },
                {"job_key": "", "trigger_type": "cron", "status": "active"},
            ],
        },
    )
    with caplog.at_level("WARNING"):
        rows = initial_data.load_initial_scheduled_jobs()
    assert [row["job_key"] for row in rows] == ["system.monitoring_refresh"]
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) >= 2


# --------------------------------------------------------------------------- #
# 3. 打包链路（静态守卫：漏一处 = 安装包缺数据 / 混进测试代码）
# --------------------------------------------------------------------------- #
def test_packaging_ships_the_initial_data():
    """`build.ps1` 与 `SpiderXHS.spec` **都必须**收 `initial_data`（两条路互补）。"""
    build_text = BUILD_PS1.read_text(encoding="utf-8-sig")
    spec_text = SPEC_FILE.read_text(encoding="utf-8")
    assert "initial_data" in build_text, "build.ps1 组装段没复制 config/initial_data"
    assert "config/initial_data" in spec_text, "SpiderXHS.spec 的 datas 没收 initial_data"


def test_packaging_ships_every_lazily_imported_third_party_package():
    """**函数内延迟 import 的第三方包必须在 hiddenimports 里**（2026-10-20）。

    为什么这是「装包版专属」缺陷：PyInstaller 的静态分析只看模块顶层，
    写在函数体 / try 块里的 `import x` **跟不到** ⇒ 包里没有它，
    而开发环境一切正常（site-packages 里有）。现场三次都栽在这个形状上：

    | 包 | 缺了会怎样（只有装包版才有） |
    |---|---|
    | `rapidocr` | 违规词预检 500 |
    | `openpyxl` | 数据包 xlsx 导入报「缺少 openpyxl 依赖」 |
    | `alembic` | 启动静默退化成 `create_all`，老库不升级表结构 |
    | `send2trash` | 素材删除抛错（代码刻意不回落 `unlink`） |

    判据：每个都必须出现在 spec 的 `_safe_submodules(...)` 调用里。

    ⚠️ 2026-10-20：OCR 引擎由 `rapidocr_onnxruntime`(1.x) 换成 `rapidocr`(3.x)，
    spec 里改用 `_OCR_PKG_NAME` 常量（一处改、两处用），所以这里按**常量 +
    `_safe_submodules(_OCR_PKG_NAME)`** 断言，而不是字面包名 —— 换包名时守卫
    仍要盯着「这个延迟导入的包真的进了 hiddenimports」。
    """
    spec_text = SPEC_FILE.read_text(encoding="utf-8")
    assert '_OCR_PKG_NAME = "rapidocr"' in spec_text, (
        "spec 里的 OCR 包名常量丢了（换引擎时会漏收）"
    )
    assert "_safe_submodules(_OCR_PKG_NAME)" in spec_text, (
        "SpiderXHS.spec 的 hiddenimports 没收 OCR 引擎（延迟导入，静态分析跟不到）"
    )
    for pkg in (
        "openpyxl",
        "alembic",
        "send2trash",
    ):
        assert f'_safe_submodules("{pkg}")' in spec_text, (
            f"SpiderXHS.spec 没收集延迟导入的 `{pkg}` —— 装包版会缺这个模块"
        )
    #: 收集器本身必须容错（可选依赖如 psutil 未装时不能让构建整条挂掉）
    assert "def _safe_submodules(" in spec_text, "缺 _safe_submodules：可选依赖未装会让构建失败"


def test_packaging_ships_rapidocr_and_its_onnx_models():
    """RapidOCR（违规词检测主引擎）**必须**进包，且 3 个 onnx 模型也在。

    现场（2026-10-20，1.0.4 安装版）：点「预检 / 检查违规词」报
    「检查违规词异常 预检失败：Request failed with status code 500」。
    **开发环境不复现** —— 这是本类缺陷最贵的地方：只有装好的客户端才炸。

    根因两层，两条都要钉：
    ① `rapidocr` 在 `rapid_ocr_service` 里是**函数内延迟导入**，
       PyInstaller 静态分析跟不到 ⇒ 必须进 `hiddenimports`；
    ② 它的 `models/*.onnx` 是包内数据文件（不是 import 进来的），
       必须显式进 `datas`，否则引擎在、模型不在，照样起不来。

    ⚠️ 2026-10-20：引擎换成 `rapidocr` 3.x（PP-OCRv5）后，模型**不再随包内置**，
    而是首次使用时下载到 `rapidocr/models/`。所以额外要求：`_rapidocr_data()`
    必须整包收集（含 `models/` 与 `config.yaml`），否则装包版第一次预检要联网。
    """
    spec_text = SPEC_FILE.read_text(encoding="utf-8")
    #: ① 模块本身
    assert "_safe_submodules(_OCR_PKG_NAME)" in spec_text, (
        "SpiderXHS.spec 的 hiddenimports 没收 OCR 引擎（延迟导入，静态分析跟不到）"
    )
    #: ② 模型文件（整包目录按 __file__ 相对路径收）
    assert "def _rapidocr_data()" in spec_text, "缺 _rapidocr_data()：onnx 模型不会被收集"
    assert "datas.extend(_rapidocr_data())" in spec_text, "_rapidocr_data() 算了却没并进 datas"
    assert "importlib.import_module(_OCR_PKG_NAME)" in spec_text, (
        "_rapidocr_data 应按常量动态导入，否则换引擎又要改两处"
    )


def test_build_has_a_positive_assertion_for_every_initial_data_file():
    """构建自检必须**逐文件正向断言**（只查禁止项会漏掉「该有的没有」）。

    ⚠️ 2026-09-27 补 `system_settings.json`：此前只断言 `scheduled_jobs.json`，
    配置表种子漏进包时自检照样「全部通过」—— **一个数据文件一条断言**，不许合并成目录级。
    """
    build_text = BUILD_PS1.read_text(encoding="utf-8-sig")
    assert "MISSING" in build_text
    for name in (
        "scheduled_jobs.json",
        "system_settings.json",
        "llm_gateway_providers.json",
    ):
        assert name in build_text, f"build.ps1 自检没有正向断言 {name}"


def test_smoke_test_checks_every_initial_data_file_and_its_rows():
    """冒烟必须：① 文件清单含每个数据文件；② 可解析；③ **库里真的铺了行**。

    判据落在「行」上而不是「文件」上（`.hermes/skills/xhs-app-initial-data` §3）：
    文件在而没播种 = 用户打开任务中心 / 设置页还是空的。
    """
    smoke_text = SMOKE_TEST_PS1.read_text(encoding="utf-8-sig")
    for name in (
        "scheduled_jobs.json",
        "system_settings.json",
        "llm_gateway_providers.json",
    ):
        assert name in smoke_text, f"smoke-test.ps1 没有校验 {name}"
    assert "select id from system_settings" in smoke_text, "冒烟没有查配置行的落库判据"
    assert "运行时配置播种失效" in smoke_text, "冒烟查到行数却没有失败判据（只打印不断言）"


def test_tests_are_not_packaged():
    """测试代码不进安装包（2026-09-24 用户口径）—— spec 排除 + 构建自检禁止项。"""
    spec_text = SPEC_FILE.read_text(encoding="utf-8")
    assert "excludes=" in spec_text
    assert '"pytest"' in spec_text and '"tests"' in spec_text

    build_text = BUILD_PS1.read_text(encoding="utf-8-sig")
    for forbidden in ("_internal\\tests", "_internal\\pytest", "_internal\\_pytest"):
        assert forbidden in build_text, f"build.ps1 自检缺少禁止项 {forbidden}"




@pytest.mark.parametrize(
    "migration_name", ["y8z9a0b1c2d3_scheduled_task_center.py"]
)
def test_migration_file_keeps_the_historical_seed_constant(migration_name):
    """原播种常量 `_SYSTEM_SEEDS` 必须**保留**（结论被推翻要留原句，不静默删除）。"""
    path = ROOT / "backend" / "alembic" / "versions" / migration_name
    text = path.read_text(encoding="utf-8")
    assert "_SYSTEM_SEEDS" in text
    assert "R24" in text, "被取代的事实必须写在迁移里（含取代者）"
