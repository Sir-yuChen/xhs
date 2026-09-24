"""Offline verification for the creator account check fix (task t_61c13d60).

Exercises the REAL adapter -> Auth -> XHS_Creator_Apis.get_fileIds chain with
only the curl_cffi network transport replaced by a canned response, so the
constructor wiring and get_fileIds argument alignment are validated against
production code instead of a mock of it.

Run with the project's own interpreter, from the repo root:

    python tests/backend/verify_creator_check_fix.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# a1 must have the length of a real browser-issued value: the mns0101 signer
# output length scales with it, so a 10-char placeholder trips the tier gate
# (x3 must be 196/197/198 chars -- see _TIER_GATE in xhs_creator/runtime.py).
A1 = "1a0b3559a300xpxexi8l5bp209ejqe9simol83d2m50000152319"
COOKIE_TEXT = f"a1={A1}; web_session=creator-session"
PERMIT_URL_TAIL = "/api/media/v1/upload/creator/permit"


class CannedResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self):
        return self._payload


class CannedTransport:
    """Drop-in for CreatorHttpClient: records calls, returns canned JSON."""

    calls = []

    def __init__(self, *args, **kwargs):
        CannedTransport.calls.append({"event": "init", "kwargs": kwargs})

    def get(self, url, **kwargs):
        CannedTransport.calls.append(
            {
                "event": "get",
                "url": url,
                "headers": dict(kwargs.get("headers") or {}),
                "cookies": kwargs.get("cookies"),
                "proxies": kwargs.get("proxies"),
                "media_type_param": _media_type_from_url(url),
            }
        )
        return CannedResponse({"success": True, "msg": "获取fileIds成功"})

    def post(self, url, **kwargs):
        CannedTransport.calls.append({"event": "post", "url": url})
        return CannedResponse({"success": True})

    def close(self):
        pass

    def state_snapshot(self):
        return {"backend": "canned"}


def _media_type_from_url(url: str):
    return "image" if "scene=image" in url else "video" if "scene=video" in url else None


def _patch_transport():
    import xhs_utils.xhs_creator.http as creator_http

    creator_http.CreatorHttpClient = CannedTransport
    # Auth module captured the class at import time.
    import xhs_utils.xhs_creator.auth as creator_auth

    creator_auth.CreatorHttpClient = CannedTransport


def main() -> int:
    _patch_transport()

    failures = []

    # --- 1. Old code path: XHS_Creator_Apis() without auth -> TypeError -----
    from apis.xhs_creator_apis import XHS_Creator_Apis

    try:
        XHS_Creator_Apis()
        failures.append("XHS_Creator_Apis() accepted a missing auth argument")
    except TypeError as exc:
        print(f"[1] reproduced old crash: TypeError: {exc}")
    except Exception as exc:  # pragma: no cover
        failures.append(f"unexpected exception type from XHS_Creator_Apis(): {type(exc).__name__}: {exc}")

    # --- 2. Fixed path: adapter builds Auth + Apis and calls get_fileIds ---
    from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter

    CannedTransport.calls = []
    adapter = XhsCreatorApiAdapter(COOKIE_TEXT)
    ok, msg = adapter.check_upload_permit("image")

    if ok is not True:
        failures.append(f"check_upload_permit returned ok={ok!r}, expected True")
    if "fileIds" not in msg and "成功" not in msg:
        failures.append(f"check_upload_permit returned unexpected message: {msg!r}")

    permit_calls = [call for call in CannedTransport.calls if call.get("event") == "get" and PERMIT_URL_TAIL in call.get("url", "")]
    if not permit_calls:
        failures.append("no request was issued to the creator permit endpoint")
    else:
        call = permit_calls[0]
        print(f"[2] permit request URL : {call['url']}")
        print(f"    cookies passed     : {type(call['cookies']).__name__} with keys {sorted(call['cookies'])}")
        print(f"    proxies position   : {call['proxies']!r}")
        if call["media_type_param"] != "image":
            failures.append(f"permit request carried media_type={call['media_type_param']!r}, expected 'image'")
        if isinstance(call["proxies"], dict):
            failures.append(
                "cookies leaked into the proxies position: get_fileIds received a "
                f"dict as its proxies argument ({call['proxies']!r})"
            )
        cookie_keys = set(call["cookies"] or {})
        if "a1" not in cookie_keys:
            failures.append(f"permit request cookie jar missing a1, got {sorted(cookie_keys)}")

    # --- 3. JSON-format stored cookie also works ----------------------------
    # QR/phone login stores a JSON object; the API layer normalizes it to a
    # Cookie header before constructing the adapter (accounts.py /
    # _cookies_to_string), so exercise that same conversion here.
    from backend.app.services.account_service import cookie_header_from_text

    CannedTransport.calls = []
    normalized = cookie_header_from_text(json.dumps({"a1": A1, "web_session": "creator-session"}))
    if "a1=" + A1 not in normalized:
        failures.append(f"cookie_header_from_text mangled the JSON cookie: {normalized!r}")
    ok, _ = XhsCreatorApiAdapter(normalized).check_upload_permit("image")
    if ok is not True:
        failures.append("JSON-format stored cookie failed through the adapter")
    else:
        print("[3] JSON-format stored cookie accepted after cookie_header_from_text")

    # --- 4. Unauthenticated cookie is still rejected -----------------------
    from xhs_utils.xhs_creator import XHSCreatorAuth

    try:
        XHSCreatorAuth.from_cookie(f"a1={A1}")
        failures.append("unauthenticated cookie was accepted by XHSCreatorAuth.from_cookie")
    except ValueError as exc:
        print(f"[4] unauthenticated cookie rejected: ValueError: {exc}")

    # --- 5. Module-level export still resolves ----------------------------
    from apis.xhs_creator_apis import XHSCreatorApis  # noqa: F401

    print("[5] XHSCreatorApis alias import OK")

    if failures:
        print("\nFAILURES:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
