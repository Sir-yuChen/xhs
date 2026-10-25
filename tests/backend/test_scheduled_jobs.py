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

import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.app.core.time import shanghai_now
from backend.app.models import ScheduledJob, ScheduledJobLog, User



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.integration

client = TestClient(app)

#: 仓库根：前端行为脚本 / 迁移文件都用它定位（不依赖 cwd）。
ROOT = Path(__file__).resolve().parents[2]




def _override_database(tmp_path):
    """指向**每用例独享 PG schema** 的库（2026-10-25 迁出 SQLite）。

    ## 为什么必须迁（不是洁癖）

    SQLite 已在 v1.1.0 退役（用户口径「完全放弃 sqlite」，连接层硬拒）。
    本文件是最后几个还在自建 `sqlite:///` 的地方，而它恰好又与设计 69 冲突：

    * `job_key` 全表唯一 ⇒ 用例要先清同 key 行；
    * `user_scoped` 任务 fan-out 时要**开第二个连接**查平台账号 ⇒
      SQLite 文件锁直接 `database is locked`（实测两轮批量改造都栽在这）。

    PG 的 schema 隔离天然支持多连接，这两个问题都消失。

    ⚠️ `tmp_path` 参数保留（既有调用点都传它），实际不使用。
    """
    global _ENGINE
    from backend.app.core.database import Base, get_db
    from tests.backend.conftest import make_engine

    #: 每个用例都调一次本函数 ⇒ 在这里重置，保证「用例独占一个 schema」。
    #: 用具名 fixture 会更干净，但这个文件有 40+ 处 `_override_database(tmp_path)`
    #: 调用点，逐个改成 fixture 注入是更大的改动面（且容易漏）。
    _ENGINE = make_engine(tmp_path)
    Base.metadata.create_all(bind=_ENGINE)
    engine = _ENGINE
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

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


#: 本用例的 engine（`_override_database` 建的那一个）—— `_db()` 必须复用它。
#: ⚠️ `make_engine()` 每次调用**新建一个 schema**（按 pid+计数分配），
#: 重复调用会得到一个空库，断言永远读到 0 行。这是迁 PG 时最容易踩的坑。
_ENGINE: Engine | None = None


def _db(tmp_path):
    """直接开一个指向**本用例那个 schema** 的 session（断言落库用）。

    ⚠️ 必须复用 `_ENGINE`：`make_engine()` 每次调用都新建 schema，
    以前 SQLite 用固定文件路径，重复 `create_engine` 连的是**同一个库**，
    迁到 PG schema 后这个前提没了 —— 不复用就会静默读到空库。
    """
    global _ENGINE
    if _ENGINE is None:
        from backend.app.core.database import Base
        from tests.backend.conftest import make_engine

        _ENGINE = make_engine(tmp_path)
        Base.metadata.create_all(bind=_ENGINE)
    return sessionmaker(autocommit=False, autoflush=False, bind=_ENGINE)()


