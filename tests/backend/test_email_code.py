"""邮箱验证码服务的守卫（v1.1.0 / S3，用户「注册改为邮箱验证与 dataPanel 一致」）。

## 这里守的是「验证码有没有真的起作用」

| 守卫 | 若失效的后果 |
|---|---|
| `test_code_is_six_digits_and_random` | 码可预测 ⇒ 任意人猜到就能注册 |
| `test_cooldown_blocks_resend` | 无冷却 ⇒ 被当短信/邮件轰炸器用 |
| `test_attempt_limit_burns_the_code` | 无上限 ⇒ 可以暴力枚举 10^6 |
| `test_expired_code_is_rejected` | 码永不过期 ⇒ 泄露的码永久可用 |
| `test_code_is_single_use` | 码可复用 ⇒ 一个码注册多个账号 |
| `test_purpose_isolation` | 注册码能用来改资料 ⇒ 越权 |
| `test_send_returns_false_when_smtp_missing` | 未配 SMTP 抛异常 ⇒ 首装就注册不了 |
| `test_email_normalisation_is_case_insensitive` | `A@x.com` 与 `a@x.com` 被当成两个账号 |

⚠️ 本文件的用例**必须自己 `reset_for_tests()`** —— 验证码状态是进程级全局，
不清理会串到下一个用例（实测：`test_email_code` 与 E2E 互相污染过一次）。
"""

import pytest

pytestmark = pytest.mark.contract

from backend.app.services import email_service


@pytest.fixture(autouse=True)
def _clean_state():
    email_service.reset_for_tests()
    yield
    email_service.reset_for_tests()


def test_code_is_six_digits_and_random():
    codes = {email_service.generate_code() for _ in range(200)}
    assert all(len(c) == 6 and c.isdigit() for c in codes)
    #: 200 次里不该全是同一个（若实现写死，这里会红）
    assert len(codes) > 150, "验证码重复率过高 —— 随机源可能没用对"
    #: 首位允许 0（用 randrange 而不是 randint(100000, 999999)）
    lead_zeros = [c for c in codes if c.startswith("0")]
    assert lead_zeros, "从未生成过 0 开头的码 ⇒ 码空间少了 10%（用了 randint 而不是 randrange）"


def test_cooldown_blocks_resend():
    email_service.issue_code("register", "a@example.com")
    ok, wait = email_service.can_send("register", "a@example.com")
    assert ok is False
    assert 0 < wait <= email_service.RESEND_COOLDOWN_SECONDS


def test_cooldown_is_per_purpose_and_per_email():
    email_service.issue_code("register", "a@example.com")
    # 另一个邮箱不受影响
    assert email_service.can_send("register", "b@example.com")[0] is True
    # 另一个用途不受影响
    assert email_service.can_send("profile", "a@example.com")[0] is True


def test_attempt_limit_burns_the_code():
    code = email_service.issue_code("register", "a@example.com")
    for _ in range(email_service.MAX_ATTEMPTS):
        ok, reason = email_service.verify_code("register", "a@example.com", "000000")
        assert ok is False
        assert reason == "CODE_INVALID"
    #: 超限后即使输入**正确**的码也不行（码已作废）
    ok, reason = email_service.verify_code("register", "a@example.com", code)
    assert ok is False
    assert reason == "CODE_TOO_MANY_ATTEMPTS"


def test_expired_code_is_rejected():
    code = email_service.issue_code("register", "a@example.com")
    #: 直接把过期时间推到过去（不睡 10 分钟）
    with email_service._LOCK:
        email_service._STATE.entries[("register", "a@example.com")].expires_at = 0
    ok, reason = email_service.verify_code("register", "a@example.com", code)
    assert ok is False
    assert reason == "CODE_EXPIRED"


def test_code_is_single_use():
    code = email_service.issue_code("register", "a@example.com")
    assert email_service.verify_code("register", "a@example.com", code)[0] is True
    #: 同一个码第二次必须失败（否则一个码能注册多个账号）
    ok, reason = email_service.verify_code("register", "a@example.com", code)
    assert ok is False
    assert reason == "CODE_EXPIRED"


def test_purpose_isolation():
    code = email_service.issue_code("register", "a@example.com")
    #: 注册码不能用来改资料
    ok, _ = email_service.verify_code("profile", "a@example.com", code)
    assert ok is False
    #: 反方向也不行
    profile_code = email_service.issue_code("profile", "b@example.com")
    ok, _ = email_service.verify_code("register", "b@example.com", profile_code)
    assert ok is False


def test_email_normalisation_is_case_insensitive():
    code = email_service.issue_code("register", "Mixed@Example.COM")
    #: 大小写不同仍是同一个验证码
    assert email_service.verify_code("register", "mixed@example.com", code)[0] is True


def test_invalid_email_rejected():
    assert email_service.is_valid_email("a@b.co")
    assert not email_service.is_valid_email("no-at-sign")
    assert not email_service.is_valid_email("a@b")
    assert not email_service.is_valid_email("")


def test_send_returns_false_when_smtp_missing(monkeypatch):
    """未配 SMTP ⇒ **返回 False 而不是抛异常**（首装/本地开发要能跑通）。

    这条对应 dataPanel 的 `邮箱未配置…（仅日志）` 行为。
    """
    from backend.app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "email_host", "", raising=False)
    monkeypatch.setattr(settings, "email_port", 0, raising=False)

    assert email_service.send_code_email("a@example.com", "123456", "register") is False


def test_send_raises_when_smtp_configured_but_fails(monkeypatch):
    """配了 SMTP 但连不上 ⇒ **抛 RuntimeError**（不能回「发送成功」让用户白等）。"""
    from backend.app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "email_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(settings, "email_port", 9, raising=False)  # discard 端口，必然连不上
    monkeypatch.setattr(settings, "email_username", "u", raising=False)
    monkeypatch.setattr(settings, "email_password", "p", raising=False)

    with pytest.raises(RuntimeError):
        email_service.send_code_email("a@example.com", "123456", "register")


def test_clear_removes_cooldown_too():
    """`clear()` 要连冷却一起清 —— 否则注册成功后立刻想改邮箱会被冷却挡住。"""
    email_service.issue_code("register", "a@example.com")
    email_service.clear("register", "a@example.com")
    assert email_service.can_send("register", "a@example.com")[0] is True
