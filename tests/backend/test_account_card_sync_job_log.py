"""账号卡「同步」按钮 = 手动触发定时任务的「执行一次」（设计 13 §35.17，2026-09-27）。

契约是「**一次点击 → 两处账，且两处账一致**」：

* `tasks` 一行（来源 `account_card_note_sync`）—— 账号卡「同步任务」区块与
  `…/notes/sync/status` 的数据源；
* `scheduled_job_logs` 一行（`trigger_type=manual`）—— 任务中心「任务日志」的数据源，
  **终态与任务行一致**、执行期间 `progress` 有心跳。

四条用例各自钉一个最容易回退的点（每一个都真实踩过或差点踩到）：

① **假成功**：建完行就写 `success` ⇒ 日志「用时 28ms 执行成功」而同步才刚开始。
   判据 = 任务还在 `pending` 时日志必须是 `running`；
② **重复记账**：`reused=True`（已有任务在跑）时又记一条 ⇒ 任务中心执行次数翻倍，
   且同一任务行可能一条 success 一条 failed。判据 = 第二次点击 `log_id is None`
   且日志只有一条；
③ **失败停在 running**：任务 `failed` ⇒ 日志必须也 `failed`（带原因），
   不能永远停在「运行中」（那看起来像卡死）；
④ **进度心跳**：执行期间写中间值，**终态后不得覆写**（否则出现
   「success 但 progress=40」的自相矛盾行）。
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from backend.app.core.time import shanghai_now
from backend.app.models import PlatformAccount, Task, User
from backend.app.models.scheduled_job import ScheduledJob, ScheduledJobLog
from backend.app.services import account_task_service as ats
from backend.app.services import scheduled_job_runner, scheduler_service
from backend.app.services import sync_task_service as sts
from tests.backend.conftest import patch_service_sessions


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _fast_watch_and_no_worker(monkeypatch, db_factory):
    """观测线程按 50ms 轮询（默认 2s 会让每条用例白等）；worker 不起（用例自己控任务状态）。

    ⚠️ 必须显式请求 `monkeypatch`：`TASK_WATCH_POLL_SECONDS` 是**模块属性**，
    在 `_await_task_terminal` 里按调用时读取 —— 不 patch 就只能等 2 秒。

    ⚠️ 打桩**不能用 `api_client` 代替**：本文件有两条用例（进度泵 / 等终态）直接调服务层，
    没有 `api_client`；不打桩时它们读的是**生产库**，而生产库里 `tasks.id=1` 恰好存在
    （状态 `completed`，不在终态集合里）⇒ 用例会**假绿**通过（2026-09-27 实测到）。
    """
    monkeypatch.setattr(scheduled_job_runner, "TASK_WATCH_POLL_SECONDS", 0.05)
    monkeypatch.setattr(ats, "ensure_account_task_worker", lambda *a, **k: None)
    patch_service_sessions(monkeypatch, db_factory)


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


def _make_account(db_factory, user_id: int, nickname: str = "账号卡号") -> int:
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


def _logs(db_factory, job_key: str) -> list[dict]:
    db = db_factory()
    try:
        rows = db.scalars(
            select(ScheduledJobLog)
            .where(ScheduledJobLog.job_key == job_key)
            .order_by(ScheduledJobLog.id)
        ).all()
        return [
            {
                "id": row.id,
                "job_id": row.job_id,
                "user_id": row.user_id,
                "trigger_type": row.trigger_type,
                "status": row.status,
                "progress": row.progress,
                "result": row.result,
                "exception_info": row.exception_info,
                "message": row.message,
                "config_snapshot": row.config_snapshot,
                "started_at": row.started_at,
                "duration_ms": row.duration_ms,
            }
            for row in rows
        ]
    finally:
        db.close()


def _wait_log_terminal(db_factory, log_id: int, *, timeout: float = 10.0) -> dict:
    """等日志行离开 `running`（观测线程是异步的，判定必须等条件而不是盲等）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db = db_factory()
        try:
            row = db.get(ScheduledJobLog, log_id)
            if row is not None and row.status != "running":
                return {"status": row.status, "progress": row.progress, "result": row.result}
        finally:
            db.close()
        time.sleep(0.05)
    pytest.fail(f"日志 #{log_id} 在 {timeout}s 内没有离开 running（观测线程没跑？）")


