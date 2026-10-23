"""店铺违规管理（设计 42）守卫 —— **纯离线**，一个真实平台请求都不打（D12）。

## 覆盖（卡 3 口径 1–8 逐条落地）

| 组 | 打的是什么 |
|---|---|
| A 时间口径 | `day_range_to_ms` 逐毫秒对齐真机（`1791561600000` / `1792252799999`）；结束是当天 23:59:59.999，**不是**次日零点 / 23:59:59 整 |
| B 原因 tag | 同名不同 ID（含 **U+200B 零宽空格**）不被 trim / 去重 / 合并；命中取全部 tagIds |
| C 列表请求体 | 「全部」省略 `violationEntityTypes`；快捷筛选逐键；时间只走唯一换算 |
| D 缺值 | `_text("")` ⇒ `None`；归一化不造 0 / 空串 |
| E 只读边界 | 适配器与 SDK **都不存在** `read_mark` / 申诉提交方法（D2/D3）；违规域**全部**签名；路由代码里不出现写端点 |
| F 门禁 | 无 ark 令牌 ⇒ 400 `ARK_SESSION_REQUIRED`（不借会话）；非本人账号 ⇒ 404；平台失败 ⇒ 502 结构化 |
| G 请求模型 | 全部 `extra="forbid"`（多传字段 422 而不是静默丢弃） |
| H 零落库 | 服务层不 import ORM；路由层不 commit；**调完端点 `metadata.tables` 一张不多** |

## 本轮的变异验证纪律

「守卫看着对」不等于「守卫抓得住」：`卡 3` 的每个变异（`strip_tag` / `unsig` / `endtime` /
`batchallow` / `appealmock` / `compositedrop`）都必须真的让本文件里的某一条**红**。
跑法见 `.hermes/scripts/mutate_verify_violation_manage.py`（目标 6/6 CAUGHT，不留哑弹）。
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re
from typing import Any

import pytest
import requests
from fastapi import HTTPException

from backend.app.services import violation_service as vs



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.redline

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICE_PATH = ROOT / "backend/app/services/violation_service.py"
SDK_PATH = ROOT / "apis/xhs_qianfan_apis.py"
ROUTE_PATH = ROOT / "backend/app/api/platforms/xhs/qianfan/violation.py"
FRONTEND_LIB = ROOT / "frontend/src/lib/violation-manage.ts"

#: 违规域 SDK 方法名前缀（前端 / 适配器委托 / 签名 三处判据共用）。
SDK_PREFIXES = (
    "post_violation",
    "get_violation",
    "post_negative_feedback",
    "post_seller_high_price",
    "post_compensate",
    "post_arbitration",
    "post_spot_check",
    "post_exemption",
)


def _violation_sdk_methods(cls: type) -> list[str]:
    return [n for n in dir(cls) if n.startswith(SDK_PREFIXES)]


def _code_only(src: str) -> str:
    """剥掉注释与字符串字面量 —— 判据要落在**代码**上，不能被 docstring 里的说明命中。"""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    # 大段 docstring 被挖空后留下空行不影响 `in` 判据；注释本就进不了 AST。
    return ast.unparse(tree)


def _names_called_in_route_module() -> set[str]:
    """路由模块里**出现过的方法名**（签名域白名单之外一个都不该有）。"""
    tree = ast.parse(ROUTE_PATH.read_text(encoding="utf-8"))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


# --------------------------------------------------------------------- #
# A · 时间口径（D9）
# --------------------------------------------------------------------- #
def test_day_range_matches_real_device_ms():
    """真机抓包：`2026-10-10 ~ 2026-10-17` ⇒ `(1791561600000, 1792252799999)`。

    变异 `end_next_midnight`（结束改次日 00:00）与 `end_no_ms`（改 23:59:59 整）都必须红。
    """
    start, end = vs.day_range_to_ms("2026-10-10", "2026-10-17")
    assert start == 1791561600000
    assert end == 1792252799999
    assert end - start > 0
    # 结束必须是「当天最后一毫秒」，写成次日零点会静默漏掉最后一天 23:59:59.9xx
    assert end % 1000 == 999


def test_day_range_single_day_covers_whole_day():
    start, end = vs.day_range_to_ms("2026-10-18", "2026-10-18")
    assert start is not None and end is not None
    assert end - start == 86_400_000 - 1


def test_day_range_absent_side_returns_none():
    assert vs.day_range_to_ms("", "") == (None, None)
    start, end = vs.day_range_to_ms("2026-10-10", "")
    assert start == 1791561600000 and end is None


def test_day_range_accepts_date_and_bad_value():
    from datetime import date

    assert vs.day_range_to_ms(date(2026, 10, 10), date(2026, 10, 17)) == (
        1791561600000,
        1792252799999,
    )
    # 坏值不抛（不让一个脏值把整页打成 500）
    assert vs.day_range_to_ms("不是日期", None) == (None, None)


# --------------------------------------------------------------------- #
# B · 原因 tag（D8 / R2 / R3）
# --------------------------------------------------------------------- #
#: 真机 `user_tag` 实测（2026-10-18，账号 8）：两条「违反法律法规」是**不同 tag** ——
#: 第二条以 U+200B（零宽空格）结尾，肉眼不可见。
REAL_TAGS = [
    {"tagName": "违反法律法规", "tagIds": ["80001101", "80001738", "80001764", "80001776", "80001801"]},
    {"tagName": "违反法律法规\u200b", "tagIds": ["80005677"]},
    {"tagName": "站外导流", "tagIds": ["80000354", "80006709"]},
    {"tagName": "笔记存在创作质量不佳的内容 ", "tagIds": ["79548256"]},
]


def test_reason_tag_ids_keeps_same_named_tags_apart():
    plain = vs.reason_tag_ids(REAL_TAGS, "违反法律法规")
    zero_width = vs.reason_tag_ids(REAL_TAGS, "违反法律法规\u200b")
    assert plain == ["80001101", "80001738", "80001764", "80001776", "80001801"]
    assert zero_width == ["80005677"]
    assert plain != zero_width


def test_reason_tag_ids_is_exact_no_trim():
    """按名字查 tagIds 必须是**精确匹配**：多一个不可见字符 / 少一个尾空格都不能命中。

    ⚠️ 这条是变异 `strip_tag`（把 `tag_name` 与查询名都 `.strip()`）的靶子：
    trim 之后 `违反法律法规` 与 `违反法律法规\u200b` 会被判成同一条，
    一个原因静默变成另一个原因的过滤结果 —— 列表条数变了但界面不会报错。
    """
    assert vs.reason_tag_ids(REAL_TAGS, "违反法律法规") == [
        "80001101", "80001738", "80001764", "80001776", "80001801",
    ]
    assert vs.reason_tag_ids(REAL_TAGS, "违反法律法规\u200b") == ["80005677"]
    assert vs.reason_tag_ids(REAL_TAGS, "笔记存在创作质量不佳的内容") == []
    assert vs.reason_tag_ids(REAL_TAGS, "笔记存在创作质量不佳的内容 ") == ["79548256"]
    assert vs.reason_tag_ids(REAL_TAGS, "不存在的原 因") == []


def test_reason_tag_ids_never_trims_any_of_the_four_real_names():
    """四个真机 tagName 全部**原样**可查（一个字符都不许动）。

    逐条查一遍：谁被 trim 了，它自己那条就查不到 ⇒ 红。
    `strip_tag` 变异只 trim 一侧时也逃不掉（两侧都查）。
    """
    for tag in REAL_TAGS:
        assert vs.reason_tag_ids(REAL_TAGS, tag["tagName"]) == tag["tagIds"], repr(tag["tagName"])


def test_normalize_reason_tags_preserves_invisible_chars():
    rows = vs.normalize_reason_tags(REAL_TAGS)
    assert [r["tag_name"] for r in rows] == [t["tagName"] for t in REAL_TAGS]
    assert len(rows) == 4  # 不去重
    assert rows[1]["tag_ids"] == ["80005677"]
    assert rows[2]["tag_ids"] == ["80000354", "80006709"]


def test_normalize_reason_tags_skips_rows_without_name():
    assert vs.normalize_reason_tags([{"tagIds": ["1"]}, "bad", {"tagName": None}]) == []


def test_build_list_body_tag_id_group_survives_trim_mutation():
    """端到端形态：两个同名原因各自组装出的 `tagIdGroup` 必须**不同**。"""
    plain = vs.build_list_body(reason_name="违反法律法规", reason_tags=REAL_TAGS)
    zero_width = vs.build_list_body(reason_name="违反法律法规\u200b", reason_tags=REAL_TAGS)
    trailing = vs.build_list_body(reason_name="笔记存在创作质量不佳的内容 ", reason_tags=REAL_TAGS)
    assert plain["tagIdGroup"] == ["80001101", "80001738", "80001764", "80001776", "80001801"]
    assert zero_width["tagIdGroup"] == ["80005677"]
    assert trailing["tagIdGroup"] == ["79548256"]
    assert plain["tagIdGroup"] != zero_width["tagIdGroup"]


# --------------------------------------------------------------------- #
# C · 列表请求体（D10 / R4 / R5）
# --------------------------------------------------------------------- #
def test_build_list_body_default_shape():
    body = vs.build_list_body()
    assert body == {"dataFlag": True, "warnFlag": 0, "pageNo": 1, "pageSize": 10}


def test_entity_all_is_omitted_not_sent():
    """\"全部\"（999）是下拉值，**不是**请求值 —— 原样下发平台会当非法类型返空。"""
    assert "violationEntityTypes" not in vs.build_list_body(entity_types=[999])
    assert vs.build_list_body(entity_types=[1, 999]) == {
        "dataFlag": True,
        "warnFlag": 0,
        "pageNo": 1,
        "pageSize": 10,
        "violationEntityTypes": [1],
    }


