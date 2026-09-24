"""Regression check for ISS-002: PC QR scan never credited.

Before the fix the web backend created the QR in one request and polled it
in another, rebuilding XHSLoginApi from cookies only.  ``gid`` is issued by
the webprofile request and never travels in a Cookie header, so the poll's
PcDeviceProfile had fingerprint_ready=False, resolved MNS tier 0101 instead
of 0301, and presented a different device identity than the one that created
the code -- the platform then never credited the scan (codeStatus stuck 0).

This asserts the two invariants the fix restores:

1. state_snapshot()/restore_state() round-trip the device identity exactly.
2. A reconstructed poll-side profile resolves the same tier as create-side.

No real scan is required; the drift was fully reproducible offline.
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, '.')

from apis.xhs_pc_login_apis import XHSLoginApi


def _sign_context(api_obj: XHSLoginApi, cookies: dict, api: str) -> dict:
    profile = api_obj._ensure_profile(cookies)
    ctx = profile.next_sign_context(api)
    return {
        'tier': ctx['tier'],
        'envConst': ctx['envConst'],
        'envFpTail': list(ctx['envFpTail']),
        'fingerprint_ready': profile.session.fingerprint_ready,
        'last_tiga': profile.session.last_tiga_update_time,
        'loadts': ctx['loadts'],
        'webBuild': ctx['webBuild'],
    }


def _snapshot_cookie_keys(api_obj: XHSLoginApi, url: str, cookies: dict):
    return sorted(api_obj._cookies_for_url(url, cookies).keys())


def main() -> int:
    print('=== build a real session and QR ===', flush=True)
    create_api = XHSLoginApi()
    cookies = create_api.generate_init_cookies()
    success, message, payload = create_api.generate_qrcode(cookies)
    assert success, f'generate_qrcode failed: {message}'
    stored_cookies = payload['cookies']
    state = create_api.state_snapshot()
    print(f'state_snapshot keys: {sorted(state)}')

    api_path = '/api/qrcode/userinfo'
    base_url = create_api.base_url

    # --- Invariant 1: round-trip is lossless -------------------------------
    host_state = create_api.host_cookie_state()
    assert host_state.get('values'), 'create-side host cookie store is empty'
    restored_api = XHSLoginApi()
    restored_api.restore_state(state, host_cookie_state=host_state)
    # restore must take effect before the profile is created, so seed it the
    # same way the backend endpoint does (via _ensure_profile on first use).
    before = _sign_context(create_api, stored_cookies, api_path)
    after = _sign_context(restored_api, stored_cookies, api_path)
    print()
    print('=== create-side vs restored-side signing context ===')
    for key in before:
        same = before[key] == after[key]
        print(f'  {"OK " if same else "DIFF"} {key}: {before[key]!r} -> {after[key]!r}')
    assert before['tier'] == after['tier'], 'tier drifted'
    assert before['fingerprint_ready'] == after['fingerprint_ready']
    assert before['envConst'] == after['envConst']
    assert before['envFpTail'] == after['envFpTail']
    assert after['tier'] == '0301', f"expected 0301 after restore, got {after['tier']}"

    # --- Invariant 2: without restore, the drift comes back ---------------
    naive_api = XHSLoginApi()
    naive_ctx = _sign_context(naive_api, stored_cookies, api_path)
    print()
    print(f'naive rebuild tier (no restore): {naive_ctx["tier"]} '
          f'fingerprint_ready={naive_ctx["fingerprint_ready"]}')
    assert naive_ctx['tier'] == '0101', 'pre-fix drift is gone; test is stale'
    assert naive_ctx['fingerprint_ready'] is False
    print('confirmed: un-restored rebuild still drifts to 0101 (baseline)')

    # --- Cookie header parity ---------------------------------------------
    # acw_tc is host-scoped by design (Chrome would not send an edith-issued
    # cookie to another host).  It is therefore not in the shared dict and
    # must be round-tripped separately through host_cookie_state, or the poll
    # sends a smaller Cookie header than the create request did.
    create_keys = _snapshot_cookie_keys(create_api, base_url, stored_cookies)
    restored_keys = _snapshot_cookie_keys(restored_api, base_url, stored_cookies)
    assert create_keys == restored_keys, (create_keys, restored_keys)

    # --- Persistence must survive the DB temp-state format ----------------
    # Mirrors backend _dump_temp_state -> _load_temp_state without crypto.
    dump = json.dumps({'cookies': stored_cookies, 'device_state': state})
    reloaded = json.loads(dump)
    reloaded_api = XHSLoginApi()
    reloaded_api.restore_state(reloaded.get('device_state') or {})
    reloaded_ctx = _sign_context(reloaded_api, stored_cookies, api_path)
    assert reloaded_ctx['tier'] == '0301', reloaded_ctx['tier']

    # --- empty/legacy state must not break -------------------------------
    legacy_api = XHSLoginApi()
    legacy_api.restore_state(None)
    legacy_api.restore_state({})
    legacy_api2 = XHSLoginApi()
    legacy_api2.restore_state(None)
    assert legacy_api2.profile is None
    print('restore_state(None)/({}) handled without error')

    print()
    print('ALL ASSERTIONS PASSED')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
