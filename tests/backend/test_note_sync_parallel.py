"""跨账号并行 + 账号级互斥（设计 13 §38.3，2026-09-27 用户口径「账号之间的任务应支持并行」）。

四组断言：

1. **认领口径**：`claim_next_item(exclude_account_ids=…)` —— 不传时行为与改造前**逐字一致**
   （其它两个队列复用同一个函数，不许被这次改造带偏）；
2. **账号级互斥**：同一账号的项在「已被占用」时**认领不到**（同账号并发只会互抢同一个
   限流桶：既不提速，又让「每账号每源 N 次/分钟」的语义失真）；
3. **跨账号并行**：两个账号各一条 ⇒ 两条都能被认领（这才是这次改造的目的）；
4. **池大小跟随配置**：`sync_parallelism()` 夹取 + `_reconcile_sync_worker_pool()`
   既**补缺**也**收多**（调小并行度必须真的生效，否则就是「假实时配置」）。

⚠️ 线程安全：本文件会真起线程，teardown 必须把池收干净（判据按**线程名前缀**，
只认 `SYNC_WORKER_THREAD_NAME` 会漏掉 `#2`..`#8`）。
"""

from __future__ import annotations

import threading
import time
from uuid import uuid4

import pytest

from backend.app.models import Note, PlatformAccount, Task
from backend.app.services import note_queue_service as nqs
from backend.app.services import note_sync_service as nss



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract


def _seed_account(db, *, nickname: str) -> int:
    # ⚠️ FK=ON（2026-10-04）：`platform_accounts.user_id -> users.id` 有外键，
    # 父行必须存在（`db_factory` 按契约不播种）。
    from tests.backend.conftest import seed_owner_rows as _seed

    _seed(db.get_bind(), user_id=1)
    account = PlatformAccount(
        user_id=1, platform="xhs", sub_type="creator", nickname=nickname, status="active"
    )
    db.add(account)
    db.commit()
    return int(account.id)


def _seed_item(db, *, account_id: int, title: str = "并行笔记") -> int:
    """落一条同步队列项（带 `payload.account_id` —— 账号互斥就靠它）。"""
    note = Note(
        user_id=1,
        platform_account_id=account_id,
        platform="xhs",
        note_id=f"par-{uuid4().hex[:8]}",
        title=title,
        note_type="synced",
        raw_json={"note_url": "https://www.xiaohongshu.com/explore/abc"},
    )
    db.add(note)
    db.commit()
    row = Task(
        user_id=1,
        platform="xhs",
        task_type=nqs.TASK_TYPE_SYNC_ITEM,
        status="pending",
        progress=0,
        payload={
            "note_pk": int(note.id),
            "account_id": int(account_id),
            "source": nqs.SOURCE_NOTE_SYNC_SINGLE,
        },
    )
    db.add(row)
    db.commit()
    return int(row.id)


@pytest.fixture()
def quiesced_pool():
    """用例前后都收干净池（按前缀），并复位池的登记表。"""
    _quiesce()
    yield
    _quiesce()


def _quiesce(timeout: float = 5.0) -> None:
    nss._sync_worker_stop.set()
    for stop in list(nss._sync_worker_stops.values()):
        stop.set()
    nss._sync_wakeup.set()
    for thread in list(threading.enumerate()):
        if nss.thread_name_matches_sync_worker(thread.name) and thread.is_alive():
            thread.join(timeout=timeout)
    with nss._sync_queue_lock:
        nss._sync_worker = None
        nss._sync_workers.clear()
        nss._sync_worker_stops.clear()
        nss._sync_busy_accounts.clear()
        nss._sync_held_items.clear()
    nss._sync_worker_stop.clear()
    nss._sync_wakeup.clear()


# --------------------------------------------------------------------------- #
# 1) 认领口径：不传排除集 = 改造前行为
# --------------------------------------------------------------------------- #
def test_claim_without_exclusion_is_unchanged(db_session):
    """`exclude_account_ids` 缺省 ⇒ 仍是「全局 FIFO 认领第一条」（其它队列的既有语义）。"""
    account_id = _seed_account(db_session, nickname="FIFO 店")
    first = _seed_item(db_session, account_id=account_id, title="第一条")
    _seed_item(db_session, account_id=account_id, title="第二条")

    claimed = nqs.claim_next_item(db_session, item_task_type=nqs.TASK_TYPE_SYNC_ITEM)
    assert claimed is not None and int(claimed.id) == first, "缺省必须认领队头（FIFO）"


