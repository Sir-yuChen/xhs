"""商品列表页「去同步化」与弹窗尺寸守卫（2026-10-27 用户口径）。

三件事，每条都要钉住 —— 它们都属于「删了容易被下一次改动悄悄加回来」的类型：

1. **商品详情弹窗 = 视口 70%**（宽高都 70%）；
2. **同步商品信息 / 批量同步商品信息 = 全链路移除**（按钮 / 弹窗 / 请求函数 /
   后端端点 / 队列类型 / 任务类型）；
3. **删除台账行按钮 = 移除**（本地台账表已 DROP，没有「台账行」可删）。

⚠️ 注意别把 3 和「批量删除」（**删平台商品**，`xhs:item:delete`）搞混：
后者是平台写操作，**必须保留**。守卫里两种都断言，防止一次删过头。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

MODAL = Path("frontend/src/pages/platforms/xhs/product-detail-modal.tsx")
PAGE = Path("frontend/src/pages/platforms/xhs/shop-items-page.tsx")
ACCOUNTS = Path("frontend/src/pages/platforms/xhs/accounts-page.tsx")
API_TS = Path("frontend/src/lib/api.ts")
GOODS_API = Path("backend/app/api/platforms/xhs/qianfan/goods.py")
TASK_SVC = Path("backend/app/services/account_task_service.py")


def _code(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"\{/\*.*?\*/\}", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    text = re.sub(r"^\s*#.*$", "", text, flags=re.M)
    return text


def test_detail_modal_is_70_percent_of_viewport() -> None:
    """商品详情弹窗宽高各占视口 70%。"""
    src = _code(MODAL)
    assert re.search(r'width="70vw"', src), "弹窗宽度不是 70vw"
    assert re.search(r'height:\s*"calc\(70vh - \d+px\)"', src), (
        "弹窗高度不是按 70vh 算的（应 `calc(70vh - 头部footer 高度)`）"
    )


def test_sync_buttons_are_gone_from_shop_items_page() -> None:
    """商品列表页不许再有「同步商品信息 / 批量同步商品信息」按钮。"""
    src = _code(PAGE)
    for text in ("同步商品信息", "批量同步商品信息", "商品同步中"):
        assert text not in src, f"「{text}」按钮文案还在"


def test_sync_modal_and_request_are_gone() -> None:
    """同步弹窗文件与请求函数必须不存在（全链路移除）。"""
    assert not Path("frontend/src/pages/platforms/xhs/sync-specified-items-modal.tsx").exists(), (
        "同步指定商品弹窗文件还在（应整块删除）"
    )
    api_src = _code(API_TS)
    for name in ("syncSpecifiedShopItems", "deleteXhsShopItems"):
        assert f"export async function {name}(" not in api_src, f"请求函数 `{name}` 还在"


def test_account_card_goods_sync_button_is_gone() -> None:
    """账号卡上的「同步商品」按钮也不许回来（同一条链路）。"""
    src = _code(ACCOUNTS)
    assert "同步商品" not in src.replace("同步商品笔记", ""), (
        "账号卡还有「同步商品」按钮（应随同步链路一起移除）"
    )


def test_backend_sync_endpoint_and_task_type_are_gone() -> None:
    """后端 `/items/sync` 端点 + `xhs_shop_goods_sync` 任务类型都必须移除。"""
    goods_src = _code(GOODS_API)
    assert '"/items/sync"' not in goods_src, "后端 `/items/sync` 端点又回来了"
    task_src = _code(TASK_SVC)
    assert '"xhs_shop_goods_sync"' not in task_src, (
        "`xhs_shop_goods_sync` 任务类型还在 worker 认领清单里"
    )


def test_delete_ledger_button_is_gone() -> None:
    """「删除台账行」按钮移除（本地台账表已 DROP）。"""
    src = _code(PAGE)
    assert "删除台账行" not in src, "「删除台账行」按钮还在"
    assert "handleBatchDelete" not in src, "`handleBatchDelete` 死代码残留"


def test_batch_delete_platform_items_is_kept() -> None:
    """⚠️ 反面对照：「批量删除」（**删平台商品**）必须**保留**。

    这条是防止「一次删过头」——用户要删的是台账行，不是平台商品。
    """
    src = _code(PAGE)
    assert "handleBatchItemOperate" in src, "「批量删除平台商品」被误删了"
    assert 'title: "批量删除商品"' in src or "批量删除商品" in src, (
        "「批量删除商品」（平台写操作）不见了 —— 它和「删除台账行」是两件事"
    )


class TestEnumAndPermissionCleanup:
    """权限码 / 菜单行 / 商品类型位置（2026-10-27 用户口径）。

    用户原话：「注意删除按钮后相关菜单权限对应的也应该删除，删除就彻底删除包括注释说明」。
    """

    def test_dead_permission_codes_are_gone_from_menu_seed(self) -> None:
        """「删除台账行」「同步商品」两条 F 权限行必须从菜单种子里消失。

        ⚠️ 只删按钮不删权限行 ⇒ 权限授权页还挂着一个「点了没反应」的勾选项，
        而且启动守卫会因为它指向不存在的路由而拒绝启动。
        """
        import json

        seed = json.loads(
            Path("config/initial_data/menus.seed.json").read_text(encoding="utf-8")
        )
        perms = {m.get("perms") for m in seed["menus"]}
        assert "xhs:item:sync" not in perms, "「同步商品」权限行还在菜单种子里"
        assert "xhs:item:deleteLocal" not in perms, "「删除台账行」权限行还在菜单种子里"

    def test_menu_seed_counts_match_reality(self) -> None:
        """菜单种子的 `counts` 声明必须与实际行数一致（历史上漂移过 5 行）。"""
        import json
        from collections import Counter

        seed = json.loads(
            Path("config/initial_data/menus.seed.json").read_text(encoding="utf-8")
        )
        actual = Counter(m.get("menu_type") for m in seed["menus"])
        declared = seed.get("counts") or {}
        for key in ("M", "C", "F"):
            assert declared.get(key) == actual.get(key, 0), (
                f"counts.{key} 声明 {declared.get(key)} ≠ 实际 {actual.get(key, 0)}"
            )
        assert declared.get("total") == len(seed["menus"])

    def test_video_upload_no_longer_borrows_dead_code(self) -> None:
        """视频上传不能再用已退场的 `xhs:item:sync`（否则启动守卫报「码指向不存在路由」）。"""
        src = Path("backend/app/core/route_perms.py").read_text(encoding="utf-8")
        line = next(
            (l for l in src.split("\n") if '"POST /api/xhs/qianfan/video/upload"' in l), ""
        )
        assert line, "video/upload 的路由权限登记不见了"
        assert "xhs:item:sync" not in line, "视频上传还挂在已退场的 `xhs:item:sync` 上"

    def test_item_type_filter_is_in_collapsed_area_only(self) -> None:
        """商品类型筛选**只在折叠区**（「更多筛选」），不占第一行（用户口径 2026-10-27）。

        ⚠️ 判据是「出现在 `searchExpanded` 之后」而不是「存在」—— 需求是「隐藏不展示在
        第一行」，不是删除功能。
        """
        src = _code(PAGE)
        assert 'name="is_channel"' in src, "商品类型筛选被整个删掉了（需求是移进折叠区，不是删除）"
        expand_at = src.index("searchExpanded ? (")
        filter_at = src.index('name="is_channel"')
        assert filter_at > expand_at, "商品类型筛选又回到第一行了（应在「更多筛选」折叠区内）"


class TestNoteBodyRenderedOnce:
    """挂链笔记弹窗的正文 / 发布时间 / 互动数**只渲染一次**（2026-10-27 用户口径：
    「挂链笔记弹窗展示两边笔记正文应只展示一次实时数据即可」）。

    ⚠️ 根因是**两份数据源各画一遍**：列表行（本地 `note.content`）与实时详情块
    （`/note-detail` 的 `desc`）。删掉本地那份，实时那份为唯一口径。
    """

    def test_local_body_is_not_rendered(self) -> None:
        """`renderItemNoteInfo` 不许再渲染 `note.content`。"""
        src = _code(PAGE)
        #: 取该组件函数体
        start = src.index("function renderItemNoteInfo(")
        end = src.index("\n}\n", start)
        body = src[start:end]
        assert "{note.content}" not in body, (
            "列表行还在渲染本地正文 —— 它和实时详情块会各画一遍（弹窗里正文出现两次）"
        )

    def test_local_publish_time_and_metrics_are_not_rendered(self) -> None:
        """发布时间 / 互动数也不许在列表行里再画一遍（实时块已有）。"""
        src = _code(PAGE)
        start = src.index("function renderItemNoteInfo(")
        end = src.index("\n}\n", start)
        body = src[start:end]
        for label in ('label="发布时间"', 'label="互动"'):
            assert label not in body, f"列表行还在渲染 {label}（实时块已有，会重复）"

    def test_live_detail_block_is_the_only_body_source(self) -> None:
        """实时详情块必须存在且从 `detail.content` 渲染正文。"""
        src = _code(PAGE)
        assert "function NoteLiveDetail(" in src, "`NoteLiveDetail` 组件不见了"
        start = src.index("function NoteLiveDetail(")
        end = src.index("\n}\n", start)
        body = src[start:end]
        assert "detail.content" in body, "实时详情块没有渲染 `detail.content`"


class TestBusinessLineBadgeRemoved:
    """「店铺线 · 千帆令牌」标签条**全站移除**（2026-10-27 用户口径：
    「店铺线 千帆令牌移除，不在展示，全部页面移除」）。

    ⚠️ 它是 `app-shell` 里的**单一挂载点**，所以「全站移除」= 删组件 + 删挂载点。
    只删挂载点会留下 123 行死代码；只删组件会 tsc 报错。
    """

    def test_badge_component_is_gone(self) -> None:
        assert not Path("frontend/src/components/layout/business-line-badge.tsx").exists(), (
            "业务线标签组件还在（应整文件删除）"
        )

    def test_badge_lib_is_gone(self) -> None:
        """`lib/business-lines.ts` 的唯一使用者就是那个组件 ⇒ 一并删除。"""
        assert not Path("frontend/src/lib/business-lines.ts").exists(), (
            "`business-lines.ts` 还在（已无使用者，属死代码）"
        )

    def test_badge_is_not_mounted(self) -> None:
        """`app-shell` 不许再挂载它。"""
        src = _code(Path("frontend/src/components/layout/app-shell.tsx"))
        assert "BusinessLineBadge" not in src, "`app-shell` 还在挂载业务线标签"
        assert "business-line-badge" not in src, "`app-shell` 还在 import 业务线标签"

    def test_business_line_type_is_kept(self) -> None:
        """⚠️ 反面对照：`BusinessLine` **类型**与账号范围记忆仍在使用，不许删。

        `lib/account-scope.ts` 按业务线记忆「上次选的账号」，与标签展示是两件事。
        """
        assert Path("frontend/src/lib/account-scope.ts").exists(), (
            "账号范围记忆被误删了（它依赖 BusinessLine 类型，但和标签无关）"
        )
        types_src = Path("frontend/src/types/index.ts").read_text(encoding="utf-8")
        assert 'export type BusinessLine = "shop" | "content" | "shared";' in types_src, (
            "`BusinessLine` 类型被误删了"
        )


class TestNoStaleLocalWording:
    """商品页面向用户的文案里不许再出现「本地台账 / 未同步到本地」这类**过时口径**。

    背景：商品与挂链笔记都已改成实时查千帆（`source=platform`），本地 `shop_products`
    与 `note_goods` 两张表已 DROP。文案若还写「从本地库读 / 未同步到本地的笔记」，
    会让用户以为看到的是缓存 —— 与实际行为正好相反。
    """

    #: 这些词只允许出现在**注释**里（说明历史），不许出现在 JSX 字符串 / 提示文案中
    STALE_IN_UI = (
        "含未同步到本地的笔记",
        "点开实时查千帆（含未同步",
    )

    def test_note_tooltip_has_no_local_hint(self) -> None:
        src = _code(PAGE)
        for phrase in self.STALE_IN_UI:
            assert phrase not in src, f"挂链笔记提示里还有过时措辞「{phrase}」"

    def test_note_tooltip_text_is_exact(self) -> None:
        """提示文案必须是「平台侧挂链篇数；点开实时查千帆」。"""
        src = _code(PAGE)
        assert 'title="平台侧挂链篇数；点开实时查千帆"' in src, "挂链笔记 Tooltip 文案变了"

    def test_empty_account_alert_does_not_say_sync(self) -> None:
        """「没有账号可选」的提示不能再说「才能同步商品」（同步链路已不存在）。"""
        src = _code(PAGE)
        idx = src.index("没有小红书账号可选")
        window = src[idx : idx + 300]
        assert "才能同步商品" not in window, "提示还写着「才能同步商品」"
        assert "实时查千帆" in window or "才能查询" in window, (
            "提示没说清现在的行为（实时查千帆），用户会以为要先同步"
        )

    def test_card_title_is_not_ledger(self) -> None:
        """卡片标题不许再叫「商品台账」（台账表已 DROP）。"""
        src = _code(PAGE)
        assert 'title="商品台账"' not in src, "卡片标题还叫「商品台账」"

    def test_backend_detail_request_docstring_is_not_local(self) -> None:
        """后端详情请求模型的 docstring 不许再写「纯本地」。"""
        src = _code(GOODS_API)
        idx = src.index("class ItemDetailRequest(")
        window = src[idx : idx + 260]
        assert "纯本地" not in window, "`ItemDetailRequest` 的 docstring 还写着「纯本地」"

    def test_local_delete_endpoint_and_type_are_gone(self) -> None:
        """`/items/delete` 端点与 `XhsShopItemsDeleteResult` 类型都已无调用方 ⇒ 删除。"""
        assert '"/items/delete"' not in _code(GOODS_API), "`/items/delete` 端点还在"
        types_src = Path("frontend/src/types/index.ts").read_text(encoding="utf-8")
        assert "XhsShopItemsDeleteResult" not in types_src, (
            "`XhsShopItemsDeleteResult` 类型还在（前端已无使用者）"
        )


class TestItemNotesIsRealtimeOnly:
    """`/item-notes` **不读任何本地表**（2026-10-27 用户口径「本地不保存笔记数据」）。

    ⚠️ 判据是「函数体里不出现本地模型查询」，不是「文件里没有那个字符串」——
    后者会被 docstring 里的说明满足（本仓踩过多次）。
    """

    def _fn_body(self) -> str:
        src = _code(GOODS_API)
        start = src.index("def item_notes(")
        end = src.index("def _realtime_adapter(", start)
        return src[start:end]

    def test_no_local_model_queries(self) -> None:
        body = self._fn_body()
        for banned in ("db.scalars(", "db.query(", "Note)", "NoteAsset", "select(Note"):
            assert banned not in body, f"`/item-notes` 还在查本地表：{banned}"

    def test_returns_platform_goods_and_flag(self) -> None:
        """响应里必须带平台自报的 `note_flag` 与挂载商品 `goods`。"""
        body = self._fn_body()
        assert '"note_flag"' in body, "响应没有 `note_flag`（前端「商业笔记」标识取不到）"
        assert '"goods"' in body, "响应没有挂载商品 `goods`"

    def test_no_sync_coverage_fields(self) -> None:
        """不许再回 `synced_notes` / `goods_synced_at` 这类「本地同步了多少」。"""
        body = self._fn_body()
        for banned in ('"synced_notes"', '"goods_synced_at"'):
            assert banned not in body, f"还在回本地覆盖率字段：{banned}"


class TestGoodsBlockShowsAllFields:
    """挂载商品区必须展示接口返回的**商品基本信息**（2026-10-27 用户口径
    「接口返回的商品基本信息都要展示」，随后细化为只展示商品 ID + 售价 + 规格）。

    `goods_infos[]` 有 4 个字段：`item_id` / `item_title` / `item_image` / `variant_desc`。
    卖家 SKU **不上屏**（用户口径「只展示商品id」「skuid 不展示」）。
    """

    def test_normalize_keeps_all_four_fields(self) -> None:
        """`goods_infos[]` 的 4 个字段必须全部进归一化结果。

        ⚠️ 判据落在**整个函数体**上，不按 `"goods": [` 取窗口：`_code()` 会剥掉
        `#:` 注释行，窗口起点前移、字段被挤出窗口（本守卫第一版就是这样假红的）。
        """
        src = _code(Path("backend/app/services/realtime_platform_query_service.py"))
        start = src.index("def normalize_goods_note")
        body = src[start : src.index(chr(10) + "def ", start + 10)]
        for field in ("item_id", "title", "image", "variant_desc"):
            assert f'"{field}"' in body, f"归一化丢了字段 `{field}`"

    def test_api_passes_goods_through(self) -> None:
        src = _code(GOODS_API)
        idx = src.index("def item_notes(")
        window = src[idx : idx + 4200]
        assert '"goods": [' in window, "`/item-notes` 没有透出挂载商品"
        assert "price_fen=_min_sku_price" in window, "挂载商品没有补价格"

    def test_goods_uses_flex_layout_not_table(self) -> None:
        """挂载商品用**弹性布局**，不用表格（表格会把 24 位商品 ID 挤进固定列折成两行）。"""
        src = _code(PAGE)
        idx = src.index('title="挂载商品"')
        window = src[idx : idx + 6400]
        assert "<Table" not in window, "挂载商品又改回表格了（ID 会折行）"
        assert 'display: "flex"' in window, "挂载商品不是弹性布局"

    def test_ui_renders_goods_fields(self) -> None:
        """界面展示商品 ID / 售价 / 规格；卖家 SKU **不展示**。"""
        src = _code(PAGE)
        idx = src.index('title="挂载商品"')
        window = src[idx : idx + 6400]
        for label in ("商品ID：", "规格：", "售价："):
            assert label in window, f"挂载商品缺少「{label}」"
        assert "SKU：" not in window, "卖家 SKU 还在展示（用户明确要求不展示）"

    def test_ui_renders_price(self) -> None:
        """售价必须有数据源，缺值显示空白（整块不渲染，而不是「未返回」）。"""
        src = _code(PAGE)
        assert "formatFen" in src, "没有价格格式化函数"
        idx = src.index('title="挂载商品"')
        window = src[idx : idx + 6400]
        assert "hit?.price_fen" in window, "没有售价数据源"

    def test_goods_name_is_one_line_ellipsis(self) -> None:
        """商品名**固定一行 + 省略号**，完整名走 Tooltip。"""
        src = _code(PAGE)
        idx = src.index('title="挂载商品"')
        window = src[idx : idx + 6400]
        assert "textOverflow" in window and "ellipsis" in window, "商品名没有省略号"
        assert 'whiteSpace: "nowrap"' in window, "商品名会换行"
        assert "<Tooltip title={name}>" in window, "商品名没有 Tooltip 兜完整名称"
        assert "minWidth: 0" in window, "flex 子项缺 `minWidth: 0`，省略号不会生效"

    def test_id_and_price_share_one_row(self) -> None:
        """商品 ID 与售价 **同一行**（2026-10-27 用户口径「价格与商品id一行展示」）。"""
        src = _code(PAGE)
        idx = src.index('title="挂载商品"')
        window = src[idx : idx + 6400]
        id_pos = window.index("商品ID：")
        price_pos = window.index("售价：")
        assert price_pos - id_pos < 400, "售价离商品 ID 太远（不在同一行）"
        assert 'whiteSpace: "nowrap"' in window[id_pos - 120 : price_pos + 60], (
            "商品 ID 那一行没钉 nowrap，24 位 ID 会折行"
        )

    def test_price_hidden_when_missing(self) -> None:
        """价格缺值时**整块不渲染**（2026-10-27 用户口径「售价：未返回 则不展示」）。"""
        src = _code(PAGE)
        idx = src.index("商品ID：")
        window = src[idx : idx + 900]
        assert "priceText &&" in window, "售价没有做缺值隐藏"
        assert "售价：{priceText}" in window, "售价没走 priceText（会渲染「未返回」）"

    def test_variant_is_kept_on_its_own_row(self) -> None:
        """规格**保留**并单独一行（用户口径「规格保存」）。"""
        src = _code(PAGE)
        idx = src.index('title="挂载商品"')
        window = src[idx : idx + 6400]
        var_pos = window.index("规格：")
        assert "variant" in window[var_pos : var_pos + 60], "规格没有渲染 variant_desc"
        price_pos = window.index("售价：")
        assert price_pos < var_pos, "规格应在「商品ID + 售价」那一行之后单独出现"


class TestNoteModalRealtimePresentation:
    """挂链笔记弹窗的展示口径（2026-10-27 用户三条口径）。"""

    def test_link_is_a_copy_text_not_full_url(self) -> None:
        """笔记链接展示成「复制链接」文本，**不渲染完整 URL**。"""
        src = _code(PAGE)
        idx = src.index("复制链接")
        window = src[max(0, idx - 600) : idx + 300]
        assert "笔记ID：" in window, "笔记 ID 与链接不在同一块（要求同一行）"
        assert "{note.note_url}" not in window, "笔记链接还在渲染完整 URL"

    def test_type_tag_shares_row_with_id_and_link(self) -> None:
        """「商业笔记」标签（笔记类型）与笔记 ID / 链接**同一行**（2026-10-27 用户口径）。"""
        src = _code(PAGE)
        id_pos = src.index("笔记ID：")
        link_pos = src.index("复制链接", id_pos)
        #: 标签要在**复制链接之后**（2026-10-27 用户口径「放复制链接后面」）
        tag_pos = src.index("商业笔记", link_pos)
        assert link_pos < tag_pos, "「商业笔记」标签不在「复制链接」之后"
        assert tag_pos - link_pos < 500, "标签离「复制链接」太远（不在同一行）"
        assert tag_pos - id_pos < 900, "标签不在笔记 ID 那一行"
        #: 不许再是独立一行的 Space 包裹
        assert "<Space size={6} wrap>" not in src[max(0, tag_pos - 300) : tag_pos], (
            "「商业笔记」还被单独包在一行 Space 里"
        )

    def test_note_id_and_link_share_one_row(self) -> None:
        """笔记 ID 与笔记链接**同一行**（2026-10-27 用户口径）。"""
        src = _code(PAGE)
        id_pos = src.index("笔记ID：")
        link_pos = src.index("复制链接", id_pos)
        assert link_pos - id_pos < 600, "复制链接离笔记 ID 太远（不在同一行）"
        #: `nowrap` 钉在**外层 flex 容器**上（一个 div 包标签 + ID + 链接），
        #: 实测在 `笔记ID：` 之前约 290 字符处。
        assert 'whiteSpace: "nowrap"' in src[max(0, id_pos - 500) : link_pos], (
            "笔记 ID 那行没钉 nowrap"
        )

    def test_topics_are_inlined_into_content(self) -> None:
        """正文里的 `#名字[话题]#` 就地渲染成蓝色 tag，**没有单独的话题行**
        （2026-10-27 用户口径「话题不要单独提取展示一行，直接在正文中替换成 tag 即可」）。"""
        src = _code(PAGE)
        assert "NoteContentWithTopics" in src, "没有正文话题内联组件"
        #: ⚠️ `[话题]` 判据必须读**原文**：`_code()` 剥 `{/* … */}` 注释时会把正则里的
        #: `[话题]` 一并吃掉（字符序列相同），剥完再断言必然假红。
        raw = PAGE.read_text(encoding="utf-8")
        assert "[话题]" in raw, "内联正则没认出平台富文本标记 `[话题]#`"
        assert "#{match[1]}" in raw, "话题 tag 没渲染话题名"
        #: 单独的话题区块必须消失（原先是 `hash_tags.length > 0` 那段）
        assert "hash_tags.length > 0" not in src, "单独的话题行还在渲染"
        assert 'label="话题"' not in src and ">话题<" not in src, "还有独立「话题」标签行"

    def test_topic_regex_built_per_render(self) -> None:
        """带 `g` 的正则必须每次渲染新建 —— 复用会因 `lastIndex` 残留而漏匹配后续话题。"""
        src = _code(PAGE)
        idx = src.index("function NoteContentWithTopics")
        body = src[idx : idx + 900]
        assert 'new RegExp(NOTE_TOPIC_RE.source, "g")' in body, (
            "正则被复用（`g` 标志带 lastIndex 状态，会漏匹配）"
        )

    def test_goods_comes_after_content(self) -> None:
        """挂载商品必须排在**正文之后**（2026-10-27 用户口径「商品放正文下」）。"""
        src = _code(PAGE)
        assert src.index('title="挂载商品"') > src.index("（平台没有返回正文）"), (
            "挂载商品还在正文前面"
        )


    def test_image_matches_by_item_id_not_goods_id(self) -> None:
        """图片匹配必须用 `item_id` —— 详情的 `goods_id` 是 SKU ID，匹配不上。"""
        src = _code(PAGE)
        idx = src.index('title="挂载商品"')
        window = src[idx : idx + 4200]
        assert "row.item_id === g.item_id" in window, "图片没有按 `item_id` 匹配"
        assert "row.item_id === g.goods_id" not in window, (
            "还在用 `goods_id` 匹配商品（那是 SKU ID，永远匹配不上 ⇒ 图片空白）"
        )


class TestEngagementIsOneLine:
    """"点赞 / 收藏 / 评论 / 分享"必须**同一行**（2026-10-27 用户口径）。"""

    def test_four_metrics_in_one_table_row(self) -> None:
        src = _code(PAGE)
        idx = src.index('<InfoCell label="点赞"')
        window = src[idx : idx + 500]
        for label in ("点赞", "收藏", "评论", "分享"):
            assert f'label="{label}"' in window, f"互动缺少「{label}」"
        assert "<tr>" not in window, "互动四项被拆到了多个 <tr>（不在同一行）"

    def test_no_engagement_descriptions_items(self) -> None:
        """互动指标不许回退成 `Descriptions.Item`（那是被取代的旧排版）。"""
        src = _code(PAGE)
        for banned in ('<Descriptions.Item label="点赞"', '<Descriptions.Item label="收藏"'):
            assert banned not in src, f"互动指标回退成了 Descriptions：{banned}"


class TestPreviewWindowKeepsFixedSize:
    """图片预览窗口尺寸**固定**（2026-10-27 用户口径「预览窗口大小固定不要变化」）。"""

    def test_media_box_has_fixed_height(self) -> None:
        src = Path("frontend/src/components/platforms/note-detail-modal.tsx").read_text(
            encoding="utf-8"
        )
        idx = src.index("const currentImage =")
        window = src[idx : idx + 2600]
        assert 'height: "58vh"' in window, "预览盒子没有固定高度（会随图片比例跳动）"
        assert 'display: "flex"' in window, "预览盒子没有用 flex 居中（图片比例变化会撑动布局）"
        # 图片自身只许满盒 contain，不许再用 maxHeight 撑开盒子
        img_idx = window.index("<img")
        img_style = window[img_idx : img_idx + 400]
        assert 'maxHeight: "100%"' in img_style, "图片没有按盒子高度约束"
        assert "maxHeight: \"58vh\"" not in img_style, "图片还在用 58vh 撑盒子（高度会跳）"


class TestDevServerPinsItsPort:
    """前端改动的「实时生效」依赖 **5173 真的在 5173 上**（2026-10-27 实测定调）。

    症状：改完前端看不到变化 —— 以为是缓存/构建问题，实际是 **vite 换端口了**。
    vite 默认「端口被占就试下一个」，三个实例互抢时最后一个跑到 **5224**，
    而 launcher / 浏览器 / 文档全指着 5173 ⇒ 用户看到的是 8000 直出的旧 dist。

    两道防线，缺一条就会重现：
      ① `vite.config.ts` 的 `strictPort: true` —— 宁可起不来，也不悄悄换端口；
      ② launcher 的 `ensure()` 按 `port` 复核 —— 「进程活着」不代表「在正确的端口」。
    """

    def test_vite_config_pins_strict_port(self) -> None:
        cfg = Path("frontend/vite.config.ts").read_text(encoding="utf-8")
        assert "strictPort: true" in cfg, (
            "`vite.config.ts` 没开 `strictPort` —— 端口被占时会悄悄换端口，"
            "用户访问 5173 拿到的是旧页面"
        )
        assert '"/api"' in cfg, "代理配置丢了（5173 要反代到 8000）"

    def test_launcher_checks_the_port_not_just_the_process(self) -> None:
        src = Path("scripts/run-app.py").read_text(encoding="utf-8")
        assert "def _listening" in src, "launcher 没有端口复核"
        assert "port_listening(" in src, "端口复核没走既有的 `port_listening`"
        #: ensure() 里必须真的用上它，否则只是个死函数
        idx = src.index("def ensure")
        body = src[idx : idx + 1800]
        assert "self._listening()" in body, "`ensure()` 没做端口复核（活着≠在正确端口）"

    def test_compat_entry_only_forwards(self) -> None:
        """`.hermes/scripts/run-app.py` 只做转发，不得再写一份实现。"""
        src = Path(".hermes/scripts/run-app.py").read_text(encoding="utf-8")
        assert "runpy" in src, "兼容入口不再转发（两份实现迟早漂移）"
        assert "uvicorn" not in src, "兼容入口里又写了启动逻辑"


class TestOnlyOneFrontendPort:
    """**只有一个前端端口**：5173（Vite / HMR）；8000 只管 API。

    2026-10-27 用户口径「8000和5173只保留一个前端端口」「访问8000端口为何页面看到前端」。

    两个前端入口的害处是**静默分叉**：8000 走 `frontend/dist`（要 `vite build` 才有新代码），
    5173 走源码（HMR 即时）。用户访问 8000 看到旧页面，会得出「改前端不生效」的错误结论 ——
    实际是两条路里的一条本来就不会实时。
    """

    def test_launcher_turns_static_serving_off(self) -> None:
        src = Path("scripts/run-app.py").read_text(encoding="utf-8")
        assert 'api_env["FRONTEND_SERVE_STATIC"] = "false"' in src, (
            "launcher 没把 8000 的静态托管关掉 ⇒ 两个前端入口都还开着"
        )
        assert 'api_env["FRONTEND_SERVE_STATIC"] = "true"' not in src, (
            "launcher 又把 8000 的静态托管打开了"
        )

    def test_vite_is_still_started(self) -> None:
        """关掉 8000 的静态托管后，5173 必须仍在 launcher 的监督列表里 ——
        否则「唯一前端入口」也没了，用户直接看不到界面。"""
        src = Path("scripts/run-app.py").read_text(encoding="utf-8")
        assert "--port\", \"5173\"" in src or '"--port", "5173"' in src or "5173" in src, (
            "launcher 不再启动 vite（唯一前端入口没了）"
        )
        idx = src.index("web = Child(")
        body = src[idx : idx + 400]
        assert "5173" in body, "vite 子进程的端口不是 5173"


class TestGenerateGateRequiresRealAdapter:
    """生成笔记的**门禁必须拿到真 adapter**（2026-10-27 线上缺陷，用户截图报的）。

    现象：商品列表能实时查（说明 ark 令牌是好的），点「生成笔记」却弹
    「该账号还没有千帆（ark）令牌，请先在浏览器登录千帆后导入。」

    根因：`_gate_for_generation(...)` 的 `adapter` 参数有默认值 `None`，而三处调用点
    **都没传** ⇒ `_product_row` 收到 `None`，把「调用方漏传 adapter」误判成
    「该账号缺令牌」⇒ 一句完全误导的凭据错误。

    判据（缺一条就会重现）：
      ① 门禁的 `adapter` **不给默认值** —— 漏传要在调用点就炸，而不是伪装成凭据问题；
      ② 三处调用点都**真的传了** adapter；
      ③ 门禁返回的商品被复用，不 `_product_row` 二次查询（同一次平台请求不打两遍）。
    """

    SERVICE = Path("backend/app/services/product_note_gen_service.py")

    def test_gate_adapter_has_no_default(self) -> None:
        src = self.SERVICE.read_text(encoding="utf-8")
        idx = src.index("def _gate_for_generation(")
        sig = src[idx : idx + 160]
        assert "adapter: Any" in sig, f"门禁没有 adapter 参数: {sig[:120]}"
        assert "adapter: Any = None" not in sig, "门禁的 adapter 有默认值 None"

    def test_all_call_sites_pass_an_adapter(self) -> None:
        src = self.SERVICE.read_text(encoding="utf-8")
        calls = [
            line.strip()
            for line in src.splitlines()
            if "_gate_for_generation(db, account, item_id" in line
        ]
        assert calls, "找不到 _gate_for_generation 的调用点"
        for call in calls:
            assert "adapter=" in call, f"调用点漏传 adapter: {call}"

    def test_missing_adapter_is_not_reported_as_credential_error(self) -> None:
        """漏传 adapter 的错误码不许再用「凭据缺失」那一档。"""
        src = self.SERVICE.read_text(encoding="utf-8")
        #: 只取**这一个 if 块**（按 raise 语句的括号配对截断）——
        #: 固定 900 字符窗口会把下一处合法的凭据检查也框进来（守卫第一版就这样假红）。
        idx = src.index("if adapter is None:")
        raise_at = src.index("raise ProductNoteGenerationError(", idx)
        close_at = src.index(chr(10) + "        )", raise_at)
        #: 先剥掉 `#:` 注释 —— 代码里那段注释**特意写了**旧错误码做历史说明，
        #: 直接搜字符串会被自己的注释命中（守卫第二次假红就是这个）。
        block = "\n".join(
            line
            for line in src[idx : close_at + 12].splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "INTERNAL_ADAPTER_MISSING" in block, (
            "漏传 adapter 仍报凭据错误（会再次误导用户去重登千帆）"
        )
        assert "QIANFAN_CREDENTIAL_MISSING" not in block, (
            "漏传 adapter 的错误码是「凭据缺失」—— 这正是本次缺陷的成因"
        )

    def test_gate_result_is_reused_not_requeried(self) -> None:
        """门禁已经查回商品，调用方不许再 `_product_row` 打一次平台。"""
        src = self.SERVICE.read_text(encoding="utf-8")
        for marker in ("def generate_note_for_item", "def enqueue_single_generation"):
            if marker not in src:
                continue
            idx = src.index(marker)
            body = src[idx : idx + 2200]
            if "_gate_for_generation" not in body:
                continue
            assert "_product_row(db, account, item_id, adapter=" not in body, (
                f"{marker} 里门禁之后又查了一遍商品（同一次平台请求打两次）"
            )


class TestProductEditChainRemoved:
    """「编辑商品」整条链路**彻底移除**（2026-10-27 用户口径「编辑商品功能逻辑移除」）。

    为什么整条删：商品改平台实时数据后，本地台账（含 `ai_title` / `ai_selling_point`
    两列）整表 DROP ⇒ 这个弹窗「改本地标注」的前提不存在了；平台的商品编辑走千帆
    后台，本系统按只读边界不碰。

    六件套一个都不能留（留一件就是留住一条 410 / 无处调用的死路径）：
      ① 前端入口按钮与弹窗挂载点
      ② 弹窗组件文件
      ③ 请求函数（`editShopItem` / `polishShopItem`）
      ④ 后端端点（`/items/edit` 墓碑、`/items/polish`）
      ⑤ 权限码 `xhs:item:edit` 与菜单行
      ⑥ 死代码（`product_polish_service` / 编辑纯逻辑 / 空壳错误映射）
    """

    def test_frontend_entry_and_modal_are_gone(self) -> None:
        src = _code(PAGE)
        for banned in ("ProductEditModal", "setEditingItem", "编辑商品（AI 润色"):
            assert banned not in src, f"商品列表页还有编辑入口：{banned}"
        assert not Path("frontend/src/pages/platforms/xhs/product-edit-modal.tsx").exists(), (
            "编辑弹窗文件还在"
        )

    @staticmethod
    def _strip_comments(text: str) -> str:
        """剥掉注释行 —— 源码里**特意写了**「已删除」的历史说明，直接搜字符串
        会被自己的注释命中（本组守卫前几版就反复假红）。"""
        return "\n".join(
            line
            for line in text.splitlines()
            if not line.strip().startswith(("//", "#", "*", "/*", "*/", "#:"))
        )

    def test_request_functions_are_gone(self) -> None:
        code = self._strip_comments(Path("frontend/src/lib/api.ts").read_text(encoding="utf-8"))
        assert "editShopItem" not in code, "`editShopItem` 还在（会打 410 墓碑）"
        assert "polishShopItem" not in code, "`polishShopItem` 还在（已无调用方）"

    def test_backend_endpoints_are_gone(self) -> None:
        src = _code(GOODS_API)
        for banned in ('"/items/edit"', '"/items/polish"', "ItemEditRequest", "ItemPolishRequest"):
            assert banned not in src, f"后端还留着编辑链路：{banned}"

    def test_permission_and_menu_row_are_gone(self) -> None:
        perms = Path("backend/app/core/route_perms.py").read_text(encoding="utf-8")
        assert "items/edit" not in perms and "items/polish" not in perms, (
            "route_perms 还登记着编辑 / 润色的端点"
        )
        menus = json.loads(Path("config/initial_data/menus.seed.json").read_text(encoding="utf-8"))
        rows = menus["menus"] if isinstance(menus, dict) and "menus" in menus else menus
        assert not [r for r in rows if r.get("perms") == "xhs:item:edit"], (
            "菜单种子还有 `xhs:item:edit` 的行"
        )

    def test_dead_code_is_deleted(self) -> None:
        assert not Path("backend/app/services/product_polish_service.py").exists(), (
            "`product_polish_service` 是死代码（零引用）却没删"
        )
        shared = Path("backend/app/api/platforms/xhs/qianfan/_shared.py").read_text(encoding="utf-8")
        assert "_item_error_to_http" not in shared, "空壳错误映射还留着（零调用方）"
        lib = self._strip_comments(Path("frontend/src/lib/product-edit.ts").read_text(encoding="utf-8"))
        for banned in ("buildEditPayload", "adoptCandidate", "updatedSummary", "EDITABLE_FIELDS"):
            assert banned not in lib, f"编辑纯逻辑还在：{banned}"
        assert "exportBlockReason" in lib, "导出上限校验被误删了（它还在用）"

    def test_edit_types_are_gone(self) -> None:
        types = Path("frontend/src/types/index.ts").read_text(encoding="utf-8")
        for banned in ("XhsItemEditPayload", "XhsItemEditResult", "XhsItemPolishResult"):
            assert banned not in types, f"编辑相关类型还在：{banned}"


class TestNoPhantomLocalReads:
    """商品域里**不许再有「读本地台账」的残留**（2026-10-27 全量排查的收口）。

    `shop_products` / `shop_note_goods` 两张表已随迁移 `n8o9p0q1r2s3` DROP。
    任何仍按「本地行」处理商品/挂载笔记的代码，要么 `AttributeError`、要么恒返回空值 ——
    共同点都是**静默说谎**（用户看不出，只能靠翻代码发现）。
    """

    def test_local_status_refresh_is_gone(self) -> None:
        """上下架成功后的「本地台账乐观刷新」必须删除。

        `_refresh_local_status` 当年改的是 ORM 行（`row.raw_json` / `card_type` / `status`），
        而 `by_id` 自设计 75 起是**实时归一化 dict** ⇒ 属性访问必然 `AttributeError`；
        被外层 except 兜住时还会把**已成功**的逐条结果连带判失败。
        """
        src = _code(Path("backend/app/services/shop_item_operate_service.py"))
        assert "_refresh_local_status" not in src, "本地台账乐观刷新还在（会 AttributeError）"
        assert "row.raw_json" not in src, "仍在按 ORM 行读 `raw_json`（by_id 是 dict）"

    def test_synced_note_count_is_gone(self) -> None:
        """`synced_note_count` 恒 0 的空壳必须连同响应字段一起删。"""
        svc = _code(Path("backend/app/services/shop_binding_service.py"))
        assert "def synced_note_count" not in svc, "`synced_note_count` 空壳还在"
        for path in (
            Path("backend/app/api/platforms/xhs/qianfan/_shared.py"),
            Path("backend/app/api/platforms/xhs/pc.py"),
        ):
            src = path.read_text(encoding="utf-8")
            assert '"synced_notes"' not in src, f"{path.name} 还回 `synced_notes`（恒 0）"

    def test_attach_local_ai_text_is_gone(self) -> None:
        """`_attach_local_ai_text` 恒给空串的假贴值必须删除（前端无人读那两个字段）。"""
        src = _code(GOODS_API)
        assert "_attach_local_ai_text" not in src, "假贴值函数还在"
        types = Path("frontend/src/types/index.ts").read_text(encoding="utf-8")
        assert "ai_title?" not in types and "ai_selling_point?" not in types, (
            "前端还留着 `ai_title` / `ai_selling_point` 字段（后端已不再下发）"
        )

    def test_note_cleanup_docstring_matches_realtime(self) -> None:
        """无流量候选清单的 docstring 不许再写「纯本地，零平台请求」（实现是实时翻页）。"""
        svc = Path("backend/app/services/note_cleanup_service.py").read_text(encoding="utf-8")
        idx = svc.index("def list_candidates(")
        doc_start = svc.index('"""', idx) + 3
        doc = svc[doc_start : svc.index('"""', doc_start)]
        #: 「原文『纯本地』那套口径已被取代」这类**历史说明**不算说谎 ——
        #: 判据是「首行摘要」不许再写纯本地（那才是读者第一眼信的结论）。
        head = doc.strip().splitlines()[0]
        assert "纯本地" not in head, f"`list_candidates` 摘要还写着「纯本地」：{head[:60]}"
        assert "rt.list_goods_notes" in svc[idx : idx + 3000], "实现里没有实时翻页调用"
        api = _code(GOODS_API)
        idx2 = api.index("def note_cleanup_candidates(")
        d2 = api.index('"""', idx2) + 3
        head2 = api[d2 : api.index('"""', d2)].strip().splitlines()[0]
        assert "零平台请求" not in head2, f"端点摘要还写着「零平台请求」：{head2[:60]}"
