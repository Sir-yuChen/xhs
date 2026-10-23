"""账号级任务统一底座（设计 13 §35.16；**§42 起并行粒度改为「每类任务一条 worker」**）。

这批用例盯的是**三条根因各自的可判据**（详见 §35.16.1）：

1. **复位覆盖**（③ 真缺陷）：进程退出留下的账号级 `running` 行必须被复位成 `pending`
   —— 它们不在 `QUEUE_ITEM_TASK_TYPES` 里，此前永远卡在「同步中」（DEF-048 同型）；
2. **并行粒度**（**§42，2026-09-28 用户口径「同一个平台账号下应支持不同任务并行执行」**）：
   * **类间并行**：同一账号的「同步笔记」与「同步商品」**同时** `running`（各一条 worker）；
   * **类内串行**：同类型的两条任务仍**不并发**（第一条在跑时第二条保持 `pending`）。
   ⚠️ 旧口径「worker 只有一条线程 ⇒ 一次只有一条账号级任务在跑」**已被取代**，原句保留
   在 `test_worker_runs_account_tasks_serially` 的 docstring 里（那里现在只守「类内串行」）；
3. **入队即返回 + 语义落库**：`POST /accounts/{id}/notes/sync` 不再起线程，
   且 `mode` / 时间窗**写进任务行**（worker 从行重建参数 —— 不落库就丢）；
4. **可见性**：`GET /tasks?task_types=…` 的筛选在**后端**做（前端过滤会被分页切掉）；
5. **保留期清理覆盖全部账号级任务类型**（§44 起 4 类：笔记 / 商品笔记 / 商品 / 清理；
   不再是「队列记录 15 天、账号级行永久」的双轨）；
6. **看门狗按缺口补齐**（§42）：死掉**那一类**的 worker 要被补回来，且复位中断行时
   **跳过**还活着的那条 worker 手上的任务（不把行从它脚下抽走）。
"""
from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import select

from backend.app.models import PlatformAccount, Task, User
from backend.app.services import account_task_service as ats
from backend.app.services import note_queue_service, sync_task_service as sts
from backend.app.services import note_sync_service as nss
from backend.app.services import shop_goods_sync_service as sgs


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration

ACCOUNT_TYPES = note_queue_service.ACCOUNT_TASK_TYPES


@pytest.fixture(autouse=True)
def _drain_account_tasks(monkeypatch, db_factory):
    """用例结束后停 worker 并等**账号级**任务没有在跑的（全局线程 + 库行，不许泄漏）。

    ⚠️ 夹具**显式请求 `monkeypatch`** ⇒ teardown 在 monkeypatch 恢复**之前**跑：
    worker 自建 Session 走模块级 `_session_factory`（本文件的用例 patch 成临时库），
    先恢复 patch 再收 worker 会让它拿**真库**去认领（数据隔离事故）。
    """
    monkeypatch.setattr(ats, "_session_factory", db_factory)
    yield
    ats.stop_account_task_worker(timeout=10)
    deadline = time.time() + 10
    while time.time() < deadline:
        db = db_factory()
        try:
            # ⚠️ 不能复用 `list_note_pks_by_status`：它按 `note_pk` 索引，而账号级行
            # 没有笔记主键（恒不命中 ⇒ 夹具会永远认为「没在跑」= 假绿）。
            busy = (
                db.scalars(
                    select(Task).where(
                        Task.task_type.in_(ACCOUNT_TYPES), Task.status == "running"
                    )
                ).first()
                is not None
            )
        finally:
            db.close()
        if not busy:
            return
        time.sleep(0.05)
    pytest.fail("账号任务 worker 在用例结束后 10s 仍在跑（worker 卡死？）")


def _register(api_client, username: str) -> dict[str, str]:
    response = api_client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _user_id(db_factory, username: str) -> int:
    db = db_factory()
    try:
        return int(db.scalar(select(User.id).where(User.username == username)))
    finally:
        db.close()


def _make_account(db_factory, user_id: int, nickname: str = "测试店") -> int:
    db = db_factory()
    try:
        # ⚠️ FK=ON（2026-10-04）：`db_factory` 按契约不播种，父行就地补
        # （本助手带 user_id 参数，跨租户用例会传别的 id）。
        from tests.backend.conftest import seed_owner_rows as _seed

        _seed(db.get_bind(), user_id=user_id)
        account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="creator", nickname=nickname, status="active"
        )
        db.add(account)
        db.commit()
        return account.id
    finally:
        db.close()


