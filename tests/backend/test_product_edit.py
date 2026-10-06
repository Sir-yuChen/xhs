"""M4「商品编辑优化」的后端契约测试（设计 20）。

覆盖面（设计 20 §6 测试要点表）：
1. **分组 CRUD** —— 建 / 改名 / 删（级联清空 + `released` 影响面）/ 重名 409 /
   跨用户隔离（B 动 A 的分组 ⇒ 404）/ `product_count` **跨账号聚合**。
2. **商品编辑** —— 「不传 = 不动」与「传 null = 清空」**两种语义分开测**（变异点）/
   归属校验 / `updated[]` 只列真改过的列。
3. **导出** —— 筛选与列表**同集合** / 超限 400（不静默截断）/ BOM / 中文文件名不崩
   （DEF-091 回归守卫）/ `ai_title` 回退原标题。
4. **AI 润色** —— 未配服务商 ⇒ 400 结构化 / 备选各 3 个 / **润色不落库**。
5. **守卫** —— 新端点 `extra="forbid"` / `product_groups` 表在 `Base.metadata` /
   润色链路不旁路直连上游。

⚠️ 边界（设计 20 §0）：**改价 / 上下架不可交付**（千帆 SDK 零商品写接口），
本文件**不测**它们 —— 没有实现的东西不写「假守卫」。
"""
from __future__ import annotations

import csv
import io

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.platforms.xhs.qianfan import MAX_EXPORT_ROWS
from backend.app.core.database import Base
from backend.app.core.security import create_access_token
from backend.app.models import PlatformAccount, User
from backend.app.models.shop_product import ShopProduct
from tests.backend.conftest import seed_owner_rows


class _Ctx:
    """本文件各用例共用的一份上下文（两用户 + owner 两个账号 + 一个外人账号）。"""

    def __init__(self, client, headers, intruder_headers, ids, db_factory):
        self.client = client
        self.headers = headers
        self.intruder_headers = intruder_headers
        self.ids = ids
        self.db_factory = db_factory


@pytest.fixture()
def ctx(api_client, db_factory):
    """建两用户（owner / intruder）+ owner 的两个平台账号。

    ⚠️ **两个账号是刻意的** —— `product_count` 的跨账号聚合只有两个账号才测得出来
    （单账号时「跨账号聚合」与「只数当前账号」结果恒等，变异抓不住）。

    ⚠️ **用 `conftest` 的公共夹具**（`api_client` / `db_factory`），不自己抄
    `create_engine + create_all + dependency_overrides` 三件套 —— 项目硬规矩之一，
    由 `test_testing_spec_guards.py` 强制。
    """
    owner_id = 2
    intruder_id = 3
    seed_owner_rows(db_factory().get_bind(), user_id=owner_id, account_id=101)
    seed_owner_rows(db_factory().get_bind(), user_id=intruder_id, account_id=102)

    db = db_factory()
    try:
        acc_a = PlatformAccount(
            user_id=owner_id, platform="xhs", sub_type="creator", nickname="账号A", status="active"
        )
        acc_b = PlatformAccount(
            user_id=owner_id, platform="xhs", sub_type="creator", nickname="账号B", status="active"
        )
        foreign_acc = PlatformAccount(
            user_id=intruder_id, platform="xhs", sub_type="pc", nickname="别人的号", status="active"
        )
        db.add_all([acc_a, acc_b, foreign_acc])
        db.commit()
        ids = {
            "a": acc_a.id,
            "b": acc_b.id,
            "foreign": foreign_acc.id,
            "owner_id": owner_id,
        }
    finally:
        db.close()

    headers = {"Authorization": f"Bearer {create_access_token(owner_id)}"}
    intruder_headers = {"Authorization": f"Bearer {create_access_token(intruder_id)}"}
    return _Ctx(api_client, headers, intruder_headers, ids, db_factory)


