"""Harness: replay the PC QR confirm flow against a stubbed XHS upstream.

The API layer talks to XHS through ``XHSLoginApi``.  The harness stubs the
HTTP client and the wire-header builder so the whole state machine can be
exercised offline, including the ``status == 2`` (confirmed) branch that the
reported bug lives in.

Usage:  py -3.12 tests/backend/replay_pc_qr_confirm.py
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # repo root
sys.path.insert(0, str(ROOT))


def log(what: str) -> None:
    print(f"[replay] {what}", flush=True)


# --------------------------------------------------------------------------
# Shared upstream stub: a single state machine for both the adapter and API
# --------------------------------------------------------------------------
class StubUpstream:
    """In-memory stand-in for the two XHS qrcode endpoints."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = "pending"  # pending -> scanned -> confirmed
        self.formal_session = "FORMAL_WEB_SESSION_abc"
        self.visitor_session = "VISITOR_WEB_SESSION_xyz"
        self.calls: list[dict] = []

    def advance(self) -> None:
        with self.lock:
            if self.state == "pending":
                self.state = "scanned"
            elif self.state == "scanned":
                self.state = "confirmed"

    def _cookies(self, session: str) -> dict:
        return {"web_session": session, "a1": "a" * 40, "websectiga": "b" * 64}

    def userinfo(self) -> dict:
        return {
            "success": True,
            "data": {
                "user_id": "user-42",
                "nickname": "replay-tester",
                "red_id": "replay-001",
                "guest": False,
                "images": "https://example/avatar.png",
            },
        }

    def handle(self, method: str, url: str, data=None):
        body = {}
        if data:
            try:
                body = json.loads(data) if isinstance(data, (bytes, str)) else dict(data)
            except Exception:
                body = {}

        with self.lock:
            state = self.state
            self.calls.append(
                {"method": method, "url": url, "state_at_call": state, "body": body}
            )

        # Real calls carry a query string; match on the path only.
        path = url.split("?", 1)[0]

        if path.endswith("/api/qrcode/userinfo"):
            # The phone is modelled by explicit ``advance()`` calls from the
            # harness, so that "polled but not yet scanned" is observable.
            code_status = {"pending": 0, "scanned": 1, "confirmed": 2}.get(state, 0)
            resp = {"success": True, "data": {"codeStatus": code_status}}
            if code_status == 2:
                # The real endpoint hands out the formal session cookie here.
                resp["data"]["web_session"] = self.formal_session
            return resp, self._cookies(self.formal_session if code_status == 2 else self.visitor_session)

        if path.endswith("/api/sns/web/v1/login/qrcode/status"):
            if state != "confirmed":
                return {"success": False, "msg": "not confirmed yet"}, {}
            return {
                "success": True,
                "data": {
                    "code_status": 2,
                    "login_info": {"session": self.formal_session},
                },
            }, self._cookies(self.formal_session)

        if path.endswith("/api/sns/web/v2/user/me"):
            return self.userinfo(), self._cookies(self.formal_session)

        return {"success": True, "data": {}}, {}


# --------------------------------------------------------------------------
# curl_cffi response shape
# --------------------------------------------------------------------------
class StubResponse:
    def __init__(self, payload, headers=None):
        self.status_code = 200
        self.ok = True
        self._payload = payload
        self.headers = headers or {}
        self.cookies = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None

    @property
    def text(self):
        return json.dumps(self._payload, ensure_ascii=False)


