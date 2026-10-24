"""调度器守活（DEF-067）：主循环死了要能被看门狗重建（2026-09-25）。

## 缺陷现场（本机实测，不是推演）

16:00:00 六个作业正常跑完 → 16:00:06 APScheduler 主循环在
`base.py::_process_jobs` 的 `jobstore.update_job()` 上抛
`sqlite3.OperationalError: database is locked` → **整个主循环线程死掉**，
之后 **14 分钟一个作业都没跑**（`定时发布补跑` 是 `0 * * * * *`，本该每分钟跑）。

⚠️ 最坑的是**看不出来**：3.11 的 `state` 只在 `shutdown()` 里改，线程死了它还是
`STATE_RUNNING` ⇒ `scheduler.running` 返回 `True`、`/api/schedule` 状态端点照样
报「运行中」。所以存活判据只能是**线程在不在**（:func:`scheduler_thread_alive`）。

## 两条修法各自守什么

1. **治因**：job store 自建引擎不带 `core/database.py` 的 `PRAGMA busy_timeout=5000`
   事件监听，只剩 pysqlite 默认 **5 秒** ⇒ 给足等待时长，把「抛异常」变成「等一会儿」；
2. **治果**：APScheduler 自己**不自愈** ⇒ 看门狗发现线程没了就**重建一台**
   （`ThreadPoolExecutor` 被 `shutdown` 后不可复用，所以只能换新实例，不能重启旧的）。

## 写「让线程死掉」这类用例的两个坑（首版四个用例连环假红，成因都是这两个）

1. **死法必须是真的**：`_process_jobs` 抛未捕获异常 ⇒ 主循环**带异常退出**、`state`
   留在 RUNNING（这就是现场）。**不能**用 `scheduler._thread = Thread()` /
   `_target = None` 假造 —— 真线程仍活在 `threading.enumerate()` 里，线程探针照旧报
   「活着」，看门狗于是什么都不做、断言等到超时。统一走 :func:`_start_dying_scheduler`。
2. **收尾必须用 `shutdown()`**，不能手搓「置 `_state` + 关执行器/job store」：
   `BlockingScheduler._main_loop` 卡在 `self._event.wait()` 上，只有 `shutdown()` 会
   `_event.set()` 唤醒它并 `join()`。手搓那套会**永久漏一条活着的 "APScheduler" 线程**，
   之后同进程内（xdist 同一 worker 会连跑多个文件）所有「线程探针」断言全部失真。
"""

from __future__ import annotations

import ast
import threading
import time
import types
from pathlib import Path

from apscheduler.schedulers.base import BaseScheduler, SchedulerNotRunningError

from backend.app.services import scheduler_service as svc

import pytest




#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[2]


class _Holder:
    """`app.state` 的最小替身（生产就是 `app.state.scheduler`）。"""

    def __init__(self) -> None:
        self.scheduler = None


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _loop_thread(scheduler) -> threading.Thread | None:
    """**这一台**调度器自己的主循环线程（`BackgroundScheduler.start` 写死 "APScheduler"）。

    判「这台死了没有」只认它 —— 不去数进程里有几条同名线程（那种写法会把同 worker
    其它文件残留的线程算进来，正是不该有的耦合）。
    """
    return getattr(scheduler, "_thread", None)


def _loop_is_alive(scheduler) -> bool:
    thread = _loop_thread(scheduler)
    return thread is not None and thread.is_alive()


def _stop_loop(scheduler) -> None:
    """让**这一台**的主循环退出并回收线程。

    必须走 `shutdown()`：它 `_event.set()` 唤醒 `_main_loop` 的 `wait()` 再 `join()`；
    `BackgroundScheduler.shutdown` 还会 `del self._thread`。已关过（state 已是 STOPPED）
    会抛 `SchedulerNotRunningError`，本函数按「已经是想要的状态」吞掉。
    """
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=False)
    except SchedulerNotRunningError:
        pass


def _started_builder():
    """测试替身：等价生产 `start_scheduler()` 的「装配 + 启动」，但走内存 store、不碰真库。

    ⚠️ 别传裸 `svc.build_scheduler` 当 `build=` —— 它**只装配不启动**，重建出来的
    实例没有主循环线程，看门狗下一轮又会判「已死」再重建一台（连环重建 + 断言全错）。
    生产的 `build` 默认值是 `start_scheduler`，是**会启动**的。
    """

    def build():
        scheduler = svc.build_scheduler()
        scheduler.start()
        return scheduler

    return build


