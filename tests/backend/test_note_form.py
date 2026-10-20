"""笔记形态「图文 / 视频」的守卫（设计 41 §15，2026-10-16）。

## 为什么有这一组

小红书平台侧**只有两大类笔记且互斥** —— 官方 `xhs.postNote` 契约原文：
*「图片 或 视频 的 url 只能二选一，视频只能一个；图片可以多张（1-18）」*、
*「image_resources / video_resources 只能二选一，如果多传会触发校验失败」*。
我们自己的 SDK 也是两套 payload（`get_post_note_image_data` 的 `type="normal"`
vs `get_post_note_video_data` 的 `type="video"`）。

本组守住三件事（每一件都对应一个真会发生的退化）：

1. **形态判定**：有视频段 ⇒ 视频笔记（`plan_form`），与「视频排第几」无关；
2. **请求体分叉**：视频笔记 `image_info=None` + `video_info{cover}`（互斥契约）；
3. **发布门禁**：视频缺封面 / 形态与素材不一致 ⇒ 可读失败，不静默发错的形态。
"""
from __future__ import annotations

from backend.app.models.generation import (
    MEDIA_SOURCE_MATERIAL,
    MEDIA_SOURCE_PRODUCT_IMAGE,
    MEDIA_SOURCE_VIDEO_AI,
    MEDIA_SOURCE_VIDEO_MATERIAL,
)
from backend.app.models.note import (
    NOTE_FORMS,
    NOTE_FORM_IMAGE,
    NOTE_FORM_VIDEO,
)
from backend.app.services import media_plan
from backend.app.services import note_publish_service as svc


# --------------------------------------------------------------------------- #
# 1. 形态判定（唯一判据 plan_form）
# --------------------------------------------------------------------------- #
class TestFormJudgement:
    def test_pure_images_is_an_image_note(self):
        assert media_plan.plan_form([{"source": MEDIA_SOURCE_PRODUCT_IMAGE, "role": "cover"}]) == NOTE_FORM_IMAGE

    def test_mixed_plan_is_a_video_note(self):
        """有视频段 ⇒ 视频笔记（图片段退化为封面）—— 与视频排第几无关。"""
        plan = [
            {"source": MEDIA_SOURCE_PRODUCT_IMAGE, "role": "cover"},
            {"source": MEDIA_SOURCE_VIDEO_AI, "role": "content"},
        ]
        assert media_plan.plan_form(plan) == NOTE_FORM_VIDEO

    def test_video_first_is_still_a_video_note(self):
        """⚠️ 反向判据：判「有没有视频」而不是「视频是不是第一个」——
        写成 `plan[0]` 的实现在这条上会红。"""
        plan = [
            {"source": MEDIA_SOURCE_VIDEO_AI, "role": "cover"},
            {"source": MEDIA_SOURCE_MATERIAL, "role": "content"},
        ]
        assert media_plan.plan_form(plan) == NOTE_FORM_VIDEO

    def test_both_video_sources_count(self):
        """两种**现行**视频来源都判视频笔记。

        ⚠️ 2026-10-16 口径变更：视频来源收敛为「本地素材 / 智能生成视频」两种
        （用户原话「视频下拉应只有本地素材和智能生成视频」），`video_reference`
        已退场 ⇒ 改为遍历 `VIDEO_MEDIA_SOURCES`（**唯一真相源**，不是手抄两个字面量：
        未来再加来源时这条自动覆盖）。
        """
        from backend.app.models.generation import VIDEO_MEDIA_SOURCES

        assert set(VIDEO_MEDIA_SOURCES) == {MEDIA_SOURCE_VIDEO_MATERIAL, MEDIA_SOURCE_VIDEO_AI}
        for source in VIDEO_MEDIA_SOURCES:
            assert media_plan.plan_form([{"source": source, "role": "cover"}]) == NOTE_FORM_VIDEO

    def test_form_values_are_exactly_the_two_platform_categories(self):
        """平台只有两大类 ⇒ 常量集合必须恰好两个（多了就是造概念）。"""
        assert set(NOTE_FORMS) == {"image", "video"}


