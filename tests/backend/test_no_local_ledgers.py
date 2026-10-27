"""§5 不变量：商品 / 订单 / 笔记↔商品三域**本地零台账**（设计 75 S5）。

这个文件守的是**一类**缺陷：去本地化之后，某个角落又悄悄把副本写回去 / 读回来。
那种回归**不会报错**——它只是让页面显示一份旧数据，几周后才有人发现「跟平台对不上」。

四段结构（`docs/04_测试文档/12`）：① 鉴权面不适用（无新端点）⇒ 换成**表集合不变量**；
② 主链路成功（实时查询链路能跑）；③ 关键失败面（平台查不到 ≠ 500）；
④ 不变量（metadata 无台账表 / 源码无台账引用 / 410 墓碑仍在）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.core.database import Base
from backend.app.models.platform_account import PlatformAccount

#: 设计 75 S5 整表 DROP 的九张表。**加回来就红**（`create_all` 会把它们建回来）。
DROPPED_TABLES = (
    "shop_products",
    "note_goods",
    "shop_note_goods",
    "orders",
    "order_items",
    "order_shipments",
    "after_sales",
    "shop_metric_snapshots",
    "topic_goods_match",
)

#: 这三域的 ORM 类名 —— `models/__init__.py` 里不该再注册任何一个。
DROPPED_MODELS = (
    "ShopProduct",
    "NoteGoods",
    "ShopNoteGoods",
    "Order",
    "OrderItem",
    "OrderShipment",
    "AfterSale",
    "ShopMetricSnapshot",
    "TopicGoodsMatch",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP = _REPO_ROOT / "backend" / "app"


@pytest.fixture()
def user_and_account(client, db):
    """一个用户 + 一个带 ark 令牌的 xhs 账号 ⇒ `(请求头, account_id)`。

    复用 `test_shop_items_realtime` 的造法是**错的**（fixture 不跨模块共享），
    这里只造这份用例需要的最小集合：注册 + 登录 + 账号行（平台查询已被打桩，
    令牌内容不需要真实有效）。
    """
    from backend.app.models.platform_account import PlatformAccount

    registered = client.post(
        "/api/auth/register", json={"username": "ledger-user", "password": "secret123"}
    )
    assert registered.status_code == 200, registered.text
    login = client.post(
        "/api/auth/login", json={"username": "ledger-user", "password": "secret123"}
    )
    assert login.status_code == 200, login.text
    account = PlatformAccount(
        user_id=login.json()["user"]["id"],
        platform="xhs",
        sub_type="qianfan",
        nickname="ledger-店",
        external_user_id="ext-ledger",
    )
    db.add(account)
    db.flush()
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, account.id


class TestSegment1_NoLedgerTables:
    """① 表集合不变量（替代鉴权面 —— 本卡无新端点）。"""

    @pytest.mark.parametrize("table", DROPPED_TABLES)
    def test_ledger_table_is_gone_from_metadata(self, table):
        assert table not in Base.metadata.tables, (
            f"`{table}` 又回到 metadata 里了 —— 台账已 DROP，类加回来 `create_all()` 会把空表建回去，"
            "然后谁也不会发现它永远是空的"
        )

    @pytest.mark.parametrize("model", DROPPED_MODELS)
    def test_model_class_is_not_registered(self, model):
        from backend.app import models as models_pkg

        assert not hasattr(models_pkg, model), (
            f"`{model}` 仍从 `backend.app.models` 导得出 —— 已退场的 ORM 类不该留在转出面"
        )

    def test_retained_tables_point_at_platform_item_id(self):
        """保留表用**平台 `item_id`**，不再引用台账主键。"""
        for table in ("generation_records", "note_generation_links"):
            columns = set(Base.metadata.tables[table].columns.keys())
            assert "item_id" in columns, f"{table} 缺少平台 item_id 列"
            assert "shop_product_id" not in columns, (
                f"{table}.shop_product_id 还在 —— 它指向已 DROP 的 shop_products"
            )


class TestSegment2_RealtimePathWorks:
    """② 主链路：只读服务层拿到的就是**平台**数据，且链路里没有写库动作。"""

    def test_list_products_normalizes_platform_rows(self, db):
        """`rt.list_products` 把平台行归一化后直接返回 —— 不落库、不缓存。"""
        from backend.app.services import realtime_platform_query_service as rt

        class _Adapter:
            def search_items(self, **kwargs):
                return True, "", {
                    "data": {
                        "items": [{"item_id": "ITEM-1", "item_name": "柜子", "status": 0}],
                        "total": 1,
                    }
                }

        page = rt.list_products(_Adapter(), page=1, page_size=20)
        assert [row["item_id"] for row in page["items"]] == ["ITEM-1"]
        assert page["source"] == rt.SOURCE_PLATFORM
        #: 判据：本函数**零 ORM 依赖**（import 段里没有台账模型，也就不可能写库）。
        source = (_APP / "services" / "realtime_platform_query_service.py").read_text(
            encoding="utf-8"
        )
        for name in ("ShopProduct", "NoteGoods", "ShopNoteGoods", "Order", "AfterSale"):
            assert f"models.{name}" not in source and f"import {name}" not in source


class TestSegment3_FailureSurfaces:
    """③ 关键失败面：平台查不到 ≠ 500、≠ 拿错商品。"""

    def test_missing_product_raises_structured_not_found(self, db):
        from backend.app.services.product_note_gen_service import (
            ProductNoteGenerationError,
            _product_row,
        )

        class _Adapter:
            def search_items(self, **kwargs):
                return True, "", {"data": {"items": [], "total": 0}}

        class _Account:
            id = 1
            user_id = 1

        with pytest.raises(ProductNoteGenerationError) as info:
            _product_row(db, _Account(), "ITEM-MISSING", adapter=_Adapter())
        #: 没有 ark 令牌的账号先撞门禁 —— 这是**确定性答案**，同样是结构化错误（非 500）。
        assert info.value.code in {"QIANFAN_ITEM_NOT_FOUND", "ARK_SESSION_REQUIRED"}

    def test_keyword_hit_that_is_not_the_target_is_still_not_found(self, db, monkeypatch):
        """关键词命中了**别的**商品 ⇒ 仍算查不到（不许拿同名商品的价和图）。"""
        from backend.app.services import product_note_gen_service as pngs

        class _Adapter:
            def search_items(self, **kwargs):
                return True, "", {"data": {"items": [{"item_id": "OTHER", "item_name": "柜子"}]}}

        class _Account:
            id = 1
            user_id = 1

        monkeypatch.setattr(pngs, "has_ark_session", lambda db_, account_id: True)
        with pytest.raises(pngs.ProductNoteGenerationError) as info:
            pngs._product_row(db, _Account(), "ITEM-WANTED", adapter=_Adapter())
        assert info.value.code == "QIANFAN_ITEM_NOT_FOUND"


class TestSegment4_Invariants:
    """④ 不变量：源码里不该再有台账写法；墓碑仍在。"""

    #: 「读/写台账」的写法特征。**剥注释后**才判（本仓 docstring 大量引用旧表名做说明）。
    _LEDGER_WRITE = re.compile(
        r"(?:db\.add\(\s*(?:ShopProduct|NoteGoods|ShopNoteGoods|Order|OrderShipment|AfterSale)"
        r"|delete\(\s*(?:ShopProduct|NoteGoods|ShopNoteGoods|OrderItem|OrderShipment|AfterSale)"
        r"|from backend\.app\.models\.shop_product import)"
    )

    def test_no_module_writes_the_dropped_ledgers(self):
        offenders: list[str] = []
        for path in sorted(_APP.rglob("*.py")):
            if path.name in {"shop.py", "order.py", "__init__.py"}:
                continue  # 退场说明 / 状态常量，本身就在讲这些名字
            source = path.read_text(encoding="utf-8")
            code = re.sub(r'""".*?"""', "", source, flags=re.S)
            code = re.sub(r"#.*", "", code)
            if self._LEDGER_WRITE.search(code):
                offenders.append(str(path.relative_to(_REPO_ROOT)))
        assert not offenders, f"这些模块仍在读写已退场的台账：{offenders}"

    def test_sync_endpoints_are_tombstones(self):
        """「同步商品 / 同步笔记 / 删台账」这些**写本地台账**的端点必须不在 OpenAPI 里。

        ⚠️ 2026-10-27 口径变更（用户原话「批量同步商品信息逻辑全链路彻底移除」）：
        原来是「保留墓碑回 410」，现在**连墓碑一起删掉** —— `/items/sync` 与
        `/items/delete` 的路由表里都不该再有这条路径。
        理由：商品列表已是平台实时数据，不存在「同步回本地」这个动作，
        留个 410 反而让人以为「以后会恢复」。
        """
        from backend.app.main import app

        paths = app.openapi()["paths"]
        for path in (
            "/api/accounts/{account_id}/notes/sync",
            "/api/accounts/{account_id}/goods-notes/sync",
            "/api/xhs/qianfan/items/sync",
            "/api/xhs/qianfan/items/delete",
        ):
            assert path not in paths, f"{path} 又回到 OpenAPI 了（同步/台账写端点应保持墓碑）"
        #: 墓碑仍在路由表里（不然客户端拿到 404，分不清「没有这个功能」和「路径写错」）。
        #: 走 `app.openapi()` 拿不到墓碑（`include_in_schema=False`）⇒ 直接查 router 的
        #: `routes`（挂载点内部的那批）。
        from backend.app.api.platforms.xhs.qianfan import router as qianfan_router

        #: `include_router` 之后每层都是 `_IncludedRouter` 包装，真正的 route 在
        #: `original_router` 里（递归展开，别只剥一层）。
        def _flatten(routes, depth=0):
            if depth > 6:
                return
            for route in routes:
                inner = getattr(route, "original_router", None)
                if inner is not None:
                    yield from _flatten(inner.routes, depth + 1)
                else:
                    yield route

        registered = {getattr(route, "path", "") for route in _flatten(qianfan_router.routes)}
        assert "/items/sync" not in registered, (
            "「同步商品」端点又回来了 —— 商品列表是平台实时数据，不该再有同步链路"
        )

    def test_route_permission_guard_still_passes(self):
        """退场端点的权限码必须**同时**摘掉 —— 否则启动自检拒绝启动。

        ⚠️ 这是本轮实测踩到的真实故障：端点改 `include_in_schema=False` 后仍是路由，
        但 `route_perms` 里的映射键指向的是**旧形态**；两边一对不上，
        `RouteGuardError: [码指向不存在的路由]` 会让**整个应用起不来**。
        这类断裂在跑测试时不会报（守卫只在 `lifespan` 里调），所以单独立一条。
        """
        from backend.app.core.route_guard import collect_violations
        from backend.app.main import app

        assert collect_violations(app) == []
