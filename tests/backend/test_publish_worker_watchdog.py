"""发布 worker 守活 + 队列持久化（设计 13 §35.9）：2026-09-25。

## 为什么发布侧单独一个文件

发布队列与同步队列**同型不同责**（§35.8 / §35.9）：同一个「单 worker FIFO +
进度快照」形态，但发布侧改造前**四件缺口全在** ——

1. `_run_one` 只有 `except Exception`（`SystemExit` 够不着，threading 对它静默）；
2. `_publish_worker_loop` **没有外层网**（篇内 finally 漏网 = 线程死）；
3. **没有看门狗**（线程死了没有任何路径发现，队列剩余篇永久停摆、笔记永远卡在
   `publishing`）；
4. **没有停止出口**（测试只能靠 daemon 语义，收不干净 ⇒ 跨文件抢队列）。

DEF-069 的现场（同步 worker 无声消失）在发布侧**同样可能发生**，只是还没撞上；
本文件把四条缺口逐条钉住，并守住「队列项落在 `tasks` 表」这件事本身。

## 与同步侧测试文件的分工

* `test_sync_worker_watchdog.py`：同步 worker 的探针 / 内网 / 外层网 / 看门狗 /
  重启续跑；
* 本文件：发布 worker 的同一套（**加**「adapter 必须按库里当前凭据重建」这条
  发布特有的判据），外加**发布快照的键名契约**（底座通用名 `synced` 必须在发布域
  改名成 `published`，不得透传）。

## 三个坑（与同步侧同款，照抄理由）

1. **死法必须是真的**：不能 `_publish_worker = Thread()` 假造死亡 —— 真线程仍活在
   `threading.enumerate()` 里，探针照旧报「活着」。要真死就起一个同名线程再 join。
2. **收尾必须按线程枚举收**：`_quiesce_named_publish_workers()` 把进程里**所有**
   活着的同名 worker 收掉，不能只信 `_publish_worker` —— xdist 同一 worker 会连跑
   多个文件，`test_product_note_publish.py` 起的真 worker 一条都不退出。
   ⚠️ 持久化后 worker 每 `PUBLISH_IDLE_POLL_SECONDS` 秒**主动查库**（不再是无超时的
   永久 `wait()`）⇒ 残留 worker 会拿**当前** `_session_factory` 去认领别人的行。
3. **收尾夹具必须显式请求 `monkeypatch`**：worker 自建 Session 走模块级
   `_session_factory`（用例 patch 成临时库）。夹具不请求 `monkeypatch` 的话，
   `monkeypatch` 会**先**恢复、worker 还活着 ⇒ 它拿着**真库**去认领测试塞的假行。
"""
from __future__ import annotations

import ast
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app.models import Note, PlatformAccount, Task
from backend.app.models.note import PUBLISH_STATUS_PUBLISHED, set_note_publish_status
from backend.app.services import note_publish_service as nps
from backend.app.services import note_queue_service as nqs

ROOT = Path(__file__).resolve().parents[2]


class _FakeAdapter:
    """假适配器：只记构造时拿到的 cookies（验证「凭据按库现取」）。"""

    made: list[str] = []

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies
        _FakeAdapter.made.append(cookies)


def _quiesce_named_publish_workers(*, timeout: float = 5.0) -> list[str]:
    """把进程里**所有活着的** `PUBLISH_WORKER_THREAD_NAME` 线程收掉。

    ⚠️ **先置停止位**（=「别再认领新任务」），再 join。持久化后 worker 的活来自
    `tasks` 表且带 `wait(timeout=…)` 兜底轮询 ⇒ 不收干净的话，残留 worker 会拿
    **当前**的 `_session_factory` 去认领别人的行 —— 夹具在场时是「抢测试的行」，
    夹具不在场时是「拿真库跑测试数据」（数据隔离事故，不是测试噪声）。
    """
    nps._publish_worker_stop.set()
    nps._publish_wakeup.set()
    stopped: list[str] = []
    for thread in list(threading.enumerate()):
        if thread.name != nps.PUBLISH_WORKER_THREAD_NAME or not thread.is_alive():
            continue
        thread.join(timeout=timeout)
        if not thread.is_alive():
            stopped.append(thread.name)
        else:
            raise AssertionError(
                f"有 worker 线程收不掉（{thread.name}）—— 它会抢后续用例的队列，"
                "必须先查清是谁起的、为什么退出不了"
            )
    with nps._publish_queue_lock:
        nps._publish_worker = None
        nps._publish_adapter_factories.clear()
    nps._publish_worker_stop.clear()
    nps._publish_wakeup.clear()
    return stopped


