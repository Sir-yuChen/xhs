"""Frontend guards for the QR login panel's **session identity**.

Two user-visible defects share one root cause: the QR on screen and the session
being polled were allowed to be different sessions.

* DEF-036 — ticking the "sync Creator" checkbox re-minted the session, so the
  QR the user had already scanned was silently invalidated.
* DEF-038 — two sessions per drawer open (React StrictMode invokes effects
  twice) plus an unconditional write of a *stale* poll response produced the
  worst variant: `session_id` flipped back to the previous session while
  `qr_image_data_url` kept the new QR image.  The screen showed QR-B, the poller
  watched session-A, and the phone's confirmation was credited to a session
  nobody was watching — the page sat still forever.

Live evidence for DEF-038 (18 PC sessions in ``login_sessions``): a poll injects
the webprofile ``gid`` cookie on its first call, so ``gid`` present == the
session was polled at least once.  Eight had been polled, ten had not: roughly
half of every StrictMode pair was minted and then abandoned.

These are source-level guards.  There is no frontend test runner in this
project, and the failure mode is a *race* that no unit test of the reducer
would catch — pinning the invariants in the source is the tripwire.
"""

from __future__ import annotations

import re
from pathlib import Path

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
_PANEL = _FRONTEND / "components" / "account" / "qr-login-panel.tsx"
_ACCOUNTS_PAGE = _FRONTEND / "pages" / "platforms" / "xhs" / "accounts-page.tsx"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _squash(value: str) -> str:
    """Collapse whitespace so the guards do not depend on formatting."""
    return re.sub(r"\s+", " ", value)


def _code_only(value: str) -> str:
    """剥掉块注释 / 行注释，只留代码。

    「某个符号不许再出现」这类守卫必须查**代码**：注释里说明「它已被移除」
    正是好文档该做的事，不能把它算成违规（第一版就被自己的注释绊倒）。
    """
    without_block = re.sub(r"/\*.*?\*/", " ", value, flags=re.S)
    without_line = re.sub(r"//[^\n]*", " ", without_block)
    return _squash(without_line)


# --- session identity -------------------------------------------------------


def test_mount_effect_cannot_mint_two_sessions_for_one_open():
    """StrictMode invokes effects twice; only one session may be created."""
    source = _squash(_read(_PANEL))
    assert "initialisedForRef.current === accountType" in source, (
        "the mount effect lost its StrictMode gate, so opening the drawer once "
        "mints two login sessions again"
    )


def test_superseded_session_creation_never_becomes_the_displayed_qr():
    source = _squash(_read(_PANEL))
    assert "generation !== createGenerationRef.current" in source, (
        "a superseded /pc/qrcode response may again be adopted as the QR on "
        "screen, which is how the displayed code stopped being the polled one"
    )


def test_poll_response_must_belong_to_the_displayed_session():
    """A response for an older session must be dropped, never merged."""
    source = _squash(_read(_PANEL))
    assert "polled.session_id !== activeSessionIdRef.current" in source, (
        "poll responses are applied unconditionally again; a late response for "
        "a previous session will hijack the panel"
    )


def test_state_never_merges_two_different_sessions():
    """`session_id` and `qr_image_data_url` must always come from one session.

    This is the exact regression: keeping the *current* image while adopting an
    older response's `session_id` puts QR-B on screen while polling session-A.
    """
    source = _squash(_read(_PANEL))
    assert "current && current.session_id === sessionId" in source, (
        "the poll setState no longer checks that it is updating the same "
        "session it polled, so the QR image and session id can diverge"
    )
    assert "? { ...polled, qr_image_data_url:" in source


def test_checkbox_still_cannot_recreate_the_session():
    """DEF-036 guard: the mount effect must stay keyed on `accountType` only.

    ⚠️ **2026-09-23 前提变化，断言改写（原文保留）**：用户口径「移除登录 PC 后同步
    Creator 账号勾选」（设计 13 §18.15.2）—— 勾选**整条移除**后，`syncCreatorRef`
    不再存在，原断言 `"syncCreatorRef.current" in source` 失去守护对象。
    改写为两条更强的不变量：

    1. effect 仍只依赖 `[accountType]`（原断言保留 —— 防未来加回任何偏好时
       又把会话创建键上去）；
    2. 面板里**不允许再出现** `syncCreator` / `sync_creator` 任何形态 ——
       把「偏好不得影响会话创建」从「实现了但要小心」升级为「偏好不存在」。
    """
    source = _read(_PANEL)
    assert re.search(r"\}, \[accountType\]\);", source), (
        "the session-creation effect gained a dependency again; toggling a "
        "preference must never mint a new QR"
    )
    squashed = _code_only(source)
    assert "syncCreator" not in squashed, (
        "the sync-Creator preference is back in the QR panel; it was removed "
        "outright on 2026-09-23 (§18.15.2) — one login updates only its own platform"
    )
    assert "sync_creator" not in squashed


# --- account card identity --------------------------------------------------


def test_account_card_shows_red_id_for_every_account_type():
    """PC cards used to omit 小红书号 entirely — only Creator cards had it."""
    source = _squash(_read(_ACCOUNTS_PAGE))
    assert "小红书号" in source
    assert 'profileValue(account, "red_id") || "-"' in source, (
        "小红书号 must render for both PC and Creator, with an explicit '-' "
        "fallback (a hidden field reads as a bug)"
    )


def test_account_card_does_not_hide_red_id_when_missing():
    """The old shape hid the whole cell when the platform returned nothing."""
    source = _squash(_read(_ACCOUNTS_PAGE))
    assert 'profileValue(account, "red_id") ? (' not in source, (
        "小红书号 is conditionally hidden again; show '-' instead"
    )
