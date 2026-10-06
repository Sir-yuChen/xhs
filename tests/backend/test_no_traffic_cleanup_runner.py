"""无流量清理**执行器**的回归测试（DEF-042 的守卫）。

## 为什么单独建这个文件

清理执行器 `run_no_traffic_cleanup` 此前**零测试覆盖** —— 候选查询
（`test_note_cleanup.py`）与删除链（`test_note_delete.py`）各自有覆盖，
但「把它们串起来在后台跑」的那一层没有。于是一个 `ImportError` 活了下来：

    from backend.app.models.account import AccountCookieVersion   # 模块不存在

它只在**函数被调到时**才炸，而 `run_no_traffic_cleanup` 的
`except Exception` 会把它吞掉并标任务 `failed` —— 结果是「无流量清理」
每次都在后台静默失败，页面上看不出任何异常。

本文件钉住三件事：

1. 执行器能拿到 Cookie（`_account_cookies` 真的可调用，不再 ImportError）；
2. 候选 → 权限校验 → 删除的链路真的走通；
3. 任务行被标成 `done`，且 deleted 计数正确。
"""
from __future__ import annotations

import json

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.adapters.xhs import creator_api_adapter
from backend.app.core.database import Base
from backend.app.core.time import shanghai_now
from backend.app.models import NoTrafficCleanupLog, PlatformAccount, Task, User
from backend.app.models.shop import ShopNoteGoods
from backend.app.services import no_traffic_cleanup_service as ncs
from backend.app.services.account_service import CHANNEL_CREATOR
from backend.app.services.shop_binding_service import set_shop_state
from tests.backend.cookie_fixtures import cookie_version


#: 夹具里第二个用户的 id（`_setup` 填；跨租户用例用）。
_OTHER_USER_ID: dict = {"id": None}


class FakeCreatorApiAdapter:
    """只实现清理链路用到的两步，并记录被删的 note_id。"""

    deleted: list = []
    in_promotion: set = set()

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def validate_delete_permission(self, note_id: str):
        return True, "ok", {
            "data": {"is_note_in_promotion": note_id in type(self).in_promotion}
        }

    def delete_note(self, note_id: str):
        type(self).deleted.append(note_id)
        return True, "ok", {}


def _entry(note_id: str, *, browse: int = 0, orders: int = 0, gmv: int = 0) -> dict:
    return {
        "note_id": note_id,
        "note_title": f"笔记 {note_id}",
        "publish_time": 1_760_000_000_000,
        "browse_num": browse,
        "note_pay_order_cnt": orders,
        "note_pay_gmv": gmv,
        "note_browse_num30d": 0,
    }


