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

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.adapters.xhs import creator_api_adapter
from backend.app.adapters.xhs import qianfan_api_adapter
from backend.app.core.database import Base
from backend.app.core.time import shanghai_now
from backend.app.models import (
    NoTrafficCleanupItem,
    NoTrafficCleanupLog,
    Note,
    NoteDatacenterStat,
    PlatformAccount,
    ScheduledJobLog,
    Task,
    User,
)
from backend.app.models.scheduled_job import LOG_SUCCESS
from backend.app.services import no_traffic_cleanup_service as ncs
from backend.app.services.account_service import CHANNEL_ARK, CHANNEL_CREATOR
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


class FakeQianfanApiAdapter:
    """只实现千帆批量删除，并记录**每次调用**的 note_ids（验证分片）。"""

    deleted: list = []          # 累计所有被删 note_id（flatten）
    calls: list = []            # 每次调用的 note_ids 列表（验证分片边界）

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def batch_delete_notes(self, note_ids):
        type(self).deleted.extend(note_ids)
        type(self).calls.append(list(note_ids))
        return True, "成功", {}


def _entry(note_id: str, *, browse: int = 0, orders: int = 0, gmv: int = 0) -> dict:
    """生成 `notes.raw_json` 形状（方案 A 数据源：`metrics` 键 + 发布时间）。

    ⚠️ 方案 A（2026-10-06）后候选池从 `notes.raw_json.metrics` 读指标，
    键名是 `read_count` / `pay_gmv` / `pay_order_cnt`（对齐 `note_data_service`），
    不再是旧的 `browse_num` / `note_pay_gmv` / `note_pay_order_cnt`。
    """
    return {
        "title": f"笔记 {note_id}",
        "published_at": "2026-08-01 00:00:00",
        "metrics": {
            "read_count": browse,
            "pay_order_cnt": orders,
            "pay_gmv": gmv,
            "like_count": 0,
            "collect_count": 0,
            "comment_count": 0,
            "share_count": 0,
        },
    }