def _ensure_fanout_target(SessionLocal) -> int:
    """给 fan-out 造一个目标（持平台账号的用户），返回其 user_id。

    ## 为什么执行类用例都必须先调它（设计 69 §4.2）

    `user_scoped=True` 的任务触发时遍历「持平台账号的用户」。测试库若是空的：

    * 目标集合为空 ⇒ 任务记 `skipped`（判据：没有目标不等于出错，
      这与「执行失败」是两回事，回执必须分开）；
    * 于是「执行成功」类断言全部变成 `assert 'skipped' == 'success'`。

    ⚠️ 幂等：已有账号就直接复用。用例体内多处调用安全。
    """
    from backend.app.models.platform_account import PlatformAccount
    from backend.app.models.user import User

    db = SessionLocal()
    try:
        existing = db.query(PlatformAccount).first()
        if existing is not None:
            return int(existing.user_id)
        owner = db.query(User).order_by(User.id.asc()).first()
        assert owner is not None, "先注册用户再调 _ensure_fanout_target"
        db.add(
            PlatformAccount(
                user_id=owner.id,
                platform="xhs",
                sub_type="pc",
                nickname="fanout-target",
                status="active",
            )
        )
        db.commit()
        return int(owner.id)
    finally:
        db.close()


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
def test_registry_has_exactly_sixteen_entries():
    """注册表全量 **16** 项（15 系统内置 + 1 业务管线 `auto_ops.keyword_pipeline`）；键集合钉死 —— 新增任务类型 = 发版。

    §44（2026-09-30）：「账号笔记同步」的混合双源拆成两个独立任务 ⇒ 新增
    `system.goods_note_sync`（**商品笔记**，ark 源）。它与 `system.note_sync`（普通笔记，
    creator 源）**各自成行**；⚠️ 与 `system.shop_goods_sync`（**店铺商品台账**，SKU/价格）
    不是一回事，两条键都必须在表里。

    ⚠️ 2026-10-04：M5/M6 新增 `workflow.`（工作流前缀，**动态**）与 `system.order_sync`
    （订单只读同步）⇒ 13 → 15。

    ⚠️ 2026-10-07：**工作流整条特性退场**（用户口径「把工作流相关逻辑表等完整移除，
    直接使用定时任务完成即可」）⇒ `workflow.` 这一项从注册表删除 ⇒ 16 → 15。

    ⚠️ 2026-10-06：新增 `system.note_data_sync`（**2026-10-13 用户口径改名为「数据中心
    笔记指标同步」**，商业+普通两类，千帆数据中心 butterfly/data 接口）⇒ 15 → 16。

    ⚠️ 2026-10-11：新增 `system.picture_space_cleanup`（图片空间清理未引用图片，
    用户口径「每周一执行一次」）⇒ 15 → 16。

    ⚠️ 2026-10-13：**移除** `system.llm_gateway_monitor`（用户口径「移除 LLM 网关预警任务，
    根据配置的规则**自动触发**」）⇒ 预警改由「台账写入即异步评估」
    （`services/llm_gateway_alert_trigger.py`），不再需要常驻调度任务 ⇒ 16 → 15。

    ⚠️ 2026-10-13（第二轮用户口径）：**移除** `system.note_metrics_collect` /
    `system.shop_metrics_collect`（指标快照子系统整块移除，后续专项）⇒ 15 → 13。
    ⚠️ 2026-10-24：新增 `system.package_staging_cleanup`（清理数据包暂存，每小时）
    —— 用户口径「后续部署在服务器，web 用户在浏览器操作」后，用户上传的数据包
    会堆在服务器磁盘上（`purge_expired()` 早已实现但**从未被调用**，实测积压 1432 个）⇒ 13 → 14。
    """
    from backend.app.services.job_registry import job_registry

    registry = job_registry()
    assert len(registry) == 14
    assert set(registry) == {
        "system.due_publish",
        "system.monitoring_refresh",
        "system.cookie_health_check",
        "system.qianfan_health_check",
        "system.note_sync",
        "system.shop_goods_sync",
        "system.goods_note_sync",
        "system.note_data_sync",
        "system.business_note_auto_publish",
        "system.queue_record_cleanup",
        "system.order_sync",
        "system.picture_space_cleanup",
        "system.package_staging_cleanup",
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


def test_create_job_rejects_duplicate_builtin_key(tmp_path):
    """设计 69：`job_key` 全表唯一 ⇒ 用内置 key 新建必须 400/409，不能建第二行。

    ## 这条用例反转了 R18 的旧断言（原句保留）

    原 `test_create_job_allows_builtin_key`：「R18：内置目标**也允许新建**
    （原一律 400 `JOB_BUILTIN_PROTECTED` 已废）」。

    被推翻的理由：一个 `job_key` 对应**一份定义**（一行 Cron / 一套参数），
    允许同 key 多行 ⇒ 调度器不知道该跑哪一行、日志不知道该记给哪份定义。
    XXL-Job 的 `xxl_job_info` 也是 job 唯一。用户口径「任务收为公共」正是这个意思。

    ⇒ 现在必须**明确拒绝**，且拒绝原因要能指向已存在的那一行。
    """
    _override_database(tmp_path)
    try:
        token = _register("job-builtin-dup")
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(token),
            json={
                "name": "再来一个巡检",
                "job_key": "system.cookie_health_check",
                "schedule": {"trigger_type": "cron", "cron_expression": "0 0 8 * * *"},
            },
        )
        assert response.status_code in (400, 409), response.text
        detail = str(response.json().get("detail", ""))
        assert "cookie_health_check" in detail or "已存在" in detail or "唯一" in detail, (
            f"拒绝原因没有指向冲突的 job_key：{detail}"
        )
    finally:
        app.dependency_overrides.clear()