def _add_product(db_factory, account_id: int, item_id: str, title: str = "测试商品", **cols):
    """往台账塞一行（走 `store_products` —— 与生产同一条落库路径）。

    ⚠️ **不走裸 `ShopProduct(...)`**：列表 / 导出的字段全部从 `raw_json` 派生
    （`normalize_item_from_raw(_parse_raw(row))`）⇒ 直接建 ORM 行会得到一行空值
    （实测：导出 1 行全空、列表 0 行）。
    """
    from backend.app.models import PlatformAccount
    from backend.app.services.shop_goods_sync_service import store_products

    db = db_factory()
    try:
        account = db.get(PlatformAccount, account_id)
        stored = store_products(db, account.user_id, account.id, [_ark_item(item_id, title)])
        assert stored == [item_id], f"播种失败（normalize_product 返回 None？）：{stored}"
        db.flush()  # 让关系行可见，再改本地列
        if cols:
            row = db.scalars(
                select(ShopProduct).where(
                    ShopProduct.platform_account_id == account_id,
                    ShopProduct.item_id == item_id,
                )
            ).one()
            for key, value in cols.items():
                setattr(row, key, value)
        db.commit()
        return item_id
    finally:
        db.close()


def _ark_item(item_id: str, title: str, **overrides) -> dict:
    """千帆商品条目的**真实字段名**（照抄 `test_shop_items._ark_item`）。

    ⚠️ 关键信号缺一不可：`buyable` / `item_freeze` / `deleted` /
    `item_audit_status` / `on_shelf_time` —— 少了它们 `normalize_product`
    会返回 `None`（而不是落一行空值），台账里就什么都没有。
    """
    base = {
        "item_id": item_id,
        "item_name": title,
        "article_no": "1056576341942",
        "min_price": 3226,          # 分
        "max_price": 5402,
        "total_stock": 700,
        "sku_count": 7,
        "on_sale_sku_count": 7,
        "product_note_num": 1,
        "product_note_ids": ["6a5d4ef90000000013027665"],
        "buyable": True,
        "item_freeze": False,
        "deleted": False,
        "item_audit_status": 2,
        "check_status": 1,
        "is_channel": False,
        "create_time": 1784082313000,
        "on_shelf_time": 1784084996000,
        "update_time": 1784082446000,
        "xsec_token": "ARK-ITEM-TOKEN",
        "sale_qty_info": {"acc_sale_qty": 12, "sale_qty30": 3},
        "inventory_risk_info": {"has_inventory_risk": True, "warning_sku_count": 2},
        "images": [{"link": f"http://qimg.example/{item_id}-1.jpg", "width": 1440, "height": 1440}],
        "categories": [
            {"id": "c1", "name": "餐饮具", "level": 1},
            {"id": "c2", "name": "野餐垫", "level": 3},
        ],
    }
    base.update(overrides)
    return base


class TestProductGroupsRemoved:
    """**分组已彻底退场**（2026-10-05 用户口径「彻底移除商品分组相关逻辑」）。

    ⚠️ 原 `TestGroupCrud` 四条的载体（4 个 CRUD 端点 + `product_groups` 表 +
    `shop_products.group_id` 列）**都不存在了**，所以那四条不是「改判据」而是
    **没有可断言的实现** ⇒ 整类删除，换成这一条「退场守卫」：
    路径必须回 410 墓碑（不是 404 —— 404 会让前端把它当「路由写错」而不是「功能没了」），
    且 ORM metadata 里不许再有那张表（`create_all` 会把迁移已 drop 的表建回来，
    2026-10-01 v2.0 的 `ModelConfig` 就踩过这个坑）。

    ⚠️ 请求函数 / 类型 / 前端筛选下拉的退场由 `verify_doc_counts.py` 与
    `tsc --noEmit` 兜（它们不在这里断言）。
    """

    def test_group_endpoints_are_tombstoned(self, ctx):
        headers = ctx.headers
        assert (
            ctx.client.get("/api/xhs/qianfan/product-groups", headers=headers).status_code
            == 410
        )
        assert (
            ctx.client.post(
                "/api/xhs/qianfan/product-groups", json={"name": "x"}, headers=headers
            ).status_code
            == 410
        )

    #: ⚠️ 表 / 列的退场判据**不在这里**再断言一遍 —— 它们是 `TestGuards`
    #: 里两条同源守卫（`test_product_groups_table_is_not_registered_in_metadata`
    #: 与 `test_shop_products_has_m4_columns`），一处实现一处断言（DEF-044）。