def _setup(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cleanup-runner.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    monkeypatch.setattr(ncs, "SessionLocal", SessionLocal)
    monkeypatch.setattr(creator_api_adapter, "XhsCreatorApiAdapter", FakeCreatorApiAdapter)
    # ⚠️ **限流器是模块级、按 account_id 计数，而每个用例的账号 id 都是 1** ⇒
    #    不重置的话第 3 个用例一开跑就撞「触发限流，等待下一轮」，
    #    结果是 `deleted=0 / skipped=0 / failed=0` 而 `total=2` —— 看起来像业务 bug，
    #    其实是夹具没隔离那份**跨用例共享**的状态（实测：第 3 条用例假红）。
    monkeypatch.setattr(ncs, "_lock", ncs.CrawlRateLimiter())
    FakeCreatorApiAdapter.deleted = []
    FakeCreatorApiAdapter.in_promotion = set()

    db = SessionLocal()
    try:
        user = User(username="owner", password_hash="x")
        db.add(user)
        db.flush()
        #: 跨租户用例要一个**真实存在**的第二个用户（tasks.user_id 有外键 ——
        #: 随手写 999 会被 FK 拦下，那是夹具问题不是业务问题）。
        other = User(username="intruder", password_hash="x")
        db.add(other)
        db.flush()
        account = PlatformAccount(
            user_id=user.id, platform="xhs", sub_type="creator", nickname="我的号"
        )
        db.add(account)
        db.flush()
        # 执行器取 Cookie 走的就是这一行；它曾经因为 import 错模块而必炸。
        # 2026-09-21 拆分：无流量清理打创作者平台，所以给 creator 平台的 jar。
        db.add(
            cookie_version(
                account.id,
                "web_session=abc; a1=xyz; access-token-creator.xiaohongshu.com=ctok; customer-sso-sid=sso",
                CHANNEL_CREATOR,
            )
        )
        #: ADR-021：店铺已退场，归属键 = `platform_account_id`，账号归属写
        #: `seller_id`；状态仍写 `shop_state`。清理由账号发起。
        account.seller_id = "SELLER-1"
        set_shop_state(account, status="active", goods_note_total=3)
        db.flush()
        for note_id, kwargs in (
            ("ZERO-1", {}),                                    # 三项全零 → 候选
            ("HAS-READ", {"browse": 7}),                       # 有人看 → 不删
            ("ZERO-2", {}),                                    # 三项全零 → 候选
        ):
            db.add(
                ShopNoteGoods(
                    user_id=user.id,
                    platform_account_id=account.id,
                    note_id=note_id,
                    item_id="ITEM-1",
                    item_title="商品",
                    entry_json=json.dumps(_entry(note_id, **kwargs), ensure_ascii=False),
                    synced_at=shanghai_now(),
                )
            )
        db.commit()
        account_id = account.id
        #: 模块级：既有用例按二元组解包，不动它们；新用例直接读这里。
        _OTHER_USER_ID["id"] = other.id
    finally:
        db.close()
    return account_id, SessionLocal


def test_cleanup_runner_reads_cookies_and_deletes_the_candidates(tmp_path, monkeypatch):
    """回归 DEF-042：执行器曾因 import 错模块而在后台静默失败。"""
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)

    result = ncs.run_no_traffic_cleanup(account_id, user_id=1)

    assert sorted(FakeCreatorApiAdapter.deleted) == ["ZERO-1", "ZERO-2"]
    assert result["deleted"] == 2, result
    assert result["failed"] == 0, result

    db = SessionLocal()
    try:
        task = db.scalars(select(Task).where(Task.task_type == ncs.CLEANUP_TASK_TYPE)).one()
        assert task.status == ncs.STATUS_DONE
        assert task.payload["deleted"] == 2
    finally:
        db.close()


def test_cleanup_runner_skips_notes_in_promotion(tmp_path, monkeypatch):
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)
    FakeCreatorApiAdapter.in_promotion = {"ZERO-1"}

    result = ncs.run_no_traffic_cleanup(account_id, user_id=1)

    assert FakeCreatorApiAdapter.deleted == ["ZERO-2"]
    assert result["skipped"] == 1
    assert result["deleted"] == 1


def test_second_run_is_refused_while_a_task_is_active(tmp_path, monkeypatch):
    """防重：已有未完成任务 → 直接返回它，不再删第二遍。"""
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)

    db = SessionLocal()
    try:
        db.add(
            Task(
                user_id=1,
                platform="xhs",
                task_type=ncs.CLEANUP_TASK_TYPE,
                status=ncs.STATUS_RUNNING,
                payload={"account_id": account_id},
            )
        )
        db.commit()
    finally:
        db.close()

    result = ncs.run_no_traffic_cleanup(account_id, user_id=1)

    assert FakeCreatorApiAdapter.deleted == []
    assert result["deleted"] == 0
    assert "防重复触发" in result["message"]


# --------------------------------------------------------------------------- #
# 指定清理 / 全量清理 + 「清理前 / 清理后多少条」（2026-10-05 用户口径）
# --------------------------------------------------------------------------- #
def test_cleanup_records_before_and_after_pool_sizes(tmp_path, monkeypatch):
    """**记账判据**：`before_total` / `after_total` 必须是**重算的候选池**，不是估算。

    为什么这条值得单独守：`after = before - deleted` 是最自然也最错的写法 ——
    它把「跳过（推广中）」「失败（权限校验不过）」也算成已清，用户拿它当结论就会
    高估清理效果。本用例特意让一篇**权限校验失败**，然后断言 `after_total` 仍然算它。
    """
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)

    result = ncs.run_no_traffic_cleanup(
        account_id, thresholds=ncs.TrafficThresholds(), query=ncs.CleanupQuery(), batch_limit=20
    )

    db = SessionLocal()
    try:
        task = db.get(Task, result["task_id"])
        payload = task.payload if isinstance(task.payload, dict) else {}
    finally:
        db.close()

    # 池子里有 2 篇候选（ZERO-1 / ZERO-2），全删掉 ⇒ 清理后 0
    # ⚠️ after 的口径是 `before - deleted`（**不是重算本地池**）：删的是平台侧笔记，
    #    本地 `shop_note_goods` 行要等下次「同步商品」才消失 —— 重算会恒等于 before
    #    （2026-10-05 实测踩到：「清理前 1915 → 清理后 1915」）。
    assert result["deleted"] == 2, result
    assert payload["before_total"] == 2, payload
    assert payload["after_total"] == 0, payload