def _stat(note_id: str, *, account_id: int, user_id: int, browse: int = 0,
          orders: int = 0, gmv: int = 0, flag: str = "goods_note", title: str = "") -> NoteDatacenterStat:
    """生成 `note_datacenter_stats` 行（2026-10-12 设计 35：无流量候选数据源）。

    指标直接落真实列（`read_count` / `pay_order_cnt` / `pay_gmv`），`note_flag` 决定
    删除通道（`goods_note` → 千帆批量 / 空 → 创作者逐篇）。
    """
    return NoteDatacenterStat(
        user_id=user_id,
        platform_account_id=account_id,
        note_id=note_id,
        stat_date="2026-10-12",
        window="recent_1",
        note_flag=flag,
        title=title or f"笔记 {note_id}",
        read_count=browse,
        like_count=0,
        collect_count=0,
        comment_count=0,
        share_count=0,
        pay_order_cnt=float(orders),
        pay_gmv=float(gmv),
        publish_time=1,
    )


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
        # 方案 A（设计 35）：候选数据源 = `note_datacenter_stats`（真实列指标 + note_flag）。
        for note_id, kwargs in (
            ("ZERO-1", {}),                                    # 三项全零 → 候选
            ("HAS-READ", {"browse": 7}),                       # 有人看 → 不删
            ("ZERO-2", {}),                                    # 三项全零 → 候选
        ):
            # 笔记行（本地 purge 目标）+ 统计行（候选数据源）。
            db.add(
                Note(
                    user_id=user.id,
                    platform_account_id=account.id,
                    platform="xhs",
                    note_id=note_id,
                    title=f"笔记 {note_id}",
                )
            )
            db.add(_stat(note_id, account_id=account.id, user_id=user.id, **kwargs))
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
        # 2026-10-12 软删除：平台删成功后本地**软删**（is_deleted=True），不再硬删。
        # ZERO-1/ZERO-2 仍在表里但标记软删，HAS-READ 保持未删。
        deleted_rows = set(
            db.scalars(
                select(Note.note_id).where(
                    Note.note_id.in_(["ZERO-1", "ZERO-2"]), Note.is_deleted.is_(True)
                )
            ).all()
        )
        assert deleted_rows == {"ZERO-1", "ZERO-2"}, f"应软删 ZERO-1/ZERO-2，实际 {deleted_rows}"
        has_read = db.scalars(
            select(Note).where(Note.note_id == "HAS-READ")
        ).one()
        assert has_read.is_deleted is False, "HAS-READ 不应被软删"
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

    # 2026-10-13：指定笔记清理改为「按勾选的 note_id 直接查统计表」，不再二次校验
    # 「是否在候选池」（阈值）。故 HAS-READ（有流量、但有统计行）现在也会被删；
    # 只有 NOT-A-CANDIDATE（统计表里不存在）才被 not_in_pool。
    assert result["deleted"] == 2, result
    assert sorted(FakeCreatorApiAdapter.deleted) == ["HAS-READ", "ZERO-1"], FakeCreatorApiAdapter.deleted

    db = SessionLocal()
    try:
        task = db.get(Task, result["task_id"])
        payload = task.payload if isinstance(task.payload, dict) else {}
    finally:
        db.close()
    assert set(payload["not_in_pool"]) == {"NOT-A-CANDIDATE"}, payload


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
    """失败要落 `failed` + **原因原文**（用户口径「失败原因」，硬约束⑤不造假）。

    ⚠️ 本用例的失败原文**含「账号存在风险，请重新登录」** —— 2026-10-06 起它同时
    触发 DEF-097 的**账号级短路**（剩余笔记不再发请求）⇒ `failed == 1` 而不是 2。
    这是**刻意**的行为：同一句风控告警重复打第二篇没有任何产出。两件事分别由
    `test_account_level_failure_aborts_the_batch_and_never_calls_the_rest`（短路）
    与 `test_failed_cleanup_records_the_reason`（原因留痕）各自钉住。
    """
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)

    class Boom(FakeCreatorApiAdapter):
        def validate_delete_permission(self, note_id: str):
            raise RuntimeError("平台回：账号存在风险，请重新登录")

    monkeypatch.setattr(creator_api_adapter, "XhsCreatorApiAdapter", Boom)

    ncs.run_no_traffic_cleanup(account_id, user_id=1)

    db = SessionLocal()
    try:
        log = db.scalars(select(NoTrafficCleanupLog)).one()
        assert log.status == "failed", "失败 ⇒ 终态 failed"
        assert log.failed == 1, "风控短路：只打第一篇（DEF-097）"
        assert any(
            "账号存在风险" in str(item.get("reason") or "") for item in (log.details or [])
        ), "失败原因保留平台原文（不猜、不吞）"
    finally:
        db.close()


def test_account_level_failure_aborts_the_batch_and_never_calls_the_rest(tmp_path, monkeypatch):
    """**账号级**失败（风控 / 凭据）⇒ 本批中止，剩下的笔记**一次请求都不发**（DEF-097）。

    真机现场（2026-10-06，账号 8）：平台回 `code=906 账号存在风险，请重新登录` 时，
    「清理全部」会朝一个**已被风控告警**的账号连打 20 次权限校验 —— 没有一次会成功，
    纯粹加重风控。判据必须落在**调用次数**上（只看 `failed` 数分不出「短路」与
    「逐篇都失败」）。
    """
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)
    calls: list[str] = []

    class Risk(FakeCreatorApiAdapter):
        def validate_delete_permission(self, note_id: str):
            calls.append(note_id)
            return False, "账号存在风险，请重新登录", {"code": 906}

    monkeypatch.setattr(creator_api_adapter, "XhsCreatorApiAdapter", Risk)

    result = ncs.run_no_traffic_cleanup(account_id, user_id=1)

    assert len(calls) == 1, f"账号级失败后不该再打第二篇，实际打了 {calls}"
    assert result["aborted"] is True
    assert "账号存在风险" in result["abort_reason"]

    db = SessionLocal()
    try:
        log = db.scalars(select(NoTrafficCleanupLog)).one()
        assert log.aborted is True, "中止事实要落库"
        assert "账号存在风险" in log.abort_reason
        # `reason` 是**失败原因**，不是覆盖度文案（DEF-095）。
        assert "账号存在风险" in log.reason, log.reason
        assert "候选池" not in log.reason, "覆盖度文案不许再冒充失败原因"
        assert "候选池" in log.coverage_note, "覆盖度改走 coverage_note"
    finally:
        db.close()