def _make_task(
    db_factory,
    user_id: int,
    task_type: str,
    *,
    account_id: int = 0,
    status: str = "pending",
    payload_extra: dict | None = None,
) -> int:
    db = db_factory()
    try:
        # ⚠️ FK=ON（2026-10-04）：`db_factory` 按契约不播种。
        from tests.backend.conftest import seed_owner_rows as _seed

        _seed(db.get_bind(), user_id=user_id)
        payload = {"account_id": account_id, "source": "manual"}
        payload.update(payload_extra or {})
        task = Task(
            user_id=user_id, platform="xhs", task_type=task_type, status=status, payload=payload
        )
        db.add(task)
        db.commit()
        return int(task.id)
    finally:
        db.close()


def _task_status(db_factory, task_id: int) -> str:
    db = db_factory()
    try:
        return str(db.get(Task, task_id).status)
    finally:
        db.close()


def _finish_task(db_factory, task_id: int, status: str = "done") -> None:
    """把一条行落成终态（用例收尾用：autouse 夹具会等「没有 running 的账号级行」）。"""
    db = db_factory()
    try:
        row = db.get(Task, task_id)
        if row is not None:
            row.status = status
            db.commit()
    finally:
        db.close()


class _FakeRunner:
    """账号级执行体的假替身（脚本化，不联网、不打平台）。

    `calls` 按调用顺序记 `task_id`；第一个调用先置 `reached` 再等 `gate`
    —— 把 worker 停在「第一条执行中」的确定态，用来断言串行与快照。
    """

    def __init__(self, db_factory) -> None:
        self.db_factory = db_factory
        self.calls: list[int] = []
        self.gate = threading.Event()
        self.reached = threading.Event()

    def __call__(self, account_id, *, task_id=None, **kwargs):
        self.calls.append(int(task_id))
        if not self.reached.is_set():
            self.reached.set()
            self.gate.wait(timeout=10)
        # 执行体自己写终态（生产里 run_note_sync / run_shop_goods_sync 就是这么做的）
        db = self.db_factory()
        try:
            row = db.get(Task, task_id)
            if row is not None:
                row.status = "done"
                db.commit()
        finally:
            db.close()
        return {"task_id": task_id, "status": "done"}


def test_recover_covers_account_level_running_rows(db_factory, monkeypatch):
    """③ 真缺陷的判据：账号级 `running` 行必须被启动恢复复位成 `pending`。"""
    monkeypatch.setattr(ats, "_session_factory", db_factory)
    task_id = _make_task(db_factory, 1, "xhs_shop_goods_sync", account_id=7, status="running")

    result = ats.recover_account_tasks()

    assert result["requeued"] == 1, "账号级 running 行没被复位（会永久卡在「同步中」）"
    assert _task_status(db_factory, task_id) == "pending"


def test_recover_leaves_terminal_rows_alone(db_factory, monkeypatch):
    """复位**只动 `running`**：终态行原样（业务失败不自动重试）。"""
    monkeypatch.setattr(ats, "_session_factory", db_factory)
    done_id = _make_task(db_factory, 1, "xhs_note_sync", account_id=3, status="done")

    ats.recover_account_tasks()

    assert _task_status(db_factory, done_id) == "done"


def test_worker_runs_account_tasks_serially(api_client, db_factory, monkeypatch):
    """**类内串行**（§42 收窄后的口径）：同类型的两条任务不并发。

    ⚠️ 原口径（2026-09-27 §35.16）是「**全局**串行」——原句保留：
    ~~「串行：第一条在跑时第二条**保持 pending**（不起子线程 ⇒ 不并发打平台）」~~
    **被 §42（2026-09-28 用户口径）取代**：并行粒度改为**任务类型级** —— 这里两条都是
    `xhs_note_sync`，所以仍然串行（**类内**）；**不同类**可并行的判据在
    `test_note_and_goods_sync_run_in_parallel_for_same_account` 里。
    """
    headers = _register(api_client, "acct-serial")
    user_id = _user_id(db_factory, "acct-serial")
    account_id = _make_account(db_factory, user_id)
    first = _make_task(db_factory, user_id, "xhs_note_sync", account_id=account_id)
    second = _make_task(db_factory, user_id, "xhs_note_sync", account_id=account_id)

    fake = _FakeRunner(db_factory)
    monkeypatch.setattr(ats, "_session_factory", db_factory)
    monkeypatch.setattr("backend.app.services.note_sync_service.run_note_sync", fake)

    ats.ensure_account_task_worker()
    assert fake.reached.wait(timeout=10), "worker 没有认领第一条"

    assert _task_status(db_factory, first) == "running"
    assert _task_status(db_factory, second) == "pending", (
        "同类型（同类）的两条任务不该并发 —— 一条线程只认自己那一类"
    )

    fake.gate.set()
    deadline = time.time() + 20
    while time.time() < deadline and _task_status(db_factory, second) != "done":
        time.sleep(0.05)
    assert fake.calls == [first, second], f"认领顺序应为 FIFO，实际 {fake.calls}"
    assert headers  # 令牌注册成功（上面用过）