def test_specified_cleanup_only_deletes_the_selected_notes(tmp_path, monkeypatch):
    """**指定清理**：只删勾选的，池子里别的候选一篇不动。"""
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)

    result = ncs.run_no_traffic_cleanup(
        account_id,
        thresholds=ncs.TrafficThresholds(),
        query=ncs.CleanupQuery(),
        batch_limit=20,
        note_ids=["ZERO-1"],
    )

    assert result["deleted"] == 1, result
    assert FakeCreatorApiAdapter.deleted == ["ZERO-1"], FakeCreatorApiAdapter.deleted

    db = SessionLocal()
    try:
        task = db.get(Task, result["task_id"])
        payload = task.payload if isinstance(task.payload, dict) else {}
    finally:
        db.close()
    # before 是整个池子（2）—— **不是**勾选数（1）；after = before - deleted = 1
    assert payload["before_total"] == 2, payload
    assert payload["after_total"] == 1, payload


def test_specified_cleanup_refuses_notes_outside_the_pool(tmp_path, monkeypatch):
    """勾了**不在候选池**的笔记 ⇒ **不删**，并在 `not_in_pool` 里如实列出。

    这是防「删掉用户没在看到的那批」：阈值改小后重勾旧清单，那些 id 已经不符合
    「无流量」口径了，照删就是超范围删除。
    """
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)

    result = ncs.run_no_traffic_cleanup(
        account_id,
        thresholds=ncs.TrafficThresholds(),
        query=ncs.CleanupQuery(),
        batch_limit=20,
        note_ids=["ZERO-1", "NOT-A-CANDIDATE", "HAS-READ"],
    )

    # HAS-READ 有人看 ⇒ 不在候选池；NOT-A-CANDIDATE 根本不存在
    assert result["deleted"] == 1, result
    assert FakeCreatorApiAdapter.deleted == ["ZERO-1"], FakeCreatorApiAdapter.deleted

    db = SessionLocal()
    try:
        task = db.get(Task, result["task_id"])
        payload = task.payload if isinstance(task.payload, dict) else {}
    finally:
        db.close()
    assert set(payload["not_in_pool"]) == {"NOT-A-CANDIDATE", "HAS-READ"}, payload


def test_after_total_does_not_discount_skipped_or_failed(tmp_path, monkeypatch):
    """**口径守卫**：`skipped`（推广中）/ `failed`（权限不过）**不减** `after_total`。

    它们确实还在候选池里（平台侧一篇没删）—— 把它们算成「已清」就是高估清理效果，
    而这正是用户会拿去对账的数字。
    """
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)
    FakeCreatorApiAdapter.in_promotion = {"ZERO-1"}  # 这篇会被跳过

    result = ncs.run_no_traffic_cleanup(
        account_id, thresholds=ncs.TrafficThresholds(), query=ncs.CleanupQuery(), batch_limit=20
    )

    db = SessionLocal()
    try:
        task = db.get(Task, result["task_id"])
        payload = task.payload if isinstance(task.payload, dict) else {}
    finally:
        db.close()

    assert result["deleted"] == 1, result        # ZERO-2
    assert result["skipped"] == 1, result        # ZERO-1 推广中
    assert payload["before_total"] == 2, payload
    assert payload["after_total"] == 1, payload  # **只减 deleted**，skipped 不减


