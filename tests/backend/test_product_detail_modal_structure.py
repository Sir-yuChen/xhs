"""商品详情弹窗与平台原始数据表的结构守卫。

为什么要这组断言（都是真机踩过的坑）：

1. **Tab 面板空白**（2026-10-27）：详情弹窗曾手写 flex 把 `height:100%` 逐层传进
   antd `Tabs`，而 `Tabs` 内部四层包装（nav / content-holder / content / tabpane）
   默认不带高度 ⇒ 面板内 `height:100%` 解析成 0，再叠 `overflow:auto` 就把内容
   整个裁掉。表现是「概览」正常、「媒体」「全部字段」一片空白。
   用户口径：「使用 antd 的组件，不要使用自研组件」。
   ⇒ 守卫钉住：详情弹窗**不许再出现**那段手写高度链 CSS，且根容器必须是 antd 布局组件。

2. **视频字段整条链路缺失**：平台 `search_item_v2` 明明下发 `videos`（实测某商品
   1 条 / 13 秒 / 720x960），但改造前从 service 到端点都没取过，界面上永远看不到。
   ⇒ 后端守卫在 `tests/backend/test_platform_raw_fields.py`，前端这里钉展示。

3. **Tab 数量**：用户口径「分为两个，一个商品数据 tab…第二个媒体 tab」
   ⇒ 恰好两个，多了少了都是偏离。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODAL = ROOT / "frontend/src/pages/platforms/xhs/product-detail-modal.tsx"


def _code(path: Path) -> str:
    """剥掉注释，只看真正会执行的代码（本仓 docstring 大量写反例）。"""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return text


def test_modal_uses_antd_components_not_handwritten_layout() -> None:
    """详情弹窗必须用 antd 组件，不许手写布局（用户口径）。

    2026-10-27 二次调整后判据变成「**不用左右分栏**」：
    用户口径「左右展示布局合并为一个」—— 商品卡整宽置顶，内容一列排下来。
    """
    src = _code(MODAL)
    #: 不许再有左右分栏
    assert "Layout.Sider" not in src, (
        "详情弹窗又用了 `Layout.Sider` 左右分栏 —— 用户口径是把左右布局合并成一列"
    )
    assert "<Layout" not in src, "详情弹窗还在用 `Layout` 分栏"
    #: 反面对照：内容仍由 antd 组件承载（不是裸 div 拼的）
    assert "<Card" in src, "商品卡没有用 antd `Card`"
    assert "<Descriptions" in src, "字段区没有用 antd `Descriptions`"



def test_modal_has_no_handwritten_tabs_height_chain() -> None:
    """不许再给 antd Tabs 手写高度链 —— 那正是「媒体 / 全部字段」空白的根因。"""
    src = _code(MODAL)
    for selector in (
        ".ant-tabs-content-holder",
        ".ant-tabs-content ",
        ".ant-tabs-tabpane",
    ):
        assert selector not in src, (
            f"详情弹窗又出现了手写高度链选择器 {selector!r} —— "
            "这正是 2026-10-27「媒体 / 全部字段 Tab 一片空白」的根因，"
            "别再给 antd Tabs 内部包装补高度，让面板按自然高度展开"
        )
    #: `height: 100%` 也不该出现在 Tab 面板容器上（那需要父级有确定高度）
    assert 'height: "100%"' not in src, (
        "详情弹窗出现 `height: \"100%\"` —— Tab 面板不该依赖百分比高度"
    )


def test_modal_has_no_tabs_and_lists_sections_inline() -> None:
    """用户口径：**不用 Tab**，一列顺序展开。

    2026-10-27 再调整 → 归并成 **5 个大模块**：
    商品信息 / 商品属性 / 商品规格 / 质检·库存·服务 / 媒体资源。
    """
    src = _code(MODAL)
    assert "<Tabs" not in src, (
        "详情弹窗又用回 antd `Tabs` 了 —— 用户口径是「不使用 tab 展示」，内容要顺序展开"
    )
    assert "Tabs," not in src, "`Tabs` 的 import 没清掉"


@pytest.mark.parametrize(
    "title",
    ["商品信息", "商品属性", "商品规格", "质检 · 库存 · 服务", "媒体资源"],
)
def test_modal_has_the_five_modules(title: str) -> None:
    """5 个大模块必须各有一个 `Card` 标题（用户口径「合并到一起，分为这些大模块」）。

    ⚠️ 钉 `title=` 渲染处，不钉字符串出现 —— 名称在注释里也可能出现。
    """
    src = _code(MODAL)
    assert re.search(rf'title=\{{?[`"]{re.escape(title)}', src), (
        f"缺少「{title}」模块（`Card` 的 title 上找不到）"
    )


def test_product_card_image_and_text_are_not_glued() -> None:
    """商品卡里主图与右侧文字之间**必须有间距**（2026-10-27 用户截图报「间距不合理」）。

    ⚠️ 根因不是忘了写间距，而是写法无效：antd 的 `Row gutter` **只对带 `span` 的
    `Col` 生效**，商品卡两列都是 `flex="140px"` / `flex="auto"` ⇒ gutter 被跳过
    ⇒ 图与文字贴在一起、文字压在图片右边缘上。
    所以这里钉「不许用 gutter 顶替间距 + 左列必须自己有 marginRight」。
    """
    src = _code(MODAL)
    #: 商品卡那段：从 `<Card size="small" title="商品信息">` 到它的 `</Card>`
    start = src.index('title="商品信息"')
    end = src.index("</Card>", start)
    block = src[start:end]
    assert 'flex="140px"' in block, "商品卡主图列不是 140px 了 —— 间距断言的前提变了，请复核"
    assert re.search(r'flex="140px"[^>]*marginRight', block), (
        "商品卡主图列没有 marginRight —— 用 `Row gutter` 对 flex 列不生效，图与文字会贴在一起"
    )



def test_modal_does_not_render_raw_field_table() -> None:
    """平台原始字段区**整块不展示**（用户口径 2026-10-27）。

    演进：先是「两个标题重复」→ 删组件内标题；再是「区域移除，不展示」→
    连调用方的标题 + 说明 + `PlatformRawDataTable` 一起删掉。
    详情弹窗只留业务可读字段（基本信息 / 时间 / 属性 / 规格 / 销量 / 质检），
    不再向用户暴露平台原始 JSON。
    """
    src = _code(MODAL)
    assert "<PlatformRawDataTable" not in src, (
        "详情弹窗又渲染了平台原始字段表 —— 用户口径是「移除区域不展示」"
    )
    assert "平台原始字段" not in src, "详情弹窗还有「平台原始字段」标题"
    assert "platform-raw-data-table" not in src, (
        "`platform-raw-data-table` 的 import 没清掉"
    )




def test_media_tab_covers_all_three_media_kinds() -> None:
    """媒体 Tab 必须展示主图 / 详情图 / 视频三类（平台下发什么就展示什么）。

    ⚠️ 断言必须落在**渲染处**（`title={...}` 或 `title="..."`），不能只查字符串出现在
    文件里 —— 「商品视频」在 import/注释里也可能出现，那样变异（删掉整块渲染）会逃逸。
    """
    src = _code(MODAL)
    for label in ("商品主图", "商品详情图", "商品视频"):
        assert re.search(rf'title=\{{?[`"]{label}', src), (
            f"媒体 Tab 没有渲染「{label}」分组（`title=` 上找不到）"
        )
    #: 三类的数据来源字段
    for field in ("product_images", "detail_images", "videos"):
        assert field in src, f"媒体 Tab 没有读 `{field}`"


def test_data_section_contains_sku_table() -> None:
    """商品数据段要含**规格表**（用户口径「商品的数据详情表格，规格表等」）。

    ⚠️ 钉**渲染处**：`columns={SKU_COLUMNS}` 是表格真正绑数据的那一行；
    只查 `SKU_COLUMNS` 这个名字会被常量定义本身满足（变异把它换成空列会逃逸）。
    """
    src = _code(MODAL)
    assert re.search(r"<Table<XhsShopItemSku>[\s\S]{0,400}?columns=\{SKU_COLUMNS\}", src), (
        "商品数据段没有渲染规格表（找不到 `<Table<XhsShopItemSku> ... columns={SKU_COLUMNS}>`）"
    )




def test_sku_table_shows_spec_image_column() -> None:
    """规格表必须展示**规格图**（平台 `sku.spec_image`）。

    ⚠️ 真机实测：接口 52 个 SKU **全部**带 `spec_image`，但界面一直没渲染这一列
    （`SKU_COLUMNS` 里从没有它）—— 属于「接口有、界面漏」的静默缺失，
    用户只能看到规格名，不知道每个规格长什么样。
    """
    src = _code(MODAL)
    assert re.search(r'title:\s*"规格图"', src), (
        "规格表没有「规格图」列 —— 平台每个 SKU 都带 `spec_image`，必须展示"
    )
    assert re.search(r"dataIndex:\s*\"spec_image\"", src), (
        "「规格图」列没有绑 `spec_image` 字段"
    )