def test_unreached_notes_are_recorded_as_not_attempted(tmp_path, monkeypatch):
    """中止后**没轮到的笔记**落 `result="not_attempted"`，**不是** `failed`。

    ⚠️ 这条判据是变异验证逼出来的（`notattemptedmissing` 变异**没被抓住**）：
    此前只断言了「剩余笔记没发请求」，没断言它们**怎么记账**。两者是两件事 ——
    记成 `failed` 就够让用户以为「打了但失败了」，进而**去查一个不存在的失败原因**；
    记成 `not_attempted` 才答得出「这篇根本没轮到，重试即可」。

    一条管**账号级中止**（风控），一条管**限流中止** —— 两条路的收尾是两份代码，
    必须各钉一次（共用一条断言会漏掉另一条）。
    """
    # ① 账号级（风控）
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)

    class Risk(FakeCreatorApiAdapter):
        def validate_delete_permission(self, note_id: str):
            return False, "账号存在风险，请重新登录", {}

    monkeypatch.setattr(creator_api_adapter, "XhsCreatorApiAdapter", Risk)
    ncs.run_no_traffic_cleanup(account_id, user_id=1)

    db = SessionLocal()
    try:
        rows = {i.note_id: i for i in db.scalars(select(NoTrafficCleanupItem)).all()}
        assert set(rows) == {"ZERO-1", "ZERO-2"}, "两篇都要有账"
        # ⚠️ **不假设谁先谁后**：候选顺序来自 `synced_at desc`，夹具里两篇同一时刻
        #    建行 ⇒ 顺序无关紧要，断言写成「恰好一篇 failed、恰好一篇 not_attempted」。
        results = sorted(i.result for i in rows.values())
        assert results == ["failed", "not_attempted"], f"实际 {results}"
        un = next(i for i in rows.values() if i.result == "not_attempted")
        assert "未执行" in un.reason
    finally:
        db.close()

    # ② 限流中止（另一份收尾代码）
    # ⚠️ 同一个 `tmp_path` 装不下两个库（`_setup` 按固定文件名建库，第二次会撞
    #    `users.username` 唯一约束）⇒ 换子目录，且**必须先建目录**，否则 SQLite
    #    报 `unable to open database file`。
    (tmp_path / "rl").mkdir()
    account_id2, SessionLocal2 = _setup(tmp_path / "rl", monkeypatch)

    class NoQuota(type(ncs.CrawlRateLimiter())):
        def allow(self, account_id: int) -> bool:  # type: ignore[override]
            return False

    monkeypatch.setattr(ncs, "_lock", NoQuota())
    ncs.run_no_traffic_cleanup(account_id2, user_id=1)

    db = SessionLocal2()
    try:
        rows = {i.note_id: i for i in db.scalars(select(NoTrafficCleanupItem)).all()}
        assert set(rows) == {"ZERO-1", "ZERO-2"}, "限流也得给两篇都记账"
        assert all(i.result == "not_attempted" for i in rows.values()), (
            f"限流 ⇒ 一篇都没打成 ⇒ 全是 not_attempted，实际 {[i.result for i in rows.values()]}"
        )
        log = db.scalars(select(NoTrafficCleanupLog)).one()
        assert log.aborted is True and "限流" in log.abort_reason
    finally:
        db.close()


def test_single_note_failure_does_not_abort_the_batch(tmp_path, monkeypatch):
    """**单篇级**失败（这一篇的问题）⇒ 照旧往下跑（短路的反面判据）。

    只认账号级特征，认不出时宁可多跑 —— 误判中止会让「有一篇坏了就整批不跑」，
    那比多打几次请求糟得多。
    """
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)
    calls: list[str] = []

    class OneBad(FakeCreatorApiAdapter):
        def validate_delete_permission(self, note_id: str):
            calls.append(note_id)
            if note_id == "ZERO-1":
                # 单篇级失败（非「笔记不存在」、非账号级）：无删除权限。
                return False, "无删除权限", {}
            return True, "ok", {"data": {"is_note_in_promotion": False}}

    monkeypatch.setattr(creator_api_adapter, "XhsCreatorApiAdapter", OneBad)

    result = ncs.run_no_traffic_cleanup(account_id, user_id=1)

    assert len(calls) == 2, f"单篇失败不该中止本批，实际 {calls}"
    assert result["aborted"] is False
    assert result["failed"] == 1 and result["deleted"] == 1