# --------------------------------------------------------------------------- #
# §42（2026-09-28 用户口径）：并行粒度 = 任务类型级
# --------------------------------------------------------------------------- #
def test_worker_pool_has_one_thread_per_task_type(db_factory, monkeypatch):
    """每类任务**各一条** worker，线程名 = `account-task-worker-{task_type}`。

    ⚠️ 这是「类间并行」的形状判据：旧实现只有一条 `account-task-worker`，
    `ACCOUNT_WORKER_TYPES` 里的两个类型由它按顺序认领 ⇒ 谁也别想并行。
    """
    monkeypatch.setattr(ats, "_session_factory", db_factory)
    ats.ensure_account_task_worker()

    assert ats.account_worker_thread_alive(), "池里每类任务都应有一条活着的 worker"
    for task_type in ats.ACCOUNT_WORKER_TYPES:
        assert ats.account_worker_alive(task_type), f"{task_type} 没有自己的 worker"
    alive = {
        thread.name
        for thread in threading.enumerate()
        if ats.thread_name_matches_account_worker(thread.name)
    }
    assert alive == {ats.account_worker_thread_name(t) for t in ats.ACCOUNT_WORKER_TYPES}, (
        f"线程名集合应为「每类一条」，实际 {alive}"
    )


def test_note_and_goods_sync_run_in_parallel_for_same_account(
    api_client, db_factory, monkeypatch
):
    """**类间并行**（用户口径「同一个平台账号下应支持不同任务并行执行」）。

    同一个账号上，笔记同步与商品同步**同时**进入 `running` —— 旧实现里第二条会一直
    停在 `pending`（要等第一条 30–48 分钟跑完），这正是用户报障的「依旧无法并行」。
    """
    headers = _register(api_client, "acct-parallel")
    user_id = _user_id(db_factory, "acct-parallel")
    account_id = _make_account(db_factory, user_id)
    note_task = _make_task(db_factory, user_id, "xhs_note_sync", account_id=account_id)
    goods_task = _make_task(db_factory, user_id, "xhs_shop_goods_sync", account_id=account_id)

    note_fake = _FakeRunner(db_factory)
    goods_fake = _FakeRunner(db_factory)
    monkeypatch.setattr(ats, "_session_factory", db_factory)
    monkeypatch.setattr(nss, "run_note_sync", note_fake)
    monkeypatch.setattr(sgs, "run_shop_goods_sync", goods_fake)

    ats.ensure_account_task_worker()
    assert note_fake.reached.wait(timeout=10), "笔记同步没被认领"
    assert goods_fake.reached.wait(timeout=10), (
        "商品同步没被认领 —— 它被笔记同步挡在后面了（类间并行的判据）"
    )
    assert _task_status(db_factory, note_task) == "running"
    assert _task_status(db_factory, goods_task) == "running", (
        "同一账号的两类任务必须能同时 running（§42 用户口径）"
    )

    note_fake.gate.set()
    goods_fake.gate.set()
    deadline = time.time() + 20
    while time.time() < deadline and (
        _task_status(db_factory, note_task) != "done"
        or _task_status(db_factory, goods_task) != "done"
    ):
        time.sleep(0.05)
    assert _task_status(db_factory, note_task) == "done"
    assert _task_status(db_factory, goods_task) == "done"
    assert headers


