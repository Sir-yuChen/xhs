"""商品「提交商品 → 发布到线上」的后端契约测试（设计 24）。

覆盖面（设计 24 §8）：

1. **单位**：发布体 `price` 是**分**（`yuan_to_fen`），与草稿体的「元」相反。
   变异：×1（当元用）⇒ 必须 CAUGHT。
2. **提交体形状**：`publishMode==1` / `options` 三键 / `priceInfo.preTax` 必填 /
   `skuData` 字段集 / 图片条目带 `uploaderInfoModel`。
3. **红线**：发布成功**必然**下架（`off_shelf_after` 默认 True）；变异：跳过下架 ⇒ CAUGHT。
4. **回执**：`failData[]` 逐条如实报，不静默。
5. **识别**：按唯一标题反查 item_id（**不是快照差集**）。
6. **端点契约**：无千帆令牌 ⇒ 400 `PUBLISH_ARK_REQUIRED`；归属校验 404。

⚠️ 全部**不碰真实平台**：SDK 用假适配器打桩。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from backend.app.core.security import create_access_token
from backend.app.models import PlatformAccount
from backend.app.services import shop_item_publish as pub
from backend.app.services import shop_item_publish_service as svc
from tests.backend.conftest import seed_owner_rows


#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分层按**实测耗时 + 语义**判定（`python .hermes/scripts/classify_tests.py --apply`）：
#:   redline     = 内循环必跑：整文件实测 <= 10s 且守护「不可接受后果」
#:   contract    = 契约层：端点 / 字段 / 行为与文档一致
#:   integration = 慢且重：整文件实测 > 30s，或真跑迁移 / 子进程
pytestmark = pytest.mark.integration


class _Ctx:
    def __init__(self, client, headers, ids, db_factory):
        self.client = client
        self.headers = headers
        self.ids = ids
        self.db_factory = db_factory


@pytest.fixture()
def ctx(api_client, db_factory):
    owner_id = 2
    seed_owner_rows(db_factory().get_bind(), user_id=owner_id, account_id=101)
    seed_owner_rows(db_factory().get_bind(), user_id=3, account_id=102)
    db = db_factory()
    try:
        mine = PlatformAccount(
            user_id=owner_id, platform="xhs", sub_type="creator",
            nickname="我的店", status="active", seller_id="seller-1",
        )
        other = PlatformAccount(
            user_id=3, platform="xhs", sub_type="creator",
            nickname="别人的店", status="active", seller_id="seller-2",
        )
        db.add_all([mine, other]); db.commit()
        ids = {"mine": mine.id, "other": other.id, "owner_id": owner_id}
    finally:
        db.close()
    headers = {"Authorization": f"Bearer {create_access_token(owner_id)}"}
    return _Ctx(api_client, headers, ids, db_factory)


# --------------------------------------------------------------------------- #
# 1. 单位：发布体的 price 是「分」
# --------------------------------------------------------------------------- #

def test_price_is_fen_in_publish_sku():
    """⚠️ 核心：页面 1280 元 ⇒ 发布体 `price.price == 128000`（分）。

    变异：把 `yuan_to_fen` 改成恒等（×1）⇒ 本用例必红。
    """
    sku = pub.build_publish_sku(
        row_number=1, price_yuan=1280, stock=200,
        logistics_plan_id="lp-1", variants=[],
    )
    assert sku["price"]["price"] == 128000
    assert sku["price"]["priceInfo"]["preTax"] == 128000


def test_yuan_to_fen_rounds_and_missing():
    assert pub.yuan_to_fen(12.8) == 1280
    assert pub.yuan_to_fen(0.1) == 10
    #: 缺值 ⇒ None（**不填 0** —— 0 元会被平台当真价格收下）
    assert pub.yuan_to_fen(None) is None


def test_publish_sku_requires_pretax():
    """`priceInfo.preTax` 必填 —— 缺了平台报「售价计算失败: 商品价格未存在」。"""
    sku = pub.build_publish_sku(
        row_number=1, price_yuan=99, stock=1, logistics_plan_id="lp", variants=[],
    )
    assert "preTax" in sku["price"]["priceInfo"]
    #: ⚠️ stockChange = 本次库存变化量（真机体 = 库存数，不是 0）。
    assert sku["stockChange"] == 1, "stockChange 应是库存变化量（真机=库存数），不是 0"


# --------------------------------------------------------------------------- #
# 2. 提交体形状
# --------------------------------------------------------------------------- #

def test_publish_mode_is_normal_and_options_complete():
    payload = pub.build_publish_payload([{"name": "x"}])
    assert payload["publishMode"] == 1
    assert payload["options"] == {"asyncExec": True, "withChild": True, "retrieveResult": True}


def test_item_body_forces_platform_defaults_and_spec():
    body = pub.build_item_body(
        skeleton={"logisticsPlanId": "lp", "shippingTemplateId": "st"},
        name="标题", category_id="cat-1", attributes=None,
        main_images=[{"key": "a"}], detail_images=[],
        variant_ids=["v-1"], skus=[{"rowNumber": 1}],
        variant_sequence=[{"variantId": "v-1"}],
    )
    assert body["categoryId"] == "cat-1"
    assert body["name"] == "标题"
    assert body["mainSpecEnable"] == 1 and body["mainSpecId"] == "v-1"
    #: ⚠️ `deliveryMode` 是**物流类型**不是发货模式：物流配送 = 0（真机 radio value="0"）。
    #: 旧断言写 1（当时的错误理解），会被平台判成「无物流发货」而报错。
    assert body["stockType"] == 1 and body["deliveryMode"] == 0 and body["source"] == 2
    #: ⚠️ brandId 必带（真机体 "-999"=无品牌）；被 _DROP_FROM_SKELETON 剥掉后必须补回。
    assert body["brandId"] == "-999", "brandId 缺失会让平台判无品牌属性"
    assert body["images"] == [{"key": "a"}]
    assert body["variantIds"] == ["v-1"]


def test_image_entry_has_uploader_model():
    entry = pub.build_publish_image({
        "materialId": "m-1", "link": "https://qimg/x",
        "url": "https://ros-preview.xhscdn.com/arkgoods/f-1?sign=abc",
        "path": "arkgoods/f-1",
        "extension": ".jpeg", "width": 800, "height": 800, "size": 100, "name": "n",
    })
    #: 2026-10-13 真机抓包重写：type=uploadImage（无 key），isSecret=true，
    #: path/fileId 用短格式 fileId（从带签名 url 提取）。
    assert entry["type"] == "uploadImage" and "key" not in entry
    assert entry["uploaderInfoModel"]["scene"] == "goods_image"
    assert entry["uploaderInfoModel"]["isSecret"] is True
    assert entry["path"] == "arkgoods/f-1"
    assert entry["uploaderInfoModel"]["fileId"] == "arkgoods/f-1"
    assert entry["uploaderInfoModel"]["url"] == "https://ros-preview.xhscdn.com/arkgoods/f-1?sign=abc"


def test_image_entry_omits_missing_dimensions():
    """尺寸拿不到 ⇒ 整个键不发（设计 22 `For input string: ""` 教训）。"""
    entry = pub.build_publish_image({"materialId": "m", "link": "l", "path": "p",
                                     "extension": ".jpeg", "name": "n"})
    assert "width" not in entry and "height" not in entry


def test_variant_sequence_dedupes_values():
    seq = pub.build_variant_sequence([
        {"variantId": "v1", "variantName": "颜色分类", "variantValue": "A"},
        {"variantId": "v1", "variantName": "颜色分类", "variantValue": "A"},
        {"variantId": "v1", "variantName": "颜色分类", "variantValue": "B"},
    ])
    assert len(seq) == 1 and seq[0]["variantValues"] == ["A", "B"]


# --------------------------------------------------------------------------- #
# 3. 回执解析（failData 不静默）
# --------------------------------------------------------------------------- #

def test_parse_task_result_reports_fails():
    payload = {"data": {"task": {
        "status": 3, "result": {"success": False,
                                "extraJson": '{"failData":[{"index":1,"name":"标题","message":"不能为空"}],"itemIds":[]}'},
    }}}
    final, success, fails = pub.parse_task_result(payload)
    assert final is True and success is False
    assert fails and fails[0]["message"] == "不能为空"


def test_parse_task_result_success_ignores_result_success_field():
    """⚠️ 成功判据**不是** `result.success`（真机成功时它是 false）——
    而是 `failData` 空 + `itemIds` 非空。"""
    payload = {"data": {"task": {
        "status": 3,
        "result": {"success": False,
                   "extraJson": '{"failData":[],"itemIds":["ITEM-X"]}'},
    }}}
    final, success, fails = pub.parse_task_result(payload)
    assert final is True and success is True and fails == []
    assert pub.parse_item_ids(payload) == ["ITEM-X"]


def test_parse_task_result_not_final():
    final, _s, _f = pub.parse_task_result({"data": {"task": {"status": 1}}})
    assert final is False


# --------------------------------------------------------------------------- #
# 4. 红线：发布成功必然下架
# --------------------------------------------------------------------------- #

class _FakeApi:
    """记录器：模拟提交 → 轮询成功 → 回查 → 下架。"""

    def __init__(self, *, publish_success=True):
        self.publish_success = publish_success
        self.calls: list[str] = []
        self.off_shelf_calls: list[tuple[list[str], bool]] = []
        self.submit_payload: dict[str, Any] | None = None

    def search_items(self, **kwargs):
        """服务层 2026-10-10 起调 `search_items`（SDK/adapter 都有此方法，`list_recent_items` 只有 adapter 有）。"""
        if kwargs.get("keyword"):
            return True, "", {"data": {"item_list": [
                {"item_id": "ITEM-NEW", "item_name": kwargs["keyword"]}]}}
        return True, "", {"data": {"item_list": [{"item_id": "SKEL-ITEM"}]}}

    def list_recent_items(self, **kwargs):
        #: adapter 别名（旧调用点兼容）
        return self.search_items(**kwargs)

    def publish_render(self, params):
        """真机 2026-10-09：**编辑读回**（publishType=2）给完整商品骨架；
        **空白新增表单**（publishType=1）的 `fulfillmentInfo` **只有**
        `logisticsPlanList` —— 既无 `shippingTemplateList` 也无 `defaultShippingTemplate`。"""
        import json
        if params.get("publishType") == 2:
            return True, "", {"data": json.dumps({"product": {"productDetail": {"item": {
                "categoryId": "cat", "itemName": "old", "itemId": "SKEL-ITEM",
                "shippingTemplateId": "st-skel"}}}})}
        return True, "", {"data": json.dumps({"fulfillmentInfo": {
            "logisticsPlanList": [{"planId": "lp-blank", "name": "发货仓", "logisticsMode": "内贸"}],
            "logisticsPlanStatus": 1}})}

    def get_seller_carriage_template(self):
        """运费模板**只在**这个接口里（不在 `publish_render`）—— 真机 2026-10-09。"""
        import json
        return True, "", {"data": json.dumps({"fulfillmentInfo": {
            "shippingTemplateList": [
                {"templateId": "st-carriage", "templateName": "系统默认-全国包邮模版"}],
            "defaultShippingTemplate": {
                "templateId": "st-carriage", "templateName": "系统默认-全国包邮模版"}}})}

    def submit_item(self, payload):
        self.calls.append("submit")
        self.submit_payload = payload
        return True, "", {"data": {"taskId": "T-1"}}

    def query_publish_task(self, task_id):
        #: 下架任务回**操作类**回执（无 failData/itemIds）—— 与真机一致，
        #: 这样「下架判据用错」的变异会被抓住。
        if task_id == "T-OFF":
            return True, "", {"data": {"task": {"status": 3, "result": {
                "success": False, "extraJson": '{"failCount":0,"successCount":1,"total":1}'}}}}
        if self.publish_success:
            extra = '{"failData":[],"itemIds":["ITEM-NEW"]}'
        else:
            extra = '{"failData":[{"index":1,"name":"x","message":"拒"}],"itemIds":[]}'
        return True, "", {"data": {"task": {"status": 3, "result": {"success": False,
                                                                    "extraJson": extra}}}}

    def set_items_availability(self, item_ids, *, available):
        self.calls.append("off_shelf")
        self.off_shelf_calls.append((list(item_ids), available))
        return True, "", {"data": {"taskId": "T-OFF"}}
    def upload_web_permit(self, *a, **k):
        raise AssertionError("测试不应真实上传")


def test_publish_success_takes_down_immediately(monkeypatch):
    """⚠️ 红线：发布成功**必然**下架（`off_shelf_after` 默认 True）。"""
    monkeypatch.setattr(svc, "_PUBLISH_POLL_INTERVAL", 0)
    api = _FakeApi(publish_success=True)
    result = svc.publish_item(
        api=api, name="唯一标题ABC", category_id="cat",
        uploaded={}, main_image_paths=[], detail_image_paths=[],
        skus=[{"name": "颜色分类:A", "price_yuan": 12, "stock": 1}],
        variant_ids=["v1"],
    )
    assert result["published"] is True
    assert "off_shelf" in api.calls, "发布成功必须下架（红线）"
    assert api.off_shelf_calls[0] == (["ITEM-NEW"], False)
    #: ⚠️ 下架任务回执是 `{"failCount":0,…}`（无 failData/itemIds）⇒ 必须判成成功。
    assert result["off_shelf"] is True
    assert result["item_id"] == "ITEM-NEW"


def test_off_shelf_after_flag_really_controls_the_off_shelf_call(monkeypatch):
    """**`off_shelf_after=False` 必须真的不调下架接口**（2026-10-11 用户取消红线后新增）。

    ⚠️ 这条此前**零覆盖** —— 整仓只有「失败不下架」的用例，没有「成功 + 明确要求
       不下架」的用例。用户实测点「发布商品」时看到的行为仍像旧版（上架后立刻下架），
       排查时只能靠临时探针手测；这条把这个判据固化下来。

    判据（**数下架接口的调用次数**，不是看返回值 —— 返回值可以被别的分支写成 False）：
      · `off_shelf_after=False` ⇒ `set_items_availability` 调用 **0** 次；
      · `off_shelf_after=True`  ⇒ 调用 **1** 次。
    """

    class _FakeApi:
        def __init__(self) -> None:
            self.off_calls = 0

        def submit_item(self, body):  # noqa: ARG002
            return True, "ok", {"data": {"taskId": "T1"}}

        def set_items_availability(self, ids, available=True):  # noqa: ARG002
            self.off_calls += 1
            return True, "ok", {"data": {"taskId": "T2"}}

        def publish_render(self, *a, **k):  # noqa: ARG002
            return False, "x", None

        def get_seller_carriage_template(self):
            return False, "x", None

    from backend.app.services import shop_item_publish_service as svc

    monkeypatch.setattr(svc, "load_item_skeleton", lambda *a, **k: {"itemId": "S1"})
    monkeypatch.setattr(svc, "build_item_from_request", lambda **k: {"skuData": []})
    monkeypatch.setattr(svc.pub, "build_publish_payload", lambda x: {"itemData": x})
    #: 发布成功 + 拿到一个 itemId（下架分支的必要输入）。
    monkeypatch.setattr(svc, "_poll_task", lambda *a, **k: (True, [], ["ITEM-1"]))

    #: ① 发布商品（不下架）⇒ 一次都别调。
    api_no = _FakeApi()
    res_no = svc.publish_item(
        api=api_no, name="T", category_id="C1", uploaded={},
        main_image_paths=[], detail_image_paths=[], skus=[], variant_ids=[],
        off_shelf_after=False,
    )
    assert api_no.off_calls == 0, "要求不下架却仍调用了下架接口（红线已取消，这是纯 bug）"
    assert res_no["off_shelf"] is False, "不下架时 `off_shelf` 应为 False"
    assert res_no["published"] is True, "`off_shelf=False` 不影响发布成功"

    #: ② 发布并下架 ⇒ 调一次（守住老路径没被改坏）。
    api_yes = _FakeApi()
    res_yes = svc.publish_item(
        api=api_yes, name="T", category_id="C1", uploaded={},
        main_image_paths=[], detail_image_paths=[], skus=[], variant_ids=[],
        off_shelf_after=True,
    )
    assert api_yes.off_calls == 1, "要求下架却没调下架接口"
    assert res_yes["off_shelf"] is True, "下架成功时 `off_shelf` 应为 True"


def test_publish_failure_does_not_off_shelf(monkeypatch):
    """发布失败 ⇒ **不下架**（没有商品可下架）+ 逐条报失败原因，不静默。"""
    monkeypatch.setattr(svc, "_PUBLISH_POLL_INTERVAL", 0)
    api = _FakeApi(publish_success=False)
    result = svc.publish_item(
        api=api, name="唯一标题ABC", category_id="cat",
        uploaded={}, main_image_paths=[], detail_image_paths=[],
        skus=[{"name": "颜色分类:A", "price_yuan": 12, "stock": 1}],
        variant_ids=["v1"],
    )
    assert result["published"] is False
    assert "off_shelf" not in api.calls
    assert result["fail_reasons"] and result["fail_reasons"][0]["message"] == "拒"


def test_publish_no_skeleton_raises(monkeypatch):
    """本店无既有商品可作骨架 ⇒ 结构化错误（**不猜骨架**）。"""
    api = _FakeApi()
    def _empty(**k):
        return True, "", {"data": {"item_list": []}}
    api.search_items = _empty
    with pytest.raises(svc.PublishError) as exc:
        svc.publish_item(api=api, name="x", category_id="cat", uploaded={},
                         main_image_paths=[], detail_image_paths=[],
                         skus=[], variant_ids=[])
    assert exc.value.code == "PUBLISH_SKELETON_REQUIRED"


def test_find_item_id_uses_title_not_diff():
    """按**唯一标题精确反查**（不是快照差集 —— 曾误删真实商品）。"""
    api = _FakeApi()
    assert svc._find_item_id_by_title(api, "唯一标题ABC") == "ITEM-NEW"
    #: 标题为空 ⇒ 不匹配任何行（宁可不删，也不误删）
    assert svc._find_item_id_by_title(api, "") == ""


# --------------------------------------------------------------------------- #
# 4b. 店级物流方案 / 运费模板（DEF-100：两个值来自两个接口）
# --------------------------------------------------------------------------- #

def test_parse_fulfillment_info_needs_carriage_source():
    """⚠️ DEF-100：`shippingTemplateId` **不在** `publish_render` 里 —— 必须单独传
    `getSellerCarriageTemplate` 回执，否则运费模板恒空。

    变异：把 `local` 的 `carriage_payload` 换成 `None`（= 旧实现只喂 precheck）⇒
    `shipping_template_id` 变空串 ⇒ 本用例必红。
    """
    import json
    blank = {"data": json.dumps({"fulfillmentInfo": {
        "logisticsPlanList": [{"planId": "lp-1"}], "logisticsPlanStatus": 1}})}
    carriage = {"data": json.dumps({"fulfillmentInfo": {
        "shippingTemplateList": [{"templateId": "st-9", "templateName": "全国包邮"}],
        "defaultShippingTemplate": {"templateId": "st-9"}}})}

    local = pub.parse_fulfillment_info(blank, carriage)
    assert local["logistics_plan_id"] == "lp-1"
    assert local["shipping_template_id"] == "st-9"

    #: 只喂 blank（旧实现行为）= 运费模板恒空 —— 这正是被修的缺陷。
    legacy = pub.parse_fulfillment_info(blank)
    assert legacy["logistics_plan_id"] == "lp-1"
    assert legacy["shipping_template_id"] == ""


def test_parse_fulfillment_info_accepts_edit_readback_template():
    """编辑读回（`publishType:2`）的 `publish_render` **会**带 `shippingTemplateList`
    ⇒ 不传 carriage 时也应能从 precheck 里取到（两个来源都认）。"""
    import json
    edit = {"data": json.dumps({"fulfillmentInfo": {
        "logisticsPlanList": [{"planId": "lp-2"}],
        "shippingTemplateList": [{"templateId": "st-edit", "templateName": "编辑读回"}]}})}
    out = pub.parse_fulfillment_info(edit)
    assert out["shipping_template_id"] == "st-edit"


def test_publish_item_fills_shipping_template_from_carriage(monkeypatch):
    """⚠️ 回归守卫：`publish_item` 组出的发布体**必须**带
    `shippingTemplateId`（来自 carriage）+ `logisticsPlanId`（来自空白 publish_render）。

    变异：`publish_item` 删掉 `get_seller_carriage_template()` 那次调用 ⇒
    `shippingTemplateId` 从请求体消失 ⇒ 本用例必红。
    """
    monkeypatch.setattr(svc, "_PUBLISH_POLL_INTERVAL", 0)
    api = _FakeApi(publish_success=True)
    svc.publish_item(
        api=api, name="唯一标题ABC", category_id="cat",
        uploaded={}, main_image_paths=[], detail_image_paths=[],
        skus=[{"name": "颜色分类:A", "price_yuan": 12, "stock": 1}],
        variant_ids=["v1"],
    )
    body = api.submit_payload["itemData"][0]
    assert body["shippingTemplateId"] == "st-carriage", (
        "运费模板必须来自 getSellerCarriageTemplate（publish_render 不返回它）"
    )
    assert body["logisticsPlanId"] == "lp-blank", (
        "物流方案（发货仓）来自空白 publish_render 的 logisticsPlanList"
    )
    #: SKU 行也要带上物流方案 id。
    assert body["skuData"][0]["logisticsPlanId"] == "lp-blank"


# --------------------------------------------------------------------------- #
# 5. 端点契约
# --------------------------------------------------------------------------- #

def test_publish_endpoint_requires_ark(ctx):
    resp = ctx.client.post(
        "/api/xhs/qianfan/items/publish",
        json={"platform_account_id": ctx.ids["mine"], "category_id": "c",
              "name": "标题", "skus": [{"name": "颜色分类:A", "price_yuan": 1}]},
        headers=ctx.headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "PUBLISH_ARK_REQUIRED"


def test_publish_endpoint_rejects_other_account(ctx):
    resp = ctx.client.post(
        "/api/xhs/qianfan/items/publish",
        json={"platform_account_id": ctx.ids["other"], "category_id": "c",
              "name": "标题", "skus": [{"name": "颜色分类:A", "price_yuan": 1}]},
        headers=ctx.headers,
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# 5b. 预检暂存 → 绝对路径 的桥接（设计 25 §6）
# --------------------------------------------------------------------------- #

def test_publish_rejects_unknown_precheck_token(ctx):
    """不存在的 token ⇒ 404 `PRECHECK_TOKEN_INVALID`（不是 500）。

    ⚠️ 归属校验在 `resolve_staged(user_id, token)` 里 —— 别人的 token 与
    过期 token 都是同一个结果，**不区分**（「过期或不属于当前账号」），
    免得 404/403 的差别泄露别人的 token 是否存在。
    """
    resp = ctx.client.post(
        "/api/xhs/qianfan/items/publish",
        json={"platform_account_id": ctx.ids["mine"], "category_id": "c",
              "name": "标题", "skus": [{"name": "颜色分类:A", "price_yuan": 1}],
              "precheck_token": "not-a-real-token"},
        headers=ctx.headers,
    )
    #: 门禁顺序：ARK 令牌先于 token 校验，所以没 ark 时先撞 400。
    assert resp.status_code in (400, 404)
    if resp.status_code == 404:
        assert resp.json()["detail"]["code"] == "PRECHECK_TOKEN_INVALID"


def test_precheck_token_defaults_to_empty_so_drafts_still_work(ctx):
    """不传 token ⇒ 空串（草稿箱发布走旧路径，不会被新字段卡住）。"""
    from backend.app.api.platforms.xhs.qianfan import ItemPublishRequest

    payload = ItemPublishRequest(
        platform_account_id=1, category_id="c", name="标题",
        skus=[{"name": "颜色分类:A", "price_yuan": 1}],
    )
    assert payload.precheck_token == ""


def test_publish_request_forbids_extra_fields(ctx):
    """`extra="forbid"`：未知字段 ⇒ 422（守住契约形状）。"""
    resp = ctx.client.post(
        "/api/xhs/qianfan/items/publish",
        json={"platform_account_id": ctx.ids["mine"], "category_id": "c",
              "name": "标题", "skus": [{"name": "颜色分类:A", "price_yuan": 1}],
              "unknown_field": 1},
        headers=ctx.headers,
    )
    assert resp.status_code == 422


def test_package_path_defaults_to_empty(ctx):
    """不传 package_path ⇒ 空串（草稿箱发布走旧路径，不被新字段卡住）。"""
    from backend.app.api.platforms.xhs.qianfan import ItemPublishRequest, ItemDraftCreateRequest

    pub = ItemPublishRequest(
        platform_account_id=1, category_id="c", name="标题",
        skus=[{"name": "颜色分类:A", "price_yuan": 1}],
    )
    assert pub.package_path == ""

    draft = ItemDraftCreateRequest(
        platform_account_id=1, category_id="c", name="标题",
        skus=[{"name": "颜色分类:A", "price_yuan": 1}],
    )
    assert draft.package_path == ""


def test_package_path_takes_priority_over_precheck_token(ctx):
    """**优先级守卫**：`package_path` 分支必须在 `precheck_token` 分支**之前**。

    ⚠️ 根因（2026-10-10）：旧实现只认 `precheck_token`（内存引用 `_LOCAL_REFS`，
    后端重启即失效）。修复后 `package_path`（本地文件夹绝对路径）应**优先**，
    token 仅兜底。变异「把两个分支顺序调反」⇒ 本守卫必红。
    """
    import re
    from pathlib import Path

    from tests.backend.conftest import read_qianfan_source

    src = read_qianfan_source()

    #: 两个端点（发布 + 保存草稿）都必须有「package_path 优先」的判据。
    #: 判据 = `if payload.package_path.strip():` 出现且在其后才是 `elif payload.precheck_token:`。
    package_if = re.findall(r"if payload\.package_path\.strip\(\):", src)
    precheck_elif = re.findall(r"elif payload\.precheck_token:", src)
    assert len(package_if) >= 2, "发布 + 保存草稿两处都应有 package_path 优先分支"
    assert len(precheck_elif) >= 2, "precheck_token 应退为 elif（兜底）"

    #: 关键：`package_path` 的 `if` 必须出现在 `precheck_token` 的 `elif` 之前
    #: （同一个 image_root 解析块里）。逐块校验：每个 `package_path` 判定行
    #: 后面跟着的是 `elif payload.precheck_token`（而不是反过来）。
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if "if payload.package_path.strip():" in line:
            # 向后找最近的 precheck 判定（package_path 分支内有 is_dir 校验 + 404 抛出，
            # 约 10 行后才到 elif precheck_token，窗口放宽到 20 行）。
            window = "\n".join(lines[i:i + 20])
            assert "elif payload.precheck_token:" in window, (
                "package_path 分支后应紧跟 elif precheck_token 兜底"
            )


def test_publish_sku_accepts_market_price_yuan(ctx):
    """⚠️ 2026-10-10：前端 `skus` state 带 `market_price_yuan`（划线价），
    但 `ItemDraftSku` 未声明该字段 ⇒ `extra="forbid"` 报
    `market_price_yuan: Extra inputs are not permitted`（发布直接 422）。
    修复：声明该字段；本条守住它**接受** `market_price_yuan` 不再 422。
    """
    from backend.app.api.platforms.xhs.qianfan import ItemDraftSku
    fields = ItemDraftSku.model_fields
    assert "market_price_yuan" in fields, "ItemDraftSku 应声明 market_price_yuan（划线价）"
    #: 序列化不应报错（round-trip 能带上划线价）
    sku = ItemDraftSku(name="颜色分类:A", price_yuan=1, market_price_yuan=2)
    assert sku.model_dump()["market_price_yuan"] == 2


def test_draft_delete_rejects_other_account(ctx):
    resp = ctx.client.post(
        "/api/xhs/qianfan/item-drafts/delete",
        json={"platform_account_id": ctx.ids["other"], "draft_ids": ["d-1"]},
        headers=ctx.headers,
    )
    assert resp.status_code == 404


def test_draft_delete_rejects_empty_ids(ctx):
    """空 `draft_ids` ⇒ 422（min_length=1）。"""
    resp = ctx.client.post(
        "/api/xhs/qianfan/item-drafts/delete",
        json={"platform_account_id": ctx.ids["mine"], "draft_ids": []},
        headers=ctx.headers,
    )
    assert resp.status_code == 422


def test_sdk_draft_delete_body_shape(monkeypatch):
    """SDK `batch_delete_draft` 的 body 形状（真机 bundle 取证）。"""
    from apis.xhs_qianfan_apis import XHS_Qianfan_Apis

    captured = {}

    def fake_signed_post(self, path, body, referer):
        captured["path"] = path
        captured["body"] = body
        return True, "ok", {"code": 0}

    monkeypatch.setattr(XHS_Qianfan_Apis, "_signed_post", fake_signed_post)
    api = XHS_Qianfan_Apis("a1=x")
    ok, _msg, _resp = api.batch_delete_draft(["d-1", "d-2"])
    assert ok is True
    assert captured["path"] == "/api/edith/product/batch_delete_draft"
    assert captured["body"] == {"productDraftIds": ["d-1", "d-2"], "channel": 2}


def test_sdk_draft_delete_empty_is_rejected(monkeypatch):
    from apis.xhs_qianfan_apis import XHS_Qianfan_Apis

    called = {"n": 0}

    def fake_signed_post(self, path, body, referer):
        called["n"] += 1
        return True, "ok", {}

    monkeypatch.setattr(XHS_Qianfan_Apis, "_signed_post", fake_signed_post)
    api = XHS_Qianfan_Apis("a1=x")
    ok, msg, _ = api.batch_delete_draft([])
    assert ok is False and called["n"] == 0


def test_shipping_templates_endpoint_does_not_crash_when_no_credentials(ctx):
    """**DEF-099 前身**：`shipping-templates` 端点曾调两个不存在的函数
    （`_qianfan_api_for` / `_assert_account_owned`）⇒ 一调就 NameError 500，
    前端拿到「请求失败，请稍后重试」。修复后走 `_ark_adapter_for`，
    无千帆凭据时应返回**结构化 400**（`ARK_SESSION_REQUIRED`），不是 500。
    """
    resp = ctx.client.post(
        "/api/xhs/qianfan/product/shipping-templates",
        json={"account_id": ctx.ids["mine"]},
        headers=ctx.headers,
    )
    assert resp.status_code == 400, f"expected 400 (no creds), got {resp.status_code}"
    detail = resp.json().get("detail")
    assert isinstance(detail, dict) and detail.get("code") == "ARK_SESSION_REQUIRED"


def test_predict_properties_endpoint_does_not_crash_when_no_credentials(ctx):
    """同上：`predict-properties` 端点也曾调不存在的函数 ⇒ 500。修复后无凭据
    返回结构化 400，不是 NameError。"""
    resp = ctx.client.post(
        "/api/xhs/qianfan/product/predict-properties",
        json={"account_id": ctx.ids["mine"], "category_id": "c", "title": "t"},
        headers=ctx.headers,
    )
    assert resp.status_code == 400, f"expected 400 (no creds), got {resp.status_code}"
    detail = resp.json().get("detail")
    assert isinstance(detail, dict) and detail.get("code") == "ARK_SESSION_REQUIRED"


def test_shipping_templates_rejects_other_account(ctx):
    """归属校验：拿 A 的账号 id 用 B 的 token 请求 ⇒ 404（不是越权返回数据）。"""
    resp = ctx.client.post(
        "/api/xhs/qianfan/product/shipping-templates",
        json={"account_id": ctx.ids["other"]},
        headers=ctx.headers,
    )
    assert resp.status_code == 404


def test_shipping_templates_parses_fulfillment_info(ctx, monkeypatch):
    """**数据路径守卫**（DEF-099）：平台把物流模板放进 `data.fulfillmentInfo`
    （不是 `data` 顶层）。改回顶层取 ⇒ 本用例必红（templates 恒空）。

    真机 2026-10-09 实测：`getSellerCarriageTemplate` 返回
    `data.fulfillmentInfo.shippingTemplateList` + `.defaultShippingTemplate`，
    顶层 `data.shippingTemplateList` 是**不存在**的键。
    """
    from backend.app.services import shop_binding_service as sbs
    from backend.app.adapters.xhs.qianfan_api_adapter import (
        get_qianfan_api_adapter_factory,
    )
    from backend.app.main import app

    class _FakeAdapter:
        def get_seller_carriage_template(self):
            return True, "ok", {
                "data": {
                    "fulfillmentInfo": {
                        "shippingTemplateList": [
                            {"templateId": "t1", "templateName": "默认包邮"},
                            {"templateId": "t2", "templateName": "全国包邮"},
                        ],
                        "defaultShippingTemplate": {"templateId": "t1", "templateName": "默认包邮"},
                    }
                }
            }

    #: `_ark_adapter_for` 在函数内 `from shop_binding_service import qianfan_cookies_for_account`
    #: ⇒ 打桩必须落在 `shop_binding_service` 模块，不是 qianfan 模块。
    monkeypatch.setattr(sbs, "qianfan_cookies_for_account", lambda db, aid: "a=1")
    #: 工厂是 `Depends(...)` 注入，路由注册时绑定了原始函数对象 ⇒ 必须走
    #: `dependency_overrides`（monkeypatch 模块属性不生效）。
    app.dependency_overrides[get_qianfan_api_adapter_factory] = lambda: lambda c: _FakeAdapter()
    try:
        resp = ctx.client.post(
            "/api/xhs/qianfan/product/shipping-templates",
            json={"account_id": ctx.ids["mine"]},
            headers=ctx.headers,
        )
    finally:
        app.dependency_overrides.pop(get_qianfan_api_adapter_factory, None)

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert len(body["templates"]) == 2, "fulfillmentInfo 里的 2 个模板应被解析出来"
    assert body["templates"][0] == {"template_id": "t1", "name": "默认包邮"}
    assert body["default_template_id"] == "t1"

# --------------------------------------------------------------------------- #
# 2b. 「已上传的图」必须转成**发布体形状**（用户实测 2026-10-10）
# --------------------------------------------------------------------------- #

def test_build_item_from_request_shapes_uploaded_images_for_publish():
    """⚠️ 用户报：「接口的媒体资源已经上传，**为何 main_images / detail_images
    使用的均不是已经上传后的返回的地址**」（2026-10-10）。

    真根因：`build_item_from_request` 把 `uploaded` 里的**简化 entry** 直接塞进
    `images` / `imageDescriptions`，**从没调 `build_publish_image`**（该函数有单测、
    却全仓零调用 ⇒ 单测绿而链路错）。发布体要的是带 `key` / `type` /
    `uploaderInfoModel` / `errors` 的那一版（真机抓包 `images[0]` 逐字比对过）。
    """
    from backend.app.services import shop_item_publish_service as svc

    uploaded = {
        "/tmp/a.jpg": {
            "link": "https://qimg/x", "path": "arkgoods/x", "extension": ".jpeg",
            "width": 800, "height": 800, "materialId": "m-1", "name": "a", "size": 10,
        },
        "/tmp/b.jpg": {
            "link": "https://qimg/y", "path": "arkgoods/y", "extension": ".jpeg",
            "width": 800, "height": 800, "materialId": "m-2", "name": "b", "size": 10,
        },
        "/tmp/s.jpg": {
            "link": "https://qimg/s", "path": "arkgoods/s", "extension": ".jpeg",
            "width": 800, "height": 800, "materialId": "m-3", "name": "s", "size": 10,
        },
    }
    body = svc.build_item_from_request(
        skeleton={}, name="标题", category_id="cat-1", uploaded=uploaded,
        main_image_paths=["/tmp/a.jpg"], detail_image_paths=["/tmp/b.jpg"],
        skus=[{"name": "颜色分类:斗柜", "price_yuan": 1, "stock": 1, "image_path": "/tmp/s.jpg"}],
        variant_ids=[],
    )
    #: ① 主图 / 详情图 / 规格图 **三处同形状**（真机一致）。
    for img in (body["images"][0], body["imageDescriptions"][0],
                body["skuData"][0]["specImage"]):
        assert img["type"] == "uploadImage", "应是发布体形状，不是上传返回的简化 entry"
        assert "key" not in img, "2026-10-13 真机抓包：uploadImage 无 key 字段"
        assert img["materialId"], "materialId 必须有"
        assert img["uploaderInfoModel"]["bizName"] == "ark"
        assert img["uploaderInfoModel"]["isSecret"] is True
    #: ② 用的必须是**上传返回的**地址（link / path / materialId 三件）。
    assert body["images"][0]["link"] == "https://qimg/x"
    assert body["images"][0]["path"] == "arkgoods/x"
    assert body["images"][0]["materialId"] == "m-1"
    assert body["skuData"][0]["specImage"]["materialId"] == "m-3"


def test_build_item_from_request_omits_spec_image_when_absent():
    """没有规格图 ⇒ **不发 `specImage` 键**（不是发一个空对象）。"""
    from backend.app.services import shop_item_publish_service as svc

    body = svc.build_item_from_request(
        skeleton={}, name="n", category_id="c", uploaded={},
        main_image_paths=[], detail_image_paths=[],
        skus=[{"name": "颜色分类:斗柜", "price_yuan": 1, "stock": 1, "image_path": None}],
        variant_ids=[],
    )
    assert "specImage" not in body["skuData"][0]


def test_build_item_from_request_applies_config_fields():
    """⚠️ 2026-10-10：第二步配置字段**必须进提交体**，不能只用骨架旧值。

    真根因：后端 `publish_item` 此前只接了 `attributes` / `specAttributes` 两个
    config 字段，其余（发货模式/价格保密/详情/回看/英文名/物流运费模板）
    **全被忽略**，提交体恒用骨架（上一个商品）的旧值。
    """
    from backend.app.services import shop_item_publish_service as svc

    cfg = {
        "deliveryMode": "现货模式",
        "priceConfidential": True,
        "desc": "这是详情描述",
        "showPlayback": True,
        "englishName": "English Name",
        "logisticsPlanId": "lp-user",
        "freightTemplateId": "st-user",
    }
    body = svc.build_item_from_request(
        skeleton={"logisticsPlanId": "lp-skel", "shippingTemplateId": "st-skel",
                   "deliveryMode": 2, "priceConfidential": False},
        name="标题", category_id="cat-1", uploaded={},
        main_image_paths=[], detail_image_paths=[],
        skus=[{"name": "颜色分类:斗柜", "price_yuan": 1, "stock": 1}],
        variant_ids=[], config=cfg,
    )
    #: ① **物流类型**决定 `deliveryMode`，与「发货模式」无关：
    #:    没给 `logisticsType` ⇒ 默认物流配送 = **0**（真机 radio value="0"）。
    #:    ⚠️ 旧断言这里是「现货模式→1」，那是把物流类型与发货模式混成一个字段，
    #:    正是用户实测「deliveryMode — 商品不支持设置无物流发货」的根因。
    assert body["deliveryMode"] == 0, "默认应为物流配送（0），不是无物流发货（1）"
    #: ② 价格保密 / 详情 / 回看 / 英文名 覆盖骨架
    assert body["priceConfidential"] is True
    assert body["description"] == "这是详情描述"
    assert body["usePlayback"] == 1
    #: ⚠️ 2026-10-11：真机字段是 `ename`（不是 `englishName`）—— 之前写错字段名。
    assert body["ename"] == "English Name"
    #: ③ 物流/运费模板用**用户选的值**优先（不是平台默认/骨架）
    assert body["logisticsPlanId"] == "lp-user"
    assert body["shippingTemplateId"] == "st-user"


def test_build_item_from_request_config_absent_falls_back_to_skeleton():
    """没有 config ⇒ 回落骨架旧值（不造、不猜）。"""
    from backend.app.services import shop_item_publish_service as svc

    body = svc.build_item_from_request(
        skeleton={"logisticsPlanId": "lp-skel", "shippingTemplateId": "st-skel",
                   "deliveryMode": 2, "priceConfidential": False},
        name="标题", category_id="cat-1", uploaded={},
        main_image_paths=[], detail_image_paths=[],
        skus=[{"name": "颜色分类:斗柜", "price_yuan": 1, "stock": 1}],
        variant_ids=[],
    )
    assert body["logisticsPlanId"] == "lp-skel"
    assert body["shippingTemplateId"] == "st-skel"
    #: 未传 config ⇒ deliveryMode 回落平台默认 **0**（物流配送，`_PLATFORM_DEFAULTS`），
    #: 不是骨架的过期值 2 —— 也绝不能是 1（= 无物流发货，平台会拒绝）。
    assert body["deliveryMode"] == 0


def test_publish_endpoint_syncs_published_item_via_sync_specified():
    """发布成功后必须走「同步指定商品」队列（`enqueue_goods_sync`），不是另写反查落库。

    ⚠️ 用户口径 2026-10-11：「上架成功的商品同步到本地库」+「走同步商品信息队列」。
    判据 = 发布端点源码里 `enqueue_goods_sync(` 的调用形态（源码守卫）。
    """
    from pathlib import Path

    from tests.backend.conftest import read_qianfan_source

    #: ⚠️ `qianfan` 已拆包（2026-10-11）⇒ 读**整包**（发布端点在 `qianfan/publish.py`，
    #:    原来读 `qf.__file__` 只会拿到包的 `__init__.py`，守卫从此恒绿）。
    src = read_qianfan_source()
    assert "shop_goods_sync_service.enqueue_goods_sync(" in src, (
        "发布成功后必须调 `enqueue_goods_sync`（同步商品信息队列），不是另写反查落库"
    )
