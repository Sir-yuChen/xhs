"""批量同步 worker 守活（DEF-069）+ 队列持久化（设计 13 §35.9）：2026-09-25。

## 缺陷现场（本机实测，不是推演）

内容库批量同步 19 篇，进度卡走到「成功 8 / 失败 0 / 待同步 11」后**再也不动**。
排查证据链：

* `py-spy dump --pid <uvicorn>`：进程里**没有** `note-batch-sync-worker` 线程
  （其余 MainThread / APScheduler / scheduler-watchdog / ThreadPool 全在）；
* `app.log`：17:36 之后**零 traceback、零 ERROR** —— 普通异常会被
  `_run_one_sync` 抓住并记 failed（失败数会 +1），日志也会留 warning；
* DB：最后一篇 `hydrated_at` 停在 17:37:30，之后 3.5 分钟无新写入；
* SQLite 写锁无竞争（`BEGIN IMMEDIATE` 0.03s 拿到）⇒ 不是卡在事务上；
* WAL 仍在更新但来源是 scheduler（`system.due_publish` 每分钟跑）⇒ 不是同步在写。

结论：worker 线程**无声死亡**（最可能是 `except Exception` 够不着的
`BaseException` 如 `SystemExit` —— threading 对它静默，不打 traceback），
队列剩余 11 篇永久停摆。与 **DEF-067**（APScheduler 主循环静默停摆）同型：
**单线程组件没有自愈，存活判据只能是线程在不在**。

## 三条修法各自守什么

1. **篇内网升格**：`_run_one_sync` 的 `except Exception` → `except BaseException`
   （`SystemExit` 也记 failed，不死线程）；
2. **循环外再包一层**：`_sync_worker_loop` 对 `_run_one_sync` 包
   `except BaseException`（漏网路径也不死）；
3. **看门狗重建**：`sync_worker_thread_alive()` 进程级探针 + 30 秒巡检，
   死了就重建 worker，并把它卡在 `running` 的那条**复位成 `pending`**（不丢篇）。

## 队列持久化（同日后续，设计 13 §35.9）

队列项的**唯一真相源是 `tasks` 表**（`note_queue_service`），内存里只剩一个唤醒
信号。⇒ 三条判据跟着换了口径，本文件逐条对准：

* 「丢不丢篇」不再看内存 deque，看库里那条 `pending` 行的死活；
* 「卡住的篇」不再重入内存队列，而是把 `running` 行 `recover_interrupted` 复位；
* 「停不停得下来」不再靠排空队列（未跑的项留在库里、下次启动接着跑），
  停止位只负责**别再认领新任务**。

## 写「让线程死掉」这类用例的三个坑（DEF-067 同款，本轮照样踩到）

1. **死法必须是真的**：不能 `_sync_worker = Thread()` 假造死亡 —— 真线程仍活在
   `threading.enumerate()` 里，探针照旧报「活着」。要真死就起一个**自控同名
   线程**再 `join()`（探针用例），或让代码真抛 `SystemExit`（篇内网用例）。
2. **收尾必须按线程枚举收**：`_quiesce_named_workers()` 把进程里**所有**活着的同名
   worker 收掉，不能只信 `_sync_worker` —— 本文件有用例直接
   `Thread(target=nss._sync_worker_loop)` 起真 worker（模块变量是 None），且
   xdist 同一 worker 会连跑多个文件、`start_batch_sync` 起的真 worker **一条都不退出**。
   2026-09-25 全量实踩：残留 worker 被本文件的 `_sync_wakeup.set()` 唤醒去**抢队列**，
   断言看到 `_sync_running == (10, 555)`（正是被抢走的队首）。
3. **收尾夹具必须显式请求 `monkeypatch`**（持久化后新增的坑）：worker 自建 Session
   走模块级 `_session_factory`（用例 patch 成临时库）。夹具不请求 `monkeypatch`
   的话，`monkeypatch` 会**先**恢复、worker 还活着 ⇒ 它拿着**真库**去认领测试塞的
   假行，那是数据隔离事故，不是测试噪声。
"""
from __future__ import annotations

import ast
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from backend.app.models import Note, PlatformAccount, Task
from backend.app.services import note_queue_service as nqs
from backend.app.services import note_sync_service as nss

ROOT = Path(__file__).resolve().parents[2]


