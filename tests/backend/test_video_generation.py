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
    BUSINESS_KEY_LLM_NOTE_VIDEO,
    BUSINESS_KEYS,
    MODEL_TYPE_VIDEO,
    MODEL_TYPES,
)
from backend.app.services.media_generation import (
    MEDIA_SOURCE_VIDEO_AI,
    MEDIA_SOURCE_VIDEO_MATERIAL,
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
    assert BUSINESS_KEY_LLM_NOTE_VIDEO.kind == MODEL_TYPE_VIDEO
    assert BUSINESS_KEY_LLM_NOTE_VIDEO in BUSINESS_KEYS
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


# --------------------------------------------------------------------------- #
# 「本地素材」= 直接用，不生成视频（2026-10-16 用户口径）
#   *「使用本地素材这无需再生成视频，后端根据商品，以及提示词，本地素材参考生成相关的
#     文案标题话题等」*
# --------------------------------------------------------------------------- #
def test_video_sources_are_exactly_material_and_ai_generate():
    """视频来源**只有两种**（用户口径：*「视频下拉应只有本地素材和智能生成视频」*）。

    ⚠️ 判据是**集合相等**而不是「包含」：`video_reference` 必须真的退场
    （它和「智能生成 + 参考图」是同一件事，两个入口一实现 = 概念冗余）。
    """
    from backend.app.models.generation import VIDEO_MEDIA_SOURCES

    assert set(VIDEO_MEDIA_SOURCES) == {MEDIA_SOURCE_VIDEO_MATERIAL, MEDIA_SOURCE_VIDEO_AI}


def test_video_material_takes_the_local_video_without_calling_the_model(monkeypatch):
    """本地素材 ⇒ **不调视频模型**（零成本、秒级），只取素材里的视频当媒体。

    判据：① 返回的资产是那条视频；② 视频客户端**一次都没被调用**
    （只断言「返回了视频」不够 —— 真调了模型也会返回视频，但那是白烧钱）。
    """
    from backend.app.services import media_generation as mg

    calls: list[str] = []
    monkeypatch.setattr(
        mg, "_assets_from_material",
        lambda db, **kw: [
            {"asset_type": "video", "url": "/api/xhs/materials/files/9/file", "local_path": "a.mp4", "sort_order": 0},
            {"asset_type": "image", "url": "/api/xhs/materials/files/8/file", "local_path": "b.jpg", "sort_order": 1},
        ],
    )
    monkeypatch.setattr(
        mg, "_assets_from_video",
        lambda *a, **kw: calls.append("video-model-called") or ([], []),
    )
    assets = mg._assets_from_material_video(None, user_id=1, media_options={"file_ids": [9, 8]})
    assert [a["asset_type"] for a in assets] == ["video"]
    assert assets[0]["url"].endswith("/9/file")
    assert calls == [], "本地素材不该调用视频模型（那是白烧一次生成）"


def test_video_material_rejects_images_with_a_readable_reason(monkeypatch):
    """本地素材只选了图片 ⇒ **结构化 400 说清原因**（不静默产出一张图冒充视频）。"""
    from backend.app.services import media_generation as mg

    monkeypatch.setattr(
        mg, "_assets_from_material",
        lambda db, **kw: [
            {"asset_type": "image", "url": "/api/xhs/materials/files/8/file", "local_path": "b.jpg", "sort_order": 0}
        ],
    )
    with pytest.raises(mg.MediaGenerationError) as excinfo:
        mg._assets_from_material_video(None, user_id=1, media_options={"file_ids": [8]})
    assert "MATERIAL_VIDEO_REQUIRED" == excinfo.value.code


def test_video_material_keeps_only_one_video():
    """平台视频笔记**只收 1 个视频** ⇒ 多选了也只取第一条（不静默全发）。

    ⚠️ 这条是**平台契约**（`xhs.postNote`：视频只能一个）—— 多传会被平台校验拒，
    用户以为选了几条就发几条。
    """
    from backend.app.services import media_generation as mg

    entries = [
        {"asset_type": "video", "url": "/api/xhs/materials/files/1/file"},
        {"asset_type": "video", "url": "/api/xhs/materials/files/2/file"},
    ]
    orig = mg._assets_from_material
    mg._assets_from_material = lambda db, **kw: entries
    try:
        got = mg._assets_from_material_video(None, user_id=1, media_options={"file_ids": [1, 2]})
    finally:
        mg._assets_from_material = orig
    assert [a["url"] for a in got] == ["/api/xhs/materials/files/1/file"]


# --------------------------------------------------------------------------- #
# 参考图两类来源都可解析（2026-10-17 用户口径）
#   *「图生成图，和图生视频均应该支持选择参考图，参考图支持选择商品图或本地素材」*
# --------------------------------------------------------------------------- #
def test_reference_resolves_http_urls_as_is():
    """商品图是**公开 CDN 直链** ⇒ 原样返回（不读盘、不 base64，省内存省时间）。"""
    from backend.app.services.ai_service import OpenAICompatibleImageClient

    url = "https://cdn.example.com/a.jpg"
    assert OpenAICompatibleImageClient._resolve_image_ref(url) == url


def test_reference_inlines_local_material_as_data_url(tmp_path, monkeypatch):
    """本地素材 ⇒ **读盘转 base64 data URL**（上游拉不到我们带鉴权的端点）。

    ⚠️ 基准目录必须是 **`material_folders.path`**（用户登记的那个真实目录），
    不是 `storage_dir` —— 拼错会静默回落成「原样返回 URL」，
    于是「选了素材但生成不出来」（本次实测真 bug）。
    """
    from pathlib import Path

    from backend.app.services.ai_service import OpenAICompatibleImageClient

    # 造一张真图片文件在 tmp 目录里
    folder = tmp_path / "素材根"
    sub = folder / "ai-生成" / "batch"
    sub.mkdir(parents=True)
    img = sub / "主图-01.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)

    # 打桩：拿到「文件夹 path + 相对路径」，实际定位逻辑与真实现一致
    class _FakeFile:
        id = 1
        rel_path = "ai-生成/batch/主图-01.png"
        folder_id = 7

    class _FakeFolder:
        id = 7
        path = str(folder)

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, model, ident):
            return _FakeFile() if ident == 1 else _FakeFolder()

    import backend.app.core.database as dbmod

    monkeypatch.setattr(dbmod, "SessionLocal", lambda: _FakeSession())

    out = OpenAICompatibleImageClient._resolve_image_ref("/api/xhs/materials/files/1/file")
    assert out.startswith("data:image/png;base64,"), "本地素材没被内联成 data URL（上游会拉不到）"
    assert len(out) > 40


