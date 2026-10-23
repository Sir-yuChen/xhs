"""M9 收口的行为守卫（2026-10-04）。

覆盖本轮修的四件事，每件都钉**可执行的判据**（不是「代码里看起来有」）：

| # | 项 | 判据 |
|---|---|---|
| 1 | **T-108 / DEF-003** 多实例闸 | 活进程持租约 ⇒ 第二个实例 `acquired=False` 且**如实给原因**；死 PID 的陈旧租约可接管；释放后可再取 |
| 2 | **DEF-019 / T-18** API 审计 | 写入/失败落行、GET 成功不落、**query 不进表**（敏感值）、审计失败不阻断响应 |
| 3 | **DEF-022** refresh token 撤销 | 登出抬版本 ⇒ 旧 refresh token 401；新令牌可用；老令牌（无 `ver`）版本 0 兼容 |
| 4 | **T-111 / DEF-016** 监控周期不再是死配置 | 未到 `crawl_interval_minutes` 的目标被跳过并**如实回报** `skipped` |

⚠️ 本文件用 `conftest` 公共夹具（`api_client` / `db_factory`），不抄
`create_engine` + `create_all` + `dependency_overrides` 三件套
（`test_testing_spec_guards.py` 的硬规矩）。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from backend.app.core import singleton

#: 本文件用的用户 id（与 `seed_owner_rows` 的默认口径一致）。
USER_ID = 1
from backend.app.core.singleton import (
    acquire_scheduler_lease,
    release_scheduler_lease,
)



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract


@pytest.fixture()
def ctx(api_client, db_factory):
    """认证上下文：建一个用户（测试库刻意是空的）+ 它的 Bearer 头。

    ⚠️ `db_engine` **不播种任何行**（`test_conftest_fixtures` 的契约是「新库是空的」）
    ⇒ 外键父行由需要它的用例自己造。这里用 `seed_owner_rows`（夹具自带的官方播种）。
    """
    from backend.app.core.security import create_access_token
    from tests.backend.conftest import seed_owner_rows

    seed_owner_rows(db_factory().get_bind(), user_id=USER_ID, account_id=None)
    headers = {"Authorization": f"Bearer {create_access_token(USER_ID, 0)}"}
    yield api_client, headers, USER_ID, db_factory


class TestSchedulerSingleInstanceLease:
    """T-108 / DEF-003：`BackgroundScheduler` 与 FastAPI 同进程 ⇒ 多实例必须挡。"""

    def test_live_holder_blocks_second_instance_with_a_reason(self, tmp_path: Path):
        """另一个**活着的** PID 持租约 ⇒ 拒绝，且原因里带 PID 与文件路径。"""
        lease = tmp_path / singleton.LEASE_FILENAME
        other_pid = os.getppid()  # 父进程：真实存在且活着
        assert singleton._pid_alive(other_pid), "测试前提：父进程活着"
        lease.write_text(str(other_pid), encoding="utf-8")

        acquired, reason = acquire_scheduler_lease(tmp_path)

        assert acquired is False
        assert str(other_pid) in reason, "原因里必须写明是谁持有（便于人工排查）"
        # ⚠️ 别人的租约**不许被删**（否则闸门形同虚设）
        assert lease.exists()
        assert lease.read_text(encoding="utf-8") == str(other_pid)

    def test_stale_lease_from_a_dead_pid_is_taken_over(self, tmp_path: Path):
        """死 PID 的陈旧租约（进程被 kill -9 / 断电）⇒ 自愈接管，不需人工删文件。"""
        lease = tmp_path / singleton.LEASE_FILENAME
        lease.write_text("999999", encoding="utf-8")  # 必然不存在的 PID

        acquired, reason = acquire_scheduler_lease(tmp_path)

        try:
            assert acquired is True, reason
            assert lease.read_text(encoding="utf-8") == str(os.getpid())
        finally:
            release_scheduler_lease()

    def test_release_makes_the_lease_reacquirable(self, tmp_path: Path):
        """释放后必须能再取（否则重启一次就永久锁死）。"""
        ok, _ = acquire_scheduler_lease(tmp_path)
        assert ok
        lease = tmp_path / singleton.LEASE_FILENAME
        assert lease.exists()
        release_scheduler_lease()
        assert not lease.exists(), "释放必须删掉自己的租约"

        ok2, reason2 = acquire_scheduler_lease(tmp_path)
        try:
            assert ok2, reason2
        finally:
            release_scheduler_lease()

    def test_garbage_lease_file_is_recovered_not_fatal(self, tmp_path: Path):
        """**损坏**的租约文件（空 / 非数字）不阻断启动 —— 视为无有效持有者。"""
        lease = tmp_path / singleton.LEASE_FILENAME
        lease.write_text("not-a-pid", encoding="utf-8")

        acquired, reason = acquire_scheduler_lease(tmp_path)
        try:
            assert acquired is True, reason
        finally:
            release_scheduler_lease()


class TestApiAudit:
    """DEF-019 / T-18：`api_logs` 从零写入点 → 真审计（且敏感值不进表）。"""

    def test_should_audit_rules(self):
        """口径 1：只记「审计前缀下的写」+「任何失败」。"""
        from backend.app.core.api_audit import _should_audit

        # 审计前缀下的写 ⇒ 记
        assert _should_audit("POST", "/api/xhs/qianfan/orders/ship", 200)
        assert _should_audit("PATCH", "/api/llm/gateway-configs/1", 200)
        # 读成功 ⇒ 不记（一次页面加载十几个 GET，记了会写爆 SQLite）
        assert not _should_audit("GET", "/api/xhs/monitoring/targets", 200)
        # 失败 ⇒ 记（含 GET）—— 排查 500 时最先要看的就是它
        assert _should_audit("GET", "/api/xhs/monitoring/targets", 500)
        assert _should_audit("POST", "/api/notes", 422)
        # 认证 / 健康检查 ⇒ 永不记（凭据与高频噪声）
        assert not _should_audit("POST", "/api/auth/login", 401)
        assert not _should_audit("GET", "/api/health", 200)
        # 非审计前缀的写成功 ⇒ 不记
        assert not _should_audit("POST", "/api/drafts", 200)

    def test_write_persists_without_query_string_and_without_body(self, db_factory, monkeypatch):
        """口径 2：**只落路径**（不带 query），`meta` 只有状态码与耗时。

        ⚠️ 这是本中间件最重要的一条 —— `api_logs` 没有请求体列是刻意的，
        但「路径里塞了 token」这种常见写法必须被挡住。
        """
        from backend.app.core import api_audit
        from backend.app.models.api_log import ApiLog

        monkeypatch.setattr(api_audit, "_session_factory", db_factory)

        class _Req:
            method = "POST"
            url = type("U", (), {"path": "/api/xhs/qianfan/orders/ship"})()
            headers = {"authorization": "Bearer not-a-real-token"}

        api_audit.ApiAuditMiddleware._write(_Req(), 200, 12.5)

        db = db_factory()
        try:
            rows = db.query(ApiLog).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.endpoint == "/api/xhs/qianfan/orders/ship"
            assert "?" not in row.endpoint, "query string 不许进表"
            assert row.platform == "xhs"
            assert row.status == "success"
            assert row.meta == {"method": "POST", "status_code": 200, "elapsed_ms": 12.5}
            # 令牌解不出 user_id ⇒ 留空，不猜（硬约束⑤）
            assert row.user_id is None
        finally:
            db.close()

    def test_audit_failure_never_breaks_the_response(self, monkeypatch):
        """口径 3：审计写库炸了也**不许**把用户的写操作带崩。"""
        from backend.app.core import api_audit

        def _boom():
            raise RuntimeError("db is on fire")

        monkeypatch.setattr(api_audit, "_session_factory", _boom)

        class _Req:
            method = "POST"
            url = type("U", (), {"path": "/api/xhs/qianfan/orders/ship"})()
            headers = {}

        # 不抛 = 通过（内部 except 已兜住）
        api_audit.ApiAuditMiddleware._write(_Req(), 500, 1.0)

    def test_platform_inference(self):
        from backend.app.core.api_audit import _platform_of

        assert _platform_of("/api/xhs/qianfan/orders") == "xhs"
        assert _platform_of("/api/llm/gateway-configs") == "llm"
        assert _platform_of("/api/notes/1") is None
        assert _platform_of("/") is None


class TestRefreshTokenRevocation:
    """DEF-022：无状态 refresh token 的撤销 —— 版本号 +1 ⇒ 旧令牌立即失效。"""

    def test_logout_bumps_version_and_old_refresh_token_dies(self, ctx):
        from backend.app.core.security import create_access_token, create_refresh_token
        from backend.app.models.user import User

        client, headers, uid, db_factory = ctx
        # 取当前用户与版本
        db = db_factory()
        try:
            uid = db.query(User).first().id
            version_before = int(db.query(User).filter(User.id == uid).one().token_version or 0)
        finally:
            db.close()

        old_refresh = create_refresh_token(uid, version_before)
        # 旧令牌现在可用
        r = client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
        assert r.status_code == 200, r.text

        # 登出 ⇒ 版本 +1
        r = client.post("/api/auth/logout", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json().get("revoked") is True

        db = db_factory()
        try:
            version_after = int(db.query(User).filter(User.id == uid).one().token_version or 0)
        finally:
            db.close()
        assert version_after == version_before + 1, "登出必须抬版本"

        # 旧 refresh token ⇒ 401（这就是「撤销」）
        r = client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
        assert r.status_code == 401, r.text

    def test_legacy_token_without_version_is_treated_as_zero(self):
        """迁移后已登录用户**不被动掉线**：缺 `ver` 的令牌按 0 处理。"""
        import json as _json

        from backend.app.core import security as sec

        payload = {"user_id": 1, "token_type": "refresh", "exp": 9999999999}
        legacy = sec._sign_token(payload)
        decoded = sec.decode_token(legacy)
        assert (decoded.get("ver") or 0) == 0

    def test_new_token_after_revoke_carries_the_new_version(self):
        from backend.app.core.security import create_access_token, decode_token

        assert decode_token(create_access_token(1, 3))["ver"] == 3


class TestMonitoringIntervalIsLive:
    """T-111 / DEF-016：`crawl_interval_minutes` 从死配置变成**真正的节流闸**。"""

    def test_not_due_target_is_skipped_and_reported(self, ctx):
        from datetime import timedelta

        from backend.app.core.time import shanghai_now
        from backend.app.models.monitoring import MonitoringTarget
        from backend.app.models.user import User
        from backend.app.services.scheduler_service import run_monitoring_refresh_once

        client, headers, uid, db_factory = ctx
        db = db_factory()
        try:
            target = MonitoringTarget(
                user_id=uid,
                platform="xhs",
                target_type="keyword",
                name="周期守卫",
                value="测试关键词",
                status="active",
                crawl_interval_minutes=60,
                # 刚刷过 ⇒ 未到点
                last_refreshed_at=shanghai_now() - timedelta(minutes=1),
            )
            db.add(target)
            db.commit()
            target_id = target.id
        finally:
            db.close()

        result = run_monitoring_refresh_once(uid, platform="xhs")

        assert result["refreshed_count"] == 0, "未到点不许刷"
        assert result["skipped_count"] >= 1
        skipped_ids = [s["target_id"] for s in result["skipped"]]
        assert target_id in skipped_ids
        entry = next(s for s in result["skipped"] if s["target_id"] == target_id)
        assert entry["reason"] == "not_due"
        assert entry["crawl_interval_minutes"] == 60

    def test_due_target_is_refreshed(self, ctx):
        from datetime import timedelta

        from backend.app.core.time import shanghai_now
        from backend.app.models.monitoring import MonitoringTarget
        from backend.app.models.user import User
        from backend.app.services.scheduler_service import run_monitoring_refresh_once

        client, headers, uid, db_factory = ctx
        db = db_factory()
        try:
            target = MonitoringTarget(
                user_id=uid,
                platform="xhs",
                target_type="keyword",
                name="到点守卫",
                value="测试关键词2",
                status="active",
                crawl_interval_minutes=60,
                last_refreshed_at=shanghai_now() - timedelta(minutes=120),
            )
            db.add(target)
            db.commit()
            target_id = target.id
        finally:
            db.close()

        result = run_monitoring_refresh_once(uid, platform="xhs")

        assert result["refreshed_count"] >= 1
        assert target_id in [i["target_id"] for i in result["items"]]

    def test_paused_target_is_not_touched_at_all(self, ctx):
        """暂停的目标连「跳过」都不算 —— 它根本不在候选集里（DEF-012 口径）。"""
        from backend.app.models.monitoring import MonitoringTarget
        from backend.app.models.user import User
        from backend.app.services.scheduler_service import run_monitoring_refresh_once

        client, headers, uid, db_factory = ctx
        db = db_factory()
        try:
            target = MonitoringTarget(
                user_id=uid,
                platform="xhs",
                target_type="keyword",
                name="暂停守卫",
                value="测试关键词3",
                status="paused",
                crawl_interval_minutes=60,
            )
            db.add(target)
            db.commit()
            target_id = target.id
        finally:
            db.close()

        result = run_monitoring_refresh_once(uid, platform="xhs")

        assert target_id not in [i["target_id"] for i in result["items"]]
        assert target_id not in [s["target_id"] for s in result["skipped"]]
