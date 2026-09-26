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

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.app.core.time import shanghai_now
from backend.app.models import ScheduledJob, ScheduledJobLog, User
from tests.backend.conftest import run_alembic

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
def test_registry_has_exactly_nine_entries():
    """注册表全量 9 项（8 系统 + 1 业务管线）；键集合钉死 —— 新增任务类型 = 发版。"""
    from backend.app.services.job_registry import job_registry

    registry = job_registry()
    assert len(registry) == 9
    assert set(registry) == {
        "system.due_publish",
        "system.monitoring_refresh",
        "system.cookie_health_check",
        "system.qianfan_health_check",
        "system.no_traffic_cleanup",
        "system.note_sync",
        "system.shop_goods_sync",
        "system.queue_record_cleanup",
        "auto_ops.keyword_pipeline",
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
        # 主人有 8 个内置行；入侵者先手动建一个自己的
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
        assert owner_stats["total"] == 8
        # 入侵者注册时也播种了 8 行 + 自己新建 1 行 = 9（只见自己的，不见主人的）
        assert intruder_stats["total"] == 9, "入侵者只应看到自己的行"
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
        # 注册表全量都在（含 8 个内置 + 1 个普通）
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
    """服务层：account_ids 非空 ⇒ 只跑指定账号；**行主隔离** ⇒ 不碰别人的账号。"""
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

    # ① account_ids 非空 ⇒ 只跑指定账号（自己的）
    sched.run_scheduled_note_syncs(user_id=user_id, account_ids=[mine[0]])
    assert calls == [mine[0]], f"只应跑指定账号，实跑 {calls}"

    # ② 空 = **本人全部账号**（DEF-066：不再是全库账号）
    calls.clear()
    sched.run_scheduled_note_syncs(user_id=user_id)
    assert sorted(calls) == sorted(mine), f"空应跑本人全部，实跑 {calls}"
    assert theirs_id not in calls, "行主隔离失效：跑别人的账号了"

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

        # ③ 失败 ⇒ 0
        def _boom(**kwargs):
            raise RuntimeError("炸了")

        _stub_spec(_boom)
        job_id = _make_job(SessionLocal(), "进度C")
        assert execute_job(job_id, trigger_type="scheduled")["status"] == "failed"
        assert _latest_progress() == 0, "失败 ⇒ 进度应为 0（不猜值）"
    finally:
        app.dependency_overrides.clear()


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
    `user_id IS NULL` 已废）⇒ 本用例钉 per-user 播种：每个用户 8 行、值逐字段
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
        assert len(first) == 16, "两个用户 × 8 个内置目标"
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
            }, "注册完打开任务中心就应有这 7 条"
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
        # DEF-066：内置行按用户播种 ⇒ 先有用户，sync 里的 ensure 才会铺出 8 行。
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
        # 活跃行 = 测试行 1 + 应用初始化数据播种的 8 行（R24 起全 active）；
        # 其中 `system.no_traffic_cleanup` 是 manual 档、**不参与调度装配** ⇒ 注册 8。
        assert stats["registered"] == 8
        assert len(ids) == 8
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


@pytest.mark.parametrize("mutate", ["groupfield", "groupsnapshot", "accountscope"])
def test_schedule_center_group_mutations_are_caught(mutate):
    """R22 反向变异：把分组字段/快照项**改回去**，行为脚本必须变红。

    DEF-066 追加 `accountscope`：账号多选退回「仅编辑态」⇒ 新建态没有账号多选
    （归属校验上线后手抄 id 只会 400）—— 与分组变异同一层防线。

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

    assert version == "f5a6b7c8d9e0", "迁移链前进到新 head"
    # ① 旧键行改名（不新增行 —— 一任务两行会让种子再补一份、清理跑两遍）
    assert jobs == [
        (1, "队列记录清理", "system.queue_record_cleanup"),
        (2, "账号笔记同步", "system.note_sync"),
    ]
    # ② 历史日志快照跟着改（任务中心按 job_key 筛选，劈成两半历史就查不到）
    assert logs == [
        (1, "队列记录清理", "system.queue_record_cleanup"),
        (2, "账号笔记同步", "system.note_sync"),
    ]