class TestProductEdit:
    """`PATCH /items/edit` 的 PATCH 语义（设计 20 §2.2 口径①）。"""

    def test_omitted_key_keeps_value_and_null_clears(self, ctx):
        """**两种语义分开测**（变异点②）。

        ⚠️ 变异：把 sentinel 改成 `Optional[..., None]` ⇒ 省略键会被当显式 null
        清空 ⇒ 本用例第一段必红。
        """
        headers, ids, db_factory = ctx.headers, ctx.ids, ctx.db_factory
        _add_product(db_factory, ids["a"], "ITEM-1", ai_title="原标题", ai_selling_point="原卖点")

        # ① 只传 ai_title ⇒ 另一项**保持原值**（不传 = 不动）
        #
        #    ⚠️ 2026-10-05：原第一段用 `group_id` 做「省略键」的载体，分组退场后
        #    改成 `ai_selling_point`（同一语义，换一个还存在的列）。
        only_title = ctx.client.patch(
            "/api/xhs/qianfan/items/edit",
            json={"platform_account_id": ids["a"], "item_id": "ITEM-1", "ai_title": "新标题"},
            headers=headers,
        )
        assert only_title.status_code == 200, only_title.text
        body = only_title.json()
        assert body["ai_title"] == "新标题"
        assert body["ai_selling_point"] == "原卖点", "省略的键必须保持原值"
        assert body["updated"] == ["ai_title"]

        # ② 显式传 null ⇒ 清空
        cleared = ctx.client.patch(
            "/api/xhs/qianfan/items/edit",
            json={"platform_account_id": ids["a"], "item_id": "ITEM-1", "ai_title": None},
            headers=headers,
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["ai_title"] is None, "显式 null 必须清空"
        assert cleared.json()["ai_selling_point"] == "原卖点", "没传的那项仍不动"
        assert cleared.json()["updated"] == ["ai_title"]

    def test_updated_lists_only_changed_fields(self, ctx):
        """`updated[]` 只列**真正变了**的列（送回同值 ⇒ 空数组）。"""
        headers, ids, db_factory = ctx.headers, ctx.ids, ctx.db_factory
        _add_product(db_factory, ids["a"], "ITEM-1", ai_title="同值")

        same = ctx.client.patch(
            "/api/xhs/qianfan/items/edit",
            json={"platform_account_id": ids["a"], "item_id": "ITEM-1", "ai_title": "同值"},
            headers=headers,
        )
        assert same.status_code == 200, same.text
        assert same.json()["updated"] == [], "值没变就不该报「已更新」"

        changed = ctx.client.patch(
            "/api/xhs/qianfan/items/edit",
            json={"platform_account_id": ids["a"], "item_id": "ITEM-1", "ai_title": "新值"},
            headers=headers,
        )
        assert changed.json()["updated"] == ["ai_title"]

    def test_editing_other_accounts_item_is_404(self, ctx):
        headers, ids, db_factory = ctx.headers, ctx.ids, ctx.db_factory
        _add_product(db_factory, ids["foreign"], "ITEM-X")

        # owner 拿自己的账号 A 去改别人账号的商品 ⇒ 台账里查不到 ⇒ 404
        response = ctx.client.patch(
            "/api/xhs/qianfan/items/edit",
            json={"platform_account_id": ids["a"], "item_id": "ITEM-X", "ai_title": "偷改"},
            headers=headers,
        )
        assert response.status_code == 404, response.text

    def test_edit_rejects_unknown_key(self, ctx):
        """**已退场的 `group_id` 不许再被收下**（2026-10-05 分组彻底移除）。

        ⚠️ 这是 `_UNSET` sentinel 之外的另一半保证：`extra="forbid"` ⇒ 前端若还留着
        旧代码发 `group_id`，真机会拿到 422（结构化），而不是被**静默忽略**
        （静默忽略 = 用户以为改了分组、其实什么都没发生）。
        """
        headers, ids, db_factory = ctx.headers, ctx.ids, ctx.db_factory
        _add_product(db_factory, ids["a"], "ITEM-1")

        response = ctx.client.patch(
            "/api/xhs/qianfan/items/edit",
            json={"platform_account_id": ids["a"], "item_id": "ITEM-1", "group_id": 7},
            headers=headers,
        )
        assert response.status_code == 422, response.text


class TestExport:
    """CSV 导出（设计 20 §2.1 端点 6 + §4.5）。"""

    def test_export_matches_list_selection_and_has_bom(self, ctx):
        """导出集合 = 列表集合（同一筛选），且带 UTF-8 BOM + 中文文件名不崩（DEF-091）。"""
        headers, ids, db_factory = ctx.headers, ctx.ids, ctx.db_factory
        _add_product(db_factory, ids["a"], "ITEM-1", "野餐垫")
        _add_product(db_factory, ids["a"], "ITEM-2", "防晒衣")

        listed = ctx.client.post(
            "/api/xhs/qianfan/items",
            json={"platform_account_id": ids["a"], "keyword": "野餐"},
            headers=headers,
        )
        assert listed.status_code == 200, listed.text
        total = listed.json()["total"]

        exported = ctx.client.post(
            "/api/xhs/qianfan/items/export",
            json={"platform_account_id": ids["a"], "keyword": "野餐"},
            headers=headers,
        )
        # ⚠️ DEF-091 回归守卫：中文文件名不许再让响应头编码炸掉（此前恒 500）
        assert exported.status_code == 200, exported.text
        disposition = exported.headers["content-disposition"]
        assert "filename*=UTF-8''" in disposition, "中文名必须走 RFC 5987"
        assert 'filename="shop-items-' in disposition, "必须留纯 ASCII 回退名"

        raw = exported.content
        assert raw.startswith(b"\xef\xbb\xbf"), "CSV 必须带 BOM（Excel 才认得中文）"
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        assert len(rows) - 1 == total, "CSV 数据行数必须等于列表 total（同一筛选）"

    def test_export_falls_back_to_platform_title_when_no_ai_title(self, ctx):
        """`ai_title` 有值导它、没有导 `item_title`（用户要的是「能用的标题」）。"""
        headers, ids, db_factory = ctx.headers, ctx.ids, ctx.db_factory
        _add_product(db_factory, ids["a"], "ITEM-1", "原标题", ai_title="AI标题")
        _add_product(db_factory, ids["a"], "ITEM-2", "另一个原标题")

        exported = ctx.client.post(
            "/api/xhs/qianfan/items/export",
            json={"platform_account_id": ids["a"]},
            headers=headers,
        )
        text = exported.content.decode("utf-8-sig")
        assert "AI标题" in text
        assert "另一个原标题" in text

    def test_export_over_limit_is_rejected_not_truncated(self, ctx, monkeypatch):
        """超限**整体拒绝**（变异点③）。

        ⚠️ 变异：去掉上限判定（静默截断）⇒ 本用例必红 —— 截断的 CSV 是
        「看起来完整」的假全量，用户会拿它对账。
        """
        headers, ids, db_factory = ctx.headers, ctx.ids, ctx.db_factory
        # 把上限压到 1 行，造出「超限」确定态（不用真塞 5000 行）
        import backend.app.api.platforms.xhs.qianfan as qf

        monkeypatch.setattr(qf, "MAX_EXPORT_ROWS", 1)
        _add_product(db_factory, ids["a"], "ITEM-1")
        _add_product(db_factory, ids["a"], "ITEM-2")

        response = ctx.client.post(
            "/api/xhs/qianfan/items/export",
            json={"platform_account_id": ids["a"]},
            headers=headers,
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "QIANFAN_EXPORT_TOO_MANY"
        assert response.json()["detail"]["limit"] == 1

    def test_export_is_account_scoped(self, ctx):
        headers, ids, db_factory = ctx.headers, ctx.ids, ctx.db_factory
        _add_product(db_factory, ids["a"], "ITEM-1", "自己的商品")
        _add_product(db_factory, ids["foreign"], "ITEM-X", "别人的商品")

        exported = ctx.client.post(
            "/api/xhs/qianfan/items/export",
            json={"platform_account_id": ids["a"]},
            headers=headers,
        )
        text = exported.content.decode("utf-8-sig")
        assert "自己的商品" in text
        assert "别人的商品" not in text


class TestPolish:
    """`POST /items/polish` 的 AI 润色（设计 20 §3）。"""

    def test_polish_without_provider_is_structured_400(self, ctx):
        """未配服务商 ⇒ **400 结构化**（不回落、不静默用别的服务商）。"""
        headers, ids, db_factory = ctx.headers, ctx.ids, ctx.db_factory
        _add_product(db_factory, ids["a"], "ITEM-1", "野餐垫")

        response = ctx.client.post(
            "/api/xhs/qianfan/items/polish",
            json={"platform_account_id": ids["a"], "item_id": "ITEM-1"},
            headers=headers,
        )
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert isinstance(detail, dict), "失败必须结构化（不是裸字符串）"
        assert detail.get("code")

    def test_polish_returns_three_candidates_and_does_not_persist(self, ctx, monkeypatch):
        """备选各 3 个 + **润色不落库**（采纳才落）。"""
        headers, ids, db_factory = ctx.headers, ctx.ids, ctx.db_factory
        _add_product(db_factory, ids["a"], "ITEM-1", "野餐垫")

        import types

        import backend.app.api.ai as ai_mod
        import backend.app.services.product_polish_service as polish_svc

        # `_business_model_context` 返回 (model_config, api_key)；假 model_config 只需四个标量
        fake_cfg = types.SimpleNamespace(
            base_url="http://fake.example/v1", model_name="fake-model", model_key=""
        )
        monkeypatch.setattr(ai_mod, "_business_model_context", lambda *a, **k: (fake_cfg, "fake-key"))

        # 两次调用（标题 / 卖点）各回 3 条分隔候选
        contents = iter(
            [
                "野餐垫防潮便携|户外野餐垫加厚|露营野餐垫防水",
                "加厚防潮|可折叠便携|耐磨易清洁",
            ]
        )
        monkeypatch.setattr(
            polish_svc, "_complete", lambda **k: next(contents)
        )
        monkeypatch.setattr(polish_svc, "_split_candidates", lambda text, count=3: text.split("|"))

        response = ctx.client.post(
            "/api/xhs/qianfan/items/polish",
            json={"platform_account_id": ids["a"], "item_id": "ITEM-1"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["title_candidates"]) == 3
        assert len(body["selling_point_candidates"]) == 3

        # ⚠️ 润色是「草稿」，没采纳 ⇒ 库里 ai_title 仍是 NULL
        db = db_factory()
        try:
            row = db.scalars(select(ShopProduct).where(ShopProduct.item_id == "ITEM-1")).one()
            assert row.ai_title is None, "润色结果不得直接落库"
        finally:
            db.close()


class TestGuards:
    """结构守卫（设计 20 §6 最后一行 + 变异点④）。"""

    def test_product_groups_table_is_not_registered_in_metadata(self):
        """**反向**守卫：`product_groups` **不许**在 `Base.metadata`。

        ⚠️ 2026-10-05 分组彻底移除 —— 原判据是「必须在」，现在翻成「必须不在」。
        理由同 v2.0 的 `ModelConfig`：ORM 类不删 ⇒ `create_all()` 会把迁移已 drop
        的表建回来（测试里重建、真机上是「删了又回来」）。
        """
        assert "product_groups" not in Base.metadata.tables

    def test_shop_products_has_m4_columns(self):
        """`shop_products` 的编辑列必须都在（`group_id` 已退场，2026-10-05）。"""
        cols = set(Base.metadata.tables["shop_products"].columns.keys())
        assert {"ai_title", "ai_selling_point"} <= cols
        assert "group_id" not in cols, "`group_id` 已随迁移 `h1i2j3k4l5m6` 删除"

    def test_polish_chain_does_not_bypass_business_provider(self):
        """润色链路**不得旁路直连上游**（变异点④的承重面）。

        ⚠️ 变异：端点或服务层里 `OpenAI(...)` 直连 ⇒ 本用例必红。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        endpoint_src = (
            root / "backend" / "app" / "api" / "platforms" / "xhs" / "qianfan.py"
        ).read_text(encoding="utf-8")
        service_src = (
            root / "backend" / "app" / "services" / "product_polish_service.py"
        ).read_text(encoding="utf-8")

        for src in (endpoint_src, service_src):
            assert "OpenAI(" not in src, "不得旁路直连上游（必须走业务点映射）"
        # 服务层通过注入的 client + resolve_context 拿模型（不自己解析服务商）
        assert "resolve_context" in service_src

    def test_export_limit_constant_is_sane(self):
        """导出上限常量存在且为正（防「上限被删成 None」这类静默退化）。"""
        assert isinstance(MAX_EXPORT_ROWS, int)
        assert MAX_EXPORT_ROWS > 0

    def test_export_header_never_contains_raw_chinese(self):
        """源码层守卫：`Content-Disposition` 不得拼裸中文（DEF-091 的根因形态）。

        ⚠️ 变异：把 header 改回 `filename="商品台账-…"` ⇒ 本用例必红
        （比「跑一次导出」更早、更直接地抓住根因）。
        """
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[2]
            / "backend"
            / "app"
            / "api"
            / "platforms"
            / "xhs"
            / "qianfan.py"
        ).read_text(encoding="utf-8")
        assert 'filename="商品台账' not in src, "中文名不得直接进响应头（latin-1 编码会崩）"
        assert "filename*=UTF-8''" in src, "必须走 RFC 5987 编码"