def test_build_list_body_reason_uses_tag_id_group():
    body = vs.build_list_body(reason_name="站外导流", reason_tags=REAL_TAGS)
    assert body["tagIdGroup"] == ["80000354", "80006709"]
    # 查不到的因 ⇒ 不带条件（响应 query 里看得出来），不抛、不塞空数组
    body = vs.build_list_body(reason_name="不存在", reason_tags=REAL_TAGS)
    assert "tagIdGroup" not in body


def test_build_list_body_advanced_fields():
    body = vs.build_list_body(
        scene=vs.SCENE_WARNING,
        page_no=2,
        page_size=50,
        keyword="  426351981816704467  ",
        entity_types=[1],
        statuses=[2],
        severe_only=True,
        allow_appeal=True,
        correctable=False,
        unread_only=True,
        exam_only=True,
        pending_only=True,
        date_start="2026-10-10",
        date_end="2026-10-17",
    )
    assert body["warnFlag"] == 1
    assert body["pageNo"] == 2 and body["pageSize"] == 50
    assert body["fuzzyMatchId"] == "426351981816704467"
    assert body["violationEntityTypes"] == [1]
    assert body["violationStatus"] == [2]
    assert body["severePunishmentFlag"] is True
    assert body["allowAppeal"] is True
    assert body["correctableFlag"] is False  # ⚠️ False 是「不可整改」这个明确条件，不能吞
    assert body["readMark"] == 0
    assert body["examStatusList"] == [1]
    assert body["filterStatus"] == "COMPOSITE"
    assert body["violationStartTime"] == 1791561600000
    assert body["violationEndTime"] == 1792252799999


def test_build_list_body_never_uses_read_mark_write_semantics():
    """只读边界的机械判据：本域组装的请求体里，`readMark` 只允许是 `0`（查询条件）。"""
    for quick in vs.QUICK_VIOLATION_KEYS:
        body = vs.quick_filter_body(quick)
        assert body.get("readMark") in (None, 0)


