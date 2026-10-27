"""账号级任务 worker（设计 13 §35.16；**§42 起改为「每类任务一条 worker」**）。

**为什么需要它**（三条根因，详见设计 13 §35.16.1）：账号级任务（笔记同步 / 商品同步）
此前是**每账号一条独立线程**（`note-sync-{account_id}` / `shop-goods-sync-{account_id}`），
带来三个问题：

1. 进程退出后 `running` 行**永久卡住** —— 它们不在任何 worker 的复位范围
   （`recover_interrupted` 只认 `QUEUE_ITEM_TASK_TYPES`）⇒ 卡片与记录页永远显示「同步中」，
   且没有任何路径能让它结束（违反 DEF-048「安全网不能条件性」）；
2. 多账号同时点 ⇒ **并发打平台**（§24.9 要避免的）；
3. 任务行在队列记录页 / 任务中心**看不到**（用户口径「点了同步商品，任务日志没新增」）。

本模块把「认领」接到**队列底座**上：认领复用 `note_queue_service.claim_next_item`
（条件 UPDATE 防抢同一行，与队列项同一把闸门），不新建第二套 worker 机制（DEF-044）。
**记账不在这里做** —— 执行体（`run_note_sync` / `run_shop_goods_sync`）自己写任务行的
running / 终态（它们本来就这么做），worker 只在**执行体崩了**时补记一次失败。

## 并行粒度：**每类任务一条 worker**（设计 13 §42，2026-09-28 用户口径）

> 用户口径：「店铺商品同步和账号笔记同步两个任务依旧无法并行执行……**同一个平台账号下
> 应支持不同任务并行执行**」。

⚠️ **原口径「串行是设计目标」已被取代**（原句保留在下面，不静默删除）：

> ~~「账号级任务实测可跑 30–48 分钟（§21.5 全量 1961 条），worker **在自身线程里直接执行**、
> 不起子线程 ⇒ 一次只有一条账号级任务在跑，第二条保持 `pending`（用户看到「排队等待」
> 而不是「并发打平台」）。」~~

原实现只有**一条** worker 线程，而 `ACCOUNT_WORKER_TYPES` 里的类型由这**同一条**线程
按顺序认领 ⇒ 「笔记同步跑着，商品同步只能等」——连**跨账号**都串行。这正是用户报障的
「依旧无法并行」的**真身**（设计 13 §39 F3 早已点名，但那一版没落地）。

新口径（本模块落地）：

| 维度 | 口径 |
|---|---|
| 并行粒度 | **任务类型级**：`xhs_note_sync` / `xhs_goods_note_sync` **各一条** worker 线程 ⇒ 同一账号的「同步笔记」「同步商品笔记」**真并行**（「同步商品」链路 2026-10-27 已整条退场） |
| 类内 | **仍逐个跑**（一条线程认领一个类型，FIFO）—— 同类型的两条任务不并发（风控口径，§38.3） |
| 跨账号 | 同类型内仍**逐个跑**（与 §39 §281「每类任务一个，类内逐个跑」同口径） |
| 额度 | 不变：限流桶按 `账号:源`（§38.3），并行不放大单账号速率，只是让**不同源**同时走 |

⚠️ **为什么不是「一条线程池 + 账号级互斥」**（`note_sync_service` 队列 worker 那种形态）：
队列 worker 的项是**篇级**的（上千条、账号维度才是并行单位），而账号级任务只有这几类、
每类同时最多一条在跑 —— 池 + 占用登记会引入「算占用 / 认领 / 登记必须同一把锁」那一整套
复杂度（§38.3 实测踩过），换不来任何并行度。**每类一条线程**是能达成目标的最小形状。

⚠️ **认领范围比 `ACCOUNT_TASK_TYPES` 少一个**：`xhs_no_traffic_cleanup` 的建行 / 执行
自成一套（破坏性写操作、当前由 `NO_TRAFFIC_CLEANUP_ENABLED` 显式关闭），本期只把它纳入
「可见 + 复位 + 保留期」三件事，**不动它的执行路径**（改破坏性链路的风险 > 收益）。
（§44 起新增的 `xhs_goods_note_sync` **在**认领范围内。）
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from backend.app.models import Task
from backend.app.services import note_queue_service

logger = logging.getLogger(__name__)

#: worker 线程名**前缀**（设计 13 §42）。每类任务一条，名字 = `前缀-{task_type}`，
#: 例如 `account-task-worker-xhs_note_sync`。
#: ⚠️ 探针 / 看门狗一律按**前缀**匹配（`thread_name_matches_account_worker`）——
#: 写死相等会让「笔记那条活着、商品那条死了」被判成健康（DEF-069 同型：存活判据必须
#: 逐条看，不能只看「有没有一条活的」）。
ACCOUNT_WORKER_THREAD_PREFIX = "account-task-worker"

#: worker **认领**的类型（§35.16.2）。**每个类型一条 worker 线程**（§42）。
ACCOUNT_WORKER_TYPES: tuple[str, ...] = (
    "xhs_note_sync",
    "xhs_goods_note_sync",
)

#: 空闲轮询（唤醒信号丢失时最坏晚这么久发现新任务 —— 安全网不条件性，DEF-048）。
ACCOUNT_IDLE_POLL_SECONDS = 5.0

#: 看门狗巡检间隔 / 重建失败指数退避上限（与 `note_sync_service` 同值）。
ACCOUNT_WATCHDOG_INTERVAL_SECONDS = 30.0
ACCOUNT_WATCHDOG_MAX_BACKOFF_SECONDS = 600.0

#: 测试可注入的 Session 工厂（与 `note_sync_service._session_factory` 同惯例）。
_session_factory = note_queue_service.SessionLocal

#: 唤醒信号（**不是队列**）：入队后 set，所有 worker 醒来各自认领自己的类型。
#: 一个 `Event` 可以有多个等待者，`set()` 会唤醒全部（§42 起需要这一点）。
_account_wakeup = threading.Event()
#: 收尾 / 测试专用出口（置位 + set wakeup ⇒ 各 worker 排空即退；生产永不置位）。
_account_worker_stop = threading.Event()
#: 池成员表：`task_type → 线程`（权威的「该类型有没有 worker」判据）。
_account_workers: dict[str, threading.Thread] = {}
#: 各 worker **当前持有的任务行 id**（看门狗复位中断行时要跳过它们，
#: 见 `note_queue_service.recover_interrupted` 的 `exclude_task_ids`）。
#: 键 = 线程名。⚠️ 只在**有 worker 死掉**时才会被读到（缺口补齐路径）。
_account_held_items: dict[str, int] = {}
_account_worker_lock = threading.Lock()
_account_watchdog_stop = threading.Event()


# --------------------------------------------------------------------------- #
# 线程名判据
# --------------------------------------------------------------------------- #
def account_worker_thread_name(task_type: str) -> str:
    """某类任务的 worker 线程名（**唯一实现**：起线程与探针共用，DEF-044）。"""
    return f"{ACCOUNT_WORKER_THREAD_PREFIX}-{task_type}"


def thread_name_matches_account_worker(name: Any) -> bool:
    """线程名是否属于本模块的 worker 池（**前缀**匹配）。"""
    text = str(name or "")
    return text == ACCOUNT_WORKER_THREAD_PREFIX or text.startswith(
        f"{ACCOUNT_WORKER_THREAD_PREFIX}-"
    )


def _alive_worker_names() -> set[str]:
    """活着成员的线程名集合（判定「持有登记是否还有主」用）。"""
    return {
        thread.name
        for thread in threading.enumerate()
        if thread_name_matches_account_worker(thread.name) and thread.is_alive()
    }


def _held_task_ids() -> set[int]:
    """**仍被活着 worker 持有**的任务行 id（复位中断行时跳过它们）。

    ⚠️ 只算**活着**的线程：死线程留下的持有登记必须失效，否则它手上那条行会被
    误判成「还在跑」而跳过复位 ⇒ 永久卡在 `running`（比死锁更隐蔽）。
    """
    alive = _alive_worker_names()
    return {int(item_id) for name, item_id in _account_held_items.items() if name in alive}


def _mark_worker_busy(thread_name: str, item_id: int) -> None:
    with _account_worker_lock:
        _account_held_items[thread_name] = int(item_id)


def _mark_worker_idle(thread_name: str) -> None:
    """这条 worker 跑完了（放掉持有登记；顺手唤醒其它 worker 立刻重试认领）。"""
    with _account_worker_lock:
        _account_held_items.pop(thread_name, None)
    _account_wakeup.set()


# --------------------------------------------------------------------------- #
# 执行（从任务行重建参数）
# --------------------------------------------------------------------------- #
def _dispatch_account_task(item_id: int) -> Optional[dict]:
    """按任务行分派执行体。返回执行体结果（None = 行不存在 / 类型不认识）。

    ⚠️ 执行参数**只能从任务行重建**（线程版是从 args 传的）—— 因此 §35.16.3 必须先
    证明 `profile_url` / `xsec_token` 已是**死参数**（§21.4 www 源整体移除），
    否则这里会静默丢凭据。建行时写进行里的 `mode` / 时间窗同理（`start_*` 负责）。
    """
    db = _session_factory()
    try:
        item = db.get(Task, item_id)
        if item is None:
            return None
        payload = item.payload if isinstance(item.payload, dict) else {}
        task_type = str(item.task_type)
        account_id = int(payload.get("account_id") or 0)
        user_id = int(item.user_id or 0)
        source = str(payload.get("source") or "manual")
        max_pages = payload.get("max_pages")
        page_size = int(payload.get("page_size") or 50)
        mode = str(payload.get("mode") or "auto")
        time_from_ms = int(payload.get("time_from_ms") or 0)
        time_to_ms = int(payload.get("time_to_ms") or 0)
        # 商品同步的状态范围（§30.3.2，2026-10-02）。缺值 = 老任务行 ⇒ 退回 `all`
        # （`normalize_item_status` 认坏值；这里传 None 也走同一条兜底）。
        item_status = payload.get("item_status")
    finally:
        db.close()

    if not account_id:
        return {"status": "failed", "error": "任务行缺少账号", "code": "ACCOUNT_MISSING"}

    if task_type == "xhs_note_sync":
        from backend.app.services import note_sync_service

        return note_sync_service.run_note_sync(
            account_id,
            task_id=item_id,
            user_id=user_id,
            max_pages=max_pages,
            page_size=page_size,
            source=source,
            # §35.16.3：www 源整体移除后这两个参数已无读取方 ⇒ 重建时传空串语义等价。
            profile_url="",
            xsec_token="",
            mode=mode,
            time_from_ms=time_from_ms,
            time_to_ms=time_to_ms,
            # §44：普通笔记 = creator 源（硬门禁 = 创作者会话）。
            only=note_sync_service.SOURCE_CREATOR,
        )

    if task_type == "xhs_goods_note_sync":
        # §44（2026-09-30）：商品笔记同步 —— 与上面**同一个执行体**，只换源（ark）。
        # 两者由**各自的 worker 线程**认领（`ACCOUNT_WORKER_TYPES`）⇒ 可并行。
        from backend.app.services import note_sync_service

        return note_sync_service.run_note_sync(
            account_id,
            task_id=item_id,
            user_id=user_id,
            max_pages=max_pages,
            page_size=page_size,
            source=source,
            profile_url="",
            xsec_token="",
            mode=mode,
            time_from_ms=time_from_ms,
            time_to_ms=time_to_ms,
            only=note_sync_service.SOURCE_ARK,
        )

def _record_failure(item_id: int, error: BaseException) -> None:
    """执行体**崩了**（抛出）时补记一次失败；行已是终态则不动（不覆写执行体的记账）。"""
    db = _session_factory()
    try:
        item = db.get(Task, item_id)
        if item is None or str(item.status) in note_queue_service.TERMINAL_STATUSES:
            return
        note_queue_service.finish_item(
            db,
            item,
            ok=False,
            reason=f"{type(error).__name__}: {error}",
            code="ACCOUNT_TASK_CRASHED",
        )
    except Exception:  # noqa: BLE001 - 连记账都失败：留痕，行由启动恢复捡回
        logger.exception("account task: failed to record failure (task=%s)", item_id)
    finally:
        db.close()


def _run_safely(label: str, runner: Any, item_id: int) -> None:
    """跑一条并把**线程级**漏网异常吃掉（DEF-069 的第二层网）。

    `BaseException` 而非 `Exception`：`SystemExit` 是它的子类，而 threading 对
    `BaseException` **静默**（不打 traceback、不重启线程）⇒ worker 无声死亡、队列停摆。
    """
    try:
        runner(item_id)
    except BaseException as error:  # noqa: BLE001 - 线程不死（DEF-069）
        logger.error(
            "%s: run escaped the inner net (task=%s): %s", label, item_id, error, exc_info=True
        )


def _run_one_account_task(item_id: int) -> None:
    """跑一条账号级任务（按 `tasks.id`）。

    **执行体自己读写任务行**（置 `running` / 写终态 —— `run_note_sync` 与
    `run_shop_goods_sync` 本来就这么做），worker 不重复记账；只有执行体**抛异常**
    时才补记一次失败（`_record_failure`），保证行不会因为一次崩溃永久停在 `running`。
    """
    try:
        outcome = _dispatch_account_task(item_id)
    except BaseException as error:  # noqa: BLE001 - 兜底安全网：SystemExit 也不许把队列卡死
        logger.warning("account task worker crashed (task=%s): %s", item_id, error)
        _record_failure(item_id, error)
        return
    logger.info("account task done (task=%s): %s", item_id, outcome and outcome.get("status"))


# --------------------------------------------------------------------------- #
# worker 生命周期
# --------------------------------------------------------------------------- #
def _claim_one_account_task(task_type: str) -> Optional[int]:
    """认领**该类型**的一条 `pending` 账号级任务，返回其 `tasks.id`（无活可干 ⇒ None）。

    认领用**独立 Session**（`claim_next_item` 内部提交换状态，用完即关），
    与 `note_sync_service._claim_one_item` 同一写法：认领期异常只让这一轮空转。

    ⚠️ 只认**自己那一类**（§42）：这正是「不同任务可并行、同类任务不并发」的实现 ——
    每条线程只碰自己的类型，所以两条线程永远不会抢同一行，也就不需要「算占用 / 认领 /
    登记同一把锁」那套跨线程纪律（§38.3 的复杂度在这里没有对应物）。
    """
    db = _session_factory()
    try:
        item = note_queue_service.claim_next_item(db, item_task_type=task_type)
        return int(item.id) if item is not None else None
    except BaseException:  # noqa: BLE001 - 认领失败只是这一轮空转
        logger.exception("account task worker: claim failed for %s", task_type)
        return None
    finally:
        db.close()


def _account_task_worker_loop(task_type: str) -> None:
    """认领一条 → 跑一条 → 再认领 …… 认不到就回去等（**每类任务一条这样的线程**）。

    ⚠️ `wait(timeout=…)` 带兜底超时：唤醒信号只是**提示**，丢一次最坏晚 5 秒
    （DEF-048：安全网不能条件性）。
    """
    thread_name = threading.current_thread().name
    while not _account_worker_stop.is_set():
        _account_wakeup.wait(timeout=ACCOUNT_IDLE_POLL_SECONDS)
        _account_wakeup.clear()
        while not _account_worker_stop.is_set():
            item_id = _claim_one_account_task(task_type)
            if item_id is None:
                break
            _mark_worker_busy(thread_name, item_id)
            try:
                _run_safely(
                    f"account task worker[{task_type}]", _run_one_account_task, item_id
                )
            finally:
                # **必须放掉持有登记**（否则那条行在缺口补齐时被当成「还在跑」而跳过复位）
                _mark_worker_idle(thread_name)


def _spawn_account_worker(task_type: str) -> None:
    """起该类任务的 worker 线程（调用方持 `_account_worker_lock`）。

    起新世代时清掉历史停止位 —— 重建出来的 worker 不该被上一世代的收尾位挡住。
    ⚠️ 只在**池里一条活的都没有**时才清：否则「停 worker → 单条缺口补齐」会把
    停止位清掉，正在退出的其它线程又被拉回循环。
    """
    if not any(thread.is_alive() for thread in _account_workers.values()):
        _account_worker_stop.clear()
    thread = threading.Thread(
        target=_account_task_worker_loop,
        args=(task_type,),
        name=account_worker_thread_name(task_type),
        daemon=True,
    )
    _account_workers[task_type] = thread
    thread.start()


def ensure_account_task_worker() -> None:
    """确保**每类任务**都有一条活着的 worker（惰性起；已活则只唤醒它）。

    公开：启动恢复（`main.py::lifespan`）与入队入口（`start_note_sync` /
    `start_shop_goods_sync`）都调它。
    """
    with _account_worker_lock:
        for task_type in ACCOUNT_WORKER_TYPES:
            thread = _account_workers.get(task_type)
            if thread is None or not thread.is_alive():
                _spawn_account_worker(task_type)
    _account_wakeup.set()


def stop_account_task_worker(*, timeout: float = 5.0) -> None:
    """让**全部** worker **立即停手退出**（**测试 / 进程收尾专用**，生产不调用）。

    只置停止位不够：worker 常态卡在 `_account_wakeup.wait(timeout=…)` 上，必须再
    `set` 一下才能唤醒它们回来检查停止位。唤醒后它们**不再认领新任务**、手上那条跑完即退。
    ⚠️ 正在跑的长任务**不会被中断**（取消走 `POST /tasks/{id}/cancel` 的协作式检查点）。
    """
    _account_worker_stop.set()
    _account_wakeup.set()
    with _account_worker_lock:
        workers = list(_account_workers.values())
    for worker in workers:
        if worker.is_alive():
            worker.join(timeout=timeout)


def account_worker_alive(task_type: str) -> bool:
    """**该类任务**的 worker 线程是否活着（逐类的唯一可信存活判据，DEF-069）。

    为什么不看模块变量：线程带异常 / `SystemExit` 死后它仍指着那个 `Thread` 对象。
    """
    expected = account_worker_thread_name(task_type)
    return any(
        thread.name == expected and thread.is_alive() for thread in threading.enumerate()
    )


def account_worker_thread_alive() -> bool:
    """**全部**任务类型的 worker 都活着（池健康判据）。

    ⚠️ 不能拿它当单条判据用：池化后「有一条活着」不代表池健康 —— 死掉的那一类会
    永远没人认领（看门狗因此必须逐类补齐，见 `_revive_missing_workers`）。
    """
    return all(account_worker_alive(task_type) for task_type in ACCOUNT_WORKER_TYPES)


def _revive_missing_workers() -> bool:
    """**按缺口**补齐 worker（并复位中断行）。返回「本轮是否真的重建过」。

    缺口 = 某类任务没有活着的 worker。只有存在缺口时才复位中断行 —— 池全活时
    复位会把手上的任务从脚下抽走（`recover_interrupted` 的判据是「所有 `running` 行」）。
    """
    missing = [
        task_type for task_type in ACCOUNT_WORKER_TYPES if not account_worker_alive(task_type)
    ]
    if not missing:
        return False

    recovered = {"requeued": 0, "failed": 0}
    try:
        db = _session_factory()
        try:
            recovered = note_queue_service.recover_interrupted(
                db,
                # ⚠️ 复位用 **`ACCOUNT_TASK_TYPES`（四类）**而不是 `ACCOUNT_WORKER_TYPES`：
                # 清理任务虽不由本 worker 执行，但它的 `running` 行同样会因进程退出而卡住，
                # 一并复位才不会留下永久行（DEF-048 同型）。
                item_task_types=note_queue_service.ACCOUNT_TASK_TYPES,
                # ⚠️ **跳过还活着的 worker 手上的行**（§38.3 同型教训）：不排除就会把
                # 「商品同步正在跑的那条」改回 `pending` ⇒ 被跑两遍 + 原 worker 跑完把
                # 一条已不属于它的行写成终态。
                exclude_task_ids=_held_task_ids(),
            )
        finally:
            db.close()
    except BaseException:  # noqa: BLE001 - 复位失败也要把 worker 建起来
        logger.exception("account task worker 重建：中断行复位失败（继续重建 worker）")

    with _account_worker_lock:
        for task_type in missing:
            if not account_worker_alive(task_type):
                _spawn_account_worker(task_type)
    _account_wakeup.set()
    logger.error(
        "account-task-worker 缺口 %s —— 已重建（中断行 requeued=%s failed=%s）",
        missing,
        recovered.get("requeued"),
        recovered.get("failed"),
    )
    return True


def start_account_task_watchdog() -> threading.Thread:
    """起看门狗：定期检查**每类** worker 是否还活着，缺了就复位 + 重建（幂等）。

    与 `note_sync_service.start_sync_worker_watchdog` 同型：探针只信**活着的同名线程**，
    重建失败指数退避（不刷日志、不忙等）。
    """
    _account_watchdog_stop.clear()

    def _loop() -> None:
        backoff = ACCOUNT_WATCHDOG_INTERVAL_SECONDS
        while not _account_watchdog_stop.wait(timeout=backoff):
            try:
                # 无缺口时是纯读（一次 `threading.enumerate()`），开销可忽略。
                _revive_missing_workers()
                backoff = ACCOUNT_WATCHDOG_INTERVAL_SECONDS
            except Exception as error:  # noqa: BLE001 - 看门狗自己必须活着
                logger.exception("account task watchdog failed: %s", error)
                backoff = min(backoff * 2, ACCOUNT_WATCHDOG_MAX_BACKOFF_SECONDS)

    thread = threading.Thread(target=_loop, name="account-task-watchdog", daemon=True)
    thread.start()
    return thread


def stop_account_task_watchdog() -> None:
    """让看门狗退出巡检（幂等；下一次 `start_account_task_watchdog` 自动复位）。"""
    _account_watchdog_stop.set()


def recover_account_tasks() -> dict:
    """启动恢复：把上次进程留下的账号级 `running` 行复位（**自动续跑**）+ 收口孤儿日志。

    与队列项的复位同一口径（`recover_interrupted`）：中断自动重试、业务失败不重试。
    返回 `{"requeued": n, "failed": m, "logs_closed": k}`。

    ⚠️ **孤儿日志收口是必需的第二步**（2026-09-27 实测，用户报「再次分析账号笔记同步任务
    是否真实还在执行」）：观测线程（`_watch_task_run`）是**进程内**的，重启即消失 ⇒ 它负责
    的日志会永远停在 `running`，账号卡据此显示「同步中」而任务其实早已结束。收口放在
    复位**之后**（任务行已定终态，收口才有据可依），且**只在启动时**做 —— 运行期有观测
    线程在写，日志该是 `running`。
    """
    db = _session_factory()
    try:
        recovered = note_queue_service.recover_interrupted(
            db, item_task_types=note_queue_service.ACCOUNT_TASK_TYPES
        )
        closed = 0
        try:
            from backend.app.services import sync_task_service

            closed = sync_task_service.close_orphan_running_logs(db)
            db.commit()
        except BaseException:  # noqa: BLE001 - 收口失败不能让应用起不来
            logger.exception("孤儿日志收口失败（继续启动）")
            db.rollback()
        recovered["logs_closed"] = closed
        if closed:
            logger.info("启动恢复：收口孤儿 running 日志 %s 条", closed)
        return recovered
    except BaseException:  # noqa: BLE001 - 复位失败不能让应用起不来
        logger.exception("account task 启动恢复失败（继续启动）")
        return {"requeued": 0, "failed": 0, "logs_closed": 0}
    finally:
        db.close()
