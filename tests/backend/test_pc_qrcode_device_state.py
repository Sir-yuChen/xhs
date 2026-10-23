"""ISS-002 / DEF-025 回归：PC 扫码的设备身份必须能跨请求往返。

## 为什么这个文件要重写（2026-10-23）

原文件是 `main()` + `if __name__ == "__main__"` 形态，**pytest 一条都收集不到**
⇒ 全量回归里它从未执行过。而它守护的正是「扫码确认后前端无反应」（ISS-002 /
DEF-025，文档里至今标「未确认根因」），属于**写了但没跑**的死测试。

重写同时修掉第二个问题：原版通过 `generate_init_cookies()` /
`generate_qrcode()` 取 cookies，这两个方法**会发真实网络请求**
（`_initialize_navigation` 与 `qrcode/create` POST）。本文件要验的两个不变量
（state 往返无损、restore 后档位一致）**本来就是纯本地可复现的**
（原 docstring 原话：「No real scan is required; the drift was fully reproducible offline」）
⇒ 改为直接构造 cookies，零网络依赖，符合 `tests/backend/conftest.py` 的禁网口径。

## 被守护的不变量

修复前：web 后端在**一个请求里建码、另一个请求里轮询**，轮询侧只用 cookies 重建
`XHSLoginApi`。而 `gid` 由 webprofile 请求下发、**从不放在 Cookie 头里** ⇒
轮询侧的 `PcDeviceProfile` 指纹未就绪，MNS 档位解析成 `0101` 而不是 `0301`，
设备身份与建码时不同 ⇒ 平台不认账（`codeStatus` 永远 0）。

本文件断言修复恢复的两条：
1. `state_snapshot()` / `restore_state()` 往返设备身份**无损**；
2. 重建的轮询侧 profile 解析出的档位与建码侧**一致**。
"""

from __future__ import annotations

import json

import pytest

from apis.xhs_pc_login_apis import XHSLoginApi

#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
pytestmark = pytest.mark.redline


#: 一份自洽的 PC 侧 cookie 集合（足够让 profile 建起来并解析档位）。
#: ⚠️ 不调 `generate_init_cookies()`：那个方法会真实访问小红书做导航初始化。
FIXTURE_COOKIES = {
    "a1": "18f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4e5f6a7b8c9d0e1f2",
    "webId": "0123456789abcdef0123456789abcdef",
    "web_session": "03003d1b2c3a4f5e6d7c8b9a0f1e2d3c4b5a6978",
    "acw_tc": "0a1b2c3d4e5f60718293a4b5c6d7e8f9",
    "abRequestId": "11111111-2222-3333-4444-555555555555",
}


def _sign_context(api_obj: XHSLoginApi, cookies: dict, api: str) -> dict:
    """取一次签名上下文（供两侧对比）。"""
    profile = api_obj._ensure_profile(cookies)
    ctx = profile.next_sign_context(api)
    return {
        "tier": ctx["tier"],
        "envConst": ctx["envConst"],
        "envFpTail": list(ctx["envFpTail"]),
        "fingerprint_ready": profile.session.fingerprint_ready,
        "loadts": ctx["loadts"],
        "webBuild": ctx["webBuild"],
    }


API_PATH = "/api/qrcode/userinfo"


def test_state_round_trip_keeps_device_identity_and_tier():
    """不变量 1：`state_snapshot()` → `restore_state()` 往返后设备身份与档位不变。

    这是 ISS-002 的核心：只要往返无损，轮询侧就不会退回 `0101`。
    """
    create_api = XHSLoginApi()
    create_api.profile = create_api._new_profile(dict(FIXTURE_COOKIES))
    #: 模拟真实时序：建码前 webprofile 已上报过一次（实现里是
    #: `xhs_pc_login_apis.py:517` 的 `mark_profile_reported()`）。只有上报过，
    #: `fingerprint_ready` 才为 True，档位才解析得到 `0301`。
    create_api.profile.mark_profile_reported()
    before = _sign_context(create_api, FIXTURE_COOKIES, API_PATH)
    state = create_api.state_snapshot()

    assert before["fingerprint_ready"] is True, (
        "建码侧 profile 指纹未就绪 —— 夹具 cookies 不足以建出有效身份："
        f"{before}"
    )
    assert before["tier"] == "0301", f"建码侧档位应为 0301，实际 {before['tier']}"

    restored_api = XHSLoginApi()
    restored_api.restore_state(state)
    after = _sign_context(restored_api, FIXTURE_COOKIES, API_PATH)

    assert after["tier"] == before["tier"], (
        f"restore 后档位漂移：{before['tier']} → {after['tier']}"
        "（这正是 ISS-002 的症状：轮询侧退回 0101，平台不认账）"
    )
    assert after["fingerprint_ready"] == before["fingerprint_ready"]
    assert after["envConst"] == before["envConst"], "envConst 漂移 ⇒ 设备身份变了"
    assert after["envFpTail"] == before["envFpTail"], "envFpTail 漂移 ⇒ 指纹尾部变了"
    assert after["webBuild"] == before["webBuild"]


def test_restored_state_survives_json_round_trip():
    """不变量 1 的持久化形态：状态要能过一遍 JSON（后端暂存就是存 JSON）。"""
    create_api = XHSLoginApi()
    create_api.profile = create_api._new_profile(dict(FIXTURE_COOKIES))
    create_api.profile.mark_profile_reported()
    original_tier = _sign_context(create_api, FIXTURE_COOKIES, API_PATH)["tier"]
    state = create_api.state_snapshot()

    reloaded = json.loads(json.dumps({"cookies": FIXTURE_COOKIES, "device_state": state}))
    api = XHSLoginApi()
    api.restore_state(reloaded.get("device_state") or {})
    ctx = _sign_context(api, FIXTURE_COOKIES, API_PATH)

    assert ctx["tier"] == original_tier, (
        "JSON 往返后档位漂移 —— 后端 `_dump_temp_state` / `_load_temp_state` "
        "走的就是这条路，漂移等于线上必然出错"
    )
    assert ctx["fingerprint_ready"] is True


def test_naive_rebuild_drifts_which_is_why_restore_is_required():
    """反向判据：**不** restore 而只拿 cookies 重建，档位会退化。

    这条是「修复仍然必要」的证明：若哪天这个退化自己消失了，说明
    `_new_profile` 的档位判据变了，本文件与实现要一起复查
    （否则 `restore_state` 成了摆设而没人知道）。
    """
    naive_api = XHSLoginApi()
    naive_ctx = _sign_context(naive_api, dict(FIXTURE_COOKIES), API_PATH)

    assert naive_ctx["fingerprint_ready"] is False, (
        "裸重建竟然也有完整指纹 —— 说明档位判据已变，`restore_state` 的必要性需重新论证"
    )
    assert naive_ctx["tier"] == "0101", (
        f"裸重建档位应为 0101（退化态），实际 {naive_ctx['tier']}；"
        "若已变成 0301，本测试的前提失效，请复查实现后再改断言"
    )


def test_restore_accepts_empty_and_none_state():
    """缺值口径：`None` / `{}` 不得抛异常（历史行里 device_state 可能是空的）。"""
    for empty in (None, {}):
        api = XHSLoginApi()
        api.restore_state(empty)
        assert api.profile is None, f"restore_state({empty!r}) 不该产生 profile"
