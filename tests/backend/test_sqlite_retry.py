"""共用撞锁重试的测试（DEF-085，2026-10-03）。

`retry_on_lock` 是**多处同型链路的唯一实现**（台账记账等），
它的判据错一次，多处一起错 —— 所以判据本身要有独立的、变异可验证的守卫。

覆盖：
1. **判据**（`is_lock_error`）—— 只认锁竞争；`no such table` / 唯一约束 / 非
   SQLAlchemy 异常一律**不**算（否则确定性错误被无谓重试，白占业务线程）。
2. **重试行为** —— 撞锁重试到成功；非锁类**一次即止**；次数用尽后**原样抛**
   （本函数**不是**安全网，「不抛穿」由调用点自己包）。

⚠️ 2026-10-13 用户口径移除指标快照子系统后，原 ③ 段（批量采集 + `ON CONFLICT DO
UPDATE` 语义）**随快照采集一并移除** —— 判据对象已不存在，留着就是守卫一个已删实现。
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
