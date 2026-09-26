"""内容库批量同步 + 同步进度卡（设计 13 §35，2026-09-25）。

这批用例盯的是**队列纪律与门禁**，不是平台行为（平台行为由真机验证记录背书）：

1. **门禁三态**：已发布 ⇒ 入队；未发布 / 发布失败 ⇒ `NOTE_NOT_SYNCABLE` 不入队；
   别人的笔记 ⇒ `NOTE_NOT_FOUND`（不泄漏「存在但无权」）；
2. **FIFO 顺序**：三篇入队按序跑完（闸门把第一篇停在执行中，断言 running/queued）；
3. **失败隔离**：一篇同步抛异常 ⇒ 记 failed 带原因，其余篇照常跑完（安全网不条件性）；
4. **快照计数**：`synced` / `failed` / `total` 与逐篇 `finished`（同步失败不落库到
   笔记行，原因只在快照里给一次）；
5. **多租户隔离**：快照只回当前用户的队列与结果（DEF-066 同规）；
6. **路由注册序**：`GET /notes/sync-queue` 返回 200 而非 422（`/{note_id}` 的 int
   转换会先吃掉 "sync-queue"，publish-queue 的同款教训）；
7. **入队幂等**：已在队列 / 执行中的 note_pk 不重复入队（`already_queued`）；
8. **账号快照**：入队即写 `payload.account_id`（= `note.platform_account_id`）——
   队列记录页「账号」列的数据来源（2026-09-26 用户口径「批次记录执行的账号」）。

与发布队列（`test_product_note_publish.py`）同构不同责：这里逐篇跑的是
`sync_note_from_platform`（唯一实现复用），测试用假替身换掉它、断言队列骨架。
"""
from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import select

from backend.app.models import Note, PlatformAccount, Task, User
from backend.app.models.note import set_note_publish_status
from backend.app.services import note_queue_service, note_sync_service

client = None  # 占位：真正 client 由 api_client 夹具注入（见各用例）


class _FakeSync:
    """`sync_note_from_platform` 的假替身（脚本化，不联网）。

    * `calls`：按**调用顺序**记 note_pk（FIFO 断言用）；
    * `gate` / `reached`：第一个调用先置 `reached` 再等 `gate` —— 把队列停在
      「第一篇执行中、其余排队」的确定态，用来断言快照；
    * `fail_pks`：这些篇抛异常（模拟平台请求炸了）⇒ 验证失败隔离。
    """

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.gate = threading.Event()
        self.reached = threading.Event()
        self.fail_pks: set[int] = set()

    def __call__(self, db, note, *, account=None, adapter_factory=None):
        self.calls.append(note.id)
        if not self.reached.is_set():
            self.reached.set()
            self.gate.wait(timeout=10)
        if note.id in self.fail_pks:
            raise RuntimeError("平台详情超时（测试）")
        return {"status": "ok", "content_updated": True, "reason": "", "code": ""}


@pytest.fixture(autouse=True)
def _drain_sync_queue(monkeypatch, db_factory):
    """用例结束后把 worker 停手并等它跑完手上那条（全局线程 + 库行，不许跨用例泄漏）。

    ⚠️ **顺序与依赖都是关键**：

    1. 夹具**显式请求 `monkeypatch`** ⇒ teardown 时它在 monkeypatch **恢复之前**
       执行。worker 自建 Session 走模块级 `_session_factory`（本文件的用例 patch 成
       临时库）：若先恢复 patch 再收 worker，worker 会拿着**真库**去认领 ——
       那是数据隔离事故，不是测试噪声。
    2. 先 `stop_sync_worker`（不再认领新任务），再等库里没有 `running` 行。
       队列**不再是内存 deque**（持久化后未跑的项是 `pending` 行），所以判据从
       「队列空」改为「没有在跑的项」。
    """
    yield
    note_sync_service.stop_sync_worker(timeout=10)
    deadline = time.time() + 10
    while time.time() < deadline:
        db = db_factory()
        try:
            still_running = note_queue_service.list_note_pks_by_status(
                db,
                item_task_type=note_queue_service.TASK_TYPE_SYNC_ITEM,
                statuses=("running",),
            )
        finally:
            db.close()
        if not still_running:
            return
        time.sleep(0.05)
    pytest.fail("同步 worker 在用例结束后 10s 仍在跑（worker 卡死？）")