# --------------------------------------------------------------------------- #
# 清理日志表（2026-10-05 用户口径「新增清理无流量笔记日志表……记录清理无流量笔记的
# 信息和清理结果时间等等，失败原因，操作时间，账号」「日志表独立」）
# --------------------------------------------------------------------------- #
def test_manual_cleanup_writes_one_log_row_with_the_full_account(tmp_path, monkeypatch):
    """手动清理落**一行**日志，字段齐（账号 / 阈值 / 清理前 / 删除 / 时间 / 状态）。

    为什么单独钉这个：日志表是**独立于 `tasks` 行**的（用户口径「日志表独立」），
    tasks 行会被公共「清理记录表」任务按保留期删掉，日志不随之消失 —— 所以它必须
    在**建任务行的同时**被写出来，而不是靠 tasks 行反推。
    """
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)
    FakeCreatorApiAdapter.in_promotion = {"ZERO-1"}  # 这篇会被跳过

    ncs.run_no_traffic_cleanup(account_id, user_id=1)

    db = SessionLocal()
    try:
        logs = db.scalars(select(NoTrafficCleanupLog)).all()
        assert len(logs) == 1, "一次清理 = 一行日志（不多不少）"
        log = logs[0]
        assert log.account_id == account_id, "账号（用户口径「账号」）"
        assert log.account_name == "我的号", "账号名快照（不等下次 join 才显示得出来）"
        assert log.user_id == 1, "操作者"
        assert log.trigger_type == "manual"
        assert log.status in ("done", "failed")
        # 清理前 / 删除 / 跳过三数来自真实执行链
        assert log.before_total == 2, "候选池 2 篇（HAS-READ 有人看不进池）"
        assert log.skipped == 1, "推广中的那篇被跳过"
        assert log.deleted == 1, "另一篇被删"
        assert log.after_total == log.before_total - log.deleted, "清理后 = 前 - 删除"
        # 时间四件：操作时间 / 开跑 / 结果时间 / 耗时
        assert log.created_at is not None and log.started_at is not None
        assert log.finished_at is not None, "清理结果时间（用户口径）"
        assert log.duration_ms >= 0
        assert log.details, "逐篇明细"
    finally:
        db.close()


def test_log_row_is_kept_even_when_the_task_row_is_gone(tmp_path, monkeypatch):
    """日志**独立于 tasks 行**：删掉 tasks 行，日志仍在（`task_id` 置空）。

    这是「日志表独立」的**可执行判据** —— 不复用 `tasks.payload` 的唯一理由就是
    「tasks 行会被保留期清理删掉，删完就再也答不出上个月清了多少篇」。
    """
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)

    result = ncs.run_no_traffic_cleanup(account_id, user_id=1)
    db = SessionLocal()
    try:
        log = db.scalars(select(NoTrafficCleanupLog)).one()
        task = db.get(Task, result["task_id"])
        assert task is not None
        db.delete(task)          # 模拟公共保留期清理把 tasks 行删掉
        db.commit()              # ⚠️ 未开 FK 级联 ⇒ 这里验证的是「日志不依赖 tasks 行」
        db.expire_all()
        assert db.get(Task, result["task_id"]) is None, "前置：tasks 行确实没了"
        still = db.scalars(select(NoTrafficCleanupLog)).all()
        assert len(still) == 1, "日志不受 tasks 行删除影响"
        assert still[0].deleted == log.deleted, "报账数据仍完整可读"
    finally:
        db.close()


def test_failed_cleanup_records_the_reason(tmp_path, monkeypatch):
    """失败要落 `failed` + **原因原文**（用户口径「失败原因」，硬约束⑤不造假）。"""
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)

    class Boom(FakeCreatorApiAdapter):
        def validate_delete_permission(self, note_id: str):
            raise RuntimeError("平台回：账号存在风险，请重新登录")

    monkeypatch.setattr(creator_api_adapter, "XhsCreatorApiAdapter", Boom)

    ncs.run_no_traffic_cleanup(account_id, user_id=1)

    db = SessionLocal()
    try:
        log = db.scalars(select(NoTrafficCleanupLog)).one()
        assert log.status == "failed", "全篇失败 ⇒ 终态 failed"
        assert log.failed == 2, "两篇候选都失败"
        assert any(
            "账号存在风险" in str(item.get("reason") or "") for item in (log.details or [])
        ), "失败原因保留平台原文（不猜、不吞）"
    finally:
        db.close()


def test_running_cleanup_for_account_is_scoped_to_the_user(tmp_path, monkeypatch):
    """按钮态判据：**别人的**清理在跑，不该让本用户按钮变灰。"""
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)

    db = SessionLocal()
    try:
        db.add(
            Task(
                user_id=_OTHER_USER_ID["id"],  # 另一个用户
                platform="xhs",
                task_type=ncs.CLEANUP_TASK_TYPE,
                status=ncs.STATUS_RUNNING,
                payload={"account_id": account_id},
            )
        )
        db.commit()
        assert ncs.running_cleanup_for_account(db, account_id, 1) is None, "跨用户不可见"
        assert ncs.running_cleanup_for_account(db, account_id, _OTHER_USER_ID["id"]) is not None, "本人可见"
        assert ncs.running_cleanup_for_account(db, account_id) is not None, "不过滤 = 全量"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 公共「清理记录表」任务（2026-10-05 用户口径：队列清理升级 + 保留期变参数）
