"""定时任务中心（设计 15，T-909）——注册表白名单 / 触发器校验 / builtin 保护 /
执行包装器（日志 + 退避门 + at 办结）/ auto-tasks 联动 / 种子一致性 /
**账号维度隔离（DEF-066）**。

铁律 recap：
- 注册表是**代码侧白名单**，运行时不可注入（§5）；
- cron 合法性唯一来源是 APScheduler 实解析（§8）；
- 内置行禁删除、禁改 key/params；调度属性可调（§4.1）；
- 失败退避门只挡调度触发，手动「执行一次」永远放行（§6.5）；
- DEF-066：所有行必归属用户（内置行按用户各持一份）；建 / 改 params 同一把尺子
  （`account_ids` 归属校验两条路都过）；调度日志按 `user_id` 快照列隔离。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.app.core.time import shanghai_now
from backend.app.models import ScheduledJob, ScheduledJobLog, User
from tests.backend.conftest import create_later_migration_tables, run_alembic

client = TestClient(app)

#: 仓库根：前端行为脚本 / 迁移文件都用它定位（不依赖 cwd）。
ROOT = Path(__file__).resolve().parents[2]


def _interval_to_cron(seconds: int) -> str:
    """从迁移模块读换算函数（**单一实现**：测试与迁移同源，不重抄一份）。

    R14：interval 退役，老行换算口径钉在 `h8i9j0k1l2m3` 里。
    """
    import importlib.util
    from pathlib import Path

    path = next(Path("backend/alembic/versions").glob("h8i9j0k1l2m3_*.py"))
    spec = importlib.util.spec_from_file_location("migration_g7", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.interval_to_cron(seconds)


def _override_database(tmp_path):
    from backend.app.core.database import Base, get_db

    engine = create_engine(
        f"sqlite:///{tmp_path / 'schedule-test.db'}", connect_args={"check_same_thread": False}
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


def _patch_service_sessions(monkeypatch, TestingSessionLocal):
    """把服务层的 `SessionLocal` 打到测试库上（与 test_scheduler_sync 同法）。

    ⚠️ 只覆盖 FastAPI 依赖不够：`execute_job` / `sync_jobs_from_db` 等服务函数
    用的是**模块级** `SessionLocal`（指向真实运行库）—— 不打桩就会变成对生产库
    的写操作。
    """
    from backend.app.services import scheduled_job_runner, scheduler_service

    monkeypatch.setattr(scheduled_job_runner, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(scheduler_service, "SessionLocal", TestingSessionLocal)


def _register(username: str) -> str:
    response = client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _db(tmp_path):
    """直接开一个指向同一测试库的 session（断言落库用）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'schedule-test.db'}", connect_args={"check_same_thread": False}
    )
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


def _make_user(SessionLocal, username: str = "job-owner") -> int:
    """直接建一个用户行并返回 id（不走 API 的用例用；跨租户用例传不同 username）。"""
    db = SessionLocal()
    try:
        user = User(username=username, password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    finally:
        db.close()


def _user_of(token: str) -> int:
    from backend.app.core.security import decode_token

    return decode_token(token)["user_id"]


# --------------------------------------------------------------------------- #
# 注册表白名单
# --------------------------------------------------------------------------- #
def test_registry_has_exactly_fifteen_entries():
    """注册表全量 15 项（13 系统内置 + 1 业务管线 + 1 工作流前缀）；键集合钉死 —— 新增任务类型 = 发版。

    §44（2026-09-30）：「账号笔记同步」的混合双源拆成两个独立任务 ⇒ 新增
    `system.goods_note_sync`（**商品笔记**，ark 源）。它与 `system.note_sync`（普通笔记，
    creator 源）**各自成行**；⚠️ 与 `system.shop_goods_sync`（**店铺商品台账**，SKU/价格）
    不是一回事，两条键都必须在表里。

    ⚠️ 2026-10-04：M5/M6 新增 `workflow.`（工作流前缀，**动态**）与 `system.order_sync`
    （订单只读同步）⇒ 13 → 15。⚠️ 种子里**只有 13 个内置行**：`workflow.` 与
    `auto_ops.keyword_pipeline` 是**运行时动态建**的，不在 `scheduled_jobs.json` 里。
    """
    from backend.app.services.job_registry import job_registry

    registry = job_registry()
    assert len(registry) == 15
    assert set(registry) == {
        "system.due_publish",
        "system.monitoring_refresh",
        "system.cookie_health_check",
        "system.qianfan_health_check",
        "system.no_traffic_cleanup",
        "system.note_sync",
        "system.shop_goods_sync",
        "system.goods_note_sync",
        "system.queue_record_cleanup",
        "system.llm_gateway_monitor",
        "system.note_metrics_collect",
        "system.shop_metrics_collect",
        "system.order_sync",
        "auto_ops.keyword_pipeline",
        "workflow.",
    }


def test_every_registry_entry_declares_a_chinese_display_name():
    """**每个注册表项都要有中文名**，不许留裸代码键（用户口径 2026-09-25）。

    现场：新增 `system.queue_record_cleanup`（队列记录清理）后，用户报
    「调用目标需要配置中文名称」。根因是注册表 `name` 这条**口径没被守卫钉住** ——
    任务列表列与日志详情弹窗的中文名唯一来源就是 `JobSpec.name`（前端不存映射表），
    漏配一处就直接显示 `system.xxx` 裸键。

    三条判据缺一不可：
    1. 非空；
    2. **不等于 `job_key`**（拿代码键顶中文名 = 没配）；
    3. 含中文字符（配成 `note task cleanup` 这种英文名同样不算「中文名称」）。
    """
    import re

    from backend.app.services.job_registry import job_registry

    for key, spec in job_registry().items():
        assert spec.name and spec.name.strip(), f"{key} 没有配中文名（前端会显示裸代码键）"
        assert spec.name != key, f"{key} 拿代码键顶中文名 = 没配"
        assert re.search(r"[\u4e00-\u9fff]", spec.name), f"{key} 的中文名不含中文：{spec.name}"


def test_create_job_rejects_unknown_job_key(tmp_path):
    """未知 job_key ⇒ 400 `JOB_KEY_UNSUPPORTED`（白名单，运行期不可注入）。"""
    _override_database(tmp_path)
    try:
        token = _register("job-unknown-key")
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(token),
            json={"name": "野任务", "job_key": "system.not_in_registry"},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "JOB_KEY_UNSUPPORTED"
    finally:
        app.dependency_overrides.clear()


def test_create_job_allows_builtin_key(tmp_path):
    """R18：内置目标**也允许新建**（原一律 400 `JOB_BUILTIN_PROTECTED` 已废）。"""
    _override_database(tmp_path)
    try:
        token = _register("job-builtin-create")
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(token),
            json={
                "name": "再来一个巡检",
                "job_key": "system.cookie_health_check",
                "schedule": {"trigger_type": "cron", "cron_expression": "0 0 8 * * *"},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_builtin"] is True
    finally:
        app.dependency_overrides.clear()


def test_create_job_supports_same_target_multiple_times(tmp_path):
    """R18：同一目标可建多条不同调度的任务行（已建过仍可再建，互不冲突）。

    ⚠️ DEF-066：注册即播种 ⇒ 列表里同 key 的行 = 播种 1 + 自建 2 = 3。
    """
    SessionLocal = _override_database(tmp_path)
    try:
        token = _register("job-same-target-twice")
        first = client.post(
            "/api/schedule/jobs",
            headers=_auth(token),
            json={
                "name": "巡检-早",
                "job_key": "system.cookie_health_check",
                "schedule": {"trigger_type": "cron", "cron_expression": "0 0 9 * * *"},
            },
        )
        assert first.status_code == 200, first.text
        second = client.post(
            "/api/schedule/jobs",
            headers=_auth(token),
            json={
                "name": "巡检-晚",
                "job_key": "system.cookie_health_check",
                "schedule": {"trigger_type": "cron", "cron_expression": "0 0 21 * * *"},
            },
        )
        assert second.status_code == 200, second.text
        assert first.json()["id"] != second.json()["id"]

        listed = client.get("/api/schedule/jobs", headers=_auth(token)).json()
        same_key = [
            item for item in listed["items"]
            if item["job_key"] == "system.cookie_health_check"
        ]
        # 注册播种 1 行 + 本测试自建 2 行（DEF-066 前的原断言是 2）
        assert len(same_key) == 3
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# DEF-066 · 账号维度隔离（2026-09-24 用户口径「所有数据按平台账号维度隔离」）
# --------------------------------------------------------------------------- #
def test_create_job_rejects_foreign_account_ids(tmp_path, monkeypatch):
    """建行也过 account_ids 归属校验（原只在 PUT 有 ⇒ 可建引用别人账号的任务行）。"""
    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        owner = _register("create-ids-owner")
        intruder = _register("create-ids-intruder")
        mine_id, _ = _make_pc_and_creator_accounts(SessionLocal, owner)
        _, theirs_id = _make_pc_and_creator_accounts(SessionLocal, intruder)

        # 别人的账号 ⇒ 400（跨租户）；`system.no_traffic_cleanup` 是平台删除操作，
        # 这条路能走通就等于「A 可以定时删 B 的笔记」
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(owner),
            json={
                "name": "越权清理",
                "job_key": "system.no_traffic_cleanup",
                "params": {"account_ids": [theirs_id]},
            },
        )
        assert response.status_code == 400
        assert response.json()["detail"].startswith("ACCOUNT_NOT_OWNED")

        # 自己的账号 ⇒ 200 且落库
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(owner),
            json={
                "name": "本人清理",
                "job_key": "system.no_traffic_cleanup",
                "params": {"account_ids": [mine_id]},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["params"]["account_ids"] == [mine_id]
    finally:
        app.dependency_overrides.clear()


def test_create_job_rejects_non_editable_param_keys(tmp_path, monkeypatch):
    """建行的 params 键也过白名单（`editable ∪ required`）—— 与 PUT 同一把尺子。"""
    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        token = _register("create-keys")
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(token),
            json={
                "name": "夹带私货",
                "job_key": "system.cookie_health_check",
                "params": {"account_ids": [], "hacked": True},
            },
        )
        assert response.status_code == 400
        assert response.json()["detail"].startswith("JOB_PARAMS_NOT_EDITABLE")
    finally:
        app.dependency_overrides.clear()


def test_execute_job_refuses_row_without_owner(tmp_path, monkeypatch):
    """user_scoped 任务的行归属为 NULL ⇒ 响亮失败，不静默跑成「全库账号」。"""
    from backend.app.services.scheduled_job_runner import execute_job

    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        db = SessionLocal()
        job = ScheduledJob(
            user_id=None,  # 迁移前的历史行 / 脏数据
            name="无主任务",
            job_key="system.cookie_health_check",
            trigger_type="cron",
            cron_expression="0 0 * * * *",
            status="active",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
        db.close()

        outcome = execute_job(job_id, trigger_type="scheduled")
        assert outcome["status"] == "failed"

        db = SessionLocal()
        log = db.scalars(
            select(ScheduledJobLog).where(ScheduledJobLog.job_id == job_id)
        ).first()
        assert log is not None
        assert "隔离" in log.message
        assert db.get(ScheduledJob, job_id).fail_count == 1
        db.close()
    finally:
        app.dependency_overrides.clear()


def test_logs_are_isolated_between_users(tmp_path, monkeypatch):
    """调度日志按用户收口：列表看不见 / 删不掉 / 清空不越界（原三端点零过滤）。"""
    from backend.app.services.scheduled_job_runner import execute_job

    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        owner = _register("logs-owner")
        intruder = _register("logs-intruder")
        db = SessionLocal()
        job = ScheduledJob(
            user_id=_user_of(owner),
            name="主人的任务",
            job_key="system.monitoring_refresh",
            trigger_type="cron",
            cron_expression="0 0 * * * *",
            status="active",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
        db.close()
        execute_job(job_id, trigger_type="scheduled")

        # ① 主人看得见
        mine = client.get("/api/schedule/logs", headers=_auth(owner)).json()
        assert mine["total"] == 1

        # ② 入侵者看不见（含按 job_id 直查）
        theirs = client.get("/api/schedule/logs", headers=_auth(intruder)).json()
        assert theirs["total"] == 0
        by_job = client.get(
            "/api/schedule/logs", headers=_auth(intruder), params={"job_id": job_id}
        ).json()
        assert by_job["total"] == 0

        # ③ 入侵者删不掉（越权 id 静默跳过，不计入 deleted）
        log_id = mine["items"][0]["id"]
        response = client.request(
            "DELETE", "/api/schedule/logs", headers=_auth(intruder), json={"ids": [log_id]}
        )
        assert response.json() == {"requested": 1, "deleted": 0}
        assert client.get("/api/schedule/logs", headers=_auth(owner)).json()["total"] == 1

        # ④ 入侵者的「清空」也不碰主人的
        response = client.delete("/api/schedule/logs/clean", headers=_auth(intruder))
        assert response.json()["deleted"] == 0
        assert client.get("/api/schedule/logs", headers=_auth(owner)).json()["total"] == 1

        # ⑤ 主人自己的清空照常工作
        response = client.delete("/api/schedule/logs/clean", headers=_auth(owner))
        assert response.json()["deleted"] == 1
        assert client.get("/api/schedule/logs", headers=_auth(owner)).json()["total"] == 0
    finally:
        app.dependency_overrides.clear()


def test_schedule_status_counts_only_my_rows(tmp_path, monkeypatch):
    """状态计数按当前用户收口（原全库口径 ⇒ 别人的任务数漏到我的看板）。"""
    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        owner = _register("status-owner")
        intruder = _register("status-intruder")
        # 主人有 13 个内置行；入侵者先手动建一个自己的
        _seed_builtin(SessionLocal, owner)
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(intruder),
            json={
                "name": "入侵者的任务",
                "job_key": "system.cookie_health_check",
                "params": {},
            },
        )
        assert response.status_code == 200

        owner_stats = client.get("/api/schedule/status", headers=_auth(owner)).json()["stats"]
        intruder_stats = client.get("/api/schedule/status", headers=_auth(intruder)).json()["stats"]
        assert owner_stats["total"] == 13
        # 入侵者注册时也播种了 12 行 + 自己新建 1 行 = 13（只见自己的，不见主人的）
        assert intruder_stats["total"] == 14, "入侵者只应看到自己的行"
    finally:
        app.dependency_overrides.clear()


def test_list_targets_returns_registry_with_existing_counts(tmp_path):
    """R18：`GET /schedule/targets` 列出注册表全量 + 各目标已建任务数。"""
    from backend.app.services.job_registry import job_registry

    SessionLocal = _override_database(tmp_path)
    try:
        token = _register("job-targets")
        # 先建两条同目标任务，再拉清单看计数
        for name, cron in (("巡检-1", "0 0 9 * * *"), ("巡检-2", "0 0 21 * * *")):
            created = client.post(
                "/api/schedule/jobs",
                headers=_auth(token),
                json={
                    "name": name,
                    "job_key": "system.cookie_health_check",
                    "schedule": {"trigger_type": "cron", "cron_expression": cron},
                },
            )
            assert created.status_code == 200, created.text

        response = client.get("/api/schedule/targets", headers=_auth(token))
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        keys = {item["job_key"] for item in items}
        # 注册表全量都在（含 13 个内置 + 1 个普通）
        assert keys == set(job_registry().keys())
        assert len(items) == len(job_registry())

        by_key = {item["job_key"]: item for item in items}
        # 注册播种 1 行 + 本测试自建 2 行（DEF-066 前的原断言是 2）
        assert by_key["system.cookie_health_check"]["existing_count"] == 3
        assert by_key["system.cookie_health_check"]["builtin"] is True
        assert by_key["auto_ops.keyword_pipeline"]["builtin"] is False
        assert by_key["auto_ops.keyword_pipeline"]["existing_count"] == 0
        assert by_key["auto_ops.keyword_pipeline"]["params_required"] == ["auto_task_id"]
        assert by_key["system.cookie_health_check"]["params_editable"] == ["account_ids"]
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 触发器与策略校验（cron 实解析）
# --------------------------------------------------------------------------- #
def test_create_job_validates_cron_with_real_parser(tmp_path):
    _override_database(tmp_path)
    try:
        token = _register("job-cron")
        for bad in ("abc", "0 0 9 * *", "0 0 25 * * *", "0 0 9 * * 8"):
            response = client.post(
                "/api/schedule/jobs",
                headers=_auth(token),
                json={
                    "name": "坏 cron",
                    "job_key": "auto_ops.keyword_pipeline",
                    "params": {"auto_task_id": 1},
                    "schedule": {"trigger_type": "cron", "cron_expression": bad},
                },
            )
            assert response.status_code == 400, bad
            assert response.json()["detail"].startswith("CRON_INVALID"), bad
    finally:
        app.dependency_overrides.clear()


def test_create_job_validates_run_at(tmp_path):
    _override_database(tmp_path)
    try:
        token = _register("job-runat")
        base = {
            "name": "定时一次",
            "job_key": "auto_ops.keyword_pipeline",
            "params": {"auto_task_id": 1},
        }
        # 缺 run_at
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(token),
            json={**base, "schedule": {"trigger_type": "at"}},
        )
        assert response.status_code == 400
        assert response.json()["detail"].startswith("RUN_AT_REQUIRED")
        # run_at 在过去
        past = (shanghai_now() - timedelta(hours=1)).isoformat()
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(token),
            json={**base, "schedule": {"trigger_type": "at", "run_at": past}},
        )
        assert response.status_code == 400
        assert response.json()["detail"].startswith("RUN_AT_INVALID")
    finally:
        app.dependency_overrides.clear()


