"""同步任务的**协作式中断**与计时/异常字段（设计 13 §24.8）。

用户口径（2026-09-23）：「任务执行中应支持中断，异常任务应记录异常原因并
页面展示，记录任务执行开始时间结束时间，页面展示耗时」。

盯五件事：

1. **启动竞态**：线程启动前就被取消 ⇒ 不翻转为 `running`、一个平台请求都不发；
2. **循环检查点**：翻页每页检查，命中即停（不再打平台），终态是 `cancelled`
   + `finished_at`，**不**被跑完的线程覆写成 done；
3. **终态保护**：`_finish` / `_mark_failed` 不覆写 `cancelled`；
4. **取消端点**：条件更新（不覆写已终态行）+ 写 `finished_at` + 发信号 + 幂等；
5. **计时字段**：`status_payload` 回 `started_at` / `finished_at` / `duration_ms`，
   `cancelled` 进 `last_result`（否则卡片结果行退回「同步已结束」，用户看不到
   自己刚点的取消生效了）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.api.platforms.xhs import pc
from backend.app.core.database import Base, get_db
from backend.app.core.security import create_access_token
from backend.app.core.time import shanghai_now
from backend.app.main import app
from backend.app.models import PlatformAccount, ScheduledJobLog, Task, User
from backend.app.models.scheduled_job import LOG_FAILED, LOG_SUCCESS
from backend.app.services import note_sync_service as nss
from backend.app.services import shop_goods_sync_service as sgs
from backend.app.services import sync_task_service as sts
from backend.app.services import task_cancel
from tests.backend.cookie_fixtures import bind_shop_account


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration

client = TestClient(app)

CREATOR_COOKIES = (
    "galaxy_creator_session_id=abc123; "
    "access-token-creator.xiaohongshu.com=ctok123; "
    "customer-sso-sid=sso123; a1=xyz"
)

#: 当前用例的 task_id（假适配器在里面发取消信号用）。
TASK_ID: list = []


class AllowLimiter:
    def allow(self, account_id, **kwargs) -> bool:  # noqa: ARG002
        return True

    def wait_allow(self, account_id, **kwargs) -> bool:  # noqa: ARG002
        return True


class FakeCreatorAdapter:
    """三页创作者笔记；第 1 页拉到手就替用户点「取消」（信号进 TASK_ID）。"""

    calls: list = []

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def get_posted_notes_page(self, page: int = 0, tab: int = 0):
        type(self).calls.append(page)
        if len(type(self).calls) == 1 and TASK_ID:
            task_cancel.signal(TASK_ID[0])
        pages = {0: (["C-N1"], 1), 1: (["C-N2"], 2), 2: (["C-N3"], -1)}
        ids, next_page = pages.get(page, ([], -1))
        return True, "", {
            "data": {
                "notes": [_creator_note(note_id) for note_id in ids],
                "page": next_page,
                "tags": [{"name": "所有笔记", "notes_count": 3, "checked": True}],
            }
        }


class FakeGoodsAdapter:
    """三页商品；第 1 页拉到手就替用户点「取消」。"""

    calls: list = []

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def search_items(self, **kwargs):
        index = len(type(self).calls)
        type(self).calls.append(kwargs)
        if index == 0 and TASK_ID:
            task_cancel.signal(TASK_ID[0])
        if index >= 3:
            return True, "", {"code": 0, "data": {"items": [], "total": 0}}
        return True, "", {
            "code": 0,
            "data": {
                "items": [_goods_item(f"ITEM-{index + 1}")],
                "total": 3,
                "has_more": True,
            },
        }

    def get_skus_by_item_ids(self, item_ids):
        return True, "", {"code": 0, "data": {"skus": {}}}


def _creator_note(note_id: str) -> dict:
    return {
        "id": note_id,
        "display_title": f"创作 {note_id}",
        "time": "2026-08-13 09:30",
        "likes": 12,
        "collected_count": 3,
        "comments_count": 1,
        "shared_count": 0,
        "view_count": 100,
        "images_list": [{"url": f"https://ci.example/{note_id}.jpg"}],
        "goods_info": {"goods_name": []},
    }


def _goods_item(item_id: str) -> dict:
    return {"item_id": item_id, "item_name": f"商品 {item_id}", "min_price": 1999, "max_price": 1999}


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cancel.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(nss, "SessionLocal", SessionLocal)
    monkeypatch.setattr(sgs, "SessionLocal", SessionLocal)
    monkeypatch.setattr(
        nss, "_account_cookies",
        lambda db, account_id, *, channel="www": CREATOR_COOKIES if channel == "creator" else "",
    )
    monkeypatch.setattr(nss, "PAGE_GAP_SECONDS", 0.0)
    monkeypatch.setattr(sgs, "PAGE_GAP_SECONDS", 0.0)
    monkeypatch.setattr(nss, "get_rate_limiter", lambda: AllowLimiter())
    monkeypatch.setattr(sgs, "get_rate_limiter", lambda: AllowLimiter())
    monkeypatch.setattr(pc, "get_xhs_creator_api_adapter_factory", lambda: FakeCreatorAdapter)
    monkeypatch.setattr(
        "backend.app.services.asset_downloader.download_asset_to_local", lambda *a, **k: None
    )
    FakeCreatorAdapter.calls = []
    FakeGoodsAdapter.calls = []
    TASK_ID.clear()

    db = SessionLocal()
    try:
        user = User(username="owner", password_hash="x")
        db.add(user)
        db.flush()
        account = PlatformAccount(
            user_id=user.id, platform="xhs", sub_type="creator",
            nickname="我的号", status="active",
        )
        db.add(account)
        db.flush()
        bind_shop_account(db, account, cookies_text=CREATOR_COOKIES)
        db.commit()
        user_id, account_id = user.id, account.id
    finally:
        db.close()
    headers = {"Authorization": f"Bearer {create_access_token(user_id)}"}
    yield headers, account_id, user_id, SessionLocal
    app.dependency_overrides.clear()
    # task_id 是库内自增：每个用例一个临时库 ⇒ id 从 1 重排。不清登记表，
    # 上一个用例 signal 过的事件会被下一个用例的同 id 任务读到（测试污染）。
    task_cancel.clear_all()


def _new_task(SessionLocal, *, user_id: int, account_id: int, task_type: str, status: str) -> int:
    db = SessionLocal()
    try:
        task = Task(
            user_id=user_id,
            platform="xhs",
            task_type=task_type,
            status=status,
            payload={"account_id": account_id, "source": "manual"},
        )
        db.add(task)
        db.commit()
        return task.id
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 1) 启动竞态：线程启动前已被取消
# --------------------------------------------------------------------------- #
def test_pre_start_cancel_returns_without_flipping_to_running(env):
    """任务行已是 `cancelled` ⇒ 直接终态返回，**不**翻转为 running、零平台请求。"""
    _, account_id, user_id, SessionLocal = env
    task_id = _new_task(
        SessionLocal, user_id=user_id, account_id=account_id,
        task_type=sts.NOTE_SYNC_TASK_TYPE, status=sts.STATUS_CANCELLED,
    )

    result = nss.run_note_sync(account_id, task_id=task_id, user_id=user_id)

    assert result["status"] == sts.STATUS_CANCELLED
    db = SessionLocal()
    try:
        row = db.get(Task, task_id)
        assert row.status == "cancelled"
        assert row.finished_at is not None  # 耗时统计的终点
    finally:
        db.close()
    assert FakeCreatorAdapter.calls == []  # 一个平台请求都没发


# --------------------------------------------------------------------------- #
# 2) 循环检查点：命中即停
# --------------------------------------------------------------------------- #
def test_note_mid_run_cancel_stops_page_loop(env):
    """第 1 页落库后取消 ⇒ 只打 1 页平台、终态 cancelled + finished_at。"""
    _, account_id, user_id, SessionLocal = env
    task_id = _new_task(
        SessionLocal, user_id=user_id, account_id=account_id,
        task_type=sts.NOTE_SYNC_TASK_TYPE, status=sts.STATUS_PENDING,
    )
    TASK_ID.append(task_id)  # 假适配器第 1 页后发信号

    result = nss.run_note_sync(account_id, task_id=task_id, user_id=user_id)

    assert result["status"] == sts.STATUS_CANCELLED
    assert FakeCreatorAdapter.calls == [0], "取消后还在翻页：检查点没生效"
    db = SessionLocal()
    try:
        row = db.get(Task, task_id)
        assert row.status == "cancelled"
        assert row.finished_at is not None
        note = str((row.payload or {}).get("note", ""))
        assert "已被用户取消" in note
        # ⚠️ **只许出现一次**（DEF-081）：调用点与 `_finish`（:3210）各拼一次会变成
        # 「已被用户取消；已被用户取消」（2026-09-27 真机 `task=11` 的 payload 实读）。
        # 上面那条 `in` 断言是**子串匹配** ⇒ 重复文案照样通过 —— 这正是瑕疵逃逸的原因，
        # 所以这里必须计数（项目纪律：负向 / 精确断言禁裸 `in`）。
        assert note.count("已被用户取消") == 1, f"取消标记重复了：{note!r}"
    finally:
        db.close()


def test_goods_mid_run_cancel_stops_page_loop(env):
    """商品同步同构：第 1 页后取消 ⇒ 只打 1 页、终态 cancelled。"""
    _, account_id, user_id, SessionLocal = env
    task_id = _new_task(
        SessionLocal, user_id=user_id, account_id=account_id,
        task_type=sts.SHOP_GOODS_SYNC_TASK_TYPE, status=sts.STATUS_PENDING,
    )
    TASK_ID.append(task_id)

    result = sgs.run_shop_goods_sync(
        account_id, task_id=task_id, user_id=user_id,
        adapter_factory=lambda cookies: FakeGoodsAdapter(cookies),
    )

    assert result["status"] == sts.STATUS_CANCELLED
    assert len(FakeGoodsAdapter.calls) == 1, "取消后还在翻页：检查点没生效"
    db = SessionLocal()
    try:
        row = db.get(Task, task_id)
        assert row.status == "cancelled"
        assert row.finished_at is not None
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 3) 终态保护：_finish 不覆写 cancelled
# --------------------------------------------------------------------------- #
def test_finish_does_not_overwrite_cancelled_note_task(env):
    """跑完的线程撞上「期间被取消」⇒ `_finish` 只补 finished_at，不改终态。"""
    _, account_id, user_id, SessionLocal = env
    task_id = _new_task(
        SessionLocal, user_id=user_id, account_id=account_id,
        task_type=sts.NOTE_SYNC_TASK_TYPE, status=sts.STATUS_RUNNING,
    )
    task_cancel.register(task_id)
    task_cancel.signal(task_id)

    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        nss._finish(task, status=sts.STATUS_DONE, stored=5, total=10, goods_stored=0)
        assert task.status == sts.STATUS_CANCELLED
        assert task.finished_at is not None
    finally:
        db.close()


def test_finish_does_not_overwrite_cancelled_goods_task(env):
    """商品侧同一条纪律（两个 service 各有一份 `_finish`，都要守）。"""
    _, account_id, user_id, SessionLocal = env
    task_id = _new_task(
        SessionLocal, user_id=user_id, account_id=account_id,
        task_type=sts.SHOP_GOODS_SYNC_TASK_TYPE, status=sts.STATUS_RUNNING,
    )
    task_cancel.register(task_id)
    task_cancel.signal(task_id)

    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        sgs._finish(task, status=sts.STATUS_DONE, stored=5, total=10)
        assert task.status == sts.STATUS_CANCELLED
        assert task.finished_at is not None
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 4) 取消端点：条件更新 + finished_at + 信号 + 幂等
# --------------------------------------------------------------------------- #
def test_cancel_endpoint_writes_finished_at_and_signals(env):
    """端点三件事一体：置 cancelled、写 finished_at、发信号；重复取消幂等。"""
    headers, account_id, user_id, SessionLocal = env
    task_id = _new_task(
        SessionLocal, user_id=user_id, account_id=account_id,
        task_type=sts.NOTE_SYNC_TASK_TYPE, status=sts.STATUS_RUNNING,
    )
    task_cancel.register(task_id)

    response = client.post(f"/api/tasks/{task_id}/cancel", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["finished_at"] is not None
    assert task_cancel.is_cancelled(task_id) is True

    again = client.post(f"/api/tasks/{task_id}/cancel", headers=headers)
    assert again.status_code == 200
    assert again.json()["status"] == "cancelled"  # 幂等，不炸


def test_cancel_endpoint_does_not_overwrite_finished_task(env):
    """线程刚提交 done，取消端点**不能**把终态覆写回 cancelled（条件更新）。"""
    headers, account_id, user_id, SessionLocal = env
    task_id = _new_task(
        SessionLocal, user_id=user_id, account_id=account_id,
        task_type=sts.NOTE_SYNC_TASK_TYPE, status=sts.STATUS_DONE,
    )
    task_cancel.register(task_id)

    response = client.post(f"/api/tasks/{task_id}/cancel", headers=headers)

    assert response.json()["status"] == "done"
    assert task_cancel.is_cancelled(task_id) is False  # 没发信号


# --------------------------------------------------------------------------- #
# 5) 计时字段 + 终态结果（**判据 = 该账号最新一条同步日志**）
# --------------------------------------------------------------------------- #
def test_status_payload_reports_timing_from_latest_log(env):
    """状态端点回起止时间与耗时；终态进 `last_result`（卡片要展示）。

    ⚠️ **判据已从「业务任务行」改为「该账号最新一条同步日志」**（2026-09-27 用户口径
    「账号信息卡的判断逻辑应只需要关注最新一条的同步笔记任务日志的状态即可」+
    「根据任务日志详情的状态判断终态即可」）：任务行与「用户点的那一下」不是一一对应
    （同一任务行会被复用 / 被恢复重跑），而日志行才是「一次点击 = 一条记录」。
    ⇒ 本用例造的是**日志行**，任务行**不再参与**判定。
    """
    _, account_id, user_id, SessionLocal = env
    started = shanghai_now() - timedelta(seconds=125)
    finished = shanghai_now()
    db = SessionLocal()
    try:
        db.add(
            ScheduledJobLog(
                user_id=user_id,
                platform_account_id=account_id,
                job_name="账号笔记同步",
                job_key="system.note_sync",
                job_trigger_type="manual",
                trigger_type="manual",
                status=LOG_SUCCESS,
                started_at=started,
                finished_at=finished,
                duration_ms=125_000,
                progress=100,
                message="同步完成",
            )
        )
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        body = sts.status_payload(
            db,
            sts.NOTE_SYNC_TASK_TYPE,
            account_id,
            user_id=user_id,
            job_key="system.note_sync",
        )
    finally:
        db.close()

    assert body["status"] == "idle"  # 日志已终态 ⇒ 不是「同步中」
    result = body["last_result"]
    assert result is not None and result["status"] == "done"
    assert result["started_at"] is not None
    assert result["finished_at"] is not None
    assert result["duration_ms"] == 125_000


def test_status_payload_running_log_means_syncing(env):
    """日志仍是 `running` ⇒ `status="running"`（前端据此显示「同步中」）。

    这是用户口径的另一半：「**只有任务日志状态在『正在执行』，才展示同步中**」。
    """
    _, account_id, user_id, SessionLocal = env
    db = SessionLocal()
    try:
        db.add(
            ScheduledJobLog(
                user_id=user_id,
                platform_account_id=account_id,
                job_name="账号笔记同步",
                job_key="system.note_sync",
                job_trigger_type="manual",
                trigger_type="manual",
                status="running",
                started_at=shanghai_now(),
                progress=37,
                message="任务仍在执行",
            )
        )
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        body = sts.status_payload(
            db,
            sts.NOTE_SYNC_TASK_TYPE,
            account_id,
            user_id=user_id,
            job_key="system.note_sync",
        )
    finally:
        db.close()

    assert body["status"] == "running"
    assert body["last_result"] is None  # 没跑完 ⇒ 没有结果行
    # ⚠️ 日志口径下 `started_at` **有值**（日志在「用户点下去」那一刻就建好了）——
    # 旧判据那句「无进行中任务 ⇒ 计时字段为 None」是**任务行**的缺值口径，不再适用。
    assert body["started_at"] is not None


# --------------------------------------------------------------------------- #
# 6) 取消时**同步收口日志** + 收口失败不许翻盘（2026-09-27 用户报障；DEF-080）
# --------------------------------------------------------------------------- #
def test_cancel_endpoint_closes_running_log(env):
    """取消一个**有 running 日志**的任务 ⇒ 日志在同一请求内收口。

    用户报障（2026-09-27）：「任务日志列表，操作列点击取消任务提示请求失败，请稍后重试，
    但任务实际已经被取消了」。

    根因：`close_running_logs_for_task` 用了**未导入**的 `shanghai_now` ⇒ `NameError`
    ⇒ 500。而取消本身在它**之前**就已 `db.commit()`（不可回滚）⇒ 用户看到「失败」但
    任务真取消了；日志行的改动也随异常一起丢掉 ⇒ 日志永远停在「正在执行」。

    ⚠️ 本用例是**唯一**能抓住它的用例：原有用例都没造 `running` 日志行 ⇒
    `close_running_logs_for_task` 返回 0 直接跳过，那条 `shanghai_now` 根本没被执行到。
    """
    headers, account_id, user_id, SessionLocal = env
    task_id = _new_task(
        SessionLocal, user_id=user_id, account_id=account_id,
        task_type=sts.NOTE_SYNC_TASK_TYPE, status=sts.STATUS_RUNNING,
    )
    db = SessionLocal()
    try:
        db.add(
            ScheduledJobLog(
                user_id=user_id,
                platform_account_id=account_id,
                job_name="账号笔记同步",
                job_key="system.note_sync",
                job_trigger_type="manual",
                trigger_type="manual",
                status="running",
                started_at=shanghai_now(),
                task_id=task_id,
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.post(f"/api/tasks/{task_id}/cancel", headers=headers)

    assert response.status_code == 200, "取消已生效却报失败 ⇒ 错误归因（用户会重复点）"
    assert response.json()["status"] == "cancelled"
    db = SessionLocal()
    try:
        log = db.scalars(select(ScheduledJobLog)).one()
        assert log.status == LOG_FAILED, "用户取消不算成功（日志表没有 cancelled 一档）"
        assert log.message == "已被用户取消"
        assert log.finished_at is not None, "日志没收口 ⇒ 会永远显示「正在执行」"
    finally:
        db.close()


def test_cancel_returns_ok_even_if_log_close_raises(env, monkeypatch):
    """收口抛异常 ⇒ 仍返回 200（取消已落库，附加动作不许翻盘）。

    守卫的是**错误归因**：把「日志没同步」说成「取消失败」，用户会以为没取消掉而重复点。
    残留的 `running` 日志由启动时的 `close_orphan_running_logs` 兜底（DEF-048）。
    """
    headers, account_id, user_id, SessionLocal = env
    task_id = _new_task(
        SessionLocal, user_id=user_id, account_id=account_id,
        task_type=sts.NOTE_SYNC_TASK_TYPE, status=sts.STATUS_RUNNING,
    )

    def _boom(db, _task_id):  # noqa: ARG001
        raise RuntimeError("收口炸了")

    monkeypatch.setattr(sts, "close_running_logs_for_task", _boom)

    response = client.post(f"/api/tasks/{task_id}/cancel", headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    db = SessionLocal()
    try:
        assert db.get(Task, task_id).status == "cancelled"
    finally:
        db.close()


def test_close_orphan_running_logs_writes_terminal_state(env):
    """孤儿日志收口**真的能跑通**（同一个 `shanghai_now` 缺失曾让它静默失败）。

    调用方 `recover_account_tasks` 用 `except BaseException` 兜底 ⇒ 抛异常时只留一行
    logger，**启动看起来完全正常**，而日志永远停在「正在执行」。这是本项目最危险的
    失败形态（静默失败）：没有报错、没有红，只有「同步中」一直转。
    """
    _, account_id, user_id, SessionLocal = env
    task_id = _new_task(
        SessionLocal, user_id=user_id, account_id=account_id,
        task_type=sts.NOTE_SYNC_TASK_TYPE, status=sts.STATUS_DONE,
    )
    db = SessionLocal()
    try:
        db.add(
            ScheduledJobLog(
                user_id=user_id,
                platform_account_id=account_id,
                job_name="账号笔记同步",
                job_key="system.note_sync",
                job_trigger_type="manual",
                trigger_type="manual",
                status="running",
                started_at=shanghai_now(),
                task_id=task_id,
            )
        )
        db.commit()

        closed = sts.close_orphan_running_logs(db)
        db.commit()

        assert closed == 1, "孤儿日志没收口（任务已 done，日志却还在 running）"
        log = db.scalars(select(ScheduledJobLog)).one()
        assert log.status == LOG_SUCCESS
        assert log.finished_at is not None
    finally:
        db.close()
