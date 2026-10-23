# encoding: utf-8
"""Node runtime adapter for the XHS PC JavaScript algorithms.

Shared cryptographic/signature algorithms live under
:mod:`xhs_utils.xhs_core.js`（唯一来源）；本目录仅保留 PC 特有模板
（profile/rap 等）。This module only validates Python inputs, invokes Node,
and validates outputs; it intentionally does not maintain a second Python
implementation.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from typing import Any, Mapping, Optional

from xhs_utils.xhs_core.runtime import node_exe as _node_exe


_JS_DIR = os.path.join(os.path.dirname(__file__), 'js')
_CORE_JS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'xhs_core', 'js',
)
_B1_JS = os.path.join(_CORE_JS_DIR, 'b1.js')
_SIGN_JS = os.path.join(_CORE_JS_DIR, 'sign.js')
_WEBSECTIGA_CLI = os.path.join(_CORE_JS_DIR, 'websectiga_cli.js')
_PROFILE_JS = os.path.join(_JS_DIR, 'profile.js')

_TIER_GATE = {
    #: ⚠️⚠️ **判据必须是区间，不能是等值**（2026-10-23 实机报障，用户口径
    #: 「换台电脑就报 JS mnsv2 签名失败或未达到硬门禁（档位 mns0201_，无短签/静态兜底）」
    #: 「重新打开软件依旧」）：
    #:
    #: `x3` 的长度**随 a1 长度线性浮动**，实测（同一份 sign.js、同一段 cookie 结构）：
    #:
    #: | a1 长度 | 0101 | 0201 | 0301 |
    #: |---|---|---|---|
    #: | 10 | 148 | 152 | 144 |
    #: | 20 | 161 | 164 | 160 |
    #: | 30 | 175 | 180 | 172 |
    #: | 52 | 205 | 208 | 200 |
    #:
    #: 原来这里写死 `205 / 208 / 200` —— **那是 a1 恰好 52 字符时的长度**。
    #: 于是本机（a1 是 52 位）一直正常，换台电脑新生成的 a1 长度不同 ⇒ 门禁恒假
    #: ⇒ `run_signer` 返回 None ⇒ `params.py` 抛「签名失败或未达到硬门禁」。
    #: 与「重开软件」无关（比的是常量），也与 node 在不在 PATH 无关。
    #:
    #: 换成区间 + 前缀：真约束是「是 mns 前缀」和「长度落在实测跨度内」，不是某一串定值。
    '0101': ('mns0101_', (140, 260)),
    '0201': ('mns0201_', (140, 260)),
    '0301': ('mns0301_', (130, 260)),
}
_WEBSECTIGA_RE = re.compile(r'^[0-9a-f]{64}$', re.I)
_PROFILE_DATA_RE = re.compile(r'^[0-9a-f]{10000,20000}$', re.I)

#: 最近一次门禁失败的**真实原因**（线程局部：签名会并发跑，全局变量会互相覆盖）。
#: 只用于把原因拼进上层错误信息，不参与任何业务判断。
_gate_failure = threading.local()


def _record_gate_failure(detail: str) -> None:
    _gate_failure.detail = detail


def last_gate_failure() -> str:
    """最近一次 `run_signer` 失败的原因（无则空串）。"""
    return str(getattr(_gate_failure, 'detail', '') or '')


def generate_b1(
    options: Optional[Mapping[str, Any]] = None,
    timeout: float = 10.0,
) -> str:
    """Generate b1 from an explicit 19-field browser-state snapshot."""
    if not os.path.isfile(_B1_JS):
        raise RuntimeError(f'b1 algorithm missing: {_B1_JS}')
    payload = json.dumps(dict(options or {}), ensure_ascii=False, separators=(',', ':'))
    try:
        proc = subprocess.run(
            [_node_exe(), _B1_JS, '--generate', payload],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=_JS_DIR,
            encoding='utf-8',
            errors='replace',
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'b1 runtime failed to start: {exc}') from exc
    if proc.returncode != 0:
        raise RuntimeError(f'b1 runtime failed: {(proc.stderr or proc.stdout).strip()}')
    try:
        value = str(json.loads(proc.stdout.strip())['b1'])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f'b1 runtime output invalid: {proc.stdout[:300]}') from exc
    if len(value) < 500 or len(value) % 4:
        raise RuntimeError(f'b1 length invalid: {len(value)}')
    return value


def run_signer(
    api: str,
    data: Any = '',
    *,
    a1: str,
    cookie: str,
    b1: Optional[str],
    dsl_pair: str,
    tier: str,
    sign_context: Mapping[str, Any],
    timeout: float = 30.0,
) -> Optional[dict]:
    """Run the JS signing core and enforce the selected MNS tier gate."""
    if not os.path.isfile(_SIGN_JS) or not cookie or 'a1=' not in cookie:
        return None
    if tier not in _TIER_GATE:
        return None
    context = dict(sign_context or {})
    required = ('loadts', 'seq', 'envConst', 'envFpTail', 'webBuild')
    if not all(key in context for key in required):
        return None

    payload = {
        'api': api,
        'data': data,
        'cookie': cookie,
        'a1': a1,
        # Empty b1 is the browser's real cold-login state. ``None`` remains
        # reserved for callers that failed to provide the field explicitly.
        'b1': '' if b1 is None else str(b1),
        'dslPair': dsl_pair,
        'tier': tier,
    }
    for key in (
        'now', 'version', 'loadts', 'seq', 'envConst', 'envFpTail', 'webBuild',
        'signVersion', 'appId', 'platform',
    ):
        if key in context:
            payload[key] = context[key]

    try:
        proc = subprocess.run(
            [_node_exe(), _SIGN_JS, json.dumps(payload, ensure_ascii=False)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=_JS_DIR,
            encoding='utf-8',
            errors='replace',
        )
    except FileNotFoundError:
        #: ⚠️ 「找不到 node」与「门禁不过」原来共用同一句上层报错（2026-10-23 实机排查
        #: 花了几轮才分清）。这里保留现场：FileNotFoundError = PATH 里没有 node（装包版
        #: 未下载 / 下载后未刷新 PATH）；TimeoutExpired = node 卡死。
        _record_gate_failure(
            '找不到可执行的 node（PATH 里没有 node.exe）：1.0.6 起 node 需在'
            '「检查运行依赖」里下载，或见 deps/node/'
        )
        return None
    except subprocess.TimeoutExpired:
        _record_gate_failure(f'node 签名超时（>{timeout}s）')
        return None
    except OSError as exc:
        _record_gate_failure(f'node 启动失败：{exc}')
        return None
    if proc.returncode != 0:
        _record_gate_failure(
            f'node 退出码 {proc.returncode}：'
            f'{(proc.stderr or proc.stdout or "").strip()[:300]}'
        )
        return None

    required_prefix, allowed_lengths = _TIER_GATE[tier]
    for line in reversed([
        item.strip() for item in (proc.stdout or '').splitlines() if item.strip()
    ]):
        if not line.startswith('{') or '"x3"' not in line:
            continue
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        x3 = str(result.get('x3') or '')
        if (
            result.get('xs')
            and x3.startswith(required_prefix)
            and allowed_lengths[0] <= len(x3) <= allowed_lengths[1]
        ):
            result['len'] = len(x3)
            result['prefix'] = x3[:12]
            return result
        #: ⚠️ 把**真实原因**带出去（2026-10-23）：原来一律 return None，上层只能报
        #: 「签名失败或未达到硬门禁」，看不出是「前缀不对」「长度越界」还是「没有 xs」，
        #: 现场排查全靠猜。这里写进 last_gate_failure 供上层拼进错误信息。
        _record_gate_failure(
            f'tier={tier} prefix={x3[:12] or "<空>"} len={len(x3)} '
            f'要求={required_prefix!r}+{allowed_lengths[0]}~{allowed_lengths[1]} '
            f'xs={"有" if result.get("xs") else "无"}'
        )
        return None
    _record_gate_failure(
        f'tier={tier} 输出里没有含 x3 的 JSON 行（stdout 前 200 字：'
        f'{(proc.stdout or "")[:200]!r} stderr：{(proc.stderr or "")[:200]!r}）'
    )
    return None


def generate_websectiga(
    scripting_code: str,
    *,
    profile: Optional[Mapping[str, Any]] = None,
    timeout: float = 20.0,
) -> str:
    """Execute a server-issued scripting program and return websectiga."""
    code = str(scripting_code or '')
    if len(code) < 1000:
        raise ValueError('scripting code is empty or truncated')
    if not os.path.isfile(_WEBSECTIGA_CLI):
        raise RuntimeError(f'websectiga runtime missing: {_WEBSECTIGA_CLI}')

    values = dict(profile or {})
    payload = {
        'code': code,
        'userAgent': values.get('userAgent', ''),
        'platform': values.get('platform', 'Win32'),
        'pageUrl': values.get(
            'pageUrl',
            'https://www.xiaohongshu.com/explore?channel_id=homefeed_recommend',
        ),
        'timeoutMs': int(max(1000, timeout * 1000 * 0.75)),
    }
    try:
        proc = subprocess.run(
            [_node_exe(), _WEBSECTIGA_CLI],
            input=json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=_JS_DIR,
            encoding='utf-8',
            errors='replace',
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'websectiga runtime failed to start: {exc}') from exc
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or '').strip()
        raise RuntimeError(f'websectiga runtime failed: {message[:500]}')

    for line in (proc.stdout or '').splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        token = str(result.get('websectiga') or '')
        if _WEBSECTIGA_RE.fullmatch(token):
            return token
    raise RuntimeError(f'websectiga runtime output invalid: {(proc.stdout or "")[:300]}')


def generate_profile_data(
    *,
    fields: Optional[Mapping[str, Any]] = None,
    timestamp_ms: Optional[int] = None,
    ets: Optional[int] = None,
    document_cookie: Optional[str] = None,
    time_origin: Optional[float] = None,
    i12_seed: Optional[int] = None,
    telemetry_fi: Optional[int] = None,
    timeout: float = 10.0,
) -> str:
    """Pure-calculate PC ``profileData`` from explicit fingerprint fields.

    Node only performs deterministic field assembly, UTF-8/Base64 encoding,
    DES-ECB zero-padding encryption and hexadecimal serialization.  It does not
    construct browser objects or execute the original fingerprint SDK.
    """
    if not os.path.isfile(_PROFILE_JS):
        raise RuntimeError(f'profileData algorithm missing: {_PROFILE_JS}')
    payload: dict[str, Any] = {'fields': dict(fields or {})}
    if timestamp_ms is not None:
        payload['timestampMs'] = int(timestamp_ms)
    if ets is not None:
        payload['ets'] = int(ets)
    if document_cookie is not None:
        payload['documentCookie'] = str(document_cookie)
    if time_origin is not None:
        payload['timeOrigin'] = float(time_origin)
    if i12_seed is not None:
        payload['i12Seed'] = int(i12_seed)
    if telemetry_fi is not None:
        payload['telemetryFi'] = int(telemetry_fi)
    try:
        proc = subprocess.run(
            [
                _node_exe(),
                _PROFILE_JS,
                json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=_JS_DIR,
            encoding='utf-8',
            errors='replace',
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'profileData runtime failed to start: {exc}') from exc
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or '').strip()
        raise RuntimeError(f'profileData runtime failed: {message[:500]}')

    for line in reversed((proc.stdout or '').splitlines()):
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        value = str(result.get('profileData') or '')
        if (
            result.get('sdkVersion') == '4.3.7'
            and int(result.get('length') or 0) == len(value)
            and result.get('algorithm') == 'base64-des-ecb-zero-padding-hex'
            and _PROFILE_DATA_RE.fullmatch(value)
        ):
            return value
    raise RuntimeError(f'profileData runtime output invalid: {(proc.stdout or "")[:300]}')


__all__ = [
    'generate_b1',
    'run_signer',
    'generate_websectiga',
    'generate_profile_data',
]