def install_upstream_stub(_target, upstream: StubUpstream):
    """Replace the HTTP client and the wire-header builder.

    ``XhsPcLoginAdapter`` builds a fresh ``XHSLoginApi`` per call, so the HTTP
    client must be patched on the *class* rather than on one instance.
    """
    import apis.xhs_pc_login_apis as login_api

    def fake_post(self, url, *, headers=None, data=None, **kwargs):
        payload, cookies = upstream.handle("POST", url, data)
        response = StubResponse(payload)
        response.cookies = cookies
        return response

    def fake_get(self, url, *, headers=None, params=None, **kwargs):
        payload, cookies = upstream.handle("GET", url, None)
        response = StubResponse(payload)
        response.cookies = cookies
        return response

    http_cls = type(login_api.XHSLoginApi().http)
    http_cls.post = fake_post
    http_cls.get = fake_get

    def pass_through(headers, cookies=None, **kwargs):
        merged = dict(headers)
        if cookies:
            merged["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        return merged

    login_api.build_pc_login_headers = pass_through

    # ``_signed_request_params`` lazily pulls the ``ds`` value over the wire;
    # offline there is nothing to fetch, so pin it to a fixed 13-digit value.
    login_api.get_dsl = lambda **kwargs: "1758000000000"

    # Signing lives behind a JS runtime with a hard gate.  This harness replays
    # the *state machine*, not the signer, so return a pass-through triple.
    login_api.generate_request_params = (
        lambda *args, **kwargs: ({"x-s": "stub", "x-t": "stub"}, None, args[2])
    )


def make_api_with_session(upstream: StubUpstream):
    """Build an XHSLoginApi with a pre-generated anonymous session."""
    from apis.xhs_pc_login_apis import XHSLoginApi

    api = XHSLoginApi()
    install_upstream_stub(api, upstream)

    cookies = {
        "a1": "a" * 40,
        "web_id": "w" * 24,
        "abRequestId": "ab" * 16,
        "websectiga": "b" * 64,
        "sec_poison_id": "c" * 36,
        "gid": "d" * 72,
        "web_session": upstream.visitor_session,
        "xsecappid": "xhs-pc-web",
        "webBuild": "1.29.1",
        "loadts": "1758000000000",
    }
    return api, cookies


# --------------------------------------------------------------------------
# Layer 1: apis/ level (the shared code path)
# --------------------------------------------------------------------------
def replay_apis_layer(upstream: StubUpstream, events: list[str]):
    log("layer 1: apis/xhs_pc_login_apis.py check_qrcode_status full cycle")
    from apis.xhs_pc_login_apis import XHSLoginApi

    api, cookies = make_api_with_session(upstream)

    # pending
    success, msg, cookies = api.check_qrcode_status("qr-1", "code-1", cookies)
    assert not success and msg == "请扫描二维码", (success, msg)
    events.append(f"apis pending -> success={success} msg={msg!r}")

    # scanned
    upstream.advance()
    success, msg, cookies = api.check_qrcode_status("qr-1", "code-1", cookies)
    assert not success and msg == "请确认登录", (success, msg)
    events.append(f"apis scanned -> success={success} msg={msg!r}")

    # confirmed: this is the branch the reported bug lives in
    upstream.advance()
    try:
        success, msg, cookies = api.check_qrcode_status("qr-1", "code-1", cookies)
        events.append(f"apis confirmed -> success={success} msg={msg!r} session={cookies.get('web_session')!r}")
        if not success:
            events.append("FAIL apis layer returned success=False on confirmed state")
            return False
        if cookies.get("web_session") != upstream.formal_session:
            events.append(
                f"FAIL apis layer kept the wrong session: {cookies.get('web_session')!r}"
            )
            return False
    except Exception as exc:  # noqa: BLE001
        events.append(f"FAIL apis layer raised on confirmed: {type(exc).__name__}: {exc}")
        return False
    return True


# --------------------------------------------------------------------------
# Layer 2: adapter level
# --------------------------------------------------------------------------
def replay_adapter_layer(upstream: StubUpstream, events: list[str]):
    log("layer 2: backend adapter XhsPcLoginAdapter.check_qrcode_status")
    from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter

    adapter = XhsPcLoginAdapter()
    cookies = {
        "a1": "a" * 40,
        "websectiga": "b" * 64,
        "web_session": upstream.visitor_session,
        "gid": "d" * 72,
    }
    install_upstream_stub(_make_blank_api(), upstream)

    seen = []
    for i in range(3):
        if i:
            upstream.advance()  # the phone scans, then confirms
        result = adapter.check_qrcode_status("qr-1", "code-1", cookies)
        seen.append(result["status"])
        cookies = result["cookies"]
        events.append(f"adapter -> status={result['status']} session={cookies.get('web_session')!r}")

    if seen[-1] != "confirmed":
        events.append(f"FAIL adapter never reached confirmed, saw {seen}")
        return False
    if cookies.get("web_session") != upstream.formal_session:
        events.append(
            f"FAIL adapter persisted wrong session {cookies.get('web_session')!r}"
        )
        return False
    return True


def _make_blank_api():
    from apis.xhs_pc_login_apis import XHSLoginApi

    api = XHSLoginApi()
    install_upstream_stub(api, _upstream_ref[0])
    return api


_upstream_ref: list[StubUpstream] = []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-port", type=int, default=8000)
    args = parser.parse_args()

    upstream = StubUpstream()
    _upstream_ref.append(upstream)

    events: list[str] = []
    ok = True

    ok &= replay_apis_layer(upstream, events)
    upstream.state = "pending"
    ok &= replay_adapter_layer(upstream, events)

    print()
    print("=" * 70)
    for line in events:
        print(line)
    print("=" * 70)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
