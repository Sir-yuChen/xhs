"""邮箱验证码服务 —— SMTP 发信 + 验证码存储/校验（设计 55 §1.2）。

## 口径对齐 dataPanel（用户「注册改为邮箱验证与 dataPanel 一致」）

| 项 | 口径 |
|---|---|
| 验证码 | **6 位数字**，有效期 **10 分钟** |
| 冷却 | 同一收件人 **60 秒**内不可重发 |
| 尝试上限 | 单条验证码 **5 次**校验失败即作废 |
| 未配置 SMTP | **降级为只写日志**（`WARNING` 级别打印收件人与码）—— 对齐 dataPanel 的 `邮箱未配置…（仅日志）`，让本地/首装能跑通 |
| 用途隔离 | 注册 / 登录 / 改资料**各自独立的码**（同一个邮箱的注册码不能用来改资料） |

## 为什么验证码存内存而不是建表

* 它是**短生命周期**数据（10 分钟），建表要配清理任务，还要处理「进程重启后孤儿码」；
* 重启丢码的后果只是「重新点一次发送」，代价可接受；
* 多副本部署时内存码不共享 —— 这一条已在 `docs/02_设计文档/52` §多副本里声明
  「当前为单副本部署」；将来上多副本需要改到 Redis/表，本模块是唯一改动点。

## ⚠️ 绝不把码写进日志以外的任何地方

线上的码只能走邮件正文。日志里的那条 `WARNING` 是**降级提示**，
生产环境未配置 SMTP 时应当直接从「发不出去」这件事上发现，而不是靠用户报障。
"""

from __future__ import annotations

import logging
import random
import re
import smtplib
import threading
import time
from dataclasses import dataclass, field
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Final, Literal

from backend.app.core.config import get_settings

logger = logging.getLogger(__name__)

#: 验证码用途（隔离不同场景的码，互不通用）
Purpose = Literal["register", "login", "profile"]

CODE_TTL_SECONDS: Final[int] = 10 * 60
RESEND_COOLDOWN_SECONDS: Final[int] = 60
MAX_ATTEMPTS: Final[int] = 5
_EMAIL_RE: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class _Entry:
    code: str
    purpose: str
    expires_at: float
    attempts: int = 0


@dataclass
class _State:
    #: `(purpose, email_lower)` → 当前有效条目
    entries: dict[tuple[str, str], _Entry] = field(default_factory=dict)
    #: `(purpose, email_lower)` → 上次发送时间戳（冷却用）
    last_sent: dict[tuple[str, str], float] = field(default_factory=dict)


_LOCK = threading.Lock()
_STATE = _State()


def normalize_email(value: str | None) -> str:
    """去空白 + 小写。**不做任何猜测**（不补全、不纠正拼写）。

    大小写归一很重要：`A@x.com` 与 `a@x.com` 是同一个收件箱，
    不归一的话「注册后登录」会因大小写不同而找不到账号。
    """
    return (value or "").strip().lower()


def is_valid_email(value: str | None) -> bool:
    return bool(_EMAIL_RE.match(normalize_email(value)))


def generate_code() -> str:
    """6 位数字（首位可以是 0 —— 用 `randrange` 而不是 `randint(100000, 999999)`，
    后者把 0 打头的码排除掉，等于少了 10% 的码空间）。"""
    return f"{random.randrange(0, 1_000_000):06d}"


def can_send(purpose: Purpose, email: str, *, now: float | None = None) -> tuple[bool, int]:
    """是否允许发送。返回 `(允许吗, 还需等待的秒数)`。"""
    key = (purpose, normalize_email(email))
    now = time.time() if now is None else now
    with _LOCK:
        last = _STATE.last_sent.get(key)
        if last is None:
            return True, 0
        remain = int(RESEND_COOLDOWN_SECONDS - (now - last))
        return (remain <= 0), max(0, remain)


def issue_code(purpose: Purpose, email: str) -> str:
    """生成并登记一个验证码（**不负责发送**）。

    调用方应先 `can_send()` 判冷却，再调本函数 + `send_code_email()`。
    分开的理由：冷却判断失败时不该消耗一个新码（否则用户点了没收到、
    重试又被冷却挡住，而旧码已被覆盖 ⇒ 彻底收不到）。
    """
    code = generate_code()
    key = (purpose, normalize_email(email))
    now = time.time()
    with _LOCK:
        _STATE.entries[key] = _Entry(code=code, purpose=purpose, expires_at=now + CODE_TTL_SECONDS)
        _STATE.last_sent[key] = now
    return code


