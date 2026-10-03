"""生成 AI 商品图 × 公共提示词模板（设计 17 §13.5.4，v2.24）。

用户口径原文：

> 「之前你把一致性抽取成公共模板了，但是未使用，在生成图片时应选择公共提示词模板，
>   保证图片商品的一致性」

这条口径暴露的是**两层**缺口，本文件各守一层：

1. **模板表是空的** ⇒ 弹窗里根本没有模板可选，选了也无内容 ——
   由**种子**修（`config/initial_data/prompt_templates.json` + 启动/注册播种），
   守：种子能载入、按用户幂等、注册即播种、模块先于模板（顺序错了模板引用不到 id）。
2. **出图链路没接模板** ⇒ 就算模板存在也不会生效 ——
   由 `template_id` 渲染修，守：渲染结果真的进了 prompt、优先级（模板 > 手输）、
   组级模板、租户隔离、不静默回落（404 / 400）。

⚠️ 为什么单独一个文件而不是塞进 `test_api.py`：种子与渲染是**两条独立的失败面**
（种子坏了 = 下拉空；渲染没接 = 选了没反应），混在一起挂一条会让「哪层坏了」难判。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.app.core.initial_data import load_initial_prompt_templates
from backend.app.main import app

# ⚠️ **必须 import 模块本身**，不能 `from ... import _test_session_factory`：
# 那个名字在 `_override_database()` 里才被赋值，`from X import name` 是**值拷贝**，
# 导入时拿到的是 None ⇒ 每次播种都 `TypeError: 'NoneType' object is not callable`。
from tests.backend import test_api as api_mod
from tests.backend.test_api import (
    _override_database,
    _register_and_get_access_token,
    _seed_account_for_last_user,
    _seed_shop_product,
)


def _db():
    """开一个临时库 Session（`_override_database()` 之后才有效）。"""
    return api_mod._test_session_factory()


def _uid() -> int:
    return api_mod._last_registered_user_id


# --------------------------------------------------------------------------- #
# 第一层：种子（「有没有模板可选」）
# --------------------------------------------------------------------------- #


def test_initial_data_ships_consistency_module_and_two_templates():
    """随包种子必须带**一致性模块**与主图/详情图两张模板。

    用户要的是「公共提示词模板保证图片商品的一致性」—— 一致性若不在种子里，
    用户第一次装完打开弹窗仍然只有一张空模板（等于没修）。
    """
    payload = load_initial_prompt_templates()
    modules = payload["modules"]
    templates = payload["templates"]

    names = {m["name"] for m in modules}
    assert "电商图一致性锁（防漂移）" in names, f"种子缺一致性模块：{names}"

    # 一致性三条约束必须**逐字**在模块里（措辞是实测有效的，不能被改写成泛泛的话）
    consistency = next(m for m in modules if m["name"] == "电商图一致性锁（防漂移）")
    for fragment in ("统一光位", "保留商品真实材质", "禁止改变产品形状"):
        assert fragment in consistency["content"], f"一致性约束缺「{fragment}」"

    task_types = {t["task_type"] for t in templates}
    assert task_types == {"image", "cover"}, f"种子模板任务类型不对：{task_types}"
    for template in templates:
        assert template["is_default"] is True, "种子模板必须是默认（否则弹窗不会自动选中）"
        assert template["module_names"], "种子模板必须引用一致性模块（否则一致性不生效）"
        # 骨架必须引用商品槽位（不引用 = 商品信息进不了提示词）
        assert "{title}" in template["prompt_skeleton"]


def test_seed_prompt_templates_is_idempotent_and_does_not_overwrite(tmp_path):
    """播种**幂等**且**不覆盖用户改过的行**。

    ⚠️ 这两件事必须一起守：「重跑播种会重建」与「重跑播种会改回默认」是两种不同的坏 ——
    前者把用户删掉的模板又冒出来，后者把用户调好的措辞改回去。种子是**起点**，
    库里的是**用户的**。
    """
    from backend.app.api.llm_prompts import seed_builtin_prompt_templates
    from backend.app.models.prompt import PromptModule, PromptTemplate

    db_dependency = _override_database(tmp_path)
    _register_and_get_access_token("template-seed-idem")
    try:
        db = _db()
        try:
            uid = _uid()
            # 注册时已经播过一次（见 auth.py）—— 这里再播一次必须是 0 新增
            assert seed_builtin_prompt_templates(db, user_id=uid) == 0
            db.commit()

            # 用户改掉模板骨架 ⇒ 再播种**不得**改回
            template = db.query(PromptTemplate).filter_by(user_id=uid).first()
            assert template is not None
            template.prompt_skeleton = "用户自己改过的骨架 {item_title}"
            db.commit()

            assert seed_builtin_prompt_templates(db, user_id=uid) == 0
            db.refresh(template)
            assert template.prompt_skeleton == "用户自己改过的骨架 {item_title}", (
                "播种覆盖了用户改过的模板 —— 种子只能补「缺失」，不能改「已有」"
            )
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_register_seeds_prompt_templates_for_new_user(tmp_path):
    """**注册即播种** —— 少了这一步新用户仍然选不到模板。

    启动播种只覆盖「启动时已存在的用户」；新注册的用户若不播，第一次打开出图弹窗
    下拉就是空的（这正是本轮的缺口形态）。
    """
    from backend.app.models.prompt import PromptModule, PromptTemplate

    db_dependency = _override_database(tmp_path)
    _register_and_get_access_token("template-seed-register")
    try:
        db = _db()
        try:
            uid = _uid()
            templates = db.query(PromptTemplate).filter_by(user_id=uid).all()
            modules = db.query(PromptModule).filter_by(user_id=uid).all()
            assert len(templates) == 2, f"注册后应有 2 张内置模板，实得 {len(templates)}"
            assert len(modules) == 2, f"注册后应有 2 个内置模块，实得 {len(modules)}"

            # ① 模块先于模板：模板引用的 module_ids 必须**都解析得到**
            #    （顺序写反 ⇒ module_ids 为空 ⇒ 一致性约束静默不生效）
            for template in templates:
                assert template.module_ids, f"模板「{template.name}」没引用到模块"
                owned = {m.id for m in modules}
                assert set(template.module_ids) <= owned, "模板引用了不存在的模块 id"

            # ② 每个 task_type 恰好一张默认（弹窗按 task_type 找默认）
            defaults = [t for t in templates if t.is_default]
            assert len(defaults) == 2
            assert {t.task_type for t in defaults} == {"image", "cover"}
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)


# --------------------------------------------------------------------------- #
# 第二层：渲染（「选了模板到底生效没有」）
# --------------------------------------------------------------------------- #


class _RecordingImageClient:
    """记录每次收到的 prompt 与参考图（用来断言模板/风格图真的进了上游请求）。"""

    def __init__(self):
        self.prompts: list[str] = []
        #: 每次调用收到的参考图列表（长度与顺序都要能验 —— 风格图约定是**最后一张**）
        self.references: list[list[str]] = []

    def generate_image(self, *, model_config, api_key, prompt, reference_images=None):
        self.prompts.append(prompt)
        self.references.append(list(reference_images or []))
        return {"url": f"https://cdn.example.test/t{len(self.prompts)}.png", "local_path": ""}


def _seed_image_provider() -> None:
    """给当前用户配一个 image 类服务商（出图链路的前置门禁）。"""
    from backend.app.api.ai import get_image_ai_client
    from backend.app.core.security import encrypt_text
    from backend.app.models import LlmBusinessProvider, LlmGatewayConfig, LlmGatewayModel
    from backend.app.services.business_provider_keys import (
        BUSINESS_KEY_GENERATE_PRODUCT_DETAIL_IMAGE,
    )

    spec = BUSINESS_KEY_GENERATE_PRODUCT_DETAIL_IMAGE
    db = _db()
    try:
        config = LlmGatewayConfig(
            user_id=_uid(),
            name="test-image-template",
            provider_key="TEST-IMGT",
            base_url="https://api.example.test/v1",
            access_token=encrypt_text("«redacted:sk-test»"),
            models_json=json.dumps(["detail-image-model"]),
            enabled=True,
            healthy=True,
        )
        db.add(config)
        db.flush()
        # ⚠️ v2.7/v2.8：能力类型与使用平台在**模型行**（服务商层已移除）
        db.add(
            LlmGatewayModel(
                user_id=_uid(),
                provider_key="TEST-IMGT",
                model_name="detail-image-model",
                model_type="image",
                platform="xhs",
                enabled=True,
            )
        )
        db.add(
            LlmBusinessProvider(
                user_id=_uid(),
                platform="xhs",
                business_key=spec.key,
                provider_key="TEST-IMGT",
                model_name="detail-image-model",
                enabled=True,
            )
        )
        db.commit()
    finally:
        db.close()


def _make_template(*, task_type: str = "image", name: str = "测试模板") -> int:
    """建一张模板（引用内置一致性模块），返回 id。"""
    from backend.app.models.prompt import PromptModule, PromptTemplate

    db = _db()
    try:
        module = (
            db.query(PromptModule)
            .filter_by(user_id=_uid(), name="电商图一致性锁（防漂移）")
            .first()
        )
        template = PromptTemplate(
            user_id=_uid(),
            name=name,
            task_type=task_type,
            prompt_skeleton="【自定义模板】为 {title} 出图。",
            module_ids=[module.id] if module else [],
            is_default=False,
            version=1,
        )
        db.add(template)
        db.commit()
        return template.id
    finally:
        db.close()


def test_template_prompt_reaches_upstream_and_wins_over_manual(tmp_path):
    """**模板渲染结果真的进了上游 prompt，且优先于手输**。

    用户口径是「**应选择**公共提示词模板」—— 选了模板就必须以模板为准；
    手输降级为「补充要求」。若手输仍然覆盖模板，用户会以为模板没生效。
    """
    from backend.app.api.ai import get_image_ai_client

    fake = _RecordingImageClient()
    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("template-render")
    try:
        app.dependency_overrides[get_image_ai_client] = lambda: fake
        _seed_image_provider()
        account_id = _seed_account_for_last_user()
        _seed_shop_product(
            platform_account_id=account_id,
            item_id="ITEM-TPL-1",
            images=["https://cdn.xhs.test/a.jpg"],
        )
        template_id = _make_template(name="模板链路")

        response = TestClient(app).post(
            "/api/xhs/qianfan/generate-detail-image",
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-TPL-1",
                "reference_image_urls": ["https://cdn.xhs.test/a.jpg"],
                "count": 3,
                "template_id": template_id,
                "prompt": "这里是手输补充",
                "refine_prompt": False,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        assert fake.prompts, "没有向上游发起出图请求"

        sent = fake.prompts[0]
        assert "【自定义模板】" in sent, "模板内容没进 prompt —— 选了模板等于没选"
        assert "测试商品" in sent, "商品名没被模板插槽注入"
        assert "这里是手输补充" in sent, "手输应作为「补充要求」保留"
        # 一致性模块内容也必须在（模板引用了它）
        assert "统一光位" in sent, "模板引用的一致性模块没被渲染进去"
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_image_ai_client, None)


def test_template_not_owned_returns_404_and_does_not_silently_fall_back(tmp_path):
    """模板不属于本用户 ⇒ 404，**不静默回落**到默认提示词。

    静默回落的观感是「出图成功了」，但用户精心选的模板一句没用上 —— 比报错更坏。
    """
    from backend.app.api.ai import get_image_ai_client

    fake = _RecordingImageClient()
    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("template-owner")
    try:
        app.dependency_overrides[get_image_ai_client] = lambda: fake
        _seed_image_provider()
        account_id = _seed_account_for_last_user()
        _seed_shop_product(
            platform_account_id=account_id,
            item_id="ITEM-TPL-2",
            images=["https://cdn.xhs.test/b.jpg"],
        )

        response = TestClient(app).post(
            "/api/xhs/qianfan/generate-detail-image",
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-TPL-2",
                "reference_image_urls": ["https://cdn.xhs.test/b.jpg"],
                "template_id": 999999,
                "refine_prompt": False,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 404, response.text
        assert response.json()["detail"]["code"] == "PROMPT_TEMPLATE_NOT_FOUND"
        assert not fake.prompts, "模板不存在却仍然发起了出图请求（静默回落）"
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_image_ai_client, None)


def test_template_with_unknown_slot_returns_400(tmp_path):
    """骨架含未知插槽 ⇒ 400 且带结构化原因（渲染器「不部分替换」纪律）。

    未知插槽若被静默替换成空串，模板作者永远不知道自己写错了 —— 提示词会静默缺内容。
    """
    from backend.app.api.ai import get_image_ai_client
    from backend.app.models.prompt import PromptTemplate

    fake = _RecordingImageClient()
    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("template-slot")
    try:
        app.dependency_overrides[get_image_ai_client] = lambda: fake
        _seed_image_provider()
        account_id = _seed_account_for_last_user()
        _seed_shop_product(
            platform_account_id=account_id,
            item_id="ITEM-TPL-3",
            images=["https://cdn.xhs.test/c.jpg"],
        )
        bad_id = _make_template(name="坏骨架")
        db = _db()
        try:
            row = db.query(PromptTemplate).filter_by(id=bad_id).one()
            row.prompt_skeleton = "出图：{title}，{unknown_slot}"
            db.commit()
        finally:
            db.close()

        response = TestClient(app).post(
            "/api/xhs/qianfan/generate-detail-image",
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-TPL-3",
                "reference_image_urls": ["https://cdn.xhs.test/c.jpg"],
                "template_id": bad_id,
                "refine_prompt": False,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "PROMPT_SLOT_UNKNOWN"
        assert not fake.prompts
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_image_ai_client, None)


def test_group_template_overrides_top_level_template(tmp_path):
    """**组级模板优先**于顶层模板（用户口径「使用 tab，各自的提示词模板」）。

    主图与详情图的风格诉求本就不同（主图要留叠字位、详情图要干净）；
    只有顶层模板会让「两个 Tab 各选一个」变成摆设。
    """
    from backend.app.api.ai import get_image_ai_client

    fake = _RecordingImageClient()
    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("template-group")
    try:
        app.dependency_overrides[get_image_ai_client] = lambda: fake
        _seed_image_provider()
        account_id = _seed_account_for_last_user()
        _seed_shop_product(
            platform_account_id=account_id,
            item_id="ITEM-TPL-4",
            images=["https://cdn.xhs.test/d.jpg"],
        )
        top_id = _make_template(task_type="image", name="顶层模板")
        group_id = _make_template(task_type="cover", name="组级模板")

        response = TestClient(app).post(
            "/api/xhs/qianfan/generate-detail-image",
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-TPL-4",
                "reference_image_urls": ["https://cdn.xhs.test/d.jpg"],
                "template_id": top_id,
                "group_templates": {"product": {"template_id": group_id}},
                "group_settings": {"product": {"count": 3}},
                "refine_prompt": False,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        assert fake.prompts
        # 只跑 product 组 ⇒ 上游只收到组级模板的渲染结果
        assert len(fake.prompts) == 3, f"应只跑 product 组 3 张，实得 {len(fake.prompts)}"
        for sent in fake.prompts:
            assert "【自定义模板】" in sent
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_image_ai_client, None)


def test_render_template_prompt_rejects_unknown_slot_directly():
    """服务层直接调用也要拦未知插槽（不只端点层拦）。"""
    from backend.app.services import detail_image_prompt, prompt_renderer

    class _Template:
        prompt_skeleton = "为 {title} 出图 {unknown_slot}"

    with pytest.raises(prompt_renderer.PromptRenderError):
        detail_image_prompt.render_template_prompt(
            template=_Template(), modules=[], item={"title": "商品"}, skus=[]
        )


def test_template_prompt_does_not_duplicate_covered_constraints(tmp_path):
    """模板已覆盖的约束**不得重复追加**（真机实测过的坑）。

    种子模板引用了「一致性锁 / 平台合规」两个模块，而服务层还会用
    `image_prompt.requirement_lines()` 补「模板没覆盖的约束」。若不做判重，
    同一组约束会在 prompt 里出现**两遍** —— 不是「更保险」：prompt 变长，
    且同一约束两种措辞时模型会各取一半。

    ⚠️ 判重必须**归一化**三层差异，否则等于没判（真机逐层实测）：
    ① 行首 `- ` 前缀（库常量有、模块正文没有）；
    ② 行尾标点（库 `；`、模块 `。`）；
    ③ 指代词（库「图中…」、种子「画面中…」）。
    """
    from backend.app.services import detail_image_prompt

    db_dependency = _override_database(tmp_path)
    _register_and_get_access_token("template-dedupe")
    try:
        db = _db()
        try:
            from backend.app.models.prompt import PromptModule, PromptTemplate

            uid = _uid()
            template = db.query(PromptTemplate).filter_by(user_id=uid, task_type="image").first()
            modules = list(
                db.query(PromptModule).filter(
                    PromptModule.user_id == uid, PromptModule.id.in_(template.module_ids)
                )
            )
            assert modules, "种子模板必须引用到模块（否则本用例测的是空转）"

            out = detail_image_prompt.render_template_prompt(
                template=template, modules=modules, item={"title": "白橘运动包"}, skus=[]
            )
            # 每个已覆盖的约束**恰好一次**
            for fragment in ("统一光位", "保留商品真实材质", "禁止改变产品形状", "不要出现文字水印"):
                assert out.count(fragment) == 1, (
                    f"「{fragment}」在 prompt 里出现 {out.count(fragment)} 次 —— "
                    f"模板已覆盖时不应重复追加"
                )
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(db_dependency, None)


def test_template_prompt_backfills_uncovered_constraints(tmp_path):
    """模板**没覆盖**的约束仍要补上（判重不能变成「不补」）。

    与上一条是**成对**的：只守「不重复」会让「干脆不补」也变绿，
    那正好丢掉了「模板只管风格、常量管底线」的设计（没选模板时也要有硬要求）。
    """
    from backend.app.services import detail_image_prompt

    class _Sparse:
        prompt_skeleton = "只写商品名 {title}"

    out = detail_image_prompt.render_template_prompt(
        template=_Sparse(), modules=[], item={"title": "白橘运动包"}, skus=[]
    )
    assert "白橘运动包" in out
    # 未覆盖 ⇒ 必须补全（构图 / 一致性 / 合规都在）
    assert "竖版 3:4" in out, "模板没覆盖构图时未补上"
    assert "统一光位" in out, "模板没覆盖一致性时未补上"
    assert "不要出现文字水印" in out, "模板没覆盖合规时未补上"


# --------------------------------------------------------------------------- #
# 第三层：风格参考图（§13.7，借鉴 gcc-model-gen 的「风格继承」）
# --------------------------------------------------------------------------- #


def _seed_material_image(*, filename: str = "style.png", file_type: str = "image") -> int:
    """给当前用户播一个素材库文件夹 + 一个文件，返回文件 id。"""
    from backend.app.models.material import MaterialFile, MaterialFolder
    from backend.app.core.time import shanghai_now

    db = _db()
    try:
        folder = MaterialFolder(
            user_id=_uid(), name="风格库", path="D:/tmp/style-lib", file_count=1
        )
        db.add(folder)
        db.flush()
        row = MaterialFile(
            user_id=_uid(),
            folder_id=folder.id,
            filename=filename,
            file_type=file_type,
            rel_path=filename,
            size_bytes=1024,
            # ⚠️ `mtime` 是 **DateTime**（不是 unix 整数）—— 增量扫描判据，见模型注解
            mtime=shanghai_now(),
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def test_style_reference_survives_full_reference_list(tmp_path):
    """**商品图满 6 张时风格图仍要送达**（真机实测发现的缺陷）。

    背景：`fallback_refs = picked[:6]` 是「商品图 ≤6」，风格图**追加在末尾**
    成了第 7 张；而下游还会再切一次 `[:6]` —— 切掉的正好是**最后一张 = 风格图**
    ⇒ 风格图**永远送不到上游**，而所有「小数据」用例都照样绿（因为只勾一两张时
    前面还有空位，切不到它）。这就是「看起来是对的」的缺陷形态。

    所以本用例必须**勾满 6 张**再验风格图仍在 —— 少于 6 张测不出这个 bug。
    """
    from backend.app.api.ai import get_image_ai_client

    fake = _RecordingImageClient()
    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("style-full")
    try:
        app.dependency_overrides[get_image_ai_client] = lambda: fake
        _seed_image_provider()
        account_id = _seed_account_for_last_user()
        urls = [f"https://cdn.xhs.test/f{i}.jpg" for i in range(6)]
        _seed_shop_product(
            platform_account_id=account_id, item_id="ITEM-STYLE-5", images=urls
        )
        file_id = _seed_material_image(filename="style-full.png")

        response = TestClient(app).post(
            "/api/xhs/qianfan/generate-detail-image",
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-STYLE-5",
                "reference_image_urls": urls,
                "count": 3,
                "style_reference_file_id": file_id,
                "refine_prompt": False,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        assert fake.references, "没有发出图请求"
        sent = fake.references[0]
        assert len(sent) == 7, f"应 6 张商品图 + 1 张风格图 = 7，实得 {sent}"
        assert "materials/files" in sent[-1], (
            "风格图不在最后一位（或已被截断切掉）—— 6 张商品图时最容易发生"
        )
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_image_ai_client, None)


def test_style_reference_is_appended_and_named_in_prompt(tmp_path):
    """风格图被**追加到最后**，且 prompt 里**点名**它是风格参考。

    两点都要守：
    ① 图真的附上了（否则「借风格」无从谈起）；
    ② prompt 里说明了哪张是风格图 —— `reference_images` 是无序列表，
       不说清模型会把风格图当商品图（把样板里的东西画进商品）。
    """
    from backend.app.api.ai import get_image_ai_client

    fake = _RecordingImageClient()
    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("style-ref")
    try:
        app.dependency_overrides[get_image_ai_client] = lambda: fake
        _seed_image_provider()
        account_id = _seed_account_for_last_user()
        _seed_shop_product(
            platform_account_id=account_id,
            item_id="ITEM-STYLE-1",
            images=["https://cdn.xhs.test/s1.jpg"],
        )
        file_id = _seed_material_image()

        response = TestClient(app).post(
            "/api/xhs/qianfan/generate-detail-image",
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-STYLE-1",
                "reference_image_urls": ["https://cdn.xhs.test/s1.jpg"],
                "count": 3,
                "style_reference_file_id": file_id,
                "refine_prompt": False,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        assert fake.prompts, "没有发出图请求"
        sent = fake.prompts[0]
        assert "风格参考" in sent, "prompt 里没有点名风格参考图"
        # 上游收到的是「商品图 + 风格图」两张
        assert len(fake.references[0]) == 2, f"参考图应 2 张（商品+风格），实得 {fake.references[0]}"
        assert any("/materials/files/" in u for u in fake.references[0]), (
            "风格图（素材库文件 URL）没被附给上游"
        )
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_image_ai_client, None)


def test_style_reference_not_owned_returns_404_without_generating(tmp_path):
    """风格图不属于本用户 ⇒ 404 且**不发出图请求**（不静默忽略）。

    静默忽略的观感是「出图成功」，但用户挑的风格图根本没参与 ——
    与 §13.6.3「不静默回落」同一纪律。
    """
    from backend.app.api.ai import get_image_ai_client

    fake = _RecordingImageClient()
    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("style-owner")
    try:
        app.dependency_overrides[get_image_ai_client] = lambda: fake
        _seed_image_provider()
        account_id = _seed_account_for_last_user()
        _seed_shop_product(
            platform_account_id=account_id,
            item_id="ITEM-STYLE-2",
            images=["https://cdn.xhs.test/s2.jpg"],
        )

        response = TestClient(app).post(
            "/api/xhs/qianfan/generate-detail-image",
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-STYLE-2",
                "reference_image_urls": ["https://cdn.xhs.test/s2.jpg"],
                "style_reference_file_id": 999999,
                "refine_prompt": False,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 404, response.text
        assert response.json()["detail"]["code"] == "STYLE_REFERENCE_NOT_FOUND"
        assert not fake.prompts, "风格图不存在却仍然出图（静默忽略）"
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_image_ai_client, None)


def test_style_reference_must_be_image(tmp_path):
    """风格图必须是图片（视频文件 ⇒ 400，理由说清楚）。"""
    from backend.app.api.ai import get_image_ai_client

    fake = _RecordingImageClient()
    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("style-video")
    try:
        app.dependency_overrides[get_image_ai_client] = lambda: fake
        _seed_image_provider()
        account_id = _seed_account_for_last_user()
        _seed_shop_product(
            platform_account_id=account_id,
            item_id="ITEM-STYLE-3",
            images=["https://cdn.xhs.test/s3.jpg"],
        )
        file_id = _seed_material_image(filename="clip.mp4", file_type="video")

        response = TestClient(app).post(
            "/api/xhs/qianfan/generate-detail-image",
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-STYLE-3",
                "reference_image_urls": ["https://cdn.xhs.test/s3.jpg"],
                "style_reference_file_id": file_id,
                "refine_prompt": False,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "STYLE_REFERENCE_NOT_IMAGE"
        assert not fake.prompts
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_image_ai_client, None)


def test_style_reference_is_optional_zero_regression(tmp_path):
    """**不传风格图 ⇒ 行为与旧版完全一致**（可选增强，不是必填）。

    这是成对的另一半：只守「传了就生效」会让「变成必填」也变绿，
    而那会破坏既有调用方（旧请求没有这个字段）。
    """
    from backend.app.api.ai import get_image_ai_client

    fake = _RecordingImageClient()
    db_dependency = _override_database(tmp_path)
    token = _register_and_get_access_token("style-optional")
    try:
        app.dependency_overrides[get_image_ai_client] = lambda: fake
        _seed_image_provider()
        account_id = _seed_account_for_last_user()
        _seed_shop_product(
            platform_account_id=account_id,
            item_id="ITEM-STYLE-4",
            images=["https://cdn.xhs.test/s4.jpg"],
        )

        response = TestClient(app).post(
            "/api/xhs/qianfan/generate-detail-image",
            json={
                "platform_account_id": account_id,
                "item_id": "ITEM-STYLE-4",
                "reference_image_urls": ["https://cdn.xhs.test/s4.jpg"],
                "count": 3,
                "refine_prompt": False,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        assert len(fake.references[0]) == 1, "不传风格图时参考图应只有商品图那一张"
        assert "风格参考" not in fake.prompts[0], "不传风格图却出现了风格说明"
    finally:
        app.dependency_overrides.pop(db_dependency, None)
        app.dependency_overrides.pop(get_image_ai_client, None)
