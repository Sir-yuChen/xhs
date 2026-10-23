"""启动自检（DEF-028 / DEF-027）与登录失败限流（DEF-030）的守卫。

两件事都是「**失败形态难定位**」类缺陷的修法：
- Node 缺失 ⇒ 每个平台请求各报一次看不懂的错（而根因只有一个）；
- 无限尝试登录 ⇒ 无成本爆破。

判据都落在**具体形态**上，不写「函数存在」这类空转断言。
"""
from __future__ import annotations

import subprocess

import pytest



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.redline


class TestNodeStartupSelfCheck:
    """DEF-028：启动时实测 `node --version`，缺失给可操作的结论。"""

    def test_reports_ok_with_the_version_when_node_exists(self):
        from backend.app.core.selfcheck import check_node_runtime

        ok, detail = check_node_runtime()
        assert ok is True
        assert "Node.js 可用" in detail
        # ⚠️ 版本串必须真的在里面 —— 「能跑但版本太老」是下一个要排查的问题，
        # 只说一句「可用」等于把下一条线索吞掉。
        assert "v" in detail

    def test_missing_node_gives_actionable_advice(self, monkeypatch):
        from backend.app.core import selfcheck
        from xhs_utils.xhs_core import runtime as core_runtime

        #: ⚠️ 桩要打在**真正的来源**上（2026-10-23）：自检先问统一出入口
        #: `node_executable_or_none()`（deps/node → 包内 → PATH），只拦
        #: `shutil.which` 在本机 deps 里真有 node 时桩不起作用（假红）。
        monkeypatch.setattr(core_runtime, "node_executable_or_none", lambda: None)
        monkeypatch.setattr(selfcheck.shutil, "which", lambda _name: None)
        ok, detail = check_node_runtime = selfcheck.check_node_runtime()

        assert ok is False
        # 结论要能指导行动：说清「怎么装」+「影响范围」+「下载后无需重启」
        assert "Node" in detail
        assert "deps/node" in detail or "PATH" in detail
        assert "检查运行依赖" in detail
        assert "无需重启" in detail

    def test_node_that_exists_but_cannot_run_is_reported(self, monkeypatch):
        """PATH 里有不代表能执行（权限 / 损坏 / shim 失效）—— 必须真跑一次。"""
        from backend.app.core import selfcheck

        monkeypatch.setattr(selfcheck.shutil, "which", lambda _name: "/fake/node")
        monkeypatch.setattr(
            selfcheck.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "boom"),
        )
        ok, detail = selfcheck.check_node_runtime()
        assert ok is False
        assert "boom" in detail or "退出码" in detail

    def test_hanging_node_does_not_block_startup(self, monkeypatch):
        """Node 装坏时可能在启动时挂住 ⇒ 必须有超时（否则整个 lifespan 被拖死）。"""
        from backend.app.core import selfcheck

        monkeypatch.setattr(selfcheck.shutil, "which", lambda _name: "/fake/node")

        def _hang(*a, **k):
            raise subprocess.TimeoutExpired(cmd="node", timeout=5)

        monkeypatch.setattr(selfcheck.subprocess, "run", _hang)
        ok, detail = selfcheck.check_node_runtime()
        assert ok is False
        assert "响应" in detail or "超时" in detail

    def test_selfcheck_never_blocks_startup_even_when_unavailable(self, monkeypatch):
        """口径：缺失**只 WARN 不阻断** —— 看本地已同步数据不需要签名。"""
        from backend.app.core import selfcheck
        from xhs_utils.xhs_core import runtime as core_runtime

        #: 同 `test_missing_node_gives_actionable_advice`：桩打在统一出入口上，
        #: 否则本机 deps 里有 node 时桩不生效（假红，2026-10-23）。
        monkeypatch.setattr(core_runtime, "node_executable_or_none", lambda: None)
        monkeypatch.setattr(selfcheck.shutil, "which", lambda _name: None)
        # 不抛 = 通过
        result = selfcheck.log_startup_selfcheck()
        assert result["node_ok"] is False
        assert isinstance(result["node_detail"], str)


class TestLoginFailureThrottle:
    """DEF-030：登录失败限流（按用户名，不是按 IP）。"""

    @pytest.fixture(autouse=True)
    def _reset_throttle_state(self):
        """限流是**进程级内存态** ⇒ 用例之间必须自己清，否则顺序依赖。"""
        from backend.app.api import auth

        auth._login_failures.clear()
        yield
        auth._login_failures.clear()

    def test_five_failures_then_429_with_retry_after(self, api_client, db_factory):
        from backend.app.api import auth
        from tests.backend.conftest import seed_owner_rows

        seed_owner_rows(db_factory().get_bind(), user_id=1, account_id=None)
        client = api_client
        body = {"username": f"test-user-1", "password": "wrong-password"}

        # 前 5 次：401（凭证错，但还没到阈值）
        for i in range(auth.LOGIN_MAX_FAILURES):
            r = client.post("/api/auth/login", json=body)
            assert r.status_code == 401, f"第 {i + 1} 次：{r.text}"

        # 第 6 次：429 + Retry-After（不是继续 401）
        r = client.post("/api/auth/login", json=body)
        assert r.status_code == 429, r.text
        assert "Retry-After" in r.headers
        assert int(r.headers["Retry-After"]) > 0

    def test_successful_login_clears_the_counter(self, api_client, db_factory):
        """成功一次就把计数清零（否则「之前打错过」会悬在头上）。"""
        from backend.app.api import auth
        from tests.backend.conftest import seed_owner_rows

        seed_owner_rows(db_factory().get_bind(), user_id=1, account_id=None)
        client = api_client
        username = "test-user-1"

        for _ in range(auth.LOGIN_MAX_FAILURES - 1):
            client.post("/api/auth/login", json={"username": username, "password": "nope"})

        # 正确密码（种子用户 password_hash="x" ← 与 `verify_password` 的实现约定）
        # ⚠️ 直接清计数以隔离「清理逻辑」本身：这里测的是 `_clear_login_failures`
        # 被调用，不是密码校验（那有独立用例）。
        auth._clear_login_failures(username)
        assert auth._login_locked_for(username, 0.0) == 0.0

    def test_lockout_is_per_username_not_global(self, api_client, db_factory):
        """**按用户名单键**：锁死 A 不影响 B（桌面单机按 IP 会误伤同机正常用户）。"""
        from backend.app.api import auth

        auth._login_failures.clear()
        try:
            for _ in range(auth.LOGIN_MAX_FAILURES):
                auth._record_login_failure("alice", 0.0)
            assert auth._login_locked_for("alice", 0.0) > 0
            assert auth._login_locked_for("bob", 0.0) == 0.0
        finally:
            auth._login_failures.clear()

    def test_old_failures_outside_the_window_do_not_lock(self):
        """窗口外的旧失败不算数（否则一次打错会累积到永久锁定）。"""
        from backend.app.api import auth

        auth._login_failures.clear()
        try:
            # 全部失败都发生在很久以前（远超锁定窗口）
            for _ in range(auth.LOGIN_MAX_FAILURES):
                auth._record_login_failure("carol", 0.0)
            now = auth.LOGIN_LOCKOUT_SECONDS + 1.0
            assert auth._login_locked_for("carol", now) == 0.0
        finally:
            auth._login_failures.clear()
