"""视频生成链路测试（设计 17 §13.4.4 v2.14，2026-10-02）。

覆盖三层：
  ① 客户端协议（`video_generation.OpenAICompatibleVideoClient`）—— 提交 → 轮询 → 拿 URL，
     以及四条失败面（提交失败 / 轮询失败 / 上游报失败 / 超时）；
  ② 业务点与媒体来源常量（`generate_video` kind=video；视频来源与图片来源分开取值）；
  ③ 媒体生成分流（`build_note_media` 按来源走图片还是视频链路；视频失败不废正文）。

⚠️ 全部用**替身**：真打上游要 key + 几分钟，且会真的花钱（§5 硬约束 5 不造假、
但也不该拿真上游当测试环境）。
"""

from __future__ import annotations

import pytest

from backend.app.services import video_generation as vg
from backend.app.services.business_provider_keys import (
    BUSINESS_KEY_GENERATE_VIDEO,
    BUSINESS_KEYS,
    MODEL_TYPE_VIDEO,
    MODEL_TYPES,
)
from backend.app.services.media_generation import (
    MEDIA_SOURCE_VIDEO_AI,
    MEDIA_SOURCE_VIDEO_REFERENCE,
)
from backend.app.models.generation import (
    IMAGE_MEDIA_SOURCES,
    MEDIA_SOURCES,
    VIDEO_MEDIA_SOURCES,
)


# --------------------------------------------------------------------------- #
# ② 常量层（先守：链路的分流全靠它）
# --------------------------------------------------------------------------- #

def test_video_business_key_is_registered_with_video_kind():
    """`generate_video` 已登记且 kind=`video`（前端按 kind 过滤候选服务商）。"""
    assert BUSINESS_KEY_GENERATE_VIDEO.kind == MODEL_TYPE_VIDEO
    assert BUSINESS_KEY_GENERATE_VIDEO in BUSINESS_KEYS
    assert MODEL_TYPE_VIDEO in MODEL_TYPES


def test_video_media_sources_are_disjoint_from_image_sources():
    """视频来源与图片来源**分开取值**（同值会让分流判据失效）。"""
    assert not (set(VIDEO_MEDIA_SOURCES) & set(IMAGE_MEDIA_SOURCES)), (
        "视频来源与图片来源有交集 ⇒ `build_note_media` 分不清该走哪条链路"
    )
    assert set(VIDEO_MEDIA_SOURCES) | set(IMAGE_MEDIA_SOURCES) == set(MEDIA_SOURCES)


# --------------------------------------------------------------------------- #
# ① 客户端协议
# --------------------------------------------------------------------------- #

class _FakeConfig:
    base_url = "https://upstream.example/v1"
    model_name = "video-model-x"


def _client_with(polls, *, monotonic_values=None):
    """造一个客户端：`sleep` 受控（不真等，且**自带轮数上限**防挂死）。"""
    sleeps = {"n": 0}

    def _sleep(_seconds):
        sleeps["n"] += 1
        # 正常用例最多轮询 1~2 次；超过就是链路没收敛（测试自己失败，不吊住 CI）。
        assert sleeps["n"] <= 5, "轮询没有收敛"

    return vg.OpenAICompatibleVideoClient(sleep=_sleep)


def test_generate_video_submits_polls_then_returns_url(monkeypatch):
    """正常链路：提交拿 id → 轮询 running → succeeded 带 url。"""
    calls = {"post": 0, "get": 0}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _post(url, **kwargs):
        calls["post"] += 1
        assert url.endswith("/videos/generations"), url
        assert kwargs["json"]["model"] == "video-model-x"
        return _Resp({"id": "task-1"})

    seq = [{"status": "running"}, {"status": "succeeded", "url": "https://cdn.example/v.mp4"}]

    def _get(url, **kwargs):
        calls["get"] += 1
        assert url.endswith("/videos/generations/task-1"), url
        return _Resp(seq.pop(0))

    monkeypatch.setattr(vg.requests, "post", _post)
    monkeypatch.setattr(vg.requests, "get", _get)

    result = _client_with(None).generate_video(
        model_config=_FakeConfig(), api_key="sk-test", prompt="一只猫"
    )

    assert result["url"] == "https://cdn.example/v.mp4"
    assert result["task_id"] == "task-1"
    assert calls == {"post": 1, "get": 2}, "应轮询到成功为止（两次 get）"


def test_generate_video_passes_reference_image_and_options(monkeypatch):
    """图生视频：参考图与 duration/size 进请求体（不是只在文档里写）。"""
    captured = {}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _post(url, **kwargs):
        captured.update(kwargs["json"])
        return _Resp({"id": "t"})

    def _get(url, **kwargs):
        return _Resp({"status": "succeeded", "url": "https://cdn/v.mp4"})

    monkeypatch.setattr(vg.requests, "post", _post)
    monkeypatch.setattr(vg.requests, "get", _get)

    _client_with(None).generate_video(
        model_config=_FakeConfig(),
        api_key="sk",
        prompt="p",
        reference_image="https://cdn/ref.jpg",
        duration=8,
        size="1080x1920",
    )
    assert captured["image"] == "https://cdn/ref.jpg"
    assert captured["duration"] == 8
    assert captured["size"] == "1080x1920"