def test_quick_filter_pending_uses_composite_flag():
    """「待处理」= `filterStatus:"COMPOSITE"`（真机 bundle 原文）。

    ⚠️ 这条是变异验证逼出来的（2026-10-18）：`composite_drop`（把 quick 的
    `pending` 映射清空）原先**逃逸**——`test_build_list_body_advanced_fields`
    断言的是「手动传 `pending_only=True`」时的组装，而快捷筛选走的是另一条映射表，
    两条路径没有交叉断言，于是「待处理」静默退化成「全部」。

    判据必须落在**快捷映射**上，不能只落在底层组装函数上。
    """
    body = vs.quick_filter_body("pending")
    assert body["filterStatus"] == "COMPOSITE"
    # 待处理不该带别的过滤条件（真机：只发 filterStatus）
    assert "violationStatus" not in body
    assert "allowAppeal" not in body


def test_quick_filter_pending_survives_route_path(tmp_path):
    """端到端：走路由的快捷分支（`payload.quick`）也必须带 `filterStatus`。

    ⚠️ 路由里快捷分支**自己拼 body**（`quick_filter_body` + 分页），
    与 `build_list_body` 是两条路径 —— 变异 `compositedrop` 若只落在服务层，
    这条与上一条会同时红（两条都钉住，不留单点）。
    """
    from backend.app.api.platforms.xhs.qianfan import violation as v

    seen: list[dict] = []

    class _Adapter:
        def post_violation_list(self, body):
            seen.append(body)
            return True, "ok", {"code": 200, "data": {"dtos": [], "counts": 0}}

    account = type("A", (), {"id": 8, "nickname": "n", "seller_id": "s"})()
    v.violation_list.__globals__["_context"]  # noqa: B018 - 存在性即可（下面整体打桩）
    import backend.app.api.platforms.xhs.qianfan.violation as route

    original_context = route._context
    route._context = lambda db, user, account_id, factory: (account, _Adapter())
    try:
        out = route.violation_list(
            route.ViolationListRequest(platform_account_id=8, quick="pending"),
            current_user=None,
            db=None,
            adapter_factory=None,
        )
    finally:
        route._context = original_context
    assert seen and seen[0]["filterStatus"] == "COMPOSITE"
    assert out["query"]["filterStatus"] == "COMPOSITE"


def test_quick_filter_keys_are_the_real_page_set():
    assert vs.QUICK_VIOLATION_KEYS == (
        "all",
        "pending",
        "unread",
        "severe",
        "recent7",
        "correctable",
        "examinable",
        "appealable",
    )
    assert vs.QUICK_WARNING_KEYS == ("all", "high_risk", "correctable", "appealable")
    assert len(vs.QUICK_VIOLATION_KEYS) == 8
    assert len(vs.QUICK_WARNING_KEYS) == 4


def test_quick_filter_severe_is_flag_plus_statuses():
    body = vs.quick_filter_body("severe")
    assert body["severePunishmentFlag"] is True
    assert body["violationStatus"] == [0, 1, 2]


def test_quick_filter_recent7_has_statuses_and_window():
    body = vs.quick_filter_body("recent7")
    assert body["violationStatus"] == [0, 1, 2]
    start, end = body["violationStartTime"], body["violationEndTime"]
    assert isinstance(start, int) and isinstance(end, int)
    assert end - start == vs.RECENT_DAYS * 86_400_000


def test_quick_filter_examinable_is_exam_status_list():
    """「可考试」⇒ `examStatusList:[1]`（只读条件，不是 `severePunishmentFlag`）。"""
    body = vs.quick_filter_body("examinable")
    assert body["examStatusList"] == [1]
    assert "severePunishmentFlag" not in body


def test_every_quick_key_maps_to_its_own_single_answer():
    """八个快捷键逐个断言（卡 3 口径 4 的「快捷筛选逐键」）。

    每个键只允许带**它自己那一个**判据 —— 多带一个条件就是把用户没选的过滤
    静默加上，条数变少但界面不报错。
    """
    expected = {
        "all": {},
        "unread": {"readMark": 0},
        "severe": {"severePunishmentFlag": True, "violationStatus": [0, 1, 2]},
        "correctable": {"correctableFlag": True},
        "examinable": {"examStatusList": [1]},
        "appealable": {"allowAppeal": True},
        "pending": {"filterStatus": "COMPOSITE"},
    }
    for key, want in expected.items():
        body = vs.quick_filter_body(key)
        for field, value in want.items():
            assert body[field] == value, (key, field)
        got_conditions = set(body) - {"dataFlag", "warnFlag", "pageNo", "pageSize"}
        assert got_conditions == set(want), (key, got_conditions)


def test_quick_filter_warning_set():
    assert vs.quick_filter_body("high_risk", scene=vs.SCENE_WARNING)["severePunishmentFlag"] is True
    assert vs.quick_filter_body("all", scene=vs.SCENE_WARNING)["warnFlag"] == 1
    # 违规列表的 quick 键用在预警页 ⇒ 退化成「全部」（不崩）
    assert "severePunishmentFlag" not in vs.quick_filter_body("examinable", scene=vs.SCENE_WARNING)


# --------------------------------------------------------------------- #
# D · 缺值（硬约束⑤ / R6）
# --------------------------------------------------------------------- #
def test_text_empty_is_none_not_empty_string():
    assert vs._text("") is None
    assert vs._text(None) is None
    assert vs._text(0) == "0"
    assert vs._text("x") == "x"


def test_normalize_row_missing_fields_are_none():
    row = vs.normalize_violation_row({"violationId": "1"})
    for key in (
        "violation_reason",
        "violation_desc",
        "tenant",
        "entity_type",
        "entity_type_name",
        "status",
        "status_name",
        "appeal_status",
        "level",
    ):
        assert row[key] is None, key
    assert row["actions"] == [] and row["action_codes"] == []