def _start_dying_scheduler(monkeypatch):
    """造一台「主循环一启动就抛」的调度器（复现缺陷的真实形态）并等它真的死掉。

    返回时该实例：**线程已死、`running` 仍为 True** —— 与 16:00:06 现场同形。
    调用方照旧用 :func:`_stop_loop` 收尾（对死掉的那台也是安全的）。
    """

    def boom(self):
        raise RuntimeError("jobstore.update_job: database is locked")

    # `monkeypatch.context()` 而不是 `monkeypatch.undo()`：只回滚这里的两处补丁，
    # 不误伤调用方（外层 fixture）已经打上的补丁。
    with monkeypatch.context() as patch:
        patch.setattr(BaseScheduler, "_process_jobs", boom)
        # 这个异常是**故意的**：pytest 的线程异常插件会把它收成
        # PytestUnhandledThreadExceptionWarning（`-W error` 下直接变红），
        # 而本文件断言的是它的**后果**（线程死掉），所以把上报静音。
        patch.setattr(threading, "excepthook", lambda args: None)
        scheduler = svc.build_scheduler()  # 内存 store：本文件不碰真库
        scheduler.start()
        thread = _loop_thread(scheduler)
        assert _wait_until(lambda: not thread.is_alive()), "主循环线程应当已经死掉"
    return scheduler


# --------------------------------------------------------------------------- #
# 治因：job store 的等锁时长
# --------------------------------------------------------------------------- #


def test_jobstore_engine_uses_postgres_safe_connect_options(monkeypatch):
    """job store 自建引擎必须带 PG 侧的连接参数（2026-10-24 随 SQLite 退役改写）。

    ⚠️ 历史坑（2026-10-23 实测）：job store **自建引擎**、不走 `core/database.py`
    的事件监听 —— 曾把 SQLite 专用的 `timeout` / `check_same_thread` 传给
    psycopg3 ⇒ `ProgrammingError: invalid connection option "timeout"`，
    异常在 jobstore 内部抛出、被 uvicorn 吞在 lifespan 里 ⇒
    **应用启动后无报错、无监听、进程静默退出**。

    本用例钉住四件事（原用例钉的「等锁时长」在 PG 上没有对应物 ——
    PG 行锁等待由 lock_timeout 表达，默认无限等）：
    ① URL 方言 = postgresql（不许再出现 sqlite 分支）；
    ② `connect_args` 带 `connect_timeout` ≥ 5（连接建立等待，不是 SQLite 的锁等待）；
    ③ `pool_pre_ping` 开（PG 重启后池里的死连接可自愈）；
    ④ `connect_args` 里**不许**再出现 SQLite 专用参数。
    """
    import apscheduler.jobstores.sqlalchemy as _js

    seen: dict = {}
    real_init = _js.SQLAlchemyJobStore.__init__

    def spy(self, *args, **kwargs):
        seen.update(kwargs)
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(_js.SQLAlchemyJobStore, "__init__", spy)

    svc._persistent_jobstores()

    assert str(seen["url"]).startswith("postgresql"), f"job store 必须用 PG：{seen['url']!r}"
    connect_args = seen["engine_options"]["connect_args"]
    assert connect_args.get("connect_timeout", 0) >= 5, "连接建立必须给足等待时间"
    assert seen["engine_options"]["pool_pre_ping"] is True, "必须开 pre_ping 自愈死连接"
    sqlite_only = {"timeout", "check_same_thread"} & set(connect_args)
    assert not sqlite_only, f"connect_args 里不许再出现 SQLite 专用参数：{sqlite_only}"


# --------------------------------------------------------------------------- #
# 判据：线程在不在（`running` 骗人）
# --------------------------------------------------------------------------- #


def _thread(name: str, alive: bool):
    return types.SimpleNamespace(name=name, is_alive=lambda: alive)


