"""视频提示词接公共模板（设计 17 §13.8，v2.26）。

背景：§13.6 让商品图接上了公共模板，但**视频提示词仍是「手输 + 内置兜底」** ——
`prompt_templates.task_type` 的白名单**早就含 `video`**，能力留好了却没人用
（与 §13.6.1「抽出来了却没接上」同一形态）。

本文件守四件：
① 传 `template_id` ⇒ 上游收到的 prompt 是**模板渲染结果**（不是内置兜底文案）；
② 优先级 **模板 > 手输 > 兜底**（手输降级为「补充要求」）；
③ 模板越权 / 不存在 ⇒ **报错，不静默回落**成手输；
④ 不传 ⇒ 与旧行为**完全一致**（可选增强，零回归）。
"""

from __future__ import annotations

import pytest

from backend.app.models.shop_product import ShopProduct


class _User:
    id = 1


def _seed_template(db, *, user_id: int = 1, name: str = "视频模板", task_type: str = "video") -> int:
    """建一张模板（引用内置一致性模块），返回 id。"""
    from backend.app.models.prompt import PromptModule, PromptTemplate

    module = (
        db.query(PromptModule)
        .filter_by(user_id=user_id, name="电商图一致性锁（防漂移）")
        .first()
    )
    template = PromptTemplate(
        user_id=user_id,
        name=name,
        task_type=task_type,
        prompt_skeleton="【视频模板】为 {title} 生成展示视频。",
        module_ids=[module.id] if module else [],
        is_default=False,
        version=1,
    )
    db.add(template)
    db.commit()
    return template.id


