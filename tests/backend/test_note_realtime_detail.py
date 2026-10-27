"""挂链笔记 = 全实时（2026-10-27 用户口径：「笔记数据不再落库，应实时查询，所有笔记全部应是实时数据」）。

链路三要素，每条都要钉住：
1. **列表**按商品 ID 实时查千帆（`item_mix_search_word`），不读本地关系表；
2. **正文**按单篇实时查（`postgw/note/detail`）——列表接口没有正文，只能按篇取；
3. **切 Tab 才查**（不预加载）——N 篇 = N 次签名请求，预加载会拖慢弹窗并吃限流。
"""

from __future__ import annotations

import re
from pathlib import Path

SDK = Path("apis/xhs_qianfan_apis.py")
RT = Path("backend/app/services/realtime_platform_query_service.py")
GOODS_API = Path("backend/app/api/platforms/xhs/qianfan/goods.py")
ADAPTER = Path("backend/app/adapters/xhs/qianfan_api_adapter.py")
PAGE = Path("frontend/src/pages/platforms/xhs/shop-items-page.tsx")
API_TS = Path("frontend/src/lib/api.ts")


def _code(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*#.*$", "", text, flags=re.M)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return text


def test_sdk_has_signed_note_detail_on_edith_host() -> None:
    """SDK 必须有 `get_note_detail`，且**打 edith 域 + 带签名**。

    ⚠️ 两个易错点：域是 `edith.xiaohongshu.com`（不是 ark），无签名回 401。
    ⚠️ 域名断言要钉**常量赋值本身**，不能只查「文件里出现过 edith」——
    docstring 里也写了 edith，那样断言会被注释满足（变异实测逃逸过一次）。
    """
    src = _code(SDK)
    assert "def get_note_detail(" in src, "SDK 没有 `get_note_detail`"
    assert re.search(
        r'NOTE_DETAIL_HOST[^=]*=\s*"https://edith\.xiaohongshu\.com"', src
    ), "`NOTE_DETAIL_HOST` 不是 edith 域（ark 域调不通这条接口）"
    assert "self._sign(" in src, "笔记详情没带签名（无签名平台回 401）"
    assert "/web_api/sns/capa/postgw/note/detail" in src, "路径不是抓包得到的真实路径"


def test_service_layer_exposes_realtime_note_detail() -> None:
    """服务层必须提供 `get_note_detail` 并调用 adapter 的同名方法。"""
    src = _code(RT)
    assert "def get_note_detail(" in src, "服务层没有 `get_note_detail`"
    assert "adapter.get_note_detail(" in src, "服务层没调用 adapter 的 get_note_detail"
    #: 正文键必须从平台 `desc` 取（平台字段名就是这个）
    assert 'data.get("desc")' in src, "正文没有从平台 `desc` 取"


def test_adapter_passes_through_with_direct_env() -> None:
    """适配器要透传，且走 `direct_xhs_request_env()`（跨域直连）。"""
    src = _code(ADAPTER)
    assert "def get_note_detail(" in src, "适配器没有透传 get_note_detail"
    idx = src.index("def get_note_detail(")
    window = src[idx : idx + 500]
    assert "direct_xhs_request_env()" in window, "适配器没走直连环境"


def test_endpoint_exists_and_does_not_write() -> None:
    """端点必须存在，且**不写任何表**（只读实时查询）。"""
    src = _code(GOODS_API)
    assert '@router.post("/note-detail")' in src, "没有 `/note-detail` 端点"
    idx = src.index("def note_detail(")
    body = src[idx : src.index('@router.post("/items")', idx)]
    for write in ("db.add(", "db.commit(", "db.flush(", "db.delete("):
        assert write not in body, f"`/note-detail` 里出现了 `{write}` —— 它必须是只读的"


def test_item_notes_reads_platform_first() -> None:
    """列表必须先实时查平台（`list_goods_notes`），本地只补平台不回的字段。"""
    src = _code(GOODS_API)
    idx = src.index("def item_notes(")
    body = src[idx : src.index('@router.post("/note-detail")', idx)]
    assert "rt.list_goods_notes(" in body, "挂链笔记列表没有实时查平台"
    assert "note_goods" not in body.replace("note_goods`", ""), (
        "列表还在读本地 `note_goods` 关系表（该表已 DROP，实时链路不再依赖它）"
    )


def test_frontend_fetches_detail_per_tab_not_preloaded() -> None:
    """前端必须**切 Tab 才查**，不许预加载整页。"""
    src = _code(PAGE)
    assert "fetchQianfanNoteDetail(" in src, "前端没调用 `/note-detail`"
    #: tab 的 onChange 里要有 loadNoteDetail
    tabs_at = src.index("activeKey={notesTab}")
    window = src[tabs_at : tabs_at + 700]
    assert "loadNoteDetail(" in window, "切换 Tab 没有触发详情查询"
    #: 不许出现「循环给每篇都拉一次」——
    for bad in ("notes.map((note) => void loadNoteDetail", "forEach((note) => void loadNoteDetail"):
        assert bad not in src, f"出现了预加载形态 `{bad}` —— 应只在切到某篇时查"


def test_frontend_has_detail_api_function() -> None:
    """前端 API 函数必须存在且指向 `/xhs/qianfan/note-detail`。"""
    src = API_TS.read_text(encoding="utf-8")
    assert "export async function fetchQianfanNoteDetail(" in src
    idx = src.index("export async function fetchQianfanNoteDetail(")
    window = src[idx : idx + 400]
    assert "/xhs/qianfan/note-detail" in window