def _register(api_client, username: str) -> dict[str, str]:
    response = api_client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _make_account(db_factory, user_id: int, nickname: str = "测试店") -> int:
    db = db_factory()
    try:
        account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="creator", nickname=nickname, status="active"
        )
        db.add(account)
        db.commit()
        return account.id
    finally:
        db.close()


def _make_note(
    db_factory,
    user_id: int,
    account_id: int,
    note_id: str,
    *,
    publish_status: str = "published",
    title: str = "",
) -> int:
    """落一条笔记（返回本地主键）。发布状态经唯一入口写（投影一起维护）。"""
    db = db_factory()
    try:
        note = Note(
            user_id=user_id,
            platform_account_id=account_id,
            platform="xhs",
            note_id=note_id,
            title=title or note_id,
            note_type="synced",
        )
        db.add(note)
        db.commit()
        set_note_publish_status(note, publish_status)
        db.commit()
        return note.id
    finally:
        db.close()


def _patch_worker(monkeypatch, db_factory, fake: _FakeSync) -> None:
    """把 worker 的 Session 工厂与单篇同步实现都换成测试替身。"""
    monkeypatch.setattr(note_sync_service, "_session_factory", db_factory)
    monkeypatch.setattr(note_sync_service, "sync_note_from_platform", fake)