def test_log_reason_is_empty_when_nothing_failed(tmp_path, monkeypatch):
    """**DEF-095 判据 ①**：没有失败时 `reason` 是**空**，覆盖度文案在 `coverage_note`。

    修复前它恒等于 `payload["note"]`（覆盖度文案）—— 于是「一次全部成功的清理」
    也会在「失败原因」列显示一句覆盖度说明，同一句话在成功与失败两种场景下都出现，
    列的语义就不成立了。
    """
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)

    ncs.run_no_traffic_cleanup(account_id, user_id=1)

    db = SessionLocal()
    try:
        log = db.scalars(select(NoTrafficCleanupLog)).one()
        assert log.deleted == 2, "前置：两篇都删掉了"
        assert log.reason == "", f"没有失败就不该有失败原因，实际 {log.reason!r}"
        assert "候选池" in log.coverage_note, "覆盖度说明仍如实保留"
    finally:
        db.close()


def test_log_reason_is_the_failure_not_the_coverage_blurb(tmp_path, monkeypatch):
    """**DEF-095 判据 ②**：有失败时 `reason` 是**那条失败的平台原文**。

    ⚠️ 修复前的现场：平台风控失败，界面「失败原因」列显示
    「候选池仅包含已同步千帆挂链映射的笔记…」—— 一句**正常态**的覆盖度说明，
    与本次为何失败毫无关系，把用户指向完全错误的方向。
    """
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)

    class Deny(FakeCreatorApiAdapter):
        def validate_delete_permission(self, note_id: str):
            return False, "无权限删除该笔记", {}

    monkeypatch.setattr(creator_api_adapter, "XhsCreatorApiAdapter", Deny)

    ncs.run_no_traffic_cleanup(account_id, user_id=1)

    db = SessionLocal()
    try:
        log = db.scalars(select(NoTrafficCleanupLog)).one()
        assert "无权限删除该笔记" in log.reason, log.reason
        assert "候选池" not in log.reason, "覆盖度文案不许再冒充失败原因"
        assert "候选池" in log.coverage_note, "覆盖度改走 coverage_note"
    finally:
        db.close()


def test_cleanup_writes_one_item_row_per_note(tmp_path, monkeypatch):
    """**笔记维度主体**（用户 2026-10-06 口径）：一篇笔记 = 一行，结果逐篇可查。

    「清理无流量的主要对象是笔记，**所有主体应是笔记维度**，记录操作日志时也应该是
    笔记维度」⇒ 批次行答「这次整体怎么样」，明细行答「**这一篇**被删了没有、为什么」。
    """
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)
    FakeCreatorApiAdapter.in_promotion = {"ZERO-1"}  # 这篇被跳过

    ncs.run_no_traffic_cleanup(account_id, user_id=1)

    db = SessionLocal()
    try:
        log = db.scalars(select(NoTrafficCleanupLog)).one()
        items = db.scalars(
            select(NoTrafficCleanupItem).order_by(NoTrafficCleanupItem.note_id)
        ).all()
        by_note = {item.note_id: item for item in items}
        assert set(by_note) == {"ZERO-1", "ZERO-2"}, f"两篇候选各一行，实际 {sorted(by_note)}"
        assert by_note["ZERO-1"].result == "skipped", "推广中 ⇒ skipped"
        assert by_note["ZERO-2"].result == "deleted", "真删 ⇒ deleted"

        # 笔记主体字段：标题 / 账号 / 归属 / 关联批次行 / 操作时间。
        assert by_note["ZERO-2"].note_title == "笔记 ZERO-2", "标题快照（平台删了也答得出）"
        assert by_note["ZERO-2"].account_id == account_id
        assert by_note["ZERO-2"].user_id == 1
        assert by_note["ZERO-2"].log_id == log.id, "挂到本批次行"
        assert by_note["ZERO-2"].operated_at is not None, "操作时间（用户口径）"
        # `deleted` 那篇没有原因；`skipped` 那篇有。
        assert by_note["ZERO-2"].reason == "", "删除成功不编原因"
        assert "推广" in by_note["ZERO-1"].reason
    finally:
        db.close()