def test_create_job_validates_params_and_concurrent_ceiling(tmp_path):
    _override_database(tmp_path)
    try:
        token = _register("job-params")
        # params 缺必填键
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(token),
            json={"name": "缺参", "job_key": "auto_ops.keyword_pipeline", "params": {}},
        )
        assert response.status_code == 400
        # 注册表硬顶：auto_ops 不允许并发（带一个合法 cron，让校验走到并发那一关）
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(token),
            json={
                "name": "要并发",
                "job_key": "auto_ops.keyword_pipeline",
                "params": {"auto_task_id": 1},
                "schedule": {"trigger_type": "cron", "cron_expression": "0 0 9 * * *", "concurrent": "allow"},
            },
        )
        assert response.status_code == 400
        assert response.json()["detail"].startswith("JOB_CONCURRENT_NOT_ALLOWED")
    finally:
        app.dependency_overrides.clear()


def test_cron_validate_returns_future_fire_times(tmp_path):
    _override_database(tmp_path)
    try:
        token = _register("job-validate")
        response = client.post(
            "/api/schedule/cron/validate",
            headers=_auth(token),
            json={"cron_expression": "0 0 9 * * *"},
        )
        assert response.status_code == 200
        times = response.json()["next_fire_times"]
        assert len(times) == 3
        assert times[0] < times[1] < times[2]
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# builtin 保护 + CRUD + 调度器同步
# --------------------------------------------------------------------------- #
def _seed_builtin(SessionLocal, token: str) -> ScheduledJob:
    """给 **token 归属用户**播种内置行，返回其 `system.cookie_health_check` 行 id。

    DEF-066：内置行按用户各持一份 ⇒ 必须按行主取（多用户库里同 job_key 有多行，
    乱取会取到别人的行）。
    """
    from backend.app.services.scheduler_service import ensure_builtin_jobs

    db = SessionLocal()
    try:
        ensure_builtin_jobs(db)
        job = db.scalars(
            select(ScheduledJob).where(
                ScheduledJob.job_key == "system.cookie_health_check",
                ScheduledJob.user_id == _user_of(token),
            )
        ).first()
        assert job is not None
        return job.id
    finally:
        db.close()


def test_any_job_can_be_deleted_and_params_stay_whitelisted(tmp_path, monkeypatch):
    """用户口径（2026-09-24）：「所有任务都是内置的」⇒ 内置不再是一种特权。

    原断言（`test_builtin_job_cannot_be_deleted_or_have_params_changed`）中
    「内置行删除一律 400 `JOB_BUILTIN_PROTECTED`」**已被推翻**，原文保留：
        response = client.delete(f"/api/schedule/jobs/{job_id}", headers=_auth(token))
        assert response.status_code == 400
        assert response.json()["detail"].startswith("JOB_BUILTIN_PROTECTED")
    不变的是**参数白名单**：内置与普通行仍同一把尺子（设计 15 §5）。
    """
    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        token = _register("job-builtin-guard")
        job_id = _seed_builtin(SessionLocal, token)

        # 自定义参数走白名单：非 params_editable 的键一律 400
        # （内置与普通行同一把尺子，设计 15 §5）
        response = client.put(
            f"/api/schedule/jobs/{job_id}",
            headers=_auth(token),
            json={"params": {"hacked": True}},
        )
        assert response.status_code == 400
        assert response.json()["detail"].startswith("JOB_PARAMS_NOT_EDITABLE")

        # 调度属性可调（换触发器）
        response = client.put(
            f"/api/schedule/jobs/{job_id}",
            headers=_auth(token),
            json={"schedule": {"trigger_type": "cron", "cron_expression": "0 0 */2 * * *"}},
        )
        assert response.status_code == 200
        assert response.json()["cron_expression"] == "0 0 */2 * * *"

        # 推翻后的新契约：内置行**可以**删掉（所有任务一律可删）
        deleted = client.delete(f"/api/schedule/jobs/{job_id}", headers=_auth(token))
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["status"] == "deleted"
        assert client.get(f"/api/schedule/jobs/{job_id}", headers=_auth(token)).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_builtin_job_account_selection(tmp_path, monkeypatch):
    """账号维度系统任务：account_ids 多选（自己的账号）+ 归属闸 + 空=全部。"""
    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        token = _register("job-accounts")
        intruder = _register("job-accounts-intruder")
        job_id = _seed_builtin(SessionLocal, token)

        db = SessionLocal()
        user = db.scalars(select(User)).first()
        from backend.app.models import PlatformAccount

        mine = PlatformAccount(user_id=user.id, platform="xhs", sub_type="pc", nickname="我的")
        db.add(mine)
        db.commit()
        db.refresh(mine)
        mine_id = mine.id
        # 别人的账号（入侵者用户）
        other_user = db.scalars(select(User).where(User.id != user.id)).first()
        theirs = PlatformAccount(user_id=other_user.id, platform="xhs", sub_type="pc", nickname="别人的")
        db.add(theirs)
        db.commit()
        db.refresh(theirs)
        theirs_id = theirs.id
        db.close()

        # 序列化带出 params_editable（前端据此渲染账号多选）
        response = client.get(f"/api/schedule/jobs/{job_id}", headers=_auth(token))
        assert response.json()["params_editable"] == ["account_ids"]

        # 选自己的账号 ⇒ 200，落库
        response = client.put(
            f"/api/schedule/jobs/{job_id}",
            headers=_auth(token),
            json={"params": {"account_ids": [mine_id]}},
        )
        assert response.status_code == 200
        assert response.json()["params"]["account_ids"] == [mine_id]

        # 清空选择 = 全部账号（2026-09-24 前的行为）
        response = client.put(
            f"/api/schedule/jobs/{job_id}",
            headers=_auth(token),
            json={"params": {"account_ids": []}},
        )
        assert response.status_code == 200
        assert response.json()["params"]["account_ids"] == []

        # 不存在的账号 ⇒ 400
        response = client.put(
            f"/api/schedule/jobs/{job_id}",
            headers=_auth(token),
            json={"params": {"account_ids": [999999]}},
        )
        assert response.status_code == 400
        assert response.json()["detail"].startswith("ACCOUNT_NOT_OWNED")

        # 别人的账号 ⇒ 400（跨租户）
        response = client.put(
            f"/api/schedule/jobs/{job_id}",
            headers=_auth(token),
            json={"params": {"account_ids": [theirs_id]}},
        )
        assert response.status_code == 400
        assert response.json()["detail"].startswith("ACCOUNT_NOT_OWNED")

        # 非整数 id ⇒ 400
        response = client.put(
            f"/api/schedule/jobs/{job_id}",
            headers=_auth(token),
            json={"params": {"account_ids": ["1"]}},
        )
        assert response.status_code == 400
        assert response.json()["detail"].startswith("ACCOUNT_IDS_INVALID")
    finally:
        app.dependency_overrides.clear()


def test_user_job_params_are_immutable_via_schedule_api(tmp_path, monkeypatch):
    """普通行（auto_ops）的 params 由代码拥有：/schedule/jobs 改不了 ⇒ 400。"""
    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        token = _register("job-immutable")
        auto_task_id = _make_auto_task(SessionLocal, token)
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(token),
            json={
                "name": "管线任务",
                "job_key": "auto_ops.keyword_pipeline",
                "params": {"auto_task_id": auto_task_id},
            },
        )
        assert response.status_code == 200
        job_id = response.json()["id"]

        response = client.put(
            f"/api/schedule/jobs/{job_id}",
            headers=_auth(token),
            json={"params": {"auto_task_id": auto_task_id + 100}},
        )
        assert response.status_code == 400
        assert response.json()["detail"].startswith("JOB_PARAMS_NOT_EDITABLE")
    finally:
        app.dependency_overrides.clear()


def test_note_sync_runner_filters_by_account_ids(tmp_path, monkeypatch):
    """服务层：account_ids 非空 ⇒ 只跑指定账号；**行主隔离** ⇒ 不碰别人的账号。

    ⚠️ 两条路径的**执行方式不同**（设计 13 §35.17.2，2026-09-27 起），所以这里对两段
    分别建桩、分别断言：

    * `account_ids` **非空**（点名了账号）⇒ `start_note_sync` 建任务行 + 等终态
      （§35.16 单 worker 串行）—— **不得**再直接调 `run_note_sync`；
    * `account_ids` **空**（定时轮转，批量）⇒ 照旧直接 `run_note_sync`。

    两段共用「只跑行主的账号」这条判断，所以隔离断言在两段上都要成立。
    """
    from backend.app.core.security import encrypt_text
    from backend.app.models import AccountCookieVersion, PlatformAccount
    from backend.app.services import note_sync_service
    from backend.app.services import scheduler_service as sched
    from backend.app.services.account_service import CHANNEL_CREATOR, dump_channel_envelope

    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    monkeypatch.setattr(sched, "_sync_account_gap_seconds", lambda: 0)

    calls: list[int] = []

    def _fake_run_note_sync(account_id, **kwargs):
        calls.append(account_id)

    monkeypatch.setattr(note_sync_service, "run_note_sync", _fake_run_note_sync)

    #: 定向路径的观察点：建行时的 (account_id, source)。
    started: list[tuple[int, str]] = []

    def _fake_start_note_sync(account_id, **kwargs):
        started.append((account_id, str(kwargs.get("source"))))
        return {"reused": False, "task_id": 900 + account_id}

    monkeypatch.setattr(note_sync_service, "start_note_sync", _fake_start_note_sync)
    # 定向执行要等业务任务到终态；本用例只盯「跑了哪些账号」，把等待桩成立即结束。
    monkeypatch.setattr(
        sched,
        "_await_targeted_task",
        lambda task_id: {
            "task_id": task_id,
            "status": "done",
            "progress": {"stored": 1, "total": 1},
            "note": "",
            "error": "",
        },
    )

    db = SessionLocal()
    # 本用例走服务层（不登录），用户行需自行播种 —— `_override_database` 只建表。
    user = User(username="sync-filter", password_hash="x")
    other = User(username="sync-filter-other", password_hash="x")
    db.add_all([user, other])
    db.commit()
    db.refresh(user)
    db.refresh(other)
    a1 = PlatformAccount(user_id=user.id, platform="xhs", sub_type="creator", nickname="甲")
    a2 = PlatformAccount(user_id=user.id, platform="xhs", sub_type="creator", nickname="乙")
    # 别人的账号（同样可同步形态 —— 隔离必须挡的是「能跑」的那些）
    theirs = PlatformAccount(user_id=other.id, platform="xhs", sub_type="creator", nickname="别人的")
    db.add_all([a1, a2, theirs])
    db.commit()
    db.refresh(a1)
    db.refresh(a2)
    db.refresh(theirs)
    creator_jar = (
        "galaxy_creator_session_id=abc123; "
        "access-token-creator.xiaohongshu.com=ctok123; customer-sso-sid=sso123; a1=xyz"
    )
    for account in (a1, a2, theirs):
        db.add(AccountCookieVersion(
            platform_account_id=account.id,
            encrypted_cookies=encrypt_text(dump_channel_envelope({CHANNEL_CREATOR: creator_jar})),
        ))
    db.commit()
    mine = (a1.id, a2.id)
    theirs_id = theirs.id
    user_id = user.id
    other_id = other.id
    db.close()

    # ① account_ids 非空 ⇒ 只跑指定账号（自己的），走「建行 + 等终态」
    sched.run_scheduled_note_syncs(user_id=user_id, account_ids=[mine[0]])
    assert [account_id for account_id, _ in started] == [mine[0]], f"只应跑指定账号，实跑 {started}"
    assert calls == [], "定向执行不得再直接调 run_note_sync（那就把 §35.16 的单 worker 串行绕过去了）"
    assert {source for _, source in started} == {"scheduled"}, (
        "调度侧定向执行的来源必须是 scheduled —— 写 account_card_* 等于在数据里造假历史"
    )

    # ② 空 = **本人全部账号**（DEF-066：不再是全库账号）
    calls.clear()
    sched.run_scheduled_note_syncs(user_id=user_id)
    assert sorted(calls) == sorted(mine), f"空应跑本人全部，实跑 {calls}"
    assert theirs_id not in calls, "行主隔离失效：跑别人的账号了"
    assert started == [(mine[0], "scheduled")], "批量路径不该建任务行（仍是直接跑执行体）"

    # ③ 行主自己的账号一个不少（回归：隔离不许把该跑的漏掉）
    calls.clear()
    sched.run_scheduled_note_syncs(user_id=other_id)
    assert calls == [theirs_id], f"别人的行应只跑别人自己的账号，实跑 {calls}"


def test_job_ownership_is_isolated(tmp_path, monkeypatch):
    """DEF-066：别人的行**列表也看不见**（原全局行放行已废）；改 / 删 / 执行一律 404。"""
    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        owner = _register("job-owner")
        intruder = _register("job-intruder")
        auto_task_id = _make_auto_task(SessionLocal, owner)
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(owner),
            json={
                "name": "我的任务",
                "job_key": "auto_ops.keyword_pipeline",
                "params": {"auto_task_id": auto_task_id},
            },
        )
        assert response.status_code == 200
        job_id = response.json()["id"]

        response = client.get(f"/api/schedule/jobs/{job_id}", headers=_auth(intruder))
        assert response.status_code == 404
        response = client.delete(f"/api/schedule/jobs/{job_id}", headers=_auth(intruder))
        assert response.status_code == 404
        response = client.post(f"/api/schedule/jobs/{job_id}/run", headers=_auth(intruder))
        assert response.status_code == 404

        # 列表隔离：入侵者只看到自己的行（原实现把 user_id IS NULL 的全局行
        # 混进每个人的列表 ⇒ 那是多用户互相操作同一份数据的入口）
        visible = client.get("/api/schedule/jobs", headers=_auth(intruder)).json()
        assert all(item["id"] != job_id for item in visible["items"])
        assert all(item["user_id"] != _user_of(owner) for item in visible["items"])
    finally:
        app.dependency_overrides.clear()