def verify_code(purpose: Purpose, email: str, code: str) -> tuple[bool, str]:
    """校验。返回 `(通过吗, 失败原因)`。

    失败原因**不区分「无此码」与「码不对」**（都返回 `CODE_INVALID`），
    否则等于给攻击者一个「这个邮箱有没有在注册」的探针。
    """
    key = (purpose, normalize_email(email))
    now = time.time()
    with _LOCK:
        entry = _STATE.entries.get(key)
        if entry is None:
            return False, "CODE_EXPIRED"
        if entry.expires_at < now:
            _STATE.entries.pop(key, None)
            return False, "CODE_EXPIRED"
        entry.attempts += 1
        if entry.attempts > MAX_ATTEMPTS:
            #: 超限即作废 —— 不删的话攻击者可以一直试（虽然每次都算 attempts）。
            _STATE.entries.pop(key, None)
            return False, "CODE_TOO_MANY_ATTEMPTS"
        if entry.code != (code or "").strip():
            return False, "CODE_INVALID"
        #: 用过即焚：同一个码不能注册两个账号。
        _STATE.entries.pop(key, None)
        return True, ""


def clear(purpose: Purpose, email: str) -> None:
    """主动清掉（注册成功后调一次，避免码残留）。"""
    key = (purpose, normalize_email(email))
    with _LOCK:
        _STATE.entries.pop(key, None)
        _STATE.last_sent.pop(key, None)


def reset_for_tests() -> None:
    """清空所有状态（**仅供测试夹具**）。"""
    with _LOCK:
        _STATE.entries.clear()
        _STATE.last_sent.clear()


# ---------------------------------------------------------------------------
# 发信
# ---------------------------------------------------------------------------


def send_code_email(to: str, code: str, purpose: Purpose) -> bool:
    """发送验证码邮件。返回是否真的发出去了。

    * 未配置 SMTP ⇒ **降级为只写日志并返回 False**（不抛异常，注册流程继续 ——
      与 dataPanel 一致：本地开发不该被邮件配置挡住）；
    * 配置了但发送失败 ⇒ 抛 `RuntimeError`（让用户看到「发送失败」而不是
      「发送成功但收不到」）。
    """
    settings = get_settings()

    #: 配置是**扁平字段**（`Settings.email_host` 等，见 core/config.py）。
    #: ⚠️ 不要为了「好看」改成嵌套的 `email:` 子模型 —— 那会让
    #: `config/default.yaml` 与环境变量两套读取口径分叉（本项目已因
    #: 「同一件事两处实现」踩过多次，DEF-044 同型）。
    host = settings.email_host or ""
    port = int(settings.email_port or 0)
    username = settings.email_username or ""
    password = settings.email_password or ""
    sender = settings.email_from_addr or username
    use_ssl = bool(settings.email_use_ssl)

    if not host or not port:
        logger.warning(
            "未配置 SMTP（email.host/port 为空）⇒ 验证码不发送，仅记录日志：purpose=%s to=%s code=%s",
            purpose,
            to,
            code,
        )
        return False

    subject_map = {"register": "注册验证码", "login": "登录验证码", "profile": "修改资料验证码"}
    subject = subject_map.get(purpose, "验证码")
    body = (
        f'<div style="padding:20px;font-family:Arial,sans-serif">'
        f"<h2>{subject}</h2>"
        f"<p>您的邮箱验证码为：</p>"
        f'<p style="font-size:28px;font-weight:bold;letter-spacing:6px">{code}</p>'
        f"<p>有效期 {CODE_TTL_SECONDS // 60} 分钟，请勿泄露给他人。</p>"
        f"<p>若非本人操作，请忽略本邮件。</p>"
        f"</div>"
    )

    msg = MIMEMultipart()
    msg["From"] = formataddr((str(Header("Spider XHS", "utf-8")), sender or username))
    msg["To"] = to
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg.attach(MIMEText(body, "html", "utf-8"))

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
        try:
            server.ehlo()
            if not use_ssl:
                try:
                    server.starttls()
                    server.ehlo()
                except smtplib.SMTPException:
                    #: 有些内网 SMTP 不支持 STARTTLS —— 不当作失败。
                    pass
            if username:
                server.login(username, password)
            server.sendmail(sender or username, [to], msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 —— quit 失败不影响「已发出」的结论
                pass
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"验证码邮件发送失败：{exc}") from exc

    logger.info("验证码邮件已发送 purpose=%s to=%s", purpose, to)
    return True
