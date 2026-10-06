"""笔记队列持久化底座 `note_queue_service` 的单测（设计 13 §35.9，2026-09-25；
2026-09-26 常驻化：批次层移除，一行 = 一个队列项）。

用公共夹具 `db_factory`（临时库）+ `monkeypatch.setattr(q, "SessionLocal", db_factory)`，
**不碰真实运行库**。盯的是队列纪律，不是平台行为：

1. **幂等入队**：同用户 + 同类型 + 未终态同主键 ⇒ 不重复入队；
2. **常驻 FIFO**：没有批次 / 轮次概念，进一条跑一条；认领只认 `*_item`；
3. **认领**：FIFO + 条件 UPDATE 防抢同一行（同一条不跑两遍）；
4. **终态**：写原因；`payload` 必须赋新 dict（JSON 列原地改不标脏）；
5. **快照**：常驻队列实时态 + 近期终态（`running`/`queued`/`total`/`finished`）；
   按 `user_id` 收口（DEF-066）；
6. **重启恢复**：只自动重试「中断」，业务失败不动；重试用尽转 `failed` + `interrupted`；
   `publishing` 僵尸笔记行复位而 `published` 不动；
7. **保留期清理**：只删终态超期、活任务永不删、按用户收口；
8. **队列注册表**（§35.11）：清单是队列记录页的唯一来源，新增队列只加一行；
9. **商品生成队列**（§31.10）：不绑定笔记 ⇒ `item_id` 维度幂等、快照条目走
   payload、清理范围从注册表派生（新队列自动收进来）；
10. **来源与入队落库**（2026-09-26）：入队即写 `payload.source`（**按钮级九值，
    必填无默认**）+ `note_title` 快照；恢复断点行标 `recovery`。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.app.core.time import shanghai_now
from backend.app.models import Note, PlatformAccount, Task, User
from backend.app.services import note_queue_service as q
from tests.backend.conftest import ROOT


@pytest.fixture
def queue_env(db_factory):
    """一个用户 + 一个账号 + 三篇笔记 + 一条已打开的 Session（返回四件套）。"""
    db = db_factory()
    user = User(username="owner", password_hash="x")
    db.add(user)
    db.flush()
    account = PlatformAccount(
        user_id=user.id, platform="xhs", sub_type="creator", nickname="测试店", status="active"
    )
    db.add(account)
    db.flush()
    notes = []
    for index in range(1, 4):
        note = Note(
            user_id=user.id,
            platform_account_id=account.id,
            platform="xhs",
            note_id=f"N-{index}",
            note_type="local_generated",
            title=f"标题-n{index}",
        )
        db.add(note)
        notes.append(note)
    db.commit()
    for note in notes:
        db.refresh(note)
    yield db, user.id, account, notes
    db.close()


@pytest.fixture(autouse=True)
def _patch_session_factory(monkeypatch, db_factory):
    """底座内部自建的 Session 一律指向**临时库**（`cleanup_queue_records` 等入口）。

    不打桩就会拿**真实运行库**跑清理 / 恢复 —— 那是数据隔离事故，不是测试噪声。
    """
    monkeypatch.setattr(q, "SessionLocal", db_factory)


def _items(db, task_type: str) -> list[Task]:
    return db.scalars(select(Task).where(Task.task_type == task_type).order_by(Task.id)).all()


def _add_note(db, user_id: int, account, index: int) -> Note:
    """造一篇**还没入队**的笔记（测试要新主键时用）。

    ⚠️ 为什么不能复用 `queue_env` 的三篇：入队是**幂等**的（同主键未终态行存在
    ⇒ 返回 `created=False`），复用同一篇的第二次入队根本挂不上批次 —— 那会让
    「一次动作一个批次」的断言变成假绿。
    """
    note = Note(
        user_id=user_id,
        platform_account_id=account.id,
        platform="xhs",
        note_id=f"N-extra-{index}",
        note_type="local_generated",
        title=f"标题-extra{index}",
    )
    db.add(note)
    db.flush()
    return note


def _enqueue(
    db,
    user_id: int,
    item_task_type: str,
    note_pk: int,
    account_id: int,
    note_title: str = "",
    source: str = q.SOURCE_NOTE_SYNC_SINGLE,
):
    """入队 + 提交，返回 `(row, created)`（常驻化后没有批次行）。"""
    row, created = q.enqueue_item(
        db,
        user_id=user_id,
        item_task_type=item_task_type,
        note_pk=note_pk,
        account_id=account_id,
        note_title=note_title,
        source=source,
    )
    db.commit()
    return row, created


# --------------------------------------------------------------------------- #
# 1) 幂等入队
# --------------------------------------------------------------------------- #
def test_enqueue_is_idempotent_for_the_same_note(queue_env):
    """同一篇在队列 / 执行中 ⇒ 不重复入队、不重复请求（`already_queued` 的库侧判据）。"""
    db, user_id, account, notes = queue_env
    first, created_first = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
    assert created_first is True
    again, created_again = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
    assert created_again is False, "已在队列的篇不该再入队"
    assert again.id == first.id, "返回的是既有那一行"
    assert len(_items(db, q.TASK_TYPE_SYNC_ITEM)) == 1, "库里只该有一行"

    # 终态之后再入队 = 合法的新任务（同一篇可以再同步一次）
    second, created_second = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[1].id, account.id)
    assert created_second is True
    q.finish_item(db, first, ok=True)
    third, created_third = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
    assert created_third is True, "已出结果的篇允许再次入队"
    assert len({first.id, second.id, third.id}) == 3
    assert third.parent_task_id is None, "常驻化后没有批次父行（顶级记录）"


def test_queue_is_resident_and_batch_is_per_action(queue_env):
    """常驻队列 + **一次动作一个批次**（2026-09-26 用户口径两轮，后者覆盖前者）。

    第一轮（「不需要队列批次，队列是常驻」）作废的是「**轮次**记账」—— 空闲即开
    一批、繁忙并入一批，那种批次会把单条点击也塞进一个长命批次里。现行形态是
    「**动作**记账」：

    * 队列仍**常驻**：空闲 / 繁忙都不改变入队行为，快照只描述队列实时态；
    * 不传 `batch` ⇒ 该行是**顶级行**（启动恢复 / 存量 / 测试直插都这样）；
    * 传同一个 `BatchRequest` ⇒ 同一个唯一批次号下的 N 条明细（**不复用**已有
      批次号 —— 复用就退回被淘汰的轮次形态）。
    """
    db, user_id, account, notes = queue_env
    first, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
    q.finish_item(db, first, ok=True)
    second, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[1].id, account.id)

    snap = q.snapshot(db, user_id=user_id, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    # 终态的 first 留在近期终态窗口里（不是「新一轮」把它清掉）
    assert [entry["note_pk"] for entry in snap["queued"]] == [notes[1].id]
    assert snap["total"] == 2, "total = 待跑 + 近期终态（跑过的记录不消失）"
    # 不传批次 ⇒ 顶级行（常驻队列的默认形态：一条记录一个动作，不必都有批次）
    assert first.parent_task_id is None and second.parent_task_id is None
    assert not [row for row in _items(db, q.TASK_TYPE_SYNC_ITEM) if row.parent_task_id], (
        "不传 batch 的行不许被挂到任何批次下"
    )

    # 一次动作一个批次号：两次动作两个号（**不复用**未闭合批次）
    note_a = _add_note(db, user_id, account, 11)
    note_b = _add_note(db, user_id, account, 12)
    batch_a = q.BatchRequest(
        user_id=user_id, item_task_type=q.TASK_TYPE_SYNC_ITEM, source=q.SOURCE_NOTE_SYNC_BATCH
    )
    third, _ = q.enqueue_item(
        db,
        user_id=user_id,
        item_task_type=q.TASK_TYPE_SYNC_ITEM,
        note_pk=note_a.id,
        note_title=str(note_a.title or ""),
        source=batch_a.source,
        batch=batch_a,
    )
    batch_b = q.BatchRequest(
        user_id=user_id, item_task_type=q.TASK_TYPE_SYNC_ITEM, source=q.SOURCE_NOTE_SYNC_BATCH
    )
    fourth, _ = q.enqueue_item(
        db,
        user_id=user_id,
        item_task_type=q.TASK_TYPE_SYNC_ITEM,
        note_pk=note_b.id,
        note_title=str(note_b.title or ""),
        source=batch_b.source,
        batch=batch_b,
    )
    assert batch_a.row.batch_no != batch_b.row.batch_no, "两次动作两个批次号（唯一）"
    assert third.parent_task_id == batch_a.row.id and fourth.parent_task_id == batch_b.row.id
    assert len(q.children_of(db, parent_id=batch_a.row.id)) == 1, "一次动作一个明细 = 单条也有批次"
    assert batch_a.row.batch_no.startswith(q.BATCH_NO_PREFIX)


def test_batch_number_is_unique_and_generated_per_row(queue_env):
    """批次号：格式固定、**逐行唯一**（同一秒连开 5 个也不撞）。"""
    import re

    db, user_id, account, notes = queue_env
    numbers = set()
    for index in range(5):
        note = _add_note(db, user_id, account, index)
        request = q.BatchRequest(
            user_id=user_id,
            item_task_type=q.TASK_TYPE_SYNC_ITEM,
            source=q.SOURCE_NOTE_SYNC_SINGLE,
        )
        q.enqueue_item(
            db,
            user_id=user_id,
            item_task_type=q.TASK_TYPE_SYNC_ITEM,
            note_pk=note.id,
            note_title=str(note.title or ""),
            source=request.source,
            batch=request,
        )
        numbers.add(request.row.batch_no)
    assert len(numbers) == 5, "五次动作五个批次号（同一秒内也不许撞）"
    for number in numbers:
        assert re.fullmatch(r"NB\d{14}-[0-9A-Z]{4}", number), f"批次号格式固定：{number}"


def test_lazy_batch_row_is_not_created_when_nothing_is_enqueued(queue_env):
    """批次行**懒创建**：一项都没真入队 ⇒ 不留空批次行（记录页不该出现 0 项批次）。"""
    db, user_id, account, notes = queue_env
    # 先把三篇都入队（占住幂等），再用一个批次上下文"重入"一次 ⇒ 全部 created=False
    for note in notes:
        _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, note.id, account.id)
    request = q.BatchRequest(
        user_id=user_id, item_task_type=q.TASK_TYPE_SYNC_ITEM, source=q.SOURCE_NOTE_SYNC_BATCH
    )
    for note in notes:
        _row, created = q.enqueue_item(
            db,
            user_id=user_id,
            item_task_type=q.TASK_TYPE_SYNC_ITEM,
            note_pk=note.id,
            note_title=str(note.title or ""),
            source=request.source,
            batch=request,
        )
        assert created is False, "幂等命中（已在队列里）"
    assert request.row is None, "没有任何真入队 ⇒ 批次行根本没建"
    assert db.scalars(select(Task).where(Task.batch_no.is_not(None))).all() == []


def test_batch_row_is_never_claimed_as_a_queue_item(queue_env):
    """批次行**不是可执行任务**：`claim_next_item` 只认队列项类型（批次行永不被认领）。"""
    db, user_id, account, notes = queue_env
    request = q.BatchRequest(
        user_id=user_id, item_task_type=q.TASK_TYPE_SYNC_ITEM, source=q.SOURCE_NOTE_SYNC_SINGLE
    )
    _row, _ = q.enqueue_item(
        db,
        user_id=user_id,
        item_task_type=q.TASK_TYPE_SYNC_ITEM,
        note_pk=notes[0].id,
        source=request.source,
        batch=request,
    )
    assert q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM) is not None, "明细被认领"
    assert q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM) is None, "只剩批次行，认不到"
    db.refresh(request.row)
    assert request.row.status != "running", "批次行不会被置 running（它不是任务）"


def test_batch_rollup_prefers_failure_over_cancellation(queue_env):
    """派生状态的分支顺序即语义：**失败优先于取消**（别把「有篇没跑成」藏起来）。

    这个顺序同时让「可清理判据」自洽：`failed` 不可清 ⇒ 派生状态为 `failed` 的
    批次也不可清，前端不必另判子行。
    """
    assert q.batch_rollup([]) == "done", "没有子行（都被单独清掉了）⇒ 没有可报的失败"
    assert q.batch_rollup(["done", "cancelled"]) == "cancelled"
    assert q.batch_rollup(["done", "cancelled", "failed"]) == "failed", "有失败就报失败"
    assert q.batch_rollup(["done", "exhausted"]) == "failed", "exhausted 对用户就是「没跑成」"
    assert q.batch_rollup(["done", "pending"]) == "pending"
    assert q.batch_rollup(["done", "running"]) == "running", "在跑优先于排队"
    failed_or_active = {"failed", "exhausted", "pending", "running"}
    for statuses in ([], ["done"], ["cancelled"], ["done", "cancelled"]):
        assert q.batch_rollup(statuses) in q.CLEANABLE_STATUSES
    for status in failed_or_active:
        assert q.batch_rollup(["done", status]) not in q.CLEANABLE_STATUSES, (
            f"派生状态 {status} 必须落进不可清集合（否则子行不可清、批次却可清）"
        )


def test_batch_accounts_are_derived_from_children(queue_env):
    """批次级账号**由子行派生**（2026-09-26 用户口径「批次记录执行的账号」）。

    为什么不给批次行存一个 `account_id`：一次批量可以**跨账号**勾选 ⇒ 批次级根本
    不存在「唯一账号」，存一个只会在跨账号时**说错**（报第一个账号，而明细里明明有
    两个）。派生还有个附带好处：某条明细被单独清掉后账号列表跟着收窄，不会留下
    「批次说是 A、明细里已经没有 A」这种自相矛盾。
    """
    db, user_id, account, notes = queue_env
    other = PlatformAccount(
        user_id=user_id, platform="xhs", sub_type="creator", nickname="第二个账号", status="active"
    )
    db.add(other)
    db.flush()
    batch = q.BatchRequest(
        user_id=user_id, item_task_type=q.TASK_TYPE_SYNC_ITEM, source=q.SOURCE_NOTE_SYNC_BATCH
    )
    extra = _add_note(db, user_id, other, 7)
    for note_pk, account_id in (
        (notes[0].id, account.id),
        (notes[1].id, account.id),
        (extra.id, other.id),
    ):
        q.enqueue_item(
            db,
            user_id=user_id,
            item_task_type=q.TASK_TYPE_SYNC_ITEM,
            note_pk=note_pk,
            account_id=account_id,
            source=batch.source,
            batch=batch,
        )
    db.commit()
    children = q.children_of(db, parent_id=batch.row.id)
    assert [child.payload["account_id"] for child in children] == [
        account.id,
        account.id,
        other.id,
    ], "入队即落账号快照（同步队列这轮才补上；发布 / 商品生成一直在写）"
    assert q.batch_account_ids(children) == [account.id, other.id], "去重 + FIFO 保序"

    # 缺值 / 非法值**跳过**（缺值口径：不知道是谁跑的就不编一个 0 出来）
    dirty = [
        Task(user_id=user_id, platform="xhs", task_type=q.TASK_TYPE_SYNC_ITEM, payload={}),
        Task(
            user_id=user_id, platform="xhs", task_type=q.TASK_TYPE_SYNC_ITEM,
            payload={"account_id": None},
        ),
        Task(
            user_id=user_id, platform="xhs", task_type=q.TASK_TYPE_SYNC_ITEM,
            payload={"account_id": "x"},
        ),
        Task(
            user_id=user_id, platform="xhs", task_type=q.TASK_TYPE_SYNC_ITEM,
            payload={"account_id": 0},
        ),
        Task(
            user_id=user_id, platform="xhs", task_type=q.TASK_TYPE_SYNC_ITEM,
            payload={"account_id": other.id},
        ),
    ]
    assert q.batch_account_ids(dirty) == [other.id], "缺值 / 非法值跳过，只留真账号"
    assert q.batch_account_ids([]) == [], "没有子行 ⇒ 空列表（调用方渲染 -）"


# --------------------------------------------------------------------------- #
# 2) 认领与记账
# --------------------------------------------------------------------------- #
def test_claim_is_fifo_and_never_returns_the_same_row_twice(queue_env):
    """认领按 id 升序（FIFO）；同一行不会被第二次认领拿到。"""
    db, user_id, account, notes = queue_env
    first, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
    second, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[1].id, account.id)

    claimed_first = q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    claimed_second = q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    assert claimed_first.id == first.id, "先入先出"
    assert claimed_second.id == second.id
    assert claimed_first.status == "running" and claimed_first.started_at is not None
    assert q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM) is None, "没有 pending 了"


def test_claim_ignores_terminal_rows(queue_env):
    """终态行不参与认领（跑过的不会再跑）。"""
    db, user_id, account, notes = queue_env
    row, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
    q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    q.finish_item(db, row, ok=True)
    assert q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM) is None


def test_finish_item_writes_reason_and_code(queue_env):
    """终态写 `payload.reason/code` 与 `error_type`（常驻化后没有批次行可刷新）。"""
    db, user_id, account, notes = queue_env
    ok_row, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
    bad_row, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[1].id, account.id)
    q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    q.finish_item(db, ok_row, ok=True)
    q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    q.finish_item(db, bad_row, ok=False, reason="平台返回空正文", code="EMPTY_CONTENT")

    db.refresh(bad_row)
    assert bad_row.status == "failed"
    assert bad_row.error_type == "EMPTY_CONTENT"
    assert bad_row.payload["reason"] == "平台返回空正文"
    assert bad_row.payload["code"] == "EMPTY_CONTENT"
    assert bad_row.finished_at is not None
    db.refresh(ok_row)
    assert ok_row.status == "done" and ok_row.progress == 100


# --------------------------------------------------------------------------- #
# 3) 快照（进度卡契约；2026-09-26 常驻化重定义）
# --------------------------------------------------------------------------- #
def test_snapshot_shape_matches_the_frontend_contract(queue_env):
    """形状：running / queued / total / finished（**没有计数字段** —— 本轮成败由前端累计）。"""
    db, user_id, account, notes = queue_env
    row_a, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
    row_b, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[1].id, account.id)
    q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM)  # 跑 row_a
    q.finish_item(db, row_a, ok=True)

    snap = q.snapshot(db, user_id=user_id, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    assert set(snap) == {"running", "queued", "total", "finished"}
    assert snap["running"] is None
    assert [entry["note_pk"] for entry in snap["queued"]] == [notes[1].id], "没跑的 = queued"
    assert snap["queued"][0]["title"] == "标题-n2", "标题本地库取（前端直接显示）"
    assert snap["total"] == 2, "total = 待跑 + 近期终态"
    assert snap["finished"][0]["ok"] is True and snap["finished"][0]["status"] == "ok"

    # 正在跑的那条：进 running，且从 queued 消失（总数不翻倍）
    q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    snap2 = q.snapshot(db, user_id=user_id, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    assert snap2["running"]["note_pk"] == notes[1].id
    assert snap2["queued"] == []
    assert snap2["total"] == 2, "running 顶上来的同时 queued 少一条"
    assert row_b.status == "running"


def test_snapshot_finished_window_is_bounded(queue_env):
    """`finished` 只回近期 `FINISHED_WINDOW` 条（响应体有界，累积结果靠前端）。"""
    db, user_id, account, notes = queue_env
    for index in range(q.FINISHED_WINDOW + 5):
        row, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
        q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM)
        q.finish_item(db, row, ok=True)
    snap = q.snapshot(db, user_id=user_id, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    assert len(snap["finished"]) == q.FINISHED_WINDOW
    assert snap["total"] == q.FINISHED_WINDOW


def test_snapshot_is_scoped_to_the_owner(queue_env, db_factory):
    """别人的队列项**一条都不出现**（DEF-066 同规）。"""
    db, user_id, account, notes = queue_env
    _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)

    intruder = User(username="queue-intruder", password_hash="x")
    db.add(intruder)
    db.commit()
    snap = q.snapshot(db, user_id=intruder.id, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    assert snap["total"] == 0 and snap["queued"] == [] and snap["running"] is None


# --------------------------------------------------------------------------- #
# 3.5) 进度卡**当日三数**（2026-09-26 用户口径「只统计当日的数据……统计队列记录表
#      中的数据」）—— 两张卡的卡面数字都从这里来
# --------------------------------------------------------------------------- #
def test_today_counts_use_the_day_window_and_skip_cancelled(queue_env):
    """当日三数：成功 / 失败 / 待处理，**跨天不串、取消不记**。

    用户口径原文（2026-09-26）：「内容库页的同步进度卡和笔记发布进度卡，只统计当日
    发布或同步的数据，在卡片右下角小字展示仅当日数据，统计队列记录表中的数据」。
    """
    db, user_id, account, notes = queue_env
    item = q.TASK_TYPE_SYNC_ITEM

    done, _ = _enqueue(db, user_id, item, notes[0].id, account.id)
    q.claim_next_item(db, item_task_type=item)
    q.finish_item(db, done, ok=True)

    failed, _ = _enqueue(db, user_id, item, notes[1].id, account.id)
    q.claim_next_item(db, item_task_type=item)
    q.finish_item(db, failed, ok=False, reason="平台拒绝", code="SYNC_REJECTED")

    # 还在队列里的一条 ⇒ 待处理
    _enqueue(db, user_id, item, notes[2].id, account.id)

    # 昨天跑完的一条：**不进今天**（终态按 finished_at 收口）
    yesterday, _ = _enqueue(db, user_id, item, _add_note(db, user_id, account, 1).id, account.id)
    yesterday.status = "done"
    yesterday.finished_at = shanghai_now() - timedelta(days=1)
    # 取消的一条：既不是成功也不是失败 ⇒ 一档都不进（硬塞会伪造成败）
    cancelled, _ = _enqueue(db, user_id, item, _add_note(db, user_id, account, 2).id, account.id)
    cancelled.status = "cancelled"
    db.commit()

    counts = q.today_item_counts(db, user_id=user_id, item_task_type=item)
    assert counts == {
        "date": shanghai_now().date().isoformat(),
        "ok": 1,
        "failed": 1,
        "pending": 1,
    }, counts
    # 另一个队列（发布）不被串进来（按 item_task_type 收口）
    assert q.today_item_counts(db, user_id=user_id, item_task_type=q.TASK_TYPE_PUBLISH_ITEM)["ok"] == 0


def test_today_counts_are_scoped_to_the_owner(queue_env, db_factory):
    """别人的当日队列数**不进我的卡**（DEF-066 同规）。"""
    db, user_id, account, notes = queue_env
    row, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
    q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    q.finish_item(db, row, ok=True)

    intruder = User(username="today-intruder", password_hash="x")
    db.add(intruder)
    db.commit()
    counts = q.today_item_counts(db, user_id=intruder.id, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    assert counts["ok"] == 0 and counts["failed"] == 0 and counts["pending"] == 0


def test_progress_card_snapshots_carry_today_counts(queue_env):
    """**接线**：两张进度卡的快照（发布 / 同步）都必须带上 `today`（卡面数字的唯一来源）。

    少任何一个键，前端 `xxx.today.ok` 读出来是 `undefined` ⇒ 卡上直接显示空白 / NaN。
    """
    from backend.app.services import note_publish_service as nps
    from backend.app.services import note_sync_service as nss

    db, user_id, account, notes = queue_env
    row, _ = _enqueue(
        db,
        user_id,
        q.TASK_TYPE_PUBLISH_ITEM,
        notes[0].id,
        account.id,
        source=q.SOURCE_NOTE_PUBLISH_SINGLE,
    )
    q.claim_next_item(db, item_task_type=q.TASK_TYPE_PUBLISH_ITEM)
    q.finish_item(db, row, ok=True)

    publish = nps.publish_queue_snapshot(db, user_id)
    assert set(publish) == {"running", "queued", "total", "today"}
    assert publish["today"]["ok"] == 1 and publish["today"]["pending"] == 0

    sync = nss.sync_queue_snapshot(db, user_id)
    assert set(sync) == {"running", "queued", "total", "finished", "today"}
    assert sync["today"]["ok"] == 0 and sync["today"]["pending"] == 0
    assert sync["today"]["date"] == shanghai_now().date().isoformat()


# --------------------------------------------------------------------------- #
# 4) 重启续跑（只自动重试「中断」）
# --------------------------------------------------------------------------- #
def test_recover_requeues_interrupted_items(queue_env):
    """进程退出留下的 `running` 行 ⇒ 回到 `pending` 并计一次重试（自动续跑）。"""
    db, user_id, account, notes = queue_env
    row, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
    q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    db.refresh(row)
    assert row.status == "running"

    result = q.recover_interrupted(db, item_task_types=(q.TASK_TYPE_SYNC_ITEM,))
    assert result == {"requeued": 1, "failed": 0}
    db.refresh(row)
    assert row.status == "pending" and row.retry_count == 1 and row.started_at is None
    assert q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM).id == row.id, "能重新认领"


def test_recover_gives_up_when_retries_exhausted(queue_env):
    """重试用尽的中断行 ⇒ `failed` + `error_type=interrupted`（不无限循环）。"""
    db, user_id, account, notes = queue_env
    row, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
    q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    db.refresh(row)
    row.retry_count = row.max_retries
    db.commit()

    result = q.recover_interrupted(db, item_task_types=(q.TASK_TYPE_SYNC_ITEM,))
    assert result == {"requeued": 0, "failed": 1}
    db.refresh(row)
    assert row.status == "failed"
    assert row.error_type == q.ERROR_INTERRUPTED
    assert row.payload["code"] == "INTERRUPTED"
    assert "中断" in row.payload["reason"]


def test_recover_never_retries_business_failures(queue_env):
    """**业务失败不自动重试**（用户口径）：已终态的行恢复扫描一律不动。"""
    db, user_id, account, notes = queue_env
    row, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
    q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    q.finish_item(db, row, ok=False, reason="平台拒绝", code="PLATFORM_REJECTED")
    result = q.recover_interrupted(db, item_task_types=(q.TASK_TYPE_SYNC_ITEM,))
    assert result == {"requeued": 0, "failed": 0}, "业务失败不是「中断」，不许被自动重跑"
    db.refresh(row)
    assert row.status == "failed" and row.error_type == "PLATFORM_REJECTED"


def test_recover_resets_notes_stuck_in_publishing(queue_env):
    """启动恢复的第二半：卡在 `publishing` 的笔记行复位成 `pending`（现有缺口）。"""
    from backend.app.models.note import PUBLISH_STATUS_PUBLISHED, PUBLISH_STATUS_PUBLISHING

    db, user_id, account, notes = queue_env
    notes[0].publish_status = PUBLISH_STATUS_PUBLISHING
    notes[1].publish_status = PUBLISH_STATUS_PUBLISHED
    db.commit()

    assert q.recover_publishing_notes(db) == 1
    db.refresh(notes[0])
    db.refresh(notes[1])
    assert notes[0].publish_status == "pending", "僵尸 publishing 必须复位（否则永远卡住）"
    assert notes[1].publish_status == PUBLISH_STATUS_PUBLISHED, "已发布的不许被碰"


# --------------------------------------------------------------------------- #
# 5) 保留期清理（定时任务 system.queue_record_cleanup）
# --------------------------------------------------------------------------- #
def test_cleanup_only_deletes_terminal_records_past_retention(queue_env):
    """只删**终态 + 超期**：15 天内的不删、`pending`/`running` 永不删。"""
    db, user_id, account, notes = queue_env
    old = shanghai_now() - timedelta(days=q.DEFAULT_RETENTION_DAYS + 1)
    fresh_done, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
    q.claim_next_item(db, item_task_type=q.TASK_TYPE_SYNC_ITEM)
    q.finish_item(db, fresh_done, ok=True)
    old_done, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[1].id, account.id)
    old_done.created_at = old
    old_done.status = "done"
    old_done.finished_at = old
    db.commit()

    result = q.cleanup_records(db, user_id=user_id)
    assert result["retention_days"] == 15
    assert result["items"] == 1, "只有「终态 + 超期」那一条被删"
    assert result["batches"] == 0, "没有批次行可清（这条记录没有父行）"
    remaining = {row.id for row in _items(db, q.TASK_TYPE_SYNC_ITEM)}
    assert remaining == {fresh_done.id}, "15 天内 / 未终态的一律留下"


def test_cleanup_never_touches_live_items(queue_env):
    """活的队列项（`pending` / `running`）**哪怕很老也不许删** —— 清理不能变成丢任务。"""
    db, user_id, account, notes = queue_env
    old = shanghai_now() - timedelta(days=40)
    stale_pending, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
    stale_pending.created_at = old
    running_row, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[1].id, account.id)
    running_row.created_at = old
    running_row.status = "running"
    db.commit()

    result = q.cleanup_records(db, user_id=user_id)
    assert result["items"] == 0, "活的任务一条都不许删"
    assert {row.status for row in _items(db, q.TASK_TYPE_SYNC_ITEM)} == {"pending", "running"}


def test_cleanup_is_user_scoped(queue_env, db_factory):
    """只清**行主本人**的记录（DEF-066）：别家的行一条不动。"""
    db, user_id, account, notes = queue_env
    old = shanghai_now() - timedelta(days=40)
    mine = Task(
        user_id=user_id,
        platform="xhs",
        task_type=q.TASK_TYPE_SYNC_ITEM,
        status="done",
        payload={"note_pk": notes[0].id},
        created_at=old,
    )
    other_user = User(username="queue-other", password_hash="x")
    db.add(other_user)
    db.flush()
    theirs = Task(
        user_id=other_user.id,
        platform="xhs",
        task_type=q.TASK_TYPE_SYNC_ITEM,
        status="done",
        payload={"note_pk": notes[1].id},
        created_at=old,
    )
    db.add_all([mine, theirs])
    db.commit()
    # ⚠️ id 必须在清理**之前**取：`commit()` 会 expire 全部实例，清理删行后再访问
    #    `mine.id` 会触发一次加载 → 行已不在 ⇒ ObjectDeletedError（不是断言失败）。
    mine_id, theirs_id = mine.id, theirs.id

    result = q.cleanup_records(db, user_id=user_id)
    assert result["items"] == 1
    fresh = db_factory()
    try:
        assert fresh.get(Task, mine_id) is None, "自己的超期终态记录被清"
        assert fresh.get(Task, theirs_id) is not None, "别家的记录一条都不许动"
    finally:
        fresh.close()


def test_cleanup_is_independent_of_unrelated_task_types(queue_env):
    """白名单之外的任务类型（ai_* / 抓取监控）不在清理范围内。

    ⚠️ 2026-09-27 被 §35.16 取代：**账号级任务（`xhs_note_sync` /
    `xhs_shop_goods_sync` / `xhs_no_traffic_cleanup`）现在与队列记录共用保留期**
    （`ACCOUNT_TASK_TYPES` 并入 `QUEUE_RECORD_TYPES`），所以它们**会被清理** ——
    本用例原先把 `xhs_shop_goods_sync` 当「别处口径」断言不许删，那是旧边界；
    现行边界只剩「真·无关类型」：AI 生成 / 抓取监控 这类不在任何队列里的行。
    """
    db, user_id, account, notes = queue_env
    old = shanghai_now() - timedelta(days=40)
    unrelated_types = ("ai_generate_note", "monitoring_crawl")
    unrelated_ids = []
    for task_type in unrelated_types:
        row = Task(
            user_id=user_id,
            platform="xhs",
            task_type=task_type,
            status="done",
            payload={},
            created_at=old,
        )
        db.add(row)
        db.commit()
        unrelated_ids.append(row.id)
    q.cleanup_records(db, user_id=user_id)
    for task_type, row_id in zip(unrelated_types, unrelated_ids):
        assert db.get(Task, row_id) is not None, (
            f"别处的任务记录有别的保留口径，不许连带删（{task_type}）"
        )


def test_cleanup_handler_uses_its_own_session_and_default_retention(queue_env, db_factory):
    """定时任务入口：自建 Session（线程里跑）、固定 15 天、只清行主本人。"""
    db, user_id, account, notes = queue_env
    old = shanghai_now() - timedelta(days=40)
    row = Task(
        user_id=user_id,
        platform="xhs",
        task_type=q.TASK_TYPE_PUBLISH_ITEM,
        status="done",
        payload={"note_pk": notes[0].id},
        created_at=old,
    )
    db.add(row)
    db.commit()
    row_id = row.id

    result = q.cleanup_queue_records(user_id)
    assert result["retention_days"] == q.DEFAULT_RETENTION_DAYS == 15
    assert result["items"] >= 1
    fresh = db_factory()
    try:
        assert fresh.get(Task, row_id) is None, "入口真的走了库（不是空转）"
    finally:
        fresh.close()


# --------------------------------------------------------------------------- #
# 6) 种子与注册表（随包初始化任务）
# --------------------------------------------------------------------------- #
def test_cleanup_job_is_seeded_as_daily_cron_at_seven():
    """初始化任务：`system.queue_record_cleanup` 每天 07:00、cron、active、内置。

    ⚠️ **2026-10-05 用户口径取代了两条旧断言**（原句保留在下方 docstring）：
    「将队列清理任务改造升级为公共的清理记录表任务 `system.queue_record_cleanup`……
    优化定时任务支持携带参数，将清理记录表任务、记录表保留时间设置为任务携带的参数，
    每次执行任务时可修改，默认记录表保留最新 15 天」。

    * 中文名 `队列记录清理` → **`清理记录表`**（2026-09-25 那条「名改为队列记录清理」
      的口径**被本次取代**：任务范围从「队列记录」扩到「所有指定记录表」）；
    * `params_editable == ()` → **`("retention_days",)`**（2026-09-27 的
      「保留期是固定口径，不做成可调项，可调会让『能翻多久的历史』每人不同」
      **被本次取代**：用户明确要求做成任务参数、每次执行可改）。
    """
    from backend.app.core.initial_data import load_initial_scheduled_jobs
    from backend.app.services.job_registry import job_registry

    spec = job_registry()["system.queue_record_cleanup"]
    assert spec.builtin is True, "必须随包种子（装完即有）"
    assert spec.user_scoped is True, "执行函数收 user_id 并只清行主本人的记录"
    assert spec.default_trigger_type == "cron"
    assert spec.default_cron == "0 0 7 * * *", "每天 7 点（6 位 cron：秒 分 时 日 月 周）"
    assert spec.params_editable == ("retention_days",), "保留期是任务参数，建 / 改都可配"
    assert spec.name == "清理记录表"

    seeded = {row["job_key"]: row for row in load_initial_scheduled_jobs()}
    row = seeded["system.queue_record_cleanup"]
    assert row["name"] == "清理记录表", "种子行的中文名与键同步改（唯一真相源是 config JSON）"
    assert row["trigger_type"] == "cron"
    assert row["cron_expression"] == "0 0 7 * * *"
    assert row["status"] == "active"
    # 默认保留期随种子显式下发（用户口径「默认记录表保留最新 15 天」）——
    # 缺它时执行侧会退同一默认值，但界面上就看不到「当前是几天」。
    assert row["params"] == {"retention_days": 15}


def test_queue_registry_drives_the_records_page():
    """注册表是**队列清单的唯一来源**：新增队列只加一行，前端零改动。

    盯三件事：① 清单非空且每项字段自洽（队列项类型 + 批次父行类型 + 中文名）；
    ② `queue_spec_for` 只认**队列项**（非队列类型返 None：账号级同步 `xhs_note_sync`
    不是队列 —— 它进账号卡片 / 任务日志，不进队列记录页），而
    `queue_spec_for_any` **连批次行一起认**（记录页要按批次行归队列做筛选）；
    ③ 注册表与 `QUEUE_ITEM_TASK_TYPES` / `QUEUE_BATCH_TASK_TYPES` 一致
    （worker 认领只认前者，漂移了就静默漏队列）。
    2026-09-26 批次层（用户口径「批次逻辑不再移除，生成唯一批次号」）：
    `batch_task_type` 回来了 —— 一次入队动作 = 一个批次行。
    """
    specs = q.list_queue_specs()
    assert specs, "注册表不能为空（否则队列记录页什么都没有）"
    for spec in specs:
        assert spec.key and spec.label, "队列键 / 中文名都不能空（前端直接显示 label）"
        assert spec.item_task_type, "队列项类型不能空"
        assert spec.batch_task_type, "批次父行类型不能空（每次入队动作要有地方记账）"
        assert q.queue_spec_for(spec.item_task_type) is spec
        assert q.queue_spec_for_any(spec.batch_task_type) is spec, "批次行也要能落回它的队列"

    assert q.queue_spec_for("xhs_note_sync") is None, "账号级全量同步不是队列"
    assert q.queue_spec_for("xhs_note_sync_batch") is None, "`queue_spec_for` 只认队列项（不是队列项 ⇒ None）"
    assert q.queue_spec_for_any("xhs_note_sync_batch") is not None, "批次行要能被解析到队列"
    assert q.queue_spec_for_any("xhs_note_sync") is None, "账号级同步行不是队列（两种解析都不认）"
    assert q.queue_spec_for("ai_generate_note") is None, "AI 生成不是队列"
    assert q.queue_spec_for("mystery") is None, "表外类型一律 None（不猜）"
    assert q.queue_spec_for_any("mystery") is None, "表外类型一律 None（不猜）"

    assert {spec.item_task_type for spec in specs} == set(q.QUEUE_ITEM_TASK_TYPES)
    assert {spec.batch_task_type for spec in specs} == set(q.QUEUE_BATCH_TASK_TYPES)
    assert not set(q.QUEUE_ITEM_TASK_TYPES) & set(q.QUEUE_BATCH_TASK_TYPES), (
        "队列项与批次行类型不许重叠（重叠 = 批次行会被 worker 认领）"
    )


def test_queue_summaries_are_user_scoped_and_bounded(queue_env, db_factory):
    """`GET /tasks/queues` 的计数：按用户收口 + 只统计**保留期内**（15 天）。

    超出保留期的行不该被算进「近 15 天」—— 否则清理任务删了记录、卡片数字还在，
    用户会以为记录还在（页面文案因此说「近 15 天」而不是「累计」）。
    """
    db, user_id, account, notes = queue_env
    stale_at = shanghai_now() - timedelta(days=20)
    db = db_factory()
    try:
        spec = q.list_queue_specs()[0]
        for status, when in (
            ("done", None),
            ("failed", None),
            ("pending", None),
            ("done", stale_at),
        ):
            row = Task(
                user_id=user_id,
                platform="xhs",
                task_type=spec.item_task_type,
                status=status,
                payload={"note_pk": 1},
                created_at=when or shanghai_now(),
            )
            db.add(row)
        # 别人的一行终态：不许被算进本用户的统计（DEF-066）
        other = db.scalars(select(User).where(User.username == "other")).first()
        if other is None:
            other = User(username="other", password_hash="x")
            db.add(other)
            db.flush()
        db.add(
            Task(
                user_id=other.id,
                platform="xhs",
                task_type=spec.item_task_type,
                status="done",
                payload={"note_pk": 2},
            )
        )
        db.commit()

        summaries = {row["key"]: row for row in q.queue_summaries(db, user_id=user_id)}
        mine = summaries[spec.key]
        assert mine["open"] == 1, "待处理 / 在跑各算 open"
        assert mine["done"] == 1, "只数保留期内的终态（超期的那行不算）"
        assert mine["failed"] == 1
        assert mine["label"] == spec.label
        assert mine["batch_task_type"] == spec.batch_task_type, "队列清单下发批次类型（前端按它归批次行）"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 7) 商品生成队列（§31.10：不绑定笔记 ⇒ item_id 维度幂等 + payload 展示体）
# --------------------------------------------------------------------------- #
def test_gen_queue_enqueue_is_idempotent_by_item_id(queue_env):
    """生成项**没有 note_pk**（生成前笔记不存在）⇒ 幂等判据是 `item_id`。

    只认 `note_pk` 会把所有生成项互为「重复」—— 第二个商品永远入不了队。
    """
    db, user_id, account, notes = queue_env
    first, created_first = q.enqueue_item(
        db,
        user_id=user_id,
        item_task_type=q.TASK_TYPE_GEN_ITEM,
        account_id=account.id,
        item_id="ITEM-1",
        item_name="测试桌布",
        source=q.SOURCE_PRODUCT_NOTE_GENERATE_SINGLE,
    )
    assert created_first is True
    assert q.note_pk_of(first) == 0, "生成项不绑定笔记"
    assert q.item_ref_of(first) == "ITEM-1"

    again, created_again = q.enqueue_item(
        db,
        user_id=user_id,
        item_task_type=q.TASK_TYPE_GEN_ITEM,
        account_id=account.id,
        item_id="ITEM-1",
        item_name="测试桌布",
        source=q.SOURCE_PRODUCT_NOTE_GENERATE_SINGLE,
    )
    assert created_again is False and again.id == first.id, "同商品未终态 ⇒ 不重复生成"

    # 不同商品照常入队（判据不是「生成队列里只能有一条」）
    other, created_other = q.enqueue_item(
        db,
        user_id=user_id,
        item_task_type=q.TASK_TYPE_GEN_ITEM,
        account_id=account.id,
        item_id="ITEM-2",
        source=q.SOURCE_PRODUCT_NOTE_GENERATE_BATCH,
    )
    assert created_other is True
    db.commit()
    assert len(_items(db, q.TASK_TYPE_GEN_ITEM)) == 2

    # 终态之后同一商品可再次生成（与另两个队列同口径）
    q.finish_item(db, first, ok=True)
    third, created_third = q.enqueue_item(
        db,
        user_id=user_id,
        item_task_type=q.TASK_TYPE_GEN_ITEM,
        account_id=account.id,
        item_id="ITEM-1",
        source=q.SOURCE_PRODUCT_NOTE_GENERATE_SINGLE,
    )
    assert created_third is True


def test_gen_queue_snapshot_entries_come_from_payload(queue_env):
    """生成队列快照条目走 `item_id` / `item_name`（终态后 `note_title` 优先）。

    笔记绑定队列的条目形状**不变**（三键）—— 两条路都不许把项丢掉（进度卡少一条
    = 用户看到「总数对不上」）。
    """
    db, user_id, account, notes = queue_env
    row_a, _ = q.enqueue_item(
        db,
        user_id=user_id,
        item_task_type=q.TASK_TYPE_GEN_ITEM,
        account_id=account.id,
        item_id="ITEM-1",
        item_name="测试桌布",
        source=q.SOURCE_PRODUCT_NOTE_GENERATE_SINGLE,
    )
    row_b, _ = q.enqueue_item(
        db,
        user_id=user_id,
        item_task_type=q.TASK_TYPE_GEN_ITEM,
        account_id=account.id,
        item_id="ITEM-2",
        item_name="第二件",
        source=q.SOURCE_PRODUCT_NOTE_GENERATE_BATCH,
    )
    db.commit()
    q.claim_next_item(db, item_task_type=q.TASK_TYPE_GEN_ITEM)  # 跑 ITEM-1
    q.finish_item(db, row_a, ok=True, extra={"note_id": "local_abc", "note_title": "测试桌布真好用"})
    q.claim_next_item(db, item_task_type=q.TASK_TYPE_GEN_ITEM)  # 跑 ITEM-2

    snap = q.snapshot(db, user_id=user_id, item_task_type=q.TASK_TYPE_GEN_ITEM)
    assert set(snap) == {"running", "queued", "total", "finished"}, "顶层形状不变"
    assert snap["running"] is not None and snap["running"]["item_id"] == "ITEM-2"
    assert snap["running"]["title"] == "第二件", "排队中显示商品名"
    assert snap["total"] == 2
    done = snap["finished"][0]
    assert done["item_id"] == "ITEM-1" and done["ok"] is True
    assert done["title"] == "测试桌布真好用", "终态后优先显示生成的笔记标题"
    assert done["note_id"] == "local_abc", "extra 写进行体（详情抽屉显示「生成了哪篇」）"


def test_gen_queue_snapshot_is_scoped_to_the_owner(queue_env, db_factory):
    """别人的生成队列一条都不出现（DEF-066 同规）。"""
    db, user_id, account, notes = queue_env
    q.enqueue_item(
        db,
        user_id=user_id,
        item_task_type=q.TASK_TYPE_GEN_ITEM,
        account_id=account.id,
        item_id="ITEM-1",
        item_name="测试桌布",
        source=q.SOURCE_PRODUCT_NOTE_GENERATE_SINGLE,
    )
    db.commit()

    intruder = User(username="gen-intruder", password_hash="x")
    db.add(intruder)
    db.commit()
    snap = q.snapshot(db, user_id=intruder.id, item_task_type=q.TASK_TYPE_GEN_ITEM)
    assert snap["total"] == 0 and snap["queued"] == [] and snap["running"] is None


def test_cleanup_covers_the_gen_queue_from_the_registry(queue_env, db_factory):
    """保留期清理的范围**从注册表派生**：新队列（商品生成）自动被收进来。

    写死两个类型 = 第三种队列的记录永远不清（静默漏项）。终态超期才删，
    活任务永不删。
    """
    db, user_id, account, notes = queue_env
    stale_at = shanghai_now() - timedelta(days=20)
    db = db_factory()
    try:
        row_done, _ = q.enqueue_item(
            db,
            user_id=user_id,
            item_task_type=q.TASK_TYPE_GEN_ITEM,
            account_id=account.id,
            item_id="ITEM-DONE",
            source=q.SOURCE_PRODUCT_NOTE_GENERATE_SINGLE,
        )
        row_open, _ = q.enqueue_item(
            db,
            user_id=user_id,
            item_task_type=q.TASK_TYPE_GEN_ITEM,
            account_id=account.id,
            item_id="ITEM-OPEN",
            source=q.SOURCE_PRODUCT_NOTE_GENERATE_SINGLE,
        )
        row_stale, _ = q.enqueue_item(
            db,
            user_id=user_id,
            item_task_type=q.TASK_TYPE_GEN_ITEM,
            account_id=account.id,
            item_id="ITEM-STALE",
            source=q.SOURCE_PRODUCT_NOTE_GENERATE_SINGLE,
        )
        db.commit()
        q.finish_item(db, row_done, ok=True)
        # 直接改创建时间造「超期终态」（finish_item 只写终态，不动 created_at）
        row_stale.created_at = stale_at
        row_stale.status = "done"
        row_stale.finished_at = stale_at
        db.commit()

        result = q.cleanup_records(db, user_id=user_id)
        assert result["items"] >= 1, "超期的生成终态行被清掉"
        remaining = {q.item_ref_of(row) for row in _items(db, q.TASK_TYPE_GEN_ITEM)}
        assert "ITEM-DONE" in remaining, "保留期内的终态不动"
        assert "ITEM-OPEN" in remaining, "未终态的生成项永不删（清理不能变成丢任务入口）"
        assert "ITEM-STALE" not in remaining, "超期终态被删"
        assert q.TASK_TYPE_GEN_ITEM in q.QUEUE_RECORD_TYPES
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 10) 来源与入队落库（2026-09-26 用户口径）
# --------------------------------------------------------------------------- #
def test_enqueue_requires_button_level_source(queue_env):
    """`source` **必填、无默认值**：每个入口都必须显式声明自己是哪个按钮。

    给默认值 = 允许「忘了传还静悄悄显示手动」—— 那正是用户投诉的来源列。
    """
    db, user_id, account, notes = queue_env
    with pytest.raises(ValueError, match="source 必填"):
        q.enqueue_item(
            db,
            user_id=user_id,
            item_task_type=q.TASK_TYPE_SYNC_ITEM,
            note_pk=notes[0].id,
            source="",
        )
    with pytest.raises(ValueError, match="source 必填"):
        q.enqueue_item(
            db,
            user_id=user_id,
            item_task_type=q.TASK_TYPE_SYNC_ITEM,
            note_pk=notes[0].id,
            source="   ",
        )
    assert _items(db, q.TASK_TYPE_SYNC_ITEM) == [], "没 source 的行一条都不该落库"


def test_enqueue_writes_button_level_source(queue_env):
    """入队即落库按钮级 source（用户口径「来源要具体到按钮」）。"""
    db, user_id, account, notes = queue_env
    row, _ = _enqueue(
        db,
        user_id,
        q.TASK_TYPE_SYNC_ITEM,
        notes[0].id,
        account.id,
        source=q.SOURCE_NOTE_SYNC_SINGLE,
    )
    assert row.payload.get("source") == "note_sync_single"

    row2, _ = _enqueue(
        db,
        user_id,
        q.TASK_TYPE_SYNC_ITEM,
        notes[1].id,
        account.id,
        source=q.SOURCE_NOTE_SYNC_BATCH,
    )
    assert row2.payload.get("source") == "note_sync_batch"


def test_enqueue_snapshots_note_title_for_note_bound_queues(queue_env):
    """入队即完整落库（2026-09-26 用户口径「进入队列的数据先进行持久化」）：

    同步 / 发布队列项的行上带 `note_title` 快照 —— 队列记录页直接显示主体，
    不为每行回头拉笔记；缺省不写（不造假）。
    """
    db, user_id, account, notes = queue_env
    row, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id, note_title="")
    assert "note_title" not in row.payload, "没给标题就不写（缺值口径）"

    row2, _ = _enqueue(
        db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[1].id, account.id, note_title=notes[1].title
    )
    assert row2.payload.get("note_title") == "标题-n2"


def test_recovery_marks_requeued_rows_as_recovery(queue_env):
    """启动恢复复位的断点行，来源改标 `recovery`（这一跑是重启续跑，不是用户当时点的）。"""
    db, user_id, account, notes = queue_env
    row, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
    row.status = "running"  # 模拟进程退出留下的断点
    db.commit()

    result = q.recover_interrupted(db)
    assert result["requeued"] == 1
    db.refresh(row)
    assert row.status == "pending"
    assert row.payload.get("source") == q.SOURCE_RECOVERY
    assert row.retry_count == 1


def test_recovery_marks_exhausted_rows_without_touching_source(queue_env):
    """重试用尽的断点行转 failed，但**不改来源**（它没重新入队，没有新入口）。"""
    db, user_id, account, notes = queue_env
    row, _ = _enqueue(db, user_id, q.TASK_TYPE_SYNC_ITEM, notes[0].id, account.id)
    row.status = "running"
    row.retry_count = 3
    row.max_retries = 3
    db.commit()

    result = q.recover_interrupted(db)
    assert result["failed"] == 1
    db.refresh(row)
    assert row.status == "failed"
    assert row.payload.get("source") == q.SOURCE_NOTE_SYNC_SINGLE, "没有新入口就不该改来源"


def test_all_queue_sources_have_frontend_labels():
    """`QUEUE_SOURCES` 的每一项都要在前端 `lib/task-records.ts` 有中文名（防两边漂移）。

    ⚠️ **判据由 `QUEUE_SOURCES` 驱动，不再手写清单**（2026-09-28）：手写清单本身
    就是「同一语义两份实现」（DEF-044），上一版手写 10 项而 `QUEUE_SOURCES` 有 12 项
    ⇒ 两个账号卡来源（`account_card_note_sync` / `account_card_goods_sync`）**真的会
    入队却缺中文名**，页面显示英文原值，而这份测试抓不到。改成遍历真相源后，后端
    新增来源而前端没加中文名 ⇒ 立刻红。

    判据落在**前端源文件**上（读文本，不 import）：只断言「有键」，具体文案由前端
    review —— 后端不硬编码前端文案（那是第二份真相源）。
    """
    lib = (ROOT / "frontend/src/lib/task-records.ts").read_text(encoding="utf-8")
    assert len(q.QUEUE_SOURCES) >= 10, "真相源退化了？QUEUE_SOURCES 不该这么短"
    # 账号卡两个来源必须在内（上一版漏的就是它们，钉住防再漏）。
    assert "account_card_note_sync" in q.QUEUE_SOURCES
    assert "account_card_goods_sync" in q.QUEUE_SOURCES

    labels_block = lib.split("export const TASK_SOURCE_LABELS")[1].split("};")[0]
    tips_block = lib.split("export const TASK_SOURCE_TIPS")[1].split("};")[0]
    for source in q.QUEUE_SOURCES:
        assert f"{source}:" in labels_block, (
            f"来源 {source} 缺前端中文名（TASK_SOURCE_LABELS）⇒ 页面显示英文原值"
        )
        assert f"{source}:" in tips_block, (
            f"来源 {source} 缺前端 Tooltip 说明（TASK_SOURCE_TIPS）"
        )

    label_fn = lib.split("export function taskSourceLabel")[1].split("}")[0]
    assert "未知" not in label_fn, (
        "taskSourceLabel 不许再返回「未知」（2026-09-26 用户口径「来源列不能是未知」）"
    )