def test_normalize_row_keeps_false_and_zero():
    """`False` / `0` 是**明确值**，不是缺值 —— 这是「缺值渲染 -、不渲染 0」的前提。"""
    row = vs.normalize_violation_row(
        {
            "violationId": "1",
            "correctableFlag": False,
            "severePunishmentFlag": False,
            "readMark": 0,
            "violationStatusInfo": {"violationStatus": 0, "displayName": "待处罚"},
            "violationLevel": {"violationLevel": 2, "name": "严重违规"},
        }
    )
    assert row["correctable"] is False
    assert row["severe"] is False
    assert row["read"] == 0
    assert row["status"] == 0
    # ⚠️ 严重违规与否只看 severePunishmentFlag，不从 level 推
    assert row["level"] == 2


def test_normalize_list_payload_reads_counts_and_vos():
    payload = {"data": {"dtos": [{"violationId": "a"}, "bad"], "counts": 56}}
    out = vs.normalize_list_payload(payload)
    assert out["total"] == 56 and len(out["items"]) == 1
    out = vs.normalize_list_payload({"data": {"vos": [{"violationId": "b"}], "total": 1}})
    assert out["total"] == 1 and out["items"][0]["violation_id"] == "b"
    assert vs.normalize_list_payload(None) == {"total": 0, "items": []}


def test_normalize_detail_requires_nested_info():
    raw = {"data": {"detail": {"info": {"violationId": "x", "violationActions": [
        {"actions": [{"actionCode": "OFF_SHELF", "displayName": "商品下架",
                      "actionTimeInfo": "执行时间：2026.09.27～2027.09.27"}]}
    ]}}}}
    detail = vs.normalize_detail(raw)
    assert detail["violation_id"] == "x"
    assert detail["action_details"] == [
        {"name": "商品下架", "code": "OFF_SHELF", "time_info": "执行时间：2026.09.27～2027.09.27", "status": None}
    ]
    # 取不到 `info` ⇒ 返回空 dict（调用方按「平台没给详情」处理，不造行）
    assert vs.normalize_detail({"data": {}}) == {}
    assert vs.normalize_detail(None) == {}


def test_normalize_meta_drops_rows_without_value_or_label():
    meta = vs.normalize_meta(
        statuses=[{"violationStatus": 0, "displayName": "待处罚"}, {"violationStatus": 1}],
        entities=[{"type": 999, "displayName": "全部"}],
        reason_tags=REAL_TAGS,
        spot_check_tags=[],
    )
    assert meta["violation_status"] == [{"value": 0, "label": "待处罚"}]
    assert meta["violation_entity"] == [{"value": 999, "label": "全部"}]
    assert len(meta["reason_tags"]) == 4
    assert meta["spot_check_reason_tags"] == []


def test_negative_feedback_and_exemption_bodies():
    q = vs.build_negative_feedback_body(kind="quality")
    assert q["timeWindowCode"] == "30"  # ⚠️ 字符串，不是 int
    assert "shipLogisticsNegativeCountOrderField" not in q
    lg = vs.build_negative_feedback_body(kind="logistics")
    assert lg["shipLogisticsNegativeCountOrderField"] == 0 and lg["showNeedSolve"] == 0
    ex = vs.build_exemption_body(start="2026-09-19", end="2026-10-18")
    assert ex["startDtm"] == "2026-09-19" and ex["endDtm"] == "2026-10-18"  # 日期串，不是毫秒
    assert ex["pageNo"] == 1


# --------------------------------------------------------------------- #
# E · 只读边界（D2 / D3）—— 机械判据，不是口头约定
# --------------------------------------------------------------------- #
def test_no_write_methods_exist_on_adapter_or_sdk():
    from apis.xhs_qianfan_apis import XHS_Qianfan_Apis
    from backend.app.adapters.xhs.qianfan_api_adapter import XhsQianfanApiAdapter

    #: 只读边界的判据：**违规域**不允许存在写语义方法（`appeal_record` 是读申诉记录，不在禁列）。
    #: ⚠️ 只查违规域 —— 适配器整体还承载商品/图片空间等**别的域**的写链路（`batch_delete_*`），
    #:    那不是本功能的边界（设计 42 只约束 violation 域）。
    forbidden = ("read_mark", "appeal_submit", "submit")
    for target in (XhsQianfanApiAdapter, XHS_Qianfan_Apis):
        violation_methods = [n for n in dir(target) if "violation" in n]
        for bad in forbidden:
            hits = [n for n in violation_methods if bad in n]
            assert hits == [], f"{target.__name__} 违规域暴露了写语义方法：{hits}"
        assert [n for n in violation_methods if "delete" in n] == []


def test_violation_sdk_methods_are_all_signed():
    """违规域 16 个方法必须走签名（无签名一律 406）。

    AST 判据：方法体里**不得**出现裸 `self._post(...)` / `self._get(...)`
    （变异 `unsig` 把一处 `_signed_post` 改成 `_post` ⇒ 这里必红）。
    """
    from apis.xhs_qianfan_apis import XHS_Qianfan_Apis

    names = _violation_sdk_methods(XHS_Qianfan_Apis)
    assert len(names) >= 16, names
    module_tree = ast.parse(SDK_PATH.read_text(encoding="utf-8"))
    methods = {
        node.name: node
        for node in ast.walk(module_tree)
        if isinstance(node, ast.ClassDef) and node.name == "XHS_Qianfan_Apis"
        for node in node.body
        if isinstance(node, ast.FunctionDef)
    }
    for name in names:
        func = methods.get(name)
        assert func is not None, f"{name} 不在 XHS_Qianfan_Apis 的类体里"
        # ⚠️ 不能用 `inspect.getsource` + `ast.parse`：多行 docstring 被 `cleandoc` 后
        #    缩进丢失 ⇒ IndentationError（实测）。直接扫**整文件 AST** 里这个方法节点。
        bare = [
            node.func.attr
            for node in ast.walk(func)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr in {"_post", "_get"}
        ]
        assert bare == [], f"{name} 走了无签名调用：{bare}"
        called = {
            node.func.attr
            for node in ast.walk(func)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        }
        assert called & {"_signed_post", "_signed_get"}, f"{name} 没有走签名：{called}"