# --------------------------------------------------------------------------- #
# 2. 请求体分叉（互斥契约：image_info 与 video_info 不能同时给）
# --------------------------------------------------------------------------- #
class TestPublishBodyByForm:
    def _common(self):
        return {"type": "video", "title": "t", "desc": "d", "hash_tag": [], "ats": []}

    def test_video_body_has_no_image_info(self):
        """⚠️ 平台契约：`image_resources` / `video_resources` **只能二选一**，
        视频笔记带 `image_info` 会被校验拒 —— 这条是互斥契约的守卫。"""
        body = svc._video_publish_body(self._common(), [{"file_id": "C1", "width": 1, "height": 2}], {"file_id": "V1"})
        assert body["image_info"] is None, "视频笔记不得携带 image_info（平台只接受二选一）"

    def test_video_body_carries_video_info_with_independent_cover(self):
        """封面是**独立上传**的文件（不是视频帧）：cover.fileid 指向封面那个 file_id。"""
        body = svc._video_publish_body(self._common(), [{"file_id": "C1", "width": 1, "height": 2}], {"file_id": "V1"})
        info = body["video_info"]
        assert info["file_id"] == "spectrum/V1"
        assert info["cover"]["file_id"] == "spectrum/C1", "封面必须是独立上传的图，不是视频的 file_id"
        assert info["cover"]["frame"]["is_upload"] is True

    def test_video_body_keeps_single_segment(self):
        """平台只接受 **1 个视频** ⇒ segments 恒 1 段（否则是「多个视频」的非法体）。"""
        body = svc._video_publish_body(self._common(), [{"file_id": "C1"}], {"file_id": "V1"})
        assert body["video_info"]["segments"]["count"] == 1
        assert len(body["video_info"]["segments"]["items"]) == 1

    def test_body_builder_picks_video_shape_only_when_video_given(self):
        """判据落在 `_build_publish_body` 的**调用形态**上（别只测内部函数）。"""
        import inspect

        src = inspect.getsource(svc._build_publish_body)
        assert '"type": "video" if video is not None else "normal"' in src, (
            "`common.type` 必须由形态派生（写死 normal ⇒ 视频笔记永远发成图文）"
        )
        assert "_video_publish_body(common, image_entries, video)" in src


# --------------------------------------------------------------------------- #
# 3. 发布门禁（按形态取媒体；缺件即可读失败）
# --------------------------------------------------------------------------- #
class TestSelectMediaByForm:
    def _note(self, form, assets):
        class _A:
            def __init__(self, kind, url):
                self.asset_type, self.url = kind, url

            def __getitem__(self, key):
                return getattr(self, key)

        class _N:
            def __init__(self):
                self.id = 1
                self.note_form = form

        return _N(), [_A(k, u) for k, u in assets]

    def _run(self, monkeypatch, form, assets):
        note, rows = self._note(form, assets)
        monkeypatch.setattr(svc, "_assets_of", lambda db, pk: rows)
        return svc._select_media(None, note)

    def test_image_note_returns_images_and_no_video(self, monkeypatch):
        got = self._run(monkeypatch, NOTE_FORM_IMAGE, [("image", "u1"), ("image", "u2")])
        assert got["form"] == NOTE_FORM_IMAGE
        assert len(got["images"]) == 2
        assert got["video"] is None

    def test_video_note_returns_one_cover_and_one_video(self, monkeypatch):
        """视频笔记只带 **1 张封面** + 1 个视频 —— 其余图不参与（互斥契约）。
        反例：把图全带上 ⇒ 这条会红（张数变 3）。"""
        got = self._run(
            monkeypatch, NOTE_FORM_VIDEO, [("image", "c"), ("image", "x"), ("video", "v")]
        )
        assert got["form"] == NOTE_FORM_VIDEO
        assert len(got["images"]) == 1, "视频笔记只应带封面那一张图"
        assert got["video"]["url"] == "v"

    def test_video_note_without_video_fails_readably(self, monkeypatch):
        """视频可能生成失败 ⇒ 明确说「没有视频素材」，不静默按图文发。"""
        import pytest

        with pytest.raises(svc.NotePublishError) as exc:
            self._run(monkeypatch, NOTE_FORM_VIDEO, [("image", "c")])
        assert exc.value.code == "NOTE_VIDEO_MEDIA_EMPTY"

    def test_video_note_without_cover_fails_readably(self, monkeypatch):
        """⚠️ 封面是**必需**的（平台视频笔记封面独立上传）—— 缺封面必须拦在同步段。"""
        import pytest

        with pytest.raises(svc.NotePublishError) as exc:
            self._run(monkeypatch, NOTE_FORM_VIDEO, [("video", "v")])
        assert exc.value.code == "NOTE_VIDEO_COVER_EMPTY"

    def test_image_form_with_only_video_is_a_mismatch(self, monkeypatch):
        """形态说图文、素材只有视频 ⇒ 报不一致（而不是静默发成视频）。"""
        import pytest

        with pytest.raises(svc.NotePublishError) as exc:
            self._run(monkeypatch, NOTE_FORM_IMAGE, [("video", "v")])
        assert exc.value.code == "NOTE_FORM_MEDIA_MISMATCH"