def test_items_are_kept_when_the_batch_log_row_is_purged(tmp_path, monkeypatch):
    """批次行被保留期删掉 ⇒ 明细行**仍在**（`log_id` 置空）。

    「某篇笔记当初为什么没删掉」这个答案不该随批次记录过期而消失 —— 这就是明细
    独立成表、且 `log_id` 用 `SET NULL` 的理由。
    """
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)
    ncs.run_no_traffic_cleanup(account_id, user_id=1)

    db = SessionLocal()
    try:
        log = db.scalars(select(NoTrafficCleanupLog)).one()
        db.delete(log)
        db.commit()
        db.expire_all()
        assert db.scalars(select(NoTrafficCleanupLog)).all() == [], "前置：批次行没了"
        items = db.scalars(select(NoTrafficCleanupItem)).all()
        assert len(items) == 2, "明细不受批次行删除影响"
        assert all(item.log_id is None for item in items), "log_id 置空（SET NULL）"
        # 归属信息冗余存着 ⇒ 批次行没了也读得懂。
        assert all(item.user_id == 1 and item.account_id == account_id for item in items)
    finally:
        db.close()


def test_not_in_pool_notes_are_recorded_as_items_too(tmp_path, monkeypatch):
    """勾选但**已不在候选池**的笔记也留一行（`not_in_pool`）——用户勾过就必须有答案。"""
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)
    ncs.run_no_traffic_cleanup(
        account_id, note_ids=["ZERO-1", "GONE-1"], user_id=1
    )

    db = SessionLocal()
    try:
        items = {
            item.note_id: item
            for item in db.scalars(select(NoTrafficCleanupItem)).all()
        }
        assert "GONE-1" in items, "勾了但不在池：必须留痕"
        assert items["GONE-1"].result == "not_in_pool"
        assert "不在当前候选池" in items["GONE-1"].reason
    finally:
        db.close()


def test_record_cleanup_registry_also_covers_the_note_items():
    """明细表登记在「清理记录表」范围内，且**排在批次行之前**（先子后父）。"""
    from backend.app.services.record_cleanup_service import RECORD_CLEANERS

    keys = [c.key for c in RECORD_CLEANERS]
    assert "no_traffic_cleanup_item" in keys, "笔记维度明细必须进清理范围"
    assert keys.index("no_traffic_cleanup_item") < keys.index("no_traffic_cleanup_log"), (
        "先清明细再清批次行（子行在前）"
    )


def test_record_cleanup_purges_the_notes_items_by_operated_at(tmp_path, monkeypatch):
    """明细表按**操作时间**判期：超期的删、未超期的一行不动。

    ⚠️ 明细**没有「进行中」这一态**（它是批次收尾时一次性写下的已发生事实）⇒
    判据只看 `operated_at`，不看 `result`。这条与批次行（看 `status`）**刻意不同**。
    """
    from datetime import timedelta

    from backend.app.core.time import shanghai_now
    from backend.app.services import record_cleanup_service as rcs

    account_id, SessionLocal = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(rcs, "SessionLocal", SessionLocal)

    old = shanghai_now() - timedelta(days=40)
    fresh = shanghai_now() - timedelta(days=1)
    db = SessionLocal()
    try:
        for result, when in (("deleted", old), ("failed", old), ("deleted", fresh)):
            db.add(
                NoTrafficCleanupItem(
                    user_id=1,
                    account_id=account_id,
                    note_id=f"N-{result}-{when.date()}",
                    result=result,
                    operated_at=when,
                )
            )
        db.commit()
    finally:
        db.close()

    result = rcs.run_record_cleanup(1, retention_days=15)

    assert result["cleaned"]["no_traffic_cleanup_item"] == 2, "超期 2 条（未超期的不动）"
    db = SessionLocal()
    try:
        rows = db.scalars(select(NoTrafficCleanupItem)).all()
        assert len(rows) == 1, "只剩未超期那条"
        assert rows[0].operated_at > old
    finally:
        db.close()