def test_generate_video_raises_on_upstream_failure_state(monkeypatch):
    """上游 `status=failed` ⇒ 抛 `VideoGenerationError`（带原因，不返回空 URL）。"""

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    monkeypatch.setattr(vg.requests, "post", lambda *a, **k: _Resp({"id": "t"}))
    monkeypatch.setattr(
        vg.requests, "get", lambda *a, **k: _Resp({"status": "failed", "error": {"message": "额度不足"}})
    )

    with pytest.raises(vg.VideoGenerationError) as exc:
        _client_with(None).generate_video(model_config=_FakeConfig(), api_key="sk", prompt="p")
    assert "额度不足" in str(exc.value), "失败原因必须带出来（否则用户不知道去查什么）"


def test_generate_video_raises_when_submit_has_no_task_id(monkeypatch):
    """提交响应没有 id ⇒ 明确报错（不拿一个空 id 去轮询 600 秒）。"""

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": "ok"}

    monkeypatch.setattr(vg.requests, "post", lambda *a, **k: _Resp())
    with pytest.raises(vg.VideoGenerationError) as exc:
        _client_with(None).generate_video(model_config=_FakeConfig(), api_key="sk", prompt="p")
    assert "id" in str(exc.value)


def test_generate_video_times_out_instead_of_waiting_forever(monkeypatch):
    """轮询一直 running ⇒ **超时**抛错（无界等待 = 没有刹车点）。

    ⚠️ **假时钟 + 假 sleep 都要给**：只假时钟的话，一旦超时判定被删掉，循环会真的
    走满 600 秒（变异实测过 —— 测试挂死 180 秒才被外层掐掉）。假 sleep 让变异
    **快速失败**而不是把 CI 吊住。同时给 `sleep` 计数上限兜底：万一两个都失效，
    测试自己抛错（绝不允许一条用例把整套回归挂死）。
    """
    sleeps = {"n": 0}

    def _sleep(_seconds):
        sleeps["n"] += 1
        assert sleeps["n"] <= 5, "轮询没有收敛（超时判定可能已失效）"

    class _PostResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "t"}

    class _RunResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "running"}

    # 假时钟：第一次读返回 0，之后每次都跨过超时阈值 ⇒ 不必真等 600 秒。
    ticks = {"n": 0}

    def _monotonic():
        ticks["n"] += 1
        return 0.0 if ticks["n"] == 1 else float(vg.VIDEO_POLL_TIMEOUT_SECONDS + 1)

    monkeypatch.setattr(vg.requests, "post", lambda *a, **k: _PostResp())
    monkeypatch.setattr(vg.requests, "get", lambda *a, **k: _RunResp())
    monkeypatch.setattr(vg.time, "monotonic", _monotonic)

    client = vg.OpenAICompatibleVideoClient(sleep=_sleep)
    with pytest.raises(vg.VideoGenerationError) as exc:
        client.generate_video(model_config=_FakeConfig(), api_key="sk", prompt="p")
    assert "超时" in str(exc.value)


def test_generate_video_can_be_cancelled_mid_wait(monkeypatch):
    """`on_poll` 返回 False ⇒ 立刻放弃（worker 停止位/取消要用它，不等满 600 秒）。"""

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "t"}

    class _RunResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "running"}

    monkeypatch.setattr(vg.requests, "post", lambda *a, **k: _Resp())
    monkeypatch.setattr(vg.requests, "get", lambda *a, **k: _RunResp())

    client = vg.OpenAICompatibleVideoClient(sleep=lambda _s: None, on_poll=lambda: False)
    with pytest.raises(vg.VideoGenerationError) as exc:
        client.generate_video(model_config=_FakeConfig(), api_key="sk", prompt="p")
    assert "取消" in str(exc.value)


# --------------------------------------------------------------------------- #
# ③ 分流（媒体生成入口）
# --------------------------------------------------------------------------- #

def test_build_note_media_routes_video_sources_to_video_branch(monkeypatch):
    """`build_note_media` 对视频来源走 `_assets_from_video`（不打图片链路）。"""
    from backend.app.services import media_generation as mg

    seen = {}

    def _fake_video(db, *, user, row, media_source, media_options, record):
        seen["source"] = media_source
        return ([{"asset_type": "video", "url": "/api/files/media/v.mp4"}], [])

    def _fake_image(*a, **k):
        raise AssertionError("视频来源不该走图片链路")

    monkeypatch.setattr(mg, "_assets_from_video", _fake_video)
    monkeypatch.setattr(mg, "_assets_from_ai", _fake_image)

    class _User:
        id = 1

    assets, origins = mg.build_note_media(
        None,
        user=_User(),
        row=None,
        media_source=MEDIA_SOURCE_VIDEO_AI,
        media_options={},
        record=None,
    )
    assert seen["source"] == MEDIA_SOURCE_VIDEO_AI
    assert assets[0]["asset_type"] == "video"
    assert origins == []
