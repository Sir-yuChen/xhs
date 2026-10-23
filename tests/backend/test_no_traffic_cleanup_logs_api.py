"""清理无流量笔记 —— **笔记维度操作日志**的端点契约测试（2026-10-06 用户口径）。

> 「清理无流量的主要对象是笔记，**所有主体应是笔记维度**，记录操作日志时也应该是
>   笔记维度」+「清理无流量笔记失败，分析原因：候选池仅包含已同步千帆挂链映射的
>   笔记；未同步/未绑定千帆的笔记不在池内」

本文件钉住**读侧契约**（写侧在 `test_no_traffic_cleanup_runner.py`）：

1. `GET /users/no-traffic/cleanup-logs` 的每行**内联 `items[]`**（笔记维度明细），
   形状与 `GET /tasks` 的 `children` 同款（父子两层）。
2. 批次行的 `reason` 是**失败原因**，`coverage_note` 才是候选池覆盖度说明
   （DEF-095 —— 修复前 `reason` 被错写成覆盖度文案，把用户指向错误方向）。
3. 跨租户不可见（别人的日志与明细一行都读不到）。

⚠️ 用 `api_client`（自动把模块级 SessionLocal 打到测试库）—— 清理线程自建
Session 才会落测试库。这里不发真实平台请求：明细行**直接造**（读侧契约只关心
「存进去的形状能不能按笔记维度读出来」），写侧由专用文件覆盖。
"""
from __future__ import annotations

from typing import Any

from backend.app.models import NoTrafficCleanupItem, NoTrafficCleanupLog
from backend.app.models.platform_account import PlatformAccount
from backend.app.core.time import shanghai_now

import pytest


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.contract

def _register(client: Any, email: str = "a@test.com") -> tuple[str, int]:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "pw123456", "username": "user_" + email.split("@")[0]},
    )
    assert r.status_code in (200, 201), r.text
    token = r.json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    return token, me.json()["id"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_log_with_items(session_factory: Any, *, user_id: int, **overrides: Any) -> int:
    """造一条批次行 + 两篇笔记明细，返回批次行 id。"""
    db = session_factory()
    try:
        account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="creator", nickname="我的号"
        )
        db.add(account)
        db.flush()
        log = NoTrafficCleanupLog(
            user_id=user_id,
            account_id=account.id,
            account_name="我的号",
            status="failed",
            before_total=10,
            after_total=9,
            total=2,
            deleted=1,
            failed=1,
            **overrides,
        )
        db.add(log)
        db.flush()
        db.add(
            NoTrafficCleanupItem(
                user_id=user_id,
                account_id=account.id,
                account_name="我的号",
                log_id=log.id,
                note_id="NOTE-OK",
                note_title="被删掉的那篇",
                result="deleted",
                operated_at=shanghai_now(),
            )
        )
        db.add(
            NoTrafficCleanupItem(
                user_id=user_id,
                account_id=account.id,
                account_name="我的号",
                log_id=log.id,
                note_id="NOTE-BAD",
                note_title="打不开的那篇",
                result="failed",
                reason="权限校验失败：账号存在风险，请重新登录",
                operated_at=shanghai_now(),
            )
        )
        db.commit()
        return int(log.id)
    finally:
        db.close()