@pytest.fixture()
def clean_publish_globals(monkeypatch):
    """收干净 worker 线程 + 工厂缓存。

    ⚠️ **显式请求 `monkeypatch`** 是为了 teardown 顺序：请求它 ⇒ 它先 setup、
    后 teardown ⇒ 本夹具的收尾跑在「`_session_factory` 恢复成真库」**之前**。
    """
    _quiesce_named_publish_workers()
    spawned: list[threading.Thread] = []
    try:
        yield spawned
    finally:
        _quiesce_named_publish_workers()


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# --------------------------------------------------------------------------- #
# 真库助手：造「账号 + 笔记 + 发布队列项」三件套
# --------------------------------------------------------------------------- #
def _account_id(db_factory) -> int:
    db = db_factory()
    try:
        # ⚠️ FK=ON（2026-10-04）：`platform_accounts.user_id -> users.id` 有外键，
        # 父行必须存在（`db_factory` 按契约**不播种**）。一处补，本文件 9 条用例都过。
        from tests.backend.conftest import seed_owner_rows as _seed

        _seed(db.get_bind(), user_id=1)
        account = PlatformAccount(
            user_id=1, platform="xhs", sub_type="creator", nickname="发布守活店", status="active"
        )
        db.add(account)
        db.commit()
        return int(account.id)
    finally:
        db.close()


def _seed_publish_item(
    db_factory,
    *,
    status: str = "pending",
    note_status: str = "publishing",
    retry_count: int = 0,
    max_retries: int = 3,
) -> tuple[int, int]:
    """落一条真的发布队列项，返回 `(task_id, note_pk)`。

    笔记状态默认 `publishing` —— 那正是「入队后进程退出」留在库里的形态
    （`_begin_publish` 先置 publishing 再入队，§31.5 步 1）。
    """
    account_id = _account_id(db_factory)
    db = db_factory()
    try:
        note = Note(
            user_id=1,
            platform_account_id=account_id,
            platform="xhs",
            note_id=f"pub-{uuid4().hex[:8]}",
            title="发布守活笔记",
            note_type="local_generated",
            publish_status="pending",
        )
        db.add(note)
        db.commit()
        if note_status != "pending":
            set_note_publish_status(note, note_status)
            db.commit()
        task = Task(
            user_id=1,
            platform="xhs",
            task_type=nqs.TASK_TYPE_PUBLISH_ITEM,
            status=status,
            progress=0,
            retry_count=retry_count,
            max_retries=max_retries,
            payload={"note_pk": note.id, "account_id": account_id},
        )
        db.add(task)
        db.commit()
        return task.id, note.id
    finally:
        db.close()


def _task_row(db_factory, task_id: int) -> dict:
    db = db_factory()
    try:
        row = db.get(Task, task_id)
        if row is None:
            return {"status": None, "payload": {}, "retry_count": None}
        return {
            "status": row.status,
            "payload": dict(row.payload or {}),
            "retry_count": row.retry_count,
        }
    finally:
        db.close()


def _task_status(db_factory, task_id: int) -> str | None:
    return _task_row(db_factory, task_id)["status"]  # type: ignore[return-value]


def _note_status(db_factory, note_pk: int) -> str | None:
    db = db_factory()
    try:
        note = db.get(Note, note_pk)
        return str(note.publish_status) if note is not None else None
    finally:
        db.close()


def _patch_publish_deps(monkeypatch, db_factory, *, executed: list | None = None) -> list:
    """把 worker 的四个外部依赖打桩：库 / 凭据 / 工厂 / 执行链路。

    返回 `executed` 列表（每次 `_execute_publish` 记一条 `(note_pk, adapter)`）。
    """
    executed = executed if executed is not None else []
    monkeypatch.setattr(nps, "_session_factory", db_factory)
    monkeypatch.setattr(
        nps, "qianfan_cookies_for_account", lambda _db, _account_id: "ARK-COOKIES-现取"
    )
    monkeypatch.setattr(nps, "_default_publish_adapter_factory", lambda: _FakeAdapter)
    monkeypatch.setattr(nps, "PUBLISH_IDLE_POLL_SECONDS", 0.1)

    def _execute(db, note, adapter):
        executed.append((note.id, adapter))
        set_note_publish_status(note, PUBLISH_STATUS_PUBLISHED)
        db.commit()
        return {"ok": True, "note_pk": note.id, "note_id": note.note_id}

    monkeypatch.setattr(nps, "_execute_publish", _execute)
    return executed


