"""Regression tests for the Creator login 406 fix (2026-09-18).

Root cause had two parts, both asserted here:

1. ``webBuild`` was pinned to a build the platform had long retired
   (``1.18.0`` while production served ``1.29.1``).  It enters the Cookie
   header *and* the MNS signing material, so a stale value made every
   signed Creator request fail server-side validation.
2. The customer-domain CAS login POSTs (qr-code / verify-code) used the
   browser-native mns0101/a1 tier, which the current platform rejects for
   scripted clients.  They now use the mns0201/nop cold path, matching the
   zones/service-ticket requests that already did so.

The tier assertions intercept ``_signed`` before the Node signer runs and
stub the wire-header builder, so they stay fast and offline.
"""

import json
from pathlib import Path

from apis.xhs_creator_login_apis import CREATOR_WEB_BUILD, XHSCreatorLoginApi
from xhs_utils.xhs_creator import state as creator_state

_REFERENCE_FILE = (
    Path(creator_state.__file__).parent / 'js' / 'reference_profile.json'
)

_SECURITY_COOKIES = {
    'a1': 'a' * 40,
    'websectiga': 'b' * 64,
    'sec_poison_id': 'c' * 36,
    'gid': 'd' * 72,
}


class _StubResponse:
    """Minimal stand-in for a curl_cffi Response."""

    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


def _load_reference():
    return json.loads(_REFERENCE_FILE.read_text(encoding='utf-8'))


def _spy_on_signed(api, monkeypatch):
    """Record the signing tier without invoking the Node signer."""
    seen = []

    def fake_signed(api_path, data='', method='POST', **kwargs):
        seen.append(
            {
                'api': api_path,
                'tier': kwargs.get('tier'),
                'b1_value': kwargs.get('b1_value'),
                'dsl_pair_value': kwargs.get('dsl_pair_value'),
            }
        )
        # Return the shapes callers use: headers, cookie_map, body. The header
        # builder is stubbed too, so no real wire contract is enforced here.
        return {'x-s': 's', 'x-t': '1', 'x-s-common': 'c'}, dict(
            _SECURITY_COOKIES
        ), json.dumps(data, ensure_ascii=False)

    monkeypatch.setattr(api, '_signed', fake_signed)
    return seen


