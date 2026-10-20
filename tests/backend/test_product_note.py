"""商品笔记生成与护栏测试（M4 起步：T-401 / T-403 / T-405）。

设计真相源：`docs/02_设计文档/07_功能设计_发布链路.md` §12、`11_安全设计.md` §7。

这批用例盯的是**合规**，不是「模型写得好不好」：

1. **护栏必须拦得住**：广告法绝对化用语 / 违禁引流词 / 功效宣称 —— 三类各取样，**全部拦截**；
2. **价格一致性靠机械比对**（不是靠 Prompt 自觉）：正文写了区间外的价格就是编造，拦；
   区间内的价格（含「54.4 元」「144 元」边界）**不许误杀**；
3. **格式校验边界**：标题 20 字、正文 300 / 800 字、图片 1 / 18 张；
4. **被拦也回正文**：页面要能显示「为什么被拦」（静默丢弃 = 用户以为模型没输出）；
5. **不落库、不发布**：干运行端点不得写任何表。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.adapters.xhs.qianfan_api_adapter import get_qianfan_api_adapter_factory
from backend.app.api.ai import get_text_ai_client
from backend.app.api.platforms.xhs.qianfan import get_qianfan_api_adapter_factory as _qf_dep
from backend.app.core.database import Base, get_db
from backend.app.core.security import encrypt_text
from backend.app.main import app
from backend.app.models import AiDraft, PlatformAccount, PublishJob, Task, User
from backend.app.models.llm_gateway import LlmBusinessProvider, LlmGatewayConfig, LlmGatewayModel
class _ModelConfigStub:
    """`ModelConfig` 的鸭子类型替身（v2.0：ORM 类已随 model_configs 表退场）。

    服务层只按属性名读取（`user_id` / `provider_key` / `base_url` /
    `model_config_id`），v2.0 的真实载体是 `ResolvedModelContext`。
    """

    def __init__(self, *, user_id=1, model_type="text", provider="p", name="m",
                 base_url="", provider_key="", model_config_id=None, **kw):
        self.user_id = user_id
        self.model_type = model_type
        self.provider = provider
        self.name = name
        self.base_url = base_url
        self.provider_key = provider_key
        self.model_config_id = model_config_id
        for k, v in kw.items():
            setattr(self, k, v)


from backend.app.services.business_provider_keys import BUSINESS_KEY_LLM_NOTE_TEXT
from backend.app.services.product_note_service import (
    ABSOLUTE_TERMS,
    BANNED_TERMS,
    EFFICACY_TERMS,
    build_product_note_prompt,
    generate_product_note,
    product_payload,
    run_guardrails,
    validate_note_format,
)
from tests.backend.cookie_fixtures import bind_shop_account
from tests.backend.conftest import upsert_business_mapping

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[2]

#: 真实形态的商品（值来自千帆商品列表的字段口径；价格单位是**分**）。
PRODUCT = {
    "item_id": "ITEM-1",
    "title": "喵宠开心 化妆室浴室地垫 吸水防滑",
    "article_no": "A1001",
    "price_min_fen": 5440,
    "price_max_fen": 14400,
    "stock": 320,
    "sku_count": 4,
    "on_sale_sku_count": 4,
    "category": "家居 / 家纺 / 地垫",
    "sales_total": 128,
    "xsec_token": "SHOULD-NOT-LEAK",  # 不在白名单里 → 不许进 Prompt
}

#: 一段 320 字的合规正文（够 BODY_MIN）。
GOOD_BODY = (
    "浴室门口那块地垫，我之前买过两块，问题都出在同一处：踩上去打滑，洗完还干得慢。"
    "这块是长绒吸水款，绒面比较密，脚踩上去是软的那种，水吸进去以后不会立刻透到背面。"
    "背面是颗粒防滑层，湿地面也能咬住。颜色是奶白和浅灰两个方向，和浅色瓷砖不打架。"
    "尺寸我选的是 40*60CM，放在洗手台前面刚好，不挡门缝。清洗方式就是常规机洗，"
    "洗完平铺晾，第二天基本就干了。要说缺点也有：长绒面掉毛这件事前两次洗会有一点，"
    "后面就稳定了；另外颜色浅，深色鞋印要早点处理。整体属于「日常用得住」的那类，"
    "不是那种买回来只敢摆着看的。家里有老人小孩的话，防滑这一点比花色重要得多。"
    "补充几点实测：铺在淋浴区外侧比铺在正中间更实用，正中间反而容易积水；边缘做过收边，"
    "不会像便宜货那样卷起来；背面颗粒在瓷砖上不打滑，但木地板上会略微移动。"
    "价格上这款是区间定价，尺寸越大越贵，按需要的尺寸选就行，没必要为省一点买小一号。"
)

#: 样例正文必须符合设计里的「正文 300–800 字」，否则格式校验会把它自己拦掉。
assert 300 <= len(GOOD_BODY.strip()) <= 800, len(GOOD_BODY.strip())


def _completion(title: str, body: str, tags: list[str] | None = None) -> str:
    import json

    return json.dumps({"title": title, "body": body, "tags": tags or ["地垫", "浴室"]}, ensure_ascii=False)


class FakeTextClient:
    """按脚本返回（不联网）；记录调用次数，用来验「不落库 / 只调一次」。

    用**实例**注入（与 `test_api.py` 的 `FakeTextAiClient` 同形）：方法要拿 `self`，
    传类本身会变成未绑定调用而报「缺 self」。
    """

    payload = _completion("浴室地垫挑这块", GOOD_BODY)

    def __init__(self) -> None:
        self.calls = 0

    def generate_product_note(self, *, model_config, api_key, product, instruction="", platform="", function=""):
        self.calls += 1
        from backend.app.services.product_note_service import parse_generated_payload

        return parse_generated_payload(FakeTextClient.payload)


#: 单例：端点与服务测试共用同一实例，便于断言「调了几次」。
FAKE_TEXT = FakeTextClient()


class FakeItemsAdapter:
    """只读商品查询的假适配器（`search_items` 返回 1 条命中 + 1 条干扰）。"""

    calls = 0

    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def search_items(self, **kwargs):
        FakeItemsAdapter.calls += 1
        return True, "ok", {
            "code": 0,
            "success": True,
            "data": {
                "total": 2,
                "items": [
                    {"item_id": "OTHER", "item_name": "别的商品", "min_price": 100, "max_price": 200},
                    {
                        "item_id": "ITEM-1",
                        "item_name": PRODUCT["title"],
                        "article_no": PRODUCT["article_no"],
                        "min_price": PRODUCT["price_min_fen"],
                        "max_price": PRODUCT["price_max_fen"],
                        "total_stock": PRODUCT["stock"],
                        "sku_count": PRODUCT["sku_count"],
                        "on_sale_sku_count": PRODUCT["on_sale_sku_count"],
                        "categories": [{"name": "家居", "level": 1}, {"name": "地垫", "level": 3}],
                        "sale_qty_info": {"acc_sale_qty": PRODUCT["sales_total"]},
                        "images": [{"link": "http://qimg.example/a.jpg"}],
                        "xsec_token": PRODUCT["xsec_token"],
                    },
                ],
            },
        }


def _setup(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'product-note.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    FAKE_TEXT.calls = 0
    FakeItemsAdapter.calls = 0
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_text_ai_client] = lambda: FAKE_TEXT
    app.dependency_overrides[get_qianfan_api_adapter_factory] = lambda: FakeItemsAdapter
    app.dependency_overrides[_qf_dep] = lambda: FakeItemsAdapter

    response = client.post("/api/auth/register", json={"username": "owner", "password": "secret123"})
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    db = SessionLocal()
    try:
        user_id = db.scalar(select(User.id).where(User.username == "owner"))
        account = PlatformAccount(
            user_id=user_id, platform="xhs", sub_type="creator", nickname="观隅好物", status="active"
        )
        db.add(account)
        # v2.0（设计 17 §13.2）：业务调用改走「平台 × 业务点 → 服务商」映射，
        # 一期 `model_configs` 已退场。这里建一条**服务商 + 模型行 + 映射**，
        # 让「已配置」的路径与改造后的调用链一致（映射缺失 ⇒ 400 未配置）。
        db.add(
            LlmGatewayConfig(
                user_id=user_id,
                name="fake-provider",
                provider_key="FAKE-KEY1",
                base_url="http://127.0.0.1:9",
                # ⚠️ 2026-10-13：**必须给一个非空 key**。此前是 `""`，因为解析层不校验；
                # 现在「服务商没配 key」会在 `resolve_provider_context` 抛
                # `ProviderKeyMissingError`（结构化 400，修「没配 key 被报成 502」）
                # ⇒ 空 key 会让本文件的用例在**解析阶段**就 400，与它们要测的分支无关。
                # 本文件用的是假客户端（不打真实上游），key 给个占位值即可。
                access_token=encrypt_text("«redacted:sk-…»"),
                models_json='["fake-model"]',
                enabled=True,
                healthy=True,
            )
        )
        # ⚠️ v2.7/v2.8：`platform` / `model_type` / `default_model` 已从服务商层移除 ——
        # 能力类型与使用平台改由**模型行**承载（v2.7），兜底名整列删除（v2.8）。
        db.add(
            LlmGatewayModel(
                user_id=user_id,
                provider_key="FAKE-KEY1",
                model_name="fake-model",
                model_type="text",
                platform="xhs",
                enabled=True,
            )
        )
        upsert_business_mapping(
            db,
            user_id=user_id,
            business_key=BUSINESS_KEY_LLM_NOTE_TEXT.key,
            provider_key="FAKE-KEY1",
            # v2.3：业务入口必须显式带 model_name，且须在服务商目录内。
            model_name="fake-model",
        )
        db.commit()
        account_id = account.id
    finally:
        db.close()
    return headers, account_id, SessionLocal


def _teardown():
    app.dependency_overrides.clear()


def _bind(db_factory, account_id: int, user_id: int = 1) -> None:
    """把账号关联到店铺并给它一份 `ark` 令牌（= 走完 `POST /bind`）。

    2026-09-22：`shop_account_bindings` 整表移除；`_fetch_product` 的取票走
    `qianfan_cookies_for_account`（**账号自己的 ark 通道**），所以夹具必须真的
    写一份 ark 令牌 —— 旧夹具里的 `encrypted_cookies="a1=x"` 那种占位串在新链路下
    连通道都判不出来，测出来的会是「没票」而不是用例想测的东西。
    """
    db = db_factory()
    try:
        account = db.get(PlatformAccount, account_id)
        bind_shop_account(db, account, shop_name="观隅好物的店")
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# ① 护栏：三类词各取样，必须全拦
# --------------------------------------------------------------------------- #
def test_absolute_terms_are_all_blocked():
    """广告法绝对化用语：**词表里每一个**都要能被拦到（不是抽样）。"""
    for term in ABSOLUTE_TERMS:
        result = run_guardrails("标题", f"这块地垫{term}，值得买。", PRODUCT)
        assert not result.passed, f"{term} 未被拦截"
        assert term in {hit.term for hit in result.hits}


def test_banned_and_efficacy_terms_are_all_blocked():
    for term in BANNED_TERMS:
        result = run_guardrails("标题", f"需要的话{term}联系我。", PRODUCT)
        assert not result.passed, f"{term} 未被拦截"
    for term in EFFICACY_TERMS:
        result = run_guardrails("标题", f"这款能{term}，效果很好。", PRODUCT)
        assert not result.passed, f"{term} 未被拦截"
        hit = next(row for row in result.hits if row.term == term)
        assert hit.category == "功效宣称"


def test_law_compliant_copy_passes_guardrails():
    """反向断言：合规文案**不许**被误杀（只会拦不放过 = 功能不可用）。"""
    result = run_guardrails("浴室地垫挑这块", GOOD_BODY, PRODUCT)
    assert result.passed, f"合规文案被误拦：{result.as_dict()}"
    assert result.hits == []


def test_guardrail_hits_carry_position_and_reason():
    """位置必须是**字段内**下标、并标出命中的是标题还是正文。

    这条是「页面能不能正确高亮」的契约：早期实现把标题+正文拼起来再找，
    位置整体偏移了（标题多长就偏多少），页面高亮会错位。
    """
    body = "前面是正常描述，这里写了全网最低，后面还有内容。"
    result = run_guardrails("顶级地垫", body, PRODUCT)
    title_hit = next(row for row in result.hits if row.term == "顶级")
    body_hit = next(row for row in result.hits if row.term == "全网最低")
    assert (title_hit.field, title_hit.position) == ("title", 0)
    assert (body_hit.field, body_hit.position) == ("body", body.find("全网最低"))
    assert "《广告法》" in body_hit.reason
    # 标题命中排在正文命中之前（页面按此顺序展示）
    assert result.hits.index(title_hit) < result.hits.index(body_hit)


# --------------------------------------------------------------------------- #
# ② 价格一致性：机械比对，不误杀也不放过
# --------------------------------------------------------------------------- #
def test_price_inside_range_is_allowed():
    for text in ("只要 54.4 元", "顶配 144 元", "区间 60 元到 120 元"):
        result = run_guardrails("标题", text, PRODUCT)
        assert result.unsupported_prices == [], f"{text} 被误判为编造价格"


def test_price_outside_range_is_blocked_with_reason():
    result = run_guardrails("标题", "今天到手价只要 39 元，比平时便宜多了。", PRODUCT)
    assert not result.passed
    assert result.unsupported_prices
    assert "不在该区间" in result.price_note
    assert "54.40" in result.price_note and "144.00" in result.price_note


def test_missing_product_price_is_reported_not_guessed():
    """商品价格未知 → 不拦（缺值不推断），但把「这次没校价」写清楚。"""
    result = run_guardrails("标题", "到手价 39 元。", {"item_id": "X"})
    assert result.unsupported_prices == []
    assert "价格未知" in result.price_note


# --------------------------------------------------------------------------- #
# ③ 格式校验边界
# --------------------------------------------------------------------------- #
def test_format_boundaries():
    assert validate_note_format("一" * 20, "字" * 300, 18).ok
    assert not validate_note_format("一" * 21, "字" * 300, 1).ok
    assert not validate_note_format("标题", "字" * 299, 1).ok
    assert not validate_note_format("标题", "字" * 801, 1).ok
    assert not validate_note_format("标题", "字" * 400, 0).ok
    assert not validate_note_format("标题", "字" * 400, 19).ok
    assert not validate_note_format("", "字" * 400, 1).ok
    issues = validate_note_format("一" * 25, "字" * 200, 0).issues
    assert len(issues) == 3, issues


# --------------------------------------------------------------------------- #
# ④ Prompt：白名单与五段结构
# --------------------------------------------------------------------------- #
def test_prompt_uses_whitelisted_fields_only():
    prompt = build_product_note_prompt(PRODUCT, instruction="口语一点")
    assert PRODUCT["title"] in prompt
    assert "54.40" in prompt and "144.00" in prompt
    # 不该进 Prompt 的东西（xsec_token 等）——防「顺手整体透传」
    assert "SHOULD-NOT-LEAK" not in prompt
    assert "xsec_token" not in prompt
    # 五段结构与硬约束
    for marker in ("【商品信息】", "【输出结构】", "【要求】", "【禁止】", "【额外风格要求】"):
        assert marker in prompt, marker
    assert "≤20 字" in prompt or "20 字" in prompt


def test_product_payload_reports_price_text():
    payload = product_payload(PRODUCT)
    assert payload["price_text"] == "¥54.40~144.00"
    assert payload["price_min_fen"] == 5440


# --------------------------------------------------------------------------- #
# ⑤ generate_product_note：被拦也返回正文
# --------------------------------------------------------------------------- #
def test_generation_returns_body_even_when_blocked():
    FakeTextClient.payload = _completion("全网最低价地垫", GOOD_BODY)
    try:
        result = generate_product_note(
            client=FAKE_TEXT, model_config=None, api_key="", product=PRODUCT, image_count=1
        )
        assert result.ok is False
        assert result.body == GOOD_BODY.strip(), "被拦也必须回正文，页面要显示原因"
        assert "绝对化用语" in result.block_reason
        assert result.guardrails.hits
    finally:
        FakeTextClient.payload = _completion("浴室地垫挑这块", GOOD_BODY)


def test_generation_ok_path_reports_format_but_skips_images_when_unknown():
    result = generate_product_note(
        client=FAKE_TEXT, model_config=None, api_key="", product=PRODUCT, image_count=None
    )
    assert result.ok is True, result.block_reason
    assert result.format is not None and result.format.image_count == 0
    assert result.tags


# --------------------------------------------------------------------------- #
# ⑥ 端点：干运行（不落库、不发布）
# --------------------------------------------------------------------------- #
def test_endpoint_generates_and_does_not_write_any_table(tmp_path):
    headers, account_id, SessionLocal = _setup(tmp_path)
    try:
        _bind(SessionLocal, account_id)
        response = client.post(
            "/api/xhs/product-notes/generate",
            json={"platform_account_id": account_id, "item_id": "ITEM-1", "image_count": 1},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["ok"] is True, payload["block_reason"]
        assert payload["note"]["title"] == "浴室地垫挑这块"
        assert payload["note"]["body"] == GOOD_BODY.strip()
        assert payload["dry_run"] is True
        assert payload["product"]["item_id"] == "ITEM-1"
        assert payload["product"]["price_text"] == "¥54.40~144.00"
        assert "SHOULD-NOT-LEAK" not in response.text, "商品的敏感字段不许进响应"
        assert FakeItemsAdapter.calls == 1, "只该查一次商品（只读）"

        db = SessionLocal()
        try:
            assert db.scalars(select(AiDraft)).all() == [], "干运行不许写草稿"
            assert db.scalars(select(PublishJob)).all() == [], "干运行不许建发布任务"
            assert db.scalars(select(Task)).all() == [], "干运行不记任务（落库在 T-406）"
        finally:
            db.close()
    finally:
        _teardown()


def test_endpoint_blocks_but_still_returns_body(tmp_path):
    headers, account_id, SessionLocal = _setup(tmp_path)
    FakeTextClient.payload = _completion("全网最低价地垫", GOOD_BODY + "今天到手只要 39 元。")
    try:
        _bind(SessionLocal, account_id)
        response = client.post(
            "/api/xhs/product-notes/generate",
            json={"platform_account_id": account_id, "item_id": "ITEM-1", "image_count": 1},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["ok"] is False
        assert "绝对化用语" in payload["block_reason"]
        assert "编造价格" in payload["block_reason"] or payload["guardrails"]["unsupported_prices"]
        assert payload["note"]["body"], "被拦也要回正文"
        assert payload["guardrails"]["blocked_terms"]
    finally:
        FakeTextClient.payload = _completion("浴室地垫挑这块", GOOD_BODY)
        _teardown()


def test_endpoint_requires_binding_and_item(tmp_path):
    headers, account_id, SessionLocal = _setup(tmp_path)
    try:
        # 未绑定 → 400（没有凭据就取不到商品，不能凭空生成）
        response = client.post(
            "/api/xhs/product-notes/generate",
            json={"platform_account_id": account_id, "item_id": "ITEM-1"},
            headers=headers,
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "ARK_SESSION_REQUIRED"

        _bind(SessionLocal, account_id)
        # 商品不属于本店铺 → 404（「写错商品」是最严重的错误类型）
        response = client.post(
            "/api/xhs/product-notes/generate",
            json={"platform_account_id": account_id, "item_id": "NOT-MINE"},
            headers=headers,
        )
        assert response.status_code == 404, response.text
        assert response.json()["detail"]["code"] == "QIANFAN_ITEM_NOT_FOUND"
        assert FAKE_TEXT.calls == 0, "商品都没取到就不该调模型（省 token 也省事）"
    finally:
        _teardown()


def test_endpoint_reports_model_failure_instead_of_faking_copy(tmp_path):
    """模型不通（本机实测就是 401）→ 照实报错，**不许编一段正文**。"""
    headers, account_id, SessionLocal = _setup(tmp_path)

    class BrokenClient:
        def generate_product_note(self, **kwargs):
            raise ValueError("AI response content is empty")

    try:
        _bind(SessionLocal, account_id)
        app.dependency_overrides[get_text_ai_client] = lambda: BrokenClient()
        response = client.post(
            "/api/xhs/product-notes/generate",
            json={"platform_account_id": account_id, "item_id": "ITEM-1"},
            headers=headers,
        )
        assert response.status_code == 502, response.text
        assert response.json()["detail"]["code"] == "AI_GENERATION_FAILED"
    finally:
        _teardown()


def test_endpoint_maps_provider_http_error_to_readable_502(tmp_path):
    """模型服务返回 HTTP 错误（本机实测 401）→ 502 `AI_PROVIDER_ERROR` + 可读原因。

    为什么单列一条：不映射就会变成不透明的 500，把「凭据过期」伪装成「代码崩了」——
    这是最容易被当成 bug 去查代码的一类误报。
    """
    import requests

    headers, account_id, SessionLocal = _setup(tmp_path)

    class UnauthorizedClient:
        def generate_product_note(self, **kwargs):
            response = requests.Response()
            response.status_code = 401
            raise requests.HTTPError("401 Client Error: Authorization Required", response=response)

    try:
        _bind(SessionLocal, account_id)
        app.dependency_overrides[get_text_ai_client] = lambda: UnauthorizedClient()
        response = client.post(
            "/api/xhs/product-notes/generate",
            json={"platform_account_id": account_id, "item_id": "ITEM-1"},
            headers=headers,
        )
        assert response.status_code == 502, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "AI_PROVIDER_ERROR"
        assert "401" in detail["message"] and "API Key" in detail["message"]
    finally:
        _teardown()


def test_endpoint_requires_a_configured_text_model(tmp_path):
    headers, account_id, SessionLocal = _setup(tmp_path)
    try:
        _bind(SessionLocal, account_id)
        db = SessionLocal()
        try:
            # v2.0：判据是「业务点没配服务商」，故删映射（而非已退场的 model_configs）
            for row in db.scalars(select(LlmBusinessProvider)).all():
                db.delete(row)
            db.commit()
        finally:
            db.close()
        response = client.post(
            "/api/xhs/product-notes/generate",
            json={"platform_account_id": account_id, "item_id": "ITEM-1"},
            headers=headers,
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "AI_MODEL_NOT_CONFIGURED"
    finally:
        _teardown()