class _ClosingBoom:
    """包一层**真** Session：`close()` 抛 BaseException，专造「篇内 finally 漏网」。

    `_run_one_publish` 的 `finally: db.close()` 在 except **之后**执行，它抛的异常
    会**穿透篇内网**，只能靠 `_publish_worker_loop` 的外层网接。其余方法原样转发
    到真 Session ⇒ 认领 / 记账都是真落库，只有「退出那一刻」是假的。
    """

    def __init__(self, inner: Any, exc: BaseException) -> None:
        self._inner = inner
        self._exc = exc

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def close(self) -> None:
        raise self._exc


# --------------------------------------------------------------------------- #
# 1) 探针：存活判据只能是「进程里有没有活着的同名线程」
# --------------------------------------------------------------------------- #
class _FakeThread:
    """受控线程表替身（`name` + `is_alive()` 两件事就够探针用）。"""

    def __init__(self, name: str, alive: bool) -> None:
        self.name = name
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


def test_probe_only_trusts_a_live_named_thread(monkeypatch):
    """探针 = 「有没有**活着的** publisher worker 线程」；别的线程、挂着的死线程都不算。"""
    monkeypatch.setattr(nps.threading, "enumerate", lambda: [_FakeThread("MainThread", True)])
    assert nps.publish_worker_thread_alive() is False, "别的线程不算 worker"

    monkeypatch.setattr(
        nps.threading,
        "enumerate",
        lambda: [
            _FakeThread("MainThread", True),
            _FakeThread(nps.PUBLISH_WORKER_THREAD_NAME, True),
        ],
    )
    assert nps.publish_worker_thread_alive() is True

    monkeypatch.setattr(
        nps.threading,
        "enumerate",
        lambda: [_FakeThread(nps.PUBLISH_WORKER_THREAD_NAME, False)],
    )
    assert nps.publish_worker_thread_alive() is False, "枚举里挂着的死线程不算活"


def test_thread_name_constants_are_the_ones_the_probes_trust():
    """线程名 / 看门狗收尾必须走常量（对不上 = 探针永假，永不重建）。"""
    import inspect

    spawn = inspect.getsource(nps._spawn_publish_worker)
    assert "PUBLISH_WORKER_THREAD_NAME" in spawn, "线程名必须用常量，不许另写裸字符串"
    assert nps.PUBLISH_WORKER_THREAD_NAME == "note-publish-worker", (
        "线程名是既有契约（真机排查按它 grep / py-spy），改名会失去历史可比性"
    )


# --------------------------------------------------------------------------- #
# 2) 篇内网：SystemExit 也要记 failed，且笔记不许卡在 publishing
# --------------------------------------------------------------------------- #
def test_run_one_publish_survives_system_exit(clean_publish_globals, db_factory, monkeypatch):
    """`_run_one_publish` 对 `BaseException`（SystemExit）记 failed，不放它逃出线程。

    ⚠️ 双重判据（发布特有）：队列项必须落终态（否则永远挂在 `running`、进度卡
    永远转圈）**且**笔记不许停在 `publishing`（那是「在跑」的合法态，卡住就再也
    不会被重跑，也不会出现在任何终态统计里）。
    """
    _patch_publish_deps(monkeypatch, db_factory)
    task_id, note_pk = _seed_publish_item(db_factory, status="running")

    def _boom(_db, _note, _adapter):
        raise SystemExit("SDK/curl_cffi 静默退出（测试替身）")

    monkeypatch.setattr(nps, "_execute_publish", _boom)
    # 不抛 = 网兜住了；结果记 failed（进度卡的失败数如实 +1，不是无声消失）。
    nps._run_one_publish(task_id)

    row = _task_row(db_factory, task_id)
    assert row["status"] == "failed"
    assert row["payload"]["ok"] is False
    assert "SystemExit" in str(row["payload"]["reason"])
    assert row["payload"]["code"] == "PUBLISH_FAILED"
    assert _note_status(db_factory, note_pk) == "failed", "笔记不许卡在 publishing"


