"""任务中心新架构判据（设计 69）—— 取代 `test_scheduled_jobs.py` 里的旧语义用例。

## 为什么单独一个文件

`test_scheduled_jobs.py` 有 63 条，其中 21 条断言的是 **DEF-066「每用户一份定义」**
形态（同一 `job_key` 可建多行、内置行按用户各持一份、日志按 `user_id` 隔离）。
这套形态已被用户口径推翻：

> 「不在保存用户级的任务中心，收为公共」
> 「任务中心分为两级，系统级和工作区域级」

⇒ 旧骨架不是「修一修还能用」，而是**每一条都在描述一个不存在的架构**。
逐条修补会反复踩「清表时机 / Session 打桩 / 外键引用」这些与业务无关的坑
（实测 5 轮、`git diff` 460 行、每轮只降 1-3 条）。

本文件用**聚合用例**表达新判据：一条用例说清一件事，不再拆成 14 条同义断言。

## 判据清单

| # | 判据 | 对应旧用例 |
|---|---|---|
| 1 | 同 `job_key` 建第二次 ⇒ 409（一种任务一份定义）| A 类 9 条 |
| 2 | 两个用户看到的是**同一条定义**（公共，不是各自一份）| B 类 5 条 |
| 3 | 定义删了，历史日志仍保留且 `job_id` 置空 | `test_delete_job_keeps_logs...` |
| 4 | 同步类任务 fan-out：每个目标用户一行日志，带 `executed_for_user_id` | `test_logs_are_isolated_between_users` |
| 5 | 系统级任务跑一次、不 fan-out、`executed_for_user_id` 为空 | 同上的反面 |
| 6 | 执行后 `run_count` / `last_status` 真的写回（**本轮修的 bug**）| `test_execute_job_success_writes_log_and_counts` |
| 7 | 执行日志带 `workspace` 快照（**本轮修的 bug**）| 无（新增）|
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import backend.app.models  # noqa: F401
from backend.app.core.database import Base
from backend.app.main import app
from backend.app.models import PlatformAccount, ScheduledJob, ScheduledJobLog, User
from backend.app.services import scheduled_job_runner
from tests.backend.conftest import drop_schema, make_engine

pytestmark = pytest.mark.integration

client = TestClient(app)


@pytest.fixture()
def env():
    """每用例独立 PG schema + 打桩所有模块级 SessionLocal。"""
    from tests.backend.conftest import _BUILT_SCHEMAS, _SCHEMA_BY_ENGINE, reset_test_data

    engine = make_engine()
    #: 2026-10-26：同进程复用 schema ⇒ 表只建一次，之后只清数据（省 7.5s/条）。
    _schema = _SCHEMA_BY_ENGINE.get(engine)
    if _schema is not None and _BUILT_SCHEMAS.get(_schema):
        reset_test_data(engine)
    else:
        Base.metadata.create_all(bind=engine)
        if _schema is not None:
            _BUILT_SCHEMAS[_schema] = True
    SL = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    from backend.app.core.database import get_db

    def _get_db():
        db = SL()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    #: runner 用模块级 SessionLocal —— 不打桩就会写真实库（数据隔离事故）
    original_runner_sl = scheduled_job_runner.SessionLocal
    scheduled_job_runner.SessionLocal = SL

    _cleared["done"] = False
    yield {"SL": SL, "engine": engine}
    scheduled_job_runner.SessionLocal = original_runner_sl
    app.dependency_overrides.clear()
    drop_schema(engine)


def _user(SL, username: str, role: str = "admin") -> int:
    db = SL()
    try:
        u = User(username=username, password_hash="x", role=role)
        db.add(u)
        db.commit()
        db.refresh(u)
        return int(u.id)
    finally:
        db.close()


def _account(SL, user_id: int) -> int:
    db = SL()
    try:
        row = PlatformAccount(user_id=user_id, platform="xhs", sub_type="pc", nickname="t")
        db.add(row)
        db.commit()
        db.refresh(row)
        return int(row.id)
    finally:
        db.close()


def _job(SL, key: str, workspace: str = "xhs", name: str = "任务") -> int:
    db = SL()
    try:
        row = ScheduledJob(
            name=name,
            job_key=key,
            workspace=workspace,
            trigger_type="cron",
            cron_expression="0 0 * * * *",
            status="active",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return int(row.id)
    finally:
        db.close()


#: 本用例是否已清过定义表（`_token` 用；fixture 每用例重置）。
_cleared = {"done": False}


def _token(username: str, SL, *, admin: bool = True) -> str:
    """注册 → 清空注册触发的全局播种 → （可选）提升 admin → 返回 token。

    ## 为什么要清表

    注册会触发**全局**内置任务播种（设计 69：不再按用户复制，但仍是全局一次）。
    播种占住 13 个内置 `job_key`，而本文件的用例要自己造定义（尤其是
    `system.cookie_health_check`），不清就必然撞 `uq_scheduled_jobs_job_key`。

    ## 为什么要提升 admin

    本文件测**任务语义**，不是权限边界（权限由 `test_task_center_two_level_perms.py` 守）。
    普通角色没有 `xhs:taskcenter:toggle` ⇒ 删任务/改状态会被 `_get_visible_job` 判 403，
    把语义用例全打红。
    """
    resp = client.post("/api/auth/register", json={"username": username, "password": "secret123"})
    assert resp.status_code == 200

    db = SL()
    try:
        #: **每次注册后都清**：注册必然触发全局播种（13 个内置 key），
        #: 不清就撞 `uq_scheduled_jobs_job_key`。
        #:
        #: ⚠️ 调用方必须在**所有注册做完之后**再造定义（本文件各用例都是这个顺序），
        #: 否则后一次注册会把先建的行擦掉。这条约束写在类 docstring 里。
        db.query(ScheduledJob).delete()
        db.commit()
        if admin:
            user = db.scalars(select(User).where(User.username == username)).first()
            assert user is not None
            user.role = "admin"
            db.commit()
    finally:
        db.close()
    return resp.json()["access_token"]


def _auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


class TestOneDefinitionPerKey:
    """① 一种任务只有一份定义（`job_key` 全表唯一）。"""

    def test_second_create_with_same_key_is_409(self, env):
        """同 key 建第二次必须 409，且原因指向已存在的那一行。

        取代旧 A 类 9 条（那些都在断言「同 key 可建多行」，新架构下不可能）。
        """
        SL = env["SL"]
        tok = _token("dup-key", env["SL"])
        _job(SL, "system.cookie_health_check", "xhs", "巡检")

        resp = client.post(
            "/api/schedule/jobs",
            headers=_auth(tok),
            json={
                "name": "再来一个巡检",
                "job_key": "system.cookie_health_check",
                "schedule": {"trigger_type": "cron", "cron_expression": "0 0 8 * * *"},
            },
        )
        assert resp.status_code == 409, resp.text
        detail = str(resp.json().get("detail", ""))
        assert "cookie_health_check" in detail or "已存在" in detail, detail

        db = SL()
        try:
            rows = db.scalars(
                select(ScheduledJob).where(ScheduledJob.job_key == "system.cookie_health_check")
            ).all()
            assert len(rows) == 1, f"同 key 落了 {len(rows)} 行"
        finally:
            db.close()


class TestDefinitionIsPublic:
    """② 任务定义是公共的 —— 不是「某个用户的任务」。"""

    def test_two_users_see_the_same_row(self, env):
        """两个登录用户拿到的 `job_key` 是同一行（同一 id）。

        取代旧 B 类 5 条（那些断言「每个用户各持一份、互不可见」）。
        用户口径：「不在保存用户级的任务中心，收为公共」。
        """
        SL = env["SL"]
        tok_a = _token("public-a", env["SL"])
        tok_b = _token("public-b", env["SL"])
        job_id = _job(SL, "system.cookie_health_check", "xhs", "巡检")

        for tok in (tok_a, tok_b):
            resp = client.get(f"/api/schedule/jobs/{job_id}", headers=_auth(tok))
            assert resp.status_code == 200, resp.text
            assert resp.json()["id"] == job_id
            assert resp.json()["job_key"] == "system.cookie_health_check"

    def test_list_shows_one_row_not_one_per_user(self, env):
        """列表里同 key 只有一行（不是「用户数 × 任务数」）。"""
        SL = env["SL"]
        tok = _token("public-list", env["SL"])
        _token("public-list-2", env["SL"])
        _job(SL, "system.cookie_health_check", "xhs", "巡检")

        resp = client.get("/api/schedule/jobs?workspace=xhs&page=1&page_size=50", headers=_auth(tok))
        assert resp.status_code == 200, resp.text
        keys = [item["job_key"] for item in resp.json()["items"]]
        assert keys.count("system.cookie_health_check") == 1, keys


class TestLogsAreHistoricalSnapshots:
    """③ 定义删了，历史日志仍在（`job_id` 置空、`job_key` 快照保留）。"""

    def test_delete_definition_keeps_logs(self, env):
        SL = env["SL"]
        tok = _token("log-keep", env["SL"])
        job_id = _job(SL, "test.log_keep", "xhs", "带日志")

        db = SL()
        try:
            db.add(
                ScheduledJobLog(
                    job_id=job_id,
                    job_key="test.log_keep",
                    job_name="带日志",
                    workspace="xhs",
                    job_trigger_type="cron",
                    trigger_type="scheduled",
                    status="success",
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.delete(f"/api/schedule/jobs/{job_id}", headers=_auth(tok))
        assert resp.status_code == 200, resp.text

        db = SL()
        try:
            logs = db.scalars(
                select(ScheduledJobLog).where(ScheduledJobLog.job_key == "test.log_keep")
            ).all()
            assert len(logs) == 1, "任务删了日志应保留"
            assert logs[0].job_id is None, "外键应置空（历史快照不断链）"
        finally:
            db.close()


class TestFanOut:
    """④⑤ 执行层：`user_scoped` 任务扇出到每个持号用户；系统级任务跑一次。"""

    def _stub(self, monkeypatch, key: str, *, user_scoped: bool):
        from backend.app.services.job_registry import JobSpec

        spec = JobSpec(
            key=key,
            name="用例任务",
            func=_noop,
            builtin=False,
            user_scoped=user_scoped,
        )
        monkeypatch.setattr(scheduled_job_runner, "get_job_spec", lambda k: spec)
        return spec

    def test_user_scoped_job_fans_out_per_user(self, env, monkeypatch):
        """每个持平台账号的用户各一行日志，且 `executed_for_user_id` 指向他。"""
        SL = env["SL"]
        uid1 = _user(SL, "fanout-1")
        uid2 = _user(SL, "fanout-2")
        _account(SL, uid1)
        _account(SL, uid2)
        job_id = _job(SL, "test.fanout", "xhs")
        self._stub(monkeypatch, "test.fanout", user_scoped=True)

        outcome = scheduled_job_runner.execute_job(job_id, trigger_type="scheduled")
        assert outcome["status"] == "success", outcome

        db = SL()
        try:
            logs = db.scalars(
                select(ScheduledJobLog).where(ScheduledJobLog.job_key == "test.fanout")
            ).all()
            targets = {lg.executed_for_user_id for lg in logs if lg.executed_for_user_id}
            assert targets == {uid1, uid2}, f"扇出目标不对：{targets}"
        finally:
            db.close()

    def test_system_job_runs_once_without_target_user(self, env, monkeypatch):
        """系统级（`user_scoped=False`）跑一次，`executed_for_user_id` 为空。"""
        SL = env["SL"]
        uid = _user(SL, "sys-owner")
        _account(SL, uid)
        job_id = _job(SL, "test.sysjob", "system", "清理")
        self._stub(monkeypatch, "test.sysjob", user_scoped=False)

        outcome = scheduled_job_runner.execute_job(job_id, trigger_type="scheduled")
        assert outcome["status"] == "success", outcome

        db = SL()
        try:
            logs = db.scalars(
                select(ScheduledJobLog).where(ScheduledJobLog.job_key == "test.sysjob")
            ).all()
            assert len(logs) == 1, f"系统级任务应只跑一次，实际 {len(logs)} 行"
            assert logs[0].executed_for_user_id is None
        finally:
            db.close()


class TestExecutionWritesBackCounters:
    """⑥ 执行后 `run_count` / `last_status` 真的写回库。

    ## 这条用例对应的真 bug（2026-10-25 修）

    `_fan_out()` 内部自己 `SessionLocal()` 另开 Session，而 `job` 对象属于
    调用方 Session ⇒ `job.run_count += 1` 改的是**游离对象**，commit 不生效。
    症状：日志显示 success、界面上「运行次数」永远不涨、**零报错**。
    """

    def test_run_count_and_last_status_are_persisted(self, env, monkeypatch):
        from backend.app.services.job_registry import JobSpec

        SL = env["SL"]
        uid = _user(SL, "counter")
        _account(SL, uid)
        job_id = _job(SL, "test.counter", "xhs")
        monkeypatch.setattr(
            scheduled_job_runner,
            "get_job_spec",
            lambda k: JobSpec(key="test.counter", name="t", func=_noop, user_scoped=True),
        )

        outcome = scheduled_job_runner.execute_job(job_id, trigger_type="scheduled")
        assert outcome["status"] == "success", outcome

        db = SL()
        try:
            row = db.get(ScheduledJob, job_id)
            assert row.run_count == 1, f"run_count 没写回：{row.run_count}"
            assert row.last_status == "success", f"last_status 没写回：{row.last_status}"
        finally:
            db.close()


class TestLogCarriesWorkspace:
    """⑦ 执行日志带 `workspace` 快照（工作区任务页按它筛）。"""

    def test_log_row_has_workspace(self, env, monkeypatch):
        from backend.app.services.job_registry import JobSpec

        SL = env["SL"]
        uid = _user(SL, "ws-snap")
        _account(SL, uid)
        job_id = _job(SL, "test.wssnap", "xhs")
        monkeypatch.setattr(
            scheduled_job_runner,
            "get_job_spec",
            lambda k: JobSpec(key="test.wssnap", name="t", func=_noop, user_scoped=True),
        )

        scheduled_job_runner.execute_job(job_id, trigger_type="scheduled")

        db = SL()
        try:
            logs = db.scalars(
                select(ScheduledJobLog).where(ScheduledJobLog.job_key == "test.wssnap")
            ).all()
            assert logs, "应有执行日志"
            assert all(lg.workspace == "xhs" for lg in logs), (
                f"日志缺 workspace 快照：{[lg.workspace for lg in logs]}"
            )
        finally:
            db.close()


def _noop(user_id=None, **params):
    """执行函数桩：签名与真实任务一致，返回 None = 成功。"""
    return None