def _probe_from_registry(monkeypatch):
    """把存活判据换成**读成员表**（测试用）。

    为什么要换：真线程**无法被杀死**（Python 没有 API），而「某类 worker 死了」正是
    要造的现场 —— 只把成员表里的那项换成已结束的线程是造不出真缺口的（真线程还在
    `threading.enumerate()` 里，探针照旧报活）。换成读成员表之后，「成员项已死」=
    「该类缺 worker」，与生产里「线程带异常退出」在**判据层面**等价。
    """
    def _alive(task_type: str) -> bool:
        thread = ats._account_workers.get(task_type)
        return bool(thread is not None and thread.is_alive())

    monkeypatch.setattr(ats, "account_worker_alive", _alive)


def _dead_thread(name: str) -> threading.Thread:
    thread = threading.Thread(target=lambda: None, name=name)
    thread.start()
    thread.join()
    return thread


def test_worker_pool_health_probe_detects_a_real_gap(db_factory, monkeypatch):
    """真探针（`account_worker_thread_alive`）**确实会**在缺一条时报「不健康」。

    上一条用例把探针换成了读成员表（真线程杀不掉），所以这里补一条**不 patch** 的：
    停掉整池 ⇒ 真探针必须判不健康（否则「池健康」这个判据是恒真的摆设）。
    """
    monkeypatch.setattr(ats, "_session_factory", db_factory)
    ats.ensure_account_task_worker()
    assert ats.account_worker_thread_alive() is True

    ats.stop_account_task_worker(timeout=10)
    assert ats.account_worker_thread_alive() is False, (
        "整池都停了还判「健康」⇒ 看门狗永远不会重建（判据恒真）"
    )
    assert ats.account_worker_alive("xhs_note_sync") is False


def test_watchdog_revives_only_the_missing_task_type(db_factory, monkeypatch):
    """看门狗**按缺口补齐**：只补死掉那一类，活着的另一类**不换实例**。

    变异实测（把 `_revive_missing_workers` 改回「有一条活着就算好」）⇒ 本用例红
    （死掉那一类永远没人认领）。所以这里必须断言**两条**：缺口被补 + 活的没被换掉。
    """
    monkeypatch.setattr(ats, "_session_factory", db_factory)
    ats.ensure_account_task_worker()
    _probe_from_registry(monkeypatch)
    alive_before = ats._account_workers["xhs_shop_goods_sync"]

    # 模拟「笔记同步那条线程死了」：把它的成员项换成一个已结束的线程
    with ats._account_worker_lock:
        ats._account_workers["xhs_note_sync"] = _dead_thread("stub-dead")

    assert not ats.account_worker_thread_alive(), "池里有缺口时不该判成健康"
    assert ats._revive_missing_workers() is True, "发现缺口就该重建"
    assert ats.account_worker_alive("xhs_note_sync"), "死掉那一类没被补回来"
    assert ats._account_workers["xhs_shop_goods_sync"] is alive_before, (
        "活着的那条不该被换掉（换实例 = 手上那条任务被从脚下抽走）"
    )
    assert ats._revive_missing_workers() is False, "池全活时不该重建（幂等）"


def test_revive_skips_tasks_held_by_the_live_worker(db_factory, monkeypatch):
    """复位中断行时**跳过还活着的 worker 手上的任务**（不把行从它脚下抽走）。

    现场：商品同步那条 worker 还活着、正在跑一条 `running` 行；笔记同步那条死了 ⇒
    重建时会调 `recover_interrupted`（判据是「所有 `running` 行」）—— 不排除持有项
    就会把商品那条改回 `pending`：被跑两遍 + 原 worker 跑完把一条已不属于它的行写成终态。
    """
    monkeypatch.setattr(ats, "_session_factory", db_factory)
    ats.ensure_account_task_worker()
    _probe_from_registry(monkeypatch)
    held = _make_task(db_factory, 1, "xhs_shop_goods_sync", account_id=9, status="running")
    # ⚠️ 孤儿行刻意用 `xhs_no_traffic_cleanup`：它在 `ACCOUNT_TASK_TYPES`（复位范围）里，
    # 但**不在** `ACCOUNT_WORKER_TYPES`（无人认领）⇒ 复位成 `pending` 之后不会被任何
    # worker 抢走，断言不引入竞态（用笔记同步类型会被新起的 worker 立刻认领）。
    orphan = _make_task(db_factory, 1, "xhs_no_traffic_cleanup", account_id=9, status="running")

    # 把「商品同步那条 worker 手上持有 held」登记成真的（`_held_task_ids` 只算活线程）
    # ⚠️ 持有者用**自己的 Event** 挂住，**不要**复用 `_account_worker_stop`：那个停止位
    # 一旦已被置位，线程会**立刻退出** ⇒ `_held_task_ids()` 变空 ⇒ 本用例对「复位是否
    # 排除持有项」**失去判别力**（变异实测出现过一次假绿）。
    release = threading.Event()
    holder = threading.Thread(
        target=lambda: release.wait(timeout=30),
        name=ats.account_worker_thread_name("xhs_shop_goods_sync"),
        daemon=True,
    )
    holder.start()
    try:
        ats._mark_worker_busy(holder.name, held)
        # 制造「笔记同步那条死了」的缺口
        with ats._account_worker_lock:
            ats._account_workers["xhs_note_sync"] = _dead_thread("stub-dead-note")

        assert ats._revive_missing_workers() is True
        assert _task_status(db_factory, held) == "running", (
            "还活着的那条 worker 手上的任务被复位了（会被跑两遍）"
        )
        assert _task_status(db_factory, orphan) == "pending", "死掉那条留下的 running 行必须复位"
    finally:
        ats._mark_worker_idle(holder.name)
        release.set()
        # 收尾：把这条 running 行落成终态，否则 autouse 夹具会等它到超时
        _finish_task(db_factory, held)


