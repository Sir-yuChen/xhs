"""枚举全量字典化守卫（设计 63，2026-10-27 用户口径「所有枚举常量走字典」）。

三条判据：
1. **种子可用**：`dict_seed.json` 能解析、组数 ≥ 46、code 唯一；
2. **镜像**：有后端常量的组，取值必须逐个一致（否则页面显示与业务判定各说一套）；
3. **前端不写死**：本次接入的 7 处枚举，渲染点必须是 `useDictOptions(...)` /
   `dictLabel(...)` / `dictListClass(...)`，不能直接吃硬编码常量。

⚠️ 第 3 条钉**渲染点**而不是「常量不存在」：这些常量按设计保留作**镜像兜底**
（字典未加载时下拉不能变空），删掉反而会让首屏空白。要防的是「只吃常量、字典白接」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

SEED = Path("config/initial_data/dict_seed.json")
FRONTEND = Path("frontend/src")

#: code → 后端常量的权威来源（import 路径, 名称）
PY_CONST_SOURCES = {
    "xhs_item_status": ("apis.xhs_qianfan_apis", "QIANFAN_ITEM_STATUSES"),
    "xhs_forbidden_word_match_mode": (
        "backend.app.models.forbidden_word",
        "_MATCH_MODES_FOR_GUARD",
    ),
}

#: 必须走字典的渲染点：(文件, 渲染处必须出现的字典调用)
DICT_RENDER_POINTS = [
    ("frontend/src/pages/platforms/xhs/shop-items-page.tsx", ['useDictOptions("xhs_item_status"']),
    ("frontend/src/pages/platforms/xhs/shop-items-page.tsx", ['useDictOptions("xhs_item_channel_type"']),
    ("frontend/src/pages/platforms/xhs/shop-items-page.tsx", ['useDictOptions("xhs_note_count_op"']),
    ("frontend/src/components/schedule/schedule-fields.tsx", ['useDictOptions("sys_misfire_policy"']),
    ("frontend/src/pages/platforms/xhs/data-package-rules-modal.tsx", ['dictLabel("xhs_package_rule_op"']),
    ("frontend/src/pages/platforms/xhs/forbidden-words-modal.tsx", [
        'useDictOptions("xhs_forbidden_word_match_mode"'
    ]),
    ("frontend/src/pages/system/system-logs-trace-tab.tsx", [
        'dictLabel("sys_log_source"', 'dictListClass("sys_log_source"'
    ]),
    ("frontend/src/pages/platforms/xhs/llm-generate-modal.tsx", ['dictLabel("sys_media_source"']),
]


def _seed() -> dict:
    return json.loads(SEED.read_text(encoding="utf-8"))


def test_seed_parses_and_groups_are_unique() -> None:
    """种子必须可解析、组数达标、code 不重复（重复 code 会让播种互相覆盖）。"""
    seed = _seed()
    codes = [t["code"] for t in seed["types"]]
    assert len(codes) >= 46, f"字典组只有 {len(codes)} 组，低于 46"
    assert len(codes) == len(set(codes)), f"存在重复 code：{len(codes) - len(set(codes))} 个"
    #: 每组都必须有 items 且 value 非空
    for t in seed["types"]:
        assert t["items"], f"字典组 {t['code']} 没有任何项"
        for item in t["items"]:
            assert str(item.get("value", "")).strip(), f"字典组 {t['code']} 有空 value"


@pytest.mark.parametrize("code", list(PY_CONST_SOURCES))
def test_group_mirrors_python_constant(code: str) -> None:
    """有后端常量的组，取值集合必须与常量逐个一致。

    不一致的后果是**静默的**：页面下拉能选，业务判定却走另一套值 ⇒ 筛了没效果。
    """
    module_name, attr = PY_CONST_SOURCES[code]
    module = __import__(module_name, fromlist=[attr])
    if attr == "_MATCH_MODES_FOR_GUARD":
        #: 违规词匹配模式是两个字面量常量，不在一张表里，按名字取。
        expected = {
            str(getattr(module, "MATCH_MODE_EXACT")),
            str(getattr(module, "MATCH_MODE_CONTAINS")),
        }
    else:
        expected = set(getattr(module, attr).keys())

    group = next(t for t in _seed()["types"] if t["code"] == code)
    actual = {str(i["value"]) for i in group["items"]}
    assert actual == expected, (
        f"字典组 {code} 与后端常量不一致：多 {actual - expected}，少 {expected - actual}"
    )


@pytest.mark.parametrize("path,markers", DICT_RENDER_POINTS)
def test_enum_render_point_goes_through_dict(path: str, markers: list[str]) -> None:
    """枚举渲染点必须走字典（只吃硬编码常量 = 字典白接）。"""
    text = Path(path).read_text(encoding="utf-8")
    for marker in markers:
        assert marker in text, f"{path} 里找不到 `{marker}` —— 该枚举没接字典"


def test_new_dict_groups_exist() -> None:
    """本轮为「商品类型 / 违规词匹配模式 / 日志来源」新建的 3 组必须在种子里。"""
    codes = {t["code"] for t in _seed()["types"]}
    for code in ("xhs_item_channel_type", "xhs_forbidden_word_match_mode", "sys_log_source"):
        assert code in codes, f"字典缺少 {code}"


def test_seed_json_is_utf8_without_bom() -> None:
    """种子必须是无 BOM 的 UTF-8（读取侧宽容 BOM，但随包形态要钉死）。"""
    raw = SEED.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "dict_seed.json 带 BOM 了"
    raw.decode("utf-8")  # 解不开就抛