def test_record_cleanup_never_purges_another_users_items(tmp_path, monkeypatch):
    """跨租户：清理只动行主本人的明细（别人的一行不动）。"""
    from datetime import timedelta

    from backend.app.core.time import shanghai_now
    from backend.app.services import record_cleanup_service as rcs

    account_id, SessionLocal = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(rcs, "SessionLocal", SessionLocal)
    old = shanghai_now() - timedelta(days=40)

    db = SessionLocal()
    try:
        db.add(
            NoTrafficCleanupItem(
                user_id=1, account_id=account_id, note_id="MINE", operated_at=old
            )
        )
        db.add(
            NoTrafficCleanupItem(
                user_id=_OTHER_USER_ID["id"], account_id=account_id,
                note_id="THEIRS", operated_at=old,
            )
        )
        db.commit()
    finally:
        db.close()

    rcs.run_record_cleanup(1, retention_days=15)

    db = SessionLocal()
    try:
        left = db.scalars(select(NoTrafficCleanupItem)).all()
        assert [row.note_id for row in left] == ["THEIRS"], "别人的明细不许被清"
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


def test_no_traffic_cleanup_job_is_gone_from_the_scheduler():
    """用户口径「无流量笔记清理任务不应在任务列表展示」的**可执行判据**。

    ⚠️ 本用例的前身 `test_no_traffic_cleanup_job_is_manual_not_scheduled` 断言
    「注册表里该键默认 `manual`、`default_cron == ""`（留着键但只能手动触发）」
    —— **已被 2026-10-07 用户口径取代**：「无流量笔记清理任务表移除该配置」+
    「不应在任务列表展示」⇒ 键**整体移除**（不是再降一档）。原句保留在 git 历史。

    ⚠️ 保留的边界（这个「移除」不能扩大化）：清理**本身**没退场 ——
    `/users/no-traffic/cleanup` 端点、候选池、两张日志表都还在（见本文件其余用例）。
    """
    from backend.app.core.initial_data import load_initial_scheduled_jobs
    from backend.app.services.job_registry import job_registry

    assert "system.no_traffic_cleanup" not in job_registry(), "任务列表/调用目标里都不该再有它"
    assert "system.no_traffic_cleanup" not in {
        row["job_key"] for row in load_initial_scheduled_jobs()
    }, "随包种子里也不许再播这一行（否则新装用户又冒出来）"

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


def test_thread_path_also_writes_a_scheduled_job_log(tmp_path, monkeypatch):
    """线程路径（页面按钮）也要落**定时任务日志**（2026-10-13 用户口径）。

    清理任务走 `_run_in_thread` 这条线程路径时，除了独立日志表
    `no_traffic_cleanup_logs`，还必须写一条 `scheduled_job_logs`（任务中心可见），
    且收尾成 `success` 终态 + `manual.no_traffic_cleanup` 键 + `task_id` 挂载。

    ⚠️ 变异逼出来的判据：把 `_run_in_thread` 里的 `job_log = ScheduledJobLog(...)`
    整段删掉，独立日志表照写、其余用例照绿 —— 只有这条会红。所以判据必须盯
    「线程跑完之后 `scheduled_job_logs` 表里有没有那一行」。
    """
    account_id, SessionLocal = _setup(tmp_path, monkeypatch)

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
        job_logs = db.scalars(select(ScheduledJobLog)).all()
        assert len(job_logs) == 1, "线程路径（页面按钮）必须落一条定时任务日志"
        jl = job_logs[0]
        assert jl.job_key == "manual.no_traffic_cleanup", "键名要能对上任务中心"
        assert jl.job_name == "清理无流量笔记", "显示名要可读"
        assert jl.trigger_type == "manual", "手动触发"
        assert jl.task_id == task_id, "定时任务日志要挂到它服务的那条任务行上"
        assert jl.status == LOG_SUCCESS, "清理成功 ⇒ 定时任务日志也要收尾成 success"
        assert jl.finished_at is not None, "要有终态时间"
        assert jl.progress == 100, "收尾进度要打满"
    finally:
        db.close()


