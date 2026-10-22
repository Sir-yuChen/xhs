"""商品「新增 → 草稿箱」的后端契约测试（设计 23）。

覆盖面（设计 23 §8 测试要点表）：

1. **红线**（最重要的一组）：`ItemDraftCreateRequest` **没有任何发布字段** /
   向契约里塞发布字段 ⇒ 422（`extra="forbid"`）/ 执行链里不出现
   `publish_async` 等上架端点 / 回执文案写明「未上架」。
2. **数据包解析**（纯本地）：列名**按表头匹配**（换列序仍正确）/ 缺列进 warnings /
   「商品属性图.png」不计入主图 / 半角与全角冒号都能拆 / 目录不存在 ⇒ 结构化 400 /
   价格两列回退（`计算价格` 空则取 `原价`）。
3. **草稿体组装**：**`price` 单位是元**（变异点）/ **`skuData` 不是 `skuList`**
   （变异点）/ 只有平台接受的五个键 / 图片条目字段齐全。
4. **回读校验**：提交与回读不符时**必须报出差集**（变异点）。
5. **端点契约**：归属校验（别人的账号 ⇒ 404）/ 解析端点零平台请求 /
   未登录千帆 ⇒ 400 结构化（**不静默降级**）。
6. **规格 id**：取不到 ⇒ 留空 + 警告（**不猜 id**）。

⚠️ 全部**不碰真实平台**：SDK 用假适配器打桩（三分组契约），
`create_product_draft` 的调用被替换成记录器。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from backend.app.core.security import create_access_token
from backend.app.models import PlatformAccount
from backend.app.services import shop_draft_package as pkg
from backend.app.services import shop_draft_service as svc
from tests.backend.conftest import seed_owner_rows


class _Ctx:
    def __init__(self, client, headers, ids, db_factory):
        self.client = client
        self.headers = headers
        self.ids = ids
        self.db_factory = db_factory


@pytest.fixture()
def ctx(api_client, db_factory):
    """一个用户 + 一个平台账号（**带 seller_id**，建草稿要用）。"""
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
        db.add_all([mine, other])
        db.commit()
        ids = {"mine": mine.id, "other": other.id, "owner_id": owner_id}
    finally:
        db.close()

    headers = {"Authorization": f"Bearer {create_access_token(owner_id)}"}
    return _Ctx(api_client, headers, ids, db_factory)


@pytest.fixture()
def package_dir(tmp_path: Path) -> Path:
    """造一个**真实结构**的商品数据包（xlsx + 三个目录）。"""
    import openpyxl

    root = tmp_path / "商品ID_999"
    (root / "SKU").mkdir(parents=True)
    (root / "主图").mkdir()
    (root / "详情图").mkdir()
    (root / "视频").mkdir()

    book = openpyxl.Workbook()
    sheet = book.active
    #: 列**故意乱序**：解析必须按表头匹配，不能按列序（变异点）。
    sheet.append(["库存", "sku名称", "原价", "计算价格", "ID", "图片"])
    sheet.append([200, "颜色分类:【中古风】实木三斗柜", 1280, None, "935947169189", ""])
    sheet.append([150, "颜色分类：【中古风】实木六斗柜", 1880, 1700, "935947169189", ""])
    book.save(root / "sku数据.xlsx")

    (root / "主图" / "主图01.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 100)
    (root / "主图" / "商品属性图.png").write_bytes(b"\x89PNG" + b"y" * 100)
    (root / "详情图" / "详情图01.jpg").write_bytes(b"\xff\xd8\xff" + b"z" * 100)
    (root / "SKU" / "SKU01_颜色分类(实木三斗柜).jpg").write_bytes(b"\xff\xd8\xff" + b"s" * 100)
    (root / "SKU" / "SKU02_颜色分类(实木六斗柜).jpg").write_bytes(b"\xff\xd8\xff" + b"t" * 100)
    (root / "视频" / "视频01.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
    return root


# --------------------------------------------------------------------- #
# 1. 红线：新增只能到草稿箱
# --------------------------------------------------------------------- #


def test_create_request_has_no_publish_field():
    """**红线守卫**：新增请求契约里**不存在**任何发布 / 上架字段。

    ⚠️ 这是本功能最重要的一条守卫。「加个 `publish: bool = False` 开关」
    是防线里最弱的一种（改一行就破），所以判据是**字段名整体不存在**。
    """
    from backend.app.api.platforms.xhs.qianfan import ItemDraftCreateRequest

    fields = set(ItemDraftCreateRequest.model_fields)
    forbidden = [name for name in fields if any(
        key in name.lower() for key in ("publish", "available", "shelf", "submit", "online")
    )]
    assert forbidden == [], (
        f"新增商品契约里出现了发布相关字段：{forbidden} —— "
        "红线是「只能存草稿」，发布开关**不允许存在于契约里**（设计 23 §3.2 口径①）"
    )
    assert "skus" in fields and "category_id" in fields, "基本字段缺失，契约被动过"


def test_create_request_rejects_publish_field(ctx):
    """往请求里塞发布字段 ⇒ **422**（`extra="forbid"`），不是被静默忽略。"""
    response = ctx.client.post(
        "/api/xhs/qianfan/item-drafts",
        json={
            "platform_account_id": ctx.ids["mine"],
            "category_id": "cat-1",
            "name": "测试商品",
            "skus": [{"name": "颜色分类:红", "price_yuan": 100, "stock": 1}],
            "publish": True,
        },
        headers=ctx.headers,
    )
    assert response.status_code == 422, (
        "传发布字段必须 422 —— 静默忽略会让调用方以为「发布了但没生效」，"
        "422 才是「这个字段不存在」的正确表达"
    )


def test_service_never_calls_publish_endpoints():
    """**红线守卫**：执行链里不出现任何「提交上架」-类端点的**调用**。

    平台侧「提交商品」走 `publish_async` + `publish_async_operate`；
    这两个**不允许**出现在本功能的任何调用点。

    ⚠️ 判据是**调用形态**（`api.publish_async(`）而不是「出现过这个词」——
    docstring 里说明「我们不接这条链路」用的正是这个词，
    宽判据会把正确注释判成违规（本技能 §0 第 3 条：宽锚点 = 假绿/假红）。
    """
    source = Path(svc.__file__).read_text(encoding="utf-8")
    for forbidden in ("publish_async", "publish_note", "batch_update_availability"):
        assert f".{forbidden}(" not in source, (
            f"草稿执行链里**调用了**上架端点 `{forbidden}` —— 红线不允许（设计 23 §6）"
        )
    # 反证：建档走的是存档端点（否则上面的断言可能只是「什么都没调」）。
    #: ⚠️ 2026-10-11 真机 bundle：**新建**走 `create_product_drafts`（返回 productDraftId），
    #: **更新**才走 `save_product_drafts`。本链路是「新建商品 → 存草稿」⇒ 走 create。
    assert ".create_product_draft(" in source


def test_result_note_says_not_on_shelf():
    """回执文案必须写明「未上架」—— 用户看到「成功」时最容易脑补成「已发布」。"""
    source = Path(svc.__file__).read_text(encoding="utf-8")
    assert "已保存到千帆草稿箱，未上架" in source


# --------------------------------------------------------------------- #
# 2. 数据包解析
# --------------------------------------------------------------------- #


def test_parse_matches_columns_by_header_not_order(package_dir: Path):
    """列名**按表头匹配** —— 表头故意乱序，值仍要出现在正确的字段上。"""
    parsed = pkg.parse_package(str(package_dir))
    assert len(parsed["sku_rows"]) == 2
    first = parsed["sku_rows"][0]
    assert first["name"] == "颜色分类:【中古风】实木三斗柜"
    assert first["stock"] == 200, "库存列在表头第 0 位，按列序读会读到 SKU 名"
    assert first["price"] == 1280, "计算价格为空时必须回退到原价"


def test_parse_price_prefers_calc_then_original(package_dir: Path):
    """价格两列取一个：优先 `计算价格`，空则回退 `原价`（真机数据 `计算价格` 全空）。"""
    parsed = pkg.parse_package(str(package_dir))
    second = parsed["sku_rows"][1]
    assert second["price"] == 1700, "有计算价格时必须用它（不是原价 1880）"


def test_parse_includes_every_image_in_the_main_dir(package_dir: Path):
    """「商品属性图.png」**不计入商品主图**（用户口径 2026-10-11 **最终版**）。

    ⚠️ 六轮口径对照（保留以便追溯）：

    | 轮次 | 属性图 | 理由 |
    |---|---|---|
    | 2026-10-XX（原） | **排除** + warning | 属性表截图不是商品展示图 |
    | 2026-10-11（中） | 保留（含在主图里） | 「不要单独过滤媒体资源」 |
    | **2026-10-11（本条·现行）** | **排除** + warning | 用户：「商品 sku 表格下依旧存在商品属性图，关于商品属性图列表的相关逻辑**彻底删除**」 |

    判据：主图目录里的图**全部**计入主图（不过滤）。
    ⚠️ 现行口径：**不排除任何媒体**（判据与叶子模块都已删除）。
    """
    parsed = pkg.parse_package(str(package_dir))
    names = [Path(p).name for p in parsed["main_images"]]
    assert "商品属性图.png" in names, "主图目录里的图应全部计入主图（不排除任何媒体）"
    assert any(n.startswith("主图0") for n in names), "普通主图必须照常保留"

def test_parse_reports_video_and_missing_dirs(package_dir: Path):
    """视频与「没有 x 目录」都如实进 warnings。"""
    parsed = pkg.parse_package(str(package_dir))
    assert len(parsed["videos"]) == 1
    assert any("视频" in text for text in parsed["warnings"])


def test_parse_missing_dir_raises_structured(tmp_path: Path):
    """目录不存在 ⇒ `PackageError`（端点把它翻成结构化 400）。"""
    with pytest.raises(pkg.PackageError):
        pkg.parse_package(str(tmp_path / "不存在的目录"))


def test_parse_without_xlsx_raises(tmp_path: Path):
    """有目录但没有 xlsx ⇒ 报错（不能当成「0 个 SKU 的商品」放过去）。"""
    (tmp_path / "空包").mkdir()
    with pytest.raises(pkg.PackageError):
        pkg.parse_package(str(tmp_path / "空包"))


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("颜色分类:实木三斗柜", ("颜色分类", "实木三斗柜")),
        ("颜色分类：实木三斗柜", ("颜色分类", "实木三斗柜")),
        ("没有分隔符", ("", "没有分隔符")),
        (":只有值", ("", ":只有值")),
    ],
)
def test_split_sku_name(raw, expected):
    """半角与全角冒号都吃；拆不出就**不猜**（规格名留空）。"""
    assert pkg.split_sku_name(raw) == expected


# --------------------------------------------------------------------- #
# 3. 草稿体组装（三个静默错值的坑）
# --------------------------------------------------------------------- #


def test_draft_body_only_has_accepted_keys():
    """草稿体**只含平台接受的五个键** —— 多传的会被静默丢弃（真机实测）。"""
    body = pkg.build_draft_body(
        category_id="c1", name="n1", main_images=[], detail_images=[], skus=[]
    )
    assert set(body) == {"categoryId", "name", "standardImages", "longImages", "skuData"}
    for dropped in ("itemName", "properties", "images", "imageDescriptions", "skuList"):
        assert dropped not in body, (
            f"`{dropped}` 被平台**静默丢弃**（接口 200、回读为空）—— "
            "塞进去只会让人以为存上了（设计 23 §0.1）"
        )


def test_sku_price_unit_is_yuan_not_fen():
    """**变异点**：SKU 价格单位必须是「元」，不是「分」。

    写成分不报错，但价格会变成一百倍 —— 静默错值，本链路最贵的坑。
    """
    entry = pkg.build_sku_entry(
        name="颜色分类:实木三斗柜", price_yuan=1280, stock=200,
        variant_id="v1", row_number=1,
    )
    assert entry["price"]["price"] == 1280, (
        "传 1280 元必须原样落 1280；如果这里变成 128000 说明被当成分做了 ×100"
    )
    assert entry["price"]["originPrice"] == 1280


def test_sku_uses_variants_not_sku_variant_infos():
    """**变异点**：写形态的字段名是 `variants`（读形态才是 `skuVariantInfos`）。"""
    entry = pkg.build_sku_entry(
        name="颜色分类:实木三斗柜", price_yuan=100, stock=1,
        variant_id="v1", row_number=1,
    )
    assert "variants" in entry
    assert "skuVariantInfos" not in entry, "写形态用 skuVariantInfos 会被静默丢弃"
    variant = entry["variants"][0]
    assert variant["attributeId"] == "v1"
    assert variant["attributeName"] == "颜色分类"
    assert variant["attributeValue"] == "实木三斗柜"


def test_sku_price_missing_stays_missing():
    """价格缺值 ⇒ **不写那个键**（不是填 0 —— 0 元商品会被平台当真价收下）。"""
    entry = pkg.build_sku_entry(
        name="颜色分类:实木三斗柜", price_yuan=None, stock=200,
        variant_id="", row_number=1,
    )
    assert "price" not in entry["price"] or entry["price"].get("price") is None
    assert entry["variants"] == [], "没有规格 id 就**不留空壳 variant**（不猜）"


def test_image_entry_drops_missing_size():
    """图片尺寸拿不到 ⇒ **整个键不发**（平台 Java 后端 parseInt("") 会炸）。"""
    entry = pkg.build_image_entry({
        "link": "https://qimg.xiaohongshu.com/a", "path": "material_space/a",
        "extension": ".jpeg", "materialId": "a", "size": 123, "width": None, "height": None,
    })
    assert "width" not in entry and "height" not in entry
    assert entry["sourceLink"] == "http://qimg.xiaohongshu.com/a", "sourceLink 用 http"


# --------------------------------------------------------------------- #
# 4. 回读校验
# --------------------------------------------------------------------- #


def test_diff_readback_reports_every_mismatch():
    """**变异点**：提交与回读不符 ⇒ 必须报出差集（不校验 = 「接口说成功、草稿是空的」）。"""
    submitted = pkg.build_draft_body(
        category_id="c1", name="标题", main_images=[{}, {}], detail_images=[{}],
        skus=[{"a": 1}, {"b": 2}],
    )
    readback = {"category_id": "c1", "name": "标题", "image_count": 1,
                "long_image_count": 1, "sku_count": 2, "read_ok": True}
    problems = pkg.diff_draft_readback(submitted, readback)
    assert any("主图" in text for text in problems), "主图数不符必须报出来"


def test_diff_readback_clean_when_matched():
    """全部相符 ⇒ 空列表（不是「永远报一堆」）。"""
    submitted = pkg.build_draft_body(
        category_id="c1", name="标题", main_images=[{}], detail_images=[{}],
        skus=[{"a": 1}],
    )
    readback = {"category_id": "c1", "name": "标题", "image_count": 1,
                "long_image_count": 1, "sku_count": 1, "read_ok": True}
    assert pkg.diff_draft_readback(submitted, readback) == []


def test_diff_readback_flags_empty_readback():
    """读不回来（全 0）⇒ 每一项都要报出来（读不到就该报警，不是静默通过）。"""
    submitted = pkg.build_draft_body(
        category_id="c1", name="标题", main_images=[{}], detail_images=[], skus=[{"a": 1}]
    )
    problems = pkg.diff_draft_readback(
        submitted,
        {"category_id": "", "name": "", "image_count": 0,
         "long_image_count": 0, "sku_count": 0, "read_ok": False},
    )
    assert len(problems) >= 3


# --------------------------------------------------------------------- #
# 5. 规格 id 解析（**不猜**）
# --------------------------------------------------------------------- #


def test_variant_id_resolved_from_ledger():
    """规格名能对上本店字典 ⇒ 取其 id。"""
    assert svc.resolve_variant_id(
        variant_name="颜色分类", ledger_variant_ids={"颜色分类": "vid-1", "尺码": "vid-2"}
    ) == "vid-1"


def test_variant_id_not_guessed_when_missing():
    """**不猜 id**：字典里没有 ⇒ 空串（不编一个「颜色分类」的 id 出来）。"""
    assert svc.resolve_variant_id(
        variant_name="不存在的规格", ledger_variant_ids={"颜色分类": "vid-1"}
    ) == ""


def test_variant_id_empty_name_returns_empty():
    """规格名为空 ⇒ 空串（不匹配任何东西）。"""
    assert svc.resolve_variant_id(variant_name="", ledger_variant_ids={"颜色分类": "v"}) == ""


# --------------------------------------------------------------------- #
# 6. 端点契约
# --------------------------------------------------------------------- #


def test_parse_endpoint_is_local_only(ctx, package_dir: Path, monkeypatch):
    """解析端点**零平台请求** —— 未登录千帆也能用（只读本地磁盘）。"""
    called = {"n": 0}

    def _boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("解析端点不应发起任何平台请求")

    from backend.app.api.platforms.xhs import qianfan as q

    monkeypatch.setattr(q, "_ark_adapter_for", _boom)
    response = ctx.client.post(
        "/api/xhs/qianfan/item-drafts/parse",
        json={"platform_account_id": ctx.ids["mine"], "package_path": str(package_dir)},
        headers=ctx.headers,
    )
    assert response.status_code == 200
    assert called["n"] == 0, "解析数据包是纯本地操作，不该碰平台"
    body = response.json()
    assert len(body["sku_rows"]) == 2
    assert body["limits"]["max_images"] > 0, "上限必须由后端下发（前端不抄常量）"


def test_parse_endpoint_ownership(ctx, package_dir: Path):
    """别人的账号 ⇒ 404（不泄露存在性）。"""
    response = ctx.client.post(
        "/api/xhs/qianfan/item-drafts/parse",
        json={"platform_account_id": ctx.ids["other"], "package_path": str(package_dir)},
        headers=ctx.headers,
    )
    assert response.status_code == 404


def test_parse_endpoint_bad_path_is_structured_400(ctx):
    """坏路径 ⇒ 结构化 400（不是 500）。"""
    response = ctx.client.post(
        "/api/xhs/qianfan/item-drafts/parse",
        json={"platform_account_id": ctx.ids["mine"], "package_path": "C:/不存在的目录"},
        headers=ctx.headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "DRAFT_PACKAGE_INVALID"


def test_create_requires_ark_session(ctx):
    """没有千帆登录态 ⇒ **精确** 400 `DRAFT_ARK_REQUIRED`，**不静默降级**。

    静默降级的表现是「接口说成功、平台没有草稿」—— 比直接报错坏得多。

    ⚠️ 判据必须**精确到 code**：宽判据（`in {A, B}`）会被下游的另一处
    令牌检查满足 ⇒ 变异体「塞个假 cookie 绕过」照样绿（本用例首轮就是这样
    ESCAPED 的）。这里断言**本端点自己**发出的那个 code。
    """
    response = ctx.client.post(
        "/api/xhs/qianfan/item-drafts",
        json={
            "platform_account_id": ctx.ids["mine"],
            "category_id": "cat-1",
            "name": "测试商品",
            "skus": [{"name": "颜色分类:红", "price_yuan": 100, "stock": 1}],
        },
        headers=ctx.headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "DRAFT_ARK_REQUIRED", (
        "必须是本端点的 DRAFT_ARK_REQUIRED（不能靠下游的 ARK_SESSION_REQUIRED 兜底 —— "
        "那说明令牌检查被绕过了一次，只是恰好又被拦下）"
    )


def test_create_reports_readback_diff(monkeypatch):
    """草稿走真机新接口 `save_product_drafts`（2026-10-11，方案 A）。

    ⚠️ 旧的「回读 diff」（`diff_draft_readback` 5 键）已随 `create_product_drafts`
    一起退场。新接口的**响应结构**（`draftId` 还是 `productDraftId`）与回读校验
    契约**待真机抓包确认**（用户 2026-10-11 口径：抓包后统一改，不做保守处理）。
    """
    def _ok_upload(api, *, seller_id, file_path, permit=None):
        return True, "成功", {"link": "https://x/a", "path": "material_space/a"}

    monkeypatch.setattr(svc, "upload_one_image", _ok_upload)

    captured = {}

    class _Api:
        @staticmethod
        def create_product_draft(body):
            captured["body"] = body
            return True, "ok", {"data": {"productDraftId": "draft-1"}}

        @staticmethod
        def is_draft_rate_limited(msg):
            return False

        @staticmethod
        def publish_render(params):
            return True, "ok", None

        @staticmethod
        def get_seller_carriage_template():
            return False, "no", None

        @staticmethod
        def search_items(**kwargs):
            return True, "ok", {"data": {"items": []}}

        @staticmethod
        def upload_web_permit(count, **kwargs):
            """**批量预分配凭证**（2026-10-22 起上传前一次性拿齐）。"""
            return True, "ok", {
                "data": {
                    "uploadTempPermits": [
                        {
                            "qos": 1,
                            "fileIds": [f"fid-{i}" for i in range(int(count))],
                            "uploadAddr": "https://ros.example/put",
                            "token": "tok",
                        }
                    ]
                }
            }

    result = svc.create_draft(
        api=_Api(), seller_id="s1", category_id="c1", name="n",
        main_image_paths=["C:/fake/a.jpg"], detail_image_paths=[], skus=[],
    )
    #: 新建走 create_product_draft + 完整 itemData（含 `images`，不是旧的 `standardImages`）。
    assert "body" in captured, "必须调 create_product_draft（新建接口）"
    assert "images" in captured["body"], "草稿体应是完整 itemData（含 images，非旧 standardImages）"
    assert result["draft_id"] == "draft-1"


def test_create_endpoint_rejects_empty_skus(ctx):
    """空 SKU 列表 ⇒ 422（契约层 `min_length=1`，不到业务逻辑就拦）。"""
    response = ctx.client.post(
        "/api/xhs/qianfan/item-drafts",
        json={
            "platform_account_id": ctx.ids["mine"],
            "category_id": "cat-1",
            "name": "测试商品",
            "skus": [],
        },
        headers=ctx.headers,
    )
    assert response.status_code == 422


def test_upload_retries_on_transient_failure(monkeypatch):
    """图片上传**失败要重试**（真机实测：系统代理下 ROS PUT 偶发 ProxyError）。

    不重试的表现是「同一份数据包这次少两张图、下次又好了」—— 用户无法归因。
    """
    attempts = {"n": 0}

    def _flaky(api, *, seller_id, file_path, permit=None):
        attempts["n"] += 1
        if attempts["n"] < 2:
            return False, "ProxyError: 模拟瞬时失败", None
        return True, "成功", {"link": "https://x/a", "path": "material_space/a"}

    monkeypatch.setattr(svc, "upload_one_image", _flaky)
    monkeypatch.setattr(svc.time, "sleep", lambda *_: None)

    class _Api:
        @staticmethod
        def create_product_draft(body):
            return True, "ok", {"data": {"productDraftId": "draft-1"}}

        @staticmethod
        def is_draft_rate_limited(msg):
            return False

        @staticmethod
        def publish_render(params):
            return False, "no", None

        @staticmethod
        def get_seller_carriage_template():
            return False, "no", None

        @staticmethod
        def search_items(**kwargs):
            return True, "ok", {"data": {"items": []}}

        @staticmethod
        def upload_web_permit(count, **kwargs):
            """**批量预分配凭证**（2026-10-22 起上传前一次性拿齐）。"""
            return True, "ok", {
                "data": {
                    "uploadTempPermits": [
                        {
                            "qos": 1,
                            "fileIds": [f"fid-{i}" for i in range(int(count))],
                            "uploadAddr": "https://ros.example/put",
                            "token": "tok",
                        }
                    ]
                }
            }

    result = svc.create_draft(
        api=_Api(), seller_id="s1", category_id="c1", name="n",
        main_image_paths=["C:/fake/a.jpg"], detail_image_paths=[], skus=[],
    )
    assert attempts["n"] == 2, "第一次失败后必须重试"
    assert result["uploaded_images"] == 1
    assert result["failed_images"] == []


def test_draft_rate_limit_recognized():
    """平台限流文案必须被识别（真机原文含「每分钟限制」）。"""
    from apis.xhs_qianfan_apis import XHS_Qianfan_Apis

    assert XHS_Qianfan_Apis.is_draft_rate_limited("提示：单店铺每分钟限制发布2个草稿！")
    assert not XHS_Qianfan_Apis.is_draft_rate_limited("其它错误")


def test_sdk_large_body_uses_at_file(monkeypatch, tmp_path):
    """**DEF 回归守卫**：大 body 的签名走 `@file` 形态，不当命令行参数传。

    Windows 命令行上限 32767 字符，商品草稿体 ~10KB+ 会命中
    `OSError: [WinError 206]`，而错误信息**完全指错方向**（说「签名运行时不可用」）。
    """
    source = Path(
        __import__("apis.xhs_qianfan_apis", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")
    #: ⚠️ DEF-101 之后签名临时文件改 `tempfile.mkstemp(prefix=".sign_input_", …)`，
    #: 每个请求唯一（防并发写删冲突）。断言锚点对齐实际前缀（带下划线）。
    assert '.sign_input_' in source, (
        "签名入参必须走 @file（临时文件前缀 .sign_input_）；"
        "直接当 argv 传会在 body 较大时 WinError 206"
    )
    assert "json.dumps(payload, ensure_ascii=False)]" not in source, (
        "不要把签名 payload 当命令行参数"
    )
