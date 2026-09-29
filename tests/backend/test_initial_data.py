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
from tests.backend.conftest import create_later_migration_tables, run_alembic

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
    """
    import logging

    logging.disable(logging.NOTSET)
    for name in ("backend.app.core.initial_data", "backend.app.services.scheduler_service"):
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
    assert len(rows) == 8, "定时任务初始化数据条数变化必须同步审阅（注册表 builtin 全集）"


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
    }
    payload = json.loads(SETTINGS_DATA_FILE.read_text(encoding="utf-8"))
    for row in payload["system_settings"]:
        key = row["key"]
        assert row["value_type"] in VALUE_TYPES, key
        assert row["category"] in categories, key
        assert row["label"], key
        assert isinstance(row["requires_restart"], bool), key
        assert isinstance(row["is_dangerous"], bool), key


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
    for key, value in baseline.items():
        assert settings_service.BUILTIN_DEFAULTS[key] == value, f"{key}：内置兜底与随包种子不同值"
        assert getattr(settings_obj, key) == value, f"{key}：config/default.yaml 与随包种子不同值"


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
        # DEF-066：内置行按用户播种 ⇒ 先有用户才有 8 行（原全局行形态已废）
        _make_user(TestingSessionLocal)
        # 直接调启动播种（它就是 lifespan 里那一步），不启调度器
        created = scheduler_service.seed_builtin_jobs_from_initial_data()
        assert created == 8

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
        assert len(created) == 7, "已存在的那一行不该再插"

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
        assert len(created) == 8, "文件缺失也要有兜底种子（不能少任务）"
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


def test_build_has_a_positive_assertion_for_every_initial_data_file():
    """构建自检必须**逐文件正向断言**（只查禁止项会漏掉「该有的没有」）。

    ⚠️ 2026-09-27 补 `system_settings.json`：此前只断言 `scheduled_jobs.json`，
    配置表种子漏进包时自检照样「全部通过」—— **一个数据文件一条断言**，不许合并成目录级。
    """
    build_text = BUILD_PS1.read_text(encoding="utf-8-sig")
    assert "MISSING" in build_text
    for name in ("scheduled_jobs.json", "system_settings.json"):
        assert name in build_text, f"build.ps1 自检没有正向断言 {name}"


def test_smoke_test_checks_every_initial_data_file_and_its_rows():
    """冒烟必须：① 文件清单含每个数据文件；② 可解析；③ **库里真的铺了行**。

    判据落在「行」上而不是「文件」上（`.workbuddy/skills/xhs-app-initial-data` §3）：
    文件在而没播种 = 用户打开任务中心 / 设置页还是空的。
    """
    smoke_text = SMOKE_TEST_PS1.read_text(encoding="utf-8-sig")
    for name in ("scheduled_jobs.json", "system_settings.json"):
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


# --------------------------------------------------------------------------- #
# 4. 迁移侧：不再播种（否则判重会挡住初始化数据，修都修不掉）
# --------------------------------------------------------------------------- #
@pytest.mark.slow
def test_migration_no_longer_seeds_scheduled_jobs(tmp_path):
    """真跑 `alembic upgrade head`：`scheduled_jobs` **零行** —— 种子只由初始化数据产生。"""
    import sqlite3

    db_path = tmp_path / "fresh.db"
    conn = sqlite3.connect(db_path)
    try:
        # `users` 必须在：批次迁移用 batch_alter_table 反射整表（含 FK 目标），
        # `scheduled_jobs.user_id` 指向 `users.id`（DEF-062 的教训）。
        conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username VARCHAR(64),
                password_hash VARCHAR(255),
                is_active BOOLEAN DEFAULT 1,
                created_at DATETIME
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE auto_tasks (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                name VARCHAR(128),
                keywords JSON,
                pc_account_id INTEGER,
                creator_account_id INTEGER,
                ai_instruction TEXT,
                schedule_type VARCHAR(32) DEFAULT 'manual',
                schedule_time VARCHAR(32) DEFAULT '09:00',
                schedule_days VARCHAR(64) DEFAULT '',
                schedule_interval_hours INTEGER DEFAULT 24,
                status VARCHAR(32) DEFAULT 'active',
                last_run_at DATETIME,
                next_run_at DATETIME,
                total_published INTEGER DEFAULT 0,
                created_at DATETIME
            )
            """
        )
        # ⚠️ 补上「区间内后续迁移会碰、但本夹具不建」的表（DEF-082）：
        # `upgrade head` 会把 `(x7y8z9a0b1c2, head]` 里的**全部**迁移跑一遍，缺一张表就红。
        create_later_migration_tables(conn)
        conn.commit()
    finally:
        conn.close()

    # stamp 到本迁移的父 revision（夹具库只有这两张表，从初始迁移跑会撞「表已存在」）
    run_alembic(db_path, "stamp", "x7y8z9a0b1c2")
    run_alembic(db_path, "upgrade", "head")

    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "scheduled_jobs" in tables
        count = conn.execute("SELECT COUNT(*) FROM scheduled_jobs").fetchone()[0]
        assert count == 0, "迁移不该再播种定时任务：种子只由应用初始化数据产生（设计 15 §4.5）"
    finally:
        conn.close()


@pytest.mark.parametrize(
    "migration_name", ["y8z9a0b1c2d3_scheduled_task_center.py"]
)
def test_migration_file_keeps_the_historical_seed_constant(migration_name):
    """原播种常量 `_SYSTEM_SEEDS` 必须**保留**（结论被推翻要留原句，不静默删除）。"""
    path = ROOT / "backend" / "alembic" / "versions" / migration_name
    text = path.read_text(encoding="utf-8")
    assert "_SYSTEM_SEEDS" in text
    assert "R24" in text, "被取代的事实必须写在迁移里（含取代者）"