def _quiesce_named_workers(*, timeout: float = 5.0) -> list[str]:
    """把进程里**所有活着的** `SYNC_WORKER_THREAD_NAME` 线程收掉。

    按线程枚举收而不是只信 `_sync_worker` 的两条理由见本文件 docstring 第 2 条坑。

    ⚠️ **先置停止位**（=「别再认领新任务」），再 join。持久化后 worker 的活来自
    `tasks` 表，不收干净的话残留 worker 会拿**当前**的 `_session_factory` 去认领
    别人的行 —— 夹具在场时是「抢测试的行」，夹具不在场时是「拿真库跑测试数据」。
    """
    nss._sync_worker_stop.set()
    for stop in list(nss._sync_worker_stops.values()):
        stop.set()
    nss._sync_wakeup.set()
    stopped: list[str] = []
    for thread in list(threading.enumerate()):
        # ⚠️ 2026-09-27 池化（设计 13 §38.3）：判据必须是**前缀**匹配 ——
        # 写死 `!= SYNC_WORKER_THREAD_NAME` 会漏掉 `…#2`..`…#8`，
        # 残留的那几条会继续抢后续用例（甚至真库）的队列。
        if not nss.thread_name_matches_sync_worker(thread.name) or not thread.is_alive():
            continue
        thread.join(timeout=timeout)
        if not thread.is_alive():
            stopped.append(thread.name)
        else:
            raise AssertionError(
                f"有 worker 线程收不掉（{thread.name}）—— 它会抢后续用例的队列，"
                "必须先查清是谁起的、为什么退出不了"
            )
    with nss._sync_queue_lock:
        nss._sync_worker = None
        nss._sync_workers.clear()
        nss._sync_worker_stops.clear()
        nss._sync_busy_accounts.clear()
        nss._sync_held_items.clear()
    nss._sync_worker_stop.clear()
    nss._sync_wakeup.clear()
    return stopped


# --------------------------------------------------------------------------- #
# 夹具：收干净进程里的 worker 线程（不污染同进程其它测试文件）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def clean_sync_globals(monkeypatch):
    """收干净 worker 线程。

    ⚠️ **显式请求 `monkeypatch`** 是为了 teardown 顺序：请求它 ⇒ 它先 setup、
    后 teardown ⇒ 本夹具的收尾跑在「`_session_factory` 恢复成真库」**之前**
    （docstring 第 3 条坑）。
    """
    _quiesce_named_workers()
    # 本文件的用例关心的是**守活**，不是「池子多大」：把并行度钉成 1（改造前的单 worker
    # 语义）⇒ 断言不受运行时配置影响，也不去读配置库。池大小 / 账号互斥另有专门用例
    # （`test_note_sync_parallel.py`）。
    monkeypatch.setattr(nss, "sync_parallelism", lambda: 1)
    spawned: list[threading.Thread] = []
    try:
        yield spawned
    finally:
        _quiesce_named_workers()


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# --------------------------------------------------------------------------- #
# 真库助手：造「账号 + 笔记 + 队列项」三件套（队列持久化后一切断言都落在库上）
# --------------------------------------------------------------------------- #
def _seed_item(
    db_factory,
    *,
    status: str = "pending",
    retry_count: int = 0,
    max_retries: int = 3,
) -> tuple[int, int]:
    """落一条真的队列项，返回 `(task_id, note_pk)`。"""
    db = db_factory()
    try:
        account = PlatformAccount(
            user_id=1, platform="xhs", sub_type="creator", nickname="守活店", status="active"
        )
        db.add(account)
        db.commit()
        note = Note(
            user_id=1,
            platform_account_id=account.id,
            platform="xhs",
            note_id=f"wd-{uuid4().hex[:8]}",
            title="守活笔记",
            note_type="synced",
            raw_json={"note_url": "https://www.xiaohongshu.com/explore/abc"},
        )
        db.add(note)
        db.commit()
        task = Task(
            user_id=1,
            platform="xhs",
            task_type=nqs.TASK_TYPE_SYNC_ITEM,
            status=status,
            progress=0,
            retry_count=retry_count,
            max_retries=max_retries,
            payload={"note_pk": note.id, "account_id": account.id},
        )
        db.add(task)
        db.commit()
        return task.id, note.id
    finally:
        db.close()


def _task_row(db_factory, task_id: int) -> dict:
    """读一条队列项的当前状态（每次新 Session ⇒ 不吃缓存）。"""
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


