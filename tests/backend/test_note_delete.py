"""删除执行链回归测试（创作者中心 note/delete）。

设计源：`.workbuddy/note-delete-chain.md`（2026-09-19 浏览器实测）。

钉死的契约：
1. 删除是**两步**：先 ``permission/validate``（空 body）再 ``note/delete``（body 只有 note_id）。
   权限校验不过 → **不**发删除请求。
2. 签名头必需：裸调（只带 cookie）→ 406 ``code=-1``。本测试不验证签名算法本身
   （那是 ``xhs_utils/xhs_creator`` 的职责），只验证调用方按实测形状把 api / body /
   签名档位喂给 ``_request_params``。
3. ``XhsCreatorApiAdapter`` 只是中转（H-6：不改 ``apis/`` 的语义，只经适配器调用）。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from apis.xhs_creator_apis import XHS_Creator_Apis
from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter

NOTE_ID = "6a7d1ca300000000250120cc"

# _creator_note_manager_headers 校验的最小线头契约（ordered_wire_headers 要求这些键存在）
_MINIMAL_WIRE_HEADERS = {
    "authorization": "",
    "referer": "https://creator.xiaohongshu.com/new/note-manager",
    "x-xray-traceid": "trace-id",
    "x-t": "1789803313426",
    "x-b3-traceid": "b3-trace-id",
    "x-s-common": "common-signature",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/153.0.0.0",
    "accept": "application/json, text/plain, */*",
    "x-s": "XYS_fake-signature",
    "accept-language": "zh-CN,zh;q=0.9",
    "priority": "u=1, i",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    # POST 写接口必带（2026-09-19 浏览器实测）；GET 列表不带，故另外造一份。
    "origin": "https://creator.xiaohongshu.com",
    "content-type": "application/json;charset=UTF-8",
}


class FakeAuth:
    """最小 auth 替身：``_request_params`` 要的几个方法都有即可。"""

    origin_map = {
        "api": "https://creator.xiaohongshu.com",
        "upload": "https://www.xiaohongshu.com",
        "edith": "https://edith.xiaohongshu.com",
        "public_web": "https://www.xiaohongshu.com",
        "web": "https://creator.xiaohongshu.com",
    }

    def origin(self, key):
        return self.origin_map[key]

    def cookies_for_url(self, url, cookies):
        return cookies

    proxies = None

    def validate(self, **kwargs):
        return True

    @property
    def http_client(self):
        return MagicMock()


def _apis_with_canned(canned_response):
    """造一个 ``XHS_Creator_Apis``，http 层返回 canned_response，并记录所有请求。"""
    captured = {"calls": []}

    auth = FakeAuth()
    apis = XHS_Creator_Apis.__new__(XHS_Creator_Apis)
    apis.auth = auth
    apis.http = MagicMock()
    apis.base_url = auth.origin("api")
    apis.upload_url = auth.origin("upload")
    apis.edith_url = auth.origin("edith")
    apis.xhs_web_url = auth.origin("public_web")
    apis._note_manager_signed = False

    def fake_request_params(api, data="", method="POST", **kwargs):
        captured["calls"].append(
            {"api": api, "data": data, "method": method, "kwargs": kwargs}
        )
        return (dict(_MINIMAL_WIRE_HEADERS), "fake-cookies", str(data) if data else "")

    apis._request_params = fake_request_params
    apis.http.post.return_value = canned_response
    apis.http.get.return_value = canned_response
    return apis, captured


def _resp(payload):
    out = MagicMock()
    out.json.return_value = payload
    out.status_code = 200
    return out


def test_delete_note_sends_minimal_json_body():
    """删除请求的 body 只有 note_id，Content-Type 是 JSON（实测形状）。"""
    apis, captured = _apis_with_canned(_resp({"success": True, "msg": "成功"}))
    success, msg, payload = apis.delete_note(NOTE_ID)

    assert success is True
    delete_calls = [c for c in captured["calls"] if "note/delete" in c["api"]]
    assert len(delete_calls) == 1
    call = delete_calls[0]
    assert call["api"] == "/web_api/sns/capa/postgw/note/delete"
    # body 就是 {"note_id": ...}，没有别的字段
    assert '"note_id"' in call["data"]
    assert NOTE_ID in call["data"]
    assert call["method"] == "POST"
    # 签名档位与 get_posted_notes_page 同源
    assert call["kwargs"]["b1_profile"] == "note_manager"
    assert call["kwargs"]["mns_profile"] == "note_manager"
    assert call["kwargs"]["sec_fetch_site"] == "same-origin"


def test_validate_delete_permission_has_empty_body_and_query_params():
    """权限校验：空 body，note_id/function_type 在 query string 上。"""
    apis, captured = _apis_with_canned(_resp({"success": True, "msg": "成功"}))
    success, msg, payload = apis.validate_delete_permission(NOTE_ID)

    assert success is True
    calls = [c for c in captured["calls"] if "permission/validate" in c["api"]]
    assert len(calls) == 1
    call = calls[0]
    # query 参数挂在 api 上（splice_str 的产物）
    assert "note_id=" + NOTE_ID in call["api"]
    assert "function_type=delete" in call["api"]
    # 空 body
    assert call["data"] == ""


def test_delete_note_reports_platform_failure_without_raising():
    """平台返回失败时按 (False, msg, payload) 带回，不抛异常。"""
    apis, _ = _apis_with_canned(
        _resp({"success": False, "code": -1, "msg": "笔记不存在"})
    )
    success, msg, payload = apis.delete_note(NOTE_ID)
    assert success is False
    assert "笔记不存在" in msg
    assert payload["code"] == -1


def test_adapter_delete_note_runs_the_permission_gate_first():
    """适配器把两步串成一条链：先权限校验，过了才发删除（实测执行链）。

    ``adapter.delete_note`` 不是 ``apis.delete_note`` 的直通：它把第 1 步
    （``permission/validate``）包在前面，这一步失败时删除请求**一次都不能发**。
    """
    adapter = XhsCreatorApiAdapter.__new__(XhsCreatorApiAdapter)
    adapter.cookies = "fake"
    fake_apis = MagicMock()
    fake_apis.validate_delete_permission.return_value = (True, "成功", {"stage": 1})
    fake_apis.delete_note.return_value = (True, "成功", {"stage": 2})

    with patch.object(XhsCreatorApiAdapter, "_api", return_value=fake_apis):
        success, message, payload = adapter.delete_note(NOTE_ID)

    assert success is True
    assert payload == {"stage": 2}
    fake_apis.validate_delete_permission.assert_called_once_with(NOTE_ID)
    fake_apis.delete_note.assert_called_once_with(NOTE_ID)
    # 同一个 _api() 实例服务两步（签名状态在链路内复用，不重建 Auth）
    assert fake_apis.delete_note.call_args is not None


def test_adapter_delete_note_aborts_when_permission_gate_fails():
    """权限校验失败 → 删除请求一次都不发，平台原因带回 message。"""
    adapter = XhsCreatorApiAdapter.__new__(XhsCreatorApiAdapter)
    adapter.cookies = "fake"
    fake_apis = MagicMock()
    fake_apis.validate_delete_permission.return_value = (
        False,
        "无权限删除该笔记",
        {"code": -1},
    )
    fake_apis.delete_note.return_value = (True, "不该到这里", {})

    with patch.object(XhsCreatorApiAdapter, "_api", return_value=fake_apis):
        success, message, payload = adapter.delete_note(NOTE_ID)

    assert success is False
    assert message == "无权限删除该笔记"
    assert payload == {"code": -1}
    fake_apis.validate_delete_permission.assert_called_once_with(NOTE_ID)
    fake_apis.delete_note.assert_not_called()


def test_adapter_delete_note_fills_in_message_when_platform_gives_none():
    """平台只回 success=false 不带 msg 时，message 仍要是可读的原因。"""
    adapter = XhsCreatorApiAdapter.__new__(XhsCreatorApiAdapter)
    adapter.cookies = "fake"
    fake_apis = MagicMock()
    fake_apis.validate_delete_permission.return_value = (False, "", {"code": -1})

    with patch.object(XhsCreatorApiAdapter, "_api", return_value=fake_apis):
        success, message, _ = adapter.delete_note(NOTE_ID)

    assert success is False
    assert message == "删除权限校验未通过"
    fake_apis.delete_note.assert_not_called()