def test_progress_flush_granularity_is_50_items():
    """「任务执行详情」刷新粒度 = **每 50 条**（2026-09-28 用户口径）。

    粒度是 `sts.PROGRESS_FLUSH_EVERY`（两个执行体共用**一个数**，DEF-044），
    且**不小于**默认页大小 —— 页是刷盘点，页比粒度大就等于「超过 50 条才刷」。
    """
    assert sts.PROGRESS_FLUSH_EVERY == 50, "用户口径写的就是 50 条刷一次"
    assert sts.DEFAULT_PAGE_SIZE <= sts.PROGRESS_FLUSH_EVERY, (
        "默认页大小不得超过刷新粒度（页边界就是刷盘点）"
    )


def test_note_sync_flushes_progress_before_the_slow_hydration():
    """笔记同步**落库即刷进度**：刷盘点必须在页内详情补齐**之前**。

    为什么这是判据（2026-09-28 用户口径「每执行 50 条应更新一次任务执行详情数据」）：
    页内详情补齐要逐篇打 www（额度 8 次/分钟 ⇒ 一页 50 条约 6 分钟），而旧实现把
    `_update_progress` 只放在**页尾** ⇒ 这 6 分钟里进度数字一动不动（用户报的
    「进度不更新」）。⚠️ 变异实测：把刷盘点挪回补齐之后 ⇒ 本用例红。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    source = (root / "backend/app/services/note_sync_service.py").read_text(encoding="utf-8")
    # 翻页循环在 `_sync_source`（`_sync_with_task` 是它的编排器，排在后面）
    body = source[source.index("def _sync_source(") : source.index("def _sync_with_task(")]
    # 剥注释：解释性注释里也写着 `_hydrate_page_notes`，不剥会命中自己写的说明（DEF-056）
    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )
    store_at = code.index("stored += len(saved)")
    flush_at = code.index("_update_progress(", store_at)
    hydrate_at = code.index("_hydrate_page_notes(")
    assert store_at < flush_at < hydrate_at, (
        "刷盘点必须在「本页落库之后、页内详情补齐之前」—— 否则一页要等 6 分钟才刷一次"
    )
    # 2026-09-28 用户口径「进度条慢慢增加」：页内补齐**必须接进度回调** ——
    # 不接的话那 6 分钟里 `processed` 一个都不加，进度条照旧一动不动。
    assert "on_progress=_on_page_hydrate" in code, (
        "页内详情补齐必须传 on_progress（否则页内进度永远不动）"
    )
    assert "processed" in code and "work_total" in code, (
        "翻页循环必须维护 processed / work_total（进度条的首选口径）"
    )


def test_start_note_sync_returns_immediately_and_persists_semantics(
    api_client, db_factory, monkeypatch
):
    """入队即返回：不建线程；`mode` / 时间窗**写进任务行**（worker 从行重建）。"""
    headers = _register(api_client, "acct-enqueue")
    user_id = _user_id(db_factory, "acct-enqueue")
    account_id = _make_account(db_factory, user_id)

    called = {"wake": 0}
    monkeypatch.setattr(
        ats, "ensure_account_task_worker", lambda *a, **k: called.__setitem__("wake", called["wake"] + 1)
    )
    # ⚠️ `start_note_sync` **自开 Session**（`SessionLocal()`）—— `api_client` 只覆盖
    # `get_db`，不覆盖模块级 SessionLocal ⇒ 不 patch 就会把行建到**真库**上
    # （断言看不到 = 假绿，且污染真实数据）。
    monkeypatch.setattr(nss, "SessionLocal", db_factory)

    response = api_client.post(
        f"/api/accounts/{account_id}/notes/sync",
        headers=headers,
        json={"mode": "full", "scope": "range", "start_date": "2026-09-01", "end_date": "2026-09-10"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    assert called["wake"] == 1, "start_note_sync 应唤醒账号任务 worker（不再起线程）"

    db = db_factory()
    try:
        row = db.scalars(
            select(Task).where(Task.task_type == sts.NOTE_SYNC_TASK_TYPE)
        ).one()
        payload = row.payload
        assert payload["mode"] == "full", "mode 必须落库（否则 worker 重建时丢失）"
        assert int(payload["time_from_ms"]) > 0 and int(payload["time_to_ms"]) > 0, (
            "时间窗必须落库（否则用户选的区间静默变成全量）"
        )
    finally:
        db.close()


def test_account_tasks_are_filtered_by_type_on_the_backend(api_client, db_factory):
    """可见性：`GET /tasks?task_types=` 在**后端**过滤（前端过滤会被分页切掉）。"""
    headers = _register(api_client, "acct-filter")
    user_id = _user_id(db_factory, "acct-filter")
    # 造 25 条**别的类型**（足够一页 20 条以上）+ 2 条账号级
    for index in range(25):
        _make_task(db_factory, user_id, "ai_generate_note", status="done", payload_extra={"n": index})
    wanted = [
        _make_task(db_factory, user_id, "xhs_shop_goods_sync", account_id=9, status="done"),
        _make_task(db_factory, user_id, "xhs_no_traffic_cleanup", account_id=9, status="failed"),
    ]

    body = api_client.get(
        "/api/tasks",
        params={"task_types": "xhs_shop_goods_sync,xhs_no_traffic_cleanup"},
        headers=headers,
    ).json()
    assert body["total"] == 2, "后端过滤：账号级行不该被 25 条别的类型挤出分页"
    assert sorted(item["id"] for item in body["items"]) == sorted(wanted)
    assert {item["task_type"] for item in body["items"]} == {
        "xhs_shop_goods_sync",
        "xhs_no_traffic_cleanup",
    }


def test_retention_cleanup_covers_all_account_level_types(db_factory, monkeypatch):
    """保留期清理覆盖**全部**账号级任务类型（原来是「队列记录 15 天、账号级行永久」的双轨）。

    ⚠️ 计数随 `ACCOUNT_TASK_TYPES` 走（§44 起 4 类：笔记同步 / 商品笔记同步 / 商品同步 /
    无流量清理），**不许写死字面量** —— 它只是「循环确实跑遍了这张表」的证明。
    """
    monkeypatch.setattr(ats, "_session_factory", db_factory)
    from backend.app.core.time import shanghai_now
    from datetime import timedelta

    long_ago = shanghai_now() - timedelta(days=40)
    db = db_factory()
    try:
        # ⚠️ FK=ON（2026-10-04）：父行就地补（`db_factory` 不播种）。
        from tests.backend.conftest import seed_owner_rows as _seed

        _seed(db.get_bind(), user_id=1)
        for task_type in ACCOUNT_TYPES:
            task = Task(
                user_id=1,
                platform="xhs",
                task_type=task_type,
                status="done",
                payload={"account_id": 5},
                created_at=long_ago,
            )
            db.add(task)
        db.commit()
    finally:
        db.close()

    result = note_queue_service.cleanup_records(db_factory(), user_id=1, retention_days=15)
    assert result["account_tasks"] == len(ACCOUNT_TYPES), (
        "账号级任务类型都要被保留期清理覆盖（计数跟着 ACCOUNT_TASK_TYPES 走，不写死）"
    )
    db = db_factory()
    try:
        assert db.scalars(select(Task).where(Task.task_type.in_(ACCOUNT_TYPES))).all() == []
    finally:
        db.close()