def test_claim_skips_busy_account_and_takes_the_next_one(db_session):
    """账号被占住 ⇒ **跳过**它的项，去认领别的账号的（而不是原地空转）。"""
    busy = _seed_account(db_session, nickname="忙店")
    idle = _seed_account(db_session, nickname="闲店")
    _seed_item(db_session, account_id=busy, title="忙店第一条")
    idle_item = _seed_item(db_session, account_id=idle, title="闲店第一条")

    claimed = nqs.claim_next_item(
        db_session, item_task_type=nqs.TASK_TYPE_SYNC_ITEM, exclude_account_ids={busy}
    )
    assert claimed is not None, "窗口里有别的账号可跑，不该返回 None"
    assert int(claimed.id) == idle_item
    assert nqs.account_id_of(claimed) == idle


def test_claim_returns_none_when_the_window_is_all_busy(db_session):
    """窗口内全是忙账号 ⇒ 返回 `None`（**不是**「队列空了」：调用方据此继续等）。"""
    account_id = _seed_account(db_session, nickname="独店")
    _seed_item(db_session, account_id=account_id)

    assert (
        nqs.claim_next_item(
            db_session, item_task_type=nqs.TASK_TYPE_SYNC_ITEM, exclude_account_ids={account_id}
        )
        is None
    )


def test_account_without_payload_id_is_never_blocked(db_session):
    """`payload.account_id` 缺值的脏行**不参与排除** —— 「未知账号」≠「忙账号」。

    否则一条脏行会把整条队列堵住（谁也认领不了它，它又永远排在队头）。
    """
    _seed_account(db_session, nickname="脏行店")
    dirty = Task(
        user_id=1,
        platform="xhs",
        task_type=nqs.TASK_TYPE_SYNC_ITEM,
        status="pending",
        progress=0,
        payload={"note_pk": 999, "source": nqs.SOURCE_NOTE_SYNC_SINGLE},  # 没有 account_id
    )
    db_session.add(dirty)
    db_session.commit()

    claimed = nqs.claim_next_item(
        db_session, item_task_type=nqs.TASK_TYPE_SYNC_ITEM, exclude_account_ids={1, 2, 3}
    )
    assert claimed is not None and int(claimed.id) == int(dirty.id)


def test_two_accounts_can_be_claimed_at_the_same_time(db_session):
    """跨账号并行：两个账号各一条 ⇒ 两次认领各拿一条（互不排除）。"""
    first = _seed_account(db_session, nickname="并行 A")
    second = _seed_account(db_session, nickname="并行 B")
    _seed_item(db_session, account_id=first)
    _seed_item(db_session, account_id=second)

    claimed_a = nqs.claim_next_item(db_session, item_task_type=nqs.TASK_TYPE_SYNC_ITEM)
    assert claimed_a is not None
    busy = {nqs.account_id_of(claimed_a)}
    claimed_b = nqs.claim_next_item(
        db_session, item_task_type=nqs.TASK_TYPE_SYNC_ITEM, exclude_account_ids=busy
    )
    assert claimed_b is not None, "另一个账号的项必须能被认领（这正是并行的目的）"
    assert nqs.account_id_of(claimed_b) not in busy
    assert {int(claimed_a.id), int(claimed_b.id)} == {
        row.id for row in db_session.query(Task).all()
    }


# --------------------------------------------------------------------------- #
# 2) 复位中断行：跳过还活着的那几条手上的项
# --------------------------------------------------------------------------- #
def test_recover_interrupted_skips_held_rows(db_session):
    """`exclude_task_ids` ⇒ 被活着的 worker 持有的行**不许**被改回 `pending`。

    ⚠️ 不给这个参数（池化前的写法）会把任务从 worker 脚下抽走：同一条被跑两遍，
    而且原 worker 跑完还会把一条已不属于它的行写成终态。
    """
    account_id = _seed_account(db_session, nickname="复位店")
    held = _seed_item(db_session, account_id=account_id, title="有人持有")
    orphan = _seed_item(db_session, account_id=account_id, title="无人认领")
    for task_id in (held, orphan):
        row = db_session.get(Task, task_id)
        row.status = "running"
    db_session.commit()

    recovered = nqs.recover_interrupted(
        db_session,
        item_task_types=(nqs.TASK_TYPE_SYNC_ITEM,),
        exclude_task_ids={held},
    )
    assert recovered["requeued"] == 1
    assert db_session.get(Task, held).status == "running", "被持有的行不许被动"
    assert db_session.get(Task, orphan).status == "pending"