class _ClosingBoom:
    """包一层**真** Session：`close()` 抛 BaseException，专造「篇内网 finally 漏网」。

    `_run_one_sync` 的 `finally: db.close()` 在 except **之后**执行，它抛的异常会
    **穿透篇内网**，只能靠 `_sync_worker_loop` 的外层网接。其余方法原样转发到真
    Session ⇒ 认领 / 记账都是真落库，只有「退出那一刻」是假的。
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
    """探针 = 「有没有**活着的** worker 线程」；别的线程、枚举里挂着的死线程都不算。

    用受控线程表断言，不受同进程其它用例残留线程影响（xdist 同一 worker 会连跑
    多个文件）；「同名但已死」这一态正是缺陷现场 —— 线程带 `SystemExit` 死后
    若还报活，看门狗永不重建（DEF-067 的 `running` 骗人同型）。
    """
    monkeypatch.setattr(nss.threading, "enumerate", lambda: [_FakeThread("MainThread", True)])
    assert nss.sync_worker_thread_alive() is False, "别的线程不算 worker"

    monkeypatch.setattr(
        nss.threading,
        "enumerate",
        lambda: [_FakeThread("MainThread", True), _FakeThread(nss.SYNC_WORKER_THREAD_NAME, True)],
    )
    assert nss.sync_worker_thread_alive() is True

    monkeypatch.setattr(
        nss.threading, "enumerate", lambda: [_FakeThread(nss.SYNC_WORKER_THREAD_NAME, False)]
    )
    assert nss.sync_worker_thread_alive() is False, "枚举里挂着的死线程不算活（is_alive 判据）"


def test_worker_thread_name_is_the_one_the_probe_trusts():
    """线程名必须走探针认的那个常量（对不上 = 探针永假/永真）。

    2026-09-27 池化（设计 13 §38.3）后名字多了一层：**唯一实现**是
    `sync_worker_thread_name(index)`（它读 `SYNC_WORKER_THREAD_NAME` 当基名，
    第 2..N 条加 `#序号`），`_spawn_sync_worker` **委托**给它 —— 所以断言要落在
    **两处**：常量在唯一实现里、起线程处只能通过它取名字（不许自己拼裸字符串）。

    ⚠️ 只断言「`_spawn_sync_worker` 源码里出现过常量名」在池化后会退化成**注释级
    守卫**（写句注释就过），所以这里额外钉住「委托」这一事实。
    """
    import inspect

    helper = inspect.getsource(nss.sync_worker_thread_name)
    assert "SYNC_WORKER_THREAD_NAME" in helper, "基名必须用常量，不许另写裸字符串"

    source = inspect.getsource(nss._spawn_sync_worker)
    assert "sync_worker_thread_name(" in source, "起线程处必须委托给唯一实现取名"

    # 名字本身：第 1 条保持原名（既有探针 / 测试 / 日志检索都认它），其余带序号
    assert nss.sync_worker_thread_name(0) == nss.SYNC_WORKER_THREAD_NAME
    assert nss.sync_worker_thread_name(1) == f"{nss.SYNC_WORKER_THREAD_NAME}#2"
    # 探针按**前缀**认：池里任何一条都算「有能干活的人」
    assert nss.thread_name_matches_sync_worker(nss.sync_worker_thread_name(0)) is True
    assert nss.thread_name_matches_sync_worker(nss.sync_worker_thread_name(7)) is True
    assert nss.thread_name_matches_sync_worker("note-publish-worker") is False
    # 前缀匹配不许退化成「包含」：别的队列名里出现同样的子串不算本池
    assert nss.thread_name_matches_sync_worker("x-note-batch-sync-worker") is False


# --------------------------------------------------------------------------- #
# 2) 篇内网：SystemExit 也要记 failed，不许把线程/队列带走
# --------------------------------------------------------------------------- #
def test_run_one_sync_survives_system_exit(clean_sync_globals, db_factory, monkeypatch):
    """`_run_one_sync` 对 `BaseException`（SystemExit）记 failed 而不是放它逃出线程。

    ⚠️ 断言落在**库**上：安全网不条件性（DEF-048）—— 任何异常路径都必须把这条
    写成终态，否则它会永远挂在 `running` 上（快照里「正在同步」永远转圈、
    前端按 `pending != 0` 无限轮询）。
    """
    monkeypatch.setattr(nss, "_session_factory", db_factory)
    task_id, _note_pk = _seed_item(db_factory, status="running")

    def _boom(_db, _note):
        raise SystemExit("SDK/curl_cffi 静默退出（测试替身）")

    monkeypatch.setattr(nss, "sync_note_from_platform", _boom)
    # 不抛 = 网兜住了；结果记 failed（进度卡的失败数如实 +1，不是无声消失）。
    nss._run_one_sync(task_id)

    row = _task_row(db_factory, task_id)
    assert row["status"] == "failed"
    assert row["payload"]["ok"] is False
    assert "SystemExit" in str(row["payload"]["reason"])
    assert row["payload"]["code"] == "SYNC_FAILED"


def test_worker_loop_survives_base_exception_and_keeps_draining(
    clean_sync_globals, db_factory, monkeypatch
):
    """循环外层网：连「篇内网 finally 漏网」的 BaseException 也不许把 worker 带走。

    构造：跑第一条时 `session.close()` 抛 `SystemExit` —— `_run_one_sync` 的
    `finally: db.close()` 在 except **之后**执行，它抛的异常穿透篇内网，只有
    `_sync_worker_loop` 的外层网能接。外层网若降格成 `except Exception`，
    本用例转红（worker 线程死、后续篇不跑、库里第二条永远停在 pending）。
    """
    task_a, note_a = _seed_item(db_factory, status="pending")
    task_b, note_b = _seed_item(db_factory, status="pending")

    real_factory = db_factory
    calls = {"n": 0}

    def _factory():
        calls["n"] += 1
        inner = real_factory()
        # 第 1 次是 `_claim_one_sync_item` 的认领 Session（要能正常关）；
        # 第 2 次是跑第一条的 Session ⇒ 在它 close 时漏网。
        if calls["n"] == 2:
            return _ClosingBoom(inner, SystemExit("finally 漏网（测试替身）"))
        return inner

    monkeypatch.setattr(nss, "_session_factory", _factory)
    synced: list[int] = []

    def _sync(_db, note):
        synced.append(note.id)
        return {"status": "ok"}

    monkeypatch.setattr(nss, "sync_note_from_platform", _sync)
    monkeypatch.setattr(nss, "SYNC_IDLE_POLL_SECONDS", 0.1)

    worker = threading.Thread(
        target=nss._sync_worker_loop, name=nss.SYNC_WORKER_THREAD_NAME, daemon=True
    )
    worker.start()
    clean_sync_globals.append(worker)
    nss._sync_wakeup.set()

    # 两条都跑到终态 ⇒ 外层网兜住了 finally 漏网、循环继续（第一条在 close 抛之前
    # 已把结果写进库）。
    assert _wait_until(
        lambda: _task_status(db_factory, task_a) == "done"
        and _task_status(db_factory, task_b) == "done",
        timeout=10,
    )
    assert worker.is_alive() is True, "连 finally 漏网的 BaseException 都不许把 worker 线程带走"
    assert synced == [note_a, note_b], "漏网那篇之后，后续篇必须继续跑"


# --------------------------------------------------------------------------- #
# 3) 队列持久化：库里那条 pending 行就是全部交接物（重启续跑）
# --------------------------------------------------------------------------- #
def test_pending_item_left_by_a_previous_process_is_picked_up(
    clean_sync_globals, db_factory, monkeypatch
):
    """**重启续跑（用户口径「应用退出再次启动后可继续执行」）的最小可验证形态。**

    库里留着上次进程没跑完的 `pending` 项 ⇒ 新起的 worker **不依赖任何内存态**
    就把它跑掉。这条守住的是「队列项落在 `tasks` 表」这件事本身：只要真相源回到
    内存 deque，本用例立刻转红（没有任何入队动作，worker 永远看不到活）。

    同时验证**兜底巡检**（`SYNC_IDLE_POLL_SECONDS`）：测试**不**手动 `set` 唤醒，
    只靠 worker 自己的超时轮询捞到它 —— 唤醒信号丢失也不会永久停摆（DEF-048）。
    """
    monkeypatch.setattr(nss, "_session_factory", db_factory)
    monkeypatch.setattr(nss, "SYNC_IDLE_POLL_SECONDS", 0.1)
    task_id, note_pk = _seed_item(db_factory, status="pending")
    ran: list[int] = []

    def _sync(_db, note):
        ran.append(note.id)
        return {"status": "ok"}

    monkeypatch.setattr(nss, "sync_note_from_platform", _sync)

    worker = threading.Thread(
        target=nss._sync_worker_loop, name=nss.SYNC_WORKER_THREAD_NAME, daemon=True
    )
    worker.start()
    clean_sync_globals.append(worker)

    assert _wait_until(lambda: _task_status(db_factory, task_id) == "done", timeout=10)
    assert ran == [note_pk]


# --------------------------------------------------------------------------- #
# 4) 看门狗：线程死了要重建，且卡住的那条复位（不丢篇）
# --------------------------------------------------------------------------- #
def test_revive_resets_stuck_item_and_spawns_fresh_worker(clean_sync_globals, db_factory, monkeypatch):
    """`_revive_sync_worker`：复位卡在 `running` 的行 + 起新 worker（判据用 is_alive）。

    ⚠️ **必须挡住新 worker 消费队列**，而且要用**阻塞桩**而不是「秒返回的替身」：
    `_revive_sync_worker` 会真起 worker 并 `set` 唤醒它，秒返回的替身会让它在
    **断言之前**就把那行吃掉（本轮定向跑侥幸过、全量 `-n auto` 下必红 —— 竞态用例
    的典型长相）。桩改阻塞在**真停止位** `_sync_worker_stop` 上：断言期间库纹丝
    不动，收尾 `stop` 一 `set` 就能放它退出，不会留活线程。同时它也不拿测试行
    走真同步链路（那会打平台）。
    """
    monkeypatch.setattr(nss, "_session_factory", db_factory)
    monkeypatch.setattr(nss, "_sync_worker_loop", lambda: nss._sync_worker_stop.wait(timeout=10))
    stuck_id, _note_pk = _seed_item(db_factory, status="running", retry_count=1)

    # 一条真死了的线程对象（模块变量会骗人，is_alive 不会）。
    dead = threading.Thread(target=lambda: None, name=nss.SYNC_WORKER_THREAD_NAME, daemon=True)
    dead.start()
    dead.join(timeout=5)
    nss._sync_worker = dead

    nss._revive_sync_worker()

    row = _task_row(db_factory, stuck_id)
    assert row["status"] == "pending", "卡在 running 的那条必须复位成 pending（不丢篇）"
    assert row["retry_count"] == 2, "复位同时计入一次重试（用尽后才会转 failed）"
    assert nss._sync_worker is not None and nss._sync_worker.is_alive()


def test_revive_gives_up_after_retries_exhausted(clean_sync_globals, db_factory, monkeypatch):
    """重试用尽的中断行转 `failed` + `interrupted`（不无限重建：毒行不该拖死启动）。"""
    monkeypatch.setattr(nss, "_session_factory", db_factory)
    monkeypatch.setattr(nss, "_sync_worker_loop", lambda: nss._sync_worker_stop.wait(timeout=10))
    stuck_id, _note_pk = _seed_item(db_factory, status="running", retry_count=3, max_retries=3)

    nss._revive_sync_worker()

    row = _task_row(db_factory, stuck_id)
    assert row["status"] == "failed"
    assert row["payload"]["code"] == "INTERRUPTED"


def test_watchdog_revives_dead_worker_and_survives_revive_failures(clean_sync_globals, monkeypatch):
    """看门狗循环：探针报死 ⇒ 重建；重建连续失败也不许把自己带走（指数退避重试）。

    退避断言（间隔）：第一次失败后 backoff 翻倍 ⇒ 第 1、2 次重建的间隔必须
    **≥ 1.5 × interval**（热循环/拿掉退避会 < 0.5 × interval，本断言转红）。
    """
    monkeypatch.setattr(nss, "sync_worker_thread_alive", lambda: False)
    stamps: list[float] = []

    def _flaky_revive():
        stamps.append(time.monotonic())
        if len(stamps) < 2:
            raise RuntimeError("重建失败（测试替身：库临时不可用）")

    monkeypatch.setattr(nss, "_revive_sync_worker", _flaky_revive)

    interval = 0.2
    thread = nss.start_sync_worker_watchdog(interval=interval)
    clean_sync_globals.append(thread)
    try:
        assert _wait_until(lambda: len(stamps) >= 3, timeout=5), "失败后要退避重试，不能停"
        assert thread.is_alive() is True, "看门狗自己绝不被一次失败带走（DEF-067 同规）"
        gap = stamps[1] - stamps[0]
        assert gap >= interval * 1.5, f"失败后必须指数退避（实测间隔 {gap:.2f}s，热循环会≈0）"
    finally:
        nss.stop_sync_worker_watchdog()
    assert _wait_until(lambda: not thread.is_alive(), timeout=5), "stop 必须能收掉看门狗"


def test_watchdog_stays_quiet_while_worker_is_alive(clean_sync_globals, monkeypatch):
    """池满员 ⇒ 看门狗什么都不做（不重建、不打扰）。

    ⚠️ 2026-09-27 池化后判据从「`sync_worker_thread_alive()` 为真」升级为
    **「池里不缺人」**（`sync_worker_pool_deficit() == 0`）—— 死 2 条还剩 1 条时
    前者仍为真，只判它就会漏补。所以本用例钉的是**新判据**（旧断言保留在
    `test_worker_thread_name_is_the_one_the_probe_trusts` 的探针部分）。
    """
    monkeypatch.setattr(nss, "sync_worker_pool_deficit", lambda: 0)
    revived = {"n": 0}
    monkeypatch.setattr(nss, "_revive_sync_worker", lambda: revived.__setitem__("n", revived["n"] + 1))

    thread = nss.start_sync_worker_watchdog(interval=0.05)
    clean_sync_globals.append(thread)
    try:
        time.sleep(0.3)
        assert revived["n"] == 0
        assert thread.is_alive() is True
    finally:
        nss.stop_sync_worker_watchdog()


def test_watchdog_revives_when_the_pool_is_short(clean_sync_globals, monkeypatch):
    """池缺人（死 2 条还剩 1 条那种）⇒ 看门狗必须补 —— 旧判据「有没有活着的」看不见。"""
    monkeypatch.setattr(nss, "sync_worker_pool_deficit", lambda: 2)
    revived = {"n": 0}
    monkeypatch.setattr(nss, "_revive_sync_worker", lambda: revived.__setitem__("n", revived["n"] + 1))

    thread = nss.start_sync_worker_watchdog(interval=0.05)
    clean_sync_globals.append(thread)
    try:
        assert _wait_until(lambda: revived["n"] > 0, timeout=5), "池缺人必须触发补齐"
    finally:
        nss.stop_sync_worker_watchdog()


def test_stop_sync_worker_stops_claiming_and_exits(clean_sync_globals, db_factory, monkeypatch):
    """`stop_sync_worker`：置位后**不再认领新任务**并退出（测试 / 收尾专用出口）。

    ⚠️ 口径与内存版不同：「不丢正在排队的篇」现在**由库保证**（未跑的项是
    `pending` 行，下次启动接着跑），不再靠 worker「排空」⇒ 判据是
    「停止位生效期间不许认领」+「线程立即退出」+「那行还在库里」。
    """
    monkeypatch.setattr(nss, "_session_factory", db_factory)
    claimed: list[int] = []
    monkeypatch.setattr(nss, "_run_one_sync", lambda task_id: claimed.append(task_id))
    task_id, _note_pk = _seed_item(db_factory, status="pending")

    nss.stop_sync_worker(timeout=5)  # 先置停止位：之后起的 worker 不该认领任何东西
    worker = threading.Thread(
        target=nss._sync_worker_loop, name=nss.SYNC_WORKER_THREAD_NAME, daemon=True
    )
    worker.start()
    clean_sync_globals.append(worker)

    assert _wait_until(lambda: not worker.is_alive(), timeout=5), "停止位 + 唤醒后必须退出"
    assert claimed == [], "停止位生效期间不许认领新任务"
    assert _task_status(db_factory, task_id) == "pending", "没被吃掉：留在库里等下次启动"


# --------------------------------------------------------------------------- #
# 5) 接线守卫：实现存在 ≠ 接上了（AST 取 lifespan 里实际被调用的函数名）
# --------------------------------------------------------------------------- #
def test_lifespan_wires_the_sync_worker_watchdog():
    """`main.py::lifespan` 必须真的起停同步 worker 看门狗（防「有实现没接上」）。"""
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
    assert "start_sync_worker_watchdog" in called, "lifespan 没起同步 worker 看门狗"
    assert "stop_sync_worker_watchdog" in called, "lifespan 没收停同步 worker 看门狗"
