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

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.api.platforms.xhs.qianfan import MAX_EXPORT_ROWS
from backend.app.core.database import Base, get_db
from backend.app.main import app
from backend.app.models import PlatformAccount, User
from backend.app.models.shop_product import ShopProduct

client = TestClient(app)


def _setup(tmp_path):
    """建临时库 + 两用户（owner / intruder）+ owner 的两个平台账号。

    ⚠️ **两个账号是刻意的** —— `product_count` 的跨账号聚合只有两个账号才测得出来
    （单账号时「跨账号聚合」与「只数当前账号」结果恒等，变异抓不住）。
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'product-edit.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    def register(username: str) -> str:
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "secret123"}
        )
        assert response.status_code == 200, response.text
        return response.json()["access_token"]

    owner_headers = {"Authorization": f"Bearer {register('owner')}"}
    intruder_headers = {"Authorization": f"Bearer {register('intruder')}"}

    db = SessionLocal()
    try:
        owner_id = db.scalar(select(User.id).where(User.username == "owner"))
        intruder_id = db.scalar(select(User.id).where(User.username == "intruder"))
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
    return owner_headers, intruder_headers, ids, SessionLocal


def _add_product(SessionLocal, account_id: int, item_id: str, title: str = "测试商品", **cols):
    """往台账塞一行（走 `store_products` —— 与生产同一条落库路径）。

    ⚠️ **不走裸 `ShopProduct(...)`**：列表 / 导出的字段全部从 `raw_json` 派生
    （`normalize_item_from_raw(_parse_raw(row))`）⇒ 直接建 ORM 行会得到一行空值
    （实测：导出 1 行全空、列表 0 行）。
    """
    from backend.app.models import PlatformAccount
    from backend.app.services.shop_goods_sync_service import store_products

    db = SessionLocal()
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


class TestGroupCrud:
    """分组 CRUD + 跨账号计数（设计 20 §2.1 端点 1–4）。"""

    def test_create_rename_delete_with_release_count(self, tmp_path):
        headers, _, ids, SessionLocal = _setup(tmp_path)

        created = client.post(
            "/api/xhs/qianfan/product-groups", json={"name": "春季上新"}, headers=headers
        )
        assert created.status_code == 201, created.text
        gid = created.json()["id"]
        assert created.json()["name"] == "春季上新"

        listed = client.get("/api/xhs/qianfan/product-groups", headers=headers)
        assert listed.status_code == 200
        assert [g["id"] for g in listed.json()["items"]] == [gid]

        renamed = client.patch(
            f"/api/xhs/qianfan/product-groups/{gid}", json={"name": "夏季上新"}, headers=headers
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "夏季上新"

        # 挂两件商品进分组，删分组必须报告影响面（released = 2）
        _add_product(SessionLocal, ids["a"], "ITEM-1", group_id=gid)
        _add_product(SessionLocal, ids["a"], "ITEM-2", group_id=gid)
        deleted = client.delete(f"/api/xhs/qianfan/product-groups/{gid}", headers=headers)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["released"] == 2

        # ⚠️ 商品一件不删，只是没了分组（分组是弱关联）
        db = SessionLocal()
        try:
            rows = db.scalars(select(ShopProduct)).all()
            assert len(rows) == 2
            assert all(r.group_id is None for r in rows)
        finally:
            db.close()

    def test_duplicate_name_conflicts_not_silently_overwritten(self, tmp_path):
        headers, _, _, _ = _setup(tmp_path)
        first = client.post(
            "/api/xhs/qianfan/product-groups", json={"name": "重名组"}, headers=headers
        )
        assert first.status_code == 201

        again = client.post(
            "/api/xhs/qianfan/product-groups", json={"name": "重名组"}, headers=headers
        )
        assert again.status_code == 409, again.text
        # 建完重名之后仍然只有一行（不静默覆盖 = 不产生第二行也不改第一行）
        listed = client.get("/api/xhs/qianfan/product-groups", headers=headers).json()
        assert len(listed["items"]) == 1

    def test_other_users_group_is_invisible(self, tmp_path):
        """跨用户隔离：B 动 A 的分组 ⇒ 404（不是 403、不是 200）。"""
        headers, intruder_headers, _, _ = _setup(tmp_path)
        gid = client.post(
            "/api/xhs/qianfan/product-groups", json={"name": "A的组"}, headers=headers
        ).json()["id"]

        assert (
            client.patch(
                f"/api/xhs/qianfan/product-groups/{gid}",
                json={"name": "改别人的"},
                headers=intruder_headers,
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/api/xhs/qianfan/product-groups/{gid}", headers=intruder_headers
            ).status_code
            == 404
        )
        assert (
            client.get("/api/xhs/qianfan/product-groups", headers=intruder_headers).json()["items"]
            == []
        )

    def test_product_count_aggregates_across_own_accounts(self, tmp_path):
        """`product_count` 数的是**该用户全部账号**的商品（变异点①）。

        ⚠️ 变异：改成「只数当前账号」⇒ 本用例必红（账号 B 那件漏计）。
        """
        headers, _, ids, SessionLocal = _setup(tmp_path)
        gid = client.post(
            "/api/xhs/qianfan/product-groups", json={"name": "跨账号组"}, headers=headers
        ).json()["id"]

        _add_product(SessionLocal, ids["a"], "ITEM-A", group_id=gid)
        _add_product(SessionLocal, ids["b"], "ITEM-B", group_id=gid)

        listed = client.get("/api/xhs/qianfan/product-groups", headers=headers).json()
        row = next(g for g in listed["items"] if g["id"] == gid)
        assert row["product_count"] == 2, "账号 A + 账号 B 各一件"


class TestProductEdit:
    """`PATCH /items/edit` 的 PATCH 语义（设计 20 §2.2 口径①）。"""

    def test_omitted_key_keeps_value_and_null_clears(self, tmp_path):
        """**两种语义分开测**（变异点②）。

        ⚠️ 变异：把 sentinel 改成 `Optional[..., None]` ⇒ 省略键会被当显式 null
        清空 ⇒ 本用例第一段必红。
        """
        headers, _, ids, SessionLocal = _setup(tmp_path)
        gid = client.post(
            "/api/xhs/qianfan/product-groups", json={"name": "组1"}, headers=headers
        ).json()["id"]
        _add_product(SessionLocal, ids["a"], "ITEM-1", ai_title="原标题", ai_selling_point="原卖点")

        # ① 只传 group_id ⇒ 另外两项**保持原值**（不传 = 不动）
        only_group = client.patch(
            "/api/xhs/qianfan/items/edit",
            json={"platform_account_id": ids["a"], "item_id": "ITEM-1", "group_id": gid},
            headers=headers,
        )
        assert only_group.status_code == 200, only_group.text
        body = only_group.json()
        assert body["group_id"] == gid
        assert body["ai_title"] == "原标题", "省略的键必须保持原值"
        assert body["ai_selling_point"] == "原卖点"
        assert body["updated"] == ["group_id"]

        # ② 显式传 null ⇒ 清空
        cleared = client.patch(
            "/api/xhs/qianfan/items/edit",
            json={"platform_account_id": ids["a"], "item_id": "ITEM-1", "ai_title": None},
            headers=headers,
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["ai_title"] is None, "显式 null 必须清空"
        assert cleared.json()["ai_selling_point"] == "原卖点", "没传的那项仍不动"
        assert cleared.json()["updated"] == ["ai_title"]

    def test_updated_lists_only_changed_fields(self, tmp_path):
        """`updated[]` 只列**真正变了**的列（送回同值 ⇒ 空数组）。"""
        headers, _, ids, SessionLocal = _setup(tmp_path)
        _add_product(SessionLocal, ids["a"], "ITEM-1", ai_title="同值")

        same = client.patch(
            "/api/xhs/qianfan/items/edit",
            json={"platform_account_id": ids["a"], "item_id": "ITEM-1", "ai_title": "同值"},
            headers=headers,
        )
        assert same.status_code == 200, same.text
        assert same.json()["updated"] == [], "值没变就不该报「已更新」"

        changed = client.patch(
            "/api/xhs/qianfan/items/edit",
            json={"platform_account_id": ids["a"], "item_id": "ITEM-1", "ai_title": "新值"},
            headers=headers,
        )
        assert changed.json()["updated"] == ["ai_title"]

    def test_editing_other_accounts_item_is_404(self, tmp_path):
        headers, _, ids, SessionLocal = _setup(tmp_path)
        _add_product(SessionLocal, ids["foreign"], "ITEM-X")

        # owner 拿自己的账号 A 去改别人账号的商品 ⇒ 台账里查不到 ⇒ 404
        response = client.patch(
            "/api/xhs/qianfan/items/edit",
            json={"platform_account_id": ids["a"], "item_id": "ITEM-X", "ai_title": "偷改"},
            headers=headers,
        )
        assert response.status_code == 404, response.text

    def test_group_id_must_belong_to_caller(self, tmp_path):
        """把商品挂到**别人的分组 id** ⇒ 404，不静默收下。"""
        headers, intruder_headers, ids, SessionLocal = _setup(tmp_path)
        foreign_gid = client.post(
            "/api/xhs/qianfan/product-groups", json={"name": "别人的组"}, headers=intruder_headers
        ).json()["id"]
        _add_product(SessionLocal, ids["a"], "ITEM-1")

        response = client.patch(
            "/api/xhs/qianfan/items/edit",
            json={"platform_account_id": ids["a"], "item_id": "ITEM-1", "group_id": foreign_gid},
            headers=headers,
        )
        assert response.status_code == 404, response.text


class TestExport:
    """CSV 导出（设计 20 §2.1 端点 6 + §4.5）。"""

    def test_export_matches_list_selection_and_has_bom(self, tmp_path):
        """导出集合 = 列表集合（同一筛选），且带 UTF-8 BOM + 中文文件名不崩（DEF-091）。"""
        headers, _, ids, SessionLocal = _setup(tmp_path)
        _add_product(SessionLocal, ids["a"], "ITEM-1", "野餐垫")
        _add_product(SessionLocal, ids["a"], "ITEM-2", "防晒衣")

        listed = client.post(
            "/api/xhs/qianfan/items",
            json={"platform_account_id": ids["a"], "keyword": "野餐"},
            headers=headers,
        )
        assert listed.status_code == 200, listed.text
        total = listed.json()["total"]

        exported = client.post(
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

    def test_export_falls_back_to_platform_title_when_no_ai_title(self, tmp_path):
        """`ai_title` 有值导它、没有导 `item_title`（用户要的是「能用的标题」）。"""
        headers, _, ids, SessionLocal = _setup(tmp_path)
        _add_product(SessionLocal, ids["a"], "ITEM-1", "原标题", ai_title="AI标题")
        _add_product(SessionLocal, ids["a"], "ITEM-2", "另一个原标题")

        exported = client.post(
            "/api/xhs/qianfan/items/export",
            json={"platform_account_id": ids["a"]},
            headers=headers,
        )
        text = exported.content.decode("utf-8-sig")
        assert "AI标题" in text
        assert "另一个原标题" in text

    def test_export_over_limit_is_rejected_not_truncated(self, tmp_path, monkeypatch):
        """超限**整体拒绝**（变异点③）。

        ⚠️ 变异：去掉上限判定（静默截断）⇒ 本用例必红 —— 截断的 CSV 是
        「看起来完整」的假全量，用户会拿它对账。
        """
        headers, _, ids, SessionLocal = _setup(tmp_path)
        # 把上限压到 1 行，造出「超限」确定态（不用真塞 5000 行）
        import backend.app.api.platforms.xhs.qianfan as qf

        monkeypatch.setattr(qf, "MAX_EXPORT_ROWS", 1)
        _add_product(SessionLocal, ids["a"], "ITEM-1")
        _add_product(SessionLocal, ids["a"], "ITEM-2")

        response = client.post(
            "/api/xhs/qianfan/items/export",
            json={"platform_account_id": ids["a"]},
            headers=headers,
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "QIANFAN_EXPORT_TOO_MANY"
        assert response.json()["detail"]["limit"] == 1

    def test_export_is_account_scoped(self, tmp_path):
        headers, _, ids, SessionLocal = _setup(tmp_path)
        _add_product(SessionLocal, ids["a"], "ITEM-1", "自己的商品")
        _add_product(SessionLocal, ids["foreign"], "ITEM-X", "别人的商品")

        exported = client.post(
            "/api/xhs/qianfan/items/export",
            json={"platform_account_id": ids["a"]},
            headers=headers,
        )
        text = exported.content.decode("utf-8-sig")
        assert "自己的商品" in text
        assert "别人的商品" not in text


class TestPolish:
    """`POST /items/polish` 的 AI 润色（设计 20 §3）。"""

    def test_polish_without_provider_is_structured_400(self, tmp_path):
        """未配服务商 ⇒ **400 结构化**（不回落、不静默用别的服务商）。"""
        headers, _, ids, SessionLocal = _setup(tmp_path)
        _add_product(SessionLocal, ids["a"], "ITEM-1", "野餐垫")

        response = client.post(
            "/api/xhs/qianfan/items/polish",
            json={"platform_account_id": ids["a"], "item_id": "ITEM-1"},
            headers=headers,
        )
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert isinstance(detail, dict), "失败必须结构化（不是裸字符串）"
        assert detail.get("code")

    def test_polish_returns_three_candidates_and_does_not_persist(self, tmp_path, monkeypatch):
        """备选各 3 个 + **润色不落库**（采纳才落）。"""
        headers, _, ids, SessionLocal = _setup(tmp_path)
        _add_product(SessionLocal, ids["a"], "ITEM-1", "野餐垫")

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

        response = client.post(
            "/api/xhs/qianfan/items/polish",
            json={"platform_account_id": ids["a"], "item_id": "ITEM-1"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["title_candidates"]) == 3
        assert len(body["selling_point_candidates"]) == 3

        # ⚠️ 润色是「草稿」，没采纳 ⇒ 库里 ai_title 仍是 NULL
        db = SessionLocal()
        try:
            row = db.scalars(select(ShopProduct).where(ShopProduct.item_id == "ITEM-1")).one()
            assert row.ai_title is None, "润色结果不得直接落库"
        finally:
            db.close()


class TestGuards:
    """结构守卫（设计 20 §6 最后一行 + 变异点④）。"""

    def test_product_groups_table_registered_in_metadata(self):
        """`product_groups` 必须在 `Base.metadata`（防 ORM 类漏建 ⇒ 与迁移分叉）。"""
        assert "product_groups" in Base.metadata.tables
        assert {"id", "user_id", "name"} <= set(
            Base.metadata.tables["product_groups"].columns.keys()
        )

    def test_shop_products_has_m4_columns(self):
        """`shop_products` 加的三列必须都在。"""
        cols = set(Base.metadata.tables["shop_products"].columns.keys())
        assert {"group_id", "ai_title", "ai_selling_point"} <= cols

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
