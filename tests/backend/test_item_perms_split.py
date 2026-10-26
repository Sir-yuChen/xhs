"""商品列表「生成笔记 / 上架 / 下架」三码拆分守卫（2026-10-26 用户口径）。

## 红线原文

> 「商品列表的『生成笔记』与『上架下架』拆成两个码，是按岗位合并还是保持，待你定，
>    **应拆分成三个权限码**」

## 这条红线要拦的是什么

原实现是 `POST /items/operate` 只挂一个 `xhs:item:operate`，而「生成笔记」按钮
判的是 `xhs:item:aiAssist`（AI 商品文案的码，与生成八竿子打不着）。后果：

* 想让某人「上架」⇒ 必须连「**删除平台商品**」一起授予（同一个勾）；
* 想让某人「生成笔记」⇒ 必须连「AI 写标题/类目/属性」一起授予。

两处都**不会报错**：授权页上就是一个勾，谁也不知道那个勾里包着什么。
这正是红线「所有按钮的权限必须独立，颗粒度到按钮级别」要拦的形态。

## 判据分布在三处，三处都要测（少测一处就会出现「登记了但不生效」）

| 层 | 判据 | 不测的后果 |
|---|---|---|
| 登记表 | 三个动作三个**互不相同**的码 | 有人图省事改回一个码，守卫照样绿 |
| 路由表 + 源码依赖 | 端点**真的**挂了 `require_perm` 且按 `action` 选码 | 登记表全绿而接口完全敞开（本仓实测 279 条写路由只有 25 条真挂） |
| 前端 | 每个按钮各判各的码 | 没权限的用户看到能点、点了才 403 |
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: 复用那个文件的重型夹具（账号 + ark 登录态 + 平台接缝打桩）。
#: pytest 允许跨模块引用夹具名 —— 这里显式 import 是为了让「用的是哪份夹具」可见。
from tests.backend.test_shop_item_operate import AllowLimiter, LIMITER, env  # noqa: F401

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


class TestRegistryAndRoutes:
    """登记表与路由表：三个码各自存在、合并码整条退场。"""

    def test_three_actions_map_to_three_distinct_codes(self):
        from backend.app.api.platforms.xhs.qianfan import goods

        mapping = goods._OPERATE_ACTION_PERMS
        assert mapping == {
            "on_shelf": "xhs:item:onShelf",
            "off_shelf": "xhs:item:offShelf",
            "delete": "xhs:item:delete",
        }
        assert len(set(mapping.values())) == 3, "三个动作必须三个不同的码"

    def test_merged_operate_code_is_gone_everywhere(self):
        """`xhs:item:operate` 必须从登记表 / 路由表 / 菜单种子 / 前端全部消失。"""
        from backend.app.core import perms_registry, route_perms

        assert "xhs:item:operate" not in {spec.code for spec in perms_registry.PERMS}
        assert "xhs:item:operate" not in set(route_perms.ROUTE_PERMS.values())

        for rel in (
            "config/initial_data/menus.seed.json",
            "frontend/src/pages/platforms/xhs/shop-items-page.tsx",
        ):
            text = (ROOT / rel).read_text(encoding="utf-8")
            #: 只看**代码**，注释里提到"原 `xhs:item:operate`"是在说明历史，
            #: 不是还在用（本仓 docstring 大量写反例，不剥注释会自命中）。
            code = "\n".join(
                ln for ln in text.splitlines()
                if not ln.lstrip().startswith(("#", "//", "/*", "*"))
            )
            assert "xhs:item:operate" not in code, f"{rel} 里还留着合并码"

    def test_generate_note_code_guards_every_generate_route(self):
        """商品列表的生成链路四个端点都挂 `xhs:item:generateNote`，不再是工坊码。"""
        from backend.app.core.route_perms import ROUTE_PERMS

        for key in (
            "POST /api/xhs/product-notes/generate-save",
            "POST /api/xhs/product-notes/generate-llm",
            "POST /api/xhs/product-notes/generate-llm/{record_id}/retry",
            "POST /api/xhs/product-notes/batch-generate",
        ):
            assert ROUTE_PERMS[key] == "xhs:item:generateNote", key
        #: 干运行（给草稿工坊看一版，不落库）仍属工坊 —— 它不是商品列表的按钮。
        assert ROUTE_PERMS["POST /api/xhs/product-notes/generate"] == "xhs:workshop:generate"

    def test_generate_routes_really_depend_on_the_code(self):
        """登记 ≠ 挂上：这些端点必须真的有 `Depends(require_perm(...))`。

        ⚠️ 全仓实测（2026-10-26）：279 条写路由里只有 25 条真挂了依赖，
        其余「只登记不挂码」—— 那种情况下 `ROUTE_PERMS` 全绿而接口完全敞开。
        所以本用例直接扫**源码**确认依赖存在，不靠登记表。
        """
        src = (ROOT / "backend/app/api/platforms/xhs/product_notes.py").read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        for route in (
            "/generate-save",
            "/generate-llm",
            "/generate-llm/{record_id}/retry",
            "/batch-generate",
        ):
            start = code.index(f'@router.post("{route}")')
            end = code.index("):", start)
            block = code[start:end]
            assert 'require_perm("xhs:item:generateNote")' in block, (
                f"{route} 没有真的挂 require_perm —— 登记表绿但接口敞开"
            )

    def test_local_delete_has_its_own_code(self):
        """删**本地台账行**不再借 `edit` 的码（能改 ≠ 能删）。"""
        from backend.app.core.route_perms import ROUTE_PERMS

        assert ROUTE_PERMS["POST /api/xhs/qianfan/items/delete"] == "xhs:item:deleteLocal"

    def test_frontend_buttons_each_check_their_own_code(self):
        """前端：生成笔记 / 上架 / 下架 / 删除 / 删台账行**各自**判权。"""
        page = (ROOT / "frontend/src/pages/platforms/xhs/shop-items-page.tsx").read_text(
            encoding="utf-8"
        )
        for code in (
            "xhs:item:generateNote",
            "xhs:item:onShelf",
            "xhs:item:offShelf",
            "xhs:item:delete",
            "xhs:item:deleteLocal",
        ):
            assert f'hasPermission("{code}")' in page or f'perm: "{code}"' in page, (
                f"前端没有按 {code} 判权（按钮会对所有人展示）"
            )

        #: ⚠️ **只查「码出现过」不够**（实测变异逃逸过）：把
        #: `{shelfAction && shelfPermitted ?` 改回 `{shelfAction ?` 时，
        #: `hasPermission("xhs:item:onShelf")` 仍在 `shelfPermitted` 的计算里 ⇒
        #: 上面那条照样绿，而按钮已经对所有人展示了。
        #: ⇒ 判据必须落到**渲染条件**上：上架/下架按钮的条件里要出现权限变量。
        shelf_render = re.search(r"\{shelfAction\s*&&\s*\w*[Pp]ermitted\s*\?", page)
        assert shelf_render, (
            "上架/下架按钮的渲染条件里没有权限变量（`{shelfAction ?` = 对所有人展示）"
        )

        #: 三个批量按钮同理：必须是 `filter((entry) => hasPermission(entry.perm))`，
        #: 不是整块一个 `hasPermission(...) &&`（那又回到一码管三个按钮）。
        assert ".filter((entry) => hasPermission(entry.perm))" in page, (
            "批量上架/下架/删除没有各判各的码（应 filter 而非整块 &&）"
        )

        #: 「生成笔记」与「LLM 生成」两个按钮都必须在 `mayGenerate` 条件下。
        assert page.count("{mayGenerate && (") >= 2, (
            "生成笔记 / LLM 生成 没有各自被 mayGenerate 条件渲染"
        )


class TestEndpointEnforcesTheChosenCode:
    """端点：按 `action` 选码再判权 —— 有**别的动作**的码也不行。"""

    @pytest.mark.parametrize(
        ("action", "own_code", "other_code"),
        [
            ("on_shelf", "xhs:item:onShelf", "xhs:item:offShelf"),
            ("off_shelf", "xhs:item:offShelf", "xhs:item:onShelf"),
            ("delete", "xhs:item:delete", "xhs:item:onShelf"),
        ],
    )
    def test_action_requires_its_own_code_not_a_sibling(
        self, env, api_client, monkeypatch, action, own_code, other_code
    ):
        """只有**兄弟动作**的码 ⇒ 403，且 `perm` 指出缺的是哪一条。

        ⚠️ 这条正是「拆分有没有真的生效」的判据：实现一旦退化成
        「有一个码就放行」，给了 `other_code` 也会 200，本用例立刻红。
        """
        headers, account_id, _user_id, _db_factory = env
        from backend.app.core import permission_cache

        base = permission_cache.perms_of

        def only_sibling(db, user):  # noqa: ANN001, ANN202
            return (frozenset(base(db, user)) - {own_code}) | {other_code}

        monkeypatch.setattr(permission_cache, "perms_of", only_sibling)

        response = api_client.post(
            "/api/xhs/qianfan/items/operate",
            json={"platform_account_id": account_id, "action": action, "item_ids": ["A"]},
            headers=headers,
        )
        assert response.status_code == 403, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "PERMISSION_DENIED"
        assert detail["perm"] == own_code, "403 必须指出缺的是哪个动作的码"

    @pytest.mark.parametrize(
        ("action", "own_code"),
        [
            ("on_shelf", "xhs:item:onShelf"),
            ("off_shelf", "xhs:item:offShelf"),
            ("delete", "xhs:item:delete"),
        ],
    )
    def test_action_passes_with_its_own_code(self, env, api_client, monkeypatch, action, own_code):
        """有**自己**那个码 ⇒ 放行（不是恒 403 的空守卫）。"""
        headers, account_id, _user_id, _db_factory = env
        from backend.app.core import permission_cache

        base = permission_cache.perms_of

        def only_own(db, user):  # noqa: ANN001, ANN202
            return frozenset(base(db, user)) | {own_code}

        monkeypatch.setattr(permission_cache, "perms_of", only_own)
        LIMITER["impl"] = AllowLimiter()

        response = api_client.post(
            "/api/xhs/qianfan/items/operate",
            json={"platform_account_id": account_id, "action": action, "item_ids": ["A"]},
            headers=headers,
        )
        assert response.status_code == 200, response.text

    def test_unregistered_action_still_400_before_perm(self, env, api_client, monkeypatch):
        """参数错（未知动作）必须报 400，不能被报成 403「没权限」。

        ⚠️ 顺序是判据：先判「认不认得这个动作」，再判「有没有这个动作的码」。
        反过来会让用户去找管理员要一个根本不存在的码。
        """
        headers, account_id, _user_id, _db_factory = env
        from backend.app.core import permission_cache

        monkeypatch.setattr(permission_cache, "perms_of", lambda db, user: frozenset())

        response = api_client.post(
            "/api/xhs/qianfan/items/operate",
            json={"platform_account_id": account_id, "action": "explode", "item_ids": ["A"]},
            headers=headers,
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "QIANFAN_OPERATE_ACTION_UNSUPPORTED"