class TestFormFallback:
    def test_missing_form_falls_back_to_image(self):
        """存量行 / 未指明写入 ⇒ 按图文（与迁移回填一致）。"""

        class _N:
            note_form = ""

        assert svc.note_form_of(_N()) == NOTE_FORM_IMAGE

    def test_unknown_form_falls_back_to_image(self):
        """未知值按图文（脏行仍可发布），但写入侧必须校验 —— 见下面那条。"""

        class _N:
            note_form = "weird"

        assert svc.note_form_of(_N()) == NOTE_FORM_IMAGE

    def test_generation_writes_form_from_the_plan(self):
        """⚠️ 写入侧必须真的写 `note_form`（漏写 ⇒ 视频笔记被当图文发）。
        判据落在**调用形态**上：worker 里必须出现 `note_form=plan_form(plan)`。"""
        import inspect

        from backend.app.services import note_gen_llm_worker as worker

        src = inspect.getsource(worker)
        assert "note_form=plan_form(plan)" in src, "生成落库必须按计划写形态（漏写即静默发错形态）"

    def test_synced_notes_never_go_through_the_publish_chain(self):
        """**同步笔记不进发布链路 ⇒ 「有视频的 synced 行 form=image」不是缺陷**（查证结论）。

        ## 为什么钉这条

        2026-10-16 实测：真库 81 条 `asset_type='video'` 资产分布在 **37 篇笔记**上，
        **全部 `note_type='synced'`（从平台搬回来的）**，且每篇**同时有图片**
        （如 7 图 6 视频）—— 我当时把它当成「形态没回填」的存量缺陷，**是错的**：

        * `note_form` 只描述**「我们本地生成的这篇要按什么形态发」**；
        * 发布链路 `_check_publishable()` **只放行 `note_type == local_generated`**
          ⇒ 那 37 篇同步笔记**永远不走发布**，碰不到 `NOTE_FORM_MEDIA_MISMATCH`；
        * 图文笔记里含视频是**平台的正常形态**（我们只是同步回来存档）。

        ⚠️ **硬去把它们回填成 `video` 反而错** —— 会破坏 `note_form` 的语义
        （它不该描述「平台搬回来的东西长什么样」）。存量全 `image` 是无害默认值。

        本守卫 = 把「同步笔记不进发布」这条口径钉住，免得后来者看到
        `synced + image + 有视频` 又当缺陷报一遍（我踩过）。
        """
        import inspect

        from backend.app.services import note_publish_service as svc

        src = inspect.getsource(svc._check_publishable)
        assert "NOTE_TYPE_LOCAL_GENERATED" in src, (
            "发布门禁未限定「只有本地生成笔记可发布」—— 同步笔记若进发布链路，"
            "「同步行 form=image 且有视频」就会真的触发 NOTE_FORM_MEDIA_MISMATCH"
        )


# --------------------------------------------------------------------------- #
# 4. 迁移（列 + 回填 + 幂等）
# --------------------------------------------------------------------------- #
class TestMigration:
    def test_migration_backfills_image_and_is_idempotent(self):
        """迁移把存量行回填 `image`（历史只成功发布过图文），且重跑安全。"""
        import importlib

        mod = importlib.import_module("backend.alembic.versions.b3e4f5a6c7d8_add_note_form")
        assert mod.DEFAULT_FORM == "image", "存量回填必须是图文（视频链路此前未接通）"
        assert mod.down_revision == "a1d2p3r4u5l6"
