"""共用撞锁重试的测试（DEF-085，2026-10-03）。

`retry_on_lock` 是**三处同型链路的唯一实现**（台账记账 + 两个快照采集），
它的判据错一次，三处一起错 —— 所以判据本身要有独立的、变异可验证的守卫。

覆盖：
1. **判据**（`is_lock_error`）—— 只认锁竞争；`no such table` / 唯一约束 / 非
   SQLAlchemy 异常一律**不**算（否则确定性错误被无谓重试，白占业务线程）。
2. **重试行为** —— 撞锁重试到成功；非锁类**一次即止**；次数用尽后**原样抛**
   （本函数**不是**安全网，「不抛穿」由调用点自己包）。
3. **批量采集不再撞锁** —— `_SNAPSHOT_BATCH` 存在且 SQL 逐字仍是 `ON CONFLICT DO UPDATE`
   （防「为重试而改成先删后插」这类语义漂移）。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from backend.app.services import sqlite_retry


def _op_error(message: str) -> OperationalError:
    return OperationalError("INSERT INTO t", {}, Exception(message))


# --------------------------------------------------------------------------- #
# ① 判据
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message, expected",
    [
        ("database is locked", True),
        ("sqlite3.OperationalError: database is locked", True),
        ("no such table: note_metric_snapshots", False),
        ("UNIQUE constraint failed: shop_metric_snapshots.product_pk", False),
        ("disk I/O error", False),
        ("no such column: foo", False),
    ],
)
def test_is_lock_error_only_matches_lock_contention(message, expected):
    """判据必须**只认锁竞争**。

    ⚠️ 变异守护：`return True`（把所有 OperationalError 都当锁）⇒
    `no such table` 那两条必红；`return False` ⇒ 前两条必红。
    """
    assert sqlite_retry.is_lock_error(_op_error(message)) is expected


def test_is_lock_error_rejects_non_operational_errors():
    """非 SQLAlchemy 异常一律不重试（例如业务层 ValueError）。

    ⚠️ 变异守护：去掉 `isinstance` 检查、只留文案匹配 ⇒ 本用例必红
    （`ValueError("database is locked")` 会被误判成锁竞争，然后重试 3 次）。
    """
    assert sqlite_retry.is_lock_error(ValueError("database is locked")) is False
    assert sqlite_retry.is_lock_error(RuntimeError("locked")) is False


# --------------------------------------------------------------------------- #
# ② 重试行为
# --------------------------------------------------------------------------- #
def test_retry_returns_first_success_without_sleeping():
    """一次成功 ⇒ 不重试、不 sleep。"""
    calls = {"n": 0}
    slept: list[float] = []

    def op():
        calls["n"] += 1
        return "ok"

    result = sqlite_retry.retry_on_lock(op, what="t", sleep=slept.append)
    assert result == "ok"
    assert calls["n"] == 1
    assert slept == [], "成功路径不该 sleep"


def test_retry_retries_lock_until_success():
    """撞锁重试到成功；sleep 是**指数退避**（0.1 / 0.2 …）。

    ⚠️ 变异守护：把退避改成固定值（`base_delay` 不带 `2**attempt`）⇒
    本用例的第二段断言必红。
    """
    calls = {"n": 0}
    slept: list[float] = []

    def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _op_error("database is locked")
        return "ok"

    result = sqlite_retry.retry_on_lock(op, what="t", sleep=slept.append)
    assert result == "ok"
    assert calls["n"] == 3
    assert slept == pytest.approx([0.1, 0.2]), "指数退避（base 0.1 × 2^n）"


def test_retry_does_not_retry_non_lock_errors():
    """确定性错误**一次即止**（不空转）。"""
    calls = {"n": 0}
    slept: list[float] = []

    def op():
        calls["n"] += 1
        raise _op_error("no such table: t")

    with pytest.raises(OperationalError):
        sqlite_retry.retry_on_lock(op, what="t", sleep=slept.append)
    assert calls["n"] == 1, "非锁类错误必须一次即止"
    assert slept == []


def test_retry_raises_after_exhausting_attempts():
    """锁一直不放 ⇒ 用尽后**原样抛**（本函数不是安全网）。

    ⚠️ 变异守护：改成「用尽返回 None / 吞掉异常」⇒ 本用例必红。
    吞异常是本项目最贵的缺陷类型（静默丢数据，DEF-048/085）。
    """
    calls = {"n": 0}
    slept: list[float] = []

    def op():
        calls["n"] += 1
        raise _op_error("database is locked")

    with pytest.raises(sqlite_retry.LockRetryExhausted):
        sqlite_retry.retry_on_lock(op, what="t", attempts=3, sleep=slept.append)
    assert calls["n"] == 3
    assert len(slept) == 2, "3 次尝试 ⇒ 2 次退避（最后一次不再等）"


# --------------------------------------------------------------------------- #
# ③ 采集链路：批量 + 语义不变
# --------------------------------------------------------------------------- #
def test_snapshot_batch_size_is_defined_and_sane():
    """两侧都定义了批大小，且足够大到真有「批」的效果。

    ⚠️ 变异守护：把 `_SNAPSHOT_BATCH` 改成 1（等于没批，退回逐行 execute）
    ⇒ 本用例必红。逐行 execute 正是 DEF-085 里持锁 6 秒的原因。
    """
    from backend.app.services import note_metrics_service, shop_metrics_service

    for module in (note_metrics_service, shop_metrics_service):
        size = module._SNAPSHOT_BATCH
        assert 100 <= size <= 1000, f"{module.__name__} 的批大小 {size} 不在合理区间"


def test_lock_exhaustion_is_retryable_not_internal_error():
    """用尽重试必须抛 `LockRetryExhausted`（可重试语义），**不是**裸 OperationalError。

    ⚠️ 变异守护：改成 `raise`（把原 OperationalError 抛出去）⇒ 本用例必红。
    裸 OperationalError 冒到 FastAPI = **500 服务器内部错误**，
    而写锁竞争是**可预期**的瞬时失败 ⇒ 会让用户以为功能坏了，也污染错误率口径。
    """
    def op():
        raise _op_error("database is locked")

    with pytest.raises(sqlite_retry.LockRetryExhausted) as info:
        sqlite_retry.retry_on_lock(op, what="采集", attempts=2, sleep=lambda _s: None)
    assert info.value.attempts == 2
    assert isinstance(info.value.cause, OperationalError)


def test_non_lock_error_is_not_wrapped():
    """确定性错误**不得**被包成可重试语义（否则调用点会去重试一个必错的请求）。"""
    def op():
        raise _op_error("no such table: t")

    with pytest.raises(OperationalError) as info:
        sqlite_retry.retry_on_lock(op, what="采集", sleep=lambda _s: None)
    assert not isinstance(info.value, sqlite_retry.LockRetryExhausted)


def test_snapshots_use_on_conflict_do_update_not_delete_insert():
    """采集必须仍是 `ON CONFLICT DO UPDATE`（同日覆盖），**不许**为先删后插。

    ⚠️ 变异守护：把 `on_conflict_do_update` 换成「先 delete 再 insert」⇒ 本用例必红。

    为什么钉死：先删后插是**两次写**，中间有窗口（另一个读者会看到空窗），
    且删+插的锁持有时间**更长** —— 正好与 DEF-085 的修法方向相反。
    """
    import inspect

    from backend.app.services import note_metrics_service, shop_metrics_service

    for module in (note_metrics_service, shop_metrics_service):
        source = inspect.getsource(module.collect_snapshots)
        assert "on_conflict_do_update" in source, f"{module.__name__} 丢了覆盖语义"
        assert "delete(" not in source, f"{module.__name__} 不得先删后插"