def test_goods_notes_delete_via_qianfan_and_normal_via_creator(tmp_path, monkeypatch):
    """商业笔记走千帆 `batch_delete_notes`，普通笔记走 creator `delete_note`（分流）。

    2026-10-06 需求「清理无流量商业笔记走千帆平台的笔记删除」。
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'split.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    monkeypatch.setattr(ncs, "SessionLocal", SessionLocal)
    monkeypatch.setattr(creator_api_adapter, "XhsCreatorApiAdapter", FakeCreatorApiAdapter)
    monkeypatch.setattr(qianfan_api_adapter, "XhsQianfanApiAdapter", FakeQianfanApiAdapter)
    monkeypatch.setattr(ncs, "_lock", ncs.CrawlRateLimiter())
    FakeCreatorApiAdapter.deleted = []
    FakeCreatorApiAdapter.in_promotion = set()
    FakeQianfanApiAdapter.deleted = []
    FakeQianfanApiAdapter.calls = []

    db = SessionLocal()
    try:
        user = User(username="owner", password_hash="x")
        db.add(user)
        db.flush()
        account = PlatformAccount(user_id=user.id, platform="xhs", sub_type="creator", nickname="我的号")
        db.add(account)
        db.flush()
        # creator + ark 两种 cookies 都给（分流依赖 ark cookies 存在）
        db.add(cookie_version(account.id, "access-token-creator.xiaohongshu.com=ctok; customer-sso-sid=sso", CHANNEL_CREATOR))
        db.add(cookie_version(account.id, "a1=xyz; access-token-ark=atok; customer-sso-sid=arksso", CHANNEL_ARK))
        account.seller_id = "SELLER-1"
        set_shop_state(account, status="active", goods_note_total=2)
        db.flush()
        # 一条商业笔记（goods_note）+ 一条普通笔记（空 flag），都无流量
        db.add(Note(
            user_id=user.id, platform_account_id=account.id, platform="xhs",
            note_id="GOODS-1", title="商业笔记",
        ))
        db.add(_stat("GOODS-1", account_id=account.id, user_id=user.id, flag="goods_note"))
        db.add(Note(
            user_id=user.id, platform_account_id=account.id, platform="xhs",
            note_id="NORMAL-1", title="普通笔记",
        ))
        db.add(_stat("NORMAL-1", account_id=account.id, user_id=user.id, flag=""))
        db.commit()
        account_id = account.id
    finally:
        db.close()

    result = ncs.run_no_traffic_cleanup(account_id, user_id=1)

    # 分流：商业 → 千帆批量，普通 → creator 逐篇
    assert sorted(FakeQianfanApiAdapter.deleted) == ["GOODS-1"], FakeQianfanApiAdapter.deleted
    assert sorted(FakeCreatorApiAdapter.deleted) == ["NORMAL-1"], FakeCreatorApiAdapter.deleted
    assert result["deleted"] == 2, result
    assert result["failed"] == 0, result


def test_goods_note_falls_back_to_creator_when_no_ark_cookies(tmp_path, monkeypatch):
    """无 ark cookies 时，商业笔记退回 creator 链路（不因缺令牌而漏删/误判）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'fallback.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    monkeypatch.setattr(ncs, "SessionLocal", SessionLocal)
    monkeypatch.setattr(creator_api_adapter, "XhsCreatorApiAdapter", FakeCreatorApiAdapter)
    monkeypatch.setattr(qianfan_api_adapter, "XhsQianfanApiAdapter", FakeQianfanApiAdapter)
    monkeypatch.setattr(ncs, "_lock", ncs.CrawlRateLimiter())
    FakeCreatorApiAdapter.deleted = []
    FakeCreatorApiAdapter.in_promotion = set()
    FakeQianfanApiAdapter.deleted = []
    FakeQianfanApiAdapter.calls = []

    db = SessionLocal()
    try:
        user = User(username="owner", password_hash="x")
        db.add(user)
        db.flush()
        account = PlatformAccount(user_id=user.id, platform="xhs", sub_type="creator", nickname="我的号")
        db.add(account)
        db.flush()
        # 只给 creator cookies，不给 ark
        db.add(cookie_version(account.id, "access-token-creator.xiaohongshu.com=ctok; customer-sso-sid=sso", CHANNEL_CREATOR))
        account.seller_id = "SELLER-1"
        set_shop_state(account, status="active", goods_note_total=1)
        db.flush()
        db.add(Note(
            user_id=user.id, platform_account_id=account.id, platform="xhs",
            note_id="GOODS-1", title="商业笔记",
        ))
        db.add(_stat("GOODS-1", account_id=account.id, user_id=user.id, flag="goods_note"))
        db.commit()
        account_id = account.id
    finally:
        db.close()

    result = ncs.run_no_traffic_cleanup(account_id, user_id=1)

    # 无 ark cookies → 千帆 adapter 为 None → 退回 creator
    assert FakeQianfanApiAdapter.deleted == []
    assert sorted(FakeCreatorApiAdapter.deleted) == ["GOODS-1"]
    assert result["deleted"] == 1, result