def test_run_one_publish_reports_missing_credential_as_failure(
    clean_publish_globals, db_factory, monkeypatch
):
    """凭据拿不到 ⇒ 记账 + 标失败，而不是抛出去（重启后凭据可能已失效，§18.14）。"""
    _patch_publish_deps(monkeypatch, db_factory)
    monkeypatch.setattr(nps, "qianfan_cookies_for_account", lambda _db, _account_id: "")
    task_id, note_pk = _seed_publish_item(db_factory, status="running")

    nps._run_one_publish(task_id)

    row = _task_row(db_factory, task_id)
    assert row["status"] == "failed"
    assert row["payload"]["code"] == "QIANFAN_CREDENTIAL_MISSING"
    assert _note_status(db_factory, note_pk) == "failed"


def test_worker_loop_survives_base_exception_and_keeps_draining(
    clean_publish_globals, db_factory, monkeypatch
):
    """循环外层网：连「篇内网 finally 漏网」的 BaseException 也不许把 worker 带走。

    构造：跑第一条时 `session.close()` 抛 `SystemExit` —— `_run_one_publish` 的
    `finally: db.close()` 在 except **之后**执行，它抛的异常穿透篇内网，只有
    `_publish_worker_loop` 的外层网能接。外层网若降格成 `except Exception`，
    本用例转红（worker 线程死、库里第二条永远停在 pending）。
    """
    executed = _patch_publish_deps(monkeypatch, db_factory)
    task_a, note_a = _seed_publish_item(db_factory, status="pending")
    task_b, note_b = _seed_publish_item(db_factory, status="pending")

    real_factory = db_factory
    calls = {"n": 0}

    def _factory():
        calls["n"] += 1
        inner = real_factory()
        # 第 1 次是 `_claim_one_publish_item` 的认领 Session（要能正常关）；
        # 第 2 次是跑第一条的 Session ⇒ 在它 close 时漏网。
        if calls["n"] == 2:
            return _ClosingBoom(inner, SystemExit("finally 漏网（测试替身）"))
        return inner

    monkeypatch.setattr(nps, "_session_factory", _factory)

    worker = threading.Thread(
        target=nps._publish_worker_loop, name=nps.PUBLISH_WORKER_THREAD_NAME, daemon=True
    )
    worker.start()
    clean_publish_globals.append(worker)
    nps._publish_wakeup.set()

    assert _wait_until(
        lambda: _task_status(db_factory, task_a) == "done"
        and _task_status(db_factory, task_b) == "done",
        timeout=10,
    )
    assert worker.is_alive() is True, "连 finally 漏网的 BaseException 都不许把 worker 带走"
    assert [note_pk for note_pk, _adapter in executed] == [note_a, note_b], (
        "漏网那篇之后，后续篇必须继续跑"
    )


# --------------------------------------------------------------------------- #
# 3) 队列持久化：库里那条 pending 行就是全部交接物（重启续跑）
# --------------------------------------------------------------------------- #
def test_pending_item_left_by_a_previous_process_is_picked_up(
    clean_publish_globals, db_factory, monkeypatch
):
    """**重启续跑（用户口径「应用退出再次启动后可继续执行」）的发布版最小形态。**

    库里留着上次进程没跑完的 `pending` 发布项 ⇒ 新起的 worker **不依赖任何内存态**
    就把它跑掉，并且：

    * adapter 是**现场重建**的（不是从内存队列取出来的旧实例）；
    * 凭据按**库里当前有效**的 ark 令牌现取（§18.14：一个账号一个平台一个令牌）。

    同时验证**兜底巡检**（`PUBLISH_IDLE_POLL_SECONDS`）：测试**不**手动 `set` 唤醒，
    只靠 worker 自己的超时轮询捞到它 —— 唤醒信号丢失也不会永久停摆（DEF-048）。
    """
    executed = _patch_publish_deps(monkeypatch, db_factory)
    task_id, note_pk = _seed_publish_item(db_factory, status="pending")
    _FakeAdapter.made = []

    worker = threading.Thread(
        target=nps._publish_worker_loop, name=nps.PUBLISH_WORKER_THREAD_NAME, daemon=True
    )
    worker.start()
    clean_publish_globals.append(worker)
    # 刻意不 set 唤醒：靠 wait(timeout=0.1) 兜底。

    assert _wait_until(lambda: _task_status(db_factory, task_id) == "done", timeout=10)
    assert [pk for pk, _a in executed] == [note_pk]
    assert _note_status(db_factory, note_pk) == "published"
    assert _FakeAdapter.made == ["ARK-COOKIES-现取"], (
        "adapter 必须按库里当前凭据现场重建（不是复用内存里的旧实例）"
    )