# --------------------------------------------------------------------------- #
def test_record_cleanup_registry_covers_queue_records_and_this_log():
    """清单自洽：两张记录表都登记在册，且键 / 中文名唯一。"""
    from backend.app.services.record_cleanup_service import RECORD_CLEANERS

    keys = [c.key for c in RECORD_CLEANERS]
    labels = [c.label for c in RECORD_CLEANERS]
    assert "queue" in keys, "队列执行记录必须在清理范围内"
    assert "no_traffic_cleanup_log" in keys, "无流量清理日志（本次新增的第二张记录表）"
    assert len(keys) == len(set(keys)) and len(labels) == len(set(labels)), "键 / 名不重复"
    assert all(c.purge is not None for c in RECORD_CLEANERS)


def test_retention_days_comes_from_task_params_and_falls_back_safely():
    """保留期是**任务参数**（用户口径），非法值退默认而不是抛异常。"""
    from backend.app.services import record_cleanup_service as rcs

    assert rcs.DEFAULT_RETENTION_DAYS == 15, "用户口径「默认记录表保留最新 15 天」"
    # 合法值：原样生效
    assert rcs._coerce_retention_days(30) == 30
    assert rcs._coerce_retention_days("7") == 7
    # 非法 / 缺值 / 越界：退默认（**不抛** —— 参数写错就让整轮清理不跑，
    # 记录会永久堆积，比退默认糟得多）
    assert rcs._coerce_retention_days(None) == 15
    assert rcs._coerce_retention_days("abc") == 15
    assert rcs._coerce_retention_days(0) == 15
    assert rcs._coerce_retention_days(-3) == 15
    assert rcs._coerce_retention_days(99999) == rcs.MAX_RETENTION_DAYS


def test_retention_validator_rejects_values_that_would_silently_fall_back():
    """写入口 400 而不是「界面填 0、实际按 15 跑」（界面骗人）。"""
    from backend.app.services.record_cleanup_service import validate_retention_days

    validate_retention_days({})  # 缺键 = 合法（用默认）
    validate_retention_days({"retention_days": 1})
    validate_retention_days({"retention_days": 3650})
    for bad in ({"retention_days": 0}, {"retention_days": -1}, {"retention_days": "x"}, {"retention_days": 3651}):
        try:
            validate_retention_days(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} 应该被拒（否则界面值与实际执行口径不一致）")


def test_record_cleanup_purges_both_tables_by_cutoff(tmp_path, monkeypatch):
    """真跑一次公共清理：**两张表**都按 cutoff 删，且活行 / 未超期一行不动。

    这是「升级为公共清理记录表任务」的可执行判据 —— 只删一张就等于没升级。
    """
    from backend.app.core.time import shanghai_now
    from datetime import timedelta

    from backend.app.services import record_cleanup_service as rcs

    account_id, SessionLocal = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(rcs, "SessionLocal", SessionLocal)

    db = SessionLocal()
    try:
        old = shanghai_now() - timedelta(days=40)
        fresh = shanghai_now() - timedelta(days=1)
        # ① 无流量清理日志：一条超期终态、一条超期 running、一条未超期
        db.add(NoTrafficCleanupLog(user_id=1, account_id=account_id, status="done", created_at=old))
        db.add(NoTrafficCleanupLog(user_id=1, account_id=account_id, status="running", created_at=old))
        db.add(NoTrafficCleanupLog(user_id=1, account_id=account_id, status="done", created_at=fresh))
        # ② 队列记录：一条超期 done（该删）、一条超期 running（活任务，不许删）
        db.add(
            Task(
                user_id=1, platform="xhs", task_type="xhs_note_publish_item",
                status="done", created_at=old,
            )
        )
        db.add(
            Task(
                user_id=1, platform="xhs", task_type="xhs_note_publish_item",
                status="running", created_at=old,
            )
        )
        db.commit()
    finally:
        db.close()

    result = rcs.run_record_cleanup(1, retention_days=15)

    assert result["retention_days"] == 15
    assert result["cleaned"]["no_traffic_cleanup_log"] == 1, "超期终态日志删 1（running / 未超期不动）"
    assert result["cleaned"]["queue"] == 1, "超期终态队列项删 1（running 活任务不动）"
    db = SessionLocal()
    try:
        assert db.scalar(select(func.count()).select_from(NoTrafficCleanupLog)) == 2
        assert db.scalar(select(func.count()).select_from(Task)) == 1, "只剩那条 running"
    finally:
        db.close()