def test_builtin_rows_are_per_user_and_independent(tmp_path, monkeypatch):
    """DEF-066：内置行按用户各持一份 ⇒ 暂停 / 删改互不影响（原全局共享单行已废）。"""
    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        owner = _register("builtin-owner")
        intruder = _register("builtin-intruder")
        owner_job = _seed_builtin(SessionLocal, owner)
        intruder_job = _seed_builtin(SessionLocal, intruder)
        assert owner_job != intruder_job, "两个用户应各持一份内置行"

        # 入侵者动自己的行（暂停）
        response = client.patch(
            f"/api/schedule/jobs/{intruder_job}/status",
            headers=_auth(intruder),
            json={"status": "paused"},
        )
        assert response.status_code == 200

        # 行主的同一目标不受影响（仍是 active）
        db = SessionLocal()
        try:
            row = db.get(ScheduledJob, owner_job)
            assert row.status == "active"
            assert row.user_id == _user_of(owner)
        finally:
            db.close()

        # 入侵者连行主的行都碰不到（404）
        assert client.get(f"/api/schedule/jobs/{owner_job}", headers=_auth(intruder)).status_code == 404
        assert client.patch(
            f"/api/schedule/jobs/{owner_job}/status",
            headers=_auth(intruder),
            json={"status": "paused"},
        ).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_auto_ops_job_rejects_foreign_auto_task_id(tmp_path, monkeypatch):
    """跨租户防护：拿别人的 auto_task_id 建调度行 ⇒ 400（validator，设计 15 §5）。"""
    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        owner = _register("job-owner2")
        intruder = _register("job-intruder2")
        foreign_task_id = _make_auto_task(SessionLocal, intruder)
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(owner),
            json={
                "name": "偷来的管线",
                "job_key": "auto_ops.keyword_pipeline",
                "params": {"auto_task_id": foreign_task_id},
            },
        )
        assert response.status_code == 400
        assert "auto_task_id" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_pause_and_resume_sync_scheduler(tmp_path, monkeypatch):
    """暂停 ⇒ 调度器摘除；恢复 ⇒ 重新注册（§6.1 API 同步原语）。"""
    from apscheduler.schedulers.background import BackgroundScheduler

    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        token = _register("job-pause")
        auto_task_id = _make_auto_task(SessionLocal, token)
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(token),
            json={
                "name": "可暂停任务",
                "job_key": "auto_ops.keyword_pipeline",
                "params": {"auto_task_id": auto_task_id},
            },
        )
        assert response.status_code == 200
        job_id = response.json()["id"]

        scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        scheduler.start()
        app.state.scheduler = scheduler
        try:
            # 创建时调度器还没挂上 ⇒ 手动补同步（模拟重启后的注册）
            from backend.app.services.scheduler_service import sync_job_registration

            db = SessionLocal()
            job = db.get(ScheduledJob, job_id)
            sync_job_registration(scheduler, job)
            db.close()
            assert scheduler.get_job(f"job_{job_id}") is not None

            response = client.patch(
                f"/api/schedule/jobs/{job_id}/status",
                headers=_auth(token),
                json={"status": "paused"},
            )
            assert response.status_code == 200
            assert scheduler.get_job(f"job_{job_id}") is None, "暂停后调度器应摘除"

            response = client.patch(
                f"/api/schedule/jobs/{job_id}/status",
                headers=_auth(token),
                json={"status": "active"},
            )
            assert response.status_code == 200
            assert scheduler.get_job(f"job_{job_id}") is not None, "恢复后应重新注册"
        finally:
            scheduler.shutdown(wait=False)
            app.state.scheduler = None
    finally:
        app.dependency_overrides.clear()


def test_pause_and_resume_sync_scheduler(tmp_path, monkeypatch):
    """暂停 ⇒ 调度器摘除；恢复 ⇒ 重新注册（§6.1 API 同步原语）。"""
    from apscheduler.schedulers.background import BackgroundScheduler

    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        token = _register("job-pause")
        auto_task_id = _make_auto_task(SessionLocal, token)
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(token),
            json={
                "name": "可暂停任务",
                "job_key": "auto_ops.keyword_pipeline",
                "params": {"auto_task_id": auto_task_id},
            },
        )
        assert response.status_code == 200
        job_id = response.json()["id"]

        scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        scheduler.start()
        app.state.scheduler = scheduler
        try:
            # 创建时调度器还没挂上 ⇒ 手动补同步（模拟重启后的注册）
            from backend.app.services.scheduler_service import sync_job_registration

            db = SessionLocal()
            job = db.get(ScheduledJob, job_id)
            sync_job_registration(scheduler, job)
            db.close()
            assert scheduler.get_job(f"job_{job_id}") is not None

            response = client.patch(
                f"/api/schedule/jobs/{job_id}/status",
                headers=_auth(token),
                json={"status": "paused"},
            )
            assert response.status_code == 200
            assert scheduler.get_job(f"job_{job_id}") is None, "暂停后调度器应摘除"

            response = client.patch(
                f"/api/schedule/jobs/{job_id}/status",
                headers=_auth(token),
                json={"status": "active"},
            )
            assert response.status_code == 200
            assert scheduler.get_job(f"job_{job_id}") is not None, "恢复后应重新注册"
        finally:
            scheduler.shutdown(wait=False)
            app.state.scheduler = None
    finally:
        app.dependency_overrides.clear()


