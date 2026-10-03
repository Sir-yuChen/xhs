"""笔记指标快照与趋势的测试（设计 17 §13.5，M2 方向 3，2026-10-03）。

覆盖三块：
1. **口径**（`note_metrics`）—— 含 v2.24 补的 `views`，以及**三种真实数据形态**
   （平铺 / `data` 下 / `note` 下）；缺值一律 0（不造假）。
2. **采集**（`collect_snapshots`）—— 覆盖语义（同日重采取最新，不是追加）、
   只采有 raw_json 的笔记（未同步 ≠ 零互动）。
3. **趋势**（`trend_series`）—— 按日聚合、不补零、租户隔离、可按单篇过滤。
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import Base
from backend.app.models.monitoring import NoteMetricSnapshot
from backend.app.models.note import Note
from backend.app.services import note_metrics_service as svc


def _session(tmp_path, name="metrics.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _note(db, *, user_id=1, note_id="n1", raw=None, title="笔记"):
    note = Note(
        user_id=user_id,
        platform_account_id=1,
        platform="xhs",
        note_id=note_id,
        title=title,
        raw_json=raw,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


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


# --------------------------------------------------------------------------- #
# ② 采集
# --------------------------------------------------------------------------- #
def test_collect_writes_snapshot_per_note(tmp_path):
    db = _session(tmp_path)
    _note(db, note_id="a", raw={"likes": 1, "view_count": 10})
    _note(db, note_id="b", raw={"likes": 2, "view_count": 20})

    written = svc.collect_snapshots(db, user_id=1, stat_date="2026-10-03")
    assert written == 2
    rows = db.query(NoteMetricSnapshot).all()
    assert len(rows) == 2
    assert all(row.stat_date == "2026-10-03" for row in rows)
    assert sorted(row.likes for row in rows) == [1, 2]
    assert sorted(row.views for row in rows) == [10, 20]


def test_collect_overwrites_same_day_not_appends(tmp_path):
    """同日重采 ⇒ **覆盖**（取最新值），不是追加（否则趋势线成锯齿）。"""
    db = _session(tmp_path)
    note = _note(db, note_id="a", raw={"likes": 1})

    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-03")
    # 再采一次（模拟当天晚些时候又同步了一次，值涨了）
    note.raw_json = {"likes": 9}
    db.commit()
    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-03")

    rows = db.query(NoteMetricSnapshot).all()
    assert len(rows) == 1, "同日重复采集不应产生第二行（唯一键 + upsert）"
    assert rows[0].likes == 9, "应取最新值，而不是保留第一次的旧值"


def test_collect_different_days_accumulate(tmp_path):
    """不同日 ⇒ 各自一行（趋势的基础）。"""
    db = _session(tmp_path)
    _note(db, note_id="a", raw={"likes": 1})
    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-01")
    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-02")
    assert db.query(NoteMetricSnapshot).count() == 2


def test_collect_skips_notes_without_raw_json(tmp_path):
    """**没同步过的笔记不采**（缺值口径：未同步 ≠ 零互动）。

    ⚠️ 变异守护：去掉 `raw_json.is_not(None)` 条件 ⇒ 本用例必红 ——
    那会把「还没同步」写成「互动为 0」，在仪表盘上表现为真实的零互动。
    """
    db = _session(tmp_path)
    _note(db, note_id="synced", raw={"likes": 1})
    _note(db, note_id="never_synced", raw=None)
    # ⚠️ 空对象也是「无指标可采」——SQLAlchemy 把它存成字符串 `'{}'`（不是 NULL），
    # 所以判据要按**实际存储形态**写（本用例守住这一点）。
    _note(db, note_id="empty_obj", raw={})

    written = svc.collect_snapshots(db, user_id=1, stat_date="2026-10-03")
    assert written == 1, "只应采有真实 raw_json 的那篇"
    assert [row.note_pk for row in db.query(NoteMetricSnapshot).all()] == [
        db.query(Note).filter_by(note_id="synced").one().id
    ]


def test_collect_is_scoped_to_user(tmp_path):
    """只采**本用户**的笔记（租户隔离）。"""
    db = _session(tmp_path)
    _note(db, user_id=1, note_id="mine", raw={"likes": 1})
    _note(db, user_id=2, note_id="others", raw={"likes": 2})

    written = svc.collect_snapshots(db, user_id=1, stat_date="2026-10-03")
    assert written == 1
    assert db.query(NoteMetricSnapshot).one().user_id == 1


# --------------------------------------------------------------------------- #
# ③ 趋势
# --------------------------------------------------------------------------- #
def test_trend_aggregates_by_day(tmp_path):
    db = _session(tmp_path)
    _note(db, note_id="a", raw={"likes": 1})
    _note(db, note_id="b", raw={"likes": 2})
    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-01")
    _note(db, note_id="c", raw={"likes": 10})
    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-02")

    series = svc.trend_series(db, user_id=1, days=0)
    assert [point["stat_date"] for point in series] == ["2026-10-01", "2026-10-02"]
    assert series[0]["likes"] == 3   # 1 + 2
    assert series[1]["likes"] == 13  # 1 + 2 + 10


def test_trend_does_not_fill_missing_days(tmp_path):
    """**不补零** —— 缺失的天直接不出现（补零会把「没采」画成「掉到 0」）。

    ⚠️ 变异守护：给它补零（如中间插一条 0 值）⇒ 本用例必红。
    """
    db = _session(tmp_path)
    _note(db, note_id="a", raw={"likes": 1})
    svc.collect_snapshots(db, user_id=1, stat_date="2026-09-01")
    svc.collect_snapshots(db, user_id=1, stat_date="2026-09-30")

    series = svc.trend_series(db, user_id=1, days=0)
    assert len(series) == 2, "只返回有快照的两天，中间不补零"
    assert {p["stat_date"] for p in series} == {"2026-09-01", "2026-09-30"}


def test_trend_can_filter_single_note(tmp_path):
    db = _session(tmp_path)
    note_a = _note(db, note_id="a", raw={"likes": 1})
    _note(db, note_id="b", raw={"likes": 100})
    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-03")

    series = svc.trend_series(db, user_id=1, days=0, note_pk=note_a.id)
    assert len(series) == 1 and series[0]["likes"] == 1


def test_trend_is_scoped_to_user(tmp_path):
    """趋势只含本用户（租户隔离 —— 与全站同一把尺子）。"""
    db = _session(tmp_path)
    _note(db, user_id=1, note_id="mine", raw={"likes": 1})
    _note(db, user_id=2, note_id="others", raw={"likes": 999})
    svc.collect_snapshots(db, user_id=1, stat_date="2026-10-03")
    svc.collect_snapshots(db, user_id=2, stat_date="2026-10-03")

    series = svc.trend_series(db, user_id=1, days=0)
    assert series[0]["likes"] == 1, "不能把别人的数据算进来"


def test_trend_empty_when_no_snapshots(tmp_path):
    """没有任何快照 ⇒ 空序列（不是编一串 0）。"""
    db = _session(tmp_path)
    _note(db, note_id="a", raw={"likes": 1})
    assert svc.trend_series(db, user_id=1, days=30) == []


def test_stat_date_is_shanghai_day():
    """统计日按**上海时区**（用 UTC 会让凌晨采集的笔记落到前一天）。"""
    from datetime import datetime

    assert svc.stat_date_of(datetime(2026, 10, 3, 23, 30)) == "2026-10-03"
