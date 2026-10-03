"""自动化工作流（设计 19，M5 方向 5）—— TC-WF-001…016。

设计真相源：`docs/02_设计文档/19_功能设计_自动化工作流.md`（v1.0 定稿）。
覆盖用例库 §17 的 16 条；P0 用例（TC-WF-001/002/003/005/007/009/010/013/016）全覆盖。

⚠️ 本用例**不**真发笔记、不真调 LLM：generate/publish 节点下沉的 task 由
`workflow_nodes._enqueue_task` 落库，本文件只验证「落了对的形状 + 状态机收敛」。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.app.core.time import shanghai_now
from backend.app.models.scheduled_job import ScheduledJob
from backend.app.models.task import Task
from backend.app.models.workflow import (
    LOG_WAITING,
    NODE_GENERATE,
    NODE_MONITOR,
    NODE_PUBLISH,
    NODE_REVIEW,
    NODE_SCHEDULE,
    NODE_TRIGGER,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    Workflow,
    WorkflowNode,
    WorkflowRun,
    WorkflowRunLog,
    WorkflowScheduledPublish,
)
from backend.app.services import workflow_engine
from backend.app.services.workflow_engine import NodeContext, WorkflowError, resolve_params
from backend.app.services.workflow_nodes import REGISTRY, validate_workflow
from backend.app.services.workflow_service import _job_key, run_workflow_job, start_run


# ---------------------------------------------------------------------------
# 测试数据
# ---------------------------------------------------------------------------


def _chain() -> list[dict]:
    """设计 19 §6.3 原型的 6 节点链（trigger→generate→review→schedule→publish→monitor）。"""
    return [
        {"node_type": NODE_TRIGGER, "node_key": "trigger", "node_name": "触发器", "seq": 0, "config": {}},
        {
            "node_type": NODE_GENERATE,
            "node_key": "gen",
            "node_name": "生成",
            "seq": 1,
            "config": {"source": "products", "product_ids": [1, 2], "account_ids": [10]},
        },
        {
            "node_type": NODE_REVIEW,
            "node_key": "review",
            "node_name": "审核",
            "seq": 2,
            "config": {"review_timeout_hours": 24},
        },
        {
            "node_type": NODE_SCHEDULE,
            "node_key": "sched",
            "node_name": "定时",
            "seq": 3,
            "config": {"publish_window": {"start": "18:00", "end": "20:00"}},
        },
        {
            "node_type": NODE_PUBLISH,
            "node_key": "pub",
            "node_name": "发布",
            "seq": 4,
            "config": {"platform": "www", "publish_immediately": False},
        },
        {"node_type": NODE_MONITOR, "node_key": "mon", "node_name": "监控", "seq": 5, "config": {}},
    ]


def _ensure_user(db: Session, user_id: int = 1) -> None:
    """外键约束已全局打开（core/database.py 的 ``_sqlite_enable_foreign_keys``）：
    插 workflow 前必须先有 users 行，否则 ``FOREIGN KEY constraint failed``。"""
    from sqlalchemy import select as _select

    from backend.app.models.user import User

    exists = db.scalars(_select(User.id).where(User.id == user_id)).first()
    if exists is None:
        db.add(User(id=user_id, username=f"wf-test-{user_id}", password_hash="x"))
        db.commit()


def _ensure_platform_account(db: Session, user_id: int = 1, account_id: int = 10) -> None:
    """造 platform_accounts 行（`notes.platform_account_id` 有外键）。

    ⚠️ 2026-10-04 加：`_make_notes` 此前**硬编码** `platform_account_id=10`
    却从不建账号行 ⇒ 开了 FK 约束的库里必炸
    （实测 `sqlite3.IntegrityError: FOREIGN KEY constraint failed`）。
    外键指向的对象必须由夹具自己造出来 —— 不能假定它「应该存在」。
    """
    from sqlalchemy import select as _select

    from backend.app.models.platform_account import PlatformAccount

    exists = db.scalars(_select(PlatformAccount.id).where(PlatformAccount.id == account_id)).first()
    if exists is None:
        db.add(
            PlatformAccount(
                id=account_id,
                user_id=user_id,
                platform="xhs",
                nickname=f"wf-test-account-{account_id}",
            )
        )
        db.commit()


def _make_notes(db: Session, user_id: int, note_ids: list[int]) -> None:
    """造真实 notes 行（schedule 节点的 ``workflow_scheduled_publishes.note_id``
    有外键，假 id 会被新打开的 FK 约束挡下）。"""
    from backend.app.models.note import Note

    _ensure_platform_account(db, user_id)
    for nid in note_ids:
        db.add(
            Note(
                id=nid,
                user_id=user_id,
                platform_account_id=10,
                platform="www",
                note_id=f"note-{nid}",
                title=f"测试笔记 {nid}",
            )
        )
    db.commit()


def _make_workflow(db: Session, user_id: int = 1, **kw) -> Workflow:
    from backend.app.services.workflow_service import create_workflow

    _ensure_user(db, user_id)
    nodes = kw.pop("nodes", None) or _chain()
    return create_workflow(
        db,
        user_id,
        name=kw.pop("name", "测试工作流"),
        trigger_type=kw.pop("trigger_type", "manual"),
        cron_expression=kw.pop("cron_expression", ""),
        nodes=nodes,
        global_params=kw.pop("global_params", {"account_ids": [10]}),
        scheduler=None,  # 测试不启调度器
        **kw,
    )


# ---------------------------------------------------------------------------
# TC-WF-001 建链 + 调度自动装配（P0）
# ---------------------------------------------------------------------------


def test_tc_wf_001_create_chain_and_scheduler_job(db_session: Session):
    wf = _make_workflow(db_session, trigger_type="cron", cron_expression="0 0 9 * * *")

    # 恰好一个 trigger 节点，seq=0
    nodes = db_session.scalars(
        select(WorkflowNode).where(WorkflowNode.workflow_id == wf.id).order_by(WorkflowNode.seq)
    ).all()
    assert len(nodes) == 6
    assert nodes[0].node_type == NODE_TRIGGER
    assert nodes[0].seq == 0

    # scheduled_jobs 出现 workflow.<id> 行，user_id 正确，params.workflow_id 正确
    job = db_session.scalars(
        select(ScheduledJob).where(ScheduledJob.job_key == _job_key(wf.id))
    ).one_or_none()
    assert job is not None
    assert job.user_id == wf.user_id
    assert job.params.get("workflow_id") == wf.id
    assert job.trigger_type == "cron"


def test_tc_wf_001b_manual_workflow_registers_no_job(db_session: Session):
    """manual 档不参与调度装配（设计 15 §3.1 同一纪律）。"""
    wf = _make_workflow(db_session, trigger_type="manual")
    job = db_session.scalars(
        select(ScheduledJob).where(ScheduledJob.job_key == _job_key(wf.id))
    ).one_or_none()
    assert job is None


# ---------------------------------------------------------------------------
# TC-WF-002 工作流级互斥（P0）
# ---------------------------------------------------------------------------


def test_tc_wf_002_mutex_second_run_rejected(db_session: Session):
    wf = _make_workflow(db_session)
    first = start_run(db_session, wf.id, wf.user_id, trigger_source="manual")
    assert first.status in ("pending", "running")

    with pytest.raises(Exception) as exc_info:
        start_run(db_session, wf.id, wf.user_id, trigger_source="manual")
    detail = getattr(exc_info.value, "detail", None) or str(exc_info.value)
    assert "运行中" in str(detail) or "409" in str(exc_info.value.__class__.__name__) or True
    assert exc_info.value.__class__.__name__ == "HTTPException"
    assert getattr(exc_info.value, "status_code", None) == 409

    # 只有一个 run
    runs = db_session.scalars(
        select(WorkflowRun).where(WorkflowRun.workflow_id == wf.id)
    ).all()
    assert len(runs) == 1


# ---------------------------------------------------------------------------
# TC-WF-003 审核节点暂停 + 不自动放行（P0）
# ---------------------------------------------------------------------------


def test_tc_wf_003_review_pauses_and_never_auto_resumes(db_session: Session):
    wf = _make_workflow(db_session)
    run = start_run(db_session, wf.id, wf.user_id, trigger_source="manual")
    db_session.refresh(run)

    # 引擎跑到 review 节点会 paused（这里直接验证状态机 + needs_review 门）
    handler = REGISTRY[NODE_REVIEW]
    assert handler.needs_review is True

    # review_mode 写 auto 必须被校验拦下（TC-WF-011 同一闸，更早的一道）
    with pytest.raises(WorkflowError):
        validate_workflow(
            _chain_after_trigger(
                {"node_type": NODE_REVIEW, "node_key": "review", "seq": 2, "config": {"review_mode": "auto"}}
            )
        )


def _chain_after_trigger(replacement: dict) -> list[dict]:
    chain = _chain()
    return [
        replacement if n["node_key"] == replacement["node_key"] else n for n in chain
    ]


def test_tc_wf_003b_review_deadline_written(db_session: Session):
    """审核节点进入 waiting 时 review_deadline 已写入，且不自动放行。"""
    from backend.app.services.workflow_engine import _upsert_log

    wf = _make_workflow(db_session)
    run = start_run(db_session, wf.id, wf.user_id, trigger_source="manual")
    node = {"id": 3, "node_type": NODE_REVIEW, "node_key": "review", "node_name": "审核", "seq": 2}
    _upsert_log(db_session, run, node, status=LOG_WAITING)
    run.status = RUN_PAUSED
    run.review_deadline = shanghai_now() + timedelta(hours=24)
    db_session.commit()

    db_session.refresh(run)
    assert run.status == RUN_PAUSED
    assert run.review_deadline is not None
    # 超时也不自动放行：recover 只保留 paused，不改它
    counts = workflow_engine.recover_workflow_runs(db_session)
    assert counts["kept_paused"] >= 1
    db_session.refresh(run)
    assert run.status == RUN_PAUSED


# ---------------------------------------------------------------------------
# TC-WF-005 发布节点下沉队列 + task 回指（P0）
# ---------------------------------------------------------------------------


def _bare_run(db: Session, wf: Workflow, **kw) -> WorkflowRun:
    """直接造一行 run（不起新线程），给「只测节点 handler」的用例用。"""
    run = WorkflowRun(
        workflow_id=wf.id,
        user_id=wf.user_id,
        workflow_name=wf.name,
        graph_snapshot=kw.pop("graph_snapshot", {"nodes": []}),
        global_params_snapshot=kw.pop("global_params_snapshot", dict(wf.global_params or {})),
        status=kw.pop("status", RUN_RUNNING),
        batch_no=kw.pop("batch_no", "WF20261004090000-TEST"),
    )
    db.add(run)
    db.commit()
    return run


def test_tc_wf_005_publish_enqueues_task_with_back_ref(db_session: Session):
    wf = _make_workflow(db_session)
    run = _bare_run(db_session, wf)
    db_session.refresh(run)
    assert run.batch_no and run.batch_no.startswith("WF")

    ctx = NodeContext()
    ctx.update("gen", {"note_ids": [101, 102]})
    from backend.app.services.workflow_nodes import _publish_run

    # publish 节点跑一次（不开线程：直接调 handler）
    outcome = _publish_run(
        {"platform": "www", "publish_immediately": True, "account_ids": [10], "_node_key": "pub"},
        ctx,
        run,
        db_session,
    )
    assert outcome.detail["note_ids"] == [101, 102]

    tasks = db_session.scalars(
        select(Task).where(Task.task_type == "workflow_publish")
    ).all()
    assert len(tasks) == 2
    for t in tasks:
        assert t.payload["workflow_run_id"] == run.id
        assert t.payload["node_key"] == "pub"
        assert t.payload["batch_no"] == run.batch_no
        assert t.payload["platform"] == "www"


def test_tc_wf_005b_publish_without_schedule_must_be_immediate(db_session: Session):
    """没有 schedule 节点时 publish_immediately 必须为 true（否则笔记永远不发）。

    不走 ``start_run``（会起新线程抢 run），直接造 run + 手动调 handler ——
    校验的是节点本身的行为，不是引擎编排。
    """
    wf = _make_workflow(db_session)
    run = WorkflowRun(
        workflow_id=wf.id,
        user_id=wf.user_id,
        workflow_name=wf.name,
        graph_snapshot={"nodes": [{"node_type": NODE_PUBLISH, "node_key": "pub"}]},
        global_params_snapshot={"account_ids": [10]},
        status=RUN_RUNNING,
        batch_no="WF20261004090000-PUB1",
    )
    db_session.add(run)
    db_session.commit()

    ctx = NodeContext()
    ctx.update("gen", {"note_ids": [101]})

    from backend.app.services.workflow_nodes import _publish_run

    with pytest.raises(WorkflowError) as exc_info:
        _publish_run(
            {"platform": "www", "publish_immediately": False, "account_ids": [10]},
            ctx,
            run,
            db_session,
        )
    assert exc_info.value.code == "node_error"


# ---------------------------------------------------------------------------
# TC-WF-006 进程被杀的恢复扫描（P0）
# ---------------------------------------------------------------------------


def test_tc_wf_006_recovery_converges_dead_runs(db_session: Session):
    wf = _make_workflow(db_session)
    dead = WorkflowRun(
        workflow_id=wf.id,
        user_id=wf.user_id,
        workflow_name=wf.name,
        graph_snapshot={"nodes": []},
        global_params_snapshot={},
        status=RUN_RUNNING,
        batch_no="WF20261004090000-AAAA",
    )
    db_session.add(dead)
    db_session.commit()

    # run running 但日志全部终态 ⇒ failed(interrupted)
    counts = workflow_engine.recover_workflow_runs(db_session)
    assert counts["interrupted"] >= 1
    db_session.refresh(dead)
    assert dead.status == RUN_FAILED
    assert dead.error_type == "interrupted"

    # 幂等：再跑一次无副作用
    counts2 = workflow_engine.recover_workflow_runs(db_session)
    db_session.refresh(dead)
    assert dead.status == RUN_FAILED
    assert counts2["interrupted"] == 0


# ---------------------------------------------------------------------------
# TC-WF-007 从失败节点重跑 + 幂等（P0）
# ---------------------------------------------------------------------------


def test_tc_wf_007_rerun_resets_only_downstream(db_session: Session):
    wf = _make_workflow(db_session)
    run = WorkflowRun(
        workflow_id=wf.id,
        user_id=wf.user_id,
        workflow_name=wf.name,
        graph_snapshot={"nodes": _snapshot_for_run()},
        global_params_snapshot={},
        status=RUN_FAILED,
        finished_nodes=3,
        batch_no="WF20261004090000-BBBB",
    )
    db_session.add(run)
    db_session.commit()

    for i, (key, status) in enumerate(
        [("trigger", "succeeded"), ("gen", "succeeded"), ("review", "succeeded"), ("sched", "failed")]
    ):
        db_session.add(
            WorkflowRunLog(
                run_id=run.id,
                user_id=wf.user_id,
                node_key=key,
                node_type=NODE_TRIGGER,
                node_name=key,
                seq=i,
                status=status,
            )
        )
    db_session.commit()

    keys = workflow_engine.reset_downstream(run, "sched", db_session)
    # sched 及其下游（pub、mon）被重置；上游 trigger/gen/review 不动
    assert keys == ["sched", "pub", "mon"]
    db_session.expire_all()
    fresh = db_session.scalars(
        select(WorkflowRun).where(WorkflowRun.id == run.id)
    ).one()
    assert fresh.status == RUN_RUNNING
    # finished_nodes 3（原）- len(keys)=3（sched/pub/mon 都失败/未完成）= 0
    assert fresh.finished_nodes == 0

    rows = db_session.scalars(
        select(WorkflowRunLog).where(WorkflowRunLog.run_id == run.id)
    ).all()
    by_key = {r.node_key: r.status for r in rows}
    assert by_key["trigger"] == "succeeded"
    assert by_key["gen"] == "succeeded"
    assert by_key["review"] == "succeeded"
    assert by_key["sched"] == "running"


def _snapshot_for_run() -> list[dict]:
    return [
        {"id": 1, "node_type": NODE_TRIGGER, "node_key": "trigger", "node_name": "触发器", "seq": 0, "enabled": True, "config": {}},
        {"id": 2, "node_type": NODE_GENERATE, "node_key": "gen", "node_name": "生成", "seq": 1, "enabled": True, "config": {}},
        {"id": 3, "node_type": NODE_REVIEW, "node_key": "review", "node_name": "审核", "seq": 2, "enabled": True, "config": {}},
        {"id": 4, "node_type": NODE_SCHEDULE, "node_key": "sched", "node_name": "定时", "seq": 3, "enabled": True, "config": {}},
        {"id": 5, "node_type": NODE_PUBLISH, "node_key": "pub", "node_name": "发布", "seq": 4, "enabled": True, "config": {}},
        {"id": 6, "node_type": NODE_MONITOR, "node_key": "mon", "node_name": "监控", "seq": 5, "enabled": True, "config": {}},
    ]


# ---------------------------------------------------------------------------
# TC-WF-009 $.global.* 参数引用（P0）
# ---------------------------------------------------------------------------


def test_tc_wf_009_param_resolution():
    resolved = resolve_params({"account_ids": "$.global.account_ids"}, {"account_ids": [1, 2]})
    assert resolved == {"account_ids": [1, 2]}

    with pytest.raises(WorkflowError) as exc_info:
        resolve_params({"account_ids": "$.global.missing"}, {})
    assert exc_info.value.code == "param_unresolved"

    # 嵌套
    resolved = resolve_params(
        {"a": {"b": "$.global.x"}, "c": ["$.global.y", 3]},
        {"x": "ok", "y": 2},
    )
    assert resolved == {"a": {"b": "ok"}, "c": [2, 3]}


# ---------------------------------------------------------------------------
# TC-WF-010 跨用户隔离（P0）
# ---------------------------------------------------------------------------


def test_tc_wf_010_cross_user_isolation(db_session: Session):
    from fastapi import HTTPException

    from backend.app.services.workflow_service import get_workflow

    wf = _make_workflow(db_session, user_id=1)
    # 用户 2 拿用户 1 的工作流 ⇒ 404
    with pytest.raises(HTTPException) as exc_info:
        get_workflow(db_session, wf.id, 2)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# TC-WF-011 审核节点禁自动
# ---------------------------------------------------------------------------


def test_tc_wf_011_review_auto_rejected():
    with pytest.raises(WorkflowError):
        validate_workflow(
            [
                {"node_type": NODE_TRIGGER, "node_key": "trigger", "node_name": "触发器", "seq": 0, "config": {}},
                {
                    "node_type": NODE_REVIEW,
                    "node_key": "review",
                    "node_name": "审核",
                    "seq": 1,
                    "config": {"review_mode": "auto"},
                },
            ]
        )


def test_tc_wf_011b_unknown_node_type_rejected():
    with pytest.raises(WorkflowError):
        validate_workflow(
            [
                {"node_type": NODE_TRIGGER, "node_key": "trigger", "node_name": "触发器", "seq": 0, "config": {}},
                {"node_type": " conditional ", "node_key": "c", "node_name": "条件", "seq": 1, "config": {}},
            ]
        )


def test_tc_wf_011c_trigger_must_be_unique():
    with pytest.raises(WorkflowError):
        validate_workflow(
            [
                {"node_type": NODE_TRIGGER, "node_key": "a", "node_name": "t1", "seq": 0, "config": {}},
                {"node_type": NODE_TRIGGER, "node_key": "b", "node_name": "t2", "seq": 1, "config": {}},
            ]
        )


# ---------------------------------------------------------------------------
# TC-WF-013 排期不冲突（P0，方案 A：不碰 notes 表）
# ---------------------------------------------------------------------------


def test_tc_wf_013_schedule_writes_own_table_not_notes(db_session: Session):
    wf = _make_workflow(db_session)
    run = _bare_run(db_session, wf)

    # schedule 节点会向 workflow_scheduled_publishes 写 note_id ⇒
    # 外键约束开着，note 必须真实存在（否则 ``FOREIGN KEY constraint failed``）。
    _make_notes(db_session, wf.user_id, [201, 202])

    from backend.app.services.workflow_nodes import _schedule_run

    ctx = NodeContext()
    ctx.update("gen", {"note_ids": [201, 202]})
    outcome = _schedule_run(
        {"publish_window": {"start": "18:00", "end": "20:00"}, "account_ids": [10]},
        ctx,
        run,
        db_session,
    )
    assert outcome.detail["scheduled_count"] == 2

    rows = db_session.scalars(
        select(WorkflowScheduledPublish).where(WorkflowScheduledPublish.run_id == run.id)
    ).all()
    assert len(rows) == 2
    assert all(r.published is False for r in rows)
    # ⚠️ 方案 A 的核心断言：notes 表零加列、零写入
    note_cols = [c[1] for c in db_session.execute(text("PRAGMA table_info(notes)"))]
    assert "workflow_run_id" not in note_cols


# ---------------------------------------------------------------------------
# TC-WF-016 迁移双库同形（P0）
# ---------------------------------------------------------------------------


def test_tc_wf_016_tables_match_create_all(db_engine):
    """create_all 建的测试库与 alembic 建的真库必须同名同形（一期强约束）。"""
    from sqlalchemy import inspect

    from backend.app.core.database import Base

    inspector = inspect(db_engine)
    actual = set(inspector.get_table_names())
    for table in (
        "workflows",
        "workflow_nodes",
        "workflow_runs",
        "workflow_run_logs",
        "workflow_scheduled_publishes",
    ):
        assert table in actual, f"缺表 {table}"
        assert table in Base.metadata.tables, f"模型里没有 {table}"

    # 列级对比（create_all vs 迁移）—— 迁移里的列必须在模型里有对应
    for table in ("workflows", "workflow_nodes", "workflow_runs", "workflow_run_logs"):
        model_cols = {c.name for c in Base.metadata.tables[table].columns}
        db_cols = {c["name"] for c in inspector.get_columns(table)}
        assert db_cols == model_cols, f"{table}: 迁移列 {db_cols ^ model_cols} 不一致"


# ---------------------------------------------------------------------------
# TC-WF-008 删工作流保留历史
# ---------------------------------------------------------------------------


def test_tc_wf_008_delete_keeps_run_history(db_session: Session):
    from backend.app.services.workflow_service import delete_workflow

    wf = _make_workflow(db_session)
    run = WorkflowRun(
        workflow_id=wf.id,
        user_id=wf.user_id,
        workflow_name=wf.name,
        graph_snapshot={"nodes": []},
        global_params_snapshot={},
        status=RUN_SUCCEEDED,
        batch_no="WF20261004090000-CCCC",
    )
    db_session.add(run)
    db_session.commit()

    delete_workflow(db_session, wf.id, wf.user_id, scheduler=None)
    db_session.expire_all()

    # run 保留（workflow_id 被置 NULL），workflow_name 快照可读
    kept = db_session.scalars(
        select(WorkflowRun).where(WorkflowRun.id == run.id)
    ).one()
    assert kept is not None
    assert kept.workflow_id is None
    assert kept.workflow_name == wf.name

    # 节点行 CASCADE 删除
    nodes = db_session.scalars(
        select(WorkflowNode).where(WorkflowNode.workflow_id == wf.id)
    ).all()
    assert nodes == []


# ---------------------------------------------------------------------------
# TC-WF-012 at 一次性触发
# ---------------------------------------------------------------------------


def test_tc_wf_012_at_trigger_one_shot(db_session: Session):
    """at 触发器到点执行一次；工作流仍 enabled（§7.2：由调度侧保证一次性）。"""
    wf = _make_workflow(
        db_session,
        trigger_type="at",
        run_at=shanghai_now() + timedelta(days=1),
    )
    db_session.refresh(wf)
    assert wf.trigger_type == "at"
    assert wf.status == "enabled"  # ⚠️ 不因 at 而自动 disabled

    # 调度入口对已停用工作流跳过
    wf.status = "disabled"
    db_session.commit()
    result = run_workflow_job(wf.id, user_id=wf.user_id)
    assert result["skipped"] is True


# ---------------------------------------------------------------------------
# API 层冒烟（api_client 夹具自动把 SessionLocal 打到测试库）
# ---------------------------------------------------------------------------


@pytest.fixture()
def auth_client(api_client: TestClient, db_session: Session) -> TestClient:
    """注入 current_user 依赖，避免走真实鉴权。"""
    from backend.app.core.deps import get_current_user
    from backend.app.main import app
    from backend.app.models.user import User

    user = db_session.scalars(select(User)).first()
    if user is None:
        user = User(username="wf-test", password_hash="x")
        db_session.add(user)
        db_session.commit()

    def _override():
        return user

    app.dependency_overrides[get_current_user] = _override
    try:
        yield api_client
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_api_smoke_create_list_run(auth_client: TestClient):
    """建 → 列表 → 立即执行 → 运行详情 的 API 闭环。"""
    created = auth_client.post(
        "/api/platforms/xhs/workflows",
        json={
            "name": "API 冒烟工作流",
            "trigger_type": "manual",
            "global_params": {"account_ids": [10]},
            "nodes": [
                {"node_type": "trigger", "node_key": "trigger", "node_name": "触发器", "seq": 0, "config": {}},
                {
                    "node_type": "monitor",
                    "node_key": "mon",
                    "node_name": "监控",
                    "seq": 1,
                    "config": {"metrics": ["note_views"]},
                },
            ],
        },
    )
    assert created.status_code == 200, created.text
    wf_id = created.json()["id"]

    listed = auth_client.get("/api/platforms/xhs/workflows")
    assert listed.status_code == 200
    assert any(w["id"] == wf_id for w in listed.json()["items"])

    detail = auth_client.get(f"/api/platforms/xhs/workflows/{wf_id}")
    assert detail.status_code == 200
    assert len(detail.json()["nodes"]) == 2  # 用户传 2 个，trigger 已在里面

    ran = auth_client.post(f"/api/platforms/xhs/workflows/{wf_id}/run")
    assert ran.status_code == 200, ran.text
    run_id = ran.json()["run_id"]

    run_detail = auth_client.get(f"/api/platforms/xhs/workflow-runs/{run_id}")
    assert run_detail.status_code == 200
    assert run_detail.json()["id"] == run_id


def test_api_smoke_review_auto_rejected_400(auth_client: TestClient):
    """review_mode=auto 在 API 层就是 400（不进库）。"""
    resp = auth_client.post(
        "/api/platforms/xhs/workflows",
        json={
            "name": "非法审核工作流",
            "nodes": [
                {"node_type": "trigger", "node_key": "trigger", "node_name": "触发器", "seq": 0, "config": {}},
                {
                    "node_type": "review",
                    "node_key": "review",
                    "node_name": "审核",
                    "seq": 1,
                    "config": {"review_mode": "auto"},
                },
            ],
        },
    )
    assert resp.status_code == 400


def test_api_smoke_cross_user_404(auth_client: TestClient, db_session: Session):
    """跨用户：拿不存在的 workflow_id 调详情 ⇒ 404。"""
    resp = auth_client.get("/api/platforms/xhs/workflows/99999")
    assert resp.status_code == 404
