"""LLM 网关预警**自动触发** + 站内消息模块补强（2026-10-13 需求 R2）。

用户口径原文：
> 「大模型的预警移除 LLM 网关预警任务，实现根据配置的规则自动触发，并站内消息提醒；
> 分析当前项目的站内消息模块是否完善成熟，优化不足的地方完善相关逻辑，提升触发消息提示效率。」

本文件盯四件事：

1. **定时任务已移除**：注册表里没有 `system.llm_gateway_monitor`（改自动触发）；
2. **自动触发**：台账写入超阈值 ⇒ 只需投递 + 评估即写通知（不必等调度）；
3. **去重保留**：同日只响一次、去抖合并；
4. **站内消息补强**：未读计数走独立端点（不再当页推断）、真分页、按来源/等级过滤。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.app.core.time import shanghai_now
from backend.app.models import (
    LlmGatewayAlert,
    LlmGatewayConfig,
    LlmGatewayUsageLog,
    Notification,
    User,
)
from backend.app.services import llm_gateway_alert_trigger as trigger


@pytest.fixture(autouse=True)
def _reset_trigger_state():
    """worker / 待办是**进程级全局** ⇒ 用例间必须隔离（否则跨用例串味）。"""
    trigger._reset_for_tests()
    yield
    trigger._reset_for_tests()


@pytest.fixture()
def _patched_sessions(monkeypatch, db_factory):
    """服务层模块级 `SessionLocal` 打到测试库上（`evaluate_and_notify` 自开 Session）。

    ⚠️ 不打桩 = 断言看不到自己写的行，而请求已落到真实库（conftest 立过的规矩）。
    """
    from tests.backend.conftest import patch_service_sessions

    patched = patch_service_sessions(monkeypatch, db_factory)
    # 断言确实打到了本需求关心的模块（不靠「测试没炸」推断）
    assert "backend.app.services.llm_gateway_alert_trigger.SessionLocal" in patched
    return patched


def _seed_user_with_gateway(db_factory, username="alert-owner"):
    db = db_factory()
    try:
        user = User(username=username, password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)
        gw = LlmGatewayConfig(
            user_id=user.id,
            provider_key="DEEPSEEK-AAAA",
            name="DeepSeek",
            base_url="https://api.deepseek.com/v1",
            access_token="sk-x",
            healthy=True,
        )
        db.add(gw)
        db.commit()
        db.refresh(gw)
        alert = LlmGatewayAlert(
            user_id=user.id,
            gateway_id=gw.id,
            platform="",
            function="",
            model_name="",
            metric="fail_rate",
            operator="gt",
            threshold=0.5,
            window_seconds=86400,
            level="warning",
            enabled=True,
        )
        db.add(alert)
        db.commit()
        return user.id, gw.id, alert.id
    finally:
        db.close()


def _add_failed_call(db_factory, user_id, gateway_id, status="failed"):
    db = db_factory()
    try:
        db.add(
            LlmGatewayUsageLog(
                user_id=user_id,
                gateway_id=gateway_id,
                platform="xhs",
                function="generate",
                model_name="deepseek-chat",
                prompt_tokens=1,
                completion_tokens=1,
                latency_ms=5,
                status=status,
            )
        )
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 1. 定时任务已移除
# --------------------------------------------------------------------------- #


def test_monitor_job_is_gone_from_registry():
    from backend.app.services.job_registry import job_registry

    assert "system.llm_gateway_monitor" not in job_registry(), (
        "预警已改自动触发，注册表不应再有该定时任务"
    )


# --------------------------------------------------------------------------- #
# 2. 自动触发
# --------------------------------------------------------------------------- #


def test_auto_trigger_fires_notification_without_scheduler(db_factory, _patched_sessions):
    user_id, gw_id, _ = _seed_user_with_gateway(db_factory)
    # 3 条失败 0 条成功 ⇒ 失败率 1.0 > 0.5 ⇒ 命中
    for _ in range(3):
        _add_failed_call(db_factory, user_id, gw_id)

    fired = trigger.evaluate_and_notify(user_id, gw_id)
    assert fired == 1

    db = db_factory()
    try:
        notes = db.scalars(
            select(Notification).where(Notification.user_id == user_id)
        ).all()
        assert len(notes) == 1
        assert notes[0].source_type == "llm_gateway_alert"
    finally:
        db.close()


def test_notify_usage_recorded_is_safe_and_non_blocking(db_factory):
    """投递本身绝不抛异常（无论目标是否存在）。"""
    trigger.notify_usage_recorded(999999, 999999)  # 不存在的用户/服务商


def test_below_threshold_does_not_fire(db_factory, _patched_sessions):
    user_id, gw_id, _ = _seed_user_with_gateway(db_factory, "alert-below")
    # 1 失败 3 成功 ⇒ 失败率 0.25 < 0.5 ⇒ 不触发
    _add_failed_call(db_factory, user_id, gw_id, status="failed")
    for _ in range(3):
        _add_failed_call(db_factory, user_id, gw_id, status="success")

    assert trigger.evaluate_and_notify(user_id, gw_id) == 0


def test_same_day_dedup_is_preserved(db_factory, _patched_sessions):
    """同日去重仍生效（换触发点不许丢去重）。"""
    user_id, gw_id, alert_id = _seed_user_with_gateway(db_factory, "alert-dedup")
    for _ in range(3):
        _add_failed_call(db_factory, user_id, gw_id)

    assert trigger.evaluate_and_notify(user_id, gw_id) == 1
    assert trigger.evaluate_and_notify(user_id, gw_id) == 0, "同日不应重复发"


def test_gateway_scope_ignores_other_gateway(db_factory, _patched_sessions):
    """服务商作用域的规则，写别的服务商的台账不应触发它。"""
    user_id, gw_id, _ = _seed_user_with_gateway(db_factory, "alert-scope")
    db = db_factory()
    try:
        other = LlmGatewayConfig(
            user_id=user_id,
            provider_key="OTHER-BBBB",
            name="Other",
            base_url="https://other/v1",
            access_token="sk-y",
        )
        db.add(other)
        db.commit()
        db.refresh(other)
        other_id = other.id
    finally:
        db.close()

    for _ in range(3):
        _add_failed_call(db_factory, user_id, other_id)
    # 评估 gw_id（规则挂在 gw_id 上）⇒ 不应命中
    assert trigger.evaluate_and_notify(user_id, gw_id) == 0


# --------------------------------------------------------------------------- #
# 3. 去抖
# --------------------------------------------------------------------------- #


def test_debounce_merges_repeated_targets():
    """同一目标在去抖窗口内的多次投递被合并（`_last_done` 记录时间）。"""
    trigger.notify_usage_recorded(1, 2)
    trigger.notify_usage_recorded(1, 2)
    # 首次处理会记录 _last_done；紧接的重复投递不重复评估。
    trigger._handle_one(1, 2)
    assert (1, 2) in trigger._last_done
    # 再次立即处理 ⇒ 被去抖放回待办（不重复评估）
    trigger._pending.clear()
    trigger._handle_one(1, 2)
    assert (1, 2) in trigger._pending, "去抖窗口内的重复应被放回待办合并"


# --------------------------------------------------------------------------- #
# 4. 站内消息补强
# --------------------------------------------------------------------------- #


def _register(api_client, username):
    resp = api_client.post(
        "/api/auth/register", json={"username": username, "password": "Passw0rd!"}
    )
    assert resp.status_code in (200, 201), resp.text
    token = resp.json().get("access_token")
    return {"Authorization": f"Bearer {token}"} if token else {}


def test_unread_count_endpoint_counts_beyond_page(api_client, db_factory):
    """未读数走独立端点：>page_size 时也准（旧前端当页推断会截断）。"""
    headers = _register(api_client, "notif-unread")
    db = db_factory()
    try:
        user = db.scalar(select(User).where(User.username == "notif-unread"))
        for i in range(25):
            db.add(
                Notification(user_id=user.id, title=f"n{i}", body="", level="info")
            )
        db.commit()
    finally:
        db.close()

    resp = api_client.get("/api/notifications/unread-count", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["unread"] == 25


def test_list_is_truly_paginated(api_client, db_factory):
    """列表真分页：total 为全量、items 为当页。"""
    headers = _register(api_client, "notif-page")
    db = db_factory()
    try:
        user = db.scalar(select(User).where(User.username == "notif-page"))
        for i in range(25):
            db.add(
                Notification(user_id=user.id, title=f"n{i}", body="", level="info")
            )
        db.commit()
    finally:
        db.close()

    resp = api_client.get("/api/notifications?page=1&page_size=10", headers=headers)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 25
    assert len(payload["items"]) == 10
    resp2 = api_client.get("/api/notifications?page=3&page_size=10", headers=headers)
    assert len(resp2.json()["items"]) == 5


def test_list_filters_by_source_type_and_level(api_client, db_factory):
    headers = _register(api_client, "notif-filter")
    db = db_factory()
    try:
        user = db.scalar(select(User).where(User.username == "notif-filter"))
        db.add(Notification(user_id=user.id, title="a", source_type="llm_gateway_alert", level="warning"))
        db.add(Notification(user_id=user.id, title="b", source_type="task", level="error"))
        db.commit()
    finally:
        db.close()

    resp = api_client.get("/api/notifications?source_type=llm_gateway_alert", headers=headers)
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["title"] == "a"

    resp2 = api_client.get("/api/notifications?level=error", headers=headers)
    assert resp2.json()["total"] == 1
    assert resp2.json()["items"][0]["title"] == "b"