def test_enqueue_caches_the_factory_for_the_worker(
    clean_publish_globals, db_factory, monkeypatch
):
    """入队时缓存下来的 adapter 工厂，就是 worker 重建 adapter 用的那个。

    这是**测试环境能用假适配器**的机制（端点的工厂是 FastAPI 依赖注入出来的，
    worker 线程拿不到注入层）；生产环境端点传的是千帆真类，语义一致。
    """

    class _Injected:
        made: list[str] = []

        def __init__(self, cookies: str) -> None:
            self.cookies = cookies
            _Injected.made.append(cookies)

    executed = []
    _patch_publish_deps(monkeypatch, db_factory, executed=executed)
    # 先入队（走真实的 start_publish 路径 ⇒ 缓存工厂），再让 worker 跑。
    db = db_factory()
    try:
        note = Note(
            user_id=1,
            platform_account_id=_account_id(db_factory),
            platform="xhs",
            note_id=f"pub-{uuid4().hex[:8]}",
            title="缓存工厂",
            note_type="local_generated",
            publish_status="pending",
        )
        db.add(note)
        db.commit()
        # 直接调 `_enqueue_publish`（绕开门禁：门禁与适配器无关，本用例只盯缓存）
        nps._enqueue_publish(
            db,
            user_id=1,
            note=note,
            adapter_factory=_Injected,
            source=nqs.SOURCE_NOTE_PUBLISH_SINGLE,
        )
        db.commit()
        row = db.scalars(
            select(Task).where(Task.task_type == nqs.TASK_TYPE_PUBLISH_ITEM)
        ).first()
        assert row is not None
        with nps._publish_queue_lock:
            assert nps._publish_adapter_factories.get(int(row.id)) is _Injected
    finally:
        db.close()

    nps.ensure_publish_worker()
    assert _wait_until(lambda: _task_status(db_factory, int(row.id)) == "done", timeout=10)
    assert isinstance(executed[0][1], _Injected), "worker 必须用入队缓存的工厂重建 adapter"
    with nps._publish_queue_lock:
        assert int(row.id) not in nps._publish_adapter_factories, "跑完要清缓存，不许无限增长"


# --------------------------------------------------------------------------- #
# 4) 看门狗：线程死了要重建，且卡住的那条复位（不丢篇）
# --------------------------------------------------------------------------- #
def test_revive_resets_stuck_item_and_spawns_fresh_worker(
    clean_publish_globals, db_factory, monkeypatch
):
    """`_revive_publish_worker`：复位卡在 `running` 的行 + 起新 worker。

    ⚠️ **必须挡住新 worker 消费队列**，而且用**阻塞桩**而不是「秒返回的替身」：
    `_revive_publish_worker` 会真起 worker 并 `set` 唤醒它，秒返回的替身会让它在
    **断言之前**就把那行吃掉（定向跑侥幸过、`-n auto` 下必红）。桩阻塞在**真停止位**
    `_publish_worker_stop` 上：断言期间库纹丝不动，收尾 `set` 就能放它退出。
    """
    _patch_publish_deps(monkeypatch, db_factory)
    monkeypatch.setattr(
        nps, "_publish_worker_loop", lambda: nps._publish_worker_stop.wait(timeout=10)
    )
    stuck_id, _note_pk = _seed_publish_item(db_factory, status="running", retry_count=1)

    # 一条真死了的线程对象（模块变量会骗人，is_alive 不会）。
    dead = threading.Thread(
        target=lambda: None, name=nps.PUBLISH_WORKER_THREAD_NAME, daemon=True
    )
    dead.start()
    dead.join(timeout=5)
    nps._publish_worker = dead

    nps._revive_publish_worker()

    row = _task_row(db_factory, stuck_id)
    assert row["status"] == "pending", "卡在 running 的那条必须复位成 pending（不丢篇）"
    assert row["retry_count"] == 2, "复位同时计入一次重试（用尽后才会转 failed）"
    assert nps._publish_worker is not None and nps._publish_worker.is_alive()