def test_persisted_store_guard_keeps_next_run_time_across_restart(tmp_path, monkeypatch):
    """§24.9 回归：store 已有同 id job ⇒ 第二次同步跳过注册（不重置计时）。"""
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

    from backend.app.services.scheduler_service import build_scheduler, sync_jobs_from_db

    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        db = SessionLocal()
        db.add(ScheduledJob(
            user_id=_make_user(SessionLocal, "persist-guard"),
            name="持久化守卫测试",
            job_key="auto_ops.keyword_pipeline",
            trigger_type="cron",
            cron_expression="0 0 */2 * * *",
            params={"auto_task_id": 1},
            status="active",
        ))
        db.commit()
        db.close()

        store = SQLAlchemyJobStore(url=f"sqlite:///{tmp_path / 'jobs.db'}")
        first = build_scheduler(jobstores={"default": store})
        first.start()
        try:
            stats = sync_jobs_from_db(first, store=store)
            assert stats["registered"] >= 1
            registered = first.get_jobs()
            assert registered, "首次同步应注册"
            first_run_time = registered[0].next_run_time
        finally:
            first.shutdown()

        second = build_scheduler(jobstores={"default": store})
        # 不 start：未启动时 get_jobs 只看待注册队列，store 里的不可见 —— 守卫必须命中
        stats = sync_jobs_from_db(second, store=store)
        assert stats["registered"] == 0, "持久化守卫失效：重启重复注册会重置计时"
        assert second.get_jobs() == []
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 执行包装器：日志 / 计数 / 退避门 / at 办结
# --------------------------------------------------------------------------- #
def test_execute_job_success_writes_log_and_counts(tmp_path, monkeypatch):
    from backend.app.services.scheduled_job_runner import execute_job

    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        user_id = _make_user(SessionLocal, "exec-success")
        db = SessionLocal()
        job = ScheduledJob(
            user_id=user_id,
            name="监控刷新（真实函数，零平台请求）",
            job_key="system.monitoring_refresh",
            trigger_type="cron",
            cron_expression="0 0 * * * *",
            status="active",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
        db.close()

        outcome = execute_job(job_id, trigger_type="scheduled")
        assert outcome["status"] == "success"

        db = SessionLocal()
        job = db.get(ScheduledJob, job_id)
        assert job.run_count == 1
        assert job.fail_count == 0
        assert job.last_status == "success"
        assert job.backoff_until is None
        log = db.scalars(select(ScheduledJobLog).where(ScheduledJobLog.job_id == job_id)).first()
        assert log is not None
        assert log.status == "success"
        assert log.trigger_type == "scheduled"
        assert log.duration_ms is not None
        assert log.finished_at is not None
        # DEF-066：日志行带归属快照（按用户隔离的依据）
        assert log.user_id == user_id
        db.close()
    finally:
        app.dependency_overrides.clear()


def test_execute_job_writes_progress_for_success_and_failure(tmp_path, monkeypatch):
    """R13：`scheduled_job_logs.progress` 的写入三分支（100 / 尊重上报 / 失败 0）。

    这是 R13 的核心字段 —— 没有它日志表第 6 列（任务进度）就没有数据源。
    用**合成 JobSpec** 替换注册表取词（runner 里 `get_job_spec` 是按名导入的，
    所以打桩打在 runner 模块上）。
    """
    from backend.app.services import scheduled_job_runner
    from backend.app.services.job_registry import JobSpec
    from backend.app.services.scheduled_job_runner import execute_job

    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)

    def _stub_spec(func):
        # user_scoped=True：与真实 system.* 任务同形（runner 会注入 user_id）
        spec = JobSpec(
            key="system.monitoring_refresh", name="桩任务", func=func, builtin=True,
            user_scoped=True,
        )
        monkeypatch.setattr(scheduled_job_runner, "get_job_spec", lambda key: spec)

    def _make_job(db, name):
        job = ScheduledJob(
            user_id=_make_user(SessionLocal, f"progress-{name}"), name=name,
            job_key="system.monitoring_refresh", trigger_type="cron",
            cron_expression="0 0 * * * *", status="active",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
        db.close()
        return job_id

    def _latest_progress():
        db = SessionLocal()
        try:
            log = db.scalars(select(ScheduledJobLog).order_by(ScheduledJobLog.id.desc())).first()
            return log.progress if log else None
        finally:
            db.close()

    try:
        # ① 成功且任务未上报 progress ⇒ 100
        _stub_spec(lambda **kwargs: {"ok": True})
        job_id = _make_job(SessionLocal(), "进度A")
        assert execute_job(job_id, trigger_type="scheduled")["status"] == "success"
        assert _latest_progress() == 100, "成功未上报 ⇒ 进度应为 100"

        # ② 成功且任务上报 42 ⇒ 尊重上报值（不覆盖为 100）
        _stub_spec(lambda **kwargs: {"ok": True, "progress": 42})
        job_id = _make_job(SessionLocal(), "进度B")
        assert execute_job(job_id, trigger_type="scheduled")["status"] == "success"
        assert _latest_progress() == 42, "任务上报 42 ⇒ 进度应为 42"

        # ②b 上报越界值 ⇒ 夹回 0~100（写库不得出现 150 / -3）
        _stub_spec(lambda **kwargs: {"ok": True, "progress": 150})
        job_id = _make_job(SessionLocal(), "进度B2")
        assert execute_job(job_id, trigger_type="scheduled")["status"] == "success"
        assert _latest_progress() == 100, "上报 150 ⇒ 应夹到 100"

        # ③ 失败 ⇒ **不清零**（2026-09-27 用户口径「任务执行失败后任务进度不要变，
        #   应展示执行的进度比例」）。本用例没给探针 ⇒ 执行期间从没写过 progress
        #   ⇒ 行上还是初始 0；**关键是不再被 `_mark_failed` 主动写 0**（保留语义）。
        def _boom(**kwargs):
            raise RuntimeError("炸了")

        _stub_spec(_boom)
        job_id = _make_job(SessionLocal(), "进度C")
        assert execute_job(job_id, trigger_type="scheduled")["status"] == "failed"
        assert _latest_progress() == 0, "没上报过进度 ⇒ 保留初始 0（不是被清成 0）"
    finally:
        app.dependency_overrides.clear()


def test_mark_failed_keeps_reached_progress(tmp_path, monkeypatch):
    """失败**不清零**已执行的进度百分比（2026-09-27 用户口径）。

    用户原话：「任务执行失败后任务进度不要变，应展示执行的进度比例」＋追问确认
    「**文案继续展示，但是已执行的进度百分比保留**」。

    旧实现无条件 `log.progress = 0` ⇒ 进度条被打回 0%（既不是「没跑」也不是「跑完了」，
    是最没信息量的那个数）。新口径两条：

    * 调用方给了终态进度（观测线程从任务行算出来的百分比）⇒ 用它（比泵的 2s 采样准）；
    * 没给 ⇒ **原样保留**泵最后一次写入的值（不猜、不清零）。
    """
    from backend.app.services import scheduled_job_runner as sjr
    from backend.app.services.scheduled_job_runner import _mark_failed

    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    db = SessionLocal()
    try:
        user_id = _make_user(SessionLocal, "progress-keep")
        job = ScheduledJob(
            user_id=user_id, name="进度保留", job_key="system.monitoring_refresh",
            trigger_type="cron", cron_expression="0 0 * * * *", status="active",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        log = sjr._new_log_row(db, job, trigger_type="manual")
        log.progress = 37  # 模拟进度泵执行期间写入的值
        db.commit()

        _mark_failed(db, job, log, "boom", message="执行失败")
        db.commit()
        assert log.progress == 37, "没给终态进度 ⇒ 保留泵写入的 37（不许清零）"

        log.status = "running"
        _mark_failed(db, job, log, "boom", message="执行失败", progress=42)
        assert log.progress == 42, "给了终态进度 ⇒ 用它"

        log.status = "running"
        _mark_failed(db, job, log, "boom", message="执行失败", progress=150)
        assert log.progress == 100, "越界值夹回 0~100"
    finally:
        db.close()
        app.dependency_overrides.clear()


def test_failure_reason_never_shows_status_code():
    """兜底文案**不许摆状态码原文**（旧实现拼 `任务状态 cancelled`）。

    真原因（`error` / `note`）优先 —— 失败三分法不变：真异常必须给真原因。
    """
    from backend.app.services.scheduled_job_runner import _failure_reason

    assert _failure_reason({"status": "failed", "error": "连接超时", "note": ""}) == "连接超时"
    assert (
        _failure_reason({"status": "cancelled", "error": "", "note": "已被用户取消"})
        == "已被用户取消"
    )
    for status in ("cancelled", "timeout", "missing", "failed"):
        reason = _failure_reason({"status": status, "error": "", "note": ""})
        assert reason, "兜底文案不能是空串"
        assert "任务状态" not in reason, f"兜底文案摆了状态码原文：{reason}"
        assert status not in reason, f"兜底文案含英文状态码 {status}：{reason}"


def test_progress_percent_is_single_implementation():
    """`sync_task_service.progress_percent` 是进度换算的**唯一实现**（泵与终态共用）。

    优先级（2026-09-28 重排，设计 13 §42.4）：**`processed` / `work_total`**（本任务
    累计已处理 / 预计总工作量，**两个执行体都写**）> 详情补齐口径（`detail_done` /
    `detail_total`）> 列表口径（`stored` / `total`）；都没有 ⇒ `None`（缺值口径，不造 0）。
    """
    from backend.app.services.sync_task_service import progress_percent

    assert progress_percent({"stored": 3, "total": 10}) == 30
    assert progress_percent({"stored": 3, "total": 10, "detail_done": 1, "detail_total": 4}) == 25
    assert progress_percent({"stored": 3, "total": 10, "detail_done": 1, "detail_total": 0}) == 30
    assert progress_percent({"stored": 3, "total": None}) is None
    assert progress_percent({}) is None
    assert progress_percent(None) is None
    # 2026-09-28：`processed` / `work_total` 是**首选**口径 —— 它俩在就必须用它，
    # 否则「有界同步（没有 total）」的进度条又会退回「比例未知」。
    assert progress_percent({"processed": 5, "work_total": 20}) == 25
    assert progress_percent(
        {"processed": 5, "work_total": 20, "stored": 1, "total": 1000}
    ) == 25, "有 processed/work_total 时不许退回 stored/total（那正是「恒 0%」的旧口径）"
    assert progress_percent(
        {"processed": 5, "work_total": 20, "detail_done": 1, "detail_total": 4}
    ) == 25, "processed/work_total 优先级高于 detail_*（后者只服务文案）"
    assert progress_percent({"processed": 5, "work_total": 0, "stored": 3, "total": 10}) == 30, (
        "work_total=0 视为「没报」⇒ 退回下一档（分母为 0 不能当比例）"
    )
    assert progress_percent({"processed": 5, "work_total": None, "stored": 3, "total": 10}) == 30
    assert progress_percent({"processed": 5, "work_total": 0}) is None, (
        "分母没报、也没有别的口径 ⇒ None（缺值口径，不造 0）"
    )
    assert progress_percent({"processed": 30, "work_total": 20}) == 100, "越界夹到 100"


def test_execute_job_failure_sets_backoff_and_gate_skips_next_scheduled_run(tmp_path, monkeypatch):
    """失败 ⇒ 退避门；调度触发被门挡住（skipped 日志）；手动执行放行。"""
    from backend.app.services.scheduled_job_runner import execute_job

    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        db = SessionLocal()
        job = ScheduledJob(
            user_id=_make_user(SessionLocal, "exec-backoff"),
            name="必然失败的任务",
            job_key="auto_ops.keyword_pipeline",
            trigger_type="cron",
            cron_expression="0 0 9 * * *",
            params={"auto_task_id": 999999},  # 不存在的任务 ⇒ AutoTaskRunError
            status="active",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
        db.close()

        first = execute_job(job_id, trigger_type="scheduled")
        assert first["status"] == "failed"

        db = SessionLocal()
        job = db.get(ScheduledJob, job_id)
        assert job.fail_count == 1
        assert job.consecutive_failures == 1
        assert job.backoff_until is not None
        db.close()

        # 调度触发：撞门 ⇒ skipped
        second = execute_job(job_id, trigger_type="scheduled")
        assert second["status"] == "skipped"
        db = SessionLocal()
        skipped = db.scalars(
            select(ScheduledJobLog).where(
                ScheduledJobLog.job_id == job_id, ScheduledJobLog.status == "skipped"
            )
        ).first()
        assert skipped is not None
        assert "退避" in skipped.message
        db.close()

        # 手动触发：门只挡调度，手动放行（依然失败，但不该是 skipped）
        third = execute_job(job_id, trigger_type="manual")
        assert third["status"] == "failed"
    finally:
        app.dependency_overrides.clear()


def test_at_job_completes_after_success_and_never_backs_off(tmp_path, monkeypatch):
    """at 一次性任务：成功 ⇒ 自动办结；失败 ⇒ 不退避（没有下一发）。"""
    from backend.app.services.scheduled_job_runner import execute_job

    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        user_id = _make_user(SessionLocal, "at-job")
        db = SessionLocal()
        job = ScheduledJob(
            user_id=user_id,
            name="定时一次的监控刷新",
            job_key="system.monitoring_refresh",
            trigger_type="at",
            run_at=shanghai_now() + timedelta(hours=1),
            status="active",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
        db.close()

        outcome = execute_job(job_id, trigger_type="scheduled")
        assert outcome["status"] == "success"

        db = SessionLocal()
        job = db.get(ScheduledJob, job_id)
        assert job.status == "completed", "at 任务成功后应自动办结"
        db.close()

        # at 失败不进退避门
        db = SessionLocal()
        failing = ScheduledJob(
            user_id=user_id,
            name="定时一次的必然失败",
            job_key="auto_ops.keyword_pipeline",
            trigger_type="at",
            run_at=shanghai_now() + timedelta(hours=1),
            params={"auto_task_id": 999999},
            status="active",
        )
        db.add(failing)
        db.commit()
        db.refresh(failing)
        failing_id = failing.id
        db.close()

        assert execute_job(failing_id, trigger_type="scheduled")["status"] == "failed"
        db = SessionLocal()
        failing = db.get(ScheduledJob, failing_id)
        assert failing.backoff_until is None, "at 任务失败不进退避门"
        assert failing.status == "active", "失败的 at 任务保持 active，等用户处置"
        db.close()
    finally:
        app.dependency_overrides.clear()


def test_delete_job_keeps_logs_with_null_job_id(tmp_path, monkeypatch):
    from backend.app.services.scheduled_job_runner import execute_job

    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        token = _register("job-log-keep")
        db = SessionLocal()
        job = ScheduledJob(
            user_id=_user_of(token),
            name="带日志的任务",
            job_key="system.monitoring_refresh",
            trigger_type="cron",
            cron_expression="0 0 * * * *",
            status="active",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
        db.close()

        execute_job(job_id, trigger_type="scheduled")

        response = client.delete(f"/api/schedule/jobs/{job_id}", headers=_auth(token))
        assert response.status_code == 200

        db = SessionLocal()
        logs = db.scalars(select(ScheduledJobLog).where(ScheduledJobLog.job_name == "带日志的任务")).all()
        assert len(logs) == 1
        assert logs[0].job_id is None, "日志保留但 job_id 置空（快照字段仍在）"
        assert logs[0].job_key == "system.monitoring_refresh"
        # DEF-066：任务删了归属快照仍在 ⇒ 历史日志仍能按用户收口
        assert logs[0].user_id == _user_of(token)
        db.close()
    finally:
        app.dependency_overrides.clear()


def test_logs_batch_delete_and_clean(tmp_path, monkeypatch):
    from backend.app.services.scheduled_job_runner import execute_job

    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        token = _register("job-logs-crud")
        db = SessionLocal()
        job = ScheduledJob(
            user_id=_user_of(token),
            name="日志清理测试",
            job_key="system.monitoring_refresh",
            trigger_type="cron",
            cron_expression="0 0 * * * *",
            status="active",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
        db.close()

        execute_job(job_id)
        execute_job(job_id)

        response = client.get("/api/schedule/logs", headers=_auth(token))
        assert response.status_code == 200
        assert response.json()["total"] == 2

        first_log_id = response.json()["items"][0]["id"]
        response = client.request(
            "DELETE", "/api/schedule/logs", headers=_auth(token), json={"ids": [first_log_id]}
        )
        assert response.status_code == 200
        assert response.json()["deleted"] == 1

        response = client.delete("/api/schedule/logs/clean", headers=_auth(token))
        assert response.status_code == 200
        assert response.json()["deleted"] == 1

        response = client.get(
            "/api/schedule/logs", headers=_auth(token), params={"status": "failed"}
        )
        assert response.json()["total"] == 0
    finally:
        app.dependency_overrides.clear()


def test_logs_reject_unknown_status(tmp_path):
    _override_database(tmp_path)
    try:
        token = _register("job-logs-status")
        response = client.get(
            "/api/schedule/logs", headers=_auth(token), params={"status": "exploded"}
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "LOG_STATUS_UNSUPPORTED"
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 调度器装配：DB 驱动 / 时区 / 旧形态已清除
# --------------------------------------------------------------------------- #
def test_build_scheduler_uses_shanghai_timezone():
    from backend.app.services.scheduler_service import build_scheduler

    scheduler = build_scheduler()
    try:
        assert str(scheduler.timezone) == "Asia/Shanghai"
    finally:
        pass  # 未启动，无需 shutdown


def test_legacy_auto_tasks_runner_is_gone():
    """60s 轮询 `run_due_auto_tasks` 与 `auto_tasks_runner` 已随重构删除。"""
    from backend.app.services import scheduler_service as sched

    assert not hasattr(sched, "run_due_auto_tasks")
    assert not hasattr(sched, "_execute_auto_task_background")
    assert not hasattr(sched, "build_due_publish_scheduler")
    assert not hasattr(sched, "start_due_publish_scheduler")


def test_ensure_builtin_jobs_seeds_from_application_initial_data(tmp_path, monkeypatch):
    """运行期补种以**应用初始化数据**为权威（设计 15 §4.5，R24）。

    ⚠️ 本用例的前身 `test_ensure_builtin_jobs_is_idempotent_and_matches_migration_seeds`
    （断言「运行期补种值 == 迁移 `_SYSTEM_SEEDS` 换算值」）**已被 R24 取代（2026-09-24）**：
    迁移播种段已删，种子改由随安装包分发的 `config/initial_data/scheduled_jobs.json`
    铺设 —— 「迁移 vs 运行期两条路径必须一致」的对照对象**不存在了**。
    原断言的**意图保留**（值不许漂）：只是对照源从迁移常量换成数据文件。

    ⚠️ **DEF-066（2026-09-24）再修订**：内置行**按用户各持一份**（原全局共享单行
    `user_id IS NULL` 已废）⇒ 本用例钉 per-user 播种：每个用户 10 行、值逐字段
    等于数据文件、二次调用不插行、**库里不允许再有 NULL 行**。
    """
    from backend.app.core.initial_data import load_initial_scheduled_jobs
    from backend.app.services.scheduler_service import ensure_builtin_jobs

    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        first_user = _make_user(SessionLocal, "seed-first")
        second_user = _make_user(SessionLocal, "seed-second")
        db = SessionLocal()
        first = ensure_builtin_jobs(db)
        assert len(first) == 26, "两个用户 × 13 个内置目标"
        second = ensure_builtin_jobs(db)
        assert second == [], "幂等：已存在的 (user, job_key) 不插行"
        rows = list(db.scalars(select(ScheduledJob).where(ScheduledJob.is_builtin.is_(True))))
        db.close()

        initial = {row["job_key"]: row for row in load_initial_scheduled_jobs()}
        by_user: dict[int, dict[str, ScheduledJob]] = {}
        for row in rows:
            assert row.user_id is not None, "DEF-066：不允许再有全局行（user_id IS NULL）"
            by_user.setdefault(row.user_id, {})[row.job_key] = row
        assert set(by_user) == {first_user, second_user}, "每个用户各持一份"
        for user_id, user_rows in by_user.items():
            assert set(user_rows) == set(initial), f"user {user_id} 的 key 集合必须一一对应"
            for key, row in user_rows.items():
                expected = initial[key]
                assert row.name == expected["name"], key
                assert row.trigger_type == expected["trigger_type"], key
                assert row.cron_expression == expected["cron_expression"], key
                assert row.status == expected["status"], key
                assert row.params == expected["params"], key
                assert (row.misfire_policy, row.concurrent) == (
                    expected["misfire_policy"],
                    expected["concurrent"],
                ), key
    finally:
        app.dependency_overrides.clear()


def test_register_seeds_builtin_jobs_for_the_new_user(tmp_path, monkeypatch):
    """DEF-066：新用户**注册即播种**（启动播种只覆盖启动时已存在的用户）。"""
    from backend.app.core.initial_data import load_initial_scheduled_jobs

    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        # 先有一个老用户（启动播种的口径）
        _make_user(SessionLocal, "seed-old")
        token = _register("seed-new")
        new_user = _user_of(token)

        db = SessionLocal()
        try:
            rows = db.scalars(
                select(ScheduledJob).where(ScheduledJob.user_id == new_user)
            ).all()
            assert {row.job_key for row in rows} == {
                row["job_key"] for row in load_initial_scheduled_jobs()
            }, "注册完打开任务中心就应有这 12 条"
        finally:
            db.close()

        # 列表端点也只返回本人的
        listed = client.get("/api/schedule/jobs", headers=_auth(token)).json()
        assert listed["total"] >= 7
        assert all(item["user_id"] == new_user for item in listed["items"])
    finally:
        app.dependency_overrides.clear()


def test_sync_jobs_from_db_registers_active_rows_only(tmp_path, monkeypatch):
    from backend.app.services.scheduler_service import build_scheduler, sync_jobs_from_db

    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        # DEF-066：内置行按用户播种 ⇒ 先有用户，sync 里的 ensure 才会铺出 10 行。
        # 测试行用 auto_ops 键（不用 system.* —— 同用户同 key 会被判重挡掉，
        # 那是「只补缺失」的正确语义，不能用来造假数字）。
        user_id = _make_user(SessionLocal, "sync-register")
        db = SessionLocal()
        db.add(ScheduledJob(
            user_id=user_id, name="活跃",
            job_key="auto_ops.keyword_pipeline", trigger_type="cron",
            cron_expression="0 0 * * * *", status="active",
            params={"auto_task_id": 1},
        ))
        db.add(ScheduledJob(
            user_id=user_id, name="暂停",
            job_key="auto_ops.keyword_pipeline", trigger_type="cron",
            cron_expression="0 0 * * * *", status="paused",
            params={"auto_task_id": 1},
        ))
        db.commit()
        db.close()

        scheduler = build_scheduler()
        stats = sync_jobs_from_db(scheduler)
        ids = {job.id for job in scheduler.get_jobs()}
        # 活跃行 = 测试行 1 + 应用初始化数据播种的 12 行（R24 起全 active）；
        # 其中 `system.no_traffic_cleanup` 是 manual 档、**不参与调度装配** ⇒ 注册 12。
        assert stats["registered"] == 13
        assert len(ids) == 13
        assert all(job_id.startswith("job_") for job_id in ids)
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# misfire / 并发 → APScheduler 参数映射
# --------------------------------------------------------------------------- #
def test_misfire_policy_maps_to_apscheduler_params():
    from backend.app.services.schedule_triggers import misfire_params

    assert misfire_params("fire_now") == (None, False)
    assert misfire_params("once") == (None, True)
    assert misfire_params("discard") == (1, True)
    # 未知值回默认档（once）
    assert misfire_params("nonsense") == (None, True)


def test_max_instances_is_capped_by_registry():
    from backend.app.services.schedule_triggers import max_instances_for
    from backend.app.models import ScheduledJob

    job = ScheduledJob(concurrent="allow", trigger_type="cron", cron_expression="0 * * * * *")
    assert max_instances_for(job, registry_allows_concurrent=False) == 1
    assert max_instances_for(job, registry_allows_concurrent=True) == 3
    job.concurrent = "forbid"
    assert max_instances_for(job, registry_allows_concurrent=True) == 1


# --------------------------------------------------------------------------- #
# auto-tasks API 与调度行联动
# --------------------------------------------------------------------------- #
def _make_pc_and_creator_accounts(SessionLocal, token: str | None = None) -> tuple[int, int]:
    """造 PC + creator 两个账号，**归属指定 token 的用户**（多租户用例必需）。

    不传 token 时回退到库中第一个用户（单租户用例的便利路径）；跨租户用例必须
    传 token —— 否则账号挂到别人名下，`POST /api/auto-tasks` 会因账号不属于
    调用者而 404（DEF-062 记录）。
    """
    from backend.app.core.security import decode_token
    from backend.app.models import PlatformAccount

    db = SessionLocal()
    if token is None:
        user = db.scalars(select(User)).first()
        if user is None:
            raise AssertionError("夹具库没有用户：请先 _register() 或显式传 token")
        user_id = user.id
    else:
        user_id = decode_token(token)["user_id"]
    pc = PlatformAccount(user_id=user_id, platform="xhs", sub_type="pc", nickname="PC")
    creator = PlatformAccount(user_id=user_id, platform="xhs", sub_type="creator", nickname="C")
    db.add_all([pc, creator])
    db.commit()
    db.refresh(pc)
    db.refresh(creator)
    ids = (pc.id, creator.id)
    db.close()
    return ids


def _make_auto_task(SessionLocal, token: str) -> int:
    """造一个**属于该 token 用户**的 auto_task（auto_ops 调度行的合法 auto_task_id）。"""
    pc_id, creator_id = _make_pc_and_creator_accounts(SessionLocal, token)
    response = client.post(
        "/api/auto-tasks",
        headers=_auth(token),
        json={
            "name": "管线任务",
            "keywords": ["露营"],
            "pc_account_id": pc_id,
            "creator_account_id": creator_id,
        },
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_create_auto_task_creates_linked_job_row(tmp_path, monkeypatch):
    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        token = _register("auto-task-create")
        pc_id, creator_id = _make_pc_and_creator_accounts(SessionLocal)

        # 不传 schedule ⇒ 惰性默认（paused，等价旧 schedule_type="manual"）
        response = client.post(
            "/api/auto-tasks",
            headers=_auth(token),
            json={
                "name": "露营装备",
                "keywords": ["露营"],
                "pc_account_id": pc_id,
                "creator_account_id": creator_id,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["schedule"]["job_id"] > 0
        assert body["schedule"]["status"] == "paused"
        assert body["schedule"]["cron_expression"] == "0 0 9 * * *"

        db = SessionLocal()
        job = db.get(ScheduledJob, body["schedule"]["job_id"])
        assert job is not None
        assert job.job_key == "auto_ops.keyword_pipeline"
        assert job.params == {"auto_task_id": body["id"], "publish_mode": "scheduled"}
        assert job.is_builtin is False
        db.close()

        # 传 schedule（每天 18:30）⇒ job 行 cron 落地
        response = client.post(
            "/api/auto-tasks",
            headers=_auth(token),
            json={
                "name": "早餐",
                "keywords": ["早餐"],
                "pc_account_id": pc_id,
                "creator_account_id": creator_id,
                "schedule": {"trigger_type": "cron", "cron_expression": "0 30 18 * * *"},
            },
        )
        assert response.status_code == 200
        assert response.json()["schedule"]["cron_expression"] == "0 30 18 * * *"
        assert response.json()["schedule"]["status"] == "active"
    finally:
        app.dependency_overrides.clear()


def test_update_auto_task_schedule_rewrites_job_row(tmp_path, monkeypatch):
    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        token = _register("auto-task-update")
        pc_id, creator_id = _make_pc_and_creator_accounts(SessionLocal)
        response = client.post(
            "/api/auto-tasks",
            headers=_auth(token),
            json={
                "name": "可改调度",
                "keywords": ["帐篷"],
                "pc_account_id": pc_id,
                "creator_account_id": creator_id,
                "schedule": {"trigger_type": "cron", "cron_expression": "0 0 */2 * * *"},
            },
        )
        task_id = response.json()["id"]

        response = client.patch(
            f"/api/auto-tasks/{task_id}",
            headers=_auth(token),
            json={"schedule": {"trigger_type": "at", "run_at": (shanghai_now() + timedelta(days=1)).isoformat()}},
        )
        assert response.status_code == 200
        schedule = response.json()["schedule"]
        assert schedule["trigger_type"] == "at"
        assert schedule["run_at"] is not None
        assert schedule["cron_expression"] == ""
    finally:
        app.dependency_overrides.clear()


def test_run_auto_task_missing_task_returns_404(tmp_path, monkeypatch):
    """立即执行不存在的任务 ⇒ 404（归属闸；行都没了不存在"执行"）。

    管线失败 → failed 日志的路径在服务层覆盖
    （`test_execute_job_failure_sets_backoff_and_gate_skips_next_scheduled_run`）。
    """
    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        token = _register("auto-task-run")
        response = client.post(f"/api/auto-tasks/999999/run", headers=_auth(token))
        assert response.status_code == 404
        assert response.json()["detail"] == "Auto task not found"
    finally:
        app.dependency_overrides.clear()


def test_schedule_status_shape_and_auth(tmp_path, monkeypatch):
    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        # 未登录 ⇒ 401
        response = client.get("/api/schedule/status")
        assert response.status_code == 401

        token = _register("job-status")
        response = client.get("/api/schedule/status", headers=_auth(token))
        assert response.status_code == 200
        payload = response.json()
        assert payload["enabled"] in (True, False)
        assert payload["running"] in (True, False)
        assert payload["timezone"] == "Asia/Shanghai"
        assert set(payload["stats"]) == {"total", "active", "paused", "completed"}
        assert payload["jobs"] == []
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 存量迁移（y8z9a0b1c2d3）：旧 auto_tasks 四态 → job 行
# --------------------------------------------------------------------------- #
@pytest.mark.slow
def test_migration_converts_legacy_auto_tasks_four_ways(tmp_path):
    """真跑 alembic upgrade，钉住四态换算（weekly 的 isoweekday → cron dow 是关键）。"""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        # `users` 必须存在：后续迁移用 batch_alter_table 改 `scheduled_jobs`，
        # 而 batch 模式会**反射整表**（含 FK 目标）；`scheduled_jobs.user_id`
        # 指向 `users.id`，缺表就 NoSuchTableError: users（DEF-062 记录）。
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
        # ⚠️ **必须真插一行 `users`（2026-10-04）**：建表还不够 ——
        # 全局 `PRAGMA foreign_keys=ON`（`core/database.py` 注册在 Engine 类上，M5 引入）
        # 之后，迁移里 `INSERT INTO scheduled_jobs (user_id=1, …)` 会撞
        # `FOREIGN KEY constraint failed`（此前 FK=OFF，缺行也能过）。
        # 外键指向的行必须由夹具造出来，不能假定「迁移自己会建」。
        conn.execute(
            "INSERT INTO users (id, username, password_hash, is_active, created_at)"
            " VALUES (1, 'legacy-user', 'x', 1, datetime('now'))"
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
        rows = [
            ("每日任务", "daily", "09:30", "", 24, "active"),
            ("每周任务", "weekly", "18:00", "1,5,7", 24, "active"),
            ("间隔任务", "interval", "09:00", "", 6, "active"),
            ("手动任务", "manual", "09:00", "", 24, "active"),
            ("暂停任务", "daily", "07:00", "", 24, "paused"),
        ]
        for name, kind, time_text, days, hours, status in rows:
            conn.execute(
                "INSERT INTO auto_tasks (user_id, name, keywords, pc_account_id, creator_account_id,"
                " schedule_type, schedule_time, schedule_days, schedule_interval_hours, status, created_at)"
                " VALUES (1, ?, '[\"k\"]', 1, 1, ?, ?, ?, ?, ?, datetime('now'))",
                (name, kind, time_text, days, hours, status),
            )
        # ⚠️ 再补上「区间内后续迁移会碰、但本夹具不建」的表（DEF-082）：`upgrade head`
        # 会把 `(x7y8z9a0b1c2, head]` 里的**全部**迁移跑一遍，缺一张表就红在这里
        # （head 迁移 `l3m4n5o6p7q8` 要 `notes`）。集中一处维护，见 conftest 的说明。
        create_later_migration_tables(conn)
        conn.commit()
    finally:
        conn.close()

    # 先 stamp 到本迁移的父 revision：夹具库只有 auto_tasks 一张表，从初始
    # migration 从头跑会撞「CREATE TABLE auto_tasks 已存在」。
    run_alembic(db_path, "stamp", "x7y8z9a0b1c2")
    run_alembic(db_path, "upgrade", "head")

    conn = sqlite3.connect(db_path)
    try:
        converted = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT t.name, j.trigger_type, j.cron_expression, j.status"
                " FROM auto_tasks t JOIN scheduled_jobs j ON j.id = t.schedule_job_id"
            )
        }
        columns = {row[1] for row in conn.execute("PRAGMA table_info(auto_tasks)")}
        job_columns = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_jobs)")}
        log_columns = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_job_logs)")}
    finally:
        conn.close()

    # 四态换算
    assert converted["每日任务"] == ("cron", "0 30 9 * * *", "active")
    # isoweekday 1,5,7（周一/五/日）→ cron dow 0,4,6（APScheduler 0=周一）
    assert converted["每周任务"] == ("cron", "0 0 18 * * * 0,4,6", "active")
    # R14：interval 退役 —— 6 小时间隔行被迁移换算成等价 cron（逐字符）
    assert converted["间隔任务"] == ("cron", _interval_to_cron(21600), "active")
    assert converted["间隔任务"][1] == "0 0 */6 * * *"
    # manual ⇒ 惰性默认 + paused
    assert converted["手动任务"] == ("cron", "0 0 9 * * *", "paused")
    # 任务本身 paused ⇒ 调度行也 paused
    assert converted["暂停任务"] == ("cron", "0 0 7 * * *", "paused")
    # 旧列删净（含 next_run_at）
    assert "schedule_type" not in columns
    assert "next_run_at" not in columns
    assert "schedule_job_id" in columns
    # R14：scheduled_jobs.interval_seconds 列已彻底删除
    assert "interval_seconds" not in job_columns
    # R20 / R22（2026-09-24 用户口径「**数据库物理删除**」）：job_group 列已彻底删除。
    # ⚠️ 这两条是「物理删除」的**唯一硬证据** —— UI 看不到 ≠ 列已删（本轮实踩教训）。
    assert "job_group" not in job_columns
    assert "job_group" not in log_columns


def test_schedule_endpoints_require_authentication(tmp_path):
    _override_database(tmp_path)
    try:
        assert client.get("/api/schedule/jobs").status_code == 401
        assert client.get("/api/schedule/logs").status_code == 401
        assert client.post("/api/schedule/cron/validate", json={"cron_expression": "0 0 9 * * *"}).status_code == 401
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# R20 / R22 · 分组字段物理删除（2026-09-24 用户口径「数据库物理删除」）
# --------------------------------------------------------------------------- #
@pytest.mark.slow
def test_migration_k1l2m3n4o5p6_drops_job_group_both_tables(tmp_path):
    """真跑 `upgrade`，钉住 `job_group` 在**两张表**上都已删除（源列 + 日志快照列）。

    用户口径（本轮）：
    > R20/R22 只去 UI，job_group 列保留；若要物理删列须另起迁移并同步清日志快照字段
    > —— **数据库物理删除**

    ⇒ 判据不是「界面看不到了」，而是**迁移跑完后 PRAGMA 里没有这一列**。
    日志快照列同理：快照指向一个已不存在的源列毫无意义，两者同生共死。
    """
    import sqlite3

    db_path = tmp_path / "drop_group.db"
    conn = sqlite3.connect(db_path)
    try:
        # 夹具要走 **真实迁移链**（stamp 到父 revision 再 upgrade head），
        # 所以 stamp 落点必须与夹具库的真实结构**逐列一致** —— 否则后续迁移会对着一张
        # 不存在的表/列动手，或者更糟：stamp 撒谎之后 patch 出一张假表。
        #
        # 首版夹具踩了两个坑（都实跑复现过）：
        #   ① stamp `y8z9a0b1c2d3` 却没建 `scheduled_job_logs`
        #      ⇒ `z9a0b1c2d3e4` 给它 ADD COLUMN 时炸 `no such table`；
        #   ② 建的是 `y8z9a0b1c2d3` 的产出形（没有 `progress` / `job_trigger_type`）
        #      却 stamp 到 `h8i9j0k1l2m3` ⇒ 后续「其它列不许误伤」的断言自欺
        #      （那两列是被 stamp 跳过的迁移加的，夹具里根本没有）。
        #
        # 下面这份 DDL 是 `data/spider_xhs.db` 在 `h8i9j0k1l2m3` 上的**原样抄录**
        # （含 `auto_tasks` 指向 `platform_accounts` 的 FK —— 夹具也得建出被指向
        #  的表，否则 batch 反射建不出 FK）。
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER NOT NULL,
                username VARCHAR(80) NOT NULL,
                password_hash VARCHAR(128) NOT NULL,
                created_at DATETIME NOT NULL,
                role VARCHAR(32) DEFAULT 'user',
                PRIMARY KEY (id)
            );
            CREATE INDEX ix_users_id ON users (id);
            CREATE UNIQUE INDEX ix_users_username ON users (username);
            CREATE INDEX ix_users_role ON users (role);

            CREATE TABLE platform_accounts (
                id INTEGER NOT NULL,
                PRIMARY KEY (id)
            );

            CREATE TABLE auto_tasks (
                id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name VARCHAR(128) NOT NULL,
                keywords JSON,
                pc_account_id INTEGER NOT NULL,
                creator_account_id INTEGER NOT NULL,
                ai_instruction TEXT NOT NULL,
                status VARCHAR(32) NOT NULL,
                last_run_at DATETIME,
                total_published INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                schedule_job_id INTEGER,
                PRIMARY KEY (id),
                FOREIGN KEY(user_id) REFERENCES users (id),
                FOREIGN KEY(pc_account_id) REFERENCES platform_accounts (id),
                FOREIGN KEY(creator_account_id) REFERENCES platform_accounts (id)
            );
            CREATE INDEX ix_auto_tasks_schedule_job_id ON auto_tasks (schedule_job_id);
            CREATE INDEX ix_auto_tasks_user_id ON auto_tasks (user_id);

            CREATE TABLE scheduled_jobs (
                id INTEGER NOT NULL,
                user_id INTEGER,
                name VARCHAR(128) NOT NULL,
                job_group VARCHAR(32) DEFAULT 'DEFAULT' NOT NULL,
                job_key VARCHAR(128) NOT NULL,
                trigger_type VARCHAR(16) DEFAULT 'cron' NOT NULL,
                cron_expression VARCHAR(64) DEFAULT ('') NOT NULL,
                run_at DATETIME,
                misfire_policy VARCHAR(16) DEFAULT 'once' NOT NULL,
                concurrent VARCHAR(16) DEFAULT 'forbid' NOT NULL,
                params JSON,
                status VARCHAR(16) DEFAULT 'active' NOT NULL,
                remark VARCHAR(500) DEFAULT ('') NOT NULL,
                is_builtin BOOLEAN DEFAULT 0 NOT NULL,
                last_run_at DATETIME,
                next_run_at DATETIME,
                run_count INTEGER DEFAULT '0' NOT NULL,
                fail_count INTEGER DEFAULT '0' NOT NULL,
                consecutive_failures INTEGER DEFAULT '0' NOT NULL,
                backoff_until DATETIME,
                last_status VARCHAR(16),
                created_at DATETIME NOT NULL,
                updated_at DATETIME,
                PRIMARY KEY (id),
                FOREIGN KEY(user_id) REFERENCES users (id)
            );
            CREATE INDEX ix_scheduled_jobs_user_id ON scheduled_jobs (user_id);
            CREATE INDEX ix_scheduled_jobs_job_key ON scheduled_jobs (job_key);
            CREATE INDEX ix_scheduled_jobs_job_group ON scheduled_jobs (job_group);
            CREATE INDEX ix_scheduled_jobs_status ON scheduled_jobs (status);

            CREATE TABLE scheduled_job_logs (
                id INTEGER NOT NULL,
                job_id INTEGER,
                job_name VARCHAR(128) DEFAULT '' NOT NULL,
                job_key VARCHAR(128) DEFAULT '' NOT NULL,
                job_group VARCHAR(32) DEFAULT 'DEFAULT' NOT NULL,
                trigger_type VARCHAR(16) DEFAULT 'scheduled' NOT NULL,
                status VARCHAR(16) DEFAULT 'running' NOT NULL,
                started_at DATETIME NOT NULL,
                finished_at DATETIME,
                duration_ms INTEGER,
                message TEXT DEFAULT '' NOT NULL,
                exception_info TEXT DEFAULT '' NOT NULL,
                result JSON,
                created_at DATETIME NOT NULL,
                config_snapshot JSON,
                job_trigger_type VARCHAR(16) DEFAULT '' NOT NULL,
                progress INTEGER DEFAULT '0' NOT NULL,
                PRIMARY KEY (id),
                FOREIGN KEY(job_id) REFERENCES scheduled_jobs (id) ON DELETE SET NULL
            );
            CREATE INDEX ix_scheduled_job_logs_job_id ON scheduled_job_logs (job_id);
            CREATE INDEX ix_scheduled_job_logs_status ON scheduled_job_logs (status);
            CREATE INDEX ix_scheduled_job_logs_created_at ON scheduled_job_logs (created_at);
            CREATE INDEX ix_scheduled_job_logs_job_trigger_type
                ON scheduled_job_logs (job_trigger_type);
            """
        )
        # 塞一行数据：证明删列是**定点操作**，不是整表重建丢数据
        conn.execute(
            "INSERT INTO scheduled_jobs (id, name, job_group, job_key, created_at)"
            " VALUES (1, '钉住用任务', 'SYSTEM', 'probe.keep_row', datetime('now'))"
        )
        conn.execute(
            "INSERT INTO scheduled_job_logs"
            " (id, job_id, job_name, job_key, job_group, started_at, created_at, progress)"
            " VALUES (1, 1, '钉住用任务', 'probe.keep_row', 'SYSTEM', datetime('now'),"
            " datetime('now'), 42)"
        )
        conn.commit()
    finally:
        conn.close()

    # stamp 到 k1l2m3n4o5p6 的**父 revision**（`h8i9j0k1l2m3`），从那里升级。
    # 注意：这里**必须**用 stamp+定点 upgrade，**绝不能用 downgrade 对齐** ——
    # `y8z9a0b1c2d3.downgrade()` 会 `drop_table("scheduled_jobs")`，把夹具砸成空库。
    #
    # ⚠️ 目标是 `k1l2m3n4o5p6` 而非 head（DEF-066 起）：本夹具的钉子行是
    # `user_id IS NULL` 的全局行，而后续迁移 `a0b1c2d3e4f5` 的职责就是**按用户克隆后
    # 删除全局行**（夹具库 users 为空 ⇒ 直接删）—— 用 head 会让这条钉子行被后续迁移
    # 合理清掉，测试就红了。定点升级让本条只盯 `job_group` 删列这一件事。
    # （DATABASE_URL 等细节收在 conftest.run_alembic）
    run_alembic(db_path, "stamp", "h8i9j0k1l2m3")
    run_alembic(db_path, "upgrade", "k1l2m3n4o5p6")

    conn = sqlite3.connect(db_path)
    try:
        job_columns = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_jobs)")}
        log_columns = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_job_logs)")}
        job_indexes = {row[1] for row in conn.execute("PRAGMA index_list(scheduled_jobs)")}
        log_indexes = {row[1] for row in conn.execute("PRAGMA index_list(scheduled_job_logs)")}
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        # 数据保真：删列不能顺手把行也丢了（batch 重建表的经典事故）
        kept_job = conn.execute(
            "SELECT name, job_key, status FROM scheduled_jobs WHERE id = 1"
        ).fetchone()
        kept_log = conn.execute(
            "SELECT job_name, job_key, progress FROM scheduled_job_logs WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()

    assert version == "k1l2m3n4o5p6"
    # 两张表的 job_group 都物理删除
    assert "job_group" not in job_columns
    assert "job_group" not in log_columns
    # 指向被删列的索引也一并退（留着就是一个建不出来的索引）
    assert "ix_scheduled_jobs_job_group" not in job_indexes
    # 其它列不许误伤（删列是定点操作，不是整表重建丢字段）
    for kept in ("job_key", "trigger_type", "cron_expression", "is_builtin", "status"):
        assert kept in job_columns
    # 日志表的快照列/进度列必须还在（夹具刻意贴合 h8i9j0k1l2m3 的真实形，
    # 就是为了让「误伤」这条断言有牙）
    for kept in (
        "job_name",
        "job_key",
        "job_trigger_type",
        "progress",
        "config_snapshot",
        "ix_scheduled_job_logs_job_trigger_type",
    ):
        if kept.startswith("ix_"):
            assert kept in log_indexes
        else:
            assert kept in log_columns
    # 行还在，内容没被重建弄丢
    assert kept_job == ("钉住用任务", "probe.keep_row", "active")
    assert kept_log == ("钉住用任务", "probe.keep_row", 42)


@pytest.mark.slow
def test_migration_a0b1c2d3e4f5_isolates_task_center(tmp_path):
    """真跑 `upgrade`，钉住账号维度隔离迁移（DEF-066，设计 15 §4.1.1）四件事：

    ① 日志表加 `user_id`（列 + 索引）；
    ② 全局内置行（user_id IS NULL）**按每个用户克隆**（account_ids 重置为空），
       原行删除；该用户已有同 key 行则跳过；
    ③ 日志 `user_id` 从任务行回填；孤儿 / 全局行日志兜底归属**首个用户**；
       首个用户的克隆行继承原全局行的历史日志链接；
    ④ 存量用户行的 `account_ids` 按归属**清洗**（滤掉不属于行主的 id）。
    """
    import json
    import sqlite3

    db_path = tmp_path / "isolation.db"
    conn = sqlite3.connect(db_path)
    try:
        # 夹具 = `k1l2m3n4o5p6` 之后的真实形态（无 job_group、日志表无 user_id）。
        # `users` / `platform_accounts` 必须存在：batch_alter_table 反射整表（含 FK 目标）。
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER NOT NULL,
                username VARCHAR(80) NOT NULL,
                password_hash VARCHAR(128) NOT NULL,
                created_at DATETIME NOT NULL,
                role VARCHAR(32) DEFAULT 'user',
                PRIMARY KEY (id)
            );
            CREATE TABLE platform_accounts (
                id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                platform VARCHAR(32) DEFAULT 'xhs',
                sub_type VARCHAR(32),
                nickname VARCHAR(128) DEFAULT '',
                external_user_id VARCHAR(128),
                deleted_at DATETIME,
                PRIMARY KEY (id),
                FOREIGN KEY(user_id) REFERENCES users (id)
            );
            CREATE TABLE scheduled_jobs (
                id INTEGER NOT NULL,
                user_id INTEGER,
                name VARCHAR(128) NOT NULL,
                job_key VARCHAR(128) NOT NULL,
                trigger_type VARCHAR(16) DEFAULT 'cron' NOT NULL,
                cron_expression VARCHAR(64) DEFAULT ('') NOT NULL,
                run_at DATETIME,
                misfire_policy VARCHAR(16) DEFAULT 'once' NOT NULL,
                concurrent VARCHAR(16) DEFAULT 'forbid' NOT NULL,
                params JSON,
                status VARCHAR(16) DEFAULT 'active' NOT NULL,
                remark VARCHAR(500) DEFAULT ('') NOT NULL,
                is_builtin BOOLEAN DEFAULT 0 NOT NULL,
                last_run_at DATETIME,
                next_run_at DATETIME,
                run_count INTEGER DEFAULT '0' NOT NULL,
                fail_count INTEGER DEFAULT '0' NOT NULL,
                consecutive_failures INTEGER DEFAULT '0' NOT NULL,
                backoff_until DATETIME,
                last_status VARCHAR(16),
                created_at DATETIME NOT NULL,
                updated_at DATETIME,
                PRIMARY KEY (id),
                FOREIGN KEY(user_id) REFERENCES users (id)
            );
            CREATE TABLE scheduled_job_logs (
                id INTEGER NOT NULL,
                job_id INTEGER,
                job_name VARCHAR(128) DEFAULT '' NOT NULL,
                job_key VARCHAR(128) DEFAULT '' NOT NULL,
                trigger_type VARCHAR(16) DEFAULT 'scheduled' NOT NULL,
                status VARCHAR(16) DEFAULT 'running' NOT NULL,
                started_at DATETIME NOT NULL,
                finished_at DATETIME,
                duration_ms INTEGER,
                message TEXT DEFAULT '' NOT NULL,
                exception_info TEXT DEFAULT '' NOT NULL,
                result JSON,
                created_at DATETIME NOT NULL,
                config_snapshot JSON,
                job_trigger_type VARCHAR(16) DEFAULT '' NOT NULL,
                progress INTEGER DEFAULT '0' NOT NULL,
                PRIMARY KEY (id),
                FOREIGN KEY(job_id) REFERENCES scheduled_jobs (id) ON DELETE SET NULL
            );
            """
        )
        conn.execute("INSERT INTO users (id, username, password_hash, created_at) VALUES (1, 'u1', 'x', datetime('now'))")
        conn.execute("INSERT INTO users (id, username, password_hash, created_at) VALUES (2, 'u2', 'x', datetime('now'))")
        # 账号 1/2 归 u1，账号 3 归 u2
        conn.execute("INSERT INTO platform_accounts (id, user_id, nickname) VALUES (1, 1, 'u1-甲')")
        conn.execute("INSERT INTO platform_accounts (id, user_id, nickname) VALUES (2, 1, 'u1-乙')")
        conn.execute("INSERT INTO platform_accounts (id, user_id, nickname) VALUES (3, 2, 'u2-的')")
        # ① 全局内置行（user_id IS NULL）—— cookie 行还带着 A 配的账号 id（脏数据）
        conn.execute(
            "INSERT INTO scheduled_jobs (id, user_id, name, job_key, trigger_type, cron_expression,"
            " params, status, is_builtin, created_at)"
            " VALUES (1, NULL, '账号 Cookie 巡检', 'system.cookie_health_check', 'cron', '0 0 * * * *',"
            " '{\"account_ids\": [2]}', 'active', 1, datetime('now'))"
        )
        # ② 全局内置行 note_sync（空 account_ids）
        conn.execute(
            "INSERT INTO scheduled_jobs (id, user_id, name, job_key, trigger_type, cron_expression,"
            " params, status, is_builtin, created_at)"
            " VALUES (2, NULL, '账号笔记同步', 'system.note_sync', 'cron', '0 0 4 * * *',"
            " '{\"account_ids\": []}', 'active', 1, datetime('now'))"
        )
        # ③ u1 自建 auto_ops 行（不动）
        conn.execute(
            "INSERT INTO scheduled_jobs (id, user_id, name, job_key, params, status, is_builtin, created_at)"
            " VALUES (3, 1, '自动运营 · 我的', 'auto_ops.keyword_pipeline', '{\"auto_task_id\": 7}',"
            " 'active', 0, datetime('now'))"
        )
        # ④ u1 自建 note_sync 行，params 里混进** u2 的账号**（POST 不校验时期的脏数据）
        conn.execute(
            "INSERT INTO scheduled_jobs (id, user_id, name, job_key, params, status, is_builtin, created_at)"
            " VALUES (4, 1, '我的笔记同步', 'system.note_sync', '{\"account_ids\": [3]}',"
            " 'active', 1, datetime('now'))"
        )
        # 日志：全局行的历史（回填后归属首个用户、job_id 改指其克隆）、
        #       u1 自建行的、孤儿日志（任务早已删除）
        conn.execute(
            "INSERT INTO scheduled_job_logs (id, job_id, job_name, job_key, status, started_at,"
            " created_at) VALUES (1, 1, '账号 Cookie 巡检', 'system.cookie_health_check', 'success',"
            " datetime('now'), datetime('now'))"
        )
        conn.execute(
            "INSERT INTO scheduled_job_logs (id, job_id, job_name, job_key, status, started_at,"
            " created_at) VALUES (2, 3, '自动运营 · 我的', 'auto_ops.keyword_pipeline', 'success',"
            " datetime('now'), datetime('now'))"
        )
        conn.execute(
            "INSERT INTO scheduled_job_logs (id, job_id, job_name, job_key, status, started_at,"
            " created_at) VALUES (3, NULL, '早删的任务', 'system.due_publish', 'success',"
            " datetime('now'), datetime('now'))"
        )
        conn.commit()
    finally:
        conn.close()

    # 夹具 = `k1l2m3n4o5p6` 之后的真实形态（无 job_group、日志表无 user_id），
    # stamp 到 k1l2m3n4o5p6 再**定点** upgrade 到本迁移（细节收在 conftest.run_alembic）。
    # ⚠️ 不能 upgrade head：head 之后还有 `b2c3d4e5f6g7`（话题表合并，动 notes/tags）
    # 与 `c2d3e4f5a6b7`（清理任务改名）—— 本夹具只建了 scheduled_* 两张表，跑到后面
    # 的迁移会因缺表炸；且断言目标就是「版本号停在 a0b1c2d3e4f5」。
    run_alembic(db_path, "stamp", "k1l2m3n4o5p6")
    run_alembic(db_path, "upgrade", "a0b1c2d3e4f5")

    conn = sqlite3.connect(db_path)
    try:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        log_columns = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_job_logs)")}
        log_indexes = {row[1] for row in conn.execute("PRAGMA index_list(scheduled_job_logs)")}
        jobs = conn.execute(
            "SELECT id, user_id, job_key, params FROM scheduled_jobs ORDER BY id"
        ).fetchall()
        logs = conn.execute(
            "SELECT id, job_id, user_id FROM scheduled_job_logs ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    # ① head 推进 + 日志表新列 / 索引
    assert version == "a0b1c2d3e4f5"
    assert "user_id" in log_columns
    assert "ix_scheduled_job_logs_user_id" in log_indexes

    # ② 全局行清零：6 行 = 2 用户 × 2 个全局 key 的克隆（u1 的 note_sync 克隆被
    #    「已有同 key 行」跳过 ⇒ 5 个克隆）+ u1 原有的 2 行；没有任何 NULL 行
    assert all(row[1] is not None for row in jobs), "全局行必须清零"
    by_user_key = {(row[1], row[2]): row for row in jobs}
    assert (1, "auto_ops.keyword_pipeline") in by_user_key, "u1 自建行不许被动"
    assert (1, "system.cookie_health_check") in by_user_key
    assert (1, "system.note_sync") in by_user_key
    assert (2, "system.cookie_health_check") in by_user_key, "u2 也应有自己的巡检行"
    assert (2, "system.note_sync") in by_user_key, "u2 也应有自己的同步行"
    # 克隆行的 account_ids 一律重置为空（= 本人全部账号）
    for key in ("system.cookie_health_check", "system.note_sync"):
        for user_id in (1, 2):
            params = json.loads(by_user_key[(user_id, key)][3])
            assert params.get("account_ids") == [], f"{user_id}/{key} 的 account_ids 应重置"
    # u1 自建 note_sync 行的**跨租户 account_ids 被清洗**（3 是 u2 的账号）
    u1_note_sync = by_user_key[(1, "system.note_sync")]
    assert json.loads(u1_note_sync[3])["account_ids"] == []

    # ③ 日志归属：全局行日志 → 首个用户且 job_id 改指其克隆；u1 自建行日志 → u1；
    #    孤儿日志 → 首个用户、job_id 仍 NULL
    u1_cookie_clone_id = by_user_key[(1, "system.cookie_health_check")][0]
    assert logs[0] == (1, u1_cookie_clone_id, 1)
    assert logs[1] == (2, 3, 1)
    assert logs[2] == (3, None, 1)


def test_job_group_has_no_reference_in_backend_or_frontend():
    """全仓静态守卫：`job_group` / `JOB_GROUP` / `jobGroupLabel` 的引用已退净。

    ⚠️ 这条防的是**回潮**：删列迁移之后若某处又写回 `job_group=...`，
    运行期会在 INSERT 时炸（列不存在），但那是用户点下去才知道；这里提前红。

    ⚠️ 允许的例外：`models/scheduled_job.py` 的注释（记录「已物理删除」这件事本身）。
    迁移文件不在扫描范围内（它的职责就是删/恢复这一列）。
    """
    offenders: list[str] = []
    scan_dirs = [ROOT / "backend" / "app", ROOT / "frontend" / "src"]
    allow_files = {ROOT / "backend" / "app" / "models" / "scheduled_job.py"}
    for base in scan_dirs:
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx"}:
                continue
            if path in allow_files:
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                # 注释行不算引用（文档可以提这件事）
                if stripped.startswith("#") or stripped.startswith("//"):
                    continue
                if "job_group" in line or "JOB_GROUP" in line or "jobGroupLabel" in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {stripped}")

    assert not offenders, "job_group 已物理删除，以下位置仍在引用它：\n" + "\n".join(offenders)


# ⚠️ 本用例与下面的变异用例曾**并行抢同一个文件**：页面侧变异原先是「就地改磁盘、跑完还原」
# ⇒ `-n auto` 下正向断言会读到「已被改坏、还没还原」的内容（DEF-078；2026-09-27 实测
# `R22 日志详情弹窗不再取 snapshot.job_group` 随机变红，而单跑脚本 / `-n0` 全绿）。
# **修法在脚本侧**（改成「读时内存变异」`readSource`），**不是**在这里加 `xdist_group` ——
# 那个标记默认不参与调度（需 `--dist loadgroup`），加了等于没加。
def test_schedule_center_behaviour_script_passes():
    """任务中心前端行为脚本（esbuild 打包真实模块 + 真实源文件结构断言）。

    ⚠️ 上一轮遗留：`tests/frontend/schedule_center.cjs` 只被手动跑过，**没接进 pytest**
    ⇒ 没人跑就等于没有这条安全网。本轮补上，并把 R20/R22 的「物理删除」守卫
    与两个反向变异（`groupfield` / `groupsnapshot`）一起纳入。

    ⚠️ **不设 skip**：node 缺失就让用例红（AGENTS.md §2 本就把 node 当必需）。
    """
    import subprocess as _subprocess

    script = ROOT / "tests/frontend/schedule_center.cjs"
    assert script.exists(), "行为验证脚本缺失（tests/frontend/schedule_center.cjs）"
    try:
        result = _subprocess.run(
            ["node", str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except FileNotFoundError:  # pragma: no cover - 环境问题，不是逻辑分支
        pytest.fail("未找到 node：本机无法运行前端行为验证（AGENTS.md §2 已依赖 node 跑 tsc）")
    assert result.returncode == 0, (result.stdout + result.stderr)[-900:]


@pytest.mark.parametrize(
    "mutate",
    [
        "groupfield",
        "groupsnapshot",
        "accountscope",
        "accountlabel",
        "progressbar",
        "fakefull",
        "unknownzero",
        "tworeads",
        "pageread",
    ],
)
def test_schedule_center_group_mutations_are_caught(mutate):
    """R22 反向变异：把分组字段/快照项**改回去**，行为脚本必须变红。

    DEF-066 追加 `accountscope`：账号多选退回「仅编辑态」⇒ 新建态没有账号多选
    （归属校验上线后手抄 id 只会 400）—— 与分组变异同一层防线。

    2026-09-27 追加 `accountlabel`：「执行账号」退回只看 `params.account_ids`（job 行的
    **任务配置**，空 = 本人全部账号）⇒ 账号卡点「同步笔记」后执行详情又显示「全部账号」
    （用户报障原样复活）—— 与「快照要说清这次跑的是谁」同一层防线。

    2026-09-28 追加 `progressbar`：任务日志「任务进度」列的**不定量进度条**退回一行
    「进行中」文字 ⇒ 用户口径「任务进度列应展示任务进度条，而不是文本进行中」原样复活
    —— 与「进度列一律进度条」同一层防线。

    2026-09-28 同日第三轮追加四条（用户报障「进度条自动达到了 100% 但任务刚刚开始，
    点详情却是 0%」，设计 13 §43）：`fakefull`（未知比例退回 `percent={100}` 满格冒充）、
    `unknownzero`（未知判据失效 ⇒ 未知被谎报成 0%）、`tworeads`（详情弹窗退回自绘
    `log.progress`）、`pageread`（列表列绕开唯一实现自绘）—— 四条合起来守的是
    「**同一行数据只能有一个读数**」+「未知既不是 100% 也不是 0%」。

    没有这一层，上面那批 `not includes` 守卫可能只是「恰好为真」
    （DEF-063 同型：变异没接入 pytest = 变异不算数）。
    """
    import os
    import subprocess as _subprocess

    script = ROOT / "tests/frontend/schedule_center.cjs"
    env = dict(os.environ)
    env["SCHEDULE_MUTATE"] = mutate
    result = _subprocess.run(
        ["node", str(script)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, f"变异 {mutate} **未被抓住**（守卫只是恰好为真）：\n{output[-600:]}"


@pytest.mark.slow
def test_migration_c2d3e4f5a6b7_renames_cleanup_job(tmp_path):
    """真跑 `upgrade`，钉住清理任务改名迁移（2026-09-25 用户口径
    「笔记任务记录清理应扩展为队列详情记录清理……任务名称修改为队列记录清理」）四件事：

    ① `scheduled_jobs` 旧键行 ⇒ 新键 + 新中文名（一任务一行，不产生第二行）；
    ② `scheduled_job_logs` 的**执行时快照**（`job_key` / `job_name`）跟着改 ——
       只改任务行不改日志 ⇒ 同一任务的历史被劈成两半（任务中心按 job_key 筛选）；
    ③ **选择性**：别家的行（`system.note_sync` 等）一行不动；
    ④ 纯数据改写：`alembic_version` 前进到 head，表结构零变化。

    ⚠️ 夹具刻意停在 `b2c3d4e5f6g7`（本迁移的父 revision）再升级到 head ——
    本迁移只改数据，从任何更早的 revision 跑都能到，但没有父 revision 的
    「旧键行」上下文，断言就只剩形状了。
    """
    import sqlite3

    db_path = tmp_path / "rename.db"
    conn = sqlite3.connect(db_path)
    try:
        # 最小夹具：只建本迁移要碰的两张表（数据迁移，不反射整库）
        conn.executescript(
            """
            CREATE TABLE scheduled_jobs (
                id INTEGER NOT NULL,
                user_id INTEGER,
                name VARCHAR(128) NOT NULL,
                job_key VARCHAR(128) NOT NULL,
                status VARCHAR(16) DEFAULT 'active' NOT NULL,
                remark TEXT DEFAULT '' NOT NULL,
                created_at DATETIME NOT NULL,
                PRIMARY KEY (id)
            );
            CREATE TABLE scheduled_job_logs (
                id INTEGER NOT NULL,
                job_id INTEGER,
                job_name VARCHAR(128) DEFAULT '' NOT NULL,
                job_key VARCHAR(128) DEFAULT '' NOT NULL,
                created_at DATETIME NOT NULL,
                PRIMARY KEY (id)
            );
            """
        )
        conn.execute(
            "INSERT INTO scheduled_jobs (id, user_id, name, job_key, status, created_at)"
            " VALUES (1, 1, '笔记任务记录清理', 'system.note_task_record_cleanup', 'active', datetime('now'))"
        )
        conn.execute(
            "INSERT INTO scheduled_jobs (id, user_id, name, job_key, status, created_at)"
            " VALUES (2, 1, '账号笔记同步', 'system.note_sync', 'active', datetime('now'))"
        )
        conn.execute(
            "INSERT INTO scheduled_job_logs (id, job_id, job_name, job_key, created_at)"
            " VALUES (1, 1, '笔记任务记录清理', 'system.note_task_record_cleanup', datetime('now'))"
        )
        conn.execute(
            "INSERT INTO scheduled_job_logs (id, job_id, job_name, job_key, created_at)"
            " VALUES (2, 2, '账号笔记同步', 'system.note_sync', datetime('now'))"
        )
        # ⚠️ 补上「区间内后续迁移会碰、但本夹具不建」的表（DEF-082）：
        # `upgrade head` 会把 `(b2c3d4e5f6g7, head]` 里的全部迁移跑一遍。
        create_later_migration_tables(conn)
        conn.commit()
    finally:
        conn.close()

    run_alembic(db_path, "stamp", "b2c3d4e5f6g7")
    run_alembic(db_path, "upgrade", "head")

    conn = sqlite3.connect(db_path)
    try:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        jobs = conn.execute(
            "SELECT id, name, job_key FROM scheduled_jobs ORDER BY id"
        ).fetchall()
        logs = conn.execute(
            "SELECT id, job_name, job_key FROM scheduled_job_logs ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    # ⚠️ **不要写死 head 字符串**（原来写的是当时的 `f5a6b7c8d9e0`）：每加一条迁移
    # 这条断言都会红，而它真正要钉的只有「upgrade 之后 alembic_version 停在 head」——
    # 写死把「链没前进」与「head 变了」混成同一个红。现在从迁移目录现算。
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    expected_head = ScriptDirectory.from_config(
        AlembicConfig("backend/alembic.ini")
    ).get_current_head()
    assert version == expected_head, "迁移链前进到新 head"
    # ① 旧键行改名（不新增行 —— 一任务两行会让种子再补一份、清理跑两遍）
    # ⚠️ 中文名断言的是 **head 时刻的最终值**：本迁移（2026-09-25）把名字改成
    #    「队列记录清理」后，2026-10-05 的 `d2e3f4a5b6c7` 又改成了「清理记录表」
    #    （任务范围从「队列记录」扩到「所有指定记录表」，用户口径）。
    #    夹具从 `b2c3d4e5f6g7` 跑到 head，所以看到的是后者 —— 写死中间态会让这条
    #    用例在每次改名时红一次，而它真正要钉的是「旧键行了被改名 + 没多出一行」。
    #    原句：`assert jobs == [(1, "队列记录清理", ...), (2, "账号笔记同步", ...)]`
    assert jobs == [
        (1, "清理记录表", "system.queue_record_cleanup"),
        (2, "账号笔记同步", "system.note_sync"),
    ]
    # ② 历史日志快照跟着改（任务中心按 job_key 筛选，劈成两半历史就查不到）
    assert logs == [
        (1, "队列记录清理", "system.queue_record_cleanup"),
        (2, "账号笔记同步", "system.note_sync"),
    ]


def test_dedupe_seeded_scheduled_jobs_migration(tmp_path):
    """DEF-077 迁移 `i0j1k2l3m4n5`：收敛「播种影子行」，且**不误伤用户行**。

    2026-09-27 用户口径「队列记录清理 `system.queue_record_cleanup` 任务，在任务列表
    出现了两次，应只有只需要一个」。根因 = 改名迁移 `c2d3e4f5a6b7` 与「按新键播种」
    时序错开：旧键行还没被改名时，按**新键**判重的播种看不见它 ⇒ 又插一批；改名一落地
    两批撞成同键两行（6 个用户各 2 行，两批都被 apscheduler 注册 ⇒ 任务**双跑**）。

    为什么是「删播种行」而不是「加 `(user_id, job_key)` 唯一索引」：R18 明确允许
    「用户新建一条**同目标、不同调度**的任务行」—— 唯一索引会把这条合法用法一并禁掉。
    所以判据落在**播种行的文案特征**上（`remark` 以种子固定前缀开头）。

    本用例钉四件事（缺一条这个迁移就可能是「删错」或「没删」）：
    ① 同一 `(user_id, job_key)` 的**多条播种行** ⇒ 只留 `id` 最小的一条；
    ② **用户新建的同目标任务行** ⇒ **一行不动**（它是 R18 的合法数据，不是脏数据）；
    ③ 跨用户互不影响：同 key 不同 `user_id` 各留自己那行；
    ④ 幂等：把版本 stamp 回父 revision 再 upgrade，**零删除**。
    """
    import sqlite3

    db_path = tmp_path / "dedupe.db"
    seeded = "系统内置任务（应用初始化数据，随安装附有；保留期固定 15 天，只清终态记录）"
    seeded_new = "系统内置任务（应用初始化数据，随安装附有；队列详情记录保留期固定 15 天，只清终态记录）"
    rows = [
        # user 1：同一 job_key 三行 —— 两行播种（新旧文案）+ 一行用户新建
        (1, 1, "队列记录清理", "system.queue_record_cleanup", seeded),
        (2, 1, "队列记录清理", "system.queue_record_cleanup", seeded_new),
        (3, 1, "我的每小时清理", "system.queue_record_cleanup", "我自己加的，每小时跑一次"),
        # user 2：同 key 只有一行播种行 ⇒ 一行都不能少
        (4, 2, "队列记录清理", "system.queue_record_cleanup", seeded),
        # user 1 的另一个任务 ⇒ 完全不该被碰
        (5, 1, "账号笔记同步", "system.note_sync", seeded),
    ]
    conn = sqlite3.connect(db_path)
    try:
        # 最小夹具：只建本迁移要碰的表（数据迁移，不反射整库）。
        # `remark` 必须建 —— 本迁移的判据就落在它上面。
        # ⚠️ `users` 也要建**并插行**（2026-10-04）：全局 FK=ON 之后，
        # `scheduled_jobs.user_id` 的外键会对上面的 INSERT 生效 ⇒ 缺行即
        # `FOREIGN KEY constraint failed`（本夹具用到 user 1 / user 2 两个）。
        conn.execute(
            "CREATE TABLE users ("
            " id INTEGER PRIMARY KEY, username VARCHAR(64),"
            " password_hash VARCHAR(255), is_active BOOLEAN DEFAULT 1,"
            " created_at DATETIME)"
        )
        for uid in (1, 2):
            conn.execute(
                "INSERT INTO users (id, username, password_hash, is_active, created_at)"
                " VALUES (?, ?, 'x', 1, datetime('now'))",
                (uid, f"dedupe-user-{uid}"),
            )
        conn.execute(
            "CREATE TABLE scheduled_jobs ("
            " id INTEGER NOT NULL, user_id INTEGER, name VARCHAR(128) NOT NULL,"
            " job_key VARCHAR(128) NOT NULL, status VARCHAR(16) DEFAULT 'active' NOT NULL,"
            " remark TEXT DEFAULT '' NOT NULL, created_at DATETIME NOT NULL, PRIMARY KEY (id))"
        )
        # ⚠️ `scheduled_job_logs` 也要建（2026-09-27）：本用例跑的是 `upgrade head`，
        # 而 head 迁移 `j1k2l3m4n5o6` 会给这张表加 `account_id` 列 —— 表不存在则迁移
        # 报错，用例红得与它要验的 DEF-077 毫无关系（夹具缺失，不是被测行为坏了）。
        # 刻意**不含** `account_id`：那正是迁移要加的列，预置会让迁移重复加列而失败。
        conn.execute(
            "CREATE TABLE scheduled_job_logs ("
            " id INTEGER NOT NULL, status VARCHAR(16) DEFAULT 'running' NOT NULL,"
            " PRIMARY KEY (id))"
        )
        for job_id, user_id, name, job_key, remark in rows:
            conn.execute(
                "INSERT INTO scheduled_jobs (id, user_id, name, job_key, status, remark, created_at)"
                " VALUES (?, ?, ?, ?, 'active', ?, datetime('now'))",
                (job_id, user_id, name, job_key, remark),
            )
        # ⚠️ 补上「区间内后续迁移会碰、但本夹具不建」的表（DEF-082）：
        # `upgrade head` 会把 `(h9i0j1k2l3m4, head]` 里的全部迁移跑一遍。
        create_later_migration_tables(conn)
        conn.commit()
    finally:
        conn.close()

    def snapshot() -> list[tuple]:
        handle = sqlite3.connect(db_path)
        try:
            return handle.execute(
                "SELECT id, remark FROM scheduled_jobs ORDER BY id"
            ).fetchall()
        finally:
            handle.close()

    run_alembic(db_path, "stamp", "h9i0j1k2l3m4")
    run_alembic(db_path, "upgrade", "head")

    jobs = snapshot()
    # ① 组内播种行只留 id 最小（id 1 留 / id 2 删）；③ 跨用户各自保留（id 4）；
    # ② 用户新建行（id 3）与别的任务（id 5）必须一行不动。
    assert [row[0] for row in jobs] == [1, 3, 4, 5], (
        "播种影子行（id 2）应被收敛，用户行（id 3）与其它任务（id 5）必须一行不动"
    )
    # 保留行字段零改动（§4.5「已有行一律不覆盖」）
    assert jobs[0][1] == seeded

    # ④ 幂等：stamp 回父 revision 再 upgrade ⇒ 零删除（收敛后条件不再命中）
    run_alembic(db_path, "stamp", "h9i0j1k2l3m4")
    run_alembic(db_path, "upgrade", "head")
    assert [row[0] for row in snapshot()] == [1, 3, 4, 5], (
        "重复执行不得再删任何行（本迁移必须幂等）"
    )


def test_migration_d5e6f7a8b9c0_backfills_blank_default_model(tmp_path):
    """真跑 `upgrade`，钉住 v1.9 服务商化迁移的**存量 `default_model` 回填**（设计 17 §13.1）。

    背景：v1.9 起「默认模型必填」，但 v1.9 **之前** `default_model` 允许空串 ⇒ 存量行可能是空。
    留空 = 一条永远调不通、而用户看不出原因的服务商（`_complete()` 会因缺 model_name 直接
    `ValueError`）。故迁移必须回填，且两条路径各自钉住：

    ① `models_json` 目录里**有**模型 ⇒ 取**第一个**可用模型名（真实的、能直接调通）；
    ② 目录**也空** ⇒ 落占位文案（**不猜真实模型名** —— 那是替用户编造凭据，§5 硬约束 5）。

    ⚠️ 反向守卫：若有人把回填整段删掉（或改成「留空」，或改成「猜个真名」），本用例必红。
    """
    import sqlite3

    db_path = tmp_path / "backfill.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER NOT NULL,
                username VARCHAR(80) NOT NULL,
                password_hash VARCHAR(128) NOT NULL,
                created_at DATETIME NOT NULL,
                role VARCHAR(32) DEFAULT 'user',
                PRIMARY KEY (id)
            );
            CREATE TABLE llm_gateway_configs (
                id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name VARCHAR(128) NOT NULL,
                base_url VARCHAR(512) NOT NULL DEFAULT 'http://127.0.0.1:3000',
                access_token TEXT NOT NULL DEFAULT '',
                local_db_path VARCHAR(512) NOT NULL DEFAULT '',
                default_model VARCHAR(128) NOT NULL DEFAULT '',
                models_json TEXT NOT NULL DEFAULT '[]',
                enabled BOOLEAN NOT NULL DEFAULT 1,
                healthy BOOLEAN NOT NULL DEFAULT 0,
                last_health_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (id)
            );
            """
        )
        # 两条存量行：① 目录有模型但 default_model 空；② 目录与模型都空。
        conn.execute(
            "INSERT INTO llm_gateway_configs "
            "(id, user_id, name, default_model, models_json, created_at, updated_at) "
            "VALUES (1, 1, 'has-catalog', '', '[\"deepseek-v4-flash\", \"other\"]', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO llm_gateway_configs "
            "(id, user_id, name, default_model, models_json, created_at, updated_at) "
            "VALUES (2, 1, 'no-catalog', '   ', '[]', '2026-01-01', '2026-01-01')"
        )
        # ③ 已填好的行不许被覆盖（回填只针对空值）
        conn.execute(
            "INSERT INTO llm_gateway_configs "
            "(id, user_id, name, default_model, models_json, created_at, updated_at) "
            "VALUES (3, 1, 'filled', 'KEEP-ME', '[\"whatever\"]', '2026-01-01', '2026-01-01')"
        )
        # 本迁移还要 ALTER `model_configs`（D5 加 provider_key）⇒ 夹具须有它。
        # 走项目公共助手（缺表清单集中一处，DEF-082 的修法）。
        create_later_migration_tables(conn)
        conn.commit()
    finally:
        conn.close()

    # ⚠️ 先 `stamp` 到本迁移的**父 revision**（`c4d5e6f7a8b9`），再 `upgrade` 到**本迁移**
    # （`d5e6f7a8b9c0`，不是 `head`）：
    # 夹具只建了本迁移要碰的表，从不跑前面的迁移链（同项目既有迁移用例的做法）；
    # 少了 stamp 这一步，alembic 会从**首迁移**开跑 ⇒ `table users already exists`。
    #
    # ⚠️ 2026-10-02 修：此处原为 `upgrade head`。v2.7（`e5f6a7b8c9d0`）与 v2.8
    # （`f6a7b8c9d0e1`）落地后，`head` 已前移到 `b8c9d0e1f2a3`，而 **v2.8 明确 drop 了
    # `llm_gateway_configs.default_model`**（死字段下线，设计 17 §13.2.10）⇒ 跑到 head 时本列
    # 已不存在，`SELECT default_model` 直接 `OperationalError`。
    # 本用例要钉的是**本迁移自己的回填行为**，故只升到本迁移即止 —— 与「改到 head 后断言随
    # 下游迁移漂移」解耦（同 test_migration_c2d3e4f5a6b7_renames_cleanup_job 的定 revision 做法）。
    run_alembic(db_path, "stamp", "c4d5e6f7a8b9")
    run_alembic(db_path, "upgrade", "d5e6f7a8b9c0")

    conn = sqlite3.connect(db_path)
    try:
        rows = dict(conn.execute("SELECT id, default_model FROM llm_gateway_configs").fetchall())
    finally:
        conn.close()

    # ① 目录有模型 ⇒ 取第一个
    assert rows[1] == "deepseek-v4-flash", "目录里有模型时应取第一个可用模型名"
    # ② 目录也空 ⇒ 占位文案（非空、且不是编造的真实模型名）
    assert rows[2].strip(), "空 default_model 必须被回填，不得留空"
    assert rows[2] != "deepseek-v4-flash", "目录为空时不得凭空猜一个真实模型名"
    assert "补填" in rows[2], f"占位文案需让用户看出要补（实测得到 {rows[2]!r}）"
    # ③ 已填值零改动
    assert rows[3] == "KEEP-ME", "已有 (非空) default_model 不得被覆盖"


PARENT_V20 = "d5e6f7a8b9c0"

# ⚠️ 本组用例要钉的是 **v2.0 迁移（`f8a9b0c1d2e3`）自己**的行为，故 `upgrade` 以它为终点，
# **不是 `head`**（2026-10-02 修）。
# 背景：v2.0 落地时它是当时的 head，`upgrade head` 与 `upgrade f8a9b0c1d2e3` 等价；
# 之后 v2.3~v2.10 链（`b2c3d4e5f6a7` → `b8c9d0e1f2a3`）续上来，`head` 前移，
# 而这条链会碰 `users` 等**夹具根本没建的表**（`create_later_migration_tables` 只补
# v2.0 及之前用到的）⇒ `upgrade head` 直接 `NoSuchTableError: users`，
# 后面的断言全成空摆。钉住 revision 即与下游链的解耦（同
# `test_migration_c2d3e4f5a6b7_renames_cleanup_job` 的定点 upgrade 做法）。
V20_REV = "f8a9b0c1d2e3"


def _seed(db_path):
    """最小夹具：本迁移要碰的三张表 + 两条存量行。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE llm_gateway_configs (
                id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name VARCHAR(128) NOT NULL,
                provider_key VARCHAR(64) NOT NULL DEFAULT '',
                platform VARCHAR(32) NOT NULL DEFAULT 'xhs',
                base_url VARCHAR(512) NOT NULL DEFAULT 'http://127.0.0.1:3000',
                access_token TEXT NOT NULL DEFAULT '',
                local_db_path VARCHAR(512) NOT NULL DEFAULT '',
                default_model VARCHAR(128) NOT NULL DEFAULT '',
                models_json TEXT NOT NULL DEFAULT '[]',
                enabled BOOLEAN NOT NULL DEFAULT 1,
                healthy BOOLEAN NOT NULL DEFAULT 0,
                last_health_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (id)
            );
            """
        )
        # 一条服务商：base_url 带**尾斜杠**（验归一化）
        conn.execute(
            "INSERT INTO llm_gateway_configs "
            "(id, user_id, name, provider_key, platform, base_url, default_model, created_at, updated_at) "
            "VALUES (1, 1, '匹配得上', 'PROV-MATCH', 'xhs', 'https://api.deepseek.com/v1/', 'm', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO llm_gateway_configs "
            "(id, user_id, name, provider_key, platform, base_url, default_model, created_at, updated_at) "
            "VALUES (2, 1, '地址不匹配', 'PROV-OTHER', 'xhs', 'https://api.other.com/v1', 'm', '2026-01-01', '2026-01-01')"
        )
        # 本迁移还要 ALTER / DROP `model_configs` ⇒ 用项目公共助手补齐缺表
        # （缺表清单集中一处，DEF-082 的修法：不从别的用例拷样板）。
        create_later_migration_tables(conn)
        conn.commit()

        # `model_configs` 的三条存量行（判「匹配才建映射」）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(model_configs)")}
        assert "is_default" in cols and "base_url" in cols, f"model_configs 列不全：{sorted(cols)}"
        for row in (
            # ① is_default=1 且 base_url（归一化）匹配上 ⇒ 应建映射
            (1, 1, "https://api.deepseek.com/v1", "text", 1),
            # ② is_default=1 但地址谁都不匹配 ⇒ 不建
            (2, 1, "https://api.nowhere.com/v1", "text", 1),
            # ③ is_default=0 ⇒ 不建（只迁默认行）
            (3, 1, "https://api.deepseek.com/v1", "text", 0),
        ):
            conn.execute(
                "INSERT INTO model_configs "
                "(id, user_id, name, model_type, provider, model_name, base_url, is_default) "
                "VALUES (?, ?, ?, ?, 'openai-compatible', 'demo', ?, ?)",
                (row[0], row[1], f"cfg-{row[0]}", row[3], row[2], row[4]),
            )
        conn.commit()
    finally:
        conn.close()



# ─────────────── v2.0 业务点映射迁移（f8a9b0c1d2e3，M4）───────────────

def test_v20_business_provider_migration(tmp_path):
    db_path = tmp_path / "v20.db"
    _seed(db_path)

    # ⚠️ 先 stamp 到父 revision 再定点 upgrade 到 **v2.0 迁移本身**（`V20_REV`，不是 `head`）：
    # 夹具只建了本迁移要碰的表，从不跑前面的迁移链（同项目既有迁移用例做法）。
    # 不用 head 的原因见 `V20_REV` 定义处（后续 v2.3~v2.10 链会碰夹具没有的表）。
    run_alembic(db_path, "stamp", PARENT_V20)
    run_alembic(db_path, "upgrade", V20_REV)

    conn = sqlite3.connect(db_path)
    try:
        # ① 新表 + 唯一索引
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "llm_business_providers" in tables, "映射表未建"
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "uq_llm_business_providers_user_platform_business_key" in indexes, "三段唯一索引未建"

        # ② model_type 加列 + 存量回填 text
        rows = dict(conn.execute("SELECT id, model_type FROM llm_gateway_configs").fetchall())
        assert rows[1] == "text", f"存量服务商 model_type 应回填 text，实测 {rows[1]!r}"
        assert rows[2] == "text"

        # ③ 只有「归一化地址匹配 + is_default=1」那条建了映射，且是文本类
        mappings = list(
            conn.execute(
                "SELECT user_id, platform, business_key, provider_key FROM llm_business_providers"
            )
        )
        # 迁移把匹配上的服务商铺到**该平台的全部文本类业务点**（5 个，
        # 见 business_provider_keys 的文本类常量）—— 图像类一律不建（§13.2.5：
        # 存量服务商 model_type 回填 text，不敢替用户猜图像能力）。
        assert len(mappings) == 5, f"应建 5 条文本类映射，实测 {len(mappings)} 条：{mappings}"
        keys = {m[2] for m in mappings}
        assert keys == {"generate_note", "rewrite_note", "generate_title", "generate_tags", "polish_text"}, (
            f"映射的业务点集合不对：{sorted(keys)}"
        )
        uid, platform, business_key, provider_key = mappings[0]
        assert provider_key == "PROV-MATCH", f"应指向地址匹配的服务商，实测 {provider_key!r}"
        assert platform == "xhs"
        # 归一化口径：尾部斜杠差异不影响匹配（本用例的服务商 base_url 带尾斜杠）
        assert business_key, "业务点 key 不得为空"

        # ④ model_configs 表已 drop
        assert "model_configs" not in tables, "model_configs 应随本迁移退场"
    finally:
        conn.close()

    # ⑤ 幂等：stamp 回父 revision 再 upgrade ⇒ 零变化
    run_alembic(db_path, "stamp", PARENT_V20)
    run_alembic(db_path, "upgrade", V20_REV)
    conn = sqlite3.connect(db_path)
    try:
        after = list(
            conn.execute("SELECT user_id, platform, business_key, provider_key FROM llm_business_providers")
        )
        assert len(after) == 5, f"重跑不得产生重复映射，实测 {len(after)} 条"
    finally:
        conn.close()


def test_v20_downgrade_recreates_empty_table(tmp_path):
    """downgrade 只把 `model_configs` 表**重建为空表** —— 设计取舍，不是 bug。

    §13.2.5：drop 前已把能迁的配置迁到映射表；回退时**不回填**模型配置
    （那是「另一条真相源」，回填等于让两条链路同时生效）。
    """
    db_path = tmp_path / "v20_down.db"
    _seed(db_path)
    run_alembic(db_path, "stamp", PARENT_V20)
    run_alembic(db_path, "upgrade", V20_REV)
    run_alembic(db_path, "downgrade", PARENT_V20)

    conn = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "model_configs" in tables, "回退应重建 model_configs 表（否则改不回来）"
        count = conn.execute("SELECT COUNT(*) FROM model_configs").fetchone()[0]
        assert count == 0, f"回退后应为空表（设计取舍：不回填），实测 {count} 行"
    finally:
        conn.close()