# --------------------------------------------------------------------------- #
# 3) 池大小跟随配置（补缺 + 收多）
# --------------------------------------------------------------------------- #
def test_sync_parallelism_is_clamped(monkeypatch):
    """并行度夹到 `[1, SYNC_MAX_PARALLELISM]`（手滑填 200 不许真起 200 条）。

    ⚠️ 打桩打在 `settings_service` 模块属性上：`sync_parallelism` 的函数体内是
    `from … import live_int_process`（惰性导入），调用时才查模块属性 ⇒ 打模块能生效。
    """
    from backend.app.services import settings_service as ss

    monkeypatch.setattr(ss, "live_int_process", lambda key, default: 0)
    assert nss.sync_parallelism() == 1
    monkeypatch.setattr(ss, "live_int_process", lambda key, default: 999)
    assert nss.sync_parallelism() == nss.SYNC_MAX_PARALLELISM
    monkeypatch.setattr(ss, "live_int_process", lambda key, default: 3)
    assert nss.sync_parallelism() == 3


@pytest.fixture()
def idle_workers(monkeypatch):
    """让池里的 worker **真的空闲**：只掐掉认领，**保留真实循环**。

    ⚠️ 为什么不把 `_sync_worker_loop` 换成 `lambda: time.sleep(30)`：那种替身**不认停止位**，
    于是「调小并行度 ⇒ 收掉多余的线程」永远收不掉（实测踩到）—— 用假循环去测
    停止语义，测出来的是替身的行为，不是产品的行为。真实循环 + 空认领既安全
    （不碰库、不认领任何行）又保留了停止位语义。
    """
    monkeypatch.setattr(nss, "_claim_one_sync_item", lambda exclude_account_ids=None: None)
    return monkeypatch


def test_pool_grows_to_the_configured_size(quiesced_pool, idle_workers):
    """配置 3 ⇒ 池里 3 条（**跨账号并行的载体**）。"""
    idle_workers.setattr(nss, "sync_parallelism", lambda: 3)

    with nss._sync_queue_lock:
        nss._reconcile_sync_worker_pool()

    assert nss.sync_worker_pool_size() == 3
    names = sorted(t.name for t in nss._sync_workers)
    assert names[0] == nss.SYNC_WORKER_THREAD_NAME, "第 1 条保持原名（既有探针认它）"
    assert names[1] == f"{nss.SYNC_WORKER_THREAD_NAME}#2"


def test_pool_shrinks_when_the_config_is_lowered(quiesced_pool, idle_workers):
    """配置从 3 降到 1 ⇒ 多余的线程被收掉（**调小也必须真的生效**）。"""
    idle_workers.setattr(nss, "sync_parallelism", lambda: 3)
    with nss._sync_queue_lock:
        nss._reconcile_sync_worker_pool()
    assert nss.sync_worker_pool_size() == 3

    idle_workers.setattr(nss, "sync_parallelism", lambda: 1)
    with nss._sync_queue_lock:
        nss._reconcile_sync_worker_pool()
    # 被收掉的那几条要真的退出（不是只从表里摘掉）
    deadline = time.time() + 5
    while time.time() < deadline and nss.sync_worker_pool_size() > 1:
        time.sleep(0.05)
    assert nss.sync_worker_pool_size() == 1, "降并行度必须真的收掉多余线程"
    assert len(nss._sync_workers) == 1


def test_pool_reconcile_replaces_dead_members(quiesced_pool, idle_workers):
    """池里死掉 1 条 ⇒ 补齐差额（死 2 条剩 1 条那种「还活着」的假象必须被看穿）。"""
    idle_workers.setattr(nss, "sync_parallelism", lambda: 3)
    with nss._sync_queue_lock:
        nss._reconcile_sync_worker_pool()
    assert nss.sync_worker_pool_size() == 3

    # 让其中一条**真死**（不是假造一个 Thread 对象）
    victim = nss._sync_workers[-1]
    nss._sync_worker_stops[victim.name].set()
    nss._sync_wakeup.set()
    victim.join(timeout=5)
    assert not victim.is_alive()

    assert nss.sync_worker_pool_deficit() == 1, "死 1 条剩 2 条 ⇒ 缺口 1（旧判据看不见）"
    with nss._sync_queue_lock:
        nss._reconcile_sync_worker_pool()
    assert nss.sync_worker_pool_size() == 3, "缺一条要补一条"


def test_dead_worker_never_keeps_an_account_busy_forever(quiesced_pool, idle_workers):
    """死线程的账号占用必须失效 —— 否则那个账号的项再也没人认领（界面显示排队中却永不开始）。"""
    idle_workers.setattr(nss, "sync_parallelism", lambda: 1)
    with nss._sync_queue_lock:
        nss._reconcile_sync_worker_pool()

    name = nss.sync_worker_thread_name(0)
    nss._sync_busy_accounts[name] = 42
    nss._sync_held_items[name] = 7
    assert nss._busy_account_ids() == {42}
    assert nss._held_task_ids() == {7}

    nss._sync_worker_stops[name].set()
    nss._sync_wakeup.set()
    nss._sync_workers[0].join(timeout=5)

    assert nss._busy_account_ids() == set(), "死线程的占用必须失效"
    assert nss._held_task_ids() == set()