def _finish_task(
    db_factory,
    task_id: int,
    *,
    status: str,
    note: str = "",
    error: str = "",
    progress: dict | None = None,
) -> None:
    """把任务行推到终态（模拟账号任务 worker 跑完）。

    `progress` 缺省 = 全跑完（7/7）—— 失败用例要传**跑到一半**的数，
    才能验证「失败后保留已执行的比例」（2026-09-27 用户口径）。
    """
    db = db_factory()
    try:
        row = db.get(Task, task_id)
        assert row is not None
        row.status = status
        row.finished_at = shanghai_now()
        # JSON 列必须**整体赋值**才会进脏检查（原地改 payload["x"] 不会）。
        row.payload = {
            **(row.payload or {}),
            "progress": progress or {"stored": 7, "total": 7},
            "note": note,
            "error": error,
        }
        db.commit()
    finally:
        db.close()


def test_click_records_a_manual_log_that_follows_the_task_to_success(api_client, db_factory):
    """①+②：点一次按钮 ⇒ 任务行 + 一条 manual 日志；日志**跟着任务**到 success。"""
    headers = _register(api_client, "card-log-ok")
    user_id = _user_id(db_factory, "card-log-ok")
    account_id = _make_account(db_factory, user_id)

    response = api_client.post(
        f"/api/accounts/{account_id}/notes/sync",
        headers=headers,
        json={"mode": "full"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reused"] is False
    log_id = body["log_id"]
    assert isinstance(log_id, int), "点击必须留下一条定时任务日志（任务中心「任务日志」可见）"

    # 任务行：来源具体到按钮（遗留值 `manual` 会在队列记录页显示成「页面按钮（早期数据）」）
    db = db_factory()
    try:
        task = db.get(Task, int(body["task_id"]))
        assert task is not None and task.payload["source"] == "account_card_note_sync"
        assert task.status == "pending", "端点只建行，执行归账号任务 worker（§35.16 串行）"
        # 同步语义必须落库（worker 从行重建；不落库就丢）
        assert task.payload["mode"] == "full"
    finally:
        db.close()

    # 任务还在 pending ⇒ 日志必须是 running，**不能**已经 success（① 假成功）
    logs = _logs(db_factory, "system.note_sync")
    assert len(logs) == 1, f"一次点击只应记一条日志，实得 {len(logs)}"
    assert logs[0]["status"] == "running", "任务没结束，日志不许先写终态（假成功）"
    assert logs[0]["trigger_type"] == "manual"
    assert logs[0]["user_id"] == user_id
    assert logs[0]["config_snapshot"]["params_override"]["account_id"] == account_id, (
        "日志快照要留下本次实际生效的语义（与任务行各一份才对得上）"
    )
    # ①b 「执行账号」口径（2026-09-27 用户口径「执行账号应是当前账号信息卡的店铺」）：
    # 用户实测报障「账号卡点同步笔记，执行详情的执行账号依旧展示的是全部账号」——
    # 快照照抄 job 行的 `[]`（本人全部账号）就把一次**单账号**执行说成了全部账号。
    snapshot = logs[0]["config_snapshot"]
    assert snapshot["params"]["account_ids"] == [account_id], (
        "定向执行的快照必须收窄到本次那个账号，否则「执行详情」显示「全部账号」"
    )
    assert snapshot["params_override"]["account_name"] == "账号卡号", (
        "快照要留账号名称（详情弹窗按它显示「执行账号」，只有 id 数量说不出是哪个账号）"
    )
    # ⚠️ 收窄的是**日志快照**，不是 job 行 —— 改了 job 行会让下一次定时轮转只剩这一个账号
    db = db_factory()
    try:
        job = db.scalars(
            select(ScheduledJob).where(ScheduledJob.job_key == "system.note_sync")
        ).first()
        assert (job.params or {}).get("account_ids") == [], (
            "任务行的 account_ids 必须原样（定向执行只改日志快照，不改任务配置）"
        )
    finally:
        db.close()

    _finish_task(db_factory, int(body["task_id"]), status="done", note="同步完成")

    final = _wait_log_terminal(db_factory, log_id)
    assert final["status"] == "success", "任务 done ⇒ 日志 success（终态必须一致）"
    assert final["progress"] == 100, "成功即 100"
    assert final["result"]["task_id"] == int(body["task_id"])
    assert final["result"]["note"] == "同步完成"

    logs = _logs(db_factory, "system.note_sync")
    assert logs[0]["duration_ms"] is not None and logs[0]["duration_ms"] >= 0


def test_reused_trigger_does_not_write_a_second_log(api_client, db_factory):
    """② 重复记账：已有任务在跑时再点 ⇒ **不记新日志**（没有新执行就没有新日志）。"""
    headers = _register(api_client, "card-log-reuse")
    user_id = _user_id(db_factory, "card-log-reuse")
    account_id = _make_account(db_factory, user_id)

    first = api_client.post(f"/api/accounts/{account_id}/notes/sync", headers=headers, json={})
    assert first.status_code == 200, first.text
    second = api_client.post(f"/api/accounts/{account_id}/notes/sync", headers=headers, json={})
    assert second.status_code == 200, second.text

    assert second.json()["reused"] is True, "第二次必须复用已有任务（不打平台）"
    assert second.json()["task_id"] == first.json()["task_id"]
    assert second.json()["log_id"] is None, "复用不是新执行 ⇒ 不记日志（否则执行次数翻倍）"
    assert len(_logs(db_factory, "system.note_sync")) == 1


def test_failed_task_marks_the_log_failed_with_the_reason(api_client, db_factory):
    """③ 失败不能停在 running：任务 failed ⇒ 日志 failed + 原因。"""
    headers = _register(api_client, "card-log-fail")
    user_id = _user_id(db_factory, "card-log-fail")
    account_id = _make_account(db_factory, user_id)

    response = api_client.post(f"/api/accounts/{account_id}/notes/sync", headers=headers, json={})
    task_id = int(response.json()["task_id"])
    _finish_task(
        db_factory,
        task_id,
        status="failed",
        error="CREATOR_SESSION_REQUIRED：该账号没有创作者平台登录会话",
        # 跑到一半就失败了（3/7）—— 用来验证「已执行的进度比例保留」。
        progress={"stored": 3, "total": 7},
    )

    body = _wait_log_terminal(db_factory, int(response.json()["log_id"]))
    assert body["status"] == "failed"
    # ⚠️ 2026-09-27 用户口径「任务执行失败后任务进度不要变，应展示执行的进度比例」：
    # 旧实现无条件把 progress 清成 0（原句保留在此：「失败停在 0」），现在保留
    # 任务行终态换算出来的 3/7 = 42% —— 用户看得到「跑到多少了」。
    assert body["progress"] == 42, "失败 ⇒ 保留已执行的进度比例（3/7），不许清零"

    log = _logs(db_factory, "system.note_sync")[0]
    assert "CREATOR_SESSION_REQUIRED" in (log["exception_info"] or ""), (
        "失败原因要留在日志里 —— 否则用户只看到「同步未成功」，不知道下一步做什么"
    )
    assert log["message"] == "同步未成功"


def test_cancelled_task_is_not_reported_as_success(api_client, db_factory):
    """③ 变体：用户点取消是**显式动作**，日志报 success 就是自相矛盾。"""
    headers = _register(api_client, "card-log-cancel")
    user_id = _user_id(db_factory, "card-log-cancel")
    account_id = _make_account(db_factory, user_id)

    response = api_client.post(f"/api/accounts/{account_id}/notes/sync", headers=headers, json={})
    _finish_task(
        db_factory, int(response.json()["task_id"]), status="cancelled", note="已被用户取消"
    )

    body = _wait_log_terminal(db_factory, int(response.json()["log_id"]))
    assert body["status"] == "failed", "取消不算成功"


def test_progress_pump_writes_while_running_and_never_overwrites_terminal(db_factory):
    """④ 进度心跳的**两条纪律**：运行期间写中间值；日志进终态后不再写。

    直接驱动 `_ProgressPump`（而不是等整条链路）：泵的间隔在**构造时**定，
    链路上固定 2s —— 用例要的是「有没有中间值」这条纪律本身，用 50ms 间隔才测得动。
    """
    db = db_factory()
    try:
        user = User(username="pump-owner", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)
        scheduler_service.seed_builtin_jobs_for_user(db, user.id)
        job = db.scalars(
            select(ScheduledJob).where(ScheduledJob.user_id == user.id)
        ).first()
        log = ScheduledJobLog(
            job_id=job.id,
            user_id=user.id,
            job_name=job.name,
            job_key=job.job_key,
            job_trigger_type=job.trigger_type,
            trigger_type="manual",
            status="running",
            started_at=shanghai_now(),
        )
        db.add(log)
        db.commit()
        log_id = log.id
    finally:
        db.close()

    readings = iter([40, 40, 80])
    # ⚠️ 间隔必须**显著长于一次采样的开销**（采样 = 建库 + 查询 + 关库，高负载下可 >100ms）。
    #    原先取 0.05s 时，全量并发跑会整段跳过 40 直接看到 80（DEF-092：实测 `实得 [0, 80]`）——
    #    不是泵没写中间值，而是**测试没来得及采样**。0.5s 让「中间值窗口」稳稳覆盖采样周期。
    pump = scheduled_job_runner._ProgressPump(log_id, lambda: next(readings, 80), interval=0.5)
    pump.start()
    try:
        deadline = time.monotonic() + 10
        seen: list[int] = []
        while time.monotonic() < deadline:
            db = db_factory()
            try:
                value = db.get(ScheduledJobLog, log_id).progress
            finally:
                db.close()
            if value not in seen:
                seen.append(value)
            if value == 80:
                break
            time.sleep(0.05)
        assert 40 in seen, f"运行期间应写入中间进度（泵在写 running 行），实得 {seen}"
        assert seen[-1] == 80
    finally:
        pump.stop()

    # 日志进终态后泵不得覆写（否则出现「success 但 progress=40」）
    pump2 = scheduled_job_runner._ProgressPump(log_id, lambda: 5, interval=0.05)
    db = db_factory()
    try:
        row = db.get(ScheduledJobLog, log_id)
        row.status = "success"
        row.progress = 100
        db.commit()
    finally:
        db.close()
    pump2.start()
    time.sleep(0.25)
    pump2.stop()
    db = db_factory()
    try:
        row = db.get(ScheduledJobLog, log_id)
        assert row.progress == 100, "终态后泵仍在写 —— 会出现 success 与 progress 自相矛盾的日志行"
    finally:
        db.close()


def test_progress_pump_never_goes_backwards(db_factory):
    """⑤ 进度**只增不减**（2026-09-28 用户口径「**不允许出现 0→100 反复跳**」）。

    为什么必须在**写侧**钉：任何让百分比倒退的输入（分母中途变大、阶段切换换了一套
    分子分母）都是**说谎** —— 用户看到进度条往回跳只会以为任务出错了。泵是唯一一处
    能对**所有**探针生效的闸门（任务行的 `payload.progress` 由执行体写，泵只是消费者）。
    """
    db = db_factory()
    try:
        user = User(username="pump-monotonic", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)
        scheduler_service.seed_builtin_jobs_for_user(db, user.id)
        job = db.scalars(
            select(ScheduledJob).where(ScheduledJob.user_id == user.id)
        ).first()
        log = ScheduledJobLog(
            job_id=job.id,
            user_id=user.id,
            job_name=job.name,
            job_key=job.job_key,
            job_trigger_type=job.trigger_type,
            trigger_type="manual",
            status="running",
            started_at=shanghai_now(),
        )
        db.add(log)
        db.commit()
        log_id = log.id
    finally:
        db.close()

    # 40 → 40（相等不写）→ 80 → 20（**倒退，必须被丢掉**）→ 90
    readings = iter([40, 40, 80, 20, 90])
    pump = scheduled_job_runner._ProgressPump(
        log_id, lambda: next(readings, 90), interval=0.05
    )
    pump.start()
    try:
        deadline = time.monotonic() + 5
        seen: list[int] = []
        while time.monotonic() < deadline:
            db = db_factory()
            try:
                value = db.get(ScheduledJobLog, log_id).progress
            finally:
                db.close()
            if value not in seen:
                seen.append(value)
            if value == 90:
                break
            time.sleep(0.02)
    finally:
        pump.stop()

    assert 40 in seen and 90 in seen, f"正常读数没被写入，实得 {seen}"
    assert 20 not in seen, f"**倒退的读数被写进日志了**（进度条会往回跳），实得 {seen}"
    assert seen == sorted(seen), f"进度序列必须单调不减，实得 {seen}"


def test_await_task_terminal_reports_missing_and_timeout_instead_of_guessing(db_factory):
    """缺值口径：任务行不存在 / 等超时都要给**明确答案**，不许返回 `None` 让调用方去猜。

    ⚠️ 末尾那条「翻成 done 必须被观测到」是**这个用例的可判据**：只断言
    「运行中的任务等到超时」在生产库 `tasks.id=1`（状态 `completed`）上**也会通过**
    —— 那就成了假绿（2026-09-27 实测到）。必须有一条只有「真读到夹具库」才成立的断言。
    """
    missing = sts.await_task_terminal(999999, timeout_seconds=0.2, poll_seconds=0.02)
    assert missing["status"] == "missing"
    assert missing["error"], "要说明「为什么没有结果」，不能只给个空状态"

    db = db_factory()
    try:
        from tests.backend.conftest import seed_owner_rows as _seed2

        _seed2(db.get_bind(), user_id=1)
        row = Task(user_id=1, platform="xhs", task_type="xhs_note_sync", status="running")
        db.add(row)
        db.commit()
        running_id = row.id
    finally:
        db.close()

    timed_out = sts.await_task_terminal(running_id, timeout_seconds=0.15, poll_seconds=0.05)
    assert timed_out["status"] == "timeout", "超时不许静默变成成功"
    assert "仍在后台继续执行" in timed_out["error"]

    # 同一个任务号翻成终态后必须被观测到（缺这条断言，本用例在生产库上也「通过」）
    _finish_task(db_factory, running_id, status="done", note="跑完了")
    finished = sts.await_task_terminal(running_id, timeout_seconds=1.0, poll_seconds=0.05)
    assert finished["status"] == "done", f"应观测到终态，实得 {finished}"
    assert finished["note"] == "跑完了"
    assert finished["progress"] == {"stored": 7, "total": 7}


# --------------------------------------------------------------------------- #
# §44（2026-09-30 用户拍板拆分）：「同步商品笔记」是**第三条链路**
# --------------------------------------------------------------------------- #
def test_goods_note_click_records_its_own_log_and_task_type(api_client, db_factory):
    """点「同步商品笔记」⇒ **自己的**任务类型 + **自己的**日志流（§44）。

    与 `…/notes/sync` 逐字同构（同一个 `_start_account_sync`），差别只有三处入参：
    `task_type` / `job_key` / `source`。这条用例把三处都钉住 —— 它们漂移的后果是
    「商品笔记的失败记在普通笔记的任务类型下」，任务中心看得到却对不上。
    """
    headers = _register(api_client, "goods-note-log")
    user_id = _user_id(db_factory, "goods-note-log")
    account_id = _make_account(db_factory, user_id)

    response = api_client.post(
        f"/api/accounts/{account_id}/goods-notes/sync",
        headers=headers,
        json={"mode": "full"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reused"] is False
    log_id = body["log_id"]
    assert isinstance(log_id, int), "点击必须留下一条定时任务日志"

    db = db_factory()
    try:
        task = db.get(Task, int(body["task_id"]))
        assert task is not None
        # ⚠️ 任务类型是**商品笔记**那条 —— 写成 xhs_note_sync 会把两条链路混成一条。
        assert task.task_type == sts.GOODS_NOTE_SYNC_TASK_TYPE
        assert task.task_type != sts.NOTE_SYNC_TASK_TYPE
        # 来源具体到按钮（与「同步笔记」「同步商品」都不同）。
        assert task.payload["source"] == "account_card_goods_note_sync"
        assert task.status == "pending", "端点只建行，执行归账号任务 worker"
        assert task.payload["mode"] == "full", "同步语义必须落库（worker 从行重建）"
    finally:
        db.close()

    # 日志落在**自己的** job_key 流上（不是 system.note_sync）。
    logs = _logs(db_factory, "system.goods_note_sync")
    assert len(logs) == 1, f"商品笔记点击只应记一条自己的日志，实得 {len(logs)}"
    assert _logs(db_factory, "system.note_sync") == [], "不许串到普通笔记的日志流"
    assert logs[0]["status"] == "running", "任务没结束，日志不许先写终态（假成功）"
    assert logs[0]["config_snapshot"]["params_override"]["account_id"] == account_id

    _finish_task(db_factory, int(body["task_id"]), status="done", note="商品笔记同步完成")
    final = _wait_log_terminal(db_factory, log_id)
    assert final["status"] == "success"
    assert final["result"]["note"] == "商品笔记同步完成"


def test_note_and_goods_note_sync_do_not_block_each_other(api_client, db_factory):
    """**防重按任务类型分别成立**（§44）：普通笔记在跑**不**阻塞商品笔记。

    拆分前两者是同一个任务类型 ⇒ 「同步笔记在跑」会让「同步商品笔记」直接复用同一个
    任务行（点了没反应、用户以为按钮坏了）。这正是拆分要修的语义之一，所以单独立一条。
    """
    headers = _register(api_client, "goods-note-parallel")
    user_id = _user_id(db_factory, "goods-note-parallel")
    account_id = _make_account(db_factory, user_id)

    note = api_client.post(f"/api/accounts/{account_id}/notes/sync", headers=headers, json={})
    assert note.status_code == 200, note.text
    assert note.json()["reused"] is False

    goods = api_client.post(
        f"/api/accounts/{account_id}/goods-notes/sync", headers=headers, json={}
    )
    assert goods.status_code == 200, goods.text
    assert goods.json()["reused"] is False, "普通笔记在跑不该阻塞商品笔记（两条独立任务）"
    assert goods.json()["task_id"] != note.json()["task_id"]
    assert isinstance(goods.json()["log_id"], int)

    # 同一条链路自己仍然防重（第二次点击复用，不重复记账）。
    again = api_client.post(
        f"/api/accounts/{account_id}/goods-notes/sync", headers=headers, json={}
    )
    assert again.json()["reused"] is True
    assert again.json()["task_id"] == goods.json()["task_id"]
    assert again.json()["log_id"] is None, "复用不是新执行 ⇒ 不记日志"
    assert len(_logs(db_factory, "system.goods_note_sync")) == 1

    db = db_factory()
    try:
        types = sorted(db.scalars(select(Task.task_type)).all())
        assert types == sorted([sts.NOTE_SYNC_TASK_TYPE, sts.GOODS_NOTE_SYNC_TASK_TYPE]), (
            "两条独立任务行都在（不是复用成同一行）"
        )
    finally:
        db.close()


def test_goods_note_status_reads_only_its_own_stream(api_client, db_factory):
    """状态端点各看各的日志流（§44）：普通笔记在跑 ⇒ 商品笔记按钮仍是「空闲」。

    判据是「**该 job_key** 下该账号最新一条日志」（§35.17.4）⇒ 两条流互相不可见。
    ⚠️ 合并回一个 job_key 时本用例必红（商品笔记按钮会被普通笔记点亮）。
    """
    headers = _register(api_client, "goods-note-status")
    user_id = _user_id(db_factory, "goods-note-status")
    account_id = _make_account(db_factory, user_id)

    idle = api_client.get(
        f"/api/accounts/{account_id}/goods-notes/sync/status", headers=headers
    )
    assert idle.status_code == 200, idle.text
    assert idle.json()["status"] == "idle", "还没跑过 ⇒ 空态（不显示同步中）"

    api_client.post(f"/api/accounts/{account_id}/notes/sync", headers=headers, json={})

    note_status = api_client.get(
        f"/api/accounts/{account_id}/notes/sync/status", headers=headers
    ).json()
    assert note_status["status"] in ("pending", "running"), note_status
    still_idle = api_client.get(
        f"/api/accounts/{account_id}/goods-notes/sync/status", headers=headers
    ).json()
    assert still_idle["status"] == "idle", (
        "普通笔记在跑**不该**让商品笔记按钮也显示同步中（两条独立日志流）"
    )

    api_client.post(f"/api/accounts/{account_id}/goods-notes/sync", headers=headers, json={})
    now_active = api_client.get(
        f"/api/accounts/{account_id}/goods-notes/sync/status", headers=headers
    ).json()
    assert now_active["status"] in ("pending", "running"), now_active