def test_violation_route_only_calls_read_methods():
    """路由模块里出现的**方法名**必须全部属于违规域**只读**方法集。

    比「黑名单字符串」更强的判据：黑名单必然漏（变异 `batchallow` 就是用一个
    **没被列进黑名单**的只读失败方法去替换写方法 —— 它会改变行为却不含禁词）。
    白名单从 SDK 实现里现取，不手抄。
    """
    from apis.xhs_qianfan_apis import XHS_Qianfan_Apis

    allowed = set(_violation_sdk_methods(XHS_Qianfan_Apis))
    called = _names_called_in_route_module()
    # 路由里还有非 SDK 的普通方法调用（`.model_dump()` 之类），只筛「看起来像 SDK 的」
    sdk_shaped = {n for n in called if n.startswith(SDK_PREFIXES)}
    unknown = sorted(sdk_shaped - allowed)
    assert unknown == [], f"路由调用了违规域不存在 / 写语义的方法：{unknown}"


def test_route_source_has_no_write_endpoint_strings():
    """路由**代码**（剥注释 / 剥字符串）里不得出现写平台状态的端点或方法名。

    ⚠️ 2026-10-18 收窄（设计 44）：新增「删除关联内容」后，路由**允许**出现
    `delete_` 前缀 —— 但只允许指向**服务层**的 `delete_related_*` / `execute_delete`，
    不许直接调平台写方法（`batch_delete` / `remove_items` / `read_mark` …）。
    判据因此从「黑名单里一个都不许有」改为：
    「平台写方法一个都不许有」+「删除只出现在服务层调用里」。
    """
    code = _code_only(ROUTE_PATH.read_text(encoding="utf-8"))
    # 平台写方法：路由里**一律**不许出现（走服务层，不直接打平台）
    forbidden_platform = (
        "read_mark",
        "readMark",
        "appeal_submit",
        "submit_appeal",
        "batch_delete",
        "batch_update",
        "remove_items",
    )
    hits = [token for token in forbidden_platform if token in code]
    assert hits == [], f"违规管理路由的函数体里出现写平台状态的调用：{hits}"

    # `delete_` 只允许出现在「调服务层」的位置
    for line in code.splitlines():
        if "delete_" not in line:
            continue
        assert (
            "execute_delete" in line
            or "from backend.app.services" in line
            or "MAX_DELETE_TARGETS" in line
            or "violation_delete_service" in line
        ), f"路由里出现了非服务层的 delete_ 调用：{line.strip()}"
    # 正例：只读方法必须在（否则上面的负例可以靠「路由什么都不调」通过 —— 空集恒真）
    assert "post_violation_list" in code
    assert "post_violation_info" in code


def test_route_declares_no_write_paths():
    """本域扣住的**全部 15 条**路由里不许出现写语义路径段（read-mark / submit / delete / write）。

    路径从 `openapi()` 取（不是 `app.routes`：后者在新版 FastAPI 里只有 `_IncludedRouter` 壳）。
    ⚠️ 前缀是平铺的 `/api/xhs/qianfan/`（`checkup/` / `compensations/` 等直接挂父 router），
    只认 `/violation/` 前缀会漏掉一半路由 —— 那正是「守卫看着在、其实只看了 4 成」。
    """
    from backend.app.main import app

    paths = [p for p in app.openapi()["paths"] if p.startswith("/api/xhs/qianfan/")]
    owned = [p for p in paths if "/violation/" in p or p.split("/api/xhs/qianfan/")[1].startswith(
        ("checkup/", "compensations", "arbitrations", "arbitration-tags", "spot-checks", "exemptions/")
    )]
    # 设计 44（2026-10-18）：+1 条 `violation/related-delete`（删除关联内容）⇒ 15 → 16
    assert len(owned) == 16, owned

    # ⚠️ 所有违规域端点的路径**必须**带 `/violation/` 或上面那几个已知前缀之一 ——
    #    写 `@router.post("/related-delete")` 会漏掉 `violation/` 段，变成
    #    `/api/xhs/qianfan/related-delete`（不在本域命名空间里）。这条一并挡住。
    stray = [
        p for p in owned
        if "/violation/" not in p
        and not p.split("/api/xhs/qianfan/")[1].startswith(
            ("checkup/", "compensations", "arbitrations", "arbitration-tags", "spot-checks", "exemptions/")
        )
    ]
    assert stray == [], f"违规域端点路径缺少命名空间前缀：{stray}"

    # 写语义路径段：`read_mark` / `submit` / `write` 一律不许；`delete` 只允许
    # `related-delete`（设计 44：删关联内容，不是删违规记录）
    bad = [p for p in owned if re.search(r"(read[-_]?mark|submit|write|/appeal/(?!record))", p)]
    assert bad == [], f"违规管理路由暴露了写语义路径：{bad}"
    for p in owned:
        if "delete" in p:
            assert p.endswith("/violation/related-delete"), f"只允许 related-delete：{p}"


def test_violation_adapter_delegates_every_sdk_method():
    from backend.app.adapters.xhs.qianfan_api_adapter import XhsQianfanApiAdapter
    from apis.xhs_qianfan_apis import XHS_Qianfan_Apis

    sdk = set(_violation_sdk_methods(XHS_Qianfan_Apis))
    adapter = set(dir(XhsQianfanApiAdapter))
    missing = sorted(sdk - adapter)
    assert missing == [], f"适配器缺委托：{missing}"


