"""商品列表排序字段守卫（2026-10-27）。

背景：千帆 `search_item_v2` 的 `sort_field` **只认 7 个值**，其余（含 `price_min_fen`
这种「看起来很像」的名字）一律回 `服务端处理失败` ⇒ 整页列表变红。
所以「前端表头能点哪些列」必须被后端白名单盖住，两边不许各写各的。

三条判据：
1. SDK 白名单 = 实测可用的 7 个（不多不少）；
2. 前端 `SORT_FIELD_BY_COLUMN` 的每个值都在白名单里；
3. 后端路由对白名单外的 `sort_field` 回**结构化 400**（不是 500、也不静默退默认）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apis.xhs_qianfan_apis import SORTABLE_ITEM_FIELDS

VIEW_LIB = Path("frontend/src/lib/shop-items-view.ts")
GOODS_API = Path("backend/app/api/platforms/xhs/qianfan/goods.py")
PAGE = Path("frontend/src/pages/platforms/xhs/shop-items-page.tsx")

EXPECTED = {
    "create_time",
    "on_shelf_time",
    "off_shelf_time",
    "update_time",
    "price",
    "stock",
    "item_sale_qty_30",
}


def test_sdk_whitelist_matches_probe() -> None:
    """SDK 白名单必须与 2026-10-27 实测结果逐项一致。

    ⚠️ `price_min_fen` / `min_price` / `total_stock` / `sale_qty30` / `last_audit_time`
    实测都是 500 —— 谁把它们加回来，这里就该红。
    """
    assert set(SORTABLE_ITEM_FIELDS) == EXPECTED, (
        f"白名单漂移：多 {set(SORTABLE_ITEM_FIELDS) - EXPECTED}，"
        f"少 {EXPECTED - set(SORTABLE_ITEM_FIELDS)}"
    )


def _frontend_sort_targets() -> dict[str, str]:
    """解析 `SORT_FIELD_BY_COLUMN` 里的 列key → 平台字段。"""
    src = VIEW_LIB.read_text(encoding="utf-8")
    start = src.index("export const SORT_FIELD_BY_COLUMN")
    end = src.index("};", start)
    body = src[start:end]
    #: 去掉行注释再抓键值对
    body = re.sub(r"//.*$", "", body, flags=re.M)
    return dict(re.findall(r'(\w+):\s*"([^"]+)"', body))


def test_every_sortable_column_is_in_the_whitelist() -> None:
    """前端每一个可排序列，平台字段都必须在白名单里。

    否则用户点了表头 → 后端把字段原样转发 → 平台 500 → 整页列表变红。
    """
    mapping = _frontend_sort_targets()
    assert mapping, "没解析到 SORT_FIELD_BY_COLUMN（结构变了？）"
    for column, platform_field in mapping.items():
        assert platform_field in SORTABLE_ITEM_FIELDS, (
            f"列 `{column}` 排到平台字段 `{platform_field}`，但它不在白名单里 —— "
            f"点了会让平台 500。可用：{sorted(SORTABLE_ITEM_FIELDS)}"
        )


def test_price_column_sorts_by_price_not_price_min_fen() -> None:
    """价格列必须排 `price`。⚠️ 这是最容易写错的一处：

    列的数据字段叫 `price_min_fen`、返回行里价格叫 `min_price`，
    但**平台排序字段叫 `price`** —— 名字三者都不同，凭直觉写必错。
    """
    mapping = _frontend_sort_targets()
    assert mapping.get("price_min_fen") == "price", (
        "价格列的排序字段不是 `price`（平台只认这个名字，传 price_min_fen 会 500）"
    )


def test_backend_rejects_unknown_sort_field() -> None:
    """后端对白名单外的排序字段必须回结构化 400。"""
    src = GOODS_API.read_text(encoding="utf-8")
    assert "QIANFAN_SORT_FIELD_UNSUPPORTED" in src, (
        "路由没有 `QIANFAN_SORT_FIELD_UNSUPPORTED` 分支 —— 未知排序字段会被原样转发给平台"
    )
    assert re.search(r"sort_field\s+not\s+in\s+SORTABLE_ITEM_FIELDS", src), (
        "路由没有按 `SORTABLE_ITEM_FIELDS` 做白名单校验"
    )


@pytest.mark.parametrize(
    "column", ["price_min_fen", "stock", "sales_30d", "on_shelf_time"]
)
def test_page_renders_sorter_on_those_columns(column: str) -> None:
    """这四列的表头必须真的挂上 `sorter`（有映射但不挂箭头 = 用户点不到）。"""
    src = PAGE.read_text(encoding="utf-8")
    #: 找到该列的 dataIndex 块，块内必须有 sorter
    idx = src.index(f'dataIndex: "{column}"')
    window = src[idx : idx + 420]
    assert "sorter: true" in window, f"列 `{column}` 没有挂 `sorter`"
