"""无流量清理：搜索条件（CleanupQuery）与阈值共同决定候选池。

设计源：2026-09-19 千帆后台「笔记管理」页实测（`.hermes/note-delete-chain.md`
与本次千帆调研）。关键事实：

* 千帆搜索框发 ``GET /api/edith/goods-note/list?...&mix_search_word=地垫``，
  是**服务端标题过滤**（实测 5/5 标题含「地垫」）。
* 无搜索条件时候选查询是**纯本地零请求**；有 keyword 时才打那一次千帆请求。
* 阈值三项（阅读/支付订单/支付金额）全部 <= 阈值才算无流量，缺值不算。
"""

from __future__ import annotations

from backend.app.services.no_traffic_service import (
    CleanupQuery,
    TrafficThresholds,
    is_no_traffic,
)

import pytest




#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.redline
ENTRY = {"browse_num": 0, "note_pay_order_cnt": 0, "note_pay_gmv": 0}


def test_cleanup_query_from_dict_accepts_pydantic_and_plain_dict():
    """from_dict 要同时吃 pydantic 模型和裸 dict（端点传模型、内部传 dict）。"""
    import pydantic

    class Model(pydantic.BaseModel):
        keyword: str = ""
        note_type: str = ""
        published_before: int | None = None

    from_model = CleanupQuery.from_dict(Model(keyword="地垫", note_type="normal"))
    assert from_model.keyword == "地垫"
    assert from_model.note_type == "normal"

    from_dict = CleanupQuery.from_dict({"keyword": "拖鞋", "published_before": 1700000000})
    assert from_dict.keyword == "拖鞋"
    assert from_dict.published_before == 1700000000

    empty = CleanupQuery.from_dict(None)
    assert empty.keyword == ""
    assert empty.published_before is None


def test_keyword_filter_matches_substring():
    """关键词是子串匹配；中文无大小写概念，只验证子串语义。"""
    candidate = _candidate(title="卫生间地垫卡通编织亚麻")
    assert CleanupQuery(keyword="地垫").matches(candidate)
    assert CleanupQuery(keyword="卫生间").matches(candidate)
    assert not CleanupQuery(keyword="不存在").matches(candidate)
    # 空关键词 = 不限
    assert CleanupQuery(keyword="").matches(candidate)


def test_note_type_filter_only_matches_exact_type():
    candidate = _candidate(raw={"type": "normal"})
    assert CleanupQuery(note_type="normal").matches(candidate)
    assert not CleanupQuery(note_type="video").matches(candidate)
    # 空类型 = 不限
    assert CleanupQuery(note_type="").matches(candidate)


def test_published_before_excludes_newer_notes():
    candidate = _candidate(publish_time=1700000000)
    assert CleanupQuery(published_before=1800000000).matches(candidate)
    assert not CleanupQuery(published_before=1600000000).matches(candidate)


def test_published_before_ignores_notes_without_time():
    candidate = _candidate(publish_time=None)
    assert not CleanupQuery(published_before=1800000000).matches(candidate)


def test_keyword_and_type_compose():
    candidate = _candidate(title="地垫", raw={"type": "normal"})
    assert CleanupQuery(keyword="地垫", note_type="normal").matches(candidate)
    assert not CleanupQuery(keyword="地垫", note_type="video").matches(candidate)


def test_thresholds_stay_configurable_not_hardcoded():
    """用户要求：阈值可配置，不写死 0。"""
    assert TrafficThresholds().max_browse == 0
    custom = TrafficThresholds.from_dict(
        {"max_browse": 10, "max_pay_order": 2, "max_pay_gmv": 0.5}
    )
    assert custom.max_browse == 10
    assert custom.max_pay_order == 2
    assert custom.max_pay_gmv == 0.5
    # 阈值放宽后，原本不达标的笔记变成无流量
    assert not is_no_traffic({**ENTRY, "browse_num": 5}, TrafficThresholds())
    assert is_no_traffic({**ENTRY, "browse_num": 5}, custom)


def _candidate(title: str = "测试笔记", raw: dict | None = None, publish_time: int | None = None):
    from backend.app.services.no_traffic_service import CleanupCandidate

    return CleanupCandidate(
        note_id="abc",
        title=title,
        cover="",
        browse_num=0,
        browse_num_30d=0,
        likes=0,
        collects=0,
        comments=0,
        shares=0,
        pay_gmv=0.0,
        pay_order_cnt=0,
        publish_time=publish_time,
        synced_at="",
        raw=raw or {},
    )
