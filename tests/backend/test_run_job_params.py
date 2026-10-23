"""「执行一次」参数覆盖的后端契约测试（设计 22 §5，2026-10-06）。

## 这批用例盯的四件事

1. **白名单是硬门禁**（不只是 UI 约定）—— `auto_task_id` / `workflow_id` 这类
   由代码拥有的参数**不许**经「执行一次」改，否则用户能越权跑别人的任务。
2. **参数不写回任务行** —— 覆盖只对**这一次**生效；下一次定时/手动仍按行配置。
   写回 = 用户点一次「执行一次」就把定时配置改了（他没这么想）。
3. **深校验生效** —— `retention_days` 越界等要 400（复用 `_apply_custom_params` 同一把尺子）。
4. **无 params ⇒ 行为不变** —— 老前端（不传 body）必须照样能执行。

⚠️ 这是**只读/记账**层面的判据：真正跑任务的是后台线程，本文件只看「参数收没收到、
校验过没过、行配置动没动」，不跑业务体。
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import get_db
from backend.app.main import app
from backend.app.models import ScheduledJob, User



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract

OWNER = {"username": "run-owner", "password": "secret123"}


def _auth(headers: dict) -> dict:
    return headers


def _override_database(tmp_path, api_client):
    """复用 `api_client` 已建好的测试库（不再自建 engine / 二次覆盖 `get_db`）。

    早先版本自己 `create_engine` + 覆盖 `get_db` ⇒ 与 `api_client` 的覆盖**打架**
    （后装的赢，另一个用例可能拿到错的库），是 `-n auto` 假红的根因。
    """
    from backend.app.core.database import SessionLocal as _ProdSessionLocal

    engine = _ProdSessionLocal.kw["bind"]
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _patch_service_sessions(monkeypatch, factory) -> None:
    """把服务层模块级 SessionLocal 打到测试库（conftest 那套的等价物）。"""
    import backend.app.services.scheduled_job_runner as runner

    monkeypatch.setattr(runner, "SessionLocal", factory, raising=False)


def _register(client, username: str) -> dict:
    resp = client.post("/api/auth/register", json={"username": username, "password": "secret123"})
    assert resp.status_code in (200, 201), resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _user_id(factory, username: str) -> int:
    db = factory()
    try:
        return db.scalar(select(User.id).where(User.username == username))
    finally:
        db.close()


def _seed_job(factory, *, user_id: int, job_key: str, params: dict | None = None) -> int:
    """建一条任务行（直接落库，绕开建任务的参数校验 —— 本文件只测 run 端点）。"""
    db = factory()
    try:
        job = ScheduledJob(
            user_id=user_id,
            name="测试任务",
            job_key=job_key,
            cron_expression="0 0 9 * * *",
            trigger_type="cron",
            misfire_policy="once",
            concurrent="forbid",
            status="active",
            params=params or {},
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id
    finally:
        db.close()


@pytest.fixture()
def env(tmp_path, monkeypatch, api_client):
    """每个用例独立的测试库 + 桩化后台执行（用 conftest 的 `api_client`）。

    ⚠️ 早先版本用**模块级** `TestClient(app)` + 模块级 `dependency_overrides` ——
    那是 conftest 明确警告的形态（全局 overrides 会泄漏给同 worker 的其它文件，
    实测在 `-n auto` 下偶发 1 条假红）。改用共享 fixture 后 override 生命周期
    由 conftest 管（用完必 pop）。
    """
    factory = _override_database(tmp_path, api_client)
    _patch_service_sessions(monkeypatch, factory)
    calls: list[dict] = []

    def _fake_run(job_id: int, *, trigger_type="manual", params_override=None, progress_probe=None):
        calls.append({"job_id": job_id, "trigger_type": trigger_type, "params_override": params_override})
        return 999

    import backend.app.api.schedule as schedule_api

    monkeypatch.setattr(schedule_api, "run_job_in_background", _fake_run)
    try:
        yield api_client, factory, calls
    finally:
        # 只摘自己放进去的 get_db（api_client 自己会摘它那个）。
        from backend.app.core.database import get_db

        app.dependency_overrides.pop(get_db, None)


class TestRunParamsWhitelist:
    def test_disallowed_key_is_400_and_never_runs(self, env):
        """表外参数（`auto_task_id` 由代码拥有）⇒ 400，且**一次都不执行**。"""
        client, factory, calls = env
        headers = _register(client, "wl-1")
        uid = _user_id(factory, "wl-1")
        job_id = _seed_job(factory, user_id=uid, job_key="system.note_data_sync", params={})
        resp = client.post(
            f"/api/schedule/jobs/{job_id}/run",
            headers=headers,
            json={"params": {"auto_task_id": 12345}},
        )
        assert resp.status_code == 400
        assert "JOB_PARAMS_NOT_EDITABLE" in str(resp.json()["detail"])
        assert calls == [], "白名单外参数必须被拦在执行之前"

    def test_editable_key_is_accepted(self, env):
        """白名单内参数（`retention_days`）⇒ 200 并把覆盖传下去。"""
        client, factory, calls = env
        headers = _register(client, "wl-2")
        uid = _user_id(factory, "wl-2")
        # 队列记录清理任务的可编辑参数就是 retention_days。
        job_id = _seed_job(
            factory, user_id=uid, job_key="system.queue_record_cleanup", params={"retention_days": 15}
        )
        resp = client.post(
            f"/api/schedule/jobs/{job_id}/run",
            headers=headers,
            json={"params": {"retention_days": 7}},
        )
        assert resp.status_code == 200, resp.text
        assert len(calls) == 1
        assert calls[0]["params_override"] == {"retention_days": 7}
        assert calls[0]["trigger_type"] == "manual"


class TestRunParamsValidation:
    def test_retention_days_out_of_range_is_400(self, env):
        """深校验生效：`retention_days` 越界 ⇒ 400（与建/改任务同一把尺子）。"""
        client, factory, calls = env
        headers = _register(client, "val-1")
        uid = _user_id(factory, "val-1")
        job_id = _seed_job(
            factory, user_id=uid, job_key="system.queue_record_cleanup", params={"retention_days": 15}
        )
        resp = client.post(
            f"/api/schedule/jobs/{job_id}/run",
            headers=headers,
            json={"params": {"retention_days": 999999}},
        )
        assert resp.status_code == 400
        assert calls == []


class TestRunParamsDoNotPersist:
    def test_override_does_not_write_back_to_job(self, env):
        """⚠️ 关键语义：覆盖参数**不写回**任务行（下一次仍按行配置）。"""
        client, factory, calls = env
        headers = _register(client, "persist-1")
        uid = _user_id(factory, "persist-1")
        job_id = _seed_job(
            factory, user_id=uid, job_key="system.queue_record_cleanup", params={"retention_days": 15}
        )
        resp = client.post(
            f"/api/schedule/jobs/{job_id}/run",
            headers=headers,
            json={"params": {"retention_days": 3}},
        )
        assert resp.status_code == 200
        # 行配置必须还是 15（不是 3）。
        db = factory()
        try:
            job = db.get(ScheduledJob, job_id)
            assert job.params == {"retention_days": 15}
        finally:
            db.close()

    def test_no_body_still_runs(self, env):
        """无 params ⇒ 行为与改造前一致（老前端 / 无参数任务）。"""
        client, factory, calls = env
        headers = _register(client, "nobody-1")
        uid = _user_id(factory, "nobody-1")
        job_id = _seed_job(factory, user_id=uid, job_key="system.note_data_sync", params={})
        resp = client.post(f"/api/schedule/jobs/{job_id}/run", headers=headers)
        assert resp.status_code == 200
        assert calls[0]["params_override"] is None


class TestRunOwnership:
    def test_other_users_job_is_404(self, env):
        """越权执行别人的任务 ⇒ 404（归属门禁只有一份）。"""
        client, factory, calls = env
        owner = _register(client, "own-1")
        intruder = _register(client, "own-2")
        uid = _user_id(factory, "own-1")
        job_id = _seed_job(factory, user_id=uid, job_key="system.note_data_sync", params={})
        resp = client.post(
            f"/api/schedule/jobs/{job_id}/run",
            headers=intruder,
            json={"params": {}},
        )
        assert resp.status_code == 404
        assert calls == []