def test_sdk_call_does_not_trigger_network(monkeypatch):
    """假签名 + 假传输：调一个违规域方法，**只发 1 次**请求到假传输上，不碰真网络。

    `_sign` 与 `_session_request` 都打桩 ⇒ 本用例在离线机上也能证明
    「方法体真的走签名 + 真的把签名头带上了」—— 这对变异 `unsig` 是第二道网
    （`_signed_post` 被换成 `_post` 时，签名头不再出现 ⇒ 红）。
    """
    import apis.xhs_qianfan_apis as sdk_mod

    sent: list[dict] = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"code": 200, "success": True, "msg": "ok", "data": {"counts": 1}}

    def _fake_request(method, url, **kwargs):
        sent.append({"method": method, "url": url, "headers": kwargs.get("headers") or {}})
        return _Resp()

    # ⚠️ 必须打在 `requests.Session` **类**上，不能打模块里的 `_session_request`
    #    （`_signed_post` 在**模块加载时**就绑定了它 ⇒ 事后 setattr 无效，变异
    #    `sign_header_dropped` 曾因此逃逸 —— 哑弹，已修）。打在类上对两种形态都生效：
    #    裸 `_post` 与 `_signed_post` 都最终走 `_http_session.request`。
    monkeypatch.setattr(requests.Session, "request", lambda self, method, url, **kw: _fake_request(method, url, **kw))

    # 自检：本机 `_sign` 必须能真的产出签名头（否则下面的断言会变成「恒假 / 恒真」的空转：
    # 真机环境下 `_sign` 是能签的，用 `in produced[0]` 这种**子串**判据会被字典的
    # repr（任意键名都可能是某值的子串）误判 —— 必须按键名判）。
    produced = sdk_mod.XHS_Qianfan_Apis(cookies="a1=1")._sign("/x", {}, sdk_mod.VIOLATION_REFERER)
    assert produced[1] is None, produced[1]
    assert produced[0] is not None and "x-s" in produced[0].keys(), list((produced[0] or {}).keys())
    monkeypatch.setattr(
        sdk_mod.XHS_Qianfan_Apis, "_sign", lambda self, api, body, referer: (
            {"Referer": referer, "x-s": "fake", "x-t": "1"}, None,
            {"xs": "fake", "xt": "1"},
        )
    )
    # ⚠️ 直接调 **`_signed_post`** 而不是某个域方法：域方法体短（一行 `return
    #    self._signed_post(...)`），谁调的 `_signed_post` 对签名头没有区别 ⇒ 用域方法
    #    做被试对象时，「删掉 x-s 头」的变异**抓不到**（实测哑弹）。本用例的判据是
    #    **签名结果的兑现**：签名头必须真的出现在发出的请求上。
    api = sdk_mod.XHS_Qianfan_Apis(cookies="a1=1")
    ok, msg, payload = api._signed_post(
        "/api/edith/ark/violation/risk/list", {"dataFlag": True, "warnFlag": 0}, sdk_mod.VIOLATION_REFERER
    )
    assert ok is True and msg
    assert len(sent) == 1, f"只允许发 1 次请求（实际 {len(sent)}）—— 多余的请求就是真实网络"
    assert sent[0]["method"] == "POST"
    assert sent[0]["url"] == "https://ark.xiaohongshu.com/api/edith/ark/violation/risk/list"
    assert sent[0]["headers"].get("x-s") == "fake", "签名头没带上 ⇒ 平台会 406"
    assert sent[0]["headers"].get("x-t") == "1"


# --------------------------------------------------------------------- #
# F · 门禁与错误映射
# --------------------------------------------------------------------- #
class _FakeFactory:
    """假适配器：只记录调用，返回固定载荷。"""

    def __init__(self, payload=None, ok=True, msg="ok"):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.payload = payload if payload is not None else {"code": 200, "success": True, "data": {}}
        self.ok = ok
        self.msg = msg

    def __call__(self, cookies):  # 工厂契约：只吃 cookies
        return self

    def __getattr__(self, name):
        if not name.startswith(SDK_PREFIXES):
            raise AttributeError(name)

        def _call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self.ok, self.msg, self.payload

        return _call


@pytest.fixture()
def owner(monkeypatch):
    """把「账号归属 + ark 令牌」两处换成确定值（**不打库、不打平台**）。"""
    from backend.app.api.platforms.xhs.qianfan import violation as v

    account = type("A", (), {"id": 8, "nickname": "观隅好物的店", "seller_id": "6a3250e48dd4d00015ca26a0"})()
    monkeypatch.setattr(v, "_owned_account", lambda db, user, account_id: account)
    monkeypatch.setattr(v, "_ark_adapter_for", lambda db, acct, factory: factory("cookies"))
    return v, account


def test_missing_ark_session_is_400(monkeypatch):
    """门禁唯一实现：无令牌 ⇒ 400 `ARK_SESSION_REQUIRED`，**不借会话**。"""
    from backend.app.api.platforms.xhs.qianfan._shared import _ark_adapter_for

    monkeypatch.setattr(
        "backend.app.services.shop_binding_service.qianfan_cookies_for_account",
        lambda db, account_id: "",
    )
    with pytest.raises(HTTPException) as exc:
        _ark_adapter_for(None, type("A", (), {"id": 8})(), object)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "ARK_SESSION_REQUIRED"


def test_owned_account_404_for_foreign_or_missing_account(db_session):
    """非本人账号 ⇒ 404（与 `_owned_account` 同一判据：不区分「不存在」与「不是你的」）。"""
    from backend.app.core.deps import get_owned_active_account
    from backend.app.models.platform_account import PlatformAccount
    from backend.app.models.user import User

    db = db_session
    db.add(User(id=1, username="u1", password_hash="x"))
    db.add(User(id=2, username="u2", password_hash="x"))
    db.flush()
    db.add(PlatformAccount(id=501, user_id=2, platform="xhs", nickname="别人的"))
    db.add(PlatformAccount(id=502, user_id=1, platform="xhs", nickname="我的"))
    db.commit()

    me = db.get(User, 1)
    with pytest.raises(HTTPException) as foreign:
        get_owned_active_account(db, me, 501, platform="xhs")
    assert foreign.value.status_code == 404
    with pytest.raises(HTTPException) as missing:
        get_owned_active_account(db, me, 999999, platform="xhs")
    assert missing.value.status_code == 404
    # 正例：自己的账号放行（否则上两条可以靠「恒 404」通过）
    assert get_owned_active_account(db, me, 502, platform="xhs").id == 502