def test_goods_batch_capped_at_50_and_normal_at_20(tmp_path, monkeypatch):
    """商业笔记单批 ≤50（千帆一次批量删），普通笔记单批 ≤20（creator 逐篇，历史不变）。"""
    from backend.app.services.no_traffic_service import CleanupQuery

    engine = create_engine(
        f"sqlite:///{tmp_path / 'cap.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    monkeypatch.setattr(ncs, "SessionLocal", SessionLocal)
    monkeypatch.setattr(creator_api_adapter, "XhsCreatorApiAdapter", FakeCreatorApiAdapter)
    monkeypatch.setattr(qianfan_api_adapter, "XhsQianfanApiAdapter", FakeQianfanApiAdapter)
    monkeypatch.setattr(ncs, "_lock", ncs.CrawlRateLimiter(max_per_minute=1000))
    FakeCreatorApiAdapter.deleted = []
    FakeCreatorApiAdapter.in_promotion = set()
    FakeQianfanApiAdapter.deleted = []
    FakeQianfanApiAdapter.calls = []

    db = SessionLocal()
    try:
        user = User(username="owner", password_hash="x")
        db.add(user)
        db.flush()
        account = PlatformAccount(user_id=user.id, platform="xhs", sub_type="creator", nickname="我的号")
        db.add(account)
        db.flush()
        db.add(cookie_version(account.id, "access-token-creator.xiaohongshu.com=ctok; customer-sso-sid=sso", CHANNEL_CREATOR))
        db.add(cookie_version(account.id, "a1=xyz; access-token-ark=atok; customer-sso-sid=arksso", CHANNEL_ARK))
        account.seller_id = "SELLER-1"
        set_shop_state(account, status="active", goods_note_total=60)
        db.flush()
        # 60 条商业 + 30 条普通，都无流量
        for i in range(60):
            db.add(Note(
                user_id=user.id, platform_account_id=account.id, platform="xhs",
                note_id=f"G-{i:03d}", title=f"商业{i}",
            ))
            db.add(_stat(f"G-{i:03d}", account_id=account.id, user_id=user.id, flag="goods_note"))
        for i in range(30):
            db.add(Note(
                user_id=user.id, platform_account_id=account.id, platform="xhs",
                note_id=f"N-{i:03d}", title=f"普通{i}",
            ))
            db.add(_stat(f"N-{i:03d}", account_id=account.id, user_id=user.id, flag=""))
        db.commit()
        account_id = account.id
    finally:
        db.close()

    # ① 商业笔记 tab：query.category="goods" → 千帆一次批量删 ≤50
    result_goods = ncs.run_no_traffic_cleanup(
        account_id, user_id=1, batch_limit=20, query=CleanupQuery(category="goods")
    )
    assert len(FakeQianfanApiAdapter.calls) == 1, FakeQianfanApiAdapter.calls
    assert len(FakeQianfanApiAdapter.calls[0]) == 50, "商业单批截断到 50"
    assert FakeCreatorApiAdapter.deleted == [], "商业笔记不走 creator"
    assert result_goods["deleted"] == 50, result_goods

    # ② 普通笔记 tab：query.category="normal" → creator 逐篇 ≤20
    result_normal = ncs.run_no_traffic_cleanup(
        account_id, user_id=1, batch_limit=20, query=CleanupQuery(category="normal")
    )
    assert len(FakeCreatorApiAdapter.deleted) == 20, "普通单批截断到 20"
    assert result_normal["deleted"] == 20, result_normal