def test_create_job_rejects_unregistered_key(tmp_path):
    """设计 69：**不允许用户创建任务** ⇒ 注册表外的 key 一律拒。

    原 `test_create_job_supports_same_target_multiple_times` 断言
    「同一目标可建多条」—— 那正是现在要禁止的形态。

    现在创建端点第一句就是 `get_job_spec(payload.job_key)`：
    不在注册表里的 key（`custom.*`）直接 400 `JOB_KEY_UNSUPPORTED`。
    用户口径「暂时不允许用户创建任务」在这里落地 —— 能建的只有系统已注册的任务。
    """
    _override_database(tmp_path)
    try:
        token = _register("job-custom-key")
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(token),
            json={
                "name": "我的巡检",
                "job_key": "custom.health_check",
                "schedule": {"trigger_type": "cron", "cron_expression": "0 0 8 * * *"},
            },
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "JOB_KEY_UNSUPPORTED"
    finally:
        app.dependency_overrides.clear()

def test_create_job_rejects_foreign_account_ids(tmp_path, monkeypatch):
    """建行也过 account_ids 归属校验（原只在 PUT 有 ⇒ 可建引用别人账号的任务行）。"""
    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        owner = _register("create-ids-owner")
        intruder = _register("create-ids-intruder")
        mine_id, _ = _make_pc_and_creator_accounts(SessionLocal, owner)
        _, theirs_id = _make_pc_and_creator_accounts(SessionLocal, intruder)

        # 别人的账号 ⇒ 400（跨租户）。⚠️ 2026-10-07：原先这里用
        # `system.no_traffic_cleanup` 当载体（「平台删除操作，走通就等于 A 能定时删
        # B 的笔记」）—— 该任务已从注册表移除（用户口径「无流量笔记清理任务不应在
        # 任务列表展示」）⇒ 换一个**仍然收 account_ids** 的平台写操作任务做载体。
        # 判据本身没变（建行也过归属校验），载体换了而已。
        response = client.post(
            "/api/schedule/jobs",
            headers=_auth(owner),
            json={
                "name": "越权同步",
                "job_key": "system.note_sync",
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
                "name": "本人同步",
                "job_key": "system.note_sync",
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

        _ensure_fanout_target(SessionLocal)
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
        _ensure_fanout_target(SessionLocal)
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
        # 主人有 13 个内置行（2026-10-07：`no_traffic_cleanup` 移除后 14→13）；
        # 入侵者先手动建一个自己的
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
        from backend.app.core.initial_data import load_initial_scheduled_jobs

        seed_n = len(load_initial_scheduled_jobs())
        assert owner_stats["total"] == seed_n
        # 入侵者注册时也播种了 seed_n 行 + 自己新建 1 行（只见自己的，不见主人的）
        assert intruder_stats["total"] == seed_n + 1, "入侵者只应看到自己的行"
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


def test_builtin_job_cannot_be_deleted_but_schedule_stays_editable(tmp_path, monkeypatch):
    """**内置任务不可删除**（2026-10-07 用户口径），调度属性与参数仍可改。

    用户口径原文：「定时任务新增表字段是否为内置任务，列表新增展示，**内容任务不允许删除**，
    前后端添加校验逻辑」+「**系统内置**任务的调用参数不可删除不可添加；**非系统内置**任务
    可删除、可添加调用参数」。

    ⚠️ 本条**推翻**了两代旧断言，原句按规矩保留在此（不静默删除）：

    * 原始（内置一律保护）：
          response = client.delete(f"/api/schedule/jobs/{job_id}", headers=_auth(token))
          assert response.status_code == 400
          assert response.json()["detail"].startswith("JOB_BUILTIN_PROTECTED")
    * 2026-09-24 一度**放开**（「所有任务都是内置的 ⇒ 一律可删」）：
          deleted = client.delete(...)
          assert deleted.status_code == 200
    现在回到「区分内置与否」，判据 = 注册表 `JobSpec.builtin`（行上的 `is_builtin` 由建行时写入）。
    """
    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        token = _register("job-builtin-guard")
        job_id = _seed_builtin(SessionLocal, token)

        # 内置任务的**参数白名单**仍生效：非 params_editable 的键一律 400
        response = client.put(
            f"/api/schedule/jobs/{job_id}",
            headers=_auth(token),
            json={"params": {"hacked": True}},
        )
        assert response.status_code == 400
        assert response.json()["detail"].startswith("JOB_PARAMS_NOT_EDITABLE")

        # 调度属性可调（换触发器）—— 「不可删」不等于「不可配」
        response = client.put(
            f"/api/schedule/jobs/{job_id}",
            headers=_auth(token),
            json={"schedule": {"trigger_type": "cron", "cron_expression": "0 0 */2 * * *"}},
        )
        assert response.status_code == 200
        assert response.json()["cron_expression"] == "0 0 */2 * * *"

        # 内置行**不可删除**
        deleted = client.delete(f"/api/schedule/jobs/{job_id}", headers=_auth(token))
        assert deleted.status_code == 400, deleted.text
        assert deleted.json()["detail"].startswith("JOB_BUILTIN_PROTECTED")
        # 行还在
        assert client.get(f"/api/schedule/jobs/{job_id}", headers=_auth(token)).status_code == 200
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


def test_non_builtin_job_params_are_user_owned(tmp_path, monkeypatch):
    """**非内置任务**的 params 由用户拥有：可自由增删键（2026-10-07 用户口径）。

    用户口径：「**非系统内置**任务可删除、可添加调用参数」—— 原先「一律 400
    `JOB_PARAMS_NOT_EDITABLE`」的判据**已收窄为只对内置任务生效**（原句保留：
        response = client.put(..., json={"params": {"auto_task_id": auto_task_id + 100}})
        assert response.status_code == 400
        assert response.json()["detail"].startswith("JOB_PARAMS_NOT_EDITABLE")
    ）。

    ⚠️ 但**深校验仍生效**：`auto_task_id` 不是本人的 ⇒ `params_validator` 抛
    「不存在或不属于当前用户」⇒ 仍 400（白名单放开不等于归属校验放开）。
    """
    SessionLocal = _override_database(tmp_path)
    _patch_service_sessions(monkeypatch, SessionLocal)
    try:
        token = _register("job-user-owned")
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

        # ① 自己加一个额外参数键 ⇒ 允许（非内置任务参数由用户拥有）
        response = client.put(
            f"/api/schedule/jobs/{job_id}",
            headers=_auth(token),
            json={"params": {"auto_task_id": auto_task_id, "my_extra": "hello"}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["params"].get("my_extra") == "hello"

        # ② 但**深校验**不能跟着放开：别人的 auto_task_id 仍 400
        response = client.put(
            f"/api/schedule/jobs/{job_id}",
            headers=_auth(token),
            json={"params": {"auto_task_id": auto_task_id + 100}},
        )
        assert response.status_code == 400
        assert "不属于当前用户" in response.json()["detail"]
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

        _ensure_fanout_target(SessionLocal)
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
        _ensure_fanout_target(SessionLocal)
        assert execute_job(job_id, trigger_type="scheduled")["status"] == "success"
        assert _latest_progress() == 100, "成功未上报 ⇒ 进度应为 100"

        # ② 成功且任务上报 42 ⇒ 尊重上报值（不覆盖为 100）
        _stub_spec(lambda **kwargs: {"ok": True, "progress": 42})
        job_id = _make_job(SessionLocal(), "进度B")
        _ensure_fanout_target(SessionLocal)
        assert execute_job(job_id, trigger_type="scheduled")["status"] == "success"
        assert _latest_progress() == 42, "任务上报 42 ⇒ 进度应为 42"

        # ②b 上报越界值 ⇒ 夹回 0~100（写库不得出现 150 / -3）
        _stub_spec(lambda **kwargs: {"ok": True, "progress": 150})
        job_id = _make_job(SessionLocal(), "进度B2")
        _ensure_fanout_target(SessionLocal)
        assert execute_job(job_id, trigger_type="scheduled")["status"] == "success"
        assert _latest_progress() == 100, "上报 150 ⇒ 应夹到 100"

        # ③ 失败 ⇒ **不清零**（2026-09-27 用户口径「任务执行失败后任务进度不要变，
        #   应展示执行的进度比例」）。本用例没给探针 ⇒ 执行期间从没写过 progress
        #   ⇒ 行上还是初始 0；**关键是不再被 `_mark_failed` 主动写 0**（保留语义）。
        def _boom(**kwargs):
            raise RuntimeError("炸了")

        _stub_spec(_boom)
        job_id = _make_job(SessionLocal(), "进度C")
        _ensure_fanout_target(SessionLocal)
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

        _ensure_fanout_target(SessionLocal)
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

        _ensure_fanout_target(SessionLocal)
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

        _ensure_fanout_target(SessionLocal)
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

        _ensure_fanout_target(SessionLocal)
        execute_job(job_id)
        _ensure_fanout_target(SessionLocal)
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


def test_ensure_builtin_jobs_seeds_from_application_init_data(tmp_path):
    """设计 69：内置任务是**公共定义**，与用户数无关。

    原断言「两个用户 × 13 个内置目标 = 26 行」（DEF-066 的「每用户各持一份」）。
    现在：**13 行，不管有几个用户**。
    """
    _override_database(tmp_path)
    try:
        _register("seed-a")
        _register("seed-b")

        from backend.app.models.scheduled_job import ScheduledJob  # noqa: PLC0415
        from backend.app.services import scheduler_service  # noqa: PLC0415

        with _db(tmp_path) as db:
            scheduler_service.ensure_builtin_jobs(db)
            total = db.query(ScheduledJob).count()
            keys = {row.job_key for row in db.query(ScheduledJob).all()}

        #: 种子权威 = 随包初始化数据（`config/initial_data/scheduled_jobs.json`），
        #: 不是代码里的兜底字典（那个只在文件缺该 key 时才用）。
        import json  # noqa: PLC0415

        seed = json.loads(
            (ROOT / "config" / "initial_data" / "scheduled_jobs.json").read_text(encoding="utf-8")
        )
        rows_seed = seed.get("jobs") or seed.get("items") or seed
        expected = {item["job_key"] for item in rows_seed}
        assert keys == expected, "内置任务集合与初始化数据不一致"
        assert total == len(expected), (
            f"内置任务应为公共 {len(expected)} 行（与用户数无关），实际 {total} 行"
        )
    finally:
        app.dependency_overrides.clear()


def test_register_does_not_duplicate_builtin_jobs(tmp_path):
    """设计 69：注册**不再播种**（播种是启动/首次访问的一次性动作）。

    原 `test_register_seeds_builtin_jobs_for_the_new_user` 断言
    「注册完打开任务中心就应有初始化数据里的全部内置任务」——
    在公共定义下这条已由「启动播种」承担，注册再播一次只会撞唯一键。
    """
    _override_database(tmp_path)
    try:
        from backend.app.models.scheduled_job import ScheduledJob  # noqa: PLC0415
        from backend.app.services import scheduler_service  # noqa: PLC0415

        with _db(tmp_path) as db:
            scheduler_service.ensure_builtin_jobs(db)
            before = db.query(ScheduledJob).count()

        _register("seed-after-startup")

        with _db(tmp_path) as db:
            after = db.query(ScheduledJob).count()

        assert after == before, f"注册又插了 {after - before} 行内置任务（应 0）"
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
        # 活跃行 = 测试行 1 + 应用初始化数据里 status=active 的内置行（paused 不装配）。
        # ⚠️ 2026-10-07 / 2026-10-13：`system.no_traffic_cleanup` 与
        # `system.llm_gateway_monitor` 先后移除 ⇒ 数字随种子变化。
        # 断言**从初始化数据推导**（写死数字会随种子增减漂移，历史上已多次踩到）。
        from backend.app.core.initial_data import load_initial_scheduled_jobs

        active_seeds = sum(
            1 for row in load_initial_scheduled_jobs() if row.get("status") == "active"
        )
        expected = active_seeds + 1  # +1 = 本用例插的活跃测试行
        assert stats["registered"] == expected
        assert len(ids) == expected
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




def test_schedule_endpoints_require_authentication(tmp_path):
    _override_database(tmp_path)
    try:
        assert client.get("/api/schedule/jobs").status_code == 401
        assert client.get("/api/schedule/logs").status_code == 401
        assert client.post("/api/schedule/cron/validate", json={"cron_expression": "0 0 9 * * *"}).status_code == 401
    finally:
        app.dependency_overrides.clear()






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














# ─────────────── v2.0 业务点映射迁移（f8a9b0c1d2e3，M4）───────────────





# --------------------------------------------------------------------------- #
# 参数默认值（2026-10-07 用户口径「调用参数不允许随意添加和删除，必须有默认值」）
# --------------------------------------------------------------------------- #
class TestParamDefaults:
    """注册表 `params_defaults` 的三条契约：键集覆盖、必有默认值、运行器兜底。"""

    def test_every_declared_param_key_has_a_default(self):
        """**每个参数键都必须有默认值** —— 新增参数漏默认值 ⇒ 守卫必红。

        这条是用户口径的机械化：没有它，「加了个参数但没想清楚缺省行为」只能靠人盯。
        """
        from backend.app.services.job_registry import job_registry

        missing: list[str] = []
        for key, spec in job_registry().items():
            # ⚠️ `params_required` 是**引用型必填**（如 `auto_task_id`）—— 它没有
            # 也不该有默认值（默认一个不存在的 id 只会让执行必然失败）。所以判据是
            # 「`params_editable` 的键**必须**有默认值」，必填键只要求被声明。
            for param_key in spec.params_editable:
                if param_key not in spec.params_defaults:
                    missing.append(f"{key}.{param_key}")
        assert not missing, f"以下可编辑参数缺默认值: {missing}"

    def test_defaults_do_not_name_undeclared_keys(self):
        """反向：`params_defaults` 里也不许有多余的键（两份真相源会打架）。"""
        from backend.app.services.job_registry import job_registry

        extra: list[str] = []
        for key, spec in job_registry().items():
            for param_key in spec.params_defaults:
                if param_key not in spec.params_editable:
                    extra.append(f"{key}.{param_key}")
        assert not extra, f"以下默认值对应的键不在 params_editable 里: {extra}"

    def test_account_scoped_tasks_default_to_all_own_accounts(self):
        """`account_ids` 的默认值恒为 `[]`（= 本人全部账号，既有口径）。"""
        from backend.app.services.job_registry import job_registry

        for key, spec in job_registry().items():
            if "account_ids" in spec.params_editable:
                assert spec.params_defaults.get("account_ids") == [], key

    def test_runner_fills_missing_params_from_defaults(self, tmp_path, monkeypatch):
        """**后端兜底**：存量行的 params 缺键时，运行器按默认值补齐再调执行函数。

        ⚠️ 这是「存量任务行早于某参数上线」的唯一安全网 —— 不补的话
        `spec.func(**params)` 会因缺参 `TypeError`，整轮任务失败。

        真跑 `execute_job`（不是复刻兜底逻辑 —— 复刻等于没测）。
        """
        from backend.app.services import job_registry, scheduled_job_runner
        from backend.app.services.scheduled_job_runner import execute_job

        SessionLocal = _override_database(tmp_path)
        _patch_service_sessions(monkeypatch, SessionLocal)

        captured: dict = {}

        def fake_func(**kwargs):
            captured.update(kwargs)
            return {"ok": True}

        # ⚠️ `JobSpec` 是 **frozen dataclass** ⇒ 不能原地改字段，用 `replace` 造替身。
        real_spec = job_registry.get_job_spec("system.business_note_auto_publish")
        spec = dataclasses.replace(real_spec, func=fake_func)
        monkeypatch.setattr(job_registry, "get_job_spec", lambda k: spec, raising=False)
        monkeypatch.setattr(scheduled_job_runner, "get_job_spec", lambda k: spec, raising=False)
        try:
            db = SessionLocal()
            #: 建行前先落一个真实用户（`scheduled_jobs.user_id` 有 FK，凭空 id 会撞约束）。
            db.add(User(id=1, username="defaults-owner", password_hash="x"))
            db.flush()
            # 存量行形态：params 只有四键，缺重试三件套
            job = ScheduledJob(
                user_id=1,
                name="存量商业笔记任务",
                job_key="system.business_note_auto_publish",
                trigger_type="cron",
                cron_expression="0 0 9 * * *",
                status="active",
                params={
                    "account_ids": [],
                    "limit": 2,
                    "interval_seconds": 5,
                    "generate_mode": "qianfan",
                },
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id
            db.close()

            _ensure_fanout_target(SessionLocal)
            execute_job(job_id, trigger_type="scheduled")
        finally:
            pass

        # 兜底必须真的发生了：三个缺键按注册表默认值补齐
        assert captured, "执行函数没被调用（兜底 or 浅校验把它挡了？）"
        assert captured.get("retry_on_failure") is True, captured
        assert captured.get("max_retries") == 3, captured
        assert captured.get("retry_interval_seconds") == 30, captured
        # 已有值**不得**被默认值覆盖
        assert captured.get("limit") == 2, captured
        assert captured.get("interval_seconds") == 5, captured


# --------------------------------------------------------------------------- #
# 任务序列化：未知 job_key 不得拖垮整页（2026-10-11 修 NameError）
# --------------------------------------------------------------------------- #
class TestJobSerializationToleratesUnknownJobKey:
    """`_serialize_job` 遇到**不在注册表的 `job_key`**（存量脏行 / 已退场的任务）时
    必须**如实降级**，不许抛异常。

    ## 为什么这条必须有（原实现的真实 bug）

    ```python
    try:
        spec = get_job_spec(job.job_key)          # 未命中 ⇒ 抛 KeyError
        params_editable = list(spec.params_editable)
    except KeyError:
        params_editable = []                       # ← 这里 spec **从未被绑定**
    ...
        "params_defaults": dict(spec.params_defaults),   # ← NameError！
    ```

    `spec` 只在 `try` 里赋值 ⇒ 走 `except` 后引用它抛的是 **`NameError`**，
    **不是 `KeyError`** ⇒ 接不住 ⇒ **整个 `GET /schedule/jobs` 500**，
    任务中心整页打不开。

    触发场景在本项目**真实发生过两次**：`system.queue_record_cleanup` 改名、
    `workflow.*` 整条退场 —— 那两次都有存量行指着旧键。当时库处于「迁移已清干净」
    的窗口期所以没炸；只要有一条脏行留着，用户看到的就是「任务中心白屏」。
    """

    def _job(self, job_key: str):
        from backend.app.models.scheduled_job import ScheduledJob

        return ScheduledJob(
            user_id=1,
            name="存量脏行",
            job_key=job_key,
            trigger_type="cron",
            cron_expression="0 0 9 * * *",
            status="active",
            params={"whatever": 1},
        )

    def test_unknown_key_does_not_raise_and_degrades_truthfully(self):
        from backend.app.api.schedule import _serialize_job

        payload = _serialize_job(self._job("system.this_key_is_gone"))

        #: 不抛异常是底线（原实现在这里 NameError）
        assert payload["job_key"] == "system.this_key_is_gone"
        #: 缺 spec ⇒ 两个派生字段**退化成空**（缺值不猜），而不是编一份默认参数
        assert payload["params_editable"] == []
        assert payload["params_defaults"] == {}
        #: 行自身的字段照旧原样透出（用户至少能看到「有这么一行、键是什么」）
        assert payload["name"] == "存量脏行"
        assert payload["params"] == {"whatever": 1}
        assert payload["cron_expression"] == "0 0 9 * * *"

    def test_known_key_still_carries_the_registry_contract(self):
        """反向：正常键必须照旧带出注册表的 editable / defaults（别把守卫写成一刀切空）。"""
        from backend.app.api.schedule import _serialize_job

        payload = _serialize_job(self._job("system.picture_space_cleanup"))
        assert payload["params_editable"] == ["account_ids", "delete_mode"]
        assert payload["params_defaults"]["delete_mode"] == "permanent"