def test_revive_gives_up_after_retries_exhausted(clean_publish_globals, db_factory, monkeypatch):
    """重试用尽的中断行转 `failed` + `interrupted`（不无限重建：毒行不该拖死启动）。"""
    _patch_publish_deps(monkeypatch, db_factory)
    monkeypatch.setattr(
        nps, "_publish_worker_loop", lambda: nps._publish_worker_stop.wait(timeout=10)
    )
    stuck_id, _note_pk = _seed_publish_item(
        db_factory, status="running", retry_count=3, max_retries=3
    )

    nps._revive_publish_worker()

    row = _task_row(db_factory, stuck_id)
    assert row["status"] == "failed"
    assert row["payload"]["code"] == "INTERRUPTED"


def test_watchdog_revives_dead_worker_and_survives_revive_failures(
    clean_publish_globals, monkeypatch
):
    """看门狗循环：探针报死 ⇒ 重建；重建连续失败也不许把自己带走（指数退避重试）。"""
    monkeypatch.setattr(nps, "publish_worker_thread_alive", lambda: False)
    stamps: list[float] = []

    def _flaky_revive():
        stamps.append(time.monotonic())
        if len(stamps) < 2:
            raise RuntimeError("重建失败（测试替身：库临时不可用）")

    monkeypatch.setattr(nps, "_revive_publish_worker", _flaky_revive)

    interval = 0.2
    thread = nps.start_publish_worker_watchdog(interval=interval)
    clean_publish_globals.append(thread)
    try:
        assert _wait_until(lambda: len(stamps) >= 3, timeout=5), "失败后要退避重试，不能停"
        assert thread.is_alive() is True, "看门狗自己绝不被一次失败带走（DEF-067 同规）"
        gap = stamps[1] - stamps[0]
        assert gap >= interval * 1.5, f"失败后必须指数退避（实测间隔 {gap:.2f}s，热循环会≈0）"
    finally:
        nps.stop_publish_worker_watchdog()
    assert _wait_until(lambda: not thread.is_alive(), timeout=5), "stop 必须能收掉看门狗"


def test_watchdog_stays_quiet_while_worker_is_alive(clean_publish_globals, monkeypatch):
    """worker 活着 ⇒ 看门狗什么都不做（不重建、不打扰）。"""
    monkeypatch.setattr(nps, "publish_worker_thread_alive", lambda: True)
    revived = {"n": 0}
    monkeypatch.setattr(
        nps, "_revive_publish_worker", lambda: revived.__setitem__("n", revived["n"] + 1)
    )

    thread = nps.start_publish_worker_watchdog(interval=0.05)
    clean_publish_globals.append(thread)
    try:
        time.sleep(0.3)
        assert revived["n"] == 0
        assert thread.is_alive() is True
    finally:
        nps.stop_publish_worker_watchdog()


def test_stop_publish_worker_stops_claiming_and_exits(clean_publish_globals, db_factory, monkeypatch):
    """`stop_publish_worker`：置位后**不再认领新任务**并退出（测试 / 收尾专用出口）。

    ⚠️ 「不丢正在排队的篇」现在**由库保证**（未跑的项是 `pending` 行，下次启动接着
    跑）⇒ 判据是「停止位生效期间不许认领」+「线程立即退出」+「那行还在库里」。
    """
    _patch_publish_deps(monkeypatch, db_factory)
    claimed: list[int] = []
    monkeypatch.setattr(nps, "_run_one_publish", lambda task_id: claimed.append(task_id))
    task_id, _note_pk = _seed_publish_item(db_factory, status="pending")

    nps.stop_publish_worker(timeout=5)  # 先置停止位：之后起的 worker 不该认领任何东西
    worker = threading.Thread(
        target=nps._publish_worker_loop, name=nps.PUBLISH_WORKER_THREAD_NAME, daemon=True
    )
    worker.start()
    clean_publish_globals.append(worker)

    assert _wait_until(lambda: not worker.is_alive(), timeout=5), "停止位 + 唤醒后必须退出"
    assert claimed == [], "停止位生效期间不许认领新任务"
    assert _task_status(db_factory, task_id) == "pending", "没被吃掉：留在库里等下次启动"


