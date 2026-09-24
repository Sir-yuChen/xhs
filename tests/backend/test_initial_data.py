"""应用初始化数据（设计 15 §4.5，R24）—— 数据文件 / 播种链路 / 打包 / 迁移守卫。

用户口径（2026-09-24）：「将当前的定时任务配置数据定义为应用初始化数据，
随应用的安装自动附有」。

本文件盯四件事，**任何一件坏了「装完即有用」就落空**：

1. **数据文件本身**：存在、可解析、`job_key` 集合 = 注册表 builtin 全集（不多不少）、
   字段全在白名单内；
2. **播种链路**：启动即播种（**与调度器开关解耦**）、值取自数据文件、幂等、
   **已有行不覆盖**（升级不重置用户配置）、文件坏/缺 ⇒ 退化代码默认并 **warning**（不静默）；
3. **打包链路**：`build.ps1` 与 `SpiderXHS.spec` 都收了 `config/initial_data`（漏一处 = 装完空）；
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

client = TestClient(app)

#: 仓库根（不依赖 cwd）
ROOT = Path(__file__).resolve().parents[2]

#: 数据文件与打包脚本的真实路径
INITIAL_DATA_FILE = ROOT / "config" / "initial_data" / "scheduled_jobs.json"
BUILD_PS1 = ROOT / "packaging" / "build.ps1"
SPEC_FILE = ROOT / "packaging" / "SpiderXHS.spec"


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
    assert len(rows) == 7, "定时任务初始化数据条数变化必须同步审阅（注册表 builtin 全集）"


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
        # DEF-066：内置行按用户播种 ⇒ 先有用户才有 7 行（原全局行形态已废）
        _make_user(TestingSessionLocal)
        # 直接调启动播种（它就是 lifespan 里那一步），不启调度器
        created = scheduler_service.seed_builtin_jobs_from_initial_data()
        assert created == 7

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
        assert len(created) == 6, "已存在的那一行不该再插"

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
        assert len(created) == 7, "文件缺失也要有兜底种子（不能少任务）"
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


def test_build_has_a_positive_assertion_for_initial_data():
    """构建自检必须**正向断言**数据文件在包内（只查禁止项会漏掉「该有的没有」）。"""
    build_text = BUILD_PS1.read_text(encoding="utf-8-sig")
    assert "scheduled_jobs.json" in build_text
    assert "MISSING" in build_text


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
def test_migration_no_longer_seeds_scheduled_jobs(tmp_path):
    """真跑 `alembic upgrade head`：`scheduled_jobs` **零行** —— 种子只由初始化数据产生。"""
    import os
    import sqlite3
    import subprocess
    import sys

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
        conn.commit()
    finally:
        conn.close()

    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    # stamp 到本迁移的父 revision（夹具库只有这两张表，从初始迁移跑会撞「表已存在」）
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "backend/alembic.ini", "stamp", "x7y8z9a0b1c2"],
        cwd=os.getcwd(),
        env=env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "backend/alembic.ini", "upgrade", "head"],
        cwd=os.getcwd(),
        env=env,
        check=True,
        capture_output=True,
    )

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