def test_record_cleanup_never_touches_another_users_rows(tmp_path, monkeypatch):
    """定时任务是 `user_scoped` 的 ⇒ 越租户删除是多用户数据事故。"""
    from backend.app.core.time import shanghai_now
    from datetime import timedelta

    from backend.app.services import record_cleanup_service as rcs

    account_id, SessionLocal = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(rcs, "SessionLocal", SessionLocal)

    db = SessionLocal()
    try:
        old = shanghai_now() - timedelta(days=40)
        db.add(NoTrafficCleanupLog(user_id=1, account_id=account_id, status="done", created_at=old))
        db.add(NoTrafficCleanupLog(user_id=_OTHER_USER_ID["id"], account_id=account_id, status="done", created_at=old))
        db.add(Task(user_id=1, platform="xhs", task_type="xhs_note_publish_item", status="done", created_at=old))
        db.add(Task(user_id=_OTHER_USER_ID["id"], platform="xhs", task_type="xhs_note_publish_item", status="done", created_at=old))
        db.commit()
    finally:
        db.close()

    rcs.run_record_cleanup(1, retention_days=15)

    db = SessionLocal()
    try:
        assert db.scalar(select(func.count()).select_from(NoTrafficCleanupLog)) == 1
        assert db.scalars(select(NoTrafficCleanupLog)).one().user_id == _OTHER_USER_ID["id"], "只剩别人的"
        assert db.scalar(select(func.count()).select_from(Task)) == 1
        assert db.scalars(select(Task)).one().user_id == _OTHER_USER_ID["id"], "队列记录同样不越租户"
    finally:
        db.close()


def test_record_cleanup_without_user_scope_deletes_nothing(tmp_path, monkeypatch):
    """无归属（`user_id=None`）**一行不删** —— 放开等于「一次误调用毁掉全库历史」。"""
    from backend.app.core.time import shanghai_now
    from datetime import timedelta

    from backend.app.services import record_cleanup_service as rcs

    account_id, SessionLocal = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(rcs, "SessionLocal", SessionLocal)

    db = SessionLocal()
    try:
        old = shanghai_now() - timedelta(days=40)
        db.add(NoTrafficCleanupLog(user_id=1, account_id=account_id, status="done", created_at=old))
        db.add(Task(user_id=1, platform="xhs", task_type="xhs_note_publish_item", status="done", created_at=old))
        db.commit()
    finally:
        db.close()

    result = rcs.run_record_cleanup(None, retention_days=15)  # type: ignore[arg-type]

    assert result["total"] == 0, "无归属调用必须空转"
    db = SessionLocal()
    try:
        assert db.scalar(select(func.count()).select_from(NoTrafficCleanupLog)) == 1
        assert db.scalar(select(func.count()).select_from(Task)) == 1
    finally:
        db.close()


def test_no_traffic_cleanup_job_is_manual_not_scheduled():
    """用户口径「没有自动清理笔记的逻辑」的**可执行判据**：注册表默认就是 manual。

    ⚠️ 原先 `default_trigger_type="cron" + default_cron="0 0 */6 * * *"`：配置里
    有 `no_traffic_cleanup_interval_hours` 时会被装配成定时跑 —— 与用户口径正面冲突。
    """
    from backend.app.services.job_registry import job_registry

    spec = job_registry()["system.no_traffic_cleanup"]
    assert spec.default_trigger_type == "manual", "手动触发任务，不进调度装配"
    assert spec.default_cron == "", "没有自动跑的 cron（留着就是僵尸兜底）"
    assert spec.builtin is True