def test_soft_deleted_account_is_404(db_session):
    """软删的账号也必须 404（DEF-055 的闸 —— 旧页面残留 id 不能继续用）。"""
    from datetime import datetime

    from backend.app.core.deps import get_owned_active_account
    from backend.app.models.platform_account import PlatformAccount
    from backend.app.models.user import User

    db = db_session
    db.add(User(id=1, username="u1", password_hash="x"))
    db.flush()
    db.add(
        PlatformAccount(
            id=503, user_id=1, platform="xhs", nickname="已删", deleted_at=datetime(2026, 10, 1)
        )
    )
    db.commit()
    with pytest.raises(HTTPException) as exc:
        get_owned_active_account(db, db.get(User, 1), 503, platform="xhs")
    assert exc.value.status_code == 404


def test_list_upstream_failure_is_502(owner):
    v, _account = owner
    factory = _FakeFactory(ok=False, msg="平台拒绝了本次请求的签名（HTTP 406）")
    with pytest.raises(HTTPException) as exc:
        v.violation_list(
            v.ViolationListRequest(platform_account_id=8), current_user=None, db=None, adapter_factory=factory
        )
    assert exc.value.status_code == 502
    assert exc.value.detail["code"] == v.UPSTREAM_FAILED
    assert "406" in exc.value.detail["message"]  # 平台原文透传，不吞成「未知错误」


def test_every_read_route_maps_upstream_failure_to_502(owner):
    """每个只读端点在上游失败时都必须是 502 + 同一码（不许有的 500 / 有的吞掉）。"""
    v, _account = owner
    failing = _FakeFactory(ok=False, msg="上游挂了：timeout")
    payloads = [
        (v.violation_list, v.ViolationListRequest(platform_account_id=8)),
        (v.violation_counts, v.ViolationCountsRequest(platform_account_id=8)),
        (v.violation_detail, v.ViolationDetailRequest(platform_account_id=8, violation_id="1")),
        (v.violation_appeal_record, v.ViolationDetailRequest(platform_account_id=8, violation_id="1")),
        (v.violation_evidence, v.ViolationEvidenceRequest(platform_account_id=8, violation_id="1")),
        (v.violation_meta, v.ViolationMetaRequest(platform_account_id=8)),
        (v.checkup_risks, v.CheckupRisksRequest(platform_account_id=8)),
        (v.checkup_advice, v.CheckupAdviceRequest(platform_account_id=8)),
        (v.checkup_negative_feedback, v.NegativeFeedbackRequest(platform_account_id=8, kind="quality")),
        (v.compensations, v.PagedSellerRequest(platform_account_id=8)),
        (v.arbitrations, v.PagedSellerRequest(platform_account_id=8)),
        (v.arbitration_tags, v.ArbitrationTagsRequest(platform_account_id=8)),
        (v.spot_checks, v.SpotCheckRequest(platform_account_id=8)),
        (v.exemption_overview, v.ExemptionOverviewRequest(platform_account_id=8)),
        (v.exemption_items, v.ExemptionItemsRequest(platform_account_id=8)),
    ]
    for func, payload in payloads:
        with pytest.raises(HTTPException) as exc:
            func(payload, current_user=None, db=None, adapter_factory=failing)
        assert exc.value.status_code == 502, func.__name__
        assert exc.value.detail["code"] == v.UPSTREAM_FAILED, func.__name__
        assert "timeout" in exc.value.detail["message"], func.__name__


def test_list_sends_built_body_and_echoes_query(owner):
    v, _account = owner
    factory = _FakeFactory(payload={"data": {"dtos": [{"violationId": "1"}], "counts": 1}})
    out = v.violation_list(
        v.ViolationListRequest(platform_account_id=8, statuses=[2], page_size=20),
        current_user=None,
        db=None,
        adapter_factory=factory,
    )
    name, args, _kwargs = factory.calls[0]
    assert name == "post_violation_list"
    assert args[0]["violationStatus"] == [2] and args[0]["pageSize"] == 20
    assert out["total"] == 1 and out["items"][0]["violation_id"] == "1"
    assert out["query"]["violationStatus"] == [2]
    assert out["scene"] == "violation"


def test_list_quick_overrides_other_conditions(owner):
    v, _account = owner
    factory = _FakeFactory(payload={"data": {"dtos": [], "counts": 0}})
    v.violation_list(
        v.ViolationListRequest(platform_account_id=8, quick="unread", statuses=[2], page_no=3),
        current_user=None,
        db=None,
        adapter_factory=factory,
    )
    body = factory.calls[0][1][0]
    assert body["readMark"] == 0
    assert "violationStatus" not in body  # 快捷 = 答案，不叠加高级条件
    assert body["pageNo"] == 3


def test_detail_never_calls_read_mark(owner):
    v, _account = owner
    factory = _FakeFactory(payload={"data": {"detail": {"info": {"violationId": "9"}}}})
    out = v.violation_detail(
        v.ViolationDetailRequest(platform_account_id=8, violation_id="9", tenant="TRADE", tag_id="80005677"),
        current_user=None,
        db=None,
        adapter_factory=factory,
    )
    names = [c[0] for c in factory.calls]
    assert names == ["post_violation_info"]
    assert factory.calls[0][1][0]["source"] == "violation_detail_ark"
    assert out["violation_id"] == "9"


def test_meta_merges_three_enums(owner):
    v, _account = owner
    factory = _FakeFactory(
        payload={"data": {"violationStatus": [{"violationStatus": 0, "displayName": "待处罚"}],
                          "violationEntity": [{"type": 999, "displayName": "全部"}],
                          "tags": REAL_TAGS}}
    )
    out = v.violation_meta(
        v.ViolationMetaRequest(platform_account_id=8), current_user=None, db=None, adapter_factory=factory
    )
    assert out["violation_status"] == [{"value": 0, "label": "待处罚"}]
    assert len(out["reason_tags"]) == 4
    assert len(factory.calls) == 4  # status / entity / user_tag / user_tag(SPOT_CHECK)


