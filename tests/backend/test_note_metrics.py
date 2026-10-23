"""笔记指标**口径**的测试（设计 17 §13.5，M2 方向 3，2026-10-03）。

覆盖：**口径**（`note_metrics`）—— 含 `views`，以及**三种真实数据形态**
（平铺 / `data` 下 / `note` 下）；缺值一律 0（不造假）。

⚠️ 2026-10-13 用户口径移除指标快照子系统：原「② 采集（`collect_snapshots`）」
与「③ 趋势（`trend_series`）」两段**随实现一并移除**（快照/趋势端点、定时任务、
服务函数已删）。口径读取仍被榜单/排名使用，故本段保留。
"""

from __future__ import annotations

from backend.app.models.note import Note
from backend.app.services import note_metrics_service as svc

import pytest




#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract

# --------------------------------------------------------------------------- #
# ① 口径
# --------------------------------------------------------------------------- #
def test_metrics_include_views():
    """`views`（阅读）必须被解析 —— v2.24 补的缺口。

    ⚠️ 变异守护：把 `views` 从 `METRIC_KEYS` 里删掉 ⇒ 本用例必红。
    没有阅读量就算不出互动率/点击率（那是笔记监控最基本的分母）。
    """
    note = Note(raw_json={"likes": 3, "view_count": 120})
    metrics = svc.note_metrics(note)
    assert metrics["views"] == 120
    # ⚠️ 阅读**不进**互动和（它是分母，不是互动）
    assert metrics["engagement"] == 3


def test_metrics_support_nested_shapes():
    """三种数据形态都能解析（平铺 / `data` 下 / `note` 下）—— 真机实测都存在。

    ⚠️ 变异守护：去掉 `_raw_value` 里的嵌套分支 ⇒ 后两条断言必红。
    这是最容易在重构中丢掉的兼容面（下沉口径时差点丢掉，已核回）。
    """
    flat = svc.note_metrics(Note(raw_json={"likes": 1, "collects": 2}))
    assert flat["likes"] == 1 and flat["collects"] == 2

    wrapped = svc.note_metrics(Note(raw_json={"data": {"likes": 5, "view_count": 9}}))
    assert wrapped["likes"] == 5 and wrapped["views"] == 9

    nested = svc.note_metrics(Note(raw_json={"note": {"likes": 7, "comments": 3}}))
    assert nested["likes"] == 7 and nested["comments"] == 3


def test_metrics_missing_values_are_zero_not_fabricated():
    """解析不到 ⇒ `0`（缺值口径：不造假、不猜）。"""
    metrics = svc.note_metrics(Note(raw_json={}))
    assert metrics["likes"] == 0 and metrics["views"] == 0 and metrics["engagement"] == 0

    # 非数值的脏数据也不崩、不算数
    dirty = svc.note_metrics(Note(raw_json={"likes": "abc", "collects": None}))
    assert dirty["likes"] == 0 and dirty["collects"] == 0

    # 字符串数字能解析（平台有时回字符串）
    text = svc.note_metrics(Note(raw_json={"likes": "12", "shares": "1,024"}))
    assert text["likes"] == 12 and text["shares"] == 1024


def test_engagement_is_sum_of_four_only():
    """`engagement` = 四项互动之和（**不含 views**）。"""
    note = Note(raw_json={"likes": 1, "collects": 2, "comments": 3, "shares": 4, "view_count": 999})
    metrics = svc.note_metrics(note)
    assert metrics["engagement"] == 1 + 2 + 3 + 4