def test_cleanup_logs_inline_the_note_dimension_items(api_client: Any, db_factory: Any):
    """每行内联 `items[]`：**一篇笔记一条**，逐篇可查（用户口径「笔记维度」）。"""
    token, user_id = _register(api_client)
    _make_log_with_items(db_factory, user_id=user_id)

    r = api_client.get("/api/xhs/pc/users/no-traffic/cleanup-logs", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    row = body["items"][0]

    assert "items" in row, "批次行没有内联笔记明细（用户口径「笔记维度」的落点）"
    assert len(row["items"]) == 2
    by_note = {item["note_id"]: item for item in row["items"]}
    assert set(by_note) == {"NOTE-OK", "NOTE-BAD"}
    # **笔记维度的主体字段**：标题 / 结果 / 原因 / 操作时间。
    assert by_note["NOTE-OK"]["note_title"] == "被删掉的那篇"
    assert by_note["NOTE-OK"]["result"] == "deleted"
    assert by_note["NOTE-BAD"]["reason"] == "权限校验失败：账号存在风险，请重新登录"
    assert by_note["NOTE-BAD"]["operated_at"], "操作时间（用户口径）"
    # 明细自带归属，不依赖 join 回批次行。
    assert by_note["NOTE-BAD"]["account_id"] == row["account_id"]
    assert by_note["NOTE-BAD"]["log_id"] == row["id"]


def test_cleanup_log_reason_is_the_failure_not_the_coverage_note(
    api_client: Any, db_factory: Any
):
    """**DEF-095 契约**：`reason` 与 `coverage_note` 是两个不同的字段。

    ⚠️ 修复前 `reason` 里装的是候选池覆盖度文案 —— 平台风控失败时界面
    「失败原因」列显示「候选池仅包含已同步千帆挂链映射的笔记…」，
    一句正常态说明被当成了失败归因。
    """
    token, user_id = _register(api_client)
    _make_log_with_items(
        db_factory,
        user_id=user_id,
        reason="权限校验失败：账号存在风险，请重新登录",
        coverage_note="候选池仅包含已同步千帆挂链映射的笔记；未同步/未绑定千帆的笔记不在池内",
        aborted=True,
        abort_reason="权限校验失败：账号存在风险，请重新登录",
    )

    r = api_client.get("/api/xhs/pc/users/no-traffic/cleanup-logs", headers=_auth(token))
    row = r.json()["items"][0]
    assert "账号存在风险" in row["reason"], row["reason"]
    assert "候选池仅包含" not in row["reason"], "覆盖度文案不许出现在失败原因里"
    assert "候选池仅包含" in row["coverage_note"]
    assert row["aborted"] is True
    assert "账号存在风险" in row["abort_reason"]


def test_cleanup_logs_are_scoped_to_the_owner(api_client: Any, db_factory: Any):
    """跨租户：别人的批次行**与明细**一行都读不到。"""
    token_a, user_a = _register(api_client, "a@test.com")
    token_b, user_b = _register(api_client, "b@test.com")
    _make_log_with_items(db_factory, user_id=user_a)

    r = api_client.get("/api/xhs/pc/users/no-traffic/cleanup-logs", headers=_auth(token_b))
    assert r.status_code == 200
    assert r.json()["total"] == 0, "B 不该看到 A 的清理日志"

    r2 = api_client.get("/api/xhs/pc/users/no-traffic/cleanup-logs", headers=_auth(token_a))
    assert r2.json()["total"] == 1
    assert len(r2.json()["items"][0]["items"]) == 2, "明细不该被租户过滤误伤"


def test_items_without_a_batch_row_are_not_leaked_into_any_log(
    api_client: Any, db_factory: Any
):
    """批次行被删掉的明细（`log_id` 置 NULL）**不挂到任何别人的行上**。

    `log_id` 用 `SET NULL`：明细比批次行活得久。但「活得久」不等于「可以乱挂」——
    端点按 `log_id IN (本页批次行)` 取数，孤儿明细不该出现在任何一行里。
    """
    token, user_id = _register(api_client)
    log_id = _make_log_with_items(db_factory, user_id=user_id)

    db = db_factory()
    try:
        db.query(NoTrafficCleanupItem).filter(
            NoTrafficCleanupItem.log_id == log_id
        ).update({"log_id": None})
        db.commit()
    finally:
        db.close()

    r = api_client.get("/api/xhs/pc/users/no-traffic/cleanup-logs", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["items"][0]["items"] == [], "孤儿明细不该挂到任何批次行上"