def test_scheduler_thread_alive_only_counts_a_live_named_loop_thread(monkeypatch):
    """探针 = 「有没有**活着的**主循环线程」；别的线程、以及表里挂着的死线程都不算。

    用受控线程表断言，是为了不受同进程其它用例残留线程影响（xdist 同一 worker
    会连跑多个文件）。真线程上的行为由下面那条独立验证。
    """
    monkeypatch.setattr(svc.threading, "enumerate", lambda: [_thread("MainThread", True)])
    assert svc.scheduler_thread_alive() is False, "别的线程不算主循环"

    monkeypatch.setattr(
        svc.threading,
        "enumerate",
        lambda: [_thread("MainThread", True), _thread(svc.SCHEDULER_THREAD_NAME, True)],
    )
    assert svc.scheduler_thread_alive() is True

    # 防御性：判据是 `is_alive()`，不是「出现在枚举里」（否则 shutdown 后残留的
    # 死线程会让探针永远报活 ⇒ 看门狗永不重建）。
    monkeypatch.setattr(svc.threading, "enumerate", lambda: [_thread(svc.SCHEDULER_THREAD_NAME, False)])
    assert svc.scheduler_thread_alive() is False


def test_a_dead_main_loop_still_reports_running(monkeypatch):
    """⚠️ 缺陷现场的核心：主循环死了，`scheduler.running` 仍是 True（端点看不出来）。"""
    scheduler = _start_dying_scheduler(monkeypatch)
    try:
        assert _loop_is_alive(scheduler) is False, "本用例的前置：主循环已经死掉"
        assert scheduler.running is True, (
            "3.11 只在 shutdown() 里改 state ⇒ 线程死了它还报 running，"
            "这正是「/api/schedule 说运行中、作业一个不跑」的成因"
        )
    finally:
        _stop_loop(scheduler)


# --------------------------------------------------------------------------- #
# 治果：看门狗重建
# --------------------------------------------------------------------------- #


def test_revive_builds_a_brand_new_scheduler_and_hands_it_to_the_holder(monkeypatch):
    """重建必须是**新实例**，且真的把主循环起起来（旧实例执行器池已 shutdown，不可复用）。"""
    holder = _Holder()
    holder.scheduler = _start_dying_scheduler(monkeypatch)
    old = holder.scheduler

    revived = svc.revive_scheduler(holder, build=_started_builder())
    try:
        assert revived is not old, "必须是新实例（旧执行器池不可复用）"
        assert holder.scheduler is revived, "要换进 holder（生产 = app.state.scheduler）"
        assert _wait_until(lambda: _loop_is_alive(revived)), (
            "重建出来的实例必须**已经在跑**（生产 build 默认值是 start_scheduler）"
        )
        assert _loop_is_alive(old) is False
    finally:
        _stop_loop(revived)


def test_watchdog_revives_a_dead_loop_and_keeps_the_holder_updated(monkeypatch):
    """看门狗自己巡检：holder 攥着的那台死了就换一台，并把 holder 更新到新实例。"""
    holder = _Holder()
    holder.scheduler = _start_dying_scheduler(monkeypatch)
    dead = holder.scheduler
    built: list = []

    def build():
        scheduler = svc.build_scheduler()
        scheduler.start()
        built.append(scheduler)
        return scheduler

    # 探针收窄到**本用例的 holder**：它还攥着那台死的 ⇒ 报死（引导看门狗动作）；
    # 换过之后交回真探针（新实例是启动的 ⇒ 报活 ⇒ 看门狗放手，不会连环重建）。
    real_probe = svc.scheduler_thread_alive
    monkeypatch.setattr(
        svc,
        "scheduler_thread_alive",
        lambda: False if holder.scheduler is dead else real_probe(),
    )

    watchdog = svc.start_scheduler_watchdog(holder, interval=0.05, build=build)
    try:
        assert _wait_until(lambda: holder.scheduler is not dead), "看门狗没有重建调度器"
        assert holder.scheduler is built[-1]
        assert _wait_until(lambda: _loop_is_alive(holder.scheduler))
        assert len(built) == 1, "新实例活着却被反复重建 ⇒ 探针或看门狗判据有误"
    finally:
        svc.stop_scheduler_watchdog()
        assert _wait_until(lambda: not watchdog.is_alive()), "看门狗应当已经退出"
        _stop_loop(built[-1] if built else holder.scheduler)