def _stub_wire_headers(monkeypatch):
    """Bypass the observed-order contract; it is validated elsewhere."""
    import apis.xhs_creator_login_apis as login_api

    def pass_through(headers, cookies=None, **kwargs):
        merged = dict(headers)
        if cookies:
            merged['cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items())
        return merged

    monkeypatch.setattr(login_api, 'build_creator_login_headers', pass_through)


def test_creator_web_build_is_not_the_retired_value():
    # 1.18.0 was the value shipped before the fix; production serves 1.29.1.
    assert CREATOR_WEB_BUILD != '1.18.0'
    assert CREATOR_WEB_BUILD.count('.') == 2


def test_reference_profile_release_matches_module_constant():
    # The signing material (the mns pack reads release.webBuild) and the
    # Cookie must agree — updating only the Cookie is not enough, both were
    # required to make the server accept requests again.
    release = _load_reference()['release']
    assert release['webBuild'] == CREATOR_WEB_BUILD, (
        'reference_profile.webBuild must equal CREATOR_WEB_BUILD: '
        f"{release['webBuild']!r} != {CREATOR_WEB_BUILD!r}"
    )


def test_generate_qrcode_uses_mns0201_cold_path(monkeypatch):
    """The customer CAS qr-code POST must sign with the 0201/nop tier."""
    api = XHSCreatorLoginApi()
    seen = _spy_on_signed(api, monkeypatch)
    _stub_wire_headers(monkeypatch)
    monkeypatch.setattr(api, '_require_security_cookies', lambda *a, **k: None)
    monkeypatch.setattr(
        api.http,
        'post',
        lambda *a, **k: _StubResponse(
            {'success': True, 'data': {'id': 'qr-1', 'url': 'https://example/qr'}}
        ),
    )
    monkeypatch.setattr(api, '_merge_response_cookies', lambda *a, **k: {})

    success, message, payload = api.generate_qrcode(dict(_SECURITY_COOKIES))

    assert success, message
    assert payload['qr_id'] == 'qr-1'
    assert [call['tier'] for call in seen] == ['0201'], (
        'generate_qrcode must sign with the mns0201/nop tier; '
        f'observed {[call["tier"] for call in seen]}'
    )
    # The nop tier requires an empty b1 and the literal dsl pair.
    assert seen[0]['b1_value'] == ''
    assert seen[0]['dsl_pair_value'] == 'null;undefined'


def test_generate_qrcode_sends_cas_post_shape(monkeypatch):
    """The POST keeps the rate-limit header and the cas body."""
    api = XHSCreatorLoginApi()
    _spy_on_signed(api, monkeypatch)
    _stub_wire_headers(monkeypatch)
    monkeypatch.setattr(api, '_require_security_cookies', lambda *a, **k: None)

    sent = {}

    def capture_post(url, *, headers, data, **kwargs):
        sent['url'] = url
        sent['headers'] = headers
        sent['data'] = data
        return _StubResponse(
            {'success': True, 'data': {'id': 'qr-1', 'url': 'https://example/qr'}}
        )

    monkeypatch.setattr(api.http, 'post', capture_post)
    monkeypatch.setattr(api, '_merge_response_cookies', lambda *a, **k: {})

    success, _, _ = api.generate_qrcode(dict(_SECURITY_COOKIES))
    assert success

    assert sent['url'].endswith('/api/cas/customer/web/qr-code')
    assert sent['headers']['content-type'] == 'application/json'
    assert sent['headers']['x-ratelimit-meta'].startswith('host=')
    assert b'"service":' in sent['data']
    assert b'creator.xiaohongshu.com' in sent['data']


def test_send_phone_code_uses_mns0201_cold_path(monkeypatch):
    api = XHSCreatorLoginApi()
    seen = _spy_on_sorted = _spy_on_signed(api, monkeypatch)
    _stub_wire_headers(monkeypatch)
    monkeypatch.setattr(api, '_require_security_cookies', lambda *a, **k: None)
    monkeypatch.setattr(
        api.http,
        'post',
        lambda *a, **k: _StubResponse({'success': True, 'msg': '成功'}),
    )
    monkeypatch.setattr(api, '_merge_response_cookies', lambda *a, **k: {})

    success, message, _ = api.send_phone_code('13800138000', dict(_SECURITY_COOKIES))

    assert success, message
    assert [call['tier'] for call in seen] == ['0201'], (
        'send_phone_code must sign with the mns0201/nop tier; '
        f'observed {[call["tier"] for call in seen]}'
    )


def test_query_qrcode_status_keeps_browser_native_tier(monkeypatch):
    """The GET status probe was never broken and stays on 0101/a1."""
    api = XHSCreatorLoginApi()
    seen = _spy_on_signed(api, monkeypatch)
    _stub_wire_headers(monkeypatch)
    monkeypatch.setattr(
        api.http,
        'get',
        lambda *a, **k: _StubResponse(
            {
                'success': True,
                'data': {
                    'status': 1,
                    'ticket': '',
                    'type': '',
                    'avatar': '',
                },
            }
        ),
    )
    monkeypatch.setattr(api, '_merge_response_cookies', lambda *a, **k: {})

    detail = api.query_qrcode_status('qr-1', dict(_SECURITY_COOKIES))

    assert [call['tier'] for call in seen] == ['0101'], (
        'GET probe should keep 0101/a1; '
        f'observed {[call["tier"] for call in seen]}'
    )
    assert detail['success'] is True


def test_anonymous_requests_do_not_fail_the_header_contract():
    """Early anonymous requests have no Cookie yet; that must not be an error.

    Regression guard for the ``wire header contract drifted: missing=['cookie']``
    failure that made _fetch_honeypot unusable on a fresh instance. The other
    headers stay mandatory — only ``cookie`` is absent before the device
    session exists.
    """
    from xhs_utils.xhs_core.http import ordered_wire_headers

    order = (
        'user-agent', 'accept', 'content-type', 'accept-encoding',
        'accept-language', 'cookie', 'origin',
    )

    base_headers = {
        'user-agent': 'ua',
        'accept': 'a',
        'content-type': 'c',
        'accept-language': 'zh-CN,zh;q=0.9',
        'origin': 'o',
    }

    # No cookies supplied -> no cookie header, contract still satisfied.
    headers = ordered_wire_headers(dict(base_headers), order=order, cookies=None)
    assert 'cookie' not in headers
    assert headers['user-agent'] == 'ua'

    # Cookies supplied -> cookie header present and ordered.
    headers = ordered_wire_headers(
        dict(base_headers), order=order, cookies={'a1': 'x'}
    )
    assert headers['cookie'] == 'a1=x'

    # A genuinely missing required header still fails loudly, and the
    # missing report must not be about the legitimately-absent cookie.
    try:
        ordered_wire_headers(
            {'user-agent': 'ua'}, order=order, cookies={'a1': 'x'}
        )
    except RuntimeError as error:
        assert 'missing' in str(error)
        assert 'cookie' not in str(error)
    else:  # pragma: no cover - the contract must be enforced
        raise AssertionError('missing required header did not raise')

    # A fresh device session with an empty cookie map is the real-world case:
    # the request must be allowed through with no cookie header at all.
    headers = ordered_wire_headers(dict(base_headers), order=order, cookies={})
    assert 'cookie' not in headers