# --------------------------------------------------------------------- #
# G · 请求模型白名单（加字段必须同步改）
# --------------------------------------------------------------------- #
def test_all_request_models_forbid_extra_fields():
    from backend.app.api.platforms.xhs.qianfan import violation as v

    models = [
        obj
        for name, obj in vars(v).items()
        if inspect.isclass(obj) and name.endswith("Request")
    ]
    assert len(models) >= 12
    for model in models:
        assert model.model_config.get("extra") == "forbid", model.__name__
        with pytest.raises(Exception):
            model(**{"platform_account_id": 8, "__nope__": 1})


def test_unknown_field_is_422_over_http(api_client):
    """走真实 HTTP（带真登录）：未知字段 ⇒ 422（不是静默丢弃后照样 200）。

    ⚠️ 必须带 token：401 会**先于** 422 返回，那会让本用例变成「测鉴权」，
    永远看不到模型校验的结果。
    """
    api_client.post("/api/auth/register", json={"username": "vm422", "password": "testpass123"})
    login = api_client.post("/api/auth/login", json={"username": "vm422", "password": "testpass123"})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = api_client.post(
        "/api/xhs/qianfan/violation/list",
        json={"platform_account_id": 8, "__nope__": 1},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    # 正例：合法请求体不会被 422 挡（否则上一条可以靠「模型恒拦」通过）
    ok = api_client.post("/api/xhs/qianfan/violation/list", json={"platform_account_id": 8}, headers=headers)
    assert ok.status_code != 422, ok.text


# --------------------------------------------------------------------- #
# H · 零落库（D1）
# --------------------------------------------------------------------- #
def test_service_module_never_touches_orm():
    src = SERVICE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any("models" in m or "database" in m or "sqlalchemy" in m.lower() for m in imported), imported
    assert ".commit(" not in src and ".add(" not in src


def _service_body_calls() -> set[str]:
    """服务层里出现的方法名（`db.add` / `db.commit` 之类写库调用会出现在这里）。"""
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_service_makes_no_db_write_calls():
    """AST 判据（比字符串判据强）：方法体里不许出现 `add` / `commit` / `flush` / `merge` / `delete`。"""
    calls = _service_body_calls()
    for writer in ("add", "add_all", "commit", "flush", "merge", "delete"):
        assert writer not in calls, f"violation_service 调用了写库方法：{writer}"
    # 正例：判据不是空转（服务层确实有方法调用）
    assert {"get", "keys", "isinstance", "int"} & calls


def test_route_module_only_the_delete_endpoint_commits():
    """路由模块**只有删除端点**能写库（其余端点全部只读）。

    ⚠️ 2026-10-18（设计 44）：删除关联内容要落库 ⇒ 允许 `db.commit()`，
    但**只允许一次**，且必须落在 `related_delete` 端点函数里。
    读端点（列表/详情/体检…）出现任何写调用仍然是缺陷。
    """
    src = ROUTE_PATH.read_text(encoding="utf-8")
    commits = src.count("db.commit()")
    assert commits == 1, f"只允许删除端点 commit 一次，实测 {commits} 次"

    # commit 必须出现在删除端点函数体内
    fn_start = src.index("def violation_related_delete(")
    next_fn = src.find("\ndef ", fn_start + 10)
    body = src[fn_start: next_fn if next_fn > 0 else len(src)]
    assert "db.commit()" in body, "唯一的那次 commit 必须属于删除端点"

    # 读端点不许有写库调用：`db.add(` / `.add_all(` / `.flush(` 一个都不许有
    for writer in ("add", "add_all", "flush", "merge"):
        assert f".{writer}(" not in src, writer


def test_metadata_has_no_violation_tables():
    """本需求零落库：`metadata` 里不该有名字带 violation 的表（表数不因此 +1）。"""
    import backend.app.models  # noqa: F401 - 登记全部表
    from backend.app.core.database import Base

    tables = set(Base.metadata.tables)
    v_tables = sorted(t for t in tables if "violation" in t)
    assert v_tables == [], f"违规管理落库了表：{v_tables}"


def test_endpoints_do_not_add_tables_at_runtime(owner):
    """调完端点后 `metadata.tables` 一张不多（有代码在运行时 `create_all` / 动态建表就红）。"""
    import backend.app.models  # noqa: F401
    from backend.app.core.database import Base

    before = set(Base.metadata.tables)
    v, _account = owner
    factory = _FakeFactory(
        payload={"data": {"dtos": [{"violationId": "1"}], "counts": 1, "detail": {"info": {"violationId": "1"}}}}
    )
    v.violation_list(
        v.ViolationListRequest(platform_account_id=8), current_user=None, db=None, adapter_factory=factory
    )
    v.violation_detail(
        v.ViolationDetailRequest(platform_account_id=8, violation_id="1"),
        current_user=None,
        db=None,
        adapter_factory=factory,
    )
    v.violation_meta(
        v.ViolationMetaRequest(platform_account_id=8), current_user=None, db=None, adapter_factory=factory
    )
    assert set(Base.metadata.tables) == before, set(Base.metadata.tables) - before


# --------------------------------------------------------------------- #
# I · 前端同源判据（后端 + 前端各一份实现，值必须一致）
# --------------------------------------------------------------------- #
def test_frontend_quick_filter_sets_match_backend():
    """前端 8 / 4 个快捷键与后端 `QUICK_*_KEYS` 逐项一致（少一个 = 点了没条件）。"""
    src = FRONTEND_LIB.read_text(encoding="utf-8")
    violation_block = src.split("VIOLATION_QUICK_FILTERS: QuickFilter[] =")[1].split("];")[0]
    warning_block = src.split("WARNING_QUICK_FILTERS: QuickFilter[] =")[1].split("];")[0]
    front_violation = tuple(re.findall(r'key:\s*"([a-z_0-9]+)"', violation_block))
    front_warning = tuple(re.findall(r'key:\s*"([a-z_0-9]+)"', warning_block))
    assert front_violation == vs.QUICK_VIOLATION_KEYS
    assert front_warning == vs.QUICK_WARNING_KEYS