def test_reference_returns_url_when_material_file_is_missing(tmp_path, monkeypatch):
    """文件不在磁盘上 ⇒ **原样返回**（让上游报错，不在这一层假装成功）。"""
    from backend.app.services.ai_service import OpenAICompatibleImageClient

    class _FakeFile:
        id = 2
        rel_path = "不存在/无.png"
        folder_id = 8

    class _FakeFolder:
        path = str(tmp_path)

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, model, ident):
            return _FakeFile() if ident == 2 else _FakeFolder()

    import backend.app.core.database as dbmod

    monkeypatch.setattr(dbmod, "SessionLocal", lambda: _FakeSession())
    url = "/api/xhs/materials/files/2/file"
    assert OpenAICompatibleImageClient._resolve_image_ref(url) == url


# --------------------------------------------------------------------------- #
# 商品原图去重（用户口径 2026-10-17：*「商品原图实现图片去重」*）
# --------------------------------------------------------------------------- #
def test_product_images_are_deduped_and_keep_order():
    """商品原图 = 主图 + 详情图，**保序去重**。

    真机实测：抽查 400 个商品 **400 个**都有重复图（主图 10 张里有 5 张原样
    重复出现在详情图组，典型 20 张 → 15 张唯一）⇒ 用户看到同一张图重复出现。

    ⚠️ **必须保序**：`pick` 是下标，排序会让「用户选第 3 张、实际出第 5 张」。
    """
    from backend.app.services.media_generation import _product_image_urls

    class _Row:
        # 主图：a b c；详情图：b c d（b、c 重复）
        images_json = '["https://x/a.jpg","https://x/b.jpg","https://x/c.jpg"]'
        image = "https://x/a.jpg"
        raw_json = (
            '{"image_descriptions":[{"link":"https://x/b.jpg"},'
            '{"link":"https://x/c.jpg"},{"link":"https://x/d.jpg"}]}'
        )

    got = _product_image_urls(_Row())
    assert got == [
        "https://x/a.jpg",
        "https://x/b.jpg",
        "https://x/c.jpg",
        "https://x/d.jpg",
    ], f"保序去重失败（拿到 {got}）"
    assert len(got) == len(set(got)), "还有重复图"


def test_product_images_dedup_is_stable_for_pure_main_images():
    """没有重复时不改变顺序、不丢图（去重不能误删）。"""
    from backend.app.services.media_generation import _product_image_urls

    class _Row:
        images_json = '["https://x/1.jpg","https://x/2.jpg"]'
        image = "https://x/1.jpg"
        raw_json = "{}"

    assert _product_image_urls(_Row()) == ["https://x/1.jpg", "https://x/2.jpg"]