def test_batch_sync_gates_reject_unpublished_and_foreign_notes(api_client, db_factory, monkeypatch):
    """门禁三态：已发布入队 / 未发布 NOTE_NOT_SYNCABLE / 别人的 NOTE_NOT_FOUND。"""
    headers = _register(api_client, "batch-sync-owner")
    db = db_factory()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "batch-sync-owner"))
    finally:
        db.close()
    account_id = _make_account(db_factory, user_id)

    published_pk = _make_note(db_factory, user_id, account_id, "pub-note-1")
    pending_pk = _make_note(db_factory, user_id, account_id, "pending-note-1", publish_status="pending")
    failed_pk = _make_note(db_factory, user_id, account_id, "failed-note-1", publish_status="failed")

    # 别人的笔记（另一个用户 + 账号 + 已发布笔记）
    other_headers = _register(api_client, "batch-sync-stranger")
    db = db_factory()
    try:
        other_user_id = db.scalar(select(User.id).where(User.username == "batch-sync-stranger"))
    finally:
        db.close()
    other_account_id = _make_account(db_factory, other_user_id, nickname="别人的店")
    other_pk = _make_note(db_factory, other_user_id, other_account_id, "other-user-note")

    fake = _FakeSync()
    _patch_worker(monkeypatch, db_factory, fake)

    response = api_client.post(
        "/api/notes/batch-sync",
        json={"note_ids": [published_pk, pending_pk, failed_pk, other_pk, 999999]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 5
    assert body["started"] == 1 and body["failed"] == 4
    by_pk = {item["note_pk"]: item for item in body["results"]}
    assert by_pk[published_pk]["ok"] is True and by_pk[published_pk]["status"] == "queued"
    assert by_pk[pending_pk]["code"] == "NOTE_NOT_SYNCABLE"
    assert by_pk[failed_pk]["code"] == "NOTE_NOT_SYNCABLE"
    # 别人的与不存在的同一个码（不泄漏「存在但无权」）
    assert by_pk[other_pk]["code"] == "NOTE_NOT_FOUND"
    assert by_pk[999999]["code"] == "NOTE_NOT_FOUND"

    # 只有通过篇真进了队列（假替身只被调用过它）
    assert note_sync_service.wait_sync_done(published_pk, timeout=20)
    assert fake.calls == [published_pk]
    assert other_headers  # 另一个用户的令牌确实注册成功（上面用过）


def test_batch_sync_runs_in_fifo_order_with_snapshot(api_client, db_factory, monkeypatch):
    """三篇入队按 FIFO 跑完；快照 running/queued/计数正确（含路由注册序）。"""
    headers = _register(api_client, "batch-sync-fifo")
    db = db_factory()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "batch-sync-fifo"))
    finally:
        db.close()
    account_id = _make_account(db_factory, user_id)
    pks = [
        _make_note(db_factory, user_id, account_id, f"fifo-note-{index}", title=f"队列笔记{index + 1}")
        for index in range(3)
    ]

    fake = _FakeSync()
    _patch_worker(monkeypatch, db_factory, fake)

    response = api_client.post(
        "/api/notes/batch-sync", json={"note_ids": pks}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["started"] == 3 and body["failed"] == 0

    # 闸门把第一篇停在执行中 ⇒ 断言「同步中 1 + 排队 2」的确定态
    assert fake.reached.wait(timeout=10)
    snapshot = api_client.get("/api/notes/sync-queue", headers=headers).json()
    assert snapshot["running"] is not None and snapshot["running"]["note_pk"] == pks[0]
    assert [q["note_pk"] for q in snapshot["queued"]] == pks[1:]
    # 2026-09-26 常驻化：快照没有计数字段（本轮成败由前端累计），total = 待跑 + 近期终态
    assert snapshot["total"] == 3
    assert "synced" not in snapshot and "failed" not in snapshot

    # 账号快照（2026-09-26 用户口径「批次记录执行的账号」）：入队时必须把
    # `note.platform_account_id` 写进 `payload.account_id` —— 同步队列这里原先
    # **漏了**（发布 / 商品生成一直在写），页面上同步记录的「账号」列会全是 `-`。
    db_check = db_factory()
    try:
        queued_rows = db_check.scalars(
            select(Task).where(Task.task_type == note_queue_service.TASK_TYPE_SYNC_ITEM).order_by(Task.id)
        ).all()
        assert [row.payload.get("account_id") for row in queued_rows] == [account_id] * 3
    finally:
        db_check.close()

    fake.gate.set()
    for pk in pks:
        assert note_sync_service.wait_sync_done(pk, timeout=20), f"note_pk={pk} 未跑完"

    # FIFO：调用顺序 = 入队顺序
    assert fake.calls == pks
    snapshot = api_client.get("/api/notes/sync-queue", headers=headers).json()
    assert snapshot["running"] is None and snapshot["queued"] == []
    assert snapshot["total"] == 3, "终态项留在近期窗口里（不是「新一轮」把它清掉）"
    assert len(snapshot["finished"]) == 3
    assert all(entry["ok"] for entry in snapshot["finished"])


def test_sync_queue_route_is_registered_before_note_id(api_client, db_factory):
    """路由注册序：`GET /notes/sync-queue` 是 200 而不是 422。

    "sync-queue" 会被 `GET /notes/{note_id}` 的 int 转换先吃掉 ⇒ 422
    （`publish-queue` 的同款教训，设计 13 §31.5 / §35.3）。
    """
    headers = _register(api_client, "batch-sync-route")
    response = api_client.get("/api/notes/sync-queue", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["running"] is None and body["queued"] == [] and body["total"] == 0


def test_batch_sync_failure_isolates_and_keeps_queue_running(api_client, db_factory, monkeypatch):
    """一篇同步抛异常 ⇒ 记 failed 带原因，其余篇照常跑完（安全网不条件性）。"""
    headers = _register(api_client, "batch-sync-fail")
    db = db_factory()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "batch-sync-fail"))
    finally:
        db.close()
    account_id = _make_account(db_factory, user_id)
    pks = [
        _make_note(db_factory, user_id, account_id, f"fail-note-{index}")
        for index in range(3)
    ]

    fake = _FakeSync()
    fake.fail_pks = {pks[1]}
    _patch_worker(monkeypatch, db_factory, fake)

    response = api_client.post(
        "/api/notes/batch-sync", json={"note_ids": pks}, headers=headers
    )
    assert response.status_code == 200 and response.json()["started"] == 3
    for pk in pks:
        assert note_sync_service.wait_sync_done(pk, timeout=20), f"note_pk={pk} 未跑完"

    assert fake.calls == pks  # 失败没阻断后续篇
    snapshot = api_client.get("/api/notes/sync-queue", headers=headers).json()
    # 2026-09-26 常驻化：没有计数字段；成败看 finished 逐项（前端据此累计「本轮」）
    assert snapshot["total"] == 3
    finished = snapshot["finished"]
    assert sum(1 for entry in finished if entry["ok"]) == 2
    failed = [entry for entry in finished if not entry["ok"]]
    assert len(failed) == 1 and failed[0]["note_pk"] == pks[1]
    assert "平台详情超时（测试）" in failed[0]["reason"]
    # 笔记行不卡任何状态（同步失败不落库到 notes 行 —— 没有 publish_error 持久位）
    db = db_factory()
    try:
        note = db.get(Note, pks[1])
        assert note.publish_status == "published"
    finally:
        db.close()