def test_watchdog_stops_on_demand(monkeypatch):
    """收尾要把守活线程停掉（否则 app 下线后它还会复活一台指向真库的调度器）。"""
    holder = _Holder()
    holder.scheduler = svc.build_scheduler()
    holder.scheduler.start()
    try:
        watchdog = svc.start_scheduler_watchdog(holder, interval=0.05, build=_started_builder())
        assert watchdog.is_alive()
        svc.stop_scheduler_watchdog()
        assert _wait_until(lambda: not watchdog.is_alive()), "看门狗应当已经退出"
    finally:
        svc.stop_scheduler_watchdog()
        _stop_loop(holder.scheduler)


def test_watchdog_keeps_retrying_with_backoff_when_the_rebuild_fails(monkeypatch):
    """重建失败**不能把看门狗带走**；且要退避重试（不做热循环）。"""
    holder = _Holder()
    holder.scheduler = None  # 没有可接管的调度器
    # 巡检恒判「已死」：本用例只考看门狗的**反应**，不依赖进程里有没有残留线程。
    monkeypatch.setattr(svc, "scheduler_thread_alive", lambda: False)
    stamps: list[float] = []

    def failing_build():
        stamps.append(time.monotonic())
        raise RuntimeError("库不可用")

    watchdog = svc.start_scheduler_watchdog(holder, interval=0.02, build=failing_build)
    try:
        assert _wait_until(lambda: len(stamps) >= 3, timeout=5.0), "应当失败后继续重试"
        assert watchdog.is_alive(), "一次失败不许把看门狗带走"
        assert holder.scheduler is None, "重建失败不许写坏 holder"
        gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:])]
        assert all(gap >= 0.019 for gap in gaps), (
            f"重试没等（间隔 {gaps}）⇒ 退避没生效，会把线程/连接耗光"
        )
    finally:
        svc.stop_scheduler_watchdog()
        assert _wait_until(lambda: not watchdog.is_alive())


# --------------------------------------------------------------------------- #
# 现场：为什么当时「看不出来」——状态端点也在替死线程撒谎
# --------------------------------------------------------------------------- #


def test_status_endpoint_reports_a_dead_loop_as_not_running(api_client, monkeypatch):
    """`/api/schedule/status` 的 `running` 必须来自线程探针，不能来自 `scheduler.running`。

    现场就是这样被骗过去的：主循环 16:00:06 已经死了，状态端点照样回
    `running: true`，前端顶栏照挂「运行中」——**没有任何一处信号**能看出作业
    已经静默停摆 14 分钟。
    """
    token = api_client.post(
        "/api/auth/register", json={"username": "def067-status", "password": "secret123"}
    ).json()["access_token"]

    dying = _start_dying_scheduler(monkeypatch)
    app = api_client.app
    previous = getattr(app.state, "scheduler", None)
    app.state.scheduler = dying
    try:
        payload = api_client.get(
            "/api/schedule/status", headers={"Authorization": f"Bearer {token}"}
        ).json()
        assert dying.running is True, "前置：这一台确实在骗人（线程已死仍报 running）"
        assert payload["running"] is False, "端点信了 `scheduler.running` ⇒ 停摆看不出来"
    finally:
        app.state.scheduler = previous
        _stop_loop(dying)


# --------------------------------------------------------------------------- #
# 接线：修了还要真的起它（「有实现没接上」与没修等价）
# --------------------------------------------------------------------------- #


def _lifespan_called_names() -> set[str]:
    """`backend/app/main.py::lifespan` 里**实际被调用**的（不带限定名的）函数名。

    用 AST 取而不是文本 `in`：否则 import 行或注释里的名字就会让断言恒真。
    """
    tree = ast.parse((ROOT / "backend/app/main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "lifespan":
            return {
                call.func.id
                for call in ast.walk(node)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            }
    raise AssertionError("main.py 里找不到 lifespan —— 守活接线无处可挂")


def test_lifespan_wires_the_watchdog_into_the_app():
    """光有看门狗不够：lifespan 得**起它**、也**停它**。"""
    calls = _lifespan_called_names()
    assert "start_scheduler_watchdog" in calls, "lifespan 没启动看门狗 ⇒ 主循环死了没人管"
    assert "stop_scheduler_watchdog" in calls, (
        "lifespan 收尾没停看门狗 ⇒ app 下线后它还会复活一台指向真库的调度器"
    )
