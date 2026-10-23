"""出图失败的**错误文案**：把上游的机器话翻成人话（v2.28）。

真机背景（2026-10-03）：千问账户免费额度耗尽时，上游返回

    HTTP 403
    {"error": {"message": "Free quota exhausted. To continue accessing the model
                on a paid basis, please add funds or disable the \"use free tier
                only\" mode in the management console.",
               "code": "insufficient_quota"}}

而 SDK 的 `HTTPError` 文案是 `403 Client Error: Forbidden for url: …`。
用户在页面上看到的就是**一串 URL** —— 既不知道原因（额度没了）、也不知道该做什么
（去充值 / 关「仅用免费额度」）。这组用例钉住「必须给可自救的提示」。

⚠️ 只给**已知且用户能自救**的情况译文；其余**原样透传**（不臆造原因，
同「缺值不造假」口径）。
"""

import json

import pytest
from unittest.mock import patch

from backend.app.services.ai_service import OpenAICompatibleImageClient



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract


def _client() -> OpenAICompatibleImageClient:
    return OpenAICompatibleImageClient()


class _Resp:
    """最小 requests.Response 替身。"""

    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}
        # ⚠️ **必须有 `.content`（bytes）**：实现走 `_load_json_response` →
        # `json.loads(response.content.decode(...))`，只有 `.json()` 的替身会让
        # 解析恒失败、`detail` 恒为空 —— 那样测的就是「替身」而不是实现
        # （本轮实测：少了它，两条用例都以为翻译没生效）。
        self.content = json.dumps(self._payload).encode("utf-8")
        self.encoding = "utf-8"
        self.headers = {"content-type": "application/json; charset=utf-8"}
        # ⚠️ `_candidate_response_encodings` 还会读 `apparent_encoding`
        # （真实 requests.Response 由 chardet 算出来）；替身漏了它 ⇒
        # `except Exception: pass` 把 AttributeError 静默吞掉、`detail` 恒为空。
        self.apparent_encoding = "utf-8"

    def raise_for_status(self):
        import requests

        err = requests.HTTPError(f"{self.status_code} Client Error: Forbidden for url: http://x/y")
        err.response = self
        raise err

    def json(self):
        return self._payload


class _ModelConfig:
    """出图客户端要的**最小**模型上下文（真实类型是 llm_gateway 的解析结果）。"""

    base_url = "https://example.invalid/v1"
    model_name = "m"
    provider_key = "TEST-XYZ"


def _run_with(resp: _Resp) -> str:
    """跑一次出图，返回错误文案（不该抛别的东西）。"""
    client = _client()
    with patch("backend.app.services.ai_service.requests.post", return_value=resp):
        with pytest.raises(ValueError) as exc:
            client.generate_image(
                model_config=_ModelConfig(),
                api_key="sk-test",
                prompt="p",
            )
    return str(exc.value)


def test_quota_exhausted_is_translated_to_actionable_chinese():
    """额度耗尽 ⇒ 提示里给出**原因 + 自救动作**（充值 / 关免费额度模式）。"""
    msg = _run_with(
        _Resp(
            403,
            {
                "error": {
                    "message": "Free quota exhausted. To continue accessing the model on a "
                    "paid basis, please add funds or disable the \"use free tier only\" mode.",
                    "code": "insufficient_quota",
                }
            },
        )
    )
    assert "额度" in msg, f"额度不足没被翻译：{msg}"
    assert "充值" in msg or "add funds" in msg, f"没给出自救动作：{msg}"
    assert "insufficient_quota" in msg, f"应带上服务商错误码（便于对账）：{msg}"


def test_forbidden_without_detail_gives_generic_actionable_hint():
    """403 但拿不到 detail ⇒ 给**通用可自查**提示（不空回一串 URL）。"""
    msg = _run_with(_Resp(403, {}))
    assert "403" in msg
    assert "服务商" in msg, f"应指向去哪自查：{msg}"
    assert "url:" not in msg, f"不该把原始 URL 当提示抛给用户：{msg}"


def test_unknown_error_is_passed_through_verbatim():
    """**不认识**的错误原样透传（不臆造原因）——同「缺值不造假」口径。"""
    msg = _run_with(_Resp(500, {"error": {"message": "internal boom"}}))
    assert "internal boom" in msg, f"未知错误应原样透传：{msg}"