@pytest.fixture()
def video_env(tmp_path, monkeypatch):
    """临时库 + 一个商品行 + 一个用户（含内置模块）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.app.core.database import Base
    from tests.backend.conftest import seed_owner_rows
    from backend.app.services import media_generation as mg

    engine = create_engine(
        f"sqlite:///{tmp_path / 'video-prompt.db'}", connect_args={"check_same_thread": False}
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    # ⚠️ 全局 FK=ON 后必须播种父行（本文件插 shop_products(user_id=1,
    # platform_account_id=1)，2026-10-04 实测 6 条红）。
    seed_owner_rows(engine, user_id=1, account_id=1)

    db = TestingSession()
    row = ShopProduct(
        user_id=1,
        platform_account_id=1,
        item_id="ITEM-V1",
        # ⚠️ ORM 列名是 `item_title`（不是 `title`）—— 槽位名 `title` 由
        # `load_product_dict_from_ledger` 转换时提供，两者不是一个东西
        item_title="白橘运动包",
    )
    db.add(row)
    db.commit()

    # 内置模块（模板要引用它 —— 与真实播种同形）
    from backend.app.models.prompt import PromptModule

    db.add(
        PromptModule(
            user_id=1,
            name="电商图一致性锁（防漂移）",
            category="style",
            content="统一光位：左上方 45° 柔光 + 右侧补光板，阴影自然、方向一致。",
            sort_order=10,
            enabled=True,
        )
    )
    db.commit()

    yield db, row, TestingSession

    db.close()


def test_template_video_prompt_renders_skeleton_and_modules(video_env):
    """模板渲染：骨架 + 引用的一致性模块都进去，且用 **USE_VIDEO** 的构图档。

    ⚠️ 用途档必须是 video（竖版 9:16 + 动态场景），不能落到图片的 3:4 静帧
    —— 那会让视频提示词带上「画面干净、静止」这类与视频矛盾的约束。
    """
    from backend.app.services import media_generation as mg

    db, row, _ = video_env
    template_id = _seed_template(db)

    out = mg._template_video_prompt(db, _User(), template_id=template_id, row=row)

    assert "【视频模板】" in out, "模板骨架没进 prompt"
    assert "白橘运动包" in out, "商品名没被插槽注入"
    assert "统一光位" in out, "模板引用的一致性模块没渲染进去"
    # 视频用途档（9:16）——不是图片的 3:4
    assert "9:16" in out, "没带视频构图档（会退化成图片的 3:4 口径）"


def test_template_video_prompt_rejects_foreign_template(video_env):
    """模板不属于本用户 ⇒ 抛错，**不静默回落**成手输。

    静默回落的观感是「视频生成成功了」，但用户选的模板一句没用上 ——
    与 §13.6.3「不静默回落」同一纪律。
    """
    from backend.app.services.media_generation import MediaGenerationError
    from backend.app.services import media_generation as mg

    db, row, _ = video_env
    with pytest.raises(MediaGenerationError) as excinfo:
        mg._template_video_prompt(db, _User(), template_id=999999, row=row)
    assert excinfo.value.code == "PROMPT_TEMPLATE_NOT_FOUND"


def test_template_video_prompt_rejects_unknown_slot(video_env):
    """骨架含未知插槽 ⇒ 抛错（渲染器「不部分替换」纪律）。"""
    from backend.app.services.media_generation import MediaGenerationError
    from backend.app.services import media_generation as mg
    from backend.app.models.prompt import PromptTemplate

    db, row, _ = video_env
    template_id = _seed_template(db, name="坏骨架")
    row_t = db.query(PromptTemplate).filter_by(id=template_id).one()
    row_t.prompt_skeleton = "出图：{title}，{unknown_slot}"
    db.commit()

    with pytest.raises(MediaGenerationError) as excinfo:
        mg._template_video_prompt(db, _User(), template_id=template_id, row=row)
    assert excinfo.value.code == "PROMPT_SLOT_UNKNOWN"


def test_assets_from_video_uses_template_over_manual_prompt(video_env, monkeypatch):
    """**模板优先于手输**，且手输降级为「补充要求」（不顶掉模板）。

    用户显式选了模板 ⇒ 必须以模板为准；否则用户会以为模板没生效。
    """
    from backend.app.services import media_generation as mg

    db, row, _ = video_env
    template_id = _seed_template(db)
    # ⚠️ `_business_model_context` 是**函数内 import**（`from backend.app.api.ai import …`），
    # 打桩必须落在 `api.ai` 上 —— 打 `mg` 会 AttributeError（模块没这个属性）
    import backend.app.api.ai as ai_mod

    monkeypatch.setattr(ai_mod, "_business_model_context", lambda *a, **k: (object(), "k"))

    sent: dict = {}

    class _Client:
        def generate_video(self, *, model_config, api_key, prompt, reference_image="", duration=0, size=""):
            sent["prompt"] = prompt
            return {"url": "https://cdn.test/v.mp4"}

    # 同理：客户端也是**函数内 import**，打桩落在 video_generation 模块上
    import backend.app.services.video_generation as vg_mod

    monkeypatch.setattr(vg_mod, "OpenAICompatibleVideoClient", lambda: _Client())

    mg._assets_from_video(
        db,
        user=_User(),
        row=row,
        media_source="video_ai_generate",
        media_options={"template_id": template_id, "prompt": "这里是手输补充"},
        record=None,
    )
    prompt = sent["prompt"]
    assert "【视频模板】" in prompt, "模板没被用上（退回手输了）"
    assert "这里是手输补充" in prompt, "手输应作为补充要求保留"


def test_assets_from_video_without_template_is_unchanged(video_env, monkeypatch):
    """不传模板 ⇒ 旧行为完全一致（可选增强，零回归）。

    成对的另一半：只守「传了就生效」会让「变成必填」也变绿，
    而那会破坏既有调用方。
    """
    from backend.app.services import media_generation as mg

    db, row, _ = video_env
    # ⚠️ `_business_model_context` 是**函数内 import**（`from backend.app.api.ai import …`），
    # 打桩必须落在 `api.ai` 上 —— 打 `mg` 会 AttributeError（模块没这个属性）
    import backend.app.api.ai as ai_mod

    monkeypatch.setattr(ai_mod, "_business_model_context", lambda *a, **k: (object(), "k"))

    sent: dict = {}

    class _Client:
        def generate_video(self, *, model_config, api_key, prompt, reference_image="", duration=0, size=""):
            sent["prompt"] = prompt
            return {"url": "https://cdn.test/v.mp4"}

    # 同理：客户端也是**函数内 import**，打桩落在 video_generation 模块上
    import backend.app.services.video_generation as vg_mod

    monkeypatch.setattr(vg_mod, "OpenAICompatibleVideoClient", lambda: _Client())

    mg._assets_from_video(
        db,
        user=_User(),
        row=row,
        media_source="video_ai_generate",
        media_options={},
        record=None,
    )
    prompt = sent["prompt"]
    # 没传模板 ⇒ 走手输为空 ⇒ 内置兜底（含商品名 + USE_VIDEO 约束）
    assert "视频" in prompt
    assert "【视频模板】" not in prompt, "不传模板却出现了模板内容"


def test_assets_from_video_invalid_template_id_reports_argument_error(video_env, monkeypatch):
    """模板 id 不是整数 ⇒ 报「参数无效」，**不要**误报成「模板不存在」。

    两者自救动作完全不同：一个是改参数，一个是去看模板 —— 混在一起会误导排查。
    """
    from backend.app.services.media_generation import MediaGenerationError
    from backend.app.services import media_generation as mg

    db, row, _ = video_env
    # ⚠️ `_business_model_context` 是**函数内 import**（`from backend.app.api.ai import …`），
    # 打桩必须落在 `api.ai` 上 —— 打 `mg` 会 AttributeError（模块没这个属性）
    import backend.app.api.ai as ai_mod

    monkeypatch.setattr(ai_mod, "_business_model_context", lambda *a, **k: (object(), "k"))

    with pytest.raises(MediaGenerationError) as excinfo:
        mg._assets_from_video(
            db,
            user=_User(),
            row=row,
            media_source="video_ai_generate",
            media_options={"template_id": "not-a-number"},
            record=None,
        )
    assert excinfo.value.code == "PROMPT_TEMPLATE_INVALID"