def test_sync_queue_snapshot_hides_other_users_queue(api_client, db_factory, monkeypatch):
    """多租户隔离：用户 B 的快照看不到用户 A 的队列与结果（DEF-066 同规）。"""
    headers_a = _register(api_client, "batch-sync-user-a")
    headers_b = _register(api_client, "batch-sync-user-b")
    db = db_factory()
    try:
        user_a = db.scalar(select(User.id).where(User.username == "batch-sync-user-a"))
    finally:
        db.close()
    account_id = _make_account(db_factory, user_a)
    pk = _make_note(db_factory, user_a, account_id, "user-a-note")

    fake = _FakeSync()
    _patch_worker(monkeypatch, db_factory, fake)

    response = api_client.post(
        "/api/notes/batch-sync", json={"note_ids": [pk]}, headers=headers_a
    )
    assert response.status_code == 200 and response.json()["started"] == 1
    assert fake.reached.wait(timeout=10)  # 停在执行中（闸门未放行）

    # B 看到的是空队列（一条都不泄漏）
    snapshot_b = api_client.get("/api/notes/sync-queue", headers=headers_b).json()
    assert snapshot_b["running"] is None and snapshot_b["queued"] == []
    assert snapshot_b["total"] == 0 and snapshot_b["finished"] == []
    # A 看到自己的
    snapshot_a = api_client.get("/api/notes/sync-queue", headers=headers_a).json()
    assert snapshot_a["running"] is not None and snapshot_a["running"]["note_pk"] == pk
    assert snapshot_a["total"] == 1

    fake.gate.set()
    assert note_sync_service.wait_sync_done(pk, timeout=20)


def test_duplicate_enqueue_is_idempotent(api_client, db_factory, monkeypatch):
    """重复入队幂等：已在队列 / 执行中的 note_pk 不二次入队（`already_queued`）。"""
    headers = _register(api_client, "batch-sync-dup")
    db = db_factory()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "batch-sync-dup"))
    finally:
        db.close()
    account_id = _make_account(db_factory, user_id)
    pk = _make_note(db_factory, user_id, account_id, "dup-note")

    fake = _FakeSync()
    _patch_worker(monkeypatch, db_factory, fake)

    first = api_client.post(
        "/api/notes/batch-sync", json={"note_ids": [pk]}, headers=headers
    ).json()
    assert first["started"] == 1
    assert fake.reached.wait(timeout=10)  # 第一篇执行中（闸门未放行）

    # 同一篇再来一次：不重复入队，明示 already_queued
    second = api_client.post(
        "/api/notes/batch-sync", json={"note_ids": [pk]}, headers=headers
    ).json()
    assert second["total"] == 1 and second["started"] == 1
    assert second["results"][0]["already_queued"] is True

    snapshot = api_client.get("/api/notes/sync-queue", headers=headers).json()
    assert snapshot["total"] == 1  # 没有变成 2

    fake.gate.set()
    assert note_sync_service.wait_sync_done(pk, timeout=20)
    assert fake.calls == [pk]  # 只真跑过一次