# --------------------------------------------------------------------------- #
# 5) 发布快照的形状契约（2026-09-26 常驻化：只有队列实时态，没有计数字段）
# --------------------------------------------------------------------------- #
def test_publish_snapshot_shape_has_no_count_fields(db_factory):
    """发布快照的键**只能**是 `running/queued/total` + **`today`**（当日三数）。

    2026-09-26 常驻化（用户口径「不需要队列批次，队列是常驻」）：后端只描述队列
    实时态 + 近期终态 ⇒ 响应里再带 `published` / `failed` 计数只会把「近期窗口」
    说成「本轮」。底座通用形状的 `synced` / 同步独有的 `finished` 更不许漏出来
    （漏一个 = 进度卡读到 undefined 恒 0，而后端不报错）。

    ⚠️ 2026-09-26 当日口径（用户口径「只统计当日发布或同步的数据，在卡片右下角小字
    展示仅当日数据，**统计队列记录表中的数据**」）：卡面三数改由 `today`
    （= 当日 `tasks` 行现算的 `ok/failed/pending`）承担。**被取代的原句**（保留，
    不静默删除）：「assert set(snap) == {"running", "queued", "total"}」——
    「三数由前端按自己入队的篇累计」这一版已被当日口径取代。
    """
    db = db_factory()
    try:
        snap = nps.publish_queue_snapshot(db, 1)
    finally:
        db.close()
    assert set(snap) == {"running", "queued", "total", "today"}
    assert set(snap["today"]) == {"date", "ok", "failed", "pending"}, "当日三数就这四个键"
    assert "synced" not in snap and "published" not in snap and "failed" not in snap
    assert "finished" not in snap, "finished 是同步契约的一部分，不在发布契约里"


def test_publish_snapshot_is_user_scoped(db_factory):
    """发布快照只回**当前用户**的队列项（多租户隔离，DEF-066 同规）。"""
    db = db_factory()
    try:
        # ⚠️ 跨租户行也要有真实 owner（FK=ON，2026-10-04）。
        from tests.backend.conftest import seed_owner_rows as _seed

        _seed(db.get_bind(), user_id=999)
        other = Task(
            user_id=999,
            platform="xhs",
            task_type=nqs.TASK_TYPE_PUBLISH_ITEM,
            status="running",
            progress=0,
            payload={"note_pk": 1},
        )
        db.add(other)
        db.commit()
        other_id = int(other.id)
    finally:
        db.close()

    db = db_factory()
    try:
        snap = nps.publish_queue_snapshot(db, 1)
        assert snap["total"] == 0, "别人家的队列项对当前用户不可见"
        assert snap["running"] is None and snap["queued"] == []
    finally:
        db.close()

    db = db_factory()
    try:
        assert db.get(Task, other_id) is not None, "只读不删：快照不许有副作用"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 6) 接线守卫：实现存在 ≠ 接上了（AST 取 lifespan 里实际被调用的函数名）
# --------------------------------------------------------------------------- #
def test_lifespan_wires_the_publish_worker_watchdog_and_recovery():
    """`main.py::lifespan` 必须真的起停发布 worker 看门狗，并做启动恢复。

    三层缺一不可：
    * 起看门狗 ⇒ 运行期线程死了能自愈；
    * 停看门狗 ⇒ 进程收尾不留巡检线程；
    * `recover_interrupted` + `recover_publishing_notes` ⇒ **重启后**把上次进程
      留下的 `running` 项复位、把卡在 `publishing` 的笔记复位（续跑的前置）。
    """
    source = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    lifespan = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "lifespan"
    )
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(lifespan)
        if isinstance(node, ast.Call)
    }
    assert "start_publish_worker_watchdog" in called, "lifespan 没起发布 worker 看门狗"
    assert "stop_publish_worker_watchdog" in called, "lifespan 没收停发布 worker 看门狗"
    assert "recover_interrupted" in called, "lifespan 没做队列中断行复位（重启续跑的前提）"
    assert "recover_publishing_notes" in called, "lifespan 没复位卡在 publishing 的笔记"