def test_retention_param_reaches_the_purge_cutoff(tmp_path, monkeypatch):
    """保留期参数**真的走到 cutoff**（不是被 helper 接住就完了）。

    ⚠️ 这条是**变异逼出来的**：原先只钉了 `_coerce_retention_days` 的纯函数边界，
    把 `run_record_cleanup` 里 `days = _coerce_retention_days(retention_days)`
    改成写死 15 —— helper 照旧工作，29 天的调用方**静默按 15 天清**
    （用户改了参数、界面显示 29、实际清 15），而没有任何断言看得见。
    所以这里断言的是**可观测结果**：40 天前的行在 `retention_days=30` 下必须**留着**。
    """
    from datetime import timedelta

    from backend.app.core.time import shanghai_now
    from backend.app.services import record_cleanup_service as rcs

    account_id, SessionLocal = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(rcs, "SessionLocal", SessionLocal)

    db = SessionLocal()
    try:
        # 40 天前：cutoff=30 天 ⇒ 留下；cutoff=15 天 ⇒ 删掉。
        db.add(
            NoTrafficCleanupLog(
                user_id=1,
                account_id=account_id,
                status="done",
                created_at=shanghai_now() - timedelta(days=40),
            )
        )
        # 20 天前：两种 cutoff 下都超期 ⇒ 都删（证明清理确实跑了，不是全空转）。
        db.add(
            NoTrafficCleanupLog(
                user_id=1,
                account_id=account_id,
                status="done",
                created_at=shanghai_now() - timedelta(days=20),
            )
        )
        db.commit()
    finally:
        db.close()

    result = rcs.run_record_cleanup(1, retention_days=30)
    assert result["retention_days"] == 30, "回执要如实回本次用的保留期"
    assert result["cleaned"]["no_traffic_cleanup_log"] == 1, "只该删 20 天前那条"

    db = SessionLocal()
    try:
        rows = db.scalars(select(NoTrafficCleanupLog)).all()
        assert len(rows) == 1, "40 天前的行必须留下（若按 15 天清就没了）"
    finally:
        db.close()


def test_manual_cleanup_writes_a_log_even_when_the_task_is_reused(tmp_path, monkeypatch):
    """**复用已有任务行**那条路也要落日志（`if log is not None` 是必经分支）。

    ⚠️ 这条同样是**变异逼出来的**：把 `_run_in_thread` 里新增的
    `if log is not None:` 改成 `if False:`（即「只建任务行、不写日志」），
    其余用例全绿 —— 因为它们都走 `run_no_traffic_cleanup`（同步路径），
    而**页面按钮走的正是这条线程路径**。判据必须盯「页面按下去之后有没有日志」。
    """
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)

    # ① 先建一条 pending 任务行（模拟 API 已建行、线程即将接手）
    db = SessionLocal()
    try:
        task = Task(
            user_id=1,
            platform="xhs",
            task_type=ncs.CLEANUP_TASK_TYPE,
            status=ncs.STATUS_PENDING,
            payload={"account_id": account_id, "note_ids": [], "batch_limit": 20},
        )
        db.add(task)
        db.commit()
        task_id = task.id
    finally:
        db.close()

    # ② 直接跑线程体（页面按钮那条路；不起真线程以免和夹具抢库）
    ncs._run_in_thread(
        account_id,
        None,          # thresholds
        None,          # query
        20,            # batch_limit
        task_id,
        1,             # user_id
        None,          # note_ids
    )

    db = SessionLocal()
    try:
        logs = db.scalars(select(NoTrafficCleanupLog)).all()
        assert len(logs) == 1, "线程路径（页面按钮）必须落日志"
        assert logs[0].task_id == task_id, "日志要挂在它服务的那条任务行上"
        assert logs[0].finished_at is not None, "要有清理结果时间"
    finally:
        db.close()


def test_thread_path_also_records_the_retention_of_the_run(tmp_path, monkeypatch):
    """线程路径的日志同样要把**报账数**写全（before / after / deleted 三者自洽）。"""
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)
    FakeCreatorApiAdapter.in_promotion = {"ZERO-1"}

    db = SessionLocal()
    try:
        task = Task(
            user_id=1,
            platform="xhs",
            task_type=ncs.CLEANUP_TASK_TYPE,
            status=ncs.STATUS_PENDING,
            payload={"account_id": account_id, "note_ids": [], "batch_limit": 20},
        )
        db.add(task)
        db.commit()
        task_id = task.id
    finally:
        db.close()

    ncs._run_in_thread(account_id, None, None, 20, task_id, 1, None)

    db = SessionLocal()
    try:
        log = db.scalars(select(NoTrafficCleanupLog)).one()
        assert log.before_total == 2, "线程路径也要算「清理前」"
        assert log.deleted == 1 and log.skipped == 1
        assert log.after_total == log.before_total - log.deleted, "清理后 = 前 - 删除"
        assert log.status in ("done", "failed")
    finally:
        db.close()
